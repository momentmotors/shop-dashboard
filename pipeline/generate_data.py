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

from fetch_momentops import get_tech_users, get_time_entries, get_all_task_assignments
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
    print("\n[7/7] Fetching focus project ClickUp data...")
    focus_config_path = DATA_DIR / "focus-projects.json"
    focus_data = None
    if checkout_data and focus_config_path.exists():
        focus_owners = json.loads(focus_config_path.read_text()).get("focus", [])
        if focus_owners:
            focus_data = fetch_focus_data(checkout_data, focus_owners)
        else:
            print("  focus-projects.json has no 'focus' owners listed; skipping")
    elif not checkout_data:
        print("  Skipping (no checkout data)")
    else:
        print("  Skipping (no data/focus-projects.json)")

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
