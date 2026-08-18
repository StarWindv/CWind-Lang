"""Unit tests for the built-in TOML loader (directional traits)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cwind_frontend.sa import builtin_methods as bm


_TOML = """
[traits]
Display = ["to_string"]
From    = ["from"]
Into    = ["into"]

[trait_methods]
to_string = { args = ["Self"], returns = "String" }
from      = { args = ["TraitArg:1"], returns = "Self" }
into      = { args = ["Self"], returns = "TraitArg:1" }

[types.UInt]
traits = ["Display", "From<String>"]

[types.String]
traits = ["Display", "Into<UInt>"]

[types.Int]
traits = ["Display"]

[modules.builtins]
"""


class TestDirectionalTraits(unittest.TestCase):
    @staticmethod
    def _load(text: str):
        return bm._load_text(text.encode("utf-8"))

    def test_directional_from_and_into_resolve(self):
        _, type_methods, _, _ = self._load(_TOML)
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
        bad = _TOML.replace('"From<String>"', '"From<String"')
        with self.assertRaises(ValueError):
            self._load(bad)

    def test_unbalanced_trait_args_rejected(self):
        for ref in ("From<A>>", "From<A<B>", "From<A<B>>C>"):
            bad = _TOML.replace('"From<String>"', f'"{ref}"')
            with self.assertRaises(ValueError):
                self._load(bad)

    def test_nested_trait_arg_resolves(self):
        toml = _TOML.replace(
            '[types.String]\n'
            'traits = ["Display", "Into<UInt>"]',
            '[types.String]\n'
            'traits = ["Display", "Into<Vector<Int>>"]',
        )
        _, type_methods, _, _ = self._load(toml)
        self.assertEqual(
            type_methods["String"]["into"].returns, "Vector<Int>"
        )

    def test_trait_arity_mismatch_rejected(self):
        bad = _TOML.replace('"From<String>"', '"From<String, Int>"')
        with self.assertRaises(ValueError) as cm:
            self._load(bad)
        self.assertIn("takes 1 type argument(s), got 2", str(cm.exception))

    def test_bare_directional_trait_rejected(self):
        bad = _TOML.replace('"From<String>"', '"From"')
        with self.assertRaises(ValueError) as cm:
            self._load(bad)
        self.assertIn("takes 1 type argument(s), got 0", str(cm.exception))

    def test_unknown_trait_rejected(self):
        bad = _TOML.replace('"From<String>"', '"Nope<String>"')
        with self.assertRaises(ValueError) as cm:
            self._load(bad)
        self.assertIn("unknown trait 'Nope'", str(cm.exception))

    def test_conflicting_explicit_method_rejected(self):
        bad = _TOML.replace(
            '[types.UInt]\ntraits = ["Display", "From<String>"]',
            '[types.UInt]\n'
            'traits = ["Display", "From<String>"]\n'
            '[types.UInt.methods]\n'
            'from = { args = ["Int"], returns = "Self" }\n',
        )
        with self.assertRaises(ValueError) as cm:
            self._load(bad)
        self.assertIn("conflicting definitions of 'from'", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
