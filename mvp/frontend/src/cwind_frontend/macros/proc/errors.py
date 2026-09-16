"""Procedural macro errors (todo-179)."""

from __future__ import annotations

from typing import Optional

from ...ast_components.errors import FrontendError

__all__ = ["ProcMacroError"]


class ProcMacroError(FrontendError):
    """A procedural-macro diagnostic (definition or expansion)."""

    def __init__(
        self,
        message: str,
        line: int,
        column: int,
        *,
        end_line: Optional[int] = None,
        end_column: Optional[int] = None,
        category: Optional[str] = None,
        source: Optional[str] = None,
    ) -> None:
        super().__init__(
            message,
            line,
            column,
            end_line=end_line,
            end_column=end_column,
            category=category,
            source=source,
        )
