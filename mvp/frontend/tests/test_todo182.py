"""todo-182: FFI type restrictions lifted (user-adjudicated scope).

解除 FFI 类型限制:
1) 引用降级  ``&T``/``&mut T`` 形参按 ``*const T``/``*mut T`` 校验与
   传递 (Rust ABI: 引用即地址, PassMode 间接), 注解写降级后的扁平指针名;
2) 不透明指针  ``*const X``/``*mut X`` 放行任意**非泛型**被指类型
   (含 String / 非纯内联 struct / fn / 二级指针), 按地址直传;
3) Option  ``Option<*const/*mut T>`` / ``Option<&mut String>`` 返回位
   放行 (NULL 判空); Option 在形参位一律拒绝;
4) 泛型  泛型实例 (Vector/Map/用户泛型) 无稳定 C 布局, 全形态拒绝;
5) 数组  ``[T; N]`` 元素扩面到非泛型结构体 (全局), FFI 形参位退化直传。

单文件数据用例在 ``cases/cffi/`` (extern_*); 本模块放组合形态与
typed-AST 注解断言。
"""

import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

import harness

from cwind_frontend import build_typed_ast, run_sa_with_errors
from cwind_frontend.parser.parser import parse_with_errors
from cwind_frontend import tokenize_file


def _sa(text: str):
    with _entry(text) as entry:
        parsed = parse_with_errors(
            tokenize_file(entry), source_path=str(entry.resolve())
        )
        if parsed.errors:
            return None, list(parsed.errors)
        result = run_sa_with_errors(parsed.program)
        return result, list(result.errors)


class _entry:
    def __init__(self, text: str):
        import tempfile
        self._td = tempfile.TemporaryDirectory()
        self.path = Path(self._td.name) / "main.wind"
        self.path.write_text(text, encoding="utf-8")

    def __enter__(self):
        return self.path

    def __exit__(self, *exc):
        self._td.cleanup()


class ReferenceDowngradeTests(unittest.TestCase):
    """引用降级: &T/&mut T 在 FFI 边界 == *const T/*mut T."""

    def test_ref_annotations_downgraded(self):
        with _entry(
            "extern \"C\" {\n"
            "    fn f(a: &Int32, b: &mut Int32, c: &String) -> Int32;\n"
            "}\n"
        ) as entry:
            parsed = parse_with_errors(
                tokenize_file(entry), source_path=str(entry.resolve())
            )
            self.assertEqual([], [e.message for e in parsed.errors])
            result = run_sa_with_errors(parsed.program)
            self.assertEqual([], [e.message for e in result.errors])
            doc = build_typed_ast(parsed.program, result.info)

        def _walk(node):
            if isinstance(node, dict):
                if node.get("kind") == "ExternBlock":
                    yield node
                for value in node.values():
                    yield from _walk(value)
            elif isinstance(node, list):
                for item in node:
                    yield from _walk(item)

        for block in _walk(doc.get("ast")):
            for fn in block.get("fns", []):
                if fn.get("name") != "f":
                    continue
                anns = [
                    p["type"]["ann"]["type"]["name"]
                    for p in fn.get("params", [])
                ]
                # 后端契约: 类型节点 ann.type.name 是扁平指针名; 源码
                # 节点保持 &T 拼写 (node.ref/node.mut 原样)。
                self.assertEqual(
                    ["*const Int32", "*mut Int32", "*const String"],
                    anns,
                    "引用形参的类型注解必须是降级后的扁平指针名",
                )
                self.assertTrue(
                    all(p.get("type", {}).get("ref") for p in fn.get("params", [])),
                    "AST 节点的 ref 标志保持引用拼写",
                )

    def test_ref_to_agg_is_opaque_pointer(self):
        """&NonPod 降级为 *const NonPod 后按不透明地址放行."""
        result, errors = _sa(
            "struct NonPod { name: String, v: Int }\n"
            "extern \"C\" {\n"
            "    fn reg(p: &mut NonPod);\n"
            "}\n"
        )
        self.assertEqual([], errors)

    def test_ref_array_param_ok(self):
        """&[T; N] 借用形参: 降级为 *const [T; N] 再退化 T*."""
        result, errors = _sa(
            "extern \"C\" {\n"
            "    fn fill(buf: &mut [Byte; 8]);\n"
            "}\n"
        )
        self.assertEqual([], errors)


class OptionTests(unittest.TestCase):
    """Option: 指针载荷返回位放行, 形参位拒绝, 标量载荷拒绝."""

    def test_opt_ptr_returns_ok(self):
        result, errors = _sa(
            "extern \"C\" {\n"
            "    fn a() -> Option<*mut Byte>;\n"
            "    fn b() -> Option<*const Int32>;\n"
            "    fn c() -> Option<&mut String>;\n"
            "}\n"
        )
        self.assertEqual([], errors)

    def test_opt_param_rejected(self):
        result, errors = _sa(
            "extern \"C\" {\n"
            "    fn bad(o: Option<String>);\n"
            "}\n"
        )
        self.assertTrue(
            any("Option crosses the boundary as a return type" in m.message
                for m in errors),
            errors,
        )

    def test_opt_scalar_return_rejected(self):
        result, errors = _sa(
            "extern \"C\" {\n"
            "    fn bad() -> Option<Int32>;\n"
            "}\n"
        )
        self.assertTrue(
            any("no C-ABI mapping" in m.message for m in errors), errors
        )

    def test_opt_generic_ptr_rejected(self):
        result, errors = _sa(
            "extern \"C\" {\n"
            "    fn bad() -> Option<*mut Vector<Int32>>;\n"
            "}\n"
        )
        self.assertTrue(
            any("generic" in m.message for m in errors), errors
        )


class OpaquePointerTests(unittest.TestCase):
    """不透明指针: 任意非泛型被指类型放行, 泛型拒绝."""

    def test_double_ptr_and_fnptr_ok(self):
        result, errors = _sa(
            "extern \"C\" {\n"
            "    fn env() -> *mut *mut Byte;\n"
            "    fn get() -> *mut fn(Int32);\n"
            "}\n"
        )
        self.assertEqual([], errors)

    def test_opaque_handle_roundtrip(self):
        result, errors = _sa(
            "struct P { x: Int32, y: Int32 }\n"
            "extern \"C\" {\n"
            "    fn reg(p: *mut P);\n"
            "    fn get() -> *const P;\n"
            "}\n"
        )
        self.assertEqual([], errors)

    def test_generic_pointee_rejected(self):
        result, errors = _sa(
            "extern \"C\" {\n"
            "    fn bad(p: *const Vector<Int32>);\n"
            "}\n"
        )
        self.assertTrue(
            any("generic" in m.message for m in errors), errors
        )


class ArrayTests(unittest.TestCase):
    """[T; N] 元素扩面 (全局) + FFI 退化."""

    def test_struct_elem_array_value_ok(self):
        result, errors = _sa(
            "struct P { x: Int32, y: Int32 }\n"
            "\n"
            "fn main() -> Int {\n"
            "    let arr: [P; 3] = [P { 1, 2 }, P { 3, 4 }, P { 5, 6 }];\n"
            "    let p: P = arr[1];\n"
            "    return p.x + p.y as Int;\n"
            "}\n"
        )
        self.assertEqual([], errors)

    def test_struct_elem_array_ffi_decay_ok(self):
        result, errors = _sa(
            "struct P { x: Int32, y: Int32 }\n"
            "extern \"C\" {\n"
            "    fn pts(arr: [P; 4], n: c_int) -> c_int;\n"
            "}\n"
        )
        self.assertEqual([], errors)

    def test_generic_elem_array_rejected(self):
        result, errors = _sa(
            "extern \"C\" {\n"
            "    fn bad(arr: [Vector<Int32>; 3]);\n"
            "}\n"
        )
        self.assertTrue(
            any("neither a fixed-width scalar nor a non-generic struct" in m.message
                for m in errors),
            errors,
        )

    def test_enum_elem_array_rejected(self):
        result, errors = _sa(
            "extern \"C\" {\n"
            "    fn bad(arr: [Option<Int32>; 2]);\n"
            "}\n"
        )
        # 泛型元素无数值内联布局: SA 拒绝 (类型位校验)
        self.assertTrue(
            any("neither a fixed-width scalar nor a non-generic struct" in m.message
                for m in errors),
            errors,
        )


class PointerCastTests(unittest.TestCase):
    """todo-75: 指针 as 转换 (数值<->指针, 指针<->指针, 数组->指针)."""

    def test_scalar_to_ptr_ok(self):
        result, errors = _sa(
            "fn main() -> Int {\n"
            "    let p: *const Byte = 0x1000 as *const Byte;\n"
            "    return 0;\n"
            "}\n"
        )
        self.assertEqual([], errors)

    def test_ptr_to_scalar_ok(self):
        result, errors = _sa(
            "fn zero_ptr() -> *mut Byte {\n"
            "    return 0 as *mut Byte;\n"
            "}\n"
            "\n"
            "fn main() -> Int {\n"
            "    let p: *mut Byte = zero_ptr();\n"
            "    let n: UInt64 = p as UInt64;\n"
            "    return n as Int;\n"
            "}\n"
        )
        self.assertEqual([], errors)

    def test_array_to_void_ptr_ok(self):
        result, errors = _sa(
            "fn main() -> Int {\n"
            "    let a: [Int32; 4] = [1, 2, 3, 4];\n"
            "    let p: *const c_void = a as *const c_void;\n"
            "    return 0;\n"
            "}\n"
        )
        self.assertEqual([], errors)


if __name__ == "__main__":
    unittest.main()
