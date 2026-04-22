---
name: integration
description: Manage codemap integration — 'check' audits installation health (scan-query reachable, index fresh, injection present), 'init' onboards codemap by discovering skills/agents, recommending injection sites, and wiring them in.
argument-hint: check | init [--approve]
effort: medium
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, AskUserQuestion
model: sonnet
---

<objective>

Two modes, run sequentially: `init` to set up, `check` to verify.

- **`check`** — fast diagnostic: finds `scan-query`, verifies index exists and fresh, runs smoke test, audits which skill files have injection block. Prints `✓`/`✗`/`⚠` per check with one-line remediation hints. Pure bash — no model reasoning needed for happy path.
- **`init`** — interactive onboarding: builds index if missing, discovers all installed skills and agents, scores by how much codemap would help, presents recommendation table, asks which to wire in, inserts correct injection block into each selected file.

NOT for: building or rebuilding index (use `/codemap:scan`); running structural query (use `/codemap:query`).

</objective>

<inputs>

- **`check`** — audit current installation. No other arguments.
- **`init`** — onboard codemap to this project.
  - **`--approve`** — non-interactive; auto-apply all starred (★) recommendations without prompting.

</inputs>

<workflow>

## Mode detection

Parse `$ARGUMENTS` (case-insensitive):

- Starts with `check` or empty → run **check mode** (Steps C1–C5)
- Starts with `init` → run **init mode** (Steps I0–I6 (I5 has sub-steps I5a, I5b))
- Anything else → print: `Usage: /codemap:integration check | init [--approve]` and stop.

______________________________________________________________________

## CHECK MODE (Steps C1–C5)

### C1 — Locate scan-query

Three-tier fallback: PATH → plugin root → cache glob.

```bash
# timeout: 5000
GRN='\033[0;32m'; RED='\033[1;31m'; YEL='\033[1;33m'; NC='\033[0m'
if command -v scan-query >/dev/null 2>&1; then
    SQ=$(command -v scan-query); SRC="PATH"
elif [ -x "${CLAUDE_PLUGIN_ROOT}/bin/scan-query" ]; then
    SQ="${CLAUDE_PLUGIN_ROOT}/bin/scan-query"; SRC="CLAUDE_PLUGIN_ROOT"
else
    SQ=$(ls "$HOME/.claude/plugins/cache"/*/codemap/*/bin/scan-query 2>/dev/null | sort -V | tail -1)
    SRC="cache glob"
fi
if [ -n "$SQ" ] && [ -x "$SQ" ]; then
    printf "${GRN}✓${NC} scan-query: %s (via %s)\n" "$SQ" "$SRC"
else
    printf "${RED}✗${NC} scan-query: not found\n"
    printf "  → Install: claude plugin install codemap@borda-ai-rig\n"
    exit 1
fi
```

### C2 — PROJ and index existence

```bash
# timeout: 5000
GIT_ROOT=$(git rev-parse --show-toplevel 2>/dev/null)
PROJ=${GIT_ROOT:+$(basename "$GIT_ROOT")}; PROJ=${PROJ:-$(basename "$PWD")}
INDEX=".cache/scan/${PROJ}.json"
printf "  project: %s\n  index:   %s\n" "$PROJ" "$INDEX"
if [ -f "$INDEX" ]; then
    printf "${GRN}✓${NC} index: exists\n"
else
    printf "${RED}✗${NC} index: not found\n"
    printf "  → Run /codemap:scan to build the index\n"
    exit 1
fi
```

### C3 — Index freshness (calendar age)

```bash
# timeout: 10000
python3 -c "
import json, sys
from datetime import datetime, timezone
d = json.load(open('$INDEX'))
sa = d.get('scanned_at', '')
if not sa:
    print('WARN|scanned_at missing — index may be corrupted|Re-run /codemap:scan')
    sys.exit()
age = (datetime.now(timezone.utc) - datetime.fromisoformat(sa)).days
s = 'WARN' if age > 7 else 'OK'
print(f'{s}|{age} day{\"s\" if age != 1 else \"\"} ago ({sa[:10]})|Run /codemap:scan to refresh')
" | while IFS='|' read s d h; do
    case $s in
        OK)   printf "${GRN}✓${NC} freshness: %s\n" "$d" ;;
        WARN) printf "${YEL}⚠${NC} freshness: %s\n  → %s\n" "$d" "$h" ;;
    esac
done
PYTHON3_EXIT=${PIPESTATUS[0]}
[ "$PYTHON3_EXIT" -ne 0 ] && printf "${YEL}⚠${NC} freshness: python3 exited %s — index may be unreadable\n" "$PYTHON3_EXIT"
```

### C4 — Smoke test and git-staleness check

```bash
# timeout: 15000
OUT=$("$SQ" central --top 3 2>/tmp/cmc_err); RC=$?
if [ $RC -ne 0 ]; then
    printf "${RED}✗${NC} smoke test: exit %s\n" "$RC"
    [ -s /tmp/cmc_err ] && printf "  stderr: %s\n" "$(cat /tmp/cmc_err)"
    printf "  → Check index with: %s list\n" "$SQ"
else
    STALE=$(python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print(d.get('index',{}).get('stale','?'))" <<< "$OUT" 2>/dev/null)
    printf "${GRN}✓${NC} smoke test: central query OK (git-stale=%s)\n" "$STALE"
    if [ "$STALE" = "True" ]; then
        printf "  ${YEL}⚠${NC} Python files changed since scan — run /codemap:scan to update\n"
    fi
fi
rm -f /tmp/cmc_err
```

### C5 — Skill injection audit

```bash
# timeout: 20000
[ -z "$CLAUDE_PLUGIN_ROOT" ] && { printf "${RED}✗${NC} CLAUDE_PLUGIN_ROOT unset — cannot audit injection\n"; exit 1; }
CACHE=$(dirname "$(dirname "$CLAUDE_PLUGIN_ROOT")")
printf "\n--- Skill injection audit (cache: %s) ---\n" "$CACHE"
FILES=$(find "$CACHE" -name "SKILL.md" -exec grep -l "command -v scan-query" {} \; 2>/dev/null | sort)
COUNT=$(echo "$FILES" | grep -c . 2>/dev/null || echo 0)
printf "${GRN}✓${NC} %s SKILL.md file(s) have the injection block:\n" "$COUNT"
echo "$FILES" | while read -r f; do
    [ -n "$f" ] && printf "  • %s\n" "${f#$CACHE/}"
done
# keep this list in sync with develop and oss plugin skill directories
for exp in "develop/*/skills/fix" "develop/*/skills/feature" "develop/*/skills/refactor" "develop/*/skills/plan" "develop/*/skills/review" "develop/*/skills/debug" "oss/*/skills/review" "oss/*/skills/resolve" "oss/*/skills/analyse"; do
    echo "$FILES" | grep -q "$exp" \
        || printf "  ${YEL}⚠${NC} missing injection in: %s/SKILL.md\n" "$exp"
done
printf "\n--- check complete ---\n"
printf "If any check failed:\n"
printf "  • /codemap:scan    — build or refresh the index\n"
printf "  • /codemap:integration init — add injection to more skills/agents\n"
printf "  • /codemap:integration check — re-run after fixes\n"
```

______________________________________________________________________

## INIT MODE (Steps I0–I6)

### I0 — Detect --approve

If `--approve` is present in `$ARGUMENTS` (case-insensitive), skip all `AskUserQuestion` calls in this workflow and auto-select the ★ option for every prompt. Print `[--approve] applying recommended options` in place of each question. This is a reasoning instruction — do not set a bash variable.

### I1 — Verify or build the index

```bash
# timeout: 5000
GIT_ROOT=$(git rev-parse --show-toplevel 2>/dev/null)
PROJ=${GIT_ROOT:+$(basename "$GIT_ROOT")}; PROJ=${PROJ:-$(basename "$PWD")}
INDEX=".cache/scan/${PROJ}.json"
```

Index exists: report and proceed. Index missing:

Use `AskUserQuestion` to present (if `--approve` was detected in I0: auto-select a; otherwise ask):

```text
No codemap index found for project: $PROJ

a) Build now ★ — scans all .py files via ast.parse (Python only), <60s on most projects
b) Skip — I'll run /codemap:scan later (recommendations will be generic, no module-count weighting)
```

If **a** (or auto-approved): run scanner — verify binary exists first:

```bash
# timeout: 5000
[ -x "${CLAUDE_PLUGIN_ROOT}/bin/scan-index" ] || { printf "${RED}✗${NC} scan-index not found at ${CLAUDE_PLUGIN_ROOT}/bin/scan-index\nTry: /codemap:scan to install and rebuild.\n"; exit 1; }
# timeout: 360000
${CLAUDE_PLUGIN_ROOT}/bin/scan-index
```

Report result (module count, degraded count). If **b**: note "Proceeding without index — recommendations based on skill purpose only, not module count."

### I2 — Discover installed skills and agents

Read `~/.claude/plugins/installed_plugins.json` to find all installed plugins. For each plugin's `installPath`, glob for:

- `skills/*/SKILL.md` — skill files
- `agents/*.md` — agent files

For each file: extract from frontmatter: `name`, `description`, `allowed-tools` (skills) or `description` body (agents). Extract first sentence of `<objective>` section.

Flag which files already have injection block:

```bash
# timeout: 10000
find "$CACHE" -name "SKILL.md" -exec grep -l "command -v scan-query" {} \; 2>/dev/null
```

Build two lists: `ALREADY_INJECTED` and `CANDIDATES` (not yet injected).

### I3 — Score and rank candidates

For each candidate skill/agent, classify by value tier using `<objective>` text and `allowed-tools`:

| Tier | Signal | Recommendation |
| --- | --- | --- |
| **High** | `allowed-tools` includes `Edit` or `Write`; `<objective>` mentions spawning `foundry:sw-engineer` or `foundry:qa-specialist`; performs code changes | "Strongly recommend — agent starts with blast-radius context" |
| **Medium** | analysis or planning skills; spawns read-only agents; multi-file review without edits | "Moderate value — centrality context speeds structural decisions" |
| **Low** | documentation, release, communication; no code traversal | "Low value — structural context unlikely to help" |
| **Skip** | config-only, single-file, non-Python purpose (e.g. shell, YAML, JS) | "Skip — not applicable for Python import graphs" |

If index built and `total_modules < 20`: downgrade all tiers one level (small project = less value from structural context).

### I4 — Present recommendations and ask user

Print candidate table:

```text
Codemap injection candidates for: $PROJ

  Status  Skill/Agent          Tier    Notes
  ──────────────────────────────────────────────────────────────────
  a)      develop:refactor     MEDIUM  restructures code; reads module deps for target
  b)      oss:ci-guardian      MEDIUM  diagnoses failures; reads code structure for context
  —       foundry:doc-scribe   LOW     writes docstrings; skip
  —       oss:release          SKIP    release artifact; no code traversal
```

Use `AskUserQuestion` to ask (if `--approve` was detected in I0: auto-select all HIGH+MEDIUM; otherwise ask):

```text
Which skills/agents should I add codemap injection to?

Reply with letters (e.g. "a b"), "all" (all High+Medium), or "none".
```

### I5 — Wire in the injection block

For each selected file, determine insertion point and content:

**For SKILL.md files** — find step that first spawns agent. Insert hardened soft-check block immediately before it, blank line before and after:

```bash
# Structural context (codemap — Python projects only, silent skip if absent)
# TARGET_MODULE — derive from $ARGUMENTS (e.g. strip leading ./ and .py suffix from file path argument)
PROJ=$(basename "$(git rev-parse --show-toplevel 2>/dev/null)" 2>/dev/null) || PROJ=$(basename "$PWD")
if command -v scan-query >/dev/null 2>&1 && [ -f ".cache/scan/${PROJ}.json" ]; then
    scan-query central --top 3  # timeout: 5000
fi
# If results returned: prepend a ## Structural Context (codemap) block to the agent spawn prompt.
# Also add: "For targeted analysis run: scan-query rdeps <module> or scan-query fn-blast module::function"
```

For skills where target module can be derived from `$ARGUMENTS` (refactor, fix with module path, review), also add after `central`:

```bash
scan-query rdeps "$TARGET_MODULE" 2>/dev/null  # timeout: 5000
scan-query deps  "$TARGET_MODULE" 2>/dev/null  # timeout: 5000
```

**For agent `.md` files** — append to last workflow instruction paragraph, before closing section or final notes:

```markdown
**Structural context (codemap — Python projects only)**: if `.cache/scan/<project>.json` exists, run `scan-query central --top 5` (and `scan-query rdeps <target_module>` when a target is known) **before** any Glob/Grep exploration for structural information. Skip silently if the index is absent.
```

Report each edit: `✓ injected: <plugin>/<skill-or-agent> at line N`

### I5a — Offer git post-commit hook

Use `AskUserQuestion` to present option (if `--approve` was detected in I0: auto-select a; otherwise ask):

```text
Install post-commit git hook for automatic incremental rebuild?

a) Install ★ — runs scan-index --incremental in background after every commit; index stays current with zero developer action
b) Skip — I'll run /codemap:scan or /codemap:scan --incremental manually
```

### I5b — Write hook file

If **a** (or auto-approved): write `.git/hooks/post-commit`. Idempotent — check for `# codemap: incremental` marker before writing:

- Marker absent and file exists: append the block
- File does not exist: create with `#!/bin/sh` header, make executable with `chmod +x`

Hook content to write or append:

```bash
# codemap: incremental index rebuild — do not remove this line
if command -v scan-index >/dev/null 2>&1; then
    scan-index --incremental 2>/dev/null &
fi
```

Report: `✓ post-commit hook installed: .git/hooks/post-commit` or `✓ already installed` if marker was already present.

### I6 — Summary report

Print:

```text
--- init complete ---

Injected codemap into N skill(s)/agent(s):
  ✓ research:plan    → <path>
  ✓ ...

Already integrated (no change):
  • develop:fix, develop:feature, ...

Skipped:
  • foundry:doc-scribe — LOW value
  • oss:release — SKIP

Post-commit hook: installed / skipped

Next: run /codemap:integration check to verify all injection blocks are wired correctly.
```

</workflow>
