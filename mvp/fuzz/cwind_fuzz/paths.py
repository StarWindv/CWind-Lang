"""Central path + toolchain discovery for the CWind fuzzing tool.

The package lives at ``<repo>/mvp/fuzz/cwind_fuzz/``; the frontend source tree
lives at ``<repo>/mvp/frontend/src`` and the backend toolchain (``cwindc.exe``
+ the built static libs) at ``<repo>/build``.  Everything the generator,
analyzer and backend need to find on disk is resolved here once so the other
modules stay free of ``sys.path`` / ``..`` gymnastics.

Resolution order for the toolchain binaries honours env overrides first, then
sensible repo-local defaults, so a checkout that has already run ``cmake``
"just works" while CI can point anywhere via env vars.
"""

from __future__ import annotations

import os
import pathlib
import sys

# --- directory anchors ---
FUZZ_DIR = pathlib.Path(__file__).resolve().parent          # .../cwind_fuzz
ROOT = FUZZ_DIR.parent                                       # mvp/fuzz  (out/, known_bugs.json live here)
REPO = ROOT.parent                                           # mvp
REPO_ROOT = REPO.parent                                      # repository root (assets/, example/)
SRC = REPO / "frontend" / "src"                              # cwind_frontend package root
BUILD = REPO_ROOT / "build"                                  # cmake build dir (cwindc.exe, *.a)

_FRONTEND_ON_PATH = False


def ensure_frontend_import() -> None:
    """Put the frontend ``src`` on ``sys.path`` so ``cwind_frontend`` imports."""
    global _FRONTEND_ON_PATH
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    _FRONTEND_ON_PATH = True


def default_out_dir() -> pathlib.Path:
    return ROOT / "out"


# --- backend toolchain ---

def find_cwindc() -> pathlib.Path:
    """Locate the ``cwindc`` compiler driver.

    Env ``CWIND_CWINDC`` wins; otherwise ``<build>/cwindc`` (``.exe`` on
    Windows) is used when present.
    """
    env = os.environ.get("CWIND_CWINDC")
    if env:
        return pathlib.Path(env)
    exe = "cwindc.exe" if os.name == "nt" else "cwindc"
    cand = BUILD / exe
    if cand.exists():
        return cand
    return cand  # caller reports a clear "not built yet" error


def find_cwindf_entry() -> pathlib.Path:
    """The cwindf CLI entry point inside the frontend source tree."""
    return SRC / "cwind_frontend" / "cli.py"


def cwindf_cmd(base_args: list[str]) -> list[str]:
    """Build an argv list that runs the frontend CLI in-process-compatible form.

    We invoke it as a module so the same interpreter (which already knows the
    frontend on ``sys.path``) handles it; ``-m cwind_frontend.cli`` resolves via
    ``SRC`` on the path.
    """
    return [sys.executable, "-m", "cwind_frontend.cli", *base_args]


def frontend_env() -> dict:
    """``os.environ`` with the frontend ``src`` prepended to ``PYTHONPATH`` so
    the ``cwindf`` subprocess imports ``cwind_frontend`` even in a fresh venv
    that did not ``pip install`` it."""
    env = dict(os.environ)
    prev = env.get("PYTHONPATH", "")
    parts = [str(SRC)] + ([prev] if prev else [])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env
