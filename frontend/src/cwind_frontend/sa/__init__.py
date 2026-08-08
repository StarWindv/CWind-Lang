"""CWind semantic-analysis package."""

from .builtin_methods import (
    BUILTIN_MODULE_FUNCTIONS,
    BUILTIN_OBJECTS,
    BUILTIN_TRAITS,
    BUILTIN_TYPE_METHODS,
)
from .sa import (
    BUILTIN_TYPES,
    ProgramInfo,
    SaError,
    SaResult,
    Symbol,
    run_sa,
    run_sa_with_errors,
)

__all__ = [
    "BUILTIN_MODULE_FUNCTIONS",
    "BUILTIN_OBJECTS",
    "BUILTIN_TRAITS",
    "BUILTIN_TYPE_METHODS",
    "BUILTIN_TYPES",
    "ProgramInfo",
    "SaError",
    "SaResult",
    "Symbol",
    "run_sa",
    "run_sa_with_errors",
]
