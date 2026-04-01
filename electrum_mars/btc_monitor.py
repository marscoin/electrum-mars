"""
Bitcoin chain monitoring for atomic swaps via mempool.space REST API.

No Bitcoin node required — uses the public mempool.space API to:
- Check if a Bitcoin HTLC address has been funded
- Get transaction details (confirmations, amounts)
- Extract preimage from claim transactions (witness data)
"""

import asyncio
import json
from typing import Optional, List, Dict
from enum import Enum

import aiohttp

from .logging import get_logger

_logger = get_logger(__name__)

# API endpoints
MEMPOOL_MAINNET = "https://mempool.space/api"
MEMPOOL_TESTNET = "https://mempool.space/testnet/api"


class BtcMonitor:
    """Monitor Bitcoin addresses and transactions via mempool.space API."""

    def __init__(self, testnet: bool = False):
        self.base_url = MEMPOOL_TESTNET if testnet else MEMPOOL_MAINNET
        self._session = None

    async def _get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30))
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get(self, path: str) -> Optional[dict]:
        """Make GET request to mempool.space API."""
        session = await self._get_session()
        url = f"{self.base_url}{path}"
        try:
            async with session.get(url) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    _logger.warning(f"BTC API {resp.status}: {url}")
                    return None
        except Exception as e:
            _logger.warning(f"BTC API error: {e}")
            return None

    async def _get_text(self, path: str) -> Optional[str]:
        """Make GET request returning text."""
        session = await self._get_session()
        url = f"{self.base_url}{path}"
        try:
            async with session.get(url) as resp:
                if resp.status == 200:
                    return await resp.text()
                return None
        except Exception as e:
            _logger.warning(f"BTC API error: {e}")
            return None

    async def get_address_utxos(self, address: str) -> List[dict]:
        """Get UTXOs for a Bitcoin address.

        Returns list of:
            {'txid': str, 'vout': int, 'value': int, 'status': {'confirmed': bool, ...}}
        """
        result = await self._get(f"/address/{address}/utxo")
        return result if result else []

    async def get_address_txs(self, address: str) -> List[dict]:
        """Get transactions for a Bitcoin address."""
        result = await self._get(f"/address/{address}/txs")
        return result if result else []

    async def get_tx(self, txid: str) -> Optional[dict]:
        """Get full transaction details."""
        return await self._get(f"/tx/{txid}")

    async def get_tx_hex(self, txid: str) -> Optional[str]:
        """Get raw transaction hex."""
        return await self._get_text(f"/tx/{txid}/hex")

    async def get_block_height(self) -> Optional[int]:
        """Get current Bitcoin block height."""
        text = await self._get_text("/blocks/tip/height")
        return int(text) if text else None

    async def check_htlc_funded(
        self,
        htlc_address: str,
        expected_amount_sat: int,
        min_confirmations: int = 1,
    ) -> Optional[dict]:
        """Check if an HTLC address has been funded with the expected amount.

        Args:
            htlc_address: Bitcoin P2WSH address of the HTLC
            expected_amount_sat: expected funding amount in satoshis
            min_confirmations: minimum confirmations required

        Returns:
            Dict with funding info if funded, None otherwise:
            {'txid': str, 'vout': int, 'value': int, 'confirmations': int}
        """
        utxos = await self.get_address_utxos(htlc_address)
        if not utxos:
            return None

        current_height = await self.get_block_height()
        if current_height is None:
            return None

        for utxo in utxos:
            if utxo['value'] < expected_amount_sat:
                continue

            confirmed = utxo.get('status', {}).get('confirmed', False)
            if not confirmed:
                if min_confirmations == 0:
                    return {
                        'txid': utxo['txid'],
                        'vout': utxo['vout'],
                        'value': utxo['value'],
                        'confirmations': 0,
                    }
                continue

            block_height = utxo['status'].get('block_height', 0)
            confirmations = current_height - block_height + 1
            if confirmations >= min_confirmations:
                return {
                    'txid': utxo['txid'],
                    'vout': utxo['vout'],
                    'value': utxo['value'],
                    'confirmations': confirmations,
                }

        return None

    async def wait_for_htlc_funding(
        self,
        htlc_address: str,
        expected_amount_sat: int,
        min_confirmations: int = 1,
        poll_interval: float = 30.0,
        timeout: float = 7200.0,
    ) -> Optional[dict]:
        """Wait for an HTLC address to be funded.

        Polls mempool.space until funded or timeout.

        Returns:
            Funding info dict if funded, None if timeout
        """
        elapsed = 0.0
        while elapsed < timeout:
            result = await self.check_htlc_funded(
                htlc_address, expected_amount_sat, min_confirmations)
            if result:
                _logger.info(f"HTLC funded: {htlc_address} "
                           f"txid={result['txid']} confirms={result['confirmations']}")
                return result
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

        _logger.warning(f"HTLC funding timeout: {htlc_address}")
        return None

    async def wait_for_preimage_reveal(
        self,
        htlc_address: str,
        poll_interval: float = 30.0,
        timeout: float = 7200.0,
    ) -> Optional[bytes]:
        """Wait for someone to claim an HTLC (revealing the preimage).

        Monitors the HTLC address for spending transactions, then
        extracts the preimage from the witness data.

        Returns:
            preimage bytes if found, None if timeout
        """
        from .atomic_swap_htlc import extract_preimage_from_witness

        elapsed = 0.0
        while elapsed < timeout:
            txs = await self.get_address_txs(htlc_address)
            for tx_info in txs:
                # Look for spending transactions (not the funding tx)
                for vin in tx_info.get('vin', []):
                    if vin.get('prevout', {}).get('scriptpubkey_address') == htlc_address:
                        # This tx spends from the HTLC — get the full hex
                        tx_hex = await self.get_tx_hex(tx_info['txid'])
                        if tx_hex:
                            preimage = extract_preimage_from_witness(tx_hex)
                            if preimage:
                                _logger.info(f"Preimage revealed: {preimage.hex()[:16]}...")
                                return preimage
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

        _logger.warning(f"Preimage reveal timeout: {htlc_address}")
        return None

    async def get_fee_rate(self) -> int:
        """Get recommended fee rate in sat/vB."""
        result = await self._get("/v1/fees/recommended")
        if result:
            return result.get('halfHourFee', 10)
        return 10  # fallback
