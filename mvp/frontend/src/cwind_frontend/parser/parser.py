"""CWind recursive-descent parser (spec: frontend/Grammar.md).

Consumes the token stream produced by :mod:`cwind_frontend.lexer` and builds
the AST defined in :mod:`cwind_frontend.ast_components.ast`.

Design notes
------------
* Contextual tokens are resolved here: ``{`` (block / map literal after ``=`` /
  struct construction), ``<``/``>`` (generics vs. comparison vs. shift), ``:``
  (type annotation / for-in sugar / map entry), ``in`` (for-in only).  The
  lexer deliberately stays context-free.
* Nested generic closers (``Vector<Vector<Int>>``) arrive as a single ``>>``
  (``SHR``) token; the parser splits it by re-queuing a synthetic ``>``.
* Grammar-level errors (missing ``;``, unbalanced delimiters, declarations
  without types, ...) raise :class:`ParseError`, which carries 1-based
  positions and is rendered with ariadne_py just like :class:`LexError`.
* The parser is error-recovering: it records every :class:`ParseError` and
  synchronizes at statement/declaration boundaries so one run surfaces many
  errors.  Use :func:`parse_with_errors` to get them all; :func:`parse`
  keeps the fail-fast behavior (raises the first error).
"""

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

from .items import ParserItems
from .attrs import ParserAttrs
from .decls import ParserDecls
from .types import ParserTypes
from .stmts import ParserStmts
from .exprs import ParserExprs
from .core import ParserCore


__all__ = [
    "ParseError",
    "ParseResult",
    "Parser",
    "parse",
    "parse_file",
    "parse_source",
    "parse_with_errors",
]


class Parser(
    ParserItems,
    ParserAttrs,
    ParserDecls,
    ParserTypes,
    ParserStmts,
    ParserExprs,
    ParserCore,
):
    """A recursive-descent parser over a token list."""


import sys as _sys
for _mod_name in ('defs', 'core', 'items', 'attrs', 'decls',
                   'types', 'stmts', 'exprs'):
    _sys.modules[__package__ + '.' + _mod_name].Parser = Parser
del _sys, _mod_name


def parse(tokens: list[Token]) -> Program:
    """Parse a token list into a :class:`Program`; raise the first ParseError."""
    result = parse_with_errors(tokens)
    if result.errors:
        raise result.errors[0]
    return result.program


def parse_with_errors(
    tokens: list[Token],
    source_path: Optional[str] = None,
    *,
    target_os: Optional[str] = None,
    target_arch: Optional[str] = None,
    target_vendor: Optional[str] = None,
    target_pointer_width: Optional[str] = None,
    package_lib: Optional[tuple[Sequence[str], str]] = None,
    flush_cache: Optional[bool] = None,
) -> ParseResult:
    """Parse a token list, collecting every :class:`ParseError`.

    The parser recovers by skipping to statement/declaration boundaries, so a
    single run reports as many independent errors as possible.

    ``source_path`` (todo-76) anchors the entry file: it locates the project
    root (the nearest ancestor owning ``libs/``) and enables the implicit
    ``std::prelude::*`` import.  Token lists without a source location (stdin,
    in-memory test sources) keep the legacy no-prelude behavior.

    ``target_os`` (todo-86/93) pins the compile-time configuration for
    ``#[cfg]`` predicates instead of auto-detecting the host; it must be one
    of :data:`~cwind_frontend.cfg.OS_NAMES`.  ``target_arch`` /
    ``target_vendor`` / ``target_pointer_width`` (todo-103/106) do the same
    for their keys; ``None`` keeps host auto-detection.

    ``package_lib`` (todo-97) is ``(alias path, absolute file)`` of the
    project's own library facade; only meaningful together with
    ``source_path``.  Its public API is wildcard-imported into the entry
    program beneath user declarations.
    """
    if target_os is not None and target_os not in CFG_KEY_VALUES["target_os"]:
        raise ValueError(
            f"unknown target_os {target_os!r} "
            f"(expected one of: {', '.join(CFG_KEY_VALUES['target_os'])})"
        )
    if target_arch is not None and target_arch not in CFG_KEY_VALUES[
        "target_arch"
    ]:
        raise ValueError(
            f"unknown target_arch {target_arch!r} "
            f"(expected one of: {', '.join(CFG_KEY_VALUES['target_arch'])})"
        )
    if target_vendor is not None and target_vendor not in CFG_KEY_VALUES[
        "target_vendor"
    ]:
        raise ValueError(
            f"unknown target_vendor {target_vendor!r} "
            f"(expected one of: {', '.join(CFG_KEY_VALUES['target_vendor'])})"
        )
    if (
        target_pointer_width is not None
        and target_pointer_width
        not in CFG_KEY_VALUES["target_pointer_width"]
    ):
        raise ValueError(
            f"unknown target_pointer_width {target_pointer_width!r} "
            "(expected one of: "
            + ", ".join(CFG_KEY_VALUES["target_pointer_width"]) + ")"
        )
    parser = Parser(tokens)
    entry_path = getattr(parser, "source_path", None)
    if source_path is not None:
        parser.source_path = str(Path(source_path).resolve())
        entry_path = parser.source_path
    parser._is_entry_source = source_path is not None
    # todo-171: a real compile (project anchored) is one process-level
    # compile boundary — drop the per-process Program caches so a previous
    # SA run's in-place AST rewrites cannot leak into this run.  In-memory
    # sources (no prelude, no cache interaction) keep the default off, and
    # callers may pin the behavior explicitly via ``flush_cache``.
    parser._flush_caches = (
        (source_path is not None)
        if flush_cache is None
        else bool(flush_cache)
    )
    parser._IMPORT_ROOTS_BASE = _entry_project_root(entry_path)
    parser._cfg_target_os = target_os
    parser._cfg_target_arch = target_arch
    parser._cfg_target_vendor = target_vendor
    parser._cfg_pointer_width = target_pointer_width
    if package_lib is not None and source_path is not None:
        parts, lib_file = package_lib
        parser._package_lib = (list(parts), Path(lib_file))
    program = parser.parse_program()
    return ParseResult(
        program,
        list(parser.errors),
        list(parser._module_order),
    )


def parse_source(source: str, *, emit_comments: bool = False) -> Program:
    """Tokenize and parse a CWind source string."""
    return parse(tokenize(source, emit_comments=emit_comments))


def parse_file(
    path: Union[str, os.PathLike[str]],
    *,
    emit_comments: bool = False,
) -> Program:
    """Tokenize and parse a CWind source file."""
    return parse(tokenize_file(path, emit_comments=emit_comments))

