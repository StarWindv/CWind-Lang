"""Compile-wide procedure-macro registry (todo-179).

Definitions are collected per file and registered here; visibility is the
user-chosen global-unique-name rule (todo-176): a ``pub`` macro is
callable from anywhere in the compile unit, a non-``pub`` one only from
its defining file.  The registry also owns the compiled-macro cache and
the driver invocation, so the expansion driver can ask for one expansion
without knowing anything about subprocesses.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from ...ast_components.token import Token, TokenKind
from ...lexer import tokenize_file
from .build import MacroBuild, build_macro, find_cwindc
from .collect import collect_proc_macros
from .definition import ProcMacroDef
from .errors import ProcMacroError
from .protocol import pairs_to_tokens, tokens_to_pairs

__all__ = ["ProcMacroRegistry", "flush_scan_cache"]

# path -> (mtime_ns, size, defs); process-wide so repeated parses of the
# std tree do not re-tokenize it (todo-171-style cache with explicit flush).
_SCAN_CACHE: dict[str, tuple[int, int, list[ProcMacroDef]]] = {}

_DRIVER_TIMEOUT = 300.0


def flush_scan_cache() -> None:
    """Drop the per-file definition scan cache (compile boundaries)."""
    _SCAN_CACHE.clear()


@dataclass
class MacroExpansion:
    """The outcome of running one macro invocation."""

    tokens: list[Token] = field(default_factory=list)
    diagnostics: list[dict] = field(default_factory=list)
    error: Optional[str] = None
    build_output: str = ""
    definition: Optional[ProcMacroDef] = None


class ProcMacroRegistry:
    """Registry + build cache + driver for one compile unit."""

    def __init__(
        self,
        project_base: Path,
        *,
        cache_dir: Optional[Path] = None,
        source_path: Optional[str] = None,
    ) -> None:
        self.project_base = Path(project_base)
        self.cache_dir = cache_dir
        self.source_path = source_path
        self.by_name: dict[str, list[ProcMacroDef]] = {}
        self._identities: set[tuple] = set()
        self._scanned: set[str] = set()
        self._builds: dict[str, MacroBuild] = {}
        self._results: dict[tuple, MacroExpansion] = {}
        self._local_defs_cache: dict[tuple, dict[str, ProcMacroDef]] = {}
        self.warnings: list = []

    def _local_defs_for(
        self, definition: ProcMacroDef
    ) -> dict[str, ProcMacroDef]:
        """The definition file's own procedure macros (attribute stripping
        when a body references a sibling macro as a plain function)."""
        key = definition.identity()
        cached = self._local_defs_cache.get(key)
        if cached is None:
            _stream, defs, _errors = collect_proc_macros(
                list(definition.file_tokens), definition.source_path
            )
            cached = {d.name: d for d in defs}
            self._local_defs_cache[key] = cached
        return cached

    # -- collection ----------------------------------------------------

    def register(self, definition: ProcMacroDef) -> None:
        identity = definition.identity()
        if identity in self._identities:
            return
        self._identities.add(identity)
        self.by_name.setdefault(definition.name, []).append(definition)

    def scan_roots(self, directories: Iterable[Path]) -> None:
        """Collect definitions from every module file under *directories*."""
        for directory in directories:
            key = str(directory)
            if key in self._scanned:
                continue
            self._scanned.add(key)
            base = Path(directory)
            if not base.is_dir():
                continue
            for path in sorted(base.rglob("*")):
                if not path.is_file():
                    continue
                if path.suffix not in (".wind", ".wd"):
                    continue
                for definition in _scan_file(path):
                    self.register(definition)

    def lookup(
        self, name: str, source_path: Optional[str] = None
    ) -> tuple[Optional[ProcMacroDef], Optional[str]]:
        """The definition *name* resolves to, or ``(None, error)``."""
        candidates = self.by_name.get(name)
        if not candidates:
            return None, None
        local = [
            d for d in candidates
            if _same_file(d.source_path, source_path)
        ]
        if len(local) > 1:
            return None, _ambiguous(name, local)
        if local:
            return local[0], None
        exported = [d for d in candidates if d.is_pub]
        if not exported:
            return None, None
        if len(exported) > 1:
            return None, _ambiguous(name, exported)
        return exported[0], None

    # -- builds --------------------------------------------------------

    def build(self, definition: ProcMacroDef) -> MacroBuild:
        """The (cached) compiled exe for *definition*."""
        identity = _build_identity(definition)
        build = self._builds.get(identity)
        if build is None:
            build = build_macro(
                definition,
                self._local_defs_for(definition),
                project_base=self.project_base,
                cache_dir=self.cache_dir,
            )
            self._builds[identity] = build
        return build

    def preload(
        self,
        tokens: list[Token],
        source_path: Optional[str],
        jobs: int,
    ) -> None:
        """Build every macro the token stream calls, in parallel.

        The pre-pass only *builds* (never runs) the macros named by
        ``name!(...)`` heads in this file; expansion later reuses the
        cached exes.  Build failures are stored on the build record and
        reported at the call site as usual.
        """
        if jobs <= 1:
            return
        names: set[str] = set()
        total = len(tokens)
        for i, tok in enumerate(tokens):
            if tok.kind != TokenKind.IDENTIFIER:
                continue
            if i + 1 >= total or tokens[i + 1].kind != TokenKind.NOT:
                continue
            if i + 2 >= total:
                continue
            names.add(str(tok.value))
        definitions: dict[tuple, ProcMacroDef] = {}
        for name in names:
            definition, error = self.lookup(name, source_path)
            if definition is not None and error is None:
                definitions[definition.identity()] = definition
        if not definitions:
            return
        if len(definitions) == 1 or jobs <= 1:
            for definition in definitions.values():
                self.build(definition)
            return
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=jobs) as pool:
            list(pool.map(self.build, definitions.values()))

    # -- expansion -----------------------------------------------------

    def expand(
        self,
        definition: ProcMacroDef,
        arg_tokens: list[Token],
        *,
        anchor: Token,
        source_path: Optional[str],
    ) -> MacroExpansion:
        if definition.issues:
            return MacroExpansion(
                error="this macro has definition errors and cannot be used",
                definition=definition,
            )
        pairs = tokens_to_pairs(arg_tokens)
        cache_key = (
            definition.identity(),
            tuple((kind, text) for kind, text in pairs),
        )
        cached = self._results.get(cache_key)
        if cached is not None:
            return _copy_expansion(cached)
        build = self.build(definition)
        if not build.ok:
            expansion = MacroExpansion(
                error=build.build_error or "procedure macro build failed",
                definition=definition,
            )
            self._results[cache_key] = expansion
            return _copy_expansion(expansion)
        expansion = self._run_driver(
            definition, build, pairs, anchor, source_path
        )
        self._results[cache_key] = expansion
        return _copy_expansion(expansion)

    def _run_driver(
        self,
        definition: ProcMacroDef,
        build: MacroBuild,
        pairs: list,
        anchor: Token,
        source_path: Optional[str],
    ) -> MacroExpansion:
        assert build.exe is not None
        request = {
            "kind": "expand",
            "macro": definition.name,
            "call": {
                "line": anchor.line,
                "column": anchor.column,
                "end_line": anchor.end_line,
                "end_column": anchor.end_column,
            },
            "tokens": pairs,
        }
        driver = Path(__file__).with_name("driver.py")
        try:
            proc = subprocess.run(
                [
                    sys.executable, str(driver),
                    "--exe", str(build.exe),
                ],
                input=json.dumps(request, ensure_ascii=False).encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=_DRIVER_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return MacroExpansion(
                error=f"procedure macro '{definition.name}' timed out",
                definition=definition,
            )
        except OSError as exc:
            return MacroExpansion(
                error=f"cannot run the procedure-macro driver: {exc}",
                definition=definition,
            )
        stderr = proc.stderr.decode("utf-8", "replace")
        try:
            response = json.loads(proc.stdout.decode("utf-8", "replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return MacroExpansion(
                error=(
                    f"the procedure-macro driver produced no usable "
                    f"response (exit {proc.returncode})"
                    + (f": {stderr.strip()}" if stderr.strip() else "")
                ),
                definition=definition,
            )
        if not isinstance(response, dict) or not response.get("ok"):
            message = response.get("error") if isinstance(response, dict) \
                else "invalid driver response"
            blob = ""
            if isinstance(response, dict):
                stdout_blob = (response.get("stdout") or "").strip()
                stderr_blob = (response.get("stderr") or "").strip()
                blob = stderr_blob or stdout_blob
            detail = f"procedure macro '{definition.name}' failed: {message}"
            if blob:
                detail += f"\n{blob}"
            return MacroExpansion(
                error=detail,
                diagnostics=list(response.get("diagnostics") or [])
                if isinstance(response, dict) else [],
                definition=definition,
            )
        out_tokens, token_errors = pairs_to_tokens(
            response.get("tokens") or [], anchor=anchor, source=source_path
        )
        diagnostics = list(response.get("diagnostics") or [])
        if token_errors:
            return MacroExpansion(
                error=token_errors[0].message,
                diagnostics=diagnostics,
                definition=definition,
            )
        return MacroExpansion(
            tokens=out_tokens,
            diagnostics=diagnostics,
            definition=definition,
        )


def _scan_file(path: Path) -> list[ProcMacroDef]:
    key = str(path)
    try:
        stat = path.stat()
    except OSError:
        return []
    cached = _SCAN_CACHE.get(key)
    if cached is not None and cached[0] == stat.st_mtime_ns \
            and cached[1] == stat.st_size:
        return cached[2]
    defs: list[ProcMacroDef] = []
    try:
        tokens = tokenize_file(path)
    except Exception:
        tokens = []
    if tokens:
        try:
            _, defs, _errors = collect_proc_macros(
                list(tokens), str(path.resolve())
            )
        except Exception:
            defs = []
    _SCAN_CACHE[key] = (stat.st_mtime_ns, stat.st_size, defs)
    return defs


def _same_file(a: Optional[str], b: Optional[str]) -> bool:
    if not a or not b:
        return False
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.normcase(os.path.abspath(a)) == \
            os.path.normcase(os.path.abspath(b))


def _ambiguous(name: str, defs: list[ProcMacroDef]) -> str:
    where = ", ".join(
        f"{d.source_path}:{d.name_token.line}:{d.name_token.column}"
        for d in defs
    )
    return f"procedure macro '{name}' is ambiguous ({where})"


def _build_identity(definition: ProcMacroDef) -> str:
    return "|".join(str(part) for part in definition.identity())


def _copy_expansion(expansion: MacroExpansion) -> MacroExpansion:
    return MacroExpansion(
        tokens=list(expansion.tokens),
        diagnostics=list(expansion.diagnostics),
        error=expansion.error,
        build_output=expansion.build_output,
        definition=expansion.definition,
    )
