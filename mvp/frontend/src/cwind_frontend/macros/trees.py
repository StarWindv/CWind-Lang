"""Macro pattern trees (todo-44): the quoted form of a macro_rules! rule.

This is CWind's counterpart of rustc's ``libsyntax/ext/mbe/quoted.rs``
(archaeology record 4): a token stream annotated with macro structure —
variable bindings, repetitions and delimited groups — so the matcher and
the expander can walk it.  Everything here is plain data; the parser
(:mod:`cwind_frontend.macros.pattern`) builds it, the matcher
(:mod:`cwind_frontend.macros.matcher`) consumes definitions of it, and
the expander (:mod:`cwind_frontend.macros.expander`) consumes bodies.

Design notes
------------
* ``macro_rules`` is *not* a keyword.  The special pattern syntax
  (``$name:fragment``, ``$(...)sep*``) is only recognized between the
  ``macro_rules !`` token pair and the rule body's braces — outside that
  window ``$`` / ``?`` stay the ordinary reserved/error tokens they have
  always been.
* Delimiters are matched in pairs exactly like Rust's: a matcher
  ``(...)`` / ``[...]`` / ``{...}`` in the definition must be closed by
  the same delimiter kind, and the invocation's delimiter is independent
  (``name!(...)`` and ``name![...]`` both work).
* Variable names are plain identifiers; the fragment specifier (the part
  after ``:``) decides how much input a binding consumes at invocation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from ..ast_components.token import Token, TokenKind

__all__ = [
    "FRAGMENTS",
    "FOLLOW_SETS",
    "Kleene",
    "GroupDelim",
    "Binding",
    "Repetition",
    "Group",
    "PatternTree",
]


# Fragment specifiers CWind supports (todo-44).  ``token`` is Rust's ``tt``
# spelled out; ``type`` reuses the existing keyword token.  ``meta`` and
# ``lifetime`` are not supported this round (see readme todo list).
FRAGMENTS: frozenset[str] = frozenset({
    "ident",    # a single identifier
    "expr",     # an expression
    "stmt",     # a statement
    "block",    # a ``{ ... }`` block
    "item",     # a top-level item
    "pat",      # a pattern
    "path",     # a name path (``a::b::c``)
    "literal",  # a literal (integer / float / string / bool, sign allowed)
    "type",     # a type
    "vis",      # a visibility prefix (``pub`` / ``pub(...)`` / empty)
    "token",    # a single token tree (Rust ``tt``)
})

# Fragments that consume exactly one token tree and may therefore be
# followed by anything (Rust ``frag_can_be_followed_by_any``).
FRAG_SINGLE: frozenset[str] = frozenset({
    "ident", "block", "item", "literal", "vis", "token",
})

# Follow sets for fragments that can consume an unbounded number of
# tokens (Rust ``is_in_follow``): a ``$x:expr`` must be followed by one
# of these so future grammar growth cannot silently break existing macros.
# Values are (token kinds, keyword token kinds).
FOLLOW_SETS: dict[str, tuple[frozenset[TokenKind], frozenset[TokenKind]]] = {
    "expr": (frozenset({TokenKind.FAT_ARROW, TokenKind.COMMA, TokenKind.SEMICOLON}), frozenset()),
    "stmt": (frozenset({TokenKind.FAT_ARROW, TokenKind.COMMA, TokenKind.SEMICOLON}), frozenset()),
    "pat": (
        frozenset({
            TokenKind.FAT_ARROW, TokenKind.COMMA, TokenKind.ASSIGN, TokenKind.PIPE,
        }),
        frozenset({TokenKind.IF, TokenKind.IN}),
    ),
    "type": (
        frozenset({
            TokenKind.LBRACE, TokenKind.LBRACKET, TokenKind.FAT_ARROW,
            TokenKind.COMMA, TokenKind.GT, TokenKind.SHR, TokenKind.ASSIGN,
            TokenKind.COLON, TokenKind.SEMICOLON, TokenKind.PIPE,
        }),
        frozenset({TokenKind.AS, TokenKind.WHERE}),
    ),
    "path": (
        frozenset({
            TokenKind.LBRACE, TokenKind.LBRACKET, TokenKind.FAT_ARROW,
            TokenKind.COMMA, TokenKind.GT, TokenKind.SHR, TokenKind.ASSIGN,
            TokenKind.COLON, TokenKind.SEMICOLON, TokenKind.PIPE,
        }),
        frozenset({TokenKind.AS, TokenKind.WHERE}),
    ),
}


class Kleene(Enum):
    """Repetition operator (Rust ``KleeneOp``)."""

    ZERO_OR_MORE = "*"
    ONE_OR_MORE = "+"
    ZERO_OR_ONE = "?"


class GroupDelim(Enum):
    """Which delimiter pair wraps a group."""

    PAREN = ("(", ")")
    BRACKET = ("[", "]")
    BRACE = ("{", "}")

    @property
    def open_kind(self) -> TokenKind:
        return {GroupDelim.PAREN: TokenKind.LPAREN,
                GroupDelim.BRACKET: TokenKind.LBRACKET,
                GroupDelim.BRACE: TokenKind.LBRACE}[self]

    @property
    def close_kind(self) -> TokenKind:
        return {GroupDelim.PAREN: TokenKind.RPAREN,
                GroupDelim.BRACKET: TokenKind.RBRACKET,
                GroupDelim.BRACE: TokenKind.RBRACE}[self]


@dataclass(frozen=True, slots=True)
class Binding:
    """``$name:fragment`` — bind what the fragment matches to *name*.

    Only legal in a matcher (definition side); bodies carry plain
    :class:`Binding` uses without the specifier.
    """

    token: Token           # the ``$`` token (position source)
    name: str
    fragment: str

    def describe(self) -> str:
        return f"${self.name}:{self.fragment}"


@dataclass(frozen=True, slots=True)
class Repetition:
    """``$( body )sep?op`` — repeat the body, separated by *separator*.

    The body may contain bindings and nested repetitions; the expander
    repeats it once per element of the shortest bound sequence (Rust's
    "lockstep iteration").
    """

    open_token: Token
    close_token: Token
    body: tuple["PatternTree", ...]
    separator: Optional[Token]
    kleene: Kleene

    def describe(self) -> str:
        sep = f" {self.separator.raw}" if self.separator is not None else ""
        return f"$(...){sep}{self.kleene.value}"


@dataclass(frozen=True, slots=True)
class Group:
    """A delimited subtree: ``( ... )`` / ``[ ... ]`` / ``{ ... }``."""

    open_token: Token
    close_token: Token
    delim: GroupDelim
    body: tuple["PatternTree", ...]


PatternTree = Token | Binding | Repetition | Group


def token_eq(a: Token, b: Token) -> bool:
    """Matcher-token equality (Rust ``token_name_eq``).

    Kind and value must match: ``foo`` matches only ``foo``, and literal
    ``1`` matches only ``1`` (the value rides in the kind in Rust).  The
    hygiene context is deliberately *not* compared — rustc matches
    context-insensitively too.  Hygiene lives in the tokens' ``context``
    marks consumed at parse time (:meth:`Parser._ident_value`), not here;
    ignoring it is what lets a macro define another macro whose patterns
    still match plain user tokens.
    """
    if a.kind != b.kind:
        return False
    if a.value != b.value:
        return False
    return True


def describe(tree: PatternTree) -> str:
    """Short display form used in diagnostics (Rust ``quoted_tt_to_string``)."""
    if isinstance(tree, Token):
        return tree.raw
    if isinstance(tree, Binding):
        return tree.describe()
    if isinstance(tree, Repetition):
        return tree.describe()
    if isinstance(tree, Group):
        return tree.delim.value[0] + "..."
    raise TypeError(f"unexpected pattern tree {tree!r}")  # pragma: no cover


def count_bindings(trees: tuple[PatternTree, ...] | list[PatternTree]) -> int:
    """How many bindings appear in *trees* (Rust ``count_names``).

    Nested repetitions contribute their own counts; this is the stride
    the matcher allocates per matcher position.
    """
    total = 0
    for tree in trees:
        if isinstance(tree, Binding):
            total += 1
        elif isinstance(tree, Group):
            total += count_bindings(tree.body)
        elif isinstance(tree, Repetition):
            total += count_bindings(tree.body)
    return total
