"""todo-81: module-qualified enum variant paths.

``use opt::*`` introduces the module namespace, not each type as a bare
namespace.  ``opt::Opt::Some(...)`` therefore has three source segments:
the first resolves through the module surface, while the final pair must
resolve to an exported enum and its variant.  The same path form is also
accepted in match patterns.

Contract under test:

- expression unit variants and payload constructor calls both resolve;
- every resolved three-segment path (expression, call callee, pattern)
  is normalized to the canonical two-segment ``Enum::Variant`` form the
  backend consumes; the alias survives only as ``ann.module`` provenance;
- diagnostics distinguish unknown module member vs private type vs
  unknown variant vs payload misuse, and an unknown first segment is
  never silently normalized away.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path


TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parents[2]
for path in (ROOT / "mvp/frontend/src", ROOT / "mvp/frontend/tests"):
    sys.path.insert(0, str(path))

from cwind_frontend import run_sa_with_errors  # noqa: E402
from cwind_frontend.parser.parser import parse_with_errors  # noqa: E402
from cwind_frontend import tokenize_file  # noqa: E402

_MODULE = (
    "pub enum Color { Red, Blue }\n"
    "pub enum Opt<T> { None, Some(T) }\n"
    "pub enum Shape { Dot(Int), Seg(Int, Int) }\n"
    "pub struct Point {\n"
    "    x: Int,\n"
    "    pub y: Int,\n"
    "}\n"
    "enum Secret { Hidden(Int) }\n"
)

_MAIN_PREFIX = (
    "use colors;\n"
    "fn main() -> Int {\n"
    "    let c: Color = colors::Color::Blue;\n"
    "    let o: Opt<Int> = colors::Opt::Some(7);\n"
    "    let n: Int = match (o) {\n"
    "        colors::Opt::None => 0,\n"
    "        colors::Opt::Some(v) => v,\n"
    "    };\n"
    "    if (c == colors::Color::Blue) { return n; }\n"
    "    return 0;\n"
)


def iter_nodes(node):
    if not hasattr(node, "__dataclass_fields__"):
        return
    yield node
    for field in node.__dataclass_fields__:
        value = getattr(node, field, None)
        if hasattr(value, "__dataclass_fields__"):
            yield from iter_nodes(value)
        elif isinstance(value, (list, tuple)):
            for item in value:
                yield from iter_nodes(item)


class Todo81QualifiedVariantTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def parse_main(
        self,
        main: str,
        module: str = _MODULE,
    ):
        self.write("libs/colors.wind", module)
        entry = self.write("main.wind", main)
        parsed = parse_with_errors(
            tokenize_file(entry), source_path=str(entry.resolve())
        )
        self.assertEqual([], [e.message for e in parsed.errors])
        return parsed

    def write(self, relative: str, text: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if not text:
            return path
        path.write_text(text, encoding="utf-8")
        return path

    def errors(self, parsed) -> list[str]:
        result = run_sa_with_errors(parsed.program)
        return [error.message for error in result.errors]

    def nodes(self, parsed, typename: str) -> list:
        return [
            node
            for item in parsed.program.items
            for node in iter_nodes(item)
            if type(node).__name__ == typename
        ]

    # ------------------------------------------------------------------
    # Happy paths
    # ------------------------------------------------------------------

    def test_constructor_and_exhaustive_qualified_match(self):
        parsed = self.parse_main(_MAIN_PREFIX + "}\n")
        self.assertEqual([], self.errors(parsed))
        # Normalization: no three-segment path survives SA anywhere.
        self.assertEqual([], [
            n for n in self.nodes(parsed, "Name") if len(n.parts) == 3
        ])
        self.assertEqual([], [
            p for p in self.nodes(parsed, "EnumPattern") if len(p.path) == 3
        ])
        some = next(
            c for c in self.nodes(parsed, "Call")
            if c.callee.parts == ["Opt", "Some"]
        )
        self.assertEqual(
            "enum_variant", some._typed_ann["call"]["callee_kind"]
        )
        self.assertEqual("Opt", some._typed_ann["enum"])
        self.assertEqual(1, some._typed_ann["variant_index"])

    def test_unit_variant_name_is_normalized_with_provenance(self):
        parsed = self.parse_main(_MAIN_PREFIX + "}\n")
        self.assertEqual([], self.errors(parsed))
        variants = [
            n for n in self.nodes(parsed, "Name")
            if n._typed_ann.get("binding", {}).get("kind") == "variant"
            and n.parts == ["Color", "Blue"]
        ]
        self.assertTrue(variants)
        target = variants[0]
        self.assertEqual(1, target._typed_ann["variant_index"])
        self.assertEqual(["colors"], target._typed_ann["module"]["path"])
        self.assertEqual("Color", target._typed_ann["type"]["name"])

    def test_payload_call_keeps_payload_type_annotation(self):
        parsed = self.parse_main(_MAIN_PREFIX + "}\n")
        self.assertEqual([], self.errors(parsed))
        calls = [
            c for c in self.nodes(parsed, "Call")
            if c.callee.parts == ["Opt", "Some"]
            and c._typed_ann.get("call", {}).get("callee_kind") == "enum_variant"
        ]
        self.assertTrue(calls)
        call = calls[0]
        self.assertEqual("Int", call._typed_ann["payload_types"][0]["name"])
        self.assertEqual("Opt", call._typed_ann["type"]["name"])
        self.assertEqual(
            "Int", call._typed_ann["type"]["args"][0]["name"]
        )
        self.assertEqual(["colors"], call.callee._typed_ann["module"]["path"])

    def test_mixed_bare_and_qualified_patterns_stay_exhaustive(self):
        parsed = self.parse_main(
            "use colors;\n"
            "fn main() -> Int {\n"
            "    let o: Opt<Int> = Opt::Some(3);\n"
            "    let n: Int = match (o) {\n"
            "        Opt::None => 0,\n"
            "        colors::Opt::Some(v) => v,\n"
            "    };\n"
            "    return n;\n"
            "}\n"
        )
        self.assertEqual([], self.errors(parsed))

    def test_all_qualified_exhaustive_match_without_wildcard(self):
        parsed = self.parse_main(_MAIN_PREFIX + "}\n")
        self.assertEqual([], self.errors(parsed))

    def test_if_let_accepts_qualified_pattern(self):
        parsed = self.parse_main(
            "use colors;\n"
            "fn main() -> Int {\n"
            "    let o: Opt<Int> = colors::Opt::None;\n"
            "    if let colors::Opt::Some(v) = o {\n"
            "        return v;\n"
            "    }\n"
            "    return 0;\n"
            "}\n"
        )
        self.assertEqual([], self.errors(parsed))

    def test_generic_inference_through_qualified_constructor(self):
        parsed = self.parse_main(
            "use colors;\n"
            "fn take(o: Opt<String>) -> Int {\n"
            "    return match (o) {\n"
            "        colors::Opt::None => 0,\n"
            "        colors::Opt::Some(v) => v.length(),\n"
            "    };\n"
            "}\n"
            "fn main() -> Int {\n"
            "    return take(colors::Opt::Some(\"hi\"));\n"
            "}\n"
        )
        self.assertEqual([], self.errors(parsed))

    def test_multi_payload_qualified_constructor(self):
        parsed = self.parse_main(
            "use colors;\n"
            "fn main() -> Int {\n"
            "    let s: Shape = colors::Shape::Seg(2, 3);\n"
            "    let d: Shape = colors::Shape::Dot(4);\n"
            "    let m: Int = match (d) {\n"
            "        colors::Shape::Dot(a) => a,\n"
            "        colors::Shape::Seg(a, b) => a + b,\n"
            "    };\n"
            "    let m2: Int = match (s) {\n"
            "        colors::Shape::Dot(a) => a,\n"
            "        colors::Shape::Seg(a, b) => a * b,\n"
            "    };\n"
            "    return m + m2;\n"
            "}\n"
        )
        self.assertEqual([], self.errors(parsed))

    def test_real_std_option_module_qualified_use(self):
        libs = ROOT / "libs"
        for name in ("option.wind", "panic.wind"):
            shutil.copyfile(libs / name, self.write(f"libs/{name}", ""))
        parsed = self.parse_main(
            "use std::option;\n"
            "fn main() -> Int {\n"
            "    let o: Option<Int> = option::Option::Some(5);\n"
            "    let n: Int = match (o) {\n"
            "        option::Option::None => 0,\n"
            "        option::Option::Some(v) => v,\n"
            "    };\n"
            "    return n;\n"
            "}\n"
        )
        self.assertEqual([], self.errors(parsed))

    def test_local_variable_named_like_module_does_not_shadow_path(self):
        parsed = self.parse_main(
            _MAIN_PREFIX
            + "    let colors: Int = 9;\n"
            "    let d: Color = colors::Color::Red;\n"
            "    return n + 1;\n"
            "}\n"
        )
        self.assertEqual([], self.errors(parsed))

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def test_private_enum_is_not_exported_through_module_alias(self):
        parsed = self.parse_main(
            _MAIN_PREFIX + "    let s: Int = colors::Secret::Hidden(1);\n}\n"
        )
        messages = "\n".join(self.errors(parsed))
        self.assertIn("private in module 'colors'", messages)

    def test_unknown_enum_and_variant_are_diagnosed_precisely(self):
        parsed = self.parse_main(
            _MAIN_PREFIX + "    let x: Int = colors::Missing::Red;\n"
            "    let y: Int = colors::Color::Green;\n"
            "    return 0;\n}\n"
        )
        messages = "\n".join(self.errors(parsed))
        self.assertIn("has no enum 'Missing'", messages)
        self.assertIn("has no variant 'Green'", messages)

    def test_unknown_enum_in_match_pattern_is_diagnosed(self):
        parsed = self.parse_main(
            "use colors;\n"
            "fn main() -> Int {\n"
            "    let c: Color = Color::Blue;\n"
            "    let n: Int = match (c) {\n"
            "        colors::Missing::Red => 1,\n"
            "        _ => 0,\n"
            "    };\n"
            "    return n;\n"
            "}\n"
        )
        messages = "\n".join(self.errors(parsed))
        self.assertTrue(
            any("Missing" in m for m in messages.splitlines()), messages
        )

    def test_unknown_module_prefix_is_never_normalized_in_expression(self):
        parsed = self.parse_main(
            _MAIN_PREFIX + "    let x: Int = nope::Color::Red;\n"
            "    return 0;\n}\n"
        )
        messages = "\n".join(self.errors(parsed))
        self.assertTrue(
            any("nope" in m for m in messages.splitlines()), messages
        )

    def test_unknown_module_prefix_is_rejected_in_match_pattern(self):
        parsed = self.parse_main(
            "use colors;\n"
            "fn main() -> Int {\n"
            "    let c: Color = Color::Blue;\n"
            "    let n: Int = match (c) {\n"
            "        nope::Color::Red => 1,\n"
            "        _ => 0,\n"
            "    };\n"
            "    return n;\n"
            "}\n"
        )
        messages = "\n".join(self.errors(parsed))
        self.assertIn("does not belong to enum 'Color'", messages)

    def test_wrong_module_alias_in_match_pattern_is_rejected(self):
        parsed = self.parse_main(
            "use colors;\n"
            "fn main() -> Int {\n"
            "    let c: Color = Color::Blue;\n"
            "    let n: Int = match (c) {\n"
            "        colors::Opt::None => 1,\n"
            "        _ => 0,\n"
            "    };\n"
            "    return n;\n"
            "}\n"
        )
        messages = "\n".join(self.errors(parsed))
        self.assertIn("does not belong to enum 'Color'", messages)

    def test_payload_variant_requires_construction_arguments(self):
        parsed = self.parse_main(
            _MAIN_PREFIX
            + "    let z: Opt<Int> = colors::Opt::Some;\n    return 0;\n}\n"
        )
        messages = "\n".join(self.errors(parsed))
        self.assertIn("carries a payload", messages)

    def test_unit_variant_called_with_arguments_is_rejected(self):
        parsed = self.parse_main(
            _MAIN_PREFIX + "    let z: Color = colors::Color::Blue(1);\n"
            "    return 0;\n}\n"
        )
        messages = "\n".join(self.errors(parsed))
        self.assertIn("takes no payload", messages)

    def test_wrong_payload_count_is_rejected(self):
        parsed = self.parse_main(
            _MAIN_PREFIX + "    let z: Opt<Int> = colors::Opt::Some();\n"
            "    return 0;\n}\n"
        )
        self.assertTrue(any(
            "expects 1 payload value(s)" in message
            for message in self.errors(parsed)
        ))

    def test_multi_payload_count_mismatch_is_rejected(self):
        parsed = self.parse_main(
            "use colors;\n"
            "fn main() -> Int {\n"
            "    let s: Shape = colors::Shape::Seg(1);\n"
            "    return 0;\n"
            "}\n"
        )
        messages = "\n".join(self.errors(parsed))
        self.assertIn("expects 2 payload value(s), got 1", messages)

    def test_non_generic_payload_type_mismatch_is_rejected(self):
        parsed = self.parse_main(
            "use colors;\n"
            "fn main() -> Int {\n"
            "    let s: Shape = colors::Shape::Dot(\"x\");\n"
            "    return 0;\n"
            "}\n"
        )
        messages = "\n".join(self.errors(parsed))
        self.assertIn("payload 1 of variant 'Dot' must be Int", messages)

    def test_generic_binding_contract_still_applies(self):
        # Inference makes Some("x") an Opt<String>; binding it to Opt<Int>
        # must fail at the initialization site.
        parsed = self.parse_main(
            "use colors;\n"
            "fn main() -> Int {\n"
            "    let o: Opt<Int> = colors::Opt::Some(\"x\");\n"
            "    return 0;\n"
            "}\n"
        )
        messages = "\n".join(self.errors(parsed))
        self.assertIn("Opt<String>", messages)

    def test_non_enum_middle_segment_reports_missing_enum(self):
        parsed = self.parse_main(
            _MAIN_PREFIX + "    let p: Point = colors::Point::new();\n"
            "    return 0;\n}\n"
        )
        messages = "\n".join(self.errors(parsed))
        self.assertIn("has no enum 'Point'", messages)

    def test_qualified_path_needs_an_alias_declared_by_this_file(self):
        # Transitive compile-dependency closure (`other` privately uses
        # colors) does NOT register the `colors` alias for main: a
        # three-segment path may only resolve through an alias the
        # importing file itself declared, and otherwise fails loudly
        # instead of silently resolving through whatever got flattened.
        self.write("libs/colors.wind", _MODULE)
        self.write(
            "libs/other.wind",
            "use colors;\n"
            "pub fn paint() -> Int {\n"
            "    let c: Color = Color::Red;\n"
            "    return match (c) {\n"
            "        Color::Red => 1,\n"
            "        _ => 0,\n"
            "    };\n"
            "}\n",
        )
        entry = self.write(
            "main.wind",
            "use other;\n"
            "fn main() -> Int {\n"
            "    let n: Int = other::paint();\n"
            "    colors::Color::Red;\n"
            "    return n;\n"
            "}\n",
        )
        parsed = parse_with_errors(
            tokenize_file(entry), source_path=str(entry.resolve())
        )
        self.assertEqual([], [e.message for e in parsed.errors])
        messages = "\n".join(self.errors(parsed))
        self.assertIn("unknown type 'colors' in path", messages)


if __name__ == "__main__":
    unittest.main()
