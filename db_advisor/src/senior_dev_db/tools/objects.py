"""Object-level tools: find_db_object, describe_table, get_object_definition,
get_object_outline, search_sql."""

from __future__ import annotations

import json
import re
from typing import Optional, Union

from ..models import ColRow, DefinitionOut, FindOut, Hit, OutlineOut, SearchSqlOut, TableOut
from .common import RO, Ctx, strip_name

KIND_ALIASES = {
    "procedure": "proc", "sproc": "proc", "sp": "proc", "function": "scalar_fn", "fn": "scalar_fn",
    "tvf": "tvf", "screen": "screen", "grid": "screen", "report": "report", "column": "column",
}


def fts_query(text: str, mode: str) -> str:
    toks = [t for t in re.findall(r"[A-Za-z0-9_]+", text) if len(t) > 1]
    if not toks:
        return ""
    return f" {mode} ".join(f'"{t}"*' for t in toks[:8])


def url_name(q: str) -> Optional[str]:
    m = re.search(r"(?:^|/)([A-Za-z0-9_]+)\.aspx", q, re.I)
    return m.group(1) if m else None


def find(ctx: Ctx, query: str, kinds: Optional[list[str]], mx: int) -> FindOut:
    mx = max(1, min(mx, ctx.cfg.limits.max_results_hard))
    kinds_n = [KIND_ALIASES.get(k.lower(), k.lower()) for k in (kinds or [])]
    scope = ctx.scope("objects, columns, grid screens and their column captions, reports and report "
                      "column captions, value lists (code enums, MemTypes, DB descriptions)")
    hits: list[Hit] = []
    seen = set()

    def add(kind, name, match, detail):
        if kind == "value":
            key0 = ("value", name.lower(), (detail or "").lower())
            if key0 not in seen and (not kinds_n or "value" in kinds_n or "column" in kinds_n):
                seen.add(key0)
                hits.append(Hit(kind=kind, name=name, match=match, detail=detail))
            return
        if kinds_n and kind not in kinds_n and not (kind in ("table", "view", "proc", "scalar_fn", "inline_tvf", "tvf")
                                                    and "object" in kinds_n):
            return
        key = (kind, name.lower())
        if key in seen:
            return
        seen.add(key)
        hits.append(Hit(kind=kind, name=name, match=match, detail=detail))

    q = query.strip()
    un = url_name(q)
    if un:
        for r in ctx.q("SELECT target_kind, name, title, url, object_name FROM ui_targets WHERE name=? COLLATE NOCASE", un):
            add("screen" if r[0] == "view" else "report", r[1], r[3] or r[1],
                f"{r[2] or ''} -> SQL {r[4] or '?'}; call trace_screen for the full chain")
    # exact object / column name
    o = ctx.obj(q)
    if o:
        add(o["kind"], o["name"], o["name"], (o["description"] or "")[:160] or None)
    if "." in q:
        t, _, c = strip_name(q).partition(".")
        ob = ctx.obj(t)
        if ob:
            cr = ctx.column_row(ob["obj_id"], c)
            if cr:
                add("column", f"{ob['name']}.{cr['name']}", q, f"{cr['type_name']}; {(cr['description'] or '')[:140]}")
    for mode in ("AND", "OR"):
        fq = fts_query(q.replace(".aspx", ""), mode)
        if not fq or len(hits) >= mx:
            break
        rows = ctx.q("SELECT s.kind, s.name, s.text, s.detail, bm25(search_fts) * s.weight AS score "
                     "FROM search_fts JOIN search_items s ON s.item_id=search_fts.rowid "
                     "WHERE search_fts MATCH ? ORDER BY score LIMIT ?", fq, mx * 4)
        for r in rows:
            add(r[0], r[1], r[2][:120], r[3])
        if hits:
            break
    total = len(hits)
    caveats = ctx.masked_caveats()
    if hits and hits[0].kind in ("grid_column", "report_column", "value_map"):
        caveats.append("Caption/value-list hits name a UI or code artefact; follow 'detail' to the base column.")
    titled = any(h.kind in ("screen", "report") for h in hits)
    if (" " in q or q.endswith(":")) and not titled:
        # page captions (legends, labels in .aspx markup and .resx) are not in this index
        caveats.append(f"If '{query}' is a caption on a page (a label, legend or section title), the Code Advisor's "
                       f"find_control(caption='{query}') gives the controls it belongs to and the code that fills them.")
    return FindOut(status="ok" if hits else "no_results", layer="mixed", searched_scope=scope,
                   truncated=total > mx, total_available=total, hits=hits[:mx], caveats=caveats)


def describe_table_impl(ctx: Ctx, name: str, columns_like: Optional[str],
                        columns: Union[list[str], str, None] = None) -> TableOut:
    o = ctx.obj(name)
    scope = ctx.scope("tables/views/functions in the catalog")
    if not o or o["kind"] not in ("table", "view", "inline_tvf", "tvf"):
        sugg = ctx.suggest(name, ("table", "view", "inline_tvf", "tvf"))
        return TableOut(status="no_results", layer="catalog", searched_scope=scope,
                        caveats=[f"No table/view named '{name}'." + (f" Did you mean: {', '.join(sugg)}?" if sugg else "")])
    oid, tname = o["obj_id"], o["name"]
    pk = ctx.q1("SELECT key_columns FROM indexes WHERE obj_id=? AND is_primary_key=1", oid)
    filt = [f.strip().lower() for f in (columns_like or "").split(",") if f.strip()]
    wanted = [c.strip() for c in (columns.split(",") if isinstance(columns, str) else columns or []) if c and c.strip()]
    exact = {c.lower() for c in wanted}
    rels = {}
    for r in ctx.q("SELECT from_column, to_table, to_column, kind, confidence FROM relationships "
                   "WHERE from_table=? COLLATE NOCASE ORDER BY declared DESC, occurrences DESC", tname):
        rels.setdefault(r[0].lower(), f"{r[1]}.{r[2]} ({r[3]}, {r[4]})")
    notes = {}
    for r in ctx.q("SELECT col, topic, note FROM column_notes WHERE tbl=? COLLATE NOCASE", tname):
        notes.setdefault(r[0].lower(), []).append(r[1])
    curated = {r[0].split(".", 1)[1].lower(): r[1] for r in ctx.q(
        "SELECT subject, text FROM curated WHERE subject LIKE ? COLLATE NOCASE AND fact_type='meaning' ORDER BY fact_id DESC",
        tname + ".%")}
    lineage = {}
    if o["kind"] == "view":
        for r in ctx.q("SELECT name, source_table, source_column FROM result_sets WHERE obj_id=? AND source_table IS NOT NULL", oid):
            lineage[r[0].lower()] = f"= {r[1]}.{r[2]}"
    cols = []
    caveats = ctx.masked_caveats() + ctx.stale_caveats([tname])
    all_cols = ctx.q("SELECT * FROM columns WHERE obj_id=? ORDER BY column_id", oid)
    for c in all_cols:
        if exact:
            if c["name"].lower() not in exact:
                continue
        elif filt and not any(f in c["name"].lower() for f in filt):
            continue
        t = c["type_name"]
        if t in ("varchar", "nvarchar", "char", "nchar", "varbinary"):
            ln = c["max_length"]
            t += "(max)" if ln == -1 else f"({ln // 2 if t.startswith('n') else ln})"
        elif t in ("decimal", "numeric"):
            t += f"({c['precision']},{c['scale']})"
        vals, conf = ctx.best_values(tname, c["name"])
        if conf:
            caveats.extend(f"{tname}.{c['name']}: {x}" for x in conf)
        desc = c["description"]
        if desc and vals and len(desc) > 60:
            desc = desc[:60] + "…"
        meaning = (desc[:160] if desc else None) or lineage.get(c["name"].lower())
        cm = curated.get(c["name"].lower())
        if cm:         # a one-word catalog description ('Ship To') says less than the curated meaning
            meaning = (f"{meaning}. " if meaning else "") + (cm if len(cm) <= 200 else cm[:200] + "…") + " [curated]"
        cols.append(ColRow(name=c["name"], type=t, null=bool(c["is_nullable"]),
                           meaning=meaning,
                           values=vals[:220] if vals else None,
                           link=rels.get(c["name"].lower()),
                           note=", ".join(notes.get(c["name"].lower(), [])) or None))
    actions = {(r[0].lower(), r[1].lower()): r[2] for r in ctx.q(
        "SELECT p.name, f.parent_column, f.on_delete FROM fks f JOIN objects p ON p.obj_id=f.parent_obj_id "
        "WHERE f.ref_obj_id=? AND f.on_delete IS NOT NULL AND f.on_delete<>'NO_ACTION'", o["obj_id"])}

    def _on_delete(t, c):
        a = actions.get((t.lower(), c.lower()))
        return f", on delete {a.replace('_', ' ').lower()}" if a else ""
    refby = [f"{r[0]}.{r[1]} ({r[2]}, {r[3]}{_on_delete(r[0], r[1]) if r[2] == 'fk' else ''})" for r in ctx.q(
        "SELECT from_table, from_column, kind, confidence FROM relationships WHERE to_table=? COLLATE NOCASE "
        "ORDER BY declared DESC, occurrences DESC LIMIT 25", tname)]
    ent = [f"{r[0]} @ {ctx.cs(r[1], r[2])}" for r in ctx.q(
        "SELECT class_fqn, rel_path, line FROM code_entities WHERE table_name=? COLLATE NOCASE", tname)]
    if any(c.note for c in cols):
        caveats.append("note values name topics; describe_column gives the full note (e.g. currency).")
    row_kinds = [r[0] for r in ctx.q("SELECT note FROM column_notes WHERE tbl=? COLLATE NOCASE AND col='*' "
                                     "AND topic='row_kind'", tname)]
    if exact:
        have = {c["name"].lower() for c in all_cols}
        missing = [c for c in wanted if c.lower() not in have]
        if missing:
            near = []
            for m in missing[:3]:
                like = [r[0] for r in ctx.q("SELECT name FROM columns WHERE obj_id=? AND name LIKE ? LIMIT 5",
                                            oid, "%" + m[:4] + "%")]
                if like:
                    near.append(f" Near {m}: {', '.join(like)}.")
            caveats.append(f"{tname} has no column {', '.join(missing)}." + "".join(near))
    if exact or filt:
        caveats.append(f"{len(cols)} of {len(all_cols)} columns shown ("
                       + (f"columns={sorted(exact)}" if exact else f"columns_like={columns_like!r}: substring match")
                       + ").")
    return TableOut(status="ok", layer="mixed", searched_scope=scope, name=tname, kind=o["kind"],
                    rows=o["row_count"], description=o["description"], pk=pk[0] if pk else None, row_kinds=row_kinds,
                    columns=cols, referenced_by=refby, entity=ent, caveats=caveats,
                    total_available=len(all_cols), truncated=bool(filt or exact))


def live_definition(ctx: Ctx, name: str) -> Optional[str]:
    """Current module text from the database (LF-normalised); "" when the database has no text for it
    (encrypted, dropped, not a module); None when the database could not be asked."""
    import pyodbc
    from ..prep.catalog import connect
    try:
        con = connect(ctx.cfg, timeout=ctx.cfg.timeouts.live_definition_s)
        row = con.cursor().execute("select object_definition(object_id(?))", name).fetchone()
        con.close()
    except pyodbc.Error:
        return None
    if not row or not row[0]:
        return ""
    return row[0].replace("\r\n", "\n").replace("\r", "\n")


def definition(ctx: Ctx, name: str, line_start: int, line_end: Optional[int], live: bool = False,
               raw: bool = False) -> DefinitionOut:
    o = ctx.obj(name)
    scope = ctx.scope("module definitions (views, procedures, functions, triggers)")
    if not o or o["kind"] == "table":
        sugg = ctx.suggest(name, ("view", "proc", "scalar_fn", "inline_tvf", "tvf", "trigger"))
        return DefinitionOut(status="no_results", layer="catalog", searched_scope=scope,
                             caveats=[f"No module named '{name}'." + (f" Did you mean: {', '.join(sugg)}?" if sugg else "")])
    layer = "catalog"
    caveats = ctx.masked_caveats()
    if live:
        text_live = None if o["name"].lower() in ctx.eval_masked_names() else live_definition(ctx, o["name"])
        if text_live is None:
            return DefinitionOut(status="unavailable", layer="live", searched_scope="live database",
                                 caveats=["live definition not available (database unreachable or not permitted)"])
        if not text_live:
            return DefinitionOut(status="no_results", layer="live", searched_scope="live database",
                                 name=o["name"], kind=o["kind"],
                                 caveats=[f"the live database returns no text for {o['name']} (encrypted, dropped "
                                          f"since the index build, or not visible to this login)"])
        lines, layer = text_live.split("\n"), "live"
        caveats.append("Live definition: line numbers refer to the current database text, not to the index.")
    else:
        lines = ctx.module_lines(o["obj_id"])
        caveats += ctx.stale_caveats([o["name"]])
    n = len(lines)
    ls = max(1, line_start or 1)
    le = min(n, line_end or n)
    cap = ctx.cfg.limits.definition_max_lines
    trunc = le - ls + 1 > cap
    if trunc:
        le = ls + cap - 1
    # 'N: ' and not 'N<tab>': a tab after the number reads as one more indent of the line (runs 8-10)
    text = "\n".join(lines[i - 1] if raw else f"{i}: {lines[i - 1]}" for i in range(ls, le + 1))
    if raw:
        caveats.append(f"raw=true: the text is the module's own lines {ls}-{le}, without line numbers.")
    if trunc:
        caveats.append(f"Truncated at {cap} lines; ask for line_start={le + 1}.")
    return DefinitionOut(status="ok", layer=layer, searched_scope=scope, name=o["name"], kind=o["kind"],
                         modified=o["modified"], line_start=ls, line_end=le, total_lines=n, text=text,
                         truncated=trunc, caveats=caveats)


def decode(ctx: Ctx, tbl: Optional[str], col: str, value: str) -> str:
    if not tbl:
        return ""
    vals = {}
    for r in ctx.q("SELECT cvm.map_id FROM column_value_maps cvm WHERE cvm.tbl=? COLLATE NOCASE AND cvm.col=? COLLATE NOCASE "
                   "ORDER BY cvm.confidence='exact' DESC", tbl, col):
        for v, lab in ctx.value_map_items(r[0]).items():
            vals.setdefault(v, lab)
    out = []
    for v in re.split(r"[,.]{1,2}", value):
        v = v.strip().strip("'")
        if v in vals:
            out.append(f"{v}={vals[v]}")
    return f" ({'; '.join(out)})" if out else ""


DYNAMIC_STMT = 100000           # SqlParser numbers statements re-parsed from dynamic SQL from here
RUNS_DYNAMIC_RE = re.compile(r"(?i)\b(sp_executesql|sp_sqlexec)\b|\bexec(?:ute)?\s*\(\s*@")


def _dynamic_caveats(ctx: Ctx, oid: int) -> list[str]:
    """Say what the outline of a module that runs dynamic SQL does and does not show."""
    d = ctx.q1("SELECT definition FROM modules WHERE obj_id=?", oid)
    if not d or not RUNS_DYNAMIC_RE.search(d[0]):
        return []
    owners = [r[0][8:] for r in ctx.q("SELECT DISTINCT owner FROM sql_outputs WHERE obj_id=? AND owner LIKE 'dynamic:%'",
                                      oid)]
    span = ctx.q1("SELECT min(line), max(line) FROM (SELECT line FROM sql_aliases WHERE obj_id=? AND stmt>=? "
                  "UNION ALL SELECT line FROM sql_outputs WHERE obj_id=? AND stmt>=?)", oid, DYNAMIC_STMT, oid, DYNAMIC_STMT)
    if span and span[0]:
        return [f"This module runs dynamic SQL. Entries marked [dynamic SQL] and the dynamic result set"
                f"{' of ' + ', '.join(owners) if owners else ''} come from re-parsing the string literals it is built "
                f"from (lines {span[0]}-{span[1]}); pieces built at run time (parameters, CASE, functions) are not "
                f"parsed, so columns, joins or filters they add are missing -- read the definition for those."]
    return ["This module runs dynamic SQL (sp_executesql / EXEC(@sql)) whose text could not be outlined (it is "
            "built mostly at run time). aliases, joins and outputs below cover only its static SQL: read the "
            "definition for what the dynamic SQL selects."]


def outline(ctx: Ctx, name: str) -> OutlineOut:
    o = ctx.obj(name)
    scope = ctx.scope("ScriptDom parse of module definitions")
    if not o or o["kind"] == "table":
        sugg = ctx.suggest(name, ("view", "proc", "scalar_fn", "inline_tvf", "tvf", "trigger"))
        return OutlineOut(status="no_results", layer="sql-parse", searched_scope=scope,
                          caveats=[f"No module named '{name}'." + (f" Did you mean: {', '.join(sugg)}?" if sugg else "")])
    oid, oname = o["obj_id"], o["name"]
    caveats = ctx.masked_caveats() + ctx.stale_caveats([oname])
    st = ctx.q1("SELECT ok, errors FROM sql_parse_status WHERE obj_id=?", oid)
    if st and not st[0]:
        caveats.append(f"Parse errors: {st[1]}")
    # parameters, with report filter semantics
    pmaps = {}
    for r in ctx.q("SELECT cvm.col, vm.name, vm.map_id, cvm.via FROM column_value_maps cvm JOIN value_maps vm "
                   "ON vm.map_id=cvm.map_id WHERE cvm.tbl=? COLLATE NOCASE AND cvm.col LIKE '@%'", oname):
        from .common import compact_values
        pmaps.setdefault(r[0].lower(), f"{r[3]}: {compact_values(ctx.value_map_items(r[2]))}")
    filters_ui = {}
    for r in ctx.q("SELECT f.filter_name, f.caption, f.filter_type, f.required, f.default_value, r.title FROM ui_report_filters f "
                   "JOIN ui_reports r ON r.report_name=f.report_name AND r.rel_path=f.rel_path "
                   "WHERE r.object_name=? COLLATE NOCASE", oname):
        for part in (r[0] or "").split("#"):
            filters_ui.setdefault("@" + part.lower(), f"report filter '{r[1] or r[0]}' {r[2]}"
                                  + (f" default={r[4]}" if r[4] else "") + (f" required" if r[3] == "true" else ""))
    params = []
    for p in ctx.q("SELECT * FROM params WHERE obj_id=? AND param_id>0 ORDER BY param_id", oid):
        s = f"{p['name']} {p['type_name']}" + (" OUTPUT" if p["is_output"] else "")
        extra = [x for x in (p["description"], filters_ui.get(p["name"].lower()), pmaps.get(p["name"].lower())) if x]
        if extra:
            s += " -- " + "; ".join(extra)
        params.append(s)
    dyn = " [dynamic SQL]"
    aliases, seen = [], set()
    for r in ctx.q("SELECT alias, target, target_kind, line, stmt FROM sql_aliases WHERE obj_id=? ORDER BY line", oid):
        key = (r[0].lower(), r[1].lower())
        if key in seen:
            continue
        seen.add(key)
        aliases.append(f"{r[0]}={r[1]}" + ("" if r[2] == "object" else f" [{r[2]}]") + f" @{r[3]}"
                       + (dyn if r[4] >= DYNAMIC_STMT else ""))
    joins, seen = [], set()
    for r in ctx.q("SELECT join_type, predicate, line, stmt FROM sql_joins WHERE obj_id=? AND join_type<>'where' "
                   "ORDER BY line", oid):
        key = (r[1], r[2])
        if key in seen:
            continue
        seen.add(key)
        joins.append(f"{r[0]}: {r[1]} @{r[2]}" + (dyn if r[3] >= DYNAMIC_STMT else ""))
    outputs = []
    sets = [(r[0], r[1]) for r in ctx.q("SELECT DISTINCT stmt, owner FROM sql_outputs WHERE obj_id=? AND "
                                         "(owner='result' OR owner LIKE 'dynamic:%') ORDER BY stmt", oid)]
    for s, owner in sets:
        if owner != "result":
            outputs.append(f"-- result set of the dynamic SQL in {owner[8:]} (statement {s % DYNAMIC_STMT}"
                           f" of that text)")
        elif len(sets) > 1:
            outputs.append(f"-- result set of statement {s}")
        for r in ctx.q("SELECT ordinal, out_name, expression, sources, line FROM sql_outputs "
                       "WHERE obj_id=? AND owner=? AND stmt=? ORDER BY ordinal", oid, owner, s):
            srcs = json.loads(r[3])
            base = ", ".join(f"{t}.{c}" + ("" if k == "object" else f"[{k}]") for t, c, k in srcs[:4] if t)
            expr = r[2] if len(r[2]) <= 110 else r[2][:110] + "…"
            nm = r[1] or "(no name)"
            outputs.append(f"{r[0]} {nm}" + ("" if expr == f"{nm}" or expr.endswith("." + nm) and len(srcs) == 1 else f" = {expr}")
                           + (f" <- {base}" if base else "") + f" @{r[4]}")
    inter = ctx.q("SELECT owner, count(*) FROM sql_outputs WHERE obj_id=? AND owner<>'result' "
                  "AND owner NOT LIKE 'dynamic:%' GROUP BY owner", oid)
    temp = [f"{r[0]} ({r[1]} cols)" for r in inter]
    filters, seen = [], set()
    for r in ctx.q("SELECT tbl, col, op, value, line FROM sql_literals WHERE obj_id=? ORDER BY line", oid):
        key = (r[0], r[1], r[2], r[3])
        if key in seen:
            continue
        seen.add(key)
        filters.append(f"{r[0] or '?'}.{r[1]} {r[2]} {r[3]}{decode(ctx, r[0], r[1], r[3])} @{r[4]}")
    calls = sorted({f"{r[0]} ({r[1]}) @{r[2]}" for r in ctx.q(
        "SELECT callee, kind, min(line) FROM sql_calls WHERE obj_id=? GROUP BY callee, kind", oid)})
    called_by = sorted({r[0] for r in ctx.q(
        "SELECT o.name FROM sql_calls c JOIN objects o ON o.obj_id=c.obj_id WHERE c.callee=? COLLATE NOCASE", oname)}
                       | {r[0] for r in ctx.q(
        "SELECT o.name FROM deps d JOIN objects o ON o.obj_id=d.referencing_id WHERE d.referenced_id=?", oid)})
    for r in ctx.q("SELECT target_kind, name, url FROM ui_targets WHERE object_name=? COLLATE NOCASE", oname):
        called_by.append(f"{'grid screen' if r[0] == 'view' else 'report'} {r[1]} ({r[2]})")
    for r in ctx.q("SELECT DISTINCT s.rel_path, s.line, s.container FROM code_sql_refs x JOIN code_sql s ON "
                   "s.code_sql_id=x.code_sql_id WHERE x.ref_kind='object' AND x.tbl=? COLLATE NOCASE LIMIT 20", oname):
        called_by.append(f"C# {ctx.cs(r[0], r[1])} {r[2]}")
    caveats += _dynamic_caveats(ctx, oid)
    purpose = ctx.q1("SELECT text, citations FROM curated WHERE subject=? COLLATE NOCASE AND fact_type='purpose'",
                     f"object:{oname}")
    return OutlineOut(status="ok", layer="sql-parse", searched_scope=scope, name=oname, kind=o["kind"],
                      purpose=(purpose[0] + f" [curated; {', '.join(json.loads(purpose[1])[:3])}]") if purpose else o["description"],
                      params=params, aliases=aliases, joins=joins, outputs=outputs, filters=filters,
                      calls=calls, called_by=called_by, temp_tables=temp, caveats=caveats)


def search_sql(ctx: Ctx, pattern: str, regex: bool, kinds: Optional[list[str]], mx: int) -> SearchSqlOut:
    mx = max(1, min(mx, ctx.cfg.limits.max_results_hard))
    kinds_n = [KIND_ALIASES.get(k.lower(), k.lower()) for k in (kinds or [])]
    try:
        rx = re.compile(pattern if regex else re.escape(pattern), re.I)
    except re.error as e:
        return SearchSqlOut(status="error", layer="text", searched_scope="", caveats=[f"bad regex: {e}"])
    sql = "SELECT o.name, o.kind, m.definition FROM modules m JOIN objects o ON o.obj_id=m.obj_id"
    rows = ctx.q(sql)
    found: dict[str, list[str]] = {}
    for name, kind, d in rows:
        if kinds_n and kind not in kinds_n:
            continue
        for i, line in enumerate(d.split("\n"), 1):
            if rx.search(line):
                found.setdefault(name, []).append(f"{name}:{i}: {line.strip()[:160]}")
    total = sum(len(v) for v in found.values())
    # objects whose name matches first, then the ones with the most matching lines: with a broad word the useful
    # object is the one named after it, or the dense one
    ranked = sorted(found.items(), key=lambda kv: (not rx.search(kv[0]), -len(kv[1]), kv[0].lower()))
    out = [m for _n, ms in ranked for m in ms][:mx]
    scope = ctx.scope(f"text of {len(rows)} module definitions" + (f" of kind {kinds_n}" if kinds_n else ""))
    caveats = ctx.masked_caveats() + ["Text match only: aliases are not resolved; use find_column_usage for that."]
    if total > mx and ranked:
        caveats.append("Most matches in: " + ", ".join(f"{n} ({len(ms)} lines)" for n, ms in ranked[:6])
                       + f" ({len(found)} objects match; objects named like the pattern first, then the densest).")
    return SearchSqlOut(status="ok" if out else "no_results", layer="text", searched_scope=scope,
                        matches=out, truncated=total > mx, total_available=total, caveats=caveats)


def register(mcp, ctx: Ctx) -> None:
    @mcp.tool(name="find_db_object", annotations=RO, description=(
        "Find tables, views, procedures, functions and columns by name, description, UI caption "
        "('Channel Type'), screen/report title ('Stock Adjustments Report'), page URL "
        "(.../views/shipmentview.aspx) or value label ('gift card'). Returns canonical names to pass "
        "to the other tools. kinds: table|view|proc|scalar_fn|tvf|column|screen|report|grid_column|"
        "report_column|value_map|object. (query is also accepted as name.)"))
    def find_db_object(query: Optional[str] = None, kinds: Optional[list[str]] = None, max: int = 20,
                       name: Optional[str] = None) -> FindOut:
        q = query or name
        if not q:
            return FindOut(status="error", layer="mixed", searched_scope="(no query)",
                           caveats=["find_db_object needs query='<name, caption, title or label>'"])
        return find(ctx, q, kinds, max)

    @mcp.tool(name="describe_table", annotations=RO, description=(
        "Compact column grid of a table or view: type, nullability, meaning, decoded value list, FK or "
        "inferred relationship, note topics (currency, date_clean, synced, derived); plus row count, PK, "
        "referencing tables and the C# entity. columns=['Ref','Sku'] shows exactly those columns; "
        "columns_like='status,date' filters columns by substring."))
    def describe_table(name: str, columns_like: Optional[str] = None,
                       columns: Union[list[str], str, None] = None) -> TableOut:
        return describe_table_impl(ctx, name, columns_like, columns)

    @mcp.tool(name="get_object_definition", annotations=RO, description=(
        "Verbatim text of a view/procedure/function/trigger with line numbers (bounded). Cite "
        "Object:line from it. Prefer get_object_outline first; read only the lines you need. live=true reads "
        "the CURRENT text from the database (use it when a caveat says the object changed after the index build). "
        "raw=true returns the lines without the number prefix (to see the real indentation)."))
    def get_object_definition(name: str, line_start: int = 1, line_end: Optional[int] = None,
                              live: bool = False, raw: bool = False) -> DefinitionOut:
        return definition(ctx, name, line_start, line_end, live, raw)

    @mcp.tool(name="get_object_outline", annotations=RO, description=(
        "Parsed structure of a view/procedure/function without reading it: parameters (with report "
        "filter meaning), alias->table map, joins, output columns with lineage to base columns, "
        "literal filters with decoded values (PayMethod = 6 -> Gift Card Redemption), calls, callers "
        "(including the screens/reports that use it). Every item has @line."))
    def get_object_outline(name: str) -> OutlineOut:
        return outline(ctx, name)

    @mcp.tool(name="search_sql", annotations=RO, description=(
        "Plain text/regex search over all module definitions (case-insensitive). A text match only; "
        "returns Object:line: text. (pattern is also accepted as query.)"))
    def search_sql_tool(pattern: Optional[str] = None, regex: bool = False, kinds: Optional[list[str]] = None,
                        max: int = 50, query: Optional[str] = None) -> SearchSqlOut:
        p = pattern or query
        if not p:
            return SearchSqlOut(status="error", layer="text", searched_scope="(no pattern)",
                                caveats=["search_sql needs pattern='<text or regex>'"])
        return search_sql(ctx, p, regex, kinds, max)
