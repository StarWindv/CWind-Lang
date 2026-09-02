"""Built-in type metadata and type-string helpers shared across SA passes."""

from __future__ import annotations

import re
from typing import Optional

from ..ast_components.ast import Type

__all__ = [
    "BUILTIN_TYPES",
    "_BUILTIN_GENERIC_ARITY",
    "_BUILTIN_NS",
    "_base",
    "_bare_type",
    "_canon_owner",
    "_compatible",
    "_is_ref",
    "_qualify_builtin",
    "_replace_self",
    "_split_args",
    "_strip_builtin_ns",
    "_type_str",
    "_type_info",
    "split_array_type"
]


BUILTIN_TYPES: frozenset[str] = frozenset({
    "Int", "Int8", "Int16", "Int32", "Int64",
    "UInt", "UInt8", "UInt16", "UInt32", "UInt64",
    "Float", "Float64", "String", "Bool", "Byte",
    "None", "Tuple", "Vector", "Map", "Set", "Iterator",
    "*const", "*mut",
    "Fn",
    "fn",
    "!",  # never 类型 (仅作为函数返回类型)
})


# todo-154: 内置类型的全限定命名空间. pass 0 把类型引用规范化到
# ``std::builtins::X`` 形态 (namespace 链, Rust 2018 re-export 语义),
# 之后所有查表/比较操作都看这个规范形. ``_base`` 与 ``_canon`` 负责在
# 查表入口剥掉前缀 (SA 的注册表 key 是裸名), ``_qualify`` 负责在输出面
# (typed-AST ann / 后端 intern 边界) 补回前缀.
_BUILTIN_NS = "std::builtins::"


def _strip_builtin_ns(t: Optional[str]) -> Optional[str]:
    """The bare name of ``std::builtins::X`` (identity otherwise)."""
    if t is not None and t.startswith(_BUILTIN_NS):
        return t[len(_BUILTIN_NS):]
    return t


def _qualify_builtin(t: Optional[str]) -> Optional[str]:
    """The FQN form of a bare built-in type name (identity otherwise)."""
    if t is None:
        return None
    if t.startswith(_BUILTIN_NS):
        return t
    if t in BUILTIN_TYPES and not t.startswith(("*", "fn", "!", "Fn")):
        return _BUILTIN_NS + t
    return t


def _canon_owner(t: Optional[str]) -> Optional[str]:
    """Canonical table key for a type string's base owner.

    pass 0 stores built-in types under their FQN form
    (``std::builtins::Vector``), so every registry lookup
    (``self.methods`` / ``self.impls`` / ...) canonicalizes through
    this: strip refs/generics/prefix via ``_base``, then re-qualify a
    bare built-in name back to its FQN key.  User types stay flat.
    """
    if t is None:
        return None
    return _qualify_builtin(_base(t))


def _bare_type(t: Optional[str]) -> Optional[str]:
    """Deeply strip the builtin namespace prefix from a type string.

    pass 0 canonicalizes built-in types to ``std::builtins::X``; the
    canonical form lives inside *flat* type names too (array elements,
    pointer pointees, fn signature segments):
    ``[std::builtins::UInt8; 624]`` / ``*mut std::builtins::String`` /
    ``fn(std::builtins::Int) -> std::builtins::Int``.  This walks those
    constructs and returns the bare-spelled equivalent, which is what
    comparisons and the JSON boundary (typed-AST) consume.
    """
    if t is None:
        return None
    ref, t = _split_ref_prefix(t)
    if t.startswith("*const ") or t.startswith("*mut "):
        prefix, _, pointee = t.partition(" ")
        bare_pointee = _bare_type(pointee)
        return ref + prefix + " " + (bare_pointee if bare_pointee is not None else pointee)
    if t.startswith("["):
        parsed = split_array_type(t)
        if parsed is not None:
            elem, n = parsed
            bare_elem = _bare_type(elem)
            return ref + f"[{bare_elem if bare_elem is not None else elem}; {n}]"
        return ref + t
    if t.startswith("fn("):
        sig = _fn_sig_parts(t)
        if sig is not None:
            params, ret = sig
            parts = []
            for p in params:
                bare = _bare_type(p)
                parts.append(bare if bare is not None else p)
            out = "fn(" + ", ".join(parts) + ")"
            if ret != "None":
                bare_ret = _bare_type(ret)
                out += " -> " + (bare_ret if bare_ret is not None else ret)
            return ref + out
        return ref + t
    args = _split_args(t)
    raw_base = t.split("<", 1)[0]
    stripped_base = _strip_builtin_ns(raw_base)
    base = stripped_base if stripped_base is not None else raw_base
    if not args:
        return ref + base
    bare_args: list[str] = []
    for a in args:
        bare = _bare_type(a)
        bare_args.append(bare if bare is not None else a)
    return ref + (f"{base}<{', '.join(bare_args)}>")


_NUMERIC: frozenset[str] = frozenset({
    "Int", "Int8", "Int16", "Int32", "Int64",
    "UInt", "UInt8", "UInt16", "UInt32", "UInt64",
    "Float", "Float64", "Byte",
})


_INTEGER: frozenset[str] = frozenset({
    "Int", "Int8", "Int16", "Int32", "Int64",
    "UInt", "UInt8", "UInt16", "UInt32", "UInt64",
})


# bug-60 后续: 字面量按折叠值选最小适配位宽 (正数 → 无符号系, 负数 →
# 带符号系)。十进制写法 (含负号) 走带符号表, 十六进制 (todo-85, u64 语义)
# 走无符号表; 超出 i64/u64 在解析期即被拒绝。
_UNSIGNED_LITERAL_WIDTHS: list[tuple[int, str]] = [
    (0xFF, "UInt8"),
    (0xFFFF, "UInt16"),
    (0xFFFFFFFF, "UInt32"),
    (0xFFFFFFFFFFFFFFFF, "UInt64"),
]
_SIGNED_LITERAL_WIDTHS: list[tuple[int, str]] = [
    (0x7F, "Int8"),
    (0x7FFF, "Int16"),
    (0x7FFFFFFF, "Int32"),
    (0x7FFFFFFFFFFFFFFF, "Int64"),
]


def _smallest_literal_type(value: int, raw: str = "") -> str:
    """Type of an integer literal by its folded value (bug-60 后续).

    规则 = 「16 位默认 + 溢出升级」: 值落在 Int/UInt (i16/u16) 值域内时
    保持既有默认 ``Int`` —— 字面量是家族默认类型, 方法派发
    (``3.square()``) 与既有生态不变; 超出 16 位后按**最小适配位宽**升级
    (正数/十六进制 → 无符号系, 负数 → 带符号系), 与后端 ``cg_lit_int``
    的宽化一一对应。i64/u64 是当前最宽整数, 更大在解析侧报错。
    """
    if raw.startswith("0x") or raw.startswith("0X"):
        value = abs(value)
    if -0x8000 <= value <= 0xFFFF:
        return "Int"
    if value >= 0:
        for bound, name in _UNSIGNED_LITERAL_WIDTHS[1:]:
            if value <= bound:
                return name
        return "UInt64"
    for bound, name in _SIGNED_LITERAL_WIDTHS[1:]:
        if -bound - 1 <= value <= bound:
            return name
    return "Int64"


def _smallest_signed_literal_type(value: int) -> str:
    """带符号系的最小适配位宽 (一元负号下的字面量重判): 在 i16 值域内
    保持默认 ``Int``, 否则升到最小 i 系; 超出 i64 为解析侧错误。"""
    if -0x8000 <= value <= 0x7FFF:
        return "Int"
    for bound, name in _SIGNED_LITERAL_WIDTHS[1:]:
        if -bound - 1 <= value <= bound:
            return name
    return "Int64"


def _split_fn_sig(sig: str) -> tuple[list[str], Optional[str]]:
    """Split a flattened fn-type string ``fn(A, B) -> R`` into its
    parameter segments and optional return segment.

    The parser flattens fn types into a single name string; nested fn
    parameters would defeat naive splitting and are rejected by callers
    via the ``fn(`` prefix check on segments.
    """
    close = sig.rfind(")")
    inner = sig[3:close] if close > 3 else ""
    ret: Optional[str] = None
    tail = sig[close + 1:].strip() if close >= 0 else ""
    if tail.startswith("->"):
        ret = tail[2:].strip()
    params = [p.strip() for p in inner.split(",") if p.strip()]
    return params, ret


_BUILTIN_RANGES: dict[str, tuple[int, int]] = {
    "Int": (-32768, 32767),
    "Int8": (-128, 127),
    "Int16": (-32768, 32767),
    "Int32": (-2147483648, 2147483647),
    "Int64": (-9223372036854775808, 9223372036854775807),
    "UInt": (0, 65535),
    "UInt8": (0, 255),
    "UInt16": (0, 65535),
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

# bug-60 后续: 字面量绝对上限 (i64/u64 为当前最宽整数, 放不下即报错)
_UINT64_MAX = 0xFFFFFFFFFFFFFFFF
_INT64_MIN = -0x8000000000000000


_INT_RANK: dict[str, int] = {
    "Int8": 1, "UInt8": 1, "Byte": 1,
    "Int": 2, "UInt": 2, "Int16": 2, "UInt16": 2,
    "Int32": 3, "UInt32": 3,
    "Int64": 4, "UInt64": 4,
}

# 同宽度但有符号/无符号差异时, 提升到下一个更宽的有符号类型 (Rust 风格);
# Int/Int16 (及 UInt/UInt16) 是同宽度的两个独立类型, 混用同样提升到 Int32,
# 与后端 cg_common_numeric 的同秩规则保持一致
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
    ("UInt16", "Int16"): "Int32",
    ("Int16", "UInt16"): "Int32",
    # 同秩双无符号对 (如 UInt16/UInt) 不入表: 前端回退取左操作数,
    # 与后端 `both unsigned -> return a` 的顺序相关行为一致
    ("Int16", "Int"): "Int32",
    ("Int", "Int16"): "Int32",
    ("Int16", "UInt"): "Int32",
    ("UInt", "Int16"): "Int32",
    ("Int", "UInt16"): "Int32",
    ("UInt16", "Int"): "Int32",
}


def _common_numeric(a: Optional[str], b: Optional[str]) -> Optional[str]:
    """Common numeric type for mixed arithmetic / bitwise (Rust-ish)."""
    a, b = _strip_builtin_ns(a), _strip_builtin_ns(b)
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


def _ref_prefix(t: Type) -> str:
    """Reference marker for a ``Type`` node: ``&mut `` / ``&`` / ``''``."""
    if not t.ref:
        return ""
    return "&mut " if getattr(t, "mut", False) else "&"


def _type_str(t: Type, subst: Optional[dict[str, str]] = None) -> str:
    name = subst.get(t.name, t.name) if subst else t.name
    # todo-154: Type.name 是 FQN 存储形 (``std::builtins::Vector``), 而
    # 字符串解释面 (SA 表 / 字面量推断 / 比较) 一律裸名 —— 在此剥除。
    if name is not None:
        bare = _strip_builtin_ns(name)
        if bare is not None:
            name = bare
    ref = _ref_prefix(t)
    if name.startswith("fn("):
        inner = name + (" -> " + _type_str(t.args[0], subst) if t.args else "")
        return ref + inner
    if not t.args:
        inner = name
    else:
        inner = f"{name}<{', '.join(_type_str(a, subst) for a in t.args)}>"
    return ref + inner


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
    ref, t = _split_ref_prefix(t)
    if subst is None:
        return (ref + t) if ref else t
    if _depth > 64:
        return (ref + t) if ref else t  # 兜底: 任何情况下都不允许递归失控
    original = t
    seen: set[str] = set()
    while "<" not in t and t in subst:
        if t in seen:
            break
        seen.add(t)
        t = subst[t]
    if "<" not in t:
        return (ref + t) if ref else t
    if t != original:
        out = t  # 替换值本身是结构化类型: 原样返回, 不再深入
        return (ref + out) if ref else out
    # todo-154: 重建保留原基名 (含 ``std::builtins::`` 前缀)
    out = (
        f"{t.split('<', 1)[0]}<"
        f"{', '.join(_subst_type_str(a, subst, _depth + 1) for a in _split_args(t))}"
        f">"
    )
    return (ref + out) if ref else out


def _base(t: str) -> str:
    _, t = _split_ref_prefix(t)
    if t.startswith("*const ") or t.startswith("*mut "):
        return t.split(" ", 1)[0]
    if t.startswith("fn("):
        return t
    base = t.split("<", 1)[0]
    # todo-154: table keys are bare names; ``std::builtins::Vector<Int>``
    # and ``std::builtins::Vector`` both look up as ``Vector``.
    return _strip_builtin_ns(base)


_ARRAY_NAME_RE = re.compile(r"^\[(.+); *(\d+)\]$", re.DOTALL)


def split_array_type(t: Optional[str]) -> Optional[tuple[str, int]]:
    """Parse a fixed-length array type name (todo-60) ``"[T; N]"``.

    Returns ``(elem, n)`` or ``None`` when ``t`` is not an array type.
    Element types are restricted to scalars by SA, so the element part
    can never contain ``';'`` itself.  A leading borrow marker
    (``&``/``&mut ``) is tolerated (bug-46).
    """
    if t is None:
        return None
    _, t = _split_ref_prefix(t)
    if not t.startswith("["):
        return None
    m = _ARRAY_NAME_RE.match(t)
    if m is None:
        return None
    return (m.group(1).strip(), int(m.group(2)))


def _is_ref(t: Optional[str]) -> bool:
    return t is not None and t.startswith("&")


def _split_ref_prefix(t: str) -> tuple[str, str]:
    """Split a stringified type into its ref prefix and the rest.

    ``"&mut Int" -> ("&mut ", "Int")``, ``"&Int" -> ("&", "Int")``,
    plain types keep a ``("", t)`` pair (bug-46).
    """
    if t.startswith("&mut "):
        return "&mut ", t[len("&mut "):]
    if t.startswith("&"):
        return "&", t[1:]
    return "", t


def _strip_ref(t: Optional[str]) -> Optional[str]:
    if t is None:
        return None
    return _split_ref_prefix(t)[1] if t.startswith("&") else t


def _common_type(types: list[Optional[str]]) -> Optional[str]:
    # todo-154: 归一化到裸名再判等 (FQN 拼写与裸名拼写是同一类型)
    seen = {_bare_type(t) for t in types if t is not None}
    seen.discard(None)
    if len(seen) == 1:
        return next(iter(seen))  # type: ignore[arg-type]
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


def _fn_sig_parts(sig: str) -> Optional[tuple[list[str], str]]:
    """Split a flat ``fn(A, B) -> R`` name into (params, ret) (todo-146).

    ``params`` splits on top-level commas (``()``/``<>``/``[]``-aware);
    a missing return segment (``fn(Int)``) yields ``"None"``.  Returns
    ``None`` on unbalanced parentheses (malformed signature).
    """
    depth = 0
    close = -1
    for i, ch in enumerate(sig):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                close = i
                break
    if depth != 0 or close < 0:
        return None
    inner = sig[3:close]
    rest = sig[close + 1:].strip()
    ret = "None"
    if rest.startswith("->"):
        ret = rest[2:].strip() or "None"
    params: list[str] = []
    if inner.strip():
        depth = 0
        start = 0
        for i, ch in enumerate(inner):
            if ch in "(<[":
                depth += 1
            elif ch in ")>]":
                depth -= 1
            elif ch == "," and depth == 0:
                params.append(inner[start:i].strip())
                start = i + 1
        tail = inner[start:].strip()
        if tail:
            params.append(tail)
    return params, ret


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
    ref, t = _split_ref_prefix(t)
    mut = ref == "&mut "
    if t.startswith("*const ") or t.startswith("*mut "):
        # 原始指针: 名字已含被指类型, 整体扁平登记
        # (todo-154: JSON 边界输出裸名拼写)
        info: dict = {"name": _bare_type(t) or t}
        if ref:
            info["ref"] = True
            if mut:
                info["mut"] = True
        return info
    if t.startswith("["):
        # 定长数组 (todo-60): 名字已含元素与长度, 整体扁平登记
        info = {"name": _bare_type(t) or t}
        if ref:
            info["ref"] = True
            if mut:
                info["mut"] = True
        return info
    name = _base(t).strip()
    if name.startswith("fn("):
        # todo-154: fn 签名段内的 ``std::builtins::`` 前缀同样剥除
        bare_name = _bare_type(name) or name
        info = {"name": bare_name}
        # todo-146: args 承载真实签名段 —— args[0] 为参数 Tuple,
        # args[1] 为返回类型; ``name`` 保持完整扁平签名 (后端
        # cg_fn_sig_split 直接解析它)。解析失败则不写 args。
        sig = _fn_sig_parts(bare_name)
        if sig is not None:
            params, ret = sig
            param_infos = [_type_info(p, opaque_names) for p in params]
            ret_info = _type_info(ret, opaque_names)
            if (
                all(p is not None for p in param_infos)
                and ret_info is not None
            ):
                info["args"] = [
                    {"name": "Tuple", "args": param_infos},
                    ret_info,
                ]
        if ref:
            info["ref"] = True
            if mut:
                info["mut"] = True
        return info
    args = [_type_info(a, opaque_names) for a in _split_args(t)]
    # todo-154: JSON 边界一律输出裸名 (``std::builtins::X`` -> ``X``);
    # FQN 只存在于 SA 内部存储, 前后端契约保持裸名形态。
    bare_leaf = _strip_builtin_ns(name)
    if bare_leaf is not None:
        name = bare_leaf
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
    if ref:
        info["ref"] = True
        if mut:
            info["mut"] = True
    return info


def _type_str_from_info(info: Optional[dict]) -> Optional[str]:
    """Best-effort reconstruction of a type string from ``_type_info``."""
    if not isinstance(info, dict) or "name" not in info:
        return None
    name = str(info["name"])
    ref = "&mut " if info.get("mut") else ("&" if info.get("ref") else "")
    if name.startswith("*const ") or name.startswith("*mut "):
        out = name
        return (ref + out) if ref else out
    if name.startswith("["):
        # 定长数组 (todo-60): 名字整体扁平登记, 原样重建
        out = name
        return (ref + out) if ref else out
    args = [_type_str_from_info(a) for a in info.get("args", [])]
    out = name
    if name.startswith("fn("):
        # todo-146: 名字本身已是完整扁平签名, 不再拼接段
        return (ref + out) if ref else out
    if all(a is not None for a in args):
        out += "<" + ", ".join(args) + ">" if args else ""
    return (ref + out) if ref else out


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


def _replace_self(t: Optional[str], owner: Optional[str]) -> Optional[str]:
    """Substitute the ``Self`` type name with an owner type string."""
    if t is None or owner is None:
        return t
    ref, t = _split_ref_prefix(t)
    if _base(t) == "Self":
        return (ref + owner) if ref else owner
    args = _split_args(t)
    if not args:
        return (ref + t) if ref else t
    # todo-154: 重建时保留原基名 (含 ``std::builtins::`` 前缀),
    # ``_base`` 只用于 Self 判定
    base = t.split("<", 1)[0]
    out = (
        f"{base}<"
        f"{', '.join(_replace_self(a, owner) for a in args)}"
        f">"
    )
    return (ref + out) if ref else out


def _compatible(expected: Optional[str], actual: Optional[str]) -> bool:
    if expected is None or actual is None:
        return True
    # todo-154: 入口归一化 —— ``std::builtins::Vector<Int>`` 与
    # ``Vector<Int>`` (含数组元素/指针被指/fn 签名段内的前缀) 是同一
    # 类型; 比较一律看裸名形。
    expected = _bare_type(expected)
    actual = _bare_type(actual)
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
    if expected.startswith("*const ") or expected.startswith("*mut "):
        # 原始指针可从同型借用创建 (&T / &mut T -> *const/*mut T);
        # 指针之间不隐式互转
        if actual.startswith("&"):
            return expected.split(" ", 1)[1] == _strip_ref(actual)
        return expected == actual
    if actual.startswith("*const ") or actual.startswith("*mut "):
        # 反向 (&T 期望处给指针) 不允许
        return False
    if _is_ref(expected) != _is_ref(actual):
        return False
    # bug-46: 借用可变性遵循 Rust —— ``&mut T`` 位置必须收到 ``&mut``,
    # 共享借用 ``&T`` 位置可收 ``&mut T`` (可变收窄为共享)。
    e_mut = expected.startswith("&mut ")
    a_mut = actual.startswith("&mut ")
    if e_mut and not a_mut:
        return False
    expected = _strip_ref(expected)
    actual = _strip_ref(actual)
    if expected is None or actual is None:
        return True
    if expected == actual:
        return True
    if expected.startswith("fn(") or actual.startswith("fn("):
        return expected == actual
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
