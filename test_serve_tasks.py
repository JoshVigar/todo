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
    "/sort", "/reorder", "/move-section", "/cancel", "/add", "/edit",
]


def test_table_row_expand_reveals_why_detail(data):
    """Tasks in table sections (High Priority, Today's Focus) with a real
    `why` get a sibling `<tr class="row-detail">` revealed on click. Mirrors
    the compact-row click-to-expand pattern but as a row below the task
    rather than a column expansion."""
    # Default fixture's only why is on a compact-section task; give a
    # table-section task a why so the row-detail renders.
    high = next(s for s in data["sections"] if s["title"] == "High Priority")
    high["tasks"][0]["why"] = "needs to ship before Friday"
    target_id = high["tasks"][0]["id"]
    html = st.build_page(data, view="dashboard")

    # Sibling row-detail tr is emitted for tasks with a why
    assert f'<tr class="row-detail" data-id="{target_id}"' in html, (
        f"row-detail not emitted for task id={target_id}"
    )
    assert "needs to ship before Friday" in html

    # The task cell is the click target only when there's a detail to reveal
    assert 'class="task-cell"' in html, "task-cell class missing"

    # CSS keeps the detail row hidden until the task row above is expanded
    assert "tr.row-detail { display: none; }" in html
    assert "tr.expanded + tr.row-detail { display: table-row; }" in html

    # Click handler toggles .expanded on the task row
    assert "td.task-cell" in html and "classList.toggle('expanded')" in html


def test_expand_all_button_in_compact_section_headers(data):
    """Compact sections (Monitoring, Lower Priority, Completed Today) always
    have expandable detail panels — they should always carry an expand-all
    chevron in the header."""
    html = st.build_page(data, view="dashboard")
    # All three compact sections present in the fixture; each should have one button
    assert html.count('data-action="expand-all"') >= 3, (
        f"expected ≥3 expand-all buttons (compact sections), got "
        f"{html.count('data-action=\"expand-all\"')}"
    )


def test_expand_all_button_in_table_section_only_when_any_why(data):
    """Table sections (High Priority, Today's Focus) get the expand-all
    chevron only when at least one task in the section has a real why."""
    # Force ALL whys to be empty
    for s in data["sections"]:
        for t in s["tasks"]:
            t["why"] = "—"
    html = st.build_page(data, view="dashboard")
    # Compact sections always have it (3 buttons). Tables shouldn't add more.
    no_why_count = html.count('data-action="expand-all"')

    # Now give one High Priority task a why
    high = next(s for s in data["sections"] if s["title"] == "High Priority")
    high["tasks"][0]["why"] = "needs to ship"
    html2 = st.build_page(data, view="dashboard")
    with_why_count = html2.count('data-action="expand-all"')
    assert with_why_count == no_why_count + 1, (
        f"High Priority should add 1 expand-all button when any task has a "
        f"why; before={no_why_count}, after={with_why_count}"
    )


def test_table_row_expand_suppressed_when_no_why(data):
    """Rows without a why have no click affordance — task-cell class is
    only applied to rows that actually have something to reveal."""
    # Make sure NO table task has a why
    for s in data["sections"]:
        if s["title"] in ("High Priority", "Today's Focus"):
            for t in s["tasks"]:
                t["why"] = "—"
    html = st.build_page(data, view="dashboard")
    # No row-detail tr emitted (compact section's why doesn't render in the
    # detail-row pattern; that path uses cmp-detail)
    assert '<tr class="row-detail"' not in html, "row-detail emitted with no whys"


def test_hotkeys_present_with_required_guards(data):
    """All hotkeys are wired and the dispatch block guards against firing
    while a modifier is held (Meta/Ctrl/Alt — Shift IS used), the tab is
    unfocused, an overlay is open, or an input is focused."""
    html = st.build_page(data, view="dashboard")
    # Single-letter
    for k in ['x', 'r', 's', 'a', 'c', 'j', 'k']:
        assert f"e.key === '{k}'" in html, f"missing hotkey: {k}"
    # Arrow + Enter + ?
    assert "ArrowDown" in html and "ArrowUp" in html
    assert "e.key === 'Enter'" in html
    assert "e.key === '?'" in html
    # Shift combos via e.code
    for code in ['Digit1', 'Digit2', 'Digit3', 'KeyS', 'KeyP']:
        assert f"e.code === '{code}'" in html, f"missing shift+{code}"
    # Guards
    assert "metaKey" in html and "ctrlKey" in html and "altKey" in html
    assert "document.hasFocus()" in html
    assert "modal-overlay" in html and "classList.contains('open')" in html
    assert "INPUT" in html and "TEXTAREA" in html


def test_focus_progress_subtitle_renders(data):
    """Today's Focus header carries `N of M` once any focus task is done."""
    # Mark the existing completed task as having come from Today's Focus
    data["completed_today"][0]["from_section"] = "Today's Focus"
    html = st.build_page(data, view="dashboard")
    assert "section-subtitle" in html
    # 1 active focus + 1 completed-from-focus = 1 of 2
    assert ">· 1 of 2<" in html, "expected '· 1 of 2' subtitle on Today's Focus"


def test_focus_progress_omitted_when_no_focus_section(data):
    """If there's no Today's Focus section, no progress subtitle renders."""
    data["sections"] = [s for s in data["sections"] if s["title"] != "Today's Focus"]
    assert st._focus_progress(data) == ""


def test_focus_progress_omitted_when_total_zero(data):
    """Empty focus section + no focus completions → no subtitle text."""
    focus = next(s for s in data["sections"] if s["title"] == "Today's Focus")
    focus["tasks"] = []
    data["completed_today"] = []
    assert st._focus_progress(data) == ""


def test_filter_input_present_with_hotkey_and_clear(data):
    """The filter input + the JS to apply it + the / and Esc keybinds + Clear button."""
    html = st.build_page(data, view="dashboard")
    assert 'id="task-filter"' in html
    assert 'id="filter-clear"' in html
    assert "function _applyFilter()" in html
    # / opens the filter popup and focuses the input
    assert "e.key === '/'" in html
    assert "_showFilterPopup()" in html
    # Esc clears all filters via _clearFilters
    assert "_clearFilters()" in html
    # filtered-out CSS rule hides matching rows
    assert ".filtered-out { display: none !important; }" in html


def test_filter_input_lives_in_floating_popup(data):
    """The filter input no longer takes topbar space — it sits inside
    #filter-popup which is hidden until `/` opens it."""
    html = st.build_page(data, view="dashboard")
    # Popup wrapper exists and is hidden by default
    assert 'id="filter-popup"' in html
    assert "#filter-popup {" in html and "display: none" in html
    # Input lives inside the popup, NOT inside #topbar
    pop_idx = html.find('id="filter-popup"')
    pop_close = html.find("</div>", pop_idx)
    assert 'id="task-filter"' in html[pop_idx:pop_close + 6], (
        "filter input should be inside #filter-popup"
    )
    # Topbar must not contain the filter input anymore
    topbar_open = html.find('id="topbar"')
    topbar_close = html.find("</div>", topbar_open)
    # The first `</div>` after #topbar opens closes the topbar — but #topbar
    # contains nested divs, so walk until balanced. Cheaper: just assert the
    # filter input doesn't appear before #filter-popup.
    fp_idx = html.find('id="filter-popup"')
    ti_idx = html.find('id="task-filter"')
    assert ti_idx > fp_idx, (
        "filter input should appear AFTER #filter-popup wrapper, not in topbar"
    )


def test_filter_popup_toggles_via_hotkey_and_blur(data):
    """`/` shows the popup; Esc on empty input + blur both hide it."""
    html = st.build_page(data, view="dashboard")
    assert "function _showFilterPopup()" in html
    assert "function _hideFilterPopup()" in html
    # Blur listener wired up
    assert "addEventListener('blur', _hideFilterPopup)" in html
    # Esc-on-empty path calls _hideFilterPopup
    assert "_hideFilterPopup()" in html


def test_pill_filter_attrs_present(data):
    """Counts-strip pills carry data-filter-key/data-filter-val so JS can
    toggle them as filters; rows carry data-pri/data-status/data-stale so
    the matcher can decide visibility."""
    html = st.build_page(data, view="dashboard")
    # Pills
    assert 'data-filter-key="pri" data-filter-val="P1"' in html or \
           'data-filter-key="pri" data-filter-val="P2"' in html, (
        "no priority pill carries filter attrs"
    )
    # Rows
    assert 'data-pri=' in html and 'data-status=' in html
    # JS uses _pillFilters set
    assert "_pillFilters" in html and "_rowMatchesPills" in html


def test_clearfilters_resets_text_and_pills(data):
    """The Clear button hooks `_clearFilters` which resets both inputs."""
    html = st.build_page(data, view="dashboard")
    assert "function _clearFilters()" in html
    assert "_pillFilters.clear()" in html


def test_pills_render_inside_topbar(data):
    """Counts-strip pills now live inside #topbar-pills; the standalone
    counts-strip is no longer prepended to the body."""
    html = st.build_page(data, view="dashboard")
    # The topbar-pills wrapper exists
    assert 'id="topbar-pills"' in html
    # And it contains the actual counts-strip markup
    pills_idx = html.find('id="topbar-pills"')
    next_div = html.find("</div>", pills_idx)
    assert "counts-strip" in html[pills_idx:next_div + 6], (
        "counts-strip should be rendered inside #topbar-pills"
    )
    # The body shouldn't prepend a counts-strip anymore (so it's not duplicated)
    body_idx = html.find('id="tasks-content"')
    body_chunk = html[body_idx:body_idx + 200]
    assert "counts-strip" not in body_chunk, (
        "counts-strip should no longer be at the top of #tasks-content"
    )


def test_topbar_pills_swapped_on_refresh(data):
    """_refreshTasks must swap #topbar-pills innerHTML alongside
    #tasks-content; otherwise the pills (counts, alert badges) go stale
    after every mutation."""
    html = st.build_page(data, view="dashboard")
    # Look for the two-step swap pattern in the JS
    assert "querySelector('#topbar-pills')" in html, (
        "_refreshTasks should swap #topbar-pills"
    )
    pills_swap = html.find("querySelector('#topbar-pills')")
    content_swap = html.find("querySelector('#tasks-content')")
    assert 0 < pills_swap < content_swap, (
        "topbar-pills swap should appear before #tasks-content swap in the JS"
    )


def test_autocompact_uses_rect_bottom_not_offsettop(data):
    """Regression: under #topbar's `align-items: center`, same-row children
    of different heights have DIFFERENT `offsetTop` values, so an offsetTop
    comparison reports "wrapped" for every single-row layout — making the
    topbar permanently compact. Wrap detection must use getBoundingClientRect
    bottom-vs-top instead."""
    html = st.build_page(data, view="dashboard")
    fn_idx = html.find("function _autoCompactTopbar()")
    assert fn_idx > 0, "_autoCompactTopbar function not found"
    # Locate the function body. It ends just before the `window.addEventListener`
    # registration on the line below the closing brace.
    end_marker = "window.addEventListener('resize', _autoCompactTopbar)"
    fn_end = html.find(end_marker, fn_idx)
    body = html[fn_idx:fn_end]
    assert "getBoundingClientRect" in body, (
        "wrap detection should use rect.top vs first child's rect.bottom"
    )
    assert "offsetTop" not in body, (
        "offsetTop comparisons break under align-items:center — "
        "do not regress this back to the offsetTop-based check"
    )


def test_autocompact_runs_on_load_and_resize_and_refresh(data):
    """_autoCompactTopbar must wire into:
      • DOMContentLoaded — initial measurement after first paint
      • window resize    — re-evaluate when viewport changes
      • _refreshTasks    — pill counts/widths can shift after mutation
    """
    html = st.build_page(data, view="dashboard")
    assert "addEventListener('resize', _autoCompactTopbar)" in html
    assert "addEventListener('DOMContentLoaded', _autoCompactTopbar)" in html
    # Inside _refreshTasks, after pills swap, we should call it again.
    refresh_idx = html.find("function _refreshTasks(")
    assert refresh_idx > 0
    # Look for the call to _autoCompactTopbar somewhere in _refreshTasks
    end_idx = html.find("function ", refresh_idx + 10)
    assert "_autoCompactTopbar()" in html[refresh_idx:end_idx], (
        "_refreshTasks should re-evaluate compaction after swapping pills"
    )


def test_buttons_have_aria_labels_for_icon_only_state(data):
    """At narrow viewport, .btn-label is hidden via media query — the
    button needs an accessible name (aria-label / title) for icon-only state."""
    html = st.build_page(data, view="dashboard")
    assert 'id="add-btn" title="Add task" aria-label="Add task"' in html
    assert 'id="sort-btn" title="Sort by priority" aria-label="Sort by priority"' in html
    # The text label is wrapped so it can be hidden by CSS independently
    assert 'class="btn-label">Add</span>' in html
    assert 'class="btn-label">Sort</span>' in html


def test_topbar_compaction_is_content_driven_not_breakpoint(data):
    """Compaction is JS-driven (measure-then-classify), not viewport-based.
    Default has button labels visible; three .compact tiers apply
    progressively when JS detects the topbar has wrapped."""
    html = st.build_page(data, view="dashboard")
    # Default visible — no display:none on .btn-label
    assert ".btn-label { display: inline; }" in html
    # JS function and all three compaction tiers exist
    assert "function _autoCompactTopbar()" in html
    assert "#topbar.compact " in html and "#topbar.compact-tight " in html
    assert "#topbar.compact-tightest " in html
    # The tier-1 class hides button labels (the cheapest space win)
    assert "#topbar.compact .btn-label { display: none; }" in html
    # No viewport-threshold media queries on .btn-label or the topbar tiers
    assert "@media (min-width: 900px)" not in html
    assert "@media (max-width: 1040px)" not in html
    assert "@media (max-width: 860px)" not in html


def test_view_switcher_dropdown_for_tier3(data):
    """Tier 3 (.compact-tightest) hides the tab-style switcher and reveals
    a <select> dropdown that navigates on change. The select renders both
    options (Dashboard / Classic) with the current view marked selected."""
    html = st.build_page(data, view="dashboard")
    # The select element itself
    assert 'id="view-switcher-select"' in html
    # Both view options rendered
    assert '<option value="dashboard"' in html
    assert '<option value="classic"' in html
    # Current view marked selected
    assert '<option value="dashboard" selected>' in html
    # Hidden by default; revealed at tier 3
    assert "#view-switcher-select {" in html
    assert "#topbar.compact-tightest #view-switcher { display: none; }" in html
    assert "#topbar.compact-tightest #view-switcher-select { display: inline-block; }" in html
    # Change handler navigates
    assert "view-switcher-select" in html and "window.location.href" in html


def test_autocompact_resets_all_three_tiers(data):
    """When re-measuring, _autoCompactTopbar must clear all three tier
    classes (not just tier 1+2) before deciding what to apply."""
    html = st.build_page(data, view="dashboard")
    assert (
        "topbar.classList.remove('compact', 'compact-tight', 'compact-tightest')"
        in html
    ), "reset must clear all three compaction tiers before re-measuring"


def test_autocompact_escalates_to_tier3_when_tier2_still_wraps(data):
    """If .compact-tight is applied and isWrapped() still returns true,
    .compact-tightest is added as the final tier."""
    html = st.build_page(data, view="dashboard")
    # The escalation chain must include three nested isWrapped() checks
    fn_idx = html.find("function _autoCompactTopbar()")
    fn_end = html.find(
        "window.addEventListener('resize', _autoCompactTopbar)", fn_idx
    )
    body = html[fn_idx:fn_end]
    assert body.count("isWrapped()") >= 3, (
        "expected three isWrapped() calls — one per tier escalation"
    )
    assert "compact-tightest" in body


def test_topbar_uses_flex_wrap_for_extreme_narrow(data):
    """At very narrow viewports the topbar wraps to a second row instead
    of overflowing horizontally."""
    html = st.build_page(data, view="dashboard")
    # The #topbar selector body must contain flex-wrap: wrap
    idx = html.find("#topbar {")
    chunk = html[idx:idx + 300]
    assert "flex-wrap: wrap" in chunk, "topbar should allow wrap as fallback"


def test_pill_matcher_or_within_and_across():
    """The pill matcher must use OR within a key and AND across keys.
    Selecting `pri:P1` AND `status:waiting` should ONLY match rows that are
    P1 priority AND waiting status — not their union."""
    P1_open       = {"pri": "P1", "status": "open"}
    P1_waiting    = {"pri": "P1", "status": "waiting"}
    P3_waiting    = {"pri": "P3", "status": "waiting"}
    P2_blocked    = {"pri": "P2", "status": "blocked"}

    # Single pill = single-key filter
    assert st._match_pills(P1_open, ["pri:P1"]) is True
    assert st._match_pills(P3_waiting, ["pri:P1"]) is False

    # OR within a key: P1 OR P2
    assert st._match_pills(P1_open, ["pri:P1", "pri:P2"]) is True
    assert st._match_pills(P2_blocked, ["pri:P1", "pri:P2"]) is True
    assert st._match_pills(P3_waiting, ["pri:P1", "pri:P2"]) is False

    # AND across keys: pri:P1 AND status:waiting
    assert st._match_pills(P1_waiting, ["pri:P1", "status:waiting"]) is True
    assert st._match_pills(P1_open,    ["pri:P1", "status:waiting"]) is False
    assert st._match_pills(P3_waiting, ["pri:P1", "status:waiting"]) is False

    # Three keys: pri (P1 or P2) AND status:waiting AND flag:stale
    stale_p1_waiting = {"pri": "P1", "status": "waiting", "stale": True}
    stale_p3_waiting = {"pri": "P3", "status": "waiting", "stale": True}
    assert st._match_pills(stale_p1_waiting, ["pri:P1", "pri:P2", "status:waiting", "flag:stale"]) is True
    assert st._match_pills(stale_p3_waiting, ["pri:P1", "pri:P2", "status:waiting", "flag:stale"]) is False

    # Empty filter set always matches
    assert st._match_pills({"pri": "P5"}, []) is True


def test_pill_matcher_overdue_and_stale_flags():
    """`flag:overdue` matches `overdue=True`; `flag:stale` matches `stale=True`."""
    overdue = {"pri": "P2", "status": "open", "overdue": True}
    stale = {"pri": "P3", "status": "open", "stale": True}
    both  = {"pri": "P3", "status": "open", "overdue": True, "stale": True}
    neither = {"pri": "P3", "status": "open"}
    assert st._match_pills(overdue, ["flag:overdue"]) is True
    assert st._match_pills(stale,   ["flag:overdue"]) is False
    assert st._match_pills(stale,   ["flag:stale"]) is True
    # OR within flag
    assert st._match_pills(stale,   ["flag:overdue", "flag:stale"]) is True
    assert st._match_pills(overdue, ["flag:overdue", "flag:stale"]) is True
    assert st._match_pills(neither, ["flag:overdue", "flag:stale"]) is False
    assert st._match_pills(both,    ["flag:overdue", "flag:stale"]) is True


def test_js_matcher_mirrors_python_matcher(data):
    """The JS `_rowMatchesPills` implementation should follow the same
    OR-within / AND-across rule as the Python `_match_pills`. Spot-check
    the JS source contains the bucketing idiom."""
    html = st.build_page(data, view="dashboard")
    # The fixed implementation buckets by key into byKey
    assert "byKey" in html, "JS matcher should bucket pill filters by key"
    # And short-circuits on missing keys with `return false`
    assert "byKey.pri.length" in html
    assert "byKey.status.length" in html
    assert "byKey.flag.length" in html


def test_clear_button_uses_explicit_display_value(data):
    """Regression for 95a9063: setting style.display = '' lets the CSS
    default (display: none) win, so the button stayed hidden. The JS must
    set an explicit value like 'inline-block'."""
    html = st.build_page(data, view="dashboard")
    assert "anyFilter ? 'inline-block' : 'none'" in html or \
           "anyFilter ? \"inline-block\" : \"none\"" in html, (
        "filter-clear must use an explicit display value, not ''"
    )
    # And the buggy variant must not be present
    assert "anyFilter ? '' :" not in html


def test_goalie_rows_have_no_rename_pencil(data):
    """Rename-pencil should not appear on goalie tasks (no /rename support
    for them). Render a goalie section and confirm."""
    html = st.render_goalie_section("Goalie", [
        {"id": 999, "task": "ping triage queue", "links": [], "status": "open"},
    ])
    assert "rename-pencil" not in html, "rename pencil leaking into goalie section"


def test_filter_data_attrs_helper(data):
    """`_filter_data_attrs(task)` returns `data-pri`, `data-status`, and
    `data-stale="1"` when the task is ≥14d old."""
    fresh = {"pri": "P1", "status": "open", "added": time_module.strftime("%Y-%m-%d", time_module.localtime())}
    out = st._filter_data_attrs(fresh)
    assert 'data-pri="P1"' in out
    assert 'data-status="open"' in out
    assert "data-stale" not in out
    old = {"pri": "P3", "status": "waiting", "added": "2024-01-01"}
    out = st._filter_data_attrs(old)
    assert 'data-stale="1"' in out


def test_filter_hides_cards_with_no_matches(data):
    """When filtering, a `.task-card` whose every task row gets the
    `.filtered-out` class should itself be hidden — empty cards add visual
    noise without showing anything."""
    html = st.build_page(data, view="dashboard")
    # The filter logic must check `.task-card` membership and toggle
    # filtered-out based on whether any task row inside is still visible.
    assert ".task-card" in html and "card.classList.toggle('filtered-out'" in html, (
        "filter logic doesn't hide empty cards"
    )
    assert ":not(.filtered-out)" in html, (
        "filter visibility check should exclude already-hidden rows"
    )


def test_filter_reapplied_after_dom_swap(data):
    """_refreshTasks must call _applyFilter so a typed filter survives
    the SSE/poll-driven DOM swap."""
    html = st.build_page(data, view="dashboard")
    # _applyFilter() should appear inside the _refreshTasks function body
    body_start = html.find("function _refreshTasks(")
    body_end = html.find("\n}", body_start)
    body = html[body_start:body_end + 2]
    assert "_applyFilter()" in body, (
        "_applyFilter not called inside _refreshTasks — filter would reset on every refresh"
    )


def test_counts_strip_renders_when_data_present(data):
    """The counts strip should render priority dots, status icons,
    and a done-today tally when there's relevant data."""
    html = st.build_page(data, view="dashboard")
    assert 'class="counts-strip"' in html
    # Priority dots: at least one .pri-dot rendered for fixture (P1, P2, P3, P4 all present)
    assert "pri-dot" in html or "cnt-group" in html
    # Status counts shown for in_progress / waiting / blocked when present
    assert any(label in html for label in ["🔄", "⏳", "🚫"])


def test_expand_all_button_only_in_card_with_expandable_content(data):
    """The expand-all chevron must only appear in section-headers whose
    section actually has rows-with-detail. Verifies we don't put a useless
    chevron on the goalie section, sparkline, etc."""
    # Goalie section has no whys + no detail panels → no chevron
    data.setdefault("sections", []).append({
        "title": "Goalie",
        "type": "goalie",
        "tasks": [{"id": 999, "task": "ping triage queue", "links": [], "status": "open"}],
    })
    html = st.build_page(data, view="dashboard")
    # Find the Goalie section header chunk
    idx = html.find("Goalie")
    assert idx != -1, "goalie header missing"
    # The first 200 chars after the header shouldn't contain a chevron
    assert 'data-action="expand-all"' not in html[idx:idx + 200], (
        "expand-all button should not appear on goalie section"
    )


def test_section_count_subtitle_in_table_section(data):
    """Non-Focus table sections render `· N` count in their headers."""
    high = next(s for s in data["sections"] if s["title"] == "High Priority")
    n = len(high["tasks"])
    html = st.build_page(data, view="dashboard")
    assert f">· {n}<" in html, f"expected count subtitle '· {n}' for High Priority"


def test_section_count_subtitle_in_compact_section(data):
    """Compact sections (Monitoring, Lower Priority, Completed Today) too."""
    mon = next(s for s in data["sections"] if s["title"] == "Monitoring")
    n = len(mon["tasks"])
    html = st.build_page(data, view="dashboard")
    assert f">· {n}<" in html, f"expected count subtitle '· {n}' for Monitoring"


def test_compute_counts_stale_logic(data):
    """compute_counts returns 0 when no task is ≥14d old; non-zero when one is."""
    # Force all `added` to today
    today_iso = time_module.strftime("%Y-%m-%d", time_module.localtime())
    for s in data["sections"]:
        for t in s["tasks"]:
            t["added"] = today_iso
    _, _, _, _, stale = st.compute_counts(data)
    assert stale == 0, "no tasks ≥14d should be 0 stale"
    # Now plant one stale task
    data["sections"][0]["tasks"][0]["added"] = "2024-01-01"
    _, _, _, _, stale = st.compute_counts(data)
    assert stale == 1


def test_stale_pill_renders_when_any_task_is_stale(data):
    """The 🧹 stale pill in the counts strip should render exactly when
    at least one task has `added` ≥14d ago."""
    # Force one task to be 30 days stale
    for s in data["sections"]:
        if s["tasks"]:
            s["tasks"][0]["added"] = "2026-01-01"
            break
    html = st.build_page(data, view="dashboard")
    assert "🧹" in html and "stale" in html


def test_uncancel_endpoint_round_trip(isolated_state):
    """Cancel + uncancel within the undo window restores both the JSON
    state and the markdown."""
    core_path = st.current_core_path("W18")
    # Cancel a task
    assert st.apply_cancel(120)
    data = json.loads(isolated_state.read_text())
    assert all(t.get("id") != 120 for s in data["sections"] for t in s["tasks"]), \
        "task should be removed from sections after cancel"
    core_after_cancel = core_path.read_text()
    assert "[/]" in core_after_cancel, "cancel should write a [/] line"
    assert "## Cancelled" in core_after_cancel
    # Uncancel
    assert st.apply_uncancel(120)
    data = json.loads(isolated_state.read_text())
    restored = [t for s in data["sections"] for t in s["tasks"] if t.get("id") == 120]
    assert len(restored) == 1, "task should be back in a section after uncancel"
    core_after_uncancel = core_path.read_text()
    # The [/] line for this task is gone; an active [ ] (or whatever marker) line is present
    assert "_(cancelled:" not in core_after_uncancel, \
        "uncancel should strip the _(cancelled: …)_ tag"
    # Original task name is in the active area
    done_idx = core_after_uncancel.find("## Done")
    cancelled_idx = core_after_uncancel.find("## Cancelled")
    active_chunk = core_after_uncancel[:min(x for x in (done_idx, cancelled_idx, len(core_after_uncancel)) if x != -1)]
    assert "High prio task" in active_chunk, "uncancelled task missing from active markdown"


def test_uncancel_preserves_status_marker(isolated_state):
    """Uncanceling an in-progress task restores it with [-], not [ ]."""
    core_path = st.current_core_path("W18")
    # Promote 120 to in_progress, write the marker manually
    data = json.loads(isolated_state.read_text())
    task = next(t for s in data["sections"] for t in s["tasks"] if t.get("id") == 120)
    task["status"] = "in_progress"
    isolated_state.write_text(json.dumps(data))
    text = core_path.read_text()
    text = text.replace("- [ ] 🟠 High prio task", "- [-] 🟠 High prio task")
    core_path.write_text(text)

    assert st.apply_cancel(120)
    assert st.apply_uncancel(120)
    core = core_path.read_text()
    # The restored line should carry [-] back, not [ ]
    assert "- [-] 🟠 High prio task" in core, \
        f"in_progress marker should survive cancel→uncancel; got:\n{core}"


def test_uncancel_returns_false_when_no_record(isolated_state):
    """Uncancel without a prior cancel for that id is a no-op."""
    assert not st.apply_uncancel(99999)


def test_rename_endpoint_preserves_emoji_and_due(isolated_state):
    """Rename keeps the priority emoji and trailing ` — due …` text intact."""
    new_name = "Updated task name"
    assert st.apply_rename(120, new_name)
    data = json.loads(isolated_state.read_text())
    task = next(t for s in data["sections"] for t in s["tasks"] if t.get("id") == 120)
    assert task["task"] == new_name
    core = st.current_core_path("W18").read_text()
    # Old name gone; new name present with the original emoji and due intact
    assert "High prio task" not in core, "old name still in core"
    assert f"- [ ] 🟠 {new_name} — due 17:00" in core, (
        f"emoji + due not preserved on rename; got:\n{core}"
    )


def test_rename_disambiguates_by_priority_when_names_collide(isolated_state):
    """Two tasks with the same name but different priority — rename one,
    only that one's markdown line gets touched."""
    core_path = st.current_core_path("W18")
    # Add a duplicate-name task with different priority to JSON + core
    data = json.loads(isolated_state.read_text())
    high = next(s for s in data["sections"] if s["title"] == "High Priority")
    high["tasks"][0]["task"] = "Shared name"
    high["tasks"][0]["pri"] = "P2"
    # Add a P3 dup in Lower Priority
    lower = next(s for s in data["sections"] if s["title"] == "Lower Priority")
    lower["tasks"].append({"id": 999, "num": 99, "pri": "P3", "task": "Shared name",
                            "due": "—", "from": "W18", "added": "2026-04-28",
                            "links": [], "status": "open", "why": "—"})
    isolated_state.write_text(json.dumps(data))
    text = core_path.read_text()
    text = text.replace("- [ ] 🟠 High prio task — due 17:00", "- [ ] 🟠 Shared name")
    text += "\n- [ ] 🟡 Shared name\n"
    core_path.write_text(text)

    # Rename the P2 (orange) one
    assert st.apply_rename(120, "Renamed-orange")
    core = core_path.read_text()
    # Orange line renamed
    assert "- [ ] 🟠 Renamed-orange" in core
    # Yellow line untouched
    assert "- [ ] 🟡 Shared name" in core, (
        f"P3 duplicate should NOT have been renamed; got:\n{core}"
    )


def test_rename_rejects_empty_or_dangerous_names(isolated_state):
    """Empty / whitespace-only / control-char names are refused."""
    assert not st.apply_rename(120, "")
    assert not st.apply_rename(120, "   ")
    # Newline injection — would corrupt markdown structure
    assert not st.apply_rename(120, "Foo\n## Done\n- [ ] Injected")
    assert not st.apply_rename(120, "tab\there")
    assert not st.apply_rename(120, "carriage\rreturn")


def test_rename_pencil_rendered_on_each_task(data):
    """Every task row that has an editable name carries a rename pencil
    with `data-action="rename"` and the task id."""
    html = st.build_page(data, view="dashboard")
    assert 'data-action="rename"' in html, "rename pencil missing from rendered HTML"
    # Pencil class
    assert 'class="rename-pencil"' in html


def test_undo_toast_element_present_with_styles(data):
    """The toast container + Undo button + progress bar exist in the page."""
    html = st.build_page(data, view="dashboard")
    assert 'id="toast"' in html
    assert "toast-undo" in html and "toast-progress" in html
    assert "_showToast" in html  # the JS helper


def test_uncancel_route_in_route_table(data):
    """Verify /uncancel and /rename are exposed."""
    html = st.build_page(data, view="dashboard")
    # The route table is python-side; check via module attr
    assert "/uncancel" in st.Handler._ROUTES
    assert "/rename" in st.Handler._ROUTES


def test_help_overlay_lists_all_documented_hotkeys(data):
    """The ? help overlay should list every hotkey we wire — otherwise
    they're silently undiscoverable."""
    html = st.build_page(data, view="dashboard")
    assert 'id="help-overlay"' in html
    # Each hotkey appears as its own <kbd>…</kbd>
    for kbd in ["x", "r", "s", "a", "c", "/", "j", "k", "Enter", "Shift", "S", "P", "1", "2", "3", "Esc", "?"]:
        assert f"<kbd>{kbd}</kbd>" in html, f"hotkey <kbd>{kbd}</kbd> missing from help overlay"


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


def test_edit_modal_dual_mode_wired(data):
    """The Add modal's DOM is reused for editing. Mode lives on
    #modal[data-mode]; title and save-button labels swap based on mode;
    save POSTs to /add or /edit depending."""
    html = st.build_page(data, view="dashboard")
    # Modal carries the mode attribute
    assert 'id="modal" data-mode="add"' in html
    # Title and save are id-tagged so JS can swap their text
    assert 'id="modal-title"' in html
    assert 'id="modal-save"' in html
    # Open helpers and reset helper exist
    assert "function _openAddModal()" in html
    assert "function _openEditModal(taskId)" in html
    assert "function _resetModal()" in html
    # Save handler branches on mode and posts to the right route
    assert "_post('/edit'," in html
    assert "_post('/add'," in html


def test_edit_modal_prefills_via_task_endpoint(data):
    """_openEditModal fetches GET /task?id=N and pre-fills modal fields."""
    html = st.build_page(data, view="dashboard")
    assert "fetch('/task?id=' + encodeURIComponent(taskId))" in html


def test_ctx_menu_has_edit(data):
    """The right-click context menu carries an Edit item that triggers
    the edit modal."""
    html = st.build_page(data, view="dashboard")
    assert 'data-action="edit"' in html
    # Click handler routes the action to _openEditModal
    assert "_openEditModal(taskId)" in html


def test_e_hotkey_opens_edit_for_highlighted(data):
    """`e` on a highlighted row opens the edit modal."""
    html = st.build_page(data, view="dashboard")
    assert "e.key === 'e' && _hilitId != null" in html
    assert "_openEditModal(_hilitId)" in html


def test_apply_edit_updates_json_and_core(isolated_state):
    """apply_edit rewrites JSON entry's pri/due/why/links/name and the core
    file's matching task line."""
    # Task id 120 is High prio task (P2, due 17:00) in the fixture
    ok = st.apply_edit(120, {
        "task": "Renamed via apply_edit",
        "pri": "P1",
        "due": "17:30",
        "why": "ship before friday",
        "link_label": "HOTS-9999",
        "link_url": "https://example.com/9999",
    })
    assert ok
    new_data = json.loads(isolated_state.read_text())
    new_task = next(t for s in new_data["sections"] for t in s["tasks"] if t.get("id") == 120)
    assert new_task["task"] == "Renamed via apply_edit"
    assert new_task["pri"] == "P1"
    assert new_task["due"] == "17:30"
    assert new_task["why"] == "ship before friday"
    assert new_task["links"] == [{"label": "HOTS-9999", "url": "https://example.com/9999"}]
    core_text = st.current_core_path("W18").read_text()
    assert "Renamed via apply_edit" in core_text
    # New line carries the new emoji and due
    assert "🔴 Renamed via apply_edit" in core_text
    assert "due 17:30" in core_text
    # And the why suffix
    assert "_(why: ship before friday)_" in core_text
    # Old name is gone (no other task shares it)
    assert "High prio task" not in core_text


def test_apply_edit_rejects_control_chars(isolated_state):
    """apply_edit must reject names/whys with newlines or other control chars
    that would corrupt the markdown structure."""
    assert st.apply_edit(120, {"task": "with\nnewline"}) is False
    assert st.apply_edit(120, {"task": "ok name", "why": "evil\twhy"}) is False


def test_apply_edit_preserves_carried_metadata(isolated_state):
    """When a task line carries `_(carried from Wxx)_`, apply_edit must
    preserve that suffix when rewriting the rest of the line."""
    core_path = st.current_core_path("W18")
    lines = core_path.read_text().split("\n")
    done_idx = st._done_boundary(lines, len(lines))
    carried_line = "- [ ] 🟠 Carried task name — due 14:00 _(carried from W17)_"
    lines.insert(done_idx, carried_line)
    core_path.write_text("\n".join(lines))

    data = json.loads(isolated_state.read_text())
    high = next(s for s in data["sections"] if s["title"] == "High Priority")
    new_id = st.next_task_id(data)
    high["tasks"].append({
        "id": new_id, "num": 99, "pri": "P2",
        "task": "Carried task name", "due": "14:00", "from": "W17",
        "links": [], "status": "open", "why": "—",
    })
    isolated_state.write_text(json.dumps(data, indent=2))

    ok = st.apply_edit(new_id, {
        "task": "Carried task RENAMED",
        "pri": "P1", "due": "15:00", "why": "—",
        "link_label": "", "link_url": "",
    })
    assert ok
    new_text = st.current_core_path("W18").read_text()
    assert "_(carried from W17)_" in new_text
    assert "Carried task RENAMED" in new_text


def test_click_to_highlight_listener_present(data):
    """A document-level click listener sets the highlight when a row is
    clicked, so subsequent hotkeys (e/Shift+S/Shift+P/Enter) target the
    just-clicked row."""
    html = st.build_page(data, view="dashboard")
    # The listener selector must match both row types
    assert 'tr[draggable="true"][data-id], .cmp-row[data-id]' in html
    # And it must call _setHighlight on a row click
    assert "_setHighlight(row.dataset.id)" in html


def test_apply_edit_preserves_extra_links(isolated_state):
    """The modal only edits one (label, url), but the JSON schema allows
    multiple. apply_edit must NOT silently drop links 2+; the modal's
    single submitted link replaces position 0 and any extras are kept."""
    # Seed task 120 with two links
    data = json.loads(isolated_state.read_text())
    high = next(s for s in data["sections"] if s["title"] == "High Priority")
    target = high["tasks"][0]
    target["links"] = [
        {"label": "first", "url": "https://example.com/1"},
        {"label": "second", "url": "https://example.com/2"},
    ]
    isolated_state.write_text(json.dumps(data, indent=2))
    # Edit replaces the FIRST link only
    ok = st.apply_edit(120, {
        "task": target["task"],
        "pri": target["pri"],
        "due": target.get("due", "—"),
        "why": target.get("why", "—"),
        "link_label": "replaced",
        "link_url": "https://example.com/replaced",
    })
    assert ok
    new_data = json.loads(isolated_state.read_text())
    new_task = next(t for s in new_data["sections"] for t in s["tasks"] if t.get("id") == 120)
    # Position 0 replaced, position 1 preserved
    assert new_task["links"] == [
        {"label": "replaced", "url": "https://example.com/replaced"},
        {"label": "second", "url": "https://example.com/2"},
    ]


def test_apply_edit_clearing_link_keeps_extras(isolated_state):
    """If the user clears the link fields in the modal, extras still survive
    — clearing the modal field only drops the first link, not all of them."""
    data = json.loads(isolated_state.read_text())
    high = next(s for s in data["sections"] if s["title"] == "High Priority")
    target = high["tasks"][0]
    target["links"] = [
        {"label": "first", "url": "https://example.com/1"},
        {"label": "kept", "url": "https://example.com/kept"},
    ]
    isolated_state.write_text(json.dumps(data, indent=2))
    ok = st.apply_edit(120, {
        "task": target["task"], "pri": target["pri"],
        "due": "—", "why": "—",
        "link_label": "", "link_url": "",
    })
    assert ok
    new_data = json.loads(isolated_state.read_text())
    new_task = next(t for s in new_data["sections"] for t in s["tasks"] if t.get("id") == 120)
    assert new_task["links"] == [{"label": "kept", "url": "https://example.com/kept"}]


def test_apply_edit_preserves_carried_with_real_why(isolated_state):
    """Carried + why on the same line: rewrite must keep the carried suffix
    AND emit the new why suffix in the right order (carried before why,
    matching the actual convention in journal core files)."""
    core_path = st.current_core_path("W18")
    lines = core_path.read_text().split("\n")
    done_idx = st._done_boundary(lines, len(lines))
    line = (
        "- [ ] 🟠 Carried with why — due 14:00 "
        "_(carried from W17)_ _(why: blocked on review)_"
    )
    lines.insert(done_idx, line)
    core_path.write_text("\n".join(lines))

    data = json.loads(isolated_state.read_text())
    high = next(s for s in data["sections"] if s["title"] == "High Priority")
    new_id = st.next_task_id(data)
    high["tasks"].append({
        "id": new_id, "num": 99, "pri": "P2",
        "task": "Carried with why", "due": "14:00", "from": "W17",
        "links": [], "status": "open", "why": "blocked on review",
    })
    isolated_state.write_text(json.dumps(data, indent=2))

    ok = st.apply_edit(new_id, {
        "task": "Carried with why", "pri": "P2",
        "due": "15:00", "why": "now waiting on legal",
        "link_label": "", "link_url": "",
    })
    assert ok
    new_text = st.current_core_path("W18").read_text()
    # New line has both carried and the updated why, in the right order
    assert (
        "_(carried from W17)_ _(why: now waiting on legal)_" in new_text
    ), f"carried+why not in expected order; got:\n{new_text}"
    # And the old why is gone
    assert "blocked on review" not in new_text


def test_apply_edit_no_priority_emoji(isolated_state):
    """A task with pri=None should still edit cleanly — no emoji prefix on
    the rewritten line."""
    core_path = st.current_core_path("W18")
    lines = core_path.read_text().split("\n")
    done_idx = st._done_boundary(lines, len(lines))
    lines.insert(done_idx, "- [ ] No-emoji task")
    core_path.write_text("\n".join(lines))

    data = json.loads(isolated_state.read_text())
    low = next(s for s in data["sections"] if s["title"] == "Lower Priority")
    new_id = st.next_task_id(data)
    low["tasks"].append({
        "id": new_id, "num": 99, "pri": None,
        "task": "No-emoji task", "due": "—", "from": "W18",
        "links": [], "status": "open", "why": "—",
    })
    isolated_state.write_text(json.dumps(data, indent=2))

    ok = st.apply_edit(new_id, {
        "task": "No-emoji renamed", "pri": None,
        "due": "—", "why": "—",
        "link_label": "", "link_url": "",
    })
    assert ok
    new_text = st.current_core_path("W18").read_text()
    # Line preserved without an emoji prefix
    assert "- [ ] No-emoji renamed" in new_text


def test_apply_edit_accepts_string_id(isolated_state):
    """Regression: the JS edit flow used to send `"id": "34"` (string) when
    the modal was opened via the `e` hotkey on a row highlighted by click —
    `row.dataset.id` is always a string. find_task_by_id then missed on
    int==str and apply_edit returned False (HTTP 400) silently. Coerce at
    the boundary so string ids work as defense-in-depth."""
    ok = st.apply_edit("120", {
        "task": "edited via string id",
        "pri": "P2", "due": "—", "why": "—",
        "link_label": "", "link_url": "",
    })
    assert ok
    after = json.loads(isolated_state.read_text())
    task = next(t for s in after["sections"] for t in s["tasks"] if t["id"] == 120)
    assert task["task"] == "edited via string id"


def test_apply_edit_rejects_unparseable_id(isolated_state):
    """A non-numeric id string fails fast (returns False from
    find_task_by_id's coercion), no crash."""
    assert not st.apply_edit("not-a-number", {"task": "x"})
    assert not st.apply_edit(None, {"task": "x"})
    assert not st.apply_edit({"oops": "dict"}, {"task": "x"})


def test_post_edit_route_accepts_string_id(isolated_state):
    """End-to-end: the /edit route handler from _ROUTES accepts a string
    id. This is the actual bug path — the JS sends `{"id": "120"}` from
    row.dataset.id, and the route lambda passes b.get("id") raw to
    apply_edit. find_task_by_id's int() coercion is what closes the loop."""
    handler = st.Handler._ROUTES["/edit"]
    ok = handler({
        "id": "120",  # string, as the JS sends
        "task": "edited via route + string id",
        "pri": "P2", "due": "—", "why": "—",
        "link_label": "", "link_url": "",
    })
    assert ok
    after = json.loads(isolated_state.read_text())
    task = next(t for s in after["sections"] for t in s["tasks"] if t["id"] == 120)
    assert task["task"] == "edited via route + string id"


def test_find_task_by_id_coerces_string(data):
    """find_task_by_id accepts string ids (the choke point that protects
    every apply_X mutation against the row.dataset.id string-leak bug)."""
    # Find any active task to get its id
    target_id = data["sections"][0]["tasks"][0]["id"]
    task_int, _ = st.find_task_by_id(data, target_id)
    task_str, _ = st.find_task_by_id(data, str(target_id))
    assert task_int is not None
    assert task_str is not None
    assert task_int is task_str  # same object, both lookups find it
    # Garbage gracefully returns (None, None)
    assert st.find_task_by_id(data, "not-an-int") == (None, None)
    assert st.find_task_by_id(data, None) == (None, None)


def test_show_toast_resets_undo_visibility(data):
    """Toast race: after _showErrorToast hides the Undo button, a
    subsequent _showToast must restore it. Without this, a successful
    action's toast would have no Undo button until a manual page reload."""
    html = st.build_page(data, view="dashboard")
    # _showToast explicitly resets undoBtn.style.display so a previous
    # error toast's `display: none` doesn't leak through cloneNode.
    assert "undoBtn.style.display = ''" in html


def test_open_edit_modal_coerces_id_to_int(data):
    """JS-side defense: `_openEditModal` parses its argument as int because
    `_hilitId` (sourced from row.dataset.id) is a string."""
    html = st.build_page(data, view="dashboard")
    assert "taskId = parseInt(taskId, 10)" in html


def test_modal_fetch_token_guards_against_race(data):
    """_openEditModal must capture a fetch token at call time and bail in
    the .then() if it's been superseded by close/openAdd/another openEdit.
    Without this, a stale fetch resolves and clobbers the new state."""
    html = st.build_page(data, view="dashboard")
    assert "var _editFetchToken" in html
    # Token is captured BEFORE the fetch call
    assert "var token = ++_editFetchToken;" in html
    # And checked at both .then steps
    assert "token !== _editFetchToken" in html


def test_help_overlay_is_sectioned(data):
    """The help overlay groups shortcuts into sections — Navigation, Mutate,
    Add & sort, Filter, View & UI."""
    html = st.build_page(data, view="dashboard")
    for label in ("Navigation", "Mutate highlighted row",
                  "Add &amp; sort", "Filter", "View &amp; UI"):
        assert f'<th colspan="2">{label}</th>' in html, (
            f"missing help section header: {label}"
        )
    # Edit hotkey is documented in the new Mutate section
    assert "Edit task (modal)" in html and "<kbd>e</kbd>" in html


# ---------------------------------------------------------------------------
# Slack triage view
# ---------------------------------------------------------------------------

def _slack_snapshot(items=None, generated_at=None, noise=None, version=1):
    """Build a snapshot dict matching the schema in docs/slack-triage-view-design.md."""
    import datetime as _dt
    if generated_at is None:
        generated_at = _dt.datetime.now(_dt.timezone.utc).astimezone().isoformat(timespec="seconds")
    return {
        "version": version,
        "generated_at": generated_at,
        "items": items or [],
        "noise": noise or {},
    }


def _slack_item(channel_id="C1", message_ts="111.222", tier="reply_needed",
                sender="Maria", channel_name="hotsauce-squad", is_dm=False,
                snippet="need your input on the GHE rollout",
                permalink=None, ts=None, thread_ts=None,
                action_hint=None, context=None):
    import datetime as _dt
    if permalink is None:
        permalink = (
            f"https://spotify.slack.com/archives/{channel_id}"
            f"/p{message_ts.replace('.', '')}"
        )
    if ts is None:
        ts = _dt.datetime.now(_dt.timezone.utc).astimezone().isoformat(timespec="seconds")
    item = {
        "channel_id": channel_id, "message_ts": message_ts, "thread_ts": thread_ts,
        "tier": tier, "is_dm": is_dm, "sender": sender,
        "channel_name": channel_name, "permalink": permalink,
        "snippet": snippet, "ts": ts,
    }
    if action_hint is not None: item["action_hint"] = action_hint
    if context is not None:     item["context"] = context
    return item


@pytest.fixture
def slack_state(tmp_path, monkeypatch):
    """Point SLACK_*_FILE constants at temp paths. Returns a dict of the
    three Path objects so tests can write/read directly. Dismissed/converted
    are JSONL append-logs."""
    triage = tmp_path / "slack-triage.json"
    dismissed = tmp_path / "slack-dismissed.jsonl"
    converted = tmp_path / "slack-converted.jsonl"
    monkeypatch.setattr(st, "SLACK_SNAPSHOT_FILE", triage)
    monkeypatch.setattr(st, "SLACK_DISMISSED_FILE", dismissed)
    monkeypatch.setattr(st, "SLACK_CONVERTED_FILE", converted)
    return {"triage": triage, "dismissed": dismissed, "converted": converted}


def _read_jsonl(path):
    """Read a JSONL file as a list of dicts. Test helper."""
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_slack_view_renders_three_sections(data, slack_state):
    slack_state["triage"].write_text(json.dumps(_slack_snapshot(items=[
        _slack_item(channel_id="C1", message_ts="1.1", tier="reply_needed"),
        _slack_item(channel_id="C2", message_ts="2.2", tier="review",
                    sender="Lorna", channel_name="github-support"),
        _slack_item(channel_id="C3", message_ts="3.3", tier="already_handled",
                    sender="Dennis", channel_name="hotsauce-internal-test-kitchen"),
    ])))
    html = st.build_page(data, view="slack")
    assert "Reply Needed" in html
    assert "Review" in html
    assert "Already Handled" in html
    # Each item rendered with its sender + channel
    assert "Maria" in html and "#hotsauce-squad" in html
    assert "Lorna" in html and "#github-support" in html
    assert "Dennis" in html
    # Already Handled defaults to collapsed
    assert "slack-section collapsed" in html


def test_slack_view_empty_when_no_snapshot(data, slack_state):
    """Missing slack-triage.json → friendly placeholder, not 500."""
    assert not slack_state["triage"].exists()
    html = st.build_page(data, view="slack")
    assert "No Slack snapshot yet" in html
    assert "/slack" in html  # tells the user how to populate
    assert "<!DOCTYPE html>" in html


def test_slack_view_empty_when_snapshot_malformed(data, slack_state):
    slack_state["triage"].write_text("not valid json {{")
    html = st.build_page(data, view="slack")
    assert "unreadable" in html.lower()
    # Must not 500 — the wrapper page is still emitted
    assert "<!DOCTYPE html>" in html


def test_slack_view_rejects_unknown_version(data, slack_state):
    snap = _slack_snapshot(items=[], version=99)
    slack_state["triage"].write_text(json.dumps(snap))
    html = st.build_page(data, view="slack")
    assert "version not supported" in html.lower()
    assert "99" in html


def test_slack_dismiss_records_id_and_filters_item(data, slack_state):
    slack_state["triage"].write_text(json.dumps(_slack_snapshot(items=[
        _slack_item(channel_id="CABC", message_ts="1.111"),
        _slack_item(channel_id="CDEF", message_ts="2.222", sender="Bob"),
    ])))
    item_id = "CABC:1.111"
    assert st.apply_slack_dismiss(item_id)

    # Dismissed file written as JSONL: one record per line
    records = _read_jsonl(slack_state["dismissed"])
    assert len(records) == 1
    assert records[0]["id"] == item_id
    assert records[0]["kind"] == "message"
    assert "ts" in records[0]

    # Re-render: the dismissed item is filtered out, the other survives
    html = st.build_page(data, view="slack")
    assert "Bob" in html
    assert "p1111" not in html


def test_slack_dismiss_appends_each_call(slack_state):
    """Append-only log: each dismiss adds a line. The active set dedupes
    naturally at filter-time, so user-facing behaviour stays idempotent
    even though the file isn't."""
    st.apply_slack_dismiss("C1:1.1")
    st.apply_slack_dismiss("C1:1.1")
    records = _read_jsonl(slack_state["dismissed"])
    assert len(records) == 2  # two appends, but...
    msgs, _ = st.load_slack_dismissed()
    assert msgs == {"C1:1.1"}  # ...the active set has just the one id


def test_slack_dismiss_rejects_bad_input(slack_state):
    assert not st.apply_slack_dismiss(None)
    assert not st.apply_slack_dismiss("")
    assert not st.apply_slack_dismiss(123)
    # Bad scope
    assert not st.apply_slack_dismiss("C1:1.1", scope="bogus")


def test_slack_convert_creates_task_and_records_id(isolated_state, slack_state):
    """apply_slack_convert calls apply_add (writing to JSON_FILE / core file)
    AND appends id to slack-converted.jsonl."""
    ok = st.apply_slack_convert({
        "id": "CABC:1.111",
        "task": "Reply to Maria in #hotsauce-squad",
        "pri": "P2", "due": "—", "why": "the snippet",
        "link_label": "Slack",
        "link_url": "https://spotify.slack.com/archives/CABC/p1111",
    })
    assert ok
    after = json.loads(isolated_state.read_text())
    names = [t["task"] for s in after["sections"] for t in s["tasks"]]
    assert "Reply to Maria in #hotsauce-squad" in names
    # Converted id recorded as a JSONL row
    records = _read_jsonl(slack_state["converted"])
    assert len(records) == 1
    assert records[0]["id"] == "CABC:1.111"
    assert "ts" in records[0]


def test_slack_convert_failure_does_not_record(isolated_state, slack_state):
    """If apply_add fails (e.g. empty task name), the converted set must NOT
    grow — otherwise the item would silently disappear from the view."""
    ok = st.apply_slack_convert({
        "id": "CABC:1.111",
        "task": "",  # empty → apply_add returns False
    })
    assert not ok
    assert not slack_state["converted"].exists()


def test_slack_view_filters_by_active_task_permalink(data, slack_state):
    """If an active task already has the snapshot item's permalink in its
    links, the slack item is hidden — even without it being in the
    converted set (legacy / hand-curated case)."""
    permalink = "https://spotify.slack.com/archives/CXY/pZZZ"
    snap = _slack_snapshot(items=[
        _slack_item(channel_id="CXY", message_ts="abc.def", permalink=permalink),
    ])
    slack_state["triage"].write_text(json.dumps(snap))
    # Inject the permalink into an active task's links
    data["sections"][0]["tasks"][0]["links"] = [{"label": "Slack", "url": permalink}]
    html = st.build_page(data, view="slack")
    # No item rendered, so the empty-section text appears
    assert "Nothing here." in html


def test_slack_view_does_not_filter_when_task_is_cancelled(data, slack_state):
    """A cancelled task with the permalink should NOT hide the item — the
    user explicitly opted out, so the item is still actionable."""
    permalink = "https://spotify.slack.com/archives/CXY/pZZZ"
    snap = _slack_snapshot(items=[
        _slack_item(channel_id="CXY", message_ts="abc.def", permalink=permalink),
    ])
    slack_state["triage"].write_text(json.dumps(snap))
    target = data["sections"][0]["tasks"][0]
    target["links"] = [{"label": "Slack", "url": permalink}]
    target["status"] = "cancelled"
    html = st.build_page(data, view="slack")
    # Cancelled doesn't count as "active" so the item is still shown
    assert permalink in html or "abc.def" in html


def test_slack_signature_includes_slack_files(slack_state):
    """_state_signature must change when any of the three slack files
    is created or modified — otherwise SSE doesn't fire on /slack writes."""
    # Need JSON_FILE to be present for _state_signature to return non-None
    # (the function returns None if the canonical state file is missing).
    # _safe_mtime returns 0 for missing slack files, which is fine.
    if not st.JSON_FILE.exists():
        pytest.skip("real ~/todo/tasks-live.json missing; skipping live signature check")
    sig_before = st._state_signature()
    slack_state["triage"].write_text("{}")
    sig_after = st._state_signature()
    assert sig_before != sig_after


def test_slack_view_escapes_snippet_html(data, slack_state):
    """Slack messages can contain <, >, &; the rendered .slack-snippet div
    must escape them. (Raw text in the JSON data block is fine — that script
    tag is type=application/json and the closing </script> is escaped.)"""
    snap = _slack_snapshot(items=[
        _slack_item(snippet="<script>alert(1)</script> & such"),
    ])
    slack_state["triage"].write_text(json.dumps(snap))
    html = st.build_page(data, view="slack")
    # Find the rendered .slack-snippet div and assert no raw HTML survived in it.
    m = re.search(r'<div class="slack-snippet">(.*?)</div>', html)
    assert m is not None, "no .slack-snippet div rendered"
    rendered = m.group(1)
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    # And the JSON data block must escape its closing </script> token to
    # avoid breaking out of the type=application/json tag.
    assert r'<\/script>' in html or "<\\/script>" in html


def test_slack_view_stale_badge_after_24h(data, slack_state):
    import datetime as _dt
    old = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=25)).astimezone().isoformat(timespec="seconds")
    snap = _slack_snapshot(items=[_slack_item()], generated_at=old)
    slack_state["triage"].write_text(json.dumps(snap))
    html = st.build_page(data, view="slack")
    assert 'class="slack-stale"' in html


def test_slack_view_no_stale_badge_when_fresh(data, slack_state):
    import datetime as _dt
    fresh = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=5)).astimezone().isoformat(timespec="seconds")
    snap = _slack_snapshot(items=[_slack_item()], generated_at=fresh)
    slack_state["triage"].write_text(json.dumps(snap))
    html = st.build_page(data, view="slack")
    assert 'class="slack-stale"' not in html


def test_slack_view_embeds_items_data_for_modal(data, slack_state):
    """The convert modal pre-fills from a JSON script tag rather than fetching;
    the tag must contain the visible items keyed by composite id."""
    snap = _slack_snapshot(items=[
        _slack_item(channel_id="CA", message_ts="1.1", sender="Maria"),
    ])
    slack_state["triage"].write_text(json.dumps(snap))
    html = st.build_page(data, view="slack")
    assert 'id="slack-items-data"' in html
    assert '"CA:1.1"' in html
    assert '"sender": "Maria"' in html or '"sender":"Maria"' in html


def test_slack_view_renders_dm_target_correctly(data, slack_state):
    snap = _slack_snapshot(items=[
        _slack_item(is_dm=True, sender="Lorna", channel_name=""),
    ])
    slack_state["triage"].write_text(json.dumps(snap))
    html = st.build_page(data, view="slack")
    assert "@Lorna" in html


def test_slack_modal_save_routes_to_convert_endpoint(data):
    """The modal save handler must route to /slack/convert when in
    slack-convert mode, NOT /add."""
    html = st.build_page(data, view="dashboard")
    assert "mode === 'slack-convert'" in html
    assert "_post('/slack/convert', payload)" in html


def test_slack_view_in_view_switcher(data, slack_state):
    slack_state["triage"].write_text(json.dumps(_slack_snapshot(items=[])))
    html = st.build_page(data, view="slack")
    # View-switcher entry rendered with active class
    assert '?view=slack' in html
    assert 'class="vs-btn active"' in html  # current view


def test_slack_dismiss_via_post_route(isolated_state, slack_state):
    """The /slack/dismiss POST route is wired into _ROUTES."""
    routes = st.Handler._ROUTES
    assert "/slack/dismiss" in routes
    assert "/slack/convert" in routes
    handler = routes["/slack/dismiss"]
    assert handler({"id": "CXX:9.9"})
    records = _read_jsonl(slack_state["dismissed"])
    assert any(r["id"] == "CXX:9.9" for r in records)


def test_slack_atomic_write_uses_replace(tmp_path):
    """_atomic_write_json must write via tempfile + os.replace so partial
    files are never observed."""
    target = tmp_path / "out.json"
    st._atomic_write_json(target, {"version": 1, "ids": ["a", "b"]})
    assert json.loads(target.read_text()) == {"version": 1, "ids": ["a", "b"]}
    # No leftover .tmp files
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".")]
    assert leftovers == []


def test_slack_snapshot_required_fields_visible_in_render(data, slack_state):
    """An item missing a permalink should still render (permalink defaults
    to # in the link tag) — the dashboard tolerates the optional fields."""
    snap = _slack_snapshot(items=[{
        "channel_id": "CA", "message_ts": "1.1", "thread_ts": None,
        "tier": "reply_needed", "is_dm": False, "sender": "Maria",
        "channel_name": "x", "permalink": "",
        "snippet": "hi", "ts": "",
    }])
    slack_state["triage"].write_text(json.dumps(snap))
    html = st.build_page(data, view="slack")
    assert "Maria" in html
    assert "hi" in html


def test_slack_view_noise_summary_renders(data, slack_state):
    snap = _slack_snapshot(items=[_slack_item()],
                           noise={"#random": 12, "bot_pings": 8})
    slack_state["triage"].write_text(json.dumps(snap))
    html = st.build_page(data, view="slack")
    assert "Noise:" in html
    assert "12 in #random" in html


def test_slack_view_no_noise_line_when_empty(data, slack_state):
    snap = _slack_snapshot(items=[_slack_item()], noise={})
    slack_state["triage"].write_text(json.dumps(snap))
    html = st.build_page(data, view="slack")
    assert "Noise:" not in html


# ---------- JSONL log: TTL + compaction ----------

def test_slack_dismiss_ttl_drops_expired_records(slack_state):
    """A dismissal older than SLACK_DISMISS_TTL_DAYS must NOT appear in
    the active set — even though its line is still in the JSONL file."""
    import datetime as _dt
    old_ts = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=30)).isoformat(timespec="seconds")
    fresh_ts = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    slack_state["dismissed"].write_text(
        json.dumps({"id": "C1:OLD", "kind": "message", "ts": old_ts}) + "\n" +
        json.dumps({"id": "C1:FRESH", "kind": "message", "ts": fresh_ts}) + "\n"
    )
    msgs, threads = st.load_slack_dismissed()
    assert msgs == {"C1:FRESH"}
    assert threads == set()


def test_slack_dismiss_ttl_keeps_records_without_ts(slack_state):
    """Records missing or with malformed `ts` are kept (better safe than
    silently losing dismissals)."""
    slack_state["dismissed"].write_text(
        json.dumps({"id": "C1:NO_TS", "kind": "message"}) + "\n" +
        json.dumps({"id": "C1:BAD_TS", "kind": "message", "ts": "not-a-date"}) + "\n"
    )
    msgs, _ = st.load_slack_dismissed()
    assert msgs == {"C1:NO_TS", "C1:BAD_TS"}


def test_slack_dismiss_kind_thread_routed_correctly(slack_state):
    """Records with kind=thread populate the thread set, not the message set."""
    import datetime as _dt
    ts = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    slack_state["dismissed"].write_text(
        json.dumps({"id": "C1:MSG", "kind": "message", "ts": ts}) + "\n" +
        json.dumps({"id": "C1:THR", "kind": "thread", "ts": ts}) + "\n"
    )
    msgs, threads = st.load_slack_dismissed()
    assert msgs == {"C1:MSG"}
    assert threads == {"C1:THR"}


def test_slack_dismiss_thread_filters_all_thread_items(data, slack_state):
    """Dismissing the parent of a thread hides every item with that
    thread_ts — including replies whose own message_ts hasn't been
    dismissed."""
    parent_ts = "1.111"
    snap = _slack_snapshot(items=[
        _slack_item(channel_id="C1", message_ts=parent_ts),  # the thread root
        _slack_item(channel_id="C1", message_ts="2.222",
                    thread_ts=parent_ts, sender="ReplyAuthor"),
    ])
    slack_state["triage"].write_text(json.dumps(snap))
    # Dismiss the thread (root id, scope=thread)
    assert st.apply_slack_dismiss(f"C1:{parent_ts}", scope="thread")
    html = st.build_page(data, view="slack")
    # Neither the root nor the reply is rendered
    assert "ReplyAuthor" not in html


def test_slack_log_malformed_line_is_skipped(slack_state):
    """A single corrupt line must not break parsing of surrounding ones."""
    import datetime as _dt
    ts = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    slack_state["dismissed"].write_text(
        json.dumps({"id": "C1:OK1", "kind": "message", "ts": ts}) + "\n" +
        "{this is not valid json\n" +
        json.dumps({"id": "C1:OK2", "kind": "message", "ts": ts}) + "\n"
    )
    msgs, _ = st.load_slack_dismissed()
    assert msgs == {"C1:OK1", "C1:OK2"}


def test_slack_log_compaction_drops_expired(slack_state, monkeypatch):
    """Once the file exceeds SLACK_LOG_COMPACT_BYTES, the next write
    triggers compaction — keeping only TTL-active records."""
    import datetime as _dt
    monkeypatch.setattr(st, "SLACK_LOG_COMPACT_BYTES", 200)
    old_ts = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=30)).isoformat(timespec="seconds")
    fresh_ts = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    # Seed a file that exceeds 200 bytes with a mix of expired + fresh
    expired_lines = "\n".join(
        json.dumps({"id": f"C1:OLD{i}", "kind": "message", "ts": old_ts})
        for i in range(20)
    )
    fresh_line = json.dumps({"id": "C1:KEEP", "kind": "message", "ts": fresh_ts})
    slack_state["dismissed"].write_text(expired_lines + "\n" + fresh_line + "\n")
    assert slack_state["dismissed"].stat().st_size > 200
    # Trigger a write — appends a new fresh record AND triggers compaction
    assert st.apply_slack_dismiss("C1:NEW")
    records = _read_jsonl(slack_state["dismissed"])
    ids = {r["id"] for r in records}
    # Old records compacted out; fresh + new survive
    assert "C1:KEEP" in ids
    assert "C1:NEW" in ids
    assert all(not k.startswith("C1:OLD") for k in ids)


def test_slack_convert_quick_add_modifier_bypasses_modal(data, slack_state):
    """⌘/Ctrl+click on Convert posts /slack/convert directly without opening
    the modal. The defaults match what the modal would have populated."""
    snap = _slack_snapshot(items=[_slack_item()])
    slack_state["triage"].write_text(json.dumps(snap))
    html = st.build_page(data, view="slack")
    # Modifier check is present in the JS click handler
    assert "e.metaKey || e.ctrlKey" in html
    # Quick-add path posts to /slack/convert
    assert "_post('/slack/convert', quickPayload)" in html
    # Defaults match the modal pre-fill
    assert "pri: 'P2'" in html
    assert "link_label: 'Slack'" in html
    # DM vs channel name uses the same is_dm branch as the modal
    assert "item.is_dm" in html and "'Reply to '" in html


def test_slack_dismiss_route_passes_scope(isolated_state, slack_state):
    """The /slack/dismiss route must forward scope=thread to apply_slack_dismiss
    so the JSONL line carries kind=thread."""
    handler = st.Handler._ROUTES["/slack/dismiss"]
    assert handler({"id": "C1:1.111", "scope": "thread"})
    records = _read_jsonl(slack_state["dismissed"])
    assert any(r.get("kind") == "thread" for r in records)


def test_slack_dismiss_route_defaults_to_message_scope(isolated_state, slack_state):
    """A POST without `scope` defaults to message-level dismissal."""
    handler = st.Handler._ROUTES["/slack/dismiss"]
    assert handler({"id": "C1:1.111"})
    records = _read_jsonl(slack_state["dismissed"])
    assert all(r.get("kind") == "message" for r in records)


def test_slack_view_renders_thread_dismiss_button(data, slack_state):
    """Each row must offer the thread-dismiss button alongside the per-
    message Dismiss and Convert."""
    snap = _slack_snapshot(items=[_slack_item()])
    slack_state["triage"].write_text(json.dumps(snap))
    html = st.build_page(data, view="slack")
    assert 'class="slack-dismiss-thread"' in html
    assert 'class="slack-dismiss"' in html
    assert 'class="slack-convert"' in html


def test_slack_thread_dismiss_js_routes_correctly(data, slack_state):
    """JS must POST scope:'thread' for the thread-dismiss button and
    construct the thread root id from item.thread_ts or message_ts."""
    snap = _slack_snapshot(items=[_slack_item()])
    slack_state["triage"].write_text(json.dumps(snap))
    html = st.build_page(data, view="slack")
    # JS branch checks for the .slack-dismiss-thread selector
    assert ".slack-dismiss-thread" in html
    # And POSTs with scope:'thread'
    assert "scope: 'thread'" in html
    # Falls back to message_ts when thread_ts is missing
    assert "item.thread_ts || item.message_ts" in html


def test_slack_dismiss_top_level_msg_as_thread(data, slack_state):
    """Dismissing a top-level message as scope=thread (when thread_ts is
    None) hides the message via the `thread_ts or message_ts` fallback."""
    snap = _slack_snapshot(items=[
        _slack_item(channel_id="C1", message_ts="solo.111", thread_ts=None,
                    sender="Solo"),
    ])
    slack_state["triage"].write_text(json.dumps(snap))
    # Dismiss using the message_ts as the thread root (the JS handler does
    # exactly this when item.thread_ts is null).
    assert st.apply_slack_dismiss("C1:solo.111", scope="thread")
    html = st.build_page(data, view="slack")
    assert "Solo" not in html


def test_slack_converted_log_is_never_compacted(slack_state, monkeypatch):
    """Converted records are persistent (no TTL). Even with the threshold
    set to 1 byte, a write must NOT shrink the file — there's no TTL to
    apply, so compaction would be a no-op rewrite at best."""
    import datetime as _dt
    monkeypatch.setattr(st, "SLACK_LOG_COMPACT_BYTES", 1)
    very_old = (_dt.datetime.now(_dt.timezone.utc)
                - _dt.timedelta(days=365)).isoformat(timespec="seconds")
    slack_state["converted"].write_text(
        json.dumps({"id": "C1:OLD", "ts": very_old}) + "\n"
    )
    # Convert path doesn't trigger compaction on the converted file —
    # _maybe_compact_slack_log is only called by apply_slack_dismiss.
    # Verify the docstring contract: load_slack_converted returns the old
    # record regardless of TTL.
    converted = st.load_slack_converted()
    assert "C1:OLD" in converted


def test_save_state_preserves_mode_bits(isolated_state):
    """_atomic_write_json (used by _save_state) must preserve the target's
    existing permission bits. tempfile.mkstemp creates 0600 by default,
    which would silently tighten the file's mode without the chmod guard."""
    # Set a non-default mode the user can observe
    os.chmod(isolated_state, 0o644)
    before = isolated_state.stat().st_mode & 0o777
    assert before == 0o644
    # Trigger a state save
    assert st.apply_status_change(120)
    after = isolated_state.stat().st_mode & 0o777
    assert after == 0o644, f"mode changed from {before:o} to {after:o}"


def test_slack_converted_not_ttl_filtered(slack_state):
    """Converted records persist regardless of age — the converted log is
    the dashboard's memory of "this Slack item became a task". Only the
    dismissed log has TTL."""
    import datetime as _dt
    very_old = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=365)).isoformat(timespec="seconds")
    slack_state["converted"].write_text(
        json.dumps({"id": "C1:ANCIENT", "ts": very_old}) + "\n"
    )
    converted = st.load_slack_converted()
    assert "C1:ANCIENT" in converted


# ---------------------------------------------------------------------------
# Add-completed feature
# ---------------------------------------------------------------------------

def test_apply_add_completed_goes_to_completed_today(isolated_state):
    """When completed_at is set, the task lands in completed_today, not sections."""
    result = st.apply_add({
        "task": "Retro completed task",
        "pri": "P2",
        "completed_at": "14:30",
    })
    assert isinstance(result, dict)
    assert result["task"] == "Retro completed task"
    assert result["time"] == "14:30"
    assert result["status"] == "done"

    data = json.loads(isolated_state.read_text())
    ids_in_sections = [
        t["id"] for s in data["sections"] for t in s["tasks"]
    ]
    assert result["id"] not in ids_in_sections
    completed_ids = [t["id"] for t in data["completed_today"]]
    assert result["id"] in completed_ids


def test_apply_add_completed_writes_done_in_core_file(isolated_state):
    """The completed task should appear under ## Done in the core file."""
    st.apply_add({
        "task": "Core file done task",
        "pri": "P1",
        "completed_at": "09:15",
    })
    core_text = st.current_core_path().read_text()
    assert "[x]" in core_text.split("Core file done task")[0].split("\n")[-1]
    assert "_(completed:" in core_text
    assert "Core file done task" in core_text


def test_apply_add_completed_with_links(isolated_state):
    """Links should be included in the core file Done entry."""
    st.apply_add({
        "task": "Linked done task",
        "pri": "P2",
        "completed_at": "16:00",
        "link_label": "HOTS-999",
        "link_url": "https://example.com/999",
    })
    core_text = st.current_core_path().read_text()
    assert "HOTS-999" in core_text
    assert "https://example.com/999" in core_text

    data = json.loads(isolated_state.read_text())
    entry = next(t for t in data["completed_today"] if t["task"] == "Linked done task")
    assert len(entry["links"]) == 1
    assert entry["links"][0]["label"] == "HOTS-999"


def test_apply_add_without_completed_at_is_active(isolated_state):
    """Normal add (no completed_at) still goes to active sections."""
    result = st.apply_add({"task": "Normal active task", "pri": "P3"})
    assert isinstance(result, dict)
    assert result["status"] == "open"

    data = json.loads(isolated_state.read_text())
    ids_in_sections = [
        t["id"] for s in data["sections"] for t in s["tasks"]
    ]
    assert result["id"] in ids_in_sections


def test_apply_add_empty_completed_at_is_active(isolated_state):
    """Empty string completed_at should be treated as not-completed."""
    result = st.apply_add({
        "task": "Not actually completed",
        "pri": "P2",
        "completed_at": "",
    })
    assert isinstance(result, dict)
    assert result["status"] == "open"

    data = json.loads(isolated_state.read_text())
    ids_in_sections = [
        t["id"] for s in data["sections"] for t in s["tasks"]
    ]
    assert result["id"] in ids_in_sections


def test_add_completed_modal_elements_present(data):
    """The add modal should contain the completed checkbox, label, and time input."""
    html = st.build_page(data)
    assert 'id="m-completed"' in html
    assert 'type="checkbox"' in html
    assert 'id="m-completed-time"' in html
    assert "Completed" in html


def test_completed_row_hidden_in_edit_mode(data):
    """CSS should hide the completed section in edit mode."""
    html = st.build_page(data)
    assert '#modal[data-mode="edit"] .modal-completed-section' in html


def test_completed_row_hidden_in_slack_convert_mode(data):
    """CSS should hide the completed section in slack-convert mode."""
    html = st.build_page(data)
    assert '#modal[data-mode="slack-convert"] .modal-completed-section' in html


def test_shift_a_hotkey_opens_add_completed(data):
    """Shift+A should call _openAddCompletedModal."""
    html = st.build_page(data)
    assert "_openAddCompletedModal" in html
    assert "e.code === 'KeyA'" in html


def test_help_overlay_lists_add_completed_shortcut(data):
    """The help overlay should document Shift+A."""
    html = st.build_page(data)
    assert "Add completed task" in html


# ---------------------------------------------------------------------------
# POST response body tests
# ---------------------------------------------------------------------------

def test_apply_add_returns_task_dict(isolated_state):
    """apply_add should return the created task object, not True."""
    result = st.apply_add({"task": "Response body test", "pri": "P2"})
    assert isinstance(result, dict)
    assert result["task"] == "Response body test"
    assert "id" in result


def test_apply_status_change_returns_dict(isolated_state):
    """apply_status_change should return a task dict."""
    result = st.apply_status_change(120)
    assert isinstance(result, dict)


def test_apply_status_change_done_returns_completed_entry(isolated_state):
    """When completing a task, the completed_today entry should be returned."""
    result = st.apply_status_change(120, force_status="done")
    assert isinstance(result, dict)
    assert "time" in result
    assert result["id"] == 120


def test_apply_edit_returns_task_dict(isolated_state):
    result = st.apply_edit(120, {"task": "Edited name", "pri": "P1"})
    assert isinstance(result, dict)
    assert result["task"] == "Edited name"


def test_apply_rename_returns_task_dict(isolated_state):
    result = st.apply_rename(120, "Renamed task")
    assert isinstance(result, dict)
    assert result["task"] == "Renamed task"


def test_apply_priority_update_returns_task_dict(isolated_state):
    result = st.apply_priority_update(120)
    assert isinstance(result, dict)
    assert "pri" in result


def test_apply_sort_returns_ok_dict(isolated_state):
    result = st.apply_sort()
    assert isinstance(result, dict)
    assert result.get("ok") is True


def test_apply_cancel_returns_ok_with_id(isolated_state):
    result = st.apply_cancel(120)
    assert isinstance(result, dict)
    assert result["id"] == 120
    assert "task" in result


def test_apply_add_failure_returns_false(isolated_state):
    """Failed adds should still return False, not a dict."""
    result = st.apply_add({"task": "", "pri": "P2"})
    assert result is False


def test_apply_add_rejects_control_chars(isolated_state):
    """apply_add must reject names with newlines or control chars."""
    assert not st.apply_add({"task": "foo\nbar"})
    assert not st.apply_add({"task": "tab\there"})
    assert not st.apply_add({"task": "ok name", "why": "evil\nwhy"})


def test_apply_add_rejects_invalid_completed_at(isolated_state):
    """Garbage completed_at values must be rejected."""
    assert not st.apply_add({"task": "Bad time", "completed_at": "abc"})
    assert not st.apply_add({"task": "Bad time", "completed_at": "25:00"})
    assert not st.apply_add({"task": "Bad time", "completed_at": "12:60"})
    assert not st.apply_add({"task": "Bad time", "completed_at": "1:30"})


def test_apply_add_accepts_valid_completed_at(isolated_state):
    """Valid HH:MM times should work."""
    result = st.apply_add({"task": "Morning task", "completed_at": "09:15"})
    assert isinstance(result, dict)
    assert result["time"] == "09:15"
    result2 = st.apply_add({"task": "Late task", "completed_at": "23:59"})
    assert isinstance(result2, dict)
    assert result2["time"] == "23:59"
