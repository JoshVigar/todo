"""Shared parsing primitives for the task system.

Used by build_tasks.py (JSON builder) and serve-tasks.py (dashboard server).
"""

import datetime
import re

# ── Constants ────────────────────────────────────────────────────────────────

STATUS_MAP = {
    "waiting":          "⏳ Waiting",
    "waiting_support":  "⏳ Waiting for support",
    "waiting_customer": "⏳ Waiting for customer",
    "in_progress":      "🔄 In Progress",
    "blocked":          "🚫 Blocked",
    "todo":             "📋 To Do",
    "open":             "🔓 Open",
    "done":             "✅ Done",
    "replied":          "💬 Replied",
}

MARKER_TO_STATUS = {
    "[ ]": None,       # open/unset — use Jira/Slack or default to "open"
    "[-]": "in_progress",
    "[~]": "waiting",
    "[!]": "blocked",
    "[x]": "done",
    "[/]": "cancelled",
}

PRI_EMOJI = {"P1": "🔴", "P2": "🟠", "P3": "🟡", "P4": "🔵", "P5": "⏸️"}
EMOJI_TO_PRI = {v: k for k, v in PRI_EMOJI.items()}

SEC_FOCUS = "Today's Focus"
SEC_MON   = "Monitoring"
SEC_HIGH  = "High Priority"
SEC_LOW   = "Lower Priority"

# ── Regexes ──────────────────────────────────────────────────────────────────

_TASK_LINE_RE = re.compile(r"^(\s*- \[(.)\]\s+)(.*)")
_TASK_NAME_BOUNDARY = re.compile(
    r"\s+(?:"
    r"— due\s"        # due metadata: ` — due `
    r"|\(\["           # link block: ` ([label](url)...)`
    r"|_\((?:carried|why|completed|cancelled)"  # italic metadata
    r")"
)
_CARRIED_RE = re.compile(r"_\(carried from (W\d+)\)_")
_WHY_RE = re.compile(r"_\(why:\s*(.+?)\)_")
_COMPLETED_RE = re.compile(r"_\(completed:\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})\)_")
_CANCELLED_RE = re.compile(r"_\(cancelled:\s*[^)]+\)_")
_DUE_RE = re.compile(r"—?\s*due\s+(\S+)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_DATE_HEADING_RE = re.compile(r"^###\s+(\d{4}-\d{2}-\d{2})")


# ── Line parsing ─────────────────────────────────────────────────────────────

def parse_task_line(line):
    """Parse a core-file task line into a dict of raw fields.

    Returns None if the line is not a task line (doesn't match `- [.]`).

    Returned dict keys:
        marker, status_override, pri, task, due, links, why, from_week, raw_line
    """
    m = _TASK_LINE_RE.match(line.strip())
    if not m:
        return None

    marker = f"[{m.group(2)}]"
    status_override = MARKER_TO_STATUS.get(marker)
    body = m.group(3)

    # Priority emoji — strip from start of body
    pri = None
    for emoji, p in EMOJI_TO_PRI.items():
        if body.startswith(emoji + " "):
            pri = p
            body = body[len(emoji) + 1:]
            break
        if body.startswith(emoji):
            pri = p
            body = body[len(emoji):]
            break

    # Task name: everything before the first metadata boundary (— or ( or _()
    # This matches serve-tasks.py's _extract_task_name approach.
    cut = _TASK_NAME_BOUNDARY.search(body)
    task_name = (body[:cut.start()] if cut else body).rstrip()
    # Metadata portion (everything after the name boundary)
    meta = body[cut.start():] if cut else ""

    # Parse metadata from the full body (not the trimmed name)
    from_week = None
    cm = _CARRIED_RE.search(meta)
    if cm:
        from_week = cm.group(1)

    why = None
    wm = _WHY_RE.search(meta)
    if wm:
        why = wm.group(1).strip()

    completed_time = None
    ctm = _COMPLETED_RE.search(meta)
    if ctm:
        completed_time = ctm.group(1).strip()

    due = None
    dm = _DUE_RE.search(meta)
    if dm:
        due = dm.group(1)

    links = [{"label": lm.group(1), "url": lm.group(2)} for lm in _LINK_RE.finditer(meta)]

    return {
        "marker": marker,
        "status_override": status_override,
        "pri": pri,
        "task": task_name,
        "due": due,
        "links": links,
        "why": why,
        "from_week": from_week,
        "completed_time": completed_time,
        "raw_line": line,
    }


# ── Section helpers ──────────────────────────────────────────────────────────

def done_boundary(lines):
    """Index of the '## Done' line, or len(lines) if absent."""
    for i, line in enumerate(lines):
        if line.strip() == "## Done":
            return i
    return len(lines)


def cancelled_boundary(lines):
    """Index of the '## Cancelled' line, or len(lines) if absent."""
    for i, line in enumerate(lines):
        if line.strip() == "## Cancelled":
            return i
    return len(lines)


def parse_active_tasks(core_lines):
    """Parse all active (non-done, non-cancelled) task lines from core file lines.

    Returns a list of parsed task dicts (from parse_task_line).
    Stops at ## Done or ## Cancelled, whichever comes first.
    """
    boundary = min(done_boundary(core_lines), cancelled_boundary(core_lines))
    tasks = []
    for line in core_lines[:boundary]:
        parsed = parse_task_line(line)
        if parsed and parsed["status_override"] not in ("done", "cancelled"):
            tasks.append(parsed)
    return tasks


def parse_done_section(core_lines, target_date=None):
    """Parse completed tasks from the ## Done section.

    If target_date is given (str YYYY-MM-DD), only return tasks under that
    date heading. Otherwise return all done tasks with their date.

    Returns list of dicts with parse_task_line fields + 'done_date'.
    """
    start = done_boundary(core_lines)
    cancel = cancelled_boundary(core_lines)
    end = min(cancel, len(core_lines)) if cancel > start else len(core_lines)

    results = []
    current_date = None
    for line in core_lines[start:end]:
        dm = _DATE_HEADING_RE.match(line.strip())
        if dm:
            current_date = dm.group(1)
            continue
        if current_date and (target_date is None or current_date == target_date):
            parsed = parse_task_line(line)
            if parsed:
                parsed["done_date"] = current_date
                results.append(parsed)
    return results


# ── Date helpers ─────────────────────────────────────────────────────────────

def week_monday(week_str, year=None):
    """Convert 'W18' to the Monday ISO date of that week.

    Uses `year` if provided, else current year. For high week numbers
    (W52/W53) queried in early January, falls back to previous year if
    the computed date would be in the future.

    Returns ISO date string like '2026-04-27'.
    """
    w = int(week_str.lstrip("Ww"))
    y = year or datetime.date.today().year
    jan4 = datetime.date(y, 1, 4)
    start_of_w1 = jan4 - datetime.timedelta(days=jan4.weekday())
    monday = start_of_w1 + datetime.timedelta(weeks=w - 1)
    if year is None and monday > datetime.date.today() and w >= 50:
        jan4_prev = datetime.date(y - 1, 1, 4)
        start_prev = jan4_prev - datetime.timedelta(days=jan4_prev.weekday())
        monday = start_prev + datetime.timedelta(weeks=w - 1)
    return monday.isoformat()


def format_due(raw_due, today_str):
    """Format a raw due string for JSON output.

    Returns the display string: 'HH:MM', 'today', '⚠️ YYYY-MM-DD', 'YYYY-MM-DD', or '—'.
    """
    if not raw_due:
        return "—"
    # Time-only (HH:MM)
    if re.match(r"^\d{2}:\d{2}$", raw_due):
        return raw_due
    # Date
    try:
        due_date = datetime.date.fromisoformat(raw_due)
        today = datetime.date.fromisoformat(today_str)
        if due_date == today:
            return "today"
        elif due_date < today:
            return f"⚠️ {raw_due}"
        else:
            return raw_due
    except ValueError:
        return raw_due


# ── Journal parsing ──────────────────────────────────────────────────────────

def parse_core_focus(journal_lines, weekday_header):
    """Extract the numbered Core Focus list from today's journal section.

    weekday_header: e.g. '## Tuesday 2026-05-05'
    Returns a list of task name strings in order, or [] if no Core Focus found.
    """
    in_today = False
    in_focus = False
    tasks = []
    for line in journal_lines:
        stripped = line.strip()
        if stripped.startswith("## ") and weekday_header in stripped:
            in_today = True
            continue
        if in_today and stripped.startswith("## "):
            break
        if in_today and stripped == "#### Core Focus":
            in_focus = True
            continue
        if in_focus:
            if stripped.startswith("###"):
                break
            if stripped == "":
                if tasks:
                    break
                continue
            m = re.match(r"^\d+\.\s+(.+)", stripped)
            if m:
                tasks.append(m.group(1).strip())
    return tasks


def parse_goalie_sections(journal_lines, weekday_header):
    """Extract goalie subsections from today's journal section.

    Returns dict: {'Start here': [...], 'Then': [...], 'Handover': [...]}
    Each value is a list of parsed task dicts.
    """
    in_today = False
    current_sub = None
    sections = {}
    for line in journal_lines:
        stripped = line.strip()
        if stripped.startswith("## ") and weekday_header in stripped:
            in_today = True
            continue
        if in_today and stripped.startswith("## "):
            break
        if in_today and stripped.startswith("##### "):
            current_sub = stripped[6:].strip()
            sections[current_sub] = []
            continue
        if current_sub:
            if stripped.startswith("###") and not stripped.startswith("#####"):
                current_sub = None
                continue
            parsed = parse_task_line(line)
            if parsed:
                sections[current_sub].append(parsed)
    return sections


# ── Counts string ────────────────────────────────────────────────────────────

def build_counts_string(core_lines):
    """Build the '✅ N core tasks completed this week (...)' string."""
    done_tasks = parse_done_section(core_lines)
    if not done_tasks:
        return "✅ 0 core tasks completed this week"

    by_date = {}
    for t in done_tasks:
        d = t["done_date"]
        by_date[d] = by_date.get(d, 0) + 1

    total = sum(by_date.values())
    sorted_dates = sorted(by_date.keys(), reverse=True)
    parts = " · ".join(f"{by_date[d]} on {d}" for d in sorted_dates)
    return f"✅ {total} core tasks completed this week ({parts})"
