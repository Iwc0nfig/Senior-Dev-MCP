"""Shared plumbing for the tool layer: per-thread read-only connections, name
resolution, compact formatting helpers and the known limitations."""

from __future__ import annotations

import difflib
import json
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from typing import Optional

from mcp.types import ToolAnnotations

from ..config import Config
from .. import db as dbm

RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

KNOWN_LIMITATIONS = [
    "Schema, module text, descriptions and data profiles are a snapshot taken at prepare time "
    "(see built_at). Only validate_sql (compile-only), preview_query (one low-limit SELECT), "
    "get_object_definition(live=true) and the staleness check talk to the live database.",
    "The login used to prepare has no EXECUTE permission, so stored-procedure result sets come from "
    "the ScriptDom parse of their final SELECT, not from the engine.",
    "Value maps are merged from DB descriptions, C# enums/ValueEnums, MemTypes, const groups and lookup "
    "tables; they disagree in places. Disagreements are reported in caveats, never silently resolved.",
    "Relationships of kind mined_join/description/naming are inferred (not declared foreign keys); "
    "their confidence and example predicate are reported.",
    "C# facts come from Roslyn syntax extraction of Legacy (net48) and Modern (net8). Code "
    "reached by reflection or built at run time is not visible. No commit history (TFVC).",
    "Grid (MasterView) and report registrations exist on several code sides (Legacy, "
    "ModernAPI.Initialization, Modern.Next); trace_screen lists every side under 'sides'.",
]


_LIVE_CACHE: dict[str, tuple[float, Optional[str]]] = {}


@dataclass
class Ctx:
    cfg: Config
    _local: threading.local = field(default_factory=threading.local, repr=False)

    @classmethod
    def create(cls, cfg: Config) -> "Ctx":
        if not cfg.paths.db_path.is_file():
            raise FileNotFoundError(f"{cfg.paths.db_path} not found. Run scripts/prepare.ps1")
        return cls(cfg=cfg)

    @property
    def con(self) -> sqlite3.Connection:
        con = getattr(self._local, "con", None)
        if con is None:
            con = dbm.connect(self.cfg.paths.db_path, read_only=True, timeouts=self.cfg.timeouts)
            self._local.con = con
        return con

    def q(self, sql: str, *args) -> list[sqlite3.Row]:
        return self.con.execute(sql, args).fetchall()

    def q1(self, sql: str, *args) -> Optional[sqlite3.Row]:
        return self.con.execute(sql, args).fetchone()

    # -- scope strings -------------------------------------------------------
    def scope(self, what: str) -> str:
        m = dbm.all_meta(self.con)
        return f"{what} (database '{m.get('db_name', '?')}', index built {m.get('built_at', '?')})"

    def masked_caveats(self) -> list[str]:
        m = dbm.get_meta(self.con, "eval_masked")
        return [f"EVAL SNAPSHOT: {m}"] if m else []

    # -- freshness ----------------------------------------------------------
    def eval_masked_names(self) -> set[str]:
        """Objects rolled back in an evaluation snapshot (never shown to agents); live tools must not
        reveal their current state."""
        v = dbm.get_meta(self.con, "eval_masked_names")
        return {n.lower() for n in json.loads(v)} if v else set()

    def mentions_masked(self, sql: str) -> bool:
        """The text names an object of eval_masked_names (whole word, any case)."""
        return any(re.search(rf"\b{re.escape(n)}\b", sql, re.I) for n in self.eval_masked_names())

    def live_modified(self, names: list[str]) -> dict[str, str]:
        """name(lower) -> live sys.objects.modify_date (ISO), cached 60 s; {} when the DB is unreachable."""
        import time
        now = time.time()
        masked = self.eval_masked_names()
        want = [n for n in {strip_name(x) for x in names if x}
                if n.lower() not in masked
                and (n.lower() not in _LIVE_CACHE or now - _LIVE_CACHE[n.lower()][0] > 60)]
        if want:
            try:
                import pyodbc
                t = self.cfg.timeouts.live_check_s
                con = pyodbc.connect(self.cfg.connection_string(timeout=t), autocommit=True, readonly=True)
                con.timeout = t
                q = ("select name, convert(varchar(19), modify_date, 126) from sys.objects where name in (%s)"
                     % ",".join("?" * len(want)))
                found = {r[0].lower(): r[1] for r in con.cursor().execute(q, *want).fetchall()}
                con.close()
                for n in want:
                    _LIVE_CACHE[n.lower()] = (now, found.get(n.lower()))
            except Exception:
                return {}
        return {n.lower(): _LIVE_CACHE[strip_name(n).lower()][1] for n in names
                if n and strip_name(n).lower() in _LIVE_CACHE and _LIVE_CACHE[strip_name(n).lower()][1]}

    def stale_caveats(self, names: list[str]) -> list[str]:
        """One caveat per object whose live definition changed after the index build."""
        names = [n for n in names if n]
        if not names:
            return []
        live = self.live_modified(names)
        built = dbm.get_meta(self.con, "built_at", "?")
        out = []
        for n in names:
            o = self.obj(n)
            lm = live.get(strip_name(n).lower())
            if o and lm and o["modified"] and lm > o["modified"]:
                out.append(f"{o['name']} changed in the database at {lm.replace('T', ' ')}, after the index "
                           f"build ({built}); its lines/columns may differ from this answer. Use "
                           f"get_object_definition(name, live=true) and re-run prepare.")
        return out

    # -- names ---------------------------------------------------------------
    def obj(self, name: str) -> Optional[sqlite3.Row]:
        name = strip_name(name)
        return self.q1("SELECT * FROM objects WHERE name=? COLLATE NOCASE ORDER BY kind='table' DESC LIMIT 1", name)

    def suggest(self, name: str, kinds: tuple[str, ...] | None = None, n: int = 5) -> list[str]:
        name = strip_name(name)
        rows = self.q("SELECT name FROM objects" + (f" WHERE kind IN ({','.join('?' * len(kinds))})" if kinds else ""),
                      *(kinds or ()))
        names = [r[0] for r in rows]
        low = {x.lower(): x for x in names}
        hits = [low[x] for x in difflib.get_close_matches(name.lower(), list(low), n=n, cutoff=0.6)]
        sub = [x for x in names if name.lower() in x.lower() and x not in hits]
        return (hits + sub)[:n]

    def column_row(self, obj_id: int, col: str) -> Optional[sqlite3.Row]:
        return self.q1("SELECT * FROM columns WHERE obj_id=? AND name=? COLLATE NOCASE", obj_id, col)

    def module_lines(self, obj_id: int) -> list[str]:
        r = self.q1("SELECT definition FROM modules WHERE obj_id=?", obj_id)
        return r[0].split("\n") if r else []

    def member(self, rel: str, line) -> Optional[sqlite3.Row]:
        """The C# member at a cited path:line (prep/code_members.py), or None (not named, or no table)."""
        try:
            return self.q1("SELECT fqn, key, kind, start_line, end_line FROM code_members WHERE rel_path=? AND line=?",
                           rel, int(line))
        except (sqlite3.OperationalError, TypeError, ValueError):
            return None

    def cs(self, rel: str, line, end=None, key: bool = False) -> str:
        """A C# citation: 'path:line (Class.Member)', with the Code Advisor key when asked.  The member name
        is what rule 1 cites; read_file_range(at='path:line') on the Code Advisor opens it."""
        at = f"{rel}:{line}" + (f"-{end}" if end and end != line else "")
        m = self.member(rel, line)
        if not m:
            return at
        short = ".".join(m["fqn"].split(".")[-2:])
        return f"{at} ({short}" + (f", key={m['key']}" if key and m["key"] else "") + ")"

    def value_map_items(self, map_id: int) -> dict[str, str]:
        return {r[0]: r[1] for r in self.q(
            "SELECT value, label FROM value_map_items WHERE map_id=? ORDER BY rowid", map_id)}

    def best_values(self, table: str, col: str) -> tuple[Optional[str], list[str]]:
        """Compact 'v=label' text from the most trustworthy map + caveats for conflicts."""
        rows = self.q("""SELECT cvm.map_id, cvm.confidence, vm.source, vm.name, vm.side
                         FROM column_value_maps cvm JOIN value_maps vm ON vm.map_id=cvm.map_id
                         WHERE cvm.tbl=? COLLATE NOCASE AND cvm.col=? COLLATE NOCASE""", table, col)
        if not rows:
            return None, []
        rank = {"exact": 0, "strong": 1, "curated": 1, "name_match": 2}
        src_rank = {"code_enum": 0, "memtype": 1, "db_description": 2, "lookup_table": 0, "const_group": 3, "curated": 4}
        rows = sorted(rows, key=lambda r: (rank.get(r[1], 9), src_rank.get(r[2], 9), r[4] != "net48"))
        merged: dict[str, str] = {}
        for r in rows:
            if rank.get(r[1], 9) > rank.get(rows[0][1], 9) + 1:
                continue
            for v, lab in self.value_map_items(r[0]).items():
                merged.setdefault(v, lab)
        conf = [r[0] for r in self.q("SELECT detail FROM conflicts WHERE subject=? COLLATE NOCASE", f"{table}.{col}")]
        if len(merged) > 40:
            text = compact_values(dict(list(merged.items())[:40])) + f" … (+{len(merged) - 40})"
        else:
            text = compact_values(merged)
        return text, conf


def strip_name(name: str) -> str:
    n = name.strip().strip(";")
    for prefix in ("[dbo].", "dbo."):
        if n.lower().startswith(prefix):
            n = n[len(prefix):]
    return n.strip("[]")


def compact_values(d: dict[str, str]) -> str:
    def key(v):
        try:
            return (0, int(v))
        except ValueError:
            return (1, v)
    return ", ".join(f"{v}={d[v]}" for v in sorted(d, key=key))


def cite_obj(name: str, line: int | None) -> str:
    return f"{name}:{line}" if line else name


def loads(s: Optional[str]):
    return json.loads(s) if s else None
