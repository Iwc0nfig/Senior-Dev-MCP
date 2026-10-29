"""The tool surface: exact set, read-only names and annotations, usable descriptions."""
from __future__ import annotations

import pytest

from senior_dev_db.tools import EXPECTED_TOOLS, FORBIDDEN


def _tools(tools):
    return tools.mcp._tool_manager.list_tools()


def test_exact_tool_set(tools):
    assert {t.name for t in _tools(tools)} == EXPECTED_TOOLS


def test_no_mutating_names(tools):
    for t in _tools(tools):
        assert not any(f in t.name.lower() for f in FORBIDDEN), t.name


# test_sql writes, but only inside the sandbox clone and always rolled back (tools/sandbox.py)
WRITES_IN_SANDBOX = {"test_sql"}


def test_read_only_annotations_and_descriptions(tools):
    for t in _tools(tools):
        assert t.annotations is not None, t.name
        assert t.annotations.read_only_hint is (t.name not in WRITES_IN_SANDBOX), t.name
        assert t.annotations.destructive_hint is False, t.name
        assert len(t.description or "") > 60, t.name


@pytest.mark.parametrize("name,kw", [
    ("describe_table", {"name": "NoSuchTableXyz"}),
    ("describe_column", {"table": "OrderTable.NoSuchColumnXyz"}),
    ("get_object_outline", {"name": "NoSuchProcXyz"}),
    ("trace_screen", {"target": "ui/manage/views/nosuchscreenxyz.aspx"}),
    ("get_value_map", {"query": "zzqqxxyy"}),
])
def test_no_results_is_grounded(tools, name, kw):
    out = tools(name, **kw)
    assert out.status == "no_results"
    assert out.searched_scope
    assert out.caveats, "a not-found answer must say what was searched / suggest"


def test_index_is_portable(ctx):
    """No absolute paths stored (the index is built on one machine and served from another)."""
    for table, col in [("code_entity_columns", "rel_path"), ("ui_views", "rel_path"), ("ui_reports", "rel_path"),
                       ("code_sql", "rel_path"), ("code_enums", "rel_path"), ("url_rules", "rel_path")]:
        bad = ctx.q1(f"SELECT count(*) FROM {table} WHERE {col} LIKE '_:%' OR {col} LIKE '/%'")[0]
        assert bad == 0, table


def test_no_credentials_in_index(ctx, cfg):
    from senior_dev_db.config import read_env
    env = read_env(cfg.db.env_file)
    secret = env.get("PASSWORDS")
    if not secret:
        pytest.skip("no env file")
    for (v,) in ctx.q("SELECT value FROM index_meta"):
        assert secret not in (v or "")
