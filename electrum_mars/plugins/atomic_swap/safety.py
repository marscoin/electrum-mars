"""Safety checks for atomic swaps.

The two refund clocks are what keep a swap atomic:

* The taker's BTC refund opens at Bitcoin block ``btc_locktime``.
* The maker's MARS refund opens at Marscoin block ``mars_locktime``.

The maker reveals the secret when claiming the BTC, which can happen right
up to the BTC refund time. The taker then needs time to claim the MARS
before the maker could take it back. So the MARS refund must open
comfortably after the BTC refund, even if Bitcoin blocks are slow and
Marscoin blocks are fast. It also means a taker must never pay BTC until
the maker's MARS is provably locked in the lockbox the taker expects.

Everything here is a pure function so it can be tested without a network.
"""

from typing import Iterable, Optional, Tuple

from electrum_mars.atomic_swap_htlc import Chain, create_htlc_script, htlc_to_p2wsh_address

BTC_BLOCK_SEC = 600
MARS_BLOCK_SEC = 123
BTC_SLOW = 1.25    # assume Bitcoin blocks may come up to 25% slower than target
MARS_FAST = 0.85   # and Marscoin blocks up to 15% faster

MIN_REFUND_GAP_SEC = 3600       # MARS refund opens >= 1 h after the BTC refund (pessimistic)
MIN_BTC_WINDOW_SEC = 2 * 3600   # never ask anyone to pay BTC with < 2 h left before its refund
MARS_MIN_CONFIRMATIONS = 2      # MARS lock must be this deep before the taker pays
MAKER_CLAIM_MARGIN_BLOCKS = 3   # maker claims BTC only if it can confirm before the BTC refund


def _hours(sec: float) -> str:
    return f'{sec / 3600:.1f} h'


def btc_refund_seconds(btc_locktime: int, btc_height: int) -> float:
    """Pessimistic wall-clock time until the BTC refund opens."""
    return max(0, btc_locktime - btc_height) * BTC_BLOCK_SEC * BTC_SLOW


def mars_refund_seconds(mars_locktime: int, mars_height: int) -> float:
    """Pessimistic wall-clock time until the MARS refund opens."""
    return max(0, mars_locktime - mars_height) * MARS_BLOCK_SEC * MARS_FAST


def refund_order_problem(mars_locktime: int, mars_height: int,
                         btc_locktime: int, btc_height: int) -> Optional[str]:
    """Return a reason if the refund clocks are unsafe, else None."""
    if not (mars_locktime and mars_height and btc_locktime and btc_height):
        return 'missing block heights; cannot check the refund times'
    gap = mars_refund_seconds(mars_locktime, mars_height) - btc_refund_seconds(btc_locktime, btc_height)
    if gap < MIN_REFUND_GAP_SEC:
        return ('the seller could reclaim the MARS too soon after the Bitcoin refund opens '
                f'(margin {_hours(max(gap, 0))}, need {_hours(MIN_REFUND_GAP_SEC)})')
    return None


def btc_window_problem(btc_locktime: int, btc_height: int) -> Optional[str]:
    """Return a reason if there is too little time left to pay and claim BTC."""
    if not (btc_locktime and btc_height):
        return 'missing Bitcoin block height'
    left = max(0, btc_locktime - btc_height) * BTC_BLOCK_SEC
    if left < MIN_BTC_WINDOW_SEC:
        return (f'only about {_hours(left)} left before the Bitcoin refund opens '
                f'(need {_hours(MIN_BTC_WINDOW_SEC)})')
    return None


def maker_claim_problem(btc_locktime: int, btc_height: int) -> Optional[str]:
    """Return a reason if it is too late for the maker to claim the BTC.

    Claiming reveals the secret. If the claim is still unconfirmed when the
    BTC refund opens, the taker could refund the BTC and use the revealed
    secret to take the MARS as well. Too close to the refund, the maker
    lets the swap unwind instead: the taker refunds BTC, the maker MARS.
    """
    if not (btc_locktime and btc_height):
        return 'missing Bitcoin block height'
    left = btc_locktime - btc_height
    if left < MAKER_CLAIM_MARGIN_BLOCKS:
        return (f'only {max(left, 0)} Bitcoin blocks before the BTC refund opens '
                f'(need {MAKER_CLAIM_MARGIN_BLOCKS})')
    return None


def expected_mars_htlc(payment_hash160: str, taker_pubkey: str, maker_pubkey: str,
                       mars_locktime: int) -> Tuple[bytes, str]:
    """The MARS lockbox a taker expects: the taker claims with the secret,
    the maker refunds after ``mars_locktime``. Built only from values the
    taker knows, never from anything the maker sent."""
    script = create_htlc_script(
        payment_hash160=bytes.fromhex(payment_hash160),
        recipient_pubkey=bytes.fromhex(taker_pubkey),
        sender_pubkey=bytes.fromhex(maker_pubkey),
        locktime=mars_locktime,
    )
    return script, htlc_to_p2wsh_address(script, Chain.MARS)


def confirmations(tx_height: int, tip_height: int) -> int:
    """Electrum convention: height <= 0 means unconfirmed."""
    if not tx_height or tx_height <= 0 or not tip_height:
        return 0
    return max(0, tip_height - tx_height + 1)


def pick_mars_funding(candidates: Iterable[dict], expected_address: str,
                      min_amount_sat: int, tip_height: int,
                      min_confirmations: int = MARS_MIN_CONFIRMATIONS
                      ) -> Tuple[Optional[dict], str]:
    """Choose the output that funds the expected MARS lockbox.

    ``candidates`` are dicts with ``txid``, ``vout``, ``address``, ``value``
    and ``height``. Returns ``(funding, status)``; ``funding`` is None until
    an output at the expected address holds enough MARS with enough
    confirmations, and ``status`` says what we are waiting for.
    """
    best_unconfirmed = None
    for c in candidates:
        if c.get('address') != expected_address or c.get('value', 0) < min_amount_sat:
            continue
        conf = confirmations(c.get('height', 0), tip_height)
        if conf >= min_confirmations:
            return dict(c, confirmations=conf), 'verified'
        if best_unconfirmed is None or conf > best_unconfirmed:
            best_unconfirmed = conf
    if best_unconfirmed is None:
        return None, 'Waiting for the seller to lock MARS…'
    return None, (f'Seller locked MARS; waiting for confirmations '
                  f'({best_unconfirmed}/{min_confirmations})…')
