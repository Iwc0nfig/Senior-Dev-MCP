"""Team SQL style: load the written rules and lint SQL against them.

The rules live in knowledge/team_sql_style.md (written by the DB developers, editable by
them).  The linter is deliberately conservative: it reports only deviations it can see
reliably, as warnings, and passes the developers' own example.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

KEYWORDS = {
    "SELECT", "FROM", "WHERE", "JOIN", "INNER", "LEFT", "RIGHT", "FULL", "OUTER", "CROSS", "APPLY", "ON",
    "AND", "OR", "NOT", "IN", "EXISTS", "BETWEEN", "LIKE", "IS", "NULL", "CASE", "WHEN", "THEN", "ELSE",
    "END", "AS", "GROUP", "BY", "ORDER", "HAVING", "UNION", "ALL", "DISTINCT", "TOP", "WITH", "NOLOCK",
    "INSERT", "INTO", "VALUES", "UPDATE", "SET", "DELETE", "DECLARE", "BEGIN", "IF", "RETURN", "ALTER",
    "CREATE", "VIEW", "PROCEDURE", "PROC", "FUNCTION", "ISNULL", "CONVERT", "CAST", "COALESCE", "SUM",
    "COUNT", "MIN", "MAX", "AVG", "DATEADD", "DATEDIFF", "GETDATE", "NULLIF", "OVER", "PARTITION", "ASC",
    "DESC", "EXEC", "EXECUTE", "OUTPUT", "NOCOUNT", "ARITHABORT", "VARCHAR", "NVARCHAR", "INT", "DATETIME",
    "DATE", "DECIMAL", "TINYINT", "BIT", "MONEY", "FLOAT",
}


def load_rules(p: Path) -> tuple[list[str], str]:
    """(rules, example) from the style file ([paths] style_rules); empty when the file is missing."""
    if not p.is_file():
        return [], ""
    text = p.read_text(encoding="utf-8")
    rules = [m.group(1).strip() for m in re.finditer(r"(?m)^- (.+)$", text.split("## Example")[0])]
    m = re.search(r"```sql\n(.*?)```", text, re.S)
    return rules, (m.group(1).rstrip() if m else "")


def _code_only(line: str) -> str:
    """The line without string literals and -- comments."""
    line = re.sub(r"'(?:[^']|'')*'", "''", line)
    return line.split("--", 1)[0]


def _depth0_commas(s: str) -> int:
    depth = n = 0
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            n += 1
    return n


TAB_RULE = "indent with spaces (four per level), not tabs"


def lint(sql: str, only: Optional[set[int]] = None, exempt: Optional[dict[str, set[int]]] = None,
         hits: Optional[dict[str, list[int]]] = None) -> list[str]:
    """Deviations from the team rules: one entry per rule, with up to 4 example line numbers.  With *only*,
    just the hits on those lines (the lines a draft changed); *exempt* drops lines from one rule (added lines
    that follow a tab-indented object's own indentation).  *hits* reuses a lint_hits(sql) already computed."""
    out = []
    for rule, lines in (lint_hits(sql) if hits is None else hits).items():
        if only is not None:
            lines = [ln for ln in lines if ln in only]
        if exempt and rule in exempt:
            lines = [ln for ln in lines if ln not in exempt[rule]]
        if not lines:
            continue
        more = f" (+{len(lines) - 4} more)" if len(lines) > 4 else ""
        out.append(f"{rule}: line {', '.join(map(str, lines[:4]))}{more}")
    return out


def lint_hits(sql: str) -> dict[str, list[int]]:
    """{rule: [line numbers]} for every deviation from the team rules."""
    hits: dict[str, list[int]] = {}

    def hit(rule: str, ln: int):
        hits.setdefault(rule, []).append(ln)

    in_block = False
    for ln, raw in enumerate(sql.replace("\r\n", "\n").split("\n"), 1):
        line = raw
        if in_block:
            if "*/" in line:
                line = line.split("*/", 1)[1]
                in_block = False
            else:
                continue
        if "/*" in line:
            before, _, after = line.partition("/*")
            in_block = "*/" not in after
            line = before + (after.split("*/", 1)[1] if not in_block else "")
        code = _code_only(line)
        stripped = code.strip()
        if not stripped or re.match(r"(?i)^(set\s+(ansi_nulls|quoted_identifier)|go)\b", stripped):
            continue
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            hit(TAB_RULE, ln)
        if any(w in KEYWORDS for w in re.findall(r"\b[A-Z][A-Z_]+\b", code)):
            hit("keywords and functions in lowercase (select, from, where, isnull, convert...)", ln)
        if stripped.endswith(",") and not stripped.lower().startswith(("declare", "@")):
            hit("commas at the beginning of the next line (', col'), not at the end", ln)
        low = stripped.lower()
        if (low.startswith("select ") or low.startswith(",")) and _depth0_commas(low.lstrip(", ").removeprefix("select ")) > 0:
            hit("one selected column per line", ln)
        if len(re.findall(r"\bjoin\b", low)) > 1:
            hit("each join on its own line", ln)
        if re.search(r"\bon\s+1\s*=\s*1\b", low):
            hit("use 'cross join' instead of 'inner join ... on 1 = 1'", ln)
        if re.search(r"\b(from|join)\s+[\w\.\[\]]+\s+with\s*\(\s*nolock\s*\)\s+(as\s+)?[a-z_]\w*", low) and \
                not re.search(r"with\s*\(\s*nolock\s*\)\s+(on|where|inner|left|right|full|cross|outer|order|group|union)\b", low):
            hit("table hint after the alias: 'InvoiceHeaderTable i with(nolock)'", ln)
        tail = re.sub(r"\bbetween\b.+?\band\b", "between", low)
        if not re.match(r"^(and|or)\b", tail) and re.search(r"\S\s+(and|or)\s+", _strip_parens(tail)) \
                and re.search(r"\b(where|on|and|or|when)\b", tail):
            hit("each additional condition on its own line starting with 'and' / 'or'", ln)
        if raw[:1] not in (" ", "\t") and re.match(
                r"^(,|(inner|left|right|full|cross|outer)\b|join\b|on\b|and\b|or\b)", low):
            hit("indent continuation lines (', column', joins, on/and/or) by four spaces", ln)
    return hits


def _strip_parens(s: str) -> str:
    """Drop parenthesised parts (conditions inside a subquery/function are their own matter)."""
    prev = None
    while prev != s:
        prev = s
        s = re.sub(r"\([^()]*\)", "()", s)
    return s
