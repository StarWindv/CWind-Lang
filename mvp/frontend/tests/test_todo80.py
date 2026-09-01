"""todo-80: module-qualified calls use the callee's full signature.

The original gap was asymmetric checking: a bare ``add(1, "x")`` was
rejected while ``mathx::add(1, "x")`` reached the backend.  These tests
also cover return-position contracts, generic substitution, visibility,
and the Rust-style coercion from a diverging (never) function.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent.parent.parent.parent
for path in (ROOT / "mvp/frontend/src", ROOT / "mvp/frontend/tests"):
    sys.path.insert(0, str(path))

import harness  # noqa: E402,F401  (sys.path side effect)
from cwind_frontend import run_sa_with_errors  # noqa: E402
from cwind_frontend.parser.parser import parse_with_errors  # noqa: E402
from cwind_frontend import tokenize_file  # noqa: E402


class Todo80QualifiedCallTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, relative: str, text: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        parts = Path(relative).parts
        if parts and parts[0] in ("libs", "src") and path.suffix in (".wind", ".wd"):
            harness.sync_mod_wind(self.root, path)
        return path
    def parse_main(
        self,
        main: str,
        module: str = (
            "pub fn add(a: Int, b: Int) -> Int { return a + b; }\n"
            "pub fn make() -> &String { return &\"value\"; }\n"
            "pub fn pick<T>(v: T) -> T { return v; }\n"
            "pub fn stop(msg: String) -> ! { exit(0); }\n"
        ),
    ):
        self.write(
            "libs/mathx.wind", module,
        )
        entry = self.write("main.wind", main)
        return parse_with_errors(
            tokenize_file(entry), source_path=str(entry.resolve())
        )

    def errors(self, parsed) -> list[str]:
        result = run_sa_with_errors(parsed.program)
        return [error.message for error in result.errors]

    def assert_clean(self, parsed) -> None:
        self.assertEqual([], [e.message for e in parsed.errors])

    def test_qualified_argument_count_and_type_are_checked(self):
        parsed = self.parse_main(
            "use mathx;\n"
            "fn main() -> Int {\n"
            "    let a: Int = mathx::add(1, \"wrong\");\n"
            "    let b: Int = mathx::add(1);\n"
            "    return mathx::add(a, b);\n"
            "}\n"
        )
        self.assert_clean(parsed)
        errors = "\n".join(self.errors(parsed))
        self.assertIn('must be Int, got String', errors)
        self.assertIn("expects 2 argument(s), got 1", errors)

    def test_qualified_return_type_is_checked_against_binding(self):
        parsed = self.parse_main(
            "use mathx;\n"
            "fn main() -> Int {\n"
            "    let text: String = mathx::make();\n"
            "    return text.len();\n"
            "}\n"
        )
        self.assert_clean(parsed)
        self.assertTrue(any(
            "return type mismatch" in message and "&String" in message
            for message in self.errors(parsed)
        ))

    def test_generic_qualified_call_is_still_inferred(self):
        parsed = self.parse_main(
            "use mathx;\n"
            "fn main() -> Int {\n"
            "    let n: Int = mathx::pick(7);\n"
            "    let s: String = mathx::pick(\"ok\");\n"
            "    return n;\n"
            "}\n"
        )
        self.assert_clean(parsed)
        self.assertEqual([], self.errors(parsed))

    def test_diverging_qualified_call_fits_any_binding(self):
        parsed = self.parse_main(
            "use mathx;\n"
            "fn main() -> Int {\n"
            "    if (true) { mathx::stop(\"done\"); }\n"
            "    return 0;\n"
            "    }\n"
        )
        self.assert_clean(parsed)
        self.assertEqual([], self.errors(parsed))

    def test_private_qualified_member_is_rejected_before_signature(self):
        parsed = self.parse_main(
            "use mathx;\n"
            "fn main() -> Int { return mathx::secret(1); }\n",
            module="fn secret(n: WrongType) -> WrongType { return n; }\n",
        )
        self.assert_clean(parsed)
        self.assertTrue(any(
            "private in module 'mathx'" in message
            for message in self.errors(parsed)
        ))


if __name__ == "__main__":
    unittest.main()
