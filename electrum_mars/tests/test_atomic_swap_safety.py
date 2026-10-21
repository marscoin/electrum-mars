import asyncio
from types import SimpleNamespace

from electrum_mars.atomic_swap_htlc import (
    BTC_TIMELOCK_BLOCKS, MARS_TIMELOCK_BLOCKS, Chain, create_htlc_script,
    generate_keypair, generate_preimage, htlc_to_p2wsh_address)
from electrum_mars.crypto import sha256, sha256d
from electrum_mars.plugins.atomic_swap import safety
from electrum_mars.plugins.atomic_swap.swap_engine import SwapEngine, SwapState
from electrum_mars.plugins.atomic_swap.swap_worker import SwapWorker

from . import ElectrumTestCase

BTC_H = 900_000
MARS_H = 3_000_000
MARS_SAT = 100 * 10**8
BTC_SAT = 3_652


# ---------------------------------------------------------------- refund clocks

class TestRefundClocks(ElectrumTestCase):

    def test_fresh_offer_is_safe(self):
        self.assertIsNone(safety.refund_order_problem(
            MARS_H + MARS_TIMELOCK_BLOCKS, MARS_H, BTC_H + BTC_TIMELOCK_BLOCKS, BTC_H))

    def test_offer_aged_an_hour_is_unsafe(self):
        # ~29 Marscoin blocks per hour eat into the margin.
        problem = safety.refund_order_problem(
            MARS_H + MARS_TIMELOCK_BLOCKS - 30, MARS_H, BTC_H + BTC_TIMELOCK_BLOCKS, BTC_H)
        self.assertIn('reclaim the MARS too soon', problem)

    def test_mars_refund_before_btc_refund_is_unsafe(self):
        self.assertIsNotNone(safety.refund_order_problem(
            MARS_H + 10, MARS_H, BTC_H + BTC_TIMELOCK_BLOCKS, BTC_H))

    def test_btc_refund_in_the_past_is_unsafe(self):
        # A taker on an old version that guessed the Bitcoin height.
        self.assertIsNotNone(safety.btc_window_problem(850_000 + 24, BTC_H))

    def test_missing_heights(self):
        self.assertIn('missing', safety.refund_order_problem(MARS_H + 234, 0, BTC_H + 24, BTC_H))
        self.assertIn('missing', safety.btc_window_problem(BTC_H + 24, None))

    def test_btc_window(self):
        self.assertIsNone(safety.btc_window_problem(BTC_H + 24, BTC_H))
        self.assertIsNone(safety.btc_window_problem(BTC_H + 12, BTC_H))   # exactly 2 h
        self.assertIn('left before', safety.btc_window_problem(BTC_H + 11, BTC_H))

    def test_maker_claim_margin(self):
        self.assertIsNone(safety.maker_claim_problem(BTC_H + 3, BTC_H))
        self.assertIsNotNone(safety.maker_claim_problem(BTC_H + 2, BTC_H))
        self.assertIn('only 0', safety.maker_claim_problem(BTC_H - 5, BTC_H))
        self.assertIsNotNone(safety.maker_claim_problem(BTC_H + 24, None))

    def test_confirmations(self):
        self.assertEqual(safety.confirmations(0, 100), 0)
        self.assertEqual(safety.confirmations(-1, 100), 0)
        self.assertEqual(safety.confirmations(100, 100), 1)
        self.assertEqual(safety.confirmations(99, 100), 2)


# ---------------------------------------------------------------- lockbox

class TestExpectedLockbox(ElectrumTestCase):

    def setUp(self):
        super().setUp()
        _, self.hash160 = generate_preimage()
        _, self.taker = generate_keypair()
        _, self.maker = generate_keypair()

    def test_matches_what_the_maker_builds(self):
        # Same construction as SwapEngine.set_peer_info on the maker side.
        script = create_htlc_script(self.hash160, recipient_pubkey=self.taker,
                                    sender_pubkey=self.maker, locktime=MARS_H + 234)
        got_script, got_addr = safety.expected_mars_htlc(
            self.hash160.hex(), self.taker.hex(), self.maker.hex(), MARS_H + 234)
        self.assertEqual(got_script, script)
        self.assertEqual(got_addr, htlc_to_p2wsh_address(script, Chain.MARS))

    def test_roles_or_locktime_change_the_lockbox(self):
        _, addr = safety.expected_mars_htlc(
            self.hash160.hex(), self.taker.hex(), self.maker.hex(), MARS_H + 234)
        _, swapped = safety.expected_mars_htlc(
            self.hash160.hex(), self.maker.hex(), self.taker.hex(), MARS_H + 234)
        _, shorter = safety.expected_mars_htlc(
            self.hash160.hex(), self.taker.hex(), self.maker.hex(), MARS_H + 20)
        self.assertNotEqual(addr, swapped)
        self.assertNotEqual(addr, shorter)


class TestPickFunding(ElectrumTestCase):
    ADDR = 'expected'

    def out(self, address=ADDR, value=MARS_SAT, height=MARS_H - 1, txid='aa'):
        return {'txid': txid, 'vout': 0, 'address': address, 'value': value, 'height': height}

    def test_nothing_yet(self):
        funding, status = safety.pick_mars_funding([], self.ADDR, MARS_SAT, MARS_H)
        self.assertIsNone(funding)
        self.assertIn('Waiting for the seller', status)

    def test_ignores_wrong_address_and_short_amount(self):
        funding, status = safety.pick_mars_funding(
            [self.out(address='other'), self.out(value=MARS_SAT - 1)],
            self.ADDR, MARS_SAT, MARS_H)
        self.assertIsNone(funding)
        self.assertIn('Waiting for the seller', status)

    def test_waits_for_confirmations(self):
        for height, seen in ((0, 0), (MARS_H, 1)):
            funding, status = safety.pick_mars_funding(
                [self.out(height=height)], self.ADDR, MARS_SAT, MARS_H)
            self.assertIsNone(funding)
            self.assertIn(f'({seen}/2)', status)

    def test_picks_the_matching_confirmed_output(self):
        funding, status = safety.pick_mars_funding(
            [self.out(address='other', txid='bad'), self.out(height=0, txid='new'),
             self.out(txid='good')],
            self.ADDR, MARS_SAT, MARS_H)
        self.assertEqual(status, 'verified')
        self.assertEqual((funding['txid'], funding['confirmations']), ('good', 2))


# ---------------------------------------------------------------- taker verification

def _raw_tx_paying(witness_script: bytes, value: int) -> str:
    """A minimal legacy-serialized transaction with one P2WSH output."""
    spk = bytes([0x00, 0x20]) + sha256(witness_script)
    return (
        (2).to_bytes(4, 'little')
        + b'\x01' + b'\x11' * 32 + b'\x00\x00\x00\x00' + b'\x00' + b'\xff' * 4
        + b'\x01' + value.to_bytes(8, 'little') + bytes([len(spk)]) + spk
        + b'\x00' * 4
    ).hex()


def _txid(raw_hex: str) -> str:
    return sha256d(bytes.fromhex(raw_hex))[::-1].hex()


class FakeSession:
    def __init__(self, unspent):
        self.unspent = unspent

    async def send_request(self, method, params):
        if method == 'blockchain.scripthash.listunspent':
            return self.unspent
        if method == 'blockchain.scripthash.repair':
            return []
        raise Exception(f'unexpected {method}')


class FakeNetwork:
    def __init__(self, height, unspent=(), txs=None, merkle_roots=None):
        self.height = height
        self.interface = SimpleNamespace(session=FakeSession(list(unspent)))
        self.txs = txs or {}
        self.merkle_roots = merkle_roots or {}   # height -> merkle_root in our headers

    def get_local_height(self):
        return self.height

    async def get_transaction(self, txid):
        return self.txs[txid]

    async def get_merkle_for_transaction(self, txid, height):
        return {'merkle': [], 'pos': 0, 'block_height': height}   # single-tx block

    def blockchain(self):
        roots = self.merkle_roots
        return SimpleNamespace(read_header=lambda h: (
            {'merkle_root': roots[h]} if h in roots else None))


class FakeBtc:
    def __init__(self, height=BTC_H, paid=None):
        self.height = height
        self.paid = paid

    async def get_block_height(self):
        return self.height

    async def check_htlc_funded(self, address, amount, min_confirmations=1):
        return self.paid


class TestTakerVerification(ElectrumTestCase):

    def setUp(self):
        super().setUp()
        _, self.hash160 = generate_preimage()
        _, self.maker = generate_keypair()
        self.engine = SwapEngine(None, FakeNetwork(MARS_H), self.electrum_path)
        self.engine.btc_monitor = FakeBtc()

    def run_coro(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.asyncio_loop).result(10)

    def new_swap(self, mars_locktime=MARS_H + MARS_TIMELOCK_BLOCKS, offer_address='',
                 btc_height=BTC_H):
        return self.engine.create_taker_swap(
            mars_amount_sat=MARS_SAT, btc_amount_sat=BTC_SAT,
            payment_hash160=self.hash160.hex(), peer_pubkey=self.maker.hex(),
            mars_htlc_address=offer_address, mars_htlc_script='',
            mars_locktime=mars_locktime, current_btc_height=btc_height)

    def lock_mars(self, swap, value=MARS_SAT, height=MARS_H - 1, in_our_headers=True,
                  pay_script=None):
        """The maker funds a lockbox; returns its txid."""
        script = pay_script or safety.expected_mars_htlc(
            swap.payment_hash160, swap.my_pubkey, swap.peer_pubkey, swap.mars_locktime)[0]
        _, address = safety.expected_mars_htlc(
            swap.payment_hash160, swap.my_pubkey, swap.peer_pubkey, swap.mars_locktime)
        raw = _raw_tx_paying(script, value)
        txid = _txid(raw)
        roots = {height: txid} if in_our_headers else {}
        self.engine.network = FakeNetwork(
            MARS_H, unspent=[{'tx_hash': txid, 'tx_pos': 0, 'value': value, 'height': height}],
            txs={txid: raw}, merkle_roots=roots)
        return txid, address

    def verify(self, swap):
        return self.run_coro(self.engine.verify_taker_mars_lock(swap.swap_id))

    def test_waits_until_the_lock_exists(self):
        swap = self.verify(self.new_swap())
        self.assertFalse(swap.mars_verified)
        self.assertEqual(swap.state, SwapState.CREATED.value)
        self.assertIn('Waiting for the seller', swap.verify_status)
        self.assertTrue(swap.mars_htlc_address)   # filled in from our own values

    def test_verifies_a_confirmed_lock(self):
        swap = self.new_swap()
        txid, address = self.lock_mars(swap)
        swap = self.verify(swap)
        self.assertTrue(swap.mars_verified)
        self.assertEqual((swap.mars_funding_txid, swap.mars_funding_vout), (txid, 0))
        self.assertEqual(swap.mars_htlc_address, address)
        self.assertTrue(self.engine.get_swap(swap.swap_id).mars_verified)

    def test_waits_for_two_confirmations(self):
        swap = self.new_swap()
        self.lock_mars(swap, height=MARS_H)
        swap = self.verify(swap)
        self.assertFalse(swap.mars_verified)
        self.assertIn('(1/2)', swap.verify_status)

    def test_server_cannot_invent_a_lock(self):
        # The server reports an output, but our headers don't contain it.
        swap = self.new_swap()
        self.lock_mars(swap, in_our_headers=False)
        swap = self.verify(swap)
        self.assertFalse(swap.mars_verified)
        self.assertEqual(swap.state, SwapState.CREATED.value)

    def test_server_cannot_lie_about_the_amount(self):
        swap = self.new_swap()
        txid, _ = self.lock_mars(swap, value=MARS_SAT - 1)
        # The server claims the full amount; the transaction itself says otherwise.
        self.engine.network.interface.session.unspent[0]['value'] = MARS_SAT
        swap = self.verify(swap)
        self.assertFalse(swap.mars_verified)

    def test_lock_with_other_terms_does_not_count(self):
        swap = self.new_swap()
        _, other_key = generate_keypair()
        wrong = create_htlc_script(self.hash160, recipient_pubkey=other_key,
                                   sender_pubkey=self.maker, locktime=swap.mars_locktime)
        self.lock_mars(swap, pay_script=wrong)
        swap = self.verify(swap)
        self.assertFalse(swap.mars_verified)

    def test_mismatched_offer_address_fails(self):
        swap = self.verify(self.new_swap(offer_address='mars1qsomethingelse'))
        self.assertEqual(swap.state, SwapState.FAILED.value)
        self.assertTrue(swap.error_msg.startswith('Do not send BTC'))

    def test_unsafe_refund_order_fails(self):
        swap = self.new_swap(mars_locktime=MARS_H + 60)
        self.lock_mars(swap)
        swap = self.verify(swap)
        self.assertEqual(swap.state, SwapState.FAILED.value)
        self.assertFalse(swap.mars_verified)
        self.assertIn('reclaim the MARS too soon', swap.error_msg)

    def test_btc_refund_in_the_past_fails(self):
        swap = self.verify(self.new_swap(btc_height=850_000))
        self.assertEqual(swap.state, SwapState.FAILED.value)

    def test_never_fails_a_swap_that_was_already_paid(self):
        # Swaps started on 4.4.1 may have paid before this check existed.
        self.engine.btc_monitor = FakeBtc(paid={'txid': 'bb' * 32, 'vout': 0})
        swap = self.verify(self.new_swap(mars_locktime=MARS_H + 60))
        self.assertEqual(swap.state, SwapState.CREATED.value)
        self.assertTrue(swap.error_msg.startswith('Do not send BTC'))

    def test_waits_when_heights_are_unknown(self):
        self.engine.btc_monitor = FakeBtc(height=None)
        swap = self.verify(self.new_swap(mars_locktime=MARS_H + 60))
        self.assertEqual(swap.state, SwapState.CREATED.value)
        self.assertIsNone(swap.error_msg)


# ---------------------------------------------------------------- maker acceptance check

class TestMakerAcceptance(ElectrumTestCase):

    def check(self, btc_locktime, mars_locktime=MARS_H + MARS_TIMELOCK_BLOCKS):
        swap = SimpleNamespace(mars_locktime=mars_locktime)
        return SwapWorker._acceptance_problem(
            swap, {'btc_locktime': btc_locktime}, BTC_H, MARS_H)

    def test_normal_acceptance(self):
        self.assertIsNone(self.check(BTC_H + BTC_TIMELOCK_BLOCKS))

    def test_refund_too_soon(self):
        self.assertIsNotNone(self.check(BTC_H + 5))

    def test_refund_too_late_for_our_mars_refund(self):
        self.assertIsNotNone(self.check(BTC_H + 200))

    def test_garbage(self):
        for bad in (None, '900024', 0, -1, True, 900024.0):
            self.assertIsNotNone(self.check(bad), bad)
