"""``python -m senior_dev_db`` -- the read-only MCP server for the application database.

Transport, bind address and port come from config.toml [server] (streamable-http:
one long-lived process on the LAN); the CLI flags override them.  The MCP instructions
are [server] instructions_file (MCP_INSTRUCTIONS.md).  All answers come from the prepared index
(.index/dbadvisor.db); validate_sql (compile-only), preview_query (low-limit SELECT)
and live definitions/staleness checks are the only access to appdb.  test_sql runs
scripts in a separate sandbox clone (always rolled back), never in appdb.
"""

from __future__ import annotations

import argparse
import logging
import sys

from mcp.server import MCPServer

from .config import load_config, render_instructions
from .tools import register_all
from .tools.common import Ctx

log = logging.getLogger("senior-dev-db")

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="senior-dev-db")
    ap.add_argument("--config", default=None)
    ap.add_argument("--list-tools", action="store_true")
    ap.add_argument("--transport", choices=("stdio", "streamable-http"), default=None)
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--path", default=None)
    ap.add_argument("--stateless-http", action="store_true")
    ap.add_argument("--db", default=None, help="serve this index file instead of .index/dbadvisor.db (eval snapshots)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config)
    if args.db:
        from dataclasses import replace
        from pathlib import Path
        cfg = replace(cfg, paths=replace(cfg.paths, db_file=Path(args.db).resolve()))
    srv = cfg.server
    transport = args.transport or srv.transport
    host = args.host or srv.host
    port = args.port or srv.port
    path = args.path or srv.path
    ctx = Ctx.create(cfg)
    mcp = MCPServer("senior-dev-db", title="Senior-Dev-MCP DB Advisor", version="0.1.0",
                    instructions=render_instructions(cfg))
    names = register_all(mcp, ctx)
    log.info("registered %d read-only tools: %s", len(names), ", ".join(sorted(names)))
    if args.list_tools:
        for n in sorted(names):
            print(n)
        return 0
    if transport == "streamable-http":
        log.info("serving MCP over streamable-http at http://%s:%d%s", host, port, path)
        mcp.run("streamable-http", host=host, port=port, streamable_http_path=path,
                stateless_http=args.stateless_http)
    else:
        mcp.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
