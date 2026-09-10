"""bug-74: statement-position (discarded) match/if arms carrying a
value must be rejected.

rustc parity (E0308): ``if`` / ``if-else`` / ``match`` used as a
statement expect their arms to be ``()``; a non-diverging arm whose
tail expression yields a value is an error, and the fix suggests
``return``.  The motivating case was a fib that silently computed the
``n <= 1`` block value, fell through, underflowed u64 and recursed
until the stack blew (runtime crash at every opt level).

Diverging arms (return/break/continue/``!`` call), the if-without-else
empty fallback arm, trailing-semicolon discards, and every
expression-position use (``let`` init / tail bare match / ``return``)
stay legal — see ``legal_forms_ok``.
"""

import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

import harness


class TestBug74(harness.CaseAssertionsMixin, unittest.TestCase):
    def test_if_block_value_discarded(self):
        # The motivating fib: ``if n <= 1 { n }`` at statement position.
        self.assert_case("bug74", "if_block_value_discarded")

    def test_if_else_block_values_discarded(self):
        # Both arms carry values — both are reported (rustc reports
        # each arm separately).
        self.assert_case("bug74", "if_else_block_values_discarded")

    def test_expr_arms_discarded(self):
        # Statement-position expression-arm match carries values too.
        self.assert_case("bug74", "expr_arms_discarded")

    def test_legal_forms_still_pass(self):
        # Diverging arms, empty else-fallback, semicolon discards and
        # all expression-position forms must survive the new check.
        self.assert_case("bug74", "legal_forms_ok")


if __name__ == "__main__":
    unittest.main()
