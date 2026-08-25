"""Breeze.toml project manifests (todo-71).

``Breeze.toml`` is the CWind counterpart of Rust's ``Cargo.toml``: it
names the package, pins the entry source and declares dependencies.
todo-71 covers discovery and parsing only — dependency resolution and
downloads stay with todo-37/41 until a package registry exists.

Example::

    [package]
    name = "demo"
    version = "0.1.0"          # required, semver-ish
    entry = "src/main.wind"    # optional, default src/main.wind
    authors = ["Someone"]
    description = "..."

    [dependencies]             # parsed, stored raw for future use
    math = "1.0"
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

__all__ = [
    "DEFAULT_ENTRY",
    "MANIFEST_NAME",
    "BreezeManifest",
    "ManifestError",
    "find_manifest",
    "load_manifest",
]

MANIFEST_NAME = "Breeze.toml"
DEFAULT_ENTRY = "src/main.wind"

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
_VERSION_RE = re.compile(
    r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)


class ManifestError(ValueError):
    """A missing, unreadable or structurally invalid manifest."""

    def __init__(self, message: str, path=None) -> None:
        self.path = Path(path) if path is not None else None
        super().__init__(message)


@dataclass
class BreezeManifest:
    """Parsed ``Breeze.toml``.

    ``entry`` is normalized to forward slashes relative to :attr:`root`;
    unknown tables/keys survive in ``raw`` so future fields keep loading
    on older frontends.
    """

    path: Path
    name: str
    version: str
    entry: str = DEFAULT_ENTRY
    authors: tuple[str, ...] = ()
    description: str = ""
    dependencies: dict[str, object] = field(default_factory=dict)
    raw: dict[str, object] = field(default_factory=dict)

    @property
    def root(self) -> Path:
        """Project root: the directory holding the manifest."""
        return self.path.parent

    def entry_path(self) -> Path:
        """Absolute path of the package entry source."""
        return self.root / Path(*PurePosixPath(self.entry).parts)


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

    version = package.get("version")
    if not isinstance(version, str) or not _VERSION_RE.match(version):
        raise ManifestError(
            f"{manifest_path}: [package] version must be semver-like "
            "'MAJOR.MINOR.PATCH' (optional -pre/+build suffix), "
            f"got {version!r}",
            manifest_path,
        )

    entry_value = package.get("entry", DEFAULT_ENTRY)
    if not isinstance(entry_value, str) or not entry_value.strip():
        raise ManifestError(
            f"{manifest_path}: [package] entry must be a non-empty string",
            manifest_path,
        )
    posix_entry = PurePosixPath(entry_value.replace("\\", "/"))
    if posix_entry.is_absolute() or ".." in posix_entry.parts:
        raise ManifestError(
            f"{manifest_path}: [package] entry must be a relative path "
            f"inside the project, got {entry_value!r}",
            manifest_path,
        )
    entry = str(PurePosixPath(*posix_entry.parts))

    authors_value = package.get("authors", [])
    if not isinstance(authors_value, list) or not all(
        isinstance(item, str) for item in authors_value
    ):
        raise ManifestError(
            f"{manifest_path}: [package] authors must be a list of strings",
            manifest_path,
        )

    description = package.get("description", "")
    if not isinstance(description, str):
        raise ManifestError(
            f"{manifest_path}: [package] description must be a string",
            manifest_path,
        )

    dependencies = data.get("dependencies", {})
    if not isinstance(dependencies, dict):
        raise ManifestError(
            f"{manifest_path}: [dependencies] must be a table",
            manifest_path,
        )

    return BreezeManifest(
        path=manifest_path.resolve(),
        name=name,
        version=version,
        entry=entry,
        authors=tuple(authors_value),
        description=description,
        dependencies=dict(dependencies),
        raw=data,
    )


def ensure_dir(path) -> Path:
    """Create *path* (and parents) as needed; returns it resolved."""
    resolved = Path(path)
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


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


if __name__ == "__main__":  # pragma: no cover - manual smoke aid
    target = os.environ.get("BREEZE_MANIFEST") or os.getcwd()
    found = find_manifest(target)
    print(found if found else f"no {MANIFEST_NAME} above {target}")
