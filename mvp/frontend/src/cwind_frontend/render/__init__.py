"""Diagnostic / report rendering for the CWind frontend.

一个模块族只做一件事: 把**已经算好的数据**排版成给控制台的文本。
渲染器不解析路径、不查符号、不做任何名字/文件归属判断 —— 职责分离
(todo-160): 数据归 pass / 分析器, 排版归这里。

- :mod:`errors` — ariadne 风格的诊断渲染 (lex/parse/SA 错误与警告);
- :mod:`pass_report` — 优化 pass 报告表 (如 ``--pass 0`` 的 FQN 展开
  表);
- :mod:`module_tree` — ``cargo tree`` 风格的模块树渲染 (数据来自
  :mod:`module_tree` 的构建器)。
"""

from .errors import offset_for_position, render_error, render_warning
from .module_tree import render_module_tree
from .pass_report import render_fqn_report

__all__ = [
    "offset_for_position",
    "render_error",
    "render_fqn_report",
    "render_module_tree",
    "render_warning",
]
