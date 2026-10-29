"""Phase link: deterministic merge of DB, data and code facts.

Builds:
  value_maps / value_map_items  every value->label list from every source
  column_value_maps             which column (or sproc parameter) uses which map, how we know
  conflicts                     sources that disagree; observed values no source explains
  relationships                 declared FKs + joins mined from modules + description links + naming
  column_notes                  currency pairs (X / XLocal), date-only copies, synced fields
  ui_targets                    screen/report -> SQL object
  code_sql_refs                 tables/columns/literals referenced by SQL text in C#
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections import defaultdict

from ..config import Config
from .. import db as dbm

log = logging.getLogger("prep.link")

TABLES = ["value_maps", "value_map_items", "column_value_maps", "conflicts", "relationships",
          "column_notes", "ui_targets", "code_sql_refs"]

PAIR_RE = re.compile(r"(?:^|[,;(:]|\s)\s*(-?\d{1,4}|[A-Z])\s*(?:=|:|,(?=\s*[A-Za-z]))\s*([^,;()=]{1,60})")
LINK_RE = re.compile(r"link to\s+\[?(\w+)\]?(?:\s*\(([^)]*)\))?", re.I)


def parse_value_pairs(desc: str | None) -> list[tuple[str, str]]:
    if not desc or ("=" not in desc and ":" not in desc):
        return []
    pairs = [(m.group(1), m.group(2).strip().rstrip(".").strip()) for m in PAIR_RE.finditer(desc)]
    pairs = [(v, l) for v, l in pairs if l and not l.lower().startswith(("link", "see "))]
    vals = [v for v, _ in pairs]
    if len(pairs) < 2 or len(set(vals)) != len(vals):
        return []
    return pairs


class Names:
    """Case-insensitive object/column lookups over the catalog."""

    def __init__(self, con: sqlite3.Connection):
        self.obj = {}
        self.kind = {}
        for oid, name, kind in con.execute("SELECT obj_id, name, kind FROM objects"):
            self.obj.setdefault(name.lower(), (oid, name))
            self.kind[name.lower()] = kind
        self.cols: dict[str, dict[str, str]] = defaultdict(dict)
        for name, col in con.execute("SELECT o.name, c.name FROM columns c JOIN objects o ON o.obj_id=c.obj_id"):
            self.cols[name.lower()][col.lower()] = col
        self.pk: dict[str, str] = {}
        for name, keys in con.execute(
                "SELECT o.name, i.key_columns FROM indexes i JOIN objects o ON o.obj_id=i.obj_id WHERE i.is_primary_key=1"):
            if keys and "," not in keys:
                self.pk[name.lower()] = keys

    def table(self, name: str | None) -> str | None:
        if not name:
            return None
        hit = self.obj.get(name.lower())
        return hit[1] if hit else None

    def column(self, table: str, col: str) -> str | None:
        return self.cols.get(table.lower(), {}).get(col.lower())

    def is_table(self, name: str) -> bool:
        return self.kind.get(name.lower()) == "table"


# ---------------------------------------------------------------------------
def _value_maps(con: sqlite3.Connection, nm: Names) -> dict:
    maps_by_name: dict[str, list[int]] = defaultdict(list)

    def new_map(name, source, ref, rel, line, side, items):
        mid = con.execute("INSERT INTO value_maps(name,source,ref,rel_path,line,side) VALUES (?,?,?,?,?,?)",
                          (name, source, ref, rel, line, side)).lastrowid
        con.executemany("INSERT INTO value_map_items VALUES (?,?,?,?,?)",
                        [(mid, str(v), lab, r, ln) for v, lab, r, ln in items])
        return mid

    # 1. DB descriptions
    n_desc = 0
    for tname, cname, desc in con.execute(
            "SELECT o.name, c.name, c.description FROM columns c JOIN objects o ON o.obj_id=c.obj_id "
            "WHERE o.kind='table' AND c.description IS NOT NULL").fetchall():
        pairs = parse_value_pairs(desc)
        if pairs:
            mid = new_map(f"{tname}.{cname}", "db_description", f"{tname}.{cname}", None, None, None,
                          [(v, l, None, None) for v, l in pairs])
            con.execute("INSERT INTO column_value_maps VALUES (?,?,?,?,?,?)",
                        (tname, cname, mid, "MS_Description of the column", f"{tname}.{cname} MS_Description", "exact"))
            n_desc += 1

    # 2. code enums / value enums / memtypes / const groups
    for eid, name, fqn, kind, side, rel, line in con.execute(
            "SELECT enum_id,name,fqn,kind,side,rel_path,line FROM code_enums").fetchall():
        items = [(v, lab, r, ln) for v, lab, r, ln in con.execute(
            "SELECT value, label, rel_path, line FROM code_enum_items WHERE enum_id=? AND value<>'helper'", (eid,))]
        if not items:
            continue
        src = {"enum": "code_enum", "value_enum": "code_enum", "memtype": "memtype", "const_group": "const_group"}[kind]
        mid = new_map(name, src, fqn, rel, line, side, items)
        maps_by_name[name.lower()].append(mid)
        if name.endswith("Enum"):
            maps_by_name[name[:-4].lower()].append(mid)

    # 3. lookup tables (rows of small reference tables)
    have_lookup = con.execute("SELECT 1 FROM sqlite_master WHERE name='lookup_rows'").fetchone()
    lookup_map: dict[str, int] = {}
    if have_lookup:
        for oid, tname in con.execute(
                "SELECT DISTINCT l.obj_id, o.name FROM lookup_rows l JOIN objects o ON o.obj_id=l.obj_id").fetchall():
            rows = con.execute("SELECT key, label, extra FROM lookup_rows WHERE obj_id=? ORDER BY rowid", (oid,)).fetchall()
            mid = new_map(tname, "lookup_table", tname, None, None, None,
                          [(k, (lab or "") + (f" [{ex}]" if ex else ""), None, None) for k, lab, ex in rows])
            lookup_map[tname.lower()] = mid
    # 4. curated (Haiku, citation-verified) value labels -> one map per column
    by_col = defaultdict(list)
    for subj, text, cites, value in con.execute(
            "SELECT subject, text, citations, value FROM curated WHERE fact_type='value_label' AND value IS NOT NULL"):
        by_col[subj].append((value, text, json.loads(cites)))
    for subj, items in by_col.items():
        tname, _, cname = subj.rpartition(".")
        t = nm.table(tname)
        c = nm.column(t, cname) if t else None
        if not t or not c:
            continue
        mid = new_map(f"{t}.{c}", "curated", subj, None, None, None,
                      [(v, lab, cites[0].rsplit(":", 1)[0] if ":" in cites[0] else None,
                        int(cites[0].rsplit(":", 1)[1]) if ":" in cites[0] and cites[0].rsplit(":", 1)[1].isdigit() else None)
                       for v, lab, cites in items])
        con.execute("INSERT INTO column_value_maps VALUES (?,?,?,?,?,?)",
                    (t, c, mid, "curated from code/SQL by Haiku; every label citation-verified",
                     "; ".join(dict.fromkeys(ci for _, _, cs in items for ci in cs[:1]))[:400], "curated"))
    return {"maps_by_name": maps_by_name, "lookup_map": lookup_map, "desc_maps": n_desc}


def _entity_tables_from_sql(con: sqlite3.Connection, nm: Names) -> dict[tuple[str, str], str]:
    """Column reads in classes that declare no TableName (OrderMasterEntity.OrderFeedbackData) get their
    table from the class's own SQL strings: when exactly one table those strings name has the column.
    Fills code_entity_columns.table_name; returns {(class, column): the SQL table} for the evidence."""
    # the code phase sets table_name only for classes with an entity record: anything else was filled here
    # by an earlier link run, so start again from the code phase's state
    con.execute("UPDATE code_entity_columns SET table_name=NULL WHERE table_name IS NOT NULL AND class_fqn NOT IN "
                "(SELECT class_fqn FROM code_entities)")
    tables_of: dict[str, set[str]] = defaultdict(set)
    for container, text in con.execute("SELECT container, sql_text FROM code_sql WHERE container LIKE '%.%'"):
        cls = container.rsplit(".", 1)[0]
        for m in ALIAS_RE.finditer(text):
            t = nm.table(m.group(2))
            if t and nm.is_table(t):
                tables_of[cls].add(t)
    found: dict[tuple[str, str], str] = {}
    for cls, col in con.execute("SELECT DISTINCT class_fqn, column FROM code_entity_columns "
                                "WHERE table_name IS NULL AND class_fqn <> ''").fetchall():
        hits = [t for t in tables_of.get(cls, ()) if nm.column(t, col)]
        if len(hits) == 1:
            found[(cls, col)] = hits[0]
    con.executemany("UPDATE code_entity_columns SET table_name=? WHERE class_fqn=? AND column=? AND table_name IS NULL",
                    [(t, cls, col) for (cls, col), t in found.items()])
    log.info("entity reads given their table from the class's SQL: %d (class, column) pairs", len(found))
    return found


def _column_links(con: sqlite3.Connection, nm: Names, maps_by_name, lookup_map,
                  inferred: dict[tuple[str, str], str] | None = None) -> int:
    seen = set()
    n = 0
    inferred = inferred or {}

    def link(tbl, col, mid, via, evidence, conf):
        nonlocal n
        key = (tbl.lower(), col.lower(), mid)
        if key in seen:
            return
        seen.add(key)
        con.execute("INSERT INTO column_value_maps VALUES (?,?,?,?,?,?)", (tbl, col, mid, via, evidence, conf))
        n += 1

    # a) entity reads converted through an enum / value-enum type
    for cls, tbl, col, vtype, rel, line in con.execute(
            "SELECT class_fqn, table_name, column, value_type, rel_path, line FROM code_entity_columns "
            "WHERE access='read' AND value_type IS NOT NULL").fetchall():
        typ = re.sub(r"\.(resolve|FromValue|Parse|FromByte|Get)$", "", vtype.replace("new ", ""), flags=re.I)
        typ = typ.split(".")[-1].split("<")[0].rstrip("?")
        mids = maps_by_name.get(typ.lower())
        if not mids:
            continue
        t = nm.table(tbl) if tbl else None
        c = nm.column(t, col) if t else None
        via_sql = (cls, col) in inferred
        for mid in mids:
            if t and c:
                if via_sql:
                    link(t, c, mid, f"entity reads the column as {typ} (class {cls.split('.')[-1]} declares no "
                         f"table; its own SQL reads {t})", f"{rel}:{line}", "strong")
                else:
                    link(t, c, mid, f"entity reads the column as {typ}", f"{rel}:{line}", "exact")

    # b) MasterView columns with MemValues, traced through the view's lineage to the base column
    lineage = _view_lineage(con)
    for view, field, memv, rel, line in con.execute(
            "SELECT v.object_name, c.field_name, c.mem_values, c.rel_path, c.line FROM ui_view_columns c "
            "JOIN ui_views v ON v.view_name=c.view_name AND v.side=c.side AND v.rel_path=c.rel_path "
            "WHERE c.mem_values IS NOT NULL AND v.object_name IS NOT NULL").fetchall():
        mids = maps_by_name.get(memv.strip('"').lower())
        if not mids:
            continue
        for bt, bc in lineage.get((view.lower(), field.lower()), []):
            for mid in mids:
                link(bt, bc, mid, f"grid column {view}.{field} shows it with MemValues={memv}", f"{rel}:{line}", "strong")
        vt = nm.table(view)
        vc = nm.column(vt, field) if vt else None
        if vt and vc:
            for mid in mids:
                link(vt, vc, mid, f"grid column shows it with MemValues={memv}", f"{rel}:{line}", "exact")

    # c) report filters with a MemType -> stored procedure parameter
    for obj, fname, memt, rel, line in con.execute(
            "SELECT r.object_name, f.filter_name, f.mem_type, f.rel_path, f.line FROM ui_report_filters f "
            "JOIN ui_reports r ON r.report_name=f.report_name AND r.rel_path=f.rel_path "
            "WHERE f.mem_type IS NOT NULL AND r.object_name IS NOT NULL").fetchall():
        mids = maps_by_name.get(memt.strip('"').lower())
        p = nm.table(obj)
        if not mids or not p or not fname:
            continue
        for part in fname.split("#"):
            for mid in mids:
                link(p, "@" + part, mid, f"report filter '{fname}' uses MemType {memt}", f"{rel}:{line}", "exact")

    # d) name match: column name == enum/value-enum/memtype name (lower confidence)
    for tname, cname in con.execute(
            "SELECT o.name, c.name FROM columns c JOIN objects o ON o.obj_id=c.obj_id "
            "WHERE o.kind='table' AND c.type_name IN ('tinyint','smallint','int','char','bit','varchar','nchar')").fetchall():
        for mid in maps_by_name.get(cname.lower(), []):
            link(tname, cname, mid, f"column name equals the code type name {cname}", None, "name_match")

    # e) FK (declared) or *_Id naming to a snapshotted lookup table
    for pt, pc, rt in con.execute(
            "SELECT p.name, f.parent_column, r.name FROM fks f JOIN objects p ON p.obj_id=f.parent_obj_id "
            "JOIN objects r ON r.obj_id=f.ref_obj_id").fetchall():
        mid = lookup_map.get(rt.lower())
        if mid:
            link(pt, pc, mid, f"foreign key to lookup table {rt}", None, "exact")
    for tname, cname in con.execute(
            "SELECT o.name, c.name FROM columns c JOIN objects o ON o.obj_id=c.obj_id "
            "WHERE o.kind='table' AND c.name LIKE '%Table_Id'").fetchall():
        target = cname[:-3]
        mid = lookup_map.get(target.lower())
        if mid:
            link(tname, cname, mid, f"naming convention {target}_Id -> lookup table {target}", None, "strong")

    # f) a column with no exact/strong list borrows the DB-description list of a same-named column in
    #    another table, when every value it holds is in that list (OrderTable.OrderKind <- OrderItemTable)
    decoded = {(r[0].lower(), r[1].lower()) for r in con.execute(
        "SELECT tbl, col FROM column_value_maps WHERE confidence IN ('exact','strong')")}
    desc_maps = defaultdict(list)
    for mid, ref in con.execute("SELECT map_id, ref FROM value_maps WHERE source='db_description'"):
        t2, _, c2 = ref.rpartition(".")
        desc_maps[c2.lower()].append((t2, mid, {v for (v,) in con.execute(
            "SELECT value FROM value_map_items WHERE map_id=?", (mid,))}))
    for tname, cname, top in con.execute(
            "SELECT o.name, c.name, p.top_values FROM columns c JOIN objects o ON o.obj_id=c.obj_id "
            "JOIN profiles p ON p.obj_id=c.obj_id AND p.column_id=c.column_id "
            "WHERE o.kind='table' AND p.top_values IS NOT NULL").fetchall():
        if (tname.lower(), cname.lower()) in decoded or cname.lower() not in desc_maps:
            continue
        observed = {str(v) for v, _n in json.loads(top) if v is not None}
        for t2, mid, vals in desc_maps[cname.lower()]:
            if t2.lower() != tname.lower() and observed and observed <= vals:
                link(tname, cname, mid, f"same-named column {t2}.{cname} is described in the DB and every value "
                     f"{tname}.{cname} holds is in that list", f"{t2}.{cname} MS_Description", "strong")
                break
    return n


def _row_kinds(con: sqlite3.Connection, nm: Names) -> int:
    """Tables whose rows are of several kinds (OrderTable: quotes and orders by OrderKind).
    Counting such a table without filtering on the kind column mixes them -- say so."""
    n = 0
    src_rank = {"db_description": 0, "memtype": 1, "code_enum": 2, "curated": 3, "const_group": 4}
    conf_rank = {"exact": 0, "strong": 1, "curated": 2, "name_match": 3}
    rows = con.execute("""SELECT o.name, c.name, p.top_values, p.rows_scanned FROM columns c
                          JOIN objects o ON o.obj_id=c.obj_id
                          JOIN profiles p ON p.obj_id=c.obj_id AND p.column_id=c.column_id
                          WHERE o.kind='table' AND c.name LIKE '%Type' AND p.top_values IS NOT NULL
                            AND p.distinct_count BETWEEN 2 AND 10 AND p.rows_scanned > 0""").fetchall()
    for tname, cname, top, total in rows:
        vals = [(str(v), cnt) for v, cnt in json.loads(top) if v is not None]
        big = [(v, cnt) for v, cnt in vals if cnt >= 0.01 * total]
        if len(big) < 2:
            continue
        maps = con.execute("""SELECT vm.map_id, vm.source, cvm.confidence FROM column_value_maps cvm
                              JOIN value_maps vm ON vm.map_id=cvm.map_id
                              WHERE cvm.tbl=? COLLATE NOCASE AND cvm.col=? COLLATE NOCASE""", (tname, cname)).fetchall()
        if not maps:
            continue
        maps = sorted(maps, key=lambda m: (conf_rank.get(m[2], 9), src_rank.get(m[1], 9)))
        labels: dict[str, str] = {}
        for mid, _src, _conf in maps:
            for v, lab in con.execute("SELECT value, label FROM value_map_items WHERE map_id=?", (mid,)):
                labels.setdefault(str(v), lab)
        if not any(v in labels for v, _ in big):
            continue
        kinds = ", ".join(f"{v}={labels.get(v, '?')} ({cnt:,} rows)" for v, cnt in vals)
        con.execute("INSERT INTO column_notes VALUES (?,?,?,?,?)", (
            tname, "*", "row_kind",
            f"{tname} rows are of several kinds by {cname}: {kinds}. Filter on {cname} when counting or "
            f"listing {tname} rows, or the kinds are mixed.",
            f"data profile + value list of {tname}.{cname}"))
        n += 1
        status = nm.column(tname, "Status")
        if status:
            extra = ""
            if cname.lower() == "usagetype" and nm.column(tname, "UsageStatus"):
                extra = f" {tname}.UsageStatus combines both (= OrderKind * 10 + Status)."
            con.execute("INSERT INTO column_notes VALUES (?,?,?,?,?)", (
                tname, status, "companion",
                f"Read {status} together with {cname}: the same {status} value applies to every kind of row "
                f"({', '.join(f'{v}={labels.get(v, chr(63))}' for v, _ in vals)}), e.g. an 'active' row may be a "
                f"quote rather than an order.{extra}",
                f"{tname}.{cname} row kinds"))
            n += 1
    return n


def _view_lineage(con: sqlite3.Connection) -> dict[tuple[str, str], list[tuple[str, str]]]:
    """(view, column) -> base (table, column) list, from the engine's own description
    first, then from the parse (resolved transitively through views/ctes)."""
    out: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for v, col, st, sc in con.execute(
            "SELECT o.name, r.name, r.source_table, r.source_column FROM result_sets r "
            "JOIN objects o ON o.obj_id=r.obj_id WHERE r.source_table IS NOT NULL AND r.name IS NOT NULL"):
        out[(v.lower(), col.lower())].append((st, sc))
    for v, name, sources in con.execute(
            "SELECT o.name, s.out_name, s.sources FROM sql_outputs s JOIN objects o ON o.obj_id=s.obj_id "
            "WHERE o.kind='view' AND s.owner='result' AND s.out_name IS NOT NULL"):
        key = (v.lower(), name.lower())
        if key in out:
            continue
        srcs = json.loads(sources)
        if len(srcs) == 1 and srcs[0][2] == "object" and srcs[0][0]:
            out[key].append((srcs[0][0], srcs[0][1]))
    return out


def _conflicts(con: sqlite3.Connection, nm: Names) -> int:
    n = 0
    links = defaultdict(list)
    for tbl, col, mid, via, conf in con.execute(
            "SELECT tbl, col, map_id, via, confidence FROM column_value_maps WHERE col NOT LIKE '@%'"):
        links[(tbl, col)].append((mid, via, conf))
    for (tbl, col), ms in links.items():
        maps = {}
        for mid, via, conf in ms:
            src, name, rel, side = con.execute(
                "SELECT source, name, rel_path, side FROM value_maps WHERE map_id=?", (mid,)).fetchone()
            if src == "lookup_table":
                continue
            vals = {v for (v,) in con.execute("SELECT value FROM value_map_items WHERE map_id=?", (mid,))}
            maps[mid] = (f"{src} {name}" + (f" ({rel})" if rel else ""), vals, conf)
        # observed data values
        prof = con.execute(
            "SELECT p.top_values, p.distinct_capped FROM profiles p JOIN objects o ON o.obj_id=p.obj_id "
            "JOIN columns c ON c.obj_id=p.obj_id AND c.column_id=p.column_id WHERE o.name=? AND c.name=?",
            (tbl, col)).fetchone()
        observed = {}
        if prof and prof[0]:
            observed = {str(v): cnt for v, cnt in json.loads(prof[0]) if v is not None}
        strong = {k: v for k, v in maps.items() if v[2] in ("exact", "strong")} or maps
        if not strong:
            continue
        union = set().union(*(v[1] for v in strong.values()))
        union_all = set().union(*(v[1] for v in maps.values()))
        missing = {v: c for v, c in observed.items() if v not in union_all}
        if missing:
            con.execute("INSERT INTO conflicts VALUES (?,?)", (f"{tbl}.{col}",
                        "values present in the data but in no value list: " +
                        ", ".join(f"{v} ({c} rows)" for v, c in sorted(missing.items(), key=lambda x: -x[1]))))
            n += 1
        for mid, (label, vals, _conf) in strong.items():
            lack = sorted(union - vals, key=lambda x: (len(x), x))
            in_data = [v for v in lack if v in observed]
            if lack and len(strong) > 1:
                con.execute("INSERT INTO conflicts VALUES (?,?)", (
                    f"{tbl}.{col}", f"{label} lacks value(s) {', '.join(lack)} that other sources define"
                    + (f" (of which {', '.join(in_data)} occur in the data)" if in_data else "")))
                n += 1
    return n


def _relationships(con: sqlite3.Connection, nm: Names) -> int:
    rels: dict[tuple, dict] = {}

    def add(ft, fc, tt, tc, kind, declared=0, example=None, ex_obj=None, ex_line=None, note=None):
        key = (ft.lower(), fc.lower(), tt.lower(), tc.lower())
        r = rels.get(key)
        if r is None:
            r = rels[key] = {"ft": ft, "fc": fc, "tt": tt, "tc": tc, "kinds": set(), "declared": 0, "occ": 0,
                             "example": None, "ex_obj": None, "ex_line": None, "ex_rank": None, "notes": set()}
        r["kinds"].add(kind)
        r["declared"] |= declared
        if kind == "mined_join":
            r["occ"] += 1
        if example:
            rank = (0 if (ex_obj or "").lower().endswith("view") else 1, len(example))
            if r["example"] is None or rank < r["ex_rank"]:
                r["example"], r["ex_obj"], r["ex_line"], r["ex_rank"] = example, ex_obj, ex_line, rank
        if note:
            r["notes"].add(note)

    for pt, pc, rt, rc, name in con.execute(
            "SELECT p.name, f.parent_column, r.name, f.ref_column, f.name FROM fks f "
            "JOIN objects p ON p.obj_id=f.parent_obj_id JOIN objects r ON r.obj_id=f.ref_obj_id"):
        add(pt, pc, rt, rc, "fk", declared=1, note=f"FK {name}")

    def orient(lt, lc, rt, rc):
        """Many side first: the side whose column is its table's single-column PK is the target."""
        lpk = nm.pk.get(lt.lower(), "").lower() == lc.lower()
        rpk = nm.pk.get(rt.lower(), "").lower() == rc.lower()
        if lpk and not rpk:
            return rt, rc, lt, lc
        return lt, lc, rt, rc

    for oname, lt, lc, rt, rc, pred, line in con.execute(
            "SELECT o.name, j.left_table, j.left_column, j.right_table, j.right_column, j.predicate, j.line "
            "FROM sql_joins j JOIN objects o ON o.obj_id=j.obj_id"):
        a, b = nm.table(lt), nm.table(rt)
        if not a or not b or not nm.is_table(a) or not nm.is_table(b):
            continue
        ac, bc = nm.column(a, lc), nm.column(b, rc)
        if not ac or not bc:
            continue
        ft, fc, tt, tc = orient(a, ac, b, bc)
        add(ft, fc, tt, tc, "mined_join", example=pred, ex_obj=oname, ex_line=line)

    for tname, cname, desc in con.execute(
            "SELECT o.name, c.name, c.description FROM columns c JOIN objects o ON o.obj_id=c.obj_id "
            "WHERE o.kind='table' AND c.description LIKE '%link to%'"):
        m = LINK_RE.search(desc)
        if not m:
            continue
        tt = nm.table(m.group(1))
        if not tt:
            continue
        tc = nm.pk.get(tt.lower(), "Id")
        add(tname, cname, tt, tc, "description", note=f"MS_Description: {desc.strip()[:160]}")

    for tname, cname in con.execute(
            "SELECT o.name, c.name FROM columns c JOIN objects o ON o.obj_id=c.obj_id "
            "WHERE o.kind='table' AND c.name LIKE '%\\_Id' ESCAPE '\\'"):
        target = nm.table(cname[:-3])
        if target and nm.is_table(target) and target.lower() != tname.lower():
            tc = nm.pk.get(target.lower())
            if tc:
                add(tname, cname, target, tc, "naming", note=f"naming convention [TableName]_Id")

    n = 0
    for r in rels.values():
        kinds = r["kinds"]
        if r["declared"]:
            conf = "exact"
        elif "mined_join" in kinds and (r["occ"] >= 2 or len(kinds) > 1):
            conf = "strong"
        elif "description" in kinds or "naming" in kinds or "mined_join" in kinds:
            conf = "strong" if len(kinds) > 1 else "weak"
        else:
            conf = "weak"
        kind = "fk" if r["declared"] else ("mined_join" if "mined_join" in kinds else sorted(kinds)[0])
        con.execute("INSERT INTO relationships(from_table,from_column,to_table,to_column,kind,declared,occurrences,"
                    "containment,confidence,example,example_obj,example_line,note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (r["ft"], r["fc"], r["tt"], r["tc"], kind, r["declared"], r["occ"], None, conf,
                     r["example"], r["ex_obj"], r["ex_line"],
                     "; ".join(sorted(r["notes"]) + ([f"sources: {', '.join(sorted(kinds))}"] if len(kinds) > 1 else []))
                     or None))
        n += 1
    return n


def _column_notes(cfg: Config, con: sqlite3.Connection, nm: Names) -> int:
    n = 0
    cols = defaultdict(dict)
    for tname, cname, desc in con.execute(
            "SELECT o.name, c.name, c.description FROM columns c JOIN objects o ON o.obj_id=c.obj_id WHERE o.kind='table'"):
        cols[tname][cname.lower()] = (cname, desc)

    # code evidence for  XLocal = ...  assignments
    local_cols = {c[0] for t in cols.values() for c in t.values() if c[0].endswith("Local")}
    local_cols |= {c[0] for t in cols.values() for c in t.values() if c[0].endswith("_Local")}
    code_ev: dict[str, str] = {}
    if local_cols:
        pat = re.compile(r"\b(" + "|".join(sorted(map(re.escape, local_cols))) + r")\s*=\s*([^;=][^;]{0,160});")
        root = cfg.paths.workspaces
        for sub in ("Legacy/Legacy.Entities", "Modern/Modern.Api.Entities"):
            base = root / sub
            if not base.is_dir():
                continue
            for p in base.rglob("*.cs"):
                try:
                    text = p.read_text(encoding="utf-8-sig", errors="replace")
                except OSError:
                    continue
                for m in pat.finditer(text):
                    if m.group(1) in code_ev or "*" not in m.group(2):
                        continue
                    line = text.count("\n", 0, m.start()) + 1
                    rel = p.relative_to(root).as_posix()
                    code_ev[m.group(1)] = f"{rel}:{line}: {m.group(1)} = {' '.join(m.group(2).split())[:140]}"

    for tname, tcols in cols.items():
        for low, (cname, desc) in tcols.items():
            base = None
            if low.endswith("_local"):
                base = tcols.get(low[:-6])
            elif low.endswith("local"):
                base = tcols.get(low[:-5])
            if base:
                ev = "; ".join(x for x in (f"MS_Description: {desc}" if desc else None, code_ev.get(cname)) if x)
                note = (f"{cname} is {base[0]} converted to the tenant's system (books) currency: "
                        f"{cname} = {base[0]} * ExchangeRate (rounded). {base[0]} is in the record's own "
                        f"currency (its CurrencyCode). Use {cname} when amounts from different "
                        f"currencies are summed or reported in the system currency.")
                con.execute("INSERT INTO column_notes VALUES (?,?,?,?,?)", (tname, cname, "currency", note, ev or None))
                con.execute("INSERT INTO column_notes VALUES (?,?,?,?,?)", (
                    tname, base[0], "currency",
                    f"{base[0]} is in the record's own currency (CurrencyCode); the system-currency amount is "
                    f"{cname} (= {base[0]} * ExchangeRate).", ev or None))
                n += 2
            if low.endswith("clean") and tcols.get(low[:-5]):
                b = tcols[low[:-5]][0]
                con.execute("INSERT INTO column_notes VALUES (?,?,?,?,?)", (
                    tname, cname, "date_clean", f"{cname} is the date-only copy of {b} (no time part); "
                    f"use it for day comparisons and grouping.", f"MS_Description: {desc}" if desc else "naming: <Col>Clean"))
                n += 1
            if desc and re.search(r"system updated|synced field|automatically", desc, re.I):
                con.execute("INSERT INTO column_notes VALUES (?,?,?,?,?)",
                            (tname, cname, "synced", f"Maintained by the system, not entered: {desc.strip()}",
                             f"{tname}.{cname} MS_Description"))
                n += 1
            if desc and desc.strip().startswith("="):
                con.execute("INSERT INTO column_notes VALUES (?,?,?,?,?)",
                            (tname, cname, "derived", f"Derived value: {cname} {desc.strip()}",
                             f"{tname}.{cname} MS_Description"))
                n += 1
    return n


def _ui_targets(con: sqlite3.Connection, nm: Names) -> int:
    n = 0
    rules = con.execute("SELECT pattern, target, rel_path, line FROM url_rules").fetchall()
    view_rule = next((r for r in rules if "viewname" in r[1].lower()), None)
    rep_rule = next((r for r in rules if "reportname" in r[1].lower()), None)
    seen = set()
    for name, obj, props in con.execute("SELECT view_name, object_name, props FROM ui_views"):
        key = ("view", name.lower())
        if key in seen:
            continue
        seen.add(key)
        title = json.loads(props).get("Title")
        url = f"ui/manage/views/{name.lower()}.aspx" if view_rule else None
        hit = nm.obj.get((obj or "").lower())
        con.execute("INSERT INTO ui_targets VALUES (?,?,?,?,?,?)",
                    ("view", name, title, url, obj, hit[0] if hit else None))
        n += 1
    for name, title, obj in con.execute("SELECT report_name, title, object_name FROM ui_reports"):
        key = ("report", name.lower())
        if key in seen:
            continue
        seen.add(key)
        url = f"ui/reports/{name.lower()}.aspx" if rep_rule else None
        hit = nm.obj.get((obj or "").lower())
        con.execute("INSERT INTO ui_targets VALUES (?,?,?,?,?,?)",
                    ("report", name, title, url, obj, hit[0] if hit else None))
        n += 1
    return n


ALIAS_RE = re.compile(r"\b(from|join|update|into)\s+(?:\[?dbo\]?\.)?\[?([A-Za-z_]\w*)\]?"
                      r"(?:\s+(?:as\s+)?(?!where\b|with\b|on\b|set\b|inner\b|left\b|right\b|join\b|order\b|group\b|values\b)([A-Za-z_]\w*))?",
                      re.I)
COLREF_RE = re.compile(r"\b([A-Za-z_]\w*)\.\[?([A-Za-z_]\w*)\]?")
LIT_RE = re.compile(r"\b(?:([A-Za-z_]\w*)\.)?\[?([A-Za-z_]\w*)\]?\s*(=|<>|!=)\s*(-?\d+|'[^']{0,20}')(?![\w.])")
EXEC_RE = re.compile(r"\bexec(?:ute)?\s+(?:\[?dbo\]?\.)?\[?([A-Za-z_]\w*)", re.I)


def _code_sql_refs(con: sqlite3.Connection, nm: Names) -> int:
    rows = []
    for sid, text in con.execute("SELECT code_sql_id, sql_text FROM code_sql").fetchall():
        aliases = {}
        objs = set()
        for m in ALIAS_RE.finditer(text):
            t = nm.table(m.group(2))
            if not t:
                continue
            objs.add(t)
            aliases[t.lower()] = t
            if m.group(3):
                aliases[m.group(3).lower()] = t
        for m in EXEC_RE.finditer(text):
            t = nm.table(m.group(1))
            if t:
                objs.add(t)
        for t in objs:
            rows.append((sid, "object", t, None, None))
        cols = set()
        for m in COLREF_RE.finditer(text):
            t = aliases.get(m.group(1).lower())
            if t:
                c = nm.column(t, m.group(2))
                if c:
                    cols.add((t, c))
        if len(objs) == 1:
            only = next(iter(objs))
            for w in re.findall(r"\b([A-Za-z_]\w*)\b", text):
                c = nm.column(only, w)
                if c and len(w) > 2:
                    cols.add((only, c))
        for t, c in cols:
            rows.append((sid, "column", t, c, None))
        for m in LIT_RE.finditer(text):
            t = aliases.get((m.group(1) or "").lower()) or (next(iter(objs)) if len(objs) == 1 and not m.group(1) else None)
            if t:
                c = nm.column(t, m.group(2))
                if c:
                    rows.append((sid, "literal", t, c, f"{m.group(3)} {m.group(4)}"))
    con.executemany("INSERT INTO code_sql_refs VALUES (?,?,?,?,?)", rows)
    return len(rows)


def run(cfg: Config, con: sqlite3.Connection) -> dict:
    dbm.clear_tables(con, TABLES)
    nm = Names(con)
    vm = _value_maps(con, nm)
    summary = {"desc_value_maps": vm["desc_maps"]}
    inferred = _entity_tables_from_sql(con, nm)
    summary["entity_tables_from_sql"] = len(inferred)
    summary["column_links"] = _column_links(con, nm, vm["maps_by_name"], vm["lookup_map"], inferred)
    summary["conflicts"] = _conflicts(con, nm)
    summary["relationships"] = _relationships(con, nm)
    summary["column_notes"] = _column_notes(cfg, con, nm)
    summary["row_kinds"] = _row_kinds(con, nm)
    summary["ui_targets"] = _ui_targets(con, nm)
    summary["code_sql_refs"] = _code_sql_refs(con, nm)
    con.commit()
    summary["value_maps"] = con.execute("SELECT count(*) FROM value_maps").fetchone()[0]
    return summary
