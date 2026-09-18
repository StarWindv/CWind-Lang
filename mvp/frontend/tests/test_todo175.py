"""todo-175 (step 1): FQN-distinct same-name functions and scope resolution.

Covers the reform's first step in the flat-program model:

- a user declaration and a std declaration may share a bare name; they are
  different FQNs and must not be reported as duplicate definitions and must
  not be renamed with an ad-hoc ``name__<hash>`` mangling;
- bare references resolve by scope (a std body sees std's declaration, the
  entry file sees its own);
- an extern C declaration without ``#[link_name]`` keeps its original C
  symbol when its std twin is FQN-qualified (``link_name`` is filled with
  the original spelling so linkage survives);
- ``--no-std`` / procedure-macro isolation is untouched (asserted elsewhere).
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

from cwind_frontend import build_typed_ast, run_sa_with_errors  # noqa: E402
from cwind_frontend import tokenize_file  # noqa: E402
from cwind_frontend.parser.parser import parse_with_errors  # noqa: E402


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for element in node:
            yield from _walk(element)


class Todo175ShadowTests(unittest.TestCase):
    def _write(self, root: Path, relative: str, text: str) -> Path:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        parts = Path(relative).parts
        if parts and parts[0] == "libs" and path.suffix in (".wind", ".wd"):
            harness.sync_mod_wind(root, path)
        return path

    def _analyze(self, root: Path, entry: Path):
        parsed = parse_with_errors(
            tokenize_file(entry), source_path=str(entry.resolve())
        )
        self.assertEqual([], [e.message for e in parsed.errors])
        result = run_sa_with_errors(parsed.program)
        self.assertEqual(
            [], [e.message for e in result.errors],
            [e.message for e in result.errors],
        )
        return parsed, result

    def test_same_extern_user_and_std_not_renamed(self):
        """User ``malloc`` and std's ``malloc`` are distinct FQNs.

        The std declaration is FQN-qualified (not hashed); the entry keeps
        its bare name; each call resolves to its own declaration; both keep
        the correct C symbol (``malloc``).
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write(
                root,
                "libs/dep.wind",
                "pub extern \"C\" { fn malloc(size: Int) -> *mut Byte; }\n"
                "pub fn alloc(n: Int) -> *mut Byte { return malloc(n); }\n",
            )
            main = self._write(
                root,
                "main.wind",
                "use std::dep::alloc;\n"
                "extern \"C\" { fn malloc(size: Int) -> *mut Byte; }\n"
                "fn main() {\n"
                "    let p: *mut Byte = malloc(16);\n"
                "    let q: *mut Byte = alloc(16);\n"
                "}\n",
            )
            parsed, result = self._analyze(root, main)
            doc = build_typed_ast(parsed.program, result.info)

            names = {sym["name"] for sym in doc["symbols"]}
            self.assertIn("malloc", names)
            self.assertIn("std::dep::malloc", names)
            self.assertFalse(
                [n for n in names if n.startswith("malloc__")],
                f"unexpected hashed rename: {names}",
            )

            decls = {
                node["name"]: node
                for node in _walk(doc["ast"])
                if node.get("kind") == "FnDecl"
                and node.get("name") in ("malloc", "std::dep::malloc")
            }
            # Extern symbols stay linkable: no link_name on the user item,
            # and the qualified std copy pins the original C spelling.
            self.assertIn("malloc", decls)
            self.assertIsNone(decls["malloc"].get("link_name"))
            self.assertEqual("malloc", decls["std::dep::malloc"].get("link_name"))

            user_id = decls["malloc"]["id"]
            std_id = decls["std::dep::malloc"]["id"]

            calls = [
                node for node in _walk(doc["ast"])
                if node.get("kind") == "Call"
                and isinstance(node.get("callee"), dict)
                and node["callee"].get("kind") == "Name"
                and node["callee"].get("parts") == ["malloc"]
            ]
            refs = {
                call["line"]: call.get("ann", {}).get("call", {}).get("callee_ref")
                for call in calls
            }
            # main.wind line 4 -> user's malloc; dep.wind line 2 -> std's.
            self.assertEqual(user_id, refs.get(4))
            self.assertEqual(std_id, refs.get(2))

    def test_same_extern_distinct_c_symbols_both_kept(self):
        """Two same-named externs with distinct ``#[link_name]``s coexist.

        A user ``gettid`` (C symbol ``gettid``) and a std ``gettid`` bound to
        another C symbol must both survive and each call must bind to the
        declaration whose C symbol it actually calls.
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write(
                root,
                "libs/thread.wind",
                "pub extern \"C\" {\n"
                "    #[link_name = \"GetCurrentThreadId\"]\n"
                "    pub fn gettid() -> Int;\n"
                "}\n"
                "pub fn current_tid() -> Int { return gettid(); }\n",
            )
            main = self._write(
                root,
                "main.wind",
                "use std::thread::current_tid;\n"
                "extern \"C\" { fn gettid() -> Int; }\n"
                "fn main() -> Int {\n"
                "    let a: Int = gettid();\n"
                "    let b: Int = current_tid();\n"
                "    return a + b;\n"
                "}\n",
            )
            parsed, result = self._analyze(root, main)
            doc = build_typed_ast(parsed.program, result.info)

            names = {sym["name"] for sym in doc["symbols"]}
            self.assertIn("gettid", names)
            self.assertIn("std::thread::gettid", names)
            self.assertFalse([n for n in names if n.startswith("gettid__")])

            decls = {
                node["name"]: node
                for node in _walk(doc["ast"])
                if node.get("kind") == "FnDecl"
                and node.get("name") in ("gettid", "std::thread::gettid")
            }
            self.assertIsNone(decls["gettid"].get("link_name"))
            self.assertEqual(
                "GetCurrentThreadId",
                decls["std::thread::gettid"].get("link_name"),
            )

    def test_non_extern_std_function_qualified_on_collision(self):
        """A plain (non-extern) std fn shadowed by a user fn gets its FQN."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write(
                root,
                "libs/dep.wind",
                "pub fn pick() -> Int { return 1; }\n"
                "pub fn via_std() -> Int { return pick(); }\n",
            )
            main = self._write(
                root,
                "main.wind",
                "use std::dep::via_std;\n"
                "fn pick() -> Int { return 42; }\n"
                "fn main() -> Int { return via_std() + pick(); }\n",
            )
            parsed, result = self._analyze(root, main)
            doc = build_typed_ast(parsed.program, result.info)

            names = {sym["name"] for sym in doc["symbols"]}
            self.assertIn("pick", names)
            self.assertIn("std::dep::pick", names)
            self.assertFalse([n for n in names if n.startswith("pick__")])

            decls = {
                node["name"]: node
                for node in _walk(doc["ast"])
                if node.get("kind") == "FnDecl"
                and node.get("name") in ("pick", "std::dep::pick")
            }
            calls = [
                node for node in _walk(doc["ast"])
                if node.get("kind") == "Call"
                and isinstance(node.get("callee"), dict)
                and node["callee"].get("kind") == "Name"
                and node["callee"].get("parts") == ["pick"]
            ]
            refs = {
                call["line"]: call.get("ann", {}).get("call", {}).get("callee_ref")
                for call in calls
            }
            # dep.wind line 2 -> std's pick; main.wind line 3 -> user's.
            self.assertEqual(decls["std::dep::pick"]["id"], refs.get(2))
            self.assertEqual(decls["pick"]["id"], refs.get(3))


if __name__ == "__main__":
    unittest.main()
