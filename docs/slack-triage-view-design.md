# Slack Triage View — Design

**Date:** 2026-05-04
**Branch:** `slack-triage-view` (worktree at `/Users/joshuav/todo-slack-triage`)
**Status:** Approved, ready for implementation planning

## Goal

Add a "Slack" view to the localhost dashboard that surfaces items the
`slack-triage` skill produced during a `/slack` run. For each item, the user
can convert it into a task in the existing tasks system, or dismiss it
persistently so it does not reappear on subsequent runs.

This integrates two existing components that today are disconnected:

- The `slack-triage` Claude Code skill (`~/.claude/skills/slack-triage/`) which
  scans Slack via MCP and emits a chat report.
- The localhost task dashboard (`~/todo/serve-tasks.py`) which renders the
  user's task list and supports add/edit/cancel mutations.

## Non-goals

- The dashboard will not call the Slack MCP. (MCP is only available inside
  Claude Code, not from a long-running Python process.)
- No "refresh from Slack" button. The user runs `/slack` in Claude Code as
  they do today; the dashboard reads whatever the latest run wrote.
- No live polling, no second daemon, no extra processes.
- No Cmd+click quick-add convert path in v1 — convert always goes through the
  Add modal. (Reconsider in v2 once the rest is stable.)
- No "dismiss thread forever" in v1 — dismissals are keyed per-message_ts;
  thread-wide dismissal can be a follow-up.

## Architecture overview

```
+------------------+        +-------------------------+        +-----------+
|   /slack run     | writes |  ~/todo/slack-triage.json| reads  | dashboard |
|  (Claude Code)   |------->|  (snapshot, atomic)      |------->| HTTP+SSE  |
+------------------+        +-------------------------+        +-----------+
                                                                      ^
                            +------------------------+    user        |
                            |  ~/todo/slack-         | dismiss / convert
                            |  dismissed.json        |<---------------|
                            |  slack-converted.json  |                |
                            +------------------------+                |
```

Data flow:

1. User runs `/slack` in Claude Code. The skill scans Slack via MCP, classifies
   items into tiers, and **before** rendering its Markdown report, writes a
   structured JSON snapshot to `~/todo/slack-triage.json` (atomic write —
   tempfile + `os.replace`).
2. The dashboard's existing `_watch_state` thread now also signatures
   `slack-triage.json`, `slack-dismissed.json`, and `slack-converted.json`.
   When the skill writes the snapshot, the dashboard's SSE channel fires and
   any open browser tab refreshes.
3. The user opens the dashboard, switches to the **Slack** view via the
   view-switcher, sees the triage list, and either converts or dismisses each
   item.
4. Convert → opens the existing Add modal pre-filled with action-oriented
   defaults. On save, the new task is created via the existing `/add` route,
   AND the item's id is appended to `slack-converted.json` so it disappears
   from the view immediately.
5. Dismiss → POST `/slack/dismiss` with the item id. Server appends to
   `slack-dismissed.json`. Item disappears.

## Components

### Producer: `slack-triage` skill modifications

The skill currently emits a Markdown report directly to chat. To produce a
machine-readable snapshot, the skill must materialise a structured
intermediate before rendering.

**New skill steps** (between current Step 2 "Classify" and Step 3 "Present"):

- **Step 2.5 — Build structured snapshot.** For every item the skill found,
  emit a record with all fields listed in the [Snapshot schema](#snapshot-schema)
  section. The skill already has all the data needed (from search and channel
  reads with `detailed` format). It just needs to materialise it.
- **Step 2.6 — Write snapshot atomically.** `tempfile.NamedTemporaryFile` →
  `os.replace` to `~/todo/slack-triage.json`. Mode `0644`.

The skill **must** resolve `<@U123>` mentions in snippets to `@username`
before writing — the skill already knows user names from `slack_search_users`
results, so this is a string substitution. mrkdwn (`*bold*`, `_italic_`,
`` `code` ``) is rendered to plain text or stripped — no Markdown leaks into
the dashboard.

The skill's existing chat-report behaviour is **unchanged** — Markdown report
still goes to chat as today. The disk write is an additive step.

### Consumer: dashboard changes

#### Routes

- `GET /?view=slack` — renders the new Slack view body.
- `POST /slack/dismiss` — body `{"id": "<channel_id>:<message_ts>"}`. Appends
  to `~/todo/slack-dismissed.json`. Returns 200 with refreshed view fragment.
- `POST /slack/convert` — body `{"id": "...", ...add-fields}`. Creates a task
  via the same path as `/add`, then appends id to `~/todo/slack-converted.json`.
  Returns 200 with refreshed view fragment.

The existing `/add` route is **not** modified; convert calls the same internal
`apply_add` function plus the converted-list append.

#### State management

- New `_slack_lock = threading.Lock()` separate from `_state_lock`.
  Serialises all reads and writes of `slack-triage.json`,
  `slack-dismissed.json`, `slack-converted.json`.
- `_state_signature()` is extended to include mtimes of the three slack
  files. This means SSE fires when `/slack` writes the snapshot or when
  the user dismisses/converts.
- ETag composition is updated in lock-step with `_state_signature` so HTTP
  304 responses stay correct.

#### Rendering

- New `VIEWS = ["dashboard", "classic", "slack"]`. The view-switcher's
  existing tier-3 dropdown picks up the new entry automatically.
- `_build_slack_body(data)` builds three section cards top-to-bottom:
  1. **Reply Needed** — open
  2. **Review** — open
  3. **Already Handled** — collapsed by default. Expand state is
     **not** persisted across reloads in v1.
- Each row HTML structure:
  ```html
  <div class="slack-row" data-id="C123ABC:1714834327.001234">
    <div class="slack-meta">
      <span class="slack-sender">Maria</span>
      <span class="slack-channel">#hotsauce-squad</span>
      <span class="slack-ts" title="2026-05-04 12:15">2h ago</span>
    </div>
    <div class="slack-snippet">blocked on you for the GHE config rollout…</div>
    <div class="slack-actions">
      <button class="slack-convert">➕ Convert</button>
      <button class="slack-dismiss">✕ Dismiss</button>
    </div>
    <a class="slack-permalink-overlay" href="https://..." target="_blank"
       rel="noopener noreferrer"></a>
  </div>
  ```
  Hover reveals the action buttons. Click anywhere on the row (except the
  buttons) navigates the overlay anchor → opens permalink in new tab.
- **No `/open` round-trip** for Slack permalinks — direct anchor link.
- Header line above the sections:
  ```
  Last refreshed: 2026-05-04 14:32 (2h ago)  [stale]
  ```
  The `[stale]` badge appears amber-styled when the snapshot is more than
  24h old.
- Noise summary line at the bottom: rendered only if the snapshot includes
  a non-empty `noise` object. Format: `Noise: 12 in #random, 8 bot pings`.

#### Dedup & filtering

At render time the dashboard:
1. Reads snapshot's items.
2. Reads `slack-dismissed.json` (set of ids).
3. Reads `slack-converted.json` (set of ids).
4. Reads task links across all active tasks (set of permalinks).
5. For each snapshot item, hide if:
   - id ∈ dismissed, OR
   - id ∈ converted, OR
   - permalink ∈ active task links (legacy fallback for tasks created before
     this feature shipped, or via manual link addition).

The permalink check is built once per render as a pre-built `set`, not nested
loops.

#### Convert flow (modal)

1. User clicks ➕ Convert on a row.
2. JS opens the existing `#modal[data-mode="add"]` pre-filled with:
   - `name` = `Reply to <sender> in #<channel>` (or `Reply to <sender>` for DM)
   - `why` = the snippet, full
   - `links` = `[{label: "Slack", url: <permalink>}]`
   - `pri`, `due` blank
3. JS stores the item id on the modal as `data-slack-id="<id>"`.
4. On save, JS POSTs to `/slack/convert` (not `/add`) with the form fields
   plus the id. Server runs `apply_add` and `_record_convert(id)` under
   `_slack_lock`.
5. SSE refresh fires; the row disappears (now in converted set).

#### Dismiss flow

1. User clicks ✕ Dismiss on a row.
2. JS POSTs to `/slack/dismiss` with `{"id": "..."}`.
3. Server appends to `slack-dismissed.json` (atomic write) under
   `_slack_lock`.
4. SSE refresh fires; row disappears.

### Snapshot schema

`~/todo/slack-triage.json`:

```json
{
  "version": 1,
  "generated_at": "2026-05-04T14:32:11+01:00",
  "items": [
    {
      "channel_id": "C123ABC",
      "message_ts": "1714834327.001234",
      "thread_ts": null,
      "tier": "reply_needed",
      "is_dm": false,
      "sender": "Maria",
      "channel_name": "hotsauce-squad",
      "permalink": "https://spotify.slack.com/archives/C123ABC/p1714834327001234",
      "snippet": "blocked on you for the GHE config rollout — can you take a look?",
      "ts": "2026-05-04T12:15:03+01:00",
      "action_hint": "Reply",
      "context": "connects to HOTS-1953"
    }
  ],
  "noise": {"#random": 12, "bot_notifications": 8}
}
```

Field rules:
- `version`: integer, currently `1`. Future schema changes bump this; the
  dashboard refuses to render a snapshot whose version it doesn't recognise.
- `generated_at`, `ts`: ISO 8601 with timezone offset, always.
- `channel_id`, `message_ts`: separate fields. The composite id used for
  dismissal/convert tracking is built at consumer time as
  `f"{channel_id}:{message_ts}"`.
- `thread_ts`: parent timestamp string if this item is a thread reply; `null`
  for top-level messages.
- `tier`: enum `"reply_needed" | "review" | "already_handled"`. Lowercase
  snake_case. UI maps to "Reply Needed" / "Review" / "Already Handled".
- `is_dm`: boolean. Determines display prefix (`@Sender` for DM, `#channel`
  for channel).
- `snippet`: clean plain text. Slack mentions resolved to `@username`. mrkdwn
  stripped or simplified. No HTML.
- `action_hint`, `context`: optional, may be omitted. The dashboard treats as
  nice-to-have hints.

### Persistent dismiss/convert state

`~/todo/slack-dismissed.json`:

```json
{
  "version": 1,
  "ids": ["C123ABC:1714834327.001234", "C456DEF:1714900000.000111"]
}
```

`~/todo/slack-converted.json`:

```json
{
  "version": 1,
  "ids": ["C123ABC:1714834327.001234"]
}
```

Both files atomic-written. Compaction (drop entries older than 90 days, drop
entries no longer in any snapshot) is a deferred concern — TODO in code.

### Error handling

| Condition | Behaviour |
|---|---|
| `slack-triage.json` missing | Empty Slack view with "Run `/slack` to populate." |
| `slack-triage.json` malformed JSON | Empty view + log error; do not 500 |
| `slack-triage.json` version mismatch | Empty view with "Snapshot version not supported." |
| Item missing required field | Skip that item; log warning |
| `slack-dismissed.json` / `slack-converted.json` missing | Treat as empty set |

## Testing strategy

New tests in `test_serve_tasks.py`:

1. **`/slack` view rendering** — fixture snapshot file with one item per
   tier; verify three sections present, items rendered correctly.
2. **Empty state** — no snapshot file; view renders empty placeholder.
3. **Malformed snapshot** — invalid JSON; view renders empty + no 500.
4. **Version mismatch** — `version: 99`; view renders error placeholder.
5. **Dismiss POST** — POST `/slack/dismiss`, verify item appears in
   `slack-dismissed.json` and is filtered out on next render.
6. **Convert POST** — POST `/slack/convert` with name/pri/due/why/links,
   verify task created in `tasks-live.json` AND id appears in
   `slack-converted.json` AND filtered from view.
7. **Permalink legacy filter** — fixture with a task whose links include the
   snapshot permalink, but id NOT in converted set; item still hidden.
8. **Cancelled task does not hide item** — task with `state: cancelled`
   containing permalink. Snapshot item should re-appear (cancelled task
   doesn't count as "active conversion").
9. **SSE fires on snapshot write** — touch `slack-triage.json`; assert
   `_state_signature` changes.
10. **ETag stability** — two reads with no slack file changes return matching
    ETag; reads bracketing a slack write return different ETags.
11. **Snippet rendering** — fixture snippets with `<>`, control chars, very
    long text; HTML-escaped, truncated where needed.
12. **Stale badge logic** — snapshot timestamps 23h, 25h, missing; assert
    badge presence/absence.
13. **Mode-locking on modal** — Add modal opens via Convert with the right
    pre-fill values; saving routes to `/slack/convert` not `/add`.
14. **Schema field validator** — round-trip a snapshot through the producer
    contract, assert all required fields present and types correct (this is
    a guard for skill-side regressions).

The skill side is harder to test in this repo — the skill is at
`~/.claude/skills/slack-triage/SKILL.md`, not in the todo repo. The skill
update lands in a separate change. We'll provide a sample
`slack-triage.json` fixture as the contract reference.

## Implementation order

**Phase 0 — Skill modification (blocking).** Modify the slack-triage skill to
emit `~/todo/slack-triage.json`. Validate output against the schema by hand
on a real `/slack` run. This unblocks the dashboard work.

**Phase 1 — Backend.**
- Add `_slack_lock`, snapshot read function, dismissed/converted read
  functions.
- Wire `slack-triage.json`, `slack-dismissed.json`, `slack-converted.json`
  mtimes into `_state_signature`.
- Add `/slack/dismiss` and `/slack/convert` routes.

**Phase 2 — Frontend.**
- Add `"slack"` to `VIEWS`. Build `_build_slack_body`.
- New CSS for `.slack-row`, sections, hover states, stale badge.
- JS: convert button → opens existing modal pre-filled, save routes to
  `/slack/convert`. Dismiss button → POST `/slack/dismiss`.

**Phase 3 — Tests.** Add the 14 tests listed above.

**Phase 4 — Polish.** Stale badge styling. Empty state copy. Edge cases.

## Risks

1. **Skill changes are out of repo.** The slack-triage skill lives outside
   the todo repo. A skill regression breaks the contract. Mitigation: the
   schema validator test (#14) and a stable sample fixture make the contract
   explicit.
2. **Snippet content surprises.** Slack messages can contain RTL text,
   emoji, very long single words, etc. Mitigation: defensive truncation and
   escaping in the dashboard, even though the skill should pre-clean.
3. **Permalink format changes.** Slack permalink formats have changed over
   the years. Mitigation: the skill produces the permalink; dashboard treats
   it as opaque and only matches by string equality.

## Out of scope (v2 candidates)

- Cmd+click quick-add (no modal, immediate POST).
- Right-click "dismiss thread forever" (uses `thread_ts` key).
- `slack-converted.json` / `slack-dismissed.json` compaction.
- Multiple snapshots merged (e.g. snapshot from each workspace).
- Renaming the view to "Inbox" or similar branding.
