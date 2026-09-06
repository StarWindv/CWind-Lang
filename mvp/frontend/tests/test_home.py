"""todo-172-era std addressing: install-root derivation (home.py).

The std (``libs/``) anchor follows the compiler's own location —
``CWIND_HOME`` override, then the running executable, then the installed
package — never the working directory.  ``pkgs/`` addressing support is
covered by the resolution layer; here we pin the derivation rules.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent.parent.parent
for path in (ROOT / "mvp/frontend/src", ROOT / "mvp/frontend/tests"):
    sys.path.insert(0, str(path))

from cwind_frontend.home import (  # noqa: E402
    install_root,
    reset_install_root_cache,
)


class InstallRootTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_home = os.environ.pop("CWIND_HOME", None)
        reset_install_root_cache()

    def tearDown(self) -> None:
        if self._saved_home is not None:
            os.environ["CWIND_HOME"] = self._saved_home
        else:
            os.environ.pop("CWIND_HOME", None)
        reset_install_root_cache()

    def test_env_override(self):
        with tempfile.TemporaryDirectory() as td:
            os.environ["CWIND_HOME"] = td
            reset_install_root_cache()
            self.assertEqual(Path(td).resolve(), install_root())

    def test_env_override_ignores_nonexistent(self):
        os.environ["CWIND_HOME"] = r"Z:\definitely\missing\dir"
        reset_install_root_cache()
        # Falls through to the repository checkout the package lives in.
        self.assertEqual(ROOT, install_root())

    def test_repo_checkout_is_install_root(self):
        # The installed package sits in <repo>/mvp/frontend/src; walking
        # up from it finds <repo>/libs.
        reset_install_root_cache()
        self.assertEqual(ROOT, install_root())

    def test_scripts_layout_derivation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            (root / "scripts").mkdir()
            (root / "libs").mkdir()
            (root / "scripts" / "cwindf").write_text("", encoding="utf-8")
            old_argv = sys.argv
            sys.argv = [str(root / "scripts" / "cwindf"), "x"]
            reset_install_root_cache()
            try:
                self.assertEqual(root, install_root())
            finally:
                sys.argv = old_argv

    def test_bin_layout_derivation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            (root / "bin").mkdir()
            (root / "libs").mkdir()
            (root / "bin" / "cwindf.exe").write_text("", encoding="utf-8")
            old_argv = sys.argv
            sys.argv = [str(root / "bin" / "cwindf.exe"), "x"]
            reset_install_root_cache()
            try:
                self.assertEqual(root, install_root())
            finally:
                sys.argv = old_argv

    def test_no_root_without_libs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            (root / "bin").mkdir()
            (root / "bin" / "cwindf.exe").write_text("", encoding="utf-8")
            old_argv = sys.argv
            sys.argv = [str(root / "bin" / "cwindf.exe"), "x"]
            reset_install_root_cache()
            try:
                self.assertIsNone(
                    _discover_from_binary(), "cwd must not leak in"
                )
            finally:
                sys.argv = old_argv


def _discover_from_binary():
    """Derive the root from the binary only (no package fallback)."""
    from cwind_frontend.home import _cached_discover, _entry_binary_dir

    return _cached_discover(_entry_binary_dir())


if __name__ == "__main__":
    unittest.main()
