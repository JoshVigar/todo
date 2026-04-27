# todo — local tasks dashboard

A small Python HTTP server that renders a markdown-driven task list as a dark-mode HTML dashboard. Designed for daily personal accountability, with stable task IDs, focus-aware polling, and a click-to-cycle status flow.

## Architecture

Two-file source of truth:

- **Core markdown** (`journal/YYYY-Www-core.md`) — task names, priority emoji, links, carried-from metadata, done history. Owned by your editor or your automation of choice.
- **JSON** (`tasks-live.json`) — runtime status (cycled in browser), display ordering, stable task IDs. Mutated by the server.

The Python server reads both, renders dark-mode HTML on `/`, and exposes endpoints for click actions.

## Features

- Stable task IDs (`id` field, never changes once assigned) — display position (`num`) is separate
- Click `#` cell to complete; click again on completed row to uncomplete
- Click status badge to cycle: open → in_progress → waiting → open
- Click priority badge to cycle: P1 → P2 → P3 → P4 → P5 → P1
- Drag-and-drop reorder; sort button redistributes by priority
- Sort and reorder both persist to the core markdown so they survive a rebuild
- Auto-refresh every 2 seconds, but only when the tab has focus
- Conditional GET (`If-Modified-Since`) — returns `304` when nothing has changed
- Live reload on edits to `serve-tasks.py`
- Age column shows days elapsed since the task was added, colour-coded for staleness
- Overdue rows highlighted red; in-progress blue; due-soon yellow

## Running

```
python3 serve-tasks.py
```

Then visit <http://localhost:6419>.

## Data format

If you want to drive the dashboard yourself (manual edits, scripts, or your own LLM/CLI integration), here's the contract.

### Core markdown — `journal/YYYY-Www-core.md`

```markdown
# Core Work — 2026-W18

- [ ] 🔴 Reply to GHS ticket ([Support](https://...)) _(carried from W17)_ _(why: deadline today)_
- [-] 🟠 Investigation in progress
- [~] 🟡 Waiting on someone else
- [!] 🔴 Blocked by upstream bug — due 2026-04-29

## Done

### 2026-04-27
- [x] 🟠 Something I finished _(completed: 2026-04-27 09:41)_
```

**State markers** (the bracket character):

| Marker | Meaning           |
|--------|-------------------|
| `[ ]`  | open              |
| `[-]`  | in progress       |
| `[~]`  | waiting           |
| `[!]`  | blocked           |
| `[x]`  | done              |

**Priority emoji** (immediately after the marker):

| Emoji | Pri |
|-------|-----|
| 🔴    | P1  |
| 🟠    | P2  |
| 🟡    | P3  |
| 🔵    | P4  |
| ⏸️    | P5  |

**Inline tags** (extracted by the renderer, stripped from the visible task name):

- `— due HH:MM` or `— due YYYY-MM-DD` — the due date / time
- `([label](url) · [label2](url2))` — links column
- `_(carried from Www)_` — the week the task was carried from (used for the Age column)
- `_(why: short reason)_` — extra context shown in the Why column
- `_(completed: YYYY-MM-DD HH:MM)_` — only on `[x]` tasks; powers the daily completion count

The `## Done` section uses `### YYYY-MM-DD` headings, newest date first.

### `tasks-live.json` schema

```jsonc
{
  "updated": "2026-04-27 12:34",
  "week": "W18",
  "sections": [
    { "title": "Today's Focus", "tasks": [ /* task objects */ ] },
    { "title": "Monitoring",    "tasks": [ /* ... */ ] },
    { "title": "High Priority", "tasks": [ /* ... */ ] },
    { "title": "Lower Priority","tasks": [ /* ... */ ] }
  ],
  "completed_today": [ /* task objects with extra "time" field */ ],
  "counts": "✅ 2 core tasks completed this week (2 on 2026-04-27)"
}
```

### Task object

```jsonc
{
  "num": 6,                       // display row position; updated on every sort/add
  "id": 47,                       // stable identifier; never changes once assigned
  "pri": "P1",                    // P1 | P2 | P3 | P4 | P5 | null
  "task": "Task name",            // task text without emoji or tags
  "due": "17:00",                 // see below
  "from": "W15",                  // ISO week the task was originally carried from, or "—"
  "added": "2026-04-13",          // ISO date the task was created — drives the Age column
  "links": [
    { "label": "JIRA-123", "url": "https://..." }
  ],
  "status": "in_progress",        // see below
  "why": "reason text"            // optional context, shown in Why column
}
```

**`status` enum:**

| Value              | Display          |
|--------------------|------------------|
| `"open"`           | 🔓 Open           |
| `"todo"`           | 📋 To Do          |
| `"in_progress"`    | 🔄 In Progress    |
| `"waiting"`        | ⏳ Waiting        |
| `"waiting_support"`| ⏳ Waiting for support |
| `"waiting_customer"`| ⏳ Waiting for customer |
| `"blocked"`        | 🚫 Blocked        |
| `"done"`           | ✅ Done            |
| `"replied"`        | 💬 Replied        |

**`due` value (display string written directly):**

| Value             | Behaviour                              |
|-------------------|----------------------------------------|
| `"HH:MM"`         | Due today at this time (yellow within 2h) |
| `"today"`         | Due today, no time                     |
| `"YYYY-MM-DD"`    | Due on a future date                   |
| `"⚠️ YYYY-MM-DD"` | Overdue (renders the row red)         |
| `"—"` or absent   | No due date                            |

### Merge rule (markdown ↔ JSON)

When rebuilding `tasks-live.json` from the core markdown:

- Carry over `id`, `status`, and `due` from the existing JSON for any task whose name matches AND whose core file marker is `[ ]`. This is what lets browser-side status cycles survive a refresh.
- If the core file marker is `[-]`, `[~]`, or `[!]`, the marker wins — set `status` to `in_progress`, `waiting`, or `blocked` respectively. Do not carry over the JSON status.
- `[x]` tasks are moved out of the active list into the `## Done` section under a `### YYYY-MM-DD` heading and recorded in `completed_today` (with a `time` field) until end of day.

### `id` vs `num`

- `id` is stable: assigned once when the task is created, never changes. Use this for any external reference (saying "task 47" out loud, scripts that act on a specific task).
- `num` is a display position: 1 = first row shown, updated by the server on every sort/add/reorder. Don't rely on it across sessions.

## Endpoints

| Method | Path          | Body                                                                                  | Action                          |
|--------|---------------|---------------------------------------------------------------------------------------|---------------------------------|
| GET    | `/`           | —                                                                                     | Render dashboard                |
| GET    | `/open?url=…` | —                                                                                     | Open URL in system browser      |
| POST   | `/update`     | `{"id": N}`                                                                           | Cycle status                    |
| POST   | `/complete`   | `{"id": N}`                                                                           | Mark done                       |
| POST   | `/uncomplete` | `{"id": N}`                                                                           | Restore from completed_today    |
| POST   | `/update-pri` | `{"id": N}`                                                                           | Cycle priority                  |
| POST   | `/sort`       | `{}`                                                                                  | Sort by priority + redistribute |
| POST   | `/reorder`    | `{"from": id, "to": id, "before": bool}`                                              | Drag-and-drop reorder           |
| POST   | `/add`        | `{"task":"…","pri":"P2","due":"—","why":"—","link_label":"…","link_url":"…"}`         | Add new task                    |

## License

MIT.
