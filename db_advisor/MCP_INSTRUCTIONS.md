This server answers questions about the application's SQL Server database 'appdb' (hundreds of
tables, views, procedures and functions) and how the C# application uses it. Knowledge about THIS
schema must come from a tool result. Full rules: AGENTS.md (shared with senior-dev-code).
Both servers index the same workspace, so every C# path:line cited here opens with the Code
Advisor's read_file_range(at=).

Workflow for a change request:
1. Locate:
   - a page URL, screen or report → trace_screen; name every side it lists;
   - a name, caption or label → find_db_object.
2. Understand: get_object_outline, then get_object_definition for only the lines you need.
3. Decode every column and magic value with describe_column / get_value_map.
4. Join through existing aliases, or find_join_path.
5. get_impact before changing a view or procedure.
6. Write in house style: get_house_style `team_rules` are binding on the lines you add.
7. Check:
   - validate_sql on the whole script;
   - preview_query on the result: read-only, at most {{db.preview_max_rows}} rows, prefer aggregates;
     it is not a data browser.
8. Test: test_sql runs the script in the sandbox clone (~{{sandbox.rows_per_table}} rows per table) and rolls back.
   - Seed the rows you target with from_live: the SELECT of your WHERE, the driving table,
     up to {{sandbox.max_from_live}} queries, the same key in each.
   - "nothing returned or affected" means not tested.

Existing view, procedure or function: never retype it. Use draft_change with anchored edits, then
pass its draft_id to validate_sql and test_sql. Hand over the diff and the script copied exactly
from the result; check your copy with validate_sql(handover=). Over {{drafts.long_script_lines}} lines: hand over the diff
exactly as returned plus the draft_id.

Change scripts (DML, DROP, ALTER): write them in full. Before handing over:
- a count with the same WHERE;
- foreign keys handled by their delete action;
- a backup and a transaction with an @@rowcount check;
- a rollback;
- validate_sql's WARNING lines passed on;
- a test_sql run.

Reads go to appdb; anything that writes runs only in the clone. A staleness caveat means read
the current text with get_object_definition(live=true). Report every caveat and the layer:
- catalog / sql-parse: exact;
- code: syntax-only C#;
- live: current, partial;
- sandbox: clone counts;
- mined_join / description / naming relationships: inferred.

If a result is surprising or empty, call index_status and quote known_limitations.
