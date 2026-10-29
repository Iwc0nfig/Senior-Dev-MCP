"""End-to-end exercise of the tool surface against the real index.

Run with --roslyn to also start the compiler hosts.
"""

from __future__ import annotations

import argparse
import sys
import time

from mcp.server import MCPServer

from senior_dev_code.config import load_config
from senior_dev_code.tools import register_all
from senior_dev_code.tools.common import Ctx


def call(mcp, name, **kwargs):
    tool = mcp._tool_manager.get_tool(name)
    return tool.fn(**kwargs)


def show(title, env, *fields):
    print(f"\n=== {title} ===")
    print(f"  status={env.status}  layer={env.layer}  truncated={env.truncated}")
    print(f"  scope: {env.searched_scope[:150]}")
    for f in fields:
        v = getattr(env, f, None)
        if isinstance(v, list):
            print(f"  {f}: {len(v)}")
        else:
            print(f"  {f}: {v}")
    for cv in env.caveats:
        print(f"  ! {cv[:170]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--roslyn", action="store_true")
    a = ap.parse_args()

    cfg = load_config()
    ctx = Ctx.create(cfg)
    mcp = MCPServer("smoke")
    register_all(mcp, ctx)

    if a.roslyn:
        t0 = time.time()
        states = ctx.pool.start_all(load=True)
        for s, st in states.items():
            print(f"roslyn {s}: {st.state} {st.projects_loaded}/{st.projects_total} "
                  f"websitedocs={st.website_documents} err={st.error}")
        print(f"warm-up: {time.time()-t0:.1f}s")

    r = call(mcp, "index_status")
    show("index_status", r, "files_indexed", "lines_indexed", "symbols", "refs",
         "controls", "pages", "designer_files", "designer_fields", "parse_error_count")
    print(f"  refs_by_resolution: {r.refs_by_resolution}")
    print(f"  roslyn: { {k: (v.state, v.projects_loaded, v.website_documents) for k,v in r.roslyn.items()} }")

    r = call(mcp, "find_symbol", query="WheelsGrid", max=5)
    show("find_symbol('WheelsGrid')", r, "symbols")
    for s in r.symbols[:3]:
        print(f"     {s.side:<6}{s.kind:<10}{s.fqn} @ {s.loc.rel_path}:{s.loc.line_start}")

    # G3: a name that exists on both sides
    r = call(mcp, "find_symbol", query="Modern", max=20)
    show("G3 find_symbol('Modern') -- collision", r, "symbols")
    print(f"     sides present: {sorted({s.side for s in r.symbols})}")

    # G4: a name that does not exist
    r = call(mcp, "find_symbol", query="ZzNoSuchSymbolQqx", max=5)
    show("G4 find_symbol(nonexistent)", r, "symbols")

    # G1: control binding on the plan's named page
    r = call(mcp, "get_control_binding",
             markup_file="Legacy/App/ui/manage/master/order.aspx", max=400)
    show("G1 get_control_binding(order.aspx)", r, "inherits", "controls",
         "is_web_application_project")
    print(f"     codebehind: {r.codebehind.rel_path if r.codebehind else None} "
          f"({r.codebehind.line_end if r.codebehind else 0} lines)")
    print(f"     master: {r.master_page.rel_path if r.master_page else None}")
    typed = sum(1 for b in r.controls if b.resolved_type)
    print(f"     controls with a resolved type: {typed}/{len(r.controls)}")
    for b in r.controls[:3]:
        print(f"       {b.control_id or '(no id)':<26}{b.tag:<34}{b.resolved_type} "
              f"[{b.type_resolution}] synth={b.codebehind_field.is_synthetic if b.codebehind_field else None}")

    r2 = call(mcp, "get_page_assets", markup_file="Legacy/App/ui/manage/master/order.aspx")
    show("get_page_assets(order.aspx)", r2, "scripts", "styles", "user_controls",
         "server_handlers", "client_handlers", "imports")
    for s in r2.scripts[:3]:
        print(f"     {s.confidence:<10}{s.resolved_rel} <- {s.raw[:60]}")

    # G7: reading a synthetic designer path must refuse
    row = ctx.con.execute(
        "SELECT synth_path FROM pages WHERE synth_path IS NOT NULL LIMIT 1").fetchone()
    if row:
        r = call(mcp, "read_file_range", file=ctx.cfg.synth_abs(row["synth_path"]))
        show("G7 read_file_range(synthetic designer)", r, "file")

    r = call(mcp, "read_file_range",
             file="Legacy/App/ui/manage/master/order.aspx", line_start=1, line_end=3)
    show("read_file_range(order.aspx 1-3)", r, "total_lines", "line_start", "line_end")
    for l in r.lines:
        print("   ", l[:120])

    r = call(mcp, "search_text", pattern="ResolvePublicUrl", extensions=["aspx"], max=5)
    show("search_text('ResolvePublicUrl')", r, "hits", "files_searched")

    # G5: ambiguity must be visible
    r = call(mcp, "find_references", symbol="Page_Load", max=20)
    show("G5 find_references('Page_Load') -- graph layer", r, "references",
         "ambiguous_sites", "ambiguous_candidates")

    r = call(mcp, "find_migration_twin", symbol="WheelsGrid", max=5)
    show("find_migration_twin('WheelsGrid')", r, "source_symbols", "twins")
    for t in r.twins[:3]:
        print(f"     {t.match_kind:<26}{t.symbol.side} {t.symbol.fqn}")

    if a.roslyn:
        r = call(mcp, "get_dependency_graph", level="project")
        show("get_dependency_graph(project)", r, "projects", "edges", "cycles")
        for p in sorted(r.projects, key=lambda x: -x.documents)[:5]:
            print(f"     {p.name:<28}{p.documents:>6} docs  synthetic={p.is_synthetic}")

        cb = ctx.con.execute("""
            SELECT f.rel_path, r.line, r.col, c.ctl_id FROM controls c
            JOIN files f ON f.file_id=c.codebehind_file
            JOIN refs r ON r.file_id=c.codebehind_file AND r.name=c.ctl_id
            WHERE c.used_in_codebehind=1 AND f.rel_path LIKE 'Legacy/App/%' LIMIT 1
        """).fetchone()
        if cb:
            r = call(mcp, "find_references", file=cb["rel_path"], line=cb["line"],
                     col=cb["col"], max=20)
            show(f"G2 find_references(synth control '{cb['ctl_id']}') -- roslyn", r,
                 "references", "total_available")
            if r.target:
                print(f"     target: {r.target.fqn}  synthetic={r.target.is_synthetic}")
                print(f"     cited at: {r.target.loc.rel_path}:{r.target.loc.line_start}")

    ctx.pool.stop_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
