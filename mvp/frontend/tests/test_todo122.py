"""todo-122: associated constants in ``extra`` blocks.

``extra Point { const MAX: Int32 = 99; }`` declares a constant scoped to
the owner type, read as ``Point::MAX`` (or ``Self::MAX`` inside methods).
Assignment -- plain or compound -- is rejected like top-level consts, and
the value is type-checked / range-checked / refined exactly like one.
"""

import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

import harness  # noqa: E402


class Todo122AssocConstTests(harness.CaseAssertionsMixin):
    def test_basic(self):
        """关联常量声明 + Type::NAME / Self::NAME 访问通过 SA."""
        self.assert_case("todo122", "basic")

    def test_assign_rejected(self):
        """关联常量不可赋值 (顶层 const 同语义)."""
        self.assert_case("todo122", "assign_rejected")

    def test_compound_rejected(self):
        """复合赋值同样拒绝."""
        self.assert_case("todo122", "compound_rejected")

    def test_type_mismatch(self):
        """初始化值类型不匹配报错."""
        self.assert_case("todo122", "type_mismatch")

    def test_duplicate(self):
        """同块内重名关联常量报错."""
        self.assert_case("todo122", "duplicate")

    def test_unknown_member(self):
        """未声明的关联常量按静态成员缺失报错."""
        self.assert_case("todo122", "unknown_member")

    def test_value_error_propagates(self):
        """初始化表达式自身的语义错误照常上报 (除零)."""
        self.assert_case("todo122", "value_error_propagates")


if __name__ == "__main__":
    unittest.main()
