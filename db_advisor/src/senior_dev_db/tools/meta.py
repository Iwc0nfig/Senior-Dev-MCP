"""index_status: what is indexed, when, and what is not."""

from __future__ import annotations

import json

from ..models import StatusOut
from .. import db as dbm
from .common import KNOWN_LIMITATIONS, RO, Ctx

COUNTED = {
    "tables": "SELECT count(*) FROM objects WHERE kind='table'",
    "views": "SELECT count(*) FROM objects WHERE kind='view'",
    "procedures": "SELECT count(*) FROM objects WHERE kind='proc'",
    "functions": "SELECT count(*) FROM objects WHERE kind IN ('scalar_fn','inline_tvf','tvf')",
    "columns": "SELECT count(*) FROM columns",
    "declared_fks": "SELECT count(DISTINCT fk_id) FROM fks",
    "relationships": "SELECT count(*) FROM relationships",
    "value_maps": "SELECT count(*) FROM value_maps",
    "columns_with_value_map": "SELECT count(*) FROM (SELECT DISTINCT tbl, col FROM column_value_maps WHERE col NOT LIKE '@%')",
    "profiled_columns": "SELECT count(*) FROM profiles",
    "grid_screens": "SELECT count(DISTINCT view_name) FROM ui_views",
    "reports": "SELECT count(DISTINCT report_name) FROM ui_reports",
    "conflicts": "SELECT count(*) FROM conflicts",
    "curated_facts": "SELECT count(*) FROM curated",
    "modules_parse_errors": "SELECT count(*) FROM sql_parse_status WHERE ok=0",
}


def status(ctx: Ctx) -> StatusOut:
    m = dbm.all_meta(ctx.con)
    counts = {k: ctx.q1(q)[0] for k, q in COUNTED.items()}
    phases = {r[0]: r[1] for r in ctx.q("SELECT name, finished_at FROM phases")}
    lim = list(KNOWN_LIMITATIONS)
    lim += [f"{k.replace(':', ' registrations on ')}: {note}" for k, note in ctx.cfg.side_notes]
    src = m.get("code_members_from")
    lim.append(f"C# citations name their member (path:line (Class.Member)), taken from the Code Advisor index "
               f"{src}; if that index was rebuilt since, a line may have moved: read_file_range(at='path:line') "
               f"shows what is there now." if src else
               "C# citations are path:line only (no Code Advisor index was found at prepare time); "
               "read_file_range(at='path:line') on the Code Advisor names the member.")
    broken = ctx.q("SELECT o.name, r.error FROM result_sets r JOIN objects o ON o.obj_id=r.obj_id WHERE r.error IS NOT NULL")
    for name, err in broken:
        lim.append(f"View {name} does not compile in the database: {err}")
    lim.append(f"C# paths are relative to the workspace {m.get('workspaces', '?')}, the same tree the "
               f"Code Advisor indexes ({ctx.cfg.code_advisor.url}); open them there with read_file_range.")
    masked = json.loads(m["eval_masked_objects"]) if m.get("eval_masked_objects") else []
    changed = None
    built = (m.get("db_snapshot_at") or m.get("built_at") or "")[:19]
    if built and not ctx.eval_masked_names():
        try:
            import pyodbc
            t = ctx.cfg.timeouts.index_status_s
            con = pyodbc.connect(ctx.cfg.connection_string(timeout=t), autocommit=True, readonly=True)
            con.timeout = t
            rows = con.cursor().execute(
                "select name, convert(varchar(16), modify_date, 120) from sys.objects where is_ms_shipped=0 "
                "and type in ('U','V','P','FN','IF','TF','TR') and modify_date > ? order by modify_date desc",
                built.replace("T", " ")).fetchall()
            con.close()
            changed = (f"{len(rows)} object(s) changed since the build" +
                       (": " + ", ".join(f"{r[0]} ({r[1]})" for r in rows[:10]) if rows else "") +
                       (" -- re-run prepare" if rows else ""))
        except Exception as e:
            changed = f"live check unavailable ({type(e).__name__})"
    return StatusOut(status="ok", layer="catalog", searched_scope=ctx.scope("index metadata"),
                     built_at=m.get("built_at"), changed_since_build=changed, database=f"{m.get('db_name')} ({m.get('db_version')})",
                     counts=counts, phases=phases, known_limitations=lim, masked=masked,
                     sandbox=sandbox_status(ctx), caveats=ctx.masked_caveats())


def sandbox_status(ctx: Ctx) -> str:
    """One line on the test_sql clone: built when, from what, what failed, whether it is out of date."""
    from .sandbox import sandbox_info
    if not ctx.cfg.sandbox.enabled:
        return "not configured ([sandbox] enabled = false): test_sql is unavailable"
    info = sandbox_info(ctx)
    if "error" in info:
        return f"not reachable ({info['error'][:120]}): test_sql is unavailable"
    if not info.get("built_at"):
        return "not built: run scripts/sandbox.ps1"
    c = info.get("counts", {})
    s = (f"built {info['built_at']} from appdb as of {info.get('source_snapshot_at')}: {c.get('tables', '?')} tables, "
         f"{c.get('rows', '?')} rows, {c.get('modules', '?')} modules, {c.get('triggers', '?')} triggers")
    failed = info.get("failed_objects", [])
    if failed:
        s += f"; not in the clone: {', '.join(failed[:10])}"
    return s


def register(mcp, ctx: Ctx) -> None:
    @mcp.tool(name="index_status", annotations=RO, description=(
        "What the index covers, when it was built, counts, and known limitations. Call it when a result is "
        "surprising or empty, and quote known_limitations instead of guessing."))
    def index_status() -> StatusOut:
        return status(ctx)
