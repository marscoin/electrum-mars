# Electrum - lightweight Bitcoin client
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
import json
import locale
import traceback
import sys

from .version import ELECTRUM_VERSION
from . import constants
from .i18n import _
from .util import make_aiohttp_session
from .logging import describe_os_version, Logger, get_git_version


class BaseCrashReporter(Logger):
    report_server = "https://crashhub.electrum-mars.org"
    config_key = "show_crash_reporter"
    issue_template = """<h2>Traceback</h2>
<pre>
{traceback}
</pre>

<h2>Additional information</h2>
<ul>
  <li>Electrum version: {app_version}</li>
  <li>Python version: {python_version}</li>
  <li>Operating system: {os}</li>
  <li>Wallet type: {wallet_type}</li>
  <li>Locale: {locale}</li>
</ul>
    """
    CRASH_MESSAGE = _('Something went wrong while executing Electrum.')
    CRASH_TITLE = _('Sorry!')
    REQUEST_HELP_MESSAGE = _('To help us diagnose and fix the problem, you can send us a bug report that contains '
                             'useful debug information:')
    DESCRIBE_ERROR_MESSAGE = _("Please briefly describe what led to the error (optional):")
    ASK_CONFIRM_SEND = _("Do you want to send this report?")
    USER_COMMENT_PLACEHOLDER = _("Do not enter sensitive/private information here. "
                                 "The report will be visible on the public issue tracker.")

    def __init__(self, exctype, value, tb):
        Logger.__init__(self)
        self.exc_args = (exctype, value, tb)

    def send_report(self, asyncio_loop, proxy, endpoint="/crash", *, timeout=None):
        if constants.net.GENESIS[-4:] not in ["29a0", "bfe2"] and ".electrum-mars.org" in BaseCrashReporter.report_server:
            # Gah! Some kind of amarsoin wants to send us crash reports.
            raise Exception(_("Missing report URL."))
        report = self.get_traceback_info()
        report.update(self.get_additional_info())
        report = json.dumps(report)
        coro = self.do_post(proxy, BaseCrashReporter.report_server + endpoint, data=report)
        response = asyncio.run_coroutine_threadsafe(coro, asyncio_loop).result(timeout)
        return response

    async def do_post(self, proxy, url, data):
        async with make_aiohttp_session(proxy) as session:
            async with session.post(url, data=data, raise_for_status=True) as resp:
                return await resp.text()

    def get_traceback_info(self):
        exc_string = str(self.exc_args[1])
        stack = traceback.extract_tb(self.exc_args[2])
        readable_trace = self.__get_traceback_str_to_send()
        id = {
            "file": stack[-1].filename,
            "name": stack[-1].name,
            "type": self.exc_args[0].__name__
        }
        return {
            "exc_string": exc_string,
            "stack": readable_trace,
            "id": id
        }

    def get_additional_info(self):
        args = {
            "app_version": get_git_version() or ELECTRUM_VERSION,
            "python_version": sys.version,
            "os": describe_os_version(),
            "wallet_type": "unknown",
            "locale": locale.getdefaultlocale()[0] or "?",
            "description": self.get_user_description()
        }
        try:
            args["wallet_type"] = self.get_wallet_type()
        except:
            # Maybe the wallet isn't loaded yet
            pass
        return args

    def __get_traceback_str_to_send(self) -> str:
        # make sure that traceback sent to crash reporter contains
        # e.__context__ and e.__cause__, i.e. if there was a chain of
        # exceptions, we want the full traceback for the whole chain.
        return "".join(traceback.format_exception(*self.exc_args))

    def _get_traceback_str_to_display(self) -> str:
        # overridden in Qt subclass
        return self.__get_traceback_str_to_send()

    def get_report_string(self):
        info = self.get_additional_info()
        info["traceback"] = self._get_traceback_str_to_display()
        return self.issue_template.format(**info)

    def get_user_description(self):
        raise NotImplementedError

    def get_wallet_type(self) -> str:
        raise NotImplementedError


def trigger_crash():
    # note: do not change the type of the exception, the message,
    # or the name of this method. All reports generated through this
    # method will be grouped together by the crash reporter, and thus
    # don't spam the issue tracker.

    class TestingException(Exception):
        pass

    def crash_test():
        raise TestingException("triggered crash for testing purposes")

    import threading
    t = threading.Thread(target=crash_test)
    t.start()
