"""todo-85: hexadecimal integer literals (``0x1F`` / ``0Xff``, u64 range).

Pipeline-level cases live in ``cases/hex`` (data-driven); lexer/parser unit
behaviour and typed-AST raw preservation are asserted here directly.
"""

import sys
import unittest
from dataclasses import fields as dc_fields
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

import harness

from cwind_frontend import (
    IntLit,
    lex_with_errors,
    parse_with_errors,
    tokenize,
)
from cwind_frontend.ast_components.token import TokenKind

HEX = "hex"

U64_MAX = 18446744073709551615


class HexLexerTests(unittest.TestCase):
    def test_basic_hex_tokens(self):
        toks = tokenize("0x0 0xFF 0Xde 0xBeef")
        ints = [t for t in toks if t.kind == TokenKind.INTEGER]
        self.assertEqual(
            [0, 255, 222, 48879],
            [t.value for t in ints],
        )
        self.assertEqual(
            ["0x0", "0xFF", "0Xde", "0xBeef"],
            [t.raw for t in ints],
        )

    def test_u64_max_is_exact(self):
        (tok,) = [t for t in tokenize("0xFFFFFFFFFFFFFFFF")
                  if t.kind == TokenKind.INTEGER]
        self.assertEqual(U64_MAX, tok.value)
        self.assertEqual("0xFFFFFFFFFFFFFFFF", tok.raw)

    def test_bare_prefix_then_non_hex_is_a_lex_error(self):
        # `g` cannot continue a hex literal and the empty digit run makes
        # the token malformed -> lexical error (Rust behaves the same).
        result = lex_with_errors("0xg")
        self.assertTrue(any(
            "hexadecimal literal requires" in e.message for e in result.errors
        ))

    def test_bare_prefix_is_a_lex_error(self):
        result = lex_with_errors("let x: UInt64 = 0x;")
        self.assertTrue(any(
            "hexadecimal literal requires" in e.message for e in result.errors
        ))

    def test_malformed_prefix_recovers(self):
        # The malformed run is swallowed; the trailing statement still lexes.
        result = lex_with_errors("0xZZZ 7")
        self.assertTrue(result.errors)
        kinds = [t.kind for t in result.tokens]
        self.assertIn(TokenKind.INTEGER, kinds)

    def test_decimal_leading_zero_untouched(self):
        (tok,) = [t for t in tokenize("007") if t.kind == TokenKind.INTEGER]
        self.assertEqual(7, tok.value)
        self.assertEqual("007", tok.raw)


class HexParserSaTests(harness.CaseAssertionsMixin, unittest.TestCase):
    def _int_lits(self, text):
        lexed = lex_with_errors(text)
        assert not lexed.errors
        parsed = parse_with_errors(lexed.tokens)
        assert not parsed.errors, parsed.errors
        lits: list[IntLit] = []

        def walk(node):
            if isinstance(node, IntLit):
                lits.append(node)
            for f in dc_fields(node):
                v = getattr(node, f.name)
                if isinstance(v, node.__class__):  # pragma: no cover
                    continue
                if hasattr(v, "line") and hasattr(v, "column"):
                    walk(v)
                elif isinstance(v, list):
                    for item in v:
                        if hasattr(item, "line") and hasattr(item, "column"):
                            walk(item)

        for item in parsed.program.items:
            walk(item)
        return lits

    def test_intlit_keeps_raw_and_value(self):
        lits = self._int_lits("fn main() -> Int { return 0xCAFE; }")
        self.assertEqual(1, len(lits))
        self.assertEqual(0xCAFE, lits[0].value)
        self.assertEqual("0xCAFE", lits[0].raw)

    def test_hex_in_range_checks(self):
        program = (
            "fn main() -> Int {\n"
            "    let a: UInt64 = 0xFFFFFFFFFFFFFFFF;\n"
            "    let b: Byte = 0xFF;\n"
            "    let c: Int32 = -0x7FFFFFFF;\n"
            "    return 0;\n"
            "}\n"
        )
        self.assert_source(program, {})

    def test_hex_overflow_rejected_by_sa(self):
        self.assert_source(
            "fn main() -> Int { let a: Byte = 0x100; return a; }",
            {"kind": "sa_err",
             "errors": [{"contains": "does not fit in Byte"}]},
        )

    def test_hex_pattern_match(self):
        program = (
            "fn main() -> Int {\n"
            "    let v: Int = 0x10;\n"
            "    match (v) { 0x10 => { return 7; }, _ => { return 0; } }\n"
            "}\n"
        )
        self.assert_source(program, {})

    def test_hex_enum_discriminant_and_array_index(self):
        program = (
            "enum Flags { A = 0x1, B = 0x2 }\n"
            "fn main() -> Int {\n"
            "    let arr: [Int; 3] = [1, 2, 3];\n"
            "    return arr[0x1] + arr[0x02];\n"
            "}\n"
        )
        self.assert_source(program, {})


class HexDataDrivenTests(harness.CaseAssertionsMixin, unittest.TestCase):
    def test_cases(self):
        area = TESTS / "cases" / HEX
        if not area.exists():
            self.skipTest("no data-driven cases")
        for wind in sorted(area.glob("*.wind")):
            with self.subTest(case=wind.stem):
                self.assert_case(HEX, wind.stem)


if __name__ == "__main__":
    unittest.main()
