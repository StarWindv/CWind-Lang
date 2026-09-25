"""Compile-wide procedure-macro registry (todo-179).

Definitions are collected per file and registered here.  Visibility
follows the module system (todo-158): definitions are keyed by
``(module path, macro name, kind)`` where the module path is the
declaring file's position in the declaration-driven ``mod`` tree, and a
call resolves through the same ``pub``/``use`` gates as ordinary items:

- a *qualified* call (``std::ext::print::println!`` /
  ``#[path::to::Attr]`` / ``#[derive(path::Derive)]``) walks the module
  tree directly;
- a *bare* call resolves only from the defining file itself, an
  explicit ``use path::to::name;`` binding recorded before expansion, or
  the std prelude (the entry file's implicit ``use std::*`` surface);
- ``pub use`` re-exports extend paths exactly like ordinary items, so
  ``libs/mod.wind``'s ``pub use crate::ext::print::{print, println};``
  makes the std macros prelude-visible.

Cross-file "global ambiguity" is gone: same-name macros in different
modules coexist, and ambiguity is reported only when one file binds the
same name to conflicting macros.

The registry also owns the compiled-macro cache and the driver
invocation, so the expansion driver can ask for one expansion without
knowing anything about subprocesses.
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
from ..definition import MacroDef
from .build import MacroBuild, build_macro, find_cwindc
from .collect import collect_proc_macros
from .definition import ProcMacroDef
from .errors import ProcMacroError
from .protocol import pairs_to_tokens, token_span, tokens_to_pairs

__all__ = ["ProcMacroRegistry", "flush_scan_cache"]

# file path -> (mtime_ns, size, imports, defs, rules); process-wide so
# repeated parses of the std tree do not re-tokenize it (todo-171-style
# cache with explicit flush).  One `_file_scan` result feeds the entry's
# ``use`` imports, the proc-macro definitions AND the rule definitions —
# the old path tokenized every file twice (prepare_file + _scan_file).
_FILE_SCAN_CACHE: dict[
    str, tuple[int, int, list, list, list]
] = {}
# Cross-run persistence: entries are keyed by (mtime_ns, size), so a
# source edit invalidates exactly its own file and nothing else; bump
# the version when Token / ProcMacroDef / MacroDef shapes or scan
# semantics change (same discipline as build.BUILD_VERSION).
_FILE_SCAN_VERSION = 1
_FILE_SCAN_STATE = {"loaded": False, "dirty": False}

_DRIVER_TIMEOUT = 300.0


def _file_scan_path() -> Path:
    from .build import _CACHE_ROOT_NAME

    return (
        Path(tempfile.gettempdir()) / _CACHE_ROOT_NAME
        / f"filescan-v{_FILE_SCAN_VERSION}.pkl"
    )


def _file_scan_load() -> None:
    if _FILE_SCAN_STATE["loaded"]:
        return
    _FILE_SCAN_STATE["loaded"] = True
    try:
        import pickle

        data = pickle.loads(_file_scan_path().read_bytes())
        if isinstance(data, dict) and data.get("version") == _FILE_SCAN_VERSION:
            entries = data.get("entries")
            if isinstance(entries, dict):
                _FILE_SCAN_CACHE.update(entries)
    except Exception:
        pass  # absent/corrupt/older: cold scan rebuilds it


def _file_scan_save() -> None:
    if not _FILE_SCAN_STATE["dirty"]:
        return
    _FILE_SCAN_STATE["dirty"] = False
    try:
        import pickle

        path = _file_scan_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(pickle.dumps(
            {
                "version": _FILE_SCAN_VERSION,
                "entries": dict(_FILE_SCAN_CACHE),
            },
            protocol=pickle.HIGHEST_PROTOCOL,
        ))
        os.replace(tmp, path)
    except Exception:
        pass  # a lost save only costs the next run's re-scan


def _file_scan(path: Path) -> tuple[list, list, list]:
    """``(imports, defs, rules)`` for *path* — tokenized at most once.

    First lookup consults the persisted cache (mtime_ns + size gate);
    a miss tokenizes ONCE and the same tokens produce the imports, the
    proc-macro definitions and the rule definitions.
    """
    _file_scan_load()
    key = str(path)
    try:
        stat = path.stat()
    except OSError:
        return [], [], []
    hit = _FILE_SCAN_CACHE.get(key)
    if (
        hit is not None
        and hit[0] == stat.st_mtime_ns
        and hit[1] == stat.st_size
    ):
        return hit[2], hit[3], hit[4]
    try:
        tokens = tokenize_file(path)
    except Exception:
        tokens = []
    imports = _scan_imports(tokens) if tokens else []
    defs, rules = _collect_definitions_at(path, tokens)
    _FILE_SCAN_CACHE[key] = (
        stat.st_mtime_ns, stat.st_size, imports, defs, rules,
    )
    _FILE_SCAN_STATE["dirty"] = True
    return imports, defs, rules


def flush_scan_cache() -> None:
    """Drop the per-file definition scan cache (compile boundaries)."""
    _FILE_SCAN_CACHE.clear()
    # A flush means "start cold": do not re-load the dumped entries.
    _FILE_SCAN_STATE["loaded"] = True
    _FILE_SCAN_STATE["dirty"] = False


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
        self.by_path = {}
        self.file_modules = {}
        self.modules = {}
        self.imports = {}
        self.prelude_files = set()
        self.locals = {}
        self.tree = None
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
            cached = {d.function_name: d for d in defs}
            self._local_defs_cache[key] = cached
        return cached

    # -- collection ----------------------------------------------------

    def register(self, definition) -> None:
        file = _file_key(definition.source_path)
        self.locals.setdefault(file, {})[definition.identity()] = definition
        module = self.file_modules.get(file)
        if module is not None:
            self.by_path.setdefault((module, definition.name, _kind(definition)), {})[
                definition.identity()
            ] = definition

    def register_rules(self, definition: MacroDef) -> None:
        self.register(definition)

    def prepare_file(
        self, tokens, source_path, *, prelude=False, imports=None
    ) -> None:
        file = _file_key(source_path)
        self.imports[file] = (
            _scan_imports(tokens) if imports is None else imports
        )
        if prelude:
            self.prelude_files.add(file)

    def scan_roots(self, directories: Iterable[Path]) -> None:
        """Discover definitions only at entries of the declaration-driven tree."""
        from ...parser.defs import _module_roots, _library_tree
        allowed = {_file_key(str(p)) for p in directories}
        if not allowed:
            return
        try:
            self.tree = _library_tree(self.project_base)
        except (OSError, ValueError):
            return
        def visit(node, parts):
            if node.entry is not None:
                file = _file_key(str(node.entry))
                self.file_modules[file] = parts
                self.modules[parts] = node
                if file not in self._scanned:
                    self._scanned.add(file)
                    imports, procs, rules = _file_scan(node.entry)
                    self.prepare_file(None, file, imports=imports)
                    for definition in [*procs, *rules]:
                        self.register(definition)
            for name, child in node.children.items():
                visit(child, (*parts, name))
        for root in _module_roots(self.project_base):
            if _file_key(str(root.directory)) not in allowed:
                continue
            if root.kind == "pkg":
                if root.prefix is None:
                    continue
                node = self.tree.crate.children.get(root.prefix)
                if node is not None:
                    visit(node, ("crate", root.prefix))
            else:
                visit(getattr(self.tree, root.kind), (root.kind,))
        _file_scan_save()

    def _absolute(self, parts, file):
        current = self.file_modules.get(file)
        kind = current[0] if current else (
            "crate" if ("crate",) in self.modules else "std")
        if parts[0] == "std":
            return tuple(parts)
        if parts[0] == "crate":
            return (kind, *parts[1:])
        if parts[0] in ("self", "super"):
            if current is None:
                return ()
            base = current if parts[0] == "self" else current[:-1]
            return (*base, *parts[1:])
        return (kind, *parts)

    def _path_candidates(self, parts, file, kind, seen):
        absolute = self._absolute(parts, file)
        if len(absolute) < 2:
            return [], None
        marker = (absolute, file, kind)
        if marker in seen:
            return [], None
        seen = {*seen, marker}
        module, name = absolute[:-1], absolute[-1]
        importer = self.file_modules.get(file)
        for depth in range(2, len(module) + 1):
            prefix = module[:depth]
            node = self.modules.get(prefix)
            if node is not None and not node.pub:
                if importer is None or importer[:depth - 1] != prefix[:-1]:
                    return [], f"module '{'::'.join(prefix)}' is private"
        node = self.modules.get(module)
        if node is None:
            if self.tree is not None:
                followed = self.tree.follow(getattr(self.tree, absolute[0]), list(absolute[1:]))
                if followed is not None:
                    tree_kind, target = followed
                    if target and target[0] in ("std", "crate"):
                        target = target[1:]
                    return self._path_candidates((tree_kind, *target), file, kind, seen)
            return [], None
        candidates = []
        private = False
        for definition in self.by_path.get((module, name, kind), {}).values():
            if _public(definition) or _file_key(definition.source_path) == file:
                candidates.append(definition)
            else:
                private = True
        if node.entry is not None:
            # ``pub use`` re-exports of the defining module extend the path
            # (Rust: only ``pub use`` is visible to importers; a private
            # ``use`` serves the defining file alone).
            owner = _file_key(str(node.entry))
            for alias, target, pub in self.imports.get(owner, ()):
                if not pub and owner != file:
                    continue
                if alias == name or alias == "*":
                    path = (*target, name) if alias == "*" else target
                    found, error = self._path_candidates(path, owner, kind, seen)
                    if error:
                        return [], error
                    candidates.extend(found)
        return candidates, (f"macro '{'::'.join(parts)}' is private" if private and not candidates else None)

    def resolve(self, name, source_path, kind="function"):
        file = _file_key(source_path)
        if "::" in name:
            candidates, error = self._path_candidates(name.split("::"), file, kind, set())
        else:
            candidates = [d for d in self.locals.get(file, {}).values()
                          if d.name == name and _kind(d) == kind]
            error = None
            for alias, target, pub in self.imports.get(file, ()):
                if alias == name or alias == "*":
                    path = (*target, name) if alias == "*" else target
                    found, problem = self._path_candidates(path, file, kind, set())
                    if alias == "*":
                        # bug-80: a ``use path::*`` glob only introduces
                        # the module's *public* items (Rust semantics): a
                        # private hit is silently skipped, never reported,
                        # and a same-named local rule shadows the glob.
                        # A *module*-level privacy error (``use private::*``
                        # is itself the E0603 analogue) still surfaces; an
                        # item-level private hit just fails to import.
                        if problem is not None and problem.startswith("module '"):
                            error = error or problem
                        candidates.extend(
                            d for d in found if _public(d)
                        )
                    else:
                        # bug-80: an explicit ``use path::name;`` binding is
                        # a hard resolution: a private hit reports the
                        # privacy error (the E0603 analogue) and must not
                        # be shadowed by a same-named local rule.
                        candidates.extend(found)
                        error = error or problem
            if not candidates and not error and file in self.prelude_files:
                # (c) prelude: the std root's ``pub use`` surface rides the
                # auto prelude; crate roots are NOT a prelude (bare crate
                # macros need an explicit binding, like ordinary items).
                node = self.modules.get(("std",))
                if node is not None and node.entry is not None:
                    found, problem = self._path_candidates(("std", name), file, kind, set())
                    candidates.extend(found)
                    error = error or problem
        unique = {d.identity(): d for d in candidates}
        if len(unique) > 1:
            return None, f"macro '{name}' is ambiguous (conflicting bindings)"
        return next(iter(unique.values()), None), error

    def lookup_rules(self, name, source_path=None):
        definition, error = self.resolve(name, source_path)
        return (definition if isinstance(definition, MacroDef) else None), error

    def lookup(self, name, source_path=None, kind="function"):
        definition, error = self.resolve(name, source_path, kind)
        if definition is not None or error:
            return (definition if isinstance(definition, ProcMacroDef) else None), error
        for other in ("function", "attribute", "derive"):
            if other == kind:
                continue
            wrong, _ = self.resolve(name, source_path, other)
            if wrong is not None:
                syntax = {"function": f"{name}!(...)", "attribute": f"#[{name}]",
                          "derive": f"#[derive({name})]"}[other]
                return None, (f"procedure macro '{name}' is a {other} macro; "
                              f"use {syntax}, not a {kind} invocation")
        return None, None

    def import_target(self, parts, source_path):
        found = []
        for kind in ("function", "attribute", "derive"):
            definition, error = self.resolve("::".join(parts), source_path, kind)
            if error:
                return None, error
            if definition is not None:
                found.append(definition)
        return (found[0] if found else None), None

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
        item_tokens: Optional[list[Token]] = None,
    ) -> MacroExpansion:
        if definition.issues:
            return MacroExpansion(
                error="this macro has definition errors and cannot be used",
                definition=definition,
            )
        pairs = tokens_to_pairs(arg_tokens)
        item_pairs = tokens_to_pairs(item_tokens) if item_tokens is not None else None
        cache_key = (
            definition.identity(),
            source_path,
            token_span(anchor),
            tuple((kind, text, tuple(span)) for kind, text, span in pairs),
            None if item_pairs is None else tuple(
                (kind, text, tuple(span)) for kind, text, span in item_pairs
            ),
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
        extra = {"item_pairs": item_pairs} if item_pairs is not None else {}
        expansion = self._run_driver(
            definition, build, pairs, anchor, source_path, **extra
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
        item_pairs: Optional[list] = None,
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
        if item_pairs is not None:
            request["item_tokens"] = item_pairs
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


def _file_key(path):
    return os.path.normcase(os.path.abspath(path)) if path else ""


def _kind(definition):
    return definition.kind if isinstance(definition, ProcMacroDef) else "function"


def _public(definition):
    return definition.is_pub if isinstance(definition, ProcMacroDef) else definition.exported


def _scan_imports(tokens):
    """Read file-level use selectors without parsing macro bodies."""
    from .collect import _scan_group
    tokens = [t for t in tokens if t.kind != TokenKind.COMMENT]
    result = []
    i = 0
    while i < len(tokens):
        pub = tokens[i].kind == TokenKind.PUB
        j = i + int(pub)
        if j >= len(tokens):
            break
        if tokens[j].kind != TokenKind.USE:
            if tokens[i].kind in (TokenKind.LBRACE, TokenKind.LPAREN, TokenKind.LBRACKET):
                i = _scan_group(tokens, i) or len(tokens)
            else:
                i += 1
            continue
        j += 1
        parts = []
        while j < len(tokens) and tokens[j].kind == TokenKind.IDENTIFIER:
            parts.append(str(tokens[j].value))
            j += 1
            if j >= len(tokens) or tokens[j].kind != TokenKind.PATH:
                break
            j += 1
        if not parts or j >= len(tokens):
            i = j
            continue
        if tokens[j].kind == TokenKind.STAR:
            result.append(("*", tuple(parts), pub))
            j += 1
        elif tokens[j].kind == TokenKind.LBRACE:
            end = _scan_group(tokens, j)
            if end is None:
                break
            j += 1
            while j < end - 1:
                if tokens[j].kind != TokenKind.IDENTIFIER:
                    j += 1
                    continue
                member = str(tokens[j].value)
                j += 1
                alias = member
                if j + 1 < end and tokens[j].kind == TokenKind.AS:
                    alias = str(tokens[j + 1].value)
                    j += 2
                if member != "self":
                    result.append((alias, (*parts, member), pub))
            j = end
        else:
            alias = parts[-1]
            if j + 1 < len(tokens) and tokens[j].kind == TokenKind.AS:
                alias = str(tokens[j + 1].value)
                j += 2
            if j < len(tokens) and tokens[j].kind == TokenKind.SEMICOLON:
                result.append((alias, tuple(parts), pub))
        i = max(i + 1, j)
    return result


def _collect_definitions_at(
    path: Path, tokens
) -> tuple[list[ProcMacroDef], list[MacroDef]]:
    """Proc-macro + rule definitions from already-tokenized *tokens*."""
    defs: list[ProcMacroDef] = []
    rules: dict[str, MacroDef] = {}
    if tokens:
        try:
            stream, defs, _errors = collect_proc_macros(
                list(tokens), str(path.resolve())
            )
        except Exception:
            defs = []
        else:
            # Lazy import avoids the expansion/context/registry import cycle.
            from ..expansion import _collect_definitions

            _collect_definitions(stream, rules, None, [], str(path.resolve()))
    return defs, list(rules.values())


def _scan_file(path: Path) -> tuple[list[ProcMacroDef], list[MacroDef]]:
    _imports, defs, rules = _file_scan(path)
    return defs, rules


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
