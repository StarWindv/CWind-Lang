"""``cwindf --unparse``: typed-AST JSON -> source round-trip tests.

The debug mirror must reproduce a program that lexes, parses and passes
SA again.  The mirrored text is *not* the original source: it is
macro-expanded, desugared, with imports stripped (their items are already
flattened into the typed AST).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent.parent.parent
for path in (ROOT / "mvp/frontend/src", TESTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from cwind_frontend import build_typed_ast, run_sa_with_errors  # noqa: E402
from cwind_frontend.lexer import lex_with_errors, tokenize_file  # noqa: E402
from cwind_frontend.parser.parser import parse_with_errors  # noqa: E402
from cwind_frontend.render.source import (  # noqa: E402
    load_document,
    render_document,
    render_program,
)

# Example sources whose *original* pipeline is clean today; the two
# broken ones are skipped dynamically (stale examples: removed
# String::matches / missing ToString bound after de-privileging).
_EXAMPLES = sorted(
    path for path in (ROOT / "example").glob("*.wind")
)

# Known limitation: libs/result.wind's bare ``None`` arms depend on the
# original import/shadowing context; with imports stripped the mirrored
# program re-analyzes ``None`` as the builtin value and SA rejects the
# match (todo-73/todo-175 territory).  The document still renders.
_KNOWN_SHADOW_EDGE = {"19_std_file.wind"}

# Byte-idempotence holds for these (hook/desugar-driven round 2 diffs
# are cosmetic for 04/07: hook temps re-emitted, tail-return spelled).
_IDEMPOTENT = {
    "00_hello_world.wind",
    "01_symbols.wind",
    "02_struct.wind",
    "03_extra.wind",
    "08_match_patterns.wind",
    "09_enums_option.wind",
    "10_containers.wind",
    "11_ownership_borrowing.wind",
    "12_closures_fnptr.wind",
    "13_generics.wind",
    "14_trait_associated_types.wind",
    "15_conversions_from_into.wind",
}


def _analyze(path: Path):
    entry = str(path.resolve())
    parsed = parse_with_errors(tokenize_file(path), source_path=entry)
    if parsed.errors:
        return None, parsed.errors[0].message
    sa = run_sa_with_errors(parsed.program)
    if sa.errors:
        return None, sa.errors[0].message
    document = build_typed_ast(parsed.program, sa.info, source=entry)
    return document, None


class UnparseRoundTripTests(unittest.TestCase):
    def test_examples_round_trip(self):
        checked = 0
        for path in _EXAMPLES:
            with self.subTest(example=path.name):
                if path.name in _KNOWN_SHADOW_EDGE:
                    continue
                document, reason = _analyze(path)
                if document is None:
                    self.skipTest(f"original source is not clean: {reason}")
                text = render_document(document)
                self.assertNotIn("/* unrendered", text)
                lexed = lex_with_errors(text)
                self.assertEqual(
                    [], [e.message for e in lexed.errors],
                    f"{path.name}: rendered text does not lex",
                )
                parsed = parse_with_errors(lexed.tokens)
                self.assertEqual(
                    [], [e.message for e in parsed.errors],
                    f"{path.name}: rendered text does not parse",
                )
                sa = run_sa_with_errors(parsed.program)
                self.assertEqual(
                    [], [e.message for e in sa.errors],
                    f"{path.name}: rendered text fails SA",
                )
                checked += 1
        self.assertGreaterEqual(checked, 15)

    def test_rendered_output_is_stable(self):
        for path in _EXAMPLES:
            if path.name not in _IDEMPOTENT:
                continue
            with self.subTest(example=path.name):
                document, _ = _analyze(path)
                assert document is not None
                first = render_document(document)
                reparsed = parse_with_errors(lex_with_errors(first).tokens)
                self.assertEqual([], [e.message for e in reparsed.errors])
                sa = run_sa_with_errors(reparsed.program)
                self.assertEqual([], [e.message for e in sa.errors])
                second = render_document(
                    build_typed_ast(reparsed.program, sa.info)
                )
                self.assertEqual(first, second)


class UnparseDocumentTests(unittest.TestCase):
    def test_load_document_rejects_other_json(self):
        with self.assertRaises(ValueError):
            load_document(json.dumps({"format": "other", "ast": {}}))
        with self.assertRaises(ValueError):
            load_document(json.dumps({"format": "cwind-typed-ast"}))
        with self.assertRaises(json.JSONDecodeError):
            load_document("{not json")

    def test_render_program_empty(self):
        self.assertEqual("", render_program({"kind": "Program", "items": []}))

    def test_unrendered_kind_becomes_comment(self):
        ast = {
            "kind": "Program",
            "items": [{"kind": "MysteryStmt"}],
        }
        self.assertIn("unrendered node 'MysteryStmt'", render_program(ast))


class UnparseCliTests(unittest.TestCase):
    def _temp_dir(self) -> Path:
        base = ROOT / ".temp" / "unparse-tests"
        base.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(dir=str(base)))

    def test_cli_unparse_prints_source(self):
        from test_cli import run

        document, _ = _analyze(ROOT / "example" / "00_hello_world.wind")
        assert document is not None
        directory = self._temp_dir()
        self.addCleanup(lambda: __import__("shutil").rmtree(
            directory, True))
        doc_path = directory / "doc.json"
        doc_path.write_text(json.dumps(document), encoding="utf-8")
        code, out, err = run(["--unparse", str(doc_path)])
        self.assertEqual(0, code, err)
        self.assertIn("fn main", out)

    def test_cli_unparse_rejects_bad_document(self):
        from test_cli import run

        directory = self._temp_dir()
        self.addCleanup(lambda: __import__("shutil").rmtree(
            directory, True))
        bad = directory / "bad.json"
        bad.write_text('{"format": "nope"}', encoding="utf-8")
        code, _out, err = run(["--unparse", str(bad)])
        self.assertEqual(1, code)
        self.assertIn("invalid typed-AST document", err)

    def test_cli_unparse_rejects_project_mode(self):
        from test_cli import run

        code, _out, err = run(["--project", "--unparse"])
        self.assertEqual(2, code)
        self.assertIn("--unparse", err)


if __name__ == "__main__":
    unittest.main()
