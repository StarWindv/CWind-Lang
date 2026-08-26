"""bug-36 regression: parse errors inside imported modules must be
reported against the module file, not mis-rendered against the entry
file's text (the "Parameter requires a type annotation" error used to
point at an unrelated closing brace of the entry program).

Each case is a project tree under ``cases/bug36/<case>/`` with a
``libs/`` tree and a ``main.wind`` entry, run with a real ``source_path``
so imports resolve the same way the CLI does.
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

from cwind_frontend import run_sa_with_errors  # noqa: E402
from cwind_frontend.parser.parser import parse_with_errors  # noqa: E402
from cwind_frontend import tokenize_file  # noqa: E402

CASES = TESTS / "cases" / "bug36"


class Bug36CaseTests(harness.CaseAssertionsMixin):
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

    def test_module_error_attributes_source(self):
        """bad_param_in_module 的报错必须归属到 stdlib.wind 模块文件."""
        case_dir = CASES / "bad_param_in_module"
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
            self.assertEqual(len(parsed.errors), 1)
            err = parsed.errors[0]
            self.assertIsNotNone(err.source, "bug-36: 模块内错误必须带 source")
            self.assertEqual(
                str(Path(str(err.source)).resolve()),
                str((root / "libs/simplified_libc/stdlib.wind").resolve()),
                "bug-36: 模块内错误必须带模块文件路径",
            )
            self.assertEqual((err.line, err.column), (8, 20))


if __name__ == "__main__":
    unittest.main()
