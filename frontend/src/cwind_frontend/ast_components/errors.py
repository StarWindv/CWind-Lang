"""Shared base for frontend diagnostics (lexer and parser errors)."""

from __future__ import annotations

from typing import Optional


class FrontendError(Exception):
    """Base for errors with 1-based source positions.

    ``end_column`` is exclusive (Python-slice style), matching
    :class:`~cwind_frontend.ast_components.token.Token`.  When the end is
    unknown it defaults to the single character at ``(line, column)``.
    """

    def __init__(
        self,
        message: str,
        line: int,
        column: int,
        *,
        end_line: Optional[int] = None,
        end_column: Optional[int] = None,
        category: Optional[str] = None,
    ) -> None:
        self.message = message
        self.category = category
        self.line = line
        self.column = column
        self.end_line = line if end_line is None else end_line
        self.end_column = column + 1 if end_column is None else end_column
        super().__init__(f"{message} (line {line}, column {column})")
