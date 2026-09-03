"""Expression mixin: call dispatch, user/builtin callee checks and generic-argument unification."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .defs import _parse_fn_signature

from ..builtin_methods import (
    BUILTIN_MODULE_FUNCTIONS,
    BUILTIN_TYPE_METHODS,
    MethodSpec,
)

from ..const_fold import (
    _match_arg_patterns,
    _patterns_arity_text,
)

from ..symbols import (
    MethodBinding,
    _find_method,
)

from ..types import (
    _INTEGER,
    _NUMERIC,
    _bare_type,
    _base,
    _generic_arg,
    _generic_ref_index,
    _replace_self,
    _split_args,
    _subst_type_str,
    _type_info,
    _type_mentions,
    _type_str,
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
                    or base in BUILTIN_MODULE_FUNCTIONS
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
                if n in BUILTIN_MODULE_FUNCTIONS:
                    if n == "print":
                        if not call.args:
                            self._record_error(
                                "print expects 1 argument",
                                call.line,
                                call.column,
                            )
                        elif not self._print_arg_has_display(arg_types[0]):
                            self._record_error(
                                f"type {self._fmt_type(arg_types[0])} "
                                "does not implement 'Display::to_string', "
                                "required by 'builtins::print'",
                                call.args[0].line,
                                call.args[0].column,
                            )
                        else:
                            self._rewrite_print_arg(call, arg_types[0])
                    self._check_builtin_call(n, call, arg_types)
                    callee._typed_ann["binding"] = {
                        "kind": "builtin", "ref": n
                    }
                    self._ann_type(callee, "Fn")
                    self._ann_call(call, "builtin", n)
                    return self._resolve_return(
                        BUILTIN_MODULE_FUNCTIONS[n].returns, None
                    ) or "None"
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
                    or base in BUILTIN_TYPE_METHODS
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
                if mod == "builtins":
                    if member in BUILTIN_MODULE_FUNCTIONS:
                        if member == "print":
                            if not call.args:
                                self._record_error(
                                    "print expects 1 argument",
                                    call.line,
                                    call.column,
                                )
                            elif not self._print_arg_has_display(arg_types[0]):
                                self._record_error(
                                    f"type {self._fmt_type(arg_types[0])} "
                                    "does not implement 'Display::to_string', "
                                    "required by 'builtins::print'",
                                    call.args[0].line,
                                    call.args[0].column,
                                )
                            else:
                                self._rewrite_print_arg(call, arg_types[0])
                        self._check_builtin_call(member, call, arg_types)
                        callee._typed_ann["binding"] = {
                            "kind": "builtin", "ref": member
                        }
                        self._ann_type(callee, "Fn")
                        self._ann_call(call, "builtin", member)
                        return self._resolve_return(
                            BUILTIN_MODULE_FUNCTIONS[member].returns, None
                        ) or "None"
                    self._record_error(
                        f"unknown builtins:: function '{member}'",
                        call.line,
                        call.column,
                    )
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
                            enum, variant, call, arg_types
                        )
                # bug-43: the method table is keyed by the expanded owner
                # type (aliases in impl/extra targets are canonicalized),
                # so resolve the alias before the lookup (mirrors the
                # builtin lookup below).  todo-154: ``mod`` is also
                # canonicalized to the expanded bare owner so ``Self``
                # return positions (``Vec::new() -> Self``) bind to
                # ``Vector``, not the alias spelling.
                mod_canon = _base(self._expand_type(mod) or mod) or mod
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
                    return result
                builtin = BUILTIN_TYPE_METHODS.get(mod_canon)
                if builtin is not None:
                    spec = builtin.get(member)
                    if spec is not None:
                        if spec.args and spec.args[0] == "Self":
                            self._record_error(
                                f"instance method '{member}' of '{mod_canon}' must "
                                "be called on a value",
                                call.line,
                                call.column,
                            )
                            callee._typed_ann["binding"] = {
                                "kind": "builtin", "ref": member
                            }
                            self._ann_type(callee, "Fn")
                            self._ann_call(call, "builtin", member)
                            return self._resolve_return(spec.returns, mod_canon)
                        self._check_spec_args(member, spec, call, arg_types, None)
                        callee._typed_ann["binding"] = {
                            "kind": "builtin", "ref": member
                        }
                        self._ann_type(callee, "Fn")
                        self._ann_call(call, "builtin", member)
                        return self._resolve_return(spec.returns, mod_canon)
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
                    enum, variant, call, arg_types
                )
            self._record_error("unsupported call target", call.line, call.column)
            return None
        if isinstance(callee, Attribute):
            recv = self._expand_type(self._check_expr(callee.obj))
            if recv is None:
                return None
            base = _base(recv)
            binding = _find_method(self.methods.get(base, []), callee.name)
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
                if not self._method_self_is_ref(binding) and recv.startswith("&"):
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
                self._mark_receiver_moved(binding, callee.obj)
                callee._typed_ann["member"] = {
                    "kind": "method", "ref": binding.id
                }
                self._ann_type(callee, result)
                self._ann_call(call, "method", binding.id, subst)
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
                # 内置方向性 trait (``Into<T>`` / ``From<T>`` 附带的 into)
                # 优先于用户声明转换: 例如 [types.String] traits = ["Into<UInt>"]
                # 让 ``s.into()`` 直接解析为 UInt。
                methods = BUILTIN_TYPE_METHODS.get(base)
                spec = methods.get("into") if methods is not None else None
                if spec is not None:
                    self._check_spec_args("into", spec, call, arg_types, recv)
                    if spec.returns == "Context":
                        # String 的 into(): 目标类型由调用处期望类型决定
                        # (Rust 风格推断), 例如 `let n: UInt = s.into();`。
                        if expected is None:
                            self._record_error(
                                "into() needs a target type (use "
                                "'T::from(value)' or bind to a typed "
                                "let/return)",
                                call.line,
                                call.column,
                            )
                            return None
                        target = self._expand_type(expected)
                        target_base = (
                            _base(target) if target is not None else None
                        )
                        tm = (
                            BUILTIN_TYPE_METHODS.get(target_base)
                            if target_base is not None else None
                        )
                        fs = tm.get("from") if tm is not None else None
                        builtin_ok = (
                            fs is not None
                            and fs.args
                            and fs.args[0] == recv
                        )
                        user_ok = target in self.conversions.get(recv, [])
                        if not builtin_ok and not user_ok:
                            self._record_error(
                                f"no conversion from "
                                f"{self._fmt_type(recv)} to "
                                f"{self._fmt_type(target)} via 'into()'",
                                call.line,
                                call.column,
                            )
                            return None
                        callee._typed_ann["member"] = {
                            "kind": "builtin", "ref": "into"
                        }
                        self._ann_type(callee, target)
                        self._ann_call(call, "builtin", "into")
                        return target
                    callee._typed_ann["member"] = {
                        "kind": "builtin", "ref": "into"
                    }
                    resolved = self._resolve_return(spec.returns, recv)
                    self._ann_type(callee, resolved)
                    self._ann_call(call, "builtin", "into")
                    return resolved
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
            methods = BUILTIN_TYPE_METHODS.get(base)
            if methods is not None:
                spec = methods.get(callee.name)
                if spec is not None:
                    if (
                        callee.name == "format"
                        and isinstance(callee.obj, StrLit)
                    ):
                        # 前端不解析模板 (那是后端栈机的工作), 只做最基本的
                        # 花括号配平检查, 让明显写坏的模板尽早报错。
                        self._check_format_braces(callee.obj, call.args)
                    self._check_spec_args(callee.name, spec, call, arg_types, recv)
                    callee._typed_ann["member"] = {
                        "kind": "builtin", "ref": callee.name
                    }
                    self._ann_type(callee, self._resolve_return(spec.returns, recv))
                    self._ann_call(call, "builtin", callee.name)
                    return self._resolve_return(spec.returns, recv)
                self._record_error(
                    f"type '{base}' has no method '{callee.name}'",
                    call.line,
                    call.column,
                )
                return None
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
    ) -> Optional[str]:
        """Check ``Enum::Variant(args)`` construction and infer the enum's
        generic arguments from the payload values."""
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
        payload_types: list[Optional[str]] = []
        for i, (f, arg) in enumerate(zip(variant.fields, call.args)):
            ft = _subst_type_str(_type_str(f), subst)
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
                want = _bare_type(want) or want
                got = _bare_type(got) or got
                if got in generic_names:
                    continue  # generic assoc value defers
                if want != got:
                    self._record_error(
                        f"type '{base}' implements '{_bare_type(bound.name) or bound.name}' with "
                        f"'{b.name} = {got}', but this call requires "
                        f"'{b.name} = {want}'",
                        call.line,
                        call.column,
                    )

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
                    for tp, ra in zip(_split_args(target), _split_args(recv)):
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
                    self._unify_generic(
                        _subst_type_str(_type_str(param.type), subst),
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
                    if not self._compat_types(expected, arg_types[i]):
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
                    self._check_refined_value(expected, arg.value)
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
        if not any(a.unpack for a in call.args) and len(call.args) == len(params):
            # 非 self 形参按值传入时移动所有权; self 现阶段按引用传递。
            for i, arg in enumerate(call.args):
                value = arg.value
                if isinstance(value, Name) and len(value.parts) == 1:
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

    def _check_builtin_call(
        self: "_Analyzer",
        name: str,
        call: Call,
        arg_types: list[Optional[str]],
    ) -> str:
        spec = BUILTIN_MODULE_FUNCTIONS[name]
        self._check_spec_args(name, spec, call, arg_types, None)
        return self._resolve_return(spec.returns, None) or "None"

    def _check_spec_args(
        self: "_Analyzer",
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
        patterns = spec.patterns
        if patterns and patterns[0] == (1, "Self"):
            patterns = patterns[1:]
        expected = _match_arg_patterns(patterns, len(call.args))
        if expected is None:
            self._record_error(
                f"'{name}' expects {_patterns_arity_text(patterns)}, "
                f"got {len(call.args)}",
                call.line,
                call.column,
            )
            return
        for i, (arg, want) in enumerate(zip(call.args, expected)):
            if self._arg_matches(want, arg_types[i], receiver):
                # Foldable arguments are checked against the refined type the
                # built-in signature resolves to (e.g. `SameAsGeneric:1` on
                # `Vector<Test1>` is `Test1`), so `push_back(101)` is rejected
                # when `Test1` requires `self < 100`.
                resolved = self._resolve_expected(want, receiver)
                if resolved is not None:
                    self._check_refined_value(resolved, arg.value)
                    self._check_literal_range(resolved, arg.value)
            else:
                resolved = self._resolve_expected(want, receiver)
                expected_text = (
                    self._fmt_type(resolved) if resolved is not None else want
                )
                self._record_error(
                    f"argument {i + 1} of '{name}' must be {expected_text}, "
                    f"got {self._fmt_type(arg_types[i])}",
                    call.line,
                    call.column,
                )

    def _arg_matches(
        self: "_Analyzer",
        expected: str,
        actual: Optional[str],
        receiver: Optional[str],
    ) -> bool:
        if actual is None:
            return True
        if expected in ("Whatever", "Any"):
            return True
        if expected in ("Self", "SameTypeOther"):
            return receiver is None or self._compat_types(receiver, actual)
        if expected == "SameAsGeneric" or expected.startswith("SameAsGeneric:"):
            elem = _generic_arg(
                self._expand_type(receiver), _generic_ref_index(expected)
            )
            return elem is None or self._compat_types(elem, actual)
        if expected == "AnyInt":
            expanded = self._expand_type(actual)
            return expanded is not None and _base(expanded) in _INTEGER
        if expected == "EveryNumber":
            expanded = self._expand_type(actual)
            return expanded is not None and _base(expanded) in _NUMERIC
        if expected == "AnyGeneric":
            expanded = self._expand_type(actual)
            return expanded is not None and "<" in expanded
        return self._compat_types(expected, actual)

    def _resolve_return(self: "_Analyzer", ret: str, receiver: Optional[str]) -> Optional[str]:
        return self._resolve_type_ref(ret, receiver)

    def _resolve_expected(
        self: "_Analyzer", expected: str, receiver: Optional[str]
    ) -> Optional[str]:
        """Resolve dynamic placeholders to the concrete type they stand for."""
        return self._resolve_type_ref(expected, receiver)

    def _resolve_type_ref(
        self: "_Analyzer", t: str, receiver: Optional[str]
    ) -> Optional[str]:
        """Resolve placeholders in a type string, including inside generic
        arguments (e.g. ``Tuple<SameAsGeneric:1, SameAsGeneric:2>``)."""
        if t in ("Self", "SameTypeOther"):
            return receiver
        if t == "SameAsGeneric" or t.startswith("SameAsGeneric:"):
            return _generic_arg(
                self._expand_type(receiver), _generic_ref_index(t)
            )
        args = _split_args(t)
        if not args:
            return t
        resolved = [self._resolve_type_ref(a, receiver) for a in args]
        if any(r is None for r in resolved):
            return None
        return f"{_base(t)}<{', '.join(resolved)}>"
