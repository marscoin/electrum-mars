"""
P2P order book for atomic swap offers.

Offers are relayed via ElectrumX servers (message relay only, no custody).
For MVP, offers can also be exchanged manually or via a simple JSON API.

Each offer contains:
- mars_amount: how much MARS is being sold
- btc_amount: how much BTC is wanted in return
- maker_pubkey: the maker's ephemeral pubkey for this offer
- payment_hash160: the hash for the HTLC
- mars_htlc_address: where the MARS will be locked
- expires_at: when the offer expires
- signature: signed by maker's wallet key for authenticity
"""

import time
import json
from typing import List, Optional, Dict
from dataclasses import dataclass, asdict

from electrum_mars.logging import get_logger

_logger = get_logger(__name__)


@dataclass
class SwapOffer:
    """An atomic swap offer published to the order book."""
    offer_id: str
    mars_amount_sat: int          # MARS being sold (in satoshis)
    btc_amount_sat: int           # BTC wanted (in satoshis)
    rate: float                   # BTC per MARS
    maker_pubkey: str             # hex, ephemeral pubkey
    payment_hash160: str          # hex, 20 bytes
    mars_htlc_address: str        # P2WSH address where MARS will be locked
    mars_htlc_script: str         # hex, the witness script
    mars_locktime: int            # block height for CLTV
    expires_at: float             # unix timestamp
    maker_address: str = ""       # maker's Marscoin address (for identification)
    signature: str = ""           # signature of offer data

    def is_expired(self) -> bool:
        return time.time() > self.expires_at

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, data: str) -> 'SwapOffer':
        return cls(**json.loads(data))

    @property
    def mars_amount(self) -> float:
        return self.mars_amount_sat / 1e8

    @property
    def btc_amount(self) -> float:
        return self.btc_amount_sat / 1e8


class OrderBook:
    """Manages swap offers — the P2P order book.

    In the MVP, offers are stored locally and can be:
    1. Published/fetched via ElectrumX RPC (when server support is added)
    2. Exchanged manually via JSON copy/paste
    3. Shared via a simple REST endpoint

    The order book is non-custodial — it only stores messages.
    """

    def __init__(self):
        self._offers: Dict[str, SwapOffer] = {}
        self._my_offers: Dict[str, SwapOffer] = {}

    def add_offer(self, offer: SwapOffer):
        """Add an offer to the local order book."""
        if not offer.is_expired():
            self._offers[offer.offer_id] = offer
            _logger.info(f"Added offer {offer.offer_id[:8]}: "
                        f"{offer.mars_amount:.2f} MARS for {offer.btc_amount:.6f} BTC")

    def add_my_offer(self, offer: SwapOffer):
        """Track an offer I created."""
        self._my_offers[offer.offer_id] = offer
        self.add_offer(offer)

    def remove_offer(self, offer_id: str):
        """Remove an offer from the order book."""
        self._offers.pop(offer_id, None)
        self._my_offers.pop(offer_id, None)

    def get_offers(self, min_mars: int = 0, max_rate: float = float('inf')
                   ) -> List[SwapOffer]:
        """Get available offers, sorted by best rate.

        Args:
            min_mars: minimum MARS amount in satoshis
            max_rate: maximum BTC/MARS rate

        Returns:
            List of offers sorted by rate (best first)
        """
        self._cleanup_expired()
        offers = [
            o for o in self._offers.values()
            if not o.is_expired()
            and o.mars_amount_sat >= min_mars
            and o.rate <= max_rate
            and o.offer_id not in self._my_offers  # don't show own offers
        ]
        return sorted(offers, key=lambda o: o.rate)

    def get_my_offers(self) -> List[SwapOffer]:
        """Get offers I've created."""
        self._cleanup_expired()
        return [o for o in self._my_offers.values() if not o.is_expired()]

    def get_best_offer(self, mars_amount_sat: int = 0) -> Optional[SwapOffer]:
        """Get the best available offer (lowest rate)."""
        offers = self.get_offers(min_mars=mars_amount_sat)
        return offers[0] if offers else None

    def _cleanup_expired(self):
        """Remove expired offers."""
        expired = [oid for oid, o in self._offers.items() if o.is_expired()]
        for oid in expired:
            self._offers.pop(oid, None)
            self._my_offers.pop(oid, None)

    def export_offers_json(self) -> str:
        """Export all offers as JSON (for manual sharing)."""
        self._cleanup_expired()
        return json.dumps([asdict(o) for o in self._offers.values()], indent=2)

    def import_offers_json(self, data: str):
        """Import offers from JSON (for manual sharing)."""
        offers = json.loads(data)
        for o in offers:
            offer = SwapOffer(**o)
            self.add_offer(offer)

    async def fetch_from_electrumx(self, network) -> List[SwapOffer]:
        """Fetch offers from connected ElectrumX server.

        Uses the atomicswap.get_offers RPC method (requires ElectrumX extension).
        Falls back gracefully if the server doesn't support it.
        """
        try:
            interface = network.interface
            if interface is None:
                return []
            result = await interface.session.send_request(
                'atomicswap.get_offers', [])
            if result and isinstance(result, list):
                offers = []
                for item in result:
                    offer = SwapOffer(**item)
                    self.add_offer(offer)
                    offers.append(offer)
                _logger.info(f"Fetched {len(offers)} offers from ElectrumX")
                return offers
        except Exception as e:
            # Server doesn't support atomic swap RPC — that's OK
            _logger.debug(f"ElectrumX atomicswap not available: {e}")
        return []

    async def publish_to_electrumx(self, network, offer: SwapOffer) -> bool:
        """Publish an offer to the ElectrumX server.

        Uses the atomicswap.post_offer RPC method.
        """
        try:
            interface = network.interface
            if interface is None:
                return False
            await interface.session.send_request(
                'atomicswap.post_offer', [asdict(offer)])
            _logger.info(f"Published offer {offer.offer_id[:8]} to ElectrumX")
            return True
        except Exception as e:
            _logger.debug(f"Could not publish to ElectrumX: {e}")
            return False
