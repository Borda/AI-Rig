---
name: resolve
description: Implement applicable PR review suggestions via Codex — fetch GitHub PR review comments, classify each as implement/skip/flag, dispatch actionable ones straight to Codex, then validate with lint, types, and tests.
argument-hint: <PR number | GitHub PR URL | omit to use /review output>
disable-model-invocation: true
allowed-tools: Read, Bash, Grep, Glob, Agent
---

<objective>

Automate applying actionable suggestions from two sources:

- **Mode A** (PR number or URL given): fetch GitHub reviewer comments on a PR, classify each as implement/skip/flag, dispatch actionable ones to Codex in parallel.
- **Mode B** (no argument): read the most recent `/review` output from `tasks/output-review-*.md`, extract mechanical findings, dispatch them to Codex in parallel.

In both modes: validate with lint, type checks, and tests after all dispatches complete.

Good for: reviewer-requested renames, docstring improvements, style fixes, missing type annotations, off-by-one corrections, and any clearly-scoped mechanical change.

Not for: re-architecting code, resolving discussions without a clear correct answer, or applying changes where intent is ambiguous.

</objective>

<inputs>

- **$ARGUMENTS**: one of:
  - A PR number (e.g., `42`) or GitHub PR URL — **Mode A**: fetch and implement GitHub reviewer comments
  - Omitted — **Mode B**: read the most recent `tasks/output-review-*.md` and implement its mechanical findings

</inputs>

<workflow>

**Task tracking**: per CLAUDE.md, create tasks (TaskCreate) for each major phase. Mark in_progress/completed throughout. On loop retry or scope change, create a new task.

## Step 0: Detect mode

If `$ARGUMENTS` is provided (PR number or URL) → **Mode A**: proceed to Step 1.

If `$ARGUMENTS` is empty → **Mode B**: skip Steps 1–3, follow the section below, then continue at Step 4.

______________________________________________________________________

### Mode B: From /review output

**Pre-flight:** Run the same codex pre-flight check as Step 1 (verify `which codex`; if missing, stop and show the install hint).

**Find review output:**

```bash
ls -t tasks/output-review-*.md 2>/dev/null | head -1
```

If no file found: stop with `No review output found. Run \`/review <number>\` first to generate it, or provide a PR number.\`

Read the most recent file and extract **mechanical, clearly-scoped findings** suitable for Codex:

| What to extract                         | Examples                                                |
| --------------------------------------- | ------------------------------------------------------- |
| Missing docstrings on public APIs       | "Public function `foo()` has no docstring"              |
| Missing type annotations                | "Return type missing on `process()`"                    |
| Consistent renames flagged across files | "Rename `x` → `scale_factor` (Static Analysis section)" |
| Simple, unambiguous bug fixes           | Explicit corrections with the correct answer stated     |

Do **not** extract: architectural concerns, performance analysis, security findings, or anything requiring human judgment — classify those as `flag`.

Map each extracted item to a unified entry: `source: review`, `author: <agent name>`, `file: <file path if stated>`, `line: <line if stated>`, `body: <finding text>`.

Replace the PR header in Step 7 with: `## Review: tasks/output-review-<date>.md`

**Continue at Step 4.**

______________________________________________________________________

## Step 1: Pre-flight

Run both checks before doing anything else:

```bash
gh auth status
```

If auth fails: stop with `Pre-flight failed: gh auth not configured. Run 'gh auth login' first.`

```bash
which codex
```

If codex is not on PATH: stop with `Pre-flight failed: codex not found on PATH. Install with: npm install -g @openai/codex`

## Step 2: Parse PR reference and detect repo

Extract the PR number from `$ARGUMENTS`:

- URL pattern (`https://github.com/.../pull/N`): extract the trailing integer
- Otherwise: use as-is

Detect the repo and check out the PR branch (assumes maintainer access):

```bash
OWNER_REPO=$(gh repo view --json nameWithOwner -q .nameWithOwner)
OWNER=${OWNER_REPO%%/*}
REPO=${OWNER_REPO##*/}
PR_NUM=<extracted number>
gh pr checkout $PR_NUM
```

## Step 3: Fetch PR data

Run all four fetches (issue them in parallel — single response, no waiting between calls):

```bash
# 1. PR metadata + top-level review bodies
gh pr view $PR_NUM --json number,title,body,headRefName,author,reviews,files

# 2. Inline line-level comments — includes diff position and suggestion blocks
gh api "repos/$OWNER_REPO/pulls/$PR_NUM/comments"

# 3. Review-level state (APPROVED, CHANGES_REQUESTED, COMMENTED)
gh api "repos/$OWNER_REPO/pulls/$PR_NUM/reviews"

# 4. Thread resolution state via GraphQL
gh api graphql -f query='
{
  repository(owner: "'"$OWNER"'", name: "'"$REPO"'") {
    pullRequest(number: '"$PR_NUM"') {
      reviewThreads(first: 100) {
        nodes {
          isResolved
          comments(first: 1) {
            nodes { databaseId }
          }
        }
      }
    }
  }
}'
```

Build a `resolved_ids` set from the GraphQL response: collect the `databaseId` of the first comment in every thread where `isResolved` is `true`.

Consolidate into a unified comment list. Each entry tracks:

| Field          | Description                                                                               |
| -------------- | ----------------------------------------------------------------------------------------- |
| `source`       | `inline` or `review_body`                                                                 |
| `author`       | reviewer login                                                                            |
| `file`         | file path (inline only)                                                                   |
| `line`         | line number (inline only)                                                                 |
| `body`         | full comment text                                                                         |
| `suggestion`   | verbatim suggestion block if present (```` ```suggestion ... ``` ```` fence), else `null` |
| `review_state` | `CHANGES_REQUESTED` \| `COMMENTED` \| `APPROVED`                                          |
| `resolved`     | `true` if the comment's thread appears in `resolved_ids`, else `false`                    |

## Step 4: Classify comments

Read all consolidated comments and assign each a decision:

| Decision      | Criteria                                                                                                                                                                        |
| ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **implement** | Actionable code change; clearly correct and unambiguous; maps to a specific file + location or has a suggestion block; not purely a matter of taste; `resolved` must be `false` |
| **skip**      | Compliment, question, "please explain", already addressed, purely aesthetic with no clear right answer; **or `resolved` is `true`** (thread already resolved on GitHub)         |
| **flag**      | Architectural decision, contradicts another reviewer, high-risk or large blast radius, requires domain context not visible in the diff                                          |

Print the full classification table immediately — then proceed straight to Codex dispatch without pausing:

```
## PR #<N> — <title>

### Classification
| # | Reviewer      | Location         | Preview (first 80 chars)                | Resolved | Decision    |
|---|---------------|------------------|-----------------------------------------|----------|-------------|
| 1 | @alice        | src/foo.py:42    | "rename `x` to `scale_factor`"          | no       | implement   |
| 2 | @bob          | —                | "Great work overall!"                   | no       | skip        |
| 3 | @charlie      | src/bar.py       | "Consider splitting this class into..." | no       | flag        |
| 4 | @alice        | src/foo.py:10    | "Add return type annotation"            | yes      | skip        |

implement: N  |  skip: N  |  flag: N
```

If there are zero `implement` items: stop here and print the summary.

## Step 5: Dispatch to Codex (parallel)

Spawn one `general-purpose` subagent per `implement` item. Issue **all spawns in a single response** — do not wait for one to finish before starting the next.

Each subagent prompt follows this template (substitute `<file>`, `<line>`, and either the suggestion block or reviewer intent):

**With a suggestion block** — quote verbatim:

```
Run this bash command and report the exit code and any stderr:

codex exec "use the sw-engineer to apply this reviewer suggestion:
\`\`\`
<suggestion block content verbatim>
\`\`\`
The suggestion is on <file> around line <N>. Apply it at the correct location and make any related changes needed to keep the code consistent." --sandbox workspace-write
```

**Without a suggestion block** — describe the change:

```
Run this bash command and report the exit code and any stderr:

codex exec "use the sw-engineer to <reviewer intent in plain terms>. Context: comment by @<reviewer> on <file> around line <N>: \"<body first 200 chars>\"" --sandbox workspace-write
```

Rules:

- **All spawns in one response** — maximum parallelism; each subagent owns exactly one suggestion
- **Max 1 attempt per suggestion** — if Codex exits non-zero or produces no diff, the subagent marks it `failed` in its return value; no retry
- **No auto-revert** — leave the working tree as-is so the user can inspect partial results and decide what to keep

After all subagents complete, collect results and print a status line per item:

```
✓ [1/N] src/foo.py:42 — renamed x → scale_factor
✗ [2/N] src/bar.py:17 — Codex exit non-zero
✓ [3/N] src/baz.py:8  — added missing type annotation
```

> **Note**: parallel dispatch is safe when suggestions touch different files. If two suggestions target the same file, Codex processes may produce conflicting edits — review `git diff HEAD` carefully and re-run `/resolve` with just the conflicting items if needed.

## Step 6: Validate

After all dispatches, run validation in this order.

**1. Show what changed:**

```bash
git diff HEAD --stat
```

**2. Linting — once across everything, after all changes have landed:**

Check the repo's standard linting command by reading `Makefile`, `pyproject.toml`, or `.github/workflows/`. Run whichever is configured (e.g. `make lint`, `ruff check .`, `pre-commit run --all-files`).

**3. Type checking:**

Run the repo's type checker per its configuration (e.g. `make typecheck`, `mypy src/`, `pyright`).

**4. Full test suite:**

Run all tests — not just the affected modules:

```bash
# use the repo's standard test command (make test, pytest, tox, etc.)
```

## Step 7: Report

Print a structured summary:

```
## PR #<N> — Implementation Report

### Applied (<N> items)
| # | Location         | Change summary             | Status     |
|---|------------------|----------------------------|------------|
| 1 | src/foo.py:42    | renamed x → scale_factor   | ✓ applied  |

### Skipped (<N> items)
| # | Reviewer | Reason           |
|---|----------|------------------|
| 2 | @bob     | compliment       |

### Flagged — needs human judgment (<N> items)
| # | Reviewer  | Location   | Issue                                 |
|---|-----------|------------|---------------------------------------|
| 3 | @charlie  | src/bar.py | architectural — consider splitting... |

### Validation
- Diff: <N> files changed, <M> insertions(+), <K> deletions(-)
- Lint: clean / N issues remaining
- Types: clean / N issues remaining
- Tests: PASS (N passed) / FAIL — see output above

### Next Steps
- [ ] Review `git diff HEAD` and commit when satisfied
- [ ] Resolve flagged items manually (see table above)
- [ ] Run `/review` for a full quality pass if changes are non-trivial

## Confidence
**Score**: [coverage of the review: how thoroughly all comments were classified and dispatched — deduct for skipped categories, ambiguous items left unresolved, or incomplete codex runs]
**Gaps**: comment classification is heuristic — ambiguous comments may be miscategorised; Codex application accuracy depends on specificity of reviewer comment; tests may not cover all changed paths
```

</workflow>

<notes>

- **`disable-model-invocation: true`**: must be explicitly invoked with `/resolve <number>` or `/resolve` — never auto-triggered
- **Mode B (from /review)**: reads `tasks/output-review-*.md` — only mechanical, clearly-scoped findings are extracted; architectural items are always flagged; run `/review <number>` first to generate the input file. The `/review <number>` → `/resolve` chain is the full review-and-apply workflow.
- **No pause after classification**: the table is printed for visibility but the skill proceeds straight to Codex dispatch — interrupt (Ctrl-C) before subagents start if you spot a problem in the table
- **Parallel dispatch**: one subagent per `implement` item, all spawned in a single response for maximum speed; suggestions on different files are safely independent; suggestions targeting the same file may conflict — review `git diff HEAD` before committing
- **No auto-revert on failure**: failed Codex attempts leave the working tree intact; the user decides what to keep or discard with `git restore`
- **Suggestion blocks**: GitHub-flavoured suggestion fences (```` ```suggestion ... ``` ````) are extracted verbatim and passed to Codex — the most faithful way to implement them
- **Resolved thread filtering**: a fourth GraphQL fetch collects threads where `isResolved: true`; their comments are automatically classified `skip` and shown with `Resolved: yes` in the table — they are never dispatched to Codex
- **REST endpoints**: `gh api "repos/{owner}/{repo}/pulls/{N}/comments"` returns all inline review comments; filter by `position != null` if you want only comments on current diff lines (not on outdated diffs)
- **`Bash(gh api:*)`** permission covers both REST and GraphQL `gh api` calls — no separate graphql permission needed
- **`flag` category**: surfaces architectural concerns explicitly so the user never misses them — they are not silently skipped
- Related agents: `sw-engineer` (Codex internal agent for implementation), `linting-expert` (validation), `qa-specialist` (test coverage)
- Follow-up chains:
  - Changes pass validation → commit and push; optionally re-request review
  - Flagged architectural items remain → address via `/feature` or `/refactor` with explicit scope
  - Validation fails → inspect `git diff HEAD`, fix manually or run `/fix` on specific errors
  - Want a quality pass on all changes → `/review` after implementing

</notes>
