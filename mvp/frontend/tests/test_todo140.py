"""todo-140: 内建 ``Vector::with_capacity(capacity) -> Self``.

声明面唯一来源是 ``libs/builtins/mod.wind`` 的 extern "CWind" 块:
``#[link_name = "cwvec_with_capacity"]`` 把静态方法绑定到 rt 符号,
后端按 ``ann.call.callee_kind == "method"`` + 声明 link_name 通用分派
(零名字特判)。单文件数据用例在 ``cases/todo140/``; 本模块补 typed-AST
结构断言 (绑定种类 / link_name 落点 / 参数类型校验)。

结构断言需要 prelude 物化后的外部声明节点, 因此把用例源写进临时
目录并以真实 ``source_path`` 解析 (与 ``test_todo182`` 同法)。
"""

import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

import harness

from cwind_frontend import (
    build_typed_ast,
    parse_source,
    run_sa_with_errors,
    tokenize_file,
)
from cwind_frontend.parser.parser import parse_with_errors

AREA = "todo140"


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


class WithCapacityPipelineCases(harness.CaseAssertionsMixin):
    def test_cases(self):
        for name in harness.iter_pipeline_cases(AREA):
            with self.subTest(case=f"{AREA}/{name}"):
                exp = harness.expect(AREA, name)
                result = harness.run_pipeline(
                    harness.source(AREA, name),
                    stage=exp.get("stage", "sa"),
                )
                self.check_outcome(result, exp, ctx=f"{AREA}/{name}")


class WithCapacityBindingTests(unittest.TestCase):
    def _doc(self, name):
        with tempfile.TemporaryDirectory() as td:
            entry = Path(td) / "main.wind"
            entry.write_bytes(
                (harness.CASES_DIR / AREA / f"{name}.wind").read_bytes()
            )
            parsed = parse_with_errors(
                tokenize_file(entry), source_path=str(entry.resolve())
            )
            self.assertEqual([], [e.message for e in parsed.errors])
            result = run_sa_with_errors(parsed.program)
            self.assertEqual([], [e.message for e in result.errors])
            return build_typed_ast(parsed.program, result.info)

    def test_call_binds_extern_decl_with_link_name(self):
        doc = self._doc("with_capacity_ok")
        call = next(
            n for n in _walk(doc["ast"])
            if n.get("kind") == "Call"
            and (n.get("callee") or {}).get("parts")
            == ["Vector", "with_capacity"]
        )
        self.assertEqual(call["ann"]["call"]["callee_kind"], "method")
        ref = call["ann"]["call"]["callee_ref"]
        binding = next(b for b in doc["bindings"] if b["id"] == ref)
        self.assertEqual(binding["owner"], "Vector")
        decl = next(
            n for n in _walk(doc["ast"]) if n.get("id") == binding["fn_id"]
        )
        self.assertEqual(decl["kind"], "FnDecl")
        self.assertEqual(decl["name"], "with_capacity")
        self.assertEqual(decl["link_name"], "cwvec_with_capacity")
        block = next(
            n for n in _walk(doc["ast"])
            if n.get("kind") == "ExternBlock"
            and n.get("id") == binding["decl_id"]
        )
        self.assertEqual(block["abi"], "CWind")

    def test_argument_type_is_checked(self):
        prog = parse_source(harness.source(AREA, "with_capacity_bad"))
        messages = [e.message for e in run_sa_with_errors(prog).errors]
        self.assertEqual(
            ["argument 1 of 'with_capacity' must be usize (UInt64), "
             "got String"],
            messages,
        )


if __name__ == "__main__":
    unittest.main()
