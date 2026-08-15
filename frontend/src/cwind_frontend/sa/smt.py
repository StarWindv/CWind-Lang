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
    _BUILTIN_RANGES,
    _FLOAT32_MAX,
    _FLOAT64_MAX,
    _base,
    _common_type,
    _split_args,
    _subst_type_str,
    _type_info,
    _type_str,
)
from ..ast_components.ast import (
    Arg,
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
    ForStmt,
    IfLetBranch,
    IfLetStmt,
    IfStmt,
    IntLit,
    LetStmt,
    LitPattern,
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
    WhileStmt,
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
        self.current_owner = owner
        self.current_owner_type = owner_type if owner_type is not None else owner
        self.active_generics = saved_generics | generic
        self._push_scope()
        for p in fn.params:
            ptype: Optional[str]
            if p.name == "self" and owner is not None:
                ptype = owner_type if owner_type is not None else owner
            else:
                ptype = _type_str(p.type) if p.type is not None else None
            self._declare(VarInfo(p.name, ptype, p.line, p.column, "param", node=p))
            self._ann_type(p, ptype)
            if p.type is not None:
                self._annotate_type_node(p.type)
        ret = _type_str(fn.return_type) if fn.return_type is not None else "None"
        if ret == "Self":
            if isinstance(owner, str):
                ret = owner_type if owner_type is not None else owner
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
        try:
            if fn.body is not None:
                self._check_block(fn.body, ret)
        finally:
            self.defined -= generic
            self.active_generics = saved_generics
        if ret != "None" and fn.body is not None and not _has_return(fn.body):
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

    def _check_block(self: "_Analyzer", block: Block, return_type: str) -> None:
        self._push_scope()
        for stmt in block.stmts:
            self._check_stmt(stmt, return_type)
        self._pop_scope()

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
            declared = _type_str(stmt.type) if stmt.type is not None else None
            value = self._check_expr(stmt.value) if stmt.value is not None else None
            if stmt.type is not None:
                self._check_type(stmt.type, stmt)
            known = (
                declared is not None
                and (_base(declared) in BUILTIN_TYPES or declared in self.defined)
            )
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
                node=stmt,
                folded=folded_init,
            ))
            self._ann_type(stmt, declared)
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
            value = self._check_expr(stmt.value)
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
            self._check_expr(stmt.expr)
        elif isinstance(stmt, IfStmt):
            self._check_condition(stmt.cond)
            self._check_block(stmt.then, return_type)
            for e in stmt.elifs:
                self._check_condition(e.cond)
                self._check_block(e.body, return_type)
            if stmt.else_ is not None:
                self._check_block(stmt.else_, return_type)
        elif isinstance(stmt, MatchStmt):
            self._check_match(stmt, return_type)
        elif isinstance(stmt, IfLetStmt):
            self._check_if_let(stmt, return_type)
        elif isinstance(stmt, WhileStmt):
            self._check_condition(stmt.cond)
            self.loop_depth += 1
            try:
                self._check_block(stmt.body, return_type)
            finally:
                self.loop_depth -= 1
        elif isinstance(stmt, ForStmt):
            if stmt.type is not None:
                self._check_type(stmt.type, stmt)
            iterable = self._check_expr(stmt.iterable)
            var_type = self._element_type(iterable)
            self._push_scope()
            self._declare(VarInfo(
                stmt.var, var_type, stmt.line, stmt.column, "let", node=stmt
            ))
            self.loop_depth += 1
            try:
                self._check_block(stmt.body, return_type)
            finally:
                self.loop_depth -= 1
            self._pop_scope()
            if iterable is not None:
                stmt._typed_ann["iterable_type"] = _type_info(
                    self._expand_type(iterable), self._opaque_names()
                )
            if var_type is not None:
                stmt._typed_ann["var_type"] = _type_info(
                    self._expand_type(var_type), self._opaque_names()
                )
        elif isinstance(stmt, Block):
            self._check_block(stmt, return_type)
        elif isinstance(stmt, (BreakStmt, ContinueStmt)):
            if self.loop_depth == 0:
                keyword = "break" if isinstance(stmt, BreakStmt) else "continue"
                self._record_error(
                    f"'{keyword}' can only be used inside a loop",
                    stmt.line,
                    stmt.column,
                )

    def _check_match(
        self: "_Analyzer",
        stmt: MatchStmt,
        return_type: Optional[str] = None,
        *,
        as_expr: bool = False,
    ) -> Optional[str]:
        """Check ``match``: the subject once, every arm's pattern in its own
        scope, guards as Bool conditions, and overall exhaustiveness.

        Block arms are statement-style; expression arms make the match a
        value (Rust style) and all arms must agree on the form.  Returns the
        common value type for expression matches, else ``None``.
        """
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
                self._check_block(arm.body, return_type or "None")
            else:
                expr_arms += 1
                arm._typed_ann["body_kind"] = "expr"
                t = self._check_expr(arm.body)
                if t is not None:
                    arm._typed_ann["body_type"] = _type_info(
                        self._expand_type(t), self._opaque_names()
                    )
                    arm_types.append(t)
            self._pop_scope()
        if block_arms and expr_arms:
            self._record_error(
                "match arms must be all blocks or all expressions",
                stmt.line,
                stmt.column,
            )
            return None
        if as_expr and block_arms:
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
            common = _common_type(arm_types)
            if common is None and arm_types:
                self._record_error(
                    "match arms have incompatible value types: "
                    + ", ".join(self._fmt_type(t) for t in arm_types),
                    stmt.line,
                    stmt.column,
                )
            else:
                self._ann_type(stmt, common)
            return common
        return None

    def _check_if_let(self: "_Analyzer", stmt: IfLetStmt, return_type: str) -> None:
        """Check ``if let`` and its ``elif`` / ``else`` chain."""
        value = self._check_expr(stmt.value)
        if value is not None:
            stmt._typed_ann["value_type"] = _type_info(
                self._expand_type(value), self._opaque_names()
            )
        self._push_scope()
        self._check_pattern(stmt.pattern, value, stmt)
        self._check_block(stmt.then, return_type)
        self._pop_scope()
        for branch in stmt.elifs:
            self._push_scope()
            if branch.cond is not None:
                self._check_condition(branch.cond)
                self._check_block(branch.body, return_type)
            else:
                bvalue = self._check_expr(branch.value)
                if bvalue is not None:
                    branch._typed_ann["value_type"] = _type_info(
                        self._expand_type(bvalue), self._opaque_names()
                    )
                self._check_pattern(branch.pattern, bvalue, branch)
                self._check_block(branch.body, return_type)
            self._pop_scope()
        if stmt.else_ is not None:
            self._check_block(stmt.else_, return_type)

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
        context: Node,
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
                _type_info(self._expand_type(t), self._opaque_names())
                for t in args
            ]
            for elem, t in zip(pattern.elems, args):
                self._check_pattern(elem, t, pattern)
            return
        if isinstance(pattern, StructPattern):
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
                f.name: _type_info(
                    _subst_type_str(_type_str(f.type), subst),
                    self._opaque_names(),
                )
                for f in fields
            }
            return
        if isinstance(pattern, EnumPattern):
            if len(pattern.path) != 2:
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
            if pattern.path[0] != enum.name:
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
            self._ann_type(pattern, expected)
            pattern._typed_ann["enum"] = enum.name
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
        self._record_error(
            "unsupported pattern",
            pattern.line,
            pattern.column,
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
        calls to functions whose body is ``return <constant>;``."""
        if expr is None:
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
        ``Float`` (f32)."""
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
                f"validation of '{field.name}'",
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
