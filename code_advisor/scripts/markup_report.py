"""Markup-parser measurements over the live App/ tree: directives, tags, type resolution, scripts."""

from __future__ import annotations

import collections
import sys

from senior_dev_code.config import load_config
from senior_dev_code.index import markup as mk


def main() -> int:
    cfg = load_config()
    ctx = mk.MarkupContext(cfg)
    root = cfg.paths.website_root
    files = sorted(
        p
        for p in root.rglob("*")
        if p.suffix.lower() in (".aspx", ".ascx", ".master") and p.is_file()
    )

    directives = collections.Counter()
    main_attrs = collections.Counter()
    prefixes = collections.Counter()
    resolutions = collections.Counter()
    no_id_tags = collections.Counter()
    server_tags = server_with_id = 0
    runat = runat_with_id = 0
    no_main = 0
    ids: set[str] = set()
    onclient = 0
    onclient_calls = 0
    server_handlers = 0
    scripts = scripts_resolved = 0
    inline_scripts = 0
    partial = 0
    unresolved_examples: list[str] = []

    for p in files:
        src = p.read_bytes()
        ex = mk.scan(src, p, ctx)
        if ex.parse_status != "ok":
            partial += 1
        if ex.page is None:
            no_main += 1
        else:
            directives[ex.page.directive] += 1
            if ex.page.master_type:
                directives["mastertype"] += 1
            directives["register"] += len(ex.page.registers)
            directives["import"] += len(ex.page.imports)
            directives["reference"] += len(ex.page.references)
            for k in ("code_file", "inherits", "language", "auto_event_wireup", "master_page"):
                if getattr(ex.page, k):
                    main_attrs[k] += 1
        for c in ex.controls:
            if c.tag_prefix:
                server_tags += 1
                prefixes[c.tag_prefix] += 1
                if c.ctl_id:
                    server_with_id += 1
                else:
                    no_id_tags[c.control_ref] += 1
            else:
                runat += 1
                if c.ctl_id:
                    runat_with_id += 1
            if c.ctl_id:
                ids.add((p.as_posix(), c.ctl_id))
            resolutions[c.resolution] += 1
            if c.resolution == "fallback_control" and len(unresolved_examples) < 12:
                unresolved_examples.append(f"{c.control_ref} @ {cfg.rel(p)}:{c.line}")
            server_handlers += len(c.server_handlers)
            for _, v in c.client_handlers:
                onclient += 1
                if mk.js_call_target(v):
                    onclient_calls += 1
        scripts += len(ex.scripts)
        scripts_resolved += sum(1 for s in ex.scripts if s.resolved_rel)
        inline_scripts += len(ex.inline_scripts)

    print(f"files: {len(files)}           no main directive: {no_main}   partial parse: {partial}")
    print("directives:", dict(directives))
    print("main-directive attrs:", dict(main_attrs))
    print(f"server tags: {server_tags:,}  (with id: {server_with_id:,})")
    print(f'html runat="server": {runat:,}  (with id: {runat_with_id:,})')
    print(f"distinct control IDs in markup: {len(ids):,}")
    print("prefixes:", dict(prefixes.most_common()))
    print("type resolution:", dict(resolutions.most_common()))
    print("top tags with no ID:", dict(no_id_tags.most_common(4)))
    print(f"OnClient* attrs: {onclient}   of which function calls: {onclient_calls}")
    print(f"server handler attrs: {server_handlers}")
    print(f"<script src>: {scripts}  resolved: {scripts_resolved}   inline <script> blocks: {inline_scripts}")
    if unresolved_examples:
        print("fallback_control examples:")
        for e in unresolved_examples:
            print("   ", e)
    if ctx.web_config_error:
        print("WEB.CONFIG:", ctx.web_config_error)
    if ctx.bcl_error:
        print("BCL:", ctx.bcl_error)
    return 0


if __name__ == "__main__":
    sys.exit(main())
