"""``#[export]`` / ``#[export(name = "...")]`` -- reverse FFI.

Marks a top-level free function as part of the shared library's C ABI
(todo-55).  Methods and generic functions are rejected: neither has a
single C signature.
"""

from __future__ import annotations

from typing import Any

from ..ast_components.ast import FnDecl
from .model import (
    ITEM,
    Attr,
    DuplicateAttribute,
    InvalidArgument,
    UnexpectedArgument,
    WrongItemKind,
)
from .registry import AttrProc, ProcCtx, register


def _apply_export(ctx: ProcCtx, item: Any, attr: Attr) -> None:
    if not isinstance(item, FnDecl):
        raise WrongItemKind(
            "the 'export' attribute only applies to a top-level "
            "free function",
            attr.line, attr.column,
        )
    if item.extern_abi is not None:
        raise WrongItemKind(
            "the 'export' attribute cannot be applied to an "
            "extern declaration (declare the body in CWind)",
            attr.line, attr.column,
        )
    if item.export_name is not None:
        raise DuplicateAttribute(
            "duplicate 'export' attribute on one function",
            attr.line, attr.column,
        )
    extra = sorted(a.name for a in attr.args if a.name not in ("name",))
    if extra:
        raise UnexpectedArgument(
            f"unsupported 'export' argument '{extra[0]}' "
            '(expected #[export] or #[export(name = "symbol")])',
            attr.line, attr.column,
        )
    pairs = attr.pairs()
    value = pairs["name"].value if "name" in pairs else None
    if value is not None and not value:
        raise InvalidArgument(
            'the export name cannot be empty: '
            '#[export(name = "symbol")]',
            attr.line, attr.column,
        )
    if item.type_params:
        raise InvalidArgument(
            f"generic function '{item.name}' cannot be exported "
            "(a generic function has no single C ABI signature)",
            attr.line, attr.column,
        )
    item.export_name = value if value is not None else item.name


register(AttrProc(
    name="export",
    positions=frozenset({ITEM}),
    apply=_apply_export,
    redirect="the 'export' attribute applies to top-level free "
             "functions only",
    doc="export a top-level function in the shared library's C ABI",
))
