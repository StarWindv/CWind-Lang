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
from .types import BUILTIN_TYPES, _base, _type_info, _type_str
from ..ast_components.ast import (
    Attribute,
    Block,
    Call,
    ConstDecl,
    EnumDecl,
    ExprStmt,
    ExtraDecl,
    Field,
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
    UseDecl,
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
        # todo-156: (struct name, trait name) recorded by ``impl !Trait for S``.
        # Consulted before positive satisfaction so a negative impl wins.
        self.negative_impls: set[tuple[str, str]] = set()
        self.into_impls: set[tuple[str, str]] = set()
        self.methods: dict[str, list[MethodBinding]] = {}
        # todo-122: associated constants by owner struct name (extra blocks)
        self.extra_consts: dict[str, list["ConstDecl"]] = {}
        self.functions: dict[str, FnDecl] = {}
        self.consts: dict[str, ConstDecl] = {}
        self.extern_statics: dict[str, "ExternStatic"] = {}
        self.const_values: dict[str, int] = {}
        self.const_floats: dict[str, float] = {}
        self.fn_folded: dict[str, Optional[Union[int, float]]] = {}
        self._folding_fns: set[str] = set()
        self.conversions: dict[str, list[str]] = {}  # source type -> target type(s)
        self.scopes: list[dict[str, VarInfo]] = []
        self.current_owner: Optional[str] = None
        self.current_owner_type: Optional[str] = None
        # todo-90: defining file of the code currently being checked
        # (parser runtime attribute ``source_module``).  ``None`` means the
        # context is untagged (stdin/tests): visibility stays permissive.
        self.current_module: Optional[str] = None
        # todo-79: per-file bare-name visibility sets built by the parser
        # (``Program._module_table``).  ``current_visible`` mirrors
        # ``current_module`` for the code under check; ``None`` keeps the
        # legacy permissive behavior (stdin / in-memory sources).
        self._module_visible: Optional[dict[str, frozenset[str]]] = None
        self.current_visible: Optional[frozenset[str]] = None
        self.active_generics: frozenset[str] = frozenset()
        # 泛型参数名 -> ``Into<Target>`` 约束目标 (bug-21):
        # 让 ``value.into()`` 能按声明的约束解析, 而不是只在具体类型上查表。
        self.generic_bounds: dict[str, str] = {}
        self._bounds_frames: list[dict[str, Optional[str]]] = []
        self.loop_depth: int = 0
        self._next_node_id: int = 1
        self._next_binding_id: int = 1
        self._binding_order: list[tuple[str, MethodBinding]] = []
        self._which_hooked: dict[tuple[str, str], str] = {}
        # todo-144: 类型名 -> 定义位置的规范模块路径 ("std::option")。
        # 填充于索引期 (仅 Struct/Enum/Type/Trait 声明), 供 typed-AST
        # 类型对象补 "def" 字段; 内建与类型形参查不到, 保持无 def。
        self._def_paths: dict[str, str] = {}
        # todo-69: module aliases declared by ``use a::b;`` and imported
        # module paths.  The latter is exposed through ProgramInfo so typed
        # AST can preserve provenance without duplicating files.
        # todo-77: each alias also carries its export surface (names the
        # importer may address as ``alias::name``) and the module's full
        # top-level name inventory (for precise privacy diagnostics).
        self.modules: dict[str, list[str]] = {}
        self.module_exports: dict[str, frozenset[str]] = {}
        self.module_known: dict[str, frozenset[str]] = {}
        self.imported_modules: list[str] = []
        self._module_sources: dict[str, Program] = {}
        self._module_item_owners: dict[int, Optional[str]] = {}
        # todo-76/78: one manifest entry per ``use`` declaration, in source
        # order.  ``auto`` marks the implicit prelude import.
        self.import_manifest: list[dict] = []

    def run(self, program: Program) -> ProgramInfo:
        # which 钩子: 在 SA 检查前把 `self.<hook>()` 插到被钩方法的每个
        # return 前 (无 return 时放在函数体尾部), 这样注入的调用也走同一套
        # 语义检查, 后端不需要再做任何 AOP 特殊处理。
        self._inline_which_hooks(program)
        for item in program.items:
            if isinstance(item, UseDecl):
                self.import_manifest.append({
                    "path": list(item.parts),
                    "source": item.module,
                    "item": getattr(item, "item", None),
                    "wildcard": bool(getattr(item, "wildcard", False)),
                    "auto": bool(getattr(item, "auto", False)),
                    "pub": bool(item.pub),
                    "alias": getattr(item, "alias", None),
                    "crate_export": bool(getattr(item, "crate_export", False)),
                })
                if item.module is None:
                    self._record_error(
                        "use declaration was not resolved to a module",
                        item.line,
                        item.column,
                    )
                else:
                    # todo-124: an `as` rename replaces the natural
                    # last-path-segment alias for module-namespace imports.
                    alias = getattr(item, "alias", None) or item.parts[-1]
                    is_item_import = (
                        getattr(item, "item", None) is not None
                        and not getattr(item, "wildcard", False)
                    )
                    # Explicit ``use m::item;`` introduces no module
                    # namespace: the item is referenced bare, and registering
                    # its name as an alias would shadow enum/struct access
                    # such as ``Option::Some``.
                    if not item.auto and not is_item_import:
                        previous = self.modules.get(alias)
                        if previous is not None and previous != item.parts:
                            self._record_error(
                                f"ambiguous import '{alias}'",
                                item.line,
                                item.column,
                            )
                        elif previous is None:
                            self.modules[alias] = list(item.parts)
                            self.module_exports[alias] = frozenset(
                                getattr(item, "exported_names", ())
                            )
                            self.module_known[alias] = frozenset(
                                getattr(item, "known_names", ())
                            )
                    # Imported declarations are already present in the root
                    # Program when the parser flattened them; the parser
                    # tagged every item with its defining file at parse
                    # time (todo-90 ``source_module``), so only provenance
                    # bookkeeping remains here.
                    if item.module not in self.imported_modules:
                        self.imported_modules.append(item.module)
                # todo-124: provenance is keyed by the alias actually used
                # to address the module from this file.
                self._module_sources[
                    getattr(item, "alias", None) or item.parts[-1]
                ] = item.module
        # todo-79: consume the parser's module scope table so references can
        # be gated by what the referring file actually declared or imported.
        table = getattr(program, "_module_table", None)
        if isinstance(table, dict) and table:
            self._module_visible = {
                home: data["visible"] for home, data in table.items()
            }
        # Number every AST node (pre-order, parents before children) so
        # symbols / bindings / annotations can reference nodes by id.
        self._assign_ids(program)
        # Pass 1: collect every top-level definition, detecting duplicates.
        for item in program.items:
            self._collect(item)
        # Pass 1.2 (bug-43): expand type aliases in impl/extra targets
        # *before* trait-conformance validation.  ``impl MyT<i32> for i32``
        # must be validated as the underlying ``Int32`` builtin (Rust never
        # sees aliases at coherence time either).  Tables built during
        # pass 1 are re-keyed so method lookup finds the canonical owner.
        self._expand_impl_target_aliases(program)
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
            saved_visible = self.current_visible
            self.current_visible = self._visible_for(item)
            try:
                self._check(item)
            finally:
                self.current_visible = saved_visible
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
            self._push_into_bounds(fn.type_params)
            self._check_fn(
                fn,
                owner=None,
                generic=frozenset(p.name for p in fn.type_params),
            )
            self._pop_into_bounds()
        for struct, methods in self.methods.items():
            for binding in methods:
                fn = binding.fn
                owner_generic = frozenset(binding.owner_params)
                fn_generic = frozenset(p.name for p in fn.type_params)
                # impl/extra 声明的泛型参数约束与方法自身约束都只在
                # 方法体内生效 (bug-21)。
                self._push_into_bounds(getattr(binding.decl, "params", None))
                self._push_into_bounds(fn.type_params)
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
                self._pop_into_bounds()
                self._pop_into_bounds()
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
        return ProgramInfo(
            symbols=self.symbols,
            bindings=bindings,
            modules=self.modules,
            imported_modules=self.imported_modules,
            import_manifest=self.import_manifest,
            def_paths=dict(self._def_paths),
        )

    # -- typed-AST metadata ----------------------------------------------
    def _expand_impl_target_aliases(self: "_Analyzer", program: Program) -> None:
        """bug-43: expand type aliases in impl/extra target types.

        ``impl MyT<i32> for i32`` is validated (and its methods registered)
        against the alias's underlying type so alias and base spellings stay
        one coherent implementation (``typedef i32 = Int32;`` + this impl
        must equal ``impl MyT for Int32``).  The pass-1 tables keyed by the
        raw alias spelling are re-keyed to the expanded name.
        """
        for item in program.items:
            if not isinstance(item, (ImplDecl, ExtraDecl)):
                continue
            raw = item.struct.name
            if raw in BUILTIN_TYPES or raw not in self.type_aliases:
                continue
            expanded = self._expand_type(raw)
            if not expanded or expanded == raw:
                continue
            item.struct.name = expanded
            if raw in self.impls:
                self.impls.setdefault(expanded, []).extend(
                    self.impls.pop(raw)
                )
            if raw in self.methods:
                self.methods.setdefault(expanded, []).extend(
                    self.methods.pop(raw)
                )
            if raw != expanded:
                self._binding_order = [
                    (expanded if owner == raw else owner, binding)
                    for owner, binding in self._binding_order
                ]
            if (
                isinstance(item, ImplDecl)
                and item.trait.name == "Into"
                and len(item.trait.args) == 1
            ):
                # into_impls was seeded with the raw spelling in pass 1;
                # re-seed with the expanded owner so `.into()` resolution
                # and the duplicate check see one canonical pair.
                self.into_impls.discard(
                    (raw, _type_str(item.trait.args[0]))
                )
                self.into_impls.add(
                    (expanded, _type_str(item.trait.args[0]))
                )

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

    def _push_generics(
        self, names: "set[str] | frozenset[str]"
    ) -> tuple[frozenset[str], set[str]]:
        """bug-51: enter a generic-parameter scope (pass-2 signatures).

        Generic parameters previously entered ``defined`` via a plain
        ``defined |= generic`` / ``defined -= generic`` pair, which corrupted
        global state whenever a parameter shadowed an existing type
        (``struct Box<T>`` removed ``struct T``'s registration after its
        check, so every later ``Box<T>`` reference died with "unknown type").
        Both ``defined`` and ``active_generics`` are now snapshot/restored
        around the scope; returns the saved frame for :meth:`_pop_generics`.
        """
        frame = (self.active_generics, self.defined)
        fro = frozenset(names)
        self.active_generics = self.active_generics | fro
        self.defined = self.defined | fro
        return frame

    def _pop_generics(
        self, frame: tuple[frozenset[str], set[str]]
    ) -> None:
        self.active_generics, self.defined = frame

    def _opaque_names(self, extra: Optional[frozenset[str]] = None) -> frozenset[str]:
        if extra is None:
            return self.active_generics
        return frozenset(extra) | self.active_generics

    def _type_def_path(self: "_Analyzer", name: str) -> Optional[str]:
        """todo-144: definition-site module path of a canonical type name.

        内建类型 (基础数值/基础容器/String 等, `BUILTIN_TYPES` 白名单) 与
        当前作用域内的类型形参 (T/Self 等绑定名, PROBLEMS-FINAL 第 3 条
        第 2 点) 没有定义位置, 返回 ``None`` 保持无 ``def`` 字段。
        """
        if name in BUILTIN_TYPES:
            return None
        if name in self.active_generics:
            return None
        return self._def_paths.get(name)

    def _flat_inner_def(self: "_Analyzer", name: str) -> Optional[str]:
        """todo-146: ``def`` of the base type inside a flat pointer/array
        name (``*const Node`` / ``[Node; 4]``).  One level only: nested
        flat compositions (``*const [Node; 4]``) stay ``def``-less for now.
        """
        inner = name
        if inner.startswith(("*const ", "*mut ")):
            inner = inner.split(" ", 1)[1]
        elif inner.startswith("["):
            inner = inner.split(";", 1)[0].strip().lstrip("[").strip()
        else:
            return None
        if not inner or inner.startswith(("*", "[", "fn(")):
            return None
        return self._type_def_path(_base(inner))

    def _enrich_type_info(
        self: "_Analyzer",
        info: Any,
        original: Optional[str] = None,
    ) -> None:
        """todo-144: add ``def`` / ``alias`` provenance to a type object.

        ``def`` 是按定义位置展开的规范模块路径 (用户裁决: 不按使用处拼写
        展开, `pub use` 重导出的多条路径全部归一); ``alias`` 记录被展开
        掉的原始拼写 (typedef 或 use 改名)。递归进 ``args``; 扁平编码的
        指针/数组名拆出被指/元素基名的 ``def`` (todo-146), 函数签名的
        参数/返回段已按结构递归覆盖。展开循环守卫见 ``_expand_type``
        (防 todo-132 后 ``std::builtins::X`` 自解析成环)。
        """
        if not isinstance(info, dict):
            return
        name = info.get("name")
        if isinstance(name, str):
            if name.startswith(("*const ", "*mut ", "[")):
                def_path = self._flat_inner_def(name)
                if def_path is not None:
                    info["def"] = def_path
            elif not name.startswith("fn("):
                def_path = self._type_def_path(name)
                if def_path is not None:
                    info["def"] = def_path
                if (
                    original is not None
                    and original != name
                    and original in self.type_aliases
                    and "alias" not in info
                ):
                    info["alias"] = original
        for arg in info.get("args") or ():
            self._enrich_type_info(arg)

    def _type_info_enriched(
        self: "_Analyzer",
        t: Optional[str],
        opaque: Optional[frozenset[str]] = None,
        original: Optional[str] = None,
    ) -> Optional[dict]:
        """``_type_info`` + todo-144 provenance; single entry for ann."""
        if original is None and t is not None:
            original = _base(t)
        if t is not None:
            t = self._expand_type(t)
        info = _type_info(t, self._opaque_names(opaque))
        if info is not None:
            self._enrich_type_info(info, original)
        return info

    def _ann_type(
        self,
        node: Node,
        t: Optional[str],
        opaque: Optional[frozenset[str]] = None,
    ) -> None:
        """Record ``ann.type`` (expanded) or ``ann.opaque`` on a node."""
        info = self._type_info_enriched(t, opaque)
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
                name: (
                    enriched
                    if (enriched := self._type_info_enriched(t)) is not None
                    else _type_info(self._expand_type(t), self._opaque_names())
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
        into its argument nodes.  Aliases are expanded into the annotation
        (bug-33/35: ``u32``/``[u32; N]`` -> ``UInt32``/``[UInt32; N]``) so
        backend consumers reading ``ann.type`` always see canonical names.

        todo-144: the pre-expansion spelling of each level survives in
        ``ann.type.alias``; ``_ann_type`` re-expands internally."""
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

    def _push_into_bounds(
        self, params: Optional[list["TypeParam"]] = None
    ) -> None:
        """Enter the ``T: Into<Target>`` bounds declared by ``params``.

        Bounds are scoped like ``defined``/``active_generics``: a nested
        declaration may shadow an outer parameter of the same name, so each
        frame remembers the previous entry for exact restoration.
        """
        frame: dict[str, Optional[str]] = {}
        for p in params or ():
            b = p.bound
            if b is None or b.name != "Into" or len(b.args) != 1:
                continue
            frame.setdefault(p.name, self.generic_bounds.get(p.name))
            self.generic_bounds[p.name] = _type_str(b.args[0])
        self._bounds_frames.append(frame)

    def _pop_into_bounds(self) -> None:
        """Leave the innermost ``_push_into_bounds`` frame."""
        for name, old in self._bounds_frames.pop().items():
            if old is None:
                self.generic_bounds.pop(name, None)
            else:
                self.generic_bounds[name] = old

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

    def _check_field_visibility(
        self,
        struct: StructDecl,
        field: "Field",
        base: str,
        node: Node,
    ) -> None:
        """todo-90: reject access to a non-pub field from another module.

        Fields default to private (Rust semantics): they are accessible only
        within the file that declares the struct.  Both sides untagged
        (stdin/tests) or either side without a ``source_module`` tag keeps
        the legacy permissive behavior.
        """
        if field.pub:
            return
        owner = getattr(struct, "source_module", None)
        current = self.current_module
        if owner is None or current is None or owner == current:
            return
        self._record_error(
            f"field '{field.name}' of struct '{base}' is private "
            "(declare it 'pub' to access it from other modules)",
            node.line,
            node.column,
        )

    def _visible_for(
        self, node: Node
    ) -> Optional[frozenset[str]]:
        """todo-79: bare-name visibility set for *node*'s home file.

        ``None`` when gating is disabled (no module table) or the file has
        no recorded surface -- both keep the legacy permissive behavior.
        """
        if self._module_visible is None:
            return None
        home = getattr(node, "source_module", None)
        if home is None:
            return None
        return self._module_visible.get(home)

    def _reject_hidden(
        self, name: str, kind: str, node: Node
    ) -> bool:
        """todo-79: True (and report) when *name* is not visible here.

        Used at every bare-name resolution site for functions, constants,
        statics, types and enum constructors: an item that only reached the
        program as another module's compile dependency must not be usable
        from a file that never declared or imported it.
        """
        visible = self.current_visible
        if visible is None or name in visible:
            return False
        self._record_error(
            f"{kind} '{name}' belongs to another module and is not "
            "visible here; export it with 'pub' and import its module "
            "with 'use' from this file",
            node.line,
            node.column,
        )
        return True

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
