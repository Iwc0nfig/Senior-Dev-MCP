"""Phase code: what the C# workspace says about the database.

Runs code-extractor/CodeExtractor.exe (Roslyn, syntax level) over Legacy and
Modern, resolves enum/const values, and loads:
  code_enums / code_enum_items   enums, ValueEnum classes, MemTypes, const groups
  code_entities                  entity class -> table
  code_entity_columns            column reads (with converting type) and written columns
  ui_views / ui_view_columns     MasterView grid screens
  ui_reports / filters / fields  reports
  url_rules                      Web.config rewrite rules
  code_sql                       SQL text in string literals
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import subprocess
import xml.etree.ElementTree as ET
from collections import defaultdict

from ..config import Config
from .. import db as dbm

log = logging.getLogger("prep.code")

TABLES = ["code_enums", "code_enum_items", "code_entities", "code_entity_columns", "ui_views",
          "ui_view_columns", "ui_reports", "ui_report_filters", "ui_report_fields", "url_rules", "code_sql"]

def side_of(rel: str) -> str:
    return "net48" if rel.lower().startswith("legacy/") else "net8"


class ValueResolver:
    """Turns an initializer expression into a value using extracted enums and consts."""

    def __init__(self, recs: list[dict]):
        self.enum_members: dict[tuple[str, str], list[str]] = defaultdict(list)
        self.consts: dict[str, list[tuple[str, str]]] = defaultdict(list)   # name -> [(container, value)]
        for r in recs:
            if r["t"] == "enum":
                for name, value, _expr, _line in r["members"]:
                    if value is not None:
                        self.enum_members[(r["name"], name)].append(value)
            elif r["t"] == "const" and r.get("value") is not None:
                self.consts[r["name"]].append((r["container"], str(r["value"])))

    def resolve(self, expr: str | None, literal: str | None = None) -> str | None:
        if literal is not None:
            return literal
        if not expr:
            return None
        e = expr.strip()
        e = re.sub(r"^\(\s*(byte|int|short|long|char|sbyte|ushort|uint)\s*\)\s*", "", e)
        e = e.strip("() ")
        if re.fullmatch(r"-?\d+", e):
            return e
        if re.fullmatch(r"'.'", e):
            return e[1]
        parts = e.split(".")
        if len(parts) >= 2:
            vals = set(self.enum_members.get((parts[-2], parts[-1]), []))
            if len(vals) == 1:
                return vals.pop()
            cands = {v for c, v in self.consts.get(parts[-1], [])
                     if c.split(".")[-len(parts[:-1]):] == parts[:-1]
                     or c.endswith("." + ".".join(parts[:-1]))}
            if len(cands) == 1:
                return cands.pop()
        elif len(parts) == 1:
            cands = {v for _c, v in self.consts.get(parts[0], [])}
            if len(cands) == 1:
                return cands.pop()
        return None


def _extract(cfg: Config) -> list[dict]:
    exe = cfg.paths.code_extractor_net8
    if not exe.is_file():
        raise FileNotFoundError(f"{exe} missing: dotnet build code-extractor -c Release")
    out = cfg.paths.stage_dir / "code.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    p = subprocess.run([str(exe), str(cfg.paths.workspaces), str(out), *cfg.code.subdirs],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr[-2000:])
    log.info("CodeExtractor: %s", " | ".join(p.stderr.strip().splitlines()[-2:]))
    with out.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _url_rules(cfg: Config, con: sqlite3.Connection) -> int:
    n = 0
    for rel in cfg.code.web_configs:
        path = cfg.paths.workspaces / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        lines = text.splitlines()
        try:
            root = ET.fromstring(text)
        except ET.ParseError as e:
            log.warning("%s: %s", rel, e)
            continue
        for rule in root.iter("rule"):
            m = rule.find("match")
            a = rule.find("action")
            if m is None or a is None or not m.get("url") or not a.get("url"):
                continue
            name = rule.get("name") or ""
            line = next((i + 1 for i, l in enumerate(lines) if f'name="{name}"' in l), 0) if name else 0
            con.execute("INSERT INTO url_rules VALUES (?,?,?,?,?)", (name, m.get("url"), a.get("url"), rel, line))
            n += 1
    return n


def run(cfg: Config, con: sqlite3.Connection) -> dict:
    dbm.clear_tables(con, TABLES)
    recs = _extract(cfg)
    res = ValueResolver(recs)
    unresolved = 0

    def add_enum(name, fqn, kind, rel, line, items):
        cur = con.execute("INSERT INTO code_enums(name,fqn,kind,side,rel_path,line) VALUES (?,?,?,?,?,?)",
                          (name, fqn, kind, side_of(rel), rel, line))
        eid = cur.lastrowid
        con.executemany("INSERT INTO code_enum_items VALUES (?,?,?,?,?,?)",
                        [(eid, v, m, lab, rel, ln) for v, m, lab, ln in items])

    # consts grouped by container + common NAME_PREFIX_ (e.g. CHANNEL_TYPE_*)
    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for r in recs:
        t = r["t"]
        if t == "enum":
            items = [(v, m, m, ln) for m, v, _e, ln in r["members"] if v is not None]
            unresolved += sum(1 for m in r["members"] if m[1] is None)
            add_enum(r["name"], r["fqn"], "enum", r["rel"], r["line"], items)
        elif t == "value_enum":
            items = []
            for field, label, value, expr, ln in r["items"]:
                v = res.resolve(expr, value)
                if v is None:
                    unresolved += 1
                    continue
                items.append((v, field, label or field, ln))
            add_enum(r["name"], r["fqn"], "value_enum", r["rel"], r["line"], items)
            for hname, body, ln in r.get("helpers", []):
                con.execute("INSERT INTO code_enum_items VALUES ((SELECT max(enum_id) FROM code_enums),?,?,?,?,?)",
                            ("helper", hname, body, r["rel"], ln))
        elif t == "memtype":
            items = []
            for label, value, expr, ln in r["items"]:
                v = res.resolve(expr, value)
                if v is None:
                    unresolved += 1
                    v = "?" + expr
                items.append((v, expr, label, ln))
            add_enum(r["name"], "MemType:" + r["name"], "memtype", r["rel"], r["line"], items)
        elif t == "const" and r.get("value") is not None and re.fullmatch(r"-?\d+", str(r["value"])):
            m = re.match(r"^([A-Z][A-Z0-9]*_(?:[A-Z0-9]+_)*)", r["name"])
            if m:
                groups[(r["container"], m.group(1), r["rel"])].append(r)
        elif t == "entity":
            con.execute("INSERT INTO code_entities VALUES (?,?,?,?,?)",
                        (r["class"], r["table"], side_of(r["rel"]), r["rel"], r["line"]))
        elif t == "view":
            p = r["props"]
            con.execute("INSERT INTO ui_views VALUES (?,?,?,?,?,?,?)",
                        (p.get("Name") or p.get("ObjectName") or "?", p.get("ObjectName"), side_of(r["rel"]),
                         json.dumps(p, ensure_ascii=False), r["rel"], r["line"], r["line_end"]))
            for i, c in enumerate(r["columns"], 1):
                con.execute("INSERT INTO ui_view_columns VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (p.get("Name") or p.get("ObjectName") or "?", side_of(r["rel"]), i,
                             c.get("FieldName") or "?", c.get("Caption"), c.get("ColumnType"),
                             c.get("MemValues"), json.dumps({k: v for k, v in c.items() if not k.startswith("_")},
                                                            ensure_ascii=False),
                             r["rel"], c["_line"], c["_line_end"]))
        elif t == "report":
            p = r["props"]
            name = p.get("Name") or "?"
            con.execute("INSERT INTO ui_reports VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (name, p.get("Title"), p.get("ObjectName"), p.get("DataType"), p.get("Category"),
                         side_of(r["rel"]), json.dumps(p, ensure_ascii=False), r["rel"], r["line"], r["line_end"]))
            for i, f in enumerate(r["filters"], 1):
                con.execute("INSERT INTO ui_report_filters VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (name, side_of(r["rel"]), r["rel"], i, f.get("FilterName"), f.get("Caption"),
                             f.get("FilterType"), f.get("MemType"), f.get("DefaultValue"), f.get("Required"),
                             f["_line"]))
            for i, f in enumerate(r["fields"], 1):
                if not f.get("DataField"):
                    continue
                con.execute("INSERT INTO ui_report_fields VALUES (?,?,?,?,?,?,?,?)",
                            (name, side_of(r["rel"]), r["rel"], i, f["DataField"], f.get("Caption"),
                             json.dumps({k: v for k, v in f.items() if not k.startswith("_")}, ensure_ascii=False),
                             f["_line"]))
        elif t == "sql":
            con.execute("INSERT INTO code_sql(side,rel_path,line,container,sql_text) VALUES (?,?,?,?,?)",
                        (side_of(r["rel"]), r["rel"], r["line"], r.get("container"), r["text"]))

    for (container, prefix, rel), members in groups.items():
        if len(members) < 2:
            continue
        name = container.split(".")[-1] + "." + prefix.rstrip("_") + "_*"
        add_enum(name, container + "." + prefix + "*", "const_group", rel, min(m["line"] for m in members),
                 [(str(m["value"]), m["name"], m["name"][len(prefix):].replace("_", " ").lower(), m["line"])
                  for m in members])

    # column reads / writes, joined to the entity's table when the class declares one
    table_of = defaultdict(set)
    for r in recs:
        if r["t"] == "entity":
            table_of[r["class"]].add(r["table"])
    for r in recs:
        if r["t"] == "colread":
            tables = table_of.get(r.get("class") or "", set())
            tbl = next(iter(tables)) if len(tables) == 1 else None
            vt = r.get("wrapper") or r.get("target_type")
            con.execute("INSERT INTO code_entity_columns VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (r.get("class") or "", tbl, r["column"], r.get("target"), vt, "read",
                         side_of(r["rel"]), r["rel"], r["line"], r.get("snippet")))
        elif r["t"] == "fields":
            tables = table_of.get(r["class"], set())
            tbl = next(iter(tables)) if len(tables) == 1 else None
            con.executemany("INSERT INTO code_entity_columns VALUES (?,?,?,?,?,?,?,?,?,?)",
                            [(r["class"], tbl, c, None, None, "write", side_of(r["rel"]), r["rel"], r["line"],
                              f"{r['member']} column list") for c in r["columns"]])

    dbm.set_meta(con, "workspaces", cfg.paths.workspaces.as_posix())
    n_rules = _url_rules(cfg, con)
    con.commit()
    summary = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES}
    summary["unresolved_values"] = unresolved
    summary["url_rules"] = n_rules
    return summary
