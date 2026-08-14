"""CWind semantic-analysis package."""

from .analyzer import run_sa, run_sa_with_errors
from .builtin_methods import (
    BUILTIN_MODULE_FUNCTIONS,
    BUILTIN_OBJECTS,
    BUILTIN_TRAITS,
    BUILTIN_TYPE_METHODS,
)
from .errors import SaError, SaResult, SaWarning
from .symbols import BindingInfo, ProgramInfo, Symbol
from .types import BUILTIN_TYPES

__all__ = [
    "BUILTIN_MODULE_FUNCTIONS",
    "BUILTIN_OBJECTS",
    "BUILTIN_TRAITS",
    "BUILTIN_TYPE_METHODS",
    "BUILTIN_TYPES",
    "BindingInfo",
    "ProgramInfo",
    "SaError",
    "SaResult",
    "SaWarning",
    "Symbol",
    "run_sa",
    "run_sa_with_errors",
]
