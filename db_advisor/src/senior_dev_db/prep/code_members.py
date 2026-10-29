"""code_members: name the C# member at every path:line this index cites.

Every C# fact this server gives (an entity column read, a SQL string, a report or grid registration, an
enum) is a path:line from the syntax extraction.  The Code Advisor's index knows the exact span of every
declaration (compiler index `rsymbols`, else structural graph `symbols`), so for each cited line this phase
stores the innermost declaration containing it: tools then cite `path:line (Class.Member)`, which the agent
can cite (rule 1) and open with read_file_range(at='path:line').

The Code Advisor index is found through [code_advisor] root (its config.toml [paths] index_dir).  Without
it the phase is skipped and citations stay path:line.  The table is created here, not in db.ALL_SCHEMAS,
so adding it does not change the schema fingerprint (which would drop the whole index).
"""

from __future__ import annotations

import logging
import sqlite3
import tomllib
from collections import defaultdict
from pathlib import Path

from .. import db as dbm

log = logging.getLogger("prep.code_members")

SCHEMA = """
CREATE TABLE IF NOT EXISTS code_members (
  rel_path   TEXT NOT NULL,
  line       INTEGER NOT NULL,
  fqn        TEXT NOT NULL,
  key        TEXT,                 -- the Code Advisor's compiler-index key (NULL for graph-only members)
  kind       TEXT NOT NULL,
  start_line INTEGER NOT NULL,
  end_line   INTEGER NOT NULL,
  layer      TEXT NOT NULL,        -- roslyn-index | graph
  PRIMARY KEY (rel_path, line)
);
"""

# every table where this index records a C# line (as rel_path, line)
CITED = ["code_enums", "code_enum_items", "code_entities", "code_entity_columns", "code_sql", "ui_views",
         "ui_view_columns", "ui_reports", "ui_report_fields", "ui_report_filters", "value_maps", "value_map_items",
         "url_rules"]
MEMBER_KINDS = ("method", "constructor", "property", "indexer", "event", "field", "class", "struct",
                "record", "interface", "enum", "delegate")


def code_index_path(cfg) -> Path | None:
    """The Code Advisor's graph.db, from [code_advisor] root/config.toml [paths] index_dir."""
    root = cfg.code_advisor.root
    if not root or not (root / "config.toml").is_file():
        return None
    try:
        raw = tomllib.loads((root / "config.toml").read_text(encoding="utf-8"))
        d = Path(raw.get("paths", {}).get("index_dir", ".index"))
    except Exception:
        return None
    p = (d if d.is_absolute() else root / d) / "graph.db"
    return p if p.is_file() else None


def _innermost(spans: list[tuple], line: int):
    best = None
    for s in spans:
        if s[0] <= line <= s[1] and (best is None or s[1] - s[0] < best[1] - best[0]):
            best = s
    return best


def run(cfg, con: sqlite3.Connection) -> dict:
    con.executescript(SCHEMA)
    con.execute("DELETE FROM code_members")
    src_path = code_index_path(cfg)
    if src_path is None:
        dbm.set_meta(con, "code_members_from", "")
        con.commit()
        log.warning("no Code Advisor index found through [code_advisor] root: C# citations stay path:line")
        return {"skipped": "no Code Advisor index ([code_advisor] root)"}
    cites: dict[str, set[int]] = defaultdict(set)
    for table in CITED:
        try:
            for rel, line in con.execute(f"SELECT DISTINCT rel_path, line FROM {table} WHERE rel_path IS NOT NULL "
                                         "AND line IS NOT NULL"):
                cites[rel].add(int(line))
        except sqlite3.OperationalError:
            continue
    src = sqlite3.connect(f"file:{src_path.as_posix()}?mode=ro", uri=True)
    try:
        has_r = bool(src.execute("SELECT 1 FROM sqlite_master WHERE name='rsymbols'").fetchone())
        meta = dict(src.execute("SELECT key, value FROM index_meta").fetchall())
        rows = []
        kinds = ",".join("?" * len(MEMBER_KINDS))
        for rel, lines in cites.items():
            spans = []
            if has_r:
                spans = [(s, e, fqn, key, kind, "roslyn-index") for s, e, fqn, key, kind in src.execute(
                    f"SELECT coalesce(start_line, line), end_line, fqn, key, kind FROM rsymbols WHERE path = ? "
                    f"AND is_generated = 0 AND kind IN ({kinds})", (rel, *MEMBER_KINDS))]
            if not spans:
                spans = [(s, e, fqn or name, None, kind, "graph") for s, e, fqn, name, kind in src.execute(
                    "SELECT s.line_start, s.line_end, s.fqn, s.name, s.kind FROM symbols s JOIN files f "
                    "ON f.file_id = s.file_id WHERE f.rel_path = ? AND s.is_synthetic = 0 "
                    "AND s.kind <> 'namespace'", (rel,))]
            for line in lines:
                b = _innermost(spans, line)
                if b:
                    rows.append((rel, line, b[2], b[3], b[4], b[0], b[1], b[5]))
        con.executemany("INSERT OR REPLACE INTO code_members VALUES (?,?,?,?,?,?,?,?)", rows)
        built = meta.get("built_at", "")
        dbm.set_meta(con, "code_members_from", f"{src_path.as_posix()} (graph built {built}, compiler index "
                                               f"built {meta.get('build_started_at', '')})")
        con.commit()
    finally:
        src.close()
    total = sum(len(v) for v in cites.values())
    return {"cited_lines": total, "named": len(rows), "files": len(cites),
            "share_named": round(len(rows) / total, 3) if total else None}
