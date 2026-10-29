"""``python -m senior_dev_db.prep [--phases a,b,...] [--finalize]``

Phases write into .index/building.db; --finalize copies it to dbadvisor.db
(the file the server opens).  A phase re-run replaces only its own tables.
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import shutil
import sys
import time

from ..config import load_config
from .. import db as dbm

PHASES = ["catalog", "profile", "lookups", "sqlparse", "code", "curated_load", "link", "code_members", "style",
          "search"]


def finalize(cfg) -> None:
    src = cfg.paths.building_path
    con = dbm.connect(src, timeouts=cfg.timeouts)
    dbm.set_meta(con, "built_at", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    con.commit()
    con.execute("PRAGMA optimize")
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    dst = cfg.paths.db_path
    tmp = dst.with_suffix(".tmp")
    shutil.copyfile(src, tmp)
    for side in ("-wal", "-shm"):
        p = dst.with_name(dst.name + side)
        if p.exists():
            p.unlink()
    tmp.replace(dst)
    logging.getLogger("prep").info("finalized -> %s", dst)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="prep")
    ap.add_argument("--config")
    ap.add_argument("--phases", default=",".join(PHASES))
    ap.add_argument("--finalize", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config)
    con = dbm.connect(cfg.paths.building_path, timeouts=cfg.timeouts)
    dbm.init_schema(con)
    for ph in [p.strip() for p in args.phases.split(",") if p.strip()]:
        if ph not in PHASES:
            raise SystemExit(f"unknown phase {ph}; known: {PHASES}")
        t0 = time.time()
        mod = importlib.import_module(f".{ph}", __package__)
        summary = mod.run(cfg, con)
        dbm.phase_done(con, ph, json.dumps(summary, default=str))
        logging.getLogger("prep").info("phase %s done in %.0fs: %s", ph, time.time() - t0, summary)
    con.close()
    if args.finalize:
        finalize(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
