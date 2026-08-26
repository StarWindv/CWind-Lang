"""Regression tests for bug-31: duplicate built-in trait implementations.

``builtin_methods.toml`` is the single source of truth for the trait
instantiations a built-in type ships with (``Display`` on every scalar,
``From<String>``/``Into<String>`` on numerics, ``Iterable`` on containers,
...).  Re-implementing one of those instantiations used to be silently
accepted; it must be rejected the same way Rust rejects conflicting impls
(E0119), independently of any orphan-rule decisions (todo-92).

Extending a built-in type with a *new* instantiation (e.g.
``impl Into<UInt> for Int``) or implementing a *user* trait for it stays
legal.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

import harness  # noqa: E402


class Bug31DuplicateBuiltinImplTests(harness.CaseAssertionsMixin):
    def test_display_for_int_is_rejected(self):
        """原始 bug-31 复现: Int 已内置 Display, 再实现即冲突."""
        self.assert_source(
            "impl Display for Int {\n"
            "    pub fn to_string(self) -> String { return \"1\"; }\n"
            "}\n"
            "fn main() {}\n",
            {
                "errors": [
                    {"contains_all": [
                        "duplicate implementation of built-in trait",
                        "'Display'",
                        "'Int'",
                    ]},
                ],
            },
        )

    def test_from_string_for_numeric_is_rejected(self):
        self.assert_source(
            "impl From<String> for UInt64 {\n"
            "    pub fn from(value: String) -> UInt64 { return 0; }\n"
            "}\n"
            "fn main() {}\n",
            {
                "errors": [{"contains": "'From<String>'"}],
            },
        )

    def test_into_string_for_byte_is_rejected(self):
        self.assert_source(
            "impl Into<String> for Byte {\n"
            "    pub fn into(self) -> String { return \"b\"; }\n"
            "}\n"
            "fn main() {}\n",
            {
                "errors": [{"contains": "'Into<String>'"}],
            },
        )

    def test_display_for_generic_container_base_is_rejected(self):
        self.assert_source(
            "impl Display for Vector<Int> {\n"
            "    pub fn to_string(self) -> String { return \"v\"; }\n"
            "}\n"
            "fn main() {}\n",
            {
                "errors": [{"contains_all": ["'Display'", "'Vector<Int>'"]}],
            },
        )

    def test_each_conflicting_impl_is_reported(self):
        src = (
            "impl Display for Int {\n"
            "    pub fn to_string(self) -> String { return \"1\"; }\n"
            "}\n"
            "impl Display for Bool {\n"
            "    pub fn to_string(self) -> String { return \"t\"; }\n"
            "}\n"
            "fn main() {}\n"
        )
        result = harness.run_pipeline(src)
        duplicates = [
            e for e in result["errors"]
            if "duplicate implementation of built-in trait" in e.message
        ]
        self.assertEqual(2, len(duplicates))

    # ------------------------------------------------------------------
    # Legal forms that must keep working
    # ------------------------------------------------------------------

    def test_new_instantiation_of_directional_trait_stays_legal(self):
        """Int 只内置 From<String>/Into<String>, 新目标类型允许扩展."""
        self.assert_source(
            "struct Meters { v: Int }\n"
            "impl Into<Meters> for Int {\n"
            "    pub fn into(self) -> Meters {\n"
            "        return Meters { self };\n"
            "    }\n"
            "}\n"
            "fn main() {}\n",
            {},
        )

    def test_user_trait_for_builtin_type_stays_legal(self):
        """用户 trait 实现到内置类型是既有合法用法 (cases/sa 同款)."""
        self.assert_source(
            "pub trait DisplayJson { fn str(self) -> String; }\n"
            "impl DisplayJson for Map<String, String> {\n"
            "    fn str(self) -> String { return \"\".format(); }\n"
            "}\n"
            "fn f(m: Map<String, String>) -> String { return m.str(); }\n"
            "fn main() {}\n",
            {},
        )

    def test_builtin_trait_for_user_struct_stays_legal(self):
        self.assert_source(
            "struct S { pub v: Int }\n"
            "impl Display for S {\n"
            "    pub fn to_string(&self) -> String {\n"
            "        return self.v.to_string();\n"
            "    }\n"
            "}\n"
            "fn main() {}\n",
            {},
        )

    def test_extra_block_on_builtin_type_stays_legal(self):
        """extra 块不是 trait 实现, 不受影响."""
        self.assert_source(
            "extra Int {\n"
            "    pub fn square(self) -> Int { return self * self; }\n"
            "}\n"
            "fn main() -> Int { return 3.square(); }\n",
            {},
        )


if __name__ == "__main__":
    unittest.main()
