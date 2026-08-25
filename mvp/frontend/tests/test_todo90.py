"""todo-90: struct field visibility (``pub`` vs private fields).

Private (non-``pub``) fields are accessible only inside the module that
declares the struct — the same file, mirroring Rust's module privacy.
Access sites covered: instance field read/write, ``Struct::static_field``,
positional construction and struct pattern destructuring.

Untagged sources (stdin / in-memory tests without ``source_path``) keep the
legacy permissive behavior, so every existing single-file program compiles
unchanged.
"""

import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

from cwind_frontend import lex_with_errors, parse_with_errors, run_sa_with_errors


def _run_project(main_text: str, lib_name: str, lib_text: str):
    """Parse a main.wind + libs/<lib_name>.wind project with real paths.

    Returns the SA error-message list.  ``source_path`` is passed so the
    parser tags every top-level item with its defining file (the todo-90
    precondition for visibility checks).
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        lib = root / "libs"
        lib.mkdir()
        (lib / f"{lib_name}.wind").write_text(lib_text, encoding="utf-8")
        main = root / "main.wind"
        main.write_text(main_text, encoding="utf-8")
        lexed = lex_with_errors(main.read_text(encoding="utf-8"))
        assert not lexed.errors, lexed.errors
        parsed = parse_with_errors(
            lexed.tokens,
            source_path=str(main),
        )
        assert not parsed.errors, [e.message for e in parsed.errors]
        result = run_sa_with_errors(parsed.program)
        return [e.message for e in result.errors]


# A library struct with one private and one pub field plus a constructor
# and a method that reads the private field internally.
LIB_POINT = (
    "pub struct Point {\n"
    "    x: Int,\n"
    "    pub y: Int,\n"
    "}\n"
    "\n"
    "extra Point {\n"
    "    pub fn new(x: Int, y: Int) -> Self {\n"
    "        return Self { x, y };\n"
    "    }\n"
    "\n"
    "    pub fn sum(self) -> Int {\n"
    "        return self.x + self.y;\n"
    "    }\n"
    "\n"
    "    fn bump(mut self) -> Int {\n"
    "        self.x = self.x + 1;\n"
    "        return self.x;\n"
    "    }\n"
    "}\n"
)

MAIN_USE = "use pointm::*;\n"


class FieldVisibilityTests(unittest.TestCase):
    def _run(self, main_body: str):
        return _run_project(
            MAIN_USE + main_body, "pointm", LIB_POINT
        )

    def test_private_field_read_is_rejected_cross_module(self):
        errors = self._run(
            "fn main() -> Int {\n"
            "    let p: Point = Point::new(1, 2);\n"
            "    return p.y + p.x;\n"
            "}\n"
        )
        self.assertEqual(1, len(errors), errors)
        self.assertIn("field 'x' of struct 'Point' is private", errors[0])

    def test_private_field_write_is_rejected_cross_module(self):
        errors = self._run(
            "fn main() -> Int {\n"
            "    let mut p: Point = Point::new(1, 2);\n"
            "    p.x = 5;\n"
            "    return p.sum();\n"
            "}\n"
        )
        self.assertEqual(1, len(errors), errors)
        self.assertIn("field 'x' of struct 'Point' is private", errors[0])

    def test_pub_field_read_is_allowed_cross_module(self):
        errors = self._run(
            "fn main() -> Int {\n"
            "    let p: Point = Point::new(1, 2);\n"
            "    return p.y;\n"
            "}\n"
        )
        self.assertEqual([], errors)

    def test_construction_with_private_fields_is_rejected_cross_module(self):
        errors = self._run(
            "fn main() -> Int {\n"
            "    let p: Point = Point { 1, 2 };\n"
            "    return p.y;\n"
            "}\n"
        )
        self.assertTrue(any(
            "field 'x' of struct 'Point' is private" in e for e in errors
        ), errors)

    def test_pattern_destructure_of_private_field_is_rejected(self):
        errors = self._run(
            "fn main() -> Int {\n"
            "    let p: Point = Point::new(1, 2);\n"
            "    match (p) { Point { x, y } => { return x + y; } }\n"
            "}\n"
        )
        self.assertTrue(any(
            "field 'x' of struct 'Point' is private" in e for e in errors
        ), errors)

    def test_pattern_wildcard_rest_still_allows_pub_fields(self):
        errors = self._run(
            "fn main() -> Int {\n"
            "    let p: Point = Point::new(1, 2);\n"
            "    match (p) { Point { y, .. } => { return y; } }\n"
            "}\n"
        )
        # Only the private field named in the pattern is rejected; `y` alone
        # must compile cleanly.
        self.assertEqual([], errors)

    def test_methods_inside_defining_module_keep_private_access(self):
        # `sum` reads self.x and `bump` writes it; both live in the lib file.
        errors = self._run(
            "fn main() -> Int {\n"
            "    let p: Point = Point::new(1, 2);\n"
            "    return p.sum();\n"
            "}\n"
        )
        self.assertEqual([], errors)

    def test_untagged_sources_stay_permissive(self):
        # In-memory single-file source: nothing carries a source_module tag,
        # so legacy behavior (no visibility enforcement) holds.
        text = (
            "struct S { a: Int, pub b: Int }\n"
            "fn main() -> Int {\n"
            "    let s: S = S { 1, 2 };\n"
            "    return s.a + s.b;\n"
            "}\n"
        )
        lexed = lex_with_errors(text)
        assert not lexed.errors
        parsed = parse_with_errors(lexed.tokens)
        assert not parsed.errors
        result = run_sa_with_errors(parsed.program)
        self.assertEqual([], result.errors)


class StaticFieldVisibilityTests(unittest.TestCase):
    def _lib(self, field_decl: str) -> str:
        return (
            "pub struct Counter {\n"
            f"    {field_decl}\n"
            "}\n"
        )

    def test_private_static_field_read_is_rejected_cross_module(self):
        errors = _run_project(
            "use counters::*;\n"
            "fn main() -> Int {\n"
            "    return Counter::count;\n"
            "}\n",
            "counters",
            self._lib("static count: Int = 7;"),
        )
        self.assertTrue(any(
            "field 'count' of struct 'Counter' is private" in e
            for e in errors
        ), errors)

    def test_pub_static_field_read_is_allowed_cross_module(self):
        errors = _run_project(
            "use counters::*;\n"
            "fn main() -> Int {\n"
            "    return Counter::count;\n"
            "}\n",
            "counters",
            self._lib("pub static count: Int = 7;"),
        )
        self.assertEqual([], errors)


if __name__ == "__main__":
    unittest.main()
