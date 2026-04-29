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
import threading
import time
import urllib.parse
from pathlib import Path

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


def _log_request(line):
    """Append one line to the request log; never crash a request on log failure."""
    try:
        with open(REQUEST_LOG, "a") as f:
            f.write(line + "\n")
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

# Notify SSE clients whenever the rendered state could have changed.
# `_state_version` increments on every observed mtime change of the JSON,
# this script, or the current-week core file.
_state_cond = threading.Condition()
_state_version = 0


def _state_signature():
    """Combined mtime tuple — None if any required file is missing."""
    try:
        json_m = os.path.getmtime(JSON_FILE)
        src_m  = os.path.getmtime(os.path.abspath(__file__))
    except OSError:
        return None
    try:
        core_m = os.path.getmtime(current_core_path())
    except OSError:
        core_m = 0
    return (json_m, src_m, core_m)


def _watch_state():
    """Bump `_state_version` and notify when mtimes change. ~50ms poll → ~50ms
    end-to-end latency from a write to the browser DOM update via SSE."""
    global _state_version
    last = _state_signature()
    while True:
        time.sleep(0.05)
        sig = _state_signature()
        if sig is None or sig == last:
            continue
        last = sig
        with _state_cond:
            _state_version += 1
            _state_cond.notify_all()


if not os.environ.get("SERVE_TASKS_NO_WATCH"):
    threading.Thread(target=_watch_state, daemon=True).start()

_TASK_NAME_BOUNDARY = re.compile(r"\s+(?:—|\(|_\()")

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

    ins = section_idx + 1
    if ins < len(lines) and lines[ins] == "":
        ins += 1
    if ins < len(lines) and lines[ins] == f"### {today_str}":
        lines.insert(ins + 1, dated_line)
    else:
        lines.insert(section_idx + 1, dated_line)
        lines.insert(section_idx + 1, f"### {today_str}")
        lines.insert(section_idx + 1, "")


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
  padding: 3px 0 3px 10px;
  margin: 0 0 8px;
  border-left: 3px solid #388bfd;
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
.expand-all-btn {
  background: transparent; border: 0; cursor: pointer;
  color: #6e7681; font-size: 32px; line-height: 1;
  padding: 0 10px; border-radius: 4px;
}
.expand-all-btn:hover { color: #c9d1d9; background: #21262d; }
.expand-all-btn.open { color: #58a6ff; }
td.task-cell { cursor: pointer; }
td.task-cell:hover { color: #58a6ff; }
tr.expanded td.task-cell { color: #58a6ff; }
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
#floating-actions {
  position: fixed; top: 16px; right: 16px; z-index: 50;
  display: flex; gap: 8px;
}
#sort-btn, #add-btn {
  background: #1c2128; border: 1px solid #30363d;
  color: #e6edf3; padding: 8px 14px; border-radius: 8px;
  cursor: pointer; font-size: 13px; font-weight: 600;
  letter-spacing: 0.02em;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.3);
}
#sort-btn:hover, #add-btn:hover { background: #2d333b; border-color: #484f58; }
#add-btn { color: #3fb950; }
#add-btn:hover { background: rgba(63, 185, 80, 0.12); border-color: rgba(63, 185, 80, 0.5); }
#modal-overlay {
  display: none; position: fixed; inset: 0; z-index: 100;
  background: rgba(0,0,0,0.6); align-items: center; justify-content: center;
}
#modal-overlay.open { display: flex; }
#modal {
  background: #161b22; border: 1px solid #30363d; border-radius: 8px;
  padding: 20px; width: 460px; max-width: 96vw;
}
#modal h3 { font-size: 14px; margin-bottom: 14px; color: #e6edf3; }
#modal label { display: block; font-size: 11px; color: #8b949e; margin-bottom: 3px; }
#modal input, #modal select {
  width: 100%; background: #0d1117; border: 1px solid #30363d;
  color: #e6edf3; border-radius: 4px; padding: 5px 8px; font-size: 12px;
  margin-bottom: 10px;
}
.modal-row { display: flex; gap: 8px; }
.modal-row > div { flex: 1; }
.modal-footer { display: flex; justify-content: flex-end; gap: 8px; margin-top: 4px; }
#modal-cancel {
  background: none; border: 1px solid #30363d; color: #8b949e;
  padding: 5px 14px; border-radius: 4px; cursor: pointer; font-size: 12px;
}
#modal-save {
  background: #238636; border: none; color: #fff;
  padding: 5px 14px; border-radius: 4px; cursor: pointer; font-size: 12px;
}
#modal-cancel:hover { background: #30363d; }
#modal-save:hover { background: #2ea043; }
tr[draggable="true"] { cursor: grab; }
tr[draggable="true"]:active { cursor: grabbing; }
tr.dragging { opacity: 0.3; }
tr.drag-over-top > td { border-top: 2px solid #388bfd !important; }
tr.drag-over-bottom > td { border-bottom: 2px solid #388bfd !important; }
#topbar {
  display: flex; align-items: center; gap: 12px; margin-bottom: 10px;
}
.week-title {
  font-size: 22px; font-weight: 700; letter-spacing: 0.01em;
  color: #e6edf3; margin-right: 4px;
}
.week-title .wk-num { color: #3fb950; }
#view-switcher {
  display: flex; gap: 4px;
  background: #161b22; border: 1px solid #30363d; border-radius: 6px;
  padding: 3px; width: fit-content;
}
#view-switcher .vs-btn {
  padding: 4px 12px; border-radius: 4px;
  color: #8b949e; font-size: 12px; text-decoration: none;
}
#view-switcher .vs-btn:hover { color: #e6edf3; background: #21262d; text-decoration: none; }
#view-switcher .vs-btn.active { background: #30363d; color: #e6edf3; }
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
  }).then(function(r) { _refreshTasks(true); return r; });
}

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

// Add button + modal
document.getElementById('add-btn').addEventListener('click', function() {
  document.getElementById('modal-overlay').classList.add('open');
  setTimeout(function(){ document.getElementById('m-task').focus(); }, 50);
});
function closeModal() {
  document.getElementById('modal-overlay').classList.remove('open');
}
document.getElementById('modal-cancel').addEventListener('click', closeModal);
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
  _post('/add', payload).then(function(r) {
    if (!r.ok) return;
    closeModal();
    ['m-task','m-due','m-why','m-link-label','m-link-url'].forEach(function(id){
      var el = document.getElementById(id);
      if (el) el.value = '';
    });
    var pri = document.getElementById('m-pri');
    if (pri) pri.value = 'P2';
  });
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
  // Preserve the current view by including the search string in the fetch URL
  fetch('/' + window.location.search).then(function(r) {
    if (r.status === 304) return null;
    return r.text();
  }).then(function(html) {
    if (!html) return;
    var doc = new DOMParser().parseFromString(html, 'text/html');
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
    var endpoint, payload;
    if (item.dataset.action === 'cancel') {
      endpoint = '/cancel';
      payload = {id: _ctxTaskId};
    } else if (item.dataset.action === 'complete') {
      endpoint = '/complete';
      payload = {id: _ctxTaskId};
    } else {
      endpoint = '/move-section';
      payload = {id: _ctxTaskId, section: item.dataset.section};
    }
    _post(endpoint, payload);
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

// Single-letter hotkeys: x=expand-all, r=refresh, s=sort, a=add task.
// Fire only when the page has focus, no modal is open, no input is focused,
// and no modifier keys are held (so Cmd+R / Cmd+A still work as expected).
document.addEventListener('keydown', function(e) {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (!document.hasFocus()) return;
  var modal = document.getElementById('modal-overlay');
  if (modal && modal.classList.contains('open')) return;
  var t = document.activeElement;
  if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' ||
            t.tagName === 'SELECT' || t.isContentEditable)) return;
  if (e.key === 'x') {
    e.preventDefault();
    _toggleExpandAll();
  } else if (e.key === 'r') {
    e.preventDefault();
    _refreshTasks(true);
  } else if (e.key === 's') {
    e.preventDefault();
    _post('/sort', {});
  } else if (e.key === 'a') {
    e.preventDefault();
    document.getElementById('modal-overlay').classList.add('open');
    setTimeout(function(){ document.getElementById('m-task').focus(); }, 50);
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


def _section_header(label, color, *, expandable=False):
    """Standard section header bar with the section's accent colour.
    If `expandable=True`, append a chevron toggle that expands/collapses
    every detail panel in the following section."""
    btn = (
        '<button class="expand-all-btn" data-action="expand-all" '
        'title="Expand / collapse all">▾</button>'
        if expandable else ""
    )
    return (
        f'<div class="section-header" style="border-left-color:{color}">'
        f'{h(label)}{btn}</div>\n'
    )

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
    return " · ".join(f'<a href="{l["url"]}">{h(l["label"])}</a>' for l in links)

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

def render_core_section(title, tasks, week):
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
            f'<td class="{task_cls}">{h(t.get("task",""))}</td>',
            f'<td>{due_html}</td>',
            f'<td>{format_age(t.get("from"), week, t.get("added"))}</td>',
            f'<td>{render_links(t.get("links",[]))}</td>',
            f'<td>{render_status(t.get("status","open"), task_id)}</td>',
        ]
        rows.append(f'<tr{rc}{drag_attrs}>{"".join(cells)}</tr>')
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
    return (
        _section_header(label, color, expandable=any_why)
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
            f'<div class="cmp-row {rc_class}" draggable="true" data-id="{task_id}">'
            f'<span class="cmp-id" data-id="{task_id}">{task_id}</span>'
            f'<span class="cmp-pri">{pri_emoji}</span>'
            f'<span class="cmp-task">{h(t.get("task",""))}</span>'
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
        _section_header(label, color, expandable=True)
        + f'<div class="cmp-section">{"".join(rows)}</div>\n'
    )


def compute_counts(data):
    pri = {"P1": 0, "P2": 0, "P3": 0, "P4": 0, "P5": 0}
    status = {"in_progress": 0, "waiting": 0, "blocked": 0}
    overdue = 0
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
    return pri, status, overdue, len(data.get("completed_today", []))


def render_counts_strip(data):
    pri, status, overdue, done = compute_counts(data)
    pri_colors = {"P1": "#f85149", "P2": "#f0883e", "P3": "#e3b341", "P4": "#79c0ff", "P5": "#8b949e"}

    groups = []

    # Priority group — colour dots speak for themselves, no label needed
    pri_stats = "".join(
        f'<span class="stat"><span class="dot" style="background:{pri_colors[p]}"></span>{pri[p]}</span>'
        for p in ("P1", "P2", "P3", "P4", "P5") if pri[p]
    )
    if pri_stats:
        groups.append(f'<div class="cnt-group">{pri_stats}</div>')

    # Status group
    status_stats = []
    if status["in_progress"]: status_stats.append(f'<span class="stat"><span class="icon">🔄</span>{status["in_progress"]}</span>')
    if status["waiting"]:     status_stats.append(f'<span class="stat"><span class="icon">⏳</span>{status["waiting"]}</span>')
    if status["blocked"]:     status_stats.append(f'<span class="stat"><span class="icon">🚫</span>{status["blocked"]}</span>')
    if status_stats:
        groups.append(f'<div class="cnt-group">{"".join(status_stats)}</div>')

    # Overdue (only show if non-zero)
    if overdue:
        groups.append(f'<div class="cnt-group alert"><span class="stat"><span class="icon">⚠️</span>{overdue} overdue</span></div>')

    # Done today (always show — small dopamine hit when it's >0)
    groups.append(f'<div class="cnt-group success"><span class="stat"><span class="icon">✅</span>{done} done today</span></div>')

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
        _section_header(title, color)
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
        rows.append(
            f'<tr>'
            f'<td class="num num-done" data-id="{task_id}">{task_id}</td>'
            f'<td>{h(t.get("task",""))}</td>'
            f'<td>{render_links(t.get("links",[]))}</td>'
            f'<td>{h(t.get("time") or "—")}</td>'
            f'</tr>'
        )
    return (
        _section_header("Completed today", color)
        + '<table><thead><tr>'
        '<th style="width:2%">#</th>'
        '<th>Task</th>'
        '<th style="width:9%">Link</th>'
        '<th style="width:5%">Time</th>'
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


def render_compact_completed(tasks):
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
            f'<span class="field"><span class="field-label">Time</span>{h(time) if time else "—"}</span>',
        ]
        if t.get("from_section"):
            detail_parts.append(
                f'<span class="field"><span class="field-label">From</span>{h(t["from_section"])}</span>'
            )
        if t.get("links"):
            detail_parts.append(
                f'<span class="field"><span class="field-label">Links</span>{render_links(t.get("links"))}</span>'
            )
        rows.append(
            f'<div class="cmp-detail" data-id="{task_id}">{"".join(detail_parts)}</div>'
        )
    return (
        _section_header("Completed today", color, expandable=True)
        + f'<div class="cmp-section">{"".join(rows)}</div>\n'
    )

VIEWS = ["dashboard", "classic"]

def _build_dashboard_body(data, week):
    parts = [render_counts_strip(data)]

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

    left_html = "".join(
        card(
            render_core_section(title, sections_by_title[title].get("tasks", []), week),
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
    completed_html = render_compact_completed(data.get("completed_today", []))
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
    """Single-column view: counts strip + each section wrapped in a task-card, stacked."""
    parts = [render_counts_strip(data)]

    def card(html, variant=""):
        if not html:
            return ""
        cls = "task-card" + (f" {variant}" if variant else "")
        return f'<div class="{cls}">{html}</div>'

    CARD_VARIANTS = {SEC_FOCUS: "focus", SEC_HIGH: "high-priority"}
    for section in data.get("sections", []):
        stype = section.get("type", "core")
        title = section.get("title", "")
        tasks = section.get("tasks", [])
        if stype == "goalie":
            parts.append(card(render_goalie_section(title, tasks)))
        else:
            parts.append(card(render_core_section(title, tasks, week), variant=CARD_VARIANTS.get(title, "")))
    completed_html = render_completed(data.get("completed_today", []))
    if completed_html:
        parts.append(card(completed_html))
    if data.get("updated"):
        parts.append(f'<p class="counts" style="margin-top:16px;color:#484f58">Updated {h(data["updated"])}</p>\n')
    return "".join(parts)


def _view_switcher_html(current, week=""):
    items = "".join(
        f'<a href="?view={v}" class="vs-btn{" active" if v == current else ""}">{v.title()}</a>'
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
        f'</div>'
    )


def build_page(data, view="dashboard"):
    if view not in VIEWS:
        view = "dashboard"
    week = data.get("week", "")
    body = _build_classic_body(data, week) if view == "classic" else _build_dashboard_body(data, week)
    switcher = _view_switcher_html(view, week)
    return (
        f'<!DOCTYPE html><html><head>'
        f'<meta charset="utf-8">'
        f'<meta name="tasks-view" content="{view}">'
        f'<title>Tasks</title><style>{CSS}</style>'
        f'</head><body>{switcher}<div id="tasks-content">{body}</div>'
        f'<div id="floating-actions">'
        f'<button id="add-btn">+ Add</button>'
        f'<button id="sort-btn">⇕ Sort</button>'
        f'</div>'
        f'<div id="modal-overlay"><div id="modal">'
        f'<h3>Add Task</h3>'
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
        f'<div class="modal-footer">'
        f'<button id="modal-cancel">Cancel</button>'
        f'<button id="modal-save">Add task</button>'
        f'</div></div></div>'
        f'<div id="tooltip"></div>'
        f'<div id="ctx-menu">'
        f'<div class="ctx-header">Move to</div>'
        f'<div class="ctx-item" data-section="Today\u0027s Focus">Today\u0027s Focus</div>'
        f'<div class="ctx-item" data-section="{SEC_MON}">{SEC_MON}</div>'
        f'<div class="ctx-item" data-section="{SEC_HIGH}">{SEC_HIGH}</div>'
        f'<div class="ctx-item" data-section="{SEC_LOW}">{SEC_LOW}</div>'
        f'<div class="ctx-divider"></div>'
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

    journal_path.write_text("\n".join(new_lines))

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

    core_path.write_text("\n".join(lines))

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
    core_path.write_text("\n".join(lines))


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
    return True


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
    """Return (task, section) for the given stable id, or (None, None)."""
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
            if name in l.lower():
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
    core_path.write_text("\n".join(new_lines))


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
    return True


def apply_uncomplete(num):
    """Move a completed task back to active. Reads priority from core file to pick section."""
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
            core_path.write_text("\n".join(lines))

    # Remove from completed_today (by stable id, not num)
    data["completed_today"] = [t for t in data["completed_today"] if t.get("id") != num]

    # Add back to JSON — pick section by priority. Carry the stable id forward.
    new_task = {
        "id": num,
        "num": num,
        "pri": pri,
        "task": task_name,
        "due": "—",
        "from": "—",
        "links": task.get("links", []),
        "status": "open",
        "why": "—",
    }
    target_title = target_section_for_pri(pri)
    target = next((s for s in data.get("sections", []) if s.get("title") == target_title), None)
    if target is None:
        target = next((s for s in data.get("sections", []) if s.get("type", "core") == "core"), None)
    if target:
        target["tasks"].append(new_task)

    renumber_tasks(data)
    _save_state(data, now)
    return True


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
        data.setdefault("completed_today", []).append({
            "num": num,
            "id": task_id,
            "task": task_name,
            "links": task.get("links", []),
            "time": now.strftime("%H:%M"),
            "from_section": source_section.get("title", ""),
        })
    else:
        task["status"] = new_status

    _save_state(data, now)
    update_core_file(task_name, new_status, now, week=data.get("week"))
    return True

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

        core_path.write_text("\n".join(lines))

    _snapshot_focus_if_touched(data, src_title)

    renumber_tasks(data)
    _save_state(data, now)
    return True


def _load_state():
    """Read tasks-live.json. Returns the dict, or None if unreadable."""
    try:
        return json.loads(JSON_FILE.read_text())
    except Exception:
        return None


def _save_state(data, now=None):
    """Stamp `updated` and persist `data` to tasks-live.json."""
    data["updated"] = (now or datetime.datetime.now()).strftime("%Y-%m-%d %H:%M")
    JSON_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))


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
        return True  # already in target section — no-op

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
    return True


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
    return True


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
    core_path.write_text("\n".join(lines))


def apply_add(task_data):
    """Add a new task to JSON and core file."""
    data = _load_state()
    if data is None:
        return False
    name = (task_data.get("task") or "").strip()
    if not name:
        return False
    pri = task_data.get("pri") or "P2"
    due = task_data.get("due") or "\u2014"
    why = task_data.get("why") or "\u2014"
    link_label = (task_data.get("link_label") or "").strip()
    link_url = (task_data.get("link_url") or "").strip()
    links = [{"label": link_label, "url": link_url}] if link_label and link_url else []

    target_title = target_section_for_pri(pri)
    target = next((s for s in data.get("sections", []) if s.get("title") == target_title), None)
    if target is None:
        return False

    new_task = {
        "num": 999,
        "id": next_task_id(data),
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
    return True


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
            if url:
                subprocess.run(["open", url])
            self.send_response(204)
            self.end_headers()
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
            combined_mtime = max(json_mtime, src_mtime, core_mtime)
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
            page = f"<pre style='color:#f85149;padding:16px'>Error: {e}</pre>"

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
        "/add":          lambda b: apply_add(b),
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
        body = json.loads(self.rfile.read(length)) if length else {}
        with _state_lock:
            ok = handler(body)
        status = 200 if ok else 400
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
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            with _state_cond:
                last_seen = _state_version
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

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    print(f"Tasks at http://localhost:{PORT}")
    _server = http.server.ThreadingHTTPServer(("", PORT), Handler)
    _server.socket.set_inheritable(False)
    _server.serve_forever()
