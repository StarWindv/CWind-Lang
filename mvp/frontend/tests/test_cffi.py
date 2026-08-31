"""todo-48/49: CFFI syntax and semantics (extern blocks + #[link]).

Data-driven pipeline cases live in ``cases/cffi``; structural assertions
over the parsed/serialized form stay in this module.
"""

import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

import harness

from cwind_frontend import (
    ExternBlock,
    FnDecl,
    Program,
    build_typed_ast,
    lex_with_errors,
    parse_source,
    parse_with_errors,
    run_sa,
    run_sa_with_errors,
)
from cwind_frontend.parser.parser import Parser

CFFI = "cffi"


def _prog(name: str) -> Program:
    return parse_source(harness.source(CFFI, name))


def _extern_blocks(prog: Program):
    return [i for i in prog.items if isinstance(i, ExternBlock)]


def _run_typed(text: str):
    """Full pipeline; returns (program, info, errors)."""
    lexed = lex_with_errors(text)
    assert not lexed.errors, lexed.errors
    parsed = parse_with_errors(lexed.tokens)
    assert not parsed.errors, parsed.errors
    sa = run_sa_with_errors(parsed.program)
    return parsed.program, sa


class TestExternParsing(unittest.TestCase):
    def test_clean_program_parses(self):
        prog = _prog("extern_clean")
        blocks = _extern_blocks(prog)
        self.assertEqual(len(blocks), 3)

        first = blocks[0]
        self.assertEqual(first.abi, "C")
        self.assertIsNone(first.link_name)
        self.assertEqual(len(first.fns), 1)
        fn = first.fns[0]
        self.assertIsInstance(fn, FnDecl)
        self.assertEqual(fn.extern_abi, "C")
        self.assertIsNone(fn.body)

    def test_extern_abi_preserved(self):
        prog = _prog("extern_full_attr")
        block = _extern_blocks(prog)[0]
        self.assertEqual(block.abi, "system")

    def test_link_attribute_fields(self):
        prog = _prog("extern_full_attr")
        block = _extern_blocks(prog)[0]
        self.assertEqual(block.link_name, "m")
        self.assertEqual(block.link_kind, "static")
        self.assertEqual(block.link_path, "./libx.a")

    def test_link_name_only(self):
        prog = _prog("extern_name_only")
        block = _extern_blocks(prog)[0]
        self.assertEqual(block.link_name, "m")
        self.assertIsNone(block.link_kind)
        self.assertIsNone(block.link_path)

    def test_link_path_kind_order_free(self):
        prog = _prog("extern_path_kind")
        block = _extern_blocks(prog)[0]
        self.assertIsNone(block.link_name)
        self.assertEqual(block.link_kind, "dylib")
        self.assertEqual(block.link_path, "./libcwindmath.a")

    def test_no_attr_block(self):
        prog = _prog("extern_no_attr")
        block = _extern_blocks(prog)[0]
        self.assertIsNone(block.link_name)
        self.assertIsNone(block.link_kind)
        self.assertIsNone(block.link_path)


class TestExternSemantics(unittest.TestCase):
    def test_clean_program_passes_sa(self):
        run_sa(_prog("extern_clean"))  # should not raise

    def test_raw_pointer_args_are_copy(self):
        # 同一指针变量两次传入 extern 函数不得触发 moved 错误
        src = """
extern "C" {
    fn sink(p: *mut Int32) -> None;
}
fn main() -> Int {
    let mut v: Int32 = 1;
    let mut p: *mut Int32 = &v;
    sink(p);
    sink(p);
    return 0;
}
"""
        _, result = _run_typed(src)
        self.assertEqual(
            [e.message for e in result.errors], []
        )

    def test_call_arity_checked(self):
        src = """
extern "C" {
    fn add(a: Int32, b: Int32) -> Int32;
}
fn main() -> Int {
    let x: Int32 = add(1);
    return 0;
}
"""
        _, result = _run_typed(src)
        self.assertTrue(
            any("expects 2 argument(s)" in e.message for e in result.errors)
        )

    def test_call_arg_type_checked(self):
        src = """
extern "C" {
    fn f(b: Bool) -> Int32;
}
fn main() -> Int {
    let x: Int32 = f(3);
    return 0;
}
"""
        _, result = _run_typed(src)
        self.assertTrue(any(result.errors))

    def test_call_result_type_flows(self):
        src = """
extern "C" {
    fn f() -> Float64;
}
fn main() -> Int {
    let x: Float64 = f();
    let bad: String = f();
    return 0;
}
"""
        _, result = _run_typed(src)
        self.assertTrue(
            any("String" in e.message for e in result.errors)
        )


class TestTypedAstSerialization(unittest.TestCase):
    def _typed(self, name: str) -> dict:
        program, _ = _run_typed(harness.source(CFFI, name))
        info = run_sa(program)
        return build_typed_ast(program, info)

    def test_extern_block_serialization(self):
        doc = self._typed("extern_full_attr")
        blocks = [
            i for i in doc["ast"]["items"]
            if i["kind"] == "ExternBlock"
        ]
        self.assertEqual(len(blocks), 1)
        b = blocks[0]
        self.assertEqual(b["abi"], "system")
        self.assertEqual(b["link_name"], "m")
        self.assertEqual(b["link_kind"], "static")
        self.assertEqual(b["link_path"], "./libx.a")

    def test_extern_fn_decl_serialization(self):
        doc = self._typed("extern_full_attr")
        blocks = [
            i for i in doc["ast"]["items"]
            if i["kind"] == "ExternBlock"
        ]
        fn = blocks[0]["fns"][0]
        self.assertEqual(fn["kind"], "FnDecl")
        self.assertEqual(fn["extern_abi"], "system")
        self.assertIsNone(fn["body"])

    def test_plain_fn_has_null_extern_abi(self):
        doc = self._typed("extern_full_attr")
        mains = [i for i in doc["ast"]["items"] if i.get("name") == "main"]
        self.assertEqual(mains[0]["extern_abi"], None)


class TestStringInteropTodo51(unittest.TestCase):
    """todo-51: String <-> char*/const char* in extern signatures."""

    def test_string_params_and_returns_clean(self):
        # 数据驱动用例: String 参数与返回均通过 SA
        exp = harness.expect(CFFI, "extern_string_ok")
        self.assertEqual(exp, {})
        _, result = _run_typed(harness.source(CFFI, "extern_string_ok"))
        self.assertEqual([e.message for e in result.errors], [])

    def test_ptr_to_string_still_rejected(self):
        exp = harness.expect(CFFI, "extern_string_ptr_rejected")
        self.assertTrue(exp.get("errors"))
        _, result = _run_typed(
            harness.source(CFFI, "extern_string_ptr_rejected")
        )
        self.assertTrue(
            any(
                "*const String" in e.message and "no C-ABI mapping" in e.message
                for e in result.errors
            ),
            [e.message for e in result.errors],
        )

    def test_string_arg_moves_ownership(self):
        src = """
extern "C" {
    fn sink(s: String) -> UInt32;
}
fn main() -> Int {
    let s: String = "abc";
    let n: UInt32 = sink(s);
    print(s);
    return 0;
}
"""
        _, result = _run_typed(src)
        self.assertTrue(
            any("used after move" in e.message for e in result.errors),
            [e.message for e in result.errors],
        )

    def test_string_return_type_flows(self):
        src = """
extern "C" {
    fn f() -> String;
}
fn main() -> Int {
    let bad: Int32 = f();
    return 0;
}
"""
        _, result = _run_typed(src)
        self.assertTrue(
            any("Int32" in e.message for e in result.errors),
            [e.message for e in result.errors],
        )


class TestExternStaticTodo56(unittest.TestCase):
    """todo-56: extern static bindings for C globals."""

    def test_statics_parse_and_serialize(self):
        src = """
extern "C" {
    fn f() -> Int32;
    static errno: Int32;
    static mut counter: UInt64;
}
fn main() -> Int { return 0; }
"""
        prog, result = _run_typed(src)
        self.assertEqual([e.message for e in result.errors], [])
        blocks = [
            i for i in prog.items if isinstance(i, ExternBlock)
        ]
        statics = blocks[0].statics
        self.assertEqual(len(statics), 2)
        self.assertEqual(statics[0].name, "errno")
        self.assertFalse(statics[0].mutable)
        self.assertEqual(statics[1].name, "counter")
        self.assertTrue(statics[1].mutable)

    def test_typed_ast_carries_extern_static(self):
        src = """
extern "C" {
    static mut counter: UInt64;
}
fn main() -> Int {
    counter = 1;
    return 0;
}
"""
        prog, result = _run_typed(src)
        self.assertEqual([e.message for e in result.errors], [])
        info = run_sa(prog)
        doc = build_typed_ast(prog, info)
        blocks = [
            i for i in doc["ast"]["items"] if i["kind"] == "ExternBlock"
        ]
        st = blocks[0]["statics"][0]
        self.assertEqual(st["kind"], "ExternStatic")
        self.assertEqual(st["name"], "counter")
        self.assertTrue(st["mutable"])
        # 符号表登记 kind = "static"
        sym = next(s for s in doc["symbols"] if s["name"] == "counter")
        self.assertEqual(sym["kind"], "static")

    def test_clean_binding_passes_sa(self):
        exp = harness.expect(CFFI, "extern_static_clean")
        self.assertEqual(exp, {})
        _, result = _run_typed(harness.source(CFFI, "extern_static_clean"))
        self.assertEqual([e.message for e in result.errors], [])

    def test_immut_write_rejected(self):
        exp = harness.expect(CFFI, "extern_static_immut_rejected")
        self.assertTrue(exp.get("errors"))
        _, result = _run_typed(
            harness.source(CFFI, "extern_static_immut_rejected")
        )
        self.assertTrue(
            any(
                "cannot assign to extern static 'errno'" in e.message
                and "mut" in e.message
                for e in result.errors
            ),
            [e.message for e in result.errors],
        )

    def test_container_type_rejected(self):
        exp = harness.expect(CFFI, "extern_static_container_rejected")
        self.assertTrue(exp.get("errors"))
        _, result = _run_typed(
            harness.source(CFFI, "extern_static_container_rejected")
        )
        self.assertTrue(
            any(
                "extern static 'v' is Vector<Int>" in e.message
                and "no C-ABI mapping" in e.message
                for e in result.errors
            ),
            [e.message for e in result.errors],
        )

    def test_name_clash_with_fn_rejected(self):
        src = """
extern "C" {
    fn dup() -> Int32;
    static dup: Int32;
}
fn main() -> Int { return 0; }
"""
        _, result = _run_typed(src)
        self.assertTrue(
            any("duplicate definition of 'dup'" in e.message
                for e in result.errors),
            [e.message for e in result.errors],
        )


class TestExternCallbacksTodo54(unittest.TestCase):
    """todo-54: extern fn addresses / C callbacks interop."""

    def test_clean_callback_program(self):
        exp = harness.expect(CFFI, "extern_callback_clean")
        self.assertEqual(exp, {})
        _, result = _run_typed(harness.source(CFFI, "extern_callback_clean"))
        self.assertEqual([e.message for e in result.errors], [])

    def test_fn_return_rejected(self):
        exp = harness.expect(CFFI, "extern_fn_return_rejected")
        self.assertTrue(exp.get("errors"))
        _, result = _run_typed(
            harness.source(CFFI, "extern_fn_return_rejected")
        )
        self.assertTrue(
            any(
                "cannot return a function pointer" in e.message
                and "'pick'" in e.message
                for e in result.errors
            ),
            [e.message for e in result.errors],
        )

    def test_callback_signature_mismatch_rejected(self):
        exp = harness.expect(CFFI, "extern_callback_sig_mismatch")
        self.assertTrue(exp.get("errors"))
        _, result = _run_typed(
            harness.source(CFFI, "extern_callback_sig_mismatch")
        )
        self.assertTrue(
            any(
                "argument 1 of 'apply'" in e.message
                and "fn(Int32, Int32) -> Int32" in e.message
                for e in result.errors
            ),
            [e.message for e in result.errors],
        )

    def test_callback_alias_spelling_accepted(self):
        # bug-58: 实参与签名可分别用别名拼写 (ctypedef), 展开后逐段
        # 同型即接受 -- 不再要求原始拼写逐字符相等。
        exp = harness.expect(CFFI, "extern_callback_alias_ok")
        self.assertEqual(exp, {})
        _, result = _run_typed(
            harness.source(CFFI, "extern_callback_alias_ok")
        )
        self.assertEqual([e.message for e in result.errors], [])

    def test_callback_ptr_sig_mismatch_rejected(self):
        # bug-58 边界: 展开后逐段必须同型, *mut Byte 与 *mut Int32 失配
        exp = harness.expect(CFFI, "extern_callback_ptr_sig_mismatch")
        self.assertTrue(exp.get("errors"))
        _, result = _run_typed(
            harness.source(CFFI, "extern_callback_ptr_sig_mismatch")
        )
        self.assertTrue(
            any("argument 1 of 'apply'" in e.message for e in result.errors),
            [e.message for e in result.errors],
        )

    def test_callback_ptr_sig_accepted(self):
        # bug-59: 回调签名段内允许原始指针 (fn(*mut Byte) -> UInt32);
        # SA 放行, 后端经适配器穿越边界 (pipeline_bug58 端到端)。
        exp = harness.expect(CFFI, "extern_callback_ptr_sig_ok")
        self.assertEqual(exp, {})
        _, result = _run_typed(
            harness.source(CFFI, "extern_callback_ptr_sig_ok")
        )
        self.assertEqual([e.message for e in result.errors], [])

    def test_matching_bare_name_accepted(self):
        # 裸函数名与回调签名逐段一致时通过 (宽度不同则拒绝)
        src = """
extern "C" {
    fn apply(op: fn(Int32) -> Int32) -> Int32;
}
fn inc(x: Int32) -> Int32 {
    return x + 1;
}
fn main() -> Int {
    let r: Int32 = apply(inc);
    print(r);
    return 0;
}
"""
        _, result = _run_typed(src)
        self.assertEqual([e.message for e in result.errors], [])


class TestExternAggregatesTodo52(unittest.TestCase):
    """todo-52: struct/enum aggregates across the FFI boundary."""

    def test_clean_aggregates(self):
        exp = harness.expect(CFFI, "extern_agg_clean")
        self.assertEqual(exp, {})
        _, result = _run_typed(harness.source(CFFI, "extern_agg_clean"))
        self.assertEqual([e.message for e in result.errors], [])

    def test_mixed_width_struct_accepted(self):
        # todo-65: 混合宽度平铺标量聚合现按目标 ABI 分派, 不再拒绝
        exp = harness.expect(CFFI, "extern_agg_mixed_width")
        self.assertEqual(exp, {})
        _, result = _run_typed(
            harness.source(CFFI, "extern_agg_mixed_width")
        )
        self.assertEqual([e.message for e in result.errors], [])

    def test_oversized_struct_rejected(self):
        # todo-65: 平铺同宽聚合上限从 8 字节放宽到 16 字节,
        # 20 字节 (5 x Int32) 仍拒绝
        exp = harness.expect(CFFI, "extern_agg_too_large")
        self.assertTrue(exp.get("errors"))
        _, result = _run_typed(
            harness.source(CFFI, "extern_agg_too_large")
        )
        self.assertTrue(
            any("exceeds 16 bytes" in e.message for e in result.errors),
            [e.message for e in result.errors],
        )

    def test_small_aggregates_accepted(self):
        # todo-65: 12 字节同宽标量聚合通过 SA
        exp = harness.expect(CFFI, "extern_smallagg_ok")
        self.assertEqual(exp, {})
        _, result = _run_typed(
            harness.source(CFFI, "extern_smallagg_ok")
        )
        self.assertEqual([e.message for e in result.errors], [])


class TestNestedStructsTodo66(unittest.TestCase):
    """todo-66: nested pure-inline struct fields across the boundary."""

    def test_nested_clean(self):
        exp = harness.expect(CFFI, "extern_nested_ok")
        self.assertEqual(exp, {})
        _, result = _run_typed(harness.source(CFFI, "extern_nested_ok"))
        self.assertEqual([e.message for e in result.errors], [])

    def test_nested_ref_field_rejected(self):
        exp = harness.expect(CFFI, "extern_nested_ref_field_rejected")
        self.assertTrue(exp.get("errors"))
        _, result = _run_typed(
            harness.source(CFFI, "extern_nested_ref_field_rejected")
        )
        self.assertTrue(
            any(
                "'b'" in e.message and "String" in e.message
                for e in result.errors
            ),
            [e.message for e in result.errors],
        )

    def test_nested_too_deep_rejected(self):
        exp = harness.expect(CFFI, "extern_nested_too_deep")
        self.assertTrue(exp.get("errors"))
        _, result = _run_typed(
            harness.source(CFFI, "extern_nested_too_deep")
        )
        self.assertTrue(
            any("nests inline structs too deeply" in e.message
                for e in result.errors),
            [e.message for e in result.errors],
        )


class TestArrayDecayTodo67(unittest.TestCase):
    """todo-67: ``[T; N]`` extern parameters follow C array decay."""

    def test_array_params_clean(self):
        exp = harness.expect(CFFI, "extern_array_param_ok")
        self.assertEqual(exp, {})
        _, result = _run_typed(
            harness.source(CFFI, "extern_array_param_ok")
        )
        self.assertEqual([e.message for e in result.errors], [])

    def test_array_return_rejected(self):
        exp = harness.expect(CFFI, "extern_array_return_rejected")
        self.assertTrue(exp.get("errors"))
        _, result = _run_typed(
            harness.source(CFFI, "extern_array_return_rejected")
        )
        self.assertTrue(
            any(
                "[Byte; 4]" in e.message and "decay" in e.message
                for e in result.errors
            ),
            [e.message for e in result.errors],
        )

    def test_array_static_rejected(self):
        exp = harness.expect(CFFI, "extern_array_static_rejected")
        self.assertTrue(exp.get("errors"))
        _, result = _run_typed(
            harness.source(CFFI, "extern_array_static_rejected")
        )
        self.assertTrue(
            any(
                "extern static 'buf'" in e.message and "decay" in e.message
                for e in result.errors
            ),
            [e.message for e in result.errors],
        )

class TestAggregateCallbacksTodo68(unittest.TestCase):
    """todo-68: aggregates in callback / fn-value signatures (repr(C))."""

    def test_agg_callback_clean(self):
        exp = harness.expect(CFFI, "extern_agg_callback_ok")
        self.assertEqual(exp, {})
        _, result = _run_typed(
            harness.source(CFFI, "extern_agg_callback_ok")
        )
        self.assertEqual([e.message for e in result.errors], [])

    def test_array_callback_param_accepted(self):
        # todo-68: 数组作回调签名的形参段按 C 退化语义放行
        exp = harness.expect(CFFI, "extern_array_callback_param_ok")
        self.assertEqual(exp, {})
        _, result = _run_typed(
            harness.source(CFFI, "extern_array_callback_param_ok")
        )
        self.assertEqual([e.message for e in result.errors], [])

    def test_array_callback_return_still_rejected(self):
        # 回调签名的返回段数组不退化 (C 中函数指针不能返回数组)
        exp = harness.expect(CFFI, "extern_array_in_callback")
        self.assertTrue(exp.get("errors"))

    def test_payload_enum_rejected(self):
        # todo-89 后 String 等引用语义载荷仍拒绝 (只有内联标量载荷放行)
        exp = harness.expect(CFFI, "extern_enum_payload_rejected")
        self.assertTrue(exp.get("errors"))
        _, result = _run_typed(
            harness.source(CFFI, "extern_enum_payload_rejected")
        )
        self.assertTrue(
            any("carrying a 'String' payload" in e.message
                for e in result.errors),
            [e.message for e in result.errors],
        )


class TestLinkNameTodo62(unittest.TestCase):
    """todo-62: ``#[link_name = "..."]`` renames the linked C symbol."""

    def test_clean_link_names(self):
        exp = harness.expect(CFFI, "extern_link_name_ok")
        self.assertEqual(exp, {})
        prog, result = _run_typed(
            harness.source(CFFI, "extern_link_name_ok")
        )
        self.assertEqual([e.message for e in result.errors], [])
        block = _extern_blocks(prog)[0]
        alias = block.fns[0]
        self.assertEqual(alias.name, "my_alias")
        self.assertEqual(alias.link_name, "real_c_fn")
        keyword = block.fns[1]
        self.assertEqual(keyword.name, "match_")
        self.assertEqual(keyword.link_name, "match")
        st = block.statics[0]
        self.assertEqual(st.name, "TAG")
        self.assertEqual(st.link_name, "ffi_tagged")

    def test_outside_extern_block_rejected(self):
        # 数据驱动: 顶层声明上的 link_name 是解析错误
        result = harness.run_pipeline(
            harness.source(CFFI, "extern_link_name_outside_rejected"),
            stage="parse",
        )
        exp = harness.expect(CFFI, "extern_link_name_outside_rejected")
        self.assertTrue(exp.get("errors"))
        messages = [e.message for e in result["errors"]]
        self.assertTrue(messages, messages)
        self.assertIn("#link_name", messages[0])
        self.assertIn("inside an extern block", messages[0])

    def test_plain_fn_has_null_link_name(self):
        doc = self._typed("extern_full_attr")
        mains = [i for i in doc["ast"]["items"] if i.get("name") == "main"]
        self.assertIsNone(mains[0]["link_name"])

    def test_typed_ast_carries_link_name(self):
        doc = self._typed("extern_link_name_ok")
        blocks = [
            i for i in doc["ast"]["items"]
            if i["kind"] == "ExternBlock"
        ]
        fns = {f["name"]: f for f in blocks[0]["fns"]}
        self.assertEqual(fns["my_alias"]["link_name"], "real_c_fn")
        self.assertEqual(fns["match_"]["link_name"], "match")
        statics = blocks[0]["statics"]
        self.assertEqual(statics[0]["name"], "TAG")
        self.assertEqual(statics[0]["link_name"], "ffi_tagged")

    def _typed(self, name: str) -> dict:
        program, _ = _run_typed(harness.source(CFFI, name))
        info = run_sa(program)
        return build_typed_ast(program, info)


class TestRelativeAnchorTodo63(unittest.TestCase):
    """todo-63: ``#[link(relative = ...)]`` path anchoring."""

    def test_relative_field_parsed(self):
        prog = _prog("extern_relative_ok")
        first = _extern_blocks(prog)[0]
        self.assertEqual(first.link_path, "./libs/libx.a")
        self.assertEqual(first.link_relative, "source")
        second = _extern_blocks(prog)[1]
        self.assertEqual(second.link_name, "m")
        self.assertIsNone(second.link_path)
        self.assertIsNone(second.link_relative)

    def test_default_anchor_is_none(self):
        # 未写 relative 时为 None (后端按 cwd 处理), 兼容旧程序
        prog = _prog("extern_full_attr")
        self.assertIsNone(_extern_blocks(prog)[0].link_relative)

    def test_bad_value_rejected(self):
        result = harness.run_pipeline(
            harness.source(CFFI, "extern_relative_bad_value"),
            stage="parse",
        )
        messages = [e.message for e in result["errors"]]
        self.assertTrue(messages, messages)
        self.assertIn("invalid link relative 'bogus'", messages[0])
        self.assertIn("cwd", messages[0])
        self.assertIn("source", messages[0])

    def test_relative_requires_path(self):
        result = harness.run_pipeline(
            harness.source(CFFI, "extern_relative_requires_path"),
            stage="parse",
        )
        messages = [e.message for e in result["errors"]]
        self.assertTrue(
            any("'relative' argument requires 'path'" in m
                for m in messages),
            messages,
        )

    def test_absolute_path_with_relative_rejected(self):
        # todo-64: 绝对 path 与 relative 锚点同现是矛盾
        result = harness.run_pipeline(
            harness.source(CFFI, "extern_relative_absolute_path"),
            stage="parse",
        )
        messages = [e.message for e in result["errors"]]
        self.assertTrue(messages, messages)
        self.assertIn("'E:/libs/libx.a' is absolute", messages[0])
        self.assertIn("'relative'", messages[0])

    def test_absolute_path_alone_still_ok(self):
        # 不带 relative 的绝对 path 依旧合法 (后端原样使用)
        src = """
#[link(path = "E:/libs/libx.a")]
extern "C" {
    fn f() -> Int32;
}
fn main() -> Int { return 0; }
"""
        _, result = _run_typed(src)
        self.assertEqual([e.message for e in result.errors], [])

    def test_path_is_absolute_matches_backend(self):
        is_abs = Parser._path_is_absolute
        self.assertTrue(is_abs("C:/x/lib.a"))
        self.assertTrue(is_abs(r"C:\x\lib.a"))
        self.assertTrue(is_abs("/usr/lib/libx.a"))
        self.assertTrue(is_abs("\\\\srv\\share\\lib.a"))
        self.assertFalse(is_abs("./libx.a"))
        self.assertFalse(is_abs("libs/libx.a"))
        self.assertFalse(is_abs("libx.a"))
        self.assertFalse(is_abs(""))
        # 与后端 cw_path_is_absolute 逐字对齐: "X:" 前缀即绝对,
        # 不含盘符的 "CD:/x" 不是
        self.assertTrue(is_abs("C:"))
        self.assertFalse(is_abs("CD:/x"))

    def test_envelope_carries_source(self):
        program, _ = _run_typed(harness.source(CFFI, "extern_relative_ok"))
        info = run_sa(program)
        doc = build_typed_ast(program, info)
        self.assertIsNone(doc["source"])
        doc = build_typed_ast(
            program, info, source=r"E:\proj\src\app.wind"
        )
        self.assertEqual(doc["source"], r"E:\proj\src\app.wind")


class TestStructPointersTodo59(unittest.TestCase):
    """todo-59: *const S / *mut S 结构体指针参数与返回."""

    def test_struct_pointer_params_accepted(self):
        exp = harness.expect(CFFI, "extern_strptr_ok")
        self.assertEqual(exp, {})
        _, result = _run_typed(
            harness.source(CFFI, "extern_strptr_ok")
        )
        self.assertEqual([e.message for e in result.errors], [])

    def test_string_field_pointee_rejected(self):
        # 被指结构体含 String 字段 (非纯内联) 仍无 C-ABI 映射
        exp = harness.expect(CFFI, "extern_strptr_bad_pointee")
        self.assertTrue(exp.get("errors"))
        _, result = _run_typed(
            harness.source(CFFI, "extern_strptr_bad_pointee")
        )
        self.assertEqual(len(result.errors), 1)


class TestOptionStringReturnTodo88(unittest.TestCase):
    """todo-88: extern 返回 Option<String> 映射可空 char*."""

    def test_option_string_return_accepted(self):
        exp = harness.expect(CFFI, "extern_optstring_ok")
        self.assertEqual(exp, {})
        _, result = _run_typed(
            harness.source(CFFI, "extern_optstring_ok")
        )
        self.assertEqual([e.message for e in result.errors], [])

    def test_option_int_return_rejected(self):
        exp = harness.expect(CFFI, "extern_optstring_int_rejected")
        self.assertTrue(exp.get("errors"))

    def test_option_string_parameter_rejected(self):
        # 可空 char* 只在返回位置成立
        exp = harness.expect(CFFI, "extern_optstring_param_rejected")
        self.assertTrue(exp.get("errors"))


class TestPayloadEnumsTodo89(unittest.TestCase):
    """todo-89: 带载荷枚举穿过 FFI."""

    def test_payload_enum_param_return_accepted(self):
        exp = harness.expect(CFFI, "extern_enumpay_ok")
        self.assertEqual(exp, {})
        _, result = _run_typed(
            harness.source(CFFI, "extern_enumpay_ok")
        )
        self.assertEqual([e.message for e in result.errors], [])

    def test_heterogeneous_payloads_rejected(self):
        # C 视图 { int32 tag; <fields> } 要求全体带载荷变体同形
        exp = harness.expect(CFFI, "extern_enumpay_hetero_rejected")
        self.assertTrue(exp.get("errors"))
        _, result = _run_typed(
            harness.source(CFFI, "extern_enumpay_hetero_rejected")
        )
        self.assertEqual(len(result.errors), 1)

    def test_payload_enum_static_rejected(self):
        # 带载荷枚举仅限函数形参/返回位; extern 静态保持旧约束
        exp = harness.expect(CFFI, "extern_enumpay_static_rejected")
        self.assertTrue(exp.get("errors"))

    def test_pointer_equality_accepted(self):
        exp = harness.expect(CFFI, "extern_ptr_eq_ok")
        self.assertEqual(exp, {})
        _, result = _run_typed(harness.source(CFFI, "extern_ptr_eq_ok"))
        self.assertEqual([e.message for e in result.errors], [])


if __name__ == "__main__":
    unittest.main()



