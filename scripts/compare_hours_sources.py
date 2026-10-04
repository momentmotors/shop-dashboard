#!/usr/bin/env python3
"""
Parity check: Harvest vs MomentOps hours, month by month.

Gate for the hours migration. Both sources are pulled for the same window and
run through the real compute_productivity(), so what gets compared is the
number the dashboard actually shows -- not a proxy for it.

Pre-cutover months (through 2026-09-30) should match exactly: they are the same
Harvest time entries, one served by Harvest's API, the other re-served by
MomentOps. Any drift there is an ETL difference worth understanding before the
Harvest client is deleted -- most likely `billable`, which MomentOps derives as
(billable AND rate > 0) rather than copying Harvest's raw flag.

Usage:
    python3 scripts/compare_hours_sources.py              # 12 months, pre-cutover only
    python3 scripts/compare_hours_sources.py --all-months # include post-cutover too
    python3 scripts/compare_hours_sources.py --tolerance 0.5

Env: HARVEST_ACCOUNT_ID, HARVEST_TOKEN, MOMENTOPS_BASE_URL, MOMENTOPS_EXPORT_TOKEN
Exit code is 1 if any compared month drifts by more than --tolerance.
"""

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "pipeline"))

import fetch_harvest
import fetch_momentops
from compute_productivity import compute_productivity
from generate_data import CONFIG

# Last day the shop logged hours in Harvest. Months after this exist only in
# MomentOps (ClickUp-sourced), so they are not a parity question.
CUTOVER = date(2026, 9, 30)


def pull(name, module, start, end):
    print(f"\n--- {name} ---")
    users = module.get_tech_users(CONFIG["tech_role_filter"])
    print(f"  {len(users)} tech users")
    entries = module.get_time_entries(start, end)
    print(f"  {len(entries)} time entries {start}..{end}")
    prod = compute_productivity(entries, set(users.keys()), CONFIG)
    return {"users": users, "entries": entries, "monthly": prod["monthly"]}


def compare_rosters(harvest_users, momentops_users):
    h_names = {n.strip().lower(): n for n in harvest_users.values()}
    m_names = {n.strip().lower(): n for n in momentops_users.values()}

    only_h = [h_names[k] for k in sorted(set(h_names) - set(m_names))]
    only_m = [m_names[k] for k in sorted(set(m_names) - set(h_names))]

    print("\n=== Tech roster ===")
    if not only_h and not only_m:
        print(f"  Identical ({len(h_names)} techs)")
        return True
    for n in only_h:
        print(f"  Harvest only:   {n}")
    for n in only_m:
        print(f"  MomentOps only: {n}")
    print("  NOTE: a roster difference moves every month's number. Resolve this"
          " before reading anything into the hours diff below.")
    return False


def compare_months(harvest_monthly, momentops_monthly, tolerance, include_post_cutover):
    h_by_month = {m["month"]: m for m in harvest_monthly}
    m_by_month = {m["month"]: m for m in momentops_monthly}

    cutover_month = CUTOVER.strftime("%Y-%m")
    months = sorted(set(h_by_month) | set(m_by_month))
    if not include_post_cutover:
        months = [m for m in months if m <= cutover_month]

    # Each source keeps only its own last 12 months, and they don't cover the
    # same span: Harvest stops at the cutover while MomentOps runs to today, so
    # MomentOps' trim drops an extra month off the front. Compare only where
    # both sources actually have coverage -- an edge month missing for that
    # reason is an artifact of the windows, not a discrepancy.
    if h_by_month and m_by_month:
        low = max(min(h_by_month), min(m_by_month))
        high = min(max(h_by_month), max(m_by_month))
        excluded = [m for m in months if not (low <= m <= high)]
        months = [m for m in months if low <= m <= high]
        if excluded:
            print(f"\n  (outside the common window, not compared: "
                  f"{', '.join(excluded)})")

    print("\n=== Productivity % by month ===")
    print(f"{'month':<9} {'harvest':>8} {'momentops':>10} {'delta':>7}   "
          f"{'billable Δ':>11} {'total Δ':>9} {'pto Δ':>8}")

    failures = []
    for month in months:
        h = h_by_month.get(month)
        m = m_by_month.get(month)

        if h is None or m is None:
            missing = "harvest" if h is None else "momentops"
            print(f"{month:<9} {'—' if h is None else h['productivity_pct']:>8} "
                  f"{'—' if m is None else m['productivity_pct']:>10}"
                  f"      missing from {missing}")
            failures.append((month, f"missing from {missing}"))
            continue

        delta = round(m["productivity_pct"] - h["productivity_pct"], 1)
        flag = "" if abs(delta) <= tolerance else "  <-- DRIFT"
        if flag:
            failures.append((month, f"{delta:+.1f} pp"))

        print(f"{month:<9} {h['productivity_pct']:>7.1f}% {m['productivity_pct']:>9.1f}% "
              f"{delta:>+6.1f}   "
              f"{m['billable_hours'] - h['billable_hours']:>+10.1f} "
              f"{m['total_hours'] - h['total_hours']:>+8.1f} "
              f"{m['pto_hours'] - h['pto_hours']:>+7.1f}{flag}")

    return failures


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=CONFIG["history_months"],
                    help="how many months back to pull (default: pipeline's history_months)")
    ap.add_argument("--tolerance", type=float, default=0.1,
                    help="allowed productivity drift in percentage points (default: 0.1)")
    ap.add_argument("--all-months", action="store_true",
                    help="also compare months after the 2026-09-30 cutover")
    args = ap.parse_args()

    today = date.today()
    start_month = today.month - args.months
    start_year = today.year
    while start_month <= 0:
        start_month += 12
        start_year -= 1
    start = date(start_year, start_month, 1)

    print(f"Comparing {start} .. {today}  (cutover {CUTOVER}, "
          f"tolerance {args.tolerance} pp)")

    harvest = pull("Harvest", fetch_harvest, start, today)
    momentops = pull("MomentOps", fetch_momentops, start, today)

    rosters_match = compare_rosters(harvest["users"], momentops["users"])
    failures = compare_months(
        harvest["monthly"], momentops["monthly"], args.tolerance, args.all_months
    )

    print("\n=== Verdict ===")
    if failures:
        print(f"  {len(failures)} month(s) outside tolerance:")
        for month, why in failures:
            print(f"    {month}: {why}")
        print("  Do NOT delete fetch_harvest.py yet.")
        return 1

    scope = "all months" if args.all_months else f"months through {CUTOVER:%Y-%m}"
    print(f"  Parity clean for {scope} within {args.tolerance} pp.")
    if not rosters_match:
        print("  (Tech rosters differ — see above. Parity holds anyway, but know why.)")
    print("  Safe to switch generate_data.py and retire the Harvest client.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
