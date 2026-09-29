"""``#[link(...)]`` on an extern block and ``#[link_name = "..."]`` on a
declaration inside one.

The two are registered as separate processors because they attach to
different syntactic sites; a name used at the wrong site gets the other
processor's diagnostic (the "redirect" hint), which is what the previous
hand-written chain did.
"""

from __future__ import annotations

from typing import Any

from ..ast_components.ast import ExternBlock, ExternStatic, FnDecl
from .model import (
    EXTERN_MEMBER,
    ITEM,
    Attr,
    AttributeError,
    DuplicateAttribute,
    InvalidArgument,
    MissingArgument,
    UnexpectedArgument,
    WrongItemKind,
)
from .registry import AttrProc, ProcCtx, register


_LINK_ARGS = ("name", "kind", "path", "relative")
_LINK_KINDS = ("static", "dylib")
_LINK_RELATIVE_MODES = ("cwd", "source")


def path_is_absolute(path: str) -> bool:
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


def _reject_extras(attr: Attr) -> set[str]:
    """Argument names outside the known set (reported by the caller)."""
    return {a.name for a in attr.args if a.name not in _LINK_ARGS}


def _apply_link(ctx: ProcCtx, item: Any, attr: Attr) -> None:
    if not isinstance(item, ExternBlock):
        raise WrongItemKind(
            "the 'link' attribute can only be applied to an extern block",
            attr.line, attr.column,
        )
    if item.link_name is not None or item.link_path is not None:
        raise DuplicateAttribute(
            "duplicate 'link' attribute on one extern block",
            attr.line, attr.column,
        )
    extra = sorted(_reject_extras(attr))
    if extra:
        raise UnexpectedArgument(
            f"unknown 'link' argument '{extra[0]}' "
            "(expected name / kind / path / relative)",
            attr.line, attr.column,
        )
    pairs = attr.pairs()
    kind = pairs["kind"].value if "kind" in pairs else None
    if kind is not None and kind not in _LINK_KINDS:
        raise InvalidArgument(
            f"invalid link kind '{kind}' "
            "(expected 'static' or 'dylib')",
            attr.line, attr.column,
        )
    relative = pairs["relative"].value if "relative" in pairs else None
    if relative is not None:
        # todo-63: the anchor for a relative link_path; the working
        # directory is the default.
        if relative not in _LINK_RELATIVE_MODES:
            raise InvalidArgument(
                f"invalid link relative '{relative}' "
                "(expected 'cwd' or 'source')",
                attr.line, attr.column,
            )
        if "path" not in pairs:
            raise MissingArgument(
                "the 'relative' argument requires 'path'",
                attr.line, attr.column,
            )
        # todo-64: an absolute path has no anchor, and saying so beats
        # silently ignoring one of the two arguments.
        if ctx.path_is_absolute(pairs["path"].value or ""):
            raise InvalidArgument(
                f"'path' '{pairs['path'].value}' is absolute; "
                "the 'relative' argument applies only to relative paths",
                attr.line, attr.column,
            )
    item.link_name = pairs["name"].value if "name" in pairs else None
    item.link_kind = kind
    item.link_path = pairs["path"].value if "path" in pairs else None
    item.link_relative = relative


def _apply_link_name(ctx: ProcCtx, item: Any, attr: Attr) -> None:
    if not isinstance(item, (FnDecl, ExternStatic)):
        raise WrongItemKind(
            "the 'link_name' attribute can only be applied to "
            "a fn or static declaration",
            attr.line, attr.column,
        )
    if item.link_name is not None:
        raise DuplicateAttribute(
            "duplicate 'link_name' attribute on one declaration",
            attr.line, attr.column,
        )
    literals = attr.positional()
    value = literals[0].value if literals else None
    if not value:
        raise MissingArgument(
            'expects a symbol name: #[link_name = "symbol"]',
            attr.line, attr.column,
        )
    item.link_name = value


register(AttrProc(
    name="link",
    positions=frozenset({ITEM}),
    apply=_apply_link,
    redirect="declare it on an extern block",
    doc="the linked library, kind and path for an extern block",
))

register(AttrProc(
    name="link_name",
    positions=frozenset({EXTERN_MEMBER}),
    apply=_apply_link_name,
    redirect="the 'link_name' attribute can only be applied to "
             "declarations inside an extern block",
    doc="rename one declaration's linked C symbol",
))
