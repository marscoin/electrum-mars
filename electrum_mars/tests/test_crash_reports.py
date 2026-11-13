import asyncio
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from electrum_mars import base_crash_reporter
from electrum_mars.base_crash_reporter import BaseCrashReporter, scrub_home

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RUN_ELECTRUM = os.path.join(REPO, "run_electrum")


def _raise_in_home():
    """An exception whose traceback names a file under the home folder."""
    code = compile("def boom():\n    raise ValueError('bad file %s' % path)\nboom()\n",
                   os.path.join(os.path.expanduser('~'), "secret-folder", "plugin.py"), "exec")
    try:
        exec(code, {"path": os.path.join(os.path.expanduser('~'), "wallets", "savings")})
    except ValueError:
        return sys.exc_info()
    raise AssertionError("did not raise")


class _Reporter(BaseCrashReporter):
    def get_user_description(self):
        return "what I did"

    def get_wallet_type(self):
        return "standard"


class TestCrashReporter(unittest.TestCase):

    def test_reports_go_to_the_foundation(self):
        self.assertEqual("https://www.marscoin.org/wallet", BaseCrashReporter.report_server)
        self.assertNotIn("public issue tracker", BaseCrashReporter.USER_COMMENT_PLACEHOLDER)

    def test_home_folder_is_not_in_the_report(self):
        home = os.path.expanduser('~')
        self.assertEqual(os.path.join('~', 'x', 'y.py'), scrub_home(os.path.join(home, 'x', 'y.py')))
        self.assertEqual('no path here', scrub_home('no path here'))
        reporter = _Reporter(*_raise_in_home())
        info = reporter.get_traceback_info()
        blob = json.dumps(info)
        self.assertNotIn(home, blob)
        self.assertIn("secret-folder", info["stack"])
        self.assertEqual("ValueError", info["id"]["type"])
        self.assertEqual("boom", info["id"]["name"])
        self.assertTrue(info["id"]["file"].startswith("~"))
        self.assertTrue(info["exc_string"].startswith("bad file ~"))

    def test_send_posts_the_report_as_json(self):
        reporter = _Reporter(*_raise_in_home())
        seen = {}

        async def fake_post(self, proxy, url, data):
            seen["url"], seen["data"] = url, json.loads(data)
            return "Thank you."

        loop = asyncio.new_event_loop()
        import threading
        t = threading.Thread(target=loop.run_forever, daemon=True)
        t.start()
        try:
            with mock.patch.object(BaseCrashReporter, "do_post", fake_post):
                # Marscoin's genesis block used to make this raise "Missing report URL"
                response = BaseCrashReporter.send_report(reporter, loop, None, timeout=10)
        finally:
            loop.call_soon_threadsafe(loop.stop)
            t.join(5)
        self.assertEqual("Thank you.", response)
        self.assertEqual("https://www.marscoin.org/wallet/crash", seen["url"])
        for key in ("exc_string", "stack", "id", "app_version", "python_version", "os", "wallet_type", "locale", "description"):
            self.assertIn(key, seen["data"])
        self.assertEqual("standard", seen["data"]["wallet_type"])
        self.assertEqual("what I did", seen["data"]["description"])


def _load_run_electrum():
    """run_electrum as a module (not as __main__, so nothing is installed or started)."""
    loader = importlib.machinery.SourceFileLoader("run_electrum_under_test", RUN_ELECTRUM)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class TestStartupGuard(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.hook_before = sys.excepthook
        cls.guard = _load_run_electrum()

    def setUp(self):
        self.datadir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.datadir, ignore_errors=True)
        patcher = mock.patch.dict(os.environ, {"ELECTRUMLTC_DIR": self.datadir})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("ELECTRUM_MARS_NO_STARTUP_DIALOG", "CI"):
            os.environ.pop(name, None)

    def test_loading_the_script_installs_nothing(self):
        self.assertIs(self.hook_before, sys.excepthook)

    def test_report_has_the_servers_fields_and_no_home_folder(self):
        report = self.guard._startup_report(*_raise_in_home())
        self.assertEqual("startup", report["kind"])
        self.assertEqual("ValueError", report["id"]["type"])
        self.assertNotIn(os.path.expanduser('~'), json.dumps(report))
        self.assertIn("ValueError: bad file", report["stack"])
        from electrum_mars.version import ELECTRUM_VERSION
        self.assertEqual(ELECTRUM_VERSION, report["app_version"])

    def test_only_a_window_start_gets_a_dialog(self):
        cases = {(): True, ("gui",): True, ("-v",): True, ("marscoin:Mxyz?amount=1",): True,
                 ("getinfo",): False, ("-v", "daemon"): False, ("--offline", "version"): False}
        for args, expected in cases.items():
            with mock.patch.object(sys, "argv", ["run_electrum", *args]):
                self.assertEqual(expected, self.guard._startup_wants_window(), args)
        with mock.patch.object(sys, "argv", ["run_electrum"]), mock.patch.dict(os.environ, {"CI": "1"}):
            self.assertFalse(self.guard._startup_wants_window())

    def _fail(self, *, answer, sent, argv=("run_electrum",)):
        asked, told, posted = [], [], []

        def dialog(title, text, ask):
            (asked if ask else told).append(text)
            return answer if ask else False

        def send(report):
            posted.append(report)
            return sent

        with mock.patch.object(self.guard, "_startup_dialog", dialog), mock.patch.object(self.guard, "_startup_send", send), \
                mock.patch.object(sys, "argv", list(argv)), mock.patch.object(sys, "__excepthook__", lambda *a: None):
            self.guard._startup_failed(*_raise_in_home())
        return asked, told, posted

    def test_error_is_saved_and_nothing_is_sent_without_a_yes(self):
        asked, told, posted = self._fail(answer=False, sent=True)
        path = os.path.join(self.datadir, "startup-error.log")
        with open(path, encoding="utf-8") as f:
            saved = f.read()
        self.assertIn("ValueError: bad file", saved)
        self.assertIn("Electrum-Mars version:", saved)
        self.assertEqual(1, len(asked))
        self.assertIn("could not start", asked[0])
        self.assertIn(path, asked[0])
        self.assertIn("nothing about your wallet", asked[0])
        self.assertEqual([], posted)
        self.assertEqual([], told)

    def test_yes_sends_and_says_so(self):
        asked, told, posted = self._fail(answer=True, sent=True)
        self.assertEqual(1, len(posted))
        self.assertEqual("startup", posted[0]["kind"])
        self.assertEqual(["Thank you. The report was sent to the Marscoin Foundation."], told)

    def test_failed_sending_points_to_the_file_and_the_address(self):
        asked, told, posted = self._fail(answer=True, sent=False)
        self.assertEqual(1, len(told))
        self.assertIn("could not be sent", told[0])
        self.assertIn("startup-error.log", told[0])
        self.assertIn("hello@marscoinfoundation.org", told[0])

    def test_a_command_in_a_terminal_gets_no_dialog(self):
        asked, told, posted = self._fail(answer=True, sent=True, argv=("run_electrum", "getinfo"))
        self.assertEqual(([], [], []), (asked, told, posted))
        self.assertTrue(os.path.exists(os.path.join(self.datadir, "startup-error.log")))

    def test_a_broken_install_leaves_an_error_file(self):
        # A wallet started for real, with one of its libraries broken.
        broken = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, broken, ignore_errors=True)
        with open(os.path.join(broken, "qrcode.py"), "w") as f:
            f.write("raise ImportError('DLL load failed while importing qrcode')\n")
        env = dict(os.environ, PYTHONPATH=broken, ELECTRUMLTC_DIR=self.datadir, ELECTRUM_MARS_NO_STARTUP_DIALOG="1")
        result = subprocess.run([sys.executable, RUN_ELECTRUM], env=env, capture_output=True, text=True, timeout=120)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("DLL load failed while importing qrcode", result.stderr)
        with open(os.path.join(self.datadir, "startup-error.log"), encoding="utf-8") as f:
            saved = f.read()
        self.assertIn("Electrum-Mars could not start.", saved)
        self.assertIn("DLL load failed while importing qrcode", saved)
        self.assertTrue(os.path.exists(os.path.join(self.datadir, "fault.log")))
