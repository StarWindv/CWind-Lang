"""Reachability pruning — two DCE layers over the compilation surface.

The prelude inlines the whole ``libs`` declaration surface into the
compile surface.  Historically only one prune existed (after SA, before
serialization); this module now provides the **two** layers the pipeline
contracts for:

* :func:`prune_unreachable_syntactic` — the *pre-SA* layer.  Runs after
  expansion/materialization (macros expanded, bodies materialized, cfg
  applied, which hooks registered), after the pass-1 registration and
  namespace hoist (so ``ProgramInfo.symbols`` / visibility tables keep
  their full-surface semantics), and before pass 2/3 check.  It is
  purely **syntactic**: no ``_typed_id`` and no annotation is needed, so
  dependencies come from source structure (identifiers, paths, type
  names, attribute member names, ``which`` targets).  It prunes only the
  expensive *bodies* — unreachable ``FnDecl`` and ``impl``/``extra``
  blocks under a ``libs`` root — while every other declaration kind
  (use/mod/extern/trait/struct/enum/type/const) is retained.  Retaining
  all exogenous declarations is cheap (pass-2 signatures) and keeps
  resolution tables complete; the win is pass-3 body checking of the
  unreachable std surface.

* :func:`prune_unreachable` — the *post-SA* layer at the existing
  serialization boundary.  It works on the **object graph** with full
  annotations and binding tables, so it can be much more precise.  It
  covers dependencies that only become visible after SA: trait/impl
  default methods (todo-194 clones), which hooks emitted at call sites,
  FFI declarations pulled by member-node ids, export adapters' types,
  and monomorphized generic instantiations.

Depencency rules (both layers) are **structural**: calls/types/impls/
traits/fields/macro expansion/attributes.  No module-name or function-
name whitelist exists.  Over-approximation is always allowed (an extra
item kept); under-approximation is a bug (a reachable item removed →
unknown symbol / dangling ref).  Every fallback below therefore widens
the kept set, never narrows it.

Post-SA propagation tracks:

* id track: bare ints in ``_typed_ann`` that hit a function node id
  (``callee_kind == "fn"`` callee_ref / binding.ref / extern static ref)
  → that function is reachable; a std ``ConstDecl`` node id (consts are
  referenced by name, the ref *is* the declaration node) → the constant
  is reachable; a struct-field node id / an extra associated-const node
  id → the owning declaration or block is reachable.
* name track (declarations only): ``{"name": ...}`` / ``alias`` values
  and Type-node canonical names hitting a Struct/Enum/Trait/Type/Const
  declaration name → that declaration is reachable.  The name track
  deliberately does **not** pull impl blocks (a referenced type is not a
  referenced method surface).
* binding track: SA method binding ids (``callee_kind == "method"``
  callee_ref, backend looks up ``x->id == bref``) → **that method's**
  ``FnDecl`` (not the whole host block): the host block survives only as
  a container.  Generic instantiations carried by the same call
  (``type_args``) are propagated into the method's substitution so
  nested bound dispatch can be resolved against the concrete receiver
  type.
* bound track (todo-166): ``callee_kind == "bound_method"`` annotations
  carry (visible bound trait, member) and the receiver's annotated type.
  With a concrete receiver type the host lookup is scoped to impls of
  that owner (matching the backend's ``owner + member`` first-wins
  dispatch); a hit marks the specific providing method, not the whole
  block.
* instantiation track (Defect A): ``type_args`` at a call site are
  recorded per **distinct concrete instantiation** and propagated into
  the callee body (``fn_inst`` / ``method_inst``).  One generic body
  called with several concrete types is scanned once per instantiation
  and the union of the concrete receivers is kept — never collapsed
  into a conflict/unknown map.  A generic user function never called is
  scanned once with an unknown context after the fixpoint (its body is
  still program surface).
* extern blocks: member fn/static node ids referenced by real FFI calls
  → **that member**; the block survives as a container with its
  ``#[link]`` metadata and only the referenced members.  Pure builtin
  dispatch (``callee_kind == "builtin"``, a string reference) does not
  ref member nodes, so builtin-only members die correctly.  Generic-
  looking extern "CWind" members (`type_of<T>` …) keep their member node
  regardless of ``type_args`` (rt dispatches by name).

Alias discipline (Defect B): ``ann.type.alias`` is display provenance,
not a live reference.  A **compiler-std** scalar typedef
(install-root ``libs``, e.g. ``u32`` / ``bool`` / ``c_int``) is only
kept when a real name/type position references it, and an alias key
whose declaration did not survive is stripped.  Typedefs owned by the
project (entry or package ``libs``) keep their alias provenance and are
kept alive by it (todo-144/146 contract).

Method-level discipline (todo-55 收尾):

* an impl/extra/extern block is a *container*: it is serialized only if
  at least one member (or associated const) is reachable (or it carries
  a which/static method, whose use cannot be decided);
* trait declarations stay (zero-body surface), but an unreferenced
  trait **default body** is dropped: the body that actually runs is the
  todo-194 clone registered on each reachable implementing type;
* bindings whose host block or method did not survive are pinned to the
  -1 sentinel so the serialized binding table never points at a pruned
  method (the backend rejects -1).

Conservative surface (always kept, both layers):

* every non-``libs`` item (user code is program surface even when
  unreachable);
* ``main`` / which hooks / static methods (their use cannot be decided
  statically);
* use/mod/extern/trait/struct/enum/type/const declarations (resolution
  contract, zero body cost).

ids are never rebuilt: removed nodes leave id holes, but the surviving
references are self-consistent by construction (only reachable nodes
survive), so serialized ref lookups never hit a hole.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields as _dc_fields
from pathlib import Path
from typing import Any, Optional, Sequence

from ..ast_components.ast import (
    Attribute,
    BindPattern,
    Call,
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
    Program,
    StructDecl,
    StructPatternField,
    TraitDecl,
    Type,
    TypeDecl,
    TypeParam,
    UseDecl,
    Variant,
)
from .types import (
    _base,
    _split_ref_prefix,
    _subst_type_str,
    _type_mentions,
    _type_str_from_info,
)

__all__ = ["prune_unreachable", "prune_unreachable_syntactic", "PruneResult"]

_DECL_KINDS = (StructDecl, EnumDecl, TraitDecl, TypeDecl, ConstDecl)
_BLOCK_KINDS = (ImplDecl, ExtraDecl, ExternBlock)
_PRUNE_KINDS = (FnDecl, ImplDecl, ExtraDecl)


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


# ---------------------------------------------------------------------------
# pre-SA layer: syntactic reachability over the expanded flat program
# ---------------------------------------------------------------------------

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


def prune_unreachable_syntactic(program: Program) -> set[int]:
    """Pre-SA reachability prune over the expanded flat program.

    Removes unreachable ``libs``-root function and impl/extra *bodies*
    from ``program.items``; every other item kind stays (see the module
    docstring).  Call it after expansion/materialization/desugar, the
    pass-1 registration (symbols/visible/module tables) and the
    namespace hoist, and before pass 2 — the retained registration
    surface keeps ``ProgramInfo.symbols`` and the visibility semantics
    intact, while pass 2/3 no longer check the pruned bodies.

    Returns the ``id()`` of every pruned item so the caller can skip
    stale registered-table entries (``self.functions`` /
    ``self.methods``) in later passes.
    """
    items: list[Node] = list(program.items)
    if not items:
        return set()

    candidates: list[Node] = []
    pending: list[Node] = []
    names: set[str] = set()
    kept: set[int] = set()
    for item in items:
        if not _is_std(item):
            # User program items are surface: keep and scan.
            pending.append(item)
            continue
        if not isinstance(item, _PRUNE_KINDS):
            # Declarations stay; scan only the surfaces that can call
            # into pruned bodies (const values, field initializers /
            # validations, trait default bodies).
            if isinstance(item, TraitDecl):
                pending.append(item)
            elif isinstance(item, ConstDecl):
                _syntactic_refs(item.value, names)
            elif isinstance(item, StructDecl):
                for f in item.fields or []:
                    if f.initializer is not None:
                        _syntactic_refs(f.initializer, names)
                    if f.validation is not None:
                        _syntactic_refs(f.validation, names)
            continue
        candidates.append(item)
        if _syntactic_conservative(item):
            # 保守项必须留在 kept: 它们只进 pending 扫描引用,
            # 不会因“被引用”而命中 (main 在生成的过程宏程序里被视为
            # libs 项, 早先漏加 kept 会被当死代码剪掉)。
            kept.add(id(item))
            pending.append(item)

    scanned: set[int] = set()
    while pending:
        item = pending.pop()
        if id(item) in scanned:
            continue
        scanned.add(id(item))
        _syntactic_refs(item, names)
        for cand in candidates:
            if id(cand) in kept:
                continue
            if _syntactic_provides(cand) & names:
                kept.add(id(cand))
                pending.append(cand)

    kept_items: list[Node] = []
    pruned_ids: set[int] = set()
    for item in items:
        if isinstance(item, _PRUNE_KINDS) and _is_std(item):
            if id(item) not in kept:
                pruned_ids.add(id(item))
                continue
        kept_items.append(item)
    program.items = kept_items
    return pruned_ids


# ---------------------------------------------------------------------------
# post-SA layer: object-graph reachability with annotations/binding tables
# ---------------------------------------------------------------------------

_SKIP_ANN_KEYS = (
    "line", "column", "def", "owner_def", "trait_def", "def_line",
    "def_column", "raw", "source", "fqn",
)


class _Refs:
    """Accumulator for one node's outgoing structural references."""

    __slots__ = (
        "ids", "names", "aliases", "binding_calls", "bound_sites",
        "fn_calls",
    )

    def __init__(self) -> None:
        self.ids: set[int] = set()
        self.names: set[str] = set()
        # typedef alias spellings (`ann.type.alias`): provenance, not a
        # live reference.  Only kept for *project* typedefs (see
        # ``propagate``); compiler-std scalar typedefs are weak.
        self.aliases: set[str] = set()
        self.binding_calls: list[tuple[int, Optional[dict]]] = []
        self.bound_sites: list[tuple[str, str, Optional[str]]] = []
        self.fn_calls: list[tuple[int, Optional[dict]]] = []


def _type_args_map(ann: dict) -> Optional[dict[str, Optional[str]]]:
    """``ann["type_args"]`` as plain type strings (None when absent)."""
    ta = ann.get("type_args")
    if not isinstance(ta, dict):
        return None
    out: dict[str, Optional[str]] = {}
    for name, value in ta.items():
        if not isinstance(name, str):
            continue
        if isinstance(value, dict):
            out[name] = _type_str_from_info(value)
        elif isinstance(value, str):
            out[name] = value
        else:
            out[name] = None
    return out or None


def _scan_ann(ann: Any, refs: _Refs) -> None:
    """按**键语义**分流 ann 里的裸 int —— 节点 id 与绑定 id 共享整数
    空间, 撞号时只看值会把 binding 当节点 (反之亦然):

    * ``{"callee_kind": "fn", "callee_ref": N}`` → 节点 id (函数声明),
      并携带 ``type_args`` 作为该函数的实例化约束;
    * ``{"callee_kind": "method", "callee_ref": N}`` / ``{"kind":
      "method", "ref": N}`` → **绑定 id** (后端按 bindings 表
      ``x->id == bref`` 查), 同样携带实例化约束;
    * 其余裸 int (``kind=="var"`` 的 binding.ref / decl_id ...) → 节点 id。
    ``{"name"/"alias": str}`` 收类型名候选。
    """
    if isinstance(ann, dict):
        kind = ann.get("callee_kind") or ann.get("kind")
        is_method = kind == "method"
        type_args = _type_args_map(ann)
        for key, value in ann.items():
            if key in _SKIP_ANN_KEYS:
                continue
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                if is_method and key in ("callee_ref", "ref"):
                    refs.binding_calls.append((value, type_args))
                else:
                    if kind == "fn" and key == "callee_ref":
                        refs.fn_calls.append((value, type_args))
                    refs.ids.add(value)
            elif isinstance(value, dict):
                nm = value.get("name")
                if isinstance(nm, str):
                    refs.names.add(nm.split("<", 1)[0])
                al = value.get("alias")
                if isinstance(al, str):
                    refs.aliases.add(al.split("<", 1)[0])
                _scan_ann(value, refs)
            else:
                _scan_ann(value, refs)
    elif isinstance(ann, list):
        for value in ann:
            _scan_ann(value, refs)


def _node_type_name(node: Optional[Node]) -> Optional[str]:
    if node is None:
        return None
    ann = getattr(node, "_typed_ann", None)
    if not isinstance(ann, dict):
        return None
    return _type_str_from_info(ann.get("type"))


def _collect_refs(
    node: Node,
    refs: _Refs,
    seen_bound: Optional[set[tuple]] = None,
) -> None:
    """Gather node-id refs, binding refs, instantiation constraints,
    bound-dispatch sites and type-name candidates from *node*'s subtree:
    every ``_typed_ann`` plus Type nodes' canonical names (pass 0 已把
    Type.name 规范化, impl 的 trait 名等从这里命中)。"""
    if seen_bound is None:
        seen_bound = set()

    def add_bound(trait: Any, member: Any, rt: Optional[str]) -> None:
        if not isinstance(member, str) or not member:
            return
        key = (
            trait if isinstance(trait, str) else None,
            member,
            rt,
        )
        if key in seen_bound:
            return
        seen_bound.add(key)
        refs.bound_sites.append((key[0] or "", member, rt))

    for n in _walk_nodes(node):
        ann = getattr(n, "_typed_ann", None)
        if isinstance(ann, dict):
            _scan_ann(ann, refs)
        if isinstance(n, Type):
            nm = getattr(n, "name", None)
            if isinstance(nm, str):
                refs.names.add(nm.split("<", 1)[0])
        if isinstance(n, Attribute):
            member = getattr(n, "_typed_ann", None)
            if isinstance(member, dict):
                m = member.get("member")
                if isinstance(m, dict) and m.get("kind") == "bound_method":
                    add_bound(m.get("trait"), m.get("member"),
                              _node_type_name(n.obj))
        if isinstance(n, Call):
            call = getattr(n, "_typed_ann", None)
            if isinstance(call, dict):
                c = call.get("call")
                if isinstance(c, dict) and c.get("callee_kind") == "bound_method":
                    ref = c.get("callee_ref")
                    trait = member = None
                    if isinstance(ref, dict):
                        trait = ref.get("trait")
                        member = ref.get("member")
                    rt = None
                    callee = n.callee
                    if isinstance(callee, Attribute):
                        rt = _node_type_name(callee.obj)
                    add_bound(trait, member, rt)


def _is_conservative(fn: Node, main_ids: set[int]) -> bool:
    """main / which 钩子 / static —— 用途不可静态判定, 一律保留。"""
    fid = fn._typed_id
    if fid is not None and fid in main_ids:
        return True
    if getattr(fn, "which", None) is not None:
        return True
    if getattr(fn, "static", False):
        return True
    return False


def _fn_params(fn: Node) -> frozenset[str]:
    return frozenset(
        p.name for p in getattr(fn, "type_params", None) or []
        if isinstance(getattr(p, "name", None), str)
    )


def _subst_sig(subst: dict[str, Optional[str]]) -> tuple:
    return tuple(sorted(subst.items()))


def _add_instance(
    store: dict[int, set[tuple]],
    key: int,
    incoming: Optional[dict[str, Optional[str]]],
) -> bool:
    """Record one *distinct* instantiation of ``key``.

    Multi-instantiation discipline (Defect A): a generic body called
    with several concrete type arguments is scanned once per
    instantiation — the union of the concrete receivers is kept, never
    collapsed into an unknown/conflict map that would fall back to all
    providers.  ``None``/unresolved values inside a map stay unknown for
    that context only.  Returns True when a new context was added.
    """
    if incoming is None:
        return False
    sig = _subst_sig(incoming)
    bucket = store.setdefault(key, set())
    if sig in bucket:
        return False
    bucket.add(sig)
    return True


def _strip_aliases(
    ann: Any,
    weak_typedefs: set[str],
    live_names: set[str],
) -> None:
    """Drop ``alias`` provenance whose declaration did not survive.

    Alias spellings are display provenance, not a reference: once pass 0
    has substituted the canonical type, an alias of a pruned
    **compiler-std** typedef must not linger (Defect B).  Project
    typedefs (their names are never in ``weak_typedefs``) keep their
    provenance contract untouched.
    """
    if isinstance(ann, dict):
        alias = ann.get("alias")
        if (
            isinstance(alias, str)
            and alias in weak_typedefs
            and alias not in live_names
        ):
            del ann["alias"]
        for value in ann.values():
            if isinstance(value, (dict, list)):
                _strip_aliases(value, weak_typedefs, live_names)
    elif isinstance(ann, list):
        for value in ann:
            _strip_aliases(value, weak_typedefs, live_names)


def _apply_outer(
    ta: Optional[dict[str, Optional[str]]],
    outer: dict[str, Optional[str]],
) -> Optional[dict[str, Optional[str]]]:
    """Instantiation map of a callee, with the caller's substitutions
    applied to each value (chained generics)."""
    if not ta:
        return None
    if not outer:
        return dict(ta)
    known = {k: v for k, v in outer.items() if v}
    conflicts = [k for k, v in outer.items() if v is None]
    out: dict[str, Optional[str]] = {}
    for name, value in ta.items():
        if value is None:
            out[name] = None
            continue
        s = _subst_type_str(value, known) if known else value
        if any(_type_mentions(s, c) for c in conflicts):
            out[name] = None
        else:
            out[name] = s
    return out


def _resolve_receiver(
    rt: Optional[str],
    subst: dict[str, Optional[str]],
    params: frozenset[str],
) -> Optional[str]:
    """Concrete receiver type string, or None when still generic."""
    if not rt:
        return None
    t = rt
    for prefix in ("*const ", "*mut "):
        if t.startswith(prefix):
            t = t[len(prefix):]
            break
    _, t = _split_ref_prefix(t)
    known = {k: v for k, v in subst.items() if v}
    if known:
        t = _subst_type_str(t, known)
    if not t:
        return None
    for name in params:
        if name and _type_mentions(t, name):
            return None
    for name, value in subst.items():
        if value is None and _type_mentions(t, name):
            return None
    return t


_NODE_REF_BINDING_KINDS = (
    "fn", "const", "variant", "var", "extern_static", "field",
    "assoc_const", "struct", "enum",
)


def _remap_ann_ids(ann: Any, mapping: dict[int, int]) -> None:
    """Rewrite node-id references of an annotation in place.

    Only the annotation shapes that actually carry a **node** id are
    touched (``call.callee_ref`` with ``callee_kind == "fn"`` and
    ``binding.ref`` with a node-binding kind); binding ids and auxiliary
    integers (``tuple_index`` / ``variant_index`` / folded literals) keep
    their values.  A node ref that did not survive pruning becomes the
    -1 sentinel so renumbering cannot silently rebind it to an unrelated
    kept node (the backend rejects -1).
    """
    if isinstance(ann, dict):
        slot: Optional[str] = None
        if ann.get("callee_kind") == "fn":
            slot = "callee_ref"
        elif ann.get("kind") in _NODE_REF_BINDING_KINDS:
            slot = "ref"
        if slot is not None:
            value = ann.get(slot)
            if isinstance(value, int) and not isinstance(value, bool):
                new = mapping.get(value)
                ann[slot] = new if new is not None else -1
        for value in ann.values():
            if isinstance(value, (dict, list)):
                _remap_ann_ids(value, mapping)
    elif isinstance(ann, list):
        for value in ann:
            _remap_ann_ids(value, mapping)


def _renumber_typed_ids(
    program: Program,
) -> tuple[dict[int, int], set[int]]:
    """Dense pre-order renumbering of the surviving node graph.

    Pruning leaves holes in the id space (ids are assigned before
    reachability is known); serializing sparse ids keeps the typed-AST
    document's id-space contract (id ↔ node pool, bounded sparsity) from
    holding.  The walk assigns 1..N pre-order, then rewrites every node
    annotation reference through the returned ``old -> new`` map (the
    caller remaps symbols; bindings are built after pruning).  The
    second return value carries the ``id()`` of every surviving node so
    the caller can tell live methods/blocks apart from pruned ones.
    """
    mapping: dict[int, int] = {}
    kept_objects: set[int] = set()
    counter = 0

    def assign(node: Node) -> None:
        nonlocal counter
        counter += 1
        kept_objects.add(id(node))
        old = getattr(node, "_typed_id", None)
        if isinstance(old, int):
            mapping[old] = counter
        node._typed_id = counter
        for f in _dc_fields(node):
            if f.name in ("line", "column"):
                continue
            value = getattr(node, f.name, None)
            if isinstance(value, Node):
                assign(value)
            elif isinstance(value, list):
                for v in value:
                    if isinstance(v, Node):
                        assign(v)

    assign(program)
    for item in program.items:
        for node in _walk_nodes(item):
            ann = getattr(node, "_typed_ann", None)
            if isinstance(ann, dict):
                _remap_ann_ids(ann, mapping)
    return mapping, kept_objects


@dataclass
class PruneResult:
    """Outcome of :func:`prune_unreachable`."""

    renumber: dict[int, int]
    kept_objects: set[int]


def prune_unreachable(
    program: Program,
    main_fns: Sequence[Node],
    bindings: Sequence[Any],
) -> PruneResult:
    """从根出发的可达性闭包; 不可达的 std 声明/成员从 items 摘除。

    ``main_fns`` 是 SA 符号表中的 main 声明节点 (一般恰一个, 无根库
    可以为空 —— 用户项全数保留仍是纪律)。
    ``bindings`` 是 SA 的 MethodBinding 序列 (``_binding_order`` 的
    绑定对象): binding id 命中即保该 **方法**, 宿主 impl/extra/extern
    块只作为容器存活 (无被引成员即摘); ``type_args`` 实例化约束下传
    到被调方法体解析 bound 分派, 以及泛型函数 (fn_subst)。

    ``program._module_file_programs`` 的 per-file 视图共享同一批
    item 对象, 摘除/裁剪后序列化面自然缩小。

    返回 :class:`PruneResult`: 存活节点的 ``old -> new`` 重编号映射
    (symbols ref 由调用方改写) 与存活节点 ``id()`` 集合 (bindings
    的 decl/fn 活跃性判定)。
    """
    items: list[Node] = list(program.items)
    if not items:
        return PruneResult({}, set())

    main_ids: set[int] = set()
    for fn in main_fns:
        fid = fn._typed_id
        if fid is not None:
            main_ids.add(fid)

    # 编译器自带 libs (install root) 的标量 typedef 只是 pass 0 展开前的
    # 拼写糖: 其 alias 是纯展示性 provenance, 不构成存活引用 (Defect B)。
    # 项目 libs/入口的 typedef 仍由 alias 维持存活 (todo-144/146 契约)。
    compiler_libs: Optional[Path] = None
    try:
        from ..home import install_root
        root = install_root()
        if root is not None:
            compiler_libs = (root / "libs").resolve()
    except Exception:  # pragma: no cover - discovery is best-effort
        compiler_libs = None

    def is_compiler_std(item: Node) -> bool:
        if compiler_libs is None:
            return False
        src = getattr(item, "source_module", None)
        if not isinstance(src, str):
            return False
        try:
            return Path(src).resolve().is_relative_to(compiler_libs)
        except (OSError, ValueError):  # pragma: no cover - defensive
            return False

    # ---- 候选索引 (仅 std 项参与削减) ----
    # fn_by_id covers user + std declarations: user generic functions are
    # reached through call-site instantiation and must be rescanable per
    # context (they are never pruning candidates themselves).
    fn_by_id: dict[int, Node] = {
        item._typed_id: item
        for item in items
        if isinstance(item, FnDecl) and item._typed_id is not None
    }
    top_fn_by_id: dict[int, Node] = {}      # 顶层 FnDecl (std, 参与削减)
    extern_member: dict[int, Node] = {}     # extern 成员 id → 宿主块
    extern_member_node: dict[int, Node] = {}  # extern 成员 id → 成员节点
    blocks: dict[int, Node] = {}            # impl/extra/extern 块容器
    decls_by_id: dict[int, Node] = {}       # Struct/Enum/Trait/Type/Const
    decls_by_name: dict[str, Node] = {}
    field_owner: dict[int, Node] = {}
    extra_consts: dict[int, tuple[Node, Node]] = {}  # const id → (宿主块, const)
    weak_typedefs: set[str] = set()         # compiler-std typedef 名
    for item in items:
        if not _is_std(item):
            continue
        fid = item._typed_id
        if isinstance(item, FnDecl):
            if fid is not None:
                top_fn_by_id[fid] = item
        elif isinstance(item, _BLOCK_KINDS):
            if fid is not None:
                blocks[fid] = item
            if isinstance(item, ExternBlock):
                for m in (item.fns or []) + (item.statics or []):
                    mid = getattr(m, "_typed_id", None)
                    if isinstance(m, Node) and mid is not None:
                        extern_member[mid] = item
                        extern_member_node[mid] = m
            elif isinstance(item, ExtraDecl):
                # todo-122: 关联 const 引用 (ann kind == assoc_const)
                # 携带的是 const 节点 id, 命中即保该 const 与宿主容器
                # (后端 cg_extra_const 按 ExtraDecl 查 const)。
                for c in item.consts or []:
                    mid = getattr(c, "_typed_id", None)
                    if isinstance(c, Node) and mid is not None:
                        extra_consts[mid] = (item, c)
        elif isinstance(item, _DECL_KINDS):
            if fid is not None:
                decls_by_id[fid] = item
            nm = getattr(item, "name", None)
            if isinstance(nm, str) and nm:
                decls_by_name[nm] = item
                if isinstance(item, TypeDecl) and is_compiler_std(item):
                    weak_typedefs.add(nm)
            if isinstance(item, StructDecl):
                for f in item.fields or []:
                    mid = getattr(f, "_typed_id", None)
                    if mid is not None:
                        field_owner[mid] = item

    # ---- binding 索引 ----
    binding_by_id: dict[int, Any] = {}
    member_bindings: dict[str, list[Any]] = {}
    owner_bindings: dict[str, dict[str, list[Any]]] = {}
    for b in bindings:
        bid = getattr(b, "id", None)
        decl = getattr(b, "decl", None)
        bfn = getattr(b, "fn", None)
        if bid is None:
            continue
        binding_by_id[bid] = b
        if (
            not isinstance(decl, _BLOCK_KINDS)
            or not isinstance(bfn, Node)
            or decl._typed_id not in blocks
        ):
            continue
        mname = getattr(bfn, "name", None)
        if not isinstance(mname, str) or not mname:
            continue
        member_bindings.setdefault(mname, []).append(b)
        owner = getattr(b, "owner_struct", None)
        obase: Optional[str] = None
        if isinstance(owner, Type) and isinstance(owner.name, str):
            obase = _base(owner.name)
        else:
            oname = getattr(b, "owner", None)
            if isinstance(oname, str):
                obase = _base(oname)
        if obase:
            bucket = owner_bindings.setdefault(obase, {}).setdefault(mname, [])
            if b not in bucket:
                bucket.append(b)

    # ---- 不动点闭包 ----
    reach_ids: set[int] = set()        # 顶层 fn / extern 成员节点 id
    reach_methods: set[int] = set()    # id(方法 FnDecl) —— 方法级可达面
    reach_consts: set[int] = set()     # id(extra ConstDecl)
    reach_decls: set[int] = set()      # id(声明项: 类型/trait/const)
    reach_blocks: set[int] = set()     # id(块容器, 有获批成员即立)
    reach_trait_members: set[tuple[str, str]] = set()
    queue: list[Any] = []  # (fn id, 实例签名) / int (extern 成员) / item 节点
    fn_inst: dict[int, set[tuple]] = {}      # 泛型函数 → 实例化签名集
    method_inst: dict[int, set[tuple]] = {}  # 泛型方法 → 实例化签名集
    scanned_fn: set[tuple] = set()
    scanned_item: set[tuple] = set()

    def mark_method(
        block: Node,
        method: Node,
        inst: Optional[dict[str, Optional[str]]] = None,
    ) -> None:
        """Mark one block member reachable; the block stays a container.

        Extern members are keyed by **node id** (FFI call annotations carry
        the member's node ref), impl/extra methods by object identity (the
        binding id is the call-site ref, not the method node).  ``inst``
        records one concrete instantiation context (method-level Defect A
        propagation).
        """
        fresh = id(method) not in reach_methods
        reach_methods.add(id(method))
        reach_blocks.add(id(block))
        mid = getattr(method, "_typed_id", None)
        if isinstance(block, ExternBlock) and mid is not None:
            reach_ids.add(mid)
        if mid is not None and _add_instance(method_inst, mid, inst):
            queue.append(block)
            return
        if fresh:
            queue.append(block)

    def mark_binding(b: Any, subst: Optional[dict] = None) -> None:
        decl = getattr(b, "decl", None)
        bfn = getattr(b, "fn", None)
        if not isinstance(decl, _BLOCK_KINDS) or not isinstance(bfn, Node):
            return
        t = getattr(b, "trait", None)
        mname = getattr(bfn, "name", None)
        if isinstance(t, str) and t and isinstance(mname, str) and mname:
            reach_trait_members.add((t, mname))
        mark_method(decl, bfn, subst)

    def mark_decl(d: Node) -> None:
        if id(d) not in reach_decls:
            reach_decls.add(id(d))
            queue.append(d)

    def mark_whole_block(block: Node) -> None:
        """Direct block-id reference (rare catch-all): keep all members."""
        if isinstance(block, ExternBlock):
            for m in (block.fns or []) + (block.statics or []):
                if isinstance(m, Node):
                    mid = getattr(m, "_typed_id", None)
                    if mid is not None:
                        reach_ids.add(mid)
                    reach_methods.add(id(m))
        else:
            for m in getattr(block, "methods", None) or []:
                if isinstance(m, Node):
                    mark_method(block, m)
        if isinstance(block, ExtraDecl):
            for c in block.consts or []:
                if isinstance(c, Node):
                    reach_consts.add(id(c))
        reach_blocks.add(id(block))
        queue.append(block)

    def push_id(i: int) -> None:
        if i in fn_by_id:
            # Unknown-context reference (function value etc.): the
            # dequeue guard skips it when concrete instantiations exist.
            queue.append((i, ()))
        elif i in extern_member:
            queue.append(i)
        elif i in field_owner:
            mark_decl(field_owner[i])
        elif i in extra_consts:
            blk, const = extra_consts[i]
            if id(const) not in reach_consts:
                reach_consts.add(id(const))
                queue.append(blk)
            reach_blocks.add(id(blk))
        elif i in decls_by_id:
            mark_decl(decls_by_id[i])
        elif i in blocks:
            mark_whole_block(blocks[i])

    def push_names(names: set[str]) -> None:
        for nm in names:
            d = decls_by_name.get(nm)
            if d is not None:
                mark_decl(d)

    def propagate(
        refs: _Refs,
        subst: dict[str, Optional[str]],
        params: frozenset[str],
    ) -> None:
        # Call targets are handled below with their instantiation map:
        # do not also scan them with an unknown/empty context, which
        # would fall back to every provider and defeat the
        # per-instantiation convergence (Defect A).  Extern "CWind"
        # builtins are *not* generic bodies — their member node must be
        # kept whatever type_args they carry (rt dispatches by name).
        call_targets = {
            fid for fid, ta in refs.fn_calls if ta and fid in fn_by_id
        }
        for i in refs.ids:
            if i in call_targets:
                continue
            if i not in reach_ids:
                push_id(i)
        for bid, ta in refs.binding_calls:
            b = binding_by_id.get(bid)
            if b is not None:
                mark_binding(b, _apply_outer(ta, subst) if ta else None)
        for trait, member, rt in refs.bound_sites:
            if trait:
                reach_trait_members.add((trait, member))
            concrete = _resolve_receiver(rt, subst, params)
            target: Optional[list[Any]] = None
            if concrete:
                target = owner_bindings.get(_base(concrete), {}).get(member)
            if not target:
                # Unresolved receiver (still generic / opaque) or no host
                # registered for the owner: keep every provider of the
                # member.  Never narrower than the backend's lookup.
                target = member_bindings.get(member, [])
            for b in target:
                mark_binding(b)
        for fid, ta in refs.fn_calls:
            if fid in extern_member:
                # extern "CWind"/"C" member (may be generic-looking):
                # keep the member node itself, no instance context.
                if fid not in reach_ids:
                    push_id(fid)
                continue
            if ta:
                inst = _apply_outer(ta, subst)
                if _add_instance(fn_inst, fid, inst):
                    queue.append((fid, _subst_sig(inst or {})))
            elif fid not in reach_ids:
                push_id(fid)
        push_names(refs.names)
        # Alias spellings: provenance, not a live reference.  A compiler
        # std scalar typedef is only kept when a *real* name/type position
        # references it; project typedefs stay alive through their alias
        # (todo-144/146 provenance contract).
        for alias in refs.aliases:
            d = decls_by_name.get(alias)
            if d is None or alias in weak_typedefs:
                continue
            mark_decl(d)

    def scan_fn(fid: int, subst: dict[str, Optional[str]]) -> None:
        fn = fn_by_id.get(fid)
        if fn is None:
            return
        refs = _Refs()
        _collect_refs(fn, refs)
        propagate(refs, subst, _fn_params(fn))

    def member_reachable(m: Node, block: Node) -> bool:
        """Whether *m* is an approved member of *block* (user blocks keep
        everything; std blocks are filtered by the member-level sets)."""
        if not _is_std(block):
            return True
        if isinstance(block, ExternBlock):
            return getattr(m, "_typed_id", None) in reach_ids
        return id(m) in reach_methods

    def trait_method_kept(trait: TraitDecl, method: Node) -> bool:
        """Signature methods stay; a default body stays only when its
        (trait, member) surface or the node itself is referenced.  The
        body that actually runs is the todo-194 clone on the impl."""
        if getattr(method, "body", None) is None:
            return True
        mid = getattr(method, "_typed_id", None)
        if mid is not None and mid in reach_ids:
            return True
        tname = getattr(trait, "name", None)
        mname = getattr(method, "name", None)
        if not isinstance(tname, str) or not isinstance(mname, str):
            return False
        tbase = _base(tname)
        return any(
            m == mname and _base(t) == tbase
            for t, m in reach_trait_members
        )

    def scan_item(item: Node) -> None:
        block_params = frozenset(
            p.name for p in getattr(item, "params", None) or []
            if isinstance(getattr(p, "name", None), str)
        )
        if isinstance(item, (ImplDecl, ExtraDecl)):
            head = _Refs()
            for t in (getattr(item, "struct", None), getattr(item, "trait", None)):
                if isinstance(t, Type):
                    _collect_refs(t, head)
            propagate(head, {}, block_params)
            for m in getattr(item, "methods", None) or []:
                if not isinstance(m, Node) or not member_reachable(m, item):
                    continue
                mid = m._typed_id
                mrefs = _Refs()
                _collect_refs(m, mrefs)
                mparams = block_params | _fn_params(m)
                insts = method_inst.get(mid) if mid is not None else None
                if insts:
                    for sig in sorted(insts):
                        propagate(mrefs, dict(sig), mparams)
                else:
                    propagate(mrefs, {}, mparams)
            for c in getattr(item, "consts", None) or []:
                if not isinstance(c, Node):
                    continue
                if _is_std(item) and id(c) not in reach_consts:
                    continue
                cref = _Refs()
                _collect_refs(c, cref)
                propagate(cref, {}, block_params)
        elif isinstance(item, ExternBlock):
            refs = _Refs()
            for m in (item.fns or []) + (item.statics or []):
                if not isinstance(m, Node) or not member_reachable(m, item):
                    continue
                _collect_refs(m, refs)
            propagate(refs, {}, frozenset())
        elif isinstance(item, TraitDecl):
            # trait 声明体不传播引用: 后端不发射 trait 默认方法
            # (调用经实现者方法表的克隆分派), 其签名/默认体里的
            # 名字与绑定引用会令削减全家桶回潮
            pass
        else:
            # 声明项: 字段/载荷/初值引用继续扩散
            refs = _Refs()
            _collect_refs(item, refs)
            propagate(refs, {}, _fn_params(item) if isinstance(item, FnDecl) else frozenset())

    def item_signature(item: Node) -> tuple:
        if isinstance(item, (ImplDecl, ExtraDecl)):
            is_std = _is_std(item)
            parts: list[Any] = []
            for m in getattr(item, "methods", None) or []:
                if is_std and id(m) not in reach_methods:
                    continue
                mid: Optional[int] = getattr(m, "_typed_id", None)
                insts = method_inst.get(mid, set()) if mid is not None else set()
                parts.append((mid, tuple(sorted(insts))))
            for c in getattr(item, "consts", None) or []:
                if is_std and id(c) not in reach_consts:
                    continue
                parts.append((getattr(c, "_typed_id", None), "const"))
            return tuple(parts)
        if isinstance(item, ExternBlock):
            return tuple(
                getattr(m, "_typed_id", None)
                for m in (item.fns or []) + (item.statics or [])
                if getattr(m, "_typed_id", None) in reach_ids
            )
        return ()

    # 种子: 用户项的全部引用 + std 保守函数/方法 (main/which/static)
    # 的引用 —— 用户面引用的 std 声明从这里被拉活。泛型用户函数推迟
    # 到实例化已知后按上下文扫描; 从未被调用的在 fixpoint 尾部以未知
    # 上下文兜底扫描 (保守)。
    deferred_fns: list[int] = []
    for item in items:
        if not _is_std(item):
            if isinstance(item, FnDecl) and getattr(item, "type_params", None):
                fid = item._typed_id
                if fid is not None:
                    deferred_fns.append(fid)
                continue
            refs = _Refs()
            _collect_refs(item, refs)
            propagate(refs, {}, frozenset())
            continue
        if isinstance(item, FnDecl):
            if _is_conservative(item, main_ids):
                refs2 = _Refs()
                _collect_refs(item, refs2)
                propagate(refs2, {}, _fn_params(item))
        elif isinstance(item, (ImplDecl, ExtraDecl)):
            block_params = frozenset(
                p.name for p in getattr(item, "params", None) or []
                if isinstance(getattr(p, "name", None), str)
            )
            for m in getattr(item, "methods", None) or []:
                if isinstance(m, Node) and _is_conservative(m, main_ids):
                    mark_method(item, m)
                    refs3 = _Refs()
                    _collect_refs(m, refs3)
                    propagate(refs3, {}, block_params | _fn_params(m))

    while queue or deferred_fns:
        if not queue:
            # Fixpoint drained: give still-uninstantiated generic user
            # functions their conservative unknown-context scan.  Ones
            # that did get a concrete context are skipped by the dequeuer.
            for fid in deferred_fns:
                queue.append((fid, ()))
            deferred_fns = []
        cur = queue.pop()
        if isinstance(cur, tuple):
            fid, sig = cur
            if fid not in fn_by_id:
                continue
            if (fid, sig) in scanned_fn:
                continue
            insts = fn_inst.get(fid)
            if sig == () and insts and () not in insts:
                # Concrete instantiations exist; the unknown-context scan
                # is subsumed and would only widen the kept set.
                continue
            scanned_fn.add((fid, sig))
            reach_ids.add(fid)
            scan_fn(fid, dict(sig))
            continue
        if isinstance(cur, int):
            if cur in extern_member:
                reach_ids.add(cur)
                member = extern_member_node.get(cur)
                host = extern_member[cur]
                if isinstance(member, Node):
                    mark_method(host, member)
                continue
            continue
        bid = cur._typed_id
        sig = item_signature(cur)
        key = (bid, sig)
        if key in scanned_item:
            continue
        scanned_item.add(key)
        scan_item(cur)

    # ---- 摘除 (用户项全保; std 块与方法逐个裁) ----
    kept: list[Node] = []
    for item in items:
        if not _is_std(item):
            kept.append(item)
            continue
        if isinstance(item, FnDecl):
            fid = item._typed_id
            if (
                fid is not None
                and fid not in reach_ids
                and not _is_conservative(item, main_ids)
            ):
                continue
            kept.append(item)
            continue
        if isinstance(item, (ImplDecl, ExtraDecl)):
            has_conservative = any(
                isinstance(m, Node) and _is_conservative(m, main_ids)
                for m in (getattr(item, "methods", None) or [])
            )
            if id(item) not in reach_blocks and not has_conservative:
                continue
            item.methods = [
                m for m in (getattr(item, "methods", None) or [])
                if id(m) in reach_methods
            ]
            if isinstance(item, ExtraDecl):
                item.consts = [
                    c for c in (getattr(item, "consts", None) or [])
                    if id(c) in reach_consts
                ]
                if not item.methods and not item.consts:
                    continue
            elif not item.methods:
                continue
            kept.append(item)
            continue
        if isinstance(item, ExternBlock):
            if id(item) not in reach_blocks:
                continue
            item.fns = [
                m for m in (item.fns or [])
                if getattr(m, "_typed_id", None) in reach_ids
            ]
            item.statics = [
                m for m in (item.statics or [])
                if getattr(m, "_typed_id", None) in reach_ids
            ]
            if not item.fns and not item.statics and not item.types:
                continue
            kept.append(item)
            continue
        if isinstance(item, TraitDecl):
            if id(item) not in reach_decls:
                continue
            item.methods = [
                m for m in (getattr(item, "methods", None) or [])
                if trait_method_kept(item, m)
            ]
            kept.append(item)
            continue
        if isinstance(item, _DECL_KINDS):
            if id(item) not in reach_decls:
                continue
            kept.append(item)
            continue
        kept.append(item)
    program.items = kept
    # Defect B: alias provenance whose declaration did not survive is
    # dropped (compiler-std scalar typedefs pruned above).  Project
    # typedefs survive through their alias, so their keys stay.
    live_names: set[str] = set()
    for item in kept:
        nm = getattr(item, "name", None)
        if isinstance(nm, str) and nm:
            live_names.add(nm)
    for item in kept:
        for node in _walk_nodes(item):
            ann = getattr(node, "_typed_ann", None)
            if isinstance(ann, dict):
                _strip_aliases(ann, weak_typedefs, live_names)
    renumber, kept_objects = _renumber_typed_ids(program)
    return PruneResult(renumber, kept_objects)
