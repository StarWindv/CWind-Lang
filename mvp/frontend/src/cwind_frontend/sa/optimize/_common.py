"""Shared AST walk helpers for the optimize package (both DCE layers)."""

from __future__ import annotations

from dataclasses import fields as _dc_fields

from ...ast_components.ast import Node

__all__ = ["_is_std", "_walk_nodes"]


def _is_std(item: Node) -> bool:
    """Whether *item* was declared inside a ``libs`` (std) tree."""
    path = getattr(item, "source_module_path", None) or []
    return bool(path) and path[0] == "std"


def _walk_nodes(node: Node):
    """Yield every AST node in the subtree, pre-order."""
    yield node
    for f in _dc_fields(node):
        if f.name in ("line", "column"):
            continue
        value = getattr(node, f.name, None)
        if isinstance(value, Node):
            yield from _walk_nodes(value)
        elif isinstance(value, list):
            for v in value:
                if isinstance(v, Node):
                    yield from _walk_nodes(v)
