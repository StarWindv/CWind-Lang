"""Top-level collection and declaration checks (SA passes 1 and 2)."""

from __future__ import annotations

from dataclasses import fields as _fields
from typing import TYPE_CHECKING, Optional

from .builtin_methods import (
    BUILTIN_TRAIT_ARITY,
    BUILTIN_TRAIT_METHOD_NAMES,
    BUILTIN_TRAIT_METHODS,
    BUILTIN_TRAITS,
    MethodSpec,
)
from .const_fold import _const_number
from .symbols import MethodBinding, Symbol
from .types import (
    BUILTIN_TYPES,
    _BUILTIN_GENERIC_ARITY,
    _INTEGER,
    _base,
    _compatible,
    _replace_self,
    _split_args,
    _split_fn_sig,
    _subst_type_str,
    _type_str,
    split_array_type,
)
from ..ast_components.ast import (
    ConstDecl,
    EnumDecl,
    ExternBlock,
    ExternStatic,
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
    TypeParam,
)

if TYPE_CHECKING:
    from .analyzer import _Analyzer

# C-ABI-compatible scalar types for extern declarations (todo-48).
_EXTERN_SCALAR_TYPES: frozenset[str] = frozenset({
    "Int", "UInt", "Int8", "UInt8", "Byte", "Bool",
    "Int16", "UInt16",
    "Int32", "UInt32", "Int64", "UInt64", "Float", "Float64",
})

# todo-52: 各标量的 C 字节宽度 (与后端 cg_scalar_bytes 一致)
_EXTERN_SCALAR_WIDTHS: dict[str, int] = {
    "Int": 2, "UInt": 2,
    "Int8": 1, "UInt8": 1, "Byte": 1, "Bool": 1,
    "Int16": 2, "UInt16": 2,
    "Int32": 4, "UInt32": 4, "Float": 4,
    "Int64": 8, "UInt64": 8, "Float64": 8,
}

# todo-66: 内嵌纯内联结构体的最大嵌套层数 (与后端 CG_EXT_MAX_NEST 一致)
_EXTERN_MAX_NEST: int = 4

# main 允许的返回类型 (bug-24): 整数类型作为进程退出码 (与后端
# cg_emit_main_wrapper/cg_is_int 一致, 含 Byte), None/省略, 以及
# never 类型 `!` (Rust 语义: 永不返回的 main 合法)。
_MAIN_RETURN_TYPES: frozenset[str] = _INTEGER | {"Byte", "None", "!"}


class DeclarationChecks:

    # -- pass 1: collection ------------------------------------------------
    def _collect(self: "_Analyzer", item: Node) -> None:
        if isinstance(item, FnDecl):
            self._module_item_owners[id(item)] = getattr(
                item, "source_module", None
            )
        self._index(item)
        if isinstance(item, ExternBlock):
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

    def _index(self: "_Analyzer", item: Node) -> None:
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
                self.functions[fn.name] = fn
            # todo-56: extern 静态变量按名索引
            for st in item.statics:
                self.extern_statics[st.name] = st
        elif isinstance(item, ConstDecl):
            self.consts[item.name] = item
        elif isinstance(item, ImplDecl):
            generic = tuple(p.name for p in item.params)
            self.impls.setdefault(item.struct.name, []).append(item.trait.name)
            if item.trait.name == "Into" and len(item.trait.args) == 1:
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
        elif isinstance(item, EnumDecl):
            generic = {p.name for p in item.params}
            self.defined |= generic
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
                self.defined -= generic
        elif isinstance(item, ConstDecl):
            self._check_type(item.type, item)
            self._annotate_type_node(item.type)
            self._ann_type(item, _type_str(item.type))
            value = self._check_expr(item.value, _type_str(item.type))
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
            for fn in item.fns:
                self._check_extern_fn(fn)
            # todo-56: extern 静态变量的类型也必须能映射到 C-ABI
            for st in item.statics:
                self._check_extern_static(st)
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
                self.defined -= generic
            trait_decl = self.traits.get(item.trait.name)
            if item.trait.name in BUILTIN_TRAITS:
                self._check_builtin_impl_conformance(item)
            elif trait_decl is not None:
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

    def _group_types_match(
        self: "_Analyzer", expected: str, actual: str
    ) -> bool:
        """Group fields/distributions must agree on the underlying base
        type; merely both being numeric is not enough for a refinement."""
        exp = self._expand_type(expected)
        act = self._expand_type(actual)
        if exp is None or act is None:
            return True
        if _base(exp) != _base(act):
            return False
        return self._compat_types(expected, actual)

    def _check_group_distribution_type(
        self: "_Analyzer",
        d: "Distribution",
        subject_type: str,
        field: Optional["Field"],
    ) -> None:
        target = _type_str(d.type)
        if not self._group_types_match(target, subject_type):
            self._record_error(
                f"group distribution '{d.subject} -> {target}' cannot "
                f"receive {self._fmt_type(subject_type)}",
                d.line,
                d.column,
            )
        if field is not None and field.initializer is not None:
            self._check_refined_value(target, field.initializer, field)

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

    def _check_main_signature(self: "_Analyzer", fn: FnDecl) -> None:
        """``main`` 的返回值只能成为进程退出码 (bug-24)。

        允许: 整数类型 (含 ``Byte``, 与后端 ``cg_is_int`` 一致)、
        ``None``/省略、never (``!``); 其余类型在编译期拒绝,
        不再静默丢弃返回值。
        """
        if fn.name != "main":
            return
        if fn.return_type is None:
            return  # 省略返回类型 = None
        ret = _type_str(fn.return_type)
        expanded = self._expand_type(ret)
        base = _base(expanded)
        if not expanded.startswith("&") and base in _MAIN_RETURN_TYPES:
            return
        self._record_error(
            f"'main' must return an integer type or 'None', "
            f"found {self._fmt_type(ret)}",
            fn.return_type.line,
            fn.return_type.column,
        )

    def _check_extern_fn(self: "_Analyzer", fn: FnDecl) -> None:
        """Validate one function inside an ``extern`` block (todo-48).

        C ABI restrictions: no generics, no ``self``, no ``which`` hooks,
        and every parameter / the return type must map to a plain C type
        (numeric/bool scalars or raw pointers to them).
        """
        if fn.type_params:
            self._record_error(
                f"extern function '{fn.name}' cannot have generic "
                "parameters",
                fn.line,
                fn.column,
            )
        if fn.body is not None:
            self._record_error(
                f"extern function '{fn.name}' must be a declaration only "
                "(its body lives in the linked library)",
                fn.body.line,
                fn.body.column,
            )
        if fn.which is not None:
            self._record_error(
                f"'which' is not allowed on extern function '{fn.name}'",
                fn.line,
                fn.column,
            )
        self._check_fn_types(fn)
        for p in fn.params:
            if p.name == "self":
                self._record_error(
                    f"extern function '{fn.name}' cannot take 'self'",
                    p.line,
                    p.column,
                )
                continue
            if p.type is None:
                self._record_error(
                    f"extern parameter '{p.name}' requires a type "
                    "annotation",
                    p.line,
                    p.column,
                )
                continue
            self._check_extern_abi_type(
                fn, p.type, f"parameter '{p.name}'", decay=True
            )
        if fn.return_type is not None:
            # None 返回映射到 C void, 合法
            if fn.return_type.name != "None" or fn.return_type.args:
                # todo-54: fn 类型只能作回调参数, 不能作返回值
                if fn.return_type.name.startswith("fn("):
                    self._record_error(
                        f"extern function '{fn.name}' cannot return a "
                        "function pointer (callbacks are parameter-only)",
                        fn.return_type.line,
                        fn.return_type.column,
                    )
                else:
                    self._check_extern_abi_type(
                        fn, fn.return_type, "return type"
                    )

    def _check_extern_abi_type(
        self: "_Analyzer", fn: FnDecl, t: Type, what: str,
        decay: bool = False,
    ) -> None:
        name = t.name
        violation = self._c_abi_violation(name, decay=decay)
        if violation is not None:
            self._record_error(
                f"{what} of extern function '{fn.name}' is "
                f"{self._fmt_type(name)}, {violation}",
                t.line,
                t.column,
            )

    def _check_extern_static(self: "_Analyzer", st: ExternStatic) -> None:
        """Validate an extern static binding (todo-56).

        The bound type must map to a C global (numeric/bool scalar, raw
        pointer to one of them, or ``String`` <-> ``char*``); generics and
        containers have no representation in C.
        """
        if st.type is None:
            return
        self._check_type(st.type, st)
        self._annotate_type_node(st.type)
        self._ann_type(st, _type_str(st.type))
        violation = self._c_abi_violation(st.type.name)
        if violation is not None:
            self._record_error(
                f"extern static '{st.name}' is "
                f"{self._fmt_type(_type_str(st.type))}, {violation}",
                st.type.line,
                st.type.column,
            )

    def _c_abi_violation(
        self: "_Analyzer", name: str, decay: bool = False
    ) -> Optional[str]:
        """Return the reason ``name`` has no C-ABI mapping, or ``None``.

        todo-67: with ``decay`` set (extern fn parameters), fixed-length
        arrays of fixed-width scalars follow C's array decay and map to
        an element pointer.
        """
        supported = _EXTERN_SCALAR_TYPES
        ok = name in supported
        if not ok and (name.startswith("*const ") or name.startswith("*mut ")):
            pointee = name.split(" ", 1)[1] if " " in name else ""
            ok = pointee in supported
        # todo-51/56: String 与 C 的 char* / const char* 双向互转.
        # 参数: 句柄 address 即字节指针直传; 返回: 按 NUL 结尾约定取 strlen.
        if not ok and name == "String":
            ok = True
        # todo-67: [T; N] 形参按 C 数组退化语义映射为 T* 元素指针
        arr = split_array_type(name)
        if not ok and arr is not None:
            elem, _n = arr
            ew = _EXTERN_SCALAR_WIDTHS.get(elem)
            if ew is None:
                return (
                    f"whose element type '{elem}' is not a fixed-width "
                    "scalar"
                )
            if not decay:
                return (
                    "which cannot appear here (only extern parameters "
                    "decay to pointers)"
                )
            ok = True
        # todo-54/68: fn 签名可作回调参数 (内部各段仍须 C-ABI 兼容;
        # 段内数组形参同样按 C 退化语义放行, 返回段不退化)
        if not ok and name.startswith("fn("):
            params, ret = _split_fn_sig(name)
            if ret is not None and ret.startswith("fn("):
                return (
                    "which nests function-pointer types (not supported "
                    "in C callback signatures)"
                )
            bad = [
                p for p in params
                if self._c_abi_violation(p, decay=True) is not None
            ]
            if ret is not None and self._c_abi_violation(ret) is not None:
                bad.append(ret)
            ok = not bad
            if not ok:
                for p in params:
                    v = self._c_abi_violation(p, decay=True)
                    if v is not None:
                        return f"whose parameter '{p}' is {v}"
                if ret is not None:
                    v = self._c_abi_violation(ret)
                    if v is not None:
                        return f"whose return type is {v}"
        # todo-52/61/65/66: 纯内联聚合 (标量/定长数组/内嵌结构体字段)
        if not ok and name in self.structs:
            return self._inline_struct_violation(name, 0)
        if not ok and name in self.enums:
            en = self.enums[name]
            for v in en.variants:
                if v.fields:
                    return (
                        f"whose variant '{v.name}' carries a payload "
                        "(only fieldless enums map to C)"
                    )
                if v.value is not None:
                    return (
                        f"whose variant '{v.name}' declares an explicit "
                        "value (C discriminants are positional indices)"
                    )
            return None
        if not ok:
            return (
                "which has no C-ABI mapping yet (v0 supports numeric/bool "
                "scalars, raw pointers to them, String <-> char*, "
                "fixed-length arrays as decaying parameters, "
                "fn signatures as callback parameters, and inline "
                "struct/fieldless-enum aggregates)"
            )
        return None

    def _inline_struct_violation(
        self: "_Analyzer", name: str, depth: int
    ) -> Optional[str]:
        """todo-52/61/65/66: validate a pure-inline aggregate struct.

        Fields may be fixed-width scalars, arrays of them, or other
        pure-inline structs (todo-66). Uniform-width all-scalar structs
        up to 16 bytes map to small-aggregate conventions (todo-65);
        aggregates containing arrays or nested structs use the memory
        convention without a size limit.
        """
        st = self.structs[name]
        if depth > _EXTERN_MAX_NEST:
            return (
                "which nests inline structs too deeply (v0 allows at "
                f"most {_EXTERN_MAX_NEST} levels)"
            )
        if st.params:
            return (
                "which is a generic struct (generic aggregates have "
                "no C-ABI mapping)"
            )
        widths = _EXTERN_SCALAR_WIDTHS
        scalar_widths: list[int] = []
        complex_field = False
        live_fields = 0
        for f in st.fields:
            if getattr(f, "static", False):
                continue
            live_fields += 1
            ft = _type_str(f.type) if f.type is not None else None
            arr = split_array_type(ft)
            if ft is not None and arr is not None:
                elem, n = arr
                ew = widths.get(elem)
                if ew is None:
                    return (
                        f"whose field '{f.name}' ({ft}) has no C-ABI "
                        "mapping (array elements must be fixed-width "
                        "scalars)"
                    )
                complex_field = True
                continue
            if ft is not None and ft in self.structs:
                sub = self._inline_struct_violation(ft, depth + 1)
                if sub is not None:
                    return f"whose field '{f.name}' ({ft}) is {sub}"
                complex_field = True
                continue
            w = widths.get(ft) if ft is not None else None
            if ft is None or w is None:
                return (
                    f"whose field '{f.name}' "
                    f"({ft if ft is not None else 'unknown'}) has no "
                    "C-ABI mapping (only scalar fields do)"
                )
            scalar_widths.append(w)
        if live_fields > 16:
            return (
                "which has more than 16 fields (v0 maps at most "
                "16-field aggregates)"
            )
        if complex_field:
            # 含数组/嵌套字段: 内存约定/寄存器对传递, 无大小限制
            return None
        if not scalar_widths:
            return "which has no fields"
        if depth > 0:
            # 嵌套内层不做大小限制 (随外层聚合整体布局)
            return None
        # todo-65: 平铺标量聚合上限从 8 放宽到 16 字节, 允许混合宽度
        # (后端按目标 ABI 分派: <=8B 单寄存器按位镜像 / SysV 寄存器对 /
        # 其余内存约定, 均按 C 视图几何布局搬运)
        total = sum(scalar_widths)
        if total > 16:
            return (
                "whose total size exceeds 16 bytes (v0 maps only "
                "single-register and small aggregates)"
            )
        return None

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
                    self._record_error(
                        f"bound '{bound_name}' expects "
                        f"{len(trait.params)} type argument(s), "
                        f"got {len(p.bound.args)}",
                        p.bound.line,
                        p.bound.column,
                    )
            for arg in p.bound.args:
                self._check_type(arg, p)

    def _check_type(self: "_Analyzer", type_: Type, ctx: Node) -> None:
        is_path = "::" in type_.name
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
                elif elem not in _EXTERN_SCALAR_TYPES:
                    self._record_error(
                        f"array element type '{elem}' is not a fixed-width "
                        "scalar",
                        type_.line,
                        type_.column,
                    )
            self._ann_type(type_, _type_str(type_))
            return
        if (not is_path
                and type_.name not in BUILTIN_TYPES
                and type_.name not in self.defined
                and type_.name != "Self"):
            # point at the type name itself, not at the enclosing statement
            self._record_error(f"unknown type '{type_.name}'", type_.line, type_.column)
        arity = _BUILTIN_GENERIC_ARITY.get(type_.name)
        if not is_path and arity is not None and len(type_.args) != arity:
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
            ref = t.startswith("&")
            if ref:
                t = t[1:]
            base = _base(t)
            alias = self.type_aliases.get(base)
            if alias is None:
                return ("&" + t) if ref else t
            args = _split_args(t)
            if len(args) != len(alias.params):
                return ("&" + t) if ref else t
            subst = dict(zip([p.name for p in alias.params], args))
            t = _type_str(alias.base, subst)
            if ref:
                t = "&" + t
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
            return _replace_self(bind_trait_arg(t), owner_type) or t

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
        assoc = {
            f"Self::{a.name}": _type_str(a.type, subst)
            for a in item.assoc_types
        }
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
            self._check_method_signature(
                tm, m, subst, trait.name, item.struct.name, assoc
            )
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
                return owner
            if assoc:
                s = _subst_type_str(s, assoc)
            return s

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
