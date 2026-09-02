"""Top-level collection and declaration checks (SA passes 1 and 2)."""

from .defs import (
    _EXTERN_MAX_NEST,
    _EXTERN_SCALAR_TYPES,
    _EXTERN_SCALAR_WIDTHS,
    _MAIN_RETURN_TYPES,
    _decl_kind_name,
)
from .collect import DeclCollect
from .extern import DeclExtern
from .impls import DeclImpls
from .misc import DeclMisc
from .types_check import DeclTypes


class DeclarationChecks(DeclMisc, DeclExtern, DeclImpls,
                        DeclTypes, DeclCollect):
    pass
