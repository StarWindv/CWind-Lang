"""Fragment parsing (todo-44): CWind nonterminals for macro bindings.

rustc calls its main parser ("the black box") when a matcher thread
reaches a ``$x:expr`` style binding.  CWind does the same: a
:class:`FragmentParser` runs the ordinary :class:`~cwind_frontend.parser.Parser`
over a token slice and reports how many tokens the fragment consumed.

Two constraints shape the design:

* the ordinary parser fails hard at end-of-input; fragments never see
  real end-of-input (the invocation continues or closes), so the
  sub-parser gets a synthetic terminator appended and the consumed count
  is read off the cursor instead of trusting the terminator;
* ``may_begin_with`` is the conservative pre-filter rustc keeps
  (``macro_parser::may_begin_with``): it decides whether a thread
  waiting on a fragment can plausibly start at the current token, so
  the NFA does not commit to the black box for hopeless candidates.
"""

from __future__ import annotations

from typing import Optional

from ..ast_components.errors import FrontendError
from ..ast_components.token import Token, TokenKind

__all__ = ["FragmentParser"]


class FragmentParser:
    """Parses one fragment from a token list starting at *pos*.

    The class is stateless; it exists so the matcher holds one injected
    dependency whose behavior tests can stub.
    """

    def may_begin_with(self, fragment: str, token: Token) -> bool:
        """Conservative start-set test (rustc ``may_begin_with``)."""
        kind = token.kind
        if fragment == "token":
            return True
        if fragment == "ident":
            return kind == TokenKind.IDENTIFIER and str(token.value) != "_"
        if fragment == "literal":
            return kind in (
                TokenKind.INTEGER, TokenKind.FLOAT, TokenKind.STRING,
                TokenKind.MINUS,
            ) or (kind == TokenKind.IDENTIFIER
                  and str(token.value) in ("true", "false"))
        if fragment == "expr":
            # Literals, unary operators, grouping, names, closures and the
            # expression-position match.  ``{`` is deliberately excluded:
            # CWind only allows map literals after ``=``, so a brace can
            # never start a matched expression.
            if kind in (
                TokenKind.INTEGER, TokenKind.FLOAT, TokenKind.STRING,
                TokenKind.LPAREN, TokenKind.LBRACKET, TokenKind.MINUS,
                TokenKind.NOT, TokenKind.PIPE, TokenKind.OR, TokenKind.MATCH,
            ):
                return True
            return kind == TokenKind.IDENTIFIER
        if fragment == "stmt":
            # Statement keywords or anything an expression may begin with.
            if kind in (
                TokenKind.LET, TokenKind.RETURN, TokenKind.BREAK,
                TokenKind.CONTINUE, TokenKind.IF, TokenKind.MATCH,
                TokenKind.WHILE, TokenKind.FOR, TokenKind.LBRACE,
            ):
                return True
            return self.may_begin_with("expr", token)
        if fragment == "block":
            return kind == TokenKind.LBRACE
        if fragment == "item":
            # ``use`` / ``mod`` items are consumed by the program loop, not
            # ``_parse_item``, so they cannot be matched as fragments yet.
            if kind in (TokenKind.PUB, TokenKind.HASH):
                return True
            return kind in (
                TokenKind.CONST, TokenKind.TYPE, TokenKind.TYPEDEF,
                TokenKind.STRUCT, TokenKind.ENUM, TokenKind.TRAIT,
                TokenKind.IMPL, TokenKind.EXTRA, TokenKind.GROUP,
                TokenKind.FN, TokenKind.EXTERN,
            )
        if fragment == "pat":
            return kind in (
                TokenKind.INTEGER, TokenKind.FLOAT, TokenKind.STRING,
                TokenKind.IDENTIFIER, TokenKind.LPAREN,
            )
        if fragment in ("type", "path"):
            return kind in (
                TokenKind.IDENTIFIER, TokenKind.FN, TokenKind.AMP,
                TokenKind.LBRACKET, TokenKind.STAR_CONST, TokenKind.STAR_MUT,
            )
        if fragment == "vis":
            # ``vis`` binds zero tokens when ``pub`` is absent, so any
            # token may follow an empty visibility binding; hopeless
            # candidates die at the next matcher element anyway.
            return True
        # Unknown fragment: rejected at definition time, fail closed.
        return False

    def parse_fragment(
        self, fragment: str, tokens: list[Token], pos: int
    ) -> tuple[int, Optional[FrontendError]]:
        """Consume one *fragment* from ``tokens[pos:]``.

        Returns ``(consumed, None)`` or ``(0, error)``.  ``tokens`` is
        the flat invocation view.
        """
        if fragment == "vis":
            return self._parse_vis(tokens, pos)
        entry = _FRAGMENT_PARSERS.get(fragment)
        if entry is None:
            return 0, FrontendError(
                f"unsupported fragment '{fragment}'", 1, 1,
            )
        try:
            return entry(tokens, pos)
        except FrontendError as exc:
            return 0, exc

    def _parse_vis(
        self, tokens: list[Token], pos: int
    ) -> tuple[int, Optional[FrontendError]]:
        """``pub`` / ``pub(...)`` or nothing (Rust ``parse_visibility``)."""
        if pos >= len(tokens) or tokens[pos].kind != TokenKind.PUB:
            return 0, None
        consumed = 1
        if (
            pos + consumed < len(tokens)
            and tokens[pos + consumed].kind == TokenKind.LPAREN
        ):
            depth = 0
            while pos + consumed < len(tokens):
                kind = tokens[pos + consumed].kind
                if kind == TokenKind.LPAREN:
                    depth += 1
                elif kind == TokenKind.RPAREN:
                    depth -= 1
                    if depth == 0:
                        consumed += 1
                        break
                consumed += 1
        return consumed, None


_CALL_OPEN_KINDS = (
    TokenKind.LPAREN, TokenKind.LBRACKET, TokenKind.LBRACE,
)


def _has_macro_call_head(tokens: list[Token]) -> bool:
    """True when *tokens* contains a ``name!(``/``[``/``{`` head.

    Fragment parsing runs the ordinary Parser, whose construction would
    re-enter macro expansion on the slice and recurse without bound when
    a self-nesting expansion leaves its own calls in an argument span.
    Such a slice can never be a valid fragment anyway (the CWind
    expression grammar has no macro-call syntax), so callers fail fast.
    """
    for j, tok in enumerate(tokens[:-1]):
        if (
            tok.kind == TokenKind.IDENTIFIER
            and tokens[j + 1].kind == TokenKind.NOT
            and j + 2 < len(tokens)
            and tokens[j + 2].kind in _CALL_OPEN_KINDS
        ):
            return True
    return False


def _parse_with_entry(
    tokens: list[Token], pos: int, entry: str
) -> tuple[int, Optional[FrontendError]]:
    """Run the ordinary parser's *entry* over ``tokens[pos:]``.

    A synthetic terminator is appended so sub-parsers never hit raw
    end-of-input.  ``stmt`` gets ``}`` (a final expression statement
    parses like a block tail, no ``;`` required); everything else gets
    ``]``, which no expression/type/pattern can consume bare.  Neither
    terminator can be *part* of a capture: the invocation's token groups
    are balanced by the time matching runs (the driver rejects
    unbalanced arguments first).  The consumed count is read off the
    cursor.
    """
    from ..parser.parser import Parser

    real = tokens[pos:]
    if not real:
        return 0, FrontendError("expected a fragment here", 1, 1)
    if _has_macro_call_head(real):
        return 0, FrontendError(
            "expected an expression, found a macro call inside the macro "
            "arguments", real[0].line, real[0].column,
        )
    terminator = "}" if entry == "_parse_stmt" else "]"
    sentinel = Token(
        TokenKind.RBRACE if terminator == "}" else TokenKind.RBRACKET,
        terminator, 10**9, 0, 10**9, 1, terminator,
    )
    parser = Parser(real + [sentinel])
    getattr(parser, entry)()
    consumed = parser.pos
    if consumed <= 0:
        return 0, FrontendError(
            "expected a fragment here", real[0].line, real[0].column,
        )
    return consumed, None


def _parse_item_fragment(
    tokens: list[Token], pos: int
) -> tuple[int, Optional[FrontendError]]:
    """One item, with its optional ``pub`` and ``#[...]`` attributes."""
    from ..parser.parser import Parser

    real = tokens[pos:]
    if not real:
        return 0, FrontendError("expected a fragment here", 1, 1)
    if _has_macro_call_head(real):
        return 0, FrontendError(
            "expected an item, found a macro call inside the macro "
            "arguments", real[0].line, real[0].column,
        )
    sentinel = Token(TokenKind.RBRACE, "}", 10**9, 0, 10**9, 1, "}")
    parser = Parser(real + [sentinel])
    try:
        parser._parse_attributes()
        pub = parser._match(TokenKind.PUB) is not None
        parser._parse_item(pub)
    except FrontendError as exc:
        return 0, exc
    return parser.pos, None


def _parse_ident(
    tokens: list[Token], pos: int
) -> tuple[int, Optional[FrontendError]]:
    tok = tokens[pos] if pos < len(tokens) else None
    if tok is None or tok.kind != TokenKind.IDENTIFIER or str(tok.value) == "_":
        where = (tok.line, tok.column) if tok is not None else (1, 1)
        return 0, FrontendError("expected an identifier", *where)
    return 1, None


def _parse_literal(
    tokens: list[Token], pos: int
) -> tuple[int, Optional[FrontendError]]:
    """A literal, optionally negated (rustc ``parse_literal_maybe_minus``)."""
    tok = tokens[pos] if pos < len(tokens) else None
    if tok is not None and tok.kind == TokenKind.MINUS:
        consumed, err = _parse_literal(tokens, pos + 1)
        if err is not None:
            return 0, err
        return consumed + 1, None
    if tok is None or tok.kind not in (
        TokenKind.INTEGER, TokenKind.FLOAT, TokenKind.STRING,
    ):
        if (
            tok is not None
            and tok.kind == TokenKind.IDENTIFIER
            and str(tok.value) in ("true", "false")
        ):
            return 1, None
        where = (tok.line, tok.column) if tok is not None else (1, 1)
        return 0, FrontendError("expected a literal", *where)
    return 1, None


def _parse_token_tree(
    tokens: list[Token], pos: int
) -> tuple[int, Optional[FrontendError]]:
    """One token tree: a single token or a balanced group (rustc
    ``parse_token_tree``)."""
    tok = tokens[pos] if pos < len(tokens) else None
    if tok is None:
        return 0, FrontendError("expected a token tree", 1, 1)
    closer = {
        TokenKind.LPAREN: TokenKind.RPAREN,
        TokenKind.LBRACKET: TokenKind.RBRACKET,
        TokenKind.LBRACE: TokenKind.RBRACE,
    }.get(tok.kind)
    if closer is None:
        return 1, None
    depth = 0
    count = 0
    while pos + count < len(tokens):
        kind = tokens[pos + count].kind
        if kind == tok.kind:
            depth += 1
        elif kind == closer:
            depth -= 1
            if depth == 0:
                return count + 1, None
        count += 1
    return 0, FrontendError(
        "this group is not closed inside the macro call", tok.line, tok.column
    )


_FRAGMENT_PARSERS = {
    "expr": lambda toks, pos: _parse_with_entry(toks, pos, "_parse_expr"),
    "stmt": lambda toks, pos: _parse_with_entry(toks, pos, "_parse_stmt"),
    "block": lambda toks, pos: _parse_with_entry(toks, pos, "_parse_block"),
    "item": _parse_item_fragment,
    "pat": lambda toks, pos: _parse_with_entry(toks, pos, "_parse_pattern"),
    "type": lambda toks, pos: _parse_with_entry(toks, pos, "_parse_type"),
    "path": lambda toks, pos: _parse_with_entry(toks, pos, "_parse_name_path"),
    "literal": _parse_literal,
    "ident": _parse_ident,
    "token": _parse_token_tree,
}
