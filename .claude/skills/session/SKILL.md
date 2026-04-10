---
name: session
description: Session parking lot — automatically parks diverging ideas and unanswered questions to project-scoped memory; /session resume shows pending items, /session archive closes them, /session summary gives a session digest
argument-hint: resume | archive <text> | summary
allowed-tools: Read, Write, Edit, Glob, Grep, TaskCreate, TaskUpdate, Bash
effort: low
model: sonnet
context: fork
---

<objective>

Track open-loop ideas, deferred questions, and diverging threads that arise during a session — without losing them to context compaction or session end. Provides three on-demand commands (`resume`, `archive`, `summary`) and a behavioral parking rule that writes `session-open-*.md` memory files automatically as items arise.

</objective>

<inputs>

- **$ARGUMENTS**: required. Three modes:
  - `resume` (alias: `pending`) — list all open `session-open-*.md` memory files for this project, grouped by age; items ≥ 14 days get `⚠ stale` prefix; items ≥ 30 days are silently deleted before listing
  - `archive <partial-text>` — fuzzy-match a parked item by name or content, delete its memory file, and append an audit entry to `.claude/logs/session-archive.jsonl`
  - `summary` — compact session digest: completed tasks, parked items, and recent git commits since session start; follows output-routing rule (≤10 lines → terminal; longer → `.temp/output-session-summary-<date>.md`)

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

- status `completed` if the work is clearly done
- status `deleted` if orphaned / no longer relevant
- keep `in_progress` only if genuinely continuing

## Mode: resume (list pending items)

### Step 1: Resolve the memory directory

Derive MEMORY_DIR using the canonical snippet from `<constants>`.

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

Use Glob with pattern `session-open-*.md` in the memory directory. For each file found, read it with the Read tool to extract the `name` and `description` frontmatter fields and the item body.

Compute age in days for each file:

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

Group items by age bucket:

- **This session** — files modified today (age = 0)
- **Earlier (`<date>`)** — files modified on prior dates, grouped by modification date

Apply `⚠ stale` prefix to items with age ≥ 14 days.

Print in this format:

```
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

Derive MEMORY_DIR using the canonical snippet from `<constants>`, then list candidates:

```bash
PROJECT="$(git rev-parse --show-toplevel)"
SLUG="$(echo "$PROJECT" | sed 's|[/.]|-|g' | sed 's|^-||')"
MEMORY_DIR="$HOME/.claude/projects/$SLUG/memory/"
ls "$MEMORY_DIR"/session-open-*.md 2>/dev/null || echo "none"
```

### Step 2: Fuzzy-match the target item

Extract `<partial-text>` from `$ARGUMENTS` (everything after `archive `).

Use Grep with the partial text against the memory directory, pattern `session-open-*.md`. Also match against file basenames. Select the best match — if ambiguous (2+ equally close matches), list them and ask the user to disambiguate before proceeding.

Read the matched file with the Read tool to extract its `name` field.

### Step 3: Delete the memory file

Use Bash to remove the matched file:

```example
rm "$MEMORY_DIR/session-open-<matched-slug>-<date>.md"
echo "deleted"
```

### Step 4: Append audit entry to resolution log

Ensure the log directory exists:

```bash
mkdir -p .claude/logs # timeout: 5000
```

Append a one-line JSON entry using Write/Edit — or Bash:

```bash
echo "{\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"item\":\"<name>\",\"action\":\"archived\"}" >>.claude/logs/session-archive.jsonl # timeout: 5000
```

### Step 5: Confirm to user

Print: `Archived: <item name>` — one line, terminal only.

## Mode: summary (session digest)

### Step 1: Collect completed tasks

Call TaskList (or use TaskCreate/TaskUpdate context) to get tasks with status `completed` from this session. Extract subject lines.

### Step 2: Collect parked items

Derive MEMORY_DIR using the canonical snippet from `<constants>`, then list parked files:

```bash
PROJECT="$(git rev-parse --show-toplevel)"
SLUG="$(echo "$PROJECT" | sed 's|[/.]|-|g' | sed 's|^-||')"
MEMORY_DIR="$HOME/.claude/projects/$SLUG/memory/"
ls "$MEMORY_DIR"/session-open-*.md 2>/dev/null || echo "none"
```

Read each file with the Read tool for its `name` and `description`.

### Step 3: Collect recent git commits

```bash
git log --oneline --since="$(date -u -d '8 hours ago' '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || date -u -v-8H '+%Y-%m-%dT%H:%M:%SZ')" 2>/dev/null | head -20 # timeout: 3000
```

If the date flag syntax fails on the current platform, fall back to:

```bash
git log --oneline -15 # timeout: 3000
```

### Step 4: Collect archived items from this session

```bash
[ -f .claude/logs/session-archive.jsonl ] && tail -20 .claude/logs/session-archive.jsonl || echo "none" # timeout: 5000
```

Filter entries with `ts` matching today's date.

### Step 5: Compose and route the digest

Draft the digest:

```
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

Apply output-routing rule: if the digest is ≤ 10 lines, print to terminal only. If longer:

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

Write to `$OUTPUT` and print a compact terminal summary with `→ file`.

</workflow>

<notes>

**Automatic parking behavior (core behavioral rule — no command needed)**

During any session, Claude proactively parks open-loop items to project-scoped memory as they arise:

| Item type                      | Trigger                                                                                | Entry format                                                |
| ------------------------------ | -------------------------------------------------------------------------------------- | ----------------------------------------------------------- |
| Unanswered clarifying question | User sends a new top-level request before answering Claude's prior clarifying question | `"User raised: <idea>. Pending: <question asked>."`         |
| Deferred exploration           | "let's come back to that", "park this for later", idea mentioned but not pursued       | `"Deferred: <idea>. Context: <one sentence why deferred>."` |
| Diverging idea mid-task        | New feature/design idea mentioned while solving something else                         | `"Side idea: <idea>. Raised while: <what we were doing>."`  |

**Topic-shift detection rule**: trigger is strictly behavioural — user submits a new top-level request without answering Claude's prior question (not a follow-up or clarification). No semantic similarity scoring.

**File format**: each parked item is a standard memory file:

```
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

Derive the project slug via: `git rev-parse --show-toplevel | sed 's|[/.]|-|g' | sed 's/^-//'`

**Memory pollution guard**: before parking a new item, count existing `session-open-*.md` files. If the count is ≥ 10, surface the full list and ask the user to archive some entries before writing a new one.

**TTL policy**: items ≥ 14 days listed with `⚠ stale`. Items ≥ 30 days deleted silently during `resume` and during `SessionEnd` cleanup. TTL thresholds are fixed global values — not configurable.

**Session-start behavior**: open-loop items are NOT surfaced automatically at session start. They appear only when `/session resume` is explicitly invoked. Do not add a session-start hygiene step for this in CLAUDE.md.

**Resolution log**: `.claude/logs/session-archive.jsonl` is project-local and append-only. It stays in the git-tracked project directory as an audit trail; it is separate from the home-scoped memory files intentionally.

**Scope**: parked ideas are scoped to the current project only — they do not appear across projects. Memory isolation is enforced by the per-project slug directory under `~/.claude/projects/`.

</notes>
