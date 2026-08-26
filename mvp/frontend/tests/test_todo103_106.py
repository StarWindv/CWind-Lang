"""todo-103/106 regression: ``#[cfg(target_arch = "...")]`` and
``#[cfg(target_vendor = "...")]`` / ``target_pointer_width``.

Values are validated at parse time against fixed tables; evaluation
pins through ``parse_with_errors`` keyword arguments, and the CLI
gains matching ``--target-arch/--target-vendor/--target-pointer-width``
flags.  Host-independent pipeline cases live in ``cases/todo103_106``.
"""

import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

import harness  # noqa: E402

from cwind_frontend import (  # noqa: E402
    FnDecl,
    lex_with_errors,
    parse_with_errors,
)
from cwind_frontend.cli import main as cli_main  # noqa: E402

T103 = "todo103_106"


def _parse(text, **target):
    lexed = lex_with_errors(text)
    assert not lexed.errors, lexed.errors
    return parse_with_errors(lexed.tokens, **target)


def _fn_names(program):
    return {i.name for i in program.items if isinstance(i, FnDecl)}


ARCH_SRC = (
    '#[cfg(target_arch = "x86_64")]\n'
    "fn word() -> Int { return 8; }\n"
    '#[cfg(target_arch = "x86")]\n'
    "fn word() -> Int { return 4; }\n"
    '#[cfg(target_arch = "aarch64")]\n'
    "fn word() -> Int { return 6; }\n"
    "fn main() -> Int { return 0; }\n"
)


class TestTargetArch(unittest.TestCase):
    def test_exact_match_keeps_one_variant(self):
        result = _parse(ARCH_SRC, target_os="linux", target_arch="x86_64")
        self.assertEqual(result.errors, [])
        self.assertEqual(_fn_names(result.program), {"word", "main"})

    def test_no_match_drops_everything(self):
        result = _parse(ARCH_SRC, target_os="linux", target_arch="riscv64")
        self.assertEqual(result.errors, [])
        self.assertEqual(_fn_names(result.program), {"main"})

    def test_invalid_value_is_parse_error(self):
        bad = '#[cfg(target_arch = "z80")]\nfn f() -> Int { return 0; }\n'
        result = _parse(bad)
        self.assertEqual(len(result.errors), 1)
        self.assertIn("invalid 'target_arch' value 'z80'", result.errors[0].message)

    def test_unknown_context_value_never_matches(self):
        # No pinned arch + unrecognized host arch -> kv simply false.
        from unittest import mock

        src = '#[cfg(target_arch = "x86_64")]\nfn w() -> Int { return 1; }\n'
        with mock.patch(
            "cwind_frontend.cfg.detect_target_arch", lambda: None
        ):
            result = _parse(src)
        self.assertEqual(_fn_names(result.program), set())


class TestTargetVendorAndPointerWidth(unittest.TestCase):
    VENDOR_SRC = (
        '#[cfg(target_vendor = "apple")]\n'
        "fn vendor() -> Int { return 1; }\n"
        '#[cfg(target_vendor = "pc")]\n'
        "fn vendor() -> Int { return 2; }\n"
        '#[cfg(all(not(target_vendor = "apple"),'
        ' not(target_vendor = "pc")))]\n'
        "fn vendor() -> Int { return 3; }\n"
        "fn main() -> Int { return 0; }\n"
    )

    def _vendor_names(self, **kw):
        result = _parse(self.VENDOR_SRC, **kw)
        self.assertEqual(result.errors, [])
        names = set()
        for item in result.program.items:
            if isinstance(item, FnDecl):
                names.add(item.name)
        return names

    def test_vendor_gating(self):
        import cwind_frontend.cfg as cfg

        self.assertEqual(self._vendor_names(target_os="macos"),
                         {"vendor", "main"})
        # Pinned context overrides detection.
        ctx = cfg.CfgContext("windows")
        self.assertEqual(ctx.target_vendor, "pc")
        apple = evaluate_predicate("target_vendor", "apple", ctx)
        pc = evaluate_predicate("target_vendor", "pc", ctx)
        self.assertFalse(apple)
        self.assertTrue(pc)

    def test_pointer_width_gating(self):
        src = (
            '#[cfg(all(target_pointer_width = "64", windows))]\n'
            "const P: Int = 1;\n"
            '#[cfg(all(target_pointer_width = "32", unix))]\n'
            "const P: Int = 2;\n"
        )
        win64 = _parse(src, target_os="windows",
                       target_pointer_width="64").program.items
        kept64 = [i for i in win64 if getattr(i, "name", "") == "P"]
        self.assertEqual([getattr(c.value, "value") for c in kept64], [1])
        win32 = _parse(src, target_os="windows",
                       target_pointer_width="32").program.items
        self.assertEqual(len(win32), 0)


def evaluate_predicate(name, value, ctx):
    from cwind_frontend import CfgPredicate, evaluate_cfg

    return evaluate_cfg(CfgPredicate("kv", name=name, value=value), ctx)


class Todo103DataDriven(harness.CaseAssertionsMixin, unittest.TestCase):
    """Host-independent cases under ``cases/todo103_106``."""

    def test_cases(self):
        for name in ("bad_arch_value",):
            with self.subTest(case=name):
                self.assert_case(T103, name)


class TestCfgCli(unittest.TestCase):
    """New ``--target-arch/...`` flags reach cfg evaluation end-to-end."""

    def _run(self, argv):
        import io
        from contextlib import redirect_stderr, redirect_stdout

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli_main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_target_arch_flag_switches_output(self):
        import json
        import os
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        path = os.path.join(tmp.name, "t.wind")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(ARCH_SRC)
        try:
            survivor = {}
            for arch in ("x86_64", "x86"):
                code, out, err = self._run(
                    ["--typed-ast", "--target-arch", arch, path]
                )
                self.assertEqual(code, 0, err)
                data = json.loads(out)
                words = [
                    item for item in data["ast"]["items"]
                    if item.get("kind") == "FnDecl"
                    and item.get("name") == "word"
                ]
                self.assertEqual(len(words), 1)
                ret = (
                    words[0]["body"]["stmts"][0]
                    ["value"]["value"]
                )
                survivor[arch] = ret
            self.assertEqual(survivor["x86_64"], 8)
            self.assertEqual(survivor["x86"], 4)
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
