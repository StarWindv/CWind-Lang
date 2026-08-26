"""bug-39 regression: unknown methods on concrete receiver types must be
reported (``a.unwrap_of("")`` used to pass SA silently with an opaque
type).  Generic opaque receivers (bare ``T``, trait-bound method calls)
stay tolerated because the method may only exist after instantiation.
"""

import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

import harness  # noqa: E402


class Bug39UnknownMethodTests(harness.CaseAssertionsMixin):
    def test_unknown_method(self):
        """具体接收者类型上的未知方法必须报错 (bug-39 原始复现)."""
        self.assert_case("bug39", "unknown_method")

    def test_generic_opaque_tolerated(self):
        """泛型 opaque 接收者上的方法调用仍被容忍 (trait bound 等)."""
        self.assert_case("bug39", "generic_opaque_tolerated")


if __name__ == "__main__":
    unittest.main()
