"""Reachability pruning of the typed program (object layer).

削减发生在 SA 完成之后、序列化之前: prelude 把整个 std 的声明面
内联进编译面, 每个顶层 FnDecl / impl/extra 块都会被后端发射成 IR
(fib 的 IR 上万行, rustc 同程序不到两百行)。这里从用户 main 出发
在**对象图**上做可达性闭包, 把不可达的 std 函数与 impl/extra 块
从 ``program.items`` 物理摘除 —— 序列化自然不再包含它们, symbols/
bindings 由 build_typed_ast 的 serialized-id 过滤兜底摘除。

削减面只覆盖 **std 项** (``source_module_path[0] == "std"``):
用户代码自己的顶层声明是程序面的一部分 (即使没人调用), 一律保留
—— 被削减的是 prelude 自动拉进来的 std 死代码。

双轨传播:

* id 轨: ``_typed_ann`` 里的裸 int 命中函数节点 id (callee_ref 的
  ``callee_kind=="fn"`` 形态 / binding.ref / decl_id) 则该函数可达,
  其函数体引用继续传播;
* binding 轨: ann 里的 int 命中 SA 方法绑定 id (``callee_kind==
  "method"`` 的 callee_ref 就是 binding id, 后端按 bindings 表
  线性查 ``x->id == bref``) → 该 binding 的宿主 impl/extra 块
  **整块**可达, 块内全部方法与引用继续传播 —— 泛型内建
  (``print<T: ToString>``) 的隐式特化绑定同样走这条轨。

类型名不再拉活 impl 块 (只保留上述两条 id 边): fib 只用 u64,
那么 UInt32/Float64 的方法面从 main 出发根本走不到, 自然全删。

保守面 (一律保留):

* 全部非 std 项 (用户自己的声明);
* std 中的 main / which/after/before 钩子方法与 static 方法
  (用途不可静态判定);
* 非 FnDecl/ImplDecl/ExtraDecl 的 std 顶层项 —— struct/enum/
  typedef/const/extern 块/宏是 SA 一致性校验与 C 链接面
  (仅保留本身, 其 ann 引用不作为种子扩散)。

id 不重建: 被摘节点的旧 id 成为空洞, 但保留节点间的引用在闭包
意义上自洽 (可达才保留), 序列化后的 ref 查询不会命中空洞。
"""

from __future__ import annotations

from dataclasses import fields as _dc_fields
from typing import Any, Sequence

from ..ast_components.ast import (
    FnDecl,
    ImplDecl,
    ExtraDecl,
    Node,
    Program,
)

__all__ = ["prune_unreachable"]


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


def _collect_ann_ids(node: Node, ids: set[int]) -> None:
    """Gather node/binding-id refs from every ``_typed_ann``.

    ``_typed_ann`` 值里的裸 int 一律视作节点/绑定引用 (line/column/
    字面量值都不进 ann)。alias/def 等字符串键值不是引用, 跳过。
    """
    for n in _walk_nodes(node):
        ann = getattr(n, "_typed_ann", None)
        if isinstance(ann, dict):
            _scan_ann(ann, ids)


def _scan_ann(ann: Any, ids: set[int]) -> None:
    if isinstance(ann, dict):
        for key, value in ann.items():
            if key in ("line", "column", "alias", "def", "owner_def",
                       "trait_def", "def_line", "def_column", "raw",
                       "source", "fqn"):
                continue
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                ids.add(value)
            else:
                _scan_ann(value, ids)
    elif isinstance(ann, list):
        for value in ann:
            _scan_ann(value, ids)


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
    """从 main 出发的可达性闭包; 不可达的 std 函数/impl 块被摘除。

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

    # 候选索引 (仅 std 项参与削减)
    fn_by_id: dict[int, Node] = {}
    blocks: list[Node] = []
    for item in items:
        if not _is_std(item):
            continue
        fid = item._typed_id
        if fid is None:
            continue
        if isinstance(item, FnDecl):
            fn_by_id[fid] = item
        elif isinstance(item, (ImplDecl, ExtraDecl)):
            blocks.append(item)

    # binding 轨索引: binding id -> 宿主块节点 (仅 std 块)
    block_by_binding: dict[int, Node] = {}
    block_ids: set[int] = {
        b._typed_id for b in blocks if b._typed_id is not None
    }
    for b in bindings:
        bid = getattr(b, "id", None)
        decl = getattr(b, "decl", None)
        if (
            bid is None
            or not isinstance(decl, (ImplDecl, ExtraDecl))
            or decl._typed_id not in block_ids
        ):
            continue
        block_by_binding[bid] = decl

    # 种子: 用户项的引用 + std 保守函数 (main/which/static) 的引用
    seed_ids: set[int] = set()
    for item in items:
        if not _is_std(item):
            _collect_ann_ids(item, seed_ids)
            continue
        if isinstance(item, FnDecl):
            if _is_conservative(item, main_ids):
                _collect_ann_ids(item, seed_ids)
        elif isinstance(item, (ImplDecl, ExtraDecl)):
            for m in getattr(item, "methods", None) or []:
                if isinstance(m, Node) and _is_conservative(m, main_ids):
                    _collect_ann_ids(m, seed_ids)

    # 不动点传播: (id, 函数) / (binding id, 宿主块) 双轨
    reach_ids: set[int] = set()
    reach_blocks: set[int] = set()
    queue: list[Any] = []
    for i in seed_ids:
        if i in fn_by_id:
            queue.append(i)
        elif i in block_by_binding:
            queue.append(block_by_binding[i])
    while queue:
        cur = queue.pop()
        if isinstance(cur, int):
            if cur in reach_ids:
                continue
            reach_ids.add(cur)
            nxt_ids: set[int] = set()
            _collect_ann_ids(fn_by_id[cur], nxt_ids)
            for i in nxt_ids:
                if i in fn_by_id and i not in reach_ids:
                    queue.append(i)
                elif i in block_by_binding:
                    blk = block_by_binding[i]
                    bid = blk._typed_id
                    if bid is not None and bid not in reach_blocks:
                        queue.append(blk)
        else:
            bid = cur._typed_id
            if bid is None or bid in reach_blocks:
                continue
            reach_blocks.add(bid)
            nxt_ids2: set[int] = set()
            for m in getattr(cur, "methods", None) or []:
                mid = m._typed_id
                if mid is not None:
                    reach_ids.add(mid)
                _collect_ann_ids(m, nxt_ids2)
            for i in nxt_ids2:
                if i in fn_by_id and i not in reach_ids:
                    queue.append(i)
                elif i in block_by_binding:
                    blk = block_by_binding[i]
                    b2id = blk._typed_id
                    if b2id is not None and b2id not in reach_blocks:
                        queue.append(blk)

    # 摘除: 不可达的 std FnDecl 与 impl/extra 块 (用户项全保)
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
        if isinstance(item, (ImplDecl, ExtraDecl)):
            bid = item._typed_id
            has_conservative = any(
                isinstance(m, Node) and _is_conservative(m, main_ids)
                for m in getattr(item, "methods", None) or []
            )
            if (
                bid is not None
                and bid not in reach_blocks
                and not has_conservative
            ):
                continue
            kept.append(item)
            continue
        kept.append(item)
    program.items = kept
