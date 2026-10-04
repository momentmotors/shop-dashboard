#!/usr/bin/env python3
"""
Main orchestrator for the ShopDashboard data pipeline.

Fetches data from MomentOps and Google Calendar iCal feeds,
computes metrics, and writes JSON files to data/.
"""

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# Add pipeline dir to path for imports
sys.path.insert(0, os.path.dirname(__file__))

from fetch_momentops import (
    get_all_task_assignments,
    get_projects,
    get_tech_users,
    get_time_entries,
)
from compute_productivity import compute_productivity
from compute_helpers_hurters import compute_helpers_hurters
from fetch_calendar import fetch_events
from fetch_checkout import fetch_checkout_data
from fetch_focus_clickup import fetch_focus_data

# Configuration
CONFIG = {
    "productivity_target_pct": 75,
    # The export API normalizes PTO to "Paid Time Off", but match the raw
    # era names too: if that normalization ever misses, vacation hours land
    # in the productivity denominator and the number silently drops.
    "pto_task_names": ["PTO", "Paid Time Off", "Vacation"],
    "tech_role_filter": "Tech",
    "helpers_hurters_top_n": 10,
    # Cars with less non-billable time than this are summarized in one
    # line instead of getting a card. Early in a month a single 30-minute
    # entry would otherwise be most of the hurters column.
    "project_hurter_min_hours": 1.0,
    "calendar_weeks_ahead": 4,
    "history_months": 12,
}

DATA_DIR = Path(__file__).parent.parent / "data"

# One timestamp for the whole run, so every file agrees on when this data was
# built. The dashboards render this as the "Last updated" age — previously they
# showed the browser's own clock, which meant a stalled pipeline still looked
# freshly updated on the shop TVs. Full UTC precision, not just a date.
RUN_GENERATED_AT = (
    datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
)


def write_json(filename, data):
    """
    Write data to a JSON file in the data/ directory.

    Stamps generated_at centrally so no output file can quietly ship without a
    timestamp — the dashboards depend on it to detect staleness.
    """
    DATA_DIR.mkdir(exist_ok=True)
    filepath = DATA_DIR / filename
    if isinstance(data, dict):
        data = {**data, "generated_at": RUN_GENERATED_AT}
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2)
    print(f"  Wrote {filepath} ({os.path.getsize(filepath)} bytes)")


def fetch_priority_entries():
    """Priority builds from MomentOps, as focus entries the ClickUp matcher takes.

    A project's `name` is the ClickUp list name verbatim ("Ritchie 71 Blazer"),
    so it pins the list directly and the first token is the owner the checkout
    sheet and the card header use. Pinning by name rather than deriving from
    `key` matters twice over: keys are not always a faithful slug of the name,
    and an owner with two cars would otherwise match both lists.

    Returns [] on any failure, which leaves the previous focus-checkout.json in
    place rather than blanking the board -- the page is on a wall, and last
    known good beats empty.
    """
    try:
        projects = get_projects()
    except Exception as e:
        print(f"  WARNING: could not read priority flags ({e}); "
              f"keeping the last published board")
        return []

    entries = []
    for p in projects:
        if not p.get("is_priority"):
            continue
        name = (p.get("name") or "").strip()
        if not name:
            continue
        entries.append({"owner": name.split()[0], "list": name})

    if not entries:
        print(f"  WARNING: no projects flagged is_priority out of {len(projects)}; "
              f"keeping the last published board")
        return []

    print(f"  {len(entries)} priority build(s) of {len(projects)} projects: "
          f"{', '.join(e['owner'] for e in entries)}")
    return entries


def main():
    print("=" * 60)
    print("ShopDashboard Data Pipeline")
    print("=" * 60)

    today = date.today()
    print(f"Date: {today.isoformat()}")

    # Determine date range: 12 months back from start of current month
    first_of_month = today.replace(day=1)
    history_start = first_of_month
    for _ in range(CONFIG["history_months"]):
        history_start = (history_start - timedelta(days=1)).replace(day=1)

    print(f"Fetching data from {history_start} to {today}")

    # --- Hours Data (MomentOps) ---
    print("\n[1/7] Fetching Tech users from MomentOps...")
    tech_users = get_tech_users(CONFIG["tech_role_filter"])
    print(f"  Found {len(tech_users)} Tech users: {', '.join(tech_users.values())}")

    if not tech_users:
        print("  WARNING: No Tech users found. Check tech_role_filter config.")
        print("  Continuing anyway to generate empty data files...")

    tech_user_ids = set(tech_users.keys())

    print("\n[2/7] Fetching time entries from MomentOps...")
    all_entries = get_time_entries(history_start, today)
    print(f"  Fetched {len(all_entries)} total time entries")

    # Current month entries for helpers/hurters
    current_month_entries = [
        e for e in all_entries
        if e["spent_date"].startswith(today.strftime("%Y-%m"))
    ]
    print(f"  Current month ({today.strftime('%Y-%m')}): {len(current_month_entries)} entries")

    print("\n[3/7] Fetching project budgets from MomentOps...")
    task_assignments = get_all_task_assignments()
    print(f"  Fetched assignments for {len(task_assignments)} projects")

    # --- Compute Metrics ---
    print("\n[4/7] Computing metrics...")

    print("  Computing productivity...")
    productivity_data = compute_productivity(all_entries, tech_user_ids, CONFIG)
    current_pct = productivity_data["current_month"]["productivity_pct"]
    print(f"  Current month productivity: {current_pct}%")

    print("  Computing helpers/hurters...")
    hh_data = compute_helpers_hurters(
        current_month_entries, tech_user_ids, task_assignments, CONFIG
    )
    print(f"  Found {len(hh_data['helpers'])} helper groups, {len(hh_data['project_hurters'])} project hurters")

    # --- Calendar Events ---
    print("\n[5/7] Fetching calendar events...")
    events_data = fetch_events(CONFIG)
    total_events = sum(
        len(day["events"])
        for week in events_data["weeks"]
        for day in week["days"]
    )
    print(f"  Found {total_events} events over {CONFIG['calendar_weeks_ahead']} weeks")

    # --- Checkout Data ---
    print("\n[6/7] Fetching vehicle checkout data...")
    has_checkout_key = bool(os.environ.get("GOOGLE_SERVICE_ACCOUNT_KEY", ""))
    if has_checkout_key:
        checkout_data = fetch_checkout_data()
        print(f"  Found {len(checkout_data['vehicles'])} vehicles")
    else:
        print("  GOOGLE_SERVICE_ACCOUNT_KEY not set, skipping checkout data")
        checkout_data = None

    # --- Focus Project ClickUp Data ---
    print("\n[7/7] Fetching priority builds...")
    focus_data = None
    if checkout_data:
        focus_entries = fetch_priority_entries()
        if focus_entries:
            focus_data = fetch_focus_data(checkout_data, focus_entries)
    else:
        print("  Skipping (no checkout data)")

    # --- Write JSON Files ---
    print("\nWriting JSON files...")
    write_json("productivity.json", productivity_data)
    write_json("helpers-hurters.json", hh_data)
    write_json("events.json", events_data)
    if checkout_data:
        write_json("checkout.json", checkout_data)
    if focus_data:
        write_json("focus-checkout.json", focus_data)

    print("\nDone!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
