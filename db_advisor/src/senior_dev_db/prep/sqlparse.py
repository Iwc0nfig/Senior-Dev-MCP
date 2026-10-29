"""Phase sqlparse: ScriptDom over every module (sql-parser/SqlParser.exe).

Writes the module texts and a catalog column map to .index/stage, runs the parser,
and loads its JSONL into the sql_* tables.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import subprocess

from ..config import Config
from .. import db as dbm

log = logging.getLogger("prep.sqlparse")

TABLES = ["sql_parse_status", "sql_aliases", "sql_joins", "sql_outputs", "sql_colrefs",
          "sql_literals", "sql_calls"]


def parse_modules(cfg: Config, modules: list[tuple[int, str]], catalog_cols: dict[str, list[str]],
                  tag: str = "modules", stage=None, dynamic: bool = False) -> list[dict]:
    """Run SqlParser.exe over (obj_id, definition) pairs; return the parsed records.  dynamic=True also
    outlines dynamic SQL (statements numbered from 100000, outputs owned by 'dynamic:@var')."""
    stage = stage or cfg.paths.stage_dir
    stage.mkdir(parents=True, exist_ok=True)
    src = stage / f"{tag}.jsonl"
    cat = stage / "catalog_columns.json"
    out = stage / f"{tag}.parsed.jsonl"
    with src.open("w", encoding="utf-8") as f:
        for oid, d in modules:
            f.write(json.dumps({"obj_id": oid, "definition": d}, ensure_ascii=False) + "\n")
    cat.write_text(json.dumps(catalog_cols), encoding="utf-8")
    exe = cfg.paths.sql_parser
    if not exe.is_file():
        raise FileNotFoundError(f"{exe} missing: dotnet build sql-parser -c Release")
    p = subprocess.run([str(exe), str(src), str(cat), str(out)] + (["--dynamic"] if dynamic else []),
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"SqlParser failed: {p.stderr[-2000:]}")
    log.info("SqlParser: %s", p.stderr.strip())
    with out.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def catalog_columns(con: sqlite3.Connection) -> dict[str, list[str]]:
    cols: dict[str, list[str]] = {}
    for name, col in con.execute(
            "SELECT o.name, c.name FROM columns c JOIN objects o ON o.obj_id=c.obj_id"):
        cols.setdefault(name.lower(), []).append(col)
    return cols


def load_records(con: sqlite3.Connection, recs: list[dict]) -> None:
    for r in recs:
        oid = r["obj_id"]
        con.execute("INSERT OR REPLACE INTO sql_parse_status VALUES (?,?,?)",
                    (oid, int(r["Ok"]), json.dumps(r["errors"]) if r["errors"] else None))
        con.executemany("INSERT INTO sql_aliases VALUES (?,?,?,?,?,?)",
                        [(oid, *a) for a in r["aliases"]])
        con.executemany("INSERT INTO sql_joins VALUES (?,?,?,?,?,?,?,?,?)",
                        [(oid, *j) for j in r["joins"]])
        con.executemany("INSERT INTO sql_outputs VALUES (?,?,?,?,?,?,?,?)",
                        [(oid, o[0], o[1], o[2], o[3], o[4], json.dumps(o[5]), o[6])
                         for o in r["outputs"]])
        con.executemany("INSERT INTO sql_colrefs VALUES (?,?,?,?,?)",
                        [(oid, *c) for c in r["colrefs"]])
        con.executemany("INSERT INTO sql_literals VALUES (?,?,?,?,?,?)",
                        [(oid, *x) for x in r["literals"]])
        con.executemany("INSERT INTO sql_calls VALUES (?,?,?,?)",
                        [(oid, *x) for x in r["calls"]])


def run(cfg: Config, con: sqlite3.Connection) -> dict:
    dbm.clear_tables(con, TABLES)
    mods = [(r[0], r[1]) for r in con.execute("SELECT obj_id, definition FROM modules ORDER BY obj_id")]
    recs = parse_modules(cfg, mods, catalog_columns(con), dynamic=True)
    load_records(con, recs)
    con.commit()
    dyn = [d for r in recs for d in r.get("dynamic", [])]
    return {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES} | {
        "parse_errors": con.execute("SELECT count(*) FROM sql_parse_status WHERE ok=0").fetchone()[0],
        "dynamic_sql_vars": len(dyn), "dynamic_sql_outlined": sum(1 for d in dyn if d[3])}
