"""Sandbox clone -- a copy of appdb' schema with a few rows per table, for test_sql.

    python -m senior_dev_db.prep.sandbox          (scripts/sandbox.ps1)

appdb is read through the usual read-only connection; the clone is written through
the [sandbox] connection, on a SQL Server we own.  Steps:

  1. (re)create the database with appdb' collation and compatibility level, marked
     with the extended property advisor_sandbox so a rebuild never drops anything else;
  2. sequences and tables (columns, identity, PK / unique constraints) -- no FKs yet;
  3. data: the newest rows_per_table rows of every table, small reference tables whole,
     plus every parent row those rows reference (recursively), so foreign keys hold;
  4. functions, views, procedures in passes until no more compile; defaults, indexes,
     foreign keys (WITH CHECK when the data allows), triggers last;
  5. a database snapshot -- test_sql reverts to it if a script escapes its transaction.

A failed object never stops the build: it is listed in .index/sandbox_report.json and
by index_status.
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from pathlib import Path

import pyodbc

from ..config import Config, load_config
from .catalog import connect

log = logging.getLogger("prep.sandbox")


def qn(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def sb_connect(cfg: Config, database: str | None = None) -> pyodbc.Connection:
    con = pyodbc.connect(cfg.sandbox_connection_string(database=database), autocommit=True)
    con.timeout = cfg.db.query_timeout_s
    return con


def coltype(t: str, max_length: int, precision: int, scale: int) -> str:
    if t in ("varchar", "char", "varbinary", "binary"):
        return f"{t}({'max' if max_length == -1 else max_length})"
    if t in ("nvarchar", "nchar"):
        return f"{t}({'max' if max_length == -1 else max_length // 2})"
    if t in ("decimal", "numeric"):
        return f"{t}({precision},{scale})"
    if t in ("datetime2", "time", "datetimeoffset"):
        return f"{t}({scale})"
    if t == "float" and precision != 53:
        return f"float({precision})"
    return t


# ---------------------------------------------------------------- partner tables
PARTNER_SQL = """
SELECT DISTINCT r.from_table, r.from_column, r.to_table, r.to_column
FROM relationships r
JOIN objects a ON a.name = r.from_table COLLATE NOCASE AND a.kind = 'table'
JOIN indexes ia ON ia.obj_id = a.obj_id AND ia.is_primary_key = 1 AND ia.key_columns = r.from_column COLLATE NOCASE
JOIN columns ca ON ca.obj_id = a.obj_id AND ca.name = r.from_column COLLATE NOCASE
JOIN objects b ON b.name = r.to_table COLLATE NOCASE AND b.kind = 'table'
JOIN indexes ib ON ib.obj_id = b.obj_id AND ib.is_primary_key = 1 AND ib.key_columns = r.to_column COLLATE NOCASE
JOIN columns cb ON cb.obj_id = b.obj_id AND cb.name = r.to_column COLLATE NOCASE
WHERE r.declared = 0 AND r.confidence = 'strong' AND r.kind IN ('mined_join', 'description')
  AND a.obj_id <> b.obj_id AND ca.type_name = cb.type_name
"""


def partner_pairs(q) -> list[tuple[str, str, str, str]]:
    """Tables sharing a primary key with no declared FK between them (ReportOrderTable.Id = OrderTable.Id):
    strong inferred links from the index where both columns are their table's whole single-column PK, same
    type.  One (table_a, col_a, table_b, col_b) per pair; q(sql) is any query callable on the index."""
    out, seen = [], set()
    for a, ca, b, cb in q(PARTNER_SQL):
        k = frozenset(((a.lower(), ca.lower()), (b.lower(), cb.lower())))
        if k not in seen:
            seen.add(k)
            out.append((a, ca, b, cb))
    return out


def index_partner_pairs(cfg: Config) -> list[tuple[str, str, str, str]] | None:
    """partner_pairs from the served index, or None when there is no index to read."""
    import sqlite3
    p = cfg.paths.db_path
    if not p.is_file():
        return None
    con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    try:
        return partner_pairs(lambda sql: con.execute(sql).fetchall())
    finally:
        con.close()


# ---------------------------------------------------------------- read the source catalog
def read_catalog(src) -> dict:
    cur = src.cursor()
    cat: dict = {}
    cat["collation"], cat["compat"] = cur.execute(
        "select convert(sysname, DATABASEPROPERTYEX(db_name(),'Collation')), compatibility_level "
        "from sys.databases where name = db_name()").fetchone()
    cat["snapshot_at"] = cur.execute("select convert(varchar(19), getdate(), 126)").fetchone()[0]
    cat["tables"] = {r.object_id: r.name for r in cur.execute(
        "select object_id, name from sys.tables where is_ms_shipped = 0 and schema_id = schema_id('dbo')")}
    cols = defaultdict(list)
    for r in cur.execute(
            "select c.object_id, c.column_id, c.name, ty.name tname, c.max_length, c.precision, c.scale, "
            "c.collation_name, c.is_nullable, c.is_identity, c.is_computed, "
            "convert(bigint, ic.seed_value) seed, convert(bigint, ic.increment_value) incr, "
            "cc.definition computed_def, cc.is_persisted "
            "from sys.columns c join sys.tables t on t.object_id = c.object_id "
            "join sys.types ty on ty.user_type_id = c.user_type_id "
            "left join sys.identity_columns ic on ic.object_id = c.object_id and ic.column_id = c.column_id "
            "left join sys.computed_columns cc on cc.object_id = c.object_id and cc.column_id = c.column_id "
            "where t.is_ms_shipped = 0 order by c.object_id, c.column_id"):
        cols[r.object_id].append(r)
    cat["columns"] = cols
    idx = defaultdict(list)          # (object_id, index_id) -> [(col, desc, included)]
    for r in cur.execute(
            "select ic.object_id, ic.index_id, c.name, ic.is_descending_key, ic.is_included_column "
            "from sys.index_columns ic join sys.columns c on c.object_id = ic.object_id and c.column_id = ic.column_id "
            "order by ic.object_id, ic.index_id, ic.is_included_column, ic.key_ordinal, ic.index_column_id"):
        idx[(r.object_id, r.index_id)].append((r.name, r.is_descending_key, r.is_included_column))
    cat["indexes"] = [dict(object_id=r.object_id, index_id=r.index_id, name=r.name, type=r.type_desc,
                           unique=r.is_unique, pk=r.is_primary_key, uq=r.is_unique_constraint,
                           filter=r.filter_definition, cols=idx[(r.object_id, r.index_id)])
                      for r in cur.execute(
            "select i.object_id, i.index_id, i.name, i.type_desc, i.is_unique, i.is_primary_key, "
            "i.is_unique_constraint, i.filter_definition from sys.indexes i join sys.tables t on t.object_id = i.object_id "
            "where t.is_ms_shipped = 0 and i.type in (1, 2) and i.is_hypothetical = 0")]
    cat["defaults"] = [(r.parent_object_id, r.name, r.col, r.definition) for r in cur.execute(
        "select d.parent_object_id, d.name, c.name col, d.definition from sys.default_constraints d "
        "join sys.columns c on c.object_id = d.parent_object_id and c.column_id = d.parent_column_id")]
    cat["checks"] = [(r.parent_object_id, r.name, r.definition, r.is_disabled) for r in cur.execute(
        "select parent_object_id, name, definition, is_disabled from sys.check_constraints")]
    fkcols = defaultdict(list)
    for r in cur.execute(
            "select fkc.constraint_object_id, pc.name pcol, rc.name rcol from sys.foreign_key_columns fkc "
            "join sys.columns pc on pc.object_id = fkc.parent_object_id and pc.column_id = fkc.parent_column_id "
            "join sys.columns rc on rc.object_id = fkc.referenced_object_id and rc.column_id = fkc.referenced_column_id "
            "order by fkc.constraint_object_id, fkc.constraint_column_id"):
        fkcols[r.constraint_object_id].append((r.pcol, r.rcol))
    cat["fks"] = [dict(name=r.name, parent=r.parent_object_id, ref=r.referenced_object_id,
                       on_delete=r.delete_referential_action_desc, on_update=r.update_referential_action_desc,
                       disabled=r.is_disabled, untrusted=r.is_not_trusted, cols=fkcols[r.object_id])
                  for r in cur.execute(
            "select object_id, name, parent_object_id, referenced_object_id, delete_referential_action_desc, "
            "update_referential_action_desc, is_disabled, is_not_trusted from sys.foreign_keys")]
    cat["sequences"] = [r for r in cur.execute(
        "select s.name, ty.name tname, convert(bigint, s.start_value) start_value, convert(bigint, s.increment) incr, "
        "convert(bigint, s.minimum_value) minv, convert(bigint, s.maximum_value) maxv, s.is_cycling, "
        "convert(bigint, s.current_value) cur from sys.sequences s join sys.types ty on ty.user_type_id = s.user_type_id")]
    cat["modules"] = [r for r in cur.execute(
        "select o.object_id, o.name, o.type, m.definition, m.uses_ansi_nulls, m.uses_quoted_identifier, "
        "isnull(tr.is_disabled, 0) trg_disabled, tr.parent_id "
        "from sys.objects o join sys.sql_modules m on m.object_id = o.object_id "
        "left join sys.triggers tr on tr.object_id = o.object_id "
        "where o.is_ms_shipped = 0 and o.type in ('V', 'P', 'FN', 'IF', 'TF', 'TR')")]
    cat["row_counts"] = {r[0]: r[1] for r in cur.execute(
        "select object_id, sum(rows) from sys.partitions where index_id in (0, 1) group by object_id")}
    return cat


# ---------------------------------------------------------------- build
class Builder:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.sb = cfg.sandbox
        self.report: dict = {"failed": [], "warnings": [], "counts": {}}

    def fail(self, what: str, err: Exception | str) -> None:
        msg = str(err)
        if isinstance(err, pyodbc.Error) and len(err.args) > 1:
            msg = str(err.args[1])
        msg = msg.split("[SQL Server]")[-1].strip()[:300]
        self.report["failed"].append(f"{what}: {msg}")
        log.debug("failed %s: %s", what, msg)

    # -- 1. database
    def recreate_database(self, cat: dict) -> None:
        db, snap = self.sb.database, self.sb.snapshot_name
        m = sb_connect(self.cfg, "master")
        c = m.cursor()
        exists = c.execute("select database_id from sys.databases where name = ?", db).fetchone()
        if exists:
            marked = c.execute(
                f"select value from {qn(db)}.sys.extended_properties where class = 0 and name = ?", self.sb.marker).fetchone()
            if not marked:
                raise SystemExit(f"database {db} exists on {self.sb.server} but is not a sandbox built by this tool "
                                 f"(no '{self.sb.marker}' property); refusing to drop it. Choose another [sandbox] database.")
        if c.execute("select 1 from sys.databases where name = ? and source_database_id is not null", snap).fetchone():
            c.execute(f"drop database {qn(snap)}")
        if exists:
            c.execute(f"alter database {qn(db)} set single_user with rollback immediate")
            c.execute(f"drop database {qn(db)}")
        c.execute(f"create database {qn(db)} collate {cat['collation']}")
        c.execute(f"alter database {qn(db)} set recovery simple")
        c.execute(f"alter database {qn(db)} set compatibility_level = {int(cat['compat'])}")
        # autocommit commits every inserted row: skip the per-row log flush (slow on VM disks).
        # Switched back off before the snapshot.
        c.execute(f"alter database {qn(db)} set delayed_durability = forced")
        m.close()
        self.con = sb_connect(self.cfg)
        self.con.cursor().execute(f"exec sys.sp_addextendedproperty @name = N'{self.sb.marker}', @value = N'1'")

    # -- 2. tables
    def create_tables(self, cat: dict) -> None:
        cur = self.con.cursor()
        for s in cat["sequences"]:
            try:
                cur.execute(f"create sequence {qn(s.name)} as {s.tname} start with {s.cur + s.incr if s.cur is not None else s.start_value} "
                            f"increment by {s.incr} minvalue {s.minv} maxvalue {s.maxv} {'cycle' if s.is_cycling else 'no cycle'}")
            except pyodbc.Error as e:
                self.fail(f"sequence {s.name}", e)
        keys = {(i["object_id"]): [] for i in cat["indexes"]}
        for i in cat["indexes"]:
            if i["pk"] or i["uq"]:
                keys.setdefault(i["object_id"], []).append(i)
        ok = 0
        for oid, tname in sorted(cat["tables"].items(), key=lambda kv: kv[1].lower()):
            parts = []
            for c in cat["columns"][oid]:
                if c.is_computed:
                    parts.append(f"{qn(c.name)} as {c.computed_def}" + (" persisted" if c.is_persisted else ""))
                    continue
                d = f"{qn(c.name)} {coltype(c.tname, c.max_length, c.precision, c.scale)}"
                if c.collation_name and c.collation_name != cat["collation"]:
                    d += f" collate {c.collation_name}"
                if c.is_identity:
                    d += f" identity({c.seed},{c.incr})"
                d += " null" if c.is_nullable else " not null"
                parts.append(d)
            for k in keys.get(oid, []):
                cols = ", ".join(qn(n) + (" desc" if desc else "") for n, desc, inc in k["cols"] if not inc)
                parts.append(f"constraint {qn(k['name'])} {'primary key' if k['pk'] else 'unique'} "
                             f"{'clustered' if k['type'] == 'CLUSTERED' else 'nonclustered'} ({cols})")
            try:
                cur.execute(f"create table {qn(tname)} (\n  " + ",\n  ".join(parts) + "\n)")
                ok += 1
            except pyodbc.Error as e:
                self.fail(f"table {tname}", e)
        self.report["counts"]["tables"] = ok

    # -- 3. data
    def copy_data(self, src, cat: dict) -> None:
        sb = self.sb
        scur, dcur = src.cursor(), self.con.cursor()
        tables = cat["tables"]
        cols = {oid: [c for c in cat["columns"][oid] if not c.is_computed] for oid in tables}
        # parameters are cast to the key column's type: an nvarchar parameter against a
        # varchar key makes SQL Server scan the table instead of seeking
        ctype = {oid: {c.name: coltype(c.tname, c.max_length, c.precision, c.scale) for c in cat["columns"][oid]}
                 for oid in tables}
        stats = {"queries": 0, "read_s": 0.0}
        pk = {i["object_id"]: [n for n, d, inc in i["cols"] if not inc] for i in cat["indexes"] if i["pk"]}
        fks_of = defaultdict(list)
        for f in cat["fks"]:
            if f["parent"] in tables and f["ref"] in tables:
                fks_of[f["parent"]].append(f)
        # partner tables (same primary key, no declared FK): each sampled row brings its partner row, so
        # joins like ReportOrderTable.Id = OrderTable.Id find their rows in the clone too
        partners = index_partner_pairs(self.cfg)
        if partners is None:
            self.report["warnings"].append("no index to read partner tables from: rows of tables that share a "
                                           "key without a foreign key (ReportOrderTable / OrderTable) may not join")
        oid_of = {n.lower(): oid for oid, n in tables.items()}
        partner_oids = set()
        for a, ca, b, cb in partners or []:
            oa, ob = oid_of.get(a.lower()), oid_of.get(b.lower())
            if oa is None or ob is None or pk.get(oa, [""])[0].lower() != ca.lower() \
                    or pk.get(ob, [""])[0].lower() != cb.lower():
                continue
            fks_of[oa].append({"ref": ob, "cols": [(pk[oa][0], pk[ob][0])], "partner": True})
            fks_of[ob].append({"ref": oa, "cols": [(pk[ob][0], pk[oa][0])], "partner": True})
            partner_oids |= {oa, ob}
        partner_rows = 0
        have: dict[tuple[int, tuple[str, ...]], set] = defaultdict(set)   # (table, key cols) -> key tuples copied
        pending: dict[int, list] = defaultdict(list)                      # rows to insert per table
        extra = defaultdict(int)

        def colnames(oid):
            return [c.name for c in cols[oid]]

        def keep(oid, rows):
            """Add rows not yet copied; returns the new ones."""
            names = colnames(oid)
            key = pk.get(oid)
            out = []
            for r in rows:
                if key:
                    kt = tuple(r[names.index(k)] for k in key)
                    if kt in have[(oid, tuple(key))]:
                        continue
                    have[(oid, tuple(key))].add(kt)
                out.append(r)
            pending[oid].extend(out)
            return out

        def select(oid, where="", params=(), top=None, order=True):
            names = colnames(oid)
            sel = ", ".join(qn(n) for n in names)
            order_by = ""
            if order and pk.get(oid):
                order_by = " order by " + ", ".join(qn(k) + " desc" for k in pk[oid])
            sql = (f"select {f'top ({top}) ' if top else ''}{sel} from {qn(tables[oid])} with (nolock)"
                   f"{(' where ' + where) if where else ''}{order_by}")
            t = time.time()
            rows = [tuple(r) for r in scur.execute(sql, *params).fetchall()]
            dt = time.time() - t
            stats["queries"] += 1
            stats["read_s"] += dt
            if dt > 5:
                log.info("slow read from appdb: %s, %d rows in %.1fs", tables[oid], len(rows), dt)
                self.report["warnings"].append(f"slow read from appdb: {tables[oid]} took {dt:.1f}s")
            return rows

        # seeds
        t0 = time.time()
        frontier: dict[int, list] = {}
        for n_done, oid in enumerate(tables, 1):
            if n_done % 100 == 0:
                log.info("  read %d of %d tables", n_done, len(tables))
            n = cat["row_counts"].get(oid, 0) or 0
            if n == 0:
                continue
            try:
                rows = select(oid) if n <= self.cfg.profile.lookup_max_rows else select(oid, top=sb.rows_per_table)
            except pyodbc.Error as e:
                self.fail(f"read {tables[oid]}", e)
                continue
            frontier[oid] = keep(oid, rows)
        log.info("  sample rows read: %d queries, %.1fs", stats["queries"], time.time() - t0)
        # parent closure
        t0, q0 = time.time(), stats["queries"]
        while frontier:
            nxt: dict[int, list] = defaultdict(list)
            for oid, rows in frontier.items():
                if not rows:
                    continue
                names = colnames(oid)
                for f in fks_of[oid]:
                    pcols = [p for p, _ in f["cols"]]
                    rcols = [r for _, r in f["cols"]]
                    vals = {tuple(row[names.index(p)] for p in pcols) for row in rows}
                    vals = {v for v in vals if all(x is not None for x in v)}
                    known = have[(f["ref"], tuple(rcols))] if pk.get(f["ref"]) == rcols else set()
                    vals -= known
                    if not vals:
                        continue
                    room = sb.max_parent_rows - extra[f["ref"]]
                    if room <= 0:
                        self.report["warnings"].append(f"parent rows of {tables[f['ref']]} capped at {sb.max_parent_rows}")
                        continue
                    vals = list(vals)[:room]
                    got = []
                    p = {c: f"cast(? as {ctype[f['ref']][c]})" for c in rcols}
                    try:
                        if len(rcols) == 1:
                            flat = [v[0] for v in vals]
                            for i in range(0, len(flat), 500):
                                chunk = flat[i:i + 500]
                                got += select(f["ref"], f"{qn(rcols[0])} in ({','.join([p[rcols[0]]] * len(chunk))})",
                                              chunk, order=False)
                        else:
                            for i in range(0, len(vals), 100):
                                chunk = vals[i:i + 100]
                                cond = " or ".join("(" + " and ".join(f"{qn(c)} = {p[c]}" for c in rcols) + ")" for _ in chunk)
                                got += select(f["ref"], cond, [x for v in chunk for x in v], order=False)
                    except pyodbc.Error as e:
                        self.fail(f"read parents {tables[f['ref']]}", e)
                        continue
                    new = keep(f["ref"], got)
                    extra[f["ref"]] += len(new)
                    if f.get("partner"):
                        partner_rows += len(new)
                    if new:
                        nxt[f["ref"]].extend(new)
            frontier = dict(nxt)
        log.info("  parent rows read: %d queries, %.1fs (%d partner rows from %d partner tables)",
                 stats["queries"] - q0, time.time() - t0, partner_rows, len(partner_oids))
        self.report["counts"]["partner_rows"] = partner_rows
        # insert
        t0 = time.time()
        total = 0
        for oid, rows in pending.items():
            if not rows:
                continue
            names = colnames(oid)
            ident = any(c.is_identity for c in cols[oid])
            # fast_executemany allocates max-size buffers: unusable for (max)/LOB columns
            lob = any(c.max_length == -1 or c.tname in ("image", "text", "ntext", "xml") for c in cols[oid])
            sql = (f"insert into {qn(tables[oid])} ({', '.join(qn(n) for n in names)}) "
                   f"values ({', '.join('?' * len(names))})")
            tw = time.time()
            try:
                if ident:
                    dcur.execute(f"set identity_insert {qn(tables[oid])} on")
                try:
                    dcur.fast_executemany = not lob
                    dcur.executemany(sql, rows)
                except (pyodbc.Error, MemoryError):
                    dcur.fast_executemany = False
                    dcur.execute(f"delete from {qn(tables[oid])}")
                    for r in rows:
                        dcur.execute(sql, *r)
                total += len(rows)
            except pyodbc.Error as e:
                self.fail(f"data {tables[oid]}", e)
            finally:
                dcur.fast_executemany = False
                if ident:
                    try:
                        dcur.execute(f"set identity_insert {qn(tables[oid])} off")
                    except pyodbc.Error:
                        pass
                if time.time() - tw > 1:
                    log.info("slow write to the clone: %s, %d rows%s in %.1fs", tables[oid], len(rows),
                             " (row by row)" if lob else "", time.time() - tw)
        log.info("  %d rows written to the clone: %.1fs", total, time.time() - t0)
        self.report["counts"]["rows"] = total
        self.report["counts"]["tables_with_rows"] = sum(1 for r in pending.values() if r)
        self.report["timings"] = {"read_queries": stats["queries"], "read_s": round(stats["read_s"], 1),
                                  "write_s": round(time.time() - t0, 1)}

    # -- 4. modules and constraints
    def _module(self, cur, m) -> None:
        cur.execute(f"set ansi_nulls {'on' if m.uses_ansi_nulls else 'off'}")
        cur.execute(f"set quoted_identifier {'on' if m.uses_quoted_identifier else 'off'}")
        cur.execute(m.definition)

    def create_modules(self, cat: dict, triggers: bool) -> None:
        cur = self.con.cursor()
        todo = [m for m in cat["modules"] if (m.type.strip() == "TR") == triggers]
        errors: dict[str, Exception] = {}
        done = 0
        while todo:
            left = []
            for m in todo:
                try:
                    self._module(cur, m)
                    done += 1
                    if triggers and m.trg_disabled:
                        cur.execute(f"disable trigger {qn(m.name)} on {qn(cat['tables'].get(m.parent_id, ''))}")
                except pyodbc.Error as e:
                    errors[m.name] = e
                    left.append(m)
            if len(left) == len(todo):
                break
            todo = left
        cur.execute("set ansi_nulls on")
        cur.execute("set quoted_identifier on")
        for m in todo:
            self.fail(f"{'trigger' if triggers else 'module'} {m.name}", errors[m.name])
        self.report["counts"]["triggers" if triggers else "modules"] = done

    def create_constraints(self, cat: dict) -> None:
        cur = self.con.cursor()
        tables = cat["tables"]
        n = 0
        for oid, name, col, definition in cat["defaults"]:
            if oid not in tables:
                continue
            try:
                cur.execute(f"alter table {qn(tables[oid])} add constraint {qn(name)} default {definition} for {qn(col)}")
                n += 1
            except pyodbc.Error as e:
                self.fail(f"default {name}", e)
        self.report["counts"]["defaults"] = n
        for oid, name, definition, disabled in cat["checks"]:
            try:
                cur.execute(f"alter table {qn(tables[oid])} with nocheck add constraint {qn(name)} check {definition}")
                if disabled:
                    cur.execute(f"alter table {qn(tables[oid])} nocheck constraint {qn(name)}")
            except pyodbc.Error as e:
                self.fail(f"check {name}", e)
        n = 0
        for i in cat["indexes"]:
            if i["pk"] or i["uq"] or i["object_id"] not in tables:
                continue
            keys = ", ".join(qn(c) + (" desc" if d else "") for c, d, inc in i["cols"] if not inc)
            inc = ", ".join(qn(c) for c, d, inc in i["cols"] if inc)
            sql = (f"create {'unique ' if i['unique'] else ''}{'clustered' if i['type'] == 'CLUSTERED' else 'nonclustered'} "
                   f"index {qn(i['name'])} on {qn(tables[i['object_id']])} ({keys})"
                   + (f" include ({inc})" if inc else "") + (f" where {i['filter']}" if i["filter"] else ""))
            try:
                cur.execute(sql)
                n += 1
            except pyodbc.Error as e:
                self.fail(f"index {i['name']}", e)
        self.report["counts"]["indexes"] = n
        n = 0
        for f in cat["fks"]:
            if f["parent"] not in tables or f["ref"] not in tables:
                continue
            body = (f"constraint {qn(f['name'])} foreign key ({', '.join(qn(p) for p, _ in f['cols'])}) "
                    f"references {qn(tables[f['ref']])} ({', '.join(qn(r) for _, r in f['cols'])})"
                    + ("" if f["on_delete"] == "NO_ACTION" else f" on delete {f['on_delete'].replace('_', ' ').lower()}")
                    + ("" if f["on_update"] == "NO_ACTION" else f" on update {f['on_update'].replace('_', ' ').lower()}"))
            t = qn(tables[f["parent"]])
            try:
                cur.execute(f"alter table {t} with check add {body}")
                n += 1
            except pyodbc.Error:
                try:
                    cur.execute(f"alter table {t} with nocheck add {body}")
                    n += 1
                    self.report["warnings"].append(f"foreign key {f['name']} created WITH NOCHECK " + ("(it is untrusted in appdb too: rows there violate it)" if f["untrusted"] else "(sampled rows violate it)"))
                except pyodbc.Error as e:
                    self.fail(f"foreign key {f['name']}", e)
                    continue
            if f["disabled"]:
                cur.execute(f"alter table {t} nocheck constraint {qn(f['name'])}")
        self.report["counts"]["foreign_keys"] = n

    # -- 5. snapshot
    def snapshot(self) -> None:
        db, snap = self.sb.database, self.sb.snapshot_name
        m = sb_connect(self.cfg, "master")
        c = m.cursor()
        files = c.execute("select name, physical_name from sys.master_files where database_id = db_id(?) and type = 0",
                          db).fetchall()
        on = ", ".join(f"(name = {qn(n)}, filename = '{str(Path(p).with_name(snap + '_' + n + '.ss'))}')"
                       for n, p in files)
        c.execute(f"create database {qn(snap)} on {on} as snapshot of {qn(db)}")
        m.close()

    def run(self) -> dict:
        t0 = time.time()
        src = connect(self.cfg)
        log.info("reading appdb catalog")
        cat = read_catalog(src)
        log.info("recreating %s on %s", self.sb.database, self.sb.server)
        self.recreate_database(cat)
        self.create_tables(cat)
        log.info("copying rows")
        self.copy_data(src, cat)
        log.info("creating functions, views, procedures")
        self.create_modules(cat, triggers=False)
        self.create_constraints(cat)
        self.create_modules(cat, triggers=True)
        cur = self.con.cursor()
        # test_sql impersonates this user: full rights inside the clone, none outside it
        # (a database user without login has no server permissions and no other database)
        cur.execute(f"create user {qn(self.sb.runner)} without login")
        cur.execute(f"alter role db_owner add member {qn(self.sb.runner)}")
        info = {"built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "source_snapshot_at": cat["snapshot_at"],
                "source_tables": len(cat["tables"]), "source_modules": len(cat["modules"]),
                "failed_count": len(self.report["failed"])}
        cur.execute("exec sys.sp_addextendedproperty @name = N'advisor_sandbox_info', @value = ?",
                    json.dumps(info | {"counts": self.report["counts"]})[:3500])
        self.con.close()
        m = sb_connect(self.cfg, "master")
        m.cursor().execute(f"alter database {qn(self.sb.database)} set delayed_durability = disabled")
        m.close()
        self.snapshot()
        src.close()
        self.report |= info
        self.report["elapsed_s"] = round(time.time() - t0, 1)
        out = self.cfg.paths.sandbox_report
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(self.report, indent=1, ensure_ascii=False), encoding="utf-8")
        return self.report


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cfg = load_config(a.config)
    if not cfg.sandbox.enabled:
        print("[sandbox] enabled = false in config.toml; nothing to build")
        return 1
    r = Builder(cfg).run()
    print(json.dumps({k: r[k] for k in ("built_at", "counts", "elapsed_s")}, indent=1))
    print(f"{len(r['failed'])} object(s) failed, {len(r['warnings'])} warning(s): see "
          f"{cfg.paths.sandbox_report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
