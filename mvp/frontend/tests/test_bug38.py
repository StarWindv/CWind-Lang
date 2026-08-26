"""bug-38 regression: generic-collapsed argument types are checked.

The original report ("SA 没有检查泛型坍缩后的类型与传入值是否匹配")
turned out to be a false positive from a stale tool: the current SA
already reports ``argument 1 of 'set' must be String, got Int`` for a
``Box<String>`` receiver whose method expects ``T``.  This module pins
that behavior so the check cannot silently regress.
"""

import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

import harness  # noqa: E402


class Bug38CollapseCheckTests(harness.CaseAssertionsMixin):
    def test_collapse_mismatch_checked(self):
        """泛型坍缩 (T -> String) 后传入类型不匹配必须报错."""
        self.assert_case("bug38", "collapse_mismatch_checked")


if __name__ == "__main__":
    unittest.main()
