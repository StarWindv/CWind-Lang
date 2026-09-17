"""Token <-> wire-text conversion for procedural macros (todo-179).

The exe talks the line protocol documented in
``.handover/analysis/CWind/ProcMacro/01_protocol.md``: the driver sends
one ``(kind, text)`` pair per token and receives ``T`` records back.
Kinds are the five tree-free names ``ident`` / ``literal`` / ``punct`` /
``group_open`` / ``group_close`` -- enough for ``syn``-style cursors to
tell separators from names without a lexer on the CWind side.

Output text is rebuilt through the ordinary lexer (one token per record)
so keywords, literals and multi-character operators recover their exact
kinds and decoded values; every rebuilt token is anchored at the call
head with no hygiene context (call-site transparency, no mangling).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ...ast_components.token import Token, TokenKind
from ...lexer import tokenize
from .errors import ProcMacroError

__all__ = [
    "token_kind_name",
    "tokens_to_pairs",
    "pairs_to_tokens",
    "PROTOCOL_KINDS",
]

PROTOCOL_KINDS = ("ident", "literal", "punct", "group_open", "group_close")

_LITERAL_KINDS = (TokenKind.INTEGER, TokenKind.FLOAT, TokenKind.STRING)
_GROUP_OPEN = (TokenKind.LPAREN, TokenKind.LBRACKET, TokenKind.LBRACE)
_GROUP_CLOSE = (TokenKind.RPAREN, TokenKind.RBRACKET, TokenKind.RBRACE)


def token_kind_name(kind: TokenKind) -> str:
    """The wire name of a frontend token kind."""
    if kind == TokenKind.IDENTIFIER:
        return "ident"
    if kind in _LITERAL_KINDS:
        return "literal"
    if kind in _GROUP_OPEN:
        return "group_open"
    if kind in _GROUP_CLOSE:
        return "group_close"
    return "punct"


def token_span(token: Token) -> tuple[int, int, int, int]:
    return token.line, token.column, token.end_line, token.end_column


def validated_span(
    span: object, anchor: Token, source: Optional[str] = None,
) -> tuple[int, int, int, int]:
    fallback = token_span(anchor)
    if not isinstance(span, (list, tuple)) or len(span) != 4:
        return fallback
    if any(type(value) is not int or not 1 <= value <= 9223372036854775807 for value in span):
        return fallback
    line, column, end_line, end_column = span
    if (end_line, end_column) < (line, column):
        return fallback
    if source is None:
        return fallback
    try:
        lines = Path(source).read_text(encoding="utf-8").split("\n")
    except (OSError, UnicodeError, ValueError):
        return fallback
    if line > len(lines) or end_line > len(lines):
        return fallback
    column = min(column, len(lines[line - 1]) + 1)
    end_column = min(end_column, len(lines[end_line - 1]) + 1)
    return line, column, end_line, end_column


def tokens_to_pairs(tokens: list[Token]) -> list[list]:
    pairs: list[list] = []
    for tok in tokens:
        if tok.kind == TokenKind.COMMENT:
            continue
        pairs.append([token_kind_name(tok.kind), tok.raw, list(token_span(tok))])
    return pairs


def pairs_to_tokens(
    pairs: list,
    *,
    anchor: Token,
    source: Optional[str] = None,
) -> tuple[list[Token], list[ProcMacroError]]:
    """Rebuild frontend tokens from the exe's ``[kind, text]`` pairs.

    Each ``text`` is lexed on its own: it must produce exactly one token.
    Rebuilt tokens sit at *anchor*'s position with ``context=None``.
    """
    out: list[Token] = []
    errors: list[ProcMacroError] = []
    for pair in pairs:
        if (
            not isinstance(pair, (list, tuple))
            or len(pair) not in (2, 3)
            or not all(isinstance(part, str) for part in pair[:2])
        ):
            errors.append(_err(
                "the macro output contains a malformed token record "
                f"({pair!r})",
                anchor,
                source,
            ))
            continue
        kind_name, text = pair[0], pair[1]
        span = (
            validated_span(pair[2], anchor, source)
            if len(pair) == 3 else token_span(anchor)
        )
        if kind_name not in PROTOCOL_KINDS:
            errors.append(_err(
                f"the macro output uses an unknown token kind "
                f"'{kind_name}'",
                anchor,
                source,
            ))
            continue
        if "\n" in text or "\r" in text:
            errors.append(_err(
                "a macro-emitted token text must stay on one line",
                anchor,
                source,
            ))
            continue
        try:
            lexed = tokenize(text)
        except Exception:
            lexed = []
        if len(lexed) != 1:
            errors.append(_err(
                f"the macro emitted invalid token text {text!r} "
                "(expected exactly one token)",
                anchor,
                source,
            ))
            continue
        token = lexed[0]
        out.append(Token(
            token.kind,
            token.value,
            *span,
            text,
            None,
        ))
    return out, errors


def _err(
    message: str,
    tok: Token,
    source: Optional[str],
) -> ProcMacroError:
    return ProcMacroError(
        message,
        tok.line,
        tok.column,
        end_line=tok.end_line,
        end_column=tok.end_column,
        category="proc macro expansion",
        source=source,
    )
