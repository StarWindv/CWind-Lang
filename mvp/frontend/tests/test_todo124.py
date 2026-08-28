"""todo-124: module imports renamed with ``as``.

``use a::b as c;`` registers the module namespace under ``c``
(``c::item`` qualified calls and ``c::Type`` type positions both resolve),
and ``use m::item as c;`` makes bare ``c`` references in the entry file
denote ``item`` (per-file rename, scope-aware: a local binding named ``c``
shadows the alias).  Wildcard and grouped imports cannot be renamed and
fail with a dedicated parse error.

Each case lives in ``cases/todo124/<case>/`` as a full project tree
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

CASES = TESTS / "cases" / "todo124"


class Todo124CaseTests(harness.CaseAssertionsMixin):
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
