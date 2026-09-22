"""Post-SA DCE: object-graph reachability fixpoint and item pruning.

This layer sits at the serialization boundary (after reassociation, before
typed-AST build).  It works on the **object graph** with full annotations
and binding tables, so it can be much more precise than the pre-SA
syntactic layer.  It covers dependencies that only become visible after
SA: trait/impl default methods (todo-194 clones), which hooks emitted at
call sites, FFI declarations pulled by member-node ids, export adapters'
types, and monomorphized generic instantiations.

Dependency rules are **structural**: calls/types/impls/traits/fields/
macro expansion/attributes.  No module-name or function-name whitelist
exists.  Over-approximation is always allowed (an extra item kept);
under-approximation is a bug (a reachable item removed → unknown symbol
/ dangling ref).  Every fallback below therefore widens the kept set,
never narrows it.

See also :mod:`.refs` (annotation scanning), :mod:`.subst` (type
substitution / instantiation) and :mod:`.renumber` (dense id rewrite).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Sequence

from ...ast_components.ast import (
    ConstDecl,
    EnumDecl,
    ExternBlock,
    ExtraDecl,
    FnDecl,
    ImplDecl,
    Node,
    Program,
    StructDecl,
    TraitDecl,
    Type,
    TypeDecl,
)
from ..types import _base
from ._common import _is_std, _walk_nodes
from .refs import _Refs, _collect_refs
from .renumber import PruneResult, renumber_typed_ids
from .subst import (
    _add_instance,
    _apply_outer,
    _fn_params,
    _is_conservative,
    _resolve_receiver,
    _strip_aliases,
    _subst_sig,
)

__all__ = ["prune_unreachable", "PruneResult"]

_DECL_KINDS = (StructDecl, EnumDecl, TraitDecl, TypeDecl, ConstDecl)
_BLOCK_KINDS = (ImplDecl, ExtraDecl, ExternBlock)


class _GraphPrune:
    """Index → seed → fixpoint → apply, over one program's item graph."""

    def __init__(
        self,
        program: Program,
        main_fns: Sequence[Node],
        bindings: Sequence[Any],
    ) -> None:
        self.program = program
        self.items: list[Node] = list(program.items)
        self.main_ids: set[int] = {
            fn._typed_id for fn in main_fns if fn._typed_id is not None
        }
        self.bindings = bindings

        # 编译器自带 libs (install root) 的标量 typedef 只是 pass 0 展开前的
        # 拼写糖: 其 alias 是纯展示性 provenance, 不构成存活引用 (Defect B)。
        # 项目 libs/入口的 typedef 仍由 alias 维持存活 (todo-144/146 契约)。
        self.compiler_libs: Optional[Path] = None
        try:
            from ...home import install_root
            root = install_root()
            if root is not None:
                self.compiler_libs = (root / "libs").resolve()
        except Exception:  # pragma: no cover - discovery is best-effort
            self.compiler_libs = None

        # ---- 候选索引 (仅 std 项参与削减) ----
        # fn_by_id covers user + std declarations: user generic functions are
        # reached through call-site instantiation and must be rescanable per
        # context (they are never pruning candidates themselves).
        self.fn_by_id: dict[int, Node] = {}
        self.extern_member: dict[int, Node] = {}
        self.extern_member_node: dict[int, Node] = {}
        self.blocks: dict[int, Node] = {}
        self.decls_by_id: dict[int, Node] = {}
        self.decls_by_name: dict[str, Node] = {}
        self.field_owner: dict[int, Node] = {}
        self.extra_consts: dict[int, tuple[Node, Node]] = {}
        self.weak_typedefs: set[str] = set()

        # ---- binding 索引 ----
        self.binding_by_id: dict[int, Any] = {}
        self.member_bindings: dict[str, list[Any]] = {}
        self.owner_bindings: dict[str, dict[str, list[Any]]] = {}

        # ---- 不动点状态 ----
        self.reach_ids: set[int] = set()
        self.reach_methods: set[int] = set()
        self.reach_consts: set[int] = set()
        self.reach_decls: set[int] = set()
        self.reach_blocks: set[int] = set()
        self.reach_trait_members: set[tuple[str, str]] = set()
        self.queue: list[Any] = []
        self.fn_inst: dict[int, set[tuple]] = {}
        self.method_inst: dict[int, set[tuple]] = {}
        self.scanned_fn: set[tuple] = set()
        self.scanned_item: set[tuple] = set()
        self.deferred_fns: list[int] = []

    # -- helpers --------------------------------------------------

    def _is_compiler_std(self, item: Node) -> bool:
        if self.compiler_libs is None:
            return False
        src = getattr(item, "source_module", None)
        if not isinstance(src, str):
            return False
        try:
            return Path(src).resolve().is_relative_to(self.compiler_libs)
        except (OSError, ValueError):  # pragma: no cover - defensive
            return False

    # -- phase: build indexes -------------------------------------

    def _index_fn(self, item: FnDecl) -> None:
        fid = item._typed_id
        if fid is not None:
            self.fn_by_id.setdefault(fid, item)

    def _index_block(self, item: ImplDecl | ExtraDecl | ExternBlock) -> None:
        fid = item._typed_id
        if fid is not None:
            self.blocks[fid] = item
        if isinstance(item, ExternBlock):
            for m in (item.fns or []) + (item.statics or []):
                mid = getattr(m, "_typed_id", None)
                if isinstance(m, Node) and mid is not None:
                    self.extern_member[mid] = item
                    self.extern_member_node[mid] = m
        elif isinstance(item, ExtraDecl):
            # todo-122: 关联 const 引用 (ann kind == assoc_const)
            # 携带的是 const 节点 id, 命中即保该 const 与宿主容器
            # (后端 cg_extra_const 按 ExtraDecl 查 const)。
            for c in item.consts or []:
                mid = getattr(c, "_typed_id", None)
                if isinstance(c, Node) and mid is not None:
                    self.extra_consts[mid] = (item, c)

    def _index_decl(self, item: StructDecl | EnumDecl | TraitDecl | TypeDecl | ConstDecl) -> None:
        fid = item._typed_id
        if fid is not None:
            self.decls_by_id[fid] = item
        nm = getattr(item, "name", None)
        if isinstance(nm, str) and nm:
            self.decls_by_name[nm] = item
            if isinstance(item, TypeDecl) and self._is_compiler_std(item):
                self.weak_typedefs.add(nm)
        if isinstance(item, StructDecl):
            for f in item.fields or []:
                mid = getattr(f, "_typed_id", None)
                if mid is not None:
                    self.field_owner[mid] = item

    def _build_indexes(self) -> None:
        # fn_by_id covers user + std: user generic functions are reached
        # through call-site instantiation and must be rescanable per
        # context (they are never pruning candidates themselves).
        for item in self.items:
            if isinstance(item, FnDecl):
                self._index_fn(item)
        for item in self.items:
            if not _is_std(item):
                continue
            if isinstance(item, FnDecl):
                continue  # already in fn_by_id
            if isinstance(item, _BLOCK_KINDS):
                self._index_block(item)
            elif isinstance(item, _DECL_KINDS):
                self._index_decl(item)

    def _build_binding_indexes(self) -> None:
        for b in self.bindings:
            bid = getattr(b, "id", None)
            decl = getattr(b, "decl", None)
            bfn = getattr(b, "fn", None)
            if bid is None:
                continue
            self.binding_by_id[bid] = b
            if (
                not isinstance(decl, _BLOCK_KINDS)
                or not isinstance(bfn, Node)
                or decl._typed_id not in self.blocks
            ):
                continue
            mname = getattr(bfn, "name", None)
            if not isinstance(mname, str) or not mname:
                continue
            self.member_bindings.setdefault(mname, []).append(b)
            owner = getattr(b, "owner_struct", None)
            obase: Optional[str] = None
            if isinstance(owner, Type) and isinstance(owner.name, str):
                obase = _base(owner.name)
            else:
                oname = getattr(b, "owner", None)
                if isinstance(oname, str):
                    obase = _base(oname)
            if obase:
                bucket = self.owner_bindings.setdefault(obase, {}).setdefault(
                    mname, []
                )
                if b not in bucket:
                    bucket.append(b)

    # -- phase: mark / push ---------------------------------------

    def mark_method(
        self,
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
        fresh = id(method) not in self.reach_methods
        self.reach_methods.add(id(method))
        self.reach_blocks.add(id(block))
        mid = getattr(method, "_typed_id", None)
        if isinstance(block, ExternBlock) and mid is not None:
            self.reach_ids.add(mid)
        if mid is not None and _add_instance(self.method_inst, mid, inst):
            self.queue.append(block)
            return
        if fresh:
            self.queue.append(block)

    def mark_binding(self, b: Any, subst: Optional[dict] = None) -> None:
        decl = getattr(b, "decl", None)
        bfn = getattr(b, "fn", None)
        if not isinstance(decl, _BLOCK_KINDS) or not isinstance(bfn, Node):
            return
        t = getattr(b, "trait", None)
        mname = getattr(bfn, "name", None)
        if isinstance(t, str) and t and isinstance(mname, str) and mname:
            self.reach_trait_members.add((t, mname))
        self.mark_method(decl, bfn, subst)

    def mark_decl(self, d: Node) -> None:
        if id(d) not in self.reach_decls:
            self.reach_decls.add(id(d))
            self.queue.append(d)

    def mark_whole_block(self, block: Node) -> None:
        """Direct block-id reference (rare catch-all): keep all members."""
        if isinstance(block, ExternBlock):
            for m in (block.fns or []) + (block.statics or []):
                if isinstance(m, Node):
                    mid = getattr(m, "_typed_id", None)
                    if mid is not None:
                        self.reach_ids.add(mid)
                    self.reach_methods.add(id(m))
        else:
            for m in getattr(block, "methods", None) or []:
                if isinstance(m, Node):
                    self.mark_method(block, m)
        if isinstance(block, ExtraDecl):
            for c in block.consts or []:
                if isinstance(c, Node):
                    self.reach_consts.add(id(c))
        self.reach_blocks.add(id(block))
        self.queue.append(block)

    def push_id(self, i: int) -> None:
        if i in self.fn_by_id:
            # Unknown-context reference (function value etc.): the
            # dequeue guard skips it when concrete instantiations exist.
            self.queue.append((i, ()))
        elif i in self.extern_member:
            self.queue.append(i)
        elif i in self.field_owner:
            self.mark_decl(self.field_owner[i])
        elif i in self.extra_consts:
            blk, const = self.extra_consts[i]
            if id(const) not in self.reach_consts:
                self.reach_consts.add(id(const))
                self.queue.append(blk)
            self.reach_blocks.add(id(blk))
        elif i in self.decls_by_id:
            self.mark_decl(self.decls_by_id[i])
        elif i in self.blocks:
            self.mark_whole_block(self.blocks[i])

    def push_names(self, names: set[str]) -> None:
        for nm in names:
            d = self.decls_by_name.get(nm)
            if d is not None:
                self.mark_decl(d)

    # -- phase: propagate -----------------------------------------

    def propagate(
        self,
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
            fid for fid, ta in refs.fn_calls if ta and fid in self.fn_by_id
        }
        for i in refs.ids:
            if i in call_targets:
                continue
            if i not in self.reach_ids:
                self.push_id(i)
        for bid, ta in refs.binding_calls:
            b = self.binding_by_id.get(bid)
            if b is not None:
                self.mark_binding(b, _apply_outer(ta, subst) if ta else None)
        for trait, member, rt in refs.bound_sites:
            if trait:
                self.reach_trait_members.add((trait, member))
            concrete = _resolve_receiver(rt, subst, params)
            target: Optional[list[Any]] = None
            if concrete:
                target = self.owner_bindings.get(_base(concrete), {}).get(member)
            if not target:
                # Unresolved receiver (still generic / opaque) or no host
                # registered for the owner: keep every provider of the
                # member.  Never narrower than the backend's lookup.
                target = self.member_bindings.get(member, [])
            for b in target:
                self.mark_binding(b)
        for fid, ta in refs.fn_calls:
            if fid in self.extern_member:
                # extern "CWind"/"C" member (may be generic-looking):
                # keep the member node itself, no instance context.
                if fid not in self.reach_ids:
                    self.push_id(fid)
                continue
            if ta:
                inst = _apply_outer(ta, subst)
                if _add_instance(self.fn_inst, fid, inst):
                    self.queue.append((fid, _subst_sig(inst or {})))
            elif fid not in self.reach_ids:
                self.push_id(fid)
        self.push_names(refs.names)
        # Alias spellings: provenance, not a live reference.  A compiler
        # std scalar typedef is only kept when a *real* name/type position
        # references it; project typedefs stay alive through their alias
        # (todo-144/146 provenance contract).
        for alias in refs.aliases:
            d = self.decls_by_name.get(alias)
            if d is None or alias in self.weak_typedefs:
                continue
            self.mark_decl(d)

    def scan_fn(self, fid: int, subst: dict[str, Optional[str]]) -> None:
        fn = self.fn_by_id.get(fid)
        if fn is None:
            return
        refs = _Refs()
        _collect_refs(fn, refs)
        self.propagate(refs, subst, _fn_params(fn))

    # -- phase: scan items ----------------------------------------

    def member_reachable(self, m: Node, block: Node) -> bool:
        """Whether *m* is an approved member of *block* (user blocks keep
        everything; std blocks are filtered by the member-level sets)."""
        if not _is_std(block):
            return True
        if isinstance(block, ExternBlock):
            return getattr(m, "_typed_id", None) in self.reach_ids
        return id(m) in self.reach_methods

    def trait_method_kept(self, trait: TraitDecl, method: Node) -> bool:
        """Signature methods stay; a default body stays only when its
        (trait, member) surface or the node itself is referenced.  The
        body that actually runs is the todo-194 clone on the impl."""
        if getattr(method, "body", None) is None:
            return True
        mid = getattr(method, "_typed_id", None)
        if mid is not None and mid in self.reach_ids:
            return True
        tname = getattr(trait, "name", None)
        mname = getattr(method, "name", None)
        if not isinstance(tname, str) or not isinstance(mname, str):
            return False
        tbase = _base(tname)
        return any(
            m == mname and _base(t) == tbase
            for t, m in self.reach_trait_members
        )

    def _scan_block(self, item: ImplDecl | ExtraDecl) -> None:
        block_params = frozenset(
            p.name for p in getattr(item, "params", None) or []
            if isinstance(getattr(p, "name", None), str)
        )
        head = _Refs()
        for t in (getattr(item, "struct", None), getattr(item, "trait", None)):
            if isinstance(t, Type):
                _collect_refs(t, head)
        self.propagate(head, {}, block_params)
        for m in getattr(item, "methods", None) or []:
            if not isinstance(m, Node) or not self.member_reachable(m, item):
                continue
            mid = m._typed_id
            mrefs = _Refs()
            _collect_refs(m, mrefs)
            mparams = block_params | _fn_params(m)
            insts = self.method_inst.get(mid) if mid is not None else None
            if insts:
                for sig in sorted(insts):
                    self.propagate(mrefs, dict(sig), mparams)
            else:
                self.propagate(mrefs, {}, mparams)
        for c in getattr(item, "consts", None) or []:
            if not isinstance(c, Node):
                continue
            if _is_std(item) and id(c) not in self.reach_consts:
                continue
            cref = _Refs()
            _collect_refs(c, cref)
            self.propagate(cref, {}, block_params)

    def _scan_extern(self, item: ExternBlock) -> None:
        refs = _Refs()
        for m in (item.fns or []) + (item.statics or []):
            if not isinstance(m, Node) or not self.member_reachable(m, item):
                continue
            _collect_refs(m, refs)
        self.propagate(refs, {}, frozenset())

    def _scan_item(self, item: Node) -> None:
        if isinstance(item, (ImplDecl, ExtraDecl)):
            self._scan_block(item)
        elif isinstance(item, ExternBlock):
            self._scan_extern(item)
        elif isinstance(item, TraitDecl):
            # trait 声明体不传播引用: 后端不发射 trait 默认方法
            # (调用经实现者方法表的克隆分派), 其签名/默认体里的
            # 名字与绑定引用会令削减全家桶回潮
            pass
        else:
            # 声明项: 字段/载荷/初值引用继续扩散
            refs = _Refs()
            _collect_refs(item, refs)
            params = _fn_params(item) if isinstance(item, FnDecl) else frozenset()
            self.propagate(refs, {}, params)

    def item_signature(self, item: Node) -> tuple:
        if isinstance(item, (ImplDecl, ExtraDecl)):
            is_std = _is_std(item)
            parts: list[Any] = []
            for m in getattr(item, "methods", None) or []:
                if is_std and id(m) not in self.reach_methods:
                    continue
                mid: Optional[int] = getattr(m, "_typed_id", None)
                insts = (
                    self.method_inst.get(mid, set())
                    if mid is not None
                    else set()
                )
                parts.append((mid, tuple(sorted(insts))))
            for c in getattr(item, "consts", None) or []:
                if is_std and id(c) not in self.reach_consts:
                    continue
                parts.append((getattr(c, "_typed_id", None), "const"))
            return tuple(parts)
        if isinstance(item, ExternBlock):
            return tuple(
                getattr(m, "_typed_id", None)
                for m in (item.fns or []) + (item.statics or [])
                if getattr(m, "_typed_id", None) in self.reach_ids
            )
        return ()

    # -- phase: seed ----------------------------------------------

    def _seed(self) -> None:
        """User items' refs + std conservative (main/which/static) refs."""
        for item in self.items:
            if not _is_std(item):
                if isinstance(item, FnDecl) and getattr(item, "type_params", None):
                    fid = item._typed_id
                    if fid is not None:
                        self.deferred_fns.append(fid)
                    continue
                refs = _Refs()
                _collect_refs(item, refs)
                self.propagate(refs, {}, frozenset())
                continue
            if isinstance(item, FnDecl):
                if _is_conservative(item, self.main_ids):
                    refs2 = _Refs()
                    _collect_refs(item, refs2)
                    self.propagate(refs2, {}, _fn_params(item))
            elif isinstance(item, (ImplDecl, ExtraDecl)):
                block_params = frozenset(
                    p.name for p in getattr(item, "params", None) or []
                    if isinstance(getattr(p, "name", None), str)
                )
                for m in getattr(item, "methods", None) or []:
                    if isinstance(m, Node) and _is_conservative(m, self.main_ids):
                        self.mark_method(item, m)
                        refs3 = _Refs()
                        _collect_refs(m, refs3)
                        self.propagate(
                            refs3, {}, block_params | _fn_params(m)
                        )

    # -- phase: fixpoint ------------------------------------------

    def _dequeue_fn(self, fid: int, sig: tuple) -> None:
        if fid not in self.fn_by_id:
            return
        if (fid, sig) in self.scanned_fn:
            return
        insts = self.fn_inst.get(fid)
        if sig == () and insts and () not in insts:
            # Concrete instantiations exist; the unknown-context scan
            # is subsumed and would only widen the kept set.
            return
        self.scanned_fn.add((fid, sig))
        self.reach_ids.add(fid)
        self.scan_fn(fid, dict(sig))

    def _dequeue_extern(self, mid: int) -> None:
        if mid not in self.extern_member:
            return
        self.reach_ids.add(mid)
        member = self.extern_member_node.get(mid)
        host = self.extern_member[mid]
        if isinstance(member, Node):
            self.mark_method(host, member)

    def _dequeue_item(self, item: Node) -> None:
        bid = item._typed_id
        sig = self.item_signature(item)
        key = (bid, sig)
        if key in self.scanned_item:
            return
        self.scanned_item.add(key)
        self._scan_item(item)

    def _fixpoint(self) -> None:
        while self.queue or self.deferred_fns:
            if not self.queue:
                # Fixpoint drained: give still-uninstantiated generic user
                # functions their conservative unknown-context scan.  Ones
                # that did get a concrete context are skipped by the dequeuer.
                for fid in self.deferred_fns:
                    self.queue.append((fid, ()))
                self.deferred_fns = []
            cur = self.queue.pop()
            if isinstance(cur, tuple):
                self._dequeue_fn(cur[0], cur[1])
                continue
            if isinstance(cur, int):
                self._dequeue_extern(cur)
                continue
            self._dequeue_item(cur)

    # -- phase: apply ---------------------------------------------

    def _keep_fn(self, item: FnDecl) -> bool:
        fid = item._typed_id
        if (
            fid is not None
            and fid not in self.reach_ids
            and not _is_conservative(item, self.main_ids)
        ):
            return False
        return True

    def _keep_block(self, item: ImplDecl | ExtraDecl) -> bool:
        has_conservative = any(
            isinstance(m, Node) and _is_conservative(m, self.main_ids)
            for m in (getattr(item, "methods", None) or [])
        )
        if id(item) not in self.reach_blocks and not has_conservative:
            return False
        item.methods = [
            m for m in (getattr(item, "methods", None) or [])
            if id(m) in self.reach_methods
        ]
        if isinstance(item, ExtraDecl):
            item.consts = [
                c for c in (getattr(item, "consts", None) or [])
                if id(c) in self.reach_consts
            ]
            if not item.methods and not item.consts:
                return False
        elif not item.methods:
            return False
        return True

    def _keep_extern(self, item: ExternBlock) -> bool:
        if id(item) not in self.reach_blocks:
            return False
        item.fns = [
            m for m in (item.fns or [])
            if getattr(m, "_typed_id", None) in self.reach_ids
        ]
        item.statics = [
            m for m in (item.statics or [])
            if getattr(m, "_typed_id", None) in self.reach_ids
        ]
        if not item.fns and not item.statics and not item.types:
            return False
        return True

    def _keep_trait(self, item: TraitDecl) -> bool:
        if id(item) not in self.reach_decls:
            return False
        item.methods = [
            m for m in (getattr(item, "methods", None) or [])
            if self.trait_method_kept(item, m)
        ]
        return True

    def _keep_decl(self, item: Node) -> bool:
        return id(item) in self.reach_decls

    def _apply(self) -> list[Node]:
        kept: list[Node] = []
        for item in self.items:
            if not _is_std(item):
                kept.append(item)
                continue
            if isinstance(item, FnDecl):
                if self._keep_fn(item):
                    kept.append(item)
                continue
            if isinstance(item, (ImplDecl, ExtraDecl)):
                if self._keep_block(item):
                    kept.append(item)
                continue
            if isinstance(item, ExternBlock):
                if self._keep_extern(item):
                    kept.append(item)
                continue
            if isinstance(item, TraitDecl):
                if self._keep_trait(item):
                    kept.append(item)
                continue
            if isinstance(item, _DECL_KINDS):
                if self._keep_decl(item):
                    kept.append(item)
                continue
            kept.append(item)
        self.program.items = kept
        return kept

    def _strip_dead_aliases(self, kept: list[Node]) -> None:
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
                    _strip_aliases(ann, self.weak_typedefs, live_names)

    # -- driver ---------------------------------------------------

    def run(self) -> PruneResult:
        if not self.items:
            return PruneResult({}, set())
        self._build_indexes()
        self._build_binding_indexes()
        self._seed()
        self._fixpoint()
        kept = self._apply()
        self._strip_dead_aliases(kept)
        renumber, kept_objects = renumber_typed_ids(self.program)
        return PruneResult(renumber, kept_objects)


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
    return _GraphPrune(program, main_fns, bindings).run()
