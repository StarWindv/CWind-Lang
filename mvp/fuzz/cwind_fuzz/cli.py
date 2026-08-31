"""Unified CLI for the CWind fuzzer: ``cwdfuzz <subcommand> ...``.

Two subcommands:

``frontend``  the classic semantic-analyzer fuzzing (``--mode gen|mutate|corpus``),
              valid-by-construction programs; any SA error/crash is a finding.
``backend``   full-pipeline blast: compile each generated program to a native
              executable and run it, classifying ok / hang / crash /
              compile_err.  This is what guards todo-155's precise-stack-map GC
              against regressions the type-checker cannot see.
``corpus``    shorthand: run the backend over every ``.wind`` under a directory.

Output layout (``--out``, default ``mvp/fuzz/out``)::

    out/<label>/cases/NNNNN.wind|json      # frontend: stored interesting cases
    out/<label>/<label>_report.json        # frontend campaign report
    out/<label>/backend/case_NNNNNN/       # backend: per-case artifacts
    out/<label>/<label>_backend_report.json

Run ``cwdfuzz <subcommand> --help`` for the per-subcommand flags.
"""

from __future__ import annotations

import argparse
import pathlib
import random
import sys
import time
from typing import Optional

from .paths import default_out_dir, REPO_ROOT, ROOT
from .frontend import (
    Generator,
    Mutator,
    Case,
    analyze,
    run_campaign,
    default_seeds,
    print_report,
)
from .backend import GcProgramBuilder, BackendCase, run_backend_campaign


def _add_frontend_args(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--mode", choices=("gen", "mutate", "corpus"), default="gen")
    sp.add_argument("--count", type=int, default=20000)
    sp.add_argument("--seed", type=int, default=1)
    sp.add_argument("--jobs", type=int, default=0)
    sp.add_argument("--report-every", type=int, default=100)
    sp.add_argument("--dir", help="directory scanned by --mode corpus")
    sp.add_argument(
        "--seeds", nargs="*",
        help="seed files for --mode mutate (default: assets exam/user files)",
    )
    sp.add_argument("--min-mutations", type=int, default=1)
    sp.add_argument("--max-mutations", type=int, default=5)
    sp.add_argument("--out", default=str(default_out_dir()))
    sp.add_argument("--label", default="run")


class _NoCases(Exception):
    """Raised by case builders for CLI-level input errors (no usable seeds);
    ``_cmd_frontend`` turns it into exit code 2, matching the pre-subcommand
    behaviour the test-suite asserts."""


def _gen_frontend_cases(args: argparse.Namespace) -> list[Case]:
    rng = random.Random(args.seed)
    out_dir = pathlib.Path(args.out)
    if args.mode == "gen":
        cases = []
        for i in range(args.count):
            cases.append(Case(index=i, source=Generator(rng).gen(), mode="gen"))
        return cases
    if args.mode == "mutate":
        seeds = [pathlib.Path(p) for p in args.seeds] if args.seeds else default_seeds()
        if not seeds:
            raise _NoCases("[Error] no seed files found")
        seeds = [p for p in seeds if p.exists()]
        clean = []
        for p in seeds:
            src = p.read_text(encoding="utf-8")
            if analyze(src)["kind"] == "clean":
                clean.append(src)
            else:
                print(f"[warn] seed not clean under current SA, skipped: {p}",
                      file=sys.stderr)
        if not clean:
            raise _NoCases("[Error] no clean seed files found")
        mut = Mutator(rng)
        cases = []
        for i in range(args.count):
            src = rng.choice(clean)
            for _ in range(rng.randint(args.min_mutations, args.max_mutations)):
                src = mut.mutate(src)
            cases.append(Case(index=i, source=src, mode="mutate"))
        return cases
    d = pathlib.Path(args.dir or (REPO_ROOT / "assets"))
    files = sorted(d.rglob("*.wind"))
    print(f"scanning {len(files)} files under {d}", file=sys.stderr, flush=True)
    return [Case(index=i, source=p.read_text(encoding="utf-8"), mode="corpus")
            for i, p in enumerate(files)]


def _cmd_frontend(args: argparse.Namespace) -> int:
    try:
        cases = _gen_frontend_cases(args)
    except _NoCases as exc:
        print(str(exc), file=sys.stderr)
        return 2
    report = run_campaign(
        cases,
        pathlib.Path(args.out),
        args.label,
        expect_syntax_valid=args.mode != "corpus",
        jobs=args.jobs,
        report_every=args.report_every,
    )
    print_report(report)
    print(f"\noutput: {pathlib.Path(args.out)}")
    return 0


def _add_backend_args(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--count", type=int, default=200,
                    help="programs to build (backend pipeline is slow; default 200)")
    sp.add_argument("--seed", type=int, default=1)
    sp.add_argument("--report-every", type=int, default=25)
    sp.add_argument("--timeout-compile", type=float, default=120.0)
    sp.add_argument("--timeout-run", type=float, default=15.0,
                    help="run timeout; exceeding it classifies as 'hang'")
    sp.add_argument("-O", "--opt", action="store_true",
                    help="build the exe with -O3 (default -O0/-g)")
    sp.add_argument("--out", default=str(default_out_dir()))
    sp.add_argument("--label", default="blast")


def _gen_backend_cases(args: argparse.Namespace) -> list[BackendCase]:
    rng = random.Random(args.seed)
    cases = []
    for i in range(args.count):
        cases.append(BackendCase(index=i, source=GcProgramBuilder(rng).build()))
    return cases


def _cmd_backend(args: argparse.Namespace) -> int:
    cases = _gen_backend_cases(args)
    report = run_backend_campaign(
        cases,
        pathlib.Path(args.out),
        args.label,
        timeout_compile=args.timeout_compile,
        timeout_run=args.timeout_run,
        opt=args.opt,
        report_every=args.report_every,
    )
    _print_backend_report(report)
    print(f"\noutput: {pathlib.Path(args.out) / 'backend'}")
    return 1 if (report["counts"].get("hang", 0)
                 or report["counts"].get("crash", 0)
                 or report["counts"].get("compile_err", 0)) else 0


def _print_backend_report(report: dict) -> None:
    print("=== backend fuzz report ===", file=sys.stderr)
    print(f"label: {report['label']}   total: {report['total']}   "
          f"opt: {report['opt']}   elapsed: {report['el']:.1f}s", file=sys.stderr)
    for kind, n in sorted(report["counts"].items()):
        print(f"  {kind:>14}: {n}", file=sys.stderr)


def _add_corpus_backend_args(sp: argparse.ArgumentParser) -> None:
    _add_backend_args(sp)
    sp.add_argument("--dir", help="directory scanned for *.wind (default: repo example/)")


def _gen_corpus_backend_cases(args: argparse.Namespace) -> list[BackendCase]:
    d = pathlib.Path(args.dir or (REPO_ROOT / "example"))
    files = sorted(d.rglob("*.wind"))
    print(f"backend-scanning {len(files)} files under {d}", file=sys.stderr, flush=True)
    cases = [BackendCase(index=i, source=p.read_text(encoding="utf-8"))
             for i, p in enumerate(files)]
    return cases


def _cmd_corpus(args: argparse.Namespace) -> int:
    cases = _gen_corpus_backend_cases(args)
    report = run_backend_campaign(
        cases,
        pathlib.Path(args.out),
        args.label,
        timeout_compile=args.timeout_compile,
        timeout_run=args.timeout_run,
        opt=args.opt,
        report_every=args.report_every,
    )
    _print_backend_report(report)
    print(f"\noutput: {pathlib.Path(args.out) / 'backend'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="cwdfuzz",
        description="Grammar-based fuzzer for CWind: frontend (SA) and full "
                    "backend (compile + run) pipelines.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("frontend", help="fuzz the semantic analyzer")
    _add_frontend_args(f)
    f.set_defaults(func=_cmd_frontend)

    b = sub.add_parser("backend", help="compile+run generated GC-churn programs")
    _add_backend_args(b)
    b.set_defaults(func=_cmd_backend)

    c = sub.add_parser("corpus", help="backend compile+run every .wind under a dir")
    _add_corpus_backend_args(c)
    c.set_defaults(func=_cmd_corpus)
    return ap


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Back-compat: the pre-subcommand CLI took ``--mode gen|mutate|corpus`` as
    # top-level flags.  Route that legacy spelling to the ``frontend``
    # subcommand so scripts and the test-suite keep working unchanged.
    if argv and argv[0].startswith("--") and argv[0] not in (
        "-h", "--help",
    ):
        argv = ["frontend", *argv]
    ap = build_parser()
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
