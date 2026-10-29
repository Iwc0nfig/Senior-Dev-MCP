"""Build masked evaluation snapshots: ``python -m senior_dev_db.prep.eval_mask``.

The live database and the C# workspace already contain every example's "after".
For each example in tests/eval/mask.json this copies the finished index to
.index/eval/ex<N>.db and:
  * rolls touched modules back to the example's _OLD.sql text (re-parsed), or deletes
    objects the request asks to create (with their report registrations);
  * resets the masked views' column list to what the OLD text outputs;
  * removes grid/report column registrations and curated facts for identifiers the
    "after" adds;
  * re-runs link, style and search on the snapshot (so mined style/idioms/search see
    only the masked state).
Nothing in a snapshot tells the agent it is masked.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys

from ..config import load_config
from .. import db as dbm
from . import link, search, style
from .sqlparse import TABLES as PARSE_TABLES, catalog_columns, load_records, parse_modules


def read_sql(path) -> str:
    b = path.read_bytes()
    if b[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = b.decode("utf-16")
    else:
        for enc in ("utf-8-sig", "cp1253", "latin-1"):
            try:
                text = b.decode(enc)
                break
            except UnicodeDecodeError:
                continue
    return text.replace("\r\n", "\n").replace("\r", "\n")


def module_text(script: str) -> str:
    """The CREATE statement as sys.sql_modules would store it, from an SSMS ALTER script."""
    m = re.search(r"(?im)^\s*(alter|create)\s+(view|procedure|proc|function|trigger)\b", script)
    if not m:
        raise ValueError("no CREATE/ALTER statement in script")
    body = script[m.start():]
    body = re.split(r"(?im)^\s*go\s*$", body)[0].rstrip() + "\n"
    return re.sub(r"(?i)^\s*alter\b", "CREATE", body, count=1)


def mask_one(cfg, ex: str, spec: dict, src, dst) -> dict:
    shutil.copyfile(src, dst)
    con = dbm.connect(dst, timeouts=cfg.timeouts)
    root = cfg.paths.examples_dir
    report = {"rolled_back": [], "deleted": [], "hidden_rows": 0}
    reparse = []
    for name, rel in spec.get("rollback", {}).items():
        o = con.execute("SELECT obj_id, kind FROM objects WHERE name=? COLLATE NOCASE", (name,)).fetchone()
        text = module_text(read_sql(root / rel))
        date = re.match(r"(\d{4})(\d{2})(\d{2})", rel.split("/")[-1])
        modified = f"{date.group(1)}-{date.group(2)}-{date.group(3)}T00:00:00" if date else None
        con.execute("UPDATE modules SET definition=? WHERE obj_id=?", (text, o[0]))
        con.execute("UPDATE objects SET line_count=?, modified=?, masked=1 WHERE obj_id=?",
                    (text.count("\n") + 1, modified, o[0]))
        reparse.append((o[0], text, o[1]))
        report["rolled_back"].append(name)
    for name in spec.get("delete", []):
        o = con.execute("SELECT obj_id FROM objects WHERE name=? COLLATE NOCASE", (name,)).fetchone()
        if o:
            for t in ("objects", "modules", "columns", "params", "result_sets") + tuple(PARSE_TABLES):
                con.execute(f"DELETE FROM {t} WHERE obj_id=?", (o[0],))
            con.execute("DELETE FROM deps WHERE referencing_id=? OR referenced_id=?", (o[0], o[0]))
        for (rname, rel) in con.execute("SELECT report_name, rel_path FROM ui_reports WHERE object_name=? COLLATE NOCASE",
                                        (name,)).fetchall():
            for t in ("ui_report_filters", "ui_report_fields"):
                con.execute(f"DELETE FROM {t} WHERE report_name=? AND rel_path=?", (rname, rel))
        con.execute("DELETE FROM ui_reports WHERE object_name=? COLLATE NOCASE", (name,))
        report["deleted"].append(name)
    if reparse:
        for t in PARSE_TABLES:
            con.executemany(f"DELETE FROM {t} WHERE obj_id=?", [(r[0],) for r in reparse])
        recs = parse_modules(cfg, [(r[0], r[1]) for r in reparse], catalog_columns(con), tag=f"eval{ex}")
        load_records(con, recs)
        # masked views expose what their OLD text outputs (keep types where the column already existed)
        for oid, _text, kind in reparse:
            if kind != "view":
                continue
            outs = [r[0] for r in con.execute("SELECT out_name FROM sql_outputs WHERE obj_id=? AND owner='result' "
                                              "AND stmt=(SELECT min(stmt) FROM sql_outputs WHERE obj_id=? AND owner='result') "
                                              "ORDER BY ordinal", (oid, oid)) if r[0]]
            old = {r["name"].lower(): r for r in con.execute("SELECT * FROM columns WHERE obj_id=?", (oid,))}
            con.execute("DELETE FROM columns WHERE obj_id=?", (oid,))
            for i, nm in enumerate(outs, 1):
                prev = old.get(nm.lower())
                con.execute("INSERT INTO columns(obj_id,column_id,name,type_name,max_length,precision,scale,is_nullable,"
                            "is_identity,is_computed,description) VALUES (?,?,?,?,?,?,?,?,0,0,?)",
                            (oid, i, nm, prev["type_name"] if prev else "unknown", prev["max_length"] if prev else None,
                             prev["precision"] if prev else None, prev["scale"] if prev else None,
                             prev["is_nullable"] if prev else 1, prev["description"] if prev else None))
            con.execute(f"DELETE FROM result_sets WHERE obj_id=? AND lower(name) NOT IN ({','.join('?' * len(outs))})",
                        (oid, *[o.lower() for o in outs]))
    for obj, fields in spec.get("hide_view_columns", {}).items():
        for f in fields:
            cur = con.execute("DELETE FROM ui_view_columns WHERE field_name=? COLLATE NOCASE AND view_name IN "
                              "(SELECT view_name FROM ui_views WHERE object_name=? COLLATE NOCASE)", (f, obj))
            report["hidden_rows"] += cur.rowcount
    for obj, fields in spec.get("hide_report_fields", {}).items():
        for f in fields:
            cur = con.execute("DELETE FROM ui_report_fields WHERE data_field=? COLLATE NOCASE AND report_name IN "
                              "(SELECT report_name FROM ui_reports WHERE object_name=? COLLATE NOCASE)", (f, obj))
            report["hidden_rows"] += cur.rowcount
    for term in spec.get("hide_terms", []):
        like = f"%{term}%"
        report["hidden_rows"] += con.execute("DELETE FROM curated WHERE text LIKE ? OR subject LIKE ? OR citations LIKE ?",
                                             (like, like, like)).rowcount
        report["hidden_rows"] += con.execute("DELETE FROM code_sql WHERE sql_text LIKE ?", (like,)).rowcount
    # live tools (preview_query, live definitions, staleness checks) must not reveal these objects'
    # current state; this flag is never shown to agents
    dbm.set_meta(con, "eval_masked_names", json.dumps(report["rolled_back"] + report["deleted"]))
    con.commit()
    for ph in (link, style, search):
        ph.run(cfg, con)
    leaks = []
    for term in spec.get("hide_terms", []):
        like = f"%{term}%"
        for t, c in (("modules", "definition"), ("style_rules", "evidence"), ("search_items", "text"),
                     ("ui_view_columns", "field_name"), ("ui_report_fields", "data_field"), ("curated", "text")):
            n = con.execute(f"SELECT count(*) FROM {t} WHERE {c} LIKE ?", (like,)).fetchone()[0]
            if n:
                leaks.append(f"{term} still in {t}.{c} ({n})")
    report["leaks"] = leaks
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config")
    ap.add_argument("--only", default=None)
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    spec = json.loads(cfg.paths.eval_mask.read_text(encoding="utf-8"))
    out = cfg.paths.eval_dir
    out.mkdir(parents=True, exist_ok=True)
    for ex, s in spec.items():
        if ex.startswith("_") or (args.only and ex not in args.only.split(",")):
            continue
        rep = mask_one(cfg, ex, s, cfg.paths.db_path, out / f"ex{ex}.db")
        print(f"ex{ex}: {json.dumps(rep)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
