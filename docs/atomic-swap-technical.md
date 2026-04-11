# Electrum-Mars Atomic Swaps — Technical Reference

**Status:** Implemented, live on mainnet
**Version:** 4.3.2.0+mars
**Last updated:** 2026-04-11

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
10. [Threat model](#threat-model)
11. [Known limitations](#known-limitations)
12. [File map](#file-map)

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
   MARS HTLC script and derives the new address
2. Calls `engine.fund_mars_htlc(swap_id, password)` which signs and
   broadcasts the MARS funding tx
3. Advances state to `MARS_LOCKED`

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
  acceptance exists, call `set_peer_info`, then `fund_mars_htlc`.
- `MARS_LOCKED`: poll mempool.space for BTC HTLC funding. If found, update
  state to `BTC_LOCKED`.
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
  16-hour gap is the margin of safety.

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
  electrumx/server/session.py  atomicswap.* RPCs, gossip, scripthash.repair
```

---

## References

- **BIP 65** — `OP_CHECKLOCKTIMEVERIFY`: https://github.com/bitcoin/bips/blob/master/bip-0065.mediawiki
- **BIP 141** — SegWit (P2WSH witness format): https://github.com/bitcoin/bips/blob/master/bip-0141.mediawiki
- **Boltz submarine swaps** — the HTLC pattern we adapted: https://github.com/BoltzExchange
- **Decred atomic swaps** — historical reference: https://github.com/decred/atomicswap
- **marscoin/electrum-mars** — this wallet: https://github.com/marscoin/electrum-mars
- **marscoin/electrumx** — modified server: https://github.com/marscoin/electrumx
