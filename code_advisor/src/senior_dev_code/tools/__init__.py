"""Tool registration.

The guarantee is that no mutating tool **exists**.  MCP
``annotations`` such as ``readOnlyHint`` are explicitly *hints* -- the spec says
clients "MUST consider tool annotations to be untrusted unless they come from
trusted servers".  So the real guarantee is the assertion below, enforced at
registration, plus this process never opening a file for writing outside ``.index/``.
"""

from __future__ import annotations

FORBIDDEN = (
    "write", "edit", "patch", "apply", "delete", "create", "move", "rename",
    "exec", "execute", "run", "shell", "command", "install", "checkout", "commit",
)

EXPECTED_TOOLS = {
    "find_symbol",
    "go_to_definition",
    "find_references",
    "find_callers",
    "find_implementations",
    "get_type_info",
    "get_dependency_graph",
    "find_dead_code",
    "read_file_range",
    "search_text",
    "get_control_binding",
    "find_control",
    "get_page_assets",
    "find_migration_twin",
    "find_api_calls",
    "index_status",
    "get_source_manifest",
    "read_source_batch",
}


def list_tool_names(mcp) -> list[str]:
    """Tool names, without needing an event loop."""
    tm = getattr(mcp, "_tool_manager", None)
    if tm is not None:
        return [t.name for t in tm.list_tools()]
    import asyncio

    return [t.name for t in asyncio.run(mcp.list_tools())]


def register_all(mcp, ctx) -> list[str]:
    from . import api, files, graph, meta, refs, source_export, symbols, webforms

    for mod in (symbols, refs, graph, files, webforms, meta, api, source_export):
        mod.register(mcp, ctx)

    names = list_tool_names(mcp)

    # Structural guarantee, enforced at registration, not by prompt.
    bad = [n for n in names if any(f in n.lower() for f in FORBIDDEN)]
    if bad:
        raise AssertionError(f"read-only violation: mutating tool name(s) registered: {bad}")

    missing = EXPECTED_TOOLS - set(names)
    extra = set(names) - EXPECTED_TOOLS
    if missing:
        raise AssertionError(f"tools failed to register: {sorted(missing)}")
    if extra:
        raise AssertionError(
            f"unexpected tool(s) registered; add them to EXPECTED_TOOLS deliberately: "
            f"{sorted(extra)}"
        )
    return names
