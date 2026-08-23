"""Unit tests for the built-in TOML loader (directional traits).

The loader fixtures live in ``cases/builtin_methods``: one ``.toml`` per
variant, plus a ``{"error_contains": ...}`` sidecar where a ValueError with
a specific message is expected.
"""

import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

import harness

from cwind_frontend.sa import builtin_methods as bm

BM = TESTS / "cases" / "builtin_methods"


class TestDirectionalTraits(unittest.TestCase):
    @staticmethod
    def _load(name: str):
        text = (BM / f"{name}.toml").read_bytes()
        return bm._load_text(text)

    @staticmethod
    def _expect(name: str) -> dict:
        path = BM / f"{name}.json"
        if not path.exists():
            return {}
        import json

        return json.loads(path.read_bytes())

    def test_directional_from_and_into_resolve(self):
        _, _, _, _, type_methods, _, _ = self._load("directional")
        uint = type_methods["UInt"]
        self.assertIn("from", uint)
        self.assertEqual(uint["from"].args, ("String",))
        self.assertEqual(uint["from"].returns, "Self")
        self.assertNotIn("into", uint)
        string = type_methods["String"]
        self.assertIn("into", string)
        self.assertEqual(string["into"].args, ("Self",))
        self.assertEqual(string["into"].returns, "UInt")
        self.assertNotIn("from", string)
        self.assertNotIn("from", type_methods["Int"])
        self.assertNotIn("into", type_methods["Int"])

    def test_malformed_trait_ref_rejected(self):
        with self.assertRaises(ValueError):
            self._load("malformed_trait_ref")

    def test_unbalanced_trait_args_rejected(self):
        for name in ("unbalanced_a_gt", "unbalanced_open", "unbalanced_nested"):
            with self.subTest(case=name):
                with self.assertRaises(ValueError):
                    self._load(name)

    def test_nested_trait_arg_resolves(self):
        _, _, _, _, type_methods, _, _ = self._load("nested_trait_arg")
        self.assertEqual(
            type_methods["String"]["into"].returns, "Vector<Int>"
        )

    def test_trait_arity_mismatch_rejected(self):
        with self.assertRaises(ValueError) as cm:
            self._load("trait_arity_mismatch")
        self.assertIn(
            self._expect("trait_arity_mismatch")["error_contains"],
            str(cm.exception),
        )

    def test_bare_directional_trait_rejected(self):
        with self.assertRaises(ValueError) as cm:
            self._load("bare_directional_trait")
        self.assertIn(
            self._expect("bare_directional_trait")["error_contains"],
            str(cm.exception),
        )

    def test_unknown_trait_rejected(self):
        with self.assertRaises(ValueError) as cm:
            self._load("unknown_trait")
        self.assertIn(
            self._expect("unknown_trait")["error_contains"],
            str(cm.exception),
        )

    def test_conflicting_explicit_method_rejected(self):
        with self.assertRaises(ValueError) as cm:
            self._load("conflicting_explicit_method")
        self.assertIn(
            self._expect("conflicting_explicit_method")["error_contains"],
            str(cm.exception),
        )


if __name__ == "__main__":
    unittest.main()
