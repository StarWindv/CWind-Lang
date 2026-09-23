"""Front-end AST optimization passes (SA-complete tree rewrites).

SA 完成后的树改写, 与 ``_emit_which_hooks`` 同纪律: 产物带完整
ann (type/call), 新名字经 ``_fresh_desugar_name`` 卫生化, 新节点经
``_assign_synthetic_ids`` 进节点池, 后端只消费 ann 不做第二次解析。
SA 已把调用点解析成 ``ann.call = {callee_kind: "fn", callee_ref:
<fn._typed_id>}``, self-call 判定就是 ``callee_ref == fn._typed_id``。

当前 pass (对齐 rustc -O 下 fib 的 accumulator 循环形态, 见
.bench/fib42.rs.ll):

* 重结合 + 左倾链 (reassociation / left-leaning chain): 函数体为
  「前置语句块 + 尾 return = self(a) + self(b)」形态 (a/b 是形参
  的纯标量表达式), 且前置块里存在基例 return (非 self-call、值
  是形参的纯标量表达式) 时::

      fn f(p) { A(p)...; if c { return base(p); }
                return f(a(p)) + f(b(p)); }
  →   fn f(p) { let mut acc = 0; loop {
                    A(p)...                      (每轮重判)
                    if c { return acc + base(p); }
                    acc = acc + f(a(p));
                    p = b(p); } }

  单 self-call + 参数滚动, 调用深度从 2^n 降到 ~n/2 (fib: 42 层
  → 21 层), 且左倾调用是后端/LLVM 可尾调用优化的形态。

保守面: 整函数体匹配才改写, 不匹配原样保留; 仅单标量值形参;
前置块出现任何 self-call 或基例值非纯标量表达式即放弃

Split: :mod:`.match` (predicates) / :mod:`.emit` (tree building).

Todo: 更加疯狂、激进的优化
"""

from __future__ import annotations

from typing import Any

from ...ast_components.ast import FnDecl, Program
from .emit import emit_reassociation
from .match import match_reassoc

__all__ = ["optimize_reassociation"]


def _try_rewrite(
    az: Any, program: Program, fn: FnDecl
) -> bool:
    outcome = match_reassoc(fn)
    if outcome is None:
        return False
    return emit_reassociation(
        az, program, fn, outcome.a_expr, outcome.b_expr, outcome.prefix
    )


def optimize_reassociation(program: Program, analyzer: Any) -> None:
    """Rewrite every matching ``FnDecl`` in *program* (in place)."""
    files = getattr(program, "_module_file_programs", None)
    if isinstance(files, dict):
        for child in files.values():
            for item in list(child.items):
                if isinstance(item, FnDecl) and item.body is not None:
                    _try_rewrite(analyzer, program, item)
    for item in list(program.items):
        if isinstance(item, FnDecl) and item.body is not None:
            _try_rewrite(analyzer, program, item)
