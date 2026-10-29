"""config.toml is the single source of operational values (Senior-Dev-MCP flow.md)."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from senior_dev_db.config import lookup, render_instructions

ADVISOR = Path(__file__).resolve().parents[1]


def test_server_section_is_read(cfg):
    assert cfg.server.transport in ("stdio", "streamable-http")
    assert isinstance(cfg.server.port, int)
    assert cfg.server.path.startswith("/")
    assert cfg.server.instructions_file == ADVISOR / "MCP_INSTRUCTIONS.md"


def test_code_advisor_root_is_the_sibling_folder(cfg):
    assert cfg.code_advisor.root == (ADVISOR.parent / "code_advisor").resolve()


def test_lookup_walks_dotted_keys(cfg):
    assert lookup(cfg, "server.port") == cfg.server.port
    assert lookup(cfg, "paths.db_path") == cfg.paths.db_path


def test_config_get_cli_prints_the_value(cfg):
    out = subprocess.run(
        [sys.executable, "-m", "senior_dev_db.config", "get", "server.port", "--config", str(cfg.path)],
        capture_output=True, text=True, check=True,
    ).stdout
    assert out.strip() == str(cfg.server.port)


def test_instructions_come_from_the_file_with_config_values(cfg):
    text = render_instructions(cfg)
    assert "{{" not in text
    assert text.startswith("This server answers questions about")
    assert f"~{cfg.sandbox.rows_per_table} rows per table" in text
    assert f"up to {cfg.sandbox.max_from_live} queries" in text
    assert f"Over {cfg.drafts.long_script_lines} lines" in text
    assert f"at most {cfg.db.preview_max_rows} rows" in text


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
