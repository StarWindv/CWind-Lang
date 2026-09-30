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
