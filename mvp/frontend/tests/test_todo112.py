"""todo-112: grouped ``use`` imports (project-tree cases + structural check).

``use path::{itemA, itemB};`` expands at parse time into one explicit import
per unique group element, so resolution / SA / flattening see the same node
set hand-written imports would produce.  Cases live in ``cases/todo112/<case>/``
(``libs/`` modules + ``main.wind`` + ``expect.json``).

This area is *not* swept by ``test_cases.py``: beyond the pipeline outcome it
asserts the exact ``UseDecl`` list the entry file expanded to, via a
runner-local ``expect.json`` key::

    "use_decls": ["path::item", ...]   entry-file imports, source order,
                                       auto-prelude excluded -- proves how
                                       many selectors a group really expanded
                                       to.

The pipeline itself runs through :func:`harness.run_project_case`.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent.parent.parent.parent
for path in (ROOT / "mvp/frontend/src", ROOT / "mvp/frontend/tests"):
    sys.path.insert(0, str(path))

import harness  # noqa: E402

from cwind_frontend.ast_components.ast import UseDecl  # noqa: E402

CASES = TESTS / "cases" / "todo112"


class Todo112CaseTests(harness.CaseAssertionsMixin):
    def test_case(self):
        for case_dir in harness.iter_project_cases("todo112"):
            with self.subTest(case=case_dir.name):
                self._run_case(case_dir)

    def _run_case(self, case_dir: Path) -> None:
        pc = harness.run_project_case(case_dir)
        if (
            pc.parsed is not None
            and not pc.parsed.errors
            and "use_decls" in pc.exp
        ):
            entry_source = str(pc.entry.resolve())
            decls = [
                d
                for d in pc.parsed.program.items
                if isinstance(d, UseDecl)
                and getattr(d, "source_module", None) == entry_source
            ]
            labels = ["::".join(d.parts) for d in decls]
            self.assertEqual(
                labels,
                pc.exp["use_decls"],
                f"{case_dir.name}: expanded use declarations",
            )
        self.check_outcome(pc.outcome, pc.exp, ctx=case_dir.name)


if __name__ == "__main__":
    unittest.main()
