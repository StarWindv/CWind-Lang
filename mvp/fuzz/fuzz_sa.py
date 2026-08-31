"""Back-compat shim.

The fuzzer was originally a single module ``fuzz_sa.py``.  It is now the
package ``cwind_fuzz`` (split into ``paths`` / ``frontend`` / ``backend`` /
``cli``).  This module re-exports the frontend engine + CLI so existing calls
(``python fuzz_sa.py --mode gen ...`` or ``import fuzz_sa`` from an old script)
keep working.  The test-suite patches the *frontend* module directly
(``from cwind_fuzz import frontend as fuzz_sa``) so its monkeypatches land on
the namespace ``analyze``/``load_known_bugs`` actually read from.
"""

from __future__ import annotations

import pathlib
import sys

# ensure the package directory is importable when this file is run directly
_HERE = pathlib.Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from cwind_fuzz import paths  # noqa: E402
from cwind_fuzz.frontend import (  # noqa: E402,F401
    Generator,
    Mutator,
    Case,
    analyze,
    tokenize,
    sig_of,
    load_known_bugs,
    match_known_bug,
    run_campaign,
    default_seeds,
    print_report,
    ROOT,
    REPO,
    REPO_ROOT,
    SRC,
)
from cwind_fuzz.frontend import (  # noqa: E402
    TokenKind,
    Lexer,
    parse_with_errors,
    run_sa_with_errors,
)
from cwind_fuzz.cli import main  # noqa: E402

KNOWN_BUG_PATTERNS = None  # kept in sync below (reloaded lazily for tests)


def _sync_known_bugs() -> None:
    """Mirror the frontend module's live ``KNOWN_BUG_PATTERNS`` onto this shim."""
    global KNOWN_BUG_PATTERNS
    import cwind_fuzz.frontend as _fe

    KNOWN_BUG_PATTERNS = _fe.KNOWN_BUG_PATTERNS


_sync_known_bugs()


if __name__ == "__main__":
    raise SystemExit(main())
