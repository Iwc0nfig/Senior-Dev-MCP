"""Verify curated facts before they enter the index.

``python -m senior_dev_db.prep.verify`` reads knowledge/curated/<packet>.json
(raw model output) and writes <packet>.verified.json with only the facts whose
EVERY citation checks out:
  * path:line   -- the file exists in the workspace and lines line-2..line+2 contain a
                   token of the fact (the value, the column name, or a word of the label);
  * object:line -- the module exists and that window of its definition contains one.
The subject column must exist in the catalog.  Nothing unverifiable is loaded.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from functools import lru_cache

from ..config import load_config
from .. import db as dbm

STOP = {"the", "a", "an", "of", "to", "in", "is", "and", "or", "for", "on", "by", "with", "as", "at", "it", "this",
        "that", "be", "are", "from", "value", "column", "table", "type"}


def tokens(fact: dict) -> set[str]:
    subj_col = fact["subject"].split(".")[-1]
    toks = {subj_col.lower()}
    if fact.get("value") not in (None, ""):
        toks.add(str(fact["value"]).lower().strip("'"))
    for w in re.findall(r"[A-Za-z][A-Za-z0-9_]{2,}", fact.get("text", "")):
        if w.lower() not in STOP:
            toks.add(w.lower())
    return toks


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    # module texts: the staging build when it exists (fresh machine), else the served index
    src_db = cfg.paths.building_path if cfg.paths.building_path.is_file() else cfg.paths.db_path
    con = dbm.connect(src_db, read_only=True, timeouts=cfg.timeouts)
    root = cfg.paths.workspaces

    @lru_cache(maxsize=4096)
    def file_lines(rel: str):
        p = root / rel
        if not p.is_file():
            return None
        return p.read_text(encoding="utf-8-sig", errors="replace").splitlines()

    @lru_cache(maxsize=4096)
    def module_lines(name: str):
        r = con.execute("SELECT m.definition FROM modules m JOIN objects o ON o.obj_id=m.obj_id WHERE o.name=? COLLATE NOCASE",
                        (name,)).fetchone()
        return r[0].split("\n") if r else None

    def check(cite: str, toks: set[str]) -> bool:
        m = re.match(r"^\s*(.+?):(\d+)(?:-\d+)?\s*$", cite)
        if not m:
            return False
        where, line = m.group(1).replace("\\", "/"), int(m.group(2))
        lines = file_lines(where) if "/" in where or where.endswith(".cs") else module_lines(where)
        if lines is None or not (1 <= line <= len(lines)):
            return False
        window = " ".join(lines[max(0, line - 3):line + 2]).lower()
        # short tokens (values like 2) must stand alone; longer ones may sit inside an
        # identifier (Draft in utDraft, Closed in rstClosed)
        hits = [t for t in toks if (len(t) >= 4 and t in window)
                or re.search(r"(?<![a-z0-9_])" + re.escape(t) + r"(?![a-z0-9_])", window)]
        return len(hits) >= 1

    total = kept = 0
    for p in sorted(cfg.paths.curated_dir.glob("*.json")):
        if p.name.endswith(".verified.json"):
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"{p.name}: invalid JSON ({e})")
            continue
        good = []
        for f in data.get("facts", []):
            total += 1
            if not all(k in f for k in ("subject", "fact_type", "text", "citations")) or not f["citations"]:
                continue
            t, _, c = f["subject"].rpartition(".")
            if not con.execute("SELECT 1 FROM columns cl JOIN objects o ON o.obj_id=cl.obj_id "
                               "WHERE o.name=? COLLATE NOCASE AND cl.name=? COLLATE NOCASE", (t, c)).fetchone():
                continue
            toks = tokens(f)
            if all(check(ci, toks) for ci in f["citations"]):
                good.append(f)
        kept += len(good)
        out = p.with_name(p.stem + ".verified.json")
        out.write_text(json.dumps({"packet": data.get("packet", p.stem), "model": data.get("model", "claude-haiku-5-5"),
                                   "facts": good}, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{p.name}: {len(good)}/{len(data.get('facts', []))} facts verified")
    print(f"total verified {kept}/{total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
