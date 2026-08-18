"""Semantic-analysis error and result types."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..ast_components.errors import FrontendError
from .symbols import ProgramInfo

__all__ = ["SaError", "SaResult", "SaWarning"]


class SaError(FrontendError):
    """Raised for semantic-level problems (as opposed to lexer/parser errors)."""


class SaWarning(FrontendError):
    """A non-blocking semantic-level warning (e.g. a refinement predicate
    that can never fail for its base type)."""


@dataclass
class SaResult:
    """SA result plus every recovered semantic error."""

    info: "ProgramInfo"
    errors: list[SaError]
    warnings: list[SaWarning] = field(default_factory=list)
