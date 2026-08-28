"""todo-144 regression: typed-AST type objects carry definition-site FQNs.

PROBLEMS-FINAL 第 3 条的用户裁决: typed JSON 里类型对象分存
``def`` (定义位置的规范模块路径) / ``name`` (展开后的规范名) /
``alias`` (被展开掉的原始拼写)。白名单 (基础数值/基础容器/编译器内建)
与类型形参不展开; ``pub use`` 重导出的多条路径全部归一到定义位置。
显示层维持 ``Alias(TrueName)`` 形状 (``_fmt_type`` 的 ``f64 (Float64)``);
展开器防环守卫见 ``_expand_type`` (todo-132 后 ``std::builtins::X`` 为
终点的自解析环不得失控)。

管线结果 (clean) 由通用发现 (``test_cases.py`` 的 todo144 区) 覆盖;
本文件断言 JSON 的结构 —— def/alias 键的有无与取值, 这是管线结果
schema 表达不了的。
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


class Todo144ProjectFqnTests(harness.CaseAssertionsMixin):
    """Project tree: def paths resolve through the libs trie."""

    def test_def_and_alias_fields(self):
        pc = harness.run_project_case(TESTS / "cases" / "todo144" / "fqn_def")
        self.check_outcome(pc.outcome, pc.exp, ctx="todo144/fqn_def")
        env = build_typed_ast(pc.parsed.program, pc.sa.info)
        types = _let_types(env)

        # std 类型: 定义位置的规范路径 (不按使用处拼写展开)
        self.assertEqual(
            types["w"], {"name": "Wrap", "def": "std::wrap"},
            "imported struct carries its definition-site path",
        )
        self.assertEqual(
            types["o"],
            {"name": "Opt", "def": "std::opt", "args": [{"name": "Int"}]},
            "pub use 重导出的 Opt 归一到定义位置 std::opt",
        )

        # 嵌套实参同样递归补 def; Map 本身是白名单容器 (无 def)
        self.assertEqual(
            types["m"],
            {
                "name": "Map",
                "args": [
                    {"name": "String"},
                    {
                        "name": "Opt",
                        "def": "std::opt",
                        "args": [{"name": "Int"}],
                    },
                ],
            },
        )

        # 白名单: 基础容器/数值不展开 (无 def); 别名保留原始拼写
        self.assertEqual(
            types["a"],
            {
                "name": "Vector",
                "args": [{"name": "Float64"}],
                "alias": "Vec",
            },
            "Vec<f64> 展开为 Vector<Float64> 且记录 alias",
        )
        self.assertEqual(
            types["v"], {"name": "Vector", "args": [{"name": "Int"}]},
            "裸内建容器既无 def 也无 alias",
        )

        # typedef 别名 (WI -> Wrap) 记录 alias + 目标 def
        self.assertEqual(
            types["wi"],
            {"name": "Wrap", "def": "std::wrap", "alias": "WI"},
        )

        # 入口文件本地类型: 定义位置就是自身工件, 不写 def
        self.assertEqual(
            types["e"], {"name": "Local"},
            "entry-local types stay bare (unambiguous in their artifact)",
        )


class Todo144SingleFileTests(unittest.TestCase):
    """No prelude / no modules: alias provenance still lands, def never."""

    @staticmethod
    def _let_types(source: str) -> dict:
        parsed = parse_with_errors(tokenize(source))
        assert not parsed.errors, parsed.errors
        result = run_sa_with_errors(parsed.program)
        assert not result.errors, [e.message for e in result.errors]
        env = build_typed_ast(parsed.program, result.info)
        return _let_types(env)

    def test_typedef_alias_recorded(self):
        types = self._let_types(
            "typedef Email = String;\n"
            "fn main() { let e: Email = \"a\"; }\n"
        )
        self.assertEqual(
            types["e"],
            {"name": "String", "alias": "Email"},
            "typedef 别名展开后 alias 保留原始拼写; String 属白名单无 def",
        )

    def test_builtin_no_def_no_alias(self):
        types = self._let_types(
            "fn main() { let v: Vector<Int> = [1]; let x: Int = 1; }\n"
        )
        self.assertEqual(types["v"], {"name": "Vector", "args": [{"name": "Int"}]})
        self.assertEqual(types["x"], {"name": "Int"})

    def test_generic_param_no_def(self):
        types = self._let_types(
            "fn f<T>(v: T) -> T { return v; }\n"
            "fn main() { let x: Int = f(1); }\n"
        )
        self.assertEqual(types["x"], {"name": "Int"})


if __name__ == "__main__":
    unittest.main()
