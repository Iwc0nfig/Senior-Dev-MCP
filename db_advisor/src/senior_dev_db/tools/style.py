"""get_house_style: measured conventions, idioms with examples, an exemplar and its header."""

from __future__ import annotations

from typing import Optional

from ..models import StyleOut
from .common import RO, Ctx
from .stylecheck import load_rules

KINDS = ("view", "report_proc", "proc", "function")


def house_style(ctx: Ctx, kind: str, topic: Optional[str]) -> StyleOut:
    k = kind.lower().strip()
    k = {"report": "report_proc", "procedure": "proc", "sproc": "proc", "fn": "function"}.get(k, k)
    scope = ctx.scope("conventions measured over modules modified since 2022, idiom examples, C# framework anchors")
    if k not in KINDS:
        return StyleOut(status="no_results", layer="sql-parse", searched_scope=scope, kind=kind,
                        caveats=[f"kind must be one of {', '.join(KINDS)}"])
    rules = [f"{r[0]}" + (f" (share {r[1]:.0%})" if r[1] is not None else "") + (f" e.g. {r[2]}" if r[2] else "")
             for r in ctx.q("SELECT rule, share, evidence FROM style_rules WHERE kind IN (?, 'any') ORDER BY kind<>'any', topic", k)]
    rules.append("Deliverable (team convention for change scripts; not measurable from the catalog, which stores "
                 "CREATE text): an SSMS-style ALTER script of the whole object (SET ANSI_NULLS ON / GO / "
                 "SET QUOTED_IDENTIFIER ON / GO / ALTER ... AS ... / GO) written to the team rules, each changed "
                 "line marked with the task comment.")
    team_rules, team_example = load_rules(ctx.cfg.paths.style_rules)
    fw = [f"{r[0]} [{r[1]}]" for r in ctx.q("SELECT rule, evidence FROM style_rules WHERE kind='framework'")]
    idioms = []
    for r in ctx.q("SELECT topic, rule, evidence FROM style_rules WHERE kind='idiom'" + (" AND topic LIKE ?" if topic else ""),
                   *((f"%{topic}%",) if topic else ())):
        ex = (r[2] or "").split("\n")
        idioms.append(f"[{r[0]}] {r[1]}" + ("".join(f"\n    {e}" for e in ex[:3 if topic else 1]) if ex and ex[0] else ""))
    ex = ctx.q1("SELECT obj_name, reason FROM style_exemplars WHERE kind=? ORDER BY rowid LIMIT 1", k)
    skeleton = None
    if ex:
        o = ctx.obj(ex[0])
        if o:
            lines = ctx.module_lines(o["obj_id"])
            head = []
            for i, line in enumerate(lines[:40], 1):
                head.append(f"{i}\t{line}")
                if k != "report_proc" and i >= 12:
                    break
                if k == "report_proc" and i > 8 and line.strip().lower().startswith(("select", ";with", "with ", "insert")):
                    break
            skeleton = "\n".join(head)
    caveats = ctx.masked_caveats()
    if team_rules:
        caveats.append("team_rules were written by the DB developers and win over the measured conventions; "
                       "validate_sql reports deviations from them under 'style'.")
    else:
        caveats.append(f"{ctx.cfg.paths.style_rules.name} not found: only measured conventions are available.")
    if topic and not idioms:
        caveats.append(f"No idiom topic matches '{topic}'. Topics: " + ", ".join(
            r[0] for r in ctx.q("SELECT topic FROM style_rules WHERE kind='idiom'")))
    return StyleOut(status="ok", layer="sql-parse", searched_scope=scope, kind=k, team_rules=team_rules,
                    team_example=team_example or None, rules=rules + fw, idioms=idioms,
                    exemplar=f"{ex[0]} ({ex[1]})" if ex else None, skeleton=skeleton, caveats=caveats)


def register(mcp, ctx: Ctx) -> None:
    @mcp.tool(name="get_house_style", annotations=RO, description=(
        "How this team writes SQL: first the team_rules written by the DB developers (authoritative: "
        "lowercase keywords, one column per line with leading commas, short aliases, hints after the alias, "
        "cross join, conditions on own lines) with their example; then conventions measured over recent "
        "modules: report-procedure header and parameters (@warehouses, @df/@dt, @datefield, @userid -> "
        "fn_userwarehouses), task-comment format, and the C# framework anchors (how grids/reports call the "
        "SQL). kind: view|report_proc|proc|function. topic (optional): currency, warehouse_security, "
        "date_filter, refund_sign, rounding, date_clean, warehouses_param, outer_apply, task_comment, "
        "age_buckets -> example lines. Returns an exemplar object and its header lines."))
    def get_house_style(kind: str, topic: Optional[str] = None) -> StyleOut:
        return house_style(ctx, kind, topic)
