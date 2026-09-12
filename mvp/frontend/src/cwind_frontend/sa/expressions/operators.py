"""Expression mixin: binary/unary/comparison operator checks and index/element type resolution."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .defs import (
    _RELATIONAL,
    _EQUALITY,
    _BITWISE,
)

from ..types import (
    _INTEGER,
    _NUMERIC,
    _base,
    _common_numeric,
    _split_args,
    _type_mentions,
)

from ...ast_components.ast import IntLit, Node

from ...ast_components.token import TokenKind

if TYPE_CHECKING:
    from ..analyzer import _Analyzer


class ExprOperators:

    def _check_binop(
        self: "_Analyzer",
        op: TokenKind,
        left: Optional[str],
        right: Optional[str],
        node: Node,
    ) -> Optional[str]:
        if op in (TokenKind.AND, TokenKind.OR):
            for side, t in (("left", left), ("right", right)):
                if t is not None and not self._compat_types("Bool", t):
                    self._record_error(
                        f"'{op.value}' requires Bool operands, got {side} {self._fmt_type(t)}",
                        node.line,
                        node.column,
                    )
            return "Bool"
        if op in _EQUALITY:
            if left is not None and right is not None and not self._compat_types(left, right):
                self._record_error(
                    f"cannot compare {self._fmt_type(left)} with {self._fmt_type(right)}",
                    node.line,
                    node.column,
                )
            return "Bool"
        if op in _RELATIONAL:
            for side, t in (("left", left), ("right", right)):
                expanded = self._expand_type(t) if t is not None else None
                if expanded is not None and _base(expanded) not in _NUMERIC:
                    self._record_error(
                        f"'{op.value}' requires numeric operands, got {side} {self._fmt_type(t)}",
                        node.line,
                        node.column,
                    )
            return "Bool"
        if op in _BITWISE:
            for side, t in (("left", left), ("right", right)):
                expanded = self._expand_type(t) if t is not None else None
                if expanded is not None and _base(expanded) not in _INTEGER:
                    self._record_error(
                        f"'{op.value}' requires integer operands, got {side} {self._fmt_type(t)}",
                        node.line,
                        node.column,
                    )
            left_e = self._expand_type(left) if left is not None else None
            right_e = self._expand_type(right) if right is not None else None
            return _common_numeric(left_e, right_e) or "Int"
        if op in (TokenKind.PLUS, TokenKind.MINUS, TokenKind.STAR, TokenKind.SLASH, TokenKind.PERCENT):
            left_e = self._expand_type(left) if left is not None else None
            right_e = self._expand_type(right) if right is not None else None
            if op == TokenKind.PLUS and (left_e == "String" or right_e == "String"):
                other = right_e if left_e == "String" else left_e
                if other is not None and other != "String":
                    self._record_error(
                        f"cannot add String and {self._fmt_type(other)}",
                        node.line,
                        node.column,
                    )
                return "String"
            for side, t in (("left", left), ("right", right)):
                expanded = self._expand_type(t) if t is not None else None
                if expanded is not None and _base(expanded) not in _NUMERIC:
                    self._record_error(
                        f"'{op.value}' requires numeric operands, got {side} {self._fmt_type(t)}",
                        node.line,
                        node.column,
                    )
            return _common_numeric(left_e, right_e) or "Int"
        return None

    def _indexed_type(
        self: "_Analyzer",
        recv: Optional[str],
        index: Optional[Node] = None,
        node: Optional[Node] = None,
    ) -> Optional[str]:
        recv = self._expand_type(recv)
        if recv is None:
            return None
        # todo-75: 指针下标 (p[i], C 指针算术语义) —— 结果为被指标量
        # (后端 cg_expr_index 的 rawptr 分支同口径)。
        if recv.startswith("*const ") or recv.startswith("*mut "):
            pointee = recv.split(" ", 1)[1] if " " in recv else None
            if pointee and pointee in _NUMERIC:
                return pointee
            return None
        base = _base(recv)
        if base == "Map":
            args = _split_args(recv)
            if len(args) < 2:
                return None
            # key 类型校验 (entry[0] 以 Int 索引 Map<String,String> 曾
            # 放行到运行时静默查空) —— 索引必须与 K 一致; 字面量索引
            # 在 key 类型期望下收窄 (与 Map 字面量构造的 expected
            # 下传同构: 后端按 ann.type 物化 key, 否则 tag 失配查空)
            if index is not None and node is not None:
                index_ann = getattr(index, "_typed_ann", {})
                it_raw = (
                    index_ann.get("type")
                    if isinstance(index_ann, dict)
                    else None
                )
                it = self._expand_type(
                    it_raw.get("name")
                    if isinstance(it_raw, dict)
                    else it_raw
                )
                kt = self._expand_type(args[0])
                if it is not None and kt is not None and _base(it) != _base(kt):
                    if isinstance(index, IntLit) and kt in _NUMERIC:
                        self._check_literal_range(args[0], index)
                        self._ann_type(index, args[0])
                    else:
                        self._record_error(
                            f"map key type is {self._fmt_type(kt)}, but "
                            f"the index is {self._fmt_type(it)}",
                            node.line,
                            node.column,
                        )
                        return None
            return args[1]
        if base in ("Vector", "Set"):
            inner = recv[recv.find("<") + 1:-1] if "<" in recv else None
            return inner if inner and inner != "Any" else None
        if base == "String":
            return "String"
        return None

    def _tuple_indexed_type(
        self: "_Analyzer",
        recv: str,
        index: Node,
        node: Node,
    ) -> Optional[str]:
        """Resolve ``tuple[const]``: compile-time index, bounds checked."""
        args = _split_args(recv)
        folded = self._fold_expr(index)
        if not isinstance(folded, int):
            self._record_error(
                "tuple index must be a compile-time integer constant",
                node.line,
                node.column,
            )
            return None
        if folded < 0 or folded >= len(args):
            self._record_error(
                f"tuple '{recv}' has no element at index {folded}",
                node.line,
                node.column,
            )
            return None
        node._typed_ann["tuple_index"] = folded
        t = args[folded]
        if any(_type_mentions(t, name) for name in self.active_generics):
            self._ann_type(node, None)
            return None
        return t

    def _array_indexed_type(
        self: "_Analyzer",
        arr: tuple[str, int],
        index: Node,
        node: Node,
    ) -> Optional[str]:
        """Resolve ``array[const]`` (todo-60): compile-time bounds check."""
        elem, n = arr
        folded = self._fold_expr(index)
        if isinstance(folded, int):
            if folded < 0 or folded >= n:
                self._record_error(
                    f"array index {folded} is out of bounds "
                    f"(length {n})",
                    node.line,
                    node.column,
                )
                return None
        # 非常量索引留给后端做运行时边界检查
        return elem
