"""draft_change: apply small edits to the LIVE text of a view/procedure/function/trigger and return the
whole script, so an agent never retypes a long module (and never drops its comments or mistypes a line).

Nothing is written anywhere: the draft is text, kept in this process for validate_sql / test_sql
(draft_id) and handed to the user, who applies it.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import threading
import time
from collections import OrderedDict
from typing import Literal, Optional

from pydantic import BaseModel, Field

from ..config import DraftsCfg
from ..models import DraftOut
from .common import RO, Ctx
from .stylecheck import TAB_RULE, lint, lint_hits

# config.toml [drafts]; register() applies the configured values (configure).
MAX_DRAFTS = DraftsCfg.max_drafts
DRAFT_TTL_S = DraftsCfg.ttl_s
# rule 9: above this the exact diff (plus the draft_id) is the deliverable, not the full script
LONG_SCRIPT_LINES = DraftsCfg.long_script_lines


def configure(d: DraftsCfg) -> None:
    global MAX_DRAFTS, DRAFT_TTL_S, LONG_SCRIPT_LINES
    MAX_DRAFTS, DRAFT_TTL_S, LONG_SCRIPT_LINES = d.max_drafts, d.ttl_s, d.long_script_lines


MODULE_KINDS = ("view", "proc", "scalar_fn", "inline_tvf", "tvf", "trigger")
HEADER_RE = re.compile(r"(?i)\bcreate\s+(?:or\s+alter\s+)?(proc(?:edure)?|view|function|trigger)\b")

_DRAFTS: "OrderedDict[str, dict]" = OrderedDict()
_LOCK = threading.Lock()


class DraftEdit(BaseModel):
    anchor: Optional[str] = Field(default=None, description="text found on exactly one line of the live text")
    line: Optional[int] = Field(default=None, description="or a line number, as get_object_definition(live=true) numbers it")
    action: Literal["insert_after", "insert_before", "replace"] = "insert_after"
    through: Optional[str] = Field(default=None, description="replace only: text on the last line to replace (first match from the anchor on)")
    text: str = Field(default="", description="the new line(s), verbatim; '' with replace deletes the lines")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _find(lines: list[str], text: str, start: int = 1) -> list[int]:
    """Lines (1-based, from *start*) containing *text*: exact first, then ignoring case and whitespace."""
    hits = [i for i in range(start, len(lines) + 1) if text in lines[i - 1]]
    if not hits:
        t = _norm(text)
        hits = [i for i in range(start, len(lines) + 1) if t and t in _norm(lines[i - 1])]
    return hits


def _code_lines(lines: list[str]) -> list[str]:
    """Each line with comments blanked (block comments may span lines)."""
    out, in_block = [], False
    for raw in lines:
        s, res = raw, ""
        while s:
            if in_block:
                if "*/" in s:
                    s = s.split("*/", 1)[1]
                    in_block = False
                else:
                    s = ""
            else:
                m = re.search(r"/\*|--", s)
                if not m:
                    res += s
                    s = ""
                elif m.group(0) == "--":
                    res += s[:m.start()]
                    s = ""
                else:
                    res += s[:m.start()]
                    s = s[m.end():]
                    in_block = True
        out.append(res)
    return out


def _string_state(lines: list[str]) -> list[Optional[int]]:
    """For the start of each line, and once more for the end of the text: the line (1-based) on which
    the '...' string literal open at that point started, or None outside one.  '' inside a literal is
    an escaped quote; -- and /* */ comments count only outside literals."""
    out: list[Optional[int]] = []
    opened: Optional[int] = None
    block = False
    for ln, s in enumerate(lines, 1):
        out.append(opened)
        i = 0
        while i < len(s):
            two = s[i:i + 2]
            if block:
                if two == "*/":
                    block = False
                    i += 1
            elif opened is not None:
                if two == "''":
                    i += 1
                elif s[i] == "'":
                    opened = None
            elif two == "--":
                break
            elif two == "/*":
                block = True
                i += 1
            elif s[i] == "'":
                opened = ln
            i += 1
    out.append(opened)
    return out


def _live_modified(ctx: Ctx, name: str) -> Optional[str]:
    try:
        from ..prep.catalog import connect
        con = connect(ctx.cfg, timeout=ctx.cfg.timeouts.live_definition_s)
        row = con.cursor().execute("select convert(varchar(19), modify_date, 126) from sys.objects "
                                   "where object_id = object_id(?)", name).fetchone()
        con.close()
        return row[0] if row else None
    except Exception:
        return None


def _store(d: dict) -> None:
    with _LOCK:
        now = time.time()
        for k in [k for k, v in _DRAFTS.items() if now - v["created"] > DRAFT_TTL_S]:
            del _DRAFTS[k]
        _DRAFTS[d["id"]] = d
        _DRAFTS.move_to_end(d["id"])
        while len(_DRAFTS) > MAX_DRAFTS:
            _DRAFTS.popitem(last=False)


def get(draft_id: str) -> Optional[dict]:
    with _LOCK:
        d = _DRAFTS.get((draft_id or "").strip())
        if d and time.time() - d["created"] > DRAFT_TTL_S:
            del _DRAFTS[d["id"]]
            return None
        return d


def _handover_lines(text: str) -> tuple[list[str], int]:
    """(lines of a script as written in an answer: no code fences, no blank edges, no trailing blanks; the
    number of lines cut from the top, so line numbers still count from the script's first line)."""
    lines = [s.rstrip() for s in text.replace("\r\n", "\n").split("\n")]
    top = 0
    while lines and (not lines[0] or lines[0].lstrip().startswith("```")):
        lines.pop(0)
        top += 1
    while lines and (not lines[-1] or lines[-1].lstrip().startswith("```")):
        lines.pop()
    return lines, top


def _excerpt(a: str, b: str, room: int = 14) -> tuple[str, str]:
    """The parts of two lines that differ, with a little context, so a one-character change in a long line shows."""
    a, b = a.strip(), b.strip()
    head = len(os.path.commonprefix([a, b]))
    tail = len(os.path.commonprefix([a[::-1], b[::-1]]))
    tail = min(tail, len(a) - head, len(b) - head)
    lo = max(0, head - room)

    def cut(x: str) -> str:
        end = len(x) - tail + room
        return ("..." if lo else "") + x[lo:end] + ("..." if end < len(x) else "")
    return cut(a), cut(b)


def handover_note(draft_id: str, n_lines: int) -> str:
    """What to hand over (rule 9): a long script is skipped or mangled when pasted, so its exact diff is the
    deliverable (run 3 of 41447299 shortened a 463-line one by hand)."""
    if n_lines <= LONG_SCRIPT_LINES:
        return (f"Hand over the diff and then this script exactly as returned, and check the copy with "
                f"validate_sql(draft_id='{draft_id}', handover=...).")
    return (f"The script has {n_lines} lines (over {LONG_SCRIPT_LINES}): hand over the diff exactly as returned, "
            f"every hunk and context line, plus draft_id '{draft_id}', and leave out the full script unless the "
            "user asks for it. Never shorten, reorder or annotate the diff; explanations go outside it. It applies "
            "to the live text, which is also the rollback.")


def handover_check(draft_id: str, text: str) -> list[str]:
    """Compare the script an answer hands over with the draft: one note saying it is identical, or naming the
    lines that differ (a hand copy of a long script goes wrong by one line; this catches it)."""
    d = get(draft_id)
    if d is None:
        return []
    (want, top), (got, _) = _handover_lines(d["script"]), _handover_lines(text)
    ops = [] if want == got else \
        [o for o in difflib.SequenceMatcher(None, want, got, autojunk=False).get_opcodes() if o[0] != "equal"]
    if not ops:
        return [f"handover: identical to draft {d['id']} ({len(want)} lines)."]
    shown = []
    for tag, i1, i2, j1, j2 in ops[:8]:
        at = f"line {top + i1 + 1}" if i2 - i1 <= 1 else f"lines {top + i1 + 1}-{top + i2}"
        if i2 - i1 == 1 and j2 - j1 == 1:
            have, wrote = _excerpt(want[i1], got[j1])
        else:
            have = " / ".join(s.strip()[:90] for s in want[i1:i2][:3]) or "(nothing)"
            wrote = " / ".join(s.strip()[:90] for s in got[j1:j2][:3]) or "(nothing)"
        shown.append(f"{at}: draft has `{have}`, handover has `{wrote}`")
    return [f"HANDOVER DIFFERS from draft {d['id']} in {len(ops)} place(s): " + "; ".join(shown)
            + (f"; ... {len(ops) - 8} more" if len(ops) > 8 else "")
            + ". Copy the script from the draft_change result again (do not retype it) and check it once more."]


def indent_check(out: list[str], changed: list[int], kept: dict[int, int], lines: list[str]) -> tuple[list[int], list[str]]:
    """(added lines indented with tabs next to tab-indented live lines, notes where the first line of an
    added block has a different tab depth than the live line above it).  *kept* maps a new line to the live
    line it copies; the team's tab rule is waived for the first group, the notes make the depth a decision."""
    def depth(s: str) -> int:
        return len(s) - len(s.lstrip("\t"))
    tab_ok: list[int] = []
    notes: list[str] = []
    for j in changed:
        above = [k for k in kept if k < j and lines[kept[k] - 1].strip()]      # nearest line with text
        near = lines[kept[max(above)] - 1] if above else ""
        lead = near[: len(near) - len(near.lstrip())]
        if out[j - 1][:1] == "\t" and "\t" in lead:
            tab_ok.append(j)
            if j - 1 not in changed and depth(out[j - 1]) != depth(near) and set(lead) <= {"\t"}:
                notes.append(f"line {j} is indented with {depth(out[j - 1])} tab(s), the live line above it "
                             f"(line {max(above)}) with {depth(near)} tab(s): if it should line up, change it")
    return tab_ok, notes


def combine(draft_id: Optional[str], sql: str) -> tuple[str, Optional[set[int]], list[str], Optional[str],
                                                         Optional[dict[str, set[int]]]]:
    """(script to run, lines to lint or None for all, caveats, error, lines exempt per style rule) for
    validate_sql / test_sql: the draft first, then *sql* after a GO."""
    if not draft_id:
        return sql, None, [], None, None
    d = get(draft_id)
    if d is None:
        return "", None, [], (f"draft {draft_id} not found (drafts live 2 h in the server process and are lost on "
                              "a restart): call draft_change again"), None
    n = d["script"].count("\n") + 1
    rest = sql.strip()
    script = d["script"] + ("\ngo\n" + rest if rest else "")
    only = set(d["changed"]) | (set(range(n + 2, script.count("\n") + 2)) if rest else set())
    cav = [f"batch 1 is draft {d['id']} ({d['verb']} of {d['name']}, live text of {d['modified'] or 'unknown date'}); "
           f"style notes cover only its changed lines" + (" and your SQL after it" if rest else "") + "."]
    return script, only, cav, None, {TAB_RULE: set(d.get("tab_ok", ()))}


def draft(ctx: Ctx, name: str, edits: list, verb: str = "alter", task_comment: Optional[str] = None) -> DraftOut:
    from .objects import live_definition
    scope = "live module text (read-only); nothing is written"
    o = ctx.obj(name)
    if not o or o["kind"] not in MODULE_KINDS:
        sugg = ctx.suggest(name, MODULE_KINDS)
        return DraftOut(status="no_results", layer="live", searched_scope=scope,
                        caveats=[f"No view, procedure, function or trigger named '{name}'."
                                 + (f" Did you mean: {', '.join(sugg)}?" if sugg else "")])
    if verb.lower() not in ("alter", "create or alter"):
        return DraftOut(status="error", layer="live", searched_scope=scope,
                        caveats=["verb must be 'alter' or 'create or alter'"])
    if not edits:
        return DraftOut(status="error", layer="live", searched_scope=scope, caveats=["no edits given"])
    text = None if o["name"].lower() in ctx.eval_masked_names() else live_definition(ctx, o["name"])
    if text is None:
        return DraftOut(status="unavailable", layer="live", searched_scope=scope,
                        caveats=["live definition not available (database unreachable or not permitted)"])
    if not text:
        return DraftOut(status="no_results", layer="live", searched_scope=scope, name=o["name"], kind=o["kind"],
                        caveats=[f"the live database returns no text for {o['name']}"])
    lines = text.split("\n")
    n = len(lines)
    errs: list[str] = []
    before: dict[int, list[tuple[int, list[str]]]] = {}       # line -> [(edit no., new lines)]
    after: dict[int, list[tuple[int, list[str]]]] = {}
    replace: dict[int, tuple[int, list[str], int]] = {}        # first line -> (last line, new lines, edit no.)
    covered: dict[int, int] = {}
    strings = _string_state(lines)                             # string literal open at the start of each line
    inside: list[tuple[int, int]] = []                         # (edit no., line its open literal started on)

    def where(e: DraftEdit, k: int) -> Optional[int]:
        if e.line is not None:
            if not 1 <= e.line <= n:
                errs.append(f"edit {k}: line {e.line} is outside the live text (1-{n})")
                return None
            return e.line
        if not e.anchor:
            errs.append(f"edit {k}: give an anchor or a line")
            return None
        hits = _find(lines, e.anchor)
        if len(hits) != 1:
            shown = "; ".join(f"line {i}: {lines[i - 1].strip()[:80]}" for i in hits[:10])
            errs.append(f"edit {k}: anchor {e.anchor!r} is on {len(hits)} lines of the live text"
                        + (f" ({shown}); use a longer anchor or line" if hits else ""))
            return None
        return hits[0]

    for k, raw in enumerate(edits, 1):
        try:
            e = raw if isinstance(raw, DraftEdit) else DraftEdit.model_validate(raw)
        except Exception as ex:
            errs.append(f"edit {k}: {str(ex)[:200]}")
            continue
        i = where(e, k)
        if i is None:
            continue
        new = e.text.replace("\r\n", "\n").removesuffix("\n").split("\n") if e.text else []
        if e.action == "replace":
            end = i
            if e.through:
                th = _find(lines, e.through, i)
                if not th:
                    errs.append(f"edit {k}: 'through' text {e.through!r} is not on or after line {i}")
                    continue
                end = th[0]
            if any(j in covered for j in range(i, end + 1)):
                errs.append(f"edit {k}: lines {i}-{end} overlap another replace")
                continue
            for j in range(i, end + 1):
                covered[j] = i
            replace[i] = (end, new, k)
            point = i
        elif not new:
            errs.append(f"edit {k}: {e.action} needs text")
            continue
        else:
            (after if e.action == "insert_after" else before).setdefault(i, []).append((k, new))
            point = i + 1 if e.action == "insert_after" else i
        if strings[point - 1] is not None:
            inside.append((k, strings[point - 1], "'" in e.text.replace("''", "")))
    for i in after:
        if i in covered and replace[covered[i]][0] != i:
            errs.append(f"insert_after line {i} falls inside a replaced range")
    for i in before:
        if i in covered and covered[i] != i:
            errs.append(f"insert_before line {i} falls inside a replaced range")
    if errs:
        return DraftOut(status="error", layer="live", searched_scope=scope, name=o["name"], kind=o["kind"],
                        caveats=errs)

    out: list[str] = []
    changed: list[int] = []
    by_edit: dict[int, int] = {}                               # new line -> edit no. that wrote it
    kept: dict[int, int] = {}                                  # new line -> live line it copies

    def emit(k: int, block: list[str]):
        for s in block:
            out.append(s)
            changed.append(len(out))
            by_edit[len(out)] = k
    for i in range(1, n + 1):
        for k, b in before.get(i, []):
            emit(k, b)
        if i in replace:
            emit(replace[i][2], replace[i][1])
        elif i not in covered:
            out.append(lines[i - 1])
            kept[len(out)] = i
        for k, b in after.get(i, []):
            emit(k, b)
    # an edit must not change which of the live text's lines are inside a string literal: an unbalanced
    # quote in dynamic SQL would turn code into string (or string into code) from there on
    new_strings = _string_state(out)
    for j, i in kept.items():
        if (new_strings[j - 1] is None) != (strings[i - 1] is None):
            k = by_edit[max(x for x in by_edit if x < j)] if any(x < j for x in by_edit) else 1
            return DraftOut(status="error", layer="live", searched_scope=scope, name=o["name"], kind=o["kind"],
                            caveats=[f"edit {k} changes which text is inside a string literal from live line {i} "
                                     f"on (now line {j}): check its quotes. Inside dynamic SQL every ' is written "
                                     "''; a lone ' opens or closes the string."])
    header = None
    for j, code in enumerate(_code_lines(out), 1):
        m = HEADER_RE.search(code)
        if m:
            kind = m.group(1).lower()
            kind = "procedure" if kind.startswith("proc") else kind
            out[j - 1] = out[j - 1][:m.start()] + f"{verb.lower()} {kind}" + out[j - 1][m.end():]
            header = j
            break
    script = "\n".join(out)
    did = hashlib.sha1((o["name"] + "\n" + script).encode("utf-8")).hexdigest()[:10]
    modified = _live_modified(ctx, o["name"])
    # added lines follow the object's own indentation: a tab-indented line next to tab-indented live lines is
    # not a style deviation (every other team rule still applies to it)
    tab_ok, depth_notes = indent_check(out, changed, kept, lines)
    _store({"id": did, "name": o["name"], "script": script, "changed": changed, "verb": verb.lower(),
            "modified": modified, "created": time.time(), "tab_ok": tab_ok})
    diff = "\n".join(difflib.unified_diff(lines, out, fromfile=f"{o['name']} (live)", tofile=f"{o['name']} (draft)",
                                          lineterm="", n=3))
    hits, chg = lint_hits(script), set(changed)
    style = lint(script, only=chg, exempt={TAB_RULE: set(tab_ok)}, hits=hits)
    old_notes = sum(1 for ls in hits.values() for ln in ls if ln not in chg and ln != header)
    caveats = [f"Built from the live text (modified {modified or 'unknown'}); everything outside the edits is kept "
               "byte for byte, comments and commented-out blocks included.",
               f"The rollback is the current text: get_object_definition('{o['name']}', live=true).",
               f"Check it with validate_sql(draft_id='{did}') and test_sql(draft_id='{did}', sql='exec ...').",
               handover_note(did, len(script.splitlines()))]
    for k, at, lone in inside:
        caveats.append(f"edit {k} is inside the string literal opened at live line {at} (dynamic SQL): write '' "
                       "for every ' in its text; a -- comment there runs to the end of its line inside the string."
                       + (" WARNING: its text has a lone ' that ends the string there; that is right only for "
                          "concatenation such as ' + @x + '. A text literal inside the dynamic SQL needs ''Void''."
                          if lone else ""))
    if header:
        caveats.append(f"line {header}: CREATE turned into '{verb.lower()}'.")
    else:
        caveats.append("no CREATE header found: the script is the live text with the edits only.")
    if old_notes:
        caveats.append(f"{old_notes} team-style note(s) on lines you did not change come from the live object: "
                       "leave them as they are and list them as pre-existing.")
    if tab_ok:
        caveats.append(f"{len(tab_ok)} added line(s) are indented with tabs, as the object's own lines are: that "
                       "indentation is kept (team rules apply to everything else on them).")
    caveats += depth_notes
    if task_comment:
        # the tag ('--task 45636983') is what each line must carry; a longer description need not repeat
        tag = re.match(r"(?i)\s*--\s*task\s*([^\s,;:]+)", task_comment)
        mark = re.compile(r"(?i)--\s*task\s*" + re.escape(tag.group(1)) + r"(?![\w-])") if tag else None
        bare = [j for j in changed if out[j - 1].strip() and task_comment not in out[j - 1]
                and not (mark and mark.search(out[j - 1]))]
        if bare:
            caveats.append(f"changed line(s) without the task comment '{task_comment}': "
                           + ", ".join(map(str, bare[:20])))
    return DraftOut(status="ok", layer="live", searched_scope=scope, draft_id=did, name=o["name"], kind=o["kind"],
                    live_modified=modified, changed_lines=changed, diff=diff, style=style, script=script,
                    caveats=caveats)


def register(mcp, ctx: Ctx) -> None:
    configure(ctx.cfg.drafts)

    @mcp.tool(name="draft_change", annotations=RO, description=(
        "Change an existing view/procedure/function/trigger WITHOUT retyping it: give small edits anchored "
        "on lines of its LIVE text (anchor = text on exactly one line, or line = number from "
        "get_object_definition(live=true); action insert_after | insert_before | replace, with 'through' for "
        "a range; text verbatim). Returns the whole script (CREATE turned into ALTER, everything else byte "
        "for byte, comments included), a unified diff, the changed line numbers, team-style notes on those "
        "lines only, and a draft_id: pass it to validate_sql and test_sql instead of pasting the script. "
        "task_comment is the tag each changed line carries, e.g. '--task 45636983'. Writes nothing."))
    def draft_change(name: str, edits: list[DraftEdit], verb: str = "alter",
                     task_comment: Optional[str] = None) -> DraftOut:
        return draft(ctx, name, edits, verb, task_comment)
