"""CWind frontend: lexer, parser, semantic analysis and CLI."""

from .ast_components.token import Token, TokenKind
from .lexer import (
    KEYWORDS,
    KEYWORD_KINDS,
    LexError,
    Lexer,
    RESERVED_KEYWORDS,
    stream_tokens,
    tokenize,
    tokenize_file,
    tokens_to_json,
)
from .render_err import offset_for_position, render_error

__version__ = "0.0.1"

__all__ = [
    "KEYWORDS",
    "KEYWORD_KINDS",
    "LexError",
    "Lexer",
    "RESERVED_KEYWORDS",
    "Token",
    "TokenKind",
    "offset_for_position",
    "render_error",
    "stream_tokens",
    "tokenize",
    "tokenize_file",
    "tokens_to_json",
]
