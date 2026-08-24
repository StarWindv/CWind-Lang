"""Unit tests for todo-60/61: fixed-length arrays ``[T; N]``.

todo-60: value-semantic inline arrays (C ``char[N]`` / Rust ``[u8; N]``
counterparts).  Covers type parsing, scalar-element restriction, literal
length checking, compile-time bounds checks, mutability of indexed
writes, and struct fields with array types.
todo-61: aggregates with array fields are accepted in extern signatures
(real C layout via byval/sret), while non-scalar array elements and
arrays inside callback signatures stay rejected.

Input programs live in ``cases/todo60`` / ``cases/cffi``; pipeline
expectations sit in ``<name>.json`` sidecars.
"""

import dataclasses
import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

import harness

from cwind_frontend import parse_source, run_sa_with_errors
from cwind_frontend.ast_components.ast import Index, VectorLit
from cwind_frontend.sa.types import split_array_type, _type_info

T60 = "todo60"
CFFI = "cffi"


def _anns(src: str):
    """Run SA and yield every annotated node of the single program."""
    from cwind_frontend.ast_components import ast as A

    prog = parse_source(src)
    result = run_sa_with_errors(prog)

    def walk(node):
        yield node
        for f in dataclasses.fields(node):
            if f.name in ("line", "column"):
                continue
            v = getattr(node, f.name)
            if isinstance(v, A.Node):
                yield from walk(v)
            elif isinstance(v, list):
                for x in v:
                    if isinstance(x, A.Node):
                        yield from walk(x)

    return prog, result


class TestTodo60Pipeline(harness.CaseAssertionsMixin):
    def test_basic_accepted(self):
        self.assert_case(T60, "array_basic_ok")

    def test_len_mismatch_rejected(self):
        self.assert_case(T60, "array_len_mismatch")

    def test_index_oob_rejected(self):
        self.assert_case(T60, "array_index_oob")

    def test_elem_not_scalar_rejected(self):
        self.assert_case(T60, "array_elem_not_scalar")

    def test_zero_length_rejected(self):
        self.assert_case(T60, "array_zero_length")

    def test_type_mismatch_rejected(self):
        self.assert_case(T60, "array_type_mismatch")


class TestTodo60Semantics(unittest.TestCase):
    def test_array_type_name_roundtrip(self):
        self.assertEqual(split_array_type("[Byte; 4]"), ("Byte", 4))
        self.assertIsNone(split_array_type("Vector<Byte>"))
        self.assertIsNone(split_array_type("Byte"))

    def test_vector_literal_retyped_as_array(self):
        from cwind_frontend.ast_components import ast as A

        src = (
            "fn main() -> Int {"
            " let a: [Int32; 3] = [1, 2, 3];"
            " return 0;"
            "}"
        )
        prog = parse_source(src)
        result = run_sa_with_errors(prog)
        self.assertEqual([e.message for e in result.errors], [])

        def find(node):
            if isinstance(node, VectorLit):
                return node
            for f in dataclasses.fields(node):
                if f.name in ("line", "column"):
                    continue
                v = getattr(node, f.name)
                if isinstance(v, A.Node):
                    r = find(v)
                    if r is not None:
                        return r
                elif isinstance(v, list):
                    for x in v:
                        if isinstance(x, A.Node):
                            r = find(x)
                            if r is not None:
                                return r
            return None

        lit = find(prog)
        self.assertIsNotNone(lit)
        self.assertEqual(lit._typed_ann["type"]["name"], "[Int32; 3]")
        self.assertEqual(lit._typed_ann["element_type"]["name"], "Int32")

    def test_index_ann_carries_container_and_elem(self):
        src = (
            "fn main() -> Int {"
            " let a: [Byte; 4] = [1, 2, 3, 4];"
            " let x: Byte = a[2];"
            " print(x);"
            " return 0;"
            "}"
        )
        prog = parse_source(src)
        result = run_sa_with_errors(prog)
        self.assertEqual([e.message for e in result.errors], [])

        from cwind_frontend.ast_components import ast as A

        def find(node):
            if isinstance(node, Index):
                return node
            for f in dataclasses.fields(node):
                if f.name in ("line", "column"):
                    continue
                v = getattr(node, f.name)
                if isinstance(v, A.Node):
                    r = find(v)
                    if r is not None:
                        return r
                elif isinstance(v, list):
                    for x in v:
                        if isinstance(x, A.Node):
                            r = find(x)
                            if r is not None:
                                return r
            return None

        idx = find(prog)
        self.assertIsNotNone(idx)
        self.assertEqual(idx._typed_ann["type"]["name"], "Byte")
        self.assertEqual(
            idx._typed_ann["container_type"]["name"], "[Byte; 4]"
        )

    def test_array_type_info_is_flat(self):
        info = _type_info("[Byte; 4]")
        self.assertEqual(info, {"name": "[Byte; 4]"})

    def test_mutability_of_indexed_write(self):
        src = (
            "fn main() -> Int {"
            " let a: [Int32; 2] = [1, 2];"
            " a[0] = 3;"
            " return 0;"
            "}"
        )
        _, result = _anns(src)
        messages = [e.message for e in result.errors]
        self.assertTrue(
            any("declare it with 'mut'" in m for m in messages),
            messages,
        )


class TestTodo61Cffi(harness.CaseAssertionsMixin):
    def test_extern_struct_with_array_fields_accepted(self):
        self.assert_case(CFFI, "extern_array_struct_ok")

    def test_extern_array_bad_elem_rejected(self):
        self.assert_case(CFFI, "extern_array_bad_elem")

    def test_array_in_callback_signature_rejected(self):
        self.assert_case(CFFI, "extern_array_in_callback")


if __name__ == "__main__":
    unittest.main()
