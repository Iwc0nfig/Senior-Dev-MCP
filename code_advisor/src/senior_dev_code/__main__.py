"""``python -m senior_dev_code`` -- the read-only MCP server.

Transport, bind address and port come from config.toml [server]
(streamable-http: one long-lived process on the LAN rather than one subprocess
per client); the CLI flags override them.  The MCP instructions are
[server] instructions_file (MCP_INSTRUCTIONS.md).

Nothing here can write to the workspace: no mutating tool is registered (enforced
in tools/__init__.py) and the only path the process writes is the index directory.
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading

from mcp.server import MCPServer

from .config import load_config, render_instructions
from .tools import register_all
from .tools.common import Ctx

log = logging.getLogger("senior-dev-code")

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="senior-dev-code")
    ap.add_argument("--config", default=None)
    ap.add_argument(
        "--no-roslyn",
        action="store_true",
        help="do not start the Roslyn hosts; graph and filesystem layers only",
    )
    ap.add_argument(
        "--list-tools", action="store_true", help="print the tool surface and exit"
    )
    ap.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default=None,
        help="stdio for a locally spawned client; streamable-http to serve over the network",
    )
    ap.add_argument("--host", default=None, help="bind address for --transport streamable-http")
    ap.add_argument("--port", type=int, default=None, help="bind port for --transport streamable-http")
    ap.add_argument("--path", default=None, help="HTTP path the MCP endpoint is served on")
    ap.add_argument(
        "--stateless-http",
        action="store_true",
        help="do not keep per-client session state between requests",
    )
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,  # stdout is the MCP transport
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = load_config(args.config)
    srv = cfg.server
    transport = args.transport or srv.transport
    host = args.host or srv.host
    port = args.port or srv.port
    path = args.path or srv.path
    ctx = Ctx.create(cfg)

    mcp = MCPServer(
        "senior-dev-code",
        title="Senior-Dev-MCP Code Advisor",
        version="0.1.0",
        instructions=render_instructions(cfg),
    )
    names = register_all(mcp, ctx)
    log.info("registered %d read-only tools: %s", len(names), ", ".join(sorted(names)))

    if args.list_tools:
        for n in sorted(names):
            print(n)
        print(f"{len(names)} tools")
        return 0

    if not args.no_roslyn:
        # Load in the background: solutions take seconds to tens of seconds; until a
        # host is ready its tools answer "unavailable", never a silently weaker layer.
        def warm() -> None:
            try:
                states = ctx.pool.start_all(load=True)
                for side, st in states.items():
                    log.info(
                        "roslyn %s: %s (%s/%s projects, %s website docs, %s failures)",
                        side, st.state, st.projects_loaded, st.projects_total,
                        st.website_documents, st.failure_count,
                    )
            except Exception:
                log.exception("roslyn warm-up failed; compiler-exact tools stay unavailable")

        threading.Thread(target=warm, name="roslyn-warmup", daemon=True).start()

    try:
        if transport == "streamable-http":
            # DNS-rebinding protection is off unless TransportSecuritySettings is passed, so
            # binding 0.0.0.0 and connecting by LAN IP or hostname needs no Host-header config.
            # If this box becomes reachable more widely, add that setting -- see README.
            log.info(
                "serving MCP over streamable-http at http://%s:%d%s",
                host, port, path,
            )
            mcp.run(
                "streamable-http",
                host=host,
                port=port,
                streamable_http_path=path,
                stateless_http=args.stateless_http,
            )
        else:
            mcp.run()
    finally:
        ctx.pool.stop_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
