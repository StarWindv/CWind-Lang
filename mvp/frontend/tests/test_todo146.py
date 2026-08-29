"""todo-146 regression: def provenance on every type-bearing stringly site.

todo-144 阶段 1 只覆盖 ann.type / type_args; 阶段 2 把剩余位点补齐:
``bindings[].owner`` / ``trait`` (owner_def / trait_def 兄弟键)、
``symbols[]`` (def)、扁平编码的指针/数组/函数签名字符串 (拆出基名 def)、
match 模式 (EnumPattern enum_def + StructPattern field_types /
元组 element_types)。

裁决不变: 字符串本身保持扁平规范名 (后端按它匹配/名称修饰),
定义位置作为兄弟键补充, 本地声明无 def 时键整体省略 (与历史输出
逐字节兼容)。into/From 注册表 (into_impls) 是 SA 内部结构, 无 JSON
表面, 不涉及。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent.parent.parent.parent
for path in (ROOT / "mvp/frontend/src", ROOT / "mvp/frontend/tests"):
    sys.path.insert(0, str(path))

import harness  # noqa: E402
from cwind_frontend import run_sa_with_errors, tokenize  # noqa: E402
from cwind_frontend.parser.parser import parse_with_errors  # noqa: E402
from cwind_frontend.typed_ast import build_typed_ast  # noqa: E402


def _walk(node, out, kind=None, name=None):
    if isinstance(node, dict):
        if (kind is None or node.get("kind") == kind) and (
            name is None or node.get("name") == name
        ):
            out.append(node)
        for value in node.values():
            _walk(value, out, kind, name)
    elif isinstance(node, list):
        for value in node:
            _walk(value, out, kind, name)


def _fns(env: dict, name: str) -> list[dict]:
    out: list = []
    _walk(env["ast"], out, kind="FnDecl", name=name)
    return out


def _patterns(env: dict, kind: str) -> list[dict]:
    out: list = []
    _walk(env["ast"], out, kind=kind)
    return out


class Todo146ProjectProvTests(harness.CaseAssertionsMixin):
    """Project tree: bindings/symbols/patterns/flat strings all traced."""

    @classmethod
    def setUpClass(cls):
        pc = harness.run_project_case(TESTS / "cases" / "todo146" / "prov_sites")
        cls.pc = pc
        cls.env = build_typed_ast(pc.parsed.program, pc.sa.info)

    def test_case_clean(self):
        self.check_outcome(self.pc.outcome, self.pc.exp, ctx="todo146/prov_sites")

    def test_symbols_def(self):
        syms = {s["name"]: s for s in self.env["symbols"]}
        self.assertEqual(syms["Wrap"].get("def"), "std::wrap")
        self.assertEqual(syms["Inner"].get("def"), "std::wrap")
        self.assertEqual(syms["Greeter"].get("def"), "std::wrap")
        self.assertEqual(syms["Opt"].get("def"), "std::opt")
        # 入口本地声明: 定义位置即自身工件, 无 def
        self.assertNotIn("def", syms["Local"])
        self.assertNotIn("def", syms["takes"])

    def test_bindings_owner_trait_def(self):
        binds = self.env["bindings"]
        extra = [b for b in binds if b["owner"] == "Wrap"]
        self.assertTrue(extra)
        for b in extra:
            self.assertEqual(b.get("owner_def"), "std::wrap")
        impl = [b for b in binds if b["owner"] == "Local"]
        self.assertTrue(impl)
        for b in impl:
            self.assertNotIn("owner_def", b)
            self.assertEqual(b["trait"], "Greeter")
            self.assertEqual(b.get("trait_def"), "std::wrap")

    def test_flat_pointer_and_fn_sig_def(self):
        fn = _fns(self.env, "takes")[0]
        types = {p["name"]: p["ann"]["type"] for p in fn["params"]}
        # 扁平指针名拆出被指基名的 def
        self.assertEqual(
            types["p"],
            {"name": "*const Wrap", "def": "std::wrap"},
        )
        # 函数签名: 参数/返回段按结构递归 (返回段带 def)
        f = types["f"]
        self.assertTrue(f["name"].startswith("fn("))
        self.assertEqual(f["args"][1], {"name": "Wrap", "def": "std::wrap"})

    def test_enum_pattern_enum_def(self):
        arms = _patterns(self.env, "EnumPattern")
        self.assertTrue(arms)
        for pat in arms:
            self.assertEqual(pat["ann"]["enum"], "Opt")
            self.assertEqual(pat["ann"].get("enum_def"), "std::opt")

    def test_struct_pattern_field_types_def(self):
        pats = _patterns(self.env, "StructPattern")
        self.assertTrue(pats)
        ft = pats[0]["ann"]["field_types"]
        self.assertEqual(ft["inner"], {"name": "Inner", "def": "std::wrap"})
        # 内建字段类型依旧无 def
        self.assertEqual(ft["v"], {"name": "Int"})


class Todo146SingleFileTests(unittest.TestCase):
    """No prelude: local flat strings stay def-less (byte-compatible)."""

    @staticmethod
    def _env(source: str) -> dict:
        parsed = parse_with_errors(tokenize(source))
        assert not parsed.errors, parsed.errors
        result = run_sa_with_errors(parsed.program)
        assert not result.errors, [e.message for e in result.errors]
        return build_typed_ast(parsed.program, result.info)

    def test_local_pointer_no_def(self):
        env = self._env(
            "fn f(p: *const Int) -> Int { return *p; }\n"
            "fn g(a: [Int; 2]) -> Int { return 0; }\n"
            "fn main() { let x: Int = 1; print(f(&x)); }\n"
        )
        pf = _fns(env, "f")[0]
        self.assertEqual(
            pf["params"][0]["ann"]["type"], {"name": "*const Int"})
        pg = _fns(env, "g")[0]
        self.assertEqual(
            pg["params"][0]["ann"]["type"], {"name": "[Int; 2]"})

    def test_local_enum_pattern_no_enum_def(self):
        env = self._env(
            "enum E { A, B }\n"
            "fn main() {\n"
            "    let e: E = E::A;\n"
            "    let r: Int = match (e) { E::A => 1, E::B => 2 };\n"
            "    print(r);\n"
            "}\n"
        )
        pats = _patterns(env, "EnumPattern")
        self.assertTrue(pats)
        for pat in pats:
            self.assertEqual(pat["ann"]["enum"], "E")
            self.assertNotIn("enum_def", pat["ann"])

    def test_pointer_to_alias_expands_with_def_via_local_typedef(self):
        # 本地 typedef 展开后基名是内建 (无 def), 但 alias 记录仍生效;
        # 指针扁平名的 alias 不记录 (只拆 def) —— 锁定当前边界
        env = self._env(
            "typedef My = Int;\n"
            "fn main() {\n"
            "    let x: My = 1;\n"
            "    let p: *const My = &x;\n"
            "    print(*p);\n"
            "}\n"
        )
        outs: list = []
        _walk(env["ast"], outs, kind="LetStmt", name="p")
        self.assertEqual(outs[0]["ann"]["type"], {"name": "*const Int"})


if __name__ == "__main__":
    unittest.main()
