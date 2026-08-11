"""Grammar-based fuzzing tool for the CWind frontend's semantic analyzer (SA).

The main route is ``--mode gen``: a rule-based generator composes small
feature snippets (struct/extra/impl/trait/typedef/group/...), each valid by
construction, into complete programs.  Because every generated program is
meant to be semantically valid, any SA error is a *false-positive candidate*
and any exception is an SA crash.  The generator itself is kept honest: if it
produces lex/parse errors those are reported as generator bugs.

Secondary routes:

* ``--mode mutate``  conservative text mutations over known-good seeds
                     (currently on hold; useful once a seed corpus exists).
* ``--mode corpus``  run SA over every ``*.wind`` under a directory.

Every interesting case (crash / SA error / generator bug) is written to the
output directory together with a JSON report, so failures can be replayed and
minimized.

Usage::

    python fuzz/fuzz_sa.py --mode gen    --count 200000 --seed 1 --jobs 8 \
        --report-every 100
    python fuzz/fuzz_sa.py --mode corpus --dir assets --jobs 8

Analysis is parallelized with a thread pool (``--jobs`` defaults to
``min(32, cpu_count + 4)``); the free-threaded build (``python3.13t.exe``)
runs the workers without a GIL, but the GIL build benefits too because the
per-case pipeline is mostly independent.

Known SA bugs (error-message patterns) are listed in ``known_bugs.json`` next
to this file; matched cases are counted separately instead of flooding the
false-positive report.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import pathlib
import random
import re
import sys
import time
import traceback
from dataclasses import dataclass, field
from typing import Optional

ROOT = pathlib.Path(__file__).resolve().parent
REPO = ROOT.parent
SRC = REPO / "frontend" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from cwind_frontend.ast_components.token import TokenKind  # noqa: E402
from cwind_frontend.lexer import Lexer  # noqa: E402
from cwind_frontend.parser import parse_with_errors  # noqa: E402
from cwind_frontend.sa import run_sa_with_errors  # noqa: E402


# 
# Small utilities
# 


def tokenize(src: str):
    lex = Lexer()
    toks = []
    for line in src.splitlines():
        toks.extend(lex.feed_line(line))
    toks.extend(lex.eof())
    return lex, toks


def analyze(src: str):
    """Run lex -> parse -> SA; return a plain dict describing the result."""
    lex, toks = tokenize(src)
    if lex.errors:
        return {
            "kind": "lex_err",
            "lex_errors": [e.message for e in lex.errors],
            "parse_errors": [],
            "sa_errors": [],
            "traceback": "",
        }
    presult = parse_with_errors(toks)
    if presult.errors:
        return {
            "kind": "parse_err",
            "lex_errors": [],
            "parse_errors": [e.message for e in presult.errors],
            "sa_errors": [],
            "traceback": "",
        }
    try:
        sresult = run_sa_with_errors(presult.program)
    except Exception:
        return {
            "kind": "crash",
            "lex_errors": [],
            "parse_errors": [],
            "sa_errors": [],
            "traceback": traceback.format_exc(),
        }
    return {
        "kind": "sa_err" if sresult.errors else "clean",
        "lex_errors": [],
        "parse_errors": [],
        "sa_errors": [
            {"line": e.line, "column": e.column, "message": e.message}
            for e in sresult.errors
        ],
        "traceback": "",
    }


def sig_of(result: dict) -> tuple[str, ...]:
    """A stable, order-insensitive signature for deduplication."""
    return tuple(sorted(e["message"] for e in result["sa_errors"]))


# 
# Conservative, semantics-preserving mutation of known-good programs
# 


_DECL_RE = re.compile(
    r"\n(?=(?:pub\s+)?(?:const|type|typedef|struct|enum|trait|impl|extra|group|fn)\b)"
)
_FN_LINE_RE = re.compile(r"\s*(pub\s+)?fn\s+\w")


class Mutator:
    """
    Mutations that should keep a valid program valid.

    Renames are deliberately *not* performed here: renaming a built-in type,
    a method name or a field name breaks semantics, and doing it correctly
    requires context that text mutations do not have.  The generator covers
    name/type variety instead.
    """

    def __init__(self, rng: random.Random):
        self.rng = rng

    def mutate(self, src: str) -> str:
        choice = self.rng.randrange(6)
        if choice == 0:
            return self._reorder_decls(src)
        if choice == 1:
            return self._add_noop_fn(src)
        if choice == 2:
            return self._add_noop_let(src)
        if choice == 3:
            return self._parenthesize(src)
        if choice == 4:
            return self._swap_literals(src)
        return self._toggle_pub(src)

    def _reorder_decls(self, src: str) -> str:
        chunks = _DECL_RE.split(src)
        if len(chunks) < 3:
            return src
        decls = chunks[1:]
        self.rng.shuffle(decls)
        return "\n".join([chunks[0]] + decls)

    def _add_noop_fn(self, src: str) -> str:
        types = self.rng.choice(["Int", "String", "Bool", "Float"])
        lit = {"Int": "1", "String": '"x"', "Bool": "true", "Float": "1.0"}[types]
        name = "fuzzfn" + str(self.rng.randint(10, 999999))
        return (
            f"{src}\n"
            f"fn {name}(a: {types}) -> {types} {{ let b: {types} = a; return b; }}\n"
        )

    def _add_noop_let(self, src: str) -> str:
        lines = src.splitlines(keepends=True)
        fn_lines = [i for i, l in enumerate(lines) if _FN_LINE_RE.match(l)]
        if not fn_lines:
            return src
        j = fn_lines[0]
        while j < len(lines) and "{" not in lines[j]:
            j += 1
        if j >= len(lines):
            return src
        types = self.rng.choice(["Int", "String", "Bool", "Float"])
        lit = {"Int": "1", "String": '"x"', "Bool": "true", "Float": "1.0"}[types]
        lines.insert(
            j + 1, f"    let zz_{self.rng.randint(1, 999999)}: {types} = {lit};\n"
        )
        return "".join(lines)

    def _parenthesize(self, src: str) -> str:
        _, toks = tokenize(src)
        cands = [
            t
            for t in toks
            if t.kind in (TokenKind.INTEGER, TokenKind.FLOAT)
        ]
        if not cands:
            return src
        t = self.rng.choice(cands)
        lines = src.splitlines(keepends=True)
        ln = lines[t.line - 1]
        start, end = t.column - 1, t.column - 1 + len(t.raw)
        lines[t.line - 1] = ln[:start] + "(" + ln[start:end] + ")" + ln[end:]
        return "".join(lines)

    def _swap_literals(self, src: str) -> str:
        _, toks = tokenize(src)
        pool = [t for t in toks if t.kind in (TokenKind.INTEGER, TokenKind.FLOAT)]
        if not pool:
            return src
        t = self.rng.choice(pool)
        lines = src.splitlines(keepends=True)
        ln = lines[t.line - 1]
        start, end = t.column - 1, t.column - 1 + len(t.raw)
        if t.kind == TokenKind.INTEGER:
            new = str(self.rng.randint(0, 100))
        else:
            new = f"{self.rng.randint(1, 99)}.{self.rng.randint(0, 99)}"
        lines[t.line - 1] = ln[:start] + new + ln[end:]
        return "".join(lines)

    def _toggle_pub(self, src: str) -> str:
        m = re.search(r"(?m)^(pub\s+)(const|type|typedef|struct|enum|trait|group|fn)\b", src)
        if m:
            return src[: m.start(1)] + src[m.end(1):]
        m = re.search(r"(?m)^(?=(const|type|typedef|struct|enum|trait|group|fn)\b)", src)
        if m:
            return src[: m.start()] + "pub " + src[m.start():]
        return src


# 
# Valid-by-construction generator
# 


BASE_TYPES = ["Int", "Int8", "UInt", "UInt8", "Float", "String", "Bool", "Byte"]
LIT = {
    "Int": "7",
    "Int8": "3",
    "UInt": "5",
    "UInt8": "2",
    "Float": "1.5",
    "String": '"s"',
    "Bool": "true",
    "Byte": "1",
}
NUMERIC = ["Int", "Int8", "UInt", "UInt8", "Float", "Byte"]


def _uid(prefix: str, used: set[str], rng: random.Random) -> str:
    n = rng.randint(10, 999999)
    while f"{prefix}{n}" in used:
        n = rng.randint(10, 999999)
    used.add(f"{prefix}{n}")
    return f"{prefix}{n}"


class Generator:
    """Random generator of programs that are valid by construction.

    Each feature is produced by its own ``gen_*`` method; a program is a
    concatenation of several feature snippets with globally unique names.
    """

    def __init__(self, rng: random.Random):
        self.rng = rng
        self.used: set[str] = set()

    def name(self, prefix: str) -> str:
        return _uid(prefix, self.used, self.rng)

    def pick(self, xs: list[str]) -> str:
        return self.rng.choice(xs)

    # -- expressions and statements 

    def expr(self, env: dict[str, str], want: Optional[str] = None):
        if want is not None:
            if want in LIT:
                return LIT[want], want
            vars_ = [v for v, t in env.items() if t == want]
            if vars_:
                return self.rng.choice(vars_), want
            if want == "Bool":
                return self.rng.choice(["true", "false"]), "Bool"
            if want == "String":
                return '"zz"', "String"
            if want in NUMERIC:
                return "9", want
            return None
        k = self.rng.randrange(8)
        if k == 0:
            t = self.pick(BASE_TYPES)
            return LIT[t], t
        if k == 1:
            if env:
                v = self.rng.choice(list(env.items()))
                return v[0], v[1]
            return self.expr(env, "Int")
        if k == 2:
            t = self.pick(NUMERIC)
            a, _ = self.expr(env, t)
            b, _ = self.expr(env, t)
            return f"({a} + {b})", t
        if k == 3:
            a, _ = self.expr(env, "Bool")
            b, _ = self.expr(env, "Bool")
            return f"({a} && {b})", "Bool"
        if k == 4:
            a, _ = self.expr(env, "Int")
            b, _ = self.expr(env, "Int")
            return f"({a} > {b})", "Bool"
        if k == 5:
            a, _ = self.expr(env, "Int")
            b, _ = self.expr(env, "Int")
            return f"({a} << 1)", "Int"
        if k == 6:
            a, _ = self.expr(env, "Int")
            return f"(-{a})", "Int"
        a, _ = self.expr(env, "Bool")
        return f"(!{a})", "Bool"

    def stmts(self, env: dict[str, str], ret_type: str) -> list[str]:
        out: list[str] = []
        for _ in range(self.rng.randint(1, 4)):
            k = self.rng.randrange(7)
            if k == 0:
                t = self.pick(BASE_TYPES)
                v, _ = self.expr(env, t)
                nm = self.name("v")
                env[nm] = t
                out.append(f"let {nm}: {t} = {v};")
            elif k == 1 and env:
                v = self.rng.choice(list(env.items()))
                e, _ = self.expr(env, v[1])
                out.append(f"{v[0]} = {e};")
            elif k == 2 and ret_type != "None":
                e, _ = self.expr(env, ret_type)
                out.append(f"return {e};")
            elif k == 3:
                c, _ = self.expr(env, "Bool")
                body, _ = self.block(env, ret_type)
                out.append(f"if ({c}) {body}")
            elif k == 4:
                c, _ = self.expr(env, "Bool")
                body, _ = self.block(env, ret_type)
                out.append(f"while ({c}) {body}")
            elif k == 5:
                e, _ = self.expr(env, "String")
                out.append(f"builtins::print({e});")
            else:
                e, _ = self.expr(env)
                out.append(f"{e};")
        return out

    def block(self, env: dict[str, str], ret_type: str):
        sub = dict(env)
        body = self.stmts(sub, ret_type)
        return "{ " + " ".join(body) + " }", sub

    def fn_wrapper(
        self,
        params: list[tuple[str, str]],
        ret_type: str,
        body_stmts: list[str],
        name: Optional[str] = None,
    ) -> str:
        n = name or self.name("f")
        ps = ", ".join(f"{pn}: {pt}" for pn, pt in params)
        stmts = list(body_stmts)
        if ret_type != "None":
            stmts.append(f"return {LIT[ret_type]};")
        return f"fn {n}({ps}) -> {ret_type} {{ {' '.join(stmts)} }}"

    # -- top-level feature snippets 

    def gen_const(self) -> str:
        t = self.pick(BASE_TYPES)
        return f"const {self.name('c')}: {t} = {LIT[t]};"

    def gen_type(self) -> str:
        t = self.pick(NUMERIC)
        cond = {
            "Int": "self > 0",
            "Int8": "self > -1",
            "UInt": "self > 0",
            "UInt8": "self != 0",
            "Float": "self > 0.0",
            "Byte": "self > 0",
        }[t]
        return f"type {self.name('Ty')} = {t} where {{ {cond}; }}"

    def gen_type_string(self) -> str:
        ty = self.name("Email")
        e = self.name("e")
        return (
            f"type {ty} = String where {{ self.length >= 1; self.matches(\"x\"); }}\n"
            + self.fn_wrapper([(e, ty)], "Bool", [f"return {e}.length() == 1;"])
        )

    def gen_typedef(self) -> str:
        k = self.rng.randrange(3)
        if k == 0:
            return f"typedef {self.name('Alias')} = Map<String, Int>;"
        if k == 1:
            return f"typedef {self.name('Alias')} = Vector<String>;"
        return f"typedef {self.name('Alias')} = Int;"

    def gen_typedef_generic(self) -> str:
        a = self.name("DM")
        fn = self.fn_wrapper(
            [],
            "Bool",
            [
                f"let m: {a}<String, Int, Float> = {{}};",
                "let inner: Map<Int, Float> = {};",
                'm.set("a", inner);',
                "let v: Map<Int, Float> = m.get(\"a\");",
                'return m.contains("a");',
            ],
        )
        return f"typedef {a}<K, T, V> = Map<K, Map<T, V>>;\n{fn}"

    def gen_typedef_vector_alias(self) -> str:
        a = self.name("SV")
        fn = self.fn_wrapper(
            [],
            "String",
            [
                f"let v: {a} = [];",
                'v.push_back("x");',
                "return v[0];",
            ],
        )
        return f"typedef {a} = Vector<String>;\n{fn}"

    def gen_enum(self) -> str:
        n = self.name("E")
        fn = self.fn_wrapper(
            [],
            "Bool",
            [
                f"let g: {n} = {n}::A;",
                f"g = {n}::B;",
                f"return g == {n}::B;",
            ],
        )
        return f"enum {n} {{ A = 1, B = 2, }}\n{fn}"

    def gen_struct(self) -> str:
        n = self.name("S")
        fields: list[tuple[str, str, bool]] = []
        for i in range(self.rng.randint(0, 2)):
            fields.append((f"f{i}", self.pick(BASE_TYPES), False))
        if self.rng.random() < 0.4:
            fields.append(("count", "Int", True))
        args = ", ".join(LIT[t] for _, t, static in fields if not static)
        field_src = ", ".join(
            f"static {nm}: {t} = 0" if st else f"pub {nm}: {t}"
            for nm, t, st in fields
        )
        fn = self.fn_wrapper(
            [], "Int", [f"let s: {n} = {n} {{ {args} }};", "return 0;"]
        )
        return f"struct {n} {{ {field_src} }}\n{fn}"

    def gen_struct_unit(self) -> str:
        n = self.name("S")
        fn = self.fn_wrapper([], "Int", [f"let s: {n} = {n} {{ }};", "return 0;"])
        return f"struct {n};\n{fn}"

    def gen_struct_generic(self) -> str:
        n = self.name("S")
        fn = self.fn_wrapper(
            [],
            "Int",
            [f'let s: {n}<String> = {n}<String> {{ "x", 1 }};', "return 0;"],
        )
        return f"struct {n}<T> {{ pub x: T, pub y: Int }}\n{fn}"

    def gen_struct_generic_field(self) -> str:
        """Generic field read/write at a call site (known SA-bug family)."""
        n = self.name("S")
        b = self.name("b")
        fn = self.fn_wrapper(
            [(b, f"{n}<String>")],
            "String",
            [f"let y: String = {b}.x;", f'{b}.x = "z";', "return y;"],
        )
        return f"struct {n}<T> {{ pub x: T }}\n{fn}"

    def gen_extra(self) -> str:
        s = self.name("S")
        struct = f"struct {s} {{ pub x: Int }}"
        extra = (
            f"extra {s} {{ fn m0(self) -> Int {{ return self.x; }} "
            "fn m1(self) -> None { } }"
        )
        fn = self.fn_wrapper(
            [],
            "Int",
            [
                f"let s: {s} = {s} {{ 1 }};",
                "let v: Int = s.m0();",
                "s.m1();",
                "return v;",
            ],
        )
        return struct + "\n" + extra + "\n" + fn

    def gen_extra_generic(self) -> str:
        """Generic extra methods called at a call site (known SA-bug family)."""
        s = self.name("S")
        struct = f"struct {s}<T> {{ pub x: T }}"
        extra = (
            f"extra<T> {s}<T> {{ fn m(self) -> T {{ return self.x; }} "
            "fn set(self, v: T) -> None { } }"
        )
        fn = self.fn_wrapper(
            [],
            "String",
            [
                f'let s: {s}<String> = {s}<String> {{ "a" }};',
                's.set("b");',
                "return s.m();",
            ],
        )
        return struct + "\n" + extra + "\n" + fn

    def gen_extra_method_generic(self) -> str:
        """Method-level generic parameters (known SA-bug family)."""
        s = self.name("S")
        x = self.name("x")
        return (
            f"struct {s} {{ }}\n"
            f"extra {s} {{ fn id<T>(self, v: T) -> T {{ return v; }} }}\n"
            f"fn {self.name('f')}() -> Int {{ let s: {s} = {s} {{ }}; "
            f"return s.id(1); }}"
        )

    def gen_impl(self) -> str:
        s = self.name("S")
        tr = self.name("T")
        ret = self.pick(["Int", "String", "Bool"])
        trait = f"trait {tr} {{ fn m(self) -> {ret}; }}"
        struct = f"struct {s} {{ }}"
        impl = f"impl {tr} for {s} {{ fn m(self) -> {ret} {{ return {LIT[ret]}; }} }}"
        fn = self.fn_wrapper(
            [], ret, [f"let s: {s} = {s} {{ }};", "return s.m();"]
        )
        return trait + "\n" + struct + "\n" + impl + "\n" + fn

    def gen_impl_generic(self) -> str:
        s = self.name("S")
        tr = self.name("T")
        trait = f"trait {tr}<X> {{ fn m(self, v: X) -> X; }}"
        struct = f"struct {s} {{ }}"
        impl = (
            f"impl {tr}<String> for {s} {{ "
            "fn m(self, v: String) -> String { return v; } }"
        )
        fn = self.fn_wrapper(
            [], "String", [f"let s: {s} = {s} {{ }};", 'return s.m("hi");']
        )
        return trait + "\n" + struct + "\n" + impl + "\n" + fn

    def gen_impl_generic_struct(self) -> str:
        s = self.name("S")
        tr = self.name("T")
        trait = f"trait {tr}<X> {{ fn m(self, v: X) -> X; }}"
        struct = f"struct {s}<T> {{ pub x: T }}"
        impl = (
            f"impl {tr}<String> for {s}<String> {{ "
            "fn m(self, v: String) -> String { return v; } }"
        )
        fn = self.fn_wrapper(
            [], "String", [f'let s: {s}<String> = {s}<String> {{ "a" }};',
            'return s.m("hi");']
        )
        return trait + "\n" + struct + "\n" + impl + "\n" + fn

    def gen_impl_self_return(self) -> str:
        s = self.name("S")
        tr = self.name("T")
        trait = f"trait {tr} {{ fn f(self) -> Self; }}"
        struct = f"struct {s} {{ }}"
        impl = f"impl {tr} for {s} {{ fn f(self) -> Self {{ return self; }} }}"
        fn = (
            f"fn {self.name('f')}() -> {s} {{ "
            f"let s: {s} = {s} {{ }}; "
            f"let y: {s} = s.f(); "
            "return y; }"
        )
        return trait + "\n" + struct + "\n" + impl + "\n" + fn

    def gen_impl_builtin_trait(self) -> str:
        s = self.name("S")
        x = self.name("x")
        struct = f"struct {s} {{ }}"
        impl = f"impl Display for {s} {{ fn to_string(self) -> String {{ return \"x\"; }} }}"
        fn = self.fn_wrapper([(x, s)], "String", [f"return {x}.to_string();"])
        return struct + "\n" + impl + "\n" + fn

    def gen_bound(self) -> str:
        tr = self.name("Named")
        s = self.name("S")
        p = self.name("Point")
        trait = f"trait {tr} {{ fn name(self) -> String; }}"
        struct = f"struct {p} {{ }}"
        impl = f"impl {tr} for {p} {{ fn name(self) -> String {{ return \"p\"; }} }}"
        box = f"struct {s}<T: {tr}> {{ pub v: T }}"
        fn = self.fn_wrapper([(self.name("b"), f"{s}<{p}>")], "None", [])
        return trait + "\n" + struct + "\n" + impl + "\n" + box + "\n" + fn

    def gen_builtin_methods(self) -> str:
        v = self.name("v")
        m = self.name("m")
        s = self.name("s")
        n = self.name("n")
        a = self.name("a")
        b = self.name("b")
        e = self.name("e")
        fn = self.fn_wrapper(
            [(v, "Vector<Int>"), (m, "Map<String, Int>"), (s, "String")],
            "Int",
            [
                f"{v}.push_back(1);",
                f"let {a}: Int = {v}.get(0);",
                f"let {b}: Int = {v}[0];",
                f"let c: Vector<Int> = {v}[0:2];",
                f"let d: Bool = {v}.contains(2);",
                f'{m}.set("a", 1);',
                f'let {e}: Int = {m}.get("a");',
                f'let f: Bool = {m}.contains("a");',
                f"let g: UInt = {s}.length();",
                f'let h: Bool = {s}.matches("x");',
                f"let i: String = {s}.format();",
                f"let j: String = {s}.to_string();",
                "let k: Vector<Int> = Vector::new();",
                "let l: Map<String, Int> = Map::new();",
                f"let {n}: Set<Int> = Set::new();",
                f"let o: Bool = {n}.contains(1);",
                f"let p: Int = {v}.length();",
                f"{v}[0] = 2;",
                f'{m}["a"] = 2;',
                f"return {a} + {b} + {e};",
            ],
        )
        return fn

    def gen_static(self) -> str:
        s = self.name("S")
        inst = self.name("inst")
        struct = f"struct {s} {{ static count: Int = 0, }}"
        extra = (
            f"extra {s} {{ static fn bump() -> None {{ }} "
            f"fn get(self) -> Int {{ return {s}::count; }} }}"
        )
        fn = self.fn_wrapper(
            [(inst, s)],
            "Int",
            [
                f"{s}::count += 1;",
                f"{s}::bump();",
                f"let v: Int = {s}::count;",
                f"let w: Int = {inst}.get();",
                "return v + w;",
            ],
        )
        return struct + "\n" + extra + "\n" + fn

    def gen_which(self) -> str:
        s = self.name("S")
        x = self.name("x")
        extra = (
            f"extra {s} {{ fn a(self) -> None {{ }} "
            "fn b(self) -> None, which ::a { } }"
        )
        fn = self.fn_wrapper([(x, s)], "None", [f"{x}.b();"])
        return f"struct {s} {{ }}\n{extra}\n{fn}"

    def gen_group(self) -> str:
        s = self.name("S")
        ty = self.name("Ty")
        g = self.name("G")
        return (
            f"type {ty} = Int where {{ self > 0; }}\n"
            f"struct {s} {{ pub a: {ty} }}\n"
            f"group {g}(a: {ty}) {{ a -> {ty}; }}\n"
            f"{g}@{s} -> {{a}}"
        )

    def gen_group_bound(self) -> str:
        s = self.name("S")
        ty = self.name("Ty")
        g = self.name("G")
        return (
            f"type {ty} = Int where {{ self > 0; }}\n"
            f"struct {s} {{ pub a: {ty} }}\n"
            f"group {g}: {s} {{ self.a -> {ty}; }}"
        )

    def gen_from(self) -> str:
        a = self.name("A")
        b = self.name("B")
        return (
            f"struct {a} {{ pub x: Int }}\n"
            f"struct {b} {{ }}\n"
            f"impl From<{a}> for {b} {{"
            f" fn from(value: {a}) -> {b} {{ return {b} {{ }}; }}"
            f" fn into(self) -> {b} {{ return {b} {{ }}; }} }}"
            f"fn {self.name('z')}(x: {a}) -> {b} {{ return x.into(); }}"
        )

    def gen_where_field(self) -> str:
        s = self.name("S")
        struct = (
            f"struct {s} {{ pub a: Int where {{ a > 0 }}, "
            "pub b: Int -> { b > 0 } }"
        )
        fn = self.fn_wrapper([], "None", [f"let x: {s} = {s} {{ 1, 2 }};"])
        return struct + "\n" + fn

    def gen_uninit(self) -> str:
        return self.fn_wrapper(
            [],
            "Int",
            ["let x: Int;", "x = 1;", "let y: Int = x;", "return y;"],
        )

    def gen_for(self) -> str:
        v = self.name("v")
        return self.fn_wrapper(
            [(v, "Vector<String>")],
            "None",
            [
                f"for x in {v} {{ builtins::print(x); }}",
                f"for (x: {v}) {{ builtins::print(x); }}",
                f"for (String x: {v}) {{ builtins::print(x); }}",
                "while (true) { break; }",
                f"for x in {v} {{ continue; }}",
            ],
        )

    def gen_map_iter(self) -> str:
        m = self.name("m")
        return self.fn_wrapper(
            [(m, "Map<String, Int>")],
            "None",
            [
                f"for kv in {m} {{ builtins::print(kv); }}",
                f"let t: Tuple = {m}.entry();",
                f"let u: Tuple = {m}.get_last();",
            ],
        )

    def gen_const_ref(self) -> str:
        c1 = self.name("c")
        c2 = self.name("d")
        fn = self.fn_wrapper([], "Int", [f"return {c1} + {c2};"])
        return f"const {c1}: Int = 1;\nconst {c2}: Int = {c1} + 2;\n{fn}"

    def gen_fn_generic(self) -> str:
        """Generic function calls (known SA-bug family)."""
        n = self.name("id")
        fn = self.fn_wrapper([], "Int", [f"return {n}(1);"])
        return f"fn {n}<T>(x: T) -> T {{ return x; }}\n{fn}"

    def gen_fn_generic_decl(self) -> str:
        n = self.name("f")
        return f"fn {n}<T>() -> None {{ let s: Map<T, String> = {{}}; }}"

    def gen_nested(self) -> str:
        m = self.name("m")
        return self.fn_wrapper(
            [(m, "Vector<Vector<Int>>")],
            "Int",
            [
                "let a: Vector<Vector<Int>> = [[1, 2], [3, 4]];",
                "let b: Int = a[0][1];",
                f"let c: Vector<Int> = {m}[0];",
                "return b;",
            ],
        )

    def gen_deep_generic(self) -> str:
        depth = self.rng.randint(3, 8)
        t = "Vector<" * depth + "Int" + ">" * depth
        return self.fn_wrapper([], "None", [f"let x: {t} = [];"])

    def gen_compound_assign(self) -> str:
        x = self.name("x")
        y = self.name("y")
        return self.fn_wrapper(
            [(x, "Int"), (y, "Int")],
            "None",
            [
                f"{x} += 1;",
                f"{x} -= 1;",
                f"{x} *= 2;",
                f"{x} /= 2;",
            ],
        )

    def gen_loop_break(self) -> str:
        return self.fn_wrapper(
            [],
            "Int",
            [
                "let i: Int = 0;",
                "while (i < 10) {",
                "  i += 1;",
                "  if (i == 3) { continue; }",
                "  if (i == 5) { break; }",
                "}",
                "for x in [1, 2] { break; }",
                "return i;",
            ],
        )

    def gen_none_ret(self) -> str:
        return self.fn_wrapper(
            [], "None", ["let x: None = None;", "return None;"]
        )

    def gen(self) -> str:
        gens = [
            self.gen_const,
            self.gen_type,
            self.gen_type_string,
            self.gen_typedef,
            self.gen_typedef_generic,
            self.gen_typedef_vector_alias,
            self.gen_enum,
            self.gen_struct,
            self.gen_struct_unit,
            self.gen_struct_generic,
            self.gen_struct_generic_field,
            self.gen_extra,
            self.gen_extra_generic,
            self.gen_extra_method_generic,
            self.gen_impl,
            self.gen_impl_generic,
            self.gen_impl_generic_struct,
            self.gen_impl_self_return,
            self.gen_impl_builtin_trait,
            self.gen_bound,
            self.gen_builtin_methods,
            self.gen_static,
            self.gen_which,
            self.gen_group,
            self.gen_group_bound,
            self.gen_from,
            self.gen_where_field,
            self.gen_uninit,
            self.gen_for,
            self.gen_map_iter,
            self.gen_const_ref,
            self.gen_fn_generic,
            self.gen_fn_generic_decl,
            self.gen_nested,
            self.gen_deep_generic,
            self.gen_compound_assign,
            self.gen_loop_break,
            self.gen_none_ret,
        ]
        parts = []
        for _ in range(self.rng.randint(1, 5)):
            parts.append(self.rng.choice(gens)())
        return "\n".join(parts)


# Known-bug filter (error-message substrings -> bug id).
#
# The generic-parameter substitution family and the method-level generic
# scope bug were fixed in the SA; the entries were removed so a regression
# is reported as a real SA error instead of being hidden as a known bug.


_DEFAULT_KNOWN_BUGS: list[tuple[str, str]] = []


def load_known_bugs() -> list[tuple[str, str]]:
    """Load ``known_bugs.json`` next to this file; fall back to the embedded
    defaults when the file is absent or malformed."""
    path = ROOT / "known_bugs.json"
    if not path.exists():
        return list(_DEFAULT_KNOWN_BUGS)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return list(_DEFAULT_KNOWN_BUGS)
    patterns = []
    for entry in data:
        pattern = entry.get("pattern")
        bug_id = entry.get("id")
        if isinstance(pattern, str) and isinstance(bug_id, str):
            patterns.append((pattern, bug_id))
    return patterns or list(_DEFAULT_KNOWN_BUGS)


KNOWN_BUG_PATTERNS = load_known_bugs()


def match_known_bug(result: dict) -> Optional[str]:
    if result["kind"] != "sa_err":
        return None
    for message in (e["message"] for e in result["sa_errors"]):
        for pattern, bug_id in KNOWN_BUG_PATTERNS:
            if re.search(pattern, message):
                return bug_id
    return None


# 
# Runner
# 


@dataclass
class Case:
    index: int
    source: str
    mode: str
    result: dict = field(default_factory=dict)
    known_bug: Optional[str] = None


def run_campaign(
    cases: list[Case],
    out_dir: pathlib.Path,
    label: str,
    *,
    expect_syntax_valid: bool,
    jobs: int = 0,
    report_every: int = 100,
) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    cases_dir = out_dir / "cases"
    cases_dir.mkdir(parents=True, exist_ok=True)

    counts = {"clean": 0, "crash": 0, "sa_err": 0, "lex_err": 0, "parse_err": 0}
    sig_counts: dict[tuple[str, ...], int] = {}
    examples: dict[tuple[str, ...], int] = {}
    saved = 0

    if jobs <= 0:
        jobs = min(32, (os.cpu_count() or 1) + 4)

    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        # ``pool.map`` keeps results in case order, so reports stay
        # deterministic; all mutable bookkeeping happens in this thread.
        analyzed = pool.map(analyze, (case.source for case in cases))
        t0 = time.monotonic()
        done = 0
        prev_counts = dict(counts)
        for case, result in zip(cases, analyzed):
            _process_case(
                case, result, counts, sig_counts, examples,
                cases_dir, label,
            )
            if case.result["kind"] != "clean":
                saved += 1
            done += 1
            if report_every > 0 and (
                done % report_every == 0 or done == len(cases)
            ):
                _print_progress(
                    done, len(cases), counts, prev_counts, saved, t0
                )
                prev_counts = dict(counts)

    report = {
        "label": label,
        "total": len(cases),
        "jobs": jobs,
        "counts": counts,
        "saved": saved,
        "expect_syntax_valid": expect_syntax_valid,
        "unique_sa_error_signatures": [
            {"messages": list(sig), "count": n, "example_index": examples[sig]}
            for sig, n in sorted(sig_counts.items(), key=lambda kv: -kv[1])
        ],
    }
    (out_dir / f"{label}_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return report


def _print_progress(
    done: int,
    total: int,
    counts: dict,
    prev_counts: dict,
    saved: int,
    t0: float,
) -> None:
    """Print a batch progress line with cumulative counts plus the deltas
    produced by the batch that just finished."""
    elapsed = time.monotonic() - t0
    rate = done / elapsed if elapsed > 0 else 0.0
    batch = {
        k: counts.get(k, 0) - prev_counts.get(k, 0)
        for k in ("crash", "known_bug", "sa_err", "lex_err", "parse_err")
    }
    batch_text = " ".join(
        f"{k}={v}" for k, v in batch.items() if v
    ) or "none"
    print(
        f"[progress] {done}/{total} cases | "
        f"clean={counts.get('clean', 0)} "
        f"crash={counts.get('crash', 0)} "
        f"known_bug={counts.get('known_bug', 0)} "
        f"sa_err={counts.get('sa_err', 0)} "
        f"lex_err={counts.get('lex_err', 0)} "
        f"parse_err={counts.get('parse_err', 0)} "
        f"saved={saved} | batch: {batch_text} | "
        f"{elapsed:.1f}s | {rate:.0f} cases/s",
        file=sys.stderr,
        flush=True,
    )


def _print_gen_progress(done: int, total: int, t0: float) -> None:
    """Print generation-phase progress (stderr, flushed)."""
    elapsed = time.monotonic() - t0
    rate = done / elapsed if elapsed > 0 else 0.0
    print(
        f"[gen] {done}/{total} cases | {elapsed:.1f}s | {rate:.0f} cases/s",
        file=sys.stderr,
        flush=True,
    )


def _process_case(
    case: Case,
    result: dict,
    counts: dict,
    sig_counts: dict,
    examples: dict,
    cases_dir: pathlib.Path,
    label: str,
) -> None:
    """Fold one analysis result into the shared report state (main thread
    only, so no locking is needed)."""
    case.result = result
    case.known_bug = match_known_bug(result)
    kind = result["kind"]
    if kind == "sa_err" and case.known_bug:
        kind = "known_bug"
    counts[kind] = counts.get(kind, 0) + 1
    if kind == "sa_err":
        sig = sig_of(result)
        sig_counts[sig] = sig_counts.get(sig, 0) + 1
        examples.setdefault(sig, case.index)

    if kind in ("crash", "sa_err", "known_bug", "lex_err", "parse_err"):
        fname = f"{label}_{case.index:06d}.wind"
        (cases_dir / fname).write_text(case.source, encoding="utf-8")
        meta = {
            "id": case.index,
            "mode": case.mode,
            "kind": kind,
            "known_bug": case.known_bug,
            **result,
        }
        (cases_dir / f"{label}_{case.index:06d}.json").write_text(
            json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
        )


def print_report(report: dict) -> None:
    print(f"total       : {report['total']}")
    print(f"jobs        : {report['jobs']}")
    for k in ("clean", "crash", "known_bug", "sa_err", "lex_err", "parse_err"):
        print(f"{k:<12}: {report['counts'].get(k, 0)}")
    print(f"saved cases : {report['saved']}")
    if report["expect_syntax_valid"] and (
        report["counts"].get("lex_err") or report["counts"].get("parse_err")
    ):
        print("[!] lex/parse errors in generated cases mean the generator is wrong")
    if report["unique_sa_error_signatures"]:
        print("\nunique SA error signatures (not matching known bugs):")
        for item in report["unique_sa_error_signatures"][:30]:
            print(f"  x{item['count']:>6}  {item['messages'][0][:120]}")


def default_seeds() -> list[pathlib.Path]:
    seeds = []
    for f in ("exam.wind", "exam2.wind", "exam3.wind"):
        p = REPO / "assets" / f
        if p.exists():
            seeds.append(p)
    p = REPO / "assets" / "user_test" / "find_primes.wind"
    if p.exists():
        seeds.append(p)
    return seeds


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="fuzz_sa",
        description="Fuzz the CWind semantic analyzer with valid-by-construction "
        "programs and conservative mutations.",
    )
    ap.add_argument(
        "--mode",
        choices=("gen", "mutate", "corpus"),
        default="gen",
        help="gen: generate valid programs; mutate: mutate known-good seeds; "
        "corpus: analyze every .wind under --dir",
    )
    ap.add_argument("--count", type=int, default=20000, help="cases to generate")
    ap.add_argument("--seed", type=int, default=1, help="RNG seed")
    ap.add_argument(
        "--jobs",
        type=int,
        default=0,
        help="parallel analysis workers "
        "(default: min(32, cpu_count + 4); 0 means auto)",
    )
    ap.add_argument(
        "--report-every",
        type=int,
        default=100,
        help="print a progress line every N cases (0 disables)",
    )
    ap.add_argument("--dir", help="directory scanned by --mode corpus")
    ap.add_argument(
        "--seeds",
        nargs="*",
        help="seed files for --mode mutate (default: assets exam/user files)",
    )
    ap.add_argument(
        "--out",
        default=str(ROOT / "out"),
        help="output directory (default: fuzz/out)",
    )
    ap.add_argument("--label", default="run", help="report/case file name prefix")
    ap.add_argument(
        "--min-mutations",
        type=int,
        default=1,
        help="mutate mode: minimum mutations per case",
    )
    ap.add_argument(
        "--max-mutations",
        type=int,
        default=5,
        help="mutate mode: maximum mutations per case",
    )
    args = ap.parse_args(argv)

    rng = random.Random(args.seed)
    out_dir = pathlib.Path(args.out)

    if args.mode == "gen":
        cases = []
        gen_t0 = time.monotonic()
        if args.report_every > 0:
            print(
                f"[gen] generating {args.count} cases...",
                file=sys.stderr,
                flush=True,
            )
        for i in range(args.count):
            gen = Generator(rng)
            cases.append(Case(index=i, source=gen.gen(), mode="gen"))
            if (
                args.report_every > 0
                and (i + 1) % args.report_every == 0
                and i + 1 != args.count
            ):
                _print_gen_progress(i + 1, args.count, gen_t0)
        if args.report_every > 0:
            _print_gen_progress(args.count, args.count, gen_t0)
    elif args.mode == "mutate":
        seeds = [pathlib.Path(p) for p in args.seeds] if args.seeds else default_seeds()
        if not seeds:
            print("[Error] no seed files found", file=sys.stderr)
            return 2
        seeds = [p for p in seeds if p.exists()]
        seed_sources = [p.read_text(encoding="utf-8") for p in seeds]
        mut = Mutator(rng)
        cases = []
        gen_t0 = time.monotonic()
        if args.report_every > 0:
            print(
                f"[gen] mutating {args.count} cases from {len(seed_sources)} seeds...",
                file=sys.stderr,
                flush=True,
            )
        for i in range(args.count):
            src = rng.choice(seed_sources)
            steps = rng.randint(args.min_mutations, args.max_mutations)
            for _ in range(steps):
                src = mut.mutate(src)
            cases.append(Case(index=i, source=src, mode="mutate"))
            if (
                args.report_every > 0
                and (i + 1) % args.report_every == 0
                and i + 1 != args.count
            ):
                _print_gen_progress(i + 1, args.count, gen_t0)
        if args.report_every > 0:
            _print_gen_progress(args.count, args.count, gen_t0)
    else:  # corpus
        d = pathlib.Path(args.dir or (REPO / "assets"))
        files = sorted(d.rglob("*.wind"))
        cases = [Case(index=i, source=p.read_text(encoding="utf-8"), mode="corpus") for i, p in enumerate(files)]
        print(f"scanning {len(files)} files under {d}", file=sys.stderr, flush=True)

    report = run_campaign(
        cases,
        out_dir,
        args.label,
        expect_syntax_valid=args.mode != "corpus",
        jobs=args.jobs,
        report_every=args.report_every,
    )
    print_report(report)
    print(f"\noutput: {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
