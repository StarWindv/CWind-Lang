"""todo-154 regression: pass-0 FQN canonicalization (alias + qualified paths).

pass 0 在 which 钩子与 pass 1 之前把一切类型引用解析为规范形:

- prelude 别名按**全量**别名表展开 (``Vec<Int>`` -> ``Vector<Int>``) ——
  首个实现的 per-file 表是 prelude 别名永不展开的根因;
- 展开终点是内置类型时把 Type 节点名写为 FQN 存储形
  (``std::builtins::Vector``) —— ``Vec`` 的路径就是
  ``Vec -> std::builtins::Vector`` (用户拍板: 全展开);
- type 位的限定路径 (``std::geom::Point``) 经 per-file 模块别名表
  解析到规范裸名 (内置类型重新限定为 FQN);
- 定义位 owner (impl/extra 目标与 trait、extern "CWind" 的 cwind_owner)
  **不参与** pass 0, 由既有 (裸名) 管线管理;
- JSON 契约 (typed-AST) 保持**裸名**: ``_type_info`` 与
  ``build_typed_ast`` 在序列化边界剥除 FQN (前后端互不知晓)。

数据用例 (``cases/todo154/``) 走 ``test_cases.py`` 的项目树跑批; 本文件
只做 typed-JSON 结构断言 (ann 溯源 / JSON 边界 / FQN 存储不变量)。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent.parent.parent.parent
for path in (ROOT / "mvp/frontend/src", ROOT / "mvp/frontend/tests"):
    sys.path.insert(0, str(path))

import harness  # noqa: E402
from cwind_frontend import run_sa_with_errors, tokenize, tokenize_file  # noqa: E402
from cwind_frontend.parser.parser import parse_with_errors  # noqa: E402
from cwind_frontend.typed_ast import build_typed_ast  # noqa: E402

PRELUDE = (
    "pub extern \"CWind\" {\n"
    "    type Vector<T>;\n"
    "    type Set<T>;\n"
    "    type Map<K, V>;\n"
    "    type String;\n"
    "    type Int;\n"
    "    type Int32;\n"
    "    type Int64;\n"
    "    type UInt8;\n"
    "    type UInt64;\n"
    "    fn Vector<T>::new() -> Self;\n"
    "    fn Vector<T>::push_back(&mut self, value: T);\n"
    "    fn Vector<T>::get(&self, index: usize) -> T;\n"
    "    fn Set<T>::new() -> Self;\n"
    "    fn Set<T>::add(&mut self, value: T);\n"
    "}\n"
    "pub typedef Vec<T> = Vector<T>;\n"
    "pub typedef u64 = UInt64;\n"
    "pub typedef usize = u64;\n"
)


def _project(main_src: str, libs: dict[str, str] | None = None):
    """Parse + SA one mini project (real temp dirs, unique per call —
    the parser memoizes loaded std programs by path within a process)."""
    td = tempfile.TemporaryDirectory()
    _KEEPALIVE.append(td)
    root = Path(td.name)
    root.joinpath("libs").mkdir()
    for rel, text in (libs or {}).items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    main = root / "main.wind"
    main.write_text(main_src, encoding="utf-8")
    parsed = parse_with_errors(
        tokenize_file(main), source_path=str(main.resolve())
    )
    assert not parsed.errors, parsed.errors
    sa = run_sa_with_errors(parsed.program)
    return parsed, sa


_KEEPALIVE: list = []


def _project_result(main_src: str, libs: dict[str, str] | None = None):
    """``_project`` variant that tolerates SA errors (boundary tests)."""
    td = tempfile.TemporaryDirectory()
    _KEEPALIVE.append(td)
    root = Path(td.name)
    root.joinpath("libs").mkdir()
    for rel, text in (libs or {}).items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    main = root / "main.wind"
    main.write_text(main_src, encoding="utf-8")
    parsed = parse_with_errors(
        tokenize_file(main), source_path=str(main.resolve())
    )
    assert not parsed.errors, parsed.errors
    return run_sa_with_errors(parsed.program)


def _let_types(env: dict) -> dict:
    """Map ``LetStmt`` names to their ``ann.type`` objects."""

    def walk(node, out):
        if isinstance(node, dict):
            if node.get("kind") == "LetStmt" and "name" in node:
                out[node["name"]] = node.get("ann", {}).get("type")
            for value in node.values():
                walk(value, out)
        elif isinstance(node, list):
            for value in node:
                walk(value, out)

    out: dict = {}
    walk(env["ast"], out)
    return out


def _fqn_leaks(env: dict) -> list[str]:
    """Every JSON site still carrying the ``std::builtins::`` prefix."""
    leaks: list[str] = []

    def walk(node, path):
        if isinstance(node, dict):
            if node.get("kind") == "Type" \
                    and isinstance(node.get("name"), str) \
                    and "std::builtins::" in node["name"]:
                leaks.append(f"{path}: Type.name={node['name']!r}")
            if isinstance(node.get("owner"), str) \
                    and "std::builtins::" in node["owner"]:
                leaks.append(f"{path}: owner={node['owner']!r}")
            for key, value in node.items():
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, f"{path}[{i}]")

    walk(env, "$")
    return leaks


class Todo154JsonBoundaryTests(unittest.TestCase):
    """The typed-AST contract stays bare-named end to end."""

    @classmethod
    def setUpClass(cls):
        libs = {"libs/mod.wind": PRELUDE}
        cls.parsed, cls.sa = _project(
            "fn main() -> Int {\n"
            "    let v: Vec<Int> = Vec::new();\n"
            "    let n: usize = 5;\n"
            "    return 0;\n"
            "}\n",
            libs=libs,
        )
        cls.env = build_typed_ast(cls.parsed.program, cls.sa.info)

    def test_json_contract_has_no_fqn(self):
        leaks = _fqn_leaks(self.env)
        self.assertEqual(
            leaks, [],
            "typed-AST JSON must be bare-named (frontend/backend boundary)",
        )

    def test_ann_type_expanded_with_alias_provenance(self):
        types = _let_types(self.env)
        self.assertEqual(
            types["v"],
            {"name": "Vector", "args": [{"name": "Int"}], "alias": "Vec"},
            "Vec<Int> expands to Vector<Int> and records the alias spelling",
        )
        self.assertEqual(
            types["n"],
            {"name": "UInt64", "alias": "usize"},
            "usize -> u64 -> UInt64 chain collapses with provenance",
        )


class Todo154FqnStorageTests(unittest.TestCase):
    """The internal AST Type nodes carry the FQN storage form."""

    @classmethod
    def setUpClass(cls):
        libs = {"libs/mod.wind": PRELUDE}
        cls.parsed, cls.sa = _project(
            "fn main() -> Int {\n"
            "    let v: Vec<Int> = Vec::new();\n"
            "    return 0;\n"
            "}\n",
            libs=libs,
        )

    def _entry_types(self):
        """Type nodes of the ENTRY file only (prelude decls excluded)."""
        out: list = []

        def walk(node):
            import cwind_frontend.ast_components.ast as ast

            if isinstance(node, ast.Type):
                out.append(node)
                for a in node.args:
                    walk(a)
                return
            for f in node.__dict__.values():
                if hasattr(f, "__dict__") and not isinstance(f, type):
                    walk(f)
                elif isinstance(f, list):
                    for v in f:
                        if hasattr(v, "__dict__"):
                            walk(v)

        for item in self.parsed.program.items:
            home = str(getattr(item, "source_module", "") or "")
            if not home.endswith("main.wind"):
                continue
            walk(item)
        return out

    def test_reference_positions_store_fqn(self):
        names = {t.name for t in self._entry_types()}
        # 展开终点为内置类型 -> FQN 存储形 (Vec -> std::builtins::Vector)
        self.assertIn(
            "std::builtins::Vector", names,
            "the alias reference stores its expanded FQN form",
        )
        self.assertIn("std::builtins::Int", names)

    def test_alias_provenance_survives(self):
        v = next(
            t for t in self._entry_types()
            if t.name == "std::builtins::Vector"
        )
        self.assertEqual(
            getattr(v, "_fqn_original", None), "Vec",
            "the pre-expansion alias spelling is preserved for diagnostics",
        )

    def test_definition_site_owners_stay_bare(self):
        """extern "CWind" 的 cwind_owner 不被 pass 0 触碰 (裸名注册键)。"""
        owners = [
            fn.cwind_owner.name
            for item in self.parsed.program.items
            for fn in getattr(item, "fns", [])
            if getattr(fn, "cwind_owner", None) is not None
        ]
        self.assertTrue(owners, "prelude methods must be present")
        for owner in owners:
            self.assertNotIn(
                "std::builtins::", owner,
                "definition-site owners stay in the bare registry form",
            )


class Todo154QualifiedPathTests(unittest.TestCase):
    """type 位限定路径解析 + 遮蔽 + 可见性豁免。"""

    def test_qualified_user_path_resolves(self):
        libs = {
            "libs/geom.wind": (
                "pub struct Point { pub x: Int, pub y: Int }\n"
            ),
            "libs/mod.wind": "pub mod geom;\n",
        }
        parsed, _sa = _project(
            "fn main() -> Int {\n"
            "    let p: std::geom::Point = std::geom::Point { 1, 2 };\n"
            "    return p.x;\n"
            "}\n",
            libs=libs,
        )
        env = build_typed_ast(parsed.program, _sa.info)

        def let_type(name):
            out = _let_types(env)
            return out[name]

        self.assertEqual(
            let_type("p"), {"name": "Point", "def": "std::geom"},
            "qualified paths resolve to the flat canonical name with def",
        )

    def test_user_typedef_shadows_prelude_alias(self):
        """层叠律: user > std —— 本地 Vec 覆盖 prelude 的 Vec。"""
        libs = {"libs/mod.wind": PRELUDE}
        parsed, sa = _project(
            "typedef Vec<T> = Set<T>;\n"
            "\n"
            "fn main() -> Int {\n"
            "    let s: Vec<Int> = Vec::new();\n"
            "    s.add(1);\n"
            "    return s.length() as Int;\n"
            "}\n",
            libs=libs,
        )
        env = build_typed_ast(parsed.program, sa.info)
        types = _let_types(env)
        self.assertEqual(
            types["s"],
            {"name": "Set", "args": [{"name": "Int"}], "alias": "Vec"},
            "the local alias wins the global alias table (layering)",
        )

    def test_trait_named_like_builtin_never_qualifies(self):
        """用户 trait 可与内置类型同名 (trait Iterator): trait 引用位
        永远不做终点限定。"""
        parsed, sa = _project(
            "trait Iterator { }\n"
            "\n"
            "struct C {}\n"
            "\n"
            "impl Iterator for C {}\n"
            "\n"
            "fn main() -> Int { return 0; }\n",
        )
        self.assertEqual([], [e.message for e in sa.errors])


class Todo154BoundaryTests(unittest.TestCase):
    """复杂边界。"""

    def test_bare_generic_alias_annotation_is_arity_error(self):
        """裸泛型别名注解不猜测 —— pass 0 不展开, 类型位报 arity。"""
        sa = _project_result(
            "fn main() -> Int {\n"
            "    let v: Vec = Vector::new();\n"
            "    return 0;\n"
            "}\n",
            libs={"libs/mod.wind": PRELUDE},
        )
        self.assertTrue(
            any("generic argument" in e.message for e in sa.errors),
            [e.message for e in sa.errors],
        )

    def test_circular_alias_does_not_hang(self):
        result = harness.run_pipeline(
            "typedef A = B;\n"
            "typedef B = A;\n"
            "\n"
            "fn main() -> Int {\n"
            "    let x: A = undefined();\n"
            "    return 0;\n"
            "}\n",
        )
        # 环别名: pass 0 防环护栏终止; 具体报错由检查面给出, 不允许挂死
        self.assertIn(result["kind"], ("clean", "sa_err", "parse_err"))

    def test_generic_param_isolated_from_same_named_alias(self):
        """泛型形参 T 与同名别名 (typedef T = Int) 完全隔离。"""
        libs = {"libs/mod.wind": PRELUDE}
        _parsed, sa = _project(
            "typedef T = Int;\n"
            "\n"
            "struct Box<T> { pub v: T }\n"
            "\n"
            "fn id<T>(x: T) -> T { return x; }\n"
            "\n"
            "fn main() -> Int {\n"
            "    let b: Box<String> = Box { \"s\" };\n"
            "    return id(b).v.length() as Int;\n"
            "}\n",
            libs=libs,
        )
        self.assertEqual([], [e.message for e in sa.errors])

    def test_cross_module_duplicate_typedef_rejected(self):
        """扁平名跨模块冲突依旧拒绝 (全量别名表的安全前提)。"""
        libs = {
            "libs/a.wind": "pub typedef Name = Int32;\n",
            "libs/b.wind": "pub typedef Name = Int64;\n",
            "libs/mod.wind": "pub mod a;\npub mod b;\n",
        }
        root = None
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            root.joinpath("libs").mkdir()
            for rel, text in libs.items():
                p = root / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text, encoding="utf-8")
            main = root / "main.wind"
            main.write_text(
                "use std::a;\nuse std::b;\nfn main() -> Int { return 0; }\n",
                encoding="utf-8",
            )
            parsed = parse_with_errors(
                tokenize_file(main), source_path=str(main.resolve())
            )
            sa = run_sa_with_errors(parsed.program)
        self.assertTrue(
            any("duplicate definition of 'Name'" in e.message
                for e in sa.errors),
        )

    def test_alias_in_impl_target_reaches_underlying(self):
        """bug-43 语义: extra 目标上的别名展开到底层类型 (别名与底层
        拼写是同一个实现)。"""
        libs = {
            "libs/wrap.wind": "pub struct W { pub v: Int }\n",
            "libs/mod.wind": "pub mod wrap;\n",
        }
        _parsed, sa = _project(
            "use std::wrap;\n"
            "typedef WI = wrap::W;\n"
            "\n"
            "extra WI {\n"
            "    pub fn twice(self) -> Int { return self.v * 2; }\n"
            "}\n"
            "\n"
            "fn main() -> Int {\n"
            "    let x: wrap::W = wrap::W { 21 };\n"
            "    return x.twice();\n"
            "}\n",
            libs=libs,
        )
        self.assertEqual([], [e.message for e in sa.errors])

    def test_refinement_alias_keeps_canonical_name(self):
        """``where`` 精化别名保持自身规范名 (谓词按别名拼写查)。"""
        sa = _project_result(
            "type Small = Int where { self > 0; }\n"
            "\n"
            "fn f(s: Small) -> Int { return s; }\n"
            "\n"
            "fn main() -> Int { return f(3); }\n",
            libs={"libs/mod.wind": PRELUDE},
        )
        self.assertEqual([], [e.message for e in sa.errors])


if __name__ == "__main__":
    unittest.main()
