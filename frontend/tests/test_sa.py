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
    "    for (word: args) { output(word); }\n"
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
            'impl DisplayJson for Map { fn str(self) -> String { return "{}".format(); } }'
            "fn f(m: Map) -> String { return m.str(); }"
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
        prog = parse_source("fn f(m: Map) -> String { return m.to_string(); }")
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
