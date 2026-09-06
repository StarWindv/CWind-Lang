"""CWind semantic-analysis package."""

from .fqn import run_pass0, run_pass1
from .analyzer import run_sa, run_sa_with_errors
from .errors import SaError, SaResult, SaWarning
from .symbols import BindingInfo, ProgramInfo, Symbol
from .types import BUILTIN_TYPES

__all__ = [
    "BUILTIN_TYPES",
    "BindingInfo",
    "ProgramInfo",
    "SaError",
    "SaResult",
    "SaWarning",
    "Symbol",
    "run_pass0",
    "run_pass1",
    "run_sa",
    "run_sa_with_errors",
]
