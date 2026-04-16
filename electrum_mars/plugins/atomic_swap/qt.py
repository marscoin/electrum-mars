"""
Atomic Swap Qt GUI plugin for Electrum-Mars.

Adds an "Atomic Swap" tab to the wallet window where users can:
- Browse available swap offers (buy MARS with BTC)
- Create offers (sell MARS for BTC)
- Monitor active swaps
- View swap history
"""

import os
import time
import asyncio
from typing import Optional, TYPE_CHECKING

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QTableWidget, QTableWidgetItem, QHeaderView, QTabWidget,
    QTextEdit, QLineEdit, QGroupBox, QFormLayout, QMessageBox,
    QProgressBar, QDialog, QDialogButtonBox, QComboBox, QSizePolicy,
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QFont

from electrum_mars.plugin import BasePlugin, hook
from electrum_mars.i18n import _
from electrum_mars.util import format_satoshis
from electrum_mars.logging import get_logger

from .swap_engine import SwapEngine, SwapData, SwapState, SwapRole
from .orderbook import OrderBook, SwapOffer
from .automaker import AutoMaker, AutoMakerConfig
from .swap_worker import SwapWorker

if TYPE_CHECKING:
    from electrum_mars.gui.qt.main_window import ElectrumWindow
    from electrum_mars.wallet import Abstract_Wallet

_logger = get_logger(__name__)


class Plugin(BasePlugin):
    """Atomic Swap plugin — adds P2P BTC/MARS trading to wallet."""

    def __init__(self, parent, config, name):
        BasePlugin.__init__(self, parent, config, name)
        self.windows = {}  # wallet -> AtomicSwapTab

    @hook
    def load_wallet(self, wallet: 'Abstract_Wallet', window: 'ElectrumWindow'):
        """Called when a wallet is loaded — add the Atomic Swap tab."""
        data_dir = os.path.join(window.config.electrum_path(), 'atomic_swaps')
        os.makedirs(data_dir, exist_ok=True)

        network = window.network
        engine = SwapEngine(wallet, network, data_dir)
        orderbook = OrderBook()

        tab = AtomicSwapTab(window, engine, orderbook)
        # Set attributes expected by the tab iteration code
        tab.tab_name = 'atomic_swap'
        tab.tab_description = 'Atomic Swap'
        tab.tab_pos = 99  # at the end
        from electrum_mars.gui.qt.util import read_QIcon
        tab.tab_icon = read_QIcon("marscoin_32x32.png")
        window.tabs.addTab(tab, tab.tab_icon, 'Atomic Swap')
        self.windows[wallet] = tab

        # Start the background worker that drives active swaps forward
        try:
            def password_getter():
                if wallet.has_password():
                    return window.password_dialog()
                return None
            worker = SwapWorker(engine, password_getter=password_getter)
            if network:
                network.run_from_another_thread(
                    self._start_worker_coro(worker))
            tab._swap_worker = worker
        except Exception as e:
            from electrum_mars.logging import get_logger
            get_logger(__name__).warning(f'Could not start SwapWorker: {e}')

    async def _start_worker_coro(self, worker):
        worker.start()

    @hook
    def on_close_window(self, window: 'ElectrumWindow'):
        wallet = window.wallet
        if wallet in self.windows:
            tab = self.windows[wallet]
            if hasattr(tab, '_swap_worker') and tab._swap_worker:
                tab._swap_worker.stop()
            tab = self.windows.pop(wallet)
            idx = window.tabs.indexOf(tab)
            if idx >= 0:
                window.tabs.removeTab(idx)


class AtomicSwapTab(QWidget):
    """The Atomic Swap tab in the wallet UI."""

    def __init__(self, window: 'ElectrumWindow', engine: SwapEngine,
                 orderbook: OrderBook):
        QWidget.__init__(self)
        self.window = window
        self.engine = engine
        self.orderbook = orderbook

        data_dir = os.path.join(window.config.electrum_path(), 'atomic_swaps')
        self.automaker = AutoMaker(engine, orderbook, data_dir)

        self._setup_ui()
        self._start_refresh_timer()

        # Auto-start if config says enabled (e.g. was running before restart)
        if self.automaker.config.enabled and self.automaker.config.btc_receive_address:
            self.automaker.start()
            _logger.info("Auto-maker auto-started from saved config")

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        # Header
        # Note: don't use _() for strings containing "BTC" — the i18n module
        # replaces "BTC" with "MARS" which mangles our cross-chain labels
        header = QLabel('Atomic Swaps \u2014 Trade BTC for MARS, Peer-to-Peer')
        header.setFont(QFont('', 14, QFont.Bold))
        layout.addWidget(header)

        desc = QLabel('No exchange needed. Trustless settlement via hash time-locked contracts.')
        desc.setStyleSheet("color: gray;")
        layout.addWidget(desc)

        # Action buttons — use QGridLayout to guarantee equal column widths
        from PyQt5.QtWidgets import QGridLayout
        btn_grid = QGridLayout()
        btn_grid.setSpacing(0)
        BUTTON_MIN_HEIGHT = 44
        BTN = ("QPushButton {{ font-size: 14px; padding: 10px; "
               "border: 1px solid #bbb; min-height: {h}px; }}".format(
                   h=BUTTON_MIN_HEIGHT))

        self.buy_btn = QPushButton('Buy MARS with BTC')
        self.buy_btn.clicked.connect(self._on_buy_mars)
        self.buy_btn.setStyleSheet(
            BTN + "QPushButton { background-color: #c0392b; color: white; }")
        btn_grid.addWidget(self.buy_btn, 0, 0)

        self.sell_btn = QPushButton('Sell MARS for BTC')
        self.sell_btn.clicked.connect(self._on_sell_mars)
        self.sell_btn.setStyleSheet(
            BTN + "QPushButton { background-color: #ecf0f1; color: #2c3e50; }")
        btn_grid.addWidget(self.sell_btn, 0, 1)

        self.refresh_btn = QPushButton('Refresh Offers')
        self.refresh_btn.clicked.connect(self._refresh_offers)
        self.refresh_btn.setStyleSheet(
            BTN + "QPushButton { background-color: #ecf0f1; color: #2c3e50; }")
        btn_grid.addWidget(self.refresh_btn, 0, 2)

        self.automaker_btn = QPushButton('Auto-Maker')
        self.automaker_btn.clicked.connect(self._on_automaker)
        self.automaker_btn.setStyleSheet(
            BTN + "QPushButton { background-color: #2c3e50; color: white; }")
        btn_grid.addWidget(self.automaker_btn, 0, 3)

        # Force all 4 columns to stretch equally
        for col in range(4):
            btn_grid.setColumnStretch(col, 1)
        layout.addLayout(btn_grid)

        # Auto-maker status bar
        self.automaker_status = QLabel('')
        self.automaker_status.setStyleSheet("color: #27ae60; font-size: 12px; padding: 2px;")
        layout.addWidget(self.automaker_status)

        # Sub-tabs
        self.sub_tabs = QTabWidget()

        # Offers tab
        self.offers_widget = self._create_offers_tab()
        self.sub_tabs.addTab(self.offers_widget, _('Available Offers'))

        # Active swaps tab
        self.active_widget = self._create_active_tab()
        self.sub_tabs.addTab(self.active_widget, _('Active Swaps'))

        # History tab
        self.history_widget = self._create_history_tab()
        self.sub_tabs.addTab(self.history_widget, _('History'))

        # Manual offer exchange tab (MVP)
        self.manual_widget = self._create_manual_tab()
        self.sub_tabs.addTab(self.manual_widget, _('Manual Exchange'))

        layout.addWidget(self.sub_tabs)

    def _create_offers_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        # Status bar at top of offers — shows loading/empty state
        self.offers_status = QLabel('')
        self.offers_status.setAlignment(Qt.AlignCenter)
        self.offers_status.setStyleSheet(
            "color: #7f8c8d; font-size: 13px; padding: 20px;")
        layout.addWidget(self.offers_status)

        self.offers_table = QTableWidget()
        self.offers_table.setColumnCount(5)
        self.offers_table.setHorizontalHeaderLabels([
            'MARS Amount', 'BTC Amount', 'Rate (BTC/MARS)',
            'Maker', 'Action',
        ])
        self.offers_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.Stretch)
        self.offers_table.setSelectionBehavior(QTableWidget.SelectRows)
        layout.addWidget(self.offers_table)

        # Track loading state
        self._offers_loading_start = None
        self._offers_ever_loaded = False

        return w

    def _create_active_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        self.active_table = QTableWidget()
        self.active_table.setColumnCount(7)
        self.active_table.setHorizontalHeaderLabels([
            'Swap ID', 'Role', 'MARS', 'BTC',
            'Status', 'Time', 'Action',
        ])
        self.active_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.Stretch)
        layout.addWidget(self.active_table)

        return w

    def _create_history_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        self.history_table = QTableWidget()
        self.history_table.setColumnCount(6)
        self.history_table.setHorizontalHeaderLabels([
            _('Swap ID'), _('Role'), _('MARS'), 'BTC',
            _('Status'), _('Date'),
        ])
        self.history_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.Stretch)
        layout.addWidget(self.history_table)

        return w

    def _create_manual_tab(self) -> QWidget:
        """Manual offer exchange for MVP — paste/copy offers as JSON."""
        w = QWidget()
        layout = QVBoxLayout(w)

        layout.addWidget(QLabel(_(
            'For the MVP, you can exchange offers manually by copying/pasting JSON. '
            'ElectrumX-based automatic discovery will be added in a future update.'
        )))

        # Import section
        import_group = QGroupBox(_('Import Offers'))
        import_layout = QVBoxLayout(import_group)
        self.import_text = QTextEdit()
        self.import_text.setPlaceholderText(_('Paste offer JSON here...'))
        self.import_text.setMaximumHeight(100)
        import_layout.addWidget(self.import_text)
        import_btn = QPushButton(_('Import'))
        import_btn.clicked.connect(self._import_offers)
        import_layout.addWidget(import_btn)
        layout.addWidget(import_group)

        # Export section
        export_group = QGroupBox(_('Export My Offers'))
        export_layout = QVBoxLayout(export_group)
        self.export_text = QTextEdit()
        self.export_text.setReadOnly(True)
        self.export_text.setMaximumHeight(100)
        export_layout.addWidget(self.export_text)
        export_btn = QPushButton(_('Copy to Clipboard'))
        export_btn.clicked.connect(self._export_offers)
        export_layout.addWidget(export_btn)
        layout.addWidget(export_group)

        layout.addStretch()
        return w

    def _start_refresh_timer(self):
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._refresh_all)
        self.timer.start(10000)  # refresh every 10 seconds

    def _refresh_all(self):
        self._refresh_offers()
        self._refresh_active()
        self._refresh_history()
        self._update_automaker_status()

    def _refresh_offers(self):
        # Track loading start time on first call
        if self._offers_loading_start is None:
            import time as _time
            self._offers_loading_start = _time.time()

        # Fetch from ElectrumX in the background
        if self.window.network:
            try:
                coro = self.orderbook.fetch_from_electrumx(self.window.network)
                self.window.network.run_from_another_thread(coro)
                self._offers_ever_loaded = True
            except Exception:
                pass  # ElectrumX may not support atomicswap yet

        # Build set of my own offer IDs and pubkeys from local DB
        my_swaps = self.engine.get_all_swaps()
        my_pubkeys = {s.my_pubkey for s in my_swaps if s.my_pubkey}
        my_offer_ids = {s.swap_id for s in my_swaps
                        if s.role == SwapRole.MAKER.value}

        # Don't filter — show all, but mark mine differently
        all_offers = list(self.orderbook._offers.values())
        all_offers = [o for o in all_offers if not o.is_expired()]
        all_offers.sort(key=lambda o: o.rate)

        # Update status message based on state
        import time as _time
        elapsed = _time.time() - self._offers_loading_start if self._offers_loading_start else 0
        if len(all_offers) == 0:
            if not self._offers_ever_loaded and elapsed < 10:
                self.offers_status.setText(
                    '\u231b  Loading offers from the network...')
                self.offers_table.setVisible(False)
            elif elapsed < 300:  # 5 minutes
                self.offers_status.setText(
                    '\u231b  Searching for offers... ({:.0f}s)'.format(elapsed))
                self.offers_table.setVisible(False)
            else:
                self.offers_status.setText(
                    '\U0001f4ed  No offers found at the moment.\n\n'
                    'Click "Sell MARS for BTC" above to create the first offer,\n'
                    'or enable Auto-Maker to let your wallet trade passively.')
                self.offers_table.setVisible(False)
        else:
            self.offers_status.setText('')
            self.offers_table.setVisible(True)

        self.offers_table.setRowCount(len(all_offers))
        for i, offer in enumerate(all_offers):
            is_mine = (offer.maker_pubkey in my_pubkeys or
                       offer.offer_id in my_offer_ids)

            self.offers_table.setItem(i, 0, QTableWidgetItem(
                f'{offer.mars_amount:.4f}'))
            self.offers_table.setItem(i, 1, QTableWidgetItem(
                f'{offer.btc_amount:.8f}'))
            self.offers_table.setItem(i, 2, QTableWidgetItem(
                f'{offer.rate:.8f}'))
            maker_display = offer.maker_address[:12] + '...' if offer.maker_address else '?'
            if is_mine:
                maker_display = 'YOU (' + maker_display + ')'
            maker_item = QTableWidgetItem(maker_display)
            if is_mine:
                from PyQt5.QtGui import QColor
                maker_item.setForeground(QColor('#c0392b'))
            self.offers_table.setItem(i, 3, maker_item)

            if is_mine:
                cancel_btn = QPushButton('Cancel')
                cancel_btn.setStyleSheet("color: #c0392b;")
                cancel_btn.clicked.connect(
                    lambda _, oid=offer.offer_id: self._cancel_offer_by_id(oid))
                self.offers_table.setCellWidget(i, 4, cancel_btn)
            else:
                accept_btn = QPushButton('Accept')
                accept_btn.clicked.connect(lambda _, o=offer: self._accept_offer(o))
                self.offers_table.setCellWidget(i, 4, accept_btn)

    def _cancel_offer_by_id(self, offer_id: str):
        """Cancel an offer by ID (from the offers table)."""
        # Find and cancel the corresponding swap
        swap = self.engine.get_swap(offer_id)
        if swap:
            self._cancel_swap(swap)
        else:
            # Just remove from orderbook
            self.orderbook.remove_offer(offer_id)
            if self.engine.network:
                try:
                    interface = self.engine.network.interface
                    if interface:
                        coro = interface.session.send_request(
                            'atomicswap.cancel_offer', [offer_id])
                        self.engine.network.run_from_another_thread(coro)
                except Exception:
                    pass
            self._refresh_all()

    def _refresh_active(self):
        swaps = self.engine.get_active_swaps()
        self.active_table.setRowCount(len(swaps))
        for i, swap in enumerate(swaps):
            summary = self.engine.get_swap_summary(swap)
            self.active_table.setItem(i, 0, QTableWidgetItem(
                summary['swap_id']))
            self.active_table.setItem(i, 1, QTableWidgetItem(
                summary['role'].upper()))
            self.active_table.setItem(i, 2, QTableWidgetItem(
                f"{summary['mars_amount']:.4f}"))
            self.active_table.setItem(i, 3, QTableWidgetItem(
                f"{summary['btc_amount']:.8f}"))
            # Human-readable status labels
            state = summary['state']
            role = summary['role']
            if role == 'maker':
                if state == 'created' and swap.peer_pubkey:
                    status_text = 'Buyer accepted! Funding MARS...'
                elif state == 'created':
                    status_text = 'Offer posted. Waiting for buyer...'
                else:
                    status_labels = {
                        'mars_locked': 'MARS locked. Waiting for BTC...',
                        'btc_locked': 'BTC received! Claiming...',
                        'btc_claimed': 'Complete! BTC received.',
                    }
                    status_text = status_labels.get(state,
                        state.replace('_', ' ').upper())
            else:
                status_labels = {
                    'created': 'Waiting for your BTC payment...',
                    'btc_locked': 'BTC confirmed. Waiting for seller...',
                    'completed': 'MARS received!',
                }
                status_text = status_labels.get(state,
                    state.replace('_', ' ').upper())
            self.active_table.setItem(i, 4, QTableWidgetItem(status_text))
            # Format age nicely
            age_min = summary['age_minutes']
            if age_min < 60:
                age_str = f'{age_min}m ago'
            elif age_min < 1440:
                age_str = f'{age_min // 60}h ago'
            else:
                age_str = f'{age_min // 1440}d ago'
            self.active_table.setItem(i, 5, QTableWidgetItem(age_str))
            # Action button depends on state and role
            if swap.state == SwapState.CREATED.value:
                cancel_btn = QPushButton('Cancel')
                cancel_btn.clicked.connect(lambda _, s=swap: self._cancel_swap(s))
                self.active_table.setCellWidget(i, 6, cancel_btn)
            elif swap.state == SwapState.BTC_CLAIMED.value:
                # Maker has claimed BTC — swap is effectively complete.
                # Do NOT show "Refund MARS" — the taker has the preimage
                # and their claim tx is either mined or in mempool.
                done_label = QPushButton('\u2713 Done')
                done_label.setStyleSheet(
                    "background-color: #27ae60; color: white;")
                done_label.setEnabled(False)
                self.active_table.setCellWidget(i, 6, done_label)
            elif (swap.role == SwapRole.TAKER.value
                  and swap.state == SwapState.BTC_LOCKED.value):
                # Taker at BTC_LOCKED: the worker is trying to extract
                # the preimage and claim MARS. Show a spinner-style label.
                # Also offer Refund BTC as fallback if something goes wrong.
                claiming_btn = QPushButton('Claiming MARS...')
                claiming_btn.setStyleSheet(
                    "background-color: #3498db; color: white;")
                claiming_btn.setEnabled(False)
                self.active_table.setCellWidget(i, 6, claiming_btn)
            elif (swap.role == SwapRole.TAKER.value
                  and swap.state == SwapState.CREATED.value
                  and swap.btc_htlc_address):
                # Taker at CREATED — hasn't sent BTC yet or it's unconfirmed.
                # Can cancel or refund if they already sent.
                refund_btn = QPushButton('Refund BTC')
                refund_btn.setStyleSheet(
                    "background-color: #e67e22; color: white;")
                refund_btn.clicked.connect(
                    lambda _, s=swap: self._refund_btc_swap(s))
                self.active_table.setCellWidget(i, 6, refund_btn)
            elif (swap.role == SwapRole.MAKER.value
                  and swap.state == SwapState.MARS_LOCKED.value
                  and swap.mars_funding_txid):
                # Maker can refund MARS after timelock — but ONLY if
                # the swap hasn't progressed to BTC_CLAIMED (handled above)
                refund_btn = QPushButton('Refund MARS')
                refund_btn.setStyleSheet(
                    "background-color: #e67e22; color: white;")
                refund_btn.clicked.connect(
                    lambda _, s=swap: self._refund_mars_swap(s))
                self.active_table.setCellWidget(i, 6, refund_btn)

    def _refresh_history(self):
        swaps = self.engine.get_all_swaps()
        terminal = {SwapState.COMPLETED.value, SwapState.FAILED.value,
                    SwapState.EXPIRED.value, SwapState.MARS_REFUNDED.value,
                    SwapState.BTC_REFUNDED.value,
                    SwapState.BTC_CLAIMED.value}
        history = [s for s in swaps if s.state in terminal]
        self.history_table.setRowCount(len(history))
        for i, swap in enumerate(history):
            summary = self.engine.get_swap_summary(swap)
            self.history_table.setItem(i, 0, QTableWidgetItem(
                summary['swap_id']))
            self.history_table.setItem(i, 1, QTableWidgetItem(
                summary['role'].upper()))
            self.history_table.setItem(i, 2, QTableWidgetItem(
                f"{summary['mars_amount']:.4f}"))
            self.history_table.setItem(i, 3, QTableWidgetItem(
                f"{summary['btc_amount']:.8f}"))
            self.history_table.setItem(i, 4, QTableWidgetItem(
                summary['state'].replace('_', ' ').upper()))
            self.history_table.setItem(i, 5, QTableWidgetItem(
                time.strftime('%Y-%m-%d', time.localtime(swap.created_at))))

    def _cancel_swap(self, swap: SwapData):
        """Cancel a swap that hasn't been funded yet."""
        reply = QMessageBox.question(
            self, 'Cancel Swap',
            f'Cancel this swap?\n\n'
            f'Swap ID: {swap.swap_id[:8]}\n'
            f'Role: {swap.role.upper()}\n'
            f'Amount: {swap.mars_amount_sat/1e8:.4f} MARS\n\n'
            f'This only works for swaps not yet funded on-chain.',
            QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        # Mark as failed in local DB
        swap.state = SwapState.FAILED.value
        swap.error_msg = 'Cancelled by user'
        self.engine.db.save(swap)
        # Remove from orderbook
        self.orderbook.remove_offer(swap.swap_id)
        # Try to cancel on ElectrumX relay
        if self.engine.network:
            try:
                interface = self.engine.network.interface
                if interface:
                    coro = interface.session.send_request(
                        'atomicswap.cancel_offer', [swap.swap_id])
                    self.engine.network.run_from_another_thread(coro)
            except Exception:
                pass
        self._refresh_all()

    def _refund_btc_swap(self, swap: SwapData):
        """Taker reclaims BTC from the HTLC after timelock expires."""
        d = BtcRefundDialog(self, swap)
        d.exec_()
        self._refresh_all()

    def _refund_mars_swap(self, swap: SwapData):
        """Maker reclaims MARS from the HTLC after timelock expires."""
        reply = QMessageBox.question(
            self, 'Refund MARS',
            f'Refund the MARS HTLC back to your wallet?\n\n'
            f'Amount: {swap.mars_amount_sat/1e8:.4f} MARS\n'
            f'Locktime: block {swap.mars_locktime}\n\n'
            f'This only succeeds after the locktime has passed.\n'
            f'Before that, the Marscoin network will reject the tx.',
            QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes:
            return

        if not self.engine.network:
            QMessageBox.warning(self, 'Error', 'Not connected to network')
            return

        # Get password if wallet is encrypted
        password = None
        if self.engine.wallet.has_password():
            password = self.window.password_dialog()
            if password is None:
                return

        try:
            async def do_refund():
                return await self.engine.refund_mars_htlc(
                    swap.swap_id, password=password)

            coro = do_refund()
            result = self.engine.network.run_from_another_thread(coro)
            QMessageBox.information(
                self, 'MARS Refunded',
                f'Refund broadcast!\n\nTxid: {result}')
        except Exception as e:
            QMessageBox.warning(self, 'Refund Failed', str(e))

    def _on_automaker(self):
        """Open auto-maker configuration dialog."""
        d = AutoMakerDialog(self.window, self.automaker)
        d.exec_()
        self._update_automaker_status()

    def _update_automaker_status(self):
        if self.automaker.is_running():
            s = self.automaker.get_status_summary()
            self.automaker_status.setText(
                f"\u2022 Auto-Maker ACTIVE \u2014 "
                f"Fee: {s['fee_percent']:.1f}% | "
                f"Offers: {s['active_offers']} | "
                f"Available: {s['available_balance']:.2f} MARS | "
                f"Earned: {s['total_btc_earned']:.8f} BTC | "
                f"Rate: {s['last_rate']:.10f} BTC/MARS"
            )
            self.automaker_btn.setStyleSheet(
                "font-size: 14px; padding: 10px; background-color: #27ae60; color: white;")
        else:
            self.automaker_status.setText('')
            self.automaker_btn.setStyleSheet(
                "font-size: 14px; padding: 10px; background-color: #2c3e50; color: white;")

    def _on_buy_mars(self):
        """User wants to buy MARS with BTC."""
        best = self.orderbook.get_best_offer()
        if best:
            self._accept_offer(best)
        else:
            QMessageBox.information(self, _('No Offers'),
                _('No swap offers available right now.\n\n'
                  'Try importing offers from the Manual Exchange tab, '
                  'or check back later.'))

    def _on_sell_mars(self):
        """User wants to sell MARS for BTC."""
        d = CreateOfferDialog(self.window, self.engine, self.orderbook)
        d.exec_()
        self._refresh_all()

    def _accept_offer(self, offer: SwapOffer):
        """Accept a swap offer (taker flow)."""
        msg = (
            f'Accept this swap offer?\n\n'
            f'You send: {offer.btc_amount:.8f} BTC\n'
            f'You receive: {offer.mars_amount:.4f} MARS\n'
            f'Rate: {offer.rate:.8f} BTC/MARS\n\n'
            f'You will need to send BTC to a generated HTLC address. '
            f'The swap will complete automatically once confirmed.'
        )
        reply = QMessageBox.question(self, 'Accept Offer', msg,
                                     QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes:
            return

        # Get current BTC block height from mempool.space
        btc_height = 850000
        try:
            from electrum_mars.btc_monitor import BtcMonitor
            monitor = BtcMonitor()
            import asyncio
            loop = asyncio.get_event_loop()
            h = loop.run_until_complete(monitor.get_block_height())
            if h:
                btc_height = h
        except Exception:
            pass

        swap = self.engine.create_taker_swap(
            mars_amount_sat=offer.mars_amount_sat,
            btc_amount_sat=offer.btc_amount_sat,
            payment_hash160=offer.payment_hash160,
            peer_pubkey=offer.maker_pubkey,
            mars_htlc_address=offer.mars_htlc_address,
            mars_htlc_script=offer.mars_htlc_script,
            mars_locktime=offer.mars_locktime,
            current_btc_height=btc_height,
        )

        # Notify the maker that we accepted the offer — they need our
        # pubkey to build the MARS HTLC script and fund it.
        if self.engine.network:
            try:
                acceptance = {
                    'taker_pubkey': swap.my_pubkey,
                    'btc_htlc_address': swap.btc_htlc_address,
                    'btc_locktime': swap.btc_locktime,
                    'timestamp': time.time(),
                }
                interface = self.engine.network.interface
                if interface:
                    coro = interface.session.send_request(
                        'atomicswap.accept_offer',
                        [offer.offer_id, acceptance])
                    self.engine.network.run_from_another_thread(coro)
                    _logger.info(f'Sent acceptance for offer {offer.offer_id[:8]}')
            except Exception as e:
                _logger.warning(f'Could not send acceptance: {e}')

        # Remove the accepted offer from the local order book so it
        # no longer shows in Available Offers with an active Accept button
        self.orderbook.remove_offer(offer.offer_id)

        # Show BTC HTLC address with QR code
        d = BtcPaymentDialog(self, swap)
        d.exec_()

        self._refresh_all()

    def _import_offers(self):
        text = self.import_text.toPlainText().strip()
        if not text:
            return
        try:
            self.orderbook.import_offers_json(text)
            self._refresh_offers()
            self.import_text.clear()
            QMessageBox.information(self, _('Success'),
                _('Offers imported successfully!'))
        except Exception as e:
            QMessageBox.warning(self, _('Error'),
                _('Failed to import offers: ') + str(e))

    def _export_offers(self):
        text = self.orderbook.export_offers_json()
        self.export_text.setPlainText(text)
        from PyQt5.QtWidgets import QApplication
        QApplication.clipboard().setText(text)


class CreateOfferDialog(QDialog):
    """Dialog for creating a new swap offer (selling MARS for BTC)."""

    def __init__(self, window: 'ElectrumWindow', engine: SwapEngine,
                 orderbook: OrderBook):
        QDialog.__init__(self, window)
        self.window = window
        self.engine = engine
        self.orderbook = orderbook
        self.market_rate = 0.0  # BTC per MARS
        self.setWindowTitle('Create Swap Offer')
        self.setMinimumWidth(450)
        self._fetch_price()
        self._setup_ui()

    def _fetch_price(self):
        """Fetch current market price from price.marscoin.org."""
        try:
            import urllib.request, json
            headers = {'User-Agent': 'Electrum-Mars/4.3.2'}
            req = urllib.request.Request(
                'https://price.marscoin.org/json', headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                mars_usd = data['data']['154']['quote']['USD']['price']
            try:
                req2 = urllib.request.Request(
                    'https://mempool.space/api/v1/prices', headers=headers)
                with urllib.request.urlopen(req2, timeout=10) as resp:
                    data = json.loads(resp.read())
                    btc_usd = data.get('USD', 83000)
            except Exception:
                btc_usd = 83000  # fallback
            self.market_rate = mars_usd / btc_usd
            self.mars_usd = mars_usd
            self.btc_usd = btc_usd
            _logger.info(f"Price fetched: MARS ${mars_usd:.4f}, BTC ${btc_usd}, "
                        f"rate {self.market_rate:.10f}")
        except Exception as e:
            _logger.warning(f"Price fetch failed: {e}")
            self.market_rate = 0.0
            self.mars_usd = 0.0
            self.btc_usd = 0.0

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        form = QFormLayout()

        self.mars_amount = QLineEdit()
        self.mars_amount.setPlaceholderText('100')
        form.addRow('MARS to sell:', self.mars_amount)

        # Auto-price checkbox + fee slider
        from PyQt5.QtWidgets import QCheckBox, QSlider, QSpinBox

        price_row = QHBoxLayout()
        self.auto_price_cb = QCheckBox('Use CMC spot price +')
        self.auto_price_cb.setChecked(True)
        self.auto_price_cb.toggled.connect(self._on_auto_price_toggled)
        price_row.addWidget(self.auto_price_cb)

        self.fee_spin = QSpinBox()
        self.fee_spin.setRange(1, 50)
        self.fee_spin.setValue(5)
        self.fee_spin.setSuffix('% fee')
        self.fee_spin.valueChanged.connect(self._recalc_btc)
        price_row.addWidget(self.fee_spin)
        form.addRow('', price_row)

        self.btc_amount = QLineEdit()
        self.btc_amount.setPlaceholderText('0.001')
        self.btc_amount.setReadOnly(True)  # read-only when auto-price is on
        form.addRow('BTC to receive:', self.btc_amount)

        # Market info
        if self.market_rate > 0:
            spot_label = QLabel(
                f'CMC spot: {self.market_rate:.10f} BTC/MARS '
                f'(${self.mars_usd:.4f}/MARS, BTC ${self.btc_usd:,.0f})')
            spot_label.setStyleSheet("color: gray; font-size: 11px;")
            form.addRow('', spot_label)
        else:
            warn = QLabel('\u26a0 Price fetch failed — enter BTC manually')
            warn.setStyleSheet("color: #e74c3c; font-size: 11px;")
            form.addRow('', warn)
            self.auto_price_cb.setChecked(False)
            self.btc_amount.setReadOnly(False)

        # BTC receive address — where the maker's BTC is paid
        self.btc_receive_addr = QLineEdit()
        self.btc_receive_addr.setPlaceholderText('bc1q... (your Bitcoin address)')
        # Try to load saved address
        saved_addr = self.engine.wallet.config.get('atomic_swap_btc_receive_addr', '')
        if saved_addr:
            self.btc_receive_addr.setText(saved_addr)
        form.addRow('BTC receive address:', self.btc_receive_addr)

        hint = QLabel('\u2139 This is YOUR Bitcoin address where you\'ll receive '
                      'the BTC when your MARS sells.\n'
                      'Get it from any Bitcoin wallet (Electrum, Sparrow, hardware, exchange).')
        hint.setStyleSheet("color: gray; font-size: 11px;")
        hint.setWordWrap(True)
        form.addRow('', hint)

        self.timeout_hours = QComboBox()
        self.timeout_hours.addItems(['2 hours', '4 hours', '6 hours', '12 hours'])
        self.timeout_hours.setCurrentIndex(1)
        form.addRow('Offer valid for:', self.timeout_hours)

        layout.addLayout(form)

        # Info label
        self.info_label = QLabel('')
        self.info_label.setStyleSheet("font-weight: bold; padding: 5px;")
        self.mars_amount.textChanged.connect(self._recalc_btc)
        self.btc_amount.textChanged.connect(self._update_info)
        layout.addWidget(self.info_label)

        # Buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._create_offer)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        # Trigger initial calculation
        self._recalc_btc()

    def _on_auto_price_toggled(self, checked):
        self.btc_amount.setReadOnly(checked)
        self.fee_spin.setEnabled(checked)
        if checked:
            self._recalc_btc()

    def _recalc_btc(self):
        """Recalculate BTC amount from MARS amount + market rate + fee."""
        if not self.auto_price_cb.isChecked() or self.market_rate <= 0:
            self._update_info()
            return
        try:
            mars = float(self.mars_amount.text() or '0')
            if mars <= 0:
                self.btc_amount.setText('')
                self.info_label.setText('')
                return
            fee = self.fee_spin.value() / 100.0
            rate_with_fee = self.market_rate * (1 + fee)
            btc = mars * rate_with_fee
            self.btc_amount.setText(f'{btc:.8f}')
            self.info_label.setText(
                f'Rate: {rate_with_fee:.10f} BTC/MARS '
                f'(spot + {self.fee_spin.value()}% fee)')
        except ValueError:
            pass

    def _update_info(self):
        if self.auto_price_cb.isChecked():
            return  # handled by _recalc_btc
        try:
            mars = float(self.mars_amount.text() or '0')
            btc = float(self.btc_amount.text() or '0')
            if mars > 0 and btc > 0:
                rate = btc / mars
                self.info_label.setText(f'Rate: {rate:.10f} BTC/MARS')
            else:
                self.info_label.setText('')
        except ValueError:
            self.info_label.setText('')

    def _create_offer(self):
        try:
            mars_sat = int(float(self.mars_amount.text()) * 1e8)
            btc_sat = int(float(self.btc_amount.text()) * 1e8)
        except ValueError:
            QMessageBox.warning(self, 'Error', 'Invalid amounts')
            return

        if mars_sat <= 0 or btc_sat <= 0:
            QMessageBox.warning(self, 'Error', 'Amounts must be positive')
            return

        # Balance check — can't offer more than you actually have
        wallet = self.engine.wallet
        balance = wallet.get_balance()
        confirmed_sat = balance[0] if balance else 0
        if mars_sat > confirmed_sat:
            QMessageBox.warning(
                self, 'Insufficient Balance',
                f'You cannot sell {mars_sat/1e8:.4f} MARS.\n\n'
                f'Your confirmed balance is {confirmed_sat/1e8:.4f} MARS.\n\n'
                f'(Unconfirmed coins cannot be used for atomic swaps.)')
            return

        # Safety: warn if committing more than 90% of balance
        if mars_sat > confirmed_sat * 0.9:
            reply = QMessageBox.question(
                self, 'Large Offer Warning',
                f'You are offering {mars_sat/1e8:.4f} MARS, which is '
                f'{100*mars_sat/confirmed_sat:.0f}% of your balance.\n\n'
                f'If this swap completes you will have very little MARS left '
                f'(plus network fees). Continue?',
                QMessageBox.Yes | QMessageBox.No)
            if reply != QMessageBox.Yes:
                return

        # Validate BTC receive address
        btc_receive = self.btc_receive_addr.text().strip()
        if not btc_receive:
            QMessageBox.warning(
                self, 'BTC Address Required',
                'You must provide a Bitcoin address where you will receive '
                'the BTC payment when your MARS sells.\n\n'
                'This should be an address you control in any Bitcoin wallet.')
            return
        if not (btc_receive.startswith('bc1') or btc_receive.startswith('1')
                or btc_receive.startswith('3')):
            QMessageBox.warning(
                self, 'Invalid BTC Address',
                'The BTC address should start with "bc1", "1", or "3".\n\n'
                'Please enter a valid mainnet Bitcoin address.')
            return
        # Save for next time
        self.engine.wallet.config.set_key(
            'atomic_swap_btc_receive_addr', btc_receive)

        # Get current Marscoin block height
        current_height = self.engine.network.blockchain().height() if self.engine.network else 0

        # Create the swap
        swap = self.engine.create_maker_swap(
            mars_amount_sat=mars_sat,
            btc_amount_sat=btc_sat,
            current_mars_height=current_height,
            btc_receive_address=btc_receive,
        )

        # Create the offer for the order book
        timeout_map = {0: 2, 1: 4, 2: 6, 3: 12}
        hours = timeout_map.get(self.timeout_hours.currentIndex(), 4)

        now = time.time()
        offer = SwapOffer(
            offer_id=swap.swap_id,
            mars_amount_sat=mars_sat,
            btc_amount_sat=btc_sat,
            rate=btc_sat / mars_sat,
            maker_pubkey=swap.my_pubkey,
            payment_hash160=swap.payment_hash160,
            mars_htlc_address=swap.mars_htlc_address or '',
            mars_htlc_script=swap.mars_htlc_script or '',
            mars_locktime=swap.mars_locktime,
            expires_at=now + hours * 3600,
            maker_address=self.engine.wallet.get_receiving_address(),
            last_seen=now,
        )
        self.orderbook.add_my_offer(offer)

        # Publish to ElectrumX relay
        published = False
        if self.engine.network:
            try:
                from dataclasses import asdict
                coro = self.orderbook.publish_to_electrumx(
                    self.engine.network, offer)
                self.engine.network.run_from_another_thread(coro)
                published = True
            except Exception as e:
                _logger.warning(f"Could not publish to ElectrumX: {e}")

        pub_msg = "Published to network!" if published else \
            "Share the offer JSON from the Manual Exchange tab."
        QMessageBox.information(self, _('Offer Created'),
            f'Swap offer created!\n\n'
            f'Selling: {mars_sat/1e8:.4f} MARS\n'
            f'For: {btc_sat/1e8:.8f} BTC\n\n'
            f'{pub_msg}')

        self.accept()


class AutoMakerDialog(QDialog):
    """Dialog for configuring the Auto-Maker — passive market making."""

    def __init__(self, window: 'ElectrumWindow', automaker: AutoMaker):
        QDialog.__init__(self, window)
        self.window = window
        self.automaker = automaker
        self.setWindowTitle('Auto-Maker \u2014 Passive Market Making')
        self.setMinimumWidth(500)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        # Header
        header = QLabel('Turn your wallet into a market maker')
        header.setFont(QFont('', 13, QFont.Bold))
        layout.addWidget(header)

        desc = QLabel(
            'Set it and forget it. Your wallet will automatically create swap offers,\n'
            'selling MARS for BTC at the market rate plus your fee. Earn a steady\n'
            'commission while helping the Marscoin ecosystem.'
        )
        desc.setStyleSheet("color: gray; margin-bottom: 10px;")
        layout.addWidget(desc)

        # Status
        status = self.automaker.get_status_summary()
        if self.automaker.is_running():
            status_label = QLabel('\u2705 Auto-Maker is RUNNING')
            status_label.setStyleSheet("color: #27ae60; font-weight: bold; font-size: 14px;")
        else:
            status_label = QLabel('\u26aa Auto-Maker is STOPPED')
            status_label.setStyleSheet("color: gray; font-weight: bold; font-size: 14px;")
        layout.addWidget(status_label)

        # Configuration form
        config = self.automaker.config
        form = QFormLayout()

        self.btc_receive_addr_am = QLineEdit(config.btc_receive_address)
        self.btc_receive_addr_am.setPlaceholderText('bc1q... (required)')
        self.btc_receive_addr_am.setToolTip(
            'Your Bitcoin address where BTC earnings will be paid. Required!')
        form.addRow('BTC receive address:', self.btc_receive_addr_am)

        self.fee_input = QLineEdit(str(config.fee_percent))
        self.fee_input.setToolTip(
            'Your minimum profit per swap. The smallest offer uses this exact fee.\n'
            'Larger offers add a premium on top. Competition can compress the\n'
            'premium but never push you below this floor.')
        form.addRow('Minimum fee (%):', self.fee_input)

        self.daily_limit = QLineEdit(str(int(config.daily_limit_mars_sat / 1e8)))
        self.daily_limit.setToolTip('Maximum MARS to sell in a 24-hour period')
        form.addRow('Daily limit (MARS):', self.daily_limit)

        self.max_swap = QLineEdit(str(int(config.max_single_swap_sat / 1e8)))
        self.max_swap.setToolTip('Maximum MARS per single swap')
        form.addRow('Max per swap (MARS):', self.max_swap)

        self.min_swap = QLineEdit(str(int(config.min_single_swap_sat / 1e8)))
        self.min_swap.setToolTip('Minimum MARS per swap (prevents dust)')
        form.addRow('Min per swap (MARS):', self.min_swap)

        self.reserve = QLineEdit(str(config.reserve_percent))
        self.reserve.setToolTip('Percentage of balance to keep unlocked (safety)')
        form.addRow('Reserve (%):', self.reserve)

        self.num_offers = QComboBox()
        self.num_offers.addItems(['1', '2', '3', '5', '10'])
        idx = ['1', '2', '3', '5', '10'].index(str(config.num_offers)) \
            if str(config.num_offers) in ['1', '2', '3', '5', '10'] else 2
        self.num_offers.setCurrentIndex(idx)
        self.num_offers.setToolTip('Number of concurrent offers to maintain')
        form.addRow('Concurrent offers:', self.num_offers)

        self.refresh_interval = QComboBox()
        self.refresh_interval.addItems([
            '1 minute', '5 minutes', '15 minutes', '30 minutes', '1 hour'])
        intervals = [60, 300, 900, 1800, 3600]
        current_idx = 1
        for i, v in enumerate(intervals):
            if config.refresh_interval_sec <= v:
                current_idx = i
                break
        self.refresh_interval.setCurrentIndex(current_idx)
        form.addRow('Price refresh:', self.refresh_interval)

        layout.addLayout(form)

        # Earnings summary
        earnings_group = QGroupBox('Earnings Summary')
        earnings_layout = QFormLayout(earnings_group)
        earnings_layout.addRow('BTC earned (total):',
            QLabel(f"{status['total_btc_earned']:.8f} BTC"))
        earnings_layout.addRow('MARS sold (total):',
            QLabel(f"{status['total_mars_sold']:.2f} MARS"))
        earnings_layout.addRow('Swaps completed:',
            QLabel(str(status['swaps_completed'])))
        earnings_layout.addRow('Sold today:',
            QLabel(f"{status['today_sold']:.2f} / {status['daily_limit']:.0f} MARS"))
        if status['last_rate'] > 0:
            earnings_layout.addRow('Current rate:',
                QLabel(f"{status['last_rate']:.10f} BTC/MARS "
                       f"(${status['last_mars_usd']:.4f}/MARS)"))
        layout.addWidget(earnings_group)

        # Buttons
        btn_layout = QHBoxLayout()

        if self.automaker.is_running():
            self.toggle_btn = QPushButton('\u23f9  Stop Auto-Maker')
            self.toggle_btn.setStyleSheet(
                "font-size: 14px; padding: 10px; background-color: #e74c3c; color: white;")
        else:
            self.toggle_btn = QPushButton('\u25b6  Start Auto-Maker')
            self.toggle_btn.setStyleSheet(
                "font-size: 14px; padding: 10px; background-color: #27ae60; color: white;")
        self.toggle_btn.clicked.connect(self._toggle)
        btn_layout.addWidget(self.toggle_btn)

        save_btn = QPushButton('Save Settings')
        save_btn.clicked.connect(self._save_settings)
        btn_layout.addWidget(save_btn)

        close_btn = QPushButton('Close')
        close_btn.clicked.connect(self.accept)
        btn_layout.addWidget(close_btn)

        layout.addLayout(btn_layout)

    def _save_settings(self):
        try:
            config = self.automaker.config
            btc_addr = self.btc_receive_addr_am.text().strip()
            if btc_addr and not (btc_addr.startswith('bc1') or
                                  btc_addr.startswith('1') or
                                  btc_addr.startswith('3')):
                raise ValueError('Invalid BTC address')
            config.btc_receive_address = btc_addr
            config.fee_percent = float(self.fee_input.text())
            config.daily_limit_mars_sat = int(float(self.daily_limit.text()) * 1e8)
            config.max_single_swap_sat = int(float(self.max_swap.text()) * 1e8)
            config.min_single_swap_sat = int(float(self.min_swap.text()) * 1e8)
            config.reserve_percent = float(self.reserve.text())
            config.num_offers = int(self.num_offers.currentText())
            intervals = [60, 300, 900, 1800, 3600]
            config.refresh_interval_sec = intervals[self.refresh_interval.currentIndex()]
            self.automaker.save_config()
            QMessageBox.information(self, 'Saved', 'Auto-Maker settings saved.')
        except Exception as e:
            QMessageBox.warning(self, 'Error', f'Invalid settings: {e}')

    def _toggle(self):
        if self.automaker.is_running():
            self.automaker.stop()
            QMessageBox.information(self, 'Stopped',
                'Auto-Maker stopped. No new offers will be created.')
        else:
            self._save_settings()
            if not self.automaker.config.btc_receive_address:
                QMessageBox.warning(
                    self, 'BTC Address Required',
                    'You must enter a Bitcoin receive address before starting '
                    'Auto-Maker.\n\nThis is where your BTC earnings will be paid.')
                return
            self.automaker.start()
            QMessageBox.information(self, 'Started',
                'Auto-Maker started! Your wallet is now a market maker.\n\n'
                'Offers will be created and refreshed automatically based on\n'
                'the live MARS/BTC price from price.marscoin.org.')
        self.accept()


class BtcPaymentDialog(QDialog):
    """Shows the BTC HTLC address with QR code for the taker to send BTC.

    Monitors mempool.space in the background to detect when the user
    has actually sent the BTC, then updates the UI live without the
    user needing to refresh anything.
    """

    # Signals from the background polling thread
    payment_detected = None  # set in __init__

    def __init__(self, parent, swap: SwapData):
        QDialog.__init__(self, parent)
        self.swap = swap
        self.setWindowTitle('Send BTC to Complete Swap')
        self.setMinimumWidth(500)
        self._poll_thread = None
        self._setup_ui()
        self._start_monitoring()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        header = QLabel('Send BTC to this address')
        header.setFont(QFont('', 14, QFont.Bold))
        header.setAlignment(Qt.AlignCenter)
        layout.addWidget(header)

        # QR Code
        try:
            import qrcode
            from PyQt5.QtGui import QPixmap, QImage
            from io import BytesIO

            qr = qrcode.QRCode(version=1, box_size=6, border=2)
            qr.add_data(self.swap.btc_htlc_address)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")

            buffer = BytesIO()
            img.save(buffer, format='PNG')
            buffer.seek(0)

            qimage = QImage()
            qimage.loadFromData(buffer.read())
            pixmap = QPixmap.fromImage(qimage)

            qr_label = QLabel()
            qr_label.setPixmap(pixmap)
            qr_label.setAlignment(Qt.AlignCenter)
            layout.addWidget(qr_label)
        except Exception as e:
            layout.addWidget(QLabel(f'(QR unavailable: {e})'))

        # Address
        addr_label = QLabel(self.swap.btc_htlc_address or 'Address not generated')
        addr_label.setFont(QFont('Courier', 11))
        addr_label.setAlignment(Qt.AlignCenter)
        addr_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        addr_label.setStyleSheet("background: #f0f0f0; padding: 10px; border-radius: 5px;")
        layout.addWidget(addr_label)

        copy_btn = QPushButton('Copy Address')
        copy_btn.clicked.connect(lambda: self._copy(self.swap.btc_htlc_address))
        layout.addWidget(copy_btn)

        btc_amount = self.swap.btc_amount_sat / 1e8
        mars_amount = self.swap.mars_amount_sat / 1e8
        info = QLabel(
            f'\nSend exactly: {btc_amount:.8f} BTC\n'
            f'You will receive: {mars_amount:.4f} MARS\n\n'
            f'Timelock: ~6 hours (your BTC is refundable if swap fails)'
        )
        info.setAlignment(Qt.AlignCenter)
        layout.addWidget(info)

        # Live status bar — updates automatically as BTC arrives
        self.status_label = QLabel('\u23f3  Waiting for your Bitcoin payment...')
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setStyleSheet(
            "background: #fff3cd; color: #856404; "
            "padding: 10px; border: 1px solid #ffc107; "
            "border-radius: 5px; font-size: 13px; margin-top: 10px;")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.close_btn = QPushButton('Cancel — I haven\'t sent yet')
        self.close_btn.clicked.connect(self.reject)
        layout.addWidget(self.close_btn)

    def _start_monitoring(self):
        """Spawn background thread to poll mempool.space."""
        from PyQt5.QtCore import QThread, pyqtSignal

        class PollThread(QThread):
            status = pyqtSignal(str, int, int)  # state, confirmations, value_sat

            def __init__(self, address, expected_sat):
                super().__init__()
                self.address = address
                self.expected_sat = expected_sat
                self._stop = False

            def stop(self):
                self._stop = True

            def run(self):
                import time as _time
                import urllib.request as _ur
                import json as _json

                url = f'https://mempool.space/api/address/{self.address}/utxo'
                while not self._stop:
                    try:
                        req = _ur.Request(url, headers={
                            'User-Agent': 'Electrum-Mars/4.3.2'})
                        with _ur.urlopen(req, timeout=10) as resp:
                            utxos = _json.loads(resp.read())
                        if utxos:
                            # Find a UTXO matching our expected amount
                            for utxo in utxos:
                                value = utxo.get('value', 0)
                                if value >= self.expected_sat:
                                    status = utxo.get('status', {})
                                    if status.get('confirmed', False):
                                        bh = status.get('block_height', 0)
                                        # Approximate confirmations using a
                                        # quick tip query
                                        try:
                                            with _ur.urlopen(_ur.Request(
                                                'https://mempool.space/api/blocks/tip/height',
                                                headers={'User-Agent': 'Electrum-Mars/4.3.2'}),
                                                timeout=5) as r:
                                                tip = int(r.read())
                                            conf = max(1, tip - bh + 1)
                                        except Exception:
                                            conf = 1
                                        self.status.emit(
                                            'confirmed', conf, value)
                                    else:
                                        self.status.emit(
                                            'mempool', 0, value)
                                    break
                            else:
                                # UTXOs exist but none match amount
                                self.status.emit('wrong_amount', 0, 0)
                    except Exception:
                        pass  # network hiccup — try again next poll
                    # Sleep in small chunks so stop() is responsive
                    for _ in range(15):
                        if self._stop:
                            return
                        _time.sleep(1)

        self._poll_thread = PollThread(
            self.swap.btc_htlc_address, self.swap.btc_amount_sat)
        self._poll_thread.status.connect(self._on_status_update)
        self._poll_thread.start()

    def _on_status_update(self, state, confirmations, value_sat):
        """Update status label with live payment info."""
        btc = value_sat / 1e8
        if state == 'mempool':
            self.status_label.setText(
                f'\u2705  Payment detected in mempool!\n'
                f'Received: {btc:.8f} BTC — waiting for confirmation.\n'
                f'You can safely close this window. The swap will complete\n'
                f'automatically once the Bitcoin transaction confirms.')
            self.status_label.setStyleSheet(
                "background: #d4edda; color: #155724; "
                "padding: 10px; border: 1px solid #28a745; "
                "border-radius: 5px; font-size: 13px; margin-top: 10px;")
            self.close_btn.setText('Close')
            self.close_btn.setStyleSheet(
                "background-color: #28a745; color: white; padding: 10px;")
        elif state == 'confirmed':
            self.status_label.setText(
                f'\u2705  Confirmed on Bitcoin ({confirmations} '
                f'confirmation{"s" if confirmations != 1 else ""})!\n'
                f'Received: {btc:.8f} BTC\n'
                f'The swap will complete automatically now.')
            self.status_label.setStyleSheet(
                "background: #d4edda; color: #155724; "
                "padding: 10px; border: 1px solid #28a745; "
                "border-radius: 5px; font-size: 13px; margin-top: 10px;")
            self.close_btn.setText('Done')
            self.close_btn.setStyleSheet(
                "background-color: #28a745; color: white; padding: 10px;")
        elif state == 'wrong_amount':
            self.status_label.setText(
                f'\u26a0  Transaction detected but amount does not match.\n'
                f'Expected: {self.swap.btc_amount_sat/1e8:.8f} BTC\n'
                f'Please send the exact amount.')
            self.status_label.setStyleSheet(
                "background: #f8d7da; color: #721c24; "
                "padding: 10px; border: 1px solid #dc3545; "
                "border-radius: 5px; font-size: 13px; margin-top: 10px;")

    def closeEvent(self, event):
        if self._poll_thread:
            self._poll_thread.stop()
            self._poll_thread.wait(1000)
        super().closeEvent(event)

    def _copy(self, text):
        from PyQt5.QtWidgets import QApplication
        QApplication.clipboard().setText(text or '')
        QMessageBox.information(self, 'Copied', 'Address copied to clipboard!')


class BtcRefundDialog(QDialog):
    """Dialog for refunding a stuck BTC HTLC back to the user's Bitcoin wallet.

    Shows the swap details, asks where to send the refund, checks if the
    timelock has expired, and broadcasts the refund transaction via
    mempool.space.
    """

    def __init__(self, parent, swap: SwapData):
        QDialog.__init__(self, parent)
        self.parent_tab = parent
        self.swap = swap
        self.setWindowTitle('Refund BTC HTLC')
        self.setMinimumWidth(500)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        header = QLabel('Refund BTC from Stuck Swap')
        header.setFont(QFont('', 14, QFont.Bold))
        layout.addWidget(header)

        info = QLabel(
            f'Swap ID: {self.swap.swap_id[:12]}...\n'
            f'Locked BTC: {self.swap.btc_amount_sat/1e8:.8f} BTC\n'
            f'HTLC address: {self.swap.btc_htlc_address}\n'
            f'Locktime: Bitcoin block {self.swap.btc_locktime}\n\n'
            f'You can reclaim your BTC after the locktime expires.\n'
            f'Before that, Bitcoin nodes will reject the refund tx.'
        )
        info.setWordWrap(True)
        info.setStyleSheet("padding: 10px; background: #f8f9fa; border-radius: 5px;")
        layout.addWidget(info)

        form = QFormLayout()
        self.refund_addr_input = QLineEdit()
        self.refund_addr_input.setPlaceholderText('bc1q... (your BTC address)')
        # Try to reuse the saved BTC receive address from the wallet config
        try:
            saved = self.parent_tab.engine.wallet.config.get(
                'atomic_swap_btc_receive_addr', '')
            if saved:
                self.refund_addr_input.setText(saved)
        except Exception:
            pass
        form.addRow('Send refund to:', self.refund_addr_input)
        layout.addLayout(form)

        # Status label (will show timelock check result + broadcast result)
        self.status_label = QLabel('')
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet(
            "padding: 10px; margin-top: 10px; border-radius: 5px;")
        layout.addWidget(self.status_label)

        btn_layout = QHBoxLayout()
        check_btn = QPushButton('Check Timelock')
        check_btn.clicked.connect(self._check_timelock)
        btn_layout.addWidget(check_btn)

        self.refund_btn = QPushButton('Broadcast Refund')
        self.refund_btn.setStyleSheet(
            "background-color: #e67e22; color: white; font-weight: bold; padding: 10px;")
        self.refund_btn.clicked.connect(self._do_refund)
        btn_layout.addWidget(self.refund_btn)

        close_btn = QPushButton('Close')
        close_btn.clicked.connect(self.reject)
        btn_layout.addWidget(close_btn)

        layout.addLayout(btn_layout)

    def _check_timelock(self):
        """Query mempool.space for current block height and show status."""
        import urllib.request
        try:
            req = urllib.request.Request(
                'https://mempool.space/api/blocks/tip/height',
                headers={'User-Agent': 'Electrum-Mars/4.3.2'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                current_height = int(resp.read())
        except Exception as e:
            self.status_label.setText(f'\u26a0 Could not fetch height: {e}')
            self.status_label.setStyleSheet(
                "background: #f8d7da; color: #721c24; padding: 10px; border-radius: 5px;")
            return

        locktime = self.swap.btc_locktime
        if current_height >= locktime:
            blocks_past = current_height - locktime
            self.status_label.setText(
                f'\u2705  Locktime expired — you can refund now.\n'
                f'Current block: {current_height}\n'
                f'Locktime: {locktime}\n'
                f'Blocks past: {blocks_past}')
            self.status_label.setStyleSheet(
                "background: #d4edda; color: #155724; padding: 10px; border-radius: 5px;")
        else:
            remaining = locktime - current_height
            eta_min = remaining * 10
            hours = eta_min // 60
            mins = eta_min % 60
            self.status_label.setText(
                f'\u23f3  Not yet refundable.\n'
                f'Current block: {current_height}\n'
                f'Locktime: {locktime}\n'
                f'Blocks remaining: {remaining} '
                f'(approx {hours}h {mins}m)')
            self.status_label.setStyleSheet(
                "background: #fff3cd; color: #856404; padding: 10px; border-radius: 5px;")

    def _do_refund(self):
        refund_addr = self.refund_addr_input.text().strip()
        if not refund_addr:
            QMessageBox.warning(self, 'Error',
                                'Please enter a BTC refund address.')
            return
        if not (refund_addr.startswith('bc1')
                or refund_addr.startswith('1')
                or refund_addr.startswith('3')):
            QMessageBox.warning(self, 'Error',
                                'Invalid BTC address format.')
            return

        reply = QMessageBox.question(
            self, 'Confirm Refund',
            f'Broadcast BTC refund transaction?\n\n'
            f'Refund to: {refund_addr}\n'
            f'Amount: {self.swap.btc_amount_sat/1e8:.8f} BTC (minus network fee)\n\n'
            f'This action is final once broadcast.',
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No)
        if reply != QMessageBox.Yes:
            return

        network = self.parent_tab.engine.network
        if not network:
            QMessageBox.warning(self, 'Error', 'Not connected to network.')
            return

        try:
            async def do_refund():
                return await self.parent_tab.engine.refund_btc_htlc(
                    self.swap.swap_id, refund_addr)
            txid = network.run_from_another_thread(do_refund())
            self.status_label.setText(
                f'\u2705  Refund broadcast!\n\n'
                f'Tx ID: {txid}\n\n'
                f'Check it on mempool.space:\n'
                f'https://mempool.space/tx/{txid}')
            self.status_label.setStyleSheet(
                "background: #d4edda; color: #155724; padding: 10px; border-radius: 5px;")
            self.refund_btn.setEnabled(False)
        except Exception as e:
            QMessageBox.warning(self, 'Refund Failed', str(e))
