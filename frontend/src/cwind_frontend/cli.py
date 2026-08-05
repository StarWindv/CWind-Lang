"""Command-line interface for the CWind frontend."""

from __future__ import annotations

import argparse
import sys
from typing import Optional, TextIO

from .lexer import LexError, Lexer, tokens_to_json
from .render_err import render_error


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Tokenize CWind source (spec: frontend/Grammar.md)."
    )
    parser.add_argument("file", nargs="?", help="source file (default: stdin)")
    parser.add_argument("--comments", action="store_true", help="emit comment tokens")
    parser.add_argument("--json", action="store_true", help="emit tokens as JSON")
    parser.add_argument(
        "--no-color", action="store_true", help="render errors without ANSI colors"
    )
    args = parser.parse_args(argv)

    lexer = Lexer(emit_comments=args.comments)
    source_text = ""
    try:
        tokens = []
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
        source_text = source_text.lstrip("\ufeff")
        print(
            render_error(
                exc,
                source_text,
                source_name=args.file,
                color=not args.no_color,
            ),
            file=sys.stderr,
        )
        return 1

    if args.json:
        print(tokens_to_json(tokens))
    else:
        for tok in tokens:
            print(f"{tok.line:>5}:{tok.column:<4} {tok.kind.value:<12} {tok.raw!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
