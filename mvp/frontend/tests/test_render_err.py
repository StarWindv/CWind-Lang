"""Tests for cwind_frontend.render.errors (tgqe-driven rendering).

Rendering input sources live in ``cases/render_err``; the rendering
assertions themselves stay in this module.  Diagnostics render through
the tgqe error bus (publisher-tagged reports, see .handover todo list).
"""

import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

import harness

from cwind_frontend import (
    LexError,
    offset_for_position,
    render_error,
    render_warning,
    tokenize,
)

RE = "render_err"


def lex_error(source):
    try:
        tokenize(source)
    except LexError as exc:
        return exc
    raise AssertionError("expected LexError")


def case_source(name):
    return harness.source(RE, name)


class TestOffsetForPosition(unittest.TestCase):
    def test_offsets(self):
        src = "abc\ndef\n"
        self.assertEqual(offset_for_position(src, 1, 1), 0)
        self.assertEqual(offset_for_position(src, 2, 1), 4)
        self.assertEqual(offset_for_position(src, 2, 4), 7)
        self.assertEqual(offset_for_position(src, 99, 1), 8)

    def test_empty_source(self):
        self.assertEqual(offset_for_position("", 1, 1), 0)
        self.assertEqual(offset_for_position("", 5, 1), 0)


class TestRenderError(unittest.TestCase):
    def test_unterminated_string(self):
        src = case_source("unterminated_string")
        out = render_error(lex_error(src), src, color=False)
        self.assertIn("Error", out)
        self.assertIn("Unterminated string literal", out)
        self.assertIn('let a: String = "oops;', out)

    def test_kind_error_headline(self):
        src = case_source("unterminated_short")
        plain = render_error(lex_error(src), src, color=False)
        # The headline carries the stage's error kind only; the message
        # rides the code span label, out of the headline.
        headline = plain.splitlines()[0]
        self.assertTrue(headline.startswith("Error: Lexical error"))
        self.assertNotIn("String literal reaches end of file", headline)

    def test_label_is_the_specific_message(self):
        src = case_source("incdec_source")
        exc = lex_error(src)
        self.assertEqual(exc.category, "wind has no increment/decrement operator")
        plain = render_error(exc, src, color=False)
        # Kind-only headline; the specific message stays on the label.
        headline = plain.splitlines()[0]
        self.assertTrue(headline.startswith("Error: Lexical error"))
        self.assertNotIn("'++' is not a valid postfix operator", headline)
        self.assertIn("'++' is not a valid postfix operator", plain)

    def test_named_source(self):
        src = case_source("unterminated_short")
        out = render_error(lex_error(src), src, source_name="main.cw", color=False)
        self.assertIn("main.cw", out)
        self.assertIn("main.cw:1:17", out)

    def test_no_color(self):
        src = case_source("unexpected_char_source")
        out = render_error(lex_error(src), src, color=False)
        self.assertNotIn("\x1b[", out)
        self.assertIn("Unexpected character", out)

    def test_color_output_has_ansi(self):
        src = case_source("unexpected_char_source")
        out = render_error(lex_error(src), src, color=True)
        self.assertIn("\x1b[", out)

    def test_publisher_note(self):
        src = case_source("unterminated_short")
        out = render_error(lex_error(src), src, color=False)
        self.assertIn("Publisher: lexer", out)

    def test_custom_publisher(self):
        src = case_source("unterminated_short")
        out = render_error(lex_error(src), src, color=False, publisher="parser")
        self.assertIn("Publisher: parser", out)

    def test_crlf_alignment(self):
        src = case_source("crlf_source")
        exc = lex_error(src)
        self.assertEqual((exc.line, exc.column), (2, 17))
        out = render_error(exc, src, color=False)
        self.assertIn('let b: String = "x;', out)

    def test_empty_source(self):
        out = render_error(LexError("unexpected character '~'", 1, 1), "", color=False)
        self.assertIn("Error", out)
        self.assertIn("Unexpected character '~'", out)

    def test_context_lines(self):
        # tgqe's renderer shows the error span only (no extra context
        # lines); the offending line is present, sibling lines are not.
        src = case_source("context_lines_source")
        plain = render_error(lex_error(src), src, color=False)
        self.assertIn("let c: Int = 3~;", plain)
        self.assertNotIn("let a: Int = 1;", plain)
        self.assertNotIn("let e: Int = 5;", plain)

    def test_render_warning(self):
        out = render_warning(LexError("unknown escape", 1, 1), "x", color=False)
        self.assertIn("Warning", out)
        self.assertIn("Unknown escape", out)


if __name__ == "__main__":
    unittest.main()
