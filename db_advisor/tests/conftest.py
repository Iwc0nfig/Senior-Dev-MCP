from __future__ import annotations

import pytest
from mcp.server import MCPServer

from senior_dev_db.config import load_config
from senior_dev_db.tools import register_all
from senior_dev_db.tools.common import Ctx


@pytest.fixture(scope="session")
def cfg():
    try:
        return load_config()
    except FileNotFoundError as exc:
        pytest.skip(f"config not usable: {exc}")


@pytest.fixture(scope="session")
def ctx(cfg):
    if not cfg.paths.db_path.is_file():
        pytest.skip("index not built; run scripts/prepare.ps1")
    return Ctx.create(cfg)


@pytest.fixture(scope="session")
def tools(ctx):
    mcp = MCPServer("test")
    register_all(mcp, ctx)

    def call(_tool, **kwargs):
        return mcp._tool_manager.get_tool(_tool).fn(**kwargs)

    call.mcp = mcp
    return call
