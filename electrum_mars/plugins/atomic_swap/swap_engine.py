"""
Atomic swap state machine and persistence.

Manages the lifecycle of a swap from offer acceptance through HTLC creation,
monitoring, claiming, and completion (or refund on timeout).

State machine:
  CREATED -> MARS_LOCKED -> BTC_LOCKED -> BTC_CLAIMED -> COMPLETED
                |              |              |
                v              v              v
          MARS_REFUNDED   BTC_REFUNDED   MARS_CLAIMED (intermediate)
                |              |
                v              v
              FAILED         FAILED
"""

import os
import time
import json
import sqlite3
import asyncio
from enum import Enum
from typing import Optional, Dict, List, TYPE_CHECKING
from dataclasses import dataclass, field, asdict

from electrum_mars.atomic_swap_htlc import (
    generate_preimage, generate_keypair, create_htlc_script,
    htlc_to_p2wsh_address, create_funding_tx, create_claim_tx,
    create_refund_tx, verify_htlc_script, extract_preimage_from_witness,
    Chain, BTC_TIMELOCK_BLOCKS, MARS_TIMELOCK_BLOCKS,
)
from electrum_mars.btc_monitor import BtcMonitor
from electrum_mars.crypto import hash_160
from electrum_mars.logging import get_logger
from electrum_mars.util import bfh, bh2u

if TYPE_CHECKING:
    from electrum_mars.wallet import Abstract_Wallet
    from electrum_mars.network import Network

_logger = get_logger(__name__)


class SwapState(Enum):
    """States in the atomic swap lifecycle."""
    CREATED = "created"              # Offer accepted, parameters agreed
    MARS_LOCKED = "mars_locked"      # MARS HTLC funded and broadcast
    BTC_LOCKED = "btc_locked"        # BTC HTLC funded and confirmed
    BTC_CLAIMED = "btc_claimed"      # Maker claimed BTC (preimage revealed)
    COMPLETED = "completed"          # Taker claimed MARS, swap done
    MARS_REFUNDED = "mars_refunded"  # MARS HTLC refunded after timeout
    BTC_REFUNDED = "btc_refunded"    # BTC HTLC refunded after timeout
    FAILED = "failed"                # Swap failed for any reason
    EXPIRED = "expired"              # Offer expired before acceptance


class SwapRole(Enum):
    """Role in the swap."""
    MAKER = "maker"  # Has MARS, wants BTC. Creates the offer. Generates preimage.
    TAKER = "taker"  # Has BTC, wants MARS. Accepts the offer.


@dataclass
class SwapData:
    """All data for a single atomic swap."""
    swap_id: str
    role: str                         # "maker" or "taker"
    state: str                        # SwapState value
    mars_amount_sat: int              # MARS amount in satoshis
    btc_amount_sat: int               # BTC amount in satoshis
    rate: float                       # BTC per MARS rate

    # Preimage (maker generates, taker learns from BTC chain)
    preimage: Optional[str] = None    # hex, 32 bytes
    payment_hash160: Optional[str] = None  # hex, 20 bytes

    # Keys
    my_privkey: Optional[str] = None  # hex, ephemeral key for this swap
    my_pubkey: Optional[str] = None   # hex
    peer_pubkey: Optional[str] = None # hex

    # MARS HTLC
    mars_htlc_script: Optional[str] = None  # hex
    mars_htlc_address: Optional[str] = None
    mars_locktime: int = 0
    mars_funding_txid: Optional[str] = None
    mars_funding_vout: int = 0
    mars_claim_txid: Optional[str] = None

    # BTC HTLC
    btc_htlc_script: Optional[str] = None  # hex
    btc_htlc_address: Optional[str] = None
    btc_locktime: int = 0
    btc_funding_txid: Optional[str] = None
    btc_funding_vout: int = 0
    btc_claim_txid: Optional[str] = None

    # Maker's BTC receive address — where the claimed BTC is sent
    # (maker provides this when creating the offer)
    btc_receive_address: Optional[str] = None

    # Metadata
    peer_id: Optional[str] = None     # peer identifier (pubkey or address)
    created_at: float = 0.0
    updated_at: float = 0.0
    error_msg: Optional[str] = None


class SwapDB:
    """SQLite persistence for swap data."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute('''CREATE TABLE IF NOT EXISTS swaps (
            swap_id TEXT PRIMARY KEY,
            data TEXT NOT NULL,
            created_at REAL,
            updated_at REAL,
            state TEXT
        )''')
        conn.execute('''CREATE INDEX IF NOT EXISTS idx_state ON swaps(state)''')
        conn.commit()
        conn.close()

    def save(self, swap: SwapData):
        swap.updated_at = time.time()
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            'INSERT OR REPLACE INTO swaps (swap_id, data, created_at, updated_at, state) '
            'VALUES (?, ?, ?, ?, ?)',
            (swap.swap_id, json.dumps(asdict(swap)),
             swap.created_at, swap.updated_at, swap.state))
        conn.commit()
        conn.close()

    def load(self, swap_id: str) -> Optional[SwapData]:
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            'SELECT data FROM swaps WHERE swap_id = ?', (swap_id,)).fetchone()
        conn.close()
        if row:
            return SwapData(**json.loads(row[0]))
        return None

    def load_by_state(self, state: str) -> List[SwapData]:
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            'SELECT data FROM swaps WHERE state = ?', (state,)).fetchall()
        conn.close()
        return [SwapData(**json.loads(r[0])) for r in rows]

    def load_active(self) -> List[SwapData]:
        """Load all swaps that are not in a terminal state.
        BTC_CLAIMED is terminal for the maker (they got their BTC,
        the taker's MARS claim runs independently).
        """
        terminal = (SwapState.COMPLETED.value, SwapState.FAILED.value,
                     SwapState.EXPIRED.value, SwapState.MARS_REFUNDED.value,
                     SwapState.BTC_REFUNDED.value,
                     SwapState.BTC_CLAIMED.value)
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            'SELECT data FROM swaps WHERE state NOT IN (?,?,?,?,?,?)',
            terminal).fetchall()
        conn.close()
        return [SwapData(**json.loads(r[0])) for r in rows]

    def load_all(self) -> List[SwapData]:
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            'SELECT data FROM swaps ORDER BY created_at DESC').fetchall()
        conn.close()
        return [SwapData(**json.loads(r[0])) for r in rows]


class SwapEngine:
    """Orchestrates atomic swap execution.

    For a MAKER (has MARS, wants BTC):
    1. Generate preimage + hash
    2. Generate ephemeral keypair
    3. Create MARS HTLC script
    4. Fund MARS HTLC (broadcast tx)
    5. Wait for taker's BTC HTLC
    6. Verify BTC HTLC parameters
    7. Claim BTC (reveals preimage on Bitcoin chain)
    8. Done — taker extracts preimage and claims MARS

    For a TAKER (has BTC, wants MARS):
    1. Generate ephemeral keypair
    2. Receive hash from maker
    3. Wait for maker's MARS HTLC
    4. Verify MARS HTLC parameters
    5. Create BTC HTLC with same hash
    6. Fund BTC HTLC (user sends BTC to address)
    7. Wait for maker to claim BTC (reveals preimage)
    8. Extract preimage from Bitcoin chain
    9. Claim MARS using preimage
    """

    def __init__(self, wallet: 'Abstract_Wallet', network: 'Network',
                 data_dir: str):
        self.wallet = wallet
        self.network = network
        self.db = SwapDB(os.path.join(data_dir, 'atomic_swaps.db'))
        self.btc_monitor = BtcMonitor()
        self._running_swaps = {}  # swap_id -> asyncio.Task

    def create_maker_swap(
        self,
        mars_amount_sat: int,
        btc_amount_sat: int,
        current_mars_height: int,
        btc_receive_address: Optional[str] = None,
    ) -> SwapData:
        """Initialize a new swap as maker (selling MARS for BTC).

        Returns SwapData with HTLC parameters ready for funding.
        """
        swap_id = os.urandom(16).hex()
        preimage, payment_hash = generate_preimage()
        my_priv, my_pub = generate_keypair()

        mars_locktime = current_mars_height + MARS_TIMELOCK_BLOCKS

        swap = SwapData(
            swap_id=swap_id,
            role=SwapRole.MAKER.value,
            state=SwapState.CREATED.value,
            mars_amount_sat=mars_amount_sat,
            btc_amount_sat=btc_amount_sat,
            rate=btc_amount_sat / mars_amount_sat if mars_amount_sat else 0,
            preimage=preimage.hex(),
            payment_hash160=payment_hash.hex(),
            my_privkey=my_priv.hex(),
            my_pubkey=my_pub.hex(),
            mars_locktime=mars_locktime,
            btc_receive_address=btc_receive_address,
            created_at=time.time(),
        )
        self.db.save(swap)
        _logger.info(f"Created maker swap {swap_id}: "
                    f"{mars_amount_sat} MARS sat for {btc_amount_sat} BTC sat "
                    f"(BTC->: {btc_receive_address})")
        return swap

    def create_taker_swap(
        self,
        mars_amount_sat: int,
        btc_amount_sat: int,
        payment_hash160: str,
        peer_pubkey: str,
        mars_htlc_address: str,
        mars_htlc_script: str,
        mars_locktime: int,
        current_btc_height: int,
    ) -> SwapData:
        """Initialize a new swap as taker (buying MARS with BTC).

        The taker receives the hash and MARS HTLC info from the maker,
        then creates the BTC HTLC with the same hash.
        """
        swap_id = os.urandom(16).hex()
        my_priv, my_pub = generate_keypair()

        btc_locktime = current_btc_height + BTC_TIMELOCK_BLOCKS

        # Create BTC HTLC script using same hash but taker's key as recipient
        btc_script = create_htlc_script(
            payment_hash160=bfh(payment_hash160),
            recipient_pubkey=bfh(peer_pubkey),   # maker claims BTC
            sender_pubkey=my_pub,                # taker can refund
            locktime=btc_locktime,
        )
        btc_address = htlc_to_p2wsh_address(btc_script, Chain.BTC)

        swap = SwapData(
            swap_id=swap_id,
            role=SwapRole.TAKER.value,
            state=SwapState.CREATED.value,
            mars_amount_sat=mars_amount_sat,
            btc_amount_sat=btc_amount_sat,
            rate=btc_amount_sat / mars_amount_sat if mars_amount_sat else 0,
            payment_hash160=payment_hash160,
            my_privkey=my_priv.hex(),
            my_pubkey=my_pub.hex(),
            peer_pubkey=peer_pubkey,
            mars_htlc_script=mars_htlc_script,
            mars_htlc_address=mars_htlc_address,
            mars_locktime=mars_locktime,
            btc_htlc_script=btc_script.hex(),
            btc_htlc_address=btc_address,
            btc_locktime=btc_locktime,
            created_at=time.time(),
        )
        self.db.save(swap)
        _logger.info(f"Created taker swap {swap_id}: "
                    f"{btc_amount_sat} BTC sat for {mars_amount_sat} MARS sat")
        return swap

    def set_peer_info(self, swap_id: str, peer_pubkey: str):
        """Set the peer's pubkey after they accept the offer."""
        swap = self.db.load(swap_id)
        if not swap:
            raise Exception(f"Swap {swap_id} not found")
        swap.peer_pubkey = peer_pubkey

        # Now we can create the MARS HTLC script (maker knows both pubkeys)
        if swap.role == SwapRole.MAKER.value:
            my_pub = bfh(swap.my_pubkey)
            peer_pub = bfh(peer_pubkey)
            mars_script = create_htlc_script(
                payment_hash160=bfh(swap.payment_hash160),
                recipient_pubkey=peer_pub,   # taker claims MARS
                sender_pubkey=my_pub,        # maker can refund
                locktime=swap.mars_locktime,
            )
            swap.mars_htlc_script = mars_script.hex()
            swap.mars_htlc_address = htlc_to_p2wsh_address(
                mars_script, Chain.MARS)

        self.db.save(swap)

    async def fund_mars_htlc(self, swap_id: str, password=None) -> str:
        """Fund the MARS HTLC (maker step).

        Creates and broadcasts the MARS funding transaction.
        Returns the funding txid.
        """
        swap = self.db.load(swap_id)
        if not swap or swap.role != SwapRole.MAKER.value:
            raise Exception("Not a maker swap")
        if not swap.mars_htlc_address:
            raise Exception("MARS HTLC address not set (need peer pubkey)")

        tx = create_funding_tx(
            self.wallet, swap.mars_htlc_address,
            swap.mars_amount_sat, password=password)
        await self.network.broadcast_transaction(tx)

        swap.mars_funding_txid = tx.txid()
        swap.mars_funding_vout = 0  # assume first output
        # Find the actual vout
        for i, o in enumerate(tx.outputs()):
            if o.address == swap.mars_htlc_address:
                swap.mars_funding_vout = i
                break
        swap.state = SwapState.MARS_LOCKED.value
        self.db.save(swap)

        # Ensure the wallet knows about this tx so the History tab and
        # balance update immediately. Without this, ElectrumX may not
        # index the P2WSH output and the wallet won't see the spend.
        try:
            txid = tx.txid()
            if not self.wallet.db.get_transaction(txid):
                self.wallet.db.add_transaction(txid, tx)
            self.wallet.add_transaction(tx)
        except Exception as e:
            _logger.debug(f"Could not add HTLC funding tx to wallet: {e}")

        _logger.info(f"Swap {swap_id}: MARS HTLC funded, txid={tx.txid()}")
        return tx.txid()

    async def monitor_btc_htlc(self, swap_id: str) -> Optional[dict]:
        """Wait for the BTC HTLC to be funded (maker step).

        Polls mempool.space until the BTC HTLC has sufficient confirmations.
        """
        swap = self.db.load(swap_id)
        if not swap or not swap.btc_htlc_address:
            raise Exception("BTC HTLC address not set")

        result = await self.btc_monitor.wait_for_htlc_funding(
            swap.btc_htlc_address,
            swap.btc_amount_sat,
            min_confirmations=1,
        )
        if result:
            swap.btc_funding_txid = result['txid']
            swap.btc_funding_vout = result['vout']
            swap.state = SwapState.BTC_LOCKED.value
            self.db.save(swap)
            _logger.info(f"Swap {swap_id}: BTC HTLC funded, txid={result['txid']}")
        return result

    async def claim_btc(self, swap_id: str) -> str:
        """Claim BTC by revealing preimage (maker step).

        This reveals the preimage on the Bitcoin blockchain,
        allowing the taker to extract it and claim MARS.
        """
        swap = self.db.load(swap_id)
        if not swap or swap.role != SwapRole.MAKER.value:
            raise Exception("Not a maker swap")
        if not swap.preimage or not swap.btc_funding_txid:
            raise Exception("Missing preimage or BTC funding info")

        # Get current fee rate
        fee_rate = await self.btc_monitor.get_fee_rate()
        fee_sat = fee_rate * 200  # ~200 vB for a claim tx

        # Use the maker's pre-configured BTC receive address
        btc_destination = swap.btc_receive_address
        if not btc_destination:
            raise Exception(
                f"Swap {swap_id}: no BTC receive address set! "
                f"Cannot claim BTC without a destination.")

        claim_tx = create_claim_tx(
            funding_txid=swap.btc_funding_txid,
            funding_vout=swap.btc_funding_vout,
            funding_amount_sat=swap.btc_amount_sat,
            witness_script=bfh(swap.btc_htlc_script),
            preimage=bfh(swap.preimage),
            claim_privkey=bfh(swap.my_privkey),
            destination_address=btc_destination,
            fee_sat=int(fee_sat),
            destination_is_btc=True,
        )

        # Broadcast via mempool.space
        raw_hex = claim_tx.serialize()
        result_txid = await self.btc_monitor.broadcast_tx(raw_hex)
        if not result_txid:
            raise Exception(f"Failed to broadcast BTC claim tx for swap {swap_id}")

        swap.btc_claim_txid = result_txid
        swap.state = SwapState.BTC_CLAIMED.value
        self.db.save(swap)

        _logger.info(f"Swap {swap_id}: BTC claimed, txid={result_txid}, "
                    f"preimage revealed on chain")
        return raw_hex

    async def wait_for_preimage_and_claim_mars(self, swap_id: str,
                                                password=None) -> Optional[str]:
        """Wait for preimage reveal on BTC chain, then claim MARS (taker step).

        Monitors the BTC HTLC address for the maker's claim transaction,
        extracts the preimage, then claims the MARS HTLC.
        """
        swap = self.db.load(swap_id)
        if not swap or swap.role != SwapRole.TAKER.value:
            raise Exception("Not a taker swap")

        # Wait for preimage on Bitcoin chain
        preimage = await self.btc_monitor.wait_for_preimage_reveal(
            swap.btc_htlc_address)
        if not preimage:
            _logger.warning(f"Swap {swap_id}: preimage reveal timeout")
            return None

        swap.preimage = preimage.hex()

        # Claim MARS using the preimage
        my_receive_address = self.wallet.get_receiving_address()
        mars_claim_tx = create_claim_tx(
            funding_txid=swap.mars_funding_txid,
            funding_vout=swap.mars_funding_vout,
            funding_amount_sat=swap.mars_amount_sat,
            witness_script=bfh(swap.mars_htlc_script),
            preimage=preimage,
            claim_privkey=bfh(swap.my_privkey),
            destination_address=my_receive_address,
        )

        await self.network.broadcast_transaction(mars_claim_tx)

        swap.mars_claim_txid = mars_claim_tx.txid()
        swap.state = SwapState.COMPLETED.value
        self.db.save(swap)

        _logger.info(f"Swap {swap_id}: MARS claimed! Swap complete.")
        return mars_claim_tx.txid()

    async def refund_mars_htlc(self, swap_id: str, password=None) -> str:
        """Refund the MARS HTLC after timeout (maker safety net)."""
        swap = self.db.load(swap_id)
        if not swap or not swap.mars_funding_txid:
            raise Exception("No MARS funding to refund")

        my_receive_address = self.wallet.get_receiving_address()
        refund_tx = create_refund_tx(
            funding_txid=swap.mars_funding_txid,
            funding_vout=swap.mars_funding_vout,
            funding_amount_sat=swap.mars_amount_sat,
            witness_script=bfh(swap.mars_htlc_script),
            refund_privkey=bfh(swap.my_privkey),
            destination_address=my_receive_address,
            locktime=swap.mars_locktime,
        )

        await self.network.broadcast_transaction(refund_tx)

        swap.state = SwapState.MARS_REFUNDED.value
        self.db.save(swap)

        _logger.info(f"Swap {swap_id}: MARS refunded")
        return refund_tx.txid()

    async def refund_btc_htlc(
        self,
        swap_id: str,
        btc_refund_address: str,
    ) -> str:
        """Refund the BTC HTLC after timeout (taker safety net).

        The taker creates a BTC HTLC when accepting an offer. If the swap
        doesn't complete (maker never claims, goes offline, etc), after
        the locktime expires the taker can use this to recover their BTC
        to an address they control.

        Args:
            swap_id: the swap identifier
            btc_refund_address: where to send the refunded BTC

        Returns:
            The refund transaction txid
        """
        swap = self.db.load(swap_id)
        if not swap:
            raise Exception(f"Swap {swap_id} not found")
        if swap.role != SwapRole.TAKER.value:
            raise Exception("Only takers can refund BTC (they funded it)")
        if not swap.btc_htlc_script:
            raise Exception("No BTC HTLC script in swap data")

        # We need to find the actual BTC funding tx — check mempool.space
        funding_info = await self.btc_monitor.check_htlc_funded(
            swap.btc_htlc_address,
            swap.btc_amount_sat,
            min_confirmations=0,
        )
        if not funding_info:
            # Also try the stored txid (maybe already confirmed and spent?)
            if swap.btc_funding_txid:
                funding_info = {
                    'txid': swap.btc_funding_txid,
                    'vout': swap.btc_funding_vout,
                    'value': swap.btc_amount_sat,
                }
            else:
                raise Exception(
                    f"Cannot find BTC funding tx for {swap.btc_htlc_address}. "
                    f"Nothing to refund.")

        # Check we're past the locktime (best effort — Bitcoin node enforces it)
        current_height = await self.btc_monitor.get_block_height()
        if current_height and swap.btc_locktime and current_height < swap.btc_locktime:
            remaining = swap.btc_locktime - current_height
            raise Exception(
                f"Too early to refund: current BTC block {current_height}, "
                f"locktime {swap.btc_locktime}. "
                f"Wait {remaining} more blocks (~{remaining*10} minutes).")

        # Get current fee rate
        fee_rate = await self.btc_monitor.get_fee_rate()
        fee_sat = fee_rate * 200

        refund_tx = create_refund_tx(
            funding_txid=funding_info['txid'],
            funding_vout=funding_info['vout'],
            funding_amount_sat=funding_info.get('value', swap.btc_amount_sat),
            witness_script=bfh(swap.btc_htlc_script),
            refund_privkey=bfh(swap.my_privkey),
            destination_address=btc_refund_address,
            locktime=swap.btc_locktime,
            fee_sat=int(fee_sat),
            destination_is_btc=True,
        )

        # Broadcast via mempool.space
        raw_hex = refund_tx.serialize()
        result_txid = await self.btc_monitor.broadcast_tx(raw_hex)
        if not result_txid:
            raise Exception(
                f"Failed to broadcast BTC refund tx. Raw hex:\n{raw_hex}")

        swap.state = SwapState.BTC_REFUNDED.value
        self.db.save(swap)

        _logger.info(f"Swap {swap_id}: BTC refunded, txid={result_txid}")
        return result_txid

    def get_swap(self, swap_id: str) -> Optional[SwapData]:
        return self.db.load(swap_id)

    def get_active_swaps(self) -> List[SwapData]:
        return self.db.load_active()

    def get_all_swaps(self) -> List[SwapData]:
        return self.db.load_all()

    def get_swap_summary(self, swap: SwapData) -> dict:
        """Get a human-readable summary of a swap."""
        return {
            'swap_id': swap.swap_id[:8],
            'role': swap.role,
            'state': swap.state,
            'mars_amount': swap.mars_amount_sat / 1e8,
            'btc_amount': swap.btc_amount_sat / 1e8,
            'rate': swap.rate,
            'mars_htlc': swap.mars_htlc_address,
            'btc_htlc': swap.btc_htlc_address,
            'age_minutes': int((time.time() - swap.created_at) / 60),
        }
