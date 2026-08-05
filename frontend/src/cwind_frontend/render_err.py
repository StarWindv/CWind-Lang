"""Fancy rendering of CWind frontend errors using ariadne_py.

The rendering layer mirrors the Rust ``ariadne`` API: a :class:`LexError`
(1-based line/column) is converted into a character span over the source text
and rendered as a colored diagnostic report.

Usage::

    try:
        tokens = tokenize(source)
    except LexError as exc:
        print(render_error(exc, source, source_name="main.cw"), file=sys.stderr)

``source_name`` is optional; when given, spans become ``(name, (start, end))``
and the report header shows the file name instead of ``<unknown>``.
"""

from __future__ import annotations

from typing import Optional

from ariadne_py import (
    AnsiMode,
    Color,
    Config,
    Label,
    Report,
    ReportKind,
    Source,
    Style,
    Styleable,
)

from .ast_components.errors import FrontendError
from .lexer import LexError

__all__ = ["offset_for_position", "render_error"]


def offset_for_position(source: Source, line: int, column: int) -> int:
    """Convert a 1-based ``(line, column)`` into a character offset.

    Positions past the end of the source are clamped to ``Source.len()``.
    """
    line_obj = source.line(line - 1)
    if line_obj is None:
        return source.len()
    return min(line_obj.offset + max(column - 1, 0), source.len())


def render_error(
    error: FrontendError,
    source_text: str,
    *,
    source_name: Optional[str] = None,
    color: bool = True,
    message_color: Optional[Color] = None,
) -> str:
    """Render a :class:`LexError` as an ariadne diagnostic string.

    Only the label message (the text after the arrow, e.g.
    ``╰────── unterminated string literal``) is highlighted in
    ``message_color`` (cyan by default); the report header, source code and
    arrows keep their default styling.
    """
    source = Source(source_text)
    start = offset_for_position(source, error.line, error.column)
    end = offset_for_position(source, error.end_line, error.end_column)
    if end <= start:
        end = min(start + 1, source.len())
    if start > source.len():
        start = end = source.len()

    span = (source_name, (start, end)) if source_name is not None else (start, end)
    cache = (source_name, source) if source_name is not None else source

    message_color = Color.Cyan if message_color is None else message_color
    style_name = "cw_message"
    config = Config().with_style(style_name, Style(fg=message_color))
    if not color:
        config.with_color(False).with_ansi_mode(AnsiMode.OFF)

    # ariadne's in-line style tags (same mechanism as the Rust crate's
    # `Styleable`), used because this port draws message text unstyled.
    styled_message = str(Styleable.style(error.message, style_name))
    report = (
        Report.build(ReportKind.Error, span)
        .with_config(config)
        .with_message(error.message)
        .with_label(Label(span).with_message(styled_message))
        .finish()
    )
    return report.write_to_string(cache)
