#!/usr/bin/env python3
"""Tests for tasklib.py and build_tasks.py."""

import datetime
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import tasklib
import build_tasks


# ── tasklib.parse_task_line ──────────────────────────────────────────────────

class TestParseTaskLine(unittest.TestCase):

    def test_simple_open_task(self):
        r = tasklib.parse_task_line("- [ ] Simple task")
        self.assertIsNotNone(r)
        self.assertEqual(r["task"], "Simple task")
        self.assertEqual(r["marker"], "[ ]")
        self.assertIsNone(r["status_override"])
        self.assertIsNone(r["pri"])
        self.assertIsNone(r["due"])
        self.assertEqual(r["links"], [])
        self.assertIsNone(r["why"])
        self.assertIsNone(r["from_week"])

    def test_full_metadata_task(self):
        line = (
            "- [-] 🟠 Scan codebase for hardcoded GHES host references"
            " — due 2026-05-10"
            " ([HOTS-1873](https://spotify.atlassian.net/browse/HOTS-1873))"
            " _(carried from W18)_"
            " _(why: high priority migration blocker)_"
        )
        r = tasklib.parse_task_line(line)
        self.assertEqual(r["task"], "Scan codebase for hardcoded GHES host references")
        self.assertEqual(r["marker"], "[-]")
        self.assertEqual(r["status_override"], "in_progress")
        self.assertEqual(r["pri"], "P2")
        self.assertEqual(r["due"], "2026-05-10")
        self.assertEqual(len(r["links"]), 1)
        self.assertEqual(r["links"][0]["label"], "HOTS-1873")
        self.assertEqual(r["from_week"], "W18")
        self.assertEqual(r["why"], "high priority migration blocker")

    def test_waiting_task(self):
        r = tasklib.parse_task_line("- [~] 🟡 Follow up with David _(why: waiting on reply)_")
        self.assertEqual(r["status_override"], "waiting")
        self.assertEqual(r["pri"], "P3")
        self.assertEqual(r["why"], "waiting on reply")

    def test_blocked_task(self):
        r = tasklib.parse_task_line("- [!] 🔴 Blocked task")
        self.assertEqual(r["status_override"], "blocked")
        self.assertEqual(r["pri"], "P1")

    def test_done_task_with_timestamp(self):
        line = "- [x] 🔴 Install ELM v0.2.3 _(completed: 2026-05-05 00:46)_"
        r = tasklib.parse_task_line(line)
        self.assertEqual(r["status_override"], "done")
        self.assertEqual(r["completed_time"], "2026-05-05 00:46")

    def test_cancelled_task(self):
        r = tasklib.parse_task_line("- [/] 🟠 Cancelled task _(cancelled: 2026-05-04 12:50)_")
        self.assertEqual(r["status_override"], "cancelled")

    def test_multi_link_task(self):
        line = (
            "- [ ] 🟠 PRs comparison issue"
            " ([INCIDENT-23535](https://jira.example.com/INCIDENT-23535)"
            " · [GH Support](https://support.github.com/ticket/123))"
        )
        r = tasklib.parse_task_line(line)
        self.assertEqual(len(r["links"]), 2)
        self.assertEqual(r["links"][0]["label"], "INCIDENT-23535")
        self.assertEqual(r["links"][1]["label"], "GH Support")

    def test_p1_priority(self):
        r = tasklib.parse_task_line("- [ ] 🔴 Critical task")
        self.assertEqual(r["pri"], "P1")

    def test_p5_priority(self):
        r = tasklib.parse_task_line("- [ ] ⏸️ Low priority task")
        self.assertEqual(r["pri"], "P5")

    def test_no_priority(self):
        r = tasklib.parse_task_line("- [ ] No priority task")
        self.assertIsNone(r["pri"])

    def test_not_a_task_line(self):
        self.assertIsNone(tasklib.parse_task_line("## Done"))
        self.assertIsNone(tasklib.parse_task_line("Just some text"))
        self.assertIsNone(tasklib.parse_task_line(""))

    def test_indented_task(self):
        r = tasklib.parse_task_line("  - [ ] Indented task")
        self.assertIsNotNone(r)
        self.assertEqual(r["task"], "Indented task")

    def test_carried_from_stripped(self):
        line = "- [ ] 🟠 Some task _(carried from W18)_"
        r = tasklib.parse_task_line(line)
        self.assertEqual(r["task"], "Some task")
        self.assertEqual(r["from_week"], "W18")

    def test_due_time_only(self):
        line = "- [ ] 🟠 Task — due 17:00"
        r = tasklib.parse_task_line(line)
        self.assertEqual(r["due"], "17:00")

    def test_due_date(self):
        line = "- [ ] 🟠 Task — due 2026-05-10"
        r = tasklib.parse_task_line(line)
        self.assertEqual(r["due"], "2026-05-10")


# ── tasklib section helpers ──────────────────────────────────────────────────

class TestSectionHelpers(unittest.TestCase):

    CORE = [
        "# Core Work — 2026-W19",
        "",
        "- [-] 🔴 Task A",
        "- [ ] 🟠 Task B",
        "- [~] 🟡 Task C _(why: waiting)_",
        "",
        "## Done",
        "",
        "### 2026-05-05",
        "- [x] 🔴 Task D _(completed: 2026-05-05 09:00)_",
        "- [x] 🟠 Task E _(completed: 2026-05-05 10:00)_",
        "",
        "### 2026-05-04",
        "- [x] 🟡 Task F _(completed: 2026-05-04 14:00)_",
        "",
        "## Cancelled",
        "",
        "### 2026-05-04",
        "- [/] 🟠 Task G _(cancelled: 2026-05-04 12:50)_",
    ]

    def test_done_boundary(self):
        self.assertEqual(tasklib.done_boundary(self.CORE), 6)

    def test_cancelled_boundary(self):
        self.assertEqual(tasklib.cancelled_boundary(self.CORE), 15)

    def test_parse_active_tasks(self):
        tasks = tasklib.parse_active_tasks(self.CORE)
        self.assertEqual(len(tasks), 3)
        self.assertEqual(tasks[0]["task"], "Task A")
        self.assertEqual(tasks[1]["task"], "Task B")
        self.assertEqual(tasks[2]["task"], "Task C")

    def test_parse_done_section_all(self):
        done = tasklib.parse_done_section(self.CORE)
        self.assertEqual(len(done), 3)
        self.assertEqual(done[0]["done_date"], "2026-05-05")
        self.assertEqual(done[2]["done_date"], "2026-05-04")

    def test_parse_done_section_filtered(self):
        done = tasklib.parse_done_section(self.CORE, target_date="2026-05-05")
        self.assertEqual(len(done), 2)
        self.assertTrue(all(d["done_date"] == "2026-05-05" for d in done))

    def test_parse_done_excludes_cancelled(self):
        done = tasklib.parse_done_section(self.CORE)
        names = [d["task"] for d in done]
        self.assertNotIn("Task G", names)

    def test_no_done_section(self):
        lines = ["- [ ] Task A", "- [ ] Task B"]
        self.assertEqual(tasklib.done_boundary(lines), 2)
        self.assertEqual(tasklib.parse_done_section(lines), [])


# ── tasklib date helpers ─────────────────────────────────────────────────────

class TestDateHelpers(unittest.TestCase):

    @patch("tasklib.datetime")
    def test_week_monday(self, mock_dt):
        mock_dt.date.today.return_value = datetime.date(2026, 5, 5)
        mock_dt.date.side_effect = lambda *a, **k: datetime.date(*a, **k)
        mock_dt.timedelta = datetime.timedelta
        result = tasklib.week_monday("W18")
        self.assertEqual(result, "2026-04-27")

    @patch("tasklib.datetime")
    def test_week_monday_w19(self, mock_dt):
        mock_dt.date.today.return_value = datetime.date(2026, 5, 5)
        mock_dt.date.side_effect = lambda *a, **k: datetime.date(*a, **k)
        mock_dt.timedelta = datetime.timedelta
        result = tasklib.week_monday("W19")
        self.assertEqual(result, "2026-05-04")

    def test_format_due_no_due(self):
        self.assertEqual(tasklib.format_due(None, "2026-05-05"), "—")

    def test_format_due_time(self):
        self.assertEqual(tasklib.format_due("17:00", "2026-05-05"), "17:00")

    def test_format_due_today(self):
        self.assertEqual(tasklib.format_due("2026-05-05", "2026-05-05"), "today")

    def test_format_due_overdue(self):
        self.assertEqual(tasklib.format_due("2026-04-29", "2026-05-05"), "⚠️ 2026-04-29")

    def test_format_due_future(self):
        self.assertEqual(tasklib.format_due("2026-05-10", "2026-05-05"), "2026-05-10")


# ── tasklib journal parsing ──────────────────────────────────────────────────

class TestJournalParsing(unittest.TestCase):

    JOURNAL = [
        "# Week of 2026-05-04",
        "",
        "## Monday 2026-05-04",
        "",
        "### Plan",
        "#### Core Focus",
        "1. Task Alpha",
        "2. Task Beta",
        "",
        "### Done",
        "",
        "## Tuesday 2026-05-05",
        "",
        "### Plan",
        "",
        "#### Core Focus",
        "1. Scan codebase for hardcoded GHES host references",
        "2. Performance@Spotify Learning Journey",
        "3. Review monorepo migration progress",
        "",
        "### Done",
        "",
        "### Notes",
    ]

    def test_parse_core_focus_tuesday(self):
        tasks = tasklib.parse_core_focus(self.JOURNAL, "## Tuesday 2026-05-05")
        self.assertEqual(len(tasks), 3)
        self.assertEqual(tasks[0], "Scan codebase for hardcoded GHES host references")
        self.assertEqual(tasks[2], "Review monorepo migration progress")

    def test_parse_core_focus_monday(self):
        tasks = tasklib.parse_core_focus(self.JOURNAL, "## Monday 2026-05-04")
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0], "Task Alpha")

    def test_parse_core_focus_missing_day(self):
        tasks = tasklib.parse_core_focus(self.JOURNAL, "## Wednesday 2026-05-06")
        self.assertEqual(tasks, [])

    def test_no_core_focus_section(self):
        journal = [
            "## Tuesday 2026-05-05",
            "### Plan",
            "### Done",
        ]
        tasks = tasklib.parse_core_focus(journal, "## Tuesday 2026-05-05")
        self.assertEqual(tasks, [])


# ── tasklib counts ───────────────────────────────────────────────────────────

class TestCounts(unittest.TestCase):

    def test_counts_string(self):
        core = [
            "- [ ] Task A",
            "## Done",
            "### 2026-05-05",
            "- [x] Task B",
            "- [x] Task C",
            "### 2026-05-04",
            "- [x] Task D",
        ]
        result = tasklib.build_counts_string(core)
        self.assertIn("3 core tasks completed", result)
        self.assertIn("2 on 2026-05-05", result)
        self.assertIn("1 on 2026-05-04", result)

    def test_counts_empty(self):
        result = tasklib.build_counts_string(["- [ ] Task A"])
        self.assertEqual(result, "✅ 0 core tasks completed this week")


# ── build_tasks integration ──────────────────────────────────────────────────

class TestBuildTasks(unittest.TestCase):

    def test_resolve_status_marker_wins(self):
        parsed = {"status_override": "in_progress", "links": []}
        self.assertEqual(build_tasks.resolve_status(parsed, {}, {}), "in_progress")

    def test_resolve_status_jira(self):
        parsed = {"status_override": None, "links": [{"label": "HOTS-1873", "url": ""}]}
        jira = {"HOTS-1873": {"status": "In Progress"}}
        self.assertEqual(build_tasks.resolve_status(parsed, jira, {}), "in_progress")

    def test_resolve_status_jira_backlog(self):
        parsed = {"status_override": None, "links": [{"label": "HOTS-1985", "url": ""}]}
        jira = {"HOTS-1985": {"status": "Backlog"}}
        self.assertEqual(build_tasks.resolve_status(parsed, jira, {}), "todo")

    def test_resolve_status_default_open(self):
        parsed = {"status_override": None, "links": []}
        self.assertEqual(build_tasks.resolve_status(parsed, {}, {}), "open")

    def test_existing_lookup(self):
        data = {
            "sections": [
                {"title": "High Priority", "tasks": [
                    {"task": "My Task", "status": "in_progress", "due": "—", "id": 5}
                ]}
            ]
        }
        lookup = build_tasks.build_existing_lookup(data)
        self.assertIn("my task", lookup)
        self.assertEqual(lookup["my task"]["status"], "in_progress")
        self.assertEqual(lookup["my task"]["id"], 5)

    def test_max_existing_id(self):
        data = {
            "sections": [{"title": "X", "tasks": [{"id": 10}, {"id": 5}]}],
            "completed_today": [{"id": 20}],
        }
        self.assertEqual(build_tasks.max_existing_id(data), 20)

    def test_max_existing_id_empty(self):
        self.assertEqual(build_tasks.max_existing_id({}), 0)


# ── Merge rules ──────────────────────────────────────────────────────────────

class TestMergeRules(unittest.TestCase):

    def test_status_carryover_open_marker(self):
        parsed = {
            "marker": "[ ]", "status_override": None, "pri": "P2",
            "task": "My Task", "due": None, "links": [], "why": None,
            "from_week": None, "completed_time": None, "raw_line": "",
        }
        existing_lookup = {"my task": {"status": "in_progress", "due": "—", "id": 5}}
        obj = build_tasks.build_task_object(
            parsed, 5, 1, "2026-05-05", existing_lookup, {}, {}
        )
        self.assertEqual(obj["status"], "in_progress")

    def test_marker_overrides_existing(self):
        parsed = {
            "marker": "[-]", "status_override": "in_progress", "pri": "P2",
            "task": "My Task", "due": None, "links": [], "why": None,
            "from_week": None, "completed_time": None, "raw_line": "",
        }
        existing_lookup = {"my task": {"status": "waiting", "due": "—", "id": 5}}
        obj = build_tasks.build_task_object(
            parsed, 5, 1, "2026-05-05", existing_lookup, {}, {}
        )
        self.assertEqual(obj["status"], "in_progress")


# ── CLI integration ──────────────────────────────────────────────────────────

class TestCLI(unittest.TestCase):

    def _run(self, core_text, journal_text="", existing=None, jira=None, slack=None, on_goalie=False):
        """Run build_tasks.py with temp files and return (exit_code, output_data_or_stdout)."""
        import tempfile, os
        tmpdir = tempfile.mkdtemp()
        core_p = Path(tmpdir) / "core.md"
        journal_p = Path(tmpdir) / "journal.md"
        existing_p = Path(tmpdir) / "existing.json"
        jira_p = Path(tmpdir) / "jira.json"
        slack_p = Path(tmpdir) / "slack.json"
        goalie_p = Path(tmpdir) / "goalie.json"
        output_p = Path(tmpdir) / "output.json"

        core_p.write_text(core_text)
        journal_p.write_text(journal_text)
        existing_p.write_text(json.dumps(existing or {}))
        jira_p.write_text(json.dumps(jira or {}))
        slack_p.write_text(json.dumps(slack or {}))
        goalie_p.write_text(json.dumps({"on_goalie": on_goalie}))

        result = subprocess.run(
            [sys.executable, str(Path(__file__).parent / "build_tasks.py"),
             "--core", str(core_p),
             "--journal", str(journal_p),
             "--existing", str(existing_p),
             "--jira-cache", str(jira_p),
             "--slack-cache", str(slack_p),
             "--goalie-cache", str(goalie_p),
             "--output", str(output_p)],
            capture_output=True, text=True,
        )

        if result.returncode == 0 and output_p.exists():
            return 0, json.loads(output_p.read_text())
        return result.returncode, result.stdout

    def test_core_missing_exits_1(self):
        import tempfile
        tmpdir = tempfile.mkdtemp()
        result = subprocess.run(
            [sys.executable, str(Path(__file__).parent / "build_tasks.py"),
             "--core", str(Path(tmpdir) / "missing.md"),
             "--journal", str(Path(tmpdir) / "j.md"),
             "--existing", str(Path(tmpdir) / "e.json"),
             "--output", str(Path(tmpdir) / "out.json")],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 1)

    def test_basic_build(self):
        core = (
            "- [-] 🔴 Task Alpha\n"
            "- [ ] 🟠 Task Beta\n"
            "- [~] 🟡 Task Gamma _(why: waiting on reply)_\n"
            "\n"
            "## Done\n"
            "\n"
            f"### {datetime.date.today().isoformat()}\n"
            "- [x] 🟠 Task Delta _(completed: 2026-05-05 09:00)_\n"
        )
        journal = (
            f"## {datetime.date.today().strftime('%A')} {datetime.date.today().isoformat()}\n"
            "\n"
            "### Plan\n"
            "\n"
            "#### Core Focus\n"
            "1. Task Alpha\n"
            "2. Task Beta\n"
            "\n"
            "### Done\n"
        )
        code, data = self._run(core, journal)
        self.assertEqual(code, 0)
        self.assertIn("sections", data)

        section_titles = [s["title"] for s in data["sections"]]
        self.assertIn("Today's Focus", section_titles)
        self.assertIn("Monitoring", section_titles)

        focus = next(s for s in data["sections"] if s["title"] == "Today's Focus")
        self.assertEqual(len(focus["tasks"]), 2)
        self.assertEqual(focus["tasks"][0]["task"], "Task Alpha")
        self.assertEqual(focus["tasks"][0]["status"], "in_progress")

        monitoring = next(s for s in data["sections"] if s["title"] == "Monitoring")
        self.assertEqual(len(monitoring["tasks"]), 1)
        self.assertEqual(monitoring["tasks"][0]["why"], "waiting on reply")

        self.assertEqual(len(data["completed_today"]), 1)
        self.assertEqual(data["completed_today"][0]["time"], "09:00")

    def test_day_rollover_clears_completed(self):
        core = "- [ ] 🔴 Task A\n\n## Done\n"
        existing = {
            "updated": "2026-05-04 23:00",
            "sections": [],
            "completed_today": [
                {"id": 99, "task": "Old Task", "time": "17:00", "status": "done"}
            ],
        }
        code, data = self._run(core, existing=existing)
        self.assertEqual(code, 0)
        self.assertEqual(len(data["completed_today"]), 0)

    def test_completion_wins_over_open(self):
        core = "- [ ] 🟠 Browser Completed Task\n\n## Done\n"
        today_str = datetime.date.today().isoformat()
        existing = {
            "updated": f"{today_str} 08:00",
            "sections": [{"title": "High Priority", "tasks": [
                {"id": 50, "task": "Browser Completed Task", "status": "open"}
            ]}],
            "completed_today": [
                {"id": 50, "task": "Browser Completed Task", "time": "07:30", "status": "done"}
            ],
        }
        code, data = self._run(core, existing=existing)
        self.assertEqual(code, 0)
        all_active = [t["task"] for s in data["sections"] for t in s["tasks"]]
        self.assertNotIn("Browser Completed Task", all_active)
        completed_names = [c["task"] for c in data["completed_today"]]
        self.assertIn("Browser Completed Task", completed_names)

    def test_auto_derive_focus(self):
        core = (
            "- [-] 🔴 High Task A\n"
            "- [ ] 🟠 High Task B\n"
            "- [ ] 🟡 Low Task\n"
            "\n## Done\n"
        )
        # No Core Focus in journal
        journal = (
            f"## {datetime.date.today().strftime('%A')} {datetime.date.today().isoformat()}\n"
            "### Plan\n"
            "### Done\n"
        )
        code, data = self._run(core, journal)
        self.assertEqual(code, 0)
        focus = next(s for s in data["sections"] if s["title"] == "Today's Focus")
        self.assertTrue(len(focus["tasks"]) >= 1)
        self.assertEqual(focus["tasks"][0]["task"], "High Task A")

    def test_id_preserved_from_existing(self):
        core = "- [ ] 🟠 My Stable Task\n\n## Done\n"
        existing = {
            "updated": "2026-01-01 00:00",
            "sections": [{"title": "High Priority", "tasks": [
                {"id": 42, "task": "My Stable Task", "status": "open", "due": "—"}
            ]}],
            "completed_today": [],
        }
        code, data = self._run(core, existing=existing)
        self.assertEqual(code, 0)
        all_tasks = [t for s in data["sections"] for t in s["tasks"]]
        stable = next(t for t in all_tasks if t["task"] == "My Stable Task")
        self.assertEqual(stable["id"], 42)

    def test_blank_lines_in_core(self):
        core = (
            "- [ ] 🔴 Task A\n"
            "\n"
            "- [ ] 🟠 Task B\n"
            "\n"
            "## Done\n"
        )
        code, data = self._run(core)
        self.assertEqual(code, 0)
        all_tasks = [t for s in data["sections"] for t in s["tasks"]]
        self.assertEqual(len(all_tasks), 2)

    def test_counts_in_output(self):
        today_str = datetime.date.today().isoformat()
        core = (
            "- [ ] 🔴 Active\n"
            "\n## Done\n"
            f"\n### {today_str}\n"
            "- [x] 🟠 Done A _(completed: 2026-05-05 09:00)_\n"
            "- [x] 🟡 Done B _(completed: 2026-05-05 10:00)_\n"
        )
        code, data = self._run(core)
        self.assertEqual(code, 0)
        self.assertIn("2 core tasks completed", data["counts"])


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


if __name__ == "__main__":
    unittest.main()
