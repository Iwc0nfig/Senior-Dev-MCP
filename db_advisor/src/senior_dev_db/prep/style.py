"""Phase style: house conventions mined from the modules themselves, idiom
examples, exemplars, and framework anchors located in the C# code.

Every rule carries the share of modules that follow it and an example citation,
so "house style" is measured, not asserted.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections import Counter

from ..config import Config
from .. import db as dbm

log = logging.getLogger("prep.style")

IDIOMS = {
    "currency": (r"\b\w+Local\b", "amounts converted to the system currency (*Local columns)"),
    "warehouse_security": (r"fn_userwarehouses", "restrict rows to the user's warehouses"),
    "date_filter": (r"@datefield|\bbetween\s+@df\s+and\s+@dt|>=\s*@df", "report date-range filtering"),
    "refund_sign": (r"PaymentType\s*=\s*2|PaymentType\s+in\s*\(", "payments vs refunds sign / filter"),
    "rounding": (r"fn_BankersRound", "banker's rounding helper"),
    "date_clean": (r"\w+Clean\b", "date-only (Clean) columns"),
    "warehouses_param": (r"@warehouses", "multi-warehouse parameter parsing"),
    "outer_apply": (r"outer\s+apply", "OUTER APPLY sub-aggregations"),
    "task_comment": (r"--\s*Task\s*:?\s*\d+", "change comments with the task id"),
    "age_buckets": (r"datediff\s*\(\s*day", "day-difference buckets"),
}

FRAMEWORK = [
    # topic, path, regex, rule[, occurrence]  -- the cited line is the occurrence-th regex match
    ("grid_query", "Legacy/Shared/Entities/GridQueryBuilder.cs", r"with \(nolock\)|with\(nolock\)",
     "MasterView grids query the SQL view by name, selecting only the registered columns WITH (NOLOCK)"),
    ("grid_query", "Legacy/App/ui/views/GridView.aspx.cs",
     r"WarehouseLimitField \+ \" in \(\" \+ warehouses",
     "on a normal grid load (GetDataAdvanced) rows are restricted to the warehouses the page sends: "
     "WarehouseLimitField in (<selected warehouses>); if no warehouse is selected this clause is skipped", 1),
    ("grid_query", "Legacy/App/ui/views/GridView.aspx.cs",
     r"WarehouseLimitField \+ \" in \(\" \+ warehouses",
     "the search box and Excel export (GetDataSimple) apply the same selected-warehouses clause", 2),
    ("grid_query", "Legacy/Shared/Entities/GridQueryBuilder.cs",
     r"WarehouseLimitField\.IsNullOrEmpty\(\) && !\(queryParams\.searchBy",
     "GridQueryBuilder adds WarehouseLimitField in (select Code from dbo.fn_userwarehouses(<user>)) ONLY when a search "
     "is applied (searchBy set and not by the key field) -- it is not applied on every load"),
    ("grid_query", "Modern/Modern.Next/Modern.Next.BL/UseCases/Views/GridViewsUseCase.cs",
     r"WarehouseLimitField\} in \(",
     "net8 grids: the same selected-warehouses clause (WarehouseLimitField in (<warehouses>))"),
    ("report_exec", "Legacy/Shared/Reporting/ReportRunner.cs", r"\"exec \"",
     "stored-procedure reports run as 'exec <ObjectName> @filter=value, ...' (one value per report Filter)"),
    ("report_exec", "Modern/Modern.Api/Reporting/ReportRunner.cs", r"\"exec \"",
     "stored-procedure reports run as 'exec <ObjectName> @filter=value, ...' (net8)"),
]


def _share(n: int, d: int) -> float:
    return round(n / d, 3) if d else 0.0


def _framework(cfg: Config, con: sqlite3.Connection) -> int:
    n = 0
    for entry in FRAMEWORK:
        topic, rel, rx, rule = entry[:4]
        occurrence = entry[4] if len(entry) > 4 else 1
        p = cfg.paths.workspaces / rel
        if not p.is_file():
            hits = list((cfg.paths.workspaces / rel.split("/")[0]).rglob(rel.split("/")[-1]))[:1]
            if not hits:
                continue
            p = hits[0]
            rel = p.relative_to(cfg.paths.workspaces).as_posix()
        lines = p.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        seen = 0
        for i, line in enumerate(lines, 1):
            if re.search(rx, line, re.I):
                seen += 1
                if seen < occurrence:
                    continue
                con.execute("INSERT INTO style_rules VALUES (?,?,?,?,?)",
                            ("framework", topic, rule, f"{rel}:{i}: {line.strip()[:160]}", None))
                n += 1
                break
    return n


def run(cfg: Config, con: sqlite3.Connection) -> dict:
    dbm.clear_tables(con, ["style_rules", "style_exemplars"])
    mods = con.execute("""SELECT o.name, o.kind, o.modified, o.line_count, m.definition FROM modules m
                          JOIN objects o ON o.obj_id=m.obj_id WHERE o.kind IN ('view','proc','scalar_fn','inline_tvf','tvf')""").fetchall()
    recent = [m for m in mods if (m[2] or "") >= cfg.profile.style_recent_since]
    groups = {
        "view": [m for m in recent if m[1] == "view"],
        "report_proc": [m for m in recent if m[1] == "proc" and m[0].lower().startswith("usp_rpt")],
        "proc": [m for m in recent if m[1] == "proc" and not m[0].lower().startswith("usp_rpt")],
        "function": [m for m in recent if m[1] in ("scalar_fn", "inline_tvf", "tvf")],
    }
    rules = []
    for kind, ms in groups.items():
        d = len(ms)
        if not d:
            continue
        low = sum(1 for m in ms if re.search(r"\bselect\b", m[4]) and not re.search(r"\bSELECT\b", m[4]))
        upp = sum(1 for m in ms if re.search(r"\bSELECT\b", m[4]) and not re.search(r"\bselect\b", m[4]))
        rules.append((kind, "keyword_case",
                      f"keywords mostly lower-case (select/from/left outer join): {low} of {d} recent modules "
                      f"lower-case only, {upp} upper-case only", _share(low, d)))
        has_from = [m for m in ms if re.search(r"\bfrom\b", m[4], re.I)]
        nl = sum(1 for m in has_from if re.search(r"with\s*\(\s*nolock\s*\)", m[4], re.I))
        rules.append((kind, "nolock", f"every table read uses WITH (NOLOCK): {nl} of {len(has_from)} recent modules", _share(nl, len(has_from))))
        lead = trail = 0
        for m in ms:
            for line in m[4].split("\n"):
                t = line.strip()
                if t.startswith(",") and len(t) > 1:
                    lead += 1
                elif t.endswith(",") and not t.startswith("--"):
                    trail += 1
        rules.append((kind, "commas", f"select lists use leading commas, one column per line ({lead} leading vs "
                      f"{trail} trailing comma lines)", _share(lead, lead + trail)))
        ap = sum(1 for m in ms if re.search(r"outer\s+apply", m[4], re.I))
        rules.append((kind, "outer_apply", f"OUTER APPLY used for per-row sub-aggregates in {ap} of {d}", _share(ap, d)))
        if kind in ("report_proc", "proc"):
            for stmt in ("set nocount on", "set arithabort on"):
                k = sum(1 for m in ms if stmt in m[4].lower())
                rules.append((kind, "header", f"body starts with '{stmt}': {k} of {d}", _share(k, d)))
        if kind == "report_proc":
            us = sum(1 for m in ms if "fn_userwarehouses" in m[4].lower())
            rules.append((kind, "security", f"warehouse security via dbo.fn_userwarehouses(@userid): {us} of {d}", _share(us, d)))
            pc = Counter()
            for m in ms:
                o = con.execute("SELECT obj_id FROM objects WHERE name=?", (m[0],)).fetchone()
                for (pn, pt) in con.execute("SELECT name, type_name FROM params WHERE obj_id=? AND param_id>0", (o[0],)):
                    pc[f"{pn} {pt}"] += 1
            rules.append((kind, "params", "most common parameters: " + ", ".join(f"{k} ({v})" for k, v in pc.most_common(10)),
                          None))
    # task comments: exact format
    fmt = Counter()
    ex = {}
    for m in mods:
        for line in re.findall(r"--\s*Task\s*:?\s*\d+[^\n]{0,80}", m[4], re.I):
            f = re.sub(r"\d+", "<id>", line.strip())
            f = re.sub(r"(<id>\s*\|?\s*).*", r"\1<description>", f)
            fmt[f] += 1
            ex.setdefault(f, f"{m[0]}: {line.strip()[:100]}")
    for f, c in fmt.most_common(3):
        rules.append(("any", "task_comment", f"change marker comment format '{f}' ({c} uses), at the end of the changed line",
                      None))
    for kind, topic, rule, share in rules:
        evidence = None
        if topic == "task_comment":
            f = rule.split("'")[1]
            evidence = ex.get(f)
        con.execute("INSERT INTO style_rules VALUES (?,?,?,?,?)", (kind, topic, rule, evidence, share))

    # idioms: examples from the most recently modified modules
    order = sorted(mods, key=lambda m: m[2] or "", reverse=True)
    for topic, (rx, desc) in IDIOMS.items():
        r = re.compile(rx, re.I)
        users = [m for m in order if r.search(m[4])]
        examples = []
        for m in users[:40]:
            for i, line in enumerate(m[4].split("\n"), 1):
                if r.search(line):
                    examples.append(f"{m[0]}:{i}: {line.strip()[:150]}")
                    break
            if len(examples) >= 4:
                break
        con.execute("INSERT INTO style_rules VALUES (?,?,?,?,?)",
                    ("idiom", topic, f"{desc}: used in {len(users)} modules", "\n".join(examples), _share(len(users), len(mods))))

    # exemplars: recent, mid-sized, following the majority conventions
    for kind, ms in groups.items():
        cands = []
        for m in ms:
            if not (40 <= (m[3] or 0) <= 220):
                continue
            score = (bool(re.search(r"with\s*\(\s*nolock", m[4], re.I)) + bool(re.search(r"(?m)^\s*,\s*\w", m[4]))
                     + bool(re.search(r"--\s*Task\s*:?\s*\d+", m[4], re.I))
                     + (kind != "report_proc" or "fn_userwarehouses" in m[4].lower()))
            cands.append((score, m[2] or "", m[0]))
        for score, mod, name in sorted(cands, reverse=True)[:3]:
            con.execute("INSERT INTO style_exemplars VALUES (?,?,?)", (kind, name, f"modified {mod}, convention score {score}/4"))
    n_fw = _framework(cfg, con)
    con.commit()
    return {"rules": con.execute("SELECT count(*) FROM style_rules").fetchone()[0], "framework_anchors": n_fw,
            "exemplars": con.execute("SELECT count(*) FROM style_exemplars").fetchone()[0]}
