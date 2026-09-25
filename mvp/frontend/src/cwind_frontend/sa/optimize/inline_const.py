"""Post-SA const inlining: flatten const values onto their use sites.

Runs after every SA check (type checks, folding, refinement, hook
emission, reassociation) and before the post-SA prune.  Each read of a
top-level or associated const — bare name, ``mod::CONST`` or
``S::CONST`` — is replaced by a deep clone of its initializer
expression, and every ``ConstDecl`` is then dropped from the program:

* the backend no longer materialises ``cwind.const.*`` globals: the
  read becomes an ordinary literal / constructor expression at the use
  site (值内联, 平铺到调用点);
* an unused const keeps no footprint at all — DCE falls out of the
  rewrite instead of needing a separate reachability rule;
* clones follow the hook-clone discipline (``_reset_hook_ids`` + fresh
  synthetic ids), so the typed-AST node pool keeps one parent per id.

The index :func:`collect_const_decls` builds matches the backend's own
resolution surface (serialized item pool), so a reference it cannot
resolve today would already be broken — such names are left untouched.

References inside a declaration cycle are left in place as well: the
cycle has already been reported by ``_check_const_cycles``, the
compilation fails, and no document reaches the backend.
"""

from __future__ import annotations

import copy
from dataclasses import fields as _dc_fields
from typing import TYPE_CHECKING, Optional

from ...ast_components.ast import ConstDecl, Name, Node, Program
from ..const_check import collect_const_decls
from ..desugar import DesugarPass

if TYPE_CHECKING:
    from ..analyzer import _Analyzer

__all__ = ["inline_consts"]

# Binding kinds whose value comes from a ConstDecl in the index.
_INLINABLE_BINDINGS = frozenset({"const", "assoc_const"})


def inline_consts(analyzer: "_Analyzer", program: Program) -> None:
    """Replace const reads with cloned initializers; drop the decls.

    Operates in place on *program* (items list and every nested block).
    """
    decls = collect_const_decls(program)
    if not decls:
        return
    _Inliner(analyzer, decls).rewrite(program)


class _Inliner:
    def __init__(self, analyzer: "_Analyzer", decls: dict[int, ConstDecl]) -> None:
        self.analyzer = analyzer
        self.decls = decls
        # Declarations whose initializer is currently being cloned —
        # the guard that keeps a cycle from recursing forever (the cycle
        # itself is reported separately by the SA check).
        self.active: set[int] = set()
        # One replacement per source Name object: ``args`` and the
        # ``named_args`` tuples share their value nodes, so both views
        # must receive the *same* clone (the tuple keeps a reference to
        # the source object, which also pins its id against reuse).
        self.memo: dict[int, tuple[Node, Node]] = {}

    def rewrite(self, node: Node) -> Optional[Node]:
        """Return the replacement for *node*, or None when it is dropped."""
        if isinstance(node, ConstDecl):
            # Declarations vanish wholesale: reads were (or will be)
            # rewritten where they occur, unused ones simply disappear.
            return None
        if isinstance(node, Name):
            replaced = self._materialize(node)
            if replaced is not None:
                return replaced
        self._rewrite_children(node)
        return node

    def _materialize(self, name: Name) -> Optional[Node]:
        binding = name._typed_ann.get("binding")
        if not isinstance(binding, dict):
            return None
        if binding.get("kind") not in _INLINABLE_BINDINGS:
            return None
        cached = self.memo.get(id(name))
        if cached is not None:
            return cached[1]
        decl = self.decls.get(binding.get("ref"))  # type: ignore[arg-type]
        if decl is None or id(decl) in self.active:
            return None
        self.active.add(id(decl))
        try:
            clone = copy.deepcopy(decl.value)
            # Hook-clone discipline: the clone carries the source ids,
            # which would collide with the (about to be dropped) decl and
            # with every sibling clone.  Reset, rewrite nested const
            # reads, then stamp fresh synthetic ids over the subtree.
            DesugarPass._reset_hook_ids(clone)
            out = self.rewrite(clone)
            if out is None:
                return None
            self.analyzer._assign_synthetic_ids(out)
            self.memo[id(name)] = (name, out)
            return out
        finally:
            self.active.discard(id(decl))

    def _rewrite_children(self, node: Node) -> None:
        for f in _dc_fields(node):
            if f.name in ("line", "column"):
                continue
            value = getattr(node, f.name, None)
            if isinstance(value, Node):
                replaced = self.rewrite(value)
                if replaced is None:
                    # ConstDecl only ever lives in lists; a required
                    # single-node field keeps its value defensively.
                    continue
                if replaced is not value:
                    setattr(node, f.name, replaced)
            elif isinstance(value, list):
                self._rewrite_list(value)

    def _rewrite_list(self, seq: list) -> None:
        out: list = []
        changed = False
        for element in seq:
            if isinstance(element, Node):
                replaced = self.rewrite(element)
                if replaced is None:
                    changed = True
                    continue
                if replaced is not element:
                    changed = True
                out.append(replaced)
            elif isinstance(element, tuple):
                # named_args: (field name, value) — SA keeps the value
                # shared with ``args``, which the loop above rewrites;
                # mirror the replacement here so both views agree.
                rebuilt: list = []
                touched = False
                for part in element:
                    if isinstance(part, Node):
                        replaced = self.rewrite(part)
                        if replaced is None:
                            touched = True
                            continue
                        if replaced is not part:
                            touched = True
                        rebuilt.append(replaced)
                    else:
                        rebuilt.append(part)
                if touched:
                    changed = True
                    out.append(tuple(rebuilt))
                else:
                    out.append(element)
            else:
                out.append(element)
        if changed:
            seq[:] = out
