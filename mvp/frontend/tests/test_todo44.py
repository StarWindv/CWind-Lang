"""todo-44: declarative ``macro_rules!`` macros — full test suite.

Three layers, mirroring the rustc ``mbe`` port structure:

* unit tests per module (pattern -> validate -> matcher -> expander),
* driver tests over token streams (definitions, fixpoint, errors),
* end-to-end parse+SA programs including hygiene behavior.

Data-driven regression cases live in ``cases/todo44/`` (see test_cases.py).
"""

from __future__ import annotations

import sys
import unittest

TESTS = __import__("pathlib").Path(__file__).resolve().parent
ROOT = TESTS.parent.parent.parent.parent
for path in (ROOT / "mvp/frontend/src", ROOT / "mvp/frontend/tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from cwind_frontend.ast_components.token import TokenKind  # noqa: E402
from cwind_frontend.lexer import lex_with_errors, tokenize  # noqa: E402
from cwind_frontend import run_sa_with_errors  # noqa: E402
from cwind_frontend.macros import (  # noqa: E402
    Binding,
    Group,
    Kleene,
    MacroDef,
    MacroExpandError,
    MacroMatchError,
    MacroPatternError,
    Repetition,
    expand_macros,
    match_rule,
)
from cwind_frontend.macros.fragments import FragmentParser  # noqa: E402
from cwind_frontend.macros.pattern import MacroTokens, read_group  # noqa: E402
from cwind_frontend.macros.trees import GroupDelim  # noqa: E402
from cwind_frontend.macros.validate import validate_matcher  # noqa: E402
from cwind_frontend.parser.parser import parse_with_errors  # noqa: E402
from cwind_frontend.parser.core import ParserCore  # noqa: E402
import harness  # noqa: E402

MAC = "todo44"


def _macro_def(head: str) -> MacroDef:
    """Parse ``($matcher) [=> $body]`` arm text into a MacroDef.

    Matcher-only form is enough for validation/matching tests; the body
    defaults to ``{ 0 }``.
    """
    toks = tokenize(head)
    cursor = MacroTokens(toks)
    macro = MacroDef(name="m")
    matcher = read_group(cursor, True, "matcher")
    arrow = cursor.next()
    if arrow is not None and arrow.kind == TokenKind.FAT_ARROW:
        body = read_group(cursor, False, "body")
    else:
        body = _macro_body_placeholder()
    from cwind_frontend.macros.definition import MacroRule

    macro.rules.append(MacroRule(matcher, body, matcher.open_token))
    return macro


_BODY_CACHE = None


def _macro_body_placeholder() -> Group:
    """The ``{ 0 }`` template used when a test passes a matcher only."""
    global _BODY_CACHE
    if _BODY_CACHE is None:
        toks = tokenize("{ 0 }")
        cursor = MacroTokens(toks)
        _BODY_CACHE = read_group(cursor, False, "body")
    return _BODY_CACHE


def _def_only(source: str) -> tuple[MacroDef, list[str]]:
    """Run only the definition-collection pass; returns the first macro."""
    defs: dict[str, MacroDef] = {}
    errors: list = []
    expand_macros(tokenize(source), lambda: 0)  # smoke: driver must not hang
    from cwind_frontend.macros.expansion import _collect_definitions

    _collect_definitions(tokenize(source), defs, errors)
    macro = next(iter(defs.values()))
    return macro, [str(e) for e in errors]


def _expand(source: str) -> tuple[str, list[str]]:
    """Token-stream level: expand and render the stream for inspection."""
    stream, errors = expand_macros(tokenize(source), iter(int, 1).__next__)
    # deterministic fresh ids via a counter
    return " ".join(t.raw for t in stream), [str(e) for e in errors]


def _expand_checked(source: str) -> tuple[str, list[str]]:
    counter = [0]

    def next_ctx() -> int:
        counter[0] += 1
        return counter[0]

    stream, errors = expand_macros(tokenize(source), next_ctx)
    return " ".join(t.raw for t in stream), [str(e) for e in errors]


def _pipeline(source: str) -> tuple[list[str], list[str]]:
    """Full parse + SA; returns (error strings) for both stages."""
    r = parse_with_errors(tokenize(source))
    errors = [str(e) for e in r.errors]
    sa_errors: list[str] = []
    if not errors:
        sa = run_sa_with_errors(r.program)
        sa_errors = [str(e) for e in sa.errors]
    return errors, sa_errors


def _match(
    macro: MacroDef, call: str
) -> dict[str, object]:
    toks = tokenize(call)
    inv = Group(toks[0], toks[-1], GroupDelim.PAREN, tuple(toks[1:-1]))
    return match_rule(macro.rules[0].matcher, inv, FragmentParser())


class TestMacroPattern(unittest.TestCase):
    """Pattern-tree building from definition tokens."""

    def test_binding(self):
        macro = _macro_def("($x:expr)")
        b = macro.rules[0].matcher.body[0]
        self.assertIsInstance(b, Binding)
        self.assertEqual(b.name, "x")
        self.assertEqual(b.fragment, "expr")

    def test_type_fragment_keyword(self):
        macro = _macro_def("($t:type)")
        b = macro.rules[0].matcher.body[0]
        self.assertIsInstance(b, Binding)
        self.assertEqual(b.fragment, "type")

    def test_repetition_with_separator(self):
        macro = _macro_def("($($x:expr),*)")
        rep = macro.rules[0].matcher.body[0]
        self.assertIsInstance(rep, Repetition)
        self.assertEqual(rep.separator.kind, TokenKind.COMMA)
        self.assertIs(rep.kleene, Kleene.ZERO_OR_MORE)
        inner = rep.body[0]
        self.assertIsInstance(inner, Binding)
        self.assertEqual(inner.fragment, "expr")

    def test_repetition_kleene_ops(self):
        for tail, op in (("*", Kleene.ZERO_OR_MORE), ("+", Kleene.ONE_OR_MORE),
                         ("?", Kleene.ZERO_OR_ONE)):
            with self.subTest(op=op):
                macro = _macro_def(f"($($x:ident){tail})")
                rep = macro.rules[0].matcher.body[0]
                self.assertIs(rep.kleene, op)
                self.assertIsNone(rep.separator)

    def test_nested_group_in_matcher(self):
        macro = _macro_def("(($a:ident))")
        group = macro.rules[0].matcher.body[0]
        self.assertIsInstance(group, Group)
        self.assertIsInstance(group.body[0], Binding)

    def test_missing_fragment_specifier(self):
        with self.assertRaises(MacroPatternError) as cm:
            _macro_def("($x)")
        self.assertIn("': fragment'", str(cm.exception))

    def test_unknown_fragment(self):
        with self.assertRaises(MacroPatternError) as cm:
            _macro_def("($x:widget)")
        self.assertIn("invalid fragment specifier 'widget'", str(cm.exception))

    def test_q_takes_no_separator(self):
        with self.assertRaises(MacroPatternError) as cm:
            _macro_def("($($x:expr),?)")
        self.assertIn("does not take a separator", str(cm.exception))

    def test_missing_kleene(self):
        with self.assertRaises(MacroPatternError) as cm:
            _macro_def("($($x:expr),)")
        self.assertIn("expected '*', '+' or '?'", str(cm.exception))

    def test_unclosed_group(self):
        with self.assertRaises(MacroPatternError):
            _macro_def("(($x:ident)")


class TestMacroValidation(unittest.TestCase):
    """Definition-time checks: follow sets, empty repetition, binders."""

    def _issues(self, head: str) -> list[str]:
        macro = _macro_def(head)
        return [i.message for i in validate_matcher(
            macro.rules[0].matcher, macro.rules[0].body
        )]

    def test_expr_cannot_be_followed_by_plus(self):
        self.assertIn("not allowed for 'expr' fragments",
                      self._issues("($x:expr + 1) => { $x }")[0])

    def test_expr_may_be_followed_by_comma(self):
        self.assertEqual(self._issues("($x:expr, $y:expr) => { $x }"), [])

    def test_expr_may_be_followed_by_fat_arrow(self):
        self.assertEqual(self._issues("($x:expr => $y:expr) => { $x }"), [])

    def test_stmt_follow_set(self):
        self.assertIn("not allowed for 'stmt' fragments",
                      self._issues("($s:stmt let) => { $s }")[0])

    def test_pat_follow_set(self):
        self.assertEqual(self._issues("($p:pat if) => { $p }"), [])
        self.assertIn("not allowed for 'pat' fragments",
                      self._issues("($p:pat 1) => { $p }")[0])

    def test_type_follow_set(self):
        self.assertEqual(self._issues("($t:type where) => { $t }"), [])
        self.assertIn("not allowed for 'type' fragments",
                      self._issues("($t:type -) => { $t }")[0])

    def test_single_token_fragments_follow_anything(self):
        self.assertEqual(self._issues("($i:ident +) => { $i }"), [])
        self.assertEqual(self._issues("($l:literal +) => { $l }"), [])
        self.assertEqual(self._issues("($t:token +) => { $t }"), [])
        self.assertEqual(self._issues("($b:block +) => { $b }"), [])

    def test_empty_repetition_rejected(self):
        self.assertIn("matches the empty token tree",
                      self._issues("($($v:vis)*) => { 0 }")[0])

    def test_empty_repetition_with_separator_ok(self):
        self.assertEqual(self._issues("($($x:expr),*) => { 0 }"), [])

    def test_duplicate_binder(self):
        macro = _macro_def("($x:expr, $x:expr)")
        issues = validate_matcher(macro.rules[0].matcher, macro.rules[0].body)
        self.assertTrue(any("duplicated bind name: x" in i.message
                            for i in issues))

    def test_body_uses_unbound_var(self):
        macro = _macro_def("($x:expr) => { $y }")
        issues = validate_matcher(macro.rules[0].matcher, macro.rules[0].body)
        self.assertTrue(any("never binds it" in i.message for i in issues))

    def test_one_or_more_needs_required_part(self):
        # `$($x:expr),+` without separator: body binds only in the
        # repetition, which requires at least one iteration -> fine.
        self.assertEqual(self._issues("($($x:expr)+) => { $x }"), [])


class TestMacroMatcher(unittest.TestCase):
    """NFA matching behavior."""

    def _matched(self, head: str, call: str) -> dict[str, str]:
        macro = _macro_def(head)
        matches = _match(macro, call)
        out: dict[str, str] = {}
        for name, m in matches.items():
            if hasattr(m, "tokens"):
                out[name] = " ".join(t.raw for t in m.tokens)
            else:
                out[name] = f"seq({len(m.items)})"
        return out
    def test_single_expr(self):
        self.assertEqual(self._matched("($x:expr)", "(1 + 2)"),
                         {"x": "1 + 2"})

    def test_expr_greedy_until_comma(self):
        self.assertEqual(
            self._matched("($x:expr, $y:expr)", "(1 + 2, f(3))"),
            {"x": "1 + 2", "y": "f ( 3 )"},
        )

    def test_ident_and_literal(self):
        self.assertEqual(self._matched("($i:ident)", "(foo)"), {"i": "foo"})
        self.assertEqual(self._matched("($l:literal)", "(3.14)"), {"l": "3.14"})
        self.assertEqual(self._matched("($l:literal)", "(-7)"), {"l": "- 7"})

    def test_literal_matches_value_exactly(self):
        macro = _macro_def("(1)")
        with self.assertRaises(MacroMatchError):
            _match(macro, "(2)")
        self.assertEqual(_match(macro, "(1)"), {})

    def test_keyword_token_matches(self):
        self.assertEqual(self._matched("(fn)", "(fn)"), {})
        with self.assertRaises(MacroMatchError):
            _match(_macro_def("(fn)"), "(let)")

    def test_repetition_lockstep(self):
        self.assertEqual(
            self._matched("($($x:expr),*)", "(1, 2, 3)"),
            {"x": "seq(3)"},
        )

    def test_repetition_zero(self):
        self.assertEqual(self._matched("($($x:expr),*)", "()"),
                         {"x": "seq(0)"})

    def test_repetition_rejects_trailing_separator(self):
        with self.assertRaises(MacroMatchError):
            _match(_macro_def("($($x:expr),*)"), "(1, 2,)")

    def test_nested_repetition_structure(self):
        # Outer repetition separated by `;`, inner by `,`: x is a seq of
        # seqs (one MatchedSeq element per outer iteration).
        macro = _macro_def("($($($x:expr),*);*)")
        toks = tokenize("((1, 2);(3))")
        inv = Group(toks[0], toks[-1], GroupDelim.PAREN, tuple(toks[1:-1]))
        matches = match_rule(macro.rules[0].matcher, inv, FragmentParser())
        outer = matches["x"]
        self.assertEqual(len(outer.items), 2)
        self.assertEqual(len(outer.items[0].items), 1)
        self.assertEqual(len(outer.items[1].items), 1)
        self.assertEqual(
            " ".join(t.raw for t in outer.items[0].items[0].tokens),
            "( 1 , 2 )",
        )

    def test_alternative_arms_first_match_wins(self):
        macro = _macro_def("(a) => { 1 }")
        from cwind_frontend.macros.definition import MacroRule
        toks = tokenize("(b) => { 2 }")
        cursor = MacroTokens(toks)
        m2 = read_group(cursor, True, "matcher")
        cursor.next()  # =>
        b2 = read_group(cursor, False, "body")
        macro.rules.append(MacroRule(m2, b2, m2.open_token))
        b_toks = tokenize("(b)")
        inv_b = Group(b_toks[0], b_toks[-1], GroupDelim.PAREN,
                      tuple(b_toks[1:-1]))
        # rule 0 (`a`) rejects `b`; rule 1 (`b`) matches
        try:
            match_rule(macro.rules[0].matcher, inv_b, FragmentParser())
            rule0_rejected = False
        except MacroMatchError:
            rule0_rejected = True
        self.assertTrue(rule0_rejected)
        self.assertEqual(
            match_rule(macro.rules[1].matcher, inv_b, FragmentParser()), {})

    def test_stmt_fragment_captures_semicolon(self):
        self.assertEqual(
            self._matched("($s:stmt)", "(let q: Int32 = 1;)"),
            {"s": "let q : Int32 = 1 ;"},
        )

    def test_stmt_fragment_at_tail_without_semicolon(self):
        # The synthetic end-of-args terminator makes a final expression
        # statement parse like a block tail.
        self.assertEqual(self._matched("($s:stmt)", "(go())"), {"s": "go ( )"})

    def test_type_fragment_generic_close(self):
        self.assertEqual(
            self._matched("($t:type)", "(Vector<Vector<Int32>>)"),
            {"t": "Vector < Vector < Int32 >>"},
        )

    def test_token_fragment_captures_group(self):
        self.assertEqual(self._matched("($t:token)", "({a + b})"),
                         {"t": "{ a + b }"})

    def test_vis_fragment_followed_by_item(self):
        self.assertEqual(
            self._matched("($v:vis $i:ident)", "(pub foo)"),
            {"v": "pub", "i": "foo"},
        )
        self.assertEqual(
            self._matched("($v:vis $i:ident)", "(foo)"),
            {"v": "", "i": "foo"},
        )

    def test_no_rule_expected(self):
        with self.assertRaises(MacroMatchError) as cm:
            _match(_macro_def("(a, b)"), "(a, c)")
        self.assertIn("no rule expected the token `c`", str(cm.exception))

    def test_missing_tokens_at_eof(self):
        with self.assertRaises(MacroMatchError) as cm:
            _match(_macro_def("($x:expr, $y:expr)"), "(1)")
        self.assertIn("unexpected end of macro invocation", str(cm.exception))

    def test_expr_then_expr_consume_in_order(self):
        # Two adjacent expr fragments: the first expr swallows `1`, the
        # second waits for more input until the args run out -> missing
        # tokens error (matches rustc behavior for ambiguous matchers).
        with self.assertRaises(MacroMatchError):
            _match(_macro_def("($x:expr $y:expr)"), "(1)")


class TestMacroExpander(unittest.TestCase):
    """Transcription: substitution, lockstep repetition, hygiene ids."""

    def _expand_call(self, head: str, call: str, context: int = 7) -> str:
        macro = _macro_def(head)
        matches = _match(macro, call)
        toks = tokenize(call)
        site = (toks[0].line, toks[0].column, toks[0].end_line,
                toks[0].end_column)
        from cwind_frontend.macros.expander import transcribe

        return " ".join(t.raw for t in transcribe(
            macro.rules[0].body, matches, context, site
        ))

    def test_substitution(self):
        self.assertEqual(self._expand_call("($x:expr) => { $x + 1 }", "(41)"),
                         "41 + 1")

    def test_substituted_tokens_keep_caller_context(self):
        macro = _macro_def("($x:expr) => { $x }")
        matches = _match(macro, "(5)")
        from cwind_frontend.macros.expander import transcribe

        toks = transcribe(macro.rules[0].body, matches, 3, (1, 1, 1, 2))
        self.assertTrue(all(t.context is None for t in toks))

    def test_template_tokens_carry_expansion_context(self):
        macro = _macro_def("($x:expr) => { $x + 1 }")
        matches = _match(macro, "(5)")
        from cwind_frontend.macros.expander import transcribe

        toks = transcribe(macro.rules[0].body, matches, 3, (1, 1, 1, 2))
        self.assertEqual([t.context for t in toks], [None, 3, 3])

    def test_repetition_expansion(self):
        out = self._expand_call(
            "($($x:expr),*) => { f($($x),*) }", "(1, 2)")
        self.assertEqual(out, "f ( 1 , 2 )")

    def test_repetition_zero_leaves_placeholder(self):
        out = self._expand_call(
            "($($x:expr),*) => { f(0 $(, $x)*) }", "()")
        self.assertEqual(out, "f ( 0 )")

    def test_separator_between_iterations(self):
        out = self._expand_call(
            "($($x:expr),*) => { [$($x);*] }", "(1, 2, 3)")
        self.assertEqual(out, "[ 1 ; 2 ; 3 ]")

    def test_plus_requires_at_least_one(self):
        # The matcher's `*` binds zero iterations; the body's `+`
        # requires at least one -> transcription error.
        with self.assertRaises(MacroExpandError) as cm:
            self._expand_call("($($x:expr),*) => { f($($x),+) }", "()")
        self.assertIn("at least once", str(cm.exception))

    def test_two_seqs_of_different_lengths_contradict(self):
        with self.assertRaises(MacroExpandError):
            self._expand_call(
                "($($x:expr),* ; $($y:expr),*) => { g($($x $y),*) }",
                "(1, 2; 3)")

    def test_still_repeating_error(self):
        with self.assertRaises(MacroExpandError) as cm:
            self._expand_call("($($x:expr),*) => { f($x) }", "(1, 2)")
        self.assertIn("still repeating", str(cm.exception))

    def test_unbound_var_reemitted_for_nested_defs(self):
        # `$y` is not bound by this macro: the body is probably the
        # template of a macro this one defines; `$y` must survive.
        out = self._expand_call("($x:expr) => { macro_rules! q { "
                                "($y:expr) => { $y } } q!($x) }", "(9)")
        self.assertEqual(
            out,
            "macro_rules ! q { ( $ y : expr ) => { $ y } } q ! ( 9 )",
        )

    def test_groups_preserved_in_output(self):
        out = self._expand_call("($x:expr) => { fn g() -> Int32 { $x } }",
                                "(3)")
        self.assertEqual(out, "fn g ( ) -> Int32 { 3 }")


class TestMacroDriver(unittest.TestCase):
    """Definition collection and the expansion fixpoint."""

    def test_definition_stripped_from_stream(self):
        stream, errors = _expand_checked(
            "macro_rules! m { () => { 1 } } fn main() -> Int32 { return 0; }")
        self.assertEqual(errors, [])
        self.assertNotIn("macro_rules", stream)
        self.assertIn("fn main ( )", stream)

    def test_file_wide_visibility_use_before_def(self):
        errors, sa_errors = _pipeline(
            "fn main() -> Int32 { let a: Int32 = m!(); return a; }\n"
            "macro_rules! m { () => { 42 } }\n")
        self.assertEqual((errors, sa_errors), ([], []))

    def test_call_expansion(self):
        stream, errors = _expand_checked(
            "macro_rules! m { ($x:expr) => { $x + 1 } }\n"
            "fn main() -> Int32 { return m!(41); }")
        self.assertEqual(errors, [])
        self.assertIn("41 + 1", stream)
        self.assertNotIn("!", stream)

    def test_nested_call_expands(self):
        stream, errors = _expand_checked(
            "macro_rules! i { ($x:expr) => { $x + 1 } }\n"
            "macro_rules! o { ($x:expr) => { i!($x) } }\n"
            "fn main() -> Int32 { return o!(41); }")
        self.assertEqual(errors, [])
        self.assertIn("41 + 1", stream)

    def test_call_inside_call_arguments(self):
        stream, errors = _expand_checked(
            "macro_rules! i { ($x:expr) => { $x + 1 } }\n"
            "macro_rules! o { ($x:expr) => { $x * 2 } }\n"
            "fn main() -> Int32 { return o!(i!(20)); }")
        self.assertEqual(errors, [])
        # innermost-first: the inner call is expanded before the outer
        # matcher sees the arguments
        self.assertIn("20 + 1 * 2", stream)

    def test_unknown_macro(self):
        _, errors = _expand_checked("fn main() -> Int32 { return nope!(1); }")
        self.assertTrue(any("cannot find macro 'nope'" in e for e in errors))

    def test_no_rule_matches(self):
        _, errors = _expand_checked(
            "macro_rules! m { (a) => { 1 } }\n"
            "fn main() -> Int32 { return m!(b); }")
        self.assertTrue(any("no rule expected the token `b`" in e
                            for e in errors))

    def test_duplicate_definition(self):
        _, errors = _expand_checked(
            "macro_rules! m { () => { 1 } }\n"
            "macro_rules! m { () => { 2 } }\n"
            "fn main() -> Int32 { return 0; }")
        self.assertTrue(any("already defined" in e for e in errors))

    def test_definition_inside_call_args_not_registered(self):
        # Args are opaque (Rust semantics): a definition that only exists
        # inside an argument span is not registered until it is spliced
        # out by an expansion.
        _, errors = _expand_checked(
            "fn main() -> Int32 { return q!(macro_rules! q { () => { 1 } }); }")
        self.assertTrue(any("cannot find macro 'q'" in e for e in errors))

    def test_definition_produced_by_expansion(self):
        # A rule body may define a macro (spliced out verbatim); its own
        # call is expanded on the next fixpoint round.
        errors, sa_errors = _pipeline(
            "macro_rules! mk { () => { macro_rules! gen { () => { 5 } } "
            "gen!() } }\n"
            "fn main() -> Int32 { let v: Int32 = mk!(); return v; }\n")
        self.assertEqual((errors, sa_errors), ([], []))

    def test_recursion_limit_reported(self):
        _, errors = _expand_checked(
            "macro_rules! r { () => { r!() } }\n"
            "fn main() -> Int32 { r!(); return 0; }")
        self.assertTrue(any("recursion depth limit" in e for e in errors))

    def test_nested_expansion_chain_respects_limit(self):
        # A chain of N distinct macros nests N expansions; deep-but-finite
        # chains expand fully, runaway chains stop at the limit with a
        # single diagnostic (no cascade, no RecursionError).
        def build(depth: int) -> str:
            lines = ["macro_rules! m0 { ($x:expr) => { $x } }"]
            for i in range(1, depth + 1):
                lines.append(
                    f"macro_rules! m{i} {{ ($x:expr) => {{ m{i - 1}!($x) }} }}"
                )
            lines.append(f"fn main() -> Int32 {{ m{depth}!(1); return 0; }}")
            return "\n".join(lines)

        ok_stream, ok_errors = _expand_checked(build(5))
        self.assertEqual(ok_errors, [])
        self.assertNotIn("!", ok_stream)

        runaway_stream, runaway_errors = _expand_checked(build(300))
        limit_hits = [e for e in runaway_errors
                      if "recursion depth limit" in e]
        self.assertEqual(len(limit_hits), 1)

    def test_env_recursion_limit_override(self):
        import os
        from cwind_frontend.macros.expansion import recursion_limit_from_env
        try:
            os.environ["CWIND_RECURSION_LIMIT"] = "4"
            self.assertEqual(recursion_limit_from_env(), 4)
            os.environ["CWIND_RECURSION_LIMIT"] = "wat"
            self.assertEqual(recursion_limit_from_env(), 128)
            os.environ["CWIND_RECURSION_LIMIT"] = "-1"
            self.assertEqual(recursion_limit_from_env(), 128)
            os.environ["CWIND_RECURSION_LIMIT"] = "0"
            self.assertEqual(recursion_limit_from_env(), 128)
        finally:
            os.environ.pop("CWIND_RECURSION_LIMIT", None)
        self.assertEqual(recursion_limit_from_env(), 128)

    def test_doubling_macro_hits_token_limit_not_hang(self):
        stream, errors = _expand_checked(
            "macro_rules! d { ($x:expr) => { d!($x) + d!($x) } }\n"
            "fn main() -> Int32 { d!(1); return 0; }")
        self.assertTrue(any("token limit" in e for e in errors))

    def test_definition_error_keeps_stream_parseable(self):
        # `$` never reaches the parser: the definition is stripped even
        # when its rules fail to parse.
        _, errors = _expand_checked(
            "macro_rules! bad { ($x) => { 1 } }\n"
            "fn main() -> Int32 { return 0; }")
        self.assertTrue(any("': fragment'" in e for e in errors))
        stream, _ = _expand_checked(
            "macro_rules! bad { ($x) => { 1 } }\n"
            "fn main() -> Int32 { return 0; }")
        self.assertNotIn("$", stream)

    def test_macro_rules_usable_as_identifier(self):
        errors, sa_errors = _pipeline(
            "fn macro_rules() -> Int32 { return 1; }\n"
            "fn main() -> Int32 { let macro_rules: Int32 = macro_rules(); "
            "return macro_rules; }\n")
        self.assertEqual((errors, sa_errors), ([], []))

    def test_context_ids_increment_per_expansion(self):
        stream, errors = expand_macros(tokenize(
            "macro_rules! m { () => { let a: Int32 = 1; } }\n"
            "fn main() -> Int32 { m!(); m!(); return 0; }"),
            (n for n in range(1, 10)).__next__)
        self.assertEqual(errors, [])
        ids = sorted({t.context for t in stream if t.context is not None})
        self.assertEqual(ids, [1, 2])


class TestMacroHygiene(unittest.TestCase):
    """Basic hygiene: expansion bindings are mangled and isolated."""

    def test_mangle_shape(self):
        self.assertEqual(ParserCore.macro_mangle(3, "x"), "_m3_x")
        self.assertEqual(ParserCore.macro_unmangle("_m3_x"), (3, "x"))
        self.assertIsNone(ParserCore.macro_unmangle("x"))
        self.assertIsNone(ParserCore.macro_unmangle("_mx"))

    def test_expansion_binding_no_collision(self):
        errors, sa_errors = _pipeline(
            "macro_rules! tmp { () => { let t: Int32 = 1; } }\n"
            "fn main() -> Int32 { let t: Int32 = 5; tmp!(); return t; }\n")
        self.assertEqual((errors, sa_errors), ([], []))

    def test_expansion_cannot_capture_caller_locals(self):
        errors, _ = _pipeline(
            "macro_rules! grab { () => { let y: Int32 = x; } }\n"
            "fn main() -> Int32 { let x: Int32 = 5; grab!(); return 0; }\n")
        # `x` inside the expansion is mangled; the caller's `x` is not
        # visible (Rust: def-site tokens never capture call-site locals).
        self.assertTrue(any("unknown identifier" in e for e in errors + _))

    def test_expansion_can_use_file_level_items(self):
        errors, sa_errors = _pipeline(
            "fn helper() -> Int32 { return 3; }\n"
            "macro_rules! use_helper { () => { helper() } }\n"
            "fn main() -> Int32 { let v: Int32 = use_helper!(); return v; }\n")
        self.assertEqual((errors, sa_errors), ([], []))

    def test_expansion_can_use_builtins(self):
        errors, sa_errors = _pipeline(
            "macro_rules! hi { () => { print(\"hi\") } }\n"
            "fn main() -> Int32 { hi!(); return 0; }\n")
        self.assertEqual((errors, sa_errors), ([], []))

    def test_two_expansions_isolated(self):
        errors, sa_errors = _pipeline(
            "macro_rules! tmp { () => { let t: Int32 = 1; } }\n"
            "fn main() -> Int32 { tmp!(); tmp!(); return 0; }\n")
        self.assertEqual((errors, sa_errors), ([], []))

    def test_substituted_names_resolve_at_call_site(self):
        errors, sa_errors = _pipeline(
            "macro_rules! inc { ($v:ident) => { $v + 1 } }\n"
            "fn main() -> Int32 { let n: Int32 = 4; let m: Int32 = inc!(n); "
            "return m; }\n")
        self.assertEqual((errors, sa_errors), ([], []))

    def test_substituted_binding_is_user_scoped(self):
        errors, sa_errors = _pipeline(
            "macro_rules! bind { ($v:ident, $e:expr) => { let $v: Int32 = $e; } }\n"
            "fn main() -> Int32 { bind!(user_var, 3); return user_var; }\n")
        self.assertEqual((errors, sa_errors), ([], []))

    def test_type_fragment_used_as_value_hints_type_exists(self):
        # `$ty:type` spliced into a value position: the identifier is a
        # known type, so the error says so instead of a bare "unknown".
        errors, sa_errors = _pipeline(
            "macro_rules! show { ($ty:type) => { print($ty) } }\n"
            "fn main() -> Int32 { show!(Int32); return 0; }\n")
        both = errors + sa_errors
        self.assertTrue(any(
            "unknown identifier 'Int32'" in e for e in both))
        self.assertTrue(any(
            "a type with this name exists" in e for e in both))

    def test_expansion_name_out_of_scope_hints_macro_origin(self):
        # A mangled expansion identifier that resolves nowhere reports
        # that it came from a macro expansion.
        errors, sa_errors = _pipeline(
            "macro_rules! leak { () => { let q: Int32 = _m9_ghost; } }\n"
            "fn main() -> Int32 { leak!(); return 0; }\n")
        both = errors + sa_errors
        self.assertTrue(any(
            "unknown identifier" in e and "macro expansion" in e
            for e in both))


class TestMacroEndToEnd(unittest.TestCase):
    """Whole programs through parse + SA at every call position."""

    def test_expr_position(self):
        errors, sa_errors = _pipeline(
            "macro_rules! double { ($x:expr) => { $x * 2 } }\n"
            "fn main() -> Int32 { let a: Int32 = double!(21); return a; }\n")
        self.assertEqual((errors, sa_errors), ([], []))

    def test_stmt_position_multiple_statements(self):
        errors, sa_errors = _pipeline(
            "macro_rules! setup { () => { let a: Int32 = 1; let b: Int32 = 2; } }\n"
            "fn main() -> Int32 { setup!(); return 0; }\n")
        self.assertEqual((errors, sa_errors), ([], []))

    def test_stmt_position_bindings_visible_after(self):
        errors, sa_errors = _pipeline(
            "macro_rules! setup { ($v:ident, $e:expr) => { let $v: Int32 = $e; } }\n"
            "fn main() -> Int32 { setup!(a, 1); return a; }\n")
        self.assertEqual((errors, sa_errors), ([], []))

    def test_stmt_position_with_trailing_semicolons(self):
        errors, sa_errors = _pipeline(
            "macro_rules! tmp { () => { let t: Int32 = 1; } }\n"
            "fn main() -> Int32 { tmp!(); return 0; }\n")
        self.assertEqual((errors, sa_errors), ([], []))

    def test_item_position_generates_function(self):
        errors, sa_errors = _pipeline(
            "macro_rules! mkfn { ($name:ident) => { "
            "fn $name() -> Int32 { return 7; } } }\n"
            "mkfn!(seven);\n"
            "fn main() -> Int32 { return seven(); }\n")
        self.assertEqual((errors, sa_errors), ([], []))

    def test_type_position(self):
        errors, sa_errors = _pipeline(
            "macro_rules! box_of { ($t:type) => { 0 } }\n"
            "fn main() -> Int32 { box_of!(Vector<Int32>); return 0; }\n")
        self.assertEqual((errors, sa_errors), ([], []))

    def test_pattern_position(self):
        errors, sa_errors = _pipeline(
            "enum Opt { Some(Int32), None }\n"
            "macro_rules! branch { ($p:pat) => { "
            "match (Opt::Some(1)) { $p => 1, _ => 0 } } }\n"
            "fn main() -> Int32 { let v: Int32 = branch!(Opt::Some(n)); "
            "return v; }\n")
        self.assertEqual((errors, sa_errors), ([], []))

    def test_repetition_builds_vector(self):
        errors, sa_errors = _pipeline(
            "fn push(v: Vector<Int32>) -> Int32 { return 0; }\n"
            "macro_rules! fill { ($v:ident, $($x:expr),*) => { "
            "let $v: Vector<Int32> = Vector::new(); push($v); } }\n"
            "fn main() -> Int32 { fill!(nums, 1, 2, 3); return 0; }\n")
        self.assertEqual((errors, sa_errors), ([], []))

    def test_lockstep_sum(self):
        errors, sa_errors = _pipeline(
            "fn sum(w: Int32, a: Int32, b: Int32, c: Int32) -> Int32 { "
            "return w + a + b + c; }\n"
            "macro_rules! call_sum { ($($x:expr),*) => { sum(0 $(, $x)*) } }\n"
            "fn main() -> Int32 { let v: Int32 = call_sum!(1, 2, 3); "
            "return v; }\n")
        self.assertEqual((errors, sa_errors), ([], []))

    def test_generics_with_shift_close(self):
        errors, sa_errors = _pipeline(
            "macro_rules! wrap { ($t:type) => { 0 } }\n"
            "fn main() -> Int32 { wrap!(Vector<Vector<Int32>>); return 0; }\n")
        self.assertEqual((errors, sa_errors), ([], []))

    def test_question_outside_macros_is_parse_error(self):
        errors, _ = _pipeline("fn main() -> Int32 { let a: Int32 = (1 ? 2); return a; }\n")
        self.assertTrue(errors)

    def test_definition_before_and_after_main(self):
        errors, sa_errors = _pipeline(
            "macro_rules! a { () => { 1 } }\n"
            "fn main() -> Int32 { let x: Int32 = a!() + b!(); return x; }\n"
            "macro_rules! b { () => { 2 } }\n")
        self.assertEqual((errors, sa_errors), ([], []))

    def test_expansion_in_function_body_via_ident_macro(self):
        errors, sa_errors = _pipeline(
            "macro_rules! make_adder { ($name:ident, $d:expr) => { "
            "fn $name(v: Int32) -> Int32 { return v + $d; } } }\n"
            "make_adder!(add3, 3);\n"
            "fn main() -> Int32 { let r: Int32 = add3(4); return r; }\n")
        self.assertEqual((errors, sa_errors), ([], []))


if __name__ == "__main__":
    unittest.main()
