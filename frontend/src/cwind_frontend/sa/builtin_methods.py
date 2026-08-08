"""
Built-in traits, methods and module functions for semantic analysis.

Data-driven: the definitions live in ``builtin_methods.toml`` next to this
module, so adding a trait or a method is a data edit, not a code change.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "BUILTIN_OBJECTS",
    "BUILTIN_TRAITS",
    "BUILTIN_TYPE_METHODS",
    "BUILTIN_MODULE_FUNCTIONS",
    "MethodSpec",
    "parse_arg_patterns",
]


@dataclass(frozen=True)
class MethodSpec:
    """Declared signature of a built-in method or module function."""

    name: str
    args: tuple[str, ...]
    returns: str

    @property
    def patterns(self) -> tuple[tuple[Optional[int], str], ...]:
        """Normalized ``(count, type)`` pairs for :meth:`args`.

        ``count`` is a positive integer for fixed repeats, or ``None`` for an
        unbounded tail (``"*: Type"``, always the final entry).
        """
        return parse_arg_patterns(self.args)


def parse_arg_patterns(
    args: tuple[str, ...],
) -> tuple[tuple[Optional[int], str], ...]:
    """Normalize declared arg patterns to ``(count, type)`` pairs.

    Supported entry forms:

    * ``"Type"``    — exactly one argument of ``Type``
    * ``"N: Type"`` — exactly ``N`` consecutive arguments of ``Type``
    * ``"*: Type"`` — any number of arguments of ``Type`` (must be last)

    The count prefix uses ``N: Type`` / ``*: Type``; a type may itself carry a
    suffix (``SameAsGeneric:1``), so a colon is only treated as a count
    separator when the text before it is ``*`` or an integer.  Malformed
    entries raise :class:`ValueError` so mistakes in the TOML are caught at
    load time instead of silently misbehaving later.
    """
    patterns: list[tuple[Optional[int], str]] = []
    for i, entry in enumerate(args):
        count_str, sep, typ = entry.partition(":")
        if not sep or (count_str.strip() != "*" and not count_str.strip().isdigit()):
            patterns.append((1, entry))
            continue
        count_str = count_str.strip()
        typ = typ.strip()
        if not typ:
            raise ValueError(f"arg pattern {entry!r} is missing its type")
        if count_str == "*":
            if i != len(args) - 1:
                raise ValueError(
                    f"unbounded arg pattern {entry!r} must be the last entry"
                )
            patterns.append((None, typ))
            continue
        try:
            count = int(count_str)
        except ValueError:
            raise ValueError(
                f"invalid arg count {count_str!r} in pattern {entry!r}"
            ) from None
        if count < 1:
            raise ValueError(f"arg count must be >= 1 in pattern {entry!r}")
        patterns.append((count, typ))
    return tuple(patterns)


def _validate_type_ref(typ: str) -> None:
    """Reject malformed generic-position suffixes at load time."""
    if not typ.startswith("SameAsGeneric:"):
        return
    suffix = typ[len("SameAsGeneric:"):]
    if not suffix.isdigit() or int(suffix) < 1:
        raise ValueError(
            f"SameAsGeneric suffix must be a positive integer, got {typ!r}"
        )


def _spec(name: str, entry: dict[str, Any]) -> MethodSpec:
    args = tuple(str(a) for a in entry.get("args", []))
    # Validate the patterns now so a bad TOML entry fails loudly at import.
    for _, typ in parse_arg_patterns(args):
        _validate_type_ref(typ)
    returns = str(entry.get("returns", "None"))
    _validate_type_ref(returns)
    return MethodSpec(
        name=name,
        args=args,
        returns=returns,
    )


def _load() -> tuple[
    frozenset[str],
    dict[str, dict[str, MethodSpec]],
    dict[str, MethodSpec],
    dict[str, str],
]:
    path = Path(__file__).with_name("builtin_methods.toml")
    with open(path, "rb") as fh:
        data: dict[str, Any] = tomllib.load(fh)

    traits: dict[str, Any] = data["traits"]
    trait_methods: dict[str, MethodSpec] = {
        name: _spec(name, entry)
        for name, entry in data["trait_methods"].items()
    }

    builtin_objects: dict[str, str] = {}
    for obj_name, obj in data.get("objects", {}).items():
        if isinstance(obj, str):
            builtin_objects[obj_name] = obj
        elif isinstance(obj, dict) and "type" in obj:
            builtin_objects[obj_name] = str(obj["type"])
        else:
            raise ValueError(
                f"built-in object {obj_name!r} must be a type string or "
                "a table with a 'type' key"
            )

    type_methods: dict[str, dict[str, MethodSpec]] = {}
    for type_name, type_data in data["types"].items():
        methods: dict[str, MethodSpec] = {}
        for mname, entry in type_data.get("methods", {}).items():
            methods[mname] = _spec(mname, entry)
        for trait in type_data.get("traits", []):
            for mname in traits[trait]:
                if mname not in methods:
                    methods[mname] = trait_methods[mname]
        type_methods[type_name] = methods

    module_functions: dict[str, MethodSpec] = {
        name: _spec(name, entry)
        for name, entry in data["modules"]["builtins"].items()
    }
    return frozenset(traits), type_methods, module_functions, builtin_objects


(
    BUILTIN_TRAITS,
    BUILTIN_TYPE_METHODS,
    BUILTIN_MODULE_FUNCTIONS,
    BUILTIN_OBJECTS,
) = _load()
