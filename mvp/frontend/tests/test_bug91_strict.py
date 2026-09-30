"""bug-91: ``--strict`` 关闭自动借用 (数据驱动).

背景: 默认模式下 (bug-64) ``&T`` 形参按 Rust auto-ref 语义接收裸值实参,
``take(s)`` 等价于 ``take(&s)``。该自动借用曾在后端有两处地址保真缺陷
(``&T`` 返回被重新装箱、纯右值按实参自身宽度 spill), 均已修复; 但
「借用是否发生」这件事本身仍应在源码里显式可见 —— ``--strict`` 把自动
借用整条关掉: ``&T`` 形参只接受显式 ``&expr`` / ``&mut expr``, 裸值按
类型不匹配报错。

用例全是 ``cases/bug91/`` 下的 ``.wind`` + ``.json`` 对: 期望里用
``"strict": true`` 打开开关 (见 harness 的 expectation schema), 这里只
负责驱动。

需要 std 前置 (println! 之类) 的两条性质不走这个 harness —— 它的
``run_pipeline`` 不注入隐式 prelude:

* 后端地址保真 (``&T`` 返回 / 右值宽度 / ``&mut`` 写穿) 与 bug-92 的
  sizeof 实占: ``mvp/test-c`` 的 ``codegen_bug91_92`` 端到端 fixture;
* std 自身在 ``--strict`` 下可编译: ``codegen_strict_mode`` 流水线用例。
"""

import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

from harness import CaseAssertionsMixin, iter_pipeline_cases


class StrictBorrowCases(CaseAssertionsMixin):
    """``--strict`` 的开/关行为, 逐个 case 对拍。"""


def _bind_cases() -> None:
    for name in iter_pipeline_cases("bug91"):
        setattr(
            StrictBorrowCases,
            f"test_{name}",
            (lambda case: lambda self: self.assert_case("bug91", case))(name),
        )


_bind_cases()


if __name__ == "__main__":
    import unittest

    unittest.main()
