"""bug-86: CFFI 内的类型检查.

复现见 ``bugs/bug86.wind``:

1. 未引入的类型别名 (``std::ctypedef`` 的 ``c_uint``) 在 extern 签名里
   被静默接受 —— pass 0 的别名表是全局的, 任何模块的 ``typedef`` 都能从
   任何文件解析出来, 于是 ``libs/ctypedef.wind`` 顶部写明的
   "使用方需自行 ``use std::ctypedef::*;``" 从未被强制。
2. 类型根本不存在时, 报错不是"未找到类型", 而是一条早已过时的 C-ABI
   限制说明 (``_c_abi_violation`` 的兜底分支) —— 顶层返回位、裸指针
   被指位、回调签名段三处都会落到那条兜底上。

用例必须走真实文件 (临时目录里的 ``main.wind``): 裸名可见性门控
(todo-79) 依赖 parser 写下的逐文件可寻址面, 内存源没有这张面。
"""

import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

from cwind_frontend import run_sa_with_errors, tokenize_file
from cwind_frontend.parser.parser import parse_with_errors


def _sa(text: str) -> list[str]:
    """Run lex -> parse -> SA on a real temp file; returns error messages."""
    with tempfile.TemporaryDirectory() as directory:
        entry = Path(directory) / "main.wind"
        entry.write_text(text, encoding="utf-8")
        parsed = parse_with_errors(
            tokenize_file(entry), source_path=str(entry.resolve())
        )
        if parsed.errors:
            return [e.message for e in parsed.errors]
        result = run_sa_with_errors(parsed.program)
        return [e.message for e in result.errors]


class UnimportedCTypeAliasTests(unittest.TestCase):
    """(1) 未导入的 std C 类型别名必须按 todo-79 报错."""

    def test_bare_alias_in_extern_param_rejected(self):
        errors = _sa(
            "extern \"C\" {\n"
            "    fn no_such_function(input: c_uint) -> i32;\n"
            "}\n"
            "fn main() -> Int { return 0; }\n"
        )
        self.assertEqual(1, len(errors), errors)
        self.assertIn("type 'c_uint'", errors[0])
        self.assertIn("not visible here", errors[0])

    def test_bare_alias_in_plain_signature_rejected(self):
        # 门控不在 FFI 专属: 同一张表管所有类型位 (形参 + 返回位各一条)。
        errors = _sa(
            "fn f(p: c_int) -> c_int { return p; }\n"
            "fn main() -> Int { return 0; }\n"
        )
        self.assertEqual(2, len(errors), errors)
        for message in errors:
            self.assertIn("type 'c_int'", message)
            self.assertIn("not visible here", message)

    def test_explicit_import_accepted(self):
        errors = _sa(
            "use std::ctypedef::*;\n"
            "extern \"C\" {\n"
            "    fn f(n: c_int, p: *mut c_void, s: c_size_t) -> c_int;\n"
            "}\n"
            "fn main() -> Int { return 0; }\n"
        )
        self.assertEqual([], errors)

    def test_qualified_path_accepted(self):
        # 限定寻址不需要裸名进作用域 (同 Rust 的 `libc::c_int`)。
        errors = _sa(
            "extern \"C\" {\n"
            "    fn f(n: std::ctypedef::c_int) -> i32;\n"
            "}\n"
            "fn main() -> Int { return 0; }\n"
        )
        self.assertEqual([], errors)


class UnknownFfiTypeTests(unittest.TestCase):
    """(2) 类型不存在时报“未找到类型”, 不再兜底成 C-ABI 限制说明."""

    def test_return_pointee_and_callback_param(self):
        errors = _sa(
            "extern \"C\" {\n"
            "    fn a() -> NoSuchType;\n"
            "    fn b(p: *const NoSuchPointee) -> i32;\n"
            "    fn c(f: fn(NoSuchParam) -> i32);\n"
            "}\n"
            "fn main() -> Int { return 0; }\n"
        )
        self.assertEqual(3, len(errors), errors)
        for name in ("NoSuchType", "NoSuchPointee", "NoSuchParam"):
            self.assertTrue(
                any(f"unknown type '{name}'" in m for m in errors), errors
            )
        self.assertFalse(
            any("no C-ABI mapping" in m for m in errors),
            f"a type that does not resolve is not an ABI restriction: {errors}",
        )

    def test_extern_static_reports_unknown_type(self):
        errors = _sa(
            "extern \"C\" {\n"
            "    static mut NO_SUCH: NoSuchStatic;\n"
            "}\n"
            "fn main() -> Int { return 0; }\n"
        )
        self.assertEqual(1, len(errors), errors)
        self.assertIn("unknown type 'NoSuchStatic'", errors[0])
        self.assertNotIn("no C-ABI mapping", errors[0])

    def test_known_but_unmappable_type_still_reports_abi_restriction(self):
        # 兜底说明本身没错, 只是不该再兜底未解析的类型。
        errors = _sa(
            "extern \"C\" {\n"
            "    fn f(v: Vector<Int32>) -> i32;\n"
            "}\n"
            "fn main() -> Int { return 0; }\n"
        )
        self.assertEqual(1, len(errors), errors)
        self.assertIn("no C-ABI mapping", errors[0])


if __name__ == "__main__":
    unittest.main()
