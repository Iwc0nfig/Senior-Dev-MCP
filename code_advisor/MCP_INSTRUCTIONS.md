This server answers questions about a large C# / ASP.NET WebForms / JavaScript codebase
that you can see only through these tools. Your general C#/ASP.NET knowledge describes other
people's code, not this one. Full rules: AGENTS.md (shared with senior-dev-db).

Two workspaces, tagged on every result as `side`:
- net48 = Legacy: legacy .NET Framework / WebForms.
- net8 = Modern: .NET 8.

The `Modern` namespace exists on BOTH sides. Always say which side.

Tool map:
- caption on a screen → find_control
- path:line (e.g. from the DB Advisor) → read_file_range(at=)
- declaration → find_symbol, then read_file_range(key=)
- uses / callers → find_references / find_callers (pass the symbol's key)
- control ↔ code-behind → get_control_binding
- endpoints a page or method reaches → find_api_calls
- legacy ↔ new DTO → find_migration_twin
- non-code text → search_text (literal unless regex=true; skips files over {{index.max_file_bytes}} bytes
  unless path_glob selects them)

Essentials:
- Locators say WHERE. Read the lines with read_file_range before describing behaviour; it is
  always layer "filesystem", the exact text.
- Report the layer:
  - roslyn-index / roslyn: compiler-exact.
  - graph: name-based; check `confidence`.
  - filesystem: text only.
- `no_results` is an answer: quote `searched_scope`. `unavailable` is not "nothing found".
- Report every caveat; ambiguous / dispatch / candidate sites separately from confirmed
  references; `compilation.clean = false` means uses may be missing.
- When a caveat says "Next: call …", call it before searching more.
- `is_synthetic: true` is a control field generated from markup, with no file on disk: cite the
  markup line.
- HTTP links between legacy calls and API endpoints are route matches, not compiler references.
  Say so and give the strength (route_exact / route_partial / route_ambiguous).
- Read-only: no tool writes or executes. Write the change in your answer; never claim you
  applied it.
- If a result is surprising or empty, call index_status and quote known_limitations. It is compact
  by default; detail=true lists every project that compiled with binding errors.
