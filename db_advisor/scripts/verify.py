"""Verify a DB Advisor installation, stage by stage ([ok] / [warn] / [FAIL]).

  prereq  Python and packages, ODBC Driver 18, .env, read-only DB login, workspace (shared with
          the Code Advisor), the two C# parsers.
  index   .index/dbadvisor.db complete (every phase, sane counts), then the pytest suite.
  server  Both MCP servers over HTTP: the DB Advisor's tools (EXPECTED_TOOLS) answer; the Code Advisor is
          reachable; C# citations given by the DB Advisor open with the Code Advisor's
          read_file_range at the same line (the two servers index the same tree).

Exit code 0 = no FAIL.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

FAIL: list[str] = []
WARN: list[str] = []


def say(m):
    print(f"==> {m}")


def ok(label, detail=""):
    print(f"  [ok  ] {label}" + (f"  {detail}" if detail else ""))


def bad(label, detail=""):
    print(f"  [FAIL] {label}" + (f"  {detail}" if detail else ""))
    FAIL.append(label)


def warn(label, detail=""):
    print(f"  [warn] {label}" + (f"  {detail}" if detail else ""))
    WARN.append(label)


# --------------------------------------------------------------------------- prereq
def stage_prereq():
    say("prerequisites")
    if sys.version_info >= (3, 12):
        ok("python", sys.version.split()[0])
    else:
        bad("python", f"{sys.version.split()[0]}; 3.12+ required")
    try:
        import mcp  # noqa: F401
        import pydantic  # noqa: F401
        import pyodbc
        ok("packages", f"mcp, pydantic, pyodbc {pyodbc.version}")
    except ImportError as e:
        bad("packages", f"{e}; run scripts\\setup.ps1")
        return
    try:
        from senior_dev_db.config import load_config, read_env
        cfg = load_config(str(ROOT / "config.toml"))
        ok("config.toml", str(cfg.path))
    except Exception as e:
        bad("config.toml", str(e))
        return

    drivers = pyodbc.drivers()
    try:
        drv = cfg.odbc_driver()
        if drv in drivers:
            ok("ODBC driver", drv + (" (auto)" if cfg.db.driver.lower() == "auto" else ""))
        else:
            bad("ODBC driver", f"'{drv}' not installed (found: {', '.join(drivers) or 'none'}); "
                               f"set [db] driver = \"auto\" or install ODBC Driver 17/18 for SQL Server")
    except RuntimeError as e:
        bad("ODBC driver", f"{e}; install Microsoft ODBC Driver 18 (or 17) for SQL Server")

    env = read_env(cfg.db.env_file)
    missing = [k for k in ("URL", "LOGIN", "PASSWORDS") if not env.get(k)]
    if missing:
        bad(".env", f"{cfg.db.env_file} lacks {missing} (see .env.example)")
    else:
        ok(".env", f"{cfg.db.env_file} (values not shown)")
        try:
            con = pyodbc.connect(cfg.connection_string(), autocommit=True, readonly=True, timeout=cfg.db.connect_timeout_s)
            cur = con.cursor()
            db, login = cur.execute("select DB_NAME(), SUSER_SNAME()").fetchone()
            ok("database login", f"{login} -> {db}")
            n = cur.execute("select count(*) from sys.objects where is_ms_shipped=0").fetchone()[0]
            ok("catalog readable", f"{n} user objects")
            writer = cur.execute("select isnull(is_rolemember('db_datawriter'),0) + isnull(is_rolemember('db_owner'),0) "
                                 "+ isnull(is_rolemember('db_ddladmin'),0)").fetchone()[0]
            if writer:
                warn("login can write", "a read-only login is recommended; validate_sql still never executes")
            else:
                ok("login is read-only")
            cur.execute("SET NOEXEC ON")
            cur.execute("select 1/0")
            cur.execute("SET NOEXEC OFF")
            ok("SET NOEXEC (validate_sql)", "compile-only works")
            con.close()
        except Exception as e:
            bad("database login", str(e)[:200])

    ws = cfg.paths.workspaces
    subdirs = cfg.code.subdirs
    if all((ws / s).is_dir() for s in subdirs):
        ok("workspace", ws.as_posix())
    else:
        bad("workspace", f"{ws} must contain {' and '.join(subdirs)}")
    ca = cfg.code_advisor
    if ca.root and (ca.root / "config.toml").is_file():
        import tomllib
        ca_ws = tomllib.loads((ca.root / "config.toml").read_text(encoding="utf-8"))["paths"]["workspaces"]
        if not Path(ca_ws).is_absolute():
            ca_ws = str((ca.root / ca_ws).resolve())
        if Path(ca_ws).resolve() == ws.resolve():
            ok("same workspace as the Code Advisor", str(ca.root))
        else:
            bad("workspace differs from the Code Advisor's", f"{ws} vs {ca_ws}: C# citations would not line up")
    else:
        warn("Code Advisor config not found", f"[code_advisor] root = {ca.root}; citations are still workspace-relative")

    for name, exe, args in (("SqlParser", cfg.paths.sql_parser, []), ("CodeExtractor", cfg.paths.code_extractor_net8, [])):
        if not exe.is_file():
            bad(name, f"{exe} missing; run scripts\\setup.ps1 (needs the .NET SDK) or copy the prebuilt bin folder")
            continue
        try:
            p = subprocess.run([str(exe), *args], capture_output=True, text=True,
                               timeout=cfg.timeouts.parser_check_s)
            if "usage" in (p.stderr + p.stdout).lower():
                ok(name, "starts")
            else:
                bad(name, (p.stderr or p.stdout)[:200] or f"exit {p.returncode}")
        except Exception as e:
            bad(name, f"{e} (needs the .NET 8+ runtime)")

    sb = cfg.sandbox
    if not sb.enabled:
        warn("sandbox", "[sandbox] enabled = false: test_sql will be unavailable")
    else:
        try:
            con = pyodbc.connect(cfg.sandbox_connection_string(database="master", timeout=cfg.timeouts.sandbox_connect_s),
                                 autocommit=True)
            ver, can = con.cursor().execute(
                "select convert(varchar(30), serverproperty('ProductVersion')), "
                "has_perms_by_name(null, null, 'CREATE ANY DATABASE')").fetchone()
            con.close()
            if not ver.startswith("16."):
                warn("sandbox server version", f"{ver}: SQL Server 2022 (16.x) matches appdb best")
            (ok if can else bad)("sandbox server", f"{sb.server}, SQL Server {ver}"
                                 + ("" if can else ": the login cannot CREATE DATABASE"))
        except Exception as e:
            bad("sandbox server", f"{sb.server} not reachable ({str(e)[:160]}); install SQL Server 2022 "
                                  f"Express/Developer or fix [sandbox] in config.toml")


# --------------------------------------------------------------------------- index
def stage_index():
    say("index")
    from senior_dev_db.config import load_config
    cfg = load_config(str(ROOT / "config.toml"))
    dbp = cfg.paths.db_path
    if not dbp.is_file():
        bad("index", f"{dbp} missing; run scripts\\prepare.ps1")
        return
    con = sqlite3.connect(f"file:{dbp.as_posix()}?mode=ro", uri=True)
    meta = dict(con.execute("SELECT key, value FROM index_meta"))
    ok("index", f"built {meta.get('built_at')} from {meta.get('db_name')}")
    phases = dict(con.execute("SELECT name, info FROM phases"))
    need = ["catalog", "profile", "lookups", "sqlparse", "code", "curated_load", "link", "style", "search"]
    miss = [p for p in need if p not in phases]
    if miss:
        bad("phases", f"not run: {miss}")
    else:
        ok("phases", "all 9 finished")
    checks = [
        ("objects", "SELECT count(*) FROM objects", 1000),
        ("modules parsed", "SELECT count(*) FROM sql_parse_status WHERE ok=1", 1000),
        ("grid screens", "SELECT count(DISTINCT view_name) FROM ui_views", 50),
        ("reports", "SELECT count(DISTINCT report_name) FROM ui_reports", 100),
        ("value maps", "SELECT count(*) FROM value_maps", 1000),
        ("relationships", "SELECT count(*) FROM relationships", 1000),
        ("profiled columns", "SELECT count(*) FROM profiles", 3000),
        ("curated facts", "SELECT count(*) FROM curated", 1),
        ("search items", "SELECT count(*) FROM search_items", 10000),
    ]
    for label, q, floor in checks:
        n = con.execute(q).fetchone()[0]
        (ok if n >= floor else bad)(label, f"{n}" + ("" if n >= floor else f" (expected >= {floor})"))
    errs = con.execute("SELECT count(*) FROM sql_parse_status WHERE ok=0").fetchone()[0]
    (ok if errs == 0 else warn)("module parse errors", str(errs))
    con.close()
    if cfg.sandbox.enabled:
        from senior_dev_db.tools.common import Ctx
        from senior_dev_db.tools.meta import sandbox_status
        st = sandbox_status(Ctx.create(cfg))
        (ok if st.startswith("built") else bad)("sandbox clone", st[:200]
                                                 + ("" if st.startswith("built") else "; run scripts\\sandbox.ps1"))
    say("tests (pytest)")
    p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(ROOT / "tests")],
                       capture_output=True, text=True, cwd=ROOT)
    tail = (p.stdout.strip().splitlines() or ["(no output)"])[-1]
    (ok if p.returncode == 0 else bad)("pytest", tail)
    if p.returncode != 0:
        print("\n".join("        " + l for l in p.stdout.splitlines()[-25:]))


# --------------------------------------------------------------------------- server
def _structured(res):
    sc = getattr(res, "structured_content", None) or getattr(res, "structuredContent", None)
    if sc:
        return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
    for c in res.content:
        t = getattr(c, "text", None)
        if t:
            try:
                return json.loads(t)
            except json.JSONDecodeError:
                return {"text": t}
    return {}


async def _with(url, fn):
    from mcp.client.streamable_http import streamable_http_client
    from mcp.client.session import ClientSession
    async with streamable_http_client(url) as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as s:
            await s.initialize()
            return await fn(s)


def stage_server(db_url: str, code_url: str):
    from senior_dev_db.config import load_config
    db_url = db_url or load_config(str(ROOT / "config.toml")).server.local_url
    say(f"DB Advisor at {db_url}")
    from senior_dev_db.tools import EXPECTED_TOOLS

    citations: list[str] = []

    async def db_checks(s):
        tools = {t.name for t in (await s.list_tools()).tools}
        (ok if tools == EXPECTED_TOOLS else bad)("tools", f"{len(tools)} registered")
        st = _structured(await s.call_tool("index_status", {}))
        (ok if st.get("status") == "ok" else bad)("index_status", f"built {st.get('built_at')}")
        col = _structured(await s.call_tool("describe_column", {"table": "OrderTable.Status"}))
        vals = col.get("values", {})
        (ok if "closed" in str(vals.get("3", "")).lower() else bad)("describe_column OrderTable.Status", str(vals)[:90])
        tr = _structured(await s.call_tool("trace_screen", {"target": "ui/manage/views/shipmentview.aspx"}))
        (ok if tr.get("sql_objects", [None])[0] == "ShipmentView" else bad)("trace_screen shipmentview.aspx",
                                                                            str(tr.get("sql_objects"))[:80])
        v = _structured(await s.call_tool("validate_sql", {"sql": "select r.Status from OrderTable r with (nolock)"}))
        (ok if v.get("ok") else bad)("validate_sql (live, compile-only)", str(v.get("errors", ""))[:120])
        pq = _structured(await s.call_tool("preview_query", {"sql": "select top 3 r.Id from OrderTable r with(nolock)"}))
        (ok if len(pq.get("rows", [])) == 3 else bad)("preview_query (live, read-only)", f"{len(pq.get('rows', []))} rows")
        rej = _structured(await s.call_tool("preview_query", {"sql": "delete from OrderTable"}))
        (ok if rej.get("status") == "error" else bad)("preview_query refuses non-SELECT", str(rej.get("caveats"))[:80])
        ts = _structured(await s.call_tool("test_sql", {"sql": "update IssueTable set TicketNo = TicketNo\n"
                                                             "select count(*) as n from IssueTable"}))
        if ts.get("status") == "ok":
            (ok if ts.get("ok") and ts.get("result_sets") else bad)(
                "test_sql (sandbox, rolled back)", "; ".join(ts.get("events", [])[:2]))
        else:
            warn("test_sql", str(ts.get("caveats"))[:120])
        for t in ("OrderTable.Status", "PaymentLogTable.PayMethod", "ProductTable.ChannelType"):
            c = _structured(await s.call_tool("describe_column", {"table": t}))
            for line in c.get("code", [])[:2]:
                citations.append((t.split(".")[1], line.split(" ")[0]))
        return True

    try:
        asyncio.run(_with(db_url, db_checks))
    except Exception as e:
        bad("DB Advisor not reachable", f"{type(e).__name__}: {str(e)[:160]} -- is scripts\\run.ps1 running?")
        return

    from senior_dev_db.config import load_config
    code_url = code_url or load_config(str(ROOT / "config.toml")).code_advisor.url
    say(f"Code Advisor at {code_url} (complementary server)")

    async def code_checks(s):
        tools = {t.name for t in (await s.list_tools()).tools}
        if "read_file_range" not in tools:
            bad("Code Advisor tools", f"read_file_range missing ({len(tools)} tools)")
            return
        ok("Code Advisor tools", f"{len(tools)} registered")
        good = 0
        for col, cite in citations:
            path, _, line = cite.rpartition(":")
            if not line.isdigit():
                continue
            r = _structured(await s.call_tool("read_file_range",
                                              {"file": path, "line_start": int(line), "line_end": int(line)}))
            text = json.dumps(r, ensure_ascii=False)
            if col.lower() in text.lower():
                good += 1
            else:
                warn("citation mismatch", f"{cite}: Code Advisor line does not mention {col}")
        if citations:
            (ok if good == len(citations) else warn)(
                "DB Advisor C# citations open in the Code Advisor", f"{good}/{len(citations)} lines match")
        return True

    try:
        asyncio.run(_with(code_url, code_checks))
    except Exception as e:
        bad("Code Advisor not reachable", f"{type(e).__name__}: {str(e)[:160]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="all", choices=["all", "prereq", "index", "server"])
    ap.add_argument("--db-url", default="", help="default: this machine, config.toml [server] port and path")
    ap.add_argument("--code-url", default="", help="default: config.toml [code_advisor] url")
    a = ap.parse_args()
    if a.stage in ("all", "prereq"):
        stage_prereq()
    if a.stage in ("all", "index"):
        stage_index()
    if a.stage in ("all", "server"):
        stage_server(a.db_url, a.code_url)
    print()
    if FAIL:
        print(f"FAILED: {len(FAIL)} check(s): {', '.join(FAIL)}")
        return 1
    print("all checks passed" + (f" ({len(WARN)} warning(s))" if WARN else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
