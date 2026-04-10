---
description: Pagination and completeness rules for external APIs and the gh CLI — never work on partial result sets
---

## Core Principle

**Never work on a partial result set.** Paginated APIs return a subset by default — always explicitly request the full dataset before drawing conclusions, counting, filtering, or ranking.

A silent truncation (30 of 300 items) is worse than an error — it produces a confidently wrong answer.

## GitHub CLI (`gh`)

Default page size is 30 items. Always override:

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

- `--limit 30` (default) is never acceptable for analysis tasks — set it at least 10× higher than you expect
- When counting ("how many open issues"), use `--paginate` or a high `--limit`; verify the count is plausible
- `gh api` without `--paginate` returns one page only — always add `--paginate` for completeness
- The `--limit` requirement applies equally when using `--json` + `--jq` — the default 30-item cap is not lifted by `--json`; always pair with `--limit 1000` or higher

## REST APIs (curl / WebFetch)

- Check response for pagination signals: `Link` header (GitHub-style), `next_cursor`, `next_page_token`, `has_more`, `total_count`
- If `total_count` > items returned → you have a partial result; fetch remaining pages
- Loop until no next-page signal; never stop after one response
- If the response contains no pagination signals and the item count is a round number (10, 20, 25, 50, 100…), treat it as a likely default page size and verify by requesting page 2; if page 2 returns items, the first response was truncated

## GraphQL APIs

- Check `pageInfo.hasNextPage` — if `true`, issue another query with `after: endCursor`
- Never treat a single query result as complete if `hasNextPage` is not explicitly `false`

## Cloud / Google-style APIs (next_page_token)

- Check for `nextPageToken` (or `next_page_token`) in the response body
- If present and non-empty, pass it as `pageToken=<value>` on the next request
- Stop when the field is absent or empty string — dataset is complete

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

| Signal                                           | Action                                                  |
| ------------------------------------------------ | ------------------------------------------------------- |
| Response includes `total_count` or `total` field | Compare against items received; fetch more if not equal |
| Task involves counting, ranking, or "all X"      | Mandate complete data before proceeding                 |
| First response looks suspiciously small          | Verify — check for truncation before continuing         |
