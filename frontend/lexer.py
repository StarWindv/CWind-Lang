"""CWind lexer — tokenizes CWind source (spec: ``frontend/Grammar.md``).

Design notes
------------
* Line-oriented streaming.  Feed the lexer one physical line at a time
  (``Lexer.feed_line``) instead of handing it the whole file; only the small
  amount of state that must survive a line boundary is kept (block comments,
  and strings continued through a backslash-newline escape).  ``tokenize``
  and ``tokenize_file`` are thin convenience wrappers.
* Not a parser.  Grammar-level rules are deliberately left to the parser:
  statement terminators, balanced delimiters, generic-vs-comparison
  disambiguation, identifiers starting with a digit, ``$``/``#`` being
  reserved, ``!<``/``!>`` rewriting, etc.  Lexical-level problems
  (unterminated string/block comment, characters outside the language) raise
  :class:`LexError`.
* Parser integration.  Collect tokens eagerly with ``tokenize`` /
  ``tokenize_file`` and walk them with an index cursor, or consume them
  lazily via :func:`stream_tokens`.  Tokens carry enough position info for
  parser diagnostics; there is no EOF token — the parser treats the end of
  the token list as EOF.  Context-ambiguous tokens (``{``/``}``, ``<``/``>``,
  ``:``, ``!<``/``!>``) are emitted as-is for the parser to disambiguate.
  Keywords have their own dedicated kinds (``TokenKind.LET``,
  ``TokenKind.STRUCT``, ...); ``KEYWORD_KINDS`` collects them all.
* Tokens can be dumped as JSON (``Token.to_dict`` / ``tokens_to_json``, or
  the CLI's ``--json`` flag) for debugging and tooling.
* Lexical errors can be rendered as colored diagnostics with ariadne_py-py via
  ``frontend/errors.py`` (``render_error``); the CLI does this automatically.

Spec notes (Grammar.md is authoritative; WSR:0 and ExpansionAndCorrection.md
are only consulted where Grammar.md is silent):
  * ``//`` is a line comment and ``/* ... */`` a block comment; the old
    backtick comment and ``//`` integer division are gone.
  * ``++`` / ``--`` are not operators.
  * Type names (``Int``, ``UInt8``, ``String``, ...) and ``Self``/``self``
    are ordinary identifiers, not keywords.
  * ``$`` and ``#`` are reserved symbols; they still lex to their own tokens
    so the parser can reject them with a proper message.
  * Strings are ``"..."`` or ``'...'``.  The escape table is
    ``\\n \\r \\t \\\\ \\' \\" \\0 \\b \\v`` plus ``\\{``/``\\}`` for literal
    braces inside format interpolation; unknown escapes are kept verbatim.
    A backslash immediately before a newline is an escaped newline: the string
    continues on the next line and the indentation after it is counted into
    the string's value.  A raw newline inside a string is not allowed.
  * Numbers are decimal integers and floats (``123``, ``3.14``).  ``1.`` is
    NOT a float — the dot after a number is the method-call dot; there are no
    hex/octal/binary or exponent literals yet.
"""

from __future__ import annotations

import os
import sys
import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Union

__all__ = [
    "TokenKind",
    "Token",
    "LexError",
    "Lexer",
    "KEYWORD_KINDS",
    "stream_tokens",
    "tokenize",
    "tokenize_file",
    "tokens_to_json",
]


class TokenKind(str, Enum):
    """Kinds of tokens produced by the CWind lexer.

    Operator members use their exact source spelling as the value, so
    ``TokenKind("==")`` is ``TokenKind.EQ``.
    """

    # Names and literals.
    IDENTIFIER = "identifier"
    INTEGER = "integer"
    FLOAT = "float"
    STRING = "string"
    COMMENT = "comment"

    # Keywords (Grammar.md §1.3).  Each keyword is its own kind so that both
    # the parser and the JSON output can name it directly (e.g. LET, STRUCT).
    STRUCT = "STRUCT"
    ENUM = "ENUM"
    EXTRA = "EXTRA"
    IMPL = "IMPL"
    TRAIT = "TRAIT"
    CONST = "CONST"
    EXPLAIN = "EXPLAIN"
    STATIC = "STATIC"
    WHICH = "WHICH"
    WHERE = "WHERE"
    TYPE = "TYPE"
    GROUP = "GROUP"
    IN = "IN"
    LET = "LET"
    FN = "FN"
    PUB = "PUB"
    RETURN = "RETURN"
    FOR = "FOR"
    WHILE = "WHILE"
    IF = "IF"
    ELIF = "ELIF"
    ELSE = "ELSE"

    # Reserved keywords (Grammar.md §1.3).
    LAMBDA = "LAMBDA"
    FROM = "FROM"
    IMPORT = "IMPORT"
    USE = "USE"
    AS = "AS"
    WHEN = "WHEN"
    DEFINE = "DEFINE"
    ASYNC = "ASYNC"
    AWAIT = "AWAIT"
    PROTECT = "PROTECT"

    # Punctuation and operators (Grammar.md §1.2.1).
    DOT = "."
    GT = ">"
    LT = "<"
    LE = "<="
    GE = ">="
    NE = "!="
    NOT_LT = "!<"          # sugar for >=
    NOT_GT = "!>"          # sugar for <=
    BACKSLASH = "\\"
    EQ = "=="              # value equality
    ADDR_EQ = "==="        # address equality
    ASSIGN = "="
    PLUS_ASSIGN = "+="
    MINUS_ASSIGN = "-="
    STAR_ASSIGN = "*="
    SLASH_ASSIGN = "/="
    ABS_LT = "<:"          # always-copy assignment to the left
    ABS_GT = ":>"          # always-copy assignment to the right
    ARROW = "->"
    NOT = "!"
    AND = "&&"
    OR = "||"
    MINUS = "-"
    SLASH = "/"
    PERCENT = "%"
    STAR = "*"
    PLUS = "+"
    SHL = "<<"
    SHR = ">>"
    AMP = "&"
    PIPE = "|"
    CARET = "^"
    SEMICOLON = ";"
    COLON = ":"
    PATH = "::"
    UNPACK = ".."
    LPAREN = "("
    RPAREN = ")"
    LBRACKET = "["
    RBRACKET = "]"
    LBRACE = "{"
    RBRACE = "}"
    AT = "@"
    COMMA = ","
    DOLLAR = "$"           # reserved symbol
    HASH = "#"             # reserved symbol


# Grammar.md §1.3 — hard keywords plus the reserved-word list.
KEYWORDS: frozenset[str] = frozenset({
    "struct", "enum", "extra", "impl", "trait", "const", "explain",
    "static", "which", "where", "type", "group", "in", "let", "fn",
    "pub", "return", "for", "while", "if", "elif", "else",
})

RESERVED_KEYWORDS: frozenset[str] = frozenset({
    "lambda", "from", "import", "use", "as", "when",
    "define", "async", "await", "protect",
})

# keyword text -> its dedicated TokenKind (member name is the upper-cased word)
_KEYWORD_KINDS: dict[str, TokenKind] = {
    word: TokenKind[word.upper()] for word in KEYWORDS | RESERVED_KEYWORDS
}

# Convenience set for parsers: tok.kind in KEYWORD_KINDS
KEYWORD_KINDS: frozenset[TokenKind] = frozenset(_KEYWORD_KINDS.values())

# Longest-first so `===` wins over `==` over `=`, `::` over `:`, etc.
_MULTI_CHAR_OPERATORS: tuple[str, ...] = (
    "===",
    "!=", "!<", "!>", "<=", ">=", "<<", ">>", "&&", "||",
    "+=", "-=", "*=", "/=", "==", "::", "..", "->", "<:", ":>",
)

_SINGLE_CHAR_TOKENS: dict[str, TokenKind] = {
    ".": TokenKind.DOT,
    ">": TokenKind.GT,
    "<": TokenKind.LT,
    "\\": TokenKind.BACKSLASH,
    "=": TokenKind.ASSIGN,
    "!": TokenKind.NOT,
    "-": TokenKind.MINUS,
    "/": TokenKind.SLASH,
    "%": TokenKind.PERCENT,
    "*": TokenKind.STAR,
    "+": TokenKind.PLUS,
    "&": TokenKind.AMP,
    "|": TokenKind.PIPE,
    "^": TokenKind.CARET,
    ";": TokenKind.SEMICOLON,
    ":": TokenKind.COLON,
    "(": TokenKind.LPAREN,
    ")": TokenKind.RPAREN,
    "[": TokenKind.LBRACKET,
    "]": TokenKind.RBRACKET,
    "{": TokenKind.LBRACE,
    "}": TokenKind.RBRACE,
    "@": TokenKind.AT,
    ",": TokenKind.COMMA,
    "$": TokenKind.DOLLAR,
    "#": TokenKind.HASH,
}

# Grammar.md §1.2.1 escape table, plus \{ \} for literal braces inside
# format-interpolated strings.  Unknown escapes stay verbatim.
_ESCAPES: dict[str, str] = {
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "\\": "\\",
    "'": "'",
    '"': '"',
    "0": "\0",
    "b": "\b",
    "v": "\v",
    "{": "{",
    "}": "}",
}


@dataclass(frozen=True, slots=True)
class Token:
    """A single lexical token.

    Positions are 1-based.  ``end_column`` is exclusive (Python-slice style):
    a one-character token at column 5 spans [5, 6).  Multi-line tokens
    (strings, block comments) carry the starting position in ``line``/``column``
    and the ending position in ``end_line``/``end_column``.

    ``value`` is the decoded value (numbers as int/float, strings unescaped),
    ``raw`` the exact source text of the token.
    """

    kind: TokenKind
    value: object
    line: int
    column: int
    end_line: int
    end_column: int
    raw: str

    def to_dict(self) -> dict:
        """Serialize the token to a JSON-friendly dict."""
        return {
            "kind": self.kind.value,
            "value": self.value,
            "line": self.line,
            "column": self.column,
            "end_line": self.end_line,
            "end_column": self.end_column,
            "raw": self.raw,
        }


class LexError(Exception):
    """Raised for lexical-level problems (not grammar-level syntax).

    Positions are 1-based; ``end_column`` is exclusive (Python-slice style),
    matching :class:`Token`.  When the end is unknown it defaults to the
    single character at ``(line, column)``.
    """

    def __init__(
        self,
        message: str,
        line: int,
        column: int,
        *,
        end_line: Optional[int] = None,
        end_column: Optional[int] = None,
    ) -> None:
        self.message = message
        self.line = line
        self.column = column
        self.end_line = line if end_line is None else end_line
        self.end_column = column + 1 if end_column is None else end_column
        super().__init__(f"{message} (line {line}, column {column})")


@dataclass
class _StringState:
    quote: str
    start_line: int
    start_col: int
    chars: list[str] = field(default_factory=list)
    raw_chars: list[str] = field(default_factory=list)
    continued: bool = False  # the line ended with a backslash-newline escape


class Lexer:
    """Streaming, line-oriented CWind lexer.

    Usage::

        lexer = Lexer()
        for line in file:                 # any iterable of physical lines
            for token in lexer.feed_line(line):
                ...
        lexer.eof()                       # flush; raises on dangling state

    ``feed_line`` accepts a line with or without its trailing newline
    (``\\n``, ``\\r\\n`` or ``\\r`` are stripped); a raw newline inside a
    string is a lexical error.
    """

    def __init__(self, *, emit_comments: bool = False) -> None:
        self.emit_comments = emit_comments
        self.line_no = 0
        self._in_block_comment = False
        self._block_comment_start: Optional[tuple[int, int]] = None
        self._block_body_parts: list[str] = []
        self._string: Optional[_StringState] = None

    def feed_line(self, line: str) -> list[Token]:
        """Lex one physical line; returns the tokens completed on it."""
        self.line_no += 1
        line = _strip_eol(line)
        if self.line_no == 1 and line.startswith("\ufeff"):
            line = line[1:]
        self._last_line_len = len(line)

        tokens: list[Token] = []
        i, n = 0, len(line)

        if self._in_block_comment:
            i = self._consume_block_comment(line, 0, tokens)

        while i < n:
            ch = line[i]

            if self._string is not None:
                i = self._consume_string_rest(line, i, tokens)
                continue

            if ch.isspace():
                i += 1
                continue

            if line.startswith("//", i):
                if self.emit_comments:
                    raw = line[i:]
                    tokens.append(self._make(TokenKind.COMMENT, raw[2:], i + 1, raw=raw))
                break

            if line.startswith("/*", i):
                self._block_comment_start = (self.line_no, i + 1)
                self._block_body_parts = []
                i = self._consume_block_comment(line, i + 2, tokens)
                continue

            if ch in "\"'":
                st = _StringState(ch, self.line_no, i + 1)
                st.raw_chars.append(ch)
                self._string = st
                i = self._consume_string_rest(line, i + 1, tokens)
                continue

            if _is_digit(ch):
                i = self._scan_number(line, i, tokens)
                continue

            if _is_ident_start(ch):
                i = self._scan_identifier(line, i, tokens)
                continue

            i = self._scan_operator(line, i, tokens)

        if self._string is not None and not self._string.continued:
            st = self._string
            raise LexError(
                "unterminated string literal",
                st.start_line,
                st.start_col,
                end_line=self.line_no,
                end_column=len(line) + 1,
            )

        return tokens

    def eof(self) -> list[Token]:
        """Flush the lexer; raises LexError if input ended inside a string or
        a block comment."""
        if self._in_block_comment:
            line, col = self._block_comment_start or (self.line_no, 1)
            raise LexError(
                "unterminated block comment",
                line,
                col,
                end_line=self.line_no,
                end_column=getattr(self, "_last_line_len", 0) + 1,
            )
        if self._string is not None:
            st = self._string
            raise LexError(
                "unterminated string literal",
                st.start_line,
                st.start_col,
                end_line=self.line_no,
                end_column=getattr(self, "_last_line_len", 0) + 1,
            )
        return []

    # -- internals -------------------------------------------------------

    def _consume_string_rest(self, line: str, i: int, tokens: list[Token]) -> int:
        st = self._string
        assert st is not None
        st.continued = False
        n = len(line)
        while i < n:
            ch = line[i]
            if ch == st.quote:
                raw = "".join(st.raw_chars) + st.quote
                tokens.append(Token(
                    TokenKind.STRING,
                    "".join(st.chars),
                    st.start_line,
                    st.start_col,
                    self.line_no,
                    i + 2,  # exclusive: the closing quote is column i+1
                    raw,
                ))
                self._string = None
                return i + 1
            if ch == "\\":
                if i + 1 >= n:
                    # Backslash at the end of the line: escaped newline; the
                    # string continues on the next line (indentation counts).
                    st.raw_chars.append("\\\n")
                    st.continued = True
                    return n
                nxt = line[i + 1]
                if nxt == "\n":
                    # Caller passed embedded line endings; treat as continuation.
                    st.raw_chars.append("\\\n")
                    if i + 2 >= n:
                        st.continued = True
                    i += 2
                    continue
                if nxt == "\r" and i + 2 < n and line[i + 2] == "\n":
                    st.raw_chars.append("\\\r\n")
                    if i + 3 >= n:
                        st.continued = True
                    i += 3
                    continue
                if nxt in _ESCAPES:
                    st.chars.append(_ESCAPES[nxt])
                    st.raw_chars.append("\\" + nxt)
                    i += 2
                    continue
                # Unknown escape: keep the backslash and the character.
                st.chars.append("\\")
                st.chars.append(nxt)
                st.raw_chars.append("\\")
                st.raw_chars.append(nxt)
                i += 2
                continue
            if ch == "\n":
                raise LexError("unterminated string literal", st.start_line, st.start_col)
            st.chars.append(ch)
            st.raw_chars.append(ch)
            i += 1
        return i

    def _consume_block_comment(self, line: str, i: int, tokens: list[Token]) -> int:
        end = line.find("*/", i)
        if end == -1:
            if self.emit_comments:
                self._block_body_parts.append(line[i:])
            self._in_block_comment = True
            return len(line)
        self._in_block_comment = False
        if self.emit_comments:
            self._block_body_parts.append(line[i:end])
            body = "\n".join(self._block_body_parts)
            start_line, start_col = self._block_comment_start or (self.line_no, i + 1)
            tokens.append(Token(
                TokenKind.COMMENT,
                body,
                start_line,
                start_col,
                self.line_no,
                end + 3,  # exclusive: `*/` occupies columns end+1..end+2
                "/*" + body + "*/",
            ))
        return end + 2

    def _scan_number(self, line: str, i: int, tokens: list[Token]) -> int:
        n = len(line)
        start = i
        while i < n and _is_digit(line[i]):
            i += 1
        if i < n and line[i] == "." and i + 1 < n and _is_digit(line[i + 1]):
            i += 1
            while i < n and _is_digit(line[i]):
                i += 1
            kind, value = TokenKind.FLOAT, float(line[start:i])
        else:
            kind, value = TokenKind.INTEGER, int(line[start:i])
        raw = line[start:i]
        tokens.append(self._make(kind, value, start + 1, raw=raw))
        return i

    def _scan_identifier(self, line: str, i: int, tokens: list[Token]) -> int:
        n = len(line)
        start = i
        while i < n and _is_ident_part(line[i]):
            i += 1
        text = line[start:i]
        kind = _KEYWORD_KINDS.get(text, TokenKind.IDENTIFIER)
        tokens.append(self._make(kind, text, start + 1))
        return i

    def _scan_operator(self, line: str, i: int, tokens: list[Token]) -> int:
        n = len(line)
        for op in _MULTI_CHAR_OPERATORS:
            if line.startswith(op, i):
                tokens.append(self._make(TokenKind(op), op, i + 1))
                return i + len(op)
        ch = line[i]
        kind = _SINGLE_CHAR_TOKENS.get(ch)
        if kind is None:
            raise LexError(
                f"unexpected character {ch!r}",
                self.line_no,
                i + 1,
                end_column=i + 2,
            )
        tokens.append(self._make(kind, ch, i + 1))
        return i + 1

    def _make(
        self,
        kind: TokenKind,
        value: object,
        column: int,
        *,
        raw: Optional[str] = None,
    ) -> Token:
        raw = raw if raw is not None else str(value)
        return Token(kind, value, self.line_no, column, self.line_no, column + len(raw), raw)


def _strip_eol(line: str) -> str:
    if line.endswith("\n"):
        line = line[:-1]
    if line.endswith("\r"):
        line = line[:-1]
    return line


def _is_digit(ch: str) -> bool:
    return "0" <= ch <= "9"


def _is_ident_start(ch: str) -> bool:
    return ch == "_" or ch.isalpha()


def _is_ident_part(ch: str) -> bool:
    return ch == "_" or ch.isalnum()


def tokenize(source: str, *, emit_comments: bool = False) -> list[Token]:
    """Tokenize a whole CWind source string (convenience wrapper)."""
    lexer = Lexer(emit_comments=emit_comments)
    tokens: list[Token] = []
    for line in source.splitlines():
        tokens.extend(lexer.feed_line(line))
    tokens.extend(lexer.eof())
    return tokens


def tokenize_file(
    path: Union[str, os.PathLike[str]],
    *,
    emit_comments: bool = False,
) -> list[Token]:
    """Tokenize a file, reading it line by line instead of into memory."""
    lexer = Lexer(emit_comments=emit_comments)
    tokens: list[Token] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            tokens.extend(lexer.feed_line(line))
    tokens.extend(lexer.eof())
    return tokens


def stream_tokens(
    lines: Iterable[str],
    *,
    emit_comments: bool = False,
) -> Iterator[Token]:
    """Lazily lex an iterable of physical lines.

    Handy when a parser wants to avoid materializing the whole token list::

        for tok in stream_tokens(open("main.cw", encoding="utf-8")):
            ...

    Note that most recursive-descent parsers prefer random access, so the
    eager ``tokenize_file`` is usually the simpler integration point.
    """
    lexer = Lexer(emit_comments=emit_comments)
    for line in lines:
        yield from lexer.feed_line(line)
    yield from lexer.eof()


def tokens_to_json(
    tokens: Iterable[Token],
    *,
    indent: int = 2,
    ensure_ascii: bool = False,
) -> str:
    """Serialize tokens as a JSON array (each token via ``Token.to_dict``)."""
    return json.dumps(
        [tok.to_dict() for tok in tokens],
        indent=indent,
        ensure_ascii=ensure_ascii,
    )


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Tokenize CWind source (spec: frontend/Grammar.md)."
    )
    parser.add_argument("file", nargs="?", help="source file (default: stdin)")
    parser.add_argument("--comments", action="store_true", help="emit comment tokens")
    parser.add_argument("--json", action="store_true", help="emit tokens as JSON")
    parser.add_argument("--no-color", action="store_true", help="render errors without ANSI colors")
    args = parser.parse_args(argv)

    lexer = Lexer(emit_comments=args.comments)
    source_text = ""
    try:
        tokens = []
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
        try:  # package-style invocation (python -m frontend.lexer)
            from .errors import render_error
        except ImportError:  # script-style invocation (python frontend/lexer.py)
            from errors import render_error

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
