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


if __name__ == "__main__":
    unittest.main()
