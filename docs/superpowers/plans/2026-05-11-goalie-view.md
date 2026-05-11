# Goalie View Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a dedicated Goalie view to the tasks dashboard that shows Today's Focus and interactive goalie subsections (Start here / Then / Handover) sourced from the daily journal.

**Architecture:** `build_tasks.py` reads `on_goalie` from the goalie cache and, when true, calls `tasklib.parse_goalie_sections()` to emit `"type": "goalie"` sections in the JSON. `serve-tasks.py` gains a new `_build_goalie_body()` and routes `?view=goalie` to it; `render_goalie_section` is updated to emit interactive rows identical to core task rows.

**Tech Stack:** Python 3, standard library only. Run tests with `python3 -m pytest test_build_tasks.py test_serve_tasks.py -q`.

---

### Task 1: Wire goalie section parsing into `build_tasks.py`

**Files:**
- Modify: `build_tasks.py:399-425` (no-silent-delete check + add goalie building after it)
- Modify: `test_build_tasks.py` (add `on_goalie` param to `_run` + new test class)

- [ ] **Step 1: Extend `_run` helper in `test_build_tasks.py` to accept `on_goalie` flag**

In `test_build_tasks.py`, change the `_run` method signature in `TestCLI` from:
```python
def _run(self, core_text, journal_text="", existing=None, jira=None, slack=None):
```
to:
```python
def _run(self, core_text, journal_text="", existing=None, jira=None, slack=None, on_goalie=False):
```

And change the hardcoded goalie cache write from:
```python
goalie_p.write_text(json.dumps({"on_goalie": False}))
```
to:
```python
goalie_p.write_text(json.dumps({"on_goalie": on_goalie}))
```

- [ ] **Step 2: Write three failing tests for goalie section building**

Add this class to `test_build_tasks.py` (after the existing `TestCLI` class):

```python
class TestGoalieSections(TestCLI):

    def _journal_with_goalie(self, start_here=None, then=None, handover=None):
        today = datetime.date.today()
        lines = [
            f"## {today.strftime('%A')} {today.isoformat()}",
            "",
            "### Plan",
            "",
            "#### Goalie",
            "",
        ]
        for title, tasks in (("Start here", start_here), ("Then", then), ("Handover", handover)):
            if tasks is not None:
                lines += [f"##### {title}", ""]
                lines += [f"- [ ] {t}" for t in tasks]
                lines += [""]
        lines += ["### Done"]
        return "\n".join(lines)

    def test_goalie_sections_emitted_when_on_rotation(self):
        journal = self._journal_with_goalie(
            start_here=["[VCSUP-1234](https://example.com) — Fix the thing"],
            then=["Check handover items"],
        )
        core = "- [ ] 🟠 Core task\n\n## Done\n"
        code, data = self._run(core, journal, on_goalie=True)
        self.assertEqual(code, 0)
        goalie = [s for s in data["sections"] if s.get("type") == "goalie"]
        self.assertEqual(len(goalie), 2)
        titles = [s["title"] for s in goalie]
        self.assertIn("Start here", titles)
        self.assertIn("Then", titles)
        self.assertNotIn("Handover", titles)  # empty subsection omitted

    def test_goalie_sections_absent_when_off_rotation(self):
        journal = self._journal_with_goalie(start_here=["Some goalie task"])
        core = "- [ ] 🟠 Core task\n\n## Done\n"
        code, data = self._run(core, journal, on_goalie=False)
        self.assertEqual(code, 0)
        goalie = [s for s in data["sections"] if s.get("type") == "goalie"]
        self.assertEqual(len(goalie), 0)

    def test_goalie_task_id_stability(self):
        journal = self._journal_with_goalie(start_here=["Existing goalie task"])
        core = "- [ ] 🟠 Core task\n\n## Done\n"
        existing = {
            "updated": "2026-01-01 00:00",
            "sections": [
                {"type": "goalie", "title": "Start here", "tasks": [
                    {"id": 77, "task": "Existing goalie task", "status": "open"}
                ]}
            ],
            "completed_today": [],
        }
        code, data = self._run(core, journal, existing=existing, on_goalie=True)
        self.assertEqual(code, 0)
        goalie = [s for s in data["sections"] if s.get("type") == "goalie"]
        self.assertEqual(len(goalie), 1)
        self.assertEqual(goalie[0]["tasks"][0]["id"], 77)
```

- [ ] **Step 3: Run tests to verify they fail**

```
python3 -m pytest test_build_tasks.py::TestGoalieSections -v
```
Expected: 3 failures — `AssertionError` because no goalie sections are emitted yet.

- [ ] **Step 4: Implement goalie section building in `build_tasks.py`**

In `build_tasks.py`, after `make_section(tasklib.SEC_LOW, low_tasks)` (line ~369), add:

```python
    # ── Build goalie sections (when on rotation) ────────────────────────────
    goalie_cache = load_json(args.goalie_cache, {})
    if goalie_cache.get("on_goalie"):
        goalie_raw = tasklib.parse_goalie_sections(journal_lines, weekday_header)
        for subsection in ("Start here", "Then", "Handover"):
            parsed_list = goalie_raw.get(subsection, [])
            open_tasks = [
                p for p in parsed_list
                if p.get("marker") not in ("[x]", "[/]")
                and p.get("status_override") not in ("done", "cancelled")
            ]
            if not open_tasks:
                continue
            tasks = []
            for p in open_tasks:
                tid = assign_id(p, existing_lookup, completed_lookup, id_counter)
                obj = build_task_object(
                    p, tid, num, today_str, existing_lookup, jira_cache, slack_cache
                )
                tasks.append(obj)
                num += 1
            sections.append({"type": "goalie", "title": subsection, "tasks": tasks})
```

- [ ] **Step 5: Exclude existing goalie sections from the no-silent-delete check**

In `build_tasks.py`, in the no-silent-delete block (line ~412), change:
```python
        for section in existing_data.get("sections", []):
            for task in section.get("tasks", []):
```
to:
```python
        for section in existing_data.get("sections", []):
            if section.get("type") == "goalie":
                continue  # goalie sections are rebuilt from journal; not browser-writable
            for task in section.get("tasks", []):
```

- [ ] **Step 6: Run tests to verify they pass**

```
python3 -m pytest test_build_tasks.py -v
```
Expected: all tests pass, including the 3 new `TestGoalieSections` tests.

- [ ] **Step 7: Commit**

```bash
git add build_tasks.py test_build_tasks.py
git commit -m "feat: emit goalie sections from journal in build_tasks.py"
```

---

### Task 2: Make `render_goalie_section` interactive

**Files:**
- Modify: `serve-tasks.py:2405-2431` (`render_goalie_section`)
- Modify: `test_serve_tasks.py` (new tests for interactive row attributes)

- [ ] **Step 1: Write failing tests for interactive goalie rows**

Add to `test_serve_tasks.py`:

```python
class TestRenderGoalieSection:

    def test_goalie_row_has_draggable_and_data_id(self):
        tasks = [
            {"id": 42, "num": 1, "task": "Fix the thing",
             "links": [{"label": "VCSUP-1", "url": "https://example.com"}],
             "status": "waiting_support"},
        ]
        html = st.render_goalie_section("Start here", tasks)
        assert 'draggable="true"' in html
        assert 'data-id="42"' in html

    def test_goalie_num_cell_has_num_class(self):
        tasks = [{"id": 7, "num": 1, "task": "A task", "links": [], "status": "open"}]
        html = st.render_goalie_section("Then", tasks)
        assert 'class="num"' in html

    def test_goalie_empty_returns_empty_string(self):
        assert st.render_goalie_section("Start here", []) == ""

    def test_goalie_section_uses_goalie_color(self):
        tasks = [{"id": 1, "num": 1, "task": "A task", "links": [], "status": "open"}]
        html = st.render_goalie_section("Start here", tasks)
        assert "#bc8cff" in html
```

- [ ] **Step 2: Run tests to verify they fail**

```
SERVE_TASKS_NO_WATCH=1 python3 -m pytest test_serve_tasks.py::TestRenderGoalieSection -v
```
Expected: `test_goalie_row_has_draggable_and_data_id` and `test_goalie_num_cell_has_num_class` fail; `test_goalie_empty_returns_empty_string` and `test_goalie_section_uses_goalie_color` may also fail.

- [ ] **Step 3: Update `render_goalie_section` in `serve-tasks.py`**

Replace the entire `render_goalie_section` function (lines 2405–2431) with:

```python
def render_goalie_section(title, tasks):
    if not tasks:
        return ""
    color = SECTION_COLORS["goalie"]
    rows = []
    for t in tasks:
        rc = row_classes(t)
        task_id = t.get("id", t.get("num", ""))
        drag_attrs = f' draggable="true" data-id="{task_id}"'
        filter_attrs = _filter_data_attrs(t)
        rows.append(
            f'<tr{rc}{drag_attrs}{filter_attrs}>'
            f'<td class="num" data-id="{task_id}">{task_id}</td>'
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
```

- [ ] **Step 4: Run tests to verify they pass**

```
SERVE_TASKS_NO_WATCH=1 python3 -m pytest test_serve_tasks.py::TestRenderGoalieSection -v
```
Expected: all 4 tests pass.

- [ ] **Step 5: Run full test suite to check for regressions**

```
SERVE_TASKS_NO_WATCH=1 python3 -m pytest test_serve_tasks.py test_build_tasks.py -q
```
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add serve-tasks.py test_serve_tasks.py
git commit -m "feat: make render_goalie_section emit interactive rows"
```

---

### Task 3: Add goalie view to `serve-tasks.py`

**Files:**
- Modify: `serve-tasks.py:2638` (`VIEWS`), `serve-tasks.py:2977-2986` (`build_page`), add `_build_goalie_body` before `_build_slack_body`
- Modify: `test_serve_tasks.py` (new tests for goalie view)

- [ ] **Step 1: Write failing tests for the goalie view**

Add to `test_serve_tasks.py`:

```python
def _goalie_fixture():
    """Data with Today's Focus + one goalie section."""
    return {
        "updated": "2026-05-11 09:00",
        "week": "W20",
        "sections": [
            {
                "title": "Today's Focus",
                "tasks": [
                    {"id": 1, "num": 1, "pri": "P2", "task": "Focus task",
                     "due": "—", "from": "W20", "added": "2026-05-11",
                     "links": [], "status": "open", "why": "—"},
                ],
            },
            {
                "type": "goalie",
                "title": "Start here",
                "tasks": [
                    {"id": 100, "num": 10, "task": "VCSUP goalie task",
                     "links": [{"label": "VCSUP-1", "url": "https://example.com"}],
                     "status": "waiting_support"},
                ],
            },
        ],
        "completed_today": [],
        "counts": "✅ 0 core tasks completed this week",
    }


class TestGoalieView:

    def test_goalie_in_views(self):
        assert "goalie" in st.VIEWS

    def test_goalie_view_renders_focus(self):
        html = st.build_page(_goalie_fixture(), view="goalie")
        assert "Focus task" in html
        assert "Today&#x27;s Focus" in html or "Today's Focus" in html

    def test_goalie_view_renders_goalie_section(self):
        html = st.build_page(_goalie_fixture(), view="goalie")
        assert "VCSUP goalie task" in html
        assert "Start here" in html

    def test_goalie_view_off_rotation_message(self):
        data = {
            "updated": "2026-05-11 09:00",
            "week": "W20",
            "sections": [
                {
                    "title": "Today's Focus",
                    "tasks": [
                        {"id": 1, "num": 1, "pri": "P2", "task": "Focus task",
                         "due": "—", "from": "W20", "added": "2026-05-11",
                         "links": [], "status": "open", "why": "—"},
                    ],
                },
            ],
            "completed_today": [],
            "counts": "✅ 0 core tasks completed this week",
        }
        html = st.build_page(data, view="goalie")
        assert "Not on goalie rotation this week" in html

    def test_goalie_view_does_not_show_high_priority_section(self):
        data = _goalie_fixture()
        data["sections"].append({
            "title": "High Priority",
            "tasks": [
                {"id": 200, "num": 2, "pri": "P1", "task": "Core high task",
                 "due": "—", "from": "W20", "added": "2026-05-11",
                 "links": [], "status": "open", "why": "—"},
            ],
        })
        html = st.build_page(data, view="goalie")
        assert "Core high task" not in html
```

- [ ] **Step 2: Run tests to verify they fail**

```
SERVE_TASKS_NO_WATCH=1 python3 -m pytest test_serve_tasks.py::TestGoalieView -v
```
Expected: `test_goalie_in_views` fails ("goalie" not in VIEWS); others fail similarly.

- [ ] **Step 3: Add `"goalie"` to `VIEWS` in `serve-tasks.py`**

Change line 2638:
```python
VIEWS = ["dashboard", "classic", "slack"]
```
to:
```python
VIEWS = ["dashboard", "classic", "goalie", "slack"]
```

- [ ] **Step 4: Add `_build_goalie_body` function to `serve-tasks.py`**

Insert this function just before `_build_slack_body` (before the line `def _build_slack_body(data, week):`):

```python
def _build_goalie_body(data, week):
    """Goalie view: Today's Focus + goalie subsections (Start here / Then / Handover).
    Shows an off-rotation message when no goalie sections are present."""
    parts = []

    def card(html, variant=""):
        if not html:
            return ""
        cls = "task-card" + (f" {variant}" if variant else "")
        return f'<div class="{cls}">{html}</div>'

    focus = next(
        (s for s in data.get("sections", []) if s.get("title") == SEC_FOCUS), None
    )
    if focus:
        focus_sub = _focus_progress(data)
        parts.append(card(
            render_core_section(SEC_FOCUS, focus.get("tasks", []), week, subtitle=focus_sub),
            variant="focus",
        ))

    goalie_sections = [s for s in data.get("sections", []) if s.get("type") == "goalie"]
    if goalie_sections:
        for section in goalie_sections:
            parts.append(card(render_goalie_section(section["title"], section.get("tasks", []))))
    else:
        parts.append(
            '<div class="task-card">'
            '<p style="color:#484f58;padding:12px 0;margin:0">'
            '<em>Not on goalie rotation this week.</em></p>'
            '</div>'
        )

    if data.get("updated"):
        parts.append(
            f'<p class="counts" style="margin-top:16px;color:#484f58">'
            f'Updated {h(data["updated"])}</p>\n'
        )
    return "".join(parts)
```

- [ ] **Step 5: Route `view="goalie"` in `build_page`**

In `build_page` (around line 2981), change:
```python
    if view == "slack":
        body = _build_slack_body(data, week)
    elif view == "classic":
        body = _build_classic_body(data, week)
    else:
        body = _build_dashboard_body(data, week)
```
to:
```python
    if view == "slack":
        body = _build_slack_body(data, week)
    elif view == "classic":
        body = _build_classic_body(data, week)
    elif view == "goalie":
        body = _build_goalie_body(data, week)
    else:
        body = _build_dashboard_body(data, week)
```

- [ ] **Step 6: Run tests to verify they pass**

```
SERVE_TASKS_NO_WATCH=1 python3 -m pytest test_serve_tasks.py::TestGoalieView -v
```
Expected: all 5 tests pass.

- [ ] **Step 7: Run full test suite**

```
SERVE_TASKS_NO_WATCH=1 python3 -m pytest test_serve_tasks.py test_build_tasks.py -q
```
Expected: all tests pass. Note the count and report if any fail.

- [ ] **Step 8: Commit**

```bash
git add serve-tasks.py test_serve_tasks.py
git commit -m "feat: add goalie view with Today's Focus and goalie sections"
```
