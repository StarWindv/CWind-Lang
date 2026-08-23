"""Tests for cwind_frontend.render_err (ariadne_py-based rendering).

Rendering input sources live in ``cases/render_err``; the rendering
assertions themselves stay in this module.
"""

import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

import harness

from ariadne_py import Color, Source

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
        src = Source("abc\ndef\n")
        self.assertEqual(offset_for_position(src, 1, 1), 0)
        self.assertEqual(offset_for_position(src, 2, 1), 4)
        self.assertEqual(offset_for_position(src, 2, 4), 7)
        self.assertEqual(offset_for_position(src, 99, 1), 8)


class TestRenderError(unittest.TestCase):
    def test_unterminated_string(self):
        src = case_source("unterminated_string")
        out = render_error(lex_error(src), src)
        self.assertIn("Error", out)
        self.assertIn("Unterminated string literal", out)
        plain = render_error(lex_error(src), src, color=False)
        self.assertIn('let a: String = "oops;', plain)

    def test_message_colored_cyan(self):
        src = case_source("unterminated_short")
        out = render_error(lex_error(src), src)
        # Only the label message (after the arrow) is cyan...
        self.assertEqual(out.count("\x1b[36m"), 1)
        self.assertIn("\x1b[36mString literal reaches end of file\x1b[0m", out)
        # ...the header message stays plain next to the red "Error".
        self.assertIn("\x1b[31mError\x1b[0m: Unterminated string literal", out)

    def test_custom_message_color(self):
        src = case_source("unterminated_short")
        out = render_error(lex_error(src), src, message_color=Color.Red)
        # red "Error" header + red label message
        self.assertEqual(out.count("\x1b[31m"), 2)
        self.assertIn("\x1b[31mString literal reaches end of file\x1b[0m", out)

    def test_category_headline_and_message_label(self):
        src = case_source("incdec_source")
        exc = lex_error(src)
        self.assertEqual(exc.category, "wind has no increment/decrement operator")
        plain = render_error(exc, src, color=False)
        self.assertIn("Error: Wind has no increment/decrement operator", plain)
        self.assertIn("'++' is not a valid postfix operator", plain)

    def test_named_source(self):
        src = case_source("unterminated_short")
        out = render_error(lex_error(src), src, source_name="main.cw")
        self.assertIn("main.cw", out)
        self.assertIn("main.cw:1:17", out)

    def test_no_color(self):
        src = case_source("unexpected_char_source")
        out = render_error(lex_error(src), src, color=False)
        self.assertNotIn("\x1b[", out)
        self.assertIn("Unexpected character", out)

    def test_crlf_alignment(self):
        src = case_source("crlf_source")
        exc = lex_error(src)
        self.assertEqual((exc.line, exc.column), (2, 17))
        out = render_error(exc, src, color=False)
        self.assertIn('let b: String = "x;', out)

    def test_empty_source(self):
        out = render_error(LexError("unexpected character '~'", 1, 1), "", color=False)
        self.assertIn("Error", out)
        self.assertIn("Unexpected character", out)

    def test_context_lines(self):
        src = case_source("context_lines_source")
        plain = render_error(lex_error(src), src, color=False)
        self.assertIn("let a: Int = 1;", plain)
        self.assertNotIn("let e: Int = 5;", plain)  # context is above only

    def test_render_warning(self):
        out = render_warning(LexError("unknown escape", 1, 1), "x", color=False)
        self.assertIn("Warning", out)
        self.assertIn("Unknown escape", out)


if __name__ == "__main__":
    unittest.main()
