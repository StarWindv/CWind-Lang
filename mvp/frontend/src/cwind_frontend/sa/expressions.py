"""Expression, call and built-in member checks."""

from __future__ import annotations

from dataclasses import fields as _fields
from typing import TYPE_CHECKING, Optional

from .builtin_methods import (
    BUILTIN_MODULE_FUNCTIONS,
    BUILTIN_OBJECTS,
    BUILTIN_TYPE_METHODS,
    MethodSpec,
)
from .const_fold import _literal_pure, _match_arg_patterns, _patterns_arity_text
from .symbols import MethodBinding, VarInfo, _find_method
from .types import (
    _BUILTIN_RANGES,
    _INTEGER,
    _NUMERIC,
    _bare_type,
    _base,
    _common_numeric,
    _common_type,
    _generic_arg,
    _generic_ref_index,
    _is_ref,
    _replace_self,
    _split_args,
    _split_fn_sig,
    _split_ref_prefix,
    _smallest_literal_type,
    _smallest_signed_literal_type,
    _strip_ref,
    _subst_type_str,
    _type_info,
    _type_mentions,
    _type_str,
    split_array_type,
)
from ..ast_components.ast import (
    Arg,
    Assign,
    Attribute,
    BinOp,
    BoolLit,
    Call,
    CastExpr,
    ConstDecl,
    EnumDecl,
    ExternStatic,
    ExtraDecl,
    Field,
    FloatLit,
    FnDecl,
    Index,
    IntLit,
    MapLit,
    MatchStmt,
    Name,
    Node,
    Slice,
    StrLit,
    StructConstruct,
    Closure,
    TupleLit,
    UnaryOp,
    Variant,
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


def _fn_type_string(params: list[Optional[str]], ret: str) -> str:
    return "fn(" + ", ".join(p or "Any" for p in params) + ") -> " + ret


def _parse_fn_signature(t: str) -> tuple[list[str], str]:
    """Split ``fn(A, B) -> R`` into ``([A, B], R)``."""
    inner = t[len("fn("):t.rfind(")")]
    args = [x.strip() for x in inner.split(",") if x.strip()]
    if "->" in t:
        ret = t.split("->", 1)[1].strip()
    else:
        ret = "None"
    return args, ret


class ExpressionChecks:

    def _check_expr(
        self: "_Analyzer", expr: Node, expected: Optional[str] = None
    ) -> Optional[str]:
        def resolve_index(ep: Index):
            rec = self._check_expr(ep.obj)
            it = self._check_expr(ep.index)
            expanded = self._expand_type(rec) if rec is not None else None
            arr = split_array_type(expanded) if expanded is not None else None
            if arr is not None:
                # 定长数组 (todo-60): 结果为元素类型, 常量索引做边界检查
                t = self._array_indexed_type(arr, ep.index, ep)
            elif expanded is not None and _base(expanded) == "Tuple":
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
            BoolLit: "Bool",
            FloatLit: "Float",
            StrLit: "String",
        }

        typo = type(expr)
        if typo is IntLit:
            # 字面量按折叠值选最小适配位宽 (正数 → 无符号系, 负数 →
            # 带符号系, 十六进制恒为无符号); 与后端 cg_lit_int 的
            # 宽化规则一一对应。带期望类型时仍以期望为准 (期望只做
            # 范围检查, 不收窄字面量的静态类型)。
            if not self._check_int_literal_bounds(expr):
                return None
            t = _smallest_literal_type(expr.value, expr.raw)
            self._ann_type(expr, t)
            return t
        if typo in base_map:
            t = base_map[typo]
            self._ann_type(expr, t)
            return t
        if isinstance(expr, Name):
            return self._check_name(expr)
        if isinstance(expr, Attribute):
            return self._check_member(self._check_expr(expr.obj), expr.name, expr)
        if isinstance(expr, Call):
            return self._check_call(expr, expected)
        if isinstance(expr, Closure):
            return self._check_closure(expr, expected)
        if isinstance(expr, Index):
            return resolve_index(expr)
        if isinstance(expr, Slice):
            return resolve_slice(expr)
        if isinstance(expr, MatchStmt):
            return self._check_match(expr, None, as_expr=True)

        if isinstance(expr, CastExpr):
            # todo-17: ``expr as T`` numeric conversion; semantics
            # (truncation / sign extension / int<->float) are the
            # backend's existing scalar coercion rules.
            operand = self._check_expr(expr.operand)
            self._check_type(expr.target, expr)
            self._annotate_type_node(expr.target)
            expanded = (
                self._expand_type(operand) if operand is not None else None
            )
            if expanded is not None:
                expr._typed_ann["operand_type"] = _type_info(
                    expanded, self._opaque_names()
                )
            # bug-33: 目标类型先展开别名 (std::prelude 的 u32/...), 别名
            # 本身是数值类型时同样放行; 结果与注解都写展开后的底层类型,
            # 后端 cg_expr_cast 依 ann.type 做宽度转换。
            target_str = _type_str(expr.target)
            target_expanded = self._expand_type(target_str)
            base_target = target_expanded if target_expanded is not None else target_str
            if _base(base_target) not in _NUMERIC:
                self._record_error(
                    "'as' requires a numeric target type, got "
                    f"{self._fmt_type(target_str)}",
                    expr.line,
                    expr.column,
                )
                return None
            if expanded is not None and _base(expanded) not in _NUMERIC:
                self._record_error(
                    "'as' requires a numeric operand, got "
                    f"{self._fmt_type(expanded)}",
                    expr.line,
                    expr.column,
                )
                return None
            result = target_expanded or target_str
            if target_expanded and target_expanded != target_str:
                expr.target._typed_ann["type"] = _type_info(
                    target_expanded, self._opaque_names()
                )
            self._ann_type(expr, result)
            return result

        if isinstance(expr, UnaryOp):
            operand = self._check_expr(expr.operand)
            if operand is not None:
                expr._typed_ann["operand_type"] = _type_info(
                    self._expand_type(operand), self._opaque_names()
                )
            if expr.op == TokenKind.NOT:
                # todo-74: ``!`` is Bool logical negation, and Rust-style
                # bitwise NOT on integer operands (same-width result).
                expanded = (
                    self._expand_type(operand)
                    if operand is not None else None
                )
                base = _base(expanded) if expanded is not None else None
                if expanded is not None and expanded != "Bool" and (
                    base not in _INTEGER and base != "Byte"
                ):
                    self._record_error(
                        "'!' requires a Bool or integer operand, got "
                        f"{self._fmt_type(operand)}",
                        expr.line,
                        expr.column,
                    )
                result = (
                    "Bool"
                    if expanded is None or expanded == "Bool"
                    else expanded
                )
                self._ann_type(expr, result)
                return result
            if expr.op == TokenKind.AMP:
                if operand is None:
                    return None
                # bug-46: ``&mut expr`` —— 可变借用要求操作数是可变绑定
                # (Rust: cannot borrow immutable as mutable); 临时值
                # (调用结果、字段读取等) 不经变量名, 无法改写调用方,
                # 与 ``&`` 同样放行。
                if expr.mutable and isinstance(expr.operand, Name) and len(
                    expr.operand.parts
                ) == 1:
                    info = self._lookup(expr.operand.parts[0])
                    if info is not None and info.kind in (
                        "let", "param"
                    ) and not info.mutable:
                        self._record_error(
                            f"cannot borrow immutable "
                            f"{'parameter' if info.kind == 'param' else 'variable'} "
                            f"'{info.name}' as mutable; declare it with 'mut'",
                            expr.line,
                            expr.column,
                        )
                result = ("&mut " if expr.mutable else "&") + operand
                self._ann_type(expr, result)
                return result
            if expr.op == TokenKind.STAR:
                expanded = (
                    self._expand_type(operand)
                    if operand is not None else None
                )
                # todo-145: &T / &mut T 引用解引用 (与裸指针语义对齐):
                # 被指类型 = 剥掉借用前缀; 可写性由赋值检查另行把关
                if expanded is not None:
                    ref, inner = _split_ref_prefix(str(expanded))
                    if ref:
                        expr._typed_ann["operand_type"] = _type_info(
                            expanded, self._opaque_names()
                        )
                        self._ann_type(expr, inner)
                        return inner
                if (
                    expanded is not None
                    and not str(expanded).startswith("*const ")
                    and not str(expanded).startswith("*mut ")
                ):
                    self._record_error(
                        "cannot dereference non-raw-pointer type "
                        f"{self._fmt_type(operand)}",
                        expr.line,
                        expr.column,
                    )
                    return None
                if expanded is None:
                    return None
                result = expanded.split(" ", 1)[1]
                expr._typed_ann["operand_type"] = _type_info(
                    self._expand_type(operand), self._opaque_names()
                )
                self._ann_type(expr, result)
                return result
            if expr.op in (TokenKind.MINUS, TokenKind.PLUS):
                # 一元负号下的整数字面量按带符号系重判 (70000 作 UInt32
                # 字面量取负应为 Int64, 而不是 UInt32 回绕)。
                if (
                    expr.op == TokenKind.MINUS
                    and isinstance(expr.operand, IntLit)
                ):
                    signed = _smallest_signed_literal_type(
                        -expr.operand.value
                    )
                    self._ann_type(expr.operand, signed)
                    expr._typed_ann["operand_type"] = _type_info(
                        signed, self._opaque_names()
                    )
                    operand = signed
                    expanded = signed
                else:
                    expanded = (
                        self._expand_type(operand)
                        if operand is not None else None
                    )
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
            # bug-60: a BinOp whose folded value leaves the result type's
            # range is rejected here, so the check covers positions without
            # a declared target too (call args, conditions, ...).
            # Two exemptions:
            # ① 纯字面量链 (操作数都是 IntLit 或纯字面量 BinOp): 整个表达式
            #    按无穷精度折叠, 最终由接收方的目标类型范围检查一次性裁决
            #    (中间溢出不是错, Rust 语义; `255 + 255 + 255` → 目标 UInt8
            #    报一次 765)。
            # ② 双侧都是未收窄的裸字面量默认 Int/UInt: 同上, 归 todo-22。
            if not _literal_pure(expr.left) and not _literal_pure(expr.right):
                self._check_expr_range(result, expr, left, right)
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
            value = self._check_expr(expr.value, target)
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
                # todo-56: extern 静态变量须以 `static mut` 声明才可写
                st = self.extern_statics.get(expr.target.parts[0])
                if isinstance(st, ExternStatic) and not st.mutable:
                    self._record_error(
                        f"cannot assign to extern static "
                        f"'{expr.target.parts[0]}'; declare it with 'mut'",
                        expr.line,
                        expr.column,
                    )
            # todo-122: associated constants are read-only like top-level
            # consts (both plain and compound assignment targets reject).
            if isinstance(expr.target, Name) and len(expr.target.parts) == 2:
                tb = expr.target._typed_ann.get("binding") or {}
                if tb.get("kind") == "assoc_const":
                    self._record_error(
                        "cannot assign to associated const "
                        f"'{'::'.join(expr.target.parts)}'",
                        expr.line,
                        expr.column,
                    )
                # bug-57: module-qualified ``mod::CONST`` is likewise
                # read-only (same binding kind as a bare top-level const).
                if tb.get("kind") == "const" and len(
                    expr.target.parts
                ) == 2:
                    self._record_error(
                        "cannot assign to const "
                        f"'{'::'.join(expr.target.parts)}'",
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
            # Rust NLL: 整体赋值 ("`= `", 非复合) 会用新值**覆盖**旧存储,
            # 目标绑定随后是初始化状态, moved 标志随之清除 —— 否则
            # `s = take(s); s = take(s);` 这类 "拿自己去换回来" 的惯用法
            # 会在第二次 RHS 实参检查时误报 use-after-move.
            # 复合赋值 (`+=` 等) 读旧值参与运算, 不在此列。
            if (
                isinstance(expr.target, Name)
                and len(expr.target.parts) == 1
                and expr.op == TokenKind.ASSIGN
            ):
                info = self._lookup(expr.target.parts[0])
                if info is not None and info.kind in ("let", "param"):
                    info.moved = False
            self._ann_type(expr, value)
            return value
        if isinstance(expr, VectorLit):
            elem_expected: Optional[str] = None
            arr_expected: Optional[tuple[str, int]] = None
            arr_name: Optional[str] = None
            repeat = expr._typed_ann.get("repeat")
            if repeat is not None and (
                not isinstance(repeat, int) or repeat < 1
            ):
                self._record_error(
                    "repeat array literal '[x; N]' requires a positive N",
                    expr.line,
                    expr.column,
                )
                return None
            if expected is not None:
                expanded_expected = self._expand_type(expected)
                if expanded_expected is not None and _base(
                    expanded_expected
                ) in ("Vector", "Set"):
                    elem_expected = _generic_arg(expanded_expected, 1)
                else:
                    # todo-60: 目标类型是定长数组时, `[...]` 字面量按
                    # 数组构造处理 (类型制导, 长度必须精确匹配)
                    arr_expected = (
                        split_array_type(expanded_expected)
                        if expanded_expected is not None
                        else None
                    )
                    if arr_expected is not None:
                        arr_name = expanded_expected
            if arr_expected is not None:
                elem_expected = arr_expected[0]
                count = (
                    repeat if repeat is not None else len(expr.elems)
                )
                if count != arr_expected[1]:
                    self._record_error(
                        f"cannot initialize {self._fmt_type(expected)} "
                        f"with {count} element(s)",
                        expr.line,
                        expr.column,
                    )
                elems = [
                    self._check_expr(e, elem_expected)
                    for e in expr.elems
                ]
                for e in expr.elems:
                    self._check_literal_range(elem_expected, e)
                    self._check_refined_value(elem_expected, e)
                result = arr_name or expected or "Vector"
                self._ann_type(expr, result)
                expr._typed_ann["element_type"] = _type_info(
                    self._expand_type(elem_expected), self._opaque_names()
                )
                return result
            if repeat is not None:
                # bug-35: `[x; N]` 重复字面量是定长数组专用语法
                self._record_error(
                    "repeat array literal '[x; N]' requires a "
                    "fixed-length array target type",
                    expr.line,
                    expr.column,
                )
                return None
            elems = [self._check_expr(e, elem_expected) for e in expr.elems]
            if elem_expected is not None:
                for e in expr.elems:
                    self._check_literal_range(elem_expected, e)
                    self._check_refined_value(elem_expected, e)
            if elem_expected is not None:
                # bug-47: 声明注解给出元素类型时, 字面量主动绑定它
                # (Rust: ``let v: Vec<f64> = vec![1.0, 2.0];``);
                # 元素逐个校验, 数值间沿用既有的隐式转换语义。
                for i, et in enumerate(elems):
                    if et is not None and not self._compat_types(
                        elem_expected, et
                    ):
                        self._record_error(
                            f"element {i + 1} must be "
                            f"{self._fmt_type(elem_expected)}, "
                            f"got {self._fmt_type(et)}",
                            expr.elems[i].line,
                            expr.elems[i].column,
                        )
                elem = elem_expected
            else:
                elem = _common_type(elems)
            result = f"Vector<{elem}>" if elem is not None else "Vector"
            self._ann_type(expr, result)
            if elem is not None:
                # element_type 给后端选装箱宽度, 用完全展开的具体类型
                expr._typed_ann["element_type"] = _type_info(
                    self._expand_type(elem, _deep=True), self._opaque_names()
                )
            return result
        if isinstance(expr, MapLit):
            key_types: list[Optional[str]] = []
            value_types: list[Optional[str]] = []
            seen_keys: dict[tuple, "Node"] = {}
            key_expected: Optional[str] = None
            value_expected: Optional[str] = None
            if expected is not None:
                expanded_expected = self._expand_type(expected)
                if expanded_expected is not None and _base(
                    expanded_expected
                ) == "Map":
                    args = _split_args(expanded_expected)
                    if len(args) >= 2:
                        key_expected = args[0]
                        value_expected = args[1]
            for e in expr.entries:
                k = self._check_expr(e.key, key_expected)
                v = self._check_expr(e.value, value_expected)
                key_types.append(k)
                value_types.append(v)
                if key_expected is not None:
                    self._check_literal_range(key_expected, e.key)
                    self._check_refined_value(key_expected, e.key)
                if value_expected is not None:
                    self._check_literal_range(value_expected, e.value)
                    self._check_refined_value(value_expected, e.value)
                tag = self._map_literal_key_tag(e.key)
                if tag is not None:
                    if tag in seen_keys:
                        self._record_error(
                            "duplicate key "
                            f"{self._map_literal_key_text(e.key)} "
                            "in map literal",
                            e.key.line,
                            e.key.column,
                        )
                    else:
                        seen_keys[tag] = e.key
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
            if key_expected is not None or value_expected is not None:
                # bug-47: 注解给出键/值类型时字面量主动绑定 (同 Vector)。
                # 逐条目的 key_type/value_type 注解一并改写为绑定后的
                # 类型: 后端按注解决定装箱宽度, 注解停留在外部推断类型
                # (如 Float) 而声明是 f64 时会按错误宽度读回。
                if key_expected is not None:
                    for i, kt in enumerate(key_types):
                        if kt is not None and not self._compat_types(
                            key_expected, kt
                        ):
                            self._record_error(
                                f"key {i + 1} must be "
                                f"{self._fmt_type(key_expected)}, "
                                f"got {self._fmt_type(kt)}",
                                expr.entries[i].key.line,
                                expr.entries[i].key.column,
                            )
                    k = key_expected
                    for e in expr.entries:
                        e._typed_ann["key_type"] = _type_info(
                            self._expand_type(k, _deep=True),
                            self._opaque_names(),
                        )
                if value_expected is not None:
                    for i, vt in enumerate(value_types):
                        if vt is not None and not self._compat_types(
                            value_expected, vt
                        ):
                            self._record_error(
                                f"value {i + 1} must be "
                                f"{self._fmt_type(value_expected)}, "
                                f"got {self._fmt_type(vt)}",
                                expr.entries[i].value.line,
                                expr.entries[i].value.column,
                            )
                    v = value_expected
                    for e in expr.entries:
                        e._typed_ann["value_type"] = _type_info(
                            self._expand_type(v, _deep=True),
                            self._opaque_names(),
                        )
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
            owner_typed = self.current_owner_type or self.current_owner
            # 类型制导: 裸名构造 ``Cell { v }`` 优先绑定 owner 的带参类型
            # (方法体内), 否则绑定调用点期望类型 (let 注解, bug-49/47);
            # 没有可用实参时字段类型停留在 T 上, 无法与实参比较。
            guidance: Optional[str] = None
            if (
                owner_typed is not None
                and "::" not in expr.type.name
                and _base(owner_typed) == expr.type.name
            ):
                guidance = owner_typed
            if (
                guidance is None
                and expected is not None
                and "::" not in expr.type.name
            ):
                exp = self._expand_type(expected)
                if exp is not None and _base(exp) == expr.type.name:
                    guidance = exp
            if (
                guidance is not None
                and not expr.type.args
                and not is_self
            ):
                is_self = True
            # todo-164: a bare constructor with no expectation to borrow
            # argument types from materializes its declaration's parameter
            # defaults (``Box2 { 5 }`` -> ``Box2<Int32>``).  When guidance
            # exists its string already carries the real arguments; filling
            # from defaults would clobber them, so it is skipped there.
            decl = self.structs.get(expr.type.name)
            if (
                decl is not None
                and guidance is None
                and not is_self
                and len(expr.type.args) < len(decl.params)
            ):
                self._fill_generic_defaults(expr.type, decl.params)
            if not is_self and not self._resolve_qualified_type_name(expr.type):
                # precise module-surface error already recorded
                self._ann_type(expr, None)
                return None
            type_name = (
                guidance
                if is_self and guidance is not None
                else (owner_typed if is_self else expr.type.name)
            )
            base_name = _base(type_name)
            self._require(base_name, {"struct", "enum"}, expr, "struct")
            self._annotate_type_node(expr.type)
            if is_self:
                expr.type._typed_ann["type"] = _type_info(
                    self._expand_type(type_name), self._opaque_names()
                )
            struct = self.structs.get(base_name)
            # 先解析字段类型再检查实参: 数组等类型制导字面量依赖期望类型
            subst: dict[str, str] = {}
            fields: list = []
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
                # todo-90: 位置式构造必须提供全部字段, 存在非 pub 字段即拒绝
                offender = next((f for f in fields if not f.pub), None)
                if offender is not None:
                    self._check_field_visibility(
                        struct, offender, base_name, expr
                    )
            arg_types: list[Optional[str]] = []
            for i, a in enumerate(expr.args):
                ft: Optional[str] = None
                if i < len(fields):
                    ft = _type_str(fields[i].type, subst or None)
                arg_types.append(self._check_expr(a, ft))
            field_types: list[Optional[dict]] = []
            if struct is not None:
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

    def _check_module_member(
        self: "_Analyzer", name: Name, mod: str, member: str
    ) -> Optional[str]:
        """Resolve ``module::member`` against the import surfaces (todo-77).

        The export surface decides accessibility: a name that exists in the
        module but was not exported reports ``private``, while an unknown
        name reports ``has no function``.  Callers must ensure ``mod`` is a
        registered module alias first.

        bug-57: ``pub const`` declarations resolve here too -- the member is
        a value of the const's own type (annotated with its module path for
        provenance), not a function.
        """
        module = self.modules[mod]
        display = "::".join(module)
        known = self.module_known.get(mod)
        exported = self.module_exports.get(mod)
        const = self.consts.get(member)
        if const is not None and not getattr(const, "pub", False):
            const = None  # same file scope: private consts stay unreferable
        fn = self.functions.get(member)
        is_known_member = known is None or member in known
        if not is_known_member:
            self._record_error(
                f"module '{display}' has no function '{member}'",
                name.line,
                name.column,
            )
            return None
        if exported is not None and member not in exported:
            # An exported fn/private const both report "private", but the
            # wording distinguishes functions from constants.
            self._record_error(
                f"{'constant' if const is not None else 'function'} "
                f"'{member}' is private in module '{display}'",
                name.line,
                name.column,
            )
            return None
        if const is not None and (fn is None or fn.pub is False):
            name._typed_ann["binding"] = {
                "kind": "const", "ref": const._typed_id,
            }
            name._typed_ann["module"] = {
                "path": list(module),
                "source": self._module_sources.get(module[-1]),
            }
            self._ann_type(name, _type_str(const.type))
            return _type_str(const.type)
        if fn is None:
            self._record_error(
                f"module '{display}' has no function '{member}'",
                name.line,
                name.column,
            )
            return None
        name._typed_ann["binding"] = {
            "kind": "fn", "ref": fn._typed_id,
        }
        name._typed_ann["module"] = {
            "path": list(module),
            "source": self._module_sources.get(module[-1]),
        }
        self._ann_type(name, "Fn")
        return "Fn"

    def _find_extra_const(
        self: "_Analyzer", owner: str, member: str
    ) -> Optional["ConstDecl"]:
        """todo-122: find an associated const ``owner::member``.

        ``extra`` blocks register their consts under the owner struct name
        in pass 1 (``_index``).  ``owner`` must already be resolved
        (``Self`` -> owner type) and non-qualified.
        """
        for c in self.extra_consts.get(owner, []):
            if c.name == member:
                return c
        return None

    def _fold_module_path(
        self: "_Analyzer", parts: list[str]
    ) -> Optional[list[str]]:
        """todo-133: collapse a leading chain of module namespaces.

        ``facade::inner::val`` walks ``facade`` (registered namespace) and
        folds every segment that names a module inside the current
        namespace's re-export surface — so ``geom::shapes::v()`` (module
        inside module) reaches its member just like the two-segment form.
        Folded namespaces absent from the alias tables (re-exported
        modules) are registered on the fly with their full chain path so
        the two-segment resolver and provenance stay accurate.  Returns the
        rewritten ``[namespace, *members]`` path, or ``None`` when no fold
        happened (the caller keeps the enum-variant handling).
        """
        if len(parts) < 3 or parts[0] not in self.modules:
            return None
        chain_parts = list(self.modules[parts[0]])
        # todo-107/133: a namespace member re-exported via ``pub mod`` is
        # not in the bare export surface; the per-declaration index maps
        # ``namespace -> frozenset(submodule names)`` for this walk.
        ns_members = self._mod_decl_submods.get(parts[0], frozenset())
        cur_exports = self.module_exports.get(parts[0])
        cur_known = self.module_known.get(parts[0])
        folded = 0
        for i in range(1, len(parts) - 1):
            seg = parts[i]
            ns = self._mod_decl_namespace.get(seg)
            if ns is None:
                break
            # The segment must be visible inside the current namespace AND
            # be a known module namespace itself (enum variants are names
            # in the export surface too, but never namespaces).  Visibility
            # reads the full known surface: a re-exported module name rides
            # the export face even when not a bare-callable symbol.
            in_ns = (
                seg in ns_members
                or (cur_exports is not None and seg in cur_exports)
                or (cur_known is not None and seg in cur_known)
            )
            if not in_ns or seg not in self._mod_decl_namespace:
                break
            folded += 1
            chain_parts = [*chain_parts, seg]
            ns_parts, ns_exports = ns
            self.modules.setdefault(seg, list(chain_parts))
            self.module_exports.setdefault(seg, ns_exports)
            self.module_known.setdefault(seg, ns_exports)
            ns_members = self._mod_decl_submods.get(seg, frozenset())
            cur_exports = ns_exports
            cur_known = ns_exports
        if folded == 0:
            return None
        return [parts[folded], *parts[1 + folded:]]

    def _check_name(self: "_Analyzer", name: Name) -> Optional[str]:
        """Resolve an identifier or path, including todo-81's qualified
        ``module::Enum::Variant`` form and todo-133's ``mod::mod::member``."""
        if len(name.parts) >= 3 and name.parts[0] in self.modules:
            folded = self._fold_module_path(name.parts)
            if folded is not None and len(folded) == 2:
                name.parts = folded
                return self._check_module_member(
                    name, folded[0], folded[1]
                )
        if len(name.parts) == 2:
            mod, member = name.parts
            if self.modules and mod in self.modules:
                return self._check_module_member(name, mod, member)
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
                if info.moved:
                    self._record_error(
                        f"value '{n}' is used after move",
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
                if self._reject_hidden(n, "function", name):
                    return None
                fn = self.functions[n]
                name._typed_ann["binding"] = {"kind": "fn", "ref": fn._typed_id}
                self._ann_type(name, "Fn")
                return "Fn"
            if n in self.consts:
                if self._reject_hidden(n, "constant", name):
                    return None
                const = self.consts[n]
                name._typed_ann["binding"] = {
                    "kind": "const", "ref": const._typed_id
                }
                self._ann_type(name, _type_str(const.type))
                return _type_str(const.type)
            if n in self.extern_statics:
                # todo-56: extern 静态变量读取 (绑定给后端分派)
                if self._reject_hidden(n, "static", name):
                    return None
                st = self.extern_statics[n]
                name._typed_ann["binding"] = {
                    "kind": "extern_static", "ref": st._typed_id
                }
                self._ann_type(name, _type_str(st.type))
                return _type_str(st.type)
            if n in BUILTIN_OBJECTS:
                name._typed_ann["binding"] = {"kind": "builtin", "ref": n}
                self._ann_type(name, BUILTIN_OBJECTS[n])
                return BUILTIN_OBJECTS[n]
            self._record_error(f"unknown identifier '{n}'", name.line, name.column)
            return None
        # todo-81: ``module::Enum::Variant`` resolves through the module
        # surface, then normalizes to the flattened two-segment enum/variant
        # path consumed by exhaustive matching and the backend.
        if len(name.parts) == 3 and name.parts[0] in self.modules:
            return self._resolve_qualified_variant(name)
        if len(name.parts) >= 2:
            mod, member = name.parts[:2]
            if mod in self.modules:
                return self._check_module_member(name, mod, member)
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
                        # todo-90: 非 pub 静态字段仅定义模块内可见
                        self._check_field_visibility(struct, f, mod, name)
                        name._typed_ann["binding"] = {
                            "kind": "field", "ref": f._typed_id
                        }
                        self._ann_type(name, _type_str(f.type))
                        return _type_str(f.type)
                # todo-122: associated constants declared in an extra block
                const = self._find_extra_const(mod, member)
                if const is not None:
                    name._typed_ann["binding"] = {
                        "kind": "assoc_const", "ref": const._typed_id
                    }
                    self._ann_type(name, _type_str(const.type))
                    return _type_str(const.type)
                binding = _find_method(
                    self.methods.get(_base(self._expand_type(mod) or mod) or "", []),
                    member,
                )
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
                for idx, v in enumerate(enum.variants):
                    if v.name == member:
                        if v.fields:
                            self._record_error(
                                f"variant '{member}' of enum '{mod}' carries "
                                "a payload and must be constructed with "
                                "arguments",
                                name.line,
                                name.column,
                            )
                        name._typed_ann["binding"] = {
                            "kind": "variant", "ref": v._typed_id
                        }
                        name._typed_ann["variant_index"] = idx
                        self._ann_type(name, mod)
                        enum_def = self._type_def_path(mod)
                        if enum_def is not None:
                            name._typed_ann["enum_def"] = enum_def
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

    def _resolve_qualified_variant(
        self: "_Analyzer", name: Name
    ) -> Optional[str]:
        """todo-81: resolve a ``module::Enum::Variant`` unit variant.

        The module alias is validated against the module surface (distinct
        unknown/private diagnostics), the flattened enum and variant are
        resolved, and the source path is normalized to the canonical
        two-segment form so the backend keeps consuming plain
        ``Enum::Variant`` names.  The alias survives only as provenance.
        """
        mod, enum_name, variant_name = name.parts
        if not self._require_module_type(name, mod, enum_name, {"enum"}):
            return None
        enum = self.enums.get(enum_name)
        if enum is None:
            self._record_error(
                f"module '{'::'.join(self.modules[mod])}' has no enum "
                f"'{enum_name}'",
                name.line,
                name.column,
            )
            return None
        variant = next(
            (v for v in enum.variants if v.name == variant_name), None
        )
        if variant is None:
            self._record_error(
                f"enum '{enum_name}' has no variant '{variant_name}'",
                name.line,
                name.column,
            )
            return None
        if variant.fields:
            self._record_error(
                f"variant '{variant_name}' of enum '{enum_name}' carries "
                "a payload and must be constructed with arguments",
                name.line,
                name.column,
            )
            return None
        if self._reject_hidden(enum_name, "enum", name):
            return None
        name._typed_ann["binding"] = {
            "kind": "variant", "ref": variant._typed_id
        }
        name._typed_ann["module"] = {
            "path": list(self.modules[mod]),
            "source": self._module_sources.get(mod),
        }
        name._typed_ann["variant_index"] = enum.variants.index(variant)
        name.parts = [enum_name, variant_name]
        self._ann_type(name, enum_name)
        enum_def = self._type_def_path(enum_name)
        if enum_def is not None:
            name._typed_ann["enum_def"] = enum_def
        return enum_name

    def _resolve_qualified_type_name(self: "_Analyzer", type_: "Type") -> bool:
        """todo-124/bug-42: normalize ``alias::Type`` in type positions to
        the flattened bare type name.

        The alias may come from ``use a::b as c;`` or a plain module import.
        Resolution validates visibility through the module surface and
        rewrites ``type_.name`` in place so downstream checks and the
        backend see one canonical spelling.  Returns True when the name is
        usable (either already bare or successfully resolved); False means
        a precise error has already been recorded.
        """
        name = type_.name
        if (
            "::" not in name
            or name.startswith(("fn(", "*const ", "*mut ", "["))
            or name.startswith("Self::")
            or name.count("::") != 1
        ):
            return True
        head, tail = name.split("::", 1)
        if head not in self.modules:
            # Not a module path (e.g. an enum variant pattern); other
            # checks own the diagnostics for it.
            return True
        if not self._require_module_type(type_, head, tail, {"type"}):
            return False
        type_.name = tail
        return True

    def _require_module_type(
        self: "_Analyzer",
        node: Node,
        mod: str,
        member: str,
        kinds: set[str],
    ) -> bool:
        """Validate that a module-qualified type name is known and public."""
        display = "::".join(self.modules[mod])
        known = self.module_known.get(mod)
        exported = self.module_exports.get(mod)
        if known is not None and member not in known:
            # todo-107: the member may be a ``mod`` namespace that is not
            # addressable from this file (private / outside its pub scope)
            # — report that instead of a misleading type-kind error.
            if any(
                member in rows for rows in self._mod_decl_aliases.values()
            ):
                self._record_error(
                    f"module '{display}::{member}' is not visible here "
                    "(declare it 'pub mod' or widen its visibility)",
                    node.line,
                    node.column,
                )
                return False
            kind_text = "/".join(sorted(kinds))
            self._record_error(
                f"module '{display}' has no {kind_text} '{member}'",
                node.line,
                node.column,
            )
            return False
        if exported is not None and member not in exported:
            self._record_error(
                f"type '{member}' is private in module '{display}'",
                node.line,
                node.column,
            )
            return False
        return True

    def register_module_source(self: "_Analyzer", alias: str, source: str) -> None:
        """Record the originating file of an imported declaration."""
        if source:
            self._module_sources[alias] = source

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
                    # todo-90: 非 pub 字段仅定义模块内可见
                    self._check_field_visibility(struct, f, base, node)
                    node._typed_ann["member"] = {
                        "kind": "field", "ref": f._typed_id
                    }
                    struct_params = [p.name for p in struct.params]
                    subst = dict(zip(struct_params, _split_args(recv)))
                    ftype = _subst_type_str(_type_str(f.type), subst)
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
        if base in self.enums:
            self._record_error(
                f"type '{base}' has no member '{member}'",
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

    def _check_call(
        self: "_Analyzer", call: Call, expected: Optional[str] = None
    ) -> Optional[str]:
        result = self._check_call_inner(call, expected)
        self._ann_type(call, result)
        return result

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

    def _check_format_braces(
        self: "_Analyzer",
        strlit: StrLit,
        args: Optional[list[Arg]] = None,
    ) -> None:
        """浅层检查格式模板的花括号配平 (只跳过转义, 与 rt 栈机解析一致)."""
        raw = strlit.raw
        if len(raw) >= 2 and raw[0] in "\"'" and raw[-1] == raw[0]:
            inner = raw[1:-1]
        else:
            inner = raw
        depth = 0
        placeholders = 0
        i = 0
        n = len(inner)
        while i < n:
            ch = inner[i]
            if ch == "\\":
                i += 2
                continue
            if ch == "{":
                if i + 1 < n and inner[i + 1] == "}":
                    placeholders += 1
                    i += 2
                    continue
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth < 0:
                    self._record_error(
                        "format string has more '}' than '{' "
                        "(use '\\}' for a literal brace)",
                        strlit.line,
                        strlit.column,
                    )
                    return
            i += 1
        if depth != 0:
            self._record_error(
                "format string has unmatched '{' "
                "(use '\\{' for a literal brace)",
                strlit.line,
                strlit.column,
            )
        if args is not None and len(args) != placeholders:
            self._record_error(
                f"format string has {placeholders} placeholder(s) but got "
                f"{len(args)} argument(s)",
                strlit.line,
                strlit.column,
            )

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

    def _array_indexed_type(
        self: "_Analyzer",
        arr: tuple[str, int],
        index: Node,
        node: Node,
    ) -> Optional[str]:
        """Resolve ``array[const]`` (todo-60): compile-time bounds check."""
        elem, n = arr
        folded = self._fold_expr(index)
        if isinstance(folded, int):
            if folded < 0 or folded >= n:
                self._record_error(
                    f"array index {folded} is out of bounds "
                    f"(length {n})",
                    node.line,
                    node.column,
                )
                return None
        # 非常量索引留给后端做运行时边界检查
        return elem

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
        if base == "Tuple":
            # entry() 的临时迭代标记: Tuple<K, V> 表示“每轮产出 (K, V) 条目”,
            # 不是逐元素遍历普通元组。
            args = _split_args(t)
            return t if args else None
        if base == "String":
            return "String"
        return None

    def _map_literal_key_tag(
        self: "_Analyzer", node: "Node"
    ) -> Optional[tuple]:
        """A comparable key identity for compile-time duplicate detection."""
        if isinstance(node, StrLit):
            return ("str", node.value)
        if isinstance(node, BoolLit):
            return ("bool", node.value)
        folded = self._fold_expr(node)
        if isinstance(folded, (int, float)):
            return ("num", folded)
        if isinstance(node, Name) and len(node.parts) == 1:
            const = self.consts.get(node.parts[0])
            if const is not None:
                return self._map_literal_key_tag(const.value)
        return None

    def _map_literal_key_text(self: "_Analyzer", node: "Node") -> str:
        raw = getattr(node, "raw", None)
        if raw:
            return raw
        if isinstance(node, StrLit):
            return f'"{node.value}"'
        return getattr(node, "value", "?")
