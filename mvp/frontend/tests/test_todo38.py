"""Unit tests for todo-38 syntax: function pointers, closures, raw pointers.

Input programs live in ``cases/todo38``; parsing structure and semantic
assertions stay in this module.
"""

import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

import harness

from cwind_frontend import (
    Closure,
    LetStmt,
    Name,
    ParseError,
    ReturnStmt,
    parse_source,
    run_sa,
    run_sa_with_errors,
)
from cwind_frontend.ast_components import ast as A

T38 = "todo38"


def _prog(name: str):
    return parse_source(harness.source(T38, name))


def _first_let(prog) -> LetStmt:
    """Return the first ``let`` inside the first function."""
    fn = prog.items[0]
    for stmt in fn.body.stmts:
        if isinstance(stmt, LetStmt):
            return stmt
    raise AssertionError("no let statement found")


def _last_let(prog) -> LetStmt:
    """Return the last ``let`` inside the first function."""
    fn = prog.items[0]
    for stmt in reversed(fn.body.stmts):
        if isinstance(stmt, LetStmt):
            return stmt
    raise AssertionError("no let statement found")


def _parse_type_of_let(name: str) -> A.Type:
    return _first_let(_prog(name)).type


class TestFnPointerParsing(unittest.TestCase):
    def test_simple_signature(self):
        t = _parse_type_of_let("fnptr_simple")
        self.assertEqual(t.name, "fn(Int) -> Int")
        self.assertEqual(t.args, [])

    def test_multi_arg_signature(self):
        t = _parse_type_of_let("fnptr_multi_arg")
        self.assertEqual(t.name, "fn(Int, String) -> Bool")

    def test_no_return(self):
        t = _parse_type_of_let("fnptr_no_return")
        self.assertEqual(t.name, "fn(Int)")

    def test_nested_pointer_arg(self):
        t = _parse_type_of_let("fnptr_nested_arg")
        self.assertEqual(t.name, "fn(Vector<Int>) -> Int")


class TestClosureParsing(unittest.TestCase):
    def _closure(self, name: str) -> Closure:
        let = _first_let(_prog(name))
        assert isinstance(let.value, Closure)
        return let.value

    def test_basic_closure(self):
        c = self._closure("closure_basic")
        self.assertEqual(len(c.params), 1)
        self.assertEqual(c.params[0].name, "x")
        self.assertIsNotNone(c.return_type)

    def test_tail_expr_becomes_return(self):
        c = self._closure("closure_basic")
        self.assertEqual(len(c.body.stmts), 1)
        self.assertIsInstance(c.body.stmts[0], ReturnStmt)

    def test_zero_param_closure(self):
        c = self._closure("closure_zero_param")
        self.assertEqual(len(c.params), 0)


class TestRawPointerParsing(unittest.TestCase):
    def test_const_pointer(self):
        t = _last_let(_prog("raw_const_pointer")).type
        self.assertEqual(t.name, "*const Int")

    def test_mut_pointer(self):
        t = _first_let(_prog("raw_mut_pointer")).type
        self.assertEqual(t.name, "*mut User")


class TestFnPtrSemantics(unittest.TestCase):
    def test_closure_matches_fn_type(self):
        run_sa(_prog("closure_matches_fn_type"))  # should not raise

    def test_closure_signature_mismatch_rejected(self):
        result = run_sa_with_errors(_prog("closure_signature_mismatch"))
        self.assertTrue(bool(result.errors))

    def test_indirect_call_checks_arity(self):
        result = run_sa_with_errors(_prog("indirect_call_arity"))
        self.assertTrue(bool(result.errors))

    def test_indirect_call_checks_arg_types(self):
        result = run_sa_with_errors(_prog("indirect_call_arg_types"))
        self.assertTrue(bool(result.errors))

    def test_fn_ptr_is_copy_across_calls(self):
        run_sa(_prog("fnptr_copy_across_calls"))
        # passing the same pointer twice must not move it

    def test_raw_pointer_accepts_borrow_init(self):
        run_sa(_prog("raw_const_pointer"))

    def test_raw_pointer_rejects_value_init(self):
        result = run_sa_with_errors(_prog("raw_pointer_value_init_rejected"))
        self.assertTrue(bool(result.errors))


if __name__ == "__main__":
    unittest.main()
