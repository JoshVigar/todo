---
id: 3
title: "CRANE: resolve owner from PR branch for new components"
source_type: slack
source_url: https://spotify.slack.com/archives/C07MZJBV18A/p1779442159468709
captured: 2026-05-22
status: new
tags: [crane, services-pilot, goalie]
---

## Problem / Opportunity
When a PR creates a brand new component in services-pilot, CRANE evaluates ownership from the base branch (master) where the component doesn't exist yet. It falls back to `__anyone-fallback__`, which has no configured approvers — so no reviewers are requested at all. The PR is stuck until someone manually approves.

The `service-info.yaml` / `lib-info.yaml` in the PR branch already declares an owner team, but CRANE doesn't read it.

## Proposed Approach
- For new components (detected via `__anyone-fallback__`), read the `owner` field from the PR branch's `*-info.yaml`
- Request review from that owner team instead of silently assigning no one
- Fallback to current behaviour if no info yaml exists in the PR branch either

## Source Context
Tomas Aschan raised PR #108006 (`feat(control-plane-health-dashboard)`) — a new dashboard component under `declarative-infra/core/`. The `service-info.yaml` declared `owner: manifesto` but CRANE logged "No approvers for key __anyone__" and requested no reviewers. Josh cc'd Kai Mallea (CRANE maintainer).

## Notes
- This is a crane2 behaviour — check whether `ghe/crane2` already has any PR-branch resolution logic
- Could also apply to renamed/moved components where the info yaml path changed
