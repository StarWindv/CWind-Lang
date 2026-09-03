"""Macro invocation matcher (todo-44): NFA match of tokens against a rule.

CWind's counterpart of rustc ``mbe/macro_parser.rs``: a thread-set (NFA)
walker that advances every viable matcher position over the invocation
tokens, one token at a time.  The port keeps rustc's shape:

* matcher positions ("threads") live in ``self.cur``; the *dot* is
  ``idx`` into the position's current element list;
* descending into a delimited group unzips it via a frame stack;
* a ``$(...)`` repetition runs as a child position; per-iteration
  captures accumulate on the child and fold into the parent as a
  :class:`MatchedSeq` when the repetition body completes — exactly one
  fold per surviving path, with the separator/restart logic deciding
  whether the same child continues (rustc's ``idx == len`` branches);
* fragments (``expr``, ``type``, ...) are parsed by the ordinary CWind
  parser over the invocation tokens (rustc's "black-box parser" calls),
  committed when the thread set demands it, else reported as local
  ambiguity.

The public entry point is :func:`match_rule`; success returns
``{name: NamedMatch}`` for :mod:`cwind_frontend.macros.expander`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Union

from ..ast_components.errors import FrontendError
from ..ast_components.token import Token, TokenKind
from .trees import (
    Group,
    Kleene,
    Binding,
    Repetition,
    PatternTree,
    token_eq,
)
from .fragments import FragmentParser

__all__ = [
    "MacroMatchError",
    "MatchedToken",
    "MatchedSeq",
    "NamedMatch",
    "match_rule",
]


class MacroMatchError(FrontendError):
    """The invocation does not (unambiguously) match a rule."""


@dataclass
class MatchedToken:
    """The token run one binding captured (Rust ``MatchedNonterminal``)."""

    tokens: list[Token]


@dataclass
class MatchedSeq:
    """One element per repetition iteration (Rust ``MatchedSeq``)."""

    items: list["NamedMatch"] = field(default_factory=list)


NamedMatch = Union[MatchedToken, MatchedSeq]

# What a position is currently matching against.
_Elts = Union[list[PatternTree], Group]


@dataclass
class _Frame:
    """One descent into a delimited subtree of the matcher."""

    elts: _Elts
    idx: int


@dataclass
class _Pos:
    """A matcher position (Rust ``MatcherPos``).

    ``matches`` holds finished bindings of the enclosing (non-repeating)
    level; ``seq_acc`` holds the per-iteration accumulation while the
    position sits inside a repetition (fragment captures append values,
    inner-repetition folds append :class:`MatchedSeq` elements).  Every
    thread transition deep-copies both, so parallel threads never bleed
    into each other (rustc's ``Rc::make_mut`` copy-on-write).
    """

    top: _Elts
    idx: int
    matches: dict[str, NamedMatch] = field(default_factory=dict)
    seq: Optional[Repetition] = None
    up: Optional["_Pos"] = None
    seq_acc: dict[str, list[NamedMatch]] = field(default_factory=dict)
    stack: list[_Frame] = field(default_factory=list)

    def get_tt(self) -> Optional[PatternTree]:
        """The matcher element under the dot (Rust ``get_tt(idx)``)."""
        top = self.top
        if isinstance(top, Group):
            if self.idx == 0:
                return top.open_token
            if self.idx <= len(top.body):
                return top.body[self.idx - 1]
            if self.idx == len(top.body) + 1:
                return top.close_token
            return None
        if 0 <= self.idx < len(top):
            return top[self.idx]
        return None

    def snapshot(self) -> "_Pos":
        return _Pos(
            top=self.top,
            idx=self.idx,
            matches=dict(self.matches),
            seq=self.seq,
            up=self.up,
            seq_acc={k: list(v) for k, v in self.seq_acc.items()},
            stack=list(self.stack),
        )


class _Matcher:
    """The NFA runner for one rule against one invocation."""

    def __init__(
        self,
        matcher: Group,
        invocation: Group,
        fragment_parser: FragmentParser,
    ) -> None:
        self.matcher = matcher
        self.invocation = invocation
        self.fragments = fragment_parser
        # Flat token view of the invocation contents (nested groups
        # contribute their delimiters); the cursor starts before the
        # first token.
        self.toks: list[Token] = _flatten_invocation(invocation)
        self.pos = 0
        self.cur: list[_Pos] = [_Pos(top=list(matcher.body), idx=0)]
        self.next_pos: list[_Pos] = []
        self.eof: list[_Pos] = []
        self.bb: list[tuple[_Pos, Binding]] = []

    # -- main loop -----------------------------------------------------------

    def run(self) -> dict[str, NamedMatch]:
        while True:
            self.next_pos = []
            self.eof = []
            self.bb = []
            token = self._current_token()
            self._inner_loop(token)
            if token is None:
                return self._finish()
            if self.bb and self.next_pos:
                raise self._local_ambiguity()
            if len(self.bb) > 1:
                raise self._local_ambiguity()
            if not self.bb and not self.next_pos:
                tok = token
                raise MacroMatchError(
                    f"no rule expected the token `{tok.raw}` in this macro "
                    "call",
                    tok.line, tok.column,
                    end_line=tok.end_line, end_column=tok.end_column,
                )
            if self.bb:
                item, binding = self.bb[0]
                self._consume_fragment(item, binding)
                self.cur = [item]
            else:
                self.cur = self.next_pos
                self.pos += 1

    # -- thread processing -----------------------------------------------------

    def _inner_loop(self, token: Optional[Token]) -> None:
        """Process every current thread exactly once (rustc
        ``inner_parse_loop``)."""
        while self.cur:
            item = self.cur.pop()
            # Backtrack out of finished delimited subtrees.
            while item.get_tt() is None and item.stack:
                frame = item.stack.pop()
                item.top = frame.elts
                item.idx = frame.idx + 1
            tree = item.get_tt()
            if tree is None:
                if item.seq is not None and item.up is not None:
                    self._end_repetition(item, token)
                else:
                    # End of the whole matcher: valid iff the input ends
                    # here too (this round's ``eof`` list is consulted
                    # only when the invocation is exhausted).
                    self.eof.append(item)
                continue
            if isinstance(tree, Repetition):
                self._start_repetition(item, tree)
                continue
            if isinstance(tree, Binding):
                if self._fragment_may_begin(tree, token):
                    self.bb.append((item, tree))
                # else: the fragment cannot start here; thread dies.
                continue
            if isinstance(tree, Group):
                # Unzip: descend into the group; its open delimiter is
                # element 0, so no input token is consumed this round.
                item.stack.append(_Frame(item.top, item.idx))
                item.top = tree
                item.idx = 0
                self.next_pos.append(item)
                continue
            assert isinstance(tree, Token)
            if token is not None and token_eq(tree, token):
                item.idx += 1
                self.next_pos.append(item)
            # else: this thread dies silently; another may survive.

    def _end_repetition(self, item: _Pos, token: Optional[Token]) -> None:
        """A repetition body finished one iteration (rustc's ``idx == len``
        branch under ``item.up.is_some()``).

        The child's accumulated per-iteration bindings fold into the
        parent as one :class:`MatchedSeq` per name; the child itself may
        continue (separator consumed, or unbounded restart) for the next
        iteration.
        """
        seq = item.seq
        assert seq is not None and item.up is not None
        new_up = item.up.snapshot()
        for name, items in item.seq_acc.items():
            if not items:
                continue
            if new_up.seq is not None:
                # The parent itself sits inside a repetition: this inner
                # result becomes one element of the parent's accumulation.
                new_up.seq_acc.setdefault(name, []).append(
                    MatchedSeq(list(items))
                )
            else:
                new_up.matches[name] = MatchedSeq(list(items))
        new_up.idx += 1
        self.cur.append(new_up)
        if seq.separator is not None:
            if token is not None and token_eq(seq.separator, token):
                again = item.snapshot()
                again.idx = 0
                self.next_pos.append(again)
        elif seq.kleene is not Kleene.ZERO_OR_ONE:
            again = item.snapshot()
            again.idx = 0
            self.cur.append(again)

    def _start_repetition(self, item: _Pos, seq: Repetition) -> None:
        """Spawn the zero-match branch (for ``*``/``?``) and the child."""
        if seq.kleene is not Kleene.ONE_OR_MORE:
            zero = item.snapshot()
            zero.idx += 1
            for name in _body_binding_names(seq.body):
                if zero.seq is not None:
                    # Inside an outer repetition: this iteration's inner
                    # value is the empty sequence.
                    zero.seq_acc[name] = [MatchedSeq([])]
                else:
                    zero.matches[name] = MatchedSeq([])
            self.cur.append(zero)
        child = _Pos(top=list(seq.body), idx=0, seq=seq, up=item)
        self.cur.append(child)

    # -- fragments ---------------------------------------------------------------

    def _fragment_may_begin(
        self, binding: Binding, token: Optional[Token]
    ) -> bool:
        if token is None:
            return False
        return self.fragments.may_begin_with(binding.fragment, token)

    def _consume_fragment(self, item: _Pos, binding: Binding) -> None:
        """Swallow one fragment with the ordinary parser (rustc's
        ``parse_nt``) and record the captured token run."""
        consumed, err = self.fragments.parse_fragment(
            binding.fragment, self.toks, self.pos
        )
        if err is not None:
            raise err
        run = self.toks[self.pos:self.pos + consumed]
        if not run and binding.fragment != "vis":
            raise MacroMatchError(
                f"expected a {binding.fragment} fragment, found the end of "
                "the macro call",
                self.invocation.close_token.line,
                self.invocation.close_token.column,
            )
        matched: NamedMatch = MatchedToken(run)
        if item.seq is not None:
            # Inside a repetition: accumulate per iteration; the
            # MatchedSeq is materialized when the repetition folds.
            item.seq_acc.setdefault(binding.name, []).append(matched)
        else:
            item.matches[binding.name] = matched
        item.idx += 1
        self.pos += consumed

    # -- helpers -------------------------------------------------------------

    def _current_token(self) -> Optional[Token]:
        if self.pos < len(self.toks):
            return self.toks[self.pos]
        return None

    def _finish(self) -> dict[str, NamedMatch]:
        if len(self.eof) == 1:
            return self._collect(self.eof[0])
        if len(self.eof) > 1:
            raise MacroMatchError(
                "ambiguity: multiple ways to match this macro call",
                self.invocation.open_token.line,
                self.invocation.open_token.column,
            )
        close = self.invocation.close_token
        raise MacroMatchError(
            "unexpected end of macro invocation: this rule needs more "
            "tokens",
            close.line, close.column,
        )

    def _collect(self, pos: _Pos) -> dict[str, NamedMatch]:
        """Materialize the final binding table (rustc ``nameize``).

        A top-level thread carries every binding in ``matches``
        (repetition folds and zero-match plants included).
        """
        return dict(pos.matches)

    def _local_ambiguity(self) -> MacroMatchError:
        tok = self._current_token()
        line = tok.line if tok is not None else self.invocation.close_token.line
        col = tok.column if tok is not None else self.invocation.close_token.column
        return MacroMatchError(
            "local ambiguity: the call could continue as a fragment or as "
            "the next matcher token",
            line, col,
            category="reorder the matcher or add a separator",
        )


def _body_binding_names(trees: tuple[PatternTree, ...]) -> list[str]:
    """Every binding name a repetition body introduces (recursively,
    mirroring rustc's ``count_names``)."""
    names: list[str] = []
    for tree in trees:
        if isinstance(tree, Binding):
            names.append(tree.name)
        elif isinstance(tree, Group):
            names.extend(_body_binding_names(tree.body))
        elif isinstance(tree, Repetition):
            names.extend(_body_binding_names(tree.body))
    return names


def _flatten_invocation(group: Group) -> list[Token]:
    """Flatten an invocation group into its token list.

    Nested groups contribute open/close delimiters plus their inner
    tokens (matching happens at the token level, exactly like rustc's
    token-tree stream; the matcher's Group branch unzips *matcher-side*
    groups, while invocation-side groups are consumed whole by fragments
    or matched delimiter-token by delimiter-token).
    """
    out: list[Token] = []
    for tree in group.body:
        if isinstance(tree, Token):
            out.append(tree)
        elif isinstance(tree, Group):
            out.append(tree.open_token)
            out.extend(_flatten_invocation(tree))
            out.append(tree.close_token)
    return out


def match_rule(
    matcher: Group,
    invocation: Group,
    fragment_parser: FragmentParser,
) -> dict[str, NamedMatch]:
    """Match one rule's matcher against one invocation.

    Raises :class:`MacroMatchError` on any failure mode (no rule
    expected this token / ambiguity / missing tokens).
    """
    return _Matcher(matcher, invocation, fragment_parser).run()
