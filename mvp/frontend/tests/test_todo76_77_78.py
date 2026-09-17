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

from cwind_frontend.home import reset_install_root_cache
from cwind_frontend import build_typed_ast, run_sa_with_errors  # noqa: E402
from cwind_frontend.parser.parser import parse_with_errors  # noqa: E402
from cwind_frontend import tokenize_file  # noqa: E402


class Todo76_77_78Tests(unittest.TestCase):
    def _write(self, root: Path, relative: str, text: str) -> Path:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        parts = Path(relative).parts
        if parts and parts[0] in ("libs", "src") and path.suffix in (".wind", ".wd"):
            harness.sync_mod_wind(root, path)
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

    def test_compilation_snapshot_fingerprints_once_per_pipeline(self):
        from unittest.mock import patch
        from cwind_frontend import parse_source, run_sa
        from cwind_frontend.parser import defs

        with patch.object(
            defs, "_library_fingerprint", wraps=defs._library_fingerprint
        ) as fingerprint:
            for _ in range(2):
                before = fingerprint.call_count
                run_sa(parse_source("fn f() -> Int { return 1; }"))
                self.assertEqual(1, fingerprint.call_count - before)

    def test_snapshot_anchored_children_and_sa_share_validation(self):
        from unittest.mock import patch
        from cwind_frontend.parser import defs

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write(root, "libs/b.wind", "pub fn b() -> Int { return 1; }")
            self._write(root, "libs/a.wind",
                        "use b; pub fn a() -> Int { return b::b(); }")
            main = self._write(root, "main.wind",
                               "use a; fn main() -> Int { return a::a(); }")
            with patch.object(defs, "_library_fingerprint",
                              wraps=defs._library_fingerprint) as fingerprint:
                for _ in range(2):
                    fingerprint.reset_mock()
                    parsed = self._parse_entry(main)
                    self._assert_clean(parsed)
                    self.assertIsNone(defs._ACTIVE_COMPILATION.get())
                    result = run_sa_with_errors(parsed.program)
                    self.assertEqual([], result.errors)
                    roots = [call.args[0] for call in fingerprint.call_args_list]
                    self.assertEqual(1, roots.count((root / "libs").resolve()))
                    self.assertEqual(len(roots), len(set(roots)))
                    self.assertIsNone(defs._ACTIVE_COMPILATION.get())
                    self.assertFalse(hasattr(parsed.program, "_compilation_snapshot"))

    def test_snapshot_next_parse_sees_added_deleted_module(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write(root, "libs/mod.wind", "pub mod added;")
            main = self._write(root, "main.wind", "use added::*;")
            self.assertTrue(self._parse_entry(main).errors)
            added = self._write(root, "libs/added.wind", "pub fn added() {}")
            self._assert_clean(self._parse_entry(main))
            added.unlink()
            self.assertTrue(self._parse_entry(main).errors)
            self._write(root, "libs/added.wind", "pub fn restored() {}")
            parsed = self._parse_entry(main)
            self._assert_clean(parsed)
            self.assertIn("restored", [getattr(i, "name", None)
                                       for i in parsed.program.items])

    def test_snapshot_manifest_entry_change_without_source_change(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write(root, "libs/mod.wind", "")
            self._write(root, "src/one.wd", "pub mod alpha;")
            self._write(root, "src/two.wd", "pub mod beta;")
            self._write(root, "src/alpha.wd", "pub fn alpha() {}")
            self._write(root, "src/beta.wd", "pub fn beta() {}")
            manifest = self._write(root, "Breeze.toml",
                '[package]\nname = "snapshot"\nversion = "0.1.0"\n'
                '[entry]\nsource = "src"\nmodule = "one.wd"\n')
            main = self._write(root, "main.wind", "use alpha::*;")
            self._assert_clean(self._parse_entry(main))
            manifest.write_text(manifest.read_text().replace("one.wd", "two.wd"),
                                encoding="utf-8")
            self.assertTrue(self._parse_entry(main).errors)
            main.write_text("use beta::*;", encoding="utf-8")
            self._assert_clean(self._parse_entry(main))

    def test_snapshot_exception_cleanup(self):
        from unittest.mock import patch
        from cwind_frontend import parse_source, run_sa, SaError
        from cwind_frontend.parser import defs

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write(root, "libs/mod.wind", "pub mod ambiguous;")
            self._write(root, "libs/ambiguous.wind", "")
            duplicate = self._write(root, "libs/ambiguous/mod.wind", "")
            main = self._write(root, "main.wind", "use ambiguous;")
            failed = self._parse_entry(main)
            self.assertTrue(any("ambiguous module" in e.message for e in failed.errors))
            self.assertFalse(hasattr(failed.program, "_compilation_snapshot"))
            self.assertIsNone(defs._ACTIVE_COMPILATION.get())
            duplicate.unlink()
            with patch.object(defs, "_library_fingerprint",
                              side_effect=RuntimeError("snapshot failure")):
                with self.assertRaisesRegex(RuntimeError, "snapshot failure"):
                    self._parse_entry(main)
            self.assertIsNone(defs._ACTIVE_COMPILATION.get())
            self._assert_clean(self._parse_entry(main))
        with self.assertRaises(SaError):
            run_sa(parse_source("fn f() -> Int { return unknown; }"))
        self.assertIsNone(defs._ACTIVE_COMPILATION.get())
        with patch.object(defs, "_library_fingerprint",
                          wraps=defs._library_fingerprint) as fingerprint:
            run_sa(parse_source("fn f() -> Int { return 1; }"))
            self.assertEqual(1, fingerprint.call_count)

    def test_bootstrap_cache_is_pristine_and_validates_config_file_and_root(self):
        from unittest.mock import patch
        from cwind_frontend import tokenize
        from cwind_frontend.sa import analyzer

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name in ("first", "second"):
                self._write(root / name, "libs/mod.wind", "pub mod builtins;")
                self._write(root / name, "libs/builtins/mod.wind",
                    '#[cfg(target_os = "windows")] pub typedef Pick = Int;\n'
                    '#[cfg(target_os = "linux")] pub typedef Pick = String;\n')
            with patch.object(analyzer, "_parse_bootstrap_file_uncached",
                              wraps=analyzer._parse_bootstrap_file_uncached) as parse:
                def compile(home, target):
                    with patch.dict(os.environ, {"CWIND_HOME": str(home)}):
                        parsed = parse_with_errors(
                            tokenize("fn f() -> Pick { return 1; }"), target_os=target
                        )
                        self._assert_clean(parsed)
                        return run_sa_with_errors(parsed.program)

                first = root / "first"
                self.assertEqual([], compile(first, "windows").errors)
                before = parse.call_count
                self.assertEqual([], compile(first, "windows").errors)
                self.assertEqual(before, parse.call_count)
                self.assertTrue(compile(first, "linux").errors)
                self.assertGreater(parse.call_count, before)
                before = parse.call_count
                path = first / "libs/builtins/mod.wind"
                with patch.dict(os.environ, {"CWIND_HOME": str(first)}):
                    items = analyzer._parse_bootstrap_file(path)
                    items[0].name = "POISON"
                    self.assertEqual("Pick", analyzer._parse_bootstrap_file(path)[0].name)
                path.write_text("pub typedef Changed = Int;", encoding="utf-8")
                compile(first, "windows")
                self.assertGreater(parse.call_count, before)
                before = parse.call_count
                compile(root / "second", "windows")
                self.assertGreater(parse.call_count, before)

    def test_bootstrap_cache_validates_all_target_keys(self):
        from unittest.mock import patch
        from cwind_frontend import tokenize

        cases = (
            ("target_os", "windows", "linux"),
            ("target_arch", "x86_64", "aarch64"),
            ("target_vendor", "pc", "apple"),
            ("target_pointer_width", "64", "32"),
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write(root, "libs/mod.wind", "pub mod builtins;")
            builtins = self._write(root, "libs/builtins/mod.wind", "")
            with patch.dict(os.environ, {"CWIND_HOME": str(root)}):
                for key, first, second in cases:
                    with self.subTest(key=key):
                        builtins.write_text(
                            f'#[cfg({key} = "{first}")] pub typedef Pick = Int;\n'
                            f'#[cfg({key} = "{second}")] pub typedef Pick = String;\n',
                            encoding="utf-8",
                        )
                        for target, clean in ((first, True), (second, False), (first, True)):
                            parsed = parse_with_errors(
                                tokenize("fn f() -> Pick { return 1; }"),
                                target_os=target if key == "target_os" else None,
                                target_arch=target if key == "target_arch" else None,
                                target_vendor=target if key == "target_vendor" else None,
                                target_pointer_width=(
                                    target if key == "target_pointer_width" else None
                                ),
                            )
                            self._assert_clean(parsed)
                            result = run_sa_with_errors(parsed.program)
                            self.assertEqual(clean, not result.errors)

    def test_bootstrap_cache_validates_changed_and_deleted_dependency(self):
        from unittest.mock import patch
        from cwind_frontend import parse_source

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write(root, "libs/mod.wind", "pub mod builtins; pub mod values;")
            self._write(root, "libs/builtins/mod.wind", "use std::values::*;")
            values = self._write(root, "libs/values.wind", "pub typedef Pick = Int;")
            with patch.dict(os.environ, {"CWIND_HOME": str(root)}):
                def compile():
                    return run_sa_with_errors(parse_source("fn f() -> Pick { return 1; }"))

                self.assertEqual([], compile().errors)
                values.write_text("pub typedef Pick = String;", encoding="utf-8")
                self.assertTrue(compile().errors)
                values.unlink()
                self.assertTrue(compile().errors)
                values.write_text("pub typedef Pick = Int;", encoding="utf-8")
                self.assertEqual([], compile().errors)

    def test_snapshot_sa_exception_consumes_handoff(self):
        from unittest.mock import patch
        from cwind_frontend import parse_source, run_sa
        from cwind_frontend.parser import defs
        from cwind_frontend.sa import analyzer

        program = parse_source("fn f() -> Int { return 1; }")
        with patch.object(analyzer, "_parse_bootstrap_file",
                          side_effect=RuntimeError("bootstrap failure")):
            with self.assertRaisesRegex(RuntimeError, "bootstrap failure"):
                run_sa(program)
        self.assertIsNone(defs._ACTIVE_COMPILATION.get())
        self.assertFalse(hasattr(program, "_compilation_snapshot"))
        with patch.object(defs, "_library_fingerprint",
                          wraps=defs._library_fingerprint) as fingerprint:
            run_sa(parse_source("fn f() -> Int { return 1; }"))
            self.assertEqual(1, fingerprint.call_count)

    def test_snapshot_parse_handoffs_are_independent(self):
        from cwind_frontend import parse_source, run_sa
        from cwind_frontend.parser import defs

        first = parse_source("fn first() -> Int { return 1; }")
        second = parse_source("fn second() -> Int { return 2; }")
        self.assertIsNot(first._compilation_snapshot, second._compilation_snapshot)
        self.assertIsNone(defs._ACTIVE_COMPILATION.get())
        run_sa(second)
        run_sa(first)
        self.assertIsNone(defs._ACTIVE_COMPILATION.get())

    def test_fingerprint_reuses_stat_for_file_kind(self):
        from unittest.mock import patch
        from cwind_frontend.parser import defs

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write(root, "child/file.wind", "")
            original = Path.is_dir
            checked = []

            def is_dir(path):
                checked.append(path)
                return original(path)

            with patch.object(Path, "is_dir", is_dir):
                self.assertTrue(defs._library_fingerprint(root))
            # pathlib may test the traversal root, but fingerprinting must
            # not issue a second stat to classify each acquired child stat.
            self.assertNotIn(root / "child/file.wind", checked)
            self.assertNotIn(root / "child", checked)

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
                "    baseprint(o.unwrap().to_string());\n"
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
                parsed.program, result.info
            )
            # todo-158: the auto std import (root module) rides along.
            explicit = [
                entry for entry in doc["imports"] if not entry.get("auto")
            ]
            self.assertEqual(2, len(explicit))
            by_path = {
                tuple(entry["path"]): entry["source"]
                for entry in explicit
            }
            self.assertEqual(
                str(mathx.resolve()), by_path[("mathx",)]
            )
            self.assertEqual(str(thing.resolve()), by_path[("thing",)])

    # -- todo-76: implicit prelude ----------------------------------------

    def test_no_prelude_module_leaves_program_untouched(self):
        # No std reachable (CWIND_HOME points at a libless root) + a
        # project without its own libs/: no prelude layer at all.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            main = self._write(
                root, "main.wind", "fn main() -> Int { return 0; }\n"
            )
            empty_home = root / "empty-home"
            empty_home.mkdir()
            old_home = os.environ.get("CWIND_HOME")
            os.environ["CWIND_HOME"] = str(empty_home)
            reset_install_root_cache()
            try:
                parsed = self._parse_entry(main)
                self._assert_clean(parsed)
                kinds = [
                    type(item).__name__ for item in parsed.program.items
                ]
                self.assertNotIn("UseDecl", kinds)
            finally:
                if old_home is None:
                    os.environ.pop("CWIND_HOME", None)
                else:
                    os.environ["CWIND_HOME"] = old_home
                reset_install_root_cache()

    def test_auto_prelude_exposes_public_api_without_use(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # todo-158: the std root module (libs/mod.wind) is the prelude.
            self._write(
                root, "libs/mod.wind", "pub fn hello() -> Int { return 7; }\n"
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
            self.assertEqual(["std"], autos[0]["path"])

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
                project, "libs/mod.wind",
                "pub fn anchored() -> Int { return 9; }\n",
            )
            entry = self._write(
                project,
                "src/deep/main.wind",
                "fn main() -> Int { return anchored(); }\n",
            )
            # The temp directory has no ancestor libs of its own, so the
            # only resolvable prelude is <project>/libs/mod.wind even
            # though pytest's CWD lives somewhere else entirely.
            parsed = self._parse_entry(entry)
            self._assert_clean(parsed)
            result = run_sa_with_errors(parsed.program)
            self.assertEqual([], [e.message for e in result.errors])
            doc = build_typed_ast(parsed.program, result.info)
            sources = [i["source"] for i in doc["imports"]]
            self.assertEqual(
                [str((project / "libs" / "mod.wind").resolve())],
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
