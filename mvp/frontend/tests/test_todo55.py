"""todo-55: reverse FFI — ``#[export]`` / ``#[export(name = "...")]``.

Covers the frontend half of the feature:

* parser/AST surface (export name default + override, rejected shapes);
* SA validation (C-ABI parity with ``extern`` declarations, duplicate
  symbol names, ``--emit share`` rejecting a top-level ``main``);
* typed-AST annotation contract consumed by the backend (flat pointer
  spelling for references on the export boundary);
* the CLI ``--emit share`` switch.

Pipeline-level error cases live in ``cases/todo55/``.
"""

import io
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

from cwind_frontend import build_typed_ast, run_sa_with_errors, tokenize_file
from cwind_frontend.cli import main
from cwind_frontend.parser.parser import parse_with_errors


@contextmanager
def _entry(text: str):
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "main.wind"
        path.write_text(text, encoding="utf-8")
        yield path


def _parse(text: str):
    with _entry(text) as path:
        return parse_with_errors(tokenize_file(path))


def _sa(text: str, *, share: bool = False):
    """Parse + SA outside a project root (stdin-style in-memory scope)."""
    parsed = _parse(text)
    if parsed.errors:
        return parsed.program, None
    return parsed.program, run_sa_with_errors(parsed.program, share=share)


def _messages(errors) -> list[str]:
    return [e.message for e in errors]


class ExportParserTests(unittest.TestCase):
    def test_export_name_defaults_to_fn_name(self):
        program, result = _sa(
            "#[export]\nfn add(a: Int32, b: Int32) -> Int32 { return a + b; }\n"
        )
        self.assertIsNotNone(result)
        self.assertEqual([], _messages(result.errors))
        fns = [i for i in program.items if getattr(i, "name", None) == "add"]
        self.assertEqual("add", fns[0].export_name)

    def test_export_name_override(self):
        program, result = _sa(
            '#[export(name = "cw_add")]\n'
            "fn add(a: Int32, b: Int32) -> Int32 { return a + b; }\n"
        )
        self.assertEqual([], _messages(result.errors))
        fns = [i for i in program.items if getattr(i, "name", None) == "add"]
        self.assertEqual("cw_add", fns[0].export_name)

    def test_non_exported_fn_has_no_export_name(self):
        program, _ = _sa("fn plain() -> Int { return 0; }\n")
        fns = [i for i in program.items if getattr(i, "name", None) == "plain"]
        self.assertIsNone(fns[0].export_name)

    def test_unknown_argument(self):
        parsed = _parse('#[export(tag = "x")]\nfn f() -> Int { return 0; }\n')
        self.assertTrue(any(
            "unsupported 'export' argument" in m
            for m in _messages(parsed.errors)
        ))

    def test_duplicate_export_attribute(self):
        parsed = _parse(
            "#[export]\n#[export]\nfn f() -> Int { return 0; }\n"
        )
        self.assertTrue(any(
            "duplicate 'export' attribute" in m
            for m in _messages(parsed.errors)
        ))

    def test_empty_export_name(self):
        parsed = _parse('#[export(name = "")]\nfn f() -> Int { return 0; }\n')
        self.assertTrue(any(
            "export name cannot be empty" in m
            for m in _messages(parsed.errors)
        ))

    def test_generic_function_rejected(self):
        parsed = _parse("#[export]\nfn gen<T>(v: T) -> T { return v; }\n")
        self.assertTrue(any(
            "generic function 'gen' cannot be exported" in m
            for m in _messages(parsed.errors)
        ))

    def test_method_rejected(self):
        parsed = _parse(
            "trait T { fn m(self) -> Int; }\n"
            "struct S { x: Int }\n"
            "impl T for S {\n"
            "    #[export]\n"
            "    fn m(self) -> Int { return 0; }\n"
            "}\n"
        )
        self.assertTrue(any(
            "attributes are not supported on methods" in m
            for m in _messages(parsed.errors)
        ))

    def test_extern_member_rejected(self):
        parsed = _parse(
            'extern "C" {\n    #[export]\n'
            "    fn puts(s: *const u8) -> Int;\n}\n"
        )
        self.assertTrue(any(
            "unsupported attribute inside an extern block" in m
            for m in _messages(parsed.errors)
        ))


class ExportSaTests(unittest.TestCase):
    def test_export_signature_uses_forward_ffi_surface(self):
        _, result = _sa(
            "#[export]\nfn bad(v: Vector<Int>) -> Int { return 0; }\n"
        )
        self.assertTrue(any(
            "parameter 'v' of exported function 'bad'" in m
            and "no C-ABI mapping" in m
            for m in _messages(result.errors)
        ))

    def test_duplicate_export_symbols(self):
        _, result = _sa(
            "#[export]\nfn a() -> Int { return 1; }\n"
            '#[export(name = "a")]\nfn b() -> Int { return 2; }\n'
        )
        self.assertTrue(any(
            "duplicate exported symbol name 'a'" in m
            for m in _messages(result.errors)
        ))

    def test_share_rejects_main(self):
        text = "fn main() -> Int { return 0; }\n"
        _, exe = _sa(text)
        self.assertEqual([], _messages(exe.errors))
        _, share = _sa(text, share=True)
        self.assertTrue(any(
            "share mode: the entry must not declare 'main'" in m
            for m in _messages(share.errors)
        ))

    def test_share_allows_entry_without_main(self):
        _, result = _sa(
            "#[export]\nfn add(a: Int32, b: Int32) -> Int32 {\n"
            "    return a + b;\n}\n",
            share=True,
        )
        self.assertEqual([], _messages(result.errors))

    def test_option_param_rejected_like_extern(self):
        _, result = _sa(
            "#[export]\nfn bad(o: Option<String>) -> Int { return 0; }\n"
        )
        self.assertTrue(any(
            "Option crosses the boundary as a return type only" in m
            for m in _messages(result.errors)
        ))


class ExportTypedAstTests(unittest.TestCase):
    def _doc(self, text: str):
        parsed = _parse(text)
        self.assertEqual([], _messages(parsed.errors))
        result = run_sa_with_errors(parsed.program)
        self.assertEqual([], _messages(result.errors))
        return build_typed_ast(parsed.program, result.info)

    def test_typed_ast_carries_export_name(self):
        doc = self._doc(
            "#[export]\nfn a() -> Int { return 1; }\n"
            '#[export(name = "b_ext")]\nfn b() -> Int { return 2; }\n'
        )

        def fns(node):
            if isinstance(node, dict):
                if node.get("kind") == "FnDecl":
                    yield node
                for value in node.values():
                    yield from fns(value)
            elif isinstance(node, list):
                for value in node:
                    yield from fns(value)

        names = {f["name"]: f.get("export_name") for f in fns(doc["ast"])}
        self.assertEqual("a", names["a"])
        self.assertEqual("b_ext", names["b"])

    def test_ref_param_annotation_is_flat_pointer(self):
        """The backend adapter reads the flat ``ann.type`` view: the
        degraded ``*const T`` spelling must survive pass 3."""
        doc = self._doc(
            "#[export]\nfn peek(p: &Int32) -> Int32 { return *p; }\n"
        )

        def params(node):
            if isinstance(node, dict):
                if node.get("kind") == "Param" and node.get("name") == "p":
                    yield node
                for value in node.values():
                    yield from params(value)
            elif isinstance(node, list):
                for value in node:
                    yield from params(value)

        p = next(params(doc["ast"]))
        self.assertEqual("*const Int32", p["type"]["ann"]["type"]["name"])


class ExportCliTests(unittest.TestCase):
    def _run_cli(self, argv: list[str]) -> tuple[int, str]:
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue() + err.getvalue()

    def test_emit_share_flag_rejects_main(self):
        with _entry("fn main() -> Int { return 0; }\n") as path:
            code, text = self._run_cli(
                ["--emit", "share", "--typed-ast", str(path)]
            )
        self.assertNotEqual(0, code)
        self.assertIn("must not declare 'main'", text)

    def test_emit_share_flag_accepts_library(self):
        with _entry(
            "#[export]\nfn add(a: Int32, b: Int32) -> Int32 {\n"
            "    return a + b;\n}\n"
        ) as path:
            code, text = self._run_cli(
                ["--emit", "share", "--typed-ast", str(path)]
            )
        self.assertEqual(0, code, text)
        self.assertIn('"export_name": "add"', text)


if __name__ == "__main__":
    unittest.main()
