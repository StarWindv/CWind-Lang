"""Data-driven frontend case runner: discovers ``cases/`` automatically.

Historically every todo / bug got its own ``test_*.py`` that was nothing
but ``sys.path`` boilerplate plus a class calling ``assert_case("area",
"name")`` -- twenty-some near-identical driver scripts around the same two
harness primitives.  This module replaces them with pure discovery:

* ``cases/<area>/<name>.wind`` (+ optional ``<name>.json`` sidecar; absent
  means "clean") are lexed -> parsed -> semantically analyzed through
  :func:`harness.run_pipeline` and checked against the sidecar.
* ``cases/<area>/<case>/`` project trees (``libs/`` + ``main.wind`` +
  ``expect.json``) are copied to a temp dir and run through
  :func:`harness.run_project_case`.

Adding a case is dropping a data file -- no Python.  Adding a *new area*
means listing it below, because a handful of areas (``sa``/``parser``/
``lexer``/``cffi``/``cfg``/``hex``/...) are driven by bespoke test modules
that assert on typed-AST/token structure with their own sidecar schema;
those areas are intentionally NOT swept here (they would double-run and
mis-read their sidecars), and a handful of project-tree areas with bespoke
per-case checks (``todo112``'s ``use_decls``, ``bug43``) stay bespoke too.

The ``why`` behind each area (root cause of a bug, the semantics a todo
locks in) lives in ``cases/README.md``, next to the data it describes.
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

# Single-file areas (``<name>.wind`` + optional ``<name>.json``) that carry
# only pipeline-outcome expectations and have no bespoke driver module.
SINGLE_FILE_AREAS = frozenset({
    "bug33", "bug34", "bug35", "bug37", "bug38", "bug39", "bug40",
    "bug46", "bug47", "bug48", "bug49", "bug52", "bug53",
    "todo17", "todo50", "todo74", "todo87", "todo108", "todo120", "todo122",
    "todo145", "todo147",
})

# Project-tree areas (``<case>/expect.json``) swept with the shared runner.
# ``todo112`` (structural ``use_decls`` check) and ``bug43`` (mixed layout)
# are owned by bespoke modules and deliberately excluded.
PROJECT_TREE_AREAS = frozenset({
    "bug32", "bug36", "bug42", "bug47", "bug52", "bug54", "todo13",
    "todo119", "todo124", "todo125", "todo126", "todo144",
})


class SingleFileCaseTests(harness.CaseAssertionsMixin):
    def test_all(self):
        for area in sorted(SINGLE_FILE_AREAS):
            for name in harness.iter_pipeline_cases(area):
                with self.subTest(case=f"{area}/{name}"):
                    exp = harness.expect(area, name)
                    result = harness.run_pipeline(
                        harness.source(area, name), stage=exp.get("stage", "sa")
                    )
                    self.check_outcome(result, exp, ctx=f"{area}/{name}")


class ProjectTreeCaseTests(harness.CaseAssertionsMixin):
    def test_all(self):
        for area in sorted(PROJECT_TREE_AREAS):
            for case_dir in harness.iter_project_cases(area):
                with self.subTest(case=f"{area}/{case_dir.name}"):
                    pc = harness.run_project_case(case_dir)
                    self.check_outcome(
                        pc.outcome, pc.exp, ctx=f"{area}/{case_dir.name}"
                    )


if __name__ == "__main__":
    unittest.main()
