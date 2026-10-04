"""
Compute helpers (billable) and hurters (non-billable) from Harvest time entries.

Helpers: grouped by project (car) with tasks nested.
Hurters: shop work summary (Internal project) + project-level non-billable.
Excludes PTO from hurters.
No employee names — only project + task combos.
"""

import os
import re
from collections import defaultdict
from datetime import date, timedelta

import requests


CLICKUP_BASE = "https://api.clickup.com/api/v2"


def resolve_clickup_contexts(entries, shop_project_name):
    """
    For non-billable, non-shop-work entries with a ClickUp external_reference,
    resolve the parent task name to provide context (e.g., "Suspension").

    Returns dict: (project_name, task_name) -> set of parent names
    """
    token = os.environ.get("CLICKUP_API_TOKEN")
    if not token:
        print("  WARNING: CLICKUP_API_TOKEN not set, skipping context resolution")
        return {}

    headers = {"Authorization": token}
    task_cache = {}  # clickup_task_id -> {name, parent}

    def get_clickup_task(task_id):
        if task_id in task_cache:
            return task_cache[task_id]
        try:
            resp = requests.get(f"{CLICKUP_BASE}/task/{task_id}", headers=headers)
            resp.raise_for_status()
            data = resp.json()
            task_cache[task_id] = {"name": data.get("name", ""), "parent": data.get("parent")}
            return task_cache[task_id]
        except Exception as e:
            print(f"  ClickUp API error for task {task_id}: {e}")
            task_cache[task_id] = None
            return None

    # Collect ClickUp task IDs from non-billable, non-shop entries
    entries_to_resolve = []
    for e in entries:
        if e.get("billable", False):
            continue
        proj_name = e.get("project", {}).get("name", "Unknown")
        if proj_name == shop_project_name:
            continue
        ref = e.get("external_reference")
        if not ref:
            continue
        # Harvest returned an object with a permalink; the MomentOps export
        # returns the URL as a plain string. Accept either shape.
        permalink = ref.get("permalink", "") if isinstance(ref, dict) else str(ref)
        match = re.search(r"clickup\.com/t/([a-z0-9]+)", permalink)
        if match:
            entries_to_resolve.append((proj_name, e.get("task", {}).get("name", "Unknown"), match.group(1)))

    if not entries_to_resolve:
        return {}

    print(f"  Resolving ClickUp context for {len(entries_to_resolve)} entries...")

    # Resolve parent names
    context_map = defaultdict(set)  # (project, task) -> set of parent names
    resolved_parents = set()  # track which clickup IDs we already resolved parent for

    for proj_name, task_name, clickup_id in entries_to_resolve:
        if clickup_id in resolved_parents:
            # Already resolved this ClickUp task's parent — just look up cached result
            task_data = get_clickup_task(clickup_id)
            if task_data and task_data.get("parent"):
                parent_data = get_clickup_task(task_data["parent"])
                if parent_data:
                    context_map[(proj_name, task_name)].add(parent_data["name"])
            continue

        resolved_parents.add(clickup_id)
        task_data = get_clickup_task(clickup_id)
        if not task_data or not task_data.get("parent"):
            continue
        parent_data = get_clickup_task(task_data["parent"])
        if parent_data:
            context_map[(proj_name, task_name)].add(parent_data["name"])

    print(f"  Resolved context for {len(context_map)} task combos ({len(task_cache)} API calls cached)")
    return context_map


def compute_helpers_hurters(time_entries, tech_user_ids, task_assignments, config):
    """
    Analyze time entries to find top helpers and hurters.

    Args:
        time_entries: list of Harvest time entry dicts (current month)
        tech_user_ids: set of user IDs with Tech role
        task_assignments: dict of project_id -> {project_name, assignments}
        config: dict with pto_task_names, helpers_hurters_top_n

    Returns:
        dict with grouped helpers, shop_work summary, project_hurters, and totals
    """
    pto_names = set(config["pto_task_names"])
    top_n = config["helpers_hurters_top_n"]
    shop_project_name = config.get("shop_project_name", "Shop Work")
    hurter_floor = config.get("project_hurter_min_hours", 0)

    # Filter to Tech users
    entries = [e for e in time_entries if e["user"]["id"] in tech_user_ids]
    tech_user_count = len(tech_user_ids)

    # Count working days (weekdays) from 1st of month through today
    today = date.today()
    first_of_month = today.replace(day=1)
    num_working_days = 0
    d = first_of_month
    while d <= today:
        if d.weekday() < 5:  # Mon-Fri
            num_working_days += 1
        d += timedelta(days=1)
    num_working_days = max(num_working_days, 1)

    # Aggregate hours by (project_name, task_name, is_billable)
    combos = defaultdict(lambda: {"hours": 0.0, "billable": False})
    total_billable = 0.0
    total_non_billable = 0.0
    total_pto = 0.0

    for e in entries:
        hours = e.get("hours", 0)
        proj_name = e.get("project", {}).get("name", "Unknown")
        task_name = e.get("task", {}).get("name", "Unknown")
        is_billable = e.get("billable", False)
        is_pto = task_name in pto_names

        if is_pto:
            total_pto += hours
            continue

        key = (proj_name, task_name, is_billable)
        combos[key]["hours"] += hours
        combos[key]["billable"] = is_billable

        if is_billable:
            total_billable += hours
        else:
            total_non_billable += hours

    total_all = total_billable + total_non_billable

    # --- Helpers: group by project (car) with tasks nested ---
    project_helpers = defaultdict(lambda: {"total_hours": 0.0, "tasks": []})

    for (proj, task, _billable), data in combos.items():
        if not data["billable"]:
            continue
        project_helpers[proj]["total_hours"] += data["hours"]
        project_helpers[proj]["tasks"].append({
            "task": task,
            "hours": round(data["hours"], 1),
        })

    # Sort projects by total hours desc, tasks within each project by hours desc
    helpers_grouped = []
    for proj_name in sorted(project_helpers, key=lambda p: project_helpers[p]["total_hours"], reverse=True):
        pdata = project_helpers[proj_name]
        tasks = sorted(pdata["tasks"], key=lambda t: t["hours"], reverse=True)
        helpers_grouped.append({
            "project": proj_name,
            "total_hours": round(pdata["total_hours"], 1),
            "pct_of_total": round(pdata["total_hours"] / total_all * 100, 1) if total_all > 0 else 0,
            "tasks": tasks,
        })

    # --- Resolve ClickUp context for project hurters ---
    clickup_contexts = resolve_clickup_contexts(entries, shop_project_name)

    # --- Hurters: split into shop work vs project non-billable (grouped) ---
    shop_work_hours = 0.0
    shop_notes_hours = defaultdict(float)  # notes -> total hours
    project_hurter_groups = defaultdict(lambda: {"total_hours": 0.0, "tasks": []})

    # Aggregate shop work by notes, bucketed into fixed categories
    shop_categories = {
        "Shop Meetings": ["shop meeting"],
        "Personal Meetings": ["personal meeting"],
        "EOD Time Entry/Cleanup": ["eod time entry", "clean up", "cleanup"],
        "Organization": ["organization"],
        "Errands": ["errands"],
    }

    def categorize_shop_entry(*fields):
        """Bucket a Shop Work entry, checking each field in turn.

        Under Harvest the activity was typed into the note ("EOD time entry");
        under ClickUp it is the task name ("EOD Time Entry/Clean Up") and notes
        come back empty. Check the task name first, then the note, so both eras
        categorize and neither needs its own keyword list.
        """
        for text in fields:
            lower = (text or "").lower()
            for category, keywords in shop_categories.items():
                if any(kw in lower for kw in keywords):
                    return category
        return "Other"

    for e in entries:
        proj_name = e.get("project", {}).get("name", "Unknown")
        if proj_name != shop_project_name:
            continue
        task_name = e.get("task", {}).get("name", "Unknown")
        if task_name in pto_names:
            continue
        hours = e.get("hours", 0)
        notes = (e.get("notes") or "").strip()
        category = categorize_shop_entry(task_name, notes)
        shop_work_hours += hours
        shop_notes_hours[category] += hours

    for (proj, task, _billable), data in combos.items():
        if data["billable"]:
            continue

        if proj == shop_project_name:
            pass  # already aggregated from raw entries above
        else:
            project_hurter_groups[proj]["total_hours"] += data["hours"]
            context = sorted(clickup_contexts.get((proj, task), []))
            task_obj = {
                "task": task,
                "hours": round(data["hours"], 1),
            }
            if context:
                task_obj["context"] = context
            project_hurter_groups[proj]["tasks"].append(task_obj)

    # Shop work summary with per-person-per-day metric
    avg_per_person_per_day = 0.0
    if tech_user_count > 0 and num_working_days > 0:
        avg_per_person_per_day = round(shop_work_hours / tech_user_count / num_working_days, 2)

    # Build notes breakdown sorted by hours desc
    shop_breakdown = sorted(
        [{"label": label, "hours": round(hrs, 1)} for label, hrs in shop_notes_hours.items()],
        key=lambda x: x["hours"],
        reverse=True,
    )

    shop_work_summary = {
        "total_hours": round(shop_work_hours, 1),
        "pct_of_total": round(shop_work_hours / total_all * 100, 1) if total_all > 0 else 0,
        "avg_per_person_per_day": avg_per_person_per_day,
        "flagged": avg_per_person_per_day > 1.0,
        "tech_count": tech_user_count,
        "working_days": num_working_days,
        "breakdown": shop_breakdown,
    }

    # Build grouped project hurters, sorted by total hours desc. Cars under the
    # floor are summarized rather than listed -- their hours stay in the totals,
    # they just don't each take a card.
    project_hurters = []
    below_floor_count = 0
    below_floor_hours = 0.0
    for proj_name in sorted(project_hurter_groups, key=lambda p: project_hurter_groups[p]["total_hours"], reverse=True):
        pdata = project_hurter_groups[proj_name]
        if pdata["total_hours"] < hurter_floor:
            below_floor_count += 1
            below_floor_hours += pdata["total_hours"]
            continue
        tasks = sorted(pdata["tasks"], key=lambda t: t["hours"], reverse=True)
        project_hurters.append({
            "project": proj_name,
            "total_hours": round(pdata["total_hours"], 1),
            "pct_of_total": round(pdata["total_hours"] / total_all * 100, 1) if total_all > 0 else 0,
            "tasks": tasks,
        })

    return {
        "helpers": helpers_grouped[:top_n],
        "shop_work": shop_work_summary,
        "project_hurters": project_hurters[:top_n],
        "hurters_below_floor": {
            "count": below_floor_count,
            "hours": round(below_floor_hours, 1),
            "min_hours": hurter_floor,
        },
        "totals": {
            "billable_hours": round(total_billable, 1),
            "non_billable_hours": round(total_non_billable, 1),
            "pto_hours": round(total_pto, 1),
            "total_hours": round(total_all, 1),
        },
    }
