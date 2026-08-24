"""Unit tests for todo-47 syntax: ``&mut self`` replaces ``mut &self``.

Input programs live in ``cases/todo47``; parsing structure and semantic
assertions stay in this module.  The receiver forms are:

    self        owned, immutable binding
    mut self    owned, mutable binding
    &self       borrowed, immutable
    &mut self   borrowed, mutable  (todo-47: postfix ``mut``)
``mut &self`` is rejected with a pointer to the new spelling.
"""

import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

import harness

from cwind_frontend import ParseError, parse_source

T47 = "todo47"

HEADER = "struct S { x: Int }\nextra S {\n    %s\n}\n"


def _first_method(sig: str):
    prog = parse_source(HEADER % sig)
    for item in prog.items:
        methods = getattr(item, "methods", None)
        if methods:
            return methods[0]
        fns = getattr(item, "fns", None)
        if fns:
            return fns[0]
    raise AssertionError("no function found for " + sig)


class TestReceiverParsing(unittest.TestCase):
    def test_ref_self_immutable(self):
        p = _first_method("fn a(&self) {}").params[0]
        self.assertEqual(p.name, "self")
        self.assertFalse(p.mutable)
        self.assertIsNotNone(p.type)
        self.assertTrue(p.type.ref)

    def test_ref_mut_self_mutable(self):
        p = _first_method("fn b(&mut self) {}").params[0]
        self.assertEqual(p.name, "self")
        self.assertTrue(p.mutable)
        self.assertIsNotNone(p.type)
        self.assertTrue(p.type.ref)

    def test_owned_self_immutable(self):
        p = _first_method("fn c(self) {}").params[0]
        self.assertFalse(p.mutable)
        self.assertIsNone(p.type)

    def test_owned_mut_self_mutable(self):
        p = _first_method("fn d(mut self) {}").params[0]
        self.assertTrue(p.mutable)
        self.assertIsNone(p.type)

    def test_named_param_still_takes_leading_mut(self):
        fn = _first_method("fn e(&mut self, mut n: Int) { n += 1; }")
        self.assertTrue(fn.params[1].mutable)
        self.assertFalse(fn.params[1].type.ref)


class TestOldSyntaxRejected(unittest.TestCase):
    def test_error_message_points_to_new_form(self):
        with self.assertRaises(ParseError) as cm:
            parse_source(HEADER % "fn e(mut &self) {}")
        self.assertIn("'&mut self'", str(cm.exception))
        self.assertIn("mut &", str(cm.exception))

    def test_error_position_is_the_ampersand(self):
        src = HEADER % "fn e(mut &self) {}"
        line = src.splitlines()[2]
        col = line.index("&") + 1
        try:
            parse_source(src)
        except ParseError as exc:
            self.assertEqual((exc.line, exc.column), (3, col))

    def test_old_and_new_cannot_mix(self):
        with self.assertRaises(ParseError):
            parse_source(HEADER % "fn f(mut &mut self) {}")


class TestTodo47Pipeline(harness.CaseAssertionsMixin):
    def test_ref_mut_self_can_mutate_fields(self):
        self.assert_case(T47, "ref_mut_self")

    def test_ref_self_readonly_regression(self):
        self.assert_case(T47, "ref_self_readonly")

    def test_owned_mut_self_accepted(self):
        self.assert_case(T47, "owned_mut_self")

    def test_trait_decl_and_impl_use_ref_mut_self(self):
        self.assert_case(T47, "trait_ref_mut_self")

    def test_write_through_ref_self_rejected(self):
        self.assert_case(T47, "ref_self_write_rejected")

    def test_old_syntax_is_a_parse_error(self):
        self.assert_case(T47, "old_mut_amp_rejected")


if __name__ == "__main__":
    unittest.main()
