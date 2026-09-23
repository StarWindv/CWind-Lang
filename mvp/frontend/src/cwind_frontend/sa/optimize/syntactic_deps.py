"""Pre-SA DCE: syntactic dependency helpers (refs / provides / conservative).

Shared vocabulary for :func:`syntactic.prune_unreachable_syntactic`.
Purely structural: no ``_typed_id``, no annotations.
"""

from __future__ import annotations

import re
from dataclasses import fields as _dc_fields
from typing import Any, Optional

from ...ast_components.ast import (
    BindPattern,
    ConstDecl,
    EnumDecl,
    ExternBlock,
    ExternStatic,
    ExtraDecl,
    Field,
    FnDecl,
    ImplDecl,
    LetStmt,
    ModDecl,
    Node,
    Param,
    StructDecl,
    StructPatternField,
    TraitDecl,
    Type,
    TypeDecl,
    TypeParam,
    UseDecl,
    Variant,
)
from ._common import _walk_nodes

__all__ = [
    "_syntactic_conservative",
    "_syntactic_provides",
    "_syntactic_refs",
]

# Nodes whose ``name`` field *declares* a local item rather than referencing
# a sibling top-level one.  ``Attribute.name`` is deliberately absent: it is
# a member *reference* (``value.to_string`` references the method).
_DECL_NAME_NODES = (
    BindPattern,
    StructPatternField,
    LetStmt,
    Param,
    Field,
    Variant,
    TypeParam,
    ConstDecl,
    TypeDecl,
    StructDecl,
    EnumDecl,
    TraitDecl,
    ModDecl,
    UseDecl,
    FnDecl,
    ExternStatic,
    ImplDecl,
    ExtraDecl,
    ExternBlock,
)


_MACRO_MANGLE_RE = re.compile(r"^_m(\d+)_(.+)$")


def _unmangled(name: str) -> Optional[str]:
    """Base name of a macro-hygiene identifier (``_m1_print`` → ``print``).

    SA's name resolution retries a mangled miss with the base name
    (expansion-bound members are unhygienic surfaces), so the pre-SA
    dependency scan must record both spellings.
    """
    m = _MACRO_MANGLE_RE.match(name)
    return m.group(2) if m is not None else None


def _add_name(names: set[str], value: str) -> None:
    names.add(value)
    base = _unmangled(value)
    if base:
        names.add(base)


def _syntactic_refs(node: Node, names: set[str]) -> None:
    """Collect *references* in *node*'s subtree.

    Declaration names are skipped (they would make every declaration
    reference itself), everything else that can name a sibling top-level
    item counts: path segments, pattern paths, type names, attribute
    member names, ``which`` targets.  Locals also leak in as false
    positives (safe: widening only).
    """
    for n in _walk_nodes(node):
        for f in _dc_fields(n):
            value = getattr(n, f.name, None)
            if f.name == "name":
                if isinstance(value, str) and not isinstance(
                    n, _DECL_NAME_NODES
                ):
                    _add_name(names, value)
            elif f.name in ("parts", "path", "group", "alias", "which"):
                if isinstance(value, str):
                    _add_name(names, value)
                elif isinstance(value, list):
                    for v in value:
                        if isinstance(v, str):
                            _add_name(names, v)
            elif f.name == "struct" and isinstance(value, str):
                _add_name(names, value)


def _provided_spellings(item: Node, name: Any) -> set[str]:
    """All spellings under which *name* may be referenced.

    ``_qualify_shadowed_std_functions`` (todo-175) renames a shadowed
    std item to its FQN and keeps the source spelling on ``_scope_orig``;
    a scoped reference in std's own body still says the base name, so
    both spellings must satisfy the dependency edge.  The FQN's last
    segment covers unqualified-looking references as well.
    """
    out: set[str] = set()
    if isinstance(name, str) and name:
        out.add(name)
        if "::" in name:
            out.add(name.rsplit("::", 1)[-1])
    orig = getattr(item, "_scope_orig", None)
    if isinstance(orig, str) and orig:
        out.add(orig)
    return out


def _syntactic_provides(item: Node) -> set[str]:
    """Names through which *item* can satisfy a reference."""
    out: set[str] = set()
    if isinstance(item, FnDecl):
        out |= _provided_spellings(item, item.name)
    elif isinstance(item, (StructDecl, EnumDecl, TypeDecl, TraitDecl, ConstDecl)):
        out |= _provided_spellings(item, item.name)
    elif isinstance(item, (ImplDecl, ExtraDecl)):
        for t in (getattr(item, "struct", None), getattr(item, "trait", None)):
            if isinstance(t, Type) and isinstance(t.name, str) and t.name:
                out.add(t.name.split("<", 1)[0])
        for m in getattr(item, "methods", None) or []:
            if isinstance(m, FnDecl):
                out |= _provided_spellings(m, m.name)
        for c in getattr(item, "consts", None) or []:
            if isinstance(c, ConstDecl) and isinstance(c.name, str):
                out.add(c.name)
    elif isinstance(item, ExternBlock):
        for m in (*item.fns, *item.statics):
            out |= _provided_spellings(m, getattr(m, "name", None))
        for t in item.types:
            out |= _provided_spellings(t, getattr(t, "name", None))
    elif isinstance(item, ModDecl):
        if isinstance(item.name, str):
            out.add(item.name)
    return out


def _syntactic_conservative(item: Node) -> bool:
    """main / which hooks / static methods — kept regardless of refs."""
    if isinstance(item, FnDecl):
        return (
            getattr(item, "name", None) == "main"
            or getattr(item, "which", None) is not None
            or bool(getattr(item, "static", False))
        )
    if isinstance(item, (ImplDecl, ExtraDecl)):
        return any(
            getattr(m, "which", None) is not None
            or bool(getattr(m, "static", False))
            for m in getattr(item, "methods", None) or []
        )
    return False
