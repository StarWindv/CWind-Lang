"""Procedural macro definitions (todo-179): the ``#[proc_macro]`` record.

A definition is one CWind ``fn`` annotated ``#[proc_macro]`` (optionally
``pub``).  Unlike ``macro_rules!`` definitions, the body is *not* template
syntax: it is ordinary CWind code that will be compiled and executed as a
standalone program.  The record keeps the item's tokens so the build step
can generate the standalone program without re-reading the source file,
plus the file's full token list for local dependency collection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ...ast_components.token import Token

__all__ = ["ProcMacroDef"]


@dataclass
class ProcMacroDef:
    """One ``#[proc_macro] [pub] fn name(...) -> ... { ... }`` definition."""

    name: str
    is_pub: bool
    name_token: Token
    # The item's tokens with the ``#[proc_macro]`` attribute removed but
    # the signature/body intact (the temp program compiles this as a
    # plain function).  ``fn_start`` is the token's index in ``file_tokens``.
    fn_tokens: list[Token]
    fn_start: int
    fn_end: int
    # Every token of the defining file (dependency collection reads it).
    file_tokens: list[Token] = field(default_factory=list)
    source_path: Optional[str] = None
    # Fatal definition-shape problems, reported where the item sits.
    issues: list = field(default_factory=list)
    kind: str = "function"

    @property
    def function_name(self) -> str:
        return str(self.name_token.value)

    def identity(self) -> tuple:
        """Stable identity for registry dedup (same item seen twice)."""
        return (
            self.source_path,
            self.name,
            self.kind,
            self.name_token.line,
            self.name_token.column,
        )
