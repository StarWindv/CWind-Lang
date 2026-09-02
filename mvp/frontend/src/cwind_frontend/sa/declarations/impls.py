"""Declaration mixin: pass-2 declaration dispatch and impl/extra conformance checks."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from ..builtin_methods import (
    BUILTIN_TRAIT_METHOD_NAMES,
    BUILTIN_TRAIT_METHODS,
    BUILTIN_TRAITS,
    BUILTIN_TYPE_TRAITS,
    MethodSpec,
)

from ..const_fold import _const_number

from ..types import (
    BUILTIN_TYPES,
    _replace_self,
    _subst_type_str,
    _type_str,
)

from ...ast_components.ast import (
    ConstDecl,
    EnumDecl,
    ExternBlock,
    ExtraDecl,
    FnDecl,
    GroupApply,
    GroupDecl,
    ImplDecl,
    Node,
    StructDecl,
    TraitDecl,
    TypeDecl,
)

if TYPE_CHECKING:
    from ..analyzer import _Analyzer


class DeclImpls:

    def _check(self: "_Analyzer", item: Node) -> None:
        if isinstance(item, TypeDecl):
            generic = {p.name for p in item.params}
            self.defined |= generic
            saved_generics = self._push_generics(generic)
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
                self._pop_generics(saved_generics)
                self.defined -= generic
        elif isinstance(item, StructDecl):
            generic = {p.name for p in item.params}
            self.defined |= generic
            saved_generics = self._push_generics(generic)
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
                self._pop_generics(saved_generics)
                self.defined -= generic
        elif isinstance(item, EnumDecl):
            generic = {p.name for p in item.params}
            self.defined |= generic
            saved_generics = self._push_generics(generic)
            try:
                self._annotate_type_params(item.params, frozenset(generic))
                seen: set[str] = set()
                for v in item.variants:
                    if v.name in seen:
                        self._record_error(
                            f"duplicate variant '{v.name}' in enum '{item.name}'",
                            v.line,
                            v.column,
                        )
                    seen.add(v.name)
                    if v.value is not None and v.fields:
                        self._record_error(
                            f"variant '{v.name}' cannot have both an explicit "
                            "value and a payload",
                            v.line,
                            v.column,
                        )
                    for f in v.fields:
                        self._check_type(f, v)
                        self._annotate_type_node(f, frozenset(generic))
            finally:
                self._pop_generics(saved_generics)
                self.defined -= generic
        elif isinstance(item, ConstDecl):
            self._check_type(item.type, item)
            self._annotate_type_node(item.type)
            self._ann_type(item, _type_str(item.type))
            # todo-90: const 值可能构造结构体/访问静态字段, 需要模块上下文
            saved_module = self.current_module
            self.current_module = getattr(item, "source_module", None)
            # todo-79: 裸名可见性同样按定义文件门禁
            saved_visible = self.current_visible
            self.current_visible = self._visible_for(item)
            try:
                value = self._check_expr(item.value, _type_str(item.type))
                if not self._compat_types(_type_str(item.type), value):
                    self._record_error(
                        f"cannot initialize {self._fmt_type(_type_str(item.type))} "
                        f"with {self._fmt_type(value)}",
                        item.line,
                        item.column,
                    )
                folded = _const_number(
                    item.value, self.const_values, self.const_floats
                )
                if folded is not None:
                    if isinstance(folded, float):
                        self.const_floats[item.name] = folded
                    else:
                        self.const_values[item.name] = folded
                    item._typed_ann["folded_value"] = folded
                self._check_const_div_zero(item.value)
                self._check_literal_range(_type_str(item.type), item.value)
                self._check_refined_value(_type_str(item.type), item.value)
            finally:
                self.current_module = saved_module
                self.current_visible = saved_visible
        elif isinstance(item, TraitDecl):
            generic = {p.name for p in item.params}
            self.defined |= generic
            saved_generics = self._push_generics(generic)
            self._push_into_bounds(item.params)
            try:
                self._annotate_type_params(item.params, frozenset(generic))
                seen_assoc: set[str] = set()
                for an in item.assoc_types:
                    if an in seen_assoc:
                        self._record_error(
                            f"duplicate associated type '{an}' in "
                            f"trait '{item.name}'",
                            item.line,
                            item.column,
                        )
                    seen_assoc.add(an)
                # todo-156: supertraits must be existing traits, with matching
                # generic arity, and must not trivially self-inherit.
                for st in item.supertraits:
                    if st.name == item.name:
                        self._record_error(
                            f"trait '{item.name}' cannot inherit itself",
                            st.line,
                            st.column,
                        )
                        continue
                    super_decl = self.traits.get(st.name)
                    if super_decl is None:
                        self._record_error(
                            f"unknown trait '{st.name}' in supertrait list of "
                            f"'{item.name}'",
                            st.line,
                            st.column,
                        )
                    elif len(st.args) != len(super_decl.params):
                        self._record_error(
                            f"supertrait '{st.name}' expects "
                            f"{len(super_decl.params)} generic argument(s), "
                            f"got {len(st.args)}",
                            st.line,
                            st.column,
                        )
                    else:
                        for sa in st.args:
                            self._check_type(sa, item)
                for m in item.methods:
                    method_generic = {p.name for p in m.type_params}
                    self.defined |= method_generic
                    try:
                        self._check_fn_types(m)
                    finally:
                        self.defined -= method_generic
                    if m.body is not None:
                        self._push_into_bounds(m.type_params)
                        self._check_fn(
                            m,
                            owner=None,
                            generic=frozenset(generic | method_generic),
                        )
                        self._pop_into_bounds()
            finally:
                self._pop_into_bounds()
                self._pop_generics(saved_generics)
                self.defined -= generic
        elif isinstance(item, FnDecl):
            generic = {p.name for p in item.type_params}
            self.defined |= generic
            try:
                self._check_fn_types(item)
            finally:
                self.defined -= generic
            self._check_main_signature(item)
        elif isinstance(item, ExternBlock):
            if item.abi == "CWind":
                # todo-132: ``extern "CWind"`` is the compiler-intrinsic ABI;
                # its type declarations are already registered and its fn
                # declarations are built-in module functions, not C-ABI.
                # Skip C-ABI validation; type-check signatures normally.
                for fn in item.fns:
                    # Method declarations carry the owner's generic
                    # parameters (``Vector<T>::...``): push them so the
                    # signature's ``T`` resolves, and mark the receiver's
                    # ``self`` type as the owner.
                    if fn.cwind_owner is not None:
                        owner_generic = frozenset(
                            a.name for a in fn.cwind_owner.args
                        )
                        saved = self._push_generics(owner_generic)
                        try:
                            self._check_fn_types(fn)
                        finally:
                            self._pop_generics(saved)
                    else:
                        self._check_fn_types(fn)
                for st in item.statics:
                    self._check_extern_static(st)
            else:
                for fn in item.fns:
                    self._check_extern_fn(fn)
                for st in item.statics:
                    self._check_extern_static(st)
        elif isinstance(item, ImplDecl):
            # bug-42: resolve module-qualified paths (pass 1 already
            # normalized them when possible; a residual '::' means the head
            # never matched a registered module alias).
            item.trait.name = self._resolve_impl_path_name(
                item.trait.name, item
            )
            item.struct.name = self._resolve_impl_path_name(
                item.struct.name, item
            )
            if "::" in item.trait.name:
                self._record_error(
                    f"unknown module '{item.trait.name.split('::')[0]}' in "
                    f"trait path '{item.trait.name}'",
                    item.line,
                    item.column,
                )
            else:
                self._require_trait(item.trait.name, item)
            if "::" in item.struct.name:
                self._record_error(
                    f"unknown module '{item.struct.name.split('::')[0]}' in "
                    f"impl target type '{item.struct.name}'",
                    item.line,
                    item.column,
                )
            else:
                self._require_type_target(item.struct.name, item, "struct")
            # todo-156: a negative impl only records a (struct, trait) veto
            # (done in pass 1).  It has no body and no method/assoc conformance
            # to check; reject any that appear.
            if item.negative:
                if item.methods or item.assoc_types:
                    self._record_error(
                        f"negative impl of '{item.trait.name}' for "
                        f"'{item.struct.name}' must not declare methods or "
                        f"associated types",
                        item.line,
                        item.column,
                    )
                return
            # bug-31: an instantiation a built-in type already ships
            # cannot be implemented again (Rust E0119 for built-ins).
            if item.trait.name in BUILTIN_TRAITS:
                self._reject_builtin_reimplementation(item)
            generic = {p.name for p in item.params}
            self.defined |= generic
            saved_generics = self._push_generics(generic)
            try:
                self._annotate_type_params(item.params, frozenset(generic))
                self._check_type(item.struct, item)
                self._annotate_type_node(item.struct, frozenset(generic))
                for arg in item.trait.args:
                    self._check_type(arg, item)
                    self._annotate_type_node(arg, frozenset(generic))
                if item.trait.name == "From":
                    self._check_from_impl(item)
                trait_decl = self.traits.get(item.trait.name)
                if trait_decl is not None:
                    required = set(trait_decl.assoc_types)
                    provided: set[str] = set()
                    for a in item.assoc_types:
                        if a.name not in required:
                            self._record_error(
                                f"trait '{item.trait.name}' has no "
                                f"associated type '{a.name}'",
                                a.line,
                                a.column,
                            )
                        if a.name in provided:
                            self._record_error(
                                f"duplicate associated type '{a.name}' "
                                f"in impl of '{item.trait.name}'",
                                a.line,
                                a.column,
                            )
                        provided.add(a.name)
                        self._check_type(a.type, a)
                        self._annotate_type_node(
                            a.type, frozenset(generic)
                        )
                        # todo-164: the impl's binding must satisfy the
                        # trait declaration's ``type Item: Bound`` bound.
                        # A generic binding value (the impl's own parameter
                        # or a trait argument) defers the check, exactly
                        # like Rust's pending-obligation handling.
                        decl = next(
                            (
                                d
                                for d in trait_decl.assoc_type_decls
                                if d.name == a.name
                            ),
                            None,
                        )
                        binding_name = a.type.name
                        deferred = (
                            binding_name in generic
                            or binding_name in {p.name for p in item.params}
                            or binding_name
                            in {x.name for x in item.trait.args}
                        )
                        if (
                            decl is not None
                            and decl.bound is not None
                            and not deferred
                            and not self._satisfies_bound(
                                binding_name, decl.bound.name
                            )
                        ):
                            self._record_error(
                                f"associated type '{a.name}' of impl "
                                f"'{item.trait.name}' does not satisfy "
                                f"bound '{decl.bound.name}'",
                                a.line,
                                a.column,
                            )
                    for missing in sorted(required - provided):
                        self._record_error(
                            f"impl of '{item.trait.name}' does not provide "
                            f"associated type '{missing}'",
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
                self._pop_generics(saved_generics)
                self.defined -= generic
            trait_decl = self.traits.get(item.trait.name)
            if item.trait.name in BUILTIN_TRAITS:
                self._check_builtin_impl_conformance(item)
            elif trait_decl is not None:
                self._check_impl_conformance(item, trait_decl)
        elif isinstance(item, ExtraDecl):
            generic = {p.name for p in item.params}
            self.defined |= generic
            saved_generics = self._push_generics(generic)
            try:
                self._annotate_type_params(item.params, frozenset(generic))
                item.struct.name = self._resolve_impl_path_name(
                    item.struct.name, item
                )
                if "::" in item.struct.name:
                    self._record_error(
                        f"unknown module '{item.struct.name.split('::')[0]}' "
                        f"in extra target type '{item.struct.name}'",
                        item.line,
                        item.column,
                    )
                else:
                    self._require_type_target(item.struct.name, item, "struct")
                self._check_type(item.struct, item)
                self._annotate_type_node(item.struct, frozenset(generic))
                struct = self.structs.get(item.struct.name)
                if struct is not None:
                    struct_params = [p.name for p in struct.params]
                    extra_params = [p.name for p in item.params]
                    # todo-147: 允许具体类型特化 (Rust `impl Cell<i32>`)。
                    # 两条合法路径: 参数名与结构体形参逐位对齐
                    # (``extra<T> Cell<T>``, bug-49 归一化形), 或实参全部
                    # 具体化且数量一致 (``extra Cell<Int>``)。与形参同名
                    # 的裸实参是泛型引用, 不算具体化 (半泛型特化如
                    # ``Cell<Pair<T, Int>>`` 里的 T 会走 unknown-type 报错)。
                    spec_args = list(getattr(item.struct, "args", None) or [])
                    concrete = (
                        not extra_params
                        and len(spec_args) == len(struct_params)
                        and all(a.name not in struct_params for a in spec_args)
                    )
                    if extra_params != struct_params and not concrete:
                        self._record_error(
                            f"extra generic parameters {extra_params} do not match "
                            f"struct '{item.struct.name}' {struct_params}",
                            item.line,
                            item.column,
                        )
                    elif concrete:
                        # todo-147: 方法分派按 owner 基名 + 方法名 (首个
                        # 绑定胜出), 特化与其它 extra/impl 的同名方法会
                        # 静默遮蔽 —— v0 直接拒绝 (Rust 式优先级立案再说)。
                        for m in item.methods:
                            clash = next(
                                (
                                    b
                                    for b in self.methods.get(
                                        item.struct.name, []
                                    )
                                    if b.fn.name == m.name and b.decl is not item
                                ),
                                None,
                            )
                            if clash is not None:
                                self._record_error(
                                    f"method '{m.name}' for "
                                    f"'{item.struct.name}' is already provided "
                                    "by another extra/impl; specialized "
                                    "implementations must not shadow it",
                                    m.line,
                                    m.column,
                                )
                for m in item.methods:
                    method_generic = {p.name for p in m.type_params}
                    self.defined |= method_generic
                    try:
                        self._check_fn_types(m)
                    finally:
                        self.defined -= method_generic
                # todo-122: associated constants.  Same value checks as a
                # top-level const, but they stay scoped to the owner type
                # (never entered into self.consts / const_values, so a same-
                # named top-level const cannot collide with them).
                seen_const: set[str] = set()
                for c in item.consts:
                    if c.name in seen_const:
                        self._record_error(
                            f"duplicate associated const '{c.name}' in extra "
                            f"of '{item.struct.name}'",
                            c.line,
                            c.column,
                        )
                    seen_const.add(c.name)
                    self._check_type(c.type, c)
                    self._annotate_type_node(c.type, frozenset(generic))
                    self._ann_type(c, _type_str(c.type))
                    saved_module = self.current_module
                    self.current_module = getattr(c, "source_module", None)
                    saved_visible = self.current_visible
                    self.current_visible = self._visible_for(c)
                    try:
                        value = self._check_expr(c.value, _type_str(c.type))
                        if not self._compat_types(_type_str(c.type), value):
                            self._record_error(
                                f"cannot initialize {self._fmt_type(_type_str(c.type))} "
                                f"with {self._fmt_type(value)}",
                                c.line,
                                c.column,
                            )
                        self._check_const_div_zero(c.value)
                        self._check_literal_range(_type_str(c.type), c.value)
                        self._check_refined_value(_type_str(c.type), c.value)
                    finally:
                        self.current_module = saved_module
                        self.current_visible = saved_visible
            finally:
                self._pop_generics(saved_generics)
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
                    if struct is not None:
                        field = next(
                            (f for f in struct.fields if f.name == d.subject),
                            None,
                        )
                        if field is None:
                            self._record_error(
                                f"'{item.struct}' has no field '{d.subject}'",
                                d.line,
                                d.column,
                            )
                        else:
                            self._check_group_distribution_type(
                                d, _type_str(field.type), field
                            )
                elif item.struct is None and d.subject not in param_names:
                    self._record_error(
                        f"group '{item.name}' has no parameter '{d.subject}'",
                        d.line,
                        d.column,
                    )
                else:
                    param = next(
                        (p for p in item.params if p.name == d.subject),
                        None,
                    )
                    if param is not None and param.type is not None:
                        self._check_group_distribution_type(
                            d, _type_str(param.type), None
                        )
        elif isinstance(item, GroupApply):
            self._require(item.group, {"group"}, item, "group")
            self._require(item.struct, {"struct", "enum"}, item, "struct")
            group = self.groups.get(item.group)
            struct = self.structs.get(item.struct)
            if group is not None and struct is not None:
                if len(item.fields) != len(group.params):
                    self._record_error(
                        f"group '{item.group}' expects "
                        f"{len(group.params)} field(s), got "
                        f"{len(item.fields)}",
                        item.line,
                        item.column,
                    )
                else:
                    for i, fname in enumerate(item.fields):
                        field = next(
                            (f for f in struct.fields if f.name == fname),
                            None,
                        )
                        if field is None:
                            continue
                        param = group.params[i]
                        if param.type is not None:
                            expected = _type_str(param.type)
                            actual = _type_str(field.type)
                            if not self._group_types_match(expected, actual):
                                self._record_error(
                                    f"field '{fname}' of '{item.struct}' is "
                                    f"{self._fmt_type(actual)}, group "
                                    f"'{item.group}' parameter "
                                    f"'{param.name}' expects "
                                    f"{self._fmt_type(expected)}",
                                    item.line,
                                    item.column,
                                )
                for d in group.distributions:
                    if d.subject_self:
                        field = next(
                            (f for f in struct.fields if f.name == d.subject),
                            None,
                        )
                        if field is not None:
                            self._check_group_distribution_type(
                                d, _type_str(field.type), field
                            )
                    else:
                        param = next(
                            (p for p in group.params if p.name == d.subject),
                            None,
                        )
                        if param is not None and param.type is not None:
                            self._check_group_distribution_type(
                                d, _type_str(param.type), None
                            )
            if struct is not None:
                for fname in item.fields:
                    if not any(ff.name == fname for ff in struct.fields):
                        self._record_error(
                            f"'{item.struct}' has no field '{fname}'",
                            item.line,
                            item.column,
                        )

    def _check_from_impl(self: "_Analyzer", item: ImplDecl) -> None:
        """Register a user-declared ``impl From<X> for Y`` conversion."""
        if len(item.trait.args) != 1:
            self._record_error(
                "From requires one type argument (the conversion source)",
                item.line,
                item.column,
            )
            return
        source = _type_str(item.trait.args[0])
        target = _type_str(item.struct)
        if (source, target) in self.into_impls:
            self._record_error(
                f"duplicate 'into()' for {source} -> {target}: "
                f"both 'impl From<{source}> for {target}' and "
                f"'impl Into<{target}> for {source}' are present",
                item.line,
                item.column,
            )
        existing = self.conversions.get(source, [])
        if target in existing:
            self._record_error(
                f"duplicate conversion from {source} to {target} via "
                "'impl From'",
                item.line,
                item.column,
            )
            return
        self.conversions.setdefault(source, []).append(target)
        method_names = {m.name for m in item.methods}
        if "from" not in method_names:
            self._record_error(
                f"impl From<{source}> for {target} must define 'from' "
                "(the corresponding 'into()' is derived automatically)",
                item.line,
                item.column,
            )

    def _reject_builtin_reimplementation(
        self: "_Analyzer", item: ImplDecl
    ) -> None:
        """bug-31: reject re-implementing a built-in trait instantiation
        that the targeted built-in type already provides.

        ``builtin_methods.toml`` is the single source of truth for what
        ships with the language; a user impl of, say, ``Display for Int``
        would silently shadow or duplicate it.  Only exact instantiations
        conflict — extending a type with a *new* directional conversion
        (``impl Into<UInt> for Int``) stays legal until todo-92 decides
        the full orphan rules.
        """
        if item.struct.name not in BUILTIN_TYPES:
            return
        trait_args = [_type_str(a) for a in item.trait.args]
        instantiation = (
            item.trait.name
            if not trait_args
            else f"{item.trait.name}<{', '.join(trait_args)}>"
        )
        shipped = BUILTIN_TYPE_TRAITS.get(item.struct.name)
        if shipped is not None and instantiation in shipped:
            self._record_error(
                f"duplicate implementation of built-in trait "
                f"'{instantiation}' for '{_type_str(item.struct)}' "
                "(already provided by the language)",
                item.line,
                item.column,
            )

    def _check_builtin_impl_conformance(
        self: "_Analyzer", item: ImplDecl
    ) -> None:
        """Check an impl of a built-in trait against its declared signature.

        Built-in trait methods are data-driven (``builtin_methods.toml``);
        this is the user-impl counterpart of the user-trait conformance
        checks in :meth:`_check_impl_conformance`.
        """
        trait_name = item.trait.name
        required = BUILTIN_TRAIT_METHOD_NAMES[trait_name]
        trait_args = [_type_str(a) for a in item.trait.args]
        owner_type = _type_str(item.struct)
        impl_methods = {m.name: m for m in item.methods}

        for m in item.methods:
            if m.name not in required:
                self._record_error(
                    f"method '{m.name}' is not declared by built-in trait "
                    f"'{trait_name}'",
                    m.line,
                    m.column,
                )

        for name in required:
            fn = impl_methods.get(name)
            if fn is None:
                self._record_error(
                    f"impl of '{trait_name}' does not implement '{name}'",
                    item.line,
                    item.column,
                )
                continue
            spec = BUILTIN_TRAIT_METHODS[name]
            if fn.type_params:
                self._record_error(
                    f"method '{name}' of built-in trait '{trait_name}' "
                    "cannot have generic parameters",
                    fn.line,
                    fn.column,
                )
            self._check_builtin_method_signature(
                spec, fn, trait_args, trait_name, owner_type
            )

    def _check_builtin_method_signature(
        self: "_Analyzer",
        spec: MethodSpec,
        impl_fn: FnDecl,
        trait_args: list[str],
        trait_name: str,
        owner_type: str,
    ) -> None:
        """Compare one impl method against an instantiated built-in spec."""

        def bind_trait_arg(t: str) -> str:
            if t.startswith("TraitArg:"):
                idx = int(t[len("TraitArg:"):])
                if 1 <= idx <= len(trait_args):
                    return trait_args[idx - 1]
            return t

        def norm(t: str) -> str:
            s = _replace_self(bind_trait_arg(t), owner_type) or t
            # bug-52: 别名 (std::prelude 的 u32/...) 在一致性比较前展开,
            # trait 声明与 impl 用不同拼写 (u32 vs UInt32) 才能对上
            expanded = self._expand_type(s)
            return expanded if expanded is not None else s

        spec_args = [norm(a) for a in spec.args]
        spec_ret = norm(spec.returns)
        spec_self = bool(spec.args and spec.args[0] == "Self")
        impl_self = bool(impl_fn.params and impl_fn.params[0].name == "self")
        if spec_self != impl_self:
            self._record_error(
                f"method '{impl_fn.name}' of '{trait_name}' has mismatched self",
                impl_fn.line,
                impl_fn.column,
            )
            return
        spec_params = spec_args[1:] if spec_self else spec_args
        impl_params = impl_fn.params[1:] if impl_self else impl_fn.params
        if len(spec_params) != len(impl_params):
            self._record_error(
                f"method '{impl_fn.name}' of '{trait_name}' expects "
                f"{len(impl_params)} parameter(s), trait requires "
                f"{len(spec_params)}",
                impl_fn.line,
                impl_fn.column,
            )
            return
        for spec_t, p in zip(spec_params, impl_params):
            if p.type is None:
                continue
            it = norm(_type_str(p.type))
            if it != spec_t:
                self._record_error(
                    f"method '{impl_fn.name}' parameter '{p.name}' is {it}, "
                    f"trait requires {spec_t}",
                    impl_fn.line,
                    impl_fn.column,
                )
        ir = norm(
            _type_str(impl_fn.return_type)
            if impl_fn.return_type is not None
            else "None"
        )
        if ir != spec_ret:
            self._record_error(
                f"method '{impl_fn.name}' of '{trait_name}' returns {ir}, "
                f"trait requires {spec_ret}",
                impl_fn.line,
                impl_fn.column,
            )

    def _supertrait_methods(
        self: "_Analyzer", trait: TraitDecl
    ) -> tuple[set[str], set[str]]:
        """(required, allowed) method-name sets across ``trait``'s transitive
        supertraits.  ``allowed`` = every inherited method name; ``required`` =
        those without a default body (a supertrait default satisfies the
        requirement, so the implementor need not repeat it).  A ``seen`` set
        guards against a malicious trait-inheritance cycle."""
        required: set[str] = set()
        allowed: set[str] = set()
        seen = {trait.name}
        frontier = list(trait.supertraits)
        while frontier:
            st = frontier.pop()
            decl = self.traits.get(st.name)
            if decl is None or st.name in seen:
                continue
            seen.add(st.name)
            for m in decl.methods:
                allowed.add(m.name)
                if m.body is None:
                    required.add(m.name)
            frontier.extend(decl.supertraits)
        return required, allowed

    def _check_impl_conformance(self: "_Analyzer", item: ImplDecl, trait: TraitDecl) -> None:
        """Check that an impl satisfies the trait's method signatures, with
        the trait's type parameters substituted by the impl's arguments."""
        trait_params = [p.name for p in trait.params]
        trait_args = [a.name for a in item.trait.args]
        if not trait_args and trait_params:
            # todo-164: trailing omitted trait arguments fall back to the
            # trait's parameter defaults (``impl MyTrait for S`` when
            # ``trait MyTrait<A = Alloc>``) before the older spelling.
            self._fill_generic_defaults(item.trait, trait.params)
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
        assoc = {
            f"Self::{a.name}": _type_str(a.type, subst)
            for a in item.assoc_types
        }
        trait_methods = {m.name: m for m in trait.methods}
        # todo-156: the required/allowed method set is the *transitive* closure
        # of the trait and its supertraits (minus inherited default bodies,
        # which need not be re-provided).  Signature conformance below still
        # checks against the *named* trait only (a generic supertrait's arg
        # substitution through the inheritance chain is out of scope for this
        # step); inherited, body-less methods must merely be *present*.
        inherited_required, inherited_allowed = self._supertrait_methods(trait)
        # bug-51: impl 块级泛型形参 (impl<T> ... ) 不得被同名别名展开;
        # 一致性检查在 pass-2 分支的泛型作用域之外运行, 这里单独压入。
        saved_generics = self.active_generics
        self.active_generics = saved_generics | frozenset(
            p.name for p in item.params
        )
        try:
            for m in item.methods:
                if m.name in trait_methods:
                    self._check_method_signature(
                        trait_methods[m.name], m, subst, trait.name,
                        item.struct.name, assoc,
                    )
                elif m.name in inherited_allowed:
                    continue  # provides an inherited trait's method
                else:
                    self._record_error(
                        f"method '{m.name}' is not declared by trait '{trait.name}'",
                        m.line,
                        m.column,
                    )
        finally:
            self.active_generics = saved_generics
        provided = {m.name for m in item.methods}
        for name in trait_methods:
            if name not in provided:
                self._record_error(
                    f"impl of '{trait.name}' does not implement '{name}'",
                    item.line,
                    item.column,
                )
        for name in inherited_required:
            if name not in provided and name not in trait_methods:
                self._record_error(
                    f"impl of '{trait.name}' does not implement inherited "
                    f"'{name}'",
                    item.line,
                    item.column,
                )

    def _check_method_signature(
        self: "_Analyzer",
        trait_fn: FnDecl,
        impl_fn: FnDecl,
        subst: dict[str, str],
        trait_name: str,
        owner: str,
        assoc: Optional[dict[str, str]] = None,
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
        trait_ref = bool(
            trait_fn.params
            and trait_fn.params[0].type is not None
            and trait_fn.params[0].type.ref
        )
        impl_ref = bool(
            impl_fn.params
            and impl_fn.params[0].type is not None
            and impl_fn.params[0].type.ref
        )
        if trait_ref != impl_ref:
            self._record_error(
                f"method '{impl_fn.name}' of '{trait_name}' has mismatched "
                "self reference",
                impl_fn.line,
                impl_fn.column,
            )
            return
        if trait_ref and (
            bool(trait_fn.params[0].mutable)
            != bool(impl_fn.params[0].mutable)
        ):
            # bug-50: &self 与 &mut self 的 trait 方法签名不可互换
            self._record_error(
                f"method '{impl_fn.name}' of '{trait_name}' has mismatched "
                "self mutability",
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
            if s == "Self":
                s = owner
            if assoc:
                s = _subst_type_str(s, assoc)
            # bug-52: 别名在一致性比较前展开 (trait 声明 u32 / impl 写
            # UInt32 必须视为同一类型)
            expanded = self._expand_type(s)
            return expanded if expanded is not None else s

        # 方法级泛型形参 (trait 的 T / impl 的 U) 是 alpha 等价比较,
        # 不得被同名类型别名展开 —— 比较期间临时并入 active_generics。
        saved_generics = self.active_generics
        self.active_generics = (
            saved_generics
            | frozenset(impl_method_subst)
            | frozenset(impl_method_subst.values())
        )
        try:
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
        finally:
            self.active_generics = saved_generics
