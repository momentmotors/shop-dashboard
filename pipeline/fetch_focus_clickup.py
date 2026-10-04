"""
Fetch ClickUp Monthly Goal tasks for "focus" projects (cars nearing delivery).

Each car has a dedicated ClickUp list named "{Owner} {Year} {Model}"
(e.g. "Vargas 66 Mustang"). Monthly Goal is a custom task type
(custom_item_id=1003) and is the only task type that gets assigned to
people, so the open MGs for a list ARE the "who owes what" picture.

Only the fields the Priority Builds grid renders are fetched: name, status,
assignee and list. Task comments and checklists were enriched per task (two
extra API calls each) to feed AI summaries; those were removed with the grid
rework, so the enrichment went too -- it was ~96% of the run's ClickUp calls
and regularly rate-limited the step.

Public entry point: fetch_focus_data(checkout_data, focus_owners)
"""

import os
import re
import time
from collections import defaultdict
from datetime import datetime

import requests


CLICKUP_BASE = "https://api.clickup.com/api/v2"

# ClickUp rate-limits per token, and enrichment fires two calls per task, so a
# busy run bunches requests tightly enough to get 429'd. Back off and retry
# rather than failing the step -- an unretried 429 drops a whole build's tasks.
MAX_RETRIES = 5
RETRY_STATUSES = {429, 500, 502, 503, 504}
DEFAULT_TEAM_ID = "9011243300"  # momentmotors workspace
MONTHLY_GOAL_CUSTOM_ITEM_ID = 1003


def _get(url, headers, params=None, timeout=30):
    """GET with exponential backoff, honoring Retry-After when ClickUp sends it."""
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=timeout)
            if resp.status_code in RETRY_STATUSES and attempt < MAX_RETRIES - 1:
                wait = float(resp.headers.get("Retry-After") or 2 ** attempt)
                print(f"    ClickUp {resp.status_code}; retrying in {wait:g}s "
                      f"(attempt {attempt + 1}/{MAX_RETRIES})...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            if attempt >= MAX_RETRIES - 1:
                raise
            wait = 2 ** attempt
            print(f"    ClickUp request failed ({e}); retrying in {wait}s "
                  f"(attempt {attempt + 1}/{MAX_RETRIES})...")
            time.sleep(wait)


def _fetch_all_open_mgs(token, team_id):
    """Paginate /team/{id}/task for all open Monthly Goal tasks."""
    headers = {"Authorization": token}
    all_tasks = []
    page = 0
    while True:
        resp = _get(
            f"{CLICKUP_BASE}/team/{team_id}/task",
            headers,
            params={
                "custom_items[]": MONTHLY_GOAL_CUSTOM_ITEM_ID,
                "include_closed": "false",
                "subtasks": "true",
                "page": page,
            },
        )
        tasks = resp.json().get("tasks", [])
        if not tasks:
            break
        all_tasks.append(tasks)
        page += 1
        # ClickUp returns 100 per page; a short page means we're done
        if len(tasks) < 100:
            break
    return [t for page_tasks in all_tasks for t in page_tasks]


def _list_matches_owner(list_name, owner):
    if not list_name or not owner:
        return False
    return bool(re.match(rf"^{re.escape(owner)}\b", list_name, re.IGNORECASE))


def _normalize_focus(focus_owners):
    """Normalize focus-projects entries into {owner, match} dicts.

    An entry may be a plain owner string ("Preheim") or an object that pins the
    ClickUp list prefix to use when an owner has more than one car:
      {"owner": "Avalos", "list": "Avalos 72 Blazer"}
    `owner` drives the vehicle lookup + display; `match` drives list matching.
    """
    norm = []
    for entry in focus_owners:
        if isinstance(entry, dict):
            owner = entry.get("owner")
            if not owner:
                continue
            norm.append({"owner": owner, "match": entry.get("list") or owner})
        elif entry:
            norm.append({"owner": entry, "match": entry})
    return norm


def _placeholder_projects(focus_entries, vehicles_by_owner, error):
    projects = []
    for entry in focus_entries:
        owner = entry["owner"]
        projects.append({
            "owner": owner,
            "vehicle": vehicles_by_owner.get(owner.lower()),
            "clickup_match": {"found": False, "error": error},
            "assignees": [],
        })
    return projects


def fetch_focus_data(checkout_data, focus_owners):
    print(f"  Focus owners requested: {focus_owners}")
    focus_entries = _normalize_focus(focus_owners)

    vehicles_by_owner = {
        (v.get("owner") or "").lower(): v
        for v in (checkout_data or {}).get("vehicles", [])
        if v.get("owner")
    }

    token = os.environ.get("CLICKUP_API_TOKEN")
    team_id = os.environ.get("CLICKUP_TEAM_ID", DEFAULT_TEAM_ID)

    if not token:
        print("  WARNING: CLICKUP_API_TOKEN not set; emitting placeholder entries")
        return {
            "updated_at": datetime.utcnow().isoformat() + "Z",
            "team_id": team_id,
            "projects": _placeholder_projects(focus_entries, vehicles_by_owner, "no_token"),
        }

    try:
        all_mgs = _fetch_all_open_mgs(token, team_id)
        print(f"  Fetched {len(all_mgs)} open Monthly Goal tasks workspace-wide")
    except Exception as e:
        print(f"  ERROR fetching MG tasks: {e}")
        return {
            "updated_at": datetime.utcnow().isoformat() + "Z",
            "team_id": team_id,
            "projects": _placeholder_projects(focus_entries, vehicles_by_owner, f"fetch_failed: {e}"),
        }

    projects = []
    for entry in focus_entries:
        owner = entry["owner"]
        match_prefix = entry["match"]
        vehicle = vehicles_by_owner.get(owner.lower())

        matching = [
            m for m in all_mgs
            if _list_matches_owner((m.get("list") or {}).get("name"), match_prefix)
        ]
        matched_lists = sorted({
            (m.get("list") or {}).get("name")
            for m in matching
            if m.get("list")
        })

        if not vehicle:
            if not matching:
                # Nothing to show at all: no checkout row and no ClickUp list.
                print(f"  '{owner}': not found in checkout data")
                projects.append({
                    "owner": owner,
                    "vehicle": None,
                    "clickup_match": {"found": False, "error": "no_vehicle"},
                    "assignees": [],
                })
                continue
            # Build is live in ClickUp but hasn't reached the checkout sheet yet.
            # Show it at 0% rather than hiding it or leaving the header blank.
            print(f"  '{owner}': not in checkout data; showing at 0%")
            vehicle = {"owner": owner, "checkout": 0, "placeholder": True}

        if not matching:
            print(f"  '{owner}': no ClickUp list match for open MGs")
            projects.append({
                "owner": owner,
                "vehicle": vehicle,
                "clickup_match": {"found": False, "error": "no_match"},
                "assignees": [],
            })
            continue

        by_assignee = defaultdict(list)
        for m in matching:
            assignees = m.get("assignees") or []
            if not assignees:
                continue
            task = {
                "id": m["id"],
                "name": m.get("name", ""),
                "status": (m.get("status") or {}).get("status", ""),
                "list": (m.get("list") or {}).get("name", ""),
                "url": m.get("url") or f"https://app.clickup.com/t/{m['id']}",
            }
            for a in assignees:
                name = a.get("username") or a.get("email") or "Unassigned"
                by_assignee[name].append(task)

        assignee_list = sorted(
            (
                {"name": n, "tasks": sorted(t, key=lambda x: x["name"])}
                for n, t in by_assignee.items()
            ),
            key=lambda x: (-len(x["tasks"]), x["name"]),
        )

        print(
            f"  '{owner}': matched {matched_lists}, "
            f"{len(matching)} open MG(s), {len(assignee_list)} assignee(s)"
        )

        projects.append({
            "owner": owner,
            "vehicle": vehicle,
            "clickup_match": {
                "found": True,
                "matched_lists": matched_lists,
                "open_tasks": len(matching),
            },
            "assignees": assignee_list,
        })

    return {
        "updated_at": datetime.utcnow().isoformat() + "Z",
        "team_id": team_id,
        "projects": projects,
    }
