"""Regression tests for bug-24: ``main`` return-value validation.

``main`` 的返回值只能成为进程退出码, 因此签名上只接受:

    整数类型 (Int/UInt/Int8/UInt8/Int32/UInt32/Int64/UInt64/Byte)
    None (显式或省略)
    never ``!``            (Rust 语义: 永不返回的 main 合法)

其余返回类型必须在 SA 阶段报错, 而不是被后端静默丢弃
(修复前 ``fn main() -> Float64 { -1 }`` 编译通过且退出码恒为 0)。

Input programs live in ``cases/bug24``; assertions stay in this module.
"""

import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

import harness

B24 = "bug24"


class TestMainSignatureRejected(harness.CaseAssertionsMixin):
    def test_float_tail_expression(self):
        """原始 bug-24 复现: `fn main() -> Float64 { -1 }` 必须被拒绝."""
        self.assert_case(B24, "float_tail")

    def test_string_return(self):
        self.assert_case(B24, "string_return")

    def test_bool_return(self):
        self.assert_case(B24, "bool_return")

    def test_struct_return(self):
        self.assert_case(B24, "struct_return")

    def test_vector_return(self):
        self.assert_case(B24, "vector_return")

    def test_reference_return_rejected(self):
        self.assert_case(B24, "ref_int_rejected")

    def test_alias_to_float_expanded_in_message(self):
        self.assert_case(B24, "float_alias")


class TestMainSignatureAccepted(harness.CaseAssertionsMixin):
    def test_implicit_none(self):
        self.assert_case(B24, "implicit_none")

    def test_explicit_none(self):
        self.assert_case(B24, "explicit_none")

    def test_int_exit_code(self):
        self.assert_case(B24, "int_exit_code")

    def test_u64_exit_code(self):
        self.assert_case(B24, "u64_exit_code")

    def test_never_main_is_legal(self):
        self.assert_case(B24, "never_main")

    def test_method_named_main_is_not_the_entry_point(self):
        self.assert_case(B24, "method_named_main_ok")


class TestEmptyReturnStillChecked(harness.CaseAssertionsMixin):
    """空返回值场景由既有的通用检查兜底, bug-24 不得使其回归."""

    def test_empty_return_with_declared_type(self):
        self.assert_case(B24, "empty_return_with_type")

    def test_missing_tail_value(self):
        self.assert_case(B24, "missing_tail_value")


class TestErrorMessage(unittest.TestCase):
    def test_error_points_at_the_offending_return_type(self):
        result = harness.run_pipeline(
            harness.source(B24, "float_tail"), stage="sa"
        )
        self.assertEqual(len(result["errors"]), 1)
        err = result["errors"][0]
        self.assertEqual((err.line, err.column), (1, 14))

    def test_message_names_allowed_kinds(self):
        result = harness.run_pipeline("fn main() -> Bool { true }")
        self.assertTrue(
            any(
                "integer type or 'None'" in e.message for e in result["errors"]
            )
        )


if __name__ == "__main__":
    unittest.main()
