"""Typed-AST serialization (spec: ``ProxyRegulations/TypedAST.md``)."""

from __future__ import annotations

from dataclasses import fields as _dc_fields
from pathlib import Path, PurePosixPath
from typing import Any, Optional

from .ast_components.ast import Node, Program, UseDecl
from .sa import ProgramInfo
from .sa.types import _bare_type

__all__ = ["build_typed_ast", "build_module_artifacts"]


def _bare_type_names(node: Any) -> None:
    """todo-154: the JSON contract is **bare-named** — strip in place.

    FQN (``std::builtins::X``) is the SA-internal storage form on Type
    AST nodes; the typed-AST is the frontend/backend boundary and both
    sides speak bare names.  This walks the serialized envelope and
    rewrites:

    - ``{"kind": "Type", "name": ...}`` nodes (incl. ``cwind_owner``,
      impl/extra targets, fn/ptr/array flat names — ``_bare_type`` is
      construct-aware);
    - ``bindings[].owner`` strings (belt-and-suspenders: pass 0 skips
      definition-site owners, so they are already bare).

    ``ann.type`` objects carry no ``kind`` and are produced bare by
    ``_type_info``, so they pass through untouched.  Module paths
    (``def`` / ``owner_def`` / ``trait_def``) never carry the builtin
    prefix.
    """
    if isinstance(node, dict):
        if node.get("kind") == "Type":
            name = node.get("name")
            if isinstance(name, str):
                bare = _bare_type(name)
                if bare is not None:
                    node["name"] = bare
        owner = node.get("owner")
        if isinstance(owner, str):
            bare = _bare_type(owner)
            if bare is not None:
                node["owner"] = bare
        for value in node.values():
            _bare_type_names(value)
    elif isinstance(node, list):
        for value in node:
            _bare_type_names(value)


def _symbol_entry(sym: Any, def_paths: dict[str, str]) -> dict[str, Any]:
    """todo-146: symbol entry with definition-site ``def`` provenance.

    本模块 (入口文件) 自己的声明没有 ``def`` (键整体省略, 保持与历史
    输出一致); prelude/其它模块内联进来的符号带其定义位置规范路径。
    """
    entry: dict[str, Any] = {"name": sym.name, "kind": sym.kind, "ref": sym.ref}
    def_path = def_paths.get(sym.name)
    if def_path is not None:
        entry["def"] = def_path
    return entry


def _binding_entry(binding: Any, def_paths: dict[str, str]) -> dict[str, Any]:
    """todo-146: binding entry with ``owner_def`` / ``trait_def``.

    ``owner`` / ``trait`` 字符串本身保持扁平规范名不变 (后端按它做
    符号匹配与名称修饰), 定义位置作为兄弟键补充, 缺省省略。
    """
    entry = binding.to_dict()
    owner_def = def_paths.get(binding.owner)
    if owner_def is not None:
        entry["owner_def"] = owner_def
    if binding.trait:
        trait_def = def_paths.get(binding.trait)
        if trait_def is not None:
            entry["trait_def"] = trait_def
    return entry


def _collect_serialized_ids(ast_dict: Any, ids: set[int]) -> None:
    """Gather every node ``id`` present in a serialized AST document."""
    if isinstance(ast_dict, dict):
        node_id = ast_dict.get("id")
        if isinstance(node_id, int):
            ids.add(node_id)
        for value in ast_dict.values():
            _collect_serialized_ids(value, ids)
    elif isinstance(ast_dict, list):
        for value in ast_dict:
            _collect_serialized_ids(value, ids)


def build_typed_ast(
    program: Node,
    info: ProgramInfo,
    source: Optional[str] = None,
) -> dict[str, Any]:
    """Assemble the ``cwind-typed-ast`` envelope.

    The AST is serialized with every node carrying its pre-order ``id`` and
    the ``ann`` dictionary filled in by the semantic analyzer; ``symbols`` /
    ``bindings`` reference those ids.  ``source`` (todo-63) is the absolute
    path of the compiled source file when known; the backend uses its
    directory to resolve ``#[link(path = "...", relative = "source")]``.
    """
    symbols = [
        _symbol_entry(sym, info.def_paths) for sym in info.symbols.values()
    ]
    bindings = [
        _binding_entry(binding, info.def_paths) for binding in info.bindings
    ]
    if info.import_manifest:
        # todo-76/78: one entry per ``use`` declaration with its own
        # resolved source file (the implicit prelude included, flagged
        # ``auto``).
        imports = [dict(entry) for entry in info.import_manifest]
    else:
        imports = [
            {"path": list(parts), "source": next(iter(info.imported_modules), None)}
            for parts in info.modules.values()
        ]
    env = {
        "format": "cwind-typed-ast",
        "version": 1,
        "source": source,
        "imports": imports,
        "symbols": symbols,
        "bindings": bindings,
        "ast": program.to_dict(include_meta=True),
    }
    # todo-154: JSON 契约裸名化 (剥 Type 节点上的 FQN 存储形)
    _bare_type_names(env)
    # 文档自洽: symbols/bindings 的 ref 必须落在本文档的节点池里
    # (与 build_module_artifacts 的 per-file 过滤同一纪律)。依赖闭包
    # 中仅被限定寻址索引的成员 (libcbind/ctypedef...) 不在入口 ast
    # 段内, 序列化成悬空 ref 会被后端节点池加载拒绝。
    serialized: set[int] = set()
    _collect_serialized_ids(env["ast"], serialized)
    env["symbols"] = [
        s for s in env["symbols"] if s.get("ref") in serialized
    ]
    env["bindings"] = [
        b for b in env["bindings"]
        if b.get("decl_id") in serialized and b.get("fn_id") in serialized
    ]
    return env


def _collect_ids(node: Node, ids: set[int]) -> None:
    """Gather every AST node id inside *node*'s subtree."""
    node_id = getattr(node, "_typed_id", None)
    if node_id is not None:
        ids.add(node_id)
    for f in _dc_fields(node):
        if f.name in ("line", "column"):
            continue
        value = getattr(node, f.name)
        if isinstance(value, Node):
            _collect_ids(value, ids)
        elif isinstance(value, list):
            for element in value:
                if isinstance(element, Node):
                    _collect_ids(element, ids)


def build_module_artifacts(
    program: Program,
    info: ProgramInfo,
    *,
    entry_source: Optional[str] = None,
) -> list[dict[str, Any]]:
    """todo-98: one semantically annotated JSON document per source file.

    The whole-program pipeline (single SA pass over the flattened program)
    stays authoritative; this partitions its results by the defining file
    so every module gets a standalone artifact mirroring the source tree.
    Each document keeps the ``cwind-typed-ast`` envelope shape:

    - ``module``: project-relative POSIX path of the source file;
    - ``role``: ``"entry"`` for the package entry, else ``"module"``;
    - ``imports``: every ``use`` homed in that file (parser module table);
    - ``symbols`` / ``bindings``: filtered to the module's own nodes;
    - ``ast``: ``Program`` holding exactly the items declared by the file,
      with all SA annotations (``id`` / ``ann``) intact.  Node ids are the
      global ones from the unified pass, so cross-artifact references
      (call targets living in another module's artifact) stay resolvable.

    These artifacts are not directly compilable yet -- they are the input
    format for future per-module/incremental backends (todo-99).
    """
    groups: dict[str, list[Node]] = {}
    order: list[str] = []

    def note_home(home: Optional[str]) -> None:
        if home and home not in groups:
            groups[home] = []
            order.append(home)

    for item in program.items:
        home: Optional[str] = getattr(item, "source_module", None)
        if isinstance(item, UseDecl):
            note_home(entry_source if home is None else home)
            continue
        # Items without a source tag cannot happen in project mode (every
        # top-level item is tagged at parse time); park them on the entry
        # so they are never silently dropped.
        effective = home if home else entry_source
        note_home(effective)
        groups.setdefault(effective or "", []).append(item)
    # Facade files may own no declarations at all but still participate
    # through their imports; the parser's module table knows every file.
    table = getattr(program, "_module_table", {}) or {}
    for home in table:
        note_home(home)

    artifacts: list[dict[str, Any]] = []
    for home in order:
        items = groups.get(home, [])
        ids: set[int] = set()
        for item in items:
            _collect_ids(item, ids)
        table_entry = table.get(home, {})
        doc = {
            "format": "cwind-typed-ast",
            "version": 1,
            "source": home,
            "role": "entry" if home == entry_source else "module",
            "module": home.replace("\\", "/"),
            "imports": [
                dict(entry) for entry in table_entry.get("imports", ())
            ],
            "symbols": [
                _symbol_entry(sym, info.def_paths)
                for sym in info.symbols.values()
                if sym.ref in ids
            ],
            "bindings": [
                _binding_entry(binding, info.def_paths)
                for binding in info.bindings
                if binding.decl_id in ids
            ],
            "ast": Program(
                items[0].line if items else 1,
                items[0].column if items else 1,
                list(items),
            ).to_dict(include_meta=True),
        }
        # todo-154: JSON 契约裸名化 (与 build_typed_ast 同一纪律)
        _bare_type_names(doc)
        artifacts.append(doc)
    return artifacts


def module_artifact_relpath(source: str, project_root: str | Path) -> str:
    """Project-relative POSIX path under which an artifact is written."""
    rel = Path(source).resolve().relative_to(Path(project_root).resolve())
    return PurePosixPath(*rel.parts).as_posix() + ".json"
