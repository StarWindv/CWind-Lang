"""todo-160: ``--module-tree`` — the resolved module tree, not an AST dump.

Builds the post-parse module graph the way the compiler actually resolved
it (declaration-driven tree, todo-158): one node per module file, each
showing its public items, private items and re-exports/imports.  ``--contain-
std`` adds the std subtree that the compilation actually touched.

Data layer: builds structured :class:`ModuleNode` graphs — no rendering
strings here.  Labels/visibility spellings live on the nodes as plain
data; ``render_module_tree`` (text) and ``module_tree_to_json`` (JSON)
only draw what the data says.

Extension seams (reserved, currently unused):

- ``ModuleNode.role`` accepts ``"extern-cwind"`` for future
  ``extern "CWind"`` built-in modules (todo-132) — same shape as ordinary
  modules, flagged so renderers can style them;
- ``ImportRow.kind`` distinguishes ``"package"`` imports (todo-37/101
  user-package addressing) from plain ``"use"`` ones — the data source
  fills it, renderers display it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .ast_components.ast import (
    ConstDecl,
    EnumDecl,
    ExternBlock,
    ExtraDecl,
    FnDecl,
    GroupDecl,
    ImplDecl,
    ModDecl,
    Node,
    Program,
    StructDecl,
    TraitDecl,
    TypeDecl,
    UseDecl,
)

__all__ = [
    "DeclRow",
    "ImportRow",
    "ModuleNode",
    "build_module_tree",
    "render_module_tree",
    "module_tree_to_json",
]

# role values a ModuleNode may carry.  "extern-cwind" is reserved for
# todo-132's ``extern "CWind"`` built-in modules; "package" for user
# packages pulled in through the package manager (todo-37/101).
ROLE_ENTRY = "entry"
ROLE_CRATE_ROOT = "crate-root"
ROLE_STD_ROOT = "std-root"
ROLE_MODULE = "module"
ROLE_INLINE = "inline"
ROLE_EXTERN_CWIND = "extern-cwind"
ROLE_PACKAGE = "package"

# ImportRow.kind values.
IMPORT_USE = "use"          # plain ``use path;`` / ``use path::item;``
IMPORT_WILDCARD = "wildcard"    # ``use path::*;``
IMPORT_REEXPORT = "reexport"    # ``pub use ...`` (item re-export)
IMPORT_MOD_DECL = "mod-decl"    # materialized ``mod name;`` submodule
IMPORT_PACKAGE = "package"      # user-package import (reserved, todo-37)
IMPORT_CRATE_EXPORT = "crate-export"    # ``export crate name;`` (todo-126)


@dataclass
class DeclRow:
    """One top-level declaration shown inside a module node."""

    name: str
    kind: str
    visibility: str = "private"     # data, not spelling: "private"|"pub"|...
    detail: str = ""                # e.g. impl target, extern members


@dataclass
class ImportRow:
    """One ``use`` (or materialized ``mod``) line shown in a module node."""

    path: str
    kind: str = IMPORT_USE
    alias: Optional[str] = None
    item: Optional[str] = None
    pub: bool = False
    # for IMPORT_MOD_DECL: the submodule's declaration visibility
    mod_pub: bool = False


@dataclass
class ModuleNode:
    """One module file (or inline namespace) in the resolved tree."""

    name: str
    parts: list[str]
    file: Optional[str] = None          # absolute source path
    inline: bool = False                # inline ``mod name { ... }`` block
    role: str = ROLE_MODULE
    decls: list[DeclRow] = field(default_factory=list)
    imports: list[ImportRow] = field(default_factory=list)
    children: list["ModuleNode"] = field(default_factory=list)


def _visibility_of(pub: bool, item: Node) -> str:
    """Visibility of *item* as plain data ("private" / "pub" / "pub(x)")."""
    if not pub:
        return "private"
    vis = getattr(item, "visibility", None)
    if vis is None:
        return "pub"
    if vis == "in":
        path = getattr(item, "vis_path", None) or []
        return f"pub(in {'::'.join(path)})"
    return f"pub({vis})"


def _decl_row(item: Node) -> Optional[DeclRow]:
    """Classify one top-level declaration for display."""
    visibility = _visibility_of(bool(getattr(item, "pub", False)), item)
    name = getattr(item, "name", None)
    orig = getattr(item, "_scope_orig", None)
    if isinstance(name, str) and isinstance(orig, str):
        # flattened private helper: show the source spelling, note the
        # mangled final name only in the detail slot
        name = orig if name.endswith(orig) or orig in name else name
    if isinstance(item, FnDecl):
        return DeclRow(name or "", "fn", visibility)
    if isinstance(item, ConstDecl):
        return DeclRow(name or "", "const", visibility)
    if isinstance(item, StructDecl):
        generics = ", ".join(p.name for p in item.params)
        return DeclRow(
            (name or "") + (f"<{generics}>" if generics else ""),
            "struct", visibility,
        )
    if isinstance(item, EnumDecl):
        return DeclRow(name or "", "enum", visibility)
    if isinstance(item, TraitDecl):
        return DeclRow(name or "", "trait", visibility)
    if isinstance(item, TypeDecl):
        return DeclRow(name or "", "type", visibility)
    if isinstance(item, GroupDecl):
        return DeclRow(name or "", "group", "private")
    if isinstance(item, ExternBlock):
        members = ", ".join(
            f"fn {m.name}" if hasattr(m, "params") else f"static {m.name}"
            for m in (*item.fns, *item.statics)
        )
        return DeclRow(
            f'extern "{item.abi}"', "extern", visibility,
            detail=f"{{ {members} }}",
        )
    if isinstance(item, ImplDecl):
        trait = ("!" if item.negative else "") + item.trait.name
        if item.trait.args:
            trait += f"<{', '.join(a.name for a in item.trait.args)}>"
        return DeclRow(f"impl {trait} for {item.struct.name}", "impl", "")
    if isinstance(item, ExtraDecl):
        owner = getattr(item.struct, "name", None) or item.struct.name
        return DeclRow(owner, "extra", "")
    return None


def _import_row(u: UseDecl) -> ImportRow:
    path = "::".join(u.parts)
    if u.wildcard:
        path = f"{path}::*" if u.parts else "*"
    kind = IMPORT_USE
    if getattr(u, "crate_export", False):
        kind = IMPORT_CRATE_EXPORT
    elif getattr(u, "_from_mod_decl", False):
        kind = IMPORT_MOD_DECL
    elif u.wildcard:
        kind = IMPORT_WILDCARD
    elif u.pub:
        kind = IMPORT_REEXPORT
    return ImportRow(
        path=path,
        kind=kind,
        alias=getattr(u, "alias", None),
        item=getattr(u, "item", None),
        pub=bool(u.pub),
        mod_pub=bool(getattr(u, "_mod_decl_pub", False)),
    )


def _group_file_items(
    items: list[Node],
) -> tuple[
    dict[str, list[Node]],      # source file -> top-level items
    dict[str, list[ModDecl]],   # source file -> inline ModDecls (recursed)
]:
    """Group every item by its defining file (``source_module``)."""
    by_file: dict[str, list[Node]] = {}
    inline: dict[str, list[ModDecl]] = {}

    def visit(item: Node) -> None:
        home = getattr(item, "source_module", None)
        if isinstance(item, ModDecl) and item.body is not None:
            inline.setdefault(home or "", []).append(item)
            for sub in item.body.stmts:
                visit(sub)
            return
        by_file.setdefault(home or "", []).append(item)

    for item in items:
        visit(item)
    return by_file, inline


def _file_label(parts: list[str], file: Optional[str], fallback: str) -> str:
    """Node display name: the module's own last segment (nesting conveys
    the full path)."""
    if parts:
        return parts[-1]
    if file:
        return Path(file).stem
    return fallback


def _build_file_node(
    parts: Optional[list[str]],
    file: Optional[str],
    items: list[Node],
    inline_decls: list[ModDecl],
    role: str,
    name_fallback: str,
) -> ModuleNode:
    """One module file node: decls (pub first), imports, inline children."""
    node = ModuleNode(
        name=_file_label(parts or [], file, name_fallback),
        parts=list(parts or []),
        file=file,
        role=role,
    )
    pub_rows: list[DeclRow] = []
    priv_rows: list[DeclRow] = []
    use_rows: list[ImportRow] = []
    for item in items:
        if isinstance(item, UseDecl):
            use_rows.append(_import_row(item))
            continue
        if isinstance(item, ModDecl):
            # External ``mod name;``: render the declaration row itself
            # (its materialized use carries the submodule re-export).
            mat = getattr(item, "_materialized_use", None)
            if mat is not None:
                row = _import_row(mat)
                use_rows.append(row)
            continue
        if isinstance(item, Program):
            continue
        row = _decl_row(item)
        if row is None:
            continue
        (pub_rows if row.visibility not in ("private", "") else priv_rows).append(row)
    # Inline ``mod`` declarations: submodule rows on the host + child nodes.
    for mod in inline_decls:
        child = _build_inline_node(mod)
        node.children.append(child)
        row = DeclRow("mod " + mod.name, "mod", _visibility_of(bool(mod.pub), mod))
        (pub_rows if mod.pub else priv_rows).append(row)
    node.decls = [*pub_rows, *priv_rows]
    node.imports = use_rows
    return node


def _build_inline_node(mod: ModDecl) -> ModuleNode:
    """One inline ``mod name { ... }`` namespace node."""
    node = ModuleNode(
        name=mod.name,
        parts=[],
        inline=True,
        role=ROLE_INLINE,
    )
    pub_rows: list[DeclRow] = []
    priv_rows: list[DeclRow] = []
    for sub in mod.body.stmts if mod.body else []:
        if isinstance(sub, UseDecl):
            node.imports.append(_import_row(sub))
            continue
        if isinstance(sub, ModDecl):
            node.children.append(_build_inline_node(sub))
            row = DeclRow("mod " + sub.name, "mod", _visibility_of(bool(sub.pub), sub))
            (pub_rows if sub.pub else priv_rows).append(row)
            continue
        row = _decl_row(sub)
        if row is None:
            continue
        (pub_rows if row.visibility not in ("private", "") else priv_rows).append(row)
    node.decls = [*pub_rows, *priv_rows]
    return node


def _insert_by_parts(
    root: ModuleNode,
    parts: list[str],
    node: ModuleNode,
) -> None:
    """Attach *node* under *root* following the module path *parts*.

    The final segment merges into an existing child (a declared directory
    module and its file module share one node); intermediate segments are
    created as plain holders on demand.
    """
    if not parts:
        root.children.append(node)
        return
    head, *rest = parts
    child = next(
        (c for c in root.children if c.name == head and not c.inline), None
    )
    if child is None:
        child = ModuleNode(name=head, parts=[head])
        root.children.append(child)
    if not rest:
        child.decls.extend(node.decls)
        child.imports.extend(node.imports)
        child.children.extend(node.children)
        child.file = child.file or node.file
        if not child.parts:
            child.parts = list(node.parts)
        return
    _insert_by_parts(child, rest, node)


def build_module_tree(
    program: Program,
    *,
    entry_file: Optional[str],
    crate_name: Optional[str] = None,
    contain_std: bool = False,
    std_root_file: Optional[str] = None,
) -> dict[str, Any]:
    """Assemble the resolved module tree as structured data.

    Returns ``{"entry", "crate_name", "roots": [ModuleNode...]}`` where
    ``roots[0]`` is the crate tree (crate root = entry + user modules) and,
    when *contain_std* is set and std modules were actually loaded,
    ``roots[-1]`` is the std subtree the compilation touched.

    The crate root node's own declarations come from the package facade
    (``lib.wd``, the crate root module); the entry is a child.  Module
    files nest by their declared module path (``modules::great`` under
    ``modules``); the re-export / import / submodule-declaration rows of
    each file come from its ``use`` lines.
    """
    by_file, inline_by_file = _group_file_items(program.items)
    # Merge the full item sets of every loaded module file (the root only
    # carries the dependency closure; a file's own ``mod`` declarations and
    # their materialized implicit uses live on its cached program).
    file_programs = getattr(program, "_module_file_programs", {}) or {}
    for path_key, child in file_programs.items():
        child_by_file, child_inline = _group_file_items(child.items)
        for home, home_items in child_by_file.items():
            existing = by_file.setdefault(home, [])
            seen = {id(x) for x in existing}
            for it in home_items:
                if id(it) not in seen:
                    existing.append(it)
                    seen.add(id(it))
        for home, home_inline in child_inline.items():
            existing_inline = inline_by_file.setdefault(home, [])
            seen_inline = {id(x) for x in existing_inline}
            for it in home_inline:
                if id(it) not in seen_inline:
                    existing_inline.append(it)
                    seen_inline.add(id(it))

    entry_resolved = str(Path(entry_file).resolve()) if entry_file else None

    crate_root = ModuleNode(
        name=crate_name or "crate", parts=[], role=ROLE_CRATE_ROOT,
    )
    std_root = ModuleNode(
        name="std", parts=["std"], role=ROLE_STD_ROOT, file=std_root_file,
    )
    std_root_used = False
    nodes_by_parts: dict[tuple[str, ...], ModuleNode] = {}

    def crate_lib_home() -> Optional[str]:
        """Home file of the package facade (lib.wd), if part of the build."""
        for home in by_file:
            if home and Path(home).name.lower().startswith("lib."):
                return home
        return None

    lib_home = crate_lib_home()

    def node_for_home(home: str) -> Optional[ModuleNode]:
        if lib_home is not None and home == lib_home:
            # The package facade IS the crate root module: its items hang
            # directly off the [crate] header.
            return crate_root
        return None

    def append_rows(target: ModuleNode, node: ModuleNode) -> None:
        target.decls.extend(node.decls)
        target.imports.extend(node.imports)
        target.children.extend(node.children)
        target.file = target.file or node.file

    for home, home_items in sorted(by_file.items()):
        # The std root module's wildcard-loaded items carry no home tag
        # (EMPTY home): they belong to the std subtree, not "<stdin>".
        # The auto prelude shell itself (only wildcard facade rows) is
        # never shown.
        real_items = [
            i for i in home_items
            if not (
                isinstance(i, UseDecl) and getattr(i, "auto", False)
                and not getattr(i, "alias", None)
            )
        ]
        if not real_items and not inline_by_file.get(home):
            continue
        parts = getattr(home_items[0], "source_module_path", None) if home_items else None
        is_std = (bool(parts) and parts[0] == "std") or home == ""
        inline_decls = inline_by_file.get(home, [])
        role = ROLE_ENTRY if entry_resolved and home == entry_resolved else ROLE_MODULE
        node = _build_file_node(
            parts, home, real_items, inline_decls, role,
            name_fallback=Path(home).stem if home else "std::prelude",
        )
        if is_std:
            if contain_std and parts is not None:
                std_root_used = True
                _insert_by_parts(std_root, list(parts[1:]), node)
            continue
        facade = node_for_home(home)
        if facade is not None:
            append_rows(facade, node)
            continue
        if not parts:
            # A source file outside every module root: entry or wild file.
            if role == ROLE_ENTRY:
                append_rows(crate_root, node)
            else:
                crate_root.children.append(node)
            continue
        # Register every path prefix so parents exist before children; a
        # later file for the same path merges into the existing node.
        parent = crate_root
        acc: list[str] = []
        for seg in parts[:-1]:
            acc.append(seg)
            if tuple(acc) not in nodes_by_parts:
                holder = ModuleNode(name=seg, parts=list(acc), role=ROLE_MODULE)
                nodes_by_parts[tuple(acc)] = holder
                parent.children.append(holder)
            parent = nodes_by_parts[tuple(acc)]
        existing = nodes_by_parts.get(tuple(parts))
        if existing is not None:
            append_rows(existing, node)
        else:
            parent.children.append(node)
            nodes_by_parts[tuple(parts)] = node

    roots = [crate_root]
    if contain_std and std_root_used:
        roots.append(std_root)
    return {
        "entry": entry_file,
        "crate_name": crate_name or "crate",
        "roots": roots,
    }


# -- text rendering (draws only what the data says) ----------------------------

_ROLE_LABELS = {
    ROLE_ENTRY: "entry",
    ROLE_CRATE_ROOT: "crate",
    ROLE_STD_ROOT: "std",
    ROLE_MODULE: "module",
    ROLE_INLINE: "inline mod",
    ROLE_EXTERN_CWIND: 'extern "CWind"',
    ROLE_PACKAGE: "package",
}

_KIND_LABELS = {
    IMPORT_USE: "use",
    IMPORT_WILDCARD: "use",
    IMPORT_REEXPORT: "pub use",
    IMPORT_MOD_DECL: "mod",
    IMPORT_PACKAGE: "package",
    IMPORT_CRATE_EXPORT: "export crate",
}


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
        title = f"[{_ROLE_LABELS.get(node.role, node.role)}] {node.name}"
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
        rows.append(f"{row.visibility} {kind_part}{row.name}{_detail(row.detail)}")
    for imp in node.imports:
        kind_label = _KIND_LABELS.get(imp.kind, imp.kind)
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


# -- JSON rendering ------------------------------------------------------------

def module_tree_to_json(tree: dict[str, Any]) -> dict[str, Any]:
    """JSON shape of the module tree (``--module-tree --json``)."""
    return {
        "format": "cwind-module-tree",
        "version": 1,
        "entry": tree.get("entry"),
        "crate_name": tree.get("crate_name"),
        "roots": [_node_json(node) for node in tree["roots"]],
    }


def _node_json(node: ModuleNode) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": node.name,
        "role": node.role,
        "inline": node.inline,
    }
    if node.file:
        out["file"] = node.file
    if node.parts:
        out["path"] = "::".join(node.parts)
    out["decls"] = [
        {
            "name": row.name,
            "kind": row.kind,
            "visibility": row.visibility,
            **({"detail": row.detail} if row.detail else {}),
        }
        for row in node.decls
    ]
    out["imports"] = [
        {
            "path": imp.path,
            "kind": imp.kind,
            **({"alias": imp.alias} if imp.alias else {}),
            **({"item": imp.item} if imp.item else {}),
            "pub": imp.pub,
            **({"mod_pub": imp.mod_pub} if imp.kind == IMPORT_MOD_DECL else {}),
        }
        for imp in node.imports
    ]
    if node.children:
        out["modules"] = [_node_json(child) for child in node.children]
    return out
