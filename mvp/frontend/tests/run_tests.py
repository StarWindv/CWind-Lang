"""Progress runner for the CWind frontend unittest suite.

Usage:
    python mvp/frontend/tests/run_tests.py [-v]

``python -m unittest discover`` stays silent until the whole run ends,
which is easy to mistake for a hang on slow disks.  This wrapper keeps a
single rewritten console line with counters / elapsed time / current test,
prints failures and errors in place, and finishes with the classic
unittest summary block.
"""

import sys
import time
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent / "src"))


class _Progress:
    """Single-line console progress state."""

    def __init__(self, total: int) -> None:
        self.total = total
        self.done = 0
        self.t0 = time.time()
        self._last_len = 0

    def draw(self, label: str = "", mark: str = "") -> None:
        elapsed = time.time() - self.t0
        line = f"[{self.done}/{self.total}] {elapsed:6.1f}s {mark} {label}"
        pad = max(0, self._last_len - len(line))
        sys.stdout.write("\r" + line + " " * pad)
        sys.stdout.flush()
        self._last_len = len(line)

    def end_line(self) -> None:
        sys.stdout.write("\n")
        sys.stdout.flush()
        self._last_len = 0


class ProgressResult(unittest.TextTestResult):
    """TextTestResult that drives the single-line progress display."""

    prog: _Progress | None = None
    _mark = "ok"

    def startTest(self, test: unittest.TestCase) -> None:
        super().startTest(test)
        self._mark = ".."
        if self.prog:
            self.prog.draw(self._label(test), self._mark)

    def addSuccess(self, test: unittest.TestCase) -> None:
        super().addSuccess(test)
        self._mark = "ok"

    def addError(self, test: unittest.TestCase, err) -> None:
        super().addError(test, err)
        self._mark = "ERR"

    def addFailure(self, test: unittest.TestCase, err) -> None:
        super().addFailure(test, err)
        self._mark = "FAIL"

    def addSkip(self, test: unittest.TestCase, reason: str) -> None:
        super().addSkip(test, reason)
        self._mark = "skip"

    def stopTest(self, test: unittest.TestCase) -> None:
        super().stopTest(test)
        if not self.prog:
            return
        self.prog.done += 1
        if self._mark in ("ERR", "FAIL"):
            # 失败用例换行落盘, 不被后续 \r 覆盖
            self.prog.end_line()
            sys.stdout.write(f"  -> {self._mark}: "
                             f"{self.getDescription(test)}\n")
            sys.stdout.flush()
            self.prog._last_len = 0
        else:
            self.prog.draw(self._label(test), self._mark)

    @staticmethod
    def _label(test: unittest.TestCase) -> str:
        return f"{test.__class__.__name__}.{getattr(test, '_testMethodName', '')}"


def main() -> int:
    verbose = "-v" in sys.argv[1:]
    suite = unittest.defaultTestLoader.discover(
        str(TESTS), pattern="test*.py")
    total = suite.countTestCases()

    result = ProgressResult
    result.prog = _Progress(total)

    print(f"discovered {total} test(s)")
    runner = unittest.TextTestRunner(
        verbosity=0, resultclass=ProgressResult)
    final = runner.run(suite)
    if ProgressResult.prog:
        ProgressResult.prog.end_line()
    return 0 if final.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
