"""Expression, call and built-in member checks."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .builtin_methods import (
    BUILTIN_MODULE_FUNCTIONS,
    BUILTIN_OBJECTS,
    BUILTIN_TYPE_METHODS,
    MethodSpec,
)
from .const_fold import _match_arg_patterns, _patterns_arity_text
from .symbols import MethodBinding, _find_method
from .types import (
    _INTEGER,
    _NUMERIC,
    _base,
    _common_numeric,
    _common_type,
    _generic_arg,
    _generic_ref_index,
    _split_args,
    _subst_type_str,
    _type_info,
    _type_mentions,
    _type_str,
)
from ..ast_components.ast import (
    Arg,
    Assign,
    Attribute,
    BinOp,
    BoolLit,
    Call,
    Field,
    FloatLit,
    FnDecl,
    Index,
    IntLit,
    MapLit,
    Name,
    Node,
    Slice,
    StrLit,
    StructConstruct,
    TupleLit,
    UnaryOp,
    VectorLit,
)
from ..ast_components.token import TokenKind

if TYPE_CHECKING:
    from .analyzer import _Analyzer


_RELATIONAL: frozenset[TokenKind] = frozenset({TokenKind.LT, TokenKind.GT, TokenKind.LE, TokenKind.GE})


_EQUALITY: frozenset[TokenKind] = frozenset({
    TokenKind.EQ, TokenKind.ADDR_EQ, TokenKind.NE, TokenKind.NOT_LT, TokenKind.NOT_GT,
})


_BITWISE: frozenset[TokenKind] = frozenset({
    TokenKind.AMP, TokenKind.PIPE, TokenKind.CARET, TokenKind.SHL, TokenKind.SHR,
})


class ExpressionChecks:

    def _check_expr(self: "_Analyzer", expr: Node) -> Optional[str]:
        def resolve_index(ep: Index):
            rec = self._check_expr(ep.obj)
            it = self._check_expr(ep.index)
            expanded = self._expand_type(rec) if rec is not None else None
            if expanded is not None and _base(expanded) == "Tuple":
                t = self._tuple_indexed_type(expanded, ep.index, ep)
            else:
                t = self._indexed_type(rec)
            self._ann_type(ep, t)
            if rec is not None:
                ep._typed_ann["container_type"] = _type_info(
                    self._expand_type(rec), self._opaque_names()
                )
            if it is not None:
                ep._typed_ann["index_type"] = _type_info(
                    self._expand_type(it), self._opaque_names()
                )
            return t

        def resolve_slice(ep: Slice):
            rec = self._check_expr(ep.obj)
            for p in (ep.start, ep.stop, ep.step):
                if p is not None:
                    self._check_expr(p)
            self._ann_type(ep, rec)
            if rec is not None:
                ep._typed_ann["container_type"] = _type_info(
                    self._expand_type(rec), self._opaque_names()
                )
            return rec

        base_map = {
            IntLit: "Int",
            BoolLit: "Bool",
            FloatLit: "Float",
            StrLit: "String",
        }

        typo = type(expr)
        if typo in base_map:
            t = base_map[typo]
            self._ann_type(expr, t)
            return t
        if isinstance(expr, Name):
            return self._check_name(expr)
        if isinstance(expr, Attribute):
            return self._check_member(self._check_expr(expr.obj), expr.name, expr)
        if isinstance(expr, Call):
            return self._check_call(expr)
        if isinstance(expr, Index):
            return resolve_index(expr)
        if isinstance(expr, Slice):
            return resolve_slice(expr)

        if isinstance(expr, UnaryOp):
            operand = self._check_expr(expr.operand)
            if operand is not None:
                expr._typed_ann["operand_type"] = _type_info(
                    self._expand_type(operand), self._opaque_names()
                )
            if expr.op == TokenKind.NOT:
                if operand is not None and not self._compat_types("Bool", operand):
                    self._record_error(
                        f"'!' requires a Bool operand, got {self._fmt_type(operand)}",
                        expr.line,
                        expr.column,
                    )
                self._ann_type(expr, "Bool")
                return "Bool"
            if expr.op in (TokenKind.MINUS, TokenKind.PLUS):
                expanded = self._expand_type(operand) if operand is not None else None
                if expanded is not None and _base(expanded) not in _NUMERIC:
                    self._record_error(
                        f"unary '{expr.op.value}' requires a numeric operand, "
                        f"got {self._fmt_type(operand)}",
                        expr.line,
                        expr.column,
                    )
                result = expanded if expanded is not None else "Int"
                self._ann_type(expr, result)
                return result
            return None
        if isinstance(expr, BinOp):
            left = self._check_expr(expr.left)
            right = self._check_expr(expr.right)
            result = self._check_binop(expr.op, left, right, expr)
            if left is not None:
                expr._typed_ann["left_type"] = _type_info(
                    self._expand_type(left), self._opaque_names()
                )
            if right is not None:
                expr._typed_ann["right_type"] = _type_info(
                    self._expand_type(right), self._opaque_names()
                )
            self._ann_type(expr, result)
            return result
        if isinstance(expr, Assign):
            if (
                isinstance(expr.target, Name)
                and len(expr.target.parts) == 1
                and expr.op == TokenKind.ASSIGN
            ):
                info = self._lookup(expr.target.parts[0])
                if info is not None and info.kind == "let":
                    info.initialized = True
                    info.folded = self._fold_expr(expr.value)
            elif (
                isinstance(expr.target, Name)
                and len(expr.target.parts) == 1
                and expr.op != TokenKind.ASSIGN
            ):
                info = self._lookup(expr.target.parts[0])
                if info is not None and info.kind == "let":
                    info.folded = None
            target = self._check_expr(expr.target)
            value = self._check_expr(expr.value)
            if not self._compat_types(target, value):
                self._record_error(
                    f"cannot assign {self._fmt_type(value)} to {self._fmt_type(target)}",
                    expr.line,
                    expr.column,
                )
            self._check_literal_range(target, expr.value)
            field_node: Optional[Field] = None
            if (
                isinstance(expr.target, Attribute)
                and isinstance(expr.target.obj, Name)
                and len(expr.target.obj.parts) == 1
            ):
                info = self._lookup(expr.target.obj.parts[0])
                if info is not None:
                    struct = self.structs.get(
                        _base(self._expand_type(info.type))
                    )
                    if struct is not None:
                        field_node = next(
                            (f for f in struct.fields
                             if f.name == expr.target.name),
                            None,
                        )
            self._check_refined_value(target, expr.value, field_node)
            if isinstance(expr.target, Name) and len(expr.target.parts) == 1:
                info = self._lookup(expr.target.parts[0])
                if info is not None and info.kind == "const":
                    self._record_error(
                        f"cannot assign to const '{expr.target.parts[0]}'",
                        expr.line,
                        expr.column,
                    )
            if target is not None:
                expr._typed_ann["target_type"] = _type_info(
                    self._expand_type(target), self._opaque_names()
                )
            if value is not None:
                expr._typed_ann["value_type"] = _type_info(
                    self._expand_type(value), self._opaque_names()
                )
            self._ann_type(expr, value)
            return value
        if isinstance(expr, VectorLit):
            elems = [self._check_expr(e) for e in expr.elems]
            elem = _common_type(elems)
            result = f"Vector<{elem}>" if elem is not None else "Vector"
            self._ann_type(expr, result)
            if elem is not None:
                expr._typed_ann["element_type"] = _type_info(
                    self._expand_type(elem), self._opaque_names()
                )
            return result
        if isinstance(expr, MapLit):
            key_types: list[Optional[str]] = []
            value_types: list[Optional[str]] = []
            for e in expr.entries:
                k = self._check_expr(e.key)
                v = self._check_expr(e.value)
                key_types.append(k)
                value_types.append(v)
                if k is not None:
                    e._typed_ann["key_type"] = _type_info(
                        self._expand_type(k), self._opaque_names()
                    )
                if v is not None:
                    e._typed_ann["value_type"] = _type_info(
                        self._expand_type(v), self._opaque_names()
                    )
            k = _common_type(key_types)
            v = _common_type(value_types)
            result = f"Map<{k}, {v}>" if k is not None and v is not None else "Map"
            self._ann_type(expr, result)
            return result
        if isinstance(expr, TupleLit):
            elem_types = [self._check_expr(e) for e in expr.elems]
            if elem_types and all(t is not None for t in elem_types):
                result = f"Tuple<{', '.join(elem_types)}>"
                expr._typed_ann["element_types"] = [
                    _type_info(self._expand_type(t), self._opaque_names())
                    for t in elem_types
                ]
            else:
                result = "Tuple"
            self._ann_type(expr, result)
            return result
        if isinstance(expr, StructConstruct):
            is_self = expr.type.name == "Self" and self.current_owner is not None
            type_name = (
                (self.current_owner_type or self.current_owner)
                if is_self
                else expr.type.name
            )
            base_name = _base(type_name)
            self._require(base_name, {"struct", "enum"}, expr, "struct")
            self._annotate_type_node(expr.type)
            if is_self:
                expr.type._typed_ann["type"] = _type_info(
                    self._expand_type(type_name), self._opaque_names()
                )
            arg_types = [self._check_expr(a) for a in expr.args]
            struct = self.structs.get(base_name)
            field_types: list[Optional[dict]] = []
            if struct is not None:
                type_args = (
                    _split_args(type_name) if is_self else [a.name for a in expr.type.args]
                )
                subst = dict(
                    zip(
                        [p.name for p in struct.params],
                        type_args,
                    )
                )
                fields = [f for f in struct.fields if not f.static]
                if len(arg_types) != len(fields):
                    self._record_error(
                        f"'{base_name}' expects {len(fields)} field value(s), "
                        f"got {len(arg_types)}",
                        expr.line,
                        expr.column,
                    )
                else:
                    for i, (f, at) in enumerate(zip(fields, arg_types)):
                        ft = _type_str(f.type, subst)
                        if not self._compat_types(ft, at):
                            self._record_error(
                                f"field {i + 1} '{f.name}' of '{base_name}' "
                                f"expects {self._fmt_type(ft)}, got {self._fmt_type(at)}",
                                expr.line,
                                expr.column,
                            )
                        self._check_refined_value(ft, expr.args[i], f)
                        field_types.append(_type_info(
                            self._expand_type(ft), self._opaque_names()
                        ))
            result_type = type_name if is_self else _type_str(expr.type)
            self._ann_type(expr, result_type)
            if field_types:
                expr._typed_ann["field_types"] = field_types
            return result_type
        return None

    def _check_name(self: "_Analyzer", name: Name) -> Optional[str]:
        if len(name.parts) == 1:
            n = name.parts[0]
            info = self._lookup(n)
            if info is not None:
                if info.kind == "let" and not info.initialized:
                    self._record_error(
                        f"variable '{n}' is used before assignment",
                        name.line,
                        name.column,
                    )
                if info.node is not None:
                    name._typed_ann["binding"] = {
                        # Top-level consts and validation fields are declared
                        # in scope too; keep their binding kind accurate
                        # instead of labeling everything a variable.
                        "kind": {
                            "const": "const",
                            "field": "field",
                        }.get(info.kind, "var"),
                        "ref": info.node._typed_id,
                    }
                self._ann_type(name, info.type)
                return info.type
            if n in self.functions:
                fn = self.functions[n]
                name._typed_ann["binding"] = {"kind": "fn", "ref": fn._typed_id}
                self._ann_type(name, "Fn")
                return "Fn"
            if n in self.consts:
                const = self.consts[n]
                name._typed_ann["binding"] = {
                    "kind": "const", "ref": const._typed_id
                }
                self._ann_type(name, _type_str(const.type))
                return _type_str(const.type)
            if n in BUILTIN_OBJECTS:
                name._typed_ann["binding"] = {"kind": "builtin", "ref": n}
                self._ann_type(name, BUILTIN_OBJECTS[n])
                return BUILTIN_OBJECTS[n]
            self._record_error(f"unknown identifier '{n}'", name.line, name.column)
            return None
        if len(name.parts) == 2:
            mod, member = name.parts
            if mod == "builtins":
                if member in BUILTIN_MODULE_FUNCTIONS:
                    name._typed_ann["binding"] = {
                        "kind": "builtin", "ref": member
                    }
                    self._ann_type(name, "Fn")
                    return "Fn"
                self._record_error(
                    f"unknown builtins:: member '{member}'",
                    name.line,
                    name.column,
                )
                return None
            if mod == "Self" and self.current_owner is not None:
                mod = self.current_owner
            struct = self.structs.get(mod)
            if struct is not None:
                for f in struct.fields:
                    if f.name == member and f.static:
                        name._typed_ann["binding"] = {
                            "kind": "field", "ref": f._typed_id
                        }
                        self._ann_type(name, _type_str(f.type))
                        return _type_str(f.type)
                binding = _find_method(self.methods.get(mod, []), member)
                if binding is not None:
                    name._typed_ann["binding"] = {
                        "kind": "method", "ref": binding.id
                    }
                    self._ann_type(name, "Fn")
                    return "Fn"
                self._record_error(
                    f"'{mod}' has no static member '{member}'",
                    name.line,
                    name.column,
                )
                return None
            enum = self.enums.get(mod)
            if enum is not None:
                for v in enum.variants:
                    if v.name == member:
                        name._typed_ann["binding"] = {
                            "kind": "variant", "ref": v._typed_id
                        }
                        self._ann_type(name, mod)
                        return mod
                self._record_error(
                    f"'{mod}' has no variant '{member}'",
                    name.line,
                    name.column,
                )
                return None
            self._record_error(f"unknown type '{mod}' in path", name.line, name.column)
            return None
        self._record_error("unsupported path expression", name.line, name.column)
        return None

    def _check_member(self: "_Analyzer", recv: Optional[str], member: str, node: Node) -> Optional[str]:
        recv = self._expand_type(recv)
        if recv is None:
            return None
        base = _base(recv)
        if base == "Tuple":
            if not member.isdigit():
                self._record_error(
                    f"tuple element must be an index like '{base}.0', "
                    f"got '{member}'",
                    node.line,
                    node.column,
                )
                return None
            args = _split_args(recv)
            idx = int(member)
            if idx >= len(args):
                self._record_error(
                    f"tuple '{recv}' has no element '{member}'",
                    node.line,
                    node.column,
                )
                return None
            node._typed_ann["member"] = {
                "kind": "tuple_elem", "index": idx
            }
            t = args[idx]
            if any(_type_mentions(t, name) for name in self.active_generics):
                self._ann_type(node, None)
                return None
            self._ann_type(node, t)
            return t
        struct = self.structs.get(base)
        if struct is not None:
            for f in struct.fields:
                if f.name == member:
                    if f.static:
                        self._record_error(
                            f"static field '{member}' must be accessed via "
                            f"'{base}::{member}'",
                            node.line,
                            node.column,
                        )
                    node._typed_ann["member"] = {
                        "kind": "field", "ref": f._typed_id
                    }
                    struct_params = [p.name for p in struct.params]
                    subst = dict(zip(struct_params, _split_args(recv)))
                    ftype = _subst_type_str(_type_str(f.type), subst)
                    if any(
                        _type_mentions(ftype, name)
                        for name in set(struct_params) | self.active_generics
                    ):
                        self._ann_type(node, None)
                        return None
                    self._ann_type(node, ftype)
                    return ftype
            binding = _find_method(self.methods.get(base, []), member)
            if binding is not None:
                # A method referenced as a value (not called): record the
                # binding; the type is left unknown rather than guessed.
                node._typed_ann["member"] = {
                    "kind": "method", "ref": binding.id
                }
                self._ann_type(node, None)
                return None
            self._record_error(
                f"type '{base}' has no field '{member}'",
                node.line,
                node.column,
            )
            return None
        methods = BUILTIN_TYPE_METHODS.get(base)
        if methods is not None:
            spec = methods.get(member)
            if spec is not None:
                node._typed_ann["member"] = {
                    "kind": "builtin", "ref": member
                }
                resolved = self._resolve_return(spec.returns, recv)
                self._ann_type(node, resolved)
                return resolved
            self._record_error(f"type '{base}' has no member '{member}'", node.line, node.column)
            return None
        return None

    def _check_call(self: "_Analyzer", call: Call) -> Optional[str]:
        result = self._check_call_inner(call)
        self._ann_type(call, result)
        return result

    def _check_call_inner(self: "_Analyzer", call: Call) -> Optional[str]:
        arg_types = [self._check_expr(a.value) for a in call.args]
        callee = call.callee
        if isinstance(callee, Name):
            if len(callee.parts) == 1:
                n = callee.parts[0]
                if n in self.functions:
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
                if mod == "builtins":
                    if member in BUILTIN_MODULE_FUNCTIONS:
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
                if mod == "Self" and self.current_owner is not None:
                    mod = self.current_owner
                binding = _find_method(self.methods.get(mod, []), member)
                if binding is not None:
                    result, subst = self._check_user_call(
                        binding.fn,
                        call,
                        arg_types,
                        is_method=True,
                        owner_hint=mod,
                        binding=binding,
                    )
                    callee._typed_ann["binding"] = {
                        "kind": "method", "ref": binding.id
                    }
                    self._ann_type(callee, "Fn")
                    self._ann_call(call, "method", binding.id, subst)
                    return result
                builtin = BUILTIN_TYPE_METHODS.get(_base(self._expand_type(mod) or mod))
                if builtin is not None:
                    spec = builtin.get(member)
                    if spec is not None:
                        if spec.args and spec.args[0] == "Self":
                            self._record_error(
                                f"instance method '{member}' of '{mod}' must "
                                "be called on a value",
                                call.line,
                                call.column,
                            )
                            callee._typed_ann["binding"] = {
                                "kind": "builtin", "ref": member
                            }
                            self._ann_type(callee, "Fn")
                            self._ann_call(call, "builtin", member)
                            return self._resolve_return(spec.returns, mod)
                        self._check_spec_args(member, spec, call, arg_types, None)
                        callee._typed_ann["binding"] = {
                            "kind": "builtin", "ref": member
                        }
                        self._ann_type(callee, "Fn")
                        self._ann_call(call, "builtin", member)
                        return self._resolve_return(spec.returns, None)
                self._record_error(f"'{mod}' has no method '{member}'", call.line, call.column)
                return None
            self._record_error("unsupported call target", call.line, call.column)
            return None
        if isinstance(callee, Attribute):
            recv = self._expand_type(self._check_expr(callee.obj))
            if recv is None:
                return None
            base = _base(recv)
            binding = _find_method(self.methods.get(base, []), callee.name)
            if binding is not None:
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
                callee._typed_ann["member"] = {
                    "kind": "method", "ref": binding.id
                }
                self._ann_type(callee, result)
                self._ann_call(call, "method", binding.id, subst)
                return result
            if callee.name == "into":
                # `x.into()` resolves through user-declared conversions; the
                # impl lives on the target type, so it is not in the receiver's
                # own method table.
                targets = self.conversions.get(recv, [])
                if len(targets) == 1:
                    callee._typed_ann["member"] = {
                        "kind": "builtin", "ref": "into"
                    }
                    self._ann_type(callee, targets[0])
                    self._ann_call(call, "builtin", "into")
                    return targets[0]
                return None  # unknown source or ambiguous conversion
            methods = BUILTIN_TYPE_METHODS.get(base)
            if methods is not None:
                spec = methods.get(callee.name)
                if spec is not None:
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
            return None  # undeclared methods on user structs are tolerated
        self._record_error("cannot call this expression", call.line, call.column)
        return None

    def _check_user_call(
        self: "_Analyzer",
        fn: FnDecl,
        call: Call,
        arg_types: list[Optional[str]],
        *,
        is_method: bool,
        owner_hint: Optional[str] = None,
        binding: Optional[MethodBinding] = None,
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

        if not any(a.unpack for a in call.args):
            if len(call.args) != len(params):
                self._record_error(
                    f"function '{fn.name}' expects {len(params)} argument(s), "
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
        ret = _type_str(fn.return_type) if fn.return_type is not None else "None"
        if ret == "Self" and owner_hint is not None:
            ret = owner_hint
        return self._resolve_use_type(ret, subst, generic_names), subst

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
        if ret in ("Self", "SameTypeOther"):
            return receiver
        if ret == "SameAsGeneric" or ret.startswith("SameAsGeneric:"):
            return _generic_arg(
                self._expand_type(receiver), _generic_ref_index(ret)
            )
        return ret

    def _resolve_expected(
        self: "_Analyzer", expected: str, receiver: Optional[str]
    ) -> Optional[str]:
        """Resolve dynamic placeholders to the concrete type they stand for."""
        if expected in ("Self", "SameTypeOther"):
            return receiver
        if expected == "SameAsGeneric" or expected.startswith("SameAsGeneric:"):
            return _generic_arg(
                self._expand_type(receiver), _generic_ref_index(expected)
            )
        return expected

    def _check_binop(
        self: "_Analyzer",
        op: TokenKind,
        left: Optional[str],
        right: Optional[str],
        node: Node,
    ) -> Optional[str]:
        if op in (TokenKind.AND, TokenKind.OR):
            for side, t in (("left", left), ("right", right)):
                if t is not None and not self._compat_types("Bool", t):
                    self._record_error(
                        f"'{op.value}' requires Bool operands, got {side} {self._fmt_type(t)}",
                        node.line,
                        node.column,
                    )
            return "Bool"
        if op in _EQUALITY:
            if left is not None and right is not None and not self._compat_types(left, right):
                self._record_error(
                    f"cannot compare {self._fmt_type(left)} with {self._fmt_type(right)}",
                    node.line,
                    node.column,
                )
            return "Bool"
        if op in _RELATIONAL:
            for side, t in (("left", left), ("right", right)):
                expanded = self._expand_type(t) if t is not None else None
                if expanded is not None and _base(expanded) not in _NUMERIC:
                    self._record_error(
                        f"'{op.value}' requires numeric operands, got {side} {self._fmt_type(t)}",
                        node.line,
                        node.column,
                    )
            return "Bool"
        if op in _BITWISE:
            for side, t in (("left", left), ("right", right)):
                expanded = self._expand_type(t) if t is not None else None
                if expanded is not None and _base(expanded) not in _INTEGER:
                    self._record_error(
                        f"'{op.value}' requires integer operands, got {side} {self._fmt_type(t)}",
                        node.line,
                        node.column,
                    )
            left_e = self._expand_type(left) if left is not None else None
            right_e = self._expand_type(right) if right is not None else None
            return _common_numeric(left_e, right_e) or "Int"
        if op in (TokenKind.PLUS, TokenKind.MINUS, TokenKind.STAR, TokenKind.SLASH, TokenKind.PERCENT):
            left_e = self._expand_type(left) if left is not None else None
            right_e = self._expand_type(right) if right is not None else None
            if op == TokenKind.PLUS and (left_e == "String" or right_e == "String"):
                other = right_e if left_e == "String" else left_e
                if other is not None and other != "String":
                    self._record_error(
                        f"cannot add String and {self._fmt_type(other)}",
                        node.line,
                        node.column,
                    )
                return "String"
            for side, t in (("left", left), ("right", right)):
                expanded = self._expand_type(t) if t is not None else None
                if expanded is not None and _base(expanded) not in _NUMERIC:
                    self._record_error(
                        f"'{op.value}' requires numeric operands, got {side} {self._fmt_type(t)}",
                        node.line,
                        node.column,
                    )
            return _common_numeric(left_e, right_e) or "Int"
        return None

    def _indexed_type(self: "_Analyzer", recv: Optional[str]) -> Optional[str]:
        recv = self._expand_type(recv)
        if recv is None:
            return None
        base = _base(recv)
        if base == "Map":
            args = _split_args(recv)
            return args[1] if len(args) >= 2 else None
        if base in ("Vector", "Set"):
            inner = recv[recv.find("<") + 1:-1] if "<" in recv else None
            return inner if inner and inner != "Any" else None
        if base == "String":
            return "String"
        return None

    def _tuple_indexed_type(
        self: "_Analyzer",
        recv: str,
        index: Node,
        node: Node,
    ) -> Optional[str]:
        """Resolve ``tuple[const]``: compile-time index, bounds checked."""
        args = _split_args(recv)
        folded = self._fold_expr(index)
        if not isinstance(folded, int):
            self._record_error(
                "tuple index must be a compile-time integer constant",
                node.line,
                node.column,
            )
            return None
        if folded < 0 or folded >= len(args):
            self._record_error(
                f"tuple '{recv}' has no element at index {folded}",
                node.line,
                node.column,
            )
            return None
        node._typed_ann["tuple_index"] = folded
        t = args[folded]
        if any(_type_mentions(t, name) for name in self.active_generics):
            self._ann_type(node, None)
            return None
        return t

    def _element_type(self: "_Analyzer", t: Optional[str]) -> Optional[str]:
        t = self._expand_type(t)
        if t is None:
            return None
        base = _base(t)
        if base in ("Vector", "Set"):
            inner = t[t.find("<") + 1:-1] if "<" in t else None
            return inner if inner and inner != "Any" else None
        if base == "Map":
            args = _split_args(t)
            if len(args) == 2:
                return f"Tuple<{args[0]}, {args[1]}>"
            return "Tuple"
        if base == "String":
            return "String"
        return None
