"""Declaration mixin: extern block validation and C-ABI mapping rules."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .defs import (
    _EXTERN_SCALAR_TYPES,
    _EXTERN_SCALAR_WIDTHS,
    _EXTERN_MAX_NEST,
)

from ..types import (
    _base,
    _split_args,
    _split_fn_sig,
    _type_str,
    split_array_type,
)

from ...ast_components.ast import (
    ExternStatic,
    FnDecl,
    Type,
)

if TYPE_CHECKING:
    from ..analyzer import _Analyzer


class DeclExtern:

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
            # (todo-154: 节点名是 FQN 存储形, 先归一化再比较)
            ret_expanded = self._expand_type(_type_str(fn.return_type))
            if ret_expanded != "None" or fn.return_type.args:
                ret_name = _type_str(fn.return_type)
                # todo-54: fn 类型只能作回调参数, 不能作返回值
                if ret_name.startswith("fn("):
                    self._record_error(
                        f"extern function '{fn.name}' cannot return a "
                        "function pointer (callbacks are parameter-only)",
                        fn.return_type.line,
                        fn.return_type.column,
                    )
                # todo-88: Option<String> 返回映射到可空 char*
                elif self._option_string_ok(ret_name):
                    pass
                # bug-37: never (`!`) 返回同样映射到 C void (noreturn,
                # 如 C 的 exit/noreturn 函数)
                elif ret_name == "!":
                    pass
                else:
                    self._check_extern_abi_type(
                        fn, fn.return_type, "return type"
                    )

    def _check_extern_abi_type(
        self: "_Analyzer", fn: FnDecl, t: Type, what: str,
        decay: bool = False,
    ) -> None:
        # todo-154: 节点名是 FQN 存储形 —— 先展开归一化到裸名, 别名与
        # ``std::builtins::`` 前缀一并消失, 后续裸名集合校验才有效。
        name = self._expand_type(_type_str(t)) or ""
        # bug-58: fn 签名段内的别名 (c_void/c_uint/ctypedef...) 先展开成
        # 底层类型再校验, 展开后的完整签名同步写回节点注解 —— 后端
        # cg_fn_sig_split 按注解名拆段, 段名与 C 类型表对齐后
        # ``fn(*mut c_void) -> c_uint`` 形态的回调即可映射。
        if name.startswith("fn("):
            params, ret = _split_fn_sig(name)
            expanded_params = [
                self._expand_type(p) or p for p in params
            ]
            expanded_ret = self._expand_type(ret) if ret is not None else None
            rebuilt = "fn(" + ", ".join(expanded_params) + ")"
            if expanded_ret is not None and expanded_ret != "None":
                rebuilt += " -> " + expanded_ret
            self._ann_type(t, rebuilt)
        # todo-89: 带载荷枚举只允许出现在 extern 函数的顶层形参/返回位
        violation = self._c_abi_violation(
            name, decay=decay, payload_enum=True
        )
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
        violation = self._c_abi_violation(
            self._expand_type(_type_str(st.type)) or ""
        )
        if violation is not None:
            self._record_error(
                f"extern static '{st.name}' is "
                f"{self._fmt_type(_type_str(st.type))}, {violation}",
                st.type.line,
                st.type.column,
            )

    def _c_abi_violation(
        self: "_Analyzer", name: str, decay: bool = False,
        payload_enum: bool = False,
    ) -> Optional[str]:
        """Return the reason ``name`` has no C-ABI mapping, or ``None``.

        todo-67: with ``decay`` set (extern fn parameters), fixed-length
        arrays of fixed-width scalars follow C's array decay and map to
        an element pointer.

        todo-89: with ``payload_enum`` set (top-level extern fn
        parameter/return positions), enums carrying payloads map to a C
        struct ``{ int32 tag; <payload fields> }``; nested callback
        signatures and extern statics keep the fieldless-only rule.

        bug-29: ``std::prelude`` 的类型别名 (``f64``/``i32``/...) 必须先
        展开到底层类型才能对齐 C-ABI 支持表; 指针被指类型与数组元素类型
        同样需要展开。
        """
        expanded = self._expand_type(name)
        if expanded is not None:
            name = expanded
        supported = _EXTERN_SCALAR_TYPES
        ok = name in supported
        if not ok and (name.startswith("*const ") or name.startswith("*mut ")):
            pointee = name.split(" ", 1)[1] if " " in name else ""
            pointee = self._expand_type(pointee) or ""
            ok = pointee in supported
            # todo-59: 被指类型为纯内联结构体 -> 传真实地址,
            # 边界处按 C 布局拷贝, 无按值大小限制
            if not ok and pointee in self.structs:
                v = self._inline_struct_violation(pointee, 0, check_size=False)
                if v is None:
                    ok = True
            # todo-108: 被指类型为枚举 -> 按不透明指针直传地址
            # (与 C 的 `MyEnum*` / `void*` 不透明句柄一致, 边界处不做
            # 内容转换, 也无写回)。带载荷与否不影响指针表示。
            if not ok and pointee in self.enums:
                ok = True
        # todo-51/56: String 与 C 的 char* / const char* 双向互转.
        # 参数: 句柄 address 即字节指针直传; 返回: 按 NUL 结尾约定取 strlen.
        if not ok and name == "String":
            ok = True
        # todo-67: [T; N] 形参按 C 数组退化语义映射为 T* 元素指针
        arr = split_array_type(name)
        if not ok and arr is not None:
            elem, _n = arr
            ew = _EXTERN_SCALAR_WIDTHS.get(self._expand_type(elem) or elem)
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
        # todo-52/89: 无载荷枚举 -> i32 判别值; 带载荷枚举 -> C 结构体
        if not ok and name in self.enums:
            return self._enum_ffi_violation(name, payload_enum)
        if not ok:
            return (
                "which has no C-ABI mapping yet (v0 supports numeric/bool "
                "scalars, raw pointers to scalars or inline structs, "
                "String <-> char*, Option<String> as nullable char* "
                "returns, fixed-length arrays as decaying parameters, "
                "fn signatures as callback parameters, and inline "
                "struct/enum aggregates)"
            )
        return None

    def _option_string_ok(self: "_Analyzer", name: str) -> bool:
        """todo-88: whether ``name`` is exactly ``Option<String>``.

        The enum must exist and be shaped like the std prelude's
        ``Option<T>`` (a fieldless ``None`` variant plus a
        single-payload ``Some`` whose parameter instantiates to
        ``String``); only then does the nullable-``char*`` mapping
        apply.
        """
        if _base(name) != "Option" or _split_args(name) != ["String"]:
            return False
        en = self.enums.get("Option")
        if en is None or len(en.params) != 1:
            return False
        subst = {en.params[0].name: "String"}
        none_seen = some_seen = False
        for v in en.variants:
            if v.name == "None" and not v.fields:
                none_seen = True
            elif v.name == "Some" and len(v.fields) == 1:
                ft = self._expand_type(_type_str(v.fields[0], subst))
                if ft == "String":
                    some_seen = True
        return none_seen and some_seen

    def _enum_ffi_violation(
        self: "_Analyzer", name: str, payload_enum: bool = False
    ) -> Optional[str]:
        """todo-52/89: C-ABI mapping check for a non-generic enum.

        Fieldless enums map to positional ``i32`` discriminants
        (todo-52). Payload enums (todo-89) cross as a C struct whose
        first member is ``int32 tag`` followed by the payload fields --
        so every payload-carrying variant must declare the same field
        types, and payload leaves must be C-mappable inline scalars,
        arrays of them, or pure-inline structs.
        """
        en = self.enums[name]
        if en.params:
            return (
                "which is a generic enum (generic enums have no "
                "C-ABI mapping)"
            )
        payloads = [v for v in en.variants if v.fields]
        for v in en.variants:
            if v.value is not None:
                return (
                    f"whose variant '{v.name}' declares an explicit "
                    "value (C discriminants are positional indices)"
                )
        if not payloads:
            return None
        if not payload_enum:
            return (
                "whose variants carry payloads (payload enums cross "
                "FFI as extern fn parameters/returns only)"
            )
        shape: Optional[tuple[str, ...]] = None
        for v in payloads:
            cur = tuple(
                self._expand_type(_type_str(t)) if t is not None else "?"
                for t in v.fields
            )
            if shape is None:
                shape = cur
            elif cur != shape:
                return (
                    f"whose variant '{v.name}' carries a different "
                    "payload shape than earlier variants (payload "
                    "enums need one shared field list to mirror in C)"
                )
            bad = self._enum_payload_violation(cur)
            if bad is not None:
                return f"whose variant '{v.name}' is {bad}"
        return None

    def _enum_payload_violation(
        self: "_Analyzer", fields: tuple[str, ...]
    ) -> Optional[str]:
        """Validate one shared payload shape for todo-89 FFI enums."""
        if len(fields) > 16:
            return (
                "carrying more than 16 payload fields (v0 maps at "
                "most 16-field payloads)"
            )
        for ft in fields:
            arr = split_array_type(ft)
            if arr is not None:
                elem, _n = arr
                if elem not in _EXTERN_SCALAR_WIDTHS:
                    return (
                        f"carrying an array of '{elem}' (elements must "
                        "be fixed-width scalars)"
                    )
                continue
            if ft in _EXTERN_SCALAR_WIDTHS:
                continue
            if ft in self.structs:
                sub = self._inline_struct_violation(ft, 1)
                if sub is not None:
                    return f"carrying a struct that is {sub}"
                continue
            return (
                f"carrying a '{ft}' payload field (only fixed-width "
                "scalars, arrays of them, and inline structs map to C)"
            )
        return None

    def _inline_struct_violation(
        self: "_Analyzer", name: str, depth: int,
        check_size: bool = True,
    ) -> Optional[str]:
        """todo-52/61/65/66: validate a pure-inline aggregate struct.

        Fields may be fixed-width scalars, arrays of them, or other
        pure-inline structs (todo-66). Uniform-width all-scalar structs
        up to 16 bytes map to small-aggregate conventions (todo-65);
        aggregates containing arrays or nested structs use the memory
        convention without a size limit.

        todo-59: ``check_size=False`` skips the flat-scalar size cap --
        struct pointers (``*const S`` / ``*mut S``) hand C a real
        address, so any C-layout size crosses the boundary fine.
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
            # bug-29: 字段类型先展开别名 (std::prelude 的 f64/i32/...)
            ft = self._expand_type(_type_str(f.type)) \
                if f.type is not None else None
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
        if not check_size:
            # todo-59: 指针被指结构体按地址访问, 无大小限制
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
