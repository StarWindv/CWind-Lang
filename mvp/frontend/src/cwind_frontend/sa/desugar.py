"""SA pre-pass desugaring (todo-165/184/186): while-let, while, if and
for-in lowering, plus which-hook registration, run before pass 1."""

from __future__ import annotations

from dataclasses import fields as _fields
from typing import TYPE_CHECKING, Optional

from ..ast_components.ast import (
    Attribute,
    BinOp,
    Block,
    BoolLit,
    BreakStmt,
    Call,
    ElifBranch,
    EnumPattern,
    ExtraDecl,
    ForStmt,
    IfLetBranch,
    IfLetStmt,
    IfStmt,
    ImplDecl,
    LetChainSeg,
    LetStmt,
    LitPattern,
    LoopStmt,
    MatchArm,
    MatchStmt,
    Name,
    Node,
    Program,
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
            """True when *fnp* takes self by reference (&self/&mut self):
            ownership must not move — the call-site hook runs on the same
            receiver after the target returns."""
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
                    f"hook method '{fn.name}' must take self by "
                    "reference (&self / &mut self) — the call-site hook "
                    "reuses the receiver, so ownership cannot move",
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
                    f"hook target '{fn.which}' must take self by "
                    "reference (&self / &mut self) — obj.hook() runs "
                    "after the call, so the target cannot consume the "
                    "receiver",
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
