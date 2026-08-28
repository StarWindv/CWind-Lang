"""todo-126: ``[pub] export crate <name> [as <alias>];``.

CWind's take on Rust 2015 ``extern crate``: bind an entire top-level crate
(a ``libs/<name>`` module tree) under one name in the current file.  It is a
*restricted* module import -- the crate name must be a lone identifier, with
no ``::`` path, item group or ``*`` wildcard -- and it reuses the ``use``
machinery, so ``export crate foo;`` behaves exactly like ``use foo;`` while
carrying a ``crate_export`` provenance flag in the import manifest.

``export`` and ``crate`` are ordinary identifiers, never keywords: the two
words only read as an extern-crate import when they appear in this exact
order at the top level, so an item genuinely named ``export`` is unaffected.

Each case lives in ``cases/todo126/<case>/`` as a full project tree
(``libs/`` modules, ``main.wind`` entry and ``expect.json``; ``expect.json``
may name the entry via an ``entry`` key relative to the case root).
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

CASES = TESTS / "cases" / "todo126"


class Todo126CaseTests(harness.CaseAssertionsMixin):
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
            entry = root / exp.get("entry", "main.wind")
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


class Todo126UnitTests(harness.CaseAssertionsMixin):
    def test_manifest_marks_crate_export(self) -> None:
        # ``export crate`` and ``use`` resolve to the same module namespace,
        # but the manifest records which spelling bound it (crate_export).
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "libs").mkdir()
            (root / "libs" / "geom.wind").write_text(
                "pub fn v() -> Int { return 2; }\n", encoding="utf-8"
            )
            (root / "libs" / "util.wind").write_text(
                "pub fn w() -> Int { return 3; }\n", encoding="utf-8"
            )
            entry = root / "main.wind"
            entry.write_text(
                "export crate geom;\n"
                "use util;\n"
                "\n"
                "fn main() -> Int { print(geom::v() + util::w()); return 0; }\n",
                encoding="utf-8",
            )
            parsed = parse_with_errors(
                tokenize_file(entry), source_path=str(entry.resolve())
            )
            self.assertEqual([str(e) for e in parsed.errors], [])
            sa = run_sa_with_errors(parsed.program)
            self.assertEqual([str(e) for e in sa.errors], [])
            by_path = {
                tuple(row["path"]): row for row in sa.info.import_manifest
            }
            self.assertTrue(by_path[("geom",)]["crate_export"])
            self.assertFalse(by_path[("util",)]["crate_export"])

    def test_cfg_gated_export_crate(self) -> None:
        # cfg is evaluated before resolution: a gated-away ``export crate``
        # never resolves, so its (missing) crate raises no error.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "libs").mkdir()
            (root / "libs" / "geom.wind").write_text(
                "pub fn v() -> Int { return 2; }\n", encoding="utf-8"
            )
            entry = root / "main.wind"
            entry.write_text(
                "#[cfg(unix)]\nexport crate nope;\n\nfn main() -> Int { return 0; }\n",
                encoding="utf-8",
            )
            skipped = parse_with_errors(
                tokenize_file(entry),
                source_path=str(entry.resolve()),
                target_os="windows",
            )
            self.assertEqual([str(e) for e in skipped.errors], [])
            active = parse_with_errors(
                tokenize_file(entry),
                source_path=str(entry.resolve()),
                target_os="linux",
            )
            self.assertTrue(
                any("cannot find module 'nope'" in str(e) for e in active.errors),
                [str(e) for e in active.errors],
            )


if __name__ == "__main__":
    unittest.main()
