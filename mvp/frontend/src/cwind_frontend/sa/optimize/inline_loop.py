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

两条路线
--------
同一个属性有两种源码形态, 分别对应两种展开。两者都**只在前端**做, 后端
看到的仍是普通 CWind 函数, typed-AST 不需要新字段。

路线 B —— 尾部累加器环套 N 层
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
先折成**累加器环**, 再把环**套 N 层**::

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
调用"的原因。环的嵌套形状只在这里用得上, 路线 C 用不到 (见下)。

路线 C —— 表达式位的自调用就地内联
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
``bernoulli(n)`` 这种形状 (``assets/bench/bernoulli30.wind``): 自调用
``bernoulli(count)`` 在**表达式位** (一个环里的复合表达式中间), 早返回有
三条, 环体不能被删。它没有"回边"可言 —— 环只是把 ``count = 0..n-1`` 的
一串调用铺平, 不是一条递归链, 所以路线 B 的累加器套层完全不适用。

路线 C 因此只做一件事: **把唯一那处自调用原地换成调用点的内联副本**。
每一层的体是原函数体的一份拷贝, 形参重绑到本层局部, 早返回改写成
"写结果槽 + 跳出本层包装环"::

    let mut r1: R = 0;
    'il1: loop {
        <体的一份拷贝: 形参 -> q1, `return X` -> `r1 = X; break 'il1`,
         用户自己的环原样保留>
        break 'il1;
    }
    <原调用点读到 r1>

第 1..N 层各有一份, 第 N 层里的那次自调用是**唯一剩下的真实调用**。
展开深度严格受 ``recursive = N`` 约束 (N 还会被 :data:`MAX_DEPTH` 再夹一层)。

**语义**: 与路线 B 不同, 路线 C 的正确性论证是**结构归纳**而不是"环不变
式": 改写后的函数体与原体逐字相同, 唯一差别是那处自调用换成了"同样一
段计算", 而那一段计算的最内层又回到 (已改写的) 自身。于是原调用树上第
k 层的每个节点在改写后仍是同一段计算, 叶子 (早返回) 完全一样 ——
终止性与返回值都逐点相等, 发散路径也一起发散。**不依赖任何关于环/基例
完备性的假设**, 所以路线 C 敢接受"基例不全"甚至"没有明显基例"的函数。

CWind **没有块表达式** (``let a = { ... }`` 是语法错误), 早返回的函数体
没法直接放进表达式位, 这就是那个"``'il: loop { ...; break }`` 包装环 +
结果槽"的由来。包装环必须是**带标签**的: 生成的 ``break`` 要跳出的是包
装环, 而原体里可能嵌着用户自己的环 —— 不带标签就会跳错层 (这正是路线 B
干脆要求"前缀里不许有环"的原因; 路线 C 克隆的是**整个**体, 躲不开, 只能
带标签)。反过来, 用户自己那些**不带标签**的 ``break`` / ``continue``
一个字都不用动: 包装环永远是**函数体副本的最外层环**, 而副本里每个不带
标签的跳转外面都严格套着一个用户自己的环, 取"最内层环"命中的仍是用户的
环, 不是包装环。

措辞上要小心: **不要**说"包装环在用户环外面"—— 调用点本身落在某个用户环
里的时候(``sum += ... * f(k)`` 这种), 包装环是嵌在那个用户环**里面**的。
成立的理由是"副本的最外层", 不是"函数的最外层"。
只有一种情形会让无标签跳转够到包装环: 跳转**直接**写在副本顶层而外面没有
任何用户环 —— 那在原程序里就不是合法代码(没有外层环), 所以
:func:`_reject_stray_jump` 直接拒。

把内联副本提到**语句**位, 唯一的要求是: "这条语句执行几次, 那次自调用就被
求值几次" —— 提到语句位正好给出这个次数 (包装环就贴在语句前面)。从调用点
往上走到那条语句, 沿途每一步都得是"父执行一次、子恰好求值一次"的边, 见
:data:`_UNCONDITIONAL_LINKS`。撞上条件边就**拒绝**, 不猜求值次数: 典型是
``match`` 的**臂守卫** (一条 match 可以对好几个臂各求值一次) 与 ``&&`` /
``||`` 的右操作数 (短路)。``match`` 的**块臂体**与用户自己的环反而没问题:
那种情况的宿主语句就在臂里 / 环里, 包装环与它同进同出。

支持范围
--------
两条路线各自的支持范围见 :func:`match_inline_loop` 与
:func:`match_inline_expr` 的 docstring。形状之外**一律不动**, 并给出
一条告警 (见 :func:`inline_loop_functions`) —— 绝不半途改写出一个语义不
同的函数。

两条路线共用的一条纪律: **体里不允许任何绑定遮蔽形参**。重绑是按**名字**
做的 (:meth:`_Builder.rebind`), 体里写一句 ``let n = ...`` 就会让自己的
读被重绑吞掉。与其教重绑器认作用域 (认漏一处就是静默算错), 不如整个函
数拒绝。
"""

from __future__ import annotations

import copy
from dataclasses import fields as _dc_fields
from typing import Any, NamedTuple, Optional, TypeVar

from ...ast_components.ast import (
    Arg,
    Assign,
    Attribute,
    BindPattern,
    BinOp,
    Block,
    BoolLit,
    BreakStmt,
    Call,
    CastExpr,
    Closure,
    ContinueStmt,
    ExprStmt,
    FloatLit,
    FnDecl,
    Index,
    IntLit,
    LetStmt,
    LoopStmt,
    Name,
    Node,
    Program,
    ReturnStmt,
    Slice,
    StructConstruct,
    TupleLit,
    UnaryOp,
    VectorLit,
)
from ...ast_components.token import TokenKind

__all__ = ["DEFAULT_DEPTH", "MAX_DEPTH", "inline_loop_functions"]

#: ``#[opt(inline_loop)]`` 不带参数时的层数。取 8 = GCC 的
#: ``max-inline-recursive-depth-auto`` 在 fib/binomial 上实测吃满的层数。
DEFAULT_DEPTH = 8

#: 层数硬上限。展开线性放大代码体积, 超过这个量级基本是笔误。属性校验
#: 只要求"非负整数", 所以真正的闸门在这里: 超过上限的函数**整个不展开**
#: (一条告警), 免得一个手滑的 ``recursive = 100000`` 把编译器挂死。
MAX_DEPTH = 64

_SCALARS = frozenset({
    "Int", "UInt", "Byte", "Bool",
    "Int8", "Int16", "Int32", "Int64",
    "UInt8", "UInt16", "UInt32", "UInt64",
    "Float", "Float32", "Float64",
})

_LIT = (IntLit, FloatLit, BoolLit)

_T = TypeVar("_T", bound=Node)


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


def _fresh_copy(node: _T) -> _T:
    """``copy.deepcopy``, 但**清掉 typed-AST id** (注解原样保留)。

    拷贝沿用原 id 只有在"原件随即被丢弃"时才安全 —— 路线 B 恰好如此 (整
    个前缀连同尾 return 一起换成新头, 原件不再出现在树里)。路线 C不行:
    第 0 层要**留下**原节点, 若第 1..N 层的克隆再带一份同样的 id, 同一
    个 id 就指了两棵不同的子树。把 id 清成 ``None``, 交给
    ``_assign_synthetic_ids`` 统一重新编号; 注解描述的是同一段表达式, 原
    样带走即可 (后端只看 ann)。
    """
    dup = copy.deepcopy(node)
    for n in _walk(dup):
        n._typed_id = None
    return dup


# --------------------------------------------------------------------------
# 匹配: 共用前缀
# --------------------------------------------------------------------------

class Shape(NamedTuple):
    """两条路线共用的起点: 按值标量形参 + 标量返回类型 + 函数体。"""

    params: list[str]
    param_types: list[str]
    ret_type: str
    body: Block


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


def _shape_of(fn: FnDecl) -> Optional[Shape]:
    sig = _param_names(fn)
    if sig is None or fn.body is None:
        return None
    ret = _ann_type_name(fn.return_type)
    if ret not in _SCALARS:
        return None
    names, param_types = sig
    return Shape(names, param_types, ret, fn.body)


def _shadows_param(region: object, params: list[str]) -> bool:
    """*region* 里有没有 let / 模式绑定顶掉了形参名。

    重绑 (:meth:`_Builder.rebind`) 是**按名字**做的, 所以体里一句
    ``let n = ...`` 会让它自己的读被重绑吞掉 (环不退化 / 算错)。两条路线
    共用这个判据: 与其教重绑器认作用域 (认漏一处 = 静默算错), 不如整个
    函数不展开。
    """
    names = set(params)
    for node in _walk(region):
        bound = None
        if isinstance(node, LetStmt):
            bound = node.name
        elif isinstance(node, BindPattern):
            bound = node.name
        if bound and bound in names:
            return True
    return False


# --------------------------------------------------------------------------
# 路线 B 的匹配
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
    body: Block
    prefix: list[Node]        # return 之前的前缀 (深拷贝, 原件被丢弃)
    terms: list[Node]         # return 表达式的加法链各项 (原节点)
    edge_index: int           # 哪一项是回边 self-call
    inner_index: int          # 哪一项是被内联的 self-call (累加器里那个)


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
    """路线 B 的源码形态 -> Plan; 不匹配返回 None (调用方保持原样不动)。

    接受:

    * **多个**标量形参 (``binomial(n, k)`` 这种);
    * 基例 = 尾部 return 之前的**直线前缀**里的若干早返回 (前缀里不允许
      环 —— 否则生成的 ``break`` 会跳错层);
    * 尾部 return = 若干 self-call 与纯标量项的 **``+`` 链**; 其中**最靠右**
      的 self-call 成为回边 (它天然在尾位), 其余项进累加器;
    * 回边实参 = 对参数的纯标量算术。

    拒绝 (调用方给一条告警, 体不动): 多个内层 self-call; 缩放过的**回边**
    调用; ``-`` 代替 ``+``; self-call 落在环里; self-call 不在尾位; 前缀
    里有环; 没有 self-call; 返回值非标量; **体里有绑定遮蔽形参**
    (:func:`_shadows_param` —— 那是会静默算错的, 不是保守取舍)。
    """
    shape = _shape_of(fn)
    if shape is None:
        return None
    names, param_types, ret, body = shape

    fid = fn._typed_id
    stmts = body.stmts
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
    if _shadows_param(body, names):
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
        names, param_types, ret, body, list(prefix), terms, edge_index,
        inner_index,
    )


# --------------------------------------------------------------------------
# 路线 C 的匹配
# --------------------------------------------------------------------------

class ExprPlan(NamedTuple):
    """路线 C: 体里**有且仅有一处**自调用, 位置在总是求值的表达式位。"""

    params: list[str]
    param_types: list[str]
    ret_type: str
    body: Block
    call: Call               # 那处自调用 (原节点)
    call_type: str           # 它的注解类型 (替换成 ``Name(r)`` 时沿用)
    body_labels: frozenset[str]   # 体里出现过的所有标签 (拼写)


#: 父 -> 子这条链上"父每执行一次, 子恰好被求值一次"的边。把内联副本提到
#: **语句**位 (它要执行几次, 就得看那条语句执行几次) 只能穿过这些边:
#:
#: * 走**不到** ``Block.stmts`` 的, 或者撞上条件边的, 一律拒绝 —— 典型是
#:   ``match`` 的**臂守卫** (一条 match 可以对好几个臂各求值一次守卫) 和
#:   ``&&`` / ``||`` 的右操作数 (短路: 左假时压根不求值);
#: * 反过来, ``match`` 臂体 / 用户自己的环**不在**拒绝之列: 那种情况的宿主
#:   语句就是臂体 / 环体里的那条语句, 包装环插在它**旁边**, 于是"语句执行
#:   几次、包装环就跑几次", 与原调用次数逐次相等。
_UNCONDITIONAL_LINKS = frozenset({
    ("Block", "stmts"),
    ("BinOp", "left"), ("BinOp", "right"),
    ("UnaryOp", "operand"),
    ("Call", "callee"), ("Call", "args"),
    ("Arg", "value"),
    ("Assign", "target"), ("Assign", "value"),
    ("Index", "obj"), ("Index", "index"),
    ("Slice", "obj"), ("Slice", "start"), ("Slice", "stop"), ("Slice", "step"),
    ("CastExpr", "operand"),
    ("Attribute", "obj"),
    ("VectorLit", "elems"), ("TupleLit", "elems"),
    ("StructConstruct", "args"),
    ("ReturnStmt", "value"),
    ("LetStmt", "value"),
    ("ExprStmt", "expr"),
    ("MatchStmt", "subject"),
})

#: 短路运算符: 右操作数可能压根不被求值。
_SHORT_CIRCUIT = frozenset({TokenKind.AND, TokenKind.OR})

#: 体里出现这些节点就整个拒绝。``if`` / ``while`` / ``for-in`` **不在**这个
#: 名单里: 它们在降糖阶段就折成 LoopStmt + match, 而 :func:`_owning_stmt`
#: 对它们的求值次数已经是对的 (条件里的调用压根够不到语句边; 分支体里的调用
#: 宿主语句就是分支里那条)。闭包不一样 —— 它有**自己的** return 归属。
_OPAQUE_SCOPE = (Closure,)


def _owning_stmt(root: Node, target: Node) -> Optional[Node]:
    """*target* 所在的那条语句; 不在语句里 (或不在无条件位置) 则 None。

    从 *target* 往上走, 只穿 :data:`_UNCONDITIONAL_LINKS` 里的边; 撞上的第
    一条 ``Block.stmts`` 边就是宿主语句 —— 包装环插在它**前面**, 于是两者
    执行次数相等。走到一半撞上条件边 (臂守卫 / 短路右操作数), 或者根本没
    有语句边 (例如具名字段实参 ``StructConstruct.named_args`` 是
    ``list[tuple[str, Node]]``, 压根不在子节点里), 都返回 None。
    """
    parents: dict[int, tuple[Node, str]] = {}
    stack: list[Node] = [root]
    while stack:
        node = stack.pop()
        for f in _dc_fields(node):
            if f.name in ("line", "column"):
                continue
            value = getattr(node, f.name, None)
            if isinstance(value, Node):
                parents[id(value)] = (node, f.name)
                stack.append(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, Node):
                        parents[id(item)] = (node, f.name)
                        stack.append(item)
    node = target
    while True:
        entry = parents.get(id(node))
        if entry is None:
            return None
        parent, field = entry
        if (type(parent).__name__, field) not in _UNCONDITIONAL_LINKS:
            return None
        if isinstance(parent, BinOp) and parent.op in _SHORT_CIRCUIT:
            return None
        if isinstance(parent, Block) and field == "stmts":
            return node
        node = parent


def _body_labels(body: Block) -> frozenset[str]:
    out: set[str] = set()
    for node in _walk(body):
        label = getattr(node, "label", None)
        if isinstance(label, str) and label:
            out.add(label)
    return frozenset(out)


def _has_stray_jump(body: Block) -> bool:
    """体里有没有"不在任何环内"的不带标签 ``break`` / ``continue``。

    SA 不查这个 (后端 ``cg_loop_resolve`` 才报 "break outside a loop"), 所以
    这种源码今天**编不过**。而包装环是**新加**的环: 一旦把体包进去, 原本落在
    体顶层的那个不带标签跳转就会绑到包装环上, 于是"编不过"变成"编得过且
    行为不同"。不是数值错, 但仍是行为变化 —— 所以看到就拒绝。

    带标签的不用管: 标签原样保留, 包不包装都还是"找不到外层环"。
    """
    def walk(node: object, depth: int) -> bool:
        if isinstance(node, LoopStmt):
            depth += 1
        if depth == 0 and isinstance(node, (BreakStmt, ContinueStmt)) \
                and node.label is None:
            return True
        if isinstance(node, Node):
            return any(
                walk(child, depth) for child in _children(node)
                if isinstance(child, (Node, list))
            )
        if isinstance(node, list):
            return any(walk(item, depth) for item in node)
        return False

    return walk(body, 0)


def match_inline_expr(fn: FnDecl) -> Optional[ExprPlan]:
    """路线 C 的源码形态 -> ExprPlan; 不匹配返回 None。

    接受:

    * 按值标量形参 + 标量返回类型 (:func:`_shape_of`);
    * 体里**有且仅有一处**自调用 (``callee_kind == "fn"`` 且 ``callee_ref``
      指回自己), 且实参个数与形参一致 —— 实参本身可以是**任意**表达式
      (环里的归纳变量、纯标量算术、别的调用……), 因为调用点就在代码里那个
      固定位置, 求值一次, 不像路线 B 那样要每轮重算;
* 那处自调用位于**每次执行宿主语句都会求值**的位置
      (:func:`_owning_stmt`): 二元/一元运算的操作数、调用实参、赋值的值、
      ``let`` 初值、``return`` 的值、``match`` 的 subject 等。臂守卫、
      表达式臂体、``&&`` / ``||`` 的右操作数等条件位置一律拒绝;
    * 体里没有闭包 (闭包体里的 ``return`` 归闭包自己, 改写就改错了);
    * 体里没有"不在任何环内"的不带标签 ``break`` / ``continue``
      (:func:`_has_stray_jump` —— 那种源码今天压根编不过, 包上包装环之后却
      会"编得过且行为不同");
    * 体里没有绑定遮蔽形参 (:func:`_shadows_param`);
    * **体最后一条语句是 ``return <有值的表达式>``**。这一条是"结果槽必被写
      过"的全盘论证: 体里唯一的出口就是 ``return`` 或者"走到体尾", 而走到
      体尾必然撞上那条 ``return`` —— 所以把 ``return X`` 换成 ``r_k = X;
      break` 之后, **每条路径**都写到了 ``r_k``。尾部不是 return 的体 (例
      如以一个能 ``break`` 出来的环收尾) 拒绝;
    * 体里每处 ``return`` 都带值 (不带值的 return 写不出结果槽)。

    拒绝: 多于一处自调用 (内联哪一处没有唯一定义, 与路线 B 同一个理由);
    闭包; 悬空的 ``break`` / ``continue``; 条件求值的调用位置; 尾部不是
    return; 遮蔽形参; 非标量形参 / 返回值。**不**拦泛型形参: 克隆始终留在
    同一个函数体内, 泛型形参与类型形参在原地解析, 与原体一致。
    """
    shape = _shape_of(fn)
    if shape is None:
        return None
    names, param_types, ret, body = shape

    if any(isinstance(n, _OPAQUE_SCOPE) for n in _walk(body)):
        return None
    if _has_stray_jump(body):
        return None
    if _shadows_param(body, names):
        return None

    fid = fn._typed_id
    found: list[Call] = []
    for n in _walk(body):
        hit = _self_call(fid, n)
        if hit is not None:
            found.append(hit)
    if len(found) != 1:
        return None
    call = found[0]
    if len(call.args) != len(names):
        return None
    call_type = _ann_type_name(call)
    if call_type is None:
        return None

    # 尾部必须是 return, 否则有的路径会掉到体尾而没写结果槽。
    stmts = body.stmts
    if not stmts or not isinstance(stmts[-1], ReturnStmt) \
            or stmts[-1].value is None:
        return None
    if any(
        isinstance(n, ReturnStmt) and n.value is None for n in _walk(body)
    ):
        return None
    # 宿主语句必须是无条件求值的那个位置。
    if _owning_stmt(body, call) is None:
        return None

    return ExprPlan(
        names, param_types, ret, body, call, call_type, _body_labels(body),
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
    的 binding 是错的; 而局部名由 :mod:`cwind_frontend.hygiene` 分配, 保证
    不与程序里任何已有拼写重复, 所以后端按名就能找到, 丢掉是安全的。
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

    局部名一律由 :mod:`cwind_frontend.hygiene` 分配 (``_m<ctx>_x``, 且保证
    不与程序里已有拼写重复), 因此既不会与用户变量撞名, 也不必操心"同名不
    同层"。
    """

    def __init__(self, az: Any, program: Program, fn: FnDecl,
                 params: list[str], param_types: list[str], ret_type: str):
        self.az = az
        self.program = program
        self.fn = fn
        self.params = params
        self.param_types = param_types
        self.ret_type = ret_type
        self.line = fn.line
        self.column = fn.column
        self.fid = fn._typed_id
        self.param_binding = _param_bindings(fn, params)

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

    def loop(self, body: list[Node], label: Optional[str] = None) -> LoopStmt:
        return LoopStmt(
            self.line, self.column, Block(self.line, self.column, body),
            label=label,
        )

    # -- 重绑: 形参引用 -> 本层 q_k -------------------------------------
    def rebind(self, expr: Node, qnames: list[str]) -> Node:
        """深拷贝一份, 把对**形参**的引用改写为对本层 ``q_k`` 的引用。

        ``expr`` 必须是本模块自己的节点 (调用方负责传副本)。按名字改写,
        所以匹配阶段已经排除了"体里有绑定遮蔽形参"
        (:func:`_shadows_param`)。
        """
        mapping = dict(zip(self.params, qnames))
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

    def clone(self, expr: _T) -> _T:
        """路线 C 的拷贝: 清 id, 留给 ``_assign_synthetic_ids`` 重编号。"""
        return _fresh_copy(expr)

    def clone_rebind(self, expr: Node, qnames: list[str]) -> Node:
        return self.rebind(_fresh_copy(expr), qnames)

    # -- 前缀改写: 基例早返回 -------------------------------------------
    def prefix_for(self, prefix: list[Node], level: int, acc_name: str,
                   v_name: Optional[str], ret_t: str,
                   qnames: list[str]) -> list[Node]:
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
        for stmt in prefix:
            clone = copy.deepcopy(stmt)
            self.rebind(clone, qnames)
            _map_stmt_lists(
                clone, self._ret_to_acc(level, acc_name, v_name, ret_t)
            )
            out.append(clone)
        return out

    def _ret_to_acc(self, level: int, acc_name: str, v_name: Optional[str],
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

    def _ret_to_slot(self, slot: str, label: str, ret_t: str) -> "callable":
        """路线 C: ``return X`` -> ``r_k = X; break 'label``。

        break **必须带标签**: 体里可能嵌着用户自己的环, 不带标签会跳出
        最近的那个用户环, 正好跳错层 (用户自己那些不带标签的 break /
        continue 不用动 —— 包装环是**副本**的最外层环, 最内层仍然是用户
        的环; 见模块 docstring 里关于措辞的那段)。
        """
        def make(ret: ReturnStmt) -> list[Node]:
            assert ret.value is not None
            return [
                self.assign(self.local(slot, ret_t), ret.value, ret_t),
                BreakStmt(ret.line, ret.column, label=label),
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


def _splice_before(node: Node, target: Node, new_stmts: list[Node]) -> bool:
    """把 *new_stmts* 插到语句列表里 *target* 的前面。

    只按身份找, 不用路径: 语句只出现在 ``Block.stmts`` 里, 而 ``_owning_stmt``
    已经保证 *target* 确实是某条语句 (而不是 ``Call.args`` 里的 Arg)。
    """
    for f in _dc_fields(node):
        if f.name in ("line", "column"):
            continue
        value = getattr(node, f.name, None)
        if isinstance(value, list):
            for i, item in enumerate(value):
                if item is target:
                    value[i:i] = new_stmts
                    return True
            for item in value:
                if isinstance(item, Node) and _splice_before(
                    item, target, new_stmts
                ):
                    return True
        elif isinstance(value, Node):
            if _splice_before(value, target, new_stmts):
                return True
    return False


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
# 路线 B 的驱动
# --------------------------------------------------------------------------

def _depth_of(fn: FnDecl) -> int:
    spec = (fn.opt or {}).get("inline_loop") or {}
    value = spec.get("recursive")
    return DEFAULT_DEPTH if value is None else int(value)


def _emit_loop(az: Any, program: Program, fn: FnDecl, plan: Plan,
               depth: int) -> None:
    b = _Builder(az, program, fn, plan.params, plan.param_types, plan.ret_type)
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
            plan.prefix, k, acc[k], v[k - 1] if k > 0 else None, ret_t, q[k]
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


# --------------------------------------------------------------------------
# 路线 C 的驱动
# --------------------------------------------------------------------------

def _emit_expr(az: Any, program: Program, fn: FnDecl, plan: ExprPlan,
               depth: int) -> None:
    b = _Builder(az, program, fn, plan.params, plan.param_types, plan.ret_type)
    ret_t = plan.ret_type
    np = len(plan.params)
    # 每层一份结果槽 + 一份形参绑定 + 一个包装环标签, 全部走卫生名。
    # slot / label 只有 1..depth 用得上 (第 0 层就是函数自己); q 多留一行
    # 是为了让 "下一层是 k+1" 的下标写法与路线 B 一致, q[0] 本身不声明也不读
    # (见 :func:`names_at`)。
    slot = [b.fresh("r") for _ in range(depth)]
    q = [[b.fresh("q") for _ in range(np)] for _ in range(depth + 1)]
    label = [b.fresh("il") for _ in range(depth)]
    for name in label:
        # 标签相撞会让用户 ``break 'x`` 命中包装环 (或者反过来), 后端会
        # 报"label does not name an enclosing loop" 或者更糟 —— 干脆不碰。
        if name in plan.body_labels:
            raise _EmitAbort("generated loop label collides with a user one")

    def names_at(k: int) -> list[str]:
        """第 k 层里"形参"叫什么。

        第 0 层就是函数自己, 形参还是**原来那几个**; 第 1..N 层才是本模块
        新 let 出来的 ``q_k``。忘了这一层区分, 第 0 层读形参会读到一组从没
        声明过的名字 (后端报 undeclared variable, 算不错但编不过)。
        """
        return plan.params if k == 0 else q[k]

    def bind_args(k: int) -> list[Node]:
        """``q_{k+1}[i] = <自调用的第 i 个实参>``, 在第 k 层的作用域里读。"""
        return [
            b.let(
                q[k + 1][i], plan.param_types[i],
                b.clone_rebind(plan.call.args[i].value, names_at(k)),
                mutable=True,
            )
            for i in range(np)
        ]

    def level_block(k: int) -> list[Node]:
        """内联的第 k 层 (1..depth) 的语句。

        顺序要紧: 先重绑形参, 再定位自调用与宿主语句, 再**先插包装环**、
        后改写 ``return`` —— 反过来的话, 宿主语句若是 ``return`` 本身,
        改写会把它从列表里换掉, 插入就找不到锚点了。
        """
        dup = b.clone(plan.body)
        b.rebind(dup, names_at(k))
        site = _sole_self_call(b.fid, dup)
        if site is None:
            raise _EmitAbort("self-call is not unique in the cloned body")
        if k < depth:
            owner = _owning_stmt(dup, site)
            if owner is None:
                raise _EmitAbort("self-call has no unconditional statement")
            nested: list[Node] = bind_args(k)
            nested.append(b.let(
                slot[k], ret_t, _zero(b.line, b.column, ret_t), mutable=True,
            ))
            nested.append(b.loop(level_block(k + 1), label=label[k]))
            if not _splice_before(dup, owner, nested):
                raise _EmitAbort("statement owning the self-call vanished")
        _map_stmt_lists(dup, b._ret_to_slot(slot[k - 1], label[k - 1], ret_t))
        if any(isinstance(n, ReturnStmt) for n in _walk(dup)):
            raise _EmitAbort("a return survived the rewrite")
        if k == depth:
            # 最深一层: 唯一剩下的**真实**自递归调用。
            real = b.self_call(
                [b.clone_rebind(a.value, names_at(k)) for a in plan.call.args],
                plan.call_type,
            )
            if not _subst_call(dup, site, real):
                raise _EmitAbort("self-call vanished")
        else:
            if not _subst_call(dup, site, b.local(slot[k], ret_t)):
                raise _EmitAbort("self-call vanished")
        return dup.stmts

    # 第 0 层就是函数自己: 形参不重绑, ``return`` 照旧是真返回 (只有内联
    # 的副本才需要"写槽 + 跳出"), 唯一改动是把那处自调用换成第 1 层。
    body = b.clone(plan.body)
    site = _sole_self_call(b.fid, body)
    if site is None:
        raise _EmitAbort("self-call is not unique in the cloned body")
    owner = _owning_stmt(body, site)
    if owner is None:
        raise _EmitAbort("self-call has no unconditional statement")
    head: list[Node] = bind_args(0)
    head.append(b.let(
        slot[0], ret_t, _zero(b.line, b.column, ret_t), mutable=True,
    ))
    head.append(b.loop(level_block(1), label=label[0]))
    if not _splice_before(body, owner, head):
        raise _EmitAbort("statement owning the self-call vanished")
    if not _subst_call(body, site, b.local(slot[0], ret_t)):
        raise _EmitAbort("self-call vanished")

    for node in body.stmts:
        az._assign_synthetic_ids(node)
    fn.body = Block(b.line, b.column, body.stmts)


# --------------------------------------------------------------------------
# 驱动
# --------------------------------------------------------------------------

def _shape_warning(fn: FnDecl, why: str = "") -> str:
    return (
        f"'{fn.name}': #[opt(inline_loop)] does not apply to this function's "
        "shape" + why + " (expected a scalar-returning function whose tail "
        "return is a '+' chain containing self-calls, with the base cases in "
        "straight-line statements before it; or a function whose body ends in "
        "a return, holds exactly one self-call in a position evaluated once "
        "per execution of that statement, and lets no let-binding or loop "
        "label shadow a parameter); the body is left unchanged"
    )


def inline_loop_functions(az: Any, program: Program) -> None:
    """对带 ``#[opt(inline_loop(...))]`` 的自由函数做递归内联 (原地)。

    先试路线 B (尾 ``'+'`` 链), 不认再试路线 C (表达式位的唯一自调用);
    两条都只**认一种形状**, 认不出就一条告警了事, 函数体一个字节都不动。
    """
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
        if depth > MAX_DEPTH:
            az._record_warning(
                f"'{fn.name}': #[opt(inline_loop(recursive = {depth}))] asks "
                f"for more than the {MAX_DEPTH}-level limit, so the body is "
                "left unchanged",
                fn.line, fn.column,
            )
            continue
        plan = match_inline_loop(fn)
        if plan is not None:
            _try_emit(az, program, fn, plan, depth, _emit_loop)
        else:
            expr = match_inline_expr(fn)
            if expr is None:
                az._record_warning(_shape_warning(fn), fn.line, fn.column)
                continue
            _try_emit(az, program, fn, expr, depth, _emit_expr)


def _try_emit(az: Any, program: Program, fn: FnDecl, plan: Any, depth: int,
              emit: Any) -> None:
    """发射, 把内部断言失败降级成一条告警。

    两条路线的发射都只在**最后一步**才把新体挂回 ``fn.body``, 所以断言失败
    的那一刻原体还完整地挂在函数上 —— 转成告警即可 (改写留到下一次修)。
    半途改写出来的体比不改写更糟, 所以这里绝不"重试一次"。
    """
    try:
        emit(az, program, fn, plan, depth)
    except _EmitAbort as exc:
        az._record_warning(
            _shape_warning(fn, f" ({exc})"), fn.line, fn.column,
        )
