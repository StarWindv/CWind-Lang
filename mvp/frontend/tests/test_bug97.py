"""bug-97: 引用语义的**类型标注契约** —— 后端 `as` 保地址所依赖的那一半。

bug-97 / bug-88 本身是纯后端洞 (`cwcodegen.c` ``cg_expr_cast`` 用
``type_name[0]=='&'`` 嗅引用, 而 ``cg_type_name_of`` 只回基名, 嗅探恒不成立,
于是引用掉进"数值 -> 指针"分支被解引用内联, 把**值的位模式**当地址传出去;
非标量引用则让 ``LLVMBuildLoad2`` 收到 NULL 类型, 编译期 0xC0000005)。
地址保真那一半由 ``fixtures/codegen_bug97.wind`` 端到端钉住, 本模块钉的是
**前端必须继续产出的标注形状** —— 后端的修法完全建立在这个契约上:

1. ``ann.operand_type.ref is True`` 当且仅当操作数是**借用**。
   后端 ``cg_expr_is_ref`` 只看这一个位。没有它, 后端无法把引用和裸指针
   值分开, bug-97 原样复活; 反过来, 一个**误加**的 ``ref`` 会把裸指针
   操作数 (``self.data as *const T`` 这种) 当成引用去取地址, 又是错地址。

2. 借用的 ``ann.operand_type.name`` 写的是**被指类型**的拼写, 不是引用本身。
   后端 ``cg_ref_pointee_is_ptr`` 要靠它区分 "`&mut *mut T`" (被指是地址)
   与 "`&T`" (被指是标量)。这条尤其关键: `#[export]` 函数的引用**形参**会把
   ``ann.type`` 改写成 `*const T` / `*mut T` (见
   ``RefDerefAnnotationTests.test_ref_param_type_node_spelling``), 于是同一个
   类型名串既可能是"引用"也可能是"裸指针", 拿 ``cg_type_name_of`` 的结果判
   必然判错 —— 这正是 bug-97 修复第一版回归 ``pipeline_reverse_ffi`` 的坑。

`cases/bug97/ref_to_rawptr_as.wind` 走公共 harness (只钉 SA 放行), 本模块
在同一份源码上追加结构断言, 所以数据只有一份。
"""

from __future__ import annotations

import json
import unittest

import harness
from cwind_frontend import parse_source, run_sa_with_errors
from cwind_frontend.typed_ast import build_typed_ast

CASE = ("bug97", "ref_to_rawptr_as")


def _typed_doc():
    """The exact JSON document the backend consumes, for the case source."""
    prog = parse_source(harness.source(*CASE))
    result = run_sa_with_errors(prog)
    if result.errors:
        raise AssertionError(
            f"{CASE[0]}/{CASE[1]} must pass SA: "
            f"{[e.message for e in result.errors]}"
        )
    return build_typed_ast(prog, result.info)


def _nodes(doc, kind):
    out = []

    def walk(o):
        if isinstance(o, dict):
            if o.get("kind") == kind:
                out.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(doc["ast"])
    return out


def _is_rawptr(name):
    return isinstance(name, str) and (
        name.startswith("*mut ") or name.startswith("*const ")
    )


def _ann(node):
    return node.get("ann") or {}


class RefToRawPtrCastAnnotationTests(unittest.TestCase):
    """`as` 转换: 操作数的借用位必须落在 `operand_type.ref` 上。"""

    @classmethod
    def setUpClass(cls):
        cls.doc = _typed_doc()

    def test_source_is_clean(self):
        """先把"SA 放行"这一半钉住 (公共 harness 也钉一遍, 这里是提醒)。"""
        result = harness.run_pipeline(harness.source(*CASE))
        self.assertEqual(result["kind"], "clean")

    def test_ref_casts_carry_operand_ref(self):
        """引用 -> 裸指针: `ref` 必在, 且 name 是**被指**拼写。

        判据 (rustc E0606, 见 test_bug88.py): 被指类型必须完全相同, 所以
        这里留下的引用位转换只有 `*mut Int32` / `*const Int32` / `*const
        String` / `*mut Color` 四种; 换被指的那些由 SA 拒绝, 不在 typed AST
        里 (见 cases/bug88)。
        """
        got = {}
        for node in _nodes(self.doc, "CastExpr"):
            ann = _ann(node)
            target = ann.get("type", {}).get("name")
            operand = ann.get("operand_type")
            if not _is_rawptr(target) or not operand:
                continue
            if operand.get("ref") is not True:
                continue  # 裸指针位的转换由 test_bug88 的 raw 侧矩阵钉
            got[target] = (operand.get("name"), operand.get("ref"))
        # 每一条都以操作数的借用位宣告"这是引用, 取它的地址"
        self.assertEqual(
            got,
            {
                "*mut Int32": ("Int32", True),     # &mut T as *mut T
                "*const Int32": ("Int32", True),   # &mut T / &T as *const T
                "*const String": ("String", True),  # 同被指的非标量引用
                "*mut Color": ("Color", True),      # 同被指的枚举引用
            },
        )

    def test_ref_pointee_name_is_never_the_const_rewrite(self):
        """借用的 name 不得被写成 `*const T`。

        这就是让 bug-97 修复第一版踩坑的形状: `#[export]` 的共享引用形参在
        ``ann.type`` 里是 `*const T`, 若操作数标注也这么写, 后端
        ``cg_ref_pointee_is_ptr`` 会把 `*p``(p: &Int32)`` 判成"解裸指针",
        于是 ``cw_peek`` 返回 `*const Int32` 而不是 Int32
        (``pipeline_reverse_ffi`` 就是这么回归的)。
        """
        checked = 0
        for node in _nodes(self.doc, "CastExpr"):
            operand = _ann(node).get("operand_type")
            if not operand or operand.get("ref") is not True:
                continue
            if operand.get("name") not in ("Int32", "String", "Color"):
                continue
            checked += 1
            self.assertFalse(
                _is_rawptr(operand["name"]),
                f"borrow pointee must not be spelled as a raw pointer: "
                f"{operand['name']!r} (-> {operand})",
            )
        self.assertGreaterEqual(checked, 5)

    def test_non_borrow_casts_have_no_ref(self):
        """反向: 裸指针**值**作操作数时不得带 ref。

        误加的 ref 会让后端把裸指针当引用去取地址 —— 于是 `p as i64` 拿到
        p 自己那个槽的地址而不是 p 的值, 同样是错地址。
        """
        plain = 0
        for node in _nodes(self.doc, "CastExpr"):
            operand = _ann(node).get("operand_type") or {}
            if not _is_rawptr(operand.get("name")):
                continue
            plain += 1
            self.assertIsNot(
                operand.get("ref"), True,
                f"raw-pointer operand must not carry ref: {operand}",
            )
        # 非空洞: `peek`/`bump` 里的 `p as i64` 与四组 `bN as i64` 都在此列
        self.assertGreaterEqual(plain, 5)

    def test_ref_to_int_is_typed_as_borrow(self):
        """引用 -> 整型只认两步式 (Rust 只接受 `&T as *const T as usize`)。

        第一步仍然是"引用 -> 裸指针"这一类, 所以 `operand_type.ref` 必在;
        后端据此取**地址**而不是被指的值。
        """
        got = [
            (n["ann"]["operand_type"], n["ann"]["type"])
            for n in _nodes(self.doc, "CastExpr")
            if n["ann"].get("operand_type", {}).get("ref")
            and n["ann"].get("type", {}).get("name") == "Int64"
        ]
        self.assertEqual(got, [])


class RefDerefAnnotationTests(unittest.TestCase):
    """同族第二条: `*p` where `p: &mut *mut T` 的标注。"""

    @classmethod
    def setUpClass(cls):
        cls.doc = _typed_doc()

    def test_deref_of_ref_to_ptr_is_a_pointer(self):
        """`*p` 类型成 `*mut T`, 操作数带 ref 且 name 是 `*mut T`。

        后端据此先 load 出那个 8 字节地址本体 (``cg_expr_unary`` 的 `*``
        分支), 旧实现按 name 抢先命中 ``cg_is_rawptr`` 就直接当裸指针解引用,
        读出指针位模式的低半截, `p == real` 静默打成 false。
        """
        star = [
            _ann(n)
            for n in _nodes(self.doc, "UnaryOp")
            if n.get("op") == "*" and _ann(n).get("operand_type")
        ]
        via_ref = [a for a in star if a["operand_type"].get("ref") is True]
        self.assertEqual(
            via_ref,
            [{"operand_type": {"name": "*mut Int32", "ref": True, "mut": True},
              "type": {"name": "*mut Int32"}}],
        )
        # `bump` 里的 `*f` 两处 (f: *mut i32, 裸指针值) 不得带 ref ——
        # 否则后端会把它们也走引用那条路
        raw = [a for a in star if a["operand_type"].get("ref") is not True]
        self.assertEqual(
            [a["operand_type"]["name"] for a in raw],
            ["*mut Int32", "*mut Int32"],
        )

    def test_ref_param_type_node_spelling(self):
        """形参 Type 节点的形状 —— 含 `#[export]` 那条改写路径。

        ``cg_type_name_of`` 优先读 ``ann.type``, 所以形参变量在后端手里的
        ``CwExpr.type_name`` 就是 ``ann.type.name``:

        * 普通函数 ``p: &Int32``  -> ``"Int32"``
        * **`#[export]`** ``p: &Int32``  -> ``"*const Int32"``
          (导出签名按 C ABI 把引用改写成裸指针; ``&mut`` -> ``"*mut ..."``)

        也就是说同一个 ``CwExpr.type_name`` 串既可能是共享引用也可能是裸指针,
        ``pipeline_reverse_ffi`` 的 ``cw_peek`` 就是后一种。这不是 bug, 是一条
        必须记住的形状: 判引用只能查结构位 (``cg_type_is_ref``), 判被指只能查
        类型对象的 ``name``, 两者都不能用 ``cg_type_name_of`` 的结果。
        """

        def params(src):
            prog = parse_source(src)
            result = run_sa_with_errors(prog)
            self.assertEqual([e.message for e in result.errors], [])
            doc = build_typed_ast(prog, result.info)
            out = {}
            for item in doc["ast"]["items"]:
                if not item.get("params"):
                    continue
                t = item["params"][0]["type"]
                out[item["name"]] = {
                    "name": t.get("name"),
                    "ref": t.get("ref"),
                    "mut": t.get("mut"),
                    "ann.type": t.get("ann", {}).get("type"),
                }
            return out

        plain = params(
            "fn peek(p: &Int32) -> Int32 { return *p; }\n"
            "fn take(q: &mut Int32) -> Int32 { return *q; }\n"
            "fn raw(r: *mut Int32) -> Int32 { return *r; }\n"
        )
        self.assertEqual(plain["peek"]["name"], "Int32")
        self.assertEqual(plain["peek"]["ann.type"],
                         {"name": "Int32", "ref": True})
        self.assertEqual(plain["take"]["ann.type"],
                         {"name": "Int32", "ref": True, "mut": True})
        self.assertIs(plain["raw"]["ref"], False)
        self.assertEqual(plain["raw"]["ann.type"], {"name": "*mut Int32"})

        exported = params(
            "#[export]\nfn peek(p: &Int32) -> Int32 { return *p; }\n"
            "#[export]\nfn take(q: &mut Int32) -> Int32 { return *q; }\n"
        )
        # 改写只动 ann.type; Type 节点自己的 ref 位原样保留
        self.assertEqual(exported["peek"]["ann.type"], {"name": "*const Int32"})
        self.assertIs(exported["peek"]["ref"], True)
        self.assertEqual(exported["take"]["ann.type"], {"name": "*mut Int32"})
        self.assertIs(exported["take"]["ref"], True)
        self.assertIs(exported["take"]["mut"], True)


class TypedDocStaysSerializableTests(unittest.TestCase):
    """上面所有断言都跑在真喂给后端的那份 JSON 上, 所以它必须还能序列化。"""

    def test_json_roundtrip(self):
        doc = _typed_doc()
        self.assertEqual(json.loads(json.dumps(doc)), doc)


if __name__ == "__main__":
    unittest.main()