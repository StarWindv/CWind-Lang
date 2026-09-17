"""Strip ``#[proc_macro]`` items from a token stream (todo-179).

Runs in the same token-level pre-parse pass as ``macro_rules!``
collection: every ``#[proc_macro]`` function is pulled out of the stream
and recorded, so the ordinary parser never sees macro syntax at all.
The stripped stream is returned; the definitions carry their own tokens
for the standalone-program build.

Definition shape (the only accepted one this round)::

    #[proc_macro]
    [pub] fn NAME ( input : TokenStream ) [-> TYPE] { ... }

Anything else (attribute arguments, extra attributes before the item,
restricted visibility) is a definition error: the item is still removed
so the parser keeps a macro-free stream, but calls to it are rejected.
"""

from __future__ import annotations

from typing import Optional

from ...ast_components.token import Token, TokenKind
from .definition import ProcMacroDef
from .errors import ProcMacroError

__all__ = ["collect_proc_macros"]

_OPEN_OF = {
    TokenKind.LPAREN: TokenKind.RPAREN,
    TokenKind.LBRACKET: TokenKind.RBRACKET,
    TokenKind.LBRACE: TokenKind.RBRACE,
}


def collect_proc_macros(
    tokens: list[Token],
    source_path: Optional[str] = None,
    records: Optional[list[dict]] = None,
) -> tuple[list[Token], list[ProcMacroDef], list[ProcMacroError]]:
    """Remove ``#[proc_macro]`` items from *tokens*.

    Returns ``(stream, defs, errors)``.  ``records`` (optional) collects
    one ``kind: "definition"`` dict per accepted definition for the
    ``--pass 1`` report.
    """
    errors: list[ProcMacroError] = []
    defs: list[ProcMacroDef] = []
    out: list[Token] = []
    i = 0
    total = len(tokens)
    while i < total:
        tok = tokens[i]
        if tok.kind == TokenKind.HASH:
            attr = _scan_attribute(tokens, i)
            if attr is not None:
                attr_end, attr_name, has_args, attr_tok = attr
                if attr_name in ("proc_macro", "proc_macro_attribute"):
                    if has_args:
                        errors.append(_err(
                            "the 'proc_macro' attribute does not take "
                            "arguments",
                            attr_tok,
                        ))
                        # The definition is invalid: strip the whole item
                        # but do not register it (calls stay unknown).
                        end, _definition, item_errors = _read_item(
                            tokens, attr_end, source_path, attr_name
                        )
                        errors.extend(item_errors)
                        i = end
                        continue
                    if attr_end >= total:
                        errors.append(_err(
                            "a 'proc_macro' attribute must be followed by "
                            "a function definition",
                            attr_tok,
                        ))
                        break
                    end, definition, item_errors = _read_item(
                        tokens, attr_end, source_path, attr_name
                    )
                    errors.extend(item_errors)
                    if definition is not None:
                        errors.extend(_extra_attrs(tokens, attr_end))
                        defs.append(definition)
                        if records is not None:
                            records.append({
                                "kind": "definition",
                                "macro": definition.name,
                                "macro_kind": "proc_attribute" if definition.kind == "attribute" else "proc",
                                "line": definition.name_token.line,
                                "column": definition.name_token.column,
                                "rules": 1,
                                "pub": definition.is_pub,
                                "source": source_path,
                            })
                    i = end
                    continue
                # Inert attributes are opaque, including declaration-looking payloads.
                out.extend(tokens[i:attr_end])
                i = attr_end
                continue
        if tok.kind == TokenKind.IDENTIFIER and i + 1 < total \
                and tokens[i + 1].kind == TokenKind.NOT:
            # A call head: copy its balanced argument span verbatim so a
            # definition written inside macro arguments is not collected
            # ahead of its expansion (macro_rules! discipline).
            nxt = tokens[i + 2] if i + 2 < total else None
            if nxt is not None and nxt.kind in _OPEN_OF:
                end = _scan_group(tokens, i + 2)
                if end is not None:
                    out.extend(tokens[i:end])
                    i = end
                    continue
        out.append(tok)
        i += 1
    return out, defs, errors


def strip_proc_macros(
    tokens: list[Token],
    source_path: Optional[str] = None,
    records: Optional[list[dict]] = None,
) -> tuple[list[Token], list[ProcMacroDef], list[ProcMacroError]]:
    """Backward-friendly alias of :func:`collect_proc_macros`."""
    return collect_proc_macros(tokens, source_path, records)


def _read_item(
    tokens: list[Token],
    start: int,
    source_path: Optional[str],
    attribute: str = "proc_macro",
) -> tuple[int, Optional[ProcMacroDef], list[ProcMacroError]]:
    """Read one definition at *start* (the token after the attribute).

    Returns ``(end, definition_or_None, errors)``; ``end`` is always one
    past the item's closing brace (or past the offending token), so the
    caller can recover.
    """
    errors: list[ProcMacroError] = []
    total = len(tokens)
    i = start
    is_pub = False
    if i < total and tokens[i].kind == TokenKind.PUB:
        nxt = tokens[i + 1] if i + 1 < total else None
        if nxt is not None and nxt.kind == TokenKind.LPAREN:
            errors.append(_err(
                "a 'proc_macro' function takes plain 'pub' or nothing; "
                "restricted visibility is not supported",
                tokens[i],
            ))
        is_pub = True
        i += 1
    if i >= total or tokens[i].kind != TokenKind.FN:
        bad = tokens[i] if i < total else tokens[start - 1]
        errors.append(_err(
            "the 'proc_macro' attribute can only be applied to a function "
            "definition ('#[proc_macro] [pub] fn name(...) { ... }')",
            bad,
        ))
        return _recover(tokens, i), None, errors
    fn_tok = tokens[i]
    i += 1
    if i >= total or tokens[i].kind != TokenKind.IDENTIFIER:
        bad = tokens[i] if i < total else fn_tok
        errors.append(_err(
            "expected a name after 'fn' in a procedure macro definition",
            bad,
        ))
        return _recover(tokens, i), None, errors
    name_tok = tokens[i]
    i += 1
    if str(name_tok.value) == "main":
        errors.append(_err(
            "a procedure macro cannot be named 'main' (the generated "
            "standalone program needs that name)",
            name_tok,
        ))
    if i >= total or tokens[i].kind != TokenKind.LPAREN:
        bad = tokens[i] if i < total else name_tok
        errors.append(_err(
            "expected '(' with the macro's 'input: TokenStream' parameter",
            bad,
        ))
        return _recover(tokens, i), None, errors
    end_params = _scan_group(tokens, i)
    if end_params is None:
        errors.append(_err(
            "the parameter list of this procedure macro is not closed",
            tokens[i],
        ))
        return total, None, errors
    params = tokens[i + 1:end_params - 1]
    i = end_params
    if i < total and tokens[i].kind == TokenKind.ARROW:
        # Skip the return type until the body brace (types contain no
        # braces at the top level in CWind).
        i += 1
        while i < total and tokens[i].kind != TokenKind.LBRACE:
            i += 1
    if i >= total or tokens[i].kind != TokenKind.LBRACE:
        bad = tokens[i] if i < total else name_tok
        errors.append(_err(
            "a procedure macro needs a body ('{ ... }'); declarations "
            "without a body are not supported",
            bad,
        ))
        return _recover(tokens, i), None, errors
    end_body = _scan_group(tokens, i)
    if end_body is None:
        errors.append(_err(
            "the body of this procedure macro is not closed",
            tokens[i],
        ))
        return total, None, errors
    if attribute == "proc_macro_attribute":
        parts: list[list[Token]] = [[]]
        for param in params:
            if param.kind == TokenKind.COMMA:
                parts.append([])
            elif param.kind != TokenKind.COMMENT:
                parts[-1].append(param)
        if parts and not parts[-1]:
            parts.pop()
        def stream_type(ts: list[Token]) -> bool:
            return [t.raw for t in ts] in (
                ["TokenStream"], ["std", "::", "proc_macro", "::", "TokenStream"]
            )
        valid = len(parts) == 2 and all(
            len(p) >= 3 and p[0].kind == TokenKind.IDENTIFIER
            and p[1].kind == TokenKind.COLON and stream_type(p[2:])
            for p in parts
        )
        result = tokens[end_params:i]
        if not valid or not result or result[0].kind != TokenKind.ARROW \
                or not stream_type(result[1:]):
            errors.append(_err(
                "a 'proc_macro_attribute' function requires two ordinary parameters "
                "(attr: TokenStream, item: TokenStream) and a TokenStream return type",
                name_tok,
            ))
    definition = ProcMacroDef(
        name=str(name_tok.value),
        is_pub=is_pub,
        name_token=name_tok,
        fn_tokens=list(tokens[start:end_body]),
        fn_start=start,
        fn_end=end_body,
        file_tokens=list(tokens),
        source_path=source_path,
        kind="attribute" if attribute == "proc_macro_attribute" else "function",
    )
    if definition.kind == "attribute":
        definition.issues = list(errors)
    return end_body, definition, errors


def _extra_attrs(tokens: list[Token], start: int) -> list[ProcMacroError]:
    """Reject attributes stacked between ``#[proc_macro]`` and the item."""
    errors: list[ProcMacroError] = []
    i = start
    if 0 <= i < len(tokens) and tokens[i].kind == TokenKind.PUB:
        i += 1
    if i < len(tokens) and tokens[i].kind == TokenKind.HASH:
        errors.append(_err(
            "attributes other than 'proc_macro' are not supported on a "
            "procedure macro definition",
            tokens[i],
        ))
    return errors


def _recover(tokens: list[Token], start: int) -> int:
    """Skip to a safe resynchronisation point after a malformed item."""
    i = start
    total = len(tokens)
    while i < total:
        tok = tokens[i]
        if tok.kind in _OPEN_OF:
            end = _scan_group(tokens, i)
            if end is not None:
                return end
            return total
        if tok.kind == TokenKind.SEMICOLON:
            return i + 1
        i += 1
    return total


def _scan_attribute(
    tokens: list[Token], start: int
) -> Optional[tuple[int, str, bool, Token]]:
    """Scan ``#[name]`` / ``#[name(args)]`` at *start*.

    Returns ``(end, name, has_args, hash_token)`` or ``None`` when the
    tokens at *start* are not an attribute.
    """
    total = len(tokens)
    if tokens[start].kind != TokenKind.HASH:
        return None
    if start + 1 >= total or tokens[start + 1].kind != TokenKind.LBRACKET:
        return None
    i = start + 2
    if i >= total or tokens[i].kind != TokenKind.IDENTIFIER:
        return None
    name_tok = tokens[i]
    i += 1
    has_args = i < total and tokens[i].kind == TokenKind.LPAREN
    depth = 1
    while i < total:
        kind = tokens[i].kind
        if kind == TokenKind.LBRACKET:
            depth += 1
        elif kind == TokenKind.RBRACKET:
            depth -= 1
            if depth == 0:
                return i + 1, str(name_tok.value), has_args, tokens[start]
        i += 1
    return None


def _scan_group(tokens: list[Token], open_idx: int) -> Optional[int]:
    """Index one past the group opened at *open_idx* (None if unclosed)."""
    open_kind = tokens[open_idx].kind
    close_kind = _OPEN_OF.get(open_kind)
    if close_kind is None:
        return None
    depth = 0
    i = open_idx
    total = len(tokens)
    while i < total:
        kind = tokens[i].kind
        if kind == open_kind:
            depth += 1
        elif kind == close_kind:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return None


def attribute_item_end(tokens: list[Token], start: int) -> Optional[int]:
    """Find an item's lexical boundary without parsing its unexpanded body.

    Also accepts member declarations (including fields). Attributes and
    balanced argument/type groups never terminate an item.
    """
    i = start
    while i < len(tokens) and tokens[i].kind == TokenKind.HASH:
        attr = _scan_attribute(tokens, i)
        if attr is None:
            return None
        i = attr[0]
    if i < len(tokens) and tokens[i].kind == TokenKind.PUB:
        i += 1
        if i < len(tokens) and tokens[i].kind == TokenKind.LPAREN:
            i = _scan_group(tokens, i) or len(tokens)
    if i >= len(tokens):
        return None
    head = tokens[i].kind
    braced = head in (
        TokenKind.FN, TokenKind.STRUCT, TokenKind.ENUM, TokenKind.TRAIT,
        TokenKind.IMPL, TokenKind.EXTRA, TokenKind.GROUP, TokenKind.EXTERN,
        TokenKind.MOD, TokenKind.TYPE,
    )
    terminated = head in (TokenKind.CONST, TokenKind.STATIC, TokenKind.TYPEDEF,
                          TokenKind.USE)
    field = (head == TokenKind.IDENTIFIER and i + 1 < len(tokens)
             and tokens[i + 1].kind == TokenKind.COLON)
    if not (braced or terminated or field):
        return None
    j = i + 1
    while j < len(tokens):
        kind = tokens[j].kind
        if kind == TokenKind.SEMICOLON or (field and kind == TokenKind.COMMA):
            return j + 1
        if kind == TokenKind.RBRACE:
            return j if field else None
        if kind in _OPEN_OF:
            end = _scan_group(tokens, j)
            if end is None:
                return None
            if kind == TokenKind.LBRACE and braced:
                return end
            j = end
        else:
            j += 1
    return len(tokens) if field else None


def _err(message: str, tok: Token) -> ProcMacroError:
    return ProcMacroError(
        message,
        tok.line,
        tok.column,
        end_line=tok.end_line,
        end_column=tok.end_column,
        category="proc macro definition",
    )
