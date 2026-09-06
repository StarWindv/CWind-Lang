"""Install-root discovery (todo-172-era std addressing).

The language std (``libs/``) is anchored to the **compiler executable**,
never to the working directory: a cwindf installed at
``<root>/.venv/Scripts/`` / ``<root>/scripts/`` / ``<root>/bin/`` (or run
from a development checkout) derives its install root from its own
location and resolves std as ``<root>/libs``.  ``CWIND_HOME`` overrides
the derivation explicitly.  External user packages live under
``<root>/pkgs/<publisher>/<pkg>/`` (addressing support only — no
installer).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

__all__ = ["install_root", "pkgs_root", "reset_install_root_cache"]

_HOME_ENV = "CWIND_HOME"
# Directories the compiler binary plausibly sits in inside an install
# root; walking up from the binary crosses these without treating them
# as the root.
_BINDIR_NAMES = {"scripts", "bin", "build", ".venv", "venv"}
# Cache: resolved start dir -> discovered root (``None`` cached too).
_CACHE: dict[str, Optional[Path]] = {}


def reset_install_root_cache() -> None:
    """Drop the discovery cache (tests / environment changes)."""
    _CACHE.clear()


def _package_dir() -> Optional[Path]:
    try:
        return Path(__file__).resolve().parent
    except OSError:  # pragma: no cover
        return None


def _entry_binary_dir() -> Optional[Path]:
    """Directory of the running ``cwindf`` (script / entry point)."""
    argv0 = sys.argv[0] if sys.argv else None
    if argv0:
        try:
            path = Path(argv0).resolve()
        except OSError:  # pragma: no cover
            path = None
        if path is not None and path.suffix.lower() != ".py":
            # A console-script / shebang binary (not this module file).
            return path.parent
        # ``python -m cwind_frontend.cli``: argv[0] is this module's file;
        # the package directory walks up through src / site-packages.
        return path.parent.parent if path is not None else None
    pkg = _package_dir()
    return pkg.parent if pkg is not None else None


def _walk_to_root(start: Path) -> Optional[Path]:
    """Walk up from *start* to the first directory owning ``libs/``."""
    directory = start
    while True:
        if (directory / "libs").is_dir():
            return directory
        parent = directory.parent
        if parent == directory:
            return None
        directory = parent


def _cached_discover(candidate: Optional[Path]) -> Optional[Path]:
    """Cached :func:`_discover` wrapper (per start directory)."""
    if candidate is None:
        return None
    key = str(candidate)
    if key not in _CACHE:
        _CACHE[key] = _discover(candidate)
    root = _CACHE[key]
    return root.resolve() if root is not None else None


def install_root() -> Optional[Path]:
    """The install root owning the std ``libs/`` tree, or ``None``.

    Order: ``CWIND_HOME`` (explicit override) → the running executable's
    location → the installed package's location.  Independent of the
    current working directory.  The first candidate whose derivation
    succeeds wins — a development checkout running from source resolves
    through the package location, an installed binary through its own.
    """
    home = os.environ.get(_HOME_ENV)
    if home:
        path = Path(home)
        if path.is_dir():
            return path.resolve()
    for candidate in (_entry_binary_dir(), _package_dir()):
        root = _cached_discover(candidate)
        if root is not None:
            return root
    return None


def _discover(start: Path) -> Optional[Path]:
    """Walk up from *start* to the first directory owning ``libs/``."""
    directory = start
    while True:
        if (directory / "libs").is_dir():
            root = directory
            # Directories that merely CONTAIN the binary (bin/, .venv/,
            # Scripts/...) are not the root even when they hold a libs/
            # dir of their own — the root is the first ancestor outside
            # them that owns libs/.
            if root.name.lower() in _BINDIR_NAMES and root.parent != root:
                root = _discover(root.parent) or root
            return root.resolve()
        parent = directory.parent
        if parent == directory:
            return None
        directory = parent


def pkgs_root() -> Optional[Path]:
    """The external-package directory of the install root (if any)."""
    root = install_root()
    if root is None:
        return None
    pkgs = root / "pkgs"
    return pkgs if pkgs.is_dir() else None


def default_import_root() -> Path:
    """Import anchor when no entry file identifies a project.

    The install root (executable-derived) rather than the working
    directory; ``Path.cwd()`` only as a last resort when no install root
    can be derived at all.
    """
    root = install_root()
    return root if root is not None else Path.cwd().resolve()
