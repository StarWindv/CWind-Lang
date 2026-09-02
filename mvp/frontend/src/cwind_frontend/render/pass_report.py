"""Pure renderers for optimization-pass reports (todo-160).

数据与渲染分离: 报告数据 (名字 / 文件 / 位置 / 原始拼写 / 规范形式) 由
pass 本身产出; 本模块只负责把拿到的数据排版成控制台文本 —— 不解析路
径、不查询符号、不知道任何名字的语义。
"""

from __future__ import annotations

from typing import Any

__all__ = ["render_fqn_report"]


def _site(entry: dict[str, Any]) -> str:
    """Lay out the source-position column from the data's own fields."""
    source = str(entry.get("source") or "<stdin>")
    return f"{source}:{entry.get('line', '?')}:{entry.get('column', '?')}"


def render_fqn_report(report: dict[str, Any]) -> str:
    """Format the pass-0 (FQN expansion) report table.

    ``report`` is the structured data produced by the pass:
    ``{"pass": {"id", "name"}, "expansions": [{kind, original, canonical,
    source, line, column}], "aliases": [{name, params, target, source,
    line, column}], "traits_excluded": [..]}``.
    """
    meta = report.get("pass") or {}
    lines: list[str] = [
        f"pass {meta.get('id', '?')} ({meta.get('name', '?')})"
    ]

    expansions = list(report.get("expansions") or [])
    lines.append(f"{len(expansions)} reference(s) rewritten")
    if expansions:
        rows = [
            (
                str(e.get("kind", "?")),
                _site(e),
                str(e.get("original", "?")),
                str(e.get("canonical", "?")),
            )
            for e in expansions
        ]
        w_kind = max(len(r[0]) for r in rows)
        w_site = max(len(r[1]) for r in rows)
        w_orig = max(len(r[2]) for r in rows)
        header = (
            f"  {'KIND':<{w_kind}}  {'SITE':<{w_site}}  "
            f"{'ORIGINAL':<{w_orig}}  ->  CANONICAL"
        )
        lines.append("")
        lines.append(header)
        lines.append("  " + "-" * (len(header) - 2))
        for kind, site, original, canonical in rows:
            lines.append(
                f"  {kind:<{w_kind}}  {site:<{w_site}}  "
                f"{original:<{w_orig}}  ->  {canonical}"
            )

    aliases = list(report.get("aliases") or [])
    lines.append("")
    lines.append(f"alias table ({len(aliases)} entr"
                 f"{'y' if len(aliases) == 1 else 'ies'})")
    if aliases:
        rows = [
            (
                (
                    f"{a.get('name', '?')}"
                    f"<{', '.join(a.get('params') or [])}>"
                    if a.get("params")
                    else str(a.get("name", "?"))
                ),
                str(a.get("target", "?")),
                (
                    f"{str(a.get('source') or '<stdin>')}:"
                    f"{a.get('line', '?')}:{a.get('column', '?')}"
                ),
            )
            for a in aliases
        ]
        w_name = max(len(r[0]) for r in rows)
        w_target = max(len(r[1]) for r in rows)
        for name, target, site in rows:
            lines.append(f"  {name:<{w_name}}  = {target:<{w_target}}  @ {site}")

    excluded = list(report.get("traits_excluded") or [])
    lines.append("")
    lines.append(f"trait exclusions ({len(excluded)}): {', '.join(excluded)}")

    return "\n".join(lines)
