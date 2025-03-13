#!/usr/bin/env python
#
# Electrum - lightweight Bitcoin client
# Copyright (C) 2014 Thomas Voegtlin
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
import asyncio
import hashlib
import time
import traceback
from typing import Dict, List, TYPE_CHECKING, Tuple, Set
from collections import defaultdict
import logging

from aiorpcx import TaskGroup, run_in_thread, RPCError

from . import util
from .transaction import Transaction, PartialTransaction
from .util import bh2u, make_aiohttp_session, NetworkJobOnDefaultServer, random_shuffled_copy
from .bitcoin import address_to_scripthash, is_address
from .logging import Logger
from .interface import GracefulDisconnect, NetworkTimeout

if TYPE_CHECKING:
    from .network import Network
    from .address_synchronizer import AddressSynchronizer

TX_HEIGHT_LOCAL = -2

class SynchronizerFailure(Exception): pass


def history_status(h):
    if not h:
        return None
    status = ''
    for tx_hash, height in h:
        status += tx_hash + ':%d:' % height
    return bh2u(hashlib.sha256(status.encode('ascii')).digest())


class SynchronizerBase(NetworkJobOnDefaultServer):
    """Subscribe over the network to a set of addresses, and monitor their statuses.
    Every time a status changes, run a coroutine provided by the subclass.
    """
    def __init__(self, network: 'Network'):
        self.asyncio_loop = network.asyncio_loop
        self._reset_request_counters()

        NetworkJobOnDefaultServer.__init__(self, network)

    def _reset(self):
        super()._reset()
        self.requested_addrs = set()
        self.scripthash_to_address = {}
        self._processed_some_notifications = False  # so that we don't miss them
        self._reset_request_counters()
        # Queues
        self.add_queue = asyncio.Queue()
        self.status_queue = asyncio.Queue()

    async def _run_tasks(self, *, taskgroup):
        await super()._run_tasks(taskgroup=taskgroup)
        try:
            async with taskgroup as group:
                await group.spawn(self.send_subscriptions())
                await group.spawn(self.handle_status())
                await group.spawn(self.main())
        finally:
            # we are being cancelled now
            self.session.unsubscribe(self.status_queue)

    def _reset_request_counters(self):
        self._requests_sent = 0
        self._requests_answered = 0

    def add(self, addr):
        asyncio.run_coroutine_threadsafe(self._add_address(addr), self.asyncio_loop)

    async def _add_address(self, addr: str):
        # note: this method is async as add_queue.put_nowait is not thread-safe.
        if not is_address(addr): raise ValueError(f"invalid bitcoin address {addr}")
        if addr in self.requested_addrs: return
        self.requested_addrs.add(addr)
        self.add_queue.put_nowait(addr)

    async def _on_address_status(self, addr, status):
        """Handle the change of the status of an address."""
        raise NotImplementedError()  # implemented by subclasses

    async def send_subscriptions(self):
        async def subscribe_to_address(addr):
            h = address_to_scripthash(addr)
            self.scripthash_to_address[h] = addr
            self._requests_sent += 1
            try:
                async with self._network_request_semaphore:
                    await self.session.subscribe('blockchain.scripthash.subscribe', [h], self.status_queue)
            except RPCError as e:
                if e.message == 'history too large':  # no unique error code
                    raise GracefulDisconnect(e, log_level=logging.ERROR) from e
                raise
            self._requests_answered += 1
            self.requested_addrs.remove(addr)

        while True:
            addr = await self.add_queue.get()
            await self.taskgroup.spawn(subscribe_to_address, addr)

    async def handle_status(self):
        while True:
            h, status = await self.status_queue.get()
            addr = self.scripthash_to_address[h]
            await self.taskgroup.spawn(self._on_address_status, addr, status)
            self._processed_some_notifications = True

    def num_requests_sent_and_answered(self) -> Tuple[int, int]:
        return self._requests_sent, self._requests_answered

    async def main(self):
        raise NotImplementedError()  # implemented by subclasses


class Synchronizer(SynchronizerBase):
    '''The synchronizer keeps the wallet up-to-date with its set of
    addresses and their transactions.  It subscribes over the network
    to wallet addresses, gets the wallet to generate new addresses
    when necessary, requests the transaction history of any addresses
    we don't have the full history of, and requests binary transaction
    data of any transactions the wallet doesn't have.
    '''
    def __init__(self, wallet: 'AddressSynchronizer'):
        self.wallet = wallet
        SynchronizerBase.__init__(self, wallet.network)

    def _reset(self):
        super()._reset()
        self.requested_tx = {}
        self.requested_histories = set()
        self._stale_histories = dict()  # type: Dict[str, asyncio.Task]

    def diagnostic_name(self):
        return self.wallet.diagnostic_name()

    def is_up_to_date(self):
        return (not self.requested_addrs
                and not self.requested_histories
                and not self.requested_tx
                and not self._stale_histories)


    # This is a patch for the verify_local_transactions method in the Synchronizer class
    # The issue is that the code is trying to use wallet.db.get_tx_height() which doesn't exist
    # Instead it should use wallet.get_tx_height() which is implemented in AddressSynchronizer

    async def verify_local_transactions(self):
        """Check for local transactions that might be confirmed on blockchain."""
        from .util import TxMinedInfo
        from .blockchain import hash_header
        import time
        
        wallet = self.wallet
        local_txs = []
        
        # Get all transactions with local status
        try:
            all_txs = wallet.db.list_transactions()
            self.logger.error(f"Total number of transactions in wallet: {len(all_txs)}")
            
            for tx_hash in all_txs:
                try:
                    tx_info = wallet.get_tx_height(tx_hash)
                    if tx_info.height == TX_HEIGHT_LOCAL:
                        local_txs.append(tx_hash)
                except Exception as e:
                    self.logger.error(f"Error checking tx height for {tx_hash}: {str(e)}")
        except Exception as e:
            self.logger.error(f"Error listing transactions: {str(e)}")
            return
        
        if not local_txs:
            self.logger.error("No local transactions found to check")
            return
        
        self.logger.info(f"Checking {len(local_txs)} local transactions for confirmations...")
        
        for tx_hash in local_txs:
            try:
                self.logger.error(f"Checking status of local tx: {tx_hash}")
                
                # Get the transaction
                try:
                    raw_tx = await self.network.get_transaction(tx_hash)
                    if not raw_tx:
                        self.logger.error(f"Failed to get transaction {tx_hash}")
                        continue
                    
                    tx = Transaction(raw_tx)
                    self.logger.error(f"Successfully fetched transaction {tx_hash}")
                    
                    # Check if this transaction is in a block
                    try:
                        current_height = self.network.get_local_height()
                        self.logger.error(f"Current blockchain height: {current_height}")
                        
                        block_height = await self.network.get_height_of_transaction(tx_hash)
                        
                        if block_height:
                            self.logger.info(f"Transaction {tx_hash} confirmed at height {block_height}")
                            
                            # Create TxMinedInfo object
                            header_hash = None
                            txpos = 0  # We don't know the position, default to 0
                            
                            # Try to get the header hash using blockchain's read_header method
                            try:
                                if self.network.blockchain():
                                    header = self.network.blockchain().read_header(block_height)
                                    if header:
                                        header_hash = hash_header(header)
                            except Exception as e:
                                self.logger.error(f"Failed to get header hash: {str(e)}")
                            
                            # Get the timestamp from the block header if possible
                            timestamp = None
                            try:
                                if self.network.blockchain():
                                    header = self.network.blockchain().read_header(block_height)
                                    if header:
                                        timestamp = header.get('timestamp')
                            except Exception as e:
                                self.logger.error(f"Failed to get timestamp: {str(e)}")
                            
                            # If we couldn't get the timestamp from the header, use current time
                            if timestamp is None:
                                timestamp = int(time.time())
                            
                            # Create the TxMinedInfo object
                            tx_mined_info = TxMinedInfo(height=block_height, 
                                                    conf=current_height - block_height + 1,
                                                    timestamp=timestamp, 
                                                    txpos=txpos,
                                                    header_hash=header_hash)
                            
                            # Update the transaction status in the wallet
                            wallet.add_verified_tx(tx_hash, tx_mined_info)
                            self.logger.error(f"Updated tx {tx_hash} with height {block_height}")
                            
                    except Exception as e:
                        self.logger.error(f"Error checking blockchain for {tx_hash}: {str(e)}")
                    
                except Exception as e:
                    self.logger.error(f"Failed to get transaction {tx_hash}: {str(e)}")
                    continue
                    
            except Exception as e:
                self.logger.error(f"Unexpected error checking local tx {tx_hash}: {str(e)}")
                continue


            
    async def _on_address_status(self, addr, status):
        history = self.wallet.db.get_addr_history(addr)
        self.logger.debug(f"Address status received for {addr}: old_status={history_status(history)}, new_status={status}")
        if history_status(history) == status:
            self.logger.debug(f"No change in status for {addr}, ignoring update")
            return
        # No point in requesting history twice for the same announced status.
        # However if we got announced a new status, we should request history again:
        if (addr, status) in self.requested_histories:
            return
        # request address history
        self.requested_histories.add((addr, status))
        self._stale_histories.pop(addr, asyncio.Future()).cancel()
        h = address_to_scripthash(addr)
        self._requests_sent += 1
        async with self._network_request_semaphore:
            result = await self.interface.get_history_for_scripthash(h)
        self._requests_answered += 1
        self.logger.info(f"receiving history {addr} {len(result)}")
        hist = list(map(lambda item: (item['tx_hash'], item['height']), result))
        # tx_fees
        tx_fees = [(item['tx_hash'], item.get('fee')) for item in result]
        tx_fees = dict(filter(lambda x:x[1] is not None, tx_fees))
        # Check that the status corresponds to what was announced
        if history_status(hist) != status:
            # could happen naturally if history changed between getting status and history (race)
            self.logger.info(f"error: status mismatch: {addr}. we'll wait a bit for status update.")
            # The server is supposed to send a new status notification, which will trigger a new
            # get_history. We shall wait a bit for this to happen, otherwise we disconnect.
            async def disconnect_if_still_stale():
                timeout = self.network.get_network_timeout_seconds(NetworkTimeout.Generic)
                await asyncio.sleep(timeout)
                raise SynchronizerFailure(f"timeout reached waiting for addr {addr}: history still stale")
            self._stale_histories[addr] = await self.taskgroup.spawn(disconnect_if_still_stale)
        else:
            self._stale_histories.pop(addr, asyncio.Future()).cancel()
            # Store received history
            self.wallet.receive_history_callback(addr, hist, tx_fees)
            # Request transactions we don't have
            await self._request_missing_txs(hist)

        # Remove request; this allows up_to_date to be True
        self.requested_histories.discard((addr, status))

    async def _request_missing_txs(self, hist, *, allow_server_not_finding_tx=False):
        # "hist" is a list of [tx_hash, tx_height] lists
        transaction_hashes = []
        for tx_hash, tx_height in hist:
            if tx_hash in self.requested_tx:
                continue
            tx = self.wallet.db.get_transaction(tx_hash)
            if tx and not isinstance(tx, PartialTransaction):
                continue  # already have complete tx
            transaction_hashes.append(tx_hash)
            self.requested_tx[tx_hash] = tx_height

        if not transaction_hashes: return
        async with TaskGroup() as group:
            for tx_hash in transaction_hashes:
                await group.spawn(self._get_transaction(tx_hash, allow_server_not_finding_tx=allow_server_not_finding_tx))

    async def _get_transaction(self, tx_hash, *, allow_server_not_finding_tx=False):
        self._requests_sent += 1
        try:
            async with self._network_request_semaphore:
                raw_tx = await self.interface.get_transaction(tx_hash)
        except RPCError as e:
            # most likely, "No such mempool or blockchain transaction"
            if allow_server_not_finding_tx:
                self.requested_tx.pop(tx_hash)
                return
            else:
                raise
        finally:
            self._requests_answered += 1
        tx = Transaction(raw_tx)
        if tx_hash != tx.txid():
            raise SynchronizerFailure(f"received tx does not match expected txid ({tx_hash} != {tx.txid()})")
        tx_height = self.requested_tx.pop(tx_hash)
        self.wallet.receive_tx_callback(tx_hash, tx, tx_height)
        self.logger.info(f"received tx {tx_hash} height: {tx_height} bytes: {len(raw_tx)}")
        # callbacks
        util.trigger_callback('new_transaction', self.wallet, tx)

    async def _manage_address_refresh(self):
        """Periodically refresh address history for subscribed addresses"""
        while True:
            await asyncio.sleep(60)  # Check every minute
            if not self.network.is_connected():
                continue
                
            self.logger.error("Running manual address history refresh")
            for addr in self.wallet.get_addresses():
                h = address_to_scripthash(addr)
                await self.interface.session.send_request('blockchain.scripthash.get_history', [h])
                self.logger.error(f"Address {addr} history")
                await asyncio.sleep(0.1)  # Avoid overwhelming the server
            self.logger.error("Completed manual address history refresh")

    async def main(self):
        addr = "MGbiwxErpjZWsi67R4g8x1hhsng8Xhgywa"
        h = address_to_scripthash(addr)
        print(f"Scripthash for {addr}: {h}")
        self.wallet.set_up_to_date(False)
        # request missing txns, if any
        for addr in random_shuffled_copy(self.wallet.db.get_history()):
            history = self.wallet.db.get_addr_history(addr)
            # Old electrum servers returned ['*'] when all history for the address
            # was pruned. This no longer happens but may remain in old wallets.
            if history == ['*']: continue
            await self._request_missing_txs(history, allow_server_not_finding_tx=True)
        # add addresses to bootstrap
        for addr in random_shuffled_copy(self.wallet.get_addresses()):
            await self._add_address(addr)
        
        last_local_tx_check = 0
        last_addr_check = 0
        verification_in_progress = False
        refresh_in_progress = False
        
        # main loop
        while True:
            await asyncio.sleep(0.1)
            try:
                await run_in_thread(self.wallet.synchronize)
            except Exception as e:
                self.logger.error(f"Error in wallet synchronize: {str(e)}")

            current_time = time.time()
            
            # Check if wallet is already synchronized
            if self.wallet.is_up_to_date():
                # Periodically verify local transactions (every 5 minutes)
                if current_time - last_local_tx_check > (5 * 60) and not verification_in_progress:  
                    try:
                        verification_in_progress = True
                        self.logger.error("Starting local transaction verification")
                        try:
                            await asyncio.wait_for(self.verify_local_transactions(), timeout=60)
                            self.logger.error("Local transaction verification completed")
                        except asyncio.TimeoutError:
                            self.logger.error("Local transaction verification timed out after 60 seconds")
                        last_local_tx_check = current_time
                    except Exception as e:
                        self.logger.error(f"Error in verify_local_transactions: {str(e)}")
                    finally:
                        verification_in_progress = False
                
                # Periodically check for updates to wallet addresses (every 30 seconds)
                if current_time - last_addr_check > 30 and not refresh_in_progress:
                    try:
                        refresh_in_progress = True
                        self.logger.error("Starting address history refresh cycle")
                        
                        # Log the current addresses we're checking
                        addresses = self.wallet.get_addresses()
                        #self.logger.error(f"Checking {len(addresses)} addresses for updates")
                        
                        for addr in random_shuffled_copy(addresses):
                            #self.logger.error(f"Checking history for address: {addr}")
                            
                            h = address_to_scripthash(addr)
                            self.scripthash_to_address[h] = addr
                            
                            try:
                                # Get current history from wallet
                                old_history = self.wallet.db.get_addr_history(addr)
                                old_hist_status = history_status(old_history)
                                self.logger.error(f"Current history status for {addr}: {old_hist_status}")
                                
                                # Get fresh history from server
                                async with self._network_request_semaphore:
                                    history_result = await self.session.send_request('blockchain.scripthash.get_history', [h])
                                    #self.logger.error(f"Server returned {len(history_result)} history items for {addr}")
                                    
                                    # Convert to same format as wallet history
                                    new_history = list(map(lambda item: (item['tx_hash'], item['height']), history_result))
                                    new_hist_status = history_status(new_history)
                                    #self.logger.error(f"New history status for {addr}: {new_hist_status}")
                                    
                                    # Compare history statuses
                                    if old_hist_status != new_hist_status:
                                        self.logger.error(f"History changed for {addr}! Processing update...")
                                        
                                        # Process similarly to _on_address_status
                                        hist = new_history
                                        tx_fees = [(item['tx_hash'], item.get('fee')) for item in history_result]
                                        tx_fees = dict(filter(lambda x:x[1] is not None, tx_fees))
                                        
                                        # Store received history
                                        self.wallet.receive_history_callback(addr, hist, tx_fees)
                                        
                                        # Request transactions we don't have
                                        await self._request_missing_txs(hist)
                                    # else:
                                    #     self.logger.error(f"No history change for {addr}")
                                        
                            except Exception as e:
                                self.logger.error(f"Error checking history for {addr}: {str(e)}")
                                self.logger.error(f"Exception details: {traceback.format_exc()}")
                            
                        self.logger.error("Completed address history refresh cycle")
                        last_addr_check = current_time
                    except Exception as e:
                        self.logger.error(f"Error in address history refresh: {str(e)}")
                        self.logger.error(f"Exception details: {traceback.format_exc()}")
                    finally:
                        refresh_in_progress = False
                    
            up_to_date = self.is_up_to_date()
            if (up_to_date != self.wallet.is_up_to_date()
                    or up_to_date and self._processed_some_notifications):
                self._processed_some_notifications = False
                if up_to_date:
                    self._reset_request_counters()
                self.wallet.set_up_to_date(up_to_date)
                util.trigger_callback('wallet_updated', self.wallet)


class Notifier(SynchronizerBase):
    """Watch addresses. Every time the status of an address changes,
    an HTTP POST is sent to the corresponding URL.
    """
    def __init__(self, network):
        SynchronizerBase.__init__(self, network)
        self.watched_addresses = defaultdict(list)  # type: Dict[str, List[str]]
        self._start_watching_queue = asyncio.Queue()  # type: asyncio.Queue[Tuple[str, str]]

    async def main(self):
        # resend existing subscriptions if we were restarted
        for addr in self.watched_addresses:
            await self._add_address(addr)
        # main loop
        while True:
            addr, url = await self._start_watching_queue.get()
            self.watched_addresses[addr].append(url)
            await self._add_address(addr)

    async def start_watching_addr(self, addr: str, url: str):
        await self._start_watching_queue.put((addr, url))

    async def stop_watching_addr(self, addr: str):
        self.watched_addresses.pop(addr, None)
        # TODO blockchain.scripthash.unsubscribe

    async def _on_address_status(self, addr, status):
        if addr not in self.watched_addresses:
            return
        self.logger.info(f'new status for addr {addr}')
        headers = {'content-type': 'application/json'}
        data = {'address': addr, 'status': status}
        for url in self.watched_addresses[addr]:
            try:
                async with make_aiohttp_session(proxy=self.network.proxy, headers=headers) as session:
                    async with session.post(url, json=data, headers=headers) as resp:
                        await resp.text()
            except Exception as e:
                self.logger.info(repr(e))
            else:
                self.logger.info(f'Got Response for {addr}')
