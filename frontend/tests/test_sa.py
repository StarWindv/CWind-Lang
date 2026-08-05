"""Unit tests for cwind_frontend.sa."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cwind_frontend import SaError, parse_source, run_sa, run_sa_with_errors


class TestSa(unittest.TestCase):
    def test_collect_symbols(self):
        prog = parse_source(
            "const a: Int = 1; struct S { pub x: Int; } fn f() -> None {}"
        )
        info = run_sa(prog)
        names = {s.name for s in info.symbols.values()}
        self.assertEqual(names, {"a", "S", "f"})
        self.assertEqual(info.symbols["S"].kind, "struct")

    def test_duplicate_definition(self):
        with self.assertRaises(SaError) as cm:
            run_sa(parse_source("struct A {} struct A {}"))
        self.assertIn("duplicate definition", cm.exception.message)

    def test_unknown_type(self):
        with self.assertRaises(SaError) as cm:
            run_sa(parse_source("struct A { pub x: Missing; }"))
        self.assertIn("unknown type", cm.exception.message)

    def test_builtin_redefinition(self):
        with self.assertRaises(SaError) as cm:
            run_sa(parse_source("type String = Int"))
        self.assertIn("redefines a built-in type", cm.exception.message)

    def test_group_apply_references(self):
        with self.assertRaises(SaError):
            run_sa(parse_source("G@S -> {f}"))
        with self.assertRaises(SaError):
            run_sa(parse_source("group G { x -> Int; } G@S -> {x}"))

    def test_impl_references(self):
        with self.assertRaises(SaError):
            run_sa(parse_source("impl Trait for Struct {}"))

    def test_run_sa_collects_many(self):
        prog = parse_source(
            "struct A { pub x: Missing; } struct B { pub y: AlsoMissing; }"
        )
        result = run_sa_with_errors(prog)
        self.assertEqual(len(result.errors), 2)
        self.assertTrue(all("unknown type" in e.message for e in result.errors))

    def test_exam2(self):
        src = (Path(__file__).resolve().parents[2] / "assets" / "exam2.wind").read_text(
            encoding="utf-8"
        )
        info = run_sa(parse_source(src))
        self.assertGreater(len(info.symbols), 15)
        json.dumps(info.to_dict())  # must be JSON-serializable


if __name__ == "__main__":
    unittest.main()
