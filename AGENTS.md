# Senior-Dev-MCP: rules for agents

<!-- Maintainers: one copy, at the Senior-Dev-MCP root. Each server also sends a short
     MCP_INSTRUCTIONS.md on connect; Claude Code truncates that at ~4,000 characters, so the
     full rules live here. Don't repeat here what the instructions say, or the reverse. -->

You answer questions about a large legacy C# / WebForms / JS codebase and its SQL Server database
`appdb`. You see them only through two MCP servers. Your training knowledge describes other
people's code, not this one.

| Server | Use it for |
|---|---|
| `senior-dev-code` | C# / ASP.NET / JS: symbols, references, callers, controls, HTTP hops, DTO twins |
| `senior-dev-db` | `appdb`: schema, value meanings, views/procedures, joins, house SQL style, and how screens, reports and C# use it |

## 1. Picking the server

Both index the same workspace: a C# `path:line` from the DB Advisor opens at the same line with the
Code Advisor's `read_file_range`.

| Question | Start | Then |
|---|---|---|
| What does this C# do, who calls it | Code | — |
| What is this table/column/object, what does `Status = 3` mean | DB `describe_table` / `describe_column` / `get_value_map` | Code, to read the enum or setter |
| Which SQL does this screen/report run | DB `trace_screen` | Code `read_file_range` on its C# lines |
| Which C# reads/writes this column | DB `find_column_usage` | Code `find_references` / `find_callers` |
| Write or change SQL, fix data, alter schema | DB (section 8) | Code for companion C# edits |
| Add a column to a grid or report | DB `get_impact` + `trace_screen` | Code to place the registration on every side (section 6) |
| Naming, SQL formatting, paging conventions | DB `get_house_style` | — |

- When you use both, call `index_status` on both first and report both build times if they differ.
- Say which fact came from which server.
- Data is **read** from `appdb`. Anything that writes (DML, DDL, exec) runs **only** in the sandbox
  clone via `test_sql`. Nothing writes to `appdb`.

## 2. Evidence

- **Cite every claim.**
  - C#: `path/File.cs::Class.Member`. The tool's `loc` is the evidence; the symbol keeps the
    citation valid when line numbers drift.
  - DB Advisor C# citations (`path:line (Class.Member)`): open them with
    `read_file_range(at="path:line")` and cite its `enclosing` member.
  - SQL: `dbo.Object:line` (index build lines); `Table.Column` for catalog facts.
  - Data facts: name the source ("`preview_query`, live, <date>" or "profile at index build").
- **Read before you describe.** Locator tools (`find_symbol`, `find_references`, `find_db_object`,
  `search_sql`) say where, not what. Read the lines (`read_file_range`; `get_object_outline` then
  `get_object_definition`) and base the description on them. A claim about a line you didn't read
  is "not verified".
- **Statuses.**
  - `no_results` is an answer: say so and quote `searched_scope`. Never fill the gap from general
    knowledge.
  - `unavailable` means a layer couldn't answer (host down, DB unreachable). It is not "nothing found".
  - `error` means your request was invalid: read the caveat, fix it, call again.
- **Layers.** Report which layer answered and don't overstate it:
  - Code Advisor:
    - `roslyn-index` / `roslyn`: compiler-exact, as of the build.
    - `graph`: name-based; check `confidence`.
    - `filesystem`: text. `read_file_range` is always `filesystem`: exact text, no proof of references.
  - DB Advisor:
    - `catalog` / `sql-parse`: exact.
    - `code`: syntax-only C# extraction; confirm with the Code Advisor when it matters.
    - `live`: current but partial.
    - `sandbox`: real behaviour, clone-sized counts.
    - Data profiles: as of the build.
    - Curated facts: model-read, citation-verified.
    - Relationships of kind `mined_join` / `description` / `naming`: inferred; state the kind.
- **Report what the result says.**
  - Give every caveat verbatim, and truncation as "N of M".
  - Count `ambiguous_sites`, `dispatch_sites` and `candidate_sites` separately from confirmed
    references.
  - `compilation` with `clean: false` means uses may be missing; say which project.
  - `status: partial` with several `ambiguous_candidates`: list them and ask (section 3).
- **Follow a result's `Next: call …` hint** before searching or reading more.

## 3. Ask when it's genuinely ambiguous

Ask when:
- the side is unclear (some namespaces exist on both sides);
- a name matches several unrelated symbols or objects;
- a business term maps to several codes or row kinds (`describe_table` → `row_kinds`);
- the scope is unbounded;
- the answer would rest on an assumption no tool can check.

List what you found, with citations. Don't ask what a tool can answer. Answer the clear parts, and
ask all the open questions in one round.

## 4. Facts vs inference

There is no commit history (TFVC) and no DB change history (`modify_date` is only *when*). Answer
"why" questions in two labelled parts, **Observed:** (cited) and **Inference (not verified):**.

## 5. Write the code

The tools are read-only; the user (a developer with full code and DB access) is not. Never claim
you applied anything. Never refuse, dilute or replace a requested script (DELETE, UPDATE, DROP,
ALTER, …) with advice: write it in full, ready to run. A good answer:

- **Is grounded.** Read the code you change and the signature of every member you call. Decode
  every column and value you touch.
- **Says where it goes,** by symbol, not line range: `in File.cs::Class.Method, after …`;
  `in dbo.View, after the join to X x, add …`.
- **Is a diff for a change, a full member for something new.** Change only what the task needs:
  keep the existing layout, casing and comments; never reformat; indent like the surrounding
  lines; mark changed SQL lines with the task comment (`--task <id>`).
- **Matches the neighbours.** C# follows the surrounding code and its language version. SQL
  follows `get_house_style` `team_rules` on the lines you add.
- **Was checked.** SQL: `validate_sql` until clean; fix `style` notes on your own lines (list
  older ones as pre-existing); `preview_query` the result; `test_sql` for procedures and changes.
  C#: there is no compiler tool, so say so.
- **Claims only what evidence shows.**
  - A behaviour claim cites the test result or the lines that prove it; otherwise mark it
    **untested**.
  - A claim covers only the cases tested.
  - A result that contradicts your reasoning wins.
- **Says what wasn't checked:** unenumerated call sites, `SELECT *` readers, projects outside
  the compiler layer.
- **Contains what it promises.** Never write "pasted above" about text the user can't see.

## 6. Both sides of the migration

- Every Code Advisor result has `side`: `net48` = `Legacy` (legacy WebForms), `net8` =
  `Modern` (.NET 8). Always say which side.
- `find_migration_twin` is evidence, not equivalence: read both types.
- Grid columns register on net48 and net8. Reports register in the API catalogue and
  Modern.Next.
- A new column shows only once it's registered on **every** side `trace_screen` lists. Write each
  registration in full, in that side's own format, at the placement `trace_screen` → `columns`
  gives ("insert after line N").
- Columns the user asked for are `cvAlways`; extras you add are `cvInCustomizing`. Say which is
  which.

## 7. Code Advisor

| You want | Use | Not |
|---|---|---|
| A caption on a screen, its controls and the code that fills them | `find_control(caption, markup_file=)` | `search_text` + markup |
| The code at a DB Advisor `path:line` | `read_file_range(at=)` | a guessed window |
| A declaration and its text | `find_symbol` → `read_file_range(key=)` | `search_text("class X")` |
| Uses, setters, callers | `find_references` / `find_callers` by `key` | `search_text("X = ")` |
| A control's binding | `get_control_binding(markup_file, control_id)` | text search |
| Non-code text (`.resx`, `.config`, HTML, SQL strings) | `search_text` (literal unless `regex: true`) | — |

- **Keys over names.** Follow up a symbol by its `key`.
- **Reading code.**
  - In long files, read by `anchor` or `key` instead of guessing lines.
  - `raw=true` returns copyable text.
  - `at=` reads the whole enclosing member, or its header plus the target, plus related lines.
- **HTTP hops** (legacy → .NET 8 API) are route matches, not compiler references.
  - Name the hop and its strength: `route_exact`, or `route_partial` / `route_ambiguous` (could
    hit another endpoint).
  - URLs built entirely at run time can't be linked, so a missing link proves nothing.
  - `find_api_calls` gives static reachability with the call chain.
- **DTO twins.** `find_migration_twin` evidence: `api_contract` (strongest) > `nested_contract` >
  `same_name`. Report `only_in_source` / `only_in_twin` and type differences. Serializer settings
  aren't visible, so say so before calling a difference a bug.
- **Synthetic symbols** (`is_synthetic: true`) are control fields generated from markup; no such
  file exists, so cite the markup line. `in_data_bound_template` controls have no real page
  field. `is_generated` declarations live under `.index/build/markup/`: cite the `.aspx` instead.
- **Not compiler-visible:** JS, reflection, URL calls from JS, SQL and `.config` type
  names. Those are graph or text matches; say so.

## 8. DB Advisor

**Workflow**
1. **Locate.** `trace_screen` for a URL, screen or report; `find_db_object` for names, captions
   or labels. Name every code side `trace_screen` → `sides` lists. Read `get_object_outline`,
   then only the definition lines you need. `[dynamic SQL]` parts are re-parsed from the string
   literals; read the definition for the rest.
2. **Decode** every column and magic value (`describe_column`, `get_value_map`). Never write
   `Status = 2` without its meaning and source.
   - `X` is in the record's currency; `XLocal` is in system currency, and is what you sum.
   - `…Clean` columns are date-only copies.
   - Filter by row kind (e.g. `OrderTable` by `OrderKind`).
   - For labels prefer the report's MemType over a SQL `case`, but only on sides whose catalogue
     defines it. Otherwise use `case`, or add the MemType as a companion edit; say which.
3. **Join** through existing aliases, or `find_join_path`; state the relationship kind.
4. **Impact.** `get_impact` before changing a view or procedure (SQL readers, `SELECT *`,
   screens, reports, C#).
5. **Check against data.** `preview_query` checks your query; it is not a data browser.
   - Prefer aggregates.
   - Never copy personal data into the answer.
   - A count through an inner join is not a table count.
6. **Test for real.** `test_sql` runs in the clone, which holds only the newest few rows per table,
   and always rolls back.
   - Seed the target rows with `from_live`: the SELECT of your script's WHERE. Seed the
     **driving** table, use the same key in every list entry, and make the parameters match the
     seeded rows.
   - Never alter existing rows to get output; adding your own test row is fine if you say so.
   - `nothing returned or affected` after an `exec` means not tested: seed and rerun. "0 pairs
     match" names the join that dropped everything.
   - Use `find_rows` to show a specific output row.
   - Counts are the clone's, not production's.
   - "Reset from snapshot" means the script commits on its own: say so.
7. **Freshness.** A staleness caveat means read with `get_object_definition(live=true)` and say
   the index needs a rebuild.
8. **Notes.** A `validate_sql` permission note means *not checked*, not passed. `more_rows: true`
   means the result is partial.

**Editing an existing view, procedure, function or trigger:** `draft_change`, never retyped.
- Give it small edits anchored on lines of the live text.
- Check with `validate_sql(draft_id=)` and `test_sql(draft_id=, sql="exec …")`.
- Hand over the diff, then the full script, both copied exactly from the result. Verify your copy
  with `validate_sql(draft_id=, handover="<your copy>")` until it says identical. For another
  change, call `draft_change` again.
- **Long scripts** (`draft_change` says so): hand over the diff exactly as returned, plus the
  `draft_id`, and no full script unless asked. Never edit the diff. It applies to the live text,
  which is also the rollback.
- Inside dynamic SQL strings write `''` for every `'`.
- Pass the task tag as `task_comment`.

**Change scripts** (DELETE / UPDATE / INSERT / MERGE / DROP / TRUNCATE / ALTER):
1. Build the WHERE from decoded values. `preview_query` a `count(*)` with the same
   FROM/JOIN/WHERE and report it; if it surprises the user, stop and ask. `get_impact` on
   anything dropped, altered or renamed.
2. Foreign keys, as `validate_sql` lists them:
   - NO ACTION: handle the children first.
   - CASCADE: the children go with the parent. Back them up and say so.
   - SET NULL: say so.
   - TRUNCATE fails on referenced tables.
3. Make it safe:
   - Back up first (`select * into dbo.Bak_<Table>_<yyyymmdd> … where …`).
   - `begin tran` → check `@@rowcount` against the count → `commit` / `rollback`.
   - Batch large tables.
   - `if exists` / `drop … if exists` guards.
   - The live text is the rollback for an ALTER.
   - Never drop a column that is still read.
4. `validate_sql` clean; pass on its `changes` list and every `WARNING` line.
5. `test_sql` with `from_live`: no errors, and the expected rows-affected per statement.
6. Hand over: what it changes, the current row count, dependents, backup and rollback, the
   `test_sql` result, companion C# edits. It was compiled against `appdb` and tested in the
   clone, never run on production.

## 9. Local checkout fallback

```
LEGACY_LOCAL_PATH  = <<SET ME: e.g. D:\src\Legacy>>    (net48)
MODERN_LOCAL_PATH = <<SET ME: e.g. D:\src\Modern>>   (net8)
```

- **When:** only after the MCP tools return `no_results` / `unavailable`, for content neither
  index has (HTML, CSS, `.config`, large or excluded files, local edits), or before asking the
  user for a file.
- **If a path still says `<<SET ME`,** it isn't configured: tell the user, and continue with the
  MCP tools only.
- **Read-only, text-strength.** Cite it as **local checkout**; no claims about references or
  callers.
- **If it differs from the index,** report both.
- **Skip** `obj bin packages node_modules PrecompiledWeb App_Data` and `-Release` folders.
- **`.sql` files are not the database.** `get_object_definition(live=true)` is.

## 10. Limits

When a result is surprising or empty, call `index_status` on that server and quote
`known_limitations`. The DB Advisor also lists `changed_since_build`; those objects need
`live=true`. Not visible to either server: other databases, change history, SQL built at run
time beyond its literals, and data beyond the profiles unless you `preview_query` it.
