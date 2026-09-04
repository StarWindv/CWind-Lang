"""Macro expansion driver (todo-44): desugar ``macro_rules!`` syntax.

Runs between the lexer and the parser: reads a flat token stream, pulls
out definitions, rewrites every ``name!(...)`` / ``name![...]`` /
``name!{...}`` call with its expansion tokens, and loops until the
stream is macro-free.  Expansion can therefore nest (a macro call
written inside a macro body) and definitions can be *produced* by other
expansions, each with a fresh hygiene context.

The driver is deliberately position-agnostic: a call expands the same
way at item, statement, expression, type and pattern positions, because
the ordinary parser sees only the spliced result.  ``macro_rules``
itself stays a plain identifier — only the ``macro_rules !`` token pair
turns on pattern mode, exactly one file at a time (definitions are
file-local this round; cross-module export rides on the package system,
see readme).

Expansion is a post-order walk driven by an explicit stack (no Python
recursion), so macro nesting depth is bounded only by the recursion
limit — rustc's ``recursion_limit`` analogue, default 128 and overridable
through the ``CWIND_RECURSION_LIMIT`` environment variable (an absent or
non-numeric value falls back to 128).  A separate token budget caps the
total size of expansion output so a doubling macro cannot hang the
process before the depth limit trips.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..ast_components.errors import FrontendError
from ..ast_components.token import Token, TokenKind
from .definition import MacroDef, MacroRule
from .matcher import MacroMatchError, match_rule
from .expander import MacroExpandError, transcribe
from .fragments import FragmentParser
from .pattern import MacroPatternError, MacroTokens, read_group
from .trees import Group, GroupDelim, PatternTree
from .validate import validate_matcher

__all__ = [
    "MacroError",
    "expand_macros",
    "recursion_limit_from_env",
    "MAX_EXPANSION_DEPTH",
    "MAX_EXPANSION_TOKENS",
]

MAX_EXPANSION_DEPTH = 128
# rustc's "expansion ignores token limit" analogue: a hard ceiling on the
# total tokens macro expansion emits.  Without it a doubling macro
# (``m!($x) => { m!($x) + m!($x) }``) explodes 2^depth tokens and hangs
# the parse forever; 1M tokens is far past any legitimate program here.
MAX_EXPANSION_TOKENS = 1_000_000


def recursion_limit_from_env() -> int:
    """The macro recursion limit: ``CWIND_RECURSION_LIMIT`` if set and a
    positive integer, else :data:`MAX_EXPANSION_DEPTH` (128)."""
    raw = os.environ.get("CWIND_RECURSION_LIMIT")
    if raw is not None:
        try:
            value = int(raw.strip())
        except ValueError:
            value = 0
        if value > 0:
            return value
    return MAX_EXPANSION_DEPTH

_DEF_HEAD = "macro_rules"

_OPEN_KINDS = (TokenKind.LPAREN, TokenKind.LBRACKET, TokenKind.LBRACE)
_CLOSE_OF = {
    TokenKind.LPAREN: TokenKind.RPAREN,
    TokenKind.LBRACKET: TokenKind.RBRACKET,
    TokenKind.LBRACE: TokenKind.RBRACE,
}
_DELIM_OF = {
    TokenKind.LPAREN: GroupDelim.PAREN,
    TokenKind.LBRACKET: GroupDelim.BRACKET,
    TokenKind.LBRACE: GroupDelim.BRACE,
}


class MacroError(FrontendError):
    """A macro-level diagnostic surfaced through the ordinary parse errors.

    ``chain`` (optional) is the macro expansion chain (innermost first)
    the diagnostic was raised inside — the same record dicts ``--pass 1``
    reports.  Rendering turns it into notes; see
    :func:`attach_expansion_chains` for the reverse (position-based)
    attachment.
    """

    def __init__(
        self,
        message: str,
        line: int,
        column: int,
        *,
        end_line: Optional[int] = None,
        end_column: Optional[int] = None,
        category: Optional[str] = None,
        source: Optional[str] = None,
        chain: Optional[list[dict]] = None,
    ) -> None:
        super().__init__(
            message,
            line,
            column,
            end_line=end_line,
            end_column=end_column,
            category=category,
            source=source,
        )
        self.expansion_chain = list(chain) if chain else []


# Historic private spelling; the public name is ``MacroError``.
_MacroError = MacroError


def attach_expansion_chains(
    errors: list[FrontendError],
    records: list[dict],
) -> None:
    """Attach expansion-chain notes to diagnostics raised on expanded code.

    Two anchor rules, both driven purely by recorded positions (never by
    names or text):

    - a diagnostic inside a definition's rule body (def-site text) comes
      from the template itself: every expansion of that rule joins the
      chain (innermost first);
    - a diagnostic exactly at a recorded call head comes from the
      expansion's output (expansion-synthesized tokens all carry the
      invocation's position; the original head token is gone).

    Diagnostics that already carry an exact ``expansion_chain`` from the
    driver are left untouched.
    """
    by_body: list[tuple[tuple[int, int, int, int], dict]] = []
    by_position: dict[tuple[int, int], list[dict]] = {}
    for record in records:
        if record.get("kind") != "expansion":
            continue
        by_position.setdefault(
            (record["line"], record["column"]), []
        ).append(record)
        by_body.append((
            (
                record["body_line"], record["body_column"],
                record["body_end_line"], record["body_end_column"],
            ),
            record,
        ))
    if not by_body and not by_position:
        return

    def nesting_depth(record: dict) -> int:
        # The record's own ``chain`` lists its enclosing expansions
        # (outermost..innermost); its depth is that list's length.
        return len(record.get("chain") or [])

    def link(record: dict) -> dict:
        return {
            "macro": record["macro"],
            "def_line": record.get("def_line", 0),
            "def_column": record.get("def_column", 0),
            "def_source": record.get("source"),
            "call_line": record["line"],
            "call_column": record["column"],
        }

    def inside_body(record: dict, line: int, column: int) -> bool:
        start = (record["body_line"], record["body_column"])
        end = (record["body_end_line"], record["body_end_column"])
        return start <= (line, column) <= end

    for exc in errors:
        if getattr(exc, "expansion_chain", None):
            continue
        # Inside a rule body (def-site template text)?
        inside = [
            record
            for _, record in by_body
            if inside_body(record, exc.line, exc.column)
        ]
        # Exactly at a recorded call head (expansion output)?
        found = by_position.get((exc.line, exc.column)) or []
        if not inside and not found:
            continue
        exc.expansion_chain = (
            [link(record) for record in sorted(inside, key=nesting_depth)]
            + [
                link(record)
                for record in sorted(found, key=nesting_depth, reverse=True)
            ]
        )


@dataclass
class _Call:
    """A macro call whose argument span was captured for expansion."""

    macro: MacroDef
    name_tok: Token
    opener: Token
    closer: Token


@dataclass
class _Frame:
    """One entry on the expansion work stack (explicit, no Python
    recursion).

    ``kind`` is ``"root"`` (the whole stream), ``"args"`` (a call's
    argument span, expanded before the call is matched) or ``"output"``
    (the tokens an expansion emitted, which may contain further calls).
    A frame's ``level`` is the macro-nesting depth of its calls: the
    root is 0, a call found in a level-*L* frame gets an args frame at
    level *L+1*, and the expansion's output is scanned at that same
    level.  The recursion limit is checked when an args frame is pushed,
    so a chain of self-invoking macros (each expansion emitting another
    call) trips the limit at exactly ``limit`` expansions.
    """

    tokens: list[Token]
    kind: str
    level: int
    parent: Optional["_Frame"] = None
    call: Optional[_Call] = None   # set on "args" frames
    pos: int = 0
    out: list[Token] = field(default_factory=list)
    # Hygiene context of the expansion whose tokens this frame holds:
    # ``None`` at the root; the *enclosing* expansion's id on ``args``
    # frames; this frame's own expansion id on ``output`` frames.
    context: Optional[int] = None
    # Expansion chain (innermost first) of the macro calls enclosing this
    # frame's tokens; attached to diagnostics raised inside the frame.
    chain: list[dict] = field(default_factory=list)
    # True when a call inside this frame was dropped at the recursion
    # limit: the frame's remaining tokens are not the real arguments
    # (their nested expansion ran away), so an empty completion must not
    # produce bogus match errors.
    tainted: bool = False


class _TokenBudgetExceeded(Exception):
    """Expansion output exceeded the token budget; abort the whole
    desugar (reported once at the outer boundary)."""

    def __init__(self, token: Token, partial: list[Token]) -> None:
        self.token = token
        self.partial = partial
        super().__init__("macro expansion token budget exceeded")


def expand_macros(
    tokens: list[Token],
    next_context: Callable[[], int],
    records: Optional[list[dict]] = None,
) -> tuple[list[Token], list[FrontendError]]:
    """Expand every macro definition and call in *tokens*.

    Returns the rewritten token stream plus macro diagnostics.  The
    stream is parse-ready when the error list is empty; with errors the
    caller still parses (the driver drops only the offending spans).

    When *records* is a list, it is filled with one dict per macro
    definition and one per successful expansion (``--pass 1`` consumes
    them; the same records power the expansion-chain notes attached to
    macro errors, see :func:`attach_expansion_chains`).
    """
    limit = recursion_limit_from_env()
    errors: list[FrontendError] = []
    defs: dict[str, MacroDef] = {}
    stream = _collect_definitions(tokens, defs, records, errors)
    rounds = 0
    try:
        while True:
            stream, any_expanded, new_errors = _expand_all(
                stream, defs, next_context, limit, records
            )
            errors.extend(new_errors)
            before = len(defs)
            stream = _collect_definitions(stream, defs, records, errors)
            new_defs = len(defs) > before
            if new_defs:
                # Definitions appeared inside expansions; their calls can
                # only resolve from the next round.
                rounds += 1
                if rounds > 512:
                    errors.append(MacroError(
                        "macro expansion kept producing new definitions "
                        "round after round",
                        tokens[0].line if tokens else 1,
                        tokens[0].column if tokens else 1,
                        category="recursion limit",
                    ))
                    return stream, errors
                continue
            if not any_expanded:
                # Nothing expanded and no new definitions: any remaining
                # ``name!(...)`` heads refer to macros that do not exist.
                stream = _drop_unknown_calls(stream, defs, errors)
                return stream, errors
    except _TokenBudgetExceeded as abort:
        errors.append(MacroError(
            "macro expansion exceeded the token limit "
            f"({MAX_EXPANSION_TOKENS}) — does an expansion duplicate its "
            "input?",
            abort.token.line, abort.token.column,
            end_line=abort.token.end_line, end_column=abort.token.end_column,
            category="expansion token limit",
        ))
        return abort.partial, errors


def _expand_all(
    stream: list[Token],
    defs: dict[str, MacroDef],
    next_context: Callable[[], int],
    limit: int,
    records: Optional[list[dict]] = None,
) -> tuple[list[Token], bool, list[FrontendError]]:
    """Fully expand *stream* with the current definitions.

    Iterative post-order over the explicit :class:`_Frame` stack: a call
    is matched only after its argument span is fully expanded, and its
    output is itself expanded before control returns to the caller.  The
    recursion limit counts how many expansions are simultaneously in
    flight (the deepest args frame's level).

    Frame completion:
    * root   — ``out`` is the fully expanded stream;
    * args   — the pending call (``call``) is matched against ``out``,
      and its transcription becomes an ``output`` frame;
    * output — the produced tokens are appended to ``parent.out``.
    """
    errors: list[FrontendError] = []
    any_expanded = False
    budget = MAX_EXPANSION_TOKENS
    root = _Frame(list(stream), "root", 0)
    stack: list[_Frame] = [root]
    while stack:
        frame = stack[-1]
        if frame.pos >= len(frame.tokens):
            stack.pop()
            if frame.kind == "root":
                continue  # done: root.out is the result
            if frame.kind == "args":
                assert frame.call is not None
                args_out = frame.out
                arg_tokens, clean_errors = _strip_unknown_calls(
                    args_out, defs, frame.chain
                )
                errors.extend(clean_errors)
                if frame.tainted and not arg_tokens:
                    # A nested call hit the recursion limit and was
                    # dropped: these arguments are not the real ones, so
                    # matching would only produce bogus errors.
                    if frame.parent is not None:
                        frame.parent.tainted = True
                    continue
                spliced, call_errors, record = _expand_one(
                    frame.call.macro,
                    frame.call.name_tok,
                    frame.call.opener,
                    frame.call.closer,
                    arg_tokens,
                    next_context,
                    frame.context,
                    frame.chain,
                )
                errors.extend(call_errors)
                if spliced:
                    any_expanded = True
                    if records is not None and record is not None:
                        records.append(record)
                    budget -= len(spliced)
                    if budget < 0:
                        raise _TokenBudgetExceeded(
                            frame.call.name_tok, list(root.out)
                        )
                    # The caller keeps its scan position past the call;
                    # the expansion output lands in the caller's out.
                    stack.append(_Frame(
                        spliced, "output", frame.level,
                        parent=frame.parent,
                        tainted=frame.tainted,
                        context=record["context"] if record else frame.context,
                        chain=frame.chain,
                    ))
                elif frame.tainted and frame.parent is not None:
                    frame.parent.tainted = True
                continue
            # "output" frame: hand the produced tokens to the caller.
            assert frame.parent is not None
            frame.parent.out.extend(frame.out)
            if frame.tainted:
                frame.parent.tainted = True
            continue
        tokens = frame.tokens
        pos = frame.pos
        tok = tokens[pos]
        if (
            tok.kind == TokenKind.IDENTIFIER
            and pos + 1 < len(tokens)
            and tokens[pos + 1].kind == TokenKind.NOT
        ):
            opener = tokens[pos + 2] if pos + 2 < len(tokens) else None
            if opener is not None and opener.kind in _OPEN_KINDS:
                end = _scan_group(tokens, pos + 2)
                macro = defs.get(str(tok.value))
                if end is None:
                    errors.append(MacroError(
                        f"the argument group of macro '{tok.value}' is "
                        "not closed",
                        tok.line, tok.column,
                        end_line=tok.end_line, end_column=tok.end_column,
                        chain=frame.chain,
                    ))
                    frame.pos = len(tokens)
                    continue
                if macro is None:
                    # Unknown macro: copy the span through unchanged.  It
                    # may only resolve once a later expansion round
                    # registers the name; if no round does, the final
                    # ``_drop_unknown_calls`` reports it and drops it.
                    frame.out.extend(tokens[pos:end])
                    frame.pos = end
                    continue
                if macro.issues:
                    # Invalid definition: already reported where it was
                    # defined; the call can never match, drop it.
                    frame.pos = end
                    continue
                args_level = frame.level + 1
                if args_level > limit:
                    errors.append(MacroError(
                        f"recursion depth limit reached while expanding "
                        f"'{tok.value}' (limit {limit})",
                        tok.line, tok.column,
                        end_line=tok.end_line, end_column=tok.end_column,
                        category="recursion limit",
                        chain=frame.chain,
                    ))
                    frame.tainted = True
                    frame.pos = end
                    continue
                # Suspend the current frame right after the call and
                # expand the call's arguments first (innermost-first).
                # The args frame's ``context`` is the *enclosing*
                # expansion's hygiene id (the id for this call's own
                # expansion is generated only when a rule matches, in
                # :func:`_expand_one`, so failed matches never consume
                # ids).
                frame.pos = end
                stack.append(_Frame(
                    list(tokens[pos + 3:end - 1]),
                    "args",
                    args_level,
                    parent=frame,
                    call=_Call(macro, tok, opener, tokens[end - 1]),
                    context=frame.context,
                    chain=[
                        *frame.chain,
                        {
                            "macro": str(tok.value),
                            "def_line": macro.name_token.line if macro.name_token else 0,
                            "def_column": macro.name_token.column if macro.name_token else 0,
                            "def_source": getattr(macro, "def_source", None),
                            "call_line": tok.line,
                            "call_column": tok.column,
                        },
                    ],
                ))
                continue
        frame.out.append(tok)
        frame.pos += 1
    return root.out, any_expanded, errors


def _drop_unknown_calls(
    stream: list[Token],
    defs: dict[str, MacroDef],
    errors: list[FrontendError],
    chain: Optional[list[dict]] = None,
) -> list[Token]:
    """Report and drop ``name!(...)`` heads that no definition provides."""
    out: list[Token] = []
    i = 0
    while i < len(stream):
        tok = stream[i]
        if (
            tok.kind == TokenKind.IDENTIFIER
            and i + 1 < len(stream)
            and stream[i + 1].kind == TokenKind.NOT
            and i + 2 < len(stream)
            and stream[i + 2].kind in _OPEN_KINDS
            and str(tok.value) not in defs
        ):
            end = _scan_group(stream, i + 2)
            if end is None:
                errors.append(MacroError(
                    f"the argument group of macro '{tok.value}' is not "
                    "closed",
                    tok.line, tok.column,
                    end_line=tok.end_line, end_column=tok.end_column,
                    chain=chain,
                ))
                return out
            errors.append(MacroError(
                f"cannot find macro '{tok.value}' in this file "
                "(macro_rules! definitions are file-local)",
                tok.line, tok.column,
                end_line=tok.end_line, end_column=tok.end_column,
                chain=chain,
            ))
            i = end
            continue
        out.append(tok)
        i += 1
    return out


def _strip_unknown_calls(
    args: list[Token],
    defs: dict[str, MacroDef],
    chain: Optional[list[dict]] = None,
) -> tuple[list[Token], list[FrontendError]]:
    """Remove unknown ``name!(...)`` heads from an expanded argument span.

    An argument may only reach a matcher with macro-call syntax still in
    it when the callee does not exist (known callees are expanded before
    matching); strip them so the ordinary fragment parsers never see a
    call they cannot parse.
    """
    errors: list[FrontendError] = []
    cleaned = _drop_unknown_calls(args, defs, errors, chain)
    return cleaned, errors


# -- definitions ------------------------------------------------------------

def _collect_definitions(
    tokens: list[Token],
    defs: dict[str, MacroDef],
    records: Optional[list[dict]],
    errors: list[FrontendError],
) -> list[Token]:
    """Strip ``macro_rules!`` definitions out of the stream, registering
    them (file-wide, position independent).  Definition heads inside a
    call's argument span are left alone: call spans are skipped so an
    argument is never reinterpreted as a definition."""
    out: list[Token] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if (
            tok.kind == TokenKind.IDENTIFIER
            and str(tok.value) == _DEF_HEAD
            and i + 1 < len(tokens)
            and tokens[i + 1].kind == TokenKind.NOT
        ):
            i = _consume_definition(tokens, i, defs, records, errors)
            continue
        if tok.kind == TokenKind.IDENTIFIER and i + 1 < len(tokens) \
                and tokens[i + 1].kind == TokenKind.NOT:
            # A call head: skip its balanced argument span so definitions
            # inside macro arguments are not pre-registered (they only
            # exist once the call expands, like Rust's opaque token args).
            nxt = tokens[i + 2] if i + 2 < len(tokens) else None
            if nxt is not None and nxt.kind in _OPEN_KINDS:
                end = _scan_group(tokens, i + 2)
                if end is not None:
                    out.extend(tokens[i:end])
                    i = end
                    continue
        out.append(tok)
        i += 1
    return out


def _consume_definition(
    tokens: list[Token],
    start: int,
    defs: dict[str, MacroDef],
    records: Optional[list[dict]],
    errors: list[FrontendError],
) -> int:
    """Parse one definition at *start*; returns the index after it.

    Layout: ``macro_rules`` ``!`` ``name`` ``{`` rules... ``}``.
    """
    head = tokens[start]
    end = _scan_definition_braces(tokens, start)
    if end is None:
        errors.append(MacroError(
            "this macro definition is missing its closing '}'",
            head.line, head.column,
            end_line=head.end_line, end_column=head.end_column,
        ))
        return len(tokens)
    if (
        start + 3 >= len(tokens)
        or tokens[start + 2].kind != TokenKind.IDENTIFIER
        or tokens[start + 3].kind != TokenKind.LBRACE
    ):
        errors.append(MacroError(
            "expected 'macro_rules! name { ... }' with a name and a "
            "braced rule body",
            head.line, head.column,
            end_line=head.end_line, end_column=head.end_column,
        ))
        return end
    name_tok = tokens[start + 2]
    macro = MacroDef(name=str(name_tok.value), name_token=name_tok)
    body = tokens[start + 4:end - 1]
    cursor = MacroTokens(list(body))
    try:
        _parse_rules(cursor, macro)
    except MacroPatternError as exc:
        errors.append(exc)
    if macro.name in defs:
        errors.append(MacroError(
            f"a macro named '{macro.name}' is already defined in this "
            "file",
            name_tok.line, name_tok.column,
            end_line=name_tok.end_line, end_column=name_tok.end_column,
        ))
    else:
        for rule in macro.rules:
            macro.issues.extend(validate_matcher(rule.matcher, rule.body))
        for issue in macro.issues:
            errors.append(MacroError(
                issue.message, issue.line, issue.column,
                end_line=issue.end_line, end_column=issue.end_column,
                category=issue.category,
            ))
        defs[macro.name] = macro
        if records is not None:
            records.append({
                "kind": "definition",
                "macro": macro.name,
                "line": name_tok.line,
                "column": name_tok.column,
                "rules": len(macro.rules),
                "source": None,
            })
    return end


def _scan_definition_braces(tokens: list[Token], start: int) -> Optional[int]:
    """Index one past the definition's closing ``}`` (or None).

    The body brace is the one after ``macro_rules ! name``; everything
    from there to its match belongs to the definition.
    """
    i = start + 3  # macro_rules, !, name consumed
    if i < len(tokens) and tokens[i].kind != TokenKind.LBRACE:
        return None  # malformed head; reported by the caller
    depth = 0
    while i < len(tokens):
        kind = tokens[i].kind
        if kind == TokenKind.LBRACE:
            depth += 1
        elif kind == TokenKind.RBRACE:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return None


def _parse_rules(cursor: MacroTokens, macro: MacroDef) -> None:
    """Read the rule list inside ``macro_rules! name { here }``.

    Grammar (CWind flavor of Rust's meta-rule): one or more
    ``( matcher ) => { body }`` pairs, ``;`` optional after each rule.
    """
    while not cursor.at_end():
        skip_tok = cursor.peek()
        if skip_tok is not None and skip_tok.kind == TokenKind.SEMICOLON:
            cursor.next()
            continue
        matcher = read_group(
            cursor, True, "'(' to open the macro matcher"
        )
        arrow = cursor.next()
        if arrow is None or arrow.kind != TokenKind.FAT_ARROW:
            where = arrow if arrow is not None else matcher.close_token
            raise MacroPatternError(
                "expected '=>' between the macro matcher and its body",
                where.line, where.column,
                end_line=where.end_line, end_column=where.end_column,
            )
        body = read_group(
            cursor, False, "'{' to open the macro body"
        )
        macro.rules.append(MacroRule(matcher, body, matcher.open_token))


def _scan_group(tokens: list[Token], open_idx: int) -> Optional[int]:
    """Index one past the group opened at *open_idx* (None if unclosed)."""
    close = _CLOSE_OF[tokens[open_idx].kind]
    depth = 0
    i = open_idx
    while i < len(tokens):
        kind = tokens[i].kind
        if kind == tokens[open_idx].kind:
            depth += 1
        elif kind == close:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return None


def _expand_one(
    macro: MacroDef,
    name_tok: Token,
    opener: Token,
    closer: Token,
    arg_tokens: list[Token],
    next_context: Callable[[], int],
    enclosing_context: Optional[int] = None,
    chain: Optional[list[dict]] = None,
) -> tuple[list[Token], list[FrontendError], Optional[dict]]:
    """Match one call against its rules with fully-expanded arguments.

    Tries each rule in order; the first match wins (rustc reports
    furthest-progress failures when nothing matches).  On any error the
    call is dropped and one diagnostic is reported.  On success also
    returns the ``--pass 1`` expansion record (``context`` is the fresh
    hygiene id of this expansion).
    """
    delim = _DELIM_OF[opener.kind]
    invocation = Group(opener, closer, delim, _group_body(list(arg_tokens)))
    failures: list[FrontendError] = []
    for rule in macro.rules:
        try:
            matches = match_rule(rule.matcher, invocation, FragmentParser())
        except MacroMatchError as exc:
            failures.append(exc)
            continue
        context = next_context()
        call_site = (
            name_tok.line, name_tok.column,
            name_tok.end_line, name_tok.end_column,
        )
        try:
            # Success: the earlier rules' failures are irrelevant.
            spliced = transcribe(rule.body, matches, context, call_site)
        except MacroExpandError as exc:
            failures.append(exc)
            break
        record = {
            "kind": "expansion",
            "macro": macro.name,
            "context": context,
            "line": name_tok.line,
            "column": name_tok.column,
            "end_line": name_tok.end_line,
            "end_column": name_tok.end_column,
            "def_line": macro.name_token.line if macro.name_token else 0,
            "def_column": macro.name_token.column if macro.name_token else 0,
            # The matched rule's body span (def-site coordinates): every
            # template token this expansion emits carries a position
            # inside it, so errors anchored there can be traced back to
            # this call (see :func:`attach_expansion_chains`).
            "body_line": rule.body.open_token.line,
            "body_column": rule.body.open_token.column,
            "body_end_line": rule.body.close_token.end_line,
            "body_end_column": rule.body.close_token.end_column,
            "source": None,
            # Token summary of what the expansion produced (and of the
            # captured argument tokens that got substituted in).
            "tokens": len(spliced),
            "inputs": len(arg_tokens),
            # Full expansion chain (outermost..innermost), the outermost
            # entries shared with nested calls expanded from this body.
            "chain": list(chain or []),
        }
        return spliced, [], record
    best = _best_failure(failures, chain)
    return [], [best], None


def _best_failure(
    failures: list[FrontendError],
    chain: Optional[list[dict]] = None,
) -> FrontendError:
    """The failure furthest into the input wins (rustc ``best_failure``),
    approximated by the latest position."""
    best: Optional[FrontendError] = failures[0] if failures else None
    for failure in failures[1:]:
        if best is None or (failure.line, failure.column) > (
            best.line, best.column
        ):
            best = failure
    if best is not None:
        if isinstance(best, MacroError):
            best.expansion_chain = list(chain or [])
        return best
    return MacroError(
        "macro call matched no rule",
        1, 1,
    )


def _group_body(tokens: list[Token]) -> tuple[PatternTree, ...]:
    """Build the invocation's inner tree: tokens plus balanced groups.

    The matcher works on flat tokens, so groups are flattened anyway;
    the Group tree exists only to keep delimiters attached for the
    flattener (and for future tree-level consumers).
    """
    trees: list[PatternTree] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.kind in _OPEN_KINDS:
            close = _CLOSE_OF[tok.kind]
            depth = 0
            j = i
            while j < len(tokens):
                if tokens[j].kind == tok.kind:
                    depth += 1
                elif tokens[j].kind == close:
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            if j >= len(tokens):  # defensive: driver pre-checks balance
                trees.append(tok)
                i += 1
                continue
            trees.append(Group(
                tok, tokens[j], _DELIM_OF[tok.kind],
                _group_body(tokens[i + 1:j]),
            ))
            i = j + 1
            continue
        trees.append(tok)
        i += 1
    return tuple(trees)
