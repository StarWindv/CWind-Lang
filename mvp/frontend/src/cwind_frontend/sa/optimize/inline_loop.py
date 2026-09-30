"""``#[opt(inline_loop(recursive = N))]`` —— 手工递归内联。

为什么需要它
------------
LLVM **不做自递归内联**: 内联器对 ``caller == callee`` 的调用整体跳过
(阈值拉到 100000 无效, ``always_inline`` 也不展开), 18/22 版本一致。
GCC 靠 IPA inliner 拿到递归树上层 8 层的内联 (9 层嵌套环), 实测比 clang
快 1.7~1.8 倍 —— 那 1.8 倍几乎全部来自"真实 call 数量减少一个数量级"。
cwindc 直接出 obj, 吃不到 GCC 那套, 所以这一层必须在前端做。

为什么不是"生成若干副本交给 LLVM 内联"
--------------------------------------
试过: 把 ``f`` 复制 N 份首尾相接成链 (副本 k 的自调用指向副本 k+1, 末尾
指向一个独立自递归函数), 让调用图无环, LLVM 就肯内联。**不可行** ——
它依赖的正是上面那个保守的内联器, 而保守是 LLVM 的设计取向而非配置问题。
所以本模块自己把展开做完。

形态
----
先把源码形态折成**累加器环** (本模块自己做, 不依赖 LLVM 的
``tailcallelim``), 再把环**套 N 层**::

    let mut acc0 = 0; let mut q0 = p;
    loop { <基例判定, 命中则 return base + acc0>     // 第 0 层
           let mut acc1 = 0; let mut q1 = a(q0); let mut v1 = 0;
           loop { <基例判定, 命中则 v1 = base + acc1; break>    // 第 1 层
                  ...
                  loop { ... accN = accN + E(qN); qN = next(qN); }  // 真实调用
                  acc_{N-1} = acc_{N-1} + vN; q_{N-1} = next(q_{N-1});
               }
           acc0 = acc0 + v1; q0 = next(q0);
           continue; }

顶部 N 层递归不再发真实调用, 第 N+1 层及以下才留真实调用。某一层命中基例
时, 它产出的值被**上一层**消费 (累加), 上一层继续自己的环; 只有第 0 层
命中基例才真正返回函数。层间只靠**普通 break** 衔接 (跳出内层环正好落在
上一层的"消费"位置), 不需要带标签跳转 —— 这也是"环套环"而非"调用套
调用"的原因。

支持范围 (窄, 但覆盖实测有收益的形状)
------------------------------------
* **多个**标量参数 (``binomial(n, k)`` 这种);
* 基例 = 尾部 return 之前的直线前缀里的若干早返回 (前缀里不允许环 ——
  否则生成的 ``break`` 会跳错层);
* 尾部 return = 若干 self-call 与纯标量项的 **``+`` 链**; 其中**最靠右**
  的 self-call 成为回边 (它天然在尾位), 其余项进累加器;
* 回边实参 = 对参数的纯标量算术。

形状之外**一律不动**, 并给出告警 (见 :func:`inline_loop_functions`) ——
绝不半途改写出一个语义不同的函数。
"""

from __future__ import annotations

import copy
from dataclasses import fields as _dc_fields
from typing import Any, NamedTuple, Optional

from ...ast_components.ast import (
    Arg,
    Assign,
    BinOp,
    Block,
    BoolLit,
    BreakStmt,
    Call,
    ContinueStmt,
    FloatLit,
    FnDecl,
    IntLit,
    LetStmt,
    LoopStmt,
    Name,
    Node,
    Program,
    ReturnStmt,
    UnaryOp,
)
from ...ast_components.token import TokenKind

__all__ = ["DEFAULT_DEPTH", "MAX_DEPTH", "inline_loop_functions"]

#: ``#[opt(inline_loop)]`` 不带参数时的层数。取 8 = GCC 的
#: ``max-inline-recursive-depth-auto`` 在 fib/binomial 上实测吃满的层数。
DEFAULT_DEPTH = 8

#: 层数硬上限。展开线性放大代码体积, 超过这个量级基本是笔误, 在属性
#: 校验处就拒掉, 不给编译期惊喜。
MAX_DEPTH = 64

_SCALARS = frozenset({
    "Int", "UInt", "Byte", "Bool",
    "Int8", "Int16", "Int32", "Int64",
    "UInt8", "UInt16", "UInt32", "UInt64",
    "Float", "Float32", "Float64",
})

_LIT = (IntLit, FloatLit, BoolLit)


# --------------------------------------------------------------------------
# 通用小工具
# --------------------------------------------------------------------------

def _ann_type_name(node: object) -> Optional[str]:
    ann = getattr(node, "_typed_ann", None)
    t = ann.get("type") if isinstance(ann, dict) else None
    if isinstance(t, dict):
        return t.get("name")
    return t if isinstance(t, str) else None


def _children(node: Node):
    for f in _dc_fields(node):
        if f.name in ("line", "column"):
            continue
        yield getattr(node, f.name, None)


def _walk(node: object):
    """Depth-first over every Node under *node* (lists included)."""
    if isinstance(node, Node):
        yield node
        for child in _children(node):
            yield from _walk(child)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _self_call(fn_id: Optional[int], node: object) -> Optional[Call]:
    """*node* 是对自身的裸名调用 (SA 已解析) 时返回它。"""
    if fn_id is None or not isinstance(node, Call):
        return None
    ann = node._typed_ann.get("call")
    if not isinstance(ann, dict) or ann.get("callee_kind") != "fn":
        return None
    if ann.get("callee_ref") != fn_id:
        return None
    return node


# --------------------------------------------------------------------------
# 匹配: 源码形态 -> Plan
# --------------------------------------------------------------------------

class Plan(NamedTuple):
    """``fn f(p...) -> R { <prefix>; return <+ chain over self-calls> }``。

    ``edge_index`` 是**回边** self-call (最靠右的那项, 处在尾位, 变成环的
    回边); ``inner`` 是**被内联**的那次 self-call —— 它留在累加器里, 正是
    展开时要替掉的那个。两者不可混: 下沉一层时新环的参数是 ``inner`` 的
    实参 (回边实参是回边自己的事, 只用来推进本层)。
    """

    params: list[str]
    param_types: list[str]
    ret_type: str
    prefix: list[Node]
    terms: list[Node]        # return 表达式的加法链各项 (原节点)
    edge_index: int          # 哪一项是回边 self-call
    inner_index: int         # 哪一项是被内联的 self-call (累加器里那个)


def _param_names(fn: FnDecl) -> Optional[tuple[list[str], list[str]]]:
    if not fn.params:
        return None
    names: list[str] = []
    types: list[str] = []
    for p in fn.params:
        t = _ann_type_name(p)
        if t not in _SCALARS or getattr(p.type, "ref", False):
            return None
        names.append(p.name)
        types.append(t)
    return names, types


def _is_pure_visible_expr(expr: object, visible: frozenset[str]) -> bool:
    """只由可见名 (形参 + 前缀里重新绑定的 let) 与字面量组成的纯标量算术。

    形参之所以必须在集合里: 回边/内层的实参每轮都要重新求值, 只能读
    本层环里稳定的量。前缀的 let 也在集合里 —— 它们是**每轮重新执行**的
    (前缀克隆进了环体), 所以环内稳定; 但前提是前缀没有对它们赋值, 由
    :func:`_visible_names` 保证。
    """
    if isinstance(expr, _LIT):
        return True
    if isinstance(expr, Name):
        return len(expr.parts) == 1 and expr.parts[0] in visible
    if isinstance(expr, BinOp):
        return (
            _is_pure_visible_expr(expr.left, visible)
            and _is_pure_visible_expr(expr.right, visible)
        )
    if isinstance(expr, UnaryOp):
        return _is_pure_visible_expr(expr.operand, visible)
    return False


def _visible_names(fn: FnDecl, params: list[str], prefix: list[Node]) -> frozenset[str]:
    """环内每轮稳定的可读名: 形参 + 前缀里 let 出来、且未被赋值的名字。

    前缀里被赋值过的 let 排除在外: 它的值会跨迭代变化, 拿它当实参会读到
    "上一轮遗留"的值, 悄悄算错。
    """
    bound: set[str] = set()
    assigned: set[str] = set()
    for node in _walk(prefix):
        if isinstance(node, LetStmt) and node.name:
            bound.add(node.name)
        if isinstance(node, Assign) and isinstance(node.target, Name):
            if len(node.target.parts) == 1:
                assigned.add(node.target.parts[0])
    return frozenset(set(params) | (bound - assigned))


def _flatten_add(expr: Node) -> Optional[list[Node]]:
    """``+`` 链拆项; 混入别的运算符则 None。"""
    if isinstance(expr, BinOp) and expr.op is TokenKind.PLUS:
        left = _flatten_add(expr.left)
        right = _flatten_add(expr.right)
        if left is None or right is None:
            return None
        return left + right
    return [expr]


def _self_calls_in(fn_id: int, term: object) -> list[Call]:
    return [n for n in _walk(term) if _self_call(fn_id, n) is not None]


def _call_args_ok(call: Call, np: int, visible: frozenset[str]) -> bool:
    if len(call.args) != np:
        return False
    return all(_is_pure_visible_expr(a.value, visible) for a in call.args)


def match_inline_loop(fn: FnDecl) -> Optional[Plan]:
    """源码形态 -> Plan; 不匹配返回 None (调用方保持原样不动)。"""
    sig = _param_names(fn)
    if sig is None or fn.body is None:
        return None
    names, param_types = sig
    ret = _ann_type_name(fn.return_type)
    if ret not in _SCALARS:
        return None

    fid = fn._typed_id
    stmts = fn.body.stmts
    if not stmts:
        return None
    tail, prefix = stmts[-1], stmts[:-1]
    if not isinstance(tail, ReturnStmt) or tail.value is None:
        return None

    terms = _flatten_add(tail.value)
    if terms is None or len(terms) < 2:
        return None

    # 前缀里不允许环: 展开生成的 break 是按"跳出本层环"设计的, 嵌在别人的
    # 环里会跳错层。
    if any(isinstance(n, LoopStmt) for n in _walk(prefix)):
        return None
    bases = [n for n in _walk(prefix) if isinstance(n, ReturnStmt)]
    if not bases:
        return None
    if _self_calls_in(fid, prefix):
        return None

    visible = _visible_names(fn, names, prefix)
    for r in bases:
        if r.value is None or not _is_pure_visible_expr(r.value, visible):
            return None

    # 回边: **最靠右**的裸 self-call (处在尾位, 变成环的回边)。必须是裸
    # 调用 —— 被别的运算包住的就不是尾位, 当不了回边。
    edge_index = None
    for i in range(len(terms) - 1, -1, -1):
        if _self_call(fid, terms[i]) is not None:
            edge_index = i
            break
    if edge_index is None:
        return None
    edge = _self_call(fid, terms[edge_index])
    assert edge is not None
    if not _call_args_ok(edge, len(names), visible):
        return None

    # 内层: 非回边项里出现的 self-call, 全树**有且仅有一个**。多项时
    # "该内联哪一个" / "新一层的参数取谁的实参" 都没有唯一定义, 与其猜
    # 不如不展开。
    #
    # 它可以是被标量运算包住的 (``f(n-1) * 0.5``, 概率/DP 递归的常见
    # 形状): 展开后把那次调用换成上一层算出的 v, 外围运算原样重建。
    inner_calls: list[tuple[int, Call]] = []
    for i, term in enumerate(terms):
        if i == edge_index:
            continue
        for call in _self_calls_in(fid, term):
            inner_calls.append((i, call))
    if len(inner_calls) != 1:
        return None
    inner_index, inner = inner_calls[0]
    if not _call_args_ok(inner, len(names), visible):
        return None
    # 其余非回边项必须是纯标量算术。
    for i, term in enumerate(terms):
        if i in (edge_index, inner_index):
            continue
        if not _is_pure_visible_expr(term, visible):
            return None

    return Plan(
        names, param_types, ret, list(prefix), terms, edge_index, inner_index
    )


# --------------------------------------------------------------------------
# 发射
# --------------------------------------------------------------------------

def _zero(line: int, column: int, type_name: str) -> Node:
    if type_name in ("Float", "Float32", "Float64"):
        node: Node = FloatLit(line, column, 0.0, "0.0")
    elif type_name == "Bool":
        node = BoolLit(line, column, False, "false")
    else:
        node = IntLit(line, column, 0, "0")
    node._typed_ann["type"] = {"name": type_name}
    return node


def _param_bindings(fn: FnDecl, names: list[str]) -> dict[str, dict]:
    """形参在原体里的 binding (深拷贝)。

    只用于第 0 层 q_0 的初始化表达式 (那里读的确实是形参本身)。重绑到
    q_k (k>0) 的引用会**丢掉** binding: q_k 是新 let 出来的局部, 挂着形参
    的 binding 是错的; 而局部名都是 ``_m<ctx>_x``, 后端按名就能找到, 同名
    的用户常量不可能存在 (那个拼写用户打不出来), 所以丢掉是安全的。
    """
    found: dict[str, dict] = {}

    def walk(n: Node) -> None:
        if isinstance(n, Name) and len(n.parts) == 1:
            b = n._typed_ann.get("binding")
            if isinstance(b, dict) and b.get("kind") == "param":
                found.setdefault(n.parts[0], b)
        for child in _children(n):
            if isinstance(child, Node):
                walk(child)
            elif isinstance(child, list):
                for item in child:
                    if isinstance(item, Node):
                        walk(item)

    if fn.body is not None:
        walk(fn.body)
    return {k: copy.deepcopy(v) for k, v in found.items() if k in names}


class _Builder:
    """按层生成注解齐全的新节点 (与 sa/optimize/emit.py 同纪律)。

    局部名一律 ``_fresh_desugar_name`` (``_m<ctx>_x``, 用户打不出来), 因此
    既不会与用户变量撞名, 也不必操心"同名不同层"。
    """

    def __init__(self, az: Any, program: Program, fn: FnDecl, plan: Plan):
        self.az = az
        self.program = program
        self.fn = fn
        self.plan = plan
        self.line = fn.line
        self.column = fn.column
        self.fid = fn._typed_id
        self.param_binding = _param_bindings(fn, plan.params)

    def fresh(self, base: str) -> str:
        return self.az._fresh_desugar_name(self.program, base)

    def typed(self, node: Node, type_name: str) -> Node:
        node._typed_ann["type"] = {"name": type_name}
        return node

    def local(self, name: str, type_name: str) -> Name:
        n = Name(self.line, self.column, [name])
        return self.typed(n, type_name)  # type: ignore[return-value]

    def param_ref(self, name: str, type_name: str) -> Name:
        n = self.local(name, type_name)
        binding = self.param_binding.get(name)
        if binding is not None:
            n._typed_ann["binding"] = copy.deepcopy(binding)
        return n

    def let(self, name: str, type_name: str, value: Node,
            mutable: bool) -> LetStmt:
        node = LetStmt(self.line, self.column, name, None, value,
                       mutable=mutable)
        return self.typed(node, type_name)  # type: ignore[return-value]

    def assign(self, target: Name, value: Node, type_name: str) -> Assign:
        node = Assign(self.line, self.column, target, TokenKind.ASSIGN, value)
        return self.typed(node, type_name)  # type: ignore[return-value]

    def plus(self, left: Node, right: Node, type_name: str) -> BinOp:
        node = BinOp(self.line, self.column, left, TokenKind.PLUS, right)
        return self.typed(node, type_name)  # type: ignore[return-value]

    def self_call(self, arg_exprs: list[Node], type_name: str) -> Call:
        callee = Name(self.line, self.column, [self.fn.name])
        callee._typed_ann["type"] = {"name": "Fn"}
        call = Call(
            self.line, self.column, callee,
            [Arg(self.line, self.column, a) for a in arg_exprs],
        )
        call._typed_ann["call"] = {
            "callee_kind": "fn", "callee_ref": self.fid,
        }
        return self.typed(call, type_name)  # type: ignore[return-value]

    def loop(self, body: list[Node]) -> LoopStmt:
        return LoopStmt(
            self.line, self.column, Block(self.line, self.column, body)
        )

    # -- 重绑: 形参引用 -> 本层 q_k -------------------------------------
    def rebind(self, expr: Node, qnames: list[str]) -> Node:
        """深拷贝一份, 把对**形参**的引用改写为对**本层 q_k** 的引用。

        ``expr`` 必须是本模块自己的节点 (调用方负责传副本)。
        """
        mapping = dict(zip(self.plan.params, qnames))
        for node in _walk(expr):
            if isinstance(node, Name) and len(node.parts) == 1:
                target = mapping.get(node.parts[0])
                if target is not None:
                    node.parts = [target]
                    # q_k 是新局部, 形参的 binding 不再适用 (见
                    # _param_bindings 的说明)。
                    node._typed_ann.pop("binding", None)
        return expr

    def copy_rebind(self, expr: Node, qnames: list[str]) -> Node:
        return self.rebind(copy.deepcopy(expr), qnames)

    # -- 前缀改写: 基例早返回 -------------------------------------------
    def prefix_for(self, level: int, acc_name: str, v_name: Optional[str],
                   ret_t: str, qnames: list[str]) -> list[Node]:
        """克隆前缀并改成本层的形态。

        两件事, **顺序不能反**:

        1. 先把整棵克隆里对**形参**的引用全部改写为对本层 ``q_k`` 的引用 ——
           包括基例判定的**条件** (``if n <= 1`` 的 subject 在 MatchStmt 上,
           不在 return 的值里)。漏掉这一步会让每层都拿永不改变的原始形参
           做基例判定, 环不退化, 真实调用无限递归 —— 表现为栈溢出。
        2. 再把每个基例 ``return X`` 换成本层形态:
           第 0 层 ``return X + acc`` (真正返回函数); 第 k>0 层
           ``v_k = X + acc_k; break`` (值交给上一层消费, 普通 break 跳出
           本层环, 正好落在上一层的"消费"位置)。
        """
        out: list[Node] = []
        for stmt in self.plan.prefix:
            clone = copy.deepcopy(stmt)
            self.rebind(clone, qnames)
            _map_stmt_lists(
                clone, self._ret_to_seq(level, acc_name, v_name, ret_t)
            )
            out.append(clone)
        return out

    def _ret_to_seq(self, level: int, acc_name: str, v_name: Optional[str],
                    ret_t: str) -> "callable":
        def make(ret: ReturnStmt) -> list[Node]:
            assert ret.value is not None
            total = self.plus(
                ret.value, self.local(acc_name, ret_t), ret_t
            )
            if level == 0:
                ret.value = total
                return [ret]
            assert v_name is not None
            return [
                self.assign(self.local(v_name, ret_t), total, ret_t),
                BreakStmt(ret.line, ret.column),
            ]
        return make


def _map_stmt_lists(node: Node, make) -> None:
    """把子树里每个 ReturnStmt 换成 ``make(ret)`` 给出的语句序列。

    ReturnStmt 一定处在某个语句列表里 (match 臂体 / 环体都是 Block), 所以
    在列表槽位上就地展开即可, 不需要造嵌套 Block。
    """
    for f in _dc_fields(node):
        if f.name in ("line", "column"):
            continue
        value = getattr(node, f.name, None)
        if isinstance(value, list):
            out: list = []
            changed = False
            for item in value:
                if isinstance(item, ReturnStmt):
                    out.extend(make(item))
                    changed = True
                else:
                    out.append(item)
                    if isinstance(item, Node):
                        _map_stmt_lists(item, make)
            if changed:
                setattr(node, f.name, out)
        elif isinstance(value, Node):
            _map_stmt_lists(value, make)


class _EmitAbort(Exception):
    """内部一致性断言失败: 计划里的节点在改写中找不到对应物。"""


def _sole_self_call(fn_id: int, node: object) -> Optional[Call]:
    """子树里**唯一**那个对 fn_id 的自调用; 0 个或多于 1 个都返回 None。

    不能拿原节点做身份比较: 累加项是深拷贝出来的 (每层都要独立重绑),
    副本里的调用与计划里保存的不是同一个对象。按"唯一性"定位才稳。
    """
    hits = [n for n in _walk(node) if _self_call(fn_id, n) is not None]
    return hits[0] if len(hits) == 1 else None


def _subst_call(root: Node, target: Call, replacement: Node) -> Optional[Node]:
    """把 *root* 子树里那一个 *target* 调用换成 *replacement*。

    形状唯一 (匹配器保证非回边项里只有一次内层调用), 所以不需要路径信息。
    返回替换后的根, 或 None (找不到 —— 内部不一致, 由调用方转告警)。
    """
    if root is target:
        # 整项就是那次调用 (``f(n-1)`` 这种裸写法): 整体换掉, 没有子节点
        # 可供替换, 所以调用方拿到的返回值必须被采用。
        return replacement
    for f in _dc_fields(root):
        if f.name in ("line", "column"):
            continue
        value = getattr(root, f.name, None)
        if value is target:
            setattr(root, f.name, replacement)
            return root
        if isinstance(value, list):
            for i, item in enumerate(value):
                if item is target:
                    value[i] = replacement
                    return root
                if isinstance(item, Node) and _subst_call(item, target, replacement):
                    return root
        elif isinstance(value, Node):
            if _subst_call(value, target, replacement):
                return value
    return None


# --------------------------------------------------------------------------
# 驱动
# --------------------------------------------------------------------------

def _depth_of(fn: FnDecl) -> int:
    spec = (fn.opt or {}).get("inline_loop") or {}
    value = spec.get("recursive")
    return DEFAULT_DEPTH if value is None else int(value)


def inline_loop_functions(az: Any, program: Program) -> None:
    """对带 ``#[opt(inline_loop(...))]`` 的自由函数做递归内联 (原地)。"""
    targets = [
        item for item in program.items
        if isinstance(item, FnDecl)
        and item.body is not None
        and (item.opt or {}).get("inline_loop") is not None
        and item.extern_abi is None
    ]
    for fn in targets:
        depth = _depth_of(fn)
        if depth <= 0:
            continue
        plan = match_inline_loop(fn)
        if plan is None:
            az._record_warning(
                f"'{fn.name}': #[opt(inline_loop)] does not apply to this "
                "function's shape (expected a scalar-returning function "
                "whose tail return is a '+' chain containing self-calls, "
                "with the base cases in straight-line statements before it); "
                "the body is left unchanged",
                fn.line, fn.column,
            )
            continue
        _emit(az, program, fn, plan, depth)


def _emit(az: Any, program: Program, fn: FnDecl, plan: Plan,
          depth: int) -> None:
    b = _Builder(az, program, fn, plan)
    ret_t = plan.ret_type
    edge = _self_call(fn._typed_id, plan.terms[plan.edge_index])
    assert edge is not None
    # 被内联的那次调用: 展开就是替掉它, 所以新一层的参数取**它的**实参。
    # 它可能自带标量系数 (``f(n-1) * 0.5``), 所以在项**内部**找而不是
    # 要求整项就是调用 (整项就是调用时它自己就是那唯一的命中)。
    inner = _sole_self_call(b.fid, plan.terms[plan.inner_index])
    assert inner is not None
    np = len(plan.params)

    acc = [b.fresh("acc") for _ in range(depth + 1)]
    q = [[b.fresh("q") for _ in range(np)] for _ in range(depth + 1)]
    v = [b.fresh("v") for _ in range(depth)]

    def accumulate(k: int, inline_inner: bool) -> Assign:
        """``acc_k = acc_k + <所有非回边项>``。

        累加器自身是链条的起点 —— 漏掉它就变成"每轮覆盖"而不是"每轮累加"。
        内层那一项可能自带标量系数 (``f(n-1) * 0.5``):

        * ``inline_inner=True`` (下沉层): 内层调用已被内联成下方的环, 把
          调用本身换成上一层算出的 ``v_k``, 外围系数原样保留;
        * ``inline_inner=False`` (最深层): 内层调用是**唯一剩下的真实递归
          调用**, 原样留着。
        """
        value: Node = b.local(acc[k], ret_t)
        for i, term in enumerate(plan.terms):
            if i == plan.edge_index:
                continue
            piece = b.copy_rebind(term, q[k])
            if i == plan.inner_index and inline_inner:
                site = _sole_self_call(b.fid, piece)
                if site is None:
                    raise _EmitAbort(
                        "inner self-call is not unique after rebinding"
                    )
                piece = _subst_call(piece, site, b.local(v[k], ret_t))
                if piece is None:
                    raise _EmitAbort("inner self-call vanished")
            value = b.plus(value, piece, ret_t)
        return b.assign(b.local(acc[k], ret_t), value, ret_t)

    def advance(k: int) -> list[Assign]:
        """``q_k = <回边实参>``, 逐参数一条。"""
        return [
            b.assign(
                b.local(q[k][i], plan.param_types[i]),
                b.copy_rebind(edge.args[i].value, q[k]),
                plan.param_types[i],
            )
            for i in range(np)
        ]

    def level_body(k: int) -> list[Node]:
        stmts: list[Node] = list(b.prefix_for(
            k, acc[k], v[k - 1] if k > 0 else None, ret_t, q[k]
        ))
        if k == depth:
            # 最深一层: 内层调用留作真实递归调用。
            stmts.append(accumulate(k, inline_inner=False))
            stmts.extend(advance(k))
        else:
            # 下沉一层: 内层调用被内联成下方这个环, 产出 v_k 供本层消费。
            stmts.append(b.let(acc[k + 1], ret_t,
                               _zero(b.line, b.column, ret_t), mutable=True))
            for i in range(np):
                stmts.append(b.let(
                    q[k + 1][i], plan.param_types[i],
                    b.copy_rebind(inner.args[i].value, q[k]), mutable=True,
                ))
            stmts.append(b.let(v[k], ret_t,
                               _zero(b.line, b.column, ret_t), mutable=True))
            stmts.append(b.loop(level_body(k + 1)))
            stmts.append(accumulate(k, inline_inner=True))
            stmts.extend(advance(k))
        stmts.append(ContinueStmt(b.line, b.column))
        return stmts

    head: list[Node] = [
        b.let(acc[0], ret_t, _zero(b.line, b.column, ret_t), mutable=True)
    ]
    for i, src in enumerate(plan.params):
        head.append(b.let(
            q[0][i], plan.param_types[i],
            b.param_ref(src, plan.param_types[i]), mutable=True,
        ))
    head.append(b.loop(level_body(0)))

    for node in head:
        az._assign_synthetic_ids(node)
    fn.body = Block(b.line, b.column, head)
