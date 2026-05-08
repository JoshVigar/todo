#!/usr/bin/env python3
"""Serves tasks-live.json as a dark-mode HTML page with auto-refresh."""
import datetime
import email.utils
import http.server
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

import tasklib

_server = None

def _watch_self():
    path = os.path.abspath(__file__)
    mtime = os.path.getmtime(path)
    while True:
        time.sleep(3)
        try:
            new_mtime = os.path.getmtime(path)
        except OSError:
            continue
        if new_mtime != mtime:
            print("serve-tasks.py changed — reloading...")
            # Close the listening socket so the new process can rebind cleanly.
            # Threads handling in-flight requests get killed by execv (acceptable for a dev tool).
            if _server is not None:
                try:
                    _server.socket.close()
                except Exception:
                    pass
            os.execv(sys.executable, [sys.executable] + sys.argv)

if not os.environ.get("SERVE_TASKS_NO_WATCH"):
    threading.Thread(target=_watch_self, daemon=True).start()

JSON_FILE = Path.home() / "todo" / "tasks-live.json"
REQUEST_LOG = Path.home() / "todo" / "serve-tasks-requests.log"
PORT = 6419

SLACK_SNAPSHOT_FILE  = Path.home() / "todo" / "slack-triage.json"
SLACK_DISMISSED_FILE = Path.home() / "todo" / "slack-dismissed.jsonl"
SLACK_CONVERTED_FILE = Path.home() / "todo" / "slack-converted.jsonl"
SLACK_SNAPSHOT_VERSION = 1
# Dismissals naturally expire after this many days. Items still present in
# a `/slack` snapshot after the TTL re-surface — typically the right call
# (a long-stale thread that's *still* active probably warrants another look).
SLACK_DISMISS_TTL_DAYS = 14
# Lazy-compaction trigger: once a JSONL log exceeds this size, the next
# write rewrites it keeping only TTL-active records. Keeps active set tiny
# without needing a separate cron.
SLACK_LOG_COMPACT_BYTES = 100_000


_LOG_MAX_BYTES = 1_000_000  # ~1MB, then truncate to last half

def _log_request(line):
    """Append one line to the request log; never crash a request on log failure."""
    try:
        with open(REQUEST_LOG, "a") as f:
            f.write(line + "\n")
        if REQUEST_LOG.stat().st_size > _LOG_MAX_BYTES:
            data = REQUEST_LOG.read_text()
            REQUEST_LOG.write_text(data[len(data) // 2:])
    except OSError:
        pass

STATUS_MAP = {
    "waiting":          ("⏳ Waiting",             "b-waiting"),
    "waiting_support":  ("⏳ Waiting for support",  "b-waiting"),
    "waiting_customer": ("⏳ Waiting for customer", "b-waiting"),
    "in_progress":      ("🔄 In Progress",          "b-progress"),
    "blocked":          ("🚫 Blocked",              "b-blocked"),
    "todo":             ("📋 To Do",                "b-todo"),
    "open":             ("🔓 Open",                 "b-open"),
    "done":             ("✅ Done",                 "b-done"),
    "replied":          ("💬 Replied",              "b-replied"),
}

# Click cycles active states only — done is NOT in the cycle (use # cell to complete)
STATUS_CYCLE = {
    "open":        "in_progress",
    "todo":        "in_progress",
    "in_progress": "waiting",
    "waiting":     "open",
    "blocked":     "in_progress",
}

# Markdown state markers for core file
STATE_MARKER = {
    "open":        "[ ]",
    "todo":        "[ ]",
    "in_progress": "[-]",
    "waiting":     "[~]",
    "blocked":     "[!]",
    "done":        "[x]",
    "cancelled":   "[/]",
}

PRI_LABEL = {"P1": "P1 🔴", "P2": "P2 🟠", "P3": "P3 🟡", "P4": "P4 🔵", "P5": "P5 ⏸️"}
PRI_CSS   = {"P1": "p1",    "P2": "p2",    "P3": "p3",    "P4": "p4",    "P5": "p5"}
PRI_EMOJI = {"P1": "🔴",    "P2": "🟠",    "P3": "🟡",    "P4": "🔵",    "P5": "⏸️"}
PRI_CYCLE = {"P1": "P2", "P2": "P3", "P3": "P4", "P4": "P5", "P5": "P1", None: "P3"}

SEC_FOCUS = "Today's Focus"
SEC_HIGH  = "High Priority"
SEC_LOW   = "Lower Priority"
SEC_MON   = "Monitoring"


def target_section_for_pri(pri):
    """Return the canonical section title a task with this priority belongs in."""
    return SEC_HIGH if pri in ("P1", "P2") else SEC_LOW


SECTION_COLORS = {
    "monitoring":      "#e3b341",
    "high priority":   "#f0883e",
    "lower priority":  "#388bfd",
    "today's focus":   "#3fb950",
    "completed today": "#3fb950",
    "goalie":          "#bc8cff",
}

# Serialise mutating endpoints. ThreadingHTTPServer otherwise interleaves
# read-mutate-write of tasks-live.json across handlers and clobbers writes.
_state_lock = threading.Lock()

# Separate lock for slack-{triage,dismissed,converted}.json read-modify-writes.
# Always acquired AFTER _state_lock when both are needed (convert uses both).
_slack_lock = threading.Lock()


def _safe_mtime(path):
    """getmtime() that returns 0 if the file doesn't exist."""
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0


def _atomic_write_json(path, data):
    """Write JSON via tempfile + os.replace so a partial write is never observed.
    Preserves the target's existing mode bits across the replace — `mkstemp`
    creates 0600 by default, which would silently tighten permissions on a
    pre-existing file (e.g. tasks-live.json) without this guard."""
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_mode = None
    try:
        existing_mode = path.stat().st_mode & 0o777
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(
        prefix=f".{path.name}-", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        if existing_mode is not None:
            os.chmod(tmp, existing_mode)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

def _atomic_write_text(path, content):
    """Write text via tempfile + os.replace so a partial write is never observed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_mode = None
    try:
        existing_mode = path.stat().st_mode & 0o777
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(
        prefix=f".{path.name}-", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        if existing_mode is not None:
            os.chmod(tmp, existing_mode)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

# Notify SSE clients whenever the rendered state could have changed.
# `_state_version` increments on every observed mtime change of the JSON,
# this script, or the current-week core file.
_state_cond = threading.Condition()
_state_version = 0


def _state_signature():
    """Combined mtime tuple — None if any required file is missing.
    Slack files are optional; missing → 0 so their absence doesn't gate signal."""
    try:
        json_m = os.path.getmtime(JSON_FILE)
        src_m  = os.path.getmtime(os.path.abspath(__file__))
    except OSError:
        return None
    try:
        core_m = os.path.getmtime(current_core_path())
    except OSError:
        core_m = 0
    slack_t = _safe_mtime(SLACK_SNAPSHOT_FILE)
    slack_d = _safe_mtime(SLACK_DISMISSED_FILE)
    slack_c = _safe_mtime(SLACK_CONVERTED_FILE)
    return (json_m, src_m, core_m, slack_t, slack_d, slack_c)


_sse_clients = 0  # active /events stream count; protected by _state_cond


def _bump_state_version():
    """Increment the SSE state version and wake every waiting client.
    Call this from any code path that mutates a watched file directly so
    we don't wait for the next polling tick."""
    global _state_version
    with _state_cond:
        _state_version += 1
        _state_cond.notify_all()


def _watch_state():
    """Catch external mutations (tk rebuilds, manual core-file edits).
    Sleeps indefinitely while no SSE clients are connected; once a client
    arrives, polls every 2s. Server's own writes call `_bump_state_version`
    directly, so this loop is only for changes we didn't make."""
    last = _state_signature()
    while True:
        with _state_cond:
            while _sse_clients == 0:
                _state_cond.wait()  # zero clients = nothing to push to
        # On waking, do an immediate poll — covers the case where an
        # external mutation happened while no clients were connected.
        sig = _state_signature()
        if sig is not None and sig != last:
            last = sig
            _bump_state_version()
        time.sleep(2.0)


if not os.environ.get("SERVE_TASKS_NO_WATCH"):
    threading.Thread(target=_watch_state, daemon=True).start()

_TASK_NAME_BOUNDARY = tasklib._TASK_NAME_BOUNDARY

def _extract_task_name(line):
    """Pull the task name out of a core-file line like '- [X] 🟠 Task name — due 17:00 (link)'.
    Returns None if the line isn't a task line."""
    m = re.match(r"\s*- \[.\]\s+", line)
    if not m:
        return None
    body = line[m.end():]
    for emoji in PRI_EMOJI.values():
        if body.startswith(emoji + " "):
            body = body[len(emoji) + 1:]
            break
    cut = _TASK_NAME_BOUNDARY.search(body)
    return (body[:cut.start()] if cut else body).rstrip()


def _done_boundary(lines, default=None):
    """Index of the '## Done' header line, or `default` if absent."""
    return next((i for i, l in enumerate(lines) if l.strip() == "## Done"), default)


def _insert_under_dated_section(lines, section_title, dated_line, today_str, *, anchor_after=None):
    """Insert `dated_line` under `## {section_title}` → `### {today_str}`.
    If the section doesn't exist, create one — placed after `## {anchor_after}`
    if given (and present), else appended at end of file."""
    section_marker = f"## {section_title}"
    section_idx = next((i for i, l in enumerate(lines) if l.strip() == section_marker), None)

    if section_idx is None:
        block = ["", section_marker, "", f"### {today_str}", dated_line]
        anchor_idx = (
            next((i for i, l in enumerate(lines) if l.strip() == f"## {anchor_after}"), None)
            if anchor_after else None
        )
        if anchor_idx is None:
            lines.extend(block)
        else:
            insert_at = next(
                (i for i, l in enumerate(lines[anchor_idx + 1:], anchor_idx + 1) if l.startswith("## ")),
                len(lines),
            )
            for j, item in enumerate(block):
                lines.insert(insert_at + j, item)
        return

    # Scan the entire section for today's date heading before creating a new one
    heading_target = f"### {today_str}"
    end_idx = next(
        (i for i in range(section_idx + 1, len(lines)) if lines[i].startswith("## ")),
        len(lines),
    )
    existing_heading = next(
        (i for i in range(section_idx + 1, end_idx) if lines[i].strip() == heading_target),
        None,
    )
    if existing_heading is not None:
        lines.insert(existing_heading + 1, dated_line)
    else:
        lines.insert(section_idx + 1, dated_line)
        lines.insert(section_idx + 1, heading_target)
        lines.insert(section_idx + 1, "")


def _match_pills(row, pill_filters):
    """Mirror of the JS `_rowMatchesPills`. `row` is a dict with `pri`,
    `status`, `stale` (bool), `overdue` (bool); `pill_filters` is an iterable
    of `key:val` strings. Semantics: OR within key, AND across keys.

    The Python version exists so the matcher can be unit-tested and to
    pin the contract — the JS implementation must produce identical output."""
    pf = list(pill_filters)
    if not pf:
        return True
    by_key = {"pri": [], "status": [], "flag": []}
    for entry in pf:
        if ":" not in entry:
            continue
        k, v = entry.split(":", 1)
        if k in by_key:
            by_key[k].append(v)
    if by_key["pri"] and (row.get("pri") or "") not in by_key["pri"]:
        return False
    if by_key["status"] and (row.get("status") or "") not in by_key["status"]:
        return False
    if by_key["flag"]:
        flags = []
        if row.get("overdue"): flags.append("overdue")
        if row.get("stale"):   flags.append("stale")
        if not any(v in flags for v in by_key["flag"]):
            return False
    return True


def _filter_data_attrs(task):
    """Per-row data-* attributes the JS filter logic reads.
    `data-pri` / `data-status` for pill matching; `data-stale="1"` if the
    task was added ≥14 days ago. Overdue is detected via the existing
    `row-overdue` class so we don't duplicate."""
    parts = []
    pri = task.get("pri")
    if pri:
        parts.append(f' data-pri="{pri}"')
    status = task.get("status")
    if status:
        parts.append(f' data-status="{status}"')
    added = task.get("added")
    if added:
        try:
            if (datetime.date.today() - datetime.date.fromisoformat(added)).days >= 14:
                parts.append(' data-stale="1"')
        except ValueError:
            pass
    return "".join(parts)


def find_task_line(lines, task_name, *, end_idx=None, marker=None):
    """Find the first line whose extracted task name == task_name (case-insensitive).
    `marker` like '[x]' constrains the state marker; None matches any.
    Returns the index, or None if no match."""
    target = task_name.strip().lower()
    if not target:
        return None
    scan = lines if end_idx is None else lines[:end_idx]
    for i, line in enumerate(scan):
        stripped = line.strip()
        if not stripped.startswith("- ["):
            continue
        if marker and not stripped.startswith(f"- {marker}"):
            continue
        name = _extract_task_name(line)
        if name is not None and name.lower() == target:
            return i
    return None

CSS = """\
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: #0d1117;
  color: #e6edf3;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, sans-serif;
  font-size: 13px;
  padding: 16px;
}
.section-header {
  font-size: 12px;
  font-weight: 700;
  color: #e6edf3;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  padding: 0 0 0 10px;
  margin: 0 0 6px;
  border-left: 3px solid #388bfd;
  min-height: 22px;
}
.task-card {
  background: #1c2128;
  border: 1px solid #30363d;
  border-radius: 8px;
  padding: 12px 14px 8px;
  margin-bottom: 14px;
}
.task-card table { margin-bottom: 2px; }
.task-card .cmp-row:last-child { border-bottom: none; }
.task-card.focus {
  border-color: rgba(63, 185, 80, 0.45);
  background: linear-gradient(180deg, rgba(63, 185, 80, 0.07) 0%, #1c2128 80%);
  box-shadow: 0 0 0 1px rgba(63, 185, 80, 0.15), 0 2px 8px rgba(63, 185, 80, 0.05);
}
.task-card.focus .section-header {
  color: #3fb950;
  font-size: 13px;
}
.task-card.focus td { font-size: 13px; }
.task-card.high-priority {
  border-color: rgba(240, 136, 62, 0.35);
  background: linear-gradient(180deg, rgba(240, 136, 62, 0.05) 0%, #1c2128 80%);
}
.task-card.high-priority .section-header { color: #f0883e; }
table { width: 100%; border-collapse: collapse; margin-bottom: 8px; table-layout: fixed; }
th {
  position: sticky; top: 0; z-index: 1;
  background: #1c2128; color: #8b949e;
  font-weight: 600; text-align: left;
  padding: 6px 10px; border-bottom: 1px solid #30363d;
  white-space: nowrap;
}
td { padding: 5px 10px; border-bottom: 1px solid #484f58; vertical-align: middle; }
td.num { color: #484f58; cursor: pointer; user-select: none; width: 28px; text-align: center; }
td.num:hover { color: #3fb950; }
td.num:hover::after { content: " ✓"; }
td.num-done:hover { color: #f85149; }
td.num-done:hover::after { content: " ↩"; }
tr:hover td { background: #2d333b !important; }
tr.row-overdue  td { background: rgba(248, 81, 73, 0.06); }
tr.row-blocked  td { background: rgba(248, 81, 73, 0.06); }
tr.row-progress td { background: rgba(56, 139, 253, 0.05); }
tr.row-due-soon td { background: rgba(230, 179, 65, 0.07); }
tr.row-due-soon .due { color: #e3b341; font-weight: 600; }
.section-header { display: flex; align-items: center; justify-content: space-between; }
.section-subtitle {
  margin-left: 8px; font-size: 11px; font-weight: 500;
  color: #8b949e; letter-spacing: 0.02em; text-transform: none;
}
/* Floating filter popup — only visible when user presses `/`. */
#filter-popup {
  display: none; position: fixed; top: 64px;
  left: 50%; transform: translateX(-50%);
  z-index: 50; align-items: center; gap: 8px;
  background: #161b22; border: 1px solid #30363d; border-radius: 8px;
  padding: 8px 10px; box-shadow: 0 8px 24px rgba(0,0,0,0.55);
}
#filter-popup.open { display: inline-flex; }
#filter-popup .hint, #filter-popup .esc-hint {
  color: #8b949e; font-size: 11px; font-family: inherit;
  border: 1px solid #30363d; border-radius: 4px;
  padding: 1px 6px; line-height: 1.4;
}
#task-filter {
  width: 280px; max-width: 60vw;
  background: #0d1117; border: 1px solid #30363d; border-radius: 6px;
  color: #e6edf3; padding: 4px 10px; font-size: 12px;
  font-family: inherit;
}
#task-filter:focus { outline: none; border-color: #58a6ff; }
#filter-clear {
  display: none; background: transparent; border: 1px solid #30363d;
  color: #8b949e; padding: 4px 10px; border-radius: 6px;
  cursor: pointer; font-size: 11px; font-family: inherit;
  margin-left: auto;  /* push to the far right of the counts strip */
}
#filter-clear:hover { color: #e6edf3; border-color: #484f58; }
.filtered-out { display: none !important; }
/* Clickable counts-strip pills */
.cnt-group [data-filter-key] {
  cursor: pointer; border-radius: 4px; padding: 1px 4px;
  transition: background 0.1s, outline-color 0.1s;
}
.cnt-group [data-filter-key]:hover { background: rgba(255,255,255,0.05); }
.cnt-group [data-filter-key].filter-active {
  background: rgba(56,139,253,0.18);
  outline: 1px solid rgba(56,139,253,0.6);
}
#toast {
  position: fixed; bottom: 16px; left: 50%; transform: translateX(-50%);
  background: #1c2128; border: 1px solid #30363d; border-radius: 8px;
  color: #e6edf3; font-size: 13px; padding: 10px 16px;
  display: none; align-items: center; gap: 12px;
  box-shadow: 0 6px 24px rgba(0,0,0,0.4); z-index: 80;
}
#toast.open { display: inline-flex; }
#toast .toast-msg { color: #c9d1d9; }
#toast .toast-undo {
  background: transparent; border: 1px solid #30363d; color: #58a6ff;
  font-weight: 600; padding: 4px 12px; border-radius: 4px; cursor: pointer;
  font-family: inherit; font-size: 12px;
}
#toast .toast-undo:hover { background: rgba(56, 139, 253, 0.12); border-color: #58a6ff; }
#toast .toast-progress {
  display: inline-block; height: 2px; background: #58a6ff; opacity: 0.6;
  width: 60px; transform-origin: left center;
  transition: transform 5s linear;
}
#toast .toast-progress.run { transform: scaleX(0); }
.expand-all-btn {
  background: transparent; border: 0; cursor: pointer;
  color: #6e7681; font-size: 22px; line-height: 1;
  padding: 0 8px; border-radius: 4px;
}
.expand-all-btn:hover { color: #c9d1d9; background: #21262d; }
.expand-all-btn.open { color: #58a6ff; }
td.task-cell { cursor: pointer; }
td.task-cell:hover { color: #58a6ff; }
tr.expanded td.task-cell { color: #58a6ff; }
.rename-pencil {
  display: inline-block; margin-left: 6px; color: #6e7681;
  cursor: pointer; visibility: hidden; font-size: 11px;
  user-select: none;
}
tr:hover .rename-pencil, .cmp-row:hover .rename-pencil { visibility: visible; }
.rename-pencil:hover { color: #58a6ff; }
.rename-input {
  background: #0d1117; border: 1px solid #58a6ff; color: #e6edf3;
  padding: 2px 6px; border-radius: 4px; width: 100%;
  font: inherit;
}
tr.row-highlight > td:first-child, .cmp-row.row-highlight {
  box-shadow: inset 3px 0 0 #58a6ff;
}
tr.row-highlight > td { background: rgba(56, 139, 253, 0.08) !important; }
.cmp-row.row-highlight { background: rgba(56, 139, 253, 0.08); }
#help-overlay {
  display: none; position: fixed; inset: 0; z-index: 100;
  background: rgba(0,0,0,0.6); align-items: center; justify-content: center;
}
#help-overlay.open { display: flex; }
#help {
  background: #161b22; border: 1px solid #30363d; border-radius: 8px;
  padding: 24px 28px; min-width: 360px; max-width: 96vw;
  font-size: 13px;
}
.help-columns { display: flex; gap: 32px; }
.help-columns > div { flex: 1; min-width: 0; }
#help h3 { font-size: 14px; margin-bottom: 14px; color: #e6edf3; }
#help table {
  width: 100%; border-collapse: collapse;
  line-height: 1.6; table-layout: auto;  /* override global fixed layout */
}
#help td { padding: 5px 0; border: 0; vertical-align: middle; }
#help td:first-child { color: #8b949e; padding-right: 14px; }
#help td:last-child { width: 1%; white-space: nowrap; text-align: left; }
#help kbd {
  font-family: ui-monospace, monospace;
  background: #21262d; color: #58a6ff;
  padding: 2px 8px; border-radius: 4px;
  border: 1px solid #30363d;
  font-size: 12px; line-height: 1;
  display: inline-block;
}
#help tr.help-section th {
  text-align: left; padding: 12px 0 4px;
  color: #8b949e; font-size: 10px; font-weight: 700;
  text-transform: uppercase; letter-spacing: 0.06em;
  border-bottom: 1px solid #30363d;
}
#help tr.help-section:first-child th { padding-top: 0; }
#help-close {
  margin-top: 14px; background: #238636; border: 0; color: #fff;
  padding: 5px 14px; border-radius: 4px; cursor: pointer; font-size: 12px;
}
tr.row-detail { display: none; }
tr.row-detail > td {
  padding: 6px 12px 8px 40px;
  background: rgba(13, 17, 23, 0.55);
  border-bottom: 1px solid #21262d;
  color: #8b949e;
  font-size: 12px;
}
tr.row-detail > td .field { display: inline-flex; align-items: center; gap: 6px; }
tr.row-detail > td .field-label {
  font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em;
  color: #6e7681; font-weight: 700;
}
tr.row-detail > td .why-text { color: #c9d1d9; font-style: italic; }
tr.expanded + tr.row-detail { display: table-row; }
tr.expanded { background: rgba(56, 139, 253, 0.04); }
.badge {
  display: inline-block;
  padding: 2px 8px; border-radius: 10px;
  font-size: 11px; font-weight: 500; white-space: nowrap;
}
.badge.status-badge  { cursor: pointer; }
.badge.status-badge:hover  { filter: brightness(1.25); }
.badge.priority-badge { cursor: pointer; }
.badge.priority-badge:hover { filter: brightness(1.25); }
.b-progress { background: #1c3a5e; color: #79c0ff; }
.b-blocked  { background: #3d1a1a; color: #f85149; }
.b-waiting  { background: #2f2008; color: #e3b341; }
.b-todo     { background: #21262d; color: #8b949e; }
.b-open     { background: #21262d; color: #8b949e; }
.b-done     { background: #12261e; color: #3fb950; }
.b-replied  { background: #1c3a5e; color: #79c0ff; }
.p1 { background: #3d1a1a; color: #f85149; }
.p2 { background: #2f1a08; color: #f0883e; }
.p3 { background: #2f2008; color: #e3b341; }
.p4 { background: #1c2a3d; color: #79c0ff; }
.p5 { background: #21262d; color: #8b949e; }
a { color: #58a6ff; text-decoration: none; cursor: pointer; }
a:hover { text-decoration: underline; }
p.counts { margin: 6px 0; color: #8b949e; font-size: 12px; }
#topbar-actions {
  display: inline-flex; gap: 8px; flex-shrink: 0;
}
#sort-btn, #add-btn {
  background: #0d1117; border: 1px solid #30363d;
  color: #c9d1d9; padding: 6px 14px; border-radius: 6px;
  cursor: pointer; font-size: 12px; font-weight: 600;
  letter-spacing: 0.02em; font-family: inherit;
  line-height: 1; box-shadow: 0 1px 0 rgba(255,255,255,0.02) inset;
  display: inline-flex; align-items: center; gap: 6px;
}
#sort-btn:hover, #add-btn:hover { background: #21262d; border-color: #484f58; color: #e6edf3; }
#add-btn { color: #3fb950; border-color: rgba(63,185,80,0.35); }
#add-btn:hover { background: rgba(63,185,80,0.12); border-color: rgba(63,185,80,0.6); color: #3fb950; }
.btn-icon { font-weight: 700; }
/* Button labels are visible by default. They get hidden when JS detects the
 * topbar has wrapped — see _autoCompactTopbar. No viewport breakpoints. */
.btn-label { display: inline; }
/* Topbar pills wrapper — the counts strip lives here */
#topbar-pills {
  display: inline-flex; flex-wrap: wrap; gap: 8px; align-items: center;
  flex-shrink: 1; min-width: 0;
}
#topbar-pills .counts-strip {
  display: inline-flex; flex-wrap: wrap; gap: 8px; margin: 0; align-items: center;
}
#modal-overlay {
  display: none; position: fixed; inset: 0; z-index: 100;
  background: rgba(0,0,0,0.6); align-items: center; justify-content: center;
}
#modal-overlay.open { display: flex; }
#modal {
  background: #161b22; border: 1px solid #30363d; border-radius: 8px;
  padding: 20px; width: 460px; max-width: 96vw;
}
#modal h3 { font-size: 15px; margin-bottom: 14px; color: #e6edf3; }
#modal label { display: block; font-size: 12px; color: #a8b3c0; margin-bottom: 4px; }
#modal input, #modal select {
  width: 100%; background: #0d1117; border: 1px solid #30363d;
  color: #e6edf3; border-radius: 4px; padding: 6px 9px; font-size: 13px;
  margin-bottom: 10px;
}
.modal-sep { border-top: 1px solid #484f58; margin: 2px 0 10px; }
.modal-row { display: flex; gap: 8px; }
.modal-row > div { flex: 1; }
.modal-completed-section {
  display: flex; align-items: baseline; gap: 8px;
  margin-top: 10px; margin-bottom: 10px;
}
#modal .modal-completed-section input { width: auto; margin-bottom: 0; }
.modal-completed-section > label {
  margin-bottom: 0; cursor: pointer; font-size: 12px; color: #a8b3c0;
}
.modal-completed-section input[type="checkbox"] {
  width: 14px; height: 14px; min-width: 14px;
  margin: 0; accent-color: #238636; cursor: pointer;
  position: relative; top: 2px;
}
.modal-completed-at { font-size: 12px; color: #a8b3c0; }
.modal-completed-time-wrap { display: none; align-items: baseline; gap: 8px; }
.modal-completed-time-wrap.visible { display: flex; }
.modal-completed-time-col { display: flex; flex-direction: column; align-items: center; }
#m-completed-time { width: 64px; text-align: center; padding: 4px 6px; font-size: 12px; }
.modal-completed-hint { font-size: 11px; color: #6e7681; margin-top: 2px; white-space: nowrap; }
.modal-footer {
  display: flex; justify-content: flex-end; gap: 8px;
  margin-top: 8px;
}
#modal[data-mode="edit"] .modal-sep,
#modal[data-mode="edit"] .modal-completed-section,
#modal[data-mode="slack-convert"] .modal-sep,
#modal[data-mode="slack-convert"] .modal-completed-section { display: none; }
#modal-cancel {
  background: none; border: 1px solid #30363d; color: #a8b3c0;
  padding: 6px 16px; border-radius: 4px; cursor: pointer; font-size: 13px;
}
#modal-save {
  background: #238636; border: none; color: #fff;
  padding: 6px 16px; border-radius: 4px; cursor: pointer; font-size: 13px;
}
#modal-cancel:hover { background: #30363d; }
#modal-save:hover { background: #2ea043; }
tr[draggable="true"] { cursor: grab; }
tr[draggable="true"]:active { cursor: grabbing; }
tr.dragging { opacity: 0.3; }
tr.drag-over-top > td { border-top: 2px solid #388bfd !important; }
tr.drag-over-bottom > td { border-bottom: 2px solid #388bfd !important; }
#topbar {
  display: flex; align-items: center; gap: 12px;
  flex-wrap: wrap; row-gap: 6px;  /* fallback at extreme-narrow widths */
  margin-bottom: 10px;
}
.week-title {
  font-size: 22px; font-weight: 700; letter-spacing: 0.01em;
  color: #e6edf3; margin-right: 4px;
  flex-shrink: 0;
}
.week-title .wk-num { color: #3fb950; }
#view-switcher {
  display: flex; gap: 4px;
  background: #161b22; border: 1px solid #30363d; border-radius: 6px;
  padding: 3px; width: fit-content;
  flex-shrink: 0;
}
#view-switcher .vs-btn {
  padding: 4px 12px; border-radius: 4px;
  color: #8b949e; font-size: 12px; text-decoration: none;
}
#view-switcher .vs-btn:hover { color: #e6edf3; background: #21262d; text-decoration: none; }
#view-switcher .vs-btn.active { background: #30363d; color: #e6edf3; }
/* Three compaction tiers, applied by JS when the topbar would wrap.
 * Tier 1 (.compact):         hide button labels, tighten gaps and pill padding.
 * Tier 2 (.compact-tight):   also shrink week title, view-switcher, pill font.
 * Tier 3 (.compact-tightest): swap the tab-style view-switcher for a dropdown
 *                             — saves the most horizontal space.
 * Selection is content-driven — see _autoCompactTopbar — not viewport-based. */
#topbar.compact .btn-label { display: none; }
#topbar.compact { gap: 8px; }
#topbar.compact #topbar-pills .cnt-group { padding: 4px 9px; gap: 9px; font-size: 12px; }
#topbar.compact #topbar-pills .counts-strip { gap: 6px; }
#topbar.compact #sort-btn, #topbar.compact #add-btn { padding: 5px 10px; }
#topbar.compact-tight .week-title { font-size: 17px; margin-right: 0; }
#topbar.compact-tight #view-switcher { padding: 2px; }
#topbar.compact-tight #view-switcher .vs-btn { padding: 3px 8px; font-size: 11px; }
#topbar.compact-tight #topbar-pills .cnt-group { padding: 3px 7px; gap: 7px; font-size: 11px; }
#topbar.compact #filter-clear { padding: 3px 7px; font-size: 10px; }
/* Dropdown view-switcher — only shown at tier 3. */
#view-switcher-select {
  display: none;  /* hidden until tier 3 swaps in */
  background: #161b22; border: 1px solid #30363d; border-radius: 6px;
  color: #e6edf3; padding: 3px 8px; font-size: 12px; font-family: inherit;
  flex-shrink: 0; cursor: pointer;
}
#view-switcher-select:focus { outline: none; border-color: #58a6ff; }
#topbar.compact-tightest #view-switcher { display: none; }
#topbar.compact-tightest #view-switcher-select { display: inline-block; }
.counts-strip {
  display: flex; flex-wrap: wrap; gap: 8px;
  margin-bottom: 14px;
}
.cnt-group {
  background: #1c2128;
  border: 1px solid #30363d;
  border-radius: 8px;
  padding: 6px 12px;
  display: inline-flex; align-items: center; gap: 14px;
  font-size: 13px;
}
.cnt-group .label {
  color: #8b949e; font-size: 10px; text-transform: uppercase;
  letter-spacing: 0.06em; font-weight: 700;
}
.cnt-group .stat {
  display: inline-flex; align-items: center; gap: 5px;
  color: #e6edf3; font-weight: 600;
}
.cnt-group .stat .dot {
  display: inline-block; width: 9px; height: 9px; border-radius: 50%;
}
.cnt-group .stat .icon { font-size: 12px; }
.cnt-group.alert { border-color: rgba(248, 81, 73, 0.55); background: rgba(248, 81, 73, 0.08); }
.cnt-group.alert .stat { color: #f85149; }
.cnt-group.success .stat { color: #3fb950; }
.dashboard-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.5fr) minmax(0, 1fr);
  gap: 16px;
  align-items: stretch;
}
.col-right { display: flex; flex-direction: column; }
.completed-anchor { margin-top: auto; }
@media (max-width: 1100px) {
  .dashboard-grid { grid-template-columns: 1fr; }
  .completed-anchor { margin-top: 0; }
}
.spark-svg { display: block; width: 100%; height: 64px; padding: 4px 4px 0; }
.spark-labels {
  display: grid; grid-template-columns: repeat(10, 1fr);
  padding: 2px 4px 0;
}
.spark-labels .spark-label {
  display: flex; flex-direction: column; align-items: center; gap: 0;
  font-size: 9px; color: #b1bac4; line-height: 1.2;
  text-transform: uppercase; letter-spacing: 0.03em;
}
.spark-labels .spark-date { font-size: 8px; color: #8b949e; }
.spark-labels .spark-label.today .spark-day { color: #3fb950; font-weight: 700; }
.spark-labels .spark-label.today .spark-date { color: #3fb950; }
.spark-total {
  float: right; margin-right: 8px;
  display: inline-flex; align-items: baseline; gap: 4px;
  text-transform: none; letter-spacing: 0;
}
.spark-total-num {
  font-size: 14px; font-weight: 800; color: #3fb950;
  font-variant-numeric: tabular-nums;
}
.spark-total-label {
  font-size: 10px; color: #6e7681; font-weight: 500;
  text-transform: uppercase; letter-spacing: 0.06em;
}
#tooltip {
  position: fixed; display: none; pointer-events: none;
  background: #161b22; border: 1px solid #30363d; border-radius: 4px;
  padding: 4px 8px; font-size: 11px; color: #e6edf3;
  z-index: 300; white-space: nowrap;
  box-shadow: 0 2px 8px rgba(0,0,0,0.4);
}
#tooltip.visible { display: block; }
.cmp-section { display: flex; flex-direction: column; }
.cmp-row {
  display: grid;
  grid-template-columns: 24px 16px 1fr auto;
  align-items: center;
  gap: 8px;
  padding: 4px 8px;
  font-size: 12px;
  border-bottom: 1px solid #21262d;
  cursor: grab;
}
.cmp-row:hover { background: #2d333b; }
.cmp-row.dragging { opacity: 0.3; cursor: grabbing; }
.cmp-row.drag-over-top { border-top: 2px solid #388bfd; }
.cmp-row.drag-over-bottom { border-bottom: 2px solid #388bfd; }
.cmp-id, .cmp-id-done {
  color: #484f58; cursor: pointer; user-select: none; text-align: center; font-size: 11px;
}
.cmp-id:hover { color: #3fb950; }
.cmp-id:hover::after { content: " ✓"; }
.cmp-id-done:hover { color: #f85149; }
.cmp-id-done:hover::after { content: " ↩"; }
.cmp-row-done { cursor: default; }
.cmp-row-done .cmp-task { color: #8b949e; }
.cmp-pri { font-size: 11px; text-align: center; }
.cmp-task {
  color: #e6edf3; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.cmp-row.expanded .cmp-task {
  white-space: normal; overflow: visible; text-overflow: clip; line-height: 1.4;
}
.cmp-due { color: #8b949e; font-size: 11px; white-space: nowrap; }
.cmp-row.row-overdue  { background: rgba(248, 81, 73, 0.06); }
.cmp-row.row-overdue .cmp-due { color: #f85149; font-weight: 600; }
.cmp-row.row-blocked  { background: rgba(248, 81, 73, 0.06); }
.cmp-row.row-progress { background: rgba(56, 139, 253, 0.05); }
.cmp-row.row-due-soon { background: rgba(230, 179, 65, 0.07); }
.cmp-row.row-due-soon .cmp-due { color: #e3b341; font-weight: 600; }
.cmp-task { cursor: pointer; }
.cmp-task:hover { color: #58a6ff; }
.cmp-row.expanded { background: #2d333b; }
.cmp-detail {
  display: none;
  padding: 8px 12px 10px 40px;
  background: rgba(13, 17, 23, 0.55);
  border-bottom: 1px solid #21262d;
  font-size: 11px;
  gap: 16px;
  flex-wrap: wrap;
  align-items: center;
}
.cmp-detail.open { display: flex; }
.cmp-detail .field { display: inline-flex; align-items: center; gap: 6px; }
.cmp-detail .field-label {
  color: #8b949e; text-transform: uppercase; letter-spacing: 0.06em;
  font-size: 9px; font-weight: 700;
}
.cmp-detail .why-text { color: #c9d1d9; font-style: italic; }
#ctx-menu {
  position: fixed; display: none; z-index: 200;
  background: #161b22; border: 1px solid #30363d; border-radius: 6px;
  box-shadow: 0 4px 12px rgba(0,0,0,0.4);
  min-width: 180px; padding: 4px 0; font-size: 12px;
}
#ctx-menu.open { display: block; }
#ctx-menu .ctx-header {
  padding: 6px 12px 4px; color: #8b949e; font-size: 11px;
  text-transform: uppercase; letter-spacing: 0.04em; border-bottom: 1px solid #30363d;
}
#ctx-menu .ctx-item {
  padding: 7px 12px; cursor: pointer; color: #e6edf3;
}
#ctx-menu .ctx-item:hover { background: #1c2128; }
#ctx-menu .ctx-item.disabled { color: #484f58; cursor: default; }
#ctx-menu .ctx-item.disabled:hover { background: transparent; }
#ctx-menu .ctx-divider { height: 1px; background: #30363d; margin: 4px 0; }
#ctx-menu .ctx-item.danger { color: #f85149; }
#ctx-menu .ctx-item.danger:hover { background: rgba(248, 81, 73, 0.12); }

/* ---------- Slack triage view ---------- */
.slack-header {
  margin: 0 0 12px; padding: 8px 12px;
  background: #161b22; border: 1px solid #30363d; border-radius: 6px;
  font-size: 12px; color: #8b949e;
}
.slack-refresh-ts { color: #c9d1d9; font-family: ui-monospace, monospace; }
.slack-refresh-rel { color: #c9d1d9; }
.slack-stale {
  display: inline-block; margin-left: 8px;
  padding: 1px 6px; border-radius: 3px;
  background: rgba(227, 179, 65, 0.15); color: #e3b341;
  font-size: 10px; text-transform: uppercase; letter-spacing: 0.04em;
}
.slack-section.collapsed .slack-section-body { display: none; }
.slack-row {
  padding: 10px 12px; border-bottom: 1px solid #21262d;
  display: grid; grid-template-columns: 1fr auto; column-gap: 16px;
  grid-template-areas: "meta actions" "snippet actions";
}
.slack-row:last-child { border-bottom: none; }
.slack-row:hover { background: #161b22; }
.slack-meta { grid-area: meta; display: flex; gap: 10px; align-items: baseline; flex-wrap: wrap; }
.slack-sender { color: #e6edf3; font-weight: 600; }
.slack-target { color: #8b949e; font-size: 12px; font-family: ui-monospace, monospace; }
.slack-ts { color: #6e7681; font-size: 11px; text-decoration: none; }
.slack-ts:hover { color: #58a6ff; text-decoration: underline; }
.slack-snippet {
  grid-area: snippet; margin-top: 4px;
  color: #c9d1d9; font-size: 13px; line-height: 1.5;
  overflow-wrap: anywhere;
}
.slack-actions {
  grid-area: actions; display: flex; gap: 6px; align-self: center;
}
.slack-actions button {
  background: #21262d; color: #c9d1d9; border: 1px solid #30363d;
  border-radius: 4px; padding: 4px 10px; font-size: 12px; cursor: pointer;
}
.slack-actions button:hover { background: #30363d; color: #e6edf3; }
.slack-actions .slack-dismiss:hover { color: #f85149; border-color: #f85149; }
.slack-actions .slack-dismiss-thread:hover {
  color: #ffa198; border-color: #f85149;
  background: rgba(248, 81, 73, 0.08);
}
.slack-actions .btn-icon { font-weight: 700; margin-right: 2px; }
.slack-empty { padding: 16px 12px; color: #6e7681; font-style: italic; }
.slack-noise {
  margin: 12px 0 0; padding: 8px 12px;
  color: #6e7681; font-size: 11px; font-style: italic;
}
.slack-empty-state {
  padding: 32px 24px; text-align: center; color: #8b949e;
}
.slack-empty-state h3 { color: #c9d1d9; margin: 0 0 8px; }
.slack-empty-state p { margin: 0; font-size: 13px; }
.slack-empty-state code {
  background: #161b22; padding: 1px 6px; border-radius: 3px;
  font-family: ui-monospace, monospace; font-size: 12px;
}
"""

def _js_consts():
    """Emit JS map literals from the Python constants — single source of truth."""
    status_label = {k: v[0] for k, v in STATUS_MAP.items()}
    status_cls   = {k: v[1] for k, v in STATUS_MAP.items()}
    pri_next     = {k: v for k, v in PRI_CYCLE.items() if k}
    return (
        f"var STATUS_NEXT = {json.dumps(dict(STATUS_CYCLE))};\n"
        f"var STATUS_LABEL = {json.dumps(status_label, ensure_ascii=False)};\n"
        f"var STATUS_CLS = {json.dumps(status_cls)};\n"
        f"var PRI_NEXT = {json.dumps(pri_next)};\n"
        f"var PRI_LABEL = {json.dumps(PRI_LABEL, ensure_ascii=False)};\n"
        f"var PRI_CLS = {json.dumps(PRI_CSS)};\n"
    )


SCRIPT = """\
// _post — POST JSON to a mutating endpoint and refresh the view from the
// server response. Always refreshes (success → new state, failure → old
// state, which clears any optimistic mutation). Returns the Response so
// callers can do special-case handling (e.g. close a modal on success).
function _post(url, payload) {
  return fetch(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload || {})
  }).then(function(r) {
    _refreshTasks(true);
    if (!r.ok) {
      // Surface server-side rejection — without this the user sees nothing
      // when /edit, /add, etc. return 400 from validation.
      console.warn('POST', url, 'returned', r.status);
      _showErrorToast(url + ' failed (' + r.status + ') — see ' +
                      '~/todo/serve-tasks-requests.log');
    }
    return r;
  });
}

function _showErrorToast(message) {
  var toast = document.getElementById('toast');
  if (!toast) return;
  toast.querySelector('.toast-msg').textContent = message;
  var progress = toast.querySelector('.toast-progress');
  progress.classList.remove('run');
  // Hide the Undo button — there's nothing to undo on failure
  var undo = toast.querySelector('.toast-undo');
  if (undo) undo.style.display = 'none';
  toast.classList.add('open');
  requestAnimationFrame(function() {
    requestAnimationFrame(function() { progress.classList.add('run'); });
  });
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(function() {
    toast.classList.remove('open');
    if (undo) undo.style.display = '';
  }, 5000);
}

// Toast for undo-able actions. Shows for 5s; click Undo to fire `onUndo`.
var _toastTimer = null;
function _showToast(message, onUndo) {
  var toast = document.getElementById('toast');
  if (!toast) return;
  toast.querySelector('.toast-msg').textContent = message;
  var progress = toast.querySelector('.toast-progress');
  progress.classList.remove('run');
  toast.classList.add('open');
  // Trigger CSS transition by toggling .run after a frame
  requestAnimationFrame(function() {
    requestAnimationFrame(function() { progress.classList.add('run'); });
  });
  var undoBtn = toast.querySelector('.toast-undo');
  // Always make Undo visible for success toasts — clears any inline
  // `display: none` left over from a preceding error toast (cloneNode
  // would otherwise preserve it).
  undoBtn.style.display = '';
  // Replace the button to remove any old listener
  var fresh = undoBtn.cloneNode(true);
  undoBtn.parentNode.replaceChild(fresh, undoBtn);
  fresh.addEventListener('click', function() {
    clearTimeout(_toastTimer);
    toast.classList.remove('open');
    onUndo();
  });
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(function() { toast.classList.remove('open'); }, 5000);
}

// Filter visible tasks by substring match against task name.
// Re-runs on every input event and after every DOM swap so the filter
// survives SSE pushes and click-driven refreshes.
// Active pill filters as `key:val` strings (`pri:P1`, `status:waiting`,
// `flag:overdue`, `flag:stale`). Multiple active = union (OR). Combines
// with the text input via AND.
var _pillFilters = new Set();

function _rowMatchesPills(row) {
  if (_pillFilters.size === 0) return true;
  // Bucket selected pills by key. Semantic: OR within a key (any selected
  // P1/P2 matches); AND across keys (must satisfy every key that has at
  // least one selected pill). Mirrors `_match_pills` in serve-tasks.py.
  var byKey = {pri: [], status: [], flag: []};
  _pillFilters.forEach(function(entry) {
    var idx = entry.indexOf(':');
    var k = entry.slice(0, idx), v = entry.slice(idx + 1);
    if (byKey[k]) byKey[k].push(v);
  });
  var pri = row.dataset.pri || '';
  var status = row.dataset.status || '';
  var stale = row.dataset.stale === '1';
  var overdue = row.classList.contains('row-overdue');
  if (byKey.pri.length && byKey.pri.indexOf(pri) === -1) return false;
  if (byKey.status.length && byKey.status.indexOf(status) === -1) return false;
  if (byKey.flag.length) {
    var anyFlag = byKey.flag.some(function(v) {
      return (v === 'overdue' && overdue) || (v === 'stale' && stale);
    });
    if (!anyFlag) return false;
  }
  return true;
}

function _applyFilter() {
  var input = document.getElementById('task-filter');
  var q = input ? input.value.trim().toLowerCase() : '';
  var rows = document.querySelectorAll('tr[draggable="true"], .cmp-row[data-id]');
  rows.forEach(function(row) {
    var nameEl = row.querySelector('.task-name') ||
                 row.querySelector('.task-cell, .cmp-task');
    var text = (nameEl ? nameEl.textContent : row.textContent).toLowerCase();
    var textMatch = !q || text.indexOf(q) !== -1;
    var pillMatch = _rowMatchesPills(row);
    var hide = !(textMatch && pillMatch);
    row.classList.toggle('filtered-out', hide);
    var nxt = row.nextElementSibling;
    if (nxt && (nxt.classList.contains('row-detail') || nxt.classList.contains('cmp-detail'))) {
      nxt.classList.toggle('filtered-out', hide);
    }
  });
  // Hide whole cards (sections) with no visible matches while any filter is active.
  var anyFilter = q || _pillFilters.size > 0;
  document.querySelectorAll('.task-card').forEach(function(card) {
    if (!anyFilter) { card.classList.remove('filtered-out'); return; }
    var visible = card.querySelectorAll(
      'tr[draggable="true"]:not(.filtered-out), .cmp-row[data-id]:not(.filtered-out)'
    );
    card.classList.toggle('filtered-out', visible.length === 0);
  });
  // Mirror active state onto the pills.
  document.querySelectorAll('[data-filter-key]').forEach(function(pill) {
    var entry = pill.dataset.filterKey + ':' + pill.dataset.filterVal;
    pill.classList.toggle('filter-active', _pillFilters.has(entry));
  });
  // Clear button visible when anything is filtering.
  var clearBtn = document.getElementById('filter-clear');
  if (clearBtn) clearBtn.style.display = anyFilter ? 'inline-block' : 'none';
}

function _clearFilters() {
  _pillFilters.clear();
  var input = document.getElementById('task-filter');
  if (input) input.value = '';
  _applyFilter();
}

// Detects whether the #topbar has wrapped to a second row. Iteratively
// applies .compact then .compact-tight to recover a single-row layout.
// Driven by content, not viewport thresholds, so it adapts to whatever
// combination of pills/badges happens to render.
//
// IMPORTANT: do NOT compare children's offsetTop here. The topbar uses
// `align-items: center`, which makes mixed-height children land at
// different offsetTop values even on a single row (each child centered
// within the row's max height). The reliable signal is whether any
// child's rect sits BELOW the first child's rect — which can only
// happen if it wrapped to a new row (row-gap > 0).
var _compactingTopbar = false;
function _autoCompactTopbar() {
  if (_compactingTopbar) return;
  var topbar = document.getElementById('topbar');
  if (!topbar) return;
  var children = topbar.children;
  if (children.length < 2) return;
  _compactingTopbar = true;
  try {
    topbar.classList.remove('compact', 'compact-tight', 'compact-tightest');
    void topbar.offsetHeight;  // force layout flush after class reset
    function isWrapped() {
      var firstBottom = children[0].getBoundingClientRect().bottom;
      for (var i = 1; i < children.length; i++) {
        // 1px tolerance for sub-pixel rounding.
        if (children[i].getBoundingClientRect().top >= firstBottom - 1) {
          return true;
        }
      }
      return false;
    }
    if (isWrapped()) {
      topbar.classList.add('compact');
      void topbar.offsetHeight;
      if (isWrapped()) {
        topbar.classList.add('compact-tight');
        void topbar.offsetHeight;
        if (isWrapped()) {
          // Tier 3: swap tabs → dropdown for the biggest space win.
          topbar.classList.add('compact-tightest');
        }
      }
    }
  } finally {
    // Release the guard on the next frame so a same-tick resize event
    // (which fires from our own class change) doesn't recurse.
    requestAnimationFrame(function() { _compactingTopbar = false; });
  }
}
window.addEventListener('resize', _autoCompactTopbar);
document.addEventListener('DOMContentLoaded', _autoCompactTopbar);
// Tier-3 dropdown: navigate when the user picks a different view.
document.addEventListener('change', function(e) {
  if (e.target && e.target.id === 'view-switcher-select') {
    window.location.href = '?view=' + encodeURIComponent(e.target.value);
  }
});

function _showFilterPopup() {
  var pop = document.getElementById('filter-popup');
  if (pop) pop.classList.add('open');
}
function _hideFilterPopup() {
  var pop = document.getElementById('filter-popup');
  // Keep it open if the user has typed something — closing would hide that
  // they're filtering. Only collapse on empty value.
  var input = document.getElementById('task-filter');
  if (pop && input && !input.value) pop.classList.remove('open');
}
(function() {
  var input = document.getElementById('task-filter');
  if (input) {
    input.addEventListener('input', _applyFilter);
    input.addEventListener('blur', _hideFilterPopup);
  }
})();

// Toggle every expandable row in `scope` (defaults to whole document).
// If anything is collapsed, expand all; if everything is already expanded,
// collapse all. Syncs every expand-all chevron in scope to match.
function _toggleExpandAll(scope) {
  scope = scope || document;
  var rows = scope.querySelectorAll('tr[draggable="true"], .cmp-row[data-id]');
  if (!rows.length) return;
  var anyCollapsed = Array.prototype.some.call(rows, function(r) {
    return !r.classList.contains('expanded');
  });
  rows.forEach(function(r) {
    r.classList.toggle('expanded', anyCollapsed);
    var nxt = r.nextElementSibling;
    if (nxt && nxt.classList.contains('cmp-detail')) {
      nxt.classList.toggle('open', anyCollapsed);
    }
  });
  scope.querySelectorAll('.expand-all-btn').forEach(function(b) {
    b.textContent = anyCollapsed ? '▴' : '▾';
    b.classList.toggle('open', anyCollapsed);
  });
}

document.addEventListener('click', function(e) {
  // Clear-filters button (lives inside #tasks-content, swapped each refresh)
  if (e.target.closest('[data-action="clear-filters"]')) {
    e.preventDefault();
    _clearFilters();
    return;
  }

  // Counts-strip pill click → toggle a filter. Multiple pills combine.
  var pill = e.target.closest('[data-filter-key]');
  if (pill) {
    e.preventDefault();
    var entry = pill.dataset.filterKey + ':' + pill.dataset.filterVal;
    if (_pillFilters.has(entry)) _pillFilters.delete(entry);
    else _pillFilters.add(entry);
    _applyFilter();
    return;
  }

  // Rename pencil — swap the task-name span for an <input>. Save on Enter
  // or blur; cancel on Esc. Stops propagation so the row's expand toggle
  // doesn't also fire.
  var pencil = e.target.closest('[data-action="rename"]');
  if (pencil) {
    e.preventDefault();
    e.stopPropagation();
    var rowEl = pencil.closest('tr[data-id], .cmp-row[data-id]');
    if (!rowEl) return;
    var nameSpan = rowEl.querySelector('.task-name');
    if (!nameSpan) return;
    var current = nameSpan.textContent;
    var taskId = parseInt(rowEl.dataset.id);
    var input = document.createElement('input');
    input.type = 'text';
    input.value = current;
    input.className = 'rename-input';
    nameSpan.replaceWith(input);
    pencil.style.display = 'none';
    input.focus();
    input.select();
    var done = false;
    function commit(save) {
      if (done) return;
      done = true;
      var newName = input.value.trim();
      if (save && newName && newName !== current) {
        _post('/rename', {id: taskId, name: newName});
      } else {
        _refreshTasks(true);  // restore the row from server state
      }
    }
    input.addEventListener('keydown', function(ev) {
      ev.stopPropagation();  // don't trigger global hotkeys
      if (ev.key === 'Enter') { ev.preventDefault(); commit(true); }
      else if (ev.key === 'Escape') { ev.preventDefault(); commit(false); }
    });
    input.addEventListener('blur', function() { commit(true); });
    return;
  }

  // Expand-all chevron in section headers — toggles every detail panel
  // in this section's task-card (or whatever wrapper holds the rows).
  var expandBtn = e.target.closest('[data-action="expand-all"]');
  if (expandBtn) {
    e.preventDefault();
    var scope = expandBtn.closest('.task-card') || expandBtn.parentElement.parentElement;
    _toggleExpandAll(scope);
    return;
  }

  // Uncomplete via # cell on completed rows (table view OR compact dashboard view)
  var done_td = e.target.closest('td.num-done, .cmp-id-done');
  if (done_td && done_td.dataset.id) {
    e.preventDefault();
    var row = done_td.closest('tr, .cmp-row');
    if (row) {
      row.style.opacity = '0.35';
      row.style.transition = 'opacity 0.2s';
    }
    _post('/uncomplete', {id: parseInt(done_td.dataset.id)});
    return;
  }

  // Complete task via # cell on active rows (table OR compact dashboard)
  var num_td = e.target.closest('td.num:not(.num-done), .cmp-id');
  if (num_td && num_td.dataset.id) {
    e.preventDefault();
    var row = num_td.closest('tr, .cmp-row');
    if (row) {
      row.style.opacity = '0.35';
      row.style.transition = 'opacity 0.2s';
    }
    _post('/complete', {id: parseInt(num_td.dataset.id)});
    return;
  }

  // Status cycle (optimistic — refresh confirms / corrects)
  var badge = e.target.closest('.status-badge');
  if (badge && badge.dataset.id && badge.dataset.status) {
    e.preventDefault();
    var cur = badge.dataset.status;
    var next = STATUS_NEXT[cur];
    if (next) {
      badge.dataset.status = next;
      badge.textContent = STATUS_LABEL[next] || next;
      badge.className = 'badge status-badge ' + (STATUS_CLS[next] || 'b-open');
      _post('/update', {id: parseInt(badge.dataset.id)});
    }
    return;
  }

  // Priority cycle (optimistic)
  var pri = e.target.closest('.priority-badge');
  if (pri && pri.dataset.id && pri.dataset.pri) {
    e.preventDefault();
    var curP = pri.dataset.pri;
    var nextP = PRI_NEXT[curP] || 'P3';
    pri.dataset.pri = nextP;
    pri.textContent = PRI_LABEL[nextP] || nextP;
    pri.className = 'badge priority-badge ' + (PRI_CLS[nextP] || '');
    _post('/update-pri', {id: parseInt(pri.dataset.id)});
    return;
  }

  // (tooltip handler installed below; nothing to do for it on click)
  // External links
  var a = e.target.closest('a');
  if (a && a.href && !a.href.startsWith('http://localhost')) {
    e.preventDefault();
    fetch('/open?url=' + encodeURIComponent(a.href));
  }

  // Compact-row expand: click task name to toggle the detail panel
  var taskSpan = e.target.closest('.cmp-task');
  if (taskSpan) {
    var row = taskSpan.closest('.cmp-row');
    if (row) {
      var detail = row.nextElementSibling;
      if (detail && detail.classList.contains('cmp-detail') && detail.dataset.id === row.dataset.id) {
        detail.classList.toggle('open');
        row.classList.toggle('expanded');
      }
    }
  }

  // Table-row expand: click task cell to toggle .expanded on the row.
  // Same idiom as compact rows — reveals the truncated Why column.
  var taskCell = e.target.closest('td.task-cell');
  if (taskCell) {
    var trRow = taskCell.closest('tr');
    if (trRow) trRow.classList.toggle('expanded');
  }
});

// Sort button
document.getElementById('sort-btn').addEventListener('click', function() {
  _post('/sort', {});
});

// Modal — same DOM serves both Add and Edit. Mode lives on
// #modal[data-mode]; _editTaskId is non-null only while editing.
// `_editFetchToken` discards the in-flight /task fetch when the user
// dismisses the modal or opens a different one before it resolves —
// without it, a stale .then() would clobber the new state.
var _editTaskId = null;
var _editFetchToken = 0;
// Slack triage convert flow reuses the Add modal; this holds the source
// item's id so the save handler can route to /slack/convert.
var _slackConvertId = null;
function _resetModal() {
  ['m-task','m-due','m-why','m-link-label','m-link-url'].forEach(function(id){
    var el = document.getElementById(id);
    if (el) el.value = '';
  });
  var pri = document.getElementById('m-pri');
  if (pri) pri.value = 'P2';
  var cb = document.getElementById('m-completed');
  if (cb) cb.checked = false;
  _toggleCompleted(false);
}
function _nowHHMM() {
  var d = new Date();
  return ('0'+d.getHours()).slice(-2) + ':' + ('0'+d.getMinutes()).slice(-2);
}
function _toggleCompleted(on) {
  var wrap = document.getElementById('m-completed-wrap');
  var hint = document.getElementById('m-completed-hint');
  if (on) {
    wrap.classList.add('visible');
    hint.textContent = 'Empty = now (' + _nowHHMM() + ')';
  } else {
    wrap.classList.remove('visible');
    hint.textContent = '';
    document.getElementById('m-completed-time').value = '';
  }
}
document.getElementById('m-completed').addEventListener('change', function() {
  _toggleCompleted(this.checked);
});
document.getElementById('m-completed-time').addEventListener('input', function() {
  var v = this.value.replace(/[^0-9]/g, '');
  if (v.length > 4) v = v.slice(0, 4);
  if (v.length > 2) v = v.slice(0, 2) + ':' + v.slice(2);
  this.value = v;
});
function _openAddCompletedModal() {
  _openAddModal();
  document.getElementById('m-completed').checked = true;
  _toggleCompleted(true);
}
function _openAddModal() {
  _editFetchToken++;
  _resetModal();
  _editTaskId = null;
  var modal = document.getElementById('modal');
  modal.dataset.mode = 'add';
  document.getElementById('modal-title').textContent = 'Add Task';
  document.getElementById('modal-save').textContent = 'Add task';
  document.getElementById('modal-overlay').classList.add('open');
  setTimeout(function(){ document.getElementById('m-task').focus(); }, 50);
}
function _openEditModal(taskId) {
  // Coerce to integer at the boundary — _hilitId comes from
  // `row.dataset.id` which is always a string. Without this, the eventual
  // /edit POST sends `"id": "34"` and find_task_by_id misses on int==str.
  taskId = parseInt(taskId, 10);
  if (isNaN(taskId)) return;
  var token = ++_editFetchToken;
  fetch('/task?id=' + encodeURIComponent(taskId)).then(function(r) {
    if (token !== _editFetchToken) return;  // superseded by close/openAdd/openEdit
    if (!r.ok) return;
    return r.json();
  }).then(function(t) {
    if (token !== _editFetchToken || !t) return;
    _editTaskId = taskId;
    document.getElementById('m-task').value = t.task || '';
    document.getElementById('m-pri').value = t.pri || 'P2';
    var due = t.due || '';
    document.getElementById('m-due').value = (due === '—' ? '' : due);
    var why = t.why || '';
    document.getElementById('m-why').value = (why === '—' ? '' : why);
    var link = (t.links && t.links[0]) || {};
    document.getElementById('m-link-label').value = link.label || '';
    document.getElementById('m-link-url').value = link.url || '';
    var modal = document.getElementById('modal');
    modal.dataset.mode = 'edit';
    document.getElementById('modal-title').textContent = 'Edit Task';
    document.getElementById('modal-save').textContent = 'Save changes';
    document.getElementById('modal-overlay').classList.add('open');
    setTimeout(function(){ document.getElementById('m-task').focus(); }, 50);
  });
}
document.getElementById('add-btn').addEventListener('click', _openAddModal);
function closeModal() {
  _editFetchToken++;  // discard any in-flight /task fetch
  document.getElementById('modal-overlay').classList.remove('open');
  _editTaskId = null;
  _slackConvertId = null;
  var modal = document.getElementById('modal');
  if (modal) modal.dataset.mode = 'add';
}
document.getElementById('modal-cancel').addEventListener('click', closeModal);
// Cmd+Enter (or Ctrl+Enter) inside the Add modal triggers Save —
// usual pattern in chat / form apps so users don't have to mouse-click.
document.getElementById('modal-overlay').addEventListener('keydown', function(e) {
  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
    e.preventDefault();
    document.getElementById('modal-save').click();
  }
});
document.getElementById('modal-overlay').addEventListener('click', function(e) {
  if (e.target === this) closeModal();
});
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') closeModal();
});
document.getElementById('modal-save').addEventListener('click', function() {
  var task = document.getElementById('m-task').value.trim();
  if (!task) { document.getElementById('m-task').focus(); return; }
  var payload = {
    task: task,
    pri: document.getElementById('m-pri').value,
    due: document.getElementById('m-due').value.trim() || '\u2014',
    why: document.getElementById('m-why').value.trim() || '\u2014',
    link_label: document.getElementById('m-link-label').value.trim(),
    link_url: document.getElementById('m-link-url').value.trim()
  };
  var cb = document.getElementById('m-completed');
  if (cb && cb.checked) {
    var ct = document.getElementById('m-completed-time');
    payload.completed_at = ct.value || _nowHHMM();
  }
  var mode = document.getElementById('modal').dataset.mode || 'add';
  function _onSaved(r) {
    if (!r.ok) return;
    closeModal();
    _resetModal();
  }
  if (mode === 'edit') {
    payload.id = _editTaskId;
    _post('/edit', payload).then(_onSaved);
  } else if (mode === 'slack-convert') {
    payload.id = _slackConvertId;
    _post('/slack/convert', payload).then(_onSaved);
  } else {
    _post('/add', payload).then(_onSaved);
  }
});

// Drag-and-drop reordering — works on both full table rows and compact rows
var DRAG_SEL = 'tr[draggable="true"], .cmp-row[draggable="true"]';
var _dragNum = null, _dragPaused = false;
document.addEventListener('dragstart', function(e) {
  var el = e.target.closest(DRAG_SEL);
  if (!el) return;
  _dragNum = parseInt(el.dataset.id);
  _dragPaused = true;
  el.classList.add('dragging');
  e.dataTransfer.effectAllowed = 'move';
  e.dataTransfer.setData('text/plain', String(_dragNum));
});
document.addEventListener('dragend', function(e) {
  _dragPaused = false;
  _dragNum = null;
  document.querySelectorAll('.dragging').forEach(function(r) { r.classList.remove('dragging'); });
  document.querySelectorAll('.drag-over-top, .drag-over-bottom').forEach(function(r) {
    r.classList.remove('drag-over-top', 'drag-over-bottom');
  });
});
document.addEventListener('dragover', function(e) {
  var el = e.target.closest(DRAG_SEL);
  if (!el || el.dataset.id == String(_dragNum)) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = 'move';
  document.querySelectorAll('.drag-over-top, .drag-over-bottom').forEach(function(r) {
    r.classList.remove('drag-over-top', 'drag-over-bottom');
  });
  var mid = el.getBoundingClientRect().top + el.getBoundingClientRect().height / 2;
  el.classList.add(e.clientY < mid ? 'drag-over-top' : 'drag-over-bottom');
});
document.addEventListener('drop', function(e) {
  var el = e.target.closest(DRAG_SEL);
  if (!el || !_dragNum) return;
  e.preventDefault();
  var toNum = parseInt(el.dataset.id);
  if (_dragNum === toNum) return;
  var before = e.clientY < el.getBoundingClientRect().top + el.getBoundingClientRect().height / 2;
  _post('/reorder', {from: _dragNum, to: toNum, before: before});
});

// View persistence — remember the user's chosen view across sessions.
(function() {
  var url = new URL(window.location.href);
  if (url.searchParams.has('view')) {
    localStorage.setItem('tasksView', url.searchParams.get('view'));
  } else {
    var stored = localStorage.getItem('tasksView');
    if (stored && stored !== 'dashboard') {
      url.searchParams.set('view', stored);
      window.location.replace(url.toString());
    }
  }
})();

// Scroll-preserving auto-refresh (updates both content and styles).
// `force=true` bypasses the focus guard for click-driven refreshes (the
// user just clicked, they want immediate feedback). SSE / poll / focus
// listeners pass nothing and respect focus.
function _refreshTasks(force) {
  if (_dragPaused) return;
  if (!force && !document.hasFocus()) return;
  // Don't blow away an in-progress rename — refreshing replaces the
  // entire #tasks-content innerHTML, which would drop the user's input.
  var ae = document.activeElement;
  if (ae && ae.classList && ae.classList.contains('rename-input')) return;
  var sy = window.scrollY;
  // Preserve which detail panels are currently expanded across the swap
  var openIds = Array.prototype.map.call(
    document.querySelectorAll('.cmp-detail.open'),
    function(el) { return el.dataset.id; }
  );
  var expandedTrIds = Array.prototype.map.call(
    document.querySelectorAll('tr.expanded[draggable="true"]'),
    function(el) { return el.dataset.id; }
  );
  var slackSectionState = {};
  document.querySelectorAll('.slack-section[data-tier]').forEach(function(el) {
    slackSectionState[el.dataset.tier] = el.classList.contains('collapsed');
  });
  // Preserve the current view by including the search string in the fetch URL
  fetch('/' + window.location.search).then(function(r) {
    if (r.status === 304) return null;
    return r.text();
  }).then(function(html) {
    if (!html) return;
    var doc = new DOMParser().parseFromString(html, 'text/html');
    // Swap the topbar pills (counts strip) so they reflect new state.
    // Filter input + week badge live in the same #topbar but outside this
    // sub-element, so they stay untouched (preserves filter focus / value).
    var freshPills = doc.querySelector('#topbar-pills');
    var curPills = document.getElementById('topbar-pills');
    if (freshPills && curPills) {
      curPills.innerHTML = freshPills.innerHTML;
      _autoCompactTopbar();  // pill width may have changed → re-check fit
    }
    var fresh = doc.querySelector('#tasks-content');
    if (fresh) {
      document.getElementById('tasks-content').innerHTML = fresh.innerHTML;
      window.scrollTo(0, sy);
      openIds.forEach(function(id) {
        var detail = document.querySelector('.cmp-detail[data-id="' + id + '"]');
        if (detail) {
          detail.classList.add('open');
          var prev = detail.previousElementSibling;
          if (prev && prev.classList.contains('cmp-row')) prev.classList.add('expanded');
        }
      });
      expandedTrIds.forEach(function(id) {
        var row = document.querySelector('tr[draggable="true"][data-id="' + id + '"]');
        if (row) row.classList.add('expanded');
      });
      Object.keys(slackSectionState).forEach(function(tier) {
        var sec = document.querySelector('.slack-section[data-tier="' + tier + '"]');
        if (!sec) return;
        var btn = sec.querySelector('[data-action="expand-all"]');
        if (slackSectionState[tier]) {
          sec.classList.add('collapsed');
          if (btn) btn.textContent = '▾';
        } else {
          sec.classList.remove('collapsed');
          if (btn) btn.textContent = '▴';
        }
      });
      // Re-apply keyboard-nav highlight if it survived the swap
      if (_hilitId != null) _setHighlight(_hilitId);
      // Preserve the active filter across refreshes
      _applyFilter();
    }
    var freshStyle = doc.querySelector('style');
    if (freshStyle) {
      var cur = document.querySelector('style');
      if (cur && cur.textContent !== freshStyle.textContent) {
        cur.textContent = freshStyle.textContent;
      }
    }
  }).catch(function(){});
}
// Server-Sent Events: server pushes a `data:` line whenever any backing
// file mtime changes (~50ms latency). Polling stays as a fallback in case
// the SSE connection drops, but at 30s instead of 2s.
var _es = null;
function _connectSSE() {
  try {
    if (_es) _es.close();
    _es = new EventSource('/events');
    _es.onmessage = function() { _refreshTasks(); };
    // EventSource auto-reconnects on error; nothing to do.
  } catch (e) {}
}
_connectSSE();
setInterval(_refreshTasks, 30000);
// Refresh immediately on regaining focus so you see fresh data as soon as you switch back.
// `_refreshTasks` itself drops the call when the tab isn't focused.
window.addEventListener('focus', _refreshTasks);

// Right-click context menu — move tasks between sections without dragging
var _ctxTaskId = null;
document.addEventListener('contextmenu', function(e) {
  var el = e.target.closest('tr[data-id], .cmp-row[data-id]');
  if (!el) return;
  e.preventDefault();
  _ctxTaskId = parseInt(el.dataset.id);
  var menu = document.getElementById('ctx-menu');
  // Use clientX/clientY because the menu is position:fixed (viewport-relative).
  // pageX/pageY include scroll offset and would push the menu below the click.
  var x = Math.min(e.clientX, window.innerWidth - 200);
  var y = Math.min(e.clientY, window.innerHeight - 220);
  menu.style.left = x + 'px';
  menu.style.top = y + 'px';
  menu.classList.add('open');
});
document.addEventListener('click', function(e) {
  var item = e.target.closest('#ctx-menu .ctx-item');
  if (item && !item.classList.contains('disabled') && _ctxTaskId !== null) {
    var taskId = _ctxTaskId;
    if (item.dataset.action === 'cancel') {
      _post('/cancel', {id: taskId});
      _showToast('Task cancelled', function() { _post('/uncancel', {id: taskId}); });
    } else if (item.dataset.action === 'complete') {
      _post('/complete', {id: taskId});
    } else if (item.dataset.action === 'edit') {
      _openEditModal(taskId);
    } else {
      _post('/move-section', {id: taskId, section: item.dataset.section});
    }
    document.getElementById('ctx-menu').classList.remove('open');
    _ctxTaskId = null;
    return;
  }
  if (!e.target.closest('#ctx-menu')) {
    document.getElementById('ctx-menu').classList.remove('open');
  }
});
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') document.getElementById('ctx-menu').classList.remove('open');
});

// Row highlight (keyboard navigation). _hilitId tracks the currently
// highlighted task by stable id so it survives DOM swaps via _refreshTasks.
var _hilitId = null;
function _highlightableRows() {
  return document.querySelectorAll('tr[draggable="true"], .cmp-row[data-id]');
}
function _setHighlight(id) {
  document.querySelectorAll('.row-highlight').forEach(function(r) {
    r.classList.remove('row-highlight');
  });
  _hilitId = null;
  if (id == null) return;
  var rows = _highlightableRows();
  for (var i = 0; i < rows.length; i++) {
    if (String(rows[i].dataset.id) === String(id)) {
      rows[i].classList.add('row-highlight');
      rows[i].scrollIntoView({block: 'nearest'});
      _hilitId = id;
      return;
    }
  }
}
// Click-to-highlight: any click within a row sets it as the keyboard-nav
// target so subsequent hotkeys (e/s/p/Enter) act on the row the user just
// clicked. Layered on top of existing click handlers — they all still fire.
// Skip when clicking a link (link nav takes precedence) or any element
// inside a modal-style overlay.
document.addEventListener('click', function(e) {
  if (e.target.closest('a[href], #modal-overlay, #help-overlay, #ctx-menu, .rename-input')) return;
  var row = e.target.closest(
    'tr[draggable="true"][data-id], .cmp-row[data-id]'
  );
  if (row && row.dataset.id) _setHighlight(row.dataset.id);
});

function _moveHighlight(dir) {
  var rows = _highlightableRows();
  if (!rows.length) return;
  var idx = -1;
  if (_hilitId != null) {
    for (var i = 0; i < rows.length; i++) {
      if (String(rows[i].dataset.id) === String(_hilitId)) { idx = i; break; }
    }
  }
  var next = idx === -1
    ? (dir > 0 ? 0 : rows.length - 1)
    : (idx + dir + rows.length) % rows.length;
  _setHighlight(rows[next].dataset.id);
}
function _scrollToSection(needle) {
  var headers = document.querySelectorAll('.section-header');
  for (var i = 0; i < headers.length; i++) {
    if (headers[i].textContent.toLowerCase().indexOf(needle.toLowerCase()) !== -1) {
      headers[i].scrollIntoView({behavior: 'smooth', block: 'start'});
      return;
    }
  }
}

// Help overlay open/close
function _showHelp() { document.getElementById('help-overlay').classList.add('open'); }
function _closeHelp() { document.getElementById('help-overlay').classList.remove('open'); }
document.getElementById('help-close').addEventListener('click', _closeHelp);
document.getElementById('help-overlay').addEventListener('click', function(e) {
  if (e.target === this) _closeHelp();
});

// Hotkeys. Fire only when page is focused, no modal/help is open, no
// input is focused, and no Meta/Ctrl/Alt is held (so Cmd+R / Cmd+A still work).
// Shift IS allowed — Shift+S, Shift+P, Shift+1/2/3 are part of the scheme.
document.addEventListener('keydown', function(e) {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (!document.hasFocus()) return;
  var modal = document.getElementById('modal-overlay');
  var help = document.getElementById('help-overlay');
  var t = document.activeElement;
  var inInput = t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' ||
                      t.tagName === 'SELECT' || t.isContentEditable);

  // Esc always handled — narrow rules so it does the least surprising thing:
  //   in filter input + has text  → clear the text (keep pills, keep focus)
  //   in filter input + no text   → blur + close popup (keep pills)
  //   not in filter + any filter  → clear all filters
  //   else                        → collapse expanded rows
  if (e.key === 'Escape') {
    if (help && help.classList.contains('open')) { _closeHelp(); return; }
    var fi2 = document.getElementById('task-filter');
    if (fi2 && t === fi2) {
      if (fi2.value) { fi2.value = ''; _applyFilter(); }
      else { fi2.blur(); _hideFilterPopup(); }
      return;
    }
    if ((fi2 && fi2.value) || _pillFilters.size > 0) {
      _clearFilters();
      return;
    }
    // (modal Escape and ctx-menu Escape are handled by their own listeners)
    // Collapse all expanded rows on Esc when no overlay is open
    document.querySelectorAll('tr.expanded, .cmp-row.expanded').forEach(function(r) {
      r.classList.remove('expanded');
    });
    document.querySelectorAll('.cmp-detail.open').forEach(function(d) {
      d.classList.remove('open');
    });
    document.querySelectorAll('.expand-all-btn').forEach(function(b) {
      b.textContent = '▾';
      b.classList.remove('open');
    });
    return;
  }

  // ? opens help; / focuses filter — only when not typing somewhere
  if (e.key === '?' && !inInput) {
    e.preventDefault(); _showHelp(); return;
  }
  if (e.key === '/' && !inInput) {
    var fi = document.getElementById('task-filter');
    if (fi) { e.preventDefault(); _showFilterPopup(); fi.focus(); fi.select(); return; }
  }

  // Suppress remaining hotkeys while any modal-style overlay is open or input focused
  if (modal && modal.classList.contains('open')) return;
  if (help && help.classList.contains('open')) return;
  if (inInput) return;

  // Section jumps (use e.code so it's layout-independent)
  if (e.shiftKey) {
    if (e.code === 'Digit1') { e.preventDefault(); _scrollToSection("Today's Focus"); return; }
    if (e.code === 'Digit2') { e.preventDefault(); _scrollToSection('High Priority'); return; }
    if (e.code === 'Digit3') { e.preventDefault(); _scrollToSection('Lower Priority'); return; }
    // Mutate highlighted row
    if (e.code === 'KeyS' && _hilitId != null) {
      e.preventDefault(); _post('/update', {id: parseInt(_hilitId)}); return;
    }
    if (e.code === 'KeyP' && _hilitId != null) {
      e.preventDefault(); _post('/update-pri', {id: parseInt(_hilitId)}); return;
    }
    if (e.code === 'KeyA') {
      e.preventDefault(); _openAddCompletedModal(); return;
    }
    return;
  }

  // Single-letter / arrow hotkeys
  if (e.key === 'x') { e.preventDefault(); _toggleExpandAll(); return; }
  if (e.key === 'r') { e.preventDefault(); _refreshTasks(true); return; }
  if (e.key === 's') { e.preventDefault(); _post('/sort', {}); return; }
  if (e.key === 'a') { e.preventDefault(); _openAddModal(); return; }
  if (e.key === 'e' && _hilitId != null) {
    e.preventDefault(); _openEditModal(_hilitId); return;
  }
  if (e.key === 'c') {
    e.preventDefault();
    var u = new URL(window.location);
    var cur = u.searchParams.get('view') || 'dashboard';
    u.searchParams.set('view', cur === 'dashboard' ? 'classic' : 'dashboard');
    window.location.href = u.toString();
    return;
  }
  if (e.key === 'j' || e.key === 'ArrowDown') { e.preventDefault(); _moveHighlight(1); return; }
  if (e.key === 'k' || e.key === 'ArrowUp') { e.preventDefault(); _moveHighlight(-1); return; }
  if (e.key === 'Enter' && _hilitId != null) {
    e.preventDefault();
    var row = document.querySelector(
      'tr[draggable="true"][data-id="' + _hilitId + '"], .cmp-row[data-id="' + _hilitId + '"]'
    );
    if (row) {
      row.classList.toggle('expanded');
      var nxt = row.nextElementSibling;
      if (nxt && nxt.classList.contains('cmp-detail')) nxt.classList.toggle('open');
    }
    return;
  }
});

// ---------- Slack triage view ----------
// The slack view embeds its item records as a JSON script tag so the
// convert modal can pre-fill from a single in-page lookup, rather than
// roundtripping to a /slack/<id> endpoint every click.
function _slackItems() {
  var el = document.getElementById('slack-items-data');
  if (!el) return {};
  try { return JSON.parse(el.textContent); }
  catch (e) { return {}; }
}

function _openSlackConvertModal(item) {
  if (!item) return;
  _editFetchToken++;  // discard any in-flight /task fetch
  _resetModal();
  _editTaskId = null;
  _slackConvertId = (item.channel_id || '') + ':' + (item.message_ts || '');
  var sender = item.sender || '';
  var name = item.is_dm
    ? 'Reply to ' + sender
    : 'Reply to ' + sender + ' in #' + (item.channel_name || '');
  document.getElementById('m-task').value = name;
  document.getElementById('m-why').value = item.snippet || '';
  document.getElementById('m-link-label').value = 'Slack';
  document.getElementById('m-link-url').value = item.permalink || '';
  document.getElementById('m-pri').value = 'P2';
  var modal = document.getElementById('modal');
  modal.dataset.mode = 'slack-convert';
  document.getElementById('modal-title').textContent = 'Convert Slack item';
  document.getElementById('modal-save').textContent = 'Add task';
  document.getElementById('modal-overlay').classList.add('open');
  setTimeout(function(){ document.getElementById('m-task').focus(); }, 50);
}

document.addEventListener('click', function(e) {
  // Convert button → open modal pre-filled, OR quick-add when ⌘/Ctrl held
  var convertBtn = e.target.closest('.slack-convert');
  if (convertBtn) {
    e.preventDefault();
    e.stopPropagation();
    var row = convertBtn.closest('.slack-row');
    if (!row) return;
    var item = _slackItems()[row.dataset.id];
    if (!item) return;
    if (e.metaKey || e.ctrlKey) {
      // Quick-add: bypass modal, POST /slack/convert directly with the
      // same defaults the modal would have populated. Useful when the
      // pre-fill is good enough as-is and the modal is just friction.
      var sender = item.sender || '';
      var name = item.is_dm
        ? 'Reply to ' + sender
        : 'Reply to ' + sender + ' in #' + (item.channel_name || '');
      var quickPayload = {
        id: (item.channel_id || '') + ':' + (item.message_ts || ''),
        task: name,
        pri: 'P2',
        due: '—',
        why: item.snippet || '—',
        link_label: 'Slack',
        link_url: item.permalink || ''
      };
      _post('/slack/convert', quickPayload).then(function(r) {
        if (r.ok) row.remove();
      });
      return;
    }
    _openSlackConvertModal(item);
    return;
  }
  // Thread-dismiss button → POST /slack/dismiss with scope=thread.
  // Must be checked BEFORE the per-message dismiss handler — the
  // .slack-dismiss-thread class doesn't include .slack-dismiss, but
  // ordering keeps the precedence intent obvious.
  var threadBtn = e.target.closest('.slack-dismiss-thread');
  if (threadBtn) {
    e.preventDefault();
    e.stopPropagation();
    var trow = threadBtn.closest('.slack-row');
    if (!trow) return;
    var item = _slackItems()[trow.dataset.id];
    if (!item) return;
    // Build the thread root id: thread_ts if this is a reply, else
    // the item's own message_ts (top-level msg = its own thread root).
    var threadRoot = item.thread_ts || item.message_ts || '';
    var threadId = (item.channel_id || '') + ':' + threadRoot;
    _post('/slack/dismiss', {id: threadId, scope: 'thread'}).then(function(r) {
      if (r.ok) trow.remove();
    });
    return;
  }
  // Per-message dismiss button → POST /slack/dismiss
  var dismissBtn = e.target.closest('.slack-dismiss');
  if (dismissBtn) {
    e.preventDefault();
    e.stopPropagation();
    var drow = dismissBtn.closest('.slack-row');
    if (!drow) return;
    var id = drow.dataset.id;
    if (id) _post('/slack/dismiss', {id: id}).then(function(r) {
      if (r.ok) drow.remove();
    });
    return;
  }
  // Section header chevron in slack view → toggle the whole section's
  // collapsed state. Runs BEFORE the generic expand-all handler because
  // slack sections don't contain task rows for it to expand.
  var slackChevron = e.target.closest('.slack-section [data-action="expand-all"]');
  if (slackChevron) {
    e.preventDefault();
    e.stopPropagation();
    var sec = slackChevron.closest('.slack-section');
    if (sec) {
      sec.classList.toggle('collapsed');
      slackChevron.textContent = sec.classList.contains('collapsed') ? '▾' : '▴';
    }
    return;
  }
});

// Custom hover tooltip — fires immediately, no native delay.
// Any element with [data-tip="..."] gets a styled bubble near the cursor.
(function() {
  var tip = document.getElementById('tooltip');
  if (!tip) return;
  document.addEventListener('mousemove', function(e) {
    var el = e.target.closest('[data-tip]');
    if (el) {
      var text = el.getAttribute('data-tip');
      if (tip.textContent !== text) tip.textContent = text;
      // Offset just above-right of cursor, keep within viewport
      var x = e.clientX + 12;
      var y = e.clientY - 28;
      tip.style.left = x + 'px';
      tip.style.top = y + 'px';
      tip.classList.add('visible');
    } else if (tip.classList.contains('visible')) {
      tip.classList.remove('visible');
    }
  });
})();
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def h(text):
    return (str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def _section_header(label, color, *, expandable=False, subtitle="", collapsed=False):
    """Standard section header bar with the section's accent colour.
    If `expandable=True`, append a chevron toggle that expands/collapses
    every detail panel in the following section. If `subtitle` is set,
    render it next to the label (used for Today's Focus progress)."""
    chevron = "▾" if collapsed else "▴"
    btn = (
        f'<button class="expand-all-btn" data-action="expand-all" '
        f'title="Expand / collapse all">{chevron}</button>'
        if expandable else ""
    )
    sub = f'<span class="section-subtitle">· {h(subtitle)}</span>' if subtitle else ""
    return (
        f'<div class="section-header" style="border-left-color:{color}">'
        f'<span class="section-label">{h(label)}{sub}</span>{btn}</div>\n'
    )


def _focus_progress(data):
    """`N of M` for Today's Focus: M = currently focused + ever-focused-and-done
    today, N = the done part."""
    focus = next((s for s in data.get("sections", []) if s.get("title") == SEC_FOCUS), None)
    if focus is None:
        return ""
    done = sum(
        1 for t in data.get("completed_today", [])
        if t.get("from_section") == SEC_FOCUS
    )
    total = len(focus.get("tasks", [])) + done
    if total == 0:
        return ""
    return f"{done} of {total}"

def format_age(from_week, current_week, added=None):
    """Show task age. Uses 'added' date (days) if available, falls back to week diff."""
    if added:
        try:
            added_date = datetime.date.fromisoformat(added)
            days = (datetime.date.today() - added_date).days
            if days <= 0:
                return '<span style="color:#484f58">today</span>'
            label = f"{days}d"
            if days <= 3:
                color = "#8b949e"
            elif days <= 7:
                color = "#b8a44a"
            elif days <= 14:
                color = "#e3b341"
            else:
                color = "#f0883e"
            return f'<span style="color:{color}">{label}</span>'
        except Exception:
            pass
    if not from_week or from_week == "—":
        return '<span style="color:#484f58">—</span>'
    try:
        weeks = int(current_week.lstrip("W")) - int(from_week.lstrip("W"))
        if weeks <= 0:
            return '<span style="color:#484f58">new</span>'
        elif weeks == 1:
            return f'<span style="color:#8b949e">{weeks}w</span>'
        elif weeks == 2:
            return f'<span style="color:#e3b341">{weeks}w</span>'
        else:
            return f'<span style="color:#f0883e">{weeks}w</span>'
    except Exception:
        return h(from_week)

def render_links(links):
    if not links:
        return "—"
    return " · ".join(f'<a href="{h(l["url"])}">{h(l["label"])}</a>' for l in links)

def render_status(status, task_id=None):
    label, cls = STATUS_MAP.get(status, (h(status), "b-open"))
    cycable = status in STATUS_CYCLE
    extra = f' data-id="{task_id}" data-status="{status}"' if cycable and task_id is not None else ""
    status_cls = " status-badge" if cycable else ""
    return f'<span class="badge {cls}{status_cls}"{extra}>{label}</span>'

def render_pri(pri, task_id=None):
    extra = f' data-id="{task_id}" data-pri="{pri or ""}"' if task_id is not None else ""
    if not pri:
        return f'<span class="badge priority-badge"{extra}>—</span>' if task_id is not None else "—"
    cls = PRI_CSS.get(pri, "")
    label = PRI_LABEL.get(pri, pri)
    return f'<span class="badge {cls} priority-badge"{extra}>{label}</span>'

def is_due_soon(due_str):
    """True if due_str is HH:MM and within the next 2 hours."""
    if not due_str or not re.fullmatch(r"\d{1,2}:\d{2}", due_str):
        return False
    try:
        hh, mm = map(int, due_str.split(":"))
        now = datetime.datetime.now()
        due = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        delta = (due - now).total_seconds()
        return 0 <= delta <= 7200
    except Exception:
        return False

def row_classes(task):
    cls = []
    status = task.get("status", "")
    due = task.get("due") or ""
    if status == "in_progress":
        cls.append("row-progress")
    if status == "blocked":
        cls.append("row-blocked")
    if "⚠️" in due:
        cls.append("row-overdue")
    elif is_due_soon(due):
        cls.append("row-due-soon")
    return f' class="{" ".join(cls)}"' if cls else ""

def section_color(title):
    key = title.lower()
    for k, c in SECTION_COLORS.items():
        if k in key:
            return c
    return "#388bfd"

# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_core_section(title, tasks, week, subtitle=""):
    if not tasks:
        return ""
    color = section_color(title)
    label = title

    rows = []
    any_why = False
    for t in tasks:
        rc = row_classes(t)
        task_id = t.get("id", t.get("num", ""))
        due = t.get("due") or "—"
        due_html = f'<span class="due">{h(due)}</span>' if is_due_soon(due) else h(due)
        drag_attrs = f' draggable="true" data-id="{task_id}"'
        filter_attrs = _filter_data_attrs(t)
        why = (t.get("why") or "").strip()
        has_why = bool(why) and why != "—"
        if has_why:
            any_why = True
        # Only mark the task cell clickable when there's a detail to reveal,
        # so empty-why rows don't have a dead expand affordance.
        task_cls = "task-cell" if has_why else ""
        cells = [
            f'<td class="num" data-id="{task_id}">{task_id}</td>',
            f'<td>{render_pri(t.get("pri"), task_id)}</td>',
            f'<td class="{task_cls}"><span class="task-name">{h(t.get("task",""))}</span>'
            f'<span class="rename-pencil" title="Rename" data-action="rename">✎</span></td>',
            f'<td>{due_html}</td>',
            f'<td>{format_age(t.get("from"), week, t.get("added"))}</td>',
            f'<td>{render_links(t.get("links",[]))}</td>',
            f'<td>{render_status(t.get("status","open"), task_id)}</td>',
        ]
        rows.append(f'<tr{rc}{drag_attrs}{filter_attrs}>{"".join(cells)}</tr>')
        # Sibling detail row revealed on `.expanded`. Mirrors the compact-row
        # detail panel — but the table already shows pri/age/link/status, so
        # the panel only carries the field that isn't visible: Why.
        if has_why:
            rows.append(
                f'<tr class="row-detail" data-id="{task_id}">'
                f'<td colspan="7">'
                f'<span class="field"><span class="field-label">Why</span>'
                f'<span class="why-text">{h(why)}</span></span>'
                f'</td>'
                f'</tr>'
            )
    headers = [
        '<th style="width:32px">#</th>',
        '<th style="width:48px">Pri</th>',
        '<th>Task</th>',
        '<th style="width:110px">Due</th>',
        '<th style="width:48px">Age</th>',
        '<th style="width:90px">Link</th>',
        '<th style="width:120px">Status</th>',
    ]
    # If no progress subtitle was passed (i.e. not Today's Focus), show count
    if not subtitle:
        subtitle = f"{len(tasks)}"
    return (
        _section_header(label, color, expandable=any_why, subtitle=subtitle, collapsed=True)
        + f'<table><thead><tr>{"".join(headers)}</tr></thead><tbody>\n'
        + "\n".join(rows)
        + "\n</tbody></table>\n"
    )

def render_compact_section(title, tasks, week):
    """Compact row rendering for monitoring / lower priority — task name + due, no full table."""
    if not tasks:
        return ""
    color = section_color(title)
    label = title
    rows = []
    for t in tasks:
        rc = row_classes(t).strip()
        rc_class = rc[len('class="'):-1] if rc.startswith('class="') else ""
        task_id = t.get("id", t.get("num", ""))
        due = t.get("due") or ""
        due_html = f'<span class="cmp-due">{h(due)}</span>' if due and due != "—" else ""
        pri = t.get("pri") or ""
        pri_emoji = PRI_EMOJI.get(pri, "")
        rows.append(
            f'<div class="cmp-row {rc_class}" draggable="true" data-id="{task_id}"{_filter_data_attrs(t)}>'
            f'<span class="cmp-id" data-id="{task_id}">{task_id}</span>'
            f'<span class="cmp-pri">{pri_emoji}</span>'
            f'<span class="cmp-task"><span class="task-name">{h(t.get("task",""))}</span>'
            f'<span class="rename-pencil" title="Rename" data-action="rename">✎</span></span>'
            f'{due_html}'
            f'</div>'
        )
        # Expandable detail panel — hidden until the task name is clicked
        detail_parts = [
            f'<span class="field"><span class="field-label">Status</span>{render_status(t.get("status","open"), task_id)}</span>',
            f'<span class="field"><span class="field-label">Pri</span>{render_pri(t.get("pri"), task_id)}</span>',
            f'<span class="field"><span class="field-label">Age</span>{format_age(t.get("from"), week, t.get("added"))}</span>',
        ]
        if t.get("links"):
            detail_parts.append(
                f'<span class="field"><span class="field-label">Links</span>{render_links(t.get("links"))}</span>'
            )
        why = (t.get("why") or "").strip()
        if why and why != "—":
            detail_parts.append(
                f'<span class="field"><span class="field-label">Why</span><span class="why-text">{h(why)}</span></span>'
            )
        rows.append(
            f'<div class="cmp-detail" data-id="{task_id}">{"".join(detail_parts)}</div>'
        )
    return (
        _section_header(label, color, expandable=True, subtitle=f"{len(tasks)}", collapsed=True)
        + f'<div class="cmp-section">{"".join(rows)}</div>\n'
    )


def compute_counts(data):
    pri = {"P1": 0, "P2": 0, "P3": 0, "P4": 0, "P5": 0}
    status = {"in_progress": 0, "waiting": 0, "blocked": 0}
    overdue = 0
    stale = 0
    today = datetime.date.today()
    for s in data.get("sections", []):
        for t in s.get("tasks", []):
            p = t.get("pri")
            if p in pri:
                pri[p] += 1
            st = t.get("status", "")
            if st in status:
                status[st] += 1
            if "⚠️" in (t.get("due") or ""):
                overdue += 1
            added = t.get("added")
            if added:
                try:
                    if (today - datetime.date.fromisoformat(added)).days >= 14:
                        stale += 1
                except ValueError:
                    pass
    return pri, status, overdue, len(data.get("completed_today", [])), stale


def render_counts_strip(data):
    pri, status, overdue, done, stale = compute_counts(data)
    pri_colors = {"P1": "#f85149", "P2": "#f0883e", "P3": "#e3b341", "P4": "#79c0ff", "P5": "#8b949e"}

    groups = []

    # Priority group — colour dots speak for themselves, no label needed.
    # Each `<span class="stat">` carries data-filter-* so a click filters the
    # task list to that priority. Multiple selected pills combine (OR).
    # `data-tip` drives the instant custom tooltip (no native ~500ms delay).
    pri_labels = {"P1": "Critical", "P2": "High", "P3": "Medium", "P4": "Low", "P5": "Paused"}
    pri_stats = "".join(
        f'<span class="stat" data-filter-key="pri" data-filter-val="{p}" '
        f'data-tip="{p} {pri_labels[p]} — {pri[p]} task{"" if pri[p]==1 else "s"} (click to filter)">'
        f'<span class="dot" style="background:{pri_colors[p]}"></span>{pri[p]}</span>'
        for p in ("P1", "P2", "P3", "P4", "P5") if pri[p]
    )
    if pri_stats:
        groups.append(f'<div class="cnt-group">{pri_stats}</div>')

    # Status group
    status_stats = []
    def _status_pill(key, icon, label, n):
        return (
            f'<span class="stat" data-filter-key="status" data-filter-val="{key}" '
            f'data-tip="{label} — {n} task{"" if n==1 else "s"} (click to filter)">'
            f'<span class="icon">{icon}</span>{n}</span>'
        )
    if status["in_progress"]: status_stats.append(_status_pill("in_progress", "🔄", "In progress", status["in_progress"]))
    if status["waiting"]:     status_stats.append(_status_pill("waiting",     "⏳", "Waiting",     status["waiting"]))
    if status["blocked"]:     status_stats.append(_status_pill("blocked",     "🚫", "Blocked",     status["blocked"]))
    if status_stats:
        groups.append(f'<div class="cnt-group">{"".join(status_stats)}</div>')

    # Overdue (only show if non-zero)
    if overdue:
        tip = f"Overdue — {overdue} task{'' if overdue==1 else 's'} past their due date (click to filter)"
        groups.append(f'<div class="cnt-group alert"><span class="stat" data-filter-key="flag" data-filter-val="overdue" data-tip="{tip}"><span class="icon">⚠️</span>{overdue} overdue</span></div>')

    # Stale (only show if any task ≥14 days old — ADHD-friendly drift detector)
    if stale:
        tip = f"Stale — {stale} task{'' if stale==1 else 's'} added ≥14 days ago (click to filter)"
        groups.append(f'<div class="cnt-group alert"><span class="stat" data-filter-key="flag" data-filter-val="stale" data-tip="{tip}"><span class="icon">🧹</span>{stale} stale</span></div>')

    # Done today (always show — small dopamine hit when it's >0)
    tip = f"Done today — {done} task{'' if done==1 else 's'} completed today"
    groups.append(f'<div class="cnt-group success"><span class="stat" data-tip="{tip}"><span class="icon">✅</span>{done} done today</span></div>')

    # Clear-filters button — sits inline with the pills, JS toggles visibility.
    # data-action wired to the global click handler so it survives DOM swaps.
    groups.append('<button id="filter-clear" data-action="clear-filters" title="Clear filters">✕ Clear</button>')

    return f'<div class="counts-strip">{"".join(groups)}</div>\n'


def render_goalie_section(title, tasks):
    if not tasks:
        return ""
    color = section_color(title)
    rows = []
    for t in tasks:
        rc = row_classes(t)
        task_id = t.get("id", t.get("num", ""))
        rows.append(
            f'<tr{rc}>'
            f'<td>{task_id}</td>'
            f'<td>{h(t.get("task",""))}</td>'
            f'<td>{render_links(t.get("links",[]))}</td>'
            f'<td>{render_status(t.get("status","open"), task_id)}</td>'
            f'</tr>'
        )
    return (
        _section_header(title, color, subtitle=f"{len(tasks)}")
        + '<table><thead><tr>'
        '<th style="width:2%">#</th>'
        '<th>Task</th>'
        '<th style="width:9%">Link</th>'
        '<th style="width:7%">Status</th>'
        '</tr></thead><tbody>\n'
        + "\n".join(rows)
        + "\n</tbody></table>\n"
    )

def render_completed(tasks):
    if not tasks:
        return ""
    color = SECTION_COLORS["completed today"]
    rows = []
    for t in tasks:
        task_id = t.get("id", t.get("num", ""))
        why = (t.get("why") or "").strip()
        why_html = f'<span class="why-text">{h(why)}</span>' if why and why != "—" else ""
        rows.append(
            f'<tr>'
            f'<td class="num num-done" data-id="{task_id}">{task_id}</td>'
            f'<td>{h(t.get("task",""))}</td>'
            f'<td>{render_links(t.get("links",[]))}</td>'
            f'<td>{h(t.get("time") or "—")}</td>'
            f'<td>{why_html}</td>'
            f'</tr>'
        )
    return (
        _section_header("Completed today", color, subtitle=f"{len(tasks)}")
        + '<table><thead><tr>'
        '<th style="width:2%">#</th>'
        '<th>Task</th>'
        '<th style="width:9%">Link</th>'
        '<th style="width:5%">Time</th>'
        '<th>Why</th>'
        '</tr></thead><tbody>\n'
        + "\n".join(rows)
        + "\n</tbody></table>\n"
    )

def compute_completions(dates):
    """Returns {ISO date: count} for the given dates by reading whichever core files cover them."""
    weeks_needed = set()
    for d in dates:
        y, w, _ = d.isocalendar()
        weeks_needed.add((y, w))
    counts = {}
    for y, w in weeks_needed:
        path = Path.home() / "todo" / "journal" / f"{y}-W{w:02d}-core.md"
        try:
            text = path.read_text()
        except Exception:
            continue
        idx = text.find("\n## Done")
        if idx < 0:
            continue
        cur_date = None
        for line in text[idx:].splitlines():
            m = re.match(r"^###\s+(\d{4}-\d{2}-\d{2})", line)
            if m:
                cur_date = m.group(1)
                continue
            if cur_date and line.startswith("- [x]"):
                counts[cur_date] = counts.get(cur_date, 0) + 1
    return counts


def ordinal(n):
    """Day-of-month with ordinal suffix: 1 -> 1st, 22 -> 22nd, 11 -> 11th."""
    if 11 <= (n % 100) <= 13:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def last_n_workdays(today, n=5):
    """Walk back from today, collecting weekdays (Mon–Fri) until we have n. Returned in chronological order."""
    days = []
    d = today
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= datetime.timedelta(days=1)
    return list(reversed(days))


def render_workdays_sparkline():
    today = datetime.date.today()
    days = last_n_workdays(today, 10)
    counts = compute_completions(days)
    values = [counts.get(d.isoformat(), 0) for d in days]
    peak = max(values) if any(values) else 1
    total = sum(values)
    weekday_abbr = ["Mon", "Tue", "Wed", "Thu", "Fri"]

    n = len(days)
    chart_w, chart_h = 200.0, 50.0  # SVG viewBox; scales to container width
    slot_w = chart_w / n
    bar_w = slot_w * 0.55
    baseline = chart_h - 1  # leave 1px for stroke at the bottom

    bars, points, hover_zones = [], [], []
    for i, (d, v) in enumerate(zip(days, values)):
        cx = i * slot_w + slot_w / 2
        x = cx - bar_w / 2
        h = (v / peak) * (chart_h - 4) if peak else 0
        y = baseline - h
        is_today = d == today
        if v == 0:
            fill = "#30363d"
        elif is_today:
            fill = "#3fb950"
        else:
            alpha = 0.30 + (v / peak) * 0.45
            fill = f"rgba(63,185,80,{alpha:.2f})"
        bars.append(
            f'<rect x="{x:.2f}" y="{y:.2f}" width="{bar_w:.2f}" '
            f'height="{max(h, 1.5):.2f}" fill="{fill}" rx="1.5" ry="1.5"/>'
        )
        points.append((cx, y))
        # Full-column transparent hover zone gives a generous hit target,
        # especially for empty days where the bar is just 1.5px tall.
        date_str = f"{d.strftime('%a %b')} {ordinal(d.day)}"
        label = f"{date_str} — {v} done" if v else f"{date_str} — nothing yet"
        hover_zones.append(
            f'<rect x="{i * slot_w:.2f}" y="0" width="{slot_w:.2f}" '
            f'height="{chart_h:.0f}" fill="transparent" data-tip="{label}"/>'
        )

    trendline = (
        f'<polyline points="{" ".join(f"{x:.2f},{y:.2f}" for x, y in points)}" fill="none" '
        f'stroke="#58a6ff" stroke-width="1.5" stroke-linecap="round" '
        f'stroke-linejoin="round" opacity="0.85" '
        f'vector-effect="non-scaling-stroke"/>'
    )
    point_dots = "".join(
        f'<circle cx="{x:.2f}" cy="{y:.2f}" r="1.5" '
        f'fill="#58a6ff" opacity="0.9"/>'
        for x, y in points
    )

    svg = (
        f'<svg class="spark-svg" viewBox="0 0 {chart_w:.0f} {chart_h:.0f}" '
        f'preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg">'
        f'{"".join(bars)}{trendline}{point_dots}'
        f'{"".join(hover_zones)}'
        f'</svg>'
    )

    labels = []
    for d in days:
        cls = "spark-label today" if d == today else "spark-label"
        labels.append(
            f'<div class="{cls}">'
            f'<span class="spark-day">{weekday_abbr[d.weekday()]}</span>'
            f'<span class="spark-date">{ordinal(d.day)}</span>'
            f'</div>'
        )

    color = SECTION_COLORS["completed today"]
    return (
        f'<div class="section-header" style="border-left-color:{color}">'
        f'Last 10 workdays '
        f'<span class="spark-total">'
        f'<span class="spark-total-num">{total}</span>'
        f'<span class="spark-total-label">done</span>'
        f'</span>'
        f'</div>\n'
        f'{svg}'
        f'<div class="spark-labels">{"".join(labels)}</div>\n'
    )


def render_compact_completed(tasks, week=""):
    """Compact rendering for dashboard view: matches the Monitoring/Lower Priority style.
    The id cell uncompletes (handled by .cmp-id-done in the global click handler).
    Click the task name to expand the detail panel — completion time + link + source."""
    if not tasks:
        return ""
    color = SECTION_COLORS["completed today"]
    rows = []
    for t in tasks:
        task_id = t.get("id", t.get("num", ""))
        time = t.get("time") or ""
        time_html = f'<span class="cmp-due">{h(time)}</span>' if time and time != "—" else ""
        rows.append(
            f'<div class="cmp-row cmp-row-done" data-id="{task_id}">'
            f'<span class="cmp-id-done" data-id="{task_id}">{task_id}</span>'
            f'<span class="cmp-pri"></span>'
            f'<span class="cmp-task">{h(t.get("task",""))}</span>'
            f'{time_html}'
            f'</div>'
        )
        detail_parts = [
            f'<span class="field"><span class="field-label">Completed</span>{h(time) if time else "—"}</span>',
            f'<span class="field"><span class="field-label">Pri</span>{render_pri(t.get("pri"), task_id)}</span>',
            f'<span class="field"><span class="field-label">Age</span>{format_age(t.get("from"), week, t.get("added"))}</span>',
        ]
        if t.get("links"):
            detail_parts.append(
                f'<span class="field"><span class="field-label">Links</span>{render_links(t.get("links"))}</span>'
            )
        why = (t.get("why") or "").strip()
        if why and why != "—":
            detail_parts.append(
                f'<span class="field"><span class="field-label">Why</span><span class="why-text">{h(why)}</span></span>'
            )
        rows.append(
            f'<div class="cmp-detail" data-id="{task_id}">{"".join(detail_parts)}</div>'
        )
    return (
        _section_header("Completed today", color, expandable=True, subtitle=f"{len(tasks)}", collapsed=True)
        + f'<div class="cmp-section">{"".join(rows)}</div>\n'
    )

VIEWS = ["dashboard", "classic", "slack"]

def _build_dashboard_body(data, week):
    parts = []  # counts strip now lives in the topbar (see _view_switcher_html)

    def card(html, variant=""):
        if not html:
            return ""
        cls = "task-card" + (f" {variant}" if variant else "")
        return f'<div class="{cls}">{html}</div>'

    # Goalie sections (if any) — full width above the grid
    for section in data.get("sections", []):
        if section.get("type") == "goalie":
            parts.append(card(render_goalie_section(section.get("title", ""), section.get("tasks", []))))

    # Two-column grid: left = active (full detail), right = monitoring + lower (compact)
    LEFT  = (SEC_FOCUS, SEC_HIGH)
    RIGHT = (SEC_MON, SEC_LOW)
    CARD_VARIANTS = {SEC_FOCUS: "focus", SEC_HIGH: "high-priority"}
    sections_by_title = {s.get("title"): s for s in data.get("sections", []) if s.get("type") != "goalie"}

    focus_sub = _focus_progress(data)
    left_html = "".join(
        card(
            render_core_section(
                title, sections_by_title[title].get("tasks", []), week,
                subtitle=focus_sub if title == SEC_FOCUS else "",
            ),
            variant=CARD_VARIANTS.get(title, ""),
        )
        for title in LEFT if title in sections_by_title
    )
    right_html = "".join(
        card(render_compact_section(title, sections_by_title[title].get("tasks", []), week))
        for title in RIGHT if title in sections_by_title
    )
    # Sparkline fills the gap between Lower Priority and the bottom-anchored Completed Today
    right_html += card(render_workdays_sparkline())

    # Completed today is anchored to the bottom of the right column
    completed_html = render_compact_completed(data.get("completed_today", []), week)
    if completed_html:
        right_html += f'<div class="completed-anchor">{card(completed_html)}</div>'

    parts.append(
        f'<div class="dashboard-grid">'
        f'<div class="col-left">{left_html}</div>'
        f'<div class="col-right">{right_html}</div>'
        f'</div>'
    )
    if data.get("updated"):
        parts.append(f'<p class="counts" style="margin-top:16px;color:#484f58">Updated {h(data["updated"])}</p>\n')
    return "".join(parts)


def _build_classic_body(data, week):
    """Single-column view: each section wrapped in a task-card, stacked.
    (Counts strip now lives in the topbar — see `_view_switcher_html`.)"""
    parts = []

    def card(html, variant=""):
        if not html:
            return ""
        cls = "task-card" + (f" {variant}" if variant else "")
        return f'<div class="{cls}">{html}</div>'

    CARD_VARIANTS = {SEC_FOCUS: "focus", SEC_HIGH: "high-priority"}
    focus_sub = _focus_progress(data)
    for section in data.get("sections", []):
        stype = section.get("type", "core")
        title = section.get("title", "")
        tasks = section.get("tasks", [])
        if stype == "goalie":
            parts.append(card(render_goalie_section(title, tasks)))
        else:
            sub = focus_sub if title == SEC_FOCUS else ""
            parts.append(card(
                render_core_section(title, tasks, week, subtitle=sub),
                variant=CARD_VARIANTS.get(title, ""),
            ))
    completed_html = render_completed(data.get("completed_today", []))
    if completed_html:
        parts.append(card(completed_html))
    if data.get("updated"):
        parts.append(f'<p class="counts" style="margin-top:16px;color:#484f58">Updated {h(data["updated"])}</p>\n')
    return "".join(parts)


def _slack_relative_time(iso_str, now=None):
    """Render a Slack item timestamp as a short relative form ("2h ago",
    "3d ago"). Falls back to a literal date for things older than a week."""
    if not iso_str:
        return ""
    try:
        ts = datetime.datetime.fromisoformat(iso_str)
    except (TypeError, ValueError):
        return h(iso_str)
    now = now or (datetime.datetime.now(ts.tzinfo) if ts.tzinfo else datetime.datetime.now())
    delta = now - ts
    secs = int(delta.total_seconds())
    if secs < 60: return "just now"
    if secs < 3600: return f"{secs // 60}m ago"
    if secs < 86400: return f"{secs // 3600}h ago"
    if secs < 7 * 86400: return f"{secs // 86400}d ago"
    return ts.strftime("%Y-%m-%d")


def _slack_active_permalinks(data):
    """Set of permalinks present in any non-done, non-cancelled task's links.
    Used as a legacy fallback for items converted before slack-converted.json
    existed (or for tasks where the user manually added the Slack link)."""
    out = set()
    inactive = {"done", "cancelled"}
    for section in data.get("sections", []):
        for task in section.get("tasks", []):
            if task.get("status") in inactive:
                continue
            for link in task.get("links", []) or []:
                url = (link or {}).get("url")
                if url:
                    out.add(url)
    return out


SLACK_TIER_LABEL = {
    "reply_needed":     "Reply Needed",
    "review":           "Review",
    "already_handled":  "Already Handled",
}


def _render_slack_section(label, items, *, color, collapsed=False):
    rows = []
    for it in items:
        sender = it.get("sender") or "?"
        is_dm = bool(it.get("is_dm"))
        target = (
            f'@{sender}' if is_dm
            else f'#{it.get("channel_name") or ""}'
        )
        ts_iso = it.get("ts") or ""
        ts_rel = _slack_relative_time(ts_iso)
        permalink = it.get("permalink") or "#"
        snippet = it.get("snippet") or ""
        # Composite id matches the JS lookup key
        item_id = f"{it.get('channel_id','')}:{it.get('message_ts','')}"
        rows.append(
            f'<div class="slack-row" data-id="{h(item_id)}">'
            f'<div class="slack-meta">'
            f'<span class="slack-sender">{h(sender)}</span>'
            f'<span class="slack-target">{h(target)}</span>'
            f'<a class="slack-ts" href="{h(permalink)}" '
            f'target="_blank" rel="noopener noreferrer" '
            f'title="{h(ts_iso)}">{h(ts_rel)} ↗</a>'
            f'</div>'
            f'<div class="slack-snippet">{h(snippet)}</div>'
            f'<div class="slack-actions">'
            f'<button class="slack-convert" type="button" '
            f'title="Convert to task (⌘+click for quick-add, no modal)">'
            f'<span class="btn-icon">+</span> Convert to task</button>'
            f'<button class="slack-dismiss" type="button" '
            f'title="Dismiss this message (re-surfaces if a new reply lands)">'
            f'<span class="btn-icon">×</span> Dismiss</button>'
            f'<button class="slack-dismiss-thread" type="button" '
            f'title="Dismiss the whole thread (TTL: '
            f'{SLACK_DISMISS_TTL_DAYS} days, then re-surfaces if still active)">'
            f'<span class="btn-icon">⊘</span> Thread</button>'
            f'</div>'
            f'</div>'
        )
    body = "".join(rows) or '<div class="slack-empty">Nothing here.</div>'
    cls = "slack-section" + (" collapsed" if collapsed else "")
    return (
        f'<div class="task-card {cls}" data-tier="{h(label)}">'
        f'{_section_header(label, color, expandable=True, subtitle=str(len(items)), collapsed=collapsed)}'
        f'<div class="slack-section-body">{body}</div>'
        f'</div>'
    )


def _build_slack_body(data, week):
    parts = []

    def card(html, variant=""):
        if not html:
            return ""
        cls = "task-card" + (f" {variant}" if variant else "")
        return f'<div class="{cls}">{html}</div>'

    snapshot = load_slack_snapshot()
    if snapshot is None:
        return (
            '<div class="task-card slack-empty-state">'
            '<h3>No Slack snapshot yet</h3>'
            '<p>Run <code>/slack</code> in Claude Code to populate this view. '
            'The skill will write '
            '<code>~/todo/slack-triage.json</code> and this page will refresh.'
            '</p></div>'
        )
    if snapshot.get("_error") == "malformed":
        return (
            '<div class="task-card slack-empty-state">'
            '<h3>Snapshot is unreadable</h3>'
            '<p><code>~/todo/slack-triage.json</code> exists but failed to parse. '
            'Re-run <code>/slack</code> to regenerate.</p></div>'
        )
    if snapshot.get("_error") == "version":
        got = snapshot.get("got")
        return (
            '<div class="task-card slack-empty-state">'
            '<h3>Snapshot version not supported</h3>'
            f'<p>Expected version <code>{SLACK_SNAPSHOT_VERSION}</code>, got '
            f'<code>{h(got)}</code>. Update either the dashboard or the '
            f'slack-triage skill.</p></div>'
        )

    items = snapshot.get("items", []) or []
    dismissed_msgs, dismissed_threads = load_slack_dismissed()
    converted = load_slack_converted()
    active_perms = _slack_active_permalinks(data)

    def is_visible(it):
        cid = it.get("channel_id", "")
        ts  = it.get("message_ts", "")
        msg_key = f"{cid}:{ts}"
        if msg_key in dismissed_msgs: return False
        if msg_key in converted: return False
        # A top-level message acts as its own thread root; dismissing the
        # parent as "thread" hides the whole conversation including the
        # parent itself.
        thread_root = it.get("thread_ts") or ts
        thread_key = f"{cid}:{thread_root}"
        if thread_key in dismissed_threads: return False
        perm = it.get("permalink") or ""
        if perm and perm in active_perms: return False
        return True

    visible = [it for it in items if is_visible(it)]

    # Header: last refreshed + stale badge
    gen_at = snapshot.get("generated_at", "")
    rel = _slack_relative_time(gen_at) if gen_at else "unknown"
    is_stale = False
    try:
        gen_dt = datetime.datetime.fromisoformat(gen_at) if gen_at else None
    except (TypeError, ValueError):
        gen_dt = None
    if gen_dt is not None:
        now = datetime.datetime.now(gen_dt.tzinfo) if gen_dt.tzinfo else datetime.datetime.now()
        if (now - gen_dt).total_seconds() > 24 * 3600:
            is_stale = True
    stale_badge = ' <span class="slack-stale">stale</span>' if is_stale else ''
    header = (
        f'<div class="slack-header">'
        f'Last refreshed <span class="slack-refresh-ts">{h(gen_at) or "—"}</span>'
        f' (<span class="slack-refresh-rel">{h(rel)}</span>){stale_badge}'
        f'</div>'
    )
    parts.append(header)

    # Three sections
    by_tier = {"reply_needed": [], "review": [], "already_handled": []}
    for it in visible:
        tier = it.get("tier") or "review"
        by_tier.setdefault(tier, []).append(it)

    parts.append(_render_slack_section(
        SLACK_TIER_LABEL["reply_needed"], by_tier["reply_needed"],
        color="#f0883e", collapsed=False,
    ))
    parts.append(_render_slack_section(
        SLACK_TIER_LABEL["review"], by_tier["review"],
        color="#388bfd", collapsed=False,
    ))
    parts.append(_render_slack_section(
        SLACK_TIER_LABEL["already_handled"], by_tier["already_handled"],
        color="#3fb950", collapsed=True,
    ))

    # Noise summary line
    noise = snapshot.get("noise") or {}
    if noise:
        bits = ", ".join(f"{h(v)} in {h(k)}" if not k.startswith("bot") else f"{h(v)} bot pings"
                         for k, v in noise.items())
        parts.append(f'<p class="slack-noise">Noise: {bits}</p>')

    # Item-data lookup table for the convert modal pre-fill
    items_json = json.dumps(
        {f"{it.get('channel_id','')}:{it.get('message_ts','')}": it for it in visible},
        ensure_ascii=False,
    )
    # Embed via a JSON-typed script tag — avoids HTML-escape pitfalls in inline JS.
    # The closing-tag guard protects against any literal "</script>" in snippets.
    items_json_safe = items_json.replace("</script", "<\\/script")
    parts.append(
        f'<script type="application/json" id="slack-items-data">'
        f'{items_json_safe}</script>'
    )

    return "".join(parts)


def _view_switcher_html(current, week="", pills_html=""):
    items = "".join(
        f'<a href="?view={v}" class="vs-btn{" active" if v == current else ""}">{v.title()}</a>'
        for v in VIEWS
    )
    select_options = "".join(
        f'<option value="{v}"{" selected" if v == current else ""}>{v.title()}</option>'
        for v in VIEWS
    )
    week_num = week.lstrip("Ww") if week else ""
    week_title = (
        f'<span class="week-title">Week <span class="wk-num">{h(week_num)}</span></span>'
        if week_num else ""
    )
    return (
        f'<div id="topbar">'
        f'{week_title}'
        f'<div id="view-switcher">{items}</div>'
        f'<select id="view-switcher-select" aria-label="View">{select_options}</select>'
        f'<div id="topbar-pills">{pills_html}</div>'
        f'<div id="topbar-actions">'
        f'<button id="add-btn" title="Add task" aria-label="Add task">'
        f'<span class="btn-icon">+</span><span class="btn-label">Add</span></button>'
        f'<button id="sort-btn" title="Sort by priority" aria-label="Sort by priority">'
        f'<span class="btn-icon">⇕</span><span class="btn-label">Sort</span></button>'
        f'</div>'
        f'</div>'
        f'<div id="filter-popup" role="dialog" aria-label="Filter tasks">'
        f'<span class="hint">/</span>'
        f'<input id="task-filter" type="text" placeholder="Filter tasks…" '
        f'autocomplete="off" spellcheck="false">'
        f'<span class="esc-hint">Esc</span>'
        f'</div>'
    )


def build_page(data, view="dashboard"):
    if view not in VIEWS:
        view = "dashboard"
    week = data.get("week", "")
    if view == "slack":
        body = _build_slack_body(data, week)
    elif view == "classic":
        body = _build_classic_body(data, week)
    else:
        body = _build_dashboard_body(data, week)
    switcher = _view_switcher_html(view, week, pills_html=render_counts_strip(data))
    return (
        f'<!DOCTYPE html><html><head>'
        f'<meta charset="utf-8">'
        f'<meta name="tasks-view" content="{view}">'
        f'<title>Tasks</title><style>{CSS}</style>'
        f'</head><body>{switcher}<div id="tasks-content">{body}</div>'
        f'<div id="modal-overlay"><div id="modal" data-mode="add">'
        f'<h3 id="modal-title">Add Task</h3>'
        f'<label>Task name</label>'
        f'<input id="m-task" type="text" placeholder="Task description">'
        f'<div class="modal-row">'
        f'<div><label>Priority</label>'
        f'<select id="m-pri">'
        f'<option value="P1">P1 \U0001F534</option>'
        f'<option value="P2" selected>P2 \U0001F7E0</option>'
        f'<option value="P3">P3 \U0001F7E1</option>'
        f'<option value="P4">P4 \U0001F535</option>'
        f'<option value="P5">P5 \u23F8\uFE0F</option>'
        f'</select></div>'
        f'<div><label>Due (HH:MM or YYYY-MM-DD)</label>'
        f'<input id="m-due" type="text" placeholder="\u2014"></div>'
        f'</div>'
        f'<label>Why</label>'
        f'<input id="m-why" type="text" placeholder="reason (optional)">'
        f'<div class="modal-row">'
        f'<div><label>Link label</label>'
        f'<input id="m-link-label" type="text" placeholder="HOTS-123"></div>'
        f'<div><label>Link URL</label>'
        f'<input id="m-link-url" type="url" placeholder="https://..."></div>'
        f'</div>'
        f'<div class="modal-sep"></div>'
        f'<div class="modal-completed-section">'
        f'<input type="checkbox" id="m-completed">'
        f'<label for="m-completed">Already completed</label>'
        f'<div class="modal-completed-time-wrap" id="m-completed-wrap">'
        f'<span class="modal-completed-at">at</span>'
        f'<div class="modal-completed-time-col">'
        f'<input type="text" id="m-completed-time" inputmode="numeric"'
        f' placeholder="HH:MM" maxlength="5">'
        f'<div class="modal-completed-hint" id="m-completed-hint"></div>'
        f'</div></div></div>'
        f'<div class="modal-footer">'
        f'<button id="modal-cancel">Cancel</button>'
        f'<button id="modal-save">Add task</button>'
        f'</div></div></div>'
        f'<div id="help-overlay"><div id="help">'
        f'<h3>Keyboard shortcuts</h3>'
        f'<div class="help-columns"><div><table>'
        f'<tr class="help-section"><th colspan="2">Navigation</th></tr>'
        f'<tr><td>Highlight next / previous</td><td><kbd>j</kbd> <kbd>k</kbd></td></tr>'
        f'<tr><td>Expand / collapse row</td><td><kbd>Enter</kbd></td></tr>'
        f'<tr><td>Jump to Focus / High / Lower</td>'
        f'<td><kbd>Shift</kbd>+<kbd>1</kbd>/<kbd>2</kbd>/<kbd>3</kbd></td></tr>'
        f'<tr class="help-section"><th colspan="2">Mutate highlighted row</th></tr>'
        f'<tr><td>Edit task (modal)</td><td><kbd>e</kbd></td></tr>'
        f'<tr><td>Cycle status</td><td><kbd>Shift</kbd>+<kbd>S</kbd></td></tr>'
        f'<tr><td>Cycle priority</td><td><kbd>Shift</kbd>+<kbd>P</kbd></td></tr>'
        f'<tr class="help-section"><th colspan="2">Add &amp; sort</th></tr>'
        f'<tr><td>Add task</td><td><kbd>a</kbd></td></tr>'
        f'<tr><td>Add completed task</td><td><kbd>Shift</kbd>+<kbd>A</kbd></td></tr>'
        f'<tr><td>Submit modal</td><td><kbd>⌘</kbd>+<kbd>Enter</kbd></td></tr>'
        f'<tr><td>Sort by priority</td><td><kbd>s</kbd></td></tr>'
        f'</table></div><div><table>'
        f'<tr class="help-section"><th colspan="2">Filter</th></tr>'
        f'<tr><td>Open filter popup</td><td><kbd>/</kbd></td></tr>'
        f'<tr><td>Filter by pill (multi-select)</td><td>click pill</td></tr>'
        f'<tr class="help-section"><th colspan="2">View &amp; UI</th></tr>'
        f'<tr><td>Expand / collapse all</td><td><kbd>x</kbd></td></tr>'
        f'<tr><td>Refresh</td><td><kbd>r</kbd></td></tr>'
        f'<tr><td>Toggle dashboard / classic</td><td><kbd>c</kbd></td></tr>'
        f'<tr><td>Close / clear / collapse</td><td><kbd>Esc</kbd></td></tr>'
        f'<tr><td>Show this help</td><td><kbd>?</kbd></td></tr>'
        f'</table></div></div>'
        f'<button id="help-close">Close</button>'
        f'</div></div>'
        f'<div id="tooltip"></div>'
        f'<div id="toast">'
        f'<span class="toast-msg"></span>'
        f'<span class="toast-progress"></span>'
        f'<button class="toast-undo">Undo</button>'
        f'</div>'
        f'<div id="ctx-menu">'
        f'<div class="ctx-header">Move to</div>'
        f'<div class="ctx-item" data-section="Today\u0027s Focus">Today\u0027s Focus</div>'
        f'<div class="ctx-item" data-section="{SEC_MON}">{SEC_MON}</div>'
        f'<div class="ctx-item" data-section="{SEC_HIGH}">{SEC_HIGH}</div>'
        f'<div class="ctx-item" data-section="{SEC_LOW}">{SEC_LOW}</div>'
        f'<div class="ctx-divider"></div>'
        f'<div class="ctx-item" data-action="edit">✏️ Edit task</div>'
        f'<div class="ctx-item" data-action="complete">✅ Mark as done</div>'
        f'<div class="ctx-item danger" data-action="cancel">Cancel task</div>'
        f'</div>'
        f'<script>{_js_consts()}{SCRIPT}</script>'
        f'</body></html>'
    )

# ---------------------------------------------------------------------------
# Core file update
# ---------------------------------------------------------------------------

def current_core_path(week=None):
    """Path to the core markdown file. `week` is a string like 'W17' from the
    JSON; if absent or unparseable, falls back to today's ISO week. This lets
    clicks on a stale tab target the JSON's week, not today's."""
    today = datetime.date.today()
    if week:
        try:
            w = int(str(week).lstrip("Ww"))
            return Path.home() / "todo" / "journal" / f"{today.year}-W{w:02d}-core.md"
        except (ValueError, AttributeError):
            pass
    y, w, _ = today.isocalendar()
    return Path.home() / "todo" / "journal" / f"{y}-W{w:02d}-core.md"

def current_journal_path():
    today = datetime.date.today()
    y, w, _ = today.isocalendar()
    return Path.home() / "todo" / "journal" / f"{y}-W{w:02d}.md"

def set_today_focus(task_names):
    """Replace today's #### Core Focus list in the journal with the given names (numbered)."""
    journal_path = current_journal_path()
    if not journal_path.exists():
        return
    lines = journal_path.read_text().split("\n")
    today = datetime.date.today()
    section_header = f"## {today.strftime('%A')} {today.isoformat()}"

    try:
        section_start = next(i for i, l in enumerate(lines) if l.strip() == section_header)
    except StopIteration:
        return

    section_end = next(
        (i for i, l in enumerate(lines[section_start + 1:], section_start + 1) if l.startswith("## ")),
        len(lines),
    )

    cf_start = next(
        (i for i, l in enumerate(lines[section_start:section_end], section_start) if l.strip() == "#### Core Focus"),
        None,
    )

    new_block = ["#### Core Focus"] + [f"{i}. {n}" for i, n in enumerate(task_names, 1)] + [""]

    if cf_start is None:
        # Create the section before ### Done if it exists, else at end of today's section
        done_idx = next(
            (i for i, l in enumerate(lines[section_start:section_end], section_start) if l.strip() == "### Done"),
            section_end,
        )
        new_lines = lines[:done_idx] + new_block + lines[done_idx:]
    else:
        cf_end = next(
            (i for i, l in enumerate(lines[cf_start + 1:section_end], cf_start + 1)
             if l.strip().startswith("####") or l.strip().startswith("###")),
            section_end,
        )
        new_lines = lines[:cf_start] + new_block + lines[cf_end:]

    _atomic_write_text(journal_path, "\n".join(new_lines))

def update_core_file(task_name, new_status, now, week=None):
    """Update the task's state marker in the core markdown file."""
    core_path = current_core_path(week)
    try:
        lines = core_path.read_text().split("\n")
    except FileNotFoundError:
        return
    today_str = now.strftime("%Y-%m-%d")
    ts = now.strftime("%Y-%m-%d %H:%M")

    # Find ## Done boundary
    done_section = _done_boundary(lines, len(lines))

    task_idx = find_task_line(lines, task_name, end_idx=done_section)
    if task_idx is None:
        return

    original = lines[task_idx]

    if new_status == "done":
        updated = re.sub(r"\[.\]", "[x]", original, count=1).rstrip()
        if "_(completed:" not in updated:
            updated += f" _(completed: {ts})_"
        lines.pop(task_idx)
        _insert_under_dated_section(lines, "Done", updated, today_str)
    else:
        marker = STATE_MARKER.get(new_status, "[ ]")
        lines[task_idx] = re.sub(r"\[.\]", marker, original, count=1)

    _atomic_write_text(core_path, "\n".join(lines))

def update_core_file_priority(task_name, new_pri, now, week=None):
    """Swap the priority emoji on the task line in the core markdown file."""
    core_path = current_core_path(week)
    try:
        lines = core_path.read_text().split("\n")
    except FileNotFoundError:
        return
    done_section = _done_boundary(lines, len(lines))
    all_emojis = set(PRI_EMOJI.values())
    new_emoji = PRI_EMOJI.get(new_pri)
    i = find_task_line(lines, task_name, end_idx=done_section)
    if i is not None:
        line = lines[i]
        has_emoji = any(e in line for e in all_emojis)
        if new_emoji:
            if has_emoji:
                for e in all_emojis:
                    if e in line:
                        lines[i] = line.replace(e, new_emoji, 1)
                        break
            else:
                lines[i] = re.sub(r"(- \[.\] )", rf"\1{new_emoji} ", line, count=1)
        else:
            for e in all_emojis:
                if e in line:
                    lines[i] = line.replace(e + " ", "", 1)
                    break
    _atomic_write_text(core_path, "\n".join(lines))


def apply_priority_update(task_id):
    """Cycle the priority of the task with the given stable id."""
    data = _load_state()
    if data is None:
        return False
    now = datetime.datetime.now()
    task, _ = find_task_by_id(data, task_id)
    if not task:
        return False
    new_pri = PRI_CYCLE.get(task.get("pri"))
    task["pri"] = new_pri
    _save_state(data, now)
    update_core_file_priority(task.get("task", ""), new_pri, now, week=data.get("week"))
    return task


def renumber_tasks(data):
    """Renumber all tasks sequentially by display order. Never touches 'id'."""
    n = 1
    for section in data.get("sections", []):
        for task in section.get("tasks", []):
            task["num"] = n
            n += 1
    for task in data.get("completed_today", []):
        task["num"] = n
        n += 1

def next_task_id(data):
    """Return the next available stable task ID."""
    ids = (
        t.get("id", 0)
        for collection in (
            (t for s in data.get("sections", []) for t in s.get("tasks", [])),
            data.get("completed_today", []),
        )
        for t in collection
    )
    return max(ids, default=0) + 1

def find_task_by_id(data, task_id):
    """Return (task, section) for the given stable id, or (None, None).
    Coerces `task_id` to int — JS callers may pass `row.dataset.id`
    which is always a string. Without this, every match would fail
    silently on `34 != "34"`. Centralising here means every caller
    (apply_edit, apply_status_change, apply_cancel, etc.) is
    protected without each having to remember the cast."""
    try:
        task_id = int(task_id)
    except (TypeError, ValueError):
        return None, None
    for section in data.get("sections", []):
        for task in section.get("tasks", []):
            if task.get("id") == task_id:
                return task, section
    for task in data.get("completed_today", []):
        if task.get("id") == task_id:
            return task, None
    return None, None


def sync_core_file_order(data):
    """Reorder active task lines in the core file to match the current JSON task order."""
    core_path = current_core_path(data.get("week"))
    try:
        lines = core_path.read_text().split("\n")
    except FileNotFoundError:
        return
    done_idx = _done_boundary(lines, len(lines))

    # Ordered task names from JSON
    ordered_names = [
        t.get("task", "").lower()
        for s in data.get("sections", [])
        for t in s.get("tasks", [])
    ]

    task_lines = [l for l in lines[:done_idx] if l.strip().startswith("- [")]

    def find_and_claim(name, remaining):
        for i, l in enumerate(remaining):
            extracted = _extract_task_name(l)
            if extracted and extracted.lower() == name:
                return remaining.pop(i)
        return None

    remaining = list(task_lines)
    reordered = []
    for name in ordered_names:
        line = find_and_claim(name, remaining)
        if line:
            reordered.append(line)
    reordered.extend(remaining)  # append any unmatched lines at end

    task_iter = iter(reordered)
    new_lines = []
    for line in lines[:done_idx]:
        new_lines.append(next(task_iter) if line.strip().startswith("- [") else line)
    new_lines.extend(lines[done_idx:])
    _atomic_write_text(core_path, "\n".join(new_lines))


def apply_sort():
    """Sort by priority; redistribute tasks between High Priority and Lower Priority sections."""
    data = _load_state()
    if data is None:
        return False
    pri_order = {"P1": 1, "P2": 2, "P3": 3, "P4": 4, "P5": 5, None: 6}
    sections = data.get("sections", [])

    high  = next((s for s in sections if s.get("title") == SEC_HIGH),  None)
    lower = next((s for s in sections if s.get("title") == SEC_LOW), None)

    if high and lower:
        pool = high.get("tasks", []) + lower.get("tasks", [])
        high["tasks"]  = sorted([t for t in pool if t.get("pri") in ("P1", "P2")],
                                 key=lambda t: pri_order.get(t.get("pri"), 6))
        lower["tasks"] = sorted([t for t in pool if t.get("pri") not in ("P1", "P2")],
                                 key=lambda t: pri_order.get(t.get("pri"), 6))

    # Sort all other sections internally
    for s in sections:
        if s.get("title") not in (SEC_HIGH, SEC_LOW):
            s["tasks"] = sorted(s.get("tasks", []),
                                 key=lambda t: pri_order.get(t.get("pri"), 6))

    renumber_tasks(data)
    _save_state(data)
    sync_core_file_order(data)
    return {"ok": True}


def apply_uncomplete(num):
    """Move a completed task back to active. Reads priority from core file to pick section."""
    try:
        num = int(num)
    except (TypeError, ValueError):
        return False
    data = _load_state()
    if data is None:
        return False

    now = datetime.datetime.now()

    # Find in completed_today by id
    task = next((t for t in data.get("completed_today", []) if t.get("id") == num), None)
    if not task:
        return False

    task_name = task.get("task", "")

    # Read core file to get priority and reconstruct the active line
    core_path = current_core_path(data.get("week"))
    pri = None
    try:
        lines = core_path.read_text().split("\n")
    except FileNotFoundError:
        lines = None
    if lines is not None:
        done_section = _done_boundary(lines, len(lines))
        emoji_to_pri = {v: k for k, v in PRI_EMOJI.items()}
        # Search the Done area only, then translate back to a global index.
        rel_idx = find_task_line(lines[done_section:], task_name, marker="[x]")
        done_line_idx = None
        if rel_idx is not None:
            done_line_idx = done_section + rel_idx
            for emoji, p in emoji_to_pri.items():
                if emoji in lines[done_line_idx]:
                    pri = p
                    break

        if done_line_idx is not None:
            original = lines[done_line_idx]
            # Restore: swap [x] → [ ], strip _(completed: ...)_
            restored = re.sub(r"\[x\]", "[ ]", original, count=1)
            restored = re.sub(r"\s*_\(completed:[^)]+\)_", "", restored)
            lines.pop(done_line_idx)
            # Remove empty heading if no tasks remain under it
            # (leave cleanup to next tk run)
            # Insert restored line at top of active area (before ## Done)
            done_section = _done_boundary(lines, len(lines))
            lines.insert(done_section, restored)
            _atomic_write_text(core_path, "\n".join(lines))

    # Remove from completed_today (by stable id, not num)
    data["completed_today"] = [t for t in data["completed_today"] if t.get("id") != num]

    # Add back to JSON — pick section by priority. Carry the stable id forward.
    new_task = {
        "id": num,
        "num": num,
        "pri": pri or task.get("pri"),
        "task": task_name,
        "due": task.get("due", "—"),
        "from": task.get("from", "—"),
        "added": task.get("added"),
        "links": task.get("links", []),
        "status": "open",
        "why": task.get("why", "—"),
    }
    target_title = target_section_for_pri(new_task["pri"])
    target = next((s for s in data.get("sections", []) if s.get("title") == target_title), None)
    if target is None:
        target = next((s for s in data.get("sections", []) if s.get("type", "core") == "core"), None)
    if target:
        target["tasks"].append(new_task)

    renumber_tasks(data)
    _save_state(data, now)
    return new_task


def apply_status_change(num, force_status=None):
    """Update the status of task `num`. Cycles if force_status is None, else sets explicitly."""
    data = _load_state()
    if data is None:
        return False

    now = datetime.datetime.now()

    # Find task by stable id
    task, source_section = find_task_by_id(data, num)

    if not task or source_section is None:
        return False

    new_status = force_status or STATUS_CYCLE.get(task.get("status", "open"))
    if not new_status:
        return False

    task_name = task.get("task", "")
    task_id = task.get("id", num)

    if new_status == "done":
        source_section["tasks"] = [t for t in source_section["tasks"] if t.get("id") != task_id]
        completed_entry = {
            "num": num,
            "id": task_id,
            "task": task_name,
            "links": task.get("links", []),
            "time": now.strftime("%H:%M"),
            "from_section": source_section.get("title", ""),
            "pri": task.get("pri"),
            "due": task.get("due", "—"),
            "from": task.get("from", "—"),
            "added": task.get("added"),
            "why": task.get("why", "—"),
        }
        data.setdefault("completed_today", []).append(completed_entry)
        result = completed_entry
    else:
        task["status"] = new_status
        result = task

    _save_state(data, now)
    update_core_file(task_name, new_status, now, week=data.get("week"))
    return result

# In-memory undo buffer for cancelled tasks. Keyed by task id; entries
# expire after UNDO_WINDOW_S so the buffer can't grow unbounded.
_undo_lock = threading.Lock()
_recent_cancels = {}
UNDO_WINDOW_S = 30


def _record_cancel(task_id, snapshot):
    with _undo_lock:
        _recent_cancels[task_id] = snapshot
        # Sweep expired entries
        cutoff = time.time() - UNDO_WINDOW_S
        for tid in [k for k, v in _recent_cancels.items() if v.get("at", 0) < cutoff]:
            _recent_cancels.pop(tid, None)


def apply_cancel(task_id):
    """Cancel a task: remove from active JSON, mark [/] in core file, move to ## Cancelled."""
    data = _load_state()
    if data is None:
        return False

    task, source_section = find_task_by_id(data, task_id)
    if not task or source_section is None:
        return False

    now = datetime.datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    ts = now.strftime("%Y-%m-%d %H:%M")
    task_name = task.get("task", "")
    src_title = source_section.get("title")

    # Snapshot for /uncancel before mutating anything
    _record_cancel(task_id, {
        "at": time.time(),
        "task": dict(task),
        "src_title": src_title,
        "ts": ts,
    })

    # Remove from JSON
    source_section["tasks"] = [t for t in source_section["tasks"] if t.get("id") != task_id]

    # Update core file: change marker to [/], append _(cancelled: ts)_, move to ## Cancelled
    core_path = current_core_path(data.get("week"))
    try:
        lines = core_path.read_text().split("\n")
    except FileNotFoundError:
        lines = None
    if lines is not None:
        done_section = _done_boundary(lines, len(lines))
        task_idx = find_task_line(lines, task_name, end_idx=done_section)
        if task_idx is not None:
            original = lines[task_idx]
            updated = re.sub(r"\[.\]", "[/]", original, count=1).rstrip()
            if "_(cancelled:" not in updated:
                updated += f" _(cancelled: {ts})_"
            lines.pop(task_idx)
            _insert_under_dated_section(lines, "Cancelled", updated, today_str, anchor_after="Done")

        _atomic_write_text(core_path, "\n".join(lines))

    _snapshot_focus_if_touched(data, src_title)

    renumber_tasks(data)
    _save_state(data, now)
    return {"ok": True, "id": task_id, "task": task_name}


def update_core_file_name(old_name, new_name, week=None, pri=None):
    """Rewrite the task-name slot of `old_name`'s active line to `new_name`,
    preserving the state marker, priority emoji, and any trailing metadata
    (` — due …`, ` (link)`, ` _(carried…)_`, ` _(why:…)_`).
    If `pri` is set, prefers a line whose emoji matches — disambiguates when
    two tasks share a name across different priorities."""
    core_path = current_core_path(week)
    try:
        lines = core_path.read_text().split("\n")
    except FileNotFoundError:
        return
    done_section = _done_boundary(lines, len(lines))
    expected_emoji = PRI_EMOJI.get(pri) if pri else None

    # Find the matching line; prefer an exact (name + priority emoji) match
    # over a name-only match when `pri` is given.
    target_idx = None
    fallback_idx = None
    for i, line in enumerate(lines[:done_section]):
        stripped = line.strip()
        if not stripped.startswith("- ["):
            continue
        if _extract_task_name(line) != old_name:
            continue
        if expected_emoji and expected_emoji in line:
            target_idx = i
            break
        if fallback_idx is None:
            fallback_idx = i
    i = target_idx if target_idx is not None else fallback_idx
    if i is None:
        return
    line = lines[i]
    m = re.match(r"(\s*- \[.\]\s+)", line)
    if not m:
        return
    rest = line[m.end():]
    emoji_prefix = ""
    for emoji in PRI_EMOJI.values():
        if rest.startswith(emoji + " "):
            emoji_prefix = emoji + " "
            rest = rest[len(emoji) + 1:]
            break
    cut = _TASK_NAME_BOUNDARY.search(rest)
    suffix = rest[cut.start():] if cut else ""
    lines[i] = m.group(1) + emoji_prefix + new_name + suffix
    _atomic_write_text(core_path, "\n".join(lines))


# Reject names containing newlines, tabs, or markdown-marker glyphs that
# would break the core-file structure if written verbatim.
_RENAME_FORBIDDEN = re.compile(r"[\x00-\x1f\x7f]")


_CARRIED_RE = re.compile(r"\s*_\(carried from [^)]+\)_")


def update_core_file_task(old_name, old_pri, new_name, new_pri,
                          new_due, new_why, new_links, week=None):
    """Rewrite the WHOLE task line in the core file: name, priority emoji,
    due, links, why. Preserves the state marker and any `_(carried from
    Wxx)_` metadata. Used by `apply_edit` — broader than `update_core_file_name`
    (name only) or `update_core_file_priority` (emoji only)."""
    core_path = current_core_path(week)
    try:
        lines = core_path.read_text().split("\n")
    except FileNotFoundError:
        return
    done_section = _done_boundary(lines, len(lines))
    expected_emoji = PRI_EMOJI.get(old_pri) if old_pri else None

    # Prefer name+emoji match (disambiguates duplicate names across priorities).
    target_idx = None
    fallback_idx = None
    for i, line in enumerate(lines[:done_section]):
        if not line.strip().startswith("- ["):
            continue
        if _extract_task_name(line) != old_name:
            continue
        if expected_emoji and expected_emoji in line:
            target_idx = i
            break
        if fallback_idx is None:
            fallback_idx = i
    i = target_idx if target_idx is not None else fallback_idx
    if i is None:
        return

    line = lines[i]
    m = re.match(r"(\s*- \[.\]\s+)", line)
    if not m:
        return
    state_prefix = m.group(1)

    # Preserve `_(carried from W..)_` if the task was carried over.
    carried_match = _CARRIED_RE.search(line)
    carried_suffix = carried_match.group(0) if carried_match else ""

    new_emoji = PRI_EMOJI.get(new_pri, "")
    body = f"{new_emoji} {new_name}" if new_emoji else new_name
    if new_due and new_due != "—":
        resolved_due = (
            datetime.date.today().isoformat() if new_due == "today" else new_due
        )
        body += f" — due {resolved_due}"
    if new_links:
        link_strs = " · ".join(f"[{l['label']}]({l['url']})" for l in new_links)
        body += f" ({link_strs})"
    if carried_suffix:
        body += carried_suffix
    if new_why and new_why != "—":
        body += f" _(why: {new_why})_"

    lines[i] = state_prefix + body
    _atomic_write_text(core_path, "\n".join(lines))


def apply_edit(task_id, fields):
    """Edit a task — updates name, pri, due, why, links in JSON + core file.
    Preserves state marker and `_(carried from)_` metadata. Returns False if
    the new name is empty or contains control chars (would corrupt markdown).
    Section is NOT auto-moved on priority change — matches `apply_priority_update`
    behaviour; the user can drag if they want a different section.
    String task_ids are accepted (find_task_by_id coerces) so the JS edit
    flow doesn't silently 400 on row.dataset.id."""
    data = _load_state()
    if data is None:
        return False
    task, source = find_task_by_id(data, task_id)
    if task is None:
        _log_request(f"WARN apply_edit({task_id!r}) rejected: task not found")
        return False

    new_name = (fields.get("task") or "").strip()
    if not new_name:
        _log_request(f"WARN apply_edit({task_id}) rejected: empty task name")
        return False
    if _RENAME_FORBIDDEN.search(new_name):
        bad = _RENAME_FORBIDDEN.search(new_name).group(0)
        _log_request(
            f"WARN apply_edit({task_id}) rejected: control char "
            f"U+{ord(bad):04X} in task name {new_name!r}"
        )
        return False
    # Preserve explicit None ("no priority") — only default when the field
    # is absent entirely. Empty-string is treated as None.
    if "pri" in fields:
        new_pri = fields["pri"] or None
    else:
        new_pri = task.get("pri") or "P2"
    new_due = (fields.get("due") or "").strip() or "—"
    new_why = (fields.get("why") or "").strip() or "—"
    if _RENAME_FORBIDDEN.search(new_why):
        bad = _RENAME_FORBIDDEN.search(new_why).group(0)
        _log_request(
            f"WARN apply_edit({task_id}) rejected: control char "
            f"U+{ord(bad):04X} in why {new_why!r}"
        )
        return False
    link_label = (fields.get("link_label") or "").strip()
    link_url = (fields.get("link_url") or "").strip()
    # The modal only edits one link, but the JSON schema allows >1.
    # Preserve any extras (existing links beyond the first) so a manually
    # curated multi-link task isn't silently truncated to one.
    extra_links = (task.get("links") or [])[1:]
    if link_label and link_url:
        new_links = [{"label": link_label, "url": link_url}] + extra_links
    else:
        new_links = extra_links

    old_name = task.get("task", "")
    old_pri = task.get("pri")

    task["task"] = new_name
    task["pri"] = new_pri
    task["due"] = new_due
    task["why"] = new_why
    task["links"] = new_links

    _save_state(data)
    update_core_file_task(
        old_name, old_pri, new_name, new_pri, new_due, new_why, new_links,
        week=data.get("week"),
    )
    # Re-snapshot Today's Focus if this task is in it (focus list keys by name).
    if source is not None:
        _snapshot_focus_if_touched(data, source.get("title", ""))
    return task


def apply_rename(task_id, new_name):
    """Rename a task's display name. Updates JSON `task` field and the
    matching markdown line's name slot. Rejects names with control chars
    (newlines, tabs, etc.) that would corrupt the markdown."""
    new_name = (new_name or "").strip()
    if not new_name:
        return False
    if _RENAME_FORBIDDEN.search(new_name):
        return False
    data = _load_state()
    if data is None:
        return False
    task, source_section = find_task_by_id(data, task_id)
    if task is None:
        return False
    old_name = task.get("task", "")
    if old_name == new_name:
        return task  # no-op success
    pri = task.get("pri")
    task["task"] = new_name
    _save_state(data)
    update_core_file_name(old_name, new_name, week=data.get("week"), pri=pri)
    # If the renamed task was in Today's Focus, the journal still keys the
    # focus list by old name — re-snapshot so the new name shows up.
    if source_section is not None:
        _snapshot_focus_if_touched(data, source_section.get("title", ""))
    return task


def apply_uncancel(task_id):
    """Reverse the most recent /cancel for `task_id` if still in the undo window.
    Restores the task to its original section in JSON and removes the [/]
    line from `## Cancelled` in the core markdown."""
    with _undo_lock:
        rec = _recent_cancels.pop(task_id, None)
    if rec is None or (time.time() - rec.get("at", 0)) > UNDO_WINDOW_S:
        return False

    data = _load_state()
    if data is None:
        return False

    # Restore the task to its original section (or fall back by priority)
    target = next(
        (s for s in data.get("sections", []) if s.get("title") == rec["src_title"]),
        None,
    )
    if target is None:
        target_title = target_section_for_pri(rec["task"].get("pri"))
        target = next((s for s in data.get("sections", []) if s.get("title") == target_title), None)
    if target is None:
        return False
    target["tasks"].append(rec["task"])

    # Reverse the markdown surgery: find the [/] line in ## Cancelled and
    # restore it to active (state marker → [ ], strip _(cancelled: …)_).
    now = datetime.datetime.now()
    core_path = current_core_path(data.get("week"))
    task_name = rec["task"].get("task", "")
    try:
        lines = core_path.read_text().split("\n")
    except FileNotFoundError:
        lines = None
    if lines is not None:
        cancel_idx = next(
            (i for i, l in enumerate(lines) if l.strip() == "## Cancelled"),
            None,
        )
        wrote = False
        if cancel_idx is not None:
            rel = find_task_line(lines[cancel_idx:], task_name, marker="[/]")
            if rel is not None:
                idx = cancel_idx + rel
                # Restore the original state marker, not always [ ].
                marker = STATE_MARKER.get(rec["task"].get("status", "open"), "[ ]")
                restored = re.sub(r"\[/\]", marker, lines[idx], count=1)
                restored = re.sub(r"\s*_\(cancelled:[^)]+\)_", "", restored)
                lines.pop(idx)
                # Insert before ## Done
                done_section = _done_boundary(lines, len(lines))
                lines.insert(done_section, restored)
                wrote = True
        if wrote:
            _atomic_write_text(core_path, "\n".join(lines))

    _snapshot_focus_if_touched(data, rec["src_title"])
    renumber_tasks(data)
    _save_state(data, now)
    return rec["task"]


def _load_state():
    """Read tasks-live.json. Returns the dict, or None if unreadable."""
    try:
        return json.loads(JSON_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    except Exception as e:
        _log_request(f"ERROR  _load_state: {type(e).__name__}: {e}")
        return None


def _save_state(data, now=None):
    """Stamp `updated` and persist `data` to tasks-live.json. Notifies SSE
    clients directly so click-driven mutations push instantly without
    waiting for the polling watcher.
    Atomic write via _atomic_write_json — a partial write is never observed,
    even if the process crashes mid-flush."""
    data["updated"] = (now or datetime.datetime.now()).strftime("%Y-%m-%d %H:%M")
    _atomic_write_json(JSON_FILE, data)
    _bump_state_version()


def _apply_cross_section_effects(src_task, src_title, tgt_title, now, week=None):
    """Priority bumps + Monitoring status flips when a task crosses sections.
    Mutates `src_task` in place and updates the core file."""
    if tgt_title == SEC_HIGH and src_task.get("pri") not in ("P1", "P2"):
        src_task["pri"] = "P2"
        update_core_file_priority(src_task.get("task", ""), "P2", now, week=week)
    elif tgt_title == SEC_LOW and src_task.get("pri") in ("P1", "P2"):
        src_task["pri"] = "P3"
        update_core_file_priority(src_task.get("task", ""), "P3", now, week=week)

    if tgt_title == SEC_MON and src_task.get("status") != "waiting":
        src_task["status"] = "waiting"
        update_core_file(src_task.get("task", ""), "waiting", now, week=week)
    elif src_title == SEC_MON and tgt_title != SEC_MON:
        src_task["status"] = "open"
        update_core_file(src_task.get("task", ""), "open", now, week=week)


def _snapshot_focus_if_touched(data, *titles):
    """If any of `titles` is 'Today's Focus', persist the current focus list
    to the journal so changes survive `tk` rebuilds."""
    if SEC_FOCUS not in titles:
        return
    focus = next((s for s in data.get("sections", []) if s.get("title") == SEC_FOCUS), None)
    if focus is not None:
        set_today_focus([t.get("task", "") for t in focus.get("tasks", [])])


def apply_move_section(task_id, target_title):
    """Move a task by id to a named section (appending at end), with the same
    side effects as drag-and-drop: priority bumps, status flips, focus journal updates."""
    data = _load_state()
    if data is None:
        return False

    src_task, src_section = find_task_by_id(data, task_id)
    if not src_task or src_section is None:
        return False

    tgt_section = next((s for s in data.get("sections", []) if s.get("title") == target_title), None)
    if tgt_section is None:
        return False

    if src_section is tgt_section:
        return src_task  # already in target section — no-op

    src_title = src_section.get("title")
    tgt_title = tgt_section.get("title")
    now = datetime.datetime.now()

    src_section["tasks"] = [t for t in src_section["tasks"] if t.get("id") != task_id]
    _apply_cross_section_effects(src_task, src_title, tgt_title, now, week=data.get("week"))

    tgt_section["tasks"].append(src_task)
    renumber_tasks(data)
    _save_state(data, now)
    sync_core_file_order(data)
    _snapshot_focus_if_touched(data, src_title, tgt_title)
    return src_task


def apply_reorder(from_num, to_num, before=True):
    """Move task (by stable id) to before/after another task. Updates priority when crossing sections."""
    data = _load_state()
    if data is None:
        return False

    src_task, src_section = find_task_by_id(data, from_num)
    tgt_task, tgt_section = find_task_by_id(data, to_num)

    if not src_task or not tgt_task or src_section is None or tgt_section is None:
        return False

    tgt_idx = next((i for i, t in enumerate(tgt_section["tasks"]) if t.get("id") == to_num), None)
    if tgt_idx is None:
        return False

    src_section["tasks"] = [t for t in src_section["tasks"] if t.get("id") != from_num]

    # Recalculate index after removal if same section
    if src_section is tgt_section:
        tgt_idx = next((i for i, t in enumerate(tgt_section["tasks"]) if t.get("id") == to_num),
                       len(tgt_section["tasks"]))

    now = datetime.datetime.now()
    src_title = src_section.get("title")
    tgt_title = tgt_section.get("title")
    if src_section is not tgt_section:
        _apply_cross_section_effects(src_task, src_title, tgt_title, now, week=data.get("week"))

    insert_pos = tgt_idx if before else tgt_idx + 1
    tgt_section["tasks"].insert(insert_pos, src_task)
    renumber_tasks(data)
    _save_state(data, now)
    sync_core_file_order(data)
    _snapshot_focus_if_touched(data, src_title, tgt_title)
    return {"ok": True}


def add_to_core_file(name, pri, due, why, links, week=None):
    """Insert a new active task line before ## Done in the core file."""
    core_path = current_core_path(week)
    try:
        lines = core_path.read_text().split("\n")
    except FileNotFoundError:
        return
    emoji = PRI_EMOJI.get(pri, "")
    task_line = f"- [ ] {emoji} {name}" if emoji else f"- [ ] {name}"
    if due and due not in ("—", "\u2014"):
        # Resolve "today" to an explicit date so it does not go stale in the core file
        resolved_due = datetime.date.today().isoformat() if due == "today" else due
        task_line += f" — due {resolved_due}"
    if links:
        link_strs = " · ".join(f"[{l['label']}]({l['url']})" for l in links)
        task_line += f" ({link_strs})"
    if why and why not in ("—", "\u2014"):
        task_line += f" _(why: {why})_"
    done_section = _done_boundary(lines, len(lines))
    lines.insert(done_section, task_line)
    _atomic_write_text(core_path, "\n".join(lines))


def add_completed_to_core_file(name, pri, completed_time, links, week=None):
    """Insert a completed task directly into the ## Done section of the core file."""
    core_path = current_core_path(week)
    try:
        lines = core_path.read_text().split("\n")
    except FileNotFoundError:
        return
    emoji = PRI_EMOJI.get(pri, "")
    today_str = datetime.date.today().isoformat()
    ts = f"{today_str} {completed_time}"
    task_line = f"- [x] {emoji} {name}" if emoji else f"- [x] {name}"
    if links:
        link_strs = " · ".join(f"[{l['label']}]({l['url']})" for l in links)
        task_line += f" ({link_strs})"
    task_line += f" _(completed: {ts})_"
    _insert_under_dated_section(lines, "Done", task_line, today_str)
    _atomic_write_text(core_path, "\n".join(lines))


# ---------------------------------------------------------------------------
# Slack triage snapshot / dismiss / convert
# ---------------------------------------------------------------------------

def load_slack_snapshot():
    """Read ~/todo/slack-triage.json. Returns:
      None                    — file missing
      {"_error": "malformed"} — file present but unparseable
      {"_error": "version", "got": N} — version mismatch
      <dict>                  — valid snapshot
    Reads do not take _slack_lock — atomic os.replace makes partial reads
    impossible. The lock is only for read-modify-write paths."""
    if not SLACK_SNAPSHOT_FILE.exists():
        return None
    try:
        data = json.loads(SLACK_SNAPSHOT_FILE.read_text())
    except (OSError, json.JSONDecodeError) as e:
        _log_request(f"WARN slack-triage.json malformed: {e}")
        return {"_error": "malformed"}
    if data.get("version") != SLACK_SNAPSHOT_VERSION:
        return {"_error": "version", "got": data.get("version")}
    return data


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec="seconds")


def load_slack_log(path, ttl_days=None):
    """Read an append-only JSONL log. Returns a list of records (dicts).
    If `ttl_days` is set, drop records whose `ts` is older than the cutoff.
    Records without a parseable `ts` are kept (better safe than sorry —
    we'd rather show a never-dismissed item than silently lose dismissals).
    Malformed individual lines are skipped silently — one bad line should
    not break the whole view."""
    if not path.exists():
        return []
    cutoff = None
    if ttl_days is not None:
        cutoff = (datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(days=ttl_days))
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if cutoff is not None:
                    ts_str = rec.get("ts")
                    if isinstance(ts_str, str):
                        try:
                            rec_ts = datetime.datetime.fromisoformat(ts_str)
                            if rec_ts.tzinfo is None:
                                rec_ts = rec_ts.replace(tzinfo=datetime.timezone.utc)
                            if rec_ts < cutoff:
                                continue
                        except ValueError:
                            pass
                out.append(rec)
    except OSError:
        return []
    return out


def _append_slack_log(path, record):
    """Append one JSON record + newline. Caller MUST hold `_slack_lock` —
    not because the append itself isn't atomic on POSIX (it is, for our
    sub-PIPE_BUF lines), but because compaction races with concurrent
    appends: `_maybe_compact_slack_log` reads the file, then atomically
    rewrites it via `os.replace`, which would silently drop any append
    that landed on the now-orphaned inode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)


def _atomic_write_jsonl(path, records):
    """Rewrite a JSONL log atomically — used by compaction."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _maybe_compact_slack_log(path, ttl_days):
    """If the file exceeds SLACK_LOG_COMPACT_BYTES, rewrite keeping only
    TTL-active records. Cheap no-op below the threshold."""
    try:
        if path.stat().st_size < SLACK_LOG_COMPACT_BYTES:
            return
    except OSError:
        return
    records = load_slack_log(path, ttl_days=ttl_days)
    _atomic_write_jsonl(path, records)


def load_slack_dismissed():
    """Returns (msg_ids, thread_ids) — two sets, both TTL-filtered.
    `msg_ids` are composite "channel_id:message_ts" keys; `thread_ids` are
    composite "channel_id:thread_ts" keys (a top-level message dismissed as
    a thread uses its own message_ts as the thread root)."""
    msgs, threads = set(), set()
    for rec in load_slack_log(SLACK_DISMISSED_FILE, ttl_days=SLACK_DISMISS_TTL_DAYS):
        rid = rec.get("id")
        if not isinstance(rid, str) or not rid:
            continue
        if rec.get("kind") == "thread":
            threads.add(rid)
        else:
            msgs.add(rid)
    return msgs, threads


def load_slack_converted():
    """Returns set of converted ids. NOT TTL-filtered: the converted record
    is the dashboard's "this Slack item became task N" memory and should
    persist as long as the task does. If the file ever grows large in
    practice, we can add TTL or compact-against-existing-tasks then."""
    out = set()
    for rec in load_slack_log(SLACK_CONVERTED_FILE):
        rid = rec.get("id")
        if isinstance(rid, str) and rid:
            out.add(rid)
    return out


def apply_slack_dismiss(item_id, scope="message"):
    """Append a dismissal record. `scope` is "message" (default — hides
    only this message_ts) or "thread" (hides every item with this
    thread_ts; the caller passes the thread root id, NOT the reply id)."""
    if not isinstance(item_id, str) or not item_id:
        return False
    if scope not in ("message", "thread"):
        return False
    record = {"id": item_id, "kind": scope, "ts": _now_iso()}
    with _slack_lock:
        _append_slack_log(SLACK_DISMISSED_FILE, record)
        _maybe_compact_slack_log(SLACK_DISMISSED_FILE, ttl_days=SLACK_DISMISS_TTL_DAYS)
    _bump_state_version()
    return {"ok": True}


def apply_slack_convert(body):
    """Add a task via apply_add, then append a converted record. On
    apply_add failure, do NOT append (item stays in the view).

    PRECONDITION: `_state_lock` must be held externally — apply_add
    mutates tasks-live.json and follows the dashboard's convention that
    do_POST takes `_state_lock` for the entire handler call. Calling
    this function outside the request path (e.g. from a script) without
    that lock will race against concurrent task mutations."""
    item_id = body.get("id")
    if not isinstance(item_id, str) or not item_id:
        return False
    added = apply_add(body)
    if not added:
        return False
    record = {"id": item_id, "ts": _now_iso()}
    with _slack_lock:
        _append_slack_log(SLACK_CONVERTED_FILE, record)
    _bump_state_version()
    return added


def apply_add(task_data):
    """Add a new task to JSON and core file."""
    data = _load_state()
    if data is None:
        return False
    name = (task_data.get("task") or "").strip()
    if not name:
        return False
    if _RENAME_FORBIDDEN.search(name):
        return False
    pri = task_data.get("pri") or "P2"
    if pri not in PRI_EMOJI:
        return False
    due = task_data.get("due") or "\u2014"
    if due != "\u2014" and not re.match(r'^(\d{4}-\d{2}-\d{2}|([01]\d|2[0-3]):[0-5]\d|today)$', due):
        return False
    why = task_data.get("why") or "\u2014"
    if why != "\u2014" and _RENAME_FORBIDDEN.search(why):
        return False
    link_label = (task_data.get("link_label") or "").strip()
    link_url = (task_data.get("link_url") or "").strip()
    links = [{"label": link_label, "url": link_url}] if link_label and link_url else []

    completed_at = (task_data.get("completed_at") or "").strip()
    if completed_at and not re.match(r'^([01]\d|2[0-3]):[0-5]\d$', completed_at):
        return False

    target_title = target_section_for_pri(pri)
    target = next((s for s in data.get("sections", []) if s.get("title") == target_title), None)
    if target is None:
        return False

    task_id = next_task_id(data)
    now = datetime.datetime.now()

    if completed_at:
        completed_entry = {
            "num": 999,
            "id": task_id,
            "task": name,
            "links": links,
            "time": completed_at,
            "status": "done",
            "from_section": target_title,
            "pri": pri,
            "due": due,
            "from": "—",
            "added": now.strftime("%Y-%m-%d"),
            "why": why,
        }
        data.setdefault("completed_today", []).append(completed_entry)
        renumber_tasks(data)
        _save_state(data, now)
        add_completed_to_core_file(name, pri, completed_at, links, week=data.get("week"))
        return completed_entry

    new_task = {
        "num": 999,
        "id": task_id,
        "pri": pri,
        "task": name,
        "due": due,
        "from": "\u2014",
        "added": datetime.date.today().isoformat(),
        "links": links,
        "status": "open",
        "why": why,
    }
    target["tasks"].append(new_task)
    renumber_tasks(data)
    _save_state(data)
    add_to_core_file(name, pri, due, why, links, week=data.get("week"))
    return new_task


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        t0 = time.perf_counter()
        if self.path == "/events":
            self._handle_sse()
            return
        if self.path.startswith("/open"):
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            url = params.get("url", [""])[0]
            if url and url.startswith(("https://", "http://")):
                subprocess.run(["open", url])
            self.send_response(204)
            self.end_headers()
            return
        if self.path.startswith("/task"):
            # Returns the JSON record for one task. Used by the Edit modal
            # to pre-fill its fields without rendering the full task data
            # into every row's HTML.
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                task_id = int(params.get("id", ["0"])[0])
            except ValueError:
                task_id = 0
            data = _load_state() if task_id else None
            task = find_task_by_id(data, task_id)[0] if data else None
            if not task:
                self.send_response(404); self.end_headers(); return
            payload = json.dumps(task).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        # Parse ?view=… so different layouts can be requested
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        view = (params.get("view", ["dashboard"])[0] or "dashboard").lower()

        # Conditional GET via ETag (sub-second precision).
        # Last-Modified is also sent for browsers that prefer it, but ETag wins
        # because Last-Modified would truncate to seconds and miss rapid updates
        # (e.g. a drag-drop write within the same second as the previous fetch).
        try:
            json_mtime = os.path.getmtime(JSON_FILE)
            src_mtime  = os.path.getmtime(os.path.abspath(__file__))
            # Include current-week core file mtime so a `## Done` edit busts the
            # ETag — the sparkline reads it directly and would otherwise stay stale.
            try:
                core_mtime = os.path.getmtime(current_core_path())
            except OSError:
                core_mtime = 0
            slack_t = _safe_mtime(SLACK_SNAPSHOT_FILE)
            slack_d = _safe_mtime(SLACK_DISMISSED_FILE)
            slack_c = _safe_mtime(SLACK_CONVERTED_FILE)
            combined_mtime = max(json_mtime, src_mtime, core_mtime,
                                 slack_t, slack_d, slack_c)
            etag = f'"{combined_mtime:.6f}-{view}"'
            last_modified = email.utils.formatdate(int(combined_mtime), usegmt=True)
        except Exception:
            combined_mtime = None
            etag = None
            last_modified = None

        inm = self.headers.get("If-None-Match")
        if etag is not None and inm and inm == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            # `no-cache` forces the browser to always revalidate instead of
            # serving from heuristic cache without hitting us. Without this,
            # Chrome happily returned a stale cached body to fetch() and the
            # post-/add refresh appeared to succeed (200) but showed old state.
            self.send_header("Cache-Control", "no-cache")
            if last_modified:
                self.send_header("Last-Modified", last_modified)
            self.end_headers()
            # 304s are pure polling no-ops — skip logging.
            return

        try:
            data = json.loads(JSON_FILE.read_text())
            page = build_page(data, view=view)
        except Exception as e:
            page = f"<pre style='color:#f85149;padding:16px'>Error: {h(str(e))}</pre>"

        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        if etag:
            self.send_header("ETag", etag)
        if last_modified:
            self.send_header("Last-Modified", last_modified)
        self.end_headers()
        self.wfile.write(page.encode())
        ms = int((time.perf_counter() - t0) * 1000)
        src = "poll" if inm else "nav"
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        _log_request(f"{ts}  GET  {self.path:25s}  ms={ms:<4d} status=200  {src}")

    _ROUTES = {
        "/update":       lambda b: apply_status_change(b.get("id")),
        "/complete":     lambda b: apply_status_change(b.get("id"), force_status="done"),
        "/update-pri":   lambda b: apply_priority_update(b.get("id")),
        "/uncomplete":   lambda b: apply_uncomplete(b.get("id")),
        "/sort":         lambda b: apply_sort(),
        "/reorder":      lambda b: apply_reorder(b.get("from"), b.get("to"), b.get("before", True)),
        "/move-section": lambda b: apply_move_section(b.get("id"), b.get("section")),
        "/cancel":       lambda b: apply_cancel(b.get("id")),
        "/uncancel":     lambda b: apply_uncancel(b.get("id")),
        "/rename":       lambda b: apply_rename(b.get("id"), b.get("name", "")),
        "/add":          lambda b: apply_add(b),
        "/edit":         lambda b: apply_edit(b.get("id"), b),
        "/slack/dismiss": lambda b: apply_slack_dismiss(
            b.get("id"), scope=(b.get("scope") or "message")),
        "/slack/convert": lambda b: apply_slack_convert(b),
    }

    def do_POST(self):
        t0 = time.perf_counter()
        handler = self._ROUTES.get(self.path)
        if handler is None:
            self.send_response(404)
            self.end_headers()
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            _log_request(f"{ts}  POST {self.path:25s}  ms=0    status=404")
            return
        length = int(self.headers.get("Content-Length", 0))
        if length > 65536:
            self.send_response(413)
            self.end_headers()
            return
        origin = self.headers.get("Origin", "")
        if origin and origin != f"http://localhost:{PORT}":
            self.send_response(403)
            self.end_headers()
            return
        try:
            body = json.loads(self.rfile.read(length)) if length else {}
        except (json.JSONDecodeError, ValueError):
            self.send_response(400)
            self.end_headers()
            return
        with _state_lock:
            result = handler(body)
        if isinstance(result, dict):
            status = 200
            payload = json.dumps(result).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            status = 200 if result else 400
            self.send_response(status)
            self.end_headers()
        ms = int((time.perf_counter() - t0) * 1000)
        # `id` is the most useful identifier for clicks; fall back to from/to for /reorder
        id_part = body.get("id")
        if id_part is None and "from" in body:
            id_part = f"{body.get('from')}->{body.get('to')}"
        id_str = f"id={id_part}" if id_part is not None else ""
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        _log_request(
            f"{ts}  POST {self.path:25s}  ms={ms:<4d} status={status}  {id_str}".rstrip()
        )

    def _handle_sse(self):
        """Long-lived Server-Sent Events stream. Pushes one `data: <version>`
        line each time `_state_version` changes; sends a `:keepalive` comment
        every 30s so intermediates don't close idle connections."""
        global _sse_clients
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        with _state_cond:
            _sse_clients += 1
            last_seen = _state_version
            _state_cond.notify_all()  # wake the watcher in case it's idle
        try:
            self.wfile.write(f"data: {last_seen}\n\n".encode())
            self.wfile.flush()
            while True:
                with _state_cond:
                    _state_cond.wait_for(
                        lambda: _state_version != last_seen,
                        timeout=30,
                    )
                    cur = _state_version
                if cur != last_seen:
                    last_seen = cur
                    msg = f"data: {cur}\n\n"
                else:
                    msg = ": keepalive\n\n"
                self.wfile.write(msg.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with _state_cond:
                _sse_clients -= 1

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    print(f"Tasks at http://localhost:{PORT}")
    _server = http.server.ThreadingHTTPServer(("", PORT), Handler)
    _server.socket.set_inheritable(False)
    _server.serve_forever()
