"""``#[opt(inline_loop(...))]`` —— 手工递归内联 (数据驱动)。

CWind 不做源码级内联; 这个属性把尾递归折成累加器环, 再把环套 N 层:
顶部 N 层递归不再发真实调用, 第 N+1 层及以下才留。LLVM 的内联器对
``caller == callee`` 的调用整体跳过, 而 cwindc 直接出 obj 吃不到 GCC 的
IPA 递归内联, 所以这一层只能在前端做。

这里只固化**属性校验**与**形状拒绝**路径 (harness 的单文件流水线拿不到
隐式 prelude, 所以用例都不带 ``println!``):

* 形状不匹配时函数体**不动**, 只给一条告警 —— 宁可什么都不做, 也不能
  半途改写出一个语义不同的函数;
* 参数名写错之类要在属性层就拒。

展开本身的验收 (数值逐值对拍 + IR 嵌套环层数) 在 ``mvp/test-c`` 的
``codegen_inline_loop`` 流水线用例里, 按 IR 断言。
"""

import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

from harness import CaseAssertionsMixin, iter_pipeline_cases


class InlineLoopCases(CaseAssertionsMixin):
    """``#[opt(inline_loop)]`` 的校验与拒绝路径, 逐个 case 对拍。"""


def _bind_cases() -> None:
    for name in iter_pipeline_cases("inline_loop"):
        setattr(
            InlineLoopCases,
            f"test_{name}",
            (lambda case: lambda self: self.assert_case("inline_loop", case))(name),
        )


_bind_cases()


if __name__ == "__main__":
    import unittest

    unittest.main()
