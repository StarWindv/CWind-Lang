"""CWind semantic analyzer — first pass.

Builds the top-level symbol table and validates declaration-level references:
duplicate definitions, references to declared types/groups/structs/traits, and
type annotations against built-in and user-defined types.  Expression-level
type checking is deliberately out of scope for now.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..ast_components.ast import (
    ConstDecl,
    EnumDecl,
    ExtraDecl,
    FnDecl,
    GroupApply,
    GroupDecl,
    ImplDecl,
    Node,
    Program,
    StructDecl,
    TraitDecl,
    Type,
    TypeDecl,
)
from ..ast_components.errors import FrontendError

__all__ = [
    "BUILTIN_TYPES",
    "SaError",
    "SaResult",
    "Symbol",
    "ProgramInfo",
    "run_sa",
    "run_sa_with_errors",
]


BUILTIN_TYPES: frozenset[str] = frozenset({
    "Int", "Int8", "UInt", "UInt8", "Float", "String", "Bool", "Byte",
    "Instance", "None", "Tuple", "Vector", "Map", "Set",
})


class SaError(FrontendError):
    """Raised for semantic-level problems (as opposed to lexer/parser errors)."""


@dataclass
class SaResult:
    """SA result plus every recovered semantic error."""

    info: "ProgramInfo"
    errors: list[SaError]


@dataclass
class Symbol:
    """A top-level definition collected during semantic analysis."""

    name: str
    kind: str
    line: int
    column: int

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "line": self.line,
            "column": self.column,
        }


@dataclass
class ProgramInfo:
    """Result of the semantic-analysis pass."""

    symbols: dict[str, Symbol] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"symbols": [sym.to_dict() for sym in self.symbols.values()]}


class _Analyzer:
    def __init__(self) -> None:
        self.symbols: dict[str, Symbol] = {}
        self.defined: set[str] = set()
        self.errors: list[SaError] = []

    def run(self, program: Program) -> ProgramInfo:
        # Pass 1: collect every top-level definition, detecting duplicates.
        for item in program.items:
            self._collect(item)
        # Pass 2: validate declaration-level references and type annotations.
        for item in program.items:
            self._check(item)
        return ProgramInfo(symbols=self.symbols)

    def _record_error(self, message: str, line: int, column: int) -> None:
        self.errors.append(SaError(message, line, column))

    def _collect(self, item: Node) -> None:
        kind_name = _decl_kind_name(item)
        if kind_name is None:
            return
        kind, name = kind_name
        if name in self.defined:
            prev = self.symbols[name]
            self._record_error(
                f"duplicate definition of '{name}' "
                f"(first defined at line {prev.line})",
                item.line,
                item.column,
            )
            return
        if name in BUILTIN_TYPES:
            self._record_error(
                f"'{name}' redefines a built-in type",
                item.line,
                item.column,
            )
            return
        self.defined.add(name)
        self.symbols[name] = Symbol(name, kind, item.line, item.column)

    def _check(self, item: Node) -> None:
        if isinstance(item, TypeDecl):
            self._check_type(item.base, item)
        elif isinstance(item, StructDecl):
            for f in item.fields:
                self._check_type(f.type, f)
        elif isinstance(item, TraitDecl):
            for m in item.methods:
                self._check_fn_types(m)
        elif isinstance(item, FnDecl):
            self._check_fn_types(item)
        elif isinstance(item, ImplDecl):
            self._require(item.trait.name, {"trait"}, item, "trait")
            self._require(item.struct.name, {"struct", "enum"}, item, "struct")
            for m in item.methods:
                self._check_fn_types(m)
        elif isinstance(item, ExtraDecl):
            self._require(item.struct, {"struct", "enum"}, item, "struct")
            for m in item.methods:
                self._check_fn_types(m)
        elif isinstance(item, GroupDecl):
            if item.struct is not None:
                self._require(item.struct, {"struct", "enum"}, item, "struct")
            for d in item.distributions:
                self._check_type(d.type, d)
        elif isinstance(item, GroupApply):
            self._require(item.group, {"group"}, item, "group")
            self._require(item.struct, {"struct", "enum"}, item, "struct")

    def _check_fn_types(self, fn: FnDecl) -> None:
        for p in fn.params:
            if p.type is not None:
                self._check_type(p.type, p)
        if fn.return_type is not None:
            self._check_type(fn.return_type, fn)

    def _check_type(self, type_: Type, ctx: Node) -> None:
        if type_.name not in BUILTIN_TYPES and type_.name not in self.defined and type_.name != "Self":
            self._record_error(f"unknown type '{type_.name}'", ctx.line, ctx.column)
        for arg in type_.args:
            self._check_type(arg, ctx)

    def _require(self, name: str, kinds: set[str], ctx: Node, what: str) -> None:
        sym = self.symbols.get(name)
        if sym is None:
            self._record_error(f"unknown {what} '{name}'", ctx.line, ctx.column)
        elif sym.kind not in kinds:
            self._record_error(
                f"'{name}' is a {sym.kind}, not a {what}",
                ctx.line,
                ctx.column,
            )


def _decl_kind_name(item: Node) -> Optional[tuple[str, str]]:
    if isinstance(item, ConstDecl):
        return "const", item.name
    if isinstance(item, TypeDecl):
        return "type", item.name
    if isinstance(item, StructDecl):
        return "struct", item.name
    if isinstance(item, EnumDecl):
        return "enum", item.name
    if isinstance(item, TraitDecl):
        return "trait", item.name
    if isinstance(item, FnDecl):
        return "fn", item.name
    if isinstance(item, GroupDecl):
        return "group", item.name
    return None  # ExtraDecl / ImplDecl / GroupApply are not symbols


def run_sa(program: Program) -> ProgramInfo:
    """Run the semantic-analysis pass; raise the first SaError."""
    result = run_sa_with_errors(program)
    if result.errors:
        raise result.errors[0]
    return result.info


def run_sa_with_errors(program: Program) -> SaResult:
    """Run the semantic-analysis pass, collecting every SaError.

    Checks are independent, so all problems are reported in a single run.
    """
    analyzer = _Analyzer()
    info = analyzer.run(program)
    return SaResult(info, list(analyzer.errors))
