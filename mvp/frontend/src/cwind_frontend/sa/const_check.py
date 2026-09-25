"""Compile-time validation for ``const`` initializers (编译期可确定).

Three concerns live here, all running as ``_Analyzer`` mixin methods:

* :meth:`ConstChecks._check_const_initializer` — pass-2 gate on the
  initializer expression itself.  A const value must be computable at
  compile time: literals, arithmetic/casts over them, reads of other
  consts, unit enum variants, function pointers and ``None`` are
  admitted; calls are admitted only when the callee is a ``const fn``
  (or an enum-variant constructor); closures, borrows, raw
  dereferences, assignments and extern-static / static-field reads are
  rejected.  The *type* of the value (declared type and every nested
  node annotation) must be a **const type**: either marked with
  ``const type X;`` in std's ``extern "CWind"`` surface or a
  structurally-allowed composite (struct / enum / array / reference /
  function signature).  No type is privileged by name — the verdict
  comes from the declaration table (``analyzer.const_types``).
* :meth:`ConstChecks._check_const_fn_return` — a ``const fn`` may only
  return a const type; heap-allocating containers stay banished from
  the return position even though their locals are allowed inside the
  body (comptime rules arrive with the next task).
* :meth:`ConstChecks._check_const_cycles` — whole-program dependency
  cycle detection over const declarations, recorded right before the
  inlining pass so a self-referential const cannot loop the rewriter.

All diagnostics run before serialization; a reported error fails the
compilation before any document reaches the backend.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from ..ast_components.ast import (
    Arg,
    Assign,
    Attribute,
    BinOp,
    BoolLit,
    Call,
    CastExpr,
    Closure,
    ConstDecl,
    ExtraDecl,
    FloatLit,
    Index,
    IntLit,
    MapEntry,
    MapLit,
    Name,
    Node,
    Slice,
    StrLit,
    StructConstruct,
    TryExpr,
    TupleLit,
    Type,
    UnaryOp,
    VectorLit,
)
from ..ast_components.token import TokenKind
from .types import _base, _split_ref_prefix, _type_str, split_array_type

if TYPE_CHECKING:
    from .analyzer import _Analyzer

__all__ = ["ConstChecks", "collect_const_decls"]

# Binding kinds that denote a compile-time constant (or a value the
# backend already materialises from the declaration itself).
_CONST_VALUE_BINDINGS = frozenset({"const", "assoc_const", "variant", "fn", "builtin"})

# Value expression kinds that are pure by construction.  Everything not
# on this list (and without a dedicated rule below) is rejected.
_PURE_EXPRS = (
    Attribute,
    Arg,
    BinOp,
    CastExpr,
    Index,
    MapEntry,
    Slice,
    StructConstruct,
    TupleLit,
    VectorLit,
)

_LITERALS = (IntLit, FloatLit, StrLit, BoolLit)


def collect_const_decls(program: Node) -> dict[int, ConstDecl]:
    """Index every serialized const declaration by typed id.

    Covers top-level ``ConstDecl`` items and ``ExtraDecl`` associated
    constants — exactly the set the backend can still resolve today
    (its symbol / extra-const tables are built from the serialized item
    pool).  The inliner and the cycle check share this index.
    """
    from .optimize._common import _walk_nodes

    decls: dict[int, ConstDecl] = {}
    for item in getattr(program, "items", None) or []:
        for node in _walk_nodes(item):
            if isinstance(node, ConstDecl):
                tid = node._typed_id
                if tid is not None:
                    decls[tid] = node
            elif isinstance(node, ExtraDecl):
                for c in node.consts or []:
                    tid = c._typed_id
                    if tid is not None:
                        decls[tid] = c
    return decls


class ConstChecks:
    """Mixin: const initializer / const-fn / const-type validation."""

    # -- const-type -------------------------------------------------------

    def _const_type_status(
        self: "_Analyzer", t: Optional[str], *, expand: bool = True
    ) -> str:
        """``"ok"`` / ``"unmarked"`` / ``"unknown"`` for a type spelling.

        * ``ok`` — marked ``const type`` in std, or structurally
          non-heap: struct / enum / fixed array / reference / raw
          pointer to an allowed pointee / function signature.
        * ``unmarked`` — a known extern built-in type (from
          ``extern "CWind"``) without a ``const type`` mark; using it
          as a const value type or const-fn return type is an error.
        * ``unknown`` — the name resolves to nothing here (typo or a
          generic parameter); other diagnostics own that case, so the
          const checks stay silent instead of double-reporting.
        """
        if not t:
            return "unknown"
        if expand:
            t = self._expand_type(t) or t
        _, inner = _split_ref_prefix(t)
        for prefix in ("*const ", "*mut "):
            if inner.startswith(prefix):
                inner = inner[len(prefix):]
                break
        if inner.startswith("fn("):
            return "ok"  # 函数指针是代码地址, 编译期确定
        array = split_array_type(inner)
        if array is not None:
            return self._const_type_status(array[0], expand=False)
        base = _base(inner)
        if not base:
            return "unknown"
        if base in self.const_types:
            return "ok"
        if base in self.structs or base in self.enums:
            return "ok"  # 复合值: 结构性放行 (存储非堆)
        if base in self._cwind_builtins:
            return "unmarked"
        return "unknown"

    def _check_const_fn_return(self: "_Analyzer", fn, ret: str) -> None:
        """``const fn`` 只允许返回 const-type (标记或结构性放行)。"""
        if self._const_type_status(ret) != "unmarked":
            return
        anchor = fn.return_type if fn.return_type is not None else fn
        self._record_error(
            f"const fn return type '{ret}' is not a const type",
            anchor.line,
            anchor.column,
        )

    # -- const initializer ------------------------------------------------

    def _check_const_initializer(self: "_Analyzer", decl: ConstDecl) -> None:
        """Reject non-constant initializers and non-const value types."""
        declared = _type_str(decl.type)
        if self._const_type_status(declared) == "unmarked":
            rendered = self._expand_type(declared) or declared
            self._record_error(
                f"type '{rendered}' is not a const type",
                decl.line,
                decl.column,
            )
            return
        self._walk_const_value(decl.value)

    def _const_fn_tables(self: "_Analyzer"):
        """``(fn id -> FnDecl, method-binding id -> FnDecl)``, lazily built.

        Built once after pass 1: const-expr call sites resolve their
        callee through ``ann.call`` (``callee_kind`` / ``callee_ref``)
        to decide whether the target carries the ``const fn`` mark.
        """
        tbl = getattr(self, "_const_fn_cache", None)
        if tbl is None:
            fn_by_id: dict[int, object] = {}
            bind_by_id: dict[int, object] = {}

            def add(fn) -> None:
                tid = getattr(fn, "_typed_id", None)
                if tid is not None:
                    fn_by_id[tid] = fn

            for fn in self.functions.values():
                add(fn)
            for fn in getattr(self, "_fqn_functions", {}).values():
                add(fn)
            for group in getattr(self, "_decl_nodes", {}).values():
                for fn in group:
                    add(fn)
            for methods in self.methods.values():
                for binding in methods:
                    add(binding.fn)
                    bind_by_id[binding.id] = binding.fn
            tbl = (fn_by_id, bind_by_id)
            self._const_fn_cache = tbl
        return tbl

    def _const_call_ok(self: "_Analyzer", call: Call) -> bool:
        """A call inside a const initializer: const-fn targets and enum
        variant constructors are admitted, everything else is not."""
        ann = call._typed_ann.get("call")
        if not isinstance(ann, dict):
            return False
        kind = ann.get("callee_kind")
        ref = ann.get("callee_ref")
        if kind == "enum_variant":
            # 变体构造是复合值构造, 不是函数调用。
            return True
        if not isinstance(ref, int):
            return False
        fn_by_id, bind_by_id = self._const_fn_tables()
        target = fn_by_id.get(ref) if kind == "fn" else (
            bind_by_id.get(ref) if kind == "method" else None
        )
        return bool(target is not None and getattr(target, "const_fn", False))

    def _walk_const_value(self: "_Analyzer", root: Node) -> None:
        """First offending node wins: one diagnostic per initializer."""
        failed = False

        def fail(msg: str, node: Node) -> None:
            nonlocal failed
            if failed:
                return
            failed = True
            self._record_error(msg, node.line, node.column)

        def visit(node: Node, *, designator: bool = False) -> None:
            nonlocal failed
            if failed or isinstance(node, Type):
                return
            ann = getattr(node, "_typed_ann", None) or {}
            info = ann.get("type")
            if not isinstance(info, dict):
                info = None
            # 值类型闸门: 节点注解类型必须是 const type (声明类型已在
            # _check_const_initializer 挡过, 这里管嵌套)。
            probe = _ann_type_str(
                info, "Map" if isinstance(node, MapLit) else ""
            )
            if probe and self._const_type_status(
                probe, expand=False
            ) == "unmarked":
                fail(f"type '{probe}' is not a const type", node)
                return
            if isinstance(node, Call):
                if not self._const_call_ok(node):
                    fail(
                        "calls in a const initializer must target a const fn",
                        node,
                    )
                    return
                # 合法调用: 形参正常校验; callee 是函数指示子 (接收者
                # 仍按值校验, 方法名/关联 fn 的 binding 豁免)。
                callee = node.callee
                if isinstance(callee, Attribute):
                    visit(callee.obj)
                else:
                    visit(callee, designator=True)
                for child in _child_nodes(node):
                    if child is callee:
                        continue
                    visit(child)
                return
            if isinstance(node, Closure):
                fail("closures are not allowed in a const initializer", node)
                return
            if isinstance(node, Assign):
                fail("assignments are not allowed in a const initializer", node)
                return
            if isinstance(node, TryExpr):
                fail("'?' is not allowed in a const initializer", node)
                return
            if isinstance(node, Name):
                binding = ann.get("binding")
                kind = binding.get("kind") if isinstance(binding, dict) else None
                if kind is None:
                    # Resolution already failed (or is still pending);
                    # the responsible pass owns that diagnostic.
                    return
                if designator and kind in ("fn", "method", "variant"):
                    return  # 函数指示子本身不是值
                if kind not in _CONST_VALUE_BINDINGS:
                    fail(
                        f"'{'::'.join(node.parts)}' is not a compile-time "
                        "constant",
                        node,
                    )
                return
            if isinstance(node, UnaryOp):
                if node.op == TokenKind.AMP:
                    fail(
                        "borrow expressions are not allowed in a const "
                        "initializer",
                        node,
                    )
                    return
                if node.op == TokenKind.STAR:
                    fail(
                        "dereference is not allowed in a const initializer",
                        node,
                    )
                    return
            elif isinstance(node, _LITERALS):
                return
            elif not isinstance(node, _PURE_EXPRS):
                fail(
                    f"'{type(node).__name__}' is not allowed in a const "
                    "initializer",
                    node,
                )
                return
            for child in _child_nodes(node):
                visit(child)

        visit(root)

    # -- cycles -----------------------------------------------------------

    def _check_const_cycles(self: "_Analyzer", program: Node) -> None:
        """Report const definitions that (transitively) depend on themselves."""
        from .optimize._common import _walk_nodes

        decls = collect_const_decls(program)
        if not decls:
            return
        edges: dict[int, set[int]] = {}
        for tid, decl in decls.items():
            deps: set[int] = set()
            for node in _walk_nodes(decl.value):
                if not isinstance(node, Name):
                    continue
                binding = getattr(node, "_typed_ann", None) or {}
                binding = binding.get("binding")
                if not isinstance(binding, dict):
                    continue
                if binding.get("kind") not in ("const", "assoc_const"):
                    continue
                ref = binding.get("ref")
                if isinstance(ref, int) and ref in decls:
                    deps.add(ref)
            edges[tid] = deps

        gray: set[int] = set()
        black: set[int] = set()
        reported: set[int] = set()

        def visit(tid: int) -> None:
            gray.add(tid)
            for dep in sorted(edges.get(tid, ())):
                if dep in gray:
                    if tid not in reported:
                        reported.add(tid)
                        decl = decls[tid]
                        self._record_error(
                            f"const '{decl.name}' is defined in terms of "
                            "itself (cyclic const definition)",
                            decl.line,
                            decl.column,
                        )
                elif dep not in black:
                    visit(dep)
            gray.discard(tid)
            black.add(tid)

        for tid in sorted(edges):
            if tid not in black:
                visit(tid)


def _ann_type_str(info: Optional[dict], fallback: str) -> str:
    """Render an ``ann.type`` dict as source spelling (``Vector<Int>``).

    The annotation stores the bare base name plus structured args, so the
    generic parameters have to be re-joined for the diagnostic.
    """
    if not isinstance(info, dict):
        return fallback
    name = str(info.get("name") or fallback)
    args = info.get("args")
    if isinstance(args, list) and args:
        rendered = ", ".join(
            _ann_type_str(a, "?") if isinstance(a, dict) else str(a)
            for a in args
        )
        return f"{name}<{rendered}>"
    return name


def _child_nodes(node: Node):
    """Yield the direct child nodes of *node* (values only, no positions).

    ``named_args`` tuples are intentionally skipped: SA moves those value
    nodes into ``args`` as well (shared objects), so the value subtree is
    fully reachable through the plain node fields.
    """
    from dataclasses import fields as _dc_fields

    for f in _dc_fields(node):
        if f.name in ("line", "column"):
            continue
        value = getattr(node, f.name, None)
        if isinstance(value, Node):
            yield value
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, Node):
                    yield item
