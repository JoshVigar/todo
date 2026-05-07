#!/usr/bin/env python3
"""Build tasks-live.json from core file + journal + caches.

Usage:
    python3 build_tasks.py \\
        --core  ~/todo/journal/2026-W19-core.md \\
        --journal ~/todo/journal/2026-W19.md \\
        --existing ~/todo/tasks-live.json \\
        --jira-cache ~/.claude/jira-status-cache.json \\
        --slack-cache ~/.claude/slack-status-cache.json \\
        --goalie-cache ~/.claude/goalie-cache.json \\
        --output ~/todo/tasks-live.json

Exit codes:
    0 — success, JSON written
    1 — core file missing
    2 — would-delete conflict (prints JSON report to stdout)
"""

import argparse
import datetime
import json
import sys
from pathlib import Path

import tasklib


def load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default if default is not None else {}


def resolve_status(parsed, jira_cache, slack_cache):
    """Determine the final status string for a parsed task."""
    if parsed["status_override"]:
        return parsed["status_override"]

    for link in parsed.get("links", []):
        url = link.get("url", "")
        label = link.get("label", "")
        # Jira ticket — exact label match or URL path segment
        for key, entry in jira_cache.items():
            if key == label or f"/{key}" in url or url.endswith(key):
                jira_status = entry.get("status", "")
                mapping = {
                    "In Progress": "in_progress",
                    "Waiting for support": "waiting_support",
                    "Waiting for customer": "waiting_customer",
                    "To Do": "todo",
                    "Backlog": "todo",
                    "Done": "done",
                    "Resolved": "done",
                    "Closed": "done",
                }
                if jira_status in mapping:
                    return mapping[jira_status]

        # Slack thread
        for thread_url, entry in slack_cache.items():
            if url == thread_url:
                slack_status = entry.get("status", "Open")
                mapping = {
                    "Open": "open",
                    "Replied": "replied",
                    "Waiting": "waiting",
                }
                return mapping.get(slack_status, "open")

    return "open"


def match_focus_task(focus_name, active_tasks):
    """Find an active task matching a Core Focus entry by name."""
    target = focus_name.lower().strip()
    # Exact match first
    for t in active_tasks:
        if t["task"].lower() == target:
            return t
    # Prefix match (task name starts with the focus entry)
    for t in active_tasks:
        if t["task"].lower().startswith(target):
            return t
    # Containment (focus entry is a substantial substring of task name)
    if len(target) >= 15:
        for t in active_tasks:
            if target in t["task"].lower():
                return t
    return None


def build_task_object(parsed, task_id, num, today_str, existing_lookup, jira_cache, slack_cache):
    """Convert a parsed task line into a JSON task object."""
    added = today_str
    if parsed["from_week"]:
        try:
            added = tasklib.week_monday(parsed["from_week"])
        except (ValueError, OverflowError):
            pass

    status = resolve_status(parsed, jira_cache, slack_cache)

    # Merge rule: carry over stable fields from existing JSON
    if parsed["task"].lower() in existing_lookup:
        existing = existing_lookup[parsed["task"].lower()]
        if existing.get("added"):
            added = existing["added"]
        # If core file marker is [ ], carry over browser-set status/due
        if parsed["marker"] == "[ ]":
            if existing.get("status"):
                status = existing["status"]
            if existing.get("due") and existing["due"] != "—":
                if not parsed["due"]:
                    parsed["due"] = existing["due"]

    return {
        "num": num,
        "id": task_id,
        "pri": parsed["pri"],
        "task": parsed["task"],
        "due": tasklib.format_due(parsed["due"], today_str),
        "from": parsed["from_week"] or "—",
        "added": added,
        "links": parsed["links"],
        "status": status,
        "why": parsed["why"] or "—",
    }


def build_completed_entry(parsed, task_id, num):
    """Convert a parsed done-section task into a completed_today entry."""
    time_str = "—"
    if parsed.get("completed_time"):
        parts = parsed["completed_time"].split()
        time_str = parts[1] if len(parts) >= 2 else parts[0]

    return {
        "num": num,
        "id": task_id,
        "task": parsed["task"],
        "links": parsed["links"],
        "time": time_str,
        "status": "done",
    }


def build_existing_lookup(existing_data):
    """Build {task_name_lowercase: {status, due, id}} from existing JSON active tasks."""
    lookup = {}
    for section in existing_data.get("sections", []):
        for task in section.get("tasks", []):
            key = task.get("task", "").lower()
            lookup[key] = {
                "status": task.get("status"),
                "due": task.get("due"),
                "id": task.get("id"),
                "added": task.get("added"),
            }
    return lookup


def build_completed_lookup(existing_data):
    """Build {task_name_lowercase: entry} from existing JSON completed_today."""
    lookup = {}
    for entry in existing_data.get("completed_today", []):
        key = entry.get("task", "").lower()
        lookup[key] = entry
    return lookup


def assign_id(parsed_task, existing_lookup, completed_lookup, id_counter):
    """Assign a stable id from existing JSON or allocate a new one."""
    key = parsed_task["task"].lower()
    if key in existing_lookup and existing_lookup[key].get("id"):
        return existing_lookup[key]["id"]
    if key in completed_lookup and completed_lookup[key].get("id"):
        return completed_lookup[key]["id"]
    id_counter[0] += 1
    return id_counter[0]


def max_existing_id(existing_data):
    """Find the highest id across all sections and completed_today."""
    ids = []
    for section in existing_data.get("sections", []):
        for task in section.get("tasks", []):
            if task.get("id"):
                ids.append(task["id"])
    for entry in existing_data.get("completed_today", []):
        if entry.get("id"):
            ids.append(entry["id"])
    return max(ids, default=0)


def main():
    parser = argparse.ArgumentParser(description="Build tasks-live.json")
    parser.add_argument("--core", required=True, help="Path to core markdown file")
    parser.add_argument("--journal", required=True, help="Path to journal markdown file")
    parser.add_argument("--existing", required=True, help="Path to existing tasks-live.json")
    parser.add_argument("--jira-cache", default="", help="Path to jira-status-cache.json")
    parser.add_argument("--slack-cache", default="", help="Path to slack-status-cache.json")
    parser.add_argument("--goalie-cache", default="", help="Path to goalie-cache.json")
    parser.add_argument("--output", required=True, help="Output path for tasks-live.json")
    args = parser.parse_args()

    # ── Load inputs ──────────────────────────────────────────────────────
    core_path = Path(args.core).expanduser()
    if not core_path.exists():
        sys.exit(1)

    core_text = core_path.read_text()
    core_lines = core_text.split("\n")

    journal_path = Path(args.journal).expanduser()
    journal_lines = []
    if journal_path.exists():
        journal_lines = journal_path.read_text().split("\n")

    existing_data = load_json(args.existing, {})
    jira_cache = load_json(args.jira_cache, {})
    slack_cache = load_json(args.slack_cache, {})
    today = datetime.date.today()
    today_str = today.isoformat()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    _, week_num, weekday = today.isocalendar()
    week_label = f"W{week_num:02d}"
    weekday_name = today.strftime("%A")
    weekday_header = f"## {weekday_name} {today_str}"

    # ── Parse active tasks ───────────────────────────────────────────────
    active_parsed = tasklib.parse_active_tasks(core_lines)
    existing_lookup = build_existing_lookup(existing_data)
    completed_lookup = build_completed_lookup(existing_data)
    id_counter = [max_existing_id(existing_data)]

    # ── Day-rollover guard for completed_today ───────────────────────────
    existing_updated = existing_data.get("updated", "")
    existing_is_today = existing_updated.startswith(today_str)

    # Completion-wins set: task names in same-day completed_today
    completion_wins = set()
    if existing_is_today:
        for entry in existing_data.get("completed_today", []):
            completion_wins.add(entry.get("task", "").lower())

    # ── Classify tasks into sections ─────────────────────────────────────
    focus_names = tasklib.parse_core_focus(journal_lines, weekday_header)
    today_had_explicit_focus = bool(focus_names)

    # Carry forward uncompleted focus tasks from the most recent previous day
    prev_focus_names = []
    for days_back in range(1, 8):
        candidate = today - datetime.timedelta(days=days_back)
        cand_header = f"## {candidate.strftime('%A')} {candidate.isoformat()}"
        cand_iso_year, cand_week, _ = candidate.isocalendar()
        if cand_iso_year == today.isocalendar()[0] and cand_week == week_num:
            cand_focus = tasklib.parse_core_focus(journal_lines, cand_header)
        else:
            cand_journal = journal_path.parent / f"{cand_iso_year}-W{cand_week:02d}.md"
            if cand_journal.exists():
                cand_lines = cand_journal.read_text().split("\n")
                cand_focus = tasklib.parse_core_focus(cand_lines, cand_header)
            else:
                cand_focus = []
        if cand_focus:
            prev_focus_names = cand_focus
            break
    today_focus_lower = {n.lower() for n in focus_names}
    for pfn in prev_focus_names:
        if pfn.lower() not in today_focus_lower:
            focus_names.append(pfn)
            today_focus_lower.add(pfn.lower())

    focus_tasks = []
    monitoring_tasks = []
    high_tasks = []
    low_tasks = []

    # Track which tasks go into Focus (by name match)
    focus_matched = set()
    for fname in focus_names:
        matched = match_focus_task(fname, active_parsed)
        if matched:
            focus_matched.add(id(matched))
            focus_tasks.append(matched)

    for parsed in active_parsed:
        if id(parsed) in focus_matched:
            continue
        # Skip tasks that were completed in the browser today
        if parsed["task"].lower() in completion_wins:
            continue
        if parsed["status_override"] == "waiting":
            monitoring_tasks.append(parsed)
        elif parsed["pri"] in ("P1", "P2"):
            high_tasks.append(parsed)
        else:
            low_tasks.append(parsed)

    # Also filter focus_tasks for completion-wins
    focus_tasks = [t for t in focus_tasks if t["task"].lower() not in completion_wins]

    # Auto-derive focus if no explicit today section and no matched tasks
    if not today_had_explicit_focus and not focus_tasks:
        candidates = [t for t in high_tasks
                      if t["status_override"] in (None, "in_progress")]
        focus_tasks = candidates[:3]
        for t in focus_tasks:
            high_tasks = [h for h in high_tasks if id(h) != id(t)]

    # ── Build task objects ───────────────────────────────────────────────
    num = 1
    sections = []

    def make_section(title, parsed_list):
        nonlocal num
        if not parsed_list and title != tasklib.SEC_FOCUS:
            return
        tasks = []
        for p in parsed_list:
            tid = assign_id(p, existing_lookup, completed_lookup, id_counter)
            obj = build_task_object(p, tid, num, today_str, existing_lookup, jira_cache, slack_cache)
            tasks.append(obj)
            num += 1
        sections.append({"title": title, "tasks": tasks})

    make_section(tasklib.SEC_FOCUS, focus_tasks)
    make_section(tasklib.SEC_MON, monitoring_tasks)
    make_section(tasklib.SEC_HIGH, high_tasks)
    make_section(tasklib.SEC_LOW, low_tasks)

    # ── Build completed_today ────────────────────────────────────────────
    completed_today = []

    # Source 1: core file Done section for today
    done_today = tasklib.parse_done_section(core_lines, target_date=today_str)
    core_done_names = set()
    for parsed in done_today:
        core_done_names.add(parsed["task"].lower())
        tid = assign_id(parsed, existing_lookup, completed_lookup, id_counter)
        # If existing JSON has this entry (same-day), preserve extra fields
        existing_entry = completed_lookup.get(parsed["task"].lower())
        entry = build_completed_entry(parsed, tid, num)
        if existing_is_today and existing_entry:
            for key in ("from_section",):
                if key in existing_entry:
                    entry[key] = existing_entry[key]
        completed_today.append(entry)
        num += 1

    # Source 2: same-day browser-added entries not in core file
    would_drop = []
    if existing_is_today:
        for entry in existing_data.get("completed_today", []):
            key = entry.get("task", "").lower()
            if key not in core_done_names:
                completed_today.append({**entry, "num": num})
                num += 1

    # ── No-silent-delete check for active sections ───────────────────────
    # Check if any same-day active tasks from existing JSON would be dropped
    if existing_is_today:
        new_active_names = set()
        for s in sections:
            for t in s["tasks"]:
                new_active_names.add(t["task"].lower())
        for ct in completed_today:
            new_active_names.add(ct.get("task", "").lower())

        for section in existing_data.get("sections", []):
            for task in section.get("tasks", []):
                name = task.get("task", "").lower()
                if name and name not in new_active_names:
                    would_drop.append({
                        "id": task.get("id"),
                        "task": task.get("task"),
                        "section": section.get("title"),
                        "reason": "not in core file active tasks",
                    })

    if would_drop:
        json.dump({"would_drop": would_drop}, sys.stdout, indent=2, ensure_ascii=False)
        sys.exit(2)

    # ── Build output ─────────────────────────────────────────────────────
    counts = tasklib.build_counts_string(core_lines)

    output = {
        "updated": now_str,
        "week": week_label,
        "sections": sections,
        "completed_today": completed_today,
        "counts": counts,
    }

    Path(args.output).expanduser().write_text(
        json.dumps(output, indent=2, ensure_ascii=False) + "\n"
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
