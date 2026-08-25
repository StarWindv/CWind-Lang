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
from pathlib import Path
from collections import deque
from dataclasses import dataclass, field, fields as _dc_fields
from typing import NoReturn, Optional, Union, cast

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
from ..lexer import tokenize, tokenize_file

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
_SOURCE_SUFFIXES = (".wind", ".wd")


@dataclass
class ModuleTrieNode:
    """Prefix-tree node for module path segments.

    A node is a module when ``entry`` points at its source file; descendants
    remain reachable even when an intermediate segment also has a file.
    """

    children: dict[str, "ModuleTrieNode"] = field(default_factory=dict)
    entry: Optional[Path] = None

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


_MODULE_TREE_CACHE: dict[str, tuple[str, ModuleTrieNode]] = {}


def _build_library_trie(root: Path) -> ModuleTrieNode:
    tree = ModuleTrieNode()
    if not root.exists():
        return tree
    files = [
        path for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in _SOURCE_SUFFIXES
    ]
    for path in sorted(files, key=lambda p: str(p).lower()):
        rel = path.relative_to(root)
        parts = [*rel.parts[:-1], rel.stem]
        node = tree
        for part in parts:
            node = node.children.setdefault(part, ModuleTrieNode())
        entry_path = path.resolve()
        if node.entry is not None and node.entry != entry_path:
            raise ValueError(f"ambiguous module file for '{'::'.join(parts)}'")
        node.entry = entry_path
    return tree


def _library_tree(base: Path) -> ModuleTrieNode:
    """Return the module prefix tree; rebuild only after a hash change."""
    root = (base / "libs").resolve()
    key = str(root)
    fingerprint = _library_fingerprint(root)
    cached = _MODULE_TREE_CACHE.get(key)
    if cached is not None and cached[0] == fingerprint:
        return cached[1]
    tree = _build_library_trie(root)
    _MODULE_TREE_CACHE[key] = (fingerprint, tree)
    return tree


_NO_PRELUDE_SENTINEL = object()


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
    therefore anchors at ``<root>`` itself.  When no ancestor provides a
    ``libs/`` directory the entry's own directory is the root.
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
    items is rewritten to the bare item name.
    """
    targets: dict[str, frozenset[str]] = {}
    for alias, items in alias_items.items():
        names = {
            n for n in (
                declaration_name(item) for item in items
            ) if n is not None
        }
        if names:
            targets[alias] = frozenset(names)

    def rewrite(node: object) -> None:
        if isinstance(node, Name):
            if len(node.parts) >= 2:
                head_targets = targets.get(node.parts[0])
                if head_targets is not None and node.parts[-1] in head_targets:
                    node.parts = [node.parts[-1]]
                    return
        elif isinstance(node, EnumPattern):
            if node.path and node.path[0] in targets and node.path[-1] in targets[node.path[0]]:
                node.path = [node.path[-1]]
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
        arguments never contain statements.
        """
        depth = 0
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
            elif tok.kind == TokenKind.SEMICOLON and depth == 1:
                return False
            offset += 1

    def _brace_looks_like_map(self) -> bool:
        """True if the ``{ ... }`` at the cursor has a top-level ``:``.

        Struct construction is positional and never contains a top-level
        colon, so this distinguishes ``Type<T> { a, b }`` from a comparison
        followed by a map literal ``A < B > { "k": v }``.
        """
        depth = 0
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
            elif tok.kind == TokenKind.COLON and depth == 1:
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
        # todo-76: the implicit ``std::prelude::*`` import is resolved before
        # the token loop but merged *after* it, so locally declared names can
        # shadow prelude items (Rust-style) instead of colliding with them.
        auto = self._parse_auto_prelude()
        while self._peek() is not None:
            is_pub_use = (
                self._at(TokenKind.PUB)
                and self._peek(1) is not None
                and self._peek(1).kind == TokenKind.USE
            )
            if self._at(TokenKind.USE) or is_pub_use:
                pub_tok = self._match(TokenKind.PUB)
                use_tok = self._advance()
                try:
                    decl = self._parse_use(
                        use_tok,
                        pub=pub_tok is not None,
                    )
                except ParseError as exc:
                    self.errors.append(exc)
                    self._synchronize_top_level()
                else:
                    items.append(decl)
                    self._append_unique(items, getattr(decl, "loaded_items", []))
                continue
            attrs = self._parse_attributes()
            pub = self._match(TokenKind.PUB) is not None
            try:
                item = self._parse_item(pub)
                self._apply_attributes(item, attrs)
                items.append(item)
            except ParseError as exc:
                self.errors.append(exc)
                self._synchronize_top_level()
                items.append(ErrorStmt(exc.line, exc.column, exc.message))
        if isinstance(auto, UseDecl):
            items = [*self._merge_auto_prelude(items, auto), *items]
        return Program(line, column, items)

    def _merge_auto_prelude(
        self,
        user_items: list[Node],
        auto: UseDecl,
    ) -> list[Node]:
        """Merge the implicit prelude under explicit user definitions.

        Prelude declarations whose name is also declared in the entry file
        are dropped (the local definition shadows them), and extra/impl
        blocks whose owner no longer survives are dropped with it.  This
        keeps projects that define their own ``Option``/``panic`` usable
        while still providing the prelude everywhere else.
        """
        shadowed = {
            name for name in (
                self._declaration_name(node) for node in user_items
            ) if name is not None
        }
        kept: list[Node] = []
        loaded = getattr(auto, "loaded_items", [])
        survivors: set[str] = set()
        for node in loaded:
            name = self._declaration_name(node)
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
        """Name used when selecting one item from a module."""
        value = getattr(node, "name", None)
        if isinstance(value, str):
            return value
        owner = getattr(node, "struct", None)
        if owner is not None:
            value = getattr(owner, "name", None)
            if isinstance(value, str):
                return value
        return None

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
        decls = [n for n in loaded.items if not isinstance(n, UseDecl)]
        uses = [n for n in loaded.items if isinstance(n, UseDecl)]
        by_name: dict[str, list[Node]] = {}
        for d in decls:
            name = self._declaration_name(d)
            if name is not None:
                by_name.setdefault(name, []).append(d)
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
        for u in uses:
            alias_items.setdefault(u.parts[-1], []).extend(
                getattr(u, "loaded_items", [])
            )

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
                if u.item is not None:
                    pub_reexports.add(u.item)
                else:
                    pub_reexports |= getattr(
                        u, "exported_names", frozenset()
                    )
            else:
                transitive_only |= names - local_names

        pub_names = {
            name for name, group in by_name.items()
            if any(getattr(d, "pub", False) for d in group)
        } | pub_reexports

        if item is not None:
            candidates = by_name.get(item, [])
            if not candidates:
                raise ParseError(
                    f"module has no item '{item}'",
                    line,
                    column,
                    category="unknown module item",
                )
            seeds = [
                d for d in candidates
                if getattr(d, "pub", False)
                or isinstance(d, (ExtraDecl, ImplDecl))
            ]
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
            exported_set: set[str] = set()
            for d in decls:
                name = self._declaration_name(d)
                if name is None:
                    continue
                is_block = isinstance(d, (ExtraDecl, ImplDecl))
                if not is_block and not getattr(d, "pub", False):
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
                for dependency in alias_items.get(ref, ()):
                    enqueue(dependency)
        _localize_qualified_refs(order, alias_items, self._declaration_name)
        return order, exported, frozenset(local_names)

    def _append_unique(self, items: list[Node], additions: list[Node]) -> None:
        seen = {id(node) for node in items}
        for node in additions:
            if id(node) not in seen:
                items.append(node)
                seen.add(id(node))

    def _parse_auto_prelude(self) -> Optional[UseDecl]:
        """Resolve the entry file's implicit ``std::prelude::*``.

        A project may not provide ``std`` yet; in that case compilation stays
        compatible with todo-69 behavior.  The result is memoized so every
        root parser for the same project shares the loaded module without
        reparsing it.
        """
        if not self._is_root_source():
            return None
        result = self._auto_prelude_result
        if result is not _NO_PRELUDE_SENTINEL:
            return cast(Optional[UseDecl], result)
        decl = UseDecl(
            1,
            1,
            ["std", "prelude"],
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
        except (OSError, ValueError):
            self._auto_prelude_result = None
            return None
        if resolved is None:
            self._auto_prelude_result = None
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
            self._auto_prelude_result = None
            self.current_use_decl = None
            return None
        finally:
            self.current_use_decl = None
        self._auto_prelude_result = decl
        return decl

    def _is_root_source(self) -> bool:
        return not self._loading and getattr(self, "_is_entry_source", False)

    def _parse_use(self, use_tok: Token, *, pub: bool = False) -> UseDecl:
        """Parse and recursively load ``use a::b;``.

        The declaration remains in the importing module's AST for provenance,
        while declarations loaded from the target file are flattened into the
        root program.  This keeps SA and codegen compatible with the existing
        single-program model instead of introducing a second backend format.
        """
        parts: list[str] = []
        wildcard = False
        while True:
            if self._match(TokenKind.STAR) is not None:
                wildcard = True
                break
            name = self._expect(TokenKind.IDENTIFIER, what="module name or '*'")
            parts.append(str(name.value))
            if self._match(TokenKind.PATH) is None:
                break
        self._expect(TokenKind.SEMICOLON, what="';' after use declaration")

        # A terminal ``*`` must be a wildcard selector.  A bare ``use *;``
        # has no module namespace and is rejected before path resolution.
        # A star in the middle of a path is a grammar error, not an
        # unknown-module error.
        decl = UseDecl(
            use_tok.line,
            use_tok.column,
            parts,
            wildcard=wildcard,
            pub=pub,
        )

        try:
            if wildcard:
                if len(parts) < 1:
                    raise ParseError(
                        "wildcard import requires a module path "
                        "(for example 'std::prelude::*')",
                        use_tok.line,
                        use_tok.column,
                    )
            elif any(part == "*" for part in parts):
                raise ParseError(
                    "'*' may appear only as the final item of an import",
                    use_tok.line,
                    use_tok.column,
                )

            resolved = self._resolve_module_path(
                # A terminal ``*`` never replaces a path segment: the module
                # path is every named part, and ``*`` selects all exports.
                parts,
                wildcard=wildcard,
                line=use_tok.line,
                column=use_tok.column,
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
                use_tok.line,
                use_tok.column,
                end_line=use_tok.end_line,
                end_column=use_tok.end_column,
                category=category,
            ) from exc

        if resolved is None:
            raise ParseError(
                f"cannot find module '{'::'.join(parts)}' "
                f"(searched {self._import_root() / 'libs'})",
                use_tok.line,
                use_tok.column,
                end_line=use_tok.end_line,
                end_column=use_tok.end_column,
                category="unknown module",
            )

        module_path, item_name = resolved
        decl.item = item_name
        if wildcard and item_name is not None:
            raise ParseError(
                f"cannot resolve import path '{'::'.join(parts)}'",
                use_tok.line,
                use_tok.column,
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
                line=use_tok.line,
                column=use_tok.column,
            )
        finally:
            self.current_use_decl = None
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
        module.  ``std`` is a virtual namespace mapped onto ``libs`` on disk.
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
        lookup_parts = parts[1:] if parts[0] == "std" else list(parts)
        tree = _library_tree(self._import_root())
        remaining, entry_path = tree.find_longest(lookup_parts)
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
        # Imported modules do not inject their own prelude.  They resolve
        # their explicit dependencies against the entry project root.
        child._module_cache = self._module_cache
        child._module_order = self._module_order
        child._loading = [*self._loading, key]
        child.import_errors = self.import_errors
        program = child.parse_program()
        self._module_cache[key] = program
        self._module_order.append(key)
        self.errors.extend(child.errors)
        self.import_errors.extend(child.import_errors)
        return program

    # -- attributes ----------------------------------------------------------

    _LINK_ATTR_ARGS = ("name", "kind", "path", "relative")
    _LINK_RELATIVE_MODES = ("cwd", "source")

    def _parse_attributes(self) -> list[tuple[str, dict[str, str], int, int]]:
        """Collect leading ``#[...]`` attribute tokens.

        Returns ``(name, args, line, column)`` tuples where ``args`` maps
        argument names to their string values; the paren-less shorthand
        ``#[name = "value"]`` (todo-62) stores its value under the empty
        key.  Unknown attribute names or non-string values are reported as
        parse errors.
        """
        attrs: list[tuple[str, dict[str, str], int, int]] = []
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
                close = self._expect(
                    TokenKind.RBRACKET, what="']' to close the attribute"
                )
                attrs.append((name, args, hash_tok.line, hash_tok.column))
            except ParseError as exc:
                self.errors.append(exc)
                # Skip to the end of this attribute so parsing can resume.
                while self._peek() is not None:
                    if self._match(TokenKind.RBRACKET) is not None:
                        break
                    self._advance()
        return attrs

    def _apply_attributes(self, item: Node, attrs: list) -> None:
        """Validate collected attributes against the item they precede.

        ``#[link(...)]`` is only valid on ``extern`` blocks (todo-49);
        ``#[link_name = "..."]`` (todo-62) only on declarations *inside*
        an extern block, which are handled by
        :meth:`_apply_extern_item_attributes`.
        """
        if not attrs:
            return
        for name, args, line, column in attrs:
            def fail(message: str) -> NoReturn:
                end = column + len(name)
                raise ParseError(
                    f"#{name}: {message}", line, column,
                    end_line=line, end_column=end,
                )

            if name == "link_name":
                fail(
                    "the 'link_name' attribute can only be applied to "
                    "declarations inside an extern block"
                )
            if name != "link":
                fail(
                    "unsupported attribute (only 'link' / 'link_name' "
                    "are supported)"
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

    def _apply_extern_item_attributes(self, item: Node, attrs: list) -> None:
        """Validate attributes attached to a declaration inside an extern
        block.  Only ``#[link_name = "..."]`` (todo-62) is supported: it
        renames the linked C symbol while the CWind-side name stays as
        declared."""
        if not attrs:
            return
        for name, args, line, column in attrs:
            def fail(message: str) -> NoReturn:
                end = column + len(name)
                raise ParseError(
                    f"#{name}: {message}", line, column,
                    end_line=line, end_column=end,
                )

            if name != "link_name":
                fail(
                    "unsupported attribute inside an extern block "
                    "(only 'link_name' is supported)"
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
        )

    def _parse_impl(self) -> ImplDecl:
        tok = self._advance()  # impl
        params = self._parse_generic_params()
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
            tok.line, tok.column, trait, struct, params, methods, assoc_types
        )

    def _parse_extra(self) -> ExtraDecl:
        tok = self._advance()  # extra
        params = self._parse_generic_params()
        struct = self._parse_type()
        self._expect(TokenKind.LBRACE, what="'{' after extra header")
        methods: list[FnDecl] = []
        while not self._at(TokenKind.RBRACE):
            method_pub = self._match(TokenKind.PUB) is not None
            method_static = self._match(TokenKind.STATIC) is not None
            methods.append(self._parse_fn(pub=method_pub, static=method_static))
        self._advance()  # }
        return ExtraDecl(tok.line, tok.column, struct, params, methods)

    def _parse_group(self) -> GroupDecl:
        tok = self._advance()  # group
        name = self._expect(TokenKind.IDENTIFIER, what="group name")
        params: list[Param] = []
        struct: Optional[str] = None
        if self._at(TokenKind.LPAREN):
            params = self._parse_params()
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
    ) -> FnDecl:
        tok = self._advance()  # fn
        name = self._expect(TokenKind.IDENTIFIER, what="function name")
        type_params = self._parse_generic_params()
        params = self._parse_params()
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
        if decl.body is not None:
            self._make_function_tail_return(decl.body)
        return decl

    def _parse_extern_block(self, *, pub: bool = False) -> ExternBlock:
        """Parse a C-FFI declaration block: ``extern "C" { fn ...; }``.

        Contained items are body-less function signatures
        (``fn name(params) -> Ret;``) and, since todo-56, extern static
        bindings (``static [mut] NAME: Type;``).  Each item may carry a
        ``#[link_name = "..."]`` attribute (todo-62) renaming its C
        symbol.  The block's ABI string is recorded on each ``FnDecl`` as
        ``extern_abi`` so the backend can emit raw-C declarations and
        calls.
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
                if self._at(TokenKind.STATIC):
                    static = self._parse_extern_static(pub=pub)
                    self._apply_extern_item_attributes(static, attrs)
                    statics.append(static)
                    continue
                fn = self._parse_fn(pub=pub, body_required=False)
                fn.extern_abi = abi
                self._apply_extern_item_attributes(fn, attrs)
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

    def _parse_params(self) -> list[Param]:
        """Parse a parameter list.

        Mutable receivers use Rust's postfix ordering ``&mut self``
        (todo-47); the retired ``mut &self`` form is rejected with a
        pointer to the new syntax.  Plain bindings keep ``mut x: T``.
        """
        self._expect(TokenKind.LPAREN, what="'(' before parameter list")
        params: list[Param] = []
        while not self._at(TokenKind.RPAREN):
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
        return params

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
            inner = self._parse_type()
            return Type(
                amp.line,
                amp.column,
                inner.name,
                inner.args,
                ref=True,
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
            # 关联类型路径: Self::Item (暂不支持带实参的路径类型)
            parts = [str(tok.value)]
            while self._at(TokenKind.PATH):
                self._advance()
                part = self._expect(
                    TokenKind.IDENTIFIER, what="name after '::' in type"
                )
                parts.append(str(part.value))
            return Type(tok.line, tok.column, "::".join(parts))
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
                if len(parts) != 2:
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
        if self._at(TokenKind.IDENTIFIER, value="in"):
            self._error("expected iteration variable before 'in'", self._peek())
        var = self._expect(TokenKind.IDENTIFIER, what="loop variable")
        in_tok = self._peek()
        if not (in_tok is not None and in_tok.kind == TokenKind.IDENTIFIER and in_tok.value == "in"):
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
        node = self._parse_unary(allow_map_literal=allow_map_literal)
        while self._at(TokenKind.PIPE):
            op = self._advance()
            node = BinOp(
                node.line,
                node.column,
                node,
                op.kind,
                self._parse_unary(allow_map_literal=allow_map_literal),
            )
        return node

    def _parse_unary(self, *, allow_map_literal: bool = False) -> Node:
        tok = self._peek()
        if tok is not None and tok.kind in _UNARY_OPS:
            op = self._advance()
            return UnaryOp(
                tok.line,
                tok.column,
                op.kind,
                self._parse_unary(allow_map_literal=allow_map_literal),
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
        while not self._at(TokenKind.RBRACKET):
            elems.append(self._parse_expr(allow_map_literal=allow_map_literal))
            if self._match(TokenKind.COMMA) is None:
                break
        self._expect(TokenKind.RBRACKET, what="']' after vector literal")
        return VectorLit(tok.line, tok.column, elems)

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
) -> ParseResult:
    """Parse a token list, collecting every :class:`ParseError`.

    The parser recovers by skipping to statement/declaration boundaries, so a
    single run reports as many independent errors as possible.

    ``source_path`` (todo-76) anchors the entry file: it locates the project
    root (the nearest ancestor owning ``libs/``) and enables the implicit
    ``std::prelude::*`` import.  Token lists without a source location (stdin,
    in-memory test sources) keep the legacy no-prelude behavior.
    """
    parser = Parser(tokens)
    entry_path = getattr(parser, "source_path", None)
    if source_path is not None:
        parser.source_path = str(Path(source_path).resolve())
        entry_path = parser.source_path
    parser._is_entry_source = source_path is not None
    parser._IMPORT_ROOTS_BASE = _entry_project_root(entry_path)
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
