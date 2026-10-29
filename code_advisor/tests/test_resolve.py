"""The resolution ladder and the structural graph."""

from __future__ import annotations


from senior_dev_code.index import resolve as rz
from senior_dev_code.index import ts_csharp as cs
from senior_dev_code.index import ts_javascript as js

VALID_RESOLUTIONS = set(rz.RESOLUTION_ORDER)


def test_every_ref_carries_a_valid_resolution(ctx):
    rows = ctx.con.execute(
        "SELECT DISTINCT resolution FROM refs"
    ).fetchall()
    assert rows
    for r in rows:
        assert r[0] in VALID_RESOLUTIONS


def test_ambiguous_refs_have_no_resolved_symbol(ctx):
    """Ambiguity is a first-class answer: it must never carry a pinned target."""
    bad = ctx.con.execute(
        "SELECT COUNT(*) FROM refs WHERE resolution='ambiguous' AND resolved_sym IS NOT NULL"
    ).fetchone()[0]
    assert bad == 0


def test_resolved_refs_point_at_a_real_symbol(ctx):
    orphans = ctx.con.execute(
        "SELECT COUNT(*) FROM refs r LEFT JOIN symbols s ON s.symbol_id = r.resolved_sym "
        "WHERE r.resolved_sym IS NOT NULL AND s.symbol_id IS NULL"
    ).fetchone()[0]
    assert orphans == 0


def test_same_file_resolution_stays_in_its_file(ctx):
    bad = ctx.con.execute(
        "SELECT COUNT(*) FROM refs r JOIN symbols s ON s.symbol_id = r.resolved_sym "
        "WHERE r.resolution = 'same_file' AND s.file_id != r.file_id"
    ).fetchone()[0]
    assert bad == 0


def test_resolution_never_crosses_the_migration_boundary(ctx):
    """A net48 use site must never resolve to a net8 declaration."""
    bad = ctx.con.execute(
        "SELECT COUNT(*) FROM refs r JOIN symbols s ON s.symbol_id = r.resolved_sym "
        "WHERE r.side != s.side"
    ).fetchone()[0]
    assert bad == 0


def test_unresolved_is_recorded_not_dropped(ctx):
    """'We saw this name here' is still grounded and must be kept."""
    n = ctx.con.execute(
        "SELECT COUNT(*) FROM refs WHERE resolution='unresolved'"
    ).fetchone()[0]
    assert n > 0


def test_inheritance_edges_are_typed_by_the_target_kind(ctx):
    """An 'implements' edge must point at an interface declaration."""
    rows = ctx.con.execute(
        "SELECT s.kind FROM edges e JOIN symbols s ON s.symbol_id = e.dst_id "
        "WHERE e.edge_type='implements' AND e.dst_id IS NOT NULL LIMIT 500"
    ).fetchall()
    assert rows
    assert all(r[0] == "interface" for r in rows)

    rows = ctx.con.execute(
        "SELECT s.kind FROM edges e JOIN symbols s ON s.symbol_id = e.dst_id "
        "WHERE e.edge_type='inherits' AND e.dst_id IS NOT NULL LIMIT 500"
    ).fetchall()
    assert rows
    assert all(r[0] != "interface" for r in rows)


def test_unresolvable_base_types_are_marked_heuristic(ctx):
    """A base type not declared in this repository is recorded by name, never
    asserted as a resolved relationship."""
    bad = ctx.con.execute(
        "SELECT COUNT(*) FROM edges WHERE edge_type IN ('inherits','implements') "
        "AND dst_id IS NULL AND confidence != 'heuristic'"
    ).fetchone()[0]
    assert bad == 0


# --- extractor behaviour ---------------------------------------------------


def test_csharp_partial_classes_are_not_deduped():
    src = b"public partial class Foo { void A() {} }\npublic partial class Foo { void B() {} }\n"
    fx = cs.extract_symbols(src)
    classes = [s for s in fx.symbols if s.kind == "class"]
    assert len(classes) == 2
    assert {c.fqn for c in classes} == {"Foo"}


def test_csharp_container_walk_builds_the_fqn():
    src = b"namespace A.B { public class C { public void D() {} } }"
    fx = cs.extract_symbols(src)
    d = next(s for s in fx.symbols if s.name == "D")
    assert d.container == "A.B.C"
    assert d.fqn == "A.B.C.D"


def test_website_codebehind_has_no_namespace():
    """Website Project code-behind is in the global namespace, so fqn is
    Class.Member -- this is what makes get_control_binding line up."""
    src = b"public partial class ui_x_y { protected void Page_Load(object s, EventArgs e) {} }"
    fx = cs.extract_symbols(src)
    m = next(s for s in fx.symbols if s.name == "Page_Load")
    assert m.container == "ui_x_y"
    assert m.fqn == "ui_x_y.Page_Load"


def test_event_field_declarations_are_captured():
    """The common `public event EventHandler X;` form, which the original query
    misses."""
    src = b"public class C { public event System.EventHandler Changed; }"
    fx = cs.extract_symbols(src)
    assert any(s.name == "Changed" and s.kind == "event" for s in fx.symbols)


def test_parse_errors_are_partial_not_silent():
    fx = cs.extract_symbols(b"public class Broken { void A( { }")
    assert fx.parse_status == "partial"


def test_base_types_become_edges():
    src = b"public class C : BaseThing, IThing { }"
    fx = cs.extract_symbols(src)
    names = {e.dst_name for e in fx.edges}
    assert {"BaseThing", "IThing"} <= names


def test_js_object_member_functions_get_a_container():
    src = b"app.forms.order = {}; app.forms.order.save = function () { return 1; };"
    fx = js.extract_symbols(src)
    save = next(s for s in fx.symbols if s.name == "save")
    assert save.container == "app.forms.order"
    assert save.fqn == "app.forms.order.save"


def test_js_unaddressable_assignment_gets_no_fqn():
    """`$(x)[0].onclick = function(){}` is a real definition with no name
    anything can reference; it must not get a fabricated fqn."""
    src = b'$("#a").children()[0].onclick = function () { return 1; };'
    fx = js.extract_symbols(src)
    assert fx.symbols
    assert all(s.fqn is None for s in fx.symbols if s.container)


def test_js_line_offset_maps_inline_scripts_to_the_host_file():
    src = b"function inlineHandler() { return 1; }"
    fx = js.extract_symbols(src, line_offset=99)
    assert fx.symbols[0].line_start == 100
