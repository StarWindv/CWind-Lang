"""Unit tests for todo-58: ``Int16`` / ``UInt16`` numeric types.

Input programs live in ``cases/todo58``; pipeline expectations sit in
``<name>.json`` sidecars.  The new types mirror ``Int8``/``UInt8``
end-to-end: literal range checking (SA), mixed-width arithmetic
promotion (Int16/UInt16 share rank 2 with Int/UInt), fn return width
checks, ``From<String>``/``Into<String>``, refinement types, main
return and CFFI extern signatures.
"""

import dataclasses
import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

import harness

from cwind_frontend import parse_source, run_sa
from cwind_frontend.ast_components.ast import BinOp

T58 = "todo58"


def _binop_ann(src: str):
    prog = parse_source(src)
    run_sa(prog)

    def walk(node):
        yield node
        for f in dataclasses.fields(node):
            if f.name in ("line", "column"):
                continue
            v = getattr(node, f.name)
            if isinstance(v, A.Node):
                yield from walk(v)
            elif isinstance(v, list):
                for x in v:
                    if isinstance(x, A.Node):
                        yield from walk(x)

    from cwind_frontend.ast_components import ast as A

    for n in walk(prog):
        if isinstance(n, BinOp):
            return n._typed_ann["type"]["name"]
    return None


class TestTodo58Pipeline(harness.CaseAssertionsMixin):
    def test_bounds_accepted(self):
        self.assert_case(T58, "int16_uint16_bounds_ok")

    def test_int16_overflow_rejected(self):
        self.assert_case(T58, "int16_overflow_rejected")

    def test_uint16_and_int16_overflow_rejected(self):
        self.assert_case(T58, "uint16_overflow_rejected")

    def test_mixed_width_promotion(self):
        self.assert_case(T58, "mixed_width_promotion_ok")

    def test_fn_return_width_rejected(self):
        self.assert_case(T58, "fn_return_width_rejected")

    def test_from_into_string(self):
        self.assert_case(T58, "from_into_string_ok")

    def test_refinement_types(self):
        self.assert_case(T58, "refinement_int16_ok")

    def test_refinement_dead_bounds_warn(self):
        self.assert_case(T58, "refinement_int16_dead_bound")

    def test_main_return_uint16(self):
        src = "fn main() -> UInt16 { return 7; }"
        self.check_outcome(harness.run_pipeline(src), {})

    def test_extern_scalar_widths(self):
        self.assert_case(T58, "extern_scalar_widths_ok")


class TestTodo58Promotion(unittest.TestCase):
    CASES = [
        ("Int16", "UInt16", "Int32"),
        ("UInt16", "Int16", "Int32"),
        ("Int", "Int16", "Int32"),
        ("Int16", "Int", "Int32"),
        ("Int16", "UInt", "Int32"),
        ("UInt16", "Byte", "UInt16"),
        ("Int8", "Int16", "Int16"),
        ("UInt8", "UInt16", "UInt16"),
        ("Int16", "Float64", "Float64"),
    ]

    def test_binop_common_type(self):
        for lt, rt, want in self.CASES:
            with self.subTest(lt=lt, rt=rt, want=want):
                src = (
                    f"fn f() -> {want} {{"
                    f" let a: {lt} = 1;"
                    f" let b: {rt} = 2;"
                    " return a + b;"
                    "}"
                )
                got = _binop_ann(src)
                self.assertEqual(got, want)


if __name__ == "__main__":
    unittest.main()
