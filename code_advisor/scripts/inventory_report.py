"""Ad-hoc inventory report: indexed files and lines per side/extension, and skipped files by reason."""

from __future__ import annotations

import collections
import sys
import time

from senior_dev_code.config import load_config
from senior_dev_code import inventory as inv


def main() -> int:
    cfg = load_config()
    t0 = time.time()
    counts: dict[tuple[str, str, str], list[int]] = collections.defaultdict(lambda: [0, 0])
    skipped: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0])
    total_files = total_lines = 0

    for p in inv.walk_files(cfg):
        rec = inv.describe(p, cfg)
        if not rec.indexed:
            s = skipped[rec.skip_reason or "?"]
            s[0] += 1
            s[1] += rec.size_bytes
            continue
        gen = "generated" if rec.is_generated else "hand-written"
        key = (rec.side, rec.ext, gen if rec.ext == ".cs" else "-")
        c = counts[key]
        c[0] += 1
        c[1] += rec.line_count
        total_files += 1
        total_lines += rec.line_count

    print(f"walked in {time.time() - t0:.1f}s\n")
    print(f"{'side':<12}{'ext':<10}{'kind':<14}{'files':>8}{'lines':>12}")
    for (side, ext, gen), (n, ln) in sorted(counts.items()):
        print(f"{side:<12}{ext:<10}{gen:<14}{n:>8}{ln:>12,}")
    print(f"\n{'TOTAL':<36}{total_files:>8}{total_lines:>12,}")
    print("\nskipped (recorded, indexed=0):")
    for reason, (n, b) in sorted(skipped.items()):
        print(f"  {reason:<16}{n:>6} files  {b:>14,} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
