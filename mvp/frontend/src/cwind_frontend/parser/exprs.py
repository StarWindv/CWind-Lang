"""Parser mixin: expression parsing (precedence ladder)."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
from collections import deque
from dataclasses import dataclass, field, fields as _dc_fields
from typing import NoReturn, Optional, Sequence, Union, cast

from ..ast_components.ast import (
    Arg,
    AssocType,
    AssocTypeDecl,
    Assign,
    Attribute,
    BindPattern,
    BinOp,
    Block,
    BoolLit,
    BreakStmt,
    Call,
    CastExpr,
    ConstDecl,
    ContinueStmt,
    Distribution,
    ElifBranch,
    EnumPattern,
    EnumDecl,
    ErrorStmt,
    ExprStmt,
    ExternBlock,
    ExternStatic,
    ExtraDecl,
    Field,
    FloatLit,
    FnDecl,
    ForStmt,
    GroupApply,
    GroupDecl,
    IfStmt,
    IfLetBranch,
    IfLetStmt,
    ImplDecl,
    Index,
    IntLit,
    LetStmt,
    LitPattern,
    MapEntry,
    MapLit,
    MatchArm,
    MatchStmt,
    ModDecl,
    Name,
    Node,
    Param,
    Program,
    ReturnStmt,
    Slice,
    StrLit,
    StructConstruct,
    Closure,
    StructDecl,
    StructPattern,
    StructPatternField,
    TraitDecl,
    TuplePattern,
    Type,
    TypeDecl,
    TypeParam,
    TupleLit,
    UnaryOp,
    UseDecl,
    Variant,
    VectorLit,
    WhileLetStmt,
    LetChainSeg,
    WhileStmt,
    WildcardPattern,
)
from ..ast_components.errors import FrontendError
from ..ast_components.token import Token, TokenKind
from ..cfg import (
    CFG_COMBINATORS,
    CFG_FLAGS,
    CFG_KEYS,
    CFG_KEY_VALUES,
    CfgContext,
    CfgPredicate,
    evaluate_cfg,
)
from ..lexer import tokenize, tokenize_file
from ..breeze import MANIFEST_NAME, ManifestError, load_manifest

from ..ast_components.ast import _type_name_for_type

from .defs import (
    ParseError,
    ParseResult,
    _ASSIGN_OPS,
    _RELATIONAL_OPS,
    _EQUALITY_OPS,
    _ADDITIVE_OPS,
    _MULTIPLICATIVE_OPS,
    _SHIFT_OPS,
    _UNARY_OPS,
    _STMT_START,
    _TOP_LEVEL_START,
    _IMPORT_ROOTS,
    _SOURCE_SUFFIXES,
    ModuleTrieNode,
    _library_fingerprint,
    _MODULE_TREE_CACHE,
    _module_parts,
    ModuleRoot,
    _module_roots,
    _scan_mod_declarations,
    _scan_reexports,
    _find_mod_entry,
    _resolve_declared_entry,
    _build_library_trie,
    ModuleTree,
    _library_tree,
    _NO_PRELUDE_SENTINEL,
    _IMPL_REGISTRY_CACHE,
    _IMPL_REGISTRY_BOOT_CACHE,
    _impl_registry_for,
    _NAME_BINDING_NODES,
    _referenced_names,
    _entry_project_root,
    _localize_qualified_refs,
    _module_mangle_suffix,
    _mangled_item_name,
    _declared_name_field,
    _set_declared_name,
    _SCOPE_PUSH_NODES,
    _rewrite_module_refs,
)


class ParserExprs:
    def _brace_is_struct_construct(self) -> bool:
        """True if the ``{ ... }`` at the cursor is a struct construction
        (comma-separated expressions) rather than a block.

        A top-level ``;`` inside the braces means it is a statement block
        (e.g. a for/while body following its iterable expression); struct
        arguments never contain statements.  bug-35: ``;`` inside ``(...)``
        or ``[...]`` (array types / ``[x; N]`` repeat literals) is not a
        statement separator, so those nestings are tracked as well.
        """
        depth = 0
        group_depth = 0
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
            elif tok.kind in (TokenKind.LPAREN, TokenKind.LBRACKET):
                group_depth += 1
            elif tok.kind in (TokenKind.RPAREN, TokenKind.RBRACKET):
                group_depth -= 1
            elif tok.kind == TokenKind.SEMICOLON and depth == 1 \
                    and group_depth == 0:
                return False
            offset += 1

    def _brace_looks_like_map(self) -> bool:
        """True if the ``{ ... }`` at the cursor has a top-level ``:``.

        Struct construction is positional and never contains a top-level
        colon, so this distinguishes ``Type<T> { a, b }`` from a comparison
        followed by a map literal ``A < B > { "k": v }``.  Colons inside
        ``(...)``/``[...]`` nestings are ignored (bug-35 mirrors).
        """
        depth = 0
        group_depth = 0
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
            elif tok.kind in (TokenKind.LPAREN, TokenKind.LBRACKET):
                group_depth += 1
            elif tok.kind in (TokenKind.RPAREN, TokenKind.RBRACKET):
                group_depth -= 1
            elif tok.kind == TokenKind.COLON and depth == 1 \
                    and group_depth == 0:
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
            # todo-165: at the top level of a while-let chain every ``&&``
            # belongs to the chain, not this boolean expression.
            if self._let_chain_ctx:
                break
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
        node = self._parse_cast(allow_map_literal=allow_map_literal)
        while self._at(TokenKind.PIPE):
            op = self._advance()
            node = BinOp(
                node.line,
                node.column,
                node,
                op.kind,
                self._parse_cast(allow_map_literal=allow_map_literal),
            )
        return node

    def _parse_cast(self, *, allow_map_literal: bool = False) -> Node:
        # todo-17: ``expr as T`` — Rust precedence: tighter than ``|``,
        # looser than unary, so ``-x as T`` is ``(-x) as T``.
        node = self._parse_unary(allow_map_literal=allow_map_literal)
        while self._at(TokenKind.AS):
            tok = self._advance()
            target = self._parse_type()
            node = CastExpr(tok.line, tok.column, node, target)
        return node

    def _parse_unary(self, *, allow_map_literal: bool = False) -> Node:
        tok = self._peek()
        if tok is not None and tok.kind in _UNARY_OPS:
            op = self._advance()
            # bug-46: ``&mut expr`` —— 借用表达式后允许可变标记;
            # ``mut`` 是关键字, 不可能是操作数首符, 消费它无歧义。
            mutable = (
                op.kind == TokenKind.AMP
                and self._match(TokenKind.MUT) is not None
            )
            return UnaryOp(
                tok.line,
                tok.column,
                op.kind,
                self._parse_unary(allow_map_literal=allow_map_literal),
                mutable=mutable,
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
                if self._at(TokenKind.INTEGER):
                    num_tok = self._peek()
                    self._advance()
                    member = str(num_tok.value)
                elif self._at(TokenKind.FLOAT):
                    # `p.0.0` 词法上是 `p . 0.0`: 把浮点拆成成员 `0` +
                    # 合成 `.0`, 让 postfix 链继续 (Rust tuple 元素访问)。
                    float_tok = self._peek()
                    parts = float_tok.raw.split(".", 1)
                    if (len(parts) == 2 and parts[0].isdigit()
                            and parts[1].isdigit()):
                        self._advance()
                        member = parts[0]
                        int_col = float_tok.column + len(parts[0]) + 1
                        self._pending.append(Token(
                            TokenKind.DOT, ".",
                            float_tok.line, float_tok.column + len(parts[0]),
                            float_tok.line, int_col, ".",
                        ))
                        self._pending.append(Token(
                            TokenKind.INTEGER, int(parts[1]),
                            float_tok.line, int_col,
                            float_tok.end_line, float_tok.end_column,
                            parts[1],
                        ))
                    else:
                        self._expect(
                            TokenKind.IDENTIFIER,
                            what="member name after '.'",
                        )
                        member = ""
                else:
                    name = self._expect(
                        TokenKind.IDENTIFIER, what="member name after '.'"
                    )
                    member = str(name.value)
                node = Attribute(node.line, node.column, node, member)
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
            if self._at(TokenKind.RPAREN):
                self._advance()
                return TupleLit(tok.line, tok.column, [])
            node = self._parse_expr(allow_map_literal=allow_map_literal)
            if self._match(TokenKind.COMMA) is None:
                self._expect(
                    TokenKind.RPAREN,
                    what="')' after parenthesized expression",
                )
                return node
            elems = [node]
            while not self._at(TokenKind.RPAREN):
                elems.append(
                    self._parse_expr(allow_map_literal=allow_map_literal)
                )
                if self._match(TokenKind.COMMA) is None:
                    break
            self._expect(TokenKind.RPAREN, what="')' after tuple literal")
            return TupleLit(tok.line, tok.column, elems)
        if tok.kind == TokenKind.IDENTIFIER and tok.value in ("true", "false"):
            self._advance()
            return BoolLit(tok.line, tok.column, tok.value == "true", tok.raw)
        if tok.kind == TokenKind.LBRACKET:
            return self._parse_vector_literal(allow_map_literal=allow_map_literal)
        if tok.kind == TokenKind.MATCH:
            return self._parse_match()
        if tok.kind == TokenKind.PIPE or tok.kind == TokenKind.OR:
            return self._parse_closure()
        if tok.kind == TokenKind.LBRACE:
            # Grammar.md: `{ ... }` is a map literal only on the right of `=`.
            if allow_map_literal:
                return self._parse_map_literal()
            self._error("unexpected token '{' in expression", tok)
        if tok.kind == TokenKind.IDENTIFIER:
            if not self._for_iterable_expr:
                generic_type = self._try_parse_generic_struct_construct()
                if generic_type is not None:
                    return self._parse_struct_construct(
                        generic_type, allow_map_literal=allow_map_literal
                    )
            name = self._parse_name_path()
            if (
                not self._for_iterable_expr
                and self._at(TokenKind.LBRACE)
                and self._brace_is_struct_construct()
            ):
                type_ = Type(name.line, name.column, "::".join(name.parts))
                return self._parse_struct_construct(
                    type_, allow_map_literal=allow_map_literal
                )
            return name
        self._error(f"unexpected token {tok.raw!r} in expression", tok)

    def _parse_name_path(self) -> Name:
        tok = self._expect(TokenKind.IDENTIFIER, what="name")
        parts = [self._ident_value(tok)]
        while self._at(TokenKind.PATH):
            self._advance()
            part = self._expect(TokenKind.IDENTIFIER, what="name after '::'")
            parts.append(self._ident_value(part))
        return Name(tok.line, tok.column, parts)

    def _parse_function_pointer(self) -> Name:
        """Parse a function-pointer type ``fn(A, B) -> R`` (type position).

        The signature is flattened into a single name string
        (``"fn(Int, String) -> Int"``) so the rest of the string-based
        type pipeline can carry it unchanged.
        """
        tok = self._expect(TokenKind.FN, what="'fn' in function-pointer type")
        self._expect(TokenKind.LPAREN, what="'(' after 'fn'")
        args: list[Type] = []
        while not self._at(TokenKind.RPAREN):
            args.append(self._parse_type())
            if self._match(TokenKind.COMMA) is None:
                break
        self._expect(TokenKind.RPAREN, what="')' after function-pointer arguments")
        ret = Type(tok.line, tok.column, "None")
        if self._match(TokenKind.ARROW) is not None:
            ret = self._parse_type()
        sig = "fn(" + ", ".join(_type_name_for_type(a) for a in args) + ")"
        if ret.name != "None" or ret.args:
            sig += " -> " + _type_name_for_type(ret)
        return Name(tok.line, tok.column, [sig])

    def _parse_closure(self) -> Closure:
        """Parse a Rust-like closure ``|x: Int| -> Int { x * 3 }``.

        ``|| -> Int { ... }`` is accepted for the zero-parameter form
        (the lexer produces a single ``OR`` token for the two pipes).
        """
        tok = self._peek()
        if tok is not None and tok.kind == TokenKind.OR:
            self._advance()
            params: list[Param] = []
        else:
            tok = self._expect(TokenKind.PIPE, what="'|' opening a closure")
            params = []
            while not self._at(TokenKind.PIPE):
                mutable = self._match(TokenKind.MUT) is not None
                name = self._expect(TokenKind.IDENTIFIER, what="closure parameter name")
                type_: Optional[Type] = None
                if self._match(TokenKind.COLON) is not None:
                    type_ = self._parse_type()
                param = Param(name.line, name.column, self._ident_value(name), type_)
                param.mutable = mutable
                params.append(param)
                if self._match(TokenKind.COMMA) is None:
                    break
            self._expect(TokenKind.PIPE, what="'|' closing closure parameters")
        ret: Optional[Type] = None
        if self._match(TokenKind.ARROW) is not None:
            ret = self._parse_type()
        body = self._parse_block()
        # 与函数体一致: 尾表达式降级成 return (后端只需处理 ReturnStmt)
        self._make_function_tail_return(body)
        return Closure(tok.line, tok.column, params, ret, body)

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
        repeat: Optional[int] = None
        if not self._at(TokenKind.RBRACKET):
            elems.append(self._parse_expr(allow_map_literal=allow_map_literal))
            if self._match(TokenKind.SEMICOLON) is not None:
                # bug-35: 定长数组重复字面量 `[x; N]` (Rust 风格): 单个元素
                # 重复 N 次; 计数只入运行时注解, 不进普通序列化字段
                len_tok = self._expect(
                    TokenKind.INTEGER,
                    what="repeat count after ';' in array literal",
                )
                repeat = cast(int, len_tok.value)
            elif self._match(TokenKind.COMMA) is not None:
                while not self._at(TokenKind.RBRACKET):
                    elems.append(
                        self._parse_expr(allow_map_literal=allow_map_literal)
                    )
                    if self._match(TokenKind.COMMA) is None:
                        break
        self._expect(TokenKind.RBRACKET, what="']' after vector literal")
        node = VectorLit(tok.line, tok.column, elems)
        if repeat is not None:
            node._typed_ann["repeat"] = repeat
        return node

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
