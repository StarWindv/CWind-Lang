"""bug-90 regression: type-argument substitution must see inside flat spellings.

泛型实参代入要认两种类型拼写:

* **结构化** —— ``Option<Int32>`` / ``std::builtins::Vector<Int>``: 基名与实参
  列表分开, 走 ``_split_args`` 递归;
* **扁平** —— ``*mut T`` / ``*const T`` / ``[T; N]`` / ``[Box<T>; 2]`` /
  ``fn(*mut T)``: 类型实参**全在字符串里**, 没有 ``<`` 可递归。

``_subst_type_str`` 原先只处理结构化一支 (以及裸名链 ``T -> U -> Int``),
扁平拼写原样返回, 于是单态化后 `*mut T` 里的 ``T`` 漏出去。这与后端
``cg_type_name_of`` 只做整名匹配是同一个洞的两半 (见 bug-90 台账)。

另一半症状更隐蔽: 按 ``<`` 切分再重建会把 ``[Box`` 整个当成基名, 产出
``[Box<Int32>`` —— ``; 2]`` 被吃掉。按标识符 token 替换则不会。
"""

from __future__ import annotations

import unittest

from cwind_frontend.sa.types import _subst_type_str


class FlatSpellingSubstTests(unittest.TestCase):
    def test_pointer_pointee(self):
        self.assertEqual(_subst_type_str("*mut T", {"T": "Int32"}), "*mut Int32")
        self.assertEqual(
            _subst_type_str("*const T", {"T": "Int32"}), "*const Int32"
        )

    def test_pointer_to_generic(self):
        self.assertEqual(
            _subst_type_str("*mut Vec<T>", {"T": "Int32"}), "*mut Vec<Int32>"
        )

    def test_array_element(self):
        self.assertEqual(_subst_type_str("[T; 2]", {"T": "Int32"}), "[Int32; 2]")
        self.assertEqual(
            _subst_type_str("[T; 4]", {"T": "Vector<Int64>"}), "[Vector<Int64>; 4]"
        )

    def test_array_of_generic_keeps_nesting(self):
        """bug-90 台账记的拼坏形态: ``[Box<T>; 2]`` -> ``[Box<Int32>``.

        结构化重建会把基名截成 ``[Box`` 并吃掉 ``; 2]``; token 替换不会。
        """
        self.assertEqual(
            _subst_type_str("[Box<T>; 2]", {"T": "Int32"}), "[Box<Int32>; 2]"
        )

    def test_fn_signature(self):
        self.assertEqual(
            _subst_type_str("fn(*mut T)", {"T": "Int32"}), "fn(*mut Int32)"
        )

    def test_ref_prefix_survives(self):
        self.assertEqual(
            _subst_type_str("&*mut T", {"T": "Int32"}), "&*mut Int32"
        )

    def test_token_boundary(self):
        """形参叫 T 时 T2/Tx 不能被误命中 (子串式实现会)。"""
        self.assertEqual(_subst_type_str("T2", {"T": "Int32"}), "T2")
        self.assertEqual(_subst_type_str("Tx", {"T": "Int32"}), "Tx")
        self.assertEqual(
            _subst_type_str("T2<T>", {"T": "Int32"}), "T2<Int32>"
        )

    def test_two_params_in_one_flat_spelling(self):
        self.assertEqual(
            _subst_type_str(
                "fn(*mut K) -> Vec<V>", {"K": "String", "V": "Int32"}
            ),
            "fn(*mut String) -> Vec<Int32>",
        )

    def test_structured_unaffected(self):
        """结构化一支的既有行为不得回退 (含 todo-154 的前缀保留)。"""
        self.assertEqual(_subst_type_str("Option<T>", {"T": "Int32"}), "Option<Int32>")
        self.assertEqual(
            _subst_type_str("Option<Vec<T>>", {"T": "Int32"}), "Option<Vec<Int32>>"
        )
        self.assertEqual(
            _subst_type_str("std::builtins::Vector<T>", {"T": "Int32"}),
            "std::builtins::Vector<Int32>",
        )
        self.assertEqual(
            _subst_type_str(
                "Map<K, Vec<V>>", {"K": "String", "V": "Int32"}
            ),
            "Map<String, Vec<Int32>>",
        )

    def test_bare_chain_still_resolves(self):
        self.assertEqual(_subst_type_str("T", {"T": "Int32"}), "Int32")
        self.assertEqual(
            _subst_type_str("T", {"T": "U", "U": "Int32"}), "Int32"
        )

    def test_self_referential_replacement_not_descended(self):
        """``T -> Node<T>`` 不再深入 (既有 SOF 纪律, 见 docstring)。"""
        self.assertEqual(
            _subst_type_str("*mut T", {"T": "Node<T>"}), "*mut Node<T>"
        )

    def test_idempotent_when_not_a_param(self):
        self.assertEqual(_subst_type_str("*mut Int32", {"T": "Int64"}), "*mut Int32")
        self.assertEqual(_subst_type_str("Int32", {}), "Int32")
        self.assertEqual(_subst_type_str("*mut T"), "*mut T")


if __name__ == "__main__":
    unittest.main()