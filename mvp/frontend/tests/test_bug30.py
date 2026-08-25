"""Regression tests for bug-30: ``main`` receiving program arguments.

``main`` 的签名只允许两种形态:

    fn main()                      无参
    fn main(args: Vector<String>)  程序参数 (后端由 C 的 argc/argv 注入)

其余参数形态在 SA 阶段拒绝, 而不是被后端静默传入空句柄。
端到端行为见 CTest ``pipeline_mainargs``。

Input programs live in ``cases/bug30``; assertions stay in this module.
"""

import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

import harness

B30 = "bug30"


class MainArgsAccepted(harness.CaseAssertionsMixin):
    def test_vector_string_args(self):
        """bug-30 目标形态: `fn main(args: Vector<String>)`."""
        self.assert_case(B30, "args_vector")

    def test_zero_params(self):
        self.assert_case(B30, "no_params")

    def test_non_main_functions_unaffected(self):
        self.assert_case(B30, "non_main_fn_ok")


class MainArgsRejected(harness.CaseAssertionsMixin):
    def test_wrong_element_type(self):
        self.assert_case(B30, "wrong_elem")

    def test_two_params(self):
        self.assert_case(B30, "two_params")


class ErrorMessage(unittest.TestCase):
    def test_scalar_param_rejected_with_message(self):
        result = harness.run_pipeline("fn main(argc: Int) { print(0); }")
        self.assertEqual("sa_err", result["kind"])
        self.assertTrue(
            any(
                "must be 'Vector<String>'" in e.message
                for e in result["errors"]
            )
        )

    def test_error_points_at_the_parameter(self):
        result = harness.run_pipeline("fn main(argc: Int) { print(0); }")
        self.assertEqual((1, 9), (result["errors"][0].line,
                                  result["errors"][0].column))


if __name__ == "__main__":
    unittest.main()
