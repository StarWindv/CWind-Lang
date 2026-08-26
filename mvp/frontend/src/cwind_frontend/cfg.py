"""Compile-time ``cfg`` predicates (todo-86/93, todo-103/106).

``#[cfg(...)]`` gates top-level declarations on the compile-time target.
Predicates form a small tree:

=============================  =============================================
form                           meaning
=============================  =============================================
``flag``                       bare name (``windows`` / ``unix`` / ...)
``key = "value"``              key-value test (``target_os = "windows"``,
                               ``target_arch = "x86_64"``, ...)
``all(p1, ...)``               true when every sub-predicate is true
``any(p1, ...)``               true when any sub-predicate is true
``not(p)``                     negation (exactly one sub-predicate)
=============================  =============================================

The parser resolves a ``#[cfg]`` right after the item it precedes is
parsed: a false predicate drops the item from the AST entirely, so the
SA/backend never see it (Rust pre-expansion semantics; two mutually
exclusive definitions of the same name therefore never collide).

todo-103/106: besides ``target_os``, the key-value form now accepts
``target_arch``, ``target_vendor`` and ``target_pointer_width``; more
unix-family OS names are recognized so cross-target libraries can be
authored without touching the compiler.
"""

from __future__ import annotations

import os
import platform
import struct
import sys
from dataclasses import dataclass
from typing import Optional, Tuple

__all__ = [
    "ARCH_NAMES",
    "CFG_COMBINATORS",
    "CFG_FLAGS",
    "CFG_KEYS",
    "FLAG_BY_OS",
    "OS_NAMES",
    "POINTER_WIDTHS",
    "VENDOR_NAMES",
    "CfgContext",
    "CfgPredicate",
    "TargetCfg",
    "detect_target_arch",
    "detect_target_os",
    "detect_target_pointer_width",
    "detect_target_vendor",
    "evaluate_cfg",
]


# Values accepted by the ``target_os`` key (todo-86, expanded todo-106).
# ``android`` is a first-class target because Termux is an officially
# supported CWind platform (readme §1.1.3); the remaining unix-family
# names exist so libc-style bindings can be gated for them even though
# CWind does not ship toolchains for those hosts yet.
OS_NAMES: Tuple[str, ...] = (
    "windows", "linux", "macos", "android",
    "freebsd", "netbsd", "openbsd", "solaris",
)

# Values accepted by the ``target_arch`` key (todo-103).
ARCH_NAMES: Tuple[str, ...] = (
    "x86", "x86_64",
    "arm", "aarch64",
    "mips", "mips64",
    "powerpc", "powerpc64",
    "riscv32", "riscv64",
    "s390x", "sparc64",
    "wasm32", "wasm64",
    "csky", "hexagon", "msp430", "xtensa", "loongarch64",
)

# Values accepted by the ``target_vendor`` key (todo-106).
VENDOR_NAMES: Tuple[str, ...] = (
    "unknown", "apple", "pc", "sony", "kmc", "espressif",
)

# Values accepted by the ``target_pointer_width`` key (todo-103/106).
POINTER_WIDTHS: Tuple[str, ...] = ("16", "32", "64")

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
    "freebsd": frozenset({"unix", "freebsd"}),
    "netbsd": frozenset({"unix", "netbsd"}),
    "openbsd": frozenset({"unix", "openbsd"}),
    "solaris": frozenset({"unix", "solaris"}),
}
CFG_FLAGS: Tuple[str, ...] = (
    "windows", "unix", "linux", "macos", "android",
    "freebsd", "netbsd", "openbsd", "solaris",
)
_UNIX_FALLBACK_FLAGS = frozenset({"unix"})

# Key-value configuration keys and their legal values; the parser rejects
# unknown keys *and* unknown values per key so a typo cannot silently
# change what compiles.
CFG_KEYS: Tuple[str, ...] = (
    "target_os", "target_arch", "target_vendor", "target_pointer_width",
)
CFG_KEY_VALUES = {
    "target_os": OS_NAMES,
    "target_arch": ARCH_NAMES,
    "target_vendor": VENDOR_NAMES,
    "target_pointer_width": POINTER_WIDTHS,
}


@dataclass(frozen=True)
class TargetCfg:
    """Explicit ``--target-*`` overrides for ``#[cfg]`` evaluation.

    ``None`` members keep host auto-detection for that component
    (todo-86/93/103/106).
    """

    os: Optional[str] = None
    arch: Optional[str] = None
    vendor: Optional[str] = None
    pointer_width: Optional[str] = None


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
    if system == "FreeBSD":
        return "freebsd"
    if system == "NetBSD":
        return "netbsd"
    if system == "OpenBSD":
        return "openbsd"
    if system == "SunOS":
        return "solaris"
    return "unix"


# platform.machine() spellings -> CWind/Rust architecture names.
_MACHINE_TO_ARCH = {
    "amd64": "x86_64", "x86_64": "x86_64", "x64": "x86_64",
    "i386": "x86", "i486": "x86", "i586": "x86", "i686": "x86", "x86": "x86",
    "armv7l": "arm", "armv6l": "arm", "arm": "arm", "armv8l": "arm",
    "aarch64": "aarch64", "arm64": "aarch64",
    "mips": "mips", "mips64": "mips64",
    "ppc": "powerpc", "powerpc": "powerpc",
    "ppc64": "powerpc64", "powerpc64": "powerpc64",
    "riscv": "riscv32", "riscv32": "riscv32", "riscv64": "riscv64",
    "s390x": "s390x",
}


def detect_target_arch() -> Optional[str]:
    """Best-effort host CPU architecture (:data:`ARCH_NAMES` member or
    ``None`` when the host machine is unrecognized)."""
    machine = platform.machine().lower()
    return _MACHINE_TO_ARCH.get(machine)


def detect_target_vendor() -> str:
    """Host vendor triple component: ``apple`` on macOS/iOS hosts,
    ``pc`` on Windows, ``unknown`` elsewhere."""
    if sys.platform == "win32":
        return "pc"
    if sys.platform == "darwin":
        return "apple"
    return "unknown"


def detect_target_pointer_width() -> str:
    """Pointer width of the interpreter's platform ABI (bits as text)."""
    try:
        import ctypes  # local import: only needed at detection time

        return str(ctypes.sizeof(ctypes.c_void_p) * 8)
    except Exception:
        return str(struct.calcsize("P") * 8)


class CfgContext:
    """The compile-time configuration a predicate is evaluated against."""

    def __init__(
        self,
        target_os: Optional[str] = None,
        target_arch: Optional[str] = None,
        target_vendor: Optional[str] = None,
        target_pointer_width: Optional[str] = None,
    ) -> None:
        self.target_os = (
            target_os if target_os is not None else detect_target_os()
        )
        self.flags = FLAG_BY_OS.get(self.target_os, _UNIX_FALLBACK_FLAGS)
        self.target_arch = (
            target_arch if target_arch is not None
            else detect_target_arch()
        )
        self.target_vendor = (
            target_vendor if target_vendor is not None
            else detect_target_vendor()
        )
        self.target_pointer_width = (
            target_pointer_width if target_pointer_width is not None
            else detect_target_pointer_width()
        )

    def kv_value(self, key: str) -> Optional[str]:
        """The context value for a kv predicate key (or ``None``)."""
        if key == "target_os":
            return self.target_os
        if key == "target_arch":
            return self.target_arch
        if key == "target_vendor":
            return self.target_vendor
        if key == "target_pointer_width":
            return self.target_pointer_width
        return None


def evaluate_cfg(predicate: CfgPredicate, context: CfgContext) -> bool:
    """Evaluate a predicate tree against ``context``."""
    kind = predicate.kind
    if kind == "flag":
        return predicate.name in context.flags
    if kind == "kv":
        expected = context.kv_value(predicate.name)
        if expected is None:
            # Unknown arch on this host: only a wildcard-less exact match
            # could hold; there is nothing to match against, so fail.
            return False
        return predicate.value == expected
    if kind == "all":
        return all(evaluate_cfg(arg, context) for arg in predicate.args)
    if kind == "any":
        return any(evaluate_cfg(arg, context) for arg in predicate.args)
    if kind == "not":
        # Exactly-one-arg is guaranteed by the parser.
        return not evaluate_cfg(predicate.args[0], context)
    raise ValueError(f"unknown cfg predicate kind {kind!r}")
