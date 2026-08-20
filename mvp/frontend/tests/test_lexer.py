"""Unit tests for cwind_frontend.lexer.

Run from the repo root:
    .venv/Scripts/python.exe -m unittest discover -s frontend/tests -v
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

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


def kinds(source, **kwargs):
    return [(t.kind, t.value) for t in tokenize(source, **kwargs)]


class TestBasics(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(tokenize(""), [])
        self.assertEqual(tokenize("   \n\t  \n"), [])

    def test_bom_stripped(self):
        toks = tokenize("\ufefflet a: Int = 1;")
        self.assertEqual(toks[0].kind, TokenKind.LET)
        self.assertEqual(toks[0].value, "let")

    def test_crlf(self):
        toks = tokenize("let a: Int = 1;\r\nlet b: Float = 2.5;\r\n")
        self.assertEqual(toks[-1].kind, TokenKind.SEMICOLON)
        self.assertEqual(toks[-1].line, 2)

    def test_positions(self):
        toks = tokenize("let a: Int = 42;")
        self.assertEqual(
            [(t.line, t.column) for t in toks],
            [(1, 1), (1, 5), (1, 6), (1, 8), (1, 12), (1, 14), (1, 16)],
        )


class TestKeywords(unittest.TestCase):
    KEYWORDS = [
        "struct", "enum", "extra", "impl", "trait", "const",
        "static", "which", "where", "type", "typedef", "group", "let", "fn",
        "pub", "return", "break", "continue", "for", "while", "if", "elif", "else",
    ]
    RESERVED = [
        "lambda", "import", "use", "as", "when",
        "define", "async", "await",
    ]

    def test_keywords(self):
        for kw in self.KEYWORDS + self.RESERVED:
            toks = tokenize(kw)
            self.assertEqual(len(toks), 1, kw)
            self.assertEqual(toks[0].kind, getattr(TokenKind, kw.upper()), kw)
            self.assertEqual(toks[0].value, kw)
            self.assertIn(toks[0].kind, KEYWORD_KINDS, kw)

    def test_types_and_self_are_identifiers(self):
        text = " ".join([
            "Int", "i16", "Int8", "UInt", "u16", "UInt8", "Float", "f32",
            "String", "Bool", "Byte", "Instance", "None", "Tuple", "Vector",
            "Map", "Set", "Self", "self",
        ])
        self.assertTrue(all(t.kind == TokenKind.IDENTIFIER for t in tokenize(text)))

    def test_keyword_prefix_is_identifier(self):
        self.assertEqual(kinds("structs"), [(TokenKind.IDENTIFIER, "structs")])
        self.assertEqual(kinds("let_x"), [(TokenKind.IDENTIFIER, "let_x")])
        self.assertEqual(kinds("if_"), [(TokenKind.IDENTIFIER, "if_")])


class TestIdentifiersAndNumbers(unittest.TestCase):
    def test_identifiers(self):
        toks = tokenize("hello _private uid_counter x1 data")
        self.assertEqual([t.value for t in toks], ["hello", "_private", "uid_counter", "x1", "data"])
        self.assertTrue(all(t.kind == TokenKind.IDENTIFIER for t in toks))

    def test_integers(self):
        toks = tokenize("0 42 007")
        self.assertEqual([t.value for t in toks], [0, 42, 7])
        self.assertEqual([t.raw for t in toks], ["0", "42", "007"])

    def test_floats(self):
        toks = tokenize("3.14 1.0 0.5")
        self.assertEqual([t.value for t in toks], [3.14, 1.0, 0.5])
        self.assertTrue(all(t.kind == TokenKind.FLOAT for t in toks))

    def test_dot_after_number_is_not_float(self):
        toks = tokenize("1.")
        self.assertEqual(
            [(t.kind, t.raw) for t in toks],
            [(TokenKind.INTEGER, "1"), (TokenKind.DOT, ".")],
        )
        toks = tokenize("1.2.3")
        self.assertEqual(
            [(t.kind, t.raw) for t in toks],
            [(TokenKind.FLOAT, "1.2"), (TokenKind.DOT, "."), (TokenKind.INTEGER, "3")],
        )


class TestStrings(unittest.TestCase):
    def test_basic_strings(self):
        toks = tokenize('"hello" \'world\'')
        self.assertEqual([t.value for t in toks], ["hello", "world"])
        self.assertEqual([t.raw for t in toks], ['"hello"', "'world'"])
        self.assertTrue(all(t.kind == TokenKind.STRING for t in toks))

    def test_escapes(self):
        src = r'"a\nb\rc\td\\e\"f\'g\0h\bi\vj"'
        toks = tokenize(src)
        self.assertEqual(toks[0].value, "a\nb\rc\td\\e\"f'g\0h\bi\vj")

    def test_quoted_quote(self):
        toks = tokenize(r"'it\'s'")
        self.assertEqual(toks[0].value, "it's")

    def test_braces_in_strings(self):
        # `{` is literal by default; \{ and \} escape literal braces.
        self.assertEqual(tokenize(r'"a{b}c"')[0].value, "a{b}c")
        self.assertEqual(tokenize(r'"a\{b\}c"')[0].value, "a{b}c")

    def test_unknown_escape_kept_verbatim(self):
        self.assertEqual(tokenize(r'"\q"')[0].value, "\\q")

    def test_multiline_string_indentation_counts(self):
        src = '"a\\\n    b"'
        toks = tokenize(src)
        self.assertEqual(len(toks), 1)
        self.assertEqual(toks[0].value, "a    b")
        self.assertEqual(toks[0].raw, '"a\\\n    b"')
        self.assertEqual((toks[0].line, toks[0].end_line), (1, 2))

    def test_multiline_string_grammar_style(self):
        src = 'return "\\\nName : {self.name}\\n\\\nAge  : {self.age}\\n\\\n".format();'
        toks = tokenize(src)
        self.assertEqual(
            [t.kind for t in toks],
            [
                TokenKind.RETURN,
                TokenKind.STRING,
                TokenKind.DOT,
                TokenKind.IDENTIFIER,
                TokenKind.LPAREN,
                TokenKind.RPAREN,
                TokenKind.SEMICOLON,
            ],
        )
        self.assertEqual(toks[1].value, "Name : {self.name}\nAge  : {self.age}\n")

    def test_unterminated_string(self):
        with self.assertRaises(LexError) as cm:
            tokenize('let a: String = "abc;')
        self.assertEqual((cm.exception.line, cm.exception.column), (1, 17))

    def test_unterminated_string_after_continuation(self):
        with self.assertRaises(LexError):
            tokenize('"abc\\\n')
        with self.assertRaises(LexError):
            tokenize('"abc\\\ndef')

    def test_unterminated_reported_at_eof(self):
        lexer = Lexer()
        self.assertEqual(lexer.feed_line('"abc'), [])
        lexer.eof()
        self.assertEqual(len(lexer.errors), 1)
        self.assertEqual((lexer.errors[0].line, lexer.errors[0].column), (1, 1))

    def test_lex_with_errors_collects_all(self):
        result = lex_with_errors("let a: Int = 1~?;")
        self.assertEqual(len(result.errors), 2)
        self.assertEqual(result.errors[0].category, "unexpected character '~'")
        self.assertIn("'~' is not valid", result.errors[0].message)
        # recovery still produced the surrounding tokens
        kinds = [t.kind for t in result.tokens]
        self.assertIn(TokenKind.LET, kinds)
        self.assertIn(TokenKind.SEMICOLON, kinds)

    def test_double_plus_minus_rejected(self):
        for src in ("1++2", "1--2", "++x", "--x"):
            result = lex_with_errors(src)
            self.assertTrue(
                any(
                    e.category == "wind has no increment/decrement operator"
                    and "is not a valid postfix operator" in e.message
                    for e in result.errors
                ),
                src,
            )
        # spaced `+ +` / `- -` remain ordinary operator sequences
        self.assertEqual(lex_with_errors("1 + + 2").errors, [])
        self.assertEqual(lex_with_errors("1 - - 2").errors, [])

    def test_unknown_escape_warns_verbatim(self):
        result = lex_with_errors(r'"\q"')
        self.assertEqual(result.errors, [])
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("unknown escape sequence", result.warnings[0].message)
        self.assertEqual(result.tokens[0].value, "\\q")

    def test_multiline_string_raw_newline_kept(self):
        src = '"first line\n    second line"'
        toks = tokenize(src)
        self.assertEqual(len(toks), 1)
        self.assertEqual(toks[0].value, "first line\n    second line")
        self.assertEqual((toks[0].line, toks[0].end_line), (1, 2))

    def test_multiline_string_rust_style(self):
        # raw newlines are content (like Rust): the leading/trailing newlines
        # around the text stay in the value.
        a = '"\n Hello, World!\n"'
        self.assertEqual(tokenize(a)[0].value, "\n Hello, World!\n")

        # backslash-newline escapes the physical newline, so the same text
        # written with escaped line breaks contains no newlines at all.
        b = '"\\\n Hello, Wind!\\\n"'
        self.assertEqual(tokenize(b)[0].value, " Hello, Wind!")


class TestComments(unittest.TestCase):
    def test_line_comment_skipped(self):
        toks = tokenize("let a: Int = 1; // trailing comment")
        self.assertEqual(
            [t.kind for t in toks],
            [
                TokenKind.LET, TokenKind.IDENTIFIER, TokenKind.COLON,
                TokenKind.IDENTIFIER, TokenKind.ASSIGN, TokenKind.INTEGER,
                TokenKind.SEMICOLON,
            ],
        )

    def test_block_comment_skipped(self):
        toks = tokenize("let a: Int = /* inline */ 1;")
        self.assertEqual([t.kind for t in toks][-2:], [TokenKind.INTEGER, TokenKind.SEMICOLON])
        toks = tokenize("/* line1\nline2 */ let b: Int = 2;")
        self.assertEqual(toks[0].kind, TokenKind.LET)

    def test_comments_emitted(self):
        toks = tokenize("// hi", emit_comments=True)
        self.assertEqual(toks[0].kind, TokenKind.COMMENT)
        self.assertEqual(toks[0].value, " hi")
        self.assertEqual(toks[0].raw, "// hi")

        toks = tokenize("/* a\nb */", emit_comments=True)
        self.assertEqual(toks[0].value, " a\nb ")
        self.assertEqual(toks[0].raw, "/* a\nb */")
        self.assertEqual((toks[0].line, toks[0].end_line), (1, 2))

    def test_unterminated_block_comment(self):
        with self.assertRaises(LexError) as cm:
            tokenize("let a: Int = 1;\n/* oops")
        self.assertEqual((cm.exception.line, cm.exception.column), (2, 1))

    def test_comment_markers_inside_string(self):
        self.assertEqual(
            kinds('let s: String = "http://x /* not a comment */";'),
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
    CASES = [
        (">", TokenKind.GT), ("<", TokenKind.LT),
        ("<=", TokenKind.LE), (">=", TokenKind.GE),
        ("!=", TokenKind.NE), ("!<", TokenKind.GE), ("!>", TokenKind.LE),
        ("==", TokenKind.EQ), ("===", TokenKind.ADDR_EQ), ("=", TokenKind.ASSIGN),
        ("+=", TokenKind.PLUS_ASSIGN), ("-=", TokenKind.MINUS_ASSIGN),
        ("*=", TokenKind.STAR_ASSIGN), ("/=", TokenKind.SLASH_ASSIGN),
        ("->", TokenKind.ARROW),
        ("!", TokenKind.NOT), ("&&", TokenKind.AND), ("||", TokenKind.OR),
        ("-", TokenKind.MINUS), ("/", TokenKind.SLASH), ("%", TokenKind.PERCENT),
        ("*", TokenKind.STAR), ("+", TokenKind.PLUS),
        ("<<", TokenKind.SHL), (">>", TokenKind.SHR),
        ("&", TokenKind.AMP), ("|", TokenKind.PIPE), ("^", TokenKind.CARET),
        (";", TokenKind.SEMICOLON), (":", TokenKind.COLON), ("::", TokenKind.PATH),
        ("..", TokenKind.UNPACK), (".", TokenKind.DOT),
        ("(", TokenKind.LPAREN), (")", TokenKind.RPAREN),
        ("[", TokenKind.LBRACKET), ("]", TokenKind.RBRACKET),
        ("{", TokenKind.LBRACE), ("}", TokenKind.RBRACE),
        ("@", TokenKind.AT), (",", TokenKind.COMMA),
        ("\\", TokenKind.BACKSLASH),
        ("$", TokenKind.DOLLAR), ("#", TokenKind.HASH),
    ]

    def test_every_operator(self):
        for op, kind in self.CASES:
            toks = tokenize(op)
            self.assertEqual(len(toks), 1, op)
            self.assertEqual(toks[0].kind, kind, op)
            self.assertEqual(toks[0].raw, op, op)

    def test_maximal_munch(self):
        src = "=== == = :: : .. . -> - < : !< !> != ! << < >> > && & || | += +"
        self.assertEqual(
            [t.kind for t in tokenize(src)],
            [
                TokenKind.ADDR_EQ, TokenKind.EQ, TokenKind.ASSIGN,
                TokenKind.PATH, TokenKind.COLON, TokenKind.UNPACK, TokenKind.DOT,
                TokenKind.ARROW, TokenKind.MINUS, TokenKind.LT, TokenKind.COLON,
                TokenKind.GE, TokenKind.LE,
                TokenKind.NE, TokenKind.NOT, TokenKind.SHL, TokenKind.LT,
                TokenKind.SHR, TokenKind.GT, TokenKind.AND, TokenKind.AMP,
                TokenKind.OR, TokenKind.PIPE, TokenKind.PLUS_ASSIGN, TokenKind.PLUS,
            ],
        )

    def test_unexpected_character(self):
        for ch in ["~", "?", "`"]:
            with self.assertRaises(LexError):
                tokenize(f"let a: Int = 1{ch};")


class TestLexErrorPositions(unittest.TestCase):
    def test_unterminated_string_end(self):
        with self.assertRaises(LexError) as cm:
            tokenize('"abc')
        self.assertEqual((cm.exception.line, cm.exception.column), (1, 1))
        self.assertEqual((cm.exception.end_line, cm.exception.end_column), (1, 5))

    def test_unexpected_char_end(self):
        with self.assertRaises(LexError) as cm:
            tokenize("let a: Int = 1~;")
        self.assertEqual((cm.exception.line, cm.exception.column), (1, 15))
        self.assertEqual((cm.exception.end_line, cm.exception.end_column), (1, 16))


class TestStreaming(unittest.TestCase):
    def test_state_across_lines(self):
        lexer = Lexer()
        out = []
        for line in [
            '"x\\',            # string opens, backslash-newline continuation
            '  y";',           # closes; indentation is part of the value
            "/* block",        # block comment spans lines
            "still */",
            "let b: Int = 2; // done",
        ]:
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
        tok = tokenize('"hi"')[0]
        d = tok.to_dict()
        self.assertEqual(d["kind"], "string")
        self.assertEqual(d["value"], "hi")
        self.assertEqual(d["raw"], '"hi"')
        self.assertEqual(d["line"], 1)
        self.assertEqual(d["column"], 1)

    def test_tokens_to_json_roundtrip(self):
        toks = tokenize("let a: Int = 1;")
        data = json.loads(tokens_to_json(toks))
        self.assertEqual(data[0]["kind"], "LET")
        self.assertEqual(data[0]["value"], "let")
        self.assertEqual(data[5]["kind"], "integer")
        self.assertEqual(data[5]["value"], 1)
        self.assertEqual(len(data), len(toks))

    def test_stream_tokens(self):
        toks = list(stream_tokens(['let a: String = "x\\', 'y";']))
        self.assertEqual(
            [t.kind for t in toks][:3],
            [TokenKind.LET, TokenKind.IDENTIFIER, TokenKind.COLON],
        )
        self.assertEqual(toks[5].kind, TokenKind.STRING)
        self.assertEqual(toks[5].value, "xy")


GRAMMAR_EXAMPLE = r"""const hello: String = "hello, world!";

const data: Map = {
    "key_1" : "value_1",
};

const array : Vector<String> = [ "hello", "world" ];


pub fn test(input: String) -> None {
    builtins::output(input);
}

pub trait DisplayJson {
    fn str(self) -> String;
}

pub struct TestStruct {
    pub data: Map;
}

impl DisplayJson for TestStruct {
    pub fn str(self) -> String {
        let result: String = "{\n";
        for (kv: self.entry()) {
            result += "\"{kv.key}\": \"{kv.value}\"".format();
            if ( kv == self.get_last() ) {
                result += "\n}";
            } else {
                result += ",\n";
            }
        }
        return result;
    }
}

extra obtain: TestStruct {
    pub fn get(self, possible_key: String) -> String {
        if ( self.data.contains(possible_key) ) {
            return self.data[possible_key];
        } return "";
    }
}

type Email = String where {
    self.length >= 5 && self.length < 20;
    self.matches("@[a-zA-Z]+\\.[a-zA-Z]+");
}
type Name  = String where {
    self.length > 1 && self.length < 10;
}
struct User {
    pub email: Email;
    pub name : Name;
    pub uid  : Int where { uid.length == 11 };
    pub age  : Int -> { age > 0 && age < 65 };
    static uid_counter: Int = 0;
}


enum Color {
    Red,
    Green,
    Blue,
}

pub enum Student {
    age = 1,
    id  = 2,
}
// 类似 C 的枚举写法, 可以带整数初始值, 但不会发生隐式类型转换


extra User {
    pub fn new(
        email: String,
        name : String,
        age  : Int
    ) -> Self {
        return User {
            email, name, Self::uid_counter, age
        };
    }
    
    static fn growth() -> None, which ::new {
        Self::uid_counter += 1;
    }
    
    fn str(self) -> String {
        return "\
Name : {self.name}\n\
Age  : {self.age}\n\
UID  : {self.uid}\n\
Email: {self.email}\n\
".format();
    }
}

group Dispenser(a: String, b: String) {
    a -> Name;
    b -> Email;
}
Dispenser@User -> {name, email}


fn main(args: Vector<String>) -> Int {
    let admin: User = User::new(
        "admin@wind-lang.starwindv.top",
        "admin",
         9 + 9 * 2 / 3 + 3
    );
    output(admin);
    // or: output(admin.str());
}
"""


class TestGrammarExample(unittest.TestCase):
    def test_full_example_tokenizes(self):
        toks = tokenize(GRAMMAR_EXAMPLE)

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
        src = (
            'enum Color { Red, Green = 2, }\n'
            'let continued: String = "\n'
            'first line\n'
            '    indented second line";\n'
        )
        toks = tokenize(src)
        vals = [(t.kind, t.value) for t in toks]
        self.assertIn((TokenKind.IDENTIFIER, "Green"), vals)
        self.assertIn((TokenKind.INTEGER, 2), vals)
        str_values = [t.value for t in toks if t.kind == TokenKind.STRING]
        self.assertIn("\nfirst line\n    indented second line", str_values)

    def test_for_in_lexes_as_identifier(self):
        src = "fn f() -> None { for word in arr { } }"
        toks = tokenize(src)
        self.assertEqual(
            len([t for t in toks if t.kind == TokenKind.IDENTIFIER and t.value == "in"]),
            1,
        )


if __name__ == "__main__":
    unittest.main()
