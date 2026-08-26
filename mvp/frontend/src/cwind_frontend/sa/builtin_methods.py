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
    "BUILTIN_TRAIT_ARITY",
    "BUILTIN_TRAIT_METHOD_NAMES",
    "BUILTIN_TRAIT_METHODS",
    "BUILTIN_TYPE_METHODS",
    "BUILTIN_TYPE_TRAITS",
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
    for prefix in ("SameAsGeneric:", "TraitArg:"):
        if not typ.startswith(prefix):
            continue
        suffix = typ[len(prefix):]
        if not suffix.isdigit() or int(suffix) < 1:
            raise ValueError(
                f"{prefix[:-1]} suffix must be a positive integer, got {typ!r}"
            )
        return


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


def _split_type_args(inner: str) -> list[str]:
    """Split ``A, B<C, D>`` at the top level of a trait argument list."""
    parts: list[str] = []
    depth = 0
    start = 0
    for i, ch in enumerate(inner):
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
            if depth < 0:
                raise ValueError(
                    f"unbalanced '>' in trait argument list {inner!r}"
                )
        elif ch == "," and depth == 0:
            parts.append(inner[start:i].strip())
            start = i + 1
    if depth != 0:
        raise ValueError(
            f"unbalanced '<' in trait argument list {inner!r}"
        )
    parts.append(inner[start:].strip())
    return [p for p in parts if p]


def _parse_trait_ref(ref: str) -> tuple[str, list[str]]:
    """Split ``"From<String>"`` into ``("From", ["String"])``."""
    if "<" not in ref:
        return ref.strip(), []
    name, _, rest = ref.partition("<")
    if not rest.endswith(">"):
        raise ValueError(f"malformed trait reference {ref!r} (missing '>')")
    return name.strip(), _split_type_args(rest[:-1])


def _trait_arity(
    trait_methods: dict[str, MethodSpec], method_names: list[str]
) -> int:
    """A trait's arity is the largest TraitArg:N its methods reference."""
    arity = 0
    for mname in method_names:
        spec = trait_methods[mname]
        for typ in (*spec.args, spec.returns):
            if typ.startswith("TraitArg:"):
                arity = max(arity, int(typ[len("TraitArg:"):]))
    return arity


def _instantiate_spec(
    spec: MethodSpec, trait_args: list[str], trait_name: str
) -> MethodSpec:
    """Bind ``TraitArg:N`` in a trait method to the trait's type arguments."""

    def bind(typ: str) -> str:
        if not typ.startswith("TraitArg:"):
            return typ
        idx = int(typ[len("TraitArg:"):])
        if idx < 1 or idx > len(trait_args):
            raise ValueError(
                f"method '{spec.name}' of trait '{trait_name}' references "
                f"TraitArg:{idx} but the trait was instantiated with "
                f"{len(trait_args)} argument(s)"
            )
        return trait_args[idx - 1]

    if not trait_args and not any(
        t.startswith("TraitArg:") for t in (*spec.args, spec.returns)
    ):
        return spec
    return MethodSpec(
        name=spec.name,
        args=tuple(bind(a) for a in spec.args),
        returns=bind(spec.returns),
    )


def _load_text(
    text: bytes,
) -> tuple[
    frozenset[str],
    dict[str, int],
    dict[str, list[str]],
    dict[str, MethodSpec],
    dict[str, dict[str, MethodSpec]],
    dict[str, MethodSpec],
    dict[str, str],
    dict[str, frozenset[str]],
]:
    data: dict[str, Any] = tomllib.loads(text.decode("utf-8"))

    traits: dict[str, Any] = data["traits"]
    trait_methods: dict[str, MethodSpec] = {
        name: _spec(name, entry)
        for name, entry in data["trait_methods"].items()
    }
    builtin_trait_arity: dict[str, int] = {
        name: _trait_arity(trait_methods, method_names)
        for name, method_names in traits.items()
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

    type_traits: dict[str, set[str]] = {}
    type_methods: dict[str, dict[str, MethodSpec]] = {}
    for type_name, type_data in data["types"].items():
        methods: dict[str, MethodSpec] = {}
        impls: set[str] = set()
        for mname, entry in type_data.get("methods", {}).items():
            methods[mname] = _spec(mname, entry)
        for trait_ref in type_data.get("traits", []):
            trait_name, trait_args = _parse_trait_ref(trait_ref)
            if trait_name not in traits:
                raise ValueError(
                    f"type '{type_name}' references unknown trait "
                    f"{trait_name!r}"
                )
            expected = _trait_arity(trait_methods, traits[trait_name])
            if len(trait_args) != expected:
                raise ValueError(
                    f"trait '{trait_name}' takes {expected} type argument(s), "
                    f"got {len(trait_args)} in {trait_ref!r} "
                    f"(on type '{type_name}')"
                )
            # bug-31: remember the shipped instantiation in canonical form
            # so user re-implementations can be rejected.
            impls.add(
                trait_name
                if not trait_args
                else f"{trait_name}<{', '.join(trait_args)}>"
            )
            for mname in traits[trait_name]:
                if mname not in methods:
                    inst = _instantiate_spec(
                        trait_methods[mname], trait_args, trait_name
                    )
                    methods[mname] = inst
                elif (
                    trait_args
                    and methods[mname] != _instantiate_spec(
                        trait_methods[mname], trait_args, trait_name
                    )
                ):
                    raise ValueError(
                        f"conflicting definitions of '{mname}' on type "
                        f"'{type_name}' (explicit method vs trait "
                        f"{trait_ref!r})"
                    )
        type_methods[type_name] = methods
        type_traits[type_name] = impls

    module_functions: dict[str, MethodSpec] = {
        name: _spec(name, entry)
        for name, entry in data["modules"]["builtins"].items()
    }
    return (
        frozenset(traits),
        builtin_trait_arity,
        {name: list(names) for name, names in traits.items()},
        trait_methods,
        type_methods,
        module_functions,
        builtin_objects,
        {
            name: frozenset(impls)
            for name, impls in type_traits.items()
        },
    )


def _load() -> tuple[
    frozenset[str],
    dict[str, int],
    dict[str, list[str]],
    dict[str, MethodSpec],
    dict[str, dict[str, MethodSpec]],
    dict[str, MethodSpec],
    dict[str, str],
    dict[str, frozenset[str]],
]:
    path = Path(__file__).with_name("builtin_methods.toml")
    with open(path, "rb") as fh:
        return _load_text(fh.read())


(
    BUILTIN_TRAITS,
    BUILTIN_TRAIT_ARITY,
    BUILTIN_TRAIT_METHOD_NAMES,
    BUILTIN_TRAIT_METHODS,
    BUILTIN_TYPE_METHODS,
    BUILTIN_MODULE_FUNCTIONS,
    BUILTIN_OBJECTS,
    BUILTIN_TYPE_TRAITS,
) = _load()
