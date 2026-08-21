"""Unit tests for cwind_frontend.sa."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cwind_frontend import SaError, parse_source, run_sa, run_sa_with_errors
from cwind_frontend.ast_components import ast as A


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


class TestSa(unittest.TestCase):
    def test_collect_symbols(self):
        prog = parse_source(
            "const a: Int = 1; struct S { pub x: Int; } fn f() -> None {}"
        )
        info = run_sa(prog)
        names = {s.name for s in info.symbols.values()}
        self.assertEqual(names, {"a", "S", "f"})
        self.assertEqual(info.symbols["S"].kind, "struct")

    @staticmethod
    def _find_first(prog, kind):
        found = []

        def walk(node):
            if found:
                return
            if isinstance(node, kind):
                found.append(node)
                return
            for attr in (
                "items", "stmts", "value", "left", "right", "operand",
                "expr", "body", "then", "else_", "elifs", "args", "elems",
                "subject", "arms", "pattern", "guard",
            ):
                v = getattr(node, attr, None)
                if isinstance(v, list):
                    for x in v:
                        walk(x)
                elif v is not None:
                    walk(v)

        walk(prog)
        return found[0] if found else None

    @staticmethod
    def _find_all(prog, kind):
        found = []

        def walk(node):
            if isinstance(node, kind):
                found.append(node)
            for attr in (
                "items", "stmts", "value", "left", "right", "operand",
                "expr", "body", "then", "else_", "elifs", "args", "elems",
            ):
                v = getattr(node, attr, None)
                if isinstance(v, list):
                    for x in v:
                        walk(x)
                elif v is not None:
                    walk(v)

        walk(prog)
        return found

    def test_new_int_widths_accept(self):
        src = (
            "fn f() -> None {"
            " let a: Int32 = 2147483647;"
            " let b: UInt32 = 4294967295;"
            " let c: Int64 = -9223372036854775808;"
            " let d: UInt64 = 18446744073709551615;"
            " let e: Float64 = 1.5;"
            "}"
        )
        self.assertEqual(run_sa_with_errors(parse_source(src)).errors, [])

    def test_new_int_widths_reject_overflow(self):
        cases = [
            ("Int32", "2147483648"),
            ("UInt32", "4294967296"),
            ("Int64", "9223372036854775808"),
            ("UInt64", "18446744073709551616"),
            ("UInt64", "-1"),
        ]
        for t, lit in cases:
            with self.subTest(t=t, lit=lit):
                result = run_sa_with_errors(
                    parse_source(f"fn f() -> None {{ let x: {t} = {lit}; }}")
                )
                self.assertTrue(
                    any(f"does not fit in {t}" in e.message for e in result.errors)
                )

    def test_float64_range(self):
        huge = "1" + "0" * 309  # ~1e309 > f64 max
        result = run_sa_with_errors(
            parse_source(f"fn f() -> None {{ let x: Float64 = {huge}; }}")
        )
        self.assertTrue(
            any("does not fit in Float64" in e.message for e in result.errors)
        )

    def test_mixed_numeric_compare(self):
        cases = [
            "fn f() -> Bool { return 2.0 > 1; }",
            "fn f() -> Bool { return 1 == 1.0; }",
            "fn f() -> Bool { let x: Int8 = 1; let y: Float64 = 2.5; return x < y; }",
            "fn f() -> Bool { let x: UInt32 = 3; let y: Int64 = 4; return x <= y; }",
        ]
        for src in cases:
            with self.subTest(src=src):
                self.assertEqual(run_sa_with_errors(parse_source(src)).errors, [])

    def test_mixed_arith_result_widens(self):
        cases = [
            ("Int32", "Int64", "Int64"),
            ("UInt32", "Int32", "Int64"),
            ("Int8", "Int32", "Int32"),
            ("UInt8", "UInt8", "UInt8"),
            ("Int8", "Byte", "Int"),
            ("UInt8", "Byte", "UInt8"),
            ("Float", "Float64", "Float64"),
            ("Float", "Int", "Float"),
        ]
        for lt, rt, want in cases:
            with self.subTest(lt=lt, rt=rt):
                prog = parse_source(
                    f"fn f() -> {want} {{"
                    f" let a: {lt} = 1;"
                    f" let b: {rt} = 2;"
                    " return a + b;"
                    "}"
                )
                run_sa(prog)
                node = self._find_first(prog, (A.BinOp,))
                self.assertIsNotNone(node)
                self.assertEqual(node._typed_ann["type"]["name"], want)

    def test_bitwise_result_widens(self):
        prog = parse_source(
            "fn f() -> Int64 { let a: Int64 = 1; let b: Int64 = 2; return a & b; }"
        )
        run_sa(prog)
        node = self._find_first(prog, (A.BinOp,))
        self.assertEqual(node._typed_ann["type"]["name"], "Int64")

    def test_float_exactness_rejected(self):
        cases = [
            ("Float", "16777216 + 1", "Float"),
            ("Float64", "9007199254740993", "Float64"),
        ]
        for t, lit, msg_t in cases:
            with self.subTest(t=t, lit=lit):
                result = run_sa_with_errors(
                    parse_source(f"fn f() -> None {{ let x: {t} = {lit}; }}")
                )
                self.assertTrue(
                    any(
                        f"is not exactly representable in {msg_t}" in e.message
                        for e in result.errors
                    ),
                    f"expected exactness error for {t} = {lit}",
                )

    def test_duplicate_definition(self):
        with self.assertRaises(SaError) as cm:
            run_sa(parse_source("struct A {} struct A {}"))
        self.assertIn("duplicate definition", cm.exception.message)

    def test_unknown_type(self):
        with self.assertRaises(SaError) as cm:
            run_sa(parse_source("struct A { pub x: Missing; }"))
        self.assertIn("unknown type", cm.exception.message)

    def test_builtin_redefinition(self):
        with self.assertRaises(SaError) as cm:
            run_sa(parse_source("type String = Int"))
        self.assertIn("redefines a built-in type", cm.exception.message)

    def test_group_apply_references(self):
        with self.assertRaises(SaError):
            run_sa(parse_source("G@S -> {f}"))
        with self.assertRaises(SaError):
            run_sa(parse_source("group G { x -> Int; } G@S -> {x}"))

    def test_group_binding_validates_self_field(self):
        ok = parse_source(
            "type Email = String where { self.length >= 3; }"
            "struct User { pub email: Email, }"
            "group StrictUser: User { self.email -> Email; }"
        )
        self.assertEqual(run_sa_with_errors(ok).errors, [])

        bad = parse_source(
            "type Email = String where { self.length >= 3; }"
            "struct User { pub email: Email, }"
            "group Bad: User { self.nope -> Email; }"
        )
        result = run_sa_with_errors(bad)
        self.assertTrue(
            any("'User' has no field 'nope'" in e.message for e in result.errors)
        )

    def test_impl_references(self):
        with self.assertRaises(SaError):
            run_sa(parse_source("impl Trait for Struct {}"))

    def test_run_sa_collects_many(self):
        prog = parse_source(
            "struct A { pub x: Missing; } struct B { pub y: AlsoMissing; }"
        )
        result = run_sa_with_errors(prog)
        self.assertEqual(len(result.errors), 2)
        self.assertTrue(all("unknown type" in e.message for e in result.errors))

    def test_unknown_identifier_in_body(self):
        result = run_sa_with_errors(parse_source("fn f() -> Int { return x; }"))
        self.assertEqual(len(result.errors), 1)
        self.assertIn("unknown identifier 'x'", result.errors[0].message)

    def test_return_type_mismatch(self):
        result = run_sa_with_errors(parse_source('fn f() -> Int { return "s"; }'))
        self.assertEqual(len(result.errors), 1)
        self.assertIn("return type mismatch", result.errors[0].message)

    def test_condition_must_be_bool(self):
        result = run_sa_with_errors(parse_source("fn f() -> None { if (1) { } }"))
        self.assertEqual(len(result.errors), 1)
        self.assertIn("condition must be Bool", result.errors[0].message)

    def test_break_continue_inside_loops(self):
        src = (
            "fn f() -> None {\n"
            "    let i: Int = 0;\n"
            "    while (i < 5) {\n"
            "        if (i == 3) { break; }\n"
            "        i += 1;\n"
            "        continue;\n"
            "    }\n"
            "    for (x: arr) {\n"
            "        if (x == 0) { continue; }\n"
            "        break;\n"
            "    }\n"
            "}\n"
        )
        # `arr` is an unknown identifier but the loop control is valid.
        messages = [e.message for e in run_sa_with_errors(parse_source(src)).errors]
        self.assertTrue(all("inside a loop" not in m for m in messages))

    def test_break_continue_outside_loops(self):
        result = run_sa_with_errors(parse_source("fn f() -> None { break; }"))
        self.assertEqual(len(result.errors), 1)
        self.assertIn("'break' can only be used inside a loop", result.errors[0].message)

        result = run_sa_with_errors(parse_source("fn f() -> None { continue; }"))
        self.assertEqual(len(result.errors), 1)
        self.assertIn("'continue' can only be used inside a loop", result.errors[0].message)

    def test_break_continue_do_not_escape_inner_loop(self):
        # a break inside a nested loop is legal even though the outer block
        # is not a loop itself
        src = (
            "fn f() -> None {\n"
            "    while (true) {\n"
            "        if (true) { break; }\n"
            "    }\n"
            "}\n"
        )
        self.assertEqual(run_sa_with_errors(parse_source(src)).errors, [])

    def test_arity_mismatch(self):
        result = run_sa_with_errors(
            parse_source("fn f(a: Int) -> None {} fn g() -> None { f(1, 2); }")
        )
        self.assertEqual(len(result.errors), 1)
        self.assertIn("expects 1 argument", result.errors[0].message)

    def test_unknown_method_on_builtin(self):
        result = run_sa_with_errors(
            parse_source('fn f() -> None { let s: String = "x"; s.nope(); }')
        )
        self.assertEqual(len(result.errors), 1)
        self.assertIn("no method 'nope'", result.errors[0].message)

    def test_builtin_methods_resolve(self):
        prog = parse_source(
            'fn f() -> Bool { let s: String = "x";'
            " return s.matches(\"y\") && s.length == 1; }"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])

    def test_duplicate_let_in_scope(self):
        result = run_sa_with_errors(
            parse_source("fn f() -> None { let x: Int = 1; let x: Int = 2; }")
        )
        self.assertEqual(len(result.errors), 1)
        self.assertIn("duplicate definition", result.errors[0].message)

    def test_struct_field_and_method(self):
        prog = parse_source(
            "struct S { pub v: Int; }"
            "extra S { pub fn get(self) -> Int { return self.v; } }"
            "fn f() -> Int { let s: S = S { 1 }; return s.get(); }"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])

    def test_impl_for_builtin_type(self):
        prog = parse_source(
            "pub trait DisplayJson { fn str(self) -> String; }"
            'impl DisplayJson for Map<String, String> { fn str(self) -> String { return "".format(); } }'
            "fn f(m: Map<String, String>) -> String { return m.str(); }"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])

    def test_builtin_trait_impl(self):
        prog = parse_source(
            "struct S { pub v: Int; }"
            "impl Display for S { pub fn to_string(&self) -> String { return self.v.to_string(); } }"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])

    def test_format_rejects_placeholder_arity_mismatch(self):
        prog = parse_source(
            'fn f() -> String { let s: String = "{}".format(1, 2, 3); return s; }'
        )
        result = run_sa_with_errors(prog)
        self.assertTrue(
            any(
                "format string has 1 placeholder(s) but got 3 argument(s)"
                in e.message
                for e in result.errors
            )
        )

    def test_format_brace_balance_ok(self):
        for src in (
            'fn f() -> String { return "a={} b={}".format(1, 2); }',
            'fn f() -> String { return "x=\\{y\\} and {}".format(1); }',
        ):
            self.assertEqual(run_sa_with_errors(parse_source(src)).errors, [])

    def test_format_unclosed_brace_error(self):
        result = run_sa_with_errors(
            parse_source('fn f() -> String { return "a={ b".format(); }')
        )
        self.assertEqual(len(result.errors), 1)
        self.assertIn(
            "format string has unmatched '{'", result.errors[0].message
        )

    def test_format_stray_brace_error(self):
        result = run_sa_with_errors(
            parse_source('fn f() -> String { return "a=} b".format(); }')
        )
        self.assertEqual(len(result.errors), 1)
        self.assertIn(
            "format string has more '}' than '{'", result.errors[0].message
        )

    def test_display_to_string_on_builtin(self):
        prog = parse_source(
            "fn f(m: Map<String, String>) -> String { return m.to_string(); }"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])

    def test_unknown_type_error_points_at_type_name(self):
        src = "fn f() -> None { let t: bool = true; }"
        result = run_sa_with_errors(parse_source(src))
        errs = [e for e in result.errors if "unknown type 'bool'" in e.message]
        self.assertEqual(len(errs), 1)
        self.assertEqual(errs[0].line, 1)
        self.assertEqual(errs[0].column, src.index("bool") + 1)

    def test_param_type_check_builtin(self):
        result = run_sa_with_errors(
            parse_source('fn f() -> None { let s: String = "x"; s.matches(1); }')
        )
        self.assertEqual(len(result.errors), 1)
        self.assertIn(
            "argument 1 of 'matches' must be String, got Int",
            result.errors[0].message,
        )

    def test_same_as_generic_check(self):
        result = run_sa_with_errors(
            parse_source('fn f() -> None { let v: Vector<Int> = [1]; v.push_back("s"); }')
        )
        self.assertEqual(len(result.errors), 1)
        self.assertIn(
            "argument 1 of 'push_back' must be Int, got String",
            result.errors[0].message,
        )

    def test_same_as_generic_position_check(self):
        bad = run_sa_with_errors(
            parse_source(
                'fn f() -> None { let m: Map<String, Int> = {}; m.set(1, "s"); }'
            )
        )
        messages = [e.message for e in bad.errors]
        self.assertTrue(
            any(
                "argument 1 of 'set' must be String, got Int"
                in m for m in messages
            )
        )
        self.assertTrue(
            any(
                "argument 2 of 'set' must be Int, got String"
                in m for m in messages
            )
        )

        ok = parse_source(
            'fn f() -> None { let m: Map<String, Int> = {}; '
            'm.set("a", 1); let v: Int = m.get("a"); }'
        )
        self.assertEqual(run_sa_with_errors(ok).errors, [])

    def test_builtin_none_object(self):
        prog = parse_source("fn f() -> None { let x: None = None; return None; }")
        self.assertEqual(run_sa_with_errors(prog).errors, [])

        result = run_sa_with_errors(parse_source("fn f() -> None { return None; }"))
        self.assertEqual(result.errors, [])

    def test_user_fn_arg_type(self):
        result = run_sa_with_errors(
            parse_source('fn f(a: Int) -> None {} fn g() -> None { f("s"); }')
        )
        self.assertEqual(len(result.errors), 1)
        self.assertIn(
            "argument 1 of 'f' must be Int, got String",
            result.errors[0].message,
        )

    def test_bool_literal(self):
        prog = parse_source("fn f() -> Bool { return true; }")
        self.assertEqual(run_sa_with_errors(prog).errors, [])

        result = run_sa_with_errors(parse_source("fn f() -> Bool { return True; }"))
        self.assertEqual(len(result.errors), 1)
        self.assertIn("unknown identifier 'True'", result.errors[0].message)

    def test_generic_trait_sa(self):
        prog = parse_source(
            "trait NoSuch<T: Into<String>> {"
            " pub fn great(value: T) -> None { print(value); }"
            "}"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])

    def test_generic_impl_sa(self):
        prog = parse_source(
            "trait Trait<T> { fn f(self, value: T) -> T; }"
            "struct S { }"
            "impl<T: Into<String>> Trait for S {"
            " fn f(self, value: T) -> T { return value; }"
            "}"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])

    def test_generic_impl_on_generic_builtin(self):
        prog = parse_source(
            "trait DisplayJson { fn str(self) -> String; }"
            "impl<K: Into<String>, V: Into<String>> DisplayJson for Map<K, V> {"
            " fn str(self) -> String { return \"\".format(); }"
            "}"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])

    def test_negative_literal_into_unsigned(self):
        result = run_sa_with_errors(parse_source("fn f() -> UInt { return -1; }"))
        self.assertEqual(len(result.errors), 1)
        self.assertIn("value -1 does not fit in UInt", result.errors[0].message)

    def test_literal_out_of_range(self):
        result = run_sa_with_errors(
            parse_source("fn f() -> None { let x: UInt8 = 300; }")
        )
        self.assertEqual(len(result.errors), 1)
        self.assertIn("value 300 does not fit in UInt8", result.errors[0].message)

    def test_const_expression_overflow(self):
        result = run_sa_with_errors(
            parse_source("const A: UInt8 = 255 + 255 + 255;")
        )
        self.assertEqual(len(result.errors), 1)
        self.assertIn("value 765 does not fit in UInt8", result.errors[0].message)

    def test_const_reference_checked_in_return(self):
        result = run_sa_with_errors(
            parse_source("const A: Int = 300; fn f() -> UInt8 { return A; }")
        )
        self.assertTrue(
            any("value 300 does not fit in UInt8" in e.message for e in result.errors)
        )

    def test_generic_args_on_non_generic_struct(self):
        result = run_sa_with_errors(
            parse_source("struct S { } fn f(x: S<String>) -> None { }")
        )
        self.assertTrue(
            any(
                "type 'S' expects 0 generic argument(s), got 1" in e.message
                for e in result.errors
            )
        )

    def test_trait_args_substitution(self):
        prog = parse_source(
            "trait T<X> { fn f(self, value: X) -> X; }"
            "struct S { }"
            "impl T<String> for S { fn f(self, value: String) -> String { return value; } }"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])

    def test_typedef_explicit_generic_params(self):
        prog = parse_source("typedef DoubleMap<K, T, V> = Map<K, Map<T, V>>;")
        self.assertEqual(run_sa_with_errors(prog).errors, [])

    def test_typedef_rejects_implicit_generics(self):
        result = run_sa_with_errors(
            parse_source("typedef DoubleMap = Map<K, Map<T, V>>;")
        )
        messages = [e.message for e in result.errors]
        self.assertTrue(any("unknown type 'K'" in m for m in messages))
        self.assertTrue(any("unknown type 'V'" in m for m in messages))

    def test_typedef_usage_and_arity(self):
        ok = parse_source(
            "typedef DoubleMap<K, T, V> = Map<K, Map<T, V>>;"
            "fn f(m: DoubleMap<String, Int, Float>) -> None { m.contains(\"a\"); }"
        )
        self.assertEqual(run_sa_with_errors(ok).errors, [])

        bad = parse_source(
            "typedef DoubleMap<K, T, V> = Map<K, Map<T, V>>;"
            "fn f(m: DoubleMap<String, Int>) -> None { }"
        )
        result = run_sa_with_errors(bad)
        self.assertTrue(
            any("expects 3 generic argument(s), got 2" in e.message for e in result.errors)
        )

    def test_typedef_method_resolution(self):
        prog = parse_source(
            "typedef DoubleMap<K, T, V> = Map<K, Map<T, V>>;"
            "fn f(m: DoubleMap<String, Int, Float>) -> Bool {"
            "  return m.contains(\"a\");"
            "}"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])

    def test_typedef_explicit_params(self):
        prog = parse_source(
            "typedef Foo<K, V> = Map<K, V>;"
            "fn f(m: Foo<String, Int>) -> None { m.entry(); }"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])

    def test_typedef_concrete_alias_and_literal(self):
        prog = parse_source(
            "typedef MyMap = Map<String, Int>;"
            "fn f() -> None {"
            "  let a: MyMap = {};"
            "  let b: Map<String, Int> = a;"
            "}"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])

    def test_typedef_in_collections_and_params(self):
        prog = parse_source(
            "typedef StringVec = Vector<String>;"
            "fn f(v: StringVec) -> String {"
            "  for (s: v) { print(s); }"
            "  return v[0];"
            "}"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])

    def test_struct_construct_field_type_mismatch(self):
        prog = parse_source(
            "struct TestStruct { pub data: Map<String, String>, }"
            "fn f() -> None {"
            "  let data: Map<String, Int> = {};"
            "  let ts: TestStruct = TestStruct { data };"
            "}"
        )
        result = run_sa_with_errors(prog)
        self.assertTrue(
            any(
                "field 1 'data' of 'TestStruct' expects Map<String, String>, "
                "got Map<String, Int>" in e.message
                for e in result.errors
            )
        )

    def test_struct_construct_field_type_ok(self):
        prog = parse_source(
            "struct TestStruct { pub data: Map<String, String>, }"
            "fn f() -> None {"
            "  let data: Map<String, String> = {};"
            "  let ts: TestStruct = TestStruct { data };"
            "}"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])

    def test_struct_construct_positional_count(self):
        prog = parse_source(
            "struct P { x: Int, y: Int }"
            "fn f() -> None { let p: P = P { 1 }; }"
        )
        result = run_sa_with_errors(prog)
        self.assertTrue(
            any("expects 2 field value(s), got 1" in e.message for e in result.errors)
        )

    def test_alias_arg_to_function(self):
        prog = parse_source(
            "typedef MapSI = Map<String, Int>;"
            "fn collections(m: Map<String, Int>) -> None { }"
            "fn f() -> None { let data: MapSI = {}; collections(data); }"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])

    def test_alias_struct_construct_matching(self):
        prog = parse_source(
            "typedef MapSI = Map<String, Int>;"
            "struct S { pub m: Map<String, Int>, }"
            "fn f() -> None { let m: MapSI = {}; let s: S = S { m }; }"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])

    def test_alias_mismatch_message_shows_expansion(self):
        prog = parse_source(
            "typedef MapSI = Map<String, Int>;"
            "struct S { pub m: Map<String, String>, }"
            "fn f() -> None { let m: MapSI = {}; let s: S = S { m }; }"
        )
        result = run_sa_with_errors(prog)
        self.assertTrue(
            any("got MapSI (Map<String, Int>)" in e.message for e in result.errors)
        )

    def test_alias_to_numeric_and_bool(self):
        prog = parse_source(
            "typedef Age = Int;"
            "typedef Flag = Bool;"
            "fn f() -> Bool {"
            "  let age: Age = 30;"
            "  let flag: Flag = true;"
            "  return age > 0 && flag;"
            "}"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])

    def test_fn_generic_params_sa(self):
        prog = parse_source("fn test<T>() { let s: Map<T, String> = {}; }")
        self.assertEqual(run_sa_with_errors(prog).errors, [])

    def test_uninitialized_variable_use(self):
        bad = parse_source("fn f() -> None { let x: Int; let y: Int = x; }")
        result = run_sa_with_errors(bad)
        self.assertTrue(
            any("used before assignment" in e.message for e in result.errors)
        )
        ok = parse_source("fn f() -> None { let x: Int; x = 1; let y: Int = x; }")
        self.assertEqual(run_sa_with_errors(ok).errors, [])

    def test_uninitialized_use_and_missing_return(self):
        # edge_part2 p01_1: an uninitialized read and a declared return type
        # with no `return` must both be reported.
        prog = parse_source(
            "fn p01_1() -> Int {\n"
            "    let v: Int;\n"
            "    print(v);\n"
            "}\n"
        )
        result = run_sa_with_errors(prog)
        messages = [e.message for e in result.errors]
        self.assertTrue(any("used before assignment" in m for m in messages))
        self.assertTrue(any("must return a value" in m for m in messages))

    def test_builtin_static_constructor_new(self):
        # Variadic `new` with values is temporarily not allowed (see the
        # comment in builtin_methods.toml): only the zero-argument form is
        # part of the current behavior.
        prog = parse_source(
            'fn f() -> None { let v: Vector<Int> = Vector::new(); }'
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])
        prog = parse_source(
            'fn f() -> None { let s: Set<Int> = Set::new(); }'
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])
        prog = parse_source(
            'fn f() -> None { let m: Map<String, Int> = Map::new(); }'
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])
        for call in ("Vector::new(1, 2, 3)", "Set::new(1, 2)", 'Map::new("a", 1)'):
            result = run_sa_with_errors(
                parse_source(f"fn f() -> None {{ let v: Vector<Int> = {call}; }}")
            )
            self.assertTrue(
                any("expects 0 argument(s)" in e.message for e in result.errors),
                call,
            )

    def test_builtin_instance_method_not_callable_statically(self):
        result = run_sa_with_errors(
            parse_source('fn f() -> None { String::to_string(); }')
        )
        self.assertEqual(len(result.errors), 1)
        self.assertIn("must be called on a value", result.errors[0].message)

    def test_string_plus_number_rejected(self):
        result = run_sa_with_errors(
            parse_source('fn f() -> String { return "x" + 1; }')
        )
        self.assertTrue(
            any("cannot add String and Int" in e.message for e in result.errors)
        )
        ok = parse_source('fn f() -> String { return "x" + "y"; }')
        self.assertEqual(run_sa_with_errors(ok).errors, [])

    def test_enum_path_and_equality(self):
        prog = parse_source(
            "enum Grade { A = 1, B = 2 }"
            "fn f() -> Bool { let g: Grade = Grade::A; return g == Grade::B; }"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])
        result = run_sa_with_errors(
            parse_source("enum Grade { A = 1 } fn f() -> Int { return Grade::A; }")
        )
        self.assertTrue(
            any("return type mismatch" in e.message for e in result.errors)
        )

    def test_which_validation(self):
        prog = parse_source(
            "struct S { }"
            "extra S {"
            " fn growth(self) -> None { }"
            " fn bad_hook(a: Int) -> None, which ::growth { }"
            " fn bad_target(self) -> None, which ::nope { }"
            "}"
        )
        result = run_sa_with_errors(prog)
        self.assertTrue(
            any(
                "must take exactly one self parameter" in e.message
                for e in result.errors
            )
        )
        self.assertTrue(
            any("which target 'nope' does not exist" in e.message for e in result.errors)
        )

    def test_which_restrictions(self):
        self_hook = parse_source(
            "struct S { }"
            "extra S { fn loop(self) -> None, which ::loop { } }"
        )
        errors = run_sa_with_errors(self_hook).errors
        self.assertTrue(
            any("cannot hook itself" in e.message for e in errors)
        )

        chain = parse_source(
            "struct S { }"
            "extra S {"
            " fn a(self) -> None { }"
            " fn b(self) -> None, which ::a { }"
            " fn c(self) -> None, which ::b { }"
            "}"
        )
        errors = run_sa_with_errors(chain).errors
        self.assertTrue(
            any("is itself a which hook" in e.message for e in errors)
        )

        duplicate = parse_source(
            "struct S { }"
            "extra S {"
            " fn a(self) -> None { }"
            " fn h1(self) -> None, which ::a { }"
            " fn h2(self) -> None, which ::a { }"
            "}"
        )
        errors = run_sa_with_errors(duplicate).errors
        self.assertTrue(
            any("already has a which hook" in e.message for e in errors)
        )

        cross_block = parse_source(
            "struct S { }"
            "extra S { fn a(self) -> None { } }"
            "extra S { fn h(self) -> None, which ::a { } }"
        )
        errors = run_sa_with_errors(cross_block).errors
        self.assertTrue(
            any("same 'S' block" in e.message for e in errors)
        )

    def test_static_access_rules(self):
        prog = parse_source(
            "struct S { static count: Int = 0, }"
            "extra S { static fn bump() -> None { } }"
            "fn f(s: S) -> Int { s.count; s.bump(); return S::count; }"
        )
        result = run_sa_with_errors(prog)
        self.assertTrue(
            any("static field 'count' must be accessed via 'S::count'" in e.message
                for e in result.errors)
        )
        self.assertTrue(
            any("static method 'bump' must be called via 'S::bump'" in e.message
                for e in result.errors)
        )

    def test_generic_bounds(self):
        prog = parse_source(
            "trait Named { fn name(self) -> String; }"
            "struct Point2D { pub x: Int, pub y: Int, }"
            "impl Named for Point2D { fn name(self) -> String { return \"p\"; } }"
            "struct bounded<T: Named> { pub v: T, }"
            "fn g(a: bounded<Int>, b: bounded<Point2D>) -> None { }"
        )
        result = run_sa_with_errors(prog)
        self.assertTrue(
            any("type 'Int' does not satisfy bound 'Named'" in e.message
                for e in result.errors)
        )
        self.assertFalse(
            any("does not satisfy bound" in e.message and "Point2D" in e.message
                for e in result.errors)
        )

    def test_extra_generic_mismatch(self):
        result = run_sa_with_errors(
            parse_source("struct P { } extra<T> P { }")
        )
        self.assertTrue(
            any("extra generic parameters" in e.message for e in result.errors)
        )

    def test_duplicate_impl_and_field(self):
        result = run_sa_with_errors(
            parse_source(
                "trait T { fn f(self) -> Int; }"
                "struct S { pub a: Int, pub a: Int, }"
                "impl T for S { fn f(self) -> Int { return 1; } }"
                "impl T for S { fn f(self) -> Int { return 2; } }"
            )
        )
        self.assertTrue(
            any("duplicate field 'a'" in e.message for e in result.errors)
        )
        self.assertTrue(
            any("duplicate impl of trait 'T'" in e.message for e in result.errors)
        )

    def test_const_reassignment_and_div_zero(self):
        result = run_sa_with_errors(
            parse_source(
                "const limit: Int = 100;"
                "fn f() -> Int { limit = 1; return limit; }"
            )
        )
        self.assertTrue(
            any("cannot assign to const 'limit'" in e.message for e in result.errors)
        )
        result = run_sa_with_errors(parse_source("const bad: Int = 1 / 0;"))
        self.assertTrue(
            any("division by zero in constant expression" in e.message
                for e in result.errors)
        )

    def test_equality_type_mismatch(self):
        result = run_sa_with_errors(
            parse_source('fn f() -> Bool { return 1 == "1"; }')
        )
        self.assertTrue(
            any("cannot compare Int with String" in e.message for e in result.errors)
        )

    def test_map_literal_value_types(self):
        bad = parse_source(
            'fn f() -> None { let m: Map<String, Int> = { "a": "b" }; }'
        )
        result = run_sa_with_errors(bad)
        self.assertTrue(
            any("cannot initialize Map<String, Int> with Map<String, String>"
                in e.message for e in result.errors)
        )
        ok = parse_source(
            'fn f() -> None { let m: Map<String, Int> = { "a": 1 }; }'
        )
        self.assertEqual(run_sa_with_errors(ok).errors, [])

    def test_map_literal_duplicate_key_rejected(self):
        result = run_sa_with_errors(parse_source(
            'fn f() -> None { let m: Map<String, Int> = { "a": 1, "a": 2 }; }'
        ))
        self.assertTrue(
            any(
                'duplicate key "a" in map literal' in e.message
                for e in result.errors
            )
        )

        result = run_sa_with_errors(parse_source(
            'fn f() -> None { let m: Map<Int, Int> = { 1: 1, 1: 2 }; }'
        ))
        self.assertTrue(
            any(
                "duplicate key 1 in map literal" in e.message
                for e in result.errors
            )
        )

        result = run_sa_with_errors(parse_source(
            'const K: String = "x";'
            'fn f() -> None { let m: Map<String, Int> = { K: 1, "x": 2 }; }'
        ))
        self.assertTrue(
            any(
                'duplicate key "x" in map literal' in e.message
                for e in result.errors
            )
        )

    def test_container_literal_type_propagation(self):
        result = run_sa_with_errors(parse_source(
            "const V: Vector<UInt8> = [0.1, -1, 513];"
        ))
        messages = [e.message for e in result.errors]
        self.assertTrue(
            any(
                "value 0.1 is not an integer and does not fit in UInt8"
                in m for m in messages
            )
        )
        self.assertTrue(
            any("value -1 does not fit in UInt8" in m for m in messages)
        )
        self.assertTrue(
            any("value 513 does not fit in UInt8" in m for m in messages)
        )

        result = run_sa_with_errors(parse_source(
            'const M: Map<String, Vector<UInt8>> = { "x": [0.1, -1, 513] };'
        ))
        self.assertEqual(len(result.errors), 3)

        ok = parse_source(
            'const M: Map<String, Vector<UInt8>> = { "x": [1, 2] };'
        )
        self.assertEqual(run_sa_with_errors(ok).errors, [])

    def test_instance_removed(self):
        result = run_sa_with_errors(
            parse_source("fn f() -> None { let o: Instance = 1; }")
        )
        self.assertTrue(
            any("unknown type 'Instance'" in e.message for e in result.errors)
        )
        ok = parse_source(
            "struct Instance { pub x: Int, }"
            "fn f() -> None { let o: Instance = Instance { 1 }; }"
        )
        self.assertEqual(run_sa_with_errors(ok).errors, [])

    def test_generic_struct_construct_sa(self):
        prog = parse_source(
            "struct Node<T> { pub k: UInt, pub v: T }"
            "fn f<T>(k: UInt, v: T) -> Node<T> { return Node<T> { k, v }; }"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])

        heap = parse_source(
            "struct MaxHeap<T> { max_heap: Vector<T>, capacity: UInt, size: UInt }"
            "extra<T> MaxHeap<T> {"
            " fn new(capacity: UInt) -> Self {"
            "   return MaxHeap<T> { Vector::new(), capacity, 0 };"
            " }"
            "}"
        )
        self.assertEqual(run_sa_with_errors(heap).errors, [])

    def test_map_requires_type_arguments(self):
        result = run_sa_with_errors(
            parse_source("fn f() -> None { let m: Map = {}; }")
        )
        self.assertTrue(
            any(
                "type 'Map' expects 2 generic argument(s), got 0" in e.message
                for e in result.errors
            )
        )

    def test_non_none_function_must_return(self):
        result = run_sa_with_errors(
            parse_source("fn f() -> Int { let x: Int = 1; }")
        )
        self.assertEqual(len(result.errors), 1)
        self.assertIn("function 'f' must return a value", result.errors[0].message)

    def test_generic_extra_missing_return(self):
        prog = parse_source(
            "struct Point<T> { x: T, y: T }"
            "extra<T> Point<T> { fn new(x: T, y: T) -> Self { Point { x, y }; } }"
        )
        result = run_sa_with_errors(prog)
        self.assertTrue(
            any("function 'new' must return a value" in e.message for e in result.errors)
        )

    def test_generic_extra_method_return_substituted(self):
        prog = parse_source(
            "struct Box<T> { pub x: T, }"
            "extra<T> Box<T> { fn m(self) -> T { return self.x; } }"
            "fn f() -> String {"
            "  let b: Box<String> = Box<String> { \"a\" };"
            "  return b.m();"
            "}"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])

    def test_generic_extra_method_arg_substituted(self):
        prog = parse_source(
            "struct Box<T> { pub x: T, }"
            "extra<T> Box<T> { fn set(self, v: T) -> None { } }"
            'fn f() -> None { let b: Box<String> = Box<String> { "a" }; b.set("x"); }'
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])

    def test_generic_struct_field_read_substituted(self):
        prog = parse_source(
            "struct Box<T> { pub x: T, }"
            "fn f(b: Box<String>) -> String { return b.x; }"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])

    def test_generic_struct_field_write_substituted(self):
        prog = parse_source(
            "struct Box<T> { pub x: T, }"
            'fn f(b: Box<String>) -> None { b.x = "y"; }'
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])

    def test_generic_fn_call_inferred(self):
        prog = parse_source(
            "fn id<T>(x: T) -> T { return x; }"
            "fn f() -> Int { return id(1); }"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])

    def test_generic_fn_nested_inference(self):
        prog = parse_source(
            "fn first<T>(v: Vector<T>) -> T { return v[0]; }"
            "fn f() -> Int { return first([1, 2]); }"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])

    def test_method_generic_params_in_scope(self):
        prog = parse_source(
            "struct S { }"
            "extra S { fn id<T>(self, v: T) -> T { return v; } }"
            "trait Tr { fn pick<T>(self, v: T) -> T; }"
            "impl Tr for S { fn pick<T>(self, v: T) -> T { return v; } }"
            "fn f() -> Int { let s: S = S { }; return s.id(1); }"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])

    def test_method_generic_alpha_equivalence(self):
        prog = parse_source(
            "struct S { }"
            "trait Tr { fn pick<T>(self, v: T) -> T; }"
            "impl Tr for S { fn pick<U>(self, v: U) -> U { return v; } }"
            "fn f() -> Int { let s: S = S { }; return s.pick(1); }"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])

    def test_generic_static_path_call_inferred(self):
        prog = parse_source(
            "struct Box<T> { pub x: T, }"
            "extra<T> Box<T> { static fn make(v: T) -> T { return v; } }"
            "fn f() -> Int { return Box::make(1); }"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])

    def test_generic_use_site_mismatch_still_detected(self):
        prog = parse_source(
            "struct Box<T> { pub x: T, }"
            "extra<T> Box<T> { fn set(self, v: T) -> None { } }"
            "fn f(b: Box<String>) -> None { b.set(1); }"
        )
        result = run_sa_with_errors(prog)
        self.assertTrue(
            any(
                "argument 1 of 'set' must be String, got Int" in e.message
                for e in result.errors
            )
        )

        prog = parse_source(
            "struct Box<T> { pub x: T, }"
            "fn f(b: Box<String>) -> None { b.x = 1; }"
        )
        result = run_sa_with_errors(prog)
        self.assertTrue(
            any("cannot assign Int to String" in e.message for e in result.errors)
        )

        prog = parse_source(
            "fn id<T>(x: T) -> T { return x; }"
            'fn f() -> Int { return id("s"); }'
        )
        result = run_sa_with_errors(prog)
        self.assertTrue(
            any(
                "return type mismatch: expected Int, got String" in e.message
                for e in result.errors
            )
        )

    def test_impl_signature_conformance(self):
        prog = parse_source(
            "trait NoSuchTrait_C<T> { fn great(value: T) -> None; }"
            "struct User { }"
            "impl<String> NoSuchTrait_C for User {"
            " fn great(value: String) -> UInt { return 1; }"
            "}"
        )
        result = run_sa_with_errors(prog)
        self.assertTrue(
            any("returns UInt, trait requires None" in e.message for e in result.errors)
        )

    def test_impl_return_type_must_match_exactly(self):
        prog = parse_source(
            "trait T<X> { fn f(self, value: X) -> UInt8; }"
            "struct S { }"
            "impl<String> T for S { fn f(self, value: String) -> UInt { return 1; } }"
        )
        result = run_sa_with_errors(prog)
        self.assertTrue(
            any("returns UInt, trait requires UInt8" in e.message for e in result.errors)
        )

    def test_impl_signature_missing_method(self):
        prog = parse_source(
            "trait T { fn a(self) -> None; fn b(self) -> None; }"
            "struct S { }"
            "impl T for S { fn a(self) -> None { } }"
        )
        result = run_sa_with_errors(prog)
        self.assertTrue(any("does not implement 'b'" in e.message for e in result.errors))

    def test_method_specs_from_data(self):
        from cwind_frontend.sa import (
            BUILTIN_MODULE_FUNCTIONS,
            BUILTIN_OBJECTS,
            BUILTIN_TYPE_METHODS,
        )
        from cwind_frontend.sa.builtin_methods import parse_arg_patterns

        self.assertEqual(BUILTIN_OBJECTS["None"], "None")

        # count-prefixed arg patterns: `*: Type` is an unbounded tail, `N: Type`
        # a fixed repeat; plain entries mean exactly one argument.
        self.assertEqual(
            parse_arg_patterns(("Self", "*: Whatever")),
            ((1, "Self"), (None, "Whatever")),
        )
        self.assertEqual(
            parse_arg_patterns(("2: SameAsGeneric",)),
            ((2, "SameAsGeneric"),),
        )
        self.assertEqual(
            parse_arg_patterns(("2: SameAsGeneric:1", "SameAsGeneric:2")),
            ((2, "SameAsGeneric:1"), (1, "SameAsGeneric:2")),
        )
        self.assertEqual(
            BUILTIN_TYPE_METHODS["String"]["format"].args,
            ("Self", "*: Whatever"),
        )
        self.assertEqual(
            BUILTIN_TYPE_METHODS["Vector"]["new"].args,
            (),
        )
        self.assertEqual(
            BUILTIN_TYPE_METHODS["Vector"]["push_back"].args,
            ("Self", "SameAsGeneric:1"),
        )
        self.assertEqual(
            BUILTIN_TYPE_METHODS["Map"]["get"].args,
            ("Self", "SameAsGeneric:1"),
        )
        self.assertEqual(
            BUILTIN_TYPE_METHODS["Map"]["get"].returns,
            "SameAsGeneric:2",
        )
        self.assertEqual(
            BUILTIN_TYPE_METHODS["Map"]["set"].args,
            ("Self", "SameAsGeneric:1", "SameAsGeneric:2"),
        )
        self.assertIn("to_string", BUILTIN_TYPE_METHODS["Map"])  # via Display trait
        # instance methods declare a leading Self; module functions (static)
        # must not.  `new` is a static constructor (no Self).
        for type_name, methods in BUILTIN_TYPE_METHODS.items():
            for spec in methods.values():
                if spec.name in ("new", "from"):
                    # new/from 是静态构造/转换: 不带 Self
                    continue
                self.assertTrue(
                    spec.args and spec.args[0] == "Self",
                    (type_name, spec.name),
                )
        for spec in BUILTIN_MODULE_FUNCTIONS.values():
            self.assertFalse(spec.args and spec.args[0] == "Self", spec.name)

    def test_from_into_conversion(self):
        prog = parse_source(
            "struct MyS { } struct Target { }"
            "impl From<MyS> for Target {"
            " fn from(value: MyS) -> Target { return Target { }; }"
            "}"
            "fn f(s: MyS) -> Target { return s.into(); }"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])

    def test_directional_builtin_from_into(self):
        """``From<String>`` / ``Into<UInt>`` attached to built-in types make
        ``UInt::from(s)`` / ``s.into()`` resolve without user impls."""
        from cwind_frontend.sa import builtin_methods as bm

        old_string = bm.BUILTIN_TYPE_METHODS.get("String")
        old_uint = bm.BUILTIN_TYPE_METHODS.get("UInt")
        bm.BUILTIN_TYPE_METHODS["String"] = dict(old_string or {})
        bm.BUILTIN_TYPE_METHODS["UInt"] = dict(old_uint or {})
        bm.BUILTIN_TYPE_METHODS["String"]["into"] = bm.MethodSpec(
            "into", ("Self",), "UInt"
        )
        bm.BUILTIN_TYPE_METHODS["UInt"]["from"] = bm.MethodSpec(
            "from", ("String",), "Self"
        )
        try:
            prog = parse_source(
                "fn f(s: String) -> None {"
                " let n: UInt = s.into();"
                " let m: UInt = UInt::from(s);"
                "}"
            )
            self.assertEqual(run_sa_with_errors(prog).errors, [])
            calls = TestSa._find_all(prog, A.Call)
            by_ref = {c._typed_ann["call"]["callee_ref"]: c for c in calls}
            self.assertEqual(by_ref["into"]._typed_ann["type"]["name"], "UInt")
            self.assertEqual(by_ref["from"]._typed_ann["type"]["name"], "UInt")
        finally:
            if old_string is None:
                del bm.BUILTIN_TYPE_METHODS["String"]
            else:
                bm.BUILTIN_TYPE_METHODS["String"] = old_string
            if old_uint is None:
                del bm.BUILTIN_TYPE_METHODS["UInt"]
            else:
                bm.BUILTIN_TYPE_METHODS["UInt"] = old_uint

    def test_from_static_call(self):
        prog = parse_source(
            "struct MyS { } struct Target { }"
            "impl From<MyS> for Target {"
            " fn from(value: MyS) -> Target { return Target { }; }"
            "}"
            "fn f(s: MyS) -> Target { return Target::from(s); }"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])

    def test_into_return_type_checked(self):
        prog = parse_source(
            "struct MyS { } struct Target { }"
            "impl From<MyS> for Target {"
            " fn from(value: MyS) -> Target { return Target { }; }"
            "}"
            'fn f(s: MyS) -> String { return s.into(); }'
        )
        result = run_sa_with_errors(prog)
        self.assertTrue(
            any(
                "return type mismatch" in e.message
                for e in result.errors
            )
        )

    def test_string_into_uses_context(self):
        prog = parse_source(
            "fn f(s: String) -> None {"
            " let n: UInt = s.into();"
            " let i: Int = Int::from(s);"
            "}"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])

    def test_into_requires_target_type(self):
        prog = parse_source(
            "fn f(s: String) -> None { s.into(); }"
        )
        errors = run_sa_with_errors(prog).errors
        self.assertTrue(
            any(
                "into() needs a target type" in e.message
                for e in errors
            )
        )

    def test_into_rejects_unsupported_target(self):
        prog = parse_source(
            "fn f(s: String) -> None { let b: Bool = s.into(); }"
        )
        errors = run_sa_with_errors(prog).errors
        self.assertTrue(
            any(
                "no conversion from String to Bool via 'into()'"
                in e.message
                for e in errors
            )
        )

    def test_from_requires_one_arg_and_methods(self):
        result = run_sa_with_errors(parse_source("struct S { } impl From for S {}"))
        self.assertTrue(
            any("From requires one type argument" in e.message for e in result.errors)
        )

        result = run_sa_with_errors(parse_source("struct S { } impl From<Int> for S {}"))
        self.assertTrue(any("must define 'from'" in e.message for e in result.errors))
        self.assertFalse(any("must define 'into'" in e.message for e in result.errors))

    def test_compact_program_sa(self):
        result = run_sa_with_errors(parse_source(_COMPACT_PROGRAM))
        self.assertEqual(result.errors, [])
        self.assertGreater(len(result.info.symbols), 6)
        json.dumps(result.info.to_dict())  # must be JSON-serializable

    def test_typed_ast_metadata(self):
        from cwind_frontend.typed_ast import build_typed_ast

        prog = parse_source(
            "struct P<T> { pub x: T }\n"
            "trait D { fn s(self) -> String; }\n"
            "impl D for P<Int> { fn s(self) -> String { return \"x\"; } }\n"
            "fn f(p: P<Int>) -> Int {\n"
            "    let v: Vector<Int> = [p.x];\n"
            "    return v[0];\n"
            "}\n"
        )
        result = run_sa_with_errors(prog)
        self.assertEqual(result.errors, [])
        info = result.info
        # impl bindings record the trait and both node refs
        self.assertEqual(len(info.bindings), 1)
        binding = info.bindings[0]
        self.assertEqual(binding.owner, "P")
        self.assertEqual(binding.trait, "D")
        self.assertEqual(binding.to_dict()["id"], binding.id)
        # every top-level symbol carries the id of its declaration node
        self.assertTrue(all(sym.ref is not None for sym in info.symbols.values()))
        # the typed document is serializable and self-consistent
        doc = build_typed_ast(prog, info)
        json.dumps(doc)
        ast = doc["ast"]
        self.assertEqual(ast["kind"], "Program")
        self.assertEqual(ast["id"], 1)

        def walk(node):
            if "kind" not in node:
                return
            yield node
            for value in node.values():
                if isinstance(value, dict) and "kind" in value:
                    yield from walk(value)
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict) and "kind" in item:
                            yield from walk(item)

        nodes = {n["id"]: n for n in walk(ast)}
        self.assertEqual(max(nodes), len(nodes))  # dense, pre-order ids
        # attribute access on a concrete generic struct field resolves
        attr = next(
            n for n in nodes.values()
            if n["kind"] == "Attribute" and n.get("name") == "x"
        )
        self.assertEqual(attr["ann"]["member"]["kind"], "field")
        self.assertEqual(attr["ann"]["type"], {"name": "Int"})
        # indexing annotates container/index types
        index = next(
            n for n in nodes.values()
            if n["kind"] == "Index" and n["ann"].get("container_type")
        )
        self.assertEqual(
            index["ann"]["container_type"],
            {"name": "Vector", "args": [{"name": "Int"}]},
        )
        self.assertEqual(index["ann"]["index_type"], {"name": "Int"})

    def test_typed_ast_coverage_fixes(self):
        from cwind_frontend.typed_ast import build_typed_ast

        prog = parse_source(
            "struct Point<T: Display> { x: T }\n"
            "trait D { fn s(self) -> Self; }\n"
            "typedef Name = String;\n"
            "group G(a: String) { a -> Name; }\n"
            "type Email = String where { self.length >= 3; }\n"
            "struct A {}\nstruct B {}\n"
            "extra A { fn a1(self) -> Int { return 1; } }\n"
            "extra B { fn b1(self) -> Int { return 1; } }\n"
            "extra A { fn a2(self) -> Int { return 2; } }\n"
            "fn g<T>(p: Point<T>) -> Vector<T> { return [p.x]; }\n"
        )
        result = run_sa_with_errors(prog)
        self.assertEqual(result.errors, [])
        doc = build_typed_ast(prog, result.info)
        ast = doc["ast"]

        def walk(node):
            if "kind" not in node:
                return
            yield node
            for value in node.values():
                if isinstance(value, dict) and "kind" in value:
                    yield from walk(value)
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict) and "kind" in item:
                            yield from walk(item)

        nodes = list(walk(ast))
        # generic-parameter bound Type nodes are annotated
        bound = next(
            n for n in nodes
            if n["kind"] == "Type" and n.get("name") == "Display"
        )
        self.assertEqual(bound["ann"], {"type": {"name": "Display"}})
        # bodyless trait methods still carry Param / FnDecl annotations,
        # with Self preserved because no owner is known
        trait_fn = next(
            n for n in nodes
            if n["kind"] == "FnDecl" and n.get("name") == "s"
        )
        self.assertEqual(trait_fn["ann"], {"type": {"name": "Self"}})
        self_param = next(
            n for n in nodes
            if n["kind"] == "Param" and n.get("name") == "self"
            and n.get("line") == trait_fn["line"]
        )
        self.assertEqual(self_param["ann"], {"type": {"name": "Self"}})
        # group parameters and their Type nodes are annotated
        group_param = next(
            n for n in nodes
            if n["kind"] == "Param" and n.get("name") == "a"
            and n["ann"].get("type", {}).get("name") == "String"
        )
        self.assertEqual(group_param["ann"]["type"], {"name": "String"})
        # validation-block `self` has a type but no binding node to point at
        validation_self = next(
            n for n in nodes
            if n["kind"] == "Name" and n.get("parts") == ["self"]
        )
        self.assertNotIn("binding", validation_self["ann"])
        self.assertEqual(validation_self["ann"]["type"], {"name": "String"})
        # bindings follow source order (ids and decl_ids both ascend)
        self.assertEqual([b["id"] for b in doc["bindings"]], [1, 2, 3])
        decl_ids = [b["decl_id"] for b in doc["bindings"]]
        self.assertEqual(decl_ids, sorted(decl_ids))
        # a field access in a generic context keeps its member ref and a
        # structured opaque type (previously blanked, which disabled all
        # type checks on generic fields)
        attr = next(
            n for n in nodes
            if n["kind"] == "Attribute" and n.get("name") == "x"
        )
        self.assertEqual(attr["ann"]["member"]["kind"], "field")
        self.assertEqual(attr["ann"]["type"], {"name": "T", "opaque": True})
        # a literal whose element type is a generic parameter keeps the
        # opaque leaf (more precise than collapsing to Vector<Any>)
        vector_lit = next(n for n in nodes if n["kind"] == "VectorLit")
        self.assertEqual(
            vector_lit["ann"]["type"],
            {"name": "Vector", "args": [{"name": "T", "opaque": True}]},
        )

    def test_typed_ast_slice_and_unary(self):
        from cwind_frontend.typed_ast import build_typed_ast

        prog = parse_source(
            "fn f(xs: Vector<Int>) -> Int {\n"
            "    let a: Int = -xs[0];\n"
            "    let b: Vector<Int> = xs[0:2];\n"
            "    return a + b.length();\n"
            "}\n"
        )
        result = run_sa_with_errors(prog)
        self.assertEqual(result.errors, [])
        ast = build_typed_ast(prog, result.info)["ast"]

        def walk(node):
            if "kind" not in node:
                return
            yield node
            for value in node.values():
                if isinstance(value, dict) and "kind" in value:
                    yield from walk(value)
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict) and "kind" in item:
                            yield from walk(item)

        nodes = list(walk(ast))
        unary = next(n for n in nodes if n["kind"] == "UnaryOp")
        self.assertEqual(unary["ann"]["type"], {"name": "Int"})
        self.assertEqual(unary["ann"]["operand_type"], {"name": "Int"})
        sl = next(n for n in nodes if n["kind"] == "Slice")
        self.assertEqual(
            sl["ann"]["type"],
            {"name": "Vector", "args": [{"name": "Int"}]},
        )
        self.assertEqual(
            sl["ann"]["container_type"],
            {"name": "Vector", "args": [{"name": "Int"}]},
        )

    def test_typed_ast_into_conversion(self):
        from cwind_frontend.typed_ast import build_typed_ast

        prog = parse_source(
            "struct S {} struct T {}"
            "impl From<S> for T {"
            " fn from(v: S) -> T { return T {}; }"
            "}"
            "fn f(s: S) -> T { return s.into(); }"
        )
        result = run_sa_with_errors(prog)
        self.assertEqual(result.errors, [])
        ast = build_typed_ast(prog, result.info)["ast"]
        call = next(n for n in _typed_nodes(ast) if n["kind"] == "Call")
        self.assertEqual(call["ann"]["call"]["callee_kind"], "method")
        self.assertEqual(call["callee"]["parts"], ["T", "from"])
        self.assertEqual(call["ann"]["type"], {"name": "T"})

    def test_typed_ast_which_method(self):
        from cwind_frontend.typed_ast import build_typed_ast

        prog = parse_source(
            "struct User { name: String }"
            "extra User {"
            " fn set_name(self, n: String) -> None {}"
            " fn after_set(self) -> None, which ::set_name {}"
            "}"
        )
        result = run_sa_with_errors(prog)
        self.assertEqual(result.errors, [])
        doc = build_typed_ast(prog, result.info)
        json.dumps(doc)  # must stay serializable
        self.assertEqual(len(doc["bindings"]), 2)
        which_fn = next(
            n for n in _typed_nodes(doc["ast"])
            if n["kind"] == "FnDecl" and n.get("which") == "set_name"
        )
        self.assertEqual(which_fn["ann"]["type"], {"name": "None"})
        hook_calls = [
            n for n in _typed_nodes(doc["ast"])
            if n["kind"] == "Call"
            and n.get("ann", {}).get("call", {}).get("callee_kind") == "method"
            and n.get("ann", {}).get("call", {}).get("callee_ref") == 2
        ]
        self.assertEqual(len(hook_calls), 1)

    def test_which_hook_cannot_be_called_directly(self):
        prog = parse_source(
            "struct User { name: String }"
            "extra User {"
            " fn greet(self) -> None { return None; }"
            " fn after(self), which ::greet {}"
            "}"
            "fn f(u: User) -> None { u.after(); }"
        )
        result = run_sa_with_errors(prog)
        self.assertTrue(
            any(
                "which hook 'after' cannot be called directly" in e.message
                for e in result.errors
            )
        )

    def test_random_programs_do_not_crash(self):
        import random

        from cwind_frontend import lex_with_errors, parse_with_errors
        from cwind_frontend.typed_ast import build_typed_ast

        rng = random.Random(20260810)
        types = ["Int", "UInt", "Int8", "UInt8", "Float", "String", "Bool", "Byte"]
        exprs = [
            "1", "2", "0", "-3", "1 + 2", "2 * 3", "1 / 2", "1 % 2",
            '"s"', "true", "1.5", "16777216 + 1", "x", "x + 1",
            "xs[0]", "m[1]", "f(1)", "x.length()", "[1, 2]", "{1: 2}",
            "Vector::new()", "s.into()",
        ]

        def random_fn():
            lets = "".join(
                f" let v{i}: {rng.choice(types)} = {rng.choice(exprs)};"
                for i in range(rng.randrange(4))
            )
            return (
                f"fn f(x: {rng.choice(types)}) -> {rng.choice(types)} {{"
                f"{lets} return {rng.choice(exprs)}; }}"
            )

        templates = [
            random_fn,
            lambda: (
                "struct S { a: Int, b: String }"
                "extra S { fn m(self) -> Int { return self.a; } }"
                "fn f(s: S) -> Int { return s.m() + 1; }"
            ),
            lambda: (
                "struct S {} struct T {}"
                "impl From<S> for T { fn from(v: S) -> T { return T {}; } }"
                "fn f(s: S) -> T { return s.into(); }"
            ),
            lambda: f"fn g<T>(x: T) -> Vector<T> {{ return [{rng.choice(exprs)}]; }}",
        ]
        for i in range(300):
            src = templates[i % len(templates)]()
            try:
                lexed = lex_with_errors(src)
                parsed = parse_with_errors(lexed.tokens)
                prog = parsed.program
                result = run_sa_with_errors(prog)
                if not result.errors:
                    doc = build_typed_ast(prog, result.info)
                    json.dumps(doc)
            except Exception as exc:  # pragma: no cover - failure is the test
                self.fail(f"pipeline crashed on:\n{src}\n{exc!r}")

    def test_struct_construct_self_in_extra(self):
        from cwind_frontend.typed_ast import build_typed_ast

        prog = parse_source(
            "struct User { name: String, age: Int }"
            "extra User {"
            " fn new(name: String, age: Int) -> Self {"
            "   return Self { name, age };"
            " }"
            "}"
        )
        result = run_sa_with_errors(prog)
        self.assertEqual(result.errors, [])
        ast = build_typed_ast(prog, result.info)["ast"]
        construct = next(
            n for n in _typed_nodes(ast) if n["kind"] == "StructConstruct"
        )
        self.assertEqual(construct["ann"]["type"], {"name": "User"})
        self.assertEqual(
            construct["ann"]["field_types"],
            [{"name": "String"}, {"name": "Int"}],
        )
        self.assertEqual(construct["type"]["ann"]["type"], {"name": "User"})
        # both the `-> Self` signature node and the `Self { ... }` node resolve
        self_types = [
            n for n in _typed_nodes(ast)
            if n["kind"] == "Type" and n.get("name") == "Self"
        ]
        self.assertTrue(self_types)
        self.assertTrue(
            all(n["ann"].get("type") == {"name": "User"} for n in self_types)
        )

    def test_typed_ast_const_reference_binding(self):
        from cwind_frontend.typed_ast import build_typed_ast

        prog = parse_source(
            'const Steve: Map<String, String> = { "name": "Steve" };'
            'fn f() -> String { return Steve["name"]; }'
        )
        result = run_sa_with_errors(prog)
        self.assertEqual(result.errors, [])
        ast = build_typed_ast(prog, result.info)["ast"]
        nodes = list(_typed_nodes(ast))
        steve = next(
            n for n in nodes
            if n["kind"] == "Name" and n.get("parts") == ["Steve"]
        )
        self.assertEqual(steve["ann"]["binding"]["kind"], "const")
        by_id = {n["id"]: n for n in nodes}
        self.assertEqual(
            by_id[steve["ann"]["binding"]["ref"]]["kind"], "ConstDecl"
        )
        # the map index still resolves to the value type
        index = next(n for n in nodes if n["kind"] == "Index")
        self.assertEqual(index["ann"]["type"], {"name": "String"})

    def test_refinement_compile_time_checks(self):
        prog = parse_source(
            "type Age = Int where { self > 0; self < 100; }"
            "struct User { name: String, age: Age }"
            "fn bad_let() -> None { let a: Age = 999; }"
            "fn bad_return() -> Age { return 999; }"
            "fn bad_construct() -> User { return User { \"x\", 999 }; }"
            "fn bad_assign() -> None {"
            " let a: Age = 50; a = 999;"
            "}"
            "fn bad_param(x: Age) -> None {}"
            "fn bad_call() -> None { bad_param(999); }"
        )
        messages = [e.message for e in run_sa_with_errors(prog).errors]
        self.assertEqual(
            sum("does not satisfy refinement of 'Age'" in m for m in messages),
            5,
            messages,
        )

        good = parse_source(
            "type Age = Int where { self > 0; self < 100; }"
            "struct User { name: String, age: Age }"
            "fn f() -> None {"
            " let a: Age = 50;"
            " a = 60;"
            " let u: User = User { \"x\", 70 };"
            "}"
            "fn g(x: Age) -> None {}"
            "fn h() -> None { g(80); }"
        )
        self.assertEqual(run_sa_with_errors(good).errors, [])

    def test_refinement_constructor_flow(self):
        prog = parse_source(
            "type Age = Int where { self > 0; self < 100; }"
            "struct User { name: String, age: Age }"
            "extra User {"
            " fn new(name: String, age: Int) -> Self {"
            "   return Self { name, age };"
            " }"
            "}"
            "fn main() -> None {"
            " let steve: User = User::new(\"Steve\", 999);"
            "}"
        )
        result = run_sa_with_errors(prog)
        self.assertTrue(
            any(
                "does not satisfy refinement of 'Age'" in e.message
                for e in result.errors
            )
        )

        # a non-foldable argument is left to runtime checks
        ok = parse_source(
            "type Age = Int where { self > 0; self < 100; }"
            "struct User { name: String, age: Age }"
            "extra User {"
            " fn new(name: String, age: Int) -> Self {"
            "   return Self { name, age };"
            " }"
            "}"
            "fn f(a: Int) -> User { return User::new(\"x\", a); }"
        )
        self.assertEqual(run_sa_with_errors(ok).errors, [])

    def test_refinement_inline_field_validation(self):
        prog = parse_source(
            "struct S { age: Int where { age > 0 && age < 100 } }"
            "fn f() -> S { return S { 999 }; }"
        )
        result = run_sa_with_errors(prog)
        self.assertTrue(
            any(
                "does not satisfy validation of field 'age'" in e.message
                for e in result.errors
            )
        )
        ok = parse_source(
            "struct S { age: Int where { age > 0 && age < 100 } }"
            "fn f() -> S { return S { 50 }; }"
        )
        self.assertEqual(run_sa_with_errors(ok).errors, [])

    def test_refinement_dead_bound_warning(self):
        # `self < 256` can never fail for Int8 (max 127): the value is blocked
        # by Int8's own range check before refinement ever runs.
        result = run_sa_with_errors(parse_source(
            "type TestAge = Int8 where {\n"
            "    self > 0;\n"
            "    self < 256;\n"
            "}\n"
        ))
        self.assertEqual(result.errors, [])
        self.assertEqual(
            [w.message for w in result.warnings],
            [
                "refinement condition '(self < 256)' can never be violated "
                "for Int8 (values -128..127)",
            ],
        )

        # a bound below the minimum is dead too (UInt8 starts at 0)
        result = run_sa_with_errors(parse_source(
            "type T = UInt8 where { self >= 0; self < 100; }\n"
        ))
        self.assertTrue(
            any(
                "can never be violated" in w.message
                and "(self >= 0)" in w.message
                for w in result.warnings
            )
        )

        # a bound above the maximum can never be satisfied at all
        result = run_sa_with_errors(parse_source(
            "type T = Int8 where { self > 127; }\n"
        ))
        self.assertTrue(
            any("can never be satisfied" in w.message for w in result.warnings)
        )

        # in-range refinements stay silent
        result = run_sa_with_errors(parse_source(
            "type T = Int8 where { self > 0; self < 100; }\n"
        ))
        self.assertEqual(result.warnings, [])

    def test_refinement_checked_in_builtin_method_args(self):
        # `Vector<Test1>`'s element type is Test1; a foldable argument like
        # 101 must be rejected by Test1's refinement even though Int8 and Int
        # are numerically compatible.
        prog = parse_source(
            "type Test1 = Int8 where { self > 0; self < 100; }"
            "fn main() -> None {"
            " let arr: Vector<Test1> = Vector::new();"
            " arr.push_back(101);"
            "}"
        )
        result = run_sa_with_errors(prog)
        self.assertTrue(
            any(
                "value 101 does not satisfy refinement of 'Test1'"
                in e.message
                for e in result.errors
            )
        )

        # in-bounds literals and non-foldable values stay valid
        ok = parse_source(
            "type Test1 = Int8 where { self > 0; self < 100; }"
            "fn f(x: Int) -> None {"
            " let arr: Vector<Test1> = Vector::new();"
            " arr.push_back(50);"
            " arr.push_back(x);"
            "}"
        )
        self.assertEqual(run_sa_with_errors(ok).errors, [])

        # the same applies to other built-in methods with refined generic
        # arguments, e.g. Map::set / contains
        prog = parse_source(
            "type Test1 = Int8 where { self > 0; self < 100; }"
            "fn f() -> None {"
            " let m: Map<Test1, Int> = Map::new();"
            " m.set(101, 1);"
            "}"
        )
        self.assertTrue(
            any(
                "does not satisfy refinement of 'Test1'" in e.message
                for e in run_sa_with_errors(prog).errors
            )
        )

        # plain width checks apply to built-in method arguments as well
        prog = parse_source(
            "fn f() -> None {"
            " let arr: Vector<Int> = Vector::new();"
            " arr.push_back(99999);"
            "}"
        )
        self.assertTrue(
            any("value 99999 does not fit in Int" in e.message
                for e in run_sa_with_errors(prog).errors)
        )

    def test_refinement_checked_through_local_constants(self):
        # `let t2: UInt8 = 127 + 1;` makes later uses of `t2` compile-time
        # known (128), so assigning it to the refined Int8 type `Test5`
        # must be rejected by both the width and the refinement checks.
        prog = parse_source(
            "type Test5 = Int8 where { self < 55; }"
            "fn main() -> None {"
            " let t1: Test5;"
            " let t2: UInt8 = 127 + 1;"
            " t1 = t2;"
            "}"
        )
        messages = [e.message for e in run_sa_with_errors(prog).errors]
        self.assertTrue(
            any("does not satisfy refinement of 'Test5'" in m for m in messages)
        )
        self.assertTrue(any("does not fit in Int8" in m for m in messages))

        # in-bounds local constants stay valid, including via `let`
        ok = parse_source(
            "type Test5 = Int8 where { self < 55; }"
            "fn main() -> None {"
            " let t2: UInt8 = 10;"
            " let t3: Test5 = t2;"
            "}"
        )
        self.assertEqual(run_sa_with_errors(ok).errors, [])

        # a chained foldable local is tracked too
        chained = parse_source(
            "type Test5 = Int8 where { self < 55; }"
            "fn main() -> None {"
            " let a: Int = 200;"
            " let b: Test5 = a;"
            "}"
        )
        messages = [e.message for e in run_sa_with_errors(chained).errors]
        self.assertTrue(
            any("does not satisfy refinement of 'Test5'" in m for m in messages)
        )

        # once a variable is reassigned from an unknown source, its known
        # value is forgotten and the refinement is left to runtime
        unknown = parse_source(
            "type Test5 = Int8 where { self < 55; }"
            "fn f(p: Int) -> None {"
            " let x: Int = 1;"
            " x = p;"
            " let y: Test5 = x;"
            "}"
        )
        self.assertEqual(run_sa_with_errors(unknown).errors, [])

    def test_refinement_checked_through_function_returns(self):
        # `fn t6() -> UInt8 { return 55 + 1; }` folds to 56, so assigning the
        # call result to the refined Int8 type `Test6` must be rejected.
        prog = parse_source(
            "type Test6 = Int8 where { self < 55; }"
            "fn t6() -> UInt8 { return 55 + 1; }"
            "fn main() -> None {"
            " let t1: Test6;"
            " t1 = t6();"
            "}"
        )
        messages = [e.message for e in run_sa_with_errors(prog).errors]
        self.assertTrue(
            any("value 56 does not satisfy refinement of 'Test6'" in m
                for m in messages)
        )

        ok = parse_source(
            "type Test6 = Int8 where { self < 55; }"
            "fn ok() -> UInt8 { return 10; }"
            "fn main() -> None {"
            " let t1: Test6;"
            " t1 = ok();"
            "}"
        )
        self.assertEqual(run_sa_with_errors(ok).errors, [])

        # function return values also feed width checks and chain through
        # other functions; recursive / parameterized calls stay unknown
        width = parse_source(
            "fn big() -> Int { return 300; }"
            "fn main() -> None { let x: Int8 = big(); }"
        )
        self.assertTrue(
            any("value 300 does not fit in Int8" in e.message
                for e in run_sa_with_errors(width).errors)
        )
        chain = parse_source(
            "type Test6 = Int8 where { self < 55; }"
            "fn b() -> Int { return 300; }"
            "fn a() -> Int { return b(); }"
            "fn main() -> None { let t: Test6 = a(); }"
        )
        self.assertTrue(
            any("does not satisfy refinement of 'Test6'" in e.message
                for e in run_sa_with_errors(chain).errors)
        )
        conservative = parse_source(
            "fn f(x: Int) -> Int { return x; }"
            "fn main() -> None { let x: Int8 = f(300); }"
        )
        self.assertEqual(run_sa_with_errors(conservative).errors, [])


class TestTupleAndMapIter(unittest.TestCase):
    """Tuple literal / element access / indexing and Map for-in typing."""

    @staticmethod
    def _find_first(prog, kind):
        return TestSa._find_first(prog, kind)

    @staticmethod
    def _find_all(prog, kind):
        found = []

        def walk(node):
            if isinstance(node, kind):
                found.append(node)
            for attr in (
                "items", "stmts", "value", "left", "right", "operand",
                "expr", "body", "then", "else_", "elifs", "args", "elems",
                "obj", "index", "target", "cond", "iterable",
                "subject", "arms", "pattern", "guard",
            ):
                v = getattr(node, attr, None)
                if isinstance(v, list):
                    for x in v:
                        walk(x)
                elif v is not None:
                    walk(v)

        walk(prog)
        return found

    def test_tuple_literal_typing(self):
        prog = parse_source(
            'fn f() -> None { let t: Tuple<Int, String> = (1, "x"); }'
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])
        lit = self._find_first(prog, A.TupleLit)
        self.assertEqual(
            lit._typed_ann["type"],
            {
                "name": "Tuple",
                "args": [{"name": "Int"}, {"name": "String"}],
            },
        )
        self.assertEqual(
            [e["name"] for e in lit._typed_ann["element_types"]],
            ["Int", "String"],
        )

    def test_bare_tuple_annotation_rejects_nonempty(self):
        bad = parse_source(
            "fn f() -> None { let t: Tuple = (1, 2); }"
        )
        errors = run_sa_with_errors(bad).errors
        self.assertTrue(
            any(
                "cannot initialize Tuple with Tuple<Int, Int>"
                in e.message
                for e in errors
            )
        )
        ok = parse_source("fn f() -> None { let e: Tuple = (); }")
        self.assertEqual(run_sa_with_errors(ok).errors, [])

    def test_tuple_index_typing(self):
        prog = parse_source(
            "fn f() -> None {"
            ' let t: Tuple<Int, String> = (1, "x");'
            " let a: String = t[1];"
            ' let p: Tuple<Tuple<Int, Int>, String> = ((1, 2), "n");'
            " let b: Int = p[0][1];"
            "}"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])
        idxs = self._find_all(prog, A.Index)
        self.assertEqual(len(idxs), 3)
        self.assertEqual(idxs[0]._typed_ann["type"]["name"], "String")
        self.assertEqual(idxs[0]._typed_ann["tuple_index"], 1)
        self.assertEqual(idxs[1]._typed_ann["type"]["name"], "Int")
        self.assertEqual(idxs[1]._typed_ann["tuple_index"], 1)
        self.assertEqual(idxs[2]._typed_ann["type"]["name"], "Tuple")
        self.assertEqual(idxs[2]._typed_ann["tuple_index"], 0)

    def test_tuple_index_errors(self):
        out_of_range = parse_source(
            'fn f() -> None { let t: Tuple<Int, String> = (1, "x");'
            " let a: Int = t[2];"
            "}"
        )
        errors = run_sa_with_errors(out_of_range).errors
        self.assertTrue(
            any("has no element at index 2" in e.message for e in errors)
        )
        dynamic = parse_source(
            "fn f(i: Int) -> None {"
            ' let t: Tuple<Int, String> = (1, "x");'
            " let a: Int = t[i];"
            "}"
        )
        errors = run_sa_with_errors(dynamic).errors
        self.assertTrue(
            any(
                "tuple index must be a compile-time integer constant"
                in e.message
                for e in errors
            )
        )

    def test_tuple_element_access_typing(self):
        prog = parse_source(
            'fn f() -> None { let t: Tuple<Int, String> = (1, "x");'
            " let a: Int = t.0;"
            " let b: String = t.1;"
            "}"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])
        attrs = self._find_all(prog, A.Attribute)
        self.assertEqual(len(attrs), 2)
        self.assertEqual(attrs[0]._typed_ann["member"]["kind"], "tuple_elem")
        self.assertEqual(attrs[0]._typed_ann["member"]["index"], 0)
        self.assertEqual(attrs[0]._typed_ann["type"]["name"], "Int")
        self.assertEqual(attrs[1]._typed_ann["member"]["kind"], "tuple_elem")
        self.assertEqual(attrs[1]._typed_ann["member"]["index"], 1)
        self.assertEqual(attrs[1]._typed_ann["type"]["name"], "String")

    def test_map_forin_var_type(self):
        prog = parse_source(
            'fn f() -> None { let m: Map<String, Int> = { "a": 1 };'
            " for kv in m { print(kv[0]); }"
            "}"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])
        forstmt = self._find_first(prog, A.ForStmt)
        var_type = forstmt._typed_ann["var_type"]
        self.assertEqual(var_type["name"], "Tuple")
        self.assertEqual(
            [a["name"] for a in var_type["args"]], ["String", "Int"]
        )
        idx = self._find_first(prog, A.Index)
        self.assertEqual(idx._typed_ann["type"]["name"], "String")

    def test_map_entry_in_generic_method_uses_tuple_marker(self):
        prog = parse_source(
            "trait D { fn to_json(self) -> String; }"
            "impl<T: Into<String>> D for Map<String, T> {"
            " fn to_json(self) -> String {"
            "  let r: String = \"\";"
            "  for (kv: self.entry()) { r += kv.0; r += kv.1.to_string(); }"
            "  return r;"
            " }"
            "}"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])
        found = []

        def walk(node):
            if isinstance(node, A.ForStmt):
                found.append(("for", node))
            if isinstance(node, A.Call):
                found.append(("call", node))
            for attr in (
                "items", "stmts", "methods", "value", "left", "right",
                "operand", "expr", "body", "then", "else_", "elifs",
                "args", "elems", "iterable",
            ):
                v = getattr(node, attr, None)
                if isinstance(v, list):
                    for x in v:
                        walk(x)
                elif v is not None:
                    walk(v)

        walk(prog)
        forstmt = next(n for k, n in found if k == "for")
        self.assertEqual(
            forstmt._typed_ann["iterable_type"]["name"], "Tuple"
        )
        var_type = forstmt._typed_ann["var_type"]
        self.assertEqual(var_type["name"], "Tuple")
        self.assertEqual(
            [a["name"] for a in var_type["args"]], ["String", "T"]
        )
        entry_call = next(
            n for k, n in found
            if k == "call"
            and n._typed_ann.get("call", {}).get("callee_ref") == "entry"
        )
        self.assertEqual(
            entry_call._typed_ann["call"]["callee_ref"], "entry"
        )
        self.assertEqual(
            entry_call._typed_ann["type"]["name"], "Tuple"
        )

    def test_unknown_generic_bound_reported(self):
        result = run_sa_with_errors(parse_source(
            "trait D { fn f(self) -> Int; }"
            "struct S<T> { x: T }"
            "impl<T: NoSuchTrait> D for S<T> {"
            " fn f(self) -> Int { return 1; }"
            "}"
        ))
        self.assertTrue(
            any("unknown bound 'NoSuchTrait'" in e.message
                for e in result.errors)
        )

        result = run_sa_with_errors(parse_source(
            "fn f<T: Missing>(x: T) -> T { return x; }"
        ))
        self.assertTrue(
            any("unknown bound 'Missing'" in e.message
                for e in result.errors)
        )

    def test_generic_bound_arity_reported(self):
        result = run_sa_with_errors(parse_source(
            "trait B<X> { fn f(self) -> Int; }"
            "trait D { fn f(self) -> Int; }"
            "struct S<T> { x: T }"
            "impl<T: B> D for S<T> { fn f(self) -> Int { return 1; } }"
        ))
        self.assertTrue(
            any(
                "bound 'B' expects 1 type argument(s), got 0" in e.message
                for e in result.errors
            )
        )

        result = run_sa_with_errors(parse_source(
            "trait D { fn f(self) -> Int; }"
            "struct S<T> { x: T }"
            "impl<T: Into> D for S<T> { fn f(self) -> Int { return 1; } }"
        ))
        self.assertTrue(
            any(
                "bound 'Into' expects 1 type argument(s), got 0" in e.message
                for e in result.errors
            )
        )


class TestPatternMatching(unittest.TestCase):
    def test_valid_match_and_if_let(self):
        prog = parse_source(
            "struct Point { x: Int, y: Int }"
            "fn f(p: Point, t: Tuple<Int, String>) -> Int {"
            " match (p) {"
            "  Point { x: 1, y } if y > 0 => { return y; },"
            "  Point { x, .. } => { return x; }"
            " }"
            " if let Point { x, y: 2 } = p { return x; } else { return 0; }"
            "}"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])

    def test_match_exhaustiveness_required(self):
        prog = parse_source(
            "fn f(x: Int) -> Int {"
            " match (x) { 1 => { return 1; }, 2 => { return 2; } }"
            "}"
        )
        errors = run_sa_with_errors(prog).errors
        self.assertTrue(
            any("match is not exhaustive" in e.message for e in errors)
        )

    def test_irrefutable_struct_pattern_is_exhaustive(self):
        prog = parse_source(
            "struct Point { x: Int, y: Int }"
            "fn f(p: Point) -> Int {"
            " match (p) { Point { x, .. } => { return x; } }"
            "}"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])

    def test_struct_pattern_missing_field(self):
        prog = parse_source(
            "struct Point { x: Int, y: Int }"
            "fn f(p: Point) -> Int {"
            " match (p) { Point { x } => { return x; }, _ => { return 0; } }"
            "}"
        )
        errors = run_sa_with_errors(prog).errors
        self.assertTrue(
            any("missing field(s): y" in e.message for e in errors)
        )

    def test_pattern_binding_scope_isolated(self):
        prog = parse_source(
            "fn f(x: Int) -> Int {"
            " match (x) { y => { return y; } }"
            " let z: Int = y;"
            " return z;"
            "}"
        )
        errors = run_sa_with_errors(prog).errors
        self.assertTrue(
            any("unknown identifier 'y'" in e.message for e in errors)
        )

    def test_pattern_binding_shadowing_allowed(self):
        prog = parse_source(
            "fn f(x: Int) -> Int {"
            " let y: Int = 1;"
            " match (x) { y => { return y; } }"
            "}"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])

    def test_duplicate_binding_rejected(self):
        prog = parse_source(
            "fn f(t: Tuple<Int, Int>) -> Int {"
            " match (t) { (a, a) => { return a; }, _ => { return 0; } }"
            "}"
        )
        errors = run_sa_with_errors(prog).errors
        self.assertTrue(
            any("duplicate definition of 'a'" in e.message for e in errors)
        )

    def test_tuple_arity_mismatch(self):
        prog = parse_source(
            "fn f(t: Tuple<Int, Int>) -> Int {"
            " match (t) { (a,) => { return a; }, _ => { return 0; } }"
            "}"
        )
        errors = run_sa_with_errors(prog).errors
        self.assertTrue(
            any("tuple pattern expects 2 element(s), got 1" in e.message
                for e in errors)
        )

    def test_guard_must_be_bool(self):
        prog = parse_source(
            "fn f(x: Int) -> Int {"
            " match (x) { y if y => { return y; }, _ => { return 0; } }"
            "}"
        )
        errors = run_sa_with_errors(prog).errors
        self.assertTrue(
            any("condition must be Bool" in e.message for e in errors)
        )

    def test_pattern_type_mismatch(self):
        prog = parse_source(
            "struct Point { x: Int, y: Int }"
            "fn f(p: Point) -> Int {"
            " match (p) { (1, 2) => { return 1; }, _ => { return 0; } }"
            "}"
        )
        errors = run_sa_with_errors(prog).errors
        self.assertTrue(
            any("tuple pattern cannot match" in e.message for e in errors)
        )

    def test_literal_range_checked(self):
        prog = parse_source(
            "fn f(v: UInt8) -> Int {"
            " match (v) { 300 => { return 1; }, _ => { return 0; } }"
            "}"
        )
        errors = run_sa_with_errors(prog).errors
        self.assertTrue(
            any("does not fit in UInt8" in e.message for e in errors)
        )

    def test_annotations(self):
        prog = parse_source(
            "fn f(t: Tuple<Int, String>) -> Int {"
            " match (t) { (1, s) => { return 1; }, _ => { return 0; } }"
            "}"
        )
        run_sa(prog)
        m = TestSa._find_first(prog, A.MatchStmt)
        self.assertEqual(m._typed_ann["subject_type"]["name"], "Tuple")
        pat = m.arms[0].pattern
        self.assertEqual(pat._typed_ann["type"]["name"], "Tuple")
        self.assertEqual(
            [e["name"] for e in pat._typed_ann["element_types"]],
            ["Int", "String"],
        )
        s = pat.elems[1]
        self.assertEqual(s._typed_ann["type"]["name"], "String")

    def test_match_expression_typing(self):
        prog = parse_source(
            "fn f(t: Tuple<Int, Int>) -> Int {"
            " let x: Int = match (t) { (1, v) => v, _ => -1 };"
            " return x;"
            "}"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])
        m = TestSa._find_first(prog, A.MatchStmt)
        self.assertEqual(m._typed_ann["type"]["name"], "Int")
        self.assertEqual(m.arms[0]._typed_ann["body_kind"], "expr")
        self.assertEqual(m.arms[0]._typed_ann["body_type"]["name"], "Int")

    def test_match_expression_incompatible_arms(self):
        prog = parse_source(
            'fn f(x: Int) -> None {'
            ' let s: String = match (x) { 1 => 1, _ => "s" };'
            "}"
        )
        errors = run_sa_with_errors(prog).errors
        self.assertTrue(
            any("incompatible value types" in e.message for e in errors)
        )

    def test_match_expression_numeric_promotion(self):
        prog = parse_source(
            "fn f(t: Tuple<Int8, Int8>) -> Int8 {"
            " let x: Int8 = match (t) { (1, v) => v, _ => -1 };"
            " return x;"
            "}"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])
        m = TestSa._find_first(prog, A.MatchStmt)
        self.assertEqual(m._typed_ann["type"]["name"], "Int")
        self.assertEqual(m.arms[1]._typed_ann["body_type"]["name"], "Int")

    def test_match_expression_mixed_arms_rejected(self):
        prog = parse_source(
            "fn f(x: Int) -> Int {"
            " let y: Int = match (x) {"
            "  1 => { return 1; },"
            "  _ => 0"
            " };"
            " return y;"
            "}"
        )
        errors = run_sa_with_errors(prog).errors
        self.assertTrue(
            any("all blocks or all expressions" in e.message for e in errors)
        )

    def test_block_arms_in_expression_position_rejected(self):
        prog = parse_source(
            "fn f(x: Int) -> Int {"
            " let y: Int = match (x) {"
            "  1 => { return 1; },"
            "  _ => { return 0; }"
            " };"
            " return y;"
            "}"
        )
        errors = run_sa_with_errors(prog).errors
        self.assertTrue(
            any("match used as an expression needs expression arms"
                in e.message for e in errors)
        )


class TestEnums(unittest.TestCase):
    def test_payload_enum_construction_and_match(self):
        prog = parse_source(
            "enum Option<T> { Some(T), None }"
            "fn f(o: Option<Int>) -> Int {"
            " let x: Option<Int> = Option::Some(5);"
            " match (o) {"
            "  Option::Some(v) => { return v; },"
            "  Option::None => { return 0; }"
            " }"
            "}"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])
        call = TestSa._find_first(prog, A.Call)
        self.assertEqual(call._typed_ann["call"]["callee_kind"], "enum_variant")
        self.assertEqual(call._typed_ann["variant_index"], 0)
        self.assertEqual(call._typed_ann["type"]["name"], "Option")
        self.assertEqual(
            [a["name"] for a in call._typed_ann["type"]["args"]], ["Int"]
        )

    def test_payload_type_mismatch(self):
        prog = parse_source(
            "enum Option<T> { Some(T), None }"
            'fn f() -> Option<Int> { return Option::Some("x"); }'
        )
        errors = run_sa_with_errors(prog).errors
        self.assertTrue(
            any(
                "return type mismatch" in e.message
                and "Option<String>" in e.message
                for e in errors
            )
        )

    def test_enum_match_exhaustiveness(self):
        missing = parse_source(
            "enum Color { Red, Green, Blue }"
            "fn f(c: Color) -> Int {"
            " match (c) { Color::Red => { return 1; },"
            "              Color::Green => { return 2; } }"
            "}"
        )
        errors = run_sa_with_errors(missing).errors
        self.assertTrue(
            any("match is not exhaustive" in e.message for e in errors)
        )
        covered = parse_source(
            "enum Color { Red, Green, Blue }"
            "fn f(c: Color) -> Int {"
            " match (c) { Color::Red => { return 1; },"
            "              Color::Green => { return 2; },"
            "              Color::Blue => { return 3; } }"
            "}"
        )
        self.assertEqual(run_sa_with_errors(covered).errors, [])

    def test_payload_variant_requires_args_in_pattern(self):
        prog = parse_source(
            "enum Option<T> { Some(T), None }"
            "fn f(o: Option<Int>) -> Int {"
            " match (o) { Option::Some => { return 1; }, _ => { return 0; } }"
            "}"
        )
        errors = run_sa_with_errors(prog).errors
        self.assertTrue(
            any("carries a payload; use 'Option::Some(p1, p2)'" in e.message
                for e in errors)
        )

    def test_unit_variant_pattern_rejects_payload(self):
        prog = parse_source(
            "enum Option<T> { Some(T), None }"
            "fn f(o: Option<Int>) -> Int {"
            " match (o) { Option::None(x) => { return 1; },"
            "              _ => { return 0; } }"
            "}"
        )
        errors = run_sa_with_errors(prog).errors
        self.assertTrue(
            any("takes no payload" in e.message for e in errors)
        )

    def test_bare_payload_variant_expression_rejected(self):
        prog = parse_source(
            "enum Option<T> { Some(T), None }"
            "fn f() -> Option<Int> { return Option::Some; }"
        )
        errors = run_sa_with_errors(prog).errors
        self.assertTrue(
            any("carries a payload and must be constructed" in e.message
                for e in errors)
        )

    def test_enum_variant_pattern_annotations(self):
        prog = parse_source(
            "enum Option<T> { Some(T), None }"
            "fn f(o: Option<Int>) -> Int {"
            " match (o) { Option::Some(v) => { return v; },"
            "              Option::None => { return 0; } }"
            "}"
        )
        run_sa(prog)
        m = TestSa._find_first(prog, A.MatchStmt)
        pat = m.arms[0].pattern
        self.assertEqual(pat._typed_ann["enum"], "Option")
        self.assertEqual(pat._typed_ann["variant_index"], 0)
        self.assertEqual(
            pat.elems[0]._typed_ann["type"]["name"], "Int"
        )

    def test_enum_payload_with_same_named_generic_no_recursion(self):
        """``Option::Some(top_node)`` inside ``extra<T> ...`` maps the enum's
        ``T`` to ``Node<T>``; the string substitution must not recurse into
        the replacement (scope collision, previously SOF)."""
        prog = parse_source(
            "enum Option<T> { None, Some(T) }"
            "struct Node<T> { v: T }"
            "fn wrap<T>(n: Node<T>) -> Option<Node<T>> {"
            " return Option::Some(n);"
            "}"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])

    def test_generic_enum_method_self_referential_payload(self):
        prog = parse_source(
            "enum Option<T> { None, Some(T) }"
            "struct Node<T> { v: T }"
            "struct Heap<T> { nodes: Vector<Option<Node<T>>> }"
            "extra<T> Heap<T> {"
            " fn wrap(self, n: Node<T>) -> Option<Node<T>> {"
            "  return Option::Some(n);"
            " }"
            "}"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])


class TestNeverType(unittest.TestCase):
    def test_never_type_flows_anywhere(self):
        prog = parse_source(
            "fn abort() -> ! { exit(1); }"
            "fn f(x: Int) -> Int { return abort(); }"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])

    def test_never_function_must_diverge(self):
        prog = parse_source("fn abort() -> ! { print(1); }")
        errors = run_sa_with_errors(prog).errors
        self.assertTrue(
            any("does not diverge" in e.message for e in errors)
        )

    def test_never_function_rejects_normal_return(self):
        prog = parse_source("fn abort() -> ! { return 5; }")
        errors = run_sa_with_errors(prog).errors
        self.assertTrue(
            any("return type mismatch" in e.message for e in errors)
        )

    def test_cannot_declare_never_value(self):
        prog = parse_source(
            "fn abort() -> ! { exit(1); }"
            "fn f() -> None { let x: ! = abort(); }"
        )
        errors = run_sa_with_errors(prog).errors
        self.assertTrue(
            any(
                "cannot declare a value of type '!'" in e.message
                for e in errors
            )
        )

    def test_never_arm_coercion(self):
        prog = parse_source(
            "enum Option<T> { None, Some(T) }"
            "fn abort() -> ! { exit(1); }"
            "fn f(o: Option<Int>) -> Int {"
            " return match (o) {"
            "  Option::Some(v) => v,"
            "  Option::None => abort()"
            " };"
            "}"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])
        m = TestSa._find_first(prog, A.MatchStmt)
        self.assertEqual(m._typed_ann["type"]["name"], "Int")


class TestGenericFieldTypeChecking(unittest.TestCase):
    """Generic field access must keep a structured opaque type so compile-time
    checks still run (previously blanked, silently accepting everything)."""

    @staticmethod
    def _heap_prog(body: str) -> str:
        return (
            "enum Option<T> { None, Some(T) }"
            "struct Node<T> { v: T }"
            "struct Heap<T> { nodes: Vector<Option<Node<T>>> }"
            f"extra<T> Heap<T> {{ {body} }}"
        )

    def test_generic_field_assign_mismatch_checked(self):
        prog = self._heap_prog(
            "fn insert(self, n: Node<T>) -> None { self.nodes[0] = n; }"
        )
        errors = run_sa_with_errors(parse_source(prog)).errors
        self.assertTrue(
            any(
                "cannot assign Node<T> to Option<Node<T>>" in e.message
                for e in errors
            )
        )

    def test_generic_let_mismatch_checked(self):
        prog = self._heap_prog(
            "fn pop(self) -> Node<T> {"
            " let n: Node<T> = self.nodes[1];"
            " return n;"
            "}"
        )
        errors = run_sa_with_errors(parse_source(prog)).errors
        self.assertTrue(
            any(
                "cannot initialize Node<T> with Option<Node<T>>"
                in e.message
                for e in errors
            )
        )

    def test_enum_member_access_rejected(self):
        prog = self._heap_prog(
            "fn peek(self) -> UInt { return self.nodes[0].k; }"
        )
        errors = run_sa_with_errors(parse_source(prog)).errors
        self.assertTrue(
            any(
                "type 'Option' has no member 'k'" in e.message
                for e in errors
            )
        )

    def test_valid_generic_field_flow_still_ok(self):
        prog = parse_source(
            "struct Box<T> { pub x: T, }"
            "extra<T> Box<T> {"
            " fn set(self, v: T) -> None { self.x = v; }"
            " fn get(self) -> T { return self.x; }"
            "}"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])


class TestAssociatedTypes(unittest.TestCase):
    def test_valid_impl_with_assoc_type(self):
        prog = parse_source(
            "enum Option<T> { None, Some(T) }"
            "trait Iter {"
            " type Item;"
            " fn next(self) -> Option<Self::Item>;"
            "}"
            "struct R { v: Int32 }"
            "impl Iter for R {"
            " type Item = Int32;"
            " fn next(self) -> Option<Self::Item> {"
            "  return Option::Some(self.v);"
            " }"
            "}"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])

    def test_missing_assoc_type_reported(self):
        prog = parse_source(
            "trait Iter {"
            " type Item;"
            " fn next(self) -> Option<Self::Item>;"
            "}"
            "struct R { }"
            "impl Iter for R {"
            " fn next(self) -> Option<Int32> { return Option::None; }"
            "}"
        )
        errors = run_sa_with_errors(prog).errors
        self.assertTrue(
            any(
                "does not provide associated type 'Item'" in e.message
                for e in errors
            )
        )

    def test_undeclared_assoc_type_reported(self):
        prog = parse_source(
            "trait Iter { fn next(self) -> Option<Int32>; }"
            "struct R { }"
            "impl Iter for R {"
            " type Item = Int32;"
            " fn next(self) -> Option<Int32> { return Option::None; }"
            "}"
        )
        errors = run_sa_with_errors(prog).errors
        self.assertTrue(
            any(
                "trait 'Iter' has no associated type 'Item'" in e.message
                for e in errors
            )
        )

    def test_assoc_type_conformance(self):
        prog = parse_source(
            "trait Iter {"
            " type Item;"
            " fn next(self) -> Option<Self::Item>;"
            "}"
            "struct R { }"
            "impl Iter for R {"
            " type Item = String;"
            " fn next(self) -> Option<Int32> { return Option::None; }"
            "}"
        )
        errors = run_sa_with_errors(prog).errors
        self.assertTrue(
            any(
                "trait requires Option<String>" in e.message
                for e in errors
            )
        )

    def test_builtin_trait_requires_all_methods(self):
        cases = [
            (
                "struct V {} struct S {} impl Into<S> for V {}",
                "impl of 'Into' does not implement 'into'",
            ),
            (
                "struct S {} impl Add for S {}",
                "impl of 'Add' does not implement 'add'",
            ),
            (
                "struct S {} impl Display for S {}",
                "impl of 'Display' does not implement 'to_string'",
            ),
        ]
        for src, needle in cases:
            with self.subTest(src=src):
                errors = run_sa_with_errors(parse_source(src)).errors
                self.assertTrue(
                    any(needle in e.message for e in errors),
                    errors,
                )

    def test_builtin_trait_signature_mismatch(self):
        prog = parse_source(
            "struct V {} struct S {}"
            "impl From<V> for S {"
            " fn from() {}"
            " fn into() {}"
            "}"
        )
        errors = run_sa_with_errors(prog).errors
        self.assertTrue(
            any(
                "method 'into' is not declared by built-in trait 'From'"
                in e.message
                for e in errors
            )
        )
        self.assertTrue(
            any(
                "method 'from' of 'From' expects 0 parameter(s), "
                "trait requires 1"
                in e.message
                for e in errors
            )
        )

    def test_duplicate_from_and_into_impl_rejected(self):
        prog = parse_source(
            "struct V {} struct S {}"
            "impl Into<S> for V { fn into(self) -> S { return S {}; } }"
            "impl From<V> for S { fn from(v: V) -> S { return S {}; } }"
        )
        errors = run_sa_with_errors(prog).errors
        self.assertTrue(
            any("duplicate 'into()' for V -> S" in e.message for e in errors)
        )

    def test_from_impl_self_generic_binding(self):
        prog = parse_source(
            "impl<T> From<Vector<T>> for Set<T> {"
            " fn from(array: Vector<T>) -> Self<T> {"
            "   let result: Set<T> = Set::new();"
            "   for ele in array {"
            "   }"
            "   return result;"
            " }"
            "}"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])

    def test_empty_for_in_body_parses(self):
        prog = parse_source(
            "impl<T> From<Vector<T>> for Set<T> {"
            " fn from(array: Vector<T>) -> Self<T> {"
            "   let result: Set<T> = Set::new();"
            "   for ele in array {"
            "   }"
            "   return result;"
            " }"
            "}"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])

    def test_print_requires_display(self):
        bad = parse_source(
            "struct User {}"
            "fn main() { let u: User = User {}; print(u); }"
        )
        errors = run_sa_with_errors(bad).errors
        self.assertTrue(
            any(
                "does not implement 'Display::to_string'" in e.message
                for e in errors
            )
        )

        from cwind_frontend.typed_ast import build_typed_ast

        ok = parse_source(
            "struct User {}"
            "impl Display for User {"
            ' fn to_string(&self) -> String { return "UserString"; }'
            "}"
            "fn main() { let u: User = User {}; print(u); }"
        )
        result = run_sa_with_errors(ok)
        self.assertEqual(result.errors, [])
        doc = build_typed_ast(ok, result.info)
        print_call = next(
            n for n in _typed_nodes(doc["ast"])
            if n["kind"] == "Call"
            and n.get("ann", {}).get("call", {}).get("callee_ref") == "print"
        )
        arg = print_call["args"][0]["value"]
        self.assertEqual(arg["kind"], "Call")
        self.assertEqual(arg["callee"]["name"], "to_string")

    def test_format_arity_rejects_extra_and_missing_args(self):
        for src, needle in (
            (
                'fn f() -> String { return "{}".format(1, 2, 3); }',
                "has 1 placeholder(s) but got 3 argument(s)",
            ),
            (
                'fn f() -> String { return "{} {}".format(1); }',
                "has 2 placeholder(s) but got 1 argument(s)",
            ),
        ):
            with self.subTest(src=src):
                errors = run_sa_with_errors(parse_source(src)).errors
                self.assertTrue(
                    any(needle in e.message for e in errors),
                    errors,
                )

    def test_user_function_argument_moves_ownership(self):
        prog = parse_source(
            "fn consume(v: Vector<Int>) -> None {}"
            "fn main() {"
            " let v: Vector<Int> = [1];"
            " consume(v);"
            " print(v);"
            "}"
        )
        errors = run_sa_with_errors(prog).errors
        self.assertTrue(
            any("value 'v' is used after move" in e.message for e in errors)
        )

    def test_from_into_moves_source(self):
        prog = parse_source(
            "impl<T> From<Vector<T>> for Set<T> {"
            " fn from(array: Vector<T>) -> Set<T> {"
            "   let result: Set<T> = Set::new();"
            "   for ele in array {"
            "     result.add(ele);"
            "   }"
            "   return result;"
            " }"
            "}"
            "fn main() {"
            " let v: Vector<Int> = [1, 1];"
            " let s: Set<Int> = v.into();"
            " print(v);"
            "}"
        )
        errors = run_sa_with_errors(prog).errors
        self.assertTrue(
            any("value 'v' is used after move" in e.message for e in errors)
        )

    def test_borrow_expression_does_not_move(self):
        prog = parse_source(
            "fn peek(v: &Vector<Int>) -> UInt { return v.length(); }"
            "fn main() {"
            " let v: Vector<Int> = [1, 2];"
            " print(peek(&v));"
            " print(peek(&v));"
            "}"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])

    def test_generic_borrow_parameter_infers(self):
        prog = parse_source(
            "fn first_len<T>(v: &Vector<T>) -> UInt { return v.length(); }"
            "fn main() {"
            " let v: Vector<Int> = [1, 2];"
            " print(first_len(&v));"
            "}"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])

    def test_borrow_argument_cannot_feed_by_value_param(self):
        prog = parse_source(
            "fn consume(v: Vector<Int>) -> None {}"
            "fn main() {"
            " let v: Vector<Int> = [1];"
            " consume(&v);"
            "}"
        )
        errors = run_sa_with_errors(prog).errors
        self.assertTrue(errors)

    def test_ref_self_method_keeps_receiver_usable(self):
        prog = parse_source(
            "struct S { x: Int }"
            "extra S { fn get(&self) -> Int { return self.x; } }"
            "fn main() {"
            " let s: S = S { 1 };"
            " print(s.get());"
            " print(s.get());"
            "}"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])

    def test_plain_self_moves_receiver(self):
        prog = parse_source(
            "struct S { x: Int }"
            "extra S { fn take(self) -> Int { return self.x; } }"
            "fn main() {"
            " let s: S = S { 1 };"
            " print(s.take());"
            " print(s.take());"
            "}"
        )
        errors = run_sa_with_errors(prog).errors
        self.assertTrue(
            any("value 's' is used after move" in e.message for e in errors)
        )

    def test_by_value_self_rejects_reference_receiver(self):
        prog = parse_source(
            "struct S { x: Int }"
            "extra S { fn take(self) -> Int { return self.x; } }"
            "fn main() {"
            " let s: S = S { 1 };"
            " print((&s).take());"
            "}"
        )
        errors = run_sa_with_errors(prog).errors
        self.assertTrue(
            any(
                "cannot call by-value method 'take' on a reference"
                in e.message
                for e in errors
            )
        )

    def test_manual_into_self_moves_source(self):
        prog = parse_source(
            "impl Into<Set<Int>> for Vector<Int> {"
            " fn into(self) -> Set<Int> {"
            "   let result: Set<Int> = Set::new();"
            "   for ele in self { result.add(ele); }"
            "   return result;"
            " }"
            "}"
            "fn main() {"
            " let v: Vector<Int> = [1, 1];"
            " let s: Set<Int> = v.into();"
            " print(v);"
            "}"
        )
        errors = run_sa_with_errors(prog).errors
        self.assertTrue(
            any("value 'v' is used after move" in e.message for e in errors)
        )

    def test_group_refinement_type_checks(self):
        bad = parse_source(
            "type Age = Int where { self < 0; }"
            "struct User1 { name: String, age: UInt }"
            "group Bad: User1 {"
            " self.name -> Age;"
            " self.age -> Age;"
            "}"
        )
        errors = run_sa_with_errors(bad).errors
        self.assertTrue(
            any(
                "group distribution 'name -> Age' cannot receive String"
                in e.message
                for e in errors
            )
        )
        self.assertTrue(
            any(
                "group distribution 'age -> Age' cannot receive UInt"
                in e.message
                for e in errors
            )
        )

        ok = parse_source(
            "type Age = Int where { self < 0; }"
            "struct User2 { age: Int }"
            "group G(a: Int) { a -> Age; }"
            "G@User2 -> { age }"
        )
        self.assertEqual(run_sa_with_errors(ok).errors, [])

        bad_apply = parse_source(
            "type Age = Int where { self < 0; }"
            "struct User2 { age: Int }"
            "group G(a: Int) { a -> Age; }"
            "G@User2 -> {}"
        )
        errors = run_sa_with_errors(bad_apply).errors
        self.assertTrue(
            any(
                "group 'G' expects 1 field(s), got 0"
                in e.message
                for e in errors
            )
        )


def _typed_nodes(root):
    """Yield AST node dicts (nodes carry ``kind``; plain type objects do not)."""
    if "kind" not in root:
        return
    yield root
    for value in root.values():
        if isinstance(value, dict) and "kind" in value:
            yield from _typed_nodes(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and "kind" in item:
                    yield from _typed_nodes(item)


if __name__ == "__main__":
    unittest.main()
