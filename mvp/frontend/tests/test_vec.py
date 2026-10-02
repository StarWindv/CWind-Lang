"""``std::collections::vec`` (``Vec<T>``): 前端这一侧能钉住的部分。

``cases/vec/`` 里的数据文件走通用 harness, 但那个 harness **不注入 std
prelude**, 于是看不见 ``Vec``。所以这里用一个 bespoke 模块: 造一个**没有
``libs/`` 的临时目录**, 让模块根解析回落到编译器的 install root (即真正的
``libs/`` 树, 与 ``cwindf --typed-ast`` 对 ``mvp/test-c/test-rtl/fixtures``
里的 fixture 是同一棵树), 于是跑的是**真 std**。

覆盖面分工:

* 本模块 —— 前端 (SA) 这一侧: 核心 API 面在真 std 下干净通过; 静态槽可用;
  ``Vec`` 与 ``Vector`` 是两个互不相干的类型; **数组字面量归属决策**:
  ``let v: Vec<i32> = [1, 2, 3];`` 必须是类型失配, 不能悄悄给一个
  ``Vector``。
* ``cases/vec/array_literal_still_vector.wind`` —— 字面量归属的另一半
  (旧行为没变), 不需要真 std。
* ``mvp/test-c/test-rtl/fixtures/codegen_vec*.wind`` —— 数值那一半
  (增长 / 写穿 / 迭代 / GC / 越界诊断 / 元素类型收窄), 走 ctest。

**元素类型收窄故意不在这里断言**: ``Vec<String>`` 前端是**放行**的, 诊断归
后端 (它才知道元素槽宽的口径)。在前端按名字特判 ``Vec`` 等于给它开特权,
所以那条断言在 ``pipeline_vec_elem_rejected`` (cwindc 负例)。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent.parent.parent.parent
for path in (ROOT / "mvp/frontend/src", ROOT / "mvp/frontend/tests"):
    sys.path.insert(0, str(path))


def run_with_real_std(source: str) -> dict:
    """Feed *source* through parse -> SA against the **real** std tree.

    The temp dir carries only ``main.wind``: with no ``libs/`` next to it,
    ``_module_roots`` falls back to the compiler install root, which is the
    std tree the fixtures are generated against too.
    """
    from cwind_frontend import run_sa_with_errors, tokenize_file
    from cwind_frontend.parser.parser import parse_with_errors

    with tempfile.TemporaryDirectory() as td:
        entry = Path(td) / "main.wind"
        entry.write_text(source, encoding="utf-8", newline="\n")
        parsed = parse_with_errors(
            tokenize_file(entry), source_path=str(entry.resolve())
        )
        if parsed.errors:
            return {
                "kind": "parse_err",
                "errors": list(parsed.errors),
                "warnings": [],
            }
        sa = run_sa_with_errors(parsed.program)
        return {
            "kind": "sa_err" if sa.errors else "clean",
            "errors": list(sa.errors),
            "warnings": list(sa.warnings),
        }


class VecSurfaceTests(unittest.TestCase):
    """核心 API 面在真 std 下必须干净通过 (模块接线 + prelude 重导出 +
    泛型方法体 + 迭代器 impl 的前端那一半)。"""

    def assert_clean(self, source: str, ctx: str) -> None:
        result = run_with_real_std(source)
        self.assertEqual(
            result["kind"], "clean",
            f"{ctx}: {[e.message for e in result['errors']]}",
        )

    def test_core_surface(self):
        self.assert_clean(
            "fn main() -> Int32 {\n"
            "    let mut v: Vec<i32> = Vec::new();\n"
            "    if v.len() != 0 || !v.is_empty() { return 1; }\n"
            "    v.push(1);\n"
            "    v.push(2);\n"
            "    v.set(0, 10);\n"
            "    let r: &mut i32 = v.get_mut(1);\n"
            "    *r = 20;\n"
            "    let got: i32 = v.get(0);\n"
            "    let last: i32 = v.pop();\n"
            "    v.clear();\n"
            "    let w: Vec<i32> = Vec::with_capacity(4);\n"
            "    return got + last + v.len() + w.capacity() as Int32;\n"
            "}\n",
            "core surface",
        )

    def test_iteration_both_faces(self):
        self.assert_clean(
            "fn main() -> Int32 {\n"
            "    let mut v: Vec<i32> = Vec::new();\n"
            "    v.push(1);\n"
            "    v.push(2);\n"
            "    let mut a: Int32 = 0;\n"
            "    for x in v.iter() { a += x; }\n"
            "    let mut b: Int32 = 0;\n"
            "    for y in v { b += y; }\n"
            "    return a + b;\n"
            "}\n",
            "iteration",
        )

    def test_all_scalar_widths(self):
        """每种定宽槽宽都要能实例化 —— 单态化在宽度之间不许串槽。"""
        self.assert_clean(
            "fn main() -> Int32 {\n"
            "    let mut a: Vec<i8> = Vec::new();\n"
            "    let mut b: Vec<u8> = Vec::new();\n"
            "    let mut c: Vec<i16> = Vec::new();\n"
            "    let mut d: Vec<u16> = Vec::new();\n"
            "    let mut e: Vec<i32> = Vec::new();\n"
            "    let mut f: Vec<u32> = Vec::new();\n"
            "    let mut g: Vec<i64> = Vec::new();\n"
            "    let mut h: Vec<u64> = Vec::new();\n"
            "    let mut i: Vec<f32> = Vec::new();\n"
            "    let mut j: Vec<f64> = Vec::new();\n"
            "    let mut k: Vec<bool> = Vec::new();\n"
            "    a.push(1); b.push(1); c.push(1); d.push(1); e.push(1);\n"
            "    f.push(1); g.push(1); h.push(1); i.push(1.0); j.push(1.0);\n"
            "    k.push(true);\n"
            "    return 0;\n"
            "}\n",
            "scalar widths",
        )

    def test_static_slot(self):
        """`static mut` 装一个泛型实例: 声明面必须放行 (端到端那一半在
        fixtures/codegen_vec.wind 的 check_static)。"""
        self.assert_clean(
            "static mut ROWS: Vec<i64> = Vec::new();\n"
            "\n"
            "fn grow(n: i32) -> i32 {\n"
            "    let mut i: i32 = 0;\n"
            "    while i < n {\n"
            "        ROWS.push(i as i64);\n"
            "        i += 1;\n"
            "    }\n"
            "    return ROWS.len() as i32;\n"
            "}\n"
            "\n"
            "fn main() -> Int32 {\n"
            "    let a: i32 = grow(3);\n"
            "    return a + ROWS.get(0) as i32;\n"
            "}\n",
            "static slot",
        )

    def test_vec_and_vector_coexist(self):
        """`Vec` 与 C 运行时的 `Vector` 是两个不相干的类型: 各自的 push 落到
        各自的容器, 不存在任何别名关系。"""
        self.assert_clean(
            "fn main() -> Int32 {\n"
            "    let mut v: Vec<i32> = Vec::new();\n"
            "    let mut r: Vector<i32> = Vector::new();\n"
            "    v.push(1);\n"
            "    r.push_back(2);\n"
            "    return v.len() as Int32 + r.length() as Int32;\n"
            "}\n",
            "coexist",
        )


class VecArrayLiteralDecisionTests(unittest.TestCase):
    """数组字面量的归属决策。

    `[1, 2, 3]` 在 SA 里仍被推成 ``Vector<T>`` (literals.py 未改)。于是
    ``let v: Vec<i32> = [1, 2, 3];`` 是**类型失配** —— 写法是 ``Vec``, 拿到
    的必须是 ``Vec``, 悄悄换成 ``Vector`` 才是 bug。改成"字面量直接构造 Vec"
    需要前端把它 desugar 成 new + 逐个 push 并带上单态化上下文, 是另一件事。
    """

    def test_array_literal_into_vec_is_a_type_mismatch(self):
        result = run_with_real_std(
            "fn main() -> Int32 {\n"
            "    let v: Vec<i32> = [1, 2, 3];\n"
            "    return v.len() as Int32;\n"
            "}\n"
        )
        self.assertEqual(result["kind"], "sa_err", result)
        messages = [e.message for e in result["errors"]]
        mismatch = [
            m for m in messages
            if "initialize Vec<" in m and "with Vector<" in m
        ]
        self.assertTrue(
            mismatch,
            f"expected a Vec-vs-Vector type mismatch, got {messages}",
        )

    def test_array_literal_still_feeds_vector(self):
        """同一份字面量喂 ``Vector`` 仍然干净 (决策没有顺手改掉旧行为)。"""
        result = run_with_real_std(
            "fn main() -> Int32 {\n"
            "    let mut v: Vector<i32> = [1, 2, 3];\n"
            "    v.push_back(4);\n"
            "    return v.length() as Int32;\n"
            "}\n"
        )
        self.assertEqual(
            result["kind"], "clean", [e.message for e in result["errors"]]
        )


class VecContainsTests(unittest.TestCase):
    """``contains``: 无 trait bound 的形状, 以及"界"为什么不能写。

    ``contains<T: PartialEq<T>>`` 这个签名在本语言**写不出来**:
    ``libs/traits/cmp.wind`` 的 ``PartialEq`` 是**零类型参数**的 trait
    (``fn eq(&self, other: &Self)``), 写 ``PartialEq<T>`` 直接被 SA 拒:
    ``bound 'PartialEq' expects 0 type argument(s), got 1``。
    ``T: PartialEq`` 写得出来但**谁也满足不了** —— 没有 impl, 而且按声明
    的签名实现不了 (``&Self`` 不被代入)。加上 SA 的界检查只对 ``ToString``
    硬编码了一条 (sa/expressions/calls.py 的
    ``_check_bound_argument_conformance``), 别的界一律不查, 于是错误要到
    单态化时后端才补一句 ``no impl of 'PartialEq' for 'Int32' provides
    method 'eq'``。相等因此走定宽标量的内建 ``==``。
    """

    def test_contains_needs_no_bound(self):
        result = run_with_real_std(
            "fn main() -> Int32 {\n"
            "    let mut v: Vec<i32> = Vec::new();\n"
            "    v.push(1);\n"
            "    v.push(2);\n"
            "    if !v.contains(2) { return 1; }\n"
            "    if v.contains(9) { return 2; }\n"
            "    return 0;\n"
            "}\n"
        )
        self.assertEqual(
            result["kind"], "clean", [e.message for e in result["errors"]]
        )

    def test_contains_takes_item_by_value(self):
        """``Vector<T>::contains(&self, value: T)`` 也是按值收, ``Vec`` 跟它
        一致 —— ``&T`` 那一形在泛型体内读不出来 (SA 不给 ``&T`` 且 T 是类型
        形参的解引用定型), 而元素集全是 Copy, 按值没有代价。"""
        result = run_with_real_std(
            "fn main() -> Int32 {\n"
            "    let mut v: Vec<i32> = Vec::new();\n"
            "    v.push(5);\n"
            "    if !v.contains(5) { return 1; }\n"
            "    return 0;\n"
            "}\n"
        )
        self.assertEqual(
            result["kind"], "clean", [e.message for e in result["errors"]]
        )

    def test_partial_eq_takes_no_type_argument(self):
        """钉住"``PartialEq<T>`` 写不出来"这条: 将来若给它加了 ``Rhs`` 形参,
        ``contains`` 的界就有意义了, 这条会提醒重新评估。"""
        result = run_with_real_std(
            "fn same<T: PartialEq<T>>(a: &T, b: &T) -> bool { return true; }\n"
            "fn main() -> Int32 { return 0; }\n"
        )
        self.assertEqual(result["kind"], "sa_err", result)
        self.assertTrue(
            any(
                "expects 0 type argument(s)" in e.message
                for e in result["errors"]
            ),
            [e.message for e in result["errors"]],
        )

    def test_partial_eq_cannot_be_implemented(self):
        """钉住"``PartialEq`` 按声明的签名实现不了": ``&Self`` 不被代入。"""
        result = run_with_real_std(
            "struct P { x: i32 }\n"
            "impl PartialEq for P {\n"
            "    fn eq(&self, other: &Self) -> bool { return self.x == other.x; }\n"
            "}\n"
            "fn main() -> Int32 { return 0; }\n"
        )
        self.assertEqual(result["kind"], "sa_err", result)
        self.assertTrue(
            any("must be &Self" in e.message for e in result["errors"]),
            [e.message for e in result["errors"]],
        )


class VecDropTests(unittest.TestCase):
    """``drop(self)`` 按值消耗, 所以"用过了"由 SA 拦 —— 这一半只能在前端钉
    (端到端 fixture 里放一个必然编译不过的片段, 就没法跑了)。"""

    def test_use_after_drop_is_rejected(self):
        result = run_with_real_std(
            "fn main() -> Int32 {\n"
            "    let mut v: Vec<i32> = Vec::new();\n"
            "    v.push(1);\n"
            "    v.drop();\n"
            "    return v.len() as Int32;\n"
            "}\n"
        )
        self.assertEqual(result["kind"], "sa_err", result)
        self.assertTrue(
            any("used after move" in e.message for e in result["errors"]),
            [e.message for e in result["errors"]],
        )

    def test_double_drop_is_rejected(self):
        result = run_with_real_std(
            "fn main() -> Int32 {\n"
            "    let mut v: Vec<i32> = Vec::new();\n"
            "    v.push(1);\n"
            "    v.drop();\n"
            "    v.drop();\n"
            "    return 0;\n"
            "}\n"
        )
        self.assertEqual(result["kind"], "sa_err", result)
        self.assertTrue(
            any("used after move" in e.message for e in result["errors"]),
            [e.message for e in result["errors"]],
        )

    def test_drop_after_move_is_rejected(self):
        """把容器 move 给别人之后再 drop 也是 use-after-move。"""
        result = run_with_real_std(
            "fn eat(v: Vec<i32>) -> Int32 { v.drop(); return 0; }\n"
            "fn main() -> Int32 {\n"
            "    let mut v: Vec<i32> = Vec::new();\n"
            "    v.push(1);\n"
            "    return eat(v) + v.len() as Int32;\n"
            "}\n"
        )
        self.assertEqual(result["kind"], "sa_err", result)
        self.assertTrue(
            any("used after move" in e.message for e in result["errors"]),
            [e.message for e in result["errors"]],
        )

    def test_drop_of_unallocated_is_clean(self):
        """`cap == 0` 时 `buf` 指向 std 的静态探针, drop 必须跳过 free。"""
        result = run_with_real_std(
            "fn main() -> Int32 {\n"
            "    let fresh: Vec<i32> = Vec::new();\n"
            "    fresh.drop();\n"
            "    let zero: Vec<i64> = Vec::with_capacity(0);\n"
            "    zero.drop();\n"
            "    return 0;\n"
            "}\n"
        )
        self.assertEqual(
            result["kind"], "clean", [e.message for e in result["errors"]]
        )


class VecBorrowingIteratorTests(unittest.TestCase):
    """``refs()``: ``Item = &T`` 的借用迭代器。

    能做成的关键两点 (都踩过):
    * ``Iterator::Item`` **可以**是引用类型, 但 ``next`` 的返回类型必须把
      ``Option<&T>`` **直接写出来** —— SA 把 impl 里的 ``Self::Item`` 解成
      impl 的类型形参, 于是会拿 ``Option<T>`` 去跟 trait 声明的
      ``Option<&T>`` 对不上;
    * ``for r in v.refs()`` 要一条**显式** ``IntoIterator`` impl ——
      blanket ``impl<I: Iterator> IntoIterator for I`` 在这里不成立, 因为
      SA 不查界。
    """

    def test_refs_borrows_without_consuming(self):
        result = run_with_real_std(
            "fn main() -> Int32 {\n"
            "    let mut v: Vec<i32> = Vec::new();\n"
            "    v.push(1);\n"
            "    v.push(2);\n"
            "    v.push(3);\n"
            "    let mut a: i32 = 0;\n"
            "    for r in v.refs() { a += *r; }\n"
            "    let mut b: i32 = 0;\n"
            "    for r in v.refs() { b += *r; }\n"
            "    if a != 6 || b != 6 { return 1; }\n"
            "    if v.len() != 3 { return 2; }\n"
            "    return 0;\n"
            "}\n"
        )
        self.assertEqual(
            result["kind"], "clean", [e.message for e in result["errors"]]
        )

    def test_self_item_return_type_is_rejected(self):
        """钉住"``next`` 不能写 ``Option<Self::Item>``"这条 —— 将来 SA 修了
        ``Self::Item`` 的解析, ``RefIter`` 的 ``next`` 就可以简化。"""
        result = run_with_real_std(
            "pub struct RIter<T> { _inner: &T, count: usize }\n"
            "impl<T> Iterator for RIter<[i32; 1]> {\n"
            "    type Item = &i32;\n"
            "    fn next(&mut self) -> Option<Self::Item> {\n"
            "        return Option::None;\n"
            "    }\n"
            "}\n"
            "fn main() -> Int32 { return 0; }\n"
        )
        self.assertEqual(result["kind"], "sa_err", result)
        self.assertTrue(
            any("trait requires Option<" in e.message
                for e in result["errors"]),
            [e.message for e in result["errors"]],
        )


class PointerCastRegressionTests(unittest.TestCase):
    """``&mut T as *mut U`` 是**数值**重解释, 不是取地址 —— 别用它。

    ``f(&mut x as *mut T)`` 在 SA 过 (实参位不做那个 cast 的类型检查), 但
    后端把引用**解引用后按值**装箱: 实测 ``&mut n`` (n == 41) 传进去就是
    地址 41, 一读一写都 faults。正确写法是**coercion**:
    ``f(&mut x)`` 或 ``let p: *mut T = &mut x;``。这两种都验一遍。
    """

    def test_coercion_at_call_site_is_accepted(self):
        result = run_with_real_std(
            "fn bump(mut f: *mut Int32) { *f = *f + 1; }\n"
            "fn main() -> Int32 {\n"
            "    let mut n: Int32 = 0;\n"
            "    bump(&mut n);\n"
            "    if n != 1 { return 1; }\n"
            "    return 0;\n"
            "}\n"
        )
        self.assertEqual(
            result["kind"], "clean", [e.message for e in result["errors"]]
        )

    def test_coercion_through_a_binding_is_accepted(self):
        result = run_with_real_std(
            "fn bump(mut f: *mut Int32) { *f = *f + 1; }\n"
            "fn main() -> Int32 {\n"
            "    let mut n: Int32 = 0;\n"
            "    let p: *mut Int32 = &mut n;\n"
            "    bump(p);\n"
            "    if n != 1 { return 1; }\n"
            "    return 0;\n"
            "}\n"
        )
        self.assertEqual(
            result["kind"], "clean", [e.message for e in result["errors"]]
        )

    def test_explicit_cast_in_a_let_types_as_a_pointer(self):
        """``let`` 位上 ``&mut T as *mut U`` 的定型**随上下文变**。

        不带 std 的最小复现里 SA 把它定成整数 (``cannot initialize *mut
        Int32 with Int``); 带着真 std 的同一段却是 clean。所以这条只钉
        "它能过 SA", 真正的错在**后端**: 实参位那个形式 (下面两条注释里
        说的) 把引用**解引用后按值**装箱, 地址变成被引用变量自己的数值。
        端到端那一半钉不住 (崩在 cwindc 里), 所以这里只留注释, 不断言。
        """
        result = run_with_real_std(
            "fn main() -> Int32 {\n"
            "    let mut n: Int32 = 0;\n"
            "    let q: *mut Int32 = &mut n as *mut Int32;\n"
            "    return 0;\n"
            "}\n"
        )
        self.assertIn(result["kind"], ("clean", "sa_err"), result)


class StaticInstantiationTests(unittest.TestCase):
    """static 初始化式是一个**实例化点** —— 前端这一侧必须把 ``type_args``
    留在初始化式的调用标注上 (后端据此注册实例并补排一轮发射)。"""

    def test_static_initializer_carries_type_args(self):
        """纯前端断言不了"后端补排了一轮", 但可以钉住它依赖的那份数据:
        初始化式的调用标注带着具体类型实参。这条失败的话, 后端就会拿
        ``nt == 0`` 的平凡 mangle 去发射, 于是链接期 undefined reference。"""
        import json as _json
        import subprocess
        import sys as _sys

        repo = Path(__file__).resolve().parent.parent.parent.parent
        env = dict(**__import__("os").environ)
        env["PYTHONPATH"] = str(repo / "mvp/frontend/src")
        env["PYTHONIOENCODING"] = "utf-8"
        with tempfile.TemporaryDirectory() as td:
            entry = Path(td) / "main.wind"
            entry.write_text(
                "struct Pair<T> { a: T, b: T }\n"
                "extra<T> Pair<T> {\n"
                "    pub fn mk(a: T, b: T) -> Self { return Self { a, b }; }\n"
                "    pub fn first(&self) -> T { return self.a; }\n"
                "}\n"
                "static mut P: Pair<Int64> = Pair::mk(1, 2);\n"
                "fn main() -> Int32 {\n"
                "    return P.first() as Int32;\n"
                "}\n",
                encoding="utf-8", newline="\n",
            )
            out = subprocess.run(
                [_sys.executable, "-m", "cwind_frontend.cli", "--typed-ast",
                 str(entry)],
                capture_output=True, env=env, cwd=str(repo),
            )
            self.assertEqual(out.returncode, 0, out.stderr.decode("utf-8"))
            doc = _json.loads(out.stdout.decode("utf-8"))

        found = []

        def walk(node):
            if isinstance(node, dict):
                if node.get("kind") == "StaticDecl" and node.get("name") == "P":
                    found.append(node)
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(doc["ast"])
        self.assertEqual(len(found), 1, "static P not found in the typed AST")
        init = found[0].get("value")
        self.assertIsNotNone(init)
        call = init.get("ann", {}).get("call", {})
        self.assertEqual(call.get("callee_kind"), "method", call)
        self.assertEqual(
            call.get("type_args"), {"T": {"name": "Int64"}}, call
        )


if __name__ == "__main__":
    unittest.main()
