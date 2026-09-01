"""todo-107/158: ``mod`` declarations, real module trees, re-export gating.

Covers (todo-158 first, todo-107 semantics on top):

- todo-158: the module prefix tree is driven by mod.wind declaration lists.
  Only declared submodules are addressable; an undeclared sibling file is
  invisible; a dangling declaration fails to resolve; the root
  ``libs/mod.wind`` is the std root module *and* the prelude
  (``std::prelude`` is gone).
- todo-107: ``[pub [vis]] mod name;`` / ``mod name { ... }`` declarations;
  ``pub mod`` re-exports the submodule to importers, a private ``mod`` is
  addressable only from inside its declaring module's subtree;
  ``pub(self)/pub(super)/pub(crate)/pub(std)/pub(in path)`` visibility
  variants parse and gate.

Test fixtures write module files through ``harness.write_module``, which
keeps the mod.wind declaration chain in sync; private/undeclared scenarios
bypass it on purpose.
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

from cwind_frontend import run_sa_with_errors, tokenize_file  # noqa: E402
from cwind_frontend.parser.parser import parse_with_errors  # noqa: E402


class ModTreeScaffold(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, relative: str | Path, text: str) -> Path:
        """Plain write (NO mod.wind syncing) for negative scenarios."""
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def write_mod(self, relative: str | Path, text: str) -> Path:
        return harness.write_module(self.root, relative, text)

    def parse(self, entry: str | Path):
        path = self.root / entry
        if not path.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")
        return parse_with_errors(
            tokenize_file(path), source_path=str(path.resolve())
        )

    def assert_clean(self, parsed) -> None:
        self.assertEqual([], [e.message for e in parsed.errors])

    def assert_errors(self, parsed) -> list[str]:
        return [e.message for e in parsed.errors]

    def sa_errors(self, parsed) -> list[str]:
        result = run_sa_with_errors(parsed.program)
        return [e.message for e in result.errors]


class Todo158DeclarationDrivenTree(ModTreeScaffold):
    def test_declared_file_module_is_addressable(self):
        self.write_mod("libs/mod.wind", "pub mod geom;\n")
        self.write_mod("libs/geom.wind", "pub fn area() -> Int { return 6; }\n")
        parsed = self.parse("main.wind")
        self.assert_clean(parsed)
        self.assertEqual([], self.sa_errors(parsed))

    def test_undeclared_sibling_file_is_not_addressable(self):
        self.write_mod("libs/mod.wind", "pub mod geom;\n")
        self.write_mod("libs/geom.wind", "pub fn area() -> Int { return 6; }\n")
        self.write("libs/stray.wind", "pub fn stray() -> Int { return 1; }\n")
        self.write("main.wind", "use stray;\nfn main() -> Int { return 0; }\n")
        parsed = self.parse("main.wind")
        self.assertTrue(any(
            "cannot find module 'stray'" in m for m in self.assert_errors(parsed)
        ), self.assert_errors(parsed))

    def test_empty_root_mod_wind_exposes_nothing(self):
        self.write("libs/mod.wind", "")
        self.write("libs/real.wind", "pub fn real() -> Int { return 1; }\n")
        self.write("main.wind", "use real;\nfn main() -> Int { return 0; }\n")
        parsed = self.parse("main.wind")
        self.assertTrue(any(
            "cannot find module 'real'" in m for m in self.assert_errors(parsed)
        ), self.assert_errors(parsed))

    def test_dangling_declaration_fails_to_resolve(self):
        self.write("libs/mod.wind", "pub mod ghost;\n")
        self.write("main.wind", "use ghost;\nfn main() -> Int { return 0; }\n")
        parsed = self.parse("main.wind")
        self.assertTrue(any(
            "cannot find module 'ghost'" in m
            for m in self.assert_errors(parsed)
        ), self.assert_errors(parsed))

    def test_directory_module_chain_requires_declarations(self):
        self.write("libs/mod.wind", "pub mod baseimpl;\n")
        self.write("libs/baseimpl/mod.wind", "pub mod random;\n")
        self.write(
            "libs/baseimpl/random/mod.wind", "pub mod engine;\n"
        )
        self.write(
            "libs/baseimpl/random/engine.wind",
            "pub fn spin() -> Int { return 42; }\n",
        )
        parsed = self.parse("main.wind")
        self.assert_clean(parsed)
        self.assertEqual([], self.sa_errors(parsed))

    def test_undeclared_intermediate_directory_blocks_descent(self):
        # baseimpl declared, but its mod.wind does NOT declare random:
        # the deep file is unreachable even though it exists on disk.
        self.write("libs/mod.wind", "pub mod baseimpl;\n")
        self.write("libs/baseimpl/mod.wind", "pub mod other;\n")
        self.write(
            "libs/baseimpl/random/engine.wind",
            "pub fn spin() -> Int { return 42; }\n",
        )
        self.write(
            "main.wind",
            "use baseimpl::random;\nfn main() -> Int { return 0; }\n",
        )
        parsed = self.parse("main.wind")
        self.assertTrue(any(
            "cannot resolve import path" in m or "cannot find module" in m
            or "no item 'random'" in m
            for m in self.assert_errors(parsed)
        ), self.assert_errors(parsed))

    def test_std_prelude_path_is_gone(self):
        self.write("libs/mod.wind", "pub fn p() -> Int { return 1; }\n")
        self.write(
            "main.wind", "use std::prelude;\nfn main() -> Int { return 0; }\n"
        )
        parsed = self.parse("main.wind")
        self.assertTrue(any(
            "cannot find module 'std::prelude'" in m
            for m in self.assert_errors(parsed)
        ), self.assert_errors(parsed))

    def test_auto_prelude_is_the_root_module(self):
        self.write("libs/mod.wind", "pub fn rooted() -> Int { return 9; }\n")
        parsed = self.parse("main.wind")
        self.assert_clean(parsed)
        self.assertEqual([], self.sa_errors(parsed))

    def test_root_module_pub_use_reexports_flow(self):
        self.write(
            "libs/mod.wind",
            "pub mod option;\npub use option::Opt;\n",
        )
        self.write(
            "libs/option.wind",
            "pub enum Opt { None, Some(Int) }\n",
        )
        parsed = self.parse("main.wind")
        self.assert_clean(parsed)
        self.assertEqual([], self.sa_errors(parsed))


class Todo107ModDeclarations(ModTreeScaffold):
    def test_private_mod_blocks_entry_file(self):
        self.write("libs/mod.wind", "pub mod geom;\nmod internal;\n")
        self.write("libs/geom.wind", "pub fn a() -> Int { return 1; }\n")
        self.write("libs/internal.wind", "pub fn s() -> Int { return 2; }\n")
        self.write(
            "main.wind",
            "use internal;\nfn main() -> Int { return 0; }\n",
        )
        parsed = self.parse("main.wind")
        self.assertTrue(any(
            "module 'internal' is private" in m
            for m in self.assert_errors(parsed)
        ), self.assert_errors(parsed))

    def test_private_mod_visible_inside_declaring_subtree(self):
        self.write("libs/mod.wind", "pub mod geom;\nmod internal;\n")
        self.write("libs/geom.wind", "pub fn a() -> Int { return 1; }\n")
        self.write("libs/internal.wind", "pub fn s() -> Int { return 2; }\n")
        self.write_mod(
            "libs/geom.wind",
            "use super::internal;\n"
            "pub fn a() -> Int { return internal::s(); }\n",
        )
        parsed = self.parse("main.wind")
        self.assert_clean(parsed)
        self.assertEqual([], self.sa_errors(parsed))

    def test_private_mod_in_subdirectory_gated_to_that_subtree(self):
        # internal declared inside baseimpl/mod.wind: entry cannot reach it,
        # but baseimpl's own files can.
        self.write("libs/mod.wind", "pub mod baseimpl;\n")
        self.write("libs/baseimpl/mod.wind", "mod internal;\npub mod core;\n")
        self.write(
            "libs/baseimpl/internal.wind",
            "pub fn i() -> Int { return 3; }\n",
        )
        self.write(
            "libs/baseimpl/core.wind",
            "use super::internal;\n"
            "pub fn c() -> Int { return internal::i(); }\n",
        )
        parsed = self.parse("main.wind")
        self.assert_clean(parsed)
        self.assertEqual([], self.sa_errors(parsed))

        entry2 = self.write(
            "main2.wind",
            "use baseimpl::internal;\nfn main() -> Int { return 0; }\n",
        )
        parsed2 = self.parse("main2.wind")
        self.assertTrue(any(
            "module 'baseimpl::internal' is private" in m
            for m in self.assert_errors(parsed2)
        ), self.assert_errors(parsed2))
        del entry2

    def test_pub_mod_reexports_from_nested_directory(self):
        self.write("libs/mod.wind", "pub mod traits;\n")
        self.write("libs/traits/mod.wind", "pub mod display;\n")
        self.write(
            "libs/traits/display.wind",
            "pub trait Show { fn show(self) -> String; }\n",
        )
        parsed = self.parse("main.wind")
        self.assert_clean(parsed)
        self.assertEqual([], self.sa_errors(parsed))

    def test_inline_mod_body_flattens_with_extended_path(self):
        self.write("libs/mod.wind", "")
        self.write(
            "main.wind",
            "pub mod inner {\n"
            "    pub fn deep() -> Int { return 5; }\n"
            "}\n"
            "fn main() -> Int { return inner::deep(); }\n",
        )
        parsed = self.parse("main.wind")
        self.assert_clean(parsed)
        self.assertEqual([], self.sa_errors(parsed))


class Todo107VisibilityVariants(ModTreeScaffold):
    def test_pub_self_blocks_cross_file_use(self):
        self.write("libs/mod.wind", "pub mod selfy;\n")
        self.write(
            "libs/selfy.wind",
            "pub(self) fn only_here() -> Int { return 1; }\n",
        )
        self.write(
            "main.wind",
            "use selfy;\nfn main() -> Int { return selfy::only_here(); }\n",
        )
        parsed = self.parse("main.wind")
        self.assert_clean(parsed)
        self.assertTrue(any(
            "only_here" in m and ("private" in m or "not visible" in m)
            for m in self.sa_errors(parsed)
        ), self.sa_errors(parsed))

    def test_pub_crate_same_tree_stays_usable(self):
        self.write("libs/mod.wind", "pub mod holder;\n")
        self.write(
            "libs/holder.wind",
            "pub(crate) fn crate_fn() -> Int { return 4; }\n",
        )
        parsed = self.parse("main.wind")
        self.assert_clean(parsed)
        self.assertEqual([], self.sa_errors(parsed))

    def test_pub_std_semantics_preserved(self):
        self.write("libs/mod.wind", "pub mod holder;\n")
        self.write(
            "libs/holder.wind",
            "pub(std) fn std_fn() -> Int { return 4; }\n",
        )
        parsed = self.parse("main.wind")
        self.assert_clean(parsed)
        self.assertEqual([], self.sa_errors(parsed))

    def test_pub_super_visible_to_parent_scope(self):
        self.write("libs/mod.wind", "pub mod holder;\n")
        self.write(
            "libs/holder.wind",
            "pub(super) fn super_fn() -> Int { return 4; }\n",
        )
        parsed = self.parse("main.wind")
        self.assert_clean(parsed)
        self.assertEqual([], self.sa_errors(parsed))

    def test_pub_in_path_declares_restricted_scope(self):
        self.write("libs/mod.wind", "pub mod a;\npub mod b;\n")
        self.write(
            "libs/a.wind",
            "pub(in crate::b) fn for_b() -> Int { return 7; }\n",
        )
        self.write("libs/b.wind", "pub fn user() -> Int { return 8; }\n")
        parsed = self.parse("main.wind")
        self.assert_clean(parsed)
        self.assertEqual([], self.sa_errors(parsed))

    def test_pub_in_path_unknown_segment_still_parses(self):
        self.write("libs/mod.wind", "pub mod a;\n")
        self.write(
            "libs/a.wind",
            "pub(in nowhere::at::all) fn x() -> Int { return 7; }\n",
        )
        parsed = self.parse("main.wind")
        self.assert_clean(parsed)

    def test_unknown_pub_qualifier_reports_error(self):
        self.write("libs/mod.wind", "")
        self.write(
            "main.wind",
            "pub(loud) fn f() -> Int { return 1; }\n"
            "fn main() -> Int { return 0; }\n",
        )
        parsed = self.parse("main.wind")
        self.assertTrue(any(
            "unknown visibility qualifier" in m
            for m in self.assert_errors(parsed)
        ), self.assert_errors(parsed))

    def test_pub_variant_on_struct_and_const(self):
        self.write("libs/mod.wind", "pub mod holder;\n")
        self.write(
            "libs/holder.wind",
            "pub(crate) const LIMIT: Int = 9;\n"
            "pub(super) struct Boxed { pub v: Int }\n",
        )
        parsed = self.parse("main.wind")
        self.assert_clean(parsed)
        self.assertEqual([], self.sa_errors(parsed))


class Todo107EdgeCases(ModTreeScaffold):
    def test_file_and_dir_collision_is_ambiguous(self):
        self.write("libs/mod.wind", "pub mod dup;\n")
        self.write("libs/dup.wind", "pub fn f1() -> Int { return 1; }\n")
        self.write("libs/dup/mod.wind", "pub fn f2() -> Int { return 2; }\n")
        self.write("main.wind", "use dup;\nfn main() -> Int { return 0; }\n")
        parsed = self.parse("main.wind")
        self.assertTrue(
            any("ambiguous" in m for m in self.assert_errors(parsed)),
            self.assert_errors(parsed),
        )

    def test_mod_name_cannot_be_keyword_in(self):
        self.write("libs/mod.wind", "")
        self.write(
            "main.wind",
            "mod in;\nfn main() -> Int { return 0; }\n",
        )
        parsed = self.parse("main.wind")
        self.assertTrue(self.assert_errors(parsed))

    def test_for_in_still_works_alongside_in_keyword(self):
        self.write("libs/mod.wind", "")
        self.write(
            "main.wind",
            "fn main() -> Int {\n"
            "    let v: Vector<Int> = [1, 2, 3];\n"
            "    let mut total: Int = 0;\n"
            "    for x in v {\n"
            "        total += x;\n"
            "    }\n"
            "    return total;\n"
            "}\n",
        )
        parsed = self.parse("main.wind")
        self.assert_clean(parsed)
        self.assertEqual([], self.sa_errors(parsed))

    def test_nested_inline_mod_path_extension(self):
        self.write("libs/mod.wind", "")
        self.write(
            "main.wind",
            "mod outer {\n"
            "    mod inner {\n"
            "        pub fn deepest() -> Int { return 3; }\n"
            "    }\n"
            "    pub fn call() -> Int { return inner::deepest(); }\n"
            "}\n"
            "fn main() -> Int { return outer::call(); }\n",
        )
        parsed = self.parse("main.wind")
        self.assert_clean(parsed)
        self.assertEqual([], self.sa_errors(parsed))

    def test_use_inside_inline_mod(self):
        self.write("libs/mod.wind", "pub mod util;\n")
        self.write(
            "libs/util.wind",
            "pub fn triple(v: Int) -> Int { return v * 3; }\n",
        )
        self.write(
            "main.wind",
            "mod wrapper {\n"
            "    use std::util;\n"
            "    pub fn go() -> Int { return util::triple(4); }\n"
            "}\n"
            "fn main() -> Int { return wrapper::go(); }\n",
        )
        parsed = self.parse("main.wind")
        self.assert_clean(parsed)
        self.assertEqual([], self.sa_errors(parsed))


if __name__ == "__main__":
    unittest.main()
