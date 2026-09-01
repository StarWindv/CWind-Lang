"""Tests for cwind_frontend.lexer (串联脚本).

The test corpus lives in ``cases/lexer``: one ``<name>.wind`` source file
per case plus a ``<name>.json`` sidecar when the expected value is a plain
pipeline outcome (see harness.py for the schema).  This module feeds the
sources through the lexer API and asserts the expectations.

Run from the repo root:
    .venv/Scripts/python.exe -m unittest discover -s mvp/frontend/tests -v
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

import harness

from cwind_frontend import (
    KEYWORD_KINDS,
    LexError,
    Lexer,
    TokenKind,
    lex_with_errors,
    stream_tokens,
    tokenize,
    tokenize_file,
    tokens_to_json,
)

LEX = "lexer"


def kinds(source, **kwargs):
    return [(t.kind, t.value) for t in tokenize(source, **kwargs)]


def kind_names(source, **kwargs):
    return [t.kind.name for t in tokenize(source, **kwargs)]


class TestBasics(unittest.TestCase):
    def test_empty(self):
        for name in ("empty", "empty_whitespace"):
            self.assertEqual(tokenize(harness.source(LEX, name)), [])

    def test_bom_stripped(self):
        toks = tokenize(harness.source(LEX, "bom_stripped"))
        self.assertEqual(toks[0].kind, TokenKind.LET)
        self.assertEqual(toks[0].value, "let")

    def test_crlf(self):
        toks = tokenize(harness.source(LEX, "crlf"))
        self.assertEqual(toks[-1].kind, TokenKind.SEMICOLON)
        self.assertEqual(toks[-1].line, 2)

    def test_positions(self):
        toks = tokenize(harness.source(LEX, "positions"))
        self.assertEqual(
            [(t.line, t.column) for t in toks],
            [(1, 1), (1, 5), (1, 6), (1, 8), (1, 12), (1, 14), (1, 16)],
        )


class TestKeywords(unittest.TestCase):
    _data = json.loads(
        (harness.CASES_DIR / LEX / "keywords.json").read_bytes().decode("utf-8")
    )

    def test_keywords(self):
        for kw in self._data["keywords"] + self._data["reserved"]:
            toks = tokenize(kw)
            self.assertEqual(len(toks), 1, kw)
            self.assertEqual(toks[0].kind, getattr(TokenKind, kw.upper()), kw)
            self.assertEqual(toks[0].value, kw)
            self.assertIn(toks[0].kind, KEYWORD_KINDS, kw)

    def test_types_and_self_are_identifiers(self):
        text = harness.source(LEX, "types_and_self_are_identifiers")
        self.assertTrue(all(t.kind == TokenKind.IDENTIFIER for t in tokenize(text)))

    def test_keyword_prefix_is_identifier(self):
        lines = harness.source(LEX, "keyword_prefixes").splitlines()
        self.assertEqual(kinds(lines[0]), [(TokenKind.IDENTIFIER, "structs")])
        self.assertEqual(kinds(lines[1]), [(TokenKind.IDENTIFIER, "let_x")])
        self.assertEqual(kinds(lines[2]), [(TokenKind.IDENTIFIER, "if_")])


class TestIdentifiersAndNumbers(unittest.TestCase):
    def test_identifiers(self):
        toks = tokenize(harness.source(LEX, "identifiers"))
        self.assertEqual([t.value for t in toks], ["hello", "_private", "uid_counter", "x1", "data"])
        self.assertTrue(all(t.kind == TokenKind.IDENTIFIER for t in toks))

    def test_integers(self):
        toks = tokenize(harness.source(LEX, "integers"))
        self.assertEqual([t.value for t in toks], [0, 42, 7])
        self.assertEqual([t.raw for t in toks], ["0", "42", "007"])

    def test_floats(self):
        toks = tokenize(harness.source(LEX, "floats"))
        self.assertEqual([t.value for t in toks], [3.14, 1.0, 0.5])
        self.assertTrue(all(t.kind == TokenKind.FLOAT for t in toks))

    def test_dot_after_number_is_not_float(self):
        toks = tokenize(harness.source(LEX, "dot_number_split"))
        self.assertEqual(
            [(t.kind, t.raw) for t in toks],
            [(TokenKind.INTEGER, "1"), (TokenKind.DOT, ".")],
        )
        toks = tokenize(harness.source(LEX, "dot_number_chain"))
        self.assertEqual(
            [(t.kind, t.raw) for t in toks],
            [(TokenKind.FLOAT, "1.2"), (TokenKind.DOT, "."), (TokenKind.INTEGER, "3")],
        )


class TestStrings(harness.CaseAssertionsMixin):
    def test_basic_strings(self):
        toks = tokenize(harness.source(LEX, "basic_strings"))
        self.assertEqual([t.value for t in toks], ["hello", "world"])
        self.assertEqual([t.raw for t in toks], ['"hello"', "'world'"])
        self.assertTrue(all(t.kind == TokenKind.STRING for t in toks))

    def test_escapes(self):
        toks = tokenize(harness.source(LEX, "escapes"))
        self.assertEqual(toks[0].value, "a\nb\rc\td\\e\"f'g\0h\bi\vj")

    def test_quoted_quote(self):
        toks = tokenize(harness.source(LEX, "quoted_quote"))
        self.assertEqual(toks[0].value, "it's")

    def test_braces_in_strings(self):
        # `{` is literal by default; \{ and \} escape literal braces.
        self.assertEqual(tokenize(harness.source(LEX, "braces_literal"))[0].value, "a{b}c")
        self.assertEqual(tokenize(harness.source(LEX, "braces_escaped"))[0].value, "a{b}c")

    def test_unknown_escape_kept_verbatim(self):
        self.assertEqual(tokenize(harness.source(LEX, "unknown_escape_verbatim"))[0].value, "\\q")

    def test_multiline_string_indentation_counts(self):
        src = harness.source(LEX, "multiline_indentation")
        toks = tokenize(src)
        self.assertEqual(len(toks), 1)
        self.assertEqual(toks[0].value, "a    b")
        self.assertEqual(toks[0].raw, '"a\\\n    b"')
        self.assertEqual((toks[0].line, toks[0].end_line), (1, 2))

    def test_multiline_string_grammar_style(self):
        src = harness.source(LEX, "multiline_grammar_style")
        toks = tokenize(src)
        self.assertEqual(
            kind_names(src),
            [
                "RETURN",
                "STRING",
                "DOT",
                "IDENTIFIER",
                "LPAREN",
                "RPAREN",
                "SEMICOLON",
            ],
        )
        self.assertEqual(toks[1].value, "Name : {self.name}\nAge  : {self.age}\n")

    def test_unterminated_string(self):
        with self.assertRaises(LexError) as cm:
            tokenize(harness.source(LEX, "unterminated_string"))
        self.assertEqual((cm.exception.line, cm.exception.column), (1, 17))

    def test_unterminated_string_after_continuation(self):
        with self.assertRaises(LexError):
            tokenize(harness.source(LEX, "string_continuation_unterminated_a"))
        with self.assertRaises(LexError):
            tokenize(harness.source(LEX, "string_continuation_unterminated_b"))

    def test_unterminated_reported_at_eof(self):
        lexer = Lexer()
        self.assertEqual(lexer.feed_line(harness.source(LEX, "string_open_no_close")), [])
        lexer.eof()
        self.assertEqual(len(lexer.errors), 1)
        self.assertEqual((lexer.errors[0].line, lexer.errors[0].column), (1, 1))

    def test_lex_with_errors_collects_all(self):
        result = lex_with_errors(harness.source(LEX, "collects_all_errors"))
        self.assertEqual(len(result.errors), 2)
        self.assertEqual(result.errors[0].category, "unexpected character '~'")
        self.assertIn("'~' is not valid", result.errors[0].message)
        # recovery still produced the surrounding tokens
        kinds_ = [t.kind for t in result.tokens]
        self.assertIn(TokenKind.LET, kinds_)
        self.assertIn(TokenKind.SEMICOLON, kinds_)

    def test_double_plus_minus_rejected(self):
        for name in (
            "incdec_pp_num", "incdec_mm_num", "incdec_pp_ident", "incdec_mm_ident",
        ):
            with self.subTest(case=name):
                self.assert_case(LEX, name)
        # spaced `+ +` / `- -` remain ordinary operator sequences
        self.assertEqual(lex_with_errors(harness.source(LEX, "spaced_plus")).errors, [])
        self.assertEqual(lex_with_errors(harness.source(LEX, "spaced_minus")).errors, [])

    def test_unknown_escape_warns_verbatim(self):
        result = lex_with_errors(harness.source(LEX, "unknown_escape_warns"))
        self.assertEqual(result.errors, [])
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("unknown escape sequence", result.warnings[0].message)
        self.assertEqual(result.tokens[0].value, "\\q")

    def test_multiline_string_raw_newline_kept(self):
        src = harness.source(LEX, "multiline_raw_newline")
        toks = tokenize(src)
        self.assertEqual(len(toks), 1)
        self.assertEqual(toks[0].value, "first line\n    second line")
        self.assertEqual((toks[0].line, toks[0].end_line), (1, 2))

    def test_multiline_string_rust_style(self):
        # raw newlines are content (like Rust): the leading/trailing newlines
        # around the text stay in the value.
        a = harness.source(LEX, "rust_style_raw")
        self.assertEqual(tokenize(a)[0].value, "\n Hello, World!\n")

        # backslash-newline escapes the physical newline, so the same text
        # written with escaped line breaks contains no newlines at all.
        b = harness.source(LEX, "rust_style_continuation")
        self.assertEqual(tokenize(b)[0].value, " Hello, Wind!")


class TestComments(unittest.TestCase):
    def test_line_comment_skipped(self):
        self.assertEqual(
            kind_names(harness.source(LEX, "line_comment_skipped")),
            [
                "LET", "IDENTIFIER", "COLON",
                "IDENTIFIER", "ASSIGN", "INTEGER",
                "SEMICOLON",
            ],
        )

    def test_block_comment_skipped(self):
        self.assertEqual(
            kind_names(harness.source(LEX, "block_comment_inline"))[-2:],
            ["INTEGER", "SEMICOLON"],
        )
        toks = tokenize(harness.source(LEX, "block_comment_multiline"))
        self.assertEqual(toks[0].kind, TokenKind.LET)

    def test_comments_emitted(self):
        toks = tokenize(harness.source(LEX, "comment_emitted_line"), emit_comments=True)
        self.assertEqual(toks[0].kind, TokenKind.COMMENT)
        self.assertEqual(toks[0].value, " hi")
        self.assertEqual(toks[0].raw, "// hi")

        toks = tokenize(harness.source(LEX, "comment_emitted_block"), emit_comments=True)
        self.assertEqual(toks[0].value, " a\nb ")
        self.assertEqual(toks[0].raw, "/* a\nb */")
        self.assertEqual((toks[0].line, toks[0].end_line), (1, 2))

    def test_unterminated_block_comment(self):
        with self.assertRaises(LexError) as cm:
            tokenize(harness.source(LEX, "unterminated_block_comment"))
        self.assertEqual((cm.exception.line, cm.exception.column), (2, 1))

    def test_comment_markers_inside_string(self):
        src = harness.source(LEX, "comment_markers_in_string")
        self.assertEqual(
            kinds(src),
            [
                (TokenKind.LET, "let"),
                (TokenKind.IDENTIFIER, "s"),
                (TokenKind.COLON, ":"),
                (TokenKind.IDENTIFIER, "String"),
                (TokenKind.ASSIGN, "="),
                (TokenKind.STRING, "http://x /* not a comment */"),
                (TokenKind.SEMICOLON, ";"),
            ],
        )


class TestOperators(unittest.TestCase):
    _ops = json.loads(
        (harness.CASES_DIR / LEX / "operators.json").read_bytes().decode("utf-8")
    )["ops"]

    def test_every_operator(self):
        for op, kind_name in self._ops:
            with self.subTest(op=op):
                toks = tokenize(op)
                self.assertEqual(len(toks), 1, op)
                self.assertIs(toks[0].kind, TokenKind[kind_name], op)
                self.assertEqual(toks[0].raw, op, op)

    def test_maximal_munch(self):
        src = harness.source(LEX, "munch_order")
        self.assertEqual(
            kind_names(src),
            [
                "ADDR_EQ", "EQ", "ASSIGN",
                "PATH", "COLON", "UNPACK", "DOT",
                "ARROW", "MINUS", "LT", "COLON",
                "GE", "LE",
                "NE", "NOT", "SHL", "LT",
                "SHR", "GT", "AND", "AMP",
                "OR", "PIPE", "PLUS_ASSIGN", "PLUS",
            ],
        )

    def test_unexpected_character(self):
        chars = json.loads(
            (harness.CASES_DIR / LEX / "unexpected_chars.json").read_bytes()
        )["chars"]
        for ch in chars:
            with self.assertRaises(LexError):
                tokenize(f"let a: Int = 1{ch};")


class TestLexErrorPositions(harness.CaseAssertionsMixin):
    def test_unterminated_string_end(self):
        with self.assertRaises(LexError) as cm:
            tokenize(harness.source(LEX, "string_open_no_close"))
        self.assertEqual((cm.exception.line, cm.exception.column), (1, 1))
        self.assertEqual((cm.exception.end_line, cm.exception.end_column), (1, 5))

    def test_unexpected_char_end(self):
        self.assert_case(LEX, "unexpected_char_end")


class TestStreaming(unittest.TestCase):
    def test_state_across_lines(self):
        lines = harness.source(LEX, "streaming_state_across_lines").splitlines()
        self.assertEqual(len(lines), 5)
        lexer = Lexer()
        out = []
        for line in lines:
            out.extend(lexer.feed_line(line))
        out.extend(lexer.eof())

        str_tok = next(t for t in out if t.kind == TokenKind.STRING)
        self.assertEqual(str_tok.value, "x  y")
        self.assertEqual(str_tok.raw, '"x\\\n  y"')
        self.assertEqual((str_tok.line, str_tok.column), (1, 1))
        self.assertEqual(str_tok.end_line, 2)

        self.assertEqual(out[-1].kind, TokenKind.SEMICOLON)
        self.assertEqual(out[-1].line, 5)

    def test_tokenize_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "main.cw")
            with open(path, "w", encoding="utf-8", newline="\r\n") as fh:
                fh.write('let a: Int = 1; // hi\nlet b: String = "x";\n')
            toks = tokenize_file(path)
            self.assertEqual(toks[-1].kind, TokenKind.SEMICOLON)
            self.assertEqual(toks[-1].line, 2)


class TestSerialization(unittest.TestCase):
    def test_to_dict(self):
        tok = tokenize(harness.source(LEX, "to_dict_token"))[0]
        d = tok.to_dict()
        self.assertEqual(d["kind"], "string")
        self.assertEqual(d["value"], "hi")
        self.assertEqual(d["raw"], '"hi"')
        self.assertEqual(d["line"], 1)
        self.assertEqual(d["column"], 1)

    def test_tokens_to_json_roundtrip(self):
        toks = tokenize(harness.source(LEX, "tokens_json_roundtrip"))
        data = json.loads(tokens_to_json(toks))
        self.assertEqual(data[0]["kind"], "LET")
        self.assertEqual(data[0]["value"], "let")
        self.assertEqual(data[5]["kind"], "integer")
        self.assertEqual(data[5]["value"], 1)
        self.assertEqual(len(data), len(toks))

    def test_stream_tokens(self):
        toks = list(stream_tokens(harness.source(LEX, "stream_tokens_lines").splitlines()))
        self.assertEqual(
            [t.kind for t in toks][:3],
            [TokenKind.LET, TokenKind.IDENTIFIER, TokenKind.COLON],
        )
        self.assertEqual(toks[5].kind, TokenKind.STRING)
        self.assertEqual(toks[5].value, "xy")


class TestGrammarExample(unittest.TestCase):
    def test_full_example_tokenizes(self):
        toks = tokenize(harness.source(LEX, "grammar_example"))

        # The multi-line str() string.
        str_values = [t.value for t in toks if t.kind == TokenKind.STRING]
        self.assertIn(
            "Name : {self.name}\nAge  : {self.age}\nUID  : {self.uid}\n"
            "Email: {self.email}\n",
            str_values,
        )

        # builtins::output(input);
        idx = next(
            i for i, t in enumerate(toks)
            if t.kind == TokenKind.IDENTIFIER and t.value == "builtins"
        )
        self.assertEqual(toks[idx + 1].kind, TokenKind.PATH)
        self.assertEqual(toks[idx + 2].value, "output")

        # which ::new
        idx = next(
            i for i, t in enumerate(toks)
            if t.kind == TokenKind.WHICH
        )
        self.assertEqual(toks[idx + 1].kind, TokenKind.PATH)
        self.assertEqual(toks[idx + 2].value, "new")

        # Interpolated-looking content inside strings stays literal.
        self.assertIn('"{kv.key}": "{kv.value}"', str_values)

        # The regex string keeps the escaped backslash.
        self.assertIn(r"@[a-zA-Z]+\.[a-zA-Z]+", str_values)


class TestExamFiles(unittest.TestCase):
    def test_multi_line_string_and_enum_smoke(self):
        src = harness.source(LEX, "exam_smoke")
        toks = tokenize(src)
        vals = [(t.kind, t.value) for t in toks]
        self.assertIn((TokenKind.IDENTIFIER, "Green"), vals)
        self.assertIn((TokenKind.INTEGER, 2), vals)
        str_values = [t.value for t in toks if t.kind == TokenKind.STRING]
        self.assertIn("\nfirst line\n    indented second line", str_values)

    def test_for_in_lexes_as_keyword(self):
        # todo-107: `in` is a hard keyword (for-in helper + pub(in path)).
        src = harness.source(LEX, "for_in_identifier")
        toks = tokenize(src)
        self.assertEqual(
            len([t for t in toks if t.kind == TokenKind.IN and t.value == "in"]),
            1,
        )


if __name__ == "__main__":
    unittest.main()
