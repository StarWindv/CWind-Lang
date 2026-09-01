"""todo-79: the per-module scope table (no single flat namespace).

Covers the three flat-namespace limitations the todo names:

- private helpers pulled into an import's dependency closure are flattened
  under mangled final names and can no longer be called by bare name;
- same-named private top-level items of different modules coexist without
  ``duplicate definition`` errors;
- items that only ride along on someone else's *non-pub* ``use`` stay
  invisible to files that never imported them (transitive visibility now
  matches Rust).

Also covers the mechanics that keep the flattening transparent: qualified
``mod::item`` calls, wildcard/explicit imports, reference rewriting inside
renamed bodies (recursion, statics, enum patterns, types), local shadowing,
and the legacy-permissive behavior of untagged (stdin) sources.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent.parent.parent.parent
for path in (ROOT / "mvp/frontend/src", ROOT / "mvp/frontend/tests"):
    sys.path.insert(0, str(path))

import harness  # noqa: E402,F401  (sys.path side effect)
from cwind_frontend import run_sa_with_errors  # noqa: E402
from cwind_frontend.parser.parser import parse_with_errors  # noqa: E402
from cwind_frontend import tokenize_file  # noqa: E402


class ScopeTableScaffold(unittest.TestCase):
    """Temp project with a libs/ tree and a main.wind entry."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, relative: str, text: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        parts = Path(relative).parts
        if parts and parts[0] in ("libs", "src") and path.suffix in (".wind", ".wd"):
            harness.sync_mod_wind(self.root, path)
        return path

    def build(self, files: dict[str, str]):
        for rel, text in files.items():
            self.write(rel, text)
        entry = self.root / "main.wind"
        return parse_with_errors(
            tokenize_file(entry), source_path=str(entry.resolve())
        )

    def assert_clean(self, parsed) -> None:
        self.assertEqual(
            [], [e.message for e in parsed.errors],
            f"parse errors: {[e.message for e in parsed.errors]}",
        )

    def sa_errors(self, parsed) -> list[str]:
        result = run_sa_with_errors(parsed.program)
        return [e.message for e in result.errors]


class PrivateClosureItems(ScopeTableScaffold):
    """Limitation 1: closure privates must not be callable by bare name."""

    def test_private_helper_referenced_by_public_api(self):
        parsed = self.build({
            "libs/thing.wind": (
                "pub fn pubf() -> Int { return helper(); }\n"
                "fn helper() -> Int { return 2; }\n"
            ),
            "main.wind": (
                "use thing::*;\n"
                "fn main() -> Int { return pubf(); }\n"
            ),
        })
        self.assert_clean(parsed)
        self.assertEqual([], self.sa_errors(parsed))

    def test_private_helper_is_not_bare_callable_from_importer(self):
        parsed = self.build({
            "libs/thing.wind": (
                "pub fn pubf() -> Int { return helper(); }\n"
                "fn helper() -> Int { return 2; }\n"
            ),
            "main.wind": (
                "use thing::*;\n"
                "fn main() -> Int { return pubf() + helper(); }\n"
            ),
        })
        self.assert_clean(parsed)
        errors = self.sa_errors(parsed)
        self.assertTrue(any("helper" in e for e in errors), errors)

    def test_renamed_helper_disappears_under_its_plain_name(self):
        parsed = self.build({
            "libs/thing.wind": (
                "pub fn pubf() -> Int { return helper(); }\n"
                "fn helper() -> Int { return 2; }\n"
            ),
            "main.wind": "use thing::*;\nfn main() -> Int { return 0; }\n",
        })
        self.assert_clean(parsed)
        self.assertEqual([], self.sa_errors(parsed))
        fn_names = [
            item.name
            for item in parsed.program.items
            if type(item).__name__ == "FnDecl"
        ]
        self.assertIn("pubf", fn_names)
        self.assertNotIn("helper", fn_names)
        self.assertEqual(
            1, sum(1 for n in fn_names if n.startswith("helper__"))
        )

    def test_renamed_helper_recursion_still_resolves(self):
        parsed = self.build({
            "libs/rec.wind": (
                "pub fn spin(n: Int) -> Int {\n"
                "    if (n <= 0) { return 0; }\n"
                "    return go(n - 1);\n"
                "}\n"
                "fn go(n: Int) -> Int {\n"
                "    if (n <= 0) { return 7; }\n"
                "    return go(n - 1);\n"
                "}\n"
            ),
            "main.wind": "use rec::*;\nfn main() -> Int { return spin(3); }\n",
        })
        self.assert_clean(parsed)
        self.assertEqual([], self.sa_errors(parsed))


class SameNamedPrivates(ScopeTableScaffold):
    """Limitation 2: cross-module same-name privates no longer collide."""

    def test_two_modules_same_named_privates_compile(self):
        parsed = self.build({
            "libs/a.wind": (
                "pub fn fa() -> Int { return util() + 1; }\n"
                "fn util() -> Int { return 10; }\n"
            ),
            "libs/b.wind": (
                "pub fn fb() -> Int { return util() + 2; }\n"
                "fn util() -> Int { return 20; }\n"
            ),
            "main.wind": (
                "use a;\nuse b;\n"
                "fn main() -> Int { return a::fa() + b::fb(); }\n"
            ),
        })
        self.assert_clean(parsed)
        self.assertEqual([], self.sa_errors(parsed))

    def test_same_named_privates_get_distinct_final_names(self):
        parsed = self.build({
            "libs/a.wind": (
                "pub fn fa() -> Int { return util() + 1; }\n"
                "fn util() -> Int { return 10; }\n"
            ),
            "libs/b.wind": (
                "pub fn fb() -> Int { return util() + 2; }\n"
                "fn util() -> Int { return 20; }\n"
            ),
            "main.wind": (
                "use a;\nuse b;\n"
                "fn main() -> Int { return a::fa() + b::fb(); }\n"
            ),
        })
        self.assert_clean(parsed)
        utils = {
            item.name
            for item in parsed.program.items
            if type(item).__name__ == "FnDecl" and item.name.startswith("util")
        }
        self.assertEqual(2, len(utils))
        self.assertTrue(all(name.startswith("util__") for name in utils))

    def test_duplicate_public_names_are_still_rejected(self):
        parsed = self.build({
            "libs/a.wind": "pub fn dup() -> Int { return 1; }\n",
            "libs/b.wind": "pub fn dup() -> Int { return 2; }\n",
            "main.wind": (
                "use a::*;\nuse b::*;\n"
                "fn main() -> Int { return 0; }\n"
            ),
        })
        self.assert_clean(parsed)
        self.assertTrue(
            any("duplicate definition of 'dup'" in e
                for e in self.sa_errors(parsed))
        )


class TransitiveVisibility(ScopeTableScaffold):
    """Limitation 3: non-pub use transitive items stay module-internal."""

    _PANIC = 'pub fn panic(msg: &String) -> ! { exit(1); }\n'
    _OPT = (
        "use std::panic;\n"
        "pub enum Opt { None, Some(Int) }\n"
        "extra Opt {\n"
        "    pub fn unwrap_or_zero(self) -> Int {\n"
        "        return match (self) {\n"
        '            Opt::Some(v) => v,\n'
        "            _ => panic::panic(&\"none\"),\n"
        "        };\n"
        "    }\n"
        "}\n"
    )

    def test_module_body_may_call_transitive_import(self):
        parsed = self.build({
            "libs/panic.wind": self._PANIC,
            "libs/opt.wind": self._OPT,
            "main.wind": (
                "use std::opt::Opt;\n"
                "fn main() -> Int {\n"
                "    let o: Opt = Opt::Some(5);\n"
                "    return o.unwrap_or_zero();\n"
                "}\n"
            ),
        })
        self.assert_clean(parsed)
        self.assertEqual([], self.sa_errors(parsed))

    def test_importer_cannot_bare_call_someone_elses_dependency(self):
        parsed = self.build({
            "libs/panic.wind": self._PANIC,
            "libs/opt.wind": self._OPT,
            "main.wind": (
                "use std::opt::Opt;\n"
                "fn main() -> Int {\n"
                "    panic(\"leak\");\n"
                "    return 0;\n"
                "}\n"
            ),
        })
        self.assert_clean(parsed)
        errors = self.sa_errors(parsed)
        self.assertTrue(
            any("panic" in e and "not visible" in e for e in errors),
            errors,
        )

    def test_directly_imported_dependency_stays_callable(self):
        parsed = self.build({
            "libs/panic.wind": self._PANIC,
            "main.wind": (
                "use std::panic::*;\n"
                "fn main() -> Int { exit(0); return 0; }\n"
            ),
        })
        self.assert_clean(parsed)
        self.assertEqual([], self.sa_errors(parsed))

    def test_transitive_type_not_usable_in_annotations(self):
        parsed = self.build({
            "libs/helper.wind": (
                "pub struct Token { value: Int }\n"
            ),
            "libs/mid.wind": (
                "use std::helper::Token;\n"
                "pub fn touch(t: Token) -> Int { return t.value; }\n"
            ),
            "main.wind": (
                "use mid;\n"
                "fn main() -> Int {\n"
                "    let t: Token = Token { 1 };\n"
                "    return mid::touch(t);\n"
                "}\n"
            ),
        })
        self.assert_clean(parsed)
        errors = self.sa_errors(parsed)
        self.assertTrue(any("Token" in e for e in errors), errors)

    def test_imported_type_remains_usable(self):
        parsed = self.build({
            "libs/helper.wind": (
                "pub struct Token { pub value: Int }\n"
            ),
            "main.wind": (
                "use std::helper::Token;\n"
                "fn main() -> Int {\n"
                "    let t: Token = Token { 1 };\n"
                "    return t.value;\n"
                "}\n"
            ),
        })
        self.assert_clean(parsed)
        self.assertEqual([], self.sa_errors(parsed))


class ReferenceRewriting(ScopeTableScaffold):
    """Renamed bodies keep working: statics, enums, consts, shadowing."""

    def test_static_and_enum_refs_inside_renamed_owner(self):
        parsed = self.build({
            "libs/store.wind": (
                "pub struct Box2 { pub v: Int }\n"
                "enum State { On(Int), Off }\n"
                "const LIMIT: Int = 9;\n"
                "extra Box2 {\n"
                "    pub fn pick(self) -> Int {\n"
                "        let s: State = State::On(LIMIT);\n"
                "        return match (s) {\n"
                "            State::On(v) => v,\n"
                "            _ => 0,\n"
                "        };\n"
                "    }\n"
                "}\n"
            ),
            "main.wind": (
                "use std::store::Box2;\n"
                "fn main() -> Int {\n"
                "    let b: Box2 = Box2 { 3 };\n"
                "    return b.pick();\n"
                "}\n"
            ),
        })
        self.assert_clean(parsed)
        self.assertEqual([], self.sa_errors(parsed))

    def test_local_shadowing_blocks_rewrite(self):
        # A local binding named like the module's own private item must not
        # be rewritten to the mangled final name.
        parsed = self.build({
            "libs/m.wind": (
                "pub fn api(x: Int) -> Int {\n"
                "    let util: Int = x * 2;\n"
                "    return inner(util) + util;\n"
                "}\n"
                "fn util() -> Int { return 7; }\n"
                "fn inner(v: Int) -> Int { return v; }\n"
            ),
            "main.wind": (
                "use std::m::api;\n"
                "fn main() -> Int { return api(5); }\n"
            ),
        })
        self.assert_clean(parsed)
        self.assertEqual([], self.sa_errors(parsed))

    def test_param_shadowing_in_library_method(self):
        parsed = self.build({
            "libs/m.wind": (
                "pub struct P { pub v: Int }\n"
                "fn scale(v: Int) -> Int { return v * 3; }\n"
                "extra P {\n"
                "    pub fn twice(self, v: Int) -> Int {\n"
                "        return scale(v) + v;\n"
                "    }\n"
                "}\n"
            ),
            "main.wind": (
                "use std::m::P;\n"
                "fn main() -> Int {\n"
                "    let p: P = P { 1 };\n"
                "    return p.twice(4);\n"
                "}\n"
            ),
        })
        self.assert_clean(parsed)
        self.assertEqual([], self.sa_errors(parsed))

    def test_explicit_item_import_brings_private_method_helper(self):
        parsed = self.build({
            "libs/optmod.wind": (
                "pub enum Opt<T> { None, Some(T) }\n"
                "fn fail() -> ! { exit(1); }\n"
                "extra<T> Opt<T> {\n"
                "    pub fn unwrap(self) -> T {\n"
                "        return match (self) {\n"
                "            Opt::Some(val) => val,\n"
                "            _ => fail(),\n"
                "        };\n"
                "    }\n"
                "}\n"
            ),
            "main.wind": (
                "use optmod::Opt;\n"
                "fn main() -> Int {\n"
                "    let o: Opt<Int> = Opt::Some(3);\n"
                "    print(o.unwrap());\n"
                "    return 0;\n"
                "}\n"
            ),
        })
        self.assert_clean(parsed)
        self.assertEqual([], self.sa_errors(parsed))


class ModuleTableBookkeeping(ScopeTableScaffold):
    """The parser-built table feeds SA gating and provenance tooling."""

    def test_table_groups_visible_names_per_file(self):
        parsed = self.build({
            "libs/thing.wind": (
                "pub fn pubf() -> Int { return helper(); }\n"
                "fn helper() -> Int { return 2; }\n"
            ),
            "main.wind": (
                "use thing::*;\n"
                "fn main() -> Int { return pubf(); }\n"
            ),
        })
        self.assert_clean(parsed)
        table = getattr(parsed.program, "_module_table")
        thing_path = str((self.root / "libs" / "thing.wind").resolve())
        main_path = str((self.root / "main.wind").resolve())
        self.assertIn("pubf", table[thing_path]["visible"])
        self.assertNotIn("helper", table[thing_path]["visible"])
        self.assertIn("pubf", table[main_path]["visible"])
        self.assertIn("main", table[main_path]["visible"])

    def test_std_prelude_exports_are_visible_to_entry(self):
        parsed = self.build({
            "main.wind": "fn main() -> Int { return 0; }\n",
        })
        self.assert_clean(parsed)
        table = getattr(parsed.program, "_module_table")
        prelude = [
            home for home in table
            if home.endswith(("prelude.wind", "prelude.wd"))
        ]
        if prelude:  # only present when the repo libs tree is reachable
            self.assertIn("Option", table[prelude[0]]["visible"])

    def test_untagged_sources_stay_permissive(self):
        # stdin / in-memory sources carry no source_module: the legacy
        # behavior (everything resolvable) must survive.
        from cwind_frontend import parse_source

        program = parse_source(
            "fn hidden() -> Int { return 1; }\n"
            "fn main() -> Int { return hidden(); }\n"
        )
        result = run_sa_with_errors(program)
        self.assertEqual([], [e.message for e in result.errors])
        # Untagged items are bucketed under ``None``; SA never gates them
        # because their ``source_module`` is absent.
        self.assertEqual([None], list(getattr(program, "_module_table", {})))


if __name__ == "__main__":
    unittest.main()
