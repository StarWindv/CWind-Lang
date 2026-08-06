"""
Built-in traits, methods and module functions for semantic analysis.

Data-driven: the definitions live in ``builtin_methods.toml`` next to this
module, so adding a trait or a method is a data edit, not a code change.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "BUILTIN_TRAITS",
    "BUILTIN_TYPE_METHODS",
    "BUILTIN_MODULE_FUNCTIONS",
    "MethodSpec",
]


@dataclass(frozen=True)
class MethodSpec:
    """Declared signature of a built-in method or module function."""

    name: str
    args: tuple[str, ...]
    returns: str
    variadic: bool = False


def _spec(name: str, entry: dict[str, Any]) -> MethodSpec:
    return MethodSpec(
        name=name,
        args=tuple(str(a) for a in entry.get("args", [])),
        returns=str(entry.get("returns", "None")),
        variadic=bool(entry.get("variadic", False)),
    )


def _load() -> tuple[
    frozenset[str],
    dict[str, dict[str, MethodSpec]],
    dict[str, MethodSpec],
]:
    path = Path(__file__).with_name("builtin_methods.toml")
    with open(path, "rb") as fh:
        data: dict[str, Any] = tomllib.load(fh)

    traits: dict[str, Any] = data["traits"]
    trait_methods: dict[str, MethodSpec] = {
        name: _spec(name, entry)
        for name, entry in data["trait_methods"].items()
    }

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
    return frozenset(traits), type_methods, module_functions


BUILTIN_TRAITS, BUILTIN_TYPE_METHODS, BUILTIN_MODULE_FUNCTIONS = _load()
