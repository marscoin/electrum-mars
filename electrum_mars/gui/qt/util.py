import asyncio
import os.path
import time
import sys
import platform
import queue
import traceback
import os
import webbrowser
from decimal import Decimal
from functools import partial, lru_cache
from typing import (NamedTuple, Callable, Optional, TYPE_CHECKING, Union, List, Dict, Any,
                    Sequence, Iterable)

from PyQt5.QtGui import (QFont, QColor, QCursor, QPixmap, QStandardItem, QImage,
                         QPalette, QIcon, QFontMetrics, QShowEvent, QPainter, QHelpEvent)
from PyQt5.QtCore import (Qt, QPersistentModelIndex, QModelIndex, pyqtSignal,
                          QCoreApplication, QItemSelectionModel, QThread,
                          QSortFilterProxyModel, QSize, QLocale, QAbstractItemModel,
                          QEvent, QRect, QPoint, QObject)
from PyQt5.QtWidgets import (QPushButton, QLabel, QMessageBox, QHBoxLayout,
                             QAbstractItemView, QVBoxLayout, QLineEdit,
                             QStyle, QDialog, QGroupBox, QButtonGroup, QRadioButton,
                             QFileDialog, QWidget, QToolButton, QTreeView, QPlainTextEdit,
                             QHeaderView, QApplication, QToolTip, QTreeWidget, QStyledItemDelegate,
                             QMenu, QStyleOptionViewItem, QLayout, QLayoutItem,
                             QGraphicsEffect, QGraphicsScene, QGraphicsPixmapItem)

from electrum_mars.i18n import _, languages
from electrum_mars.util import FileImportFailed, FileExportFailed, make_aiohttp_session, resource_path
from electrum_mars.invoices import PR_UNPAID, PR_PAID, PR_EXPIRED, PR_INFLIGHT, PR_UNKNOWN, PR_FAILED, PR_ROUTING, PR_UNCONFIRMED

if TYPE_CHECKING:
    from .main_window import ElectrumWindow
    from .installwizard import InstallWizard
    from electrum_mars.simple_config import SimpleConfig


if platform.system() == 'Windows':
    MONOSPACE_FONT = 'Lucida Console'
elif platform.system() == 'Darwin':
    MONOSPACE_FONT = 'Monaco'
else:
    MONOSPACE_FONT = 'monospace'


dialogs = []

pr_icons = {
    PR_UNKNOWN:"warning.png",
    PR_UNPAID:"unpaid.png",
    PR_PAID:"confirmed.png",
    PR_EXPIRED:"expired.png",
    PR_INFLIGHT:"unconfirmed.png",
    PR_FAILED:"warning.png",
    PR_ROUTING:"unconfirmed.png",
    PR_UNCONFIRMED:"unconfirmed.png",
}


# filter tx files in QFileDialog:
TRANSACTION_FILE_EXTENSION_FILTER_ANY = "Transaction (*.txn *.psbt);;All files (*)"
TRANSACTION_FILE_EXTENSION_FILTER_ONLY_PARTIAL_TX = "Partial Transaction (*.psbt)"
TRANSACTION_FILE_EXTENSION_FILTER_ONLY_COMPLETE_TX = "Complete Transaction (*.txn)"
TRANSACTION_FILE_EXTENSION_FILTER_SEPARATE = (f"{TRANSACTION_FILE_EXTENSION_FILTER_ONLY_PARTIAL_TX};;"
                                              f"{TRANSACTION_FILE_EXTENSION_FILTER_ONLY_COMPLETE_TX};;"
                                              f"All files (*)")


class EnterButton(QPushButton):
    def __init__(self, text, func):
        QPushButton.__init__(self, text)
        self.func = func
        self.clicked.connect(func)

    def keyPressEvent(self, e):
        if e.key() in [Qt.Key_Return, Qt.Key_Enter]:
            self.func()


class ThreadedButton(QPushButton):
    def __init__(self, text, task, on_success=None, on_error=None):
        QPushButton.__init__(self, text)
        self.task = task
        self.on_success = on_success
        self.on_error = on_error
        self.clicked.connect(self.run_task)

    def run_task(self):
        self.setEnabled(False)
        self.thread = TaskThread(self)
        self.thread.add(self.task, self.on_success, self.done, self.on_error)

    def done(self):
        self.setEnabled(True)
        self.thread.stop()


class WWLabel(QLabel):
    def __init__ (self, text="", parent=None):
        QLabel.__init__(self, text, parent)
        self.setWordWrap(True)
        self.setTextInteractionFlags(Qt.TextSelectableByMouse)


class HelpLabel(QLabel):

    def __init__(self, text, help_text):
        QLabel.__init__(self, text)
        self.help_text = help_text
        self.app = QCoreApplication.instance()
        self.font = QFont()

    def mouseReleaseEvent(self, x):
        custom_message_box(icon=QMessageBox.Information,
                           parent=self,
                           title=_('Help'),
                           text=self.help_text)

    def enterEvent(self, event):
        self.font.setUnderline(True)
        self.setFont(self.font)
        self.app.setOverrideCursor(QCursor(Qt.PointingHandCursor))
        return QLabel.enterEvent(self, event)

    def leaveEvent(self, event):
        self.font.setUnderline(False)
        self.setFont(self.font)
        self.app.setOverrideCursor(QCursor(Qt.ArrowCursor))
        return QLabel.leaveEvent(self, event)


class HelpButton(QToolButton):
    def __init__(self, text):
        QToolButton.__init__(self)
        self.setText('?')
        self.help_text = text
        self.setFocusPolicy(Qt.NoFocus)
        self.setFixedWidth(round(2.2 * char_width_in_lineedit()))
        self.clicked.connect(self.onclick)

    def onclick(self):
        custom_message_box(icon=QMessageBox.Information,
                           parent=self,
                           title=_('Help'),
                           text=self.help_text,
                           rich_text=True)


class InfoButton(QPushButton):
    def __init__(self, text):
        QPushButton.__init__(self, 'Info')
        self.help_text = text
        self.setFocusPolicy(Qt.NoFocus)
        self.setFixedWidth(6 * char_width_in_lineedit())
        self.clicked.connect(self.onclick)

    def onclick(self):
        custom_message_box(icon=QMessageBox.Information,
                           parent=self,
                           title=_('Info'),
                           text=self.help_text,
                           rich_text=True)


class Buttons(QHBoxLayout):
    def __init__(self, *buttons):
        QHBoxLayout.__init__(self)
        self.addStretch(1)
        for b in buttons:
            if b is None:
                continue
            self.addWidget(b)

class CloseButton(QPushButton):
    def __init__(self, dialog):
        QPushButton.__init__(self, _("Close"))
        self.clicked.connect(dialog.close)
        self.setDefault(True)

class CopyButton(QPushButton):
    def __init__(self, text_getter, app):
        QPushButton.__init__(self, _("Copy"))
        self.clicked.connect(lambda: app.clipboard().setText(text_getter()))

class CopyCloseButton(QPushButton):
    def __init__(self, text_getter, app, dialog):
        QPushButton.__init__(self, _("Copy and Close"))
        self.clicked.connect(lambda: app.clipboard().setText(text_getter()))
        self.clicked.connect(dialog.close)
        self.setDefault(True)

class OkButton(QPushButton):
    def __init__(self, dialog, label=None):
        QPushButton.__init__(self, label or _("OK"))
        self.clicked.connect(dialog.accept)
        self.setDefault(True)

class CancelButton(QPushButton):
    def __init__(self, dialog, label=None):
        QPushButton.__init__(self, label or _("Cancel"))
        self.clicked.connect(dialog.reject)

class MessageBoxMixin(object):
    def top_level_window_recurse(self, window=None, test_func=None):
        window = window or self
        classes = (WindowModalDialog, QMessageBox)
        if test_func is None:
            test_func = lambda x: True
        for n, child in enumerate(window.children()):
            # Test for visibility as old closed dialogs may not be GC-ed.
            # Only accept children that confirm to test_func.
            if isinstance(child, classes) and child.isVisible() \
                    and test_func(child):
                return self.top_level_window_recurse(child, test_func=test_func)
        return window

    def top_level_window(self, test_func=None):
        return self.top_level_window_recurse(test_func)

    def question(self, msg, parent=None, title=None, icon=None, **kwargs) -> bool:
        Yes, No = QMessageBox.Yes, QMessageBox.No
        return Yes == self.msg_box(icon=icon or QMessageBox.Question,
                                   parent=parent,
                                   title=title or '',
                                   text=msg,
                                   buttons=Yes|No,
                                   defaultButton=No,
                                   **kwargs)

    def show_warning(self, msg, parent=None, title=None, **kwargs):
        return self.msg_box(QMessageBox.Warning, parent,
                            title or _('Warning'), msg, **kwargs)

    def show_error(self, msg, parent=None, **kwargs):
        return self.msg_box(QMessageBox.Warning, parent,
                            _('Error'), msg, **kwargs)

    def show_critical(self, msg, parent=None, title=None, **kwargs):
        return self.msg_box(QMessageBox.Critical, parent,
                            title or _('Critical Error'), msg, **kwargs)

    def show_message(self, msg, parent=None, title=None, **kwargs):
        return self.msg_box(QMessageBox.Information, parent,
                            title or _('Information'), msg, **kwargs)

    def msg_box(self, icon, parent, title, text, *, buttons=QMessageBox.Ok,
                defaultButton=QMessageBox.NoButton, rich_text=False,
                checkbox=None):
        parent = parent or self.top_level_window()
        return custom_message_box(icon=icon,
                                  parent=parent,
                                  title=title,
                                  text=text,
                                  buttons=buttons,
                                  defaultButton=defaultButton,
                                  rich_text=rich_text,
                                  checkbox=checkbox)


def custom_message_box(*, icon, parent, title, text, buttons=QMessageBox.Ok,
                       defaultButton=QMessageBox.NoButton, rich_text=False,
                       checkbox=None):
    if type(icon) is QPixmap:
        d = QMessageBox(QMessageBox.Information, title, str(text), buttons, parent)
        d.setIconPixmap(icon)
    else:
        d = QMessageBox(icon, title, str(text), buttons, parent)
    d.setWindowModality(Qt.WindowModal)
    d.setDefaultButton(defaultButton)
    if rich_text:
        d.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse)
        # set AutoText instead of RichText
        # AutoText lets Qt figure out whether to render as rich text.
        # e.g. if text is actually plain text and uses "\n" newlines;
        #      and we set RichText here, newlines would be swallowed
        d.setTextFormat(Qt.AutoText)
    else:
        d.setTextInteractionFlags(Qt.TextSelectableByMouse)
        d.setTextFormat(Qt.PlainText)
    if checkbox is not None:
        d.setCheckBox(checkbox)
    return d.exec_()


class WindowModalDialog(QDialog, MessageBoxMixin):
    '''Handy wrapper; window modal dialogs are better for our multi-window
    daemon model as other wallet windows can still be accessed.'''
    def __init__(self, parent, title=None):
        QDialog.__init__(self, parent)
        self.setWindowModality(Qt.WindowModal)
        if title:
            self.setWindowTitle(title)


class WaitingDialog(WindowModalDialog):
    '''Shows a please wait dialog whilst running a task.  It is not
    necessary to maintain a reference to this dialog.'''
    def __init__(self, parent: QWidget, message: str, task, on_success=None, on_error=None):
        assert parent
        if isinstance(parent, MessageBoxMixin):
            parent = parent.top_level_window()
        WindowModalDialog.__init__(self, parent, _("Please wait"))
        self.message_label = QLabel(message)
        vbox = QVBoxLayout(self)
        vbox.addWidget(self.message_label)
        self.accepted.connect(self.on_accepted)
        self.show()
        self.thread = TaskThread(self)
        self.thread.finished.connect(self.deleteLater)  # see #3956
        self.thread.add(task, on_success, self.accept, on_error)

    def wait(self):
        self.thread.wait()

    def on_accepted(self):
        self.thread.stop()

    def update(self, msg):
        print(msg)
        self.message_label.setText(msg)


class BlockingWaitingDialog(WindowModalDialog):
    """Shows a waiting dialog whilst running a task.
    Should be called from the GUI thread. The GUI thread will be blocked while
    the task is running; the point of the dialog is to provide feedback
    to the user regarding what is going on.
    """
    def __init__(self, parent: QWidget, message: str, task: Callable[[], Any]):
        assert parent
        if isinstance(parent, MessageBoxMixin):
            parent = parent.top_level_window()
        WindowModalDialog.__init__(self, parent, _("Please wait"))
        self.message_label = QLabel(message)
        vbox = QVBoxLayout(self)
        vbox.addWidget(self.message_label)
        # show popup
        self.show()
        # refresh GUI; needed for popup to appear and for message_label to get drawn
        QCoreApplication.processEvents()
        QCoreApplication.processEvents()
        # block and run given task
        task()
        # close popup
        self.accept()


def line_dialog(parent, title, label, ok_label, default=None):
    dialog = WindowModalDialog(parent, title)
    dialog.setMinimumWidth(500)
    l = QVBoxLayout()
    dialog.setLayout(l)
    l.addWidget(QLabel(label))
    txt = QLineEdit()
    if default:
        txt.setText(default)
    l.addWidget(txt)
    l.addLayout(Buttons(CancelButton(dialog), OkButton(dialog, ok_label)))
    if dialog.exec_():
        return txt.text()

def text_dialog(
        *,
        parent,
        title,
        header_layout,
        ok_label,
        default=None,
        allow_multi=False,
        config: 'SimpleConfig',
):
    from .qrtextedit import ScanQRTextEdit
    dialog = WindowModalDialog(parent, title)
    dialog.setMinimumWidth(600)
    l = QVBoxLayout()
    dialog.setLayout(l)
    if isinstance(header_layout, str):
        l.addWidget(QLabel(header_layout))
    else:
        l.addLayout(header_layout)
    txt = ScanQRTextEdit(allow_multi=allow_multi, config=config)
    if default:
        txt.setText(default)
    l.addWidget(txt)
    l.addLayout(Buttons(CancelButton(dialog), OkButton(dialog, ok_label)))
    if dialog.exec_():
        return txt.toPlainText()

class ChoicesLayout(object):
    def __init__(self, msg, choices, on_clicked=None, checked_index=0):
        vbox = QVBoxLayout()
        if len(msg) > 50:
            vbox.addWidget(WWLabel(msg))
            msg = ""
        gb2 = QGroupBox(msg)
        vbox.addWidget(gb2)

        vbox2 = QVBoxLayout()
        gb2.setLayout(vbox2)

        self.group = group = QButtonGroup()
        for i,c in enumerate(choices):
            button = QRadioButton(gb2)
            button.setText(c)
            vbox2.addWidget(button)
            group.addButton(button)
            group.setId(button, i)
            if i==checked_index:
                button.setChecked(True)

        if on_clicked:
            group.buttonClicked.connect(partial(on_clicked, self))

        self.vbox = vbox

    def layout(self):
        return self.vbox

    def selected_index(self):
        return self.group.checkedId()

def address_field(addresses):
    hbox = QHBoxLayout()
    address_e = QLineEdit()
    if addresses and len(addresses) > 0:
        address_e.setText(addresses[0])
    else:
        addresses = []
    def func():
        try:
            i = addresses.index(str(address_e.text())) + 1
            i = i % len(addresses)
            address_e.setText(addresses[i])
        except ValueError:
            # the user might have changed address_e to an
            # address not in the wallet (or to something that isn't an address)
            if addresses and len(addresses) > 0:
                address_e.setText(addresses[0])
    button = QPushButton(_('Address'))
    button.clicked.connect(func)
    hbox.addWidget(button)
    hbox.addWidget(address_e)
    return hbox, address_e


def filename_field(parent, config, defaultname, select_msg):

    vbox = QVBoxLayout()
    vbox.addWidget(QLabel(_("Format")))
    gb = QGroupBox("format", parent)
    b1 = QRadioButton(gb)
    b1.setText(_("CSV"))
    b1.setChecked(True)
    b2 = QRadioButton(gb)
    b2.setText(_("json"))
    vbox.addWidget(b1)
    vbox.addWidget(b2)

    hbox = QHBoxLayout()

    directory = config.get('io_dir', os.path.expanduser('~'))
    path = os.path.join(directory, defaultname)
    filename_e = QLineEdit()
    filename_e.setText(path)

    def func():
        text = filename_e.text()
        _filter = "*.csv" if defaultname.endswith(".csv") else "*.json" if defaultname.endswith(".json") else None
        p = getSaveFileName(
            parent=None,
            title=select_msg,
            filename=text,
            filter=_filter,
            config=config,
        )
        if p:
            filename_e.setText(p)

    button = QPushButton(_('File'))
    button.clicked.connect(func)
    hbox.addWidget(button)
    hbox.addWidget(filename_e)
    vbox.addLayout(hbox)

    def set_csv(v):
        text = filename_e.text()
        text = text.replace(".json",".csv") if v else text.replace(".csv",".json")
        filename_e.setText(text)

    b1.clicked.connect(lambda: set_csv(True))
    b2.clicked.connect(lambda: set_csv(False))

    return vbox, filename_e, b1


class ElectrumItemDelegate(QStyledItemDelegate):
    def __init__(self, tv: 'MyTreeView'):
        super().__init__(tv)
        self.tv = tv
        self.opened = None
        def on_closeEditor(editor: QLineEdit, hint):
            self.opened = None
            self.tv.is_editor_open = False
            if self.tv._pending_update:
                self.tv.update()
        def on_commitData(editor: QLineEdit):
            new_text = editor.text()
            idx = QModelIndex(self.opened)
            row, col = idx.row(), idx.column()
            edit_key = self.tv.get_edit_key_from_coordinate(row, col)
            assert edit_key is not None, (idx.row(), idx.column())
            self.tv.on_edited(idx, edit_key=edit_key, text=new_text)
        self.closeEditor.connect(on_closeEditor)
        self.commitData.connect(on_commitData)

    def createEditor(self, parent, option, idx):
        self.opened = QPersistentModelIndex(idx)
        self.tv.is_editor_open = True
        return super().createEditor(parent, option, idx)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, idx: QModelIndex) -> None:
        custom_data = idx.data(MyTreeView.ROLE_CUSTOM_PAINT)
        if custom_data is None:
            return super().paint(painter, option, idx)
        else:
            # let's call the default paint method first; to paint the background (e.g. selection)
            super().paint(painter, option, idx)
            # and now paint on top of that
            custom_data.paint(painter, option.rect)

    def helpEvent(self, evt: QHelpEvent, view: QAbstractItemView, option: QStyleOptionViewItem, idx: QModelIndex) -> bool:
        custom_data = idx.data(MyTreeView.ROLE_CUSTOM_PAINT)
        if custom_data is None:
            return super().helpEvent(evt, view, option, idx)
        else:
            if evt.type() == QEvent.ToolTip:
                if custom_data.show_tooltip(evt):
                    return True
        return super().helpEvent(evt, view, option, idx)

    def sizeHint(self, option: QStyleOptionViewItem, idx: QModelIndex) -> QSize:
        custom_data = idx.data(MyTreeView.ROLE_CUSTOM_PAINT)
        if custom_data is None:
            return super().sizeHint(option, idx)
        else:
            default_size = super().sizeHint(option, idx)
            return custom_data.sizeHint(default_size)


class MyTreeView(QTreeView):
    ROLE_CLIPBOARD_DATA = Qt.UserRole + 100
    ROLE_CUSTOM_PAINT   = Qt.UserRole + 101
    ROLE_EDIT_KEY       = Qt.UserRole + 102
    ROLE_FILTER_DATA    = Qt.UserRole + 103

    filter_columns: Iterable[int]

    def __init__(self, parent: 'ElectrumWindow', create_menu, *,
                 stretch_column=None, editable_columns=None):
        super().__init__(parent)
        self.parent = parent
        self.config = self.parent.config
        self.stretch_column = stretch_column
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(create_menu)
        self.setUniformRowHeights(True)

        # Control which columns are editable
        if editable_columns is None:
            editable_columns = []
        self.editable_columns = set(editable_columns)
        self.setItemDelegate(ElectrumItemDelegate(self))
        self.current_filter = ""
        self.is_editor_open = False

        self.setRootIsDecorated(False)  # remove left margin
        self.toolbar_shown = False

        # When figuring out the size of columns, Qt by default looks at
        # the first 1000 rows (at least if resize mode is QHeaderView.ResizeToContents).
        # This would be REALLY SLOW, and it's not perfect anyway.
        # So to speed the UI up considerably, set it to
        # only look at as many rows as currently visible.
        self.header().setResizeContentsPrecision(0)

        self._pending_update = False
        self._forced_update = False

    def set_editability(self, items):
        for idx, i in enumerate(items):
            i.setEditable(idx in self.editable_columns)

    def selected_in_column(self, column: int):
        items = self.selectionModel().selectedIndexes()
        return list(x for x in items if x.column() == column)

    def get_role_data_for_current_item(self, *, col, role) -> Any:
        idx = self.selectionModel().currentIndex()
        idx = idx.sibling(idx.row(), col)
        item = self.item_from_index(idx)
        if item:
            return item.data(role)

    def item_from_index(self, idx: QModelIndex) -> Optional[QStandardItem]:
        model = self.model()
        if isinstance(model, QSortFilterProxyModel):
            idx = model.mapToSource(idx)
            return model.sourceModel().itemFromIndex(idx)
        else:
            return model.itemFromIndex(idx)

    def original_model(self) -> QAbstractItemModel:
        model = self.model()
        if isinstance(model, QSortFilterProxyModel):
            return model.sourceModel()
        else:
            return model

    def set_current_idx(self, set_current: QPersistentModelIndex):
        if set_current:
            assert isinstance(set_current, QPersistentModelIndex)
            assert set_current.isValid()
            self.selectionModel().select(QModelIndex(set_current), QItemSelectionModel.SelectCurrent)

    def update_headers(self, headers: Union[List[str], Dict[int, str]]):
        # headers is either a list of column names, or a dict: (col_idx->col_name)
        if not isinstance(headers, dict):  # convert to dict
            headers = dict(enumerate(headers))
        col_names = [headers[col_idx] for col_idx in sorted(headers.keys())]
        self.original_model().setHorizontalHeaderLabels(col_names)
        self.header().setStretchLastSection(False)
        for col_idx in headers:
            sm = QHeaderView.Stretch if col_idx == self.stretch_column else QHeaderView.ResizeToContents
            self.header().setSectionResizeMode(col_idx, sm)

    def keyPressEvent(self, event):
        if self.itemDelegate().opened:
            return
        if event.key() in [Qt.Key_F2, Qt.Key_Return, Qt.Key_Enter]:
            self.on_activated(self.selectionModel().currentIndex())
            return
        super().keyPressEvent(event)

    def on_activated(self, idx):
        # on 'enter' we show the menu
        pt = self.visualRect(idx).bottomLeft()
        pt.setX(50)
        self.customContextMenuRequested.emit(pt)

    def edit(self, idx, trigger=QAbstractItemView.AllEditTriggers, event=None):
        """
        this is to prevent:
           edit: editing failed
        from inside qt
        """
        return super().edit(idx, trigger, event)

    def on_edited(self, idx: QModelIndex, edit_key, *, text: str) -> None:
        raise NotImplementedError()

    def should_hide(self, row):
        """
        row_num is for self.model(). So if there is a proxy, it is the row number
        in that!
        """
        return False

    def get_text_from_coordinate(self, row, col) -> str:
        idx = self.model().index(row, col)
        item = self.item_from_index(idx)
        return item.text()

    def get_role_data_from_coordinate(self, row, col, *, role) -> Any:
        idx = self.model().index(row, col)
        item = self.item_from_index(idx)
        role_data = item.data(role)
        return role_data

    def get_edit_key_from_coordinate(self, row, col) -> Any:
        # overriding this might allow avoiding storing duplicate data
        return self.get_role_data_from_coordinate(row, col, role=self.ROLE_EDIT_KEY)

    def get_filter_data_from_coordinate(self, row, col) -> str:
        filter_data = self.get_role_data_from_coordinate(row, col, role=self.ROLE_FILTER_DATA)
        if filter_data:
            return filter_data
        txt = self.get_text_from_coordinate(row, col)
        txt = txt.lower()
        return txt

    def hide_row(self, row_num):
        """
        row_num is for self.model(). So if there is a proxy, it is the row number
        in that!
        """
        should_hide = self.should_hide(row_num)
        if not self.current_filter and should_hide is None:
            # no filters at all, neither date nor search
            self.setRowHidden(row_num, QModelIndex(), False)
            return
        for column in self.filter_columns:
            filter_data = self.get_filter_data_from_coordinate(row_num, column)
            if self.current_filter in filter_data:
                # the filter matched, but the date filter might apply
                self.setRowHidden(row_num, QModelIndex(), bool(should_hide))
                break
        else:
            # we did not find the filter in any columns, hide the item
            self.setRowHidden(row_num, QModelIndex(), True)

    def filter(self, p=None):
        if p is not None:
            p = p.lower()
            self.current_filter = p
        self.hide_rows()

    def hide_rows(self):
        for row in range(self.model().rowCount()):
            self.hide_row(row)

    def create_toolbar(self, config=None):
        hbox = QHBoxLayout()
        buttons = self.get_toolbar_buttons()
        for b in buttons:
            b.setVisible(False)
            hbox.addWidget(b)
        hide_button = QPushButton('x')
        hide_button.setVisible(False)
        hide_button.pressed.connect(lambda: self.show_toolbar(False, config))
        self.toolbar_buttons = buttons + (hide_button,)
        hbox.addStretch()
        hbox.addWidget(hide_button)
        return hbox

    def save_toolbar_state(self, state, config):
        pass  # implemented in subclasses

    def show_toolbar(self, state, config=None):
        if state == self.toolbar_shown:
            return
        self.toolbar_shown = state
        if config:
            self.save_toolbar_state(state, config)
        for b in self.toolbar_buttons:
            b.setVisible(state)
        if not state:
            self.on_hide_toolbar()

    def toggle_toolbar(self, config=None):
        self.show_toolbar(not self.toolbar_shown, config)

    def add_copy_menu(self, menu: QMenu, idx) -> QMenu:
        cc = menu.addMenu(_("Copy"))
        for column in self.Columns:
            column_title = self.original_model().horizontalHeaderItem(column).text()
            if not column_title:
                continue
            item_col = self.item_from_index(idx.sibling(idx.row(), column))
            clipboard_data = item_col.data(self.ROLE_CLIPBOARD_DATA)
            if clipboard_data is None:
                clipboard_data = item_col.text().strip()
            cc.addAction(column_title,
                         lambda text=clipboard_data, title=column_title:
                         self.place_text_on_clipboard(text, title=title))
        return cc

    def place_text_on_clipboard(self, text: str, *, title: str = None) -> None:
        self.parent.do_copy(text, title=title)

    def showEvent(self, e: 'QShowEvent'):
        super().showEvent(e)
        if e.isAccepted() and self._pending_update:
            self._forced_update = True
            self.update()
            self._forced_update = False

    def maybe_defer_update(self) -> bool:
        """Returns whether we should defer an update/refresh."""
        defer = (not self._forced_update
                 and (not self.isVisible() or self.is_editor_open))
        # side-effect: if we decide to defer update, the state will become stale:
        self._pending_update = defer
        return defer


class MySortModel(QSortFilterProxyModel):
    def __init__(self, parent, *, sort_role):
        super().__init__(parent)
        self._sort_role = sort_role

    def lessThan(self, source_left: QModelIndex, source_right: QModelIndex):
        item1 = self.sourceModel().itemFromIndex(source_left)
        item2 = self.sourceModel().itemFromIndex(source_right)
        data1 = item1.data(self._sort_role)
        data2 = item2.data(self._sort_role)
        if data1 is not None and data2 is not None:
            return data1 < data2
        v1 = item1.text()
        v2 = item2.text()
        try:
            return Decimal(v1) < Decimal(v2)
        except:
            return v1 < v2


class ButtonsWidget(QWidget):

    def __init__(self):
        super(QWidget, self).__init__()
        self.buttons = []  # type: List[QToolButton]

    def resizeButtons(self):
        frameWidth = self.style().pixelMetric(QStyle.PM_DefaultFrameWidth)
        x = self.rect().right() - frameWidth - 10
        y = self.rect().bottom() - frameWidth
        for button in self.buttons:
            sz = button.sizeHint()
            x -= sz.width()
            button.move(x, y - sz.height())

    def addButton(self, icon_name, on_click, tooltip):
        button = QToolButton(self)
        button.setIcon(read_QIcon(icon_name))
        button.setIconSize(QSize(25,25))
        button.setCursor(QCursor(Qt.PointingHandCursor))
        button.setStyleSheet("QToolButton { border: none; hover {border: 1px} pressed {border: 1px} padding: 0px; }")
        button.setVisible(True)
        button.setToolTip(tooltip)
        button.clicked.connect(on_click)
        self.buttons.append(button)
        return button

    def addCopyButton(self, app):
        self.app = app
        self.addButton("copy.png", self.on_copy, _("Copy to clipboard"))

    def on_copy(self):
        self.app.clipboard().setText(self.text())
        QToolTip.showText(QCursor.pos(), _("Text copied to clipboard"), self)

    def addPasteButton(self, app):
        self.app = app
        self.addButton("copy.png", self.on_paste, _("Paste from clipboard"))

    def on_paste(self):
        self.setText(self.app.clipboard().text())


class ButtonsLineEdit(QLineEdit, ButtonsWidget):
    def __init__(self, text=None):
        QLineEdit.__init__(self, text)
        self.buttons = []

    def resizeEvent(self, e):
        o = QLineEdit.resizeEvent(self, e)
        self.resizeButtons()
        return o

class ButtonsTextEdit(QPlainTextEdit, ButtonsWidget):
    def __init__(self, text=None):
        QPlainTextEdit.__init__(self, text)
        self.setText = self.setPlainText
        self.text = self.toPlainText
        self.buttons = []

    def resizeEvent(self, e):
        o = QPlainTextEdit.resizeEvent(self, e)
        self.resizeButtons()
        return o


class PasswordLineEdit(QLineEdit):
    def __init__(self, *args, **kwargs):
        QLineEdit.__init__(self, *args, **kwargs)
        self.setEchoMode(QLineEdit.Password)

    def clear(self):
        # Try to actually overwrite the memory.
        # This is really just a best-effort thing...
        self.setText(len(self.text()) * " ")
        super().clear()


class TaskThread(QThread):
    '''Thread that runs background tasks.  Callbacks are guaranteed
    to happen in the context of its parent.'''

    class Task(NamedTuple):
        task: Callable
        cb_success: Optional[Callable]
        cb_done: Optional[Callable]
        cb_error: Optional[Callable]

    doneSig = pyqtSignal(object, object, object)

    def __init__(self, parent, on_error=None):
        super(TaskThread, self).__init__(parent)
        self.on_error = on_error
        self.tasks = queue.Queue()
        self.doneSig.connect(self.on_done)
        self.start()

    def add(self, task, on_success=None, on_done=None, on_error=None):
        on_error = on_error or self.on_error
        self.tasks.put(TaskThread.Task(task, on_success, on_done, on_error))

    def run(self):
        while True:
            task = self.tasks.get()  # type: TaskThread.Task
            if not task:
                break
            try:
                result = task.task()
                self.doneSig.emit(result, task.cb_done, task.cb_success)
            except BaseException:
                self.doneSig.emit(sys.exc_info(), task.cb_done, task.cb_error)

    def on_done(self, result, cb_done, cb_result):
        # This runs in the parent's thread.
        if cb_done:
            cb_done()
        if cb_result:
            cb_result(result)

    def stop(self):
        self.tasks.put(None)
        self.exit()
        self.wait()


class ColorSchemeItem:
    def __init__(self, fg_color, bg_color):
        self.colors = (fg_color, bg_color)

    def _get_color(self, background):
        return self.colors[(int(background) + int(ColorScheme.dark_scheme)) % 2]

    def as_stylesheet(self, background=False):
        css_prefix = "background-" if background else ""
        color = self._get_color(background)
        return "QWidget {{ {}color:{}; }}".format(css_prefix, color)

    def as_color(self, background=False):
        color = self._get_color(background)
        return QColor(color)


class ColorScheme:
    dark_scheme = False

    GREEN = ColorSchemeItem("#117c11", "#8af296")
    YELLOW = ColorSchemeItem("#897b2a", "#ffff00")
    RED = ColorSchemeItem("#7c1111", "#f18c8c")
    BLUE = ColorSchemeItem("#123b7c", "#8cb3f2")
    DEFAULT = ColorSchemeItem("black", "white")
    GRAY = ColorSchemeItem("gray", "gray")

    @staticmethod
    def has_dark_background(widget):
        brightness = sum(widget.palette().color(QPalette.Background).getRgb()[0:3])
        return brightness < (255*3/2)

    @staticmethod
    def update_from_widget(widget, force_dark=False):
        if force_dark or ColorScheme.has_dark_background(widget):
            ColorScheme.dark_scheme = True


class AcceptFileDragDrop:
    def __init__(self, file_type=""):
        assert isinstance(self, QWidget)
        self.setAcceptDrops(True)
        self.file_type = file_type

    def validateEvent(self, event):
        if not event.mimeData().hasUrls():
            event.ignore()
            return False
        for url in event.mimeData().urls():
            if not url.toLocalFile().endswith(self.file_type):
                event.ignore()
                return False
        event.accept()
        return True

    def dragEnterEvent(self, event):
        self.validateEvent(event)

    def dragMoveEvent(self, event):
        if self.validateEvent(event):
            event.setDropAction(Qt.CopyAction)

    def dropEvent(self, event):
        if self.validateEvent(event):
            for url in event.mimeData().urls():
                self.onFileAdded(url.toLocalFile())

    def onFileAdded(self, fn):
        raise NotImplementedError()


def import_meta_gui(electrum_window: 'ElectrumWindow', title, importer, on_success):
    filter_ = "JSON (*.json);;All files (*)"
    filename = getOpenFileName(
        parent=electrum_window,
        title=_("Open {} file").format(title),
        filter=filter_,
        config=electrum_window.config,
    )
    if not filename:
        return
    try:
        importer(filename)
    except FileImportFailed as e:
        electrum_window.show_critical(str(e))
    else:
        electrum_window.show_message(_("Your {} were successfully imported").format(title))
        on_success()


def export_meta_gui(electrum_window: 'ElectrumWindow', title, exporter):
    filter_ = "JSON (*.json);;All files (*)"
    filename = getSaveFileName(
        parent=electrum_window,
        title=_("Select file to save your {}").format(title),
        filename='electrum-mars_{}.json'.format(title),
        filter=filter_,
        config=electrum_window.config,
    )
    if not filename:
        return
    try:
        exporter(filename)
    except FileExportFailed as e:
        electrum_window.show_critical(str(e))
    else:
        electrum_window.show_message(_("Your {0} were exported to '{1}'")
                                     .format(title, str(filename)))


def getOpenFileName(*, parent, title, filter="", config: 'SimpleConfig') -> Optional[str]:
    """Custom wrapper for getOpenFileName that remembers the path selected by the user."""
    directory = config.get('io_dir', os.path.expanduser('~'))
    fileName, __ = QFileDialog.getOpenFileName(parent, title, directory, filter)
    if fileName and directory != os.path.dirname(fileName):
        config.set_key('io_dir', os.path.dirname(fileName), True)
    return fileName


def getSaveFileName(
        *,
        parent,
        title,
        filename,
        filter="",
        default_extension: str = None,
        default_filter: str = None,
        config: 'SimpleConfig',
) -> Optional[str]:
    """Custom wrapper for getSaveFileName that remembers the path selected by the user."""
    directory = config.get('io_dir', os.path.expanduser('~'))
    path = os.path.join(directory, filename)

    file_dialog = QFileDialog(parent, title, path, filter)
    file_dialog.setAcceptMode(QFileDialog.AcceptSave)
    if default_extension:
        # note: on MacOS, the selected filter's first extension seems to have priority over this...
        file_dialog.setDefaultSuffix(default_extension)
    if default_filter:
        assert default_filter in filter, f"default_filter={default_filter!r} does not appear in filter={filter!r}"
        file_dialog.selectNameFilter(default_filter)
    if file_dialog.exec() != QDialog.Accepted:
        return None

    selected_path = file_dialog.selectedFiles()[0]
    if selected_path and directory != os.path.dirname(selected_path):
        config.set_key('io_dir', os.path.dirname(selected_path), True)
    return selected_path


def icon_path(icon_basename):
    return resource_path('gui', 'icons', icon_basename)


@lru_cache(maxsize=1000)
def read_QIcon(icon_basename):
    return QIcon(icon_path(icon_basename))

class IconLabel(QWidget):
    IconSize = QSize(16, 16)
    HorizontalSpacing = 2
    def __init__(self, *, text='', final_stretch=True):
        super(QWidget, self).__init__()
        layout = QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        self.setLayout(layout)
        self.icon = QLabel()
        self.label = QLabel(text)
        layout.addWidget(self.label)
        layout.addSpacing(self.HorizontalSpacing)
        layout.addWidget(self.icon)
        if final_stretch:
            layout.addStretch()
    def setText(self, text):
        self.label.setText(text)
    def setIcon(self, icon):
        self.icon.setPixmap(icon.pixmap(self.IconSize))
        self.icon.repaint()  # macOS hack for #6269

def get_default_language():
    name = QLocale.system().name()
    return name if name in languages else 'en_UK'


def char_width_in_lineedit() -> int:
    char_width = QFontMetrics(QLineEdit().font()).averageCharWidth()
    # 'averageCharWidth' seems to underestimate on Windows, hence 'max()'
    return max(9, char_width)


def webopen(url: str):
    if sys.platform == 'linux' and os.environ.get('APPIMAGE'):
        # When on Linux webbrowser.open can fail in AppImage because it can't find the correct libdbus.
        # We just fork the process and unset LD_LIBRARY_PATH before opening the URL.
        # See #5425
        if os.fork() == 0:
            del os.environ['LD_LIBRARY_PATH']
            webbrowser.open(url)
            os._exit(0)
    else:
        webbrowser.open(url)


class FixedAspectRatioLayout(QLayout):
    def __init__(self, parent: QWidget = None, aspect_ratio: float = 1.0):
        super().__init__(parent)
        self.aspect_ratio = aspect_ratio
        self.items: List[QLayoutItem] = []

    def set_aspect_ratio(self, aspect_ratio: float = 1.0):
        self.aspect_ratio = aspect_ratio
        self.update()

    def addItem(self, item: QLayoutItem):
        self.items.append(item)

    def count(self) -> int:
        return len(self.items)

    def itemAt(self, index: int) -> QLayoutItem:
        if index >= len(self.items):
            return None
        return self.items[index]

    def takeAt(self, index: int) -> QLayoutItem:
        if index >= len(self.items):
            return None
        return self.items.pop(index)

    def _get_contents_margins_size(self) -> QSize:
        margins = self.contentsMargins()
        return QSize(margins.left() + margins.right(), margins.top() + margins.bottom())

    def setGeometry(self, rect: QRect):
        super().setGeometry(rect)
        if not self.items:
            return

        contents = self.contentsRect()
        if contents.height() > 0:
            c_aratio = contents.width() / contents.height()
        else:
            c_aratio = 1
        s_aratio = self.aspect_ratio
        item_rect = QRect(QPoint(0, 0), QSize(
            contents.width() if c_aratio < s_aratio else contents.height() * s_aratio,
            contents.height() if c_aratio > s_aratio else contents.width() / s_aratio
        ))

        content_margins = self.contentsMargins()
        free_space = contents.size() - item_rect.size()

        for item in self.items:
            if free_space.width() > 0 and not item.alignment() & Qt.AlignLeft:
                if item.alignment() & Qt.AlignRight:
                    item_rect.moveRight(contents.width() + content_margins.right())
                else:
                    item_rect.moveLeft(content_margins.left() + (free_space.width() / 2))
            else:
                item_rect.moveLeft(content_margins.left())

            if free_space.height() > 0 and not item.alignment() & Qt.AlignTop:
                if item.alignment() & Qt.AlignBottom:
                    item_rect.moveBottom(contents.height() + content_margins.bottom())
                else:
                    item_rect.moveTop(content_margins.top() + (free_space.height() / 2))
            else:
                item_rect.moveTop(content_margins.top())

            item.widget().setGeometry(item_rect)

    def sizeHint(self) -> QSize:
        result = QSize()
        for item in self.items:
            result = result.expandedTo(item.sizeHint())
        return self._get_contents_margins_size() + result

    def minimumSize(self) -> QSize:
        result = QSize()
        for item in self.items:
            result = result.expandedTo(item.minimumSize())
        return self._get_contents_margins_size() + result

    def expandingDirections(self) -> Qt.Orientations:
        return Qt.Horizontal | Qt.Vertical


def QColorLerp(a: QColor, b: QColor, t: float):
    """
    Blends two QColors. t=0 returns a. t=1 returns b. t=0.5 returns evenly mixed.
    """
    t = max(min(t, 1.0), 0.0)
    i_t = 1.0 - t
    return QColor(
        (a.red()   * i_t) + (b.red()   * t),
        (a.green() * i_t) + (b.green() * t),
        (a.blue()  * i_t) + (b.blue()  * t),
        (a.alpha() * i_t) + (b.alpha() * t),
    )


class ImageGraphicsEffect(QObject):
    """
    Applies a QGraphicsEffect to a QImage
    """

    def __init__(self, parent: QObject, effect: QGraphicsEffect):
        super().__init__(parent)
        assert effect, 'effect must be set'
        self.effect = effect
        self.graphics_scene = QGraphicsScene()
        self.graphics_item = QGraphicsPixmapItem()
        self.graphics_item.setGraphicsEffect(effect)
        self.graphics_scene.addItem(self.graphics_item)

    def apply(self, image: QImage):
        assert image, 'image must be set'
        result = QImage(image.size(), QImage.Format_ARGB32)
        result.fill(Qt.transparent)
        painter = QPainter(result)
        self.graphics_item.setPixmap(QPixmap.fromImage(image))
        self.graphics_scene.render(painter)
        self.graphics_item.setPixmap(QPixmap())
        return result


if __name__ == "__main__":
    app = QApplication([])
    t = WaitingDialog(None, 'testing ...', lambda: [time.sleep(1)], lambda x: QMessageBox.information(None, 'done', "done"))
    t.start()
    app.exec_()
