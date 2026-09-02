"""``cargo tree``-style module-tree rendering.

Pure layout: the tree data (nodes / decls / imports) is built by
:mod:`cwind_frontend.module_tree`; this module only draws it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..module_tree import (
    IMPORT_MOD_DECL,
    KIND_LABELS,
    ROLE_LABELS,
    ModuleNode,
)

__all__ = ["render_module_tree"]


def render_module_tree(tree: dict[str, Any]) -> str:
    """Human-readable tree (``cargo tree``-style connectors)."""
    lines: list[str] = []
    entry = tree.get("entry")
    lines.append(f"Module tree for {entry or '<stdin>'}")
    lines.append("")
    for root in tree["roots"]:
        _render_node(root, "", True, lines, header=True)
        lines.append("")
    return "\n".join(lines).rstrip()


def _render_node(
    node: ModuleNode,
    prefix: str,
    is_last: bool,
    lines: list[str],
    *,
    header: bool = False,
) -> None:
    if header:
        title = f"[{ROLE_LABELS.get(node.role, node.role)}] {node.name}"
        if node.file:
            title += f"  —  {node.file}"
        lines.append(title)
        child_prefix = ""
    else:
        connector = "└── " if is_last else "├── "
        title = node.name
        if node.file:
            title += f"  —  {_shorten(node.file)}"
        lines.append(prefix + connector + title)
        child_prefix = prefix + ("    " if is_last else "│   ")

    rows: list[str] = []
    for row in node.decls:
        kind_part = f"{row.kind} " if row.kind else ""
        rows.append(
            f"{row.visibility} {kind_part}{row.name}{_detail(row.detail)}"
        )
    for imp in node.imports:
        kind_label = KIND_LABELS.get(imp.kind, imp.kind)
        target = imp.path
        if imp.kind == IMPORT_MOD_DECL:
            target = imp.path.split("::")[-1]
        label = f"{kind_label} {target}"
        if imp.kind == IMPORT_MOD_DECL and imp.mod_pub:
            label = f"pub {label}"
        if imp.alias:
            label += f" as {imp.alias}"
        elif imp.item and imp.kind != IMPORT_MOD_DECL:
            label += f"  (item: {imp.item})"
        rows.append(label)

    for i, row in enumerate(rows):
        last = i == len(rows) - 1 and not node.children
        branch = "└── " if last else "├── "
        lines.append(child_prefix + branch + row)
    for i, child in enumerate(node.children):
        _render_node(
            child,
            child_prefix,
            i == len(node.children) - 1 and not rows,
            lines,
        )


def _detail(detail: str) -> str:
    return f" {detail}" if detail else ""


def _shorten(path: str) -> str:
    try:
        return str(Path(path).resolve())
    except OSError:
        return path
