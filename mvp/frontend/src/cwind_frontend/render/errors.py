"""Fancy rendering of CWind frontend errors using ariadne_py
(My own unofficial pure Python port version, without using pyo3).

The rendering layer mirrors the Rust `ariadne` API: a :class:`LexError`
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

from ..ast_components.errors import FrontendError

__all__ = ["offset_for_position", "render_error", "render_warning"]


def _capitalize(message: str) -> str:
    """Capitalize the first character only, keeping the rest intact."""
    return message[:1].upper() + message[1:] if message else message


def _trim_below_context(rendered: str, message: str) -> str:
    """Drop the context lines ariadne draws below the label, keeping the
    closing frame, so extra context appears only above the error."""
    lines = rendered.split("\n")
    if not any(message in line for line in lines):
        return rendered
    last_msg = max(i for i, line in enumerate(lines) if message in line)
    closing = next(i for i in range(len(lines) - 1, -1, -1) if lines[i].strip())
    if closing <= last_msg:
        return rendered
    return "\n".join([*lines[:last_msg + 1], lines[closing]])


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
    context_lines: int = 2,
) -> str:
    """Render an error diagnostic (red ``Error`` header)."""
    return _render_diagnostic(
        error,
        source_text,
        source_name=source_name,
        color=color,
        message_color=message_color,
        context_lines=context_lines,
        kind=ReportKind.Error,
    )


def render_warning(
    warning: FrontendError,
    source_text: str,
    *,
    source_name: Optional[str] = None,
    color: bool = True,
    message_color: Optional[Color] = None,
    context_lines: int = 2,
) -> str:
    """Render a warning diagnostic (yellow ``Warning`` header)."""
    return _render_diagnostic(
        warning,
        source_text,
        source_name=source_name,
        color=color,
        message_color=message_color,
        context_lines=context_lines,
        kind=ReportKind.Warning,
    )


def _render_diagnostic(
    error: FrontendError,
    source_text: str,
    *,
    source_name: Optional[str],
    color: bool,
    message_color: Optional[Color],
    context_lines: int,
    kind,
) -> str:
    """Render a :class:`LexError` as an ariadne diagnostic string.

    The report header shows the broad error class
    (``error.category``, falling back to the message) while the label after
    the arrow shows the specific message, so the two lines do not have to be
    identical.  Only the label text is highlighted in ``message_color`` (cyan
    by default); the report header, source code and arrows keep their default
    styling.  ``context_lines`` extra source lines are shown above the error
    (ariadne draws them symmetrically; the part below the label is trimmed).
    """
    headline = _capitalize(error.category or error.message)
    message = _capitalize(error.message)
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
    config.with_context_lines(context_lines)
    if not color:
        config.with_color(False).with_ansi_mode(AnsiMode.OFF)

    # Ariadne's in-line style tags (same mechanism as the Rust crate's
    # `Styleable`), used because this port draws message text unstyled.
    styled_message = str(Styleable.style(message, style_name))
    report = (
        Report.build(kind, span)
        .with_config(config)
        .with_message(headline)
        .with_label(Label(span).with_message(styled_message))
        .finish()
    )
    rendered = report.write_to_string(cache)
    if context_lines > 0:
        rendered = _trim_below_context(rendered, message)
    return rendered
