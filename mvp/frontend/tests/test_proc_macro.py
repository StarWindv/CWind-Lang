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

import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
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
    """A registry whose definitions come from in-memory files.

    Module-addressed visibility (todo-158-style): each source registers as
    the module named by its file stem under a synthetic crate root, so
    ``use crate::a::m;`` / qualified paths resolve like a real declared
    tree.  Callers bind names into a consumer file with
    ``registry.prepare_file(tokenize("use ...;"), path)``.
    """
    from cwind_frontend.macros.proc.registry import _file_key

    registry = ProcMacroRegistry(ROOT)
    registry.modules[("crate",)] = type("N", (), {"entry": None, "pub": True})()
    for rel, text in sources.items():
        path = ROOT / rel
        tokens = tokenize(text)
        parts = ("crate", Path(rel).stem)
        registry.modules[parts] = type("N", (), {"entry": path, "pub": True})()
        registry.file_modules[_file_key(str(path))] = parts
        registry.prepare_file(tokens, str(path))
        stream, defs, _errors = collect_proc_macros(tokens, str(path))
        for definition in defs:
            registry.register(definition)
    return registry


class MacroExportTests(unittest.TestCase):
    def test_macro_export_proc_macros_rejected(self):
        signatures = (
            ("proc_macro", "function", "fn sample(x: TokenStream) -> TokenStream { return x; }"),
            ("proc_macro_attribute", "attribute", "fn sample(a: TokenStream, x: TokenStream) -> TokenStream { return x; }"),
            ("proc_macro_derive(Sample)", "derive", "fn sample(x: TokenStream) -> TokenStream { return x; }"),
        )
        for attr, kind, signature in signatures:
            for attributes in (f"#[macro_export] #[{attr}]", f"#[{attr}] #[macro_export]"):
                for visibility in ("", "pub "):
                    with self.subTest(attributes=attributes, visibility=visibility):
                        text = attributes + visibility + signature
                        stream, defs, errors = _collect(text, "defs.wind")
                        self.assertEqual([], defs)
                        self.assertEqual([], stream)
                        self.assertTrue(any(
                            "#[macro_export] is not supported on procedure macros; visibility follows pub"
                            in e.message for e in errors
                        ), [e.message for e in errors])
                        _, errors = expand_macros(tokenize(text), iter(range(100)).__next__)
                        self.assertTrue(any("visibility follows pub" in e.message for e in errors))
            for visibility in ("", "pub "):
                with self.subTest(attr=attr, visibility=visibility):
                    _, defs, errors = _collect(f"#[{attr}] " + visibility + signature, "defs.wind")
                    self.assertEqual([], errors)
                    definition = defs[0]
                    registry = _registry_for({"defs.wind": f"#[{attr}] " + visibility + signature})
                    # The helper re-collects from the same text, so the
                    # definition it registered equals the collected one.
                    # 文件键按 abspath 归一 —— 注册面用的是 ROOT 下的绝对
                    # 路径, 查询也必须传绝对路径 (相对路径会按 CWD 解析).
                    found, _ = registry.lookup(
                        definition.name, str(ROOT / "defs.wind"), kind
                    )
                    self.assertEqual([], definition.issues)
                    self.assertIsNotNone(found)
                    assert found is not None
                    self.assertEqual(kind, found.kind)
                    # Module-addressed visibility: a foreign file sees the
                    # macro only through an explicit binding (use), never
                    # by bare name alone.
                    self.assertEqual((None, None),
                                     registry.lookup(definition.name,
                                                     str(ROOT / "other.wind"), kind))
                    registry.prepare_file(
                        tokenize(f"use crate::defs::{definition.name};"),
                        str(ROOT / "other.wind"),
                    )
                    found, _ = registry.lookup(
                        definition.name, str(ROOT / "other.wind"), kind
                    )
                    if visibility:
                        self.assertIsNotNone(found)
                        assert found is not None
                        self.assertEqual(kind, found.kind)
                    else:
                        self.assertIsNone(found)

    def test_macro_export_non_macro_errors(self):
        for item in ("fn f() {}", "pub struct S {}", "const X: Int = 1;", "", "#[other] fn f() {}"):
            with self.subTest(item=item):
                _, errors = expand_macros(tokenize("#[macro_export] " + item), iter(range(100)).__next__)
                self.assertTrue(any("only be applied to macro_rules!" in e.message for e in errors),
                                [e.message for e in errors])
        _, errors = expand_macros(tokenize("#[macro_export(x)] macro_rules! m { () => { 1 } }"),
                                  iter(range(100)).__next__)
        self.assertTrue(any("does not take arguments" in e.message for e in errors))

    def test_std_print_rules_registration_without_proc_build(self):
        from unittest.mock import patch
        from cwind_frontend.macros.proc import ProcMacroContext
        from cwind_frontend.parser.defs import _module_roots

        roots = [root.directory for root in _module_roots(ROOT)]
        self.assertIn((ROOT / "libs").resolve(), roots)
        context = ProcMacroContext(ROOT, scan_dirs=roots)
        main = str(ROOT / "main.wind")
        bystander = str(ROOT / "libs/ext/other.wind")
        # No bindings anywhere yet: no global names, no prelude for either.
        for name in ("print", "println"):
            self.assertIsNone(context.registry.lookup_rules(name, bystander)[0])
            self.assertIsNone(context.registry.lookup_rules(name, main)[0])
            self.assertEqual((None, None), context.registry.lookup(name))
        context.registry.prepare_file(tokenize("use std::*;"), main, prelude=True)
        for name in ("print", "println"):
            rule, error = context.registry.lookup_rules(name, main)
            self.assertIsNone(error)
            self.assertIsNotNone(rule)
            assert rule is not None and rule.source_path is not None
            self.assertTrue(rule.exported)
            self.assertEqual((ROOT / "libs/ext/print.wind").resolve(),
                             Path(rule.source_path))
            # A non-prelude file without use bindings resolves nothing.
            self.assertEqual(
                (None, None), context.registry.lookup_rules(name, bystander)[0:2],
            )
            self.assertEqual((None, None), context.registry.lookup(name))
        with patch("cwind_frontend.macros.proc.registry.build_macro",
                   side_effect=AssertionError("print wrappers must not build a proc")):
            out, errors = expand_macros(
                tokenize("print!(); println!();"), iter(range(100)).__next__,
                proc_context=context, source_path=main,
            )
        self.assertEqual([], [e.message for e in errors])
        self.assertIn("_write", [t.raw for t in out])
        self.assertEqual({}, context.registry._builds)

    def test_macro_export_rules_cache_and_opaque_templates(self):
        from dataclasses import replace
        from cwind_frontend.macros.expansion import _collect_definitions
        text = "#[macro_export] macro_rules! outer { () => { #[macro_export] macro_rules! inner { () => { 42 } } } }"
        stream, proc_defs, errors = _collect(text, "rules.wind")
        self.assertEqual([], errors)
        self.assertEqual([], proc_defs)
        self.assertEqual(tokenize(text), stream)
        rules = {}
        rule_errors = []
        _collect_definitions(stream, rules, None, rule_errors, "rules.wind")
        self.assertEqual([], rule_errors)
        rule = rules["outer"]
        self.assertTrue(rule.exported)
        self.assertNotEqual(rule.definition_key(), replace(rule, exported=False).definition_key())
        self.assertNotEqual(rule.definition_key(), replace(rule, definition_tokens=[]).definition_key())
        out, errors = expand_macros(tokenize(text + " outer!(); inner!()"), iter(range(100)).__next__)
        self.assertEqual([], [e.message for e in errors])
        self.assertIn("42", [t.raw for t in out])


class CollectionTests(unittest.TestCase):
    def test_derive_definition_name_kind_and_cache(self):
        from dataclasses import replace
        stream, defs, errors = _collect(
            "#[proc_macro_derive(Value)] pub fn derive_value(input: TokenStream)"
            " -> TokenStream { return input; } struct T {}", "derive.wind"
        )
        self.assertEqual([], [e.message for e in errors])
        self.assertEqual(1, len(defs))
        definition = defs[0]
        self.assertEqual("Value", definition.name)
        self.assertEqual("derive", definition.kind)
        self.assertTrue(definition.is_pub)
        self.assertEqual("struct T { }", " ".join(t.raw for t in stream))
        self.assertNotEqual(definition_key(definition),
                            definition_key(replace(definition, kind="function")))
        program = generate_program(definition)
        self.assertIn("fn __cwpm_Value (", program)
        self.assertNotIn("fn derive_value (", program)
        self.assertEqual(1, program.count("stream_from_stdin();"))

    def test_derive_invalid_definitions(self):
        for attr in ("#[proc_macro_derive]", "#[proc_macro_derive()]",
                     "#[proc_macro_derive(A, attributes(helper))]",
                     "#[proc_macro_derive(A, B)]", '#[proc_macro_derive("A")]'):
            with self.subTest(attr=attr):
                _, defs, errors = _collect(attr + " fn d(x: TokenStream) -> TokenStream {}")
                self.assertEqual([], defs)
                self.assertTrue(any("not supported" in e.message for e in errors))
        for signature in ("fn d() -> TokenStream", "fn d(x: Int) -> TokenStream",
                          "fn d(x: TokenStream, y: TokenStream) -> TokenStream",
                          "fn d(mut x: TokenStream) -> TokenStream",
                          "fn d(x: TokenStream)", "fn d(x: TokenStream) -> Int"):
            with self.subTest(signature=signature):
                _, defs, errors = _collect("#[proc_macro_derive(A)] " + signature + " {}")
                self.assertTrue(any("proc_macro_derive" in e.message for e in errors))
                self.assertTrue(defs[0].issues)
        for item in ("struct T {}", "pub(crate) fn d(x: TokenStream) -> TokenStream {}"):
            self.assertTrue(_collect("#[proc_macro_derive(A)] " + item)[2])
        self.assertEqual([], _collect(
            "#[proc_macro_derive(A)] fn d(x: std::proc_macro::TokenStream,)"
            " -> std::proc_macro::TokenStream {}"
        )[2])

    def test_derive_visibility_and_distinct_namespace(self):
        signature = "fn d(x: TokenStream) -> TokenStream {}"
        registry = _registry_for({
            "a.wind": "#[proc_macro_derive(A)] pub " + signature,
            "b.wind": "#[proc_macro_derive(A)] " + signature,
            "c.wind": "#[proc_macro] pub fn A(x: TokenStream) -> TokenStream {}",
        })
        # Same file: the private copy is visible bare; distinct namespace:
        # c.wind's function-kind macro named A is callable A!() on its own.
        definition, error = registry.lookup("A", str(ROOT / "b.wind"), "derive")
        self.assertIsNone(error)
        self.assertEqual(str(ROOT / "b.wind"), definition.source_path)
        self.assertEqual("function", registry.lookup(
            "A", str(ROOT / "c.wind"), "function")[0].kind)
        # Cross-file needs a binding (derive macros too); private macros
        # are never importable from outside their file.
        for binding, expected in (
            ("use crate::a::A;", "a.wind"),
            ("use crate::c::A;", "c.wind"),
        ):
            consumer = str(ROOT / "d.wind")
            registry.prepare_file(tokenize(binding), consumer)
            definition, error = registry.lookup("A", consumer, "derive" if expected == "a.wind" else "function")
            self.assertIsNone(error)
            self.assertEqual(str(ROOT / expected), definition.source_path)
        private = _registry_for({"a.wind": "#[proc_macro_derive(A)] " + signature})
        self.assertEqual((None, None), private.lookup("A", str(ROOT / "b.wind"), "derive"))
        self.assertIn("#[derive(A)]", private.lookup("A", str(ROOT / "a.wind"))[1])
        ambiguous = _registry_for({
            "a.wind": "#[proc_macro_derive(A)] pub " + signature,
            "e.wind": "#[proc_macro_derive(A)] pub " + signature,
        })
        ambiguous.prepare_file(
            tokenize("use crate::a::A;\nuse crate::e::A;"),
            str(ROOT / "f.wind"),
        )
        self.assertIn("ambiguous", ambiguous.lookup("A", str(ROOT / "f.wind"), "derive")[1])

    def test_derive_order_raw_item_appending_members_and_fixpoint(self):
        from unittest.mock import patch
        from cwind_frontend.macros.proc.expand import ProcMacroContext
        definitions = ("#[proc_macro_derive(A)] fn da(x: TokenStream) -> TokenStream {} "
                       "#[proc_macro_derive(B)] fn db(x: TokenStream) -> TokenStream {} ")
        for item in ("struct T { x: Int }", "pub enum T { One, Two }",
                     "pub struct T;"):
            for attrs in ("#[derive(A, B)]", "#[derive(A)] #[derive(B,)]"):
                with self.subTest(item=item, attrs=attrs):
                    context = ProcMacroContext(ROOT)
                    seen = []
                    def run(definition, inputs, anchor, source_path):
                        seen.append((definition.name, " ".join(t.raw for t in inputs)))
                        return tokenize("emit!();" if definition.name == "A"
                                        else "impl T { fn b(&self) {} }"), []
                    records = []
                    text = (definitions + "macro_rules! emit { () => { fn a() {} } } "
                            "struct Outer { #[cfg(all())] " + attrs + " " + item + " }")
                    with patch.object(context, "expand_proc", side_effect=run):
                        out, errors = expand_macros(tokenize(text), iter(range(100)).__next__,
                            records, proc_context=context, source_path=str(ROOT / "derive.wind"))
                    self.assertEqual([], [e.message for e in errors])
                    raw = " ".join(t.raw for t in out)
                    expected_input = " ".join(t.raw for t in tokenize("#[cfg(all())] " + item))
                    self.assertEqual([("A", expected_input), ("B", expected_input)], seen)
                    self.assertIn(expected_input + " fn a ( ) { } ; impl T", raw)
                    self.assertNotIn("derive", raw)
                    self.assertEqual(["A", "B"], [r["macro"] for r in records
                                     if r.get("macro_kind") == "proc_derive"
                                     and r["kind"] == "expansion"])

    def test_derive_unknown_invalid_target_and_syntax(self):
        from cwind_frontend.macros.proc.expand import ProcMacroContext
        for text, expected in (
            ("#[derive(Missing)] struct T {}", "cannot find derive macro 'Missing'"),
            ("#[derive(Missing)] fn f() {}", "only supported on struct or enum"),
            ("struct T { #[derive(Missing)] x: Int }", "only supported on struct or enum"),
            ("#[derive] struct T {}", "comma-separated"),
            ("#[derive(A B)] enum T {}", "comma-separated"),
            # A path is legal derive syntax now (module addressing); the
            # unknown path resolves to the cannot-find diagnostic instead.
            ("#[derive(A::B)] struct T {}", "cannot find derive macro 'A::B'"),
            ("#[derive()] struct T {}", "comma-separated"),
            ("#[proc_macro] fn A(x: TokenStream) -> TokenStream {} "
             "#[derive(A)] struct T {}", "function macro"),
        ):
            with self.subTest(text=text):
                _, errors = expand_macros(tokenize(text), iter(range(100)).__next__,
                    proc_context=ProcMacroContext(ROOT), source_path=str(ROOT / "derive.wind"))
                self.assertTrue(any(expected in e.message for e in errors),
                                [e.message for e in errors])

    def test_derive_generated_definition_is_collected_before_invocation(self):
        from unittest.mock import patch
        from cwind_frontend.macros.proc.expand import ProcMacroContext
        text = ("macro_rules! emit { () => { #[proc_macro_derive(A)] "
                "fn d(x: TokenStream) -> TokenStream {} } } "
                "#[derive(A)] struct T {} emit!();")
        context = ProcMacroContext(ROOT)
        with patch.object(context, "expand_proc", return_value=(tokenize("impl T {}"), [])) as run:
            out, errors = expand_macros(tokenize(text), iter(range(100)).__next__,
                proc_context=context, source_path=str(ROOT / "derive.wind"))
        self.assertEqual([], [e.message for e in errors])
        self.assertEqual(1, run.call_count)
        self.assertEqual("struct T { } impl T { } ;", " ".join(t.raw for t in out))

    def test_derive_empty_output_recursion_and_budget(self):
        from unittest.mock import patch
        from cwind_frontend.macros.proc.expand import ProcMacroContext
        definition = "#[proc_macro_derive(A)] fn d(x: TokenStream) -> TokenStream {} "
        for output, budget, expected in (("", 1000, None),
                                        ("#[derive(A)] struct U {}", 1000, "recursion"),
                                        ("impl Trait for T {}", 2, "token limit")):
            with self.subTest(output=output):
                context = ProcMacroContext(ROOT)
                with patch.object(context, "expand_proc", return_value=(tokenize(output), [])), \
                        patch("cwind_frontend.macros.expansion.MAX_EXPANSION_TOKENS", budget), \
                        patch.dict(os.environ, {"CWIND_RECURSION_LIMIT": "3"}):
                    out, errors = expand_macros(tokenize(definition + "#[derive(A)] struct T {}"),
                        iter(range(100)).__next__, proc_context=context,
                        source_path=str(ROOT / "derive.wind"))
                if expected:
                    self.assertTrue(any(expected in e.message for e in errors),
                                    [e.message for e in errors])
                else:
                    self.assertEqual([], errors)
                    self.assertEqual("struct T { }", " ".join(t.raw for t in out))

    def test_derive_function_names_dependencies_and_cache(self):
        from dataclasses import replace
        from unittest.mock import patch
        from cwind_frontend.macros.proc.build import BUILD_VERSION
        from cwind_frontend.macros.proc.registry import MacroExpansion
        self.assertEqual(11, BUILD_VERSION)
        source = (
            "#[proc_macro_derive(A)] fn d(x: TokenStream) -> TokenStream "
            "{ return sibling(x); } "
            "#[proc_macro_derive(B)] fn sibling(x: TokenStream) -> TokenStream "
            "{ let __cwpm_A: Int = 0; return x; }"
        )
        definitions = _collect(source, "defs.wind")[1]
        registry = ProcMacroRegistry(ROOT)
        program = generate_program(definitions[0], registry._local_defs_for(definitions[0]))
        self.assertIn("fn __cwpm_A_2 (", program)
        self.assertIn("fn sibling (", program)
        self.assertNotIn("proc_macro_derive", program)
        renamed = _collect(source.replace("fn d(", "fn renamed("), "defs.wind")[1][0]
        self.assertEqual(definition_key(definitions[0]), definition_key(renamed))
        anchor = tokenize("A")[0]
        inputs = tokenize("struct T {}")
        with patch.object(registry, "build") as build, \
                patch.object(registry, "_run_driver", return_value=MacroExpansion()) as driver:
            build.return_value.ok = True
            for definition in (definitions[0], definitions[0],
                               replace(definitions[0], kind="function")):
                registry.expand(definition, inputs, anchor=anchor, source_path="call.wind")
            self.assertEqual(2, driver.call_count)
            self.assertNotIn("item_pairs", driver.call_args.kwargs)

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
        binding = "use crate::a::tag;"
        for path in ("a.wind", "c.wind"):
            if path == "c.wind":
                registry.prepare_file(tokenize(binding), str(ROOT / "c.wind"))
            definition, error = registry.lookup("tag", str(ROOT / path), "attribute")
            self.assertIsNone(error)
            assert definition is not None
            self.assertEqual(str(ROOT / "a.wind"), definition.source_path)
        # The defining file sees its own private copy bare; a consumer
        # without a binding resolves nothing.
        definition, error = registry.lookup("tag", str(ROOT / "b.wind"), "attribute")
        self.assertIsNone(error)
        assert definition is not None
        self.assertEqual(str(ROOT / "b.wind"), definition.source_path)
        registry.prepare_file(tokenize(binding), str(ROOT / "d.wind"))
        definition, error = registry.lookup("tag", str(ROOT / "d.wind"))
        self.assertIsNone(definition)
        assert error is not None
        self.assertIn("attribute macro", error)
        private = _registry_for({"a.wind": source.format(pub="")})
        self.assertEqual((None, None), private.lookup("tag", str(ROOT / "b.wind"), "attribute"))
        private = _registry_for({
            "a.wind": source.format(pub="pub "),
            "b.wind": source.format(pub="pub "),
            "c.wind": source.format(pub="pub "),
        })
        private.prepare_file(tokenize("use crate::b::tag;\nuse crate::c::tag;"),
                             str(ROOT / "d.wind"))
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

    def test_cwpm_local_defs_rendered_body_controls_occupied_names(self):
        from dataclasses import replace

        _, defs, errors = _collect(
            '#[proc_macro] fn make(input: TokenStream) -> TokenStream '
            '{ return sibling(input); }\n'
            '#[proc_macro] fn sibling(input: TokenStream) -> TokenStream '
            '{ let __cwpm_make: Int = 0; return input; }'
        )
        self.assertEqual([], errors)
        # Use a distinct replacement body to prove we inspect local_defs,
        # not the original attributed item in file_tokens (a parallel scan
        # of file_tokens alone would incorrectly choose _2 here).
        sibling = replace(defs[1], fn_tokens=tokenize(
            'fn sibling(input: TokenStream) -> TokenStream '
            '{ let __cwpm_make_2: Int = 0; return input; }'
        ))
        program = generate_program(defs[0], {"sibling": sibling})
        self.assertIn('fn __cwpm_make (', program)
        self.assertIn('let __cwpm_make_2 :', program)
        self.assertNotIn('let __cwpm_make :', program)
        self.assertNotIn('# [ proc_macro ]', program)
        sibling = replace(sibling, fn_tokens=tokenize(
            'fn sibling(input: TokenStream) -> TokenStream '
            '{ let __cwpm_make: Int = 0; let __cwpm_make_2: Int = 0; return input; }'
        ))
        program = generate_program(defs[0], {"sibling": sibling})
        self.assertEqual(1, program.count('fn __cwpm_make_3 ('))
        self.assertEqual(program, generate_program(defs[0], {"sibling": sibling}))

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
    def test_pub_macro_is_reachable_only_through_bindings(self):
        # Module-addressed visibility: a pub macro in a foreign file is
        # invisible bare; an explicit use binding (or the prelude) binds it.
        registry = _registry_for({
            "a.wind": "#[proc_macro]\npub fn m(input: T) -> T {}\n",
        })
        self.assertEqual((None, None), registry.lookup("m", str(ROOT / "b.wind")))
        registry.prepare_file(
            tokenize("use crate::a::m;"), str(ROOT / "b.wind"))
        definition, error = registry.lookup("m", str(ROOT / "b.wind"))
        self.assertIsNone(error)
        self.assertIsNotNone(definition)
        # ...and the qualified path works without any use line.
        registry.prepare_file(tokenize(""), str(ROOT / "c.wind"))
        definition, error = registry.lookup("crate::a::m", str(ROOT / "c.wind"))
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

    def test_ambiguity_needs_conflicting_bindings(self):
        # Cross-file same-name macros coexist; only two bindings in one
        # file (or one path with two reachable targets) are ambiguous.
        registry = _registry_for({
            "a.wind": "#[proc_macro]\npub fn m(input: T) -> T {}\n",
            "b.wind": "#[proc_macro]\npub fn m(input: T) -> T {}\n",
        })
        self.assertIsNone(registry.lookup("m", str(ROOT / "c.wind"))[0])
        registry.prepare_file(
            tokenize("use crate::a::m;\nuse crate::b::m;"),
            str(ROOT / "c.wind"),
        )
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

    def test_cwpm_helper_rename_and_generator_version_cache_keys(self):
        from unittest.mock import patch

        source = (
            '#[proc_macro] fn make(input: TokenStream) -> TokenStream '
            '{ return bridge(input); }\n'
            'fn bridge(input: TokenStream) -> TokenStream '
            '{ return __cwpm_make(input); }\n'
            'fn __cwpm_make(input: TokenStream) -> TokenStream { return input; }'
        )
        first = _collect(source)[1][0]
        renamed = _collect(source.replace('__cwpm_make', 'user_helper'))[1][0]
        # A dependency-only alpha rename changes the source/internal name,
        # but not the executable's behavior or external protocol entrypoint.
        self.assertEqual(definition_key(first), definition_key(renamed))
        self.assertIn('fn __cwpm_make_2 (', generate_program(first))
        self.assertIn('fn __cwpm_make (', generate_program(renamed))
        self.assertNotEqual(generate_program(first), generate_program(renamed))
        # Direct references are definition tokens and must invalidate.
        direct = source.replace('return bridge(input)', 'return __cwpm_make(input)')
        self.assertNotEqual(self._key(direct), self._key(
            direct.replace('__cwpm_make', 'user_helper')
        ))
        with patch('cwind_frontend.macros.proc.build.BUILD_VERSION', 8):
            old_key = definition_key(first)
        self.assertNotEqual(old_key, definition_key(first))

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
    def test_derive_impl_runtime(self):
        output = self._compile_and_run(
            'use std::proc_macro::*;\n'
            'trait DeriveValue { fn value(&self) -> Int; }\n'
            '#[proc_macro_derive(Value)]\n'
            'fn derive_value(input: TokenStream) -> TokenStream {\n'
            ' let first: Token = input.get(0);\n'
            ' if !first.is_text("struct") { error("derive attribute leaked"); }\n'
            ' let name: Token = input.get(1);\n'
            ' return quote!(impl DeriveValue for #{ stream_of([name]) } { fn value(&self) -> Int { return 226; } }); }\n'
            '#[derive(Value)] struct Derived {}\n'
            'fn main() { let t: Derived = Derived {}; print(t.value()); }\n'
        )
        self.assertEqual("226\n", output.replace("\r\n", "\n"))

    def test_derive_supertrait_default_dispatch_runtime(self):
        output = self._compile_and_run(
            'use std::proc_macro::*;\n'
            'trait DeriveBase {\n'
            ' fn base_id(&self) -> Int;\n'
            ' fn inherited(&self) -> Int { return self.base_id() + 100; } }\n'
            'trait DeriveChild: DeriveBase {\n'
            ' fn child(&self) -> Int { return self.inherited() + 10; } }\n'
            '#[proc_macro_derive(Child)]\n'
            'fn derive_child(input: TokenStream) -> TokenStream {\n'
            ' let name: Token = input.get(1);\n'
            ' return quote!(impl DeriveChild for #{ stream_of([name]) } {\n'
            ' fn base_id(&self) -> Int { return self.v; } }); }\n'
            '#[derive(Child)] struct Derived { v: Int }\n'
            'fn main() { let t: Derived = Derived { 7 };\n'
            ' print(t.base_id()); print(t.inherited()); print(t.child()); }\n'
        )
        self.assertEqual("7\n107\n117\n", output.replace("\r\n", "\n"))

    def test_derive_cross_file_visibility_runtime_macro(self):
        helper = (
            'use std::proc_macro::*;\n'
            '#[proc_macro_derive(Exported)] pub fn exported(x: TokenStream) -> TokenStream '
            '{ return quote!(fn derived_export() -> Int { return 42; }); }\n'
            '#[proc_macro_derive(Private)] fn private_derive(x: TokenStream) -> TokenStream '
            '{ return stream_new(); }\n'
        )
        files = {'src/lib.wd': 'pub mod derives;\n', 'src/derives.wind': helper}
        result = self._parse('use crate::derives::Exported;\n'
                             '#[derive(Exported)] enum Derived { One }\n'
                             'fn main() { print(derived_export()); }\n', files=files)
        self.assertEqual([], [e.message for e in result.errors])
        self.assertTrue(any(getattr(item, 'name', None) == 'derived_export'
                            for item in result.program.items))
        # Without the binding the derive name does not resolve.
        result = self._parse('#[derive(Exported)] struct Derived {}\n', files=files)
        self.assertTrue(any("cannot find derive macro 'Exported'" in e.message
                            for e in result.errors), [e.message for e in result.errors])
        result = self._parse('#[derive(Private)] struct Derived {}\n', files=files)
        self.assertTrue(any("cannot find derive macro 'Private'" in e.message
                            for e in result.errors), [e.message for e in result.errors])

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
        from cwind_frontend.parser.defs import _module_roots

        root = _local_temp_dir()
        self.addCleanup(shutil.rmtree, root, True)
        (root / "Breeze.toml").write_text(
            "[package]\n"
            'name = "attrtest"\n'
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
        (root / "src").mkdir()
        definition_path = root / "src" / "attributes.wind"
        definition_path.write_text(
            "use std::proc_macro::*;\n"
            "#[proc_macro_attribute]\n"
            "pub fn clean225(a: TokenStream, b: TokenStream) -> TokenStream {\n"
            '    let first: Token = b.get(0);\n'
            '    if first.is_text("#") { error("compiler attrs leaked"); }\n'
            '    return b;\n}\n', encoding="utf-8",
        )
        (root / "src" / "lib.wd").write_text(
            "pub mod attributes;\n", encoding="utf-8",
        )
        context = ProcMacroContext(
            root, scan_dirs=[r.directory for r in _module_roots(root)]
        )
        path = root / "main.wind"
        source = (
            'use crate::attributes::clean225;\n'
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

    def _parse(self, text: str, name: str = "main.wind", jobs: int = 1,
               files: dict[str, str] | None = None):
        directory = _local_temp_dir()
        self.addCleanup(shutil.rmtree, directory, True)
        if files is not None:
            (directory / "Breeze.toml").write_text(
                '[package]\nname = "cwpmtest"\nversion = "0.0.1"\n'
                'identifier = "Dev"\nid_version = "0.0.1"\n'
                '[entry]\nsource = "./src"\nis_lib = false\nmodule = "lib.wd"\n',
                encoding="utf-8",
            )
            for rel, source in files.items():
                target = directory / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(source, encoding="utf-8")
            # Keep the invoking entry outside the module scan roots: the
            # child compiler's impl discovery must not parse this invocation
            # again while building its own macro executable.
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

    def test_cwpm_imported_helper_collision_is_avoided(self):
        for local in (False, True):
            with self.subTest(local=local):
                fresh = uuid4().hex
                name = f"imported_{fresh}"
                internal = f"__cwpm_{name}"
                source = (
                    'use std::proc_macro::TokenStream;\n'
                    f'use helpers::{internal};\n'
                )
                if local:
                    source += (
                        f'fn {internal}_2(input: TokenStream) -> TokenStream '
                        f'{{ return {internal}(input); }}\n'
                    )
                source += (
                    f'#[proc_macro] fn {name}(input: TokenStream) -> TokenStream '
                    f'{{ let fresh: String = "{fresh}"; '
                    f'return {internal + "_2" if local else internal}(input); }}\n'
                    f'fn main() {{ print({name}!(42)); }}\n'
                )
                helper = (
                    'use std::proc_macro::TokenStream;\n'
                    f'pub fn {internal}(input: TokenStream) -> TokenStream '
                    '{ return input; }\n'
                )
                _, defs, errors = _collect(source)
                self.assertEqual([], errors)
                program = generate_program(defs[0])
                chosen = internal + ("_3" if local else "_2")
                self.assertEqual(1, program.count(f'fn {chosen} ('))
                # B is loaded later as AST, not copied into program text.
                self.assertEqual(0, program.count(f'fn {internal} ('))
                files = {
                    'src/lib.wd': 'pub mod helpers;\n',
                    'src/helpers.wind': helper,
                }
                generated = self._parse(program, files=files)
                self.assertEqual([], [e.message for e in generated.errors])
                names = [getattr(item, 'name', None) for item in generated.program.items]
                self.assertEqual(1, names.count(internal))
                self.assertEqual(1, names.count(chosen))
                result = self._parse(source, files=files)
                self.assertEqual([], [e.message for e in result.errors])

    def test_cwpm_sibling_proc_macro_helper_collision_is_avoided(self):
        fresh = uuid4().hex
        name = f'sibling_{fresh}'
        internal = f'__cwpm_{name}'
        source = (
            'use std::proc_macro::TokenStream;\n'
            f'#[proc_macro] fn {internal}(input: TokenStream) -> TokenStream '
            f'{{ let {internal}_2: Int = 0; return input; }}\n'
            f'#[proc_macro] fn {name}(input: TokenStream) -> TokenStream '
            f'{{ let fresh: String = "{fresh}"; return {internal}(input); }}\n'
            f'fn main() {{ print({name}!(42)); }}\n'
        )
        _, defs, errors = _collect(source)
        self.assertEqual([], errors)
        program = generate_program(defs[1], {d.name: d for d in defs})
        self.assertEqual(1, program.count(f'fn {internal} ('))
        self.assertEqual(1, program.count(f'fn {internal}_3 ('))
        self.assertNotIn('# [ proc_macro ]', program)
        self.assertEqual([], [e.message for e in self._parse(source).errors])

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

    def test_macro_export_import_does_not_leak_private_rule(self):
        fresh = uuid4().hex
        public = f"exported_{fresh}"
        private = f"private_{fresh}"
        files = {
            "src/lib.wd": "pub mod rules;\n",
            "src/rules.wind": (
                f'#[macro_export] macro_rules! {public} {{ () => {{ "{fresh}" }} }}\n'
                f'macro_rules! {private} {{ () => {{ "private_{fresh}" }} }}\n'
                f'pub fn local() -> String {{ return {private}!(); }}\n'
            ),
        }
        # Module addressing: a foreign file needs an explicit binding for
        # the exported macro; the private sibling rule stays file-local
        # (its expansion happens at the definition site inside local()).
        source = (
            f'use rules::{{local, {public}}};\n'
            f'fn main() {{ print(local()); print({public}!());'
        )
        good = self._parse(source + " }\n", files=files)
        self.assertEqual([], [e.message for e in good.errors])
        result = self._parse(source + f" {private}!(); }}\n", files=files)
        messages = [e.message for e in result.errors]
        self.assertTrue(any(f"cannot find macro '{private}'" in m and
                            "module system" in m for m in messages), messages)
        self.assertFalse(any(f"cannot find macro '{public}'" in m for m in messages))
        records = getattr(result.program, "_macro_records", [])
        self.assertTrue(any(r.get("macro") == public and r.get("kind") == "expansion"
                            for r in records), records)
        self.assertFalse(any(r.get("macro") == private and r.get("kind") == "expansion"
                             and Path(r["source"]).name == "main.wind"
                             for r in records), records)
        self.assertTrue(any(r.get("macro") == private and r.get("kind") == "expansion"
                            and Path(r["source"]).name == "rules.wind"
                            for r in records), records)

    def test_print_macros_surface_parity(self):
        output = self._compile_and_run(
            'fn add(a: Int, b: Int) -> Int { return a + b; }\n'
            'fn main() { let x: Int = 40;\n'
            r'print!("{{escaped}}\t\"{}\"\\", x + 2);' '\n'
            'println!("{}:{}", add(1, 2), x + 2,);\n'
            'println!("literal",);\n'
            '}\n'
        )
        self.assertEqual('{escaped}\t"42"\\3:42\nliteral\n',
                         output.replace("\r\n", "\n"))
        for name in ("print", "println"):
            for argument in ("x", "x + 2", "42"):
                with self.subTest(name=name, argument=argument):
                    result = self._parse(
                        f'fn main() {{ let x: Int = 40; {name}!({argument}); }}\n'
                    )
                    self.assertTrue(any("template must be a string literal" in e.message
                                        for e in result.errors),
                                    [e.message for e in result.errors])

    def test_print_macros_runtime_output(self):
        # Both wrappers preserve format! diagnostics and runtime newline policy.
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

    def test_macro_export_rules_cross_file(self):
        from cwind_frontend.macros.proc import ProcMacroContext
        root = self._project({
            "src/lib.wd": "pub mod rules;\n",
            "src/rules.wind": "#[macro_export] macro_rules! answer { () => { 42 } }",
        })
        context = ProcMacroContext(root, scan_dirs=[root / "src"])
        out, errors = expand_macros(
            tokenize("use crate::rules::answer;\nanswer!()"),
            iter(range(100)).__next__,
            proc_context=context, source_path=str(root / "main.wind"))
        self.assertEqual([], [e.message for e in errors])
        self.assertEqual("42", " ".join(t.raw for t in out[-1:]))
        out, errors = expand_macros(
            tokenize("crate::rules::answer!()"),
            iter(range(100)).__next__,
            proc_context=context, source_path=str(root / "main.wind"))
        self.assertEqual([], [e.message for e in errors])
        self.assertEqual("42", " ".join(t.raw for t in out))

    def test_macro_export_rules_stay_file_local_without_export(self):
        from cwind_frontend.macros.proc import ProcMacroContext
        root = self._project({
            "src/rules.wind": "macro_rules! answer { () => { 42 } }",
        })
        context = ProcMacroContext(root, scan_dirs=[root / "src"])
        owner = root / "src" / "rules.wind"
        out, errors = expand_macros(tokenize_file(owner) + tokenize("answer!()"),
                                   iter(range(100)).__next__, proc_context=context,
                                   source_path=str(owner.resolve()))
        self.assertEqual([], [e.message for e in errors])
        self.assertEqual("42", " ".join(t.raw for t in out))
        # Reusing the same compile context must not leak the local definition.
        out, errors = expand_macros(tokenize("answer!()"), iter(range(100)).__next__,
                                    proc_context=context, source_path=str(root / "main.wind"))
        self.assertTrue(any("cannot find macro 'answer'" in e.message for e in errors),
                        [e.message for e in errors])

    def test_macro_export_name_collision_errors(self):
        from cwind_frontend.macros.proc import ProcMacroContext
        for other in (
            "#[macro_export] macro_rules! clash { () => { 2 } }",
            "#[proc_macro] pub fn clash(x: TokenStream) -> TokenStream { return x; }",
        ):
            with self.subTest(other=other):
                root = self._project({
                    "src/lib.wd": "pub mod a;\npub mod b;\n",
                    "src/a.wind": "#[macro_export] macro_rules! clash { () => { 1 } }",
                    "src/b.wind": other,
                })
                context = ProcMacroContext(root, scan_dirs=[root / "src"])
                # Coexisting same-name macros in different modules are
                # fine; ambiguity requires two bindings in one file.
                consumer = str(root / "main.wind")
                self.assertEqual(
                    (None, None), context.registry.lookup_rules("clash", consumer))
                context.registry.prepare_file(
                    tokenize("use crate::a::clash;\nuse crate::b::clash;"),
                    consumer,
                )
                _, conflict = context.registry.lookup_rules("clash", consumer)
                self.assertIsNotNone(conflict)
                self.assertIn("ambiguous", conflict)
                # Discovery is declaration-driven, so a fresh scan of the
                # same tree produces the same diagnostic (no dict-order
                # dependence).
                context2 = ProcMacroContext(root, scan_dirs=[root / "src"])
                context2.registry.prepare_file(
                    tokenize("use crate::a::clash;\nuse crate::b::clash;"), consumer)
                self.assertEqual(
                    conflict, context2.registry.lookup_rules("clash", consumer)[1])
                _, errors = expand_macros(
                    tokenize("use crate::a::clash;\nuse crate::b::clash;\nclash!()"),
                    iter(range(100)).__next__,
                    proc_context=context, source_path=str(root / "main.wind"))
                self.assertTrue(any(e.message == conflict for e in errors),
                                [e.message for e in errors])

    def test_macro_export_rules_cross_file_parse(self):
        root = self._project({
            "src/lib.wd": "pub mod maker;\n",
            "src/maker.wind": "#[macro_export] macro_rules! answer { () => { 42 } }\n",
            "src/main.wind": (
                "use crate::maker::answer;\n"
                "fn f() -> Int { return answer!(); }\n"
            ),
        })
        path = root / "src" / "main.wind"
        result = parse_with_errors(tokenize_file(path), source_path=str(path.resolve()))
        self.assertEqual([], [e.message for e in result.errors])

    def test_pub_macro_importable_from_other_file(self):
        from cwind_frontend.macros.proc import ProcMacroContext
        from cwind_frontend.parser.defs import _module_roots

        root = self._project({
            "src/lib.wd": "pub mod util;\n",
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
        main = str(root / "src" / "main.wind")
        # Module-addressed: not visible bare from a foreign file...
        self.assertEqual((None, None), context.lookup("tag", main))
        # ...but reachable through its module path...
        definition, error = context.lookup("crate::util::tag", main)
        self.assertIsNone(error)
        self.assertIsNotNone(definition)
        assert definition is not None
        self.assertEqual(
            str((root / "src" / "util.wind").resolve()),
            definition.source_path,
        )
        # ...and through an explicit use binding.
        context.registry.prepare_file(tokenize("use crate::util::tag;"), main)
        definition, error = context.lookup("tag", main)
        self.assertIsNone(error)
        self.assertIsNotNone(definition)


class ModulePathMacroTests(unittest.TestCase):
    """Module-path addressing (design: macros follow the module system).

    Every scenario materializes a Breeze project under the repo (std
    discovery + toolchain anchoring) and drives the full parse pipeline.
    """

    def _project(self, files: dict[str, str]) -> Path:
        from cwind_frontend.parser.defs import _module_roots

        root = _local_temp_dir()
        self.addCleanup(shutil.rmtree, root, True)
        (root / "Breeze.toml").write_text(
            "[package]\n"
            'name = "modmacro"\n'
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

    def _parse(self, root: Path, rel: str = "src/main.wind", stage: str = "parse"):
        path = root / rel
        return parse_with_errors(
            tokenize_file(path), source_path=str(path.resolve())
        )

    def test_qualified_call_use_binding_and_reexport_chain(self):
        files = {
            "src/lib.wd": "pub mod inner;\n",
            "src/inner.wind": (
                "#[macro_export] macro_rules! hello { () => { 7 } }\n"
                "#[macro_export] macro_rules! hidden { () => { 8 } }\n"
                "macro_rules! private_helper { () => { 9 } }\n"
            ),
        }
        root = self._project(files)
        # Qualified function-like call, no import needed.
        path = root / "main.wind"
        path.write_text(
            "fn main() -> Int { let x: Int = crate::inner::hello!(); "
            "return x; }\n", encoding="utf-8", newline="\n")
        result = parse_with_errors(tokenize_file(path), source_path=str(path))
        self.assertEqual([], [e.message for e in result.errors])
        # use binding WITHOUT '!' (Rust 2018 style) makes it callable bare.
        path.write_text(
            "use crate::inner::hello;\n"
            "fn main() -> Int { return hello!(); }\n",
            encoding="utf-8", newline="\n")
        result = parse_with_errors(tokenize_file(path), source_path=str(path))
        self.assertEqual([], [e.message for e in result.errors])
        # pub use re-export chain through a facade: main.wind + facade.wind.
        path.write_text(
            "pub use crate::inner::hello;\n", encoding="utf-8", newline="\n")
        (root / "src" / "lib.wd").write_text(
            "pub mod inner;\npub mod facade;\n", encoding="utf-8", newline="\n")
        (root / "src" / "facade.wind").write_text(
            "pub use crate::inner::hello;\n", encoding="utf-8", newline="\n")
        path.write_text(
            "use crate::facade::hello;\n"
            "fn main() -> Int { return hello!(); }\n",
            encoding="utf-8", newline="\n")
        result = parse_with_errors(tokenize_file(path), source_path=str(path))
        self.assertEqual([], [e.message for e in result.errors])

    def test_undeclared_file_is_not_discoverable(self):
        # Discovery is declaration-driven: sibling.wind is on disk but no
        # mod file declares it, so its pub macro is unreachable by path.
        files = {
            "src/lib.wd": "pub mod declared;\n",
            "src/declared.wind": "#[macro_export] macro_rules! live { () => { 1 } }\n",
            "src/sibling.wind": "#[macro_export] macro_rules! ghost { () => { 2 } }\n",
        }
        root = self._project(files)
        path = root / "main.wind"
        path.write_text(
            "fn main() -> Int { return crate::sibling::ghost!(); }\n",
            encoding="utf-8", newline="\n")
        result = parse_with_errors(tokenize_file(path), source_path=str(path))
        messages = [e.message for e in result.errors]
        self.assertTrue(any("cannot find macro 'crate::sibling::ghost'" in m
                            for m in messages), messages)
        # The declared sibling still works.
        path.write_text(
            "fn main() -> Int { return crate::declared::live!(); }\n",
            encoding="utf-8", newline="\n")
        result = parse_with_errors(tokenize_file(path), source_path=str(path))
        self.assertEqual([], [e.message for e in result.errors])

    def test_private_use_is_not_reexported(self):
        # facade.wind re-exports hello publicly but only *uses* hidden
        # privately: an importer of the facade resolves hello, not hidden.
        files = {
            "src/lib.wd": "pub mod inner;\npub mod facade;\n",
            "src/inner.wind": (
                "#[macro_export] macro_rules! hello { () => { 7 } }\n"
                "#[macro_export] macro_rules! hidden { () => { 8 } }\n"
            ),
            "src/facade.wind": (
                "pub use crate::inner::hello;\n"
                "use crate::inner::hidden;\n"
            ),
        }
        root = self._project(files)
        path = root / "main.wind"
        path.write_text(
            "use crate::facade::hello;\n"
            "fn main() -> Int { return hello!(); }\n",
            encoding="utf-8", newline="\n")
        result = parse_with_errors(tokenize_file(path), source_path=str(path))
        self.assertEqual([], [e.message for e in result.errors])
        path.write_text(
            "use crate::facade::hidden;\n"
            "fn main() -> Int { return hidden!(); }\n",
            encoding="utf-8", newline="\n")
        result = parse_with_errors(tokenize_file(path), source_path=str(path))
        messages = [e.message for e in result.errors]
        self.assertTrue(any("hidden" in m for m in messages), messages)

    def test_definition_site_format_dependency(self):
        # A caller importing ONLY println! still gets format! inside the
        # wrapper body: print.wind's own `use std::ext::format::format;`
        # resolves at the definition site.
        files = {
            "src/lib.wd": "pub mod w;\n",
            "src/w.wind": (
                "pub use std::ext::format::format;\n"
                "#[macro_export] macro_rules! shout { ($($t:token)+) => "
                "{ _write(format!($($t)+)); } }\n"
            ),
        }
        root = self._project(files)
        path = root / "main.wind"
        path.write_text(
            "use crate::w::shout;\n"
            'fn main() { shout!("v={}", 4); }\n',
            encoding="utf-8", newline="\n")
        result = parse_with_errors(tokenize_file(path), source_path=str(path))
        self.assertEqual([], [e.message for e in result.errors])
        # The wrapper body's format! call expanded without any error, and
        # the caller never imported format itself.
        from dataclasses import fields as dc_fields

        from cwind_frontend.ast_components.ast import FnDecl

        fn = next(i for i in result.program.items
                  if isinstance(i, FnDecl) and i.name == "main")

        def leaves(node) -> list[str]:
            out = []
            if hasattr(node, "raw"):
                out.append(node.raw)
            for field in dc_fields(node):
                value = getattr(node, field.name)
                if hasattr(value, "__dataclass_fields__"):
                    out.extend(leaves(value))
                elif isinstance(value, list):
                    for element in value:
                        if hasattr(element, "__dataclass_fields__"):
                            out.extend(leaves(element))
            return out

        raw = " ".join(leaves(fn))
        # format! consumed the template literal (its diagnostics ran) and
        # the caller never imported format itself.
        self.assertIn('"v="', raw)
        self.assertIn("4", raw)

    def test_attribute_and_derive_module_paths(self):
        from cwind_frontend.macros.proc import ProcMacroContext
        from cwind_frontend.parser.defs import _module_roots

        files = {
            "src/lib.wd": "pub mod defs;\n",
            "src/defs.wind": (
                "use std::proc_macro::*;\n"
                "#[proc_macro_attribute]\n"
                "pub fn id_attr(a: TokenStream, x: TokenStream)"
                " -> TokenStream { return x; }\n"
                "#[proc_macro_derive(Marker)]\n"
                "pub fn marker(x: TokenStream) -> TokenStream"
                " { return stream_new(); }\n"
            ),
        }
        root = self._project(files)
        context = ProcMacroContext(
            root, scan_dirs=[r.directory for r in _module_roots(root)]
        )
        main = str(root / "main.wind")
        attribute, error = context.lookup(
            "crate::defs::id_attr", main, "attribute")
        self.assertIsNone(error)
        self.assertIsNotNone(attribute)
        derive, error = context.lookup(
            "crate::defs::Marker", main, "derive")
        self.assertIsNone(error)
        self.assertIsNotNone(derive)
        # ...and the expander accepts the qualified spellings.
        from cwind_frontend.macros.proc.expand import ProcMacroContext as _Ctx
        expanded, errors = expand_macros(
            tokenize(
                "#[crate::defs::id_attr] fn f() {}\n"
                "#[derive(crate::defs::Marker)] struct T {}\n"
            ),
            iter(range(1000)).__next__,
            proc_context=context, source_path=main,
        )
        self.assertEqual([], [e.message for e in errors])
        raw = " ".join(t.raw for t in expanded)
        self.assertIn("fn f", raw)
        self.assertIn("struct T", raw)
        self.assertNotIn("id_attr", raw)
        self.assertNotIn("Marker", raw)

    def test_cli_smoke_temp_project(self):
        from cwind_frontend.cli import main as cli_main

        files = {
            "src/lib.wd": "pub mod greets;\n",
            "src/greets.wind": (
                "#[macro_export] macro_rules! hi { () => { 42 } }\n"
            ),
        }
        root = self._project(files)
        path = root / "main.wind"
        path.write_text(
            "use crate::greets::hi;\n"
            "fn main() {\n"
            '    println!("start");\n'
            '    std::ext::print::print("qualified {}", hi!());\n'
            "    println!();\n"
            "}\n",
            encoding="utf-8", newline="\n")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli_main(["--parse", str(path)])
        self.assertEqual(0, code, err.getvalue())


class NoStdIsolationTests(unittest.TestCase):
    """todo-179: building a macro body stays off the whole std tree.

    A generated macro program is self-contained: it compiles in no-std
    mode, imports only its bounded dependency closure, and pulls trait
    impls from ``libs/expansion`` alone.  Unrelated std modules (and any
    procedure macros they might invoke) are never parsed or expanded, so
    no unrelated macro executable is built.
    """

    def _generated_format_program(self) -> str:
        path = ROOT / "libs" / "ext" / "format.wind"
        _stream, defs, errors = collect_proc_macros(
            tokenize_file(path), str(path)
        )
        self.assertEqual([], [e.message for e in errors])
        by_name = {d.name: d for d in defs}
        return generate_program(by_name["format"], by_name)

    def test_macro_body_does_not_load_whole_std(self):
        from unittest.mock import patch

        import cwind_frontend.parser.defs as defs_mod
        import cwind_frontend.parser.items as items_mod

        program = self._generated_format_program()
        directory = _local_temp_dir()
        self.addCleanup(shutil.rmtree, directory, True)
        source = directory / "generated.wind"
        source.write_text(program, encoding="utf-8", newline="\n")

        scan_calls: list = []
        real = defs_mod._impl_registry_for

        def spy(*args, **kwargs):
            scan_calls.append(kwargs.get("directories"))
            return real(*args, **kwargs)

        with patch.object(items_mod, "_impl_registry_for", side_effect=spy):
            result = parse_with_errors(
                tokenize_file(source), source_path=str(source), no_std=True
            )
        self.assertEqual([], [e.message for e in result.errors])
        base = (ROOT / "libs").resolve()
        loaded = {Path(p).resolve() for p in result.modules}
        for unrelated in (
            "libcbind/stdio.wind",
            "libcbind/math.wind",
            "baseimpl/file.wind",
            "baseimpl/hashmap.wind",
            "baseimpl/random/mod.wind",
            "ext/print.wind",
            "ext/format.wind",
            "memory/layout.wind",
        ):
            self.assertNotIn(
                (base / unrelated).resolve(),
                loaded,
                f"unrelated std module loaded: {unrelated}",
            )
        # The trait-impl pull consulted only the std impl directory.
        self.assertTrue(scan_calls)
        for directories in scan_calls:
            self.assertIsNotNone(directories)
            self.assertEqual(
                [(base / "expansion").resolve()],
                [Path(d).resolve() for d in directories],
            )

    def test_program_without_proc_macros_builds_nothing(self):
        from unittest.mock import patch

        directory = _local_temp_dir()
        self.addCleanup(shutil.rmtree, directory, True)
        source = directory / "main.wind"
        source.write_text(
            "fn main() { let x: Int = 1 + 2; }\n",
            encoding="utf-8", newline="\n",
        )
        with patch(
            "cwind_frontend.macros.proc.registry.build_macro",
            side_effect=AssertionError("must not build a procedure macro"),
        ):
            result = parse_with_errors(
                tokenize_file(source), source_path=str(source)
            )
        self.assertEqual([], [e.message for e in result.errors])


if __name__ == "__main__":
    unittest.main()
