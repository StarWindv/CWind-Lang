"""Compile procedure macros into standalone executables (todo-179).

The host generates a CWind program (macro body + dependency closure +
protocol harness), then compiles it with the ordinary toolchain::

    main.wind --cwindf--> main.typed.json --cwindc--> macro.exe

Builds are cached in the **system temp directory** with a JSON index; the
cache key is a normalized hash of the macro definition's tokens (token
kinds + decoded values, positions and whitespace ignored, the macro's own
name normalized to a placeholder).  Renaming the macro or reindenting its
body therefore reuses the compiled exe; editing literals/operators/
identifiers rebuilds it.

The token-only key means a *dependency* item copied out of the defining
file does not invalidate the entry: this is a deliberately temporary
cache (the todo-179 gap list records the trade-off).
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ...ast_components.token import TokenKind
from .deps import generate_program
from .definition import ProcMacroDef

__all__ = [
    "MacroBuild",
    "build_macro",
    "find_cwindc",
    "toolchain_available",
    "BUILD_VERSION",
]

# Invalidate executables built before module-addressed macro resolution.
# The generated program now compiles with definition-site imports resolved
# through the module tree (no global bare-name helpers), so pre-10 exes
# may embed differently resolved programs.  v11: generated programs compile
# in no-std mode (no implicit std prelude / whole-tree trait-impl pull), so
# pre-11 exes may embed a whole-std dependency closure.  v12: generated
# programs also defer unreachable macro-bearing std bodies, so building
# ``format`` no longer eagerly expands ``panic``'s ``println!`` back into
# itself (clean-cache bootstrap).
BUILD_VERSION = 12
_COMPILE_TIMEOUT = 900.0
_CACHE_ROOT_NAME = "cwind-procmacro"
_INDEX_NAME = "index.json"
# 递归构建护栏: 编译过程宏本体时, 被编译的程序会再次加载 prelude; 若
# prelude/std 自身调用了过程宏 (如 panic 里用 `println!`), 就会在宏本体
# 尚未构建完成时再次请求同一宏 —— 记在环境变量里 (随子进程继承) 并
# 快速失败, 而不是无限递归把进程挂死。
_BUILD_ENV = "CWIND_PROCMACRO_BUILDING"

_INDEX_LOCK = threading.Lock()
# Serial numbers for build workspaces/temp files; combined with the process
# id they make every build attempt use a private directory/file, so parallel
# workers can never race on one definition's intermediate objects.
_WORK_SERIAL = itertools.count()


def _active_builds() -> tuple[str, ...]:
    raw = os.environ.get(_BUILD_ENV, "")
    return tuple(k for k in raw.split(",") if k)


@dataclass
class MacroBuild:
    """A compiled macro program (one exe per definition-token hash)."""

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
    """Compile *defn* (cached by definition-token hash) -> build record.

    The persistent cache lives in the system temp directory (``index.json``
    + one ``macro.exe`` per definition hash); the transient compile
    workspace follows the project/temp split (see :func:`_work_dir`):
    project compiles keep it under ``<project>/target/procmacro/``, a
    loose single file under the system temp directory — the toolchain
    never writes next to the source.
    """
    program = generate_program(defn, local_defs)
    key = definition_key(defn)
    active = _active_builds()
    if key in active:
        return MacroBuild(
            key, None, program, None,
            f"recursive procedure-macro build: '{defn.name}' is requested "
            "while its own generated program is still being compiled "
            "(the dependency/std surface must not invoke procedure macros "
            "during their own bootstrap)",
        )
    root = cache_root(cache_dir)
    entry = root / key
    cached_exe = entry / "macro.exe"
    if _index_lookup(root, key) and cached_exe.is_file():
        return MacroBuild(key, cached_exe, program, entry)
    workdir = _work_dir(project_base, key, cache_dir)
    exe = workdir / "macro.exe"
    if exe.is_file():
        _mirror(exe, cached_exe)
        _index_store(root, key)
        return MacroBuild(key, cached_exe, program, workdir)
    try:
        workdir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return MacroBuild(key, None, program, None,
                          f"cannot create build directory: {exc}")
    source_dir = _source_dir(defn, workdir)
    # A process/thread-unique temp name: parallel builds (``cwindf -j``,
    # ``pytest -n``) of the same definition must not share one temp file
    # (one worker unlinking it while another's ``cwindf`` reads it).
    token = f"{os.getpid()}-{next(_WORK_SERIAL)}"
    source = source_dir / f".cwind-procmacro-{key[:16]}-{token}.tmp"
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
    env[_BUILD_ENV] = ",".join((*active, key))
    try:
        rc, out = _run(
            [*frontend, "--typed-ast", "--no-std", str(source)],
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
    _mirror(exe, cached_exe)
    _index_store(root, key)
    return MacroBuild(key, cached_exe, program, workdir)


def cache_root(cache_dir: Optional[Path] = None) -> Path:
    """The system-temp cache directory (``CWIND_PROCMACRO_CACHE`` over-
    ridable; *cache_dir* wins when given)."""
    if cache_dir is not None:
        return Path(cache_dir)
    override = os.environ.get("CWIND_PROCMACRO_CACHE")
    if override:
        return Path(override)
    return Path(tempfile.gettempdir()) / _CACHE_ROOT_NAME


def definition_key(defn: ProcMacroDef) -> str:
    """Stable content key for one macro definition.

    Token kinds + decoded values only: positions, raw spellings and
    whitespace are ignored, and every token whose text is the macro's own
    name hashes to a placeholder — renaming or reformatting the
    definition reuses the cache, editing anything semantic rebuilds it.
    """
    digest = hashlib.sha256()
    digest.update(f"cwind-procmacro-v{BUILD_VERSION}\0protocol-v2-span-i64\0".encode())
    digest.update((defn.kind + "\0").encode())
    for tok in defn.fn_tokens:
        if tok.kind == TokenKind.COMMENT:
            continue
        value = str(tok.value)
        if tok.kind == TokenKind.IDENTIFIER and value == defn.function_name:
            value = "<self>"
        digest.update(tok.kind.name.encode())
        digest.update(b"\x1f")
        digest.update(value.encode("utf-8", "surrogatepass"))
        digest.update(b"\x1e")
    return digest.hexdigest()[:32]


def _index_path(root: Path) -> Path:
    return root / _INDEX_NAME


def _read_index(root: Path) -> dict:
    try:
        data = json.loads(_index_path(root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict) or data.get("version") != BUILD_VERSION:
        return {}
    entries = data.get("entries")
    return entries if isinstance(entries, dict) else {}


def _index_lookup(root: Path, key: str) -> bool:
    return key in _read_index(root)


def _index_store(root: Path, key: str, value: str = "macro.exe") -> None:
    with _INDEX_LOCK:
        entries = _read_index(root)
        entries[key] = value
        payload = json.dumps(
            {"version": BUILD_VERSION, "entries": entries},
            ensure_ascii=False, indent=0,
        )
        target = _index_path(root)
        try:
            root.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".json.tmp")
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, target)
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


def _source_dir(defn: ProcMacroDef, fallback: Path) -> Path:
    if defn.source_path:
        directory = Path(defn.source_path).resolve().parent
        if directory.is_dir():
            return directory
    return fallback


def _work_dir(
    project_base: Path, key: str, cache_dir: Optional[Path] = None
) -> Path:
    # Private per (process, attempt): the definition's ``macro.exe.o`` /
    # typed JSON / exe are written here by ``cwindc`` and gcc, and sharing
    # one directory across parallel workers races those intermediates
    # (``ld: cannot find .../macro.exe.o``).  The **shared** result cache is
    # the system-temp mirror, not this workspace.  Placement follows the
    # project/temp split: an anchored project keeps its workspace under
    # ``<project>/target/procmacro``; a loose single file parks it under
    # the system-temp cache root so no ``target/`` is littered next to
    # the source (gcc on this toolchain writes Unicode temp paths fine).
    from ...parser.defs import project_target_base

    token = f"{key}-{os.getpid()}-{next(_WORK_SERIAL)}"
    target = project_target_base(project_base)
    if target is not None:
        return target / "procmacro" / token
    return cache_root(cache_dir) / "work" / token


def _mirror(exe: Path, target: Path) -> None:
    """Copy *exe* into the shared cache atomically.

    Parallel compiles (``pytest -n`` workers, ``cwindf -j`` jobs) can build
    the same definition at once; a plain ``copy2`` leaves a half-written
    executable visible under *target*, and another process launching it
    fails with ``WinError 1392`` (file corrupted/unreadable).  Write a
    process-unique temp file and ``os.replace`` it into place — the cached
    path therefore always names a complete file.
    """
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file():
            return
        import os
        import shutil

        tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
        try:
            shutil.copy2(exe, tmp)
            os.replace(tmp, target)
        except OSError:
            _unlink(tmp)
    except OSError:
        pass


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
