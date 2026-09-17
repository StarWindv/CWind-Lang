"""Host-side procedure-macro context (todo-179).

One context per compile unit: it owns the registry (definitions from the
whole module tree), the build/driver cache, and the builtin table
(``quote!``).  The token-level expansion driver asks it to resolve and
run macro calls; diagnostics are converted into ordinary frontend
errors/warnings so the tgqe pipeline renders them like everything else.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

from ...ast_components.errors import FrontendError
from ...ast_components.token import Token
from .builtins import BUILTIN_MACROS, expand_builtin as _expand_builtin
from .definition import ProcMacroDef
from .errors import ProcMacroError
from .registry import ProcMacroRegistry
from .protocol import validated_span

__all__ = ["ProcMacroContext", "shared_context", "clear_shared_contexts"]


class ProcMacroContext:
    """Procedure macros for one compile unit."""

    def __init__(
        self,
        project_base: Path,
        *,
        scan_dirs: Iterable[Path] = (),
        cache_dir: Optional[Path] = None,
    ) -> None:
        self.project_base = Path(project_base)
        self.registry = ProcMacroRegistry(
            self.project_base, cache_dir=cache_dir
        )
        self.registry.scan_roots(scan_dirs)
        self.warnings: list[FrontendError] = []

    # -- lookups -------------------------------------------------------

    def lookup(
        self, name: str, source_path: Optional[str], kind: str = "function"

    ) -> tuple[Optional[ProcMacroDef], Optional[str]]:
        return self.registry.lookup(name, source_path, kind)

    def is_builtin(self, name: str) -> bool:
        return name in BUILTIN_MACROS

    # -- expansion -----------------------------------------------------

    def expand_proc(
        self,
        definition: ProcMacroDef,
        arg_tokens: list[Token],
        anchor: Token,
        source_path: Optional[str],
        item_tokens: Optional[list[Token]] = None,
    ) -> tuple[list[Token], list[FrontendError]]:
        """Run one defined macro; returns ``(tokens, errors)``."""
        location = (
            f"defined at {definition.source_path or '<unknown>'}:"
            f"{definition.name_token.line}:{definition.name_token.column}"
        )
        try:
            result = self.registry.expand(
                definition,
                arg_tokens,
                anchor=anchor,
                source_path=source_path,
                item_tokens=item_tokens,
            )
        except Exception as exc:  # never ICE the frontend on a macro bug
            return [], [_anchor_error(
                f"procedure macro '{definition.name}' failed internally: "
                f"{exc}\n{location}",
                anchor,
                source_path,
            )]
        errors = self._convert_diagnostics(
            result.diagnostics, definition.name, anchor, source_path
        )
        if result.error:
            if errors:
                for error in errors:
                    error.message += f"\n{location}"
            else:
                errors.append(_anchor_error(
                    f"{result.error}\n{location}", anchor, source_path,
                ))
        if errors:
            return [], errors
        return list(result.tokens), errors

    def expand_builtin(
        self,
        name: str,
        arg_tokens: list[Token],
        anchor: Token,
        source_path: Optional[str],
    ) -> tuple[list[Token], list[FrontendError]]:
        tokens, error = _expand_builtin(name, arg_tokens, anchor)
        if error is not None:
            return [], [_anchor_error(error, anchor, source_path)]
        return list(tokens or []), []

    def _convert_diagnostics(
        self,
        diagnostics: list[dict],
        macro_name: str,
        anchor: Token,
        source_path: Optional[str],
    ) -> list[FrontendError]:
        out: list[FrontendError] = []
        for diag in diagnostics:
            if not isinstance(diag, dict):
                continue
            level = str(diag.get("level") or "note")
            message = str(diag.get("message") or "")
            if not message:
                continue
            text = f"procedure macro '{macro_name}': {message}"
            line, column, end_line, end_column = validated_span(
                diag.get("span"), anchor, source_path
            )
            if level == "error":
                out.append(ProcMacroError(
                    text,
                    line,
                    column,
                    end_line=end_line,
                    end_column=end_column,
                    category="proc macro diagnostic",
                    source=source_path,
                ))
            else:
                warning = FrontendError(
                    f"{level}: {text}",
                    line,
                    column,
                    end_line=end_line,
                    end_column=end_column,
                    category="proc macro diagnostic",
                    source=source_path,
                )
                self.warnings.append(warning)
        return out


def _anchor_error(
    message: str,
    anchor: Token,
    source_path: Optional[str],
) -> ProcMacroError:
    return ProcMacroError(
        message,
        anchor.line,
        anchor.column,
        end_line=anchor.end_line,
        end_column=anchor.end_column,
        category="proc macro expansion",
        source=source_path,
    )


# Process-wide context cache: building a context resolves the module roots
# (filesystem walks / realpath) and scans them, so parsers created in bulk
# (macro-fragment parsers, tests) must not each pay for it.  Keyed by the
# project base; a real compile boundary clears it (todo-171 discipline) so
# different projects never share a macro registry.
_SHARED_CONTEXTS: dict[str, ProcMacroContext] = {}


def shared_context(key: str, factory) -> ProcMacroContext:
    """The cached context for *key*, built by *factory* on first use."""
    context = _SHARED_CONTEXTS.get(key)
    if context is None:
        context = factory()
        _SHARED_CONTEXTS[key] = context
    return context


def clear_shared_contexts() -> None:
    """Drop every shared context (compile boundary / tests)."""
    _SHARED_CONTEXTS.clear()
