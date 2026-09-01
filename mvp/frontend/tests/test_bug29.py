"""Regression tests for bug-29: prelude type aliases at the FFI boundary.

SA 在校验 extern 声明的 C-ABI 兼容性时, 必须先把 ``std::prelude`` 里
定义的类型别名 (``f64``/``i32``/...) 展开到底层类型再查支持表;
结构体字段、数组元素与指针被指类型同理。修复前
``struct Complex64 { re: f64, im: f64 }`` 穿过 extern 边界会被误报为
"field 're' (f64) has no C-ABI mapping"。

Input programs live in ``cases/bug29``; assertions stay in this module.
"""

import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

import harness

from cwind_frontend import build_typed_ast, run_sa_with_errors
from cwind_frontend.parser.parser import parse_with_errors
from cwind_frontend import tokenize_file

B29 = "bug29"


class Bug29CaseTests(harness.CaseAssertionsMixin):
    def test_struct_alias_fields(self):
        """原始 bug-29 复现: f64 字段的结构体穿过 extern 边界."""
        self.assert_case(B29, "struct_alias_fields")

    def test_scalar_aliases(self):
        """extern 形参/返回直接使用别名标量."""
        self.assert_case(B29, "scalar_aliases")

    def test_array_and_pointer(self):
        """数组元素 / 指针被指类型的别名同样要展开."""
        self.assert_case(B29, "array_and_pointer")

    def test_string_field_still_rejected(self):
        """展开别名不能放宽既有 C-ABI 限制 (String 字段仍拒绝)."""
        self.assert_case(B29, "string_field_still_rejected")


# 与仓库 libs/prelude.wind 的数值别名一致的迷你 prelude
_MINI_PRELUDE = (
    "pub typedef f64 = Float64;\n"
    "pub typedef f32 = Float;\n"
    "pub typedef i32 = Int32;\n"
    "pub typedef u8 = UInt8;\n"
)

_ENTRY = (
    "struct Complex64 {\n"
    "    re: f64,\n"
    "    im: f64,\n"
    "}\n"
    "\n"
    "extern \"C\" {\n"
    "    fn cw_dot(a: Complex64, b: Complex64) -> f64;\n"
    "    fn cw_scale(z: Complex64, k: f32) -> Complex64;\n"
    "    fn cw_narrow(x: f64) -> i32;\n"
    "}\n"
    "\n"
    "fn main() {}\n"
)


class PreludeAliasTests(unittest.TestCase):
    """走真实 std::prelude 自动导入路径的端到端 SA 校验."""

    def _parse_entry(self, entry: Path):
        return parse_with_errors(
            tokenize_file(entry), source_path=str(entry.resolve())
        )

    def test_prelude_aliases_resolve_at_ffi_boundary(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            libs = root / "libs"
            libs.mkdir()
            # todo-158: the std root module (libs/mod.wind) is the prelude.
            (libs / "mod.wind").write_text(_MINI_PRELUDE, encoding="utf-8")
            entry = root / "main.wind"
            entry.write_text(_ENTRY, encoding="utf-8")

            parsed = self._parse_entry(entry)
            self.assertEqual([], [e.message for e in parsed.errors])
            result = run_sa_with_errors(parsed.program)
            self.assertEqual(
                [], [e.message for e in result.errors],
                "bug-29: prelude 别名必须能解析到 FFI 边界",
            )

            # typed AST 的 ann.type 必须是展开后的底层类型
            doc = build_typed_ast(parsed.program, result.info)

            def _walk(node):
                if isinstance(node, dict):
                    if node.get("kind") == "FnDecl":
                        yield node
                    for value in node.values():
                        yield from _walk(value)
                elif isinstance(node, list):
                    for item in node:
                        yield from _walk(item)

            extern_fns = [
                fn for fn in _walk(doc.get("ast"))
                if fn.get("extern_abi") is not None
            ]
            self.assertEqual(
                ["cw_dot", "cw_scale", "cw_narrow"],
                [fn["name"] for fn in extern_fns],
            )
            by_name = {fn["name"]: fn for fn in extern_fns}
            # 形参/返回的 ann.type 必须是展开后的底层类型
            self.assertEqual(
                "Float64",
                by_name["cw_dot"]["return_type"]["ann"]["type"]["name"],
            )
            self.assertEqual(
                "Complex64",
                by_name["cw_dot"]["params"][0]["type"]["ann"]["type"]["name"],
            )
            self.assertEqual(
                "Int32",
                by_name["cw_narrow"]["return_type"]["ann"]["type"]["name"],
            )


if __name__ == "__main__":
    unittest.main()
