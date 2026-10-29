"""Phase search: the find_db_object surface (FTS5 over names, captions, titles, labels)."""

from __future__ import annotations

import re
import sqlite3

from ..config import Config


def words(name: str) -> str:
    """'ChannelType' -> 'ChannelType Channel Type'; 'usp_rpt_stock_adjustments' -> '... usp rpt stock adjustments'.
    Adjacent camel parts are joined too: 'OrderFeedbackTable' also gives 'FeedBack', so 'feedback' finds it."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    camel = spaced.split()
    pairs = [a + b for a, b in zip(camel, camel[1:]) if "_" not in a + b] if len(camel) > 2 else []
    return " ".join([name, spaced.replace("_", " ")] + pairs)


def run(cfg: Config, con: sqlite3.Connection) -> dict:
    con.execute("DELETE FROM search_items")
    con.execute("INSERT INTO search_fts(search_fts) VALUES('delete-all')")
    items = []
    for name, kind, desc, rows in con.execute("SELECT name, kind, description, row_count FROM objects WHERE kind<>'trigger'"):
        items.append((kind, name, words(name) + (" " + desc[:300] if desc else ""),
                      f"{kind}" + (f", {rows} rows" if rows is not None else "") + (f": {desc[:120]}" if desc else ""), 2.0))
    table_words: dict[str, str] = {}
    for t, c, typ, desc in con.execute("SELECT o.name, c.name, c.type_name, c.description FROM columns c "
                                       "JOIN objects o ON o.obj_id=c.obj_id WHERE o.kind='table'"):
        # the table's name counts too: 'feedback invoice' -> OrderFeedbackTable.InvoiceDocNo
        tw = table_words.get(t) or table_words.setdefault(t, words(t))
        items.append(("column", f"{t}.{c}", f"{words(c)} {tw}" + (" " + desc[:200] if desc else ""),
                      f"{typ}" + (f": {desc[:120]}" if desc else ""), 1.6 if desc else 1.3))
    for tk, name, title, url, obj in con.execute("SELECT target_kind, name, title, url, object_name FROM ui_targets"):
        items.append(("screen" if tk == "view" else "report", name,
                      f"{words(name)} {title or ''} {url or ''}",
                      f"{'grid screen' if tk == 'view' else 'report'} '{title}' {url} -> SQL {obj}", 2.5))
    from ..prep.link import _view_lineage
    lineage = _view_lineage(con)
    groups: dict[tuple, dict] = {}
    for view, field, cap, obj in con.execute("SELECT c.view_name, c.field_name, c.caption, v.object_name FROM ui_view_columns c "
                                             "JOIN ui_views v ON v.view_name=c.view_name AND v.rel_path=c.rel_path"):
        if not cap:
            continue
        base = lineage.get(((obj or "").lower(), field.lower()))
        key = (cap.lower(), (f"{base[0][0]}.{base[0][1]}" if base else field).lower())
        g = groups.setdefault(key, {"cap": cap, "field": field, "base": base, "screens": []})
        if view not in g["screens"]:
            g["screens"].append(view)
    for g in groups.values():
        b = f"{g['base'][0][0]}.{g['base'][0][1]}" if g["base"] else None
        items.append(("grid_column", b or f"{g['screens'][0]}.{g['field']}", f"{g['cap']} {words(g['field'])}",
                      f"caption '{g['cap']}' on grid screen(s) {', '.join(g['screens'][:5])}"
                      + (f" (+{len(g['screens']) - 5})" if len(g["screens"]) > 5 else "")
                      + (f" = base column {b}" if b else f" = view column {g['field']}"), 1.4 if b else 1.0))
    rgroups: dict[tuple, dict] = {}
    for rep, field, cap, obj in con.execute("SELECT f.report_name, f.data_field, f.caption, r.object_name FROM ui_report_fields f "
                                            "JOIN ui_reports r ON r.report_name=f.report_name AND r.rel_path=f.rel_path"):
        if not cap:
            continue
        g = rgroups.setdefault((cap.lower(), field.lower()), {"cap": cap, "field": field, "reps": []})
        if (rep, obj) not in g["reps"]:
            g["reps"].append((rep, obj))
    for g in rgroups.values():
        items.append(("report_column", f"{g['reps'][0][1] or g['reps'][0][0]}.{g['field']}", f"{g['cap']} {words(g['field'])}",
                      f"caption '{g['cap']}' = output {g['field']} in {len(g['reps'])} report(s): "
                      + ", ".join(f"{r} ({o})" for r, o in g["reps"][:4]), 0.9))
    # value labels of linked columns: 'gift card' -> PaymentLogTable.PayMethod = 6
    for tbl, col, v, lab, src in con.execute("""SELECT DISTINCT cvm.tbl, cvm.col, i.value, i.label, vm.source
                                              FROM column_value_maps cvm JOIN value_maps vm ON vm.map_id=cvm.map_id
                                              JOIN value_map_items i ON i.map_id=vm.map_id
                                              WHERE cvm.confidence IN ('exact','strong') AND cvm.col NOT LIKE '@%'
                                              AND vm.source<>'lookup_table'"""):
        items.append(("value", f"{tbl}.{col}", f"{lab} {words(col)}", f"{tbl}.{col} = {v} means '{lab}' ({src})", 1.3))
    seen = set()
    for mid, name, source, side in con.execute("SELECT map_id, name, source, side FROM value_maps WHERE source<>'db_description'"):
        if (name.lower(), source) in seen:
            continue
        seen.add((name.lower(), source))
        labels = [r[0] for r in con.execute("SELECT label FROM value_map_items WHERE map_id=? LIMIT 60", (mid,))]
        used = [f"{r[0]}.{r[1]}" for r in con.execute(
            "SELECT DISTINCT tbl, col FROM column_value_maps WHERE map_id=? AND confidence<>'name_match' LIMIT 4", (mid,))]
        items.append(("value_map", name, f"{words(name)} {' '.join(labels)}",
                      f"{source}" + (f" used by {', '.join(used)}" if used else ""), 1.2))
    for subj, text in con.execute("SELECT subject, text FROM curated WHERE fact_type IN ('meaning','purpose')"):
        items.append(("curated", subj.replace("object:", ""), text[:400], text[:140], 1.5))
    con.executemany("INSERT INTO search_items(kind,name,text,detail,weight) VALUES (?,?,?,?,?)", items)
    con.execute("INSERT INTO search_fts(search_fts) VALUES('rebuild')")
    con.commit()
    return {"items": len(items)}
