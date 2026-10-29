from __future__ import annotations

import pytest

from mcp.server import MCPServer

from senior_dev_code.config import load_config
from senior_dev_code.index import markup as mk
from senior_dev_code.tools import register_all
from senior_dev_code.tools.common import Ctx


@pytest.fixture(scope="session")
def cfg():
    """The deployment's own config.

    ``load_config`` honours ``$SENIOR_DEV_CONFIG``, so the suite can run against another
    config.  A config pointing at a tree that is
    not on this machine is a skip, not an error: the suite is about the index, and
    without the workspace there is nothing to check it against.
    """
    try:
        return load_config()
    except (FileNotFoundError, NotADirectoryError) as exc:
        pytest.skip(f"config not usable on this machine: {exc}")


@pytest.fixture(scope="session")
def indexed(cfg):
    """Skip the whole suite politely if the index has not been built."""
    if not cfg.paths.db_path.is_file():
        pytest.skip("graph.db not built; run scripts/reindex.py")
    return True


@pytest.fixture(scope="session")
def ctx(cfg, indexed):
    return Ctx.create(cfg)


@pytest.fixture(scope="session")
def tools(ctx):
    mcp = MCPServer("test")
    register_all(mcp, ctx)

    def call(name, **kwargs):
        return mcp._tool_manager.get_tool(name).fn(**kwargs)

    call.mcp = mcp  # type: ignore[attr-defined]
    return call


@pytest.fixture(scope="session")
def markup_ctx(cfg):
    return mk.MarkupContext(cfg)


@pytest.fixture(scope="session")
def app_markup_files(cfg):
    root = cfg.paths.website_root
    if not root.is_dir():
        pytest.skip(f"{root} not present")
    return sorted(
        p
        for p in root.rglob("*")
        if p.suffix.lower() in (".aspx", ".ascx", ".master") and p.is_file()
    )
