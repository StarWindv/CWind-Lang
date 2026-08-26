"""bug-33 regression: numeric ``as`` casts whose target is a type alias.

The alias (e.g. ``u32`` from ``std::prelude`` or a local ``typedef``)
expands to a numeric builtin, so ``0 as u32`` must be accepted and the
typed AST must carry the expanded target type (``UInt32``) so the
backend's ``cg_expr_cast`` picks the right width.
"""

import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

import harness  # noqa: E402


class Bug33CastAliasTests(harness.CaseAssertionsMixin):
    def test_alias_target(self):
        """原始 bug-33 复现: `0 as u32` 的 u32 是别名, 必须放行."""
        self.assert_case("bug33", "alias_target")

    def test_non_numeric_alias_rejected(self):
        """别名指向非数值类型 (String) 时仍要报错."""
        self.assert_case("bug33", "non_numeric_alias")


if __name__ == "__main__":
    unittest.main()
