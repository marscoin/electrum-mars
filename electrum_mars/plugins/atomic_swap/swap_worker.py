"""
Background worker that drives the atomic swap state machine.

Runs inside the Electrum-Mars plugin. Every REFRESH_INTERVAL seconds it
loops through all active swaps and advances their state by calling the
appropriate engine method for the current state.

State machine:

MAKER flow (selling MARS for BTC):
    CREATED (peer_pubkey set)
        -> fund_mars_htlc()
    MARS_LOCKED
        -> monitor_btc_htlc()  (polls mempool.space)
    BTC_LOCKED
        -> claim_btc()  (reveals preimage on Bitcoin chain)
    BTC_CLAIMED (terminal for maker — taker will pick up preimage)

TAKER flow (buying MARS with BTC):
    CREATED (after user accepts offer, dialog shown)
        -> monitor_btc_htlc()  (waits for user to send BTC via external wallet)
    BTC_LOCKED
        -> wait_for_preimage_and_claim_mars()  (watches BTC chain for preimage)
    COMPLETED (MARS in taker's wallet)
"""

import asyncio
import time
from typing import TYPE_CHECKING, Optional

from electrum_mars.logging import get_logger
from .swap_engine import SwapEngine, SwapState, SwapRole, SwapData

if TYPE_CHECKING:
    from electrum_mars.network import Network

_logger = get_logger(__name__)

# How often the worker wakes up to check swaps
POLL_INTERVAL_SEC = 30


class SwapWorker:
    """Drives active swaps forward automatically in the background."""

    def __init__(self, engine: SwapEngine, password_getter=None):
        """Args:
            engine: the SwapEngine instance
            password_getter: optional callable returning the wallet password
                (needed to sign MARS funding/refund transactions)
        """
        self.engine = engine
        self.password_getter = password_getter
        self._task: Optional[asyncio.Task] = None
        self._running = False
        # Track in-flight operations so we don't double-process
        self._in_flight = set()

    def start(self):
        """Start the background worker loop."""
        if self._task and not self._task.done():
            return
        self._running = True
        loop = asyncio.get_event_loop()
        self._task = loop.create_task(self._run_loop())
        _logger.info("SwapWorker started")

    def stop(self):
        """Stop the worker."""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        _logger.info("SwapWorker stopped")

    async def _run_loop(self):
        """Main loop: check and advance active swaps."""
        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                _logger.exception(f"SwapWorker tick error: {e}")
            await asyncio.sleep(POLL_INTERVAL_SEC)

    async def _tick(self):
        """One iteration — check each active swap and advance if possible."""
        active = self.engine.get_active_swaps()
        if not active:
            return

        print(f"[SwapWorker] tick: {len(active)} active swaps")
        _logger.debug(f"SwapWorker tick: {len(active)} active swaps")

        for swap in active:
            if swap.swap_id in self._in_flight:
                continue
            # Dispatch by role + state.
            # IMPORTANT: Do NOT skip takers at BTC_LOCKED due to expiry —
            # they must always try to extract the preimage and claim MARS,
            # even if the swap is old (e.g. MARS mining was slow).
            if self._is_expired(swap):
                # Takers at BTC_LOCKED still need to claim — fall through
                if (swap.role == SwapRole.TAKER.value
                        and swap.state == SwapState.BTC_LOCKED.value):
                    pass  # don't skip — let _advance_taker handle it
                else:
                    await self._handle_expired(swap)
                    continue
            try:
                if swap.role == SwapRole.MAKER.value:
                    await self._advance_maker(swap)
                elif swap.role == SwapRole.TAKER.value:
                    await self._advance_taker(swap)
            except Exception as e:
                print(f"[SwapWorker] ERROR advancing {swap.swap_id[:8]}: {e}")
                import traceback; traceback.print_exc()
                _logger.warning(
                    f"SwapWorker error advancing {swap.swap_id[:8]}: {e}")

    def _is_expired(self, swap: SwapData) -> bool:
        """Check if the swap's timelock has passed."""
        # We don't have absolute block heights to compare directly at tick time
        # without a network call; use a conservative wall-clock cutoff: if the
        # swap is older than ~8 hours (BTC timelock is 6h), it's definitely
        # past the point where normal flow can complete.
        age = time.time() - swap.created_at
        return age > 8 * 3600

    async def _handle_expired(self, swap: SwapData):
        """Handle an expired swap: attempt refund if possible."""
        if swap.role == SwapRole.MAKER.value and swap.mars_funding_txid:
            if swap.state not in (SwapState.BTC_CLAIMED.value,
                                   SwapState.COMPLETED.value):
                try:
                    self._in_flight.add(swap.swap_id)
                    password = self._get_password()
                    await self.engine.refund_mars_htlc(
                        swap.swap_id, password=password)
                    _logger.info(
                        f"Expired swap {swap.swap_id[:8]}: MARS refunded")
                finally:
                    self._in_flight.discard(swap.swap_id)

    async def _advance_maker(self, swap: SwapData):
        """Advance a maker swap through its state machine."""
        state = swap.state

        if state == SwapState.CREATED.value:
            # Poll the ElectrumX relay to see if a taker accepted our offer
            if not swap.peer_pubkey:
                acceptance = await self._check_for_acceptance(swap)
                if not acceptance:
                    return  # still waiting
                # Got an acceptance — record the taker's pubkey and
                # have the engine build the MARS HTLC
                self.engine.set_peer_info(swap.swap_id, acceptance['taker_pubkey'])
                swap = self.engine.get_swap(swap.swap_id)
                _logger.info(
                    f"Maker {swap.swap_id[:8]}: taker accepted, "
                    f"peer_pubkey={acceptance['taker_pubkey'][:16]}...")

                # The taker also computed the BTC HTLC and sent us
                # the address + locktime. We need to independently
                # verify and store them so we can monitor and claim.
                btc_htlc_addr = acceptance.get('btc_htlc_address')
                btc_locktime = acceptance.get('btc_locktime')
                if btc_htlc_addr and btc_locktime:
                    # Verify: recompute the BTC HTLC from known params
                    from electrum_mars.atomic_swap_htlc import (
                        create_htlc_script, htlc_to_p2wsh_address, Chain)
                    from electrum_mars.util import bfh
                    btc_script = create_htlc_script(
                        payment_hash160=bfh(swap.payment_hash160),
                        recipient_pubkey=bfh(swap.my_pubkey),    # maker claims BTC
                        sender_pubkey=bfh(swap.peer_pubkey),     # taker refunds
                        locktime=btc_locktime,
                    )
                    computed_addr = htlc_to_p2wsh_address(btc_script, Chain.BTC)
                    if computed_addr != btc_htlc_addr:
                        _logger.error(
                            f"Maker {swap.swap_id[:8]}: BTC HTLC address "
                            f"mismatch! taker={btc_htlc_addr}, "
                            f"computed={computed_addr} — possible attack")
                        return
                    swap.btc_htlc_script = btc_script.hex()
                    swap.btc_htlc_address = btc_htlc_addr
                    swap.btc_locktime = btc_locktime
                    self.engine.db.save(swap)
                    _logger.info(
                        f"Maker {swap.swap_id[:8]}: verified BTC HTLC "
                        f"addr={btc_htlc_addr[:20]}...")

            if not swap.mars_htlc_address:
                return

            # A taker has accepted — fund the MARS HTLC
            self._in_flight.add(swap.swap_id)
            try:
                password = self._get_password()
                txid = await self.engine.fund_mars_htlc(
                    swap.swap_id, password=password)
                _logger.info(
                    f"Maker {swap.swap_id[:8]}: funded MARS HTLC tx={txid}")
            finally:
                self._in_flight.discard(swap.swap_id)

        elif state == SwapState.MARS_LOCKED.value:
            # MARS is locked, wait for the taker to fund the BTC HTLC
            if not swap.btc_htlc_address:
                _logger.debug(
                    f"Maker {swap.swap_id[:8]}: no BTC HTLC address yet")
                return
            result = await self.engine.btc_monitor.check_htlc_funded(
                swap.btc_htlc_address,
                swap.btc_amount_sat,
                min_confirmations=1,
            )
            if result:
                swap.btc_funding_txid = result['txid']
                swap.btc_funding_vout = result['vout']
                swap.state = SwapState.BTC_LOCKED.value
                self.engine.db.save(swap)
                _logger.info(
                    f"Maker {swap.swap_id[:8]}: BTC HTLC confirmed, "
                    f"txid={result['txid']}")

        elif state == SwapState.BTC_LOCKED.value:
            # We can claim the BTC now (reveals preimage on chain)
            self._in_flight.add(swap.swap_id)
            try:
                await self.engine.claim_btc(swap.swap_id)
                _logger.info(
                    f"Maker {swap.swap_id[:8]}: claimed BTC, "
                    f"swap effectively complete")
            finally:
                self._in_flight.discard(swap.swap_id)

    async def _advance_taker(self, swap: SwapData):
        """Advance a taker swap through its state machine."""
        state = swap.state

        if state == SwapState.CREATED.value:
            # Taker has accepted offer and (probably) sent BTC to the
            # HTLC address via an external wallet. Check if it landed.
            result = await self.engine.btc_monitor.check_htlc_funded(
                swap.btc_htlc_address,
                swap.btc_amount_sat,
                min_confirmations=0,  # accept mempool first
            )
            if result:
                swap.btc_funding_txid = result['txid']
                swap.btc_funding_vout = result['vout']
                swap.state = SwapState.BTC_LOCKED.value
                self.engine.db.save(swap)
                _logger.info(
                    f"Taker {swap.swap_id[:8]}: BTC sent to HTLC, "
                    f"txid={result['txid']}")

        elif state == SwapState.BTC_LOCKED.value:
            # Wait for the maker to claim BTC (which reveals preimage).
            print(f"[SwapWorker] Taker {swap.swap_id[:8]}: checking for preimage...")
            _logger.info(
                f"Taker {swap.swap_id[:8]}: checking for preimage at "
                f"{swap.btc_htlc_address}")
            preimage = await self._check_for_preimage(swap)
            if preimage:
                swap.preimage = preimage.hex()
                self.engine.db.save(swap)
                # Now claim the MARS
                await self._claim_mars_now(swap, preimage)

    async def _check_for_acceptance(self, swap: SwapData) -> Optional[dict]:
        """Poll ALL connected ElectrumX servers for offer acceptance.

        The taker may have sent their acceptance to a different server
        than the one the maker is primarily connected to. Acceptances
        are stored in-memory per server and don't gossip, so we must
        check every server we're connected to.
        """
        import asyncio as _asyncio
        network = self.engine.network
        if network is None:
            return None
        try:
            with network.interfaces_lock:
                interfaces = list(network.interfaces.values())
            if not interfaces:
                return None

            async def query_one(iface):
                try:
                    result = await iface.session.send_request(
                        'atomicswap.get_acceptance',
                        [swap.swap_id], timeout=15)
                    if result and isinstance(result, dict):
                        return result
                except Exception:
                    pass
                return None

            results = await _asyncio.gather(
                *[query_one(iface) for iface in interfaces],
                return_exceptions=True)
            for r in results:
                if isinstance(r, dict):
                    return r
        except Exception as e:
            _logger.debug(f"get_acceptance: {e}")
        return None

    async def _check_for_preimage(self, swap: SwapData) -> Optional[bytes]:
        """Non-blocking check: has the maker claimed BTC yet?"""
        from electrum_mars.atomic_swap_htlc import extract_preimage_from_witness

        try:
            txs = await self.engine.btc_monitor.get_address_txs(
                swap.btc_htlc_address)
            print(f"[SwapWorker] check_for_preimage: got {len(txs)} txs")
            _logger.info(
                f"check_for_preimage: got {len(txs)} txs for "
                f"{swap.btc_htlc_address[:20]}...")
            for tx_info in txs:
                # Look for spending transactions (tx spends FROM htlc addr)
                for vin in tx_info.get('vin', []):
                    prevout = vin.get('prevout', {})
                    if prevout.get('scriptpubkey_address') == swap.btc_htlc_address:
                        _logger.info(
                            f"check_for_preimage: found spending tx "
                            f"{tx_info['txid'][:16]}...")
                        # This tx spends the HTLC — get full tx to extract
                        tx_hex = await self.engine.btc_monitor.get_tx_hex(
                            tx_info['txid'])
                        if tx_hex:
                            preimage = extract_preimage_from_witness(tx_hex)
                            if preimage:
                                print(f"[SwapWorker] PREIMAGE FOUND: {preimage.hex()[:16]}...")
                                _logger.info(
                                    f"check_for_preimage: PREIMAGE FOUND "
                                    f"{preimage.hex()[:16]}...")
                                return preimage
                            else:
                                _logger.warning(
                                    f"check_for_preimage: spending tx found "
                                    f"but no preimage extracted")
        except Exception as e:
            _logger.warning(f"check_for_preimage error: {e}")
        return None

    async def _claim_mars_now(self, swap: SwapData, preimage: bytes):
        """Claim the MARS HTLC using the revealed preimage."""
        from electrum_mars.atomic_swap_htlc import (
            create_claim_tx, create_htlc_script,
            htlc_to_p2wsh_address, Chain)
        from electrum_mars.util import bfh

        # The offer was published BEFORE the maker knew the taker's pubkey,
        # so mars_htlc_script and mars_htlc_address were empty in the offer.
        # The taker stored these empty values. We must recompute both from
        # the known parameters: payment_hash160, both pubkeys, mars_locktime.
        if not swap.mars_htlc_script:
            # In the MARS HTLC:
            #   recipient = taker (my_pubkey) — they claim MARS
            #   sender = maker (peer_pubkey) — they can refund
            mars_script = create_htlc_script(
                payment_hash160=bfh(swap.payment_hash160),
                recipient_pubkey=bfh(swap.my_pubkey),
                sender_pubkey=bfh(swap.peer_pubkey),
                locktime=swap.mars_locktime,
            )
            swap.mars_htlc_script = mars_script.hex()
            swap.mars_htlc_address = htlc_to_p2wsh_address(
                mars_script, Chain.MARS)
            self.engine.db.save(swap)
            print(f"[SwapWorker] Recomputed MARS HTLC script + addr: "
                  f"{swap.mars_htlc_address[:25]}...")

        if not swap.mars_htlc_address and swap.mars_htlc_script:
            mars_addr = htlc_to_p2wsh_address(
                bfh(swap.mars_htlc_script), Chain.MARS)
            swap.mars_htlc_address = mars_addr
            self.engine.db.save(swap)
            print(f"[SwapWorker] Computed missing MARS HTLC addr: "
                  f"{mars_addr[:25]}...")

        # We need the MARS HTLC funding info. In the taker flow, we got
        # mars_htlc_address from the offer. We need to find the funding
        # txid by querying the MARS chain.
        mars_funding = await self._find_mars_htlc_funding(swap)
        if not mars_funding:
            _logger.warning(
                f"Taker {swap.swap_id[:8]}: can't find MARS HTLC funding tx yet")
            return

        swap.mars_funding_txid = mars_funding['txid']
        swap.mars_funding_vout = mars_funding['vout']

        receive_address = self.engine.wallet.get_receiving_address()
        claim_tx = create_claim_tx(
            funding_txid=swap.mars_funding_txid,
            funding_vout=swap.mars_funding_vout,
            funding_amount_sat=swap.mars_amount_sat,
            witness_script=bfh(swap.mars_htlc_script),
            preimage=preimage,
            claim_privkey=bfh(swap.my_privkey),
            destination_address=receive_address,
        )

        try:
            await self.engine.network.broadcast_transaction(claim_tx)
            swap.mars_claim_txid = claim_tx.txid()
            swap.state = SwapState.COMPLETED.value
            self.engine.db.save(swap)
            _logger.info(
                f"Taker {swap.swap_id[:8]}: MARS claimed! "
                f"txid={claim_tx.txid()} — SWAP COMPLETE")
        except Exception as e:
            _logger.error(f"MARS claim broadcast failed: {e}")

    async def _find_mars_htlc_funding(self, swap: SwapData) -> Optional[dict]:
        """Query the Marscoin chain for the MARS HTLC funding transaction.

        Returns dict with txid and vout, or None if not found.
        Tries multiple methods because ElectrumX may not have indexed
        P2WSH (bech32) addresses properly.
        """
        from electrum_mars import bitcoin
        network = self.engine.network
        if network is None or network.interface is None:
            return None

        sh = bitcoin.address_to_scripthash(swap.mars_htlc_address)
        print(f"[SwapWorker] _find_mars_htlc_funding: "
              f"addr={swap.mars_htlc_address[:20]}... sh={sh[:16]}...")

        # Method 1: listunspent
        try:
            utxos = await network.interface.session.send_request(
                'blockchain.scripthash.listunspent', [sh])
            print(f"[SwapWorker] listunspent returned: {utxos}")
            for utxo in utxos or []:
                if utxo.get('value', 0) >= swap.mars_amount_sat:
                    return {
                        'txid': utxo['tx_hash'],
                        'vout': utxo['tx_pos'],
                    }
        except Exception as e:
            print(f"[SwapWorker] listunspent error: {e}")

        # Method 2: try scripthash.repair — bypasses ElectrumX index,
        # queries marscoind's scantxoutset directly. Returns tx data
        # even when the index is missing the P2WSH output.
        try:
            repair_result = await network.interface.session.send_request(
                'blockchain.scripthash.repair',
                [sh, swap.mars_htlc_address])
            print(f"[SwapWorker] repair returned: {repair_result}")
            if repair_result and isinstance(repair_result, list):
                for item in repair_result:
                    txid = item.get('tx_hash')
                    tx_hex = item.get('tx_hex')
                    if txid and tx_hex:
                        # Parse the raw tx to find which vout pays our HTLC
                        from electrum_mars.transaction import Transaction
                        tx = Transaction(tx_hex)
                        for i, o in enumerate(tx.outputs()):
                            if o.value >= swap.mars_amount_sat:
                                print(f"[SwapWorker] repair: found funding "
                                      f"txid={txid[:16]}... vout={i} "
                                      f"value={o.value}")
                                return {
                                    'txid': txid,
                                    'vout': i,
                                }
        except Exception as e:
            print(f"[SwapWorker] repair error: {e}")
            import traceback; traceback.print_exc()

        print(f"[SwapWorker] _find_mars_htlc_funding: ALL METHODS FAILED")
        return None

    def _get_password(self) -> Optional[str]:
        """Get the wallet password if needed for signing."""
        if self.password_getter:
            try:
                return self.password_getter()
            except Exception:
                pass
        return None
