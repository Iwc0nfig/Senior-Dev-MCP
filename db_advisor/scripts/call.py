"""Call one DB Advisor tool from the shell (used by the example evaluation).

  python scripts/call.py --list [--db PATH]                 # server instructions + tool descriptions
  python scripts/call.py <tool> '<json args>' [--db PATH] [--log FILE]
  python scripts/call.py <tool> @args.json ...               # args from a file (long SQL)

--db serves another index file (an eval snapshot); --log appends {tool, args, bytes}
to FILE so the run can be audited and its token cost measured.
"""
import json
import sys
from dataclasses import replace
from pathlib import Path

from mcp.server import MCPServer

from senior_dev_db.config import load_config, render_instructions
from senior_dev_db.tools import register_all
from senior_dev_db.tools.common import Ctx


def opt(name):
    if name in sys.argv:
        i = sys.argv.index(name)
        v = sys.argv[i + 1]
        del sys.argv[i:i + 2]
        return v
    return None


def main():
    db = opt("--db")
    log = opt("--log")
    cfg = load_config()
    if db:
        cfg = replace(cfg, paths=replace(cfg.paths, db_file=Path(db).resolve()))
    mcp = MCPServer("call")
    register_all(mcp, Ctx.create(cfg))
    if "--list" in sys.argv:
        print(render_instructions(cfg))
        for t in mcp._tool_manager.list_tools():
            params = {k: v.get("type", v.get("anyOf", "")) for k, v in t.parameters.get("properties", {}).items()}
            print(f"\n## {t.name}({', '.join(params)})\n{t.description}")
        return
    raw = sys.argv[2] if len(sys.argv) > 2 else "{}"
    if raw.startswith("@"):                     # @args.json: avoids shell quoting of long SQL
        raw = Path(raw[1:]).read_text(encoding="utf-8")
    tool, args = sys.argv[1], json.loads(raw)
    out = mcp._tool_manager.get_tool(tool).fn(**args)
    d = out.model_dump(exclude_defaults=True)
    s = json.dumps(d, ensure_ascii=False)
    if log:
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps({"tool": tool, "args": args, "bytes": len(s.encode("utf-8"))}, ensure_ascii=False) + "\n")
    print(s)


if __name__ == "__main__":
    main()
