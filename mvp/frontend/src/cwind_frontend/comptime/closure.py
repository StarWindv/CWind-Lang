"""Const-fn evaluation unit: dependency closure + export wrapper (task: comptime).

The evaluation unit is the const-fn analogue of a procedure macro's
generated program (``macros/proc/deps.py``): everything one ``const fn``
call needs, rendered into a single standalone source file.  Two
deliberate deviations from the macro's *token-file* closure:

* **AST-level closure** — the unit is gathered from the live (already
  parsed, macro-expanded, import-flattened) program instead of re-reading
  the definition file's tokens.  The frontend tests feed sources as
  in-memory strings (no file on disk), cross-module items are already
  flattened into one program, and macro invocations are already expanded
  — a token-file closure could not work for any of those.
* **ann-aware reference collection** — the macro BFS matches bare
  identifier text against one file's items; here every resolved name
  carries its SA binding (``ann.binding.ref`` / ``ann.call``), so the
  closure follows *exact* declarations first (no std/user shadowing
  duplicates) and falls back to name matching only for types.

Emission renders the closure through the unparser (``render.source``)
with ``const fn`` / ``const type`` markers stripped: the unit compiles
no-std, and imports are **precise** — one ``use <mod>::<name>;`` per
facility name the closure actually consumed (plus a module wildcard
only for a nameless exact dependency such as an ``extern`` block), not
the procedure macro's blanket ``use std::…::*`` surface.  The marker
registry surface (``const type``) comes from the *real* std files the
facility imports load, not from copies inside the unit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from ..ast_components.ast import (
    Attribute,
    Call,
    ExtraDecl,
    ExternBlock,
    FnDecl,
    ImplDecl,
    ModDecl,
    Name,
    Node,
    Program,
    TraitDecl,
    Type,
)
from ..render.source import render_program
from ..sa.optimize.syntactic_deps import _syntactic_provides
from ..sa.types import BUILTIN_TYPES

if TYPE_CHECKING:
    from ..sa.analyzer import _Analyzer

__all__ = ["WrapperSpec", "EvaluationUnit", "build_unit_source"]

# std module paths the facility imports (``use std::...::*``) already
# provide wholesale.  Pulling those items into the unit as well would
# duplicate every trait/type/const they export (``Duplicate definition
# of 'From'/'Option'/'c_uint'...``), so the closure skips them entirely:
# anything reachable there is importable, and the unit compiles against
# the *real* std files rather than copies.
_FACILITY_PATHS = (
    ("std", "builtins"),
    ("std", "clone"),
    ("std", "ctypedef"),
    ("std", "option"),
    ("std", "traits"),
)


def _facility_covered(unit: Node) -> bool:
    path = tuple(getattr(unit, "source_module_path", None) or ())
    if not path or path[0] != "std":
        return False
    return any(path[: len(p)] == p for p in _FACILITY_PATHS)


def _record_import(unit: Node, imports: set[str]) -> None:
    """One precise ``use`` line for a facility-covered closure member.

    Named units import by name (``std::builtins::Int``); nameless ones
    (extern blocks, extra blocks) fall back to their module wildcard —
    their members are reachable only through the module surface.
    ImplDecl is deliberately silent: method resolution in the child
    compile rides the restricted trait-impl registry, and an impl block
    exports no importable name.
    """
    if isinstance(unit, ImplDecl):
        return
    mp = tuple(getattr(unit, "source_module_path", None) or ())
    if not mp:
        return
    mod = "::".join(mp)
    name = getattr(unit, "name", None)
    if isinstance(name, str) and name:
        imports.add(f"{mod}::{name}")
    else:
        imports.add(f"{mod}::*")

_IDENT_RE = re.compile(r"[A-Za-z_]\w*")
_TYPE_KEYWORDS = frozenset({"fn", "const", "mut", "self"})
# Lowercase scalar aliases are ``pub typedef`` in std::builtins
# (libs/builtins/mod.wind: i32/usize/f64/...).
_BUILTIN_ALIAS_RE = re.compile(r"^(?:[iu](?:size|8|16|32|64)|f(?:32|64))$")

# BUILTIN_TYPES spellings that are keywords/SA-internal rather than
# importable std::builtins declarations.
_NOT_IMPORTABLE = frozenset({"fn", "!", "*const", "*mut", "Fn"})


def _builtin_import(name: str) -> Optional[str]:
    """Precise std import for a builtin spelling absent from the index.

    A real ``cwindf`` compile flattens std into the program, so the
    provides index already carries every facility name (recorded above).
    Pure in-memory harnesses (``run_sa_with_errors`` over a single file)
    never load std, so their index misses the builtin surface entirely —
    without this fallback the child unit would render with no imports
    and fail on ``Unknown type 'i32'``.  Only genuinely declared builtin
    spellings qualify, so a typo still fails at its use site.
    """
    if _BUILTIN_ALIAS_RE.match(name):
        return f"std::builtins::{name}"
    if name in BUILTIN_TYPES and name not in _NOT_IMPORTABLE:
        return f"std::builtins::{name}"
    return None


@dataclass
class WrapperSpec:
    """The ``#[export]`` free function bridging one call site."""

    callee: Node                      # live AST Name / Attribute
    arg_types: list[dict]             # ann.type dicts (receiver first)
    ret_type: Optional[dict]          # ann.type dict of the result
    mod_path: tuple[str, ...] = ()    # enclosing inline-mod path of callee


@dataclass
class EvaluationUnit:
    """A rendered, compilable unit keyed by its exact source text."""

    key: str
    program_text: str
    wrapper: WrapperSpec
    array_ret: bool = False


def type_spelling(info: Optional[dict]) -> str:
    """``ann.type`` dict -> source type spelling (``[u8; 4]``, ``&T``...)."""
    if not isinstance(info, dict):
        return "?"
    name = str(info.get("name") or "?")
    args = info.get("args")
    if isinstance(args, list) and args:
        name = (
            name
            + "<"
            + ", ".join(type_spelling(a) for a in args)
            + ">"
        )
    if info.get("ref"):
        return ("&mut " if info.get("mut") else "&") + name
    return name


def type_names(info: Optional[dict]) -> set[str]:
    """Identifier seeds hidden inside one annotated type spelling."""
    text = type_spelling(info)
    return {
        w for w in _IDENT_RE.findall(text) if w not in _TYPE_KEYWORDS
    }


class _Index:
    """Unit + provides indexes over the live program (AST level)."""

    def __init__(self, program: Program) -> None:
        self.units: list[Node] = []            # emission order
        self.unit_of: dict[int, Node] = {}     # node tid -> owning unit
        self.provides: dict[str, list[Node]] = {}
        self.mod_path: dict[int, tuple[str, ...]] = {}  # unit tid -> mod path
        self.parent: dict[int, Node] = {}      # mod-stmt unit -> ModDecl
        self._scan(program.items, ())
        # Provides after unit_of is complete so an inner method maps to
        # its enclosing extra/impl/extern/mod-stmt unit.
        for unit in self.units:
            for node in _iter_subtree(unit):
                tid = getattr(node, "_typed_id", None)
                if tid is None:
                    continue
                owner = self.unit_of.get(tid)
                if owner is None:
                    continue
                for spelling in _syntactic_provides(node):
                    bucket = self.provides.setdefault(spelling, [])
                    if not any(u is owner for u in bucket):
                        bucket.append(owner)

    def _scan(
        self,
        items: list[Node],
        path: tuple[str, ...],
        parent: Optional[Node] = None,
    ) -> None:
        for item in items:
            self.units.append(item)
            tid = getattr(item, "_typed_id", None)
            if tid is not None:
                self.mod_path[tid] = path
                if parent is not None:
                    self.parent[tid] = parent
            for node in _iter_subtree(item):
                nt = getattr(node, "_typed_id", None)
                if nt is not None:
                    self.unit_of[nt] = item
            if isinstance(item, ModDecl):
                body = getattr(item, "body", None)
                stmts = getattr(body, "stmts", None) if body else None
                if stmts:
                    name = str(getattr(item, "name", "") or "")
                    # Innermost wins: the recursive scan re-registers the
                    # stmt subtrees with the deeper mod path.
                    self._scan(list(stmts), (*path, name), parent=item)

    def owner(self, tid: Optional[int]) -> Optional[Node]:
        if tid is None:
            return None
        return self.unit_of.get(tid)

    def mod_path_of(self, unit: Node) -> tuple[str, ...]:
        tid = getattr(unit, "_typed_id", None)
        if tid is None:
            return ()
        return self.mod_path.get(tid, ())


def _iter_subtree(node: Node):
    from dataclasses import fields as _dc_fields

    yield node
    for f in _dc_fields(node):
        if f.name in ("line", "column"):
            continue
        value = getattr(node, f.name, None)
        if isinstance(value, Node):
            yield from _iter_subtree(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, Node):
                    yield from _iter_subtree(item)


def _fn_of_binding(
    analyzer: "_Analyzer", ref: object, kind: str
) -> Optional[FnDecl]:
    """Resolve an ann ref to its FnDecl through the RIGHT id space.

    Method bindings and FnDecl nodes live in different id spaces; a
    binding id can collide with an unrelated node id, so ``method`` must
    consult only the method-binding table and ``fn`` only the fn table.
    """
    if not isinstance(ref, int):
        return None
    fn_by_id, bind_by_id = analyzer._const_fn_tables()
    table = bind_by_id if kind == "method" else fn_by_id
    target = table.get(ref)
    return target if isinstance(target, FnDecl) else None


def _method_bindings(analyzer: "_Analyzer") -> dict[str, dict[int, object]]:
    """``binding id -> binding`` + ``fn id -> binding``, cached per SA."""
    cache = getattr(analyzer, "_constfn_method_index", None)
    if cache is None:
        by_ref: dict[int, object] = {}
        by_fn: dict[int, object] = {}
        for lst in getattr(analyzer, "methods", {}).values():
            for b in lst:
                bid = getattr(b, "id", None)
                if isinstance(bid, int):
                    by_ref.setdefault(bid, b)
                fn = getattr(b, "fn", None)
                tid = getattr(fn, "_typed_id", None) if fn else None
                if isinstance(tid, int):
                    by_fn.setdefault(tid, b)
        cache = {"ref": by_ref, "fn": by_fn}
        analyzer._constfn_method_index = cache  # type: ignore[attr-defined]
    return cache


def _method_import(analyzer: "_Analyzer", ref: int) -> Optional[str]:
    """The import a consumed *method* needs in the child compile.

    Methods cannot be named in ``use`` (``use std::builtins::String::
    length`` is a syntax error), and the child resolves them from the
    declaring surface: an extern method decl needs its block's module
    wildcard, a trait method needs the trait by name (the no-std
    trait-impl registry then rides along), an inherent impl needs its
    module wildcard.  Provenance comes from ``binding.decl`` — the
    method FnDecl itself carries no module path.
    """
    binding = _method_bindings(analyzer)["ref"].get(ref)
    if binding is None:
        return None
    decl = getattr(binding, "decl", None)
    path = list(getattr(decl, "source_module_path", None) or [])
    if not path:
        return None
    mod = "::".join(path)
    if isinstance(decl, TraitDecl):
        name = getattr(decl, "name", None)
        if isinstance(name, str) and name:
            return f"{mod}::{name}"
    return f"{mod}::*"


def _method_import_for_callee(
    analyzer: "_Analyzer", callee_fn: FnDecl
) -> Optional[int]:
    """Binding ref when *callee_fn* was reached as a method binding."""
    tid = getattr(callee_fn, "_typed_id", None)
    if not isinstance(tid, int):
        return None
    b = _method_bindings(analyzer)["fn"].get(tid)
    bid = getattr(b, "id", None) if b is not None else None
    return bid if isinstance(bid, int) else None


def _collect_refs(
    analyzer: "_Analyzer",
    node: Node,
    ids: set[int],
    names: set[str],
    methods: set[int],
) -> None:
    """Ann-aware reference collection for one closure member.

    Resolved names contribute their *exact* declaration id (binding /
    call annotations); only unannotated spellings (types, unresolved
    paths) fall back to the provides index.  Method bindings also land
    in *methods* so the worklist can route their import surface.
    """

    def visit(n: Node) -> None:
        if isinstance(n, Call):
            ann = n._typed_ann.get("call")
            if isinstance(ann, dict):
                kind = ann.get("callee_kind")
                for t in (ann.get("type_args") or {}).values():
                    names.update(type_names(t))
                if kind == "fn":
                    target = _fn_of_binding(
                        analyzer, ann.get("callee_ref"), "fn"
                    )
                    if target is not None and target._typed_id is not None:
                        ids.add(target._typed_id)
                        callee = n.callee
                        if isinstance(callee, Attribute):
                            visit(callee.obj)
                        for a in n.args:
                            visit(a)
                        return
                elif kind == "method":
                    target = _fn_of_binding(
                        analyzer, ann.get("callee_ref"), "method"
                    )
                    if target is not None and target._typed_id is not None:
                        ids.add(target._typed_id)
                        if isinstance(ann.get("callee_ref"), int):
                            methods.add(ann["callee_ref"])
                        callee = n.callee
                        if isinstance(callee, Attribute):
                            visit(callee.obj)
                        for a in n.args:
                            visit(a)
                        return
                elif kind == "enum_variant":
                    callee = n.callee
                    if isinstance(callee, Name):
                        b = callee._typed_ann.get("binding") or {}
                        if isinstance(b.get("ref"), int):
                            ids.add(b["ref"])
                    if isinstance(callee, Attribute):
                        visit(callee.obj)
                    for a in n.args:
                        visit(a)
                    return
            # indirect / trait_fn / builtin / unannotated: generic walk
        elif isinstance(n, Name):
            b = n._typed_ann.get("binding") or {}
            kind = b.get("kind")
            ref = b.get("ref")
            if kind == "method":
                target = _fn_of_binding(analyzer, ref, "method")
                if target is not None and target._typed_id is not None:
                    ids.add(target._typed_id)
                    if isinstance(ref, int):
                        methods.add(ref)
                    return
            elif kind in (
                "fn", "const", "assoc_const", "variant", "field",
                "extern_static",
            ):
                if isinstance(ref, int):
                    ids.add(ref)
                    return
            if kind in ("var", "param", "builtin"):
                return  # locals / None: no dependency edge
            for part in n.parts:
                if part:
                    names.add(part)
            return
        elif isinstance(n, Attribute):
            member = n._typed_ann.get("member")
            if isinstance(member, dict) and isinstance(
                member.get("ref"), int
            ):
                if member.get("kind") == "method":
                    # method member refs live in the binding id space
                    target = _fn_of_binding(
                        analyzer, member["ref"], "method"
                    )
                    if target is not None and target._typed_id is not None:
                        ids.add(target._typed_id)
                        methods.add(member["ref"])
                        visit(n.obj)
                        return
                else:
                    # field member refs are node ids
                    ids.add(member["ref"])
                    visit(n.obj)
                    return
            binding = n._typed_ann.get("binding")
            if not isinstance(binding, dict):
                binding = {}
            ref = binding.get("ref")
            if isinstance(ref, int) and binding.get("kind") in (
                "method", "field",
            ):
                if binding.get("kind") == "method":
                    target = _fn_of_binding(analyzer, ref, "method")
                    if target is not None and target._typed_id is not None:
                        ids.add(target._typed_id)
                        visit(n.obj)
                        return
                else:
                    ids.add(ref)
                    visit(n.obj)
                    return
            if n.name:
                names.add(n.name)
            visit(n.obj)
            return
        elif isinstance(n, Type):
            if n.name:
                names.add(n.name)
                base = n.name.split("<", 1)[0].rsplit("::", 1)[-1]
                if base:
                    names.add(base)
            for arg in n.args:
                visit(arg)
            return
        from dataclasses import fields as _dc_fields

        for f in _dc_fields(n):
            if f.name in ("line", "column"):
                continue
            value = getattr(n, f.name, None)
            if isinstance(value, Node):
                visit(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, Node):
                        visit(item)

    visit(node)


UNIT_VERSION = 1
UNIT_FN = "cw_const_eval"
ARRAY_WRAP = "__cw_const_arr"


def closure_units(
    analyzer: "_Analyzer", program: Program, callee: FnDecl, seeds: set[str]
) -> tuple[list[Node], set[str]]:
    """Emission-ordered closure units + precise facility imports.

    Worklist over two queues — exact declaration ids (from ann bindings)
    and name spellings (types / unannotated paths) — until neither queue
    yields a new unit.  Adding a unit also pulls its enclosing ``mod`` (so
    a pulled statement always renders inside its parent) and caches that
    unit's references exactly once.  Facility-covered names/units never
    enter the closure (their declarations come from the real std files
    the child compile loads); each one instead records exactly one
    ``use <mod>::<name>;`` so every spelling the rendered body still
    carries resolves without the blanket wildcard surface.
    """
    from collections import deque

    index = _Index(program)
    closure: dict[int, Node] = {}
    imports: set[str] = set()
    methods: set[int] = set()
    id_q: deque[int] = deque()
    name_q: deque[str] = deque(sorted(seeds))
    seen_ids: set[int] = set()
    seen_names: set[str] = set()
    refs_done: set[int] = set()

    def add(unit: Optional[Node]) -> None:
        if unit is None:
            return
        tid = getattr(unit, "_typed_id", None)
        if tid is None:
            return
        covered = _facility_covered(unit)
        if covered:
            _record_import(unit, imports)
        if not covered and tid not in closure:
            parent = index.parent.get(tid)
            if parent is not None:
                add(parent)  # a pulled mod-statement renders via its mod
            closure[tid] = unit
        if tid in refs_done:
            return
        # Facility-covered units are not emitted but still contribute
        # references: the callee may live in std while calling into
        # non-facility std (panic, result, ...).
        refs_done.add(tid)
        ids: set[int] = set()
        names: set[str] = set()
        for node in _iter_subtree(unit):
            nt = getattr(node, "_typed_id", None)
            if nt is not None:
                id_q.append(nt)
            _collect_refs(analyzer, node, ids, names, methods)
        id_q.extend(ids)
        name_q.extend(names)

    add(index.owner(getattr(callee, "_typed_id", None)))
    # The callee itself may be a method (``String::length``): its
    # declaring surface must ride along even when no body node sits in
    # the closure (the std index may be absent for in-memory sources).
    callee_method = _method_import_for_callee(analyzer, callee)
    if callee_method is not None:
        methods.add(callee_method)
    while id_q or name_q:
        while id_q:
            tid = id_q.popleft()
            if tid in seen_ids:
                continue
            seen_ids.add(tid)
            add(index.owner(tid))
        if not name_q:
            continue
        name = name_q.popleft()
        if name in seen_names:
            continue
        seen_names.add(name)
        if len(name) == 1 and name.isupper():
            # Generic parameter names leak out of signatures
            # (``Vector<T>`` owner args, ``fn f<T>()``): nothing at top
            # level is legitimately named by a lone capital letter.
            continue
        providers = index.provides.get(name, ())
        if any(_facility_covered(u) for u in providers):
            # Facility imports already expose this name (traits, types,
            # options...): pulling the *implementors* the provides index
            # also maps here would drag the whole trait universe in.
            # Record the precise import instead.
            for u in providers:
                if _facility_covered(u) and not isinstance(u, ImplDecl):
                    path = list(
                        getattr(u, "source_module_path", None) or []
                    )
                    if path:
                        imports.add("::".join(path + [name]))
            continue
        if not providers:
            # In-memory sources carry no std items: fall back to the
            # builtin declaration surface (std::builtins).
            imp = _builtin_import(name)
            if imp:
                imports.add(imp)
        for unit in providers:
            add(unit)
    # Method surface: a method whose owning unit is NOT emitted here
    # (std index absent, or facility-covered) must be resolvable in the
    # child from its declaring module — record that import.
    for mref in sorted(methods):
        binding = _method_bindings(analyzer)["ref"].get(mref)
        if binding is None:
            continue
        fn = getattr(binding, "fn", None)
        owner = index.owner(getattr(fn, "_typed_id", None))
        if (
            owner is not None
            and not _facility_covered(owner)
            and getattr(owner, "_typed_id", None) in closure
        ):
            continue  # 方法源已随闭包子编译自带
        imp = _method_import(analyzer, mref)
        if imp:
            imports.add(imp)
    top = {id(u) for u in getattr(program, "items", []) or []}
    # Mod statements render through their (closure) ModDecl; only items
    # that are direct program members are emitted on their own.
    ordered = [
        u for u in index.units
        if id(u) in top and getattr(u, "_typed_id", None) in closure
    ]
    return ordered, imports


def _strip_markers(node) -> None:
    """Drop ``const fn`` / ``const type`` markers from a serialized copy.

    The unit recompiles no-std: ``const fn`` targets become ordinary
    functions (their const validation already happened in the outer
    compile), and ``const type`` marks are re-registered by the *real*
    std files the facility imports load — the copies inside the unit
    live in a temp file that is not std-tagged.
    """
    if isinstance(node, dict):
        kind = node.get("kind")
        if kind == "FnDecl":
            node.pop("const_fn", None)
        elif kind == "TypeDecl":
            node.pop("const_type", None)
        for value in node.values():
            _strip_markers(value)
    elif isinstance(node, list):
        for value in node:
            _strip_markers(value)


def _enclosing_mod_path(program: Program, target: FnDecl) -> tuple[str, ...]:
    """Inline-mod path wrapping *target* (``("m",)`` for a fn in ``mod m``)."""
    target_id = getattr(target, "_typed_id", None)
    if target_id is None:
        return ()

    def search(
        items: list[Node], path: tuple[str, ...]
    ) -> Optional[tuple[str, ...]]:
        for item in items:
            if any(
                getattr(n, "_typed_id", None) == target_id
                for n in _iter_subtree(item)
            ):
                if isinstance(item, ModDecl):
                    name = str(getattr(item, "name", "") or "")
                    return (*path, name)
                return path
            if isinstance(item, ModDecl):
                body = getattr(item, "body", None)
                stmts = getattr(body, "stmts", None) if body else None
                if stmts:
                    name = str(getattr(item, "name", "") or "")
                    found = search(list(stmts), (*path, name))
                    if found is not None:
                        return found
        return None

    return search(list(getattr(program, "items", []) or []), ()) or ()


def _wrapper_source(
    wrapper: WrapperSpec, callee_name: Optional[str] = None
) -> str:
    """The ``#[export]`` bridge for one call signature.

    Method callees bind their receiver to ``a0`` (with the attribute
    chain between receiver and method rendered after it); path callees
    keep their spelling, qualified by the enclosing inline mod when the
    reference itself was a bare same-mod name.  *callee_name* overrides
    the callee's last path segment when the closure renamed a fn that
    collided with the export symbol (see :func:`_rename_shadowed`).
    """
    n_args = len(wrapper.arg_types)
    params = ", ".join(
        f"a{i}: {type_spelling(t)}" for i, t in enumerate(wrapper.arg_types)
    )
    ret_text = (
        "" if wrapper.ret_type is None
        else f" -> {type_spelling(wrapper.ret_type)}"
    )
    callee = wrapper.callee
    if isinstance(callee, Attribute):
        # The method receiver IS ``callee.obj`` (whatever its shape:
        # name, index, attribute chain) — it arrives whole as ``a0``,
        # so the wrapper only appends the method name.
        rest = ", ".join(f"a{i}" for i in range(1, n_args))
        call = f"a0.{callee.name}({rest})"
    else:
        parts = [str(p) for p in (getattr(callee, "parts", None) or ["?"]) if p]
        if callee_name is not None:
            if parts:
                parts[-1] = callee_name
            else:
                parts = [callee_name]
        if len(parts) == 1 and wrapper.mod_path:
            parts = [*wrapper.mod_path, *parts]
        args = ", ".join(f"a{i}" for i in range(n_args))
        call = f"{'::'.join(parts)}({args})"
    if wrapper.ret_type is not None and _is_array_type(
        type_spelling(wrapper.ret_type)
    ):
        spelling = type_spelling(wrapper.ret_type)
        return (
            f"struct {ARRAY_WRAP} {{ v: {spelling} }}\n"
            f"#[export]\nfn {UNIT_FN}({params}) -> {ARRAY_WRAP} {{ "
            f"return {ARRAY_WRAP} {{ {call} }}; }}"
        )
    return f"#[export]\nfn {UNIT_FN}({params}){ret_text} {{ return {call}; }}"


def _is_array_type(spelling: str) -> bool:
    return spelling.startswith("[")


def _rename_shadowed(items: list[dict], callee_fn: FnDecl) -> Optional[str]:
    """Rename a closure fn that collides with the export symbol.

    The procedure macro solves the same problem with
    ``deps._macro_fn_name``: the generated program may legitimately
    declare ``cw_const_eval`` (a user fn of that exact name), and the
    child compile would then see two top-level definitions — its own
    ``#[export]`` wrapper and the closure copy — and fail with
    ``Duplicate definition``.  The wrapper name must stay fixed (ctypes
    looks the DLL symbol up by ``UNIT_FN``), so the *closure* side is
    renamed instead: the decl plus every fn-bound reference to it.
    Returns the callee's new name when the callee itself moved.
    """
    hits = [
        it for it in items
        if it.get("kind") == "FnDecl" and it.get("name") == UNIT_FN
    ]
    if not hits:
        return None
    occupied: set[str] = set()

    def collect(node: object) -> None:
        if isinstance(node, dict):
            nm = node.get("name")
            if isinstance(nm, str):
                occupied.add(nm)
            parts = node.get("parts")
            if isinstance(parts, list):
                occupied.update(p for p in parts if isinstance(p, str))
            for v in node.values():
                collect(v)
        elif isinstance(node, list):
            for v in node:
                collect(v)

    for it in items:
        collect(it)
    base = "__cwc_callee"
    new = base
    counter = 2
    while new in occupied:
        new = f"{base}_{counter}"
        counter += 1
    hit_ids: set[int] = set()
    callee_tid = getattr(callee_fn, "_typed_id", None)
    for it in hits:
        it["name"] = new
        iid = it.get("id")
        if isinstance(iid, int):
            hit_ids.add(iid)

    def rebind(node: object) -> None:
        if isinstance(node, dict):
            if node.get("kind") == "Name":
                ann = node.get("ann")
                b = ann.get("binding") if isinstance(ann, dict) else None
                if (
                    isinstance(b, dict)
                    and b.get("kind") == "fn"
                    and b.get("ref") in hit_ids
                ):
                    parts = node.get("parts")
                    if isinstance(parts, list) and parts:
                        parts[-1] = new
            for v in node.values():
                rebind(v)
        elif isinstance(node, list):
            for v in node:
                rebind(v)

    for it in items:
        rebind(it)
    return new if callee_tid in hit_ids else None


def build_unit_source(
    analyzer: "_Analyzer",
    program: Program,
    callee_fn: FnDecl,
    wrapper: WrapperSpec,
) -> EvaluationUnit:
    """Render the evaluation unit (closure + wrapper) for one call."""
    seeds: set[str] = set()
    for t in wrapper.arg_types:
        seeds |= type_names(t)
    if wrapper.ret_type is not None:
        seeds |= type_names(wrapper.ret_type)
    units, facility_imports = closure_units(
        analyzer, program, callee_fn, seeds
    )
    closure_tids = {getattr(u, "_typed_id", None) for u in units}
    items: list[dict] = []
    for unit in units:
        if isinstance(unit, ModDecl):
            body = getattr(unit, "body", None)
            stmts = list(getattr(body, "stmts", None) or [])
            kept = [
                s for s in stmts
                if getattr(s, "_typed_id", None) in closure_tids
            ]
            data = unit.to_dict(include_meta=True)
            if isinstance(data.get("body"), dict):
                data["body"]["stmts"] = [
                    s.to_dict(include_meta=True) for s in kept
                ]
        else:
            data = unit.to_dict(include_meta=True)
        _strip_markers(data)
        items.append(data)
    if not isinstance(wrapper.callee, Attribute):
        wrapper.mod_path = _enclosing_mod_path(program, callee_fn)
    renamed = _rename_shadowed(items, callee_fn)
    rendered = render_program(
        {"kind": "Program", "line": 1, "column": 1, "items": items}
    )
    use_lines = [f"use {imp};" for imp in sorted(facility_imports)]
    text = "// CWind const-fn evaluation unit (generated)\n"
    if use_lines:
        text += "\n".join(use_lines) + "\n\n"
    text += rendered
    text += ("\n" if rendered else "") + _wrapper_source(
        wrapper, callee_name=renamed
    ) + "\n"
    import hashlib

    key = hashlib.sha256(
        f"cwind-constfn-v{UNIT_VERSION}\0".encode() + text.encode("utf-8")
    ).hexdigest()[:32]
    ret = wrapper.ret_type
    return EvaluationUnit(
        key=key,
        program_text=text,
        wrapper=wrapper,
        array_ret=ret is not None and _is_array_type(type_spelling(ret)),
    )
