"""Parser soundness over the real App/ tree.

One question: **is the markup layer sound enough for an agent to trust the graph?**
The tests assert INVARIANTS that hold whatever the repository contains, never
absolute counts.

The workspace is live TFVC. A frozen count like ``assert files == 358`` is a
*measurement*: it goes false when someone adds a page, and ten red tests meaning
"someone committed" train you to ignore the suite, losing the regression
detection the exactness was for.

If a number would change when someone adds a page, it does not belong in an
assertion. What does:

  * zero-counts      -- nothing falls back, nothing is missing a directive
  * relationships    -- live tags == textual tags minus tags inside comments
  * named exceptions -- an unresolved script target must be runtime-computed
  * totality         -- EVERY control resolves, not 6,316 of them

Each of those catches a real parser regression and survives a commit.
"""

from __future__ import annotations

import bisect
import collections
import re

from senior_dev_code.index import markup as mk

TAG_RX = re.compile(rb"<([A-Za-z_]\w*)(?::([\w.]+))?(?=[\s/>])")
MASTERTYPE_RX = re.compile(rb"<%@\s*MasterType", re.IGNORECASE)

# Directive kinds and resolution strategies the parser knows. Any other value
# means the parser invented something: a regression whatever the repository did.
KNOWN_DIRECTIVES = {"page", "control", "master"}
KNOWN_RESOLUTIONS = {
    "web_config",
    "bcl_webcontrols",
    "html_runat",
    "register_src",
    "register_assembly",
    "fallback_control",
}


def _scan_all(files, ctx):
    return [(p, mk.scan(p.read_bytes(), p, ctx)) for p in files]


def _inside_outside(src: bytes, rx) -> tuple[int, int]:
    """Count matches of *rx* inside vs outside ``<%-- server comments --%>``."""
    spans = [
        (r.byte_start, r.byte_end)
        for r in mk.scan_regions(src)
        if r.kind == "comment"
    ]
    starts = [s for s, _ in spans]
    inside = outside = 0
    for m in rx.finditer(src):
        if rx is TAG_RX and m.group(2) is None:
            continue  # unprefixed HTML tag, not a server control
        i = bisect.bisect_right(starts, m.start()) - 1
        if i >= 0 and m.start() < spans[i][1]:
            inside += 1
        else:
            outside += 1
    return inside, outside


# --- the tree is there at all ----------------------------------------------


def test_the_app_tree_has_markup_to_parse(app_markup_files):
    """Readiness, not census: a suite that silently passes on an empty tree
    would tell you nothing."""
    assert app_markup_files, "no .aspx/.ascx/.master files found under website_root"


# --- every file is understood ----------------------------------------------


def test_every_file_has_a_main_directive(app_markup_files, markup_ctx):
    missing = [p for p, ex in _scan_all(app_markup_files, markup_ctx) if ex.page is None]
    assert not missing, f"{len(missing)} files with no main directive: {missing[:5]}"


def test_every_directive_kind_is_one_the_parser_knows(app_markup_files, markup_ctx):
    kinds = collections.Counter()
    for p, ex in _scan_all(app_markup_files, markup_ctx):
        assert ex.page is not None
        assert ex.page.directive in KNOWN_DIRECTIVES, f"{p}: {ex.page.directive!r}"
        kinds[ex.page.directive] += 1
    # All three shapes must be represented, or the fixture is not seeing the
    # real tree and every other assertion here is vacuous.
    assert kinds.keys() >= KNOWN_DIRECTIVES, f"only saw {sorted(kinds)}"


def test_every_page_declares_its_codebehind(app_markup_files, markup_ctx):
    """CodeFile, Inherits and Language are universal in this tree. Without all
    three the designer layer cannot synthesise a partial, so the graph would be
    wrong for that page rather than merely incomplete."""
    incomplete = []
    for p, ex in _scan_all(app_markup_files, markup_ctx):
        page = ex.page
        assert page is not None
        missing = [f for f in ("code_file", "inherits", "language") if not getattr(page, f)]
        if missing:
            incomplete.append((p.name, missing))
    assert not incomplete, f"{len(incomplete)} pages missing directive attrs: {incomplete[:5]}"


# --- commented-out markup is excluded, and provably so ----------------------


def test_commented_out_tags_are_excluded(app_markup_files, markup_ctx):
    """The invariant that makes the live counts trustworthy:

        live tags  ==  textual tags  -  tags inside <%-- --%>

    Asserting the *relationship* catches a regression in comment handling on any
    version of the tree; asserting 9,173 only catches it on one.
    """
    inside_total = outside_total = live_total = 0
    for p, ex in _scan_all(app_markup_files, markup_ctx):
        src = p.read_bytes()
        inside, outside = _inside_outside(src, TAG_RX)
        inside_total += inside
        outside_total += outside
        live_total += sum(1 for c in ex.controls if c.tag_prefix)

    assert live_total == outside_total, (
        f"parser reported {live_total} live prefixed tags but {outside_total} sit "
        "outside server comments -- comment handling has regressed"
    )
    assert inside_total > 0, (
        "no tags found inside <%-- --%> anywhere in the tree; this guard is "
        "no longer being exercised and would not catch a regression"
    )


def test_mastertype_live_excludes_commented_out(app_markup_files, markup_ctx):
    textual = live = 0
    outside_total = 0
    for p, ex in _scan_all(app_markup_files, markup_ctx):
        src = p.read_bytes()
        inside, outside = _inside_outside(src, MASTERTYPE_RX)
        textual += inside + outside
        outside_total += outside
        live += int(bool(ex.page.master_type_raw))

    assert live == outside_total, (
        f"{live} live MasterType directives vs {outside_total} outside comments"
    )
    assert textual >= live, "more live directives than exist textually"


# --- every control is classified and typed ----------------------------------


def test_every_control_is_either_prefixed_or_runat(app_markup_files, markup_ctx):
    """A control the parser cannot classify would land in the graph with no
    meaningful identity."""
    unclassified = []
    for p, ex in _scan_all(app_markup_files, markup_ctx):
        for c in ex.controls:
            if c.tag_prefix:
                if not c.tag_name:
                    unclassified.append((p.name, c.control_ref))
            elif not c.ctl_id and not c.control_ref:
                unclassified.append((p.name, "<runat with no ref>"))
    assert not unclassified, unclassified[:5]


def test_control_ids_are_usable_identifiers(app_markup_files, markup_ctx):
    """An ID is what the code-behind references by name. A blank or malformed one
    would produce a designer field that cannot compile."""
    bad = []
    seen = 0
    # A C# identifier: a Unicode letter or '_', then letters/digits/'_'.  Not
    # ASCII-only -- App/ has IDs spelt with non-Latin capitals that look Latin (in
    # a settings page), which compile and bind like any other.
    ident = re.compile(r"[^\W\d]\w*$")
    for p, ex in _scan_all(app_markup_files, markup_ctx):
        for c in ex.controls:
            if c.ctl_id is None:
                continue
            seen += 1
            if not ident.match(c.ctl_id):
                bad.append((p.name, c.ctl_id))
    assert seen > 0, "no ID-bearing controls found at all"
    assert not bad, f"malformed control IDs: {bad[:5]}"


def test_type_resolution_is_total(app_markup_files, markup_ctx):
    """The name is the invariant: *total*. Nothing falls back.

    This is the single most important assertion in the file. A control that
    silently resolves to System.Web.UI.Control produces a well-formed, wrong
    answer -- exactly the failure this project exists to prevent.
    """
    res = collections.Counter()
    untyped = []
    for p, ex in _scan_all(app_markup_files, markup_ctx):
        for c in ex.controls:
            res[c.resolution] += 1
            if not c.resolved_type:
                untyped.append((p.name, c.control_ref))

    assert not untyped, f"{len(untyped)} controls with no resolved type: {untyped[:5]}"
    assert res["fallback_control"] == 0, (
        f"{res['fallback_control']} controls fell back to the base Control type"
    )
    unknown = set(res) - KNOWN_RESOLUTIONS
    assert not unknown, f"parser produced unknown resolution kinds: {unknown}"
    # Every strategy must still be doing work -- if one silently stops firing,
    # its controls are being resolved by something else and that is a regression.
    for kind in ("web_config", "bcl_webcontrols", "html_runat"):
        assert res[kind] > 0, f"resolution strategy {kind!r} produced nothing"


# --- outbound references ----------------------------------------------------


def test_unresolved_script_targets_are_all_runtime_computed(app_markup_files, markup_ctx):
    """A script include resolves unless its path is built at run time.

    Stronger than a count: this catches a *different* target starting to fail,
    which ``assert resolved == 124`` cannot -- the total would stay the same.
    """
    total = 0
    unresolved: list[str] = []
    for _, ex in _scan_all(app_markup_files, markup_ctx):
        for s in ex.scripts:
            total += 1
            if not s.resolved_rel:
                unresolved.append(s.raw)

    assert total > 0, "no script includes found"
    static_misses = [r for r in unresolved if "<%" not in r]
    assert not static_misses, (
        "script targets with a literal path failed to resolve: " f"{static_misses[:5]}"
    )


def test_onclient_handlers_are_parsed_as_calls(app_markup_files, markup_ctx):
    """The rate floor lives in test_golden.py::test_G8, which checks resolution
    against the indexed JS. Here we only assert the parser still recognises call
    syntax at all -- a regression that stopped it would make G8 vacuous."""
    total = calls = 0
    for _, ex in _scan_all(app_markup_files, markup_ctx):
        for c in ex.controls:
            for _, v in c.client_handlers:
                total += 1
                calls += int(bool(mk.js_call_target(v)))

    assert total > 0, "no OnClient* handlers found"
    assert calls > 0, "no OnClient* attribute parsed as a function call"


# --- delimiter-family unit tests (no repository dependency) -----------------
# These never needed freezing: they assert on literal inputs.


def test_region_scanner_classifies_the_delimiter_family():
    src = (
        b'<%-- comment --%>'
        b'<%@ Page Language="C#" %>'
        b'<%= Response.Write("x") %>'
        b'<%# Eval("Field") %>'
        b'<%$ Resources: App, Title %>'
        b'<% int i = 0; %>'
        b"plain text"
    )
    kinds = [r.kind for r in mk.scan_regions(src)]
    assert kinds == [
        "comment", "directive", "expr", "databind", "appsetting", "code", "content"
    ]


def test_databinding_is_not_a_comment():
    """tree-sitter-embedded-template classifies <%# %> as a comment. It is a
    data-binding expression and must survive."""
    regions = list(mk.scan_regions(b'<td><%# Eval("CustomerName") %></td>'))
    binds = [r for r in regions if r.kind == "databind"]
    assert len(binds) == 1
    assert b"Eval" in binds[0].body


def test_server_comment_is_not_an_output_directive():
    """<%-- ... --%> starts with <%- ; it must not be read as output."""
    regions = list(mk.scan_regions(b'<%--<asp:Label ID="x" runat="server" />--%>'))
    assert [r.kind for r in regions] == ["comment"]


def test_attribute_value_may_contain_its_own_delimiter():
    attrs = mk.attrs_of(
        b'<script src="<%= appSupport.ResolvePublicUrl("js/x.js", true) %>">'
    )
    assert attrs["src"] == '<%= appSupport.ResolvePublicUrl("js/x.js", true) %>'


def test_unterminated_opener_runs_to_eof_rather_than_vanishing():
    regions = list(mk.scan_regions(b"before<%@ Page Language=\"C#\""))
    assert [r.kind for r in regions] == ["content", "directive"]
    assert regions[-1].byte_end == len(b"before<%@ Page Language=\"C#\"")