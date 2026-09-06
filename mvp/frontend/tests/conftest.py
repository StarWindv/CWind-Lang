"""Shared pytest bootstrap: make the test environment explicit.

todo-172-era std addressing: the std (``libs/``) anchor follows the
compiler executable (``CWIND_HOME`` override → executable location →
package location), never the working directory.  Tests pin the install
root to the repository root explicitly so every case runs against the
same std tree regardless of where pytest is launched from — the old
"walk upward from cwd" fallback stays retired.
"""

import os
from pathlib import Path

# tests/conftest.py -> tests -> frontend -> mvp -> repo root
REPO_ROOT = Path(__file__).resolve().parents[3]
os.environ.setdefault("CWIND_HOME", str(REPO_ROOT))
