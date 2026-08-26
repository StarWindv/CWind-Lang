"""todo-74: ``!`` negation operators (data-driven via cases/todo74).

Two contract layers:

- Bool logical negation on *variables* (previously only exercised on
  literals): let initialization, reassignment, while conditions, match
  guards, field/index operands, call arguments and ``!!x`` chains;
- Rust-style bitwise NOT on integer operands (todo-74 extension):
  ``!int_expr`` keeps the operand's width, so ``!5: Int`` is ``-6`` and
  ``!0: UInt`` is the full-width mask.  Floats / strings / containers are
  rejected with one precise diagnostic.

Each expectation lives beside its source as ``cases/todo74/<name>.json``.
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
    "bool_variable_contexts",
    "bitwise_not_widths",
    "not_binds_tighter_than_comparison",
    "string_operand",
    "float_operand",
    "container_operand",
]


class Todo74CaseTests(harness.CaseAssertionsMixin):
    def test_case(self):
        for name in CASES:
            with self.subTest(case=name):
                self.assert_case("todo74", name)


if __name__ == "__main__":
    unittest.main()
