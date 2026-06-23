"""Shared fixtures and helpers for tasks-live tests.

Sets SERVE_TASKS_NO_WATCH=1 before anything imports serve-tasks.py,
then loads the module once for the whole test session.
"""
import datetime as _dt
import importlib.util
import json
import os
from pathlib import Path

os.environ.setdefault("SERVE_TASKS_NO_WATCH", "1")

import pytest

# ── Load serve-tasks.py as a module ─────────────────────────────────────────

SCRIPT = Path(__file__).parent / "serve-tasks.py"
spec = importlib.util.spec_from_file_location("st", SCRIPT)
st = importlib.util.module_from_spec(spec)
spec.loader.exec_module(st)


# ── Core fixture data ───────────────────────────────────────────────────────

def fixture_data():
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


MUTATING_ROUTES = [
    "/update", "/complete", "/update-pri", "/uncomplete",
    "/sort", "/reorder", "/move-section", "/cancel", "/add", "/edit",
]


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def data():
    return fixture_data()


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Point JSON_FILE and current_core_path at temp files seeded with the
    fixture. Mutating endpoints can be exercised without touching the user's
    real ~/todo/tasks-live.json or ~/todo/journal/."""
    json_path = tmp_path / "tasks-live.json"
    json_path.write_text(json.dumps(fixture_data(), indent=2))
    monkeypatch.setattr(st, "JSON_FILE", json_path)

    core_path = tmp_path / "core.md"
    core_path.write_text(SEED_CORE)
    monkeypatch.setattr(st, "current_core_path", lambda week=None: core_path)

    return json_path


@pytest.fixture
def slack_state(tmp_path, monkeypatch):
    """Point SLACK_*_FILE constants at temp paths."""
    triage = tmp_path / "slack-triage.json"
    dismissed = tmp_path / "slack-dismissed.jsonl"
    converted = tmp_path / "slack-converted.jsonl"
    monkeypatch.setattr(st, "SLACK_SNAPSHOT_FILE", triage)
    monkeypatch.setattr(st, "SLACK_DISMISSED_FILE", dismissed)
    monkeypatch.setattr(st, "SLACK_CONVERTED_FILE", converted)
    return {"triage": triage, "dismissed": dismissed, "converted": converted}


@pytest.fixture
def email_state(tmp_path, monkeypatch):
    triage = tmp_path / "email-triage.json"
    dismissed = tmp_path / "email-dismissed.jsonl"
    converted = tmp_path / "email-converted.jsonl"
    monkeypatch.setattr(st, "EMAIL_SNAPSHOT_FILE", triage)
    monkeypatch.setattr(st, "EMAIL_DISMISSED_FILE", dismissed)
    monkeypatch.setattr(st, "EMAIL_CONVERTED_FILE", converted)
    return {"triage": triage, "dismissed": dismissed, "converted": converted}


@pytest.fixture
def ghsupport_state(tmp_path, monkeypatch):
    triage = tmp_path / "gh-support-triage.json"
    dismissed = tmp_path / "ghsupport-dismissed.jsonl"
    monkeypatch.setattr(st, "GH_SUPPORT_SNAPSHOT_FILE", triage)
    monkeypatch.setattr(st, "GH_SUPPORT_DISMISSED_FILE", dismissed)
    return {"triage": triage, "dismissed": dismissed}


# ── Helpers (importable by test files) ──────────────────────────────────────

def click_targets_for_task(html, task_id, *, done=False):
    """Return all click targets in `html` carrying data-id=task_id."""
    import re
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


def read_jsonl(path):
    """Read a JSONL file as a list of dicts."""
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def slack_snapshot(items=None, generated_at=None, noise=None, version=1):
    """Build a snapshot dict matching the slack-triage schema."""
    if generated_at is None:
        generated_at = _dt.datetime.now(_dt.timezone.utc).astimezone().isoformat(timespec="seconds")
    return {
        "version": version,
        "generated_at": generated_at,
        "items": items or [],
        "noise": noise or {},
    }


def slack_item(channel_id="C1", message_ts="111.222", tier="reply_needed",
               sender="Maria", channel_name="hotsauce-squad", is_dm=False,
               snippet="need your input on the GHE rollout",
               permalink=None, ts=None, thread_ts=None,
               action_hint=None, context=None):
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


def email_snapshot(items=None, version=1):
    return {
        "version": version,
        "generated_at": "2026-05-22T10:30:00+01:00",
        "items": items or [],
        "summary": {"total_fetched": 10, "filtered_noise": 5, "gh_support_threads": 1},
    }


def email_item(email_id="e1", category="general", tier="action_needed",
               sender="Alice", subject="Test subject", snippet="Test snippet"):
    return {
        "email_id": email_id,
        "thread_id": email_id,
        "category": category,
        "tier": tier,
        "sender": sender,
        "sender_email": f"{sender.lower()}@spotify.com",
        "subject": subject,
        "snippet": snippet,
        "ts": "2026-05-22T10:00:00+01:00",
        "link": f"https://mail.google.com/mail/u/0/#inbox/{email_id}",
    }


def ghsupport_snapshot(tickets=None, version=1):
    return {
        "version": version,
        "generated_at": "2026-05-22T10:30:00+01:00",
        "tickets": tickets or [],
    }


def ghsupport_ticket(ticket_id="4338773", subject="IdP issues",
                      waiting_on="us", message_count=3):
    return {
        "ticket_id": ticket_id,
        "ticket_url": f"https://support.github.com/ticket/{ticket_id}",
        "ticket_code": "TEST-CODE",
        "subject": subject,
        "category": "General support request",
        "raised_by": "Ellie Kelsch",
        "raised_at": "2026-04-29T09:34:00+01:00",
        "last_update": "2026-05-05T17:44:45+01:00",
        "waiting_on": waiting_on,
        "message_count": message_count,
        "messages": [
            {"author": "Ellie Kelsch", "ts": "2026-04-29T09:34:00+01:00",
             "is_support": False, "content": "Hello, we have an issue with IdP groups."},
            {"author": "James", "ts": "2026-04-29T15:27:00+01:00",
             "is_support": True, "content": "Hi Ellie, I'll look into this."},
            {"author": "James", "ts": "2026-05-05T16:44:00+01:00",
             "is_support": True, "content": "Just a quick update, could you try..."},
        ],
        "gmail_link": "https://mail.google.com/mail/u/0/#inbox/test123",
    }


def goalie_fixture():
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
