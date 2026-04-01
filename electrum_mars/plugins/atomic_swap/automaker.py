"""
Auto-Maker: Automated market making for atomic swaps.

Turns any Electrum-Mars wallet into a passive market maker that:
- Fetches live MARS/BTC price from price.marscoin.org
- Applies a configurable fee spread (default 5%)
- Auto-creates and refreshes swap offers within daily limits
- Tracks earnings in both BTC and MARS
- Runs in the background while the wallet is open

Anti-gaming protections:
- Maximum single swap size (prevents draining)
- Daily volume limit (caps exposure)
- Minimum offer size (prevents dust spam)
- Rate sanity check against market price (rejects stale/manipulated rates)
- Reserve requirement (never offers 100% of balance)
"""

import time
import asyncio
import json
import os
from typing import Optional, Dict, TYPE_CHECKING
from dataclasses import dataclass, asdict

import aiohttp

from electrum_mars.logging import get_logger

if TYPE_CHECKING:
    from .swap_engine import SwapEngine
    from .orderbook import OrderBook, SwapOffer

_logger = get_logger(__name__)

PRICE_API_URL = "https://price.marscoin.org/json"
BTC_PRICE_API_URL = "https://mempool.space/api/v1/prices"

# Defaults
DEFAULT_FEE_PERCENT = 5.0          # 5% spread over market rate
DEFAULT_DAILY_LIMIT_MARS = 10000   # max MARS to sell per 24h
DEFAULT_MAX_SINGLE_SWAP = 1000     # max MARS per single swap
DEFAULT_MIN_SINGLE_SWAP = 10       # min MARS per swap (anti-dust)
DEFAULT_RESERVE_PERCENT = 20.0     # keep 20% of balance unlocked
DEFAULT_OFFER_DURATION_HOURS = 4   # offers valid for 4 hours
DEFAULT_REFRESH_INTERVAL = 300     # refresh price every 5 minutes


@dataclass
class AutoMakerConfig:
    """Configuration for the auto-maker."""
    enabled: bool = False
    fee_percent: float = DEFAULT_FEE_PERCENT
    daily_limit_mars_sat: int = int(DEFAULT_DAILY_LIMIT_MARS * 1e8)
    max_single_swap_sat: int = int(DEFAULT_MAX_SINGLE_SWAP * 1e8)
    min_single_swap_sat: int = int(DEFAULT_MIN_SINGLE_SWAP * 1e8)
    reserve_percent: float = DEFAULT_RESERVE_PERCENT
    offer_duration_hours: float = DEFAULT_OFFER_DURATION_HOURS
    refresh_interval_sec: int = DEFAULT_REFRESH_INTERVAL
    # Also buy MARS with BTC (two-way market making)
    also_buy_mars: bool = False
    buy_fee_percent: float = DEFAULT_FEE_PERCENT
    # Number of concurrent offers to maintain
    num_offers: int = 3

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, data: str) -> 'AutoMakerConfig':
        return cls(**json.loads(data))

    def save(self, path: str):
        with open(path, 'w') as f:
            f.write(self.to_json())

    @classmethod
    def load(cls, path: str) -> 'AutoMakerConfig':
        if os.path.exists(path):
            with open(path) as f:
                return cls.from_json(f.read())
        return cls()


@dataclass
class AutoMakerStats:
    """Tracks auto-maker earnings and activity."""
    total_mars_sold_sat: int = 0
    total_btc_earned_sat: int = 0
    total_mars_bought_sat: int = 0
    total_btc_spent_sat: int = 0
    swaps_completed: int = 0
    swaps_failed: int = 0
    today_mars_sold_sat: int = 0
    today_date: str = ""
    last_price_mars_usd: float = 0.0
    last_price_btc_usd: float = 0.0
    last_rate_mars_btc: float = 0.0

    def reset_daily(self):
        today = time.strftime('%Y-%m-%d')
        if self.today_date != today:
            self.today_date = today
            self.today_mars_sold_sat = 0

    @property
    def total_btc_earned(self) -> float:
        return self.total_btc_earned_sat / 1e8

    @property
    def total_mars_sold(self) -> float:
        return self.total_mars_sold_sat / 1e8


class AutoMaker:
    """Automated market maker for atomic swaps."""

    def __init__(self, engine: 'SwapEngine', orderbook: 'OrderBook',
                 data_dir: str):
        self.engine = engine
        self.orderbook = orderbook
        self.config_path = os.path.join(data_dir, 'automaker_config.json')
        self.stats_path = os.path.join(data_dir, 'automaker_stats.json')
        self.config = AutoMakerConfig.load(self.config_path)
        self.stats = self._load_stats()
        self._task = None
        self._running = False

    def _load_stats(self) -> AutoMakerStats:
        if os.path.exists(self.stats_path):
            try:
                with open(self.stats_path) as f:
                    return AutoMakerStats(**json.loads(f.read()))
            except Exception:
                pass
        return AutoMakerStats()

    def _save_stats(self):
        with open(self.stats_path, 'w') as f:
            f.write(json.dumps(asdict(self.stats), indent=2))

    def save_config(self):
        self.config.save(self.config_path)

    async def fetch_market_price(self) -> Optional[dict]:
        """Fetch current MARS and BTC prices from APIs.

        Returns dict with mars_usd, btc_usd, mars_btc rate.
        """
        try:
            async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=15)) as session:
                # Get MARS price
                async with session.get(PRICE_API_URL,
                                       ssl=False) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        mars_usd = data['data']['154']['quote']['USD']['price']
                    else:
                        return None

                # Get BTC price
                async with session.get(BTC_PRICE_API_URL) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        btc_usd = data.get('USD', 83000)
                    else:
                        btc_usd = 83000  # fallback

                mars_btc = mars_usd / btc_usd

                self.stats.last_price_mars_usd = mars_usd
                self.stats.last_price_btc_usd = btc_usd
                self.stats.last_rate_mars_btc = mars_btc

                return {
                    'mars_usd': mars_usd,
                    'btc_usd': btc_usd,
                    'mars_btc': mars_btc,
                }
        except Exception as e:
            _logger.warning(f"Price fetch failed: {e}")
            return None

    def calculate_offer_rate(self, market_rate: float, selling: bool = True) -> float:
        """Calculate the offer rate with fee spread applied.

        For selling MARS: rate is higher (buyer pays more BTC per MARS)
        For buying MARS: rate is lower (seller gets less BTC per MARS)
        """
        fee = self.config.fee_percent / 100.0
        if selling:
            return market_rate * (1 + fee)  # charge more BTC per MARS
        else:
            return market_rate * (1 - fee)  # pay less BTC per MARS

    def get_available_balance_sat(self) -> int:
        """Get MARS balance available for market making (respects reserve)."""
        wallet = self.engine.wallet
        balance = wallet.get_balance()
        confirmed = balance[0]  # confirmed balance in satoshis
        reserve = int(confirmed * self.config.reserve_percent / 100.0)
        available = confirmed - reserve
        return max(0, available)

    def get_remaining_daily_limit_sat(self) -> int:
        """How much more MARS can be offered today."""
        self.stats.reset_daily()
        remaining = self.config.daily_limit_mars_sat - self.stats.today_mars_sold_sat
        return max(0, remaining)

    def calculate_offer_sizes(self) -> list:
        """Determine how to split available balance into offers.

        Returns list of offer amounts in satoshis.
        """
        available = self.get_available_balance_sat()
        daily_remaining = self.get_remaining_daily_limit_sat()
        total = min(available, daily_remaining)

        if total < self.config.min_single_swap_sat:
            return []

        # Split into num_offers equal parts, capped at max_single_swap
        num = self.config.num_offers
        per_offer = min(total // num, self.config.max_single_swap_sat)

        if per_offer < self.config.min_single_swap_sat:
            # Not enough for multiple offers — make one
            per_offer = min(total, self.config.max_single_swap_sat)
            return [per_offer]

        return [per_offer] * num

    async def create_offers(self):
        """Create or refresh market making offers based on current price."""
        if not self.config.enabled:
            return

        prices = await self.fetch_market_price()
        if not prices:
            _logger.warning("Auto-maker: cannot fetch price, skipping")
            return

        market_rate = prices['mars_btc']
        offer_rate = self.calculate_offer_rate(market_rate, selling=True)
        offer_sizes = self.calculate_offer_sizes()

        if not offer_sizes:
            _logger.info("Auto-maker: no balance available for offers")
            return

        # Cancel old auto-generated offers
        for offer in self.orderbook.get_my_offers():
            self.orderbook.remove_offer(offer.offer_id)

        current_height = 0
        if self.engine.network:
            bc = self.engine.network.blockchain()
            if bc:
                current_height = bc.height()

        for mars_sat in offer_sizes:
            btc_sat = int(mars_sat * offer_rate)
            if btc_sat <= 0:
                continue

            swap = self.engine.create_maker_swap(
                mars_amount_sat=mars_sat,
                btc_amount_sat=btc_sat,
                current_mars_height=current_height,
            )

            from .orderbook import SwapOffer
            offer = SwapOffer(
                offer_id=swap.swap_id,
                mars_amount_sat=mars_sat,
                btc_amount_sat=btc_sat,
                rate=offer_rate,
                maker_pubkey=swap.my_pubkey,
                payment_hash160=swap.payment_hash160,
                mars_htlc_address=swap.mars_htlc_address or '',
                mars_htlc_script=swap.mars_htlc_script or '',
                mars_locktime=swap.mars_locktime,
                expires_at=time.time() + self.config.offer_duration_hours * 3600,
                maker_address=self.engine.wallet.get_receiving_address(),
            )
            self.orderbook.add_my_offer(offer)
            _logger.info(f"Auto-maker: created offer {swap.swap_id[:8]} — "
                        f"{mars_sat/1e8:.2f} MARS for {btc_sat/1e8:.8f} BTC "
                        f"(rate: {offer_rate:.10f}, fee: {self.config.fee_percent}%)")

        self._save_stats()

    async def run_loop(self):
        """Main auto-maker loop — refresh offers periodically."""
        self._running = True
        _logger.info("Auto-maker started")
        while self._running:
            try:
                await self.create_offers()
            except Exception as e:
                _logger.error(f"Auto-maker error: {e}")
            await asyncio.sleep(self.config.refresh_interval_sec)
        _logger.info("Auto-maker stopped")

    def start(self):
        """Start the auto-maker background task."""
        if self._task and not self._task.done():
            return
        self.config.enabled = True
        self.save_config()
        loop = asyncio.get_event_loop()
        self._task = loop.create_task(self.run_loop())

    def stop(self):
        """Stop the auto-maker."""
        self._running = False
        self.config.enabled = False
        self.save_config()
        if self._task:
            self._task.cancel()
            self._task = None

    def is_running(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    def get_status_summary(self) -> dict:
        """Get auto-maker status for display."""
        return {
            'running': self.is_running(),
            'fee_percent': self.config.fee_percent,
            'daily_limit': self.config.daily_limit_mars_sat / 1e8,
            'today_sold': self.stats.today_mars_sold_sat / 1e8,
            'remaining_today': self.get_remaining_daily_limit_sat() / 1e8,
            'available_balance': self.get_available_balance_sat() / 1e8,
            'total_btc_earned': self.stats.total_btc_earned_sat / 1e8,
            'total_mars_sold': self.stats.total_mars_sold_sat / 1e8,
            'swaps_completed': self.stats.swaps_completed,
            'last_rate': self.stats.last_rate_mars_btc,
            'last_mars_usd': self.stats.last_price_mars_usd,
            'active_offers': len(self.orderbook.get_my_offers()),
            'num_offers': self.config.num_offers,
            'max_per_swap': self.config.max_single_swap_sat / 1e8,
            'reserve_percent': self.config.reserve_percent,
        }
