"""Smoke tests for serve-tasks.py.

Goal: catch the class of bug where one render path or click-target convention
gets out of sync with the others (the "compact-row complete is broken because
the handler used closest('tr')" class). These are not exhaustive — they are
trip-wires that fire when an obvious invariant gets violated.

Run: SERVE_TASKS_NO_WATCH=1 python3 -m pytest test_serve_tasks.py -q
"""
import importlib.util
import json
import os
import pathlib
import re
import time as time_module
from pathlib import Path

# Suppress the daemon threads (auto-restart + SSE notifier) before import.
os.environ.setdefault("SERVE_TASKS_NO_WATCH", "1")

import pytest

SCRIPT = Path(__file__).parent / "serve-tasks.py"
spec = importlib.util.spec_from_file_location("st", SCRIPT)
st = importlib.util.module_from_spec(spec)
spec.loader.exec_module(st)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _fixture_data():
    """A deliberately mixed JSON: at least one task per dashboard column type
    plus one completed entry. Exercises every render path."""
    return {
        "updated": "2026-04-28 12:00",
        "week": "W18",
        "sections": [
            {
                "title": "Today's Focus",
                "type": "core",
                "tasks": [
                    {"id": 101, "num": 1, "pri": "P1", "task": "Focus task one",
                     "due": "—", "from": "W18", "added": "2026-04-28",
                     "links": [], "status": "in_progress", "why": "—"},
                ],
            },
            {
                "title": "Monitoring",
                "type": "core",
                "tasks": [
                    {"id": 110, "num": 2, "pri": "P3", "task": "Monitoring item",
                     "due": "—", "from": "W17", "added": "2026-04-21",
                     "links": [{"label": "doc", "url": "https://example.com/doc"}],
                     "status": "waiting", "why": "watching for X"},
                ],
            },
            {
                "title": "High Priority",
                "type": "core",
                "tasks": [
                    {"id": 120, "num": 3, "pri": "P2", "task": "High prio task",
                     "due": "17:00", "from": "W18", "added": "2026-04-28",
                     "links": [], "status": "open", "why": "—"},
                ],
            },
            {
                "title": "Lower Priority",
                "type": "core",
                "tasks": [
                    {"id": 130, "num": 4, "pri": "P4", "task": "Lower prio task",
                     "due": "—", "from": "W18", "added": "2026-04-28",
                     "links": [], "status": "open", "why": "—"},
                ],
            },
        ],
        "completed_today": [
            {"id": 200, "num": 5, "task": "Already done task",
             "links": [{"label": "ref", "url": "https://example.com/ref"}],
             "time": "11:30", "from_section": "High Priority"},
        ],
    }


@pytest.fixture
def data():
    return _fixture_data()


SEED_CORE = """\
# 2026-W18 Core

## Active

- [-] 🔴 Focus task one
- [~] 🟡 Monitoring item ([doc](https://example.com/doc))
- [ ] 🟠 High prio task — due 17:00
- [ ] 🔵 Lower prio task

## Done

### 2026-04-28
- [x] 🟠 Already done task ([ref](https://example.com/ref)) _(completed: 2026-04-28 11:30)_
"""


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Point JSON_FILE and current_core_path at temp files seeded with the
    fixture. Mutating endpoints can be exercised without touching the user's
    real ~/todo/tasks-live.json or ~/todo/journal/."""
    json_path = tmp_path / "tasks-live.json"
    json_path.write_text(json.dumps(_fixture_data(), indent=2))
    monkeypatch.setattr(st, "JSON_FILE", json_path)

    core_path = tmp_path / "core.md"
    core_path.write_text(SEED_CORE)
    monkeypatch.setattr(st, "current_core_path", lambda week=None: core_path)

    return json_path


# ---------------------------------------------------------------------------
# Render-time invariants
# ---------------------------------------------------------------------------

def test_renders_dashboard(data):
    html = st.build_page(data, view="dashboard")
    assert "<html>" in html or "<!DOCTYPE html>" in html
    assert len(html) > 1000


def test_renders_classic(data):
    html = st.build_page(data, view="classic")
    assert "<html>" in html or "<!DOCTYPE html>" in html
    assert len(html) > 1000


def _click_targets_for_task(html, task_id, *, done=False):
    """Return all click targets in `html` carrying data-id=task_id.
    Looks for both table-style (`td.num` / `td.num-done`) and compact-style
    (`.cmp-id` / `.cmp-id-done`) targets — any one of them is enough."""
    if done:
        pattern = (
            rf'(?:class="[^"]*\b(?:num-done|cmp-id-done)\b[^"]*"\s+data-id="{task_id}"'
            rf'|data-id="{task_id}"\s+class="[^"]*\b(?:num-done|cmp-id-done)\b[^"]*")'
        )
    else:
        pattern = (
            rf'(?:class="[^"]*\b(?:num|cmp-id)\b(?![-])[^"]*"\s+data-id="{task_id}"'
            rf'|data-id="{task_id}"\s+class="[^"]*\b(?:num|cmp-id)\b(?![-])[^"]*")'
        )
    return re.findall(pattern, html)


@pytest.mark.parametrize("view", ["dashboard", "classic"])
def test_every_active_task_has_a_clickable_complete_target(data, view):
    """The bug we shipped: compact rows in dashboard view used .cmp-id
    but the click handler did closest('tr') — silent failure. This trips
    when any active task is rendered without a working complete target."""
    html = st.build_page(data, view=view)
    missing = []
    for s in data["sections"]:
        for t in s["tasks"]:
            if not _click_targets_for_task(html, t["id"]):
                missing.append((s["title"], t["id"], t["task"]))
    assert not missing, f"active tasks missing complete-click target: {missing}"


@pytest.mark.parametrize("view", ["dashboard", "classic"])
def test_every_completed_task_has_a_clickable_uncomplete_target(data, view):
    html = st.build_page(data, view=view)
    missing = []
    for t in data["completed_today"]:
        if not _click_targets_for_task(html, t["id"], done=True):
            missing.append((t["id"], t["task"]))
    assert not missing, f"completed tasks missing uncomplete target: {missing}"


def test_completed_rows_have_detail_panel_dashboard(data):
    """Bug #4: compact completed rows missed the cmp-detail sibling so
    click-task-name to expand did nothing."""
    html = st.build_page(data, view="dashboard")
    for t in data["completed_today"]:
        assert f'cmp-detail" data-id="{t["id"]}"' in html, (
            f"completed task id={t['id']} missing detail panel"
        )


def test_active_compact_rows_have_drag_attr_dashboard(data):
    """Drag-to-reorder needs draggable=true on every active compact row."""
    html = st.build_page(data, view="dashboard")
    for title in ("Monitoring", "Lower Priority"):
        section = next(s for s in data["sections"] if s["title"] == title)
        for t in section["tasks"]:
            row_pattern = rf'<div class="cmp-row[^"]*"\s+draggable="true"\s+data-id="{t["id"]}"'
            assert re.search(row_pattern, html), (
                f"active compact row id={t['id']} not draggable"
            )


def test_ctx_menu_has_mark_as_done(data):
    """Bug #3: the right-click menu had no /complete option."""
    html = st.build_page(data, view="dashboard")
    assert 'data-action="complete"' in html
    assert "Mark as done" in html


# ---------------------------------------------------------------------------
# Refresh-after-mutation invariants
# ---------------------------------------------------------------------------

# Every mutating route the server accepts. Each must trigger a view refresh
# (`_refreshTasks(true)`) so the dashboard never lies.
MUTATING_ROUTES = [
    "/update", "/complete", "/update-pri", "/uncomplete",
    "/sort", "/reorder", "/move-section", "/cancel", "/add",
]


def test_why_column_truncates_until_row_expanded(data):
    """Tables truncate Why to one line by default and reveal full text
    when the row gets `.expanded` class (set by clicking the task cell).
    Mirrors the compact-row click-to-expand pattern."""
    # Give the High Priority task a real `why` so show_why fires for the
    # table render. (Default fixture only has a why on a compact-section task.)
    high = next(s for s in data["sections"] if s["title"] == "High Priority")
    high["tasks"][0]["why"] = "needs to ship before Friday"
    html = st.build_page(data, view="dashboard")
    # Why cell is rendered with class so CSS can target it
    assert 'class="why"' in html, "Why cell missing the .why class"
    # Task cell is rendered with class so click handler can target it
    assert 'class="task-cell"' in html, "Task cell missing the .task-cell class"
    # CSS truncates by default
    assert "td.why { white-space: nowrap" in html or \
        re.search(r"td\.why\s*\{[^}]*white-space:\s*nowrap", html), (
        "Why column doesn't truncate by default"
    )
    # Expanded row reveals it
    assert "tr.expanded td.why" in html, "No expanded-row rule for Why"
    # Click handler toggles .expanded
    assert "td.task-cell" in html and "classList.toggle('expanded')" in html, (
        "Click handler missing for task-cell expand"
    )


def test_post_helper_refreshes_after_response(data):
    """The shared `_post` helper must call `_refreshTasks(true)` after the
    response. This is the load-bearing invariant for every call site."""
    html = st.build_page(data, view="dashboard")
    idx = html.find("function _post(")
    assert idx != -1, "_post helper not found in served JS"
    # Look at the next ~500 chars for the body
    chunk = html[idx:idx + 500]
    assert "_refreshTasks(true)" in chunk, f"_post doesn't refresh:\n{chunk}"


@pytest.mark.parametrize("route", MUTATING_ROUTES)
def test_every_mutating_route_referenced_in_client(data, route):
    """Every mutating route the server accepts must have at least one
    client-side trigger. Reference can be `_post('<route>', …)`,
    `endpoint = '<route>'` (variable-routed via ctx menu), or a bare
    `fetch('<route>', …)`. Catches dead routes and accidental typos."""
    html = st.build_page(data, view="dashboard")
    patterns = [
        rf"_post\('{re.escape(route)}'",
        rf"endpoint\s*=\s*'{re.escape(route)}'",
        rf"fetch\('{re.escape(route)}'",
    ]
    assert any(re.search(p, html) for p in patterns), (
        f"route {route} not referenced anywhere in client JS"
    )


@pytest.mark.parametrize("route", MUTATING_ROUTES)
def test_no_bare_fetch_to_mutating_route_bypasses_refresh(data, route):
    """Any bare `fetch('<route>', …)` (not via `_post`) must call
    `_refreshTasks(true)` on its own success path — bypassing `_post`
    means bypassing the refresh-on-success guarantee. A refresh gated
    only by `if (!r.ok)` doesn't count (failure-only)."""
    html = st.build_page(data, view="dashboard")
    error_only_re = re.compile(r"if\s*\(\s*!r\.ok\s*\)\s*_refreshTasks\(true\)")
    for match in re.finditer(rf"fetch\('?{re.escape(route)}", html):
        chunk = html[match.start():match.start() + 400]
        total = chunk.count("_refreshTasks(true)")
        error_only = len(error_only_re.findall(chunk))
        assert total > 0, (
            f"bare fetch to {route} bypasses _post and doesn't refresh:\n{chunk}"
        )
        assert total > error_only, (
            f"bare fetch to {route} only refreshes on failure:\n{chunk}"
        )


def test_apply_status_change_bumps_json_mtime(isolated_state):
    """SSE notifies clients via JSON mtime change. Each apply_* must move
    the mtime forward, otherwise the SSE push doesn't fire."""
    before = isolated_state.stat().st_mtime
    time_module.sleep(0.01)  # ensure mtime resolution sees the change
    assert st.apply_status_change(120)
    after = isolated_state.stat().st_mtime
    assert after > before


def test_apply_priority_update_bumps_json_mtime(isolated_state):
    before = isolated_state.stat().st_mtime
    time_module.sleep(0.01)
    assert st.apply_priority_update(120)
    after = isolated_state.stat().st_mtime
    assert after > before


def test_apply_uncomplete_bumps_json_mtime(isolated_state):
    before = isolated_state.stat().st_mtime
    time_module.sleep(0.01)
    assert st.apply_uncomplete(200)
    after = isolated_state.stat().st_mtime
    assert after > before


def test_apply_sort_bumps_json_mtime(isolated_state):
    before = isolated_state.stat().st_mtime
    time_module.sleep(0.01)
    assert st.apply_sort()
    after = isolated_state.stat().st_mtime
    assert after > before


def test_apply_reorder_bumps_json_mtime(isolated_state):
    before = isolated_state.stat().st_mtime
    time_module.sleep(0.01)
    # Swap 120 and 130 (both active)
    assert st.apply_reorder(120, 130, before=True)
    after = isolated_state.stat().st_mtime
    assert after > before


def test_apply_move_section_bumps_json_mtime(isolated_state):
    before = isolated_state.stat().st_mtime
    time_module.sleep(0.01)
    assert st.apply_move_section(130, "High Priority")
    after = isolated_state.stat().st_mtime
    assert after > before


def test_apply_cancel_bumps_json_mtime(isolated_state):
    before = isolated_state.stat().st_mtime
    time_module.sleep(0.01)
    assert st.apply_cancel(120)
    after = isolated_state.stat().st_mtime
    assert after > before


def test_apply_add_bumps_json_mtime(isolated_state):
    before = isolated_state.stat().st_mtime
    time_module.sleep(0.01)
    assert st.apply_add({"task": "Newly added", "pri": "P3"})
    after = isolated_state.stat().st_mtime
    assert after > before


def test_response_sets_cache_control_no_cache(data):
    """Regression: without `Cache-Control: no-cache`, Chrome heuristically
    cached the GET and served stale bodies after a POST mutation. The
    DOM-swap appeared to succeed (200 from cache) but showed old state."""
    # We can't easily intercept the actual HTTP response in-test, so
    # instead spot-check that the do_GET handler sets the header.
    src = pathlib.Path(__file__).parent / "serve-tasks.py"
    text = src.read_text()
    assert text.count('send_header("Cache-Control", "no-cache")') >= 2, (
        "expected Cache-Control: no-cache on both 304 and 200 responses"
    )


# ---------------------------------------------------------------------------
# State mutation invariants
# ---------------------------------------------------------------------------

def test_complete_records_from_section(isolated_state):
    """apply_status_change(force_status='done') should record the source
    section title so the completed-row detail panel can display it."""
    ok = st.apply_status_change(120, force_status="done")
    assert ok
    data = json.loads(isolated_state.read_text())
    entry = next(t for t in data["completed_today"] if t["id"] == 120)
    assert entry["from_section"] == "High Priority"


def test_uncomplete_filters_by_id_not_num(isolated_state):
    """Regression: apply_uncomplete used to filter completed_today by `num`
    instead of `id`; after a sort, removing the wrong row was possible.
    Specifically, an entry whose `num` differs from its `id` must still get
    cleanly removed when uncompleted."""
    # Set up: completed entry where id != num (id=200, num=5 in fixture)
    ok = st.apply_uncomplete(200)
    assert ok
    data = json.loads(isolated_state.read_text())
    # Removed from completed
    assert all(t["id"] != 200 for t in data["completed_today"])
    # Restored to a section, with id intact
    restored = [t for s in data["sections"] for t in s["tasks"] if t.get("id") == 200]
    assert len(restored) == 1


def test_uncomplete_preserves_id_field(isolated_state):
    """The restored task must carry its stable id forward."""
    st.apply_uncomplete(200)
    data = json.loads(isolated_state.read_text())
    restored = [t for s in data["sections"] for t in s["tasks"] if t.get("id") == 200]
    assert restored
    assert restored[0]["id"] == 200


def test_status_cycle_updates_status(isolated_state):
    ok = st.apply_status_change(120)  # was "open"
    assert ok
    data = json.loads(isolated_state.read_text())
    task = next(t for s in data["sections"] for t in s["tasks"] if t["id"] == 120)
    assert task["status"] == "in_progress"  # open → in_progress per STATUS_CYCLE


def test_priority_cycle_advances_priority(isolated_state):
    ok = st.apply_priority_update(120)  # was "P2"
    assert ok
    data = json.loads(isolated_state.read_text())
    task = next(t for s in data["sections"] for t in s["tasks"] if t["id"] == 120)
    assert task["pri"] == "P3"  # P2 → P3 per PRI_CYCLE


def test_uncomplete_renders_back_in_a_section(isolated_state):
    """Bug #5 regression — the user-visible bug. After uncompleting, the next
    render must show the task as active (with a complete-target) and NOT in
    completed_today (no uncomplete-target)."""
    assert st.apply_uncomplete(200)
    data = json.loads(isolated_state.read_text())
    html = st.build_page(data, view="dashboard")
    assert _click_targets_for_task(html, 200), "uncompleted task not rendered as active"
    assert not _click_targets_for_task(html, 200, done=True), "still rendered in completed_today"


def test_complete_then_uncomplete_round_trip(isolated_state):
    """Round trip: completing then uncompleting should put the task back
    where it can be found by id and have a sensible section."""
    assert st.apply_status_change(120, force_status="done")
    assert st.apply_uncomplete(120)
    data = json.loads(isolated_state.read_text())
    restored = [t for s in data["sections"] for t in s["tasks"] if t.get("id") == 120]
    assert len(restored) == 1


# ---------------------------------------------------------------------------
# Helper-level invariants
# ---------------------------------------------------------------------------

def test_find_task_line_exact_match():
    """find_task_line must match only the exact name slot, not a substring.
    (The other big risk path: silent JSON↔markdown divergence.)"""
    lines = [
        "## Active",
        "- [ ] 🟠 Add timeline/repo details to ELM migration tracking doc (link)",
        "- [ ] 🟠 Add context from Friday ELM meeting to ELM migration tracking doc (link)",
        "## Done",
    ]
    assert st.find_task_line(lines, "Add timeline/repo details to ELM migration tracking doc") == 1
    assert st.find_task_line(lines, "Add context from Friday ELM meeting to ELM migration tracking doc") == 2
    # Substring should NOT match
    assert st.find_task_line(lines, "ELM migration tracking doc") is None


def test_target_section_for_pri():
    assert st.target_section_for_pri("P1") == st.SEC_HIGH
    assert st.target_section_for_pri("P2") == st.SEC_HIGH
    assert st.target_section_for_pri("P3") == st.SEC_LOW
    assert st.target_section_for_pri(None) == st.SEC_LOW
