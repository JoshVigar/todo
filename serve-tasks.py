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

threading.Thread(target=_watch_self, daemon=True).start()

JSON_FILE = Path.home() / "todo" / "tasks-live.json"
PORT = 6419

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

SECTION_COLORS = {
    "monitoring":      "#e3b341",
    "high priority":   "#f0883e",
    "lower priority":  "#388bfd",
    "today's focus":   "#3fb950",
    "completed today": "#3fb950",
    "goalie":          "#bc8cff",
}

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
.week-badge {
  font-size: 11px; font-weight: 700; letter-spacing: 0.06em;
  color: #8b949e; padding: 4px 10px; border: 1px solid #30363d;
  border-radius: 5px; background: #1c2128;
}
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
.spark-grid {
  display: grid; grid-template-columns: repeat(10, 1fr);
  gap: 4px; padding: 6px 4px 2px;
}
.spark-col { display: flex; flex-direction: column; align-items: stretch; gap: 3px; }
.spark-cell {
  height: 32px; border-radius: 4px;
  display: flex; align-items: center; justify-content: center;
  font-size: 11px; font-weight: 700; color: #e6edf3;
  border: 1px solid rgba(63, 185, 80, 0.25);
}
.spark-cell.empty {
  background: #1c2128; border-color: #30363d; color: transparent;
}
.spark-cell.today {
  outline: 2px solid #3fb950; outline-offset: 1px;
}
.spark-label {
  display: flex; flex-direction: column; align-items: center; gap: 0;
  font-size: 9px; color: #6e7681;
  text-transform: uppercase; letter-spacing: 0.03em; line-height: 1.2;
}
.spark-date { font-size: 8px; color: #484f58; }
.spark-col:has(.spark-cell.today) .spark-day { color: #3fb950; font-weight: 700; }
.spark-col:has(.spark-cell.today) .spark-date { color: #3fb950; }
.spark-total {
  float: right; color: #8b949e; font-weight: 500;
  text-transform: none; letter-spacing: 0; font-size: 11px;
}
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

SCRIPT = """\
var STATUS_NEXT = {
  'open':'in_progress', 'todo':'in_progress',
  'in_progress':'waiting', 'waiting':'open', 'blocked':'in_progress'
};
var STATUS_LABEL = {
  'in_progress':'\U0001F504 In Progress', 'waiting':'\u23F3 Waiting',
  'open':'\U0001F513 Open', 'todo':'\U0001F4CB To Do'
};
var STATUS_CLS = {
  'in_progress':'b-progress', 'waiting':'b-waiting',
  'open':'b-open', 'todo':'b-todo'
};
var PRI_NEXT = {'P1':'P2','P2':'P3','P3':'P4','P4':'P5','P5':'P1'};
var PRI_LABEL = {'P1':'P1 \U0001F534','P2':'P2 \U0001F7E0','P3':'P3 \U0001F7E1','P4':'P4 \U0001F535','P5':'P5 \u23F8\uFE0F'};
var PRI_CLS   = {'P1':'p1','P2':'p2','P3':'p3','P4':'p4','P5':'p5'};

document.addEventListener('click', function(e) {
  // Uncomplete via # cell on completed rows (table view OR compact dashboard view)
  var done_td = e.target.closest('td.num-done, .cmp-id-done');
  if (done_td && done_td.dataset.id) {
    e.preventDefault();
    var row = done_td.closest('tr, .cmp-row');
    if (row) {
      row.style.opacity = '0.35';
      row.style.transition = 'opacity 0.2s';
    }
    fetch('/uncomplete', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id: parseInt(done_td.dataset.id)})
    });
    return;
  }

  // Complete task via # cell on active rows
  var num_td = e.target.closest('td.num:not(.num-done), .cmp-id');
  if (num_td && num_td.dataset.id) {
    e.preventDefault();
    var row = num_td.closest('tr');
    row.style.opacity = '0.35';
    row.style.transition = 'opacity 0.2s';
    fetch('/complete', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id: parseInt(num_td.dataset.id)})
    });
    return;
  }

  // Status cycle (optimistic)
  var badge = e.target.closest('.status-badge');
  if (badge && badge.dataset.id && badge.dataset.status) {
    e.preventDefault();
    var cur = badge.dataset.status;
    var next = STATUS_NEXT[cur];
    if (next) {
      badge.dataset.status = next;
      badge.textContent = STATUS_LABEL[next] || next;
      badge.className = 'badge status-badge ' + (STATUS_CLS[next] || 'b-open');
      badge.dataset.id = badge.dataset.id; // keep
      fetch('/update', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({id: parseInt(badge.dataset.id)})
      });
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
    fetch('/update-pri', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id: parseInt(pri.dataset.id)})
    });
    return;
  }

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
});

// Sort button
document.getElementById('sort-btn').addEventListener('click', function() {
  fetch('/sort', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'}).then(_refreshTasks);
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
  fetch('/add', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)})
    .then(function(r) {
      if (r.ok) {
        closeModal();
        ['m-task','m-due','m-why','m-link-label','m-link-url'].forEach(function(id){document.getElementById(id).value='';});
        document.getElementById('m-pri').value = 'P2';
      }
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
  fetch('/reorder', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({from: _dragNum, to: toNum, before: before})
  }).then(_refreshTasks);
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
// Only runs when the tab has focus — switching to another app or tab pauses polling.
function _refreshTasks() {
  if (_dragPaused) return;
  if (!document.hasFocus()) return;
  var sy = window.scrollY;
  // Preserve which compact-row detail panels are currently expanded across the swap
  var openIds = Array.prototype.map.call(
    document.querySelectorAll('.cmp-detail.open'),
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
setInterval(_refreshTasks, 2000);
// Refresh immediately on regaining focus so you see fresh data as soon as you switch back.
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
    } else {
      endpoint = '/move-section';
      payload = {id: _ctxTaskId, section: item.dataset.section};
    }
    fetch(endpoint, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    }).then(_refreshTasks);
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
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def h(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

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
    if not from_week or from_week in ("—", "—"):
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
    if not due_str or due_str in ("—", "today") or "⚠️" in due_str or "-" in due_str:
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
    draggable = True

    # Hide Why column when no task has a meaningful reason
    show_why = any((t.get("why") or "—").strip() not in ("", "—") for t in tasks)

    rows = []
    for t in tasks:
        rc = row_classes(t)
        task_id = t.get("id", t.get("num", ""))
        due = t.get("due") or "—"
        due_html = f'<span class="due">{h(due)}</span>' if is_due_soon(due) else h(due)
        drag_attrs = f' draggable="true" data-id="{task_id}"' if draggable else ""
        cells = [
            f'<td class="num" data-id="{task_id}">{task_id}</td>',
            f'<td>{render_pri(t.get("pri"), task_id)}</td>',
            f'<td>{h(t.get("task",""))}</td>',
            f'<td>{due_html}</td>',
            f'<td>{format_age(t.get("from"), week, t.get("added"))}</td>',
            f'<td>{render_links(t.get("links",[]))}</td>',
            f'<td>{render_status(t.get("status","open"), task_id)}</td>',
        ]
        if show_why:
            cells.append(f'<td>{h(t.get("why") or "—")}</td>')
        rows.append(f'<tr{rc}{drag_attrs}>{"".join(cells)}</tr>')
    headers = [
        '<th style="width:32px">#</th>',
        '<th style="width:48px">Pri</th>',
        '<th>Task</th>',
        '<th style="width:110px">Due</th>',
        '<th style="width:48px">Age</th>',
        '<th style="width:90px">Link</th>',
        '<th style="width:120px">Status</th>',
    ]
    if show_why:
        headers.append('<th style="width:14%">Why</th>')
    return (
        f'<div class="section-header" style="border-left-color:{color}">{h(label)}</div>\n'
        f'<table><thead><tr>{"".join(headers)}</tr></thead><tbody>\n'
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
        f'<div class="section-header" style="border-left-color:{color}">{h(label)}</div>\n'
        f'<div class="cmp-section">{"".join(rows)}</div>\n'
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
        f'<div class="section-header" style="border-left-color:{color}">{h(title)}</div>\n'
        f'<table><thead><tr>'
        f'<th style="width:2%">#</th>'
        f'<th>Task</th>'
        f'<th style="width:9%">Link</th>'
        f'<th style="width:7%">Status</th>'
        f'</tr></thead><tbody>\n'
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
        f'<div class="section-header" style="border-left-color:{color}">Completed today</div>\n'
        f'<table><thead><tr>'
        f'<th style="width:2%">#</th>'
        f'<th>Task</th>'
        f'<th style="width:9%">Link</th>'
        f'<th style="width:5%">Time</th>'
        f'</tr></thead><tbody>\n'
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
    cells = []
    for d, v in zip(days, values):
        is_today = d == today
        if v == 0:
            cell_cls = "spark-cell empty"
            cell_style = ""
        else:
            intensity = v / peak
            # Map intensity 0..1 to alpha bands so a 1-task day still reads as "something happened"
            alpha_bg = 0.18 + intensity * 0.55
            alpha_brd = 0.30 + intensity * 0.35
            cell_cls = "spark-cell"
            cell_style = (
                f' style="background:rgba(63,185,80,{alpha_bg:.2f});'
                f'border-color:rgba(63,185,80,{alpha_brd:.2f})"'
            )
        if is_today:
            cell_cls += " today"
        cells.append(
            f'<div class="spark-col">'
            f'<div class="{cell_cls}"{cell_style}>{v if v else ""}</div>'
            f'<div class="spark-label">'
            f'<span class="spark-day">{weekday_abbr[d.weekday()]}</span>'
            f'<span class="spark-date">{d.day}</span>'
            f'</div>'
            f'</div>'
        )
    color = SECTION_COLORS["completed today"]
    return (
        f'<div class="section-header" style="border-left-color:{color}">'
        f'Last 10 workdays <span class="spark-total">{total} done</span>'
        f'</div>\n'
        f'<div class="spark-grid">{"".join(cells)}</div>\n'
    )


def render_compact_completed(tasks):
    """Compact rendering for dashboard view: matches the Monitoring/Lower Priority style.
    The id cell uncompletes (handled by .cmp-id-done in the global click handler)."""
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
    return (
        f'<div class="section-header" style="border-left-color:{color}">Completed today</div>\n'
        f'<div class="cmp-section">{"".join(rows)}</div>\n'
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
    LEFT  = ("Today's Focus", "High Priority")
    RIGHT = ("Monitoring", "Lower Priority")
    CARD_VARIANTS = {"Today's Focus": "focus", "High Priority": "high-priority"}
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
    """Original single-column view: every section as a full table, stacked, no grid or counts strip."""
    parts = []
    for section in data.get("sections", []):
        stype = section.get("type", "core")
        title = section.get("title", "")
        tasks = section.get("tasks", [])
        if stype == "goalie":
            parts.append(render_goalie_section(title, tasks))
        else:
            parts.append(render_core_section(title, tasks, week))
    parts.append(render_completed(data.get("completed_today", [])))
    if data.get("updated"):
        parts.append(f'<p class="counts" style="margin-top:16px;color:#484f58">Updated {h(data["updated"])}</p>\n')
    return "".join(parts)


def _view_switcher_html(current, week=""):
    items = "".join(
        f'<a href="?view={v}" class="vs-btn{" active" if v == current else ""}">{v.title()}</a>'
        for v in VIEWS
    )
    week_badge = f'<span class="week-badge">{h(week)}</span>' if week else ""
    return (
        f'<div id="topbar">'
        f'<div id="view-switcher">{items}</div>'
        f'{week_badge}'
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
        f'<div id="ctx-menu">'
        f'<div class="ctx-header">Move to</div>'
        f'<div class="ctx-item" data-section="Today\u0027s Focus">Today\u0027s Focus</div>'
        f'<div class="ctx-item" data-section="Monitoring">Monitoring</div>'
        f'<div class="ctx-item" data-section="High Priority">High Priority</div>'
        f'<div class="ctx-item" data-section="Lower Priority">Lower Priority</div>'
        f'<div class="ctx-divider"></div>'
        f'<div class="ctx-item danger" data-action="cancel">Cancel task</div>'
        f'</div>'
        f'<script>{SCRIPT}</script>'
        f'</body></html>'
    )

# ---------------------------------------------------------------------------
# Core file update
# ---------------------------------------------------------------------------

def current_core_path():
    today = datetime.date.today()
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

def update_core_file(task_name, new_status, now):
    """Update the task's state marker in the core markdown file."""
    core_path = current_core_path()
    if not core_path.exists():
        return

    lines = core_path.read_text().split("\n")
    today_str = now.strftime("%Y-%m-%d")
    ts = now.strftime("%Y-%m-%d %H:%M")

    # Find ## Done boundary
    done_section = next((i for i, l in enumerate(lines) if l.strip() == "## Done"), len(lines))

    # Find the task line in the active area
    task_idx = None
    for i, line in enumerate(lines[:done_section]):
        if line.strip().startswith("- [") and task_name.lower() in line.lower():
            task_idx = i
            break

    if task_idx is None:
        return

    original = lines[task_idx]

    if new_status == "done":
        # Mark done + append timestamp
        updated = re.sub(r"\[.\]", "[x]", original, count=1).rstrip()
        if "_(completed:" not in updated:
            updated += f" _(completed: {ts})_"
        # Remove from active list
        lines.pop(task_idx)
        # Recalculate done_section index after pop
        done_section = next((i for i, l in enumerate(lines) if l.strip() == "## Done"), None)
        if done_section is None:
            lines += ["", "## Done", "", f"### {today_str}", updated]
        else:
            # Insert after ## Done, under today's heading (newest first)
            ins = done_section + 1
            if ins < len(lines) and lines[ins] == "":
                ins += 1
            if ins < len(lines) and lines[ins] == f"### {today_str}":
                lines.insert(ins + 1, updated)
            else:
                lines.insert(done_section + 1, updated)
                lines.insert(done_section + 1, f"### {today_str}")
                lines.insert(done_section + 1, "")
    else:
        marker = STATE_MARKER.get(new_status, "[ ]")
        lines[task_idx] = re.sub(r"\[.\]", marker, original, count=1)

    core_path.write_text("\n".join(lines))

def update_core_file_priority(task_name, new_pri, now):
    """Swap the priority emoji on the task line in the core markdown file."""
    core_path = current_core_path()
    if not core_path.exists():
        return
    lines = core_path.read_text().split("\n")
    done_section = next((i for i, l in enumerate(lines) if l.strip() == "## Done"), len(lines))
    all_emojis = set(PRI_EMOJI.values())
    new_emoji = PRI_EMOJI.get(new_pri)
    for i, line in enumerate(lines[:done_section]):
        if line.strip().startswith("- [") and task_name.lower() in line.lower():
            has_emoji = any(e in line for e in all_emojis)
            if new_emoji:
                if has_emoji:
                    for e in all_emojis:
                        if e in line:
                            lines[i] = line.replace(e, new_emoji, 1)
                            break
                else:
                    # Insert emoji after "- [X] "
                    lines[i] = re.sub(r"(- \[.\] )", rf"\1{new_emoji} ", line, count=1)
            else:
                # Remove emoji (cycling to null)
                for e in all_emojis:
                    if e in line:
                        lines[i] = line.replace(e + " ", "", 1)
                        break
            break
    core_path.write_text("\n".join(lines))


def apply_priority_update(task_id):
    """Cycle the priority of the task with the given stable id."""
    try:
        data = json.loads(JSON_FILE.read_text())
    except Exception:
        return False
    now = datetime.datetime.now()
    task, _ = find_task_by_id(data, task_id)
    if not task:
        return False
    new_pri = PRI_CYCLE.get(task.get("pri"))
    task["pri"] = new_pri
    data["updated"] = now.strftime("%Y-%m-%d %H:%M")
    JSON_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    update_core_file_priority(task.get("task", ""), new_pri, now)
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
    max_id = max(
        [t.get("id", 0) for s in data.get("sections", []) for t in s.get("tasks", [])] +
        [t.get("id", 0) for t in data.get("completed_today", [])] +
        [0]
    )
    return max_id + 1

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
    core_path = current_core_path()
    if not core_path.exists():
        return
    lines = core_path.read_text().split("\n")
    done_idx = next((i for i, l in enumerate(lines) if l.strip() == "## Done"), len(lines))

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
    try:
        data = json.loads(JSON_FILE.read_text())
    except Exception:
        return False
    pri_order = {"P1": 1, "P2": 2, "P3": 3, "P4": 4, "P5": 5, None: 6}
    sections = data.get("sections", [])

    high  = next((s for s in sections if s.get("title") == "High Priority"),  None)
    lower = next((s for s in sections if s.get("title") == "Lower Priority"), None)

    if high and lower:
        pool = high.get("tasks", []) + lower.get("tasks", [])
        high["tasks"]  = sorted([t for t in pool if t.get("pri") in ("P1", "P2")],
                                 key=lambda t: pri_order.get(t.get("pri"), 6))
        lower["tasks"] = sorted([t for t in pool if t.get("pri") not in ("P1", "P2")],
                                 key=lambda t: pri_order.get(t.get("pri"), 6))

    # Sort all other sections internally
    for s in sections:
        if s.get("title") not in ("High Priority", "Lower Priority"):
            s["tasks"] = sorted(s.get("tasks", []),
                                 key=lambda t: pri_order.get(t.get("pri"), 6))

    renumber_tasks(data)
    data["updated"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    JSON_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    sync_core_file_order(data)
    return True


def apply_uncomplete(num):
    """Move a completed task back to active. Reads priority from core file to pick section."""
    try:
        data = json.loads(JSON_FILE.read_text())
    except Exception:
        return False

    now = datetime.datetime.now()

    # Find in completed_today by id
    task = next((t for t in data.get("completed_today", []) if t.get("id") == num), None)
    if not task:
        return False

    task_name = task.get("task", "")

    # Read core file to get priority and reconstruct the active line
    core_path = current_core_path()
    pri = None
    if core_path.exists():
        lines = core_path.read_text().split("\n")
        done_section = next((i for i, l in enumerate(lines) if l.strip() == "## Done"), len(lines))
        emoji_to_pri = {v: k for k, v in PRI_EMOJI.items()}
        done_line_idx = None
        for i, line in enumerate(lines[done_section:], done_section):
            if line.strip().startswith("- [x]") and task_name.lower() in line.lower():
                done_line_idx = i
                for emoji, p in emoji_to_pri.items():
                    if emoji in line:
                        pri = p
                        break
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
            done_section = next((i for i, l in enumerate(lines) if l.strip() == "## Done"), len(lines))
            lines.insert(done_section, restored)
            core_path.write_text("\n".join(lines))

    # Remove from completed_today
    data["completed_today"] = [t for t in data["completed_today"] if t.get("num") != num]

    # Decrement counts
    counts = data.get("counts", "")
    today_str = now.strftime("%Y-%m-%d")
    m = re.search(r"(\d+) core tasks completed this week", counts)
    if m and int(m.group(1)) > 0:
        n = int(m.group(1)) - 1
        counts = re.sub(r"\d+ core tasks completed this week", f"{n} core tasks completed this week", counts)
        counts = re.sub(rf"(\d+) on {today_str}", lambda x: str(int(x.group(1)) - 1) + f" on {today_str}", counts)
        counts = re.sub(r"\b0 on [0-9-]+ · ", "", counts)
        counts = re.sub(r" · 0 on [0-9-]+", "", counts)
        data["counts"] = counts

    # Add back to JSON — pick section by priority
    new_task = {
        "num": num,
        "pri": pri,
        "task": task_name,
        "due": "—",
        "from": "—",
        "links": task.get("links", []),
        "status": "open",
        "why": "—",
    }
    target_title = "High Priority" if pri in ("P1", "P2") else "Lower Priority"
    target = next((s for s in data.get("sections", []) if s.get("title") == target_title), None)
    if target is None:
        target = next((s for s in data.get("sections", []) if s.get("type", "core") == "core"), None)
    if target:
        target["tasks"].append(new_task)

    data["updated"] = now.strftime("%Y-%m-%d %H:%M")
    JSON_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return True


def apply_status_change(num, force_status=None):
    """Update the status of task `num`. Cycles if force_status is None, else sets explicitly."""
    try:
        data = json.loads(JSON_FILE.read_text())
    except Exception:
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
        })
        # Update counts
        counts = data.get("counts", "")
        today_str = now.strftime("%Y-%m-%d")
        m = re.search(r"(\d+) core tasks completed this week", counts)
        if m:
            n = int(m.group(1)) + 1
            counts = re.sub(r"\d+ core tasks completed this week", f"{n} core tasks completed this week", counts)
            if today_str not in counts:
                counts = re.sub(r"\(", f"(1 on {today_str} · ", counts)
            else:
                counts = re.sub(rf"(\d+) on {today_str}", lambda x: f"{int(x.group(1))+1} on {today_str}", counts)
            data["counts"] = counts
    else:
        task["status"] = new_status

    data["updated"] = now.strftime("%Y-%m-%d %H:%M")
    JSON_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    update_core_file(task_name, new_status, now)
    return True

def apply_cancel(task_id):
    """Cancel a task: remove from active JSON, mark [/] in core file, move to ## Cancelled."""
    try:
        data = json.loads(JSON_FILE.read_text())
    except Exception:
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
    core_path = current_core_path()
    if core_path.exists():
        lines = core_path.read_text().split("\n")
        done_section = next((i for i, l in enumerate(lines) if l.strip() == "## Done"), len(lines))
        task_idx = next(
            (i for i, line in enumerate(lines[:done_section])
             if line.strip().startswith("- [") and task_name.lower() in line.lower()),
            None,
        )
        if task_idx is not None:
            original = lines[task_idx]
            updated = re.sub(r"\[.\]", "[/]", original, count=1).rstrip()
            if "_(cancelled:" not in updated:
                updated += f" _(cancelled: {ts})_"
            lines.pop(task_idx)

            cancelled_section = next((i for i, l in enumerate(lines) if l.strip() == "## Cancelled"), None)
            if cancelled_section is None:
                # Create the section after ## Done if it exists, else at end of file
                done_idx = next((i for i, l in enumerate(lines) if l.strip() == "## Done"), None)
                if done_idx is None:
                    lines += ["", "## Cancelled", "", f"### {today_str}", updated]
                else:
                    after_done = next(
                        (i for i, l in enumerate(lines[done_idx + 1:], done_idx + 1) if l.startswith("## ")),
                        len(lines),
                    )
                    block = ["", "## Cancelled", "", f"### {today_str}", updated]
                    for j, item in enumerate(block):
                        lines.insert(after_done + j, item)
            else:
                ins = cancelled_section + 1
                if ins < len(lines) and lines[ins] == "":
                    ins += 1
                if ins < len(lines) and lines[ins] == f"### {today_str}":
                    lines.insert(ins + 1, updated)
                else:
                    lines.insert(cancelled_section + 1, updated)
                    lines.insert(cancelled_section + 1, f"### {today_str}")
                    lines.insert(cancelled_section + 1, "")

        core_path.write_text("\n".join(lines))

    # If the cancelled task was in Today's Focus, snapshot the new focus list to journal
    if src_title == "Today's Focus":
        focus = next((s for s in data.get("sections", []) if s.get("title") == "Today's Focus"), None)
        if focus is not None:
            set_today_focus([t.get("task", "") for t in focus.get("tasks", [])])

    renumber_tasks(data)
    data["updated"] = ts
    JSON_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return True


def apply_move_section(task_id, target_title):
    """Move a task by id to a named section (appending at end), with the same
    side effects as drag-and-drop: priority bumps, status flips, focus journal updates."""
    try:
        data = json.loads(JSON_FILE.read_text())
    except Exception:
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

    # Remove from source
    src_section["tasks"] = [t for t in src_section["tasks"] if t.get("id") != task_id]

    # Priority bump for High/Lower Priority crossings
    if tgt_title == "High Priority" and src_task.get("pri") not in ("P1", "P2"):
        src_task["pri"] = "P2"
        update_core_file_priority(src_task.get("task", ""), "P2", now)
    elif tgt_title == "Lower Priority" and src_task.get("pri") in ("P1", "P2"):
        src_task["pri"] = "P3"
        update_core_file_priority(src_task.get("task", ""), "P3", now)

    # Monitoring entry/exit flips state marker
    if tgt_title == "Monitoring" and src_task.get("status") != "waiting":
        src_task["status"] = "waiting"
        update_core_file(src_task.get("task", ""), "waiting", now)
    elif src_title == "Monitoring" and tgt_title != "Monitoring":
        src_task["status"] = "open"
        update_core_file(src_task.get("task", ""), "open", now)

    tgt_section["tasks"].append(src_task)
    renumber_tasks(data)
    data["updated"] = now.strftime("%Y-%m-%d %H:%M")
    JSON_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    sync_core_file_order(data)

    if "Today's Focus" in (src_title, tgt_title):
        focus = next((s for s in data.get("sections", []) if s.get("title") == "Today's Focus"), None)
        if focus is not None:
            set_today_focus([t.get("task", "") for t in focus.get("tasks", [])])

    return True


def apply_reorder(from_num, to_num, before=True):
    """Move task (by stable id) to before/after another task. Updates priority when crossing sections."""
    try:
        data = json.loads(JSON_FILE.read_text())
    except Exception:
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

    # Cross-section side effects
    now = datetime.datetime.now()
    src_title = src_section.get("title")
    tgt_title = tgt_section.get("title")
    if src_section is not tgt_section:
        # Priority bumps for High/Lower Priority moves
        if tgt_title == "High Priority" and src_task.get("pri") not in ("P1", "P2"):
            src_task["pri"] = "P2"
            update_core_file_priority(src_task.get("task", ""), "P2", now)
        elif tgt_title == "Lower Priority" and src_task.get("pri") in ("P1", "P2"):
            src_task["pri"] = "P3"
            update_core_file_priority(src_task.get("task", ""), "P3", now)

        # Monitoring entry/exit flips the [~] / [ ] marker
        if tgt_title == "Monitoring" and src_task.get("status") != "waiting":
            src_task["status"] = "waiting"
            update_core_file(src_task.get("task", ""), "waiting", now)
        elif src_title == "Monitoring" and tgt_title != "Monitoring":
            src_task["status"] = "open"
            update_core_file(src_task.get("task", ""), "open", now)

    insert_pos = tgt_idx if before else tgt_idx + 1
    tgt_section["tasks"].insert(insert_pos, src_task)
    renumber_tasks(data)
    data["updated"] = now.strftime("%Y-%m-%d %H:%M")
    JSON_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    sync_core_file_order(data)

    # If the drag involved Today's Focus, snapshot the new focus list to the journal.
    # This converts auto-derived focus to explicit so the change survives `tk` rebuilds.
    if "Today's Focus" in (src_title, tgt_title):
        focus = next((s for s in data.get("sections", []) if s.get("title") == "Today's Focus"), None)
        if focus is not None:
            set_today_focus([t.get("task", "") for t in focus.get("tasks", [])])
    return True


def add_to_core_file(name, pri, due, why, links):
    """Insert a new active task line before ## Done in the core file."""
    core_path = current_core_path()
    if not core_path.exists():
        return
    lines = core_path.read_text().split("\n")
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
    done_section = next((i for i, l in enumerate(lines) if l.strip() == "## Done"), len(lines))
    lines.insert(done_section, task_line)
    core_path.write_text("\n".join(lines))


def apply_add(task_data):
    """Add a new task to JSON and core file."""
    try:
        data = json.loads(JSON_FILE.read_text())
    except Exception:
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

    target_title = "High Priority" if pri in ("P1", "P2") else "Lower Priority"
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
    data["updated"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    JSON_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    add_to_core_file(name, pri, due, why, links)
    return True


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
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
            combined_mtime = max(json_mtime, src_mtime)
            etag = f'"{combined_mtime:.6f}-{view}"'
            last_modified = email.utils.formatdate(int(combined_mtime), usegmt=True)
        except Exception:
            combined_mtime = None
            etag = None
            last_modified = None

        if etag is not None:
            inm = self.headers.get("If-None-Match")
            if inm and inm == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                if last_modified:
                    self.send_header("Last-Modified", last_modified)
                self.end_headers()
                return

        try:
            data = json.loads(JSON_FILE.read_text())
            page = build_page(data, view=view)
        except Exception as e:
            page = f"<pre style='color:#f85149;padding:16px'>Error: {e}</pre>"

        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        if etag:
            self.send_header("ETag", etag)
        if last_modified:
            self.send_header("Last-Modified", last_modified)
        self.end_headers()
        self.wfile.write(page.encode())

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        if self.path == "/update":
            ok = apply_status_change(body.get("id"))
        elif self.path == "/complete":
            ok = apply_status_change(body.get("id"), force_status="done")
        elif self.path == "/update-pri":
            ok = apply_priority_update(body.get("id"))
        elif self.path == "/uncomplete":
            ok = apply_uncomplete(body.get("id"))
        elif self.path == "/sort":
            ok = apply_sort()
        elif self.path == "/reorder":
            ok = apply_reorder(body.get("from"), body.get("to"), body.get("before", True))
        elif self.path == "/move-section":
            ok = apply_move_section(body.get("id"), body.get("section"))
        elif self.path == "/cancel":
            ok = apply_cancel(body.get("id"))
        elif self.path == "/add":
            ok = apply_add(body)
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200 if ok else 400)
        self.end_headers()

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    print(f"Tasks at http://localhost:{PORT}")
    _server = http.server.ThreadingHTTPServer(("", PORT), Handler)
    _server.socket.set_inheritable(False)
    _server.serve_forever()
