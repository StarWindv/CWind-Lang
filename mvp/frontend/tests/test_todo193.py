"""todo-193: borrowed-sequence types ``&[T]`` / ``&mut [T]``.

``&[T]`` is syntactic sugar for ``&Vector<T>`` (Rust slice-reference
semantics; the bare ``[T]`` form stays an error since only the borrowed
shape is meaningful).  Covering:

* parse desugar: ``&[u8]`` -> ``Type(Vector, [u8], ref=True)`` and the
  fixed-array form ``&[T; N]`` keeps its flat ``[T; N]`` spelling;
* type positions: params, ``&mut`` params, return positions;
* argument position: vector-literal elements bind to the formal's
  element type via the mismatch re-check (``write(&[1, 2])`` against a
  ``&[u8]`` formal);
* regression: ``&[T; N]`` fixed-array borrows, repeat literals still
  need an explicit array target (todo-197), bare ``[T]`` rejected.
"""

import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

import harness

from cwind_frontend import parse_source
from cwind_frontend import run_sa_with_errors

T193 = "todo193"


def _parse_type(src: str):
    """Type annotation of a one-param fn's single parameter."""
    prog = parse_source(f"fn f({src}) {{ }}")
    return prog.items[0].params[0].type


class TestSliceTypeDesugar(unittest.TestCase):
    def test_slice_ref_is_vector_ref(self):
        t = _parse_type("vals: &[u8]")
        self.assertEqual(t.name, "Vector")
        self.assertEqual(len(t.args), 1)
        self.assertEqual(t.args[0].name, "u8")
        self.assertTrue(t.ref)
        self.assertFalse(t.mut)

    def test_slice_mut_ref(self):
        t = _parse_type("vals: &mut [u8]")
        self.assertEqual(t.name, "Vector")
        self.assertTrue(t.ref)
        self.assertTrue(t.mut)

    def test_fixed_array_ref_keeps_flat_name(self):
        t = _parse_type("vals: &[u8; 4]")
        self.assertEqual(t.name, "[u8; 4]")
        self.assertEqual(t.args, [])
        self.assertTrue(t.ref)

    def test_nested_slice_element(self):
        t = _parse_type("vals: &[Vector<Int>]")
        self.assertEqual(t.name, "Vector")
        self.assertEqual(t.args[0].name, "Vector")
        self.assertEqual(t.args[0].args[0].name, "Int")


class TestSliceTypeRejected(unittest.TestCase):
    def test_bare_slice_type_is_not_a_type(self):
        with self.assertRaises(Exception):
            parse_source("fn f(vals: [u8]) { }")


class TestTodo193Pipeline(harness.CaseAssertionsMixin):
    def test_slice_ref_param_and_literal_arg(self):
        self.assert_case(T193, "slice_ref_param")

    def test_slice_mut_ref_param(self):
        self.assert_case(T193, "slice_mut_ref_param")

    def test_slice_ref_return_position(self):
        self.assert_case(T193, "slice_ref_return")

    def test_slice_in_trait_default_body(self):
        # &[Self] sugar in a trait default body signature (&Vector<Self>)
        self.assert_case(T193, "slice_self_element")

    def test_fixed_array_ref_regression(self):
        self.assert_case(T193, "slice_fixed_ref")

    def test_repeat_literal_still_needs_target(self):
        # todo-197: 实参期望单遍下传落地前保持拒绝
        self.assert_case(T193, "slice_repeat_rejected")

    def test_mismatched_element_rejected(self):
        self.assert_case(T193, "slice_element_mismatch")


if __name__ == "__main__":
    unittest.main()
