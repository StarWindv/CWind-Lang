"""Unit tests for cwind_frontend.parser (串联脚本).

Sources live in ``cases/parser`` (+ ``cases/common/compact_program.wind``
shared with the SA suite); expectations that are plain pipeline outcomes sit
in ``<name>.json`` sidecars (see harness.py).  Structural AST assertions
stay in this module and read their input programs from the case files.
"""

import json
import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

import harness

from cwind_frontend import (
    AssocType,
    Assign,
    Attribute,
    BindPattern,
    BinOp,
    Block,
    BoolLit,
    BreakStmt,
    Call,
    ConstDecl,
    ContinueStmt,
    EnumDecl,
    EnumPattern,
    ExtraDecl,
    ExprStmt,
    FnDecl,
    ForStmt,
    GroupApply,
    GroupDecl,
    IfLetStmt,
    IfStmt,
    ImplDecl,
    Index,
    IntLit,
    LetStmt,
    LitPattern,
    MapLit,
    MatchStmt,
    Name,
    ParseError,
    Program,
    ReturnStmt,
    Slice,
    StructConstruct,
    StructDecl,
    StructPattern,
    TokenKind,
    TraitDecl,
    Type,
    TupleLit,
    TypeParam,
    TypeDecl,
    TuplePattern,
    UnaryOp,
    VectorLit,
    WhileStmt,
    WildcardPattern,
    parse_source,
    parse_with_errors,
    tokenize,
)

PARSER = "parser"


def _table(name):
    return json.loads(
        (harness.CASES_DIR / PARSER / f"{name}.json").read_bytes()
    )


def prog(name):
    """Parse the full program stored at ``cases/parser/<name>.wind``."""
    return parse_source(harness.source(PARSER, name))


def fn_body(src):
    """Parse `fn f() -> None { <src> }` and return its statement list."""
    parsed = parse_source("fn f() -> None {" + src + "}")
    fn = parsed.items[0]
    assert isinstance(fn, FnDecl) and fn.body is not None
    return fn.body.stmts


def stmt_from_file(name):
    stmts = fn_body(harness.source(PARSER, name))
    assert len(stmts) == 1
    return stmts[0]


def tokenize_source(src):
    return tokenize(src)


class TestExpressions(unittest.TestCase):
    def test_precedence(self):
        st = stmt_from_file("precedence")
        self.assertIsInstance(st, LetStmt)
        value = st.value
        self.assertIsInstance(value, BinOp)
        self.assertEqual(value.op, TokenKind.PLUS)
        self.assertEqual(value.left.value, 1)
        self.assertEqual(value.right.op, TokenKind.STAR)

    def test_assignment_ops(self):
        for op, kind_name in _table("assign_ops"):
            # the five operator variants are generated from one template
            expr = fn_body(f"x {op} 1;")[0].expr
            self.assertIsInstance(expr, Assign, op)
            self.assertEqual(expr.op, TokenKind[kind_name], op)

    def test_unary(self):
        st = stmt_from_file("unary_minus")
        self.assertIsInstance(st.value, UnaryOp)
        self.assertEqual(st.value.op, TokenKind.MINUS)
        st = stmt_from_file("unary_not")
        self.assertIsInstance(st.value, UnaryOp)
        self.assertEqual(st.value.op, TokenKind.NOT)
        self.assertIsInstance(st.value.operand, BinOp)
        st = stmt_from_file("unary_borrow")
        self.assertIsInstance(st.value, UnaryOp)
        self.assertEqual(st.value.op, TokenKind.AMP)
        self.assertIsInstance(st.type, Type)
        self.assertTrue(st.type.ref)
        self.assertEqual(st.type.name, "Vector")

    def test_non_math_comparisons(self):
        for src, kind_name in _table("comparisons"):
            st = fn_body(f"let r: Bool = {src};")[0]
            self.assertIsInstance(st.value, BinOp)
            self.assertEqual(st.value.op, TokenKind[kind_name], src)

    def test_path_and_methods(self):
        st = stmt_from_file("path_call")
        expr = st.expr
        self.assertIsInstance(expr, Call)
        self.assertEqual(expr.callee.parts, ["builtins", "output"])

        st = stmt_from_file("method_chain")
        expr = st.value
        self.assertIsInstance(expr, Call)
        self.assertIsInstance(expr.callee, Attribute)
        self.assertEqual(expr.callee.name, "format")

    def test_index_and_slices(self):
        st = stmt_from_file("index_basic")
        self.assertIsInstance(st.value, Index)
        st = stmt_from_file("index_range")
        self.assertEqual((st.value.start.value, st.value.stop.value, st.value.step), (1, 3, None))
        st = stmt_from_file("index_step")
        self.assertEqual((st.value.start, st.value.stop), (None, None))
        self.assertEqual(st.value.step.value, 2)
        st = stmt_from_file("index_stop")
        self.assertEqual((st.value.start, st.value.stop.value), (None, 2))

    def test_literals(self):
        st = stmt_from_file("map_lit")
        self.assertIsInstance(st.value, MapLit)
        self.assertEqual(len(st.value.entries), 2)
        st = stmt_from_file("vector_empty")
        self.assertIsInstance(st.value, VectorLit)
        self.assertEqual(st.value.elems, [])

    def test_tuple_literal(self):
        st = stmt_from_file("tuple_two")
        self.assertIsInstance(st.value, TupleLit)
        self.assertEqual(len(st.value.elems), 2)
        st = stmt_from_file("tuple_trailing")
        self.assertIsInstance(st.value, TupleLit)
        self.assertEqual([e.value for e in st.value.elems], [1])
        st = stmt_from_file("tuple_unit")
        self.assertIsInstance(st.value, TupleLit)
        self.assertEqual(st.value.elems, [])
        # 单元素圆括号仍是普通分组, 不产生 TupleLit
        st = stmt_from_file("paren_group")
        self.assertNotIsInstance(st.value, TupleLit)

    def test_tuple_element_access(self):
        st = stmt_from_file("tuple_attr_0")
        self.assertIsInstance(st.value, Attribute)
        self.assertEqual(st.value.name, "0")
        # `p.0.0` 词法上是 `p . 0.0`, parser 拆回链式访问
        st = stmt_from_file("tuple_attr_nested")
        self.assertIsInstance(st.value, Attribute)
        self.assertEqual(st.value.name, "0")
        self.assertIsInstance(st.value.obj, Attribute)
        self.assertEqual(st.value.obj.name, "0")
        st = stmt_from_file("tuple_attr_wide")
        self.assertEqual(st.value.name, "10")
        self.assertEqual(st.value.obj.name, "0")

    def test_struct_construct(self):
        st = stmt_from_file("struct_construct_shorthand")
        self.assertIsInstance(st.value, StructConstruct)
        self.assertEqual(st.value.type.name, "TestStruct")
        self.assertEqual(len(st.value.args), 1)

    def test_generic_struct_construct(self):
        fn = prog("generic_struct_construct").items[1]
        sc = fn.body.stmts[0].value
        self.assertIsInstance(sc, StructConstruct)
        self.assertEqual(sc.type.name, "Node")
        self.assertEqual([a.name for a in sc.type.args], ["T"])
        self.assertEqual(len(sc.args), 2)

    def test_map_literal_only_allowed_after_assignment(self):
        self.assert_case_parse_error("map_return_error")

    def test_comparison_with_map_literal_after_assignment(self):
        st = stmt_from_file("cmp_map_literal")
        self.assertIsInstance(st.value, BinOp)
        self.assertEqual(st.value.op, TokenKind.GT)
        self.assertIsInstance(st.value.right, MapLit)

    def test_unpack_removed(self):
        # `..` unpack in call arguments has been cut from the language.
        self.assert_case_parse_error("unpack_in_call")

    def test_bool_literals(self):
        st = stmt_from_file("bool_true")
        self.assertIsInstance(st.value, BoolLit)
        self.assertTrue(st.value.value)
        st = stmt_from_file("bool_false")
        self.assertFalse(st.value.value)
        # only lowercase literals are recognized; `True` stays an identifier
        st = stmt_from_file("bool_true_caps")
        self.assertIsInstance(st.value, Name)

    def test_nested_generics(self):
        st = stmt_from_file("nested_generics")
        self.assertEqual(st.type.name, "Vector")
        inner = st.type.args[0]
        self.assertEqual(inner.name, "Vector")
        self.assertEqual(inner.args[0].name, "Int")

    def assert_case_parse_error(self, name):
        with self.assertRaises(ParseError):
            parse_source(harness.source(PARSER, name))


class TestStatements(unittest.TestCase):
    def test_return(self):
        st = stmt_from_file("return_stmt")
        self.assertIsInstance(st, ReturnStmt)
        self.assertEqual(st.value.value, 0)

    def test_function_tail_expression(self):
        parsed = prog("tail_expr")
        tail = parsed.items[0].body.stmts[0]
        effect = parsed.items[1].body.stmts[0]
        self.assertIsInstance(tail, ReturnStmt)
        self.assertEqual(tail.value.op, TokenKind.PLUS)
        self.assertIsInstance(effect, ExprStmt)

    def test_tail_expression_requires_final_position(self):
        with self.assertRaises(ParseError):
            parse_source(harness.source(PARSER, "tail_expr_mid"))

    def test_empty_for_in_body_is_accepted(self):
        source = harness.source(PARSER, "for_empty_body")
        result = parse_with_errors(tokenize_source(source))
        self.assertEqual(result.errors, [])

    def test_let_mutability_syntax(self):
        immutable = stmt_from_file("let_immutable")
        mutable = stmt_from_file("let_mutable")
        self.assertFalse(immutable.mutable)
        self.assertIsInstance(mutable, LetStmt)
        self.assertTrue(mutable.mutable)

    def test_break_continue(self):
        st = stmt_from_file("break_stmt")
        self.assertIsInstance(st, BreakStmt)
        st = stmt_from_file("continue_stmt")
        self.assertIsInstance(st, ContinueStmt)

        # inside loop bodies they parse to the same nodes
        st = stmt_from_file("while_break")
        self.assertIsInstance(st, WhileStmt)
        self.assertIsInstance(st.body.stmts[0], BreakStmt)
        st = stmt_from_file("for_continue")
        self.assertIsInstance(st, ForStmt)
        self.assertIsInstance(st.body.stmts[0], ContinueStmt)

    def test_break_continue_require_semicolon(self):
        with self.assertRaises(ParseError):
            stmt_from_file("bare_break")
        with self.assertRaises(ParseError):
            stmt_from_file("bare_continue")

    def test_if_elif_else(self):
        st = stmt_from_file("if_elif_else")
        self.assertIsInstance(st, IfStmt)
        self.assertEqual(len(st.elifs), 2)
        self.assertIsNotNone(st.else_)

    def test_while(self):
        st = stmt_from_file("while_stmt")
        self.assertIsInstance(st, WhileStmt)
        self.assertIsInstance(st.cond, BinOp)

    def test_for_in_forms(self):
        st = stmt_from_file("for_bare_in")
        self.assertIsInstance(st, ForStmt)
        self.assertEqual(st.var, "word")
        self.assertIsNone(st.type)
        self.assertFalse(st.paren_style)

        st = stmt_from_file("for_paren")
        self.assertIsNone(st.type)
        self.assertTrue(st.paren_style)

        st = stmt_from_file("for_typed_paren")
        self.assertEqual(st.type.name, "Tuple")
        self.assertTrue(st.paren_style)


class TestDeclarations(unittest.TestCase):
    def test_const(self):
        item = prog("const_decl").items[0]
        self.assertIsInstance(item, ConstDecl)
        self.assertEqual(item.name, "hello")

    def test_type_decl(self):
        item = prog("type_decl_where").items[0]
        self.assertIsInstance(item, TypeDecl)
        self.assertEqual(item.base.name, "String")
        self.assertIsNotNone(item.where)

    def test_typedef(self):
        parsed = prog("typedefs")
        self.assertEqual(parsed.items[0].base.name, "Map")
        self.assertEqual([p.name for p in parsed.items[0].params], ["K", "T", "V"])
        self.assertEqual([p.name for p in parsed.items[1].params], ["K", "V"])
        self.assertEqual(parsed.items[2].base.name, "Map")

    def test_typedef_requires_semicolon(self):
        with self.assertRaises(ParseError):
            parse_source(harness.source(PARSER, "typedef_semicolon_required"))

    def test_fn_generic_params(self):
        parsed = prog("fn_generic_params")
        self.assertEqual(parsed.items[0].type_params[0].name, "T")

    def test_param_requires_type_except_self(self):
        with self.assertRaises(ParseError):
            parse_source(harness.source(PARSER, "param_missing_type"))
        parse_source(harness.source(PARSER, "param_self_only"))  # self is exempt
        parsed = parse_source(harness.source(PARSER, "extra_ref_self"))
        self.assertTrue(parsed.items[0].methods[0].params[0].type.ref)
        parse_source(harness.source(PARSER, "ref_vector_param"))

    def test_empty_group_rejected(self):
        with self.assertRaises(ParseError) as cm:
            parse_source(harness.source(PARSER, "empty_group"))
        self.assertIn("group policy cannot be empty", cm.exception.message)

    def test_struct_fields(self):
        item = prog("struct_fields").items[0]
        self.assertIsInstance(item, StructDecl)
        self.assertEqual(len(item.fields), 4)
        self.assertTrue(item.fields[3].static)
        self.assertIsNotNone(item.fields[3].initializer)
        self.assertIsNotNone(item.fields[1].validation)
        self.assertIsNotNone(item.fields[2].validation)

    def test_unit_struct(self):
        parsed = prog("unit_struct")
        self.assertEqual(parsed.items[0].fields, [])
        self.assertEqual(parsed.items[1].fields, [])

    def test_enum(self):
        item = prog("enum_decl").items[0]
        self.assertIsInstance(item, EnumDecl)
        self.assertEqual([v.name for v in item.variants], ["Red", "Green", "Blue"])
        self.assertEqual(item.variants[1].value, 2)

    def test_trait(self):
        item = prog("trait_decl").items[0]
        self.assertIsInstance(item, TraitDecl)
        self.assertTrue(item.pub)
        self.assertEqual(item.methods[0].name, "str")
        self.assertIsNone(item.methods[0].body)

    def test_generic_trait(self):
        item = prog("generic_trait_bound").items[0]
        self.assertIsInstance(item, TraitDecl)
        self.assertEqual(len(item.params), 1)
        self.assertIsInstance(item.params[0], TypeParam)
        self.assertEqual(item.params[0].name, "T")
        self.assertEqual(item.params[0].bound.name, "Into")
        self.assertEqual(item.params[0].bound.args[0].name, "String")

    def test_trait_default_method_body(self):
        item = prog("trait_default_method").items[0]
        self.assertIsNotNone(item.methods[0].body)
        self.assertIsNone(item.methods[1].body)

    def test_impl(self):
        item = prog("impl_decl").items[0]
        self.assertIsInstance(item, ImplDecl)
        self.assertEqual(item.trait.name, "DisplayJson")
        self.assertEqual(item.struct.name, "TestStruct")

    def test_extra(self):
        item = prog("extra_which").items[0]
        self.assertIsInstance(item, ExtraDecl)
        self.assertEqual(item.struct.name, "User")
        self.assertEqual(item.methods[0].static, True)
        self.assertEqual(item.methods[0].which, "new")

    def test_generic_extra(self):
        item = prog("generic_extra").items[1]
        self.assertIsInstance(item, ExtraDecl)
        self.assertEqual(item.params[0].name, "T")
        self.assertEqual(item.struct.name, "Point")
        self.assertEqual(item.struct.args[0].name, "T")

    def test_comma_separated_struct_fields(self):
        parsed = prog("comma_struct_fields")
        self.assertEqual([f.name for f in parsed.items[1].fields], ["a", "b", "c"])

    def test_struct_construct_statement(self):
        parsed = prog("struct_construct_stmt")
        self.assertIsInstance(parsed.items[1].body.stmts[0].expr, StructConstruct)

    def test_group_both_forms(self):
        item = prog("group_param_form").items[0]
        self.assertIsInstance(item, GroupDecl)
        self.assertEqual(len(item.params), 2)
        self.assertEqual(item.distributions[0].subject, "a")

        item = prog("group_struct_form").items[0]
        self.assertIsInstance(item, GroupDecl)
        self.assertEqual(item.struct, "User")
        self.assertTrue(item.distributions[0].subject_self)
        self.assertEqual(item.distributions[0].subject, "email")

    def test_group_apply_without_semicolon(self):
        item = prog("group_apply_no_semi").items[0]
        self.assertIsInstance(item, GroupApply)
        self.assertEqual(item.group, "Dispenser")
        self.assertEqual(item.fields, ["name", "email"])


class TestErrors(harness.CaseAssertionsMixin):
    def test_let_requires_type(self):
        self.assert_case(PARSER, "let_requires_type")

    def test_missing_semicolon(self):
        self.assert_case(PARSER, "missing_semicolon")

    def test_unclosed_block(self):
        self.assert_case(PARSER, "unclosed_block")

    def test_bad_top_level(self):
        self.assert_case(PARSER, "bad_top_level")

    def test_parse_with_errors_collects_many(self):
        src = harness.source(PARSER, "collect_many_errors")
        result = parse_with_errors(tokenize_source(src))
        self.assertEqual(len(result.errors), 3)
        self.assertTrue(all("let needs a type" in e.message for e in result.errors))
        # recovery keeps the surrounding structure
        self.assertEqual(result.program.items[0].__class__.__name__, "FnDecl")

    def test_recovery_continues_after_item_error(self):
        src = harness.source(PARSER, "recovery_after_item_error")
        result = parse_with_errors(tokenize_source(src))
        self.assertEqual(len(result.errors), 1)
        kinds = [type(i).__name__ for i in result.program.items]
        self.assertIn("FnDecl", kinds)  # `fn ok` still parsed

    def test_map_missing_colon_recovers_without_escaping_block(self):
        # `{ "a" 1 }` must report the missing ':' at the right token and then
        # finish the statement cleanly; the `;` must not leak out as a bogus
        # top-level error.
        src = harness.source(PARSER, "map_missing_colon_recovery")
        result = parse_with_errors(tokenize_source(src))
        self.assertEqual(len(result.errors), 1)
        err = result.errors[0]
        self.assertIn("':' between map key and value", err.message)
        self.assertEqual(src.index("1", src.index("{ \"a\" 1 }")) + 1, err.column)

    def test_map_multiple_missing_colons_collected(self):
        self.assert_case(PARSER, "map_multiple_missing_colons")

    def test_struct_construct_missing_comma_recovers(self):
        self.assert_case(PARSER, "struct_construct_missing_comma")


FAIL_CASES_DIR = TESTS.parents[2] / "assets" / "parser_fail_cases"


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
    def test_compact_program_parses(self):
        parsed = parse_source(
            harness.source("common", "compact_program")
        )
        self.assertIsInstance(parsed, Program)
        self.assertGreater(len(parsed.items), 8)
        kinds = [type(i).__name__ for i in parsed.items]
        for expected in ["ConstDecl", "TypeDecl", "StructDecl", "EnumDecl", "TraitDecl",
                         "ImplDecl", "ExtraDecl", "GroupDecl", "GroupApply", "FnDecl"]:
            self.assertIn(expected, kinds)

    def test_bool_literal_case_matrix_parses(self):
        # lowercase literals parse as BoolLit; capital `True` stays a Name
        st = stmt_from_file("bool_true")
        self.assertIsInstance(st.value, BoolLit)
        st = stmt_from_file("bool_true_caps")
        self.assertIsInstance(st.value, Name)

    def test_ast_to_json(self):
        parsed = prog("ast_to_json_input")
        data = parsed.to_dict()
        self.assertEqual(data["kind"], "Program")
        self.assertEqual(data["items"][0]["kind"], "ConstDecl")
        json.dumps(data)  # must be JSON-serializable


class TestPatternMatching(unittest.TestCase):
    def test_match_stmt_parse(self):
        st = stmt_from_file("match_lit_wildcard")
        self.assertIsInstance(st, MatchStmt)
        self.assertIsInstance(st.subject, Name)
        self.assertEqual(len(st.arms), 2)
        arm0 = st.arms[0]
        self.assertIsInstance(arm0.pattern, LitPattern)
        self.assertIsInstance(arm0.pattern.value, IntLit)
        self.assertIsNone(arm0.guard)
        self.assertIsInstance(st.arms[1].pattern, WildcardPattern)

    def test_match_guard(self):
        st = stmt_from_file("match_guard_bind")
        arm = st.arms[0]
        self.assertIsInstance(arm.pattern, BindPattern)
        self.assertIsInstance(arm.guard, BinOp)

    def test_tuple_pattern(self):
        st = stmt_from_file("match_tuple_pattern")
        pat = st.arms[0].pattern
        self.assertIsInstance(pat, TuplePattern)
        self.assertEqual(len(pat.elems), 2)
        self.assertIsInstance(pat.elems[0], LitPattern)
        self.assertIsInstance(pat.elems[1], BindPattern)

    def test_struct_pattern(self):
        st = stmt_from_file("match_struct_pattern")
        pat = st.arms[0].pattern
        self.assertIsInstance(pat, StructPattern)
        self.assertEqual(pat.type.name, "Point")
        self.assertEqual(len(pat.fields), 2)
        self.assertIsNone(pat.fields[0].pattern)
        self.assertIsInstance(pat.fields[1].pattern, LitPattern)
        self.assertTrue(pat.rest)

    def test_empty_tuple_pattern(self):
        st = stmt_from_file("match_empty_tuple")
        self.assertEqual(st.arms[0].pattern.elems, [])

    def test_if_let(self):
        st = stmt_from_file("if_let_struct")
        self.assertIsInstance(st, IfLetStmt)
        self.assertIsInstance(st.pattern, StructPattern)
        self.assertIsInstance(st.value, Name)
        self.assertIsNotNone(st.else_)

    def test_if_let_elif_chain(self):
        st = stmt_from_file("if_let_elif_chain")
        self.assertIsInstance(st, IfLetStmt)
        self.assertEqual(len(st.elifs), 2)
        self.assertIsNotNone(st.elifs[0].cond)
        self.assertIsNone(st.elifs[0].pattern)
        self.assertIsNone(st.elifs[1].cond)
        self.assertIsInstance(st.elifs[1].pattern, LitPattern)

    def test_match_requires_fat_arrow(self):
        with self.assertRaises(ParseError) as cm:
            stmt_from_file("match_missing_arrow")
        self.assertIn(
            "'=>' between match pattern and body", cm.exception.message
        )

    def test_enum_variant_pattern_parses(self):
        st = stmt_from_file("match_enum_variant")
        self.assertIsInstance(st.arms[0].pattern, EnumPattern)

    def test_match_expression_arms(self):
        st = stmt_from_file("match_as_expression")
        value = st.value
        self.assertIsInstance(value, MatchStmt)
        self.assertNotIsInstance(value.arms[0].body, Block)
        self.assertIsInstance(value.arms[0].body, Name)
        self.assertIsInstance(value.arms[1].body, UnaryOp)
        self.assertEqual(value.arms[1].body.op, TokenKind.MINUS)

    def test_match_expression_with_guard(self):
        st = stmt_from_file("match_expr_guard")
        self.assertIsInstance(st.value, MatchStmt)
        arm = st.value.arms[0]
        self.assertIsInstance(arm.guard, BinOp)
        self.assertIsInstance(arm.body, Name)

    def test_match_is_a_keyword(self):
        with self.assertRaises(ParseError):
            parse_source(harness.source(PARSER, "fn_named_match"))

    def test_never_return_type(self):
        fn = prog("never_return_fn").items[0]
        self.assertIsInstance(fn, FnDecl)
        self.assertIsNotNone(fn.return_type)
        self.assertEqual(fn.return_type.name, "!")


class TestEnumSyntax(unittest.TestCase):
    def test_enum_with_generic_params_and_payloads(self):
        parsed = prog("enum_generic_payloads")
        self.assertEqual(len(parsed.items), 2)
        enum = parsed.items[0]
        self.assertIsInstance(enum, EnumDecl)
        self.assertEqual([p.name for p in enum.params], ["T"])
        self.assertEqual(len(enum.variants), 2)
        some, none = enum.variants
        self.assertEqual([t.name for t in some.fields], ["T"])
        self.assertEqual(none.fields, [])

    def test_enum_variant_pattern(self):
        st = stmt_from_file("match_option_payload")
        pat = st.arms[0].pattern
        self.assertIsInstance(pat, EnumPattern)
        self.assertEqual(pat.path, ["Option", "Some"])
        self.assertEqual(len(pat.elems), 1)
        self.assertIsInstance(pat.elems[0], BindPattern)
        unit = st.arms[1].pattern
        self.assertIsInstance(unit, EnumPattern)
        self.assertEqual(unit.path, ["Option", "None"])
        self.assertEqual(unit.elems, [])

    def test_cstyle_enum_variant_pattern(self):
        st = stmt_from_file("match_enum_variant")
        pat = st.arms[0].pattern
        self.assertIsInstance(pat, EnumPattern)
        self.assertEqual(pat.path, ["Color", "Red"])

    def test_trait_associated_type(self):
        trait = prog("trait_assoc_type").items[0]
        self.assertIsInstance(trait, TraitDecl)
        self.assertEqual(trait.assoc_types, ["Item"])
        self.assertEqual(len(trait.methods), 1)
        ret = trait.methods[0].return_type
        self.assertEqual(ret.name, "Option")
        self.assertEqual(ret.args[0].name, "Self::Item")

    def test_impl_associated_type_binding(self):
        impl = prog("impl_assoc_binding").items[2]
        self.assertIsInstance(impl, ImplDecl)
        self.assertEqual(len(impl.assoc_types), 1)
        self.assertIsInstance(impl.assoc_types[0], AssocType)
        self.assertEqual(impl.assoc_types[0].name, "Item")
        self.assertEqual(impl.assoc_types[0].type.name, "Int32")


if __name__ == "__main__":
    unittest.main()
