"""Expression mixin: the expression dispatcher -- literal/vector/map/tuple/struct literals and casts."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from ..const_fold import _literal_pure

from ..types import (
    _INTEGER,
    _NUMERIC,
    _base,
    _common_type,
    _generic_arg,
    _split_args,
    _split_ref_prefix,
    _smallest_literal_type,
    _smallest_signed_literal_type,
    _type_info,
    _type_str,
    split_array_type,
)

from ...ast_components.ast import (
    Arg,
    Assign,
    Attribute,
    BinOp,
    BoolLit,
    Call,
    CastExpr,
    ExternStatic,
    Field,
    FloatLit,
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
    VectorLit,
)

from ...ast_components.token import TokenKind

if TYPE_CHECKING:
    from ..analyzer import _Analyzer


class ExprLiterals:

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
