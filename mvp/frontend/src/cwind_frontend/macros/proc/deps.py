"""Dependency collection and standalone-program generation (todo-179).

A procedure macro's body runs in its own compiled program.  That program
needs the macro function itself plus everything it references.  This
module collects the transitive closure of top-level items referenced by
the body (``use`` items always ride along; extern blocks are indexed by
member declaration names) and renders the whole file as CWind source with
a generated ``main`` harness.

Self-recursion is explicitly *not* a dependency: a macro whose only
caller is itself is dead code and never gets compiled (user rule: DCE
definitions that are unused).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
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

_ALWAYS_INCLUDE = ("use",)


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
        if kind not in (*_ALWAYS_INCLUDE, "extern"):
            name = _declared_name(tokens, i, kind)
        end = _item_end(tokens, i, kind)
        if end is None:
            end = total
        if kind in (*_ALWAYS_INCLUDE, "extern") or name is not None:
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


def _extern_members(item: _Item) -> list[_Item]:
    """Index only declarations at the extern body's top level.

    Attributes (including their payloads) and signature groups are skipped
    atomically; parameter names must not make a block a dependency.
    """
    i = 0
    while i < len(item.tokens):
        attr = _scan_attribute(item.tokens, i)
        if attr is not None:
            i = attr[0]
            continue
        if item.tokens[i].kind == TokenKind.LBRACE:
            end = _scan_group(item.tokens, i)
            if end is not None:
                return collect_file_items(item.tokens[i + 1:end - 1])
            break
        i += 1
    return []


def _extern_copy(item: _Item, defn: ProcMacroDef, path: tuple[str, ...]) -> _Item:
    """Suppress only the bootstrap macro's block-level invocation.

    Member invocations cannot be bypassed: building the macro would need
    that macro to produce its own foreign dependencies. Reject before the
    generated program is compiled (or a cached executable is consulted).
    """
    if defn.kind != "attribute" or defn.name in ("cfg", "link", "link_name"):
        return item
    out: list[Token] = []
    i = 0
    while i < len(item.tokens):
        attr = _scan_attribute(item.tokens, i)
        if attr is None:
            break
        if attr[1] != defn.name:
            out.extend(item.tokens[i:attr[0]])
        i = attr[0]
    out.extend(item.tokens[i:])
    for member in _extern_members(item):
        j = 0
        while j < len(member.tokens):
            attr = _scan_attribute(member.tokens, j)
            if attr is None:
                break
            if attr[1] == defn.name:
                anchor = member.tokens[j]
                cycle = " -> ".join((*path, f"#[{defn.name}]"))
                raise ValueError(
                    f"circular dependency building procedure macro '{defn.name}': "
                    f"{cycle}; extern member '{member.name}' requires the macro "
                    f"under construction at {defn.source_path or '<unknown>'}:"
                    f"{anchor.line}:{anchor.column}"
                )
            j = attr[0]
    return replace(item, tokens=out)


def _extern_references(tokens: list[Token]) -> set[str]:
    # Attribute names/payloads are not foreign symbol or type references.
    # Leave the tokens themselves intact for parser-owned diagnostics.
    refs: set[str] = set()
    i = 0
    while i < len(tokens):
        attr = _scan_attribute(tokens, i)
        if attr is not None:
            i = attr[0]
            continue
        if tokens[i].kind == TokenKind.IDENTIFIER:
            refs.add(str(tokens[i].value))
        i += 1
    return refs


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
        if item.kind == "extern":
            for member in _extern_members(item):
                if member.name is not None:
                    groups.setdefault(member.name, []).append(item)
        elif item.kind in _ALWAYS_INCLUDE:
            always.append(item)
        elif item.name is not None:
            groups.setdefault(item.name, []).append(item)

    included: dict[int, _Item] = {}
    queue: list[tuple[_Item, tuple[str, ...]]] = []

    def add_group(name: str, path: tuple[str, ...]) -> None:
        for item in groups.get(name, ()):
            if item.start not in included:
                dependency_path = (*path, name)
                if item.kind == "extern":
                    item = _extern_copy(item, defn, dependency_path)
                included[item.start] = item
                queue.append((item, dependency_path))

    seeds = _identifiers(defn.fn_tokens)
    seeds.discard(defn.name)  # self-recursion is not a dependency
    for name in sorted(seeds):
        add_group(name, (defn.name,))
    while queue:
        item, path = queue.pop()
        refs = (_extern_references(item.tokens) if item.kind == "extern"
                else _identifiers(item.tokens))
        for ref in sorted(refs):
            if ref == defn.name:
                continue
            add_group(ref, path)

    pulled = [*always, *included.values()]
    occupied = _occupied_names(defn, pulled)
    internal = _macro_fn_name(defn.name, occupied)
    chunks: list[str] = ["// CWind procedure-macro program (generated)"]
    if defn.source_path:
        chunks.append(f"// source: {defn.source_path}")
        chunks.append(f"// macro: {defn.name}")
    for item in sorted(always, key=lambda it: it.start):
        chunks.append(_render(item.tokens))
    # The macro function itself (attribute stripped) is the program's
    # entry point for the harness below.  It is renamed to an internal
    # name: a macro may legitimately be called ``print`` and the std
    # bodies flattened into this program resolve bare calls against the
    # flat namespace, so the macro's own name would hijack them
    # (bug-54 class).  The name is picked against every identifier the
    # generated program carries, so a user helper spelled exactly like
    # the default choice cannot shadow or duplicate it.
    chunks.append(_render_macro_fn(defn, internal))
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
    chunks.append(_harness(internal, defn.kind))
    return "\n".join(chunks) + "\n"


def _render(tokens: list[Token]) -> str:
    return " ".join(tok.raw for tok in tokens)


def _occupied_names(
    defn: ProcMacroDef,
    pulled: list[_Item],
) -> set[str]:
    """Every identifier spelling the generated program may carry.

    Only items actually rendered (the macro body, the pulled closure and
    the always-include ``use``/``extern`` items) plus the harness
    temporaries count; a same-named item left out of the program cannot
    collide, so avoiding it would rename pointlessly.
    """
    occupied = {
        "__pm_input", "__pm_output", "__pm_item",
        "stream_from_stdin", "stream_to_stdout", "main",
    }
    occupied.update(_identifiers(defn.fn_tokens))
    for item in pulled:
        occupied.update(_identifiers(item.tokens))
    return occupied


def _macro_fn_name(base: str, occupied: set[str]) -> str:
    """An unoccupied ``__cwpm_<base>``-shaped internal name.

    Falls back to ``__cwpm_<base>_2`` and so on, so a user helper named
    exactly like the default choice cannot duplicate the renamed macro
    (see the referenced-helper collision tests).  The loop terminates:
    the alphabet of candidate suffixes grows each round.
    """
    name = f"__cwpm_{base}"
    counter = 2
    while name in occupied:
        name = f"__cwpm_{base}_{counter}"
        counter += 1
    occupied.add(name)
    return name


def _render_macro_fn(defn: ProcMacroDef, renamed: str) -> str:
    """Render the macro function with its name swapped for the internal
    one (see :func:`_macro_fn_name`).

    Every identifier spelling the macro's own name is renamed too, so a
    body that calls itself as a plain function still resolves; the one
    exception is a name in method/field position (``x.print``) or a path
    tail (``m::print``), which must keep the user's spelling.
    """
    out: list[Token] = []
    prev: Optional[Token] = None
    i = 0
    while i < len(defn.fn_tokens):
        tok = defn.fn_tokens[i]
        if (tok.kind == TokenKind.IDENTIFIER and i + 2 < len(defn.fn_tokens)
                and defn.fn_tokens[i + 1].kind == TokenKind.NOT
                and defn.fn_tokens[i + 2].kind in _OPEN_OF):
            end = _scan_group(defn.fn_tokens, i + 2)
            if end is not None:
                # Macro arguments are token data, not references to this
                # standalone function (notably self-invocations in quote!).
                out.extend(defn.fn_tokens[i:end])
                prev = defn.fn_tokens[end - 1]
                i = end
                continue
        i += 1
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


def _harness(internal: str, kind: str = "function") -> str:
    second = (
        "    let __pm_item: std::proc_macro::TokenStream = "
        "stream_from_stdin();\n"
        if kind == "attribute" else ""
    )
    args = "__pm_input, __pm_item" if kind == "attribute" else "__pm_input"
    return (
        "use std::proc_macro::stream_from_stdin;\n"
        "use std::proc_macro::stream_to_stdout;\n"
        "fn main() {\n"
        "    let __pm_input: std::proc_macro::TokenStream = "
        "stream_from_stdin();\n"
        + second
        + "    let __pm_output: std::proc_macro::TokenStream = "
        f"{internal}({args});\n"
        "    stream_to_stdout(__pm_output);\n"
        "}\n"
    )
