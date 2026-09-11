"""Front-end AST optimization passes (SA-complete tree rewrites).

SA 完成后的树改写, 与 ``_emit_which_hooks`` 同纪律: 产物带完整
ann (type/call), 新名字经 ``_fresh_desugar_name`` 卫生化, 新节点经
``_assign_synthetic_ids`` 进节点池, 后端只消费 ann 不做第二次解析。
SA 已把调用点解析成 ``ann.call = {callee_kind: "fn", callee_ref:
<fn._typed_id>}``, self-call 判定就是 ``callee_ref == fn._typed_id``。

当前 pass (对齐 rustc -O 下 fib 的 accumulator 循环形态, 见
.bench/fib42.rs.ll):

* 重结合 + 左倾链 (reassociation / left-leaning chain): 函数体为
  「前置语句块 + 尾 return = self(a) + self(b)」形态 (a/b 是形参
  的纯标量表达式), 且前置块里存在基例 return (非 self-call、值
  是形参的纯标量表达式) 时::

      fn f(p) { A(p)...; if c { return base(p); }
                return f(a(p)) + f(b(p)); }
  →   fn f(p) { let mut acc = 0; loop {
                    A(p)...                      (每轮重判)
                    if c { return acc + base(p); }
                    acc = acc + f(a(p));
                    p = b(p); } }

  单 self-call + 参数滚动, 调用深度从 2^n 降到 ~n/2 (fib: 42 层
  → 21 层), 且左倾调用是后端/LLVM 可尾调用优化的形态。

保守面: 整函数体匹配才改写, 不匹配原样保留; 仅单标量值形参;
前置块出现任何 self-call 或基例值非纯标量表达式即放弃。
"""

from __future__ import annotations

import copy as _copy
from dataclasses import fields as _dc_fields
from typing import Any, Optional

from ..ast_components.ast import (
    Arg,
    Assign,
    BinOp,
    Block,
    Call,
    ContinueStmt,
    ExprStmt,
    FnDecl,
    IntLit,
    LoopStmt,
    LetStmt,
    Name,
    Node,
    Program,
    ReturnStmt,
    UnaryOp,
)
from ..ast_components.token import TokenKind

__all__ = ["optimize_reassociation"]

_SCALARS = frozenset({
    "Int", "UInt", "Byte", "Bool",
    "Int8", "Int16", "Int32", "Int64",
    "UInt8", "UInt16", "UInt32", "UInt64",
    "Float", "Float32", "Float64",
})


def _ann_type_name(node: object) -> Optional[str]:
    ann = getattr(node, "_typed_ann", None)
    t = ann.get("type") if isinstance(ann, dict) else None
    if isinstance(t, dict):
        return t.get("name")
    return t


def _scalar_params_ok(fn: FnDecl) -> bool:
    for p in fn.params:
        if _ann_type_name(p) not in _SCALARS:
            return False
        if getattr(p.type, "ref", False):
            return False
    return True


def _self_call(fn_id: Optional[int], expr: Node) -> Optional[Call]:
    """*expr* 是 ``f(...)`` 对自身的裸名调用 (SA 已解析) 时返回它。"""
    if fn_id is None or not isinstance(expr, Call):
        return None
    ann = expr._typed_ann.get("call")
    if not isinstance(ann, dict) or ann.get("callee_kind") != "fn":
        return None
    if ann.get("callee_ref") != fn_id:
        return None
    return expr


def _is_pure_scalar_expr(expr: Optional[Node], params: frozenset[str]) -> bool:
    """形参/字面量的纯标量运算 (无调用/无方法/无移动/无借用)。"""
    if isinstance(expr, Name):
        return len(expr.parts) == 1
    if isinstance(expr, IntLit):
        return True
    if isinstance(expr, BinOp):
        return (
            _is_pure_scalar_expr(expr.left, params)
            and _is_pure_scalar_expr(expr.right, params)
        )
    if isinstance(expr, UnaryOp):
        return _is_pure_scalar_expr(expr.operand, params)
    return False


def _returns_in(node: object) -> list[ReturnStmt]:
    out: list[ReturnStmt] = []
    if isinstance(node, ReturnStmt):
        out.append(node)
    if isinstance(node, Node):
        for f in _dc_fields(node):
            if f.name in ("line", "column"):
                continue
            out.extend(_returns_in(getattr(node, f.name, None)))
    elif isinstance(node, list):
        for x in node:
            out.extend(_returns_in(x))
    return out


def _contains_self_call(fn_id: Optional[int], node: object) -> bool:
    if isinstance(node, Call):
        ann = node._typed_ann.get("call")
        if (isinstance(ann, dict) and ann.get("callee_kind") == "fn"
                and ann.get("callee_ref") == fn_id):
            return True
    if isinstance(node, Node):
        for f in _dc_fields(node):
            if f.name in ("line", "column"):
                continue
            if _contains_self_call(fn_id, getattr(node, f.name, None)):
                return True
    elif isinstance(node, list):
        for x in node:
            if _contains_self_call(fn_id, x):
                return True
    return False


class _Reassoc:
    def __init__(self, analyzer: Any, program: Program):
        self.az = analyzer
        self.program = program

    # -- 模式匹配 -------------------------------------------------

    def try_rewrite(self, fn: FnDecl) -> bool:
        if len(fn.params) != 1 or not _scalar_params_ok(fn):
            return False
        if fn.return_type is None or _ann_type_name(fn.return_type) not in _SCALARS:
            return False
        fid = fn._typed_id
        body = fn.body
        if not isinstance(body, Block) or len(body.stmts) < 2:
            return False
        stmts = body.stmts
        tail = stmts[-1]
        prefix = stmts[:-1]

        # 尾语句: return self(a) + self(b)
        if not isinstance(tail, ReturnStmt) or not isinstance(tail.value, BinOp):
            return False
        if tail.value.op is not TokenKind.PLUS:
            return False
        left = _self_call(fid, tail.value.left)
        right = _self_call(fid, tail.value.right)
        if left is None or right is None:
            return False
        if len(left.args) != 1 or len(right.args) != 1:
            return False
        a_expr = left.args[0].value
        b_expr = right.args[0].value
        pset = frozenset({fn.params[0].name})
        if not (
            _is_pure_scalar_expr(a_expr, pset)
            and _is_pure_scalar_expr(b_expr, pset)
        ):
            return False
        # b 不得是裸形参自赋值 (p = p 死循环)
        if isinstance(b_expr, Name) and b_expr.parts == [fn.params[0].name]:
            return False

        # 前置块: 必须有基例 return; 不得含 self-call; 基例值纯标量
        base_returns: list[ReturnStmt] = []
        for s in prefix:
            base_returns.extend(_returns_in(s))
        if not base_returns:
            return False
        for s in prefix:
            if _contains_self_call(fid, s):
                return False
        for r in base_returns:
            if r.value is None or not _is_pure_scalar_expr(r.value, pset):
                return False

        return self._emit(fn, a_expr, b_expr, prefix)

    # -- 改写发射 -------------------------------------------------

    def _emit(
        self, fn: FnDecl, a_expr: Node, b_expr: Node, prefix: list[Node]
    ) -> bool:
        az = self.az
        line, column = fn.line, fn.column
        pname = fn.params[0].name
        acc_t = _ann_type_name(fn.return_type)
        ret_t = acc_t
        acc_name = az._fresh_desugar_name(self.program, "acc")

        def type_ann() -> dict:
            """ann.type 的 dict 形态 (与 SA _ann_type 同构, 后端按
            ``{"name": ...}`` 消费)。"""
            return {"name": acc_t}

        def name_node(nm: str) -> Name:
            n = Name(line, column, [nm])
            n._typed_ann["type"] = type_ann()
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
            self._rewrite_base_returns(clone, acc_name, name_node)
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
        p_assign = Assign(line, column, name_node(pname),
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

    def _rewrite_base_returns(
        self, node: Node, acc_name: str, name_node
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
                    self._rewrite_base_returns(v, acc_name, name_node)
                elif isinstance(v, list):
                    for x in v:
                        if isinstance(x, Node):
                            self._rewrite_base_returns(x, acc_name, name_node)


def optimize_reassociation(program: Program, analyzer: Any) -> None:
    """Rewrite every matching ``FnDecl`` in *program* (in place)."""
    reassoc = _Reassoc(analyzer, program)
    files = getattr(program, "_module_file_programs", None)
    if isinstance(files, dict):
        for child in files.values():
            for item in list(child.items):
                if isinstance(item, FnDecl) and item.body is not None:
                    reassoc.try_rewrite(item)
    for item in list(program.items):
        if isinstance(item, FnDecl) and item.body is not None:
            reassoc.try_rewrite(item)
