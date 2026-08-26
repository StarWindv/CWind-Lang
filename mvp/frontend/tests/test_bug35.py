"""bug-35 regression: fixed-length inline array fields break ``extra``
impl parsing (repeat array literals ``[x; N]``).

The original reproducer put ``Self { [0 as UInt32; 624], ... }`` inside an
extra method.  Three defects combined:

1. ``_brace_is_struct_construct`` treated the ``;`` inside ``[0; 624]`` as
   a statement separator, so ``Self {`` was not recognized as a struct
   construct at all;
2. the ``[x; N]`` repeat-literal form was not parseable (only the comma
   form existed);
3. SA + backend had no repeat handling and element aliases (``u32``) were
   not expanded inside array types.
"""

import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

import harness  # noqa: E402


class Bug35RepeatLiteralTests(harness.CaseAssertionsMixin):
    def test_repeat_literal_in_extra(self):
        """原始 bug-35 复现: extra 构造含重复字面量的结构体."""
        self.assert_case("bug35", "repeat_literal_in_extra")

    def test_repeat_literal_plain(self):
        """普通位置的 `[x; N]` 定长数组字面量."""
        self.assert_case("bug35", "repeat_literal_plain")

    def test_repeat_count_mismatch(self):
        """重复次数与目标数组长度不一致要报错."""
        self.assert_case("bug35", "repeat_count_mismatch")

    def test_repeat_non_array_target(self):
        """重复字面量只能落到定长数组类型上."""
        self.assert_case("bug35", "repeat_non_array")

    def test_repeat_zero(self):
        """N 必须为正数."""
        self.assert_case("bug35", "repeat_zero")


if __name__ == "__main__":
    unittest.main()
