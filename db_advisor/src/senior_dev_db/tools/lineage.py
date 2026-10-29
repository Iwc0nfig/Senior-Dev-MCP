"""Chain tools: trace_screen (UI -> SQL), find_join_path, get_impact."""

from __future__ import annotations

import heapq
import json
import re
from collections import defaultdict
from typing import Optional

from ..models import ImpactOut, PathOut, Step, TraceOut
from .common import RO, Ctx, compact_values, strip_name
from .objects import url_name

MODULE_KINDS = ("view", "proc", "scalar_fn", "inline_tvf", "tvf")


def _anchor(ctx: Ctx, key: str) -> list[str]:
    return [f"{r[0]} [{r[1]}]" for r in ctx.q("SELECT rule, evidence FROM style_rules WHERE kind='framework' AND topic=?", key)]


def resolve_target(ctx: Ctx, target: str) -> list:
    t = target.strip()
    un = url_name(t)
    kind_hint = None
    if un:
        kind_hint = "report" if "/reports/" in t.lower() else "view" if "/views/" in t.lower() else None
        t = un
    rows = ctx.q("SELECT * FROM ui_targets WHERE name=? COLLATE NOCASE" + (" AND target_kind=?" if kind_hint else ""),
                 *((t, kind_hint) if kind_hint else (t,)))
    if not rows:
        rows = ctx.q("SELECT * FROM ui_targets WHERE title=? COLLATE NOCASE", t)
    if not rows:
        rows = ctx.q("SELECT * FROM ui_targets WHERE object_name=? COLLATE NOCASE", strip_name(t))
    if not rows and not un:        # a page URL is exact or nothing: a fuzzy match would pick an unrelated report
        words = [w for w in re.findall(r"[A-Za-z0-9]+", t) if w.lower() not in ("list", "report", "the", "view")]
        if words:
            like = "%" + "%".join(words) + "%"
            rows = ctx.q("SELECT * FROM ui_targets WHERE title LIKE ? OR name LIKE ? ORDER BY length(title)", like, like)
    return rows


def object_chain(ctx: Ctx, name: str, depth: int = 3) -> tuple[list[tuple[int, str, str, str]], set[str]]:
    """(level, object, kind, how) for the object and everything it reads/calls; plus base tables."""
    out, tables, seen = [], set(), set()

    def walk(n: str, level: int, how: str):
        o = ctx.obj(n)
        if not o or o["name"].lower() in seen:
            return
        seen.add(o["name"].lower())
        out.append((level, o["name"], o["kind"], how))
        if o["kind"] == "table":
            tables.add(o["name"])
            return
        if level >= depth:
            return
        for tgt, line in ctx.q("SELECT target, min(line) FROM sql_aliases WHERE obj_id=? AND target_kind IN ('object','tvf') "
                               "GROUP BY target COLLATE NOCASE ORDER BY 2", o["obj_id"]):
            t = ctx.obj(tgt)
            if t and t["kind"] == "table":
                tables.add(t["name"])
            elif t:
                walk(t["name"], level + 1, f"read by {o['name']}:{line}")
        for callee, kind, line in ctx.q("SELECT callee, kind, min(line) FROM sql_calls WHERE obj_id=? GROUP BY callee COLLATE NOCASE",
                                        o["obj_id"]):
            t = ctx.obj(callee)
            if t and t["kind"] in ("proc",) or (t and kind == "function" and t["name"].lower() not in ("fn_userwarehouses",)):
                walk(t["name"], level + 1, f"{'executed' if kind == 'exec' else 'called'} by {o['name']}:{line}")

    walk(name, 0, "screen's data source")
    return out, tables


def similar_objects(ctx: Ctx, name: str, n: int = 3, floor: float = 0.45) -> list[str]:
    """Modules whose result columns overlap this one's (Jaccard over output names):
    sibling reports/views that usually need the same fix."""
    o = ctx.obj(name)
    if not o:
        return []
    mine = {r[0].lower() for r in ctx.q("SELECT out_name FROM sql_outputs WHERE obj_id=? AND (owner='result' OR "
                                        "owner LIKE 'dynamic:%') AND out_name IS NOT NULL AND out_name<>'*'", o["obj_id"])}
    if len(mine) < 4:
        return []
    by = defaultdict(set)
    for oid, nm in ctx.q("SELECT obj_id, lower(out_name) FROM sql_outputs WHERE (owner='result' OR owner LIKE "
                         "'dynamic:%') AND out_name IS NOT NULL AND out_name<>'*' AND obj_id<>?", o["obj_id"]):
        by[oid].add(nm)
    scored = []
    for oid, cols in by.items():
        j = len(mine & cols) / len(mine | cols)
        if j >= floor:
            scored.append((j, oid))
    out = []
    for j, oid in sorted(scored, reverse=True)[:n]:
        r = ctx.q1("SELECT name, kind FROM objects WHERE obj_id=?", oid)
        ui = ctx.q1("SELECT title FROM ui_targets WHERE object_name=? COLLATE NOCASE", r[0])
        out.append(f"{r[1]} {r[0]}" + (f" (report/screen '{ui[0]}')" if ui and ui[0] else "") + f": {j:.0%} same output columns")
    return out


STRING_PROPS = {"Caption", "DataField", "FieldName", "MemType", "MemValues", "ToolTip", "Format", "Description"}
BARE_RE = re.compile(r"-?\d+(?:\.\d+)?|true|false|null|(?:\w+\.)+\w+(?:\s*[-+*/]\s*\w+)*|c[A-Z]\w*(?:\s*[-+*/]\s*\w+)*")


def _cs_prop(key: str, value) -> str:
    """One `Key = value` of a Column initializer as C#: numbers, constants and enum members as written in
    the source, strings in double quotes (the index keeps string literals without their quotes)."""
    v = str(value)
    if key not in STRING_PROPS and BARE_RE.fullmatch(v):
        return f"{key} = {v}"
    return f"{key} = {json.dumps(v, ensure_ascii=False)}"


def example_registrations(ctx: Ctx, kind: str, name: str, rel: str) -> str:
    """Verbatim props of existing column registrations to copy: the last one, plus one with
    MemValues (decoded value lists) -- from this registration, else from any registration."""
    if kind == "view":
        rows = ctx.q("SELECT props, line FROM ui_view_columns WHERE view_name=? COLLATE NOCASE AND rel_path=? ORDER BY ordinal DESC", name, rel)
        mem = next((r for r in rows if '"MemValues"' in r[0]), None) or ctx.q1(
            "SELECT props, line, rel_path FROM ui_view_columns WHERE rel_path=? AND props LIKE '%MemValues%' LIMIT 1", rel)
    else:
        rows = ctx.q("SELECT props, line FROM ui_report_fields WHERE report_name=? COLLATE NOCASE AND rel_path=? ORDER BY ordinal DESC", name, rel)
        mem = None
    out = []
    if rows:
        out.append(f"last: new Column {{ {', '.join(_cs_prop(k, v) for k, v in json.loads(rows[0][0]).items())} }} @ {rel}:{rows[0][1]}")
    if mem:
        out.append(f"with value list: new Column {{ {', '.join(_cs_prop(k, v) for k, v in json.loads(mem[0]).items())} }} @ {rel}:{mem[1]}")
    return " | ".join(out)


def code_sides(ctx: Ctx, kind: str, name: str) -> list[str]:
    """Every catalogue file that registers this kind, and every side with a configured note: is this
    screen/report registered there?  A side missing from the chain is then explained, not inferred."""
    table, key = ("ui_views", "view_name") if kind == "view" else ("ui_reports", "report_name")
    here = {r[0]: r for r in ctx.q(f"SELECT rel_path, side, line, line_end FROM {table} WHERE {key}=? COLLATE NOCASE",
                                   name)}
    out = []
    for rel, side, n in ctx.q(f"SELECT rel_path, side, count(*) FROM {table} GROUP BY rel_path, side "
                              f"ORDER BY side, rel_path"):
        if rel in here:
            out.append(f"[{side}] {rel}: registered at {rel}:{here[rel][2]}-{here[rel][3]}")
        elif n >= CATALOGUE_MIN:    # a catalogue, not a tool that registers a few reports of its own
            out.append(f"[{side}] {rel}: NOT registered here (this catalogue registers {n} other {kind}s)")
    for k, note in ctx.cfg.side_notes:
        k_kind, _, k_side = k.partition(":")
        if k_kind == kind:
            out.append(f"[{k_side}] {note}")
    return out


CATALOGUE_MIN = 20

_FILE_LINES: dict[str, tuple[float, list[str]]] = {}


def _file_lines(ctx: Ctx, rel: str) -> list[str] | None:
    """The workspace file's lines (cached by mtime), or None when it is not there."""
    p = ctx.cfg.paths.workspaces / rel
    try:
        mt = p.stat().st_mtime
    except OSError:
        return None
    hit = _FILE_LINES.get(rel)
    if hit and hit[0] == mt:
        return hit[1]
    lines = p.read_bytes().decode("utf-8-sig", errors="replace").splitlines()
    _FILE_LINES[rel] = (mt, lines)
    return lines


def insert_point(ctx: Ctx, rel: str, last_start: int) -> tuple[int, int] | None:
    """(end of the last registered column, the Columns array's closing line) in a registration file: the
    first '}' at the indentation of the last 'new Column' line, then the next non-blank line ('}' too)."""
    lines = _file_lines(ctx, rel)
    if not lines or not 0 < last_start <= len(lines):
        return None
    def ind(s: str) -> int:
        return len(s.expandtabs(4)) - len(s.expandtabs(4).lstrip())
    want = ind(lines[last_start - 1])
    end = next((j for j in range(last_start + 1, min(len(lines), last_start + 200) + 1)
                if lines[j - 1].strip().startswith("}") and ind(lines[j - 1]) == want), None)
    if end is None:
        return None
    close = next((j for j in range(end + 1, min(len(lines), end + 20) + 1) if lines[j - 1].strip()), None)
    if close is None or not lines[close - 1].strip().startswith("}"):
        return None
    return end, close


def _insert_text(ctx: Ctx, rel: str, last_start: int, last_name: str) -> str:
    ip = insert_point(ctx, rel, last_start)
    return (f"insert new columns after line {ip[0]} (end of {last_name}), before line {ip[1]} (end of the "
            f"Columns array)" if ip else f"the last column ({last_name}) starts at line {last_start}")


def registered_columns(ctx: Ctx, tk: str, name: str, max_columns: int = 80) -> tuple[list[str], list[str]]:
    """Every registered column of a report/grid, per registration side, with its line (one compact line per
    side), and a caveat per side when the sides register different column sets."""
    if tk == "view":
        regs = ctx.q("SELECT side, rel_path FROM ui_views WHERE view_name=? COLLATE NOCASE ORDER BY side, rel_path", name)
        sql = ("SELECT field_name, line FROM ui_view_columns WHERE view_name=? COLLATE NOCASE AND rel_path=? "
               "ORDER BY ordinal")
    else:
        regs = ctx.q("SELECT side, rel_path FROM ui_reports WHERE report_name=? COLLATE NOCASE ORDER BY side, rel_path",
                     name)
        sql = ("SELECT data_field, line FROM ui_report_fields WHERE report_name=? COLLATE NOCASE AND rel_path=? "
               "ORDER BY ordinal")
    out, sets = [], {}
    for side, rel in dict.fromkeys((r[0], r[1]) for r in regs):
        cols = [(f or "?", ln) for f, ln in ctx.q(sql, name, rel)]
        if not cols:
            continue
        sets[rel] = {f.lower(): f for f, _ in cols}
        shown = cols[:max_columns] if max_columns else cols
        out.append(f"[{side}] {rel} ({len(cols)} columns): " + ", ".join(f"{f}@{ln}" for f, ln in shown)
                   + (f", ... +{len(cols) - len(shown)} more (max_columns=0 lists all)" if len(shown) < len(cols) else "")
                   + f". {_insert_text(ctx, rel, cols[-1][1], cols[-1][0])}.")
    caveats = []
    if len(sets) > 1:
        every = {k: v for s in sets.values() for k, v in s.items()}
        for rel, s in sets.items():
            lacks = [every[k] for k in every if k not in s]
            if lacks:
                caveats.append(f"The registrations differ: {rel} has no {', '.join(lacks[:20])}"
                               + (f" (+{len(lacks) - 20})" if len(lacks) > 20 else "")
                               + " (registered on another side).")
    return out, caveats


def trace(ctx: Ctx, target: str, max_columns: int = 80) -> TraceOut:
    scope = ctx.scope("Web.config rewrite rules, MasterView/Report registrations (all code sides), module parse")
    rows = resolve_target(ctx, target)
    if not rows:
        o = ctx.obj(target)
        caveats = [f"No grid screen or report matches '{target}'."]
        page = url_name(target)
        if page:
            caveats.append(f"{page}.aspx is an ASP.NET page that is not registered as a grid screen or report (an "
                           f"entity form, say). Its fields and the code behind them: the Code Advisor's "
                           f"find_control(caption=..., markup_file='{page}.aspx') or get_control_binding; the data "
                           "behind a caption or column: find_db_object.")
        if o:
            caveats.append(f"'{o['name']}' is a {o['kind']} with no screen/report registration; use get_impact.")
        return TraceOut(status="no_results", layer="code", searched_scope=scope, caveats=caveats)
    caveats = ctx.masked_caveats()
    if len({(r[0], r[1].lower()) for r in rows}) > 1:
        caveats.append("Several targets match: " + "; ".join(f"{r[0]} {r[1]} ({r[2]})" for r in rows[:6])
                       + ". Showing the first; pass its exact name for another.")
    tk, name, title, url, objname, oid = rows[0]
    columns, col_caveats = registered_columns(ctx, tk, name, max_columns)
    caveats += col_caveats
    chain: list[Step] = []
    companion: list[Step] = []
    params: list[dict] = []
    rule = ctx.q1("SELECT pattern, target, rel_path, line FROM url_rules WHERE target LIKE ?",
                  "%viewName%" if tk == "view" else "%reportName%")
    if rule:
        chain.append(Step(step="url", detail=f"{url} -> rewrite '{rule[0]}' -> {rule[1]}", cite=f"{rule[2]}:{rule[3]}"))
    if tk == "view":
        for r in ctx.q("SELECT * FROM ui_views WHERE view_name=? COLLATE NOCASE ORDER BY side, rel_path", name):
            p = json.loads(r["props"])
            keys = ", ".join(f"{k}={p[k]}" for k in ("ObjectName", "WarehouseLimitField", "KeyField", "SearchField",
                                                       "ExtraWhere", "HasAttachmentsExists", "EntityPage") if k in p)
            chain.append(Step(step=f"MasterView [{r['side']}]", detail=f"'{name}' ({p.get('Title', '')}): {keys}",
                              cite=ctx.cs(r['rel_path'], r['line'], r['line_end'], key=True)))
            last = ctx.q1("SELECT max(line_end), count(*) FROM ui_view_columns WHERE view_name=? COLLATE NOCASE AND rel_path=?",
                          name, r["rel_path"])
            lastc = ctx.q1("SELECT field_name, line FROM ui_view_columns WHERE view_name=? COLLATE NOCASE AND rel_path=? "
                           "ORDER BY line DESC LIMIT 1", name, r["rel_path"])
            where = (_insert_text(ctx, r["rel_path"], lastc[1], lastc[0] or "?") if lastc
                     else f"last ends at line {last[0]}")
            companion.append(Step(step=f"grid column [{r['side']}]",
                                  detail=f"add new Column {{ FieldName, Caption, ColumnType, Visible, Width }} to "
                                         f"Columns of MasterView '{name}' ({last[1]} columns; {where}). "
                                         f"Existing entries to copy: {example_registrations(ctx, 'view', name, r['rel_path'])}",
                                  cite=ctx.cs(r['rel_path'], last[0])))
        for a in _anchor(ctx, "grid_query"):
            chain.append(Step(step="runtime query", detail=a))
    else:
        for r in ctx.q("SELECT * FROM ui_reports WHERE report_name=? COLLATE NOCASE ORDER BY side, rel_path", name):
            p = json.loads(r["props"])
            chain.append(Step(step=f"Report [{r['side']}]",
                              detail=f"'{name}' ({p.get('Title', '')}): ObjectName={p.get('ObjectName')}, "
                                     f"DataType={p.get('DataType')}, Category={p.get('Category')}",
                              cite=ctx.cs(r['rel_path'], r['line'], r['line_end'], key=True)))
            last = ctx.q1("SELECT max(line), count(*) FROM ui_report_fields WHERE report_name=? COLLATE NOCASE AND rel_path=?",
                          name, r["rel_path"])
            lastf = ctx.q1("SELECT data_field FROM ui_report_fields WHERE report_name=? COLLATE NOCASE AND rel_path=? AND line=?",
                           name, r["rel_path"], last[0])
            where = _insert_text(ctx, r["rel_path"], last[0], lastf[0] if lastf else "?") if last[0] else "no columns"
            companion.append(Step(step=f"report column [{r['side']}]",
                                  detail=f"add new Column {{ Caption, DataField, ColumnType, Visible, BandIndex, Width }} "
                                         f"to the Section Columns of report '{name}' ({last[1]} columns; {where}). "
                                         f"Existing entry to copy: {example_registrations(ctx, 'report', name, r['rel_path'])}",
                                  cite=ctx.cs(r['rel_path'], last[0])))
        seen = set()
        for f in ctx.q("SELECT f.*, r.side FROM ui_report_filters f JOIN ui_reports r ON r.report_name=f.report_name AND "
                       "r.rel_path=f.rel_path WHERE f.report_name=? COLLATE NOCASE ORDER BY f.rel_path, f.ordinal", name):
            if f["filter_name"] in seen:
                continue
            seen.add(f["filter_name"])
            d = {"filter": f["filter_name"], "params": ["@" + x for x in (f["filter_name"] or "").split("#")],
                 "caption": f["caption"], "type": f["filter_type"]}
            if f["mem_type"]:
                vm = ctx.q1("SELECT map_id FROM value_maps WHERE name=? COLLATE NOCASE AND source='memtype'", f["mem_type"].strip('"'))
                d["values"] = f"{f['mem_type']}: " + (compact_values(ctx.value_map_items(vm[0])) if vm else "?")
            if f["default_value"]:
                d["default"] = f["default_value"]
            d["cite"] = ctx.cs(f['rel_path'], f['line'])
            params.append(d)
        for a in _anchor(ctx, "report_exec"):
            chain.append(Step(step="runtime call", detail=a))
    if not objname or not oid:
        caveats.append(f"Registered ObjectName '{objname}' does not exist in the database snapshot.")
        return TraceOut(status="partial", layer="mixed", searched_scope=scope, target=f"{tk} {name}", chain=chain,
                        params=params, companion_code=companion, sides=code_sides(ctx, tk, name), columns=columns,
                        caveats=caveats)
    objs, tables = object_chain(ctx, objname)
    for level, oname, okind, how in objs:
        o = ctx.obj(oname)
        if okind == "table":
            continue
        chain.append(Step(step=f"sql {okind}" + (f" (nested {level})" if level else ""),
                          detail=f"{oname}: {o['line_count']} lines, modified {o['modified']}" + (f"; {how}" if level else ""),
                          cite=f"{oname}:1"))
    def helper(n, k):
        if k == "scalar_fn":
            return True
        if k in ("inline_tvf", "tvf"):
            oo = ctx.obj(n)
            return oo is not None and (ctx.q1("SELECT count(*) FROM columns WHERE obj_id=?", oo["obj_id"])[0] or 0) <= 2
        return False
    sql_objects = [o[1] for o in objs if o[2] != "table" and not helper(o[1], o[2])]
    caveats.extend(ctx.stale_caveats(sql_objects))
    if len(sql_objects) > 1:
        caveats.append(f"The data passes through {len(sql_objects)} SQL objects ({' -> '.join(sql_objects)}); "
                       f"a fix may belong in a nested one. get_object_outline each before deciding.")
    sims = similar_objects(ctx, objname)
    if sims:
        caveats.append("Objects with largely the same output (siblings that often need the same change): " + "; ".join(sims))
    if tk == "view":
        consumers = ctx.q("SELECT DISTINCT o.name FROM sql_aliases a JOIN objects o ON o.obj_id=a.obj_id "
                          "WHERE a.target=? COLLATE NOCASE AND o.name<>? COLLATE NOCASE", objname, objname)
        if consumers:
            caveats.append(f"View {objname} is also read by: {', '.join(r[0] for r in consumers[:12])}"
                           + (f" (+{len(consumers) - 12})" if len(consumers) > 12 else "") + ". See get_impact.")
    return TraceOut(status="ok", layer="mixed", searched_scope=scope, target=f"{'grid screen' if tk == 'view' else 'report'} {name}",
                    chain=chain, sql_objects=sql_objects, base_tables=sorted(tables), params=params,
                    companion_code=companion, sides=code_sides(ctx, tk, name), columns=columns, caveats=caveats)


# ---------------------------------------------------------------------------
COST = {"exact": 1.0, "strong": 1.4, "weak": 3.0}


def join_path(ctx: Ctx, a: str, b: str, max_hops: int) -> PathOut:
    scope = ctx.scope("declared FKs + relationships inferred from module joins, descriptions and naming")
    oa, ob = ctx.obj(a), ctx.obj(b)
    if not oa or not ob:
        miss = a if not oa else b
        return PathOut(status="no_results", layer="mixed", searched_scope=scope,
                       caveats=[f"Unknown table '{miss}'. Similar: {', '.join(ctx.suggest(miss, ('table', 'view')))}"])
    A, B = oa["name"], ob["name"]
    adj = defaultdict(list)
    for r in ctx.q("SELECT rel_id, from_table, from_column, to_table, to_column, kind, confidence, occurrences, example, "
                   "example_obj, example_line FROM relationships"):
        c = COST.get(r[6], 3.0) - min(r[7], 20) * 0.01
        adj[r[1].lower()].append((r[3], c, r))
        adj[r[3].lower()].append((r[1], c, r))
    max_hops = max(1, min(max_hops, 5))
    paths, seen_paths = [], set()
    heap = [(0.0, A, [])]
    while heap and len(paths) < 3:
        cost, node, path = heapq.heappop(heap)
        if node.lower() == B.lower():
            key = tuple(p[0] for p in path)
            if key not in seen_paths:
                seen_paths.add(key)
                paths.append(path)
            continue
        if len(path) >= max_hops:
            continue
        visited = {A.lower()} | {(p[1][3] if p[2] else p[1][1]).lower() for p in path}
        for nxt, c, r in adj.get(node.lower(), []):
            if nxt.lower() in visited:
                continue
            forward = r[1].lower() == node.lower()
            # many-to-one into a hub, then back out one-to-many (A -> Product <- B) links rows that merely
            # share a product/partner/warehouse: a sibling join, not navigation.  Penalise it.
            if path and path[-1][2] and not forward:
                c += 2.5
            heapq.heappush(heap, (cost + c, nxt, path + [(r[0], r, forward)]))
    out = []
    for path in paths:
        hops = []
        for _, r, fwd in path:
            s = f"{r[1]}.{r[2]} -> {r[3]}.{r[4]} ({r[5]}, {r[6]}" + (f", seen {r[7]}x" if r[7] else "") + ")"
            if r[8]:
                s += f" e.g. {r[8]} [{r[9]}:{r[10]}]"
            hops.append(s)
        out.append(hops)
    together = ctx.q("""SELECT o.name, count(*) FROM objects o
                        WHERE o.obj_id IN (SELECT obj_id FROM sql_aliases WHERE target=? COLLATE NOCASE)
                          AND o.obj_id IN (SELECT obj_id FROM sql_aliases WHERE target=? COLLATE NOCASE)
                        GROUP BY o.name ORDER BY o.modified DESC LIMIT 6""", A, B)
    caveats = ctx.masked_caveats()
    if together:
        caveats.append("Objects that already join both (copy their join): " + ", ".join(r[0] for r in together))
    if any("weak" in h for p in out for h in p):
        caveats.append("A hop marked weak is inferred from naming or one description only; check it with describe_column.")
    return PathOut(status="ok" if out else "no_results", layer="mixed", searched_scope=scope, paths=out, caveats=caveats)


# ---------------------------------------------------------------------------
IMPACT_COMPACT_OVER = 40


def impact(ctx: Ctx, name: str, column: Optional[str], detail: bool = False) -> ImpactOut:
    scope = ctx.scope("engine dependencies, parsed reads/EXEC calls, screen/report registrations, SQL text in C#")
    o = ctx.obj(name)
    if not o:
        return ImpactOut(status="no_results", layer="mixed", searched_scope=scope,
                         caveats=[f"No object '{name}'. Similar: {', '.join(ctx.suggest(name))}"])
    oname = o["name"]
    deps, levels, seen = [], [], {oname.lower()}
    frontier = [(oname, 0)]
    all_names = [oname]
    while frontier:
        cur, lvl = frontier.pop(0)
        if lvl >= 3:
            continue
        co = ctx.obj(cur)
        rows = ctx.q("SELECT DISTINCT o.name, o.kind, 'reads' FROM sql_aliases a JOIN objects o ON o.obj_id=a.obj_id "
                     "WHERE a.target=? COLLATE NOCASE "
                     "UNION SELECT DISTINCT o.name, o.kind, 'calls' FROM sql_calls c JOIN objects o ON o.obj_id=c.obj_id "
                     "WHERE c.callee=? COLLATE NOCASE "
                     "UNION SELECT DISTINCT o.name, o.kind, 'depends' FROM deps d JOIN objects o ON o.obj_id=d.referencing_id "
                     "WHERE d.referenced_id=?", cur, cur, co["obj_id"] if co else -1)
        for dn, dk, how in rows:
            if dn.lower() in seen:
                continue
            seen.add(dn.lower())
            all_names.append(dn)
            extra = ""
            if column and lvl == 0:
                refs = ctx.q("SELECT usage, line FROM sql_colrefs r JOIN objects o ON o.obj_id=r.obj_id WHERE o.name=? "
                             "AND r.tbl=? COLLATE NOCASE AND r.col=? COLLATE NOCASE", dn, oname, column)
                star = ctx.q1("SELECT line FROM sql_outputs s JOIN objects o ON o.obj_id=s.obj_id WHERE o.name=? AND "
                              "s.sources LIKE ? COLLATE NOCASE AND s.out_name='*'", dn, f'%["{oname}", "*"%')
                extra = (f"; uses {column} at " + ", ".join(f"{u}@{l}" for u, l in refs[:5])) if refs else \
                        (f"; selects {oname}.* @{star[0]} (picks up new columns automatically)" if star else
                         f"; does not reference {column}")
            deps.append(f"{'  ' * lvl}{dk} {dn} {how} {cur}{extra}")
            levels.append(lvl)
            frontier.append((dn, lvl + 1))
    ui, edits = [], []
    for n in all_names:
        for r in ctx.q("SELECT target_kind, name, title, url FROM ui_targets WHERE object_name=? COLLATE NOCASE", n):
            ui.append(f"{'grid screen' if r[0] == 'view' else 'report'} '{r[2] or r[1]}' ({r[1]}, {r[3]}) <- {n}")
            if r[0] == "view":
                for v in ctx.q("SELECT side, rel_path, line FROM ui_views WHERE view_name=? COLLATE NOCASE", r[1]):
                    last = ctx.q1("SELECT max(line_end) FROM ui_view_columns WHERE view_name=? COLLATE NOCASE AND rel_path=?", r[1], v[1])
                    edits.append(f"[{v[0]}] {ctx.cs(v[1], last[0])} add a Column {{ FieldName = \"<new column>\", Caption, "
                                 f"ColumnType, Visible }} to MasterView '{r[1]}' (registered at line {v[2]})")
                    hv = ctx.q1("SELECT props FROM ui_views WHERE view_name=? COLLATE NOCASE AND rel_path=?", r[1], v[1])
                    if hv and '"HasAttachmentsExists": "true"' in hv[0]:
                        edits.append(f"[{v[0]}] MasterView '{r[1]}' has HasAttachmentsExists=true: the SQL view must keep "
                                     f"its HasAttachments column")
            else:
                for v in ctx.q("SELECT side, rel_path, line FROM ui_reports WHERE report_name=? COLLATE NOCASE", r[1]):
                    last = ctx.q1("SELECT max(line) FROM ui_report_fields WHERE report_name=? COLLATE NOCASE AND rel_path=?", r[1], v[1])
                    if last[0] is None:
                        edits.append(f"[{v[0]}] report '{r[1]}' ({v[1]}:{v[2]}) declares no output columns "
                                     f"(template/print report): check its template if the output shape changes")
                        continue
                    edits.append(f"[{v[0]}] {ctx.cs(v[1], last[0])} add a report Column {{ Caption, DataField = \"<new column>\", "
                                 f"ColumnType, Visible, BandIndex }} to report '{r[1]}' (registered at line {v[2]})")
    for a in _anchor(ctx, "grid_query"):
        if any(u.startswith("grid") for u in ui):
            edits.append("Grid SELECT lists only registered columns, so a new view column is invisible until "
                         "registered: " + a)
            break
    code = []
    for r in ctx.q("SELECT DISTINCT s.rel_path, s.line, s.container FROM code_sql_refs x JOIN code_sql s ON "
                   "s.code_sql_id=x.code_sql_id WHERE x.ref_kind='object' AND x.tbl=? COLLATE NOCASE LIMIT 40", oname):
        code.append(f"{ctx.cs(r[0], r[1])} {r[2]}")
    for r in ctx.q("SELECT class_fqn, rel_path, line FROM code_entities WHERE table_name=? COLLATE NOCASE", oname):
        code.append(f"{ctx.cs(r[1], r[2])} entity {r[0].split('.')[-1]}")
        if column:
            edits.append(f"Entity {r[0].split('.')[-1]} ({r[1]}) maps {oname}: a new/changed column needs its reads "
                         f"(Fill_*) and _Fields list updated if the application must use it")
    caveats = ctx.masked_caveats() + ctx.stale_caveats([oname] + [d.split()[1] for d in deps[:10] if len(d.split()) > 1])
    if column and not ctx.column_row(o["obj_id"], column):
        caveats.append(f"{oname} has no column '{column}' yet: dependents are listed for adding it.")
    edits.sort(key=lambda e: (not e.startswith("[net48]"), e))
    total = len(deps)
    shown = deps[:60]
    edits = list(dict.fromkeys(edits))
    if not detail and total > IMPACT_COMPACT_OVER:
        # a base table reaches 100+ objects over three levels (22 KB): list the direct readers, count the rest;
        # the screens/reports stay whole, the companion edits are capped at 20 (detail=true lists them all)
        shown = [d for d, lv in zip(deps, levels) if lv == 0][:60]
        for lv in sorted({lv for lv in levels if lv > 0}):
            n = sum(1 for x in levels if x == lv)
            shown.append(f"level {lv + 1}: {n} more object(s) reading the ones above (detail=true lists them)")
        if len(code) > 15:
            code = code[:15] + [f"... {len(code) - 15} more C# reads (detail=true lists them)"]
        if len(edits) > 20:
            edits = edits[:20] + [f"... {len(edits) - 20} more companion edits for screens/reports further up "
                                  "(detail=true lists them)"]
        caveats.append(f"{total} database dependents: the direct ones are listed, deeper levels counted "
                       "(detail=true lists all). To see what a change to one view or procedure affects, call "
                       "get_impact on that object.")
    return ImpactOut(status="ok", layer="mixed", searched_scope=scope, target=f"{o['kind']} {oname}" + (f".{column}" if column else ""),
                     db_dependents=shown, ui_consumers=list(dict.fromkeys(ui)), code_consumers=code,
                     companion_edits=edits, truncated=total > len(shown), total_available=total,
                     caveats=caveats)


def register(mcp, ctx: Ctx) -> None:
    @mcp.tool(name="trace_screen", annotations=RO, description=(
        "From a page URL (.../views/returnrequestview.aspx, .../reports/returnrequest.aspx), screen "
        "or report title ('Stock Adjustments Report') or name to: rewrite rule, every MasterView/Report "
        "registration (all code sides, file:line), the SQL object and the nested views/procs it reads or "
        "executes, base tables, report parameters with their meaning, and where to register a new column. "
        "columns lists every registered column per side as DataField@line: read a neighbour with the Code "
        "Advisor's read_file_range(file, line_start=<line>); max_columns (default 80 per side, 0 = all) caps it. "
        "C# citations name their member: "
        "read_file_range(at='path:line') opens it."))
    def trace_screen(target: str, max_columns: int = 80) -> TraceOut:
        return trace(ctx, target, max_columns)

    @mcp.tool(name="find_join_path", annotations=RO, description=(
        "How to join two tables: up to 3 cheapest paths over declared FKs and relationships inferred from "
        "existing module joins, each hop with confidence and an example predicate already used in the "
        "database (Object:line). Also lists objects that already join both tables."))
    def find_join_path(from_table: str, to_table: str, max_hops: int = 3) -> PathOut:
        return join_path(ctx, from_table, to_table, max_hops)

    @mcp.tool(name="get_impact", annotations=RO, description=(
        "What depends on a table/view/procedure (optionally one column): views and procedures that read or "
        "execute it (transitively, noting whether each uses the column or SELECT *), the grid screens and "
        "reports on top, C# code that queries it, and the companion C# registrations to edit "
        "(file:line anchors). Over 40 dependents, the direct ones are listed and deeper levels counted; "
        "detail=true lists everything."))
    def get_impact(name: str, column: Optional[str] = None, detail: bool = False) -> ImpactOut:
        return impact(ctx, name, column, detail)
