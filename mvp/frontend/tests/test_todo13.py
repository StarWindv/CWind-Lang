"""todo-13 / bug-44: struct field ``pub`` visibility.

Parser already accepts ``pub`` on struct fields and SA gates cross-module
access (todo-90's ``_check_field_visibility``).  These project-tree cases
lock the whole surface in:

- ``pub`` fields are readable, writable and positionally constructible
  from other modules;
- private fields reject read, write, positional construction *and* access
  from an ``extra`` block living in another module, each with exactly one
  "is private" diagnostic;
- same-module code sees private fields freely (Rust module semantics);
  a ``pub`` constructor bridges modules.

Each case lives in ``cases/todo13/<case>/`` as a full project tree
(``libs/`` modules + ``main.wind`` entry + ``expect.json``).
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent.parent.parent.parent
for path in (ROOT / "mvp/frontend/src", ROOT / "mvp/frontend/tests"):
    sys.path.insert(0, str(path))

import harness  # noqa: E402

from cwind_frontend import run_sa_with_errors, tokenize_file  # noqa: E402
from cwind_frontend.parser.parser import parse_with_errors  # noqa: E402

CASES = TESTS / "cases" / "todo13"


class Todo13CaseTests(harness.CaseAssertionsMixin):
    def test_case(self):
        for case_dir in sorted(p for p in CASES.iterdir() if p.is_dir()):
            with self.subTest(case=case_dir.name):
                self._run_case(case_dir)

    def _run_case(self, case_dir: Path) -> None:
        exp = json.loads((case_dir / "expect.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copytree(
                case_dir,
                root,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("expect.json"),
            )
            entry = root / "main.wind"
            parsed = parse_with_errors(
                tokenize_file(entry), source_path=str(entry.resolve())
            )
            if parsed.errors:
                result = {
                    "kind": "parse_err",
                    "errors": list(parsed.errors),
                    "warnings": [],
                }
            else:
                sa = run_sa_with_errors(parsed.program)
                result = {
                    "kind": "sa_err" if sa.errors else "clean",
                    "errors": list(sa.errors),
                    "warnings": list(sa.warnings),
                }
            self.check_outcome(result, exp, ctx=case_dir.name)


if __name__ == "__main__":
    unittest.main()
