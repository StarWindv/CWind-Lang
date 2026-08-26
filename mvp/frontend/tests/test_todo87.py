"""todo-87 regression: variadic ``...`` parameters on extern functions.

Only extern blocks may declare a trailing ``...``; at least one fixed
parameter must precede it, calls must pass >= the fixed parameter count,
and extra arguments are checked by no fixed parameter (C vararg
semantics).  The backend maps the call to an LLVM variadic C function:
narrow integers promote to i32, Bool to i32, Float to double,
Int64/UInt64 pass as-is and String/raw pointers cross by address.
"""

import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

import harness  # noqa: E402


class Todo87VariadicFfiTests(harness.CaseAssertionsMixin):
    def test_extern_variadic(self):
        """extern 块内 '...' 声明与调用通过 SA."""
        self.assert_case("todo87", "extern_variadic")

    def test_non_extern_rejected(self):
        """非 extern 上下文的 '...' 在 parse 期拒绝."""
        self.assert_case("todo87", "non_extern_rejected")

    def test_no_fixed_param(self):
        """'...' 前至少需要一个固定形参 (C 标准)."""
        self.assert_case("todo87", "no_fixed_param")

    def test_call_below_fixed(self):
        """实参个数不足固定形参个数时 SA 报错."""
        self.assert_case("todo87", "call_below_fixed")


if __name__ == "__main__":
    unittest.main()
