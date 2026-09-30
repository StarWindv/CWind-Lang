"""SA optimization package: tree rewrites and the two-stage DCE pipeline.

Layout by functional role:

* :mod:`~cwind_frontend.sa.optimize.reassociation` — post-SA tree rewrite
  (reassociation / left-leaning chain); runs before the post-SA prune
  because it changes the call graph.  Matching lives in
  :mod:`~cwind_frontend.sa.optimize.match`, emission in
  :mod:`~cwind_frontend.sa.optimize.emit`.
* :mod:`~cwind_frontend.sa.optimize.inline_loop` — ``#[opt(inline_loop)]``
  hand-rolled recursive inlining (nests the accumulator loop N deep).
  Runs *before* reassociation, which would otherwise flatten the same
  shape first.
* :mod:`~cwind_frontend.sa.optimize.syntactic` — *pre-SA* DCE layer:
  purely syntactic reachability over the expanded flat program
  (helpers in :mod:`~cwind_frontend.sa.optimize.syntactic_deps`).
* :mod:`~cwind_frontend.sa.optimize.prune` — *post-SA* DCE layer:
  object-graph reachability fixpoint at the serialization boundary
  (annotation scan in ``refs``, substitution in ``subst``, dense id
  rewrite in ``renumber``).
* :mod:`~cwind_frontend.sa.optimize._common` — shared AST walk helpers.

Process-macro dependency DCE lives under ``macros.proc`` and is out of
scope here.
"""

from __future__ import annotations

from .inline_const import inline_consts
from .inline_loop import inline_loop_functions
from .prune import PruneResult, prune_unreachable
from .reassociation import optimize_reassociation
from .syntactic import prune_unreachable_syntactic

__all__ = [
    "PruneResult",
    "inline_consts",
    "inline_loop_functions",
    "optimize_reassociation",
    "prune_unreachable",
    "prune_unreachable_syntactic",
]
