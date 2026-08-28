"""Unit tests for cwind_frontend.sa (串联脚本).

Sources live in ``cases/sa`` (+ ``cases/common/compact_program.wind``);
expectations that are plain pipeline outcomes sit in ``<name>.json``
sidecars (see harness.py).  Structural annotation assertions (_typed_ann
walks) stay in this module and read their input programs from case files.
"""

import json
import sys
import unittest
from dataclasses import fields as _dc_fields
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

import harness

from cwind_frontend import SaError, parse_source, run_sa, run_sa_with_errors
from cwind_frontend.ast_components import ast as A

SA = "sa"


def _table(name):
    return json.loads(
        (harness.CASES_DIR / SA / f"{name}.json").read_bytes()
    )


def sa_prog(name, area=SA):
    return parse_source(harness.source(area, name))


def walk_nodes(node):
    """Yield every AST node reachable via dataclass fields (depth-first)."""
    yield node
    for f in _dc_fields(node):
        if f.name in ("line", "column"):
            continue
        v = getattr(node, f.name)
        if isinstance(v, A.Node):
            yield from walk_nodes(v)
        elif isinstance(v, list):
            for x in v:
                if isinstance(x, A.Node):
                    yield from walk_nodes(x)


class TestSa(harness.CaseAssertionsMixin):
    def test_collect_symbols(self):
        info = run_sa(sa_prog("collect_symbols"))
        names = {s.name for s in info.symbols.values()}
        self.assertEqual(names, {"a", "S", "f"})
        self.assertEqual(info.symbols["S"].kind, "struct")

    # -- mutability ---------------------------------------------------------

    def test_tail_return_and_mut_bindings(self):
        self.assert_case(SA, "tail_return_and_mut_bindings")

    def test_immutable_binding_cannot_reassign(self):
        self.assert_case(SA, "immutable_binding_cannot_reassign")

    def test_mutable_binding_can_reassign(self):
        self.assert_case(SA, "mutable_binding_can_reassign")

    def test_immutable_binding_field_write_rejected(self):
        self.assert_case(SA, "immutable_binding_field_write_rejected")

    def test_parameter_requires_mut_for_assignment(self):
        self.assert_case(SA, "parameter_requires_mut_for_assignment")

    def test_for_loop_variable_is_immutable(self):
        self.assert_case(SA, "for_loop_variable_is_immutable")

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

    # -- numeric widths -------------------------------------------------------

    def test_new_int_widths_accept(self):
        self.assert_case(SA, "new_int_widths_accept")

    def test_new_int_widths_reject_overflow(self):
        for t, lit in _table("int_overflow"):
            with self.subTest(t=t, lit=lit):
                src = f"fn f() -> None {{ let x: {t} = {lit}; }}"
                self.check_outcome(harness.run_pipeline(src),
                                   {"errors": [f"does not fit in {t}"]})

    def test_float64_range(self):
        huge = "1" + "0" * 309  # ~1e309 > f64 max
        src = f"fn f() -> None {{ let x: Float64 = {huge}; }}"
        self.check_outcome(harness.run_pipeline(src),
                           {"errors": ["does not fit in Float64"]})

    def test_mixed_numeric_compare(self):
        for name in ("mixed_cmp_float_int", "mixed_cmp_int_float",
                     "mixed_cmp_int8_float64", "mixed_cmp_uint32_int64"):
            with self.subTest(case=name):
                self.assert_case(SA, name)

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
        for t, lit, msg_t in _table("float_exactness"):
            with self.subTest(t=t, lit=lit):
                src = f"fn f() -> None {{ let x: {t} = {lit}; }}"
                self.check_outcome(harness.run_pipeline(src), {
                    "errors": [f"is not exactly representable in {msg_t}"],
                })

    # -- duplicates / unknown types -------------------------------------------

    def test_duplicate_definition(self):
        self.assert_case(SA, "duplicate_definition")

    def test_unknown_type(self):
        self.assert_case(SA, "unknown_type")

    def test_builtin_redefinition(self):
        self.assert_case(SA, "builtin_redefinition")

    def test_group_apply_references(self):
        self.assert_case(SA, "group_apply_missing_group")
        self.assert_case(SA, "group_apply_missing_field")

    def test_group_binding_validates_self_field(self):
        self.assert_case(SA, "group_self_field_ok")
        self.assert_case(SA, "group_self_field_bad")

    def test_impl_references(self):
        self.assert_case(SA, "impl_missing_references")

    def test_run_sa_collects_many(self):
        self.assert_case(SA, "run_sa_collects_many")

    # -- body checks -------------------------------------------------------------

    def test_unknown_identifier_in_body(self):
        self.assert_case(SA, "unknown_identifier_in_body")

    def test_return_type_mismatch(self):
        self.assert_case(SA, "return_type_mismatch")

    def test_condition_must_be_bool(self):
        self.assert_case(SA, "condition_must_be_bool")

    def test_break_continue_inside_loops(self):
        self.assert_case(SA, "break_continue_inside_loops")

    def test_break_continue_outside_loops(self):
        self.assert_case(SA, "break_outside_loop")
        self.assert_case(SA, "continue_outside_loop")

    def test_break_continue_do_not_escape_inner_loop(self):
        self.assert_case(SA, "break_continue_do_not_escape_inner_loop")

    def test_arity_mismatch(self):
        self.assert_case(SA, "arity_mismatch")

    def test_unknown_method_on_builtin(self):
        self.assert_case(SA, "unknown_method_on_builtin")

    def test_builtin_methods_resolve(self):
        self.assert_case(SA, "builtin_methods_resolve")

    def test_duplicate_let_in_scope(self):
        self.assert_case(SA, "duplicate_let_in_scope")

    def test_struct_field_and_method(self):
        self.assert_case(SA, "struct_field_and_method")

    def test_impl_for_builtin_type(self):
        self.assert_case(SA, "impl_for_builtin_type")

    def test_builtin_trait_impl(self):
        self.assert_case(SA, "builtin_trait_impl")

    # -- format -----------------------------------------------------------------

    def test_format_rejects_placeholder_arity_mismatch(self):
        self.assert_case(SA, "format_placeholder_arity_mismatch")

    def test_format_brace_balance_ok(self):
        self.assert_case(SA, "format_brace_balance_ok")
        self.assert_case(SA, "format_brace_balance_escapes")

    def test_format_unclosed_brace_error(self):
        self.assert_case(SA, "format_unclosed_brace")

    def test_format_stray_brace_error(self):
        self.assert_case(SA, "format_stray_brace")

    def test_display_to_string_on_builtin(self):
        self.assert_case(SA, "display_to_string_on_builtin")

    # -- positions / builtin arg checks ---------------------------------------------

    def test_unknown_type_error_points_at_type_name(self):
        self.assert_case(SA, "unknown_type_error_position")

    def test_param_type_check_builtin(self):
        self.assert_case(SA, "param_type_check_builtin")

    def test_same_as_generic_check(self):
        self.assert_case(SA, "generic_arg_type_check")

    def test_same_as_generic_position_check(self):
        self.assert_case(SA, "map_set_args_swapped")
        self.assert_case(SA, "map_set_args_ok")

    def test_builtin_none_object(self):
        self.assert_case(SA, "none_object_ok")
        self.assert_case(SA, "return_none_ok")

    def test_user_fn_arg_type(self):
        self.assert_case(SA, "user_fn_arg_type")

    def test_bool_literal(self):
        self.assert_case(SA, "bool_literal_ok")
        self.assert_case(SA, "bool_literal_caps")

    def test_generic_trait_sa(self):
        self.assert_case(SA, "generic_trait_sa")

    def test_generic_impl_sa(self):
        self.assert_case(SA, "generic_impl_sa")

    def test_generic_impl_on_generic_builtin(self):
        self.assert_case(SA, "generic_impl_on_generic_builtin")

    # -- literals / consts --------------------------------------------------------

    def test_negative_literal_into_unsigned(self):
        self.assert_case(SA, "negative_literal_into_unsigned")

    def test_literal_out_of_range(self):
        self.assert_case(SA, "literal_out_of_range")

    def test_const_expression_overflow(self):
        self.assert_case(SA, "const_expression_overflow")

    def test_const_reference_checked_in_return(self):
        self.assert_case(SA, "const_reference_checked_in_return")

    def test_generic_args_on_non_generic_struct(self):
        self.assert_case(SA, "generic_args_on_non_generic_struct")

    def test_trait_args_substitution(self):
        self.assert_case(SA, "trait_args_substitution")

    # -- typedefs -----------------------------------------------------------------

    def test_typedef_explicit_generic_params(self):
        self.assert_case(SA, "typedef_explicit_generic_params")

    def test_typedef_rejects_implicit_generics(self):
        self.assert_case(SA, "typedef_rejects_implicit_generics")

    def test_typedef_usage_and_arity(self):
        self.assert_case(SA, "typedef_usage_ok")
        self.assert_case(SA, "typedef_usage_arity")

    def test_typedef_method_resolution(self):
        self.assert_case(SA, "typedef_method_resolution")

    def test_typedef_explicit_params(self):
        self.assert_case(SA, "typedef_explicit_params")

    def test_typedef_concrete_alias_and_literal(self):
        self.assert_case(SA, "typedef_concrete_alias_and_literal")

    def test_typedef_in_collections_and_params(self):
        self.assert_case(SA, "typedef_in_collections_and_params")

    # -- struct construct ----------------------------------------------------------

    def test_struct_construct_field_type_mismatch(self):
        self.assert_case(SA, "struct_construct_field_type_mismatch")

    def test_struct_construct_field_type_ok(self):
        self.assert_case(SA, "struct_construct_field_type_ok")

    def test_struct_construct_positional_count(self):
        self.assert_case(SA, "struct_construct_positional_count")

    def test_alias_arg_to_function(self):
        self.assert_case(SA, "alias_arg_to_function")

    def test_alias_struct_construct_matching(self):
        self.assert_case(SA, "alias_struct_construct_matching")

    def test_alias_mismatch_message_shows_expansion(self):
        self.assert_case(SA, "alias_mismatch_message_shows_expansion")

    def test_alias_to_numeric_and_bool(self):
        self.assert_case(SA, "alias_to_numeric_and_bool")

    def test_fn_generic_params_sa(self):
        self.assert_case(SA, "fn_generic_params_sa")

    # -- initialization ---------------------------------------------------------------

    def test_uninitialized_variable_use(self):
        self.assert_case(SA, "uninitialized_variable_use")
        self.assert_case(SA, "uninitialized_then_assign_ok")

    def test_uninitialized_use_and_missing_return(self):
        self.assert_case(SA, "uninitialized_use_and_missing_return")

    # -- built-in statics ---------------------------------------------------------------

    def test_builtin_static_constructor_new(self):
        # Variadic `new` with values is temporarily not allowed (see the
        # comment in builtin_methods.toml): only the zero-argument form is
        # part of the current behavior.
        for name in ("vector_new_zero_args", "set_new_zero_args",
                     "map_new_zero_args"):
            self.assert_case(SA, name)
        for call in _table("builtin_new_arity")["calls"]:
            src = f"fn f() -> None {{ let v: Vector<Int> = {call}; }}"
            with self.subTest(call=call):
                self.check_outcome(harness.run_pipeline(src),
                                   {"errors": ["expects 0 argument(s)"]})

    def test_builtin_instance_method_not_callable_statically(self):
        self.assert_case(SA, "static_instance_method_rejected")

    # -- strings / enums ------------------------------------------------------------------

    def test_string_plus_number_rejected(self):
        self.assert_case(SA, "string_plus_number_rejected")
        self.assert_case(SA, "string_plus_string_ok")

    def test_enum_path_and_equality(self):
        self.assert_case(SA, "enum_path_equality_ok")
        self.assert_case(SA, "enum_path_return_mismatch")

    # -- which hooks ------------------------------------------------------------------------

    def test_which_validation(self):
        self.assert_case(SA, "which_validation")

    def test_which_restrictions(self):
        self.assert_case(SA, "which_hook_itself")
        self.assert_case(SA, "which_chain")
        self.assert_case(SA, "which_duplicate_hook")
        self.assert_case(SA, "which_cross_block")

    def test_static_access_rules(self):
        self.assert_case(SA, "static_access_rules")

    # -- bounds / extras ----------------------------------------------------------------------

    def test_generic_bounds(self):
        prog = sa_prog("generic_bounds")
        result = run_sa_with_errors(prog)
        messages = [e.message for e in result.errors]
        self.assertTrue(
            any("type 'Int' does not satisfy bound 'Named'" in m
                for m in messages)
        )
        self.assertFalse(
            any("does not satisfy bound" in m and "Point2D" in m
                for m in messages)
        )

    def test_extra_generic_mismatch(self):
        self.assert_case(SA, "extra_generic_mismatch")

    def test_duplicate_impl_and_field(self):
        self.assert_case(SA, "duplicate_impl_and_field")

    def test_const_reassignment_and_div_zero(self):
        self.assert_case(SA, "const_reassignment")
        self.assert_case(SA, "const_div_zero")

    def test_equality_type_mismatch(self):
        self.assert_case(SA, "equality_type_mismatch")

    def test_map_literal_value_types(self):
        self.assert_case(SA, "map_value_type_mismatch")
        self.assert_case(SA, "map_value_type_ok")

    def test_map_literal_duplicate_key_rejected(self):
        self.assert_case(SA, "map_duplicate_key_str")
        self.assert_case(SA, "map_duplicate_key_int")
        self.assert_case(SA, "map_duplicate_key_const")

    def test_container_literal_type_propagation(self):
        self.assert_case(SA, "container_literal_widths")
        self.assert_case(SA, "container_nested_map_count")
        self.assert_case(SA, "container_nested_map_ok")

    def test_instance_removed(self):
        self.assert_case(SA, "instance_removed")
        self.assert_case(SA, "instance_user_struct_ok")

    def test_generic_struct_construct_sa(self):
        self.assert_case(SA, "generic_struct_construct_sa")
        self.assert_case(SA, "generic_extra_heap_new")

    def test_map_requires_type_arguments(self):
        self.assert_case(SA, "map_requires_type_arguments")

    def test_non_none_function_must_return(self):
        self.assert_case(SA, "non_none_function_must_return")

    def test_generic_extra_missing_return(self):
        self.assert_case(SA, "generic_extra_missing_return")

    def test_generic_extra_method_return_substituted(self):
        self.assert_case(SA, "generic_extra_method_return_substituted")

    def test_generic_extra_method_arg_substituted(self):
        self.assert_case(SA, "generic_extra_method_arg_substituted")

    def test_generic_struct_field_read_substituted(self):
        self.assert_case(SA, "generic_struct_field_read_substituted")

    def test_generic_struct_field_write_substituted(self):
        self.assert_case(SA, "generic_struct_field_write_substituted")

    def test_generic_fn_call_inferred(self):
        self.assert_case(SA, "generic_fn_call_inferred")

    def test_generic_fn_nested_inference(self):
        self.assert_case(SA, "generic_fn_nested_inference")

    def test_method_generic_params_in_scope(self):
        self.assert_case(SA, "method_generic_params_in_scope")

    def test_method_generic_alpha_equivalence(self):
        self.assert_case(SA, "method_generic_alpha_equivalence")

    def test_generic_static_path_call_inferred(self):
        self.assert_case(SA, "generic_static_path_call_inferred")

    def test_generic_use_site_mismatch_still_detected(self):
        self.assert_case(SA, "use_site_method_arg_mismatch")
        self.assert_case(SA, "use_site_field_assign_mismatch")
        self.assert_case(SA, "use_site_return_mismatch")

    def test_impl_signature_conformance(self):
        self.assert_case(SA, "impl_signature_conformance")

    def test_impl_return_type_must_match_exactly(self):
        self.assert_case(SA, "impl_signature_exact_return")

    def test_impl_signature_missing_method(self):
        self.assert_case(SA, "impl_signature_missing_method")

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

    # -- From/Into -------------------------------------------------------------------------------

    def test_from_into_conversion(self):
        self.assert_case(SA, "from_into_conversion")

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
        self.assert_case(SA, "from_static_call")

    def test_into_return_type_checked(self):
        self.assert_case(SA, "into_return_type_checked")

    def test_string_into_uses_context(self):
        self.assert_case(SA, "string_into_context")

    def test_into_requires_target_type(self):
        self.assert_case(SA, "into_requires_target")

    def test_into_rejects_unsupported_target(self):
        self.assert_case(SA, "into_unsupported_target")

    def test_from_requires_one_arg_and_methods(self):
        self.assert_case(SA, "from_requires_one_arg")
        self.assert_case(SA, "from_impl_needs_from_method")

    # -- generic Into bounds (bug-21) ---------------------------------------------------------------

    def test_generic_bound_into_trait_default_method(self):
        """``T: Into<String>`` lets ``value.into()`` resolve in a trait
        default method (bugs/bug21.wind shape)."""
        self.assert_case(SA, "generic_bound_into")

    def test_generic_bound_into_top_level_fn(self):
        self.assert_case(SA, "generic_bound_into_fn")

    def test_generic_bound_into_extra(self):
        self.assert_case(SA, "generic_bound_into_extra")

    def test_generic_bound_into_missing_rejected(self):
        """Without the bound the call must stay rejected, with a diagnostic
        pointing at the missing bound."""
        self.assert_case(SA, "generic_bound_into_missing")

    def test_generic_bound_does_not_leak_between_traits(self):
        """A bound declared on one trait's parameter must not satisfy an
        unbounded parameter of another trait."""
        self.assert_case(SA, "generic_bound_into_no_leak")

    def test_generic_bound_into_resolves_to_bound_target(self):
        """The resolved into() call carries the bound's target type
        (String) while the receiver itself stays opaque (T)."""
        prog = sa_prog("generic_bound_into")
        result = run_sa_with_errors(prog)
        self.assertEqual(result.errors, [])

        calls = [
            n for n in walk_nodes(prog)
            if isinstance(n, A.Call)
            and n._typed_ann.get("call", {}).get("callee_ref") == "into"
        ]
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call._typed_ann["call"]["callee_kind"], "builtin")
        self.assertEqual(call._typed_ann["type"]["name"], "String")
        receiver = call.callee.obj
        self.assertEqual(receiver._typed_ann["type"]["name"], "T")
        self.assertTrue(receiver._typed_ann["type"].get("opaque"))

    def test_compact_program_sa(self):
        result = run_sa_with_errors(
            parse_source(harness.source("common", "compact_program"))
        )
        self.assertEqual(result.errors, [])
        self.assertGreater(len(result.info.symbols), 6)
        json.dumps(result.info.to_dict())  # must be JSON-serializable

    # -- typed AST ----------------------------------------------------------------------------------

    def _typed_doc(self, name):
        prog = sa_prog(name)
        result = run_sa_with_errors(prog)
        self.assertEqual(result.errors, [])
        from cwind_frontend.typed_ast import build_typed_ast

        return prog, result.info, build_typed_ast(prog, result.info)

    def test_typed_ast_metadata(self):
        _, info, doc = self._typed_doc("typed_ast_metadata")
        # impl bindings record the trait and both node refs
        self.assertEqual(len(info.bindings), 1)
        binding = info.bindings[0]
        self.assertEqual(binding.owner, "P")
        self.assertEqual(binding.trait, "D")
        self.assertEqual(binding.to_dict()["id"], binding.id)
        # every top-level symbol carries the id of its declaration node
        self.assertTrue(all(sym.ref is not None for sym in info.symbols.values()))
        # the typed document is serializable and self-consistent
        json.dumps(doc)
        ast = doc["ast"]
        self.assertEqual(ast["kind"], "Program")
        self.assertEqual(ast["id"], 1)

        nodes = {n["id"]: n for n in _typed_nodes(ast)}
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
        _, _, doc = self._typed_doc("typed_ast_coverage_fixes")
        ast = doc["ast"]
        nodes = list(_typed_nodes(ast))
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
        _, _, doc = self._typed_doc("typed_ast_slice_unary")
        ast = doc["ast"]
        nodes = list(_typed_nodes(ast))
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
        _, _, doc = self._typed_doc("typed_ast_into_conversion")
        call = next(n for n in _typed_nodes(doc["ast"]) if n["kind"] == "Call")
        self.assertEqual(call["ann"]["call"]["callee_kind"], "method")
        self.assertEqual(call["callee"]["parts"], ["T", "from"])
        self.assertEqual(call["ann"]["type"], {"name": "T"})

    def test_typed_ast_which_method(self):
        _, _, doc = self._typed_doc("typed_ast_which_method")
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
        self.assert_case(SA, "which_hook_called_directly")

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
        _, _, doc = self._typed_doc("struct_construct_self_in_extra")
        ast = doc["ast"]
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
        _, _, doc = self._typed_doc("typed_ast_const_reference_binding")
        ast = doc["ast"]
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

    # -- refinements ---------------------------------------------------------------------------------

    def test_refinement_compile_time_checks(self):
        bad_messages = [
            e.message
            for e in run_sa_with_errors(
                sa_prog("refinement_compile_time_checks_bad")
            ).errors
        ]
        self.assertEqual(
            sum("does not satisfy refinement of 'Age'" in m
                for m in bad_messages),
            5,
            bad_messages,
        )
        good = run_sa_with_errors(
            sa_prog("refinement_compile_time_checks_good")
        )
        self.assertEqual(good.errors, [])

    def test_refinement_constructor_flow(self):
        self.assert_case(SA, "refinement_constructor_flow_bad")
        self.assert_case(SA, "refinement_constructor_flow_runtime_arg")

    def test_refinement_inline_field_validation(self):
        self.assert_case(SA, "refinement_inline_validation_bad")
        self.assert_case(SA, "refinement_inline_validation_ok")

    def test_refinement_dead_bound_warning(self):
        # `self < 256` can never fail for Int8 (max 127): the value is blocked
        # by Int8's own range check before refinement ever runs.
        self.assert_case(SA, "refinement_dead_bound_int8")
        # a bound below the minimum is dead too (UInt8 starts at 0)
        self.assert_case(SA, "refinement_dead_bound_uint8_min")
        # a bound above the maximum can never be satisfied at all
        self.assert_case(SA, "refinement_impossible_bound")
        # in-range refinements stay silent
        self.assert_case(SA, "refinement_in_range_silent")

    def test_refinement_checked_in_builtin_method_args(self):
        self.assert_case(SA, "refinement_builtin_push_back_bad")
        self.assert_case(SA, "refinement_builtin_push_back_ok")
        # the same applies to other built-in methods with refined generic
        # arguments, e.g. Map::set / contains
        self.assert_case(SA, "refinement_builtin_map_set_bad")
        # plain width checks apply to built-in method arguments as well
        self.assert_case(SA, "width_checked_in_builtin_args")

    def test_refinement_checked_through_local_constants(self):
        self.assert_case(SA, "refinement_local_const_bad")
        # in-bounds local constants stay valid, including via `let`
        self.assert_case(SA, "refinement_local_const_ok")
        # a chained foldable local is tracked too
        self.assert_case(SA, "refinement_local_const_chain")
        # once a variable is reassigned from an unknown source, its known
        # value is forgotten and the refinement is left to runtime
        self.assert_case(SA, "refinement_local_const_unknown_source")

    def test_refinement_checked_through_function_returns(self):
        self.assert_case(SA, "refinement_fn_return_bad")
        self.assert_case(SA, "refinement_fn_return_ok")
        # function return values also feed width checks and chain through
        # other functions; recursive / parameterized calls stay unknown
        self.assert_case(SA, "fn_return_width_checked")
        self.assert_case(SA, "fn_return_chain_refinement")
        self.assert_case(SA, "fn_param_return_conservative")


class TestTupleAndMapIter(harness.CaseAssertionsMixin):
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
        prog = sa_prog("tuple_literal_typing")
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
        self.assert_case(SA, "bare_tuple_annotation_rejects_nonempty")
        self.assert_case(SA, "bare_tuple_annotation_unit_ok")

    def test_tuple_index_typing(self):
        prog = sa_prog("tuple_index_typing")
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
        self.assert_case(SA, "tuple_index_out_of_range")
        self.assert_case(SA, "tuple_index_dynamic")

    def test_tuple_element_access_typing(self):
        prog = sa_prog("tuple_element_access_typing")
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
        prog = sa_prog("map_forin_var_type")
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
        prog = sa_prog("map_entry_generic_tuple_marker")
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
        self.assert_case(SA, "unknown_generic_bound_impl")
        self.assert_case(SA, "unknown_generic_bound_fn")

    def test_generic_bound_arity_reported(self):
        self.assert_case(SA, "generic_bound_arity_b")
        self.assert_case(SA, "generic_bound_arity_into")


class TestPatternMatching(harness.CaseAssertionsMixin):
    def test_valid_match_and_if_let(self):
        self.assert_case(SA, "match_valid_struct_guards")

    def test_match_exhaustiveness_required(self):
        self.assert_case(SA, "match_not_exhaustive")

    def test_irrefutable_struct_pattern_is_exhaustive(self):
        self.assert_case(SA, "irrefutable_struct_pattern_exhaustive")

    def test_struct_pattern_missing_field(self):
        self.assert_case(SA, "struct_pattern_missing_field")

    def test_pattern_binding_scope_isolated(self):
        self.assert_case(SA, "pattern_binding_scope_isolated")

    def test_pattern_binding_shadowing_allowed(self):
        self.assert_case(SA, "pattern_binding_shadowing_allowed")

    def test_duplicate_binding_rejected(self):
        self.assert_case(SA, "duplicate_pattern_binding")

    def test_tuple_arity_mismatch(self):
        self.assert_case(SA, "tuple_pattern_arity_mismatch")

    def test_guard_must_be_bool(self):
        self.assert_case(SA, "pattern_guard_not_bool")

    def test_pattern_type_mismatch(self):
        self.assert_case(SA, "pattern_type_mismatch_tuple")

    def test_literal_range_checked(self):
        self.assert_case(SA, "pattern_literal_range_checked")

    def test_annotations(self):
        prog = sa_prog("match_annotations")
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
        prog = sa_prog("match_expression_typing")
        self.assertEqual(run_sa_with_errors(prog).errors, [])
        m = TestSa._find_first(prog, A.MatchStmt)
        self.assertEqual(m._typed_ann["type"]["name"], "Int")
        self.assertEqual(m.arms[0]._typed_ann["body_kind"], "expr")
        self.assertEqual(m.arms[0]._typed_ann["body_type"]["name"], "Int")

    def test_match_expression_incompatible_arms(self):
        self.assert_case(SA, "match_expr_incompatible_arms")

    def test_match_expression_numeric_promotion(self):
        prog = sa_prog("match_expression_numeric_promotion")
        self.assertEqual(run_sa_with_errors(prog).errors, [])
        m = TestSa._find_first(prog, A.MatchStmt)
        self.assertEqual(m._typed_ann["type"]["name"], "Int")
        self.assertEqual(m.arms[1]._typed_ann["body_type"]["name"], "Int")

    def test_match_expression_mixed_arms_rejected(self):
        self.assert_case(SA, "match_expr_mixed_arms_rejected")

    def test_block_arms_in_expression_position_rejected(self):
        self.assert_case(SA, "match_block_arms_in_expr_position")


class TestEnums(harness.CaseAssertionsMixin):
    def test_payload_enum_construction_and_match(self):
        prog = sa_prog("enum_payload_construction_match")
        self.assertEqual(run_sa_with_errors(prog).errors, [])
        call = TestSa._find_first(prog, A.Call)
        self.assertEqual(call._typed_ann["call"]["callee_kind"], "enum_variant")
        self.assertEqual(call._typed_ann["variant_index"], 0)
        self.assertEqual(call._typed_ann["type"]["name"], "Option")
        self.assertEqual(
            [a["name"] for a in call._typed_ann["type"]["args"]], ["Int"]
        )

    def test_payload_type_mismatch(self):
        self.assert_case(SA, "enum_payload_type_mismatch")

    def test_enum_match_exhaustiveness(self):
        self.assert_case(SA, "enum_match_missing_variant")
        self.assert_case(SA, "enum_match_covered")

    def test_payload_variant_requires_args_in_pattern(self):
        self.assert_case(SA, "payload_variant_pattern_requires_args")

    def test_unit_variant_pattern_rejects_payload(self):
        self.assert_case(SA, "unit_variant_pattern_rejects_payload")

    def test_bare_payload_variant_expression_rejected(self):
        self.assert_case(SA, "bare_payload_variant_rejected")

    def test_enum_variant_pattern_annotations(self):
        prog = sa_prog("enum_variant_pattern_annotations")
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
        self.assert_case(SA, "enum_same_named_generic_no_recursion")

    def test_generic_enum_method_self_referential_payload(self):
        self.assert_case(SA, "generic_enum_self_referential_payload")


class TestNeverType(harness.CaseAssertionsMixin):
    def test_never_type_flows_anywhere(self):
        self.assert_case(SA, "never_flows_anywhere")

    def test_never_function_must_diverge(self):
        self.assert_case(SA, "never_must_diverge")

    def test_never_function_rejects_normal_return(self):
        self.assert_case(SA, "never_rejects_normal_return")

    def test_cannot_declare_never_value(self):
        self.assert_case(SA, "never_value_declaration")

    def test_never_arm_coercion(self):
        prog = sa_prog("never_arm_coercion")
        self.assertEqual(run_sa_with_errors(prog).errors, [])
        m = TestSa._find_first(prog, A.MatchStmt)
        self.assertEqual(m._typed_ann["type"]["name"], "Int")


class TestGenericFieldTypeChecking(harness.CaseAssertionsMixin):
    """Generic field access must keep a structured opaque type so compile-time
    checks still run (previously blanked, silently accepting everything)."""

    def _heap_prog(self, frag_name: str):
        body = harness.source(SA, frag_name).strip()
        return (
            "enum Option<T> { None, Some(T) }"
            "struct Node<T> { v: T }"
            "struct Heap<T> { nodes: Vector<Option<Node<T>>> }"
            f"extra<T> Heap<T> {{ {body} }}"
        )

    def test_generic_field_assign_mismatch_checked(self):
        errors = run_sa_with_errors(
            parse_source(self._heap_prog("heap_frag_insert_mismatch"))
        ).errors
        self.assertTrue(
            any(
                "cannot assign Node<T> to Option<Node<T>>" in e.message
                for e in errors
            )
        )

    def test_generic_let_mismatch_checked(self):
        errors = run_sa_with_errors(
            parse_source(self._heap_prog("heap_frag_pop_mismatch"))
        ).errors
        self.assertTrue(
            any(
                "cannot initialize Node<T> with Option<Node<T>>"
                in e.message
                for e in errors
            )
        )

    def test_enum_member_access_rejected(self):
        errors = run_sa_with_errors(
            parse_source(self._heap_prog("heap_frag_enum_member_access"))
        ).errors
        self.assertTrue(
            any(
                "type 'Option' has no member 'k'" in e.message
                for e in errors
            )
        )

    def test_valid_generic_field_flow_still_ok(self):
        self.assert_case(SA, "generic_field_flow_valid")


class TestAssociatedTypes(harness.CaseAssertionsMixin):
    def test_valid_impl_with_assoc_type(self):
        self.assert_case(SA, "assoc_type_valid_impl")

    def test_missing_assoc_type_reported(self):
        self.assert_case(SA, "assoc_type_missing_reported")

    def test_undeclared_assoc_type_reported(self):
        self.assert_case(SA, "assoc_type_undeclared_reported")

    def test_assoc_type_conformance(self):
        self.assert_case(SA, "assoc_type_conformance_mismatch")

    def test_builtin_trait_requires_all_methods(self):
        self.assert_case(SA, "builtin_trait_into_missing")
        self.assert_case(SA, "builtin_trait_add_missing")
        self.assert_case(SA, "builtin_trait_display_missing")

    def test_builtin_trait_signature_mismatch(self):
        self.assert_case(SA, "builtin_trait_from_signature_mismatch")

    def test_duplicate_from_and_into_impl_rejected(self):
        self.assert_case(SA, "duplicate_from_into_impls")

    def test_from_impl_self_generic_binding(self):
        self.assert_case(SA, "from_impl_self_generic_binding")

    def test_empty_for_in_body_parses(self):
        self.assert_case(SA, "from_impl_self_generic_binding")

    def test_print_requires_display(self):
        self.assert_case(SA, "print_requires_display_bad")

        prog = sa_prog("print_display_ok")
        result = run_sa_with_errors(prog)
        self.assertEqual(result.errors, [])
        from cwind_frontend.typed_ast import build_typed_ast

        doc = build_typed_ast(prog, result.info)
        print_call = next(
            n for n in _typed_nodes(doc["ast"])
            if n["kind"] == "Call"
            and n.get("ann", {}).get("call", {}).get("callee_ref") == "print"
        )
        arg = print_call["args"][0]["value"]
        self.assertEqual(arg["kind"], "Call")
        self.assertEqual(arg["callee"]["name"], "to_string")

    def test_format_arity_rejects_extra_and_missing_args(self):
        self.assert_case(SA, "format_arity_too_many")
        self.assert_case(SA, "format_arity_missing")

    # -- ownership -------------------------------------------------------------------

    def test_user_function_argument_moves_ownership(self):
        self.assert_case(SA, "user_fn_moves_argument")

    def test_from_into_moves_source(self):
        self.assert_case(SA, "from_into_moves_source")

    def test_borrow_expression_does_not_move(self):
        self.assert_case(SA, "borrow_does_not_move")

    def test_generic_borrow_parameter_infers(self):
        self.assert_case(SA, "generic_borrow_infers")

    def test_borrow_argument_cannot_feed_by_value_param(self):
        self.assert_case(SA, "borrow_fed_to_by_value_param")

    def test_ref_self_method_keeps_receiver_usable(self):
        self.assert_case(SA, "ref_self_keeps_receiver")

    def test_plain_self_moves_receiver(self):
        self.assert_case(SA, "plain_self_moves_receiver")

    def test_assoc_fn_does_not_move_self(self):
        self.assert_case(SA, "assoc_fn_keeps_self")

    def test_assoc_fn_does_not_move_mut_self(self):
        self.assert_case(SA, "assoc_fn_keeps_mut_self")

    def test_by_value_self_still_moves_after_assoc_call(self):
        self.assert_case(SA, "by_value_self_still_moves_after_assoc")

    def test_by_value_self_rejects_reference_receiver(self):
        self.assert_case(SA, "by_value_method_on_reference_rejected")

    def test_manual_into_self_moves_source(self):
        self.assert_case(SA, "manual_into_moves_source")

    def test_group_refinement_type_checks(self):
        self.assert_case(SA, "group_refinement_bad_distributions")
        self.assert_case(SA, "group_refinement_ok_apply")
        self.assert_case(SA, "group_apply_wrong_field_count")

    # -- todo-46: raw-pointer dereference ---------------------------------

    def test_todo46_deref_read_scalar(self):
        self.assert_case("todo46", "deref_read_scalar")

    def test_todo46_deref_write_mut(self):
        self.assert_case("todo46", "deref_write_mut")

    def test_todo46_deref_compound_assign(self):
        self.assert_case("todo46", "deref_compound_assign")

    def test_todo46_deref_struct_member_read(self):
        self.assert_case("todo46", "deref_struct_member")

    def test_todo46_deref_struct_member_write(self):
        self.assert_case("todo46", "deref_struct_member_write")

    def test_todo46_deref_non_pointer_rejected(self):
        self.assert_case("todo46", "deref_non_pointer_rejected")

    def test_todo46_const_pointer_write_rejected(self):
        self.assert_case("todo46", "const_pointer_write_rejected")

    def test_todo46_immutable_pointer_write_rejected(self):
        self.assert_case("todo46", "immutable_pointer_write_rejected")


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
