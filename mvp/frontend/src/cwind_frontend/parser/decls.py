"""Parser mixin: top-level declaration parsing."""

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


class ParserDecls:
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
        if tok.kind == TokenKind.EXTERN:
            return self._parse_extern_block(pub=pub)
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
        params = self._parse_generic_params()
        self._expect(TokenKind.LBRACE, what="'{' after enum name")
        variants: list[Variant] = []
        while not self._at(TokenKind.RBRACE):
            vt = self._expect(TokenKind.IDENTIFIER, what="enum variant name")
            value: Optional[int] = None
            fields: list[Type] = []
            if self._match(TokenKind.LPAREN) is not None:
                while not self._at(TokenKind.RPAREN):
                    fields.append(self._parse_type())
                    if self._match(TokenKind.COMMA) is None:
                        break
                self._expect(
                    TokenKind.RPAREN,
                    what="')' after enum variant payload",
                )
            elif self._match(TokenKind.ASSIGN) is not None:
                num = self._expect(TokenKind.INTEGER, what="integer variant value")
                value = cast(int, num.value)
            variants.append(
                Variant(vt.line, vt.column, str(vt.value), value, fields)
            )
            if self._match(TokenKind.COMMA) is None:
                break
        self._expect(TokenKind.RBRACE, what="'}' after enum variants")
        return EnumDecl(
            tok.line, tok.column, str(name.value), variants, pub, params
        )

    def _parse_trait(self, pub: bool) -> TraitDecl:
        tok = self._advance()  # trait
        name = self._expect(TokenKind.IDENTIFIER, what="trait name")
        params = self._parse_generic_params()
        # todo-156: supertraits — ``pub trait B<T>: A, Clone { ... }``.  Only a
        # colon here means the (single) generic-param colon has already been
        # consumed by ``_parse_generic_params``, so a bare ``:`` introduces the
        # inheritance list.
        supertraits: list[Type] = []
        if self._match(TokenKind.COLON) is not None:
            supertraits.append(self._parse_type())
            while self._match(TokenKind.COMMA) is not None:
                supertraits.append(self._parse_type())
        self._expect(TokenKind.LBRACE, what="'{' after trait name")
        methods: list[FnDecl] = []
        assoc_types: list[str] = []
        assoc_type_decls: list[AssocTypeDecl] = []
        while not self._at(TokenKind.RBRACE):
            if self._match(TokenKind.TYPE) is not None:
                at = self._expect(
                    TokenKind.IDENTIFIER, what="associated type name"
                )
                # todo-164: an optional bound (``type Item: Bound;``).
                bound: Optional[Type] = None
                if self._match(TokenKind.COLON) is not None:
                    bound = self._parse_type()
                self._expect(
                    TokenKind.SEMICOLON,
                    what="';' after associated type declaration",
                )
                assoc_types.append(str(at.value))
                assoc_type_decls.append(
                    AssocTypeDecl(at.line, at.column, str(at.value), bound)
                )
                continue
            method_pub = self._match(TokenKind.PUB) is not None
            methods.append(self._parse_fn(pub=method_pub, body_required=False))
        self._advance()  # }
        return TraitDecl(
            tok.line,
            tok.column,
            str(name.value),
            params,
            methods,
            pub,
            assoc_types,
            assoc_type_decls,
            supertraits,
        )

    def _parse_impl(self) -> ImplDecl:
        tok = self._advance()  # impl
        params = self._parse_generic_params()
        # todo-156: negative impl ``impl !Trait for Type``.  Consume the
        # leading ``!`` explicitly BEFORE _parse_type, which otherwise reads it
        # as the never-type ``!`` and would then die on ``for``.
        negative = self._match(TokenKind.NOT) is not None
        trait = self._parse_type()
        self._expect(TokenKind.FOR, what="'for' in impl declaration")
        struct = self._parse_type()
        self._expect(TokenKind.LBRACE, what="'{' after impl header")
        methods: list[FnDecl] = []
        assoc_types: list[AssocType] = []
        while not self._at(TokenKind.RBRACE):
            if self._match(TokenKind.TYPE) is not None:
                at = self._expect(
                    TokenKind.IDENTIFIER, what="associated type name"
                )
                self._expect(
                    TokenKind.ASSIGN,
                    what="'=' in associated type binding",
                )
                atype = self._parse_type()
                self._expect(
                    TokenKind.SEMICOLON,
                    what="';' after associated type binding",
                )
                assoc_types.append(
                    AssocType(at.line, at.column, str(at.value), atype)
                )
                continue
            method_pub = self._match(TokenKind.PUB) is not None
            method_static = self._match(TokenKind.STATIC) is not None
            methods.append(self._parse_fn(pub=method_pub, static=method_static))
        self._advance()  # }
        return ImplDecl(
            tok.line, tok.column, trait, struct, params, methods, assoc_types,
            negative,
        )

    def _parse_extra(self) -> ExtraDecl:
        tok = self._advance()  # extra
        params = self._parse_generic_params()
        struct = self._parse_type()
        moved_params = False
        if not params and struct.args:
            # bug-49: ``extra Cell<T> { ... }`` 允许 Grammar 省略前导
            # 泛型参数, 此时把类型实参列表挪为 extra 自己的泛型形参,
            # 归一化成 ``extra<T> Cell<T>`` (实参同时保留在类型上):
            # SA 的 defined/绑定表索引 owner params 由此获得不做不下传。
            moved: list[TypeParam] = []
            for arg in struct.args:
                if arg.args or arg.ref:
                    moved = []
                    break
                moved.append(
                    TypeParam(arg.line, arg.column, arg.name, None)
                )
            if moved:
                params = moved
                # todo-147: 裸名可能是具体类型 (``extra Cell<Int>`` 的
                # Int) —— parser 无符号表无法分辨, 先标记搬移事实, 由
                # SA 索引期解析: 具体类型则还原 params 为空 (特化)。
                moved_params = True
        self._expect(TokenKind.LBRACE, what="'{' after extra header")
        methods: list[FnDecl] = []
        # todo-122: associated constants, ``const NAME: Type = value;``
        consts: list[ConstDecl] = []
        while not self._at(TokenKind.RBRACE):
            method_pub = self._match(TokenKind.PUB) is not None
            if self._at(TokenKind.CONST):
                consts.append(self._parse_const(method_pub))
                continue
            method_static = self._match(TokenKind.STATIC) is not None
            methods.append(self._parse_fn(pub=method_pub, static=method_static))
        self._advance()  # }
        extra = ExtraDecl(tok.line, tok.column, struct, params, methods, consts)
        if moved_params:
            # 运行期标记 (不进 dataclass 字段/序列化), 供 SA 索引期甄别
            extra._params_moved_from_args = True
        return extra

    def _parse_group(self) -> GroupDecl:
        tok = self._advance()  # group
        name = self._expect(TokenKind.IDENTIFIER, what="group name")
        params: list[Param] = []
        struct: Optional[str] = None
        if self._at(TokenKind.LPAREN):
            params, _variadic = self._parse_params(allow_variadic=False)
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
        allow_variadic: bool = False,
    ) -> FnDecl:
        tok = self._advance()  # fn
        name = self._expect(TokenKind.IDENTIFIER, what="function name")
        type_params = self._parse_generic_params()
        params, variadic = self._parse_params(allow_variadic=allow_variadic)
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
        decl = FnDecl(
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
        decl.variadic = variadic
        if decl.body is not None:
            self._make_function_tail_return(decl.body)
        return decl

    def _parse_extern_block(self, *, pub: bool = False) -> ExternBlock:
        """Parse a C-FFI declaration block: ``extern "C" { fn ...; }``.

        Contained items are body-less function signatures
        (``fn name(params) -> Ret;``) and, since todo-56, extern static
        bindings (``static [mut] NAME: Type;``).  Each item may carry a
        ``#[link_name = "..."]`` attribute (todo-62) renaming its C
        symbol and ``#[cfg(...)]`` attributes (todo-86/93) dropping it
        on non-matching targets.  The block's ABI string is recorded on
        each ``FnDecl`` as ``extern_abi`` so the backend can emit raw-C
        declarations and calls.
        """
        tok = self._advance()  # extern
        if str(tok.value) != "extern":
            self._error("expected 'extern'", tok)
        abi_tok = self._expect(
            TokenKind.STRING, what='an ABI string, e.g. "C"'
        )
        abi = str(abi_tok.value)
        if not abi:
            self._error("the extern ABI string cannot be empty", abi_tok)
        self._expect(
            TokenKind.LBRACE, what="'{' to open the extern block"
        )
        fns: list[FnDecl] = []
        statics: list[ExternStatic] = []
        types: list[TypeDecl] = []
        while not self._at(TokenKind.RBRACE):
            if self._peek() is None:
                self._error("expected '}' to close the extern block", tok)
            fn_tok = self._peek()
            try:
                attrs = self._parse_attributes()
                # bug-40: extern 块成员允许自带 ``pub`` (与块级 pub 取或),
                # C 符号本身不受影响.
                item_pub = self._match(TokenKind.PUB) is not None or pub
                if self._at(TokenKind.STATIC):
                    static = self._parse_extern_static(pub=item_pub)
                    if self._apply_extern_item_attributes(static, attrs):
                        # todo-86/93: a false #[cfg] drops the binding.
                        statics.append(static)
                    continue
                # todo-132: ``extern "CWind"`` blocks also allow built-in
                # type declarations: ``type Name<Params>;``.
                if abi == "CWind" and self._at(TokenKind.TYPE):
                    td = self._parse_extern_cwind_type(pub=item_pub)
                    if self._apply_extern_item_attributes(td, attrs):
                        types.append(td)
                    continue
                # todo-132: ``extern "CWind"`` method declarations use the
                # Rust-like owner form ``fn Vector<T>::push_back(...)``;
                # plain module functions in such blocks keep the bare form.
                if abi == "CWind":
                    fn = self._parse_extern_cwind_fn(pub=item_pub)
                    if self._apply_extern_item_attributes(fn, attrs):
                        fns.append(fn)
                    continue
                # todo-87: extern 块内允许 ``...`` 变参 (仅此一处).
                fn = self._parse_fn(
                    pub=item_pub, body_required=False, allow_variadic=True
                )
                fn.extern_abi = abi
                if self._apply_extern_item_attributes(fn, attrs):
                    fns.append(fn)
            except ParseError as exc:
                self.errors.append(exc)
                if self._peek() is fn_tok:
                    self._advance()  # never spin on the same token
                # Skip to the next `fn` / `static` / `type` or the closing brace.
                while (
                    self._peek() is not None
                    and not self._at(TokenKind.FN)
                    and not self._at(TokenKind.STATIC)
                    and not self._at(TokenKind.TYPE)
                    and not self._at(TokenKind.RBRACE)
                ):
                    self._advance()
        self._advance()  # }
        return ExternBlock(tok.line, tok.column, abi, fns, statics, types, pub)

    def _parse_extern_static(self, *, pub: bool = False) -> ExternStatic:
        """Parse an extern static binding (todo-56): ``static [mut] N: T;``."""
        tok = self._advance()  # static
        mutable = self._match(TokenKind.MUT) is not None
        name = self._expect(
            TokenKind.IDENTIFIER, what="an extern static name"
        )
        self._expect(TokenKind.COLON, what="':' after the extern static name")
        ty = self._parse_type()
        self._expect(
            TokenKind.SEMICOLON,
            what="';' after the extern static declaration",
        )
        return ExternStatic(tok.line, tok.column, str(name.value), ty,
                            mutable, pub)

    def _parse_extern_cwind_type(self, *, pub: bool = False) -> TypeDecl:
        """Parse a built-in type declaration (todo-132):
        ``type Name[<Params>];`` inside ``extern "CWind"`` blocks.

        Unlike a ``typedef``/``type X = ...`` alias this forward-declares a
        compiler built-in type: it carries no right-hand side (``base=None``),
        only an optional generic-parameter list.
        """
        tok = self._advance()  # type
        name = self._expect(TokenKind.IDENTIFIER, what="type name")
        params = self._parse_generic_params()
        self._expect(
            TokenKind.SEMICOLON,
            what="';' after the built-in type declaration",
        )
        return TypeDecl(
            tok.line, tok.column, str(name.value), None, None, pub, params
        )

    def _parse_extern_cwind_fn(self, *, pub: bool = False) -> FnDecl:
        """Parse a function inside ``extern "CWind"`` (todo-132).

        Two forms are accepted:

        * **method** ``fn Vector<T>::push_back(&mut self, element: T) -> None;``
        * **plain module function** ``fn print(s: String);``

        The method form carries the owner type on ``FnDecl.cwind_owner``
        so the SA can register it as a built-in type method.
        """
        tok = self._peek()  # fn (do not advance: _parse_fn expects it too)
        # Look ahead to decide method vs plain function.
        # Method:  fn Type<...>::method(...)
        # Plain:   fn name(...)
        is_method = False
        depth = 0
        for offset in range(1, 32):
            ahead = self._peek(offset)
            if ahead is None:
                break
            kind = ahead.kind
            if kind == TokenKind.LT:
                depth += 1
            elif kind == TokenKind.GT:
                depth -= 1
                if depth < 0:
                    break
            elif depth == 0:
                if kind == TokenKind.PATH:
                    is_method = True
                    break
                if kind == TokenKind.LPAREN:
                    break
        if is_method:
            self._advance()  # fn
            # Parse owner type: Name<GenericParams>
            owner_tok = self._expect(
                TokenKind.IDENTIFIER, what="owner type name"
            )
            owner_name = str(owner_tok.value)
            owner_args: list[Type] = []
            if self._match(TokenKind.LT) is not None:
                while not self._at(TokenKind.GT):
                    param = self._expect(
                        TokenKind.IDENTIFIER,
                        what="generic parameter name",
                    )
                    owner_args.append(
                        Type(param.line, param.column, str(param.value))
                    )
                    if not self._at(TokenKind.GT):
                        self._expect(
                            TokenKind.COMMA,
                            what="',' or '>' in generic parameters",
                        )
                self._advance()  # >
            owner = Type(owner_tok.line, owner_tok.column, owner_name, owner_args)
            self._expect(
                TokenKind.PATH,
                what="'::' after owner type in extern CWind method declaration",
            )
            method_tok = self._expect(
                TokenKind.IDENTIFIER, what="method name"
            )
            params, variadic = self._parse_params(allow_variadic=False)
            return_type: Optional[Type] = None
            if self._match(TokenKind.ARROW) is not None:
                return_type = self._parse_type()
            self._expect(
                TokenKind.SEMICOLON,
                what="';' after extern CWind method declaration",
            )
            decl = FnDecl(
                tok.line,
                tok.column,
                str(method_tok.value),
                [],
                params,
                return_type,
                None,
                pub,
                False,
                None,
                extern_abi="CWind",
                cwind_owner=owner,
            )
            decl.variadic = variadic
            return decl
        # Plain module function.
        fn = self._parse_fn(pub=pub, body_required=False, allow_variadic=True)
        fn.extern_abi = "CWind"
        return fn

    def _make_function_tail_return(self, body: Block) -> None:
        """Lower a Rust-like function tail expression into ``return expr;``."""
        if not body.stmts:
            return
        last = body.stmts[-1]
        if (
            isinstance(last, ExprStmt)
            and getattr(last.expr, "_tail_expr", False)
        ):
            last.expr._tail_expr = False
            body.stmts[-1] = ReturnStmt(
                last.line,
                last.column,
                last.expr,
            )

    def _parse_params(
        self, allow_variadic: bool = False
    ) -> tuple[list[Param], bool]:
        """Parse a parameter list.

        Mutable receivers use Rust's postfix ordering ``&mut self``
        (todo-47); the retired ``mut &self`` form is rejected with a
        pointer to the new syntax.  Plain bindings keep ``mut x: T``.

        todo-87: a trailing ``...`` marker is only accepted when
        ``allow_variadic`` (extern blocks).  Returns the parameter list
        plus whether a variadic marker was present.
        """
        self._expect(TokenKind.LPAREN, what="'(' before parameter list")
        params: list[Param] = []
        variadic = False
        while not self._at(TokenKind.RPAREN):
            if self._at(TokenKind.ELLIPSIS):
                ell = self._advance()
                if not allow_variadic:
                    self._error(
                        "'...' variadic parameters are only allowed "
                        "inside extern blocks",
                        ell,
                    )
                if params:
                    # A trailing comma between the fixed parameters and
                    # '...' would break the C signature shape.
                    variadic = True
                    continue
                self._error(
                    "'...' requires at least one fixed parameter before it",
                    ell,
                )
            mutable = False
            if self._at(TokenKind.MUT):
                self._advance()
                mutable = True
                if self._at(TokenKind.AMP):
                    self._error(
                        "'mut &' is not allowed; write '&mut self'",
                        self._peek(),
                    )
            if self._at(TokenKind.AMP):
                amp = self._advance()
                if self._match(TokenKind.MUT) is not None:
                    mutable = True
                tok = self._expect(TokenKind.IDENTIFIER, what="parameter name")
                if str(tok.value) != "self":
                    self._error(
                        "only 'self' may omit a type after '&'", tok
                    )
                type_ = Type(amp.line, amp.column, "Self", ref=True)
                param = Param(amp.line, amp.column, str(tok.value), type_)
                param.mutable = mutable
                params.append(param)
            else:
                tok = self._expect(TokenKind.IDENTIFIER, what="parameter name")
                type_: Optional[Type] = None
                if self._match(TokenKind.COLON) is not None:
                    type_ = self._parse_type()
                elif str(tok.value) != "self":
                    self._error("parameter requires a type annotation", tok)
                param = Param(tok.line, tok.column, str(tok.value), type_)
                param.mutable = mutable
                params.append(param)
            if self._match(TokenKind.COMMA) is None:
                break
        self._expect(TokenKind.RPAREN, what="')' after parameter list")
        return params, variadic

    def _parse_generic_params(self) -> list[TypeParam]:
        """Parse an optional generic parameter list: ``<T, U: Bound = D>``.

        todo-164: a trailing ``= Type`` after the (optional) bound gives the
        parameter a default, usable when the caller omits trailing arguments.
        """
        if self._match(TokenKind.LT) is None:
            return []
        params: list[TypeParam] = []
        while True:
            tok = self._expect(TokenKind.IDENTIFIER, what="generic parameter name")
            bound: Optional[Type] = None
            if self._match(TokenKind.COLON) is not None:
                bound = self._parse_type()
            default: Optional[Type] = None
            if self._match(TokenKind.ASSIGN) is not None:
                default = self._parse_type()
            params.append(
                TypeParam(tok.line, tok.column, str(tok.value), bound, default)
            )
            if self._match(TokenKind.COMMA) is None:
                break
        self._expect_gt("'>' closing generic parameter list")
        return params

    # -- types -------------------------------------------------------------
    def _parse_type_args(self) -> tuple[list[Type], list[AssocType]]:
        """todo-164: generic type arguments plus associated-type bindings.

        Inside ``<...>`` a bare identifier followed by ``=`` is an
        associated-type binding (``Iterator<Item = Int32>``), not a
        positional argument; positional arguments must all precede
        bindings (Rust's grammar).
        """
        args: list[Type] = []
        bindings: list[AssocType] = []
        while True:
            arg = self._parse_type()
            if self._at(TokenKind.ASSIGN):
                if arg.args or arg.ref or arg.bindings or "::" in arg.name:
                    self._error(
                        "associated type bindings must be plain names",
                    )
                self._advance()  # =
                val = self._parse_type()
                bindings.append(
                    AssocType(arg.line, arg.column, arg.name, val)
                )
            else:
                if bindings:
                    self._error(
                        "generic arguments must precede associated type "
                        "bindings",
                    )
                args.append(arg)
            if self._match(TokenKind.COMMA) is None:
                break
        self._expect_gt("'>' closing generic type")
        return args, bindings
