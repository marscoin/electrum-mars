"""
Cross-chain HTLC primitives for BTC <-> MARS atomic swaps.

Adapted from submarine_swaps.py. The HTLC script is:

  OP_IF
    OP_HASH160 <hash160(preimage)> OP_EQUALVERIFY
    <recipient_pubkey> OP_CHECKSIG
  OP_ELSE
    <locktime> OP_CHECKLOCKTIMEVERIFY OP_DROP
    <sender_pubkey> OP_CHECKSIG
  OP_ENDIF

Claim path: recipient provides preimage + signature (OP_TRUE on stack for OP_IF)
Refund path: sender provides signature after locktime (OP_FALSE on stack for OP_IF)
"""

import os
from typing import Optional, Tuple
from enum import Enum

from .crypto import sha256, hash_160
from .ecc import ECPrivkey
from .bitcoin import (
    opcodes, construct_script, construct_witness,
    script_to_p2wsh, push_script, p2wsh_nested_script,
    is_segwit_address, add_number_to_script,
)
from . import segwit_addr
from .transaction import (
    PartialTxInput, PartialTxOutput, PartialTransaction,
    TxOutpoint, script_GetOp, match_script_against_template,
    OPPushDataGeneric, OPPushDataPubkey,
)
from .util import bfh, bh2u
from .logging import get_logger

_logger = get_logger(__name__)

# Timelock constants
#
# Safety rule: the party that funds LAST refunds FIRST.
# - Taker funds BTC second  -> BTC locktime must be LOWER
# - Maker funds MARS first  -> MARS locktime must be HIGHER
#
# 4h BTC / 8h MARS gives a 4-hour safety gap between the two refund
# windows. This is generous enough to tolerate Bitcoin mempool
# congestion during fee spikes (the maker's claim tx has ~4 hours
# after their theoretical worst-case broadcast time to confirm)
# while keeping the taker's recovery window short (4 hours) so a
# stalled swap feels tolerable rather than catastrophic.
BTC_TIMELOCK_BLOCKS = 24     # ~4 hours at 10 min/block
MARS_TIMELOCK_BLOCKS = 234   # ~8 hours at 123 sec/block

# Bitcoin bech32 HRPs
BTC_SEGWIT_HRP = "bc"
BTC_TESTNET_HRP = "tb"


class Chain(Enum):
    MARS = "mars"
    BTC = "btc"


# Script template for verification (matches submarine_swaps.py pattern)
HTLC_SCRIPT_TEMPLATE = [
    opcodes.OP_IF,
    opcodes.OP_HASH160,
    OPPushDataGeneric(lambda x: x == 20),   # hash160 = 20 bytes
    opcodes.OP_EQUALVERIFY,
    OPPushDataPubkey,                        # recipient pubkey
    opcodes.OP_CHECKSIG,
    opcodes.OP_ELSE,
    OPPushDataGeneric(None),                 # locktime
    opcodes.OP_CHECKLOCKTIMEVERIFY,
    opcodes.OP_DROP,
    OPPushDataPubkey,                        # sender pubkey
    opcodes.OP_CHECKSIG,
    opcodes.OP_ENDIF,
]


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
        (privkey_32bytes, compressed_pubkey_33bytes)
    """
    secret = os.urandom(32)
    privkey = ECPrivkey(secret)
    return privkey.get_secret_bytes(), privkey.get_public_key_bytes(compressed=True)


def create_htlc_script(
    payment_hash160: bytes,
    recipient_pubkey: bytes,
    sender_pubkey: bytes,
    locktime: int,
) -> bytes:
    """Build the HTLC witness script.

    Args:
        payment_hash160: RIPEMD160(SHA256(preimage)), 20 bytes
        recipient_pubkey: compressed pubkey who can claim with preimage
        sender_pubkey: compressed pubkey who can refund after locktime
        locktime: absolute block height for CLTV refund

    Returns:
        Script as bytes
    """
    assert len(payment_hash160) == 20, f"hash160 must be 20 bytes, got {len(payment_hash160)}"
    assert len(recipient_pubkey) == 33, f"recipient pubkey must be 33 bytes"
    assert len(sender_pubkey) == 33, f"sender pubkey must be 33 bytes"

    script_hex = construct_script([
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
    return bfh(script_hex)


def htlc_to_p2wsh_address(witness_script: bytes, chain: Chain,
                           testnet: bool = False) -> str:
    """Convert HTLC witness script to a P2WSH address.

    Args:
        witness_script: the HTLC script bytes
        chain: which blockchain (MARS or BTC)
        testnet: use testnet HRP

    Returns:
        bech32 P2WSH address string
    """
    script_hash = sha256(witness_script)
    if chain == Chain.MARS:
        from . import constants
        return segwit_addr.encode_segwit_address(
            constants.net.SEGWIT_HRP, 0, script_hash)
    elif chain == Chain.BTC:
        hrp = BTC_TESTNET_HRP if testnet else BTC_SEGWIT_HRP
        return segwit_addr.encode_segwit_address(hrp, 0, script_hash)
    raise ValueError(f"Unknown chain: {chain}")


def verify_htlc_script(
    script: bytes,
    expected_hash160: bytes,
    expected_recipient_pubkey: bytes,
    expected_sender_pubkey: bytes,
    expected_locktime: int,
) -> bool:
    """Verify an HTLC script matches expected parameters.

    Returns True if valid, raises Exception if not.
    """
    if not match_script_against_template(script, HTLC_SCRIPT_TEMPLATE):
        raise Exception("Script does not match HTLC template")

    parsed = list(script_GetOp(script))
    # parsed[0] = OP_IF
    # parsed[1] = OP_HASH160
    # parsed[2] = (opcode, hash160_data)
    # parsed[3] = OP_EQUALVERIFY
    # parsed[4] = (opcode, recipient_pubkey)
    # parsed[5] = OP_CHECKSIG
    # parsed[6] = OP_ELSE
    # parsed[7] = (opcode, locktime_data)
    # parsed[8] = OP_CHECKLOCKTIMEVERIFY
    # parsed[9] = OP_DROP
    # parsed[10] = (opcode, sender_pubkey)
    # parsed[11] = OP_CHECKSIG
    # parsed[12] = OP_ENDIF

    script_hash = parsed[2][1]
    script_recipient = parsed[4][1]
    script_sender = parsed[10][1]

    if script_hash != expected_hash160:
        raise Exception(f"Hash mismatch: got {script_hash.hex()}, expected {expected_hash160.hex()}")
    if script_recipient != expected_recipient_pubkey:
        raise Exception("Recipient pubkey mismatch")
    if script_sender != expected_sender_pubkey:
        raise Exception("Sender pubkey mismatch")
    # Locktime is encoded as script number
    # For simplicity, reconstruct the expected script and compare
    expected_script = create_htlc_script(
        expected_hash160, expected_recipient_pubkey,
        expected_sender_pubkey, expected_locktime)
    if script != expected_script:
        raise Exception("Script does not match expected (locktime may differ)")

    return True


def create_funding_tx(
    wallet,
    htlc_address: str,
    amount_sat: int,
    fee=None,
    password=None,
) -> PartialTransaction:
    """Create a transaction that funds an HTLC address.

    Args:
        wallet: Electrum wallet instance
        htlc_address: P2WSH address of the HTLC
        amount_sat: amount to lock in satoshis
        fee: fee rate or None for auto
        password: wallet password for signing

    Returns:
        Signed PartialTransaction ready to broadcast
    """
    output = PartialTxOutput.from_address_and_value(htlc_address, amount_sat)
    coins = wallet.get_spendable_coins(domain=None)
    tx = wallet.make_unsigned_transaction(
        coins=coins,
        outputs=[output],
        fee=fee,
    )
    wallet.sign_transaction(tx, password)
    return tx


def create_claim_tx(
    funding_txid: str,
    funding_vout: int,
    funding_amount_sat: int,
    witness_script: bytes,
    preimage: bytes,
    claim_privkey: bytes,
    destination_address: str,
    fee_sat: int = 300,
) -> PartialTransaction:
    """Create a transaction that claims an HTLC by revealing the preimage.

    This is the "happy path" — the recipient reveals the preimage to claim funds.

    Args:
        funding_txid: txid of the HTLC funding transaction
        funding_vout: output index in funding tx
        funding_amount_sat: amount locked in the HTLC
        witness_script: the HTLC script bytes
        preimage: the 32-byte preimage that hashes to the hash160 in the script
        claim_privkey: private key of the recipient (matching recipient_pubkey in script)
        destination_address: where to send the claimed funds
        fee_sat: transaction fee in satoshis

    Returns:
        Signed transaction ready to broadcast
    """
    # Create input pointing to the HTLC output
    prevout = TxOutpoint(txid=bfh(funding_txid), out_idx=funding_vout)
    txin = PartialTxInput(prevout=prevout)
    txin._trusted_value_sats = funding_amount_sat
    txin.script_type = 'p2wsh'
    txin.script_sig = b''
    txin.witness_script = witness_script
    txin.num_sig = 1

    # Create output
    claim_amount = funding_amount_sat - fee_sat
    txout = PartialTxOutput.from_address_and_value(destination_address, claim_amount)

    # Build transaction (version 2 for CLTV compatibility)
    tx = PartialTransaction.from_io([txin], [txout], version=2)

    # Sign: witness stack = [signature, preimage, OP_TRUE, witness_script]
    # OP_TRUE (0x01) selects the OP_IF branch (claim path)
    sig = bytes.fromhex(tx.sign_txin(0, claim_privkey))
    witness = construct_witness([sig, preimage, b'\x01', witness_script])
    txin.witness = bytes.fromhex(witness)

    return tx


def create_refund_tx(
    funding_txid: str,
    funding_vout: int,
    funding_amount_sat: int,
    witness_script: bytes,
    refund_privkey: bytes,
    destination_address: str,
    locktime: int,
    fee_sat: int = 300,
) -> PartialTransaction:
    """Create a transaction that refunds an HTLC after the timelock expires.

    This is the "unhappy path" — the sender reclaims after timeout.

    Args:
        funding_txid: txid of the HTLC funding transaction
        funding_vout: output index in funding tx
        funding_amount_sat: amount locked in the HTLC
        witness_script: the HTLC script bytes
        refund_privkey: private key of the sender (matching sender_pubkey in script)
        destination_address: where to send the refunded funds
        locktime: must match the CLTV locktime in the script
        fee_sat: transaction fee in satoshis

    Returns:
        Signed transaction ready to broadcast (only valid after locktime)
    """
    prevout = TxOutpoint(txid=bfh(funding_txid), out_idx=funding_vout)
    txin = PartialTxInput(prevout=prevout)
    txin._trusted_value_sats = funding_amount_sat
    txin.script_type = 'p2wsh'
    txin.script_sig = b''
    txin.witness_script = witness_script
    txin.num_sig = 1
    txin.nsequence = 0xfffffffe  # required for CLTV

    refund_amount = funding_amount_sat - fee_sat
    txout = PartialTxOutput.from_address_and_value(destination_address, refund_amount)

    # Locktime must be set for CLTV to pass
    tx = PartialTransaction.from_io([txin], [txout], version=2, locktime=locktime)

    # Sign: witness stack = [signature, OP_FALSE, witness_script]
    # OP_FALSE (empty bytes) selects the OP_ELSE branch (refund path)
    sig = bytes.fromhex(tx.sign_txin(0, refund_privkey))
    witness = construct_witness([sig, b'', witness_script])
    txin.witness = bytes.fromhex(witness)

    return tx


def extract_preimage_from_witness(tx_hex: str) -> Optional[bytes]:
    """Extract the preimage from a claim transaction's witness data.

    When the recipient claims the HTLC, they reveal the preimage in the
    witness stack. This function extracts it so the other party can use
    it to claim their side of the swap.

    Args:
        tx_hex: raw transaction hex

    Returns:
        preimage bytes if found, None otherwise
    """
    from .transaction import Transaction
    tx = Transaction(tx_hex)
    for txin in tx.inputs():
        # The witness for a claim tx is: [sig, preimage, OP_TRUE, witness_script]
        # The preimage is the second element (index 1)
        witness_bytes = txin.witness
        if witness_bytes is None:
            continue
        try:
            # Parse witness items
            items = _parse_witness(witness_bytes)
            if len(items) >= 4:
                potential_preimage = items[1]
                if len(potential_preimage) == 32:
                    # Verify it's a valid preimage by checking if hash160 appears in script
                    h = hash_160(potential_preimage)
                    witness_script = items[-1]
                    if h in witness_script:
                        return potential_preimage
        except Exception:
            continue
    return None


def _parse_witness(witness_bytes: bytes) -> list:
    """Parse a serialized witness into its component items."""
    items = []
    pos = 0
    if pos >= len(witness_bytes):
        return items
    num_items = witness_bytes[pos]
    pos += 1
    for _ in range(num_items):
        if pos >= len(witness_bytes):
            break
        length = witness_bytes[pos]
        pos += 1
        if length == 0xfd:
            length = int.from_bytes(witness_bytes[pos:pos+2], 'little')
            pos += 2
        elif length == 0xfe:
            length = int.from_bytes(witness_bytes[pos:pos+4], 'little')
            pos += 4
        item = witness_bytes[pos:pos+length]
        items.append(item)
        pos += length
    return items
