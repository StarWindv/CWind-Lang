"""Shared plumbing for the data-driven frontend tests.

Every pipeline-level test case lives on disk as a file pair under
``cases/<area>/``::

    <name>.wind   -- 源码: the CWind program fed into the frontend
    <name>.json   -- 预期: the expected outcome (absent sidecar == clean)

The ``test_*.py`` modules are 串联脚本: they discover these pairs, feed the
source through the frontend and compare against the expectation.

Sources are read as raw bytes (no universal-newline translation) so CRLF,
BOMs and missing trailing newlines survive exactly as authored;
``*.wind -text`` in ``.gitattributes`` keeps git from normalizing them.

Expectation schema (all keys optional)::

    {
        "stage": "lex" | "parse" | "sa",   limit pipeline depth (default: sa)
        "kind": "clean" | "lex_err" | "parse_err" | "sa_err",
        "count": 2,                    exact number of stage errors
        "errors": [                    each entry must be matched by >=1 error
            "substring",
            {"contains": "...", "contains_all": ["a", "b"],
             "line": 1, "column": 15,
             "end_line": 1, "end_column": 5, "category": "..."}
        ],
        "forbid_errors": ["substring"],   no error message may contain these
        "positions": [[4, 19]],        some error starts at each (line, column)
        "warnings": ["substring" | {...}],   matched like ``errors``
        "warnings_exact": ["full message"]   exact warning-message list
    }

``kind`` defaults to ``clean`` unless ``errors`` / ``count`` /
``positions`` imply a failing stage.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any, Iterator, NamedTuple, Optional

TESTS_DIR = Path(__file__).resolve().parent
CASES_DIR = TESTS_DIR / "cases"


def source(area: str, name: str) -> str:
    """Raw text of ``cases/<area>/<name>.wind``, byte-exact."""
    path = CASES_DIR / area / f"{name}.wind"
    return path.read_bytes().decode("utf-8")


def expect(area: str, name: str) -> dict:
    """Parsed ``cases/<area>/<name>.json`` sidecar; ``{}`` when absent."""
    path = CASES_DIR / area / f"{name}.json"
    if not path.exists():
        return {}
    return json.loads(path.read_bytes().decode("utf-8"))


def run_pipeline(text: str, stage: str = "sa") -> dict:
    """Run lex -> parse -> SA, stopping at the first failing stage or at
    ``stage`` (``"lex"`` / ``"parse"`` / ``"sa"``), whichever comes first.

    Returns ``{"kind", "errors", "warnings"}`` where ``errors`` are the
    error objects of the deepest stage that reported any.
    """
    from cwind_frontend import lex_with_errors, parse_with_errors, run_sa_with_errors

    lexed = lex_with_errors(text)
    if lexed.errors or stage == "lex":
        return {
            "kind": "lex_err" if lexed.errors else "clean",
            "errors": lexed.errors,
            "warnings": lexed.warnings,
        }
    parsed = parse_with_errors(lexed.tokens)
    if parsed.errors or stage == "parse":
        return {
            "kind": "parse_err" if parsed.errors else "clean",
            "errors": parsed.errors,
            "warnings": [],
        }
    sa = run_sa_with_errors(parsed.program)
    return {
        "kind": "sa_err" if sa.errors else "clean",
        "errors": list(sa.errors),
        "warnings": list(sa.warnings),
    }


class ProjectCase(NamedTuple):
    """Everything a project-tree case exposes to its runner.

    ``outcome`` is the ``{"kind", "errors", "warnings"}`` dict for
    :meth:`CaseAssertionsMixin.check_outcome`; ``exp`` is the parsed
    ``expect.json``; ``parsed`` / ``sa`` are the raw stage results (``sa`` is
    ``None`` when the pipeline stopped at parse); ``entry`` is the resolved
    entry path inside the throwaway copy (kept so a runner can assert on
    per-case structure such as the entry's expanded ``UseDecl`` list).
    """

    outcome: dict
    exp: dict
    parsed: Any
    sa: Any
    entry: Path


def run_project_case(
    case_dir: Path,
    *,
    exp: Optional[dict] = None,
    target_os: Optional[str] = None,
) -> ProjectCase:
    """Run one ``cases/<area>/<case>/`` project tree through the frontend.

    Consolidates the copy-a-tree-into-temp-dir -> parse the entry with a real
    ``source_path`` -> (SA) sequence that every project-tree driver used to
    hand-copy.  ``expect.json`` may name the entry relative to the case root
    via an ``entry`` key (default ``main.wind``); ``stage`` set to ``"parse"``
    stops before semantic analysis.  Returns a :class:`ProjectCase`; callers
    assert with :meth:`CaseAssertionsMixin.check_outcome`.
    """
    from cwind_frontend import run_sa_with_errors, tokenize_file
    from cwind_frontend.parser.parser import parse_with_errors

    case_dir = Path(case_dir)
    if exp is None:
        exp = json.loads(case_dir.joinpath("expect.json").read_bytes().decode("utf-8"))
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        shutil.copytree(
            case_dir,
            root,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("expect.json"),
        )
        entry = root / exp.get("entry", "main.wind")
        parsed = parse_with_errors(
            tokenize_file(entry),
            source_path=str(entry.resolve()),
            target_os=target_os,
        )
        if parsed.errors or exp.get("stage") == "parse":
            outcome = {
                "kind": "parse_err" if parsed.errors else "clean",
                "errors": list(parsed.errors),
                "warnings": [],
            }
            return ProjectCase(outcome, exp, parsed, None, entry)
        sa = run_sa_with_errors(parsed.program)
        outcome = {
            "kind": "sa_err" if sa.errors else "clean",
            "errors": list(sa.errors),
            "warnings": list(sa.warnings),
        }
        return ProjectCase(outcome, exp, parsed, sa, entry)


def iter_pipeline_cases(area: str) -> Iterator[str]:
    """Case names (``<name>.wind`` stems) of a single-file area."""
    base = CASES_DIR / area
    for wind in sorted(base.glob("*.wind")):
        yield wind.stem


def iter_project_cases(area: str) -> Iterator[Path]:
    """Project-tree case dirs (each holding an ``expect.json``) of an area."""
    base = CASES_DIR / area
    for child in sorted(p for p in base.iterdir() if p.is_dir()):
        if (child / "expect.json").exists():
            yield child


def _matches(err, spec) -> bool:
    if isinstance(spec, str):
        return spec in err.message
    if "contains" in spec and spec["contains"] not in err.message:
        return False
    for sub in spec.get("contains_all", []):
        if sub not in err.message:
            return False
    for key in ("line", "column", "end_line", "end_column"):
        if key in spec and getattr(err, key) != spec[key]:
            return False
    if "category" in spec and getattr(err, "category", None) != spec["category"]:
        return False
    return True


class CaseAssertionsMixin(unittest.TestCase):
    """Adds ``assert_case`` / ``assert_source`` to a TestCase."""

    def assert_source(self, text: str, exp: dict, ctx: str = "") -> None:
        """Feed ``text`` through the pipeline and check it against ``exp``."""
        result = run_pipeline(text, stage=exp.get("stage", "sa"))
        self.check_outcome(result, exp, ctx=ctx)

    def check_outcome(self, result: dict, exp: dict, ctx: str = "") -> None:
        """Assert an already-computed pipeline result against ``exp``."""
        implied_error = bool(
            exp.get("errors") or exp.get("count") or exp.get("positions")
        )
        label = f"{ctx}: " if ctx else ""
        if "kind" in exp or not implied_error:
            want_kind = exp.get("kind", "clean")
            self.assertEqual(
                result["kind"], want_kind,
                f"{label}pipeline ended '{result['kind']}', expected "
                f"'{want_kind}'\nerrors: {[e.message for e in result['errors']]}",
            )
        else:
            self.assertNotEqual(
                result["kind"], "clean",
                f"{label}expected a failing pipeline\nsource expectations: {exp}",
            )

        errors = result["errors"]
        if "count" in exp:
            self.assertEqual(len(errors), exp["count"], label + ctx)
        for spec in exp.get("errors", []):
            self.assertTrue(
                any(_matches(e, spec) for e in errors),
                f"{label}no error matches {spec!r}; got "
                f"{[e.message for e in errors]}",
            )
        for spec in exp.get("forbid_errors", []):
            offenders = [e.message for e in errors
                         if (spec in e.message if isinstance(spec, str)
                             else _matches(e, spec))]
            self.assertEqual(
                offenders, [],
                f"{label}errors must not match {spec!r}",
            )
        for line, column in exp.get("positions", []):
            self.assertTrue(
                any((e.line, e.column) == (line, column) for e in errors),
                f"{label}no error positioned at {(line, column)}; got "
                f"{[(e.line, e.column) for e in errors]}",
            )
        for spec in exp.get("warnings", []):
            self.assertTrue(
                any(_matches(w, spec) for w in result["warnings"]),
                f"{label}no warning matches {spec!r}; got "
                f"{[w.message for w in result['warnings']]}",
            )
        if "warnings_exact" in exp:
            self.assertEqual(
                [w.message for w in result["warnings"]],
                exp["warnings_exact"],
                label + ctx,
            )

    def assert_case(self, area: str, name: str) -> dict:
        """Load a case pair, run it through the pipeline and verify."""
        exp = expect(area, name)
        self.assert_source(source(area, name), exp, ctx=f"{area}/{name}")
