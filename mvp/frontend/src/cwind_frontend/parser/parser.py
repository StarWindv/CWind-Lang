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

__all__ = [
    "ParseError",
    "ParseResult",
    "Parser",
    "parse",
    "parse_file",
    "parse_source",
    "parse_with_errors",
]


class ParseError(FrontendError):
    """Raised for grammar-level problems (as opposed to :class:`LexError`)."""


@dataclass
class ParseResult:
    """Program produced by the parser plus any recovered grammar errors."""

    program: Program
    errors: list[ParseError]
    modules: list[str] = field(default_factory=list)


_ASSIGN_OPS: frozenset[TokenKind] = frozenset({
    TokenKind.ASSIGN,
    TokenKind.PLUS_ASSIGN,
    TokenKind.MINUS_ASSIGN,
    TokenKind.STAR_ASSIGN,
    TokenKind.SLASH_ASSIGN,
})

_RELATIONAL_OPS: frozenset[TokenKind] = frozenset({
    TokenKind.LT,
    TokenKind.GT,
    TokenKind.LE,
    TokenKind.GE,
})

_EQUALITY_OPS: frozenset[TokenKind] = frozenset({
    TokenKind.EQ,
    TokenKind.ADDR_EQ,
    TokenKind.NE,
    TokenKind.NOT_LT,  # !<  sugar for >=
    TokenKind.NOT_GT,  # !>  sugar for <=
})

_ADDITIVE_OPS: frozenset[TokenKind] = frozenset({TokenKind.PLUS, TokenKind.MINUS})
_MULTIPLICATIVE_OPS: frozenset[TokenKind] = frozenset({
    TokenKind.STAR,
    TokenKind.SLASH,
    TokenKind.PERCENT,
})
_SHIFT_OPS: frozenset[TokenKind] = frozenset({TokenKind.SHL, TokenKind.SHR})
_UNARY_OPS: frozenset[TokenKind] = frozenset({
    TokenKind.NOT,
    TokenKind.MINUS,
    TokenKind.PLUS,
    TokenKind.AMP,
    TokenKind.STAR,
})

# Token kinds a new statement can start with (used by panic-mode recovery).
_STMT_START: frozenset[TokenKind] = frozenset({
    TokenKind.LET,
    TokenKind.RETURN,
    TokenKind.BREAK,
    TokenKind.CONTINUE,
    TokenKind.IF,
    TokenKind.MATCH,
    TokenKind.WHILE,
    TokenKind.FOR,
    TokenKind.LBRACE,
})

# Token kinds a new top-level declaration can start with.
_TOP_LEVEL_START: frozenset[TokenKind] = frozenset({
    TokenKind.PUB,
    TokenKind.CONST,
    TokenKind.TYPE,
    TokenKind.TYPEDEF,
    TokenKind.STRUCT,
    TokenKind.ENUM,
    TokenKind.TRAIT,
    TokenKind.IMPL,
    TokenKind.EXTRA,
    TokenKind.GROUP,
    TokenKind.FN,
    TokenKind.EXTERN,
    TokenKind.HASH,  # #[...] attributes
})

# todo-69/70 v0: user imports resolve under these project-local roots.
# ``libs`` is the current source-tree convention; the source directory is
# kept as a fallback for single-file projects.
_IMPORT_ROOTS = (Path("libs"), Path("."))
# todo-158: CWind source suffixes.  ``.wind``/``.wd`` are the classic pair;
# ``.cwind``/``.cwd`` are the C-bound spelling, legal everywhere a source
# file is accepted (module files, imports, fingerprints).
_SOURCE_SUFFIXES = (".wind", ".wd", ".cwind", ".cwd")


@dataclass
class ModuleTrieNode:
    """Prefix-tree node for module path segments.

    A node is a module when ``entry`` points at its source file; descendants
    remain reachable even when an intermediate segment also has a file.

    todo-107: ``pub`` records how the segment was declared in its parent's
    mod.wind — a ``pub mod`` re-export is addressable from anywhere, while a
    private ``mod`` is only addressable from inside its own subtree.
    """

    children: dict[str, "ModuleTrieNode"] = field(default_factory=dict)
    entry: Optional[Path] = None
    pub: bool = True

    def find_longest(
        self, parts: list[str]
    ) -> tuple[list[str], Optional[Path]]:
        node: ModuleTrieNode = self
        best: Optional[Path] = None
        best_depth = 0
        for depth, part in enumerate(parts, 1):
            nxt = node.children.get(part)
            if nxt is None:
                break
            node = nxt
            if node.entry is not None:
                best = node.entry
                best_depth = depth
        return parts[best_depth:], best


def _library_fingerprint(root: Path) -> str:
    """Fast directory fingerprint used to reuse an unchanged module trie.

    Only metadata is read here.  Source text is parsed once per module and
    cached by canonical path; changing a file's contents updates ``mtime_ns``
    and therefore invalidates both layers together.
    """
    pieces: list[str] = []
    if root.exists():
        for path in sorted(root.rglob("*"), key=lambda p: str(p).lower()):
            try:
                stat = path.stat()
                rel = path.relative_to(root).as_posix()
            except OSError:
                continue
            kind = "d" if path.is_dir() else "f"
            pieces.append(f"{kind}:{rel}:{stat.st_size}:{stat.st_mtime_ns}")
    return hashlib.sha256("\n".join(pieces).encode("utf-8")).hexdigest()


_MODULE_TREE_CACHE: dict[str, tuple[str, ModuleTree]] = {}


def _module_parts(
    rel: Path, root_file: Optional[Path] = None
) -> Optional[list[str]]:
    """Import path of one library file relative to the module root.

    todo-70: a ``mod`` file addresses its *directory*, the old-Rust
    ``dir + mod.rs`` layout — ``libs/foo.wind`` and ``libs/foo/mod.wind``
    both resolve ``use foo;``.  Deeper files keep chaining directory
    segments as before.  A bare ``<root>/mod.wind`` has no importable
    name and registers nothing.

    Rust-before-2018 layout (todo-158): a Breeze source root has no
    ``mod.wind`` — its ``lib.wd`` (the manifest ``[entry].module``) is the
    crate root module, so that file maps to the empty path too.
    """
    if root_file is not None and rel == Path(root_file.name):
        return None
    if rel.stem.lower() == "mod":
        parts = list(rel.parts[:-1])
    else:
        parts = [*rel.parts[:-1], rel.stem]
    return parts or None


@dataclass(frozen=True)
class ModuleRoot:
    """One import root directory plus its root-module file.

    ``kind`` distinguishes the std convention (``libs/``, root module
    ``mod.wind``) from a Breeze package source tree (Rust-before-2018
    layout: the manifest's ``[entry].module`` file — ``lib.wd`` — is the
    crate root; there is no ``mod.wind`` at the source root).
    """

    directory: Path
    entry: Optional[Path]
    kind: str  # "std" | "crate"


def _module_roots(base: Path) -> list[ModuleRoot]:
    """Directories whose sources feed the module trie.

    ``<base>/libs`` is the std convention.  todo-71/97: a project with a
    ``Breeze.toml`` also exposes its ``[entry].source`` tree — the
    Rust-before-2018 crate, whose root module is the manifest's
    ``[entry].module`` file (``lib.wd``), not a ``mod.wind``.  A manifest
    that fails validation is ignored here — the CLI reports manifest
    problems itself.
    """
    roots: list[ModuleRoot] = []
    libs = base / "libs"
    if libs.is_dir():
        roots.append(
            ModuleRoot(libs.resolve(), _find_mod_entry(libs), "std")
        )
    manifest_path = base / MANIFEST_NAME
    if manifest_path.is_file():
        try:
            manifest = load_manifest(manifest_path)
        except ManifestError:
            return roots
        source = manifest.source_path()
        if source.is_dir():
            entry_file = source / Path(
                *PurePosixPath(manifest.entry.module).parts
            )
            roots.append(
                ModuleRoot(
                    source.resolve(),
                    entry_file if entry_file.is_file() else None,
                    "crate",
                )
            )
    return roots


def _scan_mod_declarations(
    path: Path,
) -> Optional[list[tuple[str, bool, int, int]]]:
    """todo-158: the ``[pub] mod <name>;`` declarations of one mod.wind.

    A lightweight token scan (no full parse — this runs inside trie
    construction, before any parser exists) collects ``(name, pub, line,
    column)`` tuples.  ``None`` means the file is not a mod.wind module
    entry; an empty list means "declares nothing" (its directory stays
    empty and no submodule is addressable through it).
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return None
    try:
        tokens = tokenize(text)
    except Exception:
        # A lexically broken mod.wind must not crash the tree build; the
        # error surfaces when the file is actually parsed as a module.
        return []
    decls: list[tuple[str, bool, int, int]] = []
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok.kind == TokenKind.PUB:
            j = i + 1
            # Skip one ``pub(<qual>)`` group: a restricted-visibility pub
            # still declares a module.
            if (
                j < n
                and tokens[j].kind == TokenKind.LPAREN
            ):
                depth = 0
                while j < n:
                    if tokens[j].kind == TokenKind.LPAREN:
                        depth += 1
                    elif tokens[j].kind == TokenKind.RPAREN:
                        depth -= 1
                        if depth == 0:
                            break
                    j += 1
                j += 1
            if (
                j < n
                and tokens[j].kind == TokenKind.MOD
                and j + 1 < n
                and tokens[j + 1].kind == TokenKind.IDENTIFIER
            ):
                name_tok = tokens[j + 1]
                decls.append(
                    (str(name_tok.value), True, name_tok.line, name_tok.column)
                )
                i = j + 2
                continue
            i += 1
            continue
        if tok.kind == TokenKind.MOD:
            if (
                i + 1 < n
                and tokens[i + 1].kind == TokenKind.IDENTIFIER
            ):
                name_tok = tokens[i + 1]
                decls.append(
                    (str(name_tok.value), False, name_tok.line, name_tok.column)
                )
                i += 2
                continue
        i += 1
    return decls


def _find_mod_entry(directory: Path) -> Optional[Path]:
    """The module entry file of *directory* (``mod.wind`` / ``mod.wd``)."""
    for suffix in _SOURCE_SUFFIXES:
        candidate = directory / f"mod{suffix}"
        if candidate.is_file():
            return candidate
    return None


def _resolve_declared_entry(directory: Path, name: str) -> Optional[Path]:
    """The source file a ``mod <name>;`` declaration binds to.

    Rust's 2018 layout: ``name.wind`` (file module) or ``name/mod.wind``
    (directory module); both suffixes are accepted.  Having *both* a file
    and a directory module for one name is the todo-70 ambiguity error.
    ``None`` = dangling declaration (the ``use`` fails to resolve).
    """
    file_entry: Optional[Path] = None
    dir_entry: Optional[Path] = None
    for suffix in _SOURCE_SUFFIXES:
        candidate = directory / f"{name}{suffix}"
        if candidate.is_file():
            file_entry = candidate
            break
    dir_entry = _find_mod_entry(directory / name)
    if file_entry is not None and dir_entry is not None:
        raise ValueError(
            f"ambiguous module file for '{name}' in '{directory}'"
        )
    return file_entry if file_entry is not None else dir_entry


def _build_library_trie(roots: list[ModuleRoot]) -> "ModuleTree":
    """todo-158: the module trees, driven by module-file declarations.

    Rust semantics: a submodule exists only where its parent's module file
    declares it.  A ``mod.wind``-style file acts as the module entry of its
    directory; every other source file is a *file module* whose
    addressability is decided by its parent's declaration list:

    - ``mod <name>;`` / ``pub [vis] mod <name>;`` in a directory module
      registers ``<dir>::<name>`` — resolved to ``name.<suffix>`` (file
      module) or ``name/mod.<suffix>`` (directory module; having both is
      the todo-70 ambiguity error);
    - a file present on disk but never declared is unreachable —
      ``use std::undeclared;`` is an error (the old behavior silently
      succeeded through any sibling file);
    - the std root (``libs/mod.wind``) is the prelude module: ``use std::x;``
      / ``use x;`` and the implicit ``std::*`` import ride its declaration
      list;
    - the crate root (a Breeze package, Rust-before-2018 layout) has no
      ``mod.wind`` — its ``lib.wd`` (manifest ``[entry].module``) is the
      root module declaring the crate's submodules.

    Two trees are produced: ``std`` (the libs root) and ``crate`` (the
    package source root).  Bare paths try std first, then crate; ``std::``
    and ``crate::`` heads pick their tree explicitly.
    """
    trees = ModuleTree(std=ModuleTrieNode(), crate=ModuleTrieNode())

    def expand(node: ModuleTrieNode, mod_file: Path) -> None:
        decls = _scan_mod_declarations(mod_file)
        if not decls:
            return
        directory = mod_file.parent
        for name, pub, _line, _col in decls:
            entry = _resolve_declared_entry(directory, name)
            if entry is None:
                # Dangling declaration: the name stays unregistered so
                # ``use`` reports "cannot find module" precisely.
                continue
            entry = entry.resolve()
            child = node.children.setdefault(name, ModuleTrieNode())
            if child.entry is not None and child.entry != entry:
                raise ValueError(
                    f"ambiguous module file for '{name}' in '{directory}'"
                )
            child.entry = entry
            child.pub = pub
            if entry.stem.lower() == "mod":
                # Directory module: its own mod.wind declares the next
                # level.  File modules (name.wind) have no declaration
                # chain of their own — sibling files in the same directory
                # are governed by the directory's module file, already
                # expanded at this level.
                expand(child, entry)

    for root in roots:
        if not root.directory.exists() or root.entry is None:
            continue
        entry = root.entry.resolve()
        tree = trees.std if root.kind == "std" else trees.crate
        tree.entry = entry
        expand(tree, entry)
    return trees


@dataclass
class ModuleTree:
    """The std tree (libs/) and the crate tree (Breeze source root)."""

    std: ModuleTrieNode
    crate: ModuleTrieNode

    def resolve(
        self, parts: list[str]
    ) -> tuple[list[str], Optional[Path]]:
        """Longest-prefix resolution, crate tree first (shadowing std).

        When the crate tree exists but does not match, the miss stays a
        miss (crate items never hide in std); an empty crate tree falls
        through to std.
        """
        remaining, entry = self.crate.find_longest(parts)
        if entry is not None:
            return remaining, entry
        if self.crate.entry is not None or self.crate.children:
            # A live crate tree: misses stay misses.
            return remaining, None
        return self.std.find_longest(parts)


def _library_tree(base: Path) -> ModuleTree:
    """Return the module prefix tree; rebuild only after a hash change."""
    roots = _module_roots(base)
    key = str(base)
    fingerprint = hashlib.sha256(
        "\n".join(
            f"{root.directory}|{_library_fingerprint(root.directory)}"
            for root in roots
        ).encode("utf-8")
    ).hexdigest()
    cached = _MODULE_TREE_CACHE.get(key)
    if cached is not None and cached[0] == fingerprint:
        return cached[1]
    tree = _build_library_trie(roots)
    _MODULE_TREE_CACHE[key] = (fingerprint, tree)
    return tree


_NO_PRELUDE_SENTINEL = object()


# bug-61: per-import-root trait-impl registry, mirroring rustc's
# ``resolutions().trait_impls`` / ``trait_impls_of`` pair.  Rust registers
# *every* trait impl of a crate at def-collection time (rustc_resolve
# late.rs), independent of visibility or re-exports; method resolution then
# queries all impls of an imported trait (TyCtxt::for_each_relevant_impl).
# CWind's analogue: when a TraitDecl enters the entry's compile surface, the
# impl blocks living in *sibling std modules* must be pulled in as well --
# importing a trait == importing its implemented methods.  Keyed by trait
# name -> list of (module file, owner type name); the parsed Programs are
# kept so pulled impl blocks share node instances with normal imports (the
# ``_scope_flat`` guard keeps them from being renamed twice).
#
# The registry is built ONCE per import root + fingerprint, and only from
# top-level items (ImplDecl) of a plain parse — never through
# _select_module_items, whose pull loop re-enters this builder otherwise.
_IMPL_REGISTRY_CACHE: dict[
    str, tuple[str, dict[str, list[tuple[str, str]]], dict[str, Program]]
] = {}
# Shared module cache so registry pre-parses and real imports reuse Programs.
_IMPL_REGISTRY_BOOT_CACHE: dict[str, Program] = {}


def _impl_registry_for(
    base: Path,
    module_cache: Optional[dict[str, Program]] = None,
) -> tuple[dict[str, list[tuple[str, str]]], dict[str, Program]]:
    """Build (lazily, once per import root + library fingerprint) the index
    of every trait impl in the library tree.

    ``module_cache`` lets the caller share parsed Programs with its own
    import machinery — the entry parser passes ``self._module_cache`` so a
    file pre-parsed here is not re-parsed (and re-instantiated) by the
    normal import path: duplicate top-level node instances would otherwise
    land in the root program and SA would reject them as duplicate
    definitions.
    """
    key = str(Path(base).resolve())
    roots = _module_roots(Path(base))
    fp = hashlib.sha256(
        "\n".join(
            f"{root.directory}|{_library_fingerprint(root.directory)}"
            for root in roots
        ).encode("utf-8")
    ).hexdigest()
    if module_cache is None:
        module_cache = _IMPL_REGISTRY_BOOT_CACHE
    cached = _IMPL_REGISTRY_CACHE.get(key)
    if cached is not None and cached[0] == fp:
        # Still refresh the caller's cache with the parsed programs.
        for path_key, program in cached[2].items():
            module_cache.setdefault(path_key, program)
        return cached[1], cached[2]
    registry: dict[str, list[tuple[str, str]]] = {}
    programs: dict[str, Program] = {}
    for root in roots:
        for path in sorted(
            root.directory.rglob("*"), key=lambda p: str(p).lower()
        ):
            if not path.is_file() or path.suffix.lower() not in _SOURCE_SUFFIXES:
                continue
            # A file already parsed through the caller's import chain keeps
            # its Program (and node instances): re-parsing would duplicate
            # every top-level node and SA would reject the second copy as
            # a duplicate definition.
            file_key = str(path.resolve())
            existing = module_cache.get(file_key)
            if existing is not None:
                programs[file_key] = existing
                for item in existing.items:
                    if isinstance(item, ImplDecl):
                        registry.setdefault(item.trait.name, []).append(
                            (file_key, item.struct.name)
                        )
                continue
            try:
                text = path.read_text(encoding="utf-8-sig")
            except OSError:
                continue
            child = Parser(tokenize(text))
            child.source_path = file_key
            child._IMPORT_ROOTS_BASE = Path(base)
            child._module_cache = module_cache
            child._loading = []
            try:
                program = child.parse_program()
            except Exception:
                # A file that fails to parse standalone still reports its
                # errors through the normal import path; just skip it here.
                continue
            program._registry_home = file_key
            programs[file_key] = program
            for item in program.items:
                if isinstance(item, ImplDecl):
                    registry.setdefault(item.trait.name, []).append(
                        (file_key, item.struct.name)
                    )
    _IMPL_REGISTRY_CACHE[key] = (fp, registry, programs)
    return registry, programs


# Nodes whose ``name`` field declares a local binding rather than referencing
# a top-level item; excluded from dependency-closure scanning.
_NAME_BINDING_NODES = (
    Attribute,
    BindPattern,
    LetStmt,
    Param,
    Field,
    Variant,
    TypeParam,
    StructPatternField,
)


def _referenced_names(node: Node) -> set[str]:
    """Collect identifiers that may reference sibling top-level items.

    The scan is intentionally syntactic and over-approximate: every path
    segment, pattern path, type name and non-binding ``name`` field inside
    the subtree counts as a potential reference.  False positives only widen
    the compile-dependency surface of an import; they never change export
    semantics.
    """
    found: set[str] = set()
    stack: list[object] = [node]
    while stack:
        current = stack.pop()
        if not isinstance(current, Node):
            continue
        for f in _dc_fields(current):
            value = getattr(current, f.name)
            if f.name in ("parts", "path"):
                if isinstance(value, list):
                    found.update(v for v in value if isinstance(v, str))
            elif (
                f.name == "name"
                and isinstance(value, str)
                and not isinstance(current, _NAME_BINDING_NODES)
            ):
                found.add(value)
            elif f.name in ("group", "struct") and isinstance(value, str):
                found.add(value)
            if isinstance(value, Node):
                stack.append(value)
            elif isinstance(value, list):
                stack.extend(value)
    return found


def _entry_project_root(source_path: Optional[str]) -> Path:
    """Project root that owns the ``libs/`` tree for an entry file (todo-76).

    Walks upward from the entry file's directory until a directory owning a
    ``libs/`` folder is found; an entry placed *inside* ``<root>/libs``
    therefore anchors at ``<root>`` itself.  todo-71: a ``Breeze.toml``
    anchors the project too, so manifest-driven projects without their own
    ``libs/`` still resolve imports against the package root instead of the
    entry's subdirectory.  With neither, the entry's own directory wins.
    """
    start = (
        Path.cwd().resolve()
        if source_path is None
        else Path(source_path).resolve().parent
    )
    directory = start
    while True:
        if (directory / "libs").is_dir():
            return directory.resolve()
        if (directory / MANIFEST_NAME).is_file():
            return directory.resolve()
        parent = directory.parent
        if parent == directory:
            return start.resolve()
        directory = parent


def _localize_qualified_refs(
    nodes: list[Node],
    alias_items: dict[str, list[Node]],
    declaration_name,
) -> None:
    """Rewrite ``alias::item`` references inside flattened declarations.

    Imported bodies qualify calls through the *importing module's* own
    aliases (e.g. ``option.wind`` calls ``panic::panic`` via its private
    ``use std::panic;``).  After flattening, the referenced item lives in the
    root program under its bare name, so each qualified reference whose head
    matches a module-internal alias and whose tail names one of that alias's
    items is rewritten to that item's flattened (final) name.

    todo-79: items flattened by an earlier import surface may already carry
    their final (mangled) names, so every alias maps *both* spellings --
    the original source name and the current one -- to the same final name.
    """
    targets: dict[str, dict[str, str]] = {}
    for alias, items in alias_items.items():
        table: dict[str, str] = {}
        for item in items:
            final = declaration_name(item)
            if final is None:
                continue
            table[final] = final
            orig = getattr(item, "_scope_orig", None)
            if isinstance(orig, str):
                table[orig] = final
        if table:
            targets[alias] = table

    def rewrite(node: object) -> None:
        if isinstance(node, Name):
            if len(node.parts) >= 2:
                head_targets = targets.get(node.parts[0])
                if head_targets is not None:
                    final = head_targets.get(node.parts[-1])
                    if final is not None:
                        node.parts = [final]
                        return
        elif isinstance(node, EnumPattern):
            if node.path and node.path[0] in targets:
                final = targets[node.path[0]].get(node.path[-1])
                if final is not None:
                    node.path = [final]
                    return
        if isinstance(node, Node):
            for f in _dc_fields(node):
                value = getattr(node, f.name)
                if isinstance(value, Node):
                    rewrite(value)
                elif isinstance(value, list):
                    for element in value:
                        rewrite(element)

    for root_node in nodes:
        rewrite(root_node)


# ---------------------------------------------------------------------------
# todo-79: module scope table
#
# Imported modules are still flattened into one program for the backend, but
# they no longer share a single flat namespace:
#
# - every item keeps its home file (``source_module`` runtime attribute);
# - items outside their home module's export surface are flattened under a
#   mangled name (``<name>__<hash of home file>``), so private helpers of two
#   different modules can never collide and cannot be called by bare name;
# - all references inside flattened bodies are rewritten to the final names
#   by :func:`_rewrite_module_refs`, which honors local shadowing;
# - the entry program carries ``_module_table``: per-file visible bare-name
#   sets + resolved imports, consumed by SA to reject references to items
#   that were only pulled in as someone else's compile dependencies.
# ---------------------------------------------------------------------------


def _module_mangle_suffix(home: Optional[str]) -> str:
    """Deterministic short hash of a module file path."""
    if not home:
        return "00000000"
    return hashlib.sha1(home.encode("utf-8")).hexdigest()[:8]


def _mangled_item_name(name: str, home: Optional[str]) -> str:
    return f"{name}__{_module_mangle_suffix(home)}"


def _declared_name_field(node: Node) -> Optional[str]:
    """Name field of top-level declarations that occupy the flat namespace."""
    if isinstance(node, (ExternBlock, UseDecl)):
        return None
    value = getattr(node, "name", None)
    return value if isinstance(value, str) else None


def _set_declared_name(node: Node, value: str) -> bool:
    """Rename a flat-namespace declaration in place; False when nameless."""
    if isinstance(node, (ExternBlock, UseDecl)):
        return False
    if isinstance(getattr(node, "name", None), str):
        node.name = value
        return True
    return False


# Nodes whose walk introduces a fresh lexical scope for bindings.
_SCOPE_PUSH_NODES = (FnDecl, Closure)


def _rewrite_module_refs(root: Node, mapping: dict[str, str], bound: frozenset[str]) -> None:
    """Rewrite bare references in *root* according to *mapping*.

    ``mapping`` translates original item names to their flattened (final)
    names.  Rewriting is scope-aware: a local binding (parameter, ``let``,
    loop variable, pattern binding, generic parameter) shadows a mapped
    item name, so its references are left untouched.  Only names that are
    genuinely free at the reference site are rewritten.
    """
    mapping = {k: v for k, v in mapping.items() if k != v}
    if not mapping:
        return

    def rewrite_type(type_: Type, bound: frozenset[str]) -> None:
        name = type_.name
        if (
            not name.startswith(("fn(", "*const ", "*mut ", "["))
            and "::" not in name
            and name not in bound
            and name in mapping
        ):
            type_.name = mapping[name]
        for arg in type_.args:
            rewrite_type(arg, bound)

    def ref_name(name: str, bound: frozenset[str]) -> str:
        if name in bound:
            return name
        return mapping.get(name, name)

    def walk_pattern(pattern: Node, bound: frozenset[str]) -> set[str]:
        """Rewrite pattern heads; returns the names this pattern binds."""
        binds: set[str] = set()
        if isinstance(pattern, BindPattern):
            binds.add(pattern.name)
        elif isinstance(pattern, EnumPattern):
            if pattern.path:
                head = ref_name(pattern.path[0], bound)
                rest = pattern.path[1:]
                pattern.path = [head, *rest]
            for elem in pattern.elems:
                binds |= walk_pattern(elem, bound)
        elif isinstance(pattern, TuplePattern):
            for elem in pattern.elems:
                binds |= walk_pattern(elem, bound)
        elif isinstance(pattern, StructPattern):
            rewrite_type(pattern.type, bound)
            for field in pattern.fields:
                if field.pattern is not None:
                    binds |= walk_pattern(field.pattern, bound)
                else:
                    # shorthand ``Point { x }`` binds the field name
                    binds.add(field.name)
        elif isinstance(pattern, LitPattern):
            walk_expr(pattern.value, bound)
        return binds

    def walk_block(block: Block, bound: frozenset[str] | set[str]) -> None:
        inner = set(bound)
        for stmt in block.stmts:
            walk_stmt(stmt, inner)

    def walk_stmt(stmt: Node, bound: set[str]) -> None:
        frozen = frozenset(bound)
        if isinstance(stmt, LetStmt):
            if stmt.type is not None:
                rewrite_type(stmt.type, frozen)
            if stmt.value is not None:
                walk_expr(stmt.value, frozen)
            bound.add(stmt.name)
        elif isinstance(stmt, ForStmt):
            walk_expr(stmt.iterable, frozen)
            inner = frozenset(bound | {stmt.var})
            if stmt.type is not None:
                rewrite_type(stmt.type, inner)
            walk_block(stmt.body, inner)
        elif isinstance(stmt, WhileStmt):
            walk_expr(stmt.cond, frozen)
            walk_block(stmt.body, bound)
        elif isinstance(stmt, IfStmt):
            walk_expr(stmt.cond, frozen)
            walk_block(stmt.then, bound)
            for branch in stmt.elifs:
                walk_expr(branch.cond, frozen)
                walk_block(branch.body, bound)
            if stmt.else_ is not None:
                walk_block(stmt.else_, bound)
        elif isinstance(stmt, IfLetStmt):
            if stmt.else_ is None:
                walk_expr(stmt.value, frozen)
            else:
                # an if-let with else only binds in the then-branches
                walk_expr(stmt.value, frozen)
            binds = walk_pattern(stmt.pattern, frozen)
            inner = frozenset(bound | binds)
            walk_block(stmt.then, inner)
            for branch in stmt.elifs:
                if branch.pattern is not None and branch.value is not None:
                    walk_expr(branch.value, frozen)
                    bbinds = walk_pattern(branch.pattern, frozen)
                    walk_block(branch.body, frozenset(bound | bbinds))
                else:
                    if branch.cond is not None:
                        walk_expr(branch.cond, frozen)
                    walk_block(branch.body, bound)
            if stmt.else_ is not None:
                walk_block(stmt.else_, bound)
        elif isinstance(stmt, MatchStmt):
            walk_expr(stmt.subject, frozen)
            for arm in stmt.arms:
                arm_binds = walk_pattern(arm.pattern, frozen)
                if arm.guard is not None:
                    walk_expr(arm.guard, frozenset(bound | arm_binds))
                if isinstance(arm.body, Block):
                    walk_block(arm.body, frozenset(bound | arm_binds))
                else:
                    walk_expr(arm.body, frozenset(bound | arm_binds))
        elif isinstance(stmt, ExprStmt):
            walk_expr(stmt.expr, frozen)
        elif isinstance(stmt, ReturnStmt):
            if stmt.value is not None:
                walk_expr(stmt.value, frozen)
        elif isinstance(stmt, ErrorStmt) or isinstance(
            stmt, (BreakStmt, ContinueStmt)
        ):
            pass
        else:
            walk_any(stmt, frozen)

    def walk_expr(expr: Node, bound: frozenset[str]) -> None:
        if isinstance(expr, Name):
            parts = expr.parts
            if len(parts) == 1:
                parts[0] = ref_name(parts[0], bound)
            elif parts:
                head = ref_name(parts[0], bound)
                expr.parts = [head, *parts[1:]]
        elif isinstance(expr, Attribute):
            walk_expr(expr.obj, bound)
            # expr.name is a member access, never a flat-namespace reference
        elif isinstance(expr, Call):
            walk_expr(expr.callee, bound)
            for arg in expr.args:
                walk_expr(arg.value, bound)
        elif isinstance(expr, Index):
            walk_expr(expr.obj, bound)
            walk_expr(expr.index, bound)
        elif isinstance(expr, Slice):
            walk_expr(expr.obj, bound)
            for part in (expr.start, expr.stop, expr.step):
                if part is not None:
                    walk_expr(part, bound)
        elif isinstance(expr, BinOp) or isinstance(expr, Assign):
            walk_expr(expr.left if isinstance(expr, BinOp) else expr.target, bound)
            walk_expr(expr.right if isinstance(expr, BinOp) else expr.value, bound)
        elif isinstance(expr, UnaryOp):
            walk_expr(expr.operand, bound)
        elif isinstance(expr, CastExpr):
            walk_expr(expr.operand, bound)
            rewrite_type(expr.target, bound)
        elif isinstance(expr, VectorLit):
            for elem in expr.elems:
                walk_expr(elem, bound)
        elif isinstance(expr, MapLit):
            for entry in expr.entries:
                walk_expr(entry.key, bound)
                walk_expr(entry.value, bound)
        elif isinstance(expr, TupleLit):
            for elem in expr.elems:
                walk_expr(elem, bound)
        elif isinstance(expr, StructConstruct):
            rewrite_type(expr.type, bound)
            for arg in expr.args:
                walk_any(arg, bound)
        elif isinstance(expr, Closure):
            inner = frozenset(bound | {p.name for p in expr.params})
            if expr.return_type is not None:
                rewrite_type(expr.return_type, inner)
            walk_block(expr.body, inner)
        else:
            walk_any(expr, bound)

    def walk_fn(fn: FnDecl, bound: frozenset[str]) -> None:
        inner = frozenset(
            bound | {p.name for p in fn.params} | {p.name for p in fn.type_params}
        )
        for p in fn.params:
            if p.type is not None:
                rewrite_type(p.type, inner)
        for p in fn.type_params:
            if p.bound is not None:
                rewrite_type(p.bound, inner)
        if fn.return_type is not None:
            rewrite_type(fn.return_type, inner)
        if fn.body is not None:
            walk_block(fn.body, inner)

    def rewrite_bounds(
        params: list["TypeParam"], inner: frozenset[str]
    ) -> None:
        for p in params:
            if p.bound is not None:
                rewrite_type(p.bound, inner)

    def walk_decl(item: Node, bound: frozenset[str]) -> None:
        if isinstance(item, (FnDecl,)):
            walk_fn(item, bound)
        elif isinstance(item, ConstDecl):
            rewrite_type(item.type, bound)
            walk_expr(item.value, bound)
        elif isinstance(item, TypeDecl):
            inner = frozenset(bound | {p.name for p in item.params})
            rewrite_bounds(item.params, inner)
            rewrite_type(item.base, inner)
            if item.where is not None:
                walk_block(item.where, inner)
        elif isinstance(item, StructDecl):
            inner = frozenset(bound | {p.name for p in item.params})
            rewrite_bounds(item.params, inner)
            for f in item.fields:
                rewrite_type(f.type, inner)
                if f.initializer is not None:
                    walk_expr(f.initializer, inner)
                if f.validation is not None:
                    walk_block(f.validation, inner)
        elif isinstance(item, EnumDecl):
            inner = frozenset(bound | {p.name for p in item.params})
            rewrite_bounds(item.params, inner)
            for v in item.variants:
                for f in v.fields:
                    rewrite_type(f, inner)
        elif isinstance(item, TraitDecl):
            inner = frozenset(bound | {p.name for p in item.params})
            rewrite_bounds(item.params, inner)
            # todo-156: supertraits may mention this trait's own params
            # (``trait B<T>: A<T>``), so rename them within ``inner``.
            for st in item.supertraits:
                rewrite_type(st, inner)
            for m in item.methods:
                walk_fn(m, inner)
        elif isinstance(item, (ImplDecl, ExtraDecl)):
            inner = frozenset(bound | {p.name for p in item.params})
            rewrite_bounds(item.params, inner)
            rewrite_type(item.struct, inner)
            if isinstance(item, ImplDecl):
                rewrite_type(item.trait, inner)
                for assoc in item.assoc_types:
                    rewrite_type(assoc.type, inner)
            for m in item.methods:
                walk_fn(m, inner)
        elif isinstance(item, GroupDecl):
            inner = frozenset(bound | {p.name for p in item.params})
            if item.struct is not None and item.struct in mapping and item.struct not in bound:
                item.struct = mapping[item.struct]
            for dist in item.distributions:
                if dist.subject in mapping and dist.subject not in bound:
                    dist.subject = mapping[dist.subject]
                rewrite_type(dist.type, inner)
        elif isinstance(item, GroupApply):
            if item.group in mapping and item.group not in bound:
                item.group = mapping[item.group]
            if item.struct in mapping and item.struct not in bound:
                item.struct = mapping[item.struct]
        elif isinstance(item, ExternBlock):
            # C-side symbol names are never rewritten.
            return
        else:
            walk_any(item, bound)

    def walk_any(node: Node, bound: frozenset[str]) -> None:
        """Fallback for containers without special binding semantics."""
        # Expression shapes may appear in positions walk_expr never sees
        # directly (e.g. ``return match ...`` nests a MatchStmt inside a
        # ReturnStmt), so dispatch them through the typed handlers.
        if isinstance(node, (
            Name, Attribute, Call, Index, Slice, BinOp, UnaryOp, CastExpr,
            VectorLit, MapLit, TupleLit, StructConstruct,
        )):
            walk_expr(node, bound)
            return
        if isinstance(node, Block):
            walk_block(node, bound)
            return
        if isinstance(node, FnDecl):
            walk_fn(node, bound)
            return
        if isinstance(node, (
            LetStmt, ForStmt, WhileStmt, IfStmt, IfLetStmt, MatchStmt,
            ReturnStmt, ExprStmt, BreakStmt, ContinueStmt, ErrorStmt,
        )):
            walk_stmt(node, set(bound))
            return
        for f in _dc_fields(node):
            value = getattr(node, f.name)
            if isinstance(value, Node):
                walk_any(value, bound)
            elif isinstance(value, list):
                for element in value:
                    if isinstance(element, Node):
                        walk_any(element, bound)

    if isinstance(root, Block):
        walk_block(root, bound)
    else:
        walk_decl(root, bound)


class Parser:
    """A recursive-descent parser over a token list."""

    def __init__(self, tokens: list[Token]) -> None:
        self.tokens = [t for t in tokens if t.kind != TokenKind.COMMENT]
        self.pos = 0
        self.errors: list[ParseError] = []
        self._pending: deque[Token] = deque()  # synthetic tokens (from `>>` splits)
        self._for_iterable_expr = False
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

    def _brace_is_struct_construct(self) -> bool:
        """True if the ``{ ... }`` at the cursor is a struct construction
        (comma-separated expressions) rather than a block.

        A top-level ``;`` inside the braces means it is a statement block
        (e.g. a for/while body following its iterable expression); struct
        arguments never contain statements.  bug-35: ``;`` inside ``(...)``
        or ``[...]`` (array types / ``[x; N]`` repeat literals) is not a
        statement separator, so those nestings are tracked as well.
        """
        depth = 0
        group_depth = 0
        offset = 0
        while True:
            tok = self._peek(offset)
            if tok is None:
                return False
            if tok.kind == TokenKind.LBRACE:
                depth += 1
            elif tok.kind == TokenKind.RBRACE:
                depth -= 1
                if depth == 0:
                    return True
            elif tok.kind in (TokenKind.LPAREN, TokenKind.LBRACKET):
                group_depth += 1
            elif tok.kind in (TokenKind.RPAREN, TokenKind.RBRACKET):
                group_depth -= 1
            elif tok.kind == TokenKind.SEMICOLON and depth == 1 \
                    and group_depth == 0:
                return False
            offset += 1

    def _brace_looks_like_map(self) -> bool:
        """True if the ``{ ... }`` at the cursor has a top-level ``:``.

        Struct construction is positional and never contains a top-level
        colon, so this distinguishes ``Type<T> { a, b }`` from a comparison
        followed by a map literal ``A < B > { "k": v }``.  Colons inside
        ``(...)``/``[...]`` nestings are ignored (bug-35 mirrors).
        """
        depth = 0
        group_depth = 0
        offset = 0
        while True:
            tok = self._peek(offset)
            if tok is None:
                return False
            if tok.kind == TokenKind.LBRACE:
                depth += 1
            elif tok.kind == TokenKind.RBRACE:
                depth -= 1
                if depth == 0:
                    return False
            elif tok.kind in (TokenKind.LPAREN, TokenKind.LBRACKET):
                group_depth += 1
            elif tok.kind in (TokenKind.RPAREN, TokenKind.RBRACKET):
                group_depth -= 1
            elif tok.kind == TokenKind.COLON and depth == 1 \
                    and group_depth == 0:
                return True
            offset += 1

    def _try_parse_generic_struct_construct(self) -> Optional[Type]:
        """Speculatively parse ``Name<Args> { ... }`` as a struct construction.

        Angle brackets are ambiguous between generics and comparisons, so the
        parse is rolled back unless a brace that looks like positional struct
        arguments follows immediately.
        """
        snap = self._snapshot()
        try:
            type_ = self._parse_type()
            if (
                self._at(TokenKind.LBRACE)
                and self._brace_is_struct_construct()
                and not self._brace_looks_like_map()
            ):
                return type_
        except ParseError:
            pass
        self._restore(snap)
        return None

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

    # -- program -----------------------------------------------------------

    def parse_program(self) -> Program:
        first = self._peek()
        line = first.line if first is not None else 1
        column = first.column if first is not None else 1
        items: list[Node] = []
        # todo-124: entry-authored declarations (loaded module items are
        # excluded) -- the target set for `use ... as` reference rewrites.
        entry_items: list[Node] = []
        # todo-76/97: the implicit wildcard imports (``std::prelude::*``,
        # then the package's own ``lib.wd`` facade) are resolved before the
        # token loop but merged *after* it, so locally declared names can
        # shadow imported ones (Rust-style) instead of colliding with them.
        autos = self._parse_auto_prelude()
        while self._peek() is not None:
            # todo-86/93: attributes may prefix any top-level item, a
            # ``use`` declaration included.
            attrs = self._parse_attributes()
            is_pub_use = (
                self._at(TokenKind.PUB)
                and self._peek(1) is not None
                and self._peek(1).kind == TokenKind.USE
            )
            if self._at(TokenKind.USE) or is_pub_use:
                pub_tok = self._match(TokenKind.PUB)
                use_tok = self._advance()
                # todo-86/93: cfg is evaluated *before* resolution so a
                # platform-specific import never fails to resolve on the
                # targets it is gated away from.
                keep, unsupported = self._filter_use_attributes(attrs)
                if not keep:
                    while self._peek() is not None:
                        if self._match(TokenKind.SEMICOLON) is not None:
                            break
                        self._advance()
                    self.errors.extend(unsupported)
                    continue
                try:
                    decls = self._parse_use(
                        use_tok,
                        pub=pub_tok is not None,
                    )
                except ParseError as exc:
                    self.errors.append(exc)
                    self._synchronize_top_level()
                else:
                    # todo-79: attribute the import to its containing file so
                    # the module scope table can group visible names/imports.
                    # todo-112: a grouped import expands into one UseDecl per
                    # unique element; every one of them is tagged and fed to
                    # the flattening surface like a hand-written import.
                    for decl in decls:
                        self._tag_source_module(decl)
                        items.append(decl)
                        self._append_unique(
                            items, getattr(decl, "loaded_items", [])
                        )
                self.errors.extend(unsupported)
                continue
            # todo-126: ``[pub] export crate <name> [as <alias>];`` --
            # CWind's Rust-2015 `extern crate`: bind a whole crate (a
            # top-level module tree) under a name in this file.  It is a
            # restricted module import, so it shares the use machinery and
            # carries a ``crate_export`` provenance flag.
            if self._at_export_crate():
                pub_tok = self._match(TokenKind.PUB)
                export_tok = self._peek()
                keep, unsupported = self._filter_use_attributes(attrs)
                if not keep:
                    while self._peek() is not None:
                        if self._match(TokenKind.SEMICOLON) is not None:
                            break
                        self._advance()
                    self.errors.extend(unsupported)
                    continue
                try:
                    decl = self._parse_export_crate(
                        export_tok, pub=pub_tok is not None
                    )
                except ParseError as exc:
                    self.errors.append(exc)
                    self._synchronize_top_level()
                else:
                    self._tag_source_module(decl)
                    items.append(decl)
                    self._append_unique(
                        items, getattr(decl, "loaded_items", [])
                    )
                self.errors.extend(unsupported)
                continue
            # todo-107: ``[pub [vis]] mod name;`` / ``mod name { ... }`` --
            # Rust-style module declarations.  The external form registers
            # the submodule on the module tree (todo-158: only when the
            # declaring mod.wind lists it); ``pub mod`` re-exports it to
            # importers.  The inline form flattens its items into the
            # program tagged with the extended module path.
            # The lookahead covers every head shape: ``mod``, ``pub mod``
            # and ``pub(<qual>) mod`` (a ``pub(...`` token pair can only
            # precede an item, so peeking past the qualifier is safe).
            pub_head = self._at(TokenKind.PUB)
            mod_head = self._at(TokenKind.MOD)
            if pub_head:
                nxt = self._peek(1)
                if nxt is not None and nxt.kind == TokenKind.MOD:
                    mod_head = True
                elif (
                    nxt is not None and nxt.kind == TokenKind.LPAREN
                ):
                    vis_snap = self._snapshot()
                    self._advance()  # pub
                    try:
                        self._parse_visibility(True)
                        after = self._peek()
                        mod_head = (
                            after is not None
                            and after.kind == TokenKind.MOD
                        )
                    except ParseError:
                        # An invalid qualifier is reported by the ordinary
                        # item path below; here it only means "not a mod".
                        mod_head = False
                    finally:
                        self._restore(vis_snap)
            if mod_head:
                try:
                    pub_tok = self._match(TokenKind.PUB)
                    visibility, vis_path = self._parse_visibility(
                        pub_tok is not None
                    )
                    decl = self._parse_mod(
                        pub_tok is not None, visibility, vis_path
                    )
                except ParseError as exc:
                    self.errors.append(exc)
                    self._synchronize_top_level()
                    items.append(ErrorStmt(exc.line, exc.column, exc.message))
                else:
                    self._tag_source_module(decl)
                    items.append(decl)
                    entry_items.append(decl)
                    # todo-107 (namespace model): inline bodies stay inside
                    # their ModDecl — no flattening, so same-named private
                    # items of sibling inline modules never collide.  SA
                    # resolves through the registered namespace.  The use
                    # statements inside the namespace still load their
                    # targets into the flat program (bodies reference them).
                    self._append_unique(items, self._inline_loaded_items)
                    self._inline_loaded_items = []
                continue
            pub = self._match(TokenKind.PUB) is not None
            # todo-107/119: restricted visibility variants.  ``pub(x)`` is
            # parsed by the shared ``_parse_visibility`` helper; the result
            # rides on the parsed item as ``visibility``/``vis_path`` and is
            # enforced by ``_visibility_admits``.
            try:
                visibility, vis_path = self._parse_visibility(pub)
            except ParseError as exc:
                self.errors.append(exc)
                self._synchronize_top_level()
                items.append(ErrorStmt(exc.line, exc.column, exc.message))
                continue
            try:
                item = self._parse_item(pub)
                if self._apply_attributes(item, attrs):
                    # todo-86/93: a false #[cfg] drops the item entirely.
                    if visibility is not None:
                        item.visibility = visibility  # type: ignore[attr-defined]
                        item.vis_path = vis_path  # type: ignore[attr-defined]
                    self._tag_source_module(item)
                    items.append(item)
                    entry_items.append(item)
            except ParseError as exc:
                self.errors.append(exc)
                self._synchronize_top_level()
                items.append(ErrorStmt(exc.line, exc.column, exc.message))
        # todo-124: bare references to an item import renamed with ``as``
        # are rewritten to the item's real (flattened) name.  ``use m::item
        # as c;`` is a per-file rename: the flat namespace keeps ``item``
        # (so other importers and the visibility table are unaffected) and
        # the entry's own free ``c`` references denote ``item``.  The
        # rewrite is scope-aware: a local binding named ``c`` shadows the
        # alias.  Module renames (``use a::b as c;`` with no ``item``) are
        # namespace aliases resolved by SA through ``self.modules``.
        for decl in items:
            if (
                isinstance(decl, UseDecl)
                and decl.alias
                and decl.item
                and not decl.wildcard
            ):
                mapping = {decl.alias: decl.item}
                for node in entry_items:
                    _rewrite_module_refs(node, mapping, frozenset())
        # ``_merge_auto_prelude(U, A)`` places A *underneath* U (A's items
        # shadowed by same-named U declarations are dropped).  Merging in
        # reversed([std, lib]) order therefore layers the program as
        # [std prelude, package lib, user code], std at the very bottom.
        pkg_auto = autos[1] if len(autos) > 1 else None
        for auto in reversed(autos):
            shadowed = self._auto_shadow_names(items, auto, pkg_auto)
            items = [*self._merge_auto_prelude(items, auto, shadowed), *items]
        # bug-61: importing a trait brings its implementations with it
        # (Rust semantics — rustc registers every trait impl at resolution
        # time, independent of re-exports, and method resolution queries
        # all impls of an imported trait).  The auto prelude re-exports
        # ``Wrapping`` but not the ``impl Wrapping<...> for ...`` blocks
        # living in ``std::expansion`` modules; once those prelude
        # ``pub use`` lines were dropped (ca9e412), the impls silently
        # vanished.  One single pull pass, ENTRY PARSES ONLY — pulled impl
        # modules ride the root program like everything else, and doing
        # the pull in child parses would recurse into the registry builder
        # through their own ``parse_program`` calls.
        if self._is_root_source():
            self._pull_trait_impls(items)
        # Several import surfaces can reach the same module file through
        # the shared per-process cache; identical node instances must land
        # in the program exactly once or SA reports duplicate definitions.
        seen_ids: set[int] = set()
        unique_items: list[Node] = []
        for node in items:
            if id(node) in seen_ids:
                continue
            seen_ids.add(id(node))
            unique_items.append(node)
        program = Program(line, column, unique_items)
        # todo-107: SA walks every loaded module file's items for inline
        # ``mod`` namespaces; share the cache instead of re-parsing.
        program._module_file_programs = dict(self._module_cache)  # type: ignore[attr-defined]
        self._build_module_table(program)
        return program

    def _pull_trait_impls(self, items: list[Node]) -> None:
        """bug-61: append impl blocks of every trait in *items* to *items*.

        Consults the per-root trait-impl registry (see
        :func:`_impl_registry_for`); for each (trait, file) not already in
        the compile surface, the implementing module's selected items join
        the entry program directly (node instances shared with normal
        imports; the ``_scope_flat`` guard keeps double renames away).
        Re-pulled traits from the same file are guarded by (trait, file).
        """
        registry, programs = _impl_registry_for(
            self._import_root(), self._module_cache
        )
        present_ids = {id(node) for node in items}
        inflight: set[tuple[str, str]] = set()
        queue: list[str] = []
        for node in items:
            if isinstance(node, TraitDecl) and node.name:
                queue.append(node.name)
        pulled: list[Node] = []
        head = 0
        while head < len(queue):
            trait_name = queue[head]
            head += 1
            for file_key, _owner in registry.get(trait_name, ()):
                edge = (trait_name, file_key)
                if edge in inflight:
                    continue
                inflight.add(edge)
                impl_program = programs.get(file_key)
                if impl_program is None:
                    continue
                impl_items, _, _ = self._select_module_items(
                    impl_program,
                    item=None,
                    line=1,
                    column=1,
                )
                for node in impl_items:
                    if id(node) in present_ids:
                        continue
                    present_ids.add(id(node))
                    pulled.append(node)
                    if isinstance(node, TraitDecl) and node.name:
                        # A pulled impl module may carry another trait the
                        # entry never saw; its impls get pulled too.
                        queue.append(node.name)
        items.extend(pulled)

    def _build_module_table(self, program: Program) -> None:
        """todo-79: record every module file's bare-name visibility set.

        The table maps each participating source file to:

        - ``visible``: the exact set of names that file may reference by
          bare name -- its own top-level items (under their flattened final
          names) plus the export surface of every ``use`` it declares
          (the implicit prelude included).  SA consults this so an item
          pulled in only as someone else's compile dependency cannot be
          referenced from outside;
        - ``imports``: one provenance entry per ``use`` of that file.

        Pure runtime data (never serialized): consumers use
        ``getattr(program, "_module_table", None)``.
        """
        entry_path = getattr(self, "source_path", None)
        raw: dict[Optional[str], dict] = {}

        def bucket(home: Optional[str]) -> dict:
            return raw.setdefault(home, {"visible": set(), "imports": []})

        def add_import(home: Optional[str], decl: UseDecl) -> None:
            entry = bucket(home)
            from_mod = bool(getattr(decl, "_from_mod_decl", False))
            row = {
                "path": list(decl.parts),
                "source": decl.module,
                "item": getattr(decl, "item", None),
                "wildcard": bool(getattr(decl, "wildcard", False)),
                "auto": bool(getattr(decl, "auto", False)),
                "pub": bool(decl.pub),
                "alias": getattr(decl, "alias", None),
                "crate_export": bool(getattr(decl, "crate_export", False)),
            }
            if from_mod:
                # Materialized ``mod name;`` implicit use: carries the
                # submodule namespace for SA's alias registration.
                row["from_mod_decl"] = True
                row["exported_names"] = sorted(
                    getattr(decl, "exported_names", ()) or ()
                )
            entry["imports"].append(row)
            exports = getattr(decl, "exported_names", None)
            if isinstance(exports, frozenset):
                entry["visible"].update(exports)

        for item in program.items:
            home = getattr(item, "source_module", None)
            if isinstance(item, UseDecl):
                add_import(home if home else entry_path, item)
                continue
            if isinstance(item, ExternBlock):
                # bug-37: 无名 extern 块的 fn/static 属于声明它们的文件,
                # 必须进该文件的裸名可见集 —— 否则同文件内的 CFFI 调用
                # (如 atexit(clean)) 被 _reject_hidden 误报为
                # "belongs to another module" (与 _select_module_items 的
                # 成员按名注册逻辑保持一致)。
                for member in (*item.fns, *item.statics):
                    mname = getattr(member, "name", None)
                    if isinstance(mname, str):
                        bucket(home)["visible"].add(mname)
                continue
            name = self._declaration_name(item)
            if name is not None:
                bucket(home)["visible"].add(name)
            # todo-79: methods must inherit their block's defining file so
            # per-module visibility checks work inside method bodies too.
            if isinstance(item, (ExtraDecl, ImplDecl, TraitDecl)) and home:
                for method in item.methods:
                    if getattr(method, "source_module", None) is None:
                        method.source_module = home  # type: ignore[attr-defined]
        # Imported modules keep their own ``use`` declarations in their
        # cached programs; they contribute to their own files' surfaces.
        for path, child_program in self._module_cache.items():
            for sub in child_program.items:
                if isinstance(sub, UseDecl):
                    add_import(path, sub)
        # bug-37: std prelude 的导出面对*每个*文件都可见 (Rust 把 prelude
        # 注入所有模块)。否则导入模块里的 prelude 别名 (u32/i32/...)
        # 会被 _reject_hidden 误判为 "belongs to another module"
        # (如 stdlib.wind 的 `random_seed(seed: u32)` / `randint() -> i32`)。
        prelude_exports: frozenset[str] = frozenset()
        shadow_renamed: set[str] = set()
        for item in program.items:
            if not isinstance(item, UseDecl) or not getattr(item, "auto", False):
                continue
            exports = getattr(item, "exported_names", None)
            if isinstance(exports, frozenset):
                prelude_exports |= exports
            # bug-54: 被遮蔽重命名的本层 fn (panic__<hash>) 是 std 内部
            # 连接名, 任意文件的依赖闭包体都可能引用 —— 与导出面同一
            # 机制对所有文件可见 (入口代码不会引用这些连接名)。
            renames = getattr(item, "shadow_renames", None)
            if isinstance(renames, dict):
                shadow_renamed.update(renames.values())
        if prelude_exports:
            for data in raw.values():
                data["visible"].update(prelude_exports)
        if shadow_renamed:
            for data in raw.values():
                data["visible"].update(shadow_renamed)
        program._module_table = {  # type: ignore[attr-defined]
            home: {
                "visible": frozenset(data["visible"]),
                "imports": data["imports"],
            }
            for home, data in raw.items()
        }

    def _auto_shadow_names(
        self,
        items: list[Node],
        auto: UseDecl,
        pkg_auto: Optional[UseDecl],
    ) -> set[str]:
        """Names that may shadow the auto-imported layer ``auto``.

        Only layers strictly above the layer being merged shadow it: the
        entry file's own declarations always; plus, when merging the std
        prelude under a package lib, every name the package facade
        exports (its ``loaded_items``, re-exports included -- the facade
        file itself is often a pure ``pub use`` stub).  Items flattened
        through the entry's explicit ``use`` statements never count --
        otherwise importing a module whose dependency closure pulls
        ``Option``/``panic`` would silently strip the prelude's
        re-exports of them.  Untagged sources (stdin / in-memory tests)
        keep the legacy all-shadow behavior.
        """
        entry_home = getattr(self, "source_path", None)
        names: set[str] = set()
        # bug-43: impl/extra blocks define no flat-namespace name of their
        # own, but ``_declaration_name`` falls back to their target type
        # name (``impl ... for i32`` -> "i32").  Counting that as an entry
        # declaration silently shadowed the prelude's same-named typedef
        # (i32/u32/...) and cascaded into "unknown struct/type 'i32'".
        skip = (ImplDecl, ExtraDecl)
        if entry_home is None:
            # Legacy permissive mode: everything shadows.
            for node in items:
                if isinstance(node, skip):
                    continue
                name = self._declaration_name(node)
                if name is not None:
                    names.add(name)
        else:
            for node in items:
                if getattr(node, "source_module", None) == entry_home:
                    if isinstance(node, skip):
                        continue
                    name = self._declaration_name(node)
                    if name is not None:
                        names.add(name)
        # todo-158: the std layer is the root module import (parts == ["std"]);
        # the old ``std::prelude`` spelling is gone.
        if auto is not None and auto.parts != ["std"]:
            return names
        if pkg_auto is not None:
            for node in getattr(pkg_auto, "loaded_items", []) or []:
                name = self._declaration_name(node)
                if name is not None:
                    names.add(name)
        return names

    def _merge_auto_prelude(
        self,
        user_items: list[Node],
        auto: UseDecl,
        shadowed: set[str],
    ) -> list[Node]:
        """Merge the implicit prelude under explicit user definitions.

        Prelude declarations whose name is shadowed (``shadowed`` -- the
        entry's own declarations and the package lib layer) are dropped,
        and extra/impl blocks whose owner no longer survives are dropped
        with it.  This keeps projects that define their own
        ``Option``/``panic`` usable while still providing the prelude
        everywhere else.

        bug-54: a shadowed top-level *fn* is no longer dropped outright --
        it is kept under a home-file mangled name (``panic__<hash>``, the
        same scheme as private dependency-closure helpers).  Other std
        bodies still call it (``option.wind``'s ``unwrap_failed`` calls
        ``panic::panic``); dropping the declaration made those callers
        resolve against the entry file's flat scope and hijack the user's
        same-named function (the "signature hybrid").  References inside
        the layer's kept bodies are rewritten to the mangled name, and the
        mangled names join the prelude surface visible to every file
        (``_build_module_table``), so std callers resolve to std's own
        function while entry-file references keep resolving to the user's
        declaration -- Rust's "local definition shadows the glob import"
        without hijacking std internals.
        """
        kept: list[Node] = []
        survivors: set[str] = set()
        renames: dict[str, str] = {}
        suffix = _module_mangle_suffix(getattr(auto, "module", None))
        loaded = getattr(auto, "loaded_items", [])
        for node in loaded:
            name = self._declaration_name(node)
            if (
                name is not None
                and name in shadowed
                and isinstance(node, FnDecl)
            ):
                # bug-54: rename & keep -- see docstring.
                final = f"{name}__{suffix}"
                node._scope_orig = name  # type: ignore[attr-defined]
                _set_declared_name(node, final)
                renames[name] = final
                kept.append(node)
                continue
            if name is not None and name in shadowed:
                continue
            kept.append(node)
            if name is not None and not isinstance(node, (ExtraDecl, ImplDecl)):
                survivors.add(name)
        kept = [
            node for node in kept
            if not isinstance(node, (ExtraDecl, ImplDecl))
            or self._declaration_name(node) in survivors
        ]
        if renames:
            # 闭包体内对被重命名 fn 的引用 (含此前已被
            # _localize_qualified_refs 展平的限定调用) 同步改写;
            # 作用域感知, 局部绑定遮蔽处不动。
            for node in kept:
                _rewrite_module_refs(node, renames, frozenset())
            auto.shadow_renames = renames  # type: ignore[attr-defined]
        dropped = {
            self._declaration_name(node) for node in loaded
        } - {
            self._declaration_name(node) for node in kept
        }
        dropped.discard(None)
        if isinstance(auto.exported_names, frozenset):
            auto.exported_names = auto.exported_names - dropped
        return [auto, *kept]

    @staticmethod
    def _declaration_name(node: Node) -> Optional[str]:
        """Name used when selecting one item from a module.

        ``ModDecl`` (todo-107) is a structural declaration, not a flat
        symbol: submodule addressability is decided by the module tree, so
        it never contributes a name to any surface.
        """
        if isinstance(node, ModDecl):
            return None
        value = getattr(node, "name", None)
        if isinstance(value, str):
            return value
        owner = getattr(node, "struct", None)
        if owner is not None:
            value = getattr(owner, "name", None)
            if isinstance(value, str):
                return value
        return None

    def _visibility_admits(self, decl: Node) -> bool:
        """todo-107/119: restricted-visibility gate for one declaration.

        Variants (Rust semantics, paths resolved against the current file's
        module path):

        - ``pub(self)``  — visible only within the defining file itself;
        - ``pub(super)`` — visible within the parent module's subtree;
        - ``pub(crate)`` / ``pub(std)`` — visible across one module root
          tree (``libs/`` or a Breeze source root);
        - ``pub(in path)`` — visible within the module the path anchors
          (segments may be ``super``; a crate-rooted path folds to the
          module-tree prefix).

        During module-item selection the "importer" is the module being
        selected (its own ``use``/bodies see the full subtree scope);
        during entry-body checks it is the file under analysis.  Files off
        the declaration chain (bin entries) are outside every subtree.
        """
        visibility = getattr(decl, "visibility", None)
        if visibility is None:
            return True
        home = getattr(decl, "source_module", None)
        select_home = getattr(self, "_vis_select_home", None)
        # ``super``/``in`` are judged against the selected module itself
        # during export-surface selection (the module is always inside its
        # own subtree scope); every other variant keeps the file under
        # analysis as the importer.
        importer = (
            select_home if (select_home and visibility in ("super", "in"))
            else getattr(self, "source_path", None)
        )
        if not importer or not home:
            return True
        importer_path = Path(importer).resolve()
        home_path = Path(home).resolve()

        def same_root() -> bool:
            for root in _module_roots(self._import_root()):
                try:
                    importer_path.relative_to(root.directory)
                except ValueError:
                    continue
                try:
                    home_path.relative_to(root.directory)
                except ValueError:
                    continue
                return True
            return False

        if visibility in ("crate", "std"):
            return same_root()
        home_parts = self._canonical_module_parts(home)
        if visibility == "self":
            return importer_path == home_path
        current_parts = (
            self._canonical_module_parts(importer)
            if importer_path.is_absolute()
            else None
        )
        if current_parts is not None:
            # ``super``/``in`` are subtree-scoped: the importer must live
            # on the declaring tree (a wild entry file like main.wd sits
            # outside every declaration chain — the bin/lib split).
            probe_tree = _library_tree(self._import_root())
            probe = (
                probe_tree.crate
                if self._current_root_kind() == "crate"
                else probe_tree.std
            )
            for seg in current_parts:
                probe = probe.children.get(seg)
                if probe is None or probe.entry is None:
                    current_parts = None
                    break
        if visibility == "super":
            # Parent module's subtree: both files live under the parent.
            # An importer off the declaration tree (bin entry) never sees
            # subtree-scoped members — except during the module's own
            # export-surface selection, where the module itself (always
            # inside its own scope) is the importer.
            if home_parts is None:
                return False
            if current_parts is None:
                return False
            return current_parts[:-1] == home_parts[:-1]
        # ``pub(in path)``: fold `super` segments against the defining
        # module's path, then require the importer to live under it.
        segments = list(getattr(decl, "vis_path", None) or [])
        if home_parts is None:
            return False
        folded: list[str] = []
        for seg in segments:
            if seg == "super":
                if folded:
                    folded = folded[:-1]
                elif home_parts:
                    home_parts = home_parts[:-1]
                continue
            if seg == "crate":
                continue
            if seg == "self":
                continue
            folded.append(seg)
        target = [*home_parts, *folded]
        if current_parts is None:
            # The crate root file itself (lib.wd) is the root module: it
            # sits inside every subtree whose anchor folds to the root.
            return not target
        return current_parts[: len(target)] == target

    def _pub_use_surface(self, u: "UseDecl") -> set[str]:
        """todo-119: the names a ``pub use`` contributes to its host module.

        ``pub(std)`` restrictions travel with the underlying declaration: a
        re-export may only surface items the *current* import boundary
        admits, so a facade file cannot launder std-only items into a
        public API for outside consumers.
        """
        names = (
            {u.item}
            if getattr(u, "item", None) is not None
            else set(getattr(u, "exported_names", frozenset()))
        )
        blocked: set[str] = set()
        for t in getattr(u, "loaded_items", []):
            if self._visibility_admits(t):
                continue
            declared = self._declaration_name(t)
            if declared is not None:
                blocked.add(declared)
            if isinstance(t, ExternBlock):
                for member in (*t.fns, *t.statics):
                    member_name = getattr(member, "name", None)
                    if isinstance(member_name, str):
                        blocked.add(member_name)
        return names - blocked

    def _materialize_mod_decl(
        self,
        loaded: Program,
        decl: ModDecl,
        line: int,
        column: int,
    ) -> Optional[UseDecl]:
        """todo-107: turn one external ``mod name;`` into an implicit use.

        Rust's mod semantics: the declaration itself brings the submodule
        into scope (``secret::value()`` resolves in sibling code without a
        separate ``use``), and ``pub mod`` re-exports it.  The submodule
        file is resolved relative to the declaring file's module path on
        the tree the file belongs to.
        """
        home = getattr(loaded, "_registry_home", None)
        if home is None:
            home = next(
                (
                    getattr(n, "source_module", None)
                    for n in loaded.items
                    if getattr(n, "source_module", None)
                ),
                None,
            )
        if home is None:
            return None
        saved_source = getattr(self, "source_path", None)
        try:
            self.source_path = home
            current = self._current_module_parts()
            kind = self._current_root_kind()
        finally:
            self.source_path = saved_source
        tree = _library_tree(self._import_root())
        scoped = tree.crate if kind == "crate" else tree.std
        lookup = [*(current or []), decl.name]
        remaining, entry = scoped.find_longest(lookup)
        if entry is None or remaining:
            return None
        sub_use = UseDecl(
            decl.line, decl.column, list(lookup),
            wildcard=False, item=None,
            # Always a private bring-into-scope: ``pub mod`` re-exports the
            # *module name* (gated by the trie's pub bit), it never
            # scatters the submodule's items into the parent's export
            # surface — Rust's `pub mod` semantics.
            pub=False,
        )
        sub_use.module = str(entry)
        saved_current = self.current_use_decl
        # The declaring module itself is the scope this implicit use lives
        # in: subtree-scoped gates (pub(super)/pub(in path)) of the
        # submodule's members are judged from its vantage.
        saved_ctx = getattr(self, "_vis_select_home", None)
        self._vis_select_home = home
        self.current_use_decl = sub_use
        try:
            sub_prog = self._load_module(entry, None)
            (
                sub_use.loaded_items,
                sub_use.exported_names,
                sub_use.known_names,
            ) = self._select_module_items(
                sub_prog, item=None, line=line, column=column
            )
        finally:
            self._vis_select_home = saved_ctx
            self.current_use_decl = saved_current
        sub_use._from_mod_decl = True  # type: ignore[attr-defined]
        sub_use._mod_decl_pub = bool(decl.pub)  # type: ignore[attr-defined]
        return sub_use

    def _select_module_items(
        self,
        loaded: Program,
        *,
        item: Optional[str],
        line: int,
        column: int,
    ) -> tuple[list[Node], frozenset[str], frozenset[str]]:
        """Split a parsed module into its compile and export surfaces.

        Returns ``(items, exported_names, known_names)``:

        - ``items``: declarations flattened into the importing program.  This
          is the compile-dependency surface: the public API plus exactly
          those private helpers it references (dependency closure).
        - ``exported_names``: names addressable as ``module::name`` by the
          importer.  Wildcard/plain imports expose the public API; an
          explicit ``use m::item;`` exposes only ``item``.
        - ``known_names``: every top-level name in the module, used for
          precise "private" vs "no such member" diagnostics.
        """
        return self._select_module_items_inner(
            loaded, item=item, line=line, column=column
        )

    def _select_module_items_inner(
        self,
        loaded: Program,
        *,
        item: Optional[str],
        line: int,
        column: int,
    ) -> tuple[list[Node], frozenset[str], frozenset[str]]:
        decls = [
            n for n in loaded.items
            if not isinstance(n, (UseDecl, ModDecl))
        ]
        uses = [n for n in loaded.items if isinstance(n, UseDecl)]
        # todo-107: external ``mod name;`` declarations bring the submodule
        # into scope (Rust semantics) — materialize them as implicit uses
        # so alias registration, export surfaces and dependency closures
        # treat them exactly like hand-written ``use self::name;``.  The
        # materialized use is cached on the ModDecl (node instances are
        # shared across importers) and appended to ``loaded.items`` so the
        # module table grants the *declaring file's* scope its surface.
        for n in list(loaded.items):
            if not isinstance(n, ModDecl) or n.body is not None:
                continue
            sub: Optional[UseDecl] = getattr(n, "_materialized_use", None)
            if sub is None:
                sub = self._materialize_mod_decl(loaded, n, line, column)
                if sub is not None:
                    n._materialized_use = sub  # type: ignore[attr-defined]
                    loaded.items.append(sub)
            if sub is not None:
                uses.append(sub)
        by_name: dict[str, list[Node]] = {}
        for d in decls:
            name = self._declaration_name(d)
            if name is not None:
                by_name.setdefault(name, []).append(d)
                # todo-79: an item flattened (and possibly renamed) by an
                # earlier import surface is reachable under both spellings.
                orig = getattr(d, "_scope_orig", None)
                if isinstance(orig, str) and orig != name:
                    by_name.setdefault(orig, []).append(d)
        # ExternBlock 本身无名, 其成员需按名注册 (值指向宿主块):
        # 导入模块的方法体裸调用 C 绑定 (如 fopen) 时, 依赖闭包
        # 才能把整个块拉进编译面, 否则 SA 报 Unknown function。
        for d in decls:
            if isinstance(d, ExternBlock):
                for member in (*d.fns, *d.statics):
                    member_name = getattr(member, "name", None)
                    if isinstance(member_name, str):
                        by_name.setdefault(member_name, []).append(d)

        # Flattened items behind the module's own imports.  Qualified
        # references such as ``panic::panic(...)`` inside selected bodies
        # resolve through these aliases.
        alias_items: dict[str, list[Node]] = {}
        # todo-import-closure: wildcard ``use m::*;`` ends in ``*``, so the
        # last-segment key above cannot be used to resolve bare references
        # into transitively imported modules (e.g. std file bindings that
        # call simplified_libc externs).  Index those items by their
        # declared names -- and by ExternBlock member names -- so the
        # dependency closure below reaches them as well.
        dep_items: dict[str, list[Node]] = {}
        for u in uses:
            loaded_items = getattr(u, "loaded_items", [])
            alias_items.setdefault(
                getattr(u, "alias", None) or u.parts[-1], []
            ).extend(loaded_items)
            for t in loaded_items:
                declared = self._declaration_name(t)
                if declared is not None:
                    dep_items.setdefault(declared, []).append(t)
                if isinstance(t, ExternBlock):
                    for member in (*t.fns, *t.statics):
                        member_name = getattr(member, "name", None)
                        if isinstance(member_name, str):
                            dep_items.setdefault(member_name, []).append(t)

        local_names = frozenset(by_name)

        # Names reachable only through a *non*-pub ``use`` stay internal to
        # the module: compile dependencies, but never part of its API.
        transitive_only: set[str] = set()
        pub_reexports: set[str] = set()
        for u in uses:
            names = {
                n for n in (
                    self._declaration_name(t)
                    for t in getattr(u, "loaded_items", [])
                )
                if n is not None
            }
            if u.pub:
                pub_reexports |= self._pub_use_surface(u)
            else:
                transitive_only |= names - local_names

        pub_names = {
            name for name, group in by_name.items()
            if any(
                getattr(d, "pub", False) and self._visibility_admits(d)
                for d in group
            )
        } | pub_reexports
        # bug-56: impl/extra 块 (块本身无 pub, 块名 = owner 类型名) 属于
        # 所在模块的编译依赖面: 裸名/限定调用都要能落到它的方法上, 不得
        # 因 owner 不在导出面而被剔除 (trait 方法派发查的是全部绑定)。
        block_names = {
            name for name, group in by_name.items()
            if any(isinstance(d, (ExtraDecl, ImplDecl)) for d in group)
        }
        pub_names |= block_names

        if item is not None:
            candidates = by_name.get(item, [])
            if not candidates:
                raise ParseError(
                    f"module has no item '{item}'",
                    line,
                    column,
                    category="unknown module item",
                )
            seeds = []
            for d in candidates:
                # bug-40: 显式项导入的候选项可能是 extern 块本体,
                # 此时可见性取决于块级 pub 或该成员自身的 pub.
                if isinstance(d, ExternBlock):
                    member = next(
                        (m for m in (*d.fns, *d.statics)
                         if getattr(m, "name", None) == item),
                        None,
                    )
                    if getattr(d, "pub", False) or (
                        member is not None and getattr(member, "pub", False)
                    ):
                        seeds.append(d)
                elif (
                    getattr(d, "pub", False)
                    and self._visibility_admits(d)
                ) or isinstance(d, (ExtraDecl, ImplDecl)):
                    seeds.append(d)
            if not seeds:
                raise ParseError(
                    f"item '{item}' is private in module",
                    line,
                    column,
                    category="private module item",
                )
            exported: frozenset[str] = frozenset({item})
        else:
            seeds = []
            exported_set: set[str] = set(pub_reexports)
            for d in decls:
                if isinstance(d, ExternBlock):
                    # C 绑定块没有顶层名 (自身不进导出面), 但 pub 块属于
                    # 模块的编译面: 通配/普通导入必须携带整个块, 其成员
                    # 名计入导出面 —— 否则迁移到独立 libc 封装模块的
                    # 绑定经依赖闭包不可达, 且被可见性表误判为外部项。
                    # bug-40: 块内自带 pub 的成员同样导出.
                    block_pub = getattr(d, "pub", False)
                    member_pub = [
                        m for m in (*d.fns, *d.statics)
                        if getattr(m, "pub", False)
                        and isinstance(getattr(m, "name", None), str)
                    ]
                    if block_pub or member_pub:
                        seeds.append(d)
                        for member in (*d.fns, *d.statics):
                            member_name = getattr(member, "name", None)
                            if (
                                isinstance(member_name, str) and member_name
                                and (block_pub or getattr(member, "pub", False))
                            ):
                                exported_set.add(member_name)
                    continue
                name = self._declaration_name(d)
                if name is None:
                    continue
                is_block = isinstance(d, (ExtraDecl, ImplDecl))
                # todo-119: pub(std) items outside the importer's module
                # root are not part of this module's API for it.
                if not is_block and (
                    not getattr(d, "pub", False)
                    or not self._visibility_admits(d)
                ):
                    continue
                # Items that only ride along on someone's plain ``use``
                # belong to this module's compile surface, not its API.
                if name in transitive_only and name not in pub_names:
                    continue
                # extra/impl blocks extend a type; load them only when the
                # owning type is actually part of the public API.
                if is_block and name not in pub_names:
                    continue
                seeds.append(d)
                exported_set.add(name)
            # A facade file may be nothing but ``pub use`` statements
            # (e.g. a package ``lib.wd``): its re-exports are its whole
            # public API, so their already-resolved declarations join the
            # compile surface directly.
            for u in uses:
                if not u.pub:
                    continue
                seeds.extend(getattr(u, "loaded_items", []))
                exported_set |= self._pub_use_surface(u)
            exported = frozenset(exported_set)

        order: list[Node] = []
        queued: set[int] = set()

        def enqueue(target: Node) -> None:
            if id(target) not in queued:
                queued.add(id(target))
                order.append(target)

        for seed in seeds:
            enqueue(seed)
        index = 0
        while index < len(order):
            current = order[index]
            index += 1
            for ref in sorted(_referenced_names(current)):
                for candidate in by_name.get(ref, ()):
                    enqueue(candidate)
                for dependency in dep_items.get(ref, ()):
                    enqueue(dependency)
                for dependency in alias_items.get(ref, ()):
                    enqueue(dependency)

        # Pulled impl modules may themselves import other traits (or
        # reference types from this module's tables); one more closure
        # pass keeps the shared ``enqueue``/``order`` graph complete.
        # NOTE: the pull itself runs ONLY at the entry level
        # (``_pull_trait_impls``, called from ``parse_program`` after the
        # auto-prelude merge).  Doing it inside every ``_select_module_items``
        # call would recurse into the registry builder through the child
        # parses it spawns and blow up quadratically (bug-61 debugging).
        _localize_qualified_refs(order, alias_items, self._declaration_name)

        # todo-79: module scope table -------------------------------------
        # Items already flattened through another surface keep their final
        # names and are skipped here (they are already part of the root
        # program).  Everything else receives its final name now: items in
        # their home module's export surface stay bare, everything else
        # (private helpers, transitive compile-only dependencies are pub in
        # their own home and therefore keep their names -- SA gates them by
        # visibility instead) is mangled with a hash of the home file so
        # same-named privates of different modules cannot collide.  All
        # references inside freshly selected bodies are rewritten to the
        # final names afterwards.
        suffix = getattr(loaded, "_scope_suffix", None)
        if suffix is None:
            home0 = next(
                (
                    getattr(n, "source_module", None)
                    for n in order
                    if getattr(n, "source_module", None)
                ),
                None,
            )
            suffix = (
                _module_mangle_suffix(home0) if home0
                else format(id(loaded) & 0xFFFFFFFF, "08x")
            )
            loaded._scope_suffix = suffix  # type: ignore[attr-defined]
        accumulated: dict[str, str] = dict(
            getattr(loaded, "_scope_rename_map", {})
        )
        mapping = dict(accumulated)
        # Renaming runs exactly once per node (the ``_scope_flat`` guard);
        # every node in *order* is still returned so it lands in *this*
        # program too -- the same node instance may already have been
        # flattened into an imported module's own program earlier.
        fresh: list[Node] = []
        for node in order:
            if getattr(node, "_scope_flat", False):
                continue
            name = _declared_name_field(node)
            if name is not None and not getattr(node, "pub", False):
                final = f"{name}__{suffix}"
                if final != name:
                    node._scope_orig = name  # type: ignore[attr-defined]
                    _set_declared_name(node, final)
                    mapping[name] = final
            node._scope_flat = True  # type: ignore[attr-defined]
            fresh.append(node)
        if fresh:
            for key, value in mapping.items():
                if accumulated.get(key) != value:
                    accumulated[key] = value
            loaded._scope_rename_map = accumulated  # type: ignore[attr-defined]
            for node in fresh:
                _rewrite_module_refs(node, mapping, frozenset())
        return order, exported, frozenset(local_names)

    def _append_unique(self, items: list[Node], additions: list[Node]) -> None:
        seen = {id(node) for node in items}
        for node in additions:
            if id(node) not in seen:
                items.append(node)
                seen.add(id(node))

    def _tag_source_module(self, item: Node) -> None:
        """todo-90: record the file that declared this top-level item.

        ``source_module`` is a plain runtime attribute (never a dataclass
        field) so typed-AST serialization stays untouched.  Items parsed
        without a known source path (tests / stdin) stay untagged and keep
        the legacy permissive behavior for field visibility.

        todo-144: ``source_module_path`` additionally carries the
        *canonical* dotted module path (``std::baseimpl::file`` for
        ``libs/baseimpl/file.wind``), the definition-site half of the
        typed-AST FQN normalization.  Files outside every module root
        (entry sources) stay ``None`` — their types are unambiguous inside
        their own artifact.
        """
        source = getattr(self, "source_path", None)
        if source:
            item.source_module = source
            item.source_module_path = self._canonical_module_parts(source)

    def _canonical_module_parts(self, source: str) -> Optional[list[str]]:
        """todo-144: dotted import-path parts of ``source`` under a root.

        ``libs`` roots spell the ``std`` virtual namespace, so their files
        gain a ``std`` head (``libs/option.wind`` -> ``std::option``);
        package source roots (todo-71/97) address their files bare.  Returns
        ``None`` when the file lives outside every root.
        """
        cached = self._canonical_parts_cache
        if source in cached:
            return cached[source]
        parts: Optional[list[str]] = None
        path = Path(source)
        for root in _module_roots(
            getattr(self, "_IMPORT_ROOTS_BASE", Path.cwd())
        ):
            try:
                rel = path.relative_to(root.directory)
            except ValueError:
                continue
            if root.entry is not None and rel == Path(root.entry.name):
                # The root module file itself (lib.wd / mod.wind): the
                # empty path — the crate root module lives here.
                parts = (
                    ["std"] if root.kind == "std" else []
                )
                break
            rel_parts = _module_parts(rel, root.entry)
            if rel_parts is None:
                continue
            parts = (
                ["std", *rel_parts] if root.kind == "std" else list(rel_parts)
            )
            break
        self._canonical_parts_cache[source] = parts
        return parts

    def _parse_auto_prelude(self) -> list[UseDecl]:
        """Resolve the entry file's implicit wildcard imports (todo-76/97).

        Two layers, bottom to top in the final program:

        1. ``std::prelude::*`` — the language prelude from the project's
           ``libs`` tree (skipped when absent);
        2. the package's own library facade (``lib.wd``, todo-97) — its
           public API becomes visible to ``main`` without an explicit
           ``use``.

        A project may lack either; failures are recorded and that layer is
        skipped.  Results are memoized so every root parser for the same
        project shares the loaded modules without reparsing them.
        """
        if not self._is_root_source():
            return []
        result = self._auto_prelude_result
        if result is not _NO_PRELUDE_SENTINEL:
            return cast(list[UseDecl], result)
        decls: list[UseDecl] = []
        std_decl = self._resolve_auto_std_prelude()
        if std_decl is not None:
            decls.append(std_decl)
        pkg = self._package_lib
        if pkg is not None:
            parts, lib_path = pkg
            decl = UseDecl(
                1,
                1,
                list(parts),
                wildcard=True,
                item=None,
                auto=True,
            )
            try:
                decl.module = str(Path(lib_path).resolve())
                self.current_use_decl = decl
                loaded = self._load_module(Path(lib_path).resolve(), None)
                (
                    decl.loaded_items,
                    decl.exported_names,
                    decl.known_names,
                ) = self._select_module_items(
                    loaded,
                    item=None,
                    line=decl.line,
                    column=decl.column,
                )
                decls.append(decl)
            except ParseError as exc:
                self.errors.append(exc)
            finally:
                self.current_use_decl = None
        self._auto_prelude_result = [
            decl for decl in decls if decl is not None
        ]
        return cast(list[UseDecl], self._auto_prelude_result)

    def _resolve_auto_std_prelude(self) -> Optional[UseDecl]:
        """Build the implicit ``std::*`` import, or ``None``.

        todo-158: ``std::prelude`` is gone — the std root module
        (``libs/mod.wind``) *is* the prelude.  The implicit wildcard rides
        the root module's export surface; ``_auto_shadow_names`` keys on
        the ``parts == ["std"]`` shape now.
        """
        decl = UseDecl(
            1,
            1,
            ["std"],
            wildcard=True,
            item=None,
            auto=True,
        )
        try:
            resolved = self._resolve_module_path(
                decl.parts,
                wildcard=True,
                line=decl.line,
                column=decl.column,
            )
        except (OSError, ValueError, ParseError):
            # No root module (libs/mod.wind absent): no prelude layer.
            return None
        if resolved is None:
            return None
        module_path, item_name = resolved
        del item_name  # wildcard imports never select one item
        try:
            decl.module = str(module_path.resolve())
            self.current_use_decl = decl
            loaded = self._load_module(module_path, None)
            (
                decl.loaded_items,
                decl.exported_names,
                decl.known_names,
            ) = self._select_module_items(
                loaded,
                item=None,
                line=decl.line,
                column=decl.column,
            )
        except ParseError as exc:
            self.errors.append(exc)
            self.current_use_decl = None
            return None
        finally:
            self.current_use_decl = None
        return decl

    def _is_root_source(self) -> bool:
        return not self._loading and getattr(self, "_is_entry_source", False)

    def _parse_use(self, use_tok: Token, *, pub: bool = False) -> list[UseDecl]:
        """Parse and recursively load ``use a::b;``.

        todo-112: the grouped form ``use a::b::{c, d};`` expands at parse
        time into one import per unique group element, so resolution / SA /
        flattening see exactly what hand-written ``use a::b::c;`` +
        ``use a::b::d;`` would produce.  Plain and wildcard forms yield a
        single-element list.  Each declaration stays in the importing
        module's AST for provenance while loaded declarations are flattened
        into the root program (single-program model preserved).
        """
        parts: list[str] = []
        wildcard = False
        # todo-112: tokens of a trailing ``::{a, b}`` group (None when this
        # is not a grouped import); positions kept for diagnostics.
        # todo-125: each element carries an optional ``as`` rename token,
        # so ``use a::b::{c as d, e}`` selects ``c`` under ``d`` and ``e``
        # under its own name.
        group: Optional[list[tuple[Token, Optional[Token]]]] = None
        while True:
            if self._match(TokenKind.STAR) is not None:
                wildcard = True
                break
            name = self._expect(TokenKind.IDENTIFIER, what="module name or '*'")
            parts.append(str(name.value))
            if self._match(TokenKind.PATH) is None:
                break
            if self._at(TokenKind.LBRACE):
                # ``a::b::{x, y}``: this ``::`` introduced an item group.
                group = self._parse_use_group()
                break

        nxt = self._peek()
        if (
            group is None
            and not wildcard
            and nxt is not None
            and nxt.kind == TokenKind.LBRACE
        ):
            raise ParseError(
                "'{' starts an import group but no '::' precedes it "
                "(for example 'std::ctypedef::{c_float, c_char}')",
                nxt.line,
                nxt.column,
                category="import syntax",
            )
        # todo-124: trailing ``as`` rename of the whole import.  Plain module
        # imports register their namespace under the alias; item imports
        # rewrite bare references to the aliased item.  Wildcard forms have
        # no single name to rename, so they fail loudly here; grouped forms
        # carry their renames per element (todo-125), so a trailing ``as``
        # stays an error.
        alias_tok: Optional[Token] = None
        if self._match(TokenKind.AS) is not None:
            if wildcard:
                raise ParseError(
                    "'as' cannot rename a wildcard import "
                    "('use m::*;' already imports every name bare)",
                    self._peek().line if self._peek() else use_tok.line,
                    self._peek().column if self._peek() else use_tok.column,
                    category="import syntax",
                )
            if group is not None:
                raise ParseError(
                    "grouped imports cannot be renamed with 'as'; "
                    "import the item on its own instead "
                    "(for example 'use a::b::c as d;')",
                    self._peek().line if self._peek() else use_tok.line,
                    self._peek().column if self._peek() else use_tok.column,
                    category="import syntax",
                )
            alias_tok = self._expect(
                TokenKind.IDENTIFIER, what="alias name after 'as'"
            )
        self._expect(TokenKind.SEMICOLON, what="';' after use declaration")

        if group is None:
            return [
                self._finish_use(
                    use_tok,
                    parts,
                    wildcard=wildcard,
                    pub=pub,
                    alias=alias_tok,
                )
            ]
        decls: list[UseDecl] = []
        seen_elements: set[tuple[str, Optional[str]]] = set()
        for el_tok, el_alias in group:
            el_name = str(el_tok.value)
            alias_name = str(el_alias.value) if el_alias is not None else None
            # Duplicates collapse into one selection: node-level dedup hides
            # double loads anyway and provenance rows stay tidy.  todo-125:
            # an alias is part of the element's identity, so ``{c as d, c}``
            # selects the item twice under two names.
            if (el_name, alias_name) in seen_elements:
                continue
            seen_elements.add((el_name, alias_name))
            decls.append(
                self._finish_use(
                    use_tok,
                    [*parts, el_name],
                    wildcard=False,
                    pub=pub,
                    line=el_tok.line,
                    column=el_tok.column,
                    alias=el_alias,
                )
            )
        return decls

    def _parse_use_group(self) -> list[tuple[Token, Optional[Token]]]:
        """todo-112: parse the ``{...}`` item list of a grouped import.

        Grammar::

            group := '{' (member (',' member)* ','?)? '}'
            member := IDENTIFIER [ 'as' IDENTIFIER ]   (todo-125)

        Trailing commas are allowed.  Elements are plain identifiers, later
        resolved against the path prefix by the caller -- either an exported
        item or a nested module.  An ``as`` rename rides on its element and
        behaves exactly like the flat ``use a::b::c as d;`` form.  Nested
        paths/groups and ``*`` inside the braces fail loudly here instead of
        confusing downstream stages.
        """
        open_tok = self._advance()
        assert open_tok is not None and open_tok.kind == TokenKind.LBRACE
        elements: list[tuple[Token, Optional[Token]]] = []
        while True:
            tok = self._peek()
            if tok is None:
                raise ParseError(
                    "unterminated import group ('}' expected)",
                    open_tok.line,
                    open_tok.column,
                    category="import syntax",
                )
            if tok.kind == TokenKind.RBRACE:
                if not elements:
                    raise ParseError(
                        "empty import group",
                        open_tok.line,
                        open_tok.column,
                        category="import syntax",
                    )
                self._advance()
                return elements
            if tok.kind == TokenKind.STAR:
                raise ParseError(
                    "'*' cannot appear inside an import group "
                    "(the wildcard form is 'path::*')",
                    tok.line,
                    tok.column,
                    category="import syntax",
                )
            if tok.kind == TokenKind.PATH:
                raise ParseError(
                    "nested paths inside an import group are not supported",
                    tok.line,
                    tok.column,
                    category="import syntax",
                )
            el = self._expect(TokenKind.IDENTIFIER, what="import name or '}'")
            # todo-125: per-element rename, ``{c as d}`` selects ``c``
            # under ``d`` exactly like the flat ``use a::b::c as d;``.
            alias: Optional[Token] = None
            if self._match(TokenKind.AS) is not None:
                alias = self._expect(
                    TokenKind.IDENTIFIER, what="alias name after 'as'"
                )
            elements.append((el, alias))
            follow = self._peek()
            if follow is not None and follow.kind == TokenKind.PATH:
                raise ParseError(
                    "nested paths inside an import group are not supported",
                    follow.line,
                    follow.column,
                    category="import syntax",
                )
            if self._match(TokenKind.COMMA) is None:
                closer = self._peek()
                if closer is not None and closer.kind != TokenKind.RBRACE:
                    raise ParseError(
                        "expected ',' or '}' after group member",
                        closer.line,
                        closer.column,
                        category="import syntax",
                    )
                if closer is None:
                    raise ParseError(
                        "expected ',' or '}' after group member",
                        el.end_line,
                        el.end_column,
                        category="import syntax",
                    )

    def _finish_use(
        self,
        use_tok: Token,
        parts: list[str],
        *,
        wildcard: bool,
        pub: bool,
        line: Optional[int] = None,
        column: Optional[int] = None,
        alias: Optional[Token] = None,
    ) -> UseDecl:
        """Resolve one import selector and load its target module.

        todo-112 extraction: ``_parse_use`` may synthesize several selectors
        (one per group element); they all share this body.  Errors anchor to
        the selector's own position so grouped imports report the offending
        element instead of the statement start.
        ``alias`` (todo-124) carries the ``as`` rename token when present.
        """
        # The tree metadata doubles as the diagnostic anchor: selectors
        # synthesized from group elements pass their own token position,
        # plain imports fall back to the ``use`` keyword itself.
        anchor_line = line if line is not None else use_tok.line
        anchor_column = column if column is not None else use_tok.column

        try:
            # A terminal ``*`` must be a wildcard selector.  A bare ``use *;``
            # has no module namespace and is rejected before path resolution.
            # A star in the middle of a path is a grammar error, not an
            # unknown-module error.
            if wildcard:
                if len(parts) < 1:
                    raise ParseError(
                        "wildcard import requires a module path "
                        "(for example 'std::prelude::*')",
                        anchor_line,
                        anchor_column,
                    )
            elif any(part == "*" for part in parts):
                raise ParseError(
                    "'*' may appear only as the final item of an import",
                    anchor_line,
                    anchor_column,
                )

            resolved = self._resolve_module_path(
                # A terminal ``*`` never replaces a path segment: the module
                # path is every named part, and ``*`` selects all exports.
                parts,
                wildcard=wildcard,
                line=anchor_line,
                column=anchor_column,
            )
        except ParseError:
            raise
        except (OSError, ValueError) as exc:
            message = str(exc)
            category = "module resolution failed"
            if isinstance(exc, ValueError):
                category = "ambiguous module"
            raise ParseError(
                message,
                anchor_line,
                anchor_column,
                end_line=anchor_line,
                end_column=anchor_column,
                category=category,
            ) from exc

        if resolved is None:
            raise ParseError(
                f"cannot find module '{'::'.join(parts)}' "
                f"(searched {self._import_root() / 'libs'})",
                anchor_line,
                anchor_column,
                end_line=anchor_line,
                end_column=anchor_column,
                category="unknown module",
            )

        module_path, item_name = resolved
        decl = UseDecl(
            anchor_line,
            anchor_column,
            parts,
            wildcard=wildcard,
            pub=pub,
        )
        decl.item = item_name
        if alias is not None:
            decl.alias = str(alias.value)
        if wildcard and item_name is not None:
            raise ParseError(
                f"cannot resolve import path '{'::'.join(parts)}'",
                anchor_line,
                anchor_column,
            )
        try:
            decl.module = str(module_path.resolve())
            self.current_use_decl = decl
            loaded = self._load_module(module_path, use_tok)
            (
                decl.loaded_items,
                decl.exported_names,
                decl.known_names,
            ) = self._select_module_items(
                loaded,
                item=item_name,
                line=anchor_line,
                column=anchor_column,
            )
        finally:
            self.current_use_decl = None
        return decl

    def _at_export_crate(self) -> bool:
        """todo-126: lookahead for ``[pub] export crate <name> [as <alias>];``.

        ``export`` and ``crate`` are ordinary identifiers (neither is a
        keyword), so the pair only reads as an extern-crate import when it
        appears in this exact order at the top level; any other use of a
        binding named ``export`` falls through to normal parsing.
        """
        def is_word(offset: int, word: str) -> bool:
            tok = self._peek(offset)
            return (
                tok is not None
                and tok.kind == TokenKind.IDENTIFIER
                and str(tok.value) == word
            )

        base = 1 if self._at(TokenKind.PUB) else 0
        return is_word(base, "export") and is_word(base + 1, "crate")

    def _parse_export_crate(self, export_tok: Token, *, pub: bool) -> UseDecl:
        """Parse ``[pub] export crate <name> [as <alias>];`` (todo-126).

        CWind's take on Rust 2015 ``extern crate``: bind an entire top-level
        crate (a ``libs/<name>`` module tree) under one name in this file.
        It is deliberately a *restricted* module import -- the crate name
        must be a lone identifier, with no ``::`` path, item group or ``*``
        wildcard -- and it rides the same ``use`` machinery, so
        ``export crate foo;`` is exactly ``use foo;`` plus a ``crate_export``
        provenance flag that SA and the import manifest use to tell the two
        forms apart.
        """
        # ``export_tok`` is the ``export`` identifier the caller peeked but
        # did not consume; this method owns advancing past both words.
        export = self._advance()  # export
        crate = self._advance()   # crate
        assert (
            export.kind == TokenKind.IDENTIFIER
            and str(export.value) == "export"
            and crate.kind == TokenKind.IDENTIFIER
            and str(crate.value) == "crate"
        )
        reserved = set(self._PATH_HEAD_KEYWORDS) | {"std"}

        name_tok = self._expect(
            TokenKind.IDENTIFIER, what="crate name after 'export crate'"
        )
        name = str(name_tok.value)
        if name in reserved:
            raise ParseError(
                f"'{name}' cannot name an exported crate "
                "(it is a reserved import path head)",
                name_tok.line,
                name_tok.column,
                category="import syntax",
            )

        alias_tok: Optional[Token] = None
        if self._match(TokenKind.AS) is not None:
            alias_tok = self._expect(
                TokenKind.IDENTIFIER, what="alias name after 'as'"
            )
            if str(alias_tok.value) in reserved:
                raise ParseError(
                    f"'{alias_tok.value}' cannot be used as a crate alias "
                    "(it is a reserved import path head)",
                    alias_tok.line,
                    alias_tok.column,
                    category="import syntax",
                )

        # Only a bare crate name (plus optional ``as``) is allowed here; any
        # other trailing token means the user wanted the general ``use`` form.
        nxt = self._peek()
        if nxt is not None and nxt.kind != TokenKind.SEMICOLON:
            raise ParseError(
                "'export crate' binds a whole crate by name; "
                "use 'use' for module paths, groups and wildcards "
                "(for example 'use foo::bar;')",
                nxt.line,
                nxt.column,
                category="import syntax",
            )
        self._expect(
            TokenKind.SEMICOLON, what="';' after export crate declaration"
        )

        decl = self._finish_use(
            export_tok, [name], wildcard=False, pub=pub, alias=alias_tok
        )
        decl.crate_export = True  # type: ignore[attr-defined]
        return decl

    def _import_root(self) -> Path:
        """Project root used for library lookup.

        It is fixed once by the entry file (or explicitly by tests/tools).
        Imported files deliberately do not re-anchor it to their own
        directory: otherwise nested std modules would look for sibling
        libraries under ``libs/libs``.
        """
        explicit = getattr(self, "_IMPORT_ROOTS_BASE", None)
        if explicit is not None:
            return Path(explicit).resolve()
        source = getattr(self, "source_path", None)
        if source:
            base = Path(source).resolve().parent
            return base.parent if base.name == "libs" else base
        return Path.cwd().resolve()

    def _current_module_parts(self) -> Optional[list[str]]:
        """todo-119: this file's own module path inside its module tree.

        Computed from ``source_path`` relative to whichever module root
        (``libs/`` or a Breeze source tree) contains the file, using the
        same ``mod``-file rules as trie registration.  ``None`` means the
        file lives outside every module root (bare single-file mode), where
        ``self`` / ``super`` have no meaning.
        """
        source = getattr(self, "source_path", None)
        if not source:
            return None
        path = Path(source).resolve()
        base = self._import_root()
        for root in _module_roots(base):
            try:
                rel = path.relative_to(root.directory)
            except ValueError:
                continue
            return _module_parts(rel, root.entry)
        return None

    def _current_root_kind(self) -> str:
        """The import-root kind ("std"/"crate") the current file lives in.

        ``super`` / ``self`` heads anchor against the tree the file itself
        belongs to: a file under ``libs/`` resolves siblings on the std
        tree, a package source file on the crate tree.
        """
        source = getattr(self, "source_path", None)
        if not source:
            return "std"
        path = Path(source).resolve()
        for root in _module_roots(self._import_root()):
            try:
                path.relative_to(root.directory)
            except ValueError:
                continue
            return root.kind
        return "std"

    _PATH_HEAD_KEYWORDS = ("crate", "super", "self")

    def _resolve_head_keyword(
        self,
        parts: list[str],
        line: int,
        column: int,
    ) -> list[str]:
        """todo-119: expand ``crate`` / ``super`` / ``self`` path heads.

        - ``crate::a::b`` resolves from the module-tree root (explicit
          spelling of the default bare-path behavior);
        - ``self::a`` anchors at the current file's own module path
          (``libs/a/b/util.wind`` + ``use self::c;`` -> ``a::b::util::c``);
        - ``super::a`` anchors at the parent module (the path minus its
          last segment), the Rust sibling-module form;
        - ``std`` keeps its virtual-namespace handling and the default is
          the bare path.

        The keywords may only start a path; elsewhere they are ordinary
        identifiers.  Files outside any module root cannot use
        ``self`` / ``super`` (nothing to anchor to).
        """
        head = parts[0]
        if head not in self._PATH_HEAD_KEYWORDS:
            stripped = parts[1:] if head == "std" else list(parts)
            scan = stripped if head == "std" else stripped[1:]
            for seg in scan:
                if seg in self._PATH_HEAD_KEYWORDS:
                    raise ParseError(
                        f"'{seg}' may only appear at the start of an "
                        "import path",
                        line,
                        column,
                    )
            return stripped
        tail = parts[1:]
        for seg in tail:
            if seg in self._PATH_HEAD_KEYWORDS:
                raise ParseError(
                    f"'{seg}' may only appear at the start of an import path",
                    line,
                    column,
                )
        if head == "crate":
            if not tail:
                raise ParseError(
                    "'crate' must be followed by a module path "
                    "(for example 'crate::modules::great')",
                    line,
                    column,
                )
            return tail
        current = self._current_module_parts()
        if current is None:
            raise ParseError(
                f"'{head}' requires the file to live inside a module tree "
                "(a 'libs/' directory or a Breeze source root)",
                line,
                column,
            )
        if head == "self":
            if not tail:
                raise ParseError(
                    "'self' must be followed by a module path "
                    "(for example 'self::util')",
                    line,
                    column,
                )
            return [*current, *tail]
        # super
        if not current:
            raise ParseError(
                "'super' has no parent module here (the file already sits "
                "at its module tree's root)",
                line,
                column,
            )
        return [*current[:-1], *tail]

    def _check_module_visibility(
        self,
        tree: ModuleTrieNode,
        lookup_parts: list[str],
        line: int,
        column: int,
    ) -> None:
        """todo-107: private ``mod`` declarations gate cross-tree addressing.

        A module registered through ``mod foo;`` (no ``pub``) is addressable
        only from files living inside that same subtree (the declaring
        module's file and its descendants — Rust's module privacy).  A
        ``pub mod foo;`` re-export is addressable from anywhere.
        """
        node = tree
        importer = self._current_module_parts()
        if importer is not None:
            # The importer must live *on the declaration chain*: a wild
            # file under the root (the binary entry ``main.wd`` next to the
            # crate root ``lib.wd``) is a separate compilation unit and
            # never counts as "inside" a private module's subtree —
            # Rust's bin-crate/lib-crate split.
            probe = tree
            for seg in importer:
                probe = probe.children.get(seg)
                if probe is None or probe.entry is None:
                    importer = None
                    break
        for depth, part in enumerate(lookup_parts, 1):
            nxt = node.children.get(part)
            if nxt is None:
                return
            node = nxt
            if node.pub or node.entry is None:
                continue
            # Inside the declaring module's subtree: a private ``mod``
            # declared in module M is visible to M and every descendant of
            # M — so the importer's path must share M's segments (the
            # lookup path minus the segment being checked).  Files outside
            # every module root (entry sources) are never inside.
            if (
                importer is not None
                and importer[:depth - 1] == lookup_parts[:depth - 1]
            ):
                continue
            raise ParseError(
                f"module '{'::'.join(lookup_parts[:depth])}' is private "
                "(declare it 'pub mod' in its parent's mod.wind to "
                "re-export it)",
                line,
                column,
                category="private module",
            )

    def _resolve_module_path(
        self,
        parts: list[str],
        *,
        wildcard: bool,
        line: int,
        column: int,
    ) -> Optional[tuple[Path, Optional[str]]]:
        """Resolve one import using longest-prefix trie matching.

        Returns ``(module_file, item_name)``.  ``item_name`` is non-None only
        when trailing segments identify a public declaration inside that
        module.  ``std`` rides the libs tree, ``crate``/``super``/``self``
        the crate tree (a Breeze package source root); bare paths try the
        crate tree first, then std (the prelude is std-only).
        todo-119: ``crate`` / ``super`` / ``self`` heads anchor the path
        explicitly (crate root / parent module / current module).
        """
        if not parts:
            if wildcard:
                return None
            raise ParseError(
                "import requires a module path",
                line,
                column,
                category="empty import",
            )
        try:
            lookup_parts = self._resolve_head_keyword(parts, line, column)
        except ParseError:
            raise
        tree = _library_tree(self._import_root())
        head = parts[0]
        # Tree selection: ``std`` always anchors the libs tree;
        # ``crate``/``super``/``self`` anchor the tree the current file
        # itself belongs to (libs-only projects: the std root — the
        # todo-119 semantics; Breeze packages: the crate tree).  A bare
        # name tries the crate tree first (user modules shadow std), then
        # std.
        if head == "std":
            scoped: Optional[ModuleTrieNode] = tree.std
        elif head in ("crate", "super", "self"):
            scoped = (
                tree.crate
                if self._current_root_kind() == "crate"
                else tree.std
            )
        else:
            scoped = None
        if not lookup_parts:
            # Bare ``std`` / ``crate::*``: the trie root IS the root module
            # (``libs/mod.wind`` — the prelude — or the crate's ``lib.wd``).
            # A tree without a root module exposes nothing; explicit imports
            # get a precise error (the implicit auto prelude swallows it).
            candidates = (
                [scoped] if scoped is not None
                else [tree.crate, tree.std]
            )
            for candidate in candidates:
                if candidate.entry is not None:
                    return candidate.entry, None
            raise ParseError(
                f"cannot find module '{parts[0]}' (no module tree under "
                f"{self._import_root() / 'libs'})",
                line,
                column,
                category="unknown module",
            )
        if scoped is not None:
            remaining, entry_path = scoped.find_longest(lookup_parts)
            if (
                entry_path is None
                and head == "crate"
                and scoped.entry is not None
                and lookup_parts
            ):
                # ``crate::item``: the crate root module's own declarations
                # (lib.wd's fns/consts/types) live in the root entry file —
                # resolve to it with the full path as the item selector.
                entry_path = scoped.entry
                remaining = list(lookup_parts)
            elif entry_path is None:
                remaining, entry_path = list(lookup_parts), None
            if entry_path is not None:
                self._check_module_visibility(
                    scoped, lookup_parts, line, column
                )
        else:
            remaining, entry_path = tree.resolve(lookup_parts)
            if entry_path is not None:
                crate_hit = tree.crate.find_longest(lookup_parts)[1]
                self._check_module_visibility(
                    tree.crate if crate_hit is not None else tree.std,
                    lookup_parts,
                    line,
                    column,
                )
        if entry_path is None or wildcard:
            return None if entry_path is None else (entry_path, None)
        if not remaining:
            return entry_path, None
        if len(remaining) > 1:
            raise ParseError(
                f"cannot resolve import path '{'::'.join(parts)}'",
                line,
                column,
                category="unknown module",
            )
        return entry_path, remaining[0]

    def _load_module(self, path: Path, use_tok: Optional[Token]) -> Program:
        key = str(path.resolve())
        if key in self._module_cache:
            return self._module_cache[key]
        if key in self._loading:
            chain = " -> ".join(self._loading + [key])
            raise ParseError(
                f"recursive module import: {chain}",
                use_tok.line if use_tok is not None else 1,
                use_tok.column if use_tok is not None else 1,
                category="import cycle",
            )
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise ParseError(
                f"cannot read module '{path}': {exc.strerror or exc}",
                use_tok.line if use_tok is not None else 1,
                use_tok.column if use_tok is not None else 1,
                category="unreadable module",
            ) from exc
        try:
            tokens = tokenize(text)
        except Exception as exc:
            raise ParseError(
                f"lexical error in imported module '{path}'",
                use_tok.line if use_tok is not None else 1,
                use_tok.column if use_tok is not None else 1,
            ) from exc
        child = Parser(tokens)
        child.source_path = str(path.resolve())
        child._IMPORT_ROOTS_BASE = self._IMPORT_ROOTS_BASE
        # Imported modules evaluate #[cfg] against the same target.
        child._cfg_target_os = self._cfg_target_os
        child._cfg_target_arch = self._cfg_target_arch
        child._cfg_target_vendor = self._cfg_target_vendor
        child._cfg_pointer_width = self._cfg_pointer_width
        child._cfg_ctx = self._cfg_ctx
        # Imported modules do not inject their own prelude.  They resolve
        # their explicit dependencies against the entry project root.
        child._module_cache = self._module_cache
        child._module_order = self._module_order
        child._loading = [*self._loading, key]
        child.import_errors = self.import_errors
        program = child.parse_program()
        # bug-61: file identity for the trait-impl pull (same-module impls
        # are reached through by_name; only foreign files get pulled in).
        program._registry_home = key
        self._module_cache[key] = program
        self._module_order.append(key)
        # bug-36: 模块内报的错必须归属到模块文件本身, 否则入口文件
        # 渲染时按入口文本取位置, 得到毫无关联的奇怪报错
        module_source = str(path.resolve())
        for e in child.errors:
            if e.source is None:
                e.source = module_source
        for e in child.import_errors:
            if e.source is None:
                e.source = module_source
        self.errors.extend(child.errors)
        self.import_errors.extend(child.import_errors)
        return program

    # -- attributes ----------------------------------------------------------

    _LINK_ATTR_ARGS = ("name", "kind", "path", "relative")
    _LINK_RELATIVE_MODES = ("cwd", "source")

    def _cfg_context(self) -> CfgContext:
        """Compile-time configuration for ``#[cfg]`` evaluation (todo-86/93),
        lazily built from the explicit ``--target-os`` value or host
        auto-detection."""
        if self._cfg_ctx is None:
            self._cfg_ctx = CfgContext(
                self._cfg_target_os,
                self._cfg_target_arch,
                self._cfg_target_vendor,
                self._cfg_pointer_width,
            )
        return self._cfg_ctx

    def _parse_cfg_predicate(self) -> CfgPredicate:
        """Parse one ``#[cfg(...)]`` predicate (todo-86/93).

        Grammar::

            predicate := flag
                       | key '=' string          (e.g. target_os = "windows")
                       | ident '(' [predicate {',' predicate}] ')'

        Only ``all`` / ``any`` / ``not`` may appear in call position;
        ``not`` requires exactly one argument while empty ``all``/``any``
        follow Rust semantics (true/false).  Unknown flags, keys or values
        are reported here so a typo cannot silently change what compiles.
        """
        def fail(message: str, tok: Token) -> NoReturn:
            raise ParseError(
                f"#cfg: {message}",
                tok.line,
                tok.column,
                end_line=tok.end_line,
                end_column=tok.end_column,
            )

        tok = self._expect(TokenKind.IDENTIFIER, what="a cfg predicate")
        name = str(tok.value)
        if name in CFG_KEYS and self._at(TokenKind.LPAREN):
            fail(
                f"'{name}' expects = \"value\", not a predicate call",
                tok,
            )
        if self._match(TokenKind.ASSIGN) is not None:
            val_tok = self._expect(
                TokenKind.STRING,
                what='a quoted string value after \'=\' in the cfg predicate',
            )
            if name not in CFG_KEYS:
                fail(
                    f"unknown cfg key '{name}' "
                    f"(supported keys: {', '.join(CFG_KEYS)})",
                    tok,
                )
            value = str(val_tok.value)
            allowed = CFG_KEY_VALUES[name]
            if value not in allowed:
                fail(
                    f"invalid '{name}' value '{value}' "
                    f"(expected one of: {', '.join(allowed)})",
                    val_tok,
                )
            return CfgPredicate("kv", name=name, value=value)
        if self._match(TokenKind.LPAREN) is not None:
            if name not in CFG_COMBINATORS:
                fail(
                    f"'{name}' is not a valid cfg combinator "
                    f"(expected {', '.join(CFG_COMBINATORS)})",
                    tok,
                )
            args: list[CfgPredicate] = []
            while not self._at(TokenKind.RPAREN):
                args.append(self._parse_cfg_predicate())
                if self._match(TokenKind.COMMA) is None:
                    break
            self._expect(
                TokenKind.RPAREN,
                what="')' to close the cfg combinator",
            )
            if name == "not" and len(args) != 1:
                fail("the 'not' cfg predicate expects exactly one argument", tok)
            return CfgPredicate(name, args=tuple(args))
        if name not in CFG_FLAGS:
            fail(
                f"unknown cfg flag '{name}' "
                f"(expected a bare flag ({', '.join(CFG_FLAGS)}), "
                f"a combinator, or key = \"value\")",
                tok,
            )
        return CfgPredicate("flag", name=name)

    def _parse_attributes(self) -> list[tuple[str, object, int, int]]:
        """Collect leading ``#[...]`` attribute tokens.

        Returns ``(name, payload, line, column)`` tuples where ``payload``
        maps argument names to their string values; the paren-less
        shorthand ``#[name = "value"]`` (todo-62) stores its value under the
        empty key.  ``cfg`` (todo-86/93) carries a parsed :class:`CfgPredicate`
        tree instead of an argument dict.  Unknown attribute names or
        non-string values are reported as parse errors.
        """
        attrs: list[tuple[str, object, int, int]] = []
        while self._at(TokenKind.HASH):
            hash_tok = self._advance()  # #
            try:
                self._expect(
                    TokenKind.LBRACKET, what="'[' to open an attribute"
                )
                name_tok = self._expect(
                    TokenKind.IDENTIFIER, what="attribute name"
                )
                name = str(name_tok.value)
                if name == "cfg":
                    # todo-86/93: nested predicate grammar instead of the
                    # flat key = "value" argument list.
                    self._expect(
                        TokenKind.LPAREN,
                        what="'(' to open the 'cfg' predicate",
                    )
                    pred = self._parse_cfg_predicate()
                    self._expect(
                        TokenKind.RPAREN,
                        what="')' to close the 'cfg' predicate",
                    )
                    attrs.append((name, pred, hash_tok.line, hash_tok.column))
                else:
                    args: dict[str, str] = {}
                    if self._match(TokenKind.LPAREN) is not None:
                        while not self._at(TokenKind.RPAREN):
                            key_tok = self._expect(
                                TokenKind.IDENTIFIER,
                                what="an attribute argument name",
                            )
                            key = str(key_tok.value)
                            self._expect(
                                TokenKind.ASSIGN,
                                what="'=' after an attribute argument name",
                            )
                            val_tok = self._expect(
                                TokenKind.STRING,
                                what="a string literal attribute value",
                            )
                            if key in args:
                                raise ParseError(
                                    f"duplicate attribute argument '{key}' in "
                                    f"'{name}'",
                                    key_tok.line,
                                    key_tok.column,
                                )
                            args[key] = str(val_tok.value)
                            if self._match(TokenKind.COMMA) is None:
                                break
                        self._expect(
                            TokenKind.RPAREN,
                            what="')' to close the attribute arguments",
                        )
                    elif self._match(TokenKind.ASSIGN) is not None:
                        val_tok = self._expect(
                            TokenKind.STRING,
                            what="a string literal attribute value",
                        )
                        args[""] = str(val_tok.value)
                    attrs.append((name, args, hash_tok.line, hash_tok.column))
                self._expect(
                    TokenKind.RBRACKET, what="']' to close the attribute"
                )
            except ParseError as exc:
                self.errors.append(exc)
                # Skip to the end of this attribute so parsing can resume.
                while self._peek() is not None:
                    if self._match(TokenKind.RBRACKET) is not None:
                        break
                    self._advance()
        return attrs

    def _apply_attributes(self, item: Node, attrs: list) -> bool:
        """Validate collected attributes against the item they precede.

        Returns whether the item survives: every ``#[cfg]`` (todo-86/93)
        whose predicate evaluates to false drops the item from the AST, so
        mutually exclusive same-name definitions never collide downstream.
        Invalid usage still raises :class:`ParseError`.

        ``#[link(...)]`` is only valid on ``extern`` blocks (todo-49);
        ``#[link_name = "..."]`` (todo-62) only on declarations *inside*
        an extern block, which are handled by
        :meth:`_apply_extern_item_attributes`.
        """
        keep = True
        if not attrs:
            return keep
        for name, args, line, column in attrs:
            def fail(message: str) -> NoReturn:
                end = column + len(name)
                raise ParseError(
                    f"#{name}: {message}", line, column,
                    end_line=line, end_column=end,
                )

            if name == "cfg":
                assert isinstance(args, CfgPredicate)
                if keep and not evaluate_cfg(args, self._cfg_context()):
                    keep = False
                continue
            if name == "link_name":
                fail(
                    "the 'link_name' attribute can only be applied to "
                    "declarations inside an extern block"
                )
            if name != "link":
                fail(
                    "unsupported attribute (only 'cfg' / 'link' / "
                    "'link_name' are supported)"
                )
            if not isinstance(item, ExternBlock):
                fail(
                    "the 'link' attribute can only be applied to an "
                    "extern block"
                )
            if item.link_name is not None or item.link_path is not None:
                fail("duplicate 'link' attribute on one extern block")
            unknown = [k for k in args if k not in self._LINK_ATTR_ARGS]
            if unknown:
                fail(
                    f"unknown 'link' argument '{unknown[0]}' "
                    "(expected name / kind / path / relative)"
                )
            kind = args.get("kind")
            if kind is not None and kind not in ("static", "dylib"):
                fail(
                    f"invalid link kind '{kind}' "
                    "(expected 'static' or 'dylib')"
                )
            relative = args.get("relative")
            if relative is not None:
                # todo-63: 锚定 link_path 的主路径; 省略时默认工作目录
                if relative not in self._LINK_RELATIVE_MODES:
                    fail(
                        f"invalid link relative '{relative}' "
                        "(expected 'cwd' or 'source')"
                    )
                if args.get("path") is None:
                    fail("the 'relative' argument requires 'path'")
                # todo-64: 绝对路径没有锚点可言, 同时给出属于自相矛盾
                if self._path_is_absolute(args["path"]):
                    fail(
                        f"'path' '{args['path']}' is absolute; "
                        "the 'relative' argument applies only to "
                        "relative paths"
                    )
            item.link_name = args.get("name")
            item.link_kind = kind
            item.link_path = args.get("path")
            item.link_relative = relative
        return keep

    def _apply_extern_item_attributes(self, item: Node, attrs: list) -> bool:
        """Validate attributes attached to a declaration inside an extern
        block.  ``#[link_name = "..."]`` (todo-62) renames the linked C
        symbol while the CWind-side name stays as declared; ``#[cfg]``
        (todo-86/93) may drop the declaration entirely.  Returns whether
        the declaration survives."""
        keep = True
        if not attrs:
            return keep
        for name, args, line, column in attrs:
            def fail(message: str) -> NoReturn:
                end = column + len(name)
                raise ParseError(
                    f"#{name}: {message}", line, column,
                    end_line=line, end_column=end,
                )

            if name == "cfg":
                assert isinstance(args, CfgPredicate)
                if keep and not evaluate_cfg(args, self._cfg_context()):
                    keep = False
                continue
            if name != "link_name":
                fail(
                    "unsupported attribute inside an extern block "
                    "(only 'cfg' / 'link_name' are supported)"
                )
            if not isinstance(item, (FnDecl, ExternStatic)):
                fail(
                    "the 'link_name' attribute can only be applied to "
                    "a fn or static declaration"
                )
            if item.link_name is not None:
                fail("duplicate 'link_name' attribute on one declaration")
            value = args.get("")
            if not value:
                fail('expects a symbol name: #[link_name = "symbol"]')
            item.link_name = value
        return keep

    def _filter_use_attributes(self, attrs: list) -> tuple[bool, list[ParseError]]:
        """Validate attributes preceding a ``use`` declaration (todo-86/93).

        Only ``#[cfg]`` is meaningful on an import.  Returns whether the
        import survives and the list of unsupported-attribute errors (the
        caller reports them once the statement itself has been dealt with).
        """
        keep = True
        unsupported: list[ParseError] = []
        for name, payload, line, column in attrs:
            if name != "cfg":
                unsupported.append(ParseError(
                    f"#{name}: unsupported attribute on a use declaration "
                    "(only 'cfg' is supported)",
                    line,
                    column,
                    end_line=line,
                    end_column=column + len(name),
                ))
                continue
            assert isinstance(payload, CfgPredicate)
            if keep and not evaluate_cfg(payload, self._cfg_context()):
                keep = False
        return keep, unsupported

    @staticmethod
    def _path_is_absolute(path: str) -> bool:
        """Mirror of the backend's ``cw_path_is_absolute`` (todo-64):
        Windows drive-letter or rooted/UNC prefixes, POSIX root."""
        if not path:
            return False
        first = path[0]
        if first in ("/", "\\"):
            return True
        return (
            len(path) > 1
            and first.isascii()
            and first.isalpha()
            and path[1] == ":"
        )

    def _parse_visibility(
        self, pub: bool
    ) -> tuple[Optional[str], Optional[list[str]]]:
        """todo-107/119: parse the restricted-visibility variants of ``pub``.

        Grammar (``in`` is a hard keyword; the qualifier itself is a path of
        identifiers, not a special token)::

            vis := 'pub' [ '(' qualifier ')' ]
            qualifier := 'self' | 'super' | 'crate' | 'std' | 'in' path
            path := segment [ '::' segment ]*     (segment := IDENTIFIER
                    | 'super' | 'crate' | 'self')

        ``pub(super::super::x)`` is the segment form of ``pub(in super::x)``,
        both normalize to ``("in", [segments])``; the four named qualifiers
        are single-word shortcuts with no path.  Returns
        ``(visibility, vis_path)`` — ``("self"| "super"| "crate"| "std"| "in",
        None-or-[segments])``; plain ``pub`` returns ``(None, None)``.  Must
        be called after a ``pub`` token was consumed (or with ``pub=False``
        to return the no-op pair).
        """
        if not pub or not self._at(TokenKind.LPAREN):
            return (None, None)
        try:
            self._advance()  # (
            if self._match(TokenKind.IN) is not None:
                vis, path = "in", self._parse_vis_path()
            else:
                word = self._expect(
                    TokenKind.IDENTIFIER,
                    what="visibility qualifier "
                    "('self'/'super'/'crate'/'std'/'in path')",
                )
                name = str(word.value)
                if name == "super" and self._match(TokenKind.PATH) is not None:
                    # ``pub(super::super::x)``: segmented restricted form.
                    rest = self._parse_vis_path(allow_super=True)
                    vis, path = "in", ["super", *rest]
                elif name not in ("self", "super", "crate", "std"):
                    raise ParseError(
                        f"unknown visibility qualifier 'pub({name})' "
                        "(supported: self/super/crate/std/"
                        "in <path>[:...])",
                        word.line,
                        word.column,
                    )
                else:
                    vis, path = name, None
            self._expect(
                TokenKind.RPAREN, what="')' after visibility qualifier"
            )
        except ParseError:
            raise
        return (vis, path)

    def _parse_vis_path(self, *, allow_super: bool = False) -> list[str]:
        """Parse the path of ``pub(in ...)`` / ``pub(super::...)``.

        Segments are identifiers plus the ``super`` keyword (its only
        sanctioned non-head position: Rust's ``pub(in super::super::x)``).
        Returns the segment list; the caller folds it into ``vis_path``.
        """
        segments: list[str] = []
        while True:
            tok = self._peek()
            if (
                tok is not None
                and tok.kind == TokenKind.IDENTIFIER
                and str(tok.value) == "super"
            ):
                self._advance()
                segments.append("super")
            elif tok is not None and tok.kind == TokenKind.IDENTIFIER:
                self._advance()
                segments.append(str(tok.value))
            elif tok is not None and tok.kind == TokenKind.SUPER:
                self._advance()
                segments.append("super")
            else:
                self._error(
                    "expected a path segment in visibility path", tok
                )
            nxt = self._peek()
            if nxt is not None and nxt.kind == TokenKind.PATH:
                self._advance()
                continue
            return segments

    def _parse_mod(
        self,
        pub: Optional[bool],
        visibility: Optional[str],
        vis_path: Optional[list[str]],
    ):
        """todo-107: parse ``[pub [vis]] mod name;`` / ``mod name { ... }``.

        Returns a single :class:`ModDecl` for both forms (the inline form's
        body carries the nested items; the external form has ``body=None``).
        The caller tags provenance; submodule file loading happens through
        the trie, which mod.wind declarations drive (todo-158).
        """
        is_pub = pub is True
        mod_tok = self._advance()  # mod
        name = self._expect(TokenKind.IDENTIFIER, what="module name")
        decl = ModDecl(
            mod_tok.line,
            mod_tok.column,
            str(name.value),
            None,
            is_pub,
            visibility,
            vis_path,
        )
        if self._match(TokenKind.LBRACE) is not None:
            body = Block(mod_tok.line, mod_tok.column, [])
            closed = False
            file_path = str(getattr(self, "source_path", None) or "")
            base_path = self._canonical_module_parts(file_path) or []
            # Parse the inline items with the ordinary top-level item loop
            # (attributes / pub / cfg included).  ``use`` inside an inline
            # module rides the same import machinery as top-level use.
            while self._peek() is not None:
                if self._match(TokenKind.RBRACE) is not None:
                    closed = True
                    break
                item_attrs = self._parse_attributes()
                item_pub_tok = self._match(TokenKind.PUB)
                item_pub = item_pub_tok is not None
                try:
                    item_vis, item_vpath = self._parse_visibility(item_pub)
                    if self._at(TokenKind.MOD) or (
                        item_pub and self._at(TokenKind.MOD)
                    ):
                        nested = self._parse_mod(
                            item_pub, item_vis, item_vpath
                        )
                        self._tag_source_module(nested)
                        nested.source_module_path = [  # type: ignore[attr-defined]
                            *base_path,
                            decl.name,
                        ]
                        body.stmts.append(nested)
                        continue
                    if self._at(TokenKind.USE):
                        use_tok = self._advance()
                        for u in self._parse_use(use_tok, pub=item_pub):
                            self._tag_source_module(u)
                            body.stmts.append(u)
                            # The loaded items join the flat program (same
                            # as top-level use); the UseDecl itself stays
                            # inside the namespace block.
                            self._append_unique(
                                self._inline_loaded_items,
                                getattr(u, "loaded_items", []),
                            )
                    else:
                        item = self._parse_item(item_pub)
                        if self._apply_attributes(item, item_attrs):
                            self._tag_source_module(item)
                            item.source_module_path = (  # type: ignore[attr-defined]
                                [*base_path, decl.name]
                            )
                            if item_vis is not None:
                                item.visibility = item_vis  # type: ignore[attr-defined]
                                item.vis_path = item_vpath  # type: ignore[attr-defined]
                            body.stmts.append(item)
                except ParseError as exc:
                    self.errors.append(exc)
                    self._synchronize_top_level()
            if not closed:
                self._error(
                    f"unterminated inline module 'mod {decl.name}' "
                    "('}' expected)",
                    mod_tok,
                )
            decl.body = body
        else:
            self._expect(
                TokenKind.SEMICOLON, what="';' after module declaration"
            )
        return decl

    def _parse_item(self, pub: bool) -> Node:
        tok = self._peek()
        if tok is None:
            self._error("expected a top-level declaration")
        if tok.kind == TokenKind.CONST:
            return self._parse_const(pub)
        if tok.kind == TokenKind.TYPE:
            return self._parse_type_decl(pub)
        if tok.kind == TokenKind.TYPEDEF:
            return self._parse_typedef(pub)
        if tok.kind == TokenKind.STRUCT:
            return self._parse_struct(pub)
        if tok.kind == TokenKind.ENUM:
            return self._parse_enum(pub)
        if tok.kind == TokenKind.TRAIT:
            return self._parse_trait(pub)
        if tok.kind == TokenKind.IMPL:
            return self._parse_impl()
        if tok.kind == TokenKind.EXTRA:
            return self._parse_extra()
        if tok.kind == TokenKind.GROUP:
            return self._parse_group()
        if tok.kind == TokenKind.FN:
            return self._parse_fn(pub=pub)
        if tok.kind == TokenKind.EXTERN:
            return self._parse_extern_block(pub=pub)
        if tok.kind == TokenKind.IDENTIFIER:
            nxt = self._peek(1)
            if nxt is not None and nxt.kind == TokenKind.AT:
                return self._parse_group_apply()
        self._error(f"unexpected token {tok.raw!r} at top level", tok)

    def _parse_const(self, pub: bool) -> ConstDecl:
        tok = self._advance()  # const
        name = self._expect(TokenKind.IDENTIFIER, what="constant name")
        self._expect(TokenKind.COLON, what="':' in const declaration")
        type_ = self._parse_type()
        self._expect(TokenKind.ASSIGN, what="'=' in const declaration")
        value = self._parse_expr(allow_map_literal=True)
        self._expect(TokenKind.SEMICOLON, what="';' after const declaration")
        return ConstDecl(tok.line, tok.column, str(name.value), type_, value, pub)

    def _parse_type_decl(self, pub: bool) -> TypeDecl:
        tok = self._advance()  # type
        name = self._expect(TokenKind.IDENTIFIER, what="type name")
        self._expect(TokenKind.ASSIGN, what="'=' in type declaration")
        base = self._parse_type()
        where: Optional[Block] = None
        if self._match(TokenKind.WHERE) is not None:
            where = self._parse_block()
        return TypeDecl(tok.line, tok.column, str(name.value), base, where, pub)

    def _parse_typedef(self, pub: bool) -> TypeDecl:
        """Parse a type alias: ``typedef Name [<Params>] = Type;``.

        Generic parameters may be declared explicitly after the name; when
        omitted, the semantic analyzer infers them from the right-hand side's
        unknown type names.
        """
        tok = self._advance()  # typedef
        name = self._expect(TokenKind.IDENTIFIER, what="alias name")
        params = self._parse_generic_params()
        self._expect(TokenKind.ASSIGN, what="'=' in typedef")
        base = self._parse_type()
        self._expect(TokenKind.SEMICOLON, what="';' after typedef")
        return TypeDecl(tok.line, tok.column, str(name.value), base, None, pub, params)

    def _parse_struct(self, pub: bool) -> StructDecl:
        tok = self._advance()  # struct
        name = self._expect(TokenKind.IDENTIFIER, what="struct name")
        params = self._parse_generic_params()
        if self._match(TokenKind.SEMICOLON) is not None:
            # unit struct: `struct Name;`
            return StructDecl(tok.line, tok.column, str(name.value), params, [], pub)
        self._expect(TokenKind.LBRACE, what="'{' after struct name")
        fields: list[Field] = []
        while not self._at(TokenKind.RBRACE):
            fields.append(self._parse_field())
            if self._match(TokenKind.COMMA) is None and self._match(TokenKind.SEMICOLON) is None:
                break
        self._advance()  # }
        return StructDecl(tok.line, tok.column, str(name.value), params, fields, pub)

    def _parse_field(self) -> Field:
        tok = self._peek()
        if tok is None:
            self._error("expected struct field")
        pub = self._match(TokenKind.PUB) is not None
        static = self._match(TokenKind.STATIC) is not None
        name = self._expect(TokenKind.IDENTIFIER, what="field name")
        self._expect(TokenKind.COLON, what="':' after field name")
        type_ = self._parse_type()
        validation: Optional[Block] = None
        if self._match(TokenKind.WHERE) is not None:
            validation = self._parse_validation_block()
        elif self._match(TokenKind.ARROW) is not None:
            validation = self._parse_validation_block()
        initializer: Optional[Node] = None
        if self._match(TokenKind.ASSIGN) is not None:
            initializer = self._parse_expr(allow_map_literal=True)
        return Field(tok.line, tok.column, str(name.value), type_, pub, static, validation, initializer)

    def _parse_enum(self, pub: bool) -> EnumDecl:
        tok = self._advance()  # enum
        name = self._expect(TokenKind.IDENTIFIER, what="enum name")
        params = self._parse_generic_params()
        self._expect(TokenKind.LBRACE, what="'{' after enum name")
        variants: list[Variant] = []
        while not self._at(TokenKind.RBRACE):
            vt = self._expect(TokenKind.IDENTIFIER, what="enum variant name")
            value: Optional[int] = None
            fields: list[Type] = []
            if self._match(TokenKind.LPAREN) is not None:
                while not self._at(TokenKind.RPAREN):
                    fields.append(self._parse_type())
                    if self._match(TokenKind.COMMA) is None:
                        break
                self._expect(
                    TokenKind.RPAREN,
                    what="')' after enum variant payload",
                )
            elif self._match(TokenKind.ASSIGN) is not None:
                num = self._expect(TokenKind.INTEGER, what="integer variant value")
                value = cast(int, num.value)
            variants.append(
                Variant(vt.line, vt.column, str(vt.value), value, fields)
            )
            if self._match(TokenKind.COMMA) is None:
                break
        self._expect(TokenKind.RBRACE, what="'}' after enum variants")
        return EnumDecl(
            tok.line, tok.column, str(name.value), variants, pub, params
        )

    def _parse_trait(self, pub: bool) -> TraitDecl:
        tok = self._advance()  # trait
        name = self._expect(TokenKind.IDENTIFIER, what="trait name")
        params = self._parse_generic_params()
        # todo-156: supertraits — ``pub trait B<T>: A, Clone { ... }``.  Only a
        # colon here means the (single) generic-param colon has already been
        # consumed by ``_parse_generic_params``, so a bare ``:`` introduces the
        # inheritance list.
        supertraits: list[Type] = []
        if self._match(TokenKind.COLON) is not None:
            supertraits.append(self._parse_type())
            while self._match(TokenKind.COMMA) is not None:
                supertraits.append(self._parse_type())
        self._expect(TokenKind.LBRACE, what="'{' after trait name")
        methods: list[FnDecl] = []
        assoc_types: list[str] = []
        while not self._at(TokenKind.RBRACE):
            if self._match(TokenKind.TYPE) is not None:
                at = self._expect(
                    TokenKind.IDENTIFIER, what="associated type name"
                )
                self._expect(
                    TokenKind.SEMICOLON,
                    what="';' after associated type declaration",
                )
                assoc_types.append(str(at.value))
                continue
            method_pub = self._match(TokenKind.PUB) is not None
            methods.append(self._parse_fn(pub=method_pub, body_required=False))
        self._advance()  # }
        return TraitDecl(
            tok.line,
            tok.column,
            str(name.value),
            params,
            methods,
            pub,
            assoc_types,
            supertraits,
        )

    def _parse_impl(self) -> ImplDecl:
        tok = self._advance()  # impl
        params = self._parse_generic_params()
        # todo-156: negative impl ``impl !Trait for Type``.  Consume the
        # leading ``!`` explicitly BEFORE _parse_type, which otherwise reads it
        # as the never-type ``!`` and would then die on ``for``.
        negative = self._match(TokenKind.NOT) is not None
        trait = self._parse_type()
        self._expect(TokenKind.FOR, what="'for' in impl declaration")
        struct = self._parse_type()
        self._expect(TokenKind.LBRACE, what="'{' after impl header")
        methods: list[FnDecl] = []
        assoc_types: list[AssocType] = []
        while not self._at(TokenKind.RBRACE):
            if self._match(TokenKind.TYPE) is not None:
                at = self._expect(
                    TokenKind.IDENTIFIER, what="associated type name"
                )
                self._expect(
                    TokenKind.ASSIGN,
                    what="'=' in associated type binding",
                )
                atype = self._parse_type()
                self._expect(
                    TokenKind.SEMICOLON,
                    what="';' after associated type binding",
                )
                assoc_types.append(
                    AssocType(at.line, at.column, str(at.value), atype)
                )
                continue
            method_pub = self._match(TokenKind.PUB) is not None
            method_static = self._match(TokenKind.STATIC) is not None
            methods.append(self._parse_fn(pub=method_pub, static=method_static))
        self._advance()  # }
        return ImplDecl(
            tok.line, tok.column, trait, struct, params, methods, assoc_types,
            negative,
        )

    def _parse_extra(self) -> ExtraDecl:
        tok = self._advance()  # extra
        params = self._parse_generic_params()
        struct = self._parse_type()
        moved_params = False
        if not params and struct.args:
            # bug-49: ``extra Cell<T> { ... }`` 允许 Grammar 省略前导
            # 泛型参数, 此时把类型实参列表挪为 extra 自己的泛型形参,
            # 归一化成 ``extra<T> Cell<T>`` (实参同时保留在类型上):
            # SA 的 defined/绑定表索引 owner params 由此获得不做不下传。
            moved: list[TypeParam] = []
            for arg in struct.args:
                if arg.args or arg.ref:
                    moved = []
                    break
                moved.append(
                    TypeParam(arg.line, arg.column, arg.name, None)
                )
            if moved:
                params = moved
                # todo-147: 裸名可能是具体类型 (``extra Cell<Int>`` 的
                # Int) —— parser 无符号表无法分辨, 先标记搬移事实, 由
                # SA 索引期解析: 具体类型则还原 params 为空 (特化)。
                moved_params = True
        self._expect(TokenKind.LBRACE, what="'{' after extra header")
        methods: list[FnDecl] = []
        # todo-122: associated constants, ``const NAME: Type = value;``
        consts: list[ConstDecl] = []
        while not self._at(TokenKind.RBRACE):
            method_pub = self._match(TokenKind.PUB) is not None
            if self._at(TokenKind.CONST):
                consts.append(self._parse_const(method_pub))
                continue
            method_static = self._match(TokenKind.STATIC) is not None
            methods.append(self._parse_fn(pub=method_pub, static=method_static))
        self._advance()  # }
        extra = ExtraDecl(tok.line, tok.column, struct, params, methods, consts)
        if moved_params:
            # 运行期标记 (不进 dataclass 字段/序列化), 供 SA 索引期甄别
            extra._params_moved_from_args = True
        return extra

    def _parse_group(self) -> GroupDecl:
        tok = self._advance()  # group
        name = self._expect(TokenKind.IDENTIFIER, what="group name")
        params: list[Param] = []
        struct: Optional[str] = None
        if self._at(TokenKind.LPAREN):
            params, _variadic = self._parse_params(allow_variadic=False)
        elif self._match(TokenKind.COLON) is not None:
            struct = str(self._expect(TokenKind.IDENTIFIER, what="struct name").value)
        self._expect(TokenKind.LBRACE, what="'{' after group header")
        if self._at(TokenKind.RBRACE):
            self._error("group policy cannot be empty", self._peek())
        distributions: list[Distribution] = []
        while not self._at(TokenKind.RBRACE):
            distributions.append(self._parse_distribution())
        self._advance()  # }
        return GroupDecl(tok.line, tok.column, str(name.value), params, struct, distributions)

    def _parse_distribution(self) -> Distribution:
        tok = self._peek()
        if tok is None:
            self._error("expected group distribution")
        subject_self = False
        if self._at(TokenKind.IDENTIFIER, value="self"):
            subject_self = True
            self._advance()
            self._expect(TokenKind.DOT, what="'.' after 'self' in distribution")
            subject = str(self._expect(TokenKind.IDENTIFIER, what="field name").value)
        else:
            subject = str(self._expect(TokenKind.IDENTIFIER, what="parameter name").value)
        self._expect(TokenKind.ARROW, what="'->' in group distribution")
        type_ = self._parse_type()
        self._expect(TokenKind.SEMICOLON, what="';' after group distribution")
        return Distribution(tok.line, tok.column, subject, type_, subject_self)

    def _parse_group_apply(self) -> GroupApply:
        group = self._expect(TokenKind.IDENTIFIER, what="group name")
        self._expect(TokenKind.AT, what="'@' in group application")
        struct = self._expect(TokenKind.IDENTIFIER, what="struct name")
        self._expect(TokenKind.ARROW, what="'->' in group application")
        self._expect(TokenKind.LBRACE, what="'{' after '->'")
        fields: list[str] = []
        while not self._at(TokenKind.RBRACE):
            fields.append(str(self._expect(TokenKind.IDENTIFIER, what="field name").value))
            if self._match(TokenKind.COMMA) is None:
                break
        self._expect(TokenKind.RBRACE, what="'}' after group fields")
        self._match(TokenKind.SEMICOLON)  # optional: the grammar example omits it
        return GroupApply(group.line, group.column, str(group.value), str(struct.value), fields)

    def _parse_fn(
        self,
        *,
        pub: bool = False,
        static: bool = False,
        body_required: bool = True,
        allow_variadic: bool = False,
    ) -> FnDecl:
        tok = self._advance()  # fn
        name = self._expect(TokenKind.IDENTIFIER, what="function name")
        type_params = self._parse_generic_params()
        params, variadic = self._parse_params(allow_variadic=allow_variadic)
        return_type: Optional[Type] = None
        if self._match(TokenKind.ARROW) is not None:
            return_type = self._parse_type()
        which: Optional[str] = None
        if self._match(TokenKind.COMMA) is not None:
            self._expect(TokenKind.WHICH, what="'which' in function signature")
            self._expect(TokenKind.PATH, what="'::' after 'which'")
            which = str(self._expect(TokenKind.IDENTIFIER, what="method name after 'which ::'").value)
        if body_required or self._at(TokenKind.LBRACE):
            body = self._parse_block()
        else:
            body = None
            self._expect(TokenKind.SEMICOLON, what="';' after function signature")
        decl = FnDecl(
            tok.line,
            tok.column,
            str(name.value),
            type_params,
            params,
            return_type,
            body,
            pub,
            static,
            which,
        )
        decl.variadic = variadic
        if decl.body is not None:
            self._make_function_tail_return(decl.body)
        return decl

    def _parse_extern_block(self, *, pub: bool = False) -> ExternBlock:
        """Parse a C-FFI declaration block: ``extern "C" { fn ...; }``.

        Contained items are body-less function signatures
        (``fn name(params) -> Ret;``) and, since todo-56, extern static
        bindings (``static [mut] NAME: Type;``).  Each item may carry a
        ``#[link_name = "..."]`` attribute (todo-62) renaming its C
        symbol and ``#[cfg(...)]`` attributes (todo-86/93) dropping it
        on non-matching targets.  The block's ABI string is recorded on
        each ``FnDecl`` as ``extern_abi`` so the backend can emit raw-C
        declarations and calls.
        """
        tok = self._advance()  # extern
        if str(tok.value) != "extern":
            self._error("expected 'extern'", tok)
        abi_tok = self._expect(
            TokenKind.STRING, what='an ABI string, e.g. "C"'
        )
        abi = str(abi_tok.value)
        if not abi:
            self._error("the extern ABI string cannot be empty", abi_tok)
        self._expect(
            TokenKind.LBRACE, what="'{' to open the extern block"
        )
        fns: list[FnDecl] = []
        statics: list[ExternStatic] = []
        while not self._at(TokenKind.RBRACE):
            if self._peek() is None:
                self._error("expected '}' to close the extern block", tok)
            fn_tok = self._peek()
            try:
                attrs = self._parse_attributes()
                # bug-40: extern 块成员允许自带 ``pub`` (与块级 pub 取或),
                # C 符号本身不受影响.
                item_pub = self._match(TokenKind.PUB) is not None or pub
                if self._at(TokenKind.STATIC):
                    static = self._parse_extern_static(pub=item_pub)
                    if self._apply_extern_item_attributes(static, attrs):
                        # todo-86/93: a false #[cfg] drops the binding.
                        statics.append(static)
                    continue
                # todo-87: extern 块内允许 ``...`` 变参 (仅此一处).
                fn = self._parse_fn(
                    pub=item_pub, body_required=False, allow_variadic=True
                )
                fn.extern_abi = abi
                if self._apply_extern_item_attributes(fn, attrs):
                    fns.append(fn)
            except ParseError as exc:
                self.errors.append(exc)
                if self._peek() is fn_tok:
                    self._advance()  # never spin on the same token
                # Skip to the next `fn` / `static` or the closing brace.
                while (
                    self._peek() is not None
                    and not self._at(TokenKind.FN)
                    and not self._at(TokenKind.STATIC)
                    and not self._at(TokenKind.RBRACE)
                ):
                    self._advance()
        self._advance()  # }
        return ExternBlock(tok.line, tok.column, abi, fns, statics, pub)

    def _parse_extern_static(self, *, pub: bool = False) -> ExternStatic:
        """Parse an extern static binding (todo-56): ``static [mut] N: T;``."""
        tok = self._advance()  # static
        mutable = self._match(TokenKind.MUT) is not None
        name = self._expect(
            TokenKind.IDENTIFIER, what="an extern static name"
        )
        self._expect(TokenKind.COLON, what="':' after the extern static name")
        ty = self._parse_type()
        self._expect(
            TokenKind.SEMICOLON,
            what="';' after the extern static declaration",
        )
        return ExternStatic(tok.line, tok.column, str(name.value), ty,
                            mutable, pub)

    def _make_function_tail_return(self, body: Block) -> None:
        """Lower a Rust-like function tail expression into ``return expr;``."""
        if not body.stmts:
            return
        last = body.stmts[-1]
        if (
            isinstance(last, ExprStmt)
            and getattr(last.expr, "_tail_expr", False)
        ):
            last.expr._tail_expr = False
            body.stmts[-1] = ReturnStmt(
                last.line,
                last.column,
                last.expr,
            )

    def _parse_params(
        self, allow_variadic: bool = False
    ) -> tuple[list[Param], bool]:
        """Parse a parameter list.

        Mutable receivers use Rust's postfix ordering ``&mut self``
        (todo-47); the retired ``mut &self`` form is rejected with a
        pointer to the new syntax.  Plain bindings keep ``mut x: T``.

        todo-87: a trailing ``...`` marker is only accepted when
        ``allow_variadic`` (extern blocks).  Returns the parameter list
        plus whether a variadic marker was present.
        """
        self._expect(TokenKind.LPAREN, what="'(' before parameter list")
        params: list[Param] = []
        variadic = False
        while not self._at(TokenKind.RPAREN):
            if self._at(TokenKind.ELLIPSIS):
                ell = self._advance()
                if not allow_variadic:
                    self._error(
                        "'...' variadic parameters are only allowed "
                        "inside extern blocks",
                        ell,
                    )
                if params:
                    # A trailing comma between the fixed parameters and
                    # '...' would break the C signature shape.
                    variadic = True
                    continue
                self._error(
                    "'...' requires at least one fixed parameter before it",
                    ell,
                )
            mutable = False
            if self._at(TokenKind.MUT):
                self._advance()
                mutable = True
                if self._at(TokenKind.AMP):
                    self._error(
                        "'mut &' is not allowed; write '&mut self'",
                        self._peek(),
                    )
            if self._at(TokenKind.AMP):
                amp = self._advance()
                if self._match(TokenKind.MUT) is not None:
                    mutable = True
                tok = self._expect(TokenKind.IDENTIFIER, what="parameter name")
                if str(tok.value) != "self":
                    self._error(
                        "only 'self' may omit a type after '&'", tok
                    )
                type_ = Type(amp.line, amp.column, "Self", ref=True)
                param = Param(amp.line, amp.column, str(tok.value), type_)
                param.mutable = mutable
                params.append(param)
            else:
                tok = self._expect(TokenKind.IDENTIFIER, what="parameter name")
                type_: Optional[Type] = None
                if self._match(TokenKind.COLON) is not None:
                    type_ = self._parse_type()
                elif str(tok.value) != "self":
                    self._error("parameter requires a type annotation", tok)
                param = Param(tok.line, tok.column, str(tok.value), type_)
                param.mutable = mutable
                params.append(param)
            if self._match(TokenKind.COMMA) is None:
                break
        self._expect(TokenKind.RPAREN, what="')' after parameter list")
        return params, variadic

    def _parse_generic_params(self) -> list[TypeParam]:
        """Parse an optional generic parameter list: ``<T, U: Bound>``."""
        if self._match(TokenKind.LT) is None:
            return []
        params: list[TypeParam] = []
        while True:
            tok = self._expect(TokenKind.IDENTIFIER, what="generic parameter name")
            bound: Optional[Type] = None
            if self._match(TokenKind.COLON) is not None:
                bound = self._parse_type()
            params.append(TypeParam(tok.line, tok.column, str(tok.value), bound))
            if self._match(TokenKind.COMMA) is None:
                break
        self._expect_gt("'>' closing generic parameter list")
        return params

    # -- types -------------------------------------------------------------

    def _parse_type(self) -> Type:
        if self._at(TokenKind.FN):
            fn_node = self._parse_function_pointer()
            return Type(fn_node.line, fn_node.column, fn_node.parts[0], [], ref=False)
        if self._at(TokenKind.STAR_CONST) or self._at(TokenKind.STAR_MUT):
            # 原始指针: `*const T` / `*mut T`, 以 "*const "/"*mut " 前缀
            # 编码进类型名 (与 "&" 的 ref 标记同思路, 字符串化后仍可辨认)
            star = self._advance()
            inner = self._parse_type()
            prefix = "const" if star.kind == TokenKind.STAR_CONST else "mut"
            return Type(
                star.line,
                star.column,
                f"*{prefix} {_type_name_for_type(inner)}",
                [],
                ref=False,
            )
        if self._at(TokenKind.AMP):
            amp = self._advance()
            # bug-46: ``&mut T`` 类型位 —— MUT 只在借用标记后合法
            # (操作数表达式的 mut 由 _parse_unary 消费, 互不干扰)。
            mut = self._match(TokenKind.MUT) is not None
            inner = self._parse_type()
            return Type(
                amp.line,
                amp.column,
                inner.name,
                inner.args,
                ref=True,
                mut=mut,
            )
        if self._at(TokenKind.LBRACKET):
            # 定长数组类型 (todo-60): `[T; N]`, 与 C `char[N]` /
            # Rust `[u8; N]` 对应, 名字整体扁平化编码 (同原始指针思路)
            lb = self._advance()
            inner = self._parse_type()
            self._expect(TokenKind.SEMICOLON, what="';' in array type")
            len_tok = self._expect(
                TokenKind.INTEGER, what="array length after ';'"
            )
            self._expect(TokenKind.RBRACKET, what="']' closing array type")
            return Type(
                lb.line,
                lb.column,
                f"[{_type_name_for_type(inner)}; {len_tok.value}]",
                [],
                ref=False,
            )
        if self._at(TokenKind.NOT):
            tok = self._advance()  # !
            return Type(tok.line, tok.column, "!")
        tok = self._expect(TokenKind.IDENTIFIER, what="type name")
        if self._at(TokenKind.PATH):
            # 限定路径类型: `module::Name` / `Self::Item` (bug-42: impl
            # 头部允许 `num_wrapping::Wrapping<i32>` 这类带实参的限定名)
            parts = [str(tok.value)]
            while self._at(TokenKind.PATH):
                self._advance()
                part = self._expect(
                    TokenKind.IDENTIFIER, what="name after '::' in type"
                )
                parts.append(str(part.value))
            args: list[Type] = []
            if self._match(TokenKind.LT) is not None:
                while True:
                    args.append(self._parse_type())
                    if self._match(TokenKind.COMMA) is None:
                        break
                self._expect_gt("'>' closing generic type")
            return Type(tok.line, tok.column, "::".join(parts), args)
        args: list[Type] = []
        if self._match(TokenKind.LT) is not None:
            while True:
                args.append(self._parse_type())
                if self._match(TokenKind.COMMA) is None:
                    break
            self._expect_gt("'>' closing generic type")
        return Type(tok.line, tok.column, str(tok.value), args)

    # -- statements --------------------------------------------------------

    def _parse_block(self) -> Block:
        tok = self._expect(TokenKind.LBRACE, what="'{' to open a block")
        stmts: list[Node] = []
        while not self._at(TokenKind.RBRACE):
            if self._peek() is None:
                self._error("expected '}' to close the block", tok)
            stmt_tok = self._peek()
            try:
                stmts.append(self._parse_stmt())
            except ParseError as exc:
                self.errors.append(exc)
                if self._peek() is stmt_tok:
                    self._advance()  # never spin on the same token
                self._synchronize_statement()
                stmts.append(ErrorStmt(exc.line, exc.column, exc.message))
        self._advance()  # }
        # Always pass a fresh list: Block's mutable default can alias an
        # empty block that is still being parsed (seen with an empty for body).
        return Block(tok.line, tok.column, list(stmts))

    def _parse_validation_block(self) -> Block:
        """Parse a field-validation block: ``{ expr }`` without semicolons.

        Field validation (``where { ... }`` / ``-> { ... }``) contains bare
        expressions, unlike type-level ``where`` blocks whose statements end
        with ``;``.  Optional ``;`` separators are accepted for leniency.
        """
        tok = self._expect(TokenKind.LBRACE, what="'{' to open a validation block")
        exprs: list[Node] = []
        while not self._at(TokenKind.RBRACE):
            expr = self._parse_expr()
            exprs.append(ExprStmt(expr.line, expr.column, expr))
            if self._match(TokenKind.SEMICOLON) is None:
                break
        self._expect(TokenKind.RBRACE, what="'}' to close the validation block")
        return Block(tok.line, tok.column, exprs)

    def _parse_stmt(self) -> Node:
        tok = self._peek()
        if tok is None:
            self._error("expected statement")
        if tok.kind == TokenKind.LET:
            return self._parse_let()
        if tok.kind == TokenKind.RETURN:
            return self._parse_return()
        if tok.kind == TokenKind.BREAK:
            return self._parse_break()
        if tok.kind == TokenKind.CONTINUE:
            return self._parse_continue()
        if tok.kind == TokenKind.IF:
            return self._parse_if()
        if tok.kind == TokenKind.MATCH:
            return self._parse_match()
        if tok.kind == TokenKind.WHILE:
            return self._parse_while()
        if tok.kind == TokenKind.FOR:
            return self._parse_for()
        if tok.kind == TokenKind.LBRACE:
            return self._parse_block()
        expr = self._parse_expr()
        is_tail = self._at(TokenKind.RBRACE)
        if not is_tail:
            self._expect(TokenKind.SEMICOLON, what="';' after statement")
        expr._tail_expr = is_tail
        return ExprStmt(expr.line, expr.column, expr)

    def _parse_let(self) -> LetStmt:
        tok = self._advance()  # let
        mutable = False
        mut_tok = self._peek()
        if mut_tok is not None and mut_tok.kind == TokenKind.MUT:
            self._advance()
            mutable = True
        name = self._expect(TokenKind.IDENTIFIER, what="variable name")
        self._expect(TokenKind.COLON, what="':' after variable name (let needs a type)")
        type_ = self._parse_type()
        value: Optional[Node] = None
        if self._match(TokenKind.ASSIGN) is not None:
            value = self._parse_expr(allow_map_literal=True)
        self._expect(TokenKind.SEMICOLON, what="';' after let declaration")
        return LetStmt(
            tok.line,
            tok.column,
            str(name.value),
            type_,
            value,
            mutable=mutable,
        )

    def _parse_return(self) -> ReturnStmt:
        tok = self._advance()  # return
        value: Optional[Node] = None
        if not self._at(TokenKind.SEMICOLON):
            value = self._parse_expr()
        self._expect(TokenKind.SEMICOLON, what="';' after return")
        return ReturnStmt(tok.line, tok.column, value)

    def _parse_break(self) -> BreakStmt:
        tok = self._advance()  # break
        self._expect(TokenKind.SEMICOLON, what="';' after break")
        return BreakStmt(tok.line, tok.column)

    def _parse_continue(self) -> ContinueStmt:
        tok = self._advance()  # continue
        self._expect(TokenKind.SEMICOLON, what="';' after continue")
        return ContinueStmt(tok.line, tok.column)

    def _parse_if(self) -> IfStmt:
        tok = self._advance()  # if
        if self._match(TokenKind.LET) is not None:
            return self._parse_if_let(tok)
        self._expect(TokenKind.LPAREN, what="'(' after 'if'")
        cond = self._parse_expr()
        self._expect(TokenKind.RPAREN, what="')' after if condition")
        then = self._parse_block()
        elifs: list[ElifBranch] = []
        while self._at(TokenKind.ELIF):
            et = self._advance()
            self._expect(TokenKind.LPAREN, what="'(' after 'elif'")
            econd = self._parse_expr()
            self._expect(TokenKind.RPAREN, what="')' after elif condition")
            ebody = self._parse_block()
            elifs.append(ElifBranch(et.line, et.column, econd, ebody))
        else_: Optional[Block] = None
        if self._match(TokenKind.ELSE) is not None:
            else_ = self._parse_block()
        return IfStmt(tok.line, tok.column, cond, then, elifs, else_)

    def _parse_if_let(self, tok: Token) -> IfLetStmt:
        pattern = self._parse_pattern()
        self._expect(
            TokenKind.ASSIGN, what="'=' between if-let pattern and value"
        )
        value = self._parse_expr(allow_map_literal=True)
        then = self._parse_block()
        elifs: list[IfLetBranch] = []
        while self._at(TokenKind.ELIF):
            et = self._advance()
            if self._match(TokenKind.LET) is not None:
                ep = self._parse_pattern()
                self._expect(
                    TokenKind.ASSIGN,
                    what="'=' between elif-let pattern and value",
                )
                ev = self._parse_expr(allow_map_literal=True)
                eb = self._parse_block()
                elifs.append(IfLetBranch(et.line, et.column, None, ep, ev, eb))
            else:
                self._expect(TokenKind.LPAREN, what="'(' after 'elif'")
                econd = self._parse_expr()
                self._expect(TokenKind.RPAREN, what="')' after elif condition")
                ebody = self._parse_block()
                elifs.append(IfLetBranch(et.line, et.column, econd, None, None, ebody))
        else_: Optional[Block] = None
        if self._match(TokenKind.ELSE) is not None:
            else_ = self._parse_block()
        return IfLetStmt(
            tok.line,
            tok.column,
            pattern,
            value,
            then,
            elifs,
            else_,
        )

    def _parse_match(self) -> MatchStmt:
        tok = self._advance()  # match
        self._expect(TokenKind.LPAREN, what="'(' after 'match'")
        subject = self._parse_expr(allow_map_literal=True)
        self._expect(TokenKind.RPAREN, what="')' after match subject")
        self._expect(TokenKind.LBRACE, what="'{' after match subject")
        arms: list[MatchArm] = []
        while not self._at(TokenKind.RBRACE):
            if self._peek() is None:
                self._error("expected '}' to close the match", tok)
            at = self._peek()
            pattern = self._parse_pattern()
            guard: Optional[Node] = None
            if self._match(TokenKind.IF) is not None:
                guard = self._parse_expr(allow_map_literal=True)
            self._expect(
                TokenKind.FAT_ARROW,
                what="'=>' between match pattern and body",
            )
            if self._at(TokenKind.LBRACE):
                body = self._parse_block()
            else:
                body = self._parse_expr(allow_map_literal=True)
            arms.append(MatchArm(at.line, at.column, pattern, guard, body))
            if self._match(TokenKind.COMMA) is None:
                break
        self._expect(TokenKind.RBRACE, what="'}' after match arms")
        return MatchStmt(tok.line, tok.column, subject, arms)

    def _parse_pattern(self) -> Node:
        tok = self._peek()
        if tok is None:
            self._error("expected pattern")
        if tok.kind == TokenKind.INTEGER:
            self._advance()
            return LitPattern(
                tok.line, tok.column, IntLit(tok.line, tok.column, cast(int, tok.value), tok.raw)
            )
        if tok.kind == TokenKind.FLOAT:
            self._advance()
            return LitPattern(
                tok.line, tok.column, FloatLit(tok.line, tok.column, cast(float, tok.value), tok.raw)
            )
        if tok.kind == TokenKind.STRING:
            self._advance()
            return LitPattern(
                tok.line, tok.column, StrLit(tok.line, tok.column, str(tok.value), tok.raw)
            )
        if tok.kind == TokenKind.IDENTIFIER and tok.value in ("true", "false"):
            self._advance()
            return LitPattern(
                tok.line,
                tok.column,
                BoolLit(tok.line, tok.column, tok.value == "true", tok.raw),
            )
        if tok.kind == TokenKind.IDENTIFIER and tok.value == "_":
            self._advance()
            return WildcardPattern(tok.line, tok.column)
        if tok.kind == TokenKind.LPAREN:
            self._advance()
            if self._at(TokenKind.RPAREN):
                self._advance()
                return TuplePattern(tok.line, tok.column, [])
            elems = [self._parse_pattern()]
            while self._match(TokenKind.COMMA) is not None:
                if self._at(TokenKind.RPAREN):
                    break  # `(a, b,)` is a two-element tuple pattern
                elems.append(self._parse_pattern())
            self._expect(TokenKind.RPAREN, what="')' after tuple pattern")
            return TuplePattern(tok.line, tok.column, elems)
        if tok.kind == TokenKind.IDENTIFIER:
            type_ = self._try_parse_pattern_type()
            if type_ is not None:
                return self._parse_struct_pattern(type_)
            name = self._advance()
            if self._at(TokenKind.PATH):
                parts = [str(name.value)]
                while self._at(TokenKind.PATH):
                    self._advance()
                    part = self._expect(
                        TokenKind.IDENTIFIER, what="name after '::'"
                    )
                    parts.append(str(part.value))
                if len(parts) not in (2, 3):
                    # todo-81: ``module::Enum::Variant`` keeps its three
                    # source segments here; SA normalizes the resolved
                    # form back to the canonical two-segment path.
                    self._error("unsupported path pattern", self._peek())
                elems: list[Node] = []
                if self._match(TokenKind.LPAREN) is not None:
                    while not self._at(TokenKind.RPAREN):
                        elems.append(self._parse_pattern())
                        if self._match(TokenKind.COMMA) is None:
                            break
                    self._expect(
                        TokenKind.RPAREN,
                        what="')' after enum variant pattern",
                    )
                return EnumPattern(
                    tok.line, tok.column, parts, elems
                )
            return BindPattern(tok.line, tok.column, str(name.value))
        self._error(f"unexpected token {tok.raw!r} in pattern", tok)

    def _try_parse_pattern_type(self) -> Optional[Type]:
        """Speculatively parse ``Name<Args>`` as a type when a struct-pattern
        brace follows (pattern position is never a comparison, so ``<`` is
        unambiguous here)."""
        snap = self._snapshot()
        try:
            type_ = self._parse_type()
            if self._at(TokenKind.LBRACE):
                return type_
        except ParseError:
            pass
        self._restore(snap)
        return None

    def _parse_struct_pattern(self, type_: Type) -> StructPattern:
        tok = self._advance()  # {
        fields: list[StructPatternField] = []
        rest = False
        while not self._at(TokenKind.RBRACE):
            if self._peek() is None:
                self._error("expected '}' after struct pattern", tok)
            if self._match(TokenKind.UNPACK) is not None:
                rest = True
                if not self._at(TokenKind.RBRACE):
                    self._error(
                        "'..' must be the last field in a struct pattern",
                        self._peek(),
                    )
                break
            ft = self._expect(
                TokenKind.IDENTIFIER, what="field name in struct pattern"
            )
            sub: Optional[Node] = None
            if self._match(TokenKind.COLON) is not None:
                sub = self._parse_pattern()
            fields.append(
                StructPatternField(
                    ft.line, ft.column, str(ft.value), sub
                )
            )
            if self._match(TokenKind.COMMA) is None:
                break
        self._expect(TokenKind.RBRACE, what="'}' after struct pattern")
        return StructPattern(type_.line, type_.column, type_, fields, rest)

    def _parse_while(self) -> WhileStmt:
        tok = self._advance()  # while
        self._expect(TokenKind.LPAREN, what="'(' after 'while'")
        cond = self._parse_expr()
        self._expect(TokenKind.RPAREN, what="')' after while condition")
        body = self._parse_block()
        return WhileStmt(tok.line, tok.column, cond, body)

    def _parse_for(self) -> ForStmt:
        tok = self._advance()  # for
        if self._at(TokenKind.LPAREN):
            # for ( [Type] var : iterable ) { ... }
            self._advance()  # (
            type_: Optional[Type] = None
            nxt = self._peek(1)
            if self._at(TokenKind.IDENTIFIER) and nxt is not None and nxt.kind == TokenKind.IDENTIFIER:
                type_ = self._parse_type()
            var = self._expect(TokenKind.IDENTIFIER, what="loop variable")
            self._expect(TokenKind.COLON, what="':' in for-in sugar")
            iterable = self._parse_expr()
            self._expect(TokenKind.RPAREN, what="')' after for-in header")
            self._expect(TokenKind.LBRACE, what="'{' to open the for-in loop body")
            self.pos -= 1  # let _parse_block consume and validate the brace
            body = self._parse_block()
            return ForStmt(tok.line, tok.column, str(var.value), iterable, body, type_, True)
        if self._at(TokenKind.IN):
            self._error("expected iteration variable before 'in'", self._peek())
        var = self._expect(TokenKind.IDENTIFIER, what="loop variable")
        in_tok = self._peek()
        if not (in_tok is not None and in_tok.kind == TokenKind.IN):
            self._error("expected 'in' in for-in loop", in_tok)
        self._advance()  # in
        self._for_iterable_expr = True
        try:
            iterable = self._parse_expr()
        finally:
            self._for_iterable_expr = False
        self._expect(TokenKind.LBRACE, what="'{' to open the for-in loop body")
        self.pos -= 1  # let _parse_block consume and validate the brace
        body = self._parse_block()
        return ForStmt(tok.line, tok.column, str(var.value), iterable, body, None, False)

    # -- expressions -------------------------------------------------------

    def _parse_expr(self, *, allow_map_literal: bool = False) -> Node:
        left = self._parse_or(allow_map_literal=allow_map_literal)
        tok = self._peek()
        if tok is not None and tok.kind in _ASSIGN_OPS:
            op = self._advance()
            right = self._parse_expr(allow_map_literal=True)  # right-associative
            return Assign(left.line, left.column, left, op.kind, right)
        return left

    def _parse_or(self, *, allow_map_literal: bool = False) -> Node:
        node = self._parse_and(allow_map_literal=allow_map_literal)
        while self._at(TokenKind.OR):
            op = self._advance()
            node = BinOp(
                node.line,
                node.column,
                node,
                op.kind,
                self._parse_and(allow_map_literal=allow_map_literal),
            )
        return node

    def _parse_and(self, *, allow_map_literal: bool = False) -> Node:
        node = self._parse_equality(allow_map_literal=allow_map_literal)
        while self._at(TokenKind.AND):
            op = self._advance()
            node = BinOp(
                node.line,
                node.column,
                node,
                op.kind,
                self._parse_equality(allow_map_literal=allow_map_literal),
            )
        return node

    def _parse_equality(self, *, allow_map_literal: bool = False) -> Node:
        node = self._parse_relational(allow_map_literal=allow_map_literal)
        while (tok := self._peek()) is not None and tok.kind in _EQUALITY_OPS:
            op = self._advance()
            node = BinOp(
                node.line,
                node.column,
                node,
                op.kind,
                self._parse_relational(allow_map_literal=allow_map_literal),
            )
        return node

    def _parse_relational(self, *, allow_map_literal: bool = False) -> Node:
        node = self._parse_additive(allow_map_literal=allow_map_literal)
        while (tok := self._peek()) is not None and tok.kind in _RELATIONAL_OPS:
            op = self._advance()
            node = BinOp(
                node.line,
                node.column,
                node,
                op.kind,
                self._parse_additive(allow_map_literal=allow_map_literal),
            )
        return node

    def _parse_additive(self, *, allow_map_literal: bool = False) -> Node:
        node = self._parse_multiplicative(allow_map_literal=allow_map_literal)
        while (tok := self._peek()) is not None and tok.kind in _ADDITIVE_OPS:
            op = self._advance()
            node = BinOp(
                node.line,
                node.column,
                node,
                op.kind,
                self._parse_multiplicative(allow_map_literal=allow_map_literal),
            )
        return node

    def _parse_multiplicative(self, *, allow_map_literal: bool = False) -> Node:
        node = self._parse_shift(allow_map_literal=allow_map_literal)
        while (tok := self._peek()) is not None and tok.kind in _MULTIPLICATIVE_OPS:
            op = self._advance()
            node = BinOp(
                node.line,
                node.column,
                node,
                op.kind,
                self._parse_shift(allow_map_literal=allow_map_literal),
            )
        return node

    def _parse_shift(self, *, allow_map_literal: bool = False) -> Node:
        node = self._parse_band(allow_map_literal=allow_map_literal)
        while (tok := self._peek()) is not None and tok.kind in _SHIFT_OPS:
            op = self._advance()
            node = BinOp(
                node.line,
                node.column,
                node,
                op.kind,
                self._parse_band(allow_map_literal=allow_map_literal),
            )
        return node

    def _parse_band(self, *, allow_map_literal: bool = False) -> Node:
        node = self._parse_bxor(allow_map_literal=allow_map_literal)
        while self._at(TokenKind.AMP):
            op = self._advance()
            node = BinOp(
                node.line,
                node.column,
                node,
                op.kind,
                self._parse_bxor(allow_map_literal=allow_map_literal),
            )
        return node

    def _parse_bxor(self, *, allow_map_literal: bool = False) -> Node:
        node = self._parse_bor(allow_map_literal=allow_map_literal)
        while self._at(TokenKind.CARET):
            op = self._advance()
            node = BinOp(
                node.line,
                node.column,
                node,
                op.kind,
                self._parse_bor(allow_map_literal=allow_map_literal),
            )
        return node

    def _parse_bor(self, *, allow_map_literal: bool = False) -> Node:
        node = self._parse_cast(allow_map_literal=allow_map_literal)
        while self._at(TokenKind.PIPE):
            op = self._advance()
            node = BinOp(
                node.line,
                node.column,
                node,
                op.kind,
                self._parse_cast(allow_map_literal=allow_map_literal),
            )
        return node

    def _parse_cast(self, *, allow_map_literal: bool = False) -> Node:
        # todo-17: ``expr as T`` — Rust precedence: tighter than ``|``,
        # looser than unary, so ``-x as T`` is ``(-x) as T``.
        node = self._parse_unary(allow_map_literal=allow_map_literal)
        while self._at(TokenKind.AS):
            tok = self._advance()
            target = self._parse_type()
            node = CastExpr(tok.line, tok.column, node, target)
        return node

    def _parse_unary(self, *, allow_map_literal: bool = False) -> Node:
        tok = self._peek()
        if tok is not None and tok.kind in _UNARY_OPS:
            op = self._advance()
            # bug-46: ``&mut expr`` —— 借用表达式后允许可变标记;
            # ``mut`` 是关键字, 不可能是操作数首符, 消费它无歧义。
            mutable = (
                op.kind == TokenKind.AMP
                and self._match(TokenKind.MUT) is not None
            )
            return UnaryOp(
                tok.line,
                tok.column,
                op.kind,
                self._parse_unary(allow_map_literal=allow_map_literal),
                mutable=mutable,
            )
        return self._parse_postfix(allow_map_literal=allow_map_literal)

    def _parse_postfix(self, *, allow_map_literal: bool = False) -> Node:
        node = self._parse_primary(allow_map_literal=allow_map_literal)
        while True:
            tok = self._peek()
            if tok is None:
                break
            if tok.kind == TokenKind.DOT:
                self._advance()
                if self._at(TokenKind.INTEGER):
                    num_tok = self._peek()
                    self._advance()
                    member = str(num_tok.value)
                elif self._at(TokenKind.FLOAT):
                    # `p.0.0` 词法上是 `p . 0.0`: 把浮点拆成成员 `0` +
                    # 合成 `.0`, 让 postfix 链继续 (Rust tuple 元素访问)。
                    float_tok = self._peek()
                    parts = float_tok.raw.split(".", 1)
                    if (len(parts) == 2 and parts[0].isdigit()
                            and parts[1].isdigit()):
                        self._advance()
                        member = parts[0]
                        int_col = float_tok.column + len(parts[0]) + 1
                        self._pending.append(Token(
                            TokenKind.DOT, ".",
                            float_tok.line, float_tok.column + len(parts[0]),
                            float_tok.line, int_col, ".",
                        ))
                        self._pending.append(Token(
                            TokenKind.INTEGER, int(parts[1]),
                            float_tok.line, int_col,
                            float_tok.end_line, float_tok.end_column,
                            parts[1],
                        ))
                    else:
                        self._expect(
                            TokenKind.IDENTIFIER,
                            what="member name after '.'",
                        )
                        member = ""
                else:
                    name = self._expect(
                        TokenKind.IDENTIFIER, what="member name after '.'"
                    )
                    member = str(name.value)
                node = Attribute(node.line, node.column, node, member)
            elif tok.kind == TokenKind.LPAREN:
                args = self._parse_call_args(allow_map_literal=allow_map_literal)
                node = Call(node.line, node.column, node, args)
            elif tok.kind == TokenKind.LBRACKET:
                node = self._parse_index_or_slice(node, allow_map_literal=allow_map_literal)
            else:
                break
        return node

    def _parse_primary(self, *, allow_map_literal: bool = False) -> Node:
        tok = self._peek()
        if tok is None:
            self._error("expected expression")
        if tok.kind == TokenKind.INTEGER:
            self._advance()
            return IntLit(tok.line, tok.column, cast(int, tok.value), tok.raw)
        if tok.kind == TokenKind.FLOAT:
            self._advance()
            return FloatLit(tok.line, tok.column, cast(float, tok.value), tok.raw)
        if tok.kind == TokenKind.STRING:
            self._advance()
            return StrLit(tok.line, tok.column, str(tok.value), tok.raw)
        if tok.kind == TokenKind.LPAREN:
            self._advance()
            if self._at(TokenKind.RPAREN):
                self._advance()
                return TupleLit(tok.line, tok.column, [])
            node = self._parse_expr(allow_map_literal=allow_map_literal)
            if self._match(TokenKind.COMMA) is None:
                self._expect(
                    TokenKind.RPAREN,
                    what="')' after parenthesized expression",
                )
                return node
            elems = [node]
            while not self._at(TokenKind.RPAREN):
                elems.append(
                    self._parse_expr(allow_map_literal=allow_map_literal)
                )
                if self._match(TokenKind.COMMA) is None:
                    break
            self._expect(TokenKind.RPAREN, what="')' after tuple literal")
            return TupleLit(tok.line, tok.column, elems)
        if tok.kind == TokenKind.IDENTIFIER and tok.value in ("true", "false"):
            self._advance()
            return BoolLit(tok.line, tok.column, tok.value == "true", tok.raw)
        if tok.kind == TokenKind.LBRACKET:
            return self._parse_vector_literal(allow_map_literal=allow_map_literal)
        if tok.kind == TokenKind.MATCH:
            return self._parse_match()
        if tok.kind == TokenKind.PIPE or tok.kind == TokenKind.OR:
            return self._parse_closure()
        if tok.kind == TokenKind.LBRACE:
            # Grammar.md: `{ ... }` is a map literal only on the right of `=`.
            if allow_map_literal:
                return self._parse_map_literal()
            self._error("unexpected token '{' in expression", tok)
        if tok.kind == TokenKind.IDENTIFIER:
            if not self._for_iterable_expr:
                generic_type = self._try_parse_generic_struct_construct()
                if generic_type is not None:
                    return self._parse_struct_construct(
                        generic_type, allow_map_literal=allow_map_literal
                    )
            name = self._parse_name_path()
            if (
                not self._for_iterable_expr
                and self._at(TokenKind.LBRACE)
                and self._brace_is_struct_construct()
            ):
                type_ = Type(name.line, name.column, "::".join(name.parts))
                return self._parse_struct_construct(
                    type_, allow_map_literal=allow_map_literal
                )
            return name
        self._error(f"unexpected token {tok.raw!r} in expression", tok)

    def _parse_name_path(self) -> Name:
        tok = self._expect(TokenKind.IDENTIFIER, what="name")
        parts = [str(tok.value)]
        while self._at(TokenKind.PATH):
            self._advance()
            part = self._expect(TokenKind.IDENTIFIER, what="name after '::'")
            parts.append(str(part.value))
        return Name(tok.line, tok.column, parts)

    def _parse_function_pointer(self) -> Name:
        """Parse a function-pointer type ``fn(A, B) -> R`` (type position).

        The signature is flattened into a single name string
        (``"fn(Int, String) -> Int"``) so the rest of the string-based
        type pipeline can carry it unchanged.
        """
        tok = self._expect(TokenKind.FN, what="'fn' in function-pointer type")
        self._expect(TokenKind.LPAREN, what="'(' after 'fn'")
        args: list[Type] = []
        while not self._at(TokenKind.RPAREN):
            args.append(self._parse_type())
            if self._match(TokenKind.COMMA) is None:
                break
        self._expect(TokenKind.RPAREN, what="')' after function-pointer arguments")
        ret = Type(tok.line, tok.column, "None")
        if self._match(TokenKind.ARROW) is not None:
            ret = self._parse_type()
        sig = "fn(" + ", ".join(_type_name_for_type(a) for a in args) + ")"
        if ret.name != "None" or ret.args:
            sig += " -> " + _type_name_for_type(ret)
        return Name(tok.line, tok.column, [sig])

    def _parse_closure(self) -> Closure:
        """Parse a Rust-like closure ``|x: Int| -> Int { x * 3 }``.

        ``|| -> Int { ... }`` is accepted for the zero-parameter form
        (the lexer produces a single ``OR`` token for the two pipes).
        """
        tok = self._peek()
        if tok is not None and tok.kind == TokenKind.OR:
            self._advance()
            params: list[Param] = []
        else:
            tok = self._expect(TokenKind.PIPE, what="'|' opening a closure")
            params = []
            while not self._at(TokenKind.PIPE):
                mutable = self._match(TokenKind.MUT) is not None
                name = self._expect(TokenKind.IDENTIFIER, what="closure parameter name")
                type_: Optional[Type] = None
                if self._match(TokenKind.COLON) is not None:
                    type_ = self._parse_type()
                param = Param(name.line, name.column, str(name.value), type_)
                param.mutable = mutable
                params.append(param)
                if self._match(TokenKind.COMMA) is None:
                    break
            self._expect(TokenKind.PIPE, what="'|' closing closure parameters")
        ret: Optional[Type] = None
        if self._match(TokenKind.ARROW) is not None:
            ret = self._parse_type()
        body = self._parse_block()
        # 与函数体一致: 尾表达式降级成 return (后端只需处理 ReturnStmt)
        self._make_function_tail_return(body)
        return Closure(tok.line, tok.column, params, ret, body)

    def _parse_call_args(self, *, allow_map_literal: bool = False) -> list[Arg]:
        self._advance()  # (
        args: list[Arg] = []
        while not self._at(TokenKind.RPAREN):
            tok = self._peek()
            if tok is None:
                self._error("expected ')' to close the call")
            value = self._parse_expr(allow_map_literal=allow_map_literal)
            args.append(Arg(tok.line, tok.column, value))
            if self._match(TokenKind.COMMA) is None:
                break
        self._expect(TokenKind.RPAREN, what="')' after call arguments")
        return args

    def _parse_vector_literal(self, *, allow_map_literal: bool = False) -> VectorLit:
        tok = self._advance()  # [
        elems: list[Node] = []
        repeat: Optional[int] = None
        if not self._at(TokenKind.RBRACKET):
            elems.append(self._parse_expr(allow_map_literal=allow_map_literal))
            if self._match(TokenKind.SEMICOLON) is not None:
                # bug-35: 定长数组重复字面量 `[x; N]` (Rust 风格): 单个元素
                # 重复 N 次; 计数只入运行时注解, 不进普通序列化字段
                len_tok = self._expect(
                    TokenKind.INTEGER,
                    what="repeat count after ';' in array literal",
                )
                repeat = cast(int, len_tok.value)
            elif self._match(TokenKind.COMMA) is not None:
                while not self._at(TokenKind.RBRACKET):
                    elems.append(
                        self._parse_expr(allow_map_literal=allow_map_literal)
                    )
                    if self._match(TokenKind.COMMA) is None:
                        break
        self._expect(TokenKind.RBRACKET, what="']' after vector literal")
        node = VectorLit(tok.line, tok.column, elems)
        if repeat is not None:
            node._typed_ann["repeat"] = repeat
        return node

    def _parse_map_literal(self) -> MapLit:
        tok = self._advance()  # {
        entries: list[MapEntry] = []
        while not self._at(TokenKind.RBRACE):
            if self._peek() is None:
                self._error("expected '}' after map literal", tok)
            try:
                key = self._parse_expr(allow_map_literal=True)
                self._expect(TokenKind.COLON, what="':' between map key and value")
                value = self._parse_expr(allow_map_literal=True)
            except ParseError as exc:
                self.errors.append(exc)
                self._skip_to_entry_boundary()
                continue
            entries.append(MapEntry(key.line, key.column, key, value))
            if self._match(TokenKind.COMMA) is None:
                break
        try:
            self._expect(TokenKind.RBRACE, what="'}' after map literal")
        except ParseError as exc:
            self.errors.append(exc)
            self._skip_to_entry_boundary(consume_close=True)
        return MapLit(tok.line, tok.column, entries)

    def _parse_struct_construct(
        self, type_: Type, *, allow_map_literal: bool = False
    ) -> StructConstruct:
        tok = self._advance()  # {
        args: list[Node] = []
        while not self._at(TokenKind.RBRACE):
            if self._peek() is None:
                self._error("expected '}' after struct construction", tok)
            try:
                args.append(self._parse_expr(allow_map_literal=allow_map_literal))
            except ParseError as exc:
                self.errors.append(exc)
                self._skip_to_entry_boundary()
                continue
            if self._match(TokenKind.COMMA) is None:
                break
        try:
            self._expect(TokenKind.RBRACE, what="'}' after struct construction")
        except ParseError as exc:
            self.errors.append(exc)
            self._skip_to_entry_boundary(consume_close=True)
        return StructConstruct(type_.line, type_.column, type_, args)

    def _parse_index_or_slice(
        self, obj: Node, *, allow_map_literal: bool = False
    ) -> Node:
        self._advance()  # [
        if self._at(TokenKind.PATH):
            # [::step]
            self._advance()
            step: Optional[Node] = None
            if not self._at(TokenKind.RBRACKET):
                step = self._parse_expr(allow_map_literal=allow_map_literal)
            self._expect(TokenKind.RBRACKET, what="']' after slice")
            return Slice(obj.line, obj.column, obj, None, None, step)
        start: Optional[Node] = None
        if not self._at(TokenKind.COLON):
            start = self._parse_expr(allow_map_literal=allow_map_literal)
        if self._match(TokenKind.COLON) is not None:
            stop: Optional[Node] = None
            if not self._at(TokenKind.COLON) and not self._at(TokenKind.RBRACKET):
                stop = self._parse_expr(allow_map_literal=allow_map_literal)
            step = None
            if self._match(TokenKind.COLON) is not None:
                if not self._at(TokenKind.RBRACKET):
                    step = self._parse_expr(allow_map_literal=allow_map_literal)
            self._expect(TokenKind.RBRACKET, what="']' after slice")
            return Slice(obj.line, obj.column, obj, start, stop, step)
        self._expect(TokenKind.RBRACKET, what="']' after index")
        if start is None:
            self._error("expected index expression", self._peek())
        return Index(obj.line, obj.column, obj, start)


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
