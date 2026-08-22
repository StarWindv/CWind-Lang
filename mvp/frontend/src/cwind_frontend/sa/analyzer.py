"""Semantic analyzer core: state, pass orchestration and public entry points."""

from __future__ import annotations

from dataclasses import fields as _fields
from typing import Optional, Union

from .smt import BodyChecks
from .declarations import DeclarationChecks
from .expressions import ExpressionChecks
from .errors import SaError, SaResult, SaWarning
from .symbols import (
    BindingInfo,
    MethodBinding,
    ProgramInfo,
    Symbol,
    VarInfo,
)
from .types import _type_info, _type_str
from ..ast_components.ast import (
    Attribute,
    Block,
    Call,
    ConstDecl,
    EnumDecl,
    ExprStmt,
    ExtraDecl,
    FnDecl,
    ForStmt,
    IfLetStmt,
    IfStmt,
    ImplDecl,
    MatchStmt,
    Name,
    Node,
    Program,
    ReturnStmt,
    StructDecl,
    TraitDecl,
    Type,
    TypeDecl,
    TypeParam,
    WhileStmt,
)

__all__ = ["run_sa", "run_sa_with_errors", "_Analyzer"]


class _Analyzer(DeclarationChecks, BodyChecks, ExpressionChecks):
    def __init__(self) -> None:
        self.symbols: dict[str, Symbol] = {}
        self.defined: set[str] = set()
        self.errors: list[SaError] = []
        self.warnings: list[SaWarning] = []
        self.structs: dict[str, StructDecl] = {}
        self.enums: dict[str, EnumDecl] = {}
        self.traits: dict[str, TraitDecl] = {}
        self.groups: dict[str, GroupDecl] = {}
        self.type_aliases: dict[str, TypeDecl] = {}
        self.impls: dict[str, list[str]] = {}  # struct name -> trait names
        self.into_impls: set[tuple[str, str]] = set()
        self.methods: dict[str, list[MethodBinding]] = {}
        self.functions: dict[str, FnDecl] = {}
        self.consts: dict[str, ConstDecl] = {}
        self.const_values: dict[str, int] = {}
        self.const_floats: dict[str, float] = {}
        self.fn_folded: dict[str, Optional[Union[int, float]]] = {}
        self._folding_fns: set[str] = set()
        self.conversions: dict[str, list[str]] = {}  # source type -> target type(s)
        self.scopes: list[dict[str, VarInfo]] = []
        self.current_owner: Optional[str] = None
        self.current_owner_type: Optional[str] = None
        self.active_generics: frozenset[str] = frozenset()
        self.loop_depth: int = 0
        self._next_node_id: int = 1
        self._next_binding_id: int = 1
        self._binding_order: list[tuple[str, MethodBinding]] = []
        self._which_hooked: dict[tuple[str, str], str] = {}

    def run(self, program: Program) -> ProgramInfo:
        # which 钩子: 在 SA 检查前把 `self.<hook>()` 插到被钩方法的每个
        # return 前 (无 return 时放在函数体尾部), 这样注入的调用也走同一套
        # 语义检查, 后端不需要再做任何 AOP 特殊处理。
        self._inline_which_hooks(program)
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
        # Pass 2.5: fold top-level function return values so call sites can
        # see them (e.g. `fn t6() -> UInt8 { return 55 + 1; }` folds to 56).
        for fn in self.functions.values():
            self.fn_folded[fn.name] = self._fold_fn_return(fn)
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
    def _inline_which_hooks(self: "_Analyzer", program: Program) -> None:
        if getattr(program, "_which_inlined", False):
            return
        for item in program.items:
            if not isinstance(item, (ExtraDecl, ImplDecl)):
                continue
            owner = item.struct.name
            table = {fn.name: fn for fn in item.methods}
            for fn in table.values():
                if fn.which is None or fn.body is None:
                    continue
                target = table.get(fn.which)
                if target is None:
                    self._record_error(
                        f"which target '{fn.which}' must be declared in the "
                        f"same '{owner}' block as the hook",
                        fn.line,
                        fn.column,
                    )
                    continue
                if target is fn:
                    self._record_error(
                        f"which method '{fn.name}' cannot hook itself",
                        fn.line,
                        fn.column,
                    )
                    continue
                if target.which is not None:
                    self._record_error(
                        f"which target '{fn.which}' is itself a which hook",
                        fn.line,
                        fn.column,
                    )
                    continue
                if target.body is None:
                    self._record_error(
                        f"which target '{fn.which}' must have a body",
                        fn.line,
                        fn.column,
                    )
                    continue
                key = (owner, fn.which)
                if key in self._which_hooked:
                    self._record_error(
                        f"'{owner}::{fn.which}' already has a which hook "
                        f"('{self._which_hooked[key]}')",
                        fn.line,
                        fn.column,
                    )
                    continue
                self._which_hooked[key] = fn.name
                self._insert_hook_before_returns(
                    target.body, fn.name, fn.line, fn.column
                )
        program._which_inlined = True

    def _make_hook_call_stmt(
        self: "_Analyzer", hook_name: str, line: int, column: int
    ) -> ExprStmt:
        recv = Name(line, column, ["self"])
        callee = Attribute(line, column, recv, hook_name)
        call = Call(line, column, callee, [])
        call._synthetic = True
        return ExprStmt(line, column, call)

    def _insert_hook_before_returns(
        self: "_Analyzer",
        block: Block,
        hook_name: str,
        line: int,
        column: int,
    ) -> None:
        new_stmts: list[Node] = []
        for stmt in block.stmts:
            if isinstance(stmt, ReturnStmt):
                new_stmts.append(
                    self._make_hook_call_stmt(hook_name, line, column)
                )
            new_stmts.append(stmt)
            if isinstance(stmt, IfStmt):
                self._insert_hook_before_returns(
                    stmt.then, hook_name, line, column
                )
                for branch in stmt.elifs:
                    self._insert_hook_before_returns(
                        branch.then, hook_name, line, column
                    )
                if stmt.else_ is not None:
                    self._insert_hook_before_returns(
                        stmt.else_, hook_name, line, column
                    )
            elif isinstance(stmt, WhileStmt):
                self._insert_hook_before_returns(
                    stmt.body, hook_name, line, column
                )
            elif isinstance(stmt, MatchStmt):
                for arm in stmt.arms:
                    self._insert_hook_before_returns(
                        arm.body, hook_name, line, column
                    )
            elif isinstance(stmt, IfLetStmt):
                self._insert_hook_before_returns(
                    stmt.then, hook_name, line, column
                )
                for branch in stmt.elifs:
                    self._insert_hook_before_returns(
                        branch.body, hook_name, line, column
                    )
                if stmt.else_ is not None:
                    self._insert_hook_before_returns(
                        stmt.else_, hook_name, line, column
                    )
            elif isinstance(stmt, ForStmt):
                self._insert_hook_before_returns(
                    stmt.body, hook_name, line, column
                )
            elif isinstance(stmt, Block):
                self._insert_hook_before_returns(
                    stmt, hook_name, line, column
                )
        block.stmts = new_stmts
        # 兜底: 若函数体尾部可落到隐式 return, 也补一次钩子调用。
        block.stmts.append(self._make_hook_call_stmt(hook_name, line, column))

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
        self._check_type_param_bounds(params)
        for tp in params:
            if tp.bound is not None:
                self._annotate_type_node(tp.bound, opaque)

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

    def _require_mutable(self, info: VarInfo, node: Node) -> None:
        """Reject writes to immutable local bindings and parameters."""
        if info.kind not in ("let", "param") or info.mutable:
            return
        subject = "parameter" if info.kind == "param" else "variable"
        self._record_error(
            f"cannot assign to {subject} '{info.name}'; declare it with 'mut'",
            node.line,
            node.column,
        )

    def _lookup(self, name: str) -> Optional[VarInfo]:
        for scope in reversed(self.scopes):
            if name in scope:
                return scope[name]
        return None

    def _record_error(self, message: str, line: int, column: int) -> None:
        self.errors.append(SaError(message, line, column))

    def _record_warning(self, message: str, line: int, column: int) -> None:
        self.warnings.append(SaWarning(message, line, column))


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
    return SaResult(
        info,
        list(analyzer.errors),
        list(analyzer.warnings),
    )
