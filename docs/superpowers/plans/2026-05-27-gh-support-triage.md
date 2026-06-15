# GH Support Triage Skill — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Dedicated skill to fetch GitHub Support emails from Gmail, parse conversation threads, and populate the `?view=ghsupport` dashboard view with dismiss/restore and tracked-ticket support.

**Architecture:** New skill `gh-support-triage` owns the Gmail fetch + parse + snapshot write. Dashboard (`serve-tasks.py`) gains dismiss/restore routes and UI for the ghsupport view. Email skill has all GH Support logic removed.

**Tech Stack:** Claude skill (SKILL.md), Python (serve-tasks.py), MCP tool (`search_workplace_knowledge`), JSONL state files

---

### Task 1: New skill — `gh-support-triage/SKILL.md`

**Files:**
- Create: `~/.claude/skills/gh-support-triage/SKILL.md`

This is a Claude skill (a markdown instruction document), not executable code. It tells Claude how to fetch, parse, and write the GH Support snapshot. No unit tests — skill testing uses the writing-skills TDD approach (subagent pressure scenarios) which is done separately.

- [ ] **Step 1: Create the skill file**

Create `~/.claude/skills/gh-support-triage/SKILL.md` with the following content:

```markdown
---
name: gh-support-triage
description: Use when user says "gh support", "check gh support", "github support", "ghsupport", "support tickets", or wants to see GitHub Support ticket conversations and status.
---

# GH Support Triage

Fetch GitHub Support emails from Gmail, parse conversation threads, and write `~/todo/gh-support-triage.json` for the dashboard `?view=ghsupport`.

## Steps

### 1. Load watched ticket IDs

```bash
python3 -c "
import json, re
d = json.load(open('/Users/joshuav/todo/tasks-live.json'))
ids = set()
for s in d.get('sections', []):
    for t in s.get('tasks', []):
        for l in t.get('links', []):
            m = re.search(r'support\.github\.com/ticket/[^/]+/[^/]+/(\d+)', l.get('url',''))
            if m:
                ids.add(m.group(1))
print(' '.join(sorted(ids)))
"
```

Keep this set — tickets with matching IDs get `tracked: true` in the output.

### 2. Fetch

Call `mcp__enterprise-context-agent__search_workplace_knowledge` with:

```
gmail_query: from:githubsupport.com newer_than:7d
```

The result is saved to a large file. Slim it down and deduplicate by thread immediately — do not read the raw file into context:

```bash
python3 -c "
import json
data = json.load(open('<result-file>'))
results = data.get('search_results', [])

# Deduplicate by thread — keep only the most recent per thread
# (results are ordered most-recent-first, so first seen wins)
seen_threads = {}
for r in results:
    tid = r.get('threadId', r.get('id'))
    if tid not in seen_threads:
        seen_threads[tid] = r

deduped = list(seen_threads.values())
out = []
for r in deduped:
    out.append({
        'id': r.get('id',''),
        'thread': r.get('threadId',''),
        'sender': r.get('from',''),
        'subject': r.get('subject',''),
        'content': r.get('content') or '',
        'date': r.get('date',''),
        'link': r.get('link',''),
        'to': r.get('to',''),
        'cc': r.get('cc',''),
    })
print(json.dumps(out, indent=2))
" > \$TMPDIR/ghsupport_slim.json
```

Then read it:

```bash
cat \$TMPDIR/ghsupport_slim.json
```

### 3. Parse and write snapshot

For each email, parse the full conversation thread. Each GH Support email contains the **entire thread history**.

**Content structure:**
```
## Please do not write below this line ##
Your request ( https://support.github.com/ticket/XXXXXXX ) has been updated.
...
----------------------------------------------
Author, MMM DD, YYYY, HH:MM AM/PM UTC

Message content...
----------------------------------------------
Author, MMM DD, YYYY, HH:MM AM/PM UTC

Message content...
----------------------------------------------
...
--------------------------------
This email is a service from GitHub Support. [TICKET_CODE]
```

Parse each email:
1. Extract ticket URL from header: find the full URL matching `https://support\.github\.com/ticket/...` — store verbatim as `ticket_url` (enterprise URLs like `.../ticket/enterprise/669/4263381` must be kept intact). Extract the numeric ticket ID from the end of the URL.
2. Extract ticket code from footer: `\[([A-Z0-9]+-[A-Z0-9]+)\]` at end of content
3. **Strip footer FIRST**: find `This email is a service from GitHub Support.` and remove it and everything after it (including the 32-dash line `--------------------------------` preceding it)
4. **Then strip header**: remove everything before the first `----------------------------------------------` (46-dash separator)
5. Split the remaining text on `----------------------------------------------`
6. Each block: first non-empty line is `Author, MMM DD, YYYY, HH:MM AM/PM UTC` → extract author + timestamp. Everything after that line is the message content. Strip `#### Please describe your question or issue` and `#### What category best describes your issue?` form headers from the initial submission.
7. `is_support`: extract `@spotify.com` email addresses from the email's `to` and `cc` fields. An author is Spotify (not support) if their name appears as part of any `@spotify.com` address in to/cc. All other authors are GH Support.
8. Sort messages by parsed timestamp ascending (chronological order)
9. `raised_by`: the author of the earliest message. `raised_at`: its timestamp.
10. `waiting_on`: look at the last substantive message — skip auto-receipts (content starts with "Thank you for contacting GitHub Support"). If last substantive message is from GH Support → `"us"`, from Spotify → `"github"`.
11. Clean subject: strip `[GitHub Support] Re: ` prefix and category suffixes like ` (General support request)` or ` (GitHub Enterprise Server Administration)`
12. `tracked`: true if ticket_id is in the watched set from step 1, false otherwise.

Write atomically:

```bash
python3 -c "
import json, os, tempfile, datetime

tickets = <LIST_OF_PARSED_TICKETS>
# Each ticket: {ticket_id, ticket_url, ticket_code, subject,
#               category, raised_by, raised_at, last_update,
#               waiting_on, message_count, tracked,
#               messages: [{author, ts, is_support, content}],
#               gmail_link}

snapshot = {
    'version': 1,
    'generated_at': datetime.datetime.now(
        datetime.timezone.utc).astimezone().isoformat(
        timespec='seconds'),
    'tickets': tickets,
}

target = os.path.expanduser('~/todo/gh-support-triage.json')
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target))
with os.fdopen(fd, 'w') as f:
    json.dump(snapshot, f, ensure_ascii=False, indent=2)
os.replace(tmp, target)
print('Wrote', target)
"
```

### 4. Print summary

Print a table to chat:

| # | Ticket | Subject | Waiting On | Msgs | Tracked |
|---|--------|---------|------------|------|---------|
| 1 | #4263381 | Subject line... | us | 5 | ✓ |

If no tickets found: "No GH Support threads in the last 7 days."

## Known field names (MCP response schema)

```
id, threadId, labelIds, content, subject, from, to, cc, date, message-id, relevance_score, link
```
```

- [ ] **Step 2: Commit**

```bash
git add ~/.claude/skills/gh-support-triage/SKILL.md
git commit -m "feat: add gh-support-triage skill for dedicated GH Support email fetching"
```

---

### Task 2: Dashboard — add GH Support dismiss/restore backend

**Files:**
- Modify: `serve-tasks.py:64-73` (constants)
- Modify: `serve-tasks.py:222-242` (`_state_signature`)
- Modify: `serve-tasks.py:5280-5294` (GH Support section — add dismiss/restore functions)
- Modify: `serve-tasks.py:5488-5498` (route table)

- [ ] **Step 1: Add constants**

In `serve-tasks.py`, after the existing `GH_SUPPORT_SNAPSHOT_VERSION = 1` line (line 72), add:

```python
GH_SUPPORT_DISMISSED_FILE   = Path.home() / "todo" / "ghsupport-dismissed.jsonl"
GH_SUPPORT_DISMISS_TTL_DAYS = 14
```

Also add a lock after line 153 (`_email_lock`):

```python
_ghsupport_lock = threading.Lock()
```

- [ ] **Step 2: Update `_state_signature` to include dismissed file**

In the `_state_signature()` function (around line 240), after `ghs_t = _safe_mtime(GH_SUPPORT_SNAPSHOT_FILE)`, add:

```python
ghs_d = _safe_mtime(GH_SUPPORT_DISMISSED_FILE)
```

Update the return tuple to include `ghs_d`:

```python
return (json_m, src_m, core_m, slack_t, slack_d, slack_c,
        email_t, email_d, email_c, ghs_t, ghs_d)
```

- [ ] **Step 3: Add dismiss/restore functions**

In the `# GH Support triage snapshot` section (after `load_ghsupport_snapshot` around line 5294), add:

```python
def load_ghsupport_dismissed():
    out = set()
    for rec in load_slack_log(GH_SUPPORT_DISMISSED_FILE, ttl_days=GH_SUPPORT_DISMISS_TTL_DAYS):
        rid = rec.get("id")
        if isinstance(rid, str) and rid:
            out.add(rid)
    return out


def apply_ghsupport_dismiss(item_id):
    if not isinstance(item_id, str) or not item_id:
        return False
    record = {"id": item_id, "ts": _now_iso()}
    with _ghsupport_lock:
        _append_slack_log(GH_SUPPORT_DISMISSED_FILE, record)
        _maybe_compact_slack_log(GH_SUPPORT_DISMISSED_FILE, ttl_days=GH_SUPPORT_DISMISS_TTL_DAYS)
    _bump_state_version()
    return {"ok": True}


def apply_ghsupport_restore(item_id):
    if not isinstance(item_id, str) or not item_id:
        return False
    with _ghsupport_lock:
        records = load_slack_log(GH_SUPPORT_DISMISSED_FILE, ttl_days=GH_SUPPORT_DISMISS_TTL_DAYS)
        kept = [r for r in records if r.get("id") != item_id]
        _atomic_write_jsonl(GH_SUPPORT_DISMISSED_FILE, kept)
    _bump_state_version()
    return {"ok": True}
```

Note: `load_slack_log`, `_append_slack_log`, `_maybe_compact_slack_log`, and `_atomic_write_jsonl` are generic JSONL utilities already defined in the slack section (lines 5069-5149). They work for any JSONL file despite the "slack" prefix in their names.

- [ ] **Step 4: Add routes to the route table**

In `_ROUTES` (around line 5498, after the `/email/convert` entry), add:

```python
"/ghsupport/dismiss": lambda b: apply_ghsupport_dismiss(b.get("id")),
"/ghsupport/restore": lambda b: apply_ghsupport_restore(b.get("id")),
```

- [ ] **Step 5: Commit**

```bash
git add serve-tasks.py
git commit -m "feat: add GH Support dismiss/restore backend routes and state"
```

---

### Task 3: Dashboard — update GH Support view rendering

**Files:**
- Modify: `serve-tasks.py:1074-1155` (CSS — add dismiss/restore/tracked styles)
- Modify: `serve-tasks.py:3839-3892` (`_render_ghsupport_ticket` — add dismiss button and tracked badge)
- Modify: `serve-tasks.py:3895-3955` (`_build_ghsupport_body` — split active/dismissed, add dismissed section)

- [ ] **Step 1: Add CSS for dismiss button, tracked badge, and dismissed section**

After line 1155 (`.ghsupport-empty-state code {` block), add these styles:

```css
.ghsupport-dismiss {
  color: #6e7681; cursor: pointer; font-size: 11px; padding: 2px 6px;
  border: 1px solid transparent; border-radius: 4px;
}
.ghsupport-dismiss:hover { color: #f85149; border-color: #f8514966; }
.ghsupport-tracked {
  background: rgba(63, 185, 80, 0.15); color: #3fb950;
  font-size: 10px; padding: 1px 6px; border-radius: 10px;
  font-weight: 600; white-space: nowrap;
}
.ghsupport-dismissed-section { margin-top: 16px; }
.ghsupport-dismissed-header {
  color: #8b949e; font-size: 12px; padding: 8px 12px;
  cursor: pointer; display: flex; align-items: center; gap: 6px;
}
.ghsupport-dismissed-header:hover { color: #c9d1d9; }
.ghsupport-dismissed-list { display: none; }
.ghsupport-dismissed-section.expanded .ghsupport-dismissed-list { display: block; }
.ghsupport-dismissed-row {
  display: flex; align-items: center; gap: 8px; padding: 4px 12px;
  font-size: 12px; color: #8b949e;
}
.ghsupport-dismissed-row:hover { background: #161b22; border-radius: 4px; }
.ghsupport-restore {
  color: #6e7681; cursor: pointer; font-size: 11px; padding: 2px 6px;
  border: 1px solid transparent; border-radius: 4px;
}
.ghsupport-restore:hover { color: #3fb950; border-color: #3fb95066; }
```

- [ ] **Step 2: Update `_render_ghsupport_ticket` to add dismiss button and tracked badge**

In `_render_ghsupport_ticket` (line 3839), add a `dismissed=False` parameter to the function signature:

```python
def _render_ghsupport_ticket(ticket, expanded=False, dismissed=False):
```

After the `gmail_link` line (around line 3850), add:

```python
tracked = ticket.get("tracked", False)
```

In the links_html construction (around line 3869-3875), after the Gmail link `if` block and before `links_html += '</div>'`, add:

```python
if not dismissed:
    links_html += (
        f'<span class="ghsupport-dismiss" data-action="ghsupport-dismiss" '
        f'data-ticket-id="{h(tid)}">Dismiss</span>'
    )
```

In the ticket header HTML (around line 3882, after the badge `<a>` tag), add the tracked badge before the status span:

```python
tracked_badge = '<span class="ghsupport-tracked">tracked</span>' if tracked else ''
```

And insert `{tracked_badge}` in the return string, right after the `ghsupport-badge` link and before the `ghsupport-status` span:

```python
f'<a class="ghsupport-badge" href="{h(ticket_url)}" '
f'target="_blank" rel="noopener noreferrer">#{h(tid)}</a>'
f'{tracked_badge}'
f'<span class="ghsupport-status {status_cls}">{status_label}</span>'
```

- [ ] **Step 3: Update `_build_ghsupport_body` to split active/dismissed**

Replace the body of `_build_ghsupport_body` (lines 3895-3955). Keep the error handling (None, malformed, version checks — lines 3896-3919) intact. Replace everything from line 3921 onwards:

```python
    tickets = snapshot.get("tickets") or []
    dismissed_ids = load_ghsupport_dismissed()

    active = [t for t in tickets if str(t.get("ticket_id", "")) not in dismissed_ids]
    dismissed = [t for t in tickets if str(t.get("ticket_id", "")) in dismissed_ids]

    active_sorted = sorted(active, key=lambda t: t.get("last_update", ""), reverse=True)
    dismissed_sorted = sorted(dismissed, key=lambda t: t.get("last_update", ""), reverse=True)

    gen_at = snapshot.get("generated_at", "")
    rel = _slack_relative_time(gen_at) if gen_at else "unknown"
    is_stale = False
    try:
        gen_dt = datetime.datetime.fromisoformat(gen_at) if gen_at else None
    except (TypeError, ValueError):
        gen_dt = None
    if gen_dt is not None:
        now = datetime.datetime.now(gen_dt.tzinfo) if gen_dt.tzinfo else datetime.datetime.now()
        if (now - gen_dt).total_seconds() > 24 * 3600:
            is_stale = True
    stale_badge = ' <span class="ghsupport-stale">stale</span>' if is_stale else ''

    total = len(active) + len(dismissed)
    header = (
        f'<div class="ghsupport-header">'
        f'{total} ticket{"s" if total != 1 else ""} · '
        f'Last refreshed {h(rel)}{stale_badge}'
        f'</div>'
    )

    parts = [header]

    if not active and not dismissed:
        parts.append(
            '<div class="task-card ghsupport-empty-state">'
            '<h3>No GH Support threads</h3>'
            '<p>Run <code>/gh-support-triage</code> in Claude Code to fetch '
            'GitHub Support conversations.</p></div>'
        )
        return "".join(parts)

    for i, ticket in enumerate(active_sorted):
        parts.append(_render_ghsupport_ticket(ticket, expanded=(i == 0)))

    if dismissed_sorted:
        dismissed_rows = []
        for t in dismissed_sorted:
            tid = t.get("ticket_id") or "?"
            subj = t.get("subject") or "(no subject)"
            wo = t.get("waiting_on") or "?"
            dismissed_rows.append(
                f'<div class="ghsupport-dismissed-row" data-ticket-id="{h(tid)}">'
                f'<a class="ghsupport-badge" href="{h(t.get("ticket_url") or "")}" '
                f'target="_blank" rel="noopener noreferrer">#{h(tid)}</a>'
                f'<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{h(subj)}</span>'
                f'<span class="ghsupport-status {"waiting-us" if wo == "us" else "waiting-github"}">'
                f'{"Waiting on us" if wo == "us" else "Waiting on GitHub"}</span>'
                f'<span class="ghsupport-restore" data-action="ghsupport-restore" '
                f'data-ticket-id="{h(tid)}">Restore</span>'
                f'</div>'
            )
        parts.append(
            f'<div class="ghsupport-dismissed-section">'
            f'<div class="ghsupport-dismissed-header" data-action="toggle-dismissed">'
            f'<span class="ghsupport-chevron">▸</span>'
            f'Dismissed ({len(dismissed_sorted)})'
            f'</div>'
            f'<div class="ghsupport-dismissed-list">'
            f'{"".join(dismissed_rows)}'
            f'</div>'
            f'</div>'
        )

    return "".join(parts)
```

- [ ] **Step 4: Update empty state text in error states**

In the error handling at the top of `_build_ghsupport_body`, update the text in the "No GH Support data yet" state (line 3900) and "No tracked tickets" state (line 3926) to reference `/gh-support-triage` instead of `/email`:

```python
# "No data yet" state:
'<p>Run <code>/gh-support-triage</code> in Claude Code to populate this view. '
'GitHub Support conversations from the last 7 days will appear here.'

# Remove the "No tracked tickets" block entirely — this is now handled in the
# main body after the active/dismissed split (the empty state there covers it).
```

- [ ] **Step 5: Commit**

```bash
git add serve-tasks.py
git commit -m "feat: ghsupport view with dismiss/restore UI and tracked badge"
```

---

### Task 4: Dashboard — add GH Support dismiss/restore JS event handlers

**Files:**
- Modify: `serve-tasks.py:2380-2393` (JS click handler — add dismiss/restore/toggle-dismissed handlers)

- [ ] **Step 1: Add JS handlers**

In the JavaScript click handler section, after the existing GH Support ticket toggle code (around line 2393, just before the closing `});` of the click listener), add:

```javascript
  // GH Support dismiss button
  var ghDismiss = e.target.closest('[data-action="ghsupport-dismiss"]');
  if (ghDismiss) {
    e.preventDefault();
    e.stopPropagation();
    var ticketId = ghDismiss.dataset.ticketId;
    if (ticketId) _post('/ghsupport/dismiss', {id: ticketId}).then(function(r) {
      if (r.ok) {
        var card = ghDismiss.closest('.ghsupport-ticket');
        if (card) card.remove();
      }
    });
    return;
  }
  // GH Support restore button
  var ghRestore = e.target.closest('[data-action="ghsupport-restore"]');
  if (ghRestore) {
    e.preventDefault();
    e.stopPropagation();
    var rTicketId = ghRestore.dataset.ticketId;
    if (rTicketId) _post('/ghsupport/restore', {id: rTicketId}).then(function(r) {
      if (r.ok) {
        var row = ghRestore.closest('.ghsupport-dismissed-row');
        if (row) row.remove();
      }
    });
    return;
  }
  // GH Support dismissed section toggle
  var ghDismissedToggle = e.target.closest('[data-action="toggle-dismissed"]');
  if (ghDismissedToggle) {
    e.preventDefault();
    e.stopPropagation();
    var sec = ghDismissedToggle.closest('.ghsupport-dismissed-section');
    if (sec) {
      sec.classList.toggle('expanded');
      var ch = sec.querySelector('.ghsupport-dismissed-header .ghsupport-chevron');
      if (ch) ch.textContent = sec.classList.contains('expanded') ? '▾' : '▸';
    }
    return;
  }
```

- [ ] **Step 2: Commit**

```bash
git add serve-tasks.py
git commit -m "feat: JS handlers for ghsupport dismiss/restore/toggle"
```

---

### Task 5: Email skill cleanup — remove GH Support logic

**Files:**
- Modify: `~/.claude/skills/email/SKILL.md`

- [ ] **Step 1: Add exclusion to Gmail query**

In step 2's `gmail_query` (line with `is:unread newer_than:2d`), append `-from:githubsupport.com`:

```
gmail_query: is:unread newer_than:2d -category:promotions -category:updates -from:noreply -from:no-reply -from:evince@spotify.com -from:githubsupport.com
```

- [ ] **Step 2: Remove watched tickets loading from step 1**

Delete the entire second code block in step 1 (the `python3 -c` block that extracts ticket IDs from tasks-live.json, approximately lines 23-35) and the sentence "The second command prints the GH Support ticket IDs..." below it. Step 1 should only have the seen cache load.

- [ ] **Step 3: Remove GH Support deduplication from step 2**

In the slim-down script in step 2, remove the GH Support thread dedup logic. The script should just output all results with truncated content (no special handling for GH Support). Remove:
- The `gh_seen_threads` dict and the `if 'githubsupport.com'` branch
- The `non_gh` list and `deduped` reassembly
- The `is_gh` check for content truncation

The simplified script just truncates all emails to 300 chars:

```python
out = []
for r in results:
    out.append({'id': r.get('id',''), 'thread': r.get('threadId',''),
                'sender': r.get('from',''), 'subject': r.get('subject',''),
                'content': (r.get('content') or '')[:300],
                'date': r.get('date',''), 'link': r.get('link',''),
                'to': r.get('to',''), 'cc': r.get('cc','')})
```

- [ ] **Step 4: Remove GH Support filtering rule from step 3**

Delete rule #2 entirely ("GitHub Support emails" block). Renumber remaining rules.

- [ ] **Step 5: Remove GH Support category from step 4.5**

In the classification instructions, remove the `gh_support` category. All items should be classified as `"general"` category with appropriate tier. Remove:
- The line: `` `category`: `"gh_support"` if sender contains `githubsupport.com`, else `"general"` ``
- Replace with: `` `category`: `"general"` ``
- Remove `gh_ticket_id` extraction from the item schema

- [ ] **Step 6: Remove step 4.6 entirely**

Delete the entire "### 4.6. Write GH Support triage snapshot" section.

- [ ] **Step 7: Remove GH Support from tuned exclusions table**

Delete the row: `| GH Support not in watched tickets | post-filter | Only surface updates for tickets tracked in tasks-live.json |`

- [ ] **Step 8: Commit**

```bash
git add ~/.claude/skills/email/SKILL.md
git commit -m "refactor: remove GH Support logic from email skill"
```

---

### Task 6: Dashboard email view cleanup — remove GH Support section

**Files:**
- Modify: `serve-tasks.py:3692-3703` (`_build_email_body` — remove gh_support section)

- [ ] **Step 1: Remove GH Support section from email view**

In `_build_email_body`, remove lines 3692-3703:

```python
# DELETE these lines:
    gh_support = [it for it in visible if it.get("category") == "gh_support"]
    general = [it for it in visible if it.get("category") != "gh_support"]
```

Replace with just:

```python
    general = visible
```

This removes the GH Support Tickets section (purple `#bc8cff`) from the email view entirely. The `by_tier` dict on the next line already iterates `general`.

Also delete the `if gh_support:` block (lines 3699-3703):

```python
# DELETE:
    if gh_support:
        parts.append(_render_email_section(
            "GH Support Tickets", gh_support,
            color="#bc8cff", collapsed=False,
        ))
```

- [ ] **Step 2: Commit**

```bash
git add serve-tasks.py
git commit -m "refactor: remove GH Support section from email view"
```

---

### Task 7: Manual verification

- [ ] **Step 1: Start the dashboard and verify the ghsupport view**

```bash
python3 ~/todo/serve-tasks.py &
```

Open `http://localhost:6419/?view=ghsupport` in a browser. Verify:
- Empty state shows "Run `/gh-support-triage`..." message
- No errors in console

- [ ] **Step 2: Test with a sample snapshot**

Write a test snapshot to verify the UI renders correctly:

```bash
python3 -c "
import json, os, tempfile, datetime

tickets = [
    {
        'ticket_id': '4263381',
        'ticket_url': 'https://support.github.com/ticket/enterprise/669/4263381',
        'ticket_code': 'ABC-1234',
        'subject': 'GHES upgrade issue on 3.19',
        'category': 'GitHub Enterprise Server Administration',
        'raised_by': 'Joshua Vigar',
        'raised_at': '2026-05-20T10:00:00+00:00',
        'last_update': '2026-05-26T15:30:00+00:00',
        'waiting_on': 'github',
        'message_count': 3,
        'tracked': True,
        'messages': [
            {'author': 'Joshua Vigar', 'ts': '2026-05-20T10:00:00+00:00', 'is_support': False, 'content': 'We are seeing errors after upgrading to 3.19...'},
            {'author': 'GH Support Agent', 'ts': '2026-05-22T14:00:00+00:00', 'is_support': True, 'content': 'Thank you for reporting. Can you share the logs?'},
            {'author': 'Joshua Vigar', 'ts': '2026-05-26T15:30:00+00:00', 'is_support': False, 'content': 'Here are the logs from the affected instance.'},
        ],
        'gmail_link': 'https://mail.google.com/mail/u/0/#inbox/abc123',
    },
    {
        'ticket_id': '9999999',
        'ticket_url': 'https://support.github.com/ticket/enterprise/669/9999999',
        'ticket_code': 'XYZ-5678',
        'subject': 'Actions runner connectivity issue',
        'category': 'General support request',
        'raised_by': 'Team Member',
        'raised_at': '2026-05-25T09:00:00+00:00',
        'last_update': '2026-05-27T11:00:00+00:00',
        'waiting_on': 'us',
        'message_count': 2,
        'tracked': False,
        'messages': [
            {'author': 'Team Member', 'ts': '2026-05-25T09:00:00+00:00', 'is_support': False, 'content': 'Our runners cannot reach the server.'},
            {'author': 'GH Support', 'ts': '2026-05-27T11:00:00+00:00', 'is_support': True, 'content': 'We recommend checking your firewall settings.'},
        ],
        'gmail_link': '',
    },
]

snapshot = {
    'version': 1,
    'generated_at': datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec='seconds'),
    'tickets': tickets,
}

target = os.path.expanduser('~/todo/gh-support-triage.json')
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target))
with os.fdopen(fd, 'w') as f:
    json.dump(snapshot, f, ensure_ascii=False, indent=2)
os.replace(tmp, target)
print('Wrote', target)
"
```

Refresh `?view=ghsupport`. Verify:
- Two tickets visible, sorted by last_update (Actions runner first — more recent)
- First ticket expanded, second collapsed
- "tracked" badge appears on ticket #4263381, not on #9999999
- "Waiting on us" (orange) on #9999999, "Waiting on GitHub" (blue) on #4263381
- Dismiss button visible on each ticket
- Support Portal and Gmail links work

- [ ] **Step 3: Test dismiss/restore**

Click "Dismiss" on ticket #9999999. Verify:
- Ticket disappears from main list
- Page refreshes via SSE (or manual refresh)
- Dismissed section appears at bottom with "Dismissed (1)"
- Click to expand dismissed section — shows compact row with Restore button
- Click "Restore" — ticket returns to main list

- [ ] **Step 4: Verify email view cleanup**

Open `?view=email`. Verify:
- No "GH Support Tickets" section (purple header) appears
- Only Action Needed, FYI, and Already Handled sections show

- [ ] **Step 5: Clean up test data**

```bash
rm ~/todo/gh-support-triage.json ~/todo/ghsupport-dismissed.jsonl 2>/dev/null
```
