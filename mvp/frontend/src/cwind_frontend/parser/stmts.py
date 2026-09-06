"""Parser mixin: statement parsing."""

from __future__ import annotations

from typing import Optional

from .defs import (
    ParseError,
)
from ..ast_components.ast import (
    BindPattern,
    Block,
    BreakStmt,
    ContinueStmt,
    ElifBranch,
    ExprStmt,
    ForStmt,
    IfStmt,
    IfLetBranch,
    IfLetStmt,
    LetStmt,
    LoopStmt,
    MatchArm,
    MatchStmt,
    Node,
    ReturnStmt,
    Type,
    WhileLetStmt,
    LetChainSeg,
    WhileStmt,
)
from ..ast_components.token import Token, TokenKind


class ParserStmts:
    def _parse_stmt(self) -> Node:
        tok = self._peek()
        if tok is None:
            self._error("expected statement")
        if tok.kind == TokenKind.LET:
            return self._parse_let()
        if tok.kind == TokenKind.RETURN:
            return self._parse_return()
        if tok.kind == TokenKind.BREAK:
            return self._parse_break()
        if tok.kind == TokenKind.CONTINUE:
            return self._parse_continue()
        if tok.kind == TokenKind.IF:
            return self._parse_if()
        if tok.kind == TokenKind.MATCH:
            st = self._parse_match()
            # 尾位置 (无 ';') 的裸 match 是尾表达式 (Rust 语义,
            # match.md §4.2): 后端据此把它转成 return 发射返回值;
            # 带分号则是值丢弃语句。todo-162 (unit 类型) 落地前
            # 丢弃形态没有值语义, 二者都成立。
            st._tail_expr = self._match(TokenKind.SEMICOLON) is None
            return st
        if tok.kind == TokenKind.WHILE:
            # todo-165: ``while let P = E [&& ...]`` has no parenthesized
            # condition; plain ``while`` keeps requiring one.
            if self._peek(1) is not None and self._peek(1).kind == TokenKind.LET:
                return self._parse_while_let()
            return self._parse_while()
        if tok.kind == TokenKind.LABEL:
            return self._parse_labeled()
        if tok.kind == TokenKind.LOOP:
            return self._parse_loop()
        if tok.kind == TokenKind.FOR:
            return self._parse_for()
        if tok.kind == TokenKind.LBRACE:
            return self._parse_block()
        expr = self._parse_expr()
        is_tail = self._at(TokenKind.RBRACE)
        if not is_tail:
            self._expect(TokenKind.SEMICOLON, what="';' after statement")
        expr._tail_expr = is_tail
        return ExprStmt(expr.line, expr.column, expr)

    def _parse_let(self) -> LetStmt:
        tok = self._advance()  # let
        mutable = False
        mut_tok = self._peek()
        if mut_tok is not None and mut_tok.kind == TokenKind.MUT:
            self._advance()
            mutable = True
        # todo-168: ``let PATTERN = E else { diverging };`` — Rust 的
        # 语法判别式: let-else 无类型注解。可能命中 let-else 的头部
        # (非 ``IDENT :`` 形态) 一律先按模式解析; 裸绑定名后没有跟
        # ``else`` 时回滚到普通 let 路径 (无注解 → 既有 "let needs a
        # type" 报错), 复合模式则必须带 else (let-else)。
        name_tok = self._peek()
        head = (
            name_tok is not None
            and (
                name_tok.kind not in (TokenKind.IDENTIFIER, TokenKind.MUT)
                or (
                    self._peek(1) is not None
                    and self._peek(1).kind != TokenKind.COLON
                )
            )
        )
        if head:
            snap = self._snapshot()
            pattern = self._parse_pattern()
            self._expect(
                TokenKind.ASSIGN,
                what="'=' between let pattern and value",
            )
            value = self._parse_expr(allow_map_literal=True)
            if self._match(TokenKind.ELSE) is not None:
                else_block = self._parse_block()
                self._expect(TokenKind.SEMICOLON, what="';' after let-else")
                return LetStmt(
                    tok.line,
                    tok.column,
                    "",
                    None,
                    value,
                    mutable=mutable,
                    pattern=pattern,
                    else_block=else_block,
                )
            if isinstance(pattern, BindPattern):
                # 裸绑定名无 else: 不是 let-else, 回滚重读为普通 let
                # (类型注解必需, todo-22 落地前由普通路径报错)。
                self._restore(snap)
            else:
                self._error(
                    "patterns in let require an 'else' block (let-else)",
                    tok,
                )
        name = self._expect(TokenKind.IDENTIFIER, what="variable name")
        self._expect(TokenKind.COLON, what="':' after variable name (let needs a type)")
        type_ = self._parse_type()
        value: Optional[Node] = None
        if self._match(TokenKind.ASSIGN) is not None:
            value = self._parse_expr(allow_map_literal=True)
        self._expect(TokenKind.SEMICOLON, what="';' after let declaration")
        return LetStmt(
            tok.line,
            tok.column,
            self._ident_value(name),
            type_,
            value,
            mutable=mutable,
        )

    def _parse_return(self) -> ReturnStmt:
        tok = self._advance()  # return
        value: Optional[Node] = None
        if not self._at(TokenKind.SEMICOLON):
            value = self._parse_expr()
        self._expect(TokenKind.SEMICOLON, what="';' after return")
        return ReturnStmt(tok.line, tok.column, value)

    def _parse_break(self) -> BreakStmt:
        tok = self._advance()  # break
        label: Optional[str] = None
        if self._at(TokenKind.LABEL):
            label = str(self._advance().value)
        self._expect(TokenKind.SEMICOLON, what="';' after break")
        return BreakStmt(tok.line, tok.column, label=label)

    def _parse_continue(self) -> ContinueStmt:
        tok = self._advance()  # continue
        label: Optional[str] = None
        if self._at(TokenKind.LABEL):
            label = str(self._advance().value)
        self._expect(TokenKind.SEMICOLON, what="';' after continue")
        return ContinueStmt(tok.line, tok.column, label=label)

    def _parse_if(self) -> IfStmt:
        tok = self._advance()  # if
        if self._match(TokenKind.LET) is not None:
            return self._parse_if_let(tok)
        cond = self._parse_cond_expr("'if'")
        then = self._parse_block()
        elifs: list[ElifBranch] = []
        while self._at(TokenKind.ELIF):
            et = self._advance()
            econd = self._parse_cond_expr("'elif'")
            ebody = self._parse_block()
            elifs.append(ElifBranch(et.line, et.column, econd, ebody))
        else_: Optional[Block] = None
        if self._match(TokenKind.ELSE) is not None:
            else_ = self._parse_block()
        return IfStmt(tok.line, tok.column, cond, then, elifs, else_)

    def _parse_if_let(self, tok: Token) -> IfLetStmt:
        pattern = self._parse_pattern()
        self._expect(
            TokenKind.ASSIGN, what="'=' between if-let pattern and value"
        )
        value = self._parse_expr(allow_map_literal=True)
        then = self._parse_block()
        elifs: list[IfLetBranch] = []
        while self._at(TokenKind.ELIF):
            et = self._advance()
            if self._match(TokenKind.LET) is not None:
                ep = self._parse_pattern()
                self._expect(
                    TokenKind.ASSIGN,
                    what="'=' between elif-let pattern and value",
                )
                ev = self._parse_expr(allow_map_literal=True)
                eb = self._parse_block()
                elifs.append(IfLetBranch(et.line, et.column, None, ep, ev, eb))
            else:
                econd = self._parse_cond_expr("'elif'")
                ebody = self._parse_block()
                elifs.append(IfLetBranch(et.line, et.column, econd, None, None, ebody))
        else_: Optional[Block] = None
        if self._match(TokenKind.ELSE) is not None:
            else_ = self._parse_block()
        return IfLetStmt(
            tok.line,
            tok.column,
            pattern,
            value,
            then,
            elifs,
            else_,
        )

    def _parse_match(self) -> MatchStmt:
        tok = self._advance()  # match
        # todo-184: 条件括号可选 —— 带括号形态保留 map 字面量能力,
        # 裸形态下表达式止于臂区的 '{' (Rust 同样限制条件位的结构体
        # 字面量, 需要时加括号)。
        if self._at(TokenKind.LPAREN):
            self._advance()
            subject = self._parse_expr(allow_map_literal=True)
            self._expect(TokenKind.RPAREN, what="')' after match subject")
        else:
            self._cond_expr_ctx = True
            try:
                subject = self._parse_expr()
            finally:
                self._cond_expr_ctx = False
        self._expect(TokenKind.LBRACE, what="'{' after match subject")
        arms: list[MatchArm] = []
        while not self._at(TokenKind.RBRACE):
            if self._peek() is None:
                self._error("expected '}' to close the match", tok)
            at = self._peek()
            pattern = self._parse_pattern()
            guard: Optional[Node] = None
            if self._match(TokenKind.IF) is not None:
                guard = self._parse_expr(allow_map_literal=True)
            self._expect(
                TokenKind.FAT_ARROW,
                what="'=>' between match pattern and body",
            )
            if self._at(TokenKind.LBRACE):
                body = self._parse_block()
            else:
                body = self._parse_expr(allow_map_literal=True)
            arms.append(MatchArm(at.line, at.column, pattern, guard, body))
            # Rust 语义: 块臂的尾逗号可省 (match.md §4); 非块臂
            # 仍必须逗号分隔 (否则是尾表达式返回值)。
            if self._match(TokenKind.COMMA) is None:
                if not isinstance(body, Block):
                    break
                if self._at(TokenKind.RBRACE):
                    break
        self._expect(TokenKind.RBRACE, what="'}' after match arms")
        return MatchStmt(tok.line, tok.column, subject, arms)

    def _parse_while(self, label: Optional[str] = None) -> WhileStmt:
        tok = self._advance()  # while
        if self._at(TokenKind.LPAREN):
            self._advance()
            cond = self._parse_expr()
            self._expect(TokenKind.RPAREN, what="')' after while condition")
            body = self._parse_block()
            return WhileStmt(tok.line, tok.column, cond, body, label=label)
        # todo-165: no parens — a boolean-first let chain is accepted
        # (``while n && let P = E { ... }``); a plain condition without
        # parentheses keeps the historical "expected '('" error.
        first = self._parse_while_chain_bool()
        if self._at(TokenKind.AND) and self._peek(1) is not None and self._peek(1).kind == TokenKind.LET:
            segments = [LetChainSeg(first.line, first.column, None, first)]
            self._collect_chain_segments(segments)
            body = self._parse_block()
            return WhileLetStmt(tok.line, tok.column, segments, body, label=label)
        self._error("expected '(' after 'while'", tok)
        raise ParseError(
            "expected '(' after 'while'", tok.line, tok.column
        )

    def _parse_while(self, label: Optional[str] = None) -> WhileStmt:
        tok = self._advance()  # while
        if self._at(TokenKind.LPAREN):
            nxt = self._peek(1)
            # todo-184: ``while (let P = E [&& ...]) { }`` — the paren
            # form of the while-let chain reuses the bare-chain parser.
            if nxt is not None and nxt.kind == TokenKind.LET:
                self._advance()
                segments: list[LetChainSeg] = []
                self._collect_chain_segments(segments)
                self._expect(
                    TokenKind.RPAREN,
                    what="')' after while-let chain",
                )
                body = self._parse_block()
                return WhileLetStmt(
                    tok.line, tok.column, segments, body, label=label
                )
            self._advance()
            cond = self._parse_expr()
            self._expect(TokenKind.RPAREN, what="')' after while condition")
            body = self._parse_block()
            return WhileStmt(tok.line, tok.column, cond, body, label=label)
        # todo-184: parentheses optional — the bare form accepts both a
        # boolean-first let chain (``while n && let P = E {``) and a
        # plain condition (``while i < 3 {``); the expression ends at
        # the body's '{'.
        self._let_chain_ctx = True
        self._cond_expr_ctx = True
        try:
            first = self._parse_expr()
        finally:
            self._let_chain_ctx = False
            self._cond_expr_ctx = False
        if (
            self._at(TokenKind.AND)
            and self._peek(1) is not None
            and self._peek(1).kind == TokenKind.LET
        ):
            segments = [LetChainSeg(first.line, first.column, None, first)]
            self._collect_chain_segments(segments)
            body = self._parse_block()
            return WhileLetStmt(tok.line, tok.column, segments, body, label=label)
        body = self._parse_block()
        return WhileStmt(tok.line, tok.column, first, body, label=label)

    def _parse_labeled(self) -> Node:
        """todo-185: ``'name:`` prefix on a loop / while / for."""
        label = self._parse_label()
        tok = self._peek()
        if tok is not None and tok.kind == TokenKind.LOOP:
            return self._parse_loop(label)
        if tok is not None and tok.kind == TokenKind.WHILE:
            if self._peek(1) is not None and self._peek(1).kind == TokenKind.LET:
                return self._parse_while_let(label)
            return self._parse_while(label)
        if tok is not None and tok.kind == TokenKind.FOR:
            return self._parse_for(label)
        self._error("expected 'loop', 'while' or 'for' after a loop label")

    def _parse_label(self) -> str:
        tok = self._advance()  # 'name
        self._expect(TokenKind.COLON, what="':' after loop label")
        return str(tok.value)

    def _parse_loop(self, label: Optional[str] = None) -> LoopStmt:
        """todo-185: ``loop { ... }`` / ``'name: loop { ... }``."""
        tok = self._advance()  # loop
        body = self._parse_block()
        return LoopStmt(tok.line, tok.column, body, label=label)

    def _parse_cond_expr(self, what: str) -> Node:
        """todo-184: condition parentheses are optional — ``(cond)`` and
        bare ``cond`` both parse; the bare expression ends at the body's
        '{' (struct/map literals in a bare condition need the paren
        form, matching Rust's restriction)."""
        if self._at(TokenKind.LPAREN):
            self._advance()
            cond = self._parse_expr()
            self._expect(
                TokenKind.RPAREN, what=f"')' after {what} condition"
            )
            return cond
        self._cond_expr_ctx = True
        try:
            return self._parse_expr()
        finally:
            self._cond_expr_ctx = False

    def _parse_while_let(self, label: Optional[str] = None) -> WhileLetStmt:
        """todo-165: ``while let P = E [&& (let P2 = E2 | B)]* { ... }``.

        ``&&`` splits top-level chain operands; a boolean segment's own
        ``&&`` stays inside its expression unless the next operand is a
        ``let`` (Rust 2024 let-chain splitting).  Bindings live in one
        scope shared with the loop body; the loop exits when any segment
        fails.
        """
        tok = self._advance()  # while
        segments: list[LetChainSeg] = []
        self._collect_chain_segments(segments)
        body = self._parse_block()
        return WhileLetStmt(tok.line, tok.column, segments, body, label=label)

    def _collect_chain_segments(self, segments: list["LetChainSeg"]) -> None:
        """Parse ``&&``-separated chain operands into *segments*."""
        while True:
            if self._at(TokenKind.AND):
                self._advance()  # the && joining the previous operand
            seg_tok = self._peek()
            if self._match(TokenKind.LET) is not None:
                pattern = self._parse_pattern()
                self._expect(
                    TokenKind.ASSIGN,
                    what="'=' between while-let pattern and value",
                )
                value = self._parse_while_chain_bool()
                segments.append(LetChainSeg(seg_tok.line, seg_tok.column, pattern, value))
            else:
                value = self._parse_while_chain_bool()
                segments.append(LetChainSeg(value.line, value.column, None, value))
            if self._at(TokenKind.AND):
                self._advance()  # the && joining the next operand
                continue
            break

    def _parse_while_chain_bool(self) -> Node:
        """Parse one operand of a while-let chain.

        ``_let_chain_ctx`` makes ``&& let`` terminate the expression at
        the top level so the chain loop can claim the next operand
        (todo-165); nested parentheses still reject ``let`` chains, the
        same restriction Rust applies.
        """
        self._let_chain_ctx = True
        try:
            return self._parse_expr(allow_map_literal=True)
        finally:
            self._let_chain_ctx = False

    def _parse_for(self, label: Optional[str] = None) -> ForStmt:
        """``for PATTERN in iterable { body }`` (todo-186).

        The loop head is a full pattern: ``for x in ...`` binds a plain
        name, ``for (i, item) in ...`` destructures the element tuple
        (doc analysis/match.md §3.4) — the paren there is the tuple
        pattern's own.  The legacy C-style header ``for (Type var :
        iterable)`` keeps parsing and lands on the same shape with a
        binding pattern.
        """
        tok = self._advance()  # for
        if self._at(TokenKind.LPAREN):
            nxt = self._peek(1)
            nxt2 = self._peek(2)
            if (
                nxt is not None and nxt.kind == TokenKind.IDENTIFIER
                and nxt2 is not None
                and nxt2.kind in (TokenKind.COLON, TokenKind.IDENTIFIER)
            ):
                return self._parse_for_legacy_paren(tok, label)
        if self._at(TokenKind.IN):
            self._error("expected pattern before 'in'", self._peek())
        pattern = self._parse_pattern()
        in_tok = self._peek()
        if not (in_tok is not None and in_tok.kind == TokenKind.IN):
            self._error("expected 'in' in for-in loop", in_tok)
        self._advance()  # in
        self._for_iterable_expr = True
        try:
            iterable = self._parse_expr()
        finally:
            self._for_iterable_expr = False
        self._expect(TokenKind.LBRACE, what="'{' to open the for-in loop body")
        self.pos -= 1  # let _parse_block consume and validate the brace
        body = self._parse_block()
        return ForStmt(tok.line, tok.column, pattern, iterable, body, False, label=label)

    def _parse_for_legacy_paren(
        self, tok: "Token", label: Optional[str]
    ) -> ForStmt:
        """Legacy header ``for ( [Type] var : iterable ) { body }``."""
        self._advance()  # (
        type_: Optional[Type] = None
        nxt = self._peek(1)
        if self._at(TokenKind.IDENTIFIER) and nxt is not None and nxt.kind == TokenKind.IDENTIFIER:
            type_ = self._parse_type()
        var = self._expect(TokenKind.IDENTIFIER, what="loop variable")
        self._expect(TokenKind.COLON, what="':' in for-in sugar")
        iterable = self._parse_expr()
        self._expect(TokenKind.RPAREN, what="')' after for-in header")
        self._expect(TokenKind.LBRACE, what="'{' to open the for-in loop body")
        self.pos -= 1  # let _parse_block consume and validate the brace
        body = self._parse_block()
        pattern = BindPattern(var.line, var.column, self._ident_value(var))
        return ForStmt(tok.line, tok.column, pattern, iterable, body, True, label=label)
