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


class _MacroError(FrontendError):
    """A macro-level diagnostic surfaced through the ordinary parse errors."""


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
) -> tuple[list[Token], list[FrontendError]]:
    """Expand every macro definition and call in *tokens*.

    Returns the rewritten token stream plus macro diagnostics.  The
    stream is parse-ready when the error list is empty; with errors the
    caller still parses (the driver drops only the offending spans).
    """
    limit = recursion_limit_from_env()
    errors: list[FrontendError] = []
    defs: dict[str, MacroDef] = {}
    stream = _collect_definitions(tokens, defs, errors)
    rounds = 0
    try:
        while True:
            stream, any_expanded, new_errors = _expand_all(
                stream, defs, next_context, limit
            )
            errors.extend(new_errors)
            before = len(defs)
            stream = _collect_definitions(stream, defs, errors)
            new_defs = len(defs) > before
            if new_defs:
                # Definitions appeared inside expansions; their calls can
                # only resolve from the next round.
                rounds += 1
                if rounds > 512:
                    errors.append(_MacroError(
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
        errors.append(_MacroError(
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
                    args_out, defs
                )
                errors.extend(clean_errors)
                if frame.tainted and not arg_tokens:
                    # A nested call hit the recursion limit and was
                    # dropped: these arguments are not the real ones, so
                    # matching would only produce bogus errors.
                    if frame.parent is not None:
                        frame.parent.tainted = True
                    continue
                spliced, call_errors = _expand_one(
                    frame.call.macro,
                    frame.call.name_tok,
                    frame.call.opener,
                    frame.call.closer,
                    arg_tokens,
                    next_context,
                )
                errors.extend(call_errors)
                if spliced:
                    any_expanded = True
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
                    errors.append(_MacroError(
                        f"the argument group of macro '{tok.value}' is "
                        "not closed",
                        tok.line, tok.column,
                        end_line=tok.end_line, end_column=tok.end_column,
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
                    errors.append(_MacroError(
                        f"recursion depth limit reached while expanding "
                        f"'{tok.value}' (limit {limit})",
                        tok.line, tok.column,
                        end_line=tok.end_line, end_column=tok.end_column,
                        category="recursion limit",
                    ))
                    frame.tainted = True
                    frame.pos = end
                    continue
                # Suspend the current frame right after the call and
                # expand the call's arguments first (innermost-first).
                frame.pos = end
                stack.append(_Frame(
                    list(tokens[pos + 3:end - 1]),
                    "args",
                    args_level,
                    parent=frame,
                    call=_Call(macro, tok, opener, tokens[end - 1]),
                ))
                continue
        frame.out.append(tok)
        frame.pos += 1
    return root.out, any_expanded, errors


def _drop_unknown_calls(
    stream: list[Token],
    defs: dict[str, MacroDef],
    errors: list[FrontendError],
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
                errors.append(_MacroError(
                    f"the argument group of macro '{tok.value}' is not "
                    "closed",
                    tok.line, tok.column,
                    end_line=tok.end_line, end_column=tok.end_column,
                ))
                return out
            errors.append(_MacroError(
                f"cannot find macro '{tok.value}' in this file "
                "(macro_rules! definitions are file-local)",
                tok.line, tok.column,
                end_line=tok.end_line, end_column=tok.end_column,
            ))
            i = end
            continue
        out.append(tok)
        i += 1
    return out


def _strip_unknown_calls(
    args: list[Token],
    defs: dict[str, MacroDef],
) -> tuple[list[Token], list[FrontendError]]:
    """Remove unknown ``name!(...)`` heads from an expanded argument span.

    An argument may only reach a matcher with macro-call syntax still in
    it when the callee does not exist (known callees are expanded before
    matching); strip them so the ordinary fragment parsers never see a
    call they cannot parse.
    """
    errors: list[FrontendError] = []
    cleaned = _drop_unknown_calls(args, defs, errors)
    return cleaned, errors


# -- definitions ------------------------------------------------------------

def _collect_definitions(
    tokens: list[Token],
    defs: dict[str, MacroDef],
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
            i = _consume_definition(tokens, i, defs, errors)
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
    errors: list[FrontendError],
) -> int:
    """Parse one definition at *start*; returns the index after it.

    Layout: ``macro_rules`` ``!`` ``name`` ``{`` rules... ``}``.
    """
    head = tokens[start]
    end = _scan_definition_braces(tokens, start)
    if end is None:
        errors.append(_MacroError(
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
        errors.append(_MacroError(
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
        errors.append(_MacroError(
            f"a macro named '{macro.name}' is already defined in this "
            "file",
            name_tok.line, name_tok.column,
            end_line=name_tok.end_line, end_column=name_tok.end_column,
        ))
    else:
        for rule in macro.rules:
            macro.issues.extend(validate_matcher(rule.matcher, rule.body))
        for issue in macro.issues:
            errors.append(_MacroError(
                issue.message, issue.line, issue.column,
                end_line=issue.end_line, end_column=issue.end_column,
                category=issue.category,
            ))
        defs[macro.name] = macro
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
) -> tuple[list[Token], list[FrontendError]]:
    """Match one call against its rules with fully-expanded arguments.

    Tries each rule in order; the first match wins (rustc reports
    furthest-progress failures when nothing matches).  On any error the
    call is dropped and one diagnostic is reported.
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
            return transcribe(rule.body, matches, context, call_site), []
        except MacroExpandError as exc:
            failures.append(exc)
            break
    best = _best_failure(failures)
    return [], [best]


def _best_failure(failures: list[FrontendError]) -> FrontendError:
    """The failure furthest into the input wins (rustc ``best_failure``),
    approximated by the latest position."""
    best: Optional[FrontendError] = failures[0] if failures else None
    for failure in failures[1:]:
        if best is None or (failure.line, failure.column) > (
            best.line, best.column
        ):
            best = failure
    if best is not None:
        return best
    return _MacroError(
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
