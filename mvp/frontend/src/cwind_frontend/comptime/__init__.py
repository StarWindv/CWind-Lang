"""Const-fn compile-time evaluation (task: comptime).

Execution model (the user's design): instead of a stack VM, a const-fn
call inside a const initializer is evaluated by **compiling the callee's
dependency closure into a share DLL and calling it** — the same
"pull the dependencies into one file" idea as procedure macros, with an
``#[export]`` wrapper as the calling surface, ctypes as the marshalling
layer, and the result burned back into the AST as a literal so the
existing fold/inline machinery finishes the job.

Pipeline (all inside the inline pass, where every const reference has
already become a literal, which is what makes argument marshalling
trivial)::

    Call node
      -> wrapper spec from the call site's annotations (concrete types)
      -> export-whitelist pre-check (SA's own ``_c_abi_violation``)
      -> closure + wrapper source  ->  cached share DLL build
      -> eval args (C arithmetic semantics) -> ctypes -> DLL call
      -> demarshal -> burn as literal/constructor AST
"""

from __future__ import annotations

import copy
import ctypes
import json
import os
from dataclasses import fields as _dc_fields
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from ..ast_components.ast import Arg, Attribute, Call, Node
from .build import build_unit, guard_active, load_result, store_result
from .closure import UNIT_FN as _UNIT_FN
from .closure import WrapperSpec, build_unit_source, type_spelling
from .marshal import (
    EvalError,
    burn,
    ctype_for,
    decode_cv,
    demarshal,
    encode_cv,
    eval_value,
    marshal,
)

if TYPE_CHECKING:
    from ..sa.analyzer import _Analyzer

__all__ = ["evaluate_const_calls"]

_DLL_CACHE: dict[str, ctypes.CDLL] = {}


def evaluate_const_calls(
    analyzer: "_Analyzer",
    program,
    root: Node,
    *,
    project_base: Path,
    decl=None,
) -> Node:
    """Replace every const-fn call under *root* with its burned result.

    Children are rewritten first (a const-fn call nested in another
    call's arguments evaluates before its parent).  On any evaluation
    problem the offending call is left in place and an SA error is
    recorded at its position — the compilation fails, so the leftover
    node never reaches the backend.
    """
    out = _rewrite(analyzer, program, root, project_base)
    if out is None:
        return root
    if decl is not None and out is not root:
        # The burned node takes the const declaration's slot: give it
        # the declaration's type annotation (source-literal shape).
        decl_type = (getattr(decl, "_typed_ann", None) or {}).get("type")
        if isinstance(decl_type, dict):
            out._typed_ann["type"] = copy.deepcopy(decl_type)
    return out


def _report_guard(analyzer: "_Analyzer", at: Node) -> None:
    if getattr(analyzer, "_constfn_guard_reported", False):
        return
    analyzer._constfn_guard_reported = True  # type: ignore[attr-defined]
    analyzer._record_error(
        "recursive const-fn evaluation: this compilation is itself a "
        "const-fn evaluation unit, and one of its const initializers "
        "calls a const fn (const-eval units must not depend on "
        "const-eval results)",
        at.line,
        at.column,
    )


def _rewrite(analyzer, program, node: Node, project_base: Path) -> Optional[Node]:
    for f in _dc_fields(node):
        if f.name in ("line", "column"):
            continue
        value = getattr(node, f.name, None)
        if isinstance(value, Node):
            replaced = _rewrite(analyzer, program, value, project_base)
            if replaced is not None and replaced is not value:
                setattr(node, f.name, replaced)
        elif isinstance(value, list):
            for i, element in enumerate(value):
                if isinstance(element, Node):
                    replaced = _rewrite(
                        analyzer, program, element, project_base
                    )
                    if replaced is not None and replaced is not element:
                        value[i] = replaced
    if isinstance(node, Call):
        target = analyzer._const_call_target(node)
        if target is not None:
            return _evaluate_one(
                analyzer, program, node, target, project_base
            )
    return node


def _type_of(node: Node) -> Optional[dict]:
    ann = getattr(node, "_typed_ann", None) or {}
    info = ann.get("type")
    return info if isinstance(info, dict) else None


def _record(analyzer: "_Analyzer", call: Call, message: str) -> None:
    analyzer._record_error(message, call.line, call.column)


def _precheck(analyzer: "_Analyzer", wrapper: WrapperSpec) -> Optional[str]:
    """The export whitelist as the single source of boundary truth."""
    for t in wrapper.arg_types:
        spelling = type_spelling(t)
        if spelling == "None":
            return "void cannot be a const-fn argument"
        if spelling.startswith("fn("):
            return (
                "function-pointer arguments cannot cross const-fn "
                "evaluation"
            )
        violation = analyzer._c_abi_violation(
            spelling, decay=True, payload_enum=True
        )
        if violation is not None:
            return f"argument type '{spelling}' {violation}"
    if wrapper.ret_type is not None:
        spelling = type_spelling(wrapper.ret_type)
        if spelling != "None":
            violation = analyzer._c_abi_violation(
                spelling, decay=False, payload_enum=True
            )
            if violation is not None and not analyzer._option_ffi_ok(spelling):
                return f"return type '{spelling}' {violation}"
    return None


def _evaluate_one(
    analyzer: "_Analyzer",
    program,
    call: Call,
    target,
    project_base: Path,
) -> Node:
    if guard_active():
        # Only a real const-fn call reaching evaluation triggers the
        # recursion guard: plain const literals inline normally inside a
        # unit's own compilation.
        _report_guard(analyzer, call)
        return call
    callee = call.callee
    arg_types: list[dict] = []
    arg_values: list[Node] = []
    if isinstance(callee, Attribute):
        recv = _type_of(callee.obj)
        if recv is None:
            _record(analyzer, call, "const-fn receiver has no type")
            return call
        arg_types.append(recv)
        arg_values.append(callee.obj)
    for a in call.args or []:
        inner = a.value if isinstance(a, Arg) else a
        info = _type_of(inner) or _type_of(a)
        if info is None:
            _record(analyzer, call, "const-fn argument has no type")
            return call
        arg_types.append(info)
        arg_values.append(inner)
    ret_type = _type_of(call)
    if ret_type is None:
        ret_type = {"name": "None"}
    wrapper = WrapperSpec(
        callee=callee, arg_types=arg_types, ret_type=ret_type
    )
    violation = _precheck(analyzer, wrapper)
    if violation is not None:
        _record(
            analyzer,
            call,
            f"const fn cannot be evaluated: {violation}",
        )
        return call

    try:
        unit = build_unit_source(analyzer, program, target, wrapper)
    except EvalError as exc:
        _record(analyzer, call, f"const fn cannot be evaluated: {exc}")
        return call
    build = build_unit(unit, project_base)
    if not build.ok:
        _record(analyzer, call, build.build_error or "build failed")
        return call

    ret_spelling = type_spelling(ret_type)
    try:
        values = [eval_value(analyzer, v) for v in arg_values]
    except EvalError as exc:
        _record(
            analyzer, call,
            f"const fn argument cannot be evaluated: {exc}",
        )
        return call

    # 求值结果缓存 (纯函数前提): 同单元 + 同实参 → 跳过 DLL 加载与
    # 调用, 直接用持久化的烧录值。CWIND_CONSTFN_NO_RESULT_CACHE=1 旁路
    # (调 DLL 行为调试)。
    cache_on = not os.environ.get("CWIND_CONSTFN_NO_RESULT_CACHE")
    args_key = (
        json.dumps(
            [encode_cv(v) for v in values],
            ensure_ascii=False, separators=(",", ":"), allow_nan=True,
        )
        if cache_on else None
    )
    cached = False
    value: Any = None
    if args_key is not None:
        cached, encoded = load_result(unit.key, args_key)
        if cached:
            try:
                value = decode_cv(encoded)  # type: ignore[arg-type]
            except EvalError:
                cached = False  # corrupt entry: rebuild + overwrite

    result: Any = None
    if not cached:
        try:
            cargs = [
                marshal(analyzer, v, type_spelling(t))
                for v, t in zip(values, arg_types)
            ]
        except EvalError as exc:
            _record(
                analyzer, call,
                f"const fn argument cannot be evaluated: {exc}",
            )
            return call

        try:
            fn = _bound_fn(build.dll)
            arg_ctypes = []
            for t in arg_types:
                ct = ctype_for(analyzer, type_spelling(t))
                if ct is None:
                    raise EvalError(
                        f"cannot marshal argument type '{type_spelling(t)}'"
                    )
                arg_ctypes.append(ct)
            fn.argtypes = arg_ctypes
            if unit.array_ret:
                elem_ct: Any = ctype_for(
                    analyzer,
                    _array_element(ret_spelling),
                )
                if elem_ct is None:
                    raise EvalError(
                        f"cannot marshal array element of '{ret_spelling}'"
                    )
                wrap = type(
                    "CwArrWrap",
                    (ctypes.Structure,),
                    {"_fields_": [("v", elem_ct * _array_len(ret_spelling))]},
                )
                fn.restype = wrap
            else:
                fn.restype = ctype_for(analyzer, ret_spelling)
            result = fn(*cargs)
        except EvalError as exc:
            _record(analyzer, call, f"const fn cannot be evaluated: {exc}")
            return call
        except OSError as exc:
            _record(
                analyzer, call,
                f"const-fn evaluation call failed: {exc}",
            )
            return call

    try:
        if not cached:
            if unit.array_ret:
                result = getattr(result, "v")
            value = demarshal(analyzer, result, ret_spelling)
            if args_key is not None:
                store_result(unit.key, args_key, encode_cv(value))
        return burn(analyzer, value, call, type_info=None)
    except EvalError as exc:
        _record(
            analyzer, call,
            f"const fn result cannot be burned: {exc}",
        )
        return call


def _array_element(spelling: str) -> str:
    from ..sa.types import split_array_type

    arr = split_array_type(spelling)
    if arr is None:
        raise EvalError(f"'{spelling}' is not a fixed array")
    return arr[0]


def _array_len(spelling: str) -> int:
    from ..sa.types import split_array_type

    arr = split_array_type(spelling)
    if arr is None:
        raise EvalError(f"'{spelling}' is not a fixed array")
    return int(arr[1])


def _bound_fn(path):
    key = str(path)
    dll = _DLL_CACHE.get(key)
    if dll is None:
        dll = ctypes.CDLL(key)
        _DLL_CACHE[key] = dll
    return getattr(dll, _UNIT_FN)
