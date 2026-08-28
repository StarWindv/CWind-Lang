"""todo-119: ``crate`` / ``super`` / ``self`` import heads and ``pub(std)``.

``use crate::a::b;`` anchors at the module-tree root (an explicit spelling
of the default bare path); ``use self::x;`` anchors at the importing file's
own module path; ``use super::x;`` anchors at its parent module.  The
keywords may only start a path, and ``self`` / ``super`` need the file to
live inside a module tree (``libs/`` or a Breeze source root).

``pub(std)`` marks an item public inside its own module root tree only:
importers living under a different root (or outside every root) see it as
private, and ``pub use`` facades cannot launder it past that boundary.

Each case lives in ``cases/todo119/<case>/`` as a full project tree
(``libs/`` modules, optional ``Breeze.toml`` + ``src/``, ``main.wind``
entry and ``expect.json``; ``expect.json`` may name the entry via an
``entry`` key relative to the case root).
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

from cwind_frontend import run_sa_with_errors, tokenize, tokenize_file  # noqa: E402
from cwind_frontend.parser.parser import parse_with_errors  # noqa: E402

CASES = TESTS / "cases" / "todo119"


class Todo119CaseTests(harness.CaseAssertionsMixin):
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


class Todo119UnitTests(harness.CaseAssertionsMixin):
    def test_pub_std_parses_without_source_path(self) -> None:
        # Untagged (stdin / in-memory) sources keep the legacy permissive
        # behavior: pub(std) parses and the item stays usable bare.
        text = (
            "pub(std) fn helper() -> Int { return 1; }\n"
            "fn main() -> Int { print(helper()); return 0; }\n"
        )
        parsed = parse_with_errors(tokenize(text))
        self.assertEqual([str(e) for e in parsed.errors], [])
        sa = run_sa_with_errors(parsed.program)
        self.assertEqual([str(e) for e in sa.errors], [])

    def test_head_keywords_work_with_explicit_target(self) -> None:
        # cfg-gated head-keyword imports evaluate like any other use.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "libs").mkdir()
            (root / "libs" / "geom").mkdir()
            (root / "libs" / "geom" / "mod.wind").write_text(
                "#[cfg(unix)]\nuse self::ghost;\npub fn v() -> Int { return 2; }\n",
                encoding="utf-8",
            )
            entry = root / "main.wind"
            entry.write_text(
                "use geom;\n\nfn main() -> Int { print(geom::v()); return 0; }\n",
                encoding="utf-8",
            )
            parsed = parse_with_errors(
                tokenize_file(entry),
                source_path=str(entry.resolve()),
                target_os="windows",
            )
            self.assertEqual([str(e) for e in parsed.errors], [])


if __name__ == "__main__":
    unittest.main()
