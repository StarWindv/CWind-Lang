"""Compiler-builtin procedure macros (todo-179).

``quote!`` is implemented by the compiler itself, mirroring rustc's
``builtin_macros/quote.rs`` (where ``quote!`` is a builtin procedure
macro, not library code).  It turns a token template into CWind code
that constructs a ``std::proc_macro::TokenStream`` at run time::

    quote! { fn foo ( ) { bar } }
    =>
    std::proc_macro::stream_concat([
        std::proc_macro::stream_of([
            std::proc_macro::token_ident("fn"), ...,
        ]),
    ])

Interpolation is ``#{ expr }`` (a ``#`` immediately followed by a brace
group): the inner tokens are spliced as a CWind expression producing a
``TokenStream``, and ``quote_concat`` appends it to the template's
tokens.  Everything else in the body is literal token data.

The emitted helper names (``quote_concat`` / ``quote_of`` /
``quote_tok``) are prelude re-exports of ``std::proc_macro`` functions:
expression-position three-segment paths (``std::proc_macro::f(...)``)
are not resolvable in CWind's parser (enum-variant disambiguation), so
the builtin must emit bare, prelude-visible names.
"""

from __future__ import annotations

from typing import Optional

from ...ast_components.token import Token, TokenKind
from ...lexer import tokenize
from .collect import _scan_group
from .protocol import token_kind_name

__all__ = ["BUILTIN_MACROS", "expand_builtin"]


def expand_builtin(
    name: str,
    arg_tokens: list[Token],
    anchor: Token,
) -> tuple[Optional[list[Token]], Optional[str]]:
    """Expand a builtin macro call; ``(tokens, error)``.

    Returns ``(None, None)`` when *name* is not a builtin.
    """
    handler = BUILTIN_MACROS.get(name)
    if handler is None:
        return None, None
    return handler(arg_tokens, anchor)


def _expand_quote(
    arg_tokens: list[Token],
    anchor: Token,
) -> tuple[Optional[list[Token]], Optional[str]]:
    parts: list[str] = []
    literal_run: list[Token] = []

    def flush_literals() -> None:
        if not literal_run:
            return
        rendered = ", ".join(
            f"quote_tok({_quote_string(token_kind_name(tok.kind))}, "
            f"{_quote_string(tok.raw)})"
            for tok in literal_run
            if tok.kind != TokenKind.COMMENT
        )
        if rendered:
            parts.append(f"quote_of([{rendered}])")
        literal_run.clear()

    i = 0
    total = len(arg_tokens)
    while i < total:
        tok = arg_tokens[i]
        if (
            tok.kind == TokenKind.HASH
            and i + 1 < total
            and arg_tokens[i + 1].kind == TokenKind.LBRACE
        ):
            end = _scan_group(arg_tokens, i + 1)
            if end is None:
                return None, "quote!: the '#{ ... }' interpolation is not closed"
            flush_literals()
            inner = arg_tokens[i + 2:end - 1]
            parts.append("(" + " ".join(t.raw for t in inner) + ")")
            i = end
            continue
        if tok.kind != TokenKind.COMMENT:
            literal_run.append(tok)
        i += 1
    flush_literals()

    if not parts:
        source = "quote_of([])"
    elif len(parts) == 1 and not parts[0].startswith("("):
        source = parts[0]
    else:
        source = f"quote_concat([{', '.join(parts)}])"
    try:
        tokens = tokenize(source)
    except Exception as exc:  # pragma: no cover - generated code is valid
        return None, f"quote!: cannot build the expansion ({exc})"
    anchored = [
        Token(
            tok.kind, tok.value,
            anchor.line, anchor.column,
            anchor.end_line, anchor.end_column,
            tok.raw, None,
        )
        for tok in tokens
    ]
    return anchored, None


def _quote_string(text: str) -> str:
    out = ['"']
    for ch in text:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


BUILTIN_MACROS = {
    "quote": _expand_quote,
}
