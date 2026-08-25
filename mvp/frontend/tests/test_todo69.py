"""todo-69: package/module imports across the frontend pipeline."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent.parent.parent.parent
for path in (ROOT / "mvp/frontend/src", ROOT / "mvp/frontend/tests"):
    sys.path.insert(0, str(path))

import harness  # noqa: E402

from cwind_frontend import build_typed_ast
from cwind_frontend.parser.parser import Parser
from cwind_frontend import parse_with_errors, run_sa_with_errors, tokenize_file


class PackageImportTests(unittest.TestCase):
    def _write(self, root: Path, relative: str, text: str) -> Path:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_pub_imported_function_is_callable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write(
                root,
                "libs/mathx.wind",
                "pub fn add(a: Int, b: Int) -> Int { return a + b; }\n"
                "fn hidden() -> Int { return 100; }\n",
            )
            self._write(
                root,
                "main.wind",
                "use mathx;\n"
                "fn main() -> Int { return mathx::add(2, 3); }\n",
            )
            source = (root / "main.wind").read_text(encoding="utf-8")
            parser = Parser(tokenize_file(root / "main.wind"))
            parser._IMPORT_ROOTS_BASE = root
            program = parser.parse_program()
            parsed = type("Result", (), {
                "program": program,
                "errors": parser.errors,
                "modules": parser._module_order,
            })()
            self.assertEqual([], parsed.errors)
            self.assertEqual(1, len(parsed.modules))
            result = run_sa_with_errors(parsed.program)
            self.assertEqual(
                [], [error.message for error in result.errors]
            )
            names = {symbol.name for symbol in result.info.symbols.values()}
            self.assertIn("add", names)

    def test_private_imported_function_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write(root, "libs/secret.wind", "fn hidden() -> Int { return 1; }")
            self._write(
                root,
                "main.wind",
                "use secret;\nfn main() -> Int { return secret::hidden(); }\n",
            )
            source = (root / "main.wind").read_text(encoding="utf-8")
            parser = Parser(tokenize_file(root / "main.wind"))
            parser._IMPORT_ROOTS_BASE = root
            program = parser.parse_program()
            parsed = type("Result", (), {
                "program": program,
                "errors": parser.errors,
                "modules": parser._module_order,
            })()
            self.assertEqual([], parsed.errors)
            result = run_sa_with_errors(parsed.program)
            self.assertTrue(any("private" in error.message for error in result.errors))

    def test_missing_module_is_a_parse_error(self):
        with tempfile.TemporaryDirectory() as td:
            main = Path(td) / "main.wind"
            main.write_text("use no_such_module;\n", encoding="utf-8")
            parsed = parse_with_errors(tokenize_file(main))
            self.assertTrue(any(
                "cannot find module 'no_such_module'" in error.message
                for error in parsed.errors
            ))

    def test_recursive_cycle_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write(root, "libs/a.wind", "use b;\n")
            self._write(root, "libs/b.wind", "use a;\n")
            main = self._write(root, "main.wind", "use a;\n")
            parser = Parser(tokenize_file(main))
            parser._IMPORT_ROOTS_BASE = root
            parser.parse_program()
            errors = parser.errors
            self.assertTrue(any(
                "recursive module import" in error.message
                for error in errors
            ))

    def test_typed_ast_records_import_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write(
                root,
                "libs/mathx.wind",
                "pub fn add(a: Int, b: Int) -> Int { return a + b; }\n",
            )
            self._write(
                root,
                "main.wind",
                "use mathx;\n"
                "fn main() -> Int { return mathx::add(2, 3); }\n",
            )
            parser = Parser(tokenize_file(root / "main.wind"))
            parser._IMPORT_ROOTS_BASE = root
            program = parser.parse_program()
            result = run_sa_with_errors(program)
            self.assertEqual([], result.errors)
            doc = build_typed_ast(program, result.info, source=str(root / "main.wind"))
            self.assertEqual(1, len(doc["imports"]))
            self.assertEqual(["mathx"], doc["imports"][0]["path"])
            self.assertEqual(str((root / "libs" / "mathx.wind").resolve()), doc["imports"][0]["source"])
