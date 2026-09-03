"""Macro definitions (todo-44): the parsed ``macro_rules!`` item.

A definition is a name plus one or more ``(matcher) => { body }`` rules,
each already folded into :mod:`.trees` groups.  Definitions live in a
per-file registry (see :mod:`cwind_frontend.macros.expansion`); they are
not part of the serialized AST — after expansion they are gone, exactly
like rustc's ``macro_rules`` items which vanish into the resolver.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..ast_components.token import Token
from .trees import Group

__all__ = ["MacroRule", "MacroDef"]


@dataclass
class MacroRule:
    """One ``(matcher) => { body };`` rule."""

    matcher: Group            # the ( ... ) group with Binding structure
    body: Group               # the { ... } group used as template
    def_token: Token          # the matcher's open delimiter (position source)


@dataclass
class MacroDef:
    """One complete macro definition: name + rules + validation state."""

    name: str
    rules: list[MacroRule] = field(default_factory=list)
    name_token: Optional[Token] = None
    # Definition-time problems found by validation; a macro with issues
    # is registered anyway (matching is skipped for it) so the driver can
    # report them once and recover.
    issues: list = field(default_factory=list)
