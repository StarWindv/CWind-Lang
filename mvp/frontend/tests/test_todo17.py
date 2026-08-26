"""todo-17: numeric ``as`` casts (data-driven via cases/todo17).

``expr as T`` converts between numeric types with the backend's existing
scalar-coercion semantics: int->int truncates / sign-extends at the
target width (two's complement), float->int truncates toward zero,
int<->float convert numerically.  Precedence follows Rust: unary binds
tighter than ``as``, and ``as`` binds tighter than arithmetic and
comparison.  Constant contexts fold casts with matching semantics.

Non-numeric operands / targets are rejected with distinct diagnostics.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

import harness  # noqa: E402

CASES = [
    "numeric_conversions",
    "precedence",
    "const_fold_casts",
    "bool_operand_rejected",
    "non_numeric_rejected",
]


class Todo17CastTests(harness.CaseAssertionsMixin):
    def test_case(self):
        for name in CASES:
            with self.subTest(case=name):
                self.assert_case("todo17", name)


if __name__ == "__main__":
    unittest.main()
