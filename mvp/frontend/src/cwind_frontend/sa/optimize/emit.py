"""Reassociation emission: build the accumulator-loop body in place.

Produces fully annotated nodes (type/call) with synthetic ids so the
backend only consumes ``ann`` — same discipline as ``_emit_which_hooks``.
"""

from __future__ import annotations

import copy as _copy
from dataclasses import fields as _dc_fields
from typing import Any, Optional

from ...ast_components.ast import (
    Arg,
    Assign,
    BinOp,
    Block,
    Call,
    ContinueStmt,
    FnDecl,
    IntLit,
    LetStmt,
    LoopStmt,
    Name,
    Node,
    Program,
    ReturnStmt,
)
from ...ast_components.token import TokenKind

__all__ = ["emit_reassociation"]


def _ann_type_name(node: object) -> Optional[str]:
    ann = getattr(node, "_typed_ann", None)
    t = ann.get("type") if isinstance(ann, dict) else None
    if isinstance(t, dict):
        return t.get("name")
    return t


def _rewrite_base_returns(
    node: Node, acc_name: str, name_node
) -> None:
    """克隆块内 in-place: 基例 return 值改为 ``acc + 原值``。"""
    if isinstance(node, ReturnStmt) and node.value is not None:
        old = node.value
        ref = name_node(acc_name)
        add = BinOp(node.line, node.column, ref, TokenKind.PLUS, old)
        add._typed_ann["type"] = old._typed_ann.get("type")
        node.value = add
        return
    if isinstance(node, Node):
        for f in _dc_fields(node):
            if f.name in ("line", "column"):
                continue
            v = getattr(node, f.name, None)
            if isinstance(v, Node):
                _rewrite_base_returns(v, acc_name, name_node)
            elif isinstance(v, list):
                for x in v:
                    if isinstance(x, Node):
                        _rewrite_base_returns(x, acc_name, name_node)


def _find_param_binding(fn: FnDecl, pname: str) -> dict:
    """原体内引用形参的 Name 上 SA 记下的 binding (深拷贝)。

    改写后的 ``p = b`` LHS 复用它: (a) 闭包收集 (comptime/closure)
    认 binding.kind —— 无 binding 的裸名会当顶层引用走名字回退, 把
    用户同名 const 拖进求值单元; (b) 后端先按名找局部变量, binding
    只作补充, 两者一致时无歧义。找不到引用时给出 kind 形态 —— 只有
    闭包收集消费 kind, 后端不看 param 分支的 ref。
    """
    found: dict = {}

    def walk(n: Node) -> None:
        if found:
            return
        if isinstance(n, Name) and n.parts == [pname]:
            b = n._typed_ann.get("binding")
            if isinstance(b, dict) and b.get("kind") == "param":
                found.update(b)
                return
        for f in _dc_fields(n):
            if f.name in ("line", "column"):
                continue
            v = getattr(n, f.name, None)
            if isinstance(v, Node):
                walk(v)
            elif isinstance(v, list):
                for x in v:
                    if isinstance(x, Node):
                        walk(x)

    if fn.body is not None:
        walk(fn.body)
    return dict(found) if found else {"kind": "param"}


def emit_reassociation(
    az: Any,
    program: Program,
    fn: FnDecl,
    a_expr: Node,
    b_expr: Node,
    prefix: list[Node],
) -> bool:
    """Rewrite *fn*'s body into ``let mut acc; loop { … }`` (in place)."""
    line, column = fn.line, fn.column
    pname = fn.params[0].name
    acc_t = _ann_type_name(fn.return_type)
    acc_name = az._fresh_desugar_name(program, "acc")
    # 参数会被改写体直接赋值 (``p = b``): 置 mut —— SA 的可变性检查
    # 跑在重结合之前, 改写产物要能被**重新解析** (const-fn 求值单元
    # 把闭包渲染成源码再编一遍), 不置会被子编译的 SA 拒掉。
    fn.params[0].mutable = True
    param_binding = _find_param_binding(fn, pname)

    def type_ann() -> dict:
        """ann.type 的 dict 形态 (与 SA _ann_type 同构, 后端按
        ``{"name": ...}`` 消费)。"""
        return {"name": acc_t}

    def name_node(nm: str, binding: Optional[dict] = None) -> Name:
        n = Name(line, column, [nm])
        n._typed_ann["type"] = type_ann()
        if binding is not None:
            n._typed_ann["binding"] = _copy.deepcopy(binding)
        return n

    # let mut acc = 0;
    acc_let = LetStmt(line, column, acc_name, None,
                      IntLit(line, column, 0, "0"), mutable=True)
    acc_let._typed_ann["type"] = type_ann()
    az._assign_synthetic_ids(acc_let)

    # loop { <前置块克隆 (基例 return → acc + base)> ;
    #        acc = acc + f(a); p = b; continue; }
    loop_stmts: list[Node] = []
    for s in prefix:
        clone = _copy.deepcopy(s)
        az._assign_synthetic_ids(clone)
        _rewrite_base_returns(clone, acc_name, name_node)
        loop_stmts.append(clone)

    # acc = acc + f(a);
    callee = Name(line, column, [fn.name])
    callee._typed_ann["type"] = {"name": "Fn"}
    call = Call(line, column, callee,
                [Arg(line, column, _copy.deepcopy(a_expr))])
    call._typed_ann["call"] = {
        "callee_kind": "fn", "callee_ref": fn._typed_id,
    }
    call._typed_ann["type"] = type_ann()
    add = BinOp(line, column,
                name_node(acc_name), TokenKind.PLUS, call)
    add._typed_ann["type"] = type_ann()
    assign_acc = Assign(line, column, name_node(acc_name),
                        TokenKind.ASSIGN, add)
    assign_acc._typed_ann["type"] = type_ann()
    loop_stmts.append(assign_acc)

    # p = b;
    p_assign = Assign(line, column, name_node(pname, param_binding),
                      TokenKind.ASSIGN, _copy.deepcopy(b_expr))
    p_assign._typed_ann["type"] = type_ann()
    loop_stmts.append(p_assign)

    # continue;
    cont = ContinueStmt(line, column)
    loop_stmts.append(cont)

    loop = LoopStmt(line, column, Block(line, column, loop_stmts))
    az._assign_synthetic_ids(loop)
    for s in loop_stmts:
        az._assign_synthetic_ids(s)

    fn.body = Block(line, column, [acc_let, loop])
    return True
