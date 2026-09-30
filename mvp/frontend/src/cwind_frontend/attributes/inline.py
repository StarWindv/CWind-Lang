"""``#[inline]`` / ``#[inline(always)]`` / ``#[inline(never)]``.

内联档位标注。CWind 不自己做源码级内联 —— 档位落成
:attr:`FnDecl.inline`,后端给 LLVM 函数挂对应的函数属性
(``inlinehint`` / ``alwaysinline`` / ``noinline``),真正的内联决策与
代码膨胀控制交给 LLVM 内联器 (``opt --help-hidden`` 里的
``--inlinehint-threshold`` 就是 LLVM 侧对应 ``hint`` 的那把尺子)。

三个档位:

* ``#[inline]``          -> ``inlinehint``   建议, 由内联器按成本自行判断
* ``#[inline(always)]``  -> ``alwaysinline`` 强制内联
* ``#[inline(never)]``   -> ``noinline``     禁止内联

``always`` 与 ``never`` 互斥, 同时写即报错。

注意 ``always`` 是**强制**, 不是"更礼貌的建议": LLVM 的 alwaysinline
绕开成本模型, 函数体再大也照内联。反过来裸 ``#[inline]`` 只是 hint,
完全可能被内联器放弃 —— 所以想给某个函数开内联却没生效, 先看它是不是
压根没被调用, 或者体量大到超了阈值。
"""

from __future__ import annotations

from typing import Any

from ..ast_components.ast import FnDecl
from .model import (
    FLAG,
    ITEM,
    Attr,
    DuplicateAttribute,
    UnexpectedArgument,
    WrongItemKind,
)
from .registry import AttrProc, ProcCtx, register

#: 合法档位标志 -> ``FnDecl.inline`` 存的值。
_MODES: dict[str, str] = {"always": "always", "never": "never"}


def _apply_inline(ctx: ProcCtx, item: Any, attr: Attr) -> None:
    if not isinstance(item, FnDecl):
        raise WrongItemKind(
            "the 'inline' attribute only applies to a function",
            attr.line, attr.column,
        )
    if item.extern_abi is not None:
        raise WrongItemKind(
            "the 'inline' attribute cannot be applied to an extern "
            "declaration (there is no CWind body to inline)",
            attr.line, attr.column,
        )
    # 重复标注: 只有写了参数的才算占位, 裸 #[inline] 可以再写一个带参的
    # (覆盖成更具体的一档), 反过来不行 —— 语义上后者才是用户的最终意图。
    if item.inline in ("always", "never"):
        raise DuplicateAttribute(
            f"duplicate 'inline' attribute on '{item.name}' "
            f"(already marked #[inline({item.inline})])",
            attr.line, attr.column,
        )

    mode = ""
    for arg in attr.args:
        if arg.kind != FLAG:
            raise UnexpectedArgument(
                f"unsupported 'inline' argument '{arg.name}' "
                "(expected #[inline], #[inline(always)] or "
                "#[inline(never)])",
                *arg.where(),
            )
        if arg.name not in _MODES:
            raise UnexpectedArgument(
                f"unknown 'inline' mode '{arg.name}' "
                "(expected 'always' or 'never')",
                *arg.where(),
            )
        if mode and mode != arg.name:
            raise UnexpectedArgument(
                f"'inline({arg.name})' conflicts with 'inline({mode})' "
                "(the two modes are mutually exclusive)",
                *arg.where(),
            )
        mode = arg.name
    item.inline = _MODES[mode] if mode else "hint"


register(AttrProc(
    name="inline",
    positions=frozenset({ITEM}),
    apply=_apply_inline,
    redirect="the 'inline' attribute applies to functions only",
    doc="suggest inlining this function (bare / always / never)",
))
