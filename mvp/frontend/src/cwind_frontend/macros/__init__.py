"""CWind macro system (todo-44): declarative ``macro_rules!`` macros.

The pipeline is lexer -> **this package** -> parser -> SA: definitions
are pulled out of the token stream, every call site is matched against
its rules and spliced with the expansion tokens, and the ordinary parser
never sees macro syntax at all (a desugar pass, per the archaeology
notes on rustc's ``mbe``).  See :mod:`.expansion` for the driver and
``.handover`` for the syntax reference.

Modules (plain names, in pipeline order):

- :mod:`.definition` — the ``MacroDef``/``MacroRule`` records
- :mod:`.trees` — pattern-tree nodes (bindings / repetitions / groups)
- :mod:`.pattern` — reads definition tokens into pattern trees
- :mod:`.validate` — follow-set / empty-repetition / binder checks
- :mod:`.matcher` — NFA match of invocation tokens against a rule
- :mod:`.fragments` — fragment (``expr``/``type``/...) black-box parsing
- :mod:`.expander` — template transcription with lockstep repetitions
- :mod:`.expansion` — the token-stream driver used by the parser
"""

from .definition import MacroDef, MacroRule
from .expansion import (
    MAX_EXPANSION_DEPTH,
    expand_macros,
    recursion_limit_from_env,
)
from .expander import MacroExpandError
from .matcher import (
    MacroMatchError,
    MatchedSeq,
    MatchedToken,
    NamedMatch,
    match_rule,
)
from .pattern import MacroPatternError
from .trees import Binding, Group, Kleene, Repetition

__all__ = [
    "MAX_EXPANSION_DEPTH",
    "Binding",
    "Group",
    "Kleene",
    "MacroDef",
    "MacroExpandError",
    "MacroMatchError",
    "MacroPatternError",
    "MacroRule",
    "MatchedSeq",
    "MatchedToken",
    "NamedMatch",
    "recursion_limit_from_env",
    "Repetition",
    "expand_macros",
    "match_rule",
]
