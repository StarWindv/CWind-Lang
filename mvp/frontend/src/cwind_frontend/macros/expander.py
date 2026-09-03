"""Macro transcription (todo-44): fill a rule body with the captured tokens.

CWind's counterpart of rustc ``mbe/transcribe.rs``: walks the body
template, substitutes ``$name`` with the matched token runs, and repeats
``$(...)`` groups in lockstep over the captured sequences.  The output
is a flat token list the ordinary parser then re-reads.

Hygiene rides on :attr:`Token.context`:

* tokens the template supplies literally get the expansion's context id,
  so names bound inside the expansion are mangled at parse time
  (:meth:`Parser._ident_value`) and cannot capture or be captured by
  identifiers outside the macro (Rust's mixed-site hygiene, simplified);
* tokens substituted from a binding keep the *invocation's* context (the
  caller wrote them, so the caller's scoping applies).  This is what
  makes ``$x`` behave like the text the user passed, not like macro
  internals.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..ast_components.errors import FrontendError
from ..ast_components.token import Token, TokenKind
from .trees import (
    Group,
    Binding,
    Repetition,
    PatternTree,
)
from .matcher import MatchedSeq, MatchedToken, NamedMatch

__all__ = ["MacroExpandError", "transcribe"]


class MacroExpandError(FrontendError):
    """The body does not fit the captured bindings (writer's mistake)."""


@dataclass
class _RepeatFrame:
    """One active repetition during transcription (rustc ``Frame`` +
    ``repeats`` collapsed): index into the sequence and the lockstep
    length."""

    rep: Repetition
    open_token: Token
    close_token: Token
    length: int
    iteration: int = 0


class _Expander:
    def __init__(
        self,
        body: Group,
        matches: dict[str, NamedMatch],
        context: int,
        call_site: tuple[int, int, int, int],
    ) -> None:
        self.body = body
        self.matches = matches
        self.context = context
        # Expansion-synthesized tokens carry the invocation position so
        # diagnostics point at the call, not at the (virtual) definition.
        self.line, self.column, self.end_line, self.end_column = call_site
        self.stack: list[tuple[str, tuple[PatternTree, ...], int]] = []
        # Stack entries: ("root", body trees, 0) | ("group", trees, 0) |
        # ("repeat", trees, length).  Iteration state lives in
        # :class:`_RepeatFrame` list below.
        self.frames: list[_RepeatFrame] = []
        self.result: list[Token] = []
        self.result_stack: list[list[Token]] = []
        # Close delimiters of groups being transcribed, innermost last.
        self._pending_close: list[Token] = []

    def run(self) -> list[Token]:
        self.stack.append(("root", self.body.body, 0))
        while self.stack:
            kind, trees, idx = self.stack[-1]
            if idx < len(trees):
                self.stack[-1] = (kind, trees, idx + 1)
                self._visit(trees[idx])
                continue
            # Exhausted this frame.
            if kind == "repeat":
                frame = self.frames[-1]
                frame.iteration += 1
                if frame.iteration < frame.length:
                    # Another iteration: emit the separator and restart.
                    self.stack[-1] = (kind, trees, 0)
                    if frame.rep.separator is not None:
                        self.result.append(
                            frame.rep.separator.with_context(self.context)
                        )
                    continue
                self.frames.pop()
                self.stack.pop()
                continue
            self.stack.pop()
            if kind == "root":
                return self.result
            if kind == "group":
                # Step back into the parent level with the group's tokens
                # plus its closing delimiter.
                inner = self.result
                self.result = self.result_stack.pop()
                self.result.extend(inner)
                close = self._pending_close.pop()
                self.result.append(close.with_context(self.context))
        raise MacroExpandError(  # pragma: no cover
            "macro transcription ended without returning",
            self.line, self.column,
        )

    def _visit(self, tree: PatternTree) -> None:
        if isinstance(tree, Token):
            self.result.append(tree.with_context(self.context))
            return
        if isinstance(tree, Group):
            # Delimiters are part of the output (the re-parser rebuilds
            # groups from them); inner tokens join a fresh result list
            # that is appended when the frame pops.
            self.result.append(tree.open_token.with_context(self.context))
            self.stack.append(("group", tree.body, 0))
            self.result_stack.append(self.result)
            self.result = []
            # Schedule the closing delimiter: emit it right after the
            # inner frame is joined back (done in run()'s group pop).
            self._pending_close.append(tree.close_token)
            return
        if isinstance(tree, Repetition):
            length = self._repetition_length(tree)
            if length == 0:
                if tree.kleene.value == "+":
                    raise MacroExpandError(
                        "this must repeat at least once, but its bindings "
                        "captured no iterations",
                        self.line, self.column,
                    )
                return
            self.frames.append(_RepeatFrame(
                tree, tree.open_token, tree.close_token, length
            ))
            self.stack.append(("repeat", tree.body, 0))
            return
        # Binding use.
        assert isinstance(tree, Binding)
        if tree.name not in self.matches:
            # Not bound by this matcher: the body is likely the template
            # of another macro this one defines (rustc re-emits the
            # ``$name`` verbatim so the inner definition survives).
            self.result.append(tree.token.with_context(self.context))
            self.result.append(Token(
                TokenKind.IDENTIFIER, tree.name,
                tree.token.line, tree.token.column,
                tree.token.end_line, tree.token.end_column,
                tree.name, self.context,
            ))
            return
        matched = self._lookup(tree.name)
        if matched is None:
            raise MacroExpandError(
                f"variable '{tree.name}' is still repeating at this depth",
                self.line, self.column,
            )
        if isinstance(matched, MatchedSeq):
            raise MacroExpandError(
                f"variable '{tree.name}' is still repeating at this depth",
                self.line, self.column,
            )
        # Invocation tokens keep the caller's context (identity).
        self.result.extend(matched.tokens)

    # -- lockstep sizing (rustc ``lockstep_iter_size``) ----------------------

    def _repetition_length(self, rep: Repetition) -> int:
        length: Optional[int] = None
        owner: Optional[str] = None
        for tree in rep.body:
            for name in self._tree_binding_names(tree):
                matched = self._lookup(name)
                if matched is None:
                    continue
                if isinstance(matched, MatchedSeq):
                    if length is None:
                        length = len(matched.items)
                        owner = name
                    elif len(matched.items) != length:
                        raise MacroExpandError(
                            f"meta-variable '{owner}' repeats {length} "
                            f"time(s), but '{name}' repeats "
                            f"{len(matched.items)}",
                            self.line, self.column,
                        )
        if length is None:
            raise MacroExpandError(
                "attempted to repeat a body that binds no repeating "
                "variable",
                self.line, self.column,
            )
        return length

    def _lookup(self, name: str) -> Optional[NamedMatch]:
        """The binding visible at the current repetition depth (rustc
        ``lookup_cur_matched``): walk the enclosing frames outermost-first
        and descend one level per repetition."""
        matched = self.matches.get(name)
        if matched is None:
            return None
        for frame in self.frames:
            if isinstance(matched, MatchedSeq):
                if frame.iteration < len(matched.items):
                    matched = matched.items[frame.iteration]
                else:
                    return None
            else:
                break
        return matched

    def _tree_binding_names(self, tree: PatternTree) -> list[str]:
        if isinstance(tree, Binding):
            return [tree.name]
        if isinstance(tree, Group):
            names: list[str] = []
            for sub in tree.body:
                names.extend(self._tree_binding_names(sub))
            return names
        if isinstance(tree, Repetition):
            names = []
            for sub in tree.body:
                names.extend(self._tree_binding_names(sub))
            return names
        return []


def transcribe(
    body: Group,
    matches: dict[str, NamedMatch],
    context: int,
    call_site: tuple[int, int, int, int],
) -> list[Token]:
    """Fill *body* with *matches*; tokens carry hygiene id *context*.

    ``call_site`` is the invocation's ``(line, column, end_line,
    end_column)`` used for synthesized token positions.
    """
    return _Expander(body, matches, context, call_site).run()
