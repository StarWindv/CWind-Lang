"""todo-112: grouped ``use`` imports (data-driven).

``use path::{itemA, itemB};`` expands at parse time into one explicit
import per unique group element, so resolution / SA / flattening see the
same node set hand-written imports would produce.  Each case lives in
``cases/todo112/<case>/`` as a full project tree: ``libs/`` modules + a
``main.wind`` entry + ``expect.json``.  The harness expectation schema is
extended with one runner-local key:

    "use_decls": ["path::item", ...]   exact UseDecl list of the *entry*
                                       file, in source order, auto-prelude
                                       excluded -- proves how many selectors
                                       the group really expanded to.
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
from cwind_frontend.ast_components.ast import UseDecl  # noqa: E402
from cwind_frontend.parser.parser import parse_with_errors  # noqa: E402
from cwind_frontend import tokenize_file  # noqa: E402

CASES = TESTS / "cases" / "todo112"


class Todo112CaseTests(harness.CaseAssertionsMixin):
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
            if not parsed.errors and "use_decls" in exp:
                entry_source = str(entry.resolve())
                decls = [
                    d
                    for d in parsed.program.items
                    if isinstance(d, UseDecl)
                    and getattr(d, "source_module", None) == entry_source
                ]
                labels = ["::".join(d.parts) for d in decls]
                self.assertEqual(
                    labels,
                    exp["use_decls"],
                    f"{case_dir.name}: expanded use declarations",
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
