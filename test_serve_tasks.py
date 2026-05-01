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
    Default has button labels visible; .compact / .compact-tight classes
    apply progressively when JS detects the topbar has wrapped."""
    html = st.build_page(data, view="dashboard")
    # Default visible — no display:none on .btn-label
    assert ".btn-label { display: inline; }" in html
    # JS function and the two compaction tiers exist
    assert "function _autoCompactTopbar()" in html
    assert "#topbar.compact " in html and "#topbar.compact-tight " in html
    # The tier-1 class hides button labels (the cheapest space win)
    assert "#topbar.compact .btn-label { display: none; }" in html
    # No viewport-threshold media queries on .btn-label or the topbar tiers
    assert "@media (min-width: 900px)" not in html
    assert "@media (max-width: 1040px)" not in html
    assert "@media (max-width: 860px)" not in html


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
