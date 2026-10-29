"""test_sql: run a script for real in the sandbox clone, then roll it back.

The clone (prep/sandbox.py) has appdb' schema and a few rows per table.  Containment,
in depth:
  1. the connection goes to the [sandbox] server and database only (config refuses
     the real database name);
  2. the script runs as the database user sandbox_runner (EXECUTE AS ... WITH COOKIE):
     db_owner inside the clone, no server permissions, no other database; the cookie
     never reaches the script, so it cannot REVERT to the connecting login;
  3. everything runs inside BEGIN TRAN and is always rolled back;
  4. if the script ends that transaction itself (its own COMMIT / ROLLBACK), the clone
     is restored from its baseline snapshot afterwards;
  5. a guard refuses statements aimed outside the clone (USE, other databases, server
     configuration, logins, backup/restore ...).
"""

from __future__ import annotations

import json
import re
import threading
import time
from contextlib import contextmanager
from typing import Optional, Union

from mcp.types import ToolAnnotations

from ..models import SandboxOut, SandboxResult
from .common import Ctx
from .live import _strip_comments, cell, code_only, guard_select, is_timeout, odbc_text, split_batches

_LOCK = threading.Lock()
_INFO: dict = {}

# writes only inside the clone, and every run is rolled back: not destructive, but not read-only
SANDBOX_TOOL = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)

OUTSIDE = re.compile(
    r"\buse\s+\[?\w+\]?|\b(create|alter|drop)\s+(database|login|server|endpoint|credential|certificate|"
    r"asymmetric|master|availability|event\s+session|resource\s+pool|workload\s+group)\b|"
    r"\b(backup|restore|dbcc|shutdown|kill|reconfigure|revert|setuser)\b|\bexecute\s+as\b|\bexec\s+as\b|"
    r"\b(grant|revoke|deny)\b|\bbulk\s+insert\b|\bopen(rowset|datasource|query)\b|"
    r"\bxp_\w+|\bsp_(configure|addlinkedserver|addsrvrolemember|addrolemember|droprolemember|setapprole|"
    r"addlogin|droplogin|adduser|dropuser|change_users_login|start_job|add_job|send_dbmail|OA\w*)\b",
    re.I)
# db.schema.object; a match starts only where a word starts (mid-word starts backtrack quadratically)
THREE_PART_RE = re.compile(r"(?<!\w)\[?(\w+)\]?\s*\.\s*\[?\w*\]?\s*\.\s*\[?\w+")


def guard(sql: str, other_dbs: set[str]) -> str | None:
    code = code_only(sql)
    m = OUTSIDE.search(code)
    if m:
        return f"'{m.group(0).strip()}' is not allowed: test_sql runs only inside the sandbox clone"
    for db in THREE_PART_RE.findall(code):
        if db.lower() in other_dbs:
            return f"'{db}' is another database: test_sql runs only inside the sandbox clone"
    return None


def _msg(e: Exception) -> str:
    return odbc_text(e).replace("(SQLExecDirectW)", "").replace("(SQLMoreResults)", "").strip()[:400]


def sandbox_info(ctx: Ctx) -> dict:
    """The clone's build info (extended property written by prep/sandbox.py), cached 60 s."""
    if _INFO and time.time() - _INFO.get("_t", 0) < 60:
        return _INFO
    import pyodbc
    info: dict = {}
    try:
        con = pyodbc.connect(ctx.cfg.sandbox_connection_string(timeout=ctx.cfg.timeouts.sandbox_status_s),
                             autocommit=True)
        row = con.cursor().execute(
            "select convert(nvarchar(4000), value) from sys.extended_properties "
            "where class = 0 and name = 'advisor_sandbox_info'").fetchone()
        dbs = {r[0].lower() for r in con.cursor().execute("select name from sys.databases")}
        con.close()
        info = json.loads(row[0]) if row else {}
        info["other_dbs"] = (dbs | {ctx.cfg.db.database.lower()}) - {ctx.cfg.sandbox.database.lower()}
    except Exception as e:
        info = {"error": _msg(e)}
    try:
        rep = json.loads(ctx.cfg.paths.sandbox_report.read_text(encoding="utf-8"))
        info["failed_objects"] = [f.split(":", 1)[0].split(" ", 1)[-1] for f in rep.get("failed", [])]
    except Exception:
        info.setdefault("failed_objects", [])
    info["_t"] = time.time()
    _INFO.clear()
    _INFO.update(info)
    return info


# ---------------------------------------------------------------- from_live: bring the target rows
FROM_TABLE_RE = re.compile(r"(?is)^\s*;?\s*select\b.*?\bfrom\s+(?:\[?dbo\]?\.)?\[?(\w+)\]?")


def _q(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


class _Seed:
    """Rows to copy from appdb into the clone for one run: the rows the script targets, their direct
    children (so cascades and blocking foreign keys behave as in production) and every parent row."""

    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.tables: dict[int, dict] = {}            # obj_id -> {name, cols, identity, pk, rows{key: tuple}, keyrole}
        self.notes: list[str] = []
        self.related = 0
        self.targets: list[tuple[str, int]] = []     # (table, rows matched in appdb), one per from_live query
        cols: dict[int, list] = {}
        fks: dict[int, tuple] = {}
        for fk_id, pt, pc, rt, rc in ctx.q("SELECT fk_id, parent_obj_id, parent_column, ref_obj_id, ref_column "
                                           "FROM fks WHERE is_disabled=0 ORDER BY fk_id, ordinal"):
            cols.setdefault(fk_id, []).append((pc, rc))
            fks[fk_id] = (pt, rt)
        # only single-column foreign keys are followed
        self.fks = [{"child": fks[i][0], "parent": fks[i][1], "ccol": c[0][0], "pcol": c[0][1]}
                    for i, c in cols.items() if len(c) == 1]
        # partner tables (same primary key, no declared FK) are followed both ways, like a parent
        from ..prep.sandbox import partner_pairs
        for a, ca, b, cb in partner_pairs(ctx.q):
            oa, ob = ctx.obj(a), ctx.obj(b)
            if oa and ob:
                self.fks.append({"child": oa["obj_id"], "parent": ob["obj_id"], "ccol": ca, "pcol": cb, "partner": True})
                self.fks.append({"child": ob["obj_id"], "parent": oa["obj_id"], "ccol": cb, "pcol": ca, "partner": True})

    def meta(self, oid: int) -> dict:
        if oid not in self.tables:
            o = self.ctx.q1("SELECT name FROM objects WHERE obj_id=?", oid)
            cols = self.ctx.q("SELECT name, is_identity FROM columns WHERE obj_id=? AND coalesce(is_computed,0)=0 "
                              "ORDER BY column_id", oid)
            pk = self.ctx.q1("SELECT key_columns FROM indexes WHERE obj_id=? AND is_primary_key=1", oid)
            self.tables[oid] = {"name": o[0], "cols": [c[0] for c in cols], "identity": any(c[1] for c in cols),
                                "pk": pk[0].split(",") if pk and pk[0] else [], "rows": {}, "keyrole": {}}
        return self.tables[oid]

    def add(self, oid: int, names: list[str], rows: list, role: str) -> list[tuple]:
        """Keep rows (as tuples in the table's column order); returns the new ones."""
        m = self.meta(oid)
        idx = {n.lower(): i for i, n in enumerate(names)}
        new = []
        for r in rows:
            t = tuple(r[idx[c.lower()]] for c in m["cols"])
            key = tuple(t[m["cols"].index(k)] for k in m["pk"]) if m["pk"] else t
            if key not in m["rows"]:
                m["rows"][key] = t
                m["keyrole"][key] = role
                new.append(t)
        return new

    def fetch(self, cur, oid: int, col: str, values: list, cap: int) -> tuple[list[str], list]:
        name = self.meta(oid)["name"]
        out, names = [], []
        for i in range(0, len(values), 500):
            if len(out) >= cap:
                break
            chunk = values[i:i + 500]
            cur.execute(f"select top ({cap - len(out)}) * from {_q(name)} with (nolock) where {_q(col)} in "
                        f"({','.join('?' * len(chunk))})", *chunk)
            names = [d[0] for d in cur.description]
            out += cur.fetchall()
        return names, out

    def plan(self, from_sql: str, max_rows: int, max_related: int) -> str | None:
        """Read everything from appdb (read-only).  Returns an error, or None."""
        why = guard_select(from_sql)
        if why:
            return f"from_live: {why}"
        m = FROM_TABLE_RE.match(_strip_comments(from_sql))
        o = self.ctx.obj(m.group(1)) if m else None
        if o is None or o["kind"] != "table":
            return "from_live must select rows of one table: select * from <Table> where ..."
        from ..prep.catalog import connect
        con = connect(self.ctx.cfg, timeout=self.ctx.cfg.db.preview_timeout_s)
        try:
            cur = con.cursor()
            cur.execute(f"SET ROWCOUNT {max_rows + 1}")
            cur.execute(from_sql)
            names = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchmany(max_rows + 1)
            cur.execute("SET ROWCOUNT 0")
            need = {c.lower() for c in self.meta(o["obj_id"])["cols"]}
            if not need <= {n.lower() for n in names} or len(names) != len({n.lower() for n in names}):
                return (f"from_live must return whole rows of {o['name']} (all its columns, once): "
                        f"select * from {o['name']} where ...")
            if len(rows) > max_rows:
                self.notes.append(f"from_live matched more than {max_rows} rows in appdb: the first {max_rows} "
                                  f"were copied")
                rows = rows[:max_rows]
            seeds = self.add(o["obj_id"], names, rows, "target")
            self.targets.append((o["name"], len(rows)))
            # direct children of the target rows
            pm = self.meta(o["obj_id"])
            for f in self.fks if seeds else []:
                if f["parent"] != o["obj_id"] or f["child"] == o["obj_id"] or f.get("partner"):
                    continue
                vals = list({s[pm["cols"].index(f["pcol"])] for s in seeds} - {None})
                room = max_related - self.related
                if not vals or room <= 0:
                    continue
                n2, got = self.fetch(cur, f["child"], f["ccol"], vals, room)
                self.related += len(self.add(f["child"], n2, got, "child"))
            # parents of everything collected so far, recursively
            frontier = {oid: list(t["rows"].values()) for oid, t in self.tables.items()}
            while frontier and self.related < max_related:
                nxt: dict[int, list] = {}
                for oid, trows in frontier.items():
                    cols = self.meta(oid)["cols"]
                    for f in self.fks:
                        if f["child"] != oid:
                            continue
                        ccol, pcol = f["ccol"], f["pcol"]
                        pm = self.meta(f["parent"])
                        if pm["pk"] != [pcol]:
                            continue
                        have = {k[0] for k in pm["rows"]}
                        vals = list({r[cols.index(ccol)] for r in trows} - {None} - have)
                        room = max_related - self.related
                        if not vals or room <= 0:
                            continue
                        n2, got = self.fetch(cur, f["parent"], pcol, vals, room)
                        new = self.add(f["parent"], n2, got, "partner" if f.get("partner") else "parent")
                        self.related += len(new)
                        if new:
                            nxt.setdefault(f["parent"], []).extend(new)
                frontier = nxt
            if self.related >= max_related:
                self.notes.append(f"related rows capped at {max_related}: some child or parent rows were not copied")
        finally:
            con.close()
        return None

    def apply(self, cur) -> list[str]:
        """Insert the planned rows into the clone (inside the test transaction, as sandbox_runner)."""
        inserted: dict[tuple[str, str], int] = {}    # (role, table for children) -> rows inserted
        enabled: dict[int, tuple[list[str], list[str]]] = {}
        try:
            for oid, t in self.tables.items():          # constraints and triggers off while loading
                name = _q(t["name"])
                cons = [r[0] for r in cur.execute(
                    "select name from sys.foreign_keys where parent_object_id = object_id(?) and is_disabled = 0 "
                    "union all select name from sys.check_constraints where parent_object_id = object_id(?) "
                    "and is_disabled = 0", t["name"], t["name"]).fetchall()]
                trgs = [r[0] for r in cur.execute(
                    "select name from sys.triggers where parent_id = object_id(?) and is_disabled = 0",
                    t["name"]).fetchall()]
                enabled[oid] = (cons, trgs)
                for c in cons:
                    cur.execute(f"alter table {name} nocheck constraint {_q(c)}")
                for g in trgs:
                    cur.execute(f"disable trigger {_q(g)} on {name}")
            for oid, t in self.tables.items():
                name, cols = _q(t["name"]), t["cols"]
                # insert ... select ... where not exists: rowcount is exactly 0 or 1 (an
                # 'if not exists (...) insert' reports another rowcount, so rows already there were counted)
                ins = f"insert into {name} ({', '.join(_q(c) for c in cols)}) select {', '.join('?' * len(cols))}"
                if t["pk"]:
                    ins += (f" where not exists (select 1 from {name} where "
                            + " and ".join(f"{_q(k)} = ?" for k in t["pk"]) + ")")
                if t["identity"]:
                    cur.execute(f"set identity_insert {name} on")
                for key, row in t["rows"].items():
                    cur.execute(ins, *(list(row) + (list(key) if t["pk"] else [])))
                    if cur.rowcount and cur.rowcount > 0:
                        role = t["keyrole"][key]
                        k = role if role == "parent" else t["name"]
                        inserted[(role, k)] = inserted.get((role, k), 0) + 1
                if t["identity"]:
                    cur.execute(f"set identity_insert {name} off")
        finally:
            for oid, (cons, trgs) in enabled.items():
                name = _q(self.tables[oid]["name"])
                for c in cons:
                    cur.execute(f"alter table {name} check constraint {_q(c)}")
                for g in trgs:
                    cur.execute(f"enable trigger {_q(g)} on {name}")
        parts = []
        for tname in dict.fromkeys(t for t, _n in self.targets):
            nseed = sum(n for t, n in self.targets if t == tname)
            got = inserted.get(("target", tname), 0)
            parts.append(f"{nseed} matching {tname} row(s) in appdb; {got} copied into the clone"
                         + (f" ({nseed - got} were already there)" if got != nseed else ""))
        kids = [f"{k} {v}" for (role, k), v in sorted(inserted.items()) if role == "child"]
        if kids:
            parts.append("their child rows: " + ", ".join(kids))
        mates = [f"{k} {v}" for (role, k), v in sorted(inserted.items()) if role == "partner"]
        if mates:
            parts.append("their partner rows (same key, no declared foreign key): " + ", ".join(mates))
        pars = inserted.get(("parent", "parent"), 0)
        if pars:
            parts.append(f"{pars} parent row(s) they need")
        return ["from_live: " + "; ".join(parts)] + self.notes

    def seeded(self) -> set[str]:
        """Tables the run brings rows of (a from_live query that matched nothing seeds nothing)."""
        return {t["name"].lower() for t in self.tables.values() if t["rows"]}

    def empty_targets(self) -> set[str]:
        return {t.lower() for t, n in self.targets if n == 0} - self.seeded()


@contextmanager
def _one_run_at_a_time(ctx: Ctx):
    """One test_sql run (and its reset) at a time across ALL processes using the clone (MCP server, call.py,
    pytest, verify): an exclusive session app lock, held on a connection to master so a snapshot reset
    (which disconnects everyone in the clone) does not drop it.  Yields None when held, else the reason."""
    import pyodbc
    sb, t = ctx.cfg.sandbox, ctx.cfg.timeouts
    wait_s = t.sandbox_lock_wait_s
    try:
        lc = pyodbc.connect(ctx.cfg.sandbox_connection_string(database="master", timeout=t.sandbox_connect_s),
                            autocommit=True)
    except Exception as e:
        yield f"sandbox not reachable: {_msg(e)}"
        return
    resource = f"advisor_test_sql:{sb.database}"
    got = -1
    try:
        lc.timeout = wait_s + 10
        got = lc.cursor().execute(
            "declare @r int; exec @r = sp_getapplock @Resource = ?, @LockMode = 'Exclusive', "
            "@LockOwner = 'Session', @LockTimeout = ?; select @r",
            resource, wait_s * 1000).fetchone()[0]
        yield None if got >= 0 else (f"the sandbox is busy with another test_sql run (waited {wait_s} s); "
                                     f"try again")
    finally:
        # release explicitly: with ODBC pooling close() parks the session, lock still held, and the next
        # run (on another pooled connection) waits 60 s for it
        if got >= 0:
            try:
                lc.cursor().execute("exec sp_releaseapplock @Resource = ?, @LockOwner = 'Session'", resource)
            except Exception:
                pass
        lc.close()


def reset(ctx: Ctx) -> str | None:
    """Revert the clone to its baseline snapshot; None when done, else the error."""
    import pyodbc
    sb, t = ctx.cfg.sandbox, ctx.cfg.timeouts
    try:
        con = pyodbc.connect(ctx.cfg.sandbox_connection_string(database="master", timeout=t.sandbox_connect_s),
                             autocommit=True)
        con.timeout = t.sandbox_reset_s
        cur = con.cursor()
        cur.execute(f"alter database [{sb.database}] set single_user with rollback immediate")
        try:
            cur.execute(f"restore database [{sb.database}] from database_snapshot = '{sb.snapshot_name}'")
        finally:
            cur.execute(f"alter database [{sb.database}] set multi_user")
        con.close()
        return None
    except Exception as e:
        return _msg(e)


SCAN_MAX = 100_000


def _matcher(find: dict[str, str], cols: list[str]) -> Optional[list[tuple[int, re.Pattern]]]:
    """find_rows for one result set: (column index, pattern) per filter, or None when the set lacks
    one of the columns (it is then returned unfiltered).  Values compare as text, case-insensitive,
    '%' matches anything."""
    if not find:
        return None
    idx = {c.lower(): i for i, c in enumerate(cols)}
    if not all(k.lower() in idx for k in find):
        return None
    return [(idx[k.lower()], re.compile(".*".join(re.escape(p) for p in str(v).split("%")), re.I | re.S))
            for k, v in find.items()]


def _scan(cur, n: int, match) -> tuple[list, int, Optional[int], bool]:
    """Read a whole result set (up to SCAN_MAX rows), keeping the first n rows, or the first n that
    match.  Returns (rows kept, rows in the set, rows matched or None, whether the scan was capped)."""
    keep, total, matched = [], 0, (0 if match is not None else None)
    while True:
        chunk = cur.fetchmany(1000)
        if not chunk:
            return keep, total, matched, False
        for r in chunk:
            total += 1
            if match is None:
                if len(keep) < n:
                    keep.append(r)
            elif all(r[i] is not None and rx.fullmatch(str(r[i])) for i, rx in match):
                matched += 1
                if len(keep) < n:
                    keep.append(r)
        if total >= SCAN_MAX:
            return keep, total, matched, True


# rows affected per statement is the point of a test run: SET NOCOUNT ON would hide it
NOCOUNT_RE = re.compile(r"(?i)\bset\s+nocount\s+on\b")
EXEC_RE = re.compile(r"(?i)\bexec(?:ute)?\s+(?:@\w+\s*=\s*)?(?:\[?dbo\]?\.)?\[?(\w+)\]?")
DEFINES_RE = re.compile(r"(?is)^\s*(create|alter)\b")


def _proc_reads(ctx: Ctx, name: str) -> tuple[dict, list[str]] | None:
    """(procedure object, the tables it reads in source order) for a procedure in appdb, else None.  Tables
    come from the parse (aliases of its static and dynamic SQL) and the engine's dependencies."""
    o = ctx.obj(name)
    if not o or o["kind"] != "proc":
        return None
    tabs = [t for (t,) in ctx.q("SELECT a.target FROM sql_aliases a JOIN objects t ON t.name=a.target COLLATE NOCASE "
                                "AND t.kind='table' WHERE a.obj_id=? AND a.target_kind='object' ORDER BY a.line",
                                o["obj_id"])]
    tabs += [t for (t,) in ctx.q("SELECT t.name FROM deps d JOIN objects t ON t.obj_id=d.referenced_id "
                                 "AND t.kind='table' WHERE d.referencing_id=?", o["obj_id"])]
    return o, list(dict.fromkeys(tabs))


def _inner_tables(ctx: Ctx, o, tabs: list[str]) -> set[str]:
    """Lower-cased tables whose missing rows can empty the result: the first table it reads (where it starts)
    and both sides of its inner joins.  Left/full joins and outer apply ('where' joins) are left out."""
    out = {tabs[0].lower()} if tabs else set()
    for lt, rt in ctx.q("SELECT left_table, right_table FROM sql_joins WHERE obj_id=? AND join_type='inner'",
                        o["obj_id"]):
        out |= {lt.lower(), rt.lower()}
    return out


NULL_ARG_RE = re.compile(r"(?i)(@\w+)\s*=\s*null\b")


def _null_params_it_fills(ctx: Ctx, o, code: str) -> list[str]:
    """Parameters the exec (code: the batch without comments) passed as NULL that the procedure assigns itself
    (set @p = / select @p =), such as usp_rpt_orderjournal's @warehouses from fn_userwarehouses(@userid)."""
    d = ctx.q1("SELECT definition FROM modules WHERE obj_id=?", o["obj_id"])
    text = _strip_comments(d[0]) if d else ""
    return [p for p in dict.fromkeys(NULL_ARG_RE.findall(code))
            if re.search(rf"(?i)\b(?:set|select)\s+{re.escape(p)}\s*=", text)]


def _dead_joins(ctx: Ctx, cur, o) -> list[str]:
    """Inner joins of the procedure (static and dynamic SQL) between two tables whose rows in the clone, as
    the run sees them (seeded rows included), have no matching pair: every row is dropped there."""
    proc = o["name"]
    out: list[str] = []
    rows = ctx.q("SELECT DISTINCT j.left_table, j.left_column, j.right_table, j.right_column, j.predicate, j.line "
                 "FROM sql_joins j JOIN objects l ON l.name=j.left_table COLLATE NOCASE AND l.kind='table' "
                 "JOIN objects r ON r.name=j.right_table COLLATE NOCASE AND r.kind='table' "
                 "WHERE j.obj_id=? AND j.join_type='inner' AND l.obj_id<>r.obj_id ORDER BY j.line", o["obj_id"])
    for lt, lc, rt, rc, pred, line in rows[:ctx.cfg.sandbox.max_join_checks]:
        try:
            nl, nr, both = cur.execute(
                f"select (select count(*) from {_q(lt)}), (select count(*) from {_q(rt)}), "
                f"(select count(*) from {_q(lt)} a join {_q(rt)} b on a.{_q(lc)} = b.{_q(rc)})").fetchone()
        except Exception:
            continue
        if nl and nr and not both:
            out.append(f"inner join {pred} ({proc}:{line}): {lt} has {nl} row(s) in the clone, {rt} {nr}, 0 pairs "
                       f"match, so every row is dropped here. Seed the same key in both: from_live=['select * "
                       f"from {lt} where {lc} = X', 'select * from {rt} where {rc} = X'].")
    return out


MISSING_OBJ_RE = re.compile(r"Invalid object name '(\w+)'")
BROKER_RE = re.compile(r"(?i)service|dialog|conversation")


def _missing_object_hints(ctx: Ctx, errors: list[str], sql: str) -> list[str]:
    """'Invalid object name X' where X is no object of appdb: a trigger of the clone names it (typically a Service
    Broker service the clone does not have).  Names the triggers and the statements that switch them off."""
    import pyodbc
    names = {m.group(1) for e in errors for m in MISSING_OBJ_RE.finditer(e) if not ctx.obj(m.group(1))}
    if not names:
        return []
    out: list[str] = []
    try:
        con = pyodbc.connect(ctx.cfg.sandbox_connection_string(timeout=ctx.cfg.timeouts.sandbox_connect_s),
                             autocommit=True)
        try:
            for n in sorted(names):
                rows = con.cursor().execute(
                    "select t.name, p.name, m.definition from sys.triggers t join sys.objects p on p.object_id = t.parent_id "
                    "join sys.sql_modules m on m.object_id = t.object_id where t.is_disabled = 0 "
                    "and m.definition like ?", f"%{n}%").fetchall()
                if not rows:
                    continue
                # the triggers on tables the script touches first: those are the ones that fire
                rows = sorted(rows, key=lambda r: not re.search(r"\b" + re.escape(r[1]) + r"\b", sql, re.I))
                broker = any(BROKER_RE.search(r[2] or "") for r in rows)
                stmts = "; ".join(f"disable trigger {r[0]} on {r[1]}" for r in rows[:4])
                out.append(f"'{n}' is named in trigger(s) {', '.join(r[0] for r in rows[:4])}"
                           + (": a Service Broker service, which the clone does not have" if broker else
                              ": an object the clone does not have")
                           + f". Statements that fire the trigger fail here. Put this before them in your script: {stmts} "
                             "(the run is rolled back, so nothing stays disabled).")
        finally:
            con.close()
    except Exception:
        pass
    return out


def _empty_exec_hints(ctx: Ctx, cur, bi: int, batch: str, sets: int, seed: Optional[_Seed]) -> list[str]:
    """A batch that executed a procedure returned nothing: say why that is not proof, which tables to seed,
    and which of its inner joins match no rows in the clone."""
    out = []
    have = seed.seeded() if seed is not None else set()
    empty = seed.empty_targets() if seed is not None else set()
    code = _strip_comments(batch)
    for m in EXEC_RE.finditer(code):
        r = _proc_reads(ctx, m.group(1))
        if not r:
            continue
        o, tabs = r
        proc = o["name"]
        got = "no result set" if not sets else "only empty result sets"
        text = (f"batch {bi} executed {proc} and got {got}. That does not show the procedure works: it may have "
                "built its dynamic SQL as NULL (one NULL piece in the concatenation makes it run nothing), or its "
                "filters match none of the clone's sample rows.")
        dead = _dead_joins(ctx, cur, o)
        unseeded = [t for t in tabs if t.lower() not in have]
        nothing = [t for t in unseeded if t.lower() in empty]
        if nothing:
            text += (f" It reads {', '.join(tabs[:8])}. from_live matched 0 rows of {', '.join(nothing[:8])} in "
                     f"appdb, so nothing was copied for {'it' if len(nothing) == 1 else 'them'}: that row is not "
                     "there (a table filled from another one, such as ReportOrderTable from OrderTable, can lag "
                     "it). Check the key with preview_query, pick a key that exists in both, or insert the row in "
                     "your script.")
            unseeded = [t for t in unseeded if t not in nothing]
        # missing rows of an outer-joined table drop nothing: only the table it starts from and inner joins count
        needed = _inner_tables(ctx, o, tabs)
        outer = [t for t in unseeded if t.lower() not in needed]
        unseeded = [t for t in unseeded if t.lower() in needed]
        if unseeded:
            text += (f" It reads {', '.join(tabs[:8])}; from_live did not seed {', '.join(unseeded[:8])}. Seed the "
                     f"table it starts from, e.g. from_live=['select * from {unseeded[0]} where ...'].")
        elif tabs and not dead and not nothing:
            text += (f" It reads {', '.join(tabs[:8])}; the table it starts from and its inner joins are seeded by "
                     "from_live and find matching rows"
                     + (f" ({', '.join(outer[:8])} {'is' if len(outer) == 1 else 'are'} only outer-joined, so "
                        "missing rows there drop nothing)" if outer else "")
                     + ": check its parameters (user, warehouses, dates) against the seeded rows.")
        filled = _null_params_it_fills(ctx, o, code)
        if filled:
            text += (f" {', '.join(filled)} {'was' if len(filled) == 1 else 'were'} passed as NULL, and {proc} then "
                     f"fills {'it' if len(filled) == 1 else 'them'} itself (set/select): from the user's data, that "
                     "can come back NULL or empty in the clone. Pass a value seeded in the clone instead.")
        out.append(text)
        out += dead
    return out


def test(ctx: Ctx, sql: str, max_rows: Optional[int], from_live: str | list[str] | None = None,
         draft_id: Optional[str] = None, find_rows: Optional[dict] = None) -> SandboxOut:
    find = {str(k): str(v) for k, v in (find_rows or {}).items()}
    sb = ctx.cfg.sandbox
    scope = f"sandbox clone '{sb.database}' on {sb.server} (rolled back after the run)"

    def refuse(status: str, *caveats: str) -> SandboxOut:
        return SandboxOut(status=status, layer="sandbox", searched_scope=scope, caveats=list(caveats))
    from .drafts import combine
    sql, _only, draft_notes, derr, _exempt = combine(draft_id, sql)
    if derr:
        return refuse("error", derr)
    if not sb.enabled:
        return refuse("unavailable", "The sandbox is not configured ([sandbox] enabled = false). Use validate_sql.")
    info = sandbox_info(ctx)
    if "error" in info:
        return refuse("unavailable", f"sandbox not reachable: {info['error']}", "Build it with scripts/sandbox.ps1.")
    if not info.get("built_at"):
        return refuse("unavailable", "the sandbox database has not been built: run scripts/sandbox.ps1")
    why = guard(sql, info.get("other_dbs", set()))
    if not why and ctx.mentions_masked(sql):
        why = "this object is not available in this index snapshot"
    if why:
        return refuse("error", why)
    batches = split_batches(sql)
    hid_counts = any(NOCOUNT_RE.search(_strip_comments(b)) for b in batches)
    batches = [NOCOUNT_RE.sub("set nocount off", b) for b in batches]
    if not batches:
        return refuse("no_results", "empty script")
    n = max(1, min(max_rows or 10, sb.result_max_rows))
    seed = None
    queries = [from_live] if isinstance(from_live, str) else list(from_live or [])
    queries = [q for q in queries if q and q.strip()]
    if len(queries) > sb.max_from_live:
        return refuse("error", f"from_live accepts at most {sb.max_from_live} SELECTs (got {len(queries)})")
    if queries:
        if any(ctx.mentions_masked(q) for q in queries):
            return refuse("error", "this object is not available in this index snapshot")
        seed = _Seed(ctx)
        for qi, q in enumerate(queries, 1):
            at = f" (query {qi})" if len(queries) > 1 else ""
            try:
                err = seed.plan(q, sb.seed_max_rows, sb.seed_max_related)
            except Exception as e:      # appdb could not be read: not the request's fault
                return refuse("unavailable", f"from_live{at}: reading appdb failed: {_msg(e)}")
            if err:
                return refuse("error", err.replace("from_live", "from_live" + at, 1))

    import pyodbc
    results: list[SandboxResult] = []
    events: list[str] = []
    errors: list[str] = []
    hints: list[str] = []
    escaped, reset_err, did_reset = False, None, False
    t0 = time.time()
    with _LOCK, _one_run_at_a_time(ctx) as busy:
        if busy:
            return refuse("unavailable", busy)
        try:
            con = pyodbc.connect(ctx.cfg.sandbox_connection_string(timeout=ctx.cfg.timeouts.sandbox_connect_s),
                                 autocommit=True)
        except Exception as e:
            return refuse("unavailable", f"sandbox not reachable: {_msg(e)}")
        con.timeout = sb.run_timeout_s
        cur = con.cursor()
        try:
            cookie = cur.execute(
                f"set nocount on; declare @c varbinary(8000); execute as user = '{sb.runner}' with cookie into @c; "
                "select @c").fetchone()[0]
            cur.execute(f"set lock_timeout {int(ctx.cfg.timeouts.sandbox_lock_timeout_ms)}; set xact_abort off; "
                        "set nocount off; begin tran")
            if seed is not None:
                try:
                    events.extend(seed.apply(cur))
                except pyodbc.Error as e:
                    errors.append(f"from_live: copying the rows into the clone failed: {_msg(e)}")
                    batches = []
            for bi, b in enumerate(batches, 1):
                sets, affected, empty_sets = 0, 0, 0
                try:
                    cur.execute(b)
                    while True:
                        if cur.description is not None:
                            cols = [d[0] for d in cur.description]
                            rows, total, matched, capped = _scan(cur, n, _matcher(find, cols))
                            sets += 1
                            empty_sets += not total
                            more = (matched if matched is not None else total) > len(rows) or capped
                            results.append(SandboxResult(
                                batch=bi, columns=cols, rows=[[cell(v) for v in r] for r in rows],
                                more_rows=more, row_count=len(rows), total_rows=total, matched_rows=matched))
                            of = f"{total}{'+' if capped else ''}"
                            if matched is not None:
                                events.append(f"batch {bi}: result set {len(results)}: {matched} of {of} rows match "
                                              + ", ".join(f"{k} = {v}" for k, v in find.items()))
                            else:
                                events.append(f"batch {bi}: result set {len(results)} ("
                                              + (f"{len(rows)} of {of}" if more else f"{len(rows)}") + " rows)")
                        elif cur.rowcount is not None and cur.rowcount >= 0:
                            affected += cur.rowcount
                            events.append(f"batch {bi}: ({cur.rowcount} row{'s' if cur.rowcount != 1 else ''} affected)")
                        for _st, text in (getattr(cur, "messages", None) or []):
                            t = odbc_text(text)
                            if t:
                                events.append(f"batch {bi}: message: {t[:300]}")
                        if not cur.nextset():
                            break
                except pyodbc.Error as e:
                    msg = _msg(e)
                    if is_timeout(msg, e):
                        msg = f"exceeded {sb.run_timeout_s}s"
                    errors.append(f"batch {bi}: {msg}")
                    events.append(f"batch {bi}: ERROR {msg}")
                else:
                    events.append(f"batch {bi}: done: {sets} result set(s), {affected} row(s) affected"
                                  + (" -- nothing returned or affected" if not sets and not affected else ""))
                    if sets == empty_sets and not DEFINES_RE.match(_strip_comments(b)):
                        hints.extend(_empty_exec_hints(ctx, cur, bi, b, sets, seed))
                tc = cur.execute("select @@trancount").fetchone()[0]
                if tc == 0:
                    escaped = True
                    events.append(f"batch {bi}: the script ended the test transaction (its own COMMIT/ROLLBACK); "
                                  "the clone will be reset from its snapshot")
                    cur.execute("begin tran")
        finally:
            try:
                while cur.execute("select @@trancount").fetchone()[0] > 0:
                    cur.execute("rollback")
            except Exception:
                escaped = True
            try:
                cur.execute("revert with cookie = ?", cookie)
            except Exception:
                pass
            con.close()
            if escaped:
                reset_err = reset(ctx)
                did_reset = reset_err is None
    caveats = [f"Ran in the sandbox clone (~{sb.rows_per_table} rows per table, built {info.get('built_at')} "
               f"from appdb as of {info.get('source_snapshot_at')}) and rolled back: nothing was kept. "
               "Rows affected are the clone's, not production's; use preview_query for production counts."]
    caveats += [c.split("; style notes")[0] + "." for c in draft_notes]
    if hid_counts:
        caveats.append("SET NOCOUNT ON was switched to OFF for this test run so rows affected could be reported, "
                       "including inside procedures the script creates; your script text is unchanged.")
    if seed is not None:
        caveats.append(f"from_live copied real appdb rows into the clone for this run only (up to "
                       f"{sb.seed_max_rows} target rows per query, their child rows and the parent rows they need). "
                       f"Rows of other tables are still the clone's sample.")
    caveats += hints
    caveats += _missing_object_hints(ctx, errors, sql)
    if find and all(r.matched_rows is None for r in results):
        caveats.append(f"find_rows {sorted(find)}: no result set has all of these columns, so nothing was "
                       "filtered. Columns per result set: "
                       + "; ".join(f"{i}: {', '.join(r.columns[:40])}" for i, r in enumerate(results, 1)))
    missing = [o for o in info.get("failed_objects", []) if re.search(rf"\b{re.escape(o)}\b", sql, re.I)]
    if missing:
        caveats.append("Not in the clone (they failed to build; broken in appdb too): " + ", ".join(missing))
    if escaped:
        caveats.append("The test transaction ended before the run was over (a COMMIT/ROLLBACK in the script"
                       + (", or the run-time error above that aborted it" if errors else "") + "). "
                       + ("The clone was reset from its snapshot." if did_reset else
                          f"Resetting the clone FAILED ({reset_err}): rebuild it with scripts/sandbox.ps1."))
    return SandboxOut(status="ok", layer="sandbox", searched_scope=scope, ok=not errors, errors=errors,
                      events=events[:200], result_sets=results[:10], truncated=len(events) > 200 or len(results) > 10,
                      reset=did_reset, elapsed_ms=int((time.time() - t0) * 1000), caveats=caveats)


def register(mcp, ctx: Ctx) -> None:
    sb = ctx.cfg.sandbox

    @mcp.tool(name="test_sql", annotations=SANDBOX_TOOL, description=(
        f"Test a script FOR REAL in the sandbox clone of appdb (same schema, ~{sb.rows_per_table} rows per table), "
        "inside a transaction that is always rolled back. Use it after validate_sql for change scripts and procedures: "
        "UPDATE/DELETE/INSERT/MERGE, DDL, and EXEC of procedures (create or alter one in the script, then exec "
        "it). Returns, per batch, rows affected, messages, run-time errors (foreign key, duplicate key, "
        "conversion, trigger) and the first rows of any result set; add a SELECT after the change to see its "
        "effect. Row counts are the clone's, not production's. Statements aimed outside the clone (USE, other "
        "databases, server settings, logins, backup) are refused. "
        f"The clone holds only the newest ~{sb.rows_per_table} rows per "
        "table: when the script targets older rows, pass from_live = a SELECT of those rows from appdb "
        "(e.g. 'select * from IssueTable where ProductTable_Id is null'), "
        f"or a list of up to {sb.max_from_live} such SELECTs when "
        "the script reads several tables (seed the table a procedure starts from, not only a related one). Up to "
        f"{sb.seed_max_rows} matching rows per query, their child rows and the parent rows they need are copied "
        "into the clone first, inside the same rolled-back run, together with partner rows of tables that share their key "
        "without a foreign key (ReportOrderTable / OrderTable). Each batch ends with a 'done:' event counting "
        "its result sets and rows affected; an exec that returns nothing names the inner join that matched no "
        "rows. draft_id (from draft_change) runs that draft as the first batch, then sql (e.g. 'exec ...'). "
        "Every result set is counted in full (total_rows); find_rows = {column: value} returns only the "
        "rows of a result set that match ('%' wildcard, case-insensitive), e.g. the row your script seeded."))
    def test_sql(sql: str = "", max_rows: Optional[int] = None,
                 from_live: Union[str, list[str], None] = None, draft_id: Optional[str] = None,
                 find_rows: Optional[dict[str, str]] = None) -> SandboxOut:
        return test(ctx, sql, max_rows, from_live, draft_id, find_rows)
