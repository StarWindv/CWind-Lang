"""Post-SA DCE: annotation / object-graph reference collection.

Scan ``_typed_ann`` (and Type-node canonical names) into structured
reference buckets consumed by the fixpoint in :mod:`.prune`.
"""

from __future__ import annotations

from typing import Any, Optional

from ...ast_components.ast import (
    Attribute,
    Call,
    Node,
    Type,
)
from ..types import _type_str_from_info
from ._common import _walk_nodes

__all__ = ["_Refs", "_collect_refs"]

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
