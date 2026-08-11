"""Unit tests for cwind_frontend.parser."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cwind_frontend import (
    Assign,
    Attribute,
    BinOp,
    BoolLit,
    BreakStmt,
    Call,
    ConstDecl,
    ContinueStmt,
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
    TypeParam,
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

    def test_generic_struct_construct(self):
        prog = parse_source(
            "struct Node<T> { pub k: UInt, pub v: T }"
            "fn f<T>(k: UInt, v: T) -> Node<T> { return Node<T> { k, v }; }"
        )
        fn = prog.items[1]
        sc = fn.body.stmts[0].value
        self.assertIsInstance(sc, StructConstruct)
        self.assertEqual(sc.type.name, "Node")
        self.assertEqual([a.name for a in sc.type.args], ["T"])
        self.assertEqual(len(sc.args), 2)

    def test_map_literal_only_allowed_after_assignment(self):
        with self.assertRaises(ParseError):
            parse_source('fn f() -> Map<String, Int> { return { "a": 1 }; }')

    def test_comparison_with_map_literal_after_assignment(self):
        st = stmt('let x: Bool = A < B > { "a": 1 };')
        self.assertIsInstance(st.value, BinOp)
        self.assertEqual(st.value.op, TokenKind.GT)
        self.assertIsInstance(st.value.right, MapLit)

    def test_unpack_removed(self):
        # `..` unpack in call arguments has been cut from the language.
        with self.assertRaises(ParseError):
            stmt("let t: Int = sum3(..v);")

    def test_bool_literals(self):
        st = stmt("let b: Bool = true;")
        self.assertIsInstance(st.value, BoolLit)
        self.assertTrue(st.value.value)
        st = stmt("let b: Bool = false;")
        self.assertFalse(st.value.value)
        # only lowercase literals are recognized; `True` stays an identifier
        st = stmt("let b: Bool = True;")
        self.assertIsInstance(st.value, Name)

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

    def test_break_continue(self):
        st = stmt("break;")
        self.assertIsInstance(st, BreakStmt)
        st = stmt("continue;")
        self.assertIsInstance(st, ContinueStmt)

        # inside loop bodies they parse to the same nodes
        st = stmt("while (i < 5) { break; }")
        self.assertIsInstance(st, WhileStmt)
        self.assertIsInstance(st.body.stmts[0], BreakStmt)
        st = stmt("for x in arr { continue; }")
        self.assertIsInstance(st, ForStmt)
        self.assertIsInstance(st.body.stmts[0], ContinueStmt)

    def test_break_continue_require_semicolon(self):
        with self.assertRaises(ParseError):
            stmt("break")
        with self.assertRaises(ParseError):
            stmt("continue")

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

    def test_typedef(self):
        prog = parse_source(
            "typedef DoubleMap<K, T, V> = Map<K, Map<T, V>>;"
            "typedef Foo<K, V> = Map<K, V>;"
            "typedef MyMap = Map<String, Int>;"
        )
        self.assertEqual(prog.items[0].base.name, "Map")
        self.assertEqual([p.name for p in prog.items[0].params], ["K", "T", "V"])
        self.assertEqual([p.name for p in prog.items[1].params], ["K", "V"])
        self.assertEqual(prog.items[2].base.name, "Map")

    def test_typedef_requires_semicolon(self):
        with self.assertRaises(ParseError):
            parse_source("typedef Foo = Map<String, Int> fn main() -> None {}")

    def test_fn_generic_params(self):
        prog = parse_source("fn test<T>() { let s: Map<T, String> = {}; }")
        self.assertEqual(prog.items[0].type_params[0].name, "T")

    def test_param_requires_type_except_self(self):
        with self.assertRaises(ParseError):
            parse_source("fn f(a: Int, b) -> Int { return a; }")
        parse_source("fn f(self) -> None { }")  # self is exempt

    def test_empty_group_rejected(self):
        with self.assertRaises(ParseError) as cm:
            parse_source("group G { }")
        self.assertIn("group policy cannot be empty", cm.exception.message)

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

    def test_unit_struct(self):
        prog = parse_source("pub struct Empty; struct WithBrace {}")
        self.assertEqual(prog.items[0].fields, [])
        self.assertEqual(prog.items[1].fields, [])

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

    def test_generic_trait(self):
        prog = parse_source(
            "pub trait Cmp<T: Into<String>> { fn eq(self, other: T) -> Bool; }"
        )
        item = prog.items[0]
        self.assertIsInstance(item, TraitDecl)
        self.assertEqual(len(item.params), 1)
        self.assertIsInstance(item.params[0], TypeParam)
        self.assertEqual(item.params[0].name, "T")
        self.assertEqual(item.params[0].bound.name, "Into")
        self.assertEqual(item.params[0].bound.args[0].name, "String")

    def test_trait_default_method_body(self):
        prog = parse_source("trait X<T> { fn f(self) -> None { } fn g(self) -> None; }")
        item = prog.items[0]
        self.assertIsNotNone(item.methods[0].body)
        self.assertIsNone(item.methods[1].body)

    def test_impl(self):
        prog = parse_source(
            "impl DisplayJson for TestStruct { pub fn str(self) -> String { return \"\"; } }"
        )
        item = prog.items[0]
        self.assertIsInstance(item, ImplDecl)
        self.assertEqual(item.trait.name, "DisplayJson")
        self.assertEqual(item.struct.name, "TestStruct")

    def test_extra(self):
        prog = parse_source(
            "extra User { static fn growth() -> None, which ::new { Self::uid_counter += 1; } }"
        )
        item = prog.items[0]
        self.assertIsInstance(item, ExtraDecl)
        self.assertEqual(item.struct.name, "User")
        self.assertEqual(item.methods[0].static, True)
        self.assertEqual(item.methods[0].which, "new")

    def test_generic_extra(self):
        prog = parse_source(
            "struct Point<T> { x: T, y: T }"
            "extra<T> Point<T> { fn new(x: T, y: T) -> Self { Point { x, y }; } }"
        )
        item = prog.items[1]
        self.assertIsInstance(item, ExtraDecl)
        self.assertEqual(item.params[0].name, "T")
        self.assertEqual(item.struct.name, "Point")
        self.assertEqual(item.struct.args[0].name, "T")

    def test_comma_separated_struct_fields(self):
        prog = parse_source(
            "struct Point<T> { x: T, y: T }"
            "struct Mixed { a: Int, b: String; c: Bool, }"
        )
        self.assertEqual([f.name for f in prog.items[1].fields], ["a", "b", "c"])

    def test_struct_construct_statement(self):
        prog = parse_source(
            "struct P { x: Int, y: Int }"
            "fn f() -> None { P { 1, 2 }; }"
        )
        self.assertIsInstance(prog.items[1].body.stmts[0].expr, StructConstruct)

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

    def test_map_missing_colon_recovers_without_escaping_block(self):
        # `{ "a" 1 }` must report the missing ':' at the right token and then
        # finish the statement cleanly; the `;` must not leak out as a bogus
        # top-level error.
        src = 'fn f() -> None { let d: Map<String, Int> = { "a" 1 }; }'
        result = parse_with_errors(tokenize_source(src))
        self.assertEqual(len(result.errors), 1)
        err = result.errors[0]
        self.assertIn("':' between map key and value", err.message)
        self.assertEqual(src.index("1", src.index("{ \"a\" 1 }")) + 1, err.column)

    def test_map_multiple_missing_colons_collected(self):
        src = 'fn f() -> None { let m: Map<String, Int> = { "a" 1, "b" 2 }; }'
        result = parse_with_errors(tokenize_source(src))
        self.assertEqual(len(result.errors), 2)
        self.assertTrue(
            all("':' between map key and value" in e.message for e in result.errors)
        )

    def test_struct_construct_missing_comma_recovers(self):
        src = "struct S { pub x: Int; } fn f() -> None { let s: S = S { 1 2 }; }"
        result = parse_with_errors(tokenize_source(src))
        self.assertEqual(len(result.errors), 1)
        self.assertIn("'}' after struct construction", result.errors[0].message)


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


_COMPACT_PROGRAM = (
    'const hello: String = "hi";\n'
    "type Email = String where { self.length >= 3; }\n"
    "struct User { pub email: Email; static counter: Int = 0; }\n"
    "enum Color { Red, Green = 2, }\n"
    "trait DisplayJson { fn str(self) -> String; }\n"
    'impl DisplayJson for User { pub fn str(self) -> String { return "x".format(); } }\n'
    "extra User { pub fn new(email: Email) -> User { return User { email }; } }\n"
    "group G(a: String) { a -> Email; }\n"
    "G@User -> {email}\n"
    "fn main(args: Vector<String>) -> Int {\n"
    "    let b: Bool = true;\n"
    "    for (word: args) { print(word); }\n"
    "    if (b) { return 0; } else { return 1; }\n"
    "}\n"
)


class TestExamFiles(unittest.TestCase):
    def test_compact_program_parses(self):
        prog = parse_source(_COMPACT_PROGRAM)
        self.assertIsInstance(prog, Program)
        self.assertGreater(len(prog.items), 8)
        kinds = [type(i).__name__ for i in prog.items]
        for expected in ["ConstDecl", "TypeDecl", "StructDecl", "EnumDecl", "TraitDecl",
                         "ImplDecl", "ExtraDecl", "GroupDecl", "GroupApply", "FnDecl"]:
            self.assertIn(expected, kinds)

    def test_bool_literal_case_matrix_parses(self):
        # lowercase literals parse as BoolLit; capital `True` stays a Name
        st = stmt("let b: Bool = true;")
        self.assertIsInstance(st.value, BoolLit)
        st = stmt("let b: Bool = True;")
        self.assertIsInstance(st.value, Name)

    def test_ast_to_json(self):
        prog = parse_source('const hello: String = "hi"; fn main() -> Int { return 0; }')
        data = prog.to_dict()
        self.assertEqual(data["kind"], "Program")
        self.assertEqual(data["items"][0]["kind"], "ConstDecl")
        json.dumps(data)  # must be JSON-serializable


if __name__ == "__main__":
    unittest.main()
