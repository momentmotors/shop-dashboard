"""
Fetch ClickUp Monthly Goal tasks for "focus" projects (cars nearing delivery).

Each car has a dedicated ClickUp list named "{Owner} {Year} {Model}"
(e.g. "Vargas 66 Mustang"). Monthly Goal is a custom task type
(custom_item_id=1003) and is the only task type that gets assigned to
people, so the open MGs for a list ARE the "who owes what" picture.

Public entry point: fetch_focus_data(checkout_data, focus_owners)
"""

import hashlib
import json
import os
import re
from collections import defaultdict
from datetime import datetime

import requests


CLICKUP_BASE = "https://api.clickup.com/api/v2"
DEFAULT_TEAM_ID = "9011243300"  # momentmotors workspace
MONTHLY_GOAL_CUSTOM_ITEM_ID = 1003


def _fetch_all_open_mgs(token, team_id):
    """Paginate /team/{id}/task for all open Monthly Goal tasks."""
    headers = {"Authorization": token}
    all_tasks = []
    page = 0
    while True:
        resp = requests.get(
            f"{CLICKUP_BASE}/team/{team_id}/task",
            headers=headers,
            params={
                "custom_items[]": MONTHLY_GOAL_CUSTOM_ITEM_ID,
                "include_closed": "false",
                "subtasks": "true",
                "page": page,
            },
            timeout=30,
        )
        resp.raise_for_status()
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


def _ms_to_iso(ms_str):
    try:
        return datetime.utcfromtimestamp(int(ms_str) / 1000).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return None


def _fetch_task_detail(token, task_id):
    """Get task detail including checklists."""
    headers = {"Authorization": token}
    resp = requests.get(f"{CLICKUP_BASE}/task/{task_id}", headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _fetch_task_comments(token, task_id):
    """Get comments for a task. Returns chronological list of {author, date, text}."""
    headers = {"Authorization": token}
    resp = requests.get(f"{CLICKUP_BASE}/task/{task_id}/comment", headers=headers, timeout=15)
    resp.raise_for_status()
    raw = resp.json().get("comments", [])
    parsed = []
    for c in raw:
        text = (c.get("comment_text") or "").strip()
        if not text:
            continue
        parsed.append({
            "author": (c.get("user") or {}).get("username", "?"),
            "date": _ms_to_iso(c.get("date")),
            "text": text,
        })
    parsed.sort(key=lambda c: c["date"] or "")
    return parsed


def _flatten_checklists(checklists):
    items = []
    for cl in checklists or []:
        cl_name = cl.get("name") or ""
        for it in cl.get("items") or []:
            items.append({
                "checklist": cl_name,
                "name": it.get("name") or "",
                "resolved": bool(it.get("resolved")),
            })
    return items


def _hash_task_content(task):
    """Stable hash over the inputs that drive a Claude summary.

    Touching anything in comments or checklists invalidates the cache; touching
    the assignee or task name does not, since those are slide chrome.
    """
    payload = {
        "name": task.get("name"),
        "comments": task.get("comments", []),
        "checklist_items": task.get("checklist_items", []),
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _enrich_task(token, task):
    """Attach comments, checklist_items, and content_hash onto a task dict."""
    tid = task["id"]
    try:
        detail = _fetch_task_detail(token, tid)
    except Exception as e:
        print(f"    enrich {tid}: detail fetch failed: {e}")
        detail = {}
    try:
        comments = _fetch_task_comments(token, tid)
    except Exception as e:
        print(f"    enrich {tid}: comments fetch failed: {e}")
        comments = []
    task["comments"] = comments
    task["checklist_items"] = _flatten_checklists(detail.get("checklists"))
    task["content_hash"] = _hash_task_content(task)
    return task


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
            _enrich_task(token, task)
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
