"""Compile procedure macros into standalone executables (todo-179).

The host generates a CWind program (macro body + dependency closure +
protocol harness), then compiles it with the ordinary toolchain::

    main.wind --cwindf--> main.typed.json --cwindc--> macro.exe

Builds are content-addressed: the key covers the generated program, the
toolchain stamp and the std tree fingerprint, and compiled exes are
mirrored under the system temp directory so repeated compiles (fresh
checkouts, throwaway test projects) hit the cache.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .deps import generate_program
from .definition import ProcMacroDef
from .errors import ProcMacroError

__all__ = [
    "MacroBuild",
    "build_macro",
    "find_cwindc",
    "toolchain_available",
    "BUILD_VERSION",
]

BUILD_VERSION = 3
_COMPILE_TIMEOUT = 900.0


@dataclass
class MacroBuild:
    """A compiled macro program (one exe per content hash)."""

    key: str
    exe: Optional[Path]
    program_text: str
    workdir: Optional[Path]
    build_error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.exe is not None and self.build_error is None


def find_cwindc() -> Optional[Path]:
    """Locate the backend binary (env -> install root -> PATH)."""
    override = os.environ.get("CWIND_CWINDC")
    if override:
        path = Path(override)
        return path if path.is_file() else None
    try:
        from ...home import install_root

        root = install_root()
    except Exception:
        root = None
    if root is not None:
        for rel in (
            Path("build") / "cwindc.exe",
            Path("build") / "cwindc",
            Path("mvp") / "build" / "cwindc.exe",
            Path("mvp") / "build" / "cwindc",
        ):
            candidate = root / rel
            if candidate.is_file():
                return candidate
    from shutil import which

    found = which("cwindc")
    return Path(found) if found else None


def toolchain_available() -> bool:
    """Whether the backend needed for procedure macros is present."""
    return find_cwindc() is not None


def build_macro(
    defn: ProcMacroDef,
    local_defs: dict[str, ProcMacroDef],
    *,
    project_base: Path,
    cache_dir: Optional[Path] = None,
) -> MacroBuild:
    """Compile *defn* (cached by content) and return its build record."""
    program = generate_program(defn, local_defs)
    key = _content_key(program)
    mirror = _mirror_dir(cache_dir) / key
    exe_mirror = mirror / "macro.exe"
    if exe_mirror.is_file():
        return MacroBuild(key, exe_mirror, program, mirror)
    workdir = _work_dir(project_base, key)
    exe = workdir / "macro.exe"
    if exe.is_file():
        _mirror(exe, exe_mirror)
        return MacroBuild(key, exe, program, workdir)
    try:
        workdir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return MacroBuild(key, None, program, None,
                          f"cannot create build directory: {exc}")
    # The generated entry must sit in the defining file's directory: the
    # file's ``use crate::...`` / relative-module imports assume exactly
    # that module context (a std file's ``crate`` is the std root, a
    # project file's ``crate`` is the package root).  The name is hidden
    # and non-``.wind`` so neither the module tree nor the definition scan
    # ever picks it up; it is removed once the build finishes.
    source_dir = _source_dir(defn, workdir)
    source = source_dir / f".cwind-procmacro-{key[:16]}.tmp"
    typed = workdir / "main.typed.json"
    try:
        source.write_text(program, encoding="utf-8", newline="\n")
    except OSError as exc:
        return MacroBuild(
            key, None, program, workdir,
            f"cannot write the generated program next to the macro "
            f"definition ({source_dir}): {exc}",
        )
    cwindc = find_cwindc()
    if cwindc is None:
        _unlink(source)
        return MacroBuild(
            key, None, program, workdir,
            "the CWind backend (cwindc) was not found; set CWIND_CWINDC or "
            "build it (scripts/build.ps1)",
        )
    frontend = _frontend_command()
    env = _child_env()
    try:
        rc, out = _run(
            [*frontend, "--typed-ast", str(source)],
            cwd=str(workdir), env=env, timeout=_COMPILE_TIMEOUT,
        )
    finally:
        _unlink(source)
    if rc != 0:
        return MacroBuild(
            key, None, program, workdir,
            _format_failure("cwindf", out, defn),
        )
    typed.write_text(out, encoding="utf-8")
    rc, out = _run(
        [str(cwindc), str(typed), "-o", str(exe)],
        cwd=str(workdir), env=env, timeout=_COMPILE_TIMEOUT,
    )
    if rc != 0 or not exe.is_file():
        return MacroBuild(
            key, None, program, workdir,
            _format_failure("cwindc", out, defn),
        )
    _mirror(exe, exe_mirror)
    return MacroBuild(key, exe, program, workdir)


def _source_dir(defn: ProcMacroDef, fallback: Path) -> Path:
    if defn.source_path:
        directory = Path(defn.source_path).resolve().parent
        if directory.is_dir():
            return directory
    return fallback


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _frontend_command() -> list[str]:
    override = os.environ.get("CWINDF")
    if override:
        return [override]
    return [sys.executable, "-m", "cwind_frontend.cli"]


def _child_env() -> dict:
    env = dict(os.environ)
    package_dir = Path(__file__).resolve().parents[2]
    src_root = str(package_dir.parent)
    pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        src_root if not pythonpath else src_root + os.pathsep + pythonpath
    )
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def _run(
    cmd: list[str], *, cwd: str, env: dict, timeout: float
) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 1, f"command timed out after {timeout:g}s: {' '.join(cmd)}"
    except OSError as exc:
        return 1, f"cannot run {' '.join(cmd)}: {exc}"
    output = proc.stdout.decode("utf-8", "replace")
    return proc.returncode, output


def _format_failure(stage: str, output: str, defn: ProcMacroDef) -> str:
    return (
        f"failed to compile procedure macro '{defn.name}' ({stage}):\n"
        + _strip_ansi(output).strip()
    )


def _strip_ansi(text: str) -> str:
    import re

    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _content_key(program: str) -> str:
    digest = hashlib.sha256()
    digest.update(f"v{BUILD_VERSION}\0".encode())
    digest.update(program.encode("utf-8"))
    digest.update(b"\0toolchain\0")
    digest.update(_toolchain_stamp().encode())
    digest.update(b"\0std\0")
    digest.update(_std_stamp().encode())
    return digest.hexdigest()[:24]


def _toolchain_stamp() -> str:
    parts = [sys.version.split()[0]]
    cwindc = find_cwindc()
    if cwindc is not None:
        try:
            stat = cwindc.stat()
            parts.append(f"{cwindc}:{stat.st_mtime_ns}:{stat.st_size}")
        except OSError:
            parts.append(str(cwindc))
    return "|".join(parts)


def _std_stamp() -> str:
    try:
        from ...home import install_root

        root = install_root()
    except Exception:
        root = None
    if root is None:
        return ""
    libs = root / "libs"
    if not libs.is_dir():
        return ""
    entries: list[str] = []
    for path in sorted(libs.rglob("*")):
        if path.is_file() and path.suffix in (".wind", ".wd"):
            try:
                stat = path.stat()
                entries.append(
                    f"{path.name}:{stat.st_mtime_ns}:{stat.st_size}"
                )
            except OSError:
                continue
    return "|".join(entries)


def _work_dir(project_base: Path, key: str) -> Path:
    return project_base / "target" / "procmacro" / key


def _mirror_dir(cache_dir: Optional[Path]) -> Path:
    if cache_dir is not None:
        return cache_dir
    return Path(tempfile.gettempdir()) / "cwind-procmacro"


def _mirror(exe: Path, target: Path) -> None:
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.is_file():
            import shutil

            shutil.copy2(exe, target)
    except OSError:
        pass
