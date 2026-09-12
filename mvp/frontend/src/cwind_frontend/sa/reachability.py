"""Reachability pruning of the typed program (object layer).

削减发生在 SA 完成之后、序列化之前: prelude 把整个 std 的声明面
内联进编译面, 每个顶层 FnDecl / impl/extra 块 / 类型声明都会被
序列化 (fib 的 JSON 上万节点)。这里从用户 main 出发在**对象图**
上做可达性闭包, 把不可达的 std 项从 ``program.items`` 物理摘除
—— 序列化自然不再包含它们, symbols/bindings 由 build_typed_ast
的 serialized-id 过滤兜底摘除。

削减面只覆盖 **std 项** (``source_module_path[0] == "std"``):
用户代码自己的顶层声明是程序面的一部分 (即使没人调用), 一律保留
—— 被削减的是 prelude 自动拉进来的 std 死代码。

传播轨道:

* id 轨: ``_typed_ann`` 里的裸 int 命中函数节点 id (``callee_kind
  =="fn"`` 的 callee_ref / binding.ref / extern static 引用) →
  该函数可达; 命中 std ConstDecl 节点 id (常量按名引用, ref 即
  声明节点) → 该常量可达; 命中 SA 方法绑定 id (``callee_kind==
  "method"`` 的 callee_ref 是 bindings 表 id, 后端按 bindings 表
  ``x->id == bref`` 查) → 宿主 impl/extra/extern 块**整块**可达
  —— 泛型内建 (``print<T: ToString>``) 的隐式特化绑定同样走这条。
* 名字轨 (仅声明项): ann 里的 ``{"name": ...}`` / ``alias`` 与
  Type 节点的 canonical 名命中 StructDecl/EnumDecl/TraitDecl/
  TypeDecl 的声明名 → 该声明可达。名字轨**不**触发 impl 块
  (类型被引用 ≠ 方法面被引用, 那是初版的过肥教训); impl 块只由
  binding/id 轨拉活。可达声明自身的子树引用 (结构体字段类型/
  enum 载荷/const 初值调用) 继续扩散。
* extern 块: 成员 fn/static 的节点 id 被引用 (真实 C FFI 调用
   携带成员节点 id) → 块可达 —— ``#[link]`` 元数据随之保留;
   内建分派 (``callee_kind=="builtin"`` 是字符串引用) 不引用成员
   节点, 纯内建块正确消亡 (后端按名分发, 不读声明)。
* bound 轨 (todo-166): ``callee_kind=="bound_method"`` 的标注只携带
  (可见 bound trait, 成员名) —— 实现体在单态化时才按具体接收者类型
  选定, SA 静态图上没有 binding id 可拉活。凡方法名命中标注 member 的
  ``impl`` 绑定, 其宿主 impl 块整块保留 (按名保守存活, 与后端
  owner+member 先到先得的分派纪律同构; 也是 rustc codegen 保留全部
  可实例化 impl 的对应物)。

保守面 (一律保留):

* 全部非 std 项 (用户自己的声明);
* std 中的 main / which/after/before 钩子方法与 static 方法
  (用途不可静态判定);
* UseDecl / 宏 / group 等 std 杂项 (SA/序列化契约面, 数量少)。

id 不重建: 被摘节点的旧 id 成为空洞, 但保留节点间的引用在闭包
意义上自洽 (可达才保留), 序列化后的 ref 查询不会命中空洞。
"""

from __future__ import annotations

from dataclasses import fields as _dc_fields
from typing import Any, Optional, Sequence

from ..ast_components.ast import (
    ConstDecl,
    EnumDecl,
    ExternBlock,
    ExternStatic,
    ExtraDecl,
    FnDecl,
    ImplDecl,
    Node,
    Program,
    StructDecl,
    TraitDecl,
    TypeDecl,
    Type,
)

__all__ = ["prune_unreachable"]

_DECL_KINDS = (StructDecl, EnumDecl, TraitDecl, TypeDecl, ConstDecl)
_BLOCK_KINDS = (ImplDecl, ExtraDecl, ExternBlock)


def _is_std(item: Node) -> bool:
    """Whether *item* was declared inside the ``std`` (libs) tree."""
    path = getattr(item, "source_module_path", None) or []
    return bool(path) and path[0] == "std"


def _walk_nodes(node: Node):
    """Yield every AST node in the subtree, pre-order."""
    yield node
    for f in _dc_fields(node):
        if f.name in ("line", "column"):
            continue
        value = getattr(node, f.name, None)
        if isinstance(value, Node):
            yield from _walk_nodes(value)
        elif isinstance(value, list):
            for v in value:
                if isinstance(v, Node):
                    yield from _walk_nodes(v)


def _scan_ann(
    ann: Any,
    ids: set[int],
    names: set[str],
    binding_ids: set[int],
    bmods: set[tuple[str, str]],
) -> None:
    """按**键语义**分流 ann 里的裸 int —— 节点 id 与绑定 id 共享整数
    空间, 撞号时只看值会把 binding 当节点 (反之亦然):

    * ``{"callee_kind": "method", "callee_ref": N}`` (调用点) 与
      ``{"kind": "method", "ref": N}`` (Attribute 绑定) → **绑定 id**
      (后端按 bindings 表 ``x->id == bref`` 查);
    * ``{"callee_kind": "bound_method", ...}`` (todo-166) →
      **(trait, member)** 对进 bound 轨;
    * 其余裸 int (``callee_kind=="fn"`` 的 callee_ref / ``kind=="var"``
      的 binding.ref / decl_id ...) → 节点 id。
    ``{"name"/"alias": str}`` 收类型名候选。
    """
    if isinstance(ann, dict):
        kind = ann.get("callee_kind") or ann.get("kind")
        is_method = kind == "method"
        if kind == "bound_method":
            trait = ann.get("trait")
            member = ann.get("member")
            ref = ann.get("callee_ref")
            if isinstance(ref, dict):
                if not isinstance(trait, str):
                    trait = ref.get("trait")
                if not isinstance(member, str):
                    member = ref.get("member")
            if (
                isinstance(trait, str) and trait
                and isinstance(member, str) and member
            ):
                bmods.add((trait, member))
        for key, value in ann.items():
            if key in ("line", "column", "def", "owner_def",
                       "trait_def", "def_line", "def_column", "raw",
                       "source", "fqn"):
                continue
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                if is_method and key in ("callee_ref", "ref"):
                    binding_ids.add(value)
                else:
                    ids.add(value)
            elif isinstance(value, dict):
                nm = value.get("name")
                if isinstance(nm, str):
                    names.add(nm.split("<", 1)[0])
                al = value.get("alias")
                if isinstance(al, str):
                    names.add(al.split("<", 1)[0])
                _scan_ann(value, ids, names, binding_ids, bmods)
            else:
                _scan_ann(value, ids, names, binding_ids, bmods)
    elif isinstance(ann, list):
        for value in ann:
            _scan_ann(value, ids, names, binding_ids, bmods)


def _collect_refs(
    node: Node,
    ids: set[int],
    names: set[str],
    binding_ids: set[int],
    bmods: set[tuple[str, str]],
) -> None:
    """Gather node-id refs, binding-id refs, bound (trait, member)
    pairs and type-name candidates from *node*'s subtree: every
    ``_typed_ann`` plus Type nodes' canonical names (pass 0 已把
    Type.name 规范化, impl 的 trait 名等从这里命中)。"""
    for n in _walk_nodes(node):
        ann = getattr(n, "_typed_ann", None)
        if isinstance(ann, dict):
            _scan_ann(ann, ids, names, binding_ids, bmods)
        if isinstance(n, Type):
            nm = getattr(n, "name", None)
            if isinstance(nm, str):
                names.add(nm.split("<", 1)[0])


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


def prune_unreachable(
    program: Program,
    main_fns: Sequence[Node],
    bindings: Sequence[Any],
) -> None:
    """从 main 出发的可达性闭包; 不可达的 std 项从 items 摘除。

    ``main_fns`` 是 SA 符号表中的 main 声明节点 (一般恰一个)。
    ``bindings`` 是 SA 的 MethodBinding 序列 (``_binding_order`` 的
    绑定对象), 用于把 ann 里的 binding id 解析回宿主块。
    ``program._module_file_programs`` 的 per-file 视图共享同一批
    item 对象, 无需单独处理。
    """
    items: list[Node] = list(program.items)
    if not items:
        return

    main_ids: set[int] = set()
    for fn in main_fns:
        fid = fn._typed_id
        if fid is not None:
            main_ids.add(fid)

    # ---- 候选索引 (仅 std 项参与削减) ----
    top_fn_by_id: dict[int, Node] = {}      # 顶层 FnDecl
    extern_member: dict[int, Node] = {}     # extern 成员 fn/static → 宿主块
    blocks: dict[int, Node] = {}            # impl/extra/extern 块
    decls_by_id: dict[int, Node] = {}       # Struct/Enum/Trait/Type/Const
    decls_by_name: dict[str, Node] = {}
    for item in items:
        if not _is_std(item):
            continue
        fid = item._typed_id
        if isinstance(item, FnDecl):
            if fid is not None:
                top_fn_by_id[fid] = item
        elif isinstance(item, _BLOCK_KINDS):
            if fid is not None:
                blocks[fid] = item
            if isinstance(item, ExternBlock):
                for m in (item.fns or []) + (item.statics or []):
                    mid = getattr(m, "_typed_id", None)
                    if isinstance(m, Node) and mid is not None:
                        extern_member[mid] = item
        elif isinstance(item, _DECL_KINDS):
            if fid is not None:
                decls_by_id[fid] = item
            nm = getattr(item, "name", None)
            if isinstance(nm, str) and nm:
                decls_by_name[nm] = item

    # binding 轨索引: binding id -> 宿主块 (impl/extra/extern)
    block_by_binding: dict[int, Node] = {}
    for b in bindings:
        bid = getattr(b, "id", None)
        decl = getattr(b, "decl", None)
        if (
            bid is None
            or not isinstance(decl, _BLOCK_KINDS)
            or decl._typed_id not in blocks
        ):
            continue
        block_by_binding[bid] = decl

    # bound 轨索引 (todo-166): 方法名 -> 提供该方法的宿主 impl 块。
    # 分派纪律与后端同构: owner + 方法名先到先得, trait 链的
    # elaboration 已在 194 注入 / collect 绑定里完成, 这里按名字保守
    # 保留全部潜在实现者 (等价 rustc codegen 保留全部可实例化 impl)。
    bound_hosts: dict[str, list[Node]] = {}
    for b in bindings:
        fn = getattr(b, "fn", None)
        decl = getattr(b, "decl", None)
        if (
            not isinstance(fn, FnDecl)
            or not isinstance(decl, _BLOCK_KINDS)
            or decl._typed_id not in blocks
        ):
            continue
        mname = getattr(fn, "name", None)
        if not isinstance(mname, str) or not mname:
            continue
        hosts = bound_hosts.setdefault(mname, [])
        if decl not in hosts:
            hosts.append(decl)

    # ---- 不动点闭包 ----
    reach_ids: set[int] = set()     # 函数节点 id (顶层 + extern 成员)
    reach_items: set[int] = set()   # 块/声明项 id
    queue: list[Any] = []           # int (fn id) 或 item 节点

    def push_id(i: int) -> None:
        if i in top_fn_by_id or i in extern_member:
            queue.append(i)
        elif i in decls_by_id:
            queue.append(decls_by_id[i])
        elif i in blocks:
            queue.append(blocks[i])

    def push_names(names: set[str]) -> None:
        for nm in names:
            d = decls_by_name.get(nm)
            if d is not None:
                queue.append(d)

    def push_item(item: Node) -> None:
        bid = item._typed_id
        if bid is not None and bid not in reach_items:
            queue.append(item)

    def propagate(
        ids: set[int],
        names: set[str],
        binding_ids: set[int],
        bmods: set[tuple[str, str]],
    ) -> None:
        for i in ids:
            if i not in reach_ids:
                push_id(i)
        for b in binding_ids:
            # binding 轨: method 调用点的 callee_ref 是 bindings 表
            # id, 命中即拉活宿主 impl/extra/extern 块 (泛型
            # print<T: ToString> 的隐式特化绑定走这条)
            blk = block_by_binding.get(b)
            if blk is not None:
                push_item(blk)
        for _trait, member in bmods:
            # bound 轨: bound_method 标注的方法名面存活 (trait 仅收集)
            for blk in bound_hosts.get(member, ()):
                push_item(blk)
        push_names(names)

    # 种子: 用户项的全部引用 + std 保守函数 (main/which/static) 的
    # 引用 —— 用户面引用的 std 声明从这里被拉活。
    for item in items:
        if not _is_std(item):
            ids: set[int] = set()
            names: set[str] = set()
            bids: set[int] = set()
            bms: set[tuple[str, str]] = set()
            _collect_refs(item, ids, names, bids, bms)
            propagate(ids, names, bids, bms)
            continue
        if isinstance(item, FnDecl):
            if _is_conservative(item, main_ids):
                ids2: set[int] = set()
                names2: set[str] = set()
                bids2: set[int] = set()
                bms2: set[tuple[str, str]] = set()
                _collect_refs(item, ids2, names2, bids2, bms2)
                propagate(ids2, names2, bids2, bms2)
        elif isinstance(item, (ImplDecl, ExtraDecl)):
            for m in getattr(item, "methods", None) or []:
                if isinstance(m, Node) and _is_conservative(m, main_ids):
                    ids3: set[int] = set()
                    names3: set[str] = set()
                    bids3: set[int] = set()
                    bms3: set[tuple[str, str]] = set()
                    _collect_refs(m, ids3, names3, bids3, bms3)
                    propagate(ids3, names3, bids3, bms3)

    while queue:
        cur = queue.pop()
        if isinstance(cur, int):
            if cur in reach_ids:
                continue
            reach_ids.add(cur)
            fn = top_fn_by_id.get(cur) or extern_member.get(cur)
            if fn is None:
                continue
            nxt_ids: set[int] = set()
            nxt_names: set[str] = set()
            nxt_bids: set[int] = set()
            nxt_bms: set[tuple[str, str]] = set()
            _collect_refs(fn, nxt_ids, nxt_names, nxt_bids, nxt_bms)
            propagate(nxt_ids, nxt_names, nxt_bids, nxt_bms)
            host = extern_member.get(cur)
            if host is not None:
                push_item(host)
        else:
            bid = cur._typed_id
            if bid is None or bid in reach_items:
                continue
            reach_items.add(bid)
            nxt_ids2: set[int] = set()
            nxt_names2: set[str] = set()
            nxt_bids2: set[int] = set()
            nxt_bms2: set[tuple[str, str]] = set()
            if isinstance(cur, (ImplDecl, ExtraDecl)):
                for m in getattr(cur, "methods", None) or []:
                    if isinstance(m, Node):
                        mid = m._typed_id
                        if mid is not None:
                            reach_ids.add(mid)
                        _collect_refs(
                            m, nxt_ids2, nxt_names2, nxt_bids2, nxt_bms2
                        )
                _collect_refs(
                    cur, nxt_ids2, nxt_names2, nxt_bids2, nxt_bms2
                )
            elif isinstance(cur, ExternBlock):
                for m in (cur.fns or []) + (cur.statics or []):
                    if isinstance(m, Node):
                        mid = m._typed_id
                        if mid is not None:
                            reach_ids.add(mid)
                        _collect_refs(
                            m, nxt_ids2, nxt_names2, nxt_bids2, nxt_bms2
                        )
            elif isinstance(cur, TraitDecl):
                # trait 声明体不传播引用: 后端不发射 trait 默认方法
                # (调用经实现者方法表的克隆分派), 其签名/默认体里的
                # 名字与绑定引用会令削减全家桶回潮
                pass
            else:
                # 声明项: 字段/载荷/初值引用继续扩散
                _collect_refs(
                    cur, nxt_ids2, nxt_names2, nxt_bids2, nxt_bms2
                )
            propagate(nxt_ids2, nxt_names2, nxt_bids2, nxt_bms2)

    # ---- 摘除 (用户项全保) ----
    kept: list[Node] = []
    for item in items:
        if not _is_std(item):
            kept.append(item)
            continue
        if isinstance(item, FnDecl):
            fid = item._typed_id
            if (
                fid is not None
                and fid not in reach_ids
                and not _is_conservative(item, main_ids)
            ):
                continue
            kept.append(item)
            continue
        if isinstance(item, _BLOCK_KINDS):
            bid = item._typed_id
            has_conservative = any(
                isinstance(m, Node) and _is_conservative(m, main_ids)
                for m in (getattr(item, "methods", None) or [])
            )
            if (
                bid is not None
                and bid not in reach_items
                and not has_conservative
            ):
                continue
            kept.append(item)
            continue
        if isinstance(item, _DECL_KINDS):
            did = item._typed_id
            if did is not None and did not in reach_items:
                continue
            kept.append(item)
            continue
        kept.append(item)
    program.items = kept
