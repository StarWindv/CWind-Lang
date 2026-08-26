"""Breeze.toml project manifests (todo-71).

``Breeze.toml`` is the CWind counterpart of Rust's ``Cargo.toml``.  The
schema follows the design draft (``.exclude/demo/Breeze.toml``)::

    [package]
    name = "demo"              # required, identifier-like
    version = "0.0.1"          # required, semver-ish
    identifier = "Dev"         # release tier: Dev < Alpha < Beta < RC < Standard
    id_version = "0.0.1"       # tier's own version
    # full id: {name}-{version}-{identifier}-{id_version}
    description = ""
    authors = ["Someone"]      # list of strings
    homepage = ""

    [entry]
    source = "./src"           # source root; modules resolve under it too
    is_lib = false             # lib packages have no main
    module = "lib.wd"          # library facade file

    [dependencies]
    NonExistsLib = "0.0.1,Standard"   # "version[,identifier]"

Discovery + parsing only — dependency resolution/downloads stay with
todo-37/41 until a package registry exists.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Optional

__all__ = [
    "DEFAULT_ENTRY_MODULE",
    "DEFAULT_ENTRY_SOURCE",
    "DEFAULT_IDENTIFIER",
    "IDENTIFIER_TIERS",
    "MANIFEST_NAME",
    "BreezeManifest",
    "Dependency",
    "EntryConfig",
    "ManifestError",
    "find_manifest",
    "load_manifest",
]

MANIFEST_NAME = "Breeze.toml"
DEFAULT_ENTRY_SOURCE = "src"
DEFAULT_ENTRY_MODULE = "lib.wd"
DEFAULT_IDENTIFIER = "Dev"

# Release tiers, lowest to highest (design draft: Dev < Alpha < Beta < RC < Standard).
IDENTIFIER_TIERS = ("Dev", "Alpha", "Beta", "RC", "Standard")

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
_VERSION_RE = re.compile(
    r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
# Dependency requirements may underspecify ("1", "1.0") the way Cargo's do.
_DEP_VERSION_RE = re.compile(
    r"^\d+(?:\.\d+){0,2}(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)


class ManifestError(ValueError):
    """A missing, unreadable or structurally invalid manifest."""

    def __init__(self, message: str, path=None) -> None:
        self.path = Path(path) if path is not None else None
        super().__init__(message)


@dataclass
class EntryConfig:
    """The ``[entry]`` table: where sources live and what the package builds."""

    source: str = DEFAULT_ENTRY_SOURCE
    is_lib: bool = False
    module: str = DEFAULT_ENTRY_MODULE


@dataclass
class Dependency:
    """One ``[dependencies]`` entry parsed from ``"version[,identifier]"``."""

    name: str
    version: str
    identifier: str = DEFAULT_IDENTIFIER


@dataclass
class BreezeManifest:
    """Parsed ``Breeze.toml``.

    Paths are normalized to forward slashes relative to :attr:`root`;
    unknown tables/keys survive in ``raw`` so future fields keep loading
    on older frontends.
    """

    path: Path
    name: str
    version: str
    identifier: str = DEFAULT_IDENTIFIER
    id_version: Optional[str] = None
    description: str = ""
    authors: tuple[str, ...] = ()
    homepage: str = ""
    entry: EntryConfig = field(default_factory=EntryConfig)
    dependencies: dict[str, Dependency] = field(default_factory=dict)
    raw: dict[str, object] = field(default_factory=dict)

    @property
    def root(self) -> Path:
        """Project root: the directory holding the manifest."""
        return self.path.parent

    def source_path(self) -> Path:
        """Absolute path of the package source root (``[entry].source``)."""
        return self.root / Path(*PurePosixPath(self.entry.source).parts)

    def lib_path(self) -> Path:
        """Absolute path of the library facade file (``[entry].module``)."""
        return self.source_path() / Path(*PurePosixPath(self.entry.module).parts)

    def entry_candidates(self) -> tuple[Path, ...]:
        """Entry files to try, most canonical first.

        Lib packages build their facade module directly; binary packages
        look for ``main.wd`` then ``main.wind`` under the source root.
        """
        if self.entry.is_lib:
            return (self.lib_path(),)
        source = self.source_path()
        return (
            source / "main.wd",
            source / "main.wind",
        )


def _normalize_rel(value: str, what: str, manifest_path: Path) -> str:
    """Validate a manifest-relative path; returns it in normalized form."""
    posix = PurePosixPath(value.replace("\\", "/"))
    if posix.is_absolute() or ".." in posix.parts or not posix.parts:
        raise ManifestError(
            f"{manifest_path}: {what} must be a relative path inside "
            f"the project, got {value!r}",
            manifest_path,
        )
    return str(PurePosixPath(*posix.parts))


def find_manifest(start):
    """Locate the nearest ``Breeze.toml`` from *start*, walking upward.

    ``start`` may be the project directory, any nested source file/directory,
    or the manifest itself.  Returns the manifest path or ``None``.
    """
    resolved = Path(start).resolve()
    if resolved.is_file():
        if resolved.name == MANIFEST_NAME:
            return resolved
        resolved = resolved.parent
    directory = resolved
    while True:
        candidate = directory / MANIFEST_NAME
        if candidate.is_file():
            return candidate
        parent = directory.parent
        if parent == directory:
            return None
        directory = parent


def load_manifest(path) -> BreezeManifest:
    """Parse and validate the manifest at *path*.

    Raises :class:`ManifestError` when the file cannot be read/parsed or
    fails validation; the message names the offending field.
    """
    manifest_path = Path(path)
    try:
        text = manifest_path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ManifestError(
            f"cannot read {manifest_path}: {exc.strerror or exc}",
            manifest_path,
        ) from exc
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(
            f"invalid TOML in {manifest_path}: {exc}", manifest_path
        ) from exc

    package = data.get("package")
    if not isinstance(package, dict):
        raise ManifestError(
            f"{manifest_path}: missing [package] section", manifest_path
        )

    name = package.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ManifestError(
            f"{manifest_path}: [package] name must be a non-empty "
            "identifier-like string (letters, digits, '_', '-', '.') "
            "starting with a letter or '_'",
            manifest_path,
        )

    version = _require_version(package.get("version"), "[package] version", manifest_path)

    identifier = package.get("identifier", DEFAULT_IDENTIFIER)
    if not isinstance(identifier, str) or identifier not in IDENTIFIER_TIERS:
        raise ManifestError(
            f"{manifest_path}: [package] identifier must be one of "
            f"{' < '.join(IDENTIFIER_TIERS)}, got {identifier!r}",
            manifest_path,
        )

    id_version = package.get("id_version")
    if id_version is not None:
        id_version = _require_version(
            id_version, "[package] id_version", manifest_path
        )

    description = package.get("description", "")
    if not isinstance(description, str):
        raise ManifestError(
            f"{manifest_path}: [package] description must be a string",
            manifest_path,
        )

    authors_value = package.get("authors", [])
    if not isinstance(authors_value, list) or not all(
        isinstance(item, str) for item in authors_value
    ):
        raise ManifestError(
            f"{manifest_path}: [package] authors must be a list of strings",
            manifest_path,
        )

    homepage = package.get("homepage", "")
    if not isinstance(homepage, str):
        raise ManifestError(
            f"{manifest_path}: [package] homepage must be a string",
            manifest_path,
        )

    entry = _parse_entry(data.get("entry", {}), manifest_path)
    dependencies = _parse_dependencies(data.get("dependencies", {}), manifest_path)

    return BreezeManifest(
        path=manifest_path.resolve(),
        name=name,
        version=version,
        identifier=identifier,
        id_version=id_version,
        description=description,
        authors=tuple(authors_value),
        homepage=homepage,
        entry=entry,
        dependencies=dependencies,
        raw=data,
    )


def _require_version(
    value,
    what: str,
    manifest_path: Path,
    pattern: "re.Pattern[str]" = _VERSION_RE,
) -> str:
    if not isinstance(value, str) or not pattern.match(value):
        raise ManifestError(
            f"{manifest_path}: {what} must be semver-like "
            f"(optional -pre/+build suffix), got {value!r}",
            manifest_path,
        )
    return value


def _parse_entry(value, manifest_path: Path) -> EntryConfig:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ManifestError(
            f"{manifest_path}: [entry] must be a table", manifest_path
        )

    source_value = value.get("source", DEFAULT_ENTRY_SOURCE)
    if not isinstance(source_value, str) or not source_value.strip():
        raise ManifestError(
            f"{manifest_path}: [entry] source must be a non-empty string",
            manifest_path,
        )
    source = _normalize_rel(source_value, "[entry] source", manifest_path)

    is_lib = value.get("is_lib", False)
    if not isinstance(is_lib, bool):
        raise ManifestError(
            f"{manifest_path}: [entry] is_lib must be a boolean",
            manifest_path,
        )

    module_value = value.get("module", DEFAULT_ENTRY_MODULE)
    if not isinstance(module_value, str) or not module_value.strip():
        raise ManifestError(
            f"{manifest_path}: [entry] module must be a non-empty string",
            manifest_path,
        )
    module = _normalize_rel(module_value, "[entry] module", manifest_path)

    return EntryConfig(source=source, is_lib=is_lib, module=module)


def _parse_dependencies(value, manifest_path: Path) -> dict[str, Dependency]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ManifestError(
            f"{manifest_path}: [dependencies] must be a table",
            manifest_path,
        )
    dependencies: dict[str, Dependency] = {}
    for dep_name, spec in value.items():
        if not isinstance(spec, str):
            raise ManifestError(
                f"{manifest_path}: [dependencies] {dep_name} must be a "
                '"version[,identifier]" string',
                manifest_path,
            )
        pieces = [piece.strip() for piece in spec.split(",")]
        if len(pieces) > 2:
            raise ManifestError(
                f"{manifest_path}: [dependencies] {dep_name} expects "
                f'"version[,identifier]", got {spec!r}',
                manifest_path,
            )
        version = _require_version(
            pieces[0], f"[dependencies] {dep_name} version", manifest_path,
            pattern=_DEP_VERSION_RE,
        )
        identifier = DEFAULT_IDENTIFIER
        if len(pieces) == 2:
            identifier = pieces[1]
            if identifier not in IDENTIFIER_TIERS:
                raise ManifestError(
                    f"{manifest_path}: [dependencies] {dep_name} identifier "
                    f"must be one of {' < '.join(IDENTIFIER_TIERS)}, "
                    f"got {identifier!r}",
                    manifest_path,
                )
        dependencies[dep_name] = Dependency(
            name=dep_name, version=version, identifier=identifier
        )
    return dependencies


def write_json(path, payload) -> Path:
    """Write pretty UTF-8 JSON, creating parent directories."""
    import json

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return target
