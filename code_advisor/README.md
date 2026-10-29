# Senior-Dev-MCP Code Advisor (read-only)

A grounded, **read-only** MCP server over a large client codebase (~2.1M indexed lines of
C#, ASP.NET WebForms and vanilla JavaScript, across a legacy .NET Framework workspace and
a .NET 8 one).

It answers "where is X", "what calls this", "what does this do" with answers that trace
back to a file and line — and says "not found" instead of guessing. This file is the
developer guide. Install, deploy and run commands for everyone are in
[`docs/SETUP.md`](../docs/SETUP.md); the grounding contract clients follow is the root
[`AGENTS.md`](../AGENTS.md); what the server tells every client is
[`MCP_INSTRUCTIONS.md`](MCP_INSTRUCTIONS.md).

## Why this exists in this shape

Two facts about this repository drive the whole design:

1. **`Legacy/App/` is an ASP.NET Website Project.** It has no `.csproj`, so
   `MSBuildWorkspace` skips it entirely — the largest and most-asked-about part of the
   system would be invisible to a Roslyn-based tool. This server rebuilds it as a
   synthetic Roslyn project and grafts it into the same `Solution` as the real projects.
2. **There are no designer partials on disk.** In a Website Project ASP.NET generates the
   control field declarations at runtime. 4,901 of the 9,300 control IDs in `App/` markup
   are referenced by name in their code-behind; without synthesised partials those
   identifiers do not resolve and every reference through the UI layer is wrong.

## Layout

```
config.toml                the only place paths, limits, timeouts and server settings live
                           (relative paths resolve against this folder)
MCP_INSTRUCTIONS.md        the instructions the server sends every client
code_advisor_pipeline.ps1  setup -> build hosts -> build workspace -> controls -> reindex -> tests
markup-codegen/            net48 bridge to ASP.NET's page compiler (ClientBuildManager)
src/senior_dev_code/      the MCP server, indexer and tool surface
roslyn-host/               the C# sidecar, multi-targeted net48 + net8.0
scripts/                   one script per step (the pipeline calls them), plus run/bench/provision
tests/                     parser regressions, golden answers, read-only enforcement, config guard
.index/                    generated: graph.db, synthesised designers, logs,
                           build/ (manifest, generated page code, reference assemblies)
```

Scripts never repeat a value from `config.toml`; they read it with
`.venv\Scripts\python.exe -m senior_dev_code.config get <section.key>` (e.g. `server.port`,
`paths.db_path`). `tests/test_config.py` fails if a port or a machine path appears in `src/` or
`scripts/`.

The MCP server and the indexer never write under the workspace root. That is enforced by
a test, not a policy (`tests/test_tools_contract.py`). The one step that does write there
is `scripts/build-workspace.py`, by design. It writes only MSBuild output (`bin/`, `obj/`)
and refreshed website `Bin` folders, and never touches a read-only (TFVC
version-controlled) file. It is meant for the pipeline's workspace copy.

## Setup

`code_advisor_pipeline.ps1` runs every step below in order; each is also a script of its own.

```bash
powershell -ExecutionPolicy Bypass -File scripts/setup.ps1     # .venv + pip install -e ".[dev]"
```

Generate the ASP.NET control table (reflects over the real `System.Web` reference
assemblies; never hand-maintained):

```bash
powershell -ExecutionPolicy Bypass -File scripts/gen_aspnet_controls.ps1
```

Build both Roslyn hosts and the page-code bridge (`markup-codegen`, net48: asks ASP.NET for
every page's generated code). This also repairs the net472 BuildHost payload — see
`scripts/build-hosts.ps1` for why that is necessary:

```bash
powershell -ExecutionPolicy Bypass -File scripts/build-hosts.ps1
```

Build the workspace, then the index:

```bash
.venv/Scripts/python scripts/build-workspace.py
.venv/Scripts/python scripts/reindex.py
```

`build-workspace.py` restores and builds every `.csproj` in the workspace with Visual
Studio's MSBuild (the instance that has both the .NET SDK resolver and the web
targets), refreshes each Website Project's `Bin` the way a VS website build does, and
runs `markup-codegen` for every site. It writes `.index/build/manifest.json` with
per-project exit codes and errors. A project that fails to build is recorded, not fatal.

`reindex.py` builds the structural graph, then the **compiler index**. It loads every
project of each side into Roslyn, grafts the Website Projects from the generated page
code, replaces binary references to our own projects with source project references,
compiles everything, and persists every declaration, reference and
inheritance/override edge into `graph.db` (`rsymbols`, `rrefs`, `redges`, plus
per-project compile state in `rprojects`). Pass `--no-roslyn` to build the graph only.

## Why the workspace is built before it is indexed

Compiling in memory binds exactly what `csc` binds, so the compiler step itself loses
nothing. What loses information is **incomplete inputs**: unrestored packages, missing
.NET Framework targeting packs, HintPaths to other projects' `bin/` that were never
built, a Website Project's `Bin` out of step with its `packages.config`, and markup
code (`<%= %>`) that only exists once ASP.NET generates the page class. The build step
exists to make those inputs complete. The index records how complete they were, per
project, and every tool answer carries that record (`compilation`).

.NET Framework 4.5–4.7.1 reference assemblies are not installed by Visual Studio.
`[build] target_framework_root` points at a folder in the
`Microsoft.NETFramework.ReferenceAssemblies` layout; empty (the default) means
`<index_dir>/build/refasm`. The build step fills that folder itself when a version is
missing: the six signed `Microsoft.NETFramework.ReferenceAssemblies.net4xx` NuGet
packages (Microsoft author signature checked with `dotnet nuget verify` before
extraction) plus the installed 4.7.2–4.8.1 (`--no-fetch-refasm` turns the download
off). The build and the Roslyn load both use it as `TargetFrameworkRootPath`.

## Running

```bash
powershell -ExecutionPolicy Bypass -File scripts/run.ps1        # as configured in [server]
.venv/Scripts/python -m senior_dev_code --list-tools           # the tool surface, then exit
.venv/Scripts/python -m senior_dev_code --transport stdio --no-roslyn
```

It registers exactly 18 read-only tools. Transport, host, port and path come from
`config.toml [server]` (streamable-http on `0.0.0.0:8400/mcp`); `--transport`, `--host`,
`--port`, `--path` and `--stateless-http` override them. Add `--no-roslyn` to run the
structural and filesystem layers only. The MCP SDK leaves DNS-rebinding protection off unless
`TransportSecuritySettings` is passed, and there is no authentication: bind `0.0.0.0` only on
a trusted network.

For a locally spawned (stdio) client:

```json
{
  "mcpServers": {
    "senior-dev-code": {
      "command": "<this folder>/.venv/Scripts/python.exe",
      "args": ["-m", "senior_dev_code", "--transport", "stdio"]
    }
  }
}
```

## The tool surface

| Tool | Layer | Purpose |
|---|---|---|
| `find_symbol` | graph | Name/FQN search across both sides |
| `go_to_definition` | roslyn-index → roslyn → graph | Declaration site(s) |
| `find_references` | roslyn-index → roslyn → graph | All use sites; dispatch and candidate sites kept apart |
| `find_callers` | roslyn-index → roslyn → graph | Inbound call tree |
| `find_implementations` | roslyn-index → roslyn | Implementers, derived types (transitive), overrides |
| `get_type_info` | roslyn | Members, bases, interfaces, accessibility |
| `get_dependency_graph` | roslyn + graph | Project/type dependencies, cycles |
| `find_dead_code` | roslyn | Scoped unreferenced private/internal members |
| `read_file_range` | filesystem | Exact lines. The grounding primitive |
| `search_text` | filesystem | Literal/regex fallback |
| `find_control` | graph | A caption on a screen → its controls and the code that fills them |
| `get_control_binding` | graph | Markup control ↔ type ↔ code-behind ↔ handlers |
| `get_page_assets` | graph | Master, user controls, scripts, styles, handlers |
| `find_migration_twin` | roslyn-index → graph | Legacy ↔ new twin types: api_contract / nested_contract / same_name evidence, wire-shape diff |
| `find_api_calls` | roslyn-index | Outbound: endpoints a page/type/member ends up calling, with call chains |
| `index_status` | meta | What is indexed, what is not, and why; per-project compile/build state (`project=`) |
| `get_source_manifest` | filesystem | Hash-bound, paged inventory of the C# source (`[source_export]` bounds) |
| `read_source_batch` | filesystem | Manifest-listed files as base64 chunks, original bytes preserved |

There is no `write`, `edit`, `apply_patch` or `execute` tool. Registration fails at
startup if one is ever added.

## Measured on this machine (2026-09-07)

| | |
|---|---|
| Indexed | 6,357 files / 2,131,448 lines (+406 recorded but not parsed: vendor JS, >2 MB) |
| Symbols / refs / edges | 224,103 / 2,696,386 / 19,148 |
| Controls / pages | 10,513 / 459 |
| Synthesised designer partials | 436 files, 9,441 fields, 0 CS0102 collisions |
| Full index pass | ~6 min |
| Path refs resolved case-insensitively | 13 |
| `Legacy.sln` load | 8/8 projects + `App.Website` (815 docs), 3.3 s, 466 MB |
| `Modern.sln` load | 34/40 projects, 9.1 s, 549 MB |
| Markup type resolution | 9,173/9,173 tags resolved, 0 fallbacks |
| Script include targets | 124/125 resolved |
| `OnClient*` handlers | 118/119 resolved |

The 6 `Modern.sln` projects that do not load fail on pre-existing repository
problems (a missing `.csproj` reference, NuGet package downgrades treated as errors, a
net7.0 project referencing a net8.0 one). They are reported through `index_status`, never
silently absent.

## Compiler index, measured on this machine (2026-09-24)

| | |
|---|---|
| Build step (`build-workspace.py`) | 341 projects: 305 restore, 196 build; 0 missing targeting packs; ~5 min |
| Page codegen (App/) | 372 markup files: 367 generated, 0 failed, 5 `.asmx` with no code; 2,068 of 2,069 nuggets mapped to the exact `.aspx` column |
| Projects compiled | net48: 300 (incl. 10 Website Projects), 201 without binding errors; net8: 51, 42 without |
| App.Website | 746 documents, 60 binding errors (was: no mscorlib, every type unresolved) |
| Documents | 5,353 source, 376 ASP.NET-generated, 82 source-generated (Razor), 11 synthesised fallbacks |
| Reference substitution | 22 DLL references replaced by source projects, 9 duplicates removed, 14 left ambiguous (recorded) |
| Declarations / references / edges | 175,690 / 1,567,004 / 16,649 (every one resolved by the compiler) |
| Control fields cited at their markup tag | 8,894 |
| Compiler pass (`reindex.py --roslyn-only`) | ~5 min, peak 3.2 GB (net48 host) |
| Full pass (graph + compiler index) | ~12 min; `graph.db` 2.0 GB |
| Determinism | two passes over an unchanged workspace: identical `roslyn_content_hash_*` |

### Legacy → new API links

Legacy code reaches the .NET 8 API over HTTP. The host records both ends exactly:

- call sites that pass an `HttpMethod` plus a URL, or call `HttpClient.*Async`;
- endpoints declared with minimal-API `Map*` (group prefixes resolved through
  variables), or attribute-routed controllers.

`index/api_links.py` matches URL to route segment by segment, applying ASP.NET's
literal-over-parameter precedence and treating a computed suffix on the last segment as a
query string. Each link is stored as `route_exact` / `route_partial` / `route_ambiguous`.
Measured: 462 call sites, 679 endpoints (250 new API, 429 legacy Web API); the
`NextApiClient` wrapper has 90 exact links, 1 partial and 7 unmatched. The 7 are real
mismatches, e.g. legacy `/sys/v{}/orders/search` vs API `/ext/v{}/orders/search`.
Answers expose the links as `http_call_sites`, `CallNode.http`, URL-position
`go_to_definition`, `index_status.compiler_index.api_map`, and the outbound
`find_api_calls` (e.g. `ui/sidepane/audit.aspx` → audit search, token, XLS export).

### Legacy ↔ new DTO twins

`index/twins.py` pairs legacy types with new-API types on three kinds of evidence:
`api_contract` (request bodies at both ends of a `route_exact` call), `nested_contract`
(their member types, followed to a fixed point), and `same_name`. Each pair is compared as
it serializes: inherited members, JSON names, element types for collections. Measured:
3,553 pairs, 45 api_contract, 1,841 identical. The rest list their exact differences,
e.g. `MonexMCCompletionRequest` sends `AuthCode` where the API expects `auth-code`.

The graph layer, for comparison, resolves 1,309,661 of its 2,727,049 name-based
references only as `ambiguous`: the same name matches several declarations.

## Testing

```bash
powershell -File scripts/test.ps1          # 147 pass, 6 skipped (live-host tests), 2026-10-27
powershell -File scripts/test.ps1 -Live    # + SENIOR_DEV_ROSLYN=1: index vs live SymbolFinder
```

The suite runs against the real `graph.db` and workspace; `conftest.py` skips it when either
is missing (`$SENIOR_DEV_CONFIG` selects another config).

`tests/test_markup.py` asserts the parser's output against the real `App/` tree exactly
(358 files, 9,173 live server tags, 780 `runat="server"` tags, and so on). Drift means
either a real repository change or a parser regression — both need a human.


## The index is portable

`graph.db` stores every path relative to `paths.workspaces`; `pages.synth_path` stores a
bare basename resolved against `paths.designer_dir`. Nothing in it is absolute, so the
database can be copied to another machine or folder. The tool
layer rebuilds an absolute path only when handing one to a caller, via `Config.abs_of()`.
`tests/test_portable_index.py` enforces this; `index_status` reports the root the index
was built against beside the configured one, and raises a caveat when they differ.

Each index pass ends with a WAL checkpoint, so `graph.db` is self-contained and can be
copied on its own.

## Path case is resolved explicitly

ASP.NET resolves virtual paths case-insensitively on every platform; NTFS supplies that
for free and a case-sensitive filesystem does not. 9 of 664 directive path references in
`App/` differ from disk in case alone — 4 `CodeFile` and 5 `MasterPageFile`
(`~/system/templates/exContent.master` for `excontent.master`). `index/fsmap.py` resolves
them and returns the **disk** casing, so an index built on Windows is identical to one
built on Linux.

## Re-indexing

```bash
powershell -File scripts/reindex.ps1          # full rebuild
```

Every run is a full rebuild. `graph.db` and its `-wal`/`-shm` sidecars are deleted first,
not emptied, so the old database is never read. Everything else under `.index/`
(`aspnet_controls.json`, `designer/`, `logs/`) stays where it is. Stop the MCP server
and the Roslyn hosts before reindexing. Windows won't delete a `graph.db` that another
process has open, and when that happens the pass stops with an error.

`scripts/reindex.py` also takes `--config`, so an index can be built against a different
workspace root without editing `config.toml`.

There is no git here — the workspace is TFVC — so change detection is a
`(size, mtime_ns)` stat gate plus a BLAKE2b-128 content hash. Do not install a
filesystem watcher; it thrashes during `tf get`.
