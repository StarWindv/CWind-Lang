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
* the fully-inlined clone is then **folded**: a pure arithmetic value
  collapses to a synthesized ``IntLit``/``FloatLit`` (so the unparse
  view and the JSON show the computed result, and forward-referenced
  chains that pass 2 could not fold become constants too); arithmetic
  subtrees the root fold cannot cover get ``ann.folded`` annotations
  (todo-22 discipline — the backend emits the constant directly).
  Integer ``/`` and ``%`` are excluded from folding: Python floor /
  sign semantics differ from the backend's ``sdiv``/``srem``, so those
  expressions are kept and evaluated at the use site exactly as a
  hand-written literal chain would be;
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
import math
from dataclasses import fields as _dc_fields
from typing import TYPE_CHECKING, Optional

from ...ast_components.ast import (
    BinOp,
    BoolLit,
    ConstDecl,
    FloatLit,
    IntLit,
    Name,
    Node,
    Program,
    StrLit,
    UnaryOp,
)
from ...ast_components.token import TokenKind
from ..const_check import _child_nodes, collect_const_decls
from ..const_fold import _const_number
from ..desugar import DesugarPass
from ..types import _UINT64_MAX

if TYPE_CHECKING:
    from ..analyzer import _Analyzer

__all__ = ["inline_consts"]

# Binding kinds whose value comes from a ConstDecl in the index.
_INLINABLE_BINDINGS = frozenset({"const", "assoc_const"})

# Python `//` / `%` disagree with the backend's sdiv/srem on negative
# operands (bug-60 discipline in sa/const_fold): chains containing them
# are never folded or annotated here — they keep runtime evaluation.
_DIVMOD_OPS = frozenset({TokenKind.SLASH, TokenKind.PERCENT})


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
            # 值折叠: 内联完成后的表达式已是纯字面量链, 折出结果直接
            # 换成字面量 (前向引用链 pass 2 折不动, 这里补上); 根折不
            # 动的给子树补 ann.folded 注解 (todo-22: 后端按注解发常量)。
            out = self._fold_const_value(out, decl)
            self.analyzer._assign_synthetic_ids(out)
            self.memo[id(name)] = (name, out)
            return out
        finally:
            self.active.discard(id(decl))

    # -- post-inline folding ------------------------------------------------

    @staticmethod
    def _has_divmod(node: Node) -> bool:
        """True when the subtree contains integer ``/`` or ``%``.

        Python floor/sign semantics differ from the backend's
        ``sdiv``/``srem`` on negative operands, so such chains must keep
        runtime evaluation (same discipline as
        ``sa/expressions/literals._literal_fold_annotatable``).
        """
        if isinstance(node, BinOp):
            if node.op in _DIVMOD_OPS:
                return True
            return _Inliner._has_divmod(node.left) or _Inliner._has_divmod(
                node.right
            )
        if isinstance(node, UnaryOp):
            return _Inliner._has_divmod(node.operand)
        return False

    @staticmethod
    def _fold_ok(folded) -> bool:
        """Whether *folded* is a value the literal/annotation paths keep
        exactly (mirrors pass 2's accepted fold window + finite floats)."""
        if isinstance(folded, bool):
            return False
        if isinstance(folded, int):
            return -(1 << 63) <= folded <= _UINT64_MAX
        if isinstance(folded, float):
            return math.isfinite(folded)
        return False

    def _fold_const_value(self, node: Node, decl: ConstDecl) -> Node:
        """Collapse a fully-inlined arithmetic value to a literal.

        ``1 + 1`` becomes ``IntLit(2)`` (with the declared type
        annotation), which is what the unparse view, the JSON and any
        later consumer see; values that do not fold (string
        concatenation, struct/variant construction, const-fn calls,
        integer division) keep their expression form unchanged.
        """
        if isinstance(node, (IntLit, FloatLit, BoolLit, StrLit, Name)):
            return node  # 已是字面量 / 不可折叠的引用
        if isinstance(node, (BinOp, UnaryOp)) and not self._has_divmod(node):
            folded = _const_number(
                node,
                self.analyzer.const_values,
                self.analyzer.const_floats,
            )
            if folded is not None and self._fold_ok(folded):
                return self._synth_literal(folded, node, decl)
        # 根折不动 (调用、结构体、除法链…): 遍历整个克隆子树, 给可折叠
        # 的纯整数算术链补 ann.folded 注解 —— 后端 cg_expr_binop 见注解
        # 直接发常量 (与 pass 2 同纪律)。
        self._annotate_arith(node)
        return node

    def _annotate_arith(self, node: Node) -> None:
        for child in _child_nodes(node):
            self._annotate_arith(child)
        if not isinstance(node, BinOp):
            return
        ann = node._typed_ann
        if "folded" in ann or self._has_divmod(node):
            return
        folded = _const_number(
            node, self.analyzer.const_values, self.analyzer.const_floats
        )
        if isinstance(folded, int) and self._fold_ok(folded):
            ann["folded"] = folded

    def _synth_literal(
        self, folded, node: Node, decl: ConstDecl
    ) -> Node:
        """Build a literal node carrying the fold result.

        The annotation starts from the folded expression's own ``ann``
        (type provenance) with the declared const type layered on top —
        the shape a hand-written literal for this const would have.
        ``raw`` carries the exact decimal so the backend's u64-range
        path (``strtoull`` on raw) never loses precision on JSON numbers
        beyond int64.
        """
        ann = copy.deepcopy(getattr(node, "_typed_ann", None) or {})
        decl_type = (getattr(decl, "_typed_ann", None) or {}).get("type")
        if isinstance(decl_type, dict):
            ann["type"] = copy.deepcopy(decl_type)
        if isinstance(folded, float):
            lit: Node = FloatLit(
                node.line, node.column, float(folded), repr(float(folded))
            )
        else:
            value = int(folded)
            lit = IntLit(node.line, node.column, value, str(value))
        lit._typed_ann = ann
        return lit

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
