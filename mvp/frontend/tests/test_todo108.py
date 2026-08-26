"""todo-108 regression: raw pointers whose pointee is an enum
(``*mut E`` / ``*const E``) may cross the FFI boundary.

Semantics match C opaque handles (``MyEnum*`` / ``void*``): pointer
values pass through by address in both parameter and return positions,
with no content conversion or write-back.  Same-type pointers keep the
existing ``==``/``!=`` address comparison.
"""

import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

import harness  # noqa: E402


class Todo108EnumPtrTests(harness.CaseAssertionsMixin):
    def test_enum_ptr_extern(self):
        """extern 形参/返回位接受枚举指针, 同型指针判等可用."""
        self.assert_case("todo108", "enum_ptr_extern")


if __name__ == "__main__":
    unittest.main()
