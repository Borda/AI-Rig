<!-- file: github-review-parsing.md — consumers: analyse/modes/thread.md, resolve/modes/pr-intelligence.md -->

# GitHub review parsing — fetch completeness, collapsed findings, cross-round dedup

Applies whenever a PR's existing bot/human reviews are read for triage. Three rules.

## 1. Fetch completeness

Both endpoints are mandatory — one alone misses content only the other has:

- `gh api repos/<owner>/<repo>/pulls/<n>/reviews --paginate` — review-level bodies, authoritative. Carries embedded collapsed findings (rule 2). `gh pr view --json reviews` is a convenience alternative (GraphQL-backed, no `--paginate`) — its cap on PRs with many review rounds is unverified; prefer the REST endpoint above when completeness matters, never treat the two as interchangeable without checking.
- `gh api repos/<owner>/<repo>/pulls/<n>/comments --paginate` — inline per-line review comments. No review-body substitute exists; skipping this endpoint means every inline thread is invisible.

**Cache note**: both calls are per-review-round data — never serve them from a cache keyed before the latest push. A consumer that skips its "primary fetch" block on a cache hit must still run these two, or a same-day re-run reproduces exactly the under-reporting this file exists to prevent.

## 2. Collapsed-block expansion

A review body is not always one finding. Bots pack multiple findings into a collapsed `<details><summary>...</summary>` block instead of posting them as separate inline comments — observed patterns: Copilot's `### Suppressed comments (N)`, CodeRabbit's `🔎 Suppressed comment(s)` / walkthrough sections. A review whose footer says "Comments generated: 1" can still carry a dozen more findings hidden in this block.

Read the full raw body text (already fetched by rule 1, no separate call needed). When it contains one of these blocks: treat **each nested finding** inside as its own item — its own file, line, one-line summary — never the whole review body as a single unit. No regex/parser needed; read the markdown directly, same as any other comment. **Mark provenance**: a finding pulled out of a suppressed/collapsed block is the bot's own lower-confidence output (that's why it wasn't posted directly) — carry a note (e.g. `origin: suppressed-block`) so a downstream severity/confidence filter can tell it apart from a posted finding, never silently indistinguishable from one.

## 3. Cross-round dedup

Bots re-review on every push, which commonly shifts line numbers — so "same finding, different line" is the normal recurring case, not the exception. Two or more bots can also independently flag the same spot. Group candidates to avoid re-listing the same underlying finding — but grouping must never merge two genuinely distinct findings that happen to share only a filename; a shared file with nothing else in common is not evidence of recurrence.

Group two candidates together only when ALL three are true: same file, wording is a close/near-identical match, AND position is consistent with recurrence (exact line match, OR lines differ by an amount explainable by an intervening push, OR either item has no line — a `discussion`-location item never carries one). Wording match is never optional — position alone (same file, same or nearby line, unrelated wording) never groups; two unrelated findings can legitimately share a line or sit near one another.

Collapse each group to ONE item: keep the most-resolvable member's `location`/`url` (`inline` — real thread — beats `discussion` beats a `report` item), the highest severity seen in the group, and the union of classification codes when members disagree. Report the group's size (members folded in) — a consumer's own aggregate (e.g. "how many groups had >1 member") is derived from these per-group sizes, not a separate count to track independently. This is separate from any GitHub-vs-`/review`-report dedup a consumer skill runs elsewhere — this rule is GitHub-vs-GitHub, same source across rounds, and always applies regardless of mode.
