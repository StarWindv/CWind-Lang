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
    "Int", "Int8", "UInt", "UInt8", "Float", "String", "Bool", "Byte",
    "None", "Tuple", "Vector", "Map", "Set", "Iterator",
})


_NUMERIC: frozenset[str] = frozenset({"Int", "Int8", "UInt", "UInt8", "Float", "Byte"})


_INTEGER: frozenset[str] = frozenset({"Int", "Int8", "UInt", "UInt8"})


_BUILTIN_RANGES: dict[str, tuple[int, int]] = {
    "Int": (-32768, 32767),
    "Int8": (-128, 127),
    "UInt": (0, 65535),
    "UInt8": (0, 255),
    "Byte": (0, 255),
}


_BUILTIN_GENERIC_ARITY: dict[str, int] = {
    "Vector": 1,
    "Map": 2,
    "Set": 1,
}


_FLOAT32_MAX = 3.4028234663852886e38


def _type_str(t: Type, subst: Optional[dict[str, str]] = None) -> str:
    name = subst.get(t.name, t.name) if subst else t.name
    if not t.args:
        return name
    return f"{name}<{', '.join(_type_str(a, subst) for a in t.args)}>"


def _type_mentions(t: str, name: str) -> bool:
    """Whether a type string references the generic parameter ``name``."""
    return re.search(rf"\b{re.escape(name)}\b", t) is not None


def _subst_type_str(t: str, subst: Optional[dict[str, str]] = None) -> str:
    """Substitute generic parameters inside a stringified type."""
    if subst is None:
        return t
    for _ in range(len(subst) + 1):
        if t in subst:
            t = subst[t]
        else:
            break
    if "<" not in t:
        return t
    return f"{_base(t)}<{', '.join(_subst_type_str(a, subst) for a in _split_args(t))}>"


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
        return True
    if eb in _NUMERIC and ab in _NUMERIC:
        return True
    if eb == "Fn" or ab == "Fn":
        return True
    return False
