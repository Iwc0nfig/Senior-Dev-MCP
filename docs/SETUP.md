# Setup runbook

Every command needed to install, build, run and update both servers on a Windows machine. This
is the hands-on part; the [README](../README.md) is the overview. Developer details are in
`code_advisor/README.md` and `db_advisor/README.md`, and the rules an agent follows with the
servers are in [`AGENTS.md`](../AGENTS.md).

> Senior-Dev-MCP is tailored to each client's codebase and database. The values below
> (`Legacy` / `Modern` workspaces, the `appdb` database) are the generic defaults; a real
> deployment sets its own in the two `config.toml` files.

```
Senior-Dev-MCP\
  full_pipeline.ps1   builds both advisors: code_advisor pipeline, then db_advisor pipeline
  build_mcp.py        packages both into dist\mcp.zip (for the machine that serves them)
  AGENTS.md           rules for agents that use the servers (copy into your project)
  code_advisor\       config.toml, code_advisor_pipeline.ps1, scripts\, src\, tests\, ...
  db_advisor\         config.toml, db_advisor_pipeline.ps1, scripts\, src\, tests\, ...
```

Each advisor has its own `.venv`, `.index` and `config.toml`. **`config.toml` is the only
place for settings** (paths, ports, timeouts, limits); every script reads it, so you change a
value there and nowhere else. Relative paths in it are relative to its own folder.

All commands run in PowerShell. `powershell -ExecutionPolicy Bypass -File <script>` works on
a machine whose policy blocks unsigned scripts.

## The whole install

| Step | What | Command | Time |
|---|---|---|---|
| 1 | Install what the machine needs | `provision-vm.ps1` + installers | once |
| 2 | Create `db_advisor\.env`, check both `config.toml` | by hand | once |
| 3 | Build both advisors | `.\full_pipeline.ps1` | ~30 min on a ~2M-line codebase |
| 4 | Run both servers | `code_advisor\scripts\run.ps1`, `db_advisor\scripts\run.ps1` | leave them open |
| 5 | Check both servers together | `db_advisor\scripts\verify.ps1 -Stage server` | ~30 s |
| 6 | Connect an agent | `.mcp.example.json` + `AGENTS.md` | once per client |

## 1. Install what the machine needs

`code_advisor\scripts\provision-vm.ps1` (run as the account that will run the pipeline)
installs and checks most of it: Python 3.13, .NET SDK 9, Visual Studio Build Tools 17.14 with
the .NET and web build workloads, the .NET Framework reference assemblies, Defender exclusions
and a firewall rule for the Code Advisor's port. `-VerifyOnly` only checks.

```powershell
powershell -ExecutionPolicy Bypass -File code_advisor\scripts\provision-vm.ps1 -WorkspacesRoot D:\src\workspaces
```

The DB Advisor also needs:

| What | Why |
|---|---|
| Microsoft ODBC Driver 17 or 18 for SQL Server | reaches the databases (the newest installed is picked) |
| Network access to the production SQL Server, read-only login | reads `appdb` |
| SQL Server 2022 Express or Developer **on this machine** | the sandbox clone that `test_sql` uses |
| A firewall rule for port 8401 | clients reach the DB Advisor |

The sandbox clone lives on a SQL Server on the advisor's own machine, separate from production.
Install it as the Windows account that runs the DB Advisor (or make that account `sysadmin`),
then set `[sandbox] server` in `db_advisor\config.toml` (`"localhost/SQLEXPRESS"` for Express).
No sandbox? Set `[sandbox] enabled = false`: every tool works except `test_sql`.

## 2. `.env` and `config.toml`

- **Workspace:** one folder holding the legacy and modern solutions, a copy made for this
  purpose (the build step writes `bin\` / `obj\` into it). Point `[paths]` in
  `code_advisor\config.toml` at it.
- **`db_advisor\.env`:** copy `.env.example` and fill in the **read-only** SQL login. It is
  never shipped, logged or committed.
- **`db_advisor\config.toml`:** leave `[paths] workspaces` empty (the Code Advisor's is used).

## 3. Build both advisors

Stop both servers first, then:

```powershell
.\full_pipeline.ps1             # first run
.\full_pipeline.ps1 -SkipSetup  # routine rebuild
```

| Advisor | Step | Script | What it does |
|---|---|---|---|
| code | setup | `scripts\setup.ps1` | `.venv` and the package |
| code | build hosts | `scripts\build-hosts.ps1` | the Roslyn hosts and the ASP.NET page-code bridge |
| code | build workspace | `scripts\build-workspace.ps1` | restores and builds every project, generates every page's code |
| code | ASP.NET controls | `scripts\gen_aspnet_controls.ps1` | the control table |
| code | reindex | `scripts\reindex.ps1` | the graph and the compiler index |
| code | tests | `scripts\test.ps1` | the test suite against the new index |
| db | setup | `scripts\setup.ps1` | `.venv`, packages, the two C# parsers |
| db | check prerequisites | `scripts\verify.ps1 -Stage prereq` | Python, ODBC, `.env`, read-only login, workspace, sandbox server |
| db | prepare + sandbox | `scripts\prepare.ps1 -Sandbox` | the index from the database and the code (read-only, `NOLOCK`), then the sandbox clone |
| db | check index | `scripts\verify.ps1 -Stage index` | every phase finished with sane counts, then the tests |

## 4. Run both servers

```powershell
powershell -ExecutionPolicy Bypass -File code_advisor\scripts\run.ps1
powershell -ExecutionPolicy Bypass -File db_advisor\scripts\run.ps1
```

Ports 8400 and 8401 by default (`config.toml [server]`). There is **no authentication**: keep
them on a trusted internal network, or use `-BindHost 127.0.0.1`.

## 5. Check both servers together

```powershell
powershell -ExecutionPolicy Bypass -File db_advisor\scripts\verify.ps1 -Stage server
```

It calls both servers over MCP and opens the C# lines the DB Advisor cites through the Code
Advisor's `read_file_range`, to prove both point at the same code. It must end with
`all checks passed`.

## 6. Connect an agent

Copy [`.mcp.example.json`](../.mcp.example.json) to your project as `.mcp.json` (change the host),
copy `AGENTS.md` into the project root, and ask the agent to "call `index_status` on both
servers".

## Keep it current

After every source update, and whenever views or procedures change: stop both servers, run
`.\full_pipeline.ps1 -SkipSetup`, start them again. Between rebuilds the DB Advisor flags
objects changed in the database since its build (`changed_since_build`).
