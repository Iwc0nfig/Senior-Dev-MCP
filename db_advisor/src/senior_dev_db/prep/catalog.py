"""Phase catalog: everything SQL Server knows about its own objects.

Read-only sys.* queries over one connection.  Module text is stored with LF line
endings so that line numbers are stable and match what tools cite.
"""

from __future__ import annotations

import logging
import sqlite3

import pyodbc

from ..config import Config
from .. import db as dbm

log = logging.getLogger("prep.catalog")

KIND = {
    "U": "table", "V": "view", "P": "proc", "FN": "scalar_fn", "IF": "inline_tvf",
    "TF": "tvf", "TR": "trigger",
}

Q_OBJECTS = """
select o.object_id, s.name schema_name, o.name, o.type, o.parent_object_id,
       convert(varchar(19), o.create_date, 126), convert(varchar(19), o.modify_date, 126),
       (select sum(p.rows) from sys.partitions p where p.object_id=o.object_id and p.index_id in (0,1)) row_count,
       cast(ep.value as nvarchar(max)) description
from sys.objects o
join sys.schemas s on s.schema_id=o.schema_id
left join sys.extended_properties ep on ep.class=1 and ep.major_id=o.object_id and ep.minor_id=0 and ep.name='MS_Description'
where o.is_ms_shipped=0 and o.type in ('U','V','P','FN','IF','TF','TR')
"""

Q_MODULES = "select m.object_id, m.definition from sys.sql_modules m join sys.objects o on o.object_id=m.object_id where o.is_ms_shipped=0"

Q_COLUMNS = """
select c.object_id, c.column_id, c.name, t.name, c.max_length, c.precision, c.scale,
       c.is_nullable, c.is_identity, c.is_computed, cc.definition, dc.definition,
       cast(ep.value as nvarchar(max))
from sys.columns c
join sys.objects o on o.object_id=c.object_id and o.is_ms_shipped=0 and o.type in ('U','V','IF','TF')
join sys.types t on t.user_type_id=c.user_type_id
left join sys.computed_columns cc on cc.object_id=c.object_id and cc.column_id=c.column_id
left join sys.default_constraints dc on dc.parent_object_id=c.object_id and dc.parent_column_id=c.column_id
left join sys.extended_properties ep on ep.class=1 and ep.major_id=c.object_id and ep.minor_id=c.column_id and ep.name='MS_Description'
"""

Q_PARAMS = """
select p.object_id, p.parameter_id, p.name, t.name, p.max_length, p.precision, p.scale,
       p.is_output, p.has_default_value, cast(ep.value as nvarchar(max))
from sys.parameters p
join sys.objects o on o.object_id=p.object_id and o.is_ms_shipped=0
join sys.types t on t.user_type_id=p.user_type_id
left join sys.extended_properties ep on ep.class=2 and ep.major_id=p.object_id and ep.minor_id=p.parameter_id and ep.name='MS_Description'
"""

Q_FKS = """
select fk.object_id, fk.name, fkc.parent_object_id, pc.name, fkc.referenced_object_id, rc.name,
       fkc.constraint_column_id, fk.is_disabled, fk.delete_referential_action_desc,
       fk.update_referential_action_desc
from sys.foreign_keys fk
join sys.foreign_key_columns fkc on fkc.constraint_object_id=fk.object_id
join sys.columns pc on pc.object_id=fkc.parent_object_id and pc.column_id=fkc.parent_column_id
join sys.columns rc on rc.object_id=fkc.referenced_object_id and rc.column_id=fkc.referenced_column_id
"""

Q_INDEXES = """
select i.object_id, i.index_id, i.name, i.type_desc, i.is_primary_key, i.is_unique,
  stuff((select ','+c.name from sys.index_columns ic join sys.columns c on c.object_id=ic.object_id and c.column_id=ic.column_id
         where ic.object_id=i.object_id and ic.index_id=i.index_id and ic.is_included_column=0 order by ic.key_ordinal for xml path('')),1,1,''),
  stuff((select ','+c.name from sys.index_columns ic join sys.columns c on c.object_id=ic.object_id and c.column_id=ic.column_id
         where ic.object_id=i.object_id and ic.index_id=i.index_id and ic.is_included_column=1 for xml path('')),1,1,''),
  i.filter_definition
from sys.indexes i join sys.objects o on o.object_id=i.object_id and o.is_ms_shipped=0 and o.type in ('U','V')
where i.index_id > 0 and i.is_hypothetical=0
"""

Q_DEPS = """
select d.referencing_id, d.referenced_schema_name, d.referenced_entity_name, d.referenced_id,
       d.is_ambiguous, d.is_caller_dependent
from sys.sql_expression_dependencies d
join sys.objects o on o.object_id=d.referencing_id and o.is_ms_shipped=0
where d.referenced_entity_name is not null
"""

# Views only: this login has no EXECUTE permission, so procedures cannot be
# described (their outputs come from the ScriptDom parse instead).  Browse mode
# (1) returns the base table/column behind every view column.
Q_RESULTSET = """
select r.column_ordinal, r.name, r.system_type_name, r.is_nullable,
       r.source_table, r.source_column, r.error_message
from sys.dm_exec_describe_first_result_set(?, NULL, 1) r
"""


def connect(cfg: Config, timeout: int | None = None) -> pyodbc.Connection:
    con = pyodbc.connect(cfg.connection_string(), autocommit=True, readonly=True)
    con.timeout = timeout or cfg.db.query_timeout_s
    return con


def _desc(s: str | None) -> str | None:
    if s is None:
        return None
    t = s.strip()
    return t if len(t.strip(".-_ ?")) >= 2 else None


def _lf(s: str | None) -> str:
    return (s or "").replace("\r\n", "\n").replace("\r", "\n")


def run(cfg: Config, con: sqlite3.Connection) -> dict:
    dbm.clear_tables(con, ["objects", "modules", "columns", "params", "fks", "indexes",
                           "deps", "result_sets"])
    sq = connect(cfg)
    cur = sq.cursor()

    info = cur.execute("select @@VERSION, DB_NAME(), DATABASEPROPERTYEX(DB_NAME(),'Collation'), "
                       "convert(varchar(19), getdate(), 126)").fetchone()
    dbm.set_meta(con, "db_version", info[0].splitlines()[0])
    dbm.set_meta(con, "db_name", info[1])
    dbm.set_meta(con, "db_collation", info[2])
    # the database's own clock at snapshot time: modify_date is compared against this, never
    # against this machine's clock (time zones differ)
    dbm.set_meta(con, "db_snapshot_at", info[3])

    rows = cur.execute(Q_OBJECTS).fetchall()
    con.executemany(
        "INSERT INTO objects(obj_id,schema_name,name,kind,type_code,parent_id,created,modified,row_count,description) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(r[0], r[1], r[2], KIND[r[3].strip()], r[3].strip(), r[4] or None, r[5], r[6], r[7], _desc(r[8])) for r in rows])
    log.info("objects: %d", len(rows))

    mods = cur.execute(Q_MODULES).fetchall()
    con.executemany("INSERT INTO modules(obj_id,definition) VALUES (?,?)",
                    [(r[0], _lf(r[1])) for r in mods])
    con.execute("UPDATE objects SET line_count=(SELECT length(definition)-length(replace(definition,char(10),''))+1 "
                "FROM modules m WHERE m.obj_id=objects.obj_id) WHERE obj_id IN (SELECT obj_id FROM modules)")
    log.info("modules: %d", len(mods))

    for q, ins, n in (
        (Q_COLUMNS, "INSERT INTO columns VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", 13),
        (Q_PARAMS, "INSERT INTO params VALUES (?,?,?,?,?,?,?,?,?,?)", 10),
        (Q_FKS, "INSERT INTO fks VALUES (?,?,?,?,?,?,?,?,?,?)", 10),
        (Q_INDEXES, "INSERT INTO indexes VALUES (?,?,?,?,?,?,?,?,?)", 9),
        (Q_DEPS, "INSERT INTO deps VALUES (?,?,?,?,?,?)", 6),
    ):
        rows = cur.execute(q).fetchall()
        if q in (Q_COLUMNS, Q_PARAMS):
            rows = [tuple(r)[:n - 1] + (_desc(r[n - 1]),) for r in rows]
        con.executemany(ins, [tuple(r)[:n] for r in rows])
        log.info("%s: %d", ins.split()[2], len(rows))
    con.commit()

    # Column lineage of every view, one call each so that one failing view does
    # not hide the rest.
    targets = [(r[0], f"select * from [{r[1]}].[{r[2]}]") for r in con.execute(
        "SELECT obj_id, schema_name, name FROM objects WHERE kind='view'")]
    ok = err = 0
    batch = []
    for oid, tsql in targets:
        try:
            rs = cur.execute(Q_RESULTSET, tsql).fetchall()
        except pyodbc.Error as e:
            batch.append((oid, 0, None, None, None, None, None, str(e)[:500]))
            err += 1
            continue
        for r in rs:
            if r[6]:
                batch.append((oid, 0, None, None, None, None, None, r[6][:500]))
                err += 1
                break
            batch.append((oid, r[0], r[1], r[2], int(bool(r[3])), r[4], r[5], None))
        else:
            ok += 1
    con.executemany("INSERT INTO result_sets VALUES (?,?,?,?,?,?,?,?)", batch)
    con.commit()
    sq.close()
    log.info("result sets: %d described, %d not describable", ok, err)
    summary = {k: con.execute(f"SELECT count(*) FROM {k}").fetchone()[0]
               for k in ("objects", "modules", "columns", "params", "fks", "indexes", "deps")}
    summary["result_sets_ok"] = ok
    summary["result_sets_err"] = err
    return summary
