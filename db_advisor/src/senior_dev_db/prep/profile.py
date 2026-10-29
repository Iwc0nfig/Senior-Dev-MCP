"""Phase profile: what values columns actually hold.

For every short scalar column of every table: distinct count, nulls, min/max; for
low-cardinality columns the full value list with counts.  This is what exposes a
stale description or enum (e.g. PaymentLogTable.PayMethod holding 6 and 7 while the
MS_Description lists 1..5).  Read-only, WITH (NOLOCK), per-query timeout; a
timed-out table is retried on a TABLESAMPLE and marked sampled.
"""

from __future__ import annotations

import json
import logging
import sqlite3

import pyodbc

from ..config import Config
from .. import db as dbm
from .catalog import connect

log = logging.getLogger("prep.profile")

PROFILED_TYPES = {"tinyint", "smallint", "int", "bigint", "bit", "char", "nchar", "varchar", "nvarchar"}
CHUNK = 25


def _candidates(con: sqlite3.Connection):
    rows = con.execute("""
        SELECT o.obj_id, o.schema_name, o.name, o.row_count, c.column_id, c.name, c.type_name, c.max_length, c.is_identity
        FROM objects o JOIN columns c ON c.obj_id=o.obj_id
        WHERE o.kind='table' ORDER BY o.obj_id, c.column_id""").fetchall()
    tables: dict[int, dict] = {}
    for oid, sch, tname, nrows, cid, cname, typ, mlen, ident in rows:
        if typ not in PROFILED_TYPES or ident:
            continue
        if typ in ("char", "nchar", "varchar", "nvarchar") and (mlen == -1 or mlen > 200):
            continue
        t = tables.setdefault(oid, {"schema": sch, "name": tname, "rows": nrows or 0, "cols": []})
        t["cols"].append((cid, cname, typ))
    return tables


def _q(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def _stats(cur, t, cols, sample_pct: int | None):
    src = f"{_q(t['schema'])}.{_q(t['name'])}"
    if sample_pct:
        src += f" TABLESAMPLE ({sample_pct} PERCENT)"
    parts = ["count_big(*)"]
    for _, cname, typ in cols:
        c = _q(cname)
        parts.append(f"count_big(distinct {c})")
        parts.append(f"sum(case when {c} is null then 1 else 0 end)")
        if typ == "bit":
            parts.append(f"cast(min(cast({c} as tinyint)) as nvarchar(200))")
            parts.append(f"cast(max(cast({c} as tinyint)) as nvarchar(200))")
        else:
            parts.append(f"cast(min({c}) as nvarchar(200))")
            parts.append(f"cast(max({c}) as nvarchar(200))")
    return cur.execute(f"select {', '.join(parts)} from {src} with (nolock)").fetchone()


def run(cfg: Config, con: sqlite3.Connection) -> dict:
    dbm.clear_tables(con, ["profiles"])
    keep = cfg.profile.max_distinct_kept
    tables = _candidates(con)
    sq = connect(cfg, timeout=cfg.timeouts.profile_query_s)
    cur = sq.cursor()
    n_cols = n_low = n_sampled = n_err = 0
    for i, (oid, t) in enumerate(sorted(tables.items(), key=lambda kv: kv[1]["rows"])):
        if t["rows"] == 0:
            con.executemany("INSERT INTO profiles(obj_id,column_id,rows_scanned,distinct_count,null_count) VALUES (?,?,0,0,0)",
                            [(oid, cid) for cid, _, _ in t["cols"]])
            continue
        for k in range(0, len(t["cols"]), CHUNK):
            cols = t["cols"][k:k + CHUNK]
            sampled = 0
            try:
                r = _stats(cur, t, cols, None)
            except pyodbc.Error as e:
                log.warning("%s: full scan failed (%s); sampling", t["name"], str(e)[:120])
                try:
                    r = _stats(cur, t, cols, cfg.profile.sample_percent)
                    sampled = 1
                    n_sampled += 1
                except pyodbc.Error as e2:
                    con.executemany("INSERT INTO profiles(obj_id,column_id,error) VALUES (?,?,?)",
                                    [(oid, cid, str(e2)[:300]) for cid, _, _ in cols])
                    n_err += len(cols)
                    continue
            total = r[0]
            for j, (cid, cname, typ) in enumerate(cols):
                dcount, nulls, mn, mx = r[1 + 4 * j: 5 + 4 * j]
                top = None
                capped = 0
                if 0 < dcount <= keep:
                    expr = f"cast({_q(cname)} as tinyint)" if typ == "bit" else _q(cname)
                    try:
                        vals = cur.execute(
                            f"select top {keep + 1} cast({expr} as nvarchar(200)), count_big(*) "
                            f"from {_q(t['schema'])}.{_q(t['name'])} with (nolock) "
                            f"group by {expr} order by count_big(*) desc").fetchall()
                        top = json.dumps([[v[0], v[1]] for v in vals], ensure_ascii=False)
                        n_low += 1
                    except pyodbc.Error as e:
                        log.warning("%s.%s values: %s", t["name"], cname, str(e)[:120])
                elif dcount > keep:
                    capped = 1
                con.execute("INSERT INTO profiles VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (oid, cid, total, sampled, nulls, dcount, capped, mn, mx, top, None))
                n_cols += 1
        if i % 50 == 0:
            con.commit()
            log.info("profiled %d/%d tables (%s, %s rows)", i, len(tables), t["name"], t["rows"])
    con.commit()
    sq.close()
    return {"tables": len(tables), "columns": n_cols, "low_cardinality": n_low,
            "sampled_chunks": n_sampled, "errors": n_err}
