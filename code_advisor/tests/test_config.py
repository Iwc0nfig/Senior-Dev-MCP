"""config.toml is the single source of operational values (Senior-Dev-MCP flow.md)."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from senior_dev_code.config import load_config, lookup, render_instructions

ADVISOR = Path(__file__).resolve().parents[1]


SERVER = '[server]\ntransport = "stdio"\nhost = "127.0.0.1"\nport = 1\npath = "/mcp"\n'


def _write_config(cfg_dir: Path, ws: Path, extra: str = SERVER) -> Path:
    (ws / "Legacy" / "App").mkdir(parents=True)
    cfg_dir.mkdir(parents=True, exist_ok=True)
    path = cfg_dir / "config.toml"
    path.write_text(
        "[paths]\n"
        f'workspaces = "{ws.as_posix()}"\n'
        f'net48_solution = "{ws.as_posix()}/Legacy/Legacy.sln"\n'
        f'net8_solution = "{ws.as_posix()}/Modern/Modern.sln"\n'
        f'website_root = "{ws.as_posix()}/Legacy/App"\n'
        'index_dir = ".index"\n'
        'roslyn_host_net8 = "roslyn-host/bin/Release/net8.0/RoslynHost.exe"\n'
        'markup_codegen = "markup-codegen/bin/Release/net48/MarkupCodegen.exe"\n'
        + extra,
        encoding="utf-8",
    )
    return path


def test_relative_paths_resolve_against_config_folder(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "advisor"
    path = _write_config(cfg_dir, tmp_path / "ws")
    monkeypatch.chdir(tmp_path)  # anywhere but the config folder
    cfg = load_config(path)
    assert cfg.paths.index_dir == (cfg_dir / ".index").resolve()
    assert cfg.paths.roslyn_host_net8 == (cfg_dir / "roslyn-host/bin/Release/net8.0/RoslynHost.exe").resolve()
    assert cfg.paths.markup_codegen == (cfg_dir / "markup-codegen/bin/Release/net48/MarkupCodegen.exe").resolve()


def test_server_section_is_read(tmp_path):
    path = _write_config(
        tmp_path / "advisor", tmp_path / "ws",
        '[server]\ntransport = "stdio"\nhost = "10.1.2.3"\nport = 9123\npath = "/x"\n'
        'instructions_file = "INS.md"\n',
    )
    cfg = load_config(path)
    assert (cfg.server.transport, cfg.server.host, cfg.server.port, cfg.server.path) == (
        "stdio", "10.1.2.3", 9123, "/x")
    assert cfg.server.instructions_file == (tmp_path / "advisor" / "INS.md").resolve()


def test_lookup_walks_dotted_keys(cfg):
    assert lookup(cfg, "server.port") == cfg.server.port
    assert lookup(cfg, "paths.db_path") == cfg.paths.db_path


def test_config_get_cli_prints_the_value(cfg):
    out = subprocess.run(
        [sys.executable, "-m", "senior_dev_code.config", "get", "server.port", "--config", str(cfg.source)],
        capture_output=True, text=True, check=True,
    ).stdout
    assert out.strip() == str(cfg.server.port)


def test_instructions_come_from_the_file(cfg):
    text = render_instructions(cfg)
    assert cfg.server.instructions_file == ADVISOR / "MCP_INSTRUCTIONS.md"
    assert "{{" not in text
    assert text.startswith("This server answers questions about")


def test_instructions_fit_the_client_limit(cfg):
    """Claude Code truncates an MCP server's instructions at ~4 KB ('… [truncated]', measured 2026-10-27):
    anything past the limit never reaches the agent, so the file must fit."""
    assert len(render_instructions(cfg)) <= 4000


def test_no_operational_literals_outside_config():
    """Ports, machine paths and old source folders live in config.toml only."""
    pattern = re.compile(r"\b8[45]0[01]\b|[Cc]:[/\\]Users|codebase_agent|database_agaent")
    hits = []
    files = [*ADVISOR.joinpath("src").rglob("*.py"), *ADVISOR.joinpath("scripts").glob("*.p*"),
             *ADVISOR.glob("*.ps1")]
    for f in files:
        for n, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if pattern.search(line):
                hits.append(f"{f.relative_to(ADVISOR)}:{n}: {line.strip()}")
    assert not hits, "\n".join(hits)
