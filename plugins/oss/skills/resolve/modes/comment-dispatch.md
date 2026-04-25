**Re: Compress markdown to caveman format**

# Comment Dispatch — oss:resolve independent entry point

Reached when `$ARGUMENTS` = bare comment text (not PR number or URL). File read + executed by `/oss:resolve` Step 12.

## Step 12: Comment dispatch + Codex review loop

Reached when $ARGUMENTS = bare comment text (not PR number or URL).

Create task:

```text
TaskCreate(
  subject="Resolve: <60-char summary of $ARGUMENTS>",
  description="<full $ARGUMENTS>",
  activeForm="Resolving comment"
)
```

If `CODEX_AVAILABLE=false`: stop with `⚠ codex plugin not found — install: /plugin marketplace add openai/codex-plugin-cc && /plugin install codex@openai-codex && /reload-plugins`, mark task completed:

```text
TaskUpdate(task_id=<task_id_from_above>, status="completed")
```

and stop.

### 12a: Dispatch

```bash
touch /tmp/claude-commit-authorized  # timeout: 3000
```

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

If code changed: apply the **Step 9 lint and QA gate pattern** from the main resolve workflow — same parallel spawn of `foundry:linting-expert` + `foundry:qa-specialist`, commit lint fixes, surface blocking QA issues. Use `$RUN_DIR/linting-expert-step12c.md` and `$RUN_DIR/qa-specialist-step12c.md` as output paths. Revoke commit authorization after gate completes.

Mark task `completed`:

```text
TaskUpdate(task_id=<task_id_from_above>, status="completed")
```

Then print:

```markdown
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
