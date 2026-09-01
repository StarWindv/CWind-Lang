"""todo-119: ``crate``/``super``/``self`` import heads + ``pub(std)``.

The data-driven project-tree cases live in ``cases/todo119/`` and are swept
by ``test_cases.py``; the "why" is recorded in ``cases/README.md``.  This
module keeps only the two regressions that do not fit the file-per-case
shape: ``pub(std)`` without a ``source_path`` stays permissive, and cfg-gated
head-keyword imports evaluate against an explicitly pinned target.
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

from cwind_frontend import (  # noqa: E402
    run_sa_with_errors,
    tokenize,
    tokenize_file,
)
from cwind_frontend.parser.parser import parse_with_errors  # noqa: E402


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
            (root / "libs" / "mod.wind").write_text(
                "pub mod geom;\n", encoding="utf-8"
            )
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
