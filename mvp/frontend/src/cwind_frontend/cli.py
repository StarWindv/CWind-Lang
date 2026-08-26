"""Command-line interface for the CWind frontend.

Usage::

    cwindf [file]               lex → parse → SA (default; silent on success)
    cwindf --lex [file]         lexer only, print tokens
    cwindf --parse [file]       lexer → parser, print AST
    cwindf --sa [file]          lexer → parser → SA, print SA result
    cwindf --typed-ast [file]   full pipeline, print the typed AST as JSON
    cwindf --verbose [file]     lexer → parser, print tokens and AST
    cwindf --json ...           any of the above, JSON output (typed-ast is
                                always JSON)
    cwindf -V | --version       version banner
    cwindf -V --short           just ``v{SemVer}``

Each stage runs error-recovering: the lexer/parser/SA keep going and report
every problem they find.  If a stage produced errors, the output stops there
(the next stage is not entered) and all diagnostics are rendered to stderr.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional, Sequence

from ._version import __branch__, __commit__, __version__
from .ast_components.ast import ast_dump
from .ast_components.errors import FrontendError
from .ast_components.token import Token
from .breeze import (
    MANIFEST_NAME,
    ManifestError,
    find_manifest,
    load_manifest,
    write_json,
)
from .cfg import OS_NAMES
from .lexer import Lexer, tokens_to_json
from .parser import parse_with_errors
from .render_err import render_error, render_warning
from .sa import ProgramInfo, run_sa_with_errors
from .typed_ast import build_module_artifacts, build_typed_ast, module_artifact_relpath

VERSION_BANNER = (
    "CWind Programming Language Compiler Frontend\n"
    f"Version: v{__version__}({__branch__}/{__commit__})\n"
    "Copyright (c) 2026 StarWindv\n"
    "SPDX-License-Identifier: BSD-3-Clause"
)


def _print_tokens(tokens: list[Token], as_json: bool) -> None:
    if as_json:
        print(tokens_to_json(tokens))
    else:
        for tok in tokens:
            print(f"{tok.line:>5}:{tok.column:<4} {tok.kind.value:<12} {tok.raw!r}")


def _print_ast(program, as_json: bool) -> None:
    if as_json:
        print(json.dumps(program.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(ast_dump(program))


def _print_sa(info: ProgramInfo, as_json: bool) -> None:
    if as_json:
        print(json.dumps(info.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(f"Semantic analysis passed: {len(info.symbols)} top-level symbols")
        for sym in info.symbols.values():
            print(f"  {sym.kind:<6} {sym.name}")


def _render_error(exc, source_text: str, source_name: Optional[str], color: bool) -> None:
    print(
        render_error(exc, source_text, source_name=source_name, color=color),
        file=sys.stderr,
    )


def _emit_errors(
    errors: Sequence[FrontendError],
    source_text: str,
    source_name: Optional[str],
    color: bool,
    stage: str,
) -> None:
    """Render every error plus a closing summary line.

    bug-36: errors raised inside imported modules carry their own
    ``source`` path; render each against that file's text so the
    position points at the real location, not at unrelated text of
    the entry file."""
    cache: dict[str, tuple[str, str]] = {}
    for exc in errors:
        own = getattr(exc, "source", None)
        if own:
            entry = cache.get(own)
            if entry is None:
                try:
                    with open(own, "r", encoding="utf-8") as fh:
                        entry = (fh.read().lstrip("\ufeff"), _display_path(own))
                except OSError:
                    entry = (source_text, source_name or "<stdin>")
                cache[own] = entry
            text, display = entry
        else:
            text, display = source_text, source_name
        _render_error(exc, text, display, color)
    display = source_name if source_name is not None else "<stdin>"
    print(
        f"[Error] Could not compile `{display}` due to {len(errors)} previous errors "
        f"(in {stage})",
        file=sys.stderr,
    )


def _lex_path(path) -> tuple[str, Lexer, list[Token]]:
    """Read *path* through the streaming lexer; returns text + lexer + tokens."""
    lexer = Lexer()
    source_text = ""
    tokens: list[Token] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            source_text += line
            tokens.extend(lexer.feed_line(line))
        tokens.extend(lexer.eof())
    return source_text.lstrip("\ufeff"), lexer, tokens


def _display_path(path) -> str:
    try:
        return os.path.relpath(path)
    except ValueError:  # different drive than the working directory
        return str(path)


def _run_project_mode(project_arg: str, *, color: bool, target_os: Optional[str]) -> int:
    """todo-97: compile a whole project anchored at its Breeze.toml.

    Locates the manifest from ``project_arg`` (a directory or the manifest
    file itself), compiles the package entry through the regular pipeline
    (imports flatten as in single-file mode) and writes the build outputs
    under ``<project>/target``:

    - ``<name>.typed.json``: whole-program typed AST, directly consumable
      by ``cwindc --check/--emit-*``;
    - ``project.json``: build index (package metadata, entry, every
      resolved import with its source file, and the todo-98 artifact map);
    - one annotated JSON per source file under ``target/``, mirroring the
      source tree (todo-98): ``target/<relative/path>.wind.json``.
    """
    start = Path(project_arg)
    if not start.exists():
        print(f"[Error] ProjectNotFound: {project_arg}", file=sys.stderr)
        return 2
    manifest_path = find_manifest(start)
    if manifest_path is None:
        print(
            f"[Error] no {MANIFEST_NAME} found from "
            f"{start.resolve()} upward",
            file=sys.stderr,
        )
        return 2
    try:
        manifest = load_manifest(manifest_path)
    except ManifestError as exc:
        print(f"[Error] invalid {MANIFEST_NAME}: {exc}", file=sys.stderr)
        return 1

    root = manifest.root
    source_dir = manifest.source_path()
    candidates = manifest.entry_candidates()
    entry = next((path for path in candidates if path.is_file()), None)
    if entry is None:
        tried = " or ".join(str(manifest.entry.source + "/" + c.name)
                            for c in candidates)
        print(
            f"[Error] entry point of project '{manifest.name}' not found "
            f"(tried {tried})",
            file=sys.stderr,
        )
        return 1

    # todo-97: the package's own library facade (lib.wd) is wildcard-
    # imported into the entry program when it exists.
    package_lib = None
    if not manifest.entry.is_lib and manifest.lib_path().is_file():
        package_lib = ([manifest.name], str(manifest.lib_path()))

    display_entry = _display_path(entry)
    try:
        source_text, lexer, tokens = _lex_path(entry)
    except OSError as exc:
        print(f"[Error] cannot read entry: {exc}", file=sys.stderr)
        return 1

    for w in lexer.warnings:
        print(
            render_warning(
                FrontendError(w.message, w.line, w.column),
                source_text,
                source_name=display_entry,
                color=color,
            ),
            file=sys.stderr,
        )
    if lexer.errors:
        _emit_errors(lexer.errors, source_text, display_entry, color, "Lex")
        return 1

    presult = parse_with_errors(
        tokens,
        source_path=str(entry.resolve()),
        target_os=target_os,
        package_lib=package_lib,
    )
    if presult.errors:
        _emit_errors(presult.errors, source_text, display_entry, color, "Parse")
        return 1

    sresult = run_sa_with_errors(presult.program)
    for w in sresult.warnings:
        print(
            render_warning(
                w,
                source_text,
                source_name=display_entry,
                color=color,
            ),
            file=sys.stderr,
        )
    if sresult.errors:
        _emit_errors(sresult.errors, source_text, display_entry, color, "SA")
        return 1

    doc = build_typed_ast(presult.program, sresult.info, source=str(entry.resolve()))

    typed_path = root / "target" / f"{manifest.name}.typed.json"
    write_json(typed_path, doc)

    # todo-98: one semantically annotated JSON per source file, mirroring
    # the project's source tree under target/.
    entry_resolved = str(entry.resolve())
    artifacts: dict[str, str] = {}
    for artifact in build_module_artifacts(
        presult.program, sresult.info, entry_source=entry_resolved
    ):
        rel = module_artifact_relpath(artifact["source"], root)
        write_json(root / "target" / Path(*rel.split("/")), artifact)
        artifacts[rel[:-len(".json")]] = rel

    modules = [
        {
            "path": list(entry_info.get("path") or []),
            "source": entry_info.get("source"),
            "item": entry_info.get("item"),
            "wildcard": bool(entry_info.get("wildcard")),
            "auto": bool(entry_info.get("auto")),
        }
        for entry_info in doc["imports"]
    ]
    project_doc = {
        "format": "cwind-project",
        "version": 1,
        "package": {
            "name": manifest.name,
            "version": manifest.version,
            "identifier": manifest.identifier,
            "id_version": manifest.id_version,
            "description": manifest.description,
            "authors": list(manifest.authors),
            "homepage": manifest.homepage,
        },
        "entry": {
            "source": manifest.entry.source,
            "is_lib": manifest.entry.is_lib,
            "module": manifest.entry.module,
        },
        "root": str(root.resolve()),
        "entry_file": str(entry.resolve()),
        "target": typed_path.name,
        "modules": modules,
        # todo-98: module source (POSIX-relative) -> artifact path under
        # target/; the whole-program JSON stays the backend input.
        "artifacts": artifacts,
        "dependencies": {
            name: {
                "version": dep.version,
                "identifier": dep.identifier,
            }
            for name, dep in manifest.dependencies.items()
        },
    }
    write_json(root / "target" / "project.json", project_doc)

    print(
        f"[Project] {manifest.name} v{manifest.version}: "
        f"{len(modules)} import(s), {len(artifacts)} module artifact(s), "
        f"entry {display_entry} -> {_display_path(typed_path)}"
    )
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    # Windows 重定向输出时强制 UTF-8, 避免 JSON 里出现 GBK 字节
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        prog="cwindf",
        description="CWind compiler frontend (lexer → parser → semantic analysis).",
    )
    parser.add_argument("file", nargs="?", help="source file (default: stdin)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--lex", action="store_true", help="lexer only; print tokens")
    mode.add_argument("--parse", action="store_true", help="lexer + parser; print AST")
    mode.add_argument(
        "--sa", action="store_true", help="lexer + parser + SA; print SA result"
    )
    mode.add_argument(
        "--typed-ast",
        action="store_true",
        help="lexer + parser + SA; print the typed AST as JSON",
    )
    mode.add_argument(
        "--verbose",
        action="store_true",
        help="lexer + parser; print tokens and AST",
    )
    # todo-97: whole-project compilation anchored at Breeze.toml.
    mode.add_argument(
        "--project",
        nargs="?",
        const=".",
        default=None,
        metavar="DIR",
        help="compile a whole project: locate Breeze.toml from DIR "
        "(default: the working directory, walking upward), then compile "
        "its entry into <project>/target/<name>.typed.json",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit stage output as JSON "
        "(with --lex/--parse/--sa/--typed-ast/--verbose)",
    )
    parser.add_argument(
        "--no-color", action="store_true", help="render errors without ANSI colors"
    )
    parser.add_argument(
        "--target-os",
        choices=list(OS_NAMES),
        default=None,
        help="compile-time target for #[cfg] predicates (default: auto-detect the host)",
    )
    parser.add_argument("-V", "--version", action="store_true", help="print version info")
    parser.add_argument("--short", action="store_true", help="with --version, print v{SemVer}")
    args = parser.parse_args(argv)

    if args.version:
        print(f"v{__version__}" if args.short else VERSION_BANNER)
        return 0
    if args.short:
        print("[Error] --short requires --version", file=sys.stderr)
        return 2
    if args.json and not (
        args.lex or args.parse or args.sa or args.verbose or args.typed_ast
    ):
        print(
            "[Error] --json requires one of "
            "--lex/--parse/--sa/--typed-ast/--verbose",
            file=sys.stderr,
        )
        return 2

    # todo-97: project mode replaces the per-stage pipeline entirely.
    if args.project is not None:
        if args.file:
            print(
                "[Error] --project cannot be combined with a file argument",
                file=sys.stderr,
            )
            return 2
        return _run_project_mode(
            args.project, color=not args.no_color, target_os=args.target_os
        )

    lexer = Lexer()
    source_text = ""
    tokens: list[Token] = []
    if args.file and os.path.exists(args.file):
        source_text, lexer, tokens = _lex_path(args.file)
    elif args.file is not None and not os.path.exists(args.file):
        print(f"[Error] FileNotFound: {args.file}")
        return 2
    else:
        for line in sys.stdin:
            source_text += line
            tokens.extend(lexer.feed_line(line))
        tokens.extend(lexer.eof())
    display_path = None
    if args.file:
        display_path = _display_path(args.file)

    for w in lexer.warnings:
        print(
            render_warning(
                FrontendError(w.message, w.line, w.column),
                source_text,
                source_name=display_path,
                color=not args.no_color,
            ),
            file=sys.stderr,
        )

    if lexer.errors:
        _emit_errors(lexer.errors, source_text, display_path, not args.no_color, "Lex")
        return 1

    if args.lex:
        _print_tokens(tokens, args.json)
        return 0

    presult = parse_with_errors(
        tokens,
        # todo-76: the entry file path anchors the project root and enables
        # the implicit std::prelude::* import.
        source_path=os.path.abspath(args.file) if args.file else None,
        # todo-86/93: pin the #[cfg] target (default: auto-detect the host).
        target_os=args.target_os,
    )
    if presult.errors:
        _emit_errors(presult.errors, source_text, display_path, not args.no_color, "Parse")
        return 1
    program = presult.program

    if args.parse:
        _print_ast(program, args.json)
        return 0

    if args.verbose:
        if args.json:
            print(json.dumps(
                {"tokens": [tok.to_dict() for tok in tokens], "ast": program.to_dict()},
                indent=2,
                ensure_ascii=False,
            ))
        else:
            print("[Lexer Output]")
            _print_tokens(tokens, False)
            print("\n[Parser AST]")
            _print_ast(program, False)
        return 0

    sresult = run_sa_with_errors(program)
    for w in sresult.warnings:
        print(
            render_warning(
                w,
                source_text,
                source_name=display_path,
                color=not args.no_color,
            ),
            file=sys.stderr,
        )
    if sresult.errors:
        _emit_errors(sresult.errors, source_text, display_path, not args.no_color, "SA")
        return 1

    if args.sa:
        _print_sa(sresult.info, args.json)
    if args.typed_ast:
        # todo-63: 记录源文件绝对路径, 后端据此解析
        # #[link(path = "...", relative = "source")]
        source = os.path.abspath(args.file) if args.file else None
        print(
            json.dumps(
                build_typed_ast(program, sresult.info, source=source),
                indent=2,
                ensure_ascii=False,
            )
        )
    # default mode: full pipeline, silent on success
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
