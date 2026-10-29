"""Phase curated_load: load citation-verified facts from knowledge/curated/*.json.

The files are produced by Haiku subagents from work packets (prep/packets.py) and
verified by prep/verify.py; only facts whose every citation was verified are kept.
"""

from __future__ import annotations

import json
import sqlite3

from ..config import Config
from .. import db as dbm


def run(cfg: Config, con: sqlite3.Connection) -> dict:
    dbm.clear_tables(con, ["curated"])
    n = files = 0
    d = cfg.paths.curated_dir
    for p in sorted(d.glob("*.verified.json")) if d.is_dir() else []:
        files += 1
        data = json.loads(p.read_text(encoding="utf-8"))
        for f in data.get("facts", []):
            if not f.get("citations"):
                continue
            con.execute("INSERT INTO curated(subject,fact_type,value,text,citations,packet,model) VALUES (?,?,?,?,?,?,?)",
                        (f["subject"], f["fact_type"], None if f.get("value") in (None, "") else str(f["value"]),
                         f["text"], json.dumps(f["citations"]), data.get("packet"), data.get("model")))
            n += 1
    con.commit()
    return {"files": files, "facts": n}
