# Mode: Thread Analysis (Issue, Discussion, or PR)

All three are GitHub conversation threads — same analysis structure, different API fetch. `TYPE` is set by auto-detection in SKILL.md (`issue`, `discussion`, or `pr`). `NUMBER` = the item number (strip `discussion ` prefix if present).

**Cache check first**: if `$CACHE_FILE` exists (see SKILL.md Cache layer), read `item` and `comments` from it — skip the primary fetch. Still run wide-net searches (never cached). For PRs: `gh pr checks` and `gh pr diff` are never cached — always live.

If cache miss, run all fetches in parallel:

```bash
# --- run these in parallel ---

if [ "$TYPE" = "issue" ]; then

    gh issue view $NUMBER --json number,title,body,labels,comments,createdAt,author,state
    gh issue view $NUMBER --comments
    # After both complete: write cache (see SKILL.md Cache layer write pattern)

elif [ "$TYPE" = "pr" ]; then

    gh pr view $NUMBER --json number,title,body,labels,reviews,statusCheckRollup,files,additions,deletions,commits,author
    gh pr checks $NUMBER           # never cached — always live
    gh pr diff $NUMBER --name-only # never cached — always live
    # After pr view completes: write cache (see SKILL.md Cache layer write pattern)

else # discussion

    gh api graphql -f query='
    query($owner: String!, $repo: String!, $number: Int!) {
      repository(owner: $owner, name: $repo) {
        discussion(number: $number) {
          title body createdAt closed closedAt
          author { login }
          category { name }
          answer { body author { login } createdAt }
          comments(first: 50) { nodes { body author { login } createdAt } }
          labels(first: 10) { nodes { name } }
        }
      }
    }' -f owner='{owner}' -f repo='{repo}' -F number=$NUMBER
    # If query returns null → print "⚠ Discussions not enabled or #N not found" and stop
    # After complete: write cache (see SKILL.md Cache layer write pattern)

fi

# Wide-net: same for all types — all related items open AND closed
TITLE=$(...) # extract from fetched item above

gh issue list --state all --search "$TITLE" --json number,title,state,labels --limit 50 |
jq --argjson self $NUMBER '[.[] | select(.number != $self)]'

gh pr list --state all --search "$TITLE" --json number,title,state --limit 30 |
jq --argjson self $NUMBER '[.[] | select(.number != $self)]'

gh api graphql -f query='
  query($owner:String!,$repo:String!){
    repository(owner:$owner,name:$repo){
      discussions(first:100,orderBy:{field:UPDATED_AT,direction:DESC}){
        nodes { number title closed }
      }
    }
  }' -f owner='{owner}' -f repo='{repo}' 2>/dev/null |
jq --arg q "$TITLE" --argjson self $NUMBER '
      .data.repository.discussions.nodes // [] |
      map(select(.number != $self) |
          select(.title | ascii_downcase | contains(($q | ascii_downcase | split(" ") | .[0]))))
    '
```

Produce:

````
## Thread #[number]: [title]

**Type**: [Issue | Pull Request | Discussion]
**State**: [open/closed] | **Author**: @[author] | **Age**: [X days]
**Labels**: [labels, or "none"]
**Category**: [category]        ← discussion only; omit for issue/PR
**CI**: [passing/failing/pending]  ← PR only; omit for issue/discussion
**Size**: +[N]/-[N] lines, [N] files  ← PR only; omit for issue/discussion

### Summary
[2-3 sentence plain-language summary of the thread topic and current state]

### Thread Verdict
[Confirmed solution, accepted answer, or PR recommendation — or "No confirmed resolution."]

### Related Items

**⚠ Potential Duplicates** (same problem/question — suggest closing as duplicate):
- #N: [title] ([open/closed]) ← DUPLICATE — [why: same error / same root cause / same question]
  Canonical: #[lowest-number] — close others with "Closing as duplicate of #[canonical]"

**Related** (same area, distinct problem — cross-link):
- Issue #N: [title] ([state]) — [one-line distinction]
- PR #N: [title] ([state]) — [one-line distinction]
- Discussion #N: [title] — [one-line distinction]

_If no related items found: "No related items found."_

### Analysis

<!-- Issue: root cause + code evidence -->
**Root Cause Hypotheses** _(issue only)_:

| # | Hypothesis | Probability | Reasoning |
|---|-----------|-------------|-----------|
| 1 | [most likely cause] | [high/medium/low] | [why — reference specific code paths] |
| 2 | [alternative cause] | [medium/low] | [why] |

**Code Evidence** _(issue only)_:
```[language]
# [file:line] — [what this code does and why it relates to the hypothesis]
[relevant code snippet]
```

<!-- Discussion: viewpoints -->
**Key Viewpoints** _(discussion only)_:

| # | Position | Author | Support Level |
|---|----------|--------|---------------|
| 1 | [main viewpoint or request] | @[author] | [high/medium/low engagement] |
| 2 | [alternative viewpoint] | @[author] | [medium/low] |

<!-- PR: completeness + quality + risk -->
**Completeness** _(PR only)_:
_Legend: ✅ present · ⚠️ partial · ❌ missing · 🔵 N/A_
- [✅/⚠️/❌/🔵] Clear description of what changed and why
- [✅/⚠️/❌/🔵] Linked to a related issue (`Fixes #NNN` or `Relates to #NNN`)
- [✅/⚠️/❌/🔵] Tests added/updated (happy path, failure path, edge cases)
- [✅/⚠️/❌/🔵] Docstrings for all new/changed public APIs
- [✅/⚠️/❌/🔵] No secrets or credentials introduced
- [✅/⚠️/❌/🔵] Linting and CI checks pass

**Quality Scores** _(PR only)_:
- Code: n/5 — [reason]
- Testing: n/5 — [reason]
- Documentation: n/5 — [reason]

**Risk** _(PR only)_: n/5 [low/medium/high] — [description]
- Breaking changes: [none / detail]
- Performance: [none / detail]
- Security: [none / detail]

**Must Fix** _(PR only)_:
1. [blocking issue]

**Suggestions** _(PR only, non-blocking)_:
1. [improvement]

### Suggested Labels
[labels to add/remove]

### Suggested Response
[draft reply — or "close as duplicate of #X" — or "merge" / "request changes" for PRs]
[Use Markdown: wrap names in backticks, code samples in fenced blocks with language tag]

### Priority
[Critical / High / Medium / Low] — [rationale]  ← omit for discussions
````

Run `mkdir -p .reports/analyse/thread` then write the full report to `.reports/analyse/thread/output-analyse-thread-$NUMBER-$(date +%Y-%m-%d).md` using the Write tool — **do not print the full analysis to terminal**.

Read the compact terminal summary template from `.claude/skills/_shared/terminal-summaries.md` — use the **Issue Summary** template. Replace `[skill-specific path]` with `.reports/analyse/thread/output-analyse-thread-$NUMBER-$(date +%Y-%m-%d).md`, ensuring the block opens with `---` on its own line, the entity line follows on the next line, the `→ saved to <path>` line is present at the end, and the block closes with `---` on its own line after it. After printing to the terminal, also prepend the same compact block to the top of the report file using the Edit tool — insert it at line 1 so the file begins with the compact summary followed by a blank line, then the existing `## Thread #[number]:` content.

**⛔ DO NOT STOP — `REPLY_MODE=true`**: Skip the Confidence block here — it is emitted in SKILL.md Step 6 after the reply, or as the last step of SKILL.md if not in reply mode. Proceed **immediately** to the "Draft contributor reply" section in SKILL.md (Step 7). Your response is not complete until you have spawned oss-shepherd and written the reply file.
