"""Expression mixin: call dispatch, user/builtin callee checks and generic-argument unification."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .defs import _parse_fn_signature

from ..symbols import (
    MethodBinding,
    _find_method,
)

from ..types import (
    _INTEGER,
    _NUMERIC,
    _compatible,
    bare_type,
    _base,
    _is_ref,
    _replace_self,
    _split_args,
    _split_ref_prefix,
    _strip_ref,
    _subst_type_str,
    _trait_bare,
    _type_info,
    _type_mentions,
    _type_str,
    HANDLE_IDENTITY_TYPES,
)
from ...ast_components.ast import (
    Attribute,
    Call,
    EnumDecl,
    FnDecl,
    Name,
    StrLit,
    Variant,
)

if TYPE_CHECKING:
    from ..analyzer import _Analyzer


class ExprCalls:

    def _check_call(
        self: "_Analyzer", call: Call, expected: Optional[str] = None
    ) -> Optional[str]:
        result = self._check_call_inner(call, expected)
        self._ann_type(call, result)
        return result

    def _check_call_inner(
        self: "_Analyzer", call: Call, expected: Optional[str] = None
    ) -> Optional[str]:
        arg_types = [self._check_expr(a.value) for a in call.args]
        callee = call.callee
        if isinstance(callee, Name):
            if len(callee.parts) == 1:
                n = callee.parts[0]
                info = self._lookup(n)
                if (
                    info is not None
                    and info.type is not None
                    and self._expand_type(info.type) is not None
                    and str(self._expand_type(info.type)).startswith("fn(")
                ):
                    return self._check_indirect_call(call, n, arg_types)
                # todo-44: an expansion-bound callee that misses the scopes
                # may denote a file-level function or builtin (macro
                # hygiene is local-binding scoped); retry with base name.
                base = self._unmangle(n)
                if base is not None and base != n and info is None and (
                    base in self.functions
                ):
                    n = base
                    callee.parts = [base]
                if n in self.functions:
                    if self._reject_hidden(n, "function", callee):
                        return None
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
                self._record_error(f"unknown function '{n}'", call.line, call.column)
                return None
            if len(callee.parts) == 2:
                mod, member = callee.parts
                # todo-44: expansion-bound two-part paths
                # (``Vector::new`` spliced from a macro body) mangle the
                # owner; try the base owner when the mangled one has no
                # surface at all.  Members (methods, associated fns,
                # variants) are unhygienic, so a mangled member always
                # resolves to its base name.
                base = self._unmangle(mod)
                if base is not None and not self._file_level_hit(mod) and (
                    base in self.modules
                    or base in self.structs
                    or base in self.enums
                    or base in self.methods
                ):
                    mod = base
                member = self._hygiene_member(member)
                exports = self.module_exports.get(mod)
                if mod in self.modules and (
                    member not in self.functions
                    or self.functions[member].pub is False
                    or (exports is not None and member not in exports)
                ):
                    # Let the Name check emit the precise visibility/unknown
                    # member error, instead of reporting "unknown function".
                    self._check_expr(callee)
                    return None
                if mod in self.modules:
                    exports = self.module_exports.get(mod)
                    fn = self.functions.get(member)
                    const = self.consts.get(member)
                    if const is not None and not getattr(const, "pub", False):
                        const = None
                    if fn is None and const is not None:
                        # bug-57: ``module::CONST`` -- a pub const is a value
                        # (module provenance recorded for the backend), and
                        # assigning to it is rejected by the existing
                        # const-target guard in the assignment checker.
                        callee._typed_ann["binding"] = {
                            "kind": "const", "ref": const._typed_id,
                        }
                        callee._typed_ann["module"] = {
                            "path": list(self.modules[mod]),
                            "source": self._module_sources.get(mod),
                        }
                        self._ann_type(callee, _type_str(const.type))
                        return _type_str(const.type)
                    if fn is None:
                        self._record_error(
                            f"module '{'::'.join(self.modules[mod])}' has "
                            f"no function '{member}'",
                            call.line,
                            call.column,
                        )
                        return None
                    if fn.pub is False or (
                        exports is not None and member not in exports
                    ):
                        self._record_error(
                            f"function '{member}' is private in "
                            f"module '{'::'.join(self.modules[mod])}'",
                            call.line,
                            call.column,
                        )
                        return None
                    result, subst = self._check_user_call(
                        fn,
                        call,
                        arg_types,
                        is_method=False,
                    )
                    # todo-80: keep the return contract at the qualified
                    # call site, just like a bare-name call at its return
                    # position.  A diverging callee (`-> !`) may flow into
                    # any expected type (Rust's never-to-T coercion).
                    if (
                        expected is not None
                        and result is not None
                        and result != "!"
                        and not self._compat_types(expected, result)
                    ):
                        self._record_error(
                            f"return type mismatch: expected "
                            f"{self._fmt_type(expected)}, got "
                            f"{self._fmt_type(result)}",
                            call.line,
                            call.column,
                        )
                    callee._typed_ann["binding"] = {
                        "kind": "fn", "ref": fn._typed_id,
                    }
                    callee._typed_ann["module"] = {
                        "path": list(self.modules[mod]),
                        "source": self._module_sources.get(mod),
                    }
                    self._ann_type(callee, "Fn")
                    self._ann_call(call, "fn", fn._typed_id, subst)
                    return result
                if mod == "Self" and self.current_owner is not None:
                    mod = self.current_owner
                enum = self.enums.get(mod)
                if enum is not None:
                    variant = next(
                        (v for v in enum.variants if v.name == member),
                        None,
                    )
                    if variant is not None:
                        if self._reject_hidden(mod, "enum", callee):
                            return None
                        return self._check_enum_variant_call(
                            enum, variant, call, arg_types, expected
                        )
                # bug-43: the method table is keyed by the expanded owner
                # type (aliases in impl/extra targets are canonicalized),
                # so resolve the alias before the lookup (mirrors the
                # builtin lookup below).  todo-154: ``mod`` is also
                # canonicalized to the expanded bare owner so ``Self``
                # return positions (``Vec::new() -> Self``) bind to
                # ``Vector``, not the alias spelling.
                mod_canon = _base(self._expand_type(mod) or mod) or mod
                # bug-65: 泛型形参上的关联函数 (``U::from(x)``, 其中 U 带
                # ``U: From<T>`` 约束), 按约束 trait 的方法表解析 --
                # ``member`` 是约束 trait 声明的方法名; Self 绑定到形参,
                # TraitArg:N 绑定到约束的实参, 返回类型即形参本身。
                if mod_canon in self.active_generics:
                    resolved = self._resolve_generic_bound_method(
                        mod_canon, member, call, arg_types
                    )
                    return resolved
                binding = _find_method(
                    self.methods.get(mod_canon, []),
                    member,
                )
                if binding is not None:
                    if binding.fn.which is not None and not getattr(
                        call, "_synthetic", False
                    ):
                        self._record_error(
                            f"which hook '{member}' cannot be called directly",
                            call.line,
                            call.column,
                        )
                        return None
                    if self._binding_takes_self(binding) and self.current_owner_type is None:
                        # 实例方法经 ``Type::method(...)`` 静态调用且无隐式
                        # self 语境: 必须在值上调用 (Rust 式诊断)。
                        self._record_error(
                            f"instance method '{member}' of '{mod_canon}' must "
                            "be called on a value",
                            call.line,
                            call.column,
                        )
                        callee._typed_ann["binding"] = {
                            "kind": "method", "ref": binding.id
                        }
                        self._ann_type(callee, "Fn")
                        self._ann_call(call, "method", binding.id, {})
                        return self._binding_return(binding, mod_canon)
                    result, subst = self._check_user_call(
                        binding.fn,
                        call,
                        arg_types,
                        is_method=True,
                        owner_hint=(
                            self.current_owner_type
                            if self.current_owner_type is not None
                            else mod_canon
                        ),
                        binding=binding,
                        expected=expected,
                    )
                    self._mark_implicit_self_moved(binding)
                    callee._typed_ann["binding"] = {
                        "kind": "method", "ref": binding.id
                    }
                    self._ann_type(callee, "Fn")
                    self._ann_call(call, "method", binding.id, subst)
                    self._record_hook_site(call, binding)
                    return result
                self._record_error(f"'{mod}' has no method '{member}'", call.line, call.column)
                return None
            # todo-81: constructor form ``module::Enum::Variant(...)``.
            # Resolved through the module surface (distinct unknown/private
            # diagnostics), then normalized to the two-segment callee that
            # downstream checks and the backend consume.
            # todo-133: a leading chain of module namespaces folds first —
            # ``geom::shapes::v()`` reaches its member like the two-segment
            # form; pure ``mod::Enum::Variant`` paths stay untouched.
            if len(callee.parts) >= 3:
                folded = self._fold_module_path(callee.parts)
                if folded is not None and len(folded) == 2:
                    callee.parts = folded
                    return self._check_call_inner(call, expected)
                if len(callee.parts) != 3:
                    return None
            if len(callee.parts) == 3:
                mod, enum_name, variant_name = callee.parts
                if mod not in self.modules:
                    self._record_error(
                        f"unknown function '{'::'.join(callee.parts)}'",
                        call.line,
                        call.column,
                    )
                    return None
                if not self._require_module_type(
                    callee, mod, enum_name, {"enum"}
                ):
                    return None
                enum = self.enums.get(enum_name)
                if enum is None:
                    self._record_error(
                        f"module '{'::'.join(self.modules[mod])}' has no "
                        f"enum '{enum_name}'",
                        call.line,
                        call.column,
                    )
                    return None
                variant = next(
                    (v for v in enum.variants if v.name == variant_name),
                    None,
                )
                if variant is None:
                    self._record_error(
                        f"enum '{enum_name}' has no variant "
                        f"'{variant_name}'",
                        call.line,
                        call.column,
                    )
                    return None
                if self._reject_hidden(enum_name, "enum", callee):
                    return None
                callee._typed_ann["binding"] = {
                    "kind": "variant", "ref": variant._typed_id
                }
                callee._typed_ann["module"] = {
                    "path": list(self.modules[mod]),
                    "source": self._module_sources.get(mod),
                }
                callee.parts = [enum_name, variant_name]
                return self._check_enum_variant_call(
                    enum, variant, call, arg_types, expected
                )
            self._record_error("unsupported call target", call.line, call.column)
            return None
        if isinstance(callee, Attribute):
            recv = self._expand_type(self._check_expr(callee.obj))
            if recv is None:
                return None
            base = _base(recv)
            binding = _find_method(self.methods.get(base, []), callee.name)
            if (
                binding is not None
                and callee.name == "into"
                and base in self.active_generics
            ):
                # bug-21: a bare generic receiver resolves ``into()`` through
                # its ``Into<Target>`` bound (or is rejected for lacking one);
                # the blanket ``impl<T, U: From<T>> Into<U> for T`` must not
                # shadow that with an unresolved ``U``.
                binding = None
            if binding is not None:
                if binding.fn.which is not None and not getattr(
                    call, "_synthetic", False
                ):
                    self._record_error(
                        f"which hook '{callee.name}' cannot be called directly",
                        call.line,
                        call.column,
                    )
                    return None
                if (
                    callee.name == "format"
                    and isinstance(callee.obj, StrLit)
                ):
                    # 前端不解析模板 (那是后端栈机的工作), 只做最基本的
                    # 花括号配平检查, 让明显写坏的模板尽早报错。
                    self._check_format_braces(callee.obj, call.args)
                if not self._method_self_is_ref(binding) and recv.startswith(
                    "&"
                ) and _base(recv) not in HANDLE_IDENTITY_TYPES:
                    # todo-186: 句柄恒等表示的容器 (Vector/Map/Set/String/
                    # Tuple) 例外 —— 借用与本体同表示, 经引用调用按值 self
                    # 的方法 (for-in 降糖的 into_iter) 只是句柄拷贝, 不消耗
                    # 被借容器。
                    self._record_error(
                        f"cannot call by-value method '{callee.name}' on a "
                        "reference; declare it as '&self' or move the value",
                        call.line,
                        call.column,
                    )
                    return None
                if self._method_takes_mut_self(binding) and not (
                    self._receiver_is_mutable_place(callee.obj)
                ):
                    # bug-50: &mut self 方法要求接收者是可变位置
                    self._record_error(
                        f"cannot call mutable method '{callee.name}' on an "
                        "immutable receiver; declare the binding with 'mut'",
                        call.line,
                        call.column,
                    )
                    return None
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
                if not (
                    recv.startswith("&")
                    and _base(recv) in HANDLE_IDENTITY_TYPES
                ):
                    # 句柄恒等容器经引用调用只拷贝句柄, 被借容器未消耗,
                    # 不标记接收者为 moved。
                    self._mark_receiver_moved(binding, callee.obj)
                callee._typed_ann["member"] = {
                    "kind": "method", "ref": binding.id
                }
                self._ann_type(callee, result)
                self._ann_call(call, "method", binding.id, subst)
                self._record_hook_site(call, binding)
                return result
            if callee.name == "to_string" and any(
                _type_mentions(recv, name)
                for name in self.active_generics
            ):
                # 泛型 opaque 接收者的 Display 回退: 具体实例化时由后端
                # 把接收者替换成实参类型, 再按内置 to_string 分派。
                callee._typed_ann["member"] = {
                    "kind": "builtin", "ref": "to_string"
                }
                self._ann_type(callee, "String")
                self._ann_call(call, "builtin", "to_string")
                return "String"
            if callee.name == "into":
                # ``Into<T>`` 方向性转换: bound 驱动的泛型形参目标
                # (bug-21) 优先, 用户 ``impl From`` (source 声明) 次之。
                # bug-21: 接收者本身是带 ``Into<Target>`` 约束的泛型参数
                # (如 `trait Foo<T: Into<String>>` 里的 `value.into()`);
                # 具体目标由约束给出, 实例化时替换为实参类型。
                bound = self._generic_into_target(recv)
                if bound is not None:
                    if call.args:
                        self._record_error(
                            "'into()' derived from a trait bound takes no "
                            "arguments",
                            call.line,
                            call.column,
                        )
                        return None
                    callee._typed_ann["member"] = {
                        "kind": "builtin", "ref": "into"
                    }
                    self._ann_type(callee, bound)
                    self._ann_call(call, "builtin", "into")
                    return bound
                # `x.into()` resolves through user-declared conversions; the
                # impl lives on the target type, so it is not in the receiver's
                # own method table.  Desugar it to `Target::from(x)`.
                return self._desugar_user_into(call, recv, expected)
            # bug-68: inside a trait default-method body the receiver type
            # binds to the trait itself (``Self: Trait``); a method miss on
            # that type resolves through the trait's own method table
            # (supertraits included) instead of the unknown-method error.
            if binding is None and base == self.current_trait and (
                self.current_trait is not None
            ):
                return self._check_trait_self_method(
                    callee, call, arg_types, expected
                )
            # bug-39: 具体接收者类型上的未知方法必须报错 (此前静默容忍,
            # `a.unwrap_of("")` 这类拼写错误直接变成 opaque 类型通过 SA);
            # 泛型 opaque 接收者 (裸参数 T / Vector<T> 等) 保持容忍 ——
            # 方法可能在实例化后才确定 (trait bound / 具体实参类型)。
            if recv is not None and not any(
                _type_mentions(recv, name)
                for name in self.active_generics
            ) and base not in ("Any", "Fn"):
                self._record_error(
                    f"type '{base}' has no method '{callee.name}'",
                    call.line,
                    call.column,
                )
            return None
        self._record_error("cannot call this expression", call.line, call.column)
        return None

    def _check_indirect_call(
        self: "_Analyzer",
        call: Call,
        name: str,
        arg_types: list[Optional[str]],
    ) -> Optional[str]:
        """Call a variable/parameter holding a ``fn(...)`` value."""
        info = self._lookup(name)
        if info is None or info.type is None:
            return None
        fn_type = self._expand_type(info.type)
        if fn_type is None or not str(fn_type).startswith("fn("):
            return None
        sig_args, sig_ret = _parse_fn_signature(str(fn_type))
        if len(arg_types) != len(sig_args):
            self._record_error(
                f"function pointer '{name}' expects {len(sig_args)} "
                f"argument(s), got {len(arg_types)}",
                call.line,
                call.column,
            )
            return None
        for i, (want, got) in enumerate(zip(sig_args, arg_types)):
            if not self._compat_types(want, got):
                self._record_error(
                    f"argument {i + 1} of '{name}' must be "
                    f"{self._fmt_type(want)}, got {self._fmt_type(got)}",
                    call.line,
                    call.column,
                )
        callee = call.callee
        if info.node is not None and info.node._typed_id is not None:
            callee._typed_ann["binding"] = {
                "kind": "var", "ref": info.node._typed_id,
            }
        else:
            callee._typed_ann["binding"] = {"kind": "var"}
        self._ann_type(callee, fn_type)
        self._ann_call(call, "indirect", name)
        return sig_ret

    def _check_enum_variant_call(
        self: "_Analyzer",
        enum: EnumDecl,
        variant: Variant,
        call: Call,
        arg_types: list[Optional[str]],
        expected: Optional[str] = None,
    ) -> Optional[str]:
        """Check ``Enum::Variant(args)`` construction and infer the enum's
        generic arguments from the payload values.

        实参推断不了的形参由调用点期望类型补齐 (Rust 推断语义:
        ``return Ok(x)`` 按函数返回类型定 T)。"""
        variant_index = next(
            i for i, v in enumerate(enum.variants) if v is variant
        )
        call._typed_ann["enum"] = enum.name
        enum_def = self._type_def_path(enum.name)
        if enum_def is not None:
            call._typed_ann["enum_def"] = enum_def
        call._typed_ann["variant_index"] = variant_index
        if not variant.fields:
            if call.args:
                self._record_error(
                    f"variant '{variant.name}' of enum '{enum.name}' "
                    "takes no payload",
                    call.line,
                    call.column,
                )
            self._ann_call(call, "enum_variant", variant.name)
            if enum.params and expected is not None:
                # 单元变体构造的泛型同样由调用点期望类型补齐
                # (``_ => Option::None`` 臂, Rust 推断语义)。
                exp = self._expand_type(expected)
                if exp is not None and _base(exp) == enum.name:
                    subst_u: dict[str, str] = {}
                    self._unify_generic(
                        f"{enum.name}<{', '.join(
                            p.name for p in enum.params
                        )}>",
                        exp,
                        subst_u,
                        {p.name for p in enum.params},
                    )
                    self._ann_call(call, "enum_variant", variant.name)
                    self._ann_type(call, exp)
                    return exp
            self._ann_type(call, enum.name)
            return enum.name
        if len(call.args) != len(variant.fields):
            self._record_error(
                f"variant '{variant.name}' of enum '{enum.name}' expects "
                f"{len(variant.fields)} payload value(s), "
                f"got {len(call.args)}",
                call.line,
                call.column,
            )
            self._ann_call(call, "enum_variant", variant.name)
            self._ann_type(call, enum.name)
            return enum.name
        generic_names = {p.name for p in enum.params}
        subst: dict[str, str] = {}
        for f, at in zip(variant.fields, arg_types):
            self._unify_generic(_type_str(f), at, subst, generic_names)
        if enum.params and expected is not None:
            # 实参推断不了的形参按期望类型补齐 (``return Ok(x)`` 的
            # T 由函数返回类型给出; 与 Rust 推断语义一致)。
            exp = self._expand_type(expected)
            if exp is not None and _base(exp) == enum.name:
                self._unify_generic(
                    f"{enum.name}<{', '.join(p.name for p in enum.params)}>",
                    exp,
                    subst,
                    generic_names,
                )
        payload_types: list[Optional[str]] = []
        for i, (f, arg) in enumerate(zip(variant.fields, call.args)):
            ft = _subst_type_str(_type_str(f), subst)
            if not self._compat_types(ft, arg_types[i]):
                # 期望类型驱动的实参重查: ``Result::Err(e.into())`` 的
                # into 目标由载荷类型给出 (首次检查时 expected 未下传)。
                t2 = self._check_expr(arg.value, ft)
                if t2 is not None and self._compat_types(ft, t2):
                    arg_types[i] = t2
            if not self._compat_types(ft, arg_types[i]):
                self._record_error(
                    f"payload {i + 1} of variant '{variant.name}' must be "
                    f"{self._fmt_type(ft)}, "
                    f"got {self._fmt_type(arg_types[i])}",
                    call.line,
                    call.column,
                )
            self._check_literal_range(ft, arg.value)
            self._check_refined_value(ft, arg.value)
            payload_types.append(ft)
        result = enum.name
        if enum.params:
            result = f"{enum.name}<{', '.join(
                _subst_type_str(p.name, subst) for p in enum.params
            )}>"
        call._typed_ann["payload_types"] = [
            _type_info(self._expand_type(t), self._opaque_names())
            for t in payload_types
        ]
        self._ann_call(call, "enum_variant", variant.name)
        self._ann_type(call, result)
        return result

    def _check_call_bound_conformance(
        self: "_Analyzer",
        fn: "FnDecl",
        subst: dict[str, str],
        generic_names: set[str],
        call: "Call",
    ) -> None:
        """todo-164: call-site validation of associated-type bounds.

        With ``fn f<T: Iterator<Item = Int32>>(...)``, a call whose subst
        binds ``T`` to a concrete type verifies the impl of ``Iterator``
        for that type provides ``type Item = Int32`` (or a still-generic
        value, which defers like Rust's pending obligation).
        """
        if not subst or not fn.type_params:
            return
        for tp in fn.type_params:
            if tp.bound is None or not tp.bound.bindings:
                continue
            bound = tp.bound
            actual = subst.get(tp.name)
            if actual is None:
                continue
            actual = self._expand_type(actual)
            if actual is None or actual in generic_names:
                continue  # generic receivers defer the obligation
            base = _base(actual)
            for b in bound.bindings:
                provided = self.impl_assoc_types.get((base, bound.name))
                if provided is None:
                    # No impl of the bound trait: _satisfies_bound-style
                    # failures already surface through the bound checks;
                    # nothing to compare here.
                    continue
                pv = provided.get(b.name)
                if pv is None:
                    continue
                want = b.type.name
                got = pv.name
                # todo-154: 错误消息与比较一律裸名 (impl 关联类型的
                # Type 节点是 FQN 存储形)
                want = bare_type(want) or want
                got = bare_type(got) or got
                if got in generic_names:
                    continue  # generic assoc value defers
                if want != got:
                    self._record_error(
                        f"type '{base}' implements '{bare_type(bound.name) or bound.name}' with "
                        f"'{b.name} = {got}', but this call requires "
                        f"'{b.name} = {want}'",
                        call.line,
                        call.column,
                    )

    def _check_bound_argument_conformance(
        self: "_Analyzer",
        fn: "FnDecl",
        call: "Call",
        arg_types: list[Optional[str]],
        subst: dict[str, str],
        generic_names: set[str],
    ) -> None:
        """特权退役后的通用机制: bound 驱动的实参校验与改写。

        任何被调函数 (``extern "CWind"`` 内建或用户 fn) 的形参类型提及
        带 trait bound 的泛型形参时 (``fn print<T: Display>(value: &T)``,
        或用户写的 ``fn write<T: Display>(value: &T)``), 调用点在泛型实
        参推断完成后按声明签名统一处理 —— 不针对任何特定函数名:

        * ``Display`` bound 的实参具体化时, 实参类型必须满足 bound
          (有 ``Display`` impl, 经 ``_satisfies_bound`` 含 supertrait);
        * 满足 bound 且实参是用户 ``Display`` impl 的接收者时, 把实参
          改写成 ``expr.to_string()`` (与 bug-13 的旧特权行为一致, 但
          由 bound 通用触发);
        * 实参仍是泛型 opaque (实例化未定) 时延后, 与 Rust 的
          pending obligation 同语义。
        """
        if not fn.type_params or not subst:
            return
        display_params = {
            tp.name for tp in fn.type_params
            if tp.bound is not None
            and _trait_bare(tp.bound.name) == "ToString"
        }
        if not display_params:
            return
        params = fn.params
        if params and params[0].name == "self":
            params = params[1:]
        if len(call.args) != len(params):
            return  # arity 错误已由通用形参比对报告
        for i, param in enumerate(params):
            if param.type is None:
                continue
            _, pt = _split_ref_prefix(_type_str(param.type))
            bound_param = next(
                (name for name in display_params if _type_mentions(pt, name)),
                None,
            )
            if bound_param is None:
                continue
            actual = self._expand_type(subst.get(bound_param))
            if actual is None or actual in generic_names:
                continue  # 泛型实参仍未知: 延后
            arg_t = self._expand_type(arg_types[i])
            if arg_t is None:
                continue
            # ``&T`` 形参按 Rust 自动借用语义接收 ``值``/``&T``/``&mut T``,
            # 校验一律剥引用。
            arg_base = _base(_strip_ref(arg_t) or arg_t)
            if not self._satisfies_bound(arg_base, "ToString"):
                self._record_error(
                    f"type {self._fmt_type(_strip_ref(arg_t))} does not "
                    "implement 'ToString::to_string', required by "
                    f"'{fn.name}'",
                    call.args[i].line,
                    call.args[i].column,
                )
                continue
            self._rewrite_display_arg(call, i, arg_t)

    def _rewrite_display_arg(
        self: "_Analyzer", call: "Call", index: int, arg_type: Optional[str]
    ) -> None:
        """把满足 Display bound 的用户类型实参改写成 ``expr.to_string()``
        (通用 bound 驱动, 取代旧 print 特权改写)。"""
        binding = self._user_display_binding(arg_type)
        if binding is None:
            return
        original = call.args[index].value
        if (
            isinstance(original, Call)
            and isinstance(original.callee, Attribute)
            and original.callee.name == "to_string"
        ):
            return  # 已是 to_string 调用
        attr = Attribute(original.line, original.column, original, "to_string")
        synthetic = Call(original.line, original.column, attr, [])
        self._assign_synthetic_ids(synthetic)
        # to_string 是 ``&self`` 方法: 接收者不移动。改写时只解析方法
        # 绑定与结果类型, 不重查接收者表达式 —— 原表达式已查过一轮,
        # 重查会把按值 self 的消费标记翻倍 (误报 used after move)。
        self._move_mark_suppressed = True
        self._synthetic_recheck = True
        try:
            self._check_call(synthetic)
        finally:
            self._move_mark_suppressed = False
            self._synthetic_recheck = False
        call.args[index].value = synthetic


    def _auto_borrow_ok(
        self: "_Analyzer", expected: str, actual: Optional[str]
    ) -> bool:
        """``&T`` 形参接收未借用的同型 ``T`` 实参时成立 (Rust 共享借用
        的自动借用; bug-64 已在 print 上落地, 此处是全调用点通用形态)。
        ``&mut T`` 形参与显式借用实参仍走 ``_compat_types`` 严格比对。"""
        if actual is None:
            return False
        prefix, wanted = _split_ref_prefix(expected)
        if prefix != "&" or wanted.startswith("&") or wanted.startswith("*"):
            return False
        got = _strip_ref(actual)
        if got is None:
            return False
        return self._compat_types(wanted, got)

    def _binding_takes_self(
        self: "_Analyzer", binding: MethodBinding
    ) -> bool:
        """Whether ``binding`` is an instance method (declares ``self``)."""
        return bool(
            binding.fn.params
            and binding.fn.params[0].name == "self"
        )

    def _binding_return(
        self: "_Analyzer", binding: MethodBinding, owner: str
    ) -> Optional[str]:
        """The (substituted) return type of a binding called statically
        without arguments binding (static-call rejection path)."""
        ret = (
            _type_str(binding.fn.return_type)
            if binding.fn.return_type is not None
            else "None"
        )
        if ret == "Self" or ret.startswith("Self<"):
            ret = _replace_self(ret, owner)
        return ret

    def _check_user_call(
        self: "_Analyzer",
        fn: FnDecl,
        call: Call,
        arg_types: list[Optional[str]],
        *,
        is_method: bool,
        owner_hint: Optional[str] = None,
        binding: Optional[MethodBinding] = None,
        expected: Optional[str] = None,
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
                    # todo-132: extern "CWind" 声明的 owner 形参名 (K, V)
                    # 与声明处 owner 泛型参数量对齐 —— 实参个数一致时
                    # 逐位配对 (``Map<K, V>::get`` vs 接收者
                    # ``Map<String, Int>``), 与用户结构体同规则。
                    targs = _split_args(target)
                    rargs = _split_args(recv)
                    if targs and len(targs) == len(rargs) == len(
                        binding.owner_params
                    ):
                        for tp, ra in zip(binding.owner_params, rargs):
                            subst.setdefault(tp, ra)
                    else:
                        for tp, ra in zip(targs, rargs):
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
            # todo-132: 接收者与 owner 声明均无实参信息时 (extern
            # "CWind" 方法绑定的 owner_struct 是裸基名), 直接按
            # owner_params 声明顺序取接收者的实参位。
            if recv is not None and binding.owner_params and not subst:
                rargs = _split_args(recv)
                if len(rargs) == len(binding.owner_params):
                    for tp, ra in zip(binding.owner_params, rargs):
                        subst.setdefault(tp, ra)
            if expected is not None and owner_hint is not None:
                # 静态泛型构造 (MaxHeap::new(10)) 没有接收者, 调用点期望
                # 类型 (如 let h: MaxHeap<String> = ...) 提供 owner 实参
                exp = self._expand_type(expected)
                if exp is not None and _base(exp) == _base(owner_hint):
                    struct = self.structs.get(_base(exp))
                    if struct is not None:
                        for p, ra in zip(
                            [p.name for p in struct.params],
                            _split_args(exp),
                        ):
                            if p in binding.owner_params and p not in subst:
                                subst[p] = ra

        if not any(a.unpack for a in call.args):
            # todo-87: 变参 extern 函数至少要求固定形参个数的实参,
            # 多余实参不与固定形参比对类型 (C vararg 语义).
            variadic = bool(getattr(fn, "variadic", False))
            count_ok = len(call.args) == len(params) or (
                variadic and len(call.args) >= len(params)
            )
            if not count_ok:
                self._record_error(
                    f"function '{fn.name}' expects "
                    f"{'at least ' if variadic else ''}{len(params)} argument(s), "
                    f"got {len(call.args)}",
                    call.line,
                    call.column,
                )
            else:
                for i, (arg, param) in enumerate(zip(call.args, params)):
                    if param.type is None:
                        continue
                    formal = _subst_type_str(_type_str(param.type), subst)
                    # bug-64: 共享借用形参 ``&T`` 接收未借用的同型实参
                    # (自动借用) —— 推断时把形参剥到被指类型再统一,
                    # 否则 ``print<T: Display>(value: &T)`` 的 T 永远
                    # 推断不出实参类型。
                    if (
                        _is_ref(formal)
                        and not formal.startswith("&mut ")
                        and not _is_ref(arg_types[i])
                    ):
                        formal = _strip_ref(formal)
                    self._unify_generic(
                        formal,
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
                    if self._auto_borrow_ok(expected, arg_types[i]):
                        # ``&T``/``&mut T`` 形参接收未借用的同型实参:
                        # Rust 自动借用, 不移动所有权 (bug-64 通用化)。
                        pass
                    elif not self._compat_types(expected, arg_types[i]):
                        # todo-54: 裸函数名实参绑定到回调签名时,
                        # 按声明的形参/返回逐段比对签名
                        if (
                            arg_types[i] == "Fn"
                            and expected.startswith("fn(")
                            and self._bare_fn_matches(expected, arg.value)
                        ):
                            pass
                        else:
                            self._record_error(
                                f"argument {i + 1} of '{fn.name}' must be "
                                f"{self._fmt_type(expected)}, got {self._fmt_type(arg_types[i])}",
                                call.line,
                                call.column,
                            )
                    # 精化值按声明形参类型检查; 字面量宽度同形
                    # (toml 时代 builtin spec 的 resolved 检查的通用
                    # 形态 —— push_back(99999) 对 Int 形参拒绝,
                    # u8.wrapping_add_signed(-20) 按 libs 声明的
                    # i8 形参放行)。
                    self._check_refined_value(expected, arg.value)
                    self._check_literal_range(expected, arg.value)
        if not any(a.unpack for a in call.args) and len(call.args) == len(params):
            owner_name = (
                _base(binding.owner_struct.name)
                if binding is not None and binding.owner_struct is not None
                else None
            )
            self._check_constructor_field_flow(fn, owner_name, params, call.args)
        # todo-164: once subst has the call's concrete generic arguments,
        # re-validate every ``T: Trait<Assoc = Type>`` bound of the callee's
        # generic parameters against them.
        self._check_call_bound_conformance(fn, subst, generic_names, call)
        # 特权退役: bound 驱动的 Display 校验 + to_string 改写对一切
        # ``T: Display`` 形参生效 (print 只是普通 extern "CWind" fn)。
        self._check_bound_argument_conformance(
            fn, call, arg_types, subst, generic_names
        )
        if not any(a.unpack for a in call.args) and len(call.args) == len(params):
            # 非 self 形参按值传入时移动所有权; self 现阶段按引用传递。
            for i, arg in enumerate(call.args):
                value = arg.value
                if isinstance(value, Name) and len(value.parts) == 1:
                    # bug-64: ``&T``/``&mut T`` 形参 (如 print<T>(value: &T))
                    # 按 Rust 自动借用语义传递引用, 不移动实参。
                    param_type = params[i].type if i < len(params) else None
                    if param_type is not None and param_type.ref:
                        continue
                    t = arg_types[i]
                    if t is not None:
                        expanded = self._expand_type(t)
                        base = _base(expanded) if expanded is not None else None
                        if base in _NUMERIC or base == "Bool":
                            continue  # 标量按值复制, 不移动
                        if (
                            expanded is not None
                            and str(expanded).startswith("fn(")
                        ):
                            # 函数指针是可调用对象的地址, Copy (Rust 风格),
                            # 高阶函数场景允许同一指针多次传递
                            continue
                    info = self._lookup(value.parts[0])
                    if t is not None:
                        expanded2 = self._expand_type(t)
                        s = str(expanded2) if expanded2 is not None else ""
                        if s.startswith("*const ") or s.startswith("*mut "):
                            continue  # 原始指针是纯地址, Copy (Rust 风格)
                    if info is not None and info.kind in ("let", "param"):
                        info.moved = True
        ret = _type_str(fn.return_type) if fn.return_type is not None else "None"
        if ret == "Self" or ret.startswith("Self<"):
            if binding is not None and binding.owner_struct is not None:
                ret = _replace_self(
                    ret,
                    _subst_type_str(_type_str(binding.owner_struct), subst),
                )
            elif owner_hint is not None:
                ret = _replace_self(ret, owner_hint)
        # 返回替换后的类型字符串 (泛型上下文中保留未解析的 opaque 叶子,
        # 不再折叠成 None, 否则调用结果的字段/方法访问会丢失类型信息)
        return _subst_type_str(ret, subst), subst

    def _resolve_generic_bound_method(
        self: "_Analyzer",
        param: str,
        member: str,
        call: "Call",
        arg_types: list[Optional[str]],
    ) -> Optional[str]:
        """bug-65: resolve ``Param::member(args)`` via the parameter's
        declared trait bounds (``U: From<T>`` makes ``U::from(x)`` a
        static call returning ``U``).

        ``member`` names a method declared by one of the bound traits;
        user-trait methods bind ``Self`` to the parameter.  Returns the
        call's type, or ``None`` (after recording a precise error) when
        nothing resolves.
        """
        for bound in self.generic_trait_bounds.get(param, ()) or []:
            trait_bare = _trait_bare(bound.name)
            trait_decl = self.traits.get(trait_bare)
            if trait_decl is None:
                continue
            for m in trait_decl.methods:
                if m.name != member:
                    continue
                params = m.params
                if params and params[0].name == "self":
                    # Instance methods need a receiver; the qualified form
                    # is handled at the receiver call site instead.
                    continue
                if len(arg_types) != len(params):
                    self._record_error(
                        f"'{param}::{member}' (bound '{trait_bare}') expects "
                        f"{len(params)} argument(s), got {len(arg_types)}",
                        call.line,
                        call.column,
                    )
                    return None
                for i, p in enumerate(params):
                    if p.type is None:
                        continue
                    want = _type_str(p.type)
                    if want == "Self":
                        want = param
                    if want in self.active_generics:
                        continue
                    if not self._compat_types(want, arg_types[i]):
                        self._record_error(
                            f"argument {i + 1} of '{param}::{member}' must "
                            f"be {self._fmt_type(want)}, got "
                            f"{self._fmt_type(arg_types[i])}",
                            call.line,
                            call.column,
                        )
                ret = (
                    _type_str(m.return_type)
                    if m.return_type is not None
                    else "None"
                )
                if ret == "Self" or ret.startswith("Self<"):
                    ret = _replace_self(ret, param)
                callee = call.callee
                callee._typed_ann["binding"] = {
                    "kind": "trait_fn",
                    "ref": trait_bare,
                    "param": param,
                    "member": member,
                }
                self._ann_type(callee, "Fn")
                self._ann_call(call, "trait_fn", param)
                return ret
        self._record_error(
            f"generic parameter '{param}' has no trait bound providing "
            f"'{member}'",
            call.line,
            call.column,
        )
        return None

    def _find_trait_method_decl(
        self: "_Analyzer", trait_bare: str, member: str
    ) -> Optional[FnDecl]:
        """bug-68: the ``member`` method declared by ``trait_bare`` or,
        transitively, by one of its supertraits (a cycle-guarded walk)."""
        seen: set[str] = set()
        frontier = [trait_bare]
        while frontier:
            name = frontier.pop()
            if name in seen:
                continue
            seen.add(name)
            decl = self.traits.get(name)
            if decl is None:
                continue
            for m in decl.methods:
                if m.name == member:
                    return m
            frontier.extend(
                st
                for st in (
                    _trait_bare(s.name) for s in decl.supertraits
                )
                if st
            )
        return None

    def _check_trait_self_method(
        self: "_Analyzer",
        callee: Attribute,
        call: Call,
        arg_types: list[Optional[str]],
        expected: Optional[str],
    ) -> Optional[str]:
        """bug-68: a method call whose receiver type is the enclosing
        trait (``self.write(...)`` in a trait default-method body).

        Rust semantics: ``Self: Trait`` inside a default body, so every
        instance method of the trait — required or defaulted, inherited
        from supertraits included — is callable on ``self``.  The call is
        checked against the trait declaration and annotated with the
        ``trait_fn`` member kind (same discipline as bug-65's bound
        methods; codegen of default bodies is todo-166/169 scope)."""
        trait_bare = self.current_trait
        assert trait_bare is not None
        m = self._find_trait_method_decl(trait_bare, callee.name)
        if m is None:
            self._record_error(
                f"trait '{trait_bare}' has no method '{callee.name}'",
                call.line,
                call.column,
            )
            return None
        params = m.params
        if params and params[0].name == "self":
            params = params[1:]
        if len(arg_types) != len(params):
            self._record_error(
                f"method '{callee.name}' of trait '{trait_bare}' expects "
                f"{len(params)} argument(s), got {len(arg_types)}",
                call.line,
                call.column,
            )
            return None
        for i, p in enumerate(params):
            if p.type is None:
                continue
            want = _type_str(p.type)
            if "Self" in want:
                want = _replace_self(want, trait_bare)
            if want is None or want in self.active_generics:
                continue
            if not self._compat_types(want, arg_types[i]):
                self._record_error(
                    f"argument {i + 1} of '{callee.name}' must be "
                    f"{self._fmt_type(want)}, got "
                    f"{self._fmt_type(arg_types[i])}",
                    call.line,
                    call.column,
                )
        ret = (
            _type_str(m.return_type)
            if m.return_type is not None
            else "None"
        )
        if "Self" in ret:
            ret = _replace_self(ret, trait_bare)
        callee._typed_ann["member"] = {
            "kind": "trait_fn", "ref": trait_bare, "member": callee.name,
        }
        self._ann_type(callee, "Fn")
        self._ann_call(call, "trait_fn", trait_bare)
        return ret
