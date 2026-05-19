---
id: 1
title: Proactive GitHub event/action type alerts
source_type: slack
source_url: https://spotify.slack.com/archives/C060E60FZ89/p1778833589794979
captured: 2026-05-15
status: new
tags: [hotsauce, pipeline, github-events, hotnews]
---

## Problem / Opportunity
New GitHub event/action types appear without warning and break the HotNews pipeline because there's no data endpoint to receive them. The team only discovers new types when the pipeline fails. Some action types (like `repository_dispatch` actions) aren't documented in GitHub's techdocs, so they can't be added preemptively to the whitelist.

## Proposed Approach
- Hook into the HotNews topic to detect when a new event+action type arrives that has no matching data endpoint
- Send a notification/alert to the team so someone can create the PR for the new endpoint
- Gradually shift the alerts channel toward surfacing more important issues
- Longer term: automate the endpoint creation (autogenerate data endpoint + auto-PR), though a PR still needs human merge

## Source Context
- **Rebecca Portelli**: "The issue is that the pipeline only fails because there is no data endpoint to post the new events to." Creating all possible endpoints preemptively would leave many empty, and undocumented action types can't be whitelisted ahead of time.
- **Rebecca**: Dropping new types would create a backfill nightmare if someone later needs them.
- **Josh Vigar**: Suggested a notification as a good first step — alerting when a new type is detected so the team can process the PR.
- **Rebecca**: Confirmed this is doable by hooking into the HotNews topic and checking if a data endpoint exists for the new event+action type.

## Notes
- First step is notification only — don't need to auto-create endpoints yet
- Rebecca confirmed the hook-into-HotNews-topic approach is feasible
- This would reduce noise in the alerts channel by replacing pipeline-failure alerts with actionable "new type detected" alerts
