**Re: Compress markdown to caveman format**

# Comment Dispatch — oss:resolve independent entry point

Reached when `$ARGUMENTS` = bare comment text (not PR number or URL). File read + executed by `/oss:resolve` Step 12.

______________________________________________________________________

## Step 12: Comment dispatch + Codex review loop

Reached when $ARGUMENTS = bare comment text (not PR number or URL).

Create task:

```
TaskCreate(
  subject="Resolve: <60-char summary of $ARGUMENTS>",
  description="<full $ARGUMENTS>",
  activeForm="Resolving comment"
)
```

If `CODEX_AVAILABLE=false`: stop with `⚠ codex plugin not found — install: /plugin marketplace add openai/codex-plugin-cc && /plugin install codex@openai-codex && /reload-plugins`, mark task completed:

```
TaskUpdate(task_id=<task_id_from_above>, status="completed")
```

and stop.

### 12a: Dispatch

```bash
Agent(subagent_type="codex:codex-rescue", prompt="Apply this review comment to the codebase. If the change is already present, or the comment has no actionable code change, make no changes and briefly explain why. Comment: $ARGUMENTS")
```

Record initial dispatch outcome (code changed or no change + reason).

### 12b: Codex review loop (max 5 passes)

```bash
git diff HEAD --stat # timeout: 3000 — confirm there are changes to review
```

No changes: skip loop; set `CODEX_REVIEW_FINDINGS=""`.

Otherwise:

```pseudocode
for REVIEW_PASS in 1 2 3 4 5; do  # pseudocode — not shell

  # Review phase — Agent() is a Claude Code tool call, not a shell command
  CODEX_OUT = Agent(subagent_type="codex:codex-rescue",
                    prompt="Review working-tree changes. End output with ISSUES_FOUND=N.")
  ISSUES_FOUND = parse CODEX_OUT for ISSUES_FOUND=N (default 0)

  if ISSUES_FOUND == 0: break

  # Fix phase
  Agent(subagent_type="codex:codex-rescue",
        prompt="Apply this fix: <issue description from review>")

done

if REVIEW_PASS == 5 and ISSUES_FOUND > 0:
  echo "⚠ Review loop hit 5-pass cap — $ISSUES_FOUND issues remain; surface to user"
```

### 12c: Lint and QA gate

If code changed (dispatch or review loop produced commits):

```bash
RUN_DIR=".reports/resolve/$(date -u +%Y-%m-%dT%H-%M-%SZ)"
mkdir -p "$RUN_DIR" # timeout: 5000
```

Spawn both agents in parallel:

```
Agent(foundry:linting-expert): "Review all files changed in HEAD (git diff HEAD~N..HEAD where N = number of commits just made). List every lint/type violation. Apply inline fixes for any that are auto-fixable. Write your full findings to $RUN_DIR/linting-expert-step12c.md using the Write tool, then return ONLY a compact JSON envelope: {fixed: N, remaining: N, files: [...]}."

Agent(foundry:qa-specialist, maxTurns: 15): "Review all files changed in the most recent commits for correctness, edge cases, and regressions. Flag any blocking issues. Write your full findings to $RUN_DIR/qa-specialist-step12c.md using the Write tool, then return ONLY a compact JSON envelope: {blocking: N, warnings: N, issues: [...]}."
```

> **Health monitoring**: Agent calls synchronous; Claude awaits natively. No response within ~15 min → surface partial results from `$RUN_DIR` with ⏱.

- `linting-expert` made changes → commit:

```bash
git add $(git diff HEAD --name-only)                          # timeout: 3000
git commit -m "$(cat <<'EOF'
lint: auto-fix violations after resolve cycle

---
Co-authored-by: Claude Code <noreply@anthropic.com>
EOF
)"  # timeout: 3000
```

- `foundry:qa-specialist` reports blocking issues → fix inline, re-run once; surface unresolved issues in report

Mark task `completed`:

```
TaskUpdate(task_id=<task_id_from_above>, status="completed")
```

Then print:

```
## Resolve Report

**Verdict**: ✓ resolved | ⊘ no change — <Codex's reason>

### Codex Review
<findings across passes, or "No issues found" / "Skipped — no changes">

### Lint + QA
<linting-expert summary: N fixes applied | or "no violations"> / <foundry:qa-specialist summary: N blocking fixed, N warnings | or "clean">

**Next**: review diff and commit | reply to reviewer with Codex's explanation

## Confidence
**Score**: [0.N]
**Gaps**: [e.g. Codex partial completion, ambiguous comment intent]
**Refinements**: N passes. — omit if 0 passes
```
