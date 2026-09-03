"""Tests for the cwindf command-line interface (串联脚本).

CLI input programs live in ``cases/cli``; this module copies each source
into a temporary file, invokes ``cwind_frontend.cli.main`` and asserts on
the exit code / captured output.
"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

import harness

from cwind_frontend._version import __version__
from cwind_frontend.cli import main

CLI = "cli"


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


def write_source(text):
    tmp = tempfile.TemporaryDirectory()
    path = os.path.join(tmp.name, "t.wind")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return tmp, path


def case_path(name):
    """Materialize a case source as a real file and return (tmp, path)."""
    return write_source(harness.source(CLI, name))


FAIL_CASES_DIR = TESTS.parents[2] / "assets" / "parser_fail_cases"


class TestCli(unittest.TestCase):
    def test_version_banner(self):
        code, out, _ = run(["-V"])
        self.assertEqual(code, 0)
        self.assertIn("CWind Programming Language Compiler Frontend", out)
        self.assertIn(f"Version: v{__version__}(", out)
        self.assertIn("SPDX-License-Identifier: BSD-3-Clause", out)

    def test_version_short(self):
        code, out, _ = run(["-V", "--short"])
        self.assertEqual((code, out.strip()), (0, f"v{__version__}"))

    def test_short_requires_version(self):
        code, _, err = run(["--short"])
        self.assertEqual(code, 2)
        self.assertIn("--short requires --version", err)

    def test_default_silent(self):
        tmp, path = case_path("valid_program")
        try:
            code, out, err = run([path])
        finally:
            tmp.cleanup()
        self.assertEqual(code, 0)
        self.assertEqual(out, "")

    def test_lex(self):
        tmp, path = case_path("valid_program")
        try:
            code, out, _ = run(["--lex", "--json", path])
        finally:
            tmp.cleanup()
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data[0]["kind"], "CONST")
        self.assertEqual(data[1]["value"], "hello")

    def test_parse(self):
        tmp, path = case_path("valid_program")
        try:
            code, out, _ = run(["--parse", "--json", path])
        finally:
            tmp.cleanup()
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["kind"], "Program")
        self.assertIn("FnDecl", [item["kind"] for item in data["items"]])

    def test_sa(self):
        tmp, path = case_path("valid_program")
        try:
            code, out, _ = run(["--sa", "--json", path])
        finally:
            tmp.cleanup()
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(len(data["symbols"]), 2)
        self.assertEqual(
            {sym["name"]: sym["kind"] for sym in data["symbols"]},
            {"hello": "const", "main": "fn"},
        )

    def test_verbose(self):
        tmp, path = case_path("valid_program")
        try:
            code, out, _ = run(["--verbose", "--json", path])
        finally:
            tmp.cleanup()
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertIn("tokens", data)
        self.assertEqual(data["ast"]["kind"], "Program")

    def test_json_lex(self):
        tmp, path = case_path("valid_program")
        try:
            code, out, _ = run(["--lex", "--json", path])
        finally:
            tmp.cleanup()
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data[0]["kind"], "CONST")

    def test_json_parse(self):
        tmp, path = case_path("valid_program")
        try:
            code, out, _ = run(["--parse", "--json", path])
        finally:
            tmp.cleanup()
        self.assertEqual(json.loads(out)["kind"], "Program")

    def test_json_sa(self):
        tmp, path = case_path("valid_program")
        try:
            code, out, _ = run(["--sa", "--json", path])
        finally:
            tmp.cleanup()
        self.assertIn("symbols", json.loads(out))

    def test_json_verbose(self):
        tmp, path = case_path("valid_program")
        try:
            code, out, _ = run(["--verbose", "--json", path])
        finally:
            tmp.cleanup()
        data = json.loads(out)
        self.assertIn("tokens", data)
        self.assertEqual(data["ast"]["kind"], "Program")

    def test_json_requires_stage(self):
        tmp, path = case_path("valid_program")
        try:
            code, _, err = run(["--json", path])
        finally:
            tmp.cleanup()
        self.assertEqual(code, 2)
        self.assertIn("--json requires", err)

    def test_error_rendered(self):
        tmp, path = case_path("bad_program")
        try:
            code, _, err = run([path])
        finally:
            tmp.cleanup()
        self.assertEqual(code, 1)
        self.assertIn("let needs a type", err)

    def test_parser_fail_cases_nonzero(self):
        for path in sorted(FAIL_CASES_DIR.glob("case*.wind")):
            with self.subTest(path=path.name):
                code, _, err = run([str(path)])
                self.assertNotEqual(code, 0)
                self.assertIn("Error", err)

    def test_multiple_errors_reported_at_once(self):
        tmp, path = case_path("multiple_errors")
        try:
            code, out, err = run([path])
        finally:
            tmp.cleanup()
        self.assertEqual(code, 1)
        self.assertEqual(out, "")  # parse failed: no stage output
        # all three independent errors surface in a single run
        self.assertIn("let needs a type", err)
        self.assertIn("Unexpected token ';' in expression", err)

    def test_lexer_errors_stop_pipeline(self):
        # parser never runs: lexer errors must block the next stage.
        tmp, path = case_path("lexer_errors_stop")
        try:
            code, out, err = run(["--parse", path])
        finally:
            tmp.cleanup()
        self.assertEqual(code, 1)
        self.assertIn("Unexpected character '~'", err)
        self.assertIn("Unexpected character '`'", err)
        self.assertEqual(out, "")  # no AST was printed

    def test_sa_errors_reported(self):
        tmp, path = case_path("sa_errors")
        try:
            code, _, err = run([path])
        finally:
            tmp.cleanup()
        self.assertEqual(code, 1)
        self.assertIn("Unknown type 'Missing'", err)
        self.assertIn("Unknown type 'AlsoMissing'", err)

    def test_warning_does_not_block(self):
        tmp, path = case_path("warning_escape")
        try:
            code, out, err = run([path])
        finally:
            tmp.cleanup()
        self.assertEqual(code, 0)
        self.assertIn("Unknown escape", err)

    def test_typed_ast(self):
        tmp, path = case_path("typed_ast_input")
        try:
            code, out, err = run(["--typed-ast", path])
        finally:
            tmp.cleanup()
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data["format"], "cwind-typed-ast")
        self.assertEqual(data["version"], 1)
        # top-level symbols carry AST node refs
        symbols = {sym["name"]: sym for sym in data["symbols"]}
        self.assertEqual(symbols["Point"]["kind"], "struct")
        self.assertIsInstance(symbols["Point"]["ref"], int)
        self.assertEqual(symbols["test"]["kind"], "fn")
        # extra methods appear in the binding table
        self.assertEqual(len(data["bindings"]), 1)
        binding = data["bindings"][0]
        self.assertEqual(binding["owner"], "Point")
        self.assertIsNone(binding["trait"])
        self.assertIsInstance(binding["decl_id"], int)
        self.assertIsInstance(binding["fn_id"], int)
        # every node carries an id; annotations carry types
        nodes = list(_walk_nodes(data["ast"]))
        ids = [n["id"] for n in nodes]
        self.assertEqual(ids, list(range(1, len(nodes) + 1)))
        by_id = {n["id"]: n for n in nodes}
        self.assertEqual(by_id[symbols["Point"]["ref"]]["kind"], "StructDecl")
        self.assertEqual(by_id[binding["decl_id"]]["kind"], "ExtraDecl")
        self.assertEqual(by_id[binding["fn_id"]]["kind"], "FnDecl")
        # a generic field keeps its outer shape with an opaque leaf
        field = next(
            n for n in nodes
            if n["kind"] == "Field" and n.get("name") == "x"
        )
        self.assertEqual(field["ann"]["type"], {"name": "T", "opaque": True})
        # a local variable name resolves to its LetStmt node
        let_id = next(
            n["id"] for n in nodes
            if n["kind"] == "LetStmt" and n.get("name") == "a"
        )
        name_node = next(
            n for n in nodes
            if n["kind"] == "Name" and n["ann"].get("binding", {}).get("ref") == let_id
        )
        self.assertEqual(name_node["ann"]["binding"]["kind"], "var")
        # call annotations carry callee refs and type_args
        call = next(n for n in nodes if n["kind"] == "Call")
        self.assertEqual(call["ann"]["call"]["callee_kind"], "method")
        self.assertEqual(call["ann"]["call"]["callee_ref"], binding["id"])
        self.assertIn("type_args", call["ann"]["call"])

    def test_typed_ast_sa_errors_reported(self):
        tmp, path = case_path("typed_ast_sa_error")
        try:
            code, out, err = run(["--typed-ast", path])
        finally:
            tmp.cleanup()
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("Unknown function 'missing'", err)
        self.assertIn("(in SA)", err)

    def test_typed_ast_json_flag(self):
        tmp, path = case_path("valid_program")
        try:
            code, out, _ = run(["--typed-ast", "--json", path])
        finally:
            tmp.cleanup()
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["format"], "cwind-typed-ast")

    def test_parse_json_has_no_meta(self):
        tmp, path = case_path("valid_program")
        try:
            code, out, _ = run(["--parse", "--json", path])
        finally:
            tmp.cleanup()
        self.assertEqual(code, 0)
        self.assertNotIn('"id"', out)
        self.assertNotIn('"ann"', out)

    def test_typed_ast_from_stdin(self):
        out, err = io.StringIO(), io.StringIO()
        saved_stdin = sys.stdin
        sys.stdin = io.StringIO(harness.source(CLI, "valid_program"))
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = main(["--typed-ast"])
        finally:
            sys.stdin = saved_stdin
        self.assertEqual(code, 0, err)
        data = json.loads(out.getvalue())
        self.assertEqual(data["format"], "cwind-typed-ast")
        self.assertEqual(
            {sym["name"]: sym["kind"] for sym in data["symbols"]},
            {"hello": "const", "main": "fn"},
        )

    def test_sa_warning_rendered_and_non_blocking(self):
        tmp, path = case_path("refinement_warning")
        try:
            code, out, err = run(["--typed-ast", path])
        finally:
            tmp.cleanup()
        self.assertEqual(code, 0)
        self.assertIn("Warning", err)
        self.assertIn("can never be violated", err)
        self.assertIn("Refinement", err)
        # typed AST is still produced
        self.assertEqual(json.loads(out)["format"], "cwind-typed-ast")


class TestCliPass0(unittest.TestCase):
    """todo-160: ``--pass 0`` runs only the FQN expansion pass and
    prints the structured expansion report (data/renderer separation:
    ``run_pass0`` produces the data, ``render_fqn_report`` lays it out)."""

    def _project_file(self, main_text, libs):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for rel, text in libs.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        entry = root / "main.wind"
        entry.write_text(main_text, encoding="utf-8")
        return entry

    PRELUDE = (
        "pub typedef Vec<T> = Vector<T>;\n"
        "pub typedef u64 = UInt64;\n"
        "pub typedef usize = u64;\n"
    )

    def test_pass0_reports_alias_expansions(self):
        entry = self._project_file(
            "fn main() -> Int {\n"
            "    let v: Vec<Int> = Vec::new();\n"
            "    let n: usize = 5;\n"
            "    return 0;\n"
            "}\n",
            {"libs/mod.wind": self.PRELUDE},
        )
        code, out, err = run(["--pass", "0", str(entry)])
        self.assertEqual(code, 0, err)
        self.assertIn("pass 0 (fqn-expansion)", out)
        # 每跳记录基名 (实参是子节点, 各自成行);
        # usize 链: usize -> u64 -> UInt64 -> std::builtins::UInt64
        self.assertIn("Vec", out)
        self.assertIn("std::builtins::Vector", out)
        self.assertIn("std::builtins::UInt64", out)
        # alias table present with provenance
        self.assertIn("alias table (3 entries)", out)
        self.assertIn("Vec<T>  = std::builtins::Vector<T>", out)
        self.assertIn("usize   = std::builtins::UInt64", out)

    def test_pass0_json(self):
        entry = self._project_file(
            "fn main() -> Int {\n"
            "    let v: Vec<Int> = Vec::new();\n"
            "    return 0;\n"
            "}\n",
            {"libs/mod.wind": self.PRELUDE},
        )
        code, out, _ = run(["--pass", "0", "--json", str(entry)])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["pass"], {"id": 0, "name": "fqn-expansion"})
        kinds = {(e["kind"], e["original"], e["canonical"])
                 for e in data["expansions"]}
        self.assertIn(("alias", "Vec", "std::builtins::Vector"), kinds)
        self.assertIn(("qualify", "Vector", "std::builtins::Vector"), kinds)
        self.assertEqual(
            {a["name"] for a in data["aliases"]}, {"Vec", "u64", "usize"},
        )
        # every entry carries source provenance
        for e in data["expansions"]:
            self.assertTrue(e["source"], e)

    def test_pass0_noop_records_nothing_for_already_canonical(self):
        """显式 FQN 拼写已规范 —— 不产生展开记录。"""
        entry = self._project_file(
            "fn main() -> Int {\n"
            "    let v: std::builtins::Vector<Int> = Vector::new();\n"
            "    return 0;\n"
            "}\n",
            {"libs/mod.wind": self.PRELUDE},
        )
        code, out, _ = run(["--pass", "0", "--json", str(entry)])
        self.assertEqual(code, 0)
        data = json.loads(out)
        expansions = [
            e for e in data["expansions"]
            if e["original"] == e["canonical"]
        ]
        self.assertEqual(expansions, [])

    def test_pass0_unknown_position(self):
        tmp, path = case_path("valid_program")
        try:
            code, _, err = run(["--pass", "1", path])
        finally:
            tmp.cleanup()
        self.assertEqual(code, 2)
        self.assertIn("unknown pass '1'", err)

    def test_pass0_folds_same_file_same_spelling(self):
        """同文件同 kind 同拼写的展开折叠成一行 (todo-160)。"""
        entry = self._project_file(
            "pub typedef A = Int32;\n"
            "\n"
            "fn f(a: A, b: A, c: A) -> A { return a; }\n"
            "\n"
            "fn main() -> Int { return f(1, 2, 3); }\n",
            {"libs/mod.wind": self.PRELUDE},
        )
        code, out, err = run(["--pass", "0", str(entry)])
        self.assertEqual(code, 0, err)
        self.assertIn("(6 distinct)", out)
        # 展开表中 A 引用三处折叠为一行 (PRELUDE 的 u64 alias 是另一组)
        expansions_section = out.split("alias table")[0]
        self.assertEqual(
            expansions_section.count("A   "), 1,
            expansions_section,
        )
        self.assertIn("A", expansions_section)

    def test_pass0_no_fold_keeps_positions(self):
        entry = self._project_file(
            "pub typedef A = Int32;\n"
            "\n"
            "fn f(a: A, b: A, c: A) -> A { return a; }\n"
            "\n"
            "fn main() -> Int { return f(1, 2, 3); }\n",
            {"libs/mod.wind": self.PRELUDE},
        )
        code, out, err = run(["--pass", "0", "--no-fold", str(entry)])
        self.assertEqual(code, 0, err)
        self.assertNotIn("distinct", out)
        # 每个引用独立成行且带位置
        self.assertGreaterEqual(out.count("alias"), 3)
        self.assertIn(":2:", out)
        self.assertIn(":3:", out)

    def test_pass0_no_fold_requires_pass(self):
        tmp, path = case_path("valid_program")
        try:
            code, _, err = run(["--no-fold", path])
        finally:
            tmp.cleanup()
        self.assertEqual(code, 2)
        self.assertIn("--no-fold requires --pass", err)

    def test_pass0_reports_methods_and_traits(self):
        entry = self._project_file(
            "fn main() -> Int { return 0; }\n",
            {
                "libs/mod.wind": self.PRELUDE
                + (
                    "pub extern \"CWind\" {\n"
                    "    type Vector<T>;\n"
                    "    fn Vector<T>::new() -> Self;\n"
                    "}\n"
                    "pub trait Greet {}\n"
                ),
            },
        )
        code, out, err = run(["--pass", "0", str(entry)])
        self.assertEqual(code, 0, err)
        # 方法 FQN 表 (extern "CWind" owner 经别名展开+限定)
        self.assertIn("std::builtins::Vector::new", out)
        self.assertIn("methods (1)", out)
        # trait 展开表 (定义位置规范形), 不再是 exclusions 一行
        self.assertIn("traits (1)", out)
        self.assertIn("Greet", out)
        self.assertNotIn("trait exclusions", out)

    def test_pass0_mutually_exclusive_with_stage_modes(self):
        tmp, path = case_path("valid_program")
        try:
            with self.assertRaises(SystemExit) as ctx:
                run(["--pass", "0", "--lex", path])
            self.assertEqual(ctx.exception.code, 2)
        finally:
            tmp.cleanup()

    def test_pass0_rejects_project(self):
        code, _, err = run(["--pass", "0", "--project"])
        self.assertEqual(code, 2)
        self.assertIn("--project cannot be combined", err)


def _walk_nodes(node):
    """Yield a dict node and every nested dict it contains."""
    if "kind" not in node:
        return
    yield node
    for value in node.values():
        if isinstance(value, dict) and "kind" in value:
            yield from _walk_nodes(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and "kind" in item:
                    yield from _walk_nodes(item)


if __name__ == "__main__":
    unittest.main()
