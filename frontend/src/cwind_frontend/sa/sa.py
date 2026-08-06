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

from dataclasses import dataclass, field
from typing import Optional

from ..ast_components.ast import (
    Assign,
    Attribute,
    BinOp,
    Block,
    Call,
    ConstDecl,
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
    BUILTIN_TRAITS,
    BUILTIN_TYPE_METHODS,
    MethodSpec,
)

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
    "Instance", "None", "Tuple", "Vector", "Map", "Set", "Iterator",
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


@dataclass
class VarInfo:
    name: str
    type: Optional[str]
    line: int
    column: int
    kind: str  # "param" | "let" | "const" | "field"


def _type_str(t: Type) -> str:
    if not t.args:
        return t.name
    return f"{t.name}<{', '.join(_type_str(a) for a in t.args)}>"


def _base(t: str) -> str:
    return t.split("<", 1)[0]


def _common_type(types: list[Optional[str]]) -> Optional[str]:
    seen = {t for t in types if t is not None}
    if len(seen) == 1:
        return next(iter(seen))
    return None


def _compatible(expected: Optional[str], actual: Optional[str]) -> bool:
    if expected is None or actual is None:
        return True
    if expected == "Any" or actual == "Any":
        return True
    if expected == actual:
        return True
    eb, ab = _base(expected), _base(actual)
    if eb == ab:
        return True
    if eb == "Instance" or ab == "Instance":
        return True
    if eb in _NUMERIC and ab in _NUMERIC:
        return True
    if eb == "Fn" or ab == "Fn":
        return True
    return False


def _find_method(methods: list[FnDecl], name: str) -> Optional[FnDecl]:
    for m in methods:
        if m.name == name:
            return m
    return None


class _Analyzer:
    def __init__(self) -> None:
        self.symbols: dict[str, Symbol] = {}
        self.defined: set[str] = set()
        self.errors: list[SaError] = []
        self.structs: dict[str, StructDecl] = {}
        self.methods: dict[str, list[FnDecl]] = {}
        self.functions: dict[str, FnDecl] = {}
        self.consts: dict[str, ConstDecl] = {}
        self.conversions: dict[str, list[str]] = {}  # source type -> target type(s)
        self.scopes: list[dict[str, VarInfo]] = []
        self.current_owner: Optional[str] = None

    def run(self, program: Program) -> ProgramInfo:
        # Pass 1: collect every top-level definition, detecting duplicates.
        for item in program.items:
            self._collect(item)
        # Pass 2: validate declaration-level references and type annotations.
        for item in program.items:
            self._check(item)
        # Pass 3: check function and method bodies.
        self._push_scope()
        for c in self.consts.values():
            self._declare(VarInfo(c.name, _type_str(c.type), c.line, c.column, "const"))
        for fn in self.functions.values():
            self._check_fn(fn, owner=None)
        for struct, methods in self.methods.items():
            for fn in methods:
                self._check_fn(fn, owner=struct)
        self._pop_scope()
        return ProgramInfo(symbols=self.symbols)

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
        self.symbols[name] = Symbol(name, kind, item.line, item.column)

    def _index(self, item: Node) -> None:
        if isinstance(item, StructDecl):
            self.structs[item.name] = item
        elif isinstance(item, FnDecl):
            self.functions[item.name] = item
        elif isinstance(item, ConstDecl):
            self.consts[item.name] = item
        elif isinstance(item, ImplDecl):
            self.methods.setdefault(item.struct.name, []).extend(item.methods)
        elif isinstance(item, ExtraDecl):
            self.methods.setdefault(item.struct, []).extend(item.methods)

    # -- pass 2: declarations ---------------------------------------------

    def _check(self, item: Node) -> None:
        if isinstance(item, TypeDecl):
            self._check_type(item.base, item)
            if item.where is not None:
                self._check_validation(item.where, [("self", _type_str(item.base))])
        elif isinstance(item, StructDecl):
            for f in item.fields:
                self._check_type(f.type, f)
                if f.validation is not None:
                    self._check_validation(f.validation, [(f.name, _type_str(f.type))])
        elif isinstance(item, ConstDecl):
            self._check_type(item.type, item)
            value = self._check_expr(item.value)
            if not _compatible(_type_str(item.type), value):
                self._record_error(
                    f"cannot initialize {_type_str(item.type)} with {value or 'unknown'}",
                    item.line,
                    item.column,
                )
        elif isinstance(item, TraitDecl):
            for m in item.methods:
                self._check_fn_types(m)
        elif isinstance(item, FnDecl):
            self._check_fn_types(item)
        elif isinstance(item, ImplDecl):
            self._require_trait(item.trait.name, item)
            self._require_type_target(item.struct.name, item, "struct")
            for arg in item.trait.args:
                self._check_type(arg, item)
            if item.trait.name == "From":
                self._check_from_impl(item)
            for m in item.methods:
                self._check_fn_types(m)
        elif isinstance(item, ExtraDecl):
            self._require_type_target(item.struct, item, "struct")
            for m in item.methods:
                self._check_fn_types(m)
        elif isinstance(item, GroupDecl):
            if item.struct is not None:
                self._require(item.struct, {"struct", "enum"}, item, "struct")
            for d in item.distributions:
                self._check_type(d.type, d)
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

    def _require_trait(self, name: str, ctx: Node) -> None:
        if name in BUILTIN_TRAITS:
            return
        self._require(name, {"trait"}, ctx, "trait")

    def _require_type_target(self, name: str, ctx: Node, what: str) -> None:
        if name in BUILTIN_TYPES:
            return
        self._require(name, {"struct", "enum"}, ctx, what)

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

    def _check_fn(self, fn: FnDecl, owner: Optional[str]) -> None:
        saved_owner = self.current_owner
        self.current_owner = owner
        self._push_scope()
        for p in fn.params:
            ptype: Optional[str]
            if p.name == "self" and owner is not None:
                ptype = owner
            else:
                ptype = _type_str(p.type) if p.type is not None else None
            self._declare(VarInfo(p.name, ptype, p.line, p.column, "param"))
        ret = _type_str(fn.return_type) if fn.return_type is not None else "None"
        if ret == "Self":
            if isinstance(owner, str):
                ret = owner
        if fn.body is not None:
            self._check_block(fn.body, ret)
        self._pop_scope()
        self.current_owner = saved_owner

    def _check_block(self, block: Block, return_type: str) -> None:
        self._push_scope()
        for stmt in block.stmts:
            self._check_stmt(stmt, return_type)
        self._pop_scope()

    def _check_validation(self, block: Block, vars: list[tuple[str, str]]) -> None:
        """Check a where/arrow validation block: every statement is a Bool
        condition, with the validated value(s) in scope."""
        self._push_scope()
        for name, t in vars:
            self._declare(VarInfo(name, t, block.line, block.column, "field"))
        for stmt in block.stmts:
            if isinstance(stmt, ExprStmt):
                self._check_condition(stmt.expr)
        self._pop_scope()

    def _check_stmt(self, stmt: Node, return_type: str) -> None:
        if isinstance(stmt, LetStmt):
            declared = _type_str(stmt.type) if stmt.type is not None else None
            value = self._check_expr(stmt.value) if stmt.value is not None else None
            if declared is None:
                self._record_error("let declaration requires a type", stmt.line, stmt.column)
            elif not _compatible(declared, value):
                self._record_error(
                    f"cannot initialize {declared} with {value or 'unknown'}",
                    stmt.line,
                    stmt.column,
                )
            self._declare(VarInfo(stmt.name, declared, stmt.line, stmt.column, "let"))
        elif isinstance(stmt, ReturnStmt):
            if stmt.value is None:
                if return_type != "None":
                    self._record_error(
                        f"function returns {return_type} but returned nothing",
                        stmt.line,
                        stmt.column,
                    )
                return
            value = self._check_expr(stmt.value)
            if not _compatible(return_type, value):
                self._record_error(
                    f"return type mismatch: expected {return_type}, got {value or 'unknown'}",
                    stmt.line,
                    stmt.column,
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
            self._check_block(stmt.body, return_type)
        elif isinstance(stmt, ForStmt):
            iterable = self._check_expr(stmt.iterable)
            var_type = self._element_type(iterable)
            self._push_scope()
            self._declare(VarInfo(stmt.var, var_type, stmt.line, stmt.column, "let"))
            self._check_block(stmt.body, return_type)
            self._pop_scope()
        elif isinstance(stmt, Block):
            self._check_block(stmt, return_type)

    def _check_condition(self, cond: Node) -> None:
        t = self._check_expr(cond)
        if t is not None and not _compatible("Bool", t):
            self._record_error(f"condition must be Bool, got {t}", cond.line, cond.column)

    # -- expressions -------------------------------------------------------

    def _check_expr(self, expr: Node) -> Optional[str]:
        if isinstance(expr, IntLit):
            return "Int"
        if isinstance(expr, FloatLit):
            return "Float"
        if isinstance(expr, StrLit):
            return "String"
        if isinstance(expr, Name):
            return self._check_name(expr)
        if isinstance(expr, Attribute):
            recv = self._check_expr(expr.obj)
            return self._check_member(recv, expr.name, expr)
        if isinstance(expr, Call):
            return self._check_call(expr)
        if isinstance(expr, Index):
            recv = self._check_expr(expr.obj)
            self._check_expr(expr.index)
            return self._indexed_type(recv)
        if isinstance(expr, Slice):
            recv = self._check_expr(expr.obj)
            for part in (expr.start, expr.stop, expr.step):
                if part is not None:
                    self._check_expr(part)
            return recv
        if isinstance(expr, UnaryOp):
            operand = self._check_expr(expr.operand)
            if expr.op == TokenKind.NOT:
                if operand is not None and not _compatible("Bool", operand):
                    self._record_error(
                        f"'!' requires a Bool operand, got {operand}",
                        expr.line,
                        expr.column,
                    )
                return "Bool"
            if expr.op in (TokenKind.MINUS, TokenKind.PLUS):
                if operand is not None and _base(operand) not in _NUMERIC:
                    self._record_error(
                        f"unary '{expr.op.value}' requires a numeric operand, got {operand}",
                        expr.line,
                        expr.column,
                    )
                return operand if operand is not None else "Int"
            return None
        if isinstance(expr, BinOp):
            left = self._check_expr(expr.left)
            right = self._check_expr(expr.right)
            return self._check_binop(expr.op, left, right, expr)
        if isinstance(expr, Assign):
            target = self._check_expr(expr.target)
            value = self._check_expr(expr.value)
            if not _compatible(target, value):
                self._record_error(
                    f"cannot assign {value or 'unknown'} to {target or 'unknown'}",
                    expr.line,
                    expr.column,
                )
            return value
        if isinstance(expr, VectorLit):
            elems = [self._check_expr(e) for e in expr.elems]
            elem = _common_type(elems)
            return f"Vector<{elem}>" if elem is not None else "Vector"
        if isinstance(expr, MapLit):
            for e in expr.entries:
                self._check_expr(e.key)
                self._check_expr(e.value)
            return "Map"
        if isinstance(expr, StructConstruct):
            self._require(expr.type.name, {"struct", "enum"}, expr, "struct")
            for a in expr.args:
                self._check_expr(a)
            return expr.type.name
        return None

    def _check_name(self, name: Name) -> Optional[str]:
        if len(name.parts) == 1:
            n = name.parts[0]
            info = self._lookup(n)
            if info is not None:
                return info.type
            if n in self.functions:
                return "Fn"
            if n in self.consts:
                return _type_str(self.consts[n].type)
            self._record_error(f"unknown identifier '{n}'", name.line, name.column)
            return None
        if len(name.parts) == 2:
            mod, member = name.parts
            if mod == "builtins":
                if member in BUILTIN_MODULE_FUNCTIONS:
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
            if struct is None:
                self._record_error(f"unknown type '{mod}' in path", name.line, name.column)
                return None
            for f in struct.fields:
                if f.name == member and f.static:
                    return _type_str(f.type)
            if _find_method(self.methods.get(mod, []), member) is not None:
                return "Fn"
            self._record_error(f"'{mod}' has no static member '{member}'", name.line, name.column)
            return None
        self._record_error("unsupported path expression", name.line, name.column)
        return None

    def _check_member(self, recv: Optional[str], member: str, node: Node) -> Optional[str]:
        if recv is None:
            return None
        base = _base(recv)
        struct = self.structs.get(base)
        if struct is not None:
            for f in struct.fields:
                if f.name == member:
                    return _type_str(f.type)
            return None  # undeclared members on structs are tolerated
        methods = BUILTIN_TYPE_METHODS.get(base)
        if methods is not None:
            spec = methods.get(member)
            if spec is not None:
                return self._resolve_return(spec.returns, recv)
            self._record_error(f"type '{base}' has no member '{member}'", node.line, node.column)
            return None
        return None

    def _check_call(self, call: Call) -> Optional[str]:
        arg_types = [self._check_expr(a.value) for a in call.args]
        callee = call.callee
        if isinstance(callee, Name):
            if len(callee.parts) == 1:
                n = callee.parts[0]
                if n in self.functions:
                    return self._check_user_call(
                        self.functions[n], call, arg_types, is_method=False
                    )
                if n in BUILTIN_MODULE_FUNCTIONS:
                    return self._check_builtin_call(n, call, arg_types)
                self._record_error(f"unknown function '{n}'", call.line, call.column)
                return None
            if len(callee.parts) == 2:
                mod, member = callee.parts
                if mod == "builtins":
                    if member in BUILTIN_MODULE_FUNCTIONS:
                        return self._check_builtin_call(member, call, arg_types)
                    self._record_error(
                        f"unknown builtins:: function '{member}'",
                        call.line,
                        call.column,
                    )
                    return None
                if mod == "Self" and self.current_owner is not None:
                    mod = self.current_owner
                fn = _find_method(self.methods.get(mod, []), member)
                if fn is not None:
                    return self._check_user_call(
                        fn, call, arg_types, is_method=True, owner_hint=mod
                    )
                self._record_error(f"'{mod}' has no method '{member}'", call.line, call.column)
                return None
            self._record_error("unsupported call target", call.line, call.column)
            return None
        if isinstance(callee, Attribute):
            recv = self._check_expr(callee.obj)
            if recv is None:
                return None
            base = _base(recv)
            fn = _find_method(self.methods.get(base, []), callee.name)
            if fn is not None:
                return self._check_user_call(
                    fn, call, arg_types, is_method=True, owner_hint=recv
                )
            if callee.name == "into":
                # `x.into()` resolves through user-declared conversions; the
                # impl lives on the target type, so it is not in the receiver's
                # own method table.
                targets = self.conversions.get(recv, [])
                if len(targets) == 1:
                    return targets[0]
                return None  # unknown source or ambiguous conversion
            methods = BUILTIN_TYPE_METHODS.get(base)
            if methods is not None:
                spec = methods.get(callee.name)
                if spec is not None:
                    self._check_spec_args(callee.name, spec, call, arg_types, recv)
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
    ) -> Optional[str]:
        params = fn.params
        if is_method and params and params[0].name == "self":
            params = params[1:]
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
                    expected = _type_str(param.type)
                    if expected == "Self" and owner_hint is not None:
                        expected = owner_hint
                    if not _compatible(expected, arg_types[i]):
                        self._record_error(
                            f"argument {i + 1} of '{fn.name}' must be {expected}, "
                            f"got {arg_types[i]}",
                            call.line,
                            call.column,
                        )
        ret = _type_str(fn.return_type) if fn.return_type is not None else "None"
        if ret == "Self" and owner_hint is not None:
            ret = owner_hint
        return ret

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
        params = spec.args[1:] if spec.args and spec.args[0] == "Self" else spec.args
        if not spec.variadic and len(call.args) != len(params):
            self._record_error(
                f"'{name}' expects {len(params)} argument(s), got {len(call.args)}",
                call.line,
                call.column,
            )
            return
        for i, (arg, expected) in enumerate(zip(call.args, params)):
            if not self._arg_matches(expected, arg_types[i], receiver):
                self._record_error(
                    f"argument {i + 1} of '{name}' must be {expected}, got {arg_types[i]}",
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
            return receiver is None or _compatible(receiver, actual)
        if expected == "SameAsGeneric":
            elem = self._generic_of(receiver)
            return elem is None or _compatible(elem, actual)
        if expected == "AnyInt":
            return _base(actual) in _INTEGER
        if expected == "EveryNumber":
            return _base(actual) in _NUMERIC
        return _compatible(expected, actual)

    def _generic_of(self, t: Optional[str]) -> Optional[str]:
        if t is None or "<" not in t:
            return None
        return t[t.find("<") + 1:-1]

    def _resolve_return(self, ret: str, receiver: Optional[str]) -> Optional[str]:
        if ret in ("Self", "SameTypeOther"):
            return receiver
        if ret == "SameAsGeneric":
            return self._generic_of(receiver)
        return ret

    def _check_binop(
        self,
        op: TokenKind,
        left: Optional[str],
        right: Optional[str],
        node: Node,
    ) -> Optional[str]:
        if op in (TokenKind.AND, TokenKind.OR):
            for side, t in (("left", left), ("right", right)):
                if t is not None and not _compatible("Bool", t):
                    self._record_error(
                        f"'{op.value}' requires Bool operands, got {side} {t}",
                        node.line,
                        node.column,
                    )
            return "Bool"
        if op in _EQUALITY:
            return "Bool"
        if op in _RELATIONAL:
            for side, t in (("left", left), ("right", right)):
                if t is not None and _base(t) not in _NUMERIC:
                    self._record_error(
                        f"'{op.value}' requires numeric operands, got {side} {t}",
                        node.line,
                        node.column,
                    )
            return "Bool"
        if op in _BITWISE:
            for side, t in (("left", left), ("right", right)):
                if t is not None and _base(t) not in _INTEGER:
                    self._record_error(
                        f"'{op.value}' requires integer operands, got {side} {t}",
                        node.line,
                        node.column,
                    )
            return "Int"
        if op in (TokenKind.PLUS, TokenKind.MINUS, TokenKind.STAR, TokenKind.SLASH, TokenKind.PERCENT):
            if op == TokenKind.PLUS and (left == "String" or right == "String"):
                return "String"
            for side, t in (("left", left), ("right", right)):
                if t is not None and _base(t) not in _NUMERIC:
                    self._record_error(
                        f"'{op.value}' requires numeric operands, got {side} {t}",
                        node.line,
                        node.column,
                    )
            if left == "Float" or right == "Float":
                return "Float"
            return left if left is not None and _base(left) in _NUMERIC else "Int"
        return None

    def _indexed_type(self, recv: Optional[str]) -> Optional[str]:
        if recv is None:
            return None
        base = _base(recv)
        if base == "Map":
            return "Any"
        if base in ("Vector", "Set"):
            inner = recv[recv.find("<") + 1:-1] if "<" in recv else None
            return inner if inner and inner != "Any" else None
        if base == "String":
            return "String"
        return None

    def _element_type(self, t: Optional[str]) -> Optional[str]:
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
