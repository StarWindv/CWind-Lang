"""todo-126: ``[pub] export crate <name> [as <alias>];``.

The data-driven project-tree cases live in ``cases/todo126/`` and are swept
by ``test_cases.py``; the "why" is recorded in ``cases/README.md``.  This
module keeps the two checks that are not file-per-case: the import manifest
must flag ``crate_export`` (distinguishing ``export crate foo;`` from
``use foo;``), and cfg must be evaluated before resolution so a gated-away
export never fails to resolve.
"""

from __future__ import annotations

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
