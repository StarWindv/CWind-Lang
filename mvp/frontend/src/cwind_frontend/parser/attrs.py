"""Parser mixin: attribute collection and dispatch, #[cfg] and visibility.

Two strictly separated phases:

* **collect** (:meth:`ParserAttrs._parse_attributes`) -- purely syntactic.
  It cuts ``#[name(args...)]`` into a name plus a generic argument tree
  and reports only what is malformed *as syntax*.  It knows no attribute
  names and no argument grammar, so an attribute with a non-string
  payload needs no parser change.
* **dispatch** (:func:`dispatch_attributes`) -- hands each collected
  attribute to the processor that claims it
  (:mod:`cwind_frontend.attributes`).  An attribute no processor claims,
  or one used at a site its processor does not accept, is an error.

The public entry points below are thin wrappers over the dispatcher,
one per syntactic site, preserving each site's error protocol (raise vs.
collect).
"""

from __future__ import annotations

from typing import NoReturn, Optional

from .defs import (
    ParseError,
)
from ..ast_components.ast import (
    ExternBlock,
    ExternStatic,
    FnDecl,
    Node,
)
from ..ast_components.token import Token, TokenKind
from ..attributes import (
    CALL,
    EXTERN_MEMBER,
    FLAG,
    ITEM,
    LITERAL,
    METHOD,
    PAIR,
    USE,
    Attr,
    AttrArg,
    AttributeError,
    ProcCtx,
    UnrecognizedAttribute,
    UnsupportedAttribute,
    describe,
    lookup,
)
from ..attributes.link import path_is_absolute as _path_is_absolute
from ..cfg import CfgContext


class ParserAttrs:
    # -- attributes ----------------------------------------------------------
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

    def _attr_ctx(self) -> ProcCtx:
        return ProcCtx(
            cfg_context=self._cfg_context,
            path_is_absolute=_path_is_absolute,
        )

    # -- phase 1: collect (syntax only) --------------------------------------
    def _parse_attributes(self) -> list[Attr]:
        """Collect leading ``#[...]`` attributes.

        Returns one :class:`Attr` per attribute.  Every argument is cut
        into ``flag`` / ``literal`` / ``pair`` / ``call`` without knowing
        which is which is *intended*; a payload that cannot be cut at all
        (missing name, unbalanced brackets, junk after a value) is a
        parse error and parsing resumes after the closing ``]``.
        """
        attrs: list[Attr] = []
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
                args: tuple[AttrArg, ...] = ()
                if self._at(TokenKind.LPAREN):
                    args = tuple(self._collect_attr_args(name, hash_tok))
                elif self._at(TokenKind.ASSIGN):
                    # the paren-less shorthand: one positional literal
                    self._advance()
                    args = (self._collect_attr_literal(name),)
                attrs.append(Attr(name, args, hash_tok.line, hash_tok.column))
                self._expect(
                    TokenKind.RBRACKET, what="']' to close an attribute"
                )
            except ParseError as exc:
                self.errors.append(exc)
                # Skip to the end of this attribute so parsing can resume.
                while self._peek() is not None:
                    if self._match(TokenKind.RBRACKET) is not None:
                        break
                    self._advance()
        return attrs

    def _collect_attr_args(
        self, owner: str, hash_tok: Token
    ) -> list[AttrArg]:
        """``(arg, arg, ...)`` inside one attribute's parentheses.

        Repeated names are *not* rejected here: whether ``k = a, k = b`` is
        an error is the attribute's own policy (``#[cfg]`` reads it as OR
        and uses it throughout std), so the decision belongs to the
        processor, not to the collector.
        """
        self._expect(
            TokenKind.LPAREN, what="'(' to open the attribute arguments"
        )
        args: list[AttrArg] = []
        while not self._at(TokenKind.RPAREN):
            args.append(self._collect_attr_arg(owner, hash_tok))
            if self._match(TokenKind.COMMA) is None:
                break
        self._expect(
            TokenKind.RPAREN, what="')' to close the attribute arguments"
        )
        return args

    def _collect_attr_arg(self, owner: str, hash_tok: Token) -> AttrArg:
        """One comma-separated element: ``C`` / ``8`` / ``k = v`` / ``f(..)``."""
        tok = self._peek()
        if tok is not None and tok.kind == TokenKind.IDENTIFIER:
            name_tok = self._advance()
            name = str(name_tok.value)
            if self._match(TokenKind.ASSIGN) is not None:
                value, value_pos = self._collect_attr_value(owner, name_tok)
                return AttrArg(
                    PAIR, name, value, (), value_pos=(
                        value_pos[0], value_pos[1],
                    ),
                    line=name_tok.line, column=name_tok.column,
                )
            if self._at(TokenKind.LPAREN):
                return AttrArg(
                    CALL, name, None,
                    tuple(self._collect_attr_args(owner, name_tok)),
                    name_tok.line, name_tok.column,
                )
            return AttrArg(
                FLAG, name, None, (), name_tok.line, name_tok.column
            )
        return self._collect_attr_literal(owner)

    def _collect_attr_value(
        self, owner: str, name_tok: Token
    ) -> tuple[str, tuple[int, int]]:
        """The right-hand side of ``key = ...`` -- a literal or a word.

        Returns the text and its own position, so a processor can report a
        bad *value* against the value rather than the key.
        """
        tok = self._peek()
        if tok is None or tok.kind not in (
            TokenKind.STRING, TokenKind.INTEGER, TokenKind.FLOAT,
            TokenKind.IDENTIFIER,
        ):
            self._error(
                f"expected a value after '{name_tok.value}' in "
                f"'{owner}' (a string, a number or a word)",
                tok,
            )
        self._advance()
        return str(tok.value), (tok.line, tok.column)

    def _collect_attr_literal(self, owner: str) -> AttrArg:
        """A bare literal element (``8``, ``"windows"``, ``-8``)."""
        cur = self._peek()
        if cur is not None and cur.kind == TokenKind.MINUS:
            self._advance()
            nxt = self._peek()
            if nxt is None or nxt.kind not in (
                TokenKind.INTEGER, TokenKind.FLOAT
            ):
                self._error(
                    f"expected a number after '-' in '{owner}'", nxt
                )
            self._advance()
            return AttrArg(
                LITERAL, "", f"-{nxt.value}", (),
                cur.line, cur.column,
            )
        if cur is not None and cur.kind in (
            TokenKind.STRING, TokenKind.INTEGER, TokenKind.FLOAT,
        ):
            self._advance()
            return AttrArg(
                LITERAL, "", str(cur.value), (), cur.line, cur.column
            )
        self._error(
            f"expected an attribute argument in '{owner}' "
            "(a word, a number, a string, 'key = value' or 'name(...)')",
            cur,
        )

    # -- phase 2: dispatch ---------------------------------------------------
    def _apply_attributes(self, item: Node, attrs: list) -> bool:
        """Validate top-level item attributes.

        Returns whether the item survives: every ``#[cfg]`` (todo-86/93)
        whose predicate evaluates to false drops the item from the AST, so
        mutually exclusive same-name definitions never collide downstream.
        Invalid usage raises :class:`ParseError`.
        """
        return dispatch_attributes(
            self._attr_ctx(), item, attrs, ITEM, report=self._raise_attr
        )

    def _apply_extern_item_attributes(
        self, item: Node, attrs: list
    ) -> bool:
        """Validate attributes on a declaration inside an extern block."""
        return dispatch_attributes(
            self._attr_ctx(), item, attrs, EXTERN_MEMBER,
            report=self._raise_attr,
        )

    def _filter_use_attributes(
        self, attrs: list
    ) -> tuple[bool, list[ParseError]]:
        """Validate attributes on a ``use`` declaration (todo-86/93).

        Reports are collected rather than raised: the caller decides what
        to do with the import itself, and a gated-away import must still
        be able to drop it.
        """
        collected: list[ParseError] = []
        keep = dispatch_attributes(
            self._attr_ctx(), None, attrs, USE,
            report=collected.append,
        )
        return keep, collected

    def _reject_method_attributes(self) -> None:
        """Methods do not accept attributes (todo-55).

        ``#[export]`` is only valid on a top-level free function; parsing
        the attributes here (instead of letting the token loop trip over
        ``#``) keeps the diagnostic specific about the offending name.
        """
        errors: list[ParseError] = []
        dispatch_attributes(
            self._attr_ctx(), None, self._parse_attributes(), METHOD,
            report=errors.append,
        )
        self.errors.extend(errors)

    def _raise_attr(self, err: ParseError) -> NoReturn:
        raise err

    _path_is_absolute = staticmethod(_path_is_absolute)

    # -- visibility ----------------------------------------------------------
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
                    # ``_match`` consumes the ``::`` so the path walker below
                    # starts at the first segment.
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


def dispatch_attributes(
    ctx: ProcCtx,
    item: Optional[Node],
    attrs: list,
    position: str,
    *,
    report,
) -> bool:
    """Run every collected attribute past its processor.

    *An attribute no processor claims is an error* -- that is the single
    exit for "no processor wanted this".  Each rejection is a typed
    :class:`~cwind_frontend.attributes.AttributeError`, converted to a
    :class:`ParseError` here; ``report`` only decides whether the driver
    raises it or collects it (a ``use`` declaration must be able to drop a
    gated-away import, so it collects).

    Returns whether the item survives; a processor with a ``keep``
    predicate (``cfg``) can drop it.
    """
    keep = True
    for attr in attrs:
        error = _run_one(ctx, item, attr, position)
        if error is not None:
            report(_as_parse_error(error, attr))
            continue
        proc = lookup(attr.name)
        assert proc is not None
        if proc.keep is not None and keep and not proc.keep(ctx, attr):
            keep = False
    return keep


def _run_one(
    ctx: ProcCtx, item: Optional[Node], attr: Attr, position: str
) -> Optional[AttributeError]:
    """Claim, site-check and apply one attribute; None when it is fine."""
    proc = lookup(attr.name)
    if proc is None:
        return UnrecognizedAttribute(
            f"unrecognized attribute '{attr.name}' "
            f"({describe(position)})",
            attr.line, attr.column, attr=attr,
        )
    if position not in proc.positions:
        return UnsupportedAttribute(
            _wrong_site_text(position, proc.redirect),
            attr.line, attr.column, position=position, attr=attr,
        )
    try:
        proc.apply(ctx, item, attr)
    except AttributeError as exc:
        if exc.attr is None:
            exc.attr = attr
        return exc
    return None


def _as_parse_error(err: AttributeError, attr: Attr) -> ParseError:
    """Render a typed attribute error as a located ``ParseError``."""
    return ParseError(
        f"#{attr.name}: {err.render()}",
        err.line, err.column,
        end_line=err.line,
        end_column=err.column + len(attr.name),
        category=f"attribute: {err.kind}",
    )


def _wrong_site_text(position: str, redirect: str) -> str:
    """The wording a rejected site uses (todo-55 / todo-86 provenance).

    A processor's ``redirect`` is the most specific thing we can say, so
    it wins where the site has no wording of its own; the per-site forms
    below exist because their tests (and users) rely on them.
    """
    if position == USE:
        return (
            f"unsupported attribute on a use declaration "
            f"({describe(position)})"
        )
    if position == METHOD:
        return (
            "attributes are not supported on methods "
            f"({redirect or describe(position)})"
        )
    if position == EXTERN_MEMBER:
        return (
            f"unsupported attribute inside an extern block "
            f"({describe(position)})"
        )
    if redirect:
        return redirect
    return f"unsupported attribute ({describe(position)})"
