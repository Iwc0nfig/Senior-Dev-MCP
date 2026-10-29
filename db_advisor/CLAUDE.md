# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Kind: tool
Test: `.venv\Scripts\python.exe -m pytest -q -m "not live and not sandbox"`

## What this repo is

The database half of `Senior-Dev-MCP`; the sibling `code_advisor/` is the code half.

The **Senior-Dev-MCP DB Advisor**: a read-only MCP server (Python 3.12+, `src/senior_dev_db`, 17 tools) over a SQL Server database (`appdb` in the examples) and what the C# code says about it. It is the DB counterpart of the Code Advisor (`../code_advisor`).

`README.md` is the developer guide (layout, prepare phases, curation, sandbox); `docs/SETUP.md` is the install runbook. The root `AGENTS.md` (one copy) is the client grounding contract; `MCP_INSTRUCTIONS.md` is what this server sends every client (`{{section.key}}` filled from config).

## Commands (PowerShell, from the repo root)

```bash
powershell -ExecutionPolicy Bypass -File db_advisor_pipeline.ps1           # setup -> verify prereq -> prepare -Sandbox -> verify index
powershell -ExecutionPolicy Bypass -File scripts/setup.ps1                 # venv + deps + C# helpers
powershell -ExecutionPolicy Bypass -File scripts/verify.ps1 -Stage prereq   # then -Stage index, -Stage server
powershell -ExecutionPolicy Bypass -File scripts/prepare.ps1 [-Phases link,style,search] [-Sandbox]   # build .index/dbadvisor.db
powershell -ExecutionPolicy Bypass -File scripts/sandbox.ps1                # rebuild the sandbox clone (~25 s)
powershell -ExecutionPolicy Bypass -File scripts/run.ps1                    # serve as config.toml [server] says (0.0.0.0:8401/mcp)
.venv\Scripts\python.exe -m senior_dev_db.config get server.port      # read one config value (scripts do this)
.venv\Scripts\python.exe -m pytest -q -m "not live and not sandbox"          # offline tests
.venv\Scripts\python.exe -m pytest -q -m live                                # needs the live DB (read-only login in .env)
.venv\Scripts\python.exe -m pytest tests/test_config.py -q                     # single file
```

Stop the server before `prepare.ps1` (Windows won't replace an index file that's open).

## Architecture

- **Prepare** (`prep/`): phases `catalog`, `profile`, `lookups`, `sqlparse` (C# ScriptDom app in `sql-parser/`), `code` (C# Roslyn syntax app in `code-extractor/`), `link`, `curated_load`, `style`, `search`. Each owns its tables in the SQLite index and can be re-run alone. Staging goes to `.index/building.db`, the served index is `.index/dbadvisor.db`.
- **Curation:** `prep.packets` writes packets for gaps, then Haiku subagents in Claude Code write `knowledge/curated/<packet>.json`, then `prep.verify` keeps only facts whose citations verify, then `prepare.ps1 -Phases curated_load,search`.
- **Serve** (`tools/`): 17 read-only tools. Every answer carries a layer label and citations.
- **Sandbox** (`tools/sandbox.py`, `prep/sandbox.py`): `test_sql` runs scripts for real in the `appdb_sandbox` clone as `sandbox_runner`, inside a transaction that always rolls back. A rebuild only drops a database with the `advisor_sandbox` marker.

## Invariants

- `config.toml` is the only place for settings (ports, paths, timeouts, limits; `tests/test_config.py` guards ports and machine paths). The workspace and the Code Advisor URL come from `../code_advisor/config.toml`. Credentials come only from `.env` (never read or print it).
- Reads hit `appdb` with a read-only login and low limits. **Anything that writes runs only in the sandbox clone, never on appdb.**
- No tool name may contain a mutating verb. `test_sql` is the only tool that executes writes, and only in the clone.
- Curated facts enter the index only through `prep/verify.py`.
- SQL written for users follows `knowledge/team_sql_style.md`, which `validate_sql` lints against.
