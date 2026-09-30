"""合成名分配 (:mod:`cwind_frontend.hygiene`) 必须避开程序里已有的拼写。

为什么这是测试而不是注释: ``_m<ctx>_<name>`` 看着像"用户打不出来", 其实
**打得出来** —— ``let _m1000007_acc: Int32 = 5;`` 能编译能运行。所以合成名
一旦与用户绑定同名, 捕获就是静默误编译。这里直接对分配器断言那条性质,
不去端到端碰运气: 端到端要靠"std 的 desugar 消耗了几个计数器值"才能让诱饵
刚好落在要测的区间, std 一改就失效。
"""

import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

from cwind_frontend.hygiene import (
    NameAllocator,
    allocator_on,
    macro_context_mangle,
    suffixed_mangle,
)


class NameAllocatorTests(unittest.TestCase):
    def test_skips_a_spelling_that_is_already_taken(self):
        taken = {"_m1_x", "_m2_x"}
        alloc = NameAllocator(taken, macro_context_mangle)
        self.assertEqual(alloc.fresh("x"), "_m3_x")
        self.assertIn("_m3_x", taken)

    def test_only_the_full_spelling_matters(self):
        # 占用判的是**完整拼写**, 不是计数器号 —— 同一个号配不同 base 是
        # 两个不同的名字, 不该互相让位 (否则纯属浪费计数器)。
        alloc = NameAllocator({"_m1_x"}, macro_context_mangle)
        self.assertEqual(alloc.fresh("y"), "_m1_y")

    def test_marks_what_it_hands_out_so_later_ones_differ(self):
        alloc = NameAllocator(set(), macro_context_mangle)
        first = alloc.fresh("acc")
        second = alloc.fresh("acc")
        self.assertNotEqual(first, second)

    def test_terminates_against_a_large_taken_set(self):
        # 前 50 个候选全被占, 仍必须收敛而不是死循环。
        blocked = {f"_m{n}_x" for n in range(1, 51)}
        alloc = NameAllocator(set(blocked), macro_context_mangle)
        self.assertEqual(alloc.fresh("x"), "_m51_x")

    def test_start_offset_keeps_clear_of_another_numbering_space(self):
        # desugar 的 1_000_000 起点就是用来避开 parse 期的宏上下文(从 0 数)。
        alloc = NameAllocator({"_m5_x"}, macro_context_mangle, start=1_000_000)
        self.assertEqual(alloc.fresh("x"), "_m1000001_x")

    def test_suffixed_mangle_falls_back_to_a_counter(self):
        taken = {"__cwpm_h", "__cwpm_h_2"}
        alloc = NameAllocator(taken, suffixed_mangle("__cwpm_"))
        self.assertEqual(alloc.fresh("h"), "__cwpm_h_3")

    def test_allocator_on_memoizes_so_the_counter_keeps_moving(self):
        class Holder:
            pass

        holder = Holder()
        first = allocator_on(holder, "_a", set(), macro_context_mangle, start=10)
        second = allocator_on(holder, "_a", set(), macro_context_mangle, start=10)
        self.assertIs(first, second)
        self.assertEqual(first.fresh("x"), "_m11_x")
        self.assertEqual(second.fresh("x"), "_m12_x")


class MangledShapeIsTypableTests(unittest.TestCase):
    """记录那个前提: mangled 形状本身**挡不住**用户代码。"""

    def test_macro_mangle_output_is_a_plain_identifier(self):
        from cwind_frontend.parser.core import ParserCore

        name = ParserCore.macro_mangle(1_000_007, "acc")
        self.assertEqual(name, "_m1000007_acc")
        # 全是标识符字符 —— 所以必须靠"检查占用"而不是靠"打不出来"。
        self.assertTrue(name.replace("_", "").isalnum())


if __name__ == "__main__":
    unittest.main()
