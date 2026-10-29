"""Work packets for the Haiku curation step (gaps only).

``python -m senior_dev_db.prep.packets [--size 25]`` writes
.index/stage/packets/<kind>-NN.json.  Each packet lists columns the deterministic
pipeline could not explain, with all the evidence it does have (observed values,
code reads, SQL usages, candidate enums), so the model only has to read and cite.

kinds:
  values   enum-like columns (few distinct values) used in SQL/code with no exact/strong value list
  meaning  heavily referenced columns with no MS_Description and no value list

Each packet carries an input hash; a packet whose hash already has a verified output in
knowledge/curated is skipped, so a refresh only asks about what changed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys

from ..config import load_config
from .. import db as dbm

SKIP_COLS = {"id", "revision", "lastupdateutc", "uid", "codeid", "searchindex", "searchstatus"}


def _evidence(con: sqlite3.Connection, tbl: str, col: str) -> dict:
    ev: dict = {}
    reads = con.execute("""SELECT rel_path, line, snippet FROM code_entity_columns WHERE table_name=? COLLATE NOCASE
                           AND column=? COLLATE NOCASE ORDER BY side LIMIT 4""", (tbl, col)).fetchall()
    if not reads:
        reads = con.execute("""SELECT rel_path, line, snippet FROM code_entity_columns WHERE column=? COLLATE NOCASE
                               LIMIT 3""", (col,)).fetchall()
    ev["code"] = [f"{r[0]}:{r[1]}  {r[2] or ''}"[:220] for r in reads]
    lits = con.execute("""SELECT o.name, l.line, l.op, l.value FROM sql_literals l JOIN objects o ON o.obj_id=l.obj_id
                          WHERE l.tbl=? COLLATE NOCASE AND l.col=? COLLATE NOCASE ORDER BY o.modified DESC LIMIT 8""",
                       (tbl, col)).fetchall()
    ev["sql_literals"] = [f"{r[0]}:{r[1]}  {col} {r[2]} {r[3]}" for r in lits]
    refs = con.execute("""SELECT o.name, min(r.line) FROM sql_colrefs r JOIN objects o ON o.obj_id=r.obj_id
                          WHERE r.tbl=? COLLATE NOCASE AND r.col=? COLLATE NOCASE GROUP BY o.name
                          ORDER BY o.modified DESC LIMIT 4""", (tbl, col)).fetchall()
    ev["sql_refs"] = [f"{r[0]}:{r[1]}" for r in refs]
    cs = con.execute("""SELECT s.rel_path, s.line FROM code_sql_refs x JOIN code_sql s ON s.code_sql_id=x.code_sql_id
                        WHERE x.tbl=? COLLATE NOCASE AND x.col=? COLLATE NOCASE LIMIT 3""", (tbl, col)).fetchall()
    ev["code_sql"] = [f"{r[0]}:{r[1]}" for r in cs]
    cands = con.execute("""SELECT vm.name, vm.source, vm.rel_path, vm.line, vm.map_id FROM column_value_maps m
                           JOIN value_maps vm ON vm.map_id=m.map_id WHERE m.tbl=? COLLATE NOCASE AND m.col=? COLLATE NOCASE
                           LIMIT 3""", (tbl, col)).fetchall()
    ev["candidate_value_lists"] = [
        f"{r[1]} {r[0]} ({r[2]}:{r[3]}): " + ", ".join(f"{v}={l}" for v, l in con.execute(
            "SELECT value, label FROM value_map_items WHERE map_id=? LIMIT 25", (r[4],)))
        for r in cands]
    return {k: v for k, v in ev.items() if v}


def build(con: sqlite3.Connection) -> dict[str, list[dict]]:
    refcount = {}
    for t, c, n in con.execute("SELECT lower(tbl), lower(col), count(*) FROM sql_colrefs GROUP BY 1, 2"):
        refcount[(t, c)] = n
    for t, c, n in con.execute("SELECT lower(table_name), lower(column), count(*) FROM code_entity_columns "
                               "WHERE table_name IS NOT NULL GROUP BY 1, 2"):
        refcount[(t, c)] = refcount.get((t, c), 0) + n
    decoded = {(r[0].lower(), r[1].lower()) for r in con.execute(
        "SELECT tbl, col FROM column_value_maps WHERE confidence IN ('exact','strong')")}

    values, meaning = [], []
    for tbl, col, typ, desc, dcount, top in con.execute("""
            SELECT o.name, c.name, c.type_name, c.description, p.distinct_count, p.top_values
            FROM columns c JOIN objects o ON o.obj_id=c.obj_id
            LEFT JOIN profiles p ON p.obj_id=c.obj_id AND p.column_id=c.column_id
            WHERE o.kind='table'"""):
        key = (tbl.lower(), col.lower())
        refs = refcount.get(key, 0)
        if col.lower() in SKIP_COLS or col.lower().endswith("_id") or col.lower().endswith("id"):
            continue
        enum_like = (typ in ("tinyint", "smallint", "int", "char", "nchar") and dcount is not None
                     and 2 <= dcount <= 25)
        if enum_like and key not in decoded and refs >= 3:
            item = {"column": f"{tbl}.{col}", "type": typ, "description": desc,
                    "observed_values": json.loads(top) if top else None, "references": refs}
            item.update(_evidence(con, tbl, col))
            values.append(item)
        elif not desc and key not in decoded and refs >= 12 and typ not in ("bit",):
            item = {"column": f"{tbl}.{col}", "type": typ, "references": refs,
                    "observed": (json.loads(top)[:12] if top else None)}
            item.update(_evidence(con, tbl, col))
            meaning.append(item)
    values.sort(key=lambda x: -x["references"])
    meaning.sort(key=lambda x: -x["references"])
    return {"values": values, "meaning": meaning[:400]}


INSTRUCTIONS = {
    "values": (
        "For each column, find what each stored value means, from the C# code and SQL. Use the Code "
        "Advisor tools (search_text, read_file_range, find_symbol) or read the files under the workspace root. "
        "Start from the evidence given (code reads, SQL literals, candidate value lists). Report a label for a "
        "value ONLY if a line you read states it (enum member, constant, CASE ... WHEN value THEN 'label', "
        "comment, caption). Cite every fact with the exact path:line (workspace-relative, forward slashes) or "
        "SQL object:line you read. If a value cannot be explained from what you read, omit it."),
    "meaning": (
        "For each column, write one factual sentence on what it holds (units/currency/which entity, how it is "
        "set), based ONLY on lines you read in the C# code or SQL (Code Advisor tools: search_text, "
        "read_file_range; or the workspace files). Cite path:line or SQL object:line for every sentence. If the "
        "code does not make it clear, omit the column."),
}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config")
    ap.add_argument("--size", type=int, default=25)
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    con = dbm.connect(cfg.paths.db_path, read_only=True, timeouts=cfg.timeouts)
    out = cfg.paths.stage_dir / "packets"
    out.mkdir(parents=True, exist_ok=True)
    done = {p.name.split(".")[0] for p in cfg.paths.curated_dir.glob("*.verified.json")} if cfg.paths.curated_dir.is_dir() else set()
    n = 0
    for kind, items in build(con).items():
        for i in range(0, len(items), args.size):
            chunk = items[i:i + args.size]
            h = hashlib.blake2b(json.dumps(chunk, sort_keys=True).encode(), digest_size=6).hexdigest()
            name = f"{kind}-{i // args.size:02d}-{h}"
            if name in done:
                continue
            (out / f"{name}.json").write_text(json.dumps({
                "packet": name, "kind": kind, "instructions": INSTRUCTIONS[kind],
                "workspace_root": cfg.paths.workspaces.as_posix(),
                "output_schema": {"packet": name, "facts": [
                    {"subject": "Table.Column", "fact_type": "value_label" if kind == "values" else "meaning",
                     "value": "(value_label only) the stored value", "text": "label / sentence",
                     "citations": ["Legacy/...cs:123", "usp_x:45"]}]},
                "items": chunk}, ensure_ascii=False, indent=1), encoding="utf-8")
            n += 1
    print(f"{n} packets written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
