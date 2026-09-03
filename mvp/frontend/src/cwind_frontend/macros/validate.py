"""Macro definition validation (todo-44).

CWind's counterpart of rustc's ``macro_check`` plus the follow-set
checker living in ``mbe/macro_rules.rs``: it proves a matcher can never
parse ambiguously or loop forever before the macro is ever invoked.

Three families of checks, all ported from Rust:

1. **Follow sets** — a fragment that can swallow an unbounded number of
   tokens (``expr``, ``stmt``, ``pat``, ``type``, ``path``) must be
   followed by one of a fixed set of tokens (``FirstSets`` /
   ``check_matcher_core`` / ``is_in_follow``).  This guarantees future
   grammar growth cannot silently change what an existing macro matches.
2. **Empty repetition** — a ``$(...)*`` whose body can match nothing
   would make the matcher hang; it is rejected (``check_lhs_no_empty_seq``).
3. **Binder sanity** — a matcher may not bind the same name twice, and a
   body may not use a name the matcher never binds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..ast_components.token import Token, TokenKind
from .trees import (
    FOLLOW_SETS,
    FRAG_SINGLE,
    Group,
    Kleene,
    Binding,
    Repetition,
    PatternTree,
)

__all__ = ["MacroIssue", "validate_matcher", "check_binders"]

_TYPE_START_KINDS: frozenset[TokenKind] = frozenset({
    TokenKind.IDENTIFIER, TokenKind.FN, TokenKind.AMP, TokenKind.LBRACKET,
    TokenKind.STAR_CONST, TokenKind.STAR_MUT,
})


@dataclass
class MacroIssue:
    """One definition-time problem: message + position (+ optional hint)."""

    message: str
    line: int
    column: int
    end_line: Optional[int] = None
    end_column: Optional[int] = None
    category: Optional[str] = None


class _TokenSet:
    """Set of matcher elements that may appear next (Rust ``TokenSet``).

    ``maybe_empty`` is true iff the corresponding matcher can match the
    empty token sequence.
    """

    __slots__ = ("tokens", "maybe_empty")

    def __init__(self) -> None:
        self.tokens: list[PatternTree] = []
        self.maybe_empty = True

    @staticmethod
    def empty() -> "_TokenSet":
        return _TokenSet()

    @staticmethod
    def singleton(tree: PatternTree) -> "_TokenSet":
        s = _TokenSet()
        s.tokens.append(tree)
        s.maybe_empty = False
        return s

    def replace_with(self, tree: PatternTree) -> None:
        self.tokens = [tree]
        self.maybe_empty = False

    def replace_with_irrelevant(self) -> None:
        self.tokens = []
        self.maybe_empty = False

    def add_one(self, tree: PatternTree) -> None:
        if tree not in self.tokens:
            self.tokens.append(tree)
        self.maybe_empty = False

    def add_one_maybe(self, tree: PatternTree) -> None:
        if tree not in self.tokens:
            self.tokens.append(tree)

    def add_all(self, other: "_TokenSet") -> None:
        for tree in other.tokens:
            if tree not in self.tokens:
                self.tokens.append(tree)
        if not other.maybe_empty:
            self.maybe_empty = False

    def clone(self) -> "_TokenSet":
        copy = _TokenSet()
        copy.tokens = list(self.tokens)
        copy.maybe_empty = self.maybe_empty
        return copy


class _FirstSets:
    """Precomputed FIRST sets for every repetition in a matcher.

    Direct port of Rust ``FirstSets``: a backward scan records, for each
    ``Repetition`` object, the FIRST set of its body; the forward
    :meth:`first_of` then answers "what tokens may start this suffix" for
    arbitrary matcher suffixes.
    """

    def __init__(self, trees: tuple[PatternTree, ...]) -> None:
        self.per_repeat: dict[int, _TokenSet] = {}
        self._build(trees)

    def _build(self, trees: tuple[PatternTree, ...]) -> _TokenSet:
        first = _TokenSet.empty()
        for tree in reversed(trees):
            if isinstance(tree, (Token, Binding)):
                first.replace_with(tree)
            elif isinstance(tree, Group):
                self._build(tree.body)
                first.replace_with(tree.open_token)
            else:  # Repetition
                subfirst = self._build(tree.body)
                self.per_repeat.setdefault(id(tree), subfirst)
                if tree.separator is not None and subfirst.maybe_empty:
                    first.add_one_maybe(tree.separator)
                if (
                    subfirst.maybe_empty
                    or tree.kleene is not Kleene.ONE_OR_MORE
                ):
                    # The sequence may be empty: union, staying empty-able.
                    other = _TokenSet.empty()
                    other.tokens = list(subfirst.tokens)
                    other.maybe_empty = True
                    first.add_all(other)
                else:
                    first = subfirst.clone()
        return first

    def first_of(self, trees: tuple[PatternTree, ...]) -> _TokenSet:
        first = _TokenSet.empty()
        for tree in trees:
            if isinstance(tree, (Token, Binding)):
                first.add_one(tree)
                return first
            if isinstance(tree, Group):
                first.add_one(tree.open_token)
                return first
            # Repetition
            subfirst = self.per_repeat.get(id(tree))
            if subfirst is None:
                subfirst = self.first_of(tree.body)
            if tree.separator is not None and subfirst.maybe_empty:
                first.add_one_maybe(tree.separator)
            first.add_all(subfirst)
            if subfirst.maybe_empty or tree.kleene is not Kleene.ONE_OR_MORE:
                first.maybe_empty = True
                continue
            return first
        return first


def _tree_at(tree: PatternTree) -> tuple[int, int, Optional[int], Optional[int]]:
    """Position (line, column, end_line, end_column) of a matcher element."""
    if isinstance(tree, Token):
        return tree.line, tree.column, tree.end_line, tree.end_column
    if isinstance(tree, Binding):
        t = tree.token
        return t.line, t.column, t.end_line, t.end_column
    if isinstance(tree, Repetition):
        o, c = tree.open_token, tree.close_token
        return o.line, o.column, c.end_line, c.end_column
    o, c = tree.open_token, tree.close_token  # Group
    return o.line, o.column, c.end_line, c.end_column


def _is_close_delim(tree: PatternTree) -> bool:
    return isinstance(tree, Token) and tree.kind in (
        TokenKind.RPAREN, TokenKind.RBRACKET, TokenKind.RBRACE,
    )


def _is_in_follow(
    nxt: PatternTree, fragment: str
) -> tuple[bool, Optional[list[str]]]:
    """Can *fragment* be followed by *nxt*? (Rust ``is_in_follow``)

    Returns ``(allowed, permitted_list)``; ``permitted_list`` feeds the
    "allowed there are" hint when not allowed.
    """
    if _is_close_delim(nxt):
        # Closing a group delimits: any fragment may precede it.
        return True, None
    if fragment in FRAG_SINGLE:
        # One-token fragments cannot swallow a future grammar change.
        return True, None
    if fragment == "vis":
        if isinstance(nxt, Token):
            if nxt.kind in (TokenKind.COMMA, TokenKind.IDENTIFIER):
                return True, None
            if nxt.kind in _TYPE_START_KINDS:
                return True, None
        else:
            return True, None  # nested repetition after vis: allow (conservative)
        return False, ["`,`", "an identifier", "a type"]
    entry = FOLLOW_SETS.get(fragment)
    if entry is None:
        return True, None  # unknown fragment: rejected at parse time
    kinds, keyword_kinds = entry
    if isinstance(nxt, Token):
        if nxt.kind in kinds or nxt.kind in keyword_kinds:
            return True, None
        return False, None
    if isinstance(nxt, Binding):
        # ``$b:block`` may follow a ``type``/``path`` fragment in Rust too.
        if fragment in ("type", "path") and nxt.fragment == "block":
            return True, None
        return False, None
    return False, None


def _describe_next(nxt: PatternTree) -> str:
    if isinstance(nxt, Token):
        return f"`{nxt.raw}`"
    if isinstance(nxt, Binding):
        return f"`${nxt.name}:{nxt.fragment}`"
    return "`...`"


def _check_matcher_core(
    trees: tuple[PatternTree, ...],
    follow: _TokenSet,
    first_sets: _FirstSets,
    issues: list[MacroIssue],
) -> _TokenSet:
    """Port of Rust ``check_matcher_core``; returns the LAST set."""
    last = _TokenSet.empty()
    for i, tree in enumerate(trees):
        suffix = trees[i + 1:]

        def suffix_first_set() -> _TokenSet:
            s = first_sets.first_of(suffix)
            if s.maybe_empty:
                s.add_all(follow)
            return s

        if isinstance(tree, (Token, Binding)):
            if isinstance(tree, Binding) and tree.fragment not in FRAG_SINGLE:
                # Track the fragment as a potential LAST element...
                last.replace_with(tree)
                suffix_first = suffix_first_set()
                # ...and check it against everything that follows.
                for nxt in suffix_first.tokens:
                    allowed, permitted = _is_in_follow(nxt, tree.fragment)
                    if allowed:
                        continue
                    line, col, el, ec = _tree_at(tree)
                    may_be = (
                        "is"
                        if len(last.tokens) == 1 and len(suffix_first.tokens) == 1
                        else "may be"
                    )
                    hint = (
                        f"only {permitted[0]} is allowed after "
                        f"'{tree.fragment}' fragments"
                        if permitted and len(permitted) == 1
                        else None
                    )
                    issues.append(MacroIssue(
                        f"`${tree.name}:{tree.fragment}` {may_be} followed by "
                        f"{_describe_next(nxt)}, which is not allowed for "
                        f"'{tree.fragment}' fragments",
                        line, col, el, ec,
                        category=hint or "fragments like this are "
                        "restricted to keep macro matching deterministic",
                    ))
            else:
                # Plain tokens and one-token fragments may be followed by
                # anything: nothing to track, nothing to check.
                last.replace_with_irrelevant()
            continue
        if isinstance(tree, Group):
            inner_follow = _TokenSet.singleton(tree.close_token)
            _check_matcher_core(tree.body, inner_follow, first_sets, issues)
            last.replace_with_irrelevant()
            continue
        # Repetition: check the interior against the suffix FIRST set,
        # with the separator (if any) added as a permitted follower.
        suffix_first = suffix_first_set()
        interior_follow = suffix_first.clone()
        if tree.separator is not None:
            interior_follow.add_one_maybe(tree.separator)
        sub_last = _check_matcher_core(
            tree.body, interior_follow, first_sets, issues
        )
        if sub_last.maybe_empty:
            last.add_all(sub_last)
        else:
            last = sub_last
    return last


def validate_matcher(
    matcher: Group, body: Group
) -> list[MacroIssue]:
    """All definition-time checks for one rule; empty list == valid."""
    issues: list[MacroIssue] = []
    first_sets = _FirstSets(matcher.body)
    _check_matcher_core(matcher.body, _TokenSet.empty(), first_sets, issues)
    _check_no_empty_seq(matcher.body, issues)
    _check_binders_in_rule(matcher.body, body.body, issues)
    return issues


def _check_no_empty_seq(
    trees: tuple[PatternTree, ...], issues: list[MacroIssue]
) -> None:
    """Reject repetitions that can match nothing (Rust
    ``check_lhs_no_empty_seq``) — the matcher would loop forever."""
    for tree in trees:
        if isinstance(tree, Group):
            _check_no_empty_seq(tree.body, issues)
            continue
        if not isinstance(tree, Repetition):
            continue
        if tree.separator is None and tree.body and all(
            (isinstance(t, Binding) and t.fragment == "vis")
            or (
                isinstance(t, Repetition)
                and t.kleene is not Kleene.ONE_OR_MORE
            )
            for t in tree.body
        ):
            line, col, el, ec = _tree_at(tree)
            issues.append(MacroIssue(
                "this repetition matches the empty token tree, so the "
                "matcher could loop forever",
                line, col, el, ec,
                category="give the repetition a required part or a separator",
            ))
        _check_no_empty_seq(tree.body, issues)


def _collect_binders(
    trees: tuple[PatternTree, ...], out: dict[str, Binding]
) -> Optional[Binding]:
    """First binder name seen twice wins; returns the duplicate."""
    for tree in trees:
        if isinstance(tree, Binding):
            if tree.name in out:
                return tree
            out[tree.name] = tree
        elif isinstance(tree, Group):
            dup = _collect_binders(tree.body, out)
            if dup is not None:
                return dup
        elif isinstance(tree, Repetition):
            dup = _collect_binders(tree.body, out)
            if dup is not None:
                return dup
    return None


def _body_binding_uses(
    trees: tuple[PatternTree, ...], out: list[Binding]
) -> None:
    for tree in trees:
        if isinstance(tree, Binding):
            out.append(tree)
        elif isinstance(tree, Group):
            _body_binding_uses(tree.body, out)
        elif isinstance(tree, Repetition):
            _body_binding_uses(tree.body, out)


def _check_binders_in_rule(
    matcher: tuple[PatternTree, ...],
    body: tuple[PatternTree, ...],
    issues: list[MacroIssue],
) -> None:
    """Duplicate binders in the matcher, unknown binders in the body."""
    bound: dict[str, Binding] = {}
    dup = _collect_binders(matcher, bound)
    if dup is not None:
        line, col, el, ec = _tree_at(dup)
        issues.append(MacroIssue(
            f"duplicated bind name: {dup.name}",
            line, col, el, ec,
        ))
    uses: list[Binding] = []
    _body_binding_uses(body, uses)
    for use in uses:
        if use.name not in bound:
            line, col, el, ec = _tree_at(use)
            issues.append(MacroIssue(
                f"the body uses ${use.name}, but the matcher never binds it",
                line, col, el, ec,
                category="bind it in the matcher, e.g. "
                f"${use.name}:expr",
            ))


def check_binders(
    matcher: tuple[PatternTree, ...], body: tuple[PatternTree, ...]
) -> list[MacroIssue]:
    """Binder-only checks (used when the full follow-set pass is skipped)."""
    issues: list[MacroIssue] = []
    _check_binders_in_rule(matcher, body, issues)
    return issues
