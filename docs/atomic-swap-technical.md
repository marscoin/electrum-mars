# Electrum-Mars Atomic Swaps — Technical Reference

**Status:** Implemented, live on mainnet
**Version:** 4.3.2.0+mars
**Last updated:** 2026-04-12 (post first successful mainnet swap)

This is the authoritative technical document describing the Bitcoin↔Marscoin
atomic swap system built into Electrum-Mars. It describes how the system
actually works in code, not how it might hypothetically work. If this
document and the code disagree, the code is right and this document is
wrong — please open an issue.

The goal of this document is to give a developer enough understanding to:

1. Use the atomic swap feature correctly as a user
2. Audit the cryptographic flow and verify the safety claims
3. Implement a compatible client in a different wallet or language
4. Debug swap failures by examining the state machine

---

## Table of Contents

1. [Goals and non-goals](#goals-and-non-goals)
2. [System architecture](#system-architecture)
3. [The HTLC script](#the-htlc-script)
4. [Timelock design and safety gap](#timelock-design-and-safety-gap)
5. [Swap lifecycle and state machine](#swap-lifecycle-and-state-machine)
6. [Order book relay protocol](#order-book-relay-protocol)
7. [The swap worker](#the-swap-worker)
8. [Auto-Maker](#auto-maker)
9. [Refund paths](#refund-paths)
10. [Bitcoin address handling](#bitcoin-address-handling)
11. [Code path verification audit](#code-path-verification-audit)
12. [Bug log](#bug-log)
13. [Mainnet test log](#mainnet-test-log)
14. [Threat model](#threat-model)
15. [Known limitations](#known-limitations)
16. [File map](#file-map)

---

## Goals and non-goals

### Goals

- **Trustless BTC↔MARS swaps** — neither party can cheat the other
- **Zero custody** — no server ever touches user funds
- **No KYC, no accounts** — anyone can make or take offers
- **Self-contained** — the wallet knows how to do everything; no external services required
- **Decentralized order book** — offers relay across any ElectrumX server that supports the extension
- **Recoverable** — if anything goes wrong, both parties can always refund after the timelock

### Non-goals

- **Privacy** — this is not a mixer. Swap participants are visible on both chains.
- **Instant settlement** — swaps take on the order of 10-60 minutes due to Bitcoin block times.
- **Hiding offers from non-participants** — the order book is public within the network.
- **Price discovery** — prices are set by makers from an external feed (CoinMarketCap via price.marscoin.org).

---

## System architecture

```
+---------------------------+          +---------------------------+
|  Electrum-Mars (Maker)    |          |  Electrum-Mars (Taker)    |
|                           |          |                           |
|  +----------------------+ |          |  +----------------------+ |
|  |   Atomic Swap Tab    | |          |  |   Atomic Swap Tab    | |
|  +----------------------+ |          |  +----------------------+ |
|             |             |          |             |             |
|  +----------v-----------+ |          |  +----------v-----------+ |
|  |    Swap Engine       | |          |  |    Swap Engine       | |
|  |  (state machine)     | |          |  |  (state machine)     | |
|  +----------------------+ |          |  +----------------------+ |
|  |    Swap Worker       | |          |  |    Swap Worker       | |
|  |  (background loop)   | |          |  |  (background loop)   | |
|  +----------------------+ |          |  +----------------------+ |
|  |    Order Book        | |          |  |    Order Book        | |
|  +----------------------+ |          |  +----------------------+ |
|  |    Auto-Maker        | |          |  |                      | |
|  +----------------------+ |          |  +----------------------+ |
+-------------|-------------+          +-------------|-------------+
              |                                      |
              +--------------+   +-------------------+
                             |   |
                    +--------v---v---------+
                    |   ElectrumX Relay    |
                    |                      |
                    |  atomicswap.*        |  <--- offer relay
                    |  scripthash.repair   |  <--- self-healing
                    +----------------------+
                             |
                    +--------v-----------+
                    |    marscoind       |
                    +--------------------+

            Bitcoin chain monitoring via mempool.space REST API
            Marscoin chain monitoring via ElectrumX
```

### Components

**Client-side (Electrum-Mars wallet)**

| File | Responsibility |
|---|---|
| `electrum_mars/atomic_swap_htlc.py` | HTLC script construction, P2WSH address derivation, claim/refund tx builders, preimage extraction |
| `electrum_mars/btc_monitor.py` | Bitcoin chain queries via mempool.space (funding, confirmations, broadcast, preimage detection) |
| `electrum_mars/plugins/atomic_swap/swap_engine.py` | `SwapEngine` — creates swaps, performs state transitions (`fund_mars_htlc`, `claim_btc`, `refund_btc_htlc`, etc.) and persists to SQLite |
| `electrum_mars/plugins/atomic_swap/swap_worker.py` | `SwapWorker` — background asyncio task that drives active swaps through the state machine every 30 seconds |
| `electrum_mars/plugins/atomic_swap/orderbook.py` | `OrderBook` — manages offer publication, fetching from all connected servers, deduplication |
| `electrum_mars/plugins/atomic_swap/automaker.py` | `AutoMaker` — passive market making with CMC price feed and configurable fee spread |
| `electrum_mars/plugins/atomic_swap/qt.py` | Qt GUI: Atomic Swap tab, Create Offer dialog, BTC Payment dialog, Auto-Maker dialog, Refund dialog |

**Server-side (marscoin/electrumx)**

New RPCs added as a non-breaking extension:

| RPC method | Purpose |
|---|---|
| `atomicswap.post_offer(offer)` | Publish a swap offer to the relay, with server-side gossip to peers |
| `atomicswap.get_offers()` | List all active (non-expired) offers |
| `atomicswap.cancel_offer(offer_id)` | Remove an offer |
| `atomicswap.accept_offer(offer_id, acceptance)` | Taker notifies maker of acceptance (includes taker's pubkey and BTC HTLC address) |
| `atomicswap.get_acceptance(offer_id)` | Maker polls for acceptance notifications |
| `blockchain.scripthash.repair(sh, addr)` | Self-healing address history: cross-checks ElectrumX index against marscoind's `scantxoutset`, returns any missing txs with full block context |

ElectrumX never sees or holds funds. It is a message bus and an index.

---

## The HTLC script

Both the BTC and MARS chains use the same HTLC script template. It is a
standard P2WSH script with two spending paths:

```
OP_IF
    OP_HASH160 <hash160(preimage)> OP_EQUALVERIFY
    <recipient_pubkey> OP_CHECKSIG
OP_ELSE
    <locktime> OP_CHECKLOCKTIMEVERIFY OP_DROP
    <sender_pubkey> OP_CHECKSIG
OP_ENDIF
```

### Path 1 — Claim (reveals preimage)

To spend via this path, the witness stack is:

```
[signature, preimage, OP_TRUE, witness_script]
```

`OP_TRUE` (`0x01`) selects the `OP_IF` branch. The script verifies:

1. `HASH160(preimage) == hash160_in_script` (binds the secret)
2. `CHECKSIG(recipient_pubkey, signature)` (authorizes the recipient)

Revealing the preimage on-chain makes it publicly visible in the transaction
witness. Any observer can extract it and use it to unlock the counterpart
HTLC on the other chain.

### Path 2 — Refund (after timelock)

To spend via this path, the witness stack is:

```
[signature, OP_FALSE, witness_script]
```

`OP_FALSE` (empty bytes) selects the `OP_ELSE` branch. The script verifies:

1. `CHECKLOCKTIMEVERIFY(locktime)` — tx's `nLockTime` must be ≥ script locktime
2. `CHECKSIG(sender_pubkey, signature)` — authorizes the sender

The transaction spending via the refund path must also have its input's
`nSequence` set to a non-final value (we use `0xfffffffe`) for CLTV to be
enforced by consensus.

### Key observation

**Both paths are baked into the same output.** No one can prevent the
refund path from eventually becoming usable — it's encoded in the Bitcoin
Script bytecode that miners execute. This is the fundamental safety
property that makes atomic swaps trustless.

### Script construction (reference)

```python
from electrum_mars.atomic_swap_htlc import create_htlc_script

script = create_htlc_script(
    payment_hash160=hash_160(preimage),   # 20 bytes
    recipient_pubkey=compressed_pubkey,   # 33 bytes
    sender_pubkey=compressed_pubkey,      # 33 bytes
    locktime=current_block_height + timelock_blocks,
)
```

Addresses are derived by SHA256-hashing the script and wrapping in a P2WSH
bech32 address for the target chain:

```python
from electrum_mars.atomic_swap_htlc import htlc_to_p2wsh_address, Chain

mars_address = htlc_to_p2wsh_address(script, Chain.MARS)  # mrs1q...
btc_address  = htlc_to_p2wsh_address(script, Chain.BTC)   # bc1q...
```

The **same script** produces different addresses on each chain because the
bech32 HRP differs (`mrs` vs `bc`), but the 32-byte witness program is
identical. The chains cannot reference each other, but the preimage used
in both makes the swap atomic.

---

## Timelock design and safety gap

Defined in `atomic_swap_htlc.py`:

```python
BTC_TIMELOCK_BLOCKS  = 24    # ~4 hours at 10 min/block
MARS_TIMELOCK_BLOCKS = 234   # ~8 hours at 123 sec/block
```

### The safety rule

> **The party that funds LAST must refund FIRST.**

Why this direction:

- **Taker funds second** (they send BTC after accepting the offer). They
  need to be able to exit before the maker.
- **Maker funds first** (they lock MARS when the offer is accepted). They
  need a longer window so they can safely claim BTC before their own MARS
  refund becomes available.

If it were reversed, a malicious maker could claim BTC at the last second,
then refund MARS before the taker's wallet had time to detect the preimage
and construct a MARS claim. The taker would lose BTC and not gain MARS.

### The gap

With `BTC = 4h` and `MARS = 8h`, the gap is **4 hours**. This is a
deliberate sweet-spot between user experience and safety margin:

- **User experience**: A user with a stalled swap recovers their BTC in
  4 hours — annoying but not catastrophic. Worst case recovery (both
  sides) is 8 hours, all within a single day.
- **Safety margin**: 4 hours is comfortable under all normal Bitcoin
  conditions. Even during moderate fee congestion, a claim tx at
  `halfHourFee` rates will confirm well within an hour; the maker has
  multiple hours of additional buffer before the MARS refund becomes
  possible.
- **Tolerable under fee spikes**: Even if BTC fees surge and a claim tx
  takes an unusually long time to confirm, the 4-hour gap provides
  substantial headroom before the maker could theoretically race the
  taker's preimage extraction.

A future version may implement dynamic timelocks that adjust based on
observed fee conditions, but for a human-scale P2P swap network the
static 4h/8h values are a practical sweet spot.

### What "refundable" means on each chain

- **BTC**: Uses `OP_CHECKLOCKTIMEVERIFY` with absolute block height. The
  refund tx is valid once the Bitcoin tip reaches that height.
- **MARS**: Same. Marscoin supports CLTV identically.

The client reads the current tip height from mempool.space (for BTC) or
from its own blockchain object (for MARS) before attempting a refund. The
network will reject refund txs broadcast too early.

---

## Swap lifecycle and state machine

The `SwapData` record persists in SQLite. Each swap has a `state` field
that moves through the following transitions.

### Maker (has MARS, wants BTC)

```
         +-----------+
         |  CREATED  |  <-- offer published to relay; waiting for taker
         +-----+-----+
               |
               | SwapWorker polls atomicswap.get_acceptance(offer_id)
               | Taker's pubkey received -> set_peer_info() builds the
               | MARS HTLC script; fund_mars_htlc() signs and broadcasts
               v
         +-----+------+
         | MARS_LOCKED |  <-- MARS HTLC on-chain; watching for BTC funding
         +-----+------+
               |
               | SwapWorker polls mempool.space for BTC HTLC address
               | Taker's BTC detected at sufficient confs
               v
         +-----+------+
         |  BTC_LOCKED |  <-- both sides locked; ready to claim
         +-----+------+
               |
               | SwapWorker calls claim_btc() which:
               |   - builds BTC claim tx using preimage + maker's privkey
               |   - broadcasts via mempool.space (POST /api/tx)
               |   - preimage is now visible on Bitcoin chain
               v
         +-----+------+
         | BTC_CLAIMED |  <-- terminal for maker; taker picks up preimage
         +------------+

    Failure paths (any state above MARS_LOCKED):
         +---------------+
         | MARS_REFUNDED |  <-- maker refunded after MARS timelock
         +---------------+
```

### Taker (has BTC, wants MARS)

```
         +-----------+
         |  CREATED  |  <-- user clicked Accept, BtcPaymentDialog shown
         +-----+-----+     User sends BTC to HTLC address via external
               |           wallet (Electrum, Sparrow, hardware, exchange)
               |
               | SwapWorker polls mempool.space for btc_htlc_address
               | BTC detected in mempool/chain
               v
         +-----+------+
         |  BTC_LOCKED |  <-- watching BTC chain for maker's claim tx
         +-----+------+
               |
               | SwapWorker scans spending txs of btc_htlc_address.
               | When one appears, extracts preimage from its witness.
               | Then queries MARS chain for mars_htlc_address funding.
               | Builds and broadcasts MARS claim tx.
               v
         +-----+------+
         |  COMPLETED |  <-- MARS in taker's wallet
         +------------+

    Failure paths:
         +---------------+
         | BTC_REFUNDED  |  <-- taker refunded after BTC timelock
         +---------------+
```

### State storage

Swaps are stored in `~/.electrum-mars/atomic_swaps/atomic_swaps.db`
(SQLite). Each row has `swap_id` (32-char hex) as the primary key and a
JSON blob in `data` with all fields from the `SwapData` dataclass.

Key fields persisted per swap:

- `swap_id`, `role` (`maker` or `taker`), `state`
- `mars_amount_sat`, `btc_amount_sat`, `rate`
- `preimage` (maker only — taker learns it from BTC chain)
- `payment_hash160` (both)
- `my_privkey`, `my_pubkey` (ephemeral ECDSA keypair for this swap)
- `peer_pubkey` (populated after offer acceptance)
- `mars_htlc_script`, `mars_htlc_address`, `mars_locktime`
- `btc_htlc_script`, `btc_htlc_address`, `btc_locktime`
- `mars_funding_txid`, `mars_funding_vout`
- `btc_funding_txid`, `btc_funding_vout`
- `mars_claim_txid`, `btc_claim_txid`
- `btc_receive_address` (maker — where claimed BTC goes)
- `created_at`, `updated_at`

---

## Order book relay protocol

### Offer format (JSON)

```json
{
    "offer_id": "16-byte hex random id",
    "mars_amount_sat": 10000000000,
    "btc_amount_sat": 4145,
    "rate": 0.0000004145,
    "maker_pubkey": "03ab...",
    "payment_hash160": "cc...",
    "mars_htlc_address": "",
    "mars_htlc_script": "",
    "mars_locktime": 3500000,
    "expires_at": 1775918400.0,
    "maker_address": "MLBu..."
}
```

Note that `mars_htlc_address` and `mars_htlc_script` are initially empty —
the maker can't build the script until they know the taker's pubkey. They
fill in after `set_peer_info()` is called.

### Acceptance format (JSON)

When a taker accepts an offer, they POST back to the server:

```json
{
    "taker_pubkey": "02cd...",
    "btc_htlc_address": "bc1q...",
    "btc_locktime": 850048,
    "timestamp": 1775918500.0
}
```

The maker's `SwapWorker` polls `atomicswap.get_acceptance(offer_id)` every
30 seconds. When an acceptance appears, it:

1. Calls `engine.set_peer_info(swap_id, taker_pubkey)` which rebuilds the
   MARS HTLC script and derives the `mars_htlc_address`
2. Extracts `btc_htlc_address` and `btc_locktime` from the acceptance
3. Independently recomputes the BTC HTLC script using the known
   `(payment_hash160, maker_pubkey, taker_pubkey, btc_locktime)` and
   verifies the computed P2WSH address matches the taker's claimed address.
   This prevents a malicious taker from sending a fabricated address.
4. Stores `btc_htlc_script`, `btc_htlc_address`, `btc_locktime` on the
   maker's `SwapData`
5. Calls `engine.fund_mars_htlc(swap_id, password)` which signs and
   broadcasts the MARS funding tx
6. Advances state to `MARS_LOCKED`

### Gossip and fan-out

Clients publish and fetch from ALL connected ElectrumX interfaces in
parallel using `asyncio.gather`. This gives strong propagation even
without server-side gossip:

```python
async def publish_to_electrumx(self, network, offer):
    with network.interfaces_lock:
        interfaces = list(network.interfaces.values())
    results = await asyncio.gather(
        *[iface.session.send_request('atomicswap.post_offer', [dict(offer)])
          for iface in interfaces],
        return_exceptions=True)
```

Additionally, when an ElectrumX server receives a new offer, it forwards
it to its known good peers using the internal `PeerManager` list (see
`_gossip_offer_to_peers` in `session.py`). Peers are called with
`_gossip=False` to prevent infinite propagation loops, and each server
tracks `_atomicswap_seen` for dedup within a session.

Combined, there are **three layers of propagation**:

1. Client push to all connected servers
2. Server gossip to peer servers
3. Client pull on every Refresh

---

## The swap worker

`SwapWorker` is an asyncio background task started by the plugin on
`load_wallet`. It runs one tick every 30 seconds (`POLL_INTERVAL_SEC`).

### Tick loop

```python
async def _tick(self):
    for swap in self.engine.get_active_swaps():
        if swap.swap_id in self._in_flight:
            continue
        if self._is_expired(swap):
            await self._handle_expired(swap)
            continue
        if swap.role == 'maker':
            await self._advance_maker(swap)
        else:
            await self._advance_taker(swap)
```

### Maker state transitions

- `CREATED` + no `peer_pubkey`: poll `atomicswap.get_acceptance`. If
  acceptance exists:
  1. Call `set_peer_info(taker_pubkey)` which builds the MARS HTLC script
  2. Extract `btc_htlc_address` + `btc_locktime` from acceptance
  3. **Verify**: recompute the BTC HTLC script from `(payment_hash160,
     maker_pubkey, taker_pubkey, btc_locktime)` and confirm the computed
     P2WSH address matches the taker's claimed address — reject if mismatch
  4. Store `btc_htlc_script`, `btc_htlc_address`, `btc_locktime` on swap
  5. Call `fund_mars_htlc()` to sign and broadcast the MARS funding tx
- `MARS_LOCKED`: poll mempool.space for BTC HTLC funding. If found, update
  state to `BTC_LOCKED`. Guards against `btc_htlc_address` being `None`.
- `BTC_LOCKED`: call `claim_btc` — this builds the claim tx, broadcasts
  via mempool.space, and advances state to `BTC_CLAIMED`.

### Taker state transitions

- `CREATED`: poll mempool.space for the BTC HTLC address. Once the user
  sends BTC and it appears in mempool, update state to `BTC_LOCKED`.
- `BTC_LOCKED`: scan the BTC HTLC address's spending transactions.
  When one appears, extract the preimage from its witness. Query the
  MARS chain for the MARS HTLC funding (via ElectrumX
  `blockchain.scripthash.listunspent`). Build and broadcast the MARS
  claim tx. Update state to `COMPLETED`.

### In-flight protection

The worker tracks `_in_flight` (set of swap IDs currently being
processed) to prevent the same tick from double-processing a swap.

### Expiration handling

A swap older than 8 hours with remaining funds triggers `_handle_expired`
which attempts automatic refunds where applicable. The user can also
refund manually via the UI at any time past the locktime.

---

## Auto-Maker

`AutoMaker` is a passive market making loop. When enabled, it:

1. Fetches the current MARS/USD price from `https://price.marscoin.org/json`
   (which proxies CoinMarketCap) and the BTC/USD price from
   `https://mempool.space/api/v1/prices`
2. Computes the spot rate `mars_btc = mars_usd / btc_usd`
3. Applies a configurable fee spread (default 5%): `offer_rate = mars_btc * 1.05`
4. Determines how much MARS is available (respects reserve %)
5. Splits into `num_offers` concurrent offers, each capped at `max_single_swap`
6. Creates and publishes each offer via the engine and order book

Config fields (persisted in `automaker_config.json` alongside the swap db):

| Field | Default | Purpose |
|---|---|---|
| `enabled` | False | master on/off |
| `fee_percent` | 5.0 | spread added to spot price |
| `daily_limit_mars_sat` | 10,000 MARS | 24-hour volume cap |
| `max_single_swap_sat` | 1,000 MARS | maximum per-offer |
| `min_single_swap_sat` | 10 MARS | minimum per-offer (anti-dust) |
| `reserve_percent` | 20% | balance kept unlocked |
| `num_offers` | 3 | concurrent offers to maintain |
| `refresh_interval_sec` | 300 | how often to re-price |
| `btc_receive_address` | "" | where earned BTC goes (required) |

The Auto-Maker runs as a separate asyncio task alongside the SwapWorker.
It does not make swap decisions; it just posts offers. The SwapWorker
handles the actual execution when takers accept.

---

## Refund paths

### When refunds happen

Refunds are only needed when the happy path fails. In practice:

- **Taker sent BTC but maker never claimed** → taker refunds BTC after
  `BTC_TIMELOCK_BLOCKS` (4 hours)
- **Maker locked MARS but taker never sent BTC** → maker refunds MARS
  after `MARS_TIMELOCK_BLOCKS` (8 hours)

### How the taker refunds BTC

```python
txid = await engine.refund_btc_htlc(swap_id, btc_refund_address)
```

This:

1. Loads the swap from SQLite
2. Verifies the swap is in taker mode with a valid HTLC script
3. Queries mempool.space for the HTLC funding UTXO
4. Checks current Bitcoin tip height against `btc_locktime`; refuses if
   not past the locktime
5. Constructs a refund tx using `create_refund_tx`:
   - Input: the HTLC funding outpoint, with `nSequence=0xfffffffe`
   - Output: `btc_refund_address`, minus a network fee (200 vB × current fee rate)
   - `nLockTime`: set to `btc_locktime` so CLTV passes
   - Witness: `[sig, OP_FALSE, witness_script]` (selects OP_ELSE path)
6. Broadcasts via `btc_monitor.broadcast_tx` (POST to mempool.space `/api/tx`)
7. Marks swap state as `BTC_REFUNDED`

### How the maker refunds MARS

Same pattern, but uses the wallet's own network to broadcast to the
Marscoin chain (`network.broadcast_transaction`). Current MARS block
height is checked from the local blockchain object.

### UI

The Active Swaps table shows an orange "Refund BTC" or "Refund MARS"
button on eligible rows. The `BtcRefundDialog` offers a "Check Timelock"
button that fetches current height and displays blocks remaining.

---

## Bitcoin address handling

Electrum-Mars is a Marscoin wallet. Its internal address-handling functions
(`address_to_script()`, `PartialTxOutput.from_address_and_value()`, etc.)
default to `constants.net`, which sets `SEGWIT_HRP="mars"`. This means real
Bitcoin addresses (`bc1...`, `1...`, `3...`) fail Electrum's address
validation — they are treated as "invalid addresses" because they don't
match the Marscoin network prefix.

This is a fundamental architectural tension: the wallet lives in Marscoin
space but needs to construct transactions for the Bitcoin chain.

### Solution: `btc_address_to_scriptpubkey()`

`atomic_swap_htlc.py` provides `btc_address_to_scriptpubkey(address)` which
manually builds a `scriptPubKey` from a raw Bitcoin address without touching
Electrum's address validator.

Supported address formats:

| Format | Prefix | scriptPubKey template |
|---|---|---|
| P2WPKH (bech32) | `bc1q...` (20-byte program) | `OP_0 <20 bytes>` |
| P2WSH (bech32) | `bc1q...` (32-byte program) | `OP_0 <32 bytes>` |
| P2TR (bech32m) | `bc1p...` | `OP_1 <32 bytes>` |
| P2PKH (legacy) | `1...` | `OP_DUP OP_HASH160 <20> OP_EQUALVERIFY OP_CHECKSIG` |
| P2SH (legacy) | `3...` | `OP_HASH160 <20> OP_EQUAL` |
| Testnet bech32 | `tb1...` | same as mainnet, different HRP |
| Regtest bech32 | `bcrt1...` | same as mainnet, different HRP |

The helper performs bech32/bech32m decoding via `segwit_addr.decode_segwit_address()`
for segwit, and a minimal base58check decoder for legacy formats. Checksums
are verified in both cases.

### Where `destination_is_btc=True` is required

Both `create_claim_tx()` and `create_refund_tx()` accept a
`destination_is_btc: bool = False` parameter. When `True`, they use
`btc_address_to_scriptpubkey()` instead of `from_address_and_value()`.

Callers that MUST pass `destination_is_btc=True`:

| Function | Called from | Why |
|---|---|---|
| `create_claim_tx()` | `swap_engine.claim_btc()` | Maker claims BTC to their `bc1...` address |
| `create_refund_tx()` | `swap_engine.refund_btc_htlc()` | Taker refunds BTC to their `bc1...` address |

Callers that MUST NOT pass `True`:

| Function | Called from | Why |
|---|---|---|
| `create_claim_tx()` | `swap_worker._claim_mars_now()` | Taker claims MARS to their `mrs1q...` address — Electrum's validator is correct here |
| `create_refund_tx()` | `swap_engine.refund_mars_htlc()` | Maker refunds MARS to their `mrs1q...` address — same |

### UI validation

The BTC refund dialog (`BtcRefundDialog` in `qt.py`) validates prefix only
— it checks `bc1`, `1`, or `3` prefix before passing to the engine. The
actual address decoding and checksum verification happen inside
`btc_address_to_scriptpubkey()`.

---

## Code path verification audit

Full audit of every step in the swap flow, performed 2026-04-12.
Every path listed below is backed by real code — no stubs, no placeholders.

### Cryptographic primitives

| Step | Function | Location | Implementation |
|---|---|---|---|
| Preimage generation | `generate_preimage()` | `atomic_swap_htlc.py:83-91` | `os.urandom(32)` → `hash_160()` (SHA256 + RIPEMD160) |
| Ephemeral keypair | `generate_keypair()` | `atomic_swap_htlc.py:94-102` | `ECPrivkey(os.urandom(32))` → compressed 33-byte pubkey |
| HTLC script construction | `create_htlc_script()` | `atomic_swap_htlc.py:105-141` | `construct_script()` with OP_IF/HASH160/CLTV/CHECKSIG |
| P2WSH address | `htlc_to_p2wsh_address()` | `atomic_swap_htlc.py:144-164` | SHA256(script) → `segwit_addr.encode_segwit_address(hrp, 0, hash)` |
| Script verification | `verify_htlc_script()` | `atomic_swap_htlc.py:167-214` | Template matching + field-by-field comparison + full script rebuild |
| Preimage extraction | `extract_preimage_from_witness()` | `atomic_swap_htlc.py:440-474` | Parse witness items, find 32-byte entry whose `hash_160()` appears in script |
| Witness parsing | `_parse_witness()` | `atomic_swap_htlc.py:477-499` | Manual varint-aware deserialization of witness stack |

### Transaction construction

| Step | Function | Location | Implementation |
|---|---|---|---|
| MARS funding tx | `create_funding_tx()` | `atomic_swap_htlc.py:217-245` | `wallet.make_unsigned_transaction()` → `wallet.sign_transaction()` |
| BTC claim tx (maker) | `create_claim_tx()` | `atomic_swap_htlc.py:247-307` | Witness: `[sig, preimage, OP_TRUE, witness_script]` |
| BTC refund tx (taker) | `create_refund_tx()` | `atomic_swap_htlc.py:376-437` | Witness: `[sig, OP_FALSE, witness_script]`, `nLockTime=locktime`, `nSequence=0xfffffffe` |
| BTC address → scriptPubKey | `btc_address_to_scriptpubkey()` | `atomic_swap_htlc.py:299-362` | Manual bech32/base58 decode, bypasses Electrum's Marscoin validator |

### Chain monitoring (mempool.space REST API)

| Step | Function | Location | Implementation |
|---|---|---|---|
| Check HTLC funded | `check_htlc_funded()` | `btc_monitor.py:98-148` | `GET /address/{addr}/utxo`, checks amount + confirmations |
| Wait for funding | `wait_for_htlc_funding()` | `btc_monitor.py:150-177` | Polling loop with configurable interval and timeout |
| Wait for preimage | `wait_for_preimage_reveal()` | `btc_monitor.py:179-213` | Scans spending txs of HTLC address, extracts witness |
| Get block height | `get_block_height()` | `btc_monitor.py:93-96` | `GET /blocks/tip/height` |
| Get fee rate | `get_fee_rate()` | `btc_monitor.py:215-220` | `GET /v1/fees/recommended` → `halfHourFee` |
| Broadcast tx | `broadcast_tx()` | `btc_monitor.py:222-241` | `POST /api/tx` with raw hex body |
| Get address txs | `get_address_txs()` | `btc_monitor.py:80-83` | `GET /address/{addr}/txs` |
| Get raw tx hex | `get_tx_hex()` | `btc_monitor.py:89-91` | `GET /tx/{txid}/hex` |

### State machine (SwapEngine)

| Step | Function | Location | Implementation |
|---|---|---|---|
| Create maker swap | `create_maker_swap()` | `swap_engine.py:209-245` | Generates preimage, keypair, computes MARS locktime, stores to SQLite |
| Create taker swap | `create_taker_swap()` | `swap_engine.py:247-299` | Generates keypair, builds BTC HTLC script/address, stores to SQLite |
| Set peer info (maker) | `set_peer_info()` | `swap_engine.py:301-322` | Builds MARS HTLC script/address using both pubkeys |
| Fund MARS HTLC | `fund_mars_htlc()` | `swap_engine.py:324-352` | `create_funding_tx()` → `network.broadcast_transaction()` → `MARS_LOCKED` |
| Monitor BTC HTLC | `monitor_btc_htlc()` | `swap_engine.py:354-374` | `btc_monitor.wait_for_htlc_funding()` → `BTC_LOCKED` |
| Claim BTC (maker) | `claim_btc()` | `swap_engine.py:376-423` | `create_claim_tx(destination_is_btc=True)` → `btc_monitor.broadcast_tx()` → `BTC_CLAIMED` |
| Wait + claim MARS (taker) | `wait_for_preimage_and_claim_mars()` | `swap_engine.py:425-464` | `btc_monitor.wait_for_preimage_reveal()` → `create_claim_tx()` → `network.broadcast_transaction()` → `COMPLETED` |
| Refund MARS (maker) | `refund_mars_htlc()` | `swap_engine.py:466-489` | `create_refund_tx()` → `network.broadcast_transaction()` → `MARS_REFUNDED` |
| Refund BTC (taker) | `refund_btc_htlc()` | `swap_engine.py:491-573` | `create_refund_tx(destination_is_btc=True)` → `btc_monitor.broadcast_tx()` → `BTC_REFUNDED` |

### Background worker (SwapWorker)

| Step | Function | Location | Implementation |
|---|---|---|---|
| Start/stop lifecycle | `start()` / `stop()` | `swap_worker.py:59-74` | `asyncio.get_event_loop().create_task()`, cancels on stop |
| Tick loop | `_run_loop()` → `_tick()` | `swap_worker.py:76-110` | Iterates `get_active_swaps()`, dispatches by role |
| Maker: poll for acceptance | `_check_for_acceptance()` | `swap_worker.py:253-265` | `atomicswap.get_acceptance(offer_id)` via ElectrumX |
| Maker: verify + store BTC HTLC | `_advance_maker()` lines 155-178 | `swap_worker.py:155-178` | Recomputes BTC HTLC from known params, verifies against taker's address |
| Maker: auto-fund MARS | `_advance_maker()` lines 182-191 | `swap_worker.py:182-191` | Calls `engine.fund_mars_htlc()` with wallet password |
| Maker: detect BTC funding | `_advance_maker()` MARS_LOCKED | `swap_worker.py:193-210` | `btc_monitor.check_htlc_funded()` with min_confirmations=1 |
| Maker: auto-claim BTC | `_advance_maker()` BTC_LOCKED | `swap_worker.py:212-220` | Calls `engine.claim_btc()` → broadcasts via mempool.space |
| Taker: detect own BTC | `_advance_taker()` CREATED | `swap_worker.py:225-241` | `btc_monitor.check_htlc_funded()` with min_confirmations=0 |
| Taker: extract preimage | `_check_for_preimage()` | `swap_worker.py:267-288` | Scans BTC HTLC address spending txs for witness with 32-byte preimage |
| Taker: auto-claim MARS | `_claim_mars_now()` | `swap_worker.py:290-327` | Queries MARS chain for HTLC UTXO → `create_claim_tx()` → `network.broadcast_transaction()` |
| Taker: find MARS funding | `_find_mars_htlc_funding()` | `swap_worker.py:329-350` | `blockchain.scripthash.listunspent` on MARS HTLC address |
| Expiry detection | `_is_expired()` | `swap_worker.py:112-119` | Wall-clock `age > 8h` (see Known Limitations #9) |
| Auto-refund on expiry | `_handle_expired()` | `swap_worker.py:121-134` | Calls `engine.refund_mars_htlc()` for makers with funded MARS |

### Order book protocol

| Step | Function | Location | Implementation |
|---|---|---|---|
| Publish to all servers | `publish_to_electrumx()` | `orderbook.py:224-260` | `asyncio.gather()` across all `network.interfaces` |
| Fetch from all servers | `fetch_from_electrumx()` | `orderbook.py:144-222` | Parallel query + merge + prune stale local offers |
| Offer expiry | `_cleanup_expired()` | `orderbook.py:125-130` | Removes offers past `expires_at` |
| Server-side gossip | `_gossip_offer_to_peers()` | ElectrumX `session.py` | Forwards to `PeerManager._get_recent_good_peers()` with `_gossip=False` |

### Plugin integration

| Step | Function | Location | Implementation |
|---|---|---|---|
| Plugin load | `load_wallet()` | `qt.py:50-82` | Creates `SwapEngine`, `OrderBook`, `SwapWorker`, starts worker |
| Plugin unload | `on_close_window()` | `qt.py:88-93` | Stops worker |
| Tab creation | `AtomicSwapTab.__init__()` | `qt.py:95+` | Buy/Sell/My Offers toolbar, offers table, active swaps table |
| Accept offer | `_accept_offer()` | `qt.py:585-648` | Creates taker swap, sends acceptance to ElectrumX, shows BTC payment dialog |
| Create offer | `CreateOfferDialog.accept()` | `qt.py:830-947` | Validates inputs, creates maker swap, publishes offer |
| BTC payment QR | `BtcPaymentDialog` | `qt.py` | Shows HTLC address as QR + copyable text, polls mempool.space for confirmation |
| BTC refund dialog | `BtcRefundDialog` | `qt.py` | Check Timelock + Broadcast Refund buttons, validates bc1/1/3 prefix |

---

## Bug log

Bugs discovered and fixed during development and mainnet testing.
Organized chronologically with commit references.

### BUG-001: Maker had no BTC receive address (2026-04-10)

**Commit:** `b110a9814`
**Severity:** Critical — maker's claim tx would send BTC to the HTLC address itself (nonsensical)

**Root cause:** The `claim_btc()` function had a placeholder destination.
There was no UI field for the maker to specify where to receive BTC, and no
`btc_receive_address` field on `SwapData`.

**Fix:** Added required "BTC receive address" field to Create Offer dialog
and Auto-Maker dialog. Stored in `SwapData.btc_receive_address`. `claim_btc()`
raises if missing. Validated prefix (`bc1`/`1`/`3`). Saved between uses in
wallet config.

### BUG-002: BTC refund rejected real Bitcoin bc1 addresses (2026-04-11)

**Commit:** `a0b56b214`
**Severity:** Critical — taker could not refund BTC

**Root cause:** `create_refund_tx()` called
`PartialTxOutput.from_address_and_value(btc_addr, ...)` which internally calls
`address_to_script()` in `bitcoin.py`. This validates against
`constants.net` which defaults to Marscoin (`SEGWIT_HRP="mrs"`). Real
Bitcoin `bc1...` addresses fail the bech32 HRP check and raise "invalid
bitcoin address".

**Symptom:** User entered a valid Exodus wallet address
`bc1qxtts9kqcd22ck5upvrhz9zs3geaufp66my95e6` in the refund dialog and got
"invalid bitcoin address" error.

**Fix:** Added `btc_address_to_scriptpubkey()` helper that manually builds
the scriptPubKey for real Bitcoin addresses without going through Electrum's
Marscoin-aware address validator. Added `destination_is_btc: bool = False`
parameter to `create_refund_tx()`. Engine passes `True` for BTC-side
refunds.

**Proven on mainnet:** Taker successfully refunded 0.00004392 BTC from HTLC
`bc1q0wja8cwf9q84expekd2r0kzk84gfjf5nktmqecutwt68zrg36vsqgz3hee`
to Exodus wallet address. Confirmed on Bitcoin blockchain.

### BUG-003: BTC claim also rejected real Bitcoin bc1 addresses (2026-04-11)

**Commit:** `45f1d819d`
**Severity:** Critical — maker could not claim BTC

**Root cause:** Same as BUG-002, but in `create_claim_tx()` instead of
`create_refund_tx()`. The claim path calls
`from_address_and_value(btc_receive_address, ...)` which hits the same
Marscoin validator.

**Symptom:** Not hit in testing yet (no successful swap had reached the
BTC_LOCKED state), but would have crashed on the first successful swap.

**Fix:** Same pattern: added `destination_is_btc=True` parameter to
`create_claim_tx()`. Engine passes `True` in `claim_btc()`.

### BUG-004: Maker never stored BTC HTLC address from acceptance (2026-04-12)

**Commit:** `08a3e9104`
**Severity:** Critical — swap could never progress past MARS_LOCKED

**Root cause:** When a taker accepts an offer, they compute the BTC HTLC
and send `btc_htlc_address` + `btc_locktime` in the acceptance dict to
ElectrumX. The maker's `SwapWorker._advance_maker()` polled this acceptance
and called `set_peer_info()` — but `set_peer_info()` only built the MARS
HTLC. It never stored the BTC HTLC info on the maker's `SwapData`.

When the swap advanced to `MARS_LOCKED`, the worker called
`btc_monitor.check_htlc_funded(swap.btc_htlc_address, ...)` where
`swap.btc_htlc_address` was `None`. The worker would either crash or poll
forever.

**Fix:** After `set_peer_info()`, the worker now:
1. Extracts `btc_htlc_address` and `btc_locktime` from the acceptance
2. Independently recomputes the BTC HTLC script using the known pubkeys and
   payment hash — verifies the computed address matches the taker's claim
3. Stores `btc_htlc_script`, `btc_htlc_address`, `btc_locktime` on the swap
4. Added a guard on the `MARS_LOCKED` state that logs and returns if
   `btc_htlc_address` is still missing

The verification step is important: it prevents a malicious taker from
sending a fabricated BTC HTLC address that doesn't match the agreed HTLC
parameters.

### BUG-005: Taker expiry check blocked preimage extraction (2026-04-12)

**Commit:** `519e24697`
**Severity:** High — taker could never claim MARS if swap took > 8 hours

**Root cause:** `SwapWorker._is_expired()` uses wall-clock time
(`age > 8 hours`). Once a swap exceeded 8 hours (e.g. because Marscoin
miners were producing empty blocks and the HTLC funding tx took 9 hours
to confirm), the worker routed the swap to `_handle_expired()` instead of
`_advance_taker()`. But `_handle_expired()` only handles maker MARS
refunds — it did nothing for takers. The taker's preimage extraction
and MARS claim were silently skipped.

**Fix:** Takers at `BTC_LOCKED` state now bypass the expiry check and
always fall through to `_advance_taker()`. They must always attempt to
extract the preimage regardless of age.

### BUG-006: SEGWIT_HRP mismatch — "mrs" vs "mars" (2026-04-12)

**Commit:** `ea512dd1b`
**Severity:** Medium — addresses worked but were incompatible with marscoind

**Root cause:** `constants.py` defined `SEGWIT_HRP = "mrs"` for mainnet,
but marscoind's `chainparams.cpp` defines `bech32_hrp = "mars"`. Both
produce valid bech32 addresses with identical underlying scriptPubKeys,
but the human-readable strings are different (`mrs1q...` vs `mars1q...`).
This caused confusion when comparing addresses between the wallet, the
block explorer, and marscoind.

**Fix:** Changed to `SEGWIT_HRP = "mars"` (mainnet) and `"tmars"`
(testnet) to match marscoind exactly. No reindex or migration needed —
the scriptPubKey is derived from the witness program, not the HRP.

### BUG-007: Taker had empty MARS HTLC script and address (2026-04-12)

**Commit:** `4c37724c2`
**Severity:** Critical — taker could never find or claim the MARS HTLC

**Root cause:** The offer is published before the maker knows the taker's
pubkey. At that point, `mars_htlc_script` and `mars_htlc_address` are
both empty strings. The taker stored these empty values in `SwapData`
when accepting the offer. When `_claim_mars_now()` ran, it called
`_find_mars_htlc_funding()` which tried to compute
`address_to_scripthash('')` — which always failed.

**Fix:** `_claim_mars_now()` now recomputes the MARS HTLC script from
scratch when it's missing. The taker knows all required parameters:
`payment_hash160`, `my_pubkey` (taker = recipient who claims MARS),
`peer_pubkey` (maker = sender who can refund), and `mars_locktime`.
The script is deterministic, so the taker computes the exact same
script the maker built.

### BUG-008: ElectrumX doesn't index P2WSH outputs by address (2026-04-12)

**Commit:** `c421d49` (marscoin/electrumx repo)
**Severity:** Medium — workaround exists via `repair` RPC

**Root cause:** ElectrumX's `pay_to_address_script()` only handles base58
addresses (P2PKH/P2SH). It has no bech32 decoder, so it cannot convert
`mars1q...` addresses to scriptPubKeys. This means `address_to_hashX()`
fails for segwit addresses, and any RPC that looks up an address
internally returns empty results.

Note: ElectrumX *does* index P2WSH outputs correctly during block
processing — it hashes the raw scriptPubKey bytes and stores the hashX.
The issue is only with address→script conversion in certain RPCs. The
client-side `address_to_scripthash()` works correctly because Electrum
has its own bech32 decoder.

**Fix (ElectrumX):**
1. Added `electrumx/lib/segwit_addr.py` — BIP 173/350 bech32 codec
2. Added `SEGWIT_HRP = "mars"` to the Marscoin coin class
3. Updated `pay_to_address_script()` to try bech32 decode before
   falling back to base58
4. Added `P2WPKH_script()` and `P2WSH_script()` to `ScriptPubKey`

**Workaround (client):** `_find_mars_htlc_funding()` uses the `repair`
RPC as a fallback, which queries marscoind's `scantxoutset` directly
and returns the raw tx hex. The worker then parses the tx to find the
correct vout. This works even when ElectrumX's index is missing the
P2WSH entry.

### BUG-009: Repair RPC result not used directly (2026-04-12)

**Commit:** `517726c87`
**Severity:** High — repair found the data but worker ignored it

**Root cause:** `_find_mars_htlc_funding()` called the `repair` RPC,
received the tx data back, then retried `listunspent` — which still
returned empty because `repair` doesn't actually fix the ElectrumX
index. The repair result contained `tx_hash` and `tx_hex` but the code
never parsed the raw tx to extract the vout.

**Fix:** Now parses the `tx_hex` from the repair result directly using
`Transaction(tx_hex)`, iterates outputs, and returns the vout whose
value matches the expected MARS amount. No retry of `listunspent` needed.

---

## Mainnet test log

### Test 1: BTC refund path (2026-04-11)

**Purpose:** Verify that a taker can recover BTC from a stalled swap.

**Setup:**
- Maker posted an offer for 100 MARS at 0.0000004145 BTC/MARS
- Taker (user on second machine) accepted the offer
- Taker sent 0.00004392 BTC to the generated HTLC address

**BTC HTLC:**
- Address: `bc1q0wja8cwf9q84expekd2r0kzk84gfjf5nktmqecutwt68zrg36vsqgz3hee`
- Funding txid: `be716dde2fb75b2d4abe5a495c32c8fe0ed72353ab444d72a783b298b02558bc`
- Confirmed at block: 944596
- Locktime: 850036

**Issue encountered:** When attempting refund, user entered their Exodus
wallet `bc1qxtts9kqcd22ck5upvrhz9zs3geaufp66my95e6` and got "invalid
bitcoin address" (BUG-002 above).

**Resolution:** Fixed the Marscoin address validator issue. Rebuilt .app,
retried refund.

**Result:** Refund broadcast successful. BTC appeared as "receiving" in
Exodus wallet. Confirmed on Bitcoin blockchain (2026-04-12).

**What was proven:**
- HTLC construction produces valid P2WSH scripts accepted by Bitcoin nodes
- CLTV refund path works with real Bitcoin consensus rules
- Witness construction `[sig, OP_FALSE, witness_script]` is correct
- mempool.space broadcast API works for real mainnet txs
- The entire refund safety net is functional with real money

### Test 2: Full end-to-end atomic swap (2026-04-12)

**Purpose:** Complete a fully automated BTC↔MARS atomic swap on mainnet.

**Setup:**
- Maker (machine A): Electrum-Mars wallet with 127 MARS balance
- Taker (machine B): Electrum-Mars wallet, separate machine
- BTC sent from taker's external Bitcoin wallet

**Swap parameters:**
- Offer: 104 MARS for 0.00003652 BTC
- Rate: ~0.000000351 BTC/MARS
- BTC timelock: 24 blocks (~4 hours)
- MARS timelock: 234 blocks (~8 hours)

**Timeline:**

| Time | Event | Automated? |
|---|---|---|
| T+0 | Maker creates offer, published to ElectrumX | User action |
| T+0 | Taker accepts offer, sends acceptance via ElectrumX | User action |
| T+1m | Maker's SwapWorker detects acceptance, verifies BTC HTLC params | Auto |
| T+1m | Maker auto-funds MARS HTLC (104 MARS to `mars1qajn9e8...`) | Auto |
| T+2m | Taker sends 0.00003652 BTC to BTC HTLC address (`bc1qr29h6...`) | User action |
| T+~12m | BTC confirms (1 confirmation, block 944772) | Bitcoin network |
| T+~12m | Maker's SwapWorker detects BTC funding | Auto |
| T+~13m | Maker auto-claims BTC — preimage revealed on Bitcoin chain (block 944773) | Auto |
| T+~13m | Maker receives 0.00003452 BTC in Bitcoin wallet (Exodus) | Auto |
| T+~13m | Taker's SwapWorker extracts preimage from BTC claim tx witness | Auto |
| T+~13m | Taker auto-builds MARS claim tx, broadcasts to Marscoin network | Auto |
| T+~9h | MARS claim tx mined (block 3450475) — empty-block mining delay | Marscoin network |
| T+~9h | Taker receives 104 MARS in wallet | Auto |

**Key transactions:**

| Chain | Type | TXID | Block |
|---|---|---|---|
| MARS | HTLC funding (maker) | `47c03f61bedca74101e8ee9ed318105bb2e19e60e0935e357a6d86c1f89344f4` | 3450475 |
| BTC | HTLC funding (taker) | `bd05cf8d1eeec9be2939f578d850b90e7d8d9f862ec8269414cc3b1bc99fe30d` | 944772 |
| BTC | Claim (maker, reveals preimage) | `36fa9d7f5955d3283872c6cb267c2c4e6576441f662293d8d5929aa997b1adaa` | 944773 |
| MARS | Claim (taker, uses preimage) | `afa73eb38e405224d17a0d0a11c138230f69bf4be7ebaad7124610f2dd41d871` | pending |

**Preimage:** `e3159a59181410141b3ad14d9acd730d562a3b8a0594b2705a94e2a11e208546`

**HTLC addresses:**
- BTC: `bc1qr29h67w7dgr3lryvpfterxuggluzfxsyunnmctykw66uesl90muqzs8u3l`
- MARS: `mars1qajn9e8882v37xfk5maewqkw7e0u4jzsnjsemrrcazlah5x0fkshqwc5dkg`

**Issues encountered during test:**
1. MARS HTLC funding tx sat in mempool for 9 hours because miners
   were producing empty blocks (not an atomic swap bug — a mining
   infrastructure issue, reported to miner)
2. Taker's SwapWorker was blocked by the 8-hour wall-clock expiry
   check (BUG-005). Fixed mid-test.
3. Taker had empty `mars_htlc_script` — offer was published before
   the maker knew the taker's pubkey (BUG-007). Fixed mid-test.
4. ElectrumX's `listunspent` returned empty for the P2WSH HTLC
   address (BUG-008). Fell back to `repair` RPC which queries
   marscoind directly. Fixed mid-test by parsing repair result
   (BUG-009).
5. `SEGWIT_HRP` mismatch between wallet ("mrs") and marscoind
   ("mars") caused address format confusion (BUG-006). Fixed mid-test.

**What was proven:**
- Full atomic swap lifecycle works on mainnet with real BTC and MARS
- SwapWorker drives all state transitions automatically after initial
  user actions (create offer, accept offer, send BTC)
- Preimage extraction from Bitcoin witness data works correctly
- Cross-chain atomicity holds: maker got BTC, taker got MARS, nobody
  could cheat
- HTLC scripts are valid on both Bitcoin and Marscoin consensus
- The `repair` RPC provides a viable workaround for ElectrumX's P2WSH
  indexing gap
- The system recovers from mining delays — 9 hours of empty blocks
  did not break the swap (after expiry check fix)
- Fee bump to 0.001 MARS (100000 sat) for claim/refund txs

**What was NOT yet proven:**
- Concurrent swaps (multiple offers active simultaneously)
- Auto-Maker passive market making
- Taker refund after a genuinely failed swap (Test 1 covered maker refund)
- Swap with an untrusted counterparty

---

## Threat model

### What atomic swaps guarantee

- **Atomicity**: If both HTLCs are funded, either both complete or both
  refund. Partial completion is impossible.
- **No double-spend**: Each HTLC can only be spent once. If the recipient
  claims it, the refund path becomes moot (the output no longer exists).
- **No custody theft**: No intermediary ever controls the funds. The only
  way to spend an HTLC output is to satisfy its script.
- **No fake preimages**: The preimage is bound by `OP_HASH160`. A different
  preimage cannot unlock the HTLC.

### What atomic swaps do NOT protect against

- **Spam offers**: A malicious actor can flood the order book with fake
  offers. We have no anti-spam today. Server-side rate limiting is a
  future addition.
- **Stale price**: If the maker's price feed is manipulated, they may
  offer an unfavorable rate. The maker is responsible for their own
  pricing.
- **Censorship by relay**: If ElectrumX operators refuse to relay offers,
  swaps cannot be discovered. Mitigated by running multiple independent
  servers.
- **Privacy leaks**: Swap participants are visible on both chains. Anyone
  watching both chains can correlate.
- **Fee bidding wars**: If BTC fees spike during a swap, the maker's
  claim tx might not confirm before the MARS refund becomes available. The
  4-hour gap is the margin of safety.

### Concurrent acceptance of the same offer

**Known issue**: Two takers can accept the same offer simultaneously.
Both will get the same BTC HTLC parameters (since the offer is pinned),
and if both send BTC, the maker can only claim one — the other is
stranded until refund. A future version will add a server-side
`atomicswap.reserve_offer` with short timeout to prevent this.

### Eclipse attacks

A taker whose view of the Bitcoin chain is manipulated (e.g. connected to
only malicious peers) might not see the maker's claim tx and thus miss
the preimage reveal. Mitigation: use multiple mempool.space mirrors or
your own Bitcoin node.

---

## Known limitations

1. **No reputation system**: Makers and takers are anonymous. A user who
   has completed many swaps successfully has no durable identity.

2. **No cancellation lock**: Multiple takers can accept the same offer
   (see above).

3. **Mempool.space dependency**: The client trusts mempool.space for BTC
   chain data. A future version should allow connecting to a personal
   Bitcoin node or electrum-btc.

4. **No SegWit v1 (Taproot)**: HTLCs use P2WSH (witness v0). Taproot
   adaptor signatures would be more efficient and private but require
   more code.

5. **Fixed fee rate for claims/refunds**: Uses mempool.space's
   `halfHourFee` estimate multiplied by ~200 vB. Not RBF-ready.

6. **No partial fills**: An offer for 100 MARS must be taken in full.

7. **Single Bitcoin wallet per swap**: The taker uses an ephemeral key
   generated for the specific swap. Refund requires the local SQLite
   `atomic_swaps.db` to be intact — if the wallet is lost, the refund is
   lost. Backups are essential.

8. **ElectrumX extension is optional but required for discovery**:
   Without a patched ElectrumX server, clients must exchange offers
   manually via JSON. The Manual Exchange tab supports this.

9. **Wall-clock swap expiry**: `SwapWorker._is_expired()` uses wall-clock
   time (`age > 8 hours`) rather than comparing block heights. This means
   a swap that sits unaccepted for 7 hours and is then taken will have
   only ~1 hour of worker time before auto-expiry kills it. The correct
   approach would be to compare the current block height against the
   swap's locktime, but this requires a network call on every tick. For
   the current human-scale swap volume this is an acceptable trade-off.

10. **Acceptance overwrite**: `atomicswap.accept_offer` on ElectrumX
    stores the acceptance in a dict keyed by `offer_id`. If two takers
    accept the same offer, the second overwrites the first. The first
    taker may have already funded BTC to a now-orphaned HTLC. Both
    takers can refund after their respective timelocks, but neither swap
    completes. A future version should implement first-valid-wins
    semantics or an offer reservation protocol.

11. **No acceptance release**: When a swap fails (refunded, expired,
    cancelled), neither party sends a "release" message to the ElectrumX
    relay. The stale acceptance lingers until the server restarts. The
    offer itself may also remain listed until its `expires_at` timestamp
    passes. A clean design would have refund/cancel paths call
    `atomicswap.cancel_offer` and a new `atomicswap.release_acceptance`.

12. **Griefing via acceptance**: An attacker can accept any offer with a
    garbage pubkey. The maker's wallet auto-funds a MARS HTLC to that
    pubkey. The attacker never funds BTC (they never intended to). The
    maker's MARS is frozen for 8 hours until the refund locktime. Cost
    to attacker: zero. Mitigation: require user confirmation before
    auto-funding MARS HTLC (currently the worker auto-funds immediately).

---

## File map

### Client

```
electrum_mars/
  atomic_swap_htlc.py          HTLC script, P2WSH addresses, claim/refund tx
  btc_monitor.py               mempool.space client (polling, broadcast)
  plugins/atomic_swap/
    __init__.py                plugin metadata (default_on = True)
    swap_engine.py             state machine + SQLite persistence
    swap_worker.py             background task that drives state transitions
    orderbook.py               parallel fetch/publish across all servers
    automaker.py               passive market making
    qt.py                      Qt GUI (tab, dialogs, BtcRefundDialog, etc.)
```

### Server

```
marscoin/electrumx:
  electrumx/lib/segwit_addr.py   BIP 173/350 bech32/bech32m codec
  electrumx/lib/script.py        P2WPKH_script, P2WSH_script added
  electrumx/lib/coins.py         SEGWIT_HRP="mars", bech32 in pay_to_address_script
  electrumx/server/session.py    atomicswap.* RPCs, gossip, scripthash.repair
```

---

## References

- **BIP 65** — `OP_CHECKLOCKTIMEVERIFY`: https://github.com/bitcoin/bips/blob/master/bip-0065.mediawiki
- **BIP 141** — SegWit (P2WSH witness format): https://github.com/bitcoin/bips/blob/master/bip-0141.mediawiki
- **Boltz submarine swaps** — the HTLC pattern we adapted: https://github.com/BoltzExchange
- **Decred atomic swaps** — historical reference: https://github.com/decred/atomicswap
- **marscoin/electrum-mars** — this wallet: https://github.com/marscoin/electrum-mars
- **marscoin/electrumx** — modified server: https://github.com/marscoin/electrumx
