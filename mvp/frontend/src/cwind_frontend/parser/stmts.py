"""Parser mixin: statement parsing."""

from __future__ import annotations

from typing import Optional

from .defs import (
    ParseError,
)
from ..ast_components.ast import (
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
            return self._parse_match()
        if tok.kind == TokenKind.WHILE:
            # todo-165: ``while let P = E [&& ...]`` has no parenthesized
            # condition; plain ``while`` keeps requiring one.
            if self._peek(1) is not None and self._peek(1).kind == TokenKind.LET:
                return self._parse_while_let()
            return self._parse_while()
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
        self._expect(TokenKind.SEMICOLON, what="';' after break")
        return BreakStmt(tok.line, tok.column)

    def _parse_continue(self) -> ContinueStmt:
        tok = self._advance()  # continue
        self._expect(TokenKind.SEMICOLON, what="';' after continue")
        return ContinueStmt(tok.line, tok.column)

    def _parse_if(self) -> IfStmt:
        tok = self._advance()  # if
        if self._match(TokenKind.LET) is not None:
            return self._parse_if_let(tok)
        self._expect(TokenKind.LPAREN, what="'(' after 'if'")
        cond = self._parse_expr()
        self._expect(TokenKind.RPAREN, what="')' after if condition")
        then = self._parse_block()
        elifs: list[ElifBranch] = []
        while self._at(TokenKind.ELIF):
            et = self._advance()
            self._expect(TokenKind.LPAREN, what="'(' after 'elif'")
            econd = self._parse_expr()
            self._expect(TokenKind.RPAREN, what="')' after elif condition")
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
                self._expect(TokenKind.LPAREN, what="'(' after 'elif'")
                econd = self._parse_expr()
                self._expect(TokenKind.RPAREN, what="')' after elif condition")
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
        self._expect(TokenKind.LPAREN, what="'(' after 'match'")
        subject = self._parse_expr(allow_map_literal=True)
        self._expect(TokenKind.RPAREN, what="')' after match subject")
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
            if self._match(TokenKind.COMMA) is None:
                break
        self._expect(TokenKind.RBRACE, what="'}' after match arms")
        return MatchStmt(tok.line, tok.column, subject, arms)

    def _parse_while(self) -> WhileStmt:
        tok = self._advance()  # while
        if self._at(TokenKind.LPAREN):
            self._advance()
            cond = self._parse_expr()
            self._expect(TokenKind.RPAREN, what="')' after while condition")
            body = self._parse_block()
            return WhileStmt(tok.line, tok.column, cond, body)
        # todo-165: no parens — a boolean-first let chain is accepted
        # (``while n && let P = E { ... }``); a plain condition without
        # parentheses keeps the historical "expected '('" error.
        first = self._parse_while_chain_bool()
        if self._at(TokenKind.AND) and self._peek(1) is not None and self._peek(1).kind == TokenKind.LET:
            segments = [LetChainSeg(first.line, first.column, None, first)]
            self._collect_chain_segments(segments)
            body = self._parse_block()
            return WhileLetStmt(tok.line, tok.column, segments, body)
        self._error("expected '(' after 'while'", tok)
        raise ParseError(
            "expected '(' after 'while'", tok.line, tok.column
        )

    def _parse_while(self) -> WhileStmt:
        tok = self._advance()  # while
        if self._at(TokenKind.LPAREN):
            self._advance()
            cond = self._parse_expr()
            self._expect(TokenKind.RPAREN, what="')' after while condition")
            body = self._parse_block()
            return WhileStmt(tok.line, tok.column, cond, body)
        # todo-165: no parens — a boolean-first let chain is accepted
        # (``while n && let P = E { ... }``); a plain condition without
        # parentheses keeps the historical "expected '('" error.
        first = self._parse_while_chain_bool()
        if (
            self._at(TokenKind.AND)
            and self._peek(1) is not None
            and self._peek(1).kind == TokenKind.LET
        ):
            segments = [LetChainSeg(first.line, first.column, None, first)]
            self._collect_chain_segments(segments)
            body = self._parse_block()
            return WhileLetStmt(tok.line, tok.column, segments, body)
        self._error("expected '(' after 'while'", tok)

    def _parse_loop(self) -> LoopStmt:
        """todo-185: ``loop { ... }`` — the basic unbounded loop."""
        tok = self._advance()  # loop
        body = self._parse_block()
        return LoopStmt(tok.line, tok.column, body)

    def _parse_while_let(self) -> WhileLetStmt:
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
        return WhileLetStmt(tok.line, tok.column, segments, body)

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

    def _parse_for(self) -> ForStmt:
        tok = self._advance()  # for
        if self._at(TokenKind.LPAREN):
            # for ( [Type] var : iterable ) { ... }
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
            return ForStmt(tok.line, tok.column, self._ident_value(var), iterable, body, type_, True)
        if self._at(TokenKind.IN):
            self._error("expected iteration variable before 'in'", self._peek())
        var = self._expect(TokenKind.IDENTIFIER, what="loop variable")
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
        return ForStmt(tok.line, tok.column, self._ident_value(var), iterable, body, None, False)
