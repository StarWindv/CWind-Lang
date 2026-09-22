"""Post-SA DCE: dense renumbering of the surviving typed-id space."""

from __future__ import annotations

from dataclasses import dataclass, fields as _dc_fields
from typing import Any, Optional

from ...ast_components.ast import Node, Program
from ._common import _walk_nodes

__all__ = ["PruneResult", "renumber_typed_ids"]

_NODE_REF_BINDING_KINDS = (
    "fn", "const", "variant", "var", "extern_static", "field",
    "assoc_const", "struct", "enum",
)


@dataclass
class PruneResult:
    """Outcome of :func:`prune.prune_unreachable`."""

    renumber: dict[int, int]
    kept_objects: set[int]


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


def renumber_typed_ids(
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
