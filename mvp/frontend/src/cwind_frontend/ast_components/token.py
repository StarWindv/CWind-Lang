"""Token kinds and token values produced by the CWind frontend."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class TokenKind(str, Enum):
    """Kinds of tokens produced by the CWind lexer.

    Operator members use their exact source spelling as the value, so
    ``TokenKind("==")`` is ``TokenKind.EQ``.  Each keyword (Grammar.md §1.3)
    has its own kind so that both the parser and the JSON output can name it
    directly (e.g. ``LET``, ``STRUCT``).
    """

    # Names and literals.
    IDENTIFIER = "identifier"
    INTEGER = "integer"
    FLOAT = "float"
    STRING = "string"
    COMMENT = "comment"

    # Keywords.
    STRUCT = "STRUCT"
    ENUM = "ENUM"
    EXTRA = "EXTRA"
    IMPL = "IMPL"
    TRAIT = "TRAIT"
    CONST = "CONST"
    EXTERN = "EXTERN"
    STATIC = "STATIC"
    WHICH = "WHICH"
    WHERE = "WHERE"
    TYPE = "TYPE"
    TYPEDEF = "TYPEDEF"
    GROUP = "GROUP"
    LET = "LET"
    MUT = "MUT"
    FN = "FN"
    PUB = "PUB"
    MOD = "MOD"
    IN = "IN"
    RETURN = "RETURN"
    BREAK = "BREAK"
    CONTINUE = "CONTINUE"
    FOR = "FOR"
    WHILE = "WHILE"
    IF = "IF"
    ELIF = "ELIF"
    ELSE = "ELSE"
    MATCH = "MATCH"

    # Reserved keywords.
    LAMBDA = "LAMBDA"
    IMPORT = "IMPORT"
    USE = "USE"
    AS = "AS"
    WHEN = "WHEN"
    DEFINE = "DEFINE"
    ASYNC = "ASYNC"
    AWAIT = "AWAIT"

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
    FAT_ARROW = "=>"
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
    QUESTION = "?"         # todo-44: macro repetition "zero or one" operator
    DOLLAR = "$"           # reserved symbol
    HASH = "#"             # reserved symbol
    ELLIPSIS = "..."       # todo-87: variadic parameter list (extern only)
    STAR_CONST = "*const"
    STAR_MUT = "*mut"


@dataclass(frozen=True, slots=True)
class Token:
    """A single lexical token.

    Positions are 1-based.  ``end_column`` is exclusive (Python-slice style):
    a one-character token at column 5 spans [5, 6).  Multi-line tokens
    (strings, block comments) carry the starting position in ``line``/``column``
    and the ending position in ``end_line``/``end_column``.

    ``value`` is the decoded value (numbers as int/float, strings unescaped),
    ``raw`` the exact source text of the token.

    ``context`` (todo-44, macro hygiene) is ``None`` for tokens written by
    the programmer and an opaque expansion id for tokens synthesized by
    macro expansion.  Two tokens compare equal only when kinds/values match
    AND their contexts match, so a matcher never lets definition-site text
    capture invocation-site text and hygiene lives at the token layer
    (Rust's SyntaxContext, simplified to a per-expansion integer).
    """

    kind: TokenKind
    value: object
    line: int
    column: int
    end_line: int
    end_column: int
    raw: str
    context: Optional[int] = None

    def with_context(self, context: Optional[int]) -> "Token":
        """A copy of this token carrying *context* (hygiene marker)."""
        if context == self.context:
            return self
        return Token(
            self.kind, self.value, self.line, self.column,
            self.end_line, self.end_column, self.raw, context,
        )

    def to_dict(self) -> dict:
        """Serialize the token to a JSON-friendly dict."""
        d = {
            "kind": self.kind.value,
            "value": self.value,
            "line": self.line,
            "column": self.column,
            "end_line": self.end_line,
            "end_column": self.end_column,
            "raw": self.raw,
        }
        # Only expansion-synthesized tokens carry a context; ordinary
        # source tokens keep the legacy JSON shape byte-for-byte.
        if self.context is not None:
            d["context"] = self.context
        return d
