"""CWind semantic analyzer.

Runs three passes over the parsed program:

1. Collect every top-level definition into a symbol table, detecting
   duplicates and built-in redefinitions.
2. Validate declaration-level references (types, traits, structs, groups,
   group applications) and type annotations.
3. Check function and method bodies: scoped name resolution, arity and type
   checking for calls / assignments / returns / conditions, and field/member
   access.

Type checks are conservative: anything that cannot be determined is treated as
unknown and does not cascade further errors.  User-struct method calls that
are not declared are tolerated (the grammar's examples use pseudo-methods such
as ``entry``/``get_last`` that are not part of the spec).
"""

from __future__ import annotations

import math
import re
import struct
from dataclasses import dataclass, field, fields as _fields
from typing import Optional, Union

from ..ast_components.ast import (
    Assign,
    Attribute,
    BinOp,
    Block,
    BoolLit,
    BreakStmt,
    Call,
    ConstDecl,
    ContinueStmt,
    EnumDecl,
    ErrorStmt,
    ExprStmt,
    ExtraDecl,
    FloatLit,
    FnDecl,
    ForStmt,
    GroupApply,
    GroupDecl,
    IfStmt,
    ImplDecl,
    Index,
    IntLit,
    LetStmt,
    MapLit,
    Name,
    Node,
    Program,
    ReturnStmt,
    Slice,
    StrLit,
    StructConstruct,
    StructDecl,
    TraitDecl,
    Type,
    TypeDecl,
    UnaryOp,
    VectorLit,
    WhileStmt,
)
from ..ast_components.errors import FrontendError
from ..ast_components.token import TokenKind
from .builtin_methods import (
    BUILTIN_MODULE_FUNCTIONS,
    BUILTIN_OBJECTS,
    BUILTIN_TRAITS,
    BUILTIN_TYPE_METHODS,
    MethodSpec,
)

__all__ = [
    "BUILTIN_TYPES",
    "BindingInfo",
    "SaError",
    "SaResult",
    "Symbol",
    "ProgramInfo",
    "run_sa",
    "run_sa_with_errors",
]


BUILTIN_TYPES: frozenset[str] = frozenset({
    "Int", "Int8", "UInt", "UInt8", "Float", "String", "Bool", "Byte",
    "None", "Tuple", "Vector", "Map", "Set", "Iterator",
})

_NUMERIC: frozenset[str] = frozenset({"Int", "Int8", "UInt", "UInt8", "Float", "Byte"})
_INTEGER: frozenset[str] = frozenset({"Int", "Int8", "UInt", "UInt8"})
_RELATIONAL: frozenset[TokenKind] = frozenset({TokenKind.LT, TokenKind.GT, TokenKind.LE, TokenKind.GE})
_EQUALITY: frozenset[TokenKind] = frozenset({
    TokenKind.EQ, TokenKind.ADDR_EQ, TokenKind.NE, TokenKind.NOT_LT, TokenKind.NOT_GT,
})
_BITWISE: frozenset[TokenKind] = frozenset({
    TokenKind.AMP, TokenKind.PIPE, TokenKind.CARET, TokenKind.SHL, TokenKind.SHR,
})

# Grammar.md 1.1: Int is i16; the other integer widths follow their names.
_BUILTIN_RANGES: dict[str, tuple[int, int]] = {
    "Int": (-32768, 32767),
    "Int8": (-128, 127),
    "UInt": (0, 65535),
    "UInt8": (0, 255),
    "Byte": (0, 255),
}

# Generic built-in types must be given their type arguments (static typing).
_BUILTIN_GENERIC_ARITY: dict[str, int] = {
    "Vector": 1,
    "Map": 2,
    "Set": 1,
}

# f32 (IEEE-754 single precision) largest finite value.
_FLOAT32_MAX = 3.4028234663852886e38


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
    node: Optional["Node"] = None


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


def _type_str(t: Type, subst: Optional[dict[str, str]] = None) -> str:
    name = subst.get(t.name, t.name) if subst else t.name
    if not t.args:
        return name
    return f"{name}<{', '.join(_type_str(a, subst) for a in t.args)}>"


def _type_mentions(t: str, name: str) -> bool:
    """Whether a type string references the generic parameter ``name``."""
    return re.search(rf"\b{re.escape(name)}\b", t) is not None


def _subst_type_str(t: str, subst: Optional[dict[str, str]] = None) -> str:
    """Substitute generic parameters inside a stringified type."""
    if subst is None:
        return t
    for _ in range(len(subst) + 1):
        if t in subst:
            t = subst[t]
        else:
            break
    if "<" not in t:
        return t
    return f"{_base(t)}<{', '.join(_subst_type_str(a, subst) for a in _split_args(t))}>"


def _base(t: str) -> str:
    return t.split("<", 1)[0]


def _common_type(types: list[Optional[str]]) -> Optional[str]:
    seen = {t for t in types if t is not None}
    if len(seen) == 1:
        return next(iter(seen))
    return None


def _const_int(
    expr: Node,
    consts: Optional[dict[str, int]] = None,
) -> Optional[int]:
    """Constant-fold an integer expression: literals, unary/binary arithmetic
    on foldable operands, and references to previously folded consts."""
    if isinstance(expr, IntLit):
        return expr.value
    if isinstance(expr, UnaryOp):
        operand = _const_int(expr.operand, consts)
        if operand is None:
            return None
        if expr.op == TokenKind.MINUS:
            return -operand
        if expr.op == TokenKind.PLUS:
            return operand
        return None
    if isinstance(expr, BinOp):
        left = _const_int(expr.left, consts)
        right = _const_int(expr.right, consts)
        if left is None or right is None:
            return None
        op = expr.op
        if op == TokenKind.PLUS:
            return left + right
        if op == TokenKind.MINUS:
            return left - right
        if op == TokenKind.STAR:
            return left * right
        if op == TokenKind.SLASH:
            return left // right if right != 0 else None
        if op == TokenKind.PERCENT:
            return left % right if right != 0 else None
        if op == TokenKind.SHL:
            return left << right
        if op == TokenKind.SHR:
            return left >> right
        if op == TokenKind.AMP:
            return left & right
        if op == TokenKind.PIPE:
            return left | right
        if op == TokenKind.CARET:
            return left ^ right
        return None
    if isinstance(expr, Name) and len(expr.parts) == 1 and consts is not None:
        return consts.get(expr.parts[0])
    return None


def _const_number(
    expr: Node,
    int_consts: Optional[dict[str, int]] = None,
    float_consts: Optional[dict[str, float]] = None,
) -> Optional[Union[int, float]]:
    """Constant-fold a numeric expression to an int or float.

    Extends :func:`_const_int` with float literals and float arithmetic.
    Integer-only operators (shifts, bitwise ops) keep integer semantics and
    yield ``None`` when an operand is a float; division by zero yields
    ``None`` so callers do not have to worry about exceptions.
    """
    if isinstance(expr, IntLit):
        return expr.value
    if isinstance(expr, FloatLit):
        return expr.value
    if isinstance(expr, UnaryOp):
        operand = _const_number(expr.operand, int_consts, float_consts)
        if operand is None:
            return None
        if expr.op == TokenKind.MINUS:
            return -operand
        if expr.op == TokenKind.PLUS:
            return operand
        return None
    if isinstance(expr, BinOp):
        left = _const_number(expr.left, int_consts, float_consts)
        right = _const_number(expr.right, int_consts, float_consts)
        if left is None or right is None:
            return None
        op = expr.op
        if op == TokenKind.PLUS:
            return left + right
        if op == TokenKind.MINUS:
            return left - right
        if op == TokenKind.STAR:
            return left * right
        if op == TokenKind.SLASH:
            if right == 0:
                return None
            if isinstance(left, int) and isinstance(right, int):
                return left // right
            return left / right
        if op == TokenKind.PERCENT:
            if right == 0:
                return None
            return left % right
        if isinstance(left, int) and isinstance(right, int):
            if op == TokenKind.SHL:
                return left << right
            if op == TokenKind.SHR:
                return left >> right
            if op == TokenKind.AMP:
                return left & right
            if op == TokenKind.PIPE:
                return left | right
            if op == TokenKind.CARET:
                return left ^ right
        return None
    if isinstance(expr, Name) and len(expr.parts) == 1:
        n = expr.parts[0]
        if int_consts is not None and n in int_consts:
            return int_consts[n]
        if float_consts is not None and n in float_consts:
            return float_consts[n]
    return None


def _has_return(stmt: Node) -> bool:
    """Whether a statement subtree contains any ``return``."""
    if isinstance(stmt, ReturnStmt):
        return True
    if isinstance(stmt, Block):
        return any(_has_return(s) for s in stmt.stmts)
    if isinstance(stmt, IfStmt):
        return (
            _has_return(stmt.then)
            or any(_has_return(e.body) for e in stmt.elifs)
            or (stmt.else_ is not None and _has_return(stmt.else_))
        )
    if isinstance(stmt, WhileStmt):
        return _has_return(stmt.body)
    if isinstance(stmt, ForStmt):
        return _has_return(stmt.body)
    return False


def _match_arg_patterns(
    patterns: tuple[tuple[Optional[int], str], ...],
    nargs: int,
) -> Optional[list[str]]:
    """Return the expected argument types for a call of ``nargs`` arguments.

    ``patterns`` comes from :meth:`MethodSpec.patterns`; an unbounded tail
    pattern (``*: Type``) absorbs any remaining arguments.  Returns ``None``
    when the arity does not fit the declared patterns.
    """
    expected: list[str] = []
    for count, typ in patterns:
        if count is None:  # unbounded tail; validated to be last
            if nargs < len(expected):
                return None
            return expected + [typ] * (nargs - len(expected))
        expected.extend([typ] * count)
    return expected if len(expected) == nargs else None


def _patterns_arity_text(patterns: tuple[tuple[Optional[int], str], ...]) -> str:
    """Human-readable arity description for error messages."""
    fixed = sum(c for c, _ in patterns if c is not None)
    unbounded = any(c is None for c, _ in patterns)
    if unbounded:
        return "0 or more argument(s)" if fixed == 0 else f"at least {fixed} argument(s)"
    return f"{fixed} argument(s)"


def _split_args(t: str) -> list[str]:
    """Split the top-level type arguments of ``Name<A, B<C>>``."""
    if "<" not in t:
        return []
    inner = t[t.find("<") + 1:t.rfind(">")]
    parts: list[str] = []
    depth = 0
    start = 0
    for i, ch in enumerate(inner):
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(inner[start:i].strip())
            start = i + 1
    parts.append(inner[start:].strip())
    return [p for p in parts if p]


def _type_info(
    t: Optional[str],
    opaque_names: frozenset[str] = frozenset(),
) -> Optional[dict]:
    """Convert a type string into the typed-AST representation.

    ``{"name": "Vector", "args": [{"name": "Int"}]}``.  Leaves whose name is
    still a generic parameter are kept with ``"opaque": true`` so partially
    known types (e.g. ``Vector<T>``) do not lose their outer shape; ``None``
    means nothing is known at all.
    """
    if t is None:
        return None
    name = _base(t).strip()
    args = [_type_info(a, opaque_names) for a in _split_args(t)]
    info: dict = {"name": name}
    if name in _BUILTIN_GENERIC_ARITY and not args:
        # A built-in generic with unknown arguments never appears bare:
        # unknown arguments are represented as `Any` leaves so consumers can
        # tell "generic with unknown args" from a plain non-generic type.
        args = [{"name": "Any"} for _ in range(_BUILTIN_GENERIC_ARITY[name])]
    if args:
        info["args"] = args
    if name in opaque_names:
        info["opaque"] = True
    return info


def _generic_ref_index(expected: str) -> int:
    """Return the 1-based generic position of ``SameAsGeneric[:N]``."""
    if expected.startswith("SameAsGeneric:"):
        return int(expected[len("SameAsGeneric:"):])
    return 1


def _generic_arg(t: Optional[str], index: int) -> Optional[str]:
    """Return the index-th generic argument of ``t`` (1-based)."""
    if t is None or "<" not in t:
        return None
    args = _split_args(t)
    if index < 1 or index > len(args):
        return None
    return args[index - 1]


def _compatible(expected: Optional[str], actual: Optional[str]) -> bool:
    if expected is None or actual is None:
        return True
    if expected == "Any" or actual == "Any":
        return True
    if expected == actual:
        return True
    eb, ab = _base(expected), _base(actual)
    if eb == ab:
        # When both sides carry type arguments, compare them strictly
        # (Map<String, Int> is not Map<String, String>); a side without
        # arguments (e.g. an untyped map literal) stays lenient.
        e_args = _split_args(expected)
        a_args = _split_args(actual)
        if e_args and a_args:
            if len(e_args) != len(a_args):
                return False
            return all(
                _compatible(e, a) for e, a in zip(e_args, a_args)
            )
        return True
    if eb in _NUMERIC and ab in _NUMERIC:
        return True
    if eb == "Fn" or ab == "Fn":
        return True
    return False


def _find_method(
    methods: list["MethodBinding"],
    name: str,
) -> Optional["MethodBinding"]:
    for binding in methods:
        if binding.fn.name == name:
            return binding
    return None


class _Analyzer:
    def __init__(self) -> None:
        self.symbols: dict[str, Symbol] = {}
        self.defined: set[str] = set()
        self.errors: list[SaError] = []
        self.structs: dict[str, StructDecl] = {}
        self.enums: dict[str, EnumDecl] = {}
        self.traits: dict[str, TraitDecl] = {}
        self.type_aliases: dict[str, TypeDecl] = {}
        self.impls: dict[str, list[str]] = {}  # struct name -> trait names
        self.methods: dict[str, list[MethodBinding]] = {}
        self.functions: dict[str, FnDecl] = {}
        self.consts: dict[str, ConstDecl] = {}
        self.const_values: dict[str, int] = {}
        self.const_floats: dict[str, float] = {}
        self.conversions: dict[str, list[str]] = {}  # source type -> target type(s)
        self.scopes: list[dict[str, VarInfo]] = []
        self.current_owner: Optional[str] = None
        self.current_owner_type: Optional[str] = None
        self.active_generics: frozenset[str] = frozenset()
        self.loop_depth: int = 0
        self._next_node_id: int = 1
        self._next_binding_id: int = 1
        self._binding_order: list[tuple[str, MethodBinding]] = []

    def run(self, program: Program) -> ProgramInfo:
        # Number every AST node (pre-order, parents before children) so
        # symbols / bindings / annotations can reference nodes by id.
        self._assign_ids(program)
        # Pass 1: collect every top-level definition, detecting duplicates.
        for item in program.items:
            self._collect(item)
        # Pass 1.5: reject duplicate trait implementations.
        seen_impls: set[tuple[str, str]] = set()
        for item in program.items:
            if isinstance(item, ImplDecl):
                key = (item.struct.name, item.trait.name)
                if key in seen_impls:
                    self._record_error(
                        f"duplicate impl of trait '{item.trait.name}' for "
                        f"'{item.struct.name}'",
                        item.line,
                        item.column,
                    )
                else:
                    seen_impls.add(key)
        # Pass 2: validate declaration-level references and type annotations.
        for item in program.items:
            self._check(item)
        # Pass 3: check function and method bodies.
        self._push_scope()
        for c in self.consts.values():
            self._declare(VarInfo(
                c.name, _type_str(c.type), c.line, c.column, "const", node=c
            ))
        for fn in self.functions.values():
            self._check_fn(
                fn,
                owner=None,
                generic=frozenset(p.name for p in fn.type_params),
            )
        for struct, methods in self.methods.items():
            for binding in methods:
                fn = binding.fn
                owner_generic = frozenset(binding.owner_params)
                fn_generic = frozenset(p.name for p in fn.type_params)
                self._check_fn(
                    fn,
                    owner=struct,
                    generic=owner_generic | fn_generic,
                    owner_type=(
                        _type_str(binding.owner_struct)
                        if binding.owner_struct is not None
                        else struct
                    ),
                )
        self._pop_scope()
        bindings = []
        for owner, binding in self._binding_order:
            bindings.append(
                BindingInfo(
                    id=binding.id,
                    decl_id=binding.decl._typed_id,
                    owner=owner,
                    trait=binding.trait,
                    fn_id=binding.fn._typed_id,
                )
            )
        return ProgramInfo(symbols=self.symbols, bindings=bindings)

    # -- typed-AST metadata ----------------------------------------------

    def _assign_ids(self, node: Node) -> None:
        """Assign pre-order ids (parents before children) to every node."""
        node._typed_id = self._next_node_id
        self._next_node_id += 1
        for f in _fields(node):
            if f.name in ("line", "column"):
                continue
            value = getattr(node, f.name)
            if isinstance(value, Node):
                self._assign_ids(value)
            elif isinstance(value, list):
                for v in value:
                    if isinstance(v, Node):
                        self._assign_ids(v)

    def _opaque_names(self, extra: Optional[frozenset[str]] = None) -> frozenset[str]:
        if extra is None:
            return self.active_generics
        return frozenset(extra) | self.active_generics

    def _ann_type(
        self,
        node: Node,
        t: Optional[str],
        opaque: Optional[frozenset[str]] = None,
    ) -> None:
        """Record ``ann.type`` (expanded) or ``ann.opaque`` on a node."""
        if t is not None:
            t = self._expand_type(t)
        info = _type_info(t, self._opaque_names(opaque))
        if info is None:
            node._typed_ann["type"] = None
            node._typed_ann["opaque"] = True
        else:
            node._typed_ann["type"] = info

    def _ann_call(
        self,
        call: "Call",
        callee_kind: str,
        callee_ref: object,
        type_args: Optional[dict[str, str]] = None,
    ) -> None:
        info: dict = {"callee_kind": callee_kind, "callee_ref": callee_ref}
        if type_args:
            info["type_args"] = {
                name: _type_info(
                    self._expand_type(t), self._opaque_names()
                )
                for name, t in type_args.items()
            }
        call._typed_ann["call"] = info

    def _annotate_type_node(
        self,
        type_node: "Type",
        opaque: Optional[frozenset[str]] = None,
    ) -> None:
        """Annotate a ``Type`` AST node with its expanded type, recursing
        into its argument nodes."""
        self._ann_type(type_node, _type_str(type_node), opaque)
        for arg in type_node.args:
            self._annotate_type_node(arg, opaque)

    def _annotate_type_params(
        self,
        params: list["TypeParam"],
        opaque: Optional[frozenset[str]] = None,
    ) -> None:
        """Annotate the ``Type`` nodes used as generic-parameter bounds."""
        for tp in params:
            if tp.bound is not None:
                self._annotate_type_node(tp.bound, opaque)

    # -- pass 1: collection ------------------------------------------------

    def _collect(self, item: Node) -> None:
        self._index(item)
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
        self.symbols[name] = Symbol(
            name, kind, item.line, item.column, ref=item._typed_id
        )

    def _index(self, item: Node) -> None:
        if isinstance(item, StructDecl):
            self.structs[item.name] = item
        elif isinstance(item, EnumDecl):
            self.enums[item.name] = item
        elif isinstance(item, TypeDecl):
            self.type_aliases[item.name] = item
        elif isinstance(item, TraitDecl):
            self.traits[item.name] = item
        elif isinstance(item, FnDecl):
            self.functions[item.name] = item
        elif isinstance(item, ConstDecl):
            self.consts[item.name] = item
        elif isinstance(item, ImplDecl):
            generic = tuple(p.name for p in item.params)
            self.impls.setdefault(item.struct.name, []).append(item.trait.name)
            for m in item.methods:
                binding = MethodBinding(
                    self._next_binding_id,
                    generic,
                    item.struct,
                    m,
                    item,
                    item.trait.name,
                )
                self.methods.setdefault(item.struct.name, []).append(binding)
                self._next_binding_id += 1
                self._binding_order.append((item.struct.name, binding))
        elif isinstance(item, ExtraDecl):
            generic = tuple(p.name for p in item.params)
            for m in item.methods:
                binding = MethodBinding(
                    self._next_binding_id,
                    generic,
                    item.struct,
                    m,
                    item,
                    None,
                )
                self.methods.setdefault(item.struct.name, []).append(binding)
                self._next_binding_id += 1
                self._binding_order.append((item.struct.name, binding))

    # -- pass 2: declarations ---------------------------------------------

    def _check(self, item: Node) -> None:
        if isinstance(item, TypeDecl):
            generic = {p.name for p in item.params}
            self.defined |= generic
            try:
                self._annotate_type_params(item.params, frozenset(generic))
                self._check_type(item.base, item)
                self._annotate_type_node(item.base, frozenset(generic))
                self._ann_type(item, _type_str(item.base), frozenset(generic))
                if item.where is not None:
                    self._check_validation(
                        item.where, [("self", _type_str(item.base), None)]
                    )
            finally:
                self.defined -= generic
        elif isinstance(item, StructDecl):
            generic = {p.name for p in item.params}
            self.defined |= generic
            try:
                self._annotate_type_params(item.params, frozenset(generic))
                seen_fields: set[str] = set()
                for f in item.fields:
                    self._check_type(f.type, f)
                    self._annotate_type_node(f.type, frozenset(generic))
                    self._ann_type(f, _type_str(f.type), frozenset(generic))
                    if f.name in seen_fields:
                        self._record_error(
                            f"duplicate field '{f.name}' in struct '{item.name}'",
                            f.line,
                            f.column,
                        )
                    seen_fields.add(f.name)
                    if f.validation is not None:
                        self._check_validation(
                            f.validation, [(f.name, _type_str(f.type), f)]
                        )
            finally:
                self.defined -= generic
        elif isinstance(item, ConstDecl):
            self._check_type(item.type, item)
            self._annotate_type_node(item.type)
            self._ann_type(item, _type_str(item.type))
            value = self._check_expr(item.value)
            if not self._compat_types(_type_str(item.type), value):
                self._record_error(
                    f"cannot initialize {self._fmt_type(_type_str(item.type))} "
                    f"with {self._fmt_type(value)}",
                    item.line,
                    item.column,
                )
            folded = _const_number(item.value, self.const_values, self.const_floats)
            if folded is not None:
                if isinstance(folded, float):
                    self.const_floats[item.name] = folded
                else:
                    self.const_values[item.name] = folded
                item._typed_ann["folded_value"] = folded
            self._check_const_div_zero(item.value)
            self._check_literal_range(_type_str(item.type), item.value)
        elif isinstance(item, TraitDecl):
            generic = {p.name for p in item.params}
            self.defined |= generic
            try:
                self._annotate_type_params(item.params, frozenset(generic))
                for m in item.methods:
                    method_generic = {p.name for p in m.type_params}
                    self.defined |= method_generic
                    try:
                        self._check_fn_types(m)
                    finally:
                        self.defined -= method_generic
                    if m.body is not None:
                        self._check_fn(
                            m,
                            owner=None,
                            generic=frozenset(generic | method_generic),
                        )
            finally:
                self.defined -= generic
        elif isinstance(item, FnDecl):
            generic = {p.name for p in item.type_params}
            self.defined |= generic
            try:
                self._check_fn_types(item)
            finally:
                self.defined -= generic
        elif isinstance(item, ImplDecl):
            self._require_trait(item.trait.name, item)
            self._require_type_target(item.struct.name, item, "struct")
            generic = {p.name for p in item.params}
            self.defined |= generic
            try:
                self._annotate_type_params(item.params, frozenset(generic))
                self._check_type(item.struct, item)
                self._annotate_type_node(item.struct, frozenset(generic))
                for arg in item.trait.args:
                    self._check_type(arg, item)
                    self._annotate_type_node(arg, frozenset(generic))
                if item.trait.name == "From":
                    self._check_from_impl(item)
                for m in item.methods:
                    method_generic = {p.name for p in m.type_params}
                    self.defined |= method_generic
                    try:
                        self._check_fn_types(m)
                    finally:
                        self.defined -= method_generic
            finally:
                self.defined -= generic
            trait_decl = self.traits.get(item.trait.name)
            if trait_decl is not None:
                self._check_impl_conformance(item, trait_decl)
        elif isinstance(item, ExtraDecl):
            generic = {p.name for p in item.params}
            self.defined |= generic
            try:
                self._annotate_type_params(item.params, frozenset(generic))
                self._require_type_target(item.struct.name, item, "struct")
                self._check_type(item.struct, item)
                self._annotate_type_node(item.struct, frozenset(generic))
                struct = self.structs.get(item.struct.name)
                if struct is not None:
                    struct_params = [p.name for p in struct.params]
                    extra_params = [p.name for p in item.params]
                    if extra_params != struct_params:
                        self._record_error(
                            f"extra generic parameters {extra_params} do not match "
                            f"struct '{item.struct.name}' {struct_params}",
                            item.line,
                            item.column,
                        )
                for m in item.methods:
                    method_generic = {p.name for p in m.type_params}
                    self.defined |= method_generic
                    try:
                        self._check_fn_types(m)
                    finally:
                        self.defined -= method_generic
            finally:
                self.defined -= generic
        elif isinstance(item, GroupDecl):
            if item.struct is not None:
                self._require(item.struct, {"struct", "enum"}, item, "struct")
            for p in item.params:
                if p.type is not None:
                    self._check_type(p.type, p)
                    self._annotate_type_node(p.type)
                self._ann_type(p, _type_str(p.type) if p.type is not None else None)
            for d in item.distributions:
                self._check_type(d.type, d)
                self._annotate_type_node(d.type)
            param_names = {p.name for p in item.params}
            for d in item.distributions:
                if d.subject_self:
                    struct = self.structs.get(item.struct or "")
                    if struct is not None and not any(f.name == d.subject for f in struct.fields):
                        self._record_error(
                            f"'{item.struct}' has no field '{d.subject}'",
                            d.line,
                            d.column,
                        )
                elif item.struct is None and d.subject not in param_names:
                    self._record_error(
                        f"group '{item.name}' has no parameter '{d.subject}'",
                        d.line,
                        d.column,
                    )
        elif isinstance(item, GroupApply):
            self._require(item.group, {"group"}, item, "group")
            self._require(item.struct, {"struct", "enum"}, item, "struct")
            struct = self.structs.get(item.struct)
            if struct is not None:
                for fname in item.fields:
                    if not any(ff.name == fname for ff in struct.fields):
                        self._record_error(
                            f"'{item.struct}' has no field '{fname}'",
                            item.line,
                            item.column,
                        )

    def _check_fn_types(self, fn: FnDecl) -> None:
        opaque = frozenset(p.name for p in fn.type_params)
        self._annotate_type_params(fn.type_params, opaque)
        for p in fn.params:
            if p.type is not None:
                self._check_type(p.type, p)
                self._annotate_type_node(p.type, opaque)
            ptype = "Self" if p.name == "self" and p.type is None else (
                _type_str(p.type) if p.type is not None else None
            )
            self._ann_type(p, ptype, opaque)
        if fn.return_type is not None:
            self._check_type(fn.return_type, fn)
            self._annotate_type_node(fn.return_type, opaque)
        ret = _type_str(fn.return_type) if fn.return_type is not None else "None"
        self._ann_type(fn, ret, opaque)

    def _check_type(self, type_: Type, ctx: Node) -> None:
        if type_.name not in BUILTIN_TYPES and type_.name not in self.defined and type_.name != "Self":
            # point at the type name itself, not at the enclosing statement
            self._record_error(f"unknown type '{type_.name}'", type_.line, type_.column)
        arity = _BUILTIN_GENERIC_ARITY.get(type_.name)
        if arity is not None and len(type_.args) != arity:
            self._record_error(
                f"type '{type_.name}' expects {arity} generic argument(s), "
                f"got {len(type_.args)}",
                type_.line,
                type_.column,
            )
        struct = self.structs.get(type_.name)
        if struct is not None and len(type_.args) != len(struct.params):
            self._record_error(
                f"type '{type_.name}' expects {len(struct.params)} generic argument(s), "
                f"got {len(type_.args)}",
                type_.line,
                type_.column,
            )
            struct = None  # avoid cascading bound checks on a bad arity
        if struct is not None:
            for p, arg in zip(struct.params, type_.args):
                if p.bound is not None and p.bound.name not in BUILTIN_TRAITS:
                    trait_name = p.bound.name
                    if trait_name not in self.impls.get(arg.name, []):
                        self._record_error(
                            f"type '{arg.name}' does not satisfy bound '{trait_name}'",
                            arg.line,
                            arg.column,
                        )
        alias = self.type_aliases.get(type_.name)
        if alias is not None and type_.args and len(type_.args) != len(alias.params):
            self._record_error(
                f"type '{type_.name}' expects {len(alias.params)} generic argument(s), "
                f"got {len(type_.args)}",
                type_.line,
                type_.column,
            )
        for arg in type_.args:
            self._check_type(arg, ctx)
        self._ann_type(type_, _type_str(type_))

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

    def _require_trait(self, name: str, ctx: Node) -> None:
        if name in BUILTIN_TRAITS:
            return
        self._require(name, {"trait"}, ctx, "trait")

    def _require_type_target(self, name: str, ctx: Node, what: str) -> None:
        if name in BUILTIN_TYPES:
            return
        self._require(name, {"struct", "enum"}, ctx, what)

    def _expand_type(self, t: Optional[str]) -> Optional[str]:
        """Substitute a type alias's arguments into its right-hand side so
        method resolution sees the underlying type (e.g. ``DoubleMap<K, V>``
        expands to ``Map<K, V>``)."""
        if t is None:
            return None
        for _ in range(16):  # guard against circular aliases
            base = _base(t)
            alias = self.type_aliases.get(base)
            if alias is None:
                return t
            args = _split_args(t)
            if len(args) != len(alias.params):
                return t
            subst = dict(zip([p.name for p in alias.params], args))
            t = _type_str(alias.base, subst)
        return t

    def _compat_types(self, a: Optional[str], b: Optional[str]) -> bool:
        """Compatibility check with type aliases expanded on both sides."""
        return _compatible(self._expand_type(a), self._expand_type(b))

    def _fmt_type(self, t: Optional[str]) -> str:
        """A type for error messages; shows the expanded form for aliases."""
        if t is None:
            return "unknown"
        expanded = self._expand_type(t)
        return t if expanded == t else f"{t} ({expanded})"

    def _check_from_impl(self, item: ImplDecl) -> None:
        """Register a user-declared ``impl From<X> for Y`` conversion."""
        if len(item.trait.args) != 1:
            self._record_error(
                "From requires one type argument (the conversion source)",
                item.line,
                item.column,
            )
            return
        source = _type_str(item.trait.args[0])
        target = item.struct.name
        self.conversions.setdefault(source, []).append(target)
        method_names = {m.name for m in item.methods}
        for required in ("from", "into"):
            if required not in method_names:
                self._record_error(
                    f"impl From<{source}> for {target} must define '{required}'",
                    item.line,
                    item.column,
                )

    def _check_impl_conformance(self, item: ImplDecl, trait: TraitDecl) -> None:
        """Check that an impl satisfies the trait's method signatures, with
        the trait's type parameters substituted by the impl's arguments."""
        trait_params = [p.name for p in trait.params]
        trait_args = [a.name for a in item.trait.args]
        if not trait_args and trait_params:
            # `impl<T: Bound> Trait for S`: the impl's own parameters serve as
            # the trait's type arguments (older spelling).
            trait_args = [p.name for p in item.params]
        if len(trait_args) != len(trait_params):
            self._record_error(
                f"impl of '{trait.name}' provides {len(trait_args)} type argument(s) "
                f"but the trait has {len(trait_params)} parameter(s)",
                item.line,
                item.column,
            )
            return
        subst = dict(zip(trait_params, trait_args))
        trait_methods = {m.name: m for m in trait.methods}
        for m in item.methods:
            tm = trait_methods.get(m.name)
            if tm is None:
                self._record_error(
                    f"method '{m.name}' is not declared by trait '{trait.name}'",
                    m.line,
                    m.column,
                )
                continue
            self._check_method_signature(tm, m, subst, trait.name, item.struct.name)
        for name in trait_methods:
            if not any(m.name == name for m in item.methods):
                self._record_error(
                    f"impl of '{trait.name}' does not implement '{name}'",
                    item.line,
                    item.column,
                )

    def _check_method_signature(
        self,
        trait_fn: FnDecl,
        impl_fn: FnDecl,
        subst: dict[str, str],
        trait_name: str,
        owner: str,
    ) -> None:
        # Method-level generic parameters are bound independently on each
        # side, so compare them alpha-equivalently (impl's U == trait's T).
        impl_method_subst = dict(
            zip(
                [p.name for p in impl_fn.type_params],
                [p.name for p in trait_fn.type_params],
            )
        )
        trait_self = bool(trait_fn.params and trait_fn.params[0].name == "self")
        impl_self = bool(impl_fn.params and impl_fn.params[0].name == "self")
        if trait_self != impl_self:
            self._record_error(
                f"method '{impl_fn.name}' of '{trait_name}' has mismatched self",
                impl_fn.line,
                impl_fn.column,
            )
            return
        t_params = trait_fn.params[1:] if trait_self else trait_fn.params
        i_params = impl_fn.params[1:] if impl_self else impl_fn.params
        if len(t_params) != len(i_params):
            self._record_error(
                f"method '{impl_fn.name}' of '{trait_name}' expects "
                f"{len(i_params)} parameter(s), trait requires {len(t_params)}",
                impl_fn.line,
                impl_fn.column,
            )
            return

        def norm(s: str) -> str:
            return owner if s == "Self" else s

        for t, i in zip(t_params, i_params):
            if t.type is not None and i.type is not None:
                tt = norm(_type_str(t.type, subst))
                it = norm(_type_str(i.type, impl_method_subst))
                if tt != it:
                    self._record_error(
                        f"method '{impl_fn.name}' parameter '{i.name}' is {it}, "
                        f"trait requires {tt}",
                        impl_fn.line,
                        impl_fn.column,
                    )
        tr = norm(_type_str(trait_fn.return_type, subst)) if trait_fn.return_type is not None else "None"
        ir = (
            norm(_type_str(impl_fn.return_type, impl_method_subst))
            if impl_fn.return_type is not None
            else "None"
        )
        if tr != ir:
            self._record_error(
                f"method '{impl_fn.name}' of '{trait_name}' returns {ir}, "
                f"trait requires {tr}",
                impl_fn.line,
                impl_fn.column,
            )

    # -- scopes ------------------------------------------------------------

    def _push_scope(self) -> None:
        self.scopes.append({})

    def _pop_scope(self) -> None:
        self.scopes.pop()

    def _declare(self, info: VarInfo) -> None:
        scope = self.scopes[-1]
        if info.name in scope:
            self._record_error(
                f"duplicate definition of '{info.name}' in this scope",
                info.line,
                info.column,
            )
            return
        scope[info.name] = info

    def _lookup(self, name: str) -> Optional[VarInfo]:
        for scope in reversed(self.scopes):
            if name in scope:
                return scope[name]
        return None

    def _record_error(self, message: str, line: int, column: int) -> None:
        self.errors.append(SaError(message, line, column))

    # -- pass 3: function bodies ------------------------------------------

    def _check_fn(
        self,
        fn: FnDecl,
        owner: Optional[str],
        generic: frozenset[str] = frozenset(),
        owner_type: Optional[str] = None,
    ) -> None:
        saved_owner = self.current_owner
        saved_owner_type = self.current_owner_type
        saved_generics = self.active_generics
        self.current_owner = owner
        self.current_owner_type = owner_type if owner_type is not None else owner
        self.active_generics = saved_generics | generic
        self._push_scope()
        for p in fn.params:
            ptype: Optional[str]
            if p.name == "self" and owner is not None:
                ptype = owner_type if owner_type is not None else owner
            else:
                ptype = _type_str(p.type) if p.type is not None else None
            self._declare(VarInfo(p.name, ptype, p.line, p.column, "param", node=p))
            self._ann_type(p, ptype)
            if p.type is not None:
                self._annotate_type_node(p.type)
        ret = _type_str(fn.return_type) if fn.return_type is not None else "None"
        if ret == "Self":
            if isinstance(owner, str):
                ret = owner_type if owner_type is not None else owner
        self._ann_type(fn, ret)
        if fn.return_type is not None:
            self._annotate_type_node(fn.return_type)
            if ret != _type_str(fn.return_type):
                # The signature's own Type node resolves Self the same way
                # the FnDecl annotation does (e.g. `-> Self` in extra).
                fn.return_type._typed_ann["type"] = _type_info(
                    self._expand_type(ret), self._opaque_names()
                )
        self.defined |= generic
        try:
            if fn.body is not None:
                self._check_block(fn.body, ret)
        finally:
            self.defined -= generic
            self.active_generics = saved_generics
        if ret != "None" and fn.body is not None and not _has_return(fn.body):
            self._record_error(
                f"function '{fn.name}' must return a value",
                fn.line,
                fn.column,
            )
        if fn.which is not None:
            non_self = [p for p in fn.params if p.name != "self"]
            if non_self:
                self._record_error(
                    f"which method '{fn.name}' can only take a self parameter",
                    fn.line,
                    fn.column,
                )
            if owner is not None and _find_method(
                self.methods.get(owner, []), fn.which
            ) is None:
                self._record_error(
                    f"which target '{fn.which}' does not exist on '{owner}'",
                    fn.line,
                    fn.column,
                )
        self._pop_scope()
        self.current_owner = saved_owner
        self.current_owner_type = saved_owner_type

    def _check_block(self, block: Block, return_type: str) -> None:
        self._push_scope()
        for stmt in block.stmts:
            self._check_stmt(stmt, return_type)
        self._pop_scope()

    def _check_validation(
        self,
        block: Block,
        vars: list[tuple[str, str, Optional[Node]]],
    ) -> None:
        """Check a where/arrow validation block: every statement is a Bool
        condition, with the validated value(s) in scope."""
        self._push_scope()
        for name, t, node in vars:
            self._declare(VarInfo(
                name, t, block.line, block.column, "field", node=node
            ))
        for stmt in block.stmts:
            if isinstance(stmt, ExprStmt):
                self._check_condition(stmt.expr)
        self._pop_scope()

    def _check_stmt(self, stmt: Node, return_type: str) -> None:
        if isinstance(stmt, LetStmt):
            declared = _type_str(stmt.type) if stmt.type is not None else None
            value = self._check_expr(stmt.value) if stmt.value is not None else None
            if stmt.type is not None:
                self._check_type(stmt.type, stmt)
            known = (
                declared is not None
                and (_base(declared) in BUILTIN_TYPES or declared in self.defined)
            )
            if declared is None:
                self._record_error("let declaration requires a type", stmt.line, stmt.column)
            elif known and not self._compat_types(declared, value):
                at = stmt.value if stmt.value is not None else stmt
                self._record_error(
                    f"cannot initialize {self._fmt_type(declared)} with {self._fmt_type(value)}",
                    at.line,
                    at.column,
                )
            self._check_literal_range(declared, stmt.value)
            self._declare(VarInfo(
                stmt.name,
                declared,
                stmt.line,
                stmt.column,
                "let",
                initialized=stmt.value is not None,
                node=stmt,
            ))
            self._ann_type(stmt, declared)
            if stmt.value is not None and value is not None:
                stmt._typed_ann["init_type"] = _type_info(
                    self._expand_type(value), self._opaque_names()
                )
        elif isinstance(stmt, ReturnStmt):
            if stmt.value is None:
                if return_type != "None":
                    self._record_error(
                        f"function returns {return_type} but returned nothing",
                        stmt.line,
                        stmt.column,
                    )
                self._ann_type(stmt, "None")
                return
            value = self._check_expr(stmt.value)
            if not self._compat_types(return_type, value):
                self._record_error(
                    f"return type mismatch: expected {self._fmt_type(return_type)}, "
                    f"got {self._fmt_type(value)}",
                    stmt.line,
                    stmt.column,
                )
            self._check_literal_range(return_type, stmt.value)
            self._ann_type(stmt, value)
            stmt._typed_ann["expected_return"] = _type_info(
                self._expand_type(return_type), self._opaque_names()
            )
        elif isinstance(stmt, ExprStmt):
            self._check_expr(stmt.expr)
        elif isinstance(stmt, IfStmt):
            self._check_condition(stmt.cond)
            self._check_block(stmt.then, return_type)
            for e in stmt.elifs:
                self._check_condition(e.cond)
                self._check_block(e.body, return_type)
            if stmt.else_ is not None:
                self._check_block(stmt.else_, return_type)
        elif isinstance(stmt, WhileStmt):
            self._check_condition(stmt.cond)
            self.loop_depth += 1
            try:
                self._check_block(stmt.body, return_type)
            finally:
                self.loop_depth -= 1
        elif isinstance(stmt, ForStmt):
            if stmt.type is not None:
                self._check_type(stmt.type, stmt)
            iterable = self._check_expr(stmt.iterable)
            var_type = self._element_type(iterable)
            self._push_scope()
            self._declare(VarInfo(
                stmt.var, var_type, stmt.line, stmt.column, "let", node=stmt
            ))
            self.loop_depth += 1
            try:
                self._check_block(stmt.body, return_type)
            finally:
                self.loop_depth -= 1
            self._pop_scope()
            if iterable is not None:
                stmt._typed_ann["iterable_type"] = _type_info(
                    self._expand_type(iterable), self._opaque_names()
                )
            if var_type is not None:
                stmt._typed_ann["var_type"] = _type_info(
                    self._expand_type(var_type), self._opaque_names()
                )
        elif isinstance(stmt, Block):
            self._check_block(stmt, return_type)
        elif isinstance(stmt, (BreakStmt, ContinueStmt)):
            if self.loop_depth == 0:
                keyword = "break" if isinstance(stmt, BreakStmt) else "continue"
                self._record_error(
                    f"'{keyword}' can only be used inside a loop",
                    stmt.line,
                    stmt.column,
                )

    def _check_condition(self, cond: Node) -> None:
        t = self._check_expr(cond)
        if t is not None and not self._compat_types("Bool", t):
            self._record_error(
                f"condition must be Bool, got {self._fmt_type(t)}",
                cond.line,
                cond.column,
            )

    def _check_literal_range(self, target: Optional[str], value: Optional[Node]) -> None:
        """Reject integer literals that do not fit the declared type's width
        (e.g. ``-1`` into ``UInt``), fractional constants into integer types,
        and constants that do not fit / are not exactly representable in
        ``Float`` (f32)."""
        if target is None or value is None:
            return
        folded = _const_number(value, self.const_values, self.const_floats)
        if folded is None:
            return
        expanded = self._expand_type(target)
        if expanded is None:
            return
        base = _base(expanded)
        if base == "Float":
            self._check_float_const(folded, value)
            return
        bounds = _BUILTIN_RANGES.get(base)
        if bounds is None:
            return
        if isinstance(folded, float):
            if not folded.is_integer():
                self._record_error(
                    f"value {folded:g} is not an integer and does not fit in {base}",
                    value.line,
                    value.column,
                )
                return
            folded = int(folded)
        lo, hi = bounds
        if folded < lo or folded > hi:
            self._record_error(
                f"value {folded} does not fit in {base}",
                value.line,
                value.column,
            )

    def _check_float_const(self, folded: Union[int, float], value: Node) -> None:
        """Validate a folded constant against Float (f32): it must be finite,
        within f32's range, and integral values must be exactly
        representable (e.g. ``16777216 + 1`` is rejected because f32 cannot
        represent 16777217)."""
        if isinstance(folded, float) and not math.isfinite(folded):
            self._record_error(
                "value is not finite and does not fit in Float",
                value.line,
                value.column,
            )
            return
        if abs(folded) > _FLOAT32_MAX:
            self._record_error(
                f"value {folded:g} does not fit in Float",
                value.line,
                value.column,
            )
            return
        if isinstance(folded, float) and folded.is_integer():
            folded = int(folded)
        if isinstance(folded, int):
            f32 = struct.unpack("!f", struct.pack("!f", float(folded)))[0]
            if float(f32) != float(folded):
                self._record_error(
                    f"value {folded} is not exactly representable in Float",
                    value.line,
                    value.column,
                )

    def _check_const_div_zero(self, expr: Node) -> None:
        """Reject division by a foldable zero in constant expressions."""
        if isinstance(expr, BinOp):
            if expr.op in (TokenKind.SLASH, TokenKind.PERCENT):
                right = _const_int(expr.right, self.const_values)
                if right == 0:
                    self._record_error(
                        "division by zero in constant expression",
                        expr.line,
                        expr.column,
                    )
            self._check_const_div_zero(expr.left)
            self._check_const_div_zero(expr.right)
        elif isinstance(expr, UnaryOp):
            self._check_const_div_zero(expr.operand)


    def _check_expr(self, expr: Node) -> Optional[str]:
        def resolve_index(ep: Index):
            rec = self._check_expr(ep.obj)
            it = self._check_expr(ep.index)
            t = self._indexed_type(rec)
            self._ann_type(ep, t)
            if rec is not None:
                ep._typed_ann["container_type"] = _type_info(
                    self._expand_type(rec), self._opaque_names()
                )
            if it is not None:
                ep._typed_ann["index_type"] = _type_info(
                    self._expand_type(it), self._opaque_names()
                )
            return t

        def resolve_slice(ep: Slice):
            rec = self._check_expr(ep.obj)
            for p in (ep.start, ep.stop, ep.step):
                if p is not None:
                    self._check_expr(p)
            self._ann_type(ep, rec)
            if rec is not None:
                ep._typed_ann["container_type"] = _type_info(
                    self._expand_type(rec), self._opaque_names()
                )
            return rec

        base_map = {
            IntLit: "Int",
            BoolLit: "Bool",
            FloatLit: "Float",
            StrLit: "String",
        }

        typo = type(expr)
        if typo in base_map:
            t = base_map[typo]
            self._ann_type(expr, t)
            return t
        if isinstance(expr, Name):
            return self._check_name(expr)
        if isinstance(expr, Attribute):
            return self._check_member(self._check_expr(expr.obj), expr.name, expr)
        if isinstance(expr, Call):
            return self._check_call(expr)
        if isinstance(expr, Index):
            return resolve_index(expr)
        if isinstance(expr, Slice):
            return resolve_slice(expr)

        if isinstance(expr, UnaryOp):
            operand = self._check_expr(expr.operand)
            if operand is not None:
                expr._typed_ann["operand_type"] = _type_info(
                    self._expand_type(operand), self._opaque_names()
                )
            if expr.op == TokenKind.NOT:
                if operand is not None and not self._compat_types("Bool", operand):
                    self._record_error(
                        f"'!' requires a Bool operand, got {self._fmt_type(operand)}",
                        expr.line,
                        expr.column,
                    )
                self._ann_type(expr, "Bool")
                return "Bool"
            if expr.op in (TokenKind.MINUS, TokenKind.PLUS):
                expanded = self._expand_type(operand) if operand is not None else None
                if expanded is not None and _base(expanded) not in _NUMERIC:
                    self._record_error(
                        f"unary '{expr.op.value}' requires a numeric operand, "
                        f"got {self._fmt_type(operand)}",
                        expr.line,
                        expr.column,
                    )
                result = expanded if expanded is not None else "Int"
                self._ann_type(expr, result)
                return result
            return None
        if isinstance(expr, BinOp):
            left = self._check_expr(expr.left)
            right = self._check_expr(expr.right)
            result = self._check_binop(expr.op, left, right, expr)
            if left is not None:
                expr._typed_ann["left_type"] = _type_info(
                    self._expand_type(left), self._opaque_names()
                )
            if right is not None:
                expr._typed_ann["right_type"] = _type_info(
                    self._expand_type(right), self._opaque_names()
                )
            self._ann_type(expr, result)
            return result
        if isinstance(expr, Assign):
            if (
                isinstance(expr.target, Name)
                and len(expr.target.parts) == 1
                and expr.op == TokenKind.ASSIGN
            ):
                info = self._lookup(expr.target.parts[0])
                if info is not None and info.kind == "let":
                    info.initialized = True
            target = self._check_expr(expr.target)
            value = self._check_expr(expr.value)
            if not self._compat_types(target, value):
                self._record_error(
                    f"cannot assign {self._fmt_type(value)} to {self._fmt_type(target)}",
                    expr.line,
                    expr.column,
                )
            self._check_literal_range(target, expr.value)
            if isinstance(expr.target, Name) and len(expr.target.parts) == 1:
                info = self._lookup(expr.target.parts[0])
                if info is not None and info.kind == "const":
                    self._record_error(
                        f"cannot assign to const '{expr.target.parts[0]}'",
                        expr.line,
                        expr.column,
                    )
            if target is not None:
                expr._typed_ann["target_type"] = _type_info(
                    self._expand_type(target), self._opaque_names()
                )
            if value is not None:
                expr._typed_ann["value_type"] = _type_info(
                    self._expand_type(value), self._opaque_names()
                )
            self._ann_type(expr, value)
            return value
        if isinstance(expr, VectorLit):
            elems = [self._check_expr(e) for e in expr.elems]
            elem = _common_type(elems)
            result = f"Vector<{elem}>" if elem is not None else "Vector"
            self._ann_type(expr, result)
            if elem is not None:
                expr._typed_ann["element_type"] = _type_info(
                    self._expand_type(elem), self._opaque_names()
                )
            return result
        if isinstance(expr, MapLit):
            key_types: list[Optional[str]] = []
            value_types: list[Optional[str]] = []
            for e in expr.entries:
                k = self._check_expr(e.key)
                v = self._check_expr(e.value)
                key_types.append(k)
                value_types.append(v)
                if k is not None:
                    e._typed_ann["key_type"] = _type_info(
                        self._expand_type(k), self._opaque_names()
                    )
                if v is not None:
                    e._typed_ann["value_type"] = _type_info(
                        self._expand_type(v), self._opaque_names()
                    )
            k = _common_type(key_types)
            v = _common_type(value_types)
            result = f"Map<{k}, {v}>" if k is not None and v is not None else "Map"
            self._ann_type(expr, result)
            return result
        if isinstance(expr, StructConstruct):
            is_self = expr.type.name == "Self" and self.current_owner is not None
            type_name = (
                (self.current_owner_type or self.current_owner)
                if is_self
                else expr.type.name
            )
            base_name = _base(type_name)
            self._require(base_name, {"struct", "enum"}, expr, "struct")
            self._annotate_type_node(expr.type)
            if is_self:
                expr.type._typed_ann["type"] = _type_info(
                    self._expand_type(type_name), self._opaque_names()
                )
            arg_types = [self._check_expr(a) for a in expr.args]
            struct = self.structs.get(base_name)
            field_types: list[Optional[dict]] = []
            if struct is not None:
                type_args = (
                    _split_args(type_name) if is_self else [a.name for a in expr.type.args]
                )
                subst = dict(
                    zip(
                        [p.name for p in struct.params],
                        type_args,
                    )
                )
                fields = [f for f in struct.fields if not f.static]
                if len(arg_types) != len(fields):
                    self._record_error(
                        f"'{base_name}' expects {len(fields)} field value(s), "
                        f"got {len(arg_types)}",
                        expr.line,
                        expr.column,
                    )
                else:
                    for i, (f, at) in enumerate(zip(fields, arg_types)):
                        ft = _type_str(f.type, subst)
                        if not self._compat_types(ft, at):
                            self._record_error(
                                f"field {i + 1} '{f.name}' of '{base_name}' "
                                f"expects {self._fmt_type(ft)}, got {self._fmt_type(at)}",
                                expr.line,
                                expr.column,
                            )
                        field_types.append(_type_info(
                            self._expand_type(ft), self._opaque_names()
                        ))
            result_type = type_name if is_self else _type_str(expr.type)
            self._ann_type(expr, result_type)
            if field_types:
                expr._typed_ann["field_types"] = field_types
            return result_type
        return None

    def _check_name(self, name: Name) -> Optional[str]:
        if len(name.parts) == 1:
            n = name.parts[0]
            info = self._lookup(n)
            if info is not None:
                if info.kind == "let" and not info.initialized:
                    self._record_error(
                        f"variable '{n}' is used before assignment",
                        name.line,
                        name.column,
                    )
                if info.node is not None:
                    name._typed_ann["binding"] = {
                        "kind": "var", "ref": info.node._typed_id
                    }
                self._ann_type(name, info.type)
                return info.type
            if n in self.functions:
                fn = self.functions[n]
                name._typed_ann["binding"] = {"kind": "fn", "ref": fn._typed_id}
                self._ann_type(name, "Fn")
                return "Fn"
            if n in self.consts:
                const = self.consts[n]
                name._typed_ann["binding"] = {
                    "kind": "const", "ref": const._typed_id
                }
                self._ann_type(name, _type_str(const.type))
                return _type_str(const.type)
            if n in BUILTIN_OBJECTS:
                name._typed_ann["binding"] = {"kind": "builtin", "ref": n}
                self._ann_type(name, BUILTIN_OBJECTS[n])
                return BUILTIN_OBJECTS[n]
            self._record_error(f"unknown identifier '{n}'", name.line, name.column)
            return None
        if len(name.parts) == 2:
            mod, member = name.parts
            if mod == "builtins":
                if member in BUILTIN_MODULE_FUNCTIONS:
                    name._typed_ann["binding"] = {
                        "kind": "builtin", "ref": member
                    }
                    self._ann_type(name, "Fn")
                    return "Fn"
                self._record_error(
                    f"unknown builtins:: member '{member}'",
                    name.line,
                    name.column,
                )
                return None
            if mod == "Self" and self.current_owner is not None:
                mod = self.current_owner
            struct = self.structs.get(mod)
            if struct is not None:
                for f in struct.fields:
                    if f.name == member and f.static:
                        name._typed_ann["binding"] = {
                            "kind": "field", "ref": f._typed_id
                        }
                        self._ann_type(name, _type_str(f.type))
                        return _type_str(f.type)
                binding = _find_method(self.methods.get(mod, []), member)
                if binding is not None:
                    name._typed_ann["binding"] = {
                        "kind": "method", "ref": binding.id
                    }
                    self._ann_type(name, "Fn")
                    return "Fn"
                self._record_error(
                    f"'{mod}' has no static member '{member}'",
                    name.line,
                    name.column,
                )
                return None
            enum = self.enums.get(mod)
            if enum is not None:
                for v in enum.variants:
                    if v.name == member:
                        name._typed_ann["binding"] = {
                            "kind": "variant", "ref": v._typed_id
                        }
                        self._ann_type(name, mod)
                        return mod
                self._record_error(
                    f"'{mod}' has no variant '{member}'",
                    name.line,
                    name.column,
                )
                return None
            self._record_error(f"unknown type '{mod}' in path", name.line, name.column)
            return None
        self._record_error("unsupported path expression", name.line, name.column)
        return None

    def _check_member(self, recv: Optional[str], member: str, node: Node) -> Optional[str]:
        recv = self._expand_type(recv)
        if recv is None:
            return None
        base = _base(recv)
        struct = self.structs.get(base)
        if struct is not None:
            for f in struct.fields:
                if f.name == member:
                    if f.static:
                        self._record_error(
                            f"static field '{member}' must be accessed via "
                            f"'{base}::{member}'",
                            node.line,
                            node.column,
                        )
                    node._typed_ann["member"] = {
                        "kind": "field", "ref": f._typed_id
                    }
                    struct_params = [p.name for p in struct.params]
                    subst = dict(zip(struct_params, _split_args(recv)))
                    ftype = _subst_type_str(_type_str(f.type), subst)
                    if any(
                        _type_mentions(ftype, name)
                        for name in set(struct_params) | self.active_generics
                    ):
                        self._ann_type(node, None)
                        return None
                    self._ann_type(node, ftype)
                    return ftype
            binding = _find_method(self.methods.get(base, []), member)
            if binding is not None:
                # A method referenced as a value (not called): record the
                # binding; the type is left unknown rather than guessed.
                node._typed_ann["member"] = {
                    "kind": "method", "ref": binding.id
                }
                self._ann_type(node, None)
                return None
            self._record_error(
                f"type '{base}' has no field '{member}'",
                node.line,
                node.column,
            )
            return None
        methods = BUILTIN_TYPE_METHODS.get(base)
        if methods is not None:
            spec = methods.get(member)
            if spec is not None:
                node._typed_ann["member"] = {
                    "kind": "builtin", "ref": member
                }
                resolved = self._resolve_return(spec.returns, recv)
                self._ann_type(node, resolved)
                return resolved
            self._record_error(f"type '{base}' has no member '{member}'", node.line, node.column)
            return None
        return None

    def _check_call(self, call: Call) -> Optional[str]:
        result = self._check_call_inner(call)
        self._ann_type(call, result)
        return result

    def _check_call_inner(self, call: Call) -> Optional[str]:
        arg_types = [self._check_expr(a.value) for a in call.args]
        callee = call.callee
        if isinstance(callee, Name):
            if len(callee.parts) == 1:
                n = callee.parts[0]
                if n in self.functions:
                    fn = self.functions[n]
                    result, subst = self._check_user_call(
                        fn, call, arg_types, is_method=False
                    )
                    callee._typed_ann["binding"] = {
                        "kind": "fn", "ref": fn._typed_id
                    }
                    self._ann_type(callee, "Fn")
                    self._ann_call(call, "fn", fn._typed_id, subst)
                    return result
                if n in BUILTIN_MODULE_FUNCTIONS:
                    self._check_builtin_call(n, call, arg_types)
                    callee._typed_ann["binding"] = {
                        "kind": "builtin", "ref": n
                    }
                    self._ann_type(callee, "Fn")
                    self._ann_call(call, "builtin", n)
                    return self._resolve_return(
                        BUILTIN_MODULE_FUNCTIONS[n].returns, None
                    ) or "None"
                self._record_error(f"unknown function '{n}'", call.line, call.column)
                return None
            if len(callee.parts) == 2:
                mod, member = callee.parts
                if mod == "builtins":
                    if member in BUILTIN_MODULE_FUNCTIONS:
                        self._check_builtin_call(member, call, arg_types)
                        callee._typed_ann["binding"] = {
                            "kind": "builtin", "ref": member
                        }
                        self._ann_type(callee, "Fn")
                        self._ann_call(call, "builtin", member)
                        return self._resolve_return(
                            BUILTIN_MODULE_FUNCTIONS[member].returns, None
                        ) or "None"
                    self._record_error(
                        f"unknown builtins:: function '{member}'",
                        call.line,
                        call.column,
                    )
                    return None
                if mod == "Self" and self.current_owner is not None:
                    mod = self.current_owner
                binding = _find_method(self.methods.get(mod, []), member)
                if binding is not None:
                    result, subst = self._check_user_call(
                        binding.fn,
                        call,
                        arg_types,
                        is_method=True,
                        owner_hint=mod,
                        binding=binding,
                    )
                    callee._typed_ann["binding"] = {
                        "kind": "method", "ref": binding.id
                    }
                    self._ann_type(callee, "Fn")
                    self._ann_call(call, "method", binding.id, subst)
                    return result
                builtin = BUILTIN_TYPE_METHODS.get(_base(self._expand_type(mod) or mod))
                if builtin is not None:
                    spec = builtin.get(member)
                    if spec is not None:
                        if spec.args and spec.args[0] == "Self":
                            self._record_error(
                                f"instance method '{member}' of '{mod}' must "
                                "be called on a value",
                                call.line,
                                call.column,
                            )
                            callee._typed_ann["binding"] = {
                                "kind": "builtin", "ref": member
                            }
                            self._ann_type(callee, "Fn")
                            self._ann_call(call, "builtin", member)
                            return self._resolve_return(spec.returns, mod)
                        self._check_spec_args(member, spec, call, arg_types, None)
                        callee._typed_ann["binding"] = {
                            "kind": "builtin", "ref": member
                        }
                        self._ann_type(callee, "Fn")
                        self._ann_call(call, "builtin", member)
                        return self._resolve_return(spec.returns, None)
                self._record_error(f"'{mod}' has no method '{member}'", call.line, call.column)
                return None
            self._record_error("unsupported call target", call.line, call.column)
            return None
        if isinstance(callee, Attribute):
            recv = self._expand_type(self._check_expr(callee.obj))
            if recv is None:
                return None
            base = _base(recv)
            binding = _find_method(self.methods.get(base, []), callee.name)
            if binding is not None:
                if binding.fn.static:
                    self._record_error(
                        f"static method '{callee.name}' must be called via "
                        f"'{base}::{callee.name}'",
                        call.line,
                        call.column,
                    )
                result, subst = self._check_user_call(
                    binding.fn,
                    call,
                    arg_types,
                    is_method=True,
                    owner_hint=recv,
                    binding=binding,
                )
                callee._typed_ann["member"] = {
                    "kind": "method", "ref": binding.id
                }
                self._ann_type(callee, result)
                self._ann_call(call, "method", binding.id, subst)
                return result
            if callee.name == "into":
                # `x.into()` resolves through user-declared conversions; the
                # impl lives on the target type, so it is not in the receiver's
                # own method table.
                targets = self.conversions.get(recv, [])
                if len(targets) == 1:
                    callee._typed_ann["member"] = {
                        "kind": "builtin", "ref": "into"
                    }
                    self._ann_type(callee, targets[0])
                    self._ann_call(call, "builtin", "into")
                    return targets[0]
                return None  # unknown source or ambiguous conversion
            methods = BUILTIN_TYPE_METHODS.get(base)
            if methods is not None:
                spec = methods.get(callee.name)
                if spec is not None:
                    self._check_spec_args(callee.name, spec, call, arg_types, recv)
                    callee._typed_ann["member"] = {
                        "kind": "builtin", "ref": callee.name
                    }
                    self._ann_type(callee, self._resolve_return(spec.returns, recv))
                    self._ann_call(call, "builtin", callee.name)
                    return self._resolve_return(spec.returns, recv)
                self._record_error(
                    f"type '{base}' has no method '{callee.name}'",
                    call.line,
                    call.column,
                )
                return None
            return None  # undeclared methods on user structs are tolerated
        self._record_error("cannot call this expression", call.line, call.column)
        return None

    def _check_user_call(
        self,
        fn: FnDecl,
        call: Call,
        arg_types: list[Optional[str]],
        *,
        is_method: bool,
        owner_hint: Optional[str] = None,
        binding: Optional[MethodBinding] = None,
    ) -> tuple[Optional[str], dict[str, str]]:
        params = fn.params
        if is_method and params and params[0].name == "self":
            params = params[1:]

        subst: dict[str, str] = {}
        generic_names: set[str] = {p.name for p in fn.type_params}
        if binding is not None:
            generic_names.update(binding.owner_params)
            recv = self._expand_type(owner_hint) if owner_hint is not None else None
            if recv is not None and binding.owner_struct is not None:
                target = self._expand_type(_type_str(binding.owner_struct))
                if target is not None and _base(target) == _base(recv):
                    for tp, ra in zip(_split_args(target), _split_args(recv)):
                        if tp in binding.owner_params:
                            subst[tp] = ra
            if recv is not None:
                struct = self.structs.get(_base(recv))
                if struct is not None:
                    for p, ra in zip(
                        [p.name for p in struct.params],
                        _split_args(recv),
                    ):
                        if p in binding.owner_params and p not in subst:
                            subst[p] = ra

        if not any(a.unpack for a in call.args):
            if len(call.args) != len(params):
                self._record_error(
                    f"function '{fn.name}' expects {len(params)} argument(s), "
                    f"got {len(call.args)}",
                    call.line,
                    call.column,
                )
            else:
                for i, (arg, param) in enumerate(zip(call.args, params)):
                    if param.type is None:
                        continue
                    self._unify_generic(
                        _subst_type_str(_type_str(param.type), subst),
                        arg_types[i],
                        subst,
                        generic_names,
                    )
                for i, (arg, param) in enumerate(zip(call.args, params)):
                    if param.type is None:
                        continue
                    expected = _type_str(param.type)
                    if expected == "Self" and owner_hint is not None:
                        expected = owner_hint
                    expected = self._resolve_use_type(
                        expected, subst, generic_names
                    )
                    if expected is None:
                        continue
                    if not self._compat_types(expected, arg_types[i]):
                        self._record_error(
                            f"argument {i + 1} of '{fn.name}' must be "
                            f"{self._fmt_type(expected)}, got {self._fmt_type(arg_types[i])}",
                            call.line,
                            call.column,
                        )
        ret = _type_str(fn.return_type) if fn.return_type is not None else "None"
        if ret == "Self" and owner_hint is not None:
            ret = owner_hint
        return self._resolve_use_type(ret, subst, generic_names), subst

    def _resolve_use_type(
        self,
        t: str,
        subst: dict[str, str],
        generic_names: set[str],
    ) -> Optional[str]:
        """Substitute generic parameters; return ``None`` when the result
        still depends on a type variable and cannot be checked concretely."""
        resolved = _subst_type_str(t, subst)
        variables = generic_names | set(self.active_generics)
        if any(_type_mentions(resolved, name) for name in variables):
            return None
        return resolved

    def _unify_generic(
        self,
        expected: str,
        actual: Optional[str],
        subst: dict[str, str],
        generic_names: set[str],
    ) -> None:
        """Infer generic parameters by structurally matching an expected
        argument type against the actual one (e.g. ``Vector<T>`` against
        ``Vector<Int>`` infers ``T = Int``)."""
        if actual is None:
            return
        if expected in generic_names:
            if expected not in subst:
                subst[expected] = actual
            return
        if expected == actual:
            return
        e_args = _split_args(expected)
        a_args = _split_args(actual)
        if (
            e_args
            and a_args
            and len(e_args) == len(a_args)
            and _base(expected) == _base(actual)
        ):
            for e, a in zip(e_args, a_args):
                self._unify_generic(e, a, subst, generic_names)

    def _check_builtin_call(
        self,
        name: str,
        call: Call,
        arg_types: list[Optional[str]],
    ) -> str:
        spec = BUILTIN_MODULE_FUNCTIONS[name]
        self._check_spec_args(name, spec, call, arg_types, None)
        return self._resolve_return(spec.returns, None) or "None"

    def _check_spec_args(
        self,
        name: str,
        spec: MethodSpec,
        call: Call,
        arg_types: list[Optional[str]],
        receiver: Optional[str],
    ) -> None:
        if any(a.unpack for a in call.args):
            return
        # An explicit leading `Self` marks an instance method; it is implicit
        # in the call and stripped for arity/type checking.  Absence marks a
        # static method whose declared arguments match the call directly.
        patterns = spec.patterns
        if patterns and patterns[0] == (1, "Self"):
            patterns = patterns[1:]
        expected = _match_arg_patterns(patterns, len(call.args))
        if expected is None:
            self._record_error(
                f"'{name}' expects {_patterns_arity_text(patterns)}, "
                f"got {len(call.args)}",
                call.line,
                call.column,
            )
            return
        for i, (arg, want) in enumerate(zip(call.args, expected)):
            if not self._arg_matches(want, arg_types[i], receiver):
                resolved = self._resolve_expected(want, receiver)
                expected_text = (
                    self._fmt_type(resolved) if resolved is not None else want
                )
                self._record_error(
                    f"argument {i + 1} of '{name}' must be {expected_text}, "
                    f"got {self._fmt_type(arg_types[i])}",
                    call.line,
                    call.column,
                )

    def _arg_matches(
        self,
        expected: str,
        actual: Optional[str],
        receiver: Optional[str],
    ) -> bool:
        if actual is None:
            return True
        if expected in ("Whatever", "Any"):
            return True
        if expected in ("Self", "SameTypeOther"):
            return receiver is None or self._compat_types(receiver, actual)
        if expected == "SameAsGeneric" or expected.startswith("SameAsGeneric:"):
            elem = _generic_arg(
                self._expand_type(receiver), _generic_ref_index(expected)
            )
            return elem is None or self._compat_types(elem, actual)
        if expected == "AnyInt":
            expanded = self._expand_type(actual)
            return expanded is not None and _base(expanded) in _INTEGER
        if expected == "EveryNumber":
            expanded = self._expand_type(actual)
            return expanded is not None and _base(expanded) in _NUMERIC
        if expected == "AnyGeneric":
            expanded = self._expand_type(actual)
            return expanded is not None and "<" in expanded
        return self._compat_types(expected, actual)

    def _resolve_return(self, ret: str, receiver: Optional[str]) -> Optional[str]:
        if ret in ("Self", "SameTypeOther"):
            return receiver
        if ret == "SameAsGeneric" or ret.startswith("SameAsGeneric:"):
            return _generic_arg(
                self._expand_type(receiver), _generic_ref_index(ret)
            )
        return ret

    def _resolve_expected(
        self, expected: str, receiver: Optional[str]
    ) -> Optional[str]:
        """Resolve dynamic placeholders to the concrete type they stand for."""
        if expected in ("Self", "SameTypeOther"):
            return receiver
        if expected == "SameAsGeneric" or expected.startswith("SameAsGeneric:"):
            return _generic_arg(
                self._expand_type(receiver), _generic_ref_index(expected)
            )
        return expected

    def _check_binop(
        self,
        op: TokenKind,
        left: Optional[str],
        right: Optional[str],
        node: Node,
    ) -> Optional[str]:
        if op in (TokenKind.AND, TokenKind.OR):
            for side, t in (("left", left), ("right", right)):
                if t is not None and not self._compat_types("Bool", t):
                    self._record_error(
                        f"'{op.value}' requires Bool operands, got {side} {self._fmt_type(t)}",
                        node.line,
                        node.column,
                    )
            return "Bool"
        if op in _EQUALITY:
            if left is not None and right is not None and not self._compat_types(left, right):
                self._record_error(
                    f"cannot compare {self._fmt_type(left)} with {self._fmt_type(right)}",
                    node.line,
                    node.column,
                )
            return "Bool"
        if op in _RELATIONAL:
            for side, t in (("left", left), ("right", right)):
                expanded = self._expand_type(t) if t is not None else None
                if expanded is not None and _base(expanded) not in _NUMERIC:
                    self._record_error(
                        f"'{op.value}' requires numeric operands, got {side} {self._fmt_type(t)}",
                        node.line,
                        node.column,
                    )
            return "Bool"
        if op in _BITWISE:
            for side, t in (("left", left), ("right", right)):
                expanded = self._expand_type(t) if t is not None else None
                if expanded is not None and _base(expanded) not in _INTEGER:
                    self._record_error(
                        f"'{op.value}' requires integer operands, got {side} {self._fmt_type(t)}",
                        node.line,
                        node.column,
                    )
            return "Int"
        if op in (TokenKind.PLUS, TokenKind.MINUS, TokenKind.STAR, TokenKind.SLASH, TokenKind.PERCENT):
            left_e = self._expand_type(left) if left is not None else None
            right_e = self._expand_type(right) if right is not None else None
            if op == TokenKind.PLUS and (left_e == "String" or right_e == "String"):
                other = right_e if left_e == "String" else left_e
                if other is not None and other != "String":
                    self._record_error(
                        f"cannot add String and {self._fmt_type(other)}",
                        node.line,
                        node.column,
                    )
                return "String"
            for side, t in (("left", left), ("right", right)):
                expanded = self._expand_type(t) if t is not None else None
                if expanded is not None and _base(expanded) not in _NUMERIC:
                    self._record_error(
                        f"'{op.value}' requires numeric operands, got {side} {self._fmt_type(t)}",
                        node.line,
                        node.column,
                    )
            if left_e == "Float" or right_e == "Float":
                return "Float"
            return left_e if left_e is not None and _base(left_e) in _NUMERIC else "Int"
        return None

    def _indexed_type(self, recv: Optional[str]) -> Optional[str]:
        recv = self._expand_type(recv)
        if recv is None:
            return None
        base = _base(recv)
        if base == "Map":
            args = _split_args(recv)
            return args[1] if len(args) >= 2 else None
        if base in ("Vector", "Set"):
            inner = recv[recv.find("<") + 1:-1] if "<" in recv else None
            return inner if inner and inner != "Any" else None
        if base == "String":
            return "String"
        return None

    def _element_type(self, t: Optional[str]) -> Optional[str]:
        t = self._expand_type(t)
        if t is None:
            return None
        base = _base(t)
        if base in ("Vector", "Set"):
            inner = t[t.find("<") + 1:-1] if "<" in t else None
            return inner if inner and inner != "Any" else None
        if base == "Map":
            return "Tuple"
        if base == "String":
            return "String"
        return None


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
