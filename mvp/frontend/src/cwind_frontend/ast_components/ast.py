"""AST node definitions for the CWind frontend (spec: frontend/Grammar.md)."""

from __future__ import annotations

from dataclasses import dataclass, field, fields as _fields
from typing import Any, Optional

from .token import TokenKind

__all__ = [
    "Node",
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
class Type(Node):
    name: str
    args: list["Type"] = field(default_factory=list)
    ref: bool = False


@dataclass
class TypeParam(Node):
    """A generic type parameter (``T`` or ``T: Bound``)."""

    name: str
    bound: Optional["Type"] = None


@dataclass
class Param(Node):
    name: str
    type: Optional["Type"] = None


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


@dataclass
class TraitDecl(Node):
    name: str
    params: list["TypeParam"] = field(default_factory=list)
    methods: list["FnDecl"] = field(default_factory=list)
    pub: bool = False
    assoc_types: list[str] = field(default_factory=list)


@dataclass
class ImplDecl(Node):
    trait: "Type"
    struct: "Type"
    params: list["TypeParam"] = field(default_factory=list)
    methods: list["FnDecl"] = field(default_factory=list)
    assoc_types: list["AssocType"] = field(default_factory=list)


@dataclass
class ExtraDecl(Node):
    struct: "Type"
    params: list["TypeParam"] = field(default_factory=list)
    methods: list["FnDecl"] = field(default_factory=list)


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
