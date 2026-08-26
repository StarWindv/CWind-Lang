"""todo-98: per-module typed-AST artifacts in project mode.

``cwindf --project`` keeps writing the whole-program JSON (the backend
input) and ``project.json``; it now also emits one semantically annotated
JSON per source file, mirroring the project's source tree under ``target/``
(``target/<relative/path>.wind.json``).  These tests pin down the artifact
layout, envelope shape, annotation presence, per-module symbol/binding
filtering and the ``project.json`` artifact index.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path


TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent.parent.parent.parent
for path in (ROOT / "mvp/frontend/src", ROOT / "mvp/frontend/tests"):
    sys.path.insert(0, str(path))

from cwind_frontend.cli import main  # noqa: E402


class ProjectScaffold(unittest.TestCase):
    """Temp Breeze project helpers."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, relative: str, text: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def write_manifest(self, name: str = "art") -> Path:
        return self.write(
            "Breeze.toml",
            "[package]\n"
            f'name = "{name}"\n'
            'version = "0.1.0"\n',
        )

    def run_project(self) -> int:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--project", str(self.root)])
        self.assertEqual(0, code, err.getvalue())
        return code

    def target(self, *parts: str) -> Path:
        return self.root.joinpath("target", *parts)


class ArtifactLayout(ProjectScaffold):
    """One JSON per participating source file, mirroring src/ structure."""

    def test_artifacts_mirror_source_tree(self):
        self.write_manifest()
        self.write("src/main.wd", 'fn main() { print(greet()); }\n')
        self.write(
            "src/lib.wd",
            "pub use utils::greet;\n",
        )
        self.write(
            "src/utils.wind",
            "pub fn greet() -> String { return \"hi\"; }\n"
            "fn secret() -> Int { return 1; }\n",
        )
        self.run_project()

        expected = [
            self.target("src", "main.wd.json"),
            self.target("src", "lib.wd.json"),
            self.target("src", "utils.wind.json"),
        ]
        for path in expected:
            self.assertTrue(path.is_file(), f"missing artifact {path}")
        # the whole-program build output is untouched
        self.assertTrue(self.target("art.typed.json").is_file())

    def test_project_json_records_artifact_index(self):
        self.write_manifest()
        self.write("src/main.wd", 'fn main() { print(greet()); }\n')
        self.write("src/lib.wd", "pub use utils::greet;\n")
        self.write(
            "src/utils.wind",
            "pub fn greet() -> String { return \"hi\"; }\n",
        )
        self.run_project()
        doc = json.loads(self.target("project.json").read_text("utf-8"))
        artifacts = doc["artifacts"]
        self.assertEqual(
            {
                "src/main.wd": "src/main.wd.json",
                "src/lib.wd": "src/lib.wd.json",
                "src/utils.wind": "src/utils.wind.json",
            },
            artifacts,
        )


class ArtifactContent(ProjectScaffold):
    """Envelope shape, annotations and per-module filtering."""

    def _build(self):
        self.write_manifest()
        self.write(
            "src/main.wd",
            "fn main() {\n"
            "    let msg: String = greet();\n"
            "    print(msg);\n"
            "}\n",
        )
        self.write("src/lib.wd", "pub use utils::greet;\n")
        self.write(
            "src/utils.wind",
            "pub fn greet() -> String { return \"hi\"; }\n",
        )
        self.run_project()
        entry = json.loads(
            self.target("src", "main.wd.json").read_text("utf-8")
        )
        module = json.loads(
            self.target("src", "utils.wind.json").read_text("utf-8")
        )
        facade = json.loads(
            self.target("src", "lib.wd.json").read_text("utf-8")
        )
        return entry, module, facade

    def test_envelope_shape_and_roles(self):
        entry, module, facade = self._build()
        for doc in (entry, module, facade):
            self.assertEqual("cwind-typed-ast", doc["format"])
            self.assertEqual(1, doc["version"])
            self.assertIn(doc["role"], ("entry", "module"))
            self.assertTrue(Path(doc["source"]).is_file())
            self.assertEqual("Program", doc["ast"]["kind"])
        self.assertEqual("entry", entry["role"])
        self.assertEqual("module", module["role"])
        self.assertEqual("module", facade["role"])

    def test_items_partitioned_by_defining_file(self):
        entry, module, facade = self._build()
        self.assertEqual(
            ["FnDecl"],
            [item["kind"] for item in entry["ast"]["items"]],
        )
        self.assertEqual("main", entry["ast"]["items"][0]["name"])
        self.assertEqual(
            ["FnDecl"],
            [item["kind"] for item in module["ast"]["items"]],
        )
        self.assertEqual("greet", module["ast"]["items"][0]["name"])
        # a pure re-export facade owns no declarations but keeps its imports
        self.assertEqual([], facade["ast"]["items"])
        self.assertEqual(1, len(facade["imports"]))

    def test_sa_annotations_present(self):
        _, module, _ = self._build()
        fn = module["ast"]["items"][0]
        self.assertIsNotNone(fn.get("id"))
        self.assertEqual({"name": "String"}, fn["ann"]["type"])
        ret = fn["body"]["stmts"][0]
        self.assertEqual(
            {"name": "String"}, ret["ann"].get("expected_return")
        )

    def test_symbols_filtered_per_module(self):
        entry, module, facade = self._build()
        self.assertEqual(["main"], [s["name"] for s in entry["symbols"]])
        self.assertEqual(["greet"], [s["name"] for s in module["symbols"]])
        self.assertEqual([], [s["name"] for s in facade["symbols"]])

    def test_node_ids_stay_globally_unique(self):
        entry, module, facade = self._build()

        def collect(doc):
            found = []

            def walk(node):
                if isinstance(node, dict):
                    if isinstance(node.get("id"), int):
                        found.append(node["id"])
                    for value in node.values():
                        walk(value)
                elif isinstance(node, list):
                    for value in node:
                        walk(value)

            walk(doc)
            return found

        seen: list[int] = []
        for doc in (entry, module, facade):
            ids = collect(doc)
            self.assertEqual(len(ids), len(set(ids)), "ids collide in one doc")
            seen.extend(ids)
        self.assertEqual(len(seen), len(set(seen)), "ids collide across docs")

    def test_imports_recorded_per_module(self):
        entry, module, facade = self._build()
        # main sees only its auto-imported package lib facade
        self.assertEqual(
            [(["art"], None, True)],
            [
                (i["path"], i.get("item"), bool(i.get("auto")))
                for i in entry["imports"]
            ],
        )
        # utils.wind has no uses at all
        self.assertEqual([], module["imports"])
        # lib.wd re-exports utils::greet
        self.assertEqual(
            (["utils", "greet"], "greet"),
            (facade["imports"][0]["path"], facade["imports"][0]["item"]),
        )


class PrivateRenamingInArtifacts(ProjectScaffold):
    """todo-79 interplay: mangled private names land in the artifacts."""

    def test_renamed_private_helper_is_recorded(self):
        self.write_manifest()
        self.write(
            "src/main.wd",
            "fn main() {\n    let n: Int = api();\n    print(n);\n}\n",
        )
        self.write("src/lib.wd", "pub use engine::api;\n")
        self.write(
            "src/engine.wind",
            "pub fn api() -> Int { return boost(); }\n"
            "fn boost() -> Int { return 42; }\n",
        )
        self.run_project()
        module = json.loads(
            self.target("src", "engine.wind.json").read_text("utf-8")
        )
        names = sorted(item["name"] for item in module["ast"]["items"])
        self.assertEqual(2, len(names))
        self.assertIn("api", names)
        boosted = [n for n in names if n.startswith("boost__")]
        self.assertEqual(1, len(boosted))
        # every reference inside the module was rewritten consistently
        text = json.dumps(module)
        self.assertNotIn('"boost"', text.replace(boosted[0], ""))


if __name__ == "__main__":
    unittest.main()
