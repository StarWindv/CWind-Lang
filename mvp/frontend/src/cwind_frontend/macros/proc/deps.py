"""Dependency collection and standalone-program generation (todo-179).

A procedure macro's body runs in its own compiled program.  That program
needs the macro function itself plus everything it references.  This
module collects the transitive closure of top-level items referenced by
the body (``use`` / ``extern`` blocks always ride along, so imports of
other modules keep working inside the generated program) and renders the
whole file as CWind source with a generated ``main`` harness.

Self-recursion is explicitly *not* a dependency: a macro whose only
caller is itself is dead code and never gets compiled (user rule: DCE
definitions that are unused).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ...ast_components.token import Token, TokenKind
from .collect import _scan_attribute, _scan_group
from .definition import ProcMacroDef

__all__ = ["generate_program", "collect_file_items"]

_OPEN_OF = {
    TokenKind.LPAREN: TokenKind.RPAREN,
    TokenKind.LBRACKET: TokenKind.RBRACKET,
    TokenKind.LBRACE: TokenKind.RBRACE,
}

_ITEM_KEYWORDS = {
    TokenKind.FN: "fn",
    TokenKind.STRUCT: "struct",
    TokenKind.ENUM: "enum",
    TokenKind.TRAIT: "trait",
    TokenKind.EXTRA: "extra",
    TokenKind.IMPL: "impl",
    TokenKind.CONST: "const",
    TokenKind.STATIC: "static",
    TokenKind.TYPE: "type",
    TokenKind.TYPEDEF: "typedef",
    TokenKind.USE: "use",
    TokenKind.EXTERN: "extern",
}

_ALWAYS_INCLUDE = ("use", "extern")


@dataclass
class _Item:
    name: Optional[str]
    kind: str
    start: int
    end: int
    tokens: list[Token]


def collect_file_items(tokens: list[Token]) -> list[_Item]:
    """Index the file's top-level items (name + token span).

    The scanner mirrors the parser's declaration surface loosely; it only
    needs item boundaries and declared names for dependency matching.
    Nested groups are skipped atomically, so a brace inside a body cannot
    end an item early.
    """
    items: list[_Item] = []
    i = 0
    total = len(tokens)
    while i < total:
        start = i
        # Leading attributes belong to the item (cfg/link travel along).
        while i < total and tokens[i].kind == TokenKind.HASH:
            attr = _scan_attribute(tokens, i)
            if attr is None:
                break
            i = attr[0]
        if i < total and tokens[i].kind == TokenKind.PUB:
            i += 1
            if i < total and tokens[i].kind == TokenKind.LPAREN:
                end = _scan_group(tokens, i)
                if end is None:
                    break
                i = end
        if i >= total:
            break
        tok = tokens[i]
        kind = _ITEM_KEYWORDS.get(tok.kind)
        if kind is None:
            # Unknown top-level token: skip one token and resync.
            i += 1
            continue
        name = None
        if kind not in _ALWAYS_INCLUDE:
            name = _declared_name(tokens, i, kind)
        end = _item_end(tokens, i, kind)
        if end is None:
            end = total
        if kind in _ALWAYS_INCLUDE or name is not None:
            items.append(_Item(name, kind, start, end, list(tokens[start:end])))
        i = end
    return items


def _declared_name(
    tokens: list[Token], i: int, kind: str
) -> Optional[str]:
    if kind == "impl":
        # The owner is the identifier just before the body brace.
        j = i + 1
        last: Optional[Token] = None
        while j < len(tokens) and tokens[j].kind != TokenKind.LBRACE:
            if tokens[j].kind == TokenKind.IDENTIFIER:
                last = tokens[j]
            j += 1
        return str(last.value) if last is not None else None
    j = i + 1
    if kind in ("const", "static"):
        # ``const NAME: T`` / ``static mut NAME: T``
        if j < len(tokens) and tokens[j].kind == TokenKind.MUT:
            j += 1
    if j < len(tokens) and tokens[j].kind == TokenKind.IDENTIFIER:
        return str(tokens[j].value)
    return None


def _item_end(tokens: list[Token], i: int, kind: str) -> Optional[int]:
    """One past the item's last token (group close or ``;``)."""
    total = len(tokens)
    j = i + 1
    if kind == "impl":
        while j < total and tokens[j].kind != TokenKind.LBRACE:
            j += 1
        return _scan_group(tokens, j) if j < total else None
    if kind in ("fn", "struct", "enum", "trait", "extra"):
        # Skip to the first body brace (a struct may end at ``;``).
        while j < total:
            if tokens[j].kind == TokenKind.LBRACE:
                return _scan_group(tokens, j)
            if tokens[j].kind == TokenKind.SEMICOLON:
                return j + 1
            if tokens[j].kind in _OPEN_OF:
                end = _scan_group(tokens, j)
                if end is None:
                    return None
                j = end
                continue
            j += 1
        return total
    if kind == "extern":
        # ``extern "C" { ... }`` (block) or a declaration ending at ``;``.
        while j < total:
            if tokens[j].kind == TokenKind.LBRACE:
                return _scan_group(tokens, j)
            if tokens[j].kind == TokenKind.SEMICOLON:
                return j + 1
            j += 1
        return total
    # use/type/typedef/const/static: scan to ``;`` at this level.
    while j < total:
        kind_j = tokens[j].kind
        if kind_j == TokenKind.SEMICOLON:
            return j + 1
        if kind_j in _OPEN_OF:
            end = _scan_group(tokens, j)
            if end is None:
                return None
            j = end
            continue
        j += 1
    return total


def _identifiers(tokens: list[Token]) -> set[str]:
    names: set[str] = set()
    for tok in tokens:
        if tok.kind == TokenKind.IDENTIFIER:
            names.add(str(tok.value))
    return names


def generate_program(
    defn: ProcMacroDef,
    local_defs: Optional[dict[str, ProcMacroDef]] = None,
) -> str:
    """Render the standalone program for *defn*.

    Layout: the defining file's ``use``/``extern`` items, then the macro
    function's transitive local dependencies in source order (other
    ``#[proc_macro]`` items lose the attribute when pulled), then the
    generated ``main`` that runs the protocol.
    """
    local_defs = local_defs or {}
    items = collect_file_items(defn.file_tokens)
    groups: dict[str, list[_Item]] = {}
    always: list[_Item] = []
    for item in items:
        if item.name is None:
            always.append(item)
        else:
            groups.setdefault(item.name, []).append(item)

    included: dict[int, _Item] = {}
    queue: list[str] = []

    def add_group(name: str) -> None:
        for item in groups.get(name, ()):
            if item.start not in included:
                included[item.start] = item
                queue.append(name)

    seeds = _identifiers(defn.fn_tokens)
    seeds.discard(defn.name)  # self-recursion is not a dependency
    for name in sorted(seeds):
        add_group(name)
    while queue:
        name = queue.pop()
        for item in groups.get(name, ()):
            for ref in _identifiers(item.tokens):
                if ref == defn.name:
                    continue
                add_group(ref)

    chunks: list[str] = ["// CWind procedure-macro program (generated)"]
    if defn.source_path:
        chunks.append(f"// source: {defn.source_path}")
        chunks.append(f"// macro: {defn.name}")
    for item in sorted(always, key=lambda it: it.start):
        chunks.append(_render(item.tokens))
    # The macro function itself (attribute stripped) is the program's
    # entry point for the harness below.  It is renamed to a
    # collision-proof internal name: a macro may legitimately be called
    # ``print`` and the std bodies flattened into this program resolve
    # bare calls against the flat namespace, so the macro's own name
    # would hijack them (bug-54 class).
    chunks.append(_render_macro_fn(defn))
    for item in sorted(included.values(), key=lambda it: it.start):
        if item.kind == "impl":
            # Pull method bodies' local dependencies too (already done via
            # the closure) but never the block of another macro's owner by
            # accident -- by_name keys make that natural.
            pass
        macro_def = local_defs.get(item.name or "")
        if macro_def is not None:
            chunks.append(_render(macro_def.fn_tokens))
        else:
            chunks.append(_render(item.tokens))
    chunks.append(_harness(defn))
    return "\n".join(chunks) + "\n"


def _render(tokens: list[Token]) -> str:
    return " ".join(tok.raw for tok in tokens)


def _macro_fn_name(defn: ProcMacroDef) -> str:
    """The internal name the macro function gets inside the program.

    ``__cwpm_<name>`` cannot collide with source-spelled identifiers
    (double leading underscores are untypable user names here) and keeps
    the harness call stable across builds.
    """
    return f"__cwpm_{defn.name}"


def _render_macro_fn(defn: ProcMacroDef) -> str:
    """Render the macro function with its name swapped for the internal
    one (see :func:`_macro_fn_name`).

    Every identifier spelling the macro's own name is renamed too, so a
    body that calls itself as a plain function still resolves; the one
    exception is a name in method/field position (``x.print``) or a path
    tail (``m::print``), which must keep the user's spelling.
    """
    renamed = _macro_fn_name(defn)
    out: list[Token] = []
    prev: Optional[Token] = None
    for tok in defn.fn_tokens:
        if (
            tok.kind == TokenKind.IDENTIFIER
            and str(tok.value) == defn.name
            and not (
                prev is not None
                and prev.kind in (TokenKind.DOT, TokenKind.PATH)
            )
        ):
            out.append(Token(
                tok.kind, renamed,
                tok.line, tok.column, tok.end_line, tok.end_column,
                renamed, None,
            ))
        else:
            out.append(tok)
        prev = tok
    return _render(out)


def _harness(defn: ProcMacroDef) -> str:
    return (
        "use std::proc_macro::stream_from_stdin;\n"
        "use std::proc_macro::stream_to_stdout;\n"
        "fn main() {\n"
        "    let __pm_input: std::proc_macro::TokenStream = "
        "stream_from_stdin();\n"
        f"    let __pm_output: std::proc_macro::TokenStream = "
        f"{_macro_fn_name(defn)}(__pm_input);\n"
        "    stream_to_stdout(__pm_output);\n"
        "}\n"
    )
