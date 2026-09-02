"""SA pre-pass desugaring (todo-165): while-let lowering and
which-hook inlining, run before pass 1."""

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
    ExprStmt,
    ExtraDecl,
    ForStmt,
    IfLetStmt,
    IfStmt,
    ImplDecl,
    LetChainSeg,
    MatchArm,
    MatchStmt,
    Name,
    Node,
    Program,
    ReturnStmt,
    WhileLetStmt,
    WhileStmt,
    WildcardPattern,
)
from ..ast_components.token import TokenKind

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
                line, column, BoolLit(line, column, True, "true"), stmt.body
            )
        if len(segments) == 1 and segments[0].pattern is None:
            # Single boolean operand: identical to a plain ``while``.
            return WhileStmt(line, column, segments[0].value, stmt.body)
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
            )
        cond = self._fold_bool_chain(
            [seg.value for seg in segments[:first_let]]
        )
        if cond is None:
            cond = BoolLit(line, column, True, "true")
        inner_body = self._desugar_chain_body(segments[first_let:], stmt.body)
        return WhileStmt(line, column, cond, inner_body)

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

    def _inline_which_hooks(self: "_Analyzer", program: Program) -> None:
        if getattr(program, "_which_inlined", False):
            return
        for item in program.items:
            if not isinstance(item, (ExtraDecl, ImplDecl)):
                continue
            owner = item.struct.name
            table = {fn.name: fn for fn in item.methods}
            for fn in table.values():
                if fn.which is None or fn.body is None:
                    continue
                target = table.get(fn.which)
                if target is None:
                    self._record_error(
                        f"which target '{fn.which}' must be declared in the "
                        f"same '{owner}' block as the hook",
                        fn.line,
                        fn.column,
                    )
                    continue
                if target is fn:
                    self._record_error(
                        f"which method '{fn.name}' cannot hook itself",
                        fn.line,
                        fn.column,
                    )
                    continue
                if target.which is not None:
                    self._record_error(
                        f"which target '{fn.which}' is itself a which hook",
                        fn.line,
                        fn.column,
                    )
                    continue
                if target.body is None:
                    self._record_error(
                        f"which target '{fn.which}' must have a body",
                        fn.line,
                        fn.column,
                    )
                    continue
                key = (owner, fn.which)
                if key in self._which_hooked:
                    self._record_error(
                        f"'{owner}::{fn.which}' already has a which hook "
                        f"('{self._which_hooked[key]}')",
                        fn.line,
                        fn.column,
                    )
                    continue
                self._which_hooked[key] = fn.name
                self._insert_hook_before_returns(
                    target.body, fn.name, fn.line, fn.column
                )
        program._which_inlined = True

    def _make_hook_call_stmt(
        self: "_Analyzer", hook_name: str, line: int, column: int
    ) -> ExprStmt:
        recv = Name(line, column, ["self"])
        callee = Attribute(line, column, recv, hook_name)
        call = Call(line, column, callee, [])
        call._synthetic = True
        return ExprStmt(line, column, call)

    def _insert_hook_before_returns(
        self: "_Analyzer",
        block: Block,
        hook_name: str,
        line: int,
        column: int,
    ) -> None:
        new_stmts: list[Node] = []
        for stmt in block.stmts:
            if isinstance(stmt, ReturnStmt):
                new_stmts.append(
                    self._make_hook_call_stmt(hook_name, line, column)
                )
            new_stmts.append(stmt)
            if isinstance(stmt, IfStmt):
                self._insert_hook_before_returns(
                    stmt.then, hook_name, line, column
                )
                for branch in stmt.elifs:
                    self._insert_hook_before_returns(
                        branch.then, hook_name, line, column
                    )
                if stmt.else_ is not None:
                    self._insert_hook_before_returns(
                        stmt.else_, hook_name, line, column
                    )
            elif isinstance(stmt, WhileStmt):
                self._insert_hook_before_returns(
                    stmt.body, hook_name, line, column
                )
            elif isinstance(stmt, MatchStmt):
                for arm in stmt.arms:
                    self._insert_hook_before_returns(
                        arm.body, hook_name, line, column
                    )
            elif isinstance(stmt, IfLetStmt):
                self._insert_hook_before_returns(
                    stmt.then, hook_name, line, column
                )
                for branch in stmt.elifs:
                    self._insert_hook_before_returns(
                        branch.body, hook_name, line, column
                    )
                if stmt.else_ is not None:
                    self._insert_hook_before_returns(
                        stmt.else_, hook_name, line, column
                    )
            elif isinstance(stmt, ForStmt):
                self._insert_hook_before_returns(
                    stmt.body, hook_name, line, column
                )
            elif isinstance(stmt, Block):
                self._insert_hook_before_returns(
                    stmt, hook_name, line, column
                )
        block.stmts = new_stmts
        # 兜底: 若函数体尾部可落到隐式 return, 也补一次钩子调用。
        block.stmts.append(self._make_hook_call_stmt(hook_name, line, column))
