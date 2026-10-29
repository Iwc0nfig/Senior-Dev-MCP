"""read_file_range and search_text.

``read_file_range`` is the grounding primitive: the graph tells you WHERE code
is, this is how you find out what it says.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, Optional, Union

from mcp.types import ToolAnnotations

from ..inventory import INDEXED_EXTS, is_excluded_dir, is_vendor_js
from ..models import EnclosingMember, Loc, ReadFileOut, SearchHit, SearchTextOut
from . import common as c
from .common import Ctx

RO = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

SEARCHABLE_EXTS = set(INDEXED_EXTS) | {
    ".html", ".htm", ".css", ".config", ".sql", ".pas", ".txt", ".md", ".json", ".xml",
    ".csproj", ".sln", ".master", ".resx",
}

# a literal pattern that looks like it was meant as a regular expression
LOOKS_LIKE_REGEX = re.compile(r"\||\.\*|\.\+|\\[bdswBDSW.()\[\]]|^\^|\$$|\(\?|\[[^\]]+\]")

MEMBER_WINDOW = 40       # lines either side of the target when the enclosing member is too long to read whole
HEADER_MAX = 5           # signature lines shown before that window
MEMBER_KINDS = ("method", "constructor", "property", "indexer", "event", "field", "class", "struct",
                "record", "interface", "enum", "delegate")
AT_RE = re.compile(r"^(?P<file>.+?):(?P<start>\d+)(?:-(?P<end>\d+))?$")


CHAIN_RE = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+")
RELATED_MAX = 30         # related lines shown after the window
RELATED_COMMON = 60      # a chain on more lines of the member than this says nothing


def related_lines(lines: list[str], m_start: int, m_end: int, target: int, w_start: int, w_end: int):
    """Lines of a long member, outside the shown window, that work on the same thing as the target line:
    the shortest receiver chain on it (masterEntity.FeedBack in 'FeedStatus.Text = masterEntity.FeedBack
    .Status.name'), else the leading word of its first identifier (Feed in FeedStatus).  Returns
    (what was matched, line numbers), or (None, [])."""
    code = re.sub(r'"(?:[^"\\]|\\.)*"', '""', lines[target - 1])
    prefixes = set()
    for ch in CHAIN_RE.findall(code):
        parts = ch.split(".")
        prefixes.update(".".join(parts[:k]) for k in range(2, len(parts)))
    tests = [(p, re.compile(r"(?<![\w.])" + re.escape(p) + r"\b"))
             for p in sorted(prefixes, key=lambda p: (p.count("."), p))]
    lead = re.match(r"\s*([A-Z][a-z]+|[a-z]+)[A-Z]\w*\.", code)
    if lead and len(lead.group(1)) >= 4:
        tests.append((lead.group(1) + "*", re.compile(r"(?<![\w.])" + re.escape(lead.group(1)) + r"[A-Z]\w*\.")))
    for label, rx in tests:
        hits = [i for i in range(m_start, m_end + 1) if rx.search(lines[i - 1])]
        if 1 < len(hits) <= RELATED_COMMON:
            out = [i for i in hits if not (w_start <= i <= w_end)][:RELATED_MAX]
            return (label, out) if out else (None, [])
    return None, []


IDENT_RE = re.compile(r"^\s*(?:(?:class|interface|enum|struct|record)\s+)?([A-Za-z_]\w*)\s*(\.\w+|=)?\s*$")


def graph_hints(ctx: Ctx, pattern: str) -> list[str]:
    """When a literal search is for something the graph knows (a control ID, a declared name), say which
    tool answers it exactly.  The search still runs; this only points to the better tool."""
    m = IDENT_RE.match(pattern)
    if not m:
        return []
    name, tail = m.group(1), m.group(2)
    out: list[str] = []
    ctls = ctx.con.execute(
        "SELECT c.ctl_id, f.rel_path, c.line FROM controls c JOIN files f ON f.file_id = c.markup_file "
        "WHERE c.ctl_id = ? ORDER BY f.rel_path LIMIT 3", (name,)).fetchall()
    if ctls:
        calls = "; ".join(f"get_control_binding(markup_file='{r['rel_path']}', control_id='{name}')" for r in ctls)
        out.append(f"Next: call {calls}. '{name}' is a control there (markup line "
                   + ", ".join(str(r['line']) for r in ctls) + "); the result gives its type and every code-behind "
                   "line that uses it, which this text search does not prove.")
        return out
    try:
        decls = ctx.con.execute(
            "SELECT kind, fqn, path, coalesce(start_line, line) l, key FROM rsymbols WHERE name = ? "
            "AND is_generated = 0 ORDER BY kind = 'class' DESC, path LIMIT 3", (name,)).fetchall()
    except Exception:
        decls = []
    if decls:
        where = "; ".join(f"{r['kind']} {r['fqn']} at {r['path']}:{r['l']}" for r in decls)
        out.append(f"Next: call read_file_range(key='{decls[0]['key']}') for the whole declaration, or "
                   f"find_references(symbol=...) for its uses. '{name}' is declared in the compiler index ({where}); "
                   "these are compiler results, this search is text"
                   + (" and assignments" if tail else "") + ".")
        return out
    decls = ctx.con.execute(
        "SELECT s.kind, s.fqn, f.rel_path, s.line_start FROM symbols s JOIN files f ON f.file_id = s.file_id "
        "WHERE s.name = ? AND s.is_synthetic = 0 LIMIT 3", (name,)).fetchall()
    if decls:
        where = "; ".join(f"{r['kind']} {r['fqn']} at {r['rel_path']}:{r['line_start']}" for r in decls)
        out.append(f"Next: call find_symbol('{name}'), then read_file_range(symbol=...). '{name}' is declared "
                   f"in the structural graph ({where}).")
    return out


def enclosing_member(ctx: Ctx, rel: str, line: int) -> Optional[EnclosingMember]:
    """The innermost declaration containing *line* of *rel*: the compiler index first (exact spans and keys),
    else the structural graph (JavaScript and files no compiler sees)."""
    kinds = ",".join("?" * len(MEMBER_KINDS))
    try:
        r = ctx.con.execute(
            "SELECT key, fqn, name, kind, side, coalesce(start_line, line) s, end_line e FROM rsymbols "
            f"WHERE path = ? AND coalesce(start_line, line) <= ? AND end_line >= ? AND kind IN ({kinds}) "
            "ORDER BY is_generated, end_line - coalesce(start_line, line) LIMIT 1",
            (rel, line, line, *MEMBER_KINDS)).fetchone()
    except Exception:          # no compiler index in this deployment
        r = None
    if r:
        return EnclosingMember(name=r["name"], kind=r["kind"], fqn=r["fqn"], key=r["key"], side=r["side"],
                               line_start=r["s"], line_end=r["e"], layer="roslyn-index")
    r = ctx.con.execute(
        "SELECT s.fqn, s.name, s.kind, s.side, s.line_start, s.line_end FROM symbols s JOIN files f "
        "ON f.file_id = s.file_id WHERE f.rel_path = ? AND s.line_start <= ? AND s.line_end >= ? "
        "AND s.is_synthetic = 0 AND s.kind <> 'namespace' ORDER BY s.line_end - s.line_start LIMIT 1",
        (rel, line, line)).fetchone()
    if r:
        return EnclosingMember(name=r["name"], kind=r["kind"], fqn=r["fqn"] or r["name"], side=r["side"],
                               line_start=r["line_start"], line_end=r["line_end"], layer="graph")
    return None


COMPACT_HITS_OVER = 50
COMPACT_SNIPPET = 160


def glob_regex(glob: str) -> re.Pattern:
    """path_glob -> regex searched in the workspace-relative path (case-insensitive, unanchored):
    '*' and '**' match any characters (also '/'), '?' one character, everything else literally."""
    out = []
    i = 0
    while i < len(glob):
        ch = glob[i]
        if ch == "*":
            while i + 1 < len(glob) and glob[i + 1] == "*":
                i += 1
            out.append(".*")
        elif ch == "?":
            out.append(".")
        else:
            out.append(re.escape("/" if ch == "\\" else ch))
        i += 1
    return re.compile("".join(out), re.IGNORECASE)


def register(mcp, ctx: Ctx) -> None:
    @mcp.tool(
        name="read_file_range",
        annotations=RO,
        description=(
            "Read exact lines from a file, with line numbers. This is how you ground a "
            "description of behaviour: every other tool tells you where code is, this "
            "tells you what it says. Reads any text file in the workspace, including "
            "ones with no symbol graph (HTML, CSS, SQL, .config). In a long file, pass "
            "anchor='<text>' to start at the first line at or after line_start that "
            "contains that text (e.g. anchor='\"OrderJournal\"'); the window keeps "
            f"its length (default {ctx.cfg.limits.read_anchor_window} lines). Or pass key (from a SymbolRef) or symbol "
            "instead of file to read that declaration's whole span. The layer is always "
            "'filesystem': the exact text of the file as it is on disk. Returns 'lines' "
            "(numbered, for citing); raw=true returns 'text' instead (the exact text, for "
            "copying code). at='path:line' (the DB Advisor's citation form) or enclosing=true reads "
            "the member containing that line: whole if it fits, else its header and the lines "
            "around the target. Every result names the member containing line_start "
            "('enclosing': cite it, and pass its key to find_references / find_callers)."
        ),
    )
    def read_file_range(
        file: str = "", line_start: int = 1, line_end: Optional[int] = None,
        anchor: Optional[str] = None, key: Optional[str] = None, symbol: Optional[str] = None,
        raw: bool = False, at: Optional[str] = None, enclosing: bool = False,
    ) -> ReadFileOut:
        decl_caveats: list[str] = []
        if at:
            m = AT_RE.match(at.strip())
            if not m:
                return ReadFileOut(status="no_results", layer="filesystem", searched_scope="(at)",
                                   caveats=[f"at={at!r} is not 'path:line' or 'path:start-end'"])
            if (line_end is not None or line_start != 1) and not m["end"]:
                # an explicit window wins: at= then only names the file (and the line the caller is after);
                # without a line_start of its own the window starts at the at= line
                file, enclosing = m["file"], False
                if line_start == 1:
                    line_start = int(m["start"])
                decl_caveats.append(f"at={at!r} with explicit line_start/line_end: reading exactly "
                                    f"{line_start}-{line_end if line_end is not None else 'end'} (the member around "
                                    "the at= line is named in 'enclosing' only when it contains line_start).")
            else:
                file, line_start = m["file"], int(m["start"])
                line_end = int(m["end"]) if m["end"] else None
                enclosing = line_end is None
        if key or symbol:
            found = _declaration_span(ctx, key, symbol, file)
            if isinstance(found, ReadFileOut):
                return found
            file, line_start, line_end, decl_caveats = found
        # A synthesised designer partial is not a file; say so instead of
        # inventing a path the user could open.
        if c.is_synthetic_path(ctx.cfg, file):
            row = ctx.con.execute(
                "SELECT f.rel_path FROM pages p JOIN files f "
                "ON f.file_id=p.markup_file WHERE p.synth_path=?",
                (c.synth_name(file),),
            ).fetchone()
            where = (
                f" It was generated from {row['rel_path']}; read that markup file instead."
                if row
                else ""
            )
            return ReadFileOut(
                status="no_results",
                layer="filesystem",
                searched_scope="synthesised designer partials (in-memory only)",
                file=file,
                caveats=[
                    "That path is a designer partial this tool synthesises in memory "
                    "because ASP.NET generates it at runtime. It does not exist on disk "
                    "and must not be cited as a file." + where
                ],
            )

        p = ctx.resolve_path(file)
        if p is None:
            return ReadFileOut(
                status="no_results",
                layer="filesystem",
                searched_scope=f"filesystem under {ctx.cfg.paths.workspaces}",
                file=file,
                caveats=[
                    f"No file matching '{file}' exists under the workspace root "
                    f"{ctx.cfg.paths.workspaces}."
                ],
            )

        try:
            data = p.read_bytes()
        except OSError as exc:
            return ReadFileOut(
                status="no_results", layer="filesystem",
                searched_scope=str(p), file=str(p),
                caveats=[f"Could not read: {exc}"],
            )

        lines = data.decode("utf-8-sig", errors="replace").splitlines()
        total = len(lines)
        start = 1 if line_start < 1 else line_start
        cap = ctx.cfg.limits.read_file_max_lines
        caveats = list(decl_caveats)
        if anchor:
            hit = next((i for i in range(start, total + 1) if anchor in lines[i - 1]), None)
            if hit is None:
                return ReadFileOut(
                    status="no_results", layer="filesystem",
                    searched_scope=f"{ctx.rel(p)} lines {start}-{total} ({total} lines)",
                    file=p.as_posix(), rel_path=ctx.rel(p), total_lines=total,
                    caveats=[f"The text {anchor!r} does not occur at or after line {start} "
                             f"(literal, case-sensitive match)."],
                )
            span = ((int(line_end) - start) if line_end is not None and int(line_end) > start
                    else ctx.cfg.limits.read_anchor_window - 1)
            start, line_end = hit, hit + span
            caveats.append(f"anchor {anchor!r} first occurs at line {hit}; reading from there.")
        rel = ctx.rel(p)
        header: list[str] = []
        related: list[str] = []
        member = None
        if enclosing and not anchor:
            target = start
            member = enclosing_member(ctx, rel, target)
            if member is None:
                start, line_end = max(1, target - MEMBER_WINDOW), target + MEMBER_WINDOW
                caveats.append(f"No declaration in the index contains line {target}; showing lines around it.")
            elif member.line_end - member.line_start + 1 <= cap:
                start, line_end = member.line_start, member.line_end
                caveats.append(f"Whole {member.kind} {member.fqn} ({member.line_start}-{member.line_end}).")
            else:
                # too long to read whole: its signature, then the lines around the target
                hdr_end = member.line_start
                while hdr_end < min(member.line_start + HEADER_MAX - 1, total) and "{" not in lines[hdr_end - 1]:
                    hdr_end += 1
                start = max(hdr_end + 1, target - MEMBER_WINDOW)
                line_end = min(member.line_end, target + MEMBER_WINDOW)
                header = [f"{member.line_start + i:>6}  {t}"
                          for i, t in enumerate(lines[member.line_start - 1: hdr_end])]
                if start > hdr_end + 1:
                    header.append(f"{'':>6}  ... (lines {hdr_end + 1}-{start - 1} not shown)")
                caveats.append(
                    f"{member.kind} {member.fqn} is {member.line_end - member.line_start + 1} lines "
                    f"({member.line_start}-{member.line_end}): showing its header and lines {start}-{line_end}. "
                    "Read more with line_start/line_end inside that range."
                )
                rel_label, rel_lines = related_lines(lines, member.line_start, member.line_end, target,
                                                     start, line_end)
                if rel_lines:
                    related = [f"{'':>6}  ... related lines in this member (mention {rel_label}):"] + \
                              [f"{i:>6}  {lines[i - 1]}" for i in rel_lines]
                    caveats.append(f"{len(rel_lines)} line(s) outside the window mention {rel_label}: shown after "
                                   f"the window ({', '.join(map(str, rel_lines[:12]))}"
                                   + (", ..." if len(rel_lines) > 12 else "") + ").")
        end = total if line_end is None else min(int(line_end), total)
        if end < start:
            end = start
        truncated = (end - start + 1) > cap
        if truncated:
            end = start + cap - 1
        chunk = lines[start - 1 : end]
        numbered = header + [f"{start + i:>6}  {t}" for i, t in enumerate(chunk)] + related
        if member is None:
            member = enclosing_member(ctx, rel, start)
        if member and not enclosing:
            inner = enclosing_member(ctx, rel, end)
            if inner and inner.fqn != member.fqn and not (member.line_start <= inner.line_start
                                                           and inner.line_end <= member.line_end):
                caveats.append(f"Lines {start}-{end} cross several members: {member.fqn} at {start}, "
                               f"{inner.fqn} at {end}.")

        if truncated:
            caveats.append(
                f"Showing lines {start}-{end} of {total}; the per-call cap is {cap} lines."
            )
        return ReadFileOut(
            status="ok",
            layer="filesystem",
            searched_scope=f"{ctx.rel(p)} ({total} lines)",
            file=p.as_posix(),
            rel_path=ctx.rel(p),
            line_start=start,
            line_end=end,
            total_lines=total,
            # one copy of the text, not two: a 50-line markup read was 20 KB with both
            lines=[] if raw else numbered,
            text="\n".join(chunk) if raw else "",
            truncated=truncated,
            total_available=total,
            enclosing=member,
            caveats=caveats,
        )

    @mcp.tool(
        name="search_text",
        annotations=RO,
        description=(
            "Literal or regex text search across the workspace, with the standard "
            "exclusions applied. This is a TEXT match and nothing more -- it proves the "
            "characters occur at that line, not that a symbol is used. Use it for files "
            "that have no symbol graph (HTML, CSS, SQL, .config) or as a fallback. "
            "The pattern is LITERAL unless regex=true ('A|B' searches for the three "
            "characters A|B). extensions match the last suffix ('cs' includes "
            "page.aspx.cs); path_glob is matched anywhere in the relative path ('*' "
            "matches any characters, including '/'). Files over the 2 MB limit are skipped "
            "and named in the caveats, except files a path_glob selects (searched up to 20 MB)."
        ),
    )
    def search_text(
        pattern: str = "",
        query: Optional[str] = None,
        regex: bool = False,
        case_sensitive: bool = False,
        path_glob: Optional[str] = None,
        extensions: Union[list[str], str, None] = None,
        side: Literal["net48", "net8", "both"] = "both",
        max: int = ctx.cfg.limits.max_for("search_text"),
    ) -> SearchTextOut:
        pattern = pattern or query or ""            # query is accepted for pattern (agents reach for it first)
        if not pattern:
            return SearchTextOut(status="no_results", layer="filesystem", searched_scope="(no pattern)", pattern="",
                                 caveats=["give the text to search for as pattern (query is accepted too)"])
        if isinstance(extensions, str):
            extensions = [e for e in re.split(r"[\s,;]+", extensions) if e]
        limit = min(int(max), ctx.cfg.limits.max_results_hard)
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            rx = re.compile(pattern if regex else re.escape(pattern), flags)
        except re.error as exc:
            return SearchTextOut(
                status="no_results", layer="filesystem",
                searched_scope="(pattern not compiled)", pattern=pattern,
                caveats=[f"Invalid regular expression: {exc}"],
            )

        exts = {e.lower() if e.startswith(".") else "." + e.lower() for e in (extensions or [])}
        root = ctx.cfg.paths.workspaces
        glob_rx = glob_regex(path_glob) if path_glob else None

        hits: list[SearchHit] = []
        files_searched = 0
        truncated = False
        too_big: list[str] = []
        size_cap = ctx.cfg.index.search_glob_max_bytes if glob_rx else ctx.cfg.index.max_file_bytes

        import os

        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if is_excluded_dir(d) is None]
            base = Path(dirpath)
            for fn in filenames:
                fp = base / fn
                ext = fp.suffix.lower()
                if exts:
                    if ext not in exts:
                        continue
                elif ext not in SEARCHABLE_EXTS:
                    continue
                if ext == ".js" and is_vendor_js(fp):
                    continue
                rel = ctx.rel(fp)
                if side != "both" and ctx.cfg.side_of(fp) != side:
                    continue
                if glob_rx and not glob_rx.search(rel):
                    continue
                try:
                    if fp.stat().st_size > size_cap:
                        too_big.append(rel)
                        continue
                    text = fp.read_bytes().decode("utf-8-sig", errors="replace")
                except OSError:
                    continue
                files_searched += 1
                if not rx.search(text):
                    continue
                for i, ln in enumerate(text.splitlines(), 1):
                    m = rx.search(ln)
                    if not m:
                        continue
                    if len(hits) >= limit:
                        truncated = True
                        break
                    hits.append(
                        SearchHit(
                            loc=Loc(
                                file=fp.as_posix(), rel_path=rel,
                                line_start=i, line_end=i,
                                col_start=m.start() + 1,
                                snippet=ln.strip()[:400],
                            ),
                            match=m.group(0)[:200],
                        )
                    )
                if truncated:
                    break
            if truncated:
                break

        by_file: dict[str, int] = {}
        for h in hits:
            by_file[h.loc.rel_path] = by_file.get(h.loc.rel_path, 0) + 1

        mode = [("regex" if regex else "literal"), ("case-sensitive" if case_sensitive else "case-insensitive")]
        if exts:
            mode.append("extensions " + ",".join(sorted(exts)))
        if path_glob:
            mode.append(f"path_glob {path_glob!r}")
        if side != "both":
            mode.append(f"side {side}")
        caveats = [
            "This is a text match. It does not prove the symbol is used here -- the "
            "characters may occur in a comment, a string, or an unrelated identifier."
        ]
        if not regex:
            caveats += graph_hints(ctx, pattern)
        if not regex and LOOKS_LIKE_REGEX.search(pattern):
            caveats.append(
                f"The pattern {pattern!r} was searched as LITERAL text (regex=false), but it "
                "looks like a regular expression. Pass regex=true to use it as one."
            )
        if len(hits) > COMPACT_HITS_OVER:
            # 136 regex hits were 42 KB: past 50, drop the absolute path (rel_path stays) and cut snippets
            for h in hits:
                h.loc.file = ""
                h.loc.snippet = (h.loc.snippet or "")[:COMPACT_SNIPPET]
            caveats.append(
                f"{len(hits)} hits: shown compacted (rel_path only, snippets cut to {COMPACT_SNIPPET} "
                "characters). Narrow with path_glob, or read the file with "
                "read_file_range(anchor=..., line_start=...)."
            )
        mb = size_cap / 1_000_000
        if too_big:
            caveats.append(
                f"{len(too_big)} file(s) matching the filters are over {mb:g} MB and were NOT "
                f"searched: {', '.join(too_big[:5])}{' ...' if len(too_big) > 5 else ''}. "
                + ("Name them with path_glob to search them (up to "
                   f"{ctx.cfg.index.search_glob_max_bytes / 1_000_000:g} MB), or " if not glob_rx else "")
                + "read them with read_file_range(anchor=...)."
            )
        return SearchTextOut(
            status="ok" if hits else "no_results",
            layer="filesystem",
            searched_scope=(
                f"{', '.join(mode)} text search over {files_searched} files under {root} "
                f"(exclusions applied: obj/bin/packages/node_modules/-Release mirrors, "
                f"vendor JS, files > {mb:g} MB"
                + (f": {len(too_big)} skipped" if too_big else "") + ")"
            ),
            pattern=pattern,
            hits=hits,
            files_searched=files_searched,
            by_file=by_file,
            truncated=truncated,
            caveats=caveats,
        )


def _declaration_span(ctx: Ctx, key: Optional[str], symbol: Optional[str], file: str):
    """(file, line_start, line_end, caveats) of a symbol's declaration in the compiler index, or a
    ReadFileOut that says why there is none / which symbols the name matches."""
    from . import rindex

    scope = "compiler index declarations (rsymbols)"
    t = rindex.resolve(ctx, key=key, symbol=symbol)
    if t is None or not t.keys:
        return ReadFileOut(
            status="no_results" if rindex.available(ctx) else "unavailable", layer="filesystem",
            searched_scope=scope, file=file,
            caveats=[f"No declaration of {key or symbol!r} in the compiler index"
                     + ("" if rindex.available(ctx) else " (the compiler index is not built)")
                     + ". Find it with find_symbol, then pass its key."],
        )
    if len(t.keys) > 1:
        _syms, why = rindex.ambiguity(ctx, t)
        return ReadFileOut(status="partial", layer="filesystem", searched_scope=scope, file=file,
                           caveats=[why])
    rows = [r for r in rindex.declarations(ctx, t.keys[0]) if not r["is_generated"]] or \
        rindex.declarations(ctx, t.keys[0])
    if file:
        rel = ctx.rel(ctx.resolve_path(file)) if ctx.resolve_path(file) else file
        rows = [r for r in rows if r["path"] == rel] or rows
    if not rows:
        return ReadFileOut(status="no_results", layer="filesystem", searched_scope=scope, file=file,
                           caveats=[f"{t.keys[0]} has no declaration in source (it may be declared in a "
                                    "referenced assembly): there is no file to read."])
    r = rows[0]
    first = r["start_line"] or r["line"]
    caveats = [f"Declaration of {r['fqn']} (key={r['key']}) at {r['path']}:{first}-{r['end_line']}."]
    if len(rows) > 1:
        caveats.append("It is declared in several places (a partial type): "
                       + ", ".join(f"{x['path']}:{x['line']}" for x in rows[1:6])
                       + ". Read those with key and file.")
    return r["path"], first, r["end_line"], caveats
