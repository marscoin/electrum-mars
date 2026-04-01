I now have a comprehensive understanding of the codebase. Let me write the full implementation specification.

---

# BTC-MARS Atomic Swap Plugin: Complete Implementation Specification

## 1. Architecture Overview

The implementation follows the established Electrum-Mars plugin pattern. The atomic swap feature is built as a plugin at `electrum_mars/plugins/atomic_swap/` with core logic separated from the Qt GUI layer, mirroring how `cosigner_pool` and `labels` are structured. A standalone `atomic_swap_htlc.py` module in the main package provides HTLC primitives shared between the plugin and potential future non-plugin use.

The system is split into these logical layers:

```
Layer 1: HTLC Primitives (atomic_swap_htlc.py)      -- Script construction, signing, claiming
Layer 2: Bitcoin Chain Monitor (btc_monitor.py)       -- mempool.space polling for BTC HTLCs  
Layer 3: Swap State Machine (swap_engine.py)          -- State transitions, persistence, timeout handling
Layer 4: Order Book Protocol (orderbook.py)           -- P2P offer relay via ElectrumX extension
Layer 5: Reputation System (reputation.py)            -- Per-peer success tracking
Layer 6: Plugin Glue (atomic_swap/__init__.py + qt.py)-- Hook into Electrum-Mars plugin system
Layer 7: Qt GUI (atomic_swap/qt.py)                   -- Tab, dialogs, swap wizard
```

---

## 2. File Structure

```
electrum_mars/
    atomic_swap_htlc.py          # HTLC script creation, verification, signing, claiming
    btc_monitor.py               # Bitcoin chain monitoring via mempool.space REST API
    plugins/
        atomic_swap/
            __init__.py          # Plugin metadata
            swap_engine.py       # Swap state machine + persistence
            orderbook.py         # Order book protocol (ElectrumX RPC extension)
            reputation.py        # Peer reputation tracking
            qt.py                # Qt GUI: tab, offer list, swap wizard
```

---

## 3. Module-by-Module Specification

### 3.1 `electrum_mars/atomic_swap_htlc.py` -- HTLC Primitives

This module is modeled directly after `submarine_swaps.py` (lines 38-79, 108-131, 610-621). It reuses the same `construct_script`, `construct_witness`, `script_to_p2wsh`, and `create_claim_tx` patterns but adapted for cross-chain atomic swaps.

```python
"""
Cross-chain HTLC primitives for BTC <-> MARS atomic swaps.

Uses the same HTLC pattern as submarine_swaps.py but parameterized
for either chain. The script is:

  OP_IF
    OP_HASH160 <hash160(preimage)> OP_EQUALVERIFY
    <recipient_pubkey> OP_CHECKSIG
  OP_ELSE
    <locktime> OP_CHECKLOCKTIMEVERIFY OP_DROP
    <sender_pubkey> OP_CHECKSIG
  OP_ENDIF

This is a slight rearrangement from the submarine_swaps template
(which puts OP_HASH160 check first then branches). We use OP_IF
branch for claim-with-preimage, OP_ELSE branch for refund-after-timelock.
Both are standard and functionally equivalent.
"""

import os
from typing import Optional, Tuple, Union
from enum import Enum

import attr

from .crypto import sha256, hash_160
from .ecc import ECPrivkey
from .bitcoin import (
    opcodes, construct_script, construct_witness,
    script_to_p2wsh, push_script, p2wsh_nested_script,
    add_number_to_script, script_num_to_hex,
    is_segwit_address, hash_to_segwit_addr,
    address_to_scripthash
)
from .transaction import (
    PartialTxInput, PartialTxOutput, PartialTransaction,
    Transaction, TxOutpoint
)
from .json_db import StoredObject
from .lnutil import hex_to_bytes
from .logging import Logger


# Bitcoin mainnet constants for P2WSH address generation
BTC_SEGWIT_HRP = "bc"
BTC_TESTNET_SEGWIT_HRP = "tb"
BTC_REGTEST_SEGWIT_HRP = "bcrt"

# Timelock constants (in blocks)
BTC_TIMELOCK_BLOCKS = 36     # ~6 hours at 10 min/block
MARS_TIMELOCK_BLOCKS = 96    # ~4 hours at 2.5 min/block (Marscoin has ~2.5 min blocks)
# MARS must be shorter so the maker claims BTC before MARS refund becomes possible


class Chain(Enum):
    MARS = "mars"
    BTC = "btc"


def generate_preimage() -> Tuple[bytes, bytes]:
    """Generate a random 32-byte preimage and its hash160.
    
    Returns:
        (preimage, hash160_of_preimage)
    """
    preimage = os.urandom(32)
    h = hash_160(preimage)
    return preimage, h


def generate_keypair() -> Tuple[bytes, bytes]:
    """Generate an ephemeral keypair for HTLC use.
    
    Returns:
        (privkey_bytes, compressed_pubkey_bytes)
    """
    privkey = os.urandom(32)
    pubkey = ECPrivkey(privkey).get_public_key_bytes(compressed=True)
    return privkey, pubkey


def create_htlc_script(
    payment_hash160: bytes,
    recipient_pubkey: bytes,
    sender_pubkey: bytes,
    locktime: int,
) -> str:
    """Build the HTLC redeem script (witness script).
    
    Claim path (recipient reveals preimage):
        OP_IF
            OP_HASH160 <payment_hash160> OP_EQUALVERIFY
            <recipient_pubkey> OP_CHECKSIG
        OP_ELSE
            <locktime> OP_CHECKLOCKTIMEVERIFY OP_DROP
            <sender_pubkey> OP_CHECKSIG
        OP_ENDIF
    
    Args:
        payment_hash160: RIPEMD160(SHA256(preimage)), 20 bytes
        recipient_pubkey: compressed pubkey of party who can claim with preimage
        sender_pubkey: compressed pubkey of party who funded (can refund after locktime)
        locktime: absolute block height for refund timelock
    
    Returns:
        Hex-encoded script
    """
    assert len(payment_hash160) == 20
    assert len(recipient_pubkey) == 33
    assert len(sender_pubkey) == 33
    
    return construct_script([
        opcodes.OP_IF,
            opcodes.OP_HASH160,
            payment_hash160,
            opcodes.OP_EQUALVERIFY,
            recipient_pubkey,
            opcodes.OP_CHECKSIG,
        opcodes.OP_ELSE,
            locktime,
            opcodes.OP_CHECKLOCKTIMEVERIFY,
            opcodes.OP_DROP,
            sender_pubkey,
            opcodes.OP_CHECKSIG,
        opcodes.OP_ENDIF,
    ])


def htlc_script_to_p2wsh_address(script_hex: str, chain: Chain, testnet: bool = False) -> str:
    """Convert HTLC witness script to a P2WSH address for the specified chain.
    
    For MARS: uses constants.net.SEGWIT_HRP (e.g., "mrs")
    For BTC: uses "bc" (mainnet) or "tb" (testnet) or "bcrt" (regtest)
    """
    from .crypto import sha256 as crypto_sha256
    from .util import bfh
    
    script_bytes = bfh(script_hex)
    script_hash = crypto_sha256(script_bytes)
    
    if chain == Chain.MARS:
        from . import constants
        hrp = constants.net.SEGWIT_HRP
    elif chain == Chain.BTC:
        if testnet:
            hrp = BTC_TESTNET_SEGWIT_HRP
        else:
            hrp = BTC_SEGWIT_HRP
    else:
        raise ValueError(f"Unknown chain: {chain}")
    
    return hash_to_segwit_addr(script_hash, witver=0, net=None) 
    # Note: for BTC, we cannot use the Marscoin segwit_addr encoding directly.
    # We must use segwit_addr.encode_segwit_address with the BTC HRP.


def htlc_script_to_btc_p2wsh(script_hex: str, testnet: bool = False) -> str:
    """Convert HTLC witness script to a Bitcoin P2WSH address.
    
    Uses Bitcoin's bech32 encoding (HRP = "bc" or "tb"), NOT Marscoin's.
    """
    from . import segwit_addr
    from .util import bfh
    from .crypto import sha256 as crypto_sha256
    
    script_bytes = bfh(script_hex)
    script_hash = crypto_sha256(script_bytes)
    hrp = BTC_TESTNET_SEGWIT_HRP if testnet else BTC_SEGWIT_HRP
    addr = segwit_addr.encode_segwit_address(hrp, 0, script_hash)
    assert addr is not None
    return addr


def htlc_script_to_mars_p2wsh(script_hex: str) -> str:
    """Convert HTLC witness script to a Marscoin P2WSH address.
    
    Uses Marscoin's bech32 encoding via constants.net.SEGWIT_HRP.
    """
    return script_to_p2wsh(script_hex)


def verify_htlc_script(
    script_hex: str,
    expected_hash160: bytes,
    expected_recipient_pubkey: bytes,
    expected_sender_pubkey: bytes,
    expected_locktime: int,
) -> bool:
    """Verify an HTLC script matches expected parameters.
    
    Returns True if all parameters match, raises Exception with detail otherwise.
    """
    from .transaction import script_GetOp
    from .util import bfh
    
    script_bytes = bfh(script_hex)
    parsed = list(script_GetOp(script_bytes))
    
    # Reconstruct expected script and compare
    expected = create_htlc_script(
        expected_hash160,
        expected_recipient_pubkey,
        expected_sender_pubkey,
        expected_locktime,
    )
    if script_hex != expected:
        raise Exception("HTLC script does not match expected parameters")
    return True


def create_claim_tx(
    *,
    txin: PartialTxInput,
    witness_script: bytes,
    preimage: bytes,
    address: str,
    amount_sat: int,
) -> PartialTransaction:
    """Create a transaction to claim an HTLC output by revealing the preimage.
    
    The witness stack for claiming is: <sig> <preimage> <1> <witness_script>
    (The <1> selects the OP_IF branch.)
    
    Modeled after submarine_swaps.create_claim_tx (line 108-131).
    """
    txin.script_type = 'p2wsh'
    txin.script_sig = b''
    txin.witness_script = witness_script
    txout = PartialTxOutput.from_address_and_value(address, amount_sat)
    tx = PartialTransaction.from_io([txin], [txout], version=2, locktime=0)
    tx.set_rbf(True)
    return tx


def create_refund_tx(
    *,
    txin: PartialTxInput,
    witness_script: bytes,
    address: str,
    amount_sat: int,
    locktime: int,
) -> PartialTransaction:
    """Create a transaction to refund an HTLC output after timelock expiry.
    
    The witness stack for refunding is: <sig> <0> <witness_script>
    (The <0> selects the OP_ELSE branch.)
    """
    txin.script_type = 'p2wsh'
    txin.script_sig = b''
    txin.witness_script = witness_script
    txout = PartialTxOutput.from_address_and_value(address, amount_sat)
    tx = PartialTransaction.from_io([txin], [txout], version=2, locktime=locktime)
    tx.set_rbf(True)
    return tx


def sign_htlc_claim(
    tx: PartialTransaction,
    privkey: bytes,
    preimage: bytes,
    witness_script: bytes,
) -> None:
    """Sign a claim transaction (preimage path).
    
    Sets the witness to: <signature> <preimage> <OP_TRUE> <witness_script>
    
    Modeled after SwapManager.sign_tx (line 610-621).
    """
    txin = tx.inputs()[0]
    txin.script_type = 'p2wsh'
    txin.script_sig = b''
    txin.witness_script = witness_script
    sig = bytes.fromhex(tx.sign_txin(0, privkey))
    witness = construct_witness([sig, preimage, bytes([1]), witness_script])
    txin.witness = bytes.fromhex(witness)


def sign_htlc_refund(
    tx: PartialTransaction,
    privkey: bytes,
    witness_script: bytes,
) -> None:
    """Sign a refund transaction (timelock path).
    
    Sets the witness to: <signature> <OP_FALSE> <witness_script>
    """
    txin = tx.inputs()[0]
    txin.script_type = 'p2wsh'
    txin.script_sig = b''
    txin.witness_script = witness_script
    sig = bytes.fromhex(tx.sign_txin(0, privkey))
    witness = construct_witness([sig, 0, witness_script])
    txin.witness = bytes.fromhex(witness)


@attr.s
class AtomicSwapData(StoredObject):
    """Persistent swap state stored in the wallet database.
    
    Modeled after SwapData (submarine_swaps.py line 83-97).
    """
    swap_id = attr.ib(type=str)                    # unique identifier (hash of offer)
    role = attr.ib(type=str)                        # "maker" or "taker"
    direction = attr.ib(type=str)                   # "sell_mars" or "buy_mars"
    
    mars_amount_sat = attr.ib(type=int)             # MARS amount in satoshis
    btc_amount_sat = attr.ib(type=int)              # BTC amount in satoshis
    
    preimage = attr.ib(type=bytes, converter=hex_to_bytes)
    payment_hash160 = attr.ib(type=bytes, converter=hex_to_bytes)
    
    # MARS side
    mars_privkey = attr.ib(type=bytes, converter=hex_to_bytes)
    mars_pubkey = attr.ib(type=bytes, converter=hex_to_bytes)
    mars_peer_pubkey = attr.ib(type=bytes, converter=hex_to_bytes)
    mars_htlc_script = attr.ib(type=str)            # hex
    mars_htlc_address = attr.ib(type=str)
    mars_locktime = attr.ib(type=int)
    mars_funding_txid = attr.ib(type=Optional[str])
    mars_claim_txid = attr.ib(type=Optional[str])
    
    # BTC side
    btc_privkey = attr.ib(type=bytes, converter=hex_to_bytes)
    btc_pubkey = attr.ib(type=bytes, converter=hex_to_bytes)
    btc_peer_pubkey = attr.ib(type=bytes, converter=hex_to_bytes)
    btc_htlc_script = attr.ib(type=str)             # hex
    btc_htlc_address = attr.ib(type=str)
    btc_locktime = attr.ib(type=int)
    btc_funding_txid = attr.ib(type=Optional[str])
    btc_claim_txid = attr.ib(type=Optional[str])
    
    # State
    state = attr.ib(type=str)                       # see SwapState enum
    created_at = attr.ib(type=float)                # time.time()
    completed_at = attr.ib(type=Optional[float])
    peer_id = attr.ib(type=str)                     # peer identifier for reputation
    
    receive_address = attr.ib(type=str)             # where to receive claimed coins
```

### 3.2 `electrum_mars/btc_monitor.py` -- Bitcoin Chain Monitoring

Since Electrum-Mars connects to Marscoin ElectrumX servers (not Bitcoin ones), we need an independent way to monitor the Bitcoin blockchain. This module polls the mempool.space REST API.

```python
"""
Bitcoin blockchain monitor using mempool.space REST API.

Since Electrum-Mars talks to Marscoin ElectrumX servers,
we cannot use the built-in ElectrumX subscription mechanism
to watch Bitcoin addresses. Instead, we poll mempool.space.

API reference: https://mempool.space/docs/api/rest
"""

import asyncio
import json
from typing import Optional, Dict, List, Callable, Awaitable, Tuple
from decimal import Decimal

from .logging import Logger
from .util import make_aiohttp_session, log_exceptions


# API endpoints
MEMPOOL_MAINNET = "https://mempool.space/api"
MEMPOOL_TESTNET = "https://mempool.space/testnet/api"
MEMPOOL_SIGNET  = "https://mempool.space/signet/api"


class BtcUtxo:
    """Represents a Bitcoin UTXO found at an HTLC address."""
    def __init__(self, txid: str, vout: int, value_sat: int, 
                 confirmed: bool, block_height: Optional[int]):
        self.txid = txid
        self.vout = vout
        self.value_sat = value_sat
        self.confirmed = confirmed
        self.block_height = block_height


class BtcChainMonitor(Logger):
    """Monitors Bitcoin addresses for HTLC funding and claim transactions.
    
    Polls mempool.space at configurable intervals. Fires callbacks
    when funding is detected or when a claim reveals a preimage.
    """
    
    POLL_INTERVAL_SECONDS = 15  # check every 15 seconds
    
    def __init__(self, *, testnet: bool = False):
        Logger.__init__(self)
        self.api_url = MEMPOOL_TESTNET if testnet else MEMPOOL_MAINNET
        self._watches: Dict[str, dict] = {}  # address -> {callback, ...}
        self._running = False
        self._poll_task: Optional[asyncio.Task] = None
    
    def start(self, loop: asyncio.AbstractEventLoop):
        """Start the polling loop."""
        self._running = True
        self._poll_task = asyncio.run_coroutine_threadsafe(
            self._poll_loop(), loop
        )
    
    def stop(self):
        """Stop the polling loop."""
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
    
    def watch_address(
        self,
        address: str,
        on_funded: Callable[[BtcUtxo], Awaitable[None]],
        on_spent: Callable[[str, str], Awaitable[None]],  # (spending_txid, raw_tx_hex)
    ):
        """Start watching a Bitcoin address for activity.
        
        Args:
            address: Bitcoin bech32 address (the HTLC P2WSH address)
            on_funded: called when coins arrive at the address
            on_spent: called when coins are spent from the address
        """
        self._watches[address] = {
            'on_funded': on_funded,
            'on_spent': on_spent,
            'last_known_utxos': set(),
            'funded': False,
            'spent': False,
        }
    
    def unwatch_address(self, address: str):
        """Stop watching a Bitcoin address."""
        self._watches.pop(address, None)
    
    @log_exceptions
    async def _poll_loop(self):
        """Main polling loop."""
        while self._running:
            for address in list(self._watches.keys()):
                try:
                    await self._check_address(address)
                except Exception as e:
                    self.logger.warning(f"Error checking BTC address {address}: {e}")
            await asyncio.sleep(self.POLL_INTERVAL_SECONDS)
    
    async def _check_address(self, address: str):
        """Check a single Bitcoin address for changes."""
        watch = self._watches.get(address)
        if not watch:
            return
        
        # Get UTXOs at address
        utxos = await self._get_utxos(address)
        
        # Check for new funding
        if utxos and not watch['funded']:
            watch['funded'] = True
            for utxo in utxos:
                await watch['on_funded'](utxo)
        
        # Check if previously-funded UTXOs have been spent
        if watch['funded'] and not watch['spent']:
            if not utxos:
                # UTXOs gone -- someone spent them. Get spending tx.
                txs = await self._get_address_txs(address)
                for tx_info in txs:
                    if self._is_spending_tx(tx_info, address):
                        watch['spent'] = True
                        raw_tx = await self._get_raw_tx(tx_info['txid'])
                        await watch['on_spent'](tx_info['txid'], raw_tx)
                        break
    
    async def _get_utxos(self, address: str) -> List[BtcUtxo]:
        """GET /address/:address/utxo"""
        url = f"{self.api_url}/address/{address}/utxo"
        async with make_aiohttp_session(None, timeout=30) as session:
            async with session.get(url) as resp:
                resp.raise_for_status()
                data = json.loads(await resp.text())
                return [
                    BtcUtxo(
                        txid=item['txid'],
                        vout=item['vout'],
                        value_sat=item['value'],
                        confirmed=item.get('status', {}).get('confirmed', False),
                        block_height=item.get('status', {}).get('block_height'),
                    )
                    for item in data
                ]
    
    async def _get_address_txs(self, address: str) -> List[dict]:
        """GET /address/:address/txs"""
        url = f"{self.api_url}/address/{address}/txs"
        async with make_aiohttp_session(None, timeout=30) as session:
            async with session.get(url) as resp:
                resp.raise_for_status()
                return json.loads(await resp.text())
    
    async def _get_raw_tx(self, txid: str) -> str:
        """GET /tx/:txid/hex"""
        url = f"{self.api_url}/tx/{txid}/hex"
        async with make_aiohttp_session(None, timeout=30) as session:
            async with session.get(url) as resp:
                resp.raise_for_status()
                return await resp.text()
    
    async def _get_current_block_height(self) -> int:
        """GET /blocks/tip/height"""
        url = f"{self.api_url}/blocks/tip/height"
        async with make_aiohttp_session(None, timeout=30) as session:
            async with session.get(url) as resp:
                resp.raise_for_status()
                return int(await resp.text())
    
    async def broadcast_tx(self, raw_tx_hex: str) -> str:
        """POST /tx -- broadcast a raw Bitcoin transaction.
        
        Returns the txid.
        """
        url = f"{self.api_url}/tx"
        async with make_aiohttp_session(None, timeout=30) as session:
            async with session.post(url, data=raw_tx_hex) as resp:
                resp.raise_for_status()
                return await resp.text()
    
    def _is_spending_tx(self, tx_info: dict, address: str) -> bool:
        """Check if a transaction spends from the given address."""
        for vin in tx_info.get('vin', []):
            if vin.get('prevout', {}).get('scriptpubkey_address') == address:
                return True
        return False
    
    @staticmethod
    def extract_preimage_from_witness(raw_tx_hex: str) -> Optional[bytes]:
        """Extract the preimage from a claim transaction's witness data.
        
        When someone claims an HTLC by revealing the preimage, the witness
        stack is: <sig> <preimage> <1> <witness_script>
        
        The preimage is the second item (index 1), a 32-byte value.
        """
        # Parse raw tx to get witness data
        # We look for a 32-byte witness item that, when hashed with
        # HASH160, matches the payment hash in the witness script.
        from .transaction import Transaction
        tx = Transaction(raw_tx_hex)
        for txin in tx.inputs():
            witness_bytes = txin.witness
            if witness_bytes:
                # Parse witness stack
                # witness format: <count> <item1_len> <item1> <item2_len> <item2> ...
                # We need to find the 32-byte preimage
                # For our HTLC, witness is: <sig> <preimage> <1> <witness_script>
                # The preimage is at index 1
                # Simple heuristic: look for any 32-byte item
                # that is not a signature (sigs are 71-73 bytes)
                pass  # actual parsing logic in implementation
        return None
```

### 3.3 `electrum_mars/plugins/atomic_swap/__init__.py` -- Plugin Metadata

```python
from electrum_mars.i18n import _

fullname = _('Atomic Swap')
description = ' '.join([
    _("Trade BTC for MARS and vice versa using trustless atomic swaps."),
    _("No intermediary, no custody, no KYC."),
    _("Uses hash time-locked contracts (HTLCs) to ensure either both parties receive their coins or neither does."),
])
available_for = ['qt']
```

### 3.4 `electrum_mars/plugins/atomic_swap/swap_engine.py` -- Swap State Machine

```python
"""
Atomic swap state machine.

Manages the lifecycle of a swap from offer acceptance through
HTLC creation, monitoring, claiming, and completion.

State machine (from perspective of MARS seller / "maker"):

    CREATED ──> MARS_HTLC_FUNDED ──> WAITING_BTC_HTLC ──> BTC_HTLC_VERIFIED
        │              │                    │                      │
        │              │                    │                      v
        │              │                    │              CLAIMING_BTC
        │              │                    │                      │
        │              │                    │                      v
        │              │                    │              COMPLETED
        │              │                    │
        v              v                    v
    CANCELLED    REFUNDING_MARS      REFUNDING_MARS

State machine (from perspective of BTC sender / "taker"):

    ACCEPTED ──> WAITING_MARS_HTLC ──> MARS_HTLC_VERIFIED ──> BTC_HTLC_FUNDED
        │               │                      │                     │
        │               │                      │                     v
        │               │                      │            WAITING_PREIMAGE
        │               │                      │                     │
        │               │                      │                     v
        │               │                      │            CLAIMING_MARS
        │               │                      │                     │
        │               │                      │                     v
        │               │                      │              COMPLETED
        │               │                      │
        v               v                      v
    CANCELLED    REFUNDING_BTC          REFUNDING_BTC
"""

import asyncio
import time
from enum import Enum
from typing import TYPE_CHECKING, Optional, Dict

from electrum_mars.logging import Logger
from electrum_mars.util import log_exceptions
from electrum_mars.atomic_swap_htlc import (
    AtomicSwapData, Chain, generate_preimage, generate_keypair,
    create_htlc_script, htlc_script_to_mars_p2wsh, htlc_script_to_btc_p2wsh,
    create_claim_tx, create_refund_tx, sign_htlc_claim, sign_htlc_refund,
    verify_htlc_script, BTC_TIMELOCK_BLOCKS, MARS_TIMELOCK_BLOCKS,
)
from electrum_mars.btc_monitor import BtcChainMonitor, BtcUtxo

if TYPE_CHECKING:
    from electrum_mars.wallet import Abstract_Wallet
    from electrum_mars.network import Network
    from electrum_mars.lnwatcher import LNWalletWatcher


class SwapState(Enum):
    # Common
    CREATED = "created"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    EXPIRED = "expired"
    
    # Maker (selling MARS) states
    MARS_HTLC_FUNDED = "mars_htlc_funded"
    WAITING_BTC_HTLC = "waiting_btc_htlc"
    BTC_HTLC_VERIFIED = "btc_htlc_verified"
    CLAIMING_BTC = "claiming_btc"
    REFUNDING_MARS = "refunding_mars"
    
    # Taker (buying MARS) states
    ACCEPTED = "accepted"
    WAITING_MARS_HTLC = "waiting_mars_htlc"
    MARS_HTLC_VERIFIED = "mars_htlc_verified"
    BTC_HTLC_FUNDED = "btc_htlc_funded"
    WAITING_PREIMAGE = "waiting_preimage"
    CLAIMING_MARS = "claiming_mars"
    REFUNDING_BTC = "refunding_btc"


class SwapEngine(Logger):
    """Manages the full lifecycle of atomic swaps.
    
    Integrates with:
    - Wallet (for MARS HTLC funding/claiming)
    - LNWalletWatcher (for monitoring MARS HTLC addresses)
    - BtcChainMonitor (for monitoring BTC HTLC addresses)
    - OrderBook (for offer coordination)
    """
    
    def __init__(self, *, wallet: 'Abstract_Wallet', config):
        Logger.__init__(self)
        self.wallet = wallet
        self.config = config
        self.network: Optional['Network'] = None
        self.lnwatcher: Optional['LNWalletWatcher'] = None
        self.btc_monitor: Optional[BtcChainMonitor] = None
        
        # Persistent storage via wallet DB
        self.swaps: Dict[str, AtomicSwapData] = wallet.db.get_dict('atomic_swaps')
    
    def start_network(self, *, network: 'Network', lnwatcher: 'LNWalletWatcher'):
        """Called when network becomes available."""
        self.network = network
        self.lnwatcher = lnwatcher
        
        # Start BTC chain monitor
        testnet = network.config.get('testnet', False)
        self.btc_monitor = BtcChainMonitor(testnet=testnet)
        self.btc_monitor.start(network.asyncio_loop)
        
        # Resume any in-progress swaps
        for swap_id, swap in self.swaps.items():
            if swap.state not in (
                SwapState.COMPLETED.value, 
                SwapState.CANCELLED.value, 
                SwapState.EXPIRED.value
            ):
                self._resume_swap(swap)
    
    def stop(self):
        """Clean shutdown."""
        if self.btc_monitor:
            self.btc_monitor.stop()
    
    # ── Maker flow (selling MARS for BTC) ──
    
    async def initiate_swap_as_maker(
        self,
        mars_amount_sat: int,
        btc_amount_sat: int,
        peer_id: str,
        peer_btc_pubkey: bytes,
        password: str,
    ) -> AtomicSwapData:
        """Maker initiates swap: generate preimage, create MARS HTLC, fund it.
        
        1. Generate preimage and hash
        2. Generate ephemeral keypairs for both chains
        3. Create MARS HTLC script
        4. Fund MARS HTLC from wallet
        5. Return swap data (including hash, pubkeys) to send to taker
        """
        preimage, payment_hash160 = generate_preimage()
        mars_privkey, mars_pubkey = generate_keypair()
        btc_privkey, btc_pubkey = generate_keypair()
        
        current_height = self.network.get_local_height()
        mars_locktime = current_height + MARS_TIMELOCK_BLOCKS
        
        # Peer will create BTC HTLC where we (maker) are recipient
        # For MARS HTLC: peer is recipient (they claim with preimage after we reveal it on BTC)
        # Wait -- re-read the flow:
        # Maker has MARS. Taker has BTC.
        # Maker creates MARS HTLC: taker can claim with preimage, maker can refund after timeout
        # Taker creates BTC HTLC: maker can claim with preimage, taker can refund after timeout
        # Maker claims BTC (reveals preimage on BTC chain)
        # Taker extracts preimage from BTC chain, claims MARS
        
        mars_htlc_script = create_htlc_script(
            payment_hash160=payment_hash160,
            recipient_pubkey=peer_btc_pubkey,  # taker claims MARS with preimage
            # Wait, wrong. For MARS HTLC, the taker needs a MARS pubkey to claim.
            # The taker will provide their MARS pubkey in the negotiation.
            # Let me reconsider the key exchange...
            # Actually: for the MARS HTLC, we need:
            #   recipient_pubkey = taker's key (they claim MARS with preimage)
            #   sender_pubkey = maker's key (maker can refund after timeout)
            # For the BTC HTLC, we need:
            #   recipient_pubkey = maker's key (maker claims BTC with preimage) 
            #   sender_pubkey = taker's key (taker can refund after timeout)
            recipient_pubkey=peer_btc_pubkey,  # placeholder -- see negotiation
            sender_pubkey=mars_pubkey,
            locktime=mars_locktime,
        )
        
        mars_htlc_address = htlc_script_to_mars_p2wsh(mars_htlc_script)
        
        # Fund the MARS HTLC
        from electrum_mars.transaction import PartialTxOutput
        funding_output = PartialTxOutput.from_address_and_value(
            mars_htlc_address, mars_amount_sat
        )
        tx = self.wallet.create_transaction(
            outputs=[funding_output], rbf=False, password=password
        )
        await self.network.broadcast_transaction(tx)
        
        # Store swap data
        swap = AtomicSwapData(
            swap_id=payment_hash160.hex(),
            role="maker",
            direction="sell_mars",
            mars_amount_sat=mars_amount_sat,
            btc_amount_sat=btc_amount_sat,
            preimage=preimage,
            payment_hash160=payment_hash160,
            mars_privkey=mars_privkey,
            mars_pubkey=mars_pubkey,
            mars_peer_pubkey=peer_btc_pubkey,
            mars_htlc_script=mars_htlc_script,
            mars_htlc_address=mars_htlc_address,
            mars_locktime=mars_locktime,
            mars_funding_txid=tx.txid(),
            mars_claim_txid=None,
            btc_privkey=btc_privkey,
            btc_pubkey=btc_pubkey,
            btc_peer_pubkey=b'',  # filled when taker responds
            btc_htlc_script='',
            btc_htlc_address='',
            btc_locktime=0,
            btc_funding_txid=None,
            btc_claim_txid=None,
            state=SwapState.MARS_HTLC_FUNDED.value,
            created_at=time.time(),
            completed_at=None,
            peer_id=peer_id,
            receive_address=self.wallet.get_receiving_address(),
        )
        self.swaps[swap.swap_id] = swap
        
        # Watch MARS HTLC address for claims/refunds
        self.lnwatcher.add_callback(
            mars_htlc_address,
            lambda: self._on_mars_htlc_activity(swap),
        )
        
        return swap
    
    async def on_taker_btc_htlc_created(
        self, swap: AtomicSwapData, 
        btc_htlc_address: str, btc_htlc_script: str,
        btc_locktime: int, taker_btc_pubkey: bytes,
    ):
        """Called when taker notifies us their BTC HTLC is funded.
        
        Verify the BTC HTLC, then claim BTC by revealing preimage.
        """
        # Verify BTC HTLC script
        verify_htlc_script(
            btc_htlc_script,
            expected_hash160=swap.payment_hash160,
            expected_recipient_pubkey=swap.btc_pubkey,
            expected_sender_pubkey=taker_btc_pubkey,
            expected_locktime=btc_locktime,
        )
        
        # Verify BTC HTLC address matches script
        expected_address = htlc_script_to_btc_p2wsh(btc_htlc_script)
        if btc_htlc_address != expected_address:
            raise Exception("BTC HTLC address does not match script")
        
        # Verify BTC locktime is long enough (must be > MARS locktime)
        if btc_locktime <= swap.mars_locktime:
            raise Exception("BTC locktime must be greater than MARS locktime")
        
        # Update swap
        swap.btc_htlc_address = btc_htlc_address
        swap.btc_htlc_script = btc_htlc_script
        swap.btc_locktime = btc_locktime
        swap.btc_peer_pubkey = taker_btc_pubkey
        swap.state = SwapState.WAITING_BTC_HTLC.value
        
        # Watch BTC address for funding
        self.btc_monitor.watch_address(
            btc_htlc_address,
            on_funded=lambda utxo: self._on_btc_htlc_funded(swap, utxo),
            on_spent=lambda txid, raw: self._on_btc_htlc_spent(swap, txid, raw),
        )
    
    async def _on_btc_htlc_funded(self, swap: AtomicSwapData, utxo: BtcUtxo):
        """BTC HTLC has been funded by taker. Verify amount, then claim."""
        if utxo.value_sat < swap.btc_amount_sat:
            self.logger.warning(f"BTC HTLC underfunded: {utxo.value_sat} < {swap.btc_amount_sat}")
            return
        
        swap.btc_funding_txid = utxo.txid
        swap.state = SwapState.BTC_HTLC_VERIFIED.value
        
        # Claim BTC by revealing preimage
        await self._claim_btc(swap, utxo)
    
    async def _claim_btc(self, swap: AtomicSwapData, utxo: BtcUtxo):
        """Claim BTC from HTLC by revealing preimage.
        
        This is the critical step: once we broadcast this tx,
        the preimage is visible on the Bitcoin blockchain,
        allowing the taker to claim our MARS.
        """
        from electrum_mars.transaction import PartialTxInput, TxOutpoint
        from electrum_mars.util import bfh
        
        swap.state = SwapState.CLAIMING_BTC.value
        
        # Build claim transaction
        # Note: BTC transactions use the same serialization format
        # We construct the tx and broadcast via mempool.space API
        witness_script = bfh(swap.btc_htlc_script)
        
        # Estimate fee (simple: 200 vbytes * 10 sat/vbyte = 2000 sat)
        claim_fee_sat = 2000
        claim_amount = utxo.value_sat - claim_fee_sat
        
        # We need to construct a raw Bitcoin transaction
        # This requires BTC-specific serialization
        # Use our transaction primitives but with BTC addressing
        
        txin = PartialTxInput(
            prevout=TxOutpoint(txid=bytes.fromhex(utxo.txid), out_idx=utxo.vout),
            script_type='p2wsh',
        )
        txin.witness_script = witness_script
        txin._trusted_value_sats = utxo.value_sat
        
        claim_tx = create_claim_tx(
            txin=txin,
            witness_script=witness_script,
            preimage=swap.preimage,
            address=swap.btc_htlc_address,  # TODO: use a BTC receive address
            amount_sat=claim_amount,
        )
        
        sign_htlc_claim(claim_tx, swap.btc_privkey, swap.preimage, witness_script)
        
        # Broadcast via mempool.space
        raw_tx = claim_tx.serialize()
        txid = await self.btc_monitor.broadcast_tx(raw_tx)
        
        swap.btc_claim_txid = txid
        swap.state = SwapState.COMPLETED.value
        swap.completed_at = time.time()
        
        self.logger.info(f"Swap {swap.swap_id} completed. BTC claimed in {txid}")
    
    # ── Taker flow (buying MARS with BTC) ──
    
    async def initiate_swap_as_taker(
        self,
        swap: AtomicSwapData,
        password: str,
    ) -> AtomicSwapData:
        """Taker accepts an offer: verify MARS HTLC, create BTC HTLC.
        
        At this point swap already has the MARS HTLC details from the offer.
        """
        # Verify MARS HTLC is funded and correct
        # (done via ElectrumX queries to Marscoin chain)
        
        # Generate BTC keypair
        btc_privkey, btc_pubkey = generate_keypair()
        
        # Get current BTC block height for locktime
        btc_height = await self.btc_monitor._get_current_block_height()
        btc_locktime = btc_height + BTC_TIMELOCK_BLOCKS
        
        # Create BTC HTLC
        btc_htlc_script = create_htlc_script(
            payment_hash160=swap.payment_hash160,
            recipient_pubkey=swap.mars_pubkey,  # maker claims BTC with preimage
            sender_pubkey=btc_pubkey,            # taker can refund after timeout
            locktime=btc_locktime,
        )
        btc_htlc_address = htlc_script_to_btc_p2wsh(btc_htlc_script)
        
        # Update swap data
        swap.btc_privkey = btc_privkey
        swap.btc_pubkey = btc_pubkey
        swap.btc_htlc_script = btc_htlc_script
        swap.btc_htlc_address = btc_htlc_address
        swap.btc_locktime = btc_locktime
        swap.state = SwapState.MARS_HTLC_VERIFIED.value
        
        # Display BTC HTLC address as QR code for user to send BTC from external wallet
        # (handled by GUI layer)
        
        return swap
    
    async def on_btc_htlc_funded_by_taker(self, swap: AtomicSwapData):
        """Called after taker confirms they sent BTC to the HTLC address.
        
        Now we wait for maker to claim BTC (revealing preimage).
        """
        swap.state = SwapState.WAITING_PREIMAGE.value
        
        # Watch BTC HTLC for spending (which reveals preimage)
        self.btc_monitor.watch_address(
            swap.btc_htlc_address,
            on_funded=lambda utxo: None,  # we already know it's funded
            on_spent=lambda txid, raw: self._on_btc_claimed_extract_preimage(swap, txid, raw),
        )
    
    async def _on_btc_claimed_extract_preimage(
        self, swap: AtomicSwapData, txid: str, raw_tx: str
    ):
        """Maker claimed BTC, revealing preimage. Extract it and claim MARS."""
        preimage = BtcChainMonitor.extract_preimage_from_witness(raw_tx)
        if preimage is None:
            self.logger.error("Could not extract preimage from BTC claim tx")
            return
        
        # Verify preimage matches hash
        from electrum_mars.crypto import hash_160
        if hash_160(preimage) != swap.payment_hash160:
            self.logger.error("Extracted preimage does not match hash!")
            return
        
        swap.preimage = preimage
        swap.state = SwapState.CLAIMING_MARS.value
        
        # Claim MARS using preimage
        await self._claim_mars(swap)
    
    async def _claim_mars(self, swap: AtomicSwapData):
        """Claim MARS from HTLC using the revealed preimage."""
        from electrum_mars.util import bfh
        from electrum_mars.transaction import PartialTxInput, TxOutpoint
        
        witness_script = bfh(swap.mars_htlc_script)
        
        # Get UTXO at MARS HTLC address
        txos = self.lnwatcher.adb.get_addr_outputs(swap.mars_htlc_address)
        if not txos:
            self.logger.error("No UTXOs found at MARS HTLC address")
            return
        
        for txin in txos.values():
            claim_fee = self.wallet.config.estimate_fee(200, allow_fallback_to_static_rates=True)
            claim_amount = txin.value_sats() - claim_fee
            
            claim_tx = create_claim_tx(
                txin=txin,
                witness_script=witness_script,
                preimage=swap.preimage,
                address=swap.receive_address,
                amount_sat=claim_amount,
            )
            sign_htlc_claim(
                claim_tx, swap.mars_privkey, swap.preimage, witness_script
            )
            
            await self.network.broadcast_transaction(claim_tx)
            swap.mars_claim_txid = claim_tx.txid()
            swap.state = SwapState.COMPLETED.value
            swap.completed_at = time.time()
            
            self.logger.info(f"Swap {swap.swap_id} completed. MARS claimed in {claim_tx.txid()}")
            break
    
    # ── Refund handling ──
    
    async def check_refunds(self):
        """Periodically check if any swaps need refunding."""
        current_height = self.network.get_local_height()
        
        for swap_id, swap in self.swaps.items():
            # Maker: refund MARS if taker never created BTC HTLC
            if (swap.role == "maker" and 
                swap.state in (SwapState.MARS_HTLC_FUNDED.value, SwapState.WAITING_BTC_HTLC.value) and
                current_height >= swap.mars_locktime):
                await self._refund_mars(swap)
            
            # Taker: refund BTC if maker never claimed (preimage never revealed)
            if (swap.role == "taker" and 
                swap.state in (SwapState.BTC_HTLC_FUNDED.value, SwapState.WAITING_PREIMAGE.value)):
                btc_height = await self.btc_monitor._get_current_block_height()
                if btc_height >= swap.btc_locktime:
                    await self._refund_btc(swap)
    
    async def _refund_mars(self, swap: AtomicSwapData):
        """Refund MARS from HTLC after timelock expiry."""
        # Similar to _claim_mars but uses refund path
        swap.state = SwapState.REFUNDING_MARS.value
        # ... build and broadcast refund tx using sign_htlc_refund
    
    async def _refund_btc(self, swap: AtomicSwapData):
        """Refund BTC from HTLC after timelock expiry."""
        swap.state = SwapState.REFUNDING_BTC.value
        # ... build and broadcast BTC refund tx via mempool.space
    
    def _resume_swap(self, swap: AtomicSwapData):
        """Resume monitoring for an in-progress swap after restart."""
        # Re-register watchers based on current state
        if swap.mars_htlc_address:
            self.lnwatcher.add_callback(
                swap.mars_htlc_address,
                lambda: self._on_mars_htlc_activity(swap),
            )
        if swap.btc_htlc_address and self.btc_monitor:
            self.btc_monitor.watch_address(
                swap.btc_htlc_address,
                on_funded=lambda utxo: self._on_btc_htlc_funded(swap, utxo),
                on_spent=lambda txid, raw: self._on_btc_htlc_spent(swap, txid, raw),
            )
    
    @log_exceptions
    async def _on_mars_htlc_activity(self, swap: AtomicSwapData):
        """Callback when MARS HTLC address has activity."""
        # Check if claimed or refunded
        pass
    
    @log_exceptions
    async def _on_btc_htlc_spent(self, swap: AtomicSwapData, txid: str, raw_tx: str):
        """Callback when BTC HTLC is spent."""
        if swap.role == "taker":
            await self._on_btc_claimed_extract_preimage(swap, txid, raw_tx)
```

### 3.5 `electrum_mars/plugins/atomic_swap/orderbook.py` -- Order Book Protocol

This is the P2P offer relay layer. Given that ElectrumX needs to be extended with custom RPC methods, we define the protocol here but also provide a **fallback HTTP relay** approach for initial deployment.

```python
"""
Order book for atomic swap offers.

Two transport mechanisms:

1. ElectrumX Extension (preferred, requires server patches):
   - atomicswap.post_offer(offer_json) -> offer_id
   - atomicswap.get_offers(pair, limit) -> [offer_json, ...]
   - atomicswap.cancel_offer(offer_id, signature) -> bool
   - atomicswap.subscribe(pair) -> stream of new offers
   
   Server-side: ElectrumX stores offers in memory with TTL.
   Offers are gossipped between connected servers.
   Server never touches funds.

2. HTTP Relay Fallback (for initial deployment):
   - POST /offers  -- publish offer
   - GET  /offers  -- list offers
   - DELETE /offers/:id -- cancel offer
   
   A lightweight Flask/FastAPI server stores offers.
   Still no custody -- server is a bulletin board only.

Offer format (JSON):
{
    "version": 1,
    "pair": "BTC/MARS",
    "side": "sell_mars",           # or "buy_mars"
    "mars_amount_sat": 100000000,  # 1 MARS
    "btc_amount_sat": 1000000,     # 0.01 BTC
    "rate": 0.01,                  # BTC per MARS
    "maker_mars_pubkey": "02...",  # compressed pubkey for MARS HTLC
    "maker_btc_pubkey": "02...",   # compressed pubkey for BTC HTLC
    "payment_hash160": "a1b2...",  # HASH160 of preimage (maker knows preimage)
    "mars_locktime": 500000,       # absolute block height
    "maker_id": "peer_abc123",     # peer identifier
    "timestamp": 1711900000,
    "signature": "3045...",        # signed by maker_mars_pubkey to prove ownership
    "ttl": 3600,                   # offer valid for 1 hour
}
"""

import asyncio
import json
import time
from typing import Optional, List, Dict, Callable, TYPE_CHECKING

from electrum_mars.logging import Logger
from electrum_mars.ecc import ECPrivkey, ECPubkey
from electrum_mars.crypto import sha256

if TYPE_CHECKING:
    from electrum_mars.network import Network


class Offer:
    """Represents a swap offer on the order book."""
    
    def __init__(
        self,
        pair: str,
        side: str,
        mars_amount_sat: int,
        btc_amount_sat: int,
        maker_mars_pubkey: str,
        maker_btc_pubkey: str,
        payment_hash160: str,
        mars_locktime: int,
        maker_id: str,
        timestamp: float = None,
        signature: str = "",
        ttl: int = 3600,
        offer_id: str = "",
    ):
        self.pair = pair
        self.side = side
        self.mars_amount_sat = mars_amount_sat
        self.btc_amount_sat = btc_amount_sat
        self.maker_mars_pubkey = maker_mars_pubkey
        self.maker_btc_pubkey = maker_btc_pubkey
        self.payment_hash160 = payment_hash160
        self.mars_locktime = mars_locktime
        self.maker_id = maker_id
        self.timestamp = timestamp or time.time()
        self.signature = signature
        self.ttl = ttl
        self.offer_id = offer_id or sha256(
            json.dumps(self.to_dict_unsigned(), sort_keys=True).encode()
        ).hex()[:16]
    
    @property
    def rate(self) -> float:
        """BTC per MARS."""
        if self.mars_amount_sat == 0:
            return 0.0
        return self.btc_amount_sat / self.mars_amount_sat
    
    @property
    def is_expired(self) -> bool:
        return time.time() > self.timestamp + self.ttl
    
    def to_dict_unsigned(self) -> dict:
        return {
            "version": 1,
            "pair": self.pair,
            "side": self.side,
            "mars_amount_sat": self.mars_amount_sat,
            "btc_amount_sat": self.btc_amount_sat,
            "maker_mars_pubkey": self.maker_mars_pubkey,
            "maker_btc_pubkey": self.maker_btc_pubkey,
            "payment_hash160": self.payment_hash160,
            "mars_locktime": self.mars_locktime,
            "maker_id": self.maker_id,
            "timestamp": self.timestamp,
            "ttl": self.ttl,
        }
    
    def to_dict(self) -> dict:
        d = self.to_dict_unsigned()
        d["signature"] = self.signature
        d["offer_id"] = self.offer_id
        return d
    
    @classmethod
    def from_dict(cls, d: dict) -> 'Offer':
        return cls(
            pair=d["pair"],
            side=d["side"],
            mars_amount_sat=d["mars_amount_sat"],
            btc_amount_sat=d["btc_amount_sat"],
            maker_mars_pubkey=d["maker_mars_pubkey"],
            maker_btc_pubkey=d["maker_btc_pubkey"],
            payment_hash160=d["payment_hash160"],
            mars_locktime=d["mars_locktime"],
            maker_id=d["maker_id"],
            timestamp=d.get("timestamp", time.time()),
            signature=d.get("signature", ""),
            ttl=d.get("ttl", 3600),
            offer_id=d.get("offer_id", ""),
        )
    
    def sign(self, privkey: bytes):
        """Sign the offer with maker's private key."""
        msg = json.dumps(self.to_dict_unsigned(), sort_keys=True).encode()
        msg_hash = sha256(msg)
        eckey = ECPrivkey(privkey)
        self.signature = eckey.sign_message(msg_hash, is_compressed=True).hex()
    
    def verify_signature(self) -> bool:
        """Verify that the offer was signed by the maker."""
        msg = json.dumps(self.to_dict_unsigned(), sort_keys=True).encode()
        msg_hash = sha256(msg)
        pubkey = ECPubkey(bytes.fromhex(self.maker_mars_pubkey))
        try:
            pubkey.verify_message_hash(bytes.fromhex(self.signature), msg_hash)
            return True
        except Exception:
            return False


class OrderBook(Logger):
    """Manages the swap order book.
    
    Initially uses HTTP relay. Can be upgraded to ElectrumX extension.
    """
    
    # HTTP relay URL (fallback)
    RELAY_URL_MAINNET = "https://swap-relay.marscoin.org/api/v1"
    RELAY_URL_TESTNET = "https://swap-relay-testnet.marscoin.org/api/v1"
    
    def __init__(self, *, network: 'Network'):
        Logger.__init__(self)
        self.network = network
        self._offers: Dict[str, Offer] = {}  # local cache
        self._on_new_offer_callbacks: List[Callable] = []
        self._poll_task: Optional[asyncio.Task] = None
        
        testnet = network.config.get('testnet', False)
        self.relay_url = self.RELAY_URL_TESTNET if testnet else self.RELAY_URL_MAINNET
    
    def start(self):
        """Start polling for offers."""
        self._poll_task = asyncio.ensure_future(self._poll_loop())
    
    def stop(self):
        if self._poll_task:
            self._poll_task.cancel()
    
    def on_new_offer(self, callback: Callable):
        """Register callback for new offers."""
        self._on_new_offer_callbacks.append(callback)
    
    async def post_offer(self, offer: Offer) -> str:
        """Post an offer to the order book. Returns offer_id."""
        # Try ElectrumX first
        try:
            return await self._post_offer_electrumx(offer)
        except Exception:
            pass
        # Fallback to HTTP relay
        return await self._post_offer_http(offer)
    
    async def get_offers(self, pair: str = "BTC/MARS", limit: int = 50) -> List[Offer]:
        """Get current offers from the order book."""
        try:
            return await self._get_offers_electrumx(pair, limit)
        except Exception:
            pass
        return await self._get_offers_http(pair, limit)
    
    async def cancel_offer(self, offer_id: str, privkey: bytes) -> bool:
        """Cancel an offer. Must be signed by maker."""
        # Implementation sends signed cancellation
        pass
    
    # ── ElectrumX transport ──
    
    async def _post_offer_electrumx(self, offer: Offer) -> str:
        """Post offer via ElectrumX atomicswap.post_offer RPC."""
        session = self.network.interface.session
        result = await session.send_request(
            'atomicswap.post_offer', 
            [offer.to_dict()]
        )
        return result.get('offer_id', offer.offer_id)
    
    async def _get_offers_electrumx(self, pair: str, limit: int) -> List[Offer]:
        """Get offers via ElectrumX atomicswap.get_offers RPC."""
        session = self.network.interface.session
        result = await session.send_request(
            'atomicswap.get_offers',
            [pair, limit]
        )
        return [Offer.from_dict(d) for d in result]
    
    # ── HTTP relay transport ──
    
    async def _post_offer_http(self, offer: Offer) -> str:
        """Post offer via HTTP relay."""
        from electrum_mars.network import Network
        response = await Network.async_send_http_on_proxy(
            'post',
            self.relay_url + '/offers',
            json=offer.to_dict(),
            timeout=30,
        )
        data = json.loads(response)
        return data.get('offer_id', offer.offer_id)
    
    async def _get_offers_http(self, pair: str, limit: int) -> List[Offer]:
        """Get offers via HTTP relay."""
        from electrum_mars.network import Network
        response = await Network.async_send_http_on_proxy(
            'get',
            self.relay_url + '/offers',
            params={'pair': pair, 'limit': limit},
            timeout=30,
        )
        data = json.loads(response)
        offers = [Offer.from_dict(d) for d in data.get('offers', [])]
        # Filter expired
        return [o for o in offers if not o.is_expired]
    
    async def _poll_loop(self):
        """Poll for new offers periodically."""
        while True:
            try:
                offers = await self.get_offers()
                for offer in offers:
                    if offer.offer_id not in self._offers:
                        self._offers[offer.offer_id] = offer
                        for cb in self._on_new_offer_callbacks:
                            cb(offer)
                # Prune expired
                self._offers = {
                    k: v for k, v in self._offers.items() 
                    if not v.is_expired
                }
            except Exception as e:
                self.logger.warning(f"Error polling offers: {e}")
            await asyncio.sleep(30)  # poll every 30 seconds
```

### 3.6 `electrum_mars/plugins/atomic_swap/reputation.py` -- Reputation System

```python
"""
Simple peer reputation tracking for atomic swaps.

Stored per-wallet. Tracks:
- Number of successful swaps per peer
- Number of failed/abandoned swaps per peer
- Total volume traded
- Last interaction timestamp

Reputation is local-only (not shared between wallets).
This is a simple heuristic, not a trust system.
"""

import time
from typing import Dict, Optional, TYPE_CHECKING

import attr

from electrum_mars.json_db import StoredObject
from electrum_mars.lnutil import hex_to_bytes
from electrum_mars.logging import Logger

if TYPE_CHECKING:
    from electrum_mars.wallet import Abstract_Wallet


@attr.s
class PeerReputation(StoredObject):
    peer_id = attr.ib(type=str)
    successful_swaps = attr.ib(type=int, default=0)
    failed_swaps = attr.ib(type=int, default=0)
    abandoned_swaps = attr.ib(type=int, default=0)
    total_mars_volume_sat = attr.ib(type=int, default=0)
    total_btc_volume_sat = attr.ib(type=int, default=0)
    first_seen = attr.ib(type=float, default=0.0)
    last_seen = attr.ib(type=float, default=0.0)
    
    @property
    def total_swaps(self) -> int:
        return self.successful_swaps + self.failed_swaps + self.abandoned_swaps
    
    @property
    def success_rate(self) -> float:
        if self.total_swaps == 0:
            return 0.0
        return self.successful_swaps / self.total_swaps
    
    @property
    def trust_score(self) -> int:
        """0-100 trust score. Higher is more trustworthy."""
        if self.total_swaps == 0:
            return 50  # neutral for unknown peers
        base = self.success_rate * 100
        # Bonus for volume
        volume_bonus = min(10, self.successful_swaps)
        return min(100, int(base + volume_bonus))


class ReputationManager(Logger):
    """Manages peer reputation data."""
    
    def __init__(self, wallet: 'Abstract_Wallet'):
        Logger.__init__(self)
        self.wallet = wallet
        self.peers: Dict[str, PeerReputation] = wallet.db.get_dict('atomic_swap_reputation')
    
    def get_peer(self, peer_id: str) -> PeerReputation:
        if peer_id not in self.peers:
            self.peers[peer_id] = PeerReputation(
                peer_id=peer_id,
                first_seen=time.time(),
                last_seen=time.time(),
            )
        return self.peers[peer_id]
    
    def record_success(self, peer_id: str, mars_sat: int, btc_sat: int):
        peer = self.get_peer(peer_id)
        peer.successful_swaps += 1
        peer.total_mars_volume_sat += mars_sat
        peer.total_btc_volume_sat += btc_sat
        peer.last_seen = time.time()
    
    def record_failure(self, peer_id: str):
        peer = self.get_peer(peer_id)
        peer.failed_swaps += 1
        peer.last_seen = time.time()
    
    def record_abandoned(self, peer_id: str):
        peer = self.get_peer(peer_id)
        peer.abandoned_swaps += 1
        peer.last_seen = time.time()
    
    def get_all_peers(self) -> Dict[str, PeerReputation]:
        return dict(self.peers)
    
    def is_trusted(self, peer_id: str, threshold: int = 60) -> bool:
        peer = self.get_peer(peer_id)
        return peer.trust_score >= threshold
```

### 3.7 `electrum_mars/plugins/atomic_swap/qt.py` -- Qt GUI

```python
"""
Qt GUI for the Atomic Swap plugin.

Adds a "Swap" tab to the main wallet window with:
- Order book display (table of current offers)
- "Sell MARS" button (become a maker)
- "Buy MARS" button (accept best offer)
- Active swaps status panel
- Swap history
- Reputation viewer

Plugin follows the same pattern as cosigner_pool/qt.py and labels/qt.py:
- class Plugin(BasePlugin) with @hook decorators
- init_qt, load_wallet, on_close_window hooks
- Qt signals for cross-thread communication
"""

from functools import partial
from typing import TYPE_CHECKING, Optional, List

from PyQt5.QtCore import QObject, pyqtSignal, Qt, QTimer
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTabWidget,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QPushButton, QLabel, QGroupBox, QGridLayout,
    QDialog, QDialogButtonBox, QLineEdit, QComboBox,
    QProgressBar, QTextEdit, QSplitter, QMessageBox,
)
from PyQt5.QtGui import QFont, QColor

from electrum_mars.plugin import BasePlugin, hook
from electrum_mars.i18n import _
from electrum_mars.gui.qt.util import (
    WindowModalDialog, Buttons, OkButton, CancelButton,
    EnterButton, WWLabel, read_QIcon,
)
from electrum_mars.gui.qt.amountedit import BTCAmountEdit

from .swap_engine import SwapEngine, SwapState
from .orderbook import OrderBook, Offer
from .reputation import ReputationManager

if TYPE_CHECKING:
    from electrum_mars.gui.qt import ElectrumGui
    from electrum_mars.gui.qt.main_window import ElectrumWindow
    from electrum_mars.wallet import Abstract_Wallet


class QSwapSignalObject(QObject):
    """Qt signal bridge for cross-thread communication."""
    offers_updated = pyqtSignal()
    swap_state_changed = pyqtSignal(str)  # swap_id
    error_signal = pyqtSignal(str)


class SwapTab(QWidget):
    """Main tab widget for atomic swaps."""
    
    tab_name = 'atomic_swap'
    tab_description = _('Swap')
    tab_pos = 5
    tab_icon = None  # set in constructor
    
    def __init__(self, window: 'ElectrumWindow', plugin: 'Plugin'):
        QWidget.__init__(self)
        self.window = window
        self.plugin = plugin
        self.tab_icon = read_QIcon("tab_swap.png")  # custom icon needed
        
        layout = QVBoxLayout(self)
        
        # ── Top: Summary ──
        summary_group = QGroupBox(_("BTC / MARS Exchange"))
        summary_layout = QHBoxLayout()
        self.rate_label = QLabel(_("Best rate: --"))
        self.rate_label.setFont(QFont("", 14))
        summary_layout.addWidget(self.rate_label)
        summary_layout.addStretch()
        
        self.buy_button = QPushButton(_("Buy MARS"))
        self.buy_button.clicked.connect(self.on_buy_mars)
        self.sell_button = QPushButton(_("Sell MARS"))
        self.sell_button.clicked.connect(self.on_sell_mars)
        summary_layout.addWidget(self.buy_button)
        summary_layout.addWidget(self.sell_button)
        summary_group.setLayout(summary_layout)
        layout.addWidget(summary_group)
        
        # ── Middle: Splitter with Order Book and Active Swaps ──
        splitter = QSplitter(Qt.Vertical)
        
        # Order book table
        orderbook_group = QGroupBox(_("Order Book"))
        ob_layout = QVBoxLayout()
        self.offers_table = QTableWidget()
        self.offers_table.setColumnCount(6)
        self.offers_table.setHorizontalHeaderLabels([
            _("Side"), _("MARS Amount"), _("BTC Amount"), 
            _("Rate"), _("Peer"), _("Trust"),
        ])
        self.offers_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.offers_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.offers_table.setEditTriggers(QTableWidget.NoEditTriggers)
        ob_layout.addWidget(self.offers_table)
        
        refresh_btn = QPushButton(_("Refresh"))
        refresh_btn.clicked.connect(self.refresh_offers)
        ob_layout.addWidget(refresh_btn)
        orderbook_group.setLayout(ob_layout)
        splitter.addWidget(orderbook_group)
        
        # Active swaps panel
        swaps_group = QGroupBox(_("Active Swaps"))
        swaps_layout = QVBoxLayout()
        self.swaps_table = QTableWidget()
        self.swaps_table.setColumnCount(5)
        self.swaps_table.setHorizontalHeaderLabels([
            _("Direction"), _("MARS"), _("BTC"), _("State"), _("Time"),
        ])
        self.swaps_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.swaps_table.setEditTriggers(QTableWidget.NoEditTriggers)
        swaps_layout.addWidget(self.swaps_table)
        swaps_group.setLayout(swaps_layout)
        splitter.addWidget(swaps_group)
        
        layout.addWidget(splitter)
        
        # Periodic refresh
        self.refresh_timer = QTimer()
        self.refresh_timer.timeout.connect(self.refresh_offers)
        self.refresh_timer.start(30000)  # 30 seconds
    
    def on_buy_mars(self):
        """Open Buy MARS dialog."""
        # Show BuyMarsDialog
        pass
    
    def on_sell_mars(self):
        """Open Sell MARS dialog."""
        # Show SellMarsDialog
        pass
    
    def refresh_offers(self):
        """Refresh order book from network."""
        coro = self.plugin.orderbook.get_offers()
        self.window.run_coroutine_from_thread(
            coro, _("Refreshing offers"),
            on_result=self._update_offers_table,
        )
    
    def _update_offers_table(self, offers: List[Offer]):
        """Populate the offers table."""
        self.offers_table.setRowCount(len(offers))
        for i, offer in enumerate(offers):
            self.offers_table.setItem(i, 0, QTableWidgetItem(offer.side))
            self.offers_table.setItem(i, 1, QTableWidgetItem(
                self.window.format_amount(offer.mars_amount_sat)
            ))
            self.offers_table.setItem(i, 2, QTableWidgetItem(
                f"{offer.btc_amount_sat / 1e8:.8f}"
            ))
            self.offers_table.setItem(i, 3, QTableWidgetItem(
                f"{offer.rate:.8f}"
            ))
            self.offers_table.setItem(i, 4, QTableWidgetItem(
                offer.maker_id[:12] + "..."
            ))
            # Trust score
            peer = self.plugin.reputation.get_peer(offer.maker_id)
            trust_item = QTableWidgetItem(str(peer.trust_score))
            if peer.trust_score >= 80:
                trust_item.setForeground(QColor("green"))
            elif peer.trust_score >= 50:
                trust_item.setForeground(QColor("orange"))
            else:
                trust_item.setForeground(QColor("red"))
            self.offers_table.setItem(i, 5, trust_item)
        
        # Update best rate
        if offers:
            best = min(offers, key=lambda o: o.rate)
            self.rate_label.setText(
                _("Best rate: {rate} BTC/MARS").format(rate=f"{best.rate:.8f}")
            )


class BuyMarsDialog(WindowModalDialog):
    """Dialog for buying MARS (taker flow).
    
    1. Shows best available offer
    2. User specifies amount
    3. Generates BTC HTLC address + QR code
    4. User sends BTC from external wallet
    5. Monitors for completion
    """
    
    def __init__(self, window: 'ElectrumWindow', plugin: 'Plugin', offer: Offer):
        WindowModalDialog.__init__(self, window, _("Buy MARS"))
        self.window = window
        self.plugin = plugin
        self.offer = offer
        self._setup_ui()
    
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        
        # Offer details
        layout.addWidget(WWLabel(_(
            "You are buying {mars} MARS for {btc} BTC\n"
            "Rate: {rate} BTC/MARS\n"
            "Peer trust score: {trust}"
        ).format(
            mars=self.offer.mars_amount_sat / 1e8,
            btc=self.offer.btc_amount_sat / 1e8,
            rate=f"{self.offer.rate:.8f}",
            trust=self.plugin.reputation.get_peer(self.offer.maker_id).trust_score,
        )))
        
        # BTC payment section (shown after swap initiated)
        self.btc_address_label = QLabel("")
        self.btc_qr_label = QLabel("")  # QR code
        layout.addWidget(self.btc_address_label)
        layout.addWidget(self.btc_qr_label)
        
        # Status
        self.status_label = QLabel(_("Ready to swap"))
        layout.addWidget(self.status_label)
        
        # Progress bar
        self.progress = QProgressBar()
        self.progress.setRange(0, 5)
        self.progress.setValue(0)
        layout.addWidget(self.progress)
        
        # Buttons
        layout.addLayout(Buttons(
            CancelButton(self),
            OkButton(self, _("Start Swap")),
        ))


class SellMarsDialog(WindowModalDialog):
    """Dialog for selling MARS (maker flow).
    
    1. User specifies MARS amount and desired BTC price
    2. Creates offer and MARS HTLC
    3. Publishes offer to order book
    4. Waits for taker
    """
    
    def __init__(self, window: 'ElectrumWindow', plugin: 'Plugin'):
        WindowModalDialog.__init__(self, window, _("Sell MARS"))
        self.window = window
        self.plugin = plugin
        self._setup_ui()
    
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        
        layout.addWidget(WWLabel(_("Create an offer to sell MARS for BTC")))
        
        grid = QGridLayout()
        
        # MARS amount
        grid.addWidget(QLabel(_("MARS to sell:")), 0, 0)
        self.mars_amount_e = BTCAmountEdit(self.window.get_decimal_point)
        grid.addWidget(self.mars_amount_e, 0, 1)
        
        # BTC amount
        grid.addWidget(QLabel(_("BTC to receive:")), 1, 0)
        self.btc_amount_e = QLineEdit()
        self.btc_amount_e.setPlaceholderText("0.00000000")
        grid.addWidget(self.btc_amount_e, 1, 1)
        
        # Rate display
        grid.addWidget(QLabel(_("Rate:")), 2, 0)
        self.rate_label = QLabel("--")
        grid.addWidget(self.rate_label, 2, 1)
        
        layout.addLayout(grid)
        
        # Warning
        layout.addWidget(WWLabel(_(
            "Your MARS will be locked in an HTLC for up to 4 hours. "
            "If no buyer appears, you will be automatically refunded."
        )))
        
        layout.addLayout(Buttons(
            CancelButton(self),
            OkButton(self, _("Create Offer")),
        ))


class Plugin(BasePlugin):
    """Atomic Swap plugin entry point."""
    
    def __init__(self, parent, config, name):
        BasePlugin.__init__(self, parent, config, name)
        self.obj = QSwapSignalObject()
        self._init_qt_received = False
        self.swap_engines: dict = {}    # wallet -> SwapEngine
        self.orderbook: Optional[OrderBook] = None
        self.reputation: Optional[ReputationManager] = None
        self.swap_tabs: dict = {}       # window -> SwapTab
    
    @hook
    def init_qt(self, gui: 'ElectrumGui'):
        if self._init_qt_received:
            return
        self._init_qt_received = True
        for window in gui.windows:
            self.load_wallet(window.wallet, window)
    
    @hook
    def load_wallet(self, wallet: 'Abstract_Wallet', window: 'ElectrumWindow'):
        """Set up swap engine and tab for this wallet."""
        # Initialize swap engine
        engine = SwapEngine(wallet=wallet, config=self.config)
        self.swap_engines[wallet] = engine
        
        # Initialize reputation
        self.reputation = ReputationManager(wallet)
        
        # Initialize order book
        if window.network:
            self.orderbook = OrderBook(network=window.network)
            self.orderbook.start()
            engine.start_network(
                network=window.network,
                lnwatcher=window.wallet.lnworker.lnwatcher if hasattr(window.wallet, 'lnworker') and window.wallet.lnworker else None,
            )
        
        # Add tab
        tab = SwapTab(window, self)
        self.swap_tabs[window] = tab
        window.tabs.addTab(tab, tab.tab_icon or read_QIcon("tab_send.png"), tab.tab_description)
    
    @hook
    def on_close_window(self, window: 'ElectrumWindow'):
        """Clean up when wallet window is closed."""
        tab = self.swap_tabs.pop(window, None)
        if tab:
            i = window.tabs.indexOf(tab)
            if i >= 0:
                window.tabs.removeTab(i)
        
        wallet = window.wallet
        engine = self.swap_engines.pop(wallet, None)
        if engine:
            engine.stop()
        
        if self.orderbook:
            self.orderbook.stop()
    
    def on_close(self):
        """Plugin disabled."""
        for engine in self.swap_engines.values():
            engine.stop()
        if self.orderbook:
            self.orderbook.stop()
```

---

## 4. ElectrumX Protocol Extension Specification

The following RPC methods should be added to ElectrumX (Marscoin's fork). These are pure bulletin board operations -- the server never touches funds.

```
Method: atomicswap.post_offer
Params: [offer_dict]
Returns: {"offer_id": "abc123", "status": "ok"}
Description: Store an offer in server memory with TTL. Gossip to connected peers.
Validation: Verify signature, check TTL <= 3600, check timestamp is recent.

Method: atomicswap.get_offers  
Params: [pair, limit]  (e.g., ["BTC/MARS", 50])
Returns: [offer_dict, offer_dict, ...]
Description: Return non-expired offers sorted by rate.

Method: atomicswap.cancel_offer
Params: [offer_id, cancel_signature]
Returns: {"status": "ok"}
Description: Remove an offer. Cancel message must be signed by maker's key.

Method: atomicswap.subscribe
Params: [pair]
Returns: Stream of notifications when new offers arrive.
Description: Subscribe to real-time offer updates. Uses ElectrumX notification mechanism.

Method: atomicswap.swap_message
Params: [swap_id, from_pubkey, to_pubkey, encrypted_message]
Returns: {"status": "ok"}
Description: Relay an encrypted message between swap participants.
Used for negotiation (sharing pubkeys, HTLC details, status updates).
Messages are encrypted with recipient's pubkey (same as cosigner_pool).
Server stores messages for up to 1 hour, delivers on next poll.

Method: atomicswap.get_messages
Params: [pubkey_hash]
Returns: [message_dict, ...]
Description: Retrieve pending messages for a pubkey.
```

Until the ElectrumX extension is deployed, the HTTP relay fallback (described in `orderbook.py`) handles the same functionality via REST endpoints.

---

## 5. Swap Negotiation Protocol (Message Sequence)

```
Alice (Maker, has MARS)                    Bob (Taker, has BTC)
        │                                         │
        │  1. POST offer to orderbook             │
        │────────────────────────────────────────>│
        │                                         │
        │  2. Bob sees offer, clicks "Accept"     │
        │<────────────────────────────────────────│
        │     atomicswap.swap_message:            │
        │     {type: "accept",                    │
        │      taker_mars_pubkey: "02...",         │
        │      taker_btc_pubkey: "02..."}          │
        │                                         │
        │  3. Alice creates MARS HTLC, funds it   │
        │     atomicswap.swap_message:            │
        │     {type: "mars_htlc_created",         │
        │      mars_htlc_address: "mrs1...",       │
        │      mars_htlc_script: "...",            │
        │      mars_funding_txid: "...",           │
        │      payment_hash160: "a1b2..."}         │
        │────────────────────────────────────────>│
        │                                         │
        │  4. Bob verifies MARS HTLC on chain     │
        │     (via ElectrumX blockchain queries)   │
        │                                         │
        │  5. Bob creates BTC HTLC                │
        │     atomicswap.swap_message:            │
        │     {type: "btc_htlc_created",          │
        │      btc_htlc_address: "bc1...",         │
        │      btc_htlc_script: "...",             │
        │      btc_locktime: 800000}               │
        │<────────────────────────────────────────│
        │                                         │
        │  6. Bob sends BTC to HTLC address       │
        │     (from external BTC wallet via QR)    │
        │                                         │
        │  7. Alice monitors BTC chain             │
        │     (mempool.space polling)              │
        │     Sees BTC HTLC funded                 │
        │                                         │
        │  8. Alice claims BTC (reveals preimage)  │
        │     Broadcasts BTC claim tx              │
        │                                         │
        │  9. Bob monitors BTC chain               │
        │     Sees Alice's claim tx                │
        │     Extracts preimage from witness        │
        │                                         │
        │  10. Bob claims MARS with preimage       │
        │      Broadcasts MARS claim tx            │
        │                                         │
        │  DONE. Both have their coins.            │
```

---

## 6. BTC Transaction Construction

A critical challenge: Electrum-Mars's `PartialTransaction` class serializes transactions using Marscoin's chain parameters. For Bitcoin transactions, we need:

**Option A (Recommended): Minimal BTC TX builder**

Build a lightweight `BtcTransaction` class in `btc_monitor.py` that constructs only the specific P2WSH-claim transaction we need. This avoids entangling Marscoin and Bitcoin serialization. The claim tx is structurally simple:
- 1 input (the HTLC UTXO)
- 1 output (the claim address)
- Version 2, locktime 0 for claim / locktime=N for refund
- Witness: `<sig> <preimage> <1> <witness_script>`

The raw BTC transaction format is identical to Marscoin's format (both are Bitcoin forks), so we can reuse `PartialTransaction` serialization but must ensure:
1. The output address uses BTC's bech32 HRP ("bc" not "mrs")
2. The sighash uses the correct value

**Option B: Use python-bitcoinlib or similar**

Add `python-bitcoinlib` as a dependency specifically for BTC transaction construction. This is cleaner but adds a dependency.

The recommended approach is Option A since Marscoin's transaction format is identical to Bitcoin's at the byte level.

---

## 7. Security Considerations

1. **Timelock ordering**: BTC timelock (36 blocks, ~6h) MUST be longer than MARS timelock (96 blocks, ~4h at 2.5 min/block). This ensures the maker (who knows the preimage) must claim BTC before MARS refund becomes possible.

2. **Preimage length**: Enforce 32-byte preimages (like `WITNESS_TEMPLATE_REVERSE_SWAP` does with the OP_SIZE check). Consider adding this to our HTLC script.

3. **Amount verification**: Always verify on-chain amounts match agreed amounts before proceeding.

4. **Fee estimation**: Both BTC and MARS claim transactions need fees. The displayed swap amounts should account for claim fees.

5. **Race conditions**: If maker claims BTC and taker sees it but cannot get MARS claim tx mined before MARS locktime, taker loses. The 2-hour margin (6h BTC vs 4h MARS) provides safety.

6. **Network partitions**: If either party loses network connectivity, timelock refunds protect them.

---

## 8. Testing Strategy

**Unit Tests** (in `electrum_mars/tests/`):
- `test_atomic_swap_htlc.py`: Script construction, verification, signing
- `test_swap_state_machine.py`: State transitions, edge cases
- `test_orderbook.py`: Offer serialization, signing, validation
- `test_reputation.py`: Score calculation, persistence

**Integration Tests** (regtest):
1. Set up Marscoin regtest + Bitcoin regtest
2. Run two Electrum-Mars instances (Alice and Bob)
3. Alice creates offer, Bob accepts
4. Verify full swap completes
5. Test timeout/refund paths
6. Test interruption recovery (kill one side mid-swap)

**Testnet**:
1. Deploy to Marscoin testnet + Bitcoin testnet
2. Run swaps between real wallets
3. Test mempool.space testnet API integration

---

## 9. Implementation Sequence

**Phase 1 -- Core HTLC (1-2 weeks)**
1. `atomic_swap_htlc.py` -- Script creation, signing, verification
2. Unit tests for HTLC primitives
3. Manual testing on regtest

**Phase 2 -- Bitcoin Monitor (1 week)**
4. `btc_monitor.py` -- mempool.space polling, preimage extraction
5. BTC transaction construction for claims
6. Integration tests

**Phase 3 -- State Machine (1-2 weeks)**
7. `swap_engine.py` -- Full swap lifecycle
8. Persistence via wallet DB
9. Refund/timeout handling
10. Recovery after restart

**Phase 4 -- Order Book (1 week)**
11. `orderbook.py` -- HTTP relay fallback
12. Offer signing and validation
13. Relay server deployment

**Phase 5 -- Plugin + GUI (1-2 weeks)**
14. Plugin structure (`__init__.py`, hooks)
15. `qt.py` -- Tab, dialogs, QR codes
16. `reputation.py` -- Peer tracking

**Phase 6 -- ElectrumX Extension (1 week)**
17. ElectrumX server-side patches for `atomicswap.*` methods
18. Switch from HTTP relay to ElectrumX transport
19. Gossip between servers

**Phase 7 -- Polish + Testing (1-2 weeks)**
20. End-to-end testnet testing
21. Error handling, edge cases
22. UI polish, localization
23. Documentation

---

## 10. Key Design Decisions and Trade-offs

**Why a plugin, not a core feature?**
The existing codebase already has `submarine_swaps.py` in core, but that is tightly coupled to Lightning. Atomic swaps are an independent feature that benefits from the plugin architecture's clean isolation and the ability to enable/disable independently.

**Why HTTP relay fallback?**
Deploying ElectrumX server changes requires coordination across all Marscoin ElectrumX operators. The HTTP relay lets us ship the feature immediately with a single-server bulletin board, then migrate to fully decentralized ElectrumX gossip later.

**Why mempool.space instead of running a Bitcoin ElectrumX?**
Adding a second ElectrumX connection (to Bitcoin servers) would require significant refactoring of the network layer, which assumes a single chain. The mempool.space REST API is well-documented, reliable, has no rate limits for moderate use, and supports testnet. The 15-second polling interval is acceptable for a swap that takes minutes to complete.

**Why not use the existing SwapManager?**
`SwapManager` is designed for Lightning submarine swaps via the Boltz backend. The class structure, API calls, and flow are fundamentally different from P2P atomic swaps. Sharing `create_claim_tx` and the witness construction patterns is the right level of code reuse.

### Critical Files for Implementation
- `/Users/novalis78/Projects/electrum-mars-new/electrum_mars/submarine_swaps.py`
- `/Users/novalis78/Projects/electrum-mars-new/electrum_mars/bitcoin.py`
- `/Users/novalis78/Projects/electrum-mars-new/electrum_mars/plugin.py`
- `/Users/novalis78/Projects/electrum-mars-new/electrum_mars/plugins/cosigner_pool/qt.py`
- `/Users/novalis78/Projects/electrum-mars-new/electrum_mars/gui/qt/main_window.py`