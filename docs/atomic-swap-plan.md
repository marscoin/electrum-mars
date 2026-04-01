# P2P Atomic Swap: BTC ↔ MARS in Electrum-Mars

## Context

Small coins like Marscoin can't get exchange listings. Users have BTC and want MARS but have no way to trade. This adds a pure P2P atomic swap feature directly into the Electrum-Mars wallet — no intermediary, no custody, no regulatory surface. Anyone running the wallet can be a market maker.

Full concept doc: `/Users/novalis78/Projects/electrum-mars/docs/atomic-swap-concept.txt`
Full implementation spec: `/Users/novalis78/Projects/electrum-mars-new/docs/atomic-swap-implementation-spec.md`

## Architecture

```
Layer 1: HTLC Primitives (atomic_swap_htlc.py)       -- Script construction, signing, claiming
Layer 2: Bitcoin Chain Monitor (btc_monitor.py)       -- mempool.space API for BTC chain
Layer 3: Swap State Machine (swap_engine.py)          -- States, transitions, persistence
Layer 4: Order Book Protocol (orderbook.py)           -- Offer relay via ElectrumX RPC extension
Layer 5: Reputation System (reputation.py)            -- Per-peer success tracking
Layer 6: Plugin (plugins/atomic_swap/)                -- Hook into wallet, Qt GUI tab
```

## File Structure

```
electrum_mars/
    atomic_swap_htlc.py          # HTLC script creation, verification, signing, claiming
    btc_monitor.py               # Bitcoin chain monitoring via mempool.space REST API
    plugins/
        atomic_swap/
            __init__.py          # Plugin metadata
            swap_engine.py       # Swap state machine + SQLite persistence
            orderbook.py         # Order book protocol (ElectrumX RPC extension)
            reputation.py        # Peer reputation tracking
            qt.py                # Qt GUI: "Atomic Swap" tab, offer list, swap wizard
```

## Key Reusable Code

| What | Where | How |
|------|-------|-----|
| HTLC script template | `submarine_swaps.py:38-79` | Direct adaptation for cross-chain |
| HTLC signing/witness | `submarine_swaps.py:610-621` | Reuse `sign_tx` pattern |
| Script construction | `bitcoin.py:construct_script()` | Build HTLC scripts |
| P2WSH addresses | `bitcoin.py:script_to_p2wsh()` | Generate HTLC addresses |
| Address watching | `network.py:subscribe()` | Monitor HTLC funding |
| Transaction creation | `wallet.py:make_unsigned_transaction()` | Build swap txs |
| Plugin pattern | `plugins/cosigner_pool/` | Tab integration |

## Swap Flow

```
Alice (MARS seller)                Bob (BTC sender, wants MARS)
     |                                    |
     |--Publish offer via ElectrumX----->|
     |  "100 MARS for 0.01 BTC"          |
     |                                    |
     |<--------Bob accepts offer----------|
     |                                    |
     |--Generate preimage + hash--------->|
     |--Create MARS HTLC (lock 100 MARS)->|
     |--Broadcast to Marscoin chain------>|
     |                                    |
     |<--Bob verifies MARS HTLC----------|
     |                                    |
     |<--Bob creates BTC HTLC (0.01 BTC)-|
     |<--Broadcasts to Bitcoin chain------|
     |                                    |
     |--Alice verifies BTC HTLC--------->|
     |--Alice claims BTC (reveals preimage)
     |                                    |
     |       Bob sees preimage on BTC chain
     |<--Bob claims MARS using preimage---|
     |                                    |
     ✓ Both complete                     ✓
```

## HTLC Script

```
OP_IF
    OP_HASH160 <hash160(preimage)> OP_EQUALVERIFY
    <recipient_pubkey> OP_CHECKSIG
OP_ELSE
    <locktime> OP_CHECKLOCKTIMEVERIFY OP_DROP
    <sender_pubkey> OP_CHECKSIG
OP_ENDIF
```

Same script used on both chains (BTC and MARS). Different timelocks:
- BTC: 36 blocks (~6 hours)
- MARS: 96 blocks (~4 hours, shorter for safety)

## Swap State Machine

```
CREATED → MARS_LOCKED → BTC_LOCKED → BTC_CLAIMED → COMPLETED
                ↓            ↓            ↓
           MARS_REFUNDED  BTC_REFUNDED  MARS_CLAIMED
```

States persisted to SQLite. Background thread monitors timelocks and auto-refunds.

## ElectrumX Order Book Protocol

New RPC methods (ElectrumX relays messages only, never touches funds):

- `atomicswap.post_offer` — publish offer to order book
- `atomicswap.get_offers` — list available offers
- `atomicswap.cancel_offer` — remove offer
- `atomicswap.swap_message` — relay P2P messages between swap parties

If ElectrumX extension not available initially, offers can be exchanged via a simple JSON file on a web server or even pasted manually (MVP).

## Bitcoin Chain Monitoring

`btc_monitor.py` uses mempool.space REST API:
- `GET /api/address/{addr}/utxo` — check if BTC HTLC is funded
- `GET /api/tx/{txid}` — get transaction details (extract preimage from witness)
- Polling loop with exponential backoff (every 30s → 60s → 120s)

No Bitcoin node required. Users just need internet access.

## Qt GUI

"Atomic Swap" tab in main wallet window:

```
┌──────────────────────────────────────────┐
│ Atomic Swaps                             │
├──────────────────────────────────────────┤
│                                          │
│ [Buy MARS]  [Sell MARS]  [My Offers]    │
│                                          │
│ Available Offers:                        │
│ ┌──────────────────────────────────┐    │
│ │ 100 MARS for 0.012 BTC          │    │
│ │ Rate: 0.00012 BTC/MARS          │    │
│ │ Maker: mrs1q...xyz (⭐ 12 swaps)│    │
│ │           [Accept]               │    │
│ └──────────────────────────────────┘    │
│                                          │
│ Active Swaps:                            │
│ ┌──────────────────────────────────┐    │
│ │ Swap #a1b2: Buying 50 MARS      │    │
│ │ Status: Waiting for BTC confirm  │    │
│ │ Timelock: 5h 23m remaining      │    │
│ └──────────────────────────────────┘    │
└──────────────────────────────────────────┘
```

## Implementation Phases

### Phase 1: HTLC Primitives + CLI Test (2-3 weeks)
- `atomic_swap_htlc.py` — script creation, P2WSH addresses, signing, claiming
- `btc_monitor.py` — mempool.space API client
- CLI test: create HTLC on Marscoin regtest, claim with preimage, refund after timelock
- **Deliverable**: Can create/redeem/refund HTLCs on both chains via command line

### Phase 2: Swap Engine + P2P Protocol (2-3 weeks)
- `swap_engine.py` — state machine, SQLite persistence
- `orderbook.py` — offer relay (start with simple JSON exchange, add ElectrumX RPC later)
- End-to-end swap between two wallet instances on testnet
- **Deliverable**: Two wallets can complete an atomic swap via CLI

### Phase 3: Wallet Plugin + GUI (2-3 weeks)
- `plugins/atomic_swap/` — plugin structure, hooks
- `qt.py` — "Atomic Swap" tab, offer list, swap wizard, progress tracking
- Integrated with live wallet UI
- **Deliverable**: Non-technical users can swap via GUI

### Phase 4: Polish + Launch (1-2 weeks)
- Reputation system
- Error handling, edge cases, timeout recovery
- Documentation
- Testnet deployment, beta testing
- **Deliverable**: Production-ready feature

## Verification

1. **Unit tests**: HTLC script creation, signing, preimage extraction
2. **Regtest**: Full swap on local Marscoin regtest + Bitcoin regtest
3. **Testnet**: Swap between two wallets on Marscoin testnet + Bitcoin testnet
4. **Mainnet beta**: Small-amount swap with trusted testers

## Key Design Decisions

| Decision | Choice | Why |
|----------|--------|-----|
| HTLC type | P2WSH | SegWit, smaller witness, standard |
| Bitcoin monitoring | mempool.space API | No BTC node needed, universally accessible |
| Order book | ElectrumX RPC extension | Natural — clients already connected |
| MVP fallback | Manual offer exchange | Can work without ElectrumX changes |
| Hash function | HASH160 (SHA256+RIPEMD160) | Matches submarine_swaps.py pattern |
| Timelock type | Absolute (CLTV) | Simpler, well-tested |
