"""``#[cfg(...)]`` -- the compile-time configuration filter.

The only processor that can *drop* the item it decorates: a false
predicate removes the declaration before anything downstream sees it, so
mutually exclusive same-name definitions never collide.
"""

from __future__ import annotations

from typing import Any

from ..cfg import (
    CFG_COMBINATORS,
    CFG_FLAGS,
    CFG_KEYS,
    CFG_KEY_VALUES,
    CfgContext,
    CfgPredicate,
    evaluate_cfg,
)
from .model import (
    EXTERN_MEMBER,
    ITEM,
    METHOD,
    USE,
    Attr,
    AttrArg,
    AttributeError,
    InvalidArgument,
    MissingArgument,
    UnexpectedArgument,
    UnrecognizedAttribute,
)
from .registry import AttrProc, ProcCtx, register


def _predicate(arg: AttrArg) -> CfgPredicate:
    """Turn one collected argument into a predicate.

    Grammar (todo-86/93)::

        predicate := flag
                   | key = "value"            (e.g. target_os = "windows")
                   | ident( predicate, ... )  (only all / any / not)

    Reaching this function means the *syntax* was already accepted by the
    collector; everything rejected here is a cfg-grammar error, reported
    against the argument's own position.
    """
    if arg.kind == "flag":
        if arg.name in CFG_KEYS:
            raise UnexpectedArgument(
                f"'{arg.name}' expects = \"value\", not a bare flag",
                arg.line, arg.column,
            )
        if arg.name not in CFG_FLAGS:
            raise UnrecognizedAttribute(
                f"unknown cfg flag '{arg.name}' (expected a bare "
                f"flag ({', '.join(CFG_FLAGS)}), a combinator, or "
                "key = \"value\")",
                arg.line, arg.column,
            )
        return CfgPredicate("flag", name=arg.name)
    if arg.kind == "literal":
        line, column = arg.where()
        raise InvalidArgument(
            f"expected a flag, 'key = \"value\"' or a combinator "
            f"call, found the literal '{arg.value}'",
            line, column,
        )

    if arg.kind == "pair":
        if arg.name not in CFG_KEYS:
            raise UnrecognizedAttribute(
                f"unknown cfg key '{arg.name}' "
                f"(supported keys: {', '.join(CFG_KEYS)})",
                arg.line, arg.column,
            )
        allowed = CFG_KEY_VALUES[arg.name]
        if arg.value not in allowed:
            # blame the value, not the key it is attached to
            line, column = arg.where()
            raise InvalidArgument(
                f"invalid '{arg.name}' value '{arg.value}' "
                f"(expected one of: {', '.join(allowed)})",
                line, column,
            )
        return CfgPredicate("kv", name=arg.name, value=arg.value)

    # call
    if arg.name not in CFG_COMBINATORS:
        raise UnrecognizedAttribute(
            f"'{arg.name}' is not a valid cfg combinator "
            f"(expected {', '.join(CFG_COMBINATORS)})",
            arg.line, arg.column,
        )
    args = tuple(_predicate(a) for a in arg.args)
    if arg.name == "not" and len(args) != 1:
        raise InvalidArgument(
            "the 'not' cfg predicate expects exactly one argument",
            arg.line, arg.column,
        )
    return CfgPredicate(arg.name, args=args)


def _predicates(attr: Attr) -> tuple[CfgPredicate, ...]:
    if not attr.args:
        raise MissingArgument(
            "expects a predicate, e.g. "
            '#[cfg(target_os = "windows")]',
            attr.line, attr.column,
        )
    return tuple(_predicate(a) for a in attr.args)


def _apply(ctx: ProcCtx, item: Any, attr: Attr) -> None:
    """``cfg`` annotates nothing; it only filters (see ``_keep``).

    Running the predicate grammar here (rather than only in ``_keep``) is
    what makes a malformed ``#[cfg(...)]`` an error even when the filter
    would have dropped the item anyway.
    """
    _predicates(attr)


def _keep(ctx: ProcCtx, attr: Attr) -> bool:
    cfg_ctx = ctx.cfg_context()
    assert isinstance(cfg_ctx, CfgContext)
    return all(evaluate_cfg(p, cfg_ctx) for p in _predicates(attr))


register(AttrProc(
    name="cfg",
    positions=frozenset({ITEM, EXTERN_MEMBER, USE, METHOD}),
    apply=_apply,
    keep=_keep,
    doc="compile-time configuration filter",
))
