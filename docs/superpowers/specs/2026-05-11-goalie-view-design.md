# Goalie View Design

**Date:** 2026-05-11
**Branch:** goalie-view (from main)

## Overview

Add a dedicated "Goalie" tab to the tasks dashboard. The view shows Today's Focus followed by the goalie subsections (Start here, Then, Handover). It is always visible in the view switcher regardless of rotation status. When off rotation it shows a muted message instead of goalie sections.

## Architecture & Data Flow

```
journal/YYYY-Www.md
    #### Goalie
        ##### Start here
        ##### Then
        ##### Handover
        
          │
          ▼  build_tasks.py (when on_goalie=true)
          
tasks-live.json
    sections: [
      { "type": "goalie", "title": "Start here", "tasks": [...] },
      { "type": "goalie", "title": "Then",        "tasks": [...] },
      { "type": "goalie", "title": "Handover",    "tasks": [...] },
      ...core sections...
    ]
    
          │
          ▼  serve-tasks.py  (?view=goalie)
          
Goalie view:
    Today's Focus card
    Start here card
    Then card
    Handover card      (or "off rotation" message)
```

## Components

### `build_tasks.py`

- Read `on_goalie` from `--goalie-cache`.
- **When on rotation:** call `parse_goalie_sections(journal_lines, weekday_header)` from `tasklib.py`. Convert each non-empty subsection (Start here / Then / Handover) into a `"type": "goalie"` section in the JSON with tasks that have stable `id` fields.
- **ID stability:** match by task name (case-insensitive) against existing JSON goalie tasks; assign `max_id + 1` for new tasks. Same logic as core tasks.
- **When off rotation:** emit no goalie sections.
- Empty subsections (no `[ ]` tasks) are omitted from output.

### `serve-tasks.py`

- Add `"goalie"` to `VIEWS` dict.
- Add `render_goalie_view(data)`:
  - Render Today's Focus card using existing `render_core_section`.
  - For each `"type": "goalie"` section in `data["sections"]`: render via updated `render_goalie_section`.
  - If no goalie sections present: render a muted "Not on goalie rotation this week" card.
- Update `render_goalie_section`: emit interactive `tr[data-id]` rows (same structure as core rows — `#` cell, task name, links, status). Enables complete, uncomplete, edit, right-click move, drag-and-drop via existing endpoints with no new server-side code.
- Route `view="goalie"` in `build_page()`.
- Keep existing goalie rendering in `render_dashboard` and `render_classic` (shows goalie sections at top of those views when on rotation).

### `tasklib.py`

No changes. `parse_goalie_sections` is already implemented and handles Start here / Then / Handover subsections.

### Tests

- `test_build_tasks.py`: add cases for goalie section parsing when on/off rotation, ID assignment for new and existing goalie tasks, empty subsection omission.

## Interaction Model

Goalie task rows support the same interactions as core task rows:
- Click `#` cell → complete (moves to `completed_today`)
- Right-click → section move context menu
- Drag-and-drop reorder within section
- Edit via modal

Completing a goalie task moves it to `completed_today` in the JSON. The journal `[x]` update remains manual, consistent with the rest of the system.

## Off-Rotation State

When `on_goalie=false` in the goalie cache, `build_tasks.py` emits no goalie sections. `render_goalie_view` detects the absence and renders a muted card:

> _Not on goalie rotation this week._

Today's Focus still renders above it.

## Out of Scope

- Auto-switching to goalie view when rotation starts
- Writing `[x]` back to the journal on task completion
- Goalie task add via the `/add` endpoint (goalie tasks are journal-sourced)
