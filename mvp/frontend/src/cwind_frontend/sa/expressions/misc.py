"""Expression mixin: closures, into-desugar, display/print and receiver-move helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from dataclasses import fields as _fields

from .defs import _fn_type_string

from ..builtin_methods import BUILTIN_TYPE_METHODS

from ..symbols import (
    MethodBinding,
    VarInfo,
    _find_method,
)

from ..types import (
    _base,
    _is_ref,
    _split_args,
    _split_fn_sig,
    _strip_ref,
    _subst_type_str,
    _type_info,
    _type_mentions,
    _type_str,
)

from ...ast_components.ast import (
    Arg,
    Assign,
    Attribute,
    Call,
    Index,
    Name,
    Node,
    Closure,
    UnaryOp,
)

if TYPE_CHECKING:
    from ..analyzer import _Analyzer


class ExprMisc:

    def _check_closure(
        self: "_Analyzer", closure: Closure, expected: Optional[str] = None
    ) -> Optional[str]:
        """Check a closure against an optional function-pointer type.

        v0 closures capture nothing; the environment is therefore empty and
        the callable value can be represented by the same ABI as ``fn``.
        """
        self._push_scope()
        param_types: list[Optional[str]] = []
        for p in closure.params:
            if p.type is None:
                self._record_error(
                    "closure parameter requires a type annotation",
                    p.line,
                    p.column,
                )
                ptype = None
            else:
                self._check_type(p.type, p)
                self._annotate_type_node(p.type)
                ptype = _type_str(p.type)
            param_types.append(ptype)
            self._declare(VarInfo(
                p.name,
                ptype,
                p.line,
                p.column,
                "param",
                mutable=p.mutable,
                node=p,
            ))
            self._ann_type(p, ptype)
        ret = (
            _type_str(closure.return_type)
            if closure.return_type is not None else None
        )
        if closure.return_type is not None:
            self._check_type(closure.return_type, closure)
            self._annotate_type_node(closure.return_type)
        body_ret = self._check_block_with_return(
            closure.body,
            ret or "None",
            infer=bool(closure.return_type is None),
        )
        if ret is None:
            ret = body_ret
        if expected is not None:
            actual = _fn_type_string(param_types, ret or "None")
            if not self._compat_types(expected, actual):
                self._record_error(
                    "closure has type "
                    f"{self._fmt_type(actual)}, expected "
                    f"{self._fmt_type(expected)}",
                    closure.line,
                    closure.column,
                )
        closure._typed_ann["fn_params"] = [
            _type_info(self._expand_type(t), self._opaque_names())
            for t in param_types
        ]
        closure._typed_ann["fn_return"] = _type_info(
            self._expand_type(ret or "None"), self._opaque_names()
        )
        self._ann_type(closure, _fn_type_string(param_types, ret or "None"))
        self._pop_scope()
        return _fn_type_string(param_types, ret or "None")

    def _assign_synthetic_ids(self: "_Analyzer", node: Node) -> None:
        """Assign dense typed-AST ids to nodes created after the main walk
        without renumbering nodes that already carry an id."""
        if node._typed_id is not None:
            return
        node._typed_id = self._next_node_id
        self._next_node_id += 1
        for f in _fields(node):
            if f.name in ("line", "column"):
                continue
            value = getattr(node, f.name)
            if isinstance(value, Node):
                self._assign_synthetic_ids(value)
            elif isinstance(value, list):
                for v in value:
                    if isinstance(v, Node):
                        self._assign_synthetic_ids(v)

    def _user_display_binding(
        self: "_Analyzer", recv: Optional[str]
    ) -> Optional[MethodBinding]:
        if recv is None:
            return None
        base = _base(self._expand_type(recv))
        binding = _find_method(self.methods.get(base, []), "to_string")
        if binding is not None and binding.trait == "Display":
            return binding
        return None

    def _print_arg_has_display(self: "_Analyzer", t: Optional[str]) -> bool:
        if t is None:
            return True
        if any(_type_mentions(t, name) for name in self.active_generics):
            return True  # 泛型实例化后由具体类型决定
        expanded = self._expand_type(t)
        base = _base(expanded) if expanded is not None else None
        if base is None or base == "Any" or base == "Fn":
            return True
        methods = BUILTIN_TYPE_METHODS.get(base)
        if methods is not None and "to_string" in methods:
            return True
        return self._user_display_binding(t) is not None

    def _rewrite_print_arg(
        self: "_Analyzer", call: Call, arg_type: Optional[str]
    ) -> None:
        """Turn ``print(x)`` on a user Display type into ``print(x.to_string())``
        so the frontend and backend both go through Display::to_string."""
        if not call.args or self._user_display_binding(arg_type) is None:
            return
        original = call.args[0].value
        attr = Attribute(original.line, original.column, original, "to_string")
        synthetic = Call(original.line, original.column, attr, [])
        self._assign_synthetic_ids(synthetic)
        self._check_call(synthetic)
        call.args[0].value = synthetic

    def _generic_into_target(
        self: "_Analyzer", recv: Optional[str]
    ) -> Optional[str]:
        """``Into<Target>`` bound target when ``recv`` is exactly a generic
        parameter carrying that bound (bug-21).

        Only a bare (optionally referenced) parameter qualifies: a container
        mentioning the parameter (e.g. ``Vector<T>``) is not itself ``Into``
        of anything, matching Rust semantics.
        """
        if recv is None or not self.generic_bounds:
            return None
        expanded = self._expand_type(recv)
        if expanded is None:
            return None
        base = _base(expanded)
        target = self.generic_bounds.get(base)
        if target is None or base not in self.active_generics:
            return None
        return self._expand_type(target)

    def _desugar_user_into(
        self: "_Analyzer",
        call: Call,
        recv: Optional[str],
        expected: Optional[str],
    ) -> Optional[str]:
        """Desugar a user ``From`` conversion ``x.into()`` to
        ``Target::from(x)`` so the derived into is implemented by the
        user-written from method."""
        if call.args:
            self._record_error(
                "'into()' derived from 'From' takes no arguments",
                call.line,
                call.column,
            )
            return None
        targets = self.conversions.get(recv, []) if recv is not None else []
        if not targets and recv is not None:
            for src, ts in self.conversions.items():
                if src == recv or _base(src) == _base(recv):
                    targets.extend(ts)
        if len(targets) > 1 and expected is not None:
            exp = self._expand_type(expected)
            if exp is not None:
                filtered = [
                    t for t in targets
                    if _base(self._expand_type(t)) == _base(exp)
                ]
                if filtered:
                    targets = filtered
        if not targets:
            # bug-21: 裸泛型参数接收者没有命中任何 ``Into<Target>`` 约束时,
            # 指出缺约束本身, 而不是误导用户去实现 From。
            expanded = self._expand_type(recv) if recv is not None else None
            recv_base = _base(expanded) if expanded is not None else None
            if (
                recv_base is not None
                and recv_base in self.active_generics
                and recv_base not in self.generic_bounds
            ):
                self._record_error(
                    f"generic parameter '{recv_base}' has no "
                    f"'Into<...>' bound; declare it as '{recv_base}: "
                    "Into<Target>' to call 'into()' on it",
                    call.line,
                    call.column,
                )
                return None
            self._record_error(
                f"no conversion from {self._fmt_type(recv)} via "
                "'into()' (implement 'impl From<...> for ...')",
                call.line,
                call.column,
            )
            return None
        if len(targets) > 1:
            self._record_error(
                f"ambiguous into() conversion for {self._fmt_type(recv)}",
                call.line,
                call.column,
            )
            return None
        target = targets[0]
        target_base = _base(self._expand_type(target))
        callee = Name(call.line, call.column, [target_base, "from"])
        source = call.callee.obj if isinstance(call.callee, Attribute) else None
        if source is None:
            self._record_error(
                "'into()' must be called on a value",
                call.line,
                call.column,
            )
            return None
        arg = Arg(source.line, source.column, source)
        call.callee = callee
        call.args = [arg]
        self._assign_synthetic_ids(callee)
        self._assign_synthetic_ids(arg)
        return self._check_call(call, expected)

    def _method_self_is_ref(
        self: "_Analyzer", binding: MethodBinding
    ) -> bool:
        fn = binding.fn
        return bool(
            fn.params
            and fn.params[0].name == "self"
            and fn.params[0].type is not None
            and fn.params[0].type.ref
        )

    def _method_takes_mut_self(
        self: "_Analyzer", binding: MethodBinding
    ) -> bool:
        """bug-50: whether the method's receiver is ``&mut self``."""
        fn = binding.fn
        return bool(
            fn.params
            and fn.params[0].name == "self"
            and fn.params[0].type is not None
            and fn.params[0].type.ref
            and fn.params[0].mutable
        )

    def _receiver_is_mutable_place(
        self: "_Analyzer", node: Node
    ) -> bool:
        """bug-50: whether ``node`` denotes a place that may be borrowed
        ``&mut`` (Rust auto-ref rules).

        Local bindings must be declared ``mut``; consts are immutable;
        field/index chains inherit the mutability of their base binding;
        anything else (call results, literals, ...) is a mutable temporary.
        Unknown bindings stay permissive.  An explicit ``&mut expr`` receiver
        was already mutability-checked by ``_check_expr`` (bug-46), so it is
        accepted here to avoid a double diagnostic.
        """
        if isinstance(node, UnaryOp):
            if node.mutable:
                return True
            return self._receiver_is_mutable_place(node.operand)
        if isinstance(node, Name):
            if len(node.parts) == 1:
                info = self._lookup(node.parts[0])
                if info is not None:
                    if info.kind == "const":
                        return False
                    if info.kind in ("let", "param"):
                        return bool(info.mutable)
            return True
        if isinstance(node, (Attribute, Index)):
            return self._receiver_is_mutable_place(node.obj)
        return True

    def _method_consumes_self(
        self: "_Analyzer", binding: MethodBinding
    ) -> bool:
        """Whether calling ``binding`` moves its receiver.

        bug-44: a self-less associated function (``Type::new()``) takes no
        receiver at all, so it cannot consume one.  A bare ``self``
        parameter carries no type annotation and is by-value.
        """
        fn = binding.fn
        if not fn.params or fn.params[0].name != "self":
            return False
        return not self._method_self_is_ref(binding)

    def _mark_receiver_moved(
        self: "_Analyzer",
        binding: MethodBinding,
        receiver: Optional[Node],
    ) -> None:
        if not self._method_consumes_self(binding):
            return
        if isinstance(receiver, Name) and len(receiver.parts) == 1:
            info = self._lookup(receiver.parts[0])
            if info is not None and info.kind in ("let", "param"):
                info.moved = True

    def _mark_implicit_self_moved(
        self: "_Analyzer", binding: MethodBinding
    ) -> None:
        if not self._method_consumes_self(binding):
            return
        info = self._lookup("self")
        if info is not None and info.kind == "param":
            info.moved = True

    def _resolve_use_type(
        self: "_Analyzer",
        t: str,
        subst: dict[str, str],
        generic_names: set[str],
    ) -> Optional[str]:
        """Substitute generic parameters; return ``None`` when the result
        still depends on a type variable and cannot be checked concretely."""
        resolved = _subst_type_str(t, subst)
        variables = generic_names | set(self.active_generics)
        if any(_type_mentions(resolved, name) for name in variables):
            return None
        return resolved

    def _bare_fn_matches(
        self: "_Analyzer", sig: str, value_node: Node
    ) -> bool:
        """Whether the bare function named by ``value_node`` matches the
        flattened callback signature ``sig`` segment-by-segment (todo-54).

        bug-58: both sides expand type aliases before comparing -- the
        callback signature may name prelude/ctypedef aliases (``c_uint``,
        ``*mut c_void``) while the candidate fn's declaration spells the
        underlying builtin (or vice versa); the raw strings would differ
        even though the types are identical.
        """
        binding = getattr(value_node, "_typed_ann", {}).get("binding")
        if not binding or binding.get("kind") != "fn":
            return False
        ref = binding.get("ref")
        fn = next(
            (f for f in self.functions.values() if f._typed_id == ref),
            None,
        )
        if fn is None:
            return False
        params, ret = _split_fn_sig(sig)
        decl_params = [
            _type_str(p.type) if p.type is not None else None
            for p in fn.params
        ]
        if len(params) != len(decl_params):
            return False
        for d, p in zip(decl_params, params):
            if d is None:
                return False
            # bug-58: 段位先展开别名再比对, 但仍要求**同型一致** --
            # C 回调 ABI 按段位宽度取值, Int32 与 Int64 段宽不同即失配
            # (todo-54 原本的逐段严格语义, 仅放宽拼写维度)。
            if self._expand_type(p, _deep=True) != self._expand_type(
                d, _deep=True
            ):
                return False
        decl_ret = (
            _type_str(fn.return_type) if fn.return_type is not None
            else "None"
        )
        want_ret = ret if ret is not None else "None"
        return self._expand_type(
            want_ret, _deep=True
        ) == self._expand_type(decl_ret, _deep=True)

    def _unify_generic(
        self: "_Analyzer",
        expected: str,
        actual: Optional[str],
        subst: dict[str, str],
        generic_names: set[str],
    ) -> None:
        """Infer generic parameters by structurally matching an expected
        argument type against the actual one (e.g. ``Vector<T>`` against
        ``Vector<Int>`` infers ``T = Int``)."""
        if actual is None:
            return
        if _is_ref(expected) != _is_ref(actual):
            return
        expected = _strip_ref(expected)
        actual = _strip_ref(actual)
        if expected is None or actual is None:
            return
        if expected in generic_names:
            if expected not in subst:
                subst[expected] = actual
            return
        if expected == actual:
            return
        e_args = _split_args(expected)
        a_args = _split_args(actual)
        if (
            e_args
            and a_args
            and len(e_args) == len(a_args)
            and _base(expected) == _base(actual)
        ):
            for e, a in zip(e_args, a_args):
                self._unify_generic(e, a, subst, generic_names)
