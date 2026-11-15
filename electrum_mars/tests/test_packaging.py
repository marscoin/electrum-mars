import glob
import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PKG = os.path.join(REPO, "electrum_mars")
WORKFLOW = os.path.join(REPO, ".github", "workflows", "build-release.yml")


def _top_level_modules():
    return [os.path.basename(f)[:-3] for f in glob.glob(os.path.join(PKG, "*.py")) if not f.endswith("__init__.py")]


def _modules_used_by(files, modules):
    used = set()
    for path in files:
        with open(path, errors="replace") as f:
            text = f.read()
        for m in modules:
            pattern = (rf"from\s+electrum_mars\.{m}\s+import|import\s+electrum_mars\.{m}\b|"
                       rf"from\s+electrum_mars\s+import[^\n]*\b{m}\b|from\s+\.{m}\s+import|"
                       rf"from\s+\.\s+import[^\n]*\b{m}\b|from\s+\.\.{m}\s+import|from\s+\.\.\s+import[^\n]*\b{m}\b")
            if re.search(pattern, text):
                used.add(m)
    return used


class TestPackaging(unittest.TestCase):
    """Plugins are shipped as files, so the package builder does not see what they import.
    A wallet module that only a plugin uses is left out of the Windows and Linux packages
    unless the build names it (4.4.7 shipped without atomic_swap_htlc and btc_monitor)."""

    def test_modules_only_plugins_use_are_named_in_the_build(self):
        modules = _top_level_modules()
        everything = glob.glob(os.path.join(PKG, "**", "*.py"), recursive=True)
        skip = (os.sep + "plugins" + os.sep, os.sep + "tests" + os.sep, os.sep + "kivy", os.sep + "qml")
        core = [f for f in everything if not any(s in f for s in skip)] + [os.path.join(REPO, "run_electrum")]
        plugins = [f for f in everything if os.sep + "plugins" + os.sep in f]
        only_plugins = _modules_used_by(plugins, modules) - _modules_used_by(core, modules)
        with open(WORKFLOW) as f:
            workflow = f.read()
        for m in sorted(only_plugins | {"atomic_swap_htlc"}):
            # once for the Linux build and once for the Windows build
            self.assertGreaterEqual(workflow.count(f"--hidden-import electrum_mars.{m} "), 2,
                                    f"electrum_mars.{m} is only used by plugins; name it with --hidden-import in both package builds")

    def test_windows_build_installs_the_fast_scrypt_library(self):
        with open(WORKFLOW) as f:
            workflow = f.read()
        windows = workflow[workflow.index("\n  windows:"):workflow.index("\n  macos:")]
        self.assertRegex(windows, r"pip install[^\n]*\bscrypt\b")
        self.assertIn("--hidden-import _scrypt", windows)
