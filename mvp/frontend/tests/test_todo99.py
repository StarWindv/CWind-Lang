"""todo-99: incremental project builds (build-state fingerprint invalidation).

``cwindf --project`` records every build input (entry + participating
sources via the todo-98 artifact set, Breeze.toml semantics, the effective
#[cfg] target, import-root tree layout and the frontend version) in
``target/.build-state.json``.  A follow-up run with all inputs unchanged
skips the pipeline entirely; any single change forces a full rebuild.

These tests exercise the CLI end to end against temp projects.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path


TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent.parent.parent.parent
for path in (ROOT / "mvp/frontend/src", ROOT / "mvp/frontend/tests"):
    sys.path.insert(0, str(path))

from cwind_frontend.cli import main  # noqa: E402
from cwind_frontend.incremental import STATE_NAME  # noqa: E402


LIB_GREET = 'pub fn greet() -> String { return "hi"; }\n'


class ProjectScaffold(unittest.TestCase):
    """Temp Breeze project helpers (mirrors test_todo98)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.write_manifest()

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

    def seed_project(self) -> None:
        """Minimal two-module project: main.wd + lib.wd + utils.wind."""
        self.write("src/main.wd", 'fn main() { print(greet()); }\n')
        self.write("src/lib.wd", "pub use utils::greet;\n")
        self.write("src/utils.wind", LIB_GREET)

    TARGET_OS = "linux"

    def run_project(self, *, expect: int = 0, target_os: str = TARGET_OS):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(
                ["--project", str(self.root), "--target-os", target_os]
            )
        self.assertEqual(expect, code, err.getvalue())
        return out.getvalue(), err.getvalue()

    def build(self, **kwargs):
        """First full build; asserts the classic success line."""
        text, _ = self.run_project(**kwargs)
        self.assertIn("[Project] art v0.1.0:", text)
        self.assertIn("module artifact(s)", text)
        return text

    def target(self, *parts: str) -> Path:
        return self.root.joinpath("target", *parts)

    def state_doc(self) -> dict:
        return json.loads(self.target(STATE_NAME).read_text("utf-8"))

    def assert_rebuilt(self, text: str) -> None:
        self.assertNotIn("up to date", text)
        self.assertIn("[Project] art v0.1.0:", text)

    def assert_up_to_date(self, text: str) -> None:
        self.assertIn("up to date", text)


class FirstBuildAndNoop(ProjectScaffold):
    """The core incrementality loop."""

    def test_first_build_writes_state_with_stamps(self):
        self.seed_project()
        self.build()
        doc = self.state_doc()
        self.assertEqual("cwind-build-state", doc["format"])
        self.assertEqual(1, doc["version"])
        # entry + both package sources are stamped by relative POSIX key
        for rel in ("src/main.wd", "src/lib.wd", "src/utils.wind"):
            stamp = doc["files"][rel]
            self.assertGreater(stamp["size"], 0)
            self.assertGreater(stamp["mtime_ns"], 0)
            self.assertEqual(64, len(stamp["sha256"]))
        # outputs list covers the backend input chain: project.json,
        # whole-program JSON and one artifact per participating source.
        self.assertIn("project.json", doc["outputs"])
        self.assertIn("art.typed.json", doc["outputs"])
        self.assertIn("src/utils.wind.json", doc["outputs"])

    def test_unchanged_inputs_skip_pipeline(self):
        self.seed_project()
        self.build()
        before = {
            name: stat.st_mtime_ns
            for name, stat in (
                ("proj", self.target("project.json").stat()),
                ("typed", self.target("art.typed.json").stat()),
                ("artifact", self.target("src", "utils.wind.json").stat()),
            )
        }
        time.sleep(0.02)
        text, _ = self.run_project()
        self.assert_up_to_date(text)
        after = {
            name: stat.st_mtime_ns
            for name, stat in (
                ("proj", self.target("project.json").stat()),
                ("typed", self.target("art.typed.json").stat()),
                ("artifact", self.target("src", "utils.wind.json").stat()),
            )
        }
        self.assertEqual(before, after, "up-to-date run must not rewrite outputs")

    def test_source_content_change_triggers_rebuild(self):
        self.seed_project()
        self.build()
        time.sleep(0.02)
        self.write(
            "src/utils.wind",
            'pub fn greet() -> String { return "changed"; }\n',
        )
        text, _ = self.run_project()
        self.assert_rebuilt(text)
        doc = json.loads(self.target("art.typed.json").read_text("utf-8"))
        self.assertIn('"changed"', json.dumps(doc))

    def test_added_use_import_invalidates_entry(self):
        self.seed_project()
        self.build()
        time.sleep(0.02)
        self.write("src/auxmod.wind", 'pub fn extra_fn() -> Int { return 7; }\n')
        main_path = self.root / "src/main.wd"
        main_path.write_text(
            "use auxmod::extra_fn;\n"
            "fn main() { let n: Int = extra_fn(); print(greet()); }\n",
            encoding="utf-8",
        )
        text, _ = self.run_project()
        self.assert_rebuilt(text)

    def test_state_after_rebuild_reflects_new_sources(self):
        self.seed_project()
        self.build()
        time.sleep(0.02)
        self.write("src/utils.wind", LIB_GREET.replace("hi", "hey"))
        self.run_project()
        doc = json.loads(self.target("project.json").read_text("utf-8"))
        # the refreshed project doc still indexes every artifact on disk
        for rel in doc["artifacts"].values():
            self.assertTrue(self.target(rel).is_file(), rel)


class InvalidationCauses(ProjectScaffold):
    """Each external mutation must break freshness."""

    def setUp(self) -> None:
        super().setUp()
        self.seed_project()
        self.build()
        time.sleep(0.02)

    def test_touched_file_is_fresh_via_hash_fallback(self):
        # touch only: size/content identical -> metadata fast-path misses but
        # sha256 matches => still fresh (sccache behaviour).
        path = self.root / "src/utils.wind"
        data = path.read_bytes()
        path.write_bytes(data)
        future = time.time() + 5
        os.utime(path, (future, future))
        text, _ = self.run_project()
        self.assert_up_to_date(text)

    def test_deleted_module_forces_rebuild(self):
        (self.root / "src/utils.wind").unlink()
        text, _ = self.run_project(expect=1)
        # utils was imported by lib.wd -> the rebuild fails loudly instead of
        # claiming up-to-date over a vanished input.
        self.assertNotIn("up to date", text)

    def test_unreferenced_lib_tree_change_invalidates(self):
        # A brand-new module file changes the import-root layout even though
        # nothing imports it yet: staying stale could later mask an ambiguity
        # (e.g. foo.wind vs foo/mod.wind) or a fresh resolution.
        self.write("libs/unreferenced.wind", "pub fn stray() {}\n")
        text, _ = self.run_project()
        self.assert_rebuilt(text)

    def test_missing_output_forces_rebuild(self):
        self.target("art.typed.json").unlink()
        text, _ = self.run_project()
        self.assert_rebuilt(text)
        self.assertTrue(self.target("art.typed.json").is_file())

    def test_deleted_artifact_forces_rebuild(self):
        self.target("src", "utils.wind.json").unlink()
        text, _ = self.run_project()
        self.assert_rebuilt(text)

    def test_corrupt_state_forces_rebuild(self):
        self.target(STATE_NAME).write_text("{not json", encoding="utf-8")
        text, _ = self.run_project()
        self.assert_rebuilt(text)
        json.loads(self.target(STATE_NAME).read_text("utf-8"))  # rewritten

    def test_unknown_state_version_forces_rebuild(self):
        doc = self.state_doc()
        doc["version"] = 999
        self.target(STATE_NAME).write_text(json.dumps(doc), encoding="utf-8")
        text, _ = self.run_project()
        self.assert_rebuilt(text)

    def test_target_switch_forces_rebuild_and_persists_new_target(self):
        text, _ = self.run_project(target_os="windows")
        self.assert_rebuilt(text)
        self.assertEqual("windows", self.state_doc()["target"]["os"])
        # switching back invalidates again (different effective target)
        text, _ = self.run_project(target_os="linux")
        self.assert_rebuilt(text)
        self.assertEqual("linux", self.state_doc()["target"]["os"])

    def test_manifest_edit_invalidates_even_with_identical_sources(self):
        manifest = self.root / "Breeze.toml"
        manifest.write_text(
            "[package]\n"
            'name = "art"\n'
            'version = "0.2.0"\n',
            encoding="utf-8",
        )
        text, _ = self.run_project()
        self.assertNotIn("up to date", text)
        self.assertIn("[Project] art v0.2.0:", text)

    def test_failed_build_leaves_previous_state_intact(self):
        before = self.state_doc()
        time.sleep(0.02)
        self.write("src/utils.wind", "pub fn greet( { return 1; }\n")  # parse err
        self.run_project(expect=1)
        self.assertEqual(before, self.state_doc())


class Todo115CompilerIdentity(ProjectScaffold):
    """todo-115: the tool stamp carries compiler content identity.

    ``__version__`` is hand-maintained, so a frontend upgrade that forgets
    to bump it used to keep old build states fresh forever.  The persisted
    stamp is now ``<__version__>+<sha256 over the package's .py/.toml>``,
    and any change of the digest must invalidate.
    """

    def setUp(self) -> None:
        super().setUp()
        self.seed_project()
        self.build()
        time.sleep(0.02)

    def test_tool_stamp_includes_content_digest(self) -> None:
        from cwind_frontend import _version
        from cwind_frontend import incremental

        stamp = self.state_doc()["tool"]["frontend"]
        self.assertTrue(stamp.startswith(f"{_version.__version__}+"))
        self.assertNotEqual(stamp, _version.__version__)
        # deterministic: recomputing right now yields the same stamp
        self.assertEqual(incremental._frontend_version(), stamp)

    def test_unchanged_stamp_keeps_noop(self) -> None:
        text, _ = self.run_project()
        self.assert_up_to_date(text)

    def test_changed_compiler_digest_forces_rebuild(self) -> None:
        from cwind_frontend import incremental

        original = incremental._frontend_content_digest
        incremental._frontend_content_digest = lambda: "f" * 64
        try:
            text, _ = self.run_project()
        finally:
            incremental._frontend_content_digest = original
        self.assert_rebuilt(text)

