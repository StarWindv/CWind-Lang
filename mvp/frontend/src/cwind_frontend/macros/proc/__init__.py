"""CWind procedural macros (todo-179): CWind-implemented ``#[proc_macro]``.

A procedural macro is an ordinary CWind ``fn`` annotated ``#[proc_macro]``
whose body runs in a **separate process**: the compiler extracts the
definition plus its dependency closure, generates a standalone program
(the macro body + a generated ``main``), compiles it with the ordinary
CWind toolchain, runs it with the call-site tokens on stdin, and splices
the returned tokens back into the stream.  See ``.handover/.../ProcMacro``
for the protocol and the user-facing decisions.

Modules (pipeline order):

- :mod:`.definition` — the ``ProcMacroDef`` record
- :mod:`.collect` — strips ``#[proc_macro]`` items from a token stream
- :mod:`.protocol` — token <-> wire-text conversion (line protocol)
- :mod:`.deps` — dependency collection + generated-program template
- :mod:`.build` — toolchain discovery, subprocess compile, caching
- :mod:`.driver` — the per-build Python driver script (host <-> exe)
- :mod:`.registry` — the compile-wide macro registry (todo-176 visibility)
- :mod:`.builtins` — compiler-builtin macos (``quote!``)
"""

from .definition import ProcMacroDef
from .collect import collect_proc_macros, strip_proc_macros
from .registry import ProcMacroRegistry, flush_scan_cache
from .expand import ProcMacroContext

__all__ = [
    "ProcMacroDef",
    "ProcMacroRegistry",
    "ProcMacroContext",
    "collect_proc_macros",
    "strip_proc_macros",
    "flush_scan_cache",
]
