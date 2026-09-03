"""Parser mixin: lexical token-stream primitives, error raising and recovery."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
from collections import deque
from dataclasses import dataclass, field, fields as _dc_fields
from typing import NoReturn, Optional, Sequence, Union, cast

from ..ast_components.ast import (
    Arg,
    AssocType,
    AssocTypeDecl,
    Assign,
    Attribute,
    BindPattern,
    BinOp,
    Block,
    BoolLit,
    BreakStmt,
    Call,
    CastExpr,
    ConstDecl,
    ContinueStmt,
    Distribution,
    ElifBranch,
    EnumPattern,
    EnumDecl,
    ErrorStmt,
    ExprStmt,
    ExternBlock,
    ExternStatic,
    ExtraDecl,
    Field,
    FloatLit,
    FnDecl,
    ForStmt,
    GroupApply,
    GroupDecl,
    IfStmt,
    IfLetBranch,
    IfLetStmt,
    ImplDecl,
    Index,
    IntLit,
    LetStmt,
    LitPattern,
    MapEntry,
    MapLit,
    MatchArm,
    MatchStmt,
    ModDecl,
    Name,
    Node,
    Param,
    Program,
    ReturnStmt,
    Slice,
    StrLit,
    StructConstruct,
    Closure,
    StructDecl,
    StructPattern,
    StructPatternField,
    TraitDecl,
    TuplePattern,
    Type,
    TypeDecl,
    TypeParam,
    TupleLit,
    UnaryOp,
    UseDecl,
    Variant,
    VectorLit,
    WhileLetStmt,
    LetChainSeg,
    WhileStmt,
    WildcardPattern,
)
from ..ast_components.errors import FrontendError
from ..ast_components.token import Token, TokenKind
from ..cfg import (
    CFG_COMBINATORS,
    CFG_FLAGS,
    CFG_KEYS,
    CFG_KEY_VALUES,
    CfgContext,
    CfgPredicate,
    evaluate_cfg,
)
from ..lexer import tokenize, tokenize_file
from ..breeze import MANIFEST_NAME, ManifestError, load_manifest
from ..macros import expand_macros

from ..ast_components.ast import _type_name_for_type

from .defs import (
    ParseError,
    ParseResult,
    _ASSIGN_OPS,
    _RELATIONAL_OPS,
    _EQUALITY_OPS,
    _ADDITIVE_OPS,
    _MULTIPLICATIVE_OPS,
    _SHIFT_OPS,
    _UNARY_OPS,
    _STMT_START,
    _TOP_LEVEL_START,
    _IMPORT_ROOTS,
    _SOURCE_SUFFIXES,
    ModuleTrieNode,
    _library_fingerprint,
    _MODULE_TREE_CACHE,
    _module_parts,
    ModuleRoot,
    _module_roots,
    _scan_mod_declarations,
    _scan_reexports,
    _find_mod_entry,
    _resolve_declared_entry,
    _build_library_trie,
    ModuleTree,
    _library_tree,
    _NO_PRELUDE_SENTINEL,
    _IMPL_REGISTRY_CACHE,
    _IMPL_REGISTRY_BOOT_CACHE,
    _impl_registry_for,
    _NAME_BINDING_NODES,
    _referenced_names,
    _entry_project_root,
    _localize_qualified_refs,
    _module_mangle_suffix,
    _mangled_item_name,
    _declared_name_field,
    _set_declared_name,
    _SCOPE_PUSH_NODES,
    _rewrite_module_refs,
)


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
        self.macro_errors: list[FrontendError] = []
        self.tokens, self.macro_errors = expand_macros(
            self.tokens, self._macro_next_context
        )
        self.pos = 0
        self.errors: list[ParseError] = []
        self._pending: deque[Token] = deque()  # synthetic tokens (from `>>` splits)
        self._for_iterable_expr = False
        # todo-165: true while parsing a while-let chain operand, where a
        # top-level ``&& let`` terminates the boolean expression.
        self._let_chain_ctx = False
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
        self._IMPORT_ROOTS_BASE: Path = Path.cwd()
        self._auto_prelude_result: object = _NO_PRELUDE_SENTINEL
        self._is_entry_source: bool = False
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
