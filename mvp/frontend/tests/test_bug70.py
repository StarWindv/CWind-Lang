"""Regression tests for bug-70: generic scopes must not erase same-named
user types.

``struct S`` 在 pass 1 注册进全局 ``defined`` 后, 任何形参含同名泛型
的声明 (std::traits::num_wrapping::Wrapping<T, S> 是真实发现路径) 在
pass-2 退出泛型作用域时执行 ``defined -= generic``, 把用户的 S 从
定义表删掉 —— 之后所有 ``S`` 引用报 "unknown type 'S'" (bug-51 修复
引入 _push/_pop_generics 快照恢复后残留的 ``-=``, 对恢复后的快照
再减一次, 覆盖了预先存在的名字)。

单文件用例 (无需 prelude) 在 ``cases/bug70/``; 本模块的用例走真实
std prelude ( Wrapping<T, S> / Into<T, U> / Iterator<T> 全是单字母
泛型形参的 std trait )。

修复语义: 泛型作用域退出只回收**本作用域新登记**的名字
(``_enter_defined`` / ``_leave_defined``), 预先存在的同名类型保留。
"""

import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

from cwind_frontend import run_sa_with_errors, tokenize_file
from cwind_frontend.parser.parser import parse_with_errors


def _check_source(text: str) -> list[str]:
    """Parse+SA a single entry file with the real std prelude attached."""
    with tempfile.TemporaryDirectory() as td:
        entry = Path(td) / "main.wind"
        entry.write_text(text, encoding="utf-8")
        parsed = parse_with_errors(
            tokenize_file(entry), source_path=str(entry.resolve())
        )
        if parsed.errors:
            return [e.message for e in parsed.errors]
        result = run_sa_with_errors(parsed.program)
        return [e.message for e in result.errors]


class PreludeCollisionTests(unittest.TestCase):
    """真实 std prelude (Wrapping<T,S> 等) 与单字母用户类型共存."""

    def test_readme_bug70_struct_s(self):
        """原始 bug-70 复现 (bugs/bug70.wind): prelude Wrapping<T, S>."""
        errors = _check_source(
            "struct S { v: Int }\n"
            "\n"
            "fn f(x: S) -> Int { x.v }\n"
            "\n"
            "fn main() -> Int {\n"
            "    let s: S = S { 1 };\n"
            "    return f(s);\n"
            "}\n"
        )
        self.assertEqual(
            [], errors, "bug-70: struct S 必须能在 prelude 下正常引用"
        )

    def test_struct_t_with_generic_fn(self):
        """struct T + 泛型函数 fn g<T>: pass-3 作用域恢复不得删 T."""
        errors = _check_source(
            "struct T { v: Int }\n"
            "\n"
            "fn g<T>(x: T) -> Int { 0 }\n"
            "\n"
            "fn h(x: T) -> Int { x.v }\n"
            "\n"
            "fn main() -> Int {\n"
            "    let t: T = T { 3 };\n"
            "    return g(1) + h(t);\n"
            "}\n"
        )
        self.assertEqual([], errors)

    def test_struct_u_with_into_blanket(self):
        """struct U + prelude 的 Into blanket (impl<T, U> Into<U> for T):
        两个单字母形参都与用户类型撞名."""
        errors = _check_source(
            "struct U { n: Int }\n"
            "\n"
            "fn take_u(x: U) -> Int { x.n }\n"
            "\n"
            "fn main() -> Int {\n"
            "    let u: U = U { 5 };\n"
            "    return take_u(u);\n"
            "}\n"
        )
        self.assertEqual([], errors)

    def test_struct_s_as_generic_argument(self):
        """单字母类型作泛型实参 (Box<S>): 修复不得把单字母名一律当成
        泛型形参 (readme bug-70 描述的字面语义)."""
        errors = _check_source(
            "struct S { v: Int }\n"
            "\n"
            "struct Box<T> { v: T }\n"
            "\n"
            "fn main() -> Int {\n"
            "    let b: Box<S> = Box { S { 9 } };\n"
            "    return b.v.v;\n"
            "}\n"
        )
        self.assertEqual([], errors)

    def test_extern_fn_still_checks_abi(self):
        """修复不得顺带放宽既有检查: 泛型形参仍不可穿 extern 边界."""
        errors = _check_source(
            "extern \"C\" {\n"
            "    fn f(p: Vector<Int32>);\n"
            "}\n"
        )
        self.assertTrue(
            any("no C-ABI mapping" in m for m in errors),
            f"容器仍应被 C-ABI 检查拒绝, got: {errors}",
        )


if __name__ == "__main__":
    unittest.main()
