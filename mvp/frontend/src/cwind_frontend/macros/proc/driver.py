"""Procedure-macro driver script (todo-179).

This file is copied into every macro build directory and executed as an
independent process by the host:

    python driver.py --exe <macro.exe>

It reads a JSON request from stdin, converts the call-site tokens into
the wire text the exe consumes, runs the exe, parses its ``T``/``D``
records, and answers with a JSON response on stdout.  It must therefore
stay standard-library only -- it runs in whatever interpreter the host
used, with no package imports.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Optional

MAX_BLOB = 8192


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="CWind procedure macro driver"
    )
    parser.add_argument("--exe", required=True, help="macro executable")
    parser.add_argument(
        "--timeout", type=float, default=300.0,
        help="seconds before the macro process is killed",
    )
    args = parser.parse_args(argv)
    try:
        request = json.loads(sys.stdin.buffer.read())
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        _write({
            "ok": False,
            "error": f"driver received invalid request JSON: {exc}",
            "tokens": [],
            "diagnostics": [],
        })
        return 0
    response = run(args.exe, request, args.timeout)
    _write(response)
    return 0


def run(exe: str, request: dict, timeout: float) -> dict:
    tokens = request.get("tokens") or []
    wire = _encode_tokens(tokens, request.get("call"))
    if "item_tokens" in request:
        wire += _encode_tokens(request["item_tokens"], request.get("call"))
    try:
        proc = subprocess.run(
            [exe],
            input=wire.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except FileNotFoundError:
        return _fail("macro executable not found: " + exe)
    except subprocess.TimeoutExpired:
        return _fail(f"macro process timed out after {timeout:g}s")
    except OSError as exc:
        return _fail(f"cannot launch macro process: {exc}")
    stdout = proc.stdout.decode("utf-8", "replace")
    stderr = proc.stderr.decode("utf-8", "replace")
    out_tokens, diagnostics, noise = _decode_records(stdout)
    if proc.returncode != 0:
        return _fail(
            f"macro process exited with code {proc.returncode}",
            stdout=stdout,
            stderr=stderr,
            diagnostics=diagnostics,
        )
    if noise and not out_tokens:
        return _fail(
            "macro process produced no token records",
            stdout=stdout,
            stderr=stderr,
            diagnostics=diagnostics,
        )
    return {
        "ok": True,
        "tokens": out_tokens,
        "diagnostics": diagnostics,
        "stdout": _clip(stdout),
        "stderr": _clip(stderr),
    }


def _span_fields(span: object) -> str:
    if (
        not isinstance(span, (list, tuple)) or len(span) != 4
        or any(type(value) is not int or not 1 <= value <= 9223372036854775807 for value in span)
        or (span[2], span[3]) < (span[0], span[1])
    ):
        return "-\t-\t-\t-"
    return "\t".join(str(value) for value in span)


def _encode_tokens(tokens: list, call: Optional[dict] = None) -> str:
    coords = [call.get(key) for key in (
        "line", "column", "end_line", "end_column"
    )] if isinstance(call, dict) else None
    parts = [_span_fields(coords), str(len(tokens))]
    for item in tokens:
        if isinstance(item, (list, tuple)) and len(item) in (2, 3):
            span = item[2] if len(item) == 3 else None
            parts.append(str(item[0]) + "\t" + _span_fields(span))
            parts.append(str(item[1]))
        else:
            parts.append("punct\t-\t-\t-\t-")
            parts.append(str(item))
    return "\n".join(parts) + "\n"


def _decode_span(fields: list[str]) -> Optional[list[int]]:
    if any(not value.isascii() or not value.isdecimal() or len(value) > 19
           for value in fields):
        return None
    return [int(value) for value in fields]


def _decode_message(message: str) -> str:
    escapes = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\"}
    out: list[str] = []
    i = 0
    while i < len(message):
        if message[i] == "\\" and i + 1 < len(message):
            following = message[i + 1]
            if following in escapes:
                out.append(escapes[following])
                i += 2
                continue
        out.append(message[i])
        i += 1
    return "".join(out)


def _decode_records(stdout: str) -> tuple[list, list, list]:
    tokens: list = []
    diagnostics: list = []
    noise: list = []
    for line in stdout.splitlines():
        if not line:
            continue
        if line.startswith("T\t"):
            fields = line.split("\t", 6)
            if len(fields) == 7:
                tokens.append([fields[1], fields[6], _decode_span(fields[2:6])])
            else:
                noise.append(line)
        elif line.startswith("D\t"):
            fields = line.split("\t", 6)
            if len(fields) == 7:
                diagnostics.append({
                    "level": fields[1],
                    "span": _decode_span(fields[2:6]),
                    "message": _decode_message(fields[6]),
                })
            else:
                noise.append(line)
        else:
            noise.append(line)
    return tokens, diagnostics, noise


def _fail(
    message: str,
    *,
    stdout: str = "",
    stderr: str = "",
    diagnostics: Optional[list] = None,
) -> dict:
    return {
        "ok": False,
        "error": message,
        "tokens": [],
        "diagnostics": diagnostics or [],
        "stdout": _clip(stdout),
        "stderr": _clip(stderr),
    }


def _clip(text: str) -> str:
    if len(text) <= MAX_BLOB:
        return text
    return text[:MAX_BLOB] + "\n... (truncated)"


def _write(response: dict) -> None:
    json.dump(response, sys.stdout, ensure_ascii=True)
    sys.stdout.write("\n")
    sys.stdout.flush()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
