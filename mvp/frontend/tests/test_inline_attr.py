"""``#[inline]`` / ``#[opt(...)]`` —— 内联档位属性 (数据驱动)。

CWind 不做源码级内联: 档位只是给 LLVM 内联器的一条建议, 真正的内联决策
与代码膨胀控制走内联器自己的成本模型 (``default<O*>`` 管线里那个)。
前端只负责三件事 —— 校验写法、把档位记进 :attr:`FnDecl.inline`、把
``#[opt]`` 的参数按源码形状存进 :attr:`FnDecl.opt`。

三档与 LLVM 函数属性一一对应:

* ``#[inline]``          -> ``inlinehint``   建议
* ``#[inline(always)]``  -> ``alwaysinline`` 强制
* ``#[inline(never)]``   -> ``noinline``     禁止

``#[opt(inline_loop(recursive = N))]`` 的参数同样只是落盘; 消费方是
``sa/optimize/inline_loop.py`` (前端手工递归内联), 它的形状匹配与拒绝路径
由 ``test_inline_loop.py`` / ``cases/inline_loop/`` 覆盖。

用例全是 ``cases/inline/`` 下的 ``.wind`` + ``.json`` 对, 这里只负责驱动。
后端侧的映射 (属性真的挂上了、真的影响内联) 由 ``mvp/test-c`` 的
``codegen_inline_attr`` 流水线用例按 IR 断言。
"""

import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

from harness import CaseAssertionsMixin, iter_pipeline_cases


class InlineAttrCases(CaseAssertionsMixin):
    """``#[inline]`` / ``#[opt]`` 的写法校验, 逐个 case 对拍。"""


def _bind_cases() -> None:
    for name in iter_pipeline_cases("inline"):
        setattr(
            InlineAttrCases,
            f"test_{name}",
            (lambda case: lambda self: self.assert_case("inline", case))(name),
        )


_bind_cases()


if __name__ == "__main__":
    import unittest

    unittest.main()
