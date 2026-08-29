"""Data structures produced by semantic analysis."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Union

from ..ast_components.ast import FnDecl, Node, Type

__all__ = [
    "BindingInfo",
    "ProgramInfo",
    "Symbol",
    "MethodBinding",
    "VarInfo"
]


@dataclass
class Symbol:
    """A top-level definition collected during semantic analysis."""

    name: str
    kind: str
    line: int
    column: int
    ref: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "line": self.line,
            "column": self.column,
        }


@dataclass
class BindingInfo:
    """A method binding provided by an ``impl``/``extra`` declaration.

    ``id`` lives in its own namespace (distinct from AST node ids) and is the
    handle used by ``ann.member.ref`` / ``ann.call.callee_ref`` for methods.
    ``decl_id`` / ``fn_id`` are AST node ids of the enclosing declaration and
    of the method itself.
    """

    id: int
    decl_id: int
    owner: str
    trait: Optional[str]
    fn_id: int

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "decl_id": self.decl_id,
            "owner": self.owner,
            "trait": self.trait,
            "fn_id": self.fn_id,
        }


@dataclass
class ProgramInfo:
    """Result of the semantic-analysis pass."""

    symbols: dict[str, Symbol] = field(default_factory=dict)
    bindings: list[BindingInfo] = field(default_factory=list)
    modules: dict[str, list[str]] = field(default_factory=dict)
    imported_modules: list[str] = field(default_factory=list)
    # todo-76/78: per-``use`` import manifest (path/source/item/auto/...),
    # consumed by typed AST serialization.
    import_manifest: list[dict] = field(default_factory=list)
    # todo-144/146: canonical (definition-site) module path per declared
    # type/fn/const name; consumed by the typed-AST serializer to attach
    # ``def`` provenance to every type-bearing stringly site.
    def_paths: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"symbols": [sym.to_dict() for sym in self.symbols.values()]}


@dataclass
class VarInfo:
    name: str
    type: Optional[str]
    line: int
    column: int
    kind: str  # "param" | "let" | "const" | "field"
    initialized: bool = True
    moved: bool = False
    mutable: bool = False
    node: Optional["Node"] = None
    folded: Optional[Union[int, float]] = None
    # todo-145: ``mut`` 关键字与 ``&mut T`` 类型的可变性分开承载 ——
    # 绑定重赋值要求 declared_mut; ``*r = v`` 写穿要求 ref_mut。
    declared_mut: bool = False
    ref_mut: bool = False


@dataclass
class MethodBinding:
    """A method provided by an ``extra`` or ``impl`` declaration.

    ``owner_params`` are the declaration's generic parameters in order, and
    ``owner_struct`` is the type the declaration applies to (which may use
    those parameters, e.g. ``extra<T> Box<T>``).  They let call sites
    substitute the receiver's concrete type arguments into the method's
    signature.
    """

    id: int
    owner_params: tuple[str, ...]
    owner_struct: Optional["Type"]
    fn: "FnDecl"
    decl: "Node"
    trait: Optional[str]


def _find_method(
    methods: list["MethodBinding"],
    name: str,
) -> Optional["MethodBinding"]:
    for binding in methods:
        if binding.fn.name == name:
            return binding
    return None
