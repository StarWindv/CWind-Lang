"""Pure constant-folding and expression-rendering helpers for SA."""

from __future__ import annotations

from typing import Optional, Union

from ..ast_components.ast import (
    BinOp,
    Block,
    BoolLit,
    FloatLit,
    ForStmt,
    IfLetStmt,
    IfStmt,
    IntLit,
    MatchStmt,
    Name,
    Node,
    ReturnStmt,
    StrLit,
    UnaryOp,
    WhileStmt,
)
from ..ast_components.token import TokenKind


def _const_int(
    expr: Node,
    consts: Optional[dict[str, int]] = None,
) -> Optional[int]:
    """Constant-fold an integer expression: literals, unary/binary arithmetic
    on foldable operands, and references to previously folded consts."""
    if isinstance(expr, IntLit):
        return expr.value
    if isinstance(expr, UnaryOp):
        operand = _const_int(expr.operand, consts)
        if operand is None:
            return None
        if expr.op == TokenKind.MINUS:
            return -operand
        if expr.op == TokenKind.PLUS:
            return operand
        return None
    if isinstance(expr, BinOp):
        left = _const_int(expr.left, consts)
        right = _const_int(expr.right, consts)
        if left is None or right is None:
            return None
        op = expr.op
        if op == TokenKind.PLUS:
            return left + right
        if op == TokenKind.MINUS:
            return left - right
        if op == TokenKind.STAR:
            return left * right
        if op == TokenKind.SLASH:
            return left // right if right != 0 else None
        if op == TokenKind.PERCENT:
            return left % right if right != 0 else None
        if op == TokenKind.SHL:
            return left << right
        if op == TokenKind.SHR:
            return left >> right
        if op == TokenKind.AMP:
            return left & right
        if op == TokenKind.PIPE:
            return left | right
        if op == TokenKind.CARET:
            return left ^ right
        return None
    if isinstance(expr, Name) and len(expr.parts) == 1 and consts is not None:
        return consts.get(expr.parts[0])
    return None


def _const_number(
    expr: Node,
    int_consts: Optional[dict[str, int]] = None,
    float_consts: Optional[dict[str, float]] = None,
) -> Optional[Union[int, float]]:
    """Constant-fold a numeric expression to an int or float.

    Extends :func:`_const_int` with float literals and float arithmetic.
    Integer-only operators (shifts, bitwise ops) keep integer semantics and
    yield ``None`` when an operand is a float; division by zero yields
    ``None`` so callers do not have to worry about exceptions.
    """
    if isinstance(expr, IntLit):
        return expr.value
    if isinstance(expr, FloatLit):
        return expr.value
    if isinstance(expr, UnaryOp):
        operand = _const_number(expr.operand, int_consts, float_consts)
        if operand is None:
            return None
        if expr.op == TokenKind.MINUS:
            return -operand
        if expr.op == TokenKind.PLUS:
            return operand
        return None
    if isinstance(expr, BinOp):
        left = _const_number(expr.left, int_consts, float_consts)
        right = _const_number(expr.right, int_consts, float_consts)
        if left is None or right is None:
            return None
        op = expr.op
        if op == TokenKind.PLUS:
            return left + right
        if op == TokenKind.MINUS:
            return left - right
        if op == TokenKind.STAR:
            return left * right
        if op == TokenKind.SLASH:
            if right == 0:
                return None
            if isinstance(left, int) and isinstance(right, int):
                return left // right
            return left / right
        if op == TokenKind.PERCENT:
            if right == 0:
                return None
            return left % right
        if isinstance(left, int) and isinstance(right, int):
            if op == TokenKind.SHL:
                return left << right
            if op == TokenKind.SHR:
                return left >> right
            if op == TokenKind.AMP:
                return left & right
            if op == TokenKind.PIPE:
                return left | right
            if op == TokenKind.CARET:
                return left ^ right
        return None
    if isinstance(expr, Name) and len(expr.parts) == 1:
        n = expr.parts[0]
        if int_consts is not None and n in int_consts:
            return int_consts[n]
        if float_consts is not None and n in float_consts:
            return float_consts[n]
    return None


def _eval_refinement(
    expr: Node,
    var_name: str,
    value: Union[int, float, str, bool],
    int_consts: Optional[dict[str, int]] = None,
    float_consts: Optional[dict[str, float]] = None,
) -> Optional[bool]:
    """Evaluate one refinement predicate with the validated value bound.

    Returns ``True`` / ``False`` when the predicate can be decided at compile
    time and ``None`` when it cannot (runtime-only checks stay silent here).
    """
    if isinstance(expr, BoolLit):
        return expr.value
    if isinstance(expr, Name) and len(expr.parts) == 1:
        n = expr.parts[0]
        if n == var_name:
            return value
        if int_consts is not None and n in int_consts:
            return int_consts[n]
        if float_consts is not None and n in float_consts:
            return float_consts[n]
        return None
    if isinstance(expr, IntLit):
        return expr.value
    if isinstance(expr, FloatLit):
        return expr.value
    if isinstance(expr, StrLit):
        return expr.value
    if isinstance(expr, UnaryOp):
        operand = _eval_refinement(
            expr.operand, var_name, value, int_consts, float_consts
        )
        if operand is None:
            return None
        if expr.op == TokenKind.NOT:
            return not bool(operand)
        if expr.op == TokenKind.MINUS:
            return -operand
        if expr.op == TokenKind.PLUS:
            return operand
        return None
    if isinstance(expr, BinOp):
        op = expr.op
        if op in (TokenKind.AND, TokenKind.OR):
            left = _eval_refinement(
                expr.left, var_name, value, int_consts, float_consts
            )
            right = _eval_refinement(
                expr.right, var_name, value, int_consts, float_consts
            )
            if op == TokenKind.AND:
                if left is False or right is False:
                    return False
                if left is True and right is True:
                    return True
                return None
            if left is True or right is True:
                return True
            if left is False and right is False:
                return False
            return None
        left = _eval_refinement(
            expr.left, var_name, value, int_consts, float_consts
        )
        right = _eval_refinement(
            expr.right, var_name, value, int_consts, float_consts
        )
        if left is None or right is None:
            return None
        if op in (
            TokenKind.LT, TokenKind.GT, TokenKind.LE, TokenKind.GE,
            TokenKind.EQ, TokenKind.NE, TokenKind.NOT_LT, TokenKind.NOT_GT,
        ):
            try:
                if op == TokenKind.LT:
                    return left < right
                if op == TokenKind.GT:
                    return left > right
                if op == TokenKind.LE:
                    return left <= right
                if op == TokenKind.GE:
                    return left >= right
                if op == TokenKind.EQ:
                    return left == right
                if op == TokenKind.NE:
                    return left != right
                if op == TokenKind.NOT_LT:
                    return left >= right
                if op == TokenKind.NOT_GT:
                    return left <= right
            except TypeError:
                return None
        if op in (TokenKind.PLUS, TokenKind.MINUS, TokenKind.STAR,
                  TokenKind.SLASH, TokenKind.PERCENT):
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                try:
                    if op == TokenKind.PLUS:
                        return left + right
                    if op == TokenKind.MINUS:
                        return left - right
                    if op == TokenKind.STAR:
                        return left * right
                    if op == TokenKind.SLASH:
                        if right == 0:
                            return None
                        if isinstance(left, int) and isinstance(right, int):
                            return left // right
                        return left / right
                    if op == TokenKind.PERCENT:
                        if right == 0:
                            return None
                        return left % right
                except (ZeroDivisionError, TypeError, OverflowError):
                    return None
        return None
    return None


def _expr_str(node: Node) -> str:
    """Render a condition back to source-like text for messages."""
    if isinstance(node, Name):
        return "::".join(node.parts)
    if isinstance(node, IntLit):
        return str(node.value)
    if isinstance(node, FloatLit):
        return repr(node.value)
    if isinstance(node, StrLit):
        return repr(node.value)
    if isinstance(node, BoolLit):
        return "true" if node.value else "false"
    if isinstance(node, UnaryOp):
        return f"({node.op.value}{_expr_str(node.operand)})"
    if isinstance(node, BinOp):
        return f"({_expr_str(node.left)} {node.op.value} {_expr_str(node.right)})"
    return "<expr>"


def _has_return(stmt: Node) -> bool:
    """Whether a statement subtree contains any ``return``."""
    if isinstance(stmt, ReturnStmt):
        return True
    if isinstance(stmt, Block):
        return any(_has_return(s) for s in stmt.stmts)
    if isinstance(stmt, IfStmt):
        return (
            _has_return(stmt.then)
            or any(_has_return(e.body) for e in stmt.elifs)
            or (stmt.else_ is not None and _has_return(stmt.else_))
        )
    if isinstance(stmt, IfLetStmt):
        return (
            _has_return(stmt.then)
            or any(
                _has_return(b.body) for b in stmt.elifs
            )
            or (stmt.else_ is not None and _has_return(stmt.else_))
        )
    if isinstance(stmt, MatchStmt):
        return any(_has_return(a.body) for a in stmt.arms)
    if isinstance(stmt, WhileStmt):
        return _has_return(stmt.body)
    if isinstance(stmt, ForStmt):
        return _has_return(stmt.body)
    return False


def _match_arg_patterns(
    patterns: tuple[tuple[Optional[int], str], ...],
    nargs: int,
) -> Optional[list[str]]:
    """Return the expected argument types for a call of ``nargs`` arguments.

    ``patterns`` comes from :meth:`MethodSpec.patterns`; an unbounded tail
    pattern (``*: Type``) absorbs any remaining arguments.  Returns ``None``
    when the arity does not fit the declared patterns.
    """
    expected: list[str] = []
    for count, typ in patterns:
        if count is None:  # unbounded tail; validated to be last
            if nargs < len(expected):
                return None
            return expected + [typ] * (nargs - len(expected))
        expected.extend([typ] * count)
    return expected if len(expected) == nargs else None


def _patterns_arity_text(patterns: tuple[tuple[Optional[int], str], ...]) -> str:
    """Human-readable arity description for error messages."""
    fixed = sum(c for c, _ in patterns if c is not None)
    unbounded = any(c is None for c, _ in patterns)
    if unbounded:
        return "0 or more argument(s)" if fixed == 0 else f"at least {fixed} argument(s)"
    return f"{fixed} argument(s)"
