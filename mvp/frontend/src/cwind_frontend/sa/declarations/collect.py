"""Declaration mixin: pass-1 collection, indexing and symbol registration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from dataclasses import fields as _fields

from .defs import _decl_kind_name

from ..symbols import (
    MethodBinding,
    Symbol,
)

from ..types import (
    BUILTIN_TYPES,
    _trait_bare,
    _type_str,
)

from ...ast_components.ast import (
    ConstDecl,
    EnumDecl,
    ExternBlock,
    ExtraDecl,
    FnDecl,
    GroupDecl,
    ImplDecl,
    Node,
    StructDecl,
    TraitDecl,
    Type,
    TypeDecl,
)

if TYPE_CHECKING:
    from ..analyzer import _Analyzer


class DeclCollect:

    def _collect(self: "_Analyzer", item: Node) -> None:
        inline_ns = getattr(item, "_inline_ns", None)
        if isinstance(item, FnDecl):
            self._module_item_owners[id(item)] = getattr(
                item, "source_module", None
            )
        self._index(item)
        if inline_ns is not None:
            # todo-107 (namespace model): items inside inline ``mod {}``
            # bodies never join the flat namespace — they are reachable
            # only through their owning namespace, so no flat symbol and
            # no duplicate-definition check against other namespaces.
            return
        if isinstance(item, ExternBlock):
            if item.abi == "CWind":
                # todo-132: ``extern "CWind"`` blocks declare compiler
                # intrinsics.  Type declarations are registered by
                # ``_index`` into ``_cwind_builtins``; method declarations
                # (with ``cwind_owner``) are NOT flat symbols — they are
                # registered as method bindings by ``_index``.  Plain
                # module functions from ``extern "CWind"`` (e.g. ``print``)
                # still enter the flat symbol table.
                for fn in item.fns:
                    if fn.cwind_owner is None:
                        self._register_decl_symbol(fn, "fn", fn.name)
                for st in item.statics:
                    self._register_decl_symbol(st, "static", st.name)
                return
            for fn in item.fns:
                self._register_decl_symbol(fn, "fn", fn.name)
            # todo-56: extern 静态变量与函数同表登记 (kind = "static")
            for st in item.statics:
                self._register_decl_symbol(st, "static", st.name)
            return
        kind_name = _decl_kind_name(item)
        if kind_name is None:
            return
        kind, name = kind_name
        self._register_decl_symbol(item, kind, name)

    def _register_decl_symbol(
        self: "_Analyzer", item: Node, kind: str, name: str
    ) -> None:
        if name in self.defined:
            prev = self.symbols[name]
            self._record_error(
                f"duplicate definition of '{name}' "
                f"(first defined at line {prev.line})",
                item.line,
                item.column,
            )
            return
        if name in BUILTIN_TYPES and not isinstance(item, TraitDecl):
            # trait 与内置类型不同命名空间 (如用户可定义 trait Iterator);
            # struct/enum/type 仍禁止重定义内置类型
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

    def _declaration_type_names(self: "_Analyzer", item: Node) -> list[str]:
        """todo-144: the type names a declaration defines (for def paths).

        todo-146: 函数/常量也入表 —— prelude 内联进 typed JSON 的
        fn/const 符号同样需要 ``def`` 溯源 (类型查找不会查询这些键,
        同名冲突已被 duplicate-definition 检查挡下)。
        """
        if isinstance(
            item, (StructDecl, EnumDecl, TypeDecl, TraitDecl, FnDecl, ConstDecl)
        ):
            return [item.name]
        return []

    def _index(self: "_Analyzer", item: Node) -> None:
        # todo-144: 定义位置的规范模块路径 (typed-AST 类型对象的 "def")
        def_path = getattr(item, "source_module_path", None)
        if def_path:
            for name in self._declaration_type_names(item):
                self._def_paths.setdefault(name, "::".join(def_path))
        if isinstance(item, StructDecl):
            self.structs[item.name] = item
        elif isinstance(item, EnumDecl):
            self.enums[item.name] = item
        elif isinstance(item, TypeDecl):
            self.type_aliases[item.name] = item
        elif isinstance(item, GroupDecl):
            self.groups[item.name] = item
        elif isinstance(item, TraitDecl):
            self.traits[item.name] = item
        elif isinstance(item, FnDecl):
            self.functions[item.name] = item
        elif isinstance(item, ExternBlock):
            for fn in item.fns:
                # todo-132: extern "CWind" method declarations (``fn Type<T>::
                # method``) are not flat module functions — they bind to
                # their owner type below.
                if fn.cwind_owner is not None:
                    continue
                self.functions[fn.name] = fn
            # todo-56: extern 静态变量按名索引
            for st in item.statics:
                self.extern_statics[st.name] = st
            # todo-132: register built-in type declarations from extern "CWind"
            # blocks so the compiler's built-in type registry is extended.
            if item.abi == "CWind":
                for td in item.types:
                    self._cwind_builtins[td.name] = td
                # Register CWind methods on their owner types so method
                # resolution can find them.
                for fn in item.fns:
                    if fn.cwind_owner is None:
                        continue
                    owner = fn.cwind_owner.name
                    # Use a bare owner type (without generic args) so that
                    # ``Self`` in signatures resolves to the bare type name
                    # and generic-parameter substitution is driven by the
                    # call-site receiver / expected type through
                    # ``binding.owner_params``.
                    owner_type = Type(
                        fn.cwind_owner.line,
                        fn.cwind_owner.column,
                        owner,
                    )
                    binding = MethodBinding(
                        self._next_binding_id,
                        tuple(
                            a.name for a in fn.cwind_owner.args
                        ),
                        owner_type,
                        fn,
                        item,
                        None,
                    )
                    self._next_binding_id += 1
                    self.methods.setdefault(owner, []).append(binding)
                    self._binding_order.append((owner, binding))
        elif isinstance(item, ConstDecl):
            self.consts[item.name] = item
        elif isinstance(item, ImplDecl):
            generic = tuple(p.name for p in item.params)
            # bug-42: normalize module-qualified trait/impl-target paths
            # (``num_wrapping::Wrapping`` -> ``Wrapping``) before indexing so
            # impl tables, method bindings and duplicate detection all see
            # the flattened bare name.
            item.trait.name = self._resolve_impl_path_name(
                item.trait.name, item
            )
            item.struct.name = self._resolve_impl_path_name(
                item.struct.name, item
            )
            # todo-154: trait 引用是 FQN 存储形, 注册表/绑定按裸名键
            # (取末段), 消费点比较同形。
            trait_bare = _trait_bare(item.trait.name)
            if item.negative:
                # todo-156: a negative impl records a (struct, trait) veto and
                # carries no methods / bindings / Into seeding.  The pass-1.5
                # duplicate check already flags a positive impl of the same
                # (struct, trait) (conflicting-impl coherence).
                self.negative_impls.add((item.struct.name, trait_bare))
                return
            self.impls.setdefault(item.struct.name, []).append(trait_bare)
            if item.assoc_types:
                # todo-164: assoc-type bindings an impl provides, queried
                # at call sites validating ``T: Trait<Item = X>`` bounds.
                self.impl_assoc_types.setdefault(
                    (item.struct.name, trait_bare), {}
                ).update(
                    {a.name: a.type for a in item.assoc_types}
                )
            if trait_bare == "Into" and len(item.trait.args) == 1:
                self.into_impls.add(
                    (_type_str(item.struct), _type_str(item.trait.args[0]))
                )
            self._substitute_impl_assoc_types(item)
            for m in item.methods:
                binding = MethodBinding(
                    self._next_binding_id,
                    generic,
                    item.struct,
                    m,
                    item,
                    trait_bare,
                )
                self.methods.setdefault(item.struct.name, []).append(binding)
                self._next_binding_id += 1
                self._binding_order.append((item.struct.name, binding))
        elif isinstance(item, ExtraDecl):
            # todo-147: parser 搬移的裸"形参"若全部是可解析的具体类型
            # 且数量与结构体形参一致, 这是具体特化 (``extra Cell<Int>``,
            # Rust ``impl Cell<i32>``) —— 还原 params 为空, 走非泛型
            # 绑定; 与结构体形参同名的裸名仍是泛型引用 (tie-break:
            # 泛型语义优先), 未知名字保持搬移 (交由失配检查报错)。
            if getattr(item, "_params_moved_from_args", False) and item.params:
                struct_decl = self.structs.get(item.struct.name)
                if (
                    struct_decl is not None
                    and len(item.params) == len(struct_decl.params)
                ):
                    known = (
                        self.structs.keys()
                        | self.enums.keys()
                        | self.type_aliases.keys()
                        | BUILTIN_TYPES
                    )
                    if all(p.name in known for p in item.params):
                        item.params = []
            generic = tuple(p.name for p in item.params)
            # bug-42: same normalization for the extra target type
            item.struct.name = self._resolve_impl_path_name(
                item.struct.name, item
            )
            # todo-122: associated consts indexed by owner struct name
            for c in item.consts:
                self.extra_consts.setdefault(item.struct.name, []).append(c)
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

    def _substitute_impl_assoc_types(
        self: "_Analyzer", item: ImplDecl
    ) -> None:
        """把 impl 里 ``Self::Item`` 类型节点原地替换成关联类型绑定
        (如 Int32), 让签名校验与后端代码生成都看到具体类型。"""
        assoc: dict[str, Type] = {
            a.name: a.type for a in item.assoc_types
        }
        if not assoc:
            return
        for m in item.methods:
            self._substitute_assoc_type_nodes(m, assoc)

    def _substitute_assoc_type_nodes(
        self: "_Analyzer", node: Node, assoc: dict[str, Type]
    ) -> None:
        if isinstance(node, Type):
            if node.name == "Self::Item" and "Item" in assoc:
                src = assoc["Item"]
                node.name = src.name
                node.args = list(src.args)
            else:
                for a in node.args:
                    self._substitute_assoc_type_nodes(a, assoc)
            return
        for f in _fields(node):
            value = getattr(node, f.name)
            if isinstance(value, Node):
                self._substitute_assoc_type_nodes(value, assoc)
            elif isinstance(value, list):
                for x in value:
                    if isinstance(x, Node):
                        self._substitute_assoc_type_nodes(x, assoc)
