"""Tests for cwind_frontend.render_err (ariadne_py-based rendering)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ariadne_py import Color, Source

from cwind_frontend import LexError, offset_for_position, render_error, tokenize


def lex_error(source):
    try:
        tokenize(source)
    except LexError as exc:
        return exc
    raise AssertionError("expected LexError")


class TestOffsetForPosition(unittest.TestCase):
    def test_offsets(self):
        src = Source("abc\ndef\n")
        self.assertEqual(offset_for_position(src, 1, 1), 0)
        self.assertEqual(offset_for_position(src, 2, 1), 4)
        self.assertEqual(offset_for_position(src, 2, 4), 7)
        self.assertEqual(offset_for_position(src, 99, 1), 8)


class TestRenderError(unittest.TestCase):
    def test_unterminated_string(self):
        src = 'let a: String = "oops;\nlet b: Int = 2;\n'
        out = render_error(lex_error(src), src)
        self.assertIn("Error", out)
        self.assertIn("unterminated string literal", out)
        plain = render_error(lex_error(src), src, color=False)
        self.assertIn('let a: String = "oops;', plain)

    def test_message_colored_cyan(self):
        src = 'let a: String = "oops;\n'
        out = render_error(lex_error(src), src)
        # Only the label message (after the arrow) is cyan...
        self.assertEqual(out.count("\x1b[36m"), 1)
        self.assertIn("\x1b[36munterminated string literal\x1b[0m", out)
        # ...the header message stays plain next to the red "Error".
        self.assertIn("\x1b[31mError\x1b[0m: unterminated string literal", out)

    def test_custom_message_color(self):
        src = 'let a: String = "oops;\n'
        out = render_error(lex_error(src), src, message_color=Color.Red)
        # red "Error" header + red label message
        self.assertEqual(out.count("\x1b[31m"), 2)
        self.assertIn("\x1b[31munterminated string literal\x1b[0m", out)

    def test_named_source(self):
        src = 'let a: String = "oops;\n'
        out = render_error(lex_error(src), src, source_name="main.cw")
        self.assertIn("main.cw", out)
        self.assertIn("main.cw:1:17", out)

    def test_no_color(self):
        src = "let a: Int = 1~;\n"
        out = render_error(lex_error(src), src, color=False)
        self.assertNotIn("\x1b[", out)
        self.assertIn("unexpected character", out)

    def test_crlf_alignment(self):
        src = 'let a: Int = 1;\r\nlet b: String = "x;\r\n'
        exc = lex_error(src)
        self.assertEqual((exc.line, exc.column), (2, 17))
        out = render_error(exc, src, color=False)
        self.assertIn('let b: String = "x;', out)

    def test_empty_source(self):
        out = render_error(LexError("unexpected character '~'", 1, 1), "", color=False)
        self.assertIn("Error", out)
        self.assertIn("unexpected character", out)


if __name__ == "__main__":
    unittest.main()
