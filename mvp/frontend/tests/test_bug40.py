"""bug-40 regression: ``pub fn`` / ``pub static`` members are allowed inside
``extern "C"`` blocks (previously the parser demanded that visibility live
only on the block itself and died with "expected function name").

Visibility of an extern member is the OR of the block-level ``pub`` and the
member's own ``pub``, which also participates in the module export surface
(``use m::member;`` resolves when the member is pub even if the block is not).
"""

import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

import harness  # noqa: E402


class Bug40PubExternMemberTests(harness.CaseAssertionsMixin):
    def test_pub_fn_member(self):
        """非 pub 块内 pub fn/pub static 可声明且同文件可见."""
        self.assert_case("bug40", "pub_fn_member")

    def test_pub_block_mixed(self):
        """块级 pub 与成员级 pub 混用不再解析失败."""
        self.assert_case("bug40", "pub_block_mixed")


if __name__ == "__main__":
    unittest.main()
