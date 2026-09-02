"""Shared declaration-check machinery: the single definition point
for every module-level name used by the declaration mixins
(``collect``/``types_check``/``extern``/``impls``/``misc``)."""

from __future__ import annotations

from typing import Optional

from ...ast_components.ast import (
    ConstDecl,
    EnumDecl,
    FnDecl,
    GroupDecl,
    Node,
    StructDecl,
    TraitDecl,
    TypeDecl,
)
from ..types import _INTEGER


# C-ABI-compatible scalar types for extern declarations (todo-48).
_EXTERN_SCALAR_TYPES: frozenset[str] = frozenset({
    "Int", "UInt", "Int8", "UInt8", "Byte", "Bool",
    "Int16", "UInt16",
    "Int32", "UInt32", "Int64", "UInt64", "Float", "Float64",
})

# todo-52: 各标量的 C 字节宽度 (与后端 cg_scalar_bytes 一致)
_EXTERN_SCALAR_WIDTHS: dict[str, int] = {
    "Int": 2, "UInt": 2,
    "Int8": 1, "UInt8": 1, "Byte": 1, "Bool": 1,
    "Int16": 2, "UInt16": 2,
    "Int32": 4, "UInt32": 4, "Float": 4,
    "Int64": 8, "UInt64": 8, "Float64": 8,
}

# todo-66: 内嵌纯内联结构体的最大嵌套层数 (与后端 CG_EXT_MAX_NEST 一致)
_EXTERN_MAX_NEST: int = 4

# main 允许的返回类型 (bug-24): 整数类型作为进程退出码 (与后端
# cg_emit_main_wrapper/cg_is_int 一致, 含 Byte), None/省略, 以及
# never 类型 `!` (Rust 语义: 永不返回的 main 合法)。
_MAIN_RETURN_TYPES: frozenset[str] = _INTEGER | {"Byte", "None", "!"}


def _decl_kind_name(item: Node) -> Optional[tuple[str, str]]:
    if isinstance(item, ConstDecl):
        return "const", item.name
    if isinstance(item, TypeDecl):
        return "type", item.name
    if isinstance(item, StructDecl):
        return "struct", item.name
    if isinstance(item, EnumDecl):
        return "enum", item.name
    if isinstance(item, TraitDecl):
        return "trait", item.name
    if isinstance(item, FnDecl):
        return "fn", item.name
    if isinstance(item, GroupDecl):
        return "group", item.name
    return None  # ExtraDecl / ImplDecl / GroupApply are not symbols
