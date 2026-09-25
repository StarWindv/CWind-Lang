"""Cross-boundary values for const-fn evaluation (task: comptime).

Three conversions around the DLL boundary:

* ``eval_value`` — the post-inline argument AST -> a ``CvValue`` tree
  (pure compile-time evaluation with **C division semantics**: integer
  ``/``/``%`` must divide exactly — Python floor/sign semantics differ
  from the backend's ``sdiv``/``srem`` exactly where the two disagree,
  mirroring the fold discipline that excludes those operators);
* ``ctype_for`` / ``marshal`` / ``demarshal`` — ``CvValue`` <-> ctypes,
  driven by the call-site's annotated type spellings and the export
  ABI (scalars at native width — ``Int`` is i16! — String <-> char*,
  arrays decay to element pointers, structs/enums as C-layout mirrors,
  raw pointers as opaque addresses);
* ``burn`` — ``CvValue`` -> AST nodes with the original call's
  annotations carried over, so the inliner's fold step sees an ordinary
  literal/constructor expression.
"""

from __future__ import annotations

import copy
import ctypes
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional, Union

from ..ast_components.ast import (
    Arg,
    Attribute,
    BinOp,
    BoolLit,
    Call,
    CastExpr,
    FloatLit,
    Index,
    IntLit,
    Name,
    Node,
    StrLit,
    StructConstruct,
    Type,
    UnaryOp,
    VectorLit,
)
from ..ast_components.token import TokenKind
from ..sa.types import _split_args, _subst_type_str, _type_str, split_array_type

if TYPE_CHECKING:
    from ..sa.analyzer import _Analyzer

__all__ = [
    "EvalError",
    "CvValue",
    "StructVal",
    "ArrayVal",
    "EnumVal",
    "PtrVal",
    "eval_value",
    "ctype_for",
    "marshal",
    "demarshal",
    "burn",
]


class EvalError(Exception):
    """A const-fn evaluation boundary problem (message is user-facing)."""


@dataclass
class StructVal:
    base: str          # instance spelling, e.g. "Point" or "Foo<Int>"
    fields: list       # CvValue per declared (non-static) field


@dataclass
class ArrayVal:
    items: list


@dataclass
class EnumVal:
    base: str
    variant: str
    payload: list      # [] for fieldless
    variant_index: int = -1


@dataclass
class PtrVal:
    address: int


CvValue = Union[int, float, str, bool, None, "StructVal", "ArrayVal",
                "EnumVal", "PtrVal"]


# Native C widths per CWind scalar (Int/UInt are 16-bit, todo-208).
_SCALAR_CTYPES = {
    "Int8": ctypes.c_int8,
    "UInt8": ctypes.c_uint8,
    "Byte": ctypes.c_uint8,
    "Bool": ctypes.c_uint8,
    "Int16": ctypes.c_int16,
    "UInt16": ctypes.c_uint16,
    "Int": ctypes.c_int16,
    "UInt": ctypes.c_uint16,
    "Int32": ctypes.c_int32,
    "UInt32": ctypes.c_uint32,
    "Int64": ctypes.c_int64,
    "UInt64": ctypes.c_uint64,
    "Float": ctypes.c_float,
    "Float64": ctypes.c_double,
}


def _spelling_to_type_dict(spelling: str) -> dict:
    """Inverse of closure.type_spelling for annotation dicts."""
    if spelling.startswith("["):
        return {"name": spelling}          # arrays stay flat (todo-60)
    if spelling.startswith("&mut "):
        inner = _spelling_to_type_dict(spelling[len("&mut "):])
        inner["ref"] = True
        inner["mut"] = True
        return inner
    if spelling.startswith("&"):
        inner = _spelling_to_type_dict(spelling[1:])
        inner["ref"] = True
        return inner
    if "<" in spelling:
        name = spelling.split("<", 1)[0]
        return {
            "name": name,
            "args": [_spelling_to_type_dict(a) for a in _split_args(spelling)],
        }
    return {"name": spelling}


# ---------------------------------------------------------------------------
# argument evaluation: AST -> CvValue (C arithmetic semantics)
# ---------------------------------------------------------------------------

def _div_trunc(left: int, right: int) -> int:
    """C ``sdiv``: truncate toward zero (Python ``//`` floors)."""
    if right == 0:
        raise EvalError("division by zero in const-fn argument")
    if left % right != 0:
        raise EvalError(
            "integer division in a const-fn argument must divide exactly "
            "(C truncates toward zero; the compile-time evaluator cannot "
            "commit to a truncated result)"
        )
    quot = abs(left) // abs(right)
    return -quot if (left < 0) != (right < 0) else quot


def _mod_trunc(left: int, right: int) -> int:
    """C ``srem``: remainder keeps the dividend's sign."""
    if right == 0:
        raise EvalError("division by zero in const-fn argument")
    if left % right != 0:
        raise EvalError(
            "integer remainder in a const-fn argument must divide exactly "
            "(C truncates toward zero; the compile-time evaluator cannot "
            "commit to a truncated result)"
        )
    return left - _div_trunc(left, right) * right


def _eval_num(node: Node) -> Any:
    """Pure compile-time evaluation with C operator semantics."""
    if isinstance(node, IntLit):
        return int(node.value)
    if isinstance(node, FloatLit):
        return float(node.value)
    if isinstance(node, StrLit):
        return str(node.value)
    if isinstance(node, BoolLit):
        return bool(node.value)
    if isinstance(node, UnaryOp):
        operand: Any = _eval_num(node.operand)
        if node.op == TokenKind.MINUS:
            if isinstance(operand, str):
                raise EvalError("cannot negate a string const-fn argument")
            return -operand
        if node.op == TokenKind.PLUS:
            return operand
        if node.op == TokenKind.NOT:
            if isinstance(operand, bool):
                return not operand
            if isinstance(operand, int):
                return ~operand  # integer bitwise NOT (language rule)
            raise EvalError("'!' requires a Bool or integer argument")
        if node.op == TokenKind.STAR or node.op == TokenKind.AMP:
            raise EvalError("dereference/borrow is not a constant value")
        raise EvalError("unsupported unary operator in const-fn argument")
    if isinstance(node, BinOp):
        left = _eval_num(node.left)
        right = _eval_num(node.right)
        op = node.op
        if op == TokenKind.PLUS:
            if isinstance(left, str) and isinstance(right, str):
                return left + right
            if isinstance(left, str) or isinstance(right, str):
                raise EvalError("cannot add a string to a number")
            return left + right
        if op == TokenKind.MINUS:
            return left - right
        if op == TokenKind.STAR:
            return left * right
        if op == TokenKind.SLASH:
            if isinstance(left, int) and isinstance(right, int):
                return _div_trunc(left, right)
            return left / right
        if op == TokenKind.PERCENT:
            if isinstance(left, int) and isinstance(right, int):
                return _mod_trunc(left, right)
            return math.fmod(left, right)  # C fmod: dividend sign
        if isinstance(left, bool) or isinstance(right, bool):
            # comparisons on bools fall through the numeric path below
            pass
        int_ops = {
            TokenKind.SHL: lambda: left << right,
            TokenKind.SHR: lambda: left >> right,
            TokenKind.AMP: lambda: left & right,
            TokenKind.PIPE: lambda: left | right,
            TokenKind.CARET: lambda: left ^ right,
        }
        if op in int_ops:
            if isinstance(left, int) and isinstance(right, int):
                return int_ops[op]()
            raise EvalError("bitwise operators require integer arguments")
        cmp_ops = {
            TokenKind.LT: lambda: left < right,
            TokenKind.GT: lambda: left > right,
            TokenKind.LE: lambda: left <= right,
            TokenKind.GE: lambda: left >= right,
            TokenKind.EQ: lambda: left == right,
            TokenKind.NE: lambda: left != right,
            TokenKind.AND: lambda: bool(left) and bool(right),
            TokenKind.OR: lambda: bool(left) or bool(right),
        }
        if op in cmp_ops:
            try:
                return cmp_ops[op]()
            except TypeError as exc:
                raise EvalError(f"incompatible operands: {exc}") from None
        raise EvalError("unsupported operator in const-fn argument")
    raise EvalError(
        f"'{type(node).__name__}' is not a constant value"
    )


def _cast_value(analyzer: "_Analyzer", value, target_node) -> CvValue:
    """Numeric/pointer cast with C conversion semantics."""
    from ..sa.const_fold import _CAST_INT_BITS

    target = _type_str(target_node)
    target = analyzer._expand_type(target) or target
    if isinstance(value, str):
        if target == "String":
            return value
        raise EvalError(f"cannot cast a String to '{target}'")
    if target in _CAST_INT_BITS or target in ("Bool",):
        if target == "Bool":
            return bool(value) if isinstance(value, (bool, int, float)) \
                else bool(value)
        if isinstance(value, bool):
            iv = int(value)
        elif isinstance(value, float):
            iv = int(value)          # C truncates toward zero
        else:
            iv = int(value)
        if target == "Bool":
            return iv != 0
        return iv   # ctypes performs the final width/sign conversion
    if target in ("Float", "Float64"):
        if isinstance(value, bool):
            return float(int(value))
        if isinstance(value, (int, float)):
            return float(value)
        raise EvalError(f"cannot cast '{value!r}' to '{target}'")
    if target.startswith("*const ") or target.startswith("*mut "):
        if isinstance(value, PtrVal):
            return value
        if isinstance(value, int):
            return PtrVal(int(value))
        raise EvalError("only integer/pointer values convert to pointers")
    if isinstance(value, PtrVal) and (
        target in _SCALAR_CTYPES or target in _CAST_INT_BITS
    ):
        return value.address
    if target == "String" and isinstance(value, str):
        return value
    raise EvalError(f"unsupported cast target '{target}'")


def eval_value(analyzer: "_Analyzer", node: Node) -> CvValue:
    """Evaluate a post-inline const expression to a boundary value."""
    if isinstance(node, BoolLit):
        return bool(node.value)
    if isinstance(node, IntLit):
        return int(node.value)
    if isinstance(node, FloatLit):
        return float(node.value)
    if isinstance(node, StrLit):
        return str(node.value)
    if isinstance(node, (UnaryOp, BinOp)):
        result = _eval_num(node)
        if isinstance(result, bool):
            return bool(result)
        return result
    if isinstance(node, CastExpr):
        value = eval_value(analyzer, node.operand)
        return _cast_value(analyzer, value, node.target)
    if isinstance(node, Name):
        return _eval_name(analyzer, node)
    if isinstance(node, StructConstruct):
        base = _ann_spelling(node)
        subst = _instance_subst(analyzer, base)
        fields = _struct_field_spellings(analyzer, base, subst)
        args = list(node.args or [])
        if len(args) != len(fields):
            raise EvalError(
                f"struct '{base}' expects {len(fields)} field values, "
                f"got {len(args)}"
            )
        return StructVal(base, [
            eval_value(analyzer, a) for a in args
        ])
    if isinstance(node, VectorLit):
        spelling = _ann_spelling(node)
        elem, _n = split_array_type(spelling) or (None, None)
        if elem is None:
            raise EvalError(
                f"container values ('{spelling}') cannot cross const-fn "
                "evaluation"
            )
        return ArrayVal([eval_value(analyzer, e) for e in node.elems])
    if isinstance(node, Attribute):
        obj = eval_value(analyzer, node.obj)
        if isinstance(obj, StructVal):
            base = obj.base
            subst = _instance_subst(analyzer, base)
            names = _struct_field_names(analyzer, base, subst)
            if node.name not in names:
                raise EvalError(f"struct '{base}' has no field '{node.name}'")
            return obj.fields[names.index(node.name)]
        raise EvalError("field access on a non-struct const-fn argument")
    if isinstance(node, Index):
        obj = eval_value(analyzer, node.obj)
        idx = eval_value(analyzer, node.index)
        if isinstance(obj, ArrayVal) and isinstance(idx, int):
            try:
                return obj.items[idx]
            except IndexError as exc:
                raise EvalError("array index out of bounds") from exc
        raise EvalError("indexing is only supported on fixed arrays")
    raise EvalError(
        f"'{type(node).__name__}' is not a constant value"
    )


def _ann_spelling(node: Node) -> str:
    from .closure import type_spelling

    ann = getattr(node, "_typed_ann", None) or {}
    info = ann.get("type")
    return type_spelling(info if isinstance(info, dict) else None)


def _instance_subst(analyzer: "_Analyzer", spelling: str) -> dict:
    base = spelling.split("<", 1)[0]
    decl = analyzer.structs.get(base) or analyzer.enums.get(base)
    if decl is None or not getattr(decl, "params", None):
        return {}
    args = _split_args(spelling)
    names = [p.name for p in decl.params]
    return {
        n: a for n, a in zip(names, args) if n and a
    }


def _struct_field_entries(
    analyzer: "_Analyzer", spelling: str, subst: dict
) -> list:
    base = spelling.split("<", 1)[0]
    decl = analyzer.structs.get(base)
    if decl is None:
        raise EvalError(f"unknown struct type '{spelling}'")
    entries = []
    for f in decl.fields:
        if getattr(f, "static", False):
            raise EvalError(
                f"struct '{base}' declares static fields and cannot cross "
                "const-fn evaluation"
            )
        ft = _type_str(f.type)
        if subst:
            ft = _subst_type_str(ft, subst)
        entries.append((f.name, analyzer._expand_type(ft) or ft))
    return entries


def _struct_field_spellings(
    analyzer: "_Analyzer", spelling: str, subst: dict
) -> list[str]:
    return [t for _n, t in _struct_field_entries(analyzer, spelling, subst)]


def _struct_field_names(
    analyzer: "_Analyzer", spelling: str, subst: dict
) -> list[str]:
    return [n for n, _t in _struct_field_entries(analyzer, spelling, subst)]


def _eval_name(analyzer: "_Analyzer", node: Name) -> CvValue:
    binding = getattr(node, "_typed_ann", None) or {}
    binding = binding.get("binding") or {}
    kind = binding.get("kind")
    if kind == "builtin" and binding.get("ref") == "None":
        return None
    if kind == "variant":
        ann = node._typed_ann
        spelling = _ann_spelling(node)
        base = spelling or str((ann.get("type") or {}).get("name") or "")
        idx = int(ann.get("variant_index", -1))
        variant = str(node.parts[-1]) if node.parts else ""
        return EnumVal(base, variant, [], idx)
    raise EvalError("only constant values can cross const-fn evaluation")


# ---------------------------------------------------------------------------
# ctypes construction / reading
# ---------------------------------------------------------------------------

_STRUCT_CACHE_ATTR = "_eval_struct_ctype_cache"
_ENUM_CACHE_ATTR = "_eval_enum_ctype_cache"


def ctype_for(analyzer: "_Analyzer", spelling: str):
    """ctypes type for one annotated boundary spelling."""
    if spelling == "None":
        return None  # void
    if spelling in _SCALAR_CTYPES:
        return _SCALAR_CTYPES[spelling]
    if spelling == "String":
        return ctypes.c_char_p
    if spelling.startswith("*const ") or spelling.startswith("*mut "):
        return ctypes.c_void_p
    if spelling.startswith("&"):
        # reference parameters arrive pointer-downgraded at the boundary
        return ctypes.c_void_p
    arr = split_array_type(spelling)
    if arr is not None:
        elem, n = arr
        ct: Any = ctype_for(analyzer, elem)
        if ct is None:
            raise EvalError(
                f"array element type '{elem}' cannot cross evaluation"
            )
        return ct * int(n)
    if spelling.startswith("fn("):
        raise EvalError(
            "function-pointer values cannot cross const-fn evaluation"
        )
    if spelling.startswith("Option<"):
        inner = _split_args(spelling)[0]
        if inner == "String" or inner.startswith(("*const ", "*mut ", "&")):
            # todo-88 nullable return convention
            return ctypes.c_char_p
        return _enum_ctype(analyzer, spelling)
    base = spelling.split("<", 1)[0]
    if base in analyzer.structs:
        return _struct_ctype(analyzer, spelling)
    if base in analyzer.enums:
        return _enum_ctype(analyzer, spelling)
    raise EvalError(f"cannot marshal type '{spelling}'")


def _struct_ctype(analyzer: "_Analyzer", spelling: str):
    cache = getattr(analyzer, _STRUCT_CACHE_ATTR, None)
    if cache is None:
        cache = {}
        setattr(analyzer, _STRUCT_CACHE_ATTR, cache)
    if spelling in cache:
        return cache[spelling]
    entries = _struct_field_entries(
        analyzer, spelling, _instance_subst(analyzer, spelling)
    )
    fields = [
        (name, ctype_for(analyzer, ft)) for name, ft in entries
    ]
    cls = type(
        f"CwStruct_{abs(hash(spelling)) % 10**8}",
        (ctypes.Structure,),
        {"_fields_": fields},
    )
    cache[spelling] = cls
    return cls


def _enum_payload_fields(
    analyzer: "_Analyzer", spelling: str
) -> Optional[list]:
    """[(field name, substituted spelling)] of the shared payload shape."""
    base = spelling.split("<", 1)[0]
    decl = analyzer.enums.get(base)
    if decl is None:
        raise EvalError(f"unknown enum type '{spelling}'")
    subst = _instance_subst(analyzer, spelling)
    shape = None
    shape_names: list[str] = []
    for v in decl.variants:
        if not v.fields:
            continue
        cur = []
        for ft_node in v.fields:
            ft = _type_str(ft_node)
            if subst:
                ft = _subst_type_str(ft, subst)
            cur.append(analyzer._expand_type(ft) or ft)
        if shape is None:
            shape = cur
            shape_names = [f.name for f in v.fields]
        elif tuple(cur) != tuple(shape):
            raise EvalError(
                f"enum '{spelling}' variants carry different payload "
                "shapes and cannot cross const-fn evaluation"
            )
    if shape is None:
        return None
    return list(zip(shape_names, shape))


def _enum_ctype(analyzer: "_Analyzer", spelling: str):
    cache = getattr(analyzer, _ENUM_CACHE_ATTR, None)
    if cache is None:
        cache = {}
        setattr(analyzer, _ENUM_CACHE_ATTR, cache)
    if spelling in cache:
        return cache[spelling]
    payload = _enum_payload_fields(analyzer, spelling)
    if payload is None:
        cache[spelling] = ctypes.c_int32
        return ctypes.c_int32
    fields = [("tag", ctypes.c_int32)]
    fields += [(name, ctype_for(analyzer, ft)) for name, ft in payload]
    cls = type(
        f"CwEnum_{abs(hash(spelling)) % 10**8}",
        (ctypes.Structure,),
        {"_fields_": fields},
    )
    cache[spelling] = cls
    return cls


def marshal(
    analyzer: "_Analyzer", value: CvValue, spelling: str
):
    """CvValue -> a ctypes argument for *spelling*."""
    if value is None:
        raise EvalError("void cannot be a const-fn argument")
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        data = value.encode("utf-8")
        if b"\x00" in data:
            raise EvalError(
                "String const-fn arguments cannot contain NUL bytes"
            )
        return data
    if isinstance(value, PtrVal):
        return value.address
    if isinstance(value, ArrayVal):
        arr_ctype: Any = ctype_for(analyzer, spelling)
        if arr_ctype is None:
            raise EvalError(f"'{spelling}' is not a marshalable array")
        return arr_ctype(*[
            marshal(analyzer, v, _array_elem(spelling))
            for v in value.items
        ])
    if isinstance(value, StructVal):
        cls: Any = ctype_for(analyzer, spelling)
        if cls is None:
            raise EvalError(f"'{spelling}' is not a marshalable struct")
        entries = _struct_field_entries(
            analyzer, spelling, _instance_subst(analyzer, spelling)
        )
        if len(entries) != len(value.fields):
            raise EvalError(
                f"struct '{spelling}' expects {len(entries)} field values"
            )
        inst: Any = cls()
        for (name, ft), fv in zip(entries, value.fields):
            setattr(inst, name, marshal(analyzer, fv, ft))
        return inst
    if isinstance(value, EnumVal):
        payload = _enum_payload_fields(analyzer, spelling)
        if payload is None:
            if value.payload:
                raise EvalError(
                    f"enum '{value.base}' carries no payload shape"
                )
            return int(value.variant_index)
        enum_cls: Any = ctype_for(analyzer, spelling)
        if enum_cls is None:
            raise EvalError(f"'{spelling}' is not a marshalable enum")
        inst = enum_cls()
        inst.tag = int(value.variant_index)
        if len(payload) != len(value.payload):
            raise EvalError(
                f"enum '{value.base}' payload expects {len(payload)} values"
            )
        for (name, ft), pv in zip(payload, value.payload):
            setattr(inst, name, marshal(analyzer, pv, ft))
        return inst
    raise EvalError(f"cannot marshal value {value!r}")


def _array_elem(spelling: str) -> str:
    arr = split_array_type(spelling)
    if arr is None:
        raise EvalError(f"'{spelling}' is not a fixed array")
    return arr[0]


def demarshal(analyzer: "_Analyzer", result, spelling: str) -> CvValue:
    """ctypes result -> CvValue for *spelling*."""
    if spelling == "None":
        return None
    if spelling == "Bool":
        return bool(result)
    if spelling in _SCALAR_CTYPES:
        return result
    if spelling == "String":
        if result is None:
            raise EvalError("const-fn returned a null String")
        raw = bytes(result) if isinstance(result, bytes) else str(result)
        if isinstance(raw, bytes):
            return raw.decode("utf-8", "replace")
        return str(result)
    if spelling.startswith("*const ") or spelling.startswith("*mut ") \
            or spelling.startswith("&"):
        return PtrVal(int(result or 0))
    arr = split_array_type(spelling)
    if arr is not None:
        elem = arr[0]
        return ArrayVal([
            demarshal(analyzer, item, elem) for item in result
        ])
    if spelling.startswith("Option<"):
        inner = _split_args(spelling)[0]
        if inner == "String" or inner.startswith(("*const ", "*mut ", "&")):
            return _option_from_nullable(analyzer, spelling, result)
        return _enum_from_ctypes(analyzer, result, spelling)
    base = spelling.split("<", 1)[0]
    if base in analyzer.structs:
        entries = _struct_field_entries(
            analyzer, spelling, _instance_subst(analyzer, spelling)
        )
        return StructVal(spelling, [
            demarshal(analyzer, getattr(result, name), ft)
            for name, ft in entries
        ])
    if base in analyzer.enums:
        return _enum_from_ctypes(analyzer, result, spelling)
    raise EvalError(f"cannot read back a value of type '{spelling}'")


def _option_from_nullable(
    analyzer: "_Analyzer", spelling: str, result
) -> EnumVal:
    base = spelling.split("<", 1)[0]
    inner = _split_args(spelling)[0]
    en = analyzer.enums.get(base)
    if en is None:
        raise EvalError(f"unknown enum type '{spelling}'")
    none_i = next(
        (i for i, v in enumerate(en.variants) if v.name == "None"), -1
    )
    some_i = next(
        (i for i, v in enumerate(en.variants) if v.name == "Some"), -1
    )
    if none_i < 0 or some_i < 0:
        raise EvalError(f"enum '{base}' is not Option-shaped")
    if result is None:
        return EnumVal(base, "None", [], none_i)
    if isinstance(result, bytes):
        text = result.decode("utf-8", "replace")
    elif isinstance(result, int):
        text = None
        return EnumVal(base, "Some", [PtrVal(int(result))], some_i)
    else:
        text = str(result)
    return EnumVal(base, "Some", [text], some_i)


def _enum_from_ctypes(
    analyzer: "_Analyzer", result, spelling: str
) -> EnumVal:
    base = spelling.split("<", 1)[0]
    en = analyzer.enums.get(base)
    if en is None:
        raise EvalError(f"unknown enum type '{spelling}'")
    if isinstance(result, int):  # fieldless
        idx = int(result)
        if not (0 <= idx < len(en.variants)):
            raise EvalError(f"enum discriminant {idx} out of range")
        return EnumVal(base, en.variants[idx].name, [], idx)
    idx = int(result.tag)
    if not (0 <= idx < len(en.variants)):
        raise EvalError(f"enum discriminant {idx} out of range")
    payload_spec = _enum_payload_fields(analyzer, spelling)
    if payload_spec is None:
        return EnumVal(base, en.variants[idx].name, [], idx)
    values = [
        demarshal(analyzer, getattr(result, name), ft)
        for name, ft in payload_spec
    ]
    return EnumVal(base, en.variants[idx].name, values, idx)


# ---------------------------------------------------------------------------
# burn: CvValue -> AST
# ---------------------------------------------------------------------------

def burn(
    analyzer: "_Analyzer",
    value: CvValue,
    original: Node,
    type_info: Optional[dict] = None,
) -> Node:
    """Build the AST replacement for *original* from *value*.

    The original call's annotations carry over (they are what SA wrote
    for the call's result type); ``type_info`` overrides the root type
    when the burned node takes the const declaration's slot.
    """
    line, column = original.line, original.column
    ann = copy.deepcopy(getattr(original, "_typed_ann", None) or {})
    ann.pop("call", None)
    if type_info is not None:
        ann["type"] = copy.deepcopy(type_info)

    if isinstance(value, bool):
        node: Node = BoolLit(line, column, value)
    elif isinstance(value, int):
        node = IntLit(line, column, int(value), str(int(value)))
    elif isinstance(value, float):
        node = FloatLit(line, column, float(value), repr(float(value)))
    elif isinstance(value, str):
        node = StrLit(line, column, value)
    elif value is None:
        node = Name(line, column, ["None"])
        ann = {
            "type": {"name": "None"},
            "binding": {"kind": "builtin", "ref": "None"},
        }
    elif isinstance(value, StructVal):
        node = _burn_struct(analyzer, value, line, column, ann)
        ann = node._typed_ann
    elif isinstance(value, ArrayVal):
        node = _burn_array(analyzer, value, line, column, ann, type_info)
        ann = node._typed_ann
    elif isinstance(value, EnumVal):
        node = _burn_enum(analyzer, value, line, column, ann, original)
        ann = node._typed_ann
    elif isinstance(value, PtrVal):
        raise EvalError(
            "pointer results cannot be burned into a const initializer"
        )
    else:
        raise EvalError(f"cannot burn value {value!r}")
    node._typed_ann = ann
    return node


def _burn_struct(
    analyzer: "_Analyzer",
    value: StructVal,
    line: int,
    column: int,
    ann: dict,
) -> StructConstruct:
    base = value.base
    bare = base.split("<", 1)[0]
    subst = _instance_subst(analyzer, base)
    entries = _struct_field_entries(analyzer, base, subst)
    if len(entries) != len(value.fields):
        raise EvalError(f"struct '{base}' field count mismatch")
    args = [
        burn(analyzer, fv, _FakePos(line, column), ft)
        for (name, ft), fv in zip(entries, value.fields)
    ]
    decl = analyzer.structs[bare]
    tnode = Type(
        line, column, bare,
        [Type(line, column, a) for a in _split_args(base)]
        if "<" in base else [],
    )
    node = StructConstruct(line, column, tnode, args)
    node._typed_ann = ann
    return node


class _FakePos(Node):
    def __init__(self, line: int, column: int) -> None:
        super().__init__(line, column)


def _burn_array(
    analyzer: "_Analyzer",
    value: ArrayVal,
    line: int,
    column: int,
    ann: dict,
    type_info: Optional[dict],
) -> VectorLit:
    spelling = type_spelling_of(type_info) or ""
    elem, _n = split_array_type(spelling) or (None, None)
    elems = [
        burn(analyzer, item, _FakePos(line, column), None)
        for item in value.items
    ]
    node = VectorLit(line, column, elems)
    out = copy.deepcopy(ann)
    if type_info is not None:
        out["type"] = copy.deepcopy(type_info)
    if elem:
        out["element_type"] = _spelling_to_type_dict(elem)
    node._typed_ann = out
    return node


def type_spelling_of(info: Optional[dict]) -> str:
    from .closure import type_spelling

    return type_spelling(info)


def _burn_enum(
    analyzer: "_Analyzer",
    value: EnumVal,
    line: int,
    column: int,
    ann: dict,
    original: Node,
) -> Node:
    from .closure import type_spelling  # noqa: F401  (spelling helper)

    base = value.base.split("<", 1)[0]
    en = analyzer.enums.get(base)
    if en is None:
        raise EvalError(f"unknown enum type '{value.base}'")
    idx = value.variant_index
    if not (0 <= idx < len(en.variants)):
        raise EvalError(f"enum '{base}' variant index out of range")
    variant = en.variants[idx]
    if variant.name != value.variant and value.variant:
        # trust the index (positional discriminants)
        pass
    variant_tid = getattr(variant, "_typed_id", None)
    def_path = None
    try:
        def_path = analyzer._type_def_path(base)
    except Exception:
        def_path = None
    callee = Name(line, column, [base, str(variant.name)])
    callee_ann = {
        "type": {"name": base},
        "binding": {"kind": "variant", "ref": variant_tid},
        "variant_index": idx,
    }
    if def_path:
        callee_ann["enum_def"] = def_path
    callee._typed_ann = callee_ann
    if not value.payload:
        node: Node = callee
        out = copy.deepcopy(ann)
        out.pop("call", None)
        out["type"] = {"name": base}
        out["binding"] = {"kind": "variant", "ref": variant_tid}
        out["variant_index"] = idx
        if def_path:
            out["enum_def"] = def_path
        node._typed_ann = out
        return node
    subst = _instance_subst(analyzer, value.base)
    payload_spec = _enum_payload_fields(analyzer, value.base) or []
    if len(payload_spec) != len(value.payload):
        raise EvalError(f"enum '{base}' payload count mismatch")
    args = [
        Arg(line, column, burn(analyzer, pv, _FakePos(line, column), ft))
        for (name, ft), pv in zip(payload_spec, value.payload)
    ]
    node = Call(line, column, callee, args)
    out = copy.deepcopy(ann)
    out["type"] = _spelling_to_type_dict(value.base)
    out["enum"] = base
    if def_path:
        out["enum_def"] = def_path
    out["variant_index"] = idx
    out["payload_types"] = [
        _type_info_of(analyzer, ft) for _n, ft in payload_spec
    ]
    out["call"] = {
        "callee_kind": "enum_variant",
        "callee_ref": str(variant.name),
    }
    node._typed_ann = out
    return node


def _type_info_of(analyzer: "_Analyzer", spelling: str) -> dict:
    info = analyzer._type_info_enriched(spelling)
    return info if isinstance(info, dict) else {"name": spelling}
