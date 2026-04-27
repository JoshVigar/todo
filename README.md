# todo — local tasks dashboard

A small Python HTTP server that renders a markdown-driven task list as a dark-mode HTML dashboard. Designed for daily personal accountability, with stable task IDs, focus-aware polling, and a click-to-cycle status flow.

## Architecture

Two-file source of truth:

- **Core markdown** (`journal/YYYY-Www-core.md`) — task names, priority emoji, links, carried-from metadata, done history. Owned by your editor / Claude.
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

## File layout

```
serve-tasks.py             # the server
journal/YYYY-Www-core.md   # active task list for the week (gitignored)
journal/YYYY-Www.md        # weekly journal with daily Core Focus (gitignored)
tasks-live.json            # mutable runtime state (gitignored)
```

## License

MIT.
