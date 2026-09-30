"""Attribute processors.

The parser collects ``#[...]`` into :class:`~.model.Attr` without
knowing any attribute names, then hands each one to the processor that
claims it (:mod:`.registry`).  An attribute no processor claims is an
error.

Built-in processors live beside this module as ``cfg`` / ``link`` /
``export`` / ``inline`` / ``opt``; importing the package registers them.
``macros/`` depends on this package too -- for the list of names the
parser owns -- so the two never carry separate copies.
"""

from __future__ import annotations

from .model import (
    CALL,
    EXTERN_MEMBER,
    FLAG,
    ITEM,
    LITERAL,
    METHOD,
    PAIR,
    POSITIONS,
    USE,
    Attr,
    AttrArg,
    AttributeError,
    DuplicateAttribute,
    InvalidArgument,
    MissingArgument,
    UnexpectedArgument,
    UnrecognizedAttribute,
    UnsupportedAttribute,
    WrongItemKind,
)
from .registry import (
    AttrProc,
    ProcCtx,
    describe,
    known_for,
    lookup,
    names,
    parser_owned_names,
    register,
)

# Registering on import keeps the built-in list in one place: a new
# built-in is a new module plus one line here.
from . import cfg as _cfg  # noqa: F401
from . import export as _export  # noqa: F401
from . import inline as _inline  # noqa: F401
from . import link as _link  # noqa: F401
from . import opt as _opt  # noqa: F401

__all__ = [
    "CALL",
    "EXTERN_MEMBER",
    "FLAG",
    "ITEM",
    "LITERAL",
    "METHOD",
    "PAIR",
    "POSITIONS",
    "USE",
    "Attr",
    "AttrArg",
    "AttrProc",
    "AttributeError",
    "DuplicateAttribute",
    "InvalidArgument",
    "MissingArgument",
    "ProcCtx",
    "UnexpectedArgument",
    "UnrecognizedAttribute",
    "UnsupportedAttribute",
    "WrongItemKind",
    "describe",
    "known_for",
    "lookup",
    "names",
    "parser_owned_names",
    "register",
]
