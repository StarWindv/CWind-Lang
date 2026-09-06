"""Diagnostic rendering for the CWind frontend, driven by the tgqe pipeline.

Every frontend diagnostic (lexer / parser / SA / macro-preprocessor) is
converted into a :class:`tgqe.TgqeCtx` and handed to the tgqe error bus
(:class:`tgqe.ICC`), which renders it through its own renderer.  This
module owns only the conversion: source storage, position mapping and
the publisher (error-source) identity of each stage.

Publishers (tgqe's error-source tag, one per pipeline stage):

- ``lexer``         — lexical errors and lexer warnings;
- ``parser``        — grammar errors;
- ``preprocessor``  — the macro-rules expansion pass;
- ``sa``            — semantic analysis.

The rendered layout follows the tgqe examples: the headline carries the
error kind only (``Error: <err_type>``), the got/expected details ride
the code span label, and a note block records the publisher / label /
hints metadata.  Expansion-chain notes (macro backtrace) ride the same
mechanism through the hints field.
"""

from __future__ import annotations

import io
import re
import sys
import time
from contextlib import redirect_stderr
from typing import Optional, Sequence

from tgqe import (
    ICC,
    SourceManager,
    TgqeCoordinate,
    TgqeCtx,
    TgqeErrorInfo,
    TgqeLevelFilter,
    TgqePosition,
    TgqeSpan,
)

from ..ast_components.errors import FrontendError

__all__ = [
    "PUBLISHER_LEXER",
    "PUBLISHER_PARSER",
    "PUBLISHER_SA",
    "PUBLISHER_PREPROCESSOR",
    "offset_for_position",
    "publisher_for",
    "error_context",
    "report_contexts",
    "render_error",
    "render_warning",
]

# tgqe publisher identities, one per pipeline stage.
PUBLISHER_LEXER = "lexer"
PUBLISHER_PARSER = "parser"
PUBLISHER_SA = "sa"
PUBLISHER_PREPROCESSOR = "preprocessor"

# Headline error kind (tgqe ``err_type``) per publisher: the big error
# category of the stage that produced the diagnostic.
_STAGE_CLASSES = {
    PUBLISHER_LEXER: "lexical error",
    PUBLISHER_PARSER: "syntax error",
    PUBLISHER_SA: "semantic error",
    PUBLISHER_PREPROCESSOR: "macro error",
}

# Sources with no file identity (stdin / in-memory) share this key.
_UNNAMED_SOURCE = "<stdin>"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# tgqe renders the headline as ``<kind>: <err_type>: <got>``; with the
# got/expected details moved onto the code span label the ``got`` slot
# renders empty and the headline would end in a dangling ": " — trim it.
_EMPTY_GOT_TAIL_RE = re.compile(
    r"(?P<head>(?:\x1b\[[0-9;]*m)*(?:Error|Warning)(?:\x1b\[[0-9;]*m)*: [^:\n]*):"
    r"(?P<tail> *(?:\x1b\[[0-9;]*m)*)$"
)

# The line separators the underlying span renderer recognises (its
# ``Source`` splitting rules); offset mapping must agree with them so the
# header line:column matches the span the renderer draws.
_LINE_SEPARATORS = "\r\n\x0b\x0c\u0085\u2028\u2029"


def _split_lines_inclusive(text: str) -> list[str]:
    """Split into lines, each keeping its terminator (CRLF as one)."""
    if not text:
        return [""]
    lines: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        start = i
        j = i
        while j < n and text[j] not in _LINE_SEPARATORS:
            j += 1
        if j >= n:
            lines.append(text[start:j])
            return lines
        end = j + 2 if (text[j] == "\r" and j + 1 < n and text[j + 1] == "\n") else j + 1
        lines.append(text[start:end])
        i = end
    return lines


def _line_start_offsets(text: str) -> list[int]:
    """Character offset of every line's first character (0-based lines)."""
    offsets: list[int] = []
    offset = 0
    for chunk in _split_lines_inclusive(text):
        offsets.append(offset)
        offset += len(chunk)
    return offsets


def offset_for_position(source_text: str, line: int, column: int) -> int:
    """Convert a 1-based ``(line, column)`` into a character offset.

    Positions past the end of the source are clamped to ``len(text)``.
    """
    starts = _line_start_offsets(source_text)
    idx = line - 1
    if idx < 0 or idx >= len(starts):
        return len(source_text)
    return min(starts[idx] + max(column - 1, 0), len(source_text))


def _capitalize(message: str) -> str:
    """Capitalize the first character only, keeping the rest intact."""
    return message[:1].upper() + message[1:] if message else message


def publisher_for(exc: FrontendError) -> str:
    """The tgqe publisher identity of a diagnostic's pipeline stage.

    Routed by diagnostic class — the stage that *produced* the error
    object — never by message text.
    """
    from ..lexer import LexError
    from ..macros.expansion import MacroError
    from ..sa import SaError, SaWarning

    if isinstance(exc, LexError):
        return PUBLISHER_LEXER
    if isinstance(exc, MacroError):
        return PUBLISHER_PREPROCESSOR
    if isinstance(exc, (SaError, SaWarning)):
        return PUBLISHER_SA
    return PUBLISHER_PARSER


def store_source(name: str, text: str) -> None:
    """Register a source text under *name* in the tgqe source cache.

    Mirrors ``TgqeReader.store_file`` for in-memory sources: per-line
    entries keep the line index, the full text serves span rendering.
    """
    manager = SourceManager()
    offset = 0
    for line_no, chunk in enumerate(_split_lines_inclusive(text), 1):
        manager.push_line_at(name, line_no, offset, chunk.rstrip("\r\n"))
        offset += len(chunk)
    manager.set_text(name, text)


def _expansion_chain_hints(exc: FrontendError) -> str:
    """Multi-line hints text describing a macro expansion chain.

    ``expansion_chain`` is attached by the macro preprocessor to errors
    raised inside expansions: innermost first, each link names the macro,
    its definition site and the call site (see todo-44 / ``--pass 1``).
    """
    chain = getattr(exc, "expansion_chain", None)
    if not chain:
        return ""
    links = "\n".join(
        f"   - in expansion of macro `{link['macro']}` "
        f"(defined at {link['def_line']}:{link['def_column']}, "
        f"called at {link['call_line']}:{link['call_column']})"
        for link in chain
    )
    return f"\n{links}"


def error_context(
    exc: FrontendError,
    source_text: str,
    *,
    source_name: Optional[str] = None,
    publisher: Optional[str] = None,
    level: TgqeLevelFilter = TgqeLevelFilter.Error,
) -> TgqeCtx:
    """Convert one diagnostic into a tgqe context (and cache its source).

    The headline shows the error kind only (``_STAGE_CLASSES``); the
    message (got/expected details) rides the code span label, keeping
    the headline free of details.  The publisher records the stage the
    diagnostic came from.  Displayed fields are capitalized (first
    character only), matching the previous renderer's presentation.
    """
    stage = publisher if publisher is not None else publisher_for(exc)
    name = source_name if source_name is not None else _UNNAMED_SOURCE
    store_source(name, source_text)

    err_type = _STAGE_CLASSES.get(stage, "error")
    start_off = offset_for_position(source_text, exc.line, exc.column)
    end_off = offset_for_position(source_text, exc.end_line, exc.end_column)
    if end_off <= start_off:
        end_off = min(start_off + 1, len(source_text))
    if start_off > len(source_text):
        start_off = end_off = len(source_text)

    start = TgqeCoordinate(name, start_off, exc.line, exc.column)
    end = TgqeCoordinate(name, end_off, exc.end_line, exc.end_column)
    # Display form: capitalize the first character of the class word and
    # the message, matching the previous renderer's presentation.
    message = _capitalize(str(exc.message))
    return TgqeCtx(
        TgqePosition(start, TgqeSpan(start, end)),
        TgqeErrorInfo(_capitalize(err_type), message, "", level),
        _expansion_chain_hints(exc),
        _capitalize(exc.category) if exc.category else "",
        stage,
        time.time_ns(),
    )


def _render_batch_to_string(ctxs: Sequence[TgqeCtx], color: bool) -> str:
    """Render a batch through the tgqe bus, capturing the report text.

    tgqe's renderer always emits ANSI styling; ``color=False`` strips the
    escape sequences after capture (``--no-color``).
    """
    batch = list(ctxs)
    buffer = io.StringIO()
    with redirect_stderr(buffer):
        ICC().report(batch)
    rendered = "\n".join(
        _EMPTY_GOT_TAIL_RE.sub(r"\g<head>\g<tail>", line)
        for line in buffer.getvalue().split("\n")
    )
    return rendered if color else _ANSI_RE.sub("", rendered)


def report_contexts(ctxs: Sequence[TgqeCtx], *, color: bool = True) -> None:
    """Hand a batch of contexts to the tgqe error bus for rendering.

    With ``color=False`` the reports are additionally stripped of ANSI
    escapes before they are written to stderr unstyled.
    """
    sys.stderr.write(_render_batch_to_string(ctxs, color=color))


def _render_diagnostic(
    exc: FrontendError,
    source_text: str,
    *,
    source_name: Optional[str],
    publisher: Optional[str],
    level: TgqeLevelFilter,
    color: bool,
) -> str:
    """Render one diagnostic through the tgqe pipeline into a string."""
    ctx = error_context(
        exc,
        source_text,
        source_name=source_name,
        publisher=publisher,
        level=level,
    )
    return _render_batch_to_string([ctx], color=color)


def render_error(
    error: FrontendError,
    source_text: str,
    *,
    source_name: Optional[str] = None,
    color: bool = True,
    publisher: Optional[str] = None,
) -> str:
    """Render an error diagnostic (``Error`` kind) as a string."""
    return _render_diagnostic(
        error,
        source_text,
        source_name=source_name,
        publisher=publisher,
        level=TgqeLevelFilter.Error,
        color=color,
    )


def render_warning(
    warning: FrontendError,
    source_text: str,
    *,
    source_name: Optional[str] = None,
    color: bool = True,
    publisher: Optional[str] = None,
) -> str:
    """Render a warning diagnostic (``Warning`` kind) as a string."""
    return _render_diagnostic(
        warning,
        source_text,
        source_name=source_name,
        publisher=publisher,
        level=TgqeLevelFilter.Warn,
        color=color,
    )
