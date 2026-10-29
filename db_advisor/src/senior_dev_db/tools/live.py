"""validate_sql: compile-and-bind a proposed SQL batch against the live database
without executing it.

Safety, in depth:
  1. the connection is opened read-only (ApplicationIntent=ReadOnly) with a login
     that has no write/EXECUTE permission;
  2. SET NOEXEC ON is sent as its own batch first, so every following batch is only
     compiled (measured: an ALTER VIEW under NOEXEC leaves modify_date unchanged);
  3. NOEXEC is switched off again only after the last proposed batch.
On top of the engine's compile (which defers unknown *tables* inside procedures), the
batch is parsed with ScriptDom and every table/column it names is checked against
the catalog snapshot.
"""

from __future__ import annotations

import re
from typing import Optional

from ..models import PreviewOut, ValidateOut
from .common import RO, Ctx
from .stylecheck import lint

GO_RE = re.compile(r"(?im)^\s*go\s*(?:--.*)?$")
SKIP_RE = re.compile(r"(?is)^\s*(use\s+\[?\w+\]?|set\s+(ansi_nulls|quoted_identifier)\s+(on|off))\s*;?\s*$")
VIEW_RE = re.compile(r"(?is)^\s*(?:create\s+or\s+alter|create|alter)\s+view\s+[\w\.\[\]]+\s*(?:\([^)]*\))?\s*(?:with\s+\w+\s*)?as\s+(.*)$")


def _strip_comments(s: str) -> str:
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    return re.sub(r"--[^\n]*", "", s)


def code_only(sql: str) -> str:
    """The SQL without comments and with every string literal emptied: what the read-only guards scan."""
    return re.sub(r"'(?:[^']|'')*'", "''", _strip_comments(sql))


def split_batches(sql: str) -> list[str]:
    """The script's batches (split on GO), without the USE / SET ANSI_NULLS ones a deploy script carries."""
    return [b.strip() for b in GO_RE.split(sql) if b.strip() and not SKIP_RE.match(_strip_comments(b))]


def odbc_text(e: Exception) -> str:
    """A pyodbc error (or message text) without its [driver][server] prefixes."""
    return re.sub(r"\[[^\]]*\]", "", str(e.args[1] if len(getattr(e, "args", ())) > 1 else e)).strip()


def is_timeout(msg: str, e: Exception) -> bool:
    return "timeout" in msg.lower() or "HYT00" in str(e)


def cell(v) -> Optional[str]:
    """A result value as shown in a tool answer: text, cut at 100 characters."""
    if v is None:
        return None
    s = str(v)
    return s if len(s) <= 100 else s[:100] + "…"


def _parse(ctx: Ctx, batches: list[str]) -> list[dict] | None:
    import tempfile
    from pathlib import Path
    from ..prep.sqlparse import parse_modules, catalog_columns
    try:
        with tempfile.TemporaryDirectory(prefix="dbadv-validate-") as tmp:
            return parse_modules(ctx.cfg, list(enumerate(batches)), catalog_columns(ctx.con), tag="validate",
                                 stage=Path(tmp))
    except Exception:  # parser missing etc.
        return None


VIEW_NAME_RE = re.compile(r"(?is)^\s*(?:create\s+or\s+alter|create|alter)\s+view\s+(?:\[?dbo\]?\.)?\[?(\w+)\]?")


def _new_view_columns(batches: list[str], recs: list[dict] | None) -> dict[int, dict[str, set[str]]]:
    """For each batch index: view -> output column names created by EARLIER view batches of the script."""
    out, acc = {}, {}
    for i, b in enumerate(batches):
        out[i] = {k: set(v) for k, v in acc.items()}
        m = VIEW_NAME_RE.match(_strip_comments(b))
        if m and recs:
            names = {o[3].lower() for o in recs[i]["outputs"] if o[1] == "result" and o[3]}
            acc.setdefault(m.group(1).lower(), set()).update(names)
    return out


def _catalog_check(ctx: Ctx, r: dict, pending: dict[str, set[str]]) -> list[str]:
    """Names the batch uses that neither the catalog nor an earlier batch of the script defines."""
    out = [f"parse: {e}" for e in r["errors"]]
    ctes = {a[2].lower() for a in r["aliases"] if a[3] in ("cte", "derived", "temp", "tvf")}
    for _stmt, alias, target, kind, line in r["aliases"]:
        if kind == "object" and target.lower() not in ctes and not ctx.obj(target):
            out.append(f"line {line}: unknown table/view '{target}' (alias {alias})")
    created = {w[1].lower() for w in r.get("writes", []) if w[0].startswith("create")}
    for t, c, usage, line in r["colrefs"]:
        o = ctx.obj(t)
        if o is None and not t.startswith(("#", "@")) and t.lower() not in ctes | created \
                and usage in ("set", "insert", "where"):
            out.append(f"line {line}: unknown table/view '{t}'")
            continue
        if o and not ctx.column_row(o["obj_id"], c) and c.lower() not in pending.get(o["name"].lower(), set()):
            out.append(f"line {line}: {o['name']} has no column '{c}'")
    names = [o[3] for o in r["outputs"] if o[1] == "result" and o[3] and o[3] != "*"]
    dup = sorted({n for n in names if names.count(n) > 1})
    if dup:
        out.append("duplicate output column names: " + ", ".join(dup))
    return list(dict.fromkeys(out))


def validate(ctx: Ctx, sql: str, draft_id: Optional[str] = None, handover: Optional[str] = None) -> ValidateOut:
    out = _validate(ctx, sql, draft_id)
    if draft_id and handover and handover.strip():
        from .drafts import handover_check
        out.caveats = handover_check(draft_id, handover) + out.caveats
    return out


def _validate(ctx: Ctx, sql: str, draft_id: Optional[str] = None) -> ValidateOut:
    scope = "live compile (SET NOEXEC ON; nothing executed) + catalog name check"
    from .drafts import combine
    sql, style_only, draft_notes, err, style_exempt = combine(draft_id, sql)
    if err:
        return ValidateOut(status="error", layer="live", searched_scope=scope, caveats=[err])
    try:
        import pyodbc
        from ..prep.catalog import connect
    except ImportError as e:
        return ValidateOut(status="unavailable", layer="live", searched_scope=scope, caveats=[str(e)])
    batches = split_batches(sql)
    if not batches:
        return ValidateOut(status="no_results", layer="live", searched_scope=scope, caveats=["empty batch"])
    errors: list[str] = []
    perm_notes: list[str] = []
    cols: list[str] = []
    recs = _parse(ctx, batches)
    pending = _new_view_columns(batches, recs)

    def catalog_errors() -> list[str]:
        if recs is None:
            return ["(catalog check skipped: SqlParser unavailable)"]
        return [f"batch {i + 1}: {e}" for i, r in enumerate(recs) for e in _catalog_check(ctx, r, pending[i])]
    try:
        con = connect(ctx.cfg, timeout=ctx.cfg.db.validate_timeout_s)
    except Exception as e:
        w_errors, changes = _write_review(ctx, recs)
        return ValidateOut(status="unavailable", layer="live", searched_scope=scope,
                           caveats=[f"database not reachable: {str(e)[:200]}", "Catalog name check only below."],
                           errors=catalog_errors() + w_errors, changes=changes)
    cur = con.cursor()
    try:
        cur.execute("SET NOEXEC ON")
        for i, b in enumerate(batches, 1):
            try:
                cur.execute(b)
                while cur.nextset():
                    pass
            except pyodbc.Error as e:
                msg = odbc_text(e)
                if "permission was denied" in msg.lower() or "(229)" in msg:
                    # the prepare login lacks EXECUTE on UDFs; says nothing about the proposed SQL,
                    # but the engine stops binding at that point -- the catalog check below covers names
                    perm_notes.append(f"batch {i}: engine compile stopped on a permission check of the "
                                      f"read-only login, not on your SQL ({msg[:160]}); names were "
                                      f"checked against the catalog instead.")
                else:
                    bad = re.search(r"Invalid column name '([^']+)'", msg)
                    earlier = set().union(*pending[i - 1].values()) if pending[i - 1] else set()
                    if bad and bad.group(1).lower() in earlier:
                        perm_notes.append(f"batch {i}: column '{bad.group(1)}' is added by an earlier ALTER VIEW in "
                                          f"this script; the engine cannot see it until that batch is deployed "
                                          f"(NOEXEC), so it was checked against the parsed view instead. "
                                          f"Deploy the batches in order.")
                    else:
                        errors.append(f"batch {i}: {msg[:400]}")
        cur.execute("SET NOEXEC OFF")
        # result columns of a view body / plain select (describe never executes)
        for bi, b in enumerate(batches):
            body = _strip_comments(b).strip()
            m = VIEW_RE.match(body)
            q = m.group(1) if m else (body if re.match(r"(?is)^\s*(select|with)\b", body) else None)
            if q and not m and recs and (recs[bi].get("writes") or re.search(r"(?i)\binto\s+#", body)):
                q = None    # a change script, not a query: there is no result set to describe
            if q:
                try:
                    rows = cur.execute("SELECT name, system_type_name, error_message FROM "
                                       "sys.dm_exec_describe_first_result_set(?, NULL, 0)", q).fetchall()
                    for r in rows:
                        if r[2]:
                            if "permission was denied" in r[2].lower():
                                perm_notes.append("result columns not described: the read-only login lacks "
                                                  "EXECUTE on a function the SELECT calls")
                            else:
                                errors.append(f"describe: {r[2][:300]}")
                            break
                        cols.append(f"{r[0]} {r[1]}")
                except pyodbc.Error as e:
                    if "permission was denied" in str(e).lower():
                        perm_notes.append("result columns not described: the read-only login lacks EXECUTE "
                                          "on a function the SELECT calls")
                    else:
                        errors.append(f"describe: {str(e)[:300]}")
    finally:
        try:
            cur.execute("SET NOEXEC OFF")
        except Exception:
            pass
        con.close()
    errors.extend(catalog_errors())
    w_errors, changes = _write_review(ctx, recs)
    errors.extend(w_errors)
    errors = list(dict.fromkeys(errors))
    style = lint(sql, only=style_only, exempt=style_exempt)
    notes = draft_notes + perm_notes + ["Compiled only (SET NOEXEC ON): run-time errors (conversion, divide by "
                                        "zero, duplicate keys) are not detected."]
    modules = [m.group(2) for c in changes
               for m in [re.search(r"\b(replaces|creates) (?:procedure|view|function|trigger) (\S+?):", c)] if m]
    if changes and len(modules) == len(changes):
        # only module definitions: no rows to back up, the rollback is the current text
        notes.append(f"This script replaces or creates {', '.join(modules)}: the rollback of a replacement is its "
                     "current text (get_object_definition(name, live=true)); 'changes' lists the screens/reports built "
                     "on it.")
    elif changes:
        notes.append("This script changes data or schema. 'changes' lists each statement with what it touches; "
                     "check the rows a DELETE/UPDATE hits with a preview_query count, and hand the script over "
                     "with the safety steps from the agent rules (transaction, row-count check, backup of what it removes).")
    if not cols and any(re.match(r"(?is)^\s*(?:create|alter)(?:\s+or\s+alter)?\s+proc", _strip_comments(b))
                        for b in batches):
        notes.append("A procedure's output columns are not described by a compile (nothing is executed): run it with "
                     "test_sql (draft_id + exec) to see them.")
    if style:
        notes.append("style lists deviations from the team SQL style (get_house_style team_rules); "
                     "reformat before handing over. Line numbers count from the start of the submitted script.")
    return ValidateOut(status="ok", layer="live", searched_scope=scope, ok=not errors, errors=errors,
                       changes=changes, style=style, result_columns=cols[:200], truncated=len(cols) > 200, caveats=notes)


MODULE_KINDS = {"procedure": ("proc",), "view": ("view",), "function": ("scalar_fn", "inline_tvf", "tvf"),
                "trigger": ("trigger",), "table": ("table",), "synonym": ()}


def _impact_summary(ctx: Ctx, name: str, column: Optional[str] = None) -> str:
    from .lineage import impact
    im = impact(ctx, name, column, detail=True)   # counts below need every dependent, not the summary
    direct = [d for d in im.db_dependents if not d.startswith(" ")]
    if column:
        direct = [d for d in direct if "does not reference" not in d]
    bits = [f"{len(direct)} database object(s) read it directly"]
    if direct:
        bits[0] += " (" + ", ".join(d.split()[1] for d in direct[:5]) + (", ..." if len(direct) > 5 else "") + ")"
    if im.ui_consumers:
        bits.append(f"{len(im.ui_consumers)} screen/report(s) built on "
                    + ("objects that read the table" if column else "it"))
    if im.code_consumers:
        bits.append(f"{len(im.code_consumers)} C# use(s)")
    return "; ".join(bits) + f" -- get_impact('{name}'" + (f", '{column}'" if column else "") + ") for the list"


def _write_review(ctx: Ctx, recs: list[dict] | None) -> tuple[list[str], list[str]]:
    """Errors and change notes for statements that change data or schema (compile-only cannot see these)."""
    errors: list[str] = []
    changes: list[str] = []
    if not recs:
        return errors, changes
    created: set[str] = set()

    def local(n: str) -> bool:
        return n.startswith(("#", "@")) or n.lower() in created

    def table(n: str):
        o = ctx.obj(n)
        return o if o is not None and o["kind"] in ("table", "view") else None

    def rows(o) -> str:
        return f"~{o['row_count']:,} rows at index build" if o is not None and o["row_count"] is not None else "row count unknown"

    for bi, r in enumerate(recs, 1):
        ctes = {a[2].lower() for a in r["aliases"] if a[3] == "cte"}
        for kind, target, detail, line in r.get("writes", []):
            at = f"batch {bi} line {line}"
            if local(target) or target.lower() in ctes:
                if kind.startswith("create"):
                    created.add(target.lower())
                continue
            o = ctx.obj(target)
            if kind in ("update", "delete", "insert", "merge"):
                t = table(target)
                if t is None:
                    errors.append(f"{at}: {kind.upper()} target '{target}' is not a table or view in the database")
                    continue
                verb = kind.upper()
                if t["kind"] == "view":
                    changes.append(f"{at}: {verb} through view {t['name']} (changes its base table)")
                if kind in ("update", "delete") and detail == "no filter":
                    changes.append(f"{at}: WARNING {verb} {t['name']} has no WHERE: it changes every row ({rows(t)}).")
                elif kind in ("update", "delete"):
                    changes.append(f"{at}: {verb} {t['name']} ({detail}). Check the rows it hits first: preview_query "
                                   f"'select count(*)' with the same FROM/JOIN/WHERE.")
                else:
                    changes.append(f"{at}: {verb} into {t['name']}.")
                if kind == "delete":
                    by_action: dict[str, list[str]] = {}
                    for a, b, c in ctx.q("SELECT DISTINCT p.name, f.parent_column, coalesce(f.on_delete, 'NO_ACTION') "
                                         "FROM fks f JOIN objects p ON p.obj_id=f.parent_obj_id WHERE f.ref_obj_id=? "
                                         "AND f.is_disabled=0 AND f.parent_obj_id<>f.ref_obj_id", t["obj_id"]):
                        by_action.setdefault(c, []).append(f"{a}.{b}")

                    def names(action):
                        got = by_action[action]
                        return ", ".join(got[:8]) + (f", ... ({len(got)} in all)" if len(got) > 8 else "")
                    if "NO_ACTION" in by_action:
                        changes.append(f"{at}: rows referenced from {names('NO_ACTION')} (foreign key, NO ACTION) make "
                                       f"this DELETE fail: delete or re-point those rows first.")
                    if "CASCADE" in by_action:
                        changes.append(f"{at}: WARNING deleting from {t['name']} also deletes the referencing rows in "
                                       f"{names('CASCADE')} (ON DELETE CASCADE, and their own cascades). No separate "
                                       f"delete is needed; back them up too.")
                    for act in ("SET_NULL", "SET_DEFAULT"):
                        if act in by_action:
                            changes.append(f"{at}: referencing rows in {names(act)} get the column set to "
                                           f"{'NULL' if act == 'SET_NULL' else 'its default'} (ON DELETE {act.replace('_', ' ')}).")
            elif kind == "truncate":
                t = table(target)
                if t is None or t["kind"] != "table":
                    errors.append(f"{at}: TRUNCATE target '{target}' is not a table in the database")
                    continue
                refs = ctx.q("SELECT DISTINCT p.name FROM fks f JOIN objects p ON p.obj_id=f.parent_obj_id "
                             "WHERE f.ref_obj_id=? AND f.parent_obj_id<>f.ref_obj_id", t["obj_id"])
                if refs:
                    errors.append(f"{at}: TRUNCATE TABLE {t['name']} fails at run time: it is referenced by a FOREIGN KEY "
                                  f"from {', '.join(x[0] for x in refs[:8])}. Use DELETE (in batches) instead.")
                else:
                    changes.append(f"{at}: WARNING TRUNCATE removes every row of {t['name']} ({rows(t)}), cannot be filtered.")
            elif kind.startswith("drop ") and kind != "drop index":
                what = kind[5:]
                ok_kinds = MODULE_KINDS.get(what)
                exists = o is not None and (not ok_kinds or o["kind"] in ok_kinds)
                if not exists:
                    if detail == "if exists":
                        changes.append(f"{at}: {kind.upper()} IF EXISTS {target}: it does not exist now (no-op).")
                    else:
                        errors.append(f"{at}: {kind.upper()} {target}: no such {what} in the database "
                                      f"(add IF EXISTS if that is expected)")
                    continue
                extra = f" ({rows(o)})" if what == "table" else ""
                changes.append(f"{at}: WARNING {kind.upper()} {o['name']}{extra}: {_impact_summary(ctx, o['name'])}.")
            elif kind == "alter table":
                t = table(target)
                if t is None or t["kind"] != "table":
                    errors.append(f"{at}: ALTER TABLE target '{target}' is not a table in the database")
                    continue
                for part in re.split(r",\s*", detail or ""):
                    m = re.match(r"(add|drop|alter) column (\w+)", part.strip())
                    if not m:
                        continue
                    act, col = m.groups()
                    has = ctx.column_row(t["obj_id"], col) is not None
                    if act == "add" and has:
                        errors.append(f"{at}: {t['name']} already has a column '{col}'")
                    elif act != "add" and not has:
                        errors.append(f"{at}: {t['name']} has no column '{col}'")
                    elif act == "drop":
                        changes.append(f"{at}: WARNING drops {t['name']}.{col}: {_impact_summary(ctx, t['name'], col)}.")
                    elif act == "alter":
                        changes.append(f"{at}: alters {t['name']}.{col}: {_impact_summary(ctx, t['name'], col)}.")
                    else:
                        changes.append(f"{at}: adds {t['name']}.{col}. A screen or report shows it only after it is "
                                       f"registered in C# (get_impact('{t['name']}') lists the registrations).")
            elif kind.startswith(("create", "alter")) and kind != "create index":
                how, what = kind.rsplit(" ", 1)
                ok_kinds = MODULE_KINDS.get(what, ())
                exists = o is not None and o["kind"] in ok_kinds
                if how == "create" and exists:
                    errors.append(f"{at}: {what} {o['name']} already exists: use ALTER (or CREATE OR ALTER)")
                elif how == "alter" and not exists:
                    errors.append(f"{at}: ALTER {what.upper()} {target}: no such {what} in the database (use CREATE)")
                elif exists and what in ("view", "procedure", "function"):
                    changes.append(f"{at}: replaces {what} {o['name']}: {_impact_summary(ctx, o['name'])}.")
                created.add(target.lower())
            elif kind in ("create index", "drop index"):
                t = table(target)
                if t is None:
                    errors.append(f"{at}: {kind.upper()} on '{target}': no such table or view")
                    continue
                has = ctx.q1("SELECT 1 FROM indexes WHERE obj_id=? AND name=? COLLATE NOCASE", t["obj_id"], detail)
                if kind == "create index" and has:
                    errors.append(f"{at}: index {detail} already exists on {t['name']}")
                elif kind == "drop index" and not has:
                    errors.append(f"{at}: no index {detail} on {t['name']}")
                else:
                    changes.append(f"{at}: {kind} {detail} on {t['name']} ({rows(t)}).")
    return list(dict.fromkeys(errors)), list(dict.fromkeys(changes))


FORBIDDEN_SQL = re.compile(
    r"\b(insert|update|delete|merge|drop|alter|create|truncate|exec|execute|grant|revoke|deny|into|waitfor|"
    r"openrowset|opendatasource|openquery|bulk|backup|restore|shutdown|kill|dbcc|use)\b|\bxp_|\bsp_", re.I)


def guard_select(sql: str) -> str | None:
    """None when sql is ONE read-only SELECT/WITH statement, else the reason it is refused."""
    body = code_only(sql).strip().lstrip(";").strip()
    if not re.match(r"(?is)^(select|with)\b", body):
        return ("only a single SELECT (or WITH ... SELECT) statement is accepted. To check a change script, preview "
                "'select count(*)' with the same FROM/JOIN/WHERE, and run the script itself through validate_sql")
    if ";" in body.rstrip().rstrip(";"):
        return "only one statement is accepted (found ';' inside)"
    if re.search(r"(?im)^\s*go\s*$", body):
        return "GO batch separators are not accepted"
    m = FORBIDDEN_SQL.search(body)
    if m:
        return f"'{m.group(0)}' is not allowed: preview_query is read-only"
    return None


def preview(ctx: Ctx, sql: str, max_rows: Optional[int]) -> PreviewOut:
    scope = "live database, read-only SELECT, first rows only"
    lim = ctx.cfg.db
    n = max(1, min(max_rows or lim.preview_default_rows, lim.preview_max_rows))
    why = guard_select(sql)
    if not why and ctx.mentions_masked(sql):
        why = "live data for this object is not available in this index snapshot"
    if why:
        return PreviewOut(status="error", layer="live", searched_scope=scope, caveats=[why])
    import time
    import pyodbc
    from ..prep.catalog import connect
    t0 = time.time()
    try:
        con = connect(ctx.cfg, timeout=lim.preview_timeout_s)
    except Exception as e:
        return PreviewOut(status="unavailable", layer="live", searched_scope=scope,
                          caveats=[f"database not reachable: {str(e)[:200]}"])
    cols, rows, more = [], [], False
    try:
        cur = con.cursor()
        cur.execute(f"SET ROWCOUNT {n + 1}")          # bounds the work, not only the transfer
        cur.execute(sql)
        while cur.description is None and cur.nextset():
            pass
        if cur.description is None:
            return PreviewOut(status="no_results", layer="live", searched_scope=scope,
                              caveats=["the statement returned no result set"])
        cols = [f"{d[0]} {getattr(d[1], '__name__', d[1])}" for d in cur.description]
        fetched = cur.fetchmany(n + 1)
        more = len(fetched) > n
        rows = [[cell(v) for v in r] for r in fetched[:n]]
    except pyodbc.Error as e:
        msg = odbc_text(e)
        if is_timeout(msg, e):
            msg = f"query exceeded {lim.preview_timeout_s}s: narrow it (filters, top, aggregates)"
            return PreviewOut(status="unavailable", layer="live", searched_scope=scope, caveats=[msg[:400]])
        # the database answered: the query itself is wrong (syntax, unknown column, conversion ...)
        return PreviewOut(status="error", layer="live", searched_scope=scope,
                          caveats=[f"the query failed: {msg[:400]}", "Fix the query and run it again."])
    finally:
        try:
            con.cursor().execute("SET ROWCOUNT 0")
        except Exception:
            pass
        con.close()
    caveats = [f"Live data, first {n} row(s) only" + (" -- more rows exist" if more else "") +
               ". This is for checking a query you are writing (counts, joins, filters), not for browsing data; "
               "prefer aggregates (count/group by) over raw rows."]
    return PreviewOut(status="ok" if rows else "no_results", layer="live", searched_scope=scope, columns=cols,
                      rows=rows, more_rows=more, elapsed_ms=int((time.time() - t0) * 1000), caveats=caveats)


def register(mcp, ctx: Ctx) -> None:
    @mcp.tool(name="preview_query", annotations=RO, description=(
        "Check a query you are writing against LIVE data: runs ONE read-only SELECT/WITH and returns its "
        f"columns and first rows (default {ctx.cfg.db.preview_default_rows}, max {ctx.cfg.db.preview_max_rows}; "
        f"{ctx.cfg.db.preview_timeout_s} s timeout) plus whether more rows exist. Use it to "
        "confirm counts, joins and filters before handing SQL over (e.g. count by Status and OrderKind). "
        "Not a data browser: prefer aggregates. Anything that is not a single SELECT is refused; to check "
        "a DELETE/UPDATE you are writing, preview 'select count(*)' with its FROM/JOIN/WHERE."))
    def preview_query(sql: str, max_rows: Optional[int] = None) -> PreviewOut:
        return preview(ctx, sql, max_rows)

    @mcp.tool(name="validate_sql", annotations=RO, description=(
        "Check proposed SQL before handing it over: compiles and binds it against the LIVE database without "
        "executing anything (SET NOEXEC ON), then checks every table/column it names against the catalog "
        "(procedures defer unknown tables at compile time). Accepts a whole SSMS script with GO separators "
        "(CREATE/ALTER VIEW/PROCEDURE/FUNCTION, SELECT, and change scripts: INSERT/UPDATE/DELETE/MERGE, "
        "DROP, TRUNCATE, ALTER TABLE, CREATE INDEX). For change scripts it lists under 'changes' what each "
        "statement touches (rows, foreign keys, dependents) and errors for missing targets, DROP of objects "
        "that do not exist, and TRUNCATE of referenced tables. Also lints it against the team SQL style "
        "(warnings under 'style'). Returns errors and, for views/selects, the "
        "resulting column list. draft_id (from draft_change) checks that draft as the first batch, followed by "
        "sql if given; style notes then cover only the lines the draft changed and your sql. handover = the "
        "script exactly as you wrote it in your answer (with draft_id): the result then says whether it is "
        "identical to the draft or names every line that differs -- run it before you hand a script over."))
    def validate_sql(sql: str = "", draft_id: Optional[str] = None, handover: Optional[str] = None) -> ValidateOut:
        return validate(ctx, sql, draft_id, handover)
