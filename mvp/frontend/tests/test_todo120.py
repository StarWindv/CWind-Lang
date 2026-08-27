"""todo-120 regression: FFI may return ``*const S`` / ``*mut S`` struct
pointers and the SA accepts dereferencing them into usable const CWind
objects (backend performs the C-layout -> CWind-blob conversion).

The frontend here only confirms the source parses and passes SA clean; the
full C-layout layout round-trip is asserted by the ``pipeline_cffi_strptr_deref``
CTest fixture.
"""

import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

import harness  # noqa: E402


class Todo120StructPtrReturnTests(harness.CaseAssertionsMixin):
    def test_ffi_struct_ptr_deref(self):
        """extern 返回 *const S / *mut S 结构体指针可解引用 (SA 放行)."""
        self.assert_case("todo120", "ffi_struct_ptr_deref")

    def test_return_must_point_to_struct_only(self):
        """结构体指针返回/解引用的 .wind 保持 SA-clean (无错误消息)."""
        text = "extern \"C\" { fn get() -> *const Big; }\nstruct Big { x: Int32 }\n"
        text += "fn main() -> Int { let p: *const Big = get(); let b: Big = *p; "
        text += "print(b.x); return 0; }\n"
        self.assert_source(text, {"kind": "clean"})


if __name__ == "__main__":
    unittest.main()
