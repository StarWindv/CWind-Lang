"""Parser mixin: lexical token-stream primitives, error raising and recovery."""

from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import NoReturn, Optional

from .defs import (
    ParseError,
    _STMT_START,
    _TOP_LEVEL_START,
    _NO_PRELUDE_SENTINEL,
)
from ..ast_components.ast import (
    Node,
    Program,
    UseDecl,
)
from ..ast_components.errors import FrontendError
from ..ast_components.token import Token, TokenKind
from ..cfg import (
    CfgContext,
)
from ..macros import expand_macros


class ParserCore:
    def __init__(self, tokens: list[Token]) -> None:
        self.tokens = [t for t in tokens if t.kind != TokenKind.COMMENT]
        # todo-44: macro hygiene.  ``_macro_context`` counts expansions in
        # this parser; every expansion-synthesized token carries its id in
        # ``Token.context`` and identifiers written by the expansion are
        # renamed on read (``_ident_value``) so they cannot collide with
        # user bindings.  Must exist before the desugar below.
        self._macro_context: int = 0
        self._macro_next_context = self._macro_next_context_id
        # todo-44: desugar macros before parsing — pull definitions,
        # expand calls, iterate until the stream is macro-free.  Errors
        # ride alongside the ordinary parse errors (merged by
        # ``parse_program`` so module files keep their own attribution).
        # todo-179: macro expansion moved to the start of ``parse_program``
        # so procedure macros can see the file's ``source_path`` and the
        # project's import roots (both are assigned after construction).
        # ``macro_errors`` / ``macro_records`` keep the macro_rules shape:
        # errors are merged with parse errors, records feed ``--pass 1``.
        self.macro_errors: list[FrontendError] = []
        self.macro_records: list[dict] = []
        self._macros_expanded: bool = False
        self._proc_context = None
        # todo-planB: lazy body materialization.  Imported module files
        # expand macro-free *declarations* eagerly but defer the bodies of
        # ordinary (non-``#[proc_macro]``) functions that contain macro
        # calls: such a body's macros are only built if the function is
        # reachable from the entry.  ``_deferred_spans`` maps a body's
        # opening brace position to its raw (unexpanded) tokens; the
        # parser attaches them to the matching ``FnDecl`` (``_deferred_body``)
        # so a later pass can expand exactly the reachable ones.
        self._defer_macro_bodies: bool = False
        self._deferred_spans: dict[
            tuple[int, int], tuple[list[Token], frozenset[str]]
        ] = {}
        # File-level ``macro_rules!`` definitions (for re-expanding a
        # deferred body in isolation; the body's own token run carries no
        # definitions).
        self._file_macro_defs: dict = {}
        # todo-179: parallel procedure-macro pre-build (``cwindf -j N``).
        self._macro_jobs: int = 1
        self.macro_warnings: list[FrontendError] = []
        self.pos = 0
        self.errors: list[ParseError] = []
        self._pending: deque[Token] = deque()  # synthetic tokens (from `>>` splits)
        self._for_iterable_expr = False
        # todo-165: true while parsing a while-let chain operand, where a
        # top-level ``&& let`` terminates the boolean expression.
        self._let_chain_ctx = False
        # todo-184: 条件括号可选时, 条件位的 '{' 一律是体/臂区开始
        # (抑制结构体/映射字面量判定), 与 _let_chain_ctx 同机制。
        self._cond_expr_ctx = False
        # todo-163: re-export bridging depth guard (alias edges chain).
        self._reexport_depth = 0
        # todo-69: canonical source path -> parsed module, shared by every
        # parser instance in one recursive load.  ``order`` preserves the
        # first-use order so generated declarations are deterministic.
        self._module_cache: dict[str, Program] = {}
        self._module_order: list[str] = []
        self._loading: list[str] = []
        self.import_errors: list[ParseError] = []
        self.current_use_decl: Optional[UseDecl] = None
        # todo-76: only the entry parser injects the prelude.  Imported
        # std modules must be able to import each other without creating a
        # ``prelude -> panic -> prelude`` cycle during bootstrap.
        from ..home import default_import_root

        self._IMPORT_ROOTS_BASE: Path = default_import_root()
        self._auto_prelude_result: object = _NO_PRELUDE_SENTINEL
        self._is_entry_source: bool = False
        # todo-179: procedure-macro standalone programs compile in no-std
        # mode (no implicit std prelude, no whole-tree trait-impl pull).
        self._no_std: bool = False
        # todo-171: entry compile boundary drops the per-process Program
        # caches (a previous SA run in the same process mutates the cached
        # AST nodes in place); set by ``parse_with_errors(flush_cache=...)``.
        self._flush_caches: bool = False
        # todo-144: source file -> canonical dotted module parts memo.
        self._canonical_parts_cache: dict[str, Optional[list[str]]] = {}
        # todo-71/97: the project's own library facade (``lib.wd``), as
        # ``(alias path parts, absolute file)``.  Only the entry parser
        # receives it; its public API is wildcard-imported into main.
        self._package_lib: Optional[tuple[list[str], Path]] = None
        # todo-107: loaded items behind ``use`` lines inside inline ``mod
        # {}`` blocks; they join the root program at the mod branch so the
        # namespace's bodies resolve after flattening.
        self._inline_loaded_items: list[Node] = []
        # todo-86/93: explicit cross-compile target for ``#[cfg]``; ``None``
        # means auto-detect the host.  The context itself is built lazily.
        self._cfg_target_os: Optional[str] = None
        # todo-103/106: explicit target_arch / target_vendor /
        # target_pointer_width overrides for ``#[cfg]`` evaluation.
        self._cfg_target_arch: Optional[str] = None
        self._cfg_target_vendor: Optional[str] = None
        self._cfg_pointer_width: Optional[str] = None
        self._cfg_ctx: Optional[CfgContext] = None

    def _macro_next_context_id(self) -> int:
        self._macro_context += 1
        return self._macro_context

    # -- lazy macro expansion (todo-44 / todo-179) -------------------------
    def _ensure_proc_context(self):
        """The per-compile procedure-macro context (shared by modules).

        Built on first use, after ``source_path`` / ``_IMPORT_ROOTS_BASE``
        are set: the roots drive the definition scan (global-unique-name
        visibility, todo-176) and the project base anchors the build cache.
        Contexts are process-cached per project base so bulk parser
        creation (macro-fragment parsing, tests) does not re-walk the
        filesystem; a real compile boundary clears them.
        """
        context = getattr(self, "_proc_context", None)
        if context is not None:
            return context
        import os as _os

        from ..macros.proc import ProcMacroContext
        from ..macros.proc.expand import shared_context
        from .defs import _entry_project_root, _module_roots

        base = _entry_project_root(getattr(self, "source_path", None))
        if base is None:
            from ..home import default_import_root

            base = default_import_root()
        key = _os.path.normcase(_os.path.abspath(str(base)))

        def factory():
            return ProcMacroContext(
                base,
                scan_dirs=[root.directory for root in _module_roots(base)],
            )

        context = shared_context(key, factory)
        self._proc_context = context
        return context

    def _ensure_macros_expanded(self) -> None:
        """Run the token-level macro desugar exactly once, before parsing."""
        if self._macros_expanded:
            return
        self._macros_expanded = True
        source_path = getattr(self, "source_path", None)
        context = self._ensure_proc_context()
        original = self.tokens
        # The entry file's own bodies are always expanded; only imported
        # module files defer macro-bearing bodies.
        if self._defer_macro_bodies and not self._is_entry_source:
            # todo-planB: capture ordinary function bodies that call macros
            # and run the expansion on a copy with those bodies hollowed out
            # (braces kept).  Their macros therefore never build unless the
            # function is later found reachable (``_materialize_reachable``).
            self._file_macro_defs = _collect_file_macro_defs(original, source_path)
            reduced, self._deferred_spans = _reduce_macro_bodies(original)
            if self._deferred_spans:
                self.tokens = reduced
        context.registry.prepare_file(self.tokens, source_path, prelude=self._is_entry_source)
        jobs = int(getattr(self, "_macro_jobs", 1) or 1)
        if jobs > 1 and self.tokens:
            # Build the file's called macros in parallel first; expansion
            # then reuses the cached exes (the sequential fixpoint keeps
            # its semantics).
            context.registry.preload(self.tokens, source_path, jobs)
        self.tokens, self.macro_errors = expand_macros(
            self.tokens,
            self._macro_next_context,
            self.macro_records,
            proc_context=context,
            source_path=source_path,
        )
        self.macro_warnings = list(context.warnings)

    # -- macro hygiene (todo-44) -------------------------------------------
    @staticmethod
    def macro_mangle(context: int, name: str) -> str:
        """The parse-time name an expansion-synthesized identifier gets.

        ``let x`` written inside expansion #3 becomes ``_m3_x``: the SA
        scopes and the backend C/LLVM symbols only ever see mangled
        names, so nothing outside the expansion can capture them (and
        they capture nothing outside).  ``_m`` + digits + ``_`` is not a
        valid CWind identifier (it cannot be typed), guaranteeing no
        collision with user source.
        """
        return f"_m{context}_{name}"

    @staticmethod
    def macro_unmangle(name: str) -> Optional[tuple[int, str]]:
        """Inverse of :meth:`macro_mangle`: ``(context, original)`` when
        *name* is an expansion-bound identifier, else ``None``."""
        if not name.startswith("_m"):
            return None
        rest = name[2:]
        sep = rest.find("_")
        if sep <= 0:
            return None
        digits = rest[:sep]
        if not digits.isdigit():
            return None
        return int(digits), rest[sep + 1:]

    def _ident_value(self, tok: Token) -> str:
        """The effective name of an identifier token.

        Tokens synthesized by macro expansion (``context is not None``)
        rename their identifiers to the mangled form *here*, the single
        point where every parser path reads identifier text.  All other
        tokens keep their name.  ``self``/``Self`` never rename: they are
        keyword-position names (receivers, impl owners), not user
        bindings, and method binding machinery compares them literally.

        Non-identifier tokens pass through unchanged so callers can use
        this value generically.
        """
        value = str(tok.value)
        if (
            tok.context is not None
            and tok.kind == TokenKind.IDENTIFIER
            and value not in ("self", "Self")
        ):
            return self.macro_mangle(tok.context, value)
        return value

    # -- token helpers -----------------------------------------------------
    def _peek(self, offset: int = 0) -> Optional[Token]:
        if offset < len(self._pending):
            return self._pending[offset]
        idx = self.pos + offset - len(self._pending)
        if 0 <= idx < len(self.tokens):
            return self.tokens[idx]
        return None

    def _advance(self) -> Token:
        if self._pending:
            return self._pending.popleft()
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def _snapshot(self) -> tuple[int, list[Token]]:
        """Save the token cursor so a speculative parse can be rolled back."""
        return self.pos, list(self._pending)

    def _restore(self, snap: tuple[int, list[Token]]) -> None:
        """Restore a cursor saved by :meth:`_snapshot`."""
        self.pos, pending = snap
        self._pending = deque(pending)

    def _at(self, kind: TokenKind, value: object = None) -> bool:
        tok = self._peek()
        return (
            tok is not None
            and tok.kind == kind
            and (value is None or tok.value == value)
        )

    def _match(self, kind: TokenKind, value: object = None) -> Optional[Token]:
        if self._at(kind, value):
            return self._advance()
        return None

    def _expect(self, kind: TokenKind, value: object = None, what: str = "") -> Token:
        tok = self._peek()
        if not self._at(kind, value):
            desc = what or (repr(kind.value) if value is None else f"{kind.value} {value!r}")
            prev = self.tokens[self.pos - 1] if self.pos > 0 else None
            if (
                kind == TokenKind.SEMICOLON
                and tok is not None
                and prev is not None
                and tok.line > prev.line
            ):
                # Missing `;` at the end of the previous line: point there
                # instead of at the first token of the next line.
                raise ParseError(
                    f"expected {desc}",
                    prev.end_line,
                    prev.end_column,
                    end_line=prev.end_line,
                    end_column=prev.end_column,
                )
            self._error(f"expected {desc}", tok)
        return self._advance()

    def _expect_gt(self, what: str = "'>'") -> Token:
        """Expect ``>``, transparently splitting a ``>>`` (SHR) token."""
        tok = self._peek()
        if tok is None or tok.kind not in (TokenKind.GT, TokenKind.SHR):
            self._error(f"expected {what}", tok)
        tok = self._advance()
        if tok.kind == TokenKind.SHR:
            # `>>` closes this generic and one more; re-queue the second `>`.
            self._pending.append(Token(
                TokenKind.GT,
                ">",
                tok.line,
                tok.column + 1,
                tok.end_line,
                tok.end_column,
                ">",
            ))
        return tok

    def _error(self, message: str, token: Optional[Token] = None) -> NoReturn:
        tok = token if token is not None else self._peek()
        if tok is None:
            last = self.tokens[-1] if self.tokens else None
            line = last.end_line if last is not None else 1
            column = last.end_column if last is not None else 1
            end_line, end_column = line, column
        else:
            line, column = tok.line, tok.column
            end_line, end_column = tok.end_line, tok.end_column
        raise ParseError(message, line, column, end_line=end_line, end_column=end_column)

    def _synchronize_statement(self) -> None:
        """Panic-mode recovery inside a block: skip to the next statement
        boundary (``;`` is consumed, ``}`` and statement starters are not)."""
        while True:
            tok = self._peek()
            if tok is None:
                return
            if tok.kind == TokenKind.SEMICOLON:
                self._advance()
                return
            if tok.kind == TokenKind.RBRACE or tok.kind in _STMT_START:
                return
            self._advance()

    def _synchronize_top_level(self) -> None:
        """Panic-mode recovery at the top level: skip to the next declaration
        starter or EOF."""
        while True:
            tok = self._peek()
            if tok is None:
                return
            if tok.kind in _TOP_LEVEL_START:
                return
            self._advance()

    def _skip_to_entry_boundary(self, *, consume_close: bool = False) -> None:
        """After an error inside a ``{ ... }`` literal, consume tokens up to
        the next entry separator (`,`) or the matching ``}``.

        A trailing `,` is consumed so the literal loop can continue with the
        next entry; a `}` is normally left for ``_expect`` to consume, unless
        ``consume_close`` is set (the closing brace already failed to match,
        so it is swallowed here to let the enclosing statement finish).
        Nested braces are tracked so a ``}`` inside a nested literal is not
        mistaken for this literal's closing brace.
        """
        depth = 0
        while True:
            tok = self._peek()
            if tok is None:
                return
            if tok.kind == TokenKind.LBRACE:
                depth += 1
            elif tok.kind == TokenKind.RBRACE:
                if depth == 0:
                    if consume_close:
                        self._advance()
                    return
                depth -= 1
            elif tok.kind == TokenKind.COMMA and depth == 0:
                self._advance()
                return
            self._advance()


# -- todo-planB: lazy body reduction helpers ---------------------------------
#
# A module file's declarations must be macro-expanded eagerly (the ordinary
# parser never sees macro syntax), but an *unreachable* function body's macros
# need not be built.  The body is captured as raw tokens and hollowed out of
# the token stream before expansion; ``_collect_file_macro_defs`` records the
# file's own ``macro_rules!`` definitions so the body can be re-expanded in
# isolation once the function turns out to be reachable.

_MACRO_OPEN = (TokenKind.LPAREN, TokenKind.LBRACKET, TokenKind.LBRACE)


def _collect_file_macro_defs(tokens: list[Token], source_path) -> dict:
    """The file's ``macro_rules!`` definitions, for deferred re-expansion."""
    if not tokens:
        return {}
    try:
        from ..macros.expansion import _collect_definitions
    except Exception:  # pragma: no cover - import cycle safety
        return {}
    defs: dict = {}
    errors: list = []
    try:
        _collect_definitions(
            list(tokens), defs, None, errors, source_path
        )
    except Exception:  # pragma: no cover - never let deferral break a parse
        return {}
    return defs


def _body_has_macro_call(tokens: list[Token]) -> bool:
    """True when *tokens* contain a ``name!(...)`` / ``name![...]`` call."""
    total = len(tokens)
    for idx, tok in enumerate(tokens):
        if tok.kind != TokenKind.IDENTIFIER or str(tok.value) == "macro_rules":
            continue
        if idx + 1 >= total or tokens[idx + 1].kind != TokenKind.NOT:
            continue
        if idx + 2 < total and tokens[idx + 2].kind in _MACRO_OPEN:
            return True
    return False


def _match_brace(tokens: list[Token], open_idx: int) -> int:
    """Index of the ``}`` matching ``tokens[open_idx]`` (a ``{``), else -1."""
    depth = 0
    for idx in range(open_idx, len(tokens)):
        kind = tokens[idx].kind
        if kind == TokenKind.LBRACE:
            depth += 1
        elif kind == TokenKind.RBRACE:
            depth -= 1
            if depth == 0:
                return idx
    return -1


def _fn_body_open(tokens: list[Token], fn_idx: int) -> int:
    """Index of a declaration's body ``{`` after ``fn`` at *fn_idx*.

    Returns -1 for a body-less declaration (``fn ... ;`` in an ``extern``
    block) or when the shape is not a plain signature.
    """
    idx = fn_idx + 1
    if idx >= len(tokens) or tokens[idx].kind != TokenKind.IDENTIFIER:
        return -1  # not a named declaration (``fn(`` pointer type)
    idx += 1
    # Generic parameter list.
    if idx < len(tokens) and tokens[idx].kind == TokenKind.LT:
        depth = 0
        while idx < len(tokens):
            kind = tokens[idx].kind
            if kind == TokenKind.LT:
                depth += 1
            elif kind == TokenKind.GT:
                depth -= 1
                if depth == 0:
                    idx += 1
                    break
            elif kind == TokenKind.SHR:
                depth -= 2
                if depth <= 0:
                    idx += 1
                    break
            elif kind == TokenKind.SEMICOLON or kind == TokenKind.LBRACE:
                return -1
            idx += 1
    if idx >= len(tokens) or tokens[idx].kind != TokenKind.LPAREN:
        return -1
    # Parameter list.
    depth = 0
    while idx < len(tokens):
        kind = tokens[idx].kind
        if kind == TokenKind.LPAREN:
            depth += 1
        elif kind == TokenKind.RPAREN:
            depth -= 1
            if depth == 0:
                idx += 1
                break
        idx += 1
    # Optional ``-> Type`` / ``, after :: hook`` then the body.
    depth = 0
    while idx < len(tokens):
        kind = tokens[idx].kind
        if kind in (TokenKind.LPAREN, TokenKind.LBRACKET, TokenKind.LT):
            depth += 1
        elif kind in (TokenKind.RPAREN, TokenKind.RBRACKET):
            depth -= 1
        elif kind == TokenKind.GT:
            depth -= 1
        elif depth <= 0 and kind == TokenKind.LBRACE:
            return idx
        elif depth <= 0 and kind == TokenKind.SEMICOLON:
            return -1
        idx += 1
    return -1


def _reduce_macro_bodies(
    tokens: list[Token],
) -> tuple[list[Token], dict[tuple[int, int], tuple[list[Token], frozenset[str]]]]:
    """Hollow out top-level function bodies that contain macro calls.

    Returns the reduced token list (bodies replaced by ``{}`` so positions
    survive) and a map from the body's opening-brace position to
    ``(raw inner tokens, identifier set)``.  Only *top-level* functions are
    reduced: their declarations are unambiguous and their bodies are the
    ones safety/codegen depends on being real.  ``#[proc_macro]`` definition
    bodies stay intact (their tokens are needed to build the macro).
    """
    spans: dict[tuple[int, int], tuple[list[Token], frozenset[str]]] = {}
    idx = 0
    total = len(tokens)
    while idx < total:
        tok = tokens[idx]
        if tok.kind != TokenKind.FN:
            idx += 1
            continue
        open_idx = _fn_body_open(tokens, idx)
        if open_idx < 0:
            idx += 1
            continue
        close_idx = _match_brace(tokens, open_idx)
        if close_idx < 0:
            idx += 1
            continue
        # Skip procedure-macro definition bodies: the macro build reads the
        # definition's original tokens, so it must not be hollowed out.
        if _has_proc_macro_attribute(tokens, idx):
            idx = close_idx + 1
            continue
        inner = tokens[open_idx + 1:close_idx]
        if not _body_has_macro_call(inner):
            idx = close_idx + 1
            continue
        idents = frozenset(
            str(t.value) for t in inner if t.kind == TokenKind.IDENTIFIER
        )
        spans[(tokens[open_idx].line, tokens[open_idx].column)] = (inner, idents)
        # Keep the braces themselves so the parsed block keeps its position.
        idx = close_idx + 1
    if not spans:
        return tokens, spans
    reduced: list[Token] = []
    idx = 0
    while idx < total:
        tok = tokens[idx]
        if tok.kind == TokenKind.FN:
            open_idx = _fn_body_open(tokens, idx)
            if open_idx >= 0:
                key = (tokens[open_idx].line, tokens[open_idx].column)
                if key in spans:
                    close_idx = _match_brace(tokens, open_idx)
                    reduced.extend(tokens[idx:open_idx + 1])
                    reduced.append(tokens[close_idx])
                    idx = close_idx + 1
                    continue
        reduced.append(tok)
        idx += 1
    return reduced, spans


def _has_proc_macro_attribute(tokens: list[Token], fn_idx: int) -> bool:
    """Whether the ``fn`` at *fn_idx* is preceded by a ``#[proc_macro*]``."""
    idx = fn_idx - 1
    while idx >= 0:
        tok = tokens[idx]
        if tok.kind == TokenKind.RBRACKET:
            # Walk back to the matching '['.
            depth = 0
            start = idx
            while start >= 0:
                kind = tokens[start].kind
                if kind == TokenKind.RBRACKET:
                    depth += 1
                elif kind == TokenKind.LBRACKET:
                    depth -= 1
                    if depth == 0:
                        break
                start -= 1
            if start >= 1 and tokens[start - 1].kind == TokenKind.HASH:
                name = tokens[start + 1] if start + 1 <= idx else None
                if (
                    name is not None
                    and name.kind == TokenKind.IDENTIFIER
                    and str(name.value).startswith("proc_macro")
                ):
                    return True
                idx = start - 2
                continue
            return False
        if tok.kind in (TokenKind.PUB,):
            idx -= 1
            continue
        return False
    return False
