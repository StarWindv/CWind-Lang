"""todo-70/71/97: directory modules, Breeze.toml manifests, project builds.

Covers:
- todo-70: ``libs/foo/mod.wind`` resolves ``use foo;`` (old-Rust
  ``dir + mod.rs`` layout); sibling files stay addressable as submodules;
  a file and a ``mod`` directory claiming the same name are ambiguous;
  pure ``pub use`` facades export their re-exports.
- todo-71: ``Breeze.toml`` discovery + validation following the design
  draft (``.exclude/demo/Breeze.toml``): ``[package]`` name/version/
  identifier/id_version/authors/homepage, the ``[entry]`` table
  (source/is_lib/module) and ``"version[,identifier]"`` dependencies.
- todo-97: ``cwindf --project`` builds the package entry into
  ``target/<name>.typed.json`` plus a ``project.json`` build index; the
  package's own ``lib.wd`` facade is auto-imported into main.
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
    DEFAULT_ENTRY_MODULE,
    DEFAULT_ENTRY_SOURCE,
    DEFAULT_IDENTIFIER,
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
        version: str = "0.0.1",
        identifier: str | None = None,
        id_version: str | None = None,
        authors: list[str] | None = None,
        homepage: str | None = None,
        entry_source: str | None = None,
        is_lib: bool | None = None,
        entry_module: str | None = None,
        dependencies: dict[str, str] | None = None,
    ) -> Path:
        lines = ["[package]", f'name = "{name}"', f'version = "{version}"']
        if identifier is not None:
            lines.append(f'identifier = "{identifier}"')
        if id_version is not None:
            lines.append(f'id_version = "{id_version}"')
        if authors is not None:
            quoted = ", ".join(f'"{a}"' for a in authors)
            lines.append(f"authors = [{quoted}]")
        if homepage is not None:
            lines.append(f'homepage = "{homepage}"')
        entry_lines = []
        if entry_source is not None:
            entry_lines.append(f'source = "{entry_source}"')
        if is_lib is not None:
            entry_lines.append(f"is_lib = {str(is_lib).lower()}")
        if entry_module is not None:
            entry_lines.append(f'module = "{entry_module}"')
        if entry_lines:
            lines.append("")
            lines.append("[entry]")
            lines.extend(entry_lines)
        if dependencies:
            lines.append("")
            lines.append("[dependencies]")
            lines.extend(f'{k} = "{v}"' for k, v in dependencies.items())
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
            Path("libs") / "geom" / "mod.wind",
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
            Path("libs") / "geom" / "mod.wind",
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
        self.write(Path("libs") / "geom" / "mod.wind", "")
        self.write(
            Path("libs") / "geom" / "shapes.wind",
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

    def test_nested_directory_module(self):
        self.write(
            Path("libs") / "a" / "b" / "mod.wind",
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
            Path("libs") / "wm" / "mod.wd",
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
        self.write(Path("libs") / "dup.wind", "pub fn f1() -> Int { return 1; }\n")
        self.write(
            Path("libs") / "dup" / "mod.wind", "pub fn f2() -> Int { return 2; }\n"
        )
        main = self.write("main.wind", "use dup;\nfn main() -> Int { return 0; }\n")
        with self.assertRaises(Exception) as caught:
            self.parse_entry(main)
        self.assertIn("ambiguous", str(caught.exception))

    def test_bare_root_mod_wind_registers_nothing(self):
        self.write(
            Path("libs") / "mod.wind",
            "pub fn rootless() -> Int { return 1; }\n",
        )
        self.write(
            Path("libs") / "real.wind",
            "pub fn real() -> Int { return 2; }\n",
        )
        main = self.write(
            "main.wind",
            "use real;\nfn main() -> Int { return real::real(); }\n",
        )
        parsed = self.parse_entry(main)
        result = run_sa_with_errors(parsed.program)
        self.assertEqual([], [e.message for e in result.errors])

    def test_pure_reexport_facade_exports_its_uses(self):
        """A file consisting only of ``pub use`` still has a public API."""
        self.write(
            Path("libs") / "facade.wind",
            "pub use inner::thing;\n",
        )
        self.write(
            Path("libs") / "inner.wind",
            "pub fn thing() -> Int { return 77; }\n",
        )
        wildcard_main = self.write(
            "w.wind",
            "use facade::*;\nfn main() -> Int { return thing(); }\n",
        )
        parsed = self.parse_entry(wildcard_main)
        result = run_sa_with_errors(parsed.program)
        self.assertEqual([], [e.message for e in result.errors])

        plain_main = self.write(
            "p.wind",
            "use facade;\nfn main() -> Int { return facade::thing(); }\n",
        )
        parsed = self.parse_entry(plain_main)
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

    def test_load_manifest_defaults_match_design_draft(self):
        self.manifest()
        loaded = load_manifest(self.root / MANIFEST_NAME)
        self.assertEqual("demo", loaded.name)
        self.assertEqual("0.0.1", loaded.version)
        self.assertEqual(DEFAULT_IDENTIFIER, loaded.identifier)
        self.assertIsNone(loaded.id_version)
        self.assertEqual(DEFAULT_ENTRY_SOURCE, loaded.entry.source)
        self.assertFalse(loaded.entry.is_lib)
        self.assertEqual(DEFAULT_ENTRY_MODULE, loaded.entry.module)
        self.assertEqual(self.root.resolve(), loaded.root.resolve())
        self.assertEqual(
            (self.root / "src").resolve(), loaded.source_path().resolve()
        )

    def test_load_manifest_full_fields(self):
        loaded = load_manifest(
            self.manifest(
                version="1.2.3",
                identifier="RC",
                id_version="0.4.0",
                authors=["A", "B"],
                homepage="https://cwind.example",
                entry_source="./app",
                is_lib=True,
                entry_module="pkg.wd",
                dependencies={"mathx": "1.0,Standard"},
            )
        )
        self.assertEqual("demo", loaded.name)
        self.assertEqual("1.2.3", loaded.version)
        self.assertEqual("RC", loaded.identifier)
        self.assertEqual("0.4.0", loaded.id_version)
        self.assertEqual(("A", "B"), loaded.authors)
        self.assertEqual("https://cwind.example", loaded.homepage)
        self.assertEqual("./app".replace("./", ""), loaded.entry.source)
        self.assertTrue(loaded.entry.is_lib)
        self.assertEqual("pkg.wd", loaded.entry.module)
        dep = loaded.dependencies["mathx"]
        self.assertEqual(("mathx", "1.0", "Standard"), (
            dep.name, dep.version, dep.identifier
        ))

    def test_dependency_without_identifier_defaults_lowest_tier(self):
        loaded = load_manifest(
            self.manifest(dependencies={"loose": "0.1.0"})
        )
        self.assertEqual("Dev", loaded.dependencies["loose"].identifier)

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

    def test_invalid_version_and_id_version_rejected(self):
        self.write(MANIFEST_NAME, '[package]\nname = "ok"\nversion = "one"\n')
        with self.assertRaises(ManifestError) as caught:
            load_manifest(self.root / MANIFEST_NAME)
        self.assertIn("version", str(caught.exception))

        self.write(
            MANIFEST_NAME,
            '[package]\nname = "ok"\nversion = "1.0.0"\nid_version = "x"\n',
        )
        with self.assertRaises(ManifestError) as caught:
            load_manifest(self.root / MANIFEST_NAME)
        self.assertIn("id_version", str(caught.exception))

    def test_unknown_identifier_rejected(self):
        self.write(
            MANIFEST_NAME,
            '[package]\nname = "ok"\nversion = "1.0.0"\nidentifier = "Final"\n',
        )
        with self.assertRaises(ManifestError) as caught:
            load_manifest(self.root / MANIFEST_NAME)
        self.assertIn("identifier", str(caught.exception))

    def test_invalid_toml_rejected(self):
        self.write(MANIFEST_NAME, "[package\nname = = broken")
        with self.assertRaises(ManifestError) as caught:
            load_manifest(self.root / MANIFEST_NAME)
        self.assertIn("TOML", str(caught.exception))

    def test_absolute_entry_source_rejected(self):
        self.write(
            MANIFEST_NAME,
            '[package]\nname = "ok"\nversion = "1.0.0"\n'
            '[entry]\nsource = "/abs"\n',
        )
        with self.assertRaises(ManifestError) as caught:
            load_manifest(self.root / MANIFEST_NAME)
        self.assertIn("relative", str(caught.exception))

    def test_authors_type_checked(self):
        self.write(
            MANIFEST_NAME,
            '[package]\nname = "ok"\nversion = "1.0.0"\nauthors = [1]\n',
        )
        with self.assertRaises(ManifestError) as caught:
            load_manifest(self.root / MANIFEST_NAME)
        self.assertIn("authors", str(caught.exception))

    def test_bad_dependency_spec_rejected(self):
        cases = ['broken = "abc"', 'tier = "1.0.0,Gold"', 'many = "1.0.0,Dev,x"']
        for spec in cases:
            with self.subTest(spec=spec):
                self.write(
                    MANIFEST_NAME,
                    f'[package]\nname = "ok"\nversion = "1.0.0"\n\n'
                    f"[dependencies]\n{spec}\n",
                )
                with self.assertRaises(ManifestError):
                    load_manifest(self.root / MANIFEST_NAME)


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


class Todo97ProjectBuild(ProjectScaffold):
    """Project scaffolding mirrors the design draft (.exclude/demo): a
    source root with a ``lib.wd`` facade re-exporting ``modules/``, and a
    bare-calling ``main.wd``."""

    def _scaffold_demo_project(self) -> Path:
        self.manifest(name="demoapp", version="0.2.0")
        self.write(
            Path("src") / "modules" / "great.wd",
            'pub fn great(name: String) -> String {\n'
            '\treturn "Hello, {}!".format(name);\n}\n',
        )
        self.write(
            Path("src") / "lib.wd",
            "pub use modules::great::great;\n",
        )
        return self.write(
            Path("src") / "main.wd",
            'fn main() {\n\tprint(great("Wind"));\n}\n',
        )

    def test_demo_layout_build_and_auto_package_lib(self):
        entry = self._scaffold_demo_project()
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
        self.assertEqual("Dev", project_doc["package"]["identifier"])
        self.assertEqual(
            {"source": "src", "is_lib": False, "module": "lib.wd"},
            project_doc["entry"],
        )
        self.assertEqual(str((self.root / "src" / "main.wd").resolve()),
                         project_doc["entry_file"])
        # The import manifest lists entry-level ``use`` declarations only;
        # lib.wd's internal ``pub use`` stays inside the facade module.
        autos = [i for i in typed["imports"] if i.get("auto")]
        self.assertEqual([("demoapp",)], [tuple(i["path"]) for i in autos])
        auto_source = autos[0]["source"]
        self.assertEqual(("src", "lib.wd"), Path(auto_source).parts[-2:])

        # SA passes on the flattened program: great is bound via the
        # auto-imported lib.wd facade.
        names = {sym["name"] for sym in typed["symbols"]}
        self.assertIn("great", names)
        autos = [i for i in typed["imports"] if i.get("auto")]
        auto_paths = {tuple(i["path"]) for i in autos}
        self.assertIn(("demoapp",), auto_paths)

    def test_project_flag_defaults_to_cwd(self):
        self._scaffold_demo_project()
        saved_cwd = os.getcwd()
        try:
            os.chdir(self.root)
            code, out, err = run_cli(["--project"])
        finally:
            os.chdir(saved_cwd)
        self.assertEqual(0, code, err)
        self.assertTrue((self.root / "target" / "demoapp.typed.json").exists())

    def test_project_from_nested_start_walks_up(self):
        self._scaffold_demo_project()
        deep = self.root / "src" / "deep"
        deep.mkdir(parents=True, exist_ok=True)
        code, _, err = run_cli(["--project", str(deep)])
        self.assertEqual(0, code, err)
        self.assertTrue((self.root / "target" / "project.json").exists())

    def test_is_lib_builds_facade_as_entry(self):
        self.manifest(name="mylib", is_lib=True)
        self.write(
            Path("src") / "lib.wd",
            "pub use util::answer;\n",
        )
        self.write(
            Path("src") / "util.wd",
            "pub fn answer() -> Int { return 42; }\n",
        )
        code, _, err = run_cli(["--project", str(self.root)])
        self.assertEqual(0, code, err)
        typed = json.loads(
            (self.root / "target" / "mylib.typed.json").read_text(encoding="utf-8")
        )
        self.assertIn("answer", {sym["name"] for sym in typed["symbols"]})

    def test_missing_manifest_reports_error(self):
        code, _, err = run_cli(["--project", str(self.root)])
        self.assertEqual(2, code)
        self.assertIn(MANIFEST_NAME, err)

    def test_invalid_manifest_reports_error(self):
        self.write(MANIFEST_NAME, '[package]\nname = "ok"\n')
        code, _, err = run_cli(["--project", str(self.root)])
        self.assertEqual(1, code)
        self.assertIn("version", err)

    def test_missing_entry_reports_error(self):
        self.manifest()
        code, _, err = run_cli(["--project", str(self.root)])
        self.assertEqual(1, code)
        self.assertIn("entry point", err)

    def test_compile_failure_writes_no_target(self):
        self.manifest()
        self.write(Path("src") / "main.wd", "fn main() -> Missing { return 0; }\n")
        code, _, err = run_cli(["--project", str(self.root)])
        self.assertEqual(1, code)
        self.assertIn("Unknown type 'Missing'", err)
        self.assertFalse((self.root / "target").exists())

    def test_file_argument_conflicts_with_project(self):
        src = self.write("loose.wind", "fn main() -> Int { return 0; }\n")
        code, _, err = run_cli(["--project", str(self.root), str(src)])
        self.assertEqual(2, code)
        self.assertIn("--project cannot be combined", err)


class UserTypeShadowing(ProjectScaffold):
    """User-declared types must shadow same-named prelude/package-lib
    items (layering: std prelude < package lib < user code).

    Shadowing is name-based, so the dropped layer's extra/impl blocks
    (methods) must vanish with their owner — otherwise method bindings
    would attach to the user's unrelated type.
    """

    def _write_option_prelude(self) -> None:
        """Mimic the real std: prelude re-exports an Option enum that has
        an extra block (``unwrap``) attached to it."""
        self.write(
            Path("libs") / "option.wind",
            "pub enum Option<T> {\n"
            "    None,\n"
            "    Some(T),\n"
            "}\n"
            "\n"
            "extra<T> Option<T> {\n"
            "    pub fn unwrap(self) -> T {\n"
            "        return match (self) {\n"
            "            Option::Some(val) => val,\n"
            "            _ => panic(),\n"
            "        };\n"
            "    }\n"
            "}\n",
        )
        self.write(
            Path("libs") / "prelude.wind",
            "pub use std::option::Option;\n",
        )

    def test_user_enum_shadows_prelude_option_with_methods(self):
        self._write_option_prelude()
        main = self.write(
            "main.wind",
            "enum Option {\n"
            "    Nope,\n"
            "    Yep,\n"
            "}\n"
            "\n"
            "fn main() -> Int {\n"
            "    let o: Option = Option::Yep;\n"
            "    return match (o) {\n"
            "        Option::Nope => 1,\n"
            "        Option::Yep => 2,\n"
            "    };\n"
            "}\n",
        )
        parsed = self.parse_entry(main)
        result = run_sa_with_errors(parsed.program)
        self.assertEqual([], [e.message for e in result.errors])

        options = [
            item for item in parsed.program.items
            if type(item).__name__ == "EnumDecl"
            and getattr(item, "name", None) == "Option"
        ]
        # exactly one Option survives: the user's zero-variant-parameter one
        self.assertEqual(1, len(options))
        self.assertEqual([], [p.name for p in options[0].params])
        # the prelude's extra block must be gone with its owner: no
        # method bindings for the user's unrelated Option
        owners = {b.owner for b in result.info.bindings}
        self.assertNotIn("Option", owners)

    def test_user_struct_shadows_prelude_struct(self):
        self.write(
            Path("libs") / "prelude.wind",
            "pub struct Value {\n"
            "    pub x: Int,\n"
            "    pub y: Int,\n"
            "}\n"
            "\n"
            "extra Value {\n"
            "    pub fn sum(self) -> Int { return self.x + self.y; }\n"
            "}\n",
        )
        main = self.write(
            "main.wind",
            "struct Value {\n"
            "    pub w: Int,\n"
            "}\n"
            "\n"
            "fn main() -> Int {\n"
            "    let w: Int = 3;\n"
            "    let v: Value = Value { w };\n"
            "    return v.w;\n"
            "}\n",
        )
        parsed = self.parse_entry(main)
        result = run_sa_with_errors(parsed.program)
        self.assertEqual([], [e.message for e in result.errors])
        # field `w` only exists on the user's struct; a leaked prelude
        # struct would fail this construction with an unknown-field error

    def test_user_typedef_shadows_prelude_typedef(self):
        self.write(Path("libs") / "prelude.wind", "pub typedef Id = Int32;\n")
        main = self.write(
            "main.wind",
            "typedef Id = String;\n"
            "\n"
            "fn main() -> Int {\n"
            "    let v: Id = \"shadowed\";\n"
            "    print(v.length());\n"
            "    return 0;\n"
            "}\n",
        )
        parsed = self.parse_entry(main)
        result = run_sa_with_errors(parsed.program)
        self.assertEqual([], [e.message for e in result.errors])
        # assigning a String to a leaked Int32 alias would be an SA error

    def test_package_lib_type_shadows_prelude_type(self):
        """Layering check: the package lib sits above std prelude."""
        self.write(
            Path("libs") / "prelude.wind",
            "pub struct Value {\n"
            "    pub y: Int,\n"
            "}\n",
        )
        self.write(
            Path("src") / "pv.wind",
            "pub struct Value {\n"
            "    pub x: Int,\n"
            "}\n",
        )
        self.write(Path("src") / "lib.wd", "pub use pv::Value;\n")
        self.manifest(name="shadowpkg")
        main = self.write(
            Path("src") / "main.wd",
            "fn main() -> Int {\n"
            "    let x: Int = 5;\n"
            "    let v: Value = Value { x };\n"
            "    return v.x;\n"
            "}\n",
        )
        code, out, err = run_cli(["--project", str(self.root)])
        self.assertEqual(0, code, err)


if __name__ == "__main__":
    unittest.main()
