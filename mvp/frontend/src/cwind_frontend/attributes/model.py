"""The collected attribute form -- pure syntax, no semantics.

The collector (:mod:`cwind_frontend.parser.attrs`) knows nothing about
which attributes exist or what their arguments mean.  It only cuts
``#[...]`` into a name plus a generic argument tree::

    #[repr(C, 8)]              -> Attr("repr", [flag(C), literal("8")])
    #[cfg(target_os = "windows")]
                                -> Attr("cfg", [pair("target_os", "windows")])
    #[inline(always)]          -> Attr("inline", [call("always", ())])

:func:`cwind_frontend.parser.attrs.parse_attribute_payload` is the only
producer; processors are the only consumers.  Nothing in this module
names a built-in attribute, so adding one cannot ripple here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# Argument shapes the collector recognises.  A processor picks the ones it
# understands and rejects the rest; the collector accepts all of them
# everywhere, which is what lets an attribute with a non-string payload
# (``#[repr(align(8))]``, ``#[derive(A, B)]``) be introduced without
# touching the parser.
FLAG = "flag"        # bare word:            C, align, cold, derive
LITERAL = "literal"  # bare literal:         "windows", 8, -8
PAIR = "pair"        # name = value:         target_os = "windows"
CALL = "call"        # name( args ):         all(a, b), align(8)


@dataclass(frozen=True)
class AttrArg:
    """One comma-separated element of an attribute payload.

    ``line``/``column`` are the element's *head* (the word, the call name);
    ``value_pos`` is the right-hand side of a ``key = value`` pair, so a
    processor can blame the key and the value separately.
    """

    kind: str
    name: str
    value: Optional[str] = None
    args: tuple["AttrArg", ...] = ()
    line: int = 0
    column: int = 0
    value_pos: tuple[int, int] = (0, 0)

    def flag(self) -> bool:
        return self.kind == FLAG

    def is_named(self, name: str) -> bool:
        return self.name == name

    def where(self) -> tuple[int, int]:
        """Where to blame this argument (its value when it has one)."""
        return self.value_pos if self.value_pos != (0, 0) else (
            self.line, self.column
        )


@dataclass(frozen=True)
class Attr:
    """One collected ``#[name(args...)]``."""

    name: str
    args: tuple[AttrArg, ...] = ()
    line: int = 0
    column: int = 0

    def pairs(self) -> dict[str, AttrArg]:
        return {a.name: a for a in self.args if a.kind == PAIR}

    def positional(self) -> tuple[AttrArg, ...]:
        return tuple(a for a in self.args if a.kind == LITERAL)

    def flag_names(self) -> tuple[str, ...]:
        return tuple(a.name for a in self.args if a.kind == FLAG)

    def call(self, name: str) -> Optional[AttrArg]:
        return next(
            (a for a in self.args if a.kind == CALL and a.name == name),
            None,
        )

    def has_key(self, *names: str) -> bool:
        keys = {a.name for a in self.args if a.kind == PAIR}
        return any(n in keys for n in names)


# Positions an attribute may be attached to.  The parser resolves the
# syntactic site; a processor declares which sites it accepts.
ITEM = "item"                      # top-level or inline-mod declaration
EXTERN_MEMBER = "extern_member"    # fn / static / type inside extern "C"
USE = "use"                        # use / export crate declaration
METHOD = "method"                  # fn inside impl / extra / trait

POSITIONS: frozenset[str] = frozenset({ITEM, EXTERN_MEMBER, USE, METHOD})


class AttributeError(Exception):
    """Base class for every attribute rejection.

    A processor raises one of the subclasses below; the driver turns it
    into a diagnostic.  Splitting the kinds (instead of a single opaque
    message) keeps the taxonomy inspectable -- tooling can tell "nothing
    claims this name" from "the right processor, wrong site" from "the
    processor rejected an argument" without matching on text.

    Every instance knows the attribute it is about and the position to
    point at: ``#name`` for whole-attribute problems, the offending
    argument for argument-level ones.
    """

    #: short, stable label for tooling; the human text is ``message``
    kind = "attribute"

    def __init__(
        self, message: str, line: int = 0, column: int = 0,
        *, attr: "Optional[Attr]" = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.line = line
        self.column = column
        self.attr = attr

    def render(self) -> str:
        """The text after ``#name: `` (the driver prefixes the name)."""
        return self.message


class UnrecognizedAttribute(AttributeError):
    """No registered processor claims this spelling."""

    kind = "unrecognized"


class UnsupportedAttribute(AttributeError):
    """A processor claims the name, but not for this syntactic site."""

    kind = "unsupported"

    def __init__(
        self, message: str, line: int = 0, column: int = 0, *,
        position: str = "", attr: "Optional[Attr]" = None,
    ) -> None:
        super().__init__(message, line, column, attr=attr)
        self.position = position

    def render(self) -> str:
        return self.message


class DuplicateAttribute(AttributeError):
    """The same attribute appears twice on one declaration."""

    kind = "duplicate"


class UnexpectedArgument(AttributeError):
    """An argument the processor does not accept (unknown name or shape)."""

    kind = "unexpected-argument"


class MissingArgument(AttributeError):
    """A required argument is absent."""

    kind = "missing-argument"


class InvalidArgument(AttributeError):
    """An argument is present but its value is not acceptable."""

    kind = "invalid-argument"


class WrongItemKind(AttributeError):
    """The attribute is spelled correctly but sits on the wrong kind of
    declaration (a function where a static is required, and so on)."""

    kind = "wrong-item-kind"
