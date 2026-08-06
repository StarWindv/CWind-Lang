"""Unit tests for cwind_frontend.sa."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cwind_frontend import SaError, parse_source, run_sa, run_sa_with_errors


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
            'impl DisplayJson for Map<String, String> { fn str(self) -> String { return "{}".format(); } }'
            "fn f(m: Map<String, String>) -> String { return m.str(); }"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])

    def test_builtin_trait_impl(self):
        prog = parse_source(
            "struct S { pub v: Int; }"
            "impl Display for S { pub fn to_string(self) -> String { return self.v.to_string(); } }"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])

    def test_format_is_variadic(self):
        prog = parse_source(
            'fn f() -> String { let s: String = "{}".format(1, 2, 3); return s; }'
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])

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
            "argument 1 of 'push_back' must be SameAsGeneric, got String",
            result.errors[0].message,
        )

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
            " fn str(self) -> String { return \"{}\".format(); }"
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
        from cwind_frontend.sa import BUILTIN_MODULE_FUNCTIONS, BUILTIN_TYPE_METHODS

        self.assertTrue(BUILTIN_TYPE_METHODS["String"]["format"].variadic)
        self.assertEqual(
            BUILTIN_TYPE_METHODS["Vector"]["push_back"].args,
            ("Self", "SameAsGeneric"),
        )
        self.assertIn("to_string", BUILTIN_TYPE_METHODS["Map"])  # via Display trait
        # instance methods declare a leading Self; module functions (static)
        # must not.
        for type_name, methods in BUILTIN_TYPE_METHODS.items():
            for spec in methods.values():
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
            " fn into(self) -> Target { return Target { }; }"
            "}"
            "fn f(s: MyS) -> Target { return s.into(); }"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])

    def test_from_static_call(self):
        prog = parse_source(
            "struct MyS { } struct Target { }"
            "impl From<MyS> for Target {"
            " fn from(value: MyS) -> Target { return Target { }; }"
            " fn into(self) -> Target { return Target { }; }"
            "}"
            "fn f(s: MyS) -> Target { return Target::from(s); }"
        )
        self.assertEqual(run_sa_with_errors(prog).errors, [])

    def test_into_return_type_checked(self):
        prog = parse_source(
            "struct MyS { } struct Target { }"
            "impl From<MyS> for Target {"
            " fn from(value: MyS) -> Target { return Target { }; }"
            " fn into(self) -> Target { return Target { }; }"
            "}"
            'fn f(s: MyS) -> String { return s.into(); }'
        )
        result = run_sa_with_errors(prog)
        self.assertEqual(len(result.errors), 1)
        self.assertIn("return type mismatch", result.errors[0].message)

    def test_from_requires_one_arg_and_methods(self):
        result = run_sa_with_errors(parse_source("struct S { } impl From for S {}"))
        self.assertTrue(
            any("From requires one type argument" in e.message for e in result.errors)
        )

        result = run_sa_with_errors(parse_source("struct S { } impl From<Int> for S {}"))
        self.assertTrue(any("must define 'from'" in e.message for e in result.errors))
        self.assertTrue(any("must define 'into'" in e.message for e in result.errors))

    def test_compact_program_sa(self):
        result = run_sa_with_errors(parse_source(_COMPACT_PROGRAM))
        self.assertEqual(result.errors, [])
        self.assertGreater(len(result.info.symbols), 6)
        json.dumps(result.info.to_dict())  # must be JSON-serializable


if __name__ == "__main__":
    unittest.main()
