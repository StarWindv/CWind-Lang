"""Pre-SA DCE layer: syntactic reachability over the expanded flat program.

The prelude inlines the whole ``libs`` declaration surface into the
compile surface.  This layer runs after expansion/materialization
(macros expanded, bodies materialized, cfg applied, which hooks
registered), after the pass-1 registration and namespace hoist (so
``ProgramInfo.symbols`` / visibility tables keep their full-surface
semantics), and before pass 2/3 check.  It is purely **syntactic**: no
``_typed_id`` and no annotation is needed, so dependencies come from
source structure (identifiers, paths, type names, attribute member
names, ``which`` targets).  It prunes only the expensive *bodies* —
unreachable ``FnDecl`` and ``impl``/``extra`` blocks under a ``libs``
root — while every other declaration kind (use/mod/extern/trait/struct/
enum/type/const) is retained.  Retaining all exogenous declarations is
cheap (pass-2 signatures) and keeps resolution tables complete; the win
is pass-3 body checking of the unreachable std surface.

Pipeline (see :mod:`.syntactic_deps` for the vocabulary):

1. :func:`_seed` — classify items into prune candidates / worklist seeds
   / declaration-side reference surfaces (const values, field inits);
2. :func:`_resolve_kept` — worklist closure over syntactic names;
3. :func:`_apply` — drop unkept std bodies from ``program.items``.
"""

from __future__ import annotations

from ...ast_components.ast import (
    ConstDecl,
    ExtraDecl,
    FnDecl,
    ImplDecl,
    Node,
    Program,
    StructDecl,
    TraitDecl,
)
from ._common import _is_std
from .syntactic_deps import (
    _syntactic_conservative,
    _syntactic_provides,
    _syntactic_refs,
)

__all__ = ["prune_unreachable_syntactic"]

_PRUNE_KINDS = (FnDecl, ImplDecl, ExtraDecl)


def _seed(
    items: list[Node],
) -> tuple[list[Node], list[Node], set[str], set[int]]:
    """Split *items* into (candidates, pending, names, kept).

    * candidates — std pruneable bodies (fn / impl / extra);
    * pending — worklist roots (user items + traits + conservative std);
    * names — refs already visible from declaration-side surfaces;
    * kept — ``id()`` of conservative candidates (main / which / static).
    """
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
    return candidates, pending, names, kept


def _resolve_kept(
    candidates: list[Node],
    pending: list[Node],
    names: set[str],
    kept: set[int],
) -> None:
    """Worklist closure: scan refs, pull in provided candidates."""
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


def _apply(program: Program, items: list[Node], kept: set[int]) -> set[int]:
    """Drop unkept std pruneable bodies; return their ``id()`` set."""
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
    candidates, pending, names, kept = _seed(items)
    _resolve_kept(candidates, pending, names, kept)
    return _apply(program, items, kept)
