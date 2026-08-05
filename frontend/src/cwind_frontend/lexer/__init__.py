"""CWind lexer package (streaming, line-oriented)."""

from .lexer import (
    KEYWORDS,
    KEYWORD_KINDS,
    LexError,
    Lexer,
    LexResult,
    RESERVED_KEYWORDS,
    lex_with_errors,
    stream_tokens,
    tokenize,
    tokenize_file,
    tokens_to_json,
)

__all__ = [
    "KEYWORDS",
    "KEYWORD_KINDS",
    "LexError",
    "Lexer",
    "LexResult",
    "RESERVED_KEYWORDS",
    "lex_with_errors",
    "stream_tokens",
    "tokenize",
    "tokenize_file",
    "tokens_to_json",
]
