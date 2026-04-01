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
    QProgressBar, QDialog, QDialogButtonBox, QComboBox,
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QFont

from electrum_mars.plugin import BasePlugin, hook
from electrum_mars.i18n import _
from electrum_mars.util import format_satoshis
from electrum_mars.logging import get_logger

from .swap_engine import SwapEngine, SwapData, SwapState, SwapRole
from .orderbook import OrderBook, SwapOffer

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
        window.tabs.addTab(tab, _('Atomic Swap'))
        self.windows[wallet] = tab

    @hook
    def on_close_window(self, window: 'ElectrumWindow'):
        wallet = window.wallet
        if wallet in self.windows:
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

        self._setup_ui()
        self._start_refresh_timer()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        # Header
        header = QLabel(_('Atomic Swaps — Trade BTC for MARS, Peer-to-Peer'))
        header.setFont(QFont('', 14, QFont.Bold))
        layout.addWidget(header)

        desc = QLabel(_('No exchange needed. Trustless settlement via hash time-locked contracts.'))
        desc.setStyleSheet("color: gray;")
        layout.addWidget(desc)

        # Action buttons
        btn_layout = QHBoxLayout()
        self.buy_btn = QPushButton(_('Buy MARS with BTC'))
        self.buy_btn.clicked.connect(self._on_buy_mars)
        self.buy_btn.setStyleSheet("font-size: 14px; padding: 10px; background-color: #c0392b; color: white;")
        btn_layout.addWidget(self.buy_btn)

        self.sell_btn = QPushButton(_('Sell MARS for BTC'))
        self.sell_btn.clicked.connect(self._on_sell_mars)
        self.sell_btn.setStyleSheet("font-size: 14px; padding: 10px;")
        btn_layout.addWidget(self.sell_btn)

        self.refresh_btn = QPushButton(_('Refresh Offers'))
        self.refresh_btn.clicked.connect(self._refresh_offers)
        btn_layout.addWidget(self.refresh_btn)
        layout.addLayout(btn_layout)

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

        self.offers_table = QTableWidget()
        self.offers_table.setColumnCount(5)
        self.offers_table.setHorizontalHeaderLabels([
            _('MARS Amount'), _('BTC Amount'), _('Rate (BTC/MARS)'),
            _('Maker'), _('Action'),
        ])
        self.offers_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.Stretch)
        self.offers_table.setSelectionBehavior(QTableWidget.SelectRows)
        layout.addWidget(self.offers_table)

        return w

    def _create_active_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        self.active_table = QTableWidget()
        self.active_table.setColumnCount(6)
        self.active_table.setHorizontalHeaderLabels([
            _('Swap ID'), _('Role'), _('MARS'), _('BTC'),
            _('Status'), _('Time'),
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
            _('Swap ID'), _('Role'), _('MARS'), _('BTC'),
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
        self.timer.start(30000)  # refresh every 30 seconds

    def _refresh_all(self):
        self._refresh_offers()
        self._refresh_active()
        self._refresh_history()

    def _refresh_offers(self):
        offers = self.orderbook.get_offers()
        self.offers_table.setRowCount(len(offers))
        for i, offer in enumerate(offers):
            self.offers_table.setItem(i, 0, QTableWidgetItem(
                f'{offer.mars_amount:.4f}'))
            self.offers_table.setItem(i, 1, QTableWidgetItem(
                f'{offer.btc_amount:.8f}'))
            self.offers_table.setItem(i, 2, QTableWidgetItem(
                f'{offer.rate:.8f}'))
            self.offers_table.setItem(i, 3, QTableWidgetItem(
                offer.maker_address[:12] + '...' if offer.maker_address else '?'))

            accept_btn = QPushButton(_('Accept'))
            accept_btn.clicked.connect(lambda _, o=offer: self._accept_offer(o))
            self.offers_table.setCellWidget(i, 4, accept_btn)

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
            self.active_table.setItem(i, 4, QTableWidgetItem(
                summary['state'].replace('_', ' ').upper()))
            self.active_table.setItem(i, 5, QTableWidgetItem(
                f"{summary['age_minutes']}m ago"))

    def _refresh_history(self):
        swaps = self.engine.get_all_swaps()
        terminal = {SwapState.COMPLETED.value, SwapState.FAILED.value,
                    SwapState.EXPIRED.value, SwapState.MARS_REFUNDED.value,
                    SwapState.BTC_REFUNDED.value}
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
        msg = _(
            'Accept this swap offer?\n\n'
            f'You send: {offer.btc_amount:.8f} BTC\n'
            f'You receive: {offer.mars_amount:.4f} MARS\n'
            f'Rate: {offer.rate:.8f} BTC/MARS\n\n'
            'You will need to send BTC to a generated HTLC address. '
            'The swap will complete automatically once confirmed.'
        )
        reply = QMessageBox.question(self, _('Accept Offer'), msg,
                                     QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes:
            return

        # TODO: Get current BTC block height from mempool.space
        # For now use a placeholder
        btc_height = 850000

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

        QMessageBox.information(self, _('Swap Created'),
            _(f'Swap initiated!\n\n'
              f'Send exactly {offer.btc_amount:.8f} BTC to:\n'
              f'{swap.btc_htlc_address}\n\n'
              f'The swap will complete automatically once your BTC '
              f'is confirmed and the maker claims it.'))

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
        self.setWindowTitle(_('Create Swap Offer'))
        self.setMinimumWidth(400)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        form = QFormLayout()

        self.mars_amount = QLineEdit()
        self.mars_amount.setPlaceholderText('100')
        form.addRow(_('MARS to sell:'), self.mars_amount)

        self.btc_amount = QLineEdit()
        self.btc_amount.setPlaceholderText('0.001')
        form.addRow(_('BTC to receive:'), self.btc_amount)

        self.timeout_hours = QComboBox()
        self.timeout_hours.addItems(['2 hours', '4 hours', '6 hours', '12 hours'])
        self.timeout_hours.setCurrentIndex(1)
        form.addRow(_('Offer valid for:'), self.timeout_hours)

        layout.addLayout(form)

        # Info label
        self.info_label = QLabel('')
        self.mars_amount.textChanged.connect(self._update_info)
        self.btc_amount.textChanged.connect(self._update_info)
        layout.addWidget(self.info_label)

        # Buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._create_offer)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _update_info(self):
        try:
            mars = float(self.mars_amount.text() or '0')
            btc = float(self.btc_amount.text() or '0')
            if mars > 0 and btc > 0:
                rate = btc / mars
                self.info_label.setText(
                    f'Rate: {rate:.8f} BTC/MARS')
            else:
                self.info_label.setText('')
        except ValueError:
            self.info_label.setText('')

    def _create_offer(self):
        try:
            mars_sat = int(float(self.mars_amount.text()) * 1e8)
            btc_sat = int(float(self.btc_amount.text()) * 1e8)
        except ValueError:
            QMessageBox.warning(self, _('Error'), _('Invalid amounts'))
            return

        if mars_sat <= 0 or btc_sat <= 0:
            QMessageBox.warning(self, _('Error'), _('Amounts must be positive'))
            return

        # Get current Marscoin block height
        current_height = self.engine.network.blockchain().height() if self.engine.network else 0

        # Create the swap
        swap = self.engine.create_maker_swap(
            mars_amount_sat=mars_sat,
            btc_amount_sat=btc_sat,
            current_mars_height=current_height,
        )

        # Create the offer for the order book
        timeout_map = {0: 2, 1: 4, 2: 6, 3: 12}
        hours = timeout_map.get(self.timeout_hours.currentIndex(), 4)

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
            expires_at=time.time() + hours * 3600,
            maker_address=self.engine.wallet.get_receiving_address(),
        )
        self.orderbook.add_my_offer(offer)

        QMessageBox.information(self, _('Offer Created'),
            _(f'Swap offer created!\n\n'
              f'Selling: {mars_sat/1e8:.4f} MARS\n'
              f'For: {btc_sat/1e8:.8f} BTC\n\n'
              f'Share the offer JSON from the Manual Exchange tab, '
              f'or wait for someone to accept it.'))

        self.accept()
