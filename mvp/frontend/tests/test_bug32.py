"""bug-32 regression: std-internal wildcard imports and explicit item
imports through reorganized libc wrapper modules (data-driven).

Each case lives in ``cases/bug32/<case>/`` as a full project tree:
``libs/`` modules + a ``main.wind`` entry + ``expect.json`` (the harness
expectation schema).  The test copies the tree into a temp project,
parses the entry with a real ``source_path`` and compares the pipeline
result against the expectation.

Gaps fixed (all in the parser import system):

1. wildcard ``use m::*;`` *inside* a module could not feed its items to
   the dependency closure -- the alias index keyed on the use's last
   segment, which is ``*`` for wildcards;
2. nameless ``pub extern "C"`` blocks were skipped by wildcard module
   selection, so bindings migrated into a separate libc wrapper module
   never entered the compile/export surface ("Unknown function");
3. ``_merge_auto_prelude`` treated explicit-import flattenings as
   entry-local declarations, silently stripping the prelude's ``pub use``
   re-exports whenever an import's dependency closure happened to pull
   the same name ("belongs to another module").
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

CASES = TESTS / "cases" / "bug32"


class Bug32CaseTests(harness.CaseAssertionsMixin):
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
