"""Parser mixin: attributes, #[cfg] predicates and visibility."""

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


class ParserAttrs:
    # -- attributes ----------------------------------------------------------
    _LINK_ATTR_ARGS = ("name", "kind", "path", "relative")

    _LINK_RELATIVE_MODES = ("cwd", "source")

    def _cfg_context(self) -> CfgContext:
        """Compile-time configuration for ``#[cfg]`` evaluation (todo-86/93),
        lazily built from the explicit ``--target-os`` value or host
        auto-detection."""
        if self._cfg_ctx is None:
            self._cfg_ctx = CfgContext(
                self._cfg_target_os,
                self._cfg_target_arch,
                self._cfg_target_vendor,
                self._cfg_pointer_width,
            )
        return self._cfg_ctx

    def _parse_cfg_predicate(self) -> CfgPredicate:
        """Parse one ``#[cfg(...)]`` predicate (todo-86/93).

        Grammar::

            predicate := flag
                       | key '=' string          (e.g. target_os = "windows")
                       | ident '(' [predicate {',' predicate}] ')'

        Only ``all`` / ``any`` / ``not`` may appear in call position;
        ``not`` requires exactly one argument while empty ``all``/``any``
        follow Rust semantics (true/false).  Unknown flags, keys or values
        are reported here so a typo cannot silently change what compiles.
        """
        def fail(message: str, tok: Token) -> NoReturn:
            raise ParseError(
                f"#cfg: {message}",
                tok.line,
                tok.column,
                end_line=tok.end_line,
                end_column=tok.end_column,
            )

        tok = self._expect(TokenKind.IDENTIFIER, what="a cfg predicate")
        name = str(tok.value)
        if name in CFG_KEYS and self._at(TokenKind.LPAREN):
            fail(
                f"'{name}' expects = \"value\", not a predicate call",
                tok,
            )
        if self._match(TokenKind.ASSIGN) is not None:
            val_tok = self._expect(
                TokenKind.STRING,
                what='a quoted string value after \'=\' in the cfg predicate',
            )
            if name not in CFG_KEYS:
                fail(
                    f"unknown cfg key '{name}' "
                    f"(supported keys: {', '.join(CFG_KEYS)})",
                    tok,
                )
            value = str(val_tok.value)
            allowed = CFG_KEY_VALUES[name]
            if value not in allowed:
                fail(
                    f"invalid '{name}' value '{value}' "
                    f"(expected one of: {', '.join(allowed)})",
                    val_tok,
                )
            return CfgPredicate("kv", name=name, value=value)
        if self._match(TokenKind.LPAREN) is not None:
            if name not in CFG_COMBINATORS:
                fail(
                    f"'{name}' is not a valid cfg combinator "
                    f"(expected {', '.join(CFG_COMBINATORS)})",
                    tok,
                )
            args: list[CfgPredicate] = []
            while not self._at(TokenKind.RPAREN):
                args.append(self._parse_cfg_predicate())
                if self._match(TokenKind.COMMA) is None:
                    break
            self._expect(
                TokenKind.RPAREN,
                what="')' to close the cfg combinator",
            )
            if name == "not" and len(args) != 1:
                fail("the 'not' cfg predicate expects exactly one argument", tok)
            return CfgPredicate(name, args=tuple(args))
        if name not in CFG_FLAGS:
            fail(
                f"unknown cfg flag '{name}' "
                f"(expected a bare flag ({', '.join(CFG_FLAGS)}), "
                f"a combinator, or key = \"value\")",
                tok,
            )
        return CfgPredicate("flag", name=name)

    def _parse_attributes(self) -> list[tuple[str, object, int, int]]:
        """Collect leading ``#[...]`` attribute tokens.

        Returns ``(name, payload, line, column)`` tuples where ``payload``
        maps argument names to their string values; the paren-less
        shorthand ``#[name = "value"]`` (todo-62) stores its value under the
        empty key.  ``cfg`` (todo-86/93) carries a parsed :class:`CfgPredicate`
        tree instead of an argument dict.  Unknown attribute names or
        non-string values are reported as parse errors.
        """
        attrs: list[tuple[str, object, int, int]] = []
        while self._at(TokenKind.HASH):
            hash_tok = self._advance()  # #
            try:
                self._expect(
                    TokenKind.LBRACKET, what="'[' to open an attribute"
                )
                name_tok = self._expect(
                    TokenKind.IDENTIFIER, what="attribute name"
                )
                name = str(name_tok.value)
                if name == "cfg":
                    # todo-86/93: nested predicate grammar instead of the
                    # flat key = "value" argument list.
                    self._expect(
                        TokenKind.LPAREN,
                        what="'(' to open the 'cfg' predicate",
                    )
                    pred = self._parse_cfg_predicate()
                    self._expect(
                        TokenKind.RPAREN,
                        what="')' to close the 'cfg' predicate",
                    )
                    attrs.append((name, pred, hash_tok.line, hash_tok.column))
                else:
                    args: dict[str, str] = {}
                    if self._match(TokenKind.LPAREN) is not None:
                        while not self._at(TokenKind.RPAREN):
                            key_tok = self._expect(
                                TokenKind.IDENTIFIER,
                                what="an attribute argument name",
                            )
                            key = str(key_tok.value)
                            self._expect(
                                TokenKind.ASSIGN,
                                what="'=' after an attribute argument name",
                            )
                            val_tok = self._expect(
                                TokenKind.STRING,
                                what="a string literal attribute value",
                            )
                            if key in args:
                                raise ParseError(
                                    f"duplicate attribute argument '{key}' in "
                                    f"'{name}'",
                                    key_tok.line,
                                    key_tok.column,
                                )
                            args[key] = str(val_tok.value)
                            if self._match(TokenKind.COMMA) is None:
                                break
                        self._expect(
                            TokenKind.RPAREN,
                            what="')' to close the attribute arguments",
                        )
                    elif self._match(TokenKind.ASSIGN) is not None:
                        val_tok = self._expect(
                            TokenKind.STRING,
                            what="a string literal attribute value",
                        )
                        args[""] = str(val_tok.value)
                    attrs.append((name, args, hash_tok.line, hash_tok.column))
                self._expect(
                    TokenKind.RBRACKET, what="']' to close the attribute"
                )
            except ParseError as exc:
                self.errors.append(exc)
                # Skip to the end of this attribute so parsing can resume.
                while self._peek() is not None:
                    if self._match(TokenKind.RBRACKET) is not None:
                        break
                    self._advance()
        return attrs

    def _apply_attributes(self, item: Node, attrs: list) -> bool:
        """Validate collected attributes against the item they precede.

        Returns whether the item survives: every ``#[cfg]`` (todo-86/93)
        whose predicate evaluates to false drops the item from the AST, so
        mutually exclusive same-name definitions never collide downstream.
        Invalid usage still raises :class:`ParseError`.

        ``#[link(...)]`` is only valid on ``extern`` blocks (todo-49);
        ``#[link_name = "..."]`` (todo-62) only on declarations *inside*
        an extern block, which are handled by
        :meth:`_apply_extern_item_attributes`.
        """
        keep = True
        if not attrs:
            return keep
        for name, args, line, column in attrs:
            def fail(message: str) -> NoReturn:
                end = column + len(name)
                raise ParseError(
                    f"#{name}: {message}", line, column,
                    end_line=line, end_column=end,
                )

            if name == "cfg":
                assert isinstance(args, CfgPredicate)
                if keep and not evaluate_cfg(args, self._cfg_context()):
                    keep = False
                continue
            if name == "link_name":
                fail(
                    "the 'link_name' attribute can only be applied to "
                    "declarations inside an extern block"
                )
            if name != "link":
                fail(
                    "unsupported attribute (only 'cfg' / 'link' / "
                    "'link_name' are supported)"
                )
            if not isinstance(item, ExternBlock):
                fail(
                    "the 'link' attribute can only be applied to an "
                    "extern block"
                )
            if item.link_name is not None or item.link_path is not None:
                fail("duplicate 'link' attribute on one extern block")
            unknown = [k for k in args if k not in self._LINK_ATTR_ARGS]
            if unknown:
                fail(
                    f"unknown 'link' argument '{unknown[0]}' "
                    "(expected name / kind / path / relative)"
                )
            kind = args.get("kind")
            if kind is not None and kind not in ("static", "dylib"):
                fail(
                    f"invalid link kind '{kind}' "
                    "(expected 'static' or 'dylib')"
                )
            relative = args.get("relative")
            if relative is not None:
                # todo-63: 锚定 link_path 的主路径; 省略时默认工作目录
                if relative not in self._LINK_RELATIVE_MODES:
                    fail(
                        f"invalid link relative '{relative}' "
                        "(expected 'cwd' or 'source')"
                    )
                if args.get("path") is None:
                    fail("the 'relative' argument requires 'path'")
                # todo-64: 绝对路径没有锚点可言, 同时给出属于自相矛盾
                if self._path_is_absolute(args["path"]):
                    fail(
                        f"'path' '{args['path']}' is absolute; "
                        "the 'relative' argument applies only to "
                        "relative paths"
                    )
            item.link_name = args.get("name")
            item.link_kind = kind
            item.link_path = args.get("path")
            item.link_relative = relative
        return keep

    def _apply_extern_item_attributes(self, item: Node, attrs: list) -> bool:
        """Validate attributes attached to a declaration inside an extern
        block.  ``#[link_name = "..."]`` (todo-62) renames the linked C
        symbol while the CWind-side name stays as declared; ``#[cfg]``
        (todo-86/93) may drop the declaration entirely.  Returns whether
        the declaration survives."""
        keep = True
        if not attrs:
            return keep
        for name, args, line, column in attrs:
            def fail(message: str) -> NoReturn:
                end = column + len(name)
                raise ParseError(
                    f"#{name}: {message}", line, column,
                    end_line=line, end_column=end,
                )

            if name == "cfg":
                assert isinstance(args, CfgPredicate)
                if keep and not evaluate_cfg(args, self._cfg_context()):
                    keep = False
                continue
            if name != "link_name":
                fail(
                    "unsupported attribute inside an extern block "
                    "(only 'cfg' / 'link_name' are supported)"
                )
            if not isinstance(item, (FnDecl, ExternStatic)):
                fail(
                    "the 'link_name' attribute can only be applied to "
                    "a fn or static declaration"
                )
            if item.link_name is not None:
                fail("duplicate 'link_name' attribute on one declaration")
            value = args.get("")
            if not value:
                fail('expects a symbol name: #[link_name = "symbol"]')
            item.link_name = value
        return keep

    def _filter_use_attributes(self, attrs: list) -> tuple[bool, list[ParseError]]:
        """Validate attributes preceding a ``use`` declaration (todo-86/93).

        Only ``#[cfg]`` is meaningful on an import.  Returns whether the
        import survives and the list of unsupported-attribute errors (the
        caller reports them once the statement itself has been dealt with).
        """
        keep = True
        unsupported: list[ParseError] = []
        for name, payload, line, column in attrs:
            if name != "cfg":
                unsupported.append(ParseError(
                    f"#{name}: unsupported attribute on a use declaration "
                    "(only 'cfg' is supported)",
                    line,
                    column,
                    end_line=line,
                    end_column=column + len(name),
                ))
                continue
            assert isinstance(payload, CfgPredicate)
            if keep and not evaluate_cfg(payload, self._cfg_context()):
                keep = False
        return keep, unsupported

    @staticmethod
    def _path_is_absolute(path: str) -> bool:
        """Mirror of the backend's ``cw_path_is_absolute`` (todo-64):
        Windows drive-letter or rooted/UNC prefixes, POSIX root."""
        if not path:
            return False
        first = path[0]
        if first in ("/", "\\"):
            return True
        return (
            len(path) > 1
            and first.isascii()
            and first.isalpha()
            and path[1] == ":"
        )

    def _parse_visibility(
        self, pub: bool
    ) -> tuple[Optional[str], Optional[list[str]]]:
        """todo-107/119: parse the restricted-visibility variants of ``pub``.

        Grammar (``in`` is a hard keyword; the qualifier itself is a path of
        identifiers, not a special token)::

            vis := 'pub' [ '(' qualifier ')' ]
            qualifier := 'self' | 'super' | 'crate' | 'std' | 'in' path
            path := segment [ '::' segment ]*     (segment := IDENTIFIER
                    | 'super' | 'crate' | 'self')

        ``pub(super::super::x)`` is the segment form of ``pub(in super::x)``,
        both normalize to ``("in", [segments])``; the four named qualifiers
        are single-word shortcuts with no path.  Returns
        ``(visibility, vis_path)`` — ``("self"| "super"| "crate"| "std"| "in",
        None-or-[segments])``; plain ``pub`` returns ``(None, None)``.  Must
        be called after a ``pub`` token was consumed (or with ``pub=False``
        to return the no-op pair).
        """
        if not pub or not self._at(TokenKind.LPAREN):
            return (None, None)
        try:
            self._advance()  # (
            if self._match(TokenKind.IN) is not None:
                vis, path = "in", self._parse_vis_path()
            else:
                word = self._expect(
                    TokenKind.IDENTIFIER,
                    what="visibility qualifier "
                    "('self'/'super'/'crate'/'std'/'in path')",
                )
                name = str(word.value)
                if name == "super" and self._match(TokenKind.PATH) is not None:
                    # ``pub(super::super::x)``: segmented restricted form.
                    rest = self._parse_vis_path(allow_super=True)
                    vis, path = "in", ["super", *rest]
                elif name not in ("self", "super", "crate", "std"):
                    raise ParseError(
                        f"unknown visibility qualifier 'pub({name})' "
                        "(supported: self/super/crate/std/"
                        "in <path>[:...])",
                        word.line,
                        word.column,
                    )
                else:
                    vis, path = name, None
            self._expect(
                TokenKind.RPAREN, what="')' after visibility qualifier"
            )
        except ParseError:
            raise
        return (vis, path)

    def _parse_vis_path(self, *, allow_super: bool = False) -> list[str]:
        """Parse the path of ``pub(in ...)`` / ``pub(super::...)``.

        Segments are identifiers plus the ``super`` keyword (its only
        sanctioned non-head position: Rust's ``pub(in super::super::x)``).
        Returns the segment list; the caller folds it into ``vis_path``.
        """
        segments: list[str] = []
        while True:
            tok = self._peek()
            if (
                tok is not None
                and tok.kind == TokenKind.IDENTIFIER
                and str(tok.value) == "super"
            ):
                self._advance()
                segments.append("super")
            elif tok is not None and tok.kind == TokenKind.IDENTIFIER:
                self._advance()
                segments.append(str(tok.value))
            elif tok is not None and tok.kind == TokenKind.SUPER:
                self._advance()
                segments.append("super")
            else:
                self._error(
                    "expected a path segment in visibility path", tok
                )
            nxt = self._peek()
            if nxt is not None and nxt.kind == TokenKind.PATH:
                self._advance()
                continue
            return segments
