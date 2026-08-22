"""Unit tests for todo-38 syntax: function pointers, closures, raw pointers."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

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


def _first_let(src: str) -> LetStmt:
    """Parse ``src`` and return the first ``let`` inside the first function."""
    prog = parse_source(src)
    fn = prog.items[0]
    for stmt in fn.body.stmts:
        if isinstance(stmt, LetStmt):
            return stmt
    raise AssertionError("no let statement found")


def _last_let(src: str) -> LetStmt:
    """Parse ``src`` and return the last ``let`` inside the first function."""
    prog = parse_source(src)
    fn = prog.items[0]
    for stmt in reversed(fn.body.stmts):
        if isinstance(stmt, LetStmt):
            return stmt
    raise AssertionError("no let statement found")


def _parse_type_of_let(src: str) -> A.Type:
    return _first_let(src).type


def _has_errors(result) -> bool:
    return bool(result.errors)


class TestFnPointerParsing(unittest.TestCase):
    def test_simple_signature(self):
        t = _parse_type_of_let("fn main() -> Int { let p: fn(Int) -> Int = f; return 0; }")
        self.assertEqual(t.name, "fn(Int) -> Int")
        self.assertEqual(t.args, [])

    def test_multi_arg_signature(self):
        t = _parse_type_of_let(
            "fn main() -> Int { let p: fn(Int, String) -> Bool = f; return 0; }"
        )
        self.assertEqual(t.name, "fn(Int, String) -> Bool")

    def test_no_return(self):
        t = _parse_type_of_let("fn main() -> Int { let p: fn(Int) = f; return 0; }")
        self.assertEqual(t.name, "fn(Int)")

    def test_nested_pointer_arg(self):
        t = _parse_type_of_let(
            "fn main() -> Int { let p: fn(Vector<Int>) -> Int = f; return 0; }"
        )
        self.assertEqual(t.name, "fn(Vector<Int>) -> Int")


class TestClosureParsing(unittest.TestCase):
    def _closure(self, src: str) -> Closure:
        let = _first_let(src)
        assert isinstance(let.value, Closure)
        return let.value

    def test_basic_closure(self):
        c = self._closure(
            "fn main() -> Int { let c: fn(Int) -> Int = |x: Int| -> Int { x * 3 }; return 0; }"
        )
        self.assertEqual(len(c.params), 1)
        self.assertEqual(c.params[0].name, "x")
        self.assertIsNotNone(c.return_type)

    def test_tail_expr_becomes_return(self):
        c = self._closure(
            "fn main() -> Int { let c: fn(Int) -> Int = |x: Int| -> Int { x * 3 }; return 0; }"
        )
        self.assertEqual(len(c.body.stmts), 1)
        self.assertIsInstance(c.body.stmts[0], ReturnStmt)

    def test_zero_param_closure(self):
        c = self._closure(
            "fn main() -> Int { let c: fn() -> Int = || -> Int { 7 }; return 0; }"
        )
        self.assertEqual(len(c.params), 0)


class TestRawPointerParsing(unittest.TestCase):
    def test_const_pointer(self):
        t = _last_let("fn main() -> Int { let v: Int = 1; let q: *const Int = &v; return 0; }").type
        self.assertEqual(t.name, "*const Int")

    def test_mut_pointer(self):
        t = _first_let(
            "fn main() -> Int { let q: *mut User = &u; return 0; }"
        ).type
        self.assertEqual(t.name, "*mut User")


class TestFnPtrSemantics(unittest.TestCase):
    def test_closure_matches_fn_type(self):
        prog = parse_source(
            "fn main() -> Int {"
            " let c: fn(Int) -> Int = |x: Int| -> Int { x + 1 };"
            " return c(1);"
            "}"
        )
        run_sa(prog)  # should not raise

    def test_closure_signature_mismatch_rejected(self):
        result = run_sa_with_errors(
            parse_source(
                "fn main() -> Int {"
                " let c: fn(Int) -> Int = |x: Int| -> String { \"x\" };"
                " return 0;"
                "}"
            )
        )
        self.assertTrue(bool(result.errors))

    def test_indirect_call_checks_arity(self):
        result = run_sa_with_errors(
            parse_source(
                "fn main() -> Int {"
                " let p: fn(Int) -> Int = g;"
                " return p(1, 2);"
                "}"
                "fn g(x: Int) -> Int { return x; }"
            )
        )
        self.assertTrue(bool(result.errors))

    def test_indirect_call_checks_arg_types(self):
        result = run_sa_with_errors(
            parse_source(
                'fn main() -> Int {'
                ' let p: fn(Int) -> Int = g;'
                ' return p("hi");'
                '}'
                "fn g(x: Int) -> Int { return x; }"
            )
        )
        self.assertTrue(bool(result.errors))

    def test_fn_ptr_is_copy_across_calls(self):
        prog = parse_source(
            "fn main() -> Int {"
            " let p: fn(Int) -> Int = g;"
            " let a: Int = call_it(p);"
            " let b: Int = call_it(p);"
            " return a + b;"
            "}"
            "fn g(x: Int) -> Int { return x; }"
            "fn call_it(f: fn(Int) -> Int) -> Int { return f(1); }"
        )
        run_sa(prog)  # passing the same pointer twice must not move it

    def test_raw_pointer_accepts_borrow_init(self):
        prog = parse_source(
            "fn main() -> Int {"
            " let v: Int = 10;"
            " let q: *const Int = &v;"
            " return 0;"
            "}"
        )
        run_sa(prog)

    def test_raw_pointer_rejects_value_init(self):
        result = run_sa_with_errors(
            parse_source(
                "fn main() -> Int {"
                " let v: Int = 10;"
                " let q: *const Int = v;"
                " return 0;"
                "}"
            )
        )
        self.assertTrue(bool(result.errors))


if __name__ == "__main__":
    unittest.main()
