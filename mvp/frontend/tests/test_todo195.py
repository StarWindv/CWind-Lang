"""todo-195: match block-arm value semantics (+ bug-71 Bool arms).

Block arms now participate in arm-type unification like Rust: a
non-diverging block arm's tail expression is the arm's value (flagged
``arm_tail`` for the backend to emit it into the result slot).  Diverging
block arms keep unifying as ``!``; statement-position matches still
discard values but record the unified type for nested-tail reads.

Covering: valued block arms, mixed block/expr arms, diverging+value
mixes, nested match tails, trailing-semicolon discard, valueless arms
still rejected (unit type is todo-162), exhaustiveness and literal-range
checks, and bug-71 (``match cond { true => .., false => .. }`` is
exhaustive).
"""

import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

import harness

from cwind_frontend import parse_source, run_sa_with_errors
from cwind_frontend.ast_components import ast as A

T195 = "todo195"


def _sa_text(text: str):
    prog = parse_source(text)
    return prog, run_sa_with_errors(prog)


class TestTodo195Pipeline(harness.CaseAssertionsMixin):
    def test_valued_block_arms(self):
        # (flipped legacy negative) 两臂尾值参与合一
        self.assert_case("sa", "match_block_arms_in_expr_position")

    def test_diverging_plus_valued_blocks(self):
        self.assert_case("sa", "match_value_nondiverging_blocks")

    def test_valueless_block_rejected(self):
        self.assert_case(T195, "valueless_block_rejected")

    def test_semicolon_tail_discards_value(self):
        self.assert_case(T195, "semicolon_tail_rejected")

    def test_incompatible_block_tails(self):
        self.assert_case(T195, "block_tails_incompatible")

    def test_statement_position_valued_block(self):
        self.assert_case(T195, "statement_valued_block")

    def test_bool_exhaustive(self):
        # bug-71: Bool 两臂字面量全覆盖即穷尽
        self.assert_case(T195, "bool_exhaustive")

    def test_bool_guarded_arm_not_exhaustive(self):
        # guard 臂不算无条件覆盖, true 臂带 guard 时仍缺 false
        self.assert_case(T195, "bool_guarded_not_exhaustive")


class TestTodo195Annotations(unittest.TestCase):
    def test_arm_tail_flags_and_common_type(self):
        src = harness.source("sa", "match_block_arms_in_expr_position")
        prog, res = _sa_text(src)
        self.assertEqual(res.errors, [])
        m = TestTodo195Annotations._first_match(prog)
        self.assertEqual(m._typed_ann["type"]["name"], "Int")
        for arm in m.arms:
            self.assertTrue(arm._typed_ann["arm_diverges"] is False)
            self.assertEqual(arm._typed_ann["body_type"]["name"], "Int")
            tail = arm.body.stmts[-1].expr
            self.assertTrue(tail._typed_ann.get("arm_tail"))

    def test_statement_match_records_type_for_nested_tails(self):
        # 嵌套: 内层语句位 match (块臂带值) 写 ann.type, 外层块臂读到它
        src = harness.source(T195, "nested_match_tail")
        prog, res = _sa_text(src)
        self.assertEqual(res.errors, [])
        matches = TestTodo195Annotations._all_matches(prog)
        inner = [m for m in matches if len(m.arms) == 2 and all(
            isinstance(a.body, A.Block) for a in m.arms
        )]
        self.assertTrue(inner)
        for m in inner:
            self.assertEqual(m._typed_ann["type"]["name"], "Int")

    @staticmethod
    def _first_match(prog):
        matches = TestTodo195Annotations._all_matches(prog)
        if not matches:
            raise AssertionError("no match statement found")
        return matches[0]

    @staticmethod
    def _all_matches(prog):
        found = []
        stack = [prog]
        attrs = (
            "items", "stmts", "value", "expr", "body", "subject", "arms",
            "guard", "operand", "args", "elems", "obj", "callee",
        )
        while stack:
            n = stack.pop()
            if isinstance(n, A.MatchStmt):
                found.append(n)
            for a in attrs:
                v = getattr(n, a, None)
                if isinstance(v, list):
                    stack.extend(x for x in v if hasattr(x, "__dict__"))
                elif v is not None and hasattr(v, "__dict__"):
                    stack.append(v)
        return found


if __name__ == "__main__":
    unittest.main()
