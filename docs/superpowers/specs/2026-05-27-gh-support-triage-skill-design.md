# GH Support Triage Skill Design

**Date:** 2026-05-27
**Goal:** Dedicated skill to fetch GitHub Support emails from Gmail, parse conversation threads, and populate the `?view=ghsupport` dashboard view. Replaces the GH Support portion of the existing email skill.

## Scope

Three changes:
1. **New skill** `gh-support-triage` — fetches, parses, writes snapshot
2. **Dashboard changes** — dismiss/restore routes and UI for ghsupport view
3. **Email skill cleanup** — remove all GH Support logic from the email skill

## 1. New Skill: `gh-support-triage`

**Location:** `~/.claude/skills/gh-support-triage/SKILL.md`

**Triggers:** "gh support", "check gh support", "github support", "ghsupport", "support tickets"

### Steps

1. **Load watched ticket IDs** from `~/todo/tasks-live.json` — regex: `support\.github\.com/ticket/[^/]+/[^/]+/(\d+)`
2. **Fetch** via `mcp__enterprise-context-agent__search_workplace_knowledge`:
   - Query: `from:githubsupport.com newer_than:7d`
   - No other filters — we want ALL GH Support threads, not just watched
3. **Slim and deduplicate** — write results to temp file (keep full content for parsing). Deduplicate by threadId, keep most recent per thread.
4. **Parse each thread** into ticket object:
   - Extract ticket URL (preserve enterprise format), ticket ID, ticket code
   - Strip footer (`This email is a service from GitHub Support.` and everything after)
   - Strip header (everything before first 46-dash separator)
   - Split on 46-dash separators into message blocks
   - Each block: extract `Author, MMM DD, YYYY, HH:MM AM/PM UTC` line → author + timestamp. Remaining text = content.
   - `is_support`: author is Spotify if their name appears in any `@spotify.com` address in to/cc fields; otherwise GH Support
   - Sort messages chronologically
   - `raised_by`: earliest message author. `raised_at`: its timestamp.
   - `waiting_on`: last substantive message (skip auto-receipts starting "Thank you for contacting GitHub Support") — if from GH Support → `"us"`, from Spotify → `"github"`
   - Clean subject: strip `[GitHub Support] Re: ` prefix and category suffixes
   - `tracked`: true if ticket_id is in the watched set, false otherwise
5. **Write** `~/todo/gh-support-triage.json` atomically (tempfile + os.replace)
6. **Print summary table** to chat: ticket ID, subject (truncated), waiting_on, message count, tracked

### Snapshot Schema

```json
{
  "version": 1,
  "generated_at": "ISO 8601 timestamp",
  "tickets": [
    {
      "ticket_id": "string",
      "ticket_url": "string (preserved verbatim)",
      "ticket_code": "string",
      "subject": "string (cleaned)",
      "category": "string (from subject suffix)",
      "raised_by": "string",
      "raised_at": "ISO timestamp",
      "last_update": "ISO timestamp",
      "waiting_on": "us | github",
      "message_count": "number",
      "tracked": "boolean (new field)",
      "messages": [
        {
          "author": "string",
          "ts": "ISO timestamp",
          "is_support": "boolean",
          "content": "string"
        }
      ],
      "gmail_link": "string"
    }
  ]
}
```

The `tracked` field is the only addition to the existing schema. Version stays at 1.

## 2. Dashboard Changes (`serve-tasks.py`)

### New state file
- `GH_SUPPORT_DISMISSED_FILE = Path.home() / "todo" / "ghsupport-dismissed.jsonl"`
- `GH_SUPPORT_DISMISS_TTL_DAYS = 14`

### New routes
- `POST /ghsupport/dismiss` — body: `{"id": "<ticket_id>"}`. Appends `{"id": ..., "ts": ...}` to JSONL file.
- `POST /ghsupport/restore` — body: `{"id": "<ticket_id>"}`. Removes matching entry from JSONL file.

### Rendering changes
- At render time, load dismissed IDs from JSONL (filter expired entries by TTL)
- Split tickets into **active** and **dismissed**
- Active tickets render as current, plus:
  - "Dismiss" button on each ticket card
  - Tracked badge (visual indicator for tickets in tasks-live.json)
- Dismissed tickets render in a collapsed "Dismissed" section at the bottom:
  - Compact single-line per ticket (ticket ID, subject, waiting_on)
  - "Restore" button per ticket

### SSE integration
- Add `ghsupport-dismissed.jsonl` to `_state_signature()` so dismiss/restore triggers live updates

## 3. Email Skill Cleanup

Remove from `~/.claude/skills/email/SKILL.md`:

| What to remove | Where |
|---|---|
| Watched tickets loading | Step 1 |
| GH Support thread dedup in slim script | Step 2 |
| GH Support filtering rule | Step 3, rule #2 |
| GH Support snapshot write | Step 4.6 (entire step) |
| `gh_support` category in email-triage.json | Step 4.5 classification |
| GH Support row in tuned exclusions table | Bottom of skill |

Add to Gmail query: `-from:githubsupport.com` so GH Support emails aren't fetched at all.

After cleanup, the email skill handles only general (non-GH-Support) emails. The `?view=email` GH Support Tickets section becomes empty/removed from the dashboard.

### Dashboard email view cleanup
- Remove the "GH Support Tickets" section from `_build_email_body` (purple `#bc8cff` section for `category: "gh_support"` items)

## Out of scope
- Automatic scheduling / cron — the skill is invoked on demand
- Seen cache for GH Support emails — not needed since we fetch all and deduplicate by thread
- Converting GH Support tickets to tasks from the ghsupport view (already handled via the email view's convert pattern and manual task creation)
