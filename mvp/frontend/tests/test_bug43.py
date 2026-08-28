"""bug-43 regression: expand type aliases before trait-impl validation.

``impl MyT<i32> for i32`` reported "unknown struct 'i32'" plus cascading
"unknown type 'i32'" errors.  Two root causes:

1. ``_auto_shadow_names`` counted impl/extra blocks as entry declarations
   via ``_declaration_name``'s target-type fallback, so an entry file with
   ``impl ... for i32`` silently shadowed the prelude's ``i32`` typedef;
2. the SA never expanded type aliases in impl/extra targets, so aliases
   could never satisfy the "must be a struct/enum/builtin" check, and an
   alias impl coexisting with its base-type spelling went undetected.

Single-file cases live in ``cases/bug43/``; the prelude-shadowing case is
a full project tree under ``cases/bug43/shadow_no_cascade/``.
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

CASES = TESTS / "cases" / "bug43"


class Bug43FlatCaseTests(harness.CaseAssertionsMixin):
    """Single-file cases: ``cases/bug43/<name>.wind`` + ``.json`` sidecar."""

    def test_flat_cases(self):
        for wind in sorted(CASES.glob("*.wind")):
            with self.subTest(case=wind.stem):
                self.assert_case("bug43", wind.stem)


class Bug43ProjectCaseTests(harness.CaseAssertionsMixin):
    """Project-tree cases: ``cases/bug43/<case>/`` with a libs/ tree."""

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
