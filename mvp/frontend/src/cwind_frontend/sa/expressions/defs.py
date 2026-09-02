"""Shared expression-check machinery: the single definition point for
every module-level name used by the expression mixins (``names``/
``calls``/``literals``/``operators``/``misc``)."""

from __future__ import annotations

from typing import Optional

from ...ast_components.token import TokenKind


_RELATIONAL: frozenset[TokenKind] = frozenset({TokenKind.LT, TokenKind.GT, TokenKind.LE, TokenKind.GE})


_EQUALITY: frozenset[TokenKind] = frozenset({
    TokenKind.EQ, TokenKind.ADDR_EQ, TokenKind.NE, TokenKind.NOT_LT, TokenKind.NOT_GT,
})


_BITWISE: frozenset[TokenKind] = frozenset({
    TokenKind.AMP, TokenKind.PIPE, TokenKind.CARET, TokenKind.SHL, TokenKind.SHR,
})


def _fn_type_string(params: list[Optional[str]], ret: str) -> str:
    return "fn(" + ", ".join(p or "Any" for p in params) + ") -> " + ret


def _parse_fn_signature(t: str) -> tuple[list[str], str]:
    """Split ``fn(A, B) -> R`` into ``([A, B], R)``."""
    inner = t[len("fn("):t.rfind(")")]
    args = [x.strip() for x in inner.split(",") if x.strip()]
    if "->" in t:
        ret = t.split("->", 1)[1].strip()
    else:
        ret = "None"
    return args, ret
