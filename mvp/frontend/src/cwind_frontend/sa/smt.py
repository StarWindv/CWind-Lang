"""Function-body, statement and refinement checks (SA pass 3)."""

from __future__ import annotations

import math
import struct
from typing import TYPE_CHECKING, Optional, Union

from .const_fold import (
    _const_int,
    _const_number,
    _eval_refinement,
    _expr_str,
    _has_return,
)
from .symbols import VarInfo, _find_method
from .types import (
    BUILTIN_TYPES,
    _NUMERIC,
    _BUILTIN_RANGES,
    _FLOAT32_MAX,
    # bug-60 后续: 字面量绝对上限 (i64/u64 为当前最宽整数)
    _INT64_MIN,
    _UINT64_MAX,
    _FLOAT64_MAX,
    _base,
    _common_numeric,
    _replace_self,
    _split_args,
    _split_ref_prefix,
    _subst_type_str,
    _type_info,
    _type_str_from_info,
    _type_str,
    split_array_type,
)
from ..ast_components.ast import (
    Arg,
    Assign,
    Attribute,
    BindPattern,
    BinOp,
    Block,
    BoolLit,
    BreakStmt,
    Call,
    ContinueStmt,
    EnumPattern,
    ExprStmt,
    Field,
    FloatLit,
    FnDecl,
    Index,
    IntLit,
    LetStmt,
    LitPattern,
    MatchArm,
    MatchStmt,
    Name,
    Node,
    Param,
    Pattern,
    ReturnStmt,
    StructConstruct,
    StructPattern,
    StrLit,
    TuplePattern,
    UnaryOp,
    LoopStmt,
    WildcardPattern,
)
from ..ast_components.token import TokenKind

if TYPE_CHECKING:
    from .analyzer import _Analyzer


_SWAP_COMPARE: dict[TokenKind, TokenKind] = {
    TokenKind.LT: TokenKind.GT,
    TokenKind.GT: TokenKind.LT,
    TokenKind.LE: TokenKind.GE,
    TokenKind.GE: TokenKind.LE,
    TokenKind.EQ: TokenKind.EQ,
    TokenKind.NE: TokenKind.NE,
    TokenKind.NOT_LT: TokenKind.NOT_GT,
    TokenKind.NOT_GT: TokenKind.NOT_LT,
}


class BodyChecks:

    # -- pass 3: function bodies ------------------------------------------
    def _check_fn(
        self: "_Analyzer",
        fn: FnDecl,
        owner: Optional[str],
        generic: frozenset[str] = frozenset(),
        owner_type: Optional[str] = None,
    ) -> None:
        saved_owner = self.current_owner
        saved_owner_type = self.current_owner_type
        saved_generics = self.active_generics
        # todo-90: the defining file of the body being checked decides
        # struct-field visibility.  Methods inherit the tag of their own
        # FnDecl (same file as their impl/extra block by construction).
        saved_module = self.current_module
        self.current_module = getattr(fn, "source_module", None)
        # todo-79: bare-name visibility of the body's own file.
        saved_visible = self.current_visible
        self.current_visible = self._visible_for(fn)
        self.current_owner = owner
        self.current_owner_type = owner_type if owner_type is not None else owner
        self.active_generics = saved_generics | generic
        self._push_scope()
        for p in fn.params:
            ptype: Optional[str]
            if p.name == "self" and owner is not None:
                base_owner = owner_type if owner_type is not None else owner
                # todo-145: &mut self 的类型串如实携带 mut (此前渲染成
                # "&Self" 式的共享借用)
                if p.type is not None and p.type.ref:
                    ptype = ("&mut " if p.type.mut else "&") + base_owner
                else:
                    ptype = base_owner
            else:
                ptype = _type_str(p.type) if p.type is not None else None
                # bug-34: 方法体内非 self 形参若声明为 Self, 同样绑定到
                # 所属类型 (与返回类型在下方的一致性处理一致)
                if ptype is not None:
                    ptype = _replace_self(ptype, self.current_owner_type or owner)
            self._declare(VarInfo(
                p.name,
                ptype,
                p.line,
                p.column,
                "param",
                # bug-46: ``&mut T`` 形参可写穿引用 (同 ``&mut self``)
                mutable=p.mutable or (
                    ptype is not None and ptype.startswith("&mut ")
                ),
                # todo-145: 关键字与类型来源分开 (重赋值 vs 写穿)
                declared_mut=p.mutable,
                ref_mut=ptype is not None and ptype.startswith("&mut "),
                node=p,
            ))
            self._ann_type(p, ptype)
            if p.type is not None:
                self._annotate_type_node(p.type)
                # bug-58: keep the alias-expanded callback signature that
                # _check_extern_abi_type wrote (``fn(c_int) -> c_int`` ->
                # ``fn(Int32) -> Int32``); re-annotating from the raw
                # spelling would silently revert the expansion.
                if ptype is not None and str(ptype).startswith("fn("):
                    expanded_sig = self._expand_type(str(ptype))
                    if expanded_sig is not None and expanded_sig != str(ptype):
                        self._ann_type(p.type, expanded_sig)
        ret = _type_str(fn.return_type) if fn.return_type is not None else "None"
        if ret == "Self" or ret.startswith("Self<"):
            if isinstance(owner, str):
                ret = _replace_self(
                    ret, owner_type if owner_type is not None else owner
                )
        self._ann_type(fn, ret)
        if fn.return_type is not None:
            self._annotate_type_node(fn.return_type)
            if ret != _type_str(fn.return_type):
                # The signature's own Type node resolves Self the same way
                # the FnDecl annotation does (e.g. `-> Self` in extra).
                fn.return_type._typed_ann["type"] = _type_info(
                    self._expand_type(ret), self._opaque_names()
                )
        self.defined |= generic
        saved_fn_return = self.current_fn_return
        try:
            if fn.body is not None:
                self.current_fn_return = ret
                self._check_block(fn.body, ret)
        finally:
            self.current_fn_return = saved_fn_return
            self.defined -= generic
            self.active_generics = saved_generics
        if ret == "!" and fn.body is not None and not self._block_diverges(fn.body):
            self._record_error(
                f"function '{fn.name}' returns '!' but does not diverge "
                "(every path must end in a return or a `!` call)",
                fn.line,
                fn.column,
            )
        elif ret not in ("None", "!") and fn.body is not None \
                and not _has_return(fn.body):
            self._record_error(
                f"function '{fn.name}' must return a value",
                fn.line,
                fn.column,
            )
        if fn.which is not None:
            if owner is None:
                self._record_error(
                    f"which is only allowed on methods, not '{fn.name}'",
                    fn.line,
                    fn.column,
                )
            if fn.static:
                self._record_error(
                    f"which method '{fn.name}' cannot be static",
                    fn.line,
                    fn.column,
                )
            if len(fn.params) != 1 or fn.params[0].name != "self":
                self._record_error(
                    f"which method '{fn.name}' must take exactly one self parameter",
                    fn.line,
                    fn.column,
                )
            if fn.type_params:
                self._record_error(
                    f"which method '{fn.name}' cannot have generic parameters",
                    fn.line,
                    fn.column,
                )
            if owner is not None:
                target = _find_method(self.methods.get(owner, []), fn.which)
                if target is None:
                    self._record_error(
                        f"which target '{fn.which}' does not exist on '{owner}'",
                        fn.line,
                        fn.column,
                    )
                elif not target.fn.params or target.fn.params[0].name != "self":
                    self._record_error(
                        f"which target '{fn.which}' must be an instance method",
                        fn.line,
                        fn.column,
                    )
        self._pop_scope()
        self.current_owner = saved_owner
        self.current_owner_type = saved_owner_type
        self.current_module = saved_module
        self.current_visible = saved_visible

    def _block_diverges(self: "_Analyzer", block: Block) -> bool:
        """Whether a block can never complete normally (for ``-> !``
        functions): the last statement is a return, a diverging ``!`` call,
        or a branch whose every path diverges."""
        if not block.stmts:
            return False
        stmt = block.stmts[-1]
        if isinstance(stmt, ReturnStmt):
            return True
        if isinstance(stmt, (BreakStmt, ContinueStmt)):
            # todo-168 (let-else): break/continue 使语句不正常结束
            # (Rust 同样视为发散) — let-else 的 else 块允许它们。
            return True
        if isinstance(stmt, ExprStmt):
            ann = getattr(stmt.expr, "_typed_ann", {})
            t = ann.get("type")
            return isinstance(t, dict) and t.get("name") == "!"
        if isinstance(stmt, MatchStmt):
            return bool(stmt.arms) and all(
                self._arm_diverges(a) for a in stmt.arms
            )
        if isinstance(stmt, Block):
            return self._block_diverges(stmt)
        return False

    def _arm_diverges(self: "_Analyzer", arm: MatchArm) -> bool:
        if isinstance(arm.body, Block):
            return self._block_diverges(arm.body)
        ann = getattr(arm.body, "_typed_ann", {})
        t = ann.get("type")
        return isinstance(t, dict) and t.get("name") == "!"

    def _check_block(self: "_Analyzer", block: Block, return_type: str) -> None:
        self._push_scope()
        for stmt in block.stmts:
            self._check_stmt(stmt, return_type)
        self._pop_scope()

    def _check_block_with_return(
        self: "_Analyzer",
        block: Block,
        return_type: str,
        *,
        infer: bool = False,
    ) -> Optional[str]:
        """Check a closure body and optionally report its tail type.

        The parser lowers a closure's tail expression into ``return expr;``
        (same as function bodies), so the tail type is read from the last
        statement.  When ``infer`` is set the declared return type is not
        known yet; statements are checked against ``Any`` (permissive) and
        the caller receives the observed tail type.
        """
        self._push_scope()
        tail_type: Optional[str] = None
        check_type = return_type if not infer else "Any"
        try:
            for stmt in block.stmts:
                self._check_stmt(stmt, check_type)
            if block.stmts:
                last = block.stmts[-1]
                if isinstance(last, ReturnStmt) and last.value is not None:
                    info = getattr(last.value, "_typed_ann", {}).get("type")
                    if isinstance(info, dict):
                        tail_type = _type_str_from_info(info)
                elif isinstance(last, ExprStmt):
                    info = getattr(last.expr, "_typed_ann", {}).get("type")
                    if isinstance(info, dict):
                        tail_type = _type_str_from_info(info)
        finally:
            self._pop_scope()
        return tail_type if infer else return_type

    def _check_validation(
        self: "_Analyzer",
        block: Block,
        vars: list[tuple[str, str, Optional[Node]]],
    ) -> None:
        """Check a where/arrow validation block: every statement is a Bool
        condition, with the validated value(s) in scope."""
        self._push_scope()
        for name, t, node in vars:
            self._declare(VarInfo(
                name, t, block.line, block.column, "field", node=node
            ))
            expanded = self._expand_type(t)
            if expanded is not None:
                bounds = _BUILTIN_RANGES.get(_base(expanded))
                if bounds is not None:
                    lo, hi = bounds
                    for stmt in block.stmts:
                        if isinstance(stmt, ExprStmt):
                            self._warn_dead_refinement(
                                stmt.expr, name, _base(expanded), lo, hi
                            )
        for stmt in block.stmts:
            if isinstance(stmt, ExprStmt):
                self._check_condition(stmt.expr)
        self._pop_scope()

    def _check_stmt(self: "_Analyzer", stmt: Node, return_type: str) -> None:
        if isinstance(stmt, LetStmt):
            # todo-164: materialize generic-parameter defaults on the
            # annotation before its string form is derived, so a bare
            # ``let b: Box2 = ...`` types as ``Box2<Int32>`` everywhere.
            if (
                stmt.type is not None
                and "::" not in stmt.type.name
                and len(stmt.type.args) == 0
            ):
                decl = (
                    self.structs.get(stmt.type.name)
                    or self.enums.get(stmt.type.name)
                )
                if decl is not None and len(decl.params) > 0:
                    self._fill_generic_defaults(stmt.type, decl.params)
                else:
                    alias = self.type_aliases.get(stmt.type.name)
                    if alias is not None and len(alias.params) > 0:
                        self._fill_generic_defaults(stmt.type, alias.params)
            declared = _type_str(stmt.type) if stmt.type is not None else None
            # bug-34: 方法体内 `let x: Self = ...` 把 Self 绑定到所属类型,
            # 否则尾返回/初始化校验拿到的声明类型仍是裸 "Self" 而对不上
            if declared is not None:
                declared = _replace_self(
                    declared, self.current_owner_type or self.current_owner
                )
            if declared == "!":
                self._record_error(
                    "cannot declare a value of type '!' (it is the never type)",
                    stmt.line,
                    stmt.column,
                )
            value = (
                self._check_expr(stmt.value, declared)
                if stmt.value is not None else None
            )
            if stmt.type is not None:
                self._check_type(stmt.type, stmt)
            known = (
                declared is not None
                and (
                    _base(declared) in BUILTIN_TYPES
                    or _base(declared) in self.defined
                    or split_array_type(declared) is not None
                )
            )
            if declared is None and stmt.value is not None:
                # todo-186: 无注解的 let 按初始化表达式推断。解析器仍
                # 要求用户书写类型注解, 该分支只服务于降糖产物合成的
                # let (for-in 迭代器绑定, 类型在降糖期不可知)。
                declared = value
            if declared is None:
                self._record_error("let declaration requires a type", stmt.line, stmt.column)
            elif known and not self._compat_types(declared, value):
                at = stmt.value if stmt.value is not None else stmt
                self._record_error(
                    f"cannot initialize {self._fmt_type(declared)} with {self._fmt_type(value)}",
                    at.line,
                    at.column,
                )
            self._check_literal_range(declared, stmt.value)
            self._check_refined_value(declared, stmt.value)
            folded_init = self._fold_expr(stmt.value)
            self._declare(VarInfo(
                stmt.name,
                declared,
                stmt.line,
                stmt.column,
                "let",
                initialized=stmt.value is not None,
                # bug-46: 类型 ``&mut T`` 的绑定可写穿被借用位置
                mutable=stmt.mutable or (
                    declared is not None and declared.startswith("&mut ")
                ),
                # todo-145: 关键字与类型来源分开 (重赋值 vs 写穿)
                declared_mut=stmt.mutable,
                ref_mut=declared is not None and declared.startswith("&mut "),
                node=stmt,
                folded=folded_init,
            ))
            self._ann_type(
                stmt,
                declared,
                original=(
                    getattr(stmt.type, "_fqn_original", None)
                    if stmt.type is not None else None
                ),
            )
            if stmt.value is not None and value is not None:
                stmt._typed_ann["init_type"] = _type_info(
                    self._expand_type(value), self._opaque_names()
                )
        elif isinstance(stmt, ReturnStmt):
            if stmt.value is None:
                if return_type != "None":
                    self._record_error(
                        f"function returns {return_type} but returned nothing",
                        stmt.line,
                        stmt.column,
                    )
                self._ann_type(stmt, "None")
                return
            value = self._check_expr(stmt.value, return_type)
            if not self._compat_types(return_type, value):
                self._record_error(
                    f"return type mismatch: expected {self._fmt_type(return_type)}, "
                    f"got {self._fmt_type(value)}",
                    stmt.line,
                    stmt.column,
                )
            self._check_literal_range(return_type, stmt.value)
            self._check_refined_value(return_type, stmt.value)
            self._ann_type(stmt, value)
            stmt._typed_ann["expected_return"] = _type_info(
                self._expand_type(return_type), self._opaque_names()
            )
        elif isinstance(stmt, ExprStmt):
            expr = stmt.expr
            if isinstance(expr, Assign):
                self._check_assignment_mutability(expr)
            self._check_expr(expr)
        elif isinstance(stmt, MatchStmt):
            # todo-184/186: if / if-let / while / while-let / for-in 在
            # 降糖后都以 match 或 loop+match 的形态到达这里, 独立的
            # 分支检查不再存在。
            self._check_match(stmt, return_type)
        elif isinstance(stmt, LoopStmt):
            # todo-185: the basic form — break/continue are valid here.
            self.loop_depth += 1
            self._loop_labels.append(stmt.label)
            try:
                self._check_block(stmt.body, return_type)
            finally:
                self.loop_depth -= 1
                self._loop_labels.pop()
        elif isinstance(stmt, Block):
            self._check_block(stmt, return_type)
        elif isinstance(stmt, (BreakStmt, ContinueStmt)):
            keyword = "break" if isinstance(stmt, BreakStmt) else "continue"
            if stmt.label is not None:
                # todo-185: labeled break/continue — the label must name
                # one of the loops currently being checked.
                if stmt.label not in (
                    lbl for lbl in self._loop_labels if lbl is not None
                ):
                    self._record_error(
                        f"unknown loop label '{stmt.label}' in '{keyword}'",
                        stmt.line,
                        stmt.column,
                    )
            elif self.loop_depth == 0:
                self._record_error(
                    f"'{keyword}' can only be used inside a loop",
                    stmt.line,
                    stmt.column,
                )

    def _check_assignment_mutability(self: "_Analyzer", expr: Assign) -> None:
        """Check the binding that owns a direct or field write."""
        target = expr.target
        if isinstance(target, Name) and len(target.parts) == 1:
            info = self._lookup(target.parts[0])
            if info is not None:
                # todo-145: 绑定重赋值要求 ``mut`` 关键字本身 ——
                # ``&mut T`` 类型的可变性只属于被借用的位置
                # (``let r: &mut Int`` 之后 ``r = ...`` 仍非法)。
                if (
                    info.kind in ("let", "param")
                    and info.ref_mut
                    and not info.declared_mut
                ):
                    self._record_error(
                        f"cannot assign to binding '{info.name}'; declare it "
                        "with 'mut' ('&mut' only makes the borrowed place "
                        "writable)",
                        expr.line,
                        expr.column,
                    )
                else:
                    self._require_mutable(info, expr)
            return
        if isinstance(target, UnaryOp) and target.op == TokenKind.STAR:
            # `*p = v`: 写入被指向的存储。裸指针要求 ``*mut`` 且指针
            # 绑定本身声明 ``mut``; 引用要求 ``&mut`` (绑定无需 mut,
            # Rust: ``let r = &mut x; *r = 1;`` 合法)。
            ptr_type = self._check_expr(target.operand)
            expanded = self._expand_type(ptr_type)
            ref = ""
            inner = ""
            if expanded is not None:
                ref, inner = _split_ref_prefix(str(expanded))
            if ref == "&mut ":
                if isinstance(target.operand, Name) and len(
                    target.operand.parts
                ) == 1:
                    info = self._lookup(target.operand.parts[0])
                    if info is not None and info.kind in ("let", "param"):
                        # todo-145: 写穿可变性来自 &mut 类型本身
                        # (ref_mut ⊆ mutable, 此处额外放行裸 &mut 表达式)
                        pass
                return
            if ref == "&":
                self._record_error(
                    "cannot assign through a shared reference "
                    f"{self._fmt_type(ptr_type)}; use '&mut'",
                    target.line,
                    target.column,
                )
                return
            if expanded is not None and str(expanded).startswith("*const "):
                self._record_error(
                    "cannot assign through '*const'; use '*mut'",
                    target.line,
                    target.column,
                )
            elif expanded is not None and not str(expanded).startswith("*mut "):
                self._record_error(
                    "cannot assign through non-raw-pointer type "
                    f"{self._fmt_type(ptr_type)}",
                    target.line,
                    target.column,
                )
            if isinstance(target.operand, Name) and len(target.operand.parts) == 1:
                info = self._lookup(target.operand.parts[0])
                if info is not None:
                    self._require_mutable(info, expr)
            return
        if isinstance(target, Index):
            # `arr[i] = v`: 写入的是容器本身的数据, 容器变量须可变
            receiver = target.obj
            while isinstance(receiver, (Attribute, Index)):
                receiver = receiver.obj
            if isinstance(receiver, Name) and len(receiver.parts) == 1:
                info = self._lookup(receiver.parts[0])
                if info is not None and info.kind in ("let", "param"):
                    self._require_mutable(info, expr)
            return
        if isinstance(target, Attribute):
            receiver = target.obj
            while isinstance(receiver, Attribute):
                receiver = receiver.obj
            if isinstance(receiver, Name) and len(receiver.parts) == 1:
                info = self._lookup(receiver.parts[0])
                if info is not None and info.kind in ("let", "param"):
                    self._require_mutable(info, expr)

    def _check_match(
        self: "_Analyzer",
        stmt: MatchStmt,
        return_type: Optional[str] = None,
        *,
        as_expr: bool = False,
        expected: Optional[str] = None,
    ) -> Optional[str]:
        """Check ``match``: the subject once, every arm's pattern in its own
        scope, guards as Bool conditions, and overall exhaustiveness.

        Block arms are statement-style; expression arms make the match a
        value (Rust style).  A match used as a value (``as_expr``) also
        accepts diverging block arms (todo-168 let-else): Rust types them
        ``!`` so they unify with any arm type.  ``expected`` (todo-191)
        是调用点期望类型, 下传给各臂驱动 ``None`` 字面量的变体补全。
        Returns the common value type for expression matches, else
        ``None``.
        """
        # 表达式位经由 _check_expr 到达这里时拿不到 return_type (块臂里
        # 的 return 要按外围函数的返回类型检查), 回退到当前函数返回值。
        effective_return = (
            return_type
            if return_type is not None
            else self.current_fn_return
        )
        subject = self._check_expr(stmt.subject)
        if subject is not None:
            stmt._typed_ann["subject_type"] = _type_info(
                self._expand_type(subject), self._opaque_names()
            )
        if not stmt.arms:
            self._record_error(
                "match must have at least one arm", stmt.line, stmt.column
            )
            return
        arm_types: list[str] = []
        block_arms = 0
        expr_arms = 0
        for arm in stmt.arms:
            self._push_scope()
            self._check_pattern(arm.pattern, subject, arm)
            if arm.guard is not None:
                self._check_condition(arm.guard)
            if isinstance(arm.body, Block):
                block_arms += 1
                arm._typed_ann["body_kind"] = "block"
                self._check_block(arm.body, effective_return or "None")
                # todo-168: 块臂发散性 (let-else 的 miss 臂) — 检查后
                # ann 齐备, 发散块臂 (return/break/continue/`!` 调用)
                # 在表达式位可与任意臂类型合一。
                arm._typed_ann["arm_diverges"] = self._block_diverges(
                    arm.body
                )
            else:
                expr_arms += 1
                arm._typed_ann["body_kind"] = "expr"
                t = self._check_expr(arm.body, expected)
                if t is not None:
                    arm._typed_ann["body_type"] = _type_info(
                        self._expand_type(t), self._opaque_names()
                    )
                    arm_types.append(t)
            self._pop_scope()
        if block_arms and expr_arms:
            diverging_blocks = all(
                arm._typed_ann.get("arm_diverges")
                for arm in stmt.arms
                if isinstance(arm.body, Block)
            )
            if not (as_expr and diverging_blocks):
                self._record_error(
                    "match arms must be all blocks or all expressions "
                    "(a block arm in a value match must diverge)",
                    stmt.line,
                    stmt.column,
                )
                return None
        if as_expr and block_arms and not expr_arms:
            self._record_error(
                "match used as an expression needs expression arms "
                "(`=> expr`), not statement blocks",
                stmt.line,
                stmt.column,
            )
            return None
        if not any(
            self._pattern_is_irrefutable(arm.pattern) for arm in stmt.arms
        ):
            exhaustive = False
            expanded = (
                self._expand_type(subject) if subject is not None else None
            )
            if expanded is not None:
                enum = self.enums.get(_base(expanded))
                if enum is not None:
                    covered = {
                        arm.pattern.path[1]
                        for arm in stmt.arms
                        if isinstance(arm.pattern, EnumPattern)
                        and len(arm.pattern.path) == 2
                        and arm.pattern.path[0] == enum.name
                    }
                    if covered == {v.name for v in enum.variants}:
                        exhaustive = True
            if not exhaustive:
                self._record_error(
                    "match is not exhaustive: add a wildcard `_` "
                    "(or bare binding) arm",
                    stmt.line,
                    stmt.column,
                )
        if expr_arms:
            common = self._common_arm_type(arm_types)
            if common is None and arm_types:
                self._record_error(
                    "match arms have incompatible value types: "
                    + ", ".join(self._fmt_type(t) for t in arm_types),
                    stmt.line,
                    stmt.column,
                )
            else:
                self._ann_type(stmt, common)
                for arm in stmt.arms:
                    if (
                        arm._typed_ann.get("body_kind") == "expr"
                        and common is not None
                    ):
                        self._check_literal_range(common, arm.body)
            return common
        return None

    def _common_arm_type(
        self: "_Analyzer", types: list[str]
    ) -> Optional[str]:
        """Common type of match expression arms.

        Numeric arms promote like ordinary arithmetic (Int8 + Int → Int,
        Rust-ish literal inference); everything else must be identical —
        except a **bare generic-enum arm** (``Option``, unit variant
        ``Option::None`` checked without an expected type): Rust 推断里
        它的形参待定, 与同 base 带实参的臂兼容, 取带实参的形态。
        """
        common: Optional[str] = None
        for t in types:
            if t == "!":
                continue
            if common is None:
                common = t
            elif _base(common) in _NUMERIC and _base(t) in _NUMERIC:
                common = _common_numeric(common, t)
            elif common == t:
                continue
            elif self._bare_enum_compatible(common, t):
                common = self._prefer_parametrized(common, t)
            else:
                return None
        if common is None and types:
            return "!"
        return common

    def _bare_enum_compatible(
        self: "_Analyzer", a: str, b: str
    ) -> bool:
        """True when exactly one of *a*/*b* is a bare (param-less) enum
        name and both share the same base."""
        ba, bb = _base(a), _base(b)
        if ba != bb:
            return False
        bare_a = a == ba and ba in self.enums
        bare_b = b == bb and bb in self.enums
        return bare_a != bare_b

    @staticmethod
    def _prefer_parametrized(a: str, b: str) -> str:
        """The parametrized side of a bare/parametrized pair."""
        return b if "<" in b else a

    @staticmethod
    def _pattern_is_irrefutable(pattern: Pattern) -> bool:
        """Whether a pattern always matches (used for match exhaustiveness).

        Wildcards and bare bindings always match; tuple / struct patterns
        are irrefutable when every sub-pattern is irrefutable and struct
        patterns either cover every field or end with ``..``.
        """
        if isinstance(pattern, (WildcardPattern, BindPattern)):
            return True
        if isinstance(pattern, TuplePattern):
            return all(
                BodyChecks._pattern_is_irrefutable(e)
                for e in pattern.elems
            )
        if isinstance(pattern, StructPattern):
            # Missing fields without `..` were already rejected by
            # _check_pattern; what matters here is that no literal test
            # can fail.
            return all(
                f.pattern is None
                or BodyChecks._pattern_is_irrefutable(f.pattern)
                for f in pattern.fields
            )
        return False

    def _check_pattern(
        self: "_Analyzer",
        pattern: Pattern,
        expected: Optional[str],
        context: Node, # unused?
    ) -> None:
        """Type-check a pattern against ``expected`` and declare any
        bindings in the current scope.

        Each pattern node is annotated with the type it matches so the
        backend can lower it without re-deriving tuple/field element types.
        """
        if isinstance(pattern, WildcardPattern):
            self._ann_type(pattern, expected)
            return
        if isinstance(pattern, BindPattern):
            if pattern.name == "_":
                self._record_error(
                    "'_' is the wildcard pattern and cannot be bound",
                    pattern.line,
                    pattern.column,
                )
                return
            if expected is None:
                self._record_error(
                    "cannot bind a value of unknown type",
                    pattern.line,
                    pattern.column,
                )
                self._ann_type(pattern, None)
                return
            self._ann_type(pattern, expected)
            self._declare(VarInfo(
                pattern.name,
                expected,
                pattern.line,
                pattern.column,
                "let",
                node=pattern,
            ))
            return
        if isinstance(pattern, LitPattern):
            if expected is None:
                self._record_error(
                    "cannot match a literal against a value of unknown type",
                    pattern.line,
                    pattern.column,
                )
                self._ann_type(pattern, None)
                return
            lit = pattern.value
            lit_type = {
                IntLit: "Int",
                FloatLit: "Float",
                StrLit: "String",
                BoolLit: "Bool",
            }.get(type(lit))
            if lit_type is None:
                self._record_error(
                    "unsupported literal pattern",
                    pattern.line,
                    pattern.column,
                )
                return
            self._ann_type(pattern, expected)
            if not self._compat_types(expected, lit_type):
                self._record_error(
                    f"literal pattern of type {self._fmt_type(lit_type)} "
                    f"cannot match {self._fmt_type(expected)}",
                    pattern.line,
                    pattern.column,
                )
                return
            self._check_literal_range(expected, pattern.value)
            return
        if isinstance(pattern, TuplePattern):
            expanded = (
                self._expand_type(expected) if expected is not None else None
            )
            base = _base(expanded) if expanded is not None else None
            if base != "Tuple":
                self._record_error(
                    f"tuple pattern cannot match {self._fmt_type(expected)}",
                    pattern.line,
                    pattern.column,
                )
                self._ann_type(pattern, expected)
                return
            args = _split_args(expanded)
            if not args:
                self._record_error(
                    "cannot destructure a Tuple with unknown element types",
                    pattern.line,
                    pattern.column,
                )
                self._ann_type(pattern, expected)
                return
            if len(args) != len(pattern.elems):
                self._record_error(
                    f"tuple pattern expects {len(args)} element(s), "
                    f"got {len(pattern.elems)}",
                    pattern.line,
                    pattern.column,
                )
                self._ann_type(pattern, expected)
                return
            self._ann_type(pattern, expected)
            pattern._typed_ann["element_types"] = [
                self._type_info_enriched(t)
                for t in args
            ]
            for elem, t in zip(pattern.elems, args):
                self._check_pattern(elem, t, pattern)
            return
        if isinstance(pattern, StructPattern):
            if not self._resolve_qualified_type_name(pattern.type):
                # precise module-surface error already recorded
                self._ann_type(pattern, None)
                return
            type_name = _type_str(pattern.type)
            base = _base(type_name)
            self._require(base, {"struct"}, pattern, "struct")
            self._annotate_type_node(pattern.type)
            expanded = self._expand_type(type_name)
            self._ann_type(pattern, expanded, self._opaque_names())
            struct = self.structs.get(base)
            if struct is None:
                return
            if expected is not None and not self._compat_types(expected, type_name):
                self._record_error(
                    f"pattern of type {self._fmt_type(type_name)} "
                    f"cannot match {self._fmt_type(expected)}",
                    pattern.line,
                    pattern.column,
                )
                return
            subst = dict(
                zip(
                    [p.name for p in struct.params],
                    [a.name for a in pattern.type.args],
                )
            )
            fields = [f for f in struct.fields if not f.static]
            field_map = {f.name: f for f in fields}
            seen: set[str] = set()
            for sf in pattern.fields:
                f = field_map.get(sf.name)
                if f is None:
                    self._record_error(
                        f"struct '{base}' has no field '{sf.name}'",
                        sf.line,
                        sf.column,
                    )
                    continue
                if sf.name in seen:
                    self._record_error(
                        f"duplicate field '{sf.name}' in struct pattern",
                        sf.line,
                        sf.column,
                    )
                    continue
                seen.add(sf.name)
                # todo-90: 非 pub 字段不允许在定义模块外解构
                self._check_field_visibility(struct, f, base, sf)
                ftype = _subst_type_str(_type_str(f.type), subst)
                if sf.pattern is None:
                    self._ann_type(sf, ftype)
                    self._declare(VarInfo(
                        sf.name,
                        ftype,
                        sf.line,
                        sf.column,
                        "let",
                        node=sf,
                    ))
                else:
                    self._check_pattern(sf.pattern, ftype, sf)
            missing = [f.name for f in fields if f.name not in seen]
            if missing and not pattern.rest:
                self._record_error(
                    f"struct pattern for '{base}' is missing field(s): "
                    f"{', '.join(missing)} (add `..` to ignore remaining fields)",
                    pattern.line,
                    pattern.column,
                )
            pattern._typed_ann["field_types"] = {
                f.name: self._type_info_enriched(
                    _subst_type_str(_type_str(f.type), subst)
                )
                for f in fields
            }
            return
        if isinstance(pattern, EnumPattern):
            if len(pattern.path) == 1:
                # 用户裁决: 模式位现阶段要求手写 FQN (``Enum::Variant``)。
                # expected 驱动的裸变体反查已撤 —— 变体未经名字解析就
                # 按预期类型归属, 语义根基不对; 待 enum 成员导入落地后
                # 按作用域遮蔽把裸名展开为 FQN 再接入 (比较基于 FQN)。
                hint = ""
                expanded_expected = (
                    self._expand_type(expected) if expected is not None else None
                )
                base_expected = (
                    _base(expanded_expected)
                    if expanded_expected is not None
                    else None
                )
                if base_expected in self.enums:
                    hint = (
                        f" — write '{base_expected}::{pattern.path[0]}'"
                    )
                self._record_error(
                    "bare variant patterns are not supported yet: write "
                    "the qualified form 'Enum::Variant'" + hint,
                    pattern.line,
                    pattern.column,
                )
                self._ann_type(pattern, expected)
                return
            if pattern.named_fields is not None:
                self._check_variant_struct_pattern(pattern, expected)
                return
            if len(pattern.path) not in (2, 3):
                self._record_error(
                    "unsupported enum variant pattern",
                    pattern.line,
                    pattern.column,
                )
                return
            expanded = (
                self._expand_type(expected) if expected is not None else None
            )
            base = _base(expanded) if expanded is not None else None
            enum = self.enums.get(base) if base is not None else None
            if enum is None:
                self._record_error(
                    f"enum variant pattern cannot match "
                    f"{self._fmt_type(expected)}",
                    pattern.line,
                    pattern.column,
                )
                self._ann_type(pattern, expected)
                return
            # todo-81: normalize ``module::Enum::Variant`` after resolving it;
            # downstream exhaustive-match checks and codegen only need the
            # canonical two-segment enum/variant path.
            if pattern.path[0] != enum.name:
                if (
                    len(pattern.path) == 3
                    and pattern.path[0] in self.modules
                    and pattern.path[1] == enum.name
                ):
                    mod_alias = pattern.path[0]
                    pattern._typed_ann["module"] = {
                        "path": list(self.modules[mod_alias]),
                        "source": self._module_sources.get(mod_alias),
                    }
                    # The module alias is resolved and retained as provenance.
                    # It is intentionally not part of the canonical path used
                    # for variant lookup, exhaustiveness, or backend dispatch.
                    pattern.path = pattern.path[1:]
                else:
                    self._record_error(
                        f"variant pattern '{'::'.join(pattern.path)}' does not "
                        f"belong to enum '{enum.name}'",
                        pattern.line,
                        pattern.column,
                    )
                    self._ann_type(pattern, expected)
                    return
            variant = next(
                (v for v in enum.variants
                 if v.name == pattern.path[1]),
                None,
            )
            if variant is None:
                self._record_error(
                    f"enum '{enum.name}' has no variant '{pattern.path[1]}'",
                    pattern.line,
                    pattern.column,
                )
                self._ann_type(pattern, expected)
                return
            self._ann_type(pattern, expected)
            pattern._typed_ann["enum"] = enum.name
            # todo-146: 枚举定义位置溯源 (本地枚举无 def, 键省略)
            enum_def = self._type_def_path(enum.name)
            if enum_def is not None:
                pattern._typed_ann["enum_def"] = enum_def
            pattern._typed_ann["variant_index"] = next(
                i for i, v in enumerate(enum.variants) if v is variant
            )
            subst = dict(
                zip(
                    [p.name for p in enum.params],
                    _split_args(expanded) if expanded is not None else [],
                )
            )
            ftypes = [
                _subst_type_str(_type_str(f), subst)
                for f in variant.fields
            ]
            if variant.fields and not pattern.elems:
                self._record_error(
                    f"variant '{variant.name}' carries a payload; use "
                    f"'{enum.name}::{variant.name}(p1, p2)'",
                    pattern.line,
                    pattern.column,
                )
                return
            if not variant.fields and pattern.elems:
                self._record_error(
                    f"variant '{variant.name}' takes no payload",
                    pattern.line,
                    pattern.column,
                )
                return
            if len(pattern.elems) != len(ftypes):
                self._record_error(
                    f"variant '{variant.name}' expects {len(ftypes)} "
                    f"payload pattern(s), got {len(pattern.elems)}",
                    pattern.line,
                    pattern.column,
                )
                return
            for elem, ft in zip(pattern.elems, ftypes):
                self._check_pattern(elem, ft, pattern)
            return
            self._check_variant_struct_pattern(pattern, expected)
            return
        self._record_error(
            "unsupported pattern",
            pattern.line,
            pattern.column,
        )

    def _check_variant_struct_pattern(
        self: "_Analyzer",
        pattern: EnumPattern,
        expected: Optional[str],
    ) -> None:
        """todo-193: 具名字段变体模式 ``Enum::Variant { f: P, g, .. }``.

        与 struct pattern 同语义, 但枚举变体字段有序名字表; ann 与
        位置式变体模式同构 (enum/variant_index), 子字段类型按
        variant.field_names 查表; 载荷槽序 = 声明序。"""
        expanded = (
            self._expand_type(expected) if expected is not None else None
        )
        base = _base(expanded) if expanded is not None else None
        enum = self.enums.get(base) if base is not None else None
        if enum is None:
            self._record_error(
                f"enum variant pattern cannot match "
                f"{self._fmt_type(expected)}",
                pattern.line,
                pattern.column,
            )
            self._ann_type(pattern, expected)
            return
        if pattern.path[0] != enum.name:
            if (
                len(pattern.path) == 3
                and pattern.path[0] in self.modules
                and pattern.path[1] == enum.name
            ):
                mod_alias = pattern.path[0]
                pattern._typed_ann["module"] = {
                    "path": list(self.modules[mod_alias]),
                    "source": self._module_sources.get(mod_alias),
                }
                pattern.path = pattern.path[1:]
            else:
                self._record_error(
                    f"variant pattern '{'::'.join(pattern.path)}' does not "
                    f"belong to enum '{enum.name}'",
                    pattern.line,
                    pattern.column,
                )
                self._ann_type(pattern, expected)
                return
        variant = next(
            (v for v in enum.variants if v.name == pattern.path[1]),
            None,
        )
        if variant is None:
            self._record_error(
                f"enum '{enum.name}' has no variant '{pattern.path[1]}'",
                pattern.line,
                pattern.column,
            )
            self._ann_type(pattern, expected)
            return
        if not variant.field_names:
            self._record_error(
                f"variant '{enum.name}::{variant.name}' has no named "
                "fields; match it positionally or as a unit variant",
                pattern.line,
                pattern.column,
            )
            self._ann_type(pattern, expected)
            return
        self._ann_type(pattern, expected)
        pattern._typed_ann["enum"] = enum.name
        enum_def = self._type_def_path(enum.name)
        if enum_def is not None:
            pattern._typed_ann["enum_def"] = enum_def
        pattern._typed_ann["variant_index"] = next(
            i for i, v in enumerate(enum.variants) if v is variant
        )
        subst_n = dict(
            zip(
                [p.name for p in enum.params],
                _split_args(expanded) if expanded is not None else [],
            )
        )
        ftypes_n = [
            _subst_type_str(_type_str(f), subst_n) for f in variant.fields
        ]
        fname_to_ft = dict(zip(variant.field_names, ftypes_n))
        seen_n: set[str] = set()
        rest = False
        for sf in pattern.named_fields or []:
            if sf.pattern is None and sf.name == "..":
                rest = True
                continue
            if sf.name not in fname_to_ft:
                self._record_error(
                    f"variant '{enum.name}::{variant.name}' has no field "
                    f"'{sf.name}'",
                    sf.line,
                    sf.column,
                )
                continue
            if sf.name in seen_n:
                self._record_error(
                    f"duplicate field '{sf.name}' in variant pattern",
                    sf.line,
                    sf.column,
                )
                continue
            seen_n.add(sf.name)
            ft = fname_to_ft[sf.name]
            if sf.pattern is None:
                # 简写: 按字段名绑定
                self._declare(VarInfo(
                    sf.name,
                    ft,
                    sf.line,
                    sf.column,
                    "let",
                    node=sf,
                ))
                self._ann_type(sf, ft)
            else:
                self._check_pattern(sf.pattern, ft, sf)
        missing_n = [n for n in variant.field_names if n not in seen_n]
        if missing_n and not rest:
            self._record_error(
                f"variant pattern for '{enum.name}::{variant.name}' is "
                f"missing field(s): {', '.join(missing_n)} (add `..` to "
                "ignore remaining fields)",
                pattern.line,
                pattern.column,
            )
        # 后端按名寻址: 声明序的 field_types 表 (键 = 字段名)
        pattern._typed_ann["field_types"] = {
            name: self._type_info_enriched(ft)
            for name, ft in zip(variant.field_names, ftypes_n)
        }
        pattern._typed_ann["variant_field_names"] = list(
            variant.field_names
        )

    def _check_condition(self: "_Analyzer", cond: Node) -> None:
        t = self._check_expr(cond)
        if t is not None and not self._compat_types("Bool", t):
            self._record_error(
                f"condition must be Bool, got {self._fmt_type(t)}",
                cond.line,
                cond.column,
            )

    def _fold_expr(
        self: "_Analyzer",
        expr: Optional[Node],
        folding: Optional[set[str]] = None,
    ) -> Optional[Union[int, float]]:
        """Fold a constant expression, including references to local
        variables whose value is compile-time known (``let t2: UInt8 =
        127 + 1;`` makes later ``t2`` uses fold to 128) and no-argument
        calls to functions whose body is ``return <constant>;``.

        bug-60: BinOp nodes fold here directly (each side through this
        method) so *variable-carried* known values compose: with
        ``let a: UInt32 = 0xffffffff;`` the expression ``a + 1`` folds to
        4294967296 and the overflow check can see it.  The pure-literal
        fast path in :func:`_const_number` is unchanged."""
        if expr is None:
            return None
        if isinstance(expr, BinOp):
            left = self._fold_expr(expr.left, folding)
            right = self._fold_expr(expr.right, folding)
            if left is None or right is None:
                return None
            op = expr.op
            if op == TokenKind.PLUS:
                return left + right
            if op == TokenKind.MINUS:
                return left - right
            if op == TokenKind.STAR:
                return left * right
            if op == TokenKind.SLASH:
                if right == 0:
                    return None
                if isinstance(left, int) and isinstance(right, int):
                    return left // right
                return left / right
            if op == TokenKind.PERCENT:
                if right == 0:
                    return None
                return left % right
            if isinstance(left, int) and isinstance(right, int):
                if op == TokenKind.SHL:
                    return left << right
                if op == TokenKind.SHR:
                    return left >> right
                if op == TokenKind.AMP:
                    return left & right
                if op == TokenKind.PIPE:
                    return left | right
                if op == TokenKind.CARET:
                    return left ^ right
            return None
        folded = _const_number(expr, self.const_values, self.const_floats)
        if folded is not None:
            return folded
        if isinstance(expr, Name) and len(expr.parts) == 1:
            info = self._lookup(expr.parts[0])
            if info is not None:
                return info.folded
        if (
            isinstance(expr, Call)
            and isinstance(expr.callee, Name)
            and len(expr.callee.parts) == 1
            and not expr.args
        ):
            name = expr.callee.parts[0]
            if folding is not None and name in folding:
                return None  # recursion: the value is not known
            if name in self.functions:
                if name not in self.fn_folded:
                    self.fn_folded[name] = self._fold_fn_return(
                        self.functions[name]
                    )
                return self.fn_folded[name]
        return None

    def _fold_fn_return(
        self: "_Analyzer", fn: FnDecl
    ) -> Optional[Union[int, float]]:
        """Fold a top-level function's return value when its whole body is a
        single ``return <constant>;`` (interprocedural constant
        propagation).  Functions with parameters or with any other statement
        are left unknown."""
        if fn.params or fn.body is None or len(fn.body.stmts) != 1:
            return None
        stmt = fn.body.stmts[0]
        if not isinstance(stmt, ReturnStmt) or stmt.value is None:
            return None
        if fn.name in self._folding_fns:
            return None
        self._folding_fns.add(fn.name)
        try:
            return self._fold_expr(stmt.value, self._folding_fns)
        finally:
            self._folding_fns.discard(fn.name)

    def _check_literal_range(self: "_Analyzer", target: Optional[str], value: Optional[Node]) -> None:
        """Reject integer literals that do not fit the declared type's width
        (e.g. ``-1`` into ``UInt``), fractional constants into integer types,
        and constants that do not fit / are not exactly representable in
        ``Float`` (f32).

        bug-60: the same bounds also apply to *expressions* whose whole
        value is compile-time known (``0xffffffff + 1``, ``let a = max;
        a + 1``).  The check below is therefore the single authority for
        "folded value vs declared type"; :meth:`_check_expr_range` routes
        every typed expression position (BinOp results included) through
        it, and :attr:`_overflow_checked` keeps inner/outer checks from
        reporting the same node twice.
        """
        if target is None or value is None:
            return
        folded = self._fold_expr(value)
        if folded is None:
            return
        expanded = self._expand_type(target)
        if expanded is None:
            return
        base = _base(expanded)
        if base == "Float":
            self._check_float_const(folded, value)
            return
        if base == "Float64":
            self._check_float64_const(folded, value)
            return
        bounds = _BUILTIN_RANGES.get(base)
        if bounds is None:
            return
        # bug-60 dedup: a BinOp pass that already validated this node
        # against the *same* type stays silent; a different target width
        # still gets its own check (``e: Int8 = d + 1`` with ``d: Int8 =
        # 127`` — the BinOp check sees Int bounds, the target check Int8).
        if (id(value), base) in self._overflow_checked:
            return
        self._overflow_checked.add((id(value), base))
        if isinstance(folded, float):
            if not folded.is_integer():
                self._record_error(
                    f"value {folded:g} is not an integer and does not fit in {base}",
                    value.line,
                    value.column,
                )
                return
            folded = int(folded)
        lo, hi = bounds
        if folded < lo or folded > hi:
            self._record_error(
                f"value {folded} does not fit in {base}",
                value.line,
                value.column,
            )

    def _check_int_literal_bounds(self: "_Analyzer", expr: IntLit) -> bool:
        """字面量的绝对上限: 正数/十六进制 ≤ u64::MAX, 负数 ≥ i64::MIN
        (i64/u64 是当前最大整数, 放不下直接报错, 没有更大的容器)。
        Returns False (and reports) when out of bounds."""
        value = expr.value
        if value >= 0 and value > _UINT64_MAX:
            self._record_error(
                f"integer literal {value} does not fit in UInt64 "
                "(the widest integer type)",
                expr.line,
                expr.column,
            )
            return False
        if value < _INT64_MIN:
            self._record_error(
                f"integer literal {value} does not fit in Int64 "
                "(the widest integer type)",
                expr.line,
                expr.column,
            )
            return False
        return True

    def _check_expr_range(
        self: "_Analyzer",
        result: Optional[str],
        expr: Node,
        left: Optional[str] = None,
        right: Optional[str] = None,
    ) -> None:
        """bug-60: range-check a BinOp's *folded value* against the type the
        expression itself produces (``UInt32 + Int -> UInt32``, so
        ``a + 1`` with a known ``a: UInt32 = 0xffffffff`` overflows even
        with no declared target around it).  Expressions that are not
        fully known fold to ``None`` and pass silently — runtime overflow
        is the wrapping semantics the ``std::expansion`` traits exist for,
        not an SA error.  (node, base) pairs already checked are skipped
        so the enclosing target check does not report the same value twice
        (and a *different* target type still gets its own check)."""
        if result is None:
            return
        # Untyped literal math: both operands are the literal defaults.
        if left in ("Int", "UInt") and right in ("Int", "UInt"):
            return
        expanded = self._expand_type(result)
        if expanded is None:
            return
        base = _base(expanded)
        bounds = _BUILTIN_RANGES.get(base)
        if bounds is None:
            return
        key = (id(expr), base)
        if key in self._overflow_checked:
            return
        self._overflow_checked.add(key)
        folded = self._fold_expr(expr)
        if folded is None or isinstance(folded, bool) or isinstance(folded, float):
            return
        lo, hi = bounds
        if folded < lo or folded > hi:
            self._record_error(
                f"value {folded} does not fit in {base}",
                expr.line,
                expr.column,
            )

    def _check_float_const(self: "_Analyzer", folded: Union[int, float], value: Node) -> None:
        """Validate a folded constant against Float (f32): it must be finite,
        within f32's range, and integral values must be exactly
        representable (e.g. ``16777216 + 1`` is rejected because f32 cannot
        represent 16777217)."""
        if isinstance(folded, float) and not math.isfinite(folded):
            self._record_error(
                "value is not finite and does not fit in Float",
                value.line,
                value.column,
            )
            return
        if abs(folded) > _FLOAT32_MAX:
            self._record_error(
                f"value {folded} does not fit in Float",
                value.line,
                value.column,
            )
            return
        if isinstance(folded, float) and folded.is_integer():
            folded = int(folded)
        if isinstance(folded, int):
            f32 = struct.unpack("!f", struct.pack("!f", float(folded)))[0]
            if int(f32) != folded:
                self._record_error(
                    f"value {folded} is not exactly representable in Float",
                    value.line,
                    value.column,
                )

    def _check_float64_const(self: "_Analyzer", folded: Union[int, float], value: Node) -> None:
        """Validate a folded constant against Float64 (f64): finite, within
        f64's range, and integral values exactly representable."""
        if isinstance(folded, float) and not math.isfinite(folded):
            self._record_error(
                "value is not finite and does not fit in Float64",
                value.line,
                value.column,
            )
            return
        if abs(folded) > _FLOAT64_MAX:
            self._record_error(
                f"value {folded} does not fit in Float64",
                value.line,
                value.column,
            )
            return
        if isinstance(folded, float) and folded.is_integer():
            folded = int(folded)
        if isinstance(folded, int):
            f64 = struct.unpack("!d", struct.pack("!d", float(folded)))[0]
            if int(f64) != folded:
                self._record_error(
                    f"value {folded} is not exactly representable in Float64",
                    value.line,
                    value.column,
                )

    def _refinement(
        self: "_Analyzer", t: Optional[str]
    ) -> Optional[tuple[str, Block, str]]:
        """Return ``(label, block, var_name)`` when ``t``'s base is a refined
        type (``type X = ... where { ... }``), following alias chains."""
        if t is None:
            return None
        base = _base(t)
        seen: set[str] = set()
        while base not in seen:
            seen.add(base)
            decl = self.type_aliases.get(base)
            if decl is None:
                return None
            if decl.where is not None:
                return f"refinement of '{base}'", decl.where, "self"
            base = _base(_type_str(decl.base))
        return None

    def _check_refined_value(
        self: "_Analyzer",
        target: Optional[str],
        value: Optional[Node],
        field: Optional[Field] = None,
    ) -> None:
        """Compile-time refinement check.

        When a value constant-folds and the expected type is refined (a
        ``type`` declaration with ``where``, or a field with an inline
        validation block), every predicate is evaluated and violations are
        reported.  Non-foldable values are left to runtime checks.
        """
        if value is None:
            return
        specs: list[tuple[str, Block, str]] = []
        refined = self._refinement(target)
        if refined is not None:
            specs.append(refined)
        if field is not None and field.validation is not None:
            specs.append((
                f"validation of field '{field.name}'",
                field.validation,
                field.name,
            ))
        if not specs:
            return
        folded = self._fold_expr(value)
        if folded is None:
            return  # not compile-time known; runtime check applies
        for label, block, var_name in specs:
            for stmt in block.stmts:
                if not isinstance(stmt, ExprStmt):
                    continue
                ok = _eval_refinement(
                    stmt.expr,
                    var_name,
                    folded,
                    self.const_values,
                    self.const_floats,
                )
                if ok is False:
                    self._record_error(
                        f"value {folded:g} does not satisfy {label}",
                        value.line,
                        value.column,
                    )
                    return

    def _check_constructor_field_flow(
        self: "_Analyzer",
        fn: FnDecl,
        owner_name: Optional[str],
        params: list[Param],
        call_args: list[Arg],
    ) -> None:
        """Validate foldable call arguments against the fields they flow into.

        For the common constructor idiom ``fn new(...) -> Self {
        return Self { field: param, ... }; }``, a call-site argument that
        lands in a refined field (``age: Age``) is checked against that
        field's constraints, so ``User::new(..., 999)`` is rejected even
        though the parameter itself is declared as plain ``Int``.
        """
        if fn.body is None or owner_name is None:
            return
        if any(a.unpack for a in call_args):
            return
        for stmt in fn.body.stmts:
            if not isinstance(stmt, ReturnStmt):
                continue
            ctor = stmt.value
            if not isinstance(ctor, StructConstruct):
                continue
            ctor_base = owner_name if ctor.type.name == "Self" else ctor.type.name
            if _base(ctor_base) != owner_name:
                continue
            struct = self.structs.get(owner_name)
            if struct is None:
                continue
            fields = [f for f in struct.fields if not f.static]
            if len(fields) != len(ctor.args) or len(fields) != len(params):
                continue
            param_index = {p.name: i for i, p in enumerate(params)}
            for f, arg in zip(fields, ctor.args):
                if not isinstance(arg, Name) or len(arg.parts) != 1:
                    continue
                pi = param_index.get(arg.parts[0])
                if pi is None or pi >= len(call_args):
                    continue
                self._check_refined_value(
                    _type_str(f.type), call_args[pi].value, f
                )

    def _classify_refinement_bound(
        self: "_Analyzer",
        cond: Node,
        var_name: str,
        lo: int,
        hi: int,
    ) -> Optional[str]:
        """Classify a ``var OP constant`` comparison against the base type's
        value range: ``"always_true"`` (can never reject a value),
        ``"always_false"`` (can never accept one), or ``None``."""
        if not isinstance(cond, BinOp):
            return None
        op = cond.op
        if op not in _SWAP_COMPARE:
            return None

        def is_var(node: Node) -> bool:
            return (
                isinstance(node, Name)
                and len(node.parts) == 1
                and node.parts[0] == var_name
            )

        if is_var(cond.left) and not is_var(cond.right):
            constant = _const_number(
                cond.right, self.const_values, self.const_floats
            )
        elif is_var(cond.right) and not is_var(cond.left):
            op = _SWAP_COMPARE[op]
            constant = _const_number(
                cond.left, self.const_values, self.const_floats
            )
        else:
            return None
        if not isinstance(constant, (int, float)):
            return None

        if op == TokenKind.NOT_LT:
            op = TokenKind.GE
        elif op == TokenKind.NOT_GT:
            op = TokenKind.LE
        if op == TokenKind.LT:
            if constant > hi:
                return "always_true"
            if constant <= lo:
                return "always_false"
        elif op == TokenKind.LE:
            if constant >= hi:
                return "always_true"
            if constant < lo:
                return "always_false"
        elif op == TokenKind.GT:
            if constant < lo:
                return "always_true"
            if constant >= hi:
                return "always_false"
        elif op == TokenKind.GE:
            if constant <= lo:
                return "always_true"
            if constant > hi:
                return "always_false"
        elif op == TokenKind.EQ:
            if constant < lo or constant > hi:
                return "always_false"
        elif op == TokenKind.NE:
            if constant < lo or constant > hi:
                return "always_true"
        return None

    def _warn_dead_refinement(
        self: "_Analyzer",
        cond: Node,
        var_name: str,
        base: str,
        lo: int,
        hi: int,
    ) -> None:
        """Warn about refinement clauses whose bound is outside the base
        type's representable range (e.g. ``self < 256`` on Int8)."""
        if isinstance(cond, BinOp) and cond.op in (TokenKind.AND, TokenKind.OR):
            self._warn_dead_refinement(cond.left, var_name, base, lo, hi)
            self._warn_dead_refinement(cond.right, var_name, base, lo, hi)
            return
        if isinstance(cond, UnaryOp) and cond.op == TokenKind.NOT:
            inner = self._classify_refinement_bound(cond.operand, var_name, lo, hi)
            if inner == "always_true":
                self._record_warning(
                    f"refinement condition '{_expr_str(cond.operand)}' can never "
                    f"be satisfied for {base} (values {lo}..{hi})",
                    cond.line,
                    cond.column,
                )
            elif inner == "always_false":
                self._record_warning(
                    f"refinement condition '{_expr_str(cond.operand)}' can never "
                    f"be violated for {base} (values {lo}..{hi})",
                    cond.line,
                    cond.column,
                )
            return
        kind = self._classify_refinement_bound(cond, var_name, lo, hi)
        if kind == "always_true":
            self._record_warning(
                f"refinement condition '{_expr_str(cond)}' can never be violated "
                f"for {base} (values {lo}..{hi})",
                cond.line,
                cond.column,
            )
        elif kind == "always_false":
            self._record_warning(
                f"refinement condition '{_expr_str(cond)}' can never be satisfied "
                f"for {base} (values {lo}..{hi})",
                cond.line,
                cond.column,
            )

    def _check_const_div_zero(self: "_Analyzer", expr: Node) -> None:
        """Reject division by a foldable zero in constant expressions."""
        if isinstance(expr, BinOp):
            if expr.op in (TokenKind.SLASH, TokenKind.PERCENT):
                right = _const_int(expr.right, self.const_values)
                if right == 0:
                    self._record_error(
                        "division by zero in constant expression",
                        expr.line,
                        expr.column,
                    )
            self._check_const_div_zero(expr.left)
            self._check_const_div_zero(expr.right)
        elif isinstance(expr, UnaryOp):
            self._check_const_div_zero(expr.operand)
