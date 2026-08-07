"""CWind recursive-descent parser (spec: frontend/Grammar.md).

Consumes the token stream produced by :mod:`cwind_frontend.lexer` and builds
the AST defined in :mod:`cwind_frontend.ast_components.ast`.

Design notes
------------
* Contextual tokens are resolved here: ``{`` (block / map literal after ``=`` /
  struct construction), ``<``/``>`` (generics vs. comparison vs. shift), ``:``
  (type annotation / for-in sugar / map entry), ``in`` (for-in only).  The
  lexer deliberately stays context-free.
* Nested generic closers (``Vector<Vector<Int>>``) arrive as a single ``>>``
  (``SHR``) token; the parser splits it by re-queuing a synthetic ``>``.
* Grammar-level errors (missing ``;``, unbalanced delimiters, declarations
  without types, ...) raise :class:`ParseError`, which carries 1-based
  positions and is rendered with ariadne_py just like :class:`LexError`.
* The parser is error-recovering: it records every :class:`ParseError` and
  synchronizes at statement/declaration boundaries so one run surfaces many
  errors.  Use :func:`parse_with_errors` to get them all; :func:`parse`
  keeps the fail-fast behavior (raises the first error).
"""

from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass
from typing import NoReturn, Optional, Union, cast

from ..ast_components.ast import (
    Arg,
    Assign,
    Attribute,
    BinOp,
    Block,
    BoolLit,
    BreakStmt,
    Call,
    ConstDecl,
    ContinueStmt,
    Distribution,
    ElifBranch,
    EnumDecl,
    ErrorStmt,
    ExprStmt,
    ExtraDecl,
    Field,
    FloatLit,
    FnDecl,
    ForStmt,
    GroupApply,
    GroupDecl,
    IfStmt,
    ImplDecl,
    Index,
    IntLit,
    LetStmt,
    MapEntry,
    MapLit,
    Name,
    Node,
    Param,
    Program,
    ReturnStmt,
    Slice,
    StrLit,
    StructConstruct,
    StructDecl,
    TraitDecl,
    Type,
    TypeDecl,
    TypeParam,
    UnaryOp,
    Variant,
    VectorLit,
    WhileStmt,
)
from ..ast_components.errors import FrontendError
from ..ast_components.token import Token, TokenKind
from ..lexer import tokenize, tokenize_file

__all__ = [
    "ParseError",
    "ParseResult",
    "Parser",
    "parse",
    "parse_file",
    "parse_source",
    "parse_with_errors",
]


class ParseError(FrontendError):
    """Raised for grammar-level problems (as opposed to :class:`LexError`)."""


@dataclass
class ParseResult:
    """Program produced by the parser plus any recovered grammar errors."""

    program: Program
    errors: list[ParseError]


_ASSIGN_OPS: frozenset[TokenKind] = frozenset({
    TokenKind.ASSIGN,
    TokenKind.PLUS_ASSIGN,
    TokenKind.MINUS_ASSIGN,
    TokenKind.STAR_ASSIGN,
    TokenKind.SLASH_ASSIGN,
    TokenKind.ABS_LT,  # <:
    TokenKind.ABS_GT,  # :>
})

_RELATIONAL_OPS: frozenset[TokenKind] = frozenset({
    TokenKind.LT,
    TokenKind.GT,
    TokenKind.LE,
    TokenKind.GE,
})

_EQUALITY_OPS: frozenset[TokenKind] = frozenset({
    TokenKind.EQ,
    TokenKind.ADDR_EQ,
    TokenKind.NE,
    TokenKind.NOT_LT,  # !<  sugar for >=
    TokenKind.NOT_GT,  # !>  sugar for <=
})

_ADDITIVE_OPS: frozenset[TokenKind] = frozenset({TokenKind.PLUS, TokenKind.MINUS})
_MULTIPLICATIVE_OPS: frozenset[TokenKind] = frozenset({
    TokenKind.STAR,
    TokenKind.SLASH,
    TokenKind.PERCENT,
})
_SHIFT_OPS: frozenset[TokenKind] = frozenset({TokenKind.SHL, TokenKind.SHR})
_UNARY_OPS: frozenset[TokenKind] = frozenset({
    TokenKind.NOT,
    TokenKind.MINUS,
    TokenKind.PLUS,
})

# Token kinds a new statement can start with (used by panic-mode recovery).
_STMT_START: frozenset[TokenKind] = frozenset({
    TokenKind.LET,
    TokenKind.RETURN,
    TokenKind.BREAK,
    TokenKind.CONTINUE,
    TokenKind.IF,
    TokenKind.WHILE,
    TokenKind.FOR,
    TokenKind.LBRACE,
})

# Token kinds a new top-level declaration can start with.
_TOP_LEVEL_START: frozenset[TokenKind] = frozenset({
    TokenKind.PUB,
    TokenKind.CONST,
    TokenKind.TYPE,
    TokenKind.TYPEDEF,
    TokenKind.STRUCT,
    TokenKind.ENUM,
    TokenKind.TRAIT,
    TokenKind.IMPL,
    TokenKind.EXTRA,
    TokenKind.GROUP,
    TokenKind.FN,
})


class Parser:
    """A recursive-descent parser over a token list."""

    def __init__(self, tokens: list[Token]) -> None:
        self.tokens = [t for t in tokens if t.kind != TokenKind.COMMENT]
        self.pos = 0
        self.errors: list[ParseError] = []
        self._pending: deque[Token] = deque()  # synthetic tokens (from `>>` splits)

    # -- token helpers -----------------------------------------------------

    def _peek(self, offset: int = 0) -> Optional[Token]:
        if offset < len(self._pending):
            return self._pending[offset]
        idx = self.pos + offset - len(self._pending)
        if 0 <= idx < len(self.tokens):
            return self.tokens[idx]
        return None

    def _advance(self) -> Token:
        if self._pending:
            return self._pending.popleft()
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def _snapshot(self) -> tuple[int, list[Token]]:
        """Save the token cursor so a speculative parse can be rolled back."""
        return self.pos, list(self._pending)

    def _restore(self, snap: tuple[int, list[Token]]) -> None:
        """Restore a cursor saved by :meth:`_snapshot`."""
        self.pos, pending = snap
        self._pending = deque(pending)

    def _at(self, kind: TokenKind, value: object = None) -> bool:
        tok = self._peek()
        return (
            tok is not None
            and tok.kind == kind
            and (value is None or tok.value == value)
        )

    def _match(self, kind: TokenKind, value: object = None) -> Optional[Token]:
        if self._at(kind, value):
            return self._advance()
        return None

    def _expect(self, kind: TokenKind, value: object = None, what: str = "") -> Token:
        tok = self._peek()
        if not self._at(kind, value):
            desc = what or (repr(kind.value) if value is None else f"{kind.value} {value!r}")
            prev = self.tokens[self.pos - 1] if self.pos > 0 else None
            if (
                kind == TokenKind.SEMICOLON
                and tok is not None
                and prev is not None
                and tok.line > prev.line
            ):
                # Missing `;` at the end of the previous line: point there
                # instead of at the first token of the next line.
                raise ParseError(
                    f"expected {desc}",
                    prev.end_line,
                    prev.end_column,
                    end_line=prev.end_line,
                    end_column=prev.end_column,
                )
            self._error(f"expected {desc}", tok)
        return self._advance()

    def _expect_gt(self, what: str = "'>'") -> Token:
        """Expect ``>``, transparently splitting a ``>>`` (SHR) token."""
        tok = self._peek()
        if tok is None or tok.kind not in (TokenKind.GT, TokenKind.SHR):
            self._error(f"expected {what}", tok)
        tok = self._advance()
        if tok.kind == TokenKind.SHR:
            # `>>` closes this generic and one more; re-queue the second `>`.
            self._pending.append(Token(
                TokenKind.GT,
                ">",
                tok.line,
                tok.column + 1,
                tok.end_line,
                tok.end_column,
                ">",
            ))
        return tok

    def _error(self, message: str, token: Optional[Token] = None) -> NoReturn:
        tok = token if token is not None else self._peek()
        if tok is None:
            last = self.tokens[-1] if self.tokens else None
            line = last.end_line if last is not None else 1
            column = last.end_column if last is not None else 1
            end_line, end_column = line, column
        else:
            line, column = tok.line, tok.column
            end_line, end_column = tok.end_line, tok.end_column
        raise ParseError(message, line, column, end_line=end_line, end_column=end_column)

    def _brace_is_struct_construct(self) -> bool:
        """True if the ``{ ... }`` at the cursor is a struct construction
        (comma-separated expressions) rather than a block.

        A top-level ``;`` inside the braces means it is a statement block
        (e.g. a for/while body following its iterable expression); struct
        arguments never contain statements.
        """
        depth = 0
        offset = 0
        while True:
            tok = self._peek(offset)
            if tok is None:
                return False
            if tok.kind == TokenKind.LBRACE:
                depth += 1
            elif tok.kind == TokenKind.RBRACE:
                depth -= 1
                if depth == 0:
                    return True
            elif tok.kind == TokenKind.SEMICOLON and depth == 1:
                return False
            offset += 1

    def _brace_looks_like_map(self) -> bool:
        """True if the ``{ ... }`` at the cursor has a top-level ``:``.

        Struct construction is positional and never contains a top-level
        colon, so this distinguishes ``Type<T> { a, b }`` from a comparison
        followed by a map literal ``A < B > { "k": v }``.
        """
        depth = 0
        offset = 0
        while True:
            tok = self._peek(offset)
            if tok is None:
                return False
            if tok.kind == TokenKind.LBRACE:
                depth += 1
            elif tok.kind == TokenKind.RBRACE:
                depth -= 1
                if depth == 0:
                    return False
            elif tok.kind == TokenKind.COLON and depth == 1:
                return True
            offset += 1

    def _try_parse_generic_struct_construct(self) -> Optional[Type]:
        """Speculatively parse ``Name<Args> { ... }`` as a struct construction.

        Angle brackets are ambiguous between generics and comparisons, so the
        parse is rolled back unless a brace that looks like positional struct
        arguments follows immediately.
        """
        snap = self._snapshot()
        try:
            type_ = self._parse_type()
            if (
                self._at(TokenKind.LBRACE)
                and self._brace_is_struct_construct()
                and not self._brace_looks_like_map()
            ):
                return type_
        except ParseError:
            pass
        self._restore(snap)
        return None

    def _synchronize_statement(self) -> None:
        """Panic-mode recovery inside a block: skip to the next statement
        boundary (``;`` is consumed, ``}`` and statement starters are not)."""
        while True:
            tok = self._peek()
            if tok is None:
                return
            if tok.kind == TokenKind.SEMICOLON:
                self._advance()
                return
            if tok.kind == TokenKind.RBRACE or tok.kind in _STMT_START:
                return
            self._advance()

    def _synchronize_top_level(self) -> None:
        """Panic-mode recovery at the top level: skip to the next declaration
        starter or EOF."""
        while True:
            tok = self._peek()
            if tok is None:
                return
            if tok.kind in _TOP_LEVEL_START:
                return
            self._advance()

    def _skip_to_entry_boundary(self, *, consume_close: bool = False) -> None:
        """After an error inside a ``{ ... }`` literal, consume tokens up to
        the next entry separator (`,`) or the matching ``}``.

        A trailing `,` is consumed so the literal loop can continue with the
        next entry; a `}` is normally left for ``_expect`` to consume, unless
        ``consume_close`` is set (the closing brace already failed to match,
        so it is swallowed here to let the enclosing statement finish).
        Nested braces are tracked so a ``}`` inside a nested literal is not
        mistaken for this literal's closing brace.
        """
        depth = 0
        while True:
            tok = self._peek()
            if tok is None:
                return
            if tok.kind == TokenKind.LBRACE:
                depth += 1
            elif tok.kind == TokenKind.RBRACE:
                if depth == 0:
                    if consume_close:
                        self._advance()
                    return
                depth -= 1
            elif tok.kind == TokenKind.COMMA and depth == 0:
                self._advance()
                return
            self._advance()

    # -- program -----------------------------------------------------------

    def parse_program(self) -> Program:
        first = self._peek()
        line = first.line if first is not None else 1
        column = first.column if first is not None else 1
        items: list[Node] = []
        while self._peek() is not None:
            pub = self._match(TokenKind.PUB) is not None
            try:
                items.append(self._parse_item(pub))
            except ParseError as exc:
                self.errors.append(exc)
                self._synchronize_top_level()
                items.append(ErrorStmt(exc.line, exc.column, exc.message))
        return Program(line, column, items)

    def _parse_item(self, pub: bool) -> Node:
        tok = self._peek()
        if tok is None:
            self._error("expected a top-level declaration")
        if tok.kind == TokenKind.CONST:
            return self._parse_const(pub)
        if tok.kind == TokenKind.TYPE:
            return self._parse_type_decl(pub)
        if tok.kind == TokenKind.TYPEDEF:
            return self._parse_typedef(pub)
        if tok.kind == TokenKind.STRUCT:
            return self._parse_struct(pub)
        if tok.kind == TokenKind.ENUM:
            return self._parse_enum(pub)
        if tok.kind == TokenKind.TRAIT:
            return self._parse_trait(pub)
        if tok.kind == TokenKind.IMPL:
            return self._parse_impl()
        if tok.kind == TokenKind.EXTRA:
            return self._parse_extra()
        if tok.kind == TokenKind.GROUP:
            return self._parse_group()
        if tok.kind == TokenKind.FN:
            return self._parse_fn(pub=pub)
        if tok.kind == TokenKind.IDENTIFIER:
            nxt = self._peek(1)
            if nxt is not None and nxt.kind == TokenKind.AT:
                return self._parse_group_apply()
        self._error(f"unexpected token {tok.raw!r} at top level", tok)

    def _parse_const(self, pub: bool) -> ConstDecl:
        tok = self._advance()  # const
        name = self._expect(TokenKind.IDENTIFIER, what="constant name")
        self._expect(TokenKind.COLON, what="':' in const declaration")
        type_ = self._parse_type()
        self._expect(TokenKind.ASSIGN, what="'=' in const declaration")
        value = self._parse_expr(allow_map_literal=True)
        self._expect(TokenKind.SEMICOLON, what="';' after const declaration")
        return ConstDecl(tok.line, tok.column, str(name.value), type_, value, pub)

    def _parse_type_decl(self, pub: bool) -> TypeDecl:
        tok = self._advance()  # type
        name = self._expect(TokenKind.IDENTIFIER, what="type name")
        self._expect(TokenKind.ASSIGN, what="'=' in type declaration")
        base = self._parse_type()
        where: Optional[Block] = None
        if self._match(TokenKind.WHERE) is not None:
            where = self._parse_block()
        return TypeDecl(tok.line, tok.column, str(name.value), base, where, pub)

    def _parse_typedef(self, pub: bool) -> TypeDecl:
        """Parse a type alias: ``typedef Name [<Params>] = Type;``.

        Generic parameters may be declared explicitly after the name; when
        omitted, the semantic analyzer infers them from the right-hand side's
        unknown type names.
        """
        tok = self._advance()  # typedef
        name = self._expect(TokenKind.IDENTIFIER, what="alias name")
        params = self._parse_generic_params()
        self._expect(TokenKind.ASSIGN, what="'=' in typedef")
        base = self._parse_type()
        self._expect(TokenKind.SEMICOLON, what="';' after typedef")
        return TypeDecl(tok.line, tok.column, str(name.value), base, None, pub, params)

    def _parse_struct(self, pub: bool) -> StructDecl:
        tok = self._advance()  # struct
        name = self._expect(TokenKind.IDENTIFIER, what="struct name")
        params = self._parse_generic_params()
        if self._match(TokenKind.SEMICOLON) is not None:
            # unit struct: `struct Name;`
            return StructDecl(tok.line, tok.column, str(name.value), params, [], pub)
        self._expect(TokenKind.LBRACE, what="'{' after struct name")
        fields: list[Field] = []
        while not self._at(TokenKind.RBRACE):
            fields.append(self._parse_field())
            if self._match(TokenKind.COMMA) is None and self._match(TokenKind.SEMICOLON) is None:
                break
        self._advance()  # }
        return StructDecl(tok.line, tok.column, str(name.value), params, fields, pub)

    def _parse_field(self) -> Field:
        tok = self._peek()
        if tok is None:
            self._error("expected struct field")
        pub = self._match(TokenKind.PUB) is not None
        static = self._match(TokenKind.STATIC) is not None
        name = self._expect(TokenKind.IDENTIFIER, what="field name")
        self._expect(TokenKind.COLON, what="':' after field name")
        type_ = self._parse_type()
        validation: Optional[Block] = None
        if self._match(TokenKind.WHERE) is not None:
            validation = self._parse_validation_block()
        elif self._match(TokenKind.ARROW) is not None:
            validation = self._parse_validation_block()
        initializer: Optional[Node] = None
        if self._match(TokenKind.ASSIGN) is not None:
            initializer = self._parse_expr(allow_map_literal=True)
        return Field(tok.line, tok.column, str(name.value), type_, pub, static, validation, initializer)

    def _parse_enum(self, pub: bool) -> EnumDecl:
        tok = self._advance()  # enum
        name = self._expect(TokenKind.IDENTIFIER, what="enum name")
        self._expect(TokenKind.LBRACE, what="'{' after enum name")
        variants: list[Variant] = []
        while not self._at(TokenKind.RBRACE):
            vt = self._expect(TokenKind.IDENTIFIER, what="enum variant name")
            value: Optional[int] = None
            if self._match(TokenKind.ASSIGN) is not None:
                num = self._expect(TokenKind.INTEGER, what="integer variant value")
                value = cast(int, num.value)
            variants.append(Variant(vt.line, vt.column, str(vt.value), value))
            if self._match(TokenKind.COMMA) is None:
                break
        self._expect(TokenKind.RBRACE, what="'}' after enum variants")
        return EnumDecl(tok.line, tok.column, str(name.value), variants, pub)

    def _parse_trait(self, pub: bool) -> TraitDecl:
        tok = self._advance()  # trait
        name = self._expect(TokenKind.IDENTIFIER, what="trait name")
        params = self._parse_generic_params()
        self._expect(TokenKind.LBRACE, what="'{' after trait name")
        methods: list[FnDecl] = []
        while not self._at(TokenKind.RBRACE):
            method_pub = self._match(TokenKind.PUB) is not None
            methods.append(self._parse_fn(pub=method_pub, body_required=False))
        self._advance()  # }
        return TraitDecl(tok.line, tok.column, str(name.value), params, methods, pub)

    def _parse_impl(self) -> ImplDecl:
        tok = self._advance()  # impl
        params = self._parse_generic_params()
        trait = self._parse_type()
        self._expect(TokenKind.FOR, what="'for' in impl declaration")
        struct = self._parse_type()
        self._expect(TokenKind.LBRACE, what="'{' after impl header")
        methods: list[FnDecl] = []
        while not self._at(TokenKind.RBRACE):
            method_pub = self._match(TokenKind.PUB) is not None
            method_static = self._match(TokenKind.STATIC) is not None
            methods.append(self._parse_fn(pub=method_pub, static=method_static))
        self._advance()  # }
        return ImplDecl(tok.line, tok.column, trait, struct, params, methods)

    def _parse_extra(self) -> ExtraDecl:
        tok = self._advance()  # extra
        params = self._parse_generic_params()
        struct = self._parse_type()
        self._expect(TokenKind.LBRACE, what="'{' after extra header")
        methods: list[FnDecl] = []
        while not self._at(TokenKind.RBRACE):
            method_pub = self._match(TokenKind.PUB) is not None
            method_static = self._match(TokenKind.STATIC) is not None
            methods.append(self._parse_fn(pub=method_pub, static=method_static))
        self._advance()  # }
        return ExtraDecl(tok.line, tok.column, struct, params, methods)

    def _parse_group(self) -> GroupDecl:
        tok = self._advance()  # group
        name = self._expect(TokenKind.IDENTIFIER, what="group name")
        params: list[Param] = []
        struct: Optional[str] = None
        if self._at(TokenKind.LPAREN):
            params = self._parse_params()
        elif self._match(TokenKind.COLON) is not None:
            struct = str(self._expect(TokenKind.IDENTIFIER, what="struct name").value)
        self._expect(TokenKind.LBRACE, what="'{' after group header")
        if self._at(TokenKind.RBRACE):
            self._error("group policy cannot be empty", self._peek())
        distributions: list[Distribution] = []
        while not self._at(TokenKind.RBRACE):
            distributions.append(self._parse_distribution())
        self._advance()  # }
        return GroupDecl(tok.line, tok.column, str(name.value), params, struct, distributions)

    def _parse_distribution(self) -> Distribution:
        tok = self._peek()
        if tok is None:
            self._error("expected group distribution")
        subject_self = False
        if self._at(TokenKind.IDENTIFIER, value="self"):
            subject_self = True
            self._advance()
            self._expect(TokenKind.DOT, what="'.' after 'self' in distribution")
            subject = str(self._expect(TokenKind.IDENTIFIER, what="field name").value)
        else:
            subject = str(self._expect(TokenKind.IDENTIFIER, what="parameter name").value)
        self._expect(TokenKind.ARROW, what="'->' in group distribution")
        type_ = self._parse_type()
        self._expect(TokenKind.SEMICOLON, what="';' after group distribution")
        return Distribution(tok.line, tok.column, subject, type_, subject_self)

    def _parse_group_apply(self) -> GroupApply:
        group = self._expect(TokenKind.IDENTIFIER, what="group name")
        self._expect(TokenKind.AT, what="'@' in group application")
        struct = self._expect(TokenKind.IDENTIFIER, what="struct name")
        self._expect(TokenKind.ARROW, what="'->' in group application")
        self._expect(TokenKind.LBRACE, what="'{' after '->'")
        fields: list[str] = []
        while not self._at(TokenKind.RBRACE):
            fields.append(str(self._expect(TokenKind.IDENTIFIER, what="field name").value))
            if self._match(TokenKind.COMMA) is None:
                break
        self._expect(TokenKind.RBRACE, what="'}' after group fields")
        self._match(TokenKind.SEMICOLON)  # optional: the grammar example omits it
        return GroupApply(group.line, group.column, str(group.value), str(struct.value), fields)

    def _parse_fn(
        self,
        *,
        pub: bool = False,
        static: bool = False,
        body_required: bool = True,
    ) -> FnDecl:
        tok = self._advance()  # fn
        name = self._expect(TokenKind.IDENTIFIER, what="function name")
        type_params = self._parse_generic_params()
        params = self._parse_params()
        return_type: Optional[Type] = None
        if self._match(TokenKind.ARROW) is not None:
            return_type = self._parse_type()
        which: Optional[str] = None
        if self._match(TokenKind.COMMA) is not None:
            self._expect(TokenKind.WHICH, what="'which' in function signature")
            self._expect(TokenKind.PATH, what="'::' after 'which'")
            which = str(self._expect(TokenKind.IDENTIFIER, what="method name after 'which ::'").value)
        if body_required or self._at(TokenKind.LBRACE):
            body = self._parse_block()
        else:
            body = None
            self._expect(TokenKind.SEMICOLON, what="';' after function signature")
        return FnDecl(
            tok.line,
            tok.column,
            str(name.value),
            type_params,
            params,
            return_type,
            body,
            pub,
            static,
            which,
        )

    def _parse_params(self) -> list[Param]:
        self._expect(TokenKind.LPAREN, what="'(' before parameter list")
        params: list[Param] = []
        while not self._at(TokenKind.RPAREN):
            tok = self._expect(TokenKind.IDENTIFIER, what="parameter name")
            type_: Optional[Type] = None
            if self._match(TokenKind.COLON) is not None:
                type_ = self._parse_type()
            elif str(tok.value) != "self":
                self._error("parameter requires a type annotation", tok)
            params.append(Param(tok.line, tok.column, str(tok.value), type_))
            if self._match(TokenKind.COMMA) is None:
                break
        self._expect(TokenKind.RPAREN, what="')' after parameter list")
        return params

    def _parse_generic_params(self) -> list[TypeParam]:
        """Parse an optional generic parameter list: ``<T, U: Bound>``."""
        if self._match(TokenKind.LT) is None:
            return []
        params: list[TypeParam] = []
        while True:
            tok = self._expect(TokenKind.IDENTIFIER, what="generic parameter name")
            bound: Optional[Type] = None
            if self._match(TokenKind.COLON) is not None:
                bound = self._parse_type()
            params.append(TypeParam(tok.line, tok.column, str(tok.value), bound))
            if self._match(TokenKind.COMMA) is None:
                break
        self._expect_gt("'>' closing generic parameter list")
        return params

    # -- types -------------------------------------------------------------

    def _parse_type(self) -> Type:
        tok = self._expect(TokenKind.IDENTIFIER, what="type name")
        args: list[Type] = []
        if self._match(TokenKind.LT) is not None:
            while True:
                args.append(self._parse_type())
                if self._match(TokenKind.COMMA) is None:
                    break
            self._expect_gt("'>' closing generic type")
        return Type(tok.line, tok.column, str(tok.value), args)

    # -- statements --------------------------------------------------------

    def _parse_block(self) -> Block:
        tok = self._expect(TokenKind.LBRACE, what="'{' to open a block")
        stmts: list[Node] = []
        while not self._at(TokenKind.RBRACE):
            if self._peek() is None:
                self._error("expected '}' to close the block", tok)
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
        return Block(tok.line, tok.column, stmts)

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
        if tok.kind == TokenKind.WHILE:
            return self._parse_while()
        if tok.kind == TokenKind.FOR:
            return self._parse_for()
        if tok.kind == TokenKind.LBRACE:
            return self._parse_block()
        expr = self._parse_expr()
        self._expect(TokenKind.SEMICOLON, what="';' after statement")
        return ExprStmt(expr.line, expr.column, expr)

    def _parse_let(self) -> LetStmt:
        tok = self._advance()  # let
        name = self._expect(TokenKind.IDENTIFIER, what="variable name")
        self._expect(TokenKind.COLON, what="':' after variable name (let needs a type)")
        type_ = self._parse_type()
        value: Optional[Node] = None
        if self._match(TokenKind.ASSIGN) is not None:
            value = self._parse_expr(allow_map_literal=True)
        self._expect(TokenKind.SEMICOLON, what="';' after let declaration")
        return LetStmt(tok.line, tok.column, str(name.value), type_, value)

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

    def _parse_while(self) -> WhileStmt:
        tok = self._advance()  # while
        self._expect(TokenKind.LPAREN, what="'(' after 'while'")
        cond = self._parse_expr()
        self._expect(TokenKind.RPAREN, what="')' after while condition")
        body = self._parse_block()
        return WhileStmt(tok.line, tok.column, cond, body)

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
            body = self._parse_block()
            return ForStmt(tok.line, tok.column, str(var.value), iterable, body, type_, True)
        if self._at(TokenKind.IDENTIFIER, value="in"):
            self._error("expected iteration variable before 'in'", self._peek())
        var = self._expect(TokenKind.IDENTIFIER, what="loop variable")
        in_tok = self._peek()
        if not (in_tok is not None and in_tok.kind == TokenKind.IDENTIFIER and in_tok.value == "in"):
            self._error("expected 'in' in for-in loop", in_tok)
        self._advance()  # in
        iterable = self._parse_expr()
        body = self._parse_block()
        return ForStmt(tok.line, tok.column, str(var.value), iterable, body, None, False)

    # -- expressions -------------------------------------------------------

    def _parse_expr(self, *, allow_map_literal: bool = False) -> Node:
        left = self._parse_or(allow_map_literal=allow_map_literal)
        tok = self._peek()
        if tok is not None and tok.kind in _ASSIGN_OPS:
            op = self._advance()
            right = self._parse_expr(allow_map_literal=True)  # right-associative
            return Assign(left.line, left.column, left, op.kind, right)
        return left

    def _parse_or(self, *, allow_map_literal: bool = False) -> Node:
        node = self._parse_and(allow_map_literal=allow_map_literal)
        while self._at(TokenKind.OR):
            op = self._advance()
            node = BinOp(
                node.line,
                node.column,
                node,
                op.kind,
                self._parse_and(allow_map_literal=allow_map_literal),
            )
        return node

    def _parse_and(self, *, allow_map_literal: bool = False) -> Node:
        node = self._parse_equality(allow_map_literal=allow_map_literal)
        while self._at(TokenKind.AND):
            op = self._advance()
            node = BinOp(
                node.line,
                node.column,
                node,
                op.kind,
                self._parse_equality(allow_map_literal=allow_map_literal),
            )
        return node

    def _parse_equality(self, *, allow_map_literal: bool = False) -> Node:
        node = self._parse_relational(allow_map_literal=allow_map_literal)
        while (tok := self._peek()) is not None and tok.kind in _EQUALITY_OPS:
            op = self._advance()
            node = BinOp(
                node.line,
                node.column,
                node,
                op.kind,
                self._parse_relational(allow_map_literal=allow_map_literal),
            )
        return node

    def _parse_relational(self, *, allow_map_literal: bool = False) -> Node:
        node = self._parse_additive(allow_map_literal=allow_map_literal)
        while (tok := self._peek()) is not None and tok.kind in _RELATIONAL_OPS:
            op = self._advance()
            node = BinOp(
                node.line,
                node.column,
                node,
                op.kind,
                self._parse_additive(allow_map_literal=allow_map_literal),
            )
        return node

    def _parse_additive(self, *, allow_map_literal: bool = False) -> Node:
        node = self._parse_multiplicative(allow_map_literal=allow_map_literal)
        while (tok := self._peek()) is not None and tok.kind in _ADDITIVE_OPS:
            op = self._advance()
            node = BinOp(
                node.line,
                node.column,
                node,
                op.kind,
                self._parse_multiplicative(allow_map_literal=allow_map_literal),
            )
        return node

    def _parse_multiplicative(self, *, allow_map_literal: bool = False) -> Node:
        node = self._parse_shift(allow_map_literal=allow_map_literal)
        while (tok := self._peek()) is not None and tok.kind in _MULTIPLICATIVE_OPS:
            op = self._advance()
            node = BinOp(
                node.line,
                node.column,
                node,
                op.kind,
                self._parse_shift(allow_map_literal=allow_map_literal),
            )
        return node

    def _parse_shift(self, *, allow_map_literal: bool = False) -> Node:
        node = self._parse_band(allow_map_literal=allow_map_literal)
        while (tok := self._peek()) is not None and tok.kind in _SHIFT_OPS:
            op = self._advance()
            node = BinOp(
                node.line,
                node.column,
                node,
                op.kind,
                self._parse_band(allow_map_literal=allow_map_literal),
            )
        return node

    def _parse_band(self, *, allow_map_literal: bool = False) -> Node:
        node = self._parse_bxor(allow_map_literal=allow_map_literal)
        while self._at(TokenKind.AMP):
            op = self._advance()
            node = BinOp(
                node.line,
                node.column,
                node,
                op.kind,
                self._parse_bxor(allow_map_literal=allow_map_literal),
            )
        return node

    def _parse_bxor(self, *, allow_map_literal: bool = False) -> Node:
        node = self._parse_bor(allow_map_literal=allow_map_literal)
        while self._at(TokenKind.CARET):
            op = self._advance()
            node = BinOp(
                node.line,
                node.column,
                node,
                op.kind,
                self._parse_bor(allow_map_literal=allow_map_literal),
            )
        return node

    def _parse_bor(self, *, allow_map_literal: bool = False) -> Node:
        node = self._parse_unary(allow_map_literal=allow_map_literal)
        while self._at(TokenKind.PIPE):
            op = self._advance()
            node = BinOp(
                node.line,
                node.column,
                node,
                op.kind,
                self._parse_unary(allow_map_literal=allow_map_literal),
            )
        return node

    def _parse_unary(self, *, allow_map_literal: bool = False) -> Node:
        tok = self._peek()
        if tok is not None and tok.kind in _UNARY_OPS:
            op = self._advance()
            return UnaryOp(
                tok.line,
                tok.column,
                op.kind,
                self._parse_unary(allow_map_literal=allow_map_literal),
            )
        return self._parse_postfix(allow_map_literal=allow_map_literal)

    def _parse_postfix(self, *, allow_map_literal: bool = False) -> Node:
        node = self._parse_primary(allow_map_literal=allow_map_literal)
        while True:
            tok = self._peek()
            if tok is None:
                break
            if tok.kind == TokenKind.DOT:
                self._advance()
                name = self._expect(TokenKind.IDENTIFIER, what="member name after '.'")
                node = Attribute(node.line, node.column, node, str(name.value))
            elif tok.kind == TokenKind.LPAREN:
                args = self._parse_call_args(allow_map_literal=allow_map_literal)
                node = Call(node.line, node.column, node, args)
            elif tok.kind == TokenKind.LBRACKET:
                node = self._parse_index_or_slice(node, allow_map_literal=allow_map_literal)
            else:
                break
        return node

    def _parse_primary(self, *, allow_map_literal: bool = False) -> Node:
        tok = self._peek()
        if tok is None:
            self._error("expected expression")
        if tok.kind == TokenKind.INTEGER:
            self._advance()
            return IntLit(tok.line, tok.column, cast(int, tok.value), tok.raw)
        if tok.kind == TokenKind.FLOAT:
            self._advance()
            return FloatLit(tok.line, tok.column, cast(float, tok.value), tok.raw)
        if tok.kind == TokenKind.STRING:
            self._advance()
            return StrLit(tok.line, tok.column, str(tok.value), tok.raw)
        if tok.kind == TokenKind.LPAREN:
            self._advance()
            node = self._parse_expr()
            self._expect(TokenKind.RPAREN, what="')' after parenthesized expression")
            return node
        if tok.kind == TokenKind.IDENTIFIER and tok.value in ("true", "false"):
            self._advance()
            return BoolLit(tok.line, tok.column, tok.value == "true", tok.raw)
        if tok.kind == TokenKind.LBRACKET:
            return self._parse_vector_literal(allow_map_literal=allow_map_literal)
        if tok.kind == TokenKind.LBRACE:
            # Grammar.md: `{ ... }` is a map literal only on the right of `=`.
            if allow_map_literal:
                return self._parse_map_literal()
            self._error("unexpected token '{' in expression", tok)
        if tok.kind == TokenKind.IDENTIFIER:
            generic_type = self._try_parse_generic_struct_construct()
            if generic_type is not None:
                return self._parse_struct_construct(
                    generic_type, allow_map_literal=allow_map_literal
                )
            name = self._parse_name_path()
            if self._at(TokenKind.LBRACE) and self._brace_is_struct_construct():
                type_ = Type(name.line, name.column, "::".join(name.parts))
                return self._parse_struct_construct(
                    type_, allow_map_literal=allow_map_literal
                )
            return name
        self._error(f"unexpected token {tok.raw!r} in expression", tok)

    def _parse_name_path(self) -> Name:
        tok = self._expect(TokenKind.IDENTIFIER, what="name")
        parts = [str(tok.value)]
        while self._at(TokenKind.PATH):
            self._advance()
            part = self._expect(TokenKind.IDENTIFIER, what="name after '::'")
            parts.append(str(part.value))
        return Name(tok.line, tok.column, parts)

    def _parse_call_args(self, *, allow_map_literal: bool = False) -> list[Arg]:
        self._advance()  # (
        args: list[Arg] = []
        while not self._at(TokenKind.RPAREN):
            tok = self._peek()
            if tok is None:
                self._error("expected ')' to close the call")
            value = self._parse_expr(allow_map_literal=allow_map_literal)
            args.append(Arg(tok.line, tok.column, value))
            if self._match(TokenKind.COMMA) is None:
                break
        self._expect(TokenKind.RPAREN, what="')' after call arguments")
        return args

    def _parse_vector_literal(self, *, allow_map_literal: bool = False) -> VectorLit:
        tok = self._advance()  # [
        elems: list[Node] = []
        while not self._at(TokenKind.RBRACKET):
            elems.append(self._parse_expr(allow_map_literal=allow_map_literal))
            if self._match(TokenKind.COMMA) is None:
                break
        self._expect(TokenKind.RBRACKET, what="']' after vector literal")
        return VectorLit(tok.line, tok.column, elems)

    def _parse_map_literal(self) -> MapLit:
        tok = self._advance()  # {
        entries: list[MapEntry] = []
        while not self._at(TokenKind.RBRACE):
            if self._peek() is None:
                self._error("expected '}' after map literal", tok)
            try:
                key = self._parse_expr(allow_map_literal=True)
                self._expect(TokenKind.COLON, what="':' between map key and value")
                value = self._parse_expr(allow_map_literal=True)
            except ParseError as exc:
                self.errors.append(exc)
                self._skip_to_entry_boundary()
                continue
            entries.append(MapEntry(key.line, key.column, key, value))
            if self._match(TokenKind.COMMA) is None:
                break
        try:
            self._expect(TokenKind.RBRACE, what="'}' after map literal")
        except ParseError as exc:
            self.errors.append(exc)
            self._skip_to_entry_boundary(consume_close=True)
        return MapLit(tok.line, tok.column, entries)

    def _parse_struct_construct(
        self, type_: Type, *, allow_map_literal: bool = False
    ) -> StructConstruct:
        tok = self._advance()  # {
        args: list[Node] = []
        while not self._at(TokenKind.RBRACE):
            if self._peek() is None:
                self._error("expected '}' after struct construction", tok)
            try:
                args.append(self._parse_expr(allow_map_literal=allow_map_literal))
            except ParseError as exc:
                self.errors.append(exc)
                self._skip_to_entry_boundary()
                continue
            if self._match(TokenKind.COMMA) is None:
                break
        try:
            self._expect(TokenKind.RBRACE, what="'}' after struct construction")
        except ParseError as exc:
            self.errors.append(exc)
            self._skip_to_entry_boundary(consume_close=True)
        return StructConstruct(type_.line, type_.column, type_, args)

    def _parse_index_or_slice(
        self, obj: Node, *, allow_map_literal: bool = False
    ) -> Node:
        self._advance()  # [
        if self._at(TokenKind.PATH):
            # [::step]
            self._advance()
            step: Optional[Node] = None
            if not self._at(TokenKind.RBRACKET):
                step = self._parse_expr(allow_map_literal=allow_map_literal)
            self._expect(TokenKind.RBRACKET, what="']' after slice")
            return Slice(obj.line, obj.column, obj, None, None, step)
        start: Optional[Node] = None
        if not self._at(TokenKind.COLON):
            start = self._parse_expr(allow_map_literal=allow_map_literal)
        if self._match(TokenKind.COLON) is not None:
            stop: Optional[Node] = None
            if not self._at(TokenKind.COLON) and not self._at(TokenKind.RBRACKET):
                stop = self._parse_expr(allow_map_literal=allow_map_literal)
            step = None
            if self._match(TokenKind.COLON) is not None:
                if not self._at(TokenKind.RBRACKET):
                    step = self._parse_expr(allow_map_literal=allow_map_literal)
            self._expect(TokenKind.RBRACKET, what="']' after slice")
            return Slice(obj.line, obj.column, obj, start, stop, step)
        self._expect(TokenKind.RBRACKET, what="']' after index")
        if start is None:
            self._error("expected index expression", self._peek())
        return Index(obj.line, obj.column, obj, start)


def parse(tokens: list[Token]) -> Program:
    """Parse a token list into a :class:`Program`; raise the first ParseError."""
    result = parse_with_errors(tokens)
    if result.errors:
        raise result.errors[0]
    return result.program


def parse_with_errors(tokens: list[Token]) -> ParseResult:
    """Parse a token list, collecting every :class:`ParseError`.

    The parser recovers by skipping to statement/declaration boundaries, so a
    single run reports as many independent errors as possible.
    """
    parser = Parser(tokens)
    program = parser.parse_program()
    return ParseResult(program, list(parser.errors))


def parse_source(source: str, *, emit_comments: bool = False) -> Program:
    """Tokenize and parse a CWind source string."""
    return parse(tokenize(source, emit_comments=emit_comments))


def parse_file(
    path: Union[str, os.PathLike[str]],
    *,
    emit_comments: bool = False,
) -> Program:
    """Tokenize and parse a CWind source file."""
    return parse(tokenize_file(path, emit_comments=emit_comments))
