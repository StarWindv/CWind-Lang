"""todo-86/93: ``#[cfg(...)]`` attributes with all/any/not combinators.

Data-driven pipeline cases live in ``cases/cfg`` (host-independent);
target-dependent behaviour is asserted here by pinning ``--target-os``
through :func:`parse_with_errors`, so every test runs on any host.
"""

import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

import harness

from cwind_frontend import (
    CFG_COMBINATORS,
    CFG_FLAGS,
    CFG_KEYS,
    OS_NAMES,
    CfgContext,
    CfgPredicate,
    ExternBlock,
    ExternStatic,
    FnDecl,
    UseDecl,
    detect_target_os,
    evaluate_cfg,
    lex_with_errors,
    parse_with_errors,
)
from cwind_frontend.cli import main as cli_main

CFG = "cfg"

PLATFORM_FN = '''
#[cfg(target_os = "windows")]
fn platform() -> Int { return 1; }

#[cfg(target_os = "linux")]
fn platform() -> Int { return 2; }

#[cfg(target_os = "macos")]
fn platform() -> Int { return 3; }

#[cfg(target_os = "android")]
fn platform() -> Int { return 7; }

#[cfg(all(unix, not(macos)))]
fn gate_all() -> Int { return 4; }

#[cfg(any(windows, macos))]
fn gate_any() -> Int { return 5; }

#[cfg(not(windows))]
fn gate_not() -> Int { return 6; }

fn main() -> Int {
    let p: Int = platform();
    return p;
}
'''

# Expected surviving fn names per pinned target.
EXPECTED_FNS = {
    "windows": {"platform", "gate_any"},
    "linux": {"platform", "gate_all", "gate_not"},
    "macos": {"platform", "gate_any", "gate_not"},
    # android keeps the linux bare flag, so all(unix, not(macos)) holds.
    "android": {"platform", "gate_all", "gate_not"},
}
EXPECTED_ALL = {t: fns | {"main"} for t, fns in EXPECTED_FNS.items()}


def _parse(text, target=None, source_path=None):
    lexed = lex_with_errors(text)
    assert not lexed.errors, lexed.errors
    result = parse_with_errors(lexed.tokens, source_path=source_path, target_os=target)
    return result


def _fn_names(program):
    return {
        item.name
        for item in program.items
        if isinstance(item, FnDecl)
    }


class TestCfgEvaluation(unittest.TestCase):
    """Direct evaluation semantics of the predicate tree."""

    def _eval(self, pred, target):
        return evaluate_cfg(pred, CfgContext(target))

    def test_flag_families(self):
        self.assertTrue(self._eval(CfgPredicate("flag", name="windows"), "windows"))
        self.assertFalse(self._eval(CfgPredicate("flag", name="windows"), "linux"))
        for unix_like in ("linux", "macos"):
            self.assertTrue(self._eval(CfgPredicate("flag", name="unix"), unix_like))
        self.assertFalse(self._eval(CfgPredicate("flag", name="unix"), "windows"))

    def test_kv(self):
        pred = CfgPredicate("kv", name="target_os", value="linux")
        self.assertTrue(self._eval(pred, "linux"))
        self.assertFalse(self._eval(pred, "windows"))

    def test_android_counts_as_linux_but_is_distinct(self):
        ctx = CfgContext("android")
        # Bare flags: android implies unix AND linux.
        for flag in ("unix", "linux", "android"):
            self.assertTrue(evaluate_cfg(CfgPredicate("flag", name=flag), ctx))
        self.assertFalse(evaluate_cfg(CfgPredicate("flag", name="macos"), ctx))
        # kv target_os still tells android and linux apart.
        self.assertTrue(self._eval(
            CfgPredicate("kv", name="target_os", value="android"), "android"))
        self.assertFalse(self._eval(
            CfgPredicate("kv", name="target_os", value="linux"), "android"))
        # And the linux target does not hold the android flag.
        lin = CfgContext("linux")
        self.assertFalse(evaluate_cfg(CfgPredicate("flag", name="android"), lin))

    def test_combinators(self):
        ctx = CfgContext("linux")
        win = CfgPredicate("flag", name="windows")
        lin = CfgPredicate("flag", name="linux")
        mac = CfgPredicate("flag", name="macos")
        self.assertTrue(evaluate_cfg(
            CfgPredicate("all", args=(lin, CfgPredicate("not", args=(mac,)))), ctx))
        self.assertFalse(evaluate_cfg(
            CfgPredicate("any", args=(win, mac)), ctx))
        # Rust semantics for the empty combinators.
        self.assertTrue(evaluate_cfg(CfgPredicate("all", args=()), ctx))
        self.assertFalse(evaluate_cfg(CfgPredicate("any", args=()), ctx))

    def test_detect_returns_known_os(self):
        detected = detect_target_os()
        if detected != "unix":  # unknown hosts fall back to a unix-ish flag set
            self.assertIn(detected, OS_NAMES)


class TestCfgItemGating(unittest.TestCase):
    def test_platform_fns_per_target(self):
        for target in OS_NAMES:
            with self.subTest(target=target):
                result = _parse(PLATFORM_FN, target=target)
                self.assertEqual(result.errors, [])
                self.assertEqual(_fn_names(result.program), EXPECTED_ALL[target])

    def test_multiple_cfg_attrs_are_and(self):
        src = (
            '#[cfg(windows)]\n'
            '#[cfg(not(macos))]\n'
            'fn both() -> Int { return 1; }\n'
            'fn main() -> Int { return 0; }\n'
        )
        kept = _parse(src, target="windows")
        dropped = _parse(src, target="linux")
        self.assertEqual(_fn_names(kept.program), {"both", "main"})
        self.assertEqual(_fn_names(dropped.program), {"main"})

    def test_trailing_comma_in_combinator(self):
        result = _parse('#[cfg(all(linux,))]\nfn f() -> Int { return 0; }\n',
                        target="linux")
        self.assertEqual(result.errors, [])
        self.assertIn("f", _fn_names(result.program))

    def test_double_comma_is_error(self):
        result = _parse('#[cfg(all(linux,,))]\nfn f() -> Int { return 0; }\n')
        self.assertEqual(len(result.errors), 1)

    def test_invalid_target_os_value_rejected(self):
        with self.assertRaises(ValueError):
            _parse('fn main() -> Int { return 0; }\n', target="freebsd")

    def test_default_target_matches_host(self):
        result = _parse(PLATFORM_FN)
        self.assertEqual(result.errors, [])
        names = _fn_names(result.program)
        host = detect_target_os()
        if host == "unix":
            # Pseudo-target: every platform() variant drops, unix gates hold.
            self.assertIn("gate_all", names)
            self.assertIn("gate_not", names)
        else:
            self.assertEqual(names, EXPECTED_ALL[host])


class TestCfgExternItems(unittest.TestCase):
    EXTERN_SRC = '''
extern "C" {
    #[cfg(target_os = "windows")]
    fn win_only(x: Int32) -> Int32;

    #[cfg(target_os = "linux")]
    fn lin_only(x: Int32) -> Int32;

    static LIMIT: Int32;

    #[cfg(windows)]
    static mut WIN_LIMIT: Int32;
}

fn main() -> Int { return 0; }
'''

    def _extern(self, target):
        result = _parse(self.EXTERN_SRC, target=target)
        self.assertEqual(result.errors, [])
        blocks = [
            i for i in result.program.items if isinstance(i, ExternBlock)
        ]
        self.assertEqual(len(blocks), 1)
        return blocks[0]

    def test_inner_fn_gated(self):
        win = self._extern("windows")
        linux = self._extern("linux")
        self.assertEqual(
            sorted(fn.name for fn in win.fns), ["win_only"],
        )
        self.assertEqual(
            sorted(fn.name for fn in linux.fns), ["lin_only"],
        )

    def test_inner_static_gated(self):
        win = self._extern("windows")
        linux = self._extern("linux")
        self.assertEqual([s.name for s in win.statics], ["LIMIT", "WIN_LIMIT"])
        self.assertEqual([s.name for s in linux.statics], ["LIMIT"])
        win_limit = next(s for s in win.statics if isinstance(s, ExternStatic)
                         and s.name == "WIN_LIMIT")
        self.assertTrue(win_limit.mutable)


class TestCfgUse(unittest.TestCase):
    def test_gated_use_skips_resolution(self):
        # The module does not exist; on targets where cfg drops the import
        # resolution must never run.
        src = (
            '#[cfg(target_os = "windows")]\n'
            'use no_such_module::thing;\n'
            'fn main() -> Int { return 0; }\n'
        )
        dropped = _parse(src, target="linux")
        self.assertEqual(dropped.errors, [])
        uses = [i for i in dropped.program.items if isinstance(i, UseDecl)]
        self.assertEqual(uses, [])

        kept = _parse(src, target="windows")
        self.assertEqual(len(kept.errors), 1)
        self.assertIn("no_such_module", kept.errors[0].message)

    def test_use_with_cfg_keeps_import_on_match(self):
        src = (
            '#[cfg(any(windows, unix))]\n'
            'use std::option;\n'
            'fn main() -> Int { return 0; }\n'
        )
        result = _parse(src, target="linux", source_path="t.wind")
        uses = [
            tuple(i.parts) for i in result.program.items if isinstance(i, UseDecl)
        ]
        self.assertIn(("std", "option"), uses)


class TestCfgCli(unittest.TestCase):
    """``cwindf --target-os ...`` pins the cfg target end-to-end."""

    def _run(self, argv):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli_main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_target_os_switches_output(self):
        import json
        import os
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        path = os.path.join(tmp.name, "t.wind")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(PLATFORM_FN)
        try:
            outputs = {}
            for target in ("windows", "linux", "android"):
                code, out, err = self._run(["--typed-ast", "--target-os", target, path])
                self.assertEqual(code, 0, err)
                data = json.loads(out)
                names = {
                    item.get("name")
                    for item in data["ast"]["items"]
                    if item.get("kind") == "FnDecl"
                }
                outputs[target] = names
            self.assertEqual(outputs["windows"], EXPECTED_ALL["windows"])
            self.assertEqual(outputs["linux"], EXPECTED_ALL["linux"])
            self.assertEqual(outputs["android"], EXPECTED_ALL["android"])
        finally:
            tmp.cleanup()

    def test_target_os_rejects_unknown(self):
        with self.assertRaises(SystemExit) as ctx:
            self._run(["--target-os", "freebsd"])
        self.assertEqual(ctx.exception.code, 2)


class TestCfgDataDriven(harness.CaseAssertionsMixin, unittest.TestCase):
    """Host-independent cases under ``cases/cfg``."""

    def test_cases(self):
        for name in (
            "cfg_clean",
            "cfg_unknown_flag",
            "cfg_unknown_key",
            "cfg_bad_os_value",
            "cfg_bad_combinator",
            "cfg_not_arity",
            "cfg_missing_paren",
            "cfg_attr_on_use",
        ):
            with self.subTest(case=name):
                self.assert_case(CFG, name)


if __name__ == "__main__":
    unittest.main()
