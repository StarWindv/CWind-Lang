"""Reassociation pattern matching (pre-emit predicates).

A rewrite candidate is: single scalar param, scalar return, body is
``prefix…; return self(a) + self(b)`` with pure-scalar a/b, and the
prefix holds base returns free of self-calls.
"""

from __future__ import annotations

from dataclasses import fields as _dc_fields
from typing import NamedTuple, Optional

from ...ast_components.ast import (
    BinOp,
    Block,
    Call,
    FnDecl,
    IntLit,
    Name,
    Node,
    ReturnStmt,
    UnaryOp,
)
from ...ast_components.token import TokenKind

__all__ = ["_SCALARS", "MatchOutcome", "match_reassoc"]

_SCALARS = frozenset({
    "Int", "UInt", "Byte", "Bool",
    "Int8", "Int16", "Int32", "Int64",
    "UInt8", "UInt16", "UInt32", "UInt64",
    "Float", "Float32", "Float64",
})


class MatchOutcome(NamedTuple):
    """Successful pattern match: tail args + prefix statements."""

    a_expr: Node
    b_expr: Node
    prefix: list[Node]


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


def _match_signature(fn: FnDecl) -> Optional[frozenset[str]]:
    """Shape gate: single scalar value param + scalar return."""
    if len(fn.params) != 1 or not _scalar_params_ok(fn):
        return None
    if fn.return_type is None or _ann_type_name(fn.return_type) not in _SCALARS:
        return None
    return frozenset({fn.params[0].name})


def _match_tail(
    fn: FnDecl, pset: frozenset[str]
) -> Optional[tuple[Node, Node, list[Node]]]:
    """Tail = ``return self(a) + self(b)`` with pure-scalar a/b."""
    fid = fn._typed_id
    body = fn.body
    if not isinstance(body, Block) or len(body.stmts) < 2:
        return None
    stmts = body.stmts
    tail = stmts[-1]
    prefix = stmts[:-1]
    if not isinstance(tail, ReturnStmt) or not isinstance(tail.value, BinOp):
        return None
    if tail.value.op is not TokenKind.PLUS:
        return None
    left = _self_call(fid, tail.value.left)
    right = _self_call(fid, tail.value.right)
    if left is None or right is None:
        return None
    if len(left.args) != 1 or len(right.args) != 1:
        return None
    a_expr = left.args[0].value
    b_expr = right.args[0].value
    if not (
        _is_pure_scalar_expr(a_expr, pset)
        and _is_pure_scalar_expr(b_expr, pset)
    ):
        return None
    # b 不得是裸形参自赋值 (p = p 死循环)
    if isinstance(b_expr, Name) and b_expr.parts == [fn.params[0].name]:
        return None
    return a_expr, b_expr, prefix


def _match_prefix(
    fn: FnDecl, prefix: list[Node], pset: frozenset[str]
) -> bool:
    """Prefix must hold base returns; no self-call; pure-scalar bases."""
    fid = fn._typed_id
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
    return True


def match_reassoc(fn: FnDecl) -> Optional[MatchOutcome]:
    """Full pattern match for the accumulator reassociation rewrite."""
    pset = _match_signature(fn)
    if pset is None:
        return None
    tail = _match_tail(fn, pset)
    if tail is None:
        return None
    a_expr, b_expr, prefix = tail
    if not _match_prefix(fn, prefix, pset):
        return None
    return MatchOutcome(a_expr, b_expr, prefix)
