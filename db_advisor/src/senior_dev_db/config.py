"""config.toml -> frozen dataclasses.

config.toml is the only place paths, connection settings, timeouts, limits and
server settings live.  DB credentials are read from the env file it names and are
never logged, stored in the index, or echoed in a tool result.  Scripts read single
values with ``python -m senior_dev_db.config get <dotted.key>``.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Paths:
    root: Path               # directory holding config.toml
    workspaces: Path
    index_dir: Path
    curated_dir: Path
    examples_dir: Path
    sql_parser: Path
    code_extractor_net8: Path
    db_file: Path | None = None      # serve another index file (eval snapshots)
    style_rules: Path | None = None  # the DB developers' SQL style (validate_sql lints against it)
    eval_mask: Path | None = None    # which objects each eval snapshot rolls back

    @property
    def db_path(self) -> Path:
        return self.db_file or (self.index_dir / "dbadvisor.db")

    @property
    def building_path(self) -> Path:
        return self.index_dir / "building.db"

    @property
    def stage_dir(self) -> Path:
        return self.index_dir / "stage"

    @property
    def eval_dir(self) -> Path:
        return self.index_dir / "eval"

    @property
    def sandbox_report(self) -> Path:
        return self.index_dir / "sandbox_report.json"


@dataclass(frozen=True)
class ServerCfg:
    """[server]: how ``python -m senior_dev_db`` serves; CLI flags override it.  Required."""
    transport: str
    host: str
    port: int
    path: str
    instructions_file: Path          # the MCP instructions sent to clients

    @property
    def local_url(self) -> str:
        """This server's URL from the same machine (scripts/verify.py)."""
        return f"http://localhost:{self.port}{self.path}"


@dataclass(frozen=True)
class TimeoutsCfg:
    """Seconds unless the name says otherwise.  [db] holds the query/validate/preview timeouts."""
    live_check_s: int = 3            # staleness check of cited objects (sys.objects.modify_date)
    index_status_s: int = 5          # index_status: objects changed since the build
    live_definition_s: int = 10      # get_object_definition(live=true), draft_change
    sandbox_status_s: int = 5
    sandbox_connect_s: int = 10
    sandbox_lock_wait_s: int = 60    # one test_sql run at a time; the next waits this long
    sandbox_reset_s: int = 30        # snapshot revert
    sandbox_lock_timeout_ms: int = 5000
    profile_query_s: int = 300       # prepare: profiling query on appdb
    sqlite_read_s: int = 30
    sqlite_write_s: int = 60
    parser_check_s: int = 60         # verify: does the C# parser start


@dataclass(frozen=True)
class DraftsCfg:
    max_drafts: int = 50
    ttl_s: int = 2 * 3600
    # rule 9: above this the exact diff (plus the draft_id) is the deliverable, not the full script
    long_script_lines: int = 200


@dataclass(frozen=True)
class CodeCfg:
    """[code]: what the code phase reads from the workspace."""
    subdirs: tuple[str, ...] = ("Legacy", "Modern")
    web_configs: tuple[str, ...] = ("Legacy/App/Web.config",)   # URL rewrite rules


@dataclass(frozen=True)
class DbCfg:
    env_file: Path
    database: str
    driver: str
    connect_timeout_s: int
    query_timeout_s: int
    validate_timeout_s: int
    preview_default_rows: int = 10
    preview_max_rows: int = 50
    preview_timeout_s: int = 15


@dataclass(frozen=True)
class ProfileCfg:
    max_distinct_kept: int
    sample_percent: int
    lookup_max_rows: int = 400       # lookups phase: rows kept per lookup table
    style_recent_since: str = "2022-01-01"   # style phase: modules modified since then count as recent


@dataclass(frozen=True)
class Limits:
    max_results_default: int
    max_results_hard: int
    definition_max_lines: int


@dataclass(frozen=True)
class CodeAdvisorCfg:
    """The Senior-Dev-MCP Code Advisor this server complements: same workspace, its MCP URL."""
    root: Path | None
    url: str


@dataclass(frozen=True)
class SandboxCfg:
    """The clone database test_sql runs scripts in (same schema as appdb, a few rows per table)."""
    enabled: bool = False
    server: str = "localhost"
    database: str = "appdb_sandbox"
    auth: str = "windows"            # windows | sql (SANDBOX_LOGIN / SANDBOX_PASSWORDS in the env file)
    rows_per_table: int = 10
    max_parent_rows: int = 2000
    run_timeout_s: int = 30
    result_max_rows: int = 20
    snapshot_name: str = "appdb_sandbox_base"
    seed_max_rows: int = 50          # test_sql from_live: rows copied from appdb per run
    seed_max_related: int = 1000     # their children + parents, per run
    max_from_live: int = 8         # from_live: queries per run
    max_join_checks: int = 8
    marker: str = "advisor_sandbox"  # extended property: a rebuild only drops a database carrying it
    runner: str = "sandbox_runner"    # the user test_sql runs as


@dataclass(frozen=True)
class Config:
    path: Path
    paths: Paths
    code_advisor: CodeAdvisorCfg
    db: DbCfg
    profile: ProfileCfg
    limits: Limits
    sandbox: SandboxCfg = SandboxCfg()
    # "<view|report>:<side>" -> why that code side has no registrations (trace_screen lists every side)
    side_notes: tuple[tuple[str, str], ...] = ()
    # None only for configs built by hand; load_config requires [server].
    server: ServerCfg | None = None
    timeouts: TimeoutsCfg = TimeoutsCfg()
    drafts: DraftsCfg = DraftsCfg()
    code: CodeCfg = CodeCfg()

    def rel(self, p: str | Path) -> str:
        """Workspace-relative, forward slashes.  Anything outside is returned as given."""
        s = str(p).replace("\\", "/")
        root = self.paths.workspaces.as_posix().rstrip("/") + "/"
        if s.lower().startswith(root.lower()):
            return s[len(root):]
        return s

    def abs_of(self, rel_path: str) -> str:
        return (self.paths.workspaces / rel_path).as_posix()

    def odbc_driver(self) -> str:
        """The configured driver, or with driver = "auto" the newest SQL Server ODBC driver installed."""
        if self.db.driver.lower() != "auto":
            return self.db.driver
        import pyodbc
        installed = pyodbc.drivers()
        for d in ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server"):
            if d in installed:
                return d
        raise RuntimeError(f"no ODBC Driver 17/18 for SQL Server installed (found: {installed})")

    def connection_string(self, *, database: str | None = None, timeout: int | None = None) -> str:
        env = read_env(self.db.env_file)
        missing = [k for k in ("URL", "LOGIN", "PASSWORDS") if not env.get(k)]
        if missing:
            raise KeyError(f"{self.db.env_file} lacks {missing}")
        driver = self.odbc_driver()
        return (
            f"DRIVER={{{driver}}};SERVER={env['URL']};"
            f"DATABASE={database or self.db.database};UID={_odbc_quote(env['LOGIN'])};"
            f"PWD={_odbc_quote(env['PASSWORDS'])};TrustServerCertificate=yes;{_encrypt(driver)}APP=senior-dev-db;"
            f"ApplicationIntent=ReadOnly;Connection Timeout={timeout or self.db.connect_timeout_s}"
        )


    def sandbox_connection_string(self, *, database: str | None = None, timeout: int | None = None) -> str:
        """Full-access connection to the sandbox clone (never to appdb)."""
        sb = self.sandbox
        driver = self.odbc_driver()
        if sb.auth.lower() == "sql":
            env = read_env(self.db.env_file)
            missing = [k for k in ("SANDBOX_LOGIN", "SANDBOX_PASSWORDS") if not env.get(k)]
            if missing:
                raise KeyError(f"{self.db.env_file} lacks {missing} ([sandbox] auth = \"sql\")")
            cred = f"UID={_odbc_quote(env['SANDBOX_LOGIN'])};PWD={_odbc_quote(env['SANDBOX_PASSWORDS'])};"
        else:
            cred = "Trusted_Connection=yes;"
        return (
            # "host/INSTANCE" is accepted too, so config.toml needs no backslash escaping
            f"DRIVER={{{driver}}};SERVER={sb.server.replace('/', chr(92))};DATABASE={database or sb.database};{cred}"
            f"TrustServerCertificate=yes;{_encrypt(driver)}APP=senior-dev-db-sandbox;"
            f"Connection Timeout={timeout or self.db.connect_timeout_s}"
        )


def _odbc_quote(v: str) -> str:
    # ODBC brace-quoting: safe for ; { } = and any other character in a password
    return "{" + v.replace("}", "}}") + "}"


def _encrypt(driver: str) -> str:
    # Driver 18 knows Encrypt=optional; Driver 17 only yes/no (its default is no).
    return "Encrypt=optional;" if "18" in driver else "Encrypt=no;"


def read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _find_config(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    env = os.environ.get("SENIOR_DEV_DB_CONFIG")
    if env:
        return Path(env).resolve()
    here = Path.cwd().resolve()
    for d in (here, *here.parents):
        if (d / "config.toml").is_file() and (d / "src" / "senior_dev_db").is_dir():
            return d / "config.toml"
    pkg_root = Path(__file__).resolve().parents[2]
    if (pkg_root / "config.toml").is_file():
        return pkg_root / "config.toml"
    raise FileNotFoundError("config.toml not found (pass --config or set SENIOR_DEV_DB_CONFIG)")


def _code_advisor_url(ca_root: Path | None) -> str:
    """The Code Advisor's URL on this machine, from its own config.toml [server]; "" when that is missing
    (the server still runs; verify reports the Code Advisor as not found)."""
    if not ca_root or not (ca_root / "config.toml").is_file():
        return ""
    s = tomllib.loads((ca_root / "config.toml").read_text(encoding="utf-8")).get("server", {})
    return f"http://localhost:{s['port']}{s.get('path', '')}" if "port" in s else ""


def load_config(explicit: str | None = None) -> Config:
    path = _find_config(explicit)
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    root = path.parent

    def p(v: str) -> Path:
        q = Path(v)
        return q if q.is_absolute() else (root / q)

    rp = raw["paths"]
    rc = raw.get("code_advisor", {})
    ca_root = p(rc["root"]).resolve() if rc.get("root") else None
    workspaces = rp.get("workspaces") or ""
    if not workspaces:
        # Index exactly the tree the Code Advisor indexes, so every path:line this server
        # cites opens with the Code Advisor's read_file_range.
        if not ca_root or not (ca_root / "config.toml").is_file():
            raise FileNotFoundError("[paths] workspaces is empty and [code_advisor] root has no config.toml")
        workspaces = tomllib.loads((ca_root / "config.toml").read_text(encoding="utf-8"))["paths"]["workspaces"]
        if not Path(workspaces).is_absolute():
            workspaces = str((ca_root / workspaces).resolve())
    paths = Paths(
        root=root,
        workspaces=Path(workspaces),
        index_dir=p(rp.get("index_dir", ".index")),
        curated_dir=p(rp.get("curated_dir", "knowledge/curated")),
        examples_dir=p(rp.get("examples_dir", "examples")),
        sql_parser=p(rp["sql_parser"]),
        code_extractor_net8=p(rp["code_extractor_net8"]),
        style_rules=p(rp.get("style_rules", "knowledge/team_sql_style.md")),
        eval_mask=p(rp.get("eval_mask", "tests/eval/mask.json")),
    )
    s = raw.get("server")
    if s is None:
        raise ValueError(f"{path}: [server] section missing")
    server = ServerCfg(transport=s["transport"], host=s["host"], port=int(s["port"]), path=s["path"],
                       instructions_file=p(s.get("instructions_file", "MCP_INSTRUCTIONS.md")).resolve())
    rd = raw["db"]
    db = DbCfg(
        env_file=p(rd.get("env_file", ".env")),
        database=rd.get("database", "appdb"),
        driver=rd.get("driver", "auto"),
        connect_timeout_s=int(rd.get("connect_timeout_s", 15)),
        query_timeout_s=int(rd.get("query_timeout_s", 120)),
        validate_timeout_s=int(rd.get("validate_timeout_s", 20)),
        preview_default_rows=int(rd.get("preview_default_rows", 10)),
        preview_max_rows=int(rd.get("preview_max_rows", 50)),
        preview_timeout_s=int(rd.get("preview_timeout_s", 15)),
    )
    rpf = raw.get("profile", {})
    profile = ProfileCfg(
        max_distinct_kept=int(rpf.get("max_distinct_kept", 60)),
        sample_percent=int(rpf.get("sample_percent", 5)),
        lookup_max_rows=int(rpf.get("lookup_max_rows", ProfileCfg.lookup_max_rows)),
        style_recent_since=str(rpf.get("style_recent_since", ProfileCfg.style_recent_since)),
    )
    rl = raw.get("limits", {})
    limits = Limits(
        max_results_default=int(rl.get("max_results_default", 50)),
        max_results_hard=int(rl.get("max_results_hard", 500)),
        definition_max_lines=int(rl.get("definition_max_lines", 400)),
    )
    code_advisor = CodeAdvisorCfg(root=ca_root, url=rc.get("url") or _code_advisor_url(ca_root))
    rs = raw.get("sandbox", {})
    d = SandboxCfg()
    sandbox = SandboxCfg(
        enabled=bool(rs.get("enabled", d.enabled)),
        server=str(rs.get("server", d.server)),
        database=str(rs.get("database", d.database)),
        auth=str(rs.get("auth", d.auth)),
        rows_per_table=int(rs.get("rows_per_table", d.rows_per_table)),
        max_parent_rows=int(rs.get("max_parent_rows", d.max_parent_rows)),
        run_timeout_s=int(rs.get("run_timeout_s", d.run_timeout_s)),
        result_max_rows=int(rs.get("result_max_rows", d.result_max_rows)),
        snapshot_name=str(rs.get("snapshot_name", d.snapshot_name)),
        seed_max_rows=int(rs.get("seed_max_rows", d.seed_max_rows)),
        seed_max_related=int(rs.get("seed_max_related", d.seed_max_related)),
        max_from_live=int(rs.get("max_from_live", d.max_from_live)),
        max_join_checks=int(rs.get("max_join_checks", d.max_join_checks)),
        marker=str(rs.get("marker", d.marker)),
        runner=str(rs.get("runner", d.runner)),
    )
    if sandbox.database.lower() == db.database.lower():
        raise ValueError("[sandbox] database must not be the real database")
    rcode = raw.get("code", {})
    side_notes = tuple((str(k).lower(), str(v)) for k, v in rcode.get("side_notes", {}).items())
    dc = CodeCfg()
    code = CodeCfg(subdirs=tuple(rcode.get("subdirs", dc.subdirs)),
                   web_configs=tuple(rcode.get("web_configs", dc.web_configs)))
    timeouts = TimeoutsCfg(**{k: int(v) for k, v in raw.get("timeouts", {}).items()})
    drafts = DraftsCfg(**{k: int(v) for k, v in raw.get("drafts", {}).items()})
    return Config(path=path, paths=paths, code_advisor=code_advisor, db=db, profile=profile, limits=limits,
                  sandbox=sandbox, side_notes=side_notes, server=server, timeouts=timeouts, drafts=drafts,
                  code=code)


def lookup(cfg: Any, key: str) -> Any:
    """``cfg.<a>.<b>`` for the dotted *key* ``a.b``, e.g. ``server.port`` or ``paths.db_path``."""
    obj = cfg
    for part in key.split("."):
        obj = obj[part] if isinstance(obj, dict) else getattr(obj, part)
    return obj


_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][\w.]*)\s*\}\}")


def render_instructions(cfg: Config) -> str:
    """The MCP instructions: [server] instructions_file with ``{{dotted.key}}`` filled from *cfg*."""
    text = cfg.server.instructions_file.read_text(encoding="utf-8")
    return _PLACEHOLDER.sub(lambda m: str(lookup(cfg, m.group(1))), text)


def main(argv: list[str] | None = None) -> int:
    """``python -m senior_dev_db.config get <dotted.key> [--config <toml>]``.

    How the PowerShell scripts read a value instead of repeating it.
    """
    import argparse

    ap = argparse.ArgumentParser(prog="senior_dev_db.config")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("get", help="print one resolved value, e.g. server.port or paths.db_path")
    g.add_argument("key")
    g.add_argument("--config", default=None)
    args = ap.parse_args(argv)
    value = lookup(load_config(args.config), args.key)
    print(value.as_posix() if isinstance(value, Path) else value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
