#!/usr/bin/env python3
"""Rebuild td_database.yaml from published sources.

Sources (each entry records its own URL and retrieval date):
  * Snapmaker Orca filaments_colours.json (official Full Spectrum TD values),
    pinned to a commit.
  * Prusa Knowledge Base "HueForge filament transparency values and hexcodes".
  * filamentcolors.xyz swatch library (community-measured TD per swatch) for a
    configurable list of manufacturers.

Entries whose `source` is `user-measured` (added with `spoolmix td add`) are
carried over unchanged.

Usage: uv run python scripts/build_td_database.py [--fc-manufacturers "Sunlu,Bambu Lab,..."]
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from spoolmix.tddb import SOURCES, TDDatabase  # noqa: E402
from spoolmix.tdsources import (  # noqa: E402
    DEFAULT_FC_MANUFACTURERS, filamentcolors_entries, prusa_entries, snapmaker_entries,
)

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parents[1] / "td_database.yaml")
    ap.add_argument("--fc-manufacturers", default=",".join(DEFAULT_FC_MANUFACTURERS))
    args = ap.parse_args()

    today = dt.date.today().isoformat()
    user_entries = []
    if args.out.exists():
        user_entries = [e for e in TDDatabase.load(args.out).raw_entries() if e.get("source") == "user-measured"]

    entries = snapmaker_entries(today) + prusa_entries(today)
    entries += filamentcolors_entries([m.strip() for m in args.fc_manufacturers.split(",") if m.strip()], today)
    entries += user_entries
    TDDatabase.write_new(args.out, entries, updated=today)
    counts: dict[str, int] = {}
    for e in entries:
        counts[e["source"]] = counts.get(e["source"], 0) + 1
    print(f"wrote {len(entries)} entries to {args.out}: {counts}")
    assert set(counts) <= set(SOURCES), counts


if __name__ == "__main__":
    main()
