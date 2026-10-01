"""``#[opt(...)]`` —— codegen 选项的命名空间。

与 ``#[inline]`` 分开的原因: ``#[inline]`` 改变的是**LLVM 内联器**的决策
(落成函数属性, 由 ``default<O*>`` 管线里的内联器执行), 而 ``#[opt]`` 是
给后续 codegen 选项预留的位置 —— 那些选项要么由 CWind 自己做变换, 要么
需要一条管线之外的额外 pass。

目前只认一个子选项:

* ``#[opt(inline_loop(recursive = N))]`` —— **手工递归内联**。参数照常解析、
  校验、落进 :attr:`FnDecl.opt` (JSON 里可见); 消费方是
  :mod:`cwind_frontend.sa.optimize.inline_loop`, 它按源码形状把顶部 N 层
  递归在前端展开掉。形状之外函数体**原样不动**, 只给一条告警。

为什么值得加 (实测, ``assets/bench``, 出货配置 ``-O3 --lto fat
--target-cpu native --fast-math``, 取 5 次最好成绩):

.. code-block:: text

    bernoulli30.wind   plain                      2026 ms   1.00x
    opt_bernoulli30    只标 binomial               773 ms   2.62x
    opt2_bernoulli30   再加标 bernoulli            788 ms   2.57x

    fib42.wind         plain                       605 ms   1.00x
                      #[opt(inline_loop)]         307 ms   1.97x

**收益来自"链式递归"(每节点少数子节点、纵深), 不来自"扇出型递归"。**
``binomial`` 是 ``return f(a) + f(b)`` 的尾递归链 —— 展开后顶部若干层变成嵌套
环, 真实调用少一个数量级, 这是 2.6x 的全部来源。``bernoulli`` 是扇出型
(每节点约 n/2 个子节点, 多数直接命中基例), 顶部 8 层相对**总**调用数是零头,
再给它套 8 层包装环只剩代码膨胀的成本 —— 实测反而慢约 2%。

所以判断要不要加标注, 看的是**递归的形状**而不是它有多"递归": 纵深换来的
调用数下降才是收益, 横向铺开换不来。形状不匹配时会告警并原样保留函数体。

写成嵌套形状而不是扁平 flag, 是为了让每个子选项能自带参数表; 后续加
循环展开类选项时不必再发明一套平铺语法。
"""

from __future__ import annotations

from typing import Any

from ..ast_components.ast import FnDecl
from .model import (
    CALL,
    FLAG,
    ITEM,
    PAIR,
    Attr,
    DuplicateAttribute,
    InvalidArgument,
    UnexpectedArgument,
    WrongItemKind,
)
from .registry import AttrProc, ProcCtx, register

#: 子选项名 -> 它接受的 ``key = value`` 参数 (全部**可选**, 缺省走消费方
#: 侧的默认值; ``#[opt(inline_loop)]`` 与 ``#[opt(inline_loop(recursive
#: = N))`` 因此是同一个选项的两种写法)。
_KNOWN_OPTIONS: dict[str, frozenset[str]] = {
    "inline_loop": frozenset({"recursive"}),
}


def _positive_int(value: str | None) -> int | None:
    try:
        parsed = int(str(value), 10)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _apply_opt(ctx: ProcCtx, item: Any, attr: Attr) -> None:
    if not isinstance(item, FnDecl):
        raise WrongItemKind(
            "the 'opt' attribute only applies to a function",
            attr.line, attr.column,
        )
    if not attr.args:
        raise UnexpectedArgument(
            "'opt' needs at least one option "
            f"(known: {', '.join(sorted(_KNOWN_OPTIONS))})",
            attr.line, attr.column,
        )
    for arg in attr.args:
        # ``#[opt(inline_loop)]`` (裸词) 收集成 FLAG, ``#[opt(inline_loop(
        # recursive = 8))]`` 收集成 CALL —— 两种写法都要认。
        if arg.kind == FLAG:
            name = arg.name
            inner_args: tuple = ()
        elif arg.kind == CALL:
            name = arg.name
            inner_args = arg.args
        else:
            raise UnexpectedArgument(
                f"unsupported 'opt' entry '{arg.name}' (expected an "
                f"option, one of: {', '.join(sorted(_KNOWN_OPTIONS))})",
                *arg.where(),
            )
        if name not in _KNOWN_OPTIONS:
            raise UnexpectedArgument(
                f"unknown 'opt' option '{name}' "
                f"(expected one of: {', '.join(sorted(_KNOWN_OPTIONS))})",
                *arg.where(),
            )
        if name in item.opt:
            raise DuplicateAttribute(
                f"duplicate 'opt({name})' on '{item.name}'",
                *arg.where(),
            )
        known = _KNOWN_OPTIONS[name]
        for inner in inner_args:
            if inner.kind != PAIR or inner.name not in known:
                raise UnexpectedArgument(
                    f"unsupported '{name}' argument '{inner.name}' "
                    f"(expected one of: {', '.join(sorted(known))})",
                    *inner.where(),
                )
        params: dict[str, Any] = {}
        for inner in inner_args:
            depth = _positive_int(inner.value)
            if depth is None:
                raise InvalidArgument(
                    f"'{name}({inner.name} = ...)' expects a "
                    "non-negative integer",
                    *inner.where(),
                )
            params[inner.name] = depth
        item.opt[name] = params


register(AttrProc(
    name="opt",
    positions=frozenset({ITEM}),
    apply=_apply_opt,
    redirect="the 'opt' attribute applies to functions only",
    doc="codegen options (currently #[opt(inline_loop(recursive = N))], "
        "hand-rolled recursive inlining)",
))
