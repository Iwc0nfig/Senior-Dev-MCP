"""Tool registration.

The guarantee is structural: no tool whose name suggests a mutation may register,
and the registered set must equal EXPECTED_TOOLS exactly.  validate_sql talks to the
live database, but only under SET NOEXEC ON with a read-only login (tools/live.py).
"""

from __future__ import annotations

FORBIDDEN = (
    "write", "edit", "patch", "apply", "delete", "create", "move", "rename",
    "exec", "execute", "run", "shell", "command", "install", "checkout", "commit",
    "insert", "update", "drop", "alter", "truncate",
)

EXPECTED_TOOLS = {
    "find_db_object",
    "trace_screen",
    "describe_table",
    "describe_column",
    "get_value_map",
    "get_object_definition",
    "get_object_outline",
    "find_column_usage",
    "find_join_path",
    "get_impact",
    "get_house_style",
    "search_sql",
    "validate_sql",
    "preview_query",
    "test_sql",
    "draft_change",
    "index_status",
}


def list_tool_names(mcp) -> list[str]:
    tm = getattr(mcp, "_tool_manager", None)
    if tm is not None:
        return [t.name for t in tm.list_tools()]
    import asyncio
    return [t.name for t in asyncio.run(mcp.list_tools())]


def register_all(mcp, ctx) -> list[str]:
    from . import columns, drafts, lineage, live, meta, objects, sandbox, style

    for mod in (objects, lineage, columns, style, live, drafts, sandbox, meta):
        mod.register(mcp, ctx)
    names = list_tool_names(mcp)
    bad = [n for n in names if any(f in n.lower() for f in FORBIDDEN)]
    if bad:
        raise AssertionError(f"read-only violation: mutating tool name(s) registered: {bad}")
    missing = EXPECTED_TOOLS - set(names)
    extra = set(names) - EXPECTED_TOOLS
    if missing:
        raise AssertionError(f"tools failed to register: {sorted(missing)}")
    if extra:
        raise AssertionError(f"unexpected tool(s) registered; add them to EXPECTED_TOOLS deliberately: {sorted(extra)}")
    return names
