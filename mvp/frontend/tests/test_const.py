"""const 任务的结构断言: 值内联 (task 1) 与编译期校验 (task 2)。

错误面用例在 ``cases/const/*.wind`` + ``<name>.json`` 侧车, 由
``test_cases.SINGLE_FILE_AREAS`` 的 ``const`` 项自动清扫 (pipeline 结果
比对)。本模块只做 typed-AST **结构**断言:

* ConstDecl 与 ``kind == "const"`` 符号不再进入产物 (读取点已就地
  替换为初始化表达式克隆, 未使用的 const 自然消失);
* 各形态的引用点 (裸名 / 关联常量 / 模块限定名 / 字段访问) 确实变成
  了字面量或构造表达式;
* 内联克隆后的节点 id 仍然稠密唯一 (克隆必须重编号, 否则序列化撞号)。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

import harness  # noqa: E402

from cwind_frontend import parse_source, run_sa_with_errors  # noqa: E402
from cwind_frontend.typed_ast import build_typed_ast  # noqa: E402

AREA = "const"


def _typed(name: str) -> dict:
    prog = parse_source(harness.source(AREA, name))
    result = run_sa_with_errors(prog)
    assert not result.errors, [e.message for e in result.errors]
    return build_typed_ast(prog, result.info)


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _nodes(doc: dict) -> list[dict]:
    return [n for n in _walk(doc["ast"]) if isinstance(n.get("id"), int)]


class TestConstInline(unittest.TestCase):
    def test_scalar_read_is_inlined(self):
        doc = _typed("inline_scalar")
        nodes = _nodes(doc)
        self.assertEqual(
            [n for n in nodes if n["kind"] == "ConstDecl"], []
        )
        self.assertEqual(
            [s for s in doc["symbols"] if s["kind"] == "const"], []
        )
        # `A + 2` 的左操作数即 A 的初始化式克隆
        add = next(
            n for n in nodes
            if n["kind"] == "BinOp" and n.get("op") == "+"
        )
        self.assertEqual(add["left"]["kind"], "IntLit")
        self.assertEqual(add["left"]["value"], 40)
        # 克隆必须重新编号: id 稠密且唯一 (1..N pre-order)
        ids = sorted(n["id"] for n in nodes)
        self.assertEqual(ids, list(range(1, len(ids) + 1)))

    def test_composite_read_is_inlined(self):
        doc = _typed("inline_composite")
        nodes = _nodes(doc)
        self.assertEqual(
            [n for n in nodes if n["kind"] == "ConstDecl"], []
        )
        attrs = [n for n in nodes if n["kind"] == "Attribute"]
        self.assertEqual({a["name"] for a in attrs}, {"x", "y"})
        for attr in attrs:
            self.assertEqual(attr["obj"]["kind"], "StructConstruct")

    def test_associated_const_is_inlined(self):
        doc = _typed("inline_assoc")
        nodes = _nodes(doc)
        self.assertEqual(
            [n for n in nodes if n["kind"] == "ConstDecl"], []
        )
        # extra 块保留, 但其关联常量表已被清空
        extra = next(n for n in nodes if n["kind"] == "ExtraDecl")
        self.assertFalse(extra.get("consts"))
        ret = next(n for n in nodes if n["kind"] == "ReturnStmt")
        self.assertEqual(ret["value"]["kind"], "IntLit")
        self.assertEqual(ret["value"]["value"], 11)

    def test_module_qualified_const_is_inlined(self):
        doc = _typed("inline_module_const")
        nodes = _nodes(doc)
        self.assertEqual(
            [n for n in nodes if n["kind"] == "ConstDecl"], []
        )
        ret = next(n for n in nodes if n["kind"] == "ReturnStmt")
        self.assertEqual(ret["value"]["kind"], "IntLit")
        self.assertEqual(ret["value"]["value"], 5)

    def test_forward_reference_inlines_independently_of_order(self):
        # 声明顺序不再影响读取: A 引用其后声明的 B, 两处都内联成字面量
        doc = _typed("forward_ref")
        nodes = _nodes(doc)
        self.assertEqual(
            [n for n in nodes if n["kind"] == "ConstDecl"], []
        )
        ret = next(n for n in nodes if n["kind"] == "ReturnStmt")
        add = ret["value"]
        self.assertEqual(add["kind"], "BinOp")
        # A 的初始化式 `B + 1` 被整体搬运, 其中的 B 再内联为字面量 2
        self.assertEqual(add["left"]["kind"], "IntLit")
        self.assertEqual(add["left"]["value"], 2)
        self.assertEqual(add["right"]["value"], 1)


class TestConstFnMarker(unittest.TestCase):
    """const-fn / const-type 声明、标记与序列化贯通。"""

    def test_const_fn_marker_survives_inlining(self):
        # `const A: i32 = area(3, 4);` —— 调用点被内联进 main 的返回值,
        # FnDecl 的 const_fn 标记与 ann.call 目标引用保持一致。
        doc = _typed("const_fn_decl_ok")
        nodes = _nodes(doc)
        self.assertEqual(
            [n for n in nodes if n["kind"] == "ConstDecl"], []
        )
        area = next(
            n for n in nodes
            if n["kind"] == "FnDecl" and n.get("name") == "area"
        )
        self.assertTrue(area.get("const_fn"))
        call = next(
            n for n in nodes
            if n["kind"] == "Call"
            and isinstance(n.get("ann"), dict)
            and (n["ann"].get("call") or {}).get("callee_kind") == "fn"
            and n["ann"]["call"].get("callee_ref") == area["id"]
        )
        # 实参是常量表达式的内联结果 (字面量)
        self.assertEqual(
            [a["value"]["kind"] for a in call["args"]],
            ["IntLit", "IntLit"],
        )

    def test_std_const_fn_call_is_inlined(self):
        # std 的 `const fn String::length` (libs/builtins 标记): 调用点
        # 在 const 初始化式内被放行并整体内联进 main。
        doc = _typed("const_fn_call_ok")
        nodes = _nodes(doc)
        self.assertEqual(
            [n for n in nodes if n["kind"] == "ConstDecl"], []
        )
        call = next(
            n for n in nodes
            if n["kind"] == "Call"
            and isinstance(n.get("ann"), dict)
            and (n["ann"].get("call") or {}).get("callee_kind") == "method"
        )
        # 接收者是内联后的字符串字面量
        callee = call["callee"]
        self.assertEqual(callee["kind"], "Attribute")
        self.assertEqual(callee["obj"]["kind"], "StrLit")

    def test_const_fn_round_trips_through_unparse(self):
        # `const fn` 标记必须经 render -> parse -> SA 保持 (反编译面)。
        from cwind_frontend.lexer import lex_with_errors
        from cwind_frontend.parser.parser import parse_with_errors
        from cwind_frontend.render.source import render_document

        prog = parse_source(harness.source(AREA, "const_fn_decl_ok"))
        result = run_sa_with_errors(prog)
        self.assertEqual([e.message for e in result.errors], [])
        text = render_document(build_typed_ast(prog, result.info))
        self.assertIn("const fn", text)
        lexed = lex_with_errors(text)
        self.assertEqual([e.message for e in lexed.errors], [])
        reparsed = parse_with_errors(lexed.tokens)
        self.assertEqual([e.message for e in reparsed.errors], [])
        recheck = run_sa_with_errors(reparsed.program)
        self.assertEqual([e.message for e in recheck.errors], [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
