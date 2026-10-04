#!/usr/bin/env python3
"""
Print what the MomentOps export actually returns for the current month.

A look-only diagnostic: the export token lives in Actions, so this runs there
rather than locally. Writes nothing and commits nothing.

Usage: python3 scripts/peek_export.py [--months 1] [--project "Shop Work"]
"""

import argparse
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "pipeline"))

from fetch_momentops import get_tech_users, get_time_entries
from generate_data import CONFIG


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=1)
    ap.add_argument("--project", default="Shop Work")
    args = ap.parse_args()

    today = date.today()
    start = today.replace(day=1)
    for _ in range(args.months - 1):
        start = (start - __import__("datetime").timedelta(days=1)).replace(day=1)

    users = get_tech_users(CONFIG["tech_role_filter"])
    entries = [e for e in get_time_entries(start, today)
               if e["user"]["id"] in users]
    print(f"\n{len(entries)} tech entries {start}..{today}")

    projects = defaultdict(float)
    for e in entries:
        projects[(e.get("project") or {}).get("name", "?")] += e["hours"]
    print("\n=== hours by project ===")
    for name, hrs in sorted(projects.items(), key=lambda kv: -kv[1]):
        print(f"  {hrs:>7.1f}  {name}")

    target = [e for e in entries
              if (e.get("project") or {}).get("name") == args.project]
    print(f"\n=== '{args.project}' entries: {len(target)} ===")
    if not target:
        print("  none — check the project name above")
        return 0

    by_task = defaultdict(lambda: {"hours": 0.0, "n": 0, "with_notes": 0,
                                   "sample_note": "", "categories": set()})
    for e in target:
        task = (e.get("task") or {}).get("name", "(none)")
        notes = (e.get("notes") or "").strip()
        b = by_task[task]
        b["hours"] += e["hours"]
        b["n"] += 1
        b["categories"].add(e.get("attribution_category"))
        if notes:
            b["with_notes"] += 1
            if not b["sample_note"]:
                b["sample_note"] = notes[:60]

    print(f"\n{'task name':<34} {'hours':>7} {'n':>4} {'w/notes':>8}  "
          f"attribution / sample note")
    for task, b in sorted(by_task.items(), key=lambda kv: -kv[1]["hours"]):
        cats = ",".join(sorted(str(c) for c in b["categories"]))
        tail = f"{cats}" + (f" | {b['sample_note']!r}" if b["sample_note"] else "")
        print(f"  {task[:32]:<32} {b['hours']:>7.1f} {b['n']:>4} "
              f"{b['with_notes']:>8}  {tail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
