---
description: Pagination and completeness rules for external APIs and the gh CLI — never work on partial result sets
paths:
  - '**'
---

## Core Principle

**Never work on partial result set.** Paginated APIs return subset by default — always request full dataset before drawing conclusions, counting, filtering, or ranking.

Silent truncation (30 of 300 items) worse than error — produces confidently wrong answer.

## GitHub CLI (`gh`)

Default page size 30. Always override:

```bash
# List commands — raise the limit explicitly
gh issue list --limit 1000
gh pr list --state all --limit 500
gh release list --limit 100

# API calls — use --paginate to follow all pages automatically
gh api repos/:owner/:repo/issues --paginate
gh api repos/:owner/:repo/pulls --paginate --field state=all

# Combine: paginate + jq for large result sets
# --paginate emits one JSON array per page; jq '[.[]]'
```

Rules:

- `--limit 30` (default) never acceptable for analysis tasks — set at least 10× higher than expected
- Counting ("how many open issues") → use `--paginate` or high `--limit`; verify count plausible
- `gh api` without `--paginate` returns one page only — always add `--paginate`
- `--limit` requirement applies with `--json` + `--jq` too — 30-item cap not lifted by `--json`; pair with `--limit 1000` or higher

## REST APIs (curl / WebFetch)

- Check response for pagination signals: `Link` header (GitHub-style), `next_cursor`, `next_page_token`, `has_more`, `total_count`
- `total_count` > items returned → partial result; fetch remaining pages
- Loop until no next-page signal; never stop after one response
- No pagination signals + round item count (10, 20, 25, 50, 100…) → likely default page size; verify by requesting page 2; if page 2 returns items, first response was truncated

## GraphQL APIs

- Check `pageInfo.hasNextPage` — if `true`, issue another query with `after: endCursor`
- Never treat single query result as complete if `hasNextPage` not explicitly `false`

## Cloud / Google-style APIs (next_page_token)

- Check for `nextPageToken` (or `next_page_token`) in response body
- Present and non-empty → pass as `pageToken=<value>` on next request
- Stop when field absent or empty string — dataset complete

Example:

```bash
# Google Cloud style — loop on nextPageToken
next_token=""
all_items=()
while true; do
    url="https://api.example.com/v1/items"
    [ -n "$next_token" ] && url="${url}?pageToken=${next_token}"
    response=$(curl -s "$url")
    all_items+=($(echo "$response" | jq -r ".items[]"))
    next_token=$(echo "$response" | jq -r ".nextPageToken // empty")
    [ -z "$next_token" ] && break
done
```

## General Rules

| Signal | Action |
| --- | --- |
| Response includes `total_count` or `total` field | Compare against items received; fetch more if not equal |
| Task involves counting, ranking, or "all X" | Mandate complete data before proceeding |
| First response looks suspiciously small | Verify — check for truncation before continuing |
