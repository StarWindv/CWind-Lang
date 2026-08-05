"""AST node definitions for the CWind frontend (spec: frontend/Grammar.md)."""

from __future__ import annotations

from dataclasses import dataclass, field, fields as _fields
from typing import Any, Optional

from .token import TokenKind

__all__ = [
    "Node",
    "Program",
    "Type",
    "Param",
    "ConstDecl",
    "TypeDecl",
    "Field",
    "StructDecl",
    "Variant",
    "EnumDecl",
    "FnDecl",
    "TraitDecl",
    "ImplDecl",
    "ExtraDecl",
    "Distribution",
    "GroupDecl",
    "GroupApply",
    "Block",
    "LetStmt",
    "ReturnStmt",
    "ElifBranch",
    "IfStmt",
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
    "StructConstruct",
    "ast_dump",
]


def _jsonable(value: Any) -> Any:
    if isinstance(value, Node):
        return value.to_dict()
    if isinstance(value, TokenKind):
        return value.value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


@dataclass
class Node:
    """Base class: every AST node carries its 1-based source position."""

    line: int
    column: int

    def to_dict(self) -> dict:
        """Serialize the node (and children) to a JSON-friendly dict."""
        d: dict = {"kind": type(self).__name__, "line": self.line, "column": self.column}
        for f in _fields(self):
            if f.name in ("line", "column"):
                continue
            d[f.name] = _jsonable(getattr(self, f.name))
        return d


# -- top-level declarations ------------------------------------------------


@dataclass
class Program(Node):
    items: list[Node] = field(default_factory=list)


@dataclass
class Type(Node):
    name: str
    args: list["Type"] = field(default_factory=list)


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
    fields: list["Field"] = field(default_factory=list)
    pub: bool = False


@dataclass
class Variant(Node):
    name: str
    value: Optional[int] = None


@dataclass
class EnumDecl(Node):
    name: str
    variants: list["Variant"] = field(default_factory=list)
    pub: bool = False


@dataclass
class FnDecl(Node):
    name: str
    params: list["Param"] = field(default_factory=list)
    return_type: Optional["Type"] = None
    body: Optional["Block"] = None
    pub: bool = False
    static: bool = False
    which: Optional[str] = None


@dataclass
class TraitDecl(Node):
    name: str
    methods: list["FnDecl"] = field(default_factory=list)
    pub: bool = False


@dataclass
class ImplDecl(Node):
    trait: "Type"
    struct: "Type"
    methods: list["FnDecl"] = field(default_factory=list)


@dataclass
class ExtraDecl(Node):
    struct: str
    methods: list["FnDecl"] = field(default_factory=list)
    name: Optional[str] = None


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


# -- statements ------------------------------------------------------------


@dataclass
class Block(Node):
    stmts: list[Node] = field(default_factory=list)


@dataclass
class LetStmt(Node):
    name: str
    type: Optional["Type"] = None
    value: Optional[Node] = None


@dataclass
class ReturnStmt(Node):
    value: Optional[Node] = None


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
