"""Top-level collection and declaration checks (SA passes 1 and 2)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .builtin_methods import BUILTIN_TRAITS
from .const_fold import _const_number
from .symbols import MethodBinding, Symbol
from .types import (
    BUILTIN_TYPES,
    _BUILTIN_GENERIC_ARITY,
    _base,
    _compatible,
    _split_args,
    _type_str,
)
from ..ast_components.ast import (
    ConstDecl,
    EnumDecl,
    ExtraDecl,
    FnDecl,
    GroupApply,
    GroupDecl,
    ImplDecl,
    Node,
    StructDecl,
    TraitDecl,
    Type,
    TypeDecl,
)

if TYPE_CHECKING:
    from .analyzer import _Analyzer


class DeclarationChecks:

    # -- pass 1: collection ------------------------------------------------
    def _collect(self: "_Analyzer", item: Node) -> None:
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

    def _index(self: "_Analyzer", item: Node) -> None:
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
    def _check(self: "_Analyzer", item: Node) -> None:
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
            self._check_refined_value(_type_str(item.type), item.value)
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

    def _check_fn_types(self: "_Analyzer", fn: FnDecl) -> None:
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

    def _check_type(self: "_Analyzer", type_: Type, ctx: Node) -> None:
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
        self._require(name, {"struct", "enum"}, ctx, what)

    def _expand_type(self: "_Analyzer", t: Optional[str]) -> Optional[str]:
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

    def _compat_types(self: "_Analyzer", a: Optional[str], b: Optional[str]) -> bool:
        """Compatibility check with type aliases expanded on both sides."""
        return _compatible(self._expand_type(a), self._expand_type(b))

    def _fmt_type(self: "_Analyzer", t: Optional[str]) -> str:
        """A type for error messages; shows the expanded form for aliases."""
        if t is None:
            return "unknown"
        expanded = self._expand_type(t)
        return t if expanded == t else f"{t} ({expanded})"

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

    def _check_impl_conformance(self: "_Analyzer", item: ImplDecl, trait: TraitDecl) -> None:
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
        self: "_Analyzer",
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
