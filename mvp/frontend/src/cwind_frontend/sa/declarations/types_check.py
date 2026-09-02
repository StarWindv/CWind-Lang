"""Declaration mixin: type checking, alias expansion, bounds and requirement helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from dataclasses import fields as _fields

import copy

from .defs import _EXTERN_SCALAR_TYPES

from ..builtin_methods import (
    BUILTIN_TRAIT_ARITY,
    BUILTIN_TRAITS,
)

from ..types import (
    BUILTIN_TYPES,
    _BUILTIN_GENERIC_ARITY,
    _bare_type,
    _base,
    _compatible,
    _split_args,
    _split_fn_sig,
    _split_ref_prefix,
    _strip_builtin_ns,
    _type_str,
    split_array_type,
)

from ...ast_components.ast import (
    Node,
    Type,
    TypeParam,
)

if TYPE_CHECKING:
    from ..analyzer import _Analyzer


class DeclTypes:

    def _check_type_param_bounds(
        self: "_Analyzer", params: list[TypeParam]
    ) -> None:
        """Validate generic-parameter bounds exist and take the right arity."""
        for p in params:
            if p.bound is None:
                continue
            bound_name = p.bound.name
            if bound_name in BUILTIN_TRAITS:
                want = BUILTIN_TRAIT_ARITY.get(bound_name, 0)
                if len(p.bound.args) != want:
                    self._record_error(
                        f"bound '{bound_name}' expects {want} type "
                        f"argument(s), got {len(p.bound.args)}",
                        p.bound.line,
                        p.bound.column,
                    )
            else:
                trait = self.traits.get(bound_name)
                if trait is None:
                    self._record_error(
                        f"unknown bound '{bound_name}'",
                        p.bound.line,
                        p.bound.column,
                    )
                elif len(p.bound.args) != len(trait.params):
                    self._fill_generic_defaults(p.bound, trait.params)
                if (
                    trait is not None
                    and len(p.bound.args) != len(trait.params)
                ):
                    self._record_error(
                        f"bound '{bound_name}' expects "
                        f"{len(trait.params)} type argument(s), "
                        f"got {len(p.bound.args)}",
                        p.bound.line,
                        p.bound.column,
                    )
            for arg in p.bound.args:
                self._check_type(arg, p)
            # todo-164: associated-type bindings in bound position
            # (``T: Iterator<Item = Int32>``) must name assoc types the
            # trait declares; their values are type references.
            self._check_bound_bindings(p.bound, p)
            if p.default is not None:
                # todo-164: a parameter default is a type reference in the
                # declaring scope.
                self._check_type(p.default, p)

    def _check_bound_bindings(
        self: "_Analyzer", bound: Type, ctx: Node
    ) -> None:
        """todo-164: validate ``Trait<Assoc = Type>`` bindings on a bound.

        Each binding name must be an associated type of the bound trait;
        each binding value is type-checked in the enclosing scope.
        """
        if not bound.bindings:
            return
        trait = self.traits.get(bound.name)
        known: set[str] = (
            set(trait.assoc_types) if trait is not None else set()
        )
        for b in bound.bindings:
            if trait is None:
                # The unknown-bound error was already reported.
                continue
            if b.name not in known:
                self._record_error(
                    f"trait '{bound.name}' has no associated type "
                    f"'{b.name}'",
                    b.line,
                    b.column,
                )
            self._check_type(b.type, ctx)

    def _fill_generic_defaults(
        self: "_Analyzer", type_: Type, params: list[TypeParam]
    ) -> None:
        """todo-164: append default type arguments for trailing omitted
        generic arguments (Rust generic-parameter defaults).

        ``Box<T = Int32>`` used as bare ``Box`` or ``Box<T>``
        materializes ``Box<Int32>`` / keeps ``Box<T>`` in place.  The
        caller must have already rejected excess arguments.  Defaults are
        deep-copied per use so two uses never share one Type node, and
        the filling runs on the *canonical* declaration's parameters.
        """
        if not params or len(type_.args) >= len(params):
            return
        for p in params[len(type_.args):]:
            if p.default is None:
                return
            arg = copy.deepcopy(p.default)
            arg.line = type_.line
            arg.column = type_.column
            # The copy carries the original node's typed-AST id (and its
            # annotations); reset them and renumber from the synthetic
            # pool, or the backend sees duplicate node ids.
            self._reset_ids_for_copy(arg)
            self._assign_synthetic_ids(arg)
            type_.args.append(arg)

    def _reset_ids_for_copy(self: "_Analyzer", node: Node) -> None:
        """Blank the typed-AST id/annotation of a copied subtree."""
        node._typed_id = None
        node._typed_ann = {}
        for f in _fields(node):
            if f.name in ("line", "column"):
                continue
            value = getattr(node, f.name)
            if isinstance(value, Node):
                self._reset_ids_for_copy(value)
            elif isinstance(value, list):
                for v in value:
                    if isinstance(v, Node):
                        self._reset_ids_for_copy(v)

    def _check_type(self: "_Analyzer", type_: Type, ctx: Node) -> None:
        # todo-154: ``std::builtins::X`` 是 pass 0 写下的规范 FQN 存储形,
        # 等价于裸名内置类型 —— 查表一律按剥前缀后的名字, 且不算路径。
        is_fqn = type_.name.startswith("std::builtins::")
        bare_name = _strip_builtin_ns(type_.name) or type_.name
        is_path = "::" in type_.name and not is_fqn
        if is_path:
            before = type_.name
            if not self._resolve_qualified_type_name(type_):
                return
            if type_.name != before:
                is_path = False
                is_fqn = type_.name.startswith("std::builtins::")
                bare_name = _strip_builtin_ns(type_.name) or type_.name
        if (type_.name.startswith("fn(")
                or type_.name.startswith("*const ")
                or type_.name.startswith("*mut ")):
            # 函数指针 / 原始指针: 名字已扁平化, 只需递归登记
            self._ann_type(type_, _type_str(type_))
            return
        if type_.name.startswith("["):
            # 定长数组 (todo-60): `[T; N]`, 元素必须是定宽标量,
            # N >= 1; 内联值语义存储 (对应 C char[N] / Rust [u8; N])
            parsed = split_array_type(_type_str(type_))
            if parsed is None:
                self._record_error(
                    f"malformed array type '{type_.name}' "
                    "(expected '[T; N]')",
                    type_.line,
                    type_.column,
                )
            else:
                elem, n = parsed
                if n < 1:
                    self._record_error(
                        f"array length must be at least 1, got {n}",
                        type_.line,
                        type_.column,
                    )
                # bug-29: 元素类型先展开别名 (std::prelude 的 f64/...)
                elif self._expand_type(elem) not in _EXTERN_SCALAR_TYPES:
                    self._record_error(
                        f"array element type '{elem}' is not a fixed-width "
                        "scalar",
                        type_.line,
                        type_.column,
                    )
            # bug-35: 注解写元素别名展开后的完整类型名, 后端
            # cg_array_info 读到的就是规范标量名
            self._ann_type(type_, self._expand_type(_type_str(type_)))
            return
        if (not is_path
                and bare_name not in BUILTIN_TYPES
                and bare_name not in self.defined
                and bare_name not in self.type_aliases
                and bare_name not in self._cwind_builtins
                and type_.name != "Self"):
            # point at the type name itself, not at the enclosing statement
            self._record_error(f"unknown type '{bare_name}'", type_.line, type_.column)
        elif (not is_path
                and bare_name not in BUILTIN_TYPES
                and type_.name != "Self"
                and bare_name not in self.active_generics
                and bare_name not in self.type_aliases
                and bare_name not in self._cwind_builtins
                and (
                    bare_name in self.structs
                    or bare_name in self.enums
                    or bare_name in self.type_aliases
                    or bare_name in self.groups
                )
                and not getattr(type_, "_fqn_path", False)
                and self._reject_hidden(bare_name, "type", type_)):
            # todo-79: the type exists but was never declared/imported here.
            return
        arity = _BUILTIN_GENERIC_ARITY.get(bare_name)
        if not is_path and arity is not None and len(type_.args) != arity:
            self._record_error(
                f"type '{bare_name}' expects {arity} generic argument(s), "
                f"got {len(type_.args)}",
                type_.line,
                type_.column,
            )
        struct = self.structs.get(bare_name)
        if struct is not None and len(type_.args) != len(struct.params):
            # todo-164: trailing omitted arguments fall back to the
            # declaration's parameter defaults before the arity verdict.
            self._fill_generic_defaults(type_, struct.params)
        if struct is not None and len(type_.args) != len(struct.params):
            self._record_error(
                f"type '{bare_name}' expects {len(struct.params)} generic argument(s), "
                f"got {len(type_.args)}",
                type_.line,
                type_.column,
            )
            struct = None  # avoid cascading bound checks on a bad arity
        if struct is not None:
            for p, arg in zip(struct.params, type_.args):
                if p.bound is not None and p.bound.name not in BUILTIN_TRAITS:
                    trait_name = p.bound.name
                    if not self._satisfies_bound(arg.name, trait_name):
                        self._record_error(
                            f"type '{_bare_type(arg.name)}' does not satisfy bound '{trait_name}'",
                            arg.line,
                            arg.column,
                        )
        alias = self.type_aliases.get(bare_name)
        if alias is not None and len(type_.args) != len(alias.params):
            # todo-154: 裸泛型别名 (``let v: Vec``) 与实参不符同样拒绝
            # —— pass 0 对实参缺失不做猜测, 类型位必须给全。
            self._record_error(
                f"type '{bare_name}' expects {len(alias.params)} generic argument(s), "
                f"got {len(type_.args)}",
                type_.line,
                type_.column,
            )
        for arg in type_.args:
            self._check_type(arg, ctx)
        self._ann_type(type_, self._expand_type(_type_str(type_)))

    def _satisfies_bound(self: "_Analyzer", type_name: str, trait_name: str) -> bool:
        """Does ``type_name`` implement ``trait_name``, counting supertraits?

        todo-156: an impl of a trait that inherits ``trait_name`` satisfies the
        bound.  The existing ``self.impls`` table stores *bare* trait names
        (no args), so this matches on names — the same fidelity as the prior
        ``trait_name in self.impls[...]`` check it replaces.  A ``stack`` guards
        against a malformed trait-inheritance cycle so a bad declaration cannot
        hang the analyzer.

        todo-156 (negative): an ``impl !trait_name for type_name`` vetoes
        satisfaction outright — and, because pass 1.5 flags a positive impl of
        the same (struct, trait) as a conflict, the two can never both exist.
        """
        if type_name is None or trait_name is None:
            return False
        type_name = _strip_builtin_ns(type_name) or type_name
        trait_name = _strip_builtin_ns(trait_name) or trait_name
        if (type_name, trait_name) in self.negative_impls:
            return False
        return self._impls_closure_has(type_name, trait_name, frozenset())

    def _impls_closure_has(
        self: "_Analyzer",
        type_name: str,
        trait_name: str,
        stack: frozenset[tuple[str, str]],
    ) -> bool:
        for impl_trait in self.impls.get(type_name, []):
            if impl_trait == trait_name:
                return True
            trait_decl = self.traits.get(impl_trait)
            if trait_decl is None:
                continue
            for st in trait_decl.supertraits:
                key = (type_name, st.name)
                if st.name == trait_name:
                    return True
                if key not in stack:
                    # recurse: the supertrait itself may inherit trait_name,
                    # reached through impl_trait's chain (guard against cycles).
                    if self._super_closure_has(st.name, trait_name,
                                               stack | {key}):
                        return True
        return False

    def _super_closure_has(
        self: "_Analyzer",
        trait: str,
        trait_name: str,
        stack: frozenset[tuple[str, str]],
    ) -> bool:
        """Is ``trait_name`` reachable (transitively) through ``trait``'s
        supertrait edges?"""
        if trait == trait_name:
            return True
        trait_decl = self.traits.get(trait)
        if trait_decl is None:
            return False
        for st in trait_decl.supertraits:
            if st.name == trait_name:
                return True
            key = (trait, st.name)
            if key not in stack and self._super_closure_has(
                st.name, trait_name, stack | {key}
            ):
                return True
        return False

    def _require(self: "_Analyzer", name: str, kinds: set[str], ctx: Node, what: str) -> None:
        sym = self.symbols.get(name)
        if sym is None:
            self._record_error(f"unknown {what} '{name}'", ctx.line, ctx.column)
        elif sym.kind not in kinds:
            self._record_error(
                f"'{name}' is a {sym.kind}, not a {what}",
                ctx.line,
                ctx.column,
            )

    def _require_trait(self: "_Analyzer", name: str, ctx: Node) -> None:
        if name in BUILTIN_TRAITS:
            return
        self._require(name, {"trait"}, ctx, "trait")

    def _require_type_target(self: "_Analyzer", name: str, ctx: Node, what: str) -> None:
        if name in BUILTIN_TYPES:
            return
        if name in self.type_aliases:
            return
        if name in self._cwind_builtins:
            return
        self._require(name, {"struct", "enum"}, ctx, what)

    def _resolve_impl_path_name(
        self: "_Analyzer", name: str, ctx: Node
    ) -> str:
        """bug-42: normalize a module-qualified impl path to the flattened
        bare item name.

        ``impl num_wrapping::Wrapping<i32> for i32`` resolves the head
        segment against the module aliases registered by ``use``
        declarations (todo-69) and keeps only the final segment -- the name
        the flattened single-program model knows the item by (mirrors
        todo-81's ``module::Enum::Variant`` normalization).  Paths whose
        head does not match a registered alias are returned unchanged so
        pass-2 diagnostics report them exactly once.
        """
        if "::" not in name:
            return name
        # todo-154: ``std::builtins::X`` is the canonical FQN storage form of
        # a builtin owner — it is *already resolved*, return the bare name
        # so impl/extra registries (keyed bare) never see the prefix.
        if name.startswith("std::builtins::"):
            return name[len("std::builtins::"):]
        parts = name.split("::")
        for i in range(len(parts) - 1):
            if parts[i] in self.modules:
                tail = parts[i + 1:]
                if len(tail) == 1:
                    return tail[0]
                # alias::module::item: modules do not nest in the flat
                # namespace; leave the path untouched for the caller to
                # report a precise error.
                return name
        return name

    def _expand_type(
        self: "_Analyzer",
        t: Optional[str],
        *,
        _in_args: bool = False,
        _deep: bool = False,
    ) -> Optional[str]:
        """Normalizing wrapper over :meth:`_expand_type_raw`.

        todo-154: the raw expansion may leave ``std::builtins::`` prefixes
        behind (FQN-storage alias RHS / already-canonical leaves); the
        *consumption* view is always bare — comparisons, method-lookup
        keys and the JSON boundary use the bare spelling, so the result
        is deeply normalized here (`_bare_type`).
        """
        if t is None:
            return None
        out = self._expand_type_raw(t, _in_args=_in_args, _deep=_deep)
        if out is None:
            return None
        return _bare_type(out)

    def _expand_type_raw(
        self: "_Analyzer",
        t: Optional[str],
        *,
        _in_args: bool = False,
        _deep: bool = False,
    ) -> Optional[str]:
        """Substitute a type alias's arguments into its right-hand side so
        method resolution sees the underlying type (e.g. ``DoubleMap<K, V>``
        expands to ``Map<K, V>``).  Fixed-length array types ``[T; N]``
        expand their element type the same way (bug-35: ``[u32; 624]`` ->
        ``[UInt32; 624]``).

        bug-48: expansion recurses into generic arguments (``Vector<f32>`` ->
        ``Vector<Float>``, ``Vector<Vec<u8>>`` -> ``Vector<Vector<UInt8>>``)
        and into raw-pointer pointees, so aliases nested inside containers no
        longer leak into compatibility checks or typed annotations.

        ``type X = ... where`` 精化别名在泛型实参位保留原名: 精化检查
        (``_refinement``) 按别名名查找谓词, 展开成基类型会丢失约束。
        ``_deep=True`` (兼容性比较) 连实参位的精化别名一并展开; 基位的
        精化别名两种模式下都照旧展开 (标量兼容性判断依赖它)。"""
        if t is None:
            return None
        # 防环 (todo-144, PROBLEMS-FINAL 第 3 条裁决第 4 点): 已是规范形的
        # 引用 (带 `::` 的全限定名不在扁平别名表里) 天然终止; 别名链再叠加
        # visited 集 —— todo-132 落地后内置类型终点变为 `std::builtins::*`,
        # 重导出洗白环 (A 洗白 B、B 洗白 A) 不得使展开器失控。
        seen: set[str] = set()
        for _ in range(16):  # guard against circular aliases
            ref, t = _split_ref_prefix(t)
            if t in seen:
                return (ref + t) if ref else t
            seen.add(t)
            if t.startswith("fn("):
                # bug-58: fn 签名段内的别名同样展开 (todo-54 回调签名与
                # 候选 fn 声明可以分别用别名/底层类型拼写), 名字整体
                # 扁平编码, 逐段展开后重建。
                params, ret = _split_fn_sig(t)
                expanded_params = [
                    self._expand_type(p, _in_args=_in_args, _deep=_deep)
                    or p
                    for p in params
                ]
                expanded_ret = (
                    self._expand_type(ret, _in_args=_in_args, _deep=_deep)
                    if ret is not None else None
                )
                out = "fn(" + ", ".join(expanded_params) + ")"
                if expanded_ret is not None and expanded_ret != "None":
                    out += " -> " + expanded_ret
                return (ref + out) if ref else out
            if t.startswith("["):
                # 定长数组: 元素别名展开后整体重建 (类型名扁平编码)
                parsed = split_array_type(t)
                if parsed is None:
                    return (ref + t) if ref else t
                elem, n = parsed
                expanded_elem = self._expand_type(
                    elem, _in_args=_in_args, _deep=_deep
                )
                if expanded_elem is None:
                    return (ref + t) if ref else t
                t = f"[{expanded_elem}; {n}]"
                return (ref + t) if ref else t
            if t.startswith("*const ") or t.startswith("*mut "):
                # 原始指针: 被指类型里的别名同样展开 (bug-29 语义泛化)
                prefix, _, pointee = t.partition(" ")
                expanded_pointee = self._expand_type(
                    pointee, _in_args=_in_args, _deep=_deep
                )
                if expanded_pointee is None:
                    return (ref + t) if ref else t
                t = f"{prefix} {expanded_pointee}"
                return (ref + t) if ref else t
            base = _base(t)
            # bug-51: 当前作用域的泛型形参不是类型别名 —— 即使外部有同名
            # struct/typedef (如用户 struct T 与 std 的 Option<T>), 展开器
            # 也不得把形参替换成别名右侧值
            if base in self.active_generics:
                return (ref + t) if ref else t
            args = _split_args(t)
            alias = self.type_aliases.get(base)
            if (
                alias is not None
                and not (_in_args and not _deep and alias.where is not None)
            ):
                if len(args) == len(alias.params):
                    subst = dict(zip([p.name for p in alias.params], args))
                    t = _type_str(alias.base, subst)
                    if ref:
                        t = ref + t
                    continue
                # todo-154: 无实参泛型别名 (``Vec::new()`` 的 ``Vec``) —
                # 没有可供替换的实参, 以别名自身形参做恒等展开得到 owner
                # 基名 (``Vec`` -> RHS ``std::builtins::Vector<T>`` 的基名
                # ``Vector``)。带实参但个数不符是写错, 留给 arity 检查。
                if not args and alias.params:
                    subst = {p.name: p.name for p in alias.params}
                    t = _type_str(alias.base, subst)
                    if ref:
                        t = ref + t
                    continue
            if not args:
                return (ref + t) if ref else t
            # 基名不是别名: 仍要展开实参里的别名 (bug-48)
            expanded_args = [
                self._expand_type(a, _in_args=True, _deep=_deep)
                for a in args
            ]
            if any(a is None for a in expanded_args):
                return (ref + t) if ref else t
            t = f"{base}<{', '.join(expanded_args)}>"
            return (ref + t) if ref else t
        return t

    def _compat_types(self: "_Analyzer", a: Optional[str], b: Optional[str]) -> bool:
        """Compatibility check with type aliases expanded on both sides.

        ``_deep=True``: 泛型实参位的精化别名也展开 (Vector<Test1> 与
        Vector<Int8> 是同一类型), 字面量绑定后的比较才不会假阳性。"""
        return _compatible(
            self._expand_type(a, _deep=True),
            self._expand_type(b, _deep=True),
        )

    def _fmt_type(self: "_Analyzer", t: Optional[str]) -> str:
        """A type for error messages; shows the expanded form for aliases."""
        if t is None:
            return "unknown"
        expanded = self._expand_type(t)
        # todo-154: FQN 只存在于内部存储, 错误消息一律输出裸名
        bare = _bare_type(t) or t
        return bare if expanded == t else f"{bare} ({expanded})"
