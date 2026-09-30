"""Synthesized-name allocation: mint a spelling, check it, re-mint on clash.

Several passes invent identifiers that never appear in the source — macro
hygiene contexts (:meth:`~cwind_frontend.parser.core.ParserCore.macro_mangle`),
control-flow desugaring (``acc`` / ``q`` / ``v`` / ``iter`` / ``tryv`` …),
procedural-macro helpers (``__cwpm_*``).  All of them need the same thing:

    hand out a name that is **not already in use**, and remember it so the
    next one does not collide either.

Why the mangled shape is not enough on its own
---------------------------------------------
``_m<digits>_<name>`` looks untypable but is not: ``let _m1000007_acc: Int32
= 5;`` compiles and runs (verified).  A synthesized binding can therefore in
principle be captured by, or capture, a user binding of the same spelling —
a silent miscompile, the worst kind.  Allocating against the set of
spellings actually present is cheap and makes the property hold by
construction instead of by luck.

The *set* of occupied spellings is source-specific (an AST walk vs. a
proc-macro token list), so the caller supplies it; only the mint-check-
retry loop lives here.  Termination: the counter is monotonic and the
occupied set is finite.
"""

from __future__ import annotations

from typing import Callable, Optional

__all__ = ["NameAllocator", "MangleFn"]

#: ``(counter, base) -> spelling``.  Called with a strictly increasing
#: counter until the result is free.
MangleFn = Callable[[int, str], str]


class NameAllocator:
    """Hands out spellings that are not currently in use.

    ``mangle`` decides the *shape*; the allocator only guarantees the
    property that matters: the returned spelling was absent from
    ``occupied`` and is now present in it.  ``start`` seeds the counter, so
    a caller whose counter shares a numbering space with another (parse-time
    macro contexts count from 0; synthesized bindings start at 1_000_000)
    can stay clear of it.
    """

    __slots__ = ("_occupied", "_mangle", "_n")

    def __init__(
        self, occupied: set[str], mangle: MangleFn, start: int = 0
    ) -> None:
        self._occupied = occupied
        self._mangle = mangle
        self._n = start

    @property
    def occupied(self) -> set[str]:
        """The live set, for callers that keep adding to it themselves."""
        return self._occupied

    @property
    def counter(self) -> int:
        """How many candidates have been minted (successful or not)."""
        return self._n

    def fresh(self, base: str) -> str:
        """A spelling for *base* that nothing in scope is already using."""
        while True:
            self._n += 1
            name = self._mangle(self._n, base)
            if name and name not in self._occupied:
                self._occupied.add(name)
                return name

    def reserve(self, name: str) -> None:
        """Mark *name* taken without minting (for pre-known spellings)."""
        if name:
            self._occupied.add(name)


def macro_context_mangle(context: int, base: str) -> str:
    """Mangle in the macro-hygiene format, ``_m<context>_<base>``."""
    from .parser.core import ParserCore

    return ParserCore.macro_mangle(context, base)


def suffixed_mangle(prefix: str) -> MangleFn:
    """``<prefix><base>``, then ``<prefix><base>_2``, ``_3`` … on collision."""

    def mangle(n: int, base: str) -> str:
        return f"{prefix}{base}" if n <= 1 else f"{prefix}{base}_{n}"

    return mangle


def allocator_on(
    holder: object, attr: str, occupied: set[str], mangle: MangleFn,
    start: int = 0,
) -> NameAllocator:
    """A :class:`NameAllocator` memoized on *holder* (a ``Program``).

    Synthesizing a name is a per-tree concern, so the allocator and its
    occupied set live on the tree itself and survive across calls.
    """
    existing: Optional[NameAllocator] = getattr(holder, attr, None)
    if isinstance(existing, NameAllocator):
        return existing
    alloc = NameAllocator(occupied, mangle, start=start)
    setattr(holder, attr, alloc)
    return alloc
