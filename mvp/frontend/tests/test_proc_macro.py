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

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent.parent.parent.parent
for path in (ROOT / "mvp/frontend/src", ROOT / "mvp/frontend/tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from cwind_frontend.ast_components.token import TokenKind  # noqa: E402
from cwind_frontend.lexer import tokenize, tokenize_file  # noqa: E402
from cwind_frontend.macros import expand_macros  # noqa: E402
from cwind_frontend.macros.proc.builtins import expand_builtin  # noqa: E402
from cwind_frontend.macros.proc.collect import collect_proc_macros  # noqa: E402
from cwind_frontend.macros.proc.deps import generate_program  # noqa: E402
from cwind_frontend.macros.proc.driver import _decode_records, _encode_tokens  # noqa: E402
from cwind_frontend.macros.proc.protocol import (  # noqa: E402
    pairs_to_tokens,
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
        wire = _encode_tokens([["ident", "foo"], ["punct", "!"]])
        self.assertEqual("2\nident\nfoo\npunct\n!\n", wire)
        tokens, diagnostics, noise = _decode_records(
            "T\tident\tfoo\nD\terror\tboom\n"
        )
        self.assertEqual([["ident", "foo"]], tokens)
        self.assertEqual(
            [{"level": "error", "message": "boom"}], diagnostics
        )
        self.assertEqual([], noise)


class DependencyTests(unittest.TestCase):
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
        # The macro function itself and the harness are present.
        self.assertIn("fn make", program)
        self.assertIn("stream_from_stdin", program)
        # Self-recursion is not a dependency (DCE): a macro that only
        # calls itself is never pulled into its own dependency set.
        self.assertEqual(1, program.count("fn make"))

    def test_self_reference_does_not_duplicate(self):
        source = (
            "#[proc_macro]\n"
            "pub fn loop_macro(input: TokenStream) -> TokenStream {\n"
            "    return loop_macro(input);\n"
            "}\n"
        )
        definition = self._definition(source)
        program = generate_program(definition, {definition.name: definition})
        self.assertEqual(1, program.count("fn loop_macro"))


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


def _toolchain_available() -> bool:
    from cwind_frontend.macros.proc.build import toolchain_available

    return toolchain_available()


@unittest.skipUnless(
    _toolchain_available(),
    "procedure macros need the CWind backend (cwindc)",
)
class ProcedureMacroEndToEndTests(unittest.TestCase):
    def _parse(self, text: str, name: str = "main.wind"):
        directory = _local_temp_dir()
        self.addCleanup(shutil.rmtree, directory, True)
        path = directory / name
        path.write_text(text, encoding="utf-8", newline="\n")
        return parse_with_errors(
            tokenize_file(path), source_path=str(path.resolve())
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

    def test_stringify_macro_runtime_output(self):
        output = self._compile_and_run(
            "fn main() { print(stringify!(a + b)); }\n"
        )
        self.assertEqual("a + b\n", output.replace("\r\n", "\n"))

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
