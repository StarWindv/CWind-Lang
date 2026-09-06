"""SA pre-pass desugaring (todo-165/184/186): while-let, while, if and
for-in lowering, plus which-hook registration, run before pass 1."""

from __future__ import annotations

from dataclasses import fields as _fields
from typing import TYPE_CHECKING, Optional

from ..ast_components.ast import (
    Arg,
    Attribute,
    BinOp,
    Block,
    BoolLit,
    BreakStmt,
    Call,
    Closure,
    ElifBranch,
    EnumPattern,
    TupleLit,
    TuplePattern,
    ExtraDecl,
    ExprStmt,
    ForStmt,
    IfLetBranch,
    IfLetStmt,
    IfStmt,
    ImplDecl,
    LetChainSeg,
    BindPattern,
    LetStmt,
    LitPattern,
    LoopStmt,
    MatchArm,
    MatchStmt,
    Name,
    Node,
    Program,
    ReturnStmt,
    TryExpr,
    WhileLetStmt,
    WhileStmt,
    WildcardPattern,
)
from ..ast_components.token import TokenKind
from ..parser.core import ParserCore

if TYPE_CHECKING:
    from .analyzer import _Analyzer


class DesugarPass:

    # -- todo-165: while-let desugar ----------------------------------------
    def _desugar_while_lets(self: "_Analyzer", program: Program) -> None:
        """Rewrite every ``while let`` into ``while (true) { match ... }``.

        todo-165 is pure syntax sugar, so the analysis and the backend only
        ever see the two constructs that already exist.  The Rust 2024
        let-chain semantics map onto nested matches: each ``let`` segment
        opens a match layer on its value (arm pattern = segment pattern),
        the boolean segments following it become that arm's guard, and the
        final arm body is the loop body.  A failed pattern or a false
        guard falls through to the ``_ => { break; }`` arm.  Runs before
        which hooks and pass 1 so the rewritten nodes flow through the
        ordinary analysis and codegen unchanged.
        """
        if getattr(program, "_while_let_desugared", False):
            return
        def walk_items(items: list[Node]) -> None:
            for item in items:
                self._desugar_while_lets_node(item)
        walk_items(program.items)
        files = getattr(program, "_module_file_programs", None)
        if isinstance(files, dict):
            for child in files.values():
                walk_items(child.items)
        program._while_let_desugared = True

    def _desugar_while_lets_node(self: "_Analyzer", node: Node) -> None:
        for f in _fields(node):
            if f.name in ("line", "column"):
                continue
            value = getattr(node, f.name)
            if isinstance(value, WhileLetStmt):
                setattr(node, f.name, self._desugar_while_let(value))
            elif isinstance(value, Node):
                self._desugar_while_lets_node(value)
            elif isinstance(value, list):
                for i, x in enumerate(value):
                    if isinstance(x, WhileLetStmt):
                        value[i] = self._desugar_while_let(x)
                    elif isinstance(x, Node):
                        self._desugar_while_lets_node(x)

    def _desugar_while_let(self: "_Analyzer", stmt: WhileLetStmt) -> WhileStmt:
        line, column = stmt.line, stmt.column
        segments = stmt.segments
        if not segments:
            # Parser guarantees at least one operand; defensive only.
            return WhileStmt(
                line, column, BoolLit(line, column, True, "true"), stmt.body,
                label=stmt.label,
            )
        if len(segments) == 1 and segments[0].pattern is None:
            # Single boolean operand: identical to a plain ``while``.
            return WhileStmt(line, column, segments[0].value, stmt.body, label=stmt.label)
        # Left-to-right nesting: boolean segments before the first ``let``
        # fold into the while condition; each ``let`` segment opens one
        # match layer on its value whose arm pattern is the segment's
        # pattern, whose guard AND-folds the boolean segments right after
        # it, and whose body is the next layer (or the loop body).  This
        # preserves Rust 2024 short-circuit order: E0 → P0 → B1 → E1 → P1.
        first_let = next(
            (
                i
                for i, seg in enumerate(segments)
                if seg.pattern is not None
            ),
            None,
        )
        if first_let is None:
            # Parser never produces a let-less chain; defensive only.
            return WhileStmt(
                line,
                column,
                self._fold_bool_chain(
                    [seg.value for seg in segments]
                )
                or BoolLit(line, column, True, "true"),
                stmt.body,
                label=stmt.label,
            )
        cond = self._fold_bool_chain(
            [seg.value for seg in segments[:first_let]]
        )
        if cond is None:
            cond = BoolLit(line, column, True, "true")
        inner_body = self._desugar_chain_body(segments[first_let:], stmt.body)
        return WhileStmt(line, column, cond, inner_body, label=stmt.label)

    def _desugar_chain_body(
        self: "_Analyzer", segments: list["LetChainSeg"], loop_body: "Block"
    ) -> "Block":
        """The nested match for ``segments`` (all starting with a ``let``).

        Each layer's arm pattern is the segment's pattern, its guard the
        AND of the boolean segments between it and the next ``let``, and
        the final layer's arm body is the loop body.
        """
        line, column = segments[0].line, segments[0].column
        first = segments[0]
        rest = segments[1:]
        # Boolean segments right after this let are the arm's guard.
        guard_end = 0
        while guard_end < len(rest) and rest[guard_end].pattern is None:
            guard_end += 1
        guard = self._fold_bool_chain(
            [seg.value for seg in rest[:guard_end]]
        )
        if guard_end < len(rest):
            inner_body = self._desugar_chain_body(
                rest[guard_end:], loop_body
            )
        else:
            inner_body = loop_body
        arm = MatchArm(
            first.line, first.column, first.pattern, guard, inner_body
        )
        break_arm = MatchArm(
            first.line,
            first.column,
            WildcardPattern(first.line, first.column),
            None,
            Block(first.line, first.column, [BreakStmt(first.line, first.column)]),
        )
        match = MatchStmt(first.line, first.column, first.value, [arm, break_arm])
        return Block(line, column, [match])

    def _fold_bool_chain(
        self: "_Analyzer", parts: list[Node]
    ) -> Optional[Node]:
        """AND-fold *parts* (source order); ``None`` when empty."""
        if not parts:
            return None
        acc = parts[0]
        for part in parts[1:]:
            acc = BinOp(
                acc.line, acc.column, acc, TokenKind.AND, part
            )
        return acc

    # -- todo-168: let-else → match ----------------------------------------
    def _desugar_let_elses(self: "_Analyzer", program: Program) -> None:
        """Rewrite ``let P = E else B;`` into a plain let + match.

        doc(analysis/match.md §2.6): ``let Some(x) = opt else { return; }``
        降为 ``let x = match opt { Some(x) => x, _ => return };`` — 绑定名
        直接取自模式 (BindPattern; 多绑定模式先不支持, 报诊断)。
        else 块必须发散 (return/break/continue/panic) — 这里只做结构
        检查, 发散性与 match 臂一致由既有分析兜底。
        """
        if getattr(program, "_let_else_desugared", False):
            return

        def walk_items(items: list[Node]) -> None:
            for item in items:
                self._desugar_let_elses_node(item)

        walk_items(program.items)
        files = getattr(program, "_module_file_programs", None)
        if isinstance(files, dict):
            for child in files.values():
                walk_items(child.items)
        program._let_else_desugared = True

    def _desugar_let_elses_node(self: "_Analyzer", node: Node) -> None:
        for f in _fields(node):
            if f.name in ("line", "column"):
                continue
            value = getattr(node, f.name)
            if isinstance(value, LetStmt) and value.else_block is not None:
                # 先递归进旧节点 (value/else_block 内可有嵌套 let-else),
                # 再整体替换; 产物自身的字段已被递归覆盖。
                self._desugar_let_elses_node(value)
                setattr(node, f.name, self._desugar_let_else(value))
            elif isinstance(value, Node):
                self._desugar_let_elses_node(value)
            elif isinstance(value, list):
                for i, x in enumerate(value):
                    if isinstance(x, LetStmt) and x.else_block is not None:
                        self._desugar_let_elses_node(x)
                        value[i] = self._desugar_let_else(x)
                    elif isinstance(x, Node):
                        self._desugar_let_elses_node(x)

    def _desugar_let_else(self: "_Analyzer", stmt: LetStmt) -> Node:
        """§2.6: ``let P = E else B;`` → ``let <bind> = match E { P =>
        <bind>, _ => B };`` — Rust 语义: 绑定落在**外围作用域** (这正是
        let-else 与 if-let 的区别)。

        - 单绑定: hit 臂是表达式臂 (``=> Name``), let 直接声明该名;
          miss 臂保持发散 Block — Rust 把它类型化为 ``!``, 与任意臂
          类型合一 (SA 的 as_expr 混合臂分支)。
        - 零绑定 (unit 变体, 如 ``let Option::None = ... else``): 模式
          不产生值, match 无表达式形态 — 整体降为**语句位** match
          (hit 臂空块, miss 臂发散块), 不合成 let。
        - 多绑定: 需要元组解构 let (todo-162), 先报诊断。
        """
        line, column = stmt.line, stmt.column
        pattern = stmt.pattern
        names = self._pattern_bound_names(pattern)
        if len(names) > 1:
            self._record_error(
                "let-else with multi-binding patterns requires tuple "
                "destructuring let (todo-162)",
                line,
                column,
            )
        if not names:
            # 零绑定: 语句位 match, hit 臂空块 (值被丢弃), miss 臂发散。
            hit_arm = MatchArm(
                line, column, pattern, None, Block(line, column, []),
            )
            miss_arm = MatchArm(
                line, column, WildcardPattern(line, column), None,
                stmt.else_block,
            )
            return MatchStmt(
                line, column, stmt.value, [hit_arm, miss_arm],
            )
        bind = names[0]
        hit_arm = MatchArm(
            line, column, pattern, None, Name(line, column, [bind]),
        )
        miss_arm = MatchArm(
            line, column, WildcardPattern(line, column), None,
            stmt.else_block,
        )
        match_expr = MatchStmt(
            line, column, stmt.value, [hit_arm, miss_arm],
        )
        return LetStmt(
            line, column, bind, None, match_expr,
            mutable=stmt.mutable,
        )

    def _pattern_bound_names(self: "_Analyzer", pattern: Node) -> list[str]:
        """Every name the pattern binds, in source order."""
        if isinstance(pattern, BindPattern):
            return [pattern.name]
        if isinstance(pattern, EnumPattern):
            names: list[str] = []
            for e in pattern.elems:
                names.extend(self._pattern_bound_names(e))
            return names
        if isinstance(pattern, TuplePattern):
            names = []
            for e in pattern.elems:
                names.extend(self._pattern_bound_names(e))
            return names
        return []

    # -- todo-190: ``E?`` → match -------------------------------------------
    def _desugar_tries(self: "_Analyzer", program: Program) -> None:
        """Rewrite every postfix ``E?`` (doc analysis/match.md §2.4).

        ::

            let x = bar()?;
        → ::
            let x = match bar() {
                Result::Ok($v) => $v,
                Result::Err($e) => { return Result::Err($e.into()); }
            };

        hit 臂是表达式臂 (载荷绑定), miss 臂是发散块 — 与 let-else 相同的
        混合臂形态 (SA as_expr 分支)。``?`` 之后的后缀 (.x / [i]) 挂在
        TryExpr 外层, 降糖后即 match 表达式的成员访问。语义约束由既有
        检查通用兜底: 函数不返回 Result 时 miss 臂的 return 报类型错,
        ``e.into()`` 需要 ``impl From<E2> for E1`` (无特判)。
        """
        if getattr(program, "_tries_desugared", False):
            return

        def walk_items(items: list[Node]) -> None:
            for item in items:
                self._desugar_tries_node(item, program)

        walk_items(program.items)
        files = getattr(program, "_module_file_programs", None)
        if isinstance(files, dict):
            for child in files.values():
                walk_items(child.items)
        program._tries_desugared = True

    def _desugar_tries_node(self: "_Analyzer", node: Node,
                            program: Program) -> None:
        for f in _fields(node):
            if f.name in ("line", "column"):
                continue
            value = getattr(node, f.name)
            if isinstance(value, TryExpr):
                # 后序: 先降操作数 (内层 ``?``), 再包裹自身。
                self._desugar_tries_node(value, program)
                setattr(node, f.name, self._desugar_try(value, program))
            elif isinstance(value, Node):
                self._desugar_tries_node(value, program)
            elif isinstance(value, list):
                for i, x in enumerate(value):
                    if isinstance(x, Node):
                        self._desugar_tries_node(x, program)

    def _desugar_try(self: "_Analyzer", stmt: TryExpr,
                     program: Program) -> MatchStmt:
        line, column = stmt.line, stmt.column
        vname = self._fresh_desugar_name(program, "tryv")
        ename = self._fresh_desugar_name(program, "trye")
        hit_arm = MatchArm(
            line, column,
            EnumPattern(line, column, ["Result", "Ok"], [
                BindPattern(line, column, vname),
            ]),
            None, Name(line, column, [vname]),
        )
        err_construct = Call(
            line, column,
            Name(line, column, ["Result", "Err"]),
            [Arg(line, column, Name(line, column, [ename]))],
        )
        miss_arm = MatchArm(
            line, column,
            EnumPattern(line, column, ["Result", "Err"], [
                BindPattern(line, column, ename),
            ]),
            None,
            Block(line, column, [ReturnStmt(line, column, err_construct)]),
        )
        return MatchStmt(line, column, stmt.expr, [hit_arm, miss_arm])

    # -- todo-184: while → loop + match -------------------------------------
    def _desugar_whiles(self: "_Analyzer", program: Program) -> None:
        """Rewrite every ``while`` into ``loop { match cond { ... } }``.

        doc(analysis/match.md §2.7): while 是 loop 的语法糖 —— 条件为
        true 的臂执行循环体, 兜底臂 ``break`` (文档示例写 ``continue``,
        语义上应为 break, 否则 while 永不退出)。todo-185 的标签原样
        搬到 LoopStmt 上。在 while-let 降糖之后运行, 让它产出的
        WhileStmt 一并降到基本形式; 后端从此不再处理 WhileStmt。
        """
        if getattr(program, "_whiles_desugared", False):
            return

        def walk_items(items: list[Node]) -> None:
            for item in items:
                self._desugar_whiles_node(item)

        walk_items(program.items)
        files = getattr(program, "_module_file_programs", None)
        if isinstance(files, dict):
            for child in files.values():
                walk_items(child.items)
        program._whiles_desugared = True

    def _desugar_whiles_node(self: "_Analyzer", node: Node) -> None:
        for f in _fields(node):
            if f.name in ("line", "column"):
                continue
            value = getattr(node, f.name)
            if isinstance(value, WhileStmt):
                # 先递归进旧节点 (体内可有嵌套 while), 再整体替换。
                self._desugar_whiles_node(value)
                setattr(node, f.name, self._desugar_while(value))
            elif isinstance(value, Node):
                self._desugar_whiles_node(value)
            elif isinstance(value, list):
                for i, x in enumerate(value):
                    if isinstance(x, WhileStmt):
                        self._desugar_whiles_node(x)
                        value[i] = self._desugar_while(x)
                    elif isinstance(x, Node):
                        self._desugar_whiles_node(x)

    def _desugar_while(self: "_Analyzer", stmt: WhileStmt) -> LoopStmt:
        line, column = stmt.line, stmt.column
        break_arm = MatchArm(
            line, column, WildcardPattern(line, column), None,
            Block(line, column, [BreakStmt(line, column)]),
        )
        true_arm = MatchArm(
            line, column,
            LitPattern(line, column, BoolLit(line, column, True, "true")),
            None, stmt.body,
        )
        match_stmt = MatchStmt(
            line, column, stmt.cond, [true_arm, break_arm]
        )
        return LoopStmt(
            line, column, Block(line, column, [match_stmt]),
            label=stmt.label,
        )

    # -- todo-184: if / if-let → match --------------------------------------
    def _desugar_ifs(self: "_Analyzer", program: Program) -> None:
        """Rewrite every ``if`` / ``if-let`` into ``match``.

        doc(analysis/match.md §2.1/§2.2): 条件臂 ``true => then``、兜底臂
        ``_ => <elif 链>``; elif 链自后向前折叠成通配臂里的嵌套 match,
        if-let 的匹配臂模式来自 ``let P = E``, 兜底臂接 elif 链。无
        else 时兜底臂为空块 (``=> ()`` 的对应物)。在 whiles 降糖之后
        运行; 后端从此不再处理 IfStmt。
        """
        if getattr(program, "_ifs_desugared", False):
            return

        def walk_items(items: list[Node]) -> None:
            for item in items:
                self._desugar_ifs_node(item)

        walk_items(program.items)
        files = getattr(program, "_module_file_programs", None)
        if isinstance(files, dict):
            for child in files.values():
                walk_items(child.items)
        program._ifs_desugared = True

    def _desugar_ifs_node(self: "_Analyzer", node: Node) -> None:
        for f in _fields(node):
            if f.name in ("line", "column"):
                continue
            value = getattr(node, f.name)
            if isinstance(value, (IfStmt, IfLetStmt)):
                self._desugar_ifs_node(value)
                setattr(node, f.name, self._desugar_if_kind(value))
            elif isinstance(value, Node):
                self._desugar_ifs_node(value)
            elif isinstance(value, list):
                for i, x in enumerate(value):
                    if isinstance(x, (IfStmt, IfLetStmt)):
                        self._desugar_ifs_node(x)
                        value[i] = self._desugar_if_kind(x)
                    elif isinstance(x, Node):
                        self._desugar_ifs_node(x)

    def _stmt_body(self: "_Analyzer", line: int, column: int,
                   body: Node) -> Node:
        """Statement-match arm bodies are Blocks; a nested MatchStmt (the
        elif fold) is wrapped so SA never reads it as an expression arm."""
        if isinstance(body, Block):
            return body
        return Block(line, column, [body])

    def _true_arm(self: "_Analyzer", line: int, column: int,
                  body: Node) -> MatchArm:
        return MatchArm(
            line, column,
            LitPattern(line, column, BoolLit(line, column, True, "true")),
            None, self._stmt_body(line, column, body),
        )

    def _wild_arm(self: "_Analyzer", line: int, column: int,
                  body: Node) -> MatchArm:
        return MatchArm(
            line, column, WildcardPattern(line, column), None,
            self._stmt_body(line, column, body),
        )

    def _desugar_if_kind(self: "_Analyzer", stmt: Node) -> MatchStmt:
        if isinstance(stmt, IfLetStmt):
            return self._desugar_if_let(stmt)
        return self._desugar_if(stmt)

    def _desugar_if(self: "_Analyzer", stmt: IfStmt) -> MatchStmt:
        line, column = stmt.line, stmt.column
        tail: Node = stmt.else_ if stmt.else_ is not None else Block(
            line, column, []
        )
        for branch in reversed(stmt.elifs):
            tail = MatchStmt(
                branch.line, branch.column, branch.cond,
                [self._true_arm(branch.line, branch.column, branch.body),
                 self._wild_arm(branch.line, branch.column, tail)],
            )
        return MatchStmt(
            line, column, stmt.cond,
            [self._true_arm(line, column, stmt.then),
             self._wild_arm(line, column, tail)],
        )

    def _desugar_if_let(self: "_Analyzer", stmt: IfLetStmt) -> MatchStmt:
        line, column = stmt.line, stmt.column
        tail: Node = stmt.else_ if stmt.else_ is not None else Block(
            line, column, []
        )
        for branch in reversed(stmt.elifs):
            if branch.pattern is not None:
                tail = MatchStmt(
                    branch.line, branch.column, branch.value,
                    [MatchArm(
                        branch.line, branch.column, branch.pattern, None,
                        branch.body,
                     ),
                     self._wild_arm(branch.line, branch.column, tail)],
                )
            else:
                tail = MatchStmt(
                    branch.line, branch.column, branch.cond,
                    [self._true_arm(branch.line, branch.column, branch.body),
                     self._wild_arm(branch.line, branch.column, tail)],
                )
        return MatchStmt(
            line, column, stmt.value,
            [MatchArm(line, column, stmt.pattern, None, stmt.then),
             self._wild_arm(line, column, tail)],
        )

    # -- todo-186: for-in → 迭代器协议 + loop + match ------------------------
    def _desugar_fors(self: "_Analyzer", program: Program) -> None:
        """Rewrite every ``for PATTERN in E`` into the iterator protocol.

        doc(analysis/match.md §2.5): ``for`` 是迭代器的语法糖 ——
        ``let mut iter = E.into_iter(); loop { match iter.next() {
        Option::Some(PATTERN) => body, Option::None => break } }``。
        容器遍历去特殊化 (groups.md §2): Vector 走用户已实现的
        iter_vec.wind 协议, Map/Set 走同构的 std 侧迭代器; 后端与
        SA 从此不再认识 ForStmt。头部模式原样成为 Some 臂的模式,
        元组解构 (§3.4) 由 match 的模式能力自然获得; 标签原样搬到
        LoopStmt。迭代器绑定的名字走宏卫生 mangling, 用户代码无法
        捕获也不会撞名; SA 对无注解 let 按初始化推断 (仅降糖产物
        会产生无注解 let)。
        """
        if getattr(program, "_fors_desugared", False):
            return

        def walk_items(items: list[Node]) -> None:
            for item in items:
                self._desugar_fors_node(item, program)

        walk_items(program.items)
        files = getattr(program, "_module_file_programs", None)
        if isinstance(files, dict):
            for child in files.values():
                walk_items(child.items)
        program._fors_desugared = True

    def _desugar_fors_node(self: "_Analyzer", node: Node,
                           program: Program) -> None:
        for f in _fields(node):
            if f.name in ("line", "column"):
                continue
            value = getattr(node, f.name)
            if isinstance(value, list):
                # 语句表: for 降糖成 (iter let, loop) 两条语句, 原位拼接;
                # let 必须在 loop 之外 (§2.5), 放进循环体里每轮都会拿
                # 新迭代器, 永不终止。
                spliced: list[Node] = []
                for x in value:
                    if isinstance(x, ForStmt):
                        # 先递归进旧节点 (体内可有嵌套 for), 再拼接替换。
                        self._desugar_fors_node(x, program)
                        spliced.extend(self._desugar_for(program, x))
                    else:
                        if isinstance(x, Node):
                            self._desugar_fors_node(x, program)
                        spliced.append(x)
                if len(spliced) != len(value):
                    value[:] = spliced
            elif isinstance(value, ForStmt):
                self._desugar_fors_node(value, program)
            elif isinstance(value, Node):
                self._desugar_fors_node(value, program)

    def _fresh_desugar_name(self: "_Analyzer", program: Program,
                            base: str) -> str:
        """A hygiene-mangled name for a desugar-synthesized binding.

        Shares the macro mangling format (``_m<context>_<name>``) so the
        binding can neither be captured by user code nor collide with a
        user binding of the same spelling.  Context ids start past the
        parse-time range (parsers count macro expansions from 0).
        """
        ctx = getattr(program, "_desugar_ctx_count", 1_000_000) + 1
        program._desugar_ctx_count = ctx
        return ParserCore.macro_mangle(ctx, base)

    def _desugar_for(
        self: "_Analyzer", program: Program, stmt: ForStmt
    ) -> tuple[LetStmt, LoopStmt]:
        """The two-statement lowering of one ``for`` (see
        :meth:`_desugar_fors`): ``(iter let, loop)`` — the iterator
        binding lands in the enclosing block, *before* the loop."""
        line, column = stmt.line, stmt.column
        iter_name = self._fresh_desugar_name(program, "iter")
        iter_ref = Name(line, column, [iter_name])
        # let mut iter = <iterable>.into_iter();
        into_call = Call(
            line, column,
            Attribute(line, column, stmt.iterable, "into_iter"),
            [],
        )
        iter_let = LetStmt(
            line, column, iter_name, None, into_call, mutable=True
        )
        # match iter.next() { Option::Some(PATTERN) => body, Option::None => break }
        next_call = Call(
            line, column, Attribute(line, column, iter_ref, "next"), []
        )
        some_arm = MatchArm(
            line, column,
            EnumPattern(line, column, ["Option", "Some"], [stmt.pattern]),
            None, stmt.body,
        )
        none_arm = MatchArm(
            line, column,
            EnumPattern(line, column, ["Option", "None"], []),
            None,
            Block(line, column, [BreakStmt(line, column)]),
        )
        match_stmt = MatchStmt(
            line, column, next_call, [some_arm, none_arm]
        )
        return (
            iter_let,
            LoopStmt(
                line, column,
                Block(line, column, [match_stmt]),
                label=stmt.label,
            ),
        )

    # -- todo-23/24: which-hook registration (redesigned) -------------------
    def _inline_which_hooks(self: "_Analyzer", program: Program) -> None:
        """Hook clause ``fn hook(&self), after ::target``.

        The hook and its target only need to live in the same **crate**
        (any extra/impl block of the same owner type), the hook must be
        a ``&self`` method, and it fires at the **call site** after the
        target method returns (the backend emits ``obj.hook()`` next to
        every call of the target).  SA only validates and records the
        association — the function bodies are left untouched, so the
        return-injection machinery is gone.
        """
        if getattr(program, "_which_inlined", False):
            return
        # Crate-wide method index: (owner, name) -> FnDecl.
        methods: dict[tuple[str, str], "FnDecl"] = {}
        hook_decls: list[tuple[str, "FnDecl"]] = []

        def scan(items: list[Node]) -> None:
            for item in items:
                if not isinstance(item, (ExtraDecl, ImplDecl)):
                    continue
                owner = item.struct.name
                for fn in item.methods:
                    methods.setdefault((owner, fn.name), fn)
                    if fn.which is not None:
                        hook_decls.append((owner, fn))

        scan(program.items)
        files = getattr(program, "_module_file_programs", None)
        if isinstance(files, dict):
            for child in files.values():
                scan(child.items)

        def self_is_ref(fnp: "FnDecl") -> bool:
            """True when *fnp*'s receiver is borrowed (``&self`` /
            ``&mut self`` / explicit ``self: &Type``): ownership must not
            move — the call-site hook runs on the same receiver after the
            target returns, and the hook itself reuses it too."""
            ps = fnp.params or []
            if not ps or ps[0].name != "self":
                return False
            t = ps[0].type
            return t is not None and bool(getattr(t, "ref", False))

        for owner, fn in hook_decls:
            if fn.body is None:
                continue
            if not self_is_ref(fn):
                self._record_error(
                    f"hook method '{fn.name}' must not move ownership of "
                    "its receiver — take self by reference (&self / "
                    "&mut self / self: &Type)",
                    fn.line,
                    fn.column,
                )
                continue
            if len(fn.params or []) != 1:
                self._record_error(
                    f"hook method '{fn.name}' must take exactly "
                    "'&self' (no extra parameters)",
                    fn.line,
                    fn.column,
                )
                continue
            if fn.return_type is not None:
                self._record_error(
                    f"hook method '{fn.name}' must not declare a "
                    "return type — the call site discards it",
                    fn.line,
                    fn.column,
                )
                continue
            target = methods.get((owner, fn.which))
            if target is not None and not self_is_ref(target):
                self._record_error(
                    f"hook target '{fn.which}' must not move ownership of "
                    "its receiver — take self by reference (&self / "
                    "&mut self / self: &Type); the hook fires on the same "
                    "receiver after the call returns",
                    fn.line,
                    fn.column,
                )
                continue
            if target is None:
                self._record_error(
                    f"hook target '{fn.which}' must be a method of "
                    f"'{owner}' in this crate",
                    fn.line,
                    fn.column,
                )
                continue
            if target is fn:
                self._record_error(
                    f"hook method '{fn.name}' cannot hook itself",
                    fn.line,
                    fn.column,
                )
                continue
            if target.which is not None:
                self._record_error(
                    f"hook target '{fn.which}' is itself a hook",
                    fn.line,
                    fn.column,
                )
                continue
            if target.body is None:
                self._record_error(
                    f"hook target '{fn.which}' must have a body",
                    fn.line,
                    fn.column,
                )
                continue
            key = (owner, fn.which)
            if key in self._which_hooked:
                self._record_error(
                    f"'{owner}::{fn.which}' already has a hook "
                    f"('{self._which_hooked[key]}')",
                    fn.line,
                    fn.column,
                )
                continue
            self._which_hooked[key] = fn.name
        program._which_inlined = True

    # -- todo-23/24 重设计: 钩子调用点发射 (前端降糖) ------------------------
    def _emit_which_hooks(self: "_Analyzer", program: Program) -> None:
        """Emit ``after`` hooks at every target call site, in the frontend.

        pass 3 检查被钩调用点时记录 :data:`_hook_sites`; 体检查结束后
        这里做语句级提升改写, 后端只看到普通方法调用 (只负责 codegen/GC,
        不感知钩子)。每个调用点

        ::

            <expr 位> obj.target(args) <expr 位>
        → ::
            let $r = <复合接收者>;        # 仅接收者含调用时
            let $t = obj.target(args);
            obj.hook();
            <原位替换为 $t>

        求值顺序由后序遍历保持 (内层调用先提升, receiver 先于实参);
        所有产物带完整 ann (binding/member/type), 与 "后端只消费 ann"
        的纪律一致。接收者判定按约束:
         - 被钩方法与钩子自身都不得移动接收者所有权 (self 参数必须是引用形态, 注册时已校验),
         - 钩子无返回值。
        """
        if getattr(program, "_hooks_emitted", False):
            return
        if self._hook_sites:
            sites = {
                id(call): hook_binding
                for call, hook_binding in self._hook_sites
            }
            self._hook_sites_by_call = sites
            self._hook_program = program
            self._walk_hook_blocks(program.items)
            files = getattr(program, "_module_file_programs", None)
            if isinstance(files, dict):
                for child in files.values():
                    self._walk_hook_blocks(child.items)
            self._hook_sites_by_call = {}
            self._hook_program = None
            self._hook_sites = []
        program._hooks_emitted = True

    def _walk_hook_blocks(self: "_Analyzer", nodes: object) -> None:
        """Rewrite every Block reachable from *nodes* (item/list/Node).

        Block 命中后由 :meth:`_rewrite_hook_block` 内部递归, 不再深入,
        避免双重处理。"""
        if isinstance(nodes, list):
            for x in nodes:
                self._walk_hook_blocks(x)
            return
        if not isinstance(nodes, Node):
            return
        for f in _fields(nodes):
            if f.name in ("line", "column"):
                continue
            value = getattr(nodes, f.name)
            if isinstance(value, Block):
                self._rewrite_hook_block(value)
            elif isinstance(value, Node):
                self._walk_hook_blocks(value)
            elif isinstance(value, list):
                for x in value:
                    if isinstance(x, Node):
                        self._walk_hook_blocks(x)

    def _rewrite_hook_block(self: "_Analyzer", block: Block) -> None:
        new: list[Node] = []
        for stmt in block.stmts:
            new.extend(self._rewrite_hook_stmt(stmt))
        block.stmts[:] = new

    def _rewrite_hook_stmt(self: "_Analyzer", stmt: Node) -> list[Node]:
        """One statement → (lifting prefix, rewritten statement)."""
        # 1. 先递归子语句块 (match 臂体 / loop 体 / 嵌套块)。
        for f in _fields(stmt):
            if f.name in ("line", "column"):
                continue
            value = getattr(stmt, f.name)
            if isinstance(value, Block):
                self._rewrite_hook_block(value)
            elif isinstance(value, Node):
                self._rewrite_hook_descendant_blocks(value)
            elif isinstance(value, list):
                for x in value:
                    if isinstance(x, Node):
                        self._rewrite_hook_descendant_blocks(x)
        # 2. 本语句表达式树中的被钩调用点提升到语句边界。
        prefix: list[Node] = []
        for f in _fields(stmt):
            if f.name in ("line", "column"):
                continue
            value = getattr(stmt, f.name)
            if isinstance(value, Block):
                continue  # 步骤 1 已重写
            if isinstance(value, Node):
                p, repl = self._lift_hook_calls(value)
                if p:
                    prefix.extend(p)
                    setattr(stmt, f.name, repl)
            elif isinstance(value, list):
                for i, x in enumerate(value):
                    if isinstance(x, Node) and not isinstance(x, Block):
                        p, repl = self._lift_hook_calls(x)
                        if p:
                            prefix.extend(p)
                            value[i] = repl
        return prefix + [stmt]

    def _rewrite_hook_descendant_blocks(self: "_Analyzer", node: Node) -> None:
        for f in _fields(node):
            if f.name in ("line", "column"):
                continue
            value = getattr(node, f.name)
            if isinstance(value, Block):
                self._rewrite_hook_block(value)
            elif isinstance(value, Node):
                self._rewrite_hook_descendant_blocks(value)
            elif isinstance(value, list):
                for x in value:
                    if isinstance(x, Node):
                        self._rewrite_hook_descendant_blocks(x)

    def _lift_hook_calls(
        self: "_Analyzer", node: Node
    ) -> tuple[list[Node], Node]:
        """Post-order: 子表达式先提升, 自身最后包裹。

        返回 ``(前置语句, 替换节点)`` —— 被钩调用被包裹后原位置由
        ``Name($t)`` 顶替, 调用本体移入 ``let $t = ...;``。"""
        if isinstance(node, Block):
            return [], node  # 语句块已被递归重写
        prefix: list[Node] = []
        for f in _fields(node):
            if f.name in ("line", "column"):
                continue
            value = getattr(node, f.name)
            if isinstance(value, Node):
                p, repl = self._lift_hook_calls(value)
                if p:
                    prefix.extend(p)
                    setattr(node, f.name, repl)
            elif isinstance(value, list):
                for i, x in enumerate(value):
                    if isinstance(x, Node):
                        p, repl = self._lift_hook_calls(x)
                        if p:
                            prefix.extend(p)
                            value[i] = repl
        hook_binding = self._hook_sites_by_call.get(id(node))
        if hook_binding is None:
            return prefix, node
        p, repl = self._wrap_hook_call(node, hook_binding)
        prefix.extend(p)
        return prefix, repl

    def _wrap_hook_call(
        self: "_Analyzer", call: Node, hook_binding: "MethodBinding"
    ) -> tuple[list[Node], Node]:
        """``obj.target(args)`` → ``(let $t = ...; obj.hook();, Name($t))``。"""
        line, column = call.line, call.column
        callee = call.callee
        prefix: list[Node] = []
        if isinstance(callee, Attribute):
            recv_expr = callee.obj
        else:
            # 隐式 self (``Self::target(...)`): 接收者是当前函数的 self。
            recv_expr = Name(line, column, ["self"])
        if not self._hook_expr_is_pure(recv_expr):
            # 接收者含调用: 提一层, 保证 hook 与 target 复用同一次求值。
            rn = self._fresh_desugar_name(self._hook_program, "hookr")
            let_r = LetStmt(line, column, rn, None, recv_expr)
            let_r._typed_ann["type"] = recv_expr._typed_ann.get("type")
            self._assign_synthetic_ids(let_r)
            prefix.append(let_r)
            new_recv = Name(line, column, [rn])
            new_recv._typed_ann["type"] = recv_expr._typed_ann.get("type")
            if isinstance(callee, Attribute):
                callee.obj = new_recv
            recv_node = new_recv
        else:
            # 纯接收者无副作用, hook 调用复用同一表达式; 但 AST 节点
            # 必须克隆 (节点池契约: 每节点一个父引用一个 id)。
            recv_node = self._clone_hook_node(recv_expr)
        tn = self._fresh_desugar_name(self._hook_program, "hookv")
        let_t = LetStmt(line, column, tn, None, call)
        let_t._typed_ann["type"] = call._typed_ann.get("type")
        self._assign_synthetic_ids(let_t)
        prefix.append(let_t)
        hook_callee = Attribute(
            recv_node.line, recv_node.column, recv_node, hook_binding.fn.name
        )
        hook_callee._typed_ann["binding"] = {
            "kind": "method", "ref": hook_binding.id,
        }
        self._ann_type(hook_callee, "Fn")
        hook_call = Call(line, column, hook_callee, [])
        hook_call._synthetic = True
        hook_call._typed_ann["call"] = {
            "callee_kind": "method", "callee_ref": hook_binding.id,
        }
        self._ann_type(hook_call, "None")
        self._assign_synthetic_ids(hook_call)
        prefix.append(ExprStmt(line, column, hook_call))
        repl = Name(line, column, [tn])
        repl._typed_ann["type"] = call._typed_ann.get("type")
        self._assign_synthetic_ids(repl)
        return prefix, repl

    def _clone_hook_node(self: "_Analyzer", node: Node) -> Node:
        """Clone a pure receiver expression for the hook call.

        语义上求值两次无差别 (纯表达式), 但 typed-AST 节点池要求每个
        节点只有一个父引用 —— 克隆子树并重新分配 id, ann 原样搬运
        (后端 cg_expr 按类型/binding 消费, 不做第二次解析)。"""
        import copy as _copy
        clone = _copy.deepcopy(node)
        self._reset_hook_ids(clone)
        self._assign_synthetic_ids(clone)
        return clone

    @staticmethod
    def _reset_hook_ids(node: Node) -> None:
        node._typed_id = None
        node._typed_ann = dict(getattr(node, "_typed_ann", {}))
        for f in _fields(node):
            if f.name in ("line", "column"):
                continue
            value = getattr(node, f.name)
            if isinstance(value, Node):
                DesugarPass._reset_hook_ids(value)
            elif isinstance(value, list):
                for x in value:
                    if isinstance(x, Node):
                        DesugarPass._reset_hook_ids(x)

    @staticmethod
    def _hook_expr_is_pure(node: Node) -> bool:
        """接收者复用安全性: 表达式树里没有调用/赋值/闭包时, 求值两次
        无副作用差别, hook 调用可直接复用原表达式。"""
        from ..ast_components.ast import Assign as _Assign
        if isinstance(node, (Call, _Assign, Closure)):
            return False
        for f in _fields(node):
            if f.name in ("line", "column"):
                continue
            value = getattr(node, f.name)
            if isinstance(value, Node) and not DesugarPass._hook_expr_is_pure(
                value
            ):
                return False
            if isinstance(value, list):
                for x in value:
                    if (
                        isinstance(x, Node)
                        and not DesugarPass._hook_expr_is_pure(x)
                    ):
                        return False
        return True
