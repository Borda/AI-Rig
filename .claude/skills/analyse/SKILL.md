---
name: analyse
description: Analyze GitHub issues, Pull Requests (PRs), Discussions, and repo health for an Open Source Software (OSS) project. Summarizes long threads, assesses PR readiness, detects duplicates, extracts reproduction steps, and generates repo health stats. Uses gh Command Line Interface (CLI) for GitHub Application Programming Interface (API) access. Complements oss-maintainer agent.
argument-hint: <number|discussion <number>|health|dupes [keyword]|contributors|ecosystem>
allowed-tools: Read, Bash, Write
context: fork
---

<objective>

Analyze GitHub issues and PRs to help maintainers triage, respond, and decide quickly. Produces actionable, structured output — not just summaries.

</objective>

<inputs>

- **$ARGUMENTS**: one of:
  - Number (e.g. `42`) — auto-detects issue vs PR
  - `discussion <number>` (e.g. `discussion 15`) — analyze a GitHub Discussion thread
  - `health` — generate repo issue/PR health overview
  - `dupes [keyword]` — find potential duplicate issues
  - `contributors` — top contributor activity and release cadence
  - `ecosystem` — downstream consumer impact analysis for library maintainers

</inputs>

<workflow>

## Auto-Detection (for numeric arguments)

When `$ARGUMENTS` starts with `discussion`, skip auto-detection and route directly to **Discussion Analysis** mode.

When `$ARGUMENTS` is a number, determine whether it is an issue or a PR before routing.
**Default assumption: issue** — this skill is primarily for issue analysis.
The command below determines the type; the `if/else` routing block is illustrative pseudocode — the actual mode switch is by natural-language interpretation of the result.

```bash
# GitHub's issues API covers both issues and PRs.
# A PR has a "pull_request" key; a plain issue does not.
TYPE=$(gh api "repos/{owner}/{repo}/issues/$ARGUMENTS" \
  --jq 'if .pull_request then "pr" else "issue" end' 2>/dev/null || echo "issue")

if [ "$TYPE" = "pr" ]; then
  # → Route to PR Analysis mode
else
  # → Route to Issue Analysis mode
fi
```

## Mode: Issue Analysis

```bash
# Fetch issue details
gh issue view $ARGUMENTS --json number,title,body,labels,comments,createdAt,author,state

# Fetch all comments
gh issue view $ARGUMENTS --comments
```

Produce:

````
## Issue #[number]: [title]

**State**: [open/closed] | **Author**: @[author] | **Age**: [X days]
**Labels**: [current labels]

### Summary
[2-3 sentence plain-language summary of the issue]

### Thread Verdict
[If thread contains a verified/confirmed solution: extract it here with attribution]
[If no verified solution: "No confirmed solution in thread." — skip thread detail]

### Root Cause Hypotheses

| # | Hypothesis | Probability | Reasoning |
|---|-----------|-------------|-----------|
| 1 | [most likely cause] | [high/medium/low] | [why — reference specific code paths] |
| 2 | [alternative cause] | [medium/low] | [why] |
| 3 | [less likely] | [low] | [why] |

### Code Evidence

For the top hypothesis, trace through relevant code:

```[language]
# [file:line] — [what this code does and why it relates to the hypothesis]
[relevant code snippet]
```

### Suggested Labels

[labels to add/remove based on analysis]

### Suggested Response

[draft reply — or "close as duplicate of #X"]

[Use Markdown formatting: wrap function/class/method names in backticks (`func_name`), wrap code samples in fenced blocks with language tag]

### Priority

[Critical / High / Medium / Low] — [rationale]

````

Write the full report to `tasks/output-analyse-issue-$ARGUMENTS-$(date +%Y-%m-%d).md` using the Write tool — **do not print the full analysis to terminal**.

Read the compact terminal summary template from `.claude/skills/_shared/terminal-summaries.md` — use the **Issue Summary** template. Replace `[skill-specific path]` with `tasks/output-analyse-issue-$ARGUMENTS-$(date +%Y-%m-%d).md`.

## Mode: PR Analysis

Run all three `gh` commands in parallel — they are independent API calls:

```bash
# --- run these three in parallel ---

# PR metadata
gh pr view $ARGUMENTS --json number,title,body,labels,reviews,statusCheckRollup,files,additions,deletions,commits,author

# CI status
gh pr checks $ARGUMENTS

# Files changed
gh pr diff $ARGUMENTS --name-only
```

Produce:

```
## PR #[number]: [title]

**Author**: @[author] | **Size**: +[additions]/-[deletions] lines, [N] files
**CI**: [passing/failing/pending]

### Recommendation
[🟢 Approve / 🟡 Minor Suggestions / 🟠 Request Changes / 🔴 Block] — [one-sentence justification]

### Completeness
_Legend: ✅ present · ⚠️ partial · ❌ missing · 🔵 N/A_
- [✅/⚠️/❌/🔵] Clear description of what changed and why
- [✅/⚠️/❌/🔵] Linked to a related issue (`Fixes #NNN` or `Relates to #NNN`)
- [✅/⚠️/❌/🔵] Tests added/updated (happy path, failure path, edge cases)
- [✅/⚠️/❌/🔵] Docstrings (NumPy or Google style, consistent with project) for all new/changed public APIs
- [✅/⚠️/❌/🔵] No secrets or credentials introduced
- [✅/⚠️/❌/🔵] Linting and CI checks pass

### Quality Scores
- Code: n/5 [emoji] — [reason]
- Testing: n/5 [emoji] — [reason]
- Documentation: n/5 [emoji] — [reason]

### Risk: n/5 [low / medium / high] [emoji] — [brief description]
- Breaking changes: [none / detail]
- Performance: [none / detail]
- Security: [none / detail]
- Compatibility: [none / detail]

### Must Fix
1. [blocking issue]

### Suggestions (non-blocking)
1. [improvement]

### Next Steps
1. [most important action for the author]
2. [second action]
```

Write the full report to `tasks/output-analyse-pr-$ARGUMENTS-$(date +%Y-%m-%d).md` using the Write tool — **do not print the full analysis to terminal**.

Read the compact terminal summary template from `.claude/skills/_shared/terminal-summaries.md` — use the **PR Summary** template. Replace `[entity-line]` with `PR #$ARGUMENTS — [title]` and replace `[skill-specific path]` with `tasks/output-analyse-pr-$ARGUMENTS-$(date +%Y-%m-%d).md`.

## Mode: Discussion Analysis

When `$ARGUMENTS` starts with `discussion` (e.g., `discussion 15`), route directly here.

```bash
DISC_NUM=${ARGUMENTS#discussion }

gh api graphql -f query='
  query($owner: String!, $repo: String!, $number: Int!) {
    repository(owner: $owner, name: $repo) {
      discussion(number: $number) {
        title
        body
        author { login }
        category { name }
        answer { body author { login } createdAt }
        comments(first: 50) {
          nodes { body author { login } createdAt }
        }
        labels(first: 10) { nodes { name } }
        closed
        closedAt
        createdAt
      }
    }
  }' -f owner='{owner}' -f repo='{repo}' -F number=$DISC_NUM
```

If the query returns null for `discussion`, output:

```
⚠ Discussions not enabled or discussion #[number] not found on this repository.
```

and stop.

Produce:

```
## Discussion #[number]: [title]

**State**: [open/closed] | **Author**: @[author] | **Age**: [X days]
**Category**: [category name]
**Labels**: [current labels, or "none"]

### Summary
[2-3 sentence plain-language summary of the discussion topic and current state]

### Thread Verdict
[If discussion has a marked answer: extract it here with attribution]
[If no marked answer: "No accepted answer." — note the most useful response if one is clear]

### Key Viewpoints

| # | Position | Author | Support Level |
|---|----------|--------|---------------|
| 1 | [main viewpoint or request] | @[author] | [high/medium/low engagement] |
| 2 | [alternative viewpoint] | @[author] | [medium/low] |

### Actionable Outcome
[concrete recommendation — e.g. "convert to issue", "mark as answered", "add to docs", "close as resolved"]

### Suggested Labels
[labels to add/remove based on discussion content]
```

Write the full report to `tasks/output-analyse-discussion-$DISC_NUM-$(date +%Y-%m-%d).md` using the Write tool — **do not print the full analysis to terminal**.

Read the compact terminal summary template from `.claude/skills/_shared/terminal-summaries.md` — use the **Discussion Summary** template. Replace `[skill-specific path]` with `tasks/output-analyse-discussion-$DISC_NUM-$(date +%Y-%m-%d).md`.

## Mode: Repo Health Overview

Run all three `gh` commands in parallel — they are independent API calls:

```bash
# --- run these three in parallel ---

# Open issues count and age distribution
gh issue list --state open --json number,createdAt,labels --limit 200

# Stale issues (no activity > 90 days)
gh issue list --state open --json number,title,updatedAt --limit 200 | \
  jq '[.[] | select(.updatedAt < (now - 7776000 | todate))]'

# Open PRs
gh pr list --state open --json number,title,createdAt,reviews,statusCheckRollup
```

Produce:

```
## Repo Health: [repo]

### Issue Summary
- Open issues: [N]
- Stale (>90 days): [N] — [list top 5]
- Needs triage (no labels): [N]
- Bugs: [N] | Enhancements: [N] | Questions: [N]

### PR Summary
- Open PRs: [N]
- Awaiting review: [N]
- CI failing: [N]
- Stale (>30 days): [N]

### Recommended Actions
1. [most urgent triage action]
2. [second]
3. [third]
```

Write the full report to `tasks/output-analyse-health-$(date +%Y-%m-%d).md` using the Write tool — **do not print the full analysis to terminal**.

Read the compact terminal summary template from `.claude/skills/_shared/terminal-summaries.md` — use the **Repo Health Summary** template. Replace `[skill-specific path]` with `tasks/output-analyse-health-$(date +%Y-%m-%d).md`.

## Mode: Duplicate Detection

```bash
# Search existing issues for keyword
gh issue list --state all --search "$ARGUMENTS" --json number,title,state --limit 50
```

Group by similarity and output:

```
## Potential Duplicates for: "[keyword]"

### Group 1: [theme]
- #[N]: [title] ([state])
- #[N]: [title] ([state])
Canonical: #[oldest open issue] — suggest closing others as duplicates

### Unique (not duplicates)
- #[N]: [title] — [why it's distinct]

### Recommendations
1. Close #[N] as duplicate of #[canonical] — add comment: "Closing as duplicate of #[canonical]"
2. [Next highest-impact triage action]
3. [Any label additions or reassignments]
```

Write the full report to `tasks/output-analyse-dupes-$(date +%Y-%m-%d).md` using the Write tool — **do not print the full analysis to terminal**.

Read the compact terminal summary template from `.claude/skills/_shared/terminal-summaries.md` — use the **Duplicate Detection Summary** template. Replace `[skill-specific path]` with `tasks/output-analyse-dupes-$(date +%Y-%m-%d).md`.

## Mode: Contributor Activity

```bash
# Top contributors in last 90 days
gh api "repos/{owner}/{repo}/stats/contributors" \
  | jq '[.[] | {author: .author.login, commits: .total, last_week: .weeks[-1]}] | sort_by(-.commits) | .[:10]'

# Release cadence
gh release list --limit 20 --json tagName,publishedAt \
  | jq '[.[] | .publishedAt[:10]]'
```

Produce:

```
## Contributor Activity: [repo]

### Top Contributors (90 days)
| Author | Commits | Trend |
|--------|---------|-------|
| @... | N | ... |

### Release Cadence
- Average: [N days] between releases
- Last release: [date] ([tag])
- Overdue? [yes/no based on cadence]

### Recommendations
1. [Most urgent action — e.g., "cut overdue release", "review stale PRs", "thank top contributor"]
2. [Bus factor concern if ≥60% commits from one author — suggest onboarding new contributors]
3. [Cadence suggestion if overdue]
```

Write the full report to `tasks/output-analyse-contributors-$(date +%Y-%m-%d).md` using the Write tool — **do not print the full analysis to terminal**.

Read the compact terminal summary template from `.claude/skills/_shared/terminal-summaries.md` — use the **Contributor Activity Summary** template. Replace `[skill-specific path]` with `tasks/output-analyse-contributors-$(date +%Y-%m-%d).md`.

## Mode: Ecosystem Impact (for library maintainers)

When assessing the impact of a change on downstream users:

Replace `mypackage` in the commands below with the actual package name (e.g., from `gh repo view --json name --jq .name`).

```bash
# Find downstream dependents on GitHub
gh api "search/code" --field "q=from mypackage import language:python" \
  --jq '[.items[].repository.full_name] | unique | .[]'

# Check PyPI reverse dependencies (who depends on us?)
# Requires johnnydep: pip install johnnydep (not installed by default — skip if unavailable)
# johnnydep mypackage --fields=name --reverse 2>/dev/null || echo "johnnydep not available — skipping PyPI reverse deps"

# Check conda-forge feedstock dependents
gh api "search/code" --field "q=mypackage repo:conda-forge/*-feedstock filename:meta.yaml" \
  --jq '[.items[].repository.full_name] | .[]'
```

Produce:

```
## Ecosystem Impact: [change description]

### Downstream Consumers Found
- [repo]: uses [specific API being changed]

### Breaking Risk
- [High/Medium/Low] — [N] known consumers of changed API
- Migration path: [available / needs documentation]

### Recommended Communication
- [create migration guide / add deprecation warning / notify maintainers directly]
```

Write the full report to `tasks/output-analyse-ecosystem-$(date +%Y-%m-%d).md` using the Write tool — **do not print the full analysis to terminal**.

Read the compact terminal summary template from `.claude/skills/_shared/terminal-summaries.md` — use the **Ecosystem Impact Summary** template. Replace `[skill-specific path]` with `tasks/output-analyse-ecosystem-$(date +%Y-%m-%d).md`.

End your response with a `## Confidence` block per CLAUDE.md output standards.

</workflow>

<notes>

- This skill uses mode dispatch (`## Mode: X` sections) rather than sequential numbered steps — each mode is self-contained
- Always use `gh` CLI — never hardcode repo URLs
- Run `gh auth status` first if commands fail; user may need to authenticate
- For closed issues/PRs, note the resolution so history is useful
- Don't post responses without explicit user instruction — only draft them
- **Forked context**: this skill runs with `context: fork` — it operates without access to the current conversation history. All required context (PR number, issue URL, branch name) must be provided as the skill argument or in your prompt.
- Follow-up chains:
  - Issue with confirmed bug → `/develop fix` to diagnose, reproduce with test, and apply targeted fix
  - Issue is a feature request → `/develop feature` for TDD-first implementation
  - Issue with code smell or structural problem → `/develop refactor` for test-first improvements
  - PR with quality concerns → `/review` for comprehensive multi-agent code review
  - Draft responses or comments to be posted publicly → `oss-maintainer` for final wording and voice

</notes>
