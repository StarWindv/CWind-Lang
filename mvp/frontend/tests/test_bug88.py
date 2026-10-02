"""bug-88: `as` 的判据取 **rustc 的 E0606** —— 引用位与裸指针位各判各的。

ledger 原文: "SA 未拦截引用转指针时同时更换被指类型 (`&T1 as *const T2`),
`&String as *const c_void` 放行后 `cwindc` 直接崩溃 (0xC0000005); `&i32 as
*const Byte` 同样被错误放行"。修 bug-97 那轮把地址算对了, 但"放行了却不该
放行"仍在, 所以判据本身要与 Rust 对齐。

**判据不是"一律收紧"或"一律放开", 而是照抄 Rust 分成两类**
(`rustc 1.98.1`, 逐条实跑, 见本模块 docstring 的矩阵):

* **引用位** (`&T` / `&mut T`): 目标是原始指针时**被指类型必须完全相同**
  —— 同大小也不行 (`&mut i32 as *mut u32` 同样拒绝, 判的是类型同一性而不是
  布局); 共享引用不能变 `*mut`; 目标是标量时一律拒绝, 只认两步
  (`&T as *const T as usize`)。
* **裸指针位** (`*mut T` / `*const T`): 换被指宽度、换被指类型、转 `c_void`
  一律放行 —— Rust 也放行, C 的 `void*` 惯用法正靠这条。

本模块把两列 verdict 钉在一起, 任何一边漂移都看得见。
`cases/bug88/ref_to_rawptr_as_rejected.wind` 是同一判据的数据化版本 (SA
只钉放行/拒绝这一半, 这里额外钉住逐条形状与措辞)。
"""

from __future__ import annotations

import unittest

import harness

BOX = ("|", "-", "\u2500", "\u251c", "\u2514", "\u2502")

# (label, kind, CWind source fragment, rustc verdict, expected CWind verdict)
#
# `kind` 只给子集断言用: "ref" = 引用位, "raw" = 裸指针位, "int" = 整型。
# rustc 一列是 rustc 1.98.1 实跑出来的 (D:\RUST\.cargo\bin\rustc.exe
# --edition 2021)。形状与大小/别名一一对应: i32/u32 同大小, u8/u64 异大小,
# String 是 fat, Pair/Big 是同形状的用户结构体, c_void <-> Rust 的 ().
MATRIX = [
    # ---- 引用 -> 裸指针: Rust 放行的同被指写法 ----
    ("&mut i32 -> *mut i32", "ref", "let q: *mut Int32 = &mut n as *mut Int32;", True, "accept"),
    ("&mut i32 -> *const i32", "ref", "let q: *const Int32 = &mut n as *const Int32;", True, "accept"),
    ("&i32 -> *const i32", "ref", "let q: *const Int32 = &n as *const Int32;", True, "accept"),
    ("&String -> *const String", "ref", 'let s: String = "x"; let q: *const String = &s as *const String;', True, "accept"),
    ("&mut Pair -> *const Pair", "ref", "let mut pr: Pair = Pair { 1, 2 }; let q: *const Pair = &mut pr as *const Pair;", True, "accept"),
    # ---- 引用 -> 裸指针: Rust 拒绝 ----
    ("&i32 -> *mut i32 (shared->mut)", "ref", "let q: *mut Int32 = &n as *mut Int32;", False, "reject"),
    ("&mut i32 -> *mut u32 (same size!)", "ref", "let q: *mut UInt32 = &mut n as *mut UInt32;", False, "reject"),
    ("&mut i32 -> *const u32 (same size!)", "ref", "let q: *const UInt32 = &mut n as *const UInt32;", False, "reject"),
    ("&i32 -> *const u32 (same size!)", "ref", "let q: *const UInt32 = &n as *const UInt32;", False, "reject"),
    ("&mut i32 -> *mut u8", "ref", "let q: *mut UInt8 = &mut n as *mut UInt8;", False, "reject"),
    ("&i32 -> *const u8 (ledger's Byte)", "ref", "let q: *const UInt8 = &n as *const UInt8;", False, "reject"),
    ("&mut i32 -> *mut i64", "ref", "let q: *mut Int64 = &mut n as *mut Int64;", False, "reject"),
    ("&String -> *const () (bug-88)", "ref", 'let s: String = "x"; let q: *const c_void = &s as *const c_void;', False, "reject"),
    ("&mut Color -> *mut ()", "ref", "let mut c2: Color = Color::Red; let q: *mut c_void = &mut c2 as *mut c_void;", False, "reject"),
    ("&Pair -> *const Big", "ref", "let pr: Pair = Pair { 1, 2 }; let q: *const Big = &pr as *const Big;", False, "reject"),
    # ---- 引用 -> 标量: Rust 只认两步 ----
    ("&i32 -> i64 (one step)", "ref", "let q: Int64 = &n as Int64;", False, "reject"),
    ("&i32 -> *const i32 -> i64", "ref", "let q: Int64 = &n as *const Int32 as Int64;", True, "accept"),
    # ---- 裸指针位: Rust 全部放行 ----
    ("*mut i32 -> *mut i32", "raw", "let q: *mut Int32 = p as *mut Int32;", True, "accept"),
    ("*mut i32 -> *const i32", "raw", "let q: *const Int32 = p as *const Int32;", True, "accept"),
    ("*const i32 -> *mut i32", "raw", "let q: *mut Int32 = c as *mut Int32;", True, "accept"),
    ("*mut i32 -> *mut u32", "raw", "let q: *mut UInt32 = p as *mut UInt32;", True, "accept"),
    ("*mut i32 -> *mut u8", "raw", "let q: *mut UInt8 = p as *mut UInt8;", True, "accept"),
    ("*mut i32 -> *mut i64", "raw", "let q: *mut Int64 = p as *mut Int64;", True, "accept"),
    ("*mut i32 -> *mut () (void* idiom)", "raw", "let q: *mut c_void = p as *mut c_void;", True, "accept"),
    ("*const Pair -> *const Big", "raw", "let q: *const Big = c as *const Big;", True, "accept"),
    # ---- 裸指针 <-> 整型: Rust 放行 ----
    ("*mut i32 -> usize", "raw", "let q: Int64 = p as Int64;", True, "accept"),
    ("*mut i32 -> i32 (narrow)", "raw", "let q: Int32 = p as Int32;", True, "accept"),
    ("usize -> *const i32", "int", "let q: *const Int32 = a as *const Int32;", True, "accept"),
]

PREAMBLE = (
    "use std::ctypedef::*;\n"
    "struct Pair { x: Int32, y: Int32 }\n"
    "struct Big { a: Int64, b: Int64, c: Int64 }\n"
    "enum Color { Red, Green }\n"
    "fn main() -> Int64 {\n"
    "    let mut n: Int32 = 41;\n"
    "    let mut p: *mut Int32 = &mut n;\n"
    "    let c: *const Int32 = &n;\n"
    "    let a: Int64 = 4096;\n"
)


def _verdict(fragment: str) -> tuple[str, str]:
    result = harness.run_pipeline(PREAMBLE + "    " + fragment + "\n    return 0;\n}\n")
    if result["kind"] == "clean":
        return "accept", ""
    msg = result["errors"][0].message if result["errors"] else ""
    lines = [ln.strip() for ln in msg.splitlines()
             if ln.strip() and not ln.strip().startswith(BOX)]
    return "reject", (lines[-1] if lines else "")


class CastMatrixTests(harness.CaseAssertionsMixin, unittest.TestCase):
    """CWind 的 `as` 判据与 rustc 1.98.1 逐条一致。"""

    def test_matrix_matches_rustc(self):
        mismatches = []
        for label, _kind, fragment, rustc_ok, want in MATRIX:
            with self.subTest(shape=label):
                got, why = _verdict(fragment)
                rustc_verdict = "accept" if rustc_ok else "reject"
                self.assertEqual(
                    rustc_verdict, want,
                    f"{label}: the pinned rustc column and CWind column "
                    f"disagree about the same shape",
                )
                self.assertEqual(
                    got, want,
                    f"{label}: expected {want}, got {got} ({why}); "
                    f"rustc says {rustc_verdict}",
                )

    def test_accepted_matrix_is_non_empty(self):
        """非空洞: 两侧都有一批放行与拒绝。"""
        accept = sum(1 for _, _, _, _, w in MATRIX if w == "accept")
        reject = sum(1 for _, _, _, _, w in MATRIX if w == "reject")
        self.assertGreaterEqual(accept, 10)
        self.assertGreaterEqual(reject, 10)

    def test_pointee_rule_rejects_same_size_change(self):
        """判据是类型**同一性**, 不是布局 —— 这一条最容易改错。

        `&mut Int32 as *mut UInt32` 与 `&mut Int32 as *mut Int32` 布局完全
        相同 (都是 4B/align 4), 只有类型不同; 若实现改成"比大小", 这一条会
        静默变回放行。
        """
        same_size, why = _verdict("let q: *mut UInt32 = &mut n as *mut UInt32;")
        self.assertEqual(same_size, "reject", why)
        self.assertIn("keeps the pointee type", why)

    def test_shared_ref_to_mut_ptr_is_rejected(self):
        _, why = _verdict("let q: *mut Int32 = &n as *mut Int32;")
        self.assertIn("shared reference cannot be cast to a mutable raw pointer", why)

    def test_ref_to_scalar_needs_the_pointer_step(self):
        _, why = _verdict("let q: Int64 = &n as Int64;")
        self.assertIn("needs a raw pointer in between", why)

    def test_raw_side_is_untouched(self):
        """引用位收紧**不得**波及裸指针位 —— C 的 void* 惯用法靠它。

        这条是本次判据最容易误伤的地方: 若把"被指必须相同"写成无条件规则,
        `p as *mut c_void` 与 `p as *mut u8` 会被一起拒掉, `libs/**` 里
        `free(self.buf as *mut u8)` 之类立刻编不出来。
        """
        rows = [r for r in MATRIX if r[1] in ("raw", "int")]
        self.assertGreaterEqual(len(rows), 10)
        for label, _kind, fragment, _rustc_ok, want in rows:
            with self.subTest(shape=label):
                got, why = _verdict(fragment)
                self.assertEqual(got, want, f"{label}: {why}")

    def test_rejected_case_is_clean_data(self):
        """数据化那半也要在 (逐条措辞见 cases/bug88 的 sidecar)。"""
        self.assert_case("bug88", "ref_to_rawptr_as_rejected")

    def test_accepted_case_is_clean_data(self):
        self.assert_case("bug97", "ref_to_rawptr_as")


if __name__ == "__main__":
    unittest.main()