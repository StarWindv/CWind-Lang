"""Macro pattern builder (todo-44): read a token cursor into a pattern tree.

CWind's counterpart of rustc ``quoted::parse``: walks the tokens inside a
``macro_rules!`` rule (matcher side or body side) and folds
``$name:fragment`` / ``$(...)sep*`` structure into :mod:`.trees` nodes.
On the body side (``expect_matchers=False``) ``$name`` stays a plain
binding use and ``$(...)`` carries no specifier, matching Rust's
distinction between the pattern and the template.

``macro_rules`` is deliberately *not* a keyword: the special syntax is
only recognized here, between the ``macro_rules !`` token pair and the
end of the definition.  Everywhere else ``$`` and ``?`` keep behaving
exactly as they did before todo-44.
"""

from __future__ import annotations

from typing import Optional

from ..ast_components.errors import FrontendError
from ..ast_components.token import Token, TokenKind
from .trees import (
    FRAGMENTS,
    Group,
    GroupDelim,
    Kleene,
    Binding,
    Repetition,
    PatternTree,
)


class MacroPatternError(FrontendError):
    """A structural problem inside a macro_rules! definition."""


class MacroTokens:
    """A cursor over macro definition tokens with one-token lookahead.

    The pattern builder needs ``peek``/``next`` plus the ability to fail
    with exact positions.  An empty stream yields ``None`` (EOF).
    """

    def __init__(self, tokens: list[Token]) -> None:
        self.tokens = tokens
        self.pos = 0

    def peek(self, offset: int = 0) -> Optional[Token]:
        idx = self.pos + offset
        if 0 <= idx < len(self.tokens):
            return self.tokens[idx]
        return None

    def next(self) -> Optional[Token]:
        tok = self.peek()
        if tok is not None:
            self.pos += 1
        return tok

    def at_end(self) -> bool:
        return self.pos >= len(self.tokens)


_DELIM_OPEN = {
    TokenKind.LPAREN: GroupDelim.PAREN,
    TokenKind.LBRACKET: GroupDelim.BRACKET,
    TokenKind.LBRACE: GroupDelim.BRACE,
}


def read_group(
    cursor: MacroTokens, expect_matchers: bool, what: str
) -> Group:
    """Consume one balanced delimiter group from *cursor*.

    ``macro_rules! name`` reads the rule braces with this, each rule's
    matcher is a ``(...)`` group read with ``expect_matchers=True`` and
    each body a ``{...}`` group read with ``False``.  ``what`` shapes the
    error text only.
    """
    tok = cursor.next()
    if tok is None:
        raise MacroPatternError(
            f"unexpected end of macro definition, expected {what}",
            _eof_line(cursor), _eof_column(cursor),
        )
    delim = _DELIM_OPEN.get(tok.kind)
    if delim is None:
        raise MacroPatternError(
            f"expected {what}, found {tok.raw!r}", tok.line, tok.column,
            end_line=tok.end_line, end_column=tok.end_column,
        )
    body: list[PatternTree] = []
    while True:
        nxt = cursor.peek()
        if nxt is None:
            raise MacroPatternError(
                f"this {delim.value[0]} is missing a closing {delim.value[1]}",
                tok.line, tok.column,
                end_line=tok.end_line, end_column=tok.end_column,
            )
        if nxt.kind == delim.close_kind:
            close = cursor.next()
            assert close is not None
            return Group(tok, close, delim, tuple(body))
        body.append(read_tree(cursor, expect_matchers=expect_matchers))


def read_tree(cursor: MacroTokens, *, expect_matchers: bool) -> PatternTree:
    """Read one pattern-tree element (Rust ``parse_tree``).

    ``$`` opens either a repetition (``$(...)``), a binding use
    (``$name``) or a binding declaration (``$name:frag``, matcher side
    only).  Everything else is a literal token or a nested group.
    """
    tok = cursor.next()
    if tok is None:
        raise MacroPatternError(
            "unexpected end of macro definition",
            _eof_line(cursor), _eof_column(cursor),
        )
    if tok.kind == TokenKind.DOLLAR:
        return _read_after_dollar(cursor, tok, expect_matchers)
    delim = _DELIM_OPEN.get(tok.kind)
    if delim is not None:
        # Descend with the same matcher mode: a group in a matcher binds
        # inside, a group in a body still interpolates ``$name`` uses.
        body: list[PatternTree] = []
        while True:
            nxt = cursor.peek()
            if nxt is None:
                raise MacroPatternError(
                    f"this {delim.value[0]} is missing a closing "
                    f"{delim.value[1]}",
                    tok.line, tok.column,
                    end_line=tok.end_line, end_column=tok.end_column,
                )
            if nxt.kind == delim.close_kind:
                close = cursor.next()
                assert close is not None
                return Group(tok, close, delim, tuple(body))
            body.append(read_tree(cursor, expect_matchers=expect_matchers))
    return tok


def _read_after_dollar(
    cursor: MacroTokens, dollar: Token, expect_matchers: bool
) -> PatternTree:
    nxt = cursor.peek()
    if nxt is None:
        raise MacroPatternError(
            "expected a variable or '(' after '$' in the macro definition",
            dollar.line, dollar.column,
            end_line=dollar.end_line, end_column=dollar.end_column,
        )
    # ``$(...)`` opens a repetition.
    if nxt.kind == TokenKind.LPAREN:
        return _read_repetition(cursor, dollar, expect_matchers)
    # ``$name`` (body side) or ``$name:frag`` (matcher side).
    if nxt.kind == TokenKind.IDENTIFIER:
        cursor.next()
        name_tok = nxt
        name = str(name_tok.value)
        if expect_matchers:
            colon = cursor.peek()
            if colon is None or colon.kind != TokenKind.COLON:
                raise MacroPatternError(
                    f"expected ': fragment' after '${name}' in the macro "
                    f"matcher (e.g. ${name}:expr)",
                    name_tok.line, name_tok.column,
                    end_line=name_tok.end_line, end_column=name_tok.end_column,
                )
            cursor.next()
            frag_tok = cursor.next()
            if frag_tok is None or frag_tok.kind not in (
                TokenKind.IDENTIFIER, TokenKind.TYPE,
            ):
                where = (
                    f"found {frag_tok.raw!r}" if frag_tok is not None
                    else "the definition ended"
                )
                at = frag_tok if frag_tok is not None else name_tok
                raise MacroPatternError(
                    f"expected a fragment specifier after '${name}:', {where}",
                    at.line, at.column,
                    end_line=at.end_line, end_column=at.end_column,
                )
            fragment = str(frag_tok.value)
            if fragment not in FRAGMENTS:
                valid = ", ".join(sorted(FRAGMENTS))
                raise MacroPatternError(
                    f"invalid fragment specifier '{fragment}'",
                    frag_tok.line, frag_tok.column,
                    end_line=frag_tok.end_line, end_column=frag_tok.end_column,
                    category=f"valid fragment specifiers are: {valid}",
                )
            return Binding(dollar, name, fragment)
        return Binding(dollar, name, "")
    raise MacroPatternError(
        f"expected an identifier or '(' after '$', found {nxt.raw!r}",
        nxt.line, nxt.column,
        end_line=nxt.end_line, end_column=nxt.end_column,
    )


def _read_repetition(
    cursor: MacroTokens, dollar: Token, expect_matchers: bool
) -> Repetition:
    cursor.next()  # (
    body: list[PatternTree] = []
    while True:
        nxt = cursor.peek()
        if nxt is None:
            raise MacroPatternError(
                "this repetition '(' is missing a closing ')'",
                dollar.line, dollar.column,
                end_line=dollar.end_line, end_column=dollar.end_column,
            )
        if nxt.kind == TokenKind.RPAREN:
            close = cursor.next()
            assert close is not None
            break
        body.append(read_tree(cursor, expect_matchers=expect_matchers))
    separator, kleene = _read_sep_and_kleene(cursor, dollar)
    return Repetition(dollar, close, tuple(body), separator, kleene)


def _read_sep_and_kleene(
    cursor: MacroTokens, dollar: Token
) -> tuple[Optional[Token], Kleene]:
    """Read the optional separator and the Kleene operator (Rust
    ``parse_sep_and_kleene_op``)."""
    first = cursor.next()
    if first is None:
        raise MacroPatternError(
            "expected '*', '+' or '?' after the repetition",
            dollar.line, dollar.column,
            end_line=dollar.end_line, end_column=dollar.end_column,
        )
    op = _kleene_of(first)
    if op is not None:
        return None, op
    # First token is the separator; a Kleene op must follow immediately.
    second = cursor.next()
    if second is None:
        raise MacroPatternError(
            "expected '*', '+' or '?' after the repetition separator "
            f"{first.raw!r}",
            first.line, first.column,
            end_line=first.end_line, end_column=first.end_column,
        )
    op2 = _kleene_of(second)
    if op2 is None:
        raise MacroPatternError(
            f"expected '*', '+' or '?' after the repetition separator, "
            f"found {second.raw!r}",
            second.line, second.column,
            end_line=second.end_line, end_column=second.end_column,
        )
    if op2 is Kleene.ZERO_OR_ONE:
        raise MacroPatternError(
            "the '?' repetition operator does not take a separator",
            first.line, first.column,
            end_line=first.end_line, end_column=first.end_column,
        )
    return first, op2


def _kleene_of(tok: Token) -> Optional[Kleene]:
    if tok.kind == TokenKind.STAR:
        return Kleene.ZERO_OR_MORE
    if tok.kind == TokenKind.PLUS:
        return Kleene.ONE_OR_MORE
    if tok.kind == TokenKind.QUESTION:
        return Kleene.ZERO_OR_ONE
    return None


def _eof_line(cursor: MacroTokens) -> int:
    if cursor.tokens:
        return cursor.tokens[-1].end_line
    return 1


def _eof_column(cursor: MacroTokens) -> int:
    if cursor.tokens:
        return cursor.tokens[-1].end_column
    return 1
