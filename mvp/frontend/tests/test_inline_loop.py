"""``#[opt(inline_loop(...))]`` —— 手工递归内联 (数据驱动)。

CWind 不做源码级内联; 这个属性把顶部 N 层递归在前端展开掉 (LLVM 的内联器
对 ``caller == callee`` 的调用整体跳过, 而 cwindc 直接出 obj, 吃不到 GCC 的
IPA 递归内联, 所以这一层只能自己做)。两条路线:

* **路线 B** —— 尾 return 是 ``+`` 链: 先折成累加器环, 再把环套 N 层;
* **路线 C** —— 体里有且仅有一处自调用, 位置在**总是求值**的表达式位
  (``bernoulli`` 那种环里的复合表达式中间): 把那处调用就地换成内联副本。

这里固化**属性校验**与**接受 / 拒绝**的边界 (harness 的单文件流水线拿不到
隐式 prelude, 所以用例都不带 ``println!``):

* 接受��形状用 ``warnings_exact: []`` 锁住 —— "一条告警都没有" 就是
  "展开真的发生了";
* 不接受的形状**函数体一个字都不动**, 只给一条告警 (必要时与未标注版逐
  字节对比 ``cwindf --unparse`` 的输出来证明这一点) —— 宁可什么都不做,
  也不能半途改写出一个语义不同的函数。

展开本身的验收 (数值逐值对拍 + IR 嵌套环层数) 在 ``mvp/test-c`` 的
``codegen_inline_loop`` / ``codegen_inline_loop_expr`` 流水线用例里,
按 IR 断言。
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
