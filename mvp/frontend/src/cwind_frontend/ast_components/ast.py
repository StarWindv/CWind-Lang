"""AST node definitions for the CWind frontend (spec: frontend/Grammar.md)."""

from __future__ import annotations

from dataclasses import dataclass, field, fields as _fields
from typing import Any, Optional

from .token import TokenKind

__all__ = [
    "Node",
    "UseDecl",
    "ModDecl",
    "Program",
    "Type",
    "TypeParam",
    "Param",
    "ConstDecl",
    "TypeDecl",
    "Field",
    "StructDecl",
    "Variant",
    "EnumDecl",
    "EnumPattern",
    "Pattern",
    "AssocType",
    "WildcardPattern",
    "BindPattern",
    "LitPattern",
    "TuplePattern",
    "StructPatternField",
    "StructPattern",
    "ErrorStmt",
    "FnDecl",
    "ExternBlock",
    "ExternStatic",
    "TraitDecl",
    "ImplDecl",
    "ExtraDecl",
    "Distribution",
    "GroupDecl",
    "GroupApply",
    "Block",
    "BoolLit",
    "LetStmt",
    "ReturnStmt",
    "BreakStmt",
    "ContinueStmt",
    "ElifBranch",
    "IfStmt",
    "IfLetBranch",
    "IfLetStmt",
    "MatchArm",
    "MatchStmt",
    "WhileStmt",
    "ForStmt",
    "ExprStmt",
    "IntLit",
    "FloatLit",
    "StrLit",
    "Name",
    "Attribute",
    "Arg",
    "Call",
    "Index",
    "Slice",
    "BinOp",
    "UnaryOp",
    "Assign",
    "VectorLit",
    "MapEntry",
    "MapLit",
    "TupleLit",
    "StructConstruct",
    "Closure",
    "ast_dump",
]


def _jsonable(value: Any, include_meta: bool = False) -> Any:
    if isinstance(value, Node):
        return value.to_dict(include_meta=include_meta)
    if isinstance(value, TokenKind):
        return value.value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v, include_meta) for v in value]
    return value


@dataclass
class Node:
    """Base class: every AST node carries its 1-based source position."""

    line: int
    column: int

    def __post_init__(self) -> None:
        # Typed-AST metadata.  Deliberately *not* dataclass fields so that
        # positional constructors in the parser keep their existing field
        # order and plain ``--parse`` output stays byte-for-byte stable.
        self._typed_id: Optional[int] = None
        self._typed_ann: dict[str, Any] = {}
        self._tail_expr: bool = False

    def to_dict(self, include_meta: bool = False) -> dict:
        """Serialize the node (and children) to a JSON-friendly dict."""
        d: dict = {"kind": type(self).__name__, "line": self.line, "column": self.column}
        if include_meta:
            d["id"] = self._typed_id
            d["ann"] = self._typed_ann
        for f in _fields(self):
            if f.name in ("line", "column"):
                continue
            d[f.name] = _jsonable(getattr(self, f.name), include_meta)
        return d


# -- top-level declarations ------------------------------------------------


@dataclass
class Program(Node):
    items: list[Node] = field(default_factory=list)


@dataclass
class UseDecl(Node):
    """A module import: ``use std::option;``.

    ``parts`` is the dotted module path (``std`` + ``option`` here).  The
    frontend resolves it to a CWind source file, recursively imports that
    file's own dependencies, and records the resolved path in ``module`` so
    typed-AST consumers can audit provenance without re-running resolution.

    ``alias`` (todo-124) is the ``as`` rename: ``use a::b as c;`` registers
    the module namespace under ``c`` and ``use m::item as c;`` makes bare
    ``c`` references denote ``item``.  ``None`` keeps the natural last-path-
    segment alias.
    """

    parts: list[str]
    module: Optional[str] = None
    wildcard: bool = False
    item: Optional[str] = None
    auto: bool = False
    pub: bool = False
    alias: Optional[str] = None


@dataclass
class ModDecl(Node):
    """todo-107: a ``mod`` declaration (Rust ``ItemKind::Mod``).

    Two forms:

    - external: ``[pub [vis]] mod name;`` — ``body`` is ``None``; the module
      file (``name.wind`` or ``name/mod.wind``) is addressed through the
      module tree only when its parent's ``mod.wind`` declares it, and a
      ``pub`` declaration re-exports the submodule to importers (todo-107).
    - inline: ``[pub [vis]] mod name { ...items... }`` — ``body`` carries
      the nested items; they join the flat program tagged with the extended
      module path (``<parent path>::name``).

    ``visibility`` (todo-119 family) is ``None`` for plain ``pub``/private
    declarations, otherwise one of ``"self"`` / ``"super"`` / ``"crate"`` /
    ``"std"`` / ``"in"``; ``vis_path`` carries the path segments of
    ``pub(super::super::x)`` / ``pub(in super::x)`` style qualifiers.
    """

    name: str
    body: Optional["Block"] = None
    pub: bool = False
    visibility: Optional[str] = None
    vis_path: Optional[list[str]] = None


@dataclass
class Type(Node):
    name: str
    args: list["Type"] = field(default_factory=list)
    ref: bool = False
    # bug-46: ``&mut T`` —— 可变借用; 共享借用 ``&T`` 保持 False。
    mut: bool = False
    # todo-164: associated-type bindings written in bound position
    # (``T: Iterator<Item = Int32>``).  ``args`` still carries the
    # positional generic arguments before ``=``; each binding is an
    # ``AssocType(name, type)`` pair.
    bindings: list["AssocType"] = field(default_factory=list)


@dataclass
class TypeParam(Node):
    """A generic type parameter (``T`` or ``T: Bound``).

    todo-164: ``default`` carries the ``= Default`` part of
    ``T: Bound = Default`` (Rust generic-parameter defaults); ``None``
    when the parameter has no default.
    """

    name: str
    bound: Optional["Type"] = None
    default: Optional["Type"] = None


@dataclass
class Param(Node):
    name: str
    type: Optional["Type"] = None
    mutable: bool = False


@dataclass
class ConstDecl(Node):
    name: str
    type: "Type"
    value: Node
    pub: bool = False


@dataclass
class TypeDecl(Node):
    name: str
    base: "Type"
    where: Optional["Block"] = None
    pub: bool = False
    params: list["TypeParam"] = field(default_factory=list)


@dataclass
class Field(Node):
    name: str
    type: "Type"
    pub: bool = False
    static: bool = False
    validation: Optional["Block"] = None
    initializer: Optional[Node] = None


@dataclass
class StructDecl(Node):
    name: str
    params: list["TypeParam"] = field(default_factory=list)
    fields: list["Field"] = field(default_factory=list)
    pub: bool = False


@dataclass
class Variant(Node):
    name: str
    value: Optional[int] = None
    fields: list["Type"] = field(default_factory=list)


@dataclass
class EnumDecl(Node):
    name: str
    variants: list["Variant"] = field(default_factory=list)
    pub: bool = False
    params: list["TypeParam"] = field(default_factory=list)


@dataclass
class FnDecl(Node):
    name: str
    type_params: list["TypeParam"] = field(default_factory=list)
    params: list["Param"] = field(default_factory=list)
    return_type: Optional["Type"] = None
    body: Optional["Block"] = None
    pub: bool = False
    static: bool = False
    which: Optional[str] = None
    extern_abi: Optional[str] = None  # set for fns declared in an extern block
    # todo-62: rename the linked C symbol (`#[link_name = "..."]`); the
    # CWind-side name stays whatever the fn declares
    link_name: Optional[str] = None
    # todo-87: the parameter list ends with a variadic ``...`` marker
    # (extern blocks only); at least one fixed parameter must precede it.
    variadic: bool = False
    # todo-132: for ``extern "CWind"`` method declarations in the form
    # ``fn Vector<T>::push_back(&mut self, ...)``, this holds the owner
    # type (e.g. ``Type("Vector", args=[Type("T")])``).  ``None`` for
    # plain functions or ``extern "C"`` declarations.
    cwind_owner: Optional["Type"] = None


@dataclass
class ExternBlock(Node):
    """A C-FFI declaration block: ``extern "C" { fn ...; }``.

    ``link_*`` fields carry the (optional) ``#[link(...)]`` attribute:
    ``link_name`` links a library by name, ``link_kind`` is ``static`` /
    ``dylib``, and ``link_path`` points at a concrete library file.
    ``link_relative`` (todo-63) anchors ``link_path``: ``None`` / ``cwd``
    resolves it against the compiler's working directory (default),
    ``source`` against the directory of the compiled source file.
    """

    abi: str = "C"
    fns: list["FnDecl"] = field(default_factory=list)
    statics: list["ExternStatic"] = field(default_factory=list)
    # todo-132: ``extern "CWind"`` blocks may also declare built-in types.
    types: list["TypeDecl"] = field(default_factory=list)
    pub: bool = False
    link_name: Optional[str] = None
    link_kind: Optional[str] = None
    link_path: Optional[str] = None
    link_relative: Optional[str] = None


@dataclass
class ExternStatic(Node):
    """An extern static binding (todo-56): ``static [mut] NAME: Type;``.

    Binds a C global variable into CWind.  Without ``mut`` the binding is
    read-only from CWind code (Rust ``extern static`` semantics).
    ``link_name`` (todo-62) renames the underlying C symbol.
    """

    name: str = ""
    type: Optional["Type"] = None
    mutable: bool = False
    pub: bool = False
    link_name: Optional[str] = None


@dataclass
class AssocTypeDecl(Node):
    """todo-164: a trait's associated type declaration.

    ``type Item;`` declares the name; ``type Item: Bound`` additionally
    bounds it (the impl must provide a type satisfying ``Bound``).
    """

    name: str
    bound: Optional["Type"] = None


@dataclass
class TraitDecl(Node):
    name: str
    params: list[TypeParam] = field(default_factory=list)
    methods: list[FnDecl] = field(default_factory=list)
    pub: bool = False
    assoc_types: list[str] = field(default_factory=list)
    # todo-164: ``type Item: Bound`` declarations carrying their bound.
    # The plain-name list above stays in sync (names only) so existing
    # consumers keep working.
    assoc_type_decls: list["AssocTypeDecl"] = field(default_factory=list)
    # todo-156: supertrait list from ``pub trait B<T: A>: A, Clone`` — the
    # traits ``B`` inherits (each a fully-resolved type so generic supertraits
    # like ``Into<T>`` carry their args).  ``to_dict`` emits it automatically.
    supertraits: list["Type"] = field(default_factory=list)


@dataclass
class ImplDecl(Node):
    trait: "Type"
    struct: "Type"
    params: list["TypeParam"] = field(default_factory=list)
    methods: list["FnDecl"] = field(default_factory=list)
    assoc_types: list["AssocType"] = field(default_factory=list)
    # todo-156: a *negative* impl ``impl<T> !Trait for Type`` (Rust's
    # ``impl !Send for T``).  Records "this type definitely does NOT implement
    # the trait"; it carries no methods and must not enter the positive impl /
    # method-binding tables.
    negative: bool = False


@dataclass
class ExtraDecl(Node):
    struct: "Type"
    params: list["TypeParam"] = field(default_factory=list)
    methods: list["FnDecl"] = field(default_factory=list)
    # todo-122: associated constants declared in the block
    # (``extra Point { const MAX: Int32 = 99; ... }``), addressed as
    # ``Point::MAX`` / ``Self::MAX``; read-only like top-level consts.
    consts: list["ConstDecl"] = field(default_factory=list)


@dataclass
class Distribution(Node):
    subject: str
    type: "Type"
    subject_self: bool = False


@dataclass
class GroupDecl(Node):
    name: str
    params: list["Param"] = field(default_factory=list)
    struct: Optional[str] = None
    distributions: list["Distribution"] = field(default_factory=list)


@dataclass
class GroupApply(Node):
    group: str
    struct: str
    fields: list[str] = field(default_factory=list)


@dataclass
class AssocType(Node):
    """An associated type binding in an impl: ``type Item = Int32;``."""

    name: str
    type: "Type"


# -- patterns --------------------------------------------------------------


@dataclass
class Pattern(Node):
    """Base class for match / if-let patterns."""


@dataclass
class WildcardPattern(Pattern):
    """The wildcard pattern ``_``: matches anything, binds nothing."""


@dataclass
class BindPattern(Pattern):
    """A binding pattern: ``name`` matches anything and binds the value."""

    name: str


@dataclass
class LitPattern(Pattern):
    """A literal pattern (integer / float / string / bool)."""

    value: Node


@dataclass
class TuplePattern(Pattern):
    """A tuple pattern: ``(p1, p2, ...)`` or ``()``."""

    elems: list["Pattern"] = field(default_factory=list)


@dataclass
class StructPatternField(Node):
    """One field of a struct pattern.

    ``pattern`` is ``None`` for the shorthand form ``Point { x }``, which
    binds the field's value to a variable named after the field.
    """

    name: str
    pattern: Optional["Pattern"] = None


@dataclass
class StructPattern(Pattern):
    """A struct pattern: ``Point { x, y: 1, .. }``."""

    type: "Type"
    fields: list["StructPatternField"] = field(default_factory=list)
    rest: bool = False


@dataclass
class EnumPattern(Pattern):
    """An enum variant pattern: ``Option::Some(x)`` / ``Color::Red``."""

    path: list[str] = field(default_factory=list)
    elems: list["Pattern"] = field(default_factory=list)


# -- statements ------------------------------------------------------------


@dataclass
class Block(Node):
    stmts: list[Node] = field(default_factory=list)


@dataclass
class BoolLit(Node):
    """A boolean literal (``true`` / ``false``)."""

    value: bool
    raw: str = ""


@dataclass
class LetStmt(Node):
    name: str
    type: Optional["Type"] = None
    value: Optional[Node] = None
    mutable: bool = False


@dataclass
class ReturnStmt(Node):
    value: Optional[Node] = None


@dataclass
class BreakStmt(Node):
    """Exit the innermost enclosing loop (``break;``)."""


@dataclass
class ContinueStmt(Node):
    """Skip to the next iteration of the innermost loop (``continue;``)."""


@dataclass
class ElifBranch(Node):
    cond: Node
    body: "Block"


@dataclass
class IfStmt(Node):
    cond: Node
    then: "Block"
    elifs: list["ElifBranch"] = field(default_factory=list)
    else_: Optional["Block"] = None


@dataclass
class IfLetBranch(Node):
    """An ``elif`` branch of an if-let statement.

    Plain ``elif (cond)`` branches have ``cond`` set and ``pattern`` /
    ``value`` left ``None``; ``elif let pattern = value`` branches have
    ``cond`` set to ``None``.
    """

    cond: Optional[Node] = None
    pattern: Optional["Pattern"] = None
    value: Optional[Node] = None
    body: "Block" = None  # type: ignore[assignment]


@dataclass
class IfLetStmt(Node):
    """``if let pattern = value { ... } [elif ...] [else ...]``."""

    pattern: "Pattern"
    value: Node
    then: "Block"
    elifs: list["IfLetBranch"] = field(default_factory=list)
    else_: Optional["Block"] = None


@dataclass
class MatchArm(Node):
    """One match arm: ``pattern [if guard] => body``.

    ``body`` is a :class:`Block` for statement-style match arms
    (``=> { ... }``) or an expression node for Rust-style match expressions
    (``=> expr``).
    """

    pattern: "Pattern"
    guard: Optional[Node] = None
    body: "Node" = None  # type: ignore[assignment]


@dataclass
class MatchStmt(Node):
    """``match (value) { arm, arm, ... }``."""

    subject: Node
    arms: list["MatchArm"] = field(default_factory=list)


@dataclass
class WhileStmt(Node):
    cond: Node
    body: "Block"


@dataclass
class LetChainSeg(Node):
    """todo-165: one ``&&``-separated operand of a while-let chain.

    ``pattern`` is set for a ``let P = E`` segment and ``None`` for a
    plain boolean condition segment; ``value`` is the segment's
    expression either way.
    """

    pattern: Optional["Pattern"] = None
    value: "Node" = None  # type: ignore[assignment]


@dataclass
class WhileLetStmt(Node):
    """``while let P = E [&& (let P2 = E2 | B)]* { ... }`` (todo-165).

    The loop re-evaluates every segment each iteration: a ``let``
    segment exits the loop when its pattern fails to match, a boolean
    segment exits when it evaluates false (Rust 2024 let-chain
    semantics).  Bindings from all segments are visible in ``body``.
    """

    segments: list["LetChainSeg"] = field(default_factory=list)
    body: "Block" = None  # type: ignore[assignment]


@dataclass
class ForStmt(Node):
    var: str
    iterable: Node
    body: "Block"
    type: Optional["Type"] = None
    paren_style: bool = False


@dataclass
class ExprStmt(Node):
    expr: Node


@dataclass
class ErrorStmt(Node):
    """Placeholder for a construct that failed to parse (recovery)."""

    message: str = ""


# -- expressions -----------------------------------------------------------


@dataclass
class IntLit(Node):
    value: int
    raw: str = ""


@dataclass
class FloatLit(Node):
    value: float
    raw: str = ""


@dataclass
class StrLit(Node):
    value: str
    raw: str = ""


@dataclass
class Name(Node):
    parts: list[str] = field(default_factory=list)


@dataclass
class Attribute(Node):
    obj: Node
    name: str


@dataclass
class Arg(Node):
    value: Node
    unpack: bool = False


@dataclass
class Call(Node):
    callee: Node
    args: list["Arg"] = field(default_factory=list)


@dataclass
class Index(Node):
    obj: Node
    index: Node


@dataclass
class Slice(Node):
    obj: Node
    start: Optional[Node] = None
    stop: Optional[Node] = None
    step: Optional[Node] = None


@dataclass
class BinOp(Node):
    left: Node
    op: TokenKind
    right: Node


@dataclass
class UnaryOp(Node):
    op: TokenKind
    operand: Node
    # bug-46: ``&mut expr`` —— 可变借用表达式 (op 仍是 AMP);
    # SA 据此要求操作数是可变绑定, 后端把借用当同一句柄, 不读此位。
    mutable: bool = False


@dataclass
class CastExpr(Node):
    """todo-17: ``operand as TargetType`` numeric conversion."""

    operand: Node
    target: "Type"


@dataclass
class Assign(Node):
    target: Node
    op: TokenKind
    value: Node


@dataclass
class VectorLit(Node):
    elems: list[Node] = field(default_factory=list)


@dataclass
class MapEntry(Node):
    key: Node
    value: Node


@dataclass
class MapLit(Node):
    entries: list["MapEntry"] = field(default_factory=list)


@dataclass
class TupleLit(Node):
    elems: list[Node] = field(default_factory=list)


@dataclass
class StructConstruct(Node):
    type: "Type"
    args: list[Node] = field(default_factory=list)


@dataclass
class Closure(Node):
    """A Rust-like anonymous function: ``|x: Int| -> Int { x + 1 }``."""

    params: list["Param"] = field(default_factory=list)
    return_type: Optional["Type"] = None
    body: "Block" = None  # type: ignore[assignment]


def ast_dump(node: Node, indent: int = 0) -> str:
    """Render an AST as an indented, human-readable tree."""
    pad = "  " * indent
    scalars: list[str] = []
    child_lines: list[str] = []
    for f in _fields(node):
        if f.name in ("line", "column"):
            continue
        value = getattr(node, f.name)
        if isinstance(value, Node):
            child_lines.append(f"{pad}{f.name}:")
            child_lines.append(ast_dump(value, indent + 1))
        elif isinstance(value, list):
            if any(isinstance(v, Node) for v in value):
                child_lines.append(f"{pad}{f.name}:")
                for v in value:
                    if isinstance(v, Node):
                        child_lines.append(ast_dump(v, indent + 1))
                    else:
                        child_lines.append(f"{pad}  {v!r}")
            else:
                scalars.append(f"{f.name}={value!r}")
        else:
            scalars.append(f"{f.name}={value!r}")
    header = pad + type(node).__name__
    if scalars:
        header += " " + " ".join(scalars)
    return "\n".join([header, *child_lines])


def _type_name_for_type(t: "Type") -> str:
    name = t.name
    if t.args:
        name += "<" + ", ".join(_type_name_for_type(a) for a in t.args) + ">"
    if t.ref:
        name = "&" + name
    return name
