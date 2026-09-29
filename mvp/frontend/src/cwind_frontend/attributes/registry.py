"""The attribute-processor registry.

An attribute is *claimed* by exactly one processor; an attribute no
processor claims is an error (see
:func:`cwind_frontend.parser.attrs.dispatch_attributes`).  Built-in
attributes register here at import time, which is why
``macros/`` can ask "which names does the parser own?" instead of
carrying its own copy of the list.

A processor declares:

* ``name``          -- the spelling it claims
* ``positions``     -- the syntactic sites it accepts
* ``apply``         -- validate the arguments and annotate the node
* ``keep``          -- optional predicate; a false verdict prunes the item
                       (this is how ``#[cfg]`` drops a declaration)
* ``redirect``      -- shown when it is used at a site it does not accept
* ``doc``           -- one clause for the "supported attributes" message
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from .model import (
    EXTERN_MEMBER,
    ITEM,
    METHOD,
    POSITIONS,
    USE,
    Attr,
    AttributeError,
)


@dataclass(frozen=True)
class ProcCtx:
    """What a processor may need from the driver.

    Deliberately tiny: the registry must not become a back door into the
    parser.  ``cfg_context`` is built lazily by the caller and
    ``path_is_absolute`` mirrors the backend's path classification.
    """

    cfg_context: Callable[[], object]
    path_is_absolute: Callable[[str], bool]


ApplyFn = Callable[[ProcCtx, object, Attr], None]
KeepFn = Callable[[ProcCtx, Attr], bool]


@dataclass(frozen=True)
class AttrProc:
    name: str
    positions: frozenset[str]
    apply: ApplyFn
    keep: Optional[KeepFn] = None
    redirect: str = ""
    doc: str = ""


_PROCS: dict[str, AttrProc] = {}


def register(proc: AttrProc) -> None:
    """Add one processor.  A duplicate name is a programming error."""
    if proc.name in _PROCS:
        raise AssertionError(
            f"attribute '{proc.name}' already has a processor"
        )
    unknown = proc.positions - POSITIONS
    if unknown:
        raise AssertionError(
            f"attribute '{proc.name}' declares unknown positions: "
            f"{sorted(unknown)}"
        )
    _PROCS[proc.name] = proc


def lookup(name: str) -> Optional[AttrProc]:
    return _PROCS.get(name)


def names() -> tuple[str, ...]:
    """Every registered attribute name."""
    return tuple(sorted(_PROCS))


def known_for(position: str) -> tuple[str, ...]:
    """Registered names accepted at *position* (for diagnostics)."""
    return tuple(
        sorted(n for n, p in _PROCS.items() if position in p.positions)
    )


def parser_owned_names() -> frozenset[str]:
    """Names the *parser* claims.

    The macro layer needs this in two places: to leave a compiler
    attribute alone instead of trying to resolve it as a procedure
    attribute macro, and to re-attach it to each item a macro produces.
    Both used to carry their own copy of the tuple.
    """
    return frozenset(_PROCS)


def describe(position: str) -> str:
    """``"only 'a' / 'b' are supported"`` for the given site."""
    known = known_for(position)
    if not known:
        return "no attributes are supported here"
    return "only " + " / ".join(f"'{n}'" for n in known) + " are supported"
