"""Declaration mixin: function signatures, main-signature and group checks."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .defs import _MAIN_RETURN_TYPES

from ..types import (
    _base,
    _type_str,
)

from ...ast_components.ast import FnDecl, Name, StaticDecl

if TYPE_CHECKING:
    from ..analyzer import _Analyzer


class DeclMisc:

    def _group_types_match(
        self: "_Analyzer", expected: str, actual: str
    ) -> bool:
        """Group fields/distributions must agree on the underlying base
        type; merely both being numeric is not enough for a refinement."""
        exp = self._expand_type(expected)
        act = self._expand_type(actual)
        if exp is None or act is None:
            return True
        if _base(exp) != _base(act):
            return False
        return self._compat_types(expected, actual)

    def _check_group_distribution_type(
        self: "_Analyzer",
        d: "Distribution",
        subject_type: str,
        field: Optional["Field"],
    ) -> None:
        target = _type_str(d.type)
        if not self._group_types_match(target, subject_type):
            self._record_error(
                f"group distribution '{d.subject} -> {target}' cannot "
                f"receive {self._fmt_type(subject_type)}",
                d.line,
                d.column,
            )
        if field is not None and field.initializer is not None:
            self._check_refined_value(target, field.initializer, field)

    def _check_fn_types(self: "_Analyzer", fn: FnDecl) -> None:
        opaque = frozenset(p.name for p in fn.type_params)
        # bug-51: 签名里的泛型形参不参与全局类型表解析 (同名结构体/别名
        # 不能遮蔽本声明的形参)
        saved_generics = self._push_generics(opaque)
        try:
            self._annotate_type_params(fn.type_params, opaque)
            for p in fn.params:
                if p.type is not None:
                    self._check_type(p.type, p)
                    self._annotate_type_node(p.type, opaque)
                ptype = "Self" if p.name == "self" and p.type is None else (
                    _type_str(p.type) if p.type is not None else None
                )
                self._ann_type(p, ptype, opaque)
            if fn.return_type is not None:
                self._check_type(fn.return_type, fn)
                self._annotate_type_node(fn.return_type, opaque)
            ret = _type_str(fn.return_type) if fn.return_type is not None else "None"
            self._ann_type(fn, ret, opaque)
            # const-fn: 返回类型必须是 const-type (标记或结构性放行的
            # 复合类型); 堆容器等未标记类型不允许从 const-fn 返回。
            if getattr(fn, "const_fn", False):
                self._check_const_fn_return(fn, ret)
        finally:
            self._pop_generics(saved_generics)

    def _check_static_decl_type(
        self: "_Analyzer", decl: "StaticDecl", declared: str
    ) -> None:
        """static 存储的类型闸门: 借用类型出局。

        ``&T``/``&mut T`` 存的是**被借用位置的地址**, 而那个位置可以
        是栈上的临时量 —— 进程期存活的槽里留着这样的地址就是悬垂。
        其它类型 (标量 / String / 容器 / 结构体 / 枚举 / 定长数组 /
        函数指针 / 裸指针) 一律放行。
        """
        if not declared.startswith("&"):
            return
        self._record_error(
            f"a static cannot have the borrow type {self._fmt_type(declared)}",
            decl.type.line if decl.type is not None else decl.line,
            decl.type.column if decl.type is not None else decl.column,
        )

    def _check_static_init_refs(self: "_Analyzer", decl: "StaticDecl") -> None:
        """初始化式不得读另一个 static。

        静态初始化段按源码序跑一遍, 读到尚未初始化的槽只会看到零值 ——
        那是一个静默错误的值, 所以这里直接拒绝而不是给一个含糊的顺序。
        (经函数调用的间接读取仍不设防, 与结构体静态字段的既有面一致。)
        """
        from ..optimize._common import _walk_nodes

        for node in _walk_nodes(decl.value):
            if not isinstance(node, Name):
                continue
            binding = node._typed_ann.get("binding")
            kind = binding.get("kind") if isinstance(binding, dict) else None
            if kind != "static":
                continue
            self._record_error(
                f"the initializer of static '{decl.name}' cannot read the "
                f"static '{'::'.join(node.parts)}' (static initialization "
                f"order is source order; read a 'const' instead)",
                node.line,
                node.column,
            )

    def _check_main_signature(self: "_Analyzer", fn: FnDecl) -> None:
        """``main`` 的返回值只能成为进程退出码 (bug-24)。

        允许: 整数类型 (含 ``Byte``, 与后端 ``cg_is_int`` 一致)、
        ``None``/省略、never (``!``); 其余类型在编译期拒绝,
        不再静默丢弃返回值。

        bug-30: 参数只能为空, 或恰好一个 ``Vector<String>``
        (由后端从 C 的 argc/argv 构造注入)。
        """
        if fn.name != "main":
            return
        # bug-30: main 的程序参数
        if len(fn.params) > 1:
            self._record_error(
                "'main' accepts at most one parameter "
                "(a 'Vector<String>' receiving the program arguments)",
                fn.params[1].line,
                fn.params[1].column,
            )
        if len(fn.params) == 1:
            p = fn.params[0]
            ptype = _type_str(p.type) if p.type is not None else None
            expanded = self._expand_type(ptype)
            if expanded != "Vector<String>":
                self._record_error(
                    f"parameter '{p.name}' of 'main' must be "
                    f"'Vector<String>' (the program arguments), "
                    f"found {self._fmt_type(ptype)}",
                    p.line,
                    p.column,
                )
        if fn.return_type is None:
            return  # 省略返回类型 = None
        ret = _type_str(fn.return_type)
        expanded = self._expand_type(ret)
        base = _base(expanded)
        if not expanded.startswith("&") and base in _MAIN_RETURN_TYPES:
            return
        self._record_error(
            f"'main' must return an integer type or 'None', "
            f"found {self._fmt_type(ret)}",
            fn.return_type.line,
            fn.return_type.column,
        )
