"""todo-179: procedure macros (``#[proc_macro]``) — unit + end-to-end tests.

Three layers:

* unit tests for the token-level pieces (collection, protocol, dependency
  collection, quote! builtin, registry visibility, driver codec);
* project-tree cases under ``cases/proc_macro/`` for definition/unknown
  behaviour that needs no toolchain (see ``test_cases.py`` discovery);
* end-to-end integration tests that build and run a real macro process —
  skipped when the backend (``cwindc``) is not available.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent.parent.parent
for path in (ROOT / "mvp/frontend/src", ROOT / "mvp/frontend/tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from cwind_frontend.ast_components.token import TokenKind  # noqa: E402
from cwind_frontend.lexer import tokenize, tokenize_file  # noqa: E402
from cwind_frontend.macros import expand_macros  # noqa: E402
from cwind_frontend.macros.proc.builtins import expand_builtin  # noqa: E402
from cwind_frontend.macros.proc.build import definition_key  # noqa: E402
from cwind_frontend.macros.proc.collect import collect_proc_macros  # noqa: E402
from cwind_frontend.macros.proc.deps import generate_program  # noqa: E402
from cwind_frontend.macros.proc.driver import _decode_records, _encode_tokens  # noqa: E402
from cwind_frontend.macros.proc.protocol import (  # noqa: E402
    pairs_to_tokens,
    token_span,
    validated_span,
    token_kind_name,
    tokens_to_pairs,
)
from cwind_frontend.macros.proc.registry import (  # noqa: E402
    ProcMacroRegistry,
)
from cwind_frontend.parser.parser import parse_with_errors  # noqa: E402

MAC = "proc_macro"


def _collect(source: str, source_path: str | None = None):
    tokens = tokenize(source)
    return collect_proc_macros(tokens, source_path)


def _registry_for(sources: dict[str, str]) -> ProcMacroRegistry:
    """A registry whose definitions come from in-memory files."""
    registry = ProcMacroRegistry(ROOT)
    for rel, text in sources.items():
        path = ROOT / rel
        stream, defs, _errors = collect_proc_macros(
            tokenize(text), str(path)
        )
        for definition in defs:
            registry.register(definition)
    return registry


class CollectionTests(unittest.TestCase):
    def test_attribute_definition_is_stripped_and_typed(self):
        for visibility in ("", "pub "):
            with self.subTest(visibility=visibility):
                stream, defs, errors = _collect(
                    "#[proc_macro_attribute] " + visibility
                    + "fn decorate(attr: TokenStream, item: TokenStream)"
                    " -> TokenStream { return item; } fn main() {}", "attrs.wind"
                )
                self.assertEqual([], [e.message for e in errors])
                self.assertEqual(1, len(defs))
                self.assertEqual("attribute", defs[0].kind)
                self.assertEqual(bool(visibility), defs[0].is_pub)
                self.assertEqual("fn main ( ) { }", " ".join(t.raw for t in stream))

    def test_attribute_invalid_signatures_fail_at_definition(self):
        signatures = [
            "a: TokenStream", "a: TokenStream, b: TokenStream, c: TokenStream",
            "self, b: TokenStream", "mut a: TokenStream, b: TokenStream",
            "a: Int, b: TokenStream", "a: TokenStream = x, b: TokenStream",
            "a: Alias, b: Alias",
        ]
        for params in signatures:
            with self.subTest(params=params):
                _, defs, errors = _collect(
                    f"#[proc_macro_attribute] fn bad({params}) -> TokenStream {{}}"
                )
                self.assertTrue(errors)
                self.assertTrue(defs[0].issues)
        for result in ("", "-> Int", "-> &TokenStream"):
            with self.subTest(result=result):
                _, defs, errors = _collect(
                    "#[proc_macro_attribute] fn bad(a: TokenStream, b: TokenStream) "
                    + result + " {}"
                )
                self.assertTrue(errors)
                self.assertTrue(defs[0].issues)

    def test_attribute_payload_and_quote_declarations_are_opaque(self):
        for source in (
            "#[inert(#[proc_macro_attribute] fn hidden(a: TokenStream, b: TokenStream)"
            " -> TokenStream {})] fn keep() {}",
            "quote!(#[proc_macro_attribute] fn hidden(a: TokenStream, b: TokenStream)"
            " -> TokenStream {});",
        ):
            stream, defs, errors = _collect(source)
            self.assertEqual([], defs)
            self.assertEqual([], errors)
            self.assertEqual([t.raw for t in tokenize(source)], [t.raw for t in stream])

    def test_attribute_visibility_and_kind_resolution(self):
        source = "#[proc_macro_attribute] {pub}fn tag(a: TokenStream, b: TokenStream) -> TokenStream {{}}"
        registry = _registry_for({"a.wind": source.format(pub="pub "),
                                  "b.wind": source.format(pub="")})
        for path in ("a.wind", "b.wind", "c.wind"):
            definition, error = registry.lookup("tag", str(ROOT / path), "attribute")
            self.assertIsNone(error)
            assert definition is not None
            self.assertEqual(str(ROOT / ("b.wind" if path == "b.wind" else "a.wind")),
                             definition.source_path)
        definition, error = registry.lookup("tag", str(ROOT / "c.wind"))
        self.assertIsNone(definition)
        assert error is not None
        self.assertIn("attribute macro", error)
        private = _registry_for({"a.wind": source.format(pub="")})
        self.assertEqual((None, None), private.lookup("tag", str(ROOT / "b.wind"), "attribute"))
        private.register(_collect(source.format(pub="pub "), str(ROOT / "b.wind"))[1][0])
        private.register(_collect(source.format(pub="pub "), str(ROOT / "c.wind"))[1][0])
        _, ambiguous = private.lookup("tag", str(ROOT / "d.wind"), "attribute")
        assert ambiguous is not None
        self.assertIn("ambiguous", ambiguous)

    def test_definition_key_and_harness_include_kind(self):
        from dataclasses import replace
        _, defs, errors = _collect(
            "#[proc_macro_attribute] fn tag(a: TokenStream, b: TokenStream) -> TokenStream {}"
        )
        self.assertEqual([], errors)
        attr = defs[0]
        function = replace(attr, kind="function")
        self.assertNotEqual(definition_key(attr), definition_key(function))
        self.assertIn("__cwpm_tag(__pm_input, __pm_item)", generate_program(attr))
        self.assertEqual(2, generate_program(attr).count("stream_from_stdin();"))
        self.assertEqual(1, generate_program(function).count("stream_from_stdin();"))

    def test_attribute_kind_errors_unknown_and_inert_payloads(self):
        from cwind_frontend.macros.proc.expand import ProcMacroContext
        definitions = (
            "#[proc_macro_attribute] fn attr225(a: TokenStream, b: TokenStream) -> TokenStream {}\n"
            "#[proc_macro] fn function225(a: TokenStream) -> TokenStream {}\n"
        )
        for invocation, expected in (
            ("attr225!()", "attribute macro"),
            ("#[function225] fn f() {}", "function macro"),
        ):
            with self.subTest(invocation=invocation):
                _, errors = expand_macros(tokenize(definitions + invocation),
                    iter(range(100)).__next__, proc_context=ProcMacroContext(ROOT),
                    source_path=str(ROOT / "kind225.wind"))
                self.assertTrue(any(expected in e.message for e in errors),
                                [e.message for e in errors])
        text = '#[unknown225(payload225!())] fn f() {}'
        stream, errors = expand_macros(tokenize(text), iter(range(100)).__next__,
                                      proc_context=ProcMacroContext(ROOT))
        self.assertEqual([], errors)
        self.assertEqual([t.raw for t in tokenize(text)], [t.raw for t in stream])
        parsed = parse_with_errors(tokenize("#[unknown225] fn f() {}"))
        self.assertTrue(any("unsupported attribute" in e.message for e in parsed.errors))

    def test_attribute_cfg_replacement_and_token_budget(self):
        from unittest.mock import patch
        from cwind_frontend.macros.proc.expand import ProcMacroContext
        source = ("#[proc_macro_attribute] fn replace225(a: TokenStream, b: TokenStream)"
                  " -> TokenStream {}\n#[cfg(any())] #[replace225] fn old225() {} fn keep225() {}")
        for replacement in ("", "fn one225() {} fn two225() {}"):
            with self.subTest(replacement=replacement):
                context = ProcMacroContext(ROOT)
                with patch.object(context, "expand_proc", return_value=(tokenize(replacement), [])):
                    out, errors = expand_macros(tokenize(source), iter(range(100)).__next__,
                        proc_context=context, source_path=str(ROOT / "cfg225.wind"))
                self.assertEqual([], errors)
                parsed = parse_with_errors(out)
                self.assertEqual([], [e.message for e in parsed.errors])
                self.assertEqual(["keep225"], [i.name for i in parsed.program.items])
        context = ProcMacroContext(ROOT)
        with patch.object(context, "expand_proc", return_value=(tokenize("fn large225() {}"), [])), \
                patch("cwind_frontend.macros.expansion.MAX_EXPANSION_TOKENS", 2):
            _, errors = expand_macros(tokenize(source), iter(range(100)).__next__,
                proc_context=context, source_path=str(ROOT / "cfg225.wind"))
        self.assertTrue(any("token limit" in e.message for e in errors))

    def test_definition_is_stripped_and_recorded(self):
        source = (
            "#[proc_macro]\n"
            "pub fn make(input: TokenStream) -> TokenStream { return input; }\n"
            "fn main() {}\n"
        )
        stream, defs, errors = _collect(source, "x.wind")
        self.assertEqual([], errors)
        self.assertEqual(1, len(defs))
        definition = defs[0]
        self.assertEqual("make", definition.name)
        self.assertTrue(definition.is_pub)
        # The item is gone from the stream; only main survives.
        self.assertEqual(["fn", "main", "(", ")", "{", "}"], [
            str(tok.value) for tok in stream
        ])
        # The stored tokens carry no attribute.
        kinds = [tok.kind for tok in definition.fn_tokens]
        self.assertNotIn(TokenKind.HASH, kinds)

    def test_private_definition_is_not_pub(self):
        _, defs, _ = _collect(
            "#[proc_macro]\nfn make(input: TokenStream) -> TokenStream {}\n"
        )
        self.assertFalse(defs[0].is_pub)

    def test_non_function_is_rejected(self):
        _, defs, errors = _collect(
            "#[proc_macro]\nstruct S {}\n"
        )
        self.assertEqual([], defs)
        self.assertTrue(any(
            "only be applied to a function" in e.message for e in errors
        ))

    def test_attribute_arguments_rejected(self):
        _, defs, errors = _collect(
            '#[proc_macro(name = "x")]\n'
            "fn make(input: TokenStream) -> TokenStream {}\n"
        )
        self.assertEqual([], defs)
        self.assertTrue(any(
            "does not take arguments" in e.message for e in errors
        ))

    def test_main_name_rejected(self):
        _, _, errors = _collect(
            "#[proc_macro]\nfn main(input: TokenStream) -> TokenStream {}\n"
        )
        self.assertTrue(any(
            "cannot be named 'main'" in e.message for e in errors
        ))

    def test_definition_inside_call_span_is_left_alone(self):
        # A call's argument is opaque: the definition inside expands first.
        source = 'm!(#[proc_macro] fn inner(input: TokenStream) {});\n'
        stream, defs, _ = _collect(source)
        self.assertEqual([], defs)
        self.assertTrue(any(
            tok.kind == TokenKind.HASH for tok in stream
        ))


class ProtocolTests(unittest.TestCase):
    def test_spans_validate_identically_for_tokens_and_diagnostics(self):
        from cwind_frontend.macros.proc.expand import ProcMacroContext

        directory = _local_temp_dir()
        self.addCleanup(shutil.rmtree, directory, True)
        path = directory / "spans.wind"
        path.write_text("call\n中文\n\n" + "x" * 40010, encoding="utf-8")
        anchor = tokenize("call")[0]
        cases = [
            ([2, 1, 2, 2], (2, 1, 2, 2)),
            ([2, 2, 2, 90000], (2, 2, 2, 3)),
            ([2, 90000, 3, 90000], (2, 3, 3, 1)),
            ([4, 40000, 4, 40001], (4, 40000, 4, 40001)),
            ([2, 1, 2, 1], (2, 1, 2, 1)),
        ]
        invalid = [None, [], [1, 2], ["-", "-", "-", "-"],
                   [0, 0, 0, 0], [-1, 1, 1, 1], [True, 1, 1, 1],
                   [1.5, 1, 2, 1], ["2", 1, 2, 2], [3, 1, 2, 1],
                   [2, 3, 2, 1], [5, 1, 5, 2], [1, 1, 2**63, 1]]
        cases.extend((span, token_span(anchor)) for span in invalid)
        context = ProcMacroContext(ROOT)
        for span, expected in cases:
            with self.subTest(span=span):
                rebuilt, errors = pairs_to_tokens(
                    [["ident", "x", span]], anchor=anchor, source=str(path)
                )
                self.assertEqual([], errors)
                self.assertEqual(expected, token_span(rebuilt[0]))
                diagnostics = context._convert_diagnostics(
                    [{"level": "error", "message": "bad", "span": span}],
                    "probe", anchor, str(path),
                )
                diagnostic = diagnostics[0]
                self.assertEqual(expected, (
                    diagnostic.line, diagnostic.column,
                    diagnostic.end_line, diagnostic.end_column,
                ))
                self.assertEqual(token_span(anchor), validated_span(span, anchor))
        self.assertEqual(token_span(anchor), validated_span(
            [2, 1, 2, 2], anchor, str(directory / "missing.wind")
        ))
        rebuilt, errors = pairs_to_tokens([["ident", "x"]], anchor=anchor)
        self.assertEqual([], errors)
        self.assertEqual(token_span(anchor), token_span(rebuilt[0]))

    def test_driver_int64_and_unset_spans(self):
        from cwind_frontend.macros.proc.driver import _span_fields

        maximum = 2**63 - 1
        self.assertEqual(f"40000\t1\t40000\t{maximum}",
                         _span_fields([40000, 1, 40000, maximum]))
        for span in (None, [0] * 4, [1, 1, 1, 2**63], [2, 1, 1, 1]):
            self.assertEqual("-\t-\t-\t-", _span_fields(span))
        tokens, diagnostics, noise = _decode_records(
            "T\tident\t-\t-\t-\t-\tx\n"
            f"D\tnote\t40000\t1\t40000\t{maximum}\t中文\\nmessage\n"
            "T\tident\t1\tbroken\t1\t3\ty\n"
        )
        self.assertEqual([["ident", "x", None], ["ident", "y", None]], tokens)
        self.assertEqual([40000, 1, 40000, maximum], diagnostics[0]["span"])
        self.assertEqual("中文\nmessage", diagnostics[0]["message"])
        self.assertEqual([], noise)

    def test_roundtrip_kinds_and_text(self):
        tokens = tokenize('fn f(x: Int) { "s"; x }')
        pairs = tokens_to_pairs(tokens)
        anchor = tokens[0]
        rebuilt, errors = pairs_to_tokens(pairs, anchor=anchor)
        self.assertEqual([], errors)
        self.assertEqual(
            [tok.kind for tok in tokens], [tok.kind for tok in rebuilt]
        )
        self.assertEqual(
            [tok.raw for tok in tokens], [tok.raw for tok in rebuilt]
        )

    def test_group_delimiters_are_tagged(self):
        tokens = tokenize("(a) [b] {c}")
        kinds = [token_kind_name(tok.kind) for tok in tokens]
        self.assertEqual(
            [
                "group_open", "ident", "group_close",
                "group_open", "ident", "group_close",
                "group_open", "ident", "group_close",
            ],
            kinds,
        )

    def test_invalid_text_is_reported(self):
        tokens = tokenize("f")
        rebuilt, errors = pairs_to_tokens(
            [["punct", "//"]], anchor=tokens[0]
        )
        self.assertEqual([], rebuilt)
        self.assertTrue(errors)

    def test_unknown_kind_is_reported(self):
        tokens = tokenize("f")
        rebuilt, errors = pairs_to_tokens(
            [["nonsense", "x"]], anchor=tokens[0]
        )
        self.assertEqual([], rebuilt)
        self.assertTrue(errors)

    def test_driver_codec_roundtrip(self):
        wire = _encode_tokens(
            [["ident", "foo", [3, 5, 3, 7]], ["punct", "!"]],
            {"line": 3, "column": 5, "end_line": 3, "end_column": 7},
        )
        self.assertEqual(
            "3\t5\t3\t7\n2\nident\t3\t5\t3\t7\nfoo\npunct\t-\t-\t-\t-\n!\n",
            wire,
        )
        tokens, diagnostics, noise = _decode_records(
            "T\tident\t1\t2\t1\t3\tx\n"
            "D\terror\t4\t5\t4\t6\ta\\nb\t\\\\\n"
            "noise\n"
        )
        self.assertEqual([["ident", "x", [1, 2, 1, 3]]], tokens)
        self.assertEqual(
            [{
                "level": "error",
                "span": [4, 5, 4, 6],
                "message": "a\nb\t\\",
            }],
            diagnostics,
        )
        self.assertEqual(["noise"], noise)


class DependencyTests(unittest.TestCase):
    def test_extern_reference_closure_and_bootstrap_copy(self):
        source = (
            'use std::proc_macro::*;\n'
            '#[proc_macro_attribute] fn decorate(a: TokenStream, b: TokenStream)'
            ' -> TokenStream { let n: Int = helper(); return b; }\n'
            'fn helper() -> Int { return foreign(); }\n'
            '#[decorate] #[cfg(all())] #[link(name = "c")] extern "C" {\n'
            ' #[link_name = "abs"] fn foreign(n: Int) -> Int;\n'
            ' static mut STATE: Int; }\n'
            '#[decorate] extern "C" { fn unused_foreign(); }\n'
        )
        definition = self._definition(source)
        original = [t.raw for t in definition.file_tokens]
        program = generate_program(definition)
        self.assertIn('fn foreign (', program)
        self.assertIn('static mut STATE', program)
        self.assertIn('# [ cfg ( all ( ) ) ] # [ link ( name = "c" ) ] extern', program)
        self.assertIn('# [ link_name = "abs" ]', program)
        self.assertNotIn('# [ decorate ]', program)
        self.assertNotIn('unused_foreign', program)
        self.assertEqual(1, program.count('extern "C"'))
        self.assertEqual(original, [t.raw for t in definition.file_tokens])

    def test_extern_static_reference_and_signature_type_closure(self):
        definition = self._definition(
            'typedef ForeignValue = Int;\n'
            '#[cfg(all())] extern "C" { static mut FOREIGN_STATE: ForeignValue; }\n'
            '#[proc_macro] fn read_state(input: TokenStream) -> TokenStream '
            '{ let x: ForeignValue = FOREIGN_STATE; return input; }'
        )
        program = generate_program(definition)
        self.assertIn('static mut FOREIGN_STATE : ForeignValue', program)
        self.assertIn('typedef ForeignValue = Int', program)

    def test_extern_member_attributes_are_not_dependency_names(self):
        for payload in ('#[unrecognized(decorate)]', '#[unrecognized(#[decorate])]'):
            with self.subTest(payload=payload):
                definition = self._definition(
                    '#[proc_macro_attribute] fn decorate(a: TokenStream, b: TokenStream)'
                    ' -> TokenStream { foreign(); return b; }\n'
                    'extern "C" { ' + payload + ' fn foreign(); }'
                )
                program = generate_program(definition)
                self.assertIn('unrecognized', program)
                self.assertEqual(1, program.count('fn __cwpm_decorate'))

    def test_extern_member_bootstrap_cycle_reports_dependency_path(self):
        definition = self._definition(
            '#[proc_macro_attribute] fn decorate(a: TokenStream, b: TokenStream)'
            ' -> TokenStream { helper(); return b; }\n'
            'fn helper() { foreign(); }\n'
            'extern "C" { #[decorate] fn foreign(); }'
        )
        with self.assertRaisesRegex(ValueError, r'circular dependency.*decorate.*helper.*foreign.*decorate'):
            generate_program(definition)

    def test_cwpm_prefix_is_accepted_by_lexer(self):
        # The generated-name prefix is not reserved in user source.
        for name in ("__cwpm_", "__cwpm_make", "__cwpm_make_via_helper"):
            with self.subTest(name=name):
                tokens = tokenize(f"fn {name}() {{}}")
                self.assertEqual(TokenKind.IDENTIFIER, tokens[1].kind)
                self.assertEqual(name, tokens[1].value)
                self.assertEqual(name, tokens[1].raw)

    def test_cwpm_unused_same_named_helper_is_not_a_dependency(self):
        source = (
            "use std::proc_macro::TokenStream;\n"
            "fn __cwpm_make(input: TokenStream) -> TokenStream { return input; }\n"
            "#[proc_macro]\n"
            "fn make(input: TokenStream) -> TokenStream { return input; }\n"
            "fn main() { print(make!(42)); }\n"
        )
        definition = self._definition(source)
        program = generate_program(definition, {definition.name: definition})
        # The unused helper stays out of the closure (DCE), so it occupies
        # nothing: the macro keeps the default internal name, and the
        # single definition below is the renamed macro itself.
        self.assertEqual(1, program.count("fn __cwpm_make ("))
        self.assertEqual(0, program.count("fn __cwpm_make_2 ("))
        self.assertEqual(1, program.count("fn main("))
        self.assertNotIn("print", program)

    def test_static_field_reference_retains_private_std_owner(self):
        from cwind_frontend.sa import run_sa_with_errors
        from cwind_frontend.typed_ast import build_typed_ast

        for body in ("State::value = 42;", "let value: Int64 = State::value;"):
            with self.subTest(body=body):
                parsed = parse_with_errors(tokenize(
                    "struct State { static value: Int64 = 7, }\n"
                    "struct Unused { static value: Int64 = 9, }\n"
                    f"fn main() {{ {body} }}\n"
                ))
                self.assertEqual([], [e.message for e in parsed.errors])
                program = parsed.program
                owners = [item for item in program.items
                          if getattr(item, "name", None) in ("State", "Unused")]
                for owner in owners:
                    setattr(owner, "source_module_path", ["std", "static_test"])
                result = run_sa_with_errors(program)
                self.assertEqual([], [e.message for e in result.errors])
                doc = build_typed_ast(program, result.info)
                declarations = {item.get("name"): item
                                for item in doc["ast"]["items"]}
                self.assertIn("State", declarations)
                self.assertNotIn("Unused", declarations)
                self.assertFalse(declarations["State"]["pub"])
                self.assertTrue(declarations["State"]["fields"][0]["static"])

    def _definition(self, source: str):
        _stream, defs, _errors = collect_proc_macros(
            tokenize(source), "x.wind"
        )
        return defs[0]

    def test_closure_pulls_transitive_helpers(self):
        source = (
            "use std::proc_macro::TokenStream;\n"
            "fn helper() -> Int { return leaf(); }\n"
            "fn leaf() -> Int { return 1; }\n"
            "fn unused() -> Int { return 2; }\n"
            "#[proc_macro]\n"
            "pub fn make(input: TokenStream) -> TokenStream {\n"
            "    let x: Int = helper();\n"
            "    return input;\n"
            "}\n"
        )
        definition = self._definition(source)
        program = generate_program(definition, {definition.name: definition})
        self.assertIn("fn helper", program)
        self.assertIn("fn leaf", program)
        self.assertNotIn("fn unused", program)
        # The macro function itself (renamed to the collision-proof
        # internal name) and the harness are present.
        self.assertIn("fn __cwpm_make", program)
        self.assertIn("stream_from_stdin", program)
        # Self-recursion is not a dependency (DCE): a macro that only
        # calls itself is never pulled into its own dependency set.
        self.assertEqual(1, program.count("fn __cwpm_make"))

    def test_self_reference_does_not_duplicate(self):
        source = (
            "#[proc_macro]\n"
            "pub fn loop_macro(input: TokenStream) -> TokenStream {\n"
            "    return loop_macro(input);\n"
            "}\n"
        )
        definition = self._definition(source)
        program = generate_program(definition, {definition.name: definition})
        self.assertEqual(1, program.count("fn __cwpm_loop_macro"))
        # The body's self-call follows the internal rename.
        self.assertIn("return __cwpm_loop_macro", program)
        self.assertNotIn("return loop_macro", program)

    def test_macro_name_is_collision_proof(self):
        # A macro named like a prelude function must not define that bare
        # name inside its generated program (std bodies resolve bare
        # names against the flat namespace).
        source = (
            "#[proc_macro]\n"
            "pub fn print(input: TokenStream) -> TokenStream {\n"
            "    return input;\n"
            "}\n"
        )
        definition = self._definition(source)
        program = generate_program(definition, {definition.name: definition})
        self.assertIn("fn __cwpm_print", program)
        self.assertNotIn("fn print(", program)


class QuoteBuiltinTests(unittest.TestCase):
    def test_literal_tokens_become_quote_of(self):
        tokens = tokenize("fn foo ( )")
        anchor = tokens[0]
        out, error = expand_builtin("quote", tokens, anchor)
        self.assertIsNone(error)
        text = " ".join(str(tok.value) for tok in out or [])
        self.assertIn("quote_of", text)
        self.assertIn("quote_tok", text)

    def test_interpolation_becomes_quote_concat(self):
        tokens = tokenize("fn #{ value }")
        anchor = tokens[0]
        out, error = expand_builtin("quote", tokens, anchor)
        self.assertIsNone(error)
        text = " ".join(str(tok.value) for tok in out or [])
        self.assertIn("quote_concat", text)
        self.assertIn("value", text)

    def test_unknown_builtin_is_not_claimed(self):
        tokens = tokenize("x")
        out, error = expand_builtin("nope", tokens, tokens[0])
        self.assertIsNone(out)
        self.assertIsNone(error)


class RegistryVisibilityTests(unittest.TestCase):
    def test_pub_macro_is_global(self):
        registry = _registry_for({
            "a.wind": "#[proc_macro]\npub fn m(input: T) -> T {}\n",
        })
        definition, error = registry.lookup("m", str(ROOT / "b.wind"))
        self.assertIsNone(error)
        self.assertIsNotNone(definition)

    def test_private_macro_is_file_local(self):
        registry = _registry_for({
            "a.wind": "#[proc_macro]\nfn m(input: T) -> T {}\n",
        })
        definition, error = registry.lookup("m", str(ROOT / "b.wind"))
        self.assertIsNone(definition)
        self.assertIsNone(error)
        local, error = registry.lookup("m", str(ROOT / "a.wind"))
        self.assertIsNone(error)
        self.assertIsNotNone(local)

    def test_two_pub_macros_are_ambiguous(self):
        registry = _registry_for({
            "a.wind": "#[proc_macro]\npub fn m(input: T) -> T {}\n",
            "b.wind": "#[proc_macro]\npub fn m(input: T) -> T {}\n",
        })
        definition, error = registry.lookup("m", str(ROOT / "c.wind"))
        self.assertIsNone(definition)
        self.assertIn("ambiguous", error or "")


class UnknownMacroCollectionTests(unittest.TestCase):
    def test_unknown_calls_are_recorded(self):
        stream = tokenize("nope!(x);\n")
        records: list[dict] = []
        _out, errors = expand_macros(stream, lambda: 1, records)
        self.assertTrue(any("cannot find macro" in e.message for e in errors))
        self.assertTrue(any(
            record["kind"] == "unknown_macro"
            and record["macro"] == "nope"
            for record in records
        ))


def _local_temp_dir() -> Path:
    """A throwaway directory under the repo.

    Procedure-macro builds compile with the ordinary toolchain (gcc), and
    the OS temp path here carries a non-ASCII short name that MSYS2's ld
    refuses as an output location; keeping test work under the repo also
    anchors std discovery exactly like a real checkout.
    """
    base = ROOT / ".temp" / "proc-macro-tests"
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(dir=str(base)))


class CacheKeyTests(unittest.TestCase):
    """Definition-token hashing: renames/whitespace must not invalidate."""

    def test_expansion_cache_includes_anchor_source_and_input_spans(self):
        from unittest.mock import patch
        from cwind_frontend.macros.proc.build import MacroBuild
        from cwind_frontend.macros.proc.registry import MacroExpansion

        _, definitions, errors = _collect(
            "#[proc_macro] pub fn probe(input: T) -> T { return input; }", "defs.wind"
        )
        self.assertEqual([], errors)
        registry = ProcMacroRegistry(ROOT)
        first, second = tokenize("probe\n probe")
        input_one = tokenize("x")
        input_two = tokenize("\n x")
        calls = [(first, "a.wind", input_one), (second, "a.wind", input_one),
                 (first, "b.wind", input_one), (first, "a.wind", input_two)]
        build = MacroBuild("test", ROOT / "macro.exe", "", ROOT)

        def run(definition, build, pairs, anchor, source_path):
            return MacroExpansion(tokens=[anchor], diagnostics=[{
                "level": "note", "message": source_path,
                "span": list(token_span(anchor)),
            }])

        with patch.object(registry, "build", return_value=build), \
                patch.object(registry, "_run_driver", side_effect=run) as driver:
            for anchor, source_path, inputs in calls:
                result = registry.expand(definitions[0], inputs, anchor=anchor,
                                         source_path=source_path)
                self.assertEqual(token_span(anchor), token_span(result.tokens[0]))
                self.assertEqual(source_path, result.diagnostics[0]["message"])
            self.assertEqual(4, driver.call_count)
            registry.expand(definitions[0], input_one, anchor=first, source_path="a.wind")
            self.assertEqual(4, driver.call_count)

    def _key(self, source: str) -> str:
        _stream, defs, errors = collect_proc_macros(tokenize(source), "x.wind")
        self.assertEqual([], [e.message for e in errors])
        return definition_key(defs[0])

    def test_rename_and_whitespace_reuse(self):
        first = self._key(
            "#[proc_macro]\n"
            "pub fn make(input: TokenStream) -> TokenStream {\n"
            "    let x: Int = 1;\n"
            "    return input;\n"
            "}\n"
        )
        second = self._key(
            "  #[proc_macro]   pub  fn   renamed (  input : TokenStream  )"
            " -> TokenStream { let x : Int = 1 ; return input ; }"
        )
        self.assertEqual(first, second)

    def test_semantic_edits_rebuild(self):
        base = (
            "#[proc_macro]\n"
            "pub fn make(input: TokenStream) -> TokenStream {"
            " let x: Int = 1; return input; }\n"
        )
        literal = base.replace("= 1", "= 2")
        operator = base.replace("return input", "return input")
        self.assertNotEqual(self._key(base), self._key(literal))
        self.assertEqual(self._key(base), self._key(operator))


def _toolchain_available() -> bool:
    from cwind_frontend.macros.proc.build import toolchain_available

    return toolchain_available()


@unittest.skipUnless(
    _toolchain_available(),
    "procedure macros need the CWind backend (cwindc)",
)
class ProcedureMacroEndToEndTests(unittest.TestCase):
    def test_same_file_unreferenced_attributed_extern_macro_runs(self):
        # Fresh body identity prevents the unchanged v8 executable cache
        # from masking a generator regression (dependencies aren't hashed).
        fresh = uuid4().hex
        source = (
            'use std::proc_macro::*;\n'
            '#[proc_macro_attribute]\n'
            'fn extern_unused225(a: TokenStream, b: TokenStream) -> TokenStream {\n'
            f' let fresh: String = "{fresh}"; return b; }}\n'
            '#[extern_unused225] extern "C" { fn unreferenced225(); }\n'
            'extern "C" { #[extern_unused225] fn unreferenced_member225(); }\n'
            '#[extern_unused225] fn value225() -> Int { return 2261; }\n'
            'fn main() { print(value225()); }\n'
        )
        _, defs, errors = _collect(source)
        self.assertEqual([], errors)
        program = generate_program(defs[0])
        self.assertNotIn('extern "C"', program)
        output = self._compile_and_run(source)
        self.assertEqual("2261\n", output.replace("\r\n", "\n"))

    def test_same_file_referenced_attributed_extern_member_callable(self):
        fresh = uuid4().hex
        source = (
            'use std::proc_macro::*;\n'
            '#[proc_macro_attribute]\n'
            'fn extern_used225(a: TokenStream, b: TokenStream) -> TokenStream {\n'
            f' let fresh: String = "{fresh}";\n'
            ' let value: Int32 = abs_helper225();\n'
            ' if value != 2262 { error("foreign call failed"); } return b; }\n'
            'fn abs_helper225() -> Int32 { return foreign_abs225(-2262); }\n'
            '#[extern_used225] #[cfg(all())] extern "C" {\n'
            ' #[link_name = "abs"] fn foreign_abs225(value: Int32) -> Int32; }\n'
            '#[extern_used225] fn value225() -> Int { return 2262; }\n'
            'fn main() { print(value225()); }\n'
        )
        _, defs, errors = _collect(source)
        self.assertEqual([], errors)
        program = generate_program(defs[0])
        self.assertNotIn('# [ extern_used225 ]', program)
        self.assertIn('# [ cfg ( all ( ) ) ] extern "C"', program)
        self.assertIn('# [ link_name = "abs" ] fn foreign_abs225', program)
        output = self._compile_and_run(source)
        self.assertEqual("2262\n", output.replace("\r\n", "\n"))

    def test_same_file_extern_member_circular_dependency_diagnostic(self):
        result = self._parse(
            'use std::proc_macro::*;\n'
            '#[proc_macro_attribute]\n'
            'fn circular225(a: TokenStream, b: TokenStream) -> TokenStream {\n'
            ' cycle_helper225(); return b; }\n'
            'fn cycle_helper225() { cycle_foreign225(); }\n'
            'extern "C" { #[circular225] fn cycle_foreign225(); }\n'
            '#[circular225] fn value225() {}\nfn main() {}\n'
        )
        messages = [e.message for e in result.errors]
        self.assertTrue(any(
            'circular dependency' in message
            and 'circular225 -> cycle_helper225 -> cycle_foreign225 -> #[circular225]' in message
            and 'defined at ' in message
            for message in messages
        ), messages)

    def test_attribute_raw_replacement_delete_and_stacking_runtime(self):
        output = self._compile_and_run(
            "use std::proc_macro::*;\n"
            "#[proc_macro_attribute]\n"
            "fn outer225(attr: TokenStream, item: TokenStream) -> TokenStream {\n"
            '    if attr.length() != 4 { error("args were preexpanded"); }\n'
            '    let first: Token = item.get(0);\n'
            '    if !first.is_text("#") { error("missing inner attribute"); }\n'
            '    return item;\n}\n'
            "#[proc_macro_attribute]\n"
            "fn inner225(attr: TokenStream, item: TokenStream) -> TokenStream {\n"
            '    if attr.length() != 0 { error("expected empty parens"); }\n'
            '    return quote!(fn replaced225() -> Int { return nested225!(); } '
            'fn extra225() -> Int { return 2; });\n}\n'
            "#[proc_macro_attribute]\n"
            "fn delete225(attr: TokenStream, item: TokenStream) -> TokenStream { return stream_new(); }\n"
            "#[proc_macro]\n"
            'fn nested225(input: TokenStream) -> TokenStream { return quote!(223); }\n'
            "#[outer225(not_defined!())] #[inner225()] fn old225() {}\n"
            "#[delete225] fn gone225() { never_defined!(); }\n"
            "fn main() { print(replaced225() + extra225()); }\n"
        )
        self.assertEqual("225\n", output.replace("\r\n", "\n"))

    def test_function_macro_emits_attribute_and_definition(self):
        result = self._parse(
            "use std::proc_macro::*;\n"
            "#[proc_macro]\n"
            'fn emit225(input: TokenStream) -> TokenStream { return quote!('
            '#[proc_macro_attribute] fn generated225(a: TokenStream, b: TokenStream) -> TokenStream '
            '{ return b; } #[generated225] fn generated_item225() {}); }\n'
            "emit225!()\nfn main() {}\n"
        )
        self.assertEqual([], [e.message for e in result.errors])

    def test_attribute_cfg_link_and_members(self):
        # Keep the macro definition independent of its invocation-bearing
        # extern block: the harness always imports same-file extern blocks.
        from cwind_frontend.macros.proc.expand import ProcMacroContext
        directory = _local_temp_dir()
        self.addCleanup(shutil.rmtree, directory, True)
        definition_path = directory / "attributes.wind"
        definition_path.write_text(
            "use std::proc_macro::*;\n"
            "#[proc_macro_attribute]\n"
            "pub fn clean225(a: TokenStream, b: TokenStream) -> TokenStream {\n"
            '    let first: Token = b.get(0);\n'
            '    if first.is_text("#") { error("compiler attrs leaked"); }\n'
            '    return b;\n}\n', encoding="utf-8",
        )
        context = ProcMacroContext(ROOT, scan_dirs=[directory])
        path = directory / "main.wind"
        source = (
            '#[cfg(all())] #[clean225] mod nested225 {\n'
            '    #[clean225] fn inside225() {}\n}\n'
            '#[clean225] #[link(name = "c")] extern "C" {\n'
            '    #[clean225] #[link_name = "puts"] fn puts225(s: *const u8) -> Int;\n}\n'
            'struct Owner225 {}\n'
            'extra Owner225 { #[clean225] fn method225(&self) {} }\n'
            'trait Trait225 { #[clean225] fn method225(&self); }\n'
            '#[cfg(any())] #[clean225] fn removed225() {}\n'
            'fn main() {}\n'
        )
        path.write_text(source, encoding="utf-8")
        expanded, errors = expand_macros(tokenize(source), iter(range(10000)).__next__,
                                         proc_context=context, source_path=str(path))
        self.assertEqual([], [e.message for e in errors])
        result = parse_with_errors(expanded, source_path=str(path))
        self.assertEqual([], [e.message for e in result.errors])
        names = [getattr(item, "name", None) for item in result.program.items]
        self.assertNotIn("removed225", names)

    def test_attribute_output_errors_and_recursion(self):
        from unittest.mock import patch
        cases = [
            ('return stream_of_token(token_punct("//"));', "invalid token text"),
            ('error_at("attr failure225", a.span()); return b;', "attr failure225"),
            ('return quote!(#[loop225] fn repeated225() {});', "recursion depth limit"),
        ]
        for body, expected in cases:
            with self.subTest(expected=expected), patch.dict(os.environ, {"CWIND_RECURSION_LIMIT": "4"}):
                result = self._parse(
                    "use std::proc_macro::*;\n#[proc_macro_attribute]\n"
                    "fn loop225(a: TokenStream, b: TokenStream) -> TokenStream { "
                    + body + " }\n#[loop225(arg225)] fn original225() {}\nfn main() {}\n"
                )
                self.assertTrue(any(expected in e.message for e in result.errors),
                                [e.message for e in result.errors])

    def test_attribute_stream_spans_and_cache(self):
        from cwind_frontend.macros.proc.expand import ProcMacroContext
        directory = _local_temp_dir()
        self.addCleanup(shutil.rmtree, directory, True)
        path = directory / "spans.wind"
        source = (
            "use std::proc_macro::*;\n#[proc_macro_attribute]\n"
            "fn spans225(a: TokenStream, b: TokenStream) -> TokenStream {\n"
            'note_at("args225", a.span()); note_at("item225", b.span());\n'
            'note_at("call225", Span::call_site()); return stream_concat([a, b]); }\n'
            "#[spans225(alpha beta)]\nfn item225() {}\n"
        )
        path.write_text(source, encoding="utf-8")
        stream, defs, errors = _collect(source, str(path))
        self.assertEqual([], errors)
        start = next(i for i, t in enumerate(stream) if t.raw == "spans225")
        anchor = stream[start - 2]
        args = stream[start + 2:start + 4]
        item = stream[start + 6:]
        context = ProcMacroContext(ROOT, cache_dir=directory / "cache")
        output, errors = context.expand_proc(defs[0], args, anchor, str(path), item_tokens=item)
        self.assertEqual([], [e.message for e in errors])
        self.assertEqual([token_span(t) for t in args + item], [token_span(t) for t in output])
        self.assertEqual([t.raw for t in args + item], [t.raw for t in output])
        self.assertEqual((args[0].line, args[0].column),
                         (context.warnings[0].line, context.warnings[0].column))
        self.assertEqual((item[0].line, item[0].column),
                         (context.warnings[1].line, context.warnings[1].column))
        self.assertEqual(token_span(anchor), (context.warnings[2].line,
                         context.warnings[2].column, context.warnings[2].end_line,
                         context.warnings[2].end_column))
        for a, b in ((args, item[1:]), (args[1:], item), (args, item)):
            output, errors = context.expand_proc(defs[0], a, anchor, str(path), item_tokens=b)
            self.assertEqual([], errors)
            self.assertEqual([t.raw for t in a + b], [t.raw for t in output])
        self.assertEqual(3, len(context.registry._results))

    def test_attribute_identity_runtime(self):
        output = self._compile_and_run(
            "use std::proc_macro::*;\n"
            "#[proc_macro_attribute]\n"
            "fn attr_identity225(attr: TokenStream, item: TokenStream) -> TokenStream {\n"
            '    if attr.length() != 0 { error("expected empty args"); }\n'
            "    return item;\n}\n"
            '#[attr_identity225] fn value225() -> Int { return 225; }\n'
            'fn main() { print(value225()); }\n'
        )
        self.assertEqual("225\n", output.replace("\r\n", "\n"))

    def _span_expansion(self, body: str, invocation: str = "probe!(alpha beta)"):
        from cwind_frontend.macros.proc.expand import ProcMacroContext

        directory = _local_temp_dir()
        self.addCleanup(shutil.rmtree, directory, True)
        path = directory / "span.wind"
        text = (
            "use std::proc_macro::*;\n"
            "use std::panic::panic;\n"
            "#[proc_macro]\n"
            "pub fn probe(input: TokenStream) -> TokenStream {\n"
            + body + "\n}\n" + invocation + "\n"
        )
        path.write_text(text, encoding="utf-8", newline="\n")
        stream, definitions, errors = _collect(text, str(path))
        self.assertEqual([], errors)
        anchor = next(tok for tok in reversed(stream) if tok.raw == "probe")
        start = stream.index(anchor)
        args = stream[start + 3:-1]
        context = ProcMacroContext(ROOT, cache_dir=directory / "cache")
        output, errors = context.expand_proc(definitions[0], args, anchor, str(path))
        return output, errors, context, anchor, args, definitions[0]

    def test_span_call_site_input_stream_and_empty(self):
        output, errors, context, anchor, args, _ = self._span_expansion(
            'let call: Span = Span::call_site();\n'
            'let first: Token = input.get(0);\n'
            'let position: Span = first.span();\n'
            'let empty: TokenStream = stream_new();\n'
            'report_at("note", call.line.to_string(), call);\n'
            'note_at(position.column.to_string(), position);\n'
            'warning_at("stream", input.span());\n'
            'let diag: Diagnostic = Diagnostic::note_at("empty", empty.span());\n'
            'diag.emit();\n'
            'return input;',
            "\n" * 40000 + "probe!(alpha\n beta)",
        )
        self.assertEqual([], [e.message for e in errors])
        self.assertEqual([token_span(t) for t in args], [token_span(t) for t in output])
        warnings = context.warnings
        self.assertEqual(4, len(warnings))
        expected = [token_span(anchor), token_span(args[0]),
                    (args[0].line, args[0].column, args[-1].end_line, args[-1].end_column),
                    token_span(anchor)]
        for warning, span in zip(warnings, expected):
            self.assertEqual(span, (warning.line, warning.column,
                                    warning.end_line, warning.end_column))
        self.assertIn(str(anchor.line), warnings[0].message)
        self.assertIn(str(args[0].column), warnings[1].message)
        self.assertGreater(anchor.line, 32767)

    def test_span_set_span_quote_and_constructors(self):
        output, errors, _, anchor, args, _ = self._span_expansion(
            'let first: Token = input.get(0);\n'
            'let at: Span = first.span();\n'
            'let mut changed: Token = token_ident("changed");\n'
            'changed.set_span(at);\n'
            'let keep: TokenStream = stream_of_token(changed);\n'
            'let quoted: TokenStream = quote!(template #{ keep });\n'
            'return stream_concat([quoted, stream_of([\n'
            'token_ident_at(first.span(), "x"), token_literal_at(first.span(), "42"),\n'
            'token_punct_at(first.span(), "+"), token_group_open_at(first.span(), "("),\n'
            'token_group_close_at(first.span(), ")"), token_literal("7"),\n'
            'token_punct("-"), token_group_open("["), token_group_close("]")])]);'
        )
        self.assertEqual([], [e.message for e in errors])
        self.assertEqual(["template", "changed", "x", "42", "+", "(", ")", "7", "-", "[", "]"],
                         [t.raw for t in output])
        self.assertEqual([token_span(anchor)] + [token_span(args[0])] * 6
                         + [token_span(anchor)] * 4,
                         [token_span(t) for t in output])

    def test_span_diagnostics_escape_clamp_and_fallback(self):
        output, errors, context, anchor, args, _ = self._span_expansion(
            'let first: Token = input.get(0);\n'
            'let at: Span = first.span();\n'
            'error_at("first\\nsecond\\t中文\\r\\\\", at);\n'
            'let invalid: Span = Span { 0, 0, 0, 0 };\n'
            'let bad: Diagnostic = Diagnostic::error_at("fallback", invalid);\n'
            'bad.emit();\n'
            'let at2: Span = first.span();\n'
            'let wide: Span = Span { at2.line, at2.column, at2.line, 90000 };\n'
            'warning_at("clamped", wide);\n'
            'error("default"); warning("warning"); note("note");\n'
            'let d: Diagnostic = Diagnostic::note("default note"); d.emit();\n'
            'return input;'
        )
        self.assertEqual([], output)
        self.assertEqual(3, len(errors))
        self.assertIn("first\nsecond\t中文\r\\", errors[0].message)
        self.assertEqual((args[0].line, args[0].column), (errors[0].line, errors[0].column))
        for error in errors[1:]:
            self.assertEqual((anchor.line, anchor.column), (error.line, error.column))
        self.assertEqual(args[-1].end_column + 1, context.warnings[0].end_column)
        self.assertEqual(4, len(context.warnings))

    def test_span_output_clamp_fallback_and_cached_positions(self):
        output, errors, context, anchor, args, definition = self._span_expansion(
            'let first: Token = input.get(0);\n'
            'let at: Span = first.span();\n'
            'let wide: Span = Span { at.line, at.column, at.line, 90000 };\n'
            'let invalid: Span = Span { 90000, 1, 90000, 2 };\n'
            'return stream_of([token_ident_at(wide, "wide"),\n'
            'token_ident_at(invalid, "invalid"), token_ident("default")]);'
        )
        self.assertEqual([], [e.message for e in errors])
        self.assertEqual((args[0].line, args[0].column, args[0].line,
                          args[-1].end_column + 1), token_span(output[0]))
        self.assertEqual(token_span(anchor), token_span(output[1]))
        self.assertEqual(token_span(anchor), token_span(output[2]))
        moved_anchor = args[-1]
        moved, errors = context.expand_proc(
            definition, args, moved_anchor, definition.source_path
        )
        self.assertEqual([], [e.message for e in errors])
        self.assertEqual(token_span(moved_anchor), token_span(moved[1]))
        self.assertEqual(token_span(moved_anchor), token_span(moved[2]))
        self.assertEqual(token_span(anchor), token_span(output[2]))

    def test_span_wire_int64_limits_and_unset_input(self):
        from cwind_frontend.macros.proc.driver import run

        _, errors, context, _, _, definition = self._span_expansion(
            'let first: Token = input.get(0);\n'
            'note_at("wire", first.span());\n'
            'return stream_of([first, token_ident("default")]);'
        )
        self.assertEqual([], [e.message for e in errors])
        build = context.registry.build(definition)
        maximum = 2**63 - 1
        call = {"line": maximum, "column": 1, "end_line": maximum, "end_column": 2}
        for span in (None, [1, 1, 1, 2**63], [maximum, 1, maximum, 2]):
            response = run(str(build.exe), {
                "call": call, "tokens": [["ident", "中文", span]],
            }, 30)
            self.assertTrue(response["ok"], response)
            self.assertEqual([maximum, 1, maximum, 2], response["tokens"][0][2])
            self.assertEqual("中文", response["tokens"][0][1])
            self.assertEqual(response["tokens"][0][2], response["tokens"][1][2])
            self.assertEqual(response["tokens"][0][2], response["diagnostics"][0]["span"])

    def test_span_empty_invocation(self):
        output, errors, _, anchor, _, _ = self._span_expansion(
            'return stream_of_token(token_ident_at(input.span(), "empty"));',
            "probe!()",
        )
        self.assertEqual([], [e.message for e in errors])
        self.assertEqual(token_span(anchor), token_span(output[0]))

    def test_proc_failure_includes_definition_location(self):
        for body, expected in (
            ('panic(&"span panic");', "span panic"),
            ('missing_span_helper(); return input;', "failed to compile"),
        ):
            with self.subTest(body=body):
                output, errors, _, _, _, definition = self._span_expansion(body)
                self.assertEqual([], output)
                self.assertTrue(errors)
                self.assertIn(expected, errors[0].message)
                self.assertIn(
                    f"defined at {definition.source_path}:"
                    f"{definition.name_token.line}:{definition.name_token.column}",
                    errors[0].message,
                )

    def _parse(self, text: str, name: str = "main.wind", jobs: int = 1):
        directory = _local_temp_dir()
        self.addCleanup(shutil.rmtree, directory, True)
        path = directory / name
        path.write_text(text, encoding="utf-8", newline="\n")
        return parse_with_errors(
            tokenize_file(path), source_path=str(path.resolve()), jobs=jobs
        )

    def _compile_and_run(self, text: str) -> str:
        from cwind_frontend.macros.proc.build import (
            _child_env,
            _frontend_command,
            find_cwindc,
        )

        work = _local_temp_dir()
        self.addCleanup(shutil.rmtree, work, True)
        source = work / "prog.wind"
        source.write_text(text, encoding="utf-8", newline="\n")
        env = _child_env()
        typed = subprocess.run(
            [*_frontend_command(), "--typed-ast", str(source)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=str(work),
        )
        self.assertEqual(
            0, typed.returncode, typed.stderr.decode("utf-8", "replace")
        )
        json_path = work / "prog.typed.json"
        json_path.write_bytes(typed.stdout)
        cwindc = find_cwindc()
        assert cwindc is not None
        exe = work / "prog.exe"
        built = subprocess.run(
            [str(cwindc), str(json_path), "-o", str(exe)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=str(work),
        )
        self.assertEqual(
            0, built.returncode, built.stdout.decode("utf-8", "replace")
        )
        run = subprocess.run(
            [str(exe)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(work),
        )
        return run.stdout.decode("utf-8", "replace")

    def test_cwpm_referenced_helper_collision_is_avoided(self):
        # Generator avoidance: the internal name is picked against every
        # identifier the generated program carries, so a user helper
        # spelled exactly like the default choice cannot duplicate the
        # renamed macro (direct call or through a transitive helper).
        for indirect in (False, True):
            with self.subTest(indirect=indirect):
                unique = uuid4().hex
                name = "make" if not indirect else f"make_via_helper_{unique}"
                internal = f"__cwpm_{name}"
                bridge = f"bridge_{unique}"
                source = (
                    "use std::proc_macro::TokenStream;\n"
                    f"fn {internal}(input: TokenStream) -> TokenStream {{\n"
                    "    return input;\n"
                    "}\n"
                )
                if indirect:
                    source += (
                        f"fn {bridge}(input: TokenStream) -> TokenStream {{\n"
                        f"    return {internal}(input);\n"
                        "}\n"
                    )
                source += (
                    "#[proc_macro]\n"
                    f"fn {name}(input: TokenStream) -> TokenStream {{\n"
                    f"    let cache_isolation_{unique}: Int = 0;\n"
                    f"    return {bridge if indirect else internal}(input);\n"
                    "}\n"
                    f"fn main() {{ print({name}!(42)); }}\n"
                )
                _, definitions, errors = _collect(source)
                self.assertEqual([], errors)
                definition = definitions[0]
                program = generate_program(definition, {name: definition})
                self.assertEqual(1, program.count(f"fn {internal} ("))
                chosen = f"__cwpm_{name}_2"
                self.assertEqual(1, program.count(f"fn {chosen} ("))
                if indirect:
                    self.assertIn(f"fn {bridge} (", program)
                result = self._parse(source)
                self.assertEqual([], [e.message for e in result.errors])

    def test_cwpm_consecutive_suffixed_helpers_are_all_avoided(self):
        # The avoidance loop keeps walking until it leaves the occupied
        # set: helpers named after every default and suffixed candidate
        # are all pulled (each one calls the next), so the macro lands
        # on the first free suffix.
        unique = uuid4().hex
        name = f"make_{unique}"
        taken = [f"__cwpm_{name}"]
        taken.extend(f"__cwpm_{name}_{n}" for n in range(2, 5))
        source = "use std::proc_macro::TokenStream;\n"
        source += "\n".join(
            f"fn {tok}(input: TokenStream) -> TokenStream {{\n"
            f"    return {taken[i + 1]}(input);\n"
            "}\n"
            for i, tok in enumerate(taken[:3])
        )
        source += (
            f"fn {taken[3]}(input: TokenStream) -> TokenStream "
            "{ return input; }\n"
            "#[proc_macro]\n"
            f"fn {name}(input: TokenStream) -> TokenStream {{\n"
            f"    let cache_isolation_{unique}: Int = 0;\n"
            f"    return {taken[0]}(input);\n"
            "}\n"
            f"fn main() {{ print({name}!(42)); }}\n"
        )
        _, definitions, errors = _collect(source)
        self.assertEqual([], errors)
        program = generate_program(definitions[0], {name: definitions[0]})
        self.assertEqual(1, program.count(f"fn __cwpm_{name}_5 ("))

    def test_cwpm_final_program_function_is_separate_from_macro_exe(self):
        unique = uuid4().hex
        name = f"make_runtime_{unique}"
        internal = f"__cwpm_{name}"
        source = (
            "use std::proc_macro::TokenStream;\n"
            f"fn {internal}() -> Int {{ return 7; }}\n"
            "#[proc_macro]\n"
            f"fn {name}(input: TokenStream) -> TokenStream {{\n"
            f"    let cache_isolation_{unique}: Int = 0;\n"
            "    return input;\n"
            "}\n"
            f"fn main() {{ print({name}!(42)); print({internal}()); }}\n"
        )
        _, definitions, errors = _collect(source)
        self.assertEqual([], errors)
        program = generate_program(definitions[0], {name: definitions[0]})
        # The final-program fn is not pulled into the macro program, so it
        # occupies nothing and the macro keeps the default internal name.
        self.assertEqual(1, program.count(f"fn {internal} ("))
        self.assertNotIn("return 7", program)
        output = self._compile_and_run(source)
        self.assertEqual("42\n7\n", output.replace("\r\n", "\n"))

    def test_format_macro_expands(self):
        result = self._parse(
            'fn main() { let s: String = format!("x={} y={}", 1, 2); '
            "print(s); }\n"
        )
        self.assertEqual([], [e.message for e in result.errors])

    def test_format_macro_runtime_output(self):
        output = self._compile_and_run(
            'fn main() { let s: String = format!("{} and {}", 41 + 1, "b"); '
            "print(s); }\n"
        )
        self.assertEqual("42 and b\n", output.replace("\r\n", "\n"))

    def test_format_escapes_and_encoding(self):
        output = self._compile_and_run(
            'fn main() { print(format!("{{literal}} {}<{}>", 1, 2)); }\n'
        )
        self.assertEqual(
            "{literal} 1<2>\n", output.replace("\r\n", "\n")
        )

    def test_format_placeholder_shape_is_rejected(self):
        result = self._parse(
            'fn main() { print(format!("{name}")); }\n'
        )
        self.assertTrue(any(
            "only '{}' placeholders" in e.message
            for e in result.errors
        ))

    def test_format_argument_count_is_checked(self):
        missing = self._parse(
            'fn main() { print(format!("{}")); }\n'
        )
        self.assertTrue(any(
            "not enough arguments" in e.message for e in missing.errors
        ))
        extra = self._parse(
            'fn main() { print(format!("{}", 1, 2)); }\n'
        )
        self.assertTrue(any(
            "too many arguments" in e.message for e in extra.errors
        ))

    def test_string_byte_iteration_runtime(self):
        output = self._compile_and_run(
            'fn main() { let s: String = "AB"; '
            "for b in s { print(b); } }\n"
        )
        self.assertEqual("65\n66\n", output.replace("\r\n", "\n"))

    def test_stringify_macro_runtime_output(self):
        output = self._compile_and_run(
            "fn main() { print(stringify!(a + b)); }\n"
        )
        self.assertEqual("a + b\n", output.replace("\r\n", "\n"))

    def test_stringify_macro_escapes_token_text(self):
        cases = [
            '',
            r'"hello"',
            r'"a\nb\tc\r\\\""',
            r'"中文" + (value)',
            r'stringify!("nested")',
        ]
        source = "fn main() {\n" + "\n".join(
            f"print(stringify!({text}));" for text in cases
        ) + "\n}\n"
        expected = "".join(
            " ".join(tok.raw for tok in tokenize(text)) + "\n"
            for text in cases
        )
        output = self._compile_and_run(source)
        self.assertEqual(expected, output.replace("\r\n", "\n"))

    def test_print_macros_runtime_output(self):
        # todo-176 stand-in: print!/println! are proc macros wrapping
        # _write/print around format!; a macro named like a prelude
        # function must not hijack std bodies in its generated program.
        output = self._compile_and_run(
            "fn main() {\n"
            '    print!("a");\n'
            '    print!("{}", 1);\n'
            '    println!("b");\n'
            '    println!("{} + {} = {}", 1, 2, 3);\n'
            "    print!();\n"
            "    println!();\n"
            "}\n"
        )
        self.assertEqual(
            "a1b\n1 + 2 = 3\n\n", output.replace("\r\n", "\n")
        )

    def test_print_macros_parse_clean(self):
        result = self._parse(
            "fn main() {\n"
            '    print!("no {} here", "args");\n'
            "    println!();\n"
            "}\n"
        )
        self.assertEqual([], [e.message for e in result.errors])

    def test_parallel_jobs_expand_multiple_macros(self):
        result = self._parse(
            "fn main() {\n"
            '    print(format!("{} + {}", 1, 2));\n'
            "    print(stringify!(a * b));\n"
            "}\n",
            jobs=4,
        )
        self.assertEqual([], [e.message for e in result.errors])

    def test_macro_error_diagnostic_aborts(self):
        result = self._parse(
            "fn main() { let s: String = format!(); print(s); }\n"
        )
        self.assertTrue(any(
            "expected a format string literal" in e.message
            for e in result.errors
        ))

    def test_local_macro_definition(self):
        result = self._parse(
            "use std::proc_macro::TokenStream;\n"
            "use std::proc_macro::stream_of_token;\n"
            "use std::proc_macro::token_literal;\n"
            "\n"
            "#[proc_macro]\n"
            "pub fn shout(input: TokenStream) -> TokenStream {\n"
            '    return stream_of_token('
            'token_literal("\\"" + input.source() + "!\\""));\n'
            "}\n"
            "\n"
            "fn main() { print(shout!(hello world)); }\n"
        )
        self.assertEqual([], [e.message for e in result.errors])

    def test_unused_invalid_macro_is_not_compiled(self):
        # DCE: a definition that is never called is stripped but never
        # built, so even a nonsense body stays harmless.
        result = self._parse(
            "#[proc_macro]\n"
            "pub fn never_used(input: TotallyUnknown) -> Nope {\n"
            "    this is not even close to CWind !!!\n"
            "}\n"
            "fn main() { print(1); }\n"
        )
        self.assertEqual([], [e.message for e in result.errors])


class CrossModuleMacroTests(unittest.TestCase):
    """Global-unique-name visibility (todo-176) without the toolchain."""

    def _project(self, files: dict[str, str]) -> Path:
        root = _local_temp_dir()
        self.addCleanup(shutil.rmtree, root, True)
        (root / "Breeze.toml").write_text(
            "[package]\n"
            'name = "pmtest"\n'
            'version = "0.0.1"\n'
            'identifier = "Dev"\n'
            'id_version = "0.0.1"\n'
            "\n"
            "[entry]\n"
            'source = "./src"\n'
            "is_lib = false\n"
            'module = "lib.wd"\n',
            encoding="utf-8",
            newline="\n",
        )
        for rel, text in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
        return root

    def test_pub_macro_defined_in_other_file_is_registered_without_use(self):
        from cwind_frontend.macros.proc import ProcMacroContext
        from cwind_frontend.parser.defs import _module_roots

        root = self._project({
            "src/util.wind": (
                "use std::proc_macro::TokenStream;\n"
                "[proc_macro]\n"
                "pub fn tag(input: TokenStream) -> TokenStream { "
                "return input; }\n"
            ).replace("[proc_macro]", "#[proc_macro]"),
        })
        context = ProcMacroContext(
            root, scan_dirs=[
                r.directory for r in _module_roots(root)
            ],
        )
        definition, error = context.lookup(
            "tag", str(root / "src" / "main.wind")
        )
        self.assertIsNone(error)
        self.assertIsNotNone(definition)
        assert definition is not None
        self.assertEqual(
            str((root / "src" / "util.wind").resolve()),
            definition.source_path,
        )


if __name__ == "__main__":
    unittest.main()
