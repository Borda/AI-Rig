# External Data & API Completeness

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
gh api repos/:owner/:repo/issues --paginate | jq '[.[]]'
```

Rules:

- `--limit 30` (default) is never acceptable for analysis tasks — set it at least 10× higher than you expect
- When counting ("how many open issues"), use `--paginate` or a high `--limit`; verify the count is plausible
- `gh api` without `--paginate` returns one page only — always add `--paginate` for completeness

## REST APIs (curl / WebFetch)

- Check response for pagination signals: `Link` header (GitHub-style), `next_cursor`, `next_page_token`, `has_more`, `total_count`
- If `total_count` > items returned → you have a partial result; fetch remaining pages
- Loop until no next-page signal; never stop after one response

## GraphQL APIs

- Check `pageInfo.hasNextPage` — if `true`, issue another query with `after: endCursor`
- Never treat a single query result as complete if `hasNextPage` is not explicitly `false`

## General Rules

| Signal                                           | Action                                                  |
| ------------------------------------------------ | ------------------------------------------------------- |
| Response includes `total_count` or `total` field | Compare against items received; fetch more if not equal |
| Default limit not explicitly overridden          | Always override — never rely on the default             |
| Task involves counting, ranking, or "all X"      | Mandate complete data before proceeding                 |
| First response looks suspiciously small          | Verify — check for truncation before continuing         |
