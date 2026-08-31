"""Self-contained tests for the backend blast module (``cwind_fuzz.backend``).

These keep the tool honest: the GC-churn programs the builder synthesizes must
(a) pass the frontend SA, and (b) — when a built toolchain is present — actually
compile and run to completion (rc 0), which is the regression todo-155 fixes.

Run directly or via pytest/unittest discovery::

    python mvp/fuzz/tests/test_fuzz_backend.py
"""

from __future__ import annotations

import pathlib
import random
import unittest

TESTS_DIR = pathlib.Path(__file__).resolve().parent
FUZZ_DIR = TESTS_DIR.parent
import sys
if str(FUZZ_DIR) not in sys.path:
    sys.path.insert(0, str(FUZZ_DIR))

from cwind_fuzz import frontend as fe  # noqa: E402
from cwind_fuzz import backend as be  # noqa: E402
from cwind_fuzz.paths import find_cwindc  # noqa: E402


_HAS_TOOLCHAIN = find_cwindc().exists()


def _ascii_workdir() -> pathlib.Path:
    """A repo-local scratch dir (ASCII path).  The mingw linker chokes on the
    Windows temp dir when ``USERPROFILE`` contains non-ASCII (our checkout
    lives under ``C:\\Users\\星灿长风v``), so we never put build artifacts in
    ``tempfile``."""
    d = FUZZ_DIR / "_selftest_out"
    if d.exists():
        import shutil

        shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    return d


class TestGcProgramBuilder(unittest.TestCase):
    def test_programs_are_frontend_clean(self):
        """Every synthesized churn program must pass the SA (the whole
        backend premise is that a clean program should compile+run)."""
        for seed in range(24):
            src = be.GcProgramBuilder(random.Random(seed)).build()
            res = fe.analyze(src)
            self.assertEqual(
                res["kind"], "clean",
                msg=f"seed {seed} not clean: {res['sa_errors'][:3]}",
            )

    def test_program_has_main_and_churn_and_collect(self):
        src = be.GcProgramBuilder(random.Random(3)).build()
        self.assertIn("fn main() -> Int", src)
        self.assertIn("fn churn(n: Int) -> Int", src)
        self.assertIn("builtins::gc_collect();", src)
        # the collect happens *inside* the callee (the hang_g5 shape), i.e.
        # before churn's own return, not only at end of main.
        self.assertIn("while (i < n) {", src)


@unittest.skipUnless(_HAS_TOOLCHAIN, "build/cwindc not present")
class TestBackendPipeline(unittest.TestCase):
    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(FUZZ_DIR / "_selftest_out", ignore_errors=True)

    def test_single_program_compiles_and_runs(self):
        src = be.GcProgramBuilder(random.Random(1234)).build()
        out = _ascii_workdir() / "case"
        res = be.compile_and_run(src, out, timeout_run=20.0)
        self.assertEqual(res.get("kind"), "ok", msg=str(res)[:2000])
        self.assertEqual(res.get("rc"), 0)

    def test_campaign_writes_report(self):
        td = _ascii_workdir()
        cases = [
            be.BackendCase(index=i,
                           source=be.GcProgramBuilder(random.Random(i)).build())
            for i in range(2)
        ]
        report = be.run_backend_campaign(
            cases, td, "t", timeout_run=20.0, report_every=0,
        )
        self.assertEqual(report["total"], 2)
        self.assertGreaterEqual(report["counts"]["ok"], 0)
        self.assertTrue((td / "t_backend_report.json").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
