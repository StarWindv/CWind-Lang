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


def _fold_expansions(
    expansions: list[dict[str, Any]],
) -> list[tuple[str, str, str, str]]:
    """Collapse same-file / same-kind / same-spelling rewrites into one
    row (todo-160): the per-reference ``file:line:column`` is dropped, the
    file path stays.  Written spellings fold separately — ``Vec`` and
    ``Vector`` rows never merge, even when they canonicalize alike.
    """
    rows: list[tuple[str, str, str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for e in expansions:
        source = str(e.get("source") or "<stdin>")
        key = (
            str(e.get("kind", "?")),
            source,
            str(e.get("original", "?")),
            str(e.get("canonical", "?")),
        )
        if key in seen:
            continue
        seen.add(key)
        rows.append((key[0], source, key[2], key[3]))
    return rows


def render_fqn_report(report: dict[str, Any], fold: bool = True) -> str:
    """Format the pass-0 (FQN expansion) report table.

    ``report`` is the structured data produced by the pass:
    ``{"pass": {"id", "name"}, "expansions": [{kind, original, canonical,
    source, line, column}], "aliases": [...], "methods": [...],
    "traits": [...]}``.  ``fold`` collapses same-file / same-kind /
    same-spelling expansion rows (the default); ``--no-fold`` keeps every
    reference on its own line with its position.
    """
    meta = report.get("pass") or {}
    lines: list[str] = [
        f"pass {meta.get('id', '?')} ({meta.get('name', '?')})"
    ]

    expansions = list(report.get("expansions") or [])
    if fold:
        rows = _fold_expansions(expansions)
        lines.append(f"{len(expansions)} reference(s) rewritten "
                     f"({len(rows)} distinct)")
    else:
        rows = [
            (
                str(e.get("kind", "?")),
                _site(e),
                str(e.get("original", "?")),
                str(e.get("canonical", "?")),
            )
            for e in expansions
        ]
        lines.append(f"{len(expansions)} reference(s) rewritten")
    if rows:
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

    methods = list(report.get("methods") or [])
    lines.append("")
    lines.append(f"methods ({len(methods)})")
    if methods:
        rows = [
            (
                f"{m.get('owner', '?')}::{m.get('name', '?')}",
                str(m.get("trait")) if m.get("trait") else "",
                _site(m),
            )
            for m in methods
        ]
        w_name = max(len(r[0]) for r in rows)
        w_trait = max(len(r[1]) for r in rows) if rows else 0
        for name, trait, site in rows:
            trait_part = f"  [{trait}]" if trait else ""
            lines.append(f"  {name:<{w_name}}{trait_part}  @ {site}")

    traits = list(report.get("traits") or [])
    lines.append("")
    lines.append(f"traits ({len(traits)})")
    if traits:
        rows = [
            (
                str(t.get("name", "?")),
                str(t.get("fqn", "?")),
                _site(t),
            )
            for t in traits
        ]
        w_name = max(len(r[0]) for r in rows)
        w_fqn = max(len(r[1]) for r in rows)
        for name, fqn, site in rows:
            lines.append(f"  {name:<{w_name}}  = {fqn:<{w_fqn}}  @ {site}")

    return "\n".join(lines)
