"""Built-in type metadata and type-string helpers shared across SA passes."""

from __future__ import annotations

import re
from typing import Optional

from ..ast_components.ast import Type

__all__ = [
    "BUILTIN_TYPES",
    "BUILTIN_TYPES",
    "_BUILTIN_GENERIC_ARITY",
    "_base",
    "_compatible",
    "_split_args",
    "_type_str",
    "_type_info"
]


BUILTIN_TYPES: frozenset[str] = frozenset({
    "Int", "Int8", "Int32", "Int64",
    "UInt", "UInt8", "UInt32", "UInt64",
    "Float", "Float64", "String", "Bool", "Byte",
    "None", "Tuple", "Vector", "Map", "Set", "Iterator",
    "!",  # never 类型 (仅作为函数返回类型)
})


_NUMERIC: frozenset[str] = frozenset({
    "Int", "Int8", "Int32", "Int64",
    "UInt", "UInt8", "UInt32", "UInt64",
    "Float", "Float64", "Byte",
})


_INTEGER: frozenset[str] = frozenset({
    "Int", "Int8", "Int32", "Int64",
    "UInt", "UInt8", "UInt32", "UInt64",
})


_BUILTIN_RANGES: dict[str, tuple[int, int]] = {
    "Int": (-32768, 32767),
    "Int8": (-128, 127),
    "Int32": (-2147483648, 2147483647),
    "Int64": (-9223372036854775808, 9223372036854775807),
    "UInt": (0, 65535),
    "UInt8": (0, 255),
    "UInt32": (0, 4294967295),
    "UInt64": (0, 18446744073709551615),
    "Byte": (0, 255),
}


_BUILTIN_GENERIC_ARITY: dict[str, int] = {
    "Vector": 1,
    "Map": 2,
    "Set": 1,
}


_FLOAT32_MAX = 3.4028234663852886e38
_FLOAT64_MAX = 1.7976931348623157e308


_INT_RANK: dict[str, int] = {
    "Int8": 1, "UInt8": 1, "Byte": 1,
    "Int": 2, "UInt": 2,
    "Int32": 3, "UInt32": 3,
    "Int64": 4, "UInt64": 4,
}

# 同宽度但有符号/无符号差异时, 提升到下一个更宽的有符号类型 (Rust 风格)
_INT_WIDER: dict[tuple[str, str], str] = {
    ("UInt8", "Int8"): "Int",
    ("Int8", "UInt8"): "Int",
    ("Byte", "Int8"): "Int",
    ("Int8", "Byte"): "Int",
    ("UInt", "Int"): "Int32",
    ("Int", "UInt"): "Int32",
    ("UInt32", "Int32"): "Int64",
    ("Int32", "UInt32"): "Int64",
    ("UInt64", "Int64"): "Int64",
    ("Int64", "UInt64"): "Int64",
}


def _common_numeric(a: Optional[str], b: Optional[str]) -> Optional[str]:
    """Common numeric type for mixed arithmetic / bitwise (Rust-ish)."""
    if a is None:
        return b
    if b is None:
        return a
    if a == b:
        return a
    if "Float64" in (a, b):
        return "Float64"
    if "Float" in (a, b):
        return "Float"
    ra, rb = _INT_RANK.get(a), _INT_RANK.get(b)
    if ra is None or rb is None:
        return None
    if ra != rb:
        return a if ra > rb else b
    return _INT_WIDER.get((a, b), a)


def _type_str(t: Type, subst: Optional[dict[str, str]] = None) -> str:
    name = subst.get(t.name, t.name) if subst else t.name
    if not t.args:
        return name
    return f"{name}<{', '.join(_type_str(a, subst) for a in t.args)}>"


def _type_mentions(t: str, name: str) -> bool:
    """Whether a type string references the generic parameter ``name``."""
    return re.search(rf"\b{re.escape(name)}\b", t) is not None


def _subst_type_str(
    t: str,
    subst: Optional[dict[str, str]] = None,
    _depth: int = 0,
) -> str:
    """Substitute generic parameters inside a stringified type.

    关键规则: 当 ``T -> Node<T>`` 这类替换值里出现与 key 同名的参数时
    (两个不同作用域的泛型参数在字符串模型里无法区分), 直接原样返回替换值,
    不再深入其内部 —— 深入会 ``T -> Node<T> -> Node<Node<T>> -> ...``
    无限递归 (实测 my_heap.wind 的 ``Option::Some(top_node)`` SOF)。
    裸名链式替换 (``T -> U -> Int``) 仍保留。
    """
    if subst is None:
        return t
    if _depth > 64:
        return t  # 兜底: 任何情况下都不允许递归失控
    original = t
    seen: set[str] = set()
    while "<" not in t and t in subst:
        if t in seen:
            break
        seen.add(t)
        t = subst[t]
    if "<" not in t:
        return t
    if t != original:
        return t  # 替换值本身是结构化类型: 原样返回, 不再深入
    return (
        f"{_base(t)}<"
        f"{', '.join(_subst_type_str(a, subst, _depth + 1) for a in _split_args(t))}"
        f">"
    )


def _base(t: str) -> str:
    return t.split("<", 1)[0]


def _common_type(types: list[Optional[str]]) -> Optional[str]:
    seen = {t for t in types if t is not None}
    if len(seen) == 1:
        return next(iter(seen))
    return None


def _split_args(t: str) -> list[str]:
    """Split the top-level type arguments of ``Name<A, B<C>>``."""
    if "<" not in t:
        return []
    inner = t[t.find("<") + 1:t.rfind(">")]
    parts: list[str] = []
    depth = 0
    start = 0
    for i, ch in enumerate(inner):
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(inner[start:i].strip())
            start = i + 1
    parts.append(inner[start:].strip())
    return [p for p in parts if p]


def _type_info(
    t: Optional[str],
    opaque_names: frozenset[str] = frozenset(),
) -> Optional[dict]:
    """Convert a type string into the typed-AST representation.

    ``{"name": "Vector", "args": [{"name": "Int"}]}``.  Leaves whose name is
    still a generic parameter are kept with ``"opaque": true`` so partially
    known types (e.g. ``Vector<T>``) do not lose their outer shape; ``None``
    means nothing is known at all.
    """
    if t is None:
        return None
    name = _base(t).strip()
    args = [_type_info(a, opaque_names) for a in _split_args(t)]
    info: dict = {"name": name}
    if name in _BUILTIN_GENERIC_ARITY and not args:
        # A built-in generic with unknown arguments never appears bare:
        # unknown arguments are represented as `Any` leaves so consumers can
        # tell "generic with unknown args" from a plain non-generic type.
        args = [{"name": "Any"} for _ in range(_BUILTIN_GENERIC_ARITY[name])]
    if args:
        info["args"] = args
    if name in opaque_names:
        info["opaque"] = True
    return info


def _generic_ref_index(expected: str) -> int:
    """Return the 1-based generic position of ``SameAsGeneric[:N]``."""
    if expected.startswith("SameAsGeneric:"):
        return int(expected[len("SameAsGeneric:"):])
    return 1


def _generic_arg(t: Optional[str], index: int) -> Optional[str]:
    """Return the index-th generic argument of ``t`` (1-based)."""
    if t is None or "<" not in t:
        return None
    args = _split_args(t)
    if index < 1 or index > len(args):
        return None
    return args[index - 1]


def _compatible(expected: Optional[str], actual: Optional[str]) -> bool:
    if expected is None or actual is None:
        return True
    if expected == "Any" or actual == "Any":
        return True
    if actual == "!":
        # never 值可流向任意类型 (Rust 的 ! 自动强转)
        return True
    if expected == "!":
        # 反向不允许: `return 5;` 不能出现在 `-> !` 函数里
        return False
    if expected == actual:
        return True
    eb, ab = _base(expected), _base(actual)
    if eb == ab:
        # When both sides carry type arguments, compare them strictly
        # (Map<String, Int> is not Map<String, String>); a side without
        # arguments (e.g. an untyped map literal) stays lenient.
        e_args = _split_args(expected)
        a_args = _split_args(actual)
        if e_args and a_args:
            if len(e_args) != len(a_args):
                return False
            return all(
                _compatible(e, a) for e, a in zip(e_args, a_args)
            )
        if eb == "Tuple":
            # 裸 `Tuple` 是空/未知元组: 与非空元组类型互不兼容,
            # 防止 `let t: Tuple = (1, 2);` 这类声明静默通过。
            if e_args and not a_args:
                return False
            if a_args and not e_args:
                return False
        return True
    if eb in _NUMERIC and ab in _NUMERIC:
        return True
    if eb == "Fn" or ab == "Fn":
        return True
    return False
