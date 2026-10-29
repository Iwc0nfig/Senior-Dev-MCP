"""Column-level tools: describe_column, get_value_map, find_column_usage."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Optional

from ..models import ColumnOut, UsageOut, ValueMapOut, ValueSource
from .common import RO, Ctx, strip_name
from .lineage import CATALOGUE_MIN


def _split(table: str, column: Optional[str]) -> tuple[str, Optional[str]]:
    if column is None and "." in table:
        t, _, c = strip_name(table).rpartition(".")
        return t, c
    return strip_name(table), column


def _sources(ctx: Ctx, table: str, col: str) -> list[ValueSource]:
    out = []
    for r in ctx.q("""SELECT cvm.map_id, cvm.via, cvm.evidence, cvm.confidence, vm.source, vm.name, vm.rel_path, vm.line, vm.side
                      FROM column_value_maps cvm JOIN value_maps vm ON vm.map_id=cvm.map_id
                      WHERE cvm.tbl=? COLLATE NOCASE AND cvm.col=? COLLATE NOCASE
                      ORDER BY CASE cvm.confidence WHEN 'exact' THEN 0 WHEN 'strong' THEN 1 WHEN 'curated' THEN 2 ELSE 3 END""", table, col):
        vals = ctx.value_map_items(r[0])
        if len(vals) > 60:
            vals = dict(list(vals.items())[:60])
        src = f"{r[4]} {r[5]}" + (f" [{r[8]}]" if r[8] else "")
        cite = ctx.cs(r[6], r[7]) if r[6] else None
        out.append(ValueSource(source=src, via=r[1] + (f" ({r[2]})" if r[2] and r[2] != cite else ""),
                               confidence=r[3], values=vals, cite=cite))
    # collapse identical value lists from mirrored code sides into one entry
    merged: list[ValueSource] = []
    for s in out:
        twin = next((m for m in merged if m.values == s.values and m.confidence == s.confidence
                     and m.source.split(" [")[0].split(" ", 1)[0] == s.source.split(" [")[0].split(" ", 1)[0]), None)
        if twin:
            if s.cite and s.cite not in (twin.cite or ""):
                twin.cite = (twin.cite + "; " if twin.cite else "") + s.cite
            continue
        merged.append(s)
    return merged


def _norm_label(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _net8_catalogues(ctx: Ctx) -> list[str]:
    """The net8 MemType catalogues: files that register many MemTypes (each report side loads its own)."""
    return [r for r, in ctx.q("SELECT rel_path FROM value_maps WHERE source='memtype' AND side='net8' "
                              "GROUP BY rel_path HAVING count(*) >= ?", CATALOGUE_MIN)]


def _memtype_defined(ctx: Ctx, name: str, catalogues: list[str], fallback: str = "") -> str:
    """'MemType X is defined at ...', and the net8 catalogues that do not define it: a report registered on a
    side that loads only one of them cannot decode it."""
    defs = ctx.q("SELECT DISTINCT rel_path, line, side FROM value_maps WHERE source='memtype' AND name=? "
                 "COLLATE NOCASE ORDER BY side, rel_path", name)
    text = (f"MemType {name} is defined at "
            + "; ".join(f"{r}:{ln} [{sd}]" for r, ln, sd in defs[:4]) if defs else f"MemType {name} is defined at {fallback}")
    have = {r for r, _ln, _sd in defs}
    others = [r for r in catalogues if r not in have]
    if others:
        text += (f"; NOT defined in {', '.join(others[:3])} (net8 catalogue): a report side that loads only "
                 "that one cannot decode it -- check with the Code Advisor which catalogue each side loads")
    if not any(sd == "net8" for _r, _ln, sd in defs):
        text += " (no net8 definition found: the net8 report catalogues may not know it)"
    return text


def _display_hints(ctx: Ctx, table: str, col: str) -> list[str]:
    """When a MemType is linked to the column: reports and grids can show its labels themselves
    (Column ColumnType = beMemType, MemType = X), so a CASE in SQL is usually not the house way.
    Says how often reports do that, where, and whether the MemType's labels differ from the enum's."""
    out: list[str] = []
    mems = ctx.q("SELECT DISTINCT vm.map_id, vm.name, vm.rel_path, vm.line FROM column_value_maps cvm "
                 "JOIN value_maps vm ON vm.map_id=cvm.map_id WHERE cvm.tbl=? COLLATE NOCASE AND cvm.col=? COLLATE NOCASE "
                 "AND vm.source='memtype' AND cvm.confidence<>'name_match'", table, col)
    if not mems:
        return out
    catalogues = _net8_catalogues(ctx)
    seen: set[str] = set()
    for map_id, name, rel, line in mems:
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        # compare with the code enum of the same name first (OrderStatus, not OrderStatusEnum)
        enum = ctx.q1("SELECT cvm.map_id FROM column_value_maps cvm JOIN value_maps vm ON vm.map_id=cvm.map_id "
                      "WHERE cvm.tbl=? COLLATE NOCASE AND cvm.col=? COLLATE NOCASE AND vm.source='code_enum' "
                      "AND cvm.confidence<>'name_match' ORDER BY vm.name=? COLLATE NOCASE DESC, "
                      "cvm.confidence='exact' DESC LIMIT 1", table, col, name)
        enum_labels = ctx.value_map_items(enum[0]) if enum else {}
        like = f'%"MemType": "{name}"%'
        n = ctx.q1("SELECT count(*) FROM ui_report_fields WHERE props LIKE ? COLLATE NOCASE", like)[0]
        if n:
            u = ctx.q1("SELECT report_name, data_field, rel_path, line FROM ui_report_fields WHERE props LIKE ? "
                       "COLLATE NOCASE ORDER BY side DESC LIMIT 1", like)
            prec = f"{n} report column(s) use MemType {name}, e.g. report {u[0]} {u[1]} at {u[2]}:{u[3]}"
        else:
            total = ctx.q1("SELECT count(*) FROM ui_report_fields WHERE props LIKE '%beMemType%'")[0]
            u = ctx.q1("SELECT report_name, data_field, rel_path, line FROM ui_report_fields "
                       "WHERE props LIKE '%beMemType%' ORDER BY side DESC LIMIT 1")
            prec = (f"no report column uses MemType {name} yet; {total} report columns use beMemType"
                    + (f", e.g. report {u[0]} {u[1]} at {u[2]}:{u[3]}" if u else ""))
        text = (f"Reports can show these labels themselves: Column {{ ColumnType = enumColumnType.beMemType, "
                f"MemType = \"{name}\" }} ({prec})")
        grids = ctx.q("SELECT tbl, col, evidence FROM column_value_maps WHERE map_id=? AND via LIKE '%MemValues%' LIMIT 2",
                      map_id)
        if grids:
            text += "; grids show it with MemValues=" + name + ": " + ", ".join(f"{g[0]}.{g[1]} [{g[2]}]" for g in grids)
        text += ". " + _memtype_defined(ctx, name, catalogues, f"{rel}:{line}")
        labels = ctx.value_map_items(map_id)
        if enum_labels and labels and any(_norm_label(labels.get(k, "")) != _norm_label(v) for k, v in enum_labels.items()):
            text += (f"; its labels ({', '.join(f'{k}={v}' for k, v in labels.items())}) differ from the code enum's "
                     f"({', '.join(f'{k}={v}' for k, v in enum_labels.items())}): choose which the report should show")
        out.append(text + ".")
    return out


def exposed_as(ctx: Ctx, table: str, col: str, limit: int = 25) -> list[str]:
    """Outputs of views/procs that ARE this column (direct), then a count of derived ones."""
    out = []
    pat = f'%["{table}", "{col}", "object"]%'
    views = ctx.q("""SELECT DISTINCT o.name, s.out_name, s.line, s.expression, s.sources FROM sql_outputs s
                     JOIN objects o ON o.obj_id=s.obj_id
                     WHERE (s.owner='result' OR s.owner LIKE 'dynamic:%') AND s.sources LIKE ? COLLATE NOCASE ORDER BY o.kind='view' DESC, o.name""", pat)
    by_obj = defaultdict(list)
    derived = set()
    plain = re.compile(r"(\[?\w+\]?\.)?\[?" + re.escape(col) + r"\]?", re.I)
    for v, name, line, expr, srcs in views:
        if len(json.loads(srcs)) == 1 and plain.fullmatch(expr.strip()):
            by_obj[v].append(f"{name}@{line}")
        else:
            derived.add(v)
    for v, items in list(by_obj.items())[:limit]:
        o = ctx.obj(v)
        out.append(f"{o['kind'] if o else '?'} {v}: {', '.join(items[:4])}")
        for r in ctx.q("""SELECT DISTINCT c.view_name, c.caption, c.rel_path, c.line FROM ui_view_columns c
                          JOIN ui_views u ON u.view_name=c.view_name AND u.rel_path=c.rel_path
                          WHERE u.object_name=? COLLATE NOCASE AND c.field_name IN (%s) COLLATE NOCASE""" %
                       ",".join("?" * len(items)), v, *[i.split("@")[0] for i in items]):
            out.append(f"  grid screen {r[0]} column '{r[1]}' @ {ctx.cs(r[2], r[3])}")
        for r in ctx.q("""SELECT DISTINCT f.report_name, f.caption, f.rel_path, f.line FROM ui_report_fields f
                          JOIN ui_reports u ON u.report_name=f.report_name AND u.rel_path=f.rel_path
                          WHERE u.object_name=? COLLATE NOCASE AND f.data_field IN (%s) COLLATE NOCASE""" %
                       ",".join("?" * len(items)), v, *[i.split("@")[0] for i in items]):
            out.append(f"  report {r[0]} column '{r[1]}' @ {ctx.cs(r[2], r[3])}")
    if len(by_obj) > limit:
        out.append(f"... and {len(by_obj) - limit} more objects expose it directly (find_column_usage)")
    derived -= set(by_obj)
    if derived:
        out.append(f"derived from it (CASE/expressions) in {len(derived)} more objects, e.g. {', '.join(sorted(derived)[:5])}")
    return out


def describe_column(ctx: Ctx, table: str, column: Optional[str]) -> ColumnOut:
    table, column = _split(table, column)
    scope = ctx.scope("catalog columns, value lists, data profile, relationships, C# entity code")
    o = ctx.obj(table)
    c = ctx.column_row(o["obj_id"], column) if o and column else None
    if not o or not c:
        hint = []
        if o:
            hint = [r[0] for r in ctx.q("SELECT name FROM columns WHERE obj_id=? AND name LIKE ? LIMIT 8",
                                        o["obj_id"], f"%{column or ''}%")]
        return ColumnOut(status="no_results", layer="catalog", searched_scope=scope,
                         caveats=[f"No column {table}.{column}." + (f" Similar: {', '.join(hint)}" if hint else
                                                                   (f" Tables like it: {', '.join(ctx.suggest(table))}" if not o else ""))])
    tname, cname = o["name"], c["name"]
    t = c["type_name"] + (f"({c['max_length']})" if c["type_name"] in ("varchar", "char") else
                          f"({c['max_length'] // 2 if c['max_length'] > 0 else 'max'})" if c["type_name"] in ("nvarchar", "nchar") else
                          f"({c['precision']},{c['scale']})" if c["type_name"] in ("decimal", "numeric") else "")
    caveats = ctx.masked_caveats() + [r[0] for r in ctx.q(
        "SELECT detail FROM conflicts WHERE subject=? COLLATE NOCASE", f"{tname}.{cname}")]
    prof = ctx.q1("SELECT * FROM profiles WHERE obj_id=? AND column_id=?", o["obj_id"], c["column_id"])
    observed = None
    if prof:
        if prof["top_values"]:
            vals = json.loads(prof["top_values"])
            show = vals if len(vals) <= 25 else vals[:10]
            observed = ", ".join(f"{'NULL' if v is None else v} ({n})" for v, n in show)
            if len(show) < len(vals):
                observed += f" … ({prof['distinct_count']} distinct values, min {prof['min_v']}, max {prof['max_v']})"
        elif prof["distinct_count"] is not None:
            observed = f"{prof['distinct_count']} distinct, min {prof['min_v']}, max {prof['max_v']}, nulls {prof['null_count']}"
        if prof["rows_scanned"] is not None:
            observed = (observed or "") + f" of {prof['rows_scanned']} rows" + (" (sampled)" if prof["sampled"] else "")
    notes = [f"{r[0]}" + (f" [evidence: {r[1]}]" if r[1] else "") for r in ctx.q(
        "SELECT note, evidence FROM column_notes WHERE tbl=? COLLATE NOCASE AND col=? COLLATE NOCASE", tname, cname)]
    notes += [r[0] for r in ctx.q("SELECT note FROM column_notes WHERE tbl=? COLLATE NOCASE AND col='*' "
                                  "AND topic='row_kind'", tname)]
    rels = [f"-> {r[0]}.{r[1]} ({r[2]}, {r[3]}" + (f", seen {r[4]}x" if r[4] else "") + ")"
            + (f" e.g. {r[5]} [{r[6]}:{r[7]}]" if r[5] else "") + (f" {r[8]}" if r[8] else "")
            for r in ctx.q("SELECT to_table, to_column, kind, confidence, occurrences, example, example_obj, example_line, note "
                           "FROM relationships WHERE from_table=? COLLATE NOCASE AND from_column=? COLLATE NOCASE "
                           "ORDER BY declared DESC, occurrences DESC LIMIT 6", tname, cname)]
    rels += [f"<- {r[0]}.{r[1]} ({r[2]}, {r[3]})" for r in ctx.q(
        "SELECT from_table, from_column, kind, confidence FROM relationships WHERE to_table=? COLLATE NOCASE "
        "AND to_column=? COLLATE NOCASE ORDER BY declared DESC, occurrences DESC LIMIT 12", tname, cname)]
    code, seen = [], set()
    for r in ctx.q("SELECT class_fqn, property, value_type, access, rel_path, line, side FROM code_entity_columns "
                   "WHERE table_name=? COLLATE NOCASE AND column=? COLLATE NOCASE ORDER BY side, access", tname, cname):
        key = (r[0].split(".")[-1], r[3], r[2])
        if key in seen:
            continue
        seen.add(key)
        code.append(f"{ctx.cs(r[4], r[5])} {r[0].split('.')[-1]}" + (f".{r[1]}" if r[1] else "") + f" {r[3]}"
                    + (f" as {r[2]}" if r[2] else "") + f" [{r[6]}]")
        if len(code) >= 8:
            break
    meaning = [f"{r[0]} [curated; {', '.join(json.loads(r[1])[:3])}]" for r in ctx.q(
        "SELECT text, citations FROM curated WHERE subject=? COLLATE NOCASE AND fact_type IN ('meaning','value_label')",
        f"{tname}.{cname}")]
    srcs = _sources(ctx, tname, cname)
    good = [x for x in srcs if x.confidence != "name_match"] or srcs
    merged: dict[str, list[str]] = {}
    for x in good:
        for v, lab in x.values.items():
            labs = merged.setdefault(v, [])
            if lab.lower() not in (l.lower() for l in labs):
                labs.append(lab)
    values = {v: " | ".join(l) for v, l in sorted(merged.items(), key=lambda kv: (len(kv[0]), kv[0]))}
    vsrc = [f"{x.source} ({x.via}; {x.confidence})" + (f" {x.cite}" if x.cite else "") for x in srcs]
    for r in ctx.q("""SELECT DISTINCT e.name, i.member, i.label, i.rel_path, i.line FROM column_value_maps cvm
                      JOIN value_maps vm ON vm.map_id=cvm.map_id JOIN code_enums e ON e.fqn=vm.ref
                      JOIN code_enum_items i ON i.enum_id=e.enum_id AND i.value='helper'
                      WHERE cvm.tbl=? COLLATE NOCASE AND cvm.col=? COLLATE NOCASE AND cvm.confidence<>'name_match'""",
                   tname, cname):
        notes.append(f"code helper {r[0]}.{r[1]}(): {r[2]} [{ctx.cs(r[3], r[4])}]")
    notes += _display_hints(ctx, tname, cname)
    notes = list(dict.fromkeys(notes))
    return ColumnOut(status="ok", layer="mixed", searched_scope=scope, table=tname, column=cname, type=t,
                     nullable=bool(c["is_nullable"]), default=c["default_def"], description=c["description"],
                     meaning=meaning, values=values, value_sources=vsrc, observed=observed, notes=notes,
                     relationships=rels, code=code, exposed_as=exposed_as(ctx, tname, cname), caveats=caveats)


def value_map(ctx: Ctx, query: str) -> ValueMapOut:
    scope = ctx.scope("value lists: DB descriptions, C# enums/ValueEnums, MemTypes, const groups, lookup tables")
    q = strip_name(query)
    maps: list[ValueSource] = []
    used_by: list[str] = []
    caveats = ctx.masked_caveats()
    if "." in q:
        t, _, c = q.rpartition(".")
        o = ctx.obj(t)
        if o:
            maps = _sources(ctx, o["name"], c)
            used_by += _display_hints(ctx, o["name"], c)
            caveats += [r[0] for r in ctx.q("SELECT detail FROM conflicts WHERE subject=? COLLATE NOCASE", f"{o['name']}.{c}")]
            prof = ctx.q1("SELECT p.top_values FROM profiles p JOIN columns cc ON cc.obj_id=p.obj_id AND cc.column_id=p.column_id "
                          "WHERE p.obj_id=? AND cc.name=? COLLATE NOCASE", o["obj_id"], c)
            if prof and prof[0]:
                used_by.append("observed in data: " + ", ".join(f"{v} ({n})" for v, n in json.loads(prof[0])[:30]))
    if not maps:
        rows = ctx.q("SELECT map_id, source, name, rel_path, line, side FROM value_maps WHERE name=? COLLATE NOCASE "
                     "OR name=? COLLATE NOCASE OR ref LIKE ? COLLATE NOCASE LIMIT 12", q, q + "Enum", f"%.{q}")
        if not rows:
            rows = ctx.q("SELECT DISTINCT vm.map_id, vm.source, vm.name, vm.rel_path, vm.line, vm.side FROM value_map_items i "
                         "JOIN value_maps vm ON vm.map_id=i.map_id WHERE i.label LIKE ? COLLATE NOCASE LIMIT 12", f"%{q}%")
        for r in rows:
            vals = ctx.value_map_items(r[0])
            maps.append(ValueSource(source=f"{r[1]} {r[2]}" + (f" [{r[5]}]" if r[5] else ""), via="name/label match",
                                    confidence="exact" if r[2].lower() in (q.lower(), q.lower() + "enum") else "name_match",
                                    values=dict(list(vals.items())[:60]), cite=ctx.cs(r[3], r[4]) if r[3] else None))
            used_by += [f"{u[0]}.{u[1]} ({u[2]}: {u[3]})" for u in ctx.q(
                "SELECT tbl, col, confidence, via FROM column_value_maps WHERE map_id=? LIMIT 10", r[0])]
        # asked by MemType name: say where it is registered, as the column form does (run 11)
        for name in dict.fromkeys(r[2] for r in rows if r[1] == "memtype"):
            caveats.append(_memtype_defined(ctx, name, _net8_catalogues(ctx)) + ".")
    used_by = list(dict.fromkeys(used_by))
    if not maps:
        caveats.append(f"No value list is named, linked to, or has a label like '{query}'. "
                       f"Try find_db_object for the column, then describe_column.")
    return ValueMapOut(status="ok" if maps else "no_results", layer="mixed", searched_scope=scope, query=query,
                       maps=maps, used_by=used_by[:30], caveats=caveats)


def column_usage(ctx: Ctx, table: str, column: Optional[str], mx: int) -> UsageOut:
    table, column = _split(table, column)
    mx = max(1, min(mx, ctx.cfg.limits.max_results_hard))
    scope = ctx.scope("alias-resolved column references in all modules (ScriptDom), C# entity reads/writes, "
                      "SQL text in C#, grid/report registrations")
    o = ctx.obj(table)
    if not o or not column or not ctx.column_row(o["obj_id"], column):
        return UsageOut(status="no_results", layer="mixed", searched_scope=scope,
                        caveats=[f"No column {table}.{column} in the catalog."])
    tname, cname = o["name"], ctx.column_row(o["obj_id"], column)["name"]
    by_obj = defaultdict(list)
    for name, usage, line in ctx.q("SELECT o.name, r.usage, r.line FROM sql_colrefs r JOIN objects o ON o.obj_id=r.obj_id "
                                   "WHERE r.tbl=? COLLATE NOCASE AND r.col=? COLLATE NOCASE ORDER BY o.name, r.line",
                                   tname, cname):
        by_obj[name].append(f"{usage}@{line}")
    sql = [f"{k}: {', '.join(v[:8])}" + (f" (+{len(v) - 8})" if len(v) > 8 else "") for k, v in by_obj.items()]
    code = []
    for r in ctx.q("SELECT rel_path, line, class_fqn, access, value_type FROM code_entity_columns "
                   "WHERE table_name=? COLLATE NOCASE AND column=? COLLATE NOCASE", tname, cname):
        code.append(f"{ctx.cs(r[0], r[1])} {r[2].split('.')[-1]} {r[3]}" + (f" as {r[4]}" if r[4] else ""))
    for r in ctx.q("SELECT s.rel_path, s.line, s.container, x.ref_kind, x.value FROM code_sql_refs x JOIN code_sql s "
                   "ON s.code_sql_id=x.code_sql_id WHERE x.tbl=? COLLATE NOCASE AND x.col=? COLLATE NOCASE", tname, cname):
        code.append(f"{ctx.cs(r[0], r[1])} {r[2]} sql" + (f" {r[4]}" if r[3] == "literal" else ""))
    code = list(dict.fromkeys(code))
    ui = exposed_as(ctx, tname, cname, limit=15)
    total = len(sql) + len(code)
    return UsageOut(status="ok" if total or ui else "no_results", layer="mixed", searched_scope=scope,
                    table=tname, column=cname, sql=sql[:mx], code=code[:mx], ui=ui,
                    truncated=len(sql) > mx or len(code) > mx, total_available=total,
                    caveats=ctx.masked_caveats() + ["C# SQL-text references are regex matches over string literals (text strength)."])


def register(mcp, ctx: Ctx) -> None:
    @mcp.tool(name="describe_column", annotations=RO, description=(
        "Everything known about one column: type, description, value lists from every source (DB "
        "description, C# enum/ValueEnum, MemType, lookup table) with how each was linked and conflicts, "
        "values observed in the data, notes (currency X/XLocal, date-only copies, derived), FK/implicit "
        "relationships with example predicates, C# entity code, and the views/grid/report columns that "
        "expose it. Pass table='Table.Column' or table + column (table is also accepted as name)."))
    def describe_column_tool(table: Optional[str] = None, column: Optional[str] = None,
                             name: Optional[str] = None) -> ColumnOut:
        t = table or name
        if not t:
            return ColumnOut(status="error", layer="catalog", searched_scope="(no column)",
                             caveats=["describe_column needs table='Table.Column' (or table + column)"])
        return describe_column(ctx, t, column)

    @mcp.tool(name="get_value_map", annotations=RO, description=(
        "Value -> label list for 'Table.Column', a code enum/ValueEnum/MemType name (PayMethod, ChannelType, "
        "ReturnRequestDateSelect) or a label ('gift'). Shows every source separately, the columns "
        "and parameters using it, and values observed in the data. (query is also accepted as name or table.)"))
    def get_value_map(query: Optional[str] = None, name: Optional[str] = None,
                      table: Optional[str] = None) -> ValueMapOut:
        q = query or name or table
        if not q:
            return ValueMapOut(status="error", layer="mixed", searched_scope="(no query)",
                               caveats=["get_value_map needs query='Table.Column' or a value-list name"])
        return value_map(ctx, q)

    @mcp.tool(name="find_column_usage", annotations=RO, description=(
        "Where a column is read or written: every view/procedure/function (alias-resolved, with clause "
        "and line), C# entity code and SQL strings, and grid/report columns that show it. Pass "
        "table='Table.Column' or table + column."))
    def find_column_usage(table: str, column: Optional[str] = None, max: int = 60) -> UsageOut:
        return column_usage(ctx, table, column, max)
