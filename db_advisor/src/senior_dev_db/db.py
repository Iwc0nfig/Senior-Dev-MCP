"""SQLite schema for the prepared index, connection handling, meta helpers.

One file, rebuilt from scratch by ``prepare``; served read-only.  Nothing here stores
an absolute path (code locations are workspace-relative) or any credential.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .config import TimeoutsCfg

USER_VERSION = 1

PRAGMAS = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;
"""

# --- phase: catalog (SQL Server sys.* views) ------------------------------------
SCHEMA_CATALOG = """
CREATE TABLE IF NOT EXISTS index_meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS phases (name TEXT PRIMARY KEY, finished_at TEXT, info TEXT);

-- kind: table | view | proc | scalar_fn | inline_tvf | tvf | trigger
CREATE TABLE IF NOT EXISTS objects (
  obj_id       INTEGER PRIMARY KEY,
  schema_name  TEXT NOT NULL,
  name         TEXT NOT NULL,
  kind         TEXT NOT NULL,
  type_code    TEXT NOT NULL,
  parent_id    INTEGER,
  created      TEXT,
  modified     TEXT,
  row_count    INTEGER,
  description  TEXT,
  line_count   INTEGER,
  masked       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_obj_name ON objects(name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS ix_obj_kind ON objects(kind);

CREATE TABLE IF NOT EXISTS modules (
  obj_id      INTEGER PRIMARY KEY,
  definition  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS columns (
  obj_id        INTEGER NOT NULL,
  column_id     INTEGER NOT NULL,
  name          TEXT NOT NULL,
  type_name     TEXT NOT NULL,
  max_length    INTEGER,
  precision     INTEGER,
  scale         INTEGER,
  is_nullable   INTEGER,
  is_identity   INTEGER,
  is_computed   INTEGER,
  computed_def  TEXT,
  default_def   TEXT,
  description   TEXT,
  PRIMARY KEY (obj_id, column_id)
);
CREATE INDEX IF NOT EXISTS ix_col_name ON columns(name COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS params (
  obj_id       INTEGER NOT NULL,
  param_id     INTEGER NOT NULL,
  name         TEXT NOT NULL,
  type_name    TEXT NOT NULL,
  max_length   INTEGER,
  precision    INTEGER,
  scale        INTEGER,
  is_output    INTEGER,
  has_default  INTEGER,
  description  TEXT,
  PRIMARY KEY (obj_id, param_id)
);

CREATE TABLE IF NOT EXISTS fks (
  fk_id          INTEGER NOT NULL,
  name           TEXT NOT NULL,
  parent_obj_id  INTEGER NOT NULL,
  parent_column  TEXT NOT NULL,
  ref_obj_id     INTEGER NOT NULL,
  ref_column     TEXT NOT NULL,
  ordinal        INTEGER NOT NULL,
  is_disabled    INTEGER NOT NULL DEFAULT 0,
  on_delete      TEXT,              -- NO_ACTION | CASCADE | SET_NULL | SET_DEFAULT
  on_update      TEXT
);
CREATE INDEX IF NOT EXISTS ix_fk_parent ON fks(parent_obj_id);
CREATE INDEX IF NOT EXISTS ix_fk_ref ON fks(ref_obj_id);

CREATE TABLE IF NOT EXISTS indexes (
  obj_id          INTEGER NOT NULL,
  index_id        INTEGER NOT NULL,
  name            TEXT,
  type_desc       TEXT,
  is_primary_key  INTEGER,
  is_unique       INTEGER,
  key_columns     TEXT,
  included_columns TEXT,
  filter          TEXT
);
CREATE INDEX IF NOT EXISTS ix_indexes_obj ON indexes(obj_id);

-- sys.sql_expression_dependencies (what the engine itself recorded)
CREATE TABLE IF NOT EXISTS deps (
  referencing_id     INTEGER NOT NULL,
  referenced_schema  TEXT,
  referenced_name    TEXT NOT NULL,
  referenced_id      INTEGER,
  is_ambiguous       INTEGER,
  is_caller_dependent INTEGER
);
CREATE INDEX IF NOT EXISTS ix_deps_from ON deps(referencing_id);
CREATE INDEX IF NOT EXISTS ix_deps_to ON deps(referenced_id);

-- sys.dm_exec_describe_first_result_set_for_object
CREATE TABLE IF NOT EXISTS result_sets (
  obj_id         INTEGER NOT NULL,
  ordinal        INTEGER NOT NULL,
  name           TEXT,
  type_name      TEXT,
  is_nullable    INTEGER,
  source_table   TEXT,
  source_column  TEXT,
  error          TEXT
);
CREATE INDEX IF NOT EXISTS ix_rs_obj ON result_sets(obj_id);
"""

# --- phase: profile (data values) ----------------------------------------------
SCHEMA_PROFILE = """
CREATE TABLE IF NOT EXISTS profiles (
  obj_id          INTEGER NOT NULL,
  column_id       INTEGER NOT NULL,
  rows_scanned    INTEGER,
  sampled         INTEGER NOT NULL DEFAULT 0,
  null_count      INTEGER,
  distinct_count  INTEGER,
  distinct_capped INTEGER NOT NULL DEFAULT 0,
  min_v           TEXT,
  max_v           TEXT,
  top_values      TEXT,      -- JSON [[value, count], ...] when low-cardinality
  error           TEXT,
  PRIMARY KEY (obj_id, column_id)
);
-- rows of small reference tables (key -> label), see prep/lookups.py
CREATE TABLE IF NOT EXISTS lookup_rows (
  obj_id     INTEGER NOT NULL,
  key_col    TEXT NOT NULL,
  key        TEXT NOT NULL,
  label_col  TEXT NOT NULL,
  label      TEXT,
  extra      TEXT
);
CREATE INDEX IF NOT EXISTS ix_lookup ON lookup_rows(obj_id);
"""

# --- phase: sqlparse (ScriptDom over every module) ------------------------------
SCHEMA_SQLPARSE = """
CREATE TABLE IF NOT EXISTS sql_parse_status (
  obj_id    INTEGER PRIMARY KEY,
  ok        INTEGER NOT NULL,
  errors    TEXT
);
-- alias -> table/view/function/cte/temp table, per statement scope
CREATE TABLE IF NOT EXISTS sql_aliases (
  obj_id       INTEGER NOT NULL,
  stmt         INTEGER NOT NULL,
  alias        TEXT NOT NULL,
  target       TEXT NOT NULL,       -- name as written (no schema)
  target_kind  TEXT NOT NULL,       -- object | cte | temp | derived | tvf
  line         INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_alias_obj ON sql_aliases(obj_id);
CREATE INDEX IF NOT EXISTS ix_alias_target ON sql_aliases(target COLLATE NOCASE);

-- one row per equality in a join/where predicate linking two column refs
CREATE TABLE IF NOT EXISTS sql_joins (
  obj_id      INTEGER NOT NULL,
  stmt        INTEGER NOT NULL,
  join_type   TEXT,
  left_table  TEXT, left_column  TEXT,
  right_table TEXT, right_column TEXT,
  predicate   TEXT NOT NULL,          -- verbatim ON clause text (bounded)
  line        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_joins_l ON sql_joins(left_table COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS ix_joins_r ON sql_joins(right_table COLLATE NOCASE);

-- output columns of the top-level select(s), with lineage to base columns
CREATE TABLE IF NOT EXISTS sql_outputs (
  obj_id      INTEGER NOT NULL,
  stmt        INTEGER NOT NULL,
  owner       TEXT NOT NULL,          -- result | cte:X | derived:X | temp:#X | insert:T | into:T
  ordinal     INTEGER NOT NULL,
  out_name    TEXT,
  expression  TEXT NOT NULL,
  sources     TEXT NOT NULL,          -- JSON [[table, column], ...] resolved through aliases
  line        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_outputs_obj ON sql_outputs(obj_id);

-- every column reference, alias-resolved (for find_column_usage)
CREATE TABLE IF NOT EXISTS sql_colrefs (
  obj_id    INTEGER NOT NULL,
  tbl       TEXT NOT NULL,
  col       TEXT NOT NULL,
  usage     TEXT NOT NULL,            -- select | where | join | group | order | set | insert | other
  line      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_colrefs ON sql_colrefs(tbl COLLATE NOCASE, col COLLATE NOCASE);

-- comparisons of a column with a literal (magic values in use)
CREATE TABLE IF NOT EXISTS sql_literals (
  obj_id    INTEGER NOT NULL,
  tbl       TEXT,
  col       TEXT NOT NULL,
  op        TEXT NOT NULL,
  value     TEXT NOT NULL,
  line      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_lit ON sql_literals(tbl COLLATE NOCASE, col COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS sql_calls (
  obj_id   INTEGER NOT NULL,
  callee   TEXT NOT NULL,
  kind     TEXT NOT NULL,             -- exec | function
  line     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_calls_callee ON sql_calls(callee COLLATE NOCASE);
"""

# --- phase: code (Roslyn extraction over the C# workspace) ----------------------
SCHEMA_CODE = """
CREATE TABLE IF NOT EXISTS code_enums (
  enum_id    INTEGER PRIMARY KEY,
  name       TEXT NOT NULL,           -- simple type name (e.g. PayMethod)
  fqn        TEXT NOT NULL,
  kind       TEXT NOT NULL,           -- enum | value_enum | const_group | memtype
  side       TEXT NOT NULL,
  rel_path   TEXT NOT NULL,
  line       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_enum_name ON code_enums(name COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS code_enum_items (
  enum_id    INTEGER NOT NULL,
  value      TEXT NOT NULL,
  member     TEXT,
  label      TEXT,
  rel_path   TEXT NOT NULL,
  line       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_enum_items ON code_enum_items(enum_id);

-- C# entity class <-> table
CREATE TABLE IF NOT EXISTS code_entities (
  class_fqn  TEXT NOT NULL,
  table_name TEXT NOT NULL,
  side       TEXT NOT NULL,
  rel_path   TEXT NOT NULL,
  line       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_entity_table ON code_entities(table_name COLLATE NOCASE);
-- column reads/writes in entity classes, with the type it is converted to
CREATE TABLE IF NOT EXISTS code_entity_columns (
  class_fqn  TEXT NOT NULL,
  table_name TEXT,
  column     TEXT NOT NULL,
  property   TEXT,
  value_type TEXT,                    -- e.g. OrderStatus (enum/value-enum type) or byte
  access     TEXT NOT NULL,           -- read | write
  side       TEXT NOT NULL,
  rel_path   TEXT NOT NULL,
  line       INTEGER NOT NULL,
  snippet    TEXT
);
CREATE INDEX IF NOT EXISTS ix_ecol ON code_entity_columns(table_name COLLATE NOCASE, column COLLATE NOCASE);

-- MasterViews (grid screens) and their columns
CREATE TABLE IF NOT EXISTS ui_views (
  view_name   TEXT NOT NULL,
  object_name TEXT,
  side        TEXT NOT NULL,
  props       TEXT,                   -- JSON of the scalar initializer properties
  rel_path    TEXT NOT NULL,
  line        INTEGER NOT NULL,
  line_end    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_uiv_name ON ui_views(view_name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS ix_uiv_obj ON ui_views(object_name COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS ui_view_columns (
  view_name   TEXT NOT NULL,
  side        TEXT NOT NULL,
  ordinal     INTEGER NOT NULL,
  field_name  TEXT NOT NULL,
  caption     TEXT,
  column_type TEXT,
  mem_values  TEXT,
  props       TEXT,
  rel_path    TEXT NOT NULL,
  line        INTEGER NOT NULL,
  line_end    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_uivc ON ui_view_columns(view_name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS ix_uivc_field ON ui_view_columns(field_name COLLATE NOCASE);

-- Reports and their filters (= sproc parameters) and output fields
CREATE TABLE IF NOT EXISTS ui_reports (
  report_name TEXT NOT NULL,
  title       TEXT,
  object_name TEXT,
  data_type   TEXT,
  category    TEXT,
  side        TEXT NOT NULL,
  props       TEXT,
  rel_path    TEXT NOT NULL,
  line        INTEGER NOT NULL,
  line_end    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_uir_name ON ui_reports(report_name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS ix_uir_obj ON ui_reports(object_name COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS ui_report_filters (
  report_name TEXT NOT NULL,
  side        TEXT NOT NULL,
  rel_path    TEXT NOT NULL,
  ordinal     INTEGER NOT NULL,
  filter_name TEXT,
  caption     TEXT,
  filter_type TEXT,
  mem_type    TEXT,
  default_value TEXT,
  required    TEXT,
  line        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_uirf ON ui_report_filters(report_name COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS ui_report_fields (
  report_name TEXT NOT NULL,
  side        TEXT NOT NULL,
  rel_path    TEXT NOT NULL,
  ordinal     INTEGER NOT NULL,
  data_field  TEXT NOT NULL,
  caption     TEXT,
  props       TEXT,
  line        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_uirfl ON ui_report_fields(report_name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS ix_uirfl_field ON ui_report_fields(data_field COLLATE NOCASE);

-- URL rewrite rules (Web.config)
CREATE TABLE IF NOT EXISTS url_rules (
  name      TEXT,
  pattern   TEXT NOT NULL,
  target    TEXT NOT NULL,
  rel_path  TEXT NOT NULL,
  line      INTEGER NOT NULL
);

-- SQL text found in C# (string literals at data-access call sites)
CREATE TABLE IF NOT EXISTS code_sql (
  code_sql_id INTEGER PRIMARY KEY,
  side       TEXT NOT NULL,
  rel_path   TEXT NOT NULL,
  line       INTEGER NOT NULL,
  container  TEXT,
  sql_text   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS code_sql_refs (
  code_sql_id INTEGER NOT NULL,
  ref_kind    TEXT NOT NULL,        -- object | column | literal
  tbl         TEXT,
  col         TEXT,
  value       TEXT
);
CREATE INDEX IF NOT EXISTS ix_csr ON code_sql_refs(tbl COLLATE NOCASE, col COLLATE NOCASE);
"""

# --- phase: link (deterministic merge) and curate (Haiku, verified) -------------
SCHEMA_LINK = """
-- every value map known for a column, one row per source
CREATE TABLE IF NOT EXISTS value_maps (
  map_id     INTEGER PRIMARY KEY,
  name       TEXT NOT NULL,
  source     TEXT NOT NULL,        -- db_description | code_enum | memtype | const_group | curated
  ref        TEXT,                 -- enum fqn, memtype name, column key ...
  rel_path   TEXT,
  line       INTEGER,
  side       TEXT
);
CREATE TABLE IF NOT EXISTS value_map_items (
  map_id   INTEGER NOT NULL,
  value    TEXT NOT NULL,
  label    TEXT NOT NULL,
  rel_path TEXT,
  line     INTEGER
);
CREATE INDEX IF NOT EXISTS ix_vmi ON value_map_items(map_id);
CREATE TABLE IF NOT EXISTS column_value_maps (
  tbl        TEXT NOT NULL,
  col        TEXT NOT NULL,
  map_id     INTEGER NOT NULL,
  via        TEXT NOT NULL,        -- how the column was linked to this map
  evidence   TEXT,                 -- human-readable pointer (path:line / object:line)
  confidence TEXT NOT NULL         -- exact | strong | name_match
);
CREATE INDEX IF NOT EXISTS ix_cvm ON column_value_maps(tbl COLLATE NOCASE, col COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS relationships (
  rel_id       INTEGER PRIMARY KEY,
  from_table   TEXT NOT NULL,
  from_column  TEXT NOT NULL,
  to_table     TEXT NOT NULL,
  to_column    TEXT NOT NULL,
  kind         TEXT NOT NULL,      -- fk | mined_join | description | naming
  declared     INTEGER NOT NULL DEFAULT 0,
  occurrences  INTEGER NOT NULL DEFAULT 0,
  containment  REAL,               -- share of distinct from-values found in to-column
  confidence   TEXT NOT NULL,      -- exact | strong | weak
  example      TEXT,               -- verbatim predicate
  example_obj  TEXT,
  example_line INTEGER,
  note         TEXT
);
CREATE INDEX IF NOT EXISTS ix_rel_from ON relationships(from_table COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS ix_rel_to ON relationships(to_table COLLATE NOCASE);

-- local/foreign currency pairs and other column pair semantics
CREATE TABLE IF NOT EXISTS column_notes (
  tbl     TEXT NOT NULL,
  col     TEXT NOT NULL,
  topic   TEXT NOT NULL,           -- currency | date_clean | synced | security | ...
  note    TEXT NOT NULL,
  evidence TEXT
);
CREATE INDEX IF NOT EXISTS ix_cn ON column_notes(tbl COLLATE NOCASE, col COLLATE NOCASE);

-- screen/report -> object chain endpoints
CREATE TABLE IF NOT EXISTS ui_targets (
  target_kind TEXT NOT NULL,       -- view | report
  name        TEXT NOT NULL,
  title       TEXT,
  url         TEXT,
  object_name TEXT,
  obj_id      INTEGER
);
CREATE INDEX IF NOT EXISTS ix_uit_name ON ui_targets(name COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS curated (
  fact_id     INTEGER PRIMARY KEY,
  subject     TEXT NOT NULL,       -- 'Table.Column' | 'object:Name' | 'value:Table.Column=5' ...
  fact_type   TEXT NOT NULL,       -- meaning | value_label | purpose | param | adjudication
  value       TEXT,                -- value_label: the stored value
  text        TEXT NOT NULL,
  citations   TEXT NOT NULL,       -- JSON list of verified "path:line" / "object:line"
  packet      TEXT,
  model       TEXT
);
CREATE INDEX IF NOT EXISTS ix_cur ON curated(subject COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS style_rules (
  kind     TEXT NOT NULL,          -- view | proc | report_proc | function | any
  topic    TEXT NOT NULL,
  rule     TEXT NOT NULL,
  evidence TEXT,
  share    REAL
);
CREATE TABLE IF NOT EXISTS style_exemplars (
  kind     TEXT NOT NULL,
  obj_name TEXT NOT NULL,
  reason   TEXT
);

CREATE TABLE IF NOT EXISTS conflicts (
  subject TEXT NOT NULL,
  detail  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_conf ON conflicts(subject COLLATE NOCASE);

-- search surface for find_db_object
CREATE TABLE IF NOT EXISTS search_items (
  item_id   INTEGER PRIMARY KEY,
  kind      TEXT NOT NULL,         -- table | view | proc | ... | column | ui_view | report | caption | url
  name      TEXT NOT NULL,         -- canonical name to pass to other tools
  text      TEXT NOT NULL,         -- what is matched
  detail    TEXT,
  weight    REAL NOT NULL DEFAULT 1
);
"""

SCHEMA_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
  text, name UNINDEXED, kind UNINDEXED, content='search_items', content_rowid='item_id',
  tokenize = "unicode61 tokenchars '_'"
);
"""

ALL_SCHEMAS = (SCHEMA_CATALOG, SCHEMA_PROFILE, SCHEMA_SQLPARSE, SCHEMA_CODE, SCHEMA_LINK)


def schema_fingerprint() -> str:
    h = hashlib.blake2b(digest_size=8)
    for s in ALL_SCHEMAS:
        h.update(s.encode())
    h.update(SCHEMA_FTS.encode())
    h.update(str(USER_VERSION).encode())
    return h.hexdigest()


def connect(db_path: Path, *, read_only: bool = False, timeouts: TimeoutsCfg | None = None) -> sqlite3.Connection:
    db_path = Path(db_path)
    t = timeouts or TimeoutsCfg()
    if read_only:
        con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=float(t.sqlite_read_s),
                              check_same_thread=True)
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(db_path, timeout=float(t.sqlite_write_s))
        con.executescript(PRAGMAS)
    con.row_factory = sqlite3.Row
    return con


def _drop_all(con: sqlite3.Connection) -> None:
    names = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "AND name NOT LIKE 'search_fts_%'")]
    for n in names:
        con.execute(f'DROP TABLE IF EXISTS "{n}"')
    con.commit()


def init_schema(con: sqlite3.Connection) -> None:
    """Create the schema; a fingerprint change drops everything (the index is derived)."""
    have = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='index_meta'").fetchone()
    if have and get_meta(con, "schema_fingerprint") != schema_fingerprint():
        _drop_all(con)
    for s in ALL_SCHEMAS:
        con.executescript(s)
    con.executescript(SCHEMA_FTS)
    con.execute(f"PRAGMA user_version = {USER_VERSION}")
    set_meta(con, "schema_version", USER_VERSION)
    set_meta(con, "schema_fingerprint", schema_fingerprint())
    con.commit()


def clear_tables(con: sqlite3.Connection, tables: Iterable[str]) -> None:
    for t in tables:
        con.execute(f"DELETE FROM {t}")
    con.commit()


def set_meta(con: sqlite3.Connection, key: str, value: Any) -> None:
    con.execute("INSERT INTO index_meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def get_meta(con: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = con.execute("SELECT value FROM index_meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def all_meta(con: sqlite3.Connection) -> dict[str, str]:
    return {r[0]: r[1] for r in con.execute("SELECT key,value FROM index_meta")}


def phase_done(con: sqlite3.Connection, name: str, info: str = "") -> None:
    import datetime as _dt
    con.execute("INSERT INTO phases(name,finished_at,info) VALUES(?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET finished_at=excluded.finished_at, info=excluded.info",
                (name, _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"), info))
    con.commit()
