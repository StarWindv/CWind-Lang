"""Unit tests for cwind_frontend.parser."""

import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cwind_frontend import (
    Assign,
    Attribute,
    BinOp,
    Call,
    ConstDecl,
    EnumDecl,
    ExtraDecl,
    FnDecl,
    ForStmt,
    GroupApply,
    GroupDecl,
    IfStmt,
    ImplDecl,
    Index,
    LetStmt,
    MapLit,
    Name,
    ParseError,
    Program,
    ReturnStmt,
    Slice,
    StructConstruct,
    StructDecl,
    TraitDecl,
    Type,
    TypeDecl,
    UnaryOp,
    VectorLit,
    WhileStmt,
    parse_source,
    parse_with_errors,
    tokenize,
    TokenKind,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _grammar_example():
    text = (REPO_ROOT / "frontend" / "Grammar.md").read_text(encoding="utf-8")
    match = re.search(r"```cwind\n(.*?)```", text, re.S)
    assert match is not None, "cwind code block not found in Grammar.md"
    return match.group(1)


GRAMMAR_EXAMPLE = _grammar_example()


def fn_body(src):
    """Parse `fn f() -> None { <src> }` and return its statement list."""
    prog = parse_source("fn f() -> None {" + src + "}")
    fn = prog.items[0]
    assert isinstance(fn, FnDecl) and fn.body is not None
    return fn.body.stmts


def stmt(src):
    stmts = fn_body(src)
    assert len(stmts) == 1
    return stmts[0]


def tokenize_source(src):
    return tokenize(src)


class TestExpressions(unittest.TestCase):
    def test_precedence(self):
        st = stmt("let x: Int = 1 + 2 * 3;")
        self.assertIsInstance(st, LetStmt)
        value = st.value
        self.assertIsInstance(value, BinOp)
        self.assertEqual(value.op, TokenKind.PLUS)
        self.assertEqual(value.left.value, 1)
        self.assertEqual(value.right.op, TokenKind.STAR)

    def test_assignment_ops(self):
        for op, kind in [
            ("=", TokenKind.ASSIGN),
            ("+=", TokenKind.PLUS_ASSIGN),
            ("-=", TokenKind.MINUS_ASSIGN),
            ("*=", TokenKind.STAR_ASSIGN),
            ("/=", TokenKind.SLASH_ASSIGN),
            ("<:", TokenKind.ABS_LT),
            (":>", TokenKind.ABS_GT),
        ]:
            st = stmt(f"x {op} 1;")
            expr = st.expr
            self.assertIsInstance(expr, Assign, op)
            self.assertEqual(expr.op, kind, op)

    def test_unary(self):
        st = stmt("let n: Int = -a;")
        self.assertIsInstance(st.value, UnaryOp)
        self.assertEqual(st.value.op, TokenKind.MINUS)
        st = stmt("let l: Bool = !(a > b);")
        self.assertIsInstance(st.value, UnaryOp)
        self.assertEqual(st.value.op, TokenKind.NOT)
        self.assertIsInstance(st.value.operand, BinOp)

    def test_non_math_comparisons(self):
        cases = [
            ("a !< b", TokenKind.NOT_LT),
            ("b !> a", TokenKind.NOT_GT),
            ("s === s", TokenKind.ADDR_EQ),
            ("a != b", TokenKind.NE),
            ("a <= b", TokenKind.LE),
        ]
        for src, kind in cases:
            st = stmt(f"let r: Bool = {src};")
            self.assertIsInstance(st.value, BinOp)
            self.assertEqual(st.value.op, kind, src)

    def test_path_and_methods(self):
        st = stmt("builtins::output(input);")
        expr = st.expr
        self.assertIsInstance(expr, Call)
        self.assertEqual(expr.callee.parts, ["builtins", "output"])

        st = stmt("let s: String = self.data.contains(possible_key).format();")
        expr = st.value
        self.assertIsInstance(expr, Call)
        self.assertIsInstance(expr.callee, Attribute)
        self.assertEqual(expr.callee.name, "format")

    def test_index_and_slices(self):
        st = stmt("let a: Int = v[0];")
        self.assertIsInstance(st.value, Index)
        st = stmt("let b: Vector<Int> = v[1:3];")
        self.assertEqual((st.value.start.value, st.value.stop.value, st.value.step), (1, 3, None))
        st = stmt("let c: Vector<Int> = v[::2];")
        self.assertEqual((st.value.start, st.value.stop), (None, None))
        self.assertEqual(st.value.step.value, 2)
        st = stmt("let d: Vector<Int> = v[:2];")
        self.assertEqual((st.value.start, st.value.stop.value), (None, 2))

    def test_literals(self):
        st = stmt('let m: Map = {"a" : 1, "b" : 2};')
        self.assertIsInstance(st.value, MapLit)
        self.assertEqual(len(st.value.entries), 2)
        st = stmt("let e: Vector<Int> = [];")
        self.assertIsInstance(st.value, VectorLit)
        self.assertEqual(st.value.elems, [])

    def test_struct_construct(self):
        st = stmt("let t: TestStruct = TestStruct { data };")
        self.assertIsInstance(st.value, StructConstruct)
        self.assertEqual(st.value.type.name, "TestStruct")
        self.assertEqual(len(st.value.args), 1)

    def test_unpack_arg(self):
        st = stmt("let t: Int = sum3(..v);")
        self.assertIsInstance(st.value, Call)
        self.assertTrue(st.value.args[0].unpack)

    def test_nested_generics(self):
        st = stmt("let m: Vector<Vector<Int>> = [];")
        self.assertEqual(st.type.name, "Vector")
        inner = st.type.args[0]
        self.assertEqual(inner.name, "Vector")
        self.assertEqual(inner.args[0].name, "Int")


class TestStatements(unittest.TestCase):
    def test_return(self):
        st = stmt("return 0;")
        self.assertIsInstance(st, ReturnStmt)
        self.assertEqual(st.value.value, 0)

    def test_if_elif_else(self):
        st = stmt(
            "if (a) { x(); } elif (b) { y(); } elif (c) { z(); } else { w(); }"
        )
        self.assertIsInstance(st, IfStmt)
        self.assertEqual(len(st.elifs), 2)
        self.assertIsNotNone(st.else_)

    def test_while(self):
        st = stmt("while (i < 5) { i += 1; }")
        self.assertIsInstance(st, WhileStmt)
        self.assertIsInstance(st.cond, BinOp)

    def test_for_in_forms(self):
        st = stmt("for word in arr { output(word); }")
        self.assertIsInstance(st, ForStmt)
        self.assertEqual(st.var, "word")
        self.assertIsNone(st.type)
        self.assertFalse(st.paren_style)

        st = stmt("for (word: arr) { output(word); }")
        self.assertIsNone(st.type)
        self.assertTrue(st.paren_style)

        st = stmt("for (Tuple kv: self.entry()) { output(kv.key); }")
        self.assertEqual(st.type.name, "Tuple")
        self.assertTrue(st.paren_style)


class TestDeclarations(unittest.TestCase):
    def test_const(self):
        prog = parse_source('const hello: String = "hi";')
        item = prog.items[0]
        self.assertIsInstance(item, ConstDecl)
        self.assertEqual(item.name, "hello")

    def test_type_decl(self):
        prog = parse_source(
            "type Email = String where { self.length >= 5; }"
        )
        item = prog.items[0]
        self.assertIsInstance(item, TypeDecl)
        self.assertEqual(item.base.name, "String")
        self.assertIsNotNone(item.where)

    def test_struct_fields(self):
        prog = parse_source(
            "struct User {"
            " pub email: Email;"
            " pub uid : Int where { uid.length == 11 };"
            " pub age : Int -> { age > 0 };"
            " static uid_counter: Int = 0;"
            "}"
        )
        item = prog.items[0]
        self.assertIsInstance(item, StructDecl)
        self.assertEqual(len(item.fields), 4)
        self.assertTrue(item.fields[3].static)
        self.assertIsNotNone(item.fields[3].initializer)
        self.assertIsNotNone(item.fields[1].validation)
        self.assertIsNotNone(item.fields[2].validation)

    def test_enum(self):
        prog = parse_source("enum Color { Red, Green = 2, Blue, }")
        item = prog.items[0]
        self.assertIsInstance(item, EnumDecl)
        self.assertEqual([v.name for v in item.variants], ["Red", "Green", "Blue"])
        self.assertEqual(item.variants[1].value, 2)

    def test_trait(self):
        prog = parse_source("pub trait DisplayJson { fn str(self) -> String; }")
        item = prog.items[0]
        self.assertIsInstance(item, TraitDecl)
        self.assertTrue(item.pub)
        self.assertEqual(item.methods[0].name, "str")
        self.assertIsNone(item.methods[0].body)

    def test_impl(self):
        prog = parse_source(
            "impl DisplayJson for TestStruct { pub fn str(self) -> String { return \"\"; } }"
        )
        item = prog.items[0]
        self.assertIsInstance(item, ImplDecl)
        self.assertEqual(item.trait.name, "DisplayJson")
        self.assertEqual(item.struct.name, "TestStruct")

    def test_extra_both_forms(self):
        prog = parse_source(
            "extra User { static fn growth() -> None, which ::new { Self::uid_counter += 1; } }"
        )
        item = prog.items[0]
        self.assertIsInstance(item, ExtraDecl)
        self.assertIsNone(item.name)
        self.assertEqual(item.methods[0].static, True)
        self.assertEqual(item.methods[0].which, "new")

        prog = parse_source("extra validators: User { fn is_adult(self) -> Bool { return true; } }")
        item = prog.items[0]
        self.assertIsInstance(item, ExtraDecl)
        self.assertEqual(item.name, "validators")
        self.assertEqual(item.struct, "User")

    def test_group_both_forms(self):
        prog = parse_source(
            "group Dispenser(a: String, b: String) { a -> Name; b -> Email; }"
        )
        item = prog.items[0]
        self.assertIsInstance(item, GroupDecl)
        self.assertEqual(len(item.params), 2)
        self.assertEqual(item.distributions[0].subject, "a")

        prog = parse_source(
            "group StrictUser: User { self.email -> Email; self.name -> Name; }"
        )
        item = prog.items[0]
        self.assertIsInstance(item, GroupDecl)
        self.assertEqual(item.struct, "User")
        self.assertTrue(item.distributions[0].subject_self)
        self.assertEqual(item.distributions[0].subject, "email")

    def test_group_apply_without_semicolon(self):
        prog = parse_source("Dispenser@User -> {name, email}")
        item = prog.items[0]
        self.assertIsInstance(item, GroupApply)
        self.assertEqual(item.group, "Dispenser")
        self.assertEqual(item.fields, ["name", "email"])


class TestErrors(unittest.TestCase):
    def test_let_requires_type(self):
        with self.assertRaises(ParseError) as cm:
            parse_source("fn f() -> None { let x = 5; }")
        self.assertIn("let needs a type", cm.exception.message)

    def test_missing_semicolon(self):
        with self.assertRaises(ParseError) as cm:
            parse_source("fn f() -> None { let x: Int = 1 }")
        self.assertIn("';'", cm.exception.message)

    def test_unclosed_block(self):
        with self.assertRaises(ParseError):
            parse_source("fn f() -> None { let x: Int = 1;")

    def test_bad_top_level(self):
        with self.assertRaises(ParseError):
            parse_source("foobar;")

    def test_parse_with_errors_collects_many(self):
        src = "fn f() -> None { let x = 1; let y = 2; let z = 3; }"
        result = parse_with_errors(tokenize_source(src))
        self.assertEqual(len(result.errors), 3)
        self.assertTrue(all("let needs a type" in e.message for e in result.errors))
        # recovery keeps the surrounding structure
        self.assertEqual(result.program.items[0].__class__.__name__, "FnDecl")

    def test_recovery_continues_after_item_error(self):
        src = "fn broken( -> Int { return 1; } fn ok() -> None {}"
        result = parse_with_errors(tokenize_source(src))
        self.assertEqual(len(result.errors), 1)
        kinds = [type(i).__name__ for i in result.program.items]
        self.assertIn("FnDecl", kinds)  # `fn ok` still parsed


FAIL_CASES_DIR = REPO_ROOT / "assets" / "parser_fail_cases"


class TestParserFailCases(unittest.TestCase):
    def test_all_case_files_fail(self):
        for path in sorted(FAIL_CASES_DIR.glob("case*.wind")):
            with self.subTest(path=path.name):
                with self.assertRaises(ParseError):
                    parse_source(path.read_text(encoding="utf-8"))

    def test_case07_missing_semicolon_position(self):
        src = (FAIL_CASES_DIR / "case07_missing_semicolon.wind").read_text(
            encoding="utf-8"
        )
        with self.assertRaises(ParseError) as cm:
            parse_source(src)
        # points at the end of `3` (line 4, column 19), not at the next line.
        self.assertEqual((cm.exception.line, cm.exception.column), (4, 19))
        self.assertIn("';' after let declaration", cm.exception.message)

    def test_case08_forin_missing_var(self):
        src = (FAIL_CASES_DIR / "case08_forin_missing_var.wind").read_text(
            encoding="utf-8"
        )
        with self.assertRaises(ParseError) as cm:
            parse_source(src)
        self.assertEqual((cm.exception.line, cm.exception.column), (4, 9))
        self.assertIn("expected iteration variable before 'in'", cm.exception.message)


class TestExamFiles(unittest.TestCase):
    def test_exam2_parses(self):
        src = (REPO_ROOT / "assets" / "exam2.wind").read_text(encoding="utf-8")
        prog = parse_source(src)
        self.assertIsInstance(prog, Program)
        self.assertGreater(len(prog.items), 15)
        kinds = [type(i).__name__ for i in prog.items]
        for expected in ["ConstDecl", "TypeDecl", "StructDecl", "EnumDecl", "TraitDecl",
                         "ImplDecl", "ExtraDecl", "GroupDecl", "GroupApply", "FnDecl"]:
            self.assertIn(expected, kinds)

    def test_exam_wind_parses(self):
        src = (REPO_ROOT / "assets" / "exam.wind").read_text(encoding="utf-8")
        # NOTE: exam.wind currently carries a deliberate error (line 120 is
        # missing its `;`) while the author experiments with error rendering.
        # Flip back to the clean-parse assertion once the file is fixed.
        with self.assertRaises(ParseError) as cm:
            parse_source(src)
        self.assertEqual(cm.exception.line, 120)
        self.assertIn("';'", cm.exception.message)

    def test_grammar_example_parses(self):
        prog = parse_source(GRAMMAR_EXAMPLE)
        self.assertGreater(len(prog.items), 10)

    def test_ast_to_json(self):
        prog = parse_source('const hello: String = "hi"; fn main() -> Int { return 0; }')
        data = prog.to_dict()
        self.assertEqual(data["kind"], "Program")
        self.assertEqual(data["items"][0]["kind"], "ConstDecl")
        json.dumps(data)  # must be JSON-serializable


if __name__ == "__main__":
    unittest.main()
