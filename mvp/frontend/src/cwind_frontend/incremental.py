"""todo-99/82: build-state fingerprints for incremental project builds.

A project build is a pure function of

    (entry file + every transitively imported source)  x  the ``#[cfg]``
    target quadruple  x  Breeze.toml semantics  x  the compiler itself
    (declared version *and* content identity, todo-115),

so re-running ``cwindf --project`` can skip the whole lex → parse → SA
pipeline when **every** input is provably unchanged.  The proof lives in a
small state document next to the other build outputs::

    target/.build-state.json

* per-source stamps ``{size, mtime_ns, sha256}`` — the metadata pair acts as
  the fast path (sccache style: no content reads while mtimes keep matching),
  the hash backstops pathological writers that restore an old mtime;
* module-tree fingerprints over each import root (``libs/``, ``src/``) so
  structurally new/deleted/renamed modules invalidate even when no imported
  file changed (e.g. a newly added ``foo/mod.wind`` turns ``use foo;`` into
  an ambiguity);
* a semantic signature of the manifest and of the resolved target — editing
  Breeze.toml or switching ``--target-os`` invalidates without touching any
  source;
* the expected output list — missing artifacts force a rebuild.

An absent/corrupt/older-format state document simply means "not fresh".
Any single check failing means "rebuild everything": SA annotations are
global to the flattened program, so partial reuse inside one run is left to
a future todo.  A failed compilation never writes state.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from .parser.parser import _SOURCE_SUFFIXES


if TYPE_CHECKING:  # pragma: no cover - typing only
    from .breeze import BreezeManifest
    from .cfg import TargetCfg

__all__ = [
    "STATE_FORMAT",
    "STATE_NAME",
    "STATE_VERSION",
    "BuildState",
    "check_freshness",
    "write_state",
]

STATE_FORMAT = "cwind-build-state"
STATE_VERSION = 1
STATE_NAME = ".build-state.json"

_CHUNK = 1 << 20


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _tree_fingerprint(root: Path) -> str:
    """Structural fingerprint of one import root's source tree.

    Only the *set* of compilable paths participates — deliberately no
    mtimes/sizes: content changes are the per-source stamps' job (with their
    sha256 fallback), while merely touching a file must not invalidate (the
    metadata fast path is what keeps CI checkouts cheap).
    """
    names: list[str] = []
    if root.is_dir():
        for path in sorted(root.rglob("*"), key=lambda p: str(p).lower()):
            try:
                if path.is_file() and path.suffix.lower() in _SOURCE_SUFFIXES:
                    names.append(path.relative_to(root).as_posix())
            except OSError:
                continue
    return hashlib.sha256("\n".join(names).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class BuildState:
    """The inputs whose equality with the last build proves freshness."""

    entry_file: str                  # root-relative POSIX path
    target: dict                     # effective cfg quadruple
    package: dict                    # semantic manifest signature
    trees: dict                      # import-root name -> fingerprint
    files: dict                      # rel POSIX -> {size, mtime_ns, sha256}
    outputs: tuple[str, ...]         # target-relative POSIX paths


def state_path(target_dir: Path) -> Path:
    return target_dir / STATE_NAME


def _load(target_dir: Path) -> Optional[dict]:
    path = state_path(target_dir)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (
        not isinstance(doc, dict)
        or doc.get("format") != STATE_FORMAT
        or doc.get("version") != STATE_VERSION
        or not isinstance(doc.get("entry_file"), str)
        or not isinstance(doc.get("files"), dict)
        or not isinstance(doc.get("outputs"), list)
    ):
        return None
    return doc


def _package_signature(manifest) -> dict:
    """The part of Breeze.toml that can change what gets built."""
    return {
        "name": manifest.name,
        "version": manifest.version,
        "identifier": manifest.identifier,
        "id_version": manifest.id_version,
        "description": manifest.description,
        "authors": list(manifest.authors),
        "homepage": manifest.homepage,
        "entry": {
            "source": manifest.entry.source,
            "is_lib": manifest.entry.is_lib,
            "module": manifest.entry.module,
        },
        "dependencies": {
            name: {"version": dep.version, "identifier": dep.identifier}
            for name, dep in manifest.dependencies.items()
        },
    }


def _effective_target(target: "TargetCfg") -> dict:
    """The cfg quadruple ``#[cfg]`` is actually evaluated against."""
    from .cfg import CfgContext  # local import keeps module load cheap

    ctx = CfgContext(
        target.os, target.arch, target.vendor, target.pointer_width
    )
    return {
        "os": ctx.target_os,
        "arch": ctx.target_arch,
        "vendor": ctx.target_vendor,
        "pointer_width": ctx.target_pointer_width,
    }


_FRONTEND_DATA_SUFFIXES = frozenset({".py", ".toml"})


def _frontend_root() -> Path:
    """Directory whose contents define compiler behaviour."""
    return Path(__file__).resolve().parent


def _frontend_sources() -> list[Path]:
    """The files that define compiler behaviour (code + packaged tables).

    ``__pycache__`` residue is skipped everywhere: only inputs whose bytes
    change what the compiler does may invalidate builds.
    """
    root = _frontend_root()
    paths: list[Path] = []
    try:
        candidates = sorted(root.rglob("*"), key=lambda p: str(p))
    except OSError:
        return paths
    for path in candidates:
        if "__pycache__" in path.parts or (
            path.suffix.lower() not in _FRONTEND_DATA_SUFFIXES
        ):
            continue
        try:
            if path.is_file():
                paths.append(path)
        except OSError:
            continue
    return paths


@lru_cache(maxsize=1)
def _frontend_content_digest() -> str:
    """todo-115: content digest over the compiler itself.

    ``__version__`` is maintained by hand, so upgrading the frontend without
    bumping it used to leave old build states fresh forever.  The tool stamp
    therefore mixes the declared version with a hash over every file under
    the ``cwind_frontend`` package directory (source modules plus packaged
    ``sa/*.toml`` rule tables).  The file set is bounded by the package and
    memoized per process -- a deliberate exception to the metadata-fast-path
    discipline applied to *user* sources: skipping the whole pipeline is
    exactly what this check buys, so paying ~40 tiny hashes once per
    invocation is the correct trade.
    """
    from ._version import __version__

    root = _frontend_root()
    digest = hashlib.sha256()
    digest.update(f"cwind-frontend {__version__}\n".encode("utf-8"))
    for path in _frontend_sources():
        try:
            rel = path.relative_to(root).as_posix()
            content = _sha256_of(path)
        except OSError:
            continue
        digest.update(rel.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(content.encode("ascii"))
    return digest.hexdigest()


def _frontend_version() -> str:
    """Tool stamp persisted in the build state (todo-99 + todo-115)."""
    from ._version import __version__

    return f"{__version__}+{_frontend_content_digest()}"


def collect_files(
    root: Path,
    artifacts: dict[str, str],
    entry_file: Path,
) -> dict[str, dict]:
    """Stamps for every participating source.

    The project's artifact map (todo-98) names one JSON per participating
    file — its keys are the authoritative input set; the entry is unioned in
    so an items-free entry file still gets stamped.
    """
    files: dict[str, dict] = {}
    for rel in sorted({*artifacts.keys(), entry_file.relative_to(root).as_posix()}):
        source = root / rel
        try:
            stat = source.stat()
        except OSError:
            files[rel] = {}  # vanished since the parse: forced miss below
            continue
        files[rel] = {
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            # Hash eagerly: the save-path follows freshly parsed sources, and
            # complete stamps keep later runs on the metadata fast path.
            "sha256": _sha256_of(source),
        }
    return files


def _import_root_fingerprints(root: Path, manifest) -> dict[str, str]:
    roots: dict[str, str] = {}
    libs = root / "libs"
    src = root / manifest.entry.source
    if libs.is_dir():
        roots["libs"] = _tree_fingerprint(libs)
    if (
        src.is_dir()
        and src != libs
        and src.resolve().is_relative_to(root.resolve())
    ):
        roots[manifest.entry.source] = _tree_fingerprint(src)
    return roots


def write_state(
    root: Path,
    target_dir: Path,
    manifest,
    target_cfg: "TargetCfg",
    entry_file: Path,
    artifacts: dict[str, str],
) -> Path:
    """Persist the build state after a *successful* full rebuild."""
    # Every artifact on disk belongs to the new snapshot; record them all so
    # an externally deleted product forces a rebuild instead of a stale link.
    # The whole-program JSON is the backend's real input (project.json's
    # 'target' pointer) so it must be part of the watched set too.
    whole = f"{manifest.name}.typed.json"
    outputs = sorted(
        {Path(rel).as_posix() for rel in artifacts.values()}
        | {"project.json", whole}
    )

    files = collect_files(root, artifacts, entry_file)

    state = {
        "format": STATE_FORMAT,
        "version": STATE_VERSION,
        "tool": {"frontend": _frontend_version()},
        "target": _effective_target(target_cfg),
        "package": _package_signature(manifest),
        "entry_file": entry_file.relative_to(root).as_posix(),
        "trees": _import_root_fingerprints(root, manifest),
        "files": files,
        "outputs": outputs,
        "whole_program": f"{manifest.name}.typed.json",
    }
    path = state_path(target_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return path


def check_freshness(
    root: Path,
    target_dir: Path,
    manifest,
    target_cfg: "TargetCfg",
    entry_file: Path,
) -> tuple[bool, str]:
    """Return ``(fresh, reason)`` describing the previous build state.

    ``fresh`` is true only when every input of the last build is provably
    identical now; ``reason`` names the first difference found otherwise.
    """
    state = _load(target_dir)
    if state is None:
        return False, "no previous build state"

    if state.get("tool", {}).get("frontend") != _frontend_version():
        return False, "frontend version changed"

    if state.get("target") != _effective_target(target_cfg):
        return False, "#[cfg] target changed"

    if state.get("package") != _package_signature(manifest):
        return False, "Breeze.toml changed"

    entry_rel = entry_file.relative_to(root).as_posix()
    if state.get("entry_file") != entry_rel:
        return False, f"entry moved to {entry_rel}"

    if (state.get("trees") or {}) != _import_root_fingerprints(root, manifest):
        return False, "module tree layout changed"

    files = state.get("files") or {}
    for rel, stamp in sorted(files.items()):
        source = root / rel
        if set(stamp) < {"size", "mtime_ns", "sha256"} or not source.is_file():
            return False, f"source '{rel}' disappeared"
        try:
            meta_ok = (
                stamp["size"] == source.stat().st_size
                and stamp["mtime_ns"] == source.stat().st_mtime_ns
            )
        except OSError:
            return False, f"source '{rel}' disappeared"
        if not meta_ok and _sha256_of(source) != stamp["sha256"]:
            return False, f"source '{rel}' content changed"

    for out in state.get("outputs", []):
        if not isinstance(out, str) or not (target_dir / out).is_file():
            return False, f"missing output '{out}'"

    return True, "inputs unchanged"


