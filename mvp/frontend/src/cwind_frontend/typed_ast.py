"""Typed-AST serialization (spec: ``ProxyRegulations/TypedAST.md``)."""

from __future__ import annotations

from typing import Any, Optional

from .ast_components.ast import Node
from .sa import ProgramInfo

__all__ = ["build_typed_ast"]


def build_typed_ast(
    program: Node,
    info: ProgramInfo,
    source: Optional[str] = None,
) -> dict[str, Any]:
    """Assemble the ``cwind-typed-ast`` envelope.

    The AST is serialized with every node carrying its pre-order ``id`` and
    the ``ann`` dictionary filled in by the semantic analyzer; ``symbols`` /
    ``bindings`` reference those ids.  ``source`` (todo-63) is the absolute
    path of the compiled source file when known; the backend uses its
    directory to resolve ``#[link(path = "...", relative = "source")]``.
    """
    symbols = [
        {"name": sym.name, "kind": sym.kind, "ref": sym.ref}
        for sym in info.symbols.values()
    ]
    bindings = [binding.to_dict() for binding in info.bindings]
    if info.import_manifest:
        # todo-76/78: one entry per ``use`` declaration with its own
        # resolved source file (the implicit prelude included, flagged
        # ``auto``).
        imports = [dict(entry) for entry in info.import_manifest]
    else:
        imports = [
            {"path": list(parts), "source": next(iter(info.imported_modules), None)}
            for parts in info.modules.values()
        ]
    return {
        "format": "cwind-typed-ast",
        "version": 1,
        "source": source,
        "imports": imports,
        "symbols": symbols,
        "bindings": bindings,
        "ast": program.to_dict(include_meta=True),
    }
