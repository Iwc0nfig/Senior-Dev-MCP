# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Kind: tool
Test: `.venv\Scripts\python.exe -m pytest tests/ -q`

## What this repo is

The code half of `Senior-Dev-MCP`; the sibling `db_advisor/` is the database half.

The **Senior-Dev-MCP Code Advisor**: a read-only MCP server (Python, `src/senior_dev_code`) that answers questions about a large C# / ASP.NET WebForms / JS codebase (two workspaces: `Legacy` = net48/legacy, `Modern` = net8). The codebase it analyses is **not in this repo**; `config.toml` points at it (`C:/src/workspaces`, TFVC, no git history). This repo is the indexer + server, not the application.

The root `AGENTS.md` is the grounding contract for *clients* answering questions through the advisor (cite or don't say it, `no_results` is an answer, etc.) and also covers the sibling DB Advisor; `MCP_INSTRUCTIONS.md` is what this server sends every client. Neither is a guide to editing this repo. `README.md` is the developer guide (tool table, measurements); `docs/SETUP.md` has the operations runbook. 18 tools (`EXPECTED_TOOLS`); `config.toml [server]` sets streamable-http on port 8400.

## Commands

Windows / PowerShell; the venv is `.venv`.

```bash
powershell -ExecutionPolicy Bypass -File code_advisor_pipeline.ps1      # every step below, in order
powershell -ExecutionPolicy Bypass -File scripts/setup.ps1              # setup: .venv + pip install -e ".[dev]"
.venv/Scripts/python -m senior_dev_code.config get server.port         # read one config value (scripts do this)
powershell -ExecutionPolicy Bypass -File scripts/gen_aspnet_controls.ps1 # writes .index/aspnet_controls.json (indexer input)
powershell -ExecutionPolicy Bypass -File scripts/build-hosts.ps1         # build roslyn-host (net48 + net8.0) and markup-codegen (net48)

.venv/Scripts/python scripts/build-workspace.py     # restore+build every .csproj in the workspace, generate page code; WRITES into the workspace (bin/obj)
.venv/Scripts/python scripts/reindex.py             # full index rebuild -> .index/graph.db (add --no-roslyn for graph only, --roslyn-only, --config <toml>)
powershell -File scripts/reindex.ps1                # same, with venv check; -RestartHosts restarts RoslynHost processes

.venv/Scripts/python -m senior_dev_code            # serve as config.toml [server] says (streamable-http 0.0.0.0:8400/mcp)
.venv/Scripts/python -m senior_dev_code --transport stdio --no-roslyn
.venv/Scripts/python -m senior_dev_code --list-tools
powershell -File scripts/run.ps1                    # same, with an index check (no auth: trusted network only)
```

Tests:

```bash
.venv/Scripts/python -m pytest tests/ -q                                    # skips live-host tests
SENIOR_DEV_ROSLYN=1 .venv/Scripts/python -m pytest tests/ -q                   # + compiler-layer / live SymbolFinder tests
.venv/Scripts/python -m pytest tests/test_config.py -q                      # single file
```

Tests run against the **real** `graph.db` and workspace, not fixtures: `conftest.py` skips the whole suite if `.index/graph.db` is missing or the configured workspace isn't on the machine (`$SENIOR_DEV_CONFIG` selects another config). This public copy ships only the tests that don't depend on a client's code; the golden and compiler-layer suites are written per client against their own workspace. Stop the server and Roslyn hosts before reindexing (Windows can't delete an open `graph.db`).

## Architecture

Pipeline: **build workspace → index → serve**, each stage a separate step.

1. **Build** (`build/`, `scripts/build-workspace.py`): restores/builds each `.csproj` individually in dependency order (many are in no `.sln`), fills .NET Framework reference assemblies, refreshes Website Project `Bin`s, then runs `markup-codegen` (net48 `ClientBuildManager`) to get ASP.NET's generated page code. Writes `.index/build/manifest.json`. Failed projects are recorded, not fatal. This is the only stage that writes inside the workspace.
2. **Index** (`index/`, `index/runner.py`): always a full rebuild into SQLite `graph.db` (schema in `db.py`, `USER_VERSION`). Two layers:
   - *Graph*: tree-sitter C#/JS (`ts_csharp.py`, `ts_javascript.py`) + `markup.py` for `.aspx/.ascx/.master`, two-phase (declarations, then use sites filtered by phase-1 names). Name-based, so many refs resolve only as `ambiguous`.
   - *Compiler index* (`roslyn_index.py`): runs `RoslynHost --index` (the `roslyn-host/` C# sidecar, multi-targeted net48 + net8.0) over every project and bulk-loads `rsymbols/rrefs/redges/rprojects`. Also derives HTTP links between legacy call sites and net8 endpoints (`api_links.py`) and legacy↔new DTO twins (`twins.py`).
   - `inventory.py` holds the exclusion rules; every skipped file is recorded with a reason.
3. **Serve** (`__main__.py`, `tools/`): `register_all` in `tools/__init__.py` registers the tool modules and **fails at startup** if a tool name contains a mutating word (`FORBIDDEN`) or if the set differs from `EXPECTED_TOOLS`. Adding a tool means adding it to `EXPECTED_TOOLS` deliberately. Tool precedence is `roslyn-index` → live `roslyn` (via `roslyn/client.py` NDJSON-RPC pool, loaded in a background thread) → `graph`/`filesystem`; every result states its layer. `tools/common.py` has `scope_of` (the `searched_scope` string) and `translate_location` (synthetic designer paths → markup lines).

## Invariants to preserve

- **Read-only.** The server and indexer write only under `.index/`. `tests/test_tools_contract.py` enforces it; don't add write/exec tools or write into the workspace outside `build/`.
- **Unavailable ≠ not found.** If a Roslyn host is down or times out, tools return `status: "unavailable"` with no results; they must never silently fall back to a weaker layer. An empty result is `no_results` with a populated `searched_scope`.
- **Portable index.** `graph.db` stores only workspace-relative paths (and basenames for synthesised designers); absolute paths are rebuilt via `Config.abs_of()` at output time. Enforced by `tests/test_portable_index.py`.
- **Path case.** `index/fsmap.py` resolves ASP.NET virtual paths case-insensitively and returns disk casing, so Windows and Linux builds are identical.
- **Synthetic designers.** `App/` is a Website Project (no `.csproj`, no designer files); the graph layer synthesises designer partials into `.index/designer/` that exist in no workspace file. Never point a user at one; cite the markup line. The compiler index instead uses ASP.NET's own generated code (`is_generated`, under `.index/build/markup/`).
- **No filesystem watcher** for change detection: workspace is TFVC and watchers thrash during `tf get`; use the `(size, mtime_ns)` + BLAKE2b hash gate.
- All paths, ports, timeouts and limits live in `config.toml` (relative paths resolve against its folder); don't hardcode them elsewhere. `tests/test_config.py` guards ports and machine paths. The exclusion lists in `inventory.py` / `source_export.py` are code.

## Repo notes

- An example client configuration for both servers is the root `.mcp.example.json`.
- `tools/source_export.py` is a self-described drop-in extension (`get_source_manifest`, `read_source_batch`) with its own exclusion list, separate from `inventory.py`.
