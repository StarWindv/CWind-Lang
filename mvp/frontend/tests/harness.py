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


# -- todo-158: mod.wind-driven module trees -----------------------------------

_SOURCE_SUFFIXES = (".wind", ".wd")


def _declared_names(mod_wind: Path) -> set[str]:
    import re

    if not mod_wind.is_file():
        return set()
    text = mod_wind.read_text(encoding="utf-8-sig")
    return set(
        re.findall(
            r"(?:^|\n)\s*(?:pub\s+)?mod\s+([A-Za-z_]\w*)\s*[;{]", text
        )
    )


def _module_file_of(directory: Path) -> Path:
    """The module file governing *directory* (todo-158 layout rules).

    A Breeze source root (Rust-before-2018 layout) uses its crate-root
    file ``lib.wd``/``lib.wind``; every other directory uses the
    ``mod.wind``/``mod.wd`` directory-module file.
    """
    lib = next(
        (
            directory / f"lib{suffix}"
            for suffix in _SOURCE_SUFFIXES
            if (directory / f"lib{suffix}").is_file()
        ),
        None,
    )
    if lib is not None:
        return lib
    mod = next(
        (
            directory / f"mod{suffix}"
            for suffix in _SOURCE_SUFFIXES
            if (directory / f"mod{suffix}").is_file()
        ),
        None,
    )
    if mod is not None:
        return mod
    return directory / "mod.wind"


def sync_mod_wind(root: Path, path: Path) -> None:
    """Keep the module declaration chain of *path* in sync (todo-158).

    Writing ``libs/a/b/util.wind`` ensures ``libs/a/b/mod.wind`` declares
    ``pub mod util;``, ``libs/a/mod.wind`` declares ``pub mod b;`` and
    ``libs/mod.wind`` declares ``pub mod a;`` — a submodule is only
    addressable where its parent module declares it.  A Breeze source root
    declares through its ``lib.wd`` instead of a mod.wind.  Module roots
    (``libs``/``src``) terminate the chain: nothing declares them.
    Existing module-file content is preserved; missing declarations
    appended.
    """
    path = Path(path).resolve()
    root = Path(root).resolve()
    directory = path.parent
    while True:
        if path.name.lower().startswith("mod."):
            # mod.wind files declare their *directory*, not themselves.
            name = None
        elif path.name.lower().startswith("lib."):
            # The crate-root file (lib.wd) is the root module itself.
            name = None
        else:
            name = path.stem
        mod_wind = _module_file_of(directory)
        if name is not None and name not in _declared_names(mod_wind):
            existing = (
                mod_wind.read_text(encoding="utf-8-sig")
                if mod_wind.is_file()
                else ""
            )
            if existing and not existing.endswith("\n"):
                existing += "\n"
            mod_wind.write_text(
                existing + f"pub mod {name};\n",
                encoding="utf-8",
                newline="\n",
            )
        if directory == root or directory.name in ("libs", "src"):
            break
        # Walk up: this directory is itself a module; its parent's
        # module file must declare it (directories double as the next
        # path).
        path = directory
        directory = directory.parent


def write_module(root: Path, relative: str | Path, text: str) -> Path:
    """Write a module source file and sync its mod.wind chain."""
    path = Path(root) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    sync_mod_wind(root, path)
    return path


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
