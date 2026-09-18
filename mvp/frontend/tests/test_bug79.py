"""bug-79: a method receiving ``&self`` must not consume its receiver.

Rust parity (E0507): ``self`` behind a reference cannot be moved.  The
for-in desugar lowers to ``self.into_iter()``, and ``IntoIterator`` is
implemented for ``Vector<T>`` by value — the old todo-186 handle-identity
exemption let containers pass through a borrowed receiver, silently
moving elements out of the borrow (and blocking the container ``Clone``
impls).  By-value ``self`` receivers and by-value iteration stay legal.
"""

import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

import harness


class TestBug79(harness.CaseAssertionsMixin, unittest.TestCase):
    def test_for_in_borrowed_self_rejected(self):
        # The motivating shape: ``for ele in self`` inside ``&self``.
        self.assert_case("bug79", "for_in_borrowed_self_rejected")

    def test_consuming_method_on_borrowed_self_rejected(self):
        # Direct call of a by-value method through ``&self``.
        self.assert_case("bug79", "consuming_method_on_borrowed_self_rejected")

    def test_mut_self_cannot_consume(self):
        # ``&mut self`` borrows as well: moving out is still E0507.
        self.assert_case("bug79", "mut_self_cannot_consume")

    def test_for_in_borrowed_local_rejected(self):
        # The rule is general: any borrowed receiver, not just ``self``.
        self.assert_case("bug79", "for_in_borrowed_local_rejected")

    def test_by_value_self_can_consume(self):
        # Ownership is fine when the receiver is by value.
        self.assert_case("bug79", "by_value_self_can_consume")


if __name__ == "__main__":
    unittest.main()
