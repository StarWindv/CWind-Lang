"""bug-37 regression: CFFI functions declared in the *same file* via an
unnamed ``extern "C"`` block must be callable by bare name without
``pub``.

``_build_module_table`` never registered ``ExternBlock`` members in their
home file's bare-name visible set (``_declaration_name`` returns ``None``
for the unnamed block), so a same-file call like ``atexit(clean)`` was
rejected with "belongs to another module and is not visible here".

Also covers the follow-up: the std prelude's export surface is visible to
every module file (Rust semantics), so imported modules may use prelude
aliases (``u32``/``i32``) without tripping the same gate.
"""

import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

import harness  # noqa: E402


class Bug37SameFileCffiTests(harness.CaseAssertionsMixin):
    def test_same_file_cffi(self):
        """原始 bug-37 复现: 同文件无名 extern 块的函数裸名调用."""
        self.assert_case("bug37", "same_file_cffi")

    def test_extern_noreturn(self):
        """bug-37: extern 函数可标记发散 (`-> !`, 映射 C noreturn/void)."""
        self.assert_case("bug37", "extern_noreturn")


if __name__ == "__main__":
    unittest.main()
