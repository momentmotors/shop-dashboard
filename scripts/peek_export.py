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

from fetch_momentops import get_projects, get_tech_users, get_time_entries
from generate_data import CONFIG


def show_projects():
    """Dump the projects feed and check it against the dashboard's own keys."""
    import json
    projects = get_projects()
    print(f"\n{len(projects)} projects; "
          f"{sum(1 for p in projects if p.get('is_priority'))} flagged is_priority\n")

    checkout = json.load(open("data/checkout.json"))
    owners = {(v.get("owner") or "").lower() for v in checkout.get("vehicles", [])}
    try:
        focus = json.load(open("data/focus-projects.json"))["focus"]
    except FileNotFoundError:
        focus = []  # retired once MomentOps became the source of truth
    current = {(f if isinstance(f, str) else f["owner"]).lower() for f in focus}

    print(f"  {'name':<28} {'key':<24} {'status':<12} {'build_status':<14} "
          f"owner-token in checkout? / in focus list?")
    for p in sorted(projects, key=lambda p: (not p.get("is_priority"), p.get("name") or "")):
        if not p.get("is_priority"):
            continue
        name = p.get("name") or ""
        token = name.split()[0] if name else ""
        print(f"  {name[:27]:<28} {str(p.get('key'))[:23]:<24} "
              f"{str(p.get('status'))[:11]:<12} {str(p.get('build_status'))[:13]:<14} "
              f"{'yes' if token.lower() in owners else 'NO':<4} "
              f"{'yes' if token.lower() in current else 'no'}")

    flagged = {(p.get('name') or '').split()[0].lower() for p in projects if p.get('is_priority')}
    print(f"\n  on the manual list but not is_priority: "
          f"{sorted(current - flagged) or 'none'}")
    print(f"  is_priority but not on the manual list: "
          f"{sorted(flagged - current) or 'none'}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=1)
    ap.add_argument("--project", default="Shop Work")
    ap.add_argument("--projects", action="store_true",
                    help="dump /api/export/projects and cross-check the identifiers")
    args = ap.parse_args()

    today = date.today()
    start = today.replace(day=1)
    for _ in range(args.months - 1):
        start = (start - __import__("datetime").timedelta(days=1)).replace(day=1)

    if args.projects:
        return show_projects()

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

    print(f"\n=== '{args.project}' raw entries ===")
    print(f"  {'date':<11} {'hours':>5} {'billable':>9} {'productive':>11} "
          f"{'attribution':<22} task / notes")
    for e in sorted(target, key=lambda x: x["spent_date"]):
        notes = (e.get("notes") or "").strip()
        print(f"  {e['spent_date']:<11} {e['hours']:>5.1f} "
              f"{str(e.get('billable')):>9} {str(e.get('productive')):>11} "
              f"{str(e.get('attribution_category')):<22} "
              f"{(e.get('task') or {}).get('name', '?')}"
              f"{' | ' + notes[:40] if notes else ''}")

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
