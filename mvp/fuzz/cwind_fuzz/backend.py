"""Full-backend fuzzing: generate valid programs, compile them all the way to a
native executable and *run* it, classifying the outcome.

Where the frontend ``fuzz_sa`` only checks that the semantic analyzer stays
silent, ``backend`` walks the entire pipeline that todo-155 (precise stack-map /
shadow-frame GC) actually touches:

    .wind source  ->  cwindf --typed-ast  ->  typed.json
                  ->  cwindc --emit-exe    ->  program.exe
                  ->  run under a timeout  ->  {ok, hang, crash,
                                                frontend_err, compile_err}

The whole point is to catch regressions the type-checker cannot see: GC hangs
(the shadow-frame linked list cycling), crashes from a slot left un-registered,
or over-retention.  Every generated program carries a *main* that exercises the
GC stack map hard — bounded loops that allocate reference-bearing slots and call
``builtins::gc_collect()`` from inside a callee (the exact ``hang_g5`` shape),
plus multi-frame call chains.

Programs are built valid-by-construction from the same ``Generator`` the
frontend uses (so we reuse its SA-clean guarantee) plus a deterministic churn
``main``.  A program that fails the *frontend* stage is counted separately: it
is an SA concern, not a backend one, and is not mislabelled as a crash.
"""

from __future__ import annotations

import json
import os
import pathlib
import random
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Optional

from .paths import cwindf_cmd, find_cwindc, frontend_env, ROOT
from .frontend import Generator, Case, analyze, sig_of, match_known_bug


# ---------------------------------------------------------------------------
# Program synthesis: GC-churn main + selected fragments
# ---------------------------------------------------------------------------

# Bounded churn knobs.  ``N`` iterations keep every generated case a
# few milliseconds at -O0 while still driving dozens of allocations + a
# collect inside a callee.  Kept small on purpose: the fuzzer's job is
# coverage of *shapes*, not soak.
_CHURN_N = 48


def _gc_churn_main() -> str:
    """A ``main`` that stresses the precise stack map through several shapes:

    * allocation in a callee (``churn``) followed by ``builtins::gc_collect()``
      *inside* the callee — the shape that regressed before todo-155's fix;
    * a for-in loop over a freshly built ``Map`` (registers a shadow-frame
      node every iteration; exercises the loop-node cycle bug);
    * a nested call chain (``helper`` -> ``churn``) so multiple frames are live
      at the collection point.
    """
    return (
        f"fn helper(x: Int) -> Int {{ return x + 1; }}\n"
        f"fn churn(n: Int) -> Int {{\n"
        "  let mut keep: Vector<Int> = Vector::new();\n"
        "  let mut acc: Int = 0;\n"
        "  let mut i: Int = 0;\n"
        "  while (i < n) {\n"
        "    let mut m: Map<Int, Int> = Map::new();\n"
        "    m.set(i, i * 2);\n"
        "    for kv in m { acc += kv.0; }\n"
        "    keep.push_back(helper(m.get(i)));\n"
        "    i += 1;\n"
        "  }\n"
        "  builtins::gc_collect();\n"
        "  return acc + keep.length();\n"
        "}\n"
        "fn main() -> Int {\n"
        f"  let r: Int = churn({_CHURN_N});\n"
        "  builtins::print(r);\n"
        "  builtins::gc_collect();\n"
        "  return 0;\n"
        "}\n"
    )


class GcProgramBuilder:
    """Compose a runnable program: a random set of valid feature fragments
    followed by the deterministic GC-churn ``main``."""

    def __init__(self, rng: random.Random):
        self.rng = rng

    def build(self) -> str:
        gen = Generator(self.rng)
        frags: list[str] = []
        for _ in range(self.rng.randint(0, 3)):
            frags.append(self._one_fragment(gen))
        frags = [f for f in frags if f]
        body = "\n".join(frags)
        return f"{body}\n{_gc_churn_main()}"

    def _one_fragment(self, gen: Generator) -> str:
        # gc_collect()/print/while/for-in-over-container are the load-bearing
        # shapes; sprinkle one random *backend-safe* SA fragment on top so we
        # also cross-check that ordinary constructs coexist with the
        # shadow-frame codegen.  The full set of gen_* fragments is exercised
        # by the frontend mode; here we deliberately avoid the few fragments
        # that hit a known *backend* limitation (e.g. ``Map.entry()`` bound to
        # a name, which the backend rejects with a compile error) so a clean
        # run stays an *ok* and does not show up as a false compile_err.
        pick = self.rng.randrange(3)
        if pick == 0:
            return gen.gen_nested()
        if pick == 1:
            return gen.gen_for()
        return gen.gen_compound_assign()


# ---------------------------------------------------------------------------
# Per-program pipeline
# ---------------------------------------------------------------------------


@dataclass
class BackendCase:
    index: int
    source: str
    result: dict = field(default_factory=dict)


def _run_capture(argv: list[str], cwd: Optional[str], timeout: float,
                 env: Optional[dict] = None) -> tuple[int, str]:
    """Run a subprocess, capture combined output, kill on timeout (rc=124)."""
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            timeout=timeout,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return proc.returncode, (proc.stdout or "")
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or b""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        return 124, out or ""
    except OSError as exc:
        return 127, f"spawn failed: {exc}\n"


def compile_and_run(
    source: str,
    workdir: pathlib.Path,
    *,
    timeout_compile: float = 120.0,
    timeout_run: float = 15.0,
    opt: bool = False,
) -> dict:
    """Drive one program through the whole backend.  Returns a result dict
    with keys ``kind`` (ok|frontend_err|compile_err|hang|crash), plus
    diagnostics.

    ``kind`` meanings:
      * ``ok``            — ran to completion with rc==0
      * ``frontend_err``  — cwindf rejected a program the frontend said was
                            clean (pipeline inconsistency, a real bug)
      * ``compile_err``   — cwindc could not build the exe
      * ``hang``          — exe hit the run timeout (todo-155 shadow-frame
                            cycle would surface exactly here)
      * ``crash``         — exe exited non-zero (signal / abort) or rc==127
                            spawn failure
    """
    workdir.mkdir(parents=True, exist_ok=True)
    wind = workdir / "case.wind"
    typed = workdir / "case.json"
    exe = workdir / "case.exe" if os.name == "nt" else workdir / "case.bin"
    wind.write_text(source, encoding="utf-8")

    frontend = analyze(source)
    if frontend["kind"] != "clean":
        # Generator/SA inconsistency: the frontend should have accepted this.
        # Record but do NOT try to compile (we would chase the wrong bug).
        return {
            "kind": "frontend_err",
            "stage": "frontend",
            "frontend": frontend,
        }

    fargv = cwindf_cmd(["--typed-ast", str(wind)])
    # cwindf writes the typed-ast JSON to *stdout*; redirect it into the file
    # and capture stderr separately so we do not mistake the JSON payload for
    # a failure.
    try:
        with open(typed, "w", encoding="utf-8") as fout:
            proc = subprocess.run(
                fargv,
                cwd=None,
                timeout=timeout_compile,
                stdout=fout,
                stderr=subprocess.PIPE,
                env=frontend_env(),
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        rc, ferr = proc.returncode, (proc.stderr or "")
    except subprocess.TimeoutExpired:
        rc, ferr = 124, "cwindf timeout"
    except OSError as exc:
        rc, ferr = 127, f"spawn failed: {exc}\n"
    if rc != 0 or not typed.exists() or typed.stat().st_size == 0:
        return {
            "kind": "frontend_err",
            "stage": "typed_ast",
            "rc": rc,
            "stderr": ferr[-4000:],
        }

    # Re-validate the emitted typed AST is real JSON (cwindf writes diagnostics
    # on failure in some paths).
    try:
        json.loads(typed.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {
            "kind": "frontend_err",
            "stage": "typed_ast_parse",
            "stderr": typed.read_text(encoding="utf-8", errors="replace")[-4000:]
            if typed.exists() else "<missing>",
        }

    cc = [str(find_cwindc()), "--emit-exe"]
    if opt:
        cc.append("-O3")
    cc += [str(exe), str(typed)]
    rc, out = _run_capture(cc, None, timeout_compile)
    if rc != 0 or not exe.exists():
        return {
            "kind": "compile_err",
            "stage": "compile",
            "rc": rc,
            "stderr": out[-4000:],
        }

    rc, out = _run_capture([str(exe)], str(exe.parent), timeout_run)
    if rc == 124:
        return {"kind": "hang", "stage": "run", "rc": rc, "stdout": out[-2000:]}
    if rc != 0:
        return {"kind": "crash", "stage": "run", "rc": rc, "stdout": out[-2000:]}
    return {"kind": "ok", "stage": "run", "rc": rc, "stdout": out[-2000:]}


# ---------------------------------------------------------------------------
# Campaign orchestration
# ---------------------------------------------------------------------------


def _classify_counts(cases: list[BackendCase]) -> dict:
    counts = {"ok": 0, "frontend_err": 0, "compile_err": 0, "hang": 0, "crash": 0}
    for c in cases:
        k = c.result.get("kind", "unknown")
        counts[k] = counts.get(k, 0) + 1
    return counts


def _print_backend_progress(done: int, total: int, counts: dict, t0: float) -> None:
    el = time.monotonic() - t0
    rate = done / el if el > 0 else 0.0
    summary = " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    print(
        f"[backend] {done}/{total} ({rate:.1f}/s, {el:.0f}s elapsed) {summary}",
        file=sys.stderr,
        flush=True,
    )


def run_backend_campaign(
    cases: list[BackendCase],
    out_dir: pathlib.Path,
    label: str,
    *,
    timeout_compile: float = 120.0,
    timeout_run: float = 15.0,
    opt: bool = False,
    report_every: int = 25,
    keep_failures: bool = True,
    jobs: int = 1,
) -> dict:
    """Drive every program through ``compile_and_run`` and write a report.

    Failures are persisted under ``out/<label>/backend/case_<idx>/`` so they
    can be replayed by hand; passing programs leave no artifacts (the pipeline
    is expensive, we do not want to keep thousands of exes).

    Backend stages shell out to ``cwindf`` + ``cwindc`` which are not thread
    safe under this tool, hence the sequential default (``jobs`` reserved for a
    future process pool).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    workroot = out_dir / "backend"
    workroot.mkdir(parents=True, exist_ok=True)

    t0 = time.monotonic()
    for i, case in enumerate(cases):
        cdir = workroot / f"case_{case.index:06d}"
        try:
            case.result = compile_and_run(
                case.source,
                cdir,
                timeout_compile=timeout_compile,
                timeout_run=timeout_run,
                opt=opt,
            )
        except Exception as exc:  # a harness bug, not a toolchain result
            case.result = {
                "kind": "crash",
                "stage": "harness",
                "rc": -1,
                "stdout": f"harness exception: {exc!r}",
            }
        if case.result.get("kind") != "ok":
            (cdir / "case.wind").write_text(case.source, encoding="utf-8")
        else:
            # success: keep the source (tiny) for reproducibility, drop heavy
            # intermediates + exe.
            for junk in ("case.json", "case.exe", "case.bin"):
                p = cdir / junk
                try:
                    p.unlink()
                except OSError:
                    pass

        counts = _classify_counts(cases[: i + 1])
        if report_every > 0 and ((i + 1) % report_every == 0 or i + 1 == len(cases)):
            _print_backend_progress(i + 1, len(cases), counts, t0)

    counts = _classify_counts(cases)
    failures = [
        {"index": c.index, **c.result}
        for c in cases
        if c.result.get("kind") != "ok"
    ][:50]
    report = {
        "label": label,
        "total": len(cases),
        "counts": counts,
        "el": time.monotonic() - t0,
        "opt": opt,
        "failures": failures,
    }
    (out_dir / f"{label}_backend_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return report
