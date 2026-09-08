"""Parser mixin: type and pattern parsing."""

from __future__ import annotations

from typing import Optional, cast

from .defs import (
    ParseError,
)
from ..ast_components.ast import (
    AssocType,
    BindPattern,
    Block,
    BoolLit,
    EnumPattern,
    ErrorStmt,
    ExprStmt,
    FloatLit,
    IntLit,
    LitPattern,
    Node,
    StrLit,
    StructPattern,
    StructPatternField,
    TuplePattern,
    Type,
    WildcardPattern,
)
from ..ast_components.ast import _type_name_for_type
from ..ast_components.token import TokenKind


class ParserTypes:
    def _parse_type(self) -> Type:
        if self._at(TokenKind.FN):
            fn_node = self._parse_function_pointer()
            return Type(fn_node.line, fn_node.column, fn_node.parts[0], [], ref=False)
        if self._at(TokenKind.STAR_CONST) or self._at(TokenKind.STAR_MUT):
            # 原始指针: `*const T` / `*mut T`, 以 "*const "/"*mut " 前缀
            # 编码进类型名 (与 "&" 的 ref 标记同思路, 字符串化后仍可辨认)
            star = self._advance()
            inner = self._parse_type()
            prefix = "const" if star.kind == TokenKind.STAR_CONST else "mut"
            return Type(
                star.line,
                star.column,
                f"*{prefix} {_type_name_for_type(inner)}",
                [],
                ref=False,
            )
        if self._at(TokenKind.AMP):
            amp = self._advance()
            # bug-46: ``&mut T`` 类型位 —— MUT 只在借用标记后合法
            # (操作数表达式的 mut 由 _parse_unary 消费, 互不干扰)。
            mut = self._match(TokenKind.MUT) is not None
            # todo-193: 借用切片类型 ``&[T]`` / ``&mut [T]`` — 无长度
            # 的序列借用, 语法糖为 ``&Vector<T>`` (Rust slice 引用语义;
            # 裸 ``[T]`` 不是合法类型, 只有借用形式可用)。后跟 ``;``
            # 则回落到定长数组形态 ``&[T; N]``。
            if self._at(TokenKind.LBRACKET):
                lb = self._advance()
                inner = self._parse_type()
                if self._match(TokenKind.SEMICOLON) is not None:
                    len_tok = self._expect(
                        TokenKind.INTEGER, what="array length after ';'"
                    )
                    self._expect(
                        TokenKind.RBRACKET, what="']' closing array type"
                    )
                    return Type(
                        amp.line,
                        amp.column,
                        f"[{_type_name_for_type(inner)}; {len_tok.value}]",
                        [],
                        ref=True,
                        mut=mut,
                    )
                self._expect(
                    TokenKind.RBRACKET, what="']' closing slice type"
                )
                return Type(
                    amp.line,
                    amp.column,
                    "Vector",
                    [inner],
                    ref=True,
                    mut=mut,
                )
            inner = self._parse_type()
            return Type(
                amp.line,
                amp.column,
                inner.name,
                inner.args,
                ref=True,
                mut=mut,
            )
        if self._at(TokenKind.LBRACKET):
            # 定长数组类型 (todo-60): `[T; N]`, 与 C `char[N]` /
            # Rust `[u8; N]` 对应, 名字整体扁平化编码 (同原始指针思路)
            lb = self._advance()
            inner = self._parse_type()
            self._expect(TokenKind.SEMICOLON, what="';' in array type")
            len_tok = self._expect(
                TokenKind.INTEGER, what="array length after ';'"
            )
            self._expect(TokenKind.RBRACKET, what="']' closing array type")
            return Type(
                lb.line,
                lb.column,
                f"[{_type_name_for_type(inner)}; {len_tok.value}]",
                [],
                ref=False,
            )
        if self._at(TokenKind.NOT):
            tok = self._advance()  # !
            return Type(tok.line, tok.column, "!")
        tok = self._expect(TokenKind.IDENTIFIER, what="type name")
        if self._at(TokenKind.PATH):
            # 限定路径类型: `module::Name` / `Self::Item` (bug-42: impl
            # 头部允许 `num_wrapping::Wrapping<i32>` 这类带实参的限定名)
            # 注意: 类型位不做宏混淆 (todo-44) —— 类型是文件级结构名,
            # 宏展开体里写的 Vector/Int 等照常按文件作用域解析。
            parts = [str(tok.value)]
            while self._at(TokenKind.PATH):
                self._advance()
                part = self._expect(
                    TokenKind.IDENTIFIER, what="name after '::' in type"
                )
                parts.append(str(part.value))
            args: list[Type] = []
            bindings: list[AssocType] = []
            if self._match(TokenKind.LT) is not None:
                args, bindings = self._parse_type_args()
            result = Type(tok.line, tok.column, "::".join(parts), args)
            result.bindings = bindings
            return result
        args: list[Type] = []
        bindings: list[AssocType] = []
        if self._match(TokenKind.LT) is not None:
            args, bindings = self._parse_type_args()
        result = Type(tok.line, tok.column, str(tok.value), args)
        result.bindings = bindings
        return result

    # -- statements --------------------------------------------------------
    def _parse_block(self) -> Block:
        tok = self._expect(TokenKind.LBRACE, what="'{' to open a block")
        stmts: list[Node] = []
        while not self._at(TokenKind.RBRACE):
            if self._peek() is None:
                self._error("expected '}' to close the block", tok)
            # A lone `;` is an empty statement (Rust semantics).  This is
            # load-bearing for macros: `m!();` where the rule body already
            # ends with `;` splices a trailing `;;`, which must stay legal.
            if self._match(TokenKind.SEMICOLON) is not None:
                continue
            stmt_tok = self._peek()
            try:
                stmts.append(self._parse_stmt())
            except ParseError as exc:
                self.errors.append(exc)
                if self._peek() is stmt_tok:
                    self._advance()  # never spin on the same token
                self._synchronize_statement()
                stmts.append(ErrorStmt(exc.line, exc.column, exc.message))
        self._advance()  # }
        # Always pass a fresh list: Block's mutable default can alias an
        # empty block that is still being parsed (seen with an empty for body).
        return Block(tok.line, tok.column, list(stmts))

    def _parse_validation_block(self) -> Block:
        """Parse a field-validation block: ``{ expr }`` without semicolons.

        Field validation (``where { ... }`` / ``-> { ... }``) contains bare
        expressions, unlike type-level ``where`` blocks whose statements end
        with ``;``.  Optional ``;`` separators are accepted for leniency.
        """
        tok = self._expect(TokenKind.LBRACE, what="'{' to open a validation block")
        exprs: list[Node] = []
        while not self._at(TokenKind.RBRACE):
            expr = self._parse_expr()
            exprs.append(ExprStmt(expr.line, expr.column, expr))
            if self._match(TokenKind.SEMICOLON) is None:
                break
        self._expect(TokenKind.RBRACE, what="'}' to close the validation block")
        return Block(tok.line, tok.column, exprs)

    def _parse_pattern(self) -> Node:
        tok = self._peek()
        if tok is None:
            self._error("expected pattern")
        if tok.kind == TokenKind.INTEGER:
            self._advance()
            return LitPattern(
                tok.line, tok.column, IntLit(tok.line, tok.column, cast(int, tok.value), tok.raw)
            )
        if tok.kind == TokenKind.FLOAT:
            self._advance()
            return LitPattern(
                tok.line, tok.column, FloatLit(tok.line, tok.column, cast(float, tok.value), tok.raw)
            )
        if tok.kind == TokenKind.STRING:
            self._advance()
            return LitPattern(
                tok.line, tok.column, StrLit(tok.line, tok.column, str(tok.value), tok.raw)
            )
        if tok.kind == TokenKind.IDENTIFIER and tok.value in ("true", "false"):
            self._advance()
            return LitPattern(
                tok.line,
                tok.column,
                BoolLit(tok.line, tok.column, tok.value == "true", tok.raw),
            )
        if tok.kind == TokenKind.IDENTIFIER and tok.value == "_":
            self._advance()
            return WildcardPattern(tok.line, tok.column)
        if tok.kind == TokenKind.LPAREN:
            self._advance()
            if self._at(TokenKind.RPAREN):
                self._advance()
                return TuplePattern(tok.line, tok.column, [])
            elems = [self._parse_pattern()]
            while self._match(TokenKind.COMMA) is not None:
                if self._at(TokenKind.RPAREN):
                    break  # `(a, b,)` is a two-element tuple pattern
                elems.append(self._parse_pattern())
            self._expect(TokenKind.RPAREN, what="')' after tuple pattern")
            return TuplePattern(tok.line, tok.column, elems)
        if tok.kind == TokenKind.IDENTIFIER:
            # todo-193: ``Enum::Variant { f: P, .. }`` / ``mod::E::V { .. }``
            # (enum 具名字段变体模式) — 2/3 段路径 + '{' 优先于此处的
            # struct pattern 形态 (1 段路径 + '{' 仍是本地 struct 模式)。
            snap2 = self._snapshot()
            try:
                head = self._parse_name_path()
                if (
                    len(head.parts) in (2, 3)
                    and self._at(TokenKind.LBRACE)
                ):
                    named = self._parse_variant_struct_pattern_fields()
                    return EnumPattern(
                        tok.line, tok.column, head.parts, [], named_fields=named
                    )
            except ParseError:
                pass
            self._restore(snap2)
            type_ = self._try_parse_pattern_type()
            if type_ is not None:
                return self._parse_struct_pattern(type_)
            name = self._advance()
            # todo-168 (裸变体模式, doc analysis/match.md §3.1): 名字后
            # 跟 '(' 且该名字当前语境无法判定时, 枚举变体与函数调用形状
            # 相同 — 模式位无调用语义, `Name(args)` 一律是带载荷变体。
            if self._at(TokenKind.LPAREN) and not self._at_known_unit_call():
                parts = [str(name.value)]
                elems = self._parse_pattern_call_elems()
                return EnumPattern(
                    tok.line, tok.column, parts, elems
                )
            if self._at(TokenKind.PATH):
                parts = [str(name.value)]
                while self._at(TokenKind.PATH):
                    self._advance()
                    part = self._expect(
                        TokenKind.IDENTIFIER, what="name after '::'"
                    )
                    parts.append(str(part.value))
                if len(parts) not in (2, 3):
                    # todo-81: ``module::Enum::Variant`` keeps its three
                    # source segments here; SA normalizes the resolved
                    # form back to the canonical two-segment path.
                    self._error("unsupported path pattern", self._peek())
                elems: list[Node] = []
                if self._match(TokenKind.LPAREN) is not None:
                    while not self._at(TokenKind.RPAREN):
                        elems.append(self._parse_pattern())
                        if self._match(TokenKind.COMMA) is None:
                            break
                    self._expect(
                        TokenKind.RPAREN,
                        what="')' after enum variant pattern",
                    )
                return EnumPattern(
                    tok.line, tok.column, parts, elems
                )
            return BindPattern(tok.line, tok.column, self._ident_value(name))
        self._error(f"unexpected token {tok.raw!r} in pattern", tok)

    def _at_known_unit_call(self) -> bool:
        """True when `Name(` at the cursor is a unit-variant path being
        disambiguated — 模式位不存在调用语义, 目前恒 False; 保留钩子供
        未来上下文 (如宏产物) 需要时扩展。"""
        return False

    def _parse_variant_struct_pattern_fields(self) -> list:
        """Parse ``{ f: P, g, .. }`` of a named-field variant pattern
        (todo-193); 简写形式 (无 ':') 按字段名绑定, ``..`` 允许省略
        其余字段。"""
        self._advance()  # {
        fields: list[StructPatternField] = []
        while not self._at(TokenKind.RBRACE):
            if self._peek() is None:
                self._error("expected '}' after variant fields", self._peek())
            if self._match(TokenKind.UNPACK) is not None:
                if not self._at(TokenKind.RBRACE):
                    self._error(
                        "'..' must be the last item in a variant pattern",
                        self._peek(),
                    )
                break
            ft = self._expect(
                TokenKind.IDENTIFIER, what="field name in variant pattern"
            )
            sub: Optional[Node] = None
            if self._match(TokenKind.COLON) is not None:
                sub = self._parse_pattern()
            fields.append(
                StructPatternField(
                    ft.line, ft.column, str(ft.value), sub
                )
            )
            if self._match(TokenKind.COMMA) is None:
                break
        self._expect(TokenKind.RBRACE, what="'}' after variant fields")
        return fields

    def _parse_pattern_call_elems(self) -> list[Node]:
        """Parse the `(P1, P2, ...)` payload of a bare variant pattern."""
        elems: list[Node] = []
        self._expect(TokenKind.LPAREN, what="'(' after variant name")
        while not self._at(TokenKind.RPAREN):
            elems.append(self._parse_pattern())
            if self._match(TokenKind.COMMA) is None:
                break
        self._expect(TokenKind.RPAREN, what="')' after variant payload")
        return elems

    def _try_parse_pattern_type(self) -> Optional[Type]:
        """Speculatively parse ``Name<Args>`` as a type when a struct-pattern
        brace follows (pattern position is never a comparison, so ``<`` is
        unambiguous here)."""
        snap = self._snapshot()
        try:
            type_ = self._parse_type()
            if self._at(TokenKind.LBRACE):
                return type_
        except ParseError:
            pass
        self._restore(snap)
        return None

    def _parse_struct_pattern(self, type_: Type) -> StructPattern:
        tok = self._advance()  # {
        fields: list[StructPatternField] = []
        rest = False
        while not self._at(TokenKind.RBRACE):
            if self._peek() is None:
                self._error("expected '}' after struct pattern", tok)
            if self._match(TokenKind.UNPACK) is not None:
                rest = True
                if not self._at(TokenKind.RBRACE):
                    self._error(
                        "'..' must be the last field in a struct pattern",
                        self._peek(),
                    )
                break
            ft = self._expect(
                TokenKind.IDENTIFIER, what="field name in struct pattern"
            )
            sub: Optional[Node] = None
            if self._match(TokenKind.COLON) is not None:
                sub = self._parse_pattern()
            fields.append(
                StructPatternField(
                    ft.line, ft.column, str(ft.value), sub
                )
            )
            if self._match(TokenKind.COMMA) is None:
                break
        self._expect(TokenKind.RBRACE, what="'}' after struct pattern")
        return StructPattern(type_.line, type_.column, type_, fields, rest)
