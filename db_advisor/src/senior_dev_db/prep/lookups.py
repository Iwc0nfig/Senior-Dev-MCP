"""Phase lookups: rows of small reference tables.

A magic id like ``TicketTaskTable_Id = 5`` only means something through the rows
of TicketTaskTable.  Every small table with a single-column integer/char key and a
name-like column is snapshotted (key -> label), so the id can be decoded.
"""

from __future__ import annotations

import logging
import sqlite3

import pyodbc

from ..config import Config
from .. import db as dbm
from .catalog import connect

log = logging.getLogger("prep.lookups")

LABEL_COLS = ["Name", "Description", "Descr", "Caption", "Title", "Text", "Label", "Code"]

def run(cfg: Config, con: sqlite3.Connection) -> dict:
    dbm.clear_tables(con, ["lookup_rows"])
    max_rows = cfg.profile.lookup_max_rows
    cands = []
    for oid, sch, name, rows in con.execute(
            "SELECT obj_id, schema_name, name, row_count FROM objects WHERE kind='table' AND row_count BETWEEN 1 AND ?",
            (max_rows,)):
        pk = con.execute("SELECT key_columns FROM indexes WHERE obj_id=? AND is_primary_key=1", (oid,)).fetchone()
        if not pk or "," in (pk[0] or ""):
            continue
        cols = {r[0].lower(): (r[0], r[1]) for r in con.execute(
            "SELECT name, type_name FROM columns WHERE obj_id=?", (oid,))}
        key = cols.get(pk[0].lower())
        if not key or key[1] not in ("tinyint", "smallint", "int", "bigint", "char", "nchar", "varchar", "nvarchar"):
            continue
        label = next((cols[c.lower()][0] for c in LABEL_COLS if c.lower() in cols and c.lower() != pk[0].lower()), None)
        if not label:
            continue
        extra = next((cols[c.lower()][0] for c in ("Code", "Codeid", "Type", "Active")
                      if c.lower() in cols and cols[c.lower()][0] not in (label, key[0])), None)
        cands.append((oid, sch, name, key[0], label, extra))

    sq = connect(cfg)
    cur = sq.cursor()
    n = 0
    for oid, sch, name, kcol, lcol, ecol in cands:
        sel = f"cast([{kcol}] as nvarchar(100)), cast([{lcol}] as nvarchar(300))"
        sel += f", cast([{ecol}] as nvarchar(100))" if ecol else ", null"
        try:
            rows = cur.execute(f"select top {max_rows} {sel} from [{sch}].[{name}] with (nolock) order by [{kcol}]").fetchall()
        except pyodbc.Error as e:
            log.warning("%s: %s", name, str(e)[:120])
            continue
        con.executemany("INSERT INTO lookup_rows VALUES (?,?,?,?,?,?)",
                        [(oid, kcol, r[0], lcol, r[1], (f"{ecol}={r[2]}" if ecol and r[2] is not None else None))
                         for r in rows])
        n += 1
    con.commit()
    sq.close()
    return {"lookup_tables": n, "rows": con.execute("SELECT count(*) FROM lookup_rows").fetchone()[0]}
