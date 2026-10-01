"""bug-93/94/95: the three fixes' shared substrate — type-spelling handling.

三者都落在"**类型拼写**怎么处理"这一个地基上, 但各自修的是不同的洞:

* **bug-93** ``_unify_generic`` 沿类型构造子逐位统一, 裸指针/数组/函数指针
  拼写里的推断变量才绑得上; 引用与裸指针互为 coercion site。
* **bug-94** 类型参数作内联数组元素: 声明放行 (SA) + 布局按实例算 (后端),
  以及**拼写代入**要能看见 ``[T; N]`` 里的 T (前端 ``_subst_leaf_name``)。
* **bug-95** 见 ``test_cwtype.c`` 与 ``codegen_bug95.wind`` (纯后端别名)。

本模块只钉前端的**拼写口径** —— 数值与别名那两半在别处。
"""

from __future__ import annotations

import unittest

from cwind_frontend.sa.types import (
    _is_ref,
    _split_ptr_pointee,
    _subst_leaf_name,
)


class PointerPointeeTests(unittest.TestCase):
    """bug-93: 裸指针拼写要能拆出被指类型。"""

    def test_pointee(self):
        self.assertEqual(_split_ptr_pointee("*mut T"), "T")
        self.assertEqual(_split_ptr_pointee("*const T"), "T")
        self.assertEqual(_split_ptr_pointee("*mut Pt<Int32>"), "Pt<Int32>")

    def test_non_pointer(self):
        """引用不是裸指针拼写 (ref 由 `_split_ref_prefix` 那一支处理)。"""
        self.assertIsNone(_split_ptr_pointee("&mut Int32"))
        self.assertIsNone(_split_ptr_pointee("Int32"))
        self.assertIsNone(_split_ptr_pointee("fn(*mut T)"))
        self.assertIsNone(_split_ptr_pointee("[T; 2]"))

    def test_is_ref_agrees(self):
        self.assertFalse(_is_ref("*mut T"))
        self.assertTrue(_is_ref("&mut T"))


class LeafSubstTests(unittest.TestCase):
    """bug-94: ``_type_str`` 用的单步代入, 必须看得见扁平拼写里的形参。

    ``tag: [T; 2]`` 的 ``Type.name`` 是**整个字符串** ``"[T; 2]"``, 不是
    ``subst`` 的 key, 所以旧的整名查表 (`subst.get(t.name, t.name)`) 静默
    空转, 字面 ``"T"`` 一路走到后端变成
    ``unsupported array element type: T``。
    """

    def test_flat_array_spellings(self):
        self.assertEqual(
            _subst_leaf_name("[T; 2]", {"T": "Int32"}), "[Int32; 2]"
        )
        self.assertEqual(
            _subst_leaf_name("[Box<T>; 2]", {"T": "Int32"}), "[Box<Int32>; 2]"
        )

    def test_flat_pointer_and_fn_spellings(self):
        self.assertEqual(_subst_leaf_name("*mut T", {"T": "Int32"}), "*mut Int32")
        self.assertEqual(
            _subst_leaf_name("*const T", {"T": "Int32"}), "*const Int32"
        )
        self.assertEqual(
            _subst_leaf_name("fn(*mut T)", {"T": "Int32"}), "fn(*mut Int32)"
        )

    def test_bare_name_is_single_step(self):
        """**单步**: 结构化字面量的字段代入是按位的, 绝不追链。

        追链会把互换实例化 `Pair<B, A> { self.second, self.first }` 变成恒等
        (`A -> B -> A`), 于是字段类型"没变", 两个字段都报失配
        (codegen_structfull 就是这么回归的)。追链是单态化
        (`_subst_type_str`) 的职责, 不是这里的。
        """
        self.assertEqual(
            _subst_leaf_name("A", {"A": "B", "B": "A"}), "B"
        )
        self.assertEqual(_subst_leaf_name("Pair", {"A": "X"}), "Pair")

    def test_token_boundary(self):
        """形参叫 T 时 ``T2``/``Tx`` 不能被误命中 (子串式实现会)。"""
        self.assertEqual(_subst_leaf_name("[T2; 2]", {"T": "Int32"}), "[T2; 2]")
        self.assertEqual(_subst_leaf_name("[Tx; 2]", {"T": "Int32"}), "[Tx; 2]")

    def test_non_generic_untouched(self):
        """非泛型代码零行为变化: 没有 key 就原样返回。"""
        self.assertEqual(_subst_leaf_name("[Int32; 2]", {"T": "Int64"}),
                         "[Int32; 2]")
        self.assertEqual(_subst_leaf_name("Int32", {"T": "Int64"}), "Int32")
        self.assertEqual(_subst_leaf_name("[T; 2]", {}), "[T; 2]")

    def test_builtin_prefix_preserved(self):
        """todo-154: 存储形带 FQN 前缀时代入不动它 (剥前缀是调用方的事)。"""
        self.assertEqual(
            _subst_leaf_name("std::builtins::Vector", {}),
            "std::builtins::Vector",
        )


if __name__ == "__main__":
    unittest.main()
