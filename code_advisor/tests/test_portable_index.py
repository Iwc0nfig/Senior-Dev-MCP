"""The index must contain no absolute paths at all.

``graph.db`` is built on one machine and served from another, so an absolute path
anywhere in it silently pins the index to its builder, resolving citations
against a tree that is not there.

Enforced here, as in ``test_tools_contract.py``: a rule is only real if
something checks it.
"""

from __future__ import annotations

import re

import pytest

# 'C:/...' or '/srv/...'.  Stored paths are relative to the workspace root or the
# designer directory, so they match neither.
ABSOLUTE = re.compile(r"^(?:[A-Za-z]:/|/)")


def _norm(v: str) -> str:
    """Compare in the one separator the index is supposed to use."""
    return str(v).replace("\\", "/")


def _offenders(rows, label):
    return [(label, v) for (v,) in rows if v and ABSOLUTE.match(_norm(v))]


def test_files_are_stored_workspace_relative(ctx):
    total = ctx.con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    assert total > 0, "empty index"
    bad = _offenders(ctx.con.execute("SELECT rel_path FROM files"), "files.rel_path")
    assert not bad, f"{len(bad)} of {total} rows absolute, e.g. {bad[:3]}"


def test_page_paths_are_relative(ctx):
    bad = _offenders(
        ctx.con.execute("SELECT synth_path FROM pages WHERE synth_path IS NOT NULL"),
        "pages.synth_path",
    )
    bad += _offenders(
        ctx.con.execute("SELECT designer_file FROM pages WHERE designer_file IS NOT NULL"),
        "pages.designer_file",
    )
    assert not bad, bad[:5]


def test_synth_path_is_a_bare_basename(ctx):
    """The designer directory lives outside the workspace and differs per machine."""
    rows = [
        r[0]
        for r in ctx.con.execute(
            "SELECT synth_path FROM pages WHERE synth_path IS NOT NULL"
        )
    ]
    assert rows, "no synthesised designers recorded"
    assert all("/" not in r and "\\" not in r for r in rows), [
        r for r in rows if "/" in r or "\\" in r
    ][:5]
    assert all(r.endswith(".synth.designer.cs") for r in rows)


# Edge types whose dst_name names a file in the workspace.  includes_script and
# includes_style are deliberately excluded: their destination is the URL as
# written, which is legitimately root-relative ("/public/js/x.js") and sometimes
# protocol-relative ("//code.jquery.com/..."), and neither is a path on this disk.
FILE_VALUED_EDGES = ("master_of", "codebehind_of")


def test_edge_destinations_are_relative(ctx):
    """master_of is the edge type that used to carry an absolute path."""
    placeholders = ",".join("?" * len(FILE_VALUED_EDGES))
    bad = _offenders(
        ctx.con.execute(
            f"SELECT dst_name FROM edges WHERE dst_name IS NOT NULL "
            f"AND edge_type IN ({placeholders})",
            FILE_VALUED_EDGES,
        ),
        "edges.dst_name",
    )
    assert not bad, f"{len(bad)} absolute edge destinations, e.g. {bad[:3]}"


def test_master_edges_point_at_indexed_files(ctx):
    """A relative destination is only useful if it still joins to a file row."""
    rows = [r[0] for r in ctx.con.execute(
        "SELECT dst_name FROM edges WHERE edge_type='master_of' AND dst_name IS NOT NULL"
    )]
    assert rows, "no master_of edges recorded"
    missing = [
        d for d in rows
        if ctx.con.execute(
            "SELECT 1 FROM files WHERE rel_path=?", (d,)
        ).fetchone() is None
    ]
    assert not missing, f"{len(missing)} master_of edges name no indexed file: {missing[:3]}"


def test_the_index_records_where_it_was_built(ctx):
    """So index_status can report a root mismatch instead of answering emptily."""
    row = ctx.con.execute(
        "SELECT value FROM index_meta WHERE key='workspaces_root'"
    ).fetchone()
    assert row is not None, "workspaces_root not recorded; rebuild the index"
    assert row[0]


def test_locations_handed_out_are_absolute_and_real(tools, cfg):
    """The stored form is relative; what a caller receives must be openable."""
    out = tools("find_symbol", query="Page_Load", max=5)
    if out.status != "ok" or not out.symbols:
        pytest.skip("Page_Load not present in this index")
    for s in out.symbols[:5]:
        if s.is_synthetic:
            continue
        assert ABSOLUTE.match(_norm(s.loc.file)), s.loc.file
        assert s.loc.file.startswith(cfg.paths.workspaces.as_posix())
        assert not ABSOLUTE.match(_norm(s.loc.rel_path)), s.loc.rel_path
