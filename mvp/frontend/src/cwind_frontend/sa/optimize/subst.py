"""Post-SA DCE: type-substitution, instantiation and alias helpers."""

from __future__ import annotations

from typing import Optional

from ...ast_components.ast import Node
from ..types import (
    _split_ref_prefix,
    _subst_type_str,
    _type_mentions,
)

__all__ = [
    "_add_instance",
    "_apply_outer",
    "_fn_params",
    "_is_conservative",
    "_resolve_receiver",
    "_strip_aliases",
    "_subst_sig",
]


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
    ann: object,
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
