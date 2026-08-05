"""Command-line interface for the CWind frontend.

Usage::

    cwindf [file]               lex → parse → SA (default; silent on success)
    cwindf --lex [file]         lexer only, print tokens
    cwindf --parse [file]       lexer → parser, print AST
    cwindf --sa [file]          lexer → parser → SA, print SA result
    cwindf --verbose [file]     lexer → parser, print tokens and AST
    cwindf --json ...           any of the above, JSON output
    cwindf -V | --version       version banner
    cwindf -V --short           just ``v{SemVer}``
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional, TextIO

from ._version import __branch__, __commit__, __version__
from .ast_components.ast import ast_dump
from .ast_components.token import Token
from .lexer import LexError, Lexer, tokens_to_json
from .parser import ParseError, parse
from .render_err import render_error
from .sa import ProgramInfo, SaError, run_sa

VERSION_BANNER = (
    "CWind Programming Language Compiler Frontend\n"
    f"Version: v{__version__}({__branch__}/{__commit__})\n"
    "Copyright (c) 2026 Wind-Project\n"
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


def main(argv: Optional[list[str]] = None) -> int:
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
        "--verbose",
        action="store_true",
        help="lexer + parser; print tokens and AST",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit stage output as JSON (with --lex/--parse/--sa/--verbose)",
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
        print("error: --short requires --version", file=sys.stderr)
        return 2
    if args.json and not (args.lex or args.parse or args.sa or args.verbose):
        print(
            "error: --json requires one of --lex/--parse/--sa/--verbose",
            file=sys.stderr,
        )
        return 2

    lexer = Lexer()
    source_text = ""
    try:
        tokens: list[Token] = []
        fh: TextIO
        if args.file:
            fh = open(args.file, "r", encoding="utf-8")
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
    except LexError as exc:
        _render_error(exc, source_text.lstrip("\ufeff"), args.file, not args.no_color)
        return 1

    source_text = source_text.lstrip("\ufeff")

    if args.lex:
        _print_tokens(tokens, args.json)
        return 0

    try:
        program = parse(tokens)
    except ParseError as exc:
        _render_error(exc, source_text, args.file, not args.no_color)
        return 1

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
            print("=== lexer output ===")
            _print_tokens(tokens, False)
            print("=== parser AST ===")
            _print_ast(program, False)
        return 0

    try:
        info = run_sa(program)
    except SaError as exc:
        _render_error(exc, source_text, args.file, not args.no_color)
        return 1

    if args.sa:
        _print_sa(info, args.json)
    # default mode: full pipeline, silent on success
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
