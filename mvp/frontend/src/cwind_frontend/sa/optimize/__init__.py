"""SA optimization package: tree rewrites and the two-stage DCE pipeline.

Layout by functional role:

* :mod:`~cwind_frontend.sa.optimize.reassociation` — post-SA tree rewrite
  (reassociation / left-leaning chain); runs before the post-SA prune
  because it changes the call graph.  Matching lives in
  :mod:`~cwind_frontend.sa.optimize.match`, emission in
  :mod:`~cwind_frontend.sa.optimize.emit`.
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

from .prune import PruneResult, prune_unreachable
from .reassociation import optimize_reassociation
from .syntactic import prune_unreachable_syntactic

__all__ = [
    "PruneResult",
    "optimize_reassociation",
    "prune_unreachable",
    "prune_unreachable_syntactic",
]
