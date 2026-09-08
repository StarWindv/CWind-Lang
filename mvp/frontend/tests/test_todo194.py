"""todo-194: trait default-method dispatch on implementor call sites.

bug-68 made default bodies check with the *trait* as owner and let impls
skip re-providing them; todo-194 completes the story by materializing
every unprovided default body (transitive supertraits included) into the
implementing impl's method table, so ``obj.default_method()`` dispatches
through the normal method path and the backend emits
``cwind.<Owner>.<fn>`` with zero backend changes.

Covering: injected-clone bookkeeping (``default_of_trait`` marker,
binding ids), call-site ``method`` annotations, impl-provided overrides,
supertrait default inheritance, default-calls-default chains, generic
impl owners, and the static ``Type::method`` rejection (todo-199).
"""

import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))

import harness

from cwind_frontend import parse_source, run_sa_with_errors
from cwind_frontend.ast_components import ast as A

T194 = "todo194"


def _sa(name: str):
    prog = parse_source(harness.source(T194, name))
    return prog, run_sa_with_errors(prog)


def _first(prog, kind):
    for item in prog.items:
        if isinstance(item, kind):
            return item
    raise AssertionError(f"no {kind.__name__} in program")


class TestTodo194Pipeline(harness.CaseAssertionsMixin):
    def test_bug68_trait_default_self_regression(self):
        # bug-68 回归形态: 实现者调用点调用 trait 默认方法
        self.assert_case("sa", "bug68_trait_default_self")

    def test_default_dispatch(self):
        self.assert_case(T194, "default_dispatch")

    def test_default_calls_default(self):
        self.assert_case(T194, "default_calls_default")

    def test_generic_impl_owner(self):
        self.assert_case(T194, "generic_impl_owner")

    def test_supertrait_default(self):
        self.assert_case(T194, "supertrait_default")

    def test_static_instance_method_rejected(self):
        # todo-199: 静态路径调实例方法仍拒绝, 锁定现状
        self.assert_case(T194, "static_instance_rejected")


class TestTodo194Annotations(unittest.TestCase):
    def test_dispatch_ann_and_injected_binding(self):
        prog, res = _sa("default_dispatch")
        self.assertEqual(res.errors, [])
        impl = _first(prog, A.ImplDecl)
        injected = [
            m for m in impl.methods
            if m._typed_ann.get("default_of_trait") == "Greeter"
        ]
        self.assertEqual(
            [m.name for m in injected], ["greet"]
        )
        calls = harness.run_pipeline(
            harness.source(T194, "default_dispatch")
        )
        self.assertEqual(calls["kind"], "clean")

    def test_impl_provided_not_injected(self):
        # 实现者显式提供同名方法时不得注入 (覆盖优先)
        prog, res = _sa("override_default")
        self.assertEqual(res.errors, [])
        impl = _first(prog, A.ImplDecl)
        injected = [
            m for m in impl.methods
            if m._typed_ann.get("default_of_trait") is not None
        ]
        self.assertEqual(injected, [])

    def test_call_sites_carry_method_kind(self):
        prog, res = _sa("default_dispatch")
        self.assertEqual(res.errors, [])
        dispatch = []
        for call in TestTodo194Annotations._iter_calls(prog):
            if isinstance(call.callee, A.Attribute):
                ann = call._typed_ann.get("call", {})
                if ann.get("callee_kind") == "method":
                    dispatch.append((call.callee.name, ann))
        by_name = {n: a for n, a in dispatch}
        self.assertIn("greet", by_name)
        # 调用点的 callee_ref 指向一个真实 binding
        ref = by_name["greet"]["callee_ref"]
        self.assertIn(ref, [b.id for b in res.info.bindings])

    @staticmethod
    def _iter_calls(node):
        stack = [node]
        attrs = (
            "items", "stmts", "value", "left", "right", "operand",
            "expr", "body", "args", "elems", "subject", "arms", "guard",
            "obj", "index", "callee", "then", "else_",
        )
        while stack:
            n = stack.pop()
            if isinstance(n, A.Call):
                yield n
            for a in attrs:
                v = getattr(n, a, None)
                if isinstance(v, list):
                    stack.extend(x for x in v if hasattr(x, "__dict__"))
                elif v is not None and hasattr(v, "__dict__"):
                    stack.append(v)

    def test_supertrait_default_bound_to_impl_trait_name(self):
        # 后端 cwmodule.c 校验 binding.trait == decl.trait.name:
        # 超 trait 默认体的 binding.trait 记为 impl 自己的 trait 名
        prog, res = _sa("supertrait_default")
        self.assertEqual(res.errors, [])
        trait_names = {
            b.trait for b in res.info.bindings
            if b.trait is not None
        }
        self.assertNotIn("Base", trait_names)


if __name__ == "__main__":
    unittest.main()
