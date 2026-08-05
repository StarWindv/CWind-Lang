"""Tests for the cwindf command-line interface."""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cwind_frontend._version import __version__
from cwind_frontend.cli import main

VALID = 'const hello: String = "hi";\nfn main() -> Int { return 0; }\n'
BAD = "fn main() -> Int { let x = 1; }\n"


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


def write_source(text):
    tmp = tempfile.TemporaryDirectory()
    path = os.path.join(tmp.name, "t.wind")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return tmp, path


class TestCli(unittest.TestCase):
    def test_version_banner(self):
        code, out, _ = run(["-V"])
        self.assertEqual(code, 0)
        self.assertIn("CWind Programming Language Compiler Frontend", out)
        self.assertIn(f"Version: v{__version__}(", out)
        self.assertIn("SPDX-License-Identifier: BSD-3-Clause", out)

    def test_version_short(self):
        code, out, _ = run(["-V", "--short"])
        self.assertEqual((code, out.strip()), (0, f"v{__version__}"))

    def test_short_requires_version(self):
        code, _, err = run(["--short"])
        self.assertEqual(code, 2)
        self.assertIn("--short requires --version", err)

    def test_default_silent(self):
        tmp, path = write_source(VALID)
        try:
            code, out, err = run([path])
        finally:
            tmp.cleanup()
        self.assertEqual(code, 0)
        self.assertEqual(out, "")

    def test_lex(self):
        tmp, path = write_source(VALID)
        try:
            code, out, _ = run(["--lex", path])
        finally:
            tmp.cleanup()
        self.assertEqual(code, 0)
        self.assertIn("CONST", out)
        self.assertIn('"hi"', out)

    def test_parse(self):
        tmp, path = write_source(VALID)
        try:
            code, out, _ = run(["--parse", path])
        finally:
            tmp.cleanup()
        self.assertEqual(code, 0)
        self.assertIn("Program", out)
        self.assertIn("FnDecl", out)

    def test_sa(self):
        tmp, path = write_source(VALID)
        try:
            code, out, _ = run(["--sa", path])
        finally:
            tmp.cleanup()
        self.assertEqual(code, 0)
        self.assertIn("Semantic analysis passed: 2 top-level symbols", out)
        self.assertIn("  fn     main", out)

    def test_verbose(self):
        tmp, path = write_source(VALID)
        try:
            code, out, _ = run(["--verbose", path])
        finally:
            tmp.cleanup()
        self.assertEqual(code, 0)
        self.assertIn("=== lexer output ===", out)
        self.assertIn("=== parser AST ===", out)

    def test_json_lex(self):
        tmp, path = write_source(VALID)
        try:
            code, out, _ = run(["--lex", "--json", path])
        finally:
            tmp.cleanup()
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data[0]["kind"], "CONST")

    def test_json_parse(self):
        tmp, path = write_source(VALID)
        try:
            code, out, _ = run(["--parse", "--json", path])
        finally:
            tmp.cleanup()
        self.assertEqual(json.loads(out)["kind"], "Program")

    def test_json_sa(self):
        tmp, path = write_source(VALID)
        try:
            code, out, _ = run(["--sa", "--json", path])
        finally:
            tmp.cleanup()
        self.assertIn("symbols", json.loads(out))

    def test_json_verbose(self):
        tmp, path = write_source(VALID)
        try:
            code, out, _ = run(["--verbose", "--json", path])
        finally:
            tmp.cleanup()
        data = json.loads(out)
        self.assertIn("tokens", data)
        self.assertEqual(data["ast"]["kind"], "Program")

    def test_json_requires_stage(self):
        tmp, path = write_source(VALID)
        try:
            code, _, err = run(["--json", path])
        finally:
            tmp.cleanup()
        self.assertEqual(code, 2)
        self.assertIn("--json requires", err)

    def test_error_rendered(self):
        tmp, path = write_source(BAD)
        try:
            code, _, err = run([path])
        finally:
            tmp.cleanup()
        self.assertEqual(code, 1)
        self.assertIn("let needs a type", err)


if __name__ == "__main__":
    unittest.main()
