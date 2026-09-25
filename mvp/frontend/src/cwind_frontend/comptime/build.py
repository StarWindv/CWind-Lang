"""Compile const-fn evaluation units into share DLLs (task: comptime).

Same toolchain shape as procedure-macro builds (``macros/proc/build.py``)
— generated source --cwindf--> typed JSON --cwindc--> link library —
with three deliberate differences:

* **Cache key = hash of the generated source** (the whole closure +
  wrapper text).  The proc-macro key hashes only the macro's own tokens
  and lets stale *dependencies* slip through; a const-fn unit burns its
  result into the user's program, so a dependency edit MUST invalidate.
* **share mode**: the unit has no ``main``; ``cwindc --emit share``
  exports exactly the wrapper (``cw_const_eval``).
* **Recursion guard**: the child ``cwindf`` inherits
  ``CWIND_CONSTFN_BUILDING`` — if the unit's own compilation trips
  another const-fn evaluation (e.g. through a ``use``-loaded std const
  that calls a const fn), that evaluation errors instead of nesting.
"""

from __future__ import annotations

import itertools
import json
import os
import threading
from pathlib import Path
from typing import Optional

from ..macros.proc.build import (
    _INDEX_NAME,
    _child_env,
    _frontend_command,
    _index_lookup,
    _index_store,
    _mirror,
    _read_index,
    _run,
    _strip_ansi,
    find_cwindc,
)
from .closure import UNIT_VERSION, EvaluationUnit

__all__ = [
    "UnitBuild",
    "build_unit",
    "toolchain_available",
    "load_result",
    "store_result",
    "GUARD_ENV",
]

_GUARD_ENV = "CWIND_CONSTFN_BUILDING"
# Public alias used by the evaluator for the recursion guard.
GUARD_ENV = _GUARD_ENV

_CACHE_ROOT_NAME = "cwind-constfn"
_WORK_DIR_NAME = "constfn"
_COMPILE_TIMEOUT = 900.0

_INDEX_LOCK = threading.Lock()
_WORK_SERIAL = itertools.count()


def _work_dir(project_base: Path, key: str) -> Path:
    """Private per (process, attempt) workspace for one unit build.

    Same discipline as ``macros/proc/build._work_dir`` (pid + serial so
    parallel workers never race intermediates) and the same
    project/temp split: an anchored project keeps its workspace under
    ``<project>/target/constfn`` (cleanable with the rest of
    ``target/``); a loose single file parks it under the shared
    system-temp cache root so no ``target/`` is littered next to the
    source.
    """
    from ..parser.defs import project_target_base

    token = f"{key}-{os.getpid()}-{next(_WORK_SERIAL)}"
    target = project_target_base(project_base)
    if target is not None:
        return target / _WORK_DIR_NAME / token
    return cache_root() / "work" / token


class UnitBuild:
    """A compiled evaluation unit (one DLL per unit key)."""

    def __init__(
        self,
        unit: EvaluationUnit,
        dll: Optional[Path],
        build_error: Optional[str] = None,
    ) -> None:
        self.unit = unit
        self.dll = dll
        self.build_error = build_error

    @property
    def ok(self) -> bool:
        return self.dll is not None and self.build_error is None


def toolchain_available() -> bool:
    return find_cwindc() is not None


def guard_active() -> bool:
    """Whether this process is itself a const-fn unit compilation."""
    return bool(os.environ.get(_GUARD_ENV))


def cache_root() -> Path:
    override = os.environ.get("CWIND_CONSTFN_CACHE")
    if override:
        return Path(override)
    import tempfile

    return Path(tempfile.gettempdir()) / _CACHE_ROOT_NAME


def build_unit(unit: EvaluationUnit, project_base: Path) -> UnitBuild:
    """Compile *unit* (cached by its generated-source hash) -> DLL."""
    key = unit.key
    root = cache_root()
    entry = root / key
    cached = entry / "unit.dll"
    if _index_lookup(root, key) and cached.is_file():
        return UnitBuild(unit, cached)
    workdir = _work_dir(project_base, key)
    dll = workdir / "unit.dll"
    if dll.is_file():
        _mirror(dll, cached)
        _index_store(root, key, value="unit.dll")
        return UnitBuild(unit, cached)
    try:
        workdir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return UnitBuild(unit, None, f"cannot create build directory: {exc}")
    token = f"{os.getpid()}-{next(_WORK_SERIAL)}"
    source = workdir / f".cwind-constfn-{key[:16]}-{token}.wind"
    typed = workdir / "unit.typed.json"
    try:
        source.write_text(unit.program_text, encoding="utf-8", newline="\n")
    except OSError as exc:
        return UnitBuild(
            unit, None, f"cannot write the evaluation unit source: {exc}"
        )
    cwindc = find_cwindc()
    if cwindc is None:
        _unlink(source)
        return UnitBuild(
            unit,
            None,
            "the CWind backend (cwindc) was not found; set CWIND_CWINDC or "
            "build it (scripts/build.ps1)",
        )
    frontend = _frontend_command()
    env = _child_env()
    env[_GUARD_ENV] = key
    rc, out = _run(
        [*frontend, "--typed-ast", "--emit", "share", "--no-std",
         str(source)],
        cwd=str(workdir), env=env, timeout=_COMPILE_TIMEOUT,
    )
    if rc != 0:
        # Keep the generated source and the full child output next to the
        # workspace: the condensed SA error alone cannot diagnose a bad
        # closure, and workspaces are disposable scratch space.
        try:
            (workdir / "cwindf.log").write_text(out, encoding="utf-8")
        except OSError:
            pass
        return UnitBuild(unit, None, _format_failure("cwindf", out))
    _unlink(source)
    typed.write_text(out, encoding="utf-8")
    rc, out = _run(
        [str(cwindc), "--emit", "share", str(typed), "-o", str(dll)],
        cwd=str(workdir), env=env, timeout=_COMPILE_TIMEOUT,
    )
    if rc != 0 or not dll.is_file():
        try:
            (workdir / "cwindc.log").write_text(out, encoding="utf-8")
        except OSError:
            pass
        return UnitBuild(unit, None, _format_failure("cwindc", out))
    _mirror(dll, cached)
    _index_store(root, key, value="unit.dll")
    return UnitBuild(unit, cached)


def _format_failure(stage: str, output: str) -> str:
    # 与过程宏同纪律 (macros/proc/build._format_failure): 子编译的
    # 完整多行诊断原样保留 —— tgqe 的 span label 逐行渲染它; 压平成
    # `` | `` 单行嵌进外层报错正是之前 const-fn 错误不可读的根源。
    return (
        f"failed to compile const-fn evaluation unit ({stage}):\n"
        + _strip_ansi(output).strip()
    )


# -- evaluation-result cache -------------------------------------------------
# const-fn 是纯函数 (前提): 同一单元 + 同一实参必然同一结果。DLL 构建
# 已按单元源码哈希缓存, 但每次编译仍重跑 ctypes 调用 (fib(42) ≈ 秒级)。
# 结果以 ``<cache>/<unit key>/results.json`` 持久化: key 为实参的
# encode_cv JSON, 值为 encode_cv 的结果 —— 命中后连 DLL 都不加载。
# 写入为原子替换; 并发进程最后写者胜 (丢一次重算, 不损坏)。

_RESULTS_LOCK = threading.Lock()


def _results_path(unit_key: str) -> Path:
    return cache_root() / unit_key / "results.json"


def load_result(unit_key: str, args_key: str) -> tuple[bool, object]:
    """Cached (encoded) result for *args_key*, or ``(False, None)``."""
    try:
        data = json.loads(_results_path(unit_key).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, None
    if isinstance(data, dict) and args_key in data:
        return True, data[args_key]
    return False, None


def store_result(unit_key: str, args_key: str, encoded: object) -> None:
    """Persist one evaluation result (read-merge, atomic replace)."""
    with _RESULTS_LOCK:
        path = _results_path(unit_key)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (OSError, json.JSONDecodeError):
            data = {}
        data[args_key] = encoded
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, allow_nan=True),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except OSError:
            pass


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
