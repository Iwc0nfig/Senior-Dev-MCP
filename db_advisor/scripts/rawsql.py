"""Baseline access for the A/B test: run ONE read-only SELECT against the 'appdb' database.

  python scripts/rawsql.py "<select ...>" [--max-rows 100] [--log FILE]

Only statements starting with SELECT or WITH are accepted; the login is read-only anyway.
Output: compact JSON {columns, rows, truncated} or {error}.  --log appends {sql, bytes}.
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def opt(name, default=None):
    if name in sys.argv:
        i = sys.argv.index(name)
        v = sys.argv[i + 1]
        del sys.argv[i:i + 2]
        return v
    return default


def main():
    max_rows = int(opt("--max-rows", "100"))
    log = opt("--log")
    sql = sys.argv[1]
    body = re.sub(r"(?s)/\*.*?\*/|--[^\n]*", "", sql).strip().lower()
    if not re.match(r"^(;?\s*with\b|select\b)", body) or re.search(r"\b(insert|update|delete|merge|drop|alter|create|exec|execute|truncate|grant)\b", body):
        out = {"error": "only a single read-only SELECT / WITH query is allowed"}
    else:
        import pyodbc
        from senior_dev_db.config import load_config
        cfg = load_config()
        try:
            con = pyodbc.connect(cfg.connection_string(), autocommit=True, readonly=True)
            con.timeout = 60
            cur = con.cursor()
            cur.execute(sql)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchmany(max_rows + 1)
            out = {"columns": cols, "rows": [[None if v is None else str(v) for v in r] for r in rows[:max_rows]],
                   "truncated": len(rows) > max_rows}
        except Exception as e:
            out = {"error": str(e)[:500]}
    s = json.dumps(out, ensure_ascii=False)
    if log:
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps({"sql": sql[:300], "bytes": len(s.encode("utf-8"))}, ensure_ascii=False) + "\n")
    print(s)


if __name__ == "__main__":
    main()
