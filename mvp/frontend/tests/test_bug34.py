"""bug-34 regression: ``Self`` binds to the owner struct inside extra
bodies for ``let`` declarations and non-self parameters.

The bug: inside ``extra User`` a tail return without ``return``
(``let mut u: Self = Self { ... }; u``) failed with
"Return type mismatch: expected User, got Self" because the declared
type stayed the raw string ``Self`` while the function's return type
had already been resolved to the owner.
"""

import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

import harness  # noqa: E402


class Bug34SelfBindingTests(harness.CaseAssertionsMixin):
    def test_tail_return_self(self):
        """原始 bug-34 复现: extra 内 `let ...: Self` + 尾返回 Self."""
        self.assert_case("bug34", "tail_return_self")

    def test_self_params_and_lets(self):
        """Self 形参与局部声明的类型都要绑定到所属类型."""
        self.assert_case("bug34", "self_params_and_lets")


if __name__ == "__main__":
    unittest.main()
