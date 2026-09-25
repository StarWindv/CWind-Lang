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
        # 声明顺序不再影响读取: A 引用其后声明的 B —— pass 2 折不动的
        # 前向链在内联后 (`2 + 1`) 由内联 pass 折出结果, 直接落成字面量。
        doc = _typed("forward_ref")
        nodes = _nodes(doc)
        self.assertEqual(
            [n for n in nodes if n["kind"] == "ConstDecl"], []
        )
        ret = next(n for n in nodes if n["kind"] == "ReturnStmt")
        self.assertEqual(ret["value"]["kind"], "IntLit")
        self.assertEqual(ret["value"]["value"], 3)

    def test_arith_const_folds_to_literal(self):
        # `const a: u8 = 1 + 1` → 使用点是折叠后的字面量 (带声明类型),
        # 浮点同理 (`1.5 + 2.5` → FloatLit 4.0)。
        doc = _typed("inline_folded")
        nodes = _nodes(doc)
        self.assertEqual(
            [n for n in nodes if n["kind"] == "ConstDecl"], []
        )
        # `1 + 1` 不再以字面量加法的形态出现 (已被折叠)
        self.assertFalse([
            n for n in nodes
            if n["kind"] == "BinOp" and n.get("op") == "+"
            and (n.get("left") or {}).get("kind") == "IntLit"
            and (n.get("right") or {}).get("kind") == "IntLit"
        ])
        cast = next(
            n for n in nodes
            if n["kind"] == "CastExpr"
            and (n.get("operand") or {}).get("kind") == "IntLit"
        )
        self.assertEqual(cast["operand"]["value"], 2)
        self.assertEqual(cast["operand"]["ann"]["type"]["name"], "UInt8")
        let = next(n for n in nodes if n["kind"] == "LetStmt")
        self.assertEqual(let["value"]["kind"], "FloatLit")
        self.assertEqual(let["value"]["value"], 4.0)

    def test_unfoldable_root_folds_after_evaluation(self):
        # `twice(3) + (1 + 2)`: const-fn 调用先被求值烧录 (6), 整条根
        # 表达式随后折成字面量 9 —— 根折不动时退化为子树 ann.folded
        # (见 inline_div_mod 的断言)。
        doc = _typed("inline_fold_nested")
        nodes = _nodes(doc)
        self.assertEqual(
            [n for n in nodes if n["kind"] == "ConstDecl"], []
        )
        self.assertFalse([n for n in nodes if n["kind"] == "Call"])
        main = next(
            n for n in nodes
            if n["kind"] == "FnDecl" and n.get("name") == "main"
        )
        ret = next(
            n for n in _walk(main)
            if isinstance(n, dict) and n.get("kind") == "ReturnStmt"
        )
        value = ret["value"]
        if value["kind"] == "CastExpr":
            value = value["operand"]
        self.assertEqual(value["kind"], "IntLit")
        self.assertEqual(value["value"], 9)

    def test_annotate_arith_when_root_not_foldable(self):
        # 根含除法 (语义敏感不折叠) 时, 可折叠的子树补 ann.folded 注解
        # (todo-22: 后端见注解直接发常量) —— `(1+2)*0` 折 0, 其中
        # `(1+2)` 折 3, 注解逐层写入。
        doc = _typed("inline_div_mod")
        nodes = _nodes(doc)
        root = next(n for n in nodes if n["kind"] == "BinOp")
        self.assertNotIn("folded", root.get("ann") or {})
        inner = root["left"]
        self.assertEqual(inner["kind"], "BinOp")
        self.assertEqual(inner["ann"].get("folded"), 0)
        deepest = inner["left"]
        self.assertEqual(deepest["kind"], "BinOp")
        self.assertEqual(deepest["ann"].get("folded"), 3)

    def test_integer_division_chain_is_not_folded(self):
        # Python `//` 与后端 sdiv 负数语义不同: 除法链保留表达式形态,
        # 运行期按 C 语义求值 (-7 / 2 == -3)。
        doc = _typed("inline_div_mod")
        nodes = _nodes(doc)
        divs = [n for n in nodes if n["kind"] == "BinOp" and n.get("op") == "/"]
        self.assertTrue(divs)
        for div in divs:
            self.assertNotIn("folded", div.get("ann") or {})


class TestConstFnMarker(unittest.TestCase):
    """const-fn / const-type 声明、标记与序列化贯通。"""

    def test_const_fn_marker_survives_inlining(self):
        # `const A: i32 = area(3, 4);` —— 调用点被求值烧录为字面量 12,
        # FnDecl 的 const_fn 标记留在声明上 (求值单元的拷贝另有剥标)。
        doc = _typed("const_fn_decl_ok")
        nodes = _nodes(doc)
        self.assertEqual(
            [n for n in nodes if n["kind"] == "ConstDecl"], []
        )
        self.assertFalse([n for n in nodes if n["kind"] == "Call"])
        area = next(
            n for n in nodes
            if n["kind"] == "FnDecl" and n.get("name") == "area"
        )
        self.assertTrue(area.get("const_fn"))
        main = next(
            n for n in nodes
            if n["kind"] == "FnDecl" and n.get("name") == "main"
        )
        ret = next(
            n for n in _walk(main)
            if isinstance(n, dict) and n.get("kind") == "ReturnStmt"
        )
        value = ret["value"]
        if value["kind"] == "CastExpr":
            value = value["operand"]
        self.assertEqual(value["kind"], "IntLit")
        self.assertEqual(value["value"], 12)

    def test_std_const_fn_call_is_inlined(self):
        # std 的 `const fn String::length` (libs/builtins 标记): 调用点
        # 被求值烧录为字面量 3, 不再保留 Call。
        doc = _typed("const_fn_call_ok")
        nodes = _nodes(doc)
        self.assertEqual(
            [n for n in nodes if n["kind"] == "ConstDecl"], []
        )
        self.assertFalse([n for n in nodes if n["kind"] == "Call"])
        lengths = [
            n for n in nodes
            if n["kind"] == "IntLit" and n.get("value") == 3
        ]
        self.assertTrue(lengths)

    def test_monomorphized_enum_result_burns_as_variant(self):
        # `Option<Int>` 单态化枚举返回 (前端白名单 + 后端 C 视图都放行):
        # 求值结果烧录回变体构造 `Option::Some(7)`。
        doc = _typed("const_fn_option_ok")
        nodes = _nodes(doc)
        self.assertEqual(
            [n for n in nodes if n["kind"] == "ConstDecl"], []
        )
        variant_calls = [
            n for n in nodes
            if n["kind"] == "Call"
            and isinstance(n.get("ann"), dict)
            and (n["ann"].get("call") or {}).get("callee_kind")
            == "enum_variant"
        ]
        self.assertTrue(variant_calls)
        args = variant_calls[0].get("args") or []
        self.assertEqual(len(args), 1)
        self.assertEqual(args[0]["value"]["kind"], "IntLit")
        self.assertEqual(args[0]["value"]["value"], 7)

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
