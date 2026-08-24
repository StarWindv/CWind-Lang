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
from typing import Optional, Sequence, TextIO

from ._version import __branch__, __commit__, __version__
from .ast_components.ast import ast_dump
from .ast_components.errors import FrontendError
from .ast_components.token import Token
from .lexer import Lexer, tokens_to_json
from .parser import parse_with_errors
from .render_err import render_error, render_warning
from .sa import ProgramInfo, run_sa_with_errors
from .typed_ast import build_typed_ast

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
    """Render every error plus a closing summary line."""
    for exc in errors:
        _render_error(exc, source_text, source_name, color)
    display = source_name if source_name is not None else "<stdin>"
    print(
        f"[Error] Could not compile `{display}` due to {len(errors)} previous errors "
        f"(in {stage})",
        file=sys.stderr,
    )


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
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit stage output as JSON "
        "(with --lex/--parse/--sa/--typed-ast/--verbose)",
    )
    parser.add_argument(
        "--no-color", action="store_true", help="render errors without ANSI colors"
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

    lexer = Lexer()
    source_text = ""
    tokens: list[Token] = []
    fh: TextIO
    if args.file and os.path.exists(args.file):
        fh = open(args.file, "r", encoding="utf-8")
    elif args.file is not None and not os.path.exists(args.file):
        print(f"[Error] FileNotFound: {args.file}")
        return 2
    else:
        fh = sys.stdin
    try:
        for line in fh:
            source_text += line
            tokens.extend(lexer.feed_line(line))
        tokens.extend(lexer.eof())
    finally:
        if args.file:
            fh.close()
    source_text = source_text.lstrip("\ufeff")
    display_path = None
    if args.file:
        try:
            display_path = os.path.relpath(args.file)
        except ValueError:  # different drive than the working directory
            display_path = args.file

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

    presult = parse_with_errors(tokens)
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
