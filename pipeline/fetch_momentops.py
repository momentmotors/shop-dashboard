"""
MomentOps hours API client for ShopDashboard.

Replaces fetch_harvest.py. Reads the sanitized export endpoints on the
MomentOps portal, which serve the unified hours history -- Harvest rows
through 2026-09-30, ClickUp rows from 2026-10-01 -- in Harvest-compatible
shape. No cost/rate data crosses the boundary.

Auth via MOMENTOPS_BASE_URL and MOMENTOPS_EXPORT_TOKEN environment variables.

Public entry points mirror fetch_harvest.py exactly, so generate_data.py only
changes its import line:
    get_tech_users(role_filter)        -> {user_id: "First Last"}
    get_time_entries(from, to, user_id)-> [entry dicts]
    get_all_task_assignments()         -> {project_key: {...}}
"""

import os
import time
from datetime import date

import requests

# The export API is a single app on Railway; transient 5xx on deploy/restart
# shouldn't fail the whole pipeline run.
MAX_RETRIES = 5
RETRY_STATUSES = {429, 500, 502, 503, 504}

# The endpoint clamps `months` to this range; mirror it so we fail loudly here
# rather than silently getting a different window than we asked for.
MIN_MONTHS = 1
MAX_MONTHS = 60

# First day ClickUp-sourced entries exist. Used only for the cutover sanity
# check below, not for filtering.
CLICKUP_ERA_START = "2026-10-01"


def _base_url():
    base = os.environ["MOMENTOPS_BASE_URL"].rstrip("/")
    return base


def _headers():
    return {
        "Authorization": f"Bearer {os.environ['MOMENTOPS_EXPORT_TOKEN']}",
        "Accept": "application/json",
    }


def _get(path, params=None, timeout=120):
    """GET an export endpoint with exponential backoff on transient errors."""
    url = f"{_base_url()}{path}"
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, headers=_headers(), params=params, timeout=timeout)
            if resp.status_code in RETRY_STATUSES and attempt < MAX_RETRIES - 1:
                raise requests.exceptions.HTTPError(
                    f"{resp.status_code} transient", response=resp
                )
            resp.raise_for_status()
            return resp.json()
        except (requests.exceptions.HTTPError, requests.exceptions.ConnectionError,
                requests.exceptions.Timeout, ValueError) as e:
            if attempt >= MAX_RETRIES - 1:
                raise
            wait = 2 ** attempt
            print(f"  MomentOps request failed ({e}); retrying in {wait}s "
                  f"(attempt {attempt + 1}/{MAX_RETRIES})...")
            time.sleep(wait)


def _as_list(payload, *keys):
    """Accept either a bare JSON array or an object wrapping one.

    The handoff doc shows single objects without saying whether the response is
    a bare array or wrapped ({"entries": [...]}), so accept both rather than
    guessing and breaking on the first real call.
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return value
        for value in payload.values():
            if isinstance(value, list):
                return value
    raise ValueError(f"Unexpected export payload shape: {type(payload).__name__}")


def _months_back(from_date, to_date):
    """Whole months to request so the window covers from_date..to_date."""
    months = (to_date.year - from_date.year) * 12 + (to_date.month - from_date.month) + 1
    return max(MIN_MONTHS, min(MAX_MONTHS, months))


def _iso(value):
    return value.isoformat() if isinstance(value, date) else str(value)


def get_tech_users(role_filter="Tech"):
    """Fetch active techs. Returns dict of user_id -> user_name.

    The endpoint applies `role='Tech' AND is_active` server-side, so
    role_filter is accepted for signature compatibility with fetch_harvest and
    verified rather than applied. A different filter needs a MomentOps change.
    """
    if role_filter and role_filter != "Tech":
        print(f"  WARNING: tech_role_filter is {role_filter!r}, but the export "
              f"endpoint always filters on 'Tech'. Ask MomentOps for a role param.")

    users = _as_list(_get("/api/export/tech-users"), "users", "tech_users")
    tech = {}
    for u in users:
        if u.get("is_active") is False:
            continue
        uid, name = u.get("id"), u.get("name")
        if uid is None or not name:
            continue
        tech[uid] = name
    return tech


def get_time_entries(from_date, to_date, user_id=None):
    """
    Fetch time entries for a date range.
    Optionally filter by user_id.
    Returns list of time entry dicts in Harvest shape.

    The endpoint takes whole months back from today, so request a window wide
    enough to cover the range and trim locally to the exact dates asked for.
    """
    today = date.today()
    anchor = from_date if isinstance(from_date, date) else today
    months = _months_back(anchor, today)

    payload = _get("/api/export/time-entries", params={"months": months})
    entries = _as_list(payload, "entries", "time_entries")

    start, end = _iso(from_date), _iso(to_date)
    filtered = []
    for e in entries:
        spent = e.get("spent_date")
        if not spent or not (start <= spent <= end):
            continue
        if user_id is not None and (e.get("user") or {}).get("id") != user_id:
            continue
        filtered.append(_merge_productive(e))

    _warn_if_suspicious(entries, filtered, months, start, end)
    return filtered


def _merge_productive(entry):
    """Fold `productive` hours back into `billable`.

    MomentOps splits Harvest's billable flag in two: `billable` for work with a
    rate attached, `productive` for billable work without one -- in-house
    manufacturing, kitting and Engineering-Product. The dashboard's metric is
    the share of worked time spent on productive work, so it wants both; using
    `billable` alone understated every month by 9-25 points.

    Defaults to False if the key is absent, which degrades to the old behaviour
    rather than raising.
    """
    if entry.get("productive"):
        entry["billable"] = True
    return entry


def _warn_if_suspicious(all_entries, filtered, months, start, end):
    """Print loud warnings for the two ways this can go quietly wrong.

    An empty or pre-cutover-only response still produces valid-looking JSON and
    a dashboard reading 0%, so say so in the Actions log rather than letting it
    pass as a normal run.
    """
    if not all_entries:
        print(f"  WARNING: export returned 0 time entries for months={months}. "
              f"Hours-based pages will render empty.")
        return
    if not filtered:
        print(f"  WARNING: export returned {len(all_entries)} entries but none "
              f"fall in {start}..{end}. Check the window.")
    latest = max((e.get("spent_date") or "") for e in all_entries)
    if latest < CLICKUP_ERA_START:
        print(f"  WARNING: newest entry is {latest}, before the ClickUp cutover "
              f"({CLICKUP_ERA_START}). Post-cutover hours may not be flowing yet.")


def get_all_task_assignments():
    """Per-project budgeted hours. Returns dict of project_key -> project info.

    Budgets are per-project baselines now, not per-Harvest-task. Shape mirrors
    the old project_id -> {project_name, assignments} mapping so callers that
    only len() or iterate it keep working.
    """
    rows = _as_list(_get("/api/export/task-budgets"), "projects", "task_budgets")
    result = {}
    for r in rows:
        key = r.get("project_key")
        if not key:
            continue
        result[key] = {
            "project_name": r.get("project_name"),
            "budget_hours": r.get("budget_hours"),
            "assignments": [],
        }
    return result


def get_projects():
    """All projects with their priority/status flags.

    Returns list of {key, name, is_priority, status, build_status}. `name`
    matches the ClickUp list name, which is what the focus pipeline matches on.
    """
    return _as_list(_get("/api/export/projects"), "projects")
