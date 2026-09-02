"""Shared parser machinery: the single definition point for every
module-level name used by the parser mixins (``core``/``items``/``attrs``/
``decls``/``types``/``stmts``/``exprs``)."""

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

    todo-163: ``reexports`` maps a local alias -> ``(tree_kind, parts)``
    for ``[pub] use path::to::module;`` re-exports found in this file (only
    module targets with a ``pub`` spelling register; item re-exports and
    private uses ride the export surface).  ``parts`` is tree-absolute and
    ``tree_kind`` names the tree it lives in.  ``ModuleTree.follow`` walks
    these edges so a facade layer's re-exported modules resolve to their
    real position in the tree (Rust 2018 re-export semantics).
    """

    children: dict[str, "ModuleTrieNode"] = field(default_factory=dict)
    entry: Optional[Path] = None
    pub: bool = True
    reexports: dict[str, tuple[str, list[str]]] = field(default_factory=dict)

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


def _scan_reexports(
    path: Path,
) -> Optional[list[tuple[str, list[str], bool, int, int]]]:
    """todo-163: the ``[pub] use path::to::name [as alias];`` re-exports of
    one module file.

    A lightweight token scan (no full parse — this runs inside trie
    construction) collects ``(local_name, target_parts, pub, line, column)``
    tuples.  ``local_name`` is the trailing segment of the target (or the
    ``as`` alias).  Group imports (``use m::{a, b};``) and wildcards are
    skipped: they do not introduce a single module alias.  ``None`` means
    the file could not be read; an empty list means "re-exports nothing".
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return None
    try:
        tokens = tokenize(text)
    except Exception:
        # A lexically broken file must not crash the tree build; the error
        # surfaces when the file is actually parsed as a module.
        return []
    out: list[tuple[str, list[str], bool, int, int]] = []
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        pub = False
        j = i
        if tok.kind == TokenKind.PUB:
            j = i + 1
            # Skip one ``pub(<qual>)`` group like the mod scanner does.
            if j < n and tokens[j].kind == TokenKind.LPAREN:
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
            if j >= n or tokens[j].kind != TokenKind.USE:
                i += 1
                continue
            pub = True
        if tokens[j].kind != TokenKind.USE:
            i += 1
            continue
        j += 1  # use
        parts: list[str] = []
        ok = False
        alias: Optional[str] = None
        line = tokens[j].line if j < n else tok.line
        column = tokens[j].column if j < n else tok.column
        while j < n:
            t = tokens[j]
            if t.kind == TokenKind.IDENTIFIER:
                parts.append(str(t.value))
                j += 1
                if j < n and tokens[j].kind == TokenKind.PATH:
                    j += 1
                    continue
                if (
                    j < n
                    and tokens[j].kind == TokenKind.AS
                    and j + 1 < n
                    and tokens[j + 1].kind == TokenKind.IDENTIFIER
                ):
                    alias = str(tokens[j + 1].value)
                    j += 2
                if j < n and tokens[j].kind == TokenKind.SEMICOLON:
                    ok = True
                break
            break
        if ok and len(parts) >= 2:
            out.append(
                (alias or parts[-1], parts, pub, line, column)
            )
        i = j if j > i else i + 1
    return out


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

    # todo-163 pass 2: register ``[pub] use ...::module`` re-exports as
    # alias edges on the node of the file that spells them.  Only *module*
    # targets register (the target must land on a real module file); item
    # re-exports keep riding the export surface.  Private uses do not
    # re-export (Rust semantics: only ``pub use`` extends visibility).

    def register(tree: ModuleTrieNode, kind: str) -> None:
        # Scan every file module once; chain re-exports (a facade
        # re-exporting another facade's alias) need earlier alias edges
        # registered first, so unresolved entries retry in later waves.
        stack: list[tuple[ModuleTrieNode, Path]] = [(tree, tree.entry)]
        pending: list[
            tuple[ModuleTrieNode, str, list[str], bool]
        ] = []
        while stack:
            n, entry = stack.pop()
            for c in n.children.values():
                if c.entry is not None:
                    stack.append((c, c.entry))
            if entry is None:
                continue
            scanned = _scan_reexports(entry)
            for local, target, pub, _line, _col in scanned:
                if pub:
                    pending.append((n, local, target, pub))
        def walk_with_reexports(
            root: ModuleTrieNode, parts: list[str], kind: str
        ) -> Optional[tuple[Path, str]]:
            """Follow children *and* already-registered alias edges."""
            node = root
            i = 0
            while i < len(parts):
                seg = parts[i]
                nxt = node.children.get(seg)
                if nxt is not None:
                    node = nxt
                    i += 1
                    continue
                re = node.reexports.get(seg)
                if re is not None:
                    re_kind, target = re
                    root2 = trees.std if re_kind == "std" else trees.crate
                    return walk_with_reexports(
                        root2, [*target, *parts[i + 1:]], re_kind
                    )
                return None
            if node.entry is None:
                return None
            return node.entry, kind

        for _wave in range(16):
            still: list[tuple[ModuleTrieNode, str, list[str], bool]] = []
            progressed = False
            for n, local, target, pub in pending:
                head = target[0]
                if head == "std":
                    hit = walk_with_reexports(trees.std, target[1:], "std")
                elif head == "crate":
                    hit = walk_with_reexports(
                        trees.crate, target[1:], "crate"
                    )
                else:
                    hit = walk_with_reexports(trees.crate, target, "crate")
                    if hit is None:
                        hit = walk_with_reexports(trees.std, target, "std")
                if hit is None:
                    still.append((n, local, target, pub))
                    continue
                entry_path, hit_kind = hit
                hit_tree = trees.std if hit_kind == "std" else trees.crate
                abs_parts = self_parts_of(hit_tree, entry_path)
                if abs_parts is None:
                    still.append((n, local, target, pub))
                    continue
                n.reexports[local] = (hit_kind, abs_parts)
                progressed = True
            pending = still
            if not progressed or not pending:
                break

    def self_parts_of(
        tree: ModuleTrieNode, path: Path
    ) -> Optional[list[str]]:
        """The tree-absolute module parts of the node at *path*.

        ``None`` when *path* is not in *tree*.
        """
        if tree.entry == path:
            return []
        stack: list[tuple[ModuleTrieNode, list[str]]] = [(tree, [])]
        while stack:
            n, prefix = stack.pop()
            for c_name, c in n.children.items():
                if c.entry == path:
                    return [*prefix, c_name]
                stack.append((c, [*prefix, c_name]))
        return None

    for root in roots:
        if not root.directory.exists() or root.entry is None:
            continue
        register(trees.std if root.kind == "std" else trees.crate, root.kind)
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

    def follow(
        self, start: ModuleTrieNode, parts: list[str], depth: int = 0
    ) -> Optional[tuple[str, list[str]]]:
        """todo-163: rewrite *parts* by following pub-use module re-exports.

        Walks *parts* through *start*'s children; at a dead segment the
        node's registered re-export alias is substituted (its target is
        stored tree-absolute) and the walk restarts there, so chains of
        facades resolve.  Returns ``(tree_kind, absolute_parts)`` or
        ``None`` when the path dead-ends without an alias edge (the caller
        keeps the original spelling).  ``tree_kind`` re-anchors the
        resolution into the std or crate tree.
        """
        if depth > 16:
            return None
        node = start
        for i, seg in enumerate(parts):
            nxt = node.children.get(seg)
            if nxt is not None:
                node = nxt
                continue
            re = node.reexports.get(seg)
            if re is not None:
                kind, target = re[0], re[1]
                rewritten = [*target, *parts[i + 1:]]
                head = rewritten[0] if rewritten else ""
                if head == "std":
                    t2, rest2 = self.std, rewritten[1:]
                elif head == "crate":
                    t2, rest2 = self.crate, rewritten[1:]
                else:
                    t2, rest2 = start, rewritten
                deeper = self.follow(t2, rest2, depth + 1)
                return deeper if deeper is not None else (kind, rewritten)
            return None
        return None  # consumed without an alias edge: no rewrite needed


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
    *,
    flush: bool = False,
) -> tuple[dict[str, list[tuple[str, str]]], dict[str, Program]]:
    """Build (lazily, once per import root + library fingerprint) the index
    of every trait impl in the library tree.

    ``module_cache`` lets the caller share parsed Programs with its own
    import machinery — the entry parser passes ``self._module_cache`` so a
    file pre-parsed here is not re-parsed (and re-instantiated) by the
    normal import path: duplicate top-level node instances would otherwise
    land in the root program and SA would reject them as duplicate
    definitions.

    todo-171: ``flush=True`` drops the per-process Program caches before
    the lookup.  The cached Programs are *mutable* AST nodes — a previous
    SA run in the same process rewrites Type nodes in place (alias
    expansion, impl-target canonicalization), and those mutations used to
    survive into the next compile that shared the same libs path.  The
    fingerprint only tracks file content, not in-memory AST edits, so
    mtime-stable reruns kept poisoned nodes.  Callers compile **once per
    process boundary**: the CLI/entry passes ``flush=True``; within one
    compile the cache still shares node instances (the ``_scope_flat``
    guard relies on that).  The prefix-trie cache (``_MODULE_TREE_CACHE``)
    holds immutable path data only and is intentionally kept.
    """
    key = str(Path(base).resolve())
    if flush:
        _IMPL_REGISTRY_CACHE.clear()
        _IMPL_REGISTRY_BOOT_CACHE.clear()
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
        elif isinstance(stmt, WhileLetStmt):
            # todo-165: all chain bindings share the body scope.
            binds: set[str] = set()
            for seg in stmt.segments:
                walk_expr(seg.value, frozen)
                if seg.pattern is not None:
                    binds |= walk_pattern(seg.pattern, frozen)
            walk_block(stmt.body, frozenset(bound | binds))
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
            LetStmt, ForStmt, WhileStmt, WhileLetStmt, IfStmt, IfLetStmt,
            MatchStmt, ReturnStmt, ExprStmt, BreakStmt, ContinueStmt,
            ErrorStmt,
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
