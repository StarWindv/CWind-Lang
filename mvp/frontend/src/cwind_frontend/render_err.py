"""Compatibility shim: the diagnostic renderer moved to
``cwind_frontend.render`` (``render.errors``).  Import from there.
"""

from .render.errors import (  # noqa: F401
    offset_for_position,
    render_error,
    render_warning,
)

__all__ = ["offset_for_position", "render_error", "render_warning"]
