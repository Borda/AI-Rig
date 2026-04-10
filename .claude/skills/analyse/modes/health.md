# Mode: Repo Health Overview

Run all five `gh` commands in parallel — they are independent API calls:

```bash
# --- run these in parallel ---

# Open issues: count, age, labels (for triage stats and stale detection)
gh issue list --state open --json number,title,createdAt,updatedAt,labels --limit 200

# Open PRs with review and CI status
gh pr list --state open --json number,title,createdAt,reviews,statusCheckRollup

# All issues open+closed (for duplicate clustering)
gh issue list --state all --json number,title,state,labels,createdAt --limit 200

# All PRs open+closed (for duplicate clustering — same bug may have a related PR)
gh pr list --state all --json number,title,state,createdAt --limit 100

# All discussions open+closed (for duplicate clustering — questions/proposals may duplicate issues)
gh api graphql -f query='
  query($owner:String!,$repo:String!){
    repository(owner:$owner,name:$repo){
      discussions(first:100,orderBy:{field:UPDATED_AT,direction:DESC}){
        nodes { number title closed createdAt }
      }
    }
  }' -f owner='{owner}' -f repo='{repo}' 2>/dev/null
```

Produce:

```
## Repo Health: [repo]

### Issue Summary
- Open issues: [N]
- Stale (>90 days): [N] — [list top 5 by title]
- Needs triage (no labels): [N]
- Bugs: [N] | Enhancements: [N] | Questions: [N]

### PR Summary
- Open PRs: [N]
- Awaiting review: [N]
- CI failing: [N]
- Stale (>30 days): [N]

### Duplicates

Group all issues, PRs, and discussions (open and closed) by their shared duplication root —
the specific element that makes them the same problem: identical error message, identical
feature ask, or identical root cause even if symptoms differ. Flag as RELATED (not duplicate)
when items share a component/area but have distinct problems.

#### Group 1
**Root**: [the shared key — e.g. exact error message, exact feature request, exact failure mode]
- Issue #N: [title] ([open/closed]) — created [date]  ← CANONICAL
- Issue #N: [title] ([open/closed]) ← DUPLICATE
- PR #N: [title] ([state]) ← related fix
- Discussion #N: [title] ([open/closed]) ← DUPLICATE
  → Close duplicates with: "Closing as duplicate of #[canonical]"

_(Repeat for each group. If no duplicate groups found: "No obvious duplicates detected.")_

### Recommended Actions
1. [most urgent triage action]
2. [second]
3. [third]
```

Run `mkdir -p .reports/analyse/health` then write the full report to `.reports/analyse/health/output-analyse-health-$(date +%Y-%m-%d).md` using the Write tool — **do not print the full analysis to terminal**.

Read the compact terminal summary template from `.claude/skills/_shared/terminal-summaries.md` — use the **Repo Health Summary** template. Replace `[skill-specific path]` with `.reports/analyse/health/output-analyse-health-$(date +%Y-%m-%d).md`, ensuring the output begins with `---` on its own line, followed by the entity line on the next line, includes a `→ saved to <path>` line at the end, and closes with `---` on its own line after it. After printing to the terminal, also prepend the same compact block to the top of the report file using the Edit tool — insert it at line 1 so the file begins with the compact summary followed by a blank line, then the existing `## Repo Health:` content.
