"""SA pass 0 (todo-154/160): FQN expansion and qualified type-path
resolution, plus the ``--pass 0`` report entry point."""

from __future__ import annotations

import copy
from dataclasses import fields as _fields
from typing import TYPE_CHECKING, Optional

from .types import (
    _qualify_builtin,
    _strip_builtin_ns,
    _type_str_raw,
)
from ..ast_components.ast import (
    ExternBlock,
    ExtraDecl,
    FnDecl,
    ImplDecl,
    Node,
    Program,
    TraitDecl,
    Type,
    TypeDecl,
    TypeParam,
)

if TYPE_CHECKING:
    from .analyzer import _Analyzer


class FqnPass:

    # -- pass 0: FQN expansion (todo-154) ----------------------------------
    def _fqn_expand(self: "_Analyzer", program: Program) -> None:
        """todo-154: expand typedef aliases to their canonical FQN forms.

        Runs before which hooks, pass 1 and everything else.  The alias
        table is collected **globally** (every ``TypeDecl`` of every file,
        prelude included): the flat namespace rejects cross-module
        duplicate definitions at pass 1, so a single table cannot mix up
        scopes — the per-file table of the first implementation was the
        root cause of prelude aliases never expanding.

        Two canonicalization steps per ``Type`` node:

        1. alias expansion — chained typedefs collapse to their RHS with
           generic parameters substituted (``Vec<Int>`` -> RHS of ``Vec``);
           the pre-expansion spelling is preserved on ``_fqn_original``;
        2. builtin qualification — a bare built-in base name becomes
           ``std::builtins::X`` (the FQN storage form).  User types keep
           their flat name; qualified paths (``std::option::Option``) are
           resolved later by :meth:`_fqn_resolve_paths`, which needs the
           module table.

        The JSON contract (typed-AST) stays bare-named: ``_type_info``
        and the typed-AST builder strip the prefix at the serialization
        boundary, so the backend is untouched.
        """
        if getattr(self, "_fqn_expanded", False):
            return
        self._fqn_expanded = True
        # todo-160 (--pass 0): structured expansion records, consumed by
        # the CLI's expansion-table renderer.
        self._fqn_report: list[dict] = []

        # Collect all items reachable from the root program (files +
        # inline mod bodies; namespace-hoisted files join later and fall
        # back to the check-time expanders).
        all_items = self._fqn_all_items(program)

        # One global alias table (refinement aliases ``where`` keep their
        # canonical name — predicates are keyed by the alias spelling).
        # Trait DECLS are collected alongside into a def-path table: a
        # trait reference canonicalizes to ``<def module path>::<Trait>``
        # (todo-154 extension — traits join the FQN storage convention so
        # a same-named user trait is distinguishable from a built-in type
        # in every downstream operation).  First spelling wins: a trait
        # flattened once per importing file yields several TraitDecl
        # instances of the same object.
        self._fqn_aliases = {}
        self._fqn_trait_paths: dict[str, str] = {}
        for item in all_items:
            if isinstance(item, TraitDecl) and item.name:
                if item.name in self._fqn_trait_paths:
                    continue
                def_path = getattr(item, "source_module_path", None)
                # 无 def 路径 (stdin/内存源) 记录恒等形: 同名 builtin
                # (trait Iterator) 不得被终点限定成内置类型。
                self._fqn_trait_paths[item.name] = (
                    "::".join(def_path) if def_path else item.name
                )
            if not isinstance(item, TypeDecl) or item.base is None:
                continue
            if item.where is not None:
                continue
            self._fqn_aliases[item.name] = item

        for item in all_items:
            self._fqn_walk_node(
                item, frozenset(), self._fqn_aliases,
                getattr(item, "source_module", None),
            )

    def _fqn_walk_node(
        self: "_Analyzer", node: Node, generics: frozenset[str],
        aliases: dict[str, TypeDecl],
        home: Optional[str] = None,
    ) -> None:
        """Recursively walk a node, expanding Type nodes."""
        skip: frozenset[str] = frozenset()
        if isinstance(node, (ImplDecl, ExtraDecl)):
            # 定义位 owner: impl/extra 的目标由现有 (裸名) 管线管理
            # (pass 1 bug-42 / pass 1.2 bug-43 别名展开 / 方法注册), pass 0
            # 保持原样。trait 引用**不跳过** —— trait 已纳入 FQN 存储
            # (``std::traits::display::Display`` 形态), 注册表消费点按
            # 裸名归一化。
            skip = frozenset({"struct"})
        elif isinstance(node, FnDecl):
            skip = frozenset({"cwind_owner"})
        if isinstance(node, Type):
            # todo-160 (--pass 0): provenance for the expansion report.
            if home is not None:
                t_home = getattr(node, "_fqn_home", None)
                if t_home is None:
                    node._fqn_home = home  # type: ignore[attr-defined]
            self._fqn_expand_type(node, generics, aliases, home)
            return
        item_home = getattr(node, "source_module", None) or home
        new_generics = self._fqn_generics_of(node)
        if new_generics:
            generics = generics | new_generics
        for f in _fields(node):
            if f.name in ("line", "column") or f.name in skip:
                continue
            value = getattr(node, f.name)
            if isinstance(value, Node):
                self._fqn_walk_node(value, generics, aliases, item_home)
            elif isinstance(value, list):
                for v in value:
                    if isinstance(v, Node):
                        self._fqn_walk_node(v, generics, aliases, item_home)

    @staticmethod
    def _fqn_generics_of(node: Node) -> frozenset[str]:
        for attr in ("params", "type_params"):
            params = getattr(node, attr, None)
            if isinstance(params, list):
                names = [p.name for p in params if isinstance(p, TypeParam)]
                if names:
                    return frozenset(names)
        return frozenset()

    def _fqn_expand_type(
        self: "_Analyzer", t: Type, generics: frozenset[str],
        aliases: dict[str, TypeDecl],
        home: Optional[str] = None,
    ) -> None:
        """Structurally expand one Type node's typedef alias.

        Recurse into args first, then expand the base name.  The original
        alias is saved on ``t._fqn_original`` so downstream code (typed-AST
        ``alias`` provenance, ``_fmt_type``) can still reference it.  The
        expansion endpoint is canonicalized: bare built-ins become
        ``std::builtins::X`` (FQN storage form).  ``home`` threads the
        owning file into the expansion report (--pass 0) — arg nodes are
        recursed here (not via the walker), so they inherit it.
        """
        if home is not None:
            t_home = getattr(t, "_fqn_home", None)
            if t_home is None:
                t._fqn_home = home  # type: ignore[attr-defined]
        for arg in t.args:
            self._fqn_expand_type(arg, generics, aliases, home)
        if (t.name.startswith("fn(") or t.name.startswith("*")
                or t.name.startswith("[")):
            return
        if t.name in generics or t.name == "Self":
            return
        # Iterate so alias chains (``vm2 = vm`` -> ``Vector<...>``) fully
        # collapse (bug-62: the impl target must reach the underlying type).
        for _ in range(16):  # guard against circular aliases
            if t.name.startswith("fn(") or t.name.startswith("*") \
                    or t.name.startswith("["):
                return
            if t.name in generics or t.name == "Self":
                return
            if "::" in t.name:
                # Qualified spellings are resolved by _fqn_resolve_paths
                # (needs the module table); ``std::builtins::X`` is already
                # canonical.  Either way alias expansion stops here.
                return
            alias = aliases.get(t.name)
            if alias is None or alias.base is None \
                    or alias.where is not None:
                break
            if len(t.args) != len(alias.params):
                # A bare generic alias (``Vec`` with no ``<...>``) has no
                # substitution at type positions — it is an arity error at
                # check time.  Never guess.
                break
            subst = dict(zip([p.name for p in alias.params], t.args))
            replacement = copy.deepcopy(alias.base)
            self._fqn_subst_type(replacement, subst, generics)
            if t.name == replacement.name and not replacement.args:
                return
            if not hasattr(t, "_fqn_original"):
                t._fqn_original = t.name
            # todo-160 (--pass 0): record one entry per alias expansion
            # step (chained aliases yield one row per hop).
            if getattr(self, "_fqn_report", None) is not None:
                self._fqn_report.append({
                    "kind": "alias",
                    "original": t.name,
                    "canonical": replacement.name,
                    "line": t.line,
                    "column": t.column,
                    "source": getattr(t, "_fqn_home", None),
                })
            t.name = replacement.name
            t.args = replacement.args
            # bug-52 ref-pointee aliases: ``&MyInt`` expands the pointee but
            # keeps the borrow marker (the RHS ``Int32`` carries no ``&``).
            if not replacement.ref:
                continue
            t.ref = replacement.ref
            t.mut = replacement.mut
            return
        # Endpoint qualification: a bare name becomes its FQN storage
        # form.  Priority: a known trait decl wins with its definition-
        # site module path (``std::traits::display::Display``) — a user
        # trait may share its name with a built-in type and must resolve
        # to the trait, not the builtin; otherwise a bare built-in
        # becomes ``std::builtins::X``.  ``!``/fn/ptr/array flat names
        # and generics never qualify.
        bare = _strip_builtin_ns(t.name)
        if bare is None or bare != t.name:
            return
        trait_fqn = getattr(self, "_fqn_trait_paths", {}).get(t.name)
        if trait_fqn is not None:
            # trait 的规范形 = 定义位置模块路径 + trait 名; 恒等形
            # (stdin/内存源无 def 路径) 保持裸名, 仅阻止 builtin qualify。
            qualified = f"{trait_fqn}::{t.name}" if trait_fqn != t.name else t.name
        else:
            qualified = _qualify_builtin(t.name)
        if qualified is not None and qualified != t.name:
            # todo-160 (--pass 0): record the qualification hop.
            if getattr(self, "_fqn_report", None) is not None:
                self._fqn_report.append({
                    "kind": "qualify",
                    "original": t.name,
                    "canonical": qualified,
                    "line": t.line,
                    "column": t.column,
                    "source": getattr(t, "_fqn_home", None),
                })
            t.name = qualified

    # -- pass 0 (phase 2): qualified type-path resolution -------------------
    def _fqn_all_items(self: "_Analyzer", program: Program) -> list[Node]:
        """Every item pass 0 must walk: the pass-1 collection surface
        (flattened items + inline mod bodies).

        deliberately NOT ``_module_file_programs``' top-level items: those
        include entries the parser *dropped* from the flat program because
        user code shadows them (todo-70 layering) — a shadowed prelude
        alias must never win the alias table over the live flat name.
        """
        items = list(program.items)
        items.extend(self._hoist_inline_mod_items(program.items))
        for child in getattr(program, "_module_file_programs", {}).values():
            items.extend(self._hoist_inline_mod_items(child.items))
        return items

    def _fqn_resolve_paths(self: "_Analyzer", program: Program) -> None:
        """todo-154: resolve qualified type paths to canonical names.

        Runs after the ``use``/module tables are built, still before the
        which-hook and analysis passes.  A leading chain of module
        segments walks the per-file module alias map (parser's
        ``_module_table``, auto prelude included) plus the todo-133
        namespace index; the trailing member is rewritten to the flat
        canonical name (built-ins re-qualified to their FQN form by the
        shared endpoint rule).

        Resolution failures are intentionally **left untouched** — the
        existing checkers (`_resolve_qualified_type_name`,
        `_reject_hidden`, the unknown-type verdict) keep emitting their
        precise diagnostics instead of pass 0 guessing a message.
        """
        maps = self._fqn_module_maps(program)
        for item in self._fqn_all_items(program):
            home = getattr(item, "source_module", None)
            self._fqn_resolve_node(item, maps.get(home, {}), home)

    def _fqn_module_maps(
        self: "_Analyzer", program: Program
    ) -> dict[Optional[str], dict[str, list[str]]]:
        """Per-file alias -> module-path maps for pass 0 path resolution.

        Built straight from the parser's module table so auto prelude
        imports (``use std::*`` -> alias ``std``) and materialized
        ``mod`` declarations are visible before ``self.modules`` exists.
        Item imports (``use m::T as X;``) are not module namespaces and
        are excluded.
        """
        table = getattr(program, "_module_table", None)
        maps: dict[Optional[str], dict[str, list[str]]] = {}
        if not isinstance(table, dict):
            return maps
        for home, data in table.items():
            m = maps.setdefault(home, {})
            for entry in data.get("imports", ()):
                if entry.get("item") is not None:
                    continue
                raw_parts = entry.get("path") or []
                parts: list[str] = [str(p) for p in raw_parts]
                if not parts:
                    continue
                alias = entry.get("alias") or parts[-1]
                m.setdefault(alias, parts)
        return maps

    def _fqn_resolve_node(
        self: "_Analyzer", node: Node, modmap: dict[str, list[str]],
        home: Optional[str] = None,
    ) -> None:
        if isinstance(node, Type):
            if home is not None:
                t_home = getattr(node, "_fqn_home", None)
                if t_home is None:
                    node._fqn_home = home  # type: ignore[attr-defined]
            self._fqn_resolve_type(node, modmap)
            return
        item_home = getattr(node, "source_module", None) or home
        skip: frozenset[str] = frozenset()
        if isinstance(node, (ImplDecl, ExtraDecl)):
            skip = frozenset({"struct"}) | (
                frozenset({"trait"}) if isinstance(node, ImplDecl) else frozenset()
            )
        elif isinstance(node, FnDecl):
            skip = frozenset({"cwind_owner"})
        for f in _fields(node):
            if f.name in ("line", "column") or f.name in skip:
                continue
            value = getattr(node, f.name)
            if isinstance(value, Node):
                self._fqn_resolve_node(value, modmap, item_home)
            elif isinstance(value, list):
                for v in value:
                    if isinstance(v, Node):
                        self._fqn_resolve_node(v, modmap, item_home)

    def _fqn_resolve_type(
        self: "_Analyzer", t: Type, modmap: dict[str, list[str]],
        home: Optional[str] = None,
    ) -> None:
        """Rewrite one qualified Type name to its canonical spelling."""
        if home is not None:
            t_home = getattr(t, "_fqn_home", None)
            if t_home is None:
                t._fqn_home = home  # type: ignore[attr-defined]
        for arg in t.args:
            self._fqn_resolve_type(arg, modmap, home)
        name = t.name
        if ("::" not in name
                or name.startswith(("fn(", "*const ", "*mut ", "["))
                or name.startswith("Self::")):
            return
        parts = name.split("::")
        # Reachability walk over the leading module chain.
        head = modmap.get(parts[0])
        if head is None:
            ns = self._mod_decl_namespace.get(parts[0])
            if ns is None:
                return  # not a module head: existing checkers diagnose it
        for seg in parts[1:-1]:
            if modmap.get(seg) is None \
                    and seg not in self._mod_decl_namespace:
                return  # broken chain: leave precise errors to pass 2
        member = parts[-1]
        resolved = member
        trait_fqn = getattr(self, "_fqn_trait_paths", {}).get(member)
        if trait_fqn is not None:
            # trait 引用的规范形 = 定义位置模块路径 + trait 名
            resolved = f"{trait_fqn}::{member}"
        else:
            qualified = _qualify_builtin(member)
            resolved = qualified if qualified is not None else member
        t.name = resolved
        # todo-154: 该引用经限定路径抵达 —— 裸名遮蔽门 (todo-79) 对它
        # 豁免, 限定寻址的可见性由模块面语义负责。
        t._fqn_path = True  # type: ignore[attr-defined]
        # todo-160 (--pass 0): record the path resolution hop — only when
        # the resolution actually changed the name (an already-canonical
        # ``std::builtins::X`` spelling is confirmed, not rewritten).
        if (
            resolved != name
            and getattr(self, "_fqn_report", None) is not None
        ):
            self._fqn_report.append({
                "kind": "path",
                "original": name,
                "canonical": resolved,
                "line": t.line,
                "column": t.column,
                "source": getattr(t, "_fqn_home", None),
            })

    def _fqn_subst_type(
        self: "_Analyzer", t: Type, subst: dict[str, Type],
        generics: frozenset[str]
    ) -> None:
        """Substitute generic parameters inside a Type node tree."""
        if t.name in subst:
            arg = subst[t.name]
            t.name = arg.name
            t.args = [copy.deepcopy(a) for a in arg.args]
            t.ref = arg.ref
            t.mut = arg.mut
            return
        for arg in t.args:
            self._fqn_subst_type(arg, subst, generics)
        if t.name.startswith("fn(") and t.args:
            self._fqn_subst_type(t.args[0], subst, generics)


def _iter_type_tree(t: "Type"):
    """Yield a Type node and every Type child under it (pre-order)."""
    yield t
    for arg in t.args:
        yield from _iter_type_tree(arg)


def run_pass0(program: Program) -> dict:
    """todo-160 (--pass 0): run **only** the FQN expansion pass.

    Lex -> parse -> pass 0 — no SA checks, no which hooks, nothing after
    the expansion.  Returns the structured expansion report (pure data:
    per-reference rewrite records + the alias table + trait exclusions);
    the CLI hands it to the renderer, which only lays it out.
    """
    analyzer = _Analyzer()
    # The minimal orchestration pass 0 depends on: inline mod namespaces
    # register into ``_mod_decl_namespace`` (used by path resolution) and
    # the parser's ``_module_table`` rides on the program itself.
    analyzer._register_inline_modules(program.items)
    file_programs = getattr(program, "_module_file_programs", None)
    if isinstance(file_programs, dict):
        analyzer._file_programs = file_programs
        for child in file_programs.values():
            analyzer._register_inline_modules(child.items)
    analyzer._fqn_expand(program)
    analyzer._fqn_resolve_paths(program)

    aliases = [
        {
            "name": name,
            "params": [p.name for p in decl.params],
            "target": _type_str_raw(decl.base),
            "source": getattr(decl, "source_module", None),
            "line": decl.line,
            "column": decl.column,
        }
        for name, decl in sorted(analyzer._fqn_aliases.items())
    ]

    # todo-160: traits expand like everything else — one row per trait
    # with its definition-site FQN (module path), so a same-named user
    # trait is distinguishable from a built-in type.  A trait flattened
    # once per importing file yields several TraitDecl instances; keep
    # the first spelling per name.
    traits: dict[str, dict] = {}
    for item in analyzer._fqn_all_items(program):
        if not isinstance(item, TraitDecl) or not item.name:
            continue
        if item.name in traits:
            continue
        def_path = getattr(item, "source_module_path", None)
        traits[item.name] = {
            "name": item.name,
            # 引用规范形: 定义位置模块路径 + trait 名 (与 Type 节点的
            # 存储形一致); stdin/内存源无路径时为裸名。
            "fqn": (
                f"{'::'.join(def_path)}::{item.name}"
                if def_path else item.name
            ),
            "source": getattr(item, "source_module", None),
            "line": item.line,
            "column": item.column,
        }

    # todo-160: methods in their expanded FQN form —
    # ``Owner::name`` with the owner run through the same alias-chain
    # expansion + builtin qualification as type references (definition
    # sites keep their bare registry spelling; this is the report view).
    methods: list[dict] = []

    def _owner_fqn(name: str, generics: frozenset[str]) -> str:
        if not name or name == "Self" or name in generics:
            return name
        probe = Type(name.line if False else 1, 1, name)
        saved = analyzer._fqn_report
        analyzer._fqn_report = None  # suppress expansion records
        try:
            analyzer._fqn_expand_type(probe, frozenset(generics),
                                      analyzer._fqn_aliases)
        finally:
            analyzer._fqn_report = saved
        return _type_str_raw(probe)

    for item in analyzer._fqn_all_items(program):
        if isinstance(item, ExternBlock) and item.abi == "CWind":
            for fn in item.fns:
                if fn.cwind_owner is None:
                    continue
                owner = fn.cwind_owner.name
                methods.append({
                    "owner": _owner_fqn(owner, frozenset()),
                    "name": fn.name,
                    "trait": None,
                    "source": getattr(item, "source_module", None),
                    "line": fn.line,
                    "column": fn.column,
                })
        elif isinstance(item, ImplDecl):
            generics = frozenset(p.name for p in item.params)
            owner = _owner_fqn(item.struct.name, generics)
            for m in item.methods:
                methods.append({
                    "owner": owner,
                    "name": m.name,
                    "trait": item.trait.name or None,
                    "source": getattr(item, "source_module", None),
                    "line": m.line,
                    "column": m.column,
                })
        elif isinstance(item, ExtraDecl):
            generics = frozenset(p.name for p in item.params)
            owner = _owner_fqn(item.struct.name, generics)
            for m in item.methods:
                methods.append({
                    "owner": owner,
                    "name": m.name,
                    "trait": None,
                    "source": getattr(item, "source_module", None),
                    "line": m.line,
                    "column": m.column,
                })

    return {
        "pass": {"id": 0, "name": "fqn-expansion"},
        "expansions": list(analyzer._fqn_report),
        "aliases": aliases,
        "methods": methods,
        "traits": sorted(traits.values(), key=lambda t: t["name"]),
    }

# run_pass0 needs the composed analyzer class; importing here (after
# FqnPass/_iter_type_tree are defined) keeps the analyzer<->fqn import
# cycle resolvable: sa/__init__ imports this module first, and
# analyzer.py's own `from .fqn import FqnPass, _iter_type_tree` then
# finds those names already present in this partially-initialized
# module.
from .analyzer import _Analyzer
