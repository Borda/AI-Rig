---
name: session
description: Session parking lot — automatically parks diverging ideas and unanswered questions to project-scoped memory; /session resume shows pending items, /session archive closes them, /session summary gives a session digest
argument-hint: resume | archive <text> | summary
allowed-tools: Read, Write, Edit, Glob, Grep, TaskCreate, TaskUpdate, Bash, AskUserQuestion
effort: low
model: sonnet
context: fork
---

<objective>

Track open-loop ideas, deferred questions, diverging threads — without losing to context compaction or session end. Three on-demand commands (`resume`, `archive`, `summary`) plus behavioral parking rule that writes `session-open-*.md` memory files as items arise.

</objective>

<inputs>

- **$ARGUMENTS**: required. Three modes:
  - `resume` (alias: `pending`) — list all open `session-open-*.md` memory files for this project, grouped by age; items ≥ 14 days get `⚠ stale` prefix; items ≥ 30 days deleted silently before listing
  - `archive <partial-text>` — fuzzy-match parked item by name or content, delete memory file, append audit entry to `.claude/logs/session-archive.jsonl`
  - `summary` — compact session digest: completed tasks, parked items, recent git commits since session start; follows output-routing rule (≤10 lines → terminal; longer → `.temp/output-session-summary-<date>.md`)

</inputs>

<constants>

- Memory dir: `$HOME/.claude/projects/$(git rev-parse --show-toplevel | sed 's|[/.]|-|g' | sed 's/^-//')/memory/`
- Canonical MEMORY_DIR snippet (use in every bash block that needs the path):
  ```bash
  PROJECT="$(git rev-parse --show-toplevel)"
  SLUG="$(echo "$PROJECT" | sed 's|[/.]|-|g' | sed 's|^-||')"
  MEMORY_DIR="$HOME/.claude/projects/$SLUG/memory/"
  ```
- File pattern: `session-open-*.md`
- Resolution log: `.claude/logs/session-archive.jsonl`
- Stale threshold: 14 days (add `⚠ stale` prefix when listing)
- Delete threshold: 30 days (silently remove before listing)
- Max open items: 10 (surface list and ask to archive before parking new ones)

</constants>

<workflow>

**Task hygiene**: Before creating tasks, call `TaskList`. For each found task:

- status `completed` if work clearly done
- status `deleted` if orphaned / no longer relevant
- keep `in_progress` only if genuinely continuing

## Step 0: Validate and dispatch mode

Extract the first word of `$ARGUMENTS` as `MODE`.

If `MODE` matches one of:
- `resume` or `pending` → proceed to **Mode: resume**
- `archive` → proceed to **Mode: archive**
- `summary` → proceed to **Mode: summary**

Otherwise (empty, unrecognized, or misspelled `$ARGUMENTS`): use `AskUserQuestion`:

> "Which session mode did you want?"
> Options: (a) `resume` — list all open parked items, (b) `archive <name>` — close a parked item by name, (c) `summary` — compact digest of this session's work

## Mode: resume (list pending items)

### Step 1: Resolve the memory directory

Derive MEMORY_DIR using canonical snippet from `<constants>`.

```bash
# Use canonical MEMORY_DIR from <constants>
PROJECT="$(git rev-parse --show-toplevel)"
SLUG="$(echo "$PROJECT" | sed 's|[/.]|-|g' | sed 's|^-||')"
MEMORY_DIR="$HOME/.claude/projects/$SLUG/memory/"
echo "$MEMORY_DIR"
```

### Step 2: Age-out expired items (≥ 30 days) silently

```bash
# MEMORY_DIR derived in Step 1 — reuse that value
find "$MEMORY_DIR" -name "session-open-*.md" -mtime +30 -delete 2>/dev/null # timeout: 5000
echo "cleanup done"
```

### Step 3: Collect remaining items and compute age

Use Glob with pattern `session-open-*.md` in memory directory. For each file, read with Read tool to extract `name` and `description` frontmatter fields and item body.

Compute age in days per file:

```bash
# MEMORY_DIR derived in Step 1 — reuse that value
NOW=$(date +%s)
for f in "$MEMORY_DIR"/session-open-*.md; do
    [ -f "$f" ] || continue
    MTIME=$(if [ "$(uname -s)" = "Darwin" ]; then stat -f "%m" "$f"; else stat -c "%Y" "$f"; fi) # timeout: 5000
    AGE=$(((NOW - MTIME) / 86400))
    echo "$AGE $f"
done
```

### Step 4: Render grouped list

Group by age bucket:

- **This session** — files modified today (age = 0)
- **Earlier (`<date>`)** — files modified on prior dates, grouped by modification date

Apply `⚠ stale` prefix to items with age ≥ 14 days.

Print in this format:

```markdown
## Session Pending — <today's date>

### This session
- [ ] <item name> — <description>

### Earlier (<YYYY-MM-DD>)
- [ ] ⚠ stale — <item name> — <description>

→ /session archive <slug> to close an item
→ /session summary for a full session digest
```

If no files exist, print: `No pending session items.`

## Mode: archive (close a parked item)

### Step 1: Locate the memory directory and list candidates

Derive MEMORY_DIR using canonical snippet from `<constants>`. Use Glob tool with pattern `session-open-*.md` in MEMORY_DIR to list candidates.

### Step 2: Fuzzy-match the target item

Extract `<partial-text>` from `$ARGUMENTS` (everything after `archive `).

Use Grep with partial text against memory directory, pattern `session-open-*.md`. Also match against file basenames. Select best match — if ambiguous (2+ equally close matches), list them and ask user to disambiguate before proceeding.

Read matched file with Read tool to extract its `name` field.

### Step 3: Delete the memory file

Set `MATCHED_FILE` to the full path of the matched file from Step 2, then:

```bash
rm "$MATCHED_FILE"  # timeout: 5000
echo "deleted"
```

### Step 4: Append audit entry to resolution log

Ensure log directory exists:

```bash
mkdir -p .claude/logs # timeout: 5000
```

Append one-line JSON entry using the Edit tool: read `.claude/logs/session-archive.jsonl` (or create with Write if absent), append a new line: `{"ts":"<ISO8601-UTC>","item":"<name>","action":"archived"}`

### Step 5: Confirm to user

Print: `Archived: <item name>` — one line, terminal only.

## Mode: summary (session digest)

### Step 1: Collect completed tasks

Call TaskList (or use TaskCreate/TaskUpdate context) to get tasks with status `completed` from this session. Extract subject lines.

### Step 2: Collect parked items

Derive MEMORY_DIR using canonical snippet from `<constants>`. Use Glob tool with pattern `session-open-*.md` in MEMORY_DIR to list candidates. Read each matched file with Read tool for `name` and `description`.

### Step 3: Collect recent git commits

```bash
git log --oneline --since="$(date -u -d '8 hours ago' '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date -u -v-8H '+%Y-%m-%dT%H:%M:%SZ')" 2>/dev/null | head -20 # timeout: 3000
```

If date flag syntax fails, fall back to:

```bash
git log --oneline -15 # timeout: 3000
```

### Step 4: Collect archived items from this session

```bash
[ -f .claude/logs/session-archive.jsonl ] && tail -20 .claude/logs/session-archive.jsonl || echo "none" # timeout: 5000
```

Filter entries with `ts` matching today's date.

### Step 5: Compose and route the digest

Draft digest:

```markdown
## Session Summary — <date>

### Completed
- <task 1>
- <task 2>

### Parked / Pending (<N> items)
- [ ] <item> — <description>

### Archived this session
- <item> — <ts>

### Recent commits
- <hash> <message>
```

Output-routing rule: ≤ 10 lines → terminal only. If longer:

```bash
mkdir -p .temp/
OUTPUT=".temp/output-session-summary-$(date +%Y-%m-%d).md"
# Anti-overwrite: increment counter if slug already exists
if [ -f "$OUTPUT" ]; then
    n=2
    while [ -f "${OUTPUT%.md}-$n.md" ]; do n=$((n + 1)); done
    OUTPUT="${OUTPUT%.md}-$n.md"
fi
```

Write to `$OUTPUT`, print compact terminal summary with `→ file`.

</workflow>

<notes>

**Automatic parking behavior (core behavioral rule — no command needed)**

During any session, Claude proactively parks open-loop items to project-scoped memory as they arise:

| Item type | Trigger | Entry format |
| --- | --- | --- |
| Unanswered clarifying question | User sends a new top-level request before answering Claude's prior clarifying question | `"User raised: <idea>. Pending: <question asked>."` |
| Deferred exploration | "let's come back to that", "park this for later", idea mentioned but not pursued | `"Deferred: <idea>. Context: <one sentence why deferred>."` |
| Diverging idea mid-task | New feature/design idea mentioned while solving something else | `"Side idea: <idea>. Raised while: <what we were doing>."` |

**Topic-shift detection rule**: trigger strictly behavioural — user submits new top-level request without answering Claude's prior question (not follow-up or clarification). No semantic similarity scoring.

**File format**: each parked item = standard memory file:

```markdown
---
name: <short slug>
description: <one-line summary of the parked item>
type: project
---

<item text>

**Why:** <one sentence on why it was deferred or what triggered it>
**How to apply:** <what question to ask or action to take when revisiting>
```

Written to: `~/.claude/projects/<project-slug>/memory/session-open-<slug>-<YYYY-MM-DD>.md`

Derive project slug via: `git rev-parse --show-toplevel | sed 's|[/.]|-|g' | sed 's/^-//'`

**Memory pollution guard**: before parking new item, count existing `session-open-*.md` files. If count ≥ 10, surface full list and ask user to archive some before writing new one.

**TTL policy**: items ≥ 14 days listed with `⚠ stale`. Items ≥ 30 days deleted silently during `resume` and `SessionEnd` cleanup. TTL thresholds fixed global values — not configurable.

**Session-start behavior**: open-loop items NOT surfaced automatically at session start. Appear only when `/session resume` explicitly invoked. Do not add session-start hygiene step for this in CLAUDE.md.

**Resolution log**: `.claude/logs/session-archive.jsonl` is project-local, append-only. Stays in git-tracked project directory as audit trail; separate from home-scoped memory files intentionally.

**Scope**: parked ideas scoped to current project only — don't appear across projects. Memory isolation enforced by per-project slug directory under `~/.claude/projects/`.

</notes>
