"""Data-driven frontend case runner: discovers ``cases/`` automatically.

Historically every todo / bug got its own ``test_*.py`` that was nothing
but ``sys.path`` boilerplate plus a class calling ``assert_case("area",
"name")`` -- twenty-some near-identical driver scripts around the same two
harness primitives.  This module replaces them with pure discovery:

* ``cases/<area>/<name>.wind`` (+ optional ``<name>.json`` sidecar; absent
  means "clean") are lexed -> parsed -> semantically analyzed through
  :func:`harness.run_pipeline` and checked against the sidecar.
* ``cases/<area>/<case>/`` project trees (``libs/`` + ``main.wind`` +
  ``expect.json``) are copied to a temp dir and run through
  :func:`harness.run_project_case`.

Adding a case is dropping a data file -- no Python.  Adding a *new area*
means listing it below, because a handful of areas (``sa``/``parser``/
``lexer``/``cffi``/``cfg``/``hex``/...) are driven by bespoke test modules
that assert on typed-AST/token structure with their own sidecar schema;
those areas are intentionally NOT swept here (they would double-run and
mis-read their sidecars), and a handful of project-tree areas with bespoke
per-case checks (``todo112``'s ``use_decls``, ``bug43``) stay bespoke too.

The ``why`` behind each area (root cause of a bug, the semantics a todo
locks in) lives in ``cases/README.md``, next to the data it describes.
"""

from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent.parent.parent.parent
for path in (ROOT / "mvp/frontend/src", ROOT / "mvp/frontend/tests"):
    sys.path.insert(0, str(path))

import harness  # noqa: E402

# Single-file areas (``<name>.wind`` + optional ``<name>.json``) that carry
# only pipeline-outcome expectations and have no bespoke driver module.
SINGLE_FILE_AREAS = frozenset({
    "bug33", "bug34", "bug35", "bug37", "bug38", "bug39", "bug40",
    "bug46", "bug47", "bug48", "bug49", "bug52", "bug53", "bug62",
    "bug64", "bug65", "bug66", "bug70", "const", "copy",
    "todo17", "todo50", "todo74", "todo75", "todo87", "todo108", "todo120",
    "todo122", "todo132", "todo145", "todo147", "todo151", "todo156",
    "todo164", "todo165", "todo44", "todo180", "todo182",
    "todo55",
})

# Project-tree areas (``<case>/expect.json``) swept with the shared runner.
# ``todo112`` (structural ``use_decls`` check) and ``bug43`` (mixed layout)
# are owned by bespoke modules and deliberately excluded.
PROJECT_TREE_AREAS = frozenset({
    "bug32", "bug36", "bug42", "bug47", "bug52", "bug54", "bug61", "bug63",
    "bug69", "bug70", "bug80", "proc_macro",
    "todo13", "todo107", "todo119", "todo124", "todo125", "todo126",
    "todo144", "todo154", "todo163",
})


class SingleFileCaseTests(harness.CaseAssertionsMixin):
    def test_all(self):
        for area in sorted(SINGLE_FILE_AREAS):
            for name in harness.iter_pipeline_cases(area):
                with self.subTest(case=f"{area}/{name}"):
                    exp = harness.expect(area, name)
                    result = harness.run_pipeline(
                        harness.source(area, name), stage=exp.get("stage", "sa")
                    )
                    self.check_outcome(result, exp, ctx=f"{area}/{name}")


class ProjectTreeCaseTests(harness.CaseAssertionsMixin):
    def test_all(self):
        for area in sorted(PROJECT_TREE_AREAS):
            for case_dir in harness.iter_project_cases(area):
                with self.subTest(case=f"{area}/{case_dir.name}"):
                    pc = harness.run_project_case(case_dir)
                    self.check_outcome(
                        pc.outcome, pc.exp, ctx=f"{area}/{case_dir.name}"
                    )


def _fixture_manifest(root: Path) -> tuple[tuple[str, bytes], ...]:
    return tuple(
        (path.relative_to(root).as_posix(), path.read_bytes())
        for path in sorted(root.rglob("*")) if path.is_file()
    )


# Captured from the canonical case trees BEFORE migration; path + length + bytes.
_ORIGINAL_PROFILE_DIGESTS = {
    "bug32_extern_closure": "b82926ebde7fb7fd7adf4c74d56e8a852be9ee64a2a462170b54960f5960e241",
    "bug54_shadow_local_panic": "8fe1049e628e4eae51ab39b56baf0169980b198dbf222a85b5bafe071849054e",
    "todo107_group_self": "d96a8231663a1c096f4ed60ef8fbf61c4f5252067497dea8b80d7f6de4566528",
    "todo112_basic_group": "7ae6863b22d58a909ea9f28e539aedd4afeb0e08634ccd8282c3230c8e4230b7",
    "todo119_bare_crate": "6b7e04386d48fa916d5f3b7c79779cc1ae72161f8c62d0ccec3c07b9f66a14c4",
    "todo119_group_self_rejected": "f090740849eb71d3e1ff9b66106c7da548b72c186a3be3651ce283715e5196bc",
    "todo119_pub_std_bare_name_unknown": "8d1bc225c3c364c1d9887e49bbf9310be32a6d6346e067e2afc3e8762c4f3f60",
    "todo124_group_as_rejected": "ce26bdf4db22a0cceaaef333f19e28a17921109061bdbfb1ec86e42929d97007",
    "todo124_item_alias": "535d56e6d04cab14f7df26af5418587110300b3c6c3b58d32f930b9eda3ce5cb",
    "todo125_item_alias": "e0ab15acd9dbae5b8a1ffb7009705a1fb575e0ad5e1e6bab23687bc008ee0684",
    "todo126_alias_std": "a0eccc583bd5226d6ed8339bf8464e7d09ec2723c38b383a61ac2748ee578deb",
    "todo126_basic": "aca19835c16aacb27c8e296f2084e4b15f0773b14042eb4f0a50fcd70cfdafa4",
    "todo13_extra_private_field_rejected": "069dacc329972358dae256c38655fc26d350a64ef3e8fe0d723a399467c27d29",
    "todo13_private_read_rejected": "e44e0599db6a2266ba5933a43e8a8d27e0517605e5b8123f378c70e1645aed67",
    "todo154_fqn_alias_basic": "27998189e4692f19526a1fb2dc4e501cf4640eb601b4fe2a9e22b5f2d35dd63f",
    "todo154_fqn_alias_chain": "8d3054f687542f5c8efbb9a323480f0b9ace6836170e87d3258781cce1e09b67",
}


class SharedLibsTests(unittest.TestCase):
    def test_migrated_copies_match_original_manifests(self):
        self.assertEqual(set(harness.LIBS_PROFILE_CASES), set(_ORIGINAL_PROFILE_DIGESTS))
        self.assertEqual(
            sum(map(len, harness.LIBS_PROFILE_CASES.values())),
            len(harness._CASE_LIBS_PROFILES),
        )
        self.assertEqual(
            {p.name for p in harness.PROFILE_DIR.iterdir()},
            set(harness.LIBS_PROFILE_CASES),
        )
        for profile, cases in harness.LIBS_PROFILE_CASES.items():
            original = _fixture_manifest(harness.PROFILE_DIR / profile)
            digest = hashlib.sha256()
            for path, data in original:
                digest.update(path.encode("utf-8") + b"\0")
                digest.update(len(data).to_bytes(8, "big"))
                digest.update(data)
            self.assertEqual(digest.hexdigest(), _ORIGINAL_PROFILE_DIGESTS[profile])
            for case in cases:
                with self.subTest(case=case), tempfile.TemporaryDirectory() as td:
                    source = harness.CASES_DIR / case
                    self.assertFalse((source / "libs").exists())
                    self.assertIn(source, list(harness.iter_project_cases(source.parent.name)))
                    self.assertIn(source.parent.name, PROJECT_TREE_AREAS | {"todo112"})
                    root = Path(td)
                    harness.copy_project_case(source, root)
                    self.assertEqual(_fixture_manifest(root / "libs"), original)
                    expected = tuple(
                        (p, data) for p, data in _fixture_manifest(source)
                        if Path(p).name != "expect.json"
                    )
                    actual = tuple(
                        (p, data) for p, data in _fixture_manifest(root)
                        if not p.startswith("libs/")
                    )
                    self.assertEqual(actual, expected)

    def test_projects_and_templates_are_independent(self):
        for profile, cases in harness.LIBS_PROFILE_CASES.items():
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as td:
                template = harness.PROFILE_DIR / profile
                original = _fixture_manifest(template)
                first, second, third = (Path(td) / name for name in ("one", "two", "three"))
                harness.copy_project_case(harness.CASES_DIR / cases[0], first)
                harness.copy_project_case(harness.CASES_DIR / cases[1], second)
                relative, _ = original[0]
                a, b, t = first / "libs" / relative, second / "libs" / relative, template / relative
                self.assertFalse(a.samefile(b))
                self.assertFalse(a.samefile(t))
                self.assertFalse(a.is_symlink())
                a.write_bytes(b"changed\r\n")
                self.assertEqual(_fixture_manifest(second / "libs"), original)
                self.assertEqual(_fixture_manifest(template), original)
                a.unlink()
                harness.copy_project_case(harness.CASES_DIR / cases[0], third)
                self.assertEqual(_fixture_manifest(third / "libs"), original)

    def test_local_libs_are_exact_and_profile_conflicts_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            case = base / "cases" / "area" / "local"
            libs = case / "libs"
            libs.mkdir(parents=True)
            # Deliberately no mod.wind: copying must not add declarations.
            (libs / "raw.wind").write_bytes(b"\xef\xbb\xbfpub fn raw() {}\r\n// no trailing newline")
            with patch.object(harness, "CASES_DIR", base / "cases"):
                root = base / "copy"
                harness.copy_project_case(case, root)
                self.assertEqual(_fixture_manifest(libs), _fixture_manifest(root / "libs"))
                with self.assertRaisesRegex(ValueError, "must be empty"):
                    harness.copy_project_case(case, root)
                with patch.dict(harness._CASE_LIBS_PROFILES, {"area/local": "unused"}):
                    with self.assertRaisesRegex(ValueError, "local libs conflicts"):
                        harness.copy_project_case(case, base / "conflict")
                    self.assertFalse((base / "conflict").exists())
                with self.assertRaisesRegex(ValueError, "must be separate"):
                    harness.copy_project_case(case, case / "nested")

    def test_profiles_are_not_compiler_import_roots(self):
        from cwind_frontend.parser.defs import _module_roots
        self.assertFalse(harness.PROFILE_DIR.is_relative_to(harness.CASES_DIR))
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            harness.copy_project_case(harness.CASES_DIR / "todo124/item_alias", root)
            roots = _module_roots(root)
            self.assertEqual([r.directory for r in roots if r.kind == "std"], [(root / "libs").resolve()])
            for base in (TESTS, root):
                for module in _module_roots(base):
                    self.assertFalse(harness.PROFILE_DIR.is_relative_to(module.directory))
                    self.assertFalse(module.directory.is_relative_to(harness.PROFILE_DIR))


if __name__ == "__main__":
    unittest.main()
