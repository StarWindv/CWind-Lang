"""todo-76/77/78: implicit prelude, wildcard imports, and the module trie.

Covers:
- todo-76: ``std::prelude::*`` is imported implicitly for entry files, is
  anchored at the project root discovered from the entry path (not the
  process CWD), and locally declared names shadow prelude items.
- todo-77: ``use`` accepts a terminal ``*`` wildcard; bare ``use *;`` and
  mid-path stars are rejected.
- todo-78: module lookup goes through the ``libs/`` prefix tree with a
  fingerprint cache; ``.wind`` and ``.wd`` suffixes, subdirectory modules,
  per-import manifest sources, and cache invalidation on file changes.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path


TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent.parent.parent.parent
for path in (ROOT / "mvp/frontend/src", ROOT / "mvp/frontend/tests"):
    sys.path.insert(0, str(path))

import harness  # noqa: E402,F401  (sys.path side effect)

from cwind_frontend import build_typed_ast, run_sa_with_errors  # noqa: E402
from cwind_frontend.parser.parser import parse_with_errors  # noqa: E402
from cwind_frontend import tokenize_file  # noqa: E402


class Todo76_77_78Tests(unittest.TestCase):
    def _write(self, root: Path, relative: str, text: str) -> Path:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def _parse_entry(self, entry: Path):
        """Parse an entry file exactly like the CLI does."""
        return parse_with_errors(
            tokenize_file(entry), source_path=str(entry.resolve())
        )

    def _assert_clean(self, parsed) -> None:
        self.assertEqual(
            [], [error.message for error in parsed.errors]
        )

    # -- todo-77: wildcard imports ---------------------------------------

    def test_wildcard_import_exposes_public_functions(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write(
                root,
                "libs/mathx.wind",
                "pub fn add(a: Int, b: Int) -> Int { return a + b; }\n"
                "fn hidden() -> Int { return 100; }\n",
            )
            main = self._write(
                root,
                "main.wind",
                "use mathx::*;\n"
                "fn main() -> Int { return add(2, 3); }\n",
            )
            parsed = self._parse_entry(main)
            self._assert_clean(parsed)
            result = run_sa_with_errors(parsed.program)
            self.assertEqual([], [e.message for e in result.errors])
            names = {s.name for s in result.info.symbols.values()}
            self.assertIn("add", names)

    def test_wildcard_does_not_leak_unreferenced_privates(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write(
                root,
                "libs/thing.wind",
                "pub fn pubf() -> Int { return 1; }\n"
                "fn privf() -> Int { return 2; }\n",
            )
            main = self._write(
                root,
                "main.wind",
                "use thing::*;\n"
                "fn main() -> Int { return privf(); }\n",
            )
            parsed = self._parse_entry(main)
            self._assert_clean(parsed)
            result = run_sa_with_errors(parsed.program)
            self.assertTrue(any(
                "privf" in e.message
                and ("unknown" in e.message or "no function" in e.message)
                for e in result.errors
            ))

    def test_bare_star_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            main = Path(td) / "main.wind"
            main.write_text("use *;\n", encoding="utf-8")
            parsed = parse_with_errors(tokenize_file(main))
            self.assertTrue(any(
                "*" in e.message or "wildcard" in e.message
                for e in parsed.errors
            ))

    def test_explicit_private_item_access_reports_private(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write(
                root, "libs/secret.wind", "fn hidden() -> Int { return 1; }\n"
            )
            main = self._write(
                root,
                "main.wind",
                "use secret;\n"
                "fn main() -> Int { return secret::hidden(); }\n",
            )
            parsed = self._parse_entry(main)
            self._assert_clean(parsed)
            result = run_sa_with_errors(parsed.program)
            self.assertTrue(any(
                "private" in e.message for e in result.errors
            ))

    def test_unknown_module_member_reports_missing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write(
                root,
                "libs/mathx.wind",
                "pub fn add(a: Int, b: Int) -> Int { return a + b; }\n",
            )
            main = self._write(
                root,
                "main.wind",
                "use mathx;\n"
                "fn main() -> Int { return mathx::nope(); }\n",
            )
            parsed = self._parse_entry(main)
            self._assert_clean(parsed)
            result = run_sa_with_errors(parsed.program)
            self.assertTrue(any(
                "has no function 'nope'" in e.message for e in result.errors
            ))

    # -- todo-77: explicit item imports ----------------------------------

    def test_explicit_function_item_import(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write(
                root,
                "libs/util.wind",
                "pub fn make(a: Int) -> Int { return a * 2; }\n"
                "pub fn drop(a: Int) -> Int { return a / 2; }\n",
            )
            main = self._write(
                root,
                "main.wind",
                "use util::make;\n"
                "fn main() -> Int { return make(21); }\n",
            )
            parsed = self._parse_entry(main)
            self._assert_clean(parsed)
            result = run_sa_with_errors(parsed.program)
            self.assertEqual([], [e.message for e in result.errors])
            names = {s.name for s in result.info.symbols.values()}
            self.assertIn("make", names)

    def test_explicit_enum_import_brings_private_method_helper(self):
        """The Option scenario: an explicit type import must carry the
        private top-level helpers its method blocks reference."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write(
                root,
                "libs/optmod.wind",
                "pub enum Opt<T> { None, Some(T) }\n"
                "fn fail() -> ! { exit(1); }\n"
                "extra<T> Opt<T> {\n"
                "    pub fn unwrap(self) -> T {\n"
                "        return match (self) {\n"
                "            Opt::Some(val) => val,\n"
                "            _ => fail(),\n"
                "        };\n"
                "    }\n"
                "}\n",
            )
            main = self._write(
                root,
                "main.wind",
                "use optmod::Opt;\n"
                "fn main() -> Int {\n"
                "    let o: Opt<Int> = Opt::Some(3);\n"
                "    print(o.unwrap());\n"
                "    return 0;\n"
                "}\n",
            )
            parsed = self._parse_entry(main)
            self._assert_clean(parsed)
            result = run_sa_with_errors(parsed.program)
            self.assertEqual([], [e.message for e in result.errors])

    def test_explicit_import_of_private_item_reports_private(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write(root, "libs/s.wind", "fn hush() -> Int { return 0; }\n")
            main = self._write(
                root, "main.wind", "use s::hush;\nfn main() -> Int { return 0; }\n"
            )
            parsed = self._parse_entry(main)
            self.assertTrue(any(
                "private" in e.message for e in parsed.errors
            ))

    # -- todo-78: trie resolution ----------------------------------------

    def test_wind_and_wd_suffixes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write(
                root, "libs/alpha.wind", "pub fn fa() -> Int { return 1; }\n"
            )
            self._write(
                root, "libs/beta.wd", "pub fn fb() -> Int { return 2; }\n"
            )
            main = self._write(
                root,
                "main.wind",
                "use alpha;\n"
                "use beta;\n"
                "fn main() -> Int { return alpha::fa() + beta::fb(); }\n",
            )
            parsed = self._parse_entry(main)
            self._assert_clean(parsed)
            result = run_sa_with_errors(parsed.program)
            self.assertEqual([], [e.message for e in result.errors])

    def test_subdirectory_module_and_item(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write(
                root,
                "libs/deep/util.wind",
                "pub fn shared() -> Int { return 5; }\n"
                "pub fn helper() -> Int { return 6; }\n",
            )
            main = self._write(
                root,
                "main.wind",
                "use deep::util;\n"
                "use deep::util::helper;\n"
                "fn main() -> Int { return util::shared() + helper(); }\n",
            )
            parsed = self._parse_entry(main)
            self._assert_clean(parsed)
            result = run_sa_with_errors(parsed.program)
            self.assertEqual([], [e.message for e in result.errors])

    def test_trie_cache_rebuilds_after_file_change(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lib = self._write(
                root, "libs/gen.wind", "pub fn g1() -> Int { return 1; }\n"
            )
            main = self._write(
                root,
                "main.wind",
                "use gen::*;\n"
                "fn main() -> Int { return g1(); }\n",
            )
            parsed = self._parse_entry(main)
            self._assert_clean(parsed)
            first = run_sa_with_errors(parsed.program)
            self.assertEqual([], [e.message for e in first.errors])

            lib.write_text(
                "pub fn g1() -> Int { return 1; }\n"
                "pub fn g2() -> Int { return 2; }\n",
                encoding="utf-8",
            )
            stamp = lib.stat().st_mtime_ns + 1_000_000
            os.utime(lib, ns=(stamp, stamp))
            parsed = self._parse_entry(main)
            self._assert_clean(parsed)
            second = run_sa_with_errors(parsed.program)
            self.assertEqual([], [e.message for e in second.errors])
            names = {s.name for s in second.info.symbols.values()}
            self.assertIn("g2", names)

    def test_recursive_cycle_still_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write(root, "libs/a.wind", "use b;\n")
            self._write(root, "libs/b.wind", "use a;\n")
            main = self._write(root, "main.wind", "use a;\n")
            parsed = self._parse_entry(main)
            self.assertTrue(any(
                "recursive module import" in e.message
                for e in parsed.errors
            ))

    def test_manifest_records_source_per_import(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            mathx = self._write(
                root,
                "libs/mathx.wind",
                "pub fn add(a: Int, b: Int) -> Int { return a + b; }\n",
            )
            thing = self._write(
                root, "libs/thing.wind", "pub fn t() -> Int { return 0; }\n"
            )
            main = self._write(
                root,
                "main.wind",
                "use mathx;\n"
                "use thing::*;\n"
                "fn main() -> Int { return mathx::add(1, 2); }\n",
            )
            parsed = self._parse_entry(main)
            self._assert_clean(parsed)
            result = run_sa_with_errors(parsed.program)
            self.assertEqual([], [e.message for e in result.errors])
            doc = build_typed_ast(
                parsed.program, result.info, source=str(main.resolve())
            )
            self.assertEqual(2, len(doc["imports"]))
            by_path = {
                tuple(entry["path"]): entry["source"]
                for entry in doc["imports"]
            }
            self.assertEqual(
                str(mathx.resolve()), by_path[("mathx",)]
            )
            self.assertEqual(str(thing.resolve()), by_path[("thing",)])

    # -- todo-76: implicit prelude ----------------------------------------

    def test_no_prelude_module_leaves_program_untouched(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            main = self._write(
                root, "main.wind", "fn main() -> Int { return 0; }\n"
            )
            parsed = self._parse_entry(main)
            self._assert_clean(parsed)
            kinds = [
                type(item).__name__ for item in parsed.program.items
            ]
            self.assertNotIn("UseDecl", kinds)

    def test_auto_prelude_exposes_public_api_without_use(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write(
                root, "libs/prelude.wind", "pub fn hello() -> Int { return 7; }\n"
            )
            main = self._write(
                root,
                "main.wind",
                "fn main() -> Int { return hello(); }\n",
            )
            parsed = self._parse_entry(main)
            self._assert_clean(parsed)
            result = run_sa_with_errors(parsed.program)
            self.assertEqual([], [e.message for e in result.errors])
            names = {s.name for s in result.info.symbols.values()}
            self.assertIn("hello", names)
            doc = build_typed_ast(parsed.program, result.info)
            autos = [i for i in doc["imports"] if i.get("auto")]
            self.assertEqual(1, len(autos))
            self.assertEqual(["std", "prelude"], autos[0]["path"])

    def test_local_definitions_shadow_prelude_items(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write(
                root,
                "libs/prelude.wind",
                "pub fn pick() -> Int { return 1; }\n",
            )
            main = self._write(
                root,
                "main.wind",
                "fn pick() -> Int { return 42; }\n"
                "fn main() -> Int { return pick(); }\n",
            )
            parsed = self._parse_entry(main)
            self._assert_clean(parsed)
            result = run_sa_with_errors(parsed.program)
            self.assertEqual([], [e.message for e in result.errors])
            picks = [
                item for item in parsed.program.items
                if type(item).__name__ == "FnDecl"
                and getattr(item, "name", None) == "pick"
            ]
            self.assertEqual(1, len(picks))

    def test_prelude_resolved_from_entry_path_not_cwd(self):
        """Entry anywhere under the project still finds that project's libs,
        regardless of the process working directory."""
        with tempfile.TemporaryDirectory() as td:
            outer = Path(td).resolve()
            project = outer / "proj"
            self._write(
                project, "libs/prelude.wind",
                "pub fn anchored() -> Int { return 9; }\n",
            )
            entry = self._write(
                project,
                "src/deep/main.wind",
                "fn main() -> Int { return anchored(); }\n",
            )
            # The temp directory has no ancestor libs of its own, so the
            # only resolvable prelude is <project>/libs/prelude.wind even
            # though pytest's CWD lives somewhere else entirely.
            parsed = self._parse_entry(entry)
            self._assert_clean(parsed)
            result = run_sa_with_errors(parsed.program)
            self.assertEqual([], [e.message for e in result.errors])
            doc = build_typed_ast(parsed.program, result.info)
            sources = [i["source"] for i in doc["imports"]]
            self.assertEqual(
                [str((project / "libs" / "prelude.wind").resolve())],
                sources,
            )

    def test_std_virtual_namespace_maps_to_libs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write(
                root, "libs/core.wind", "pub fn cfn() -> Int { return 3; }\n"
            )
            main = self._write(
                root,
                "main.wind",
                "use std::core;\n"
                "fn main() -> Int { return core::cfn(); }\n",
            )
            parsed = self._parse_entry(main)
            self._assert_clean(parsed)
            result = run_sa_with_errors(parsed.program)
            self.assertEqual([], [e.message for e in result.errors])


if __name__ == "__main__":
    unittest.main()
