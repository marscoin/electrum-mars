#!/usr/bin/env python
#
# Electrum - lightweight Bitcoin client
# Copyright (C) 2012 thomasv@gitorious
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

import re
import decimal
from decimal import Decimal
from typing import NamedTuple, Sequence, Optional, List, TYPE_CHECKING

from PyQt5.QtGui import QFontMetrics, QFont

from electrum_mars import bitcoin
from electrum_mars.util import bfh, maybe_extract_bolt11_invoice, BITCOIN_BIP21_URI_SCHEME
from electrum_mars.transaction import PartialTxOutput
from electrum_mars.bitcoin import opcodes, construct_script
from electrum_mars.logging import Logger
from electrum_mars.lnaddr import LnDecodeException

from .qrtextedit import ScanQRTextEdit
from .completion_text_edit import CompletionTextEdit
from . import util
from .util import MONOSPACE_FONT

if TYPE_CHECKING:
    from .main_window import ElectrumWindow


RE_ALIAS = r'(.*?)\s*\<([0-9A-Za-z]{1,})\>'

frozen_style = "QWidget {border:none;}"
normal_style = "QPlainTextEdit { }"


class PayToLineError(NamedTuple):
    line_content: str
    exc: Exception
    idx: int = 0  # index of line
    is_multiline: bool = False


class PayToEdit(CompletionTextEdit, ScanQRTextEdit, Logger):

    def __init__(self, win: 'ElectrumWindow'):
        CompletionTextEdit.__init__(self)
        ScanQRTextEdit.__init__(self, config=win.config)
        Logger.__init__(self)
        self.win = win
        self.amount_edit = win.amount_e
        self.setFont(QFont(MONOSPACE_FONT))
        self.document().contentsChanged.connect(self.update_size)
        self.heightMin = 0
        self.heightMax = 150
        self.c = None
        self.textChanged.connect(self.check_text)
        self.outputs = []  # type: List[PartialTxOutput]
        self.errors = []  # type: List[PayToLineError]
        self.is_pr = False
        self.is_alias = False
        self.update_size()
        self.payto_scriptpubkey = None  # type: Optional[bytes]
        self.lightning_invoice = None
        self.previous_payto = ''

    def setFrozen(self, b):
        self.setReadOnly(b)
        self.setStyleSheet(frozen_style if b else normal_style)
        for button in self.buttons:
            button.setHidden(b)

    def setGreen(self):
        self.setStyleSheet(util.ColorScheme.GREEN.as_stylesheet(True))

    def setExpired(self):
        self.setStyleSheet(util.ColorScheme.RED.as_stylesheet(True))

    def parse_address_and_amount(self, line) -> PartialTxOutput:
        try:
            x, y = line.split(',')
        except ValueError:
            raise Exception("expected two comma-separated values: (address, amount)") from None
        scriptpubkey = self.parse_output(x)
        amount = self.parse_amount(y)
        return PartialTxOutput(scriptpubkey=scriptpubkey, value=amount)

    def parse_output(self, x) -> bytes:
        try:
            address = self.parse_address(x)
            return bfh(bitcoin.address_to_script(address))
        except Exception:
            pass
        try:
            script = self.parse_script(x)
            return bfh(script)
        except Exception:
            pass
        raise Exception("Invalid address or script.")

    def parse_script(self, x):
        script = ''
        for word in x.split():
            if word[0:3] == 'OP_':
                opcode_int = opcodes[word]
                script += construct_script([opcode_int])
            else:
                bfh(word)  # to test it is hex data
                script += construct_script([word])
        return script

    def parse_amount(self, x):
        x = x.strip()
        if not x:
            raise Exception("Amount is empty")
        if x == '!':
            return '!'
        p = pow(10, self.amount_edit.decimal_point())
        try:
            return int(p * Decimal(x))
        except decimal.InvalidOperation:
            raise Exception("Invalid amount")

    def parse_address(self, line):
        r = line.strip()
        m = re.match('^'+RE_ALIAS+'$', r)
        address = str(m.group(2) if m else r)
        assert bitcoin.is_address(address)
        return address

    def check_text(self):
        self.errors = []
        if self.is_pr:
            return
        # filter out empty lines
        lines = [i for i in self.lines() if i]

        self.payto_scriptpubkey = None
        self.lightning_invoice = None
        self.outputs = []

        if len(lines) == 1:
            data = lines[0]
            # try bip21 URI
            if data.lower().startswith(BITCOIN_BIP21_URI_SCHEME + ':'):
                self.win.pay_to_URI(data)
                return
            # try LN invoice
            bolt11_invoice = maybe_extract_bolt11_invoice(data)
            if bolt11_invoice is not None:
                self.win.set_ln_invoice(bolt11_invoice)
                return
            # try "address, amount" on-chain format
            try:
                self._parse_as_multiline(lines, raise_errors=True)
            except Exception as e:
                pass
            else:
                return
            # try address/script
            try:
                self.payto_scriptpubkey = self.parse_output(data)
            except Exception as e:
                self.errors.append(PayToLineError(line_content=data, exc=e))
            else:
                self.win.set_onchain(True)
                self.win.lock_amount(False)
                return
        else:
            # there are multiple lines
            self._parse_as_multiline(lines, raise_errors=False)

    def _parse_as_multiline(self, lines, *, raise_errors: bool):
        outputs = []  # type: List[PartialTxOutput]
        total = 0
        is_max = False
        for i, line in enumerate(lines):
            try:
                output = self.parse_address_and_amount(line)
            except Exception as e:
                if raise_errors:
                    raise
                else:
                    self.errors.append(PayToLineError(
                        idx=i, line_content=line.strip(), exc=e, is_multiline=True))
                    continue
            outputs.append(output)
            if output.value == '!':
                is_max = True
            else:
                total += output.value
        if outputs:
            self.win.set_onchain(True)

        self.win.max_button.setChecked(is_max)
        self.outputs = outputs
        self.payto_scriptpubkey = None

        if self.win.max_button.isChecked():
            self.win.spend_max()
        else:
            self.amount_edit.setAmount(total if outputs else None)
        self.win.lock_amount(self.win.max_button.isChecked() or bool(outputs))

    def get_errors(self) -> Sequence[PayToLineError]:
        return self.errors

    def get_destination_scriptpubkey(self) -> Optional[bytes]:
        return self.payto_scriptpubkey

    def get_outputs(self, is_max):
        if self.payto_scriptpubkey:
            if is_max:
                amount = '!'
            else:
                amount = self.amount_edit.get_amount()
            self.outputs = [PartialTxOutput(scriptpubkey=self.payto_scriptpubkey, value=amount)]

        return self.outputs[:]

    def lines(self):
        return self.toPlainText().split('\n')

    def is_multiline(self):
        return len(self.lines()) > 1

    def paytomany(self):
        self.setText("\n\n\n")
        self.update_size()

    def update_size(self):
        lineHeight = QFontMetrics(self.document().defaultFont()).height()
        docHeight = self.document().size().height()
        h = round(docHeight * lineHeight + 11)
        h = min(max(h, self.heightMin), self.heightMax)
        self.setMinimumHeight(h)
        self.setMaximumHeight(h)
        self.verticalScrollBar().hide()

    def qr_input(self, *, callback=None):
        def _on_qr_success(data):
            if data.lower().startswith(BITCOIN_BIP21_URI_SCHEME + ':'):
                self.win.pay_to_URI(data)
                # TODO: update fee
        super(PayToEdit, self).qr_input(callback=_on_qr_success)

    def resolve(self):
        self.is_alias = False
        if self.hasFocus():
            return
        if self.is_multiline():  # only supports single line entries atm
            return
        if self.is_pr:
            return
        key = str(self.toPlainText())
        key = key.strip()  # strip whitespaces
        if key == self.previous_payto:
            return
        self.previous_payto = key
        if not (('.' in key) and (not '<' in key) and (not ' ' in key)):
            return
        parts = key.split(sep=',')  # assuming single line
        if parts and len(parts) > 0 and bitcoin.is_address(parts[0]):
            return
        try:
            data = self.win.contacts.resolve(key)
        except Exception as e:
            self.logger.info(f'error resolving address/alias: {repr(e)}')
            return
        if not data:
            return
        self.is_alias = True

        address = data.get('address')
        name = data.get('name')
        new_url = key + ' <' + address + '>'
        self.setText(new_url)
        self.previous_payto = new_url

        #if self.win.config.get('openalias_autoadd') == 'checked':
        self.win.contacts[key] = ('openalias', name)
        self.win.contact_list.update()

        self.setFrozen(True)
        if data.get('type') == 'openalias':
            self.validated = data.get('validated')
            if self.validated:
                self.setGreen()
            else:
                self.setExpired()
        else:
            self.validated = None
