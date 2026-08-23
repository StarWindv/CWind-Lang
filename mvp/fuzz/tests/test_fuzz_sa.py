"""Standalone, self-contained tests for ``fuzz/fuzz_sa.py`` (串联脚本).

The fuzzer's core promise is: every program produced by ``--mode gen`` is
semantically valid under the *current* language rules, so any SA error it
reports is a false-positive candidate.  These tests pin that promise down:

* every feature snippet generator emits SA-clean programs (per-snippet
  regression net that automatically covers snippets added later);
* composed multi-snippet campaigns are clean across seeds;
* generation is deterministic for a fixed seed;
* ``analyze()`` classifies lex/parse/SA outcomes correctly;
* the mutator is conservative: mutations of clean programs stay clean;
* mutate-mode seed filtering skips seeds that are no longer clean;
* the CLI end-to-end writes its report and case files.

Static fixtures live in ``cases/``: the clean mutation sample, mutate-mode
seeds and a small corpus.  Corpus/seed fixtures are *discovered* by
globbing ``*.wind`` — dropping a file into ``cases/corpus`` or
``cases/seeds`` (or adding repo examples under ``example/``) extends those
tests automatically.  The generator/mutator logic itself is exercised
directly, so most of these tests are pure code (they test the tool, not a
fixed corpus).

Run directly (no pytest required)::

    python mvp/fuzz/tests/test_fuzz_sa.py

or via pytest / unittest discovery.
"""

from __future__ import annotations

import contextlib
import io
import json
import random
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent      # mvp/fuzz/tests
FUZZ_DIR = TESTS_DIR.parent                      # mvp/fuzz
REPO_ROOT = TESTS_DIR.parents[2]                 # repository root
CASES_DIR = TESTS_DIR / "cases"
if str(FUZZ_DIR) not in sys.path:
    sys.path.insert(0, str(FUZZ_DIR))

import fuzz_sa  # noqa: E402

# Corpus fixtures are *discovered*, not enumerated: every ``*.wind`` under
# these roots joins the corpus-mode test automatically.  ``assets/`` is
# deliberately excluded — programs there are historical scratch material,
# not guaranteed to pass the current frontend.
CORPUS_ROOTS = [
    CASES_DIR / "corpus",
    REPO_ROOT / "example",
]


def _quiet_main(argv: list[str]) -> int:
    """Run fuzz_sa.main with stdout/stderr captured (reports are asserted
    via the written JSON, not the console output)."""
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        return fuzz_sa.main(argv)


def _case(name: str) -> str:
    return (CASES_DIR / name).read_text(encoding="utf-8")


def _snippet_methods() -> list[str]:
    """Every per-feature generator except the top-level composer."""
    names = []
    for name in sorted(dir(fuzz_sa.Generator)):
        if not name.startswith("gen_") or name == "gen":
            continue
        attr = getattr(fuzz_sa.Generator, name)
        if callable(attr):
            names.append(name)
    return names


class TestFeatureSnippets(unittest.TestCase):
    """Each gen_* snippet must be valid by construction under current rules."""

    SAMPLES_PER_SNIPPET = 8

    def test_every_snippet_is_sa_clean(self):
        rng = random.Random(20260823)
        for method in _snippet_methods():
            for i in range(self.SAMPLES_PER_SNIPPET):
                gen = fuzz_sa.Generator(random.Random(rng.getrandbits(32)))
                src = getattr(gen, method)()
                with self.subTest(snippet=method, sample=i):
                    result = fuzz_sa.analyze(src)
                    self.assertEqual(
                        result["kind"],
                        "clean",
                        f"{method} produced a {result['kind']} program:\n"
                        f"{src}\n"
                        f"errors: {result['sa_errors'] or result['parse_errors']}",
                    )

    def test_snippets_have_unique_names_within_a_program(self):
        """Composed programs reuse the global used-set, so picking the same
        snippet twice must still yield distinct definitions."""
        rng = random.Random(7)
        gen = fuzz_sa.Generator(rng)
        parts = [gen.gen_borrow_ref(), gen.gen_borrow_ref()]
        result = fuzz_sa.analyze("\n".join(parts))
        self.assertEqual(result["kind"], "clean", result)


class TestGeneratedCampaign(unittest.TestCase):
    """Composed programs (the real --mode gen payload) must be clean."""

    def test_multi_seed_campaign_is_clean(self):
        total = 0
        for seed in (1, 42, 999):
            gen = fuzz_sa.Generator(random.Random(seed))
            for _ in range(120):
                with self.subTest(seed=seed, index=total):
                    result = fuzz_sa.analyze(gen.gen())
                    self.assertEqual(
                        result["kind"],
                        "clean",
                        f"seed={seed}: {result}",
                    )
                total += 1
        self.assertEqual(total, 360)

    def test_same_seed_is_deterministic(self):
        def sources(seed: int) -> list[str]:
            gen = fuzz_sa.Generator(random.Random(seed))
            return [gen.gen() for _ in range(20)]

        self.assertEqual(sources(123), sources(123))

    def test_different_seed_differs(self):
        gen_a = fuzz_sa.Generator(random.Random(1))
        gen_b = fuzz_sa.Generator(random.Random(2))
        # A shared RNG stream would make these identical; different streams
        # must produce at least one difference within a small sample.
        diff = any(
            gen_a.gen() != gen_b.gen() for _ in range(10)
        )
        self.assertTrue(diff)


class TestAnalyzeClassification(unittest.TestCase):
    def test_clean_program(self):
        result = fuzz_sa.analyze(_case("seeds/clean.wind"))
        self.assertEqual(result["kind"], "clean")
        self.assertEqual(result["sa_errors"], [])

    def test_sa_error_is_reported_with_position(self):
        result = fuzz_sa.analyze(_case("seeds/stale.wind"))
        self.assertEqual(result["kind"], "sa_err")
        self.assertTrue(result["sa_errors"])
        first = result["sa_errors"][0]
        self.assertIn("mut", first["message"])
        self.assertGreater(first["line"], 0)
        self.assertGreater(first["column"], 0)

    def test_lex_error_kind(self):
        result = fuzz_sa.analyze(
            "fn f() -> None { let a: Int = 1 ++ 2; }"
        )
        self.assertEqual(result["kind"], "lex_err")
        self.assertTrue(result["lex_errors"])

    def test_parse_error_kind(self):
        result = fuzz_sa.analyze("fn f() -> Int { return ) }")
        self.assertEqual(result["kind"], "parse_err")
        self.assertTrue(result["parse_errors"])

    def test_crash_kind_via_injected_failure(self):
        original = fuzz_sa.run_sa_with_errors

        def boom(_program):
            raise RuntimeError("injected")

        fuzz_sa.run_sa_with_errors = boom
        try:
            result = fuzz_sa.analyze(_case("seeds/clean.wind"))
        finally:
            fuzz_sa.run_sa_with_errors = original
        self.assertEqual(result["kind"], "crash")
        self.assertIn("RuntimeError: injected", result["traceback"])


class TestSignaturesAndKnownBugs(unittest.TestCase):
    def test_sig_of_is_order_insensitive(self):
        r1 = {"sa_errors": [
            {"line": 1, "column": 1, "message": "b"},
            {"line": 2, "column": 1, "message": "a"},
        ]}
        r2 = {"sa_errors": [
            {"line": 9, "column": 9, "message": "a"},
            {"line": 9, "column": 9, "message": "b"},
        ]}
        self.assertEqual(fuzz_sa.sig_of(r1), fuzz_sa.sig_of(r2))
        self.assertEqual(len(fuzz_sa.sig_of(r1)), 2)

    def test_match_known_bug_none_for_clean(self):
        self.assertIsNone(fuzz_sa.match_known_bug({"kind": "clean", "sa_errors": []}))

    def test_match_known_bug_hits_pattern(self):
        result = {
            "kind": "sa_err",
            "sa_errors": [
                {"line": 1, "column": 1, "message": "unknown type 'T'"}
            ],
        }
        saved = fuzz_sa.KNOWN_BUG_PATTERNS
        fuzz_sa.KNOWN_BUG_PATTERNS = [(r"unknown type 'T'", "BUG-XXX")]
        try:
            self.assertEqual(fuzz_sa.match_known_bug(result), "BUG-XXX")
        finally:
            fuzz_sa.KNOWN_BUG_PATTERNS = saved

    def test_load_known_bugs_missing_file_uses_defaults(self):
        saved = fuzz_sa.ROOT
        fuzz_sa.ROOT = Path(tempfile.gettempdir()) / "no-such-dir-xyz"
        try:
            self.assertEqual(fuzz_sa.load_known_bugs(), [])
        finally:
            fuzz_sa.ROOT = saved


_CLEAN_SAMPLE = _case("clean_sample.wind")


class TestMutatorConservative(unittest.TestCase):
    def test_mutations_of_clean_program_stay_clean(self):
        mut = fuzz_sa.Mutator(random.Random(5))
        seen_ops = set()
        for i in range(400):
            src = _CLEAN_SAMPLE
            steps = random.Random(i).randint(1, 4)
            for _ in range(steps):
                src = mut.mutate(src)
            with self.subTest(i=i):
                result = fuzz_sa.analyze(src)
                self.assertEqual(
                    result["kind"],
                    "clean",
                    f"mutation produced {result['kind']}:\n{src}\n{result}",
                )

    def test_noop_let_never_lands_at_top_level(self):
        # Regression: inserting below a single-line fn header used to emit a
        # top-level `let` (parse error).
        single_line_fn = _case("single_line_fn.wind")
        mut = fuzz_sa.Mutator(random.Random(11))
        mutated = 0
        for _ in range(200):
            src = mut.mutate(single_line_fn)
            if src != single_line_fn:
                mutated += 1
            with self.subTest(src=src):
                self.assertEqual(fuzz_sa.analyze(src)["kind"], "clean")
        self.assertGreater(mutated, 0)

    def test_each_mutation_operation_individually(self):
        ops = [
            "_reorder_decls",
            "_add_noop_fn",
            "_add_noop_let",
            "_parenthesize",
            "_swap_literals",
            "_toggle_pub",
        ]
        for op in ops:
            for i in range(20):
                m = fuzz_sa.Mutator(random.Random(100 + i))
                src = getattr(m, op)(_CLEAN_SAMPLE)
                with self.subTest(op=op, i=i):
                    self.assertEqual(
                        fuzz_sa.analyze(src)["kind"],
                        "clean",
                        f"{op} produced an invalid program:\n{src}",
                    )

    def test_mutations_actually_change_the_source(self):
        # The mutator must not silently degenerate into a no-op.
        fired = set()
        mut = fuzz_sa.Mutator(random.Random(3))
        for _ in range(100):
            after = mut.mutate(_CLEAN_SAMPLE)
            if after != _CLEAN_SAMPLE:
                fired.add(True)
        self.assertIn(True, fired)


class TestMutateSeedFiltering(unittest.TestCase):
    SEEDS = CASES_DIR / "seeds"

    @classmethod
    def _partition_seeds(cls):
        """Discover seed files and split them by their current SA verdict."""
        seeds = sorted(cls.SEEDS.glob("*.wind"))
        stale = [
            p for p in seeds
            if fuzz_sa.analyze(p.read_text(encoding="utf-8"))["kind"] != "clean"
        ]
        return seeds, stale

    def test_all_stale_seeds_rejected(self):
        _, stale = self._partition_seeds()
        self.assertTrue(stale, "expected at least one stale seed fixture")
        with tempfile.TemporaryDirectory() as td:
            rc = _quiet_main([
                "--mode", "mutate",
                "--count", "5",
                "--seeds", *[str(p) for p in stale],
                "--out", str(Path(td) / "out"),
                "--report-every", "0",
            ])
            self.assertEqual(rc, 2)

    def test_mixed_seeds_keep_only_clean(self):
        seeds, stale = self._partition_seeds()
        self.assertTrue(stale, "expected at least one stale seed fixture")
        self.assertLess(len(stale), len(seeds), "expected at least one clean seed")
        with tempfile.TemporaryDirectory() as td:
            rc = _quiet_main([
                "--mode", "mutate",
                "--count", "20",
                "--seed", "1",
                "--seeds", *[str(p) for p in seeds],
                "--out", str(Path(td) / "out"),
                "--report-every", "0",
            ])
            self.assertEqual(rc, 0)


class TestCliEndToEnd(unittest.TestCase):
    def test_gen_mode_writes_report_and_no_cases(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out"
            rc = _quiet_main([
                "--mode", "gen",
                "--count", "30",
                "--seed", "8",
                "--jobs", "4",
                "--report-every", "0",
                "--label", "smoke",
                "--out", str(out),
            ])
            self.assertEqual(rc, 0)
            report = json.loads(
                (out / "smoke_report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(report["total"], 30)
            self.assertEqual(report["counts"]["clean"], 30)
            self.assertEqual(report["counts"]["crash"], 0)
            self.assertEqual(report["counts"]["sa_err"], 0)
            self.assertEqual(report["counts"]["parse_err"], 0)
            self.assertEqual(report["counts"]["lex_err"], 0)
            self.assertEqual(list((out / "cases").glob("*")), [])

    def test_corpus_mode_counts_both_kinds(self):
        """Corpus mode over every discovered ``*.wind`` fixture: the union of
        our own corpus, the repo ``example/`` directory and ``assets/``.

        Expectations are derived by re-running ``analyze`` on the very same
        files, so the test adapts automatically as examples come and go."""
        with tempfile.TemporaryDirectory() as td:
            corpus = Path(td) / "corpus"
            corpus.mkdir()
            copied = []
            for root in CORPUS_ROOTS:
                for src in sorted(root.rglob("*.wind")):
                    dst = corpus / f"{root.name}__{src.name}"
                    dst.write_bytes(src.read_bytes())
                    copied.append(dst)
            self.assertGreater(len(copied), 0)

            expected = {"clean": 0}
            for path in sorted(copied):
                result = fuzz_sa.analyze(path.read_text(encoding="utf-8"))
                kind = result["kind"]
                if kind == "sa_err" and fuzz_sa.match_known_bug(result):
                    kind = "known_bug"
                expected[kind] = expected.get(kind, 0) + 1
            self.assertGreater(expected["clean"], 0)
            self.assertGreater(len(copied) - expected["clean"], 0)

            out = Path(td) / "out"
            rc = _quiet_main([
                "--mode", "corpus",
                "--dir", str(corpus),
                "--report-every", "0",
                "--label", "corp",
                "--out", str(out),
            ])
            self.assertEqual(rc, 0)
            report = json.loads(
                (out / "corp_report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(report["total"], len(copied))
            for kind, count in expected.items():
                self.assertEqual(report["counts"].get(kind, 0), count, kind)
            # Corpus cases are *expected* to fail sometimes; every non-clean
            # file must have been saved for replay.
            saved = list((out / "cases").glob("*.wind"))
            self.assertEqual(
                len(saved),
                len(copied) - report["counts"]["clean"],
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
