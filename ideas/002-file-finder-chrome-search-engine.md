---
id: 2
title: Add query param search to backend-monorepo-file-finder
source_type: prompt
source_url: https://snow.spotify.net/s/backend-monorepo-file-finder/
captured: 2026-05-22
status: new
tags: [monorepo, developer-tools, contribution]
---

## Problem / Opportunity
Matt Brown's backend-monorepo-file-finder is a fast fuzzy file finder for services-pilot, deployed as a Snow app. Currently you have to visit the page and type your query. Adding query param support (e.g. `?q=some/path`) would let it be registered as a Chrome custom search engine, making it instantly accessible from the address bar.

## Proposed Approach
- Add URL query parameter parsing (e.g. `?q=`) that pre-populates and triggers the fuzzy search on page load
- This enables Chrome's "site search" / custom search engine feature: set the URL to `https://snow.spotify.net/s/backend-monorepo-file-finder/?q=%s`

## Source Context
Matt Brown posted the tool in #monorepo-collab on 2026-05-20 as a hack week project. Source code is not on GHE yet — Matt is considering pushing it. Uses fuzzysort for fuzzy matching.

## Notes
- Depends on Matt pushing the source to GHE first
- Small contribution — likely a few lines of JS to read `URLSearchParams` on load
