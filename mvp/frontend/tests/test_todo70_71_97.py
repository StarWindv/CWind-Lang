"""todo-70/71/97: directory modules, Breeze.toml manifests, project builds.

Covers:
- todo-70: ``libs/foo/mod.wind`` resolves ``use foo;`` (old-Rust
  ``dir + mod.rs`` layout); sibling files stay addressable as submodules;
  a file and a ``mod`` directory claiming the same name are ambiguous.
- todo-71: ``Breeze.toml`` discovery (walking upward) and validation
  ([package] name/version/entry, dependencies table, bad TOML).
- todo-97: ``cwindf --project`` compiles the package entry into
  ``target/<name>.typed.json`` plus a ``project.json`` build index.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path


TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent.parent.parent.parent
for path in (ROOT / "mvp/frontend/src", ROOT / "mvp/frontend/tests"):
    sys.path.insert(0, str(path))

import harness  # noqa: E402,F401  (sys.path side effect)

from cwind_frontend import build_typed_ast, run_sa_with_errors  # noqa: E402
from cwind_frontend.breeze import (  # noqa: E402
    DEFAULT_ENTRY,
    MANIFEST_NAME,
    ManifestError,
    find_manifest,
    load_manifest,
)
from cwind_frontend.cli import main  # noqa: E402
from cwind_frontend.parser.parser import parse_with_errors  # noqa: E402
from cwind_frontend import tokenize_file  # noqa: E402


class ProjectScaffold(unittest.TestCase):
    """Shared temp-project helpers."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, relative, text: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def manifest(
        self,
        name: str = "demo",
        version: str = "0.1.0",
        entry: str | None = None,
        extra_package: str = "",
        dependencies: str = "",
    ) -> Path:
        lines = ["[package]", f'name = "{name}"', f'version = "{version}"']
        if entry is not None:
            lines.append(f'entry = "{entry}"')
        if extra_package:
            lines.append(extra_package)
        if dependencies:
            lines.append("")
            lines.append("[dependencies]")
            lines.append(dependencies)
        return self.write(MANIFEST_NAME, "\n".join(lines) + "\n")

    def parse_entry(self, entry: Path):
        parsed = parse_with_errors(
            tokenize_file(entry), source_path=str(entry.resolve())
        )
        self.assertEqual([], [e.message for e in parsed.errors])
        return parsed


class Todo70DirectoryModules(ProjectScaffold):
    def test_mod_wind_resolves_as_directory_module(self):
        self.write(
            self.root / "libs" / "geom" / "mod.wind",
            "pub fn area(w: Int, h: Int) -> Int { return w * h; }\n",
        )
        main = self.write(
            "main.wind",
            "use geom;\n"
            "fn main() -> Int { return geom::area(3, 4); }\n",
        )
        parsed = self.parse_entry(main)
        result = run_sa_with_errors(parsed.program)
        self.assertEqual([], [e.message for e in result.errors])

    def test_mod_wind_wildcard_and_item_import(self):
        self.write(
            self.root / "libs" / "geom" / "mod.wind",
            "pub fn area(w: Int, h: Int) -> Int { return w * h; }\n"
            "fn secret() -> Int { return 1; }\n",
        )
        wildcard_main = self.write(
            "w.wind",
            "use geom::*;\nfn main() -> Int { return area(2, 5); }\n",
        )
        parsed = self.parse_entry(wildcard_main)
        result = run_sa_with_errors(parsed.program)
        self.assertEqual([], [e.message for e in result.errors])
        self.assertIn("area", {s.name for s in result.info.symbols.values()})

        item_main = self.write(
            "i.wind",
            "use geom::area;\nfn main() -> Int { return area(2, 5); }\n",
        )
        parsed = self.parse_entry(item_main)
        result = run_sa_with_errors(parsed.program)
        self.assertEqual([], [e.message for e in result.errors])

    def test_directory_sibling_files_are_submodules(self):
        self.write(
            self.root / "libs" / "geom" / "mod.wind",
            "pub use shapes::square;\n",
        )
        self.write(
            self.root / "libs" / "geom" / "shapes.wind",
            "pub fn square(s: Int) -> Int { return s * s; }\n",
        )
        main = self.write(
            "main.wind",
            "use geom::shapes;\n"
            "fn main() -> Int { return shapes::square(6); }\n",
        )
        parsed = self.parse_entry(main)
        result = run_sa_with_errors(parsed.program)
        self.assertEqual([], [e.message for e in result.errors])
        doc = build_typed_ast(parsed.program, result.info)
        by_path = {
            tuple(i["path"]): i["source"] for i in doc["imports"]
        }
        self.assertIn(("geom", "shapes"), by_path)

    def test_nested_directory_module(self):
        self.write(
            self.root / "libs" / "a" / "b" / "mod.wind",
            "pub fn deep() -> Int { return 9; }\n",
        )
        main = self.write(
            "main.wind",
            "use a::b::*;\nfn main() -> Int { return deep(); }\n",
        )
        parsed = self.parse_entry(main)
        result = run_sa_with_errors(parsed.program)
        self.assertEqual([], [e.message for e in result.errors])

    def test_mod_wd_suffix_supported(self):
        self.write(
            self.root / "libs" / "wm" / "mod.wd",
            "pub fn wd_fn() -> Int { return 8; }\n",
        )
        main = self.write(
            "main.wind",
            "use wm;\nfn main() -> Int { return wm::wd_fn(); }\n",
        )
        parsed = self.parse_entry(main)
        result = run_sa_with_errors(parsed.program)
        self.assertEqual([], [e.message for e in result.errors])

    def test_file_and_mod_dir_collide_ambiguously(self):
        self.write(self.root / "libs" / "dup.wind", "pub fn f1() -> Int { return 1; }\n")
        self.write(self.root / "libs" / "dup" / "mod.wind", "pub fn f2() -> Int { return 2; }\n")
        main = self.write("main.wind", "use dup;\nfn main() -> Int { return 0; }\n")
        with self.assertRaises(Exception) as caught:
            self.parse_entry(main)
        self.assertIn("ambiguous", str(caught.exception))

    def test_bare_root_mod_wind_registers_nothing(self):
        self.write(
            self.root / "libs" / "mod.wind",
            "pub fn rootless() -> Int { return 1; }\n",
        )
        self.write(
            self.root / "libs" / "real.wind",
            "pub fn real() -> Int { return 2; }\n",
        )
        main = self.write(
            "main.wind",
            "use real;\nfn main() -> Int { return real::real(); }\n",
        )
        parsed = self.parse_entry(main)
        result = run_sa_with_errors(parsed.program)
        self.assertEqual([], [e.message for e in result.errors])


class Todo71BreezeManifest(ProjectScaffold):
    def test_find_manifest_walks_up_from_nested_dir(self):
        manifest = self.manifest()
        nested = self.root / "src" / "deep"
        nested.mkdir(parents=True)
        found = find_manifest(nested)
        self.assertEqual(manifest.resolve(), found.resolve())

    def test_find_manifest_accepts_the_manifest_itself(self):
        manifest = self.manifest()
        self.assertEqual(manifest.resolve(), find_manifest(manifest).resolve())

    def test_find_manifest_returns_none_when_absent(self):
        empty = self.root / "empty"
        empty.mkdir()
        self.assertIsNone(find_manifest(empty))

    def test_load_manifest_defaults(self):
        self.manifest()
        loaded = load_manifest(self.root / MANIFEST_NAME)
        self.assertEqual("demo", loaded.name)
        self.assertEqual("0.1.0", loaded.version)
        self.assertEqual(DEFAULT_ENTRY, loaded.entry)
        self.assertEqual(self.root.resolve(), loaded.root.resolve())
        self.assertEqual(
            (self.root / "src" / "main.wind").resolve(),
            loaded.entry_path().resolve(),
        )

    def test_load_manifest_full_fields(self):
        self.write(
            MANIFEST_NAME,
            "[package]\n"
            'name = "full_pkg"\n'
            'version = "1.2.3-beta.1"\n'
            'entry = "app/main.wind"\n'
            'description = "example"\n'
            'authors = ["A", "B"]\n'
            "\n[dependencies]\n"
            'mathx = "1.0"\n',
        )
        loaded = load_manifest(self.root / MANIFEST_NAME)
        self.assertEqual("full_pkg", loaded.name)
        self.assertEqual("1.2.3-beta.1", loaded.version)
        self.assertEqual("app/main.wind", loaded.entry)
        self.assertEqual(("A", "B"), loaded.authors)
        self.assertEqual("example", loaded.description)
        self.assertEqual({"mathx": "1.0"}, loaded.dependencies)

    def test_missing_package_section_rejected(self):
        self.write(MANIFEST_NAME, 'name = "orphan"\n')
        with self.assertRaises(ManifestError) as caught:
            load_manifest(self.root / MANIFEST_NAME)
        self.assertIn("[package]", str(caught.exception))

    def test_missing_or_invalid_name_rejected(self):
        self.write(MANIFEST_NAME, '[package]\nversion = "1.0.0"\n')
        with self.assertRaises(ManifestError) as caught:
            load_manifest(self.root / MANIFEST_NAME)
        self.assertIn("name", str(caught.exception))

        self.write(MANIFEST_NAME, '[package]\nname = "1bad"\nversion = "1.0.0"\n')
        with self.assertRaises(ManifestError):
            load_manifest(self.root / MANIFEST_NAME)

    def test_invalid_version_rejected(self):
        self.write(MANIFEST_NAME, '[package]\nname = "ok"\nversion = "one"\n')
        with self.assertRaises(ManifestError) as caught:
            load_manifest(self.root / MANIFEST_NAME)
        self.assertIn("version", str(caught.exception))

    def test_invalid_toml_rejected(self):
        self.write(MANIFEST_NAME, "[package\nname = = broken")
        with self.assertRaises(ManifestError) as caught:
            load_manifest(self.root / MANIFEST_NAME)
        self.assertIn("TOML", str(caught.exception))

    def test_absolute_entry_rejected(self):
        self.write(
            MANIFEST_NAME,
            '[package]\nname = "ok"\nversion = "1.0.0"\nentry = "/abs/x.wind"\n',
        )
        with self.assertRaises(ManifestError) as caught:
            load_manifest(self.root / MANIFEST_NAME)
        self.assertIn("relative", str(caught.exception))

    def test_authors_type_checked(self):
        self.write(
            MANIFEST_NAME,
            "[package]\nname = \"ok\"\nversion = \"1.0.0\"\nauthors = [1]\n",
        )
        with self.assertRaises(ManifestError) as caught:
            load_manifest(self.root / MANIFEST_NAME)
        self.assertIn("authors", str(caught.exception))


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


class Todo97ProjectBuild(ProjectScaffold):
    def _scaffold_project(self) -> Path:
        self.manifest(name="demoapp", version="0.2.0")
        self.write(
            Path("libs") / "geom" / "mod.wind",
            "pub fn twice(x: Int) -> Int { return x * 2; }\n",
        )
        return self.write(
            Path("src") / "main.wind",
            "use geom;\n"
            "fn main() -> Int { return geom::twice(21); }\n",
        )

    def test_project_build_outputs(self):
        entry = self._scaffold_project()
        code, out, err = run_cli(["--project", str(self.root)])
        self.assertEqual(0, code, err)
        self.assertIn("demoapp", out)

        typed = json.loads(
            (self.root / "target" / "demoapp.typed.json").read_text(encoding="utf-8")
        )
        self.assertEqual("cwind-typed-ast", typed["format"])
        self.assertEqual(str(entry.resolve()), typed["source"])

        project_doc = json.loads(
            (self.root / "target" / "project.json").read_text(encoding="utf-8")
        )
        self.assertEqual("cwind-project", project_doc["format"])
        self.assertEqual("demoapp", project_doc["package"]["name"])
        self.assertEqual("0.2.0", project_doc["package"]["version"])
        self.assertEqual("src/main.wind", project_doc["package"]["entry"])
        self.assertEqual("demoapp.typed.json", project_doc["target"])
        module_paths = [tuple(m["path"]) for m in project_doc["modules"]]
        self.assertIn(("geom",), module_paths)
        sources = {
            tuple(m["path"]): m["source"] for m in project_doc["modules"]
        }
        self.assertEqual(
            ("geom", "mod.wind"), Path(sources[("geom",)]).parts[-2:]
        )

    def test_project_flag_defaults_to_cwd(self):
        self._scaffold_project()
        saved_cwd = os.getcwd()
        try:
            os.chdir(self.root)
            code, out, err = run_cli(["--project"])
        finally:
            os.chdir(saved_cwd)
        self.assertEqual(0, code, err)
        self.assertTrue((self.root / "target" / "demoapp.typed.json").exists())

    def test_project_from_nested_start_walks_up(self):
        self._scaffold_project()
        deep = self.root / "src" / "deep"
        deep.mkdir(parents=True, exist_ok=True)
        code, _, err = run_cli(["--project", str(deep)])
        self.assertEqual(0, code, err)
        self.assertTrue((self.root / "target" / "project.json").exists())

    def test_missing_manifest_reports_error(self):
        code, _, err = run_cli(["--project", str(self.root)])
        self.assertEqual(2, code)
        self.assertIn(MANIFEST_NAME, err)

    def test_invalid_manifest_reports_error(self):
        self.write(MANIFEST_NAME, "[package]\nname = \"ok\"\n")
        code, _, err = run_cli(["--project", str(self.root)])
        self.assertEqual(1, code)
        self.assertIn("version", err)

    def test_missing_entry_reports_error(self):
        self.manifest(entry="src/main.wind")
        code, _, err = run_cli(["--project", str(self.root)])
        self.assertEqual(1, code)
        self.assertIn("entry point", err)

    def test_compile_failure_writes_no_target(self):
        self.manifest()
        self.write("src/main.wind", "fn main() -> Missing { return 0; }\n")
        code, _, err = run_cli(["--project", str(self.root)])
        self.assertEqual(1, code)
        self.assertIn("Unknown type 'Missing'", err)
        self.assertFalse((self.root / "target").exists())

    def test_file_argument_conflicts_with_project(self):
        src = self.write("loose.wind", "fn main() -> Int { return 0; }\n")
        code, _, err = run_cli(["--project", str(self.root), str(src)])
        self.assertEqual(2, code)
        self.assertIn("--project cannot be combined", err)


if __name__ == "__main__":
    unittest.main()
