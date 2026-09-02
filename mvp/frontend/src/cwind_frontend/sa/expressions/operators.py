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

from ...ast_components.ast import Node

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

    def _indexed_type(self: "_Analyzer", recv: Optional[str]) -> Optional[str]:
        recv = self._expand_type(recv)
        if recv is None:
            return None
        base = _base(recv)
        if base == "Map":
            args = _split_args(recv)
            return args[1] if len(args) >= 2 else None
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

    def _element_type(self: "_Analyzer", t: Optional[str]) -> Optional[str]:
        t = self._expand_type(t)
        if t is None:
            return None
        base = _base(t)
        if base in ("Vector", "Set"):
            inner = t[t.find("<") + 1:-1] if "<" in t else None
            return inner if inner and inner != "Any" else None
        if base == "Map":
            args = _split_args(t)
            if len(args) == 2:
                return f"Tuple<{args[0]}, {args[1]}>"
            return "Tuple"
        if base == "Tuple":
            # entry() 的临时迭代标记: Tuple<K, V> 表示“每轮产出 (K, V) 条目”,
            # 不是逐元素遍历普通元组。
            args = _split_args(t)
            return t if args else None
        if base == "String":
            return "String"
        return None
