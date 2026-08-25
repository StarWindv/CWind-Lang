"""Compile-time ``cfg`` predicates (todo-86/93).

``#[cfg(...)]`` gates top-level declarations on the compile-time target.
Predicates form a small tree:

==============================  =============================================
form                            meaning
==============================  =============================================
``flag``                        bare name (``windows`` / ``unix`` / ...)
``key = "value"``               key-value test (``target_os = "windows"``)
``all(p1, ...)``                true when every sub-predicate is true
``any(p1, ...)``                true when any sub-predicate is true
``not(p)``                      negation (exactly one sub-predicate)
==============================  =============================================

The parser resolves a ``#[cfg]`` right after the item it precedes is
parsed: a false predicate drops the item from the AST entirely, so the
SA/backend never see it (Rust pre-expansion semantics; two mutually
exclusive definitions of the same name therefore never collide).
"""

from __future__ import annotations

import os
import platform
import sys
from dataclasses import dataclass
from typing import Optional, Tuple

__all__ = [
    "CFG_COMBINATORS",
    "CFG_FLAGS",
    "CFG_KEYS",
    "FLAG_BY_OS",
    "OS_NAMES",
    "CfgContext",
    "CfgPredicate",
    "detect_target_os",
    "evaluate_cfg",
]


# Values accepted by the ``target_os`` key (todo-86).  ``android`` is a
# first-class target because Termux is an officially supported CWind
# platform (readme §1.1.3).
OS_NAMES: Tuple[str, ...] = ("windows", "linux", "macos", "android")

# Combinator predicates (todo-93).
CFG_COMBINATORS: Tuple[str, ...] = ("all", "any", "not")

# Bare flags recognized inside a cfg predicate.  ``unix`` follows the Rust
# family convention: true on every non-Windows target.  Deliberate
# deviation from Rust: ``android`` also holds the bare ``linux`` flag
# (bionic on the Linux kernel), while ``target_os`` still distinguishes
# the two exactly.
FLAG_BY_OS = {
    "windows": frozenset({"windows"}),
    "linux": frozenset({"unix", "linux"}),
    "macos": frozenset({"unix", "macos"}),
    "android": frozenset({"unix", "linux", "android"}),
}
CFG_FLAGS: Tuple[str, ...] = ("windows", "unix", "linux", "macos", "android")
_UNIX_FALLBACK_FLAGS = frozenset({"unix"})

# Key-value configuration keys; only ``target_os`` for now.
CFG_KEYS: Tuple[str, ...] = ("target_os",)


@dataclass(frozen=True)
class CfgPredicate:
    """One parsed ``#[cfg(...)]`` predicate.

    ``kind`` is ``"flag"``, ``"kv"`` or one of :data:`CFG_COMBINATORS`.
    ``name`` carries the flag/key/combinator name, ``value`` the string
    of a kv predicate and ``args`` the sub-predicates of a combinator
    (empty ``all``/``any`` are allowed and follow Rust semantics).
    """

    kind: str
    name: str = ""
    value: Optional[str] = None
    args: Tuple["CfgPredicate", ...] = ()

    def describe(self) -> str:
        """Human-readable rendering for diagnostics."""
        if self.kind == "kv":
            return f'{self.name} = "{self.value}"'
        if self.kind == "flag":
            return self.name
        inner = ", ".join(arg.describe() for arg in self.args)
        return f"{self.name}({inner})"


def _looks_like_android() -> bool:
    """Best-effort Android/Termux detection on a Linux-kernel host."""
    if "TERMUX_VERSION" in os.environ:
        return True
    try:
        # Termux app data dir only exists on Android; harmless elsewhere.
        return os.path.isdir("/data/data/com.termux")
    except OSError:
        return False


def detect_target_os() -> str:
    """Best-effort host detection, returning a member of :data:`OS_NAMES`
    or the pseudo-target ``"unix"`` (only the bare ``unix`` flag holds)."""
    if sys.platform == "win32":
        return "windows"
    system = platform.system()
    if system == "Darwin":
        return "macos"
    if system == "Linux":
        return "android" if _looks_like_android() else "linux"
    return "unix"


class CfgContext:
    """The compile-time configuration a predicate is evaluated against."""

    def __init__(self, target_os: Optional[str] = None) -> None:
        if target_os is None:
            target_os = detect_target_os()
        self.target_os = target_os
        self.flags = FLAG_BY_OS.get(target_os, _UNIX_FALLBACK_FLAGS)


def evaluate_cfg(predicate: CfgPredicate, context: CfgContext) -> bool:
    """Evaluate a predicate tree against ``context``."""
    kind = predicate.kind
    if kind == "flag":
        return predicate.name in context.flags
    if kind == "kv":
        if predicate.name == "target_os":
            return predicate.value == context.target_os
        return False  # unreachable: the parser rejects unknown keys
    if kind == "all":
        return all(evaluate_cfg(arg, context) for arg in predicate.args)
    if kind == "any":
        return any(evaluate_cfg(arg, context) for arg in predicate.args)
    if kind == "not":
        # Exactly-one-arg is guaranteed by the parser.
        return not evaluate_cfg(predicate.args[0], context)
    raise ValueError(f"unknown cfg predicate kind {kind!r}")
