# Senior-Dev-MCP DB Advisor

A read-only MCP server that gives an agent accurate, cheap access to a SQL Server
database (`appdb`) **and** to what the C# code says about it. It's the database counterpart of
the Senior-Dev-MCP Code Advisor (`../code_advisor`) and follows the same design:

- Heavy preparation up front.
- One SQLite index.
- Small, structured, cited answers.
- A layer label on every result.
- A strictly read-only tool surface.

## Why code + DB

Many facts about the DB live only in the code, or in the data:

- Status codes such as `OrderTable.Status`, where `IsClosed()` covers 2 and 3.
- The meaning of `PaymentLogTable.PayMethod = 6`.
- Which SQL view a grid page queries.
- Which procedure a report runs, and with which parameters.
- Where a new column must be registered before a screen shows it.

Preparation merges four sources, keeps the provenance of each fact, and reports disagreements
instead of hiding them:

1. The SQL Server catalog.
2. A ScriptDom parse of every module.
3. A Roslyn syntax extraction of the C# workspace.
4. Data profiles.

This file is the developer guide. Install, deploy and run commands for everyone are in the root
[`README.md`](../README.md); the grounding contract clients follow is the root
[`AGENTS.md`](../AGENTS.md); what the server tells every client is
[`MCP_INSTRUCTIONS.md`](MCP_INSTRUCTIONS.md) (`{{section.key}}` placeholders are filled from
`config.toml`).

## Layout

| Path | What |
|---|---|
| `src/senior_dev_db/prep/` | Preparation phases. Each owns its tables and can be re-run alone |
| `config.toml` | The only place for paths, connection settings, timeouts, limits and `[server]`; relative paths resolve against this folder. Scripts read it with `python -m senior_dev_db.config get <section.key>` |
| `db_advisor_pipeline.ps1` | setup → `verify -Stage prereq` → `prepare -Sandbox` → `verify -Stage index`, each a script in `scripts/` |
| `src/senior_dev_db/tools/` | The 17 MCP tools |
| `sql-parser/` | C# (net8) console app using Microsoft ScriptDom. It extracts aliases, joins, output lineage, column references, literal filters and calls from every module |
| `code-extractor/` | C# (net8) console app using Roslyn syntax trees. It extracts enums, ValueEnums, MemTypes, consts, entity↔table maps, column reads, MasterView/Report registrations and SQL strings |
| `knowledge/curated/` | Haiku-curated facts (raw output, plus `*.verified.json` holding only facts whose citations verified) |
| `tests/` | Contract and config tests (the golden tests and `tests/eval/` are written per client, against their database) |
| `.index/` | `dbadvisor.db` (served), `building.db` (staging), `stage/`, `eval/`, `logs/` |

## End to end

On the server, follow `../docs/SETUP.md`: it's the full runbook, with what to do when a check
fails. `db_advisor_pipeline.ps1` runs step 3 below except `run` and `-Stage server`. On a dev
box:

1. **Install:**
   - Python 3.12+;
   - the .NET 8+ SDK;
   - ODBC Driver 17 or 18 for SQL Server;
   - a local SQL Server 2022 Express or Developer for the sandbox clone. Run
     `Get-Service 'MSSQL*'` to see its instance; `config.toml [sandbox] server` must name it
     (`MSSQL$SQLEXPRESS` → `localhost/SQLEXPRESS`, `MSSQLSERVER` → `localhost`).
2. **Configure:**
   - `.env` from `.env.example`: `URL`, `LOGIN` and `PASSWORDS` of the read-only login;
   - `config.toml`: leave `[paths] workspaces` empty (the Code Advisor's is used, through
     `[code_advisor] root = "../code_advisor"`), and set `[sandbox] server`.
3. **Run, in order.** Each `verify` stage must end with `all checks passed` before the next
   step.

   ```bash
   powershell -ExecutionPolicy Bypass -File scripts/setup.ps1
   ```

   ```bash
   powershell -ExecutionPolicy Bypass -File scripts/verify.ps1 -Stage prereq
   ```

   ```bash
   powershell -ExecutionPolicy Bypass -File scripts/prepare.ps1 -Sandbox
   ```

   ```bash
   powershell -ExecutionPolicy Bypass -File scripts/verify.ps1 -Stage index
   ```

   ```bash
   powershell -ExecutionPolicy Bypass -File scripts/run.ps1
   ```

   Then, in a second window, with the Code Advisor also running:

   ```bash
   powershell -ExecutionPolicy Bypass -File scripts/verify.ps1 -Stage server
   ```

4. **Connect an agent:** add both servers to its MCP config and put `AGENTS.md` in the project
   root (`docs/SETUP.md`, "Connect an agent").

`verify -Stage index` checks that the sandbox clone is built, so build it (`-Sandbox`, or
`scripts/sandbox.ps1`) before that stage. Without a local SQL Server, set
`[sandbox] enabled = false`: the checks then warn instead of failing, and only `test_sql` is
unavailable.

## Prepare

Requirements:

- .NET 8+ SDK.
- ODBC Driver 17 or 18 for SQL Server.
- `.env` with `URL`, `LOGIN` and `PASSWORDS` (read-only login).
- The workspace checkout path in `config.toml`.
- For `-Sandbox`: a local SQL Server (see "Sandbox clone").

```bash
powershell -File scripts/prepare.ps1
```

Stop the server first: Windows won't replace an index file the server holds open.
`-Phases link,style,search` re-runs some phases only; `-Sandbox` also rebuilds the sandbox
clone afterwards.

| Phase | What it collects | Typical time |
|---|---|---|
| `catalog` | Objects, columns, parameters, FKs, indexes, `MS_Description`, module text, engine dependencies, view column lineage | 10 s |
| `profile` | Distinct values and counts for every short scalar column | ~6 min |
| `lookups` | Rows of small reference tables (id → name) | 1 s |
| `sqlparse` | ScriptDom over 1,156 modules | 6 s |
| `code` | Roslyn syntax extraction over about 5,000 `.cs` files | 10 s |
| `link` | Value maps per column, conflicts, relationships (FK, mined joins, descriptions, naming), currency and date notes, screen → object targets | 5 s |
| `curated_load` | Loads verified Haiku facts | — |
| `style` | Measured conventions, idioms and C# framework anchors | ~2 min |
| `search` | FTS5 surface for `find_db_object` | 1 s |

## Curation (Haiku)

`python -m senior_dev_db.prep.packets` writes work packets for the gaps only:

- Enum-like columns that are used but have no trustworthy value list.
- Heavily used columns with no description.

Each packet goes to a Haiku subagent in a Claude Code session, which writes
`knowledge/curated/<packet>.json`. Then:

```bash
python -m senior_dev_db.prep.verify
```

```bash
powershell -File scripts/prepare.ps1 -Phases curated_load,search
```

The verifier keeps a fact only if every cited `path:line` or `Object:line` exists and contains a
token of the fact. Packets are content-hashed, so a refresh only asks about what changed.

## Sandbox clone (test_sql)

`test_sql` runs procedures and change scripts **for real** in a clone of `appdb`, then rolls
them back.

```bash
powershell -File scripts/sandbox.ps1
```

It needs a SQL Server we fully own, on the same machine: SQL Server 2022 Express or
Developer, with the account that runs the advisor as `sysadmin` (it creates databases and a
snapshot). `verify -Stage prereq` checks that it's reachable and that the login can
`CREATE DATABASE`. An `Error Locating Server/Instance Specified` there means no instance of
that name is running: install one, start its service, or fix `[sandbox] server`.

This builds `[sandbox] database` on `[sandbox] server` (config.toml) in under a minute:

- appdb' schema: tables, identities, keys, defaults, indexes, and foreign keys with their
  cascade rules;
- every view, function, procedure and trigger;
- the newest ~10 rows of each table, small reference tables whole, plus every parent row those
  rows reference, so the foreign keys hold;
- the partner row of each of those rows in tables that share its primary key without a declared
  foreign key (`ReportOrderTable.Id` = `OrderTable.Id`, `*_RemarksTable.EntityID`, ...): strong
  inferred links from the index (`prep/sandbox.py partner_pairs`), so joins between them find rows;
- a database snapshot.

A rebuild only drops a database that carries the builder's `advisor_sandbox` marker. Objects that
fail (they're broken in appdb too) are listed in `.index/sandbox_report.json` and by
`index_status`. `prepare.ps1 -Sandbox` rebuilds it after the index.

Each `test_sql` call:

1. connects to the sandbox only;
2. impersonates `sandbox_runner` (db_owner of the clone; no server rights, no other database)
   with a cookie the script never sees;
3. runs the script in `begin tran` and always rolls back.

If the script ends that transaction itself, the clone is restored from the snapshot. A guard
refuses USE, other databases, server settings, logins, backup and so on.

`from_live` (optional) is a `select * from <Table> where …` run read-only against appdb
(`preview_query`'s guard), or a list of up to 8 of them (one per table the script reads). Inside the
same rolled-back transaction it copies into the clone:
- up to `seed_max_rows` (50) matching rows;
- their direct child rows;
- their partner rows (same primary key, no declared foreign key);
- every parent row those need, capped at `seed_max_related`.

Constraints and triggers on those tables are off while the rows load, then switched back on
(`tools/sandbox.py _Seed`). This lets a script be tested against the old rows it targets, which
the newest-rows sample doesn't contain. The first event counts what was copied and what was already
in the clone. When an `exec` returns nothing, a caveat names each inner join of the procedure whose
two tables have no matching rows in the clone.

`draft_id` (optional, also on `validate_sql`) runs a draft from `draft_change` as the first batch,
before `sql`.

Every result set is read to the end (up to 100,000 rows) and counted: `total_rows`, and an event
`result set 1 (10 of 534 rows)`. `find_rows` (optional, `{column: value}`, case-insensitive, `%`
wildcard) returns only the rows of a result set that match, so a row the script added can be seen.
The sandbox lock (one run at a time across processes) is released explicitly after each run;
with ODBC connection pooling, closing the connection alone would leave it held.

## C# citations name their member (prep phase code_members)

Every C# fact this server gives (entity column reads, SQL strings, report and grid registrations, enums)
comes from the syntax extraction as `path:line`. The `code_members` phase runs after `link`:

1. It opens the Code Advisor's index read-only: `[code_advisor] root`, then its `config.toml`
   `[paths] index_dir`, then `graph.db`.
2. For each cited line, it stores the innermost declaration that contains it, from the compiler index
   `rsymbols`, else the structural `symbols`.
3. The tools then cite `path:line (Class.Member)`. `trace_screen`'s registration steps also carry the
   Code Advisor key.

On this box, 99.7% of the 52,263 cited lines get a member, in about 1 second. Without a Code Advisor
index the phase is skipped with a warning, and citations stay `path:line`. The table is created by the
phase itself, so adding it does not change the schema fingerprint (which would drop the whole index):

```bash
powershell -File scripts/prepare.ps1 -Phases code_members,search
```

`trace_screen` also returns `columns`: every registered report or grid column per side, as
`DataField@line`, capped at 80 per side (`max_columns=0` lists all). When the sides register different
columns, a caveat says which side lacks which; it compares all of them, not just the ones shown.
Each side's line ends with where a new column goes (`insert new columns after line 760 (end of Domain),
before line 762 (end of the Columns array)`). This is worked out when the tool runs from the C# file in
the workspace: the first `}` at the last `new Column` line's indentation, then the next non-blank line.
`describe_table(columns=[...])` gives exact columns; `columns_like` stays a substring filter, and both
say how many columns they show.
`validate_sql` of a view/procedure/function replacement says so (rollback = its current text) instead of
the data-change advice, and for a procedure it says output columns are checked with `test_sql`.
`find_db_object` with a caption-like query (several words, or ending in `:`) that matches no screen or
report points to the Code Advisor's `find_control(caption=...)`. Page captions in `.aspx` markup and
`.resx` files are not in this index.

## Editing an existing module (draft_change)

`draft_change(name, edits)` reads the live text of a view, procedure, function or trigger and applies
small edits anchored on its lines. Each edit has an `anchor` (text found on exactly one line) or a
`line` number, an action (`insert_after`, `insert_before`, or `replace` with an optional `through`
anchor), and the new text. It returns:

- the whole script, with `CREATE` turned into `ALTER` and every other line byte for byte;
- a unified diff and the changed line numbers;
- style notes on the changed lines only;
- a `draft_id`.

Pass the `draft_id` to `validate_sql` and `test_sql` instead of pasting the script. Drafts are kept
in the server process for 2 hours (`tools/drafts.py`). Nothing is written anywhere.

An edit that lands inside a string literal (the dynamic SQL of report procedures) gets a caveat:
write `''` for every `'`. A lone quote in such an edit is warned about (it ends the string, which is
right only for concatenation). An edit that changes which of the live text's lines are inside a
string is refused (`status: "error"`).

Added lines indented with tabs, next to live lines that are tab-indented, get no tab note: they follow
the object's own indentation. Every other team rule still applies to them. `task_comment` is the tag
each changed line must carry (`--task 12345`); a longer description passed with it need not be
repeated on every line.

Other result-size and naming rules:
- `get_impact` on an object with more than 40 dependents lists the direct ones and counts the deeper
  levels, and lists at most 20 companion edits. `detail=true` gives everything.
- `describe_column` and `get_value_map` say when a MemType is linked to the column. Reports can then show
  its labels themselves (`beMemType`). The hint includes how many report columns already do, and whether
  its labels differ from the code enum's.
- A few parameters accept the name agents reach for first: `search_sql(query=...)`,
  `get_value_map(name=... | table=...)`, `describe_column(name=...)` and `find_db_object(name=...)`.

## Serve

```bash
powershell -File scripts/run.ps1
```

This serves what `config.toml [server]` says (`http://0.0.0.0:8401/mcp`), with no
authentication; `-BindHost 127.0.0.1` keeps it local. For a stdio client run
`.venv\Scripts\python.exe -m senior_dev_db --transport stdio`.

## Tests

```bash
.venv/Scripts/python -m pytest -q -m "not live and not sandbox"   # offline: 57 pass, 1 skip without .env (2026-10-27)
.venv/Scripts/python -m pytest -q -m live                         # needs the database (.env)
.venv/Scripts/python -m pytest -q -m sandbox                      # needs the sandbox clone
```

The `live` tests (`validate_sql`, `preview_query`, live definitions) need the database.

## Evaluation against the examples

```bash
python -m senior_dev_db.prep.eval_mask
```

This builds `.index/eval/ex<N>.db`. In each snapshot, the objects an example touches are rolled
back to its `_OLD.sql`, and the C# registrations and curated facts for the identifiers its
"after" adds are hidden, because the live DB and the code already contain every "after".
The agent under test may use only `scripts/call.py --db .index/eval/ex<N>.db`.
Rubrics go in `tests/eval/rubrics.json` and results in `tests/eval/results/`. The examples are
real past tickets from the client's own tracker, so they are not part of this public copy.

## Agent rules

The root `AGENTS.md` holds the operating rules for agents that use **both** servers: the Code
Advisor (`senior-dev-code`) and this one (`senior-dev-db`). There is one copy.

## For maintainers

- `config.toml` is the only place for paths and settings (`tests/test_config.py` fails on a port
  or machine path in `src/` or `scripts/`). Credentials come from `.env` only and
  are never stored in the index or returned by a tool (`tests/test_tools_contract.py` checks this).
- The index is derived: a schema change (fingerprint) drops and rebuilds it. Each phase replaces
  only its own tables, so phases can be re-run alone.
- No absolute paths in the index: code locations are workspace-relative.
- A failed source never stops the build. It's recorded (parse errors, undescribable views,
  unresolved values) and reported by `index_status`.
- Tool names may not contain mutating verbs (`tools/__init__.py` asserts this at registration),
  and the registered set must equal `EXPECTED_TOOLS`.
- Runtime DB access is limited to:
  - `validate_sql`: `SET NOEXEC ON`, sent as a separate batch before anything else. For change
    scripts (DML/DDL), it also reviews what each statement touches, from SqlParser's `writes` and
    the index (`tools/live.py _write_review`).
  - `preview_query`: one SELECT/WITH only, guarded (`tools/live.py guard_select`), `SET ROWCOUNT`,
    low row and time limits in `config.toml [db]`.
  - `get_object_definition(live=true)`, `draft_change` (the same read of the live text) and the
    staleness check (one `sys.objects` read, cached for 60 s).

  All of them use the read-only login and a ReadOnly-intent connection. In eval snapshots, live data
  for masked objects is refused.
- `test_sql` is the only tool that writes, and only in the sandbox clone (`tools/sandbox.py`),
  always rolled back; its annotations say `readOnlyHint=false, destructiveHint=false`, and
  `tests/test_tools_contract.py` whitelists exactly that tool. It never writes to appdb: its only
  connection to appdb is `from_live`, which reads the target rows with the read-only login
  (`preview_query`'s guard) and copies them into the clone.
- The rule, in one line: **data is read from appdb; anything that needs write permission runs in
  the clone.**
- Curated facts enter the index only through `prep/verify.py`.

## Deploy

Both advisors ship together: `python build_mcp.py` at the root builds `dist\mcp.zip`
(sources, prebuilt parsers, an offline wheelhouse, `config.toml` as it is; never `.env` or
`.index`). `docs/SETUP.md` covers unzipping and running it. `verify -Stage server` also
proves that the DB Advisor's C# citations open at the same line in the Code Advisor.
