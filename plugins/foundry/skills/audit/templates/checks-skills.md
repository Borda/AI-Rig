# Skill Checks — 21, 22, 23, 24, 27, 28

## Check 21 — Skill frontmatter conflicts

`context:fork + disable-model-invocation:true` is a broken combination.

```bash
RED='\033[1;31m'
YEL='\033[1;33m'
GRN='\033[0;32m'
CYN='\033[0;36m'
NC='\033[0m'
for f in .claude/skills/*/SKILL.md; do # timeout: 5000
    name=$(basename "$(dirname "$f")")
    if awk '/^---$/{c++} c<2' "$f" 2>/dev/null | grep -q 'context: fork' &&
    awk '/^---$/{c++} c<2' "$f" 2>/dev/null | grep -q 'disable-model-invocation: true'; then
        printf "${RED}! BREAKING${NC} skills/%s: context:fork + disable-model-invocation:true\n" "$name"
        printf "  ${RED}→${NC} forked skill has no model to coordinate agents or synthesize results\n"
        printf "  ${CYN}fix${NC}: remove disable-model-invocation:true (or remove context:fork if purely tool-only)\n"
    fi
done
```

## Check 22 — Calibration coverage gap

**Step 1 — Read the calibrate domain table**: Read `.claude/skills/calibrate/modes/skills.md` and extract the registered target list under `### Domain table`. Build the set of registered targets.

**Step 2 — Scan all skill modes on disk**: Use Glob (`skills/*/SKILL.md`, path `.claude/`) and Glob (`skills/*/modes/*.md`, path `.claude/`) to enumerate every skill and mode file. Extract mode names from `argument-hint:` frontmatter and `## Mode:` / `### Mode:` headings.

**Step 3 — Validate registered targets exist on disk**: For each registered target, verify the corresponding skill/mode file exists. A registered target with no matching file → **medium** (calibrate will fail at runtime).

**Step 4 — Identify unregistered calibratable candidates** (model reasoning):

A mode is calibratable when ALL three signals are present:

1. **Deterministic structured output**: findings list, completeness checklist, structured table, or machine-readable verdict
2. **Synthetic input feasible**: can be tested without external services
3. **Ground truth constructable**: known issues can be injected and scored

→ Unregistered mode matching all three signals: **low** (add to `calibrate/modes/skills.md` domain table)

**Step 5 — Read the agents domain table**: Read `.claude/skills/calibrate/modes/agents.md` and extract all agent names from the `### Domain table` section. Build the set of registered agent names.

**Step 6 — Scan all agent files on disk**: Use Glob (`plugins/*/agents/*.md`, path project root) to enumerate plugin agent files; also Glob (`agents/*.md`, path `.claude/`) for any directly installed agents. Derive a qualified name for each: `plugins/<plugin>/agents/<name>.md` → `<plugin>:<name>`; `.claude/agents/<name>.md` → `<name>`. Build the full discovered-agent set.

**Step 7 — Validate registered agents exist on disk**: For each registered agent in the domain table, verify it resolves to a discovered file. A bare name in the domain table (e.g. `sw-engineer`) matches `foundry:sw-engineer` when no `.claude/agents/sw-engineer.md` exists — apply model reasoning to resolve bare names against plugin-qualified discoveries. Registered agent with no matching file → **medium** (stale entry will cause calibrate to fail at runtime; remove from domain table or correct the prefix).

**Step 8 — Identify unregistered agents**: For each discovered agent not represented in the domain table, apply the same three-signal calibratability test from Step 4. → Unregistered calibratable agent: **low** (add to `calibrate/modes/agents.md` domain table with an appropriate domain string).

## Check 23 — Bash command misuse / native tool substitution

```bash
YEL='\033[1;33m'
GRN='\033[0;32m'
CYN='\033[0;36m'
NC='\033[0m'
printf "=== Check 23: Bash misuse candidates ===\n"
grep -rn '\bcat \|`cat ' .claude/agents/ .claude/skills/ .claude/rules/ 2>/dev/null |
grep -v '^Binary' | grep -v '# ' &&
printf "  ${CYN}hint${NC}: replace cat with Read tool\n" || true
grep -rn '\bgrep \|\brg \b' .claude/agents/ .claude/skills/ .claude/rules/ 2>/dev/null |
grep -v '^Binary' | grep -v '# .*grep\|Grep tool\|Use Grep' &&
printf "  ${CYN}hint${NC}: replace grep/rg with Grep tool\n" || true
grep -rn '\bfind \b.*-name\|\bls \b.*\*' .claude/agents/ .claude/skills/ .claude/rules/ 2>/dev/null |
grep -v '^Binary' | grep -v '# .*Glob\|Use Glob\|Glob tool' &&
printf "  ${CYN}hint${NC}: replace find/ls with Glob tool\n" || true
grep -rn 'echo .* >\|tee ' .claude/agents/ .claude/skills/ .claude/rules/ 2>/dev/null |
grep -v '^Binary' | grep -v '# .*Write tool\|Use Write' &&
printf "  ${CYN}hint${NC}: replace echo-redirect/tee with Write tool\n" || true
grep -rn '\bsed \b\|\bawk \b' .claude/agents/ .claude/skills/ .claude/rules/ 2>/dev/null |
grep -v '^Binary' | grep -v '# .*Edit tool\|Use Edit\|awk.*{print\|awk.*BEGIN' &&
printf "  ${CYN}hint${NC}: replace sed/awk text-substitution with Edit tool\n" || true
printf "${GRN}✓${NC}: Check 23 scan complete\n"
```

After the scan, apply model reasoning to each match — exclude cases where the shell command is genuinely necessary. Flag only where the native tool is a direct drop-in replacement.

| Shell command | Preferred native tool | Severity |
| --- | --- | --- |
| `cat <file>` | Read tool | medium |
| `grep`/`rg` for content search | Grep tool | medium |
| `find`/`ls` for file listing | Glob tool | medium |
| `echo … >` / `tee` to write a file | Write tool | medium |
| `sed`/`awk` for text substitution | Edit tool | medium |

**Report only** — never auto-fix; some Bash invocations in example/illustration code blocks are intentional.

## Check 24 — Skill sequence compatibility

Skill `<notes>` and `<workflow>` sections frequently document multi-skill chains (e.g., `→ /audit fix`, `suggested next: /brainstorm breakdown <file>`). This check verifies that documented sequences are internally consistent:

- **24a (target existence)**: every skill referenced in a documented chain exists on disk — root skills under `.claude/skills/<name>/`, plugin skills under `plugins/<plugin>/skills/<skill>/`
- **24b (argument plausibility)**: when a suggestion includes an explicit argument (e.g., `→ /audit fix`), that argument must appear as a substring in the target skill's `argument-hint:` frontmatter (case-insensitive)

**Step 1 — Extract sequence references**:

Scan three sources for documented chains:

1. **Skill files**: Grep (pattern `→.*` + backtick + `/[a-z]|suggest.*` + backtick + `/[a-z]|run.*after.*` + backtick + `/[a-z]`, glob `skills/*/SKILL.md`, path `.claude/`, output mode `content`)
2. **Agent files**: same Grep on `agents/*.md` (path `.claude/`)
3. **README files**: Grep the same pattern in `README.md` (project root), `plugins/*/README.md`, and `.claude/README.md` — README sequence tables are canonical documentation of the intended workflow chains and must be consistent with what is actually installed

Filter out:

- Lines starting with `#` (comments)
- Lines containing `e.g.` or `for example` (illustrative, not directive)
- Lines whose surrounding context is a description of what the skill does rather than a "run next" directive

Collect all unique (source-file, skill-reference, trailing-argument) triples. README-sourced sequences are held to the same validity standard as skill-sourced ones: a broken sequence in a README is a **high** finding because it is the user-facing documentation of the workflow.

**Step 2 — Resolve each reference (Check 24a)**:

| Reference form | Resolution |
| --- | --- |
| `/name` | Glob `.claude/skills/name/SKILL.md` — must exist |
| `/plugin:name` | Glob `plugins/plugin/skills/name/SKILL.md` — must exist; if no `plugins/` dir, note "installed plugin — cannot verify statically" and skip |

Missing target → **[high]**: `Sequence reference /<name> in <file> resolves to no installed skill`

**Step 3 — Argument plausibility (Check 24b)**:

For references with a trailing argument token (e.g., `fix` in `/audit fix`, `breakdown` in `/brainstorm breakdown`):

1. Read the target skill's frontmatter `argument-hint:` (Glob-resolved path, first 5 lines)
2. If the argument token does NOT appear as a case-insensitive substring of `argument-hint` → **[medium]**: `Sequence argument '<arg>' absent from /<name> argument-hint: '<hint>'`

**Step 4 — Cycle detection (Check 24c)**:

Build a directed graph from (source-file, skill-reference) pairs collected in Step 1. Walk all paths from each node; flag back-edges (skill A → skill B → … → skill A).

→ Any cycle found: **[high] 24c**: `Cycle: <A> → <B> → … → <A>` — document full cycle path; do not auto-fix; resolution requires removing or redirecting one chain edge.

**Report only** — do not auto-fix; sequence intent requires human judgment.

| Sub-check | Severity | Auto-fix |
| --- | --- | --- |
| 24a — target skill not on disk | high | no |
| 24b — argument absent from argument-hint | medium | no |
| 24c — directed cycle in follow-up chain | high | no |

## Check 27 — Cross-plugin shared-file reference integrity

Plugin SKILL.md files (non-foundry plugins) must not contain `Read` calls or inline references to `.claude/skills/_shared/<file>` unless that exact file ships inside `plugins/foundry/skills/_shared/`. That path is only available at runtime via the `foundry:init` symlink — any file absent from foundry's own `_shared/` is a broken reference when foundry is installed, and entirely unreachable when foundry is not installed.

**Special antipattern — foundry-dependency catch-22**: when the referenced file's purpose is to describe fallback behaviour for users without foundry (e.g. an `agent-resolution.md` listing `general-purpose` substitutes), the reference is **critical** — the file that explains how to work without foundry is only accessible via foundry.

**Step 1 — Collect cross-plugin shared-file references**:

```bash
# Find all Read/include refs to .claude/skills/_shared/ in plugin SKILL.md files  # timeout: 5000
grep -rn '\.claude/skills/_shared/' plugins/*/skills/ 2>/dev/null | grep -v 'foundry'
```

For each match: record `(plugin, skill-file, referenced-filename)`.

**Step 2 — Verify file exists in foundry's \_shared/**:

```bash
ls plugins/foundry/skills/_shared/ 2>/dev/null  # timeout: 5000
```

For each referenced filename from Step 1: check if it appears in the foundry `_shared/` listing.

- Present → reference is valid at runtime (when foundry is installed) — **no finding**
- Absent → **[high] 27a**: `<plugin>/<skill>: references .claude/skills/_shared/<file> which is absent from foundry/_shared/ — broken at all times`

**Step 3 — Catch-22 upgrade**:

For each file flagged in Step 2 (absent from foundry `_shared/`): inspect the referenced filename and any surrounding context for signals that it provides fallback/degraded-mode behaviour (keywords: `fallback`, `without foundry`, `agent-resolution`, `general-purpose`, `not installed`).

- Match → upgrade to **[critical] 27b**: `<plugin>/<skill>: fallback file <name> is only reachable via foundry — catch-22`
- No match → keep as **[high] 27a**

**Step 4 — Plugin-local \_shared/ unmounted files**:

```bash
ls plugins/*/skills/_shared/ 2>/dev/null  # timeout: 5000
```

Plugin-local `_shared/` directories (e.g. `plugins/develop/skills/_shared/`) have **no install-time mount point** — they are invisible to the model at runtime. Any file there that a SKILL.md references is unreachable.

```bash
# For each plugin-local _shared/ file, check if any SKILL.md in that plugin references it  # timeout: 5000
for f in plugins/*/skills/_shared/*; do
    plugin=$(echo "$f" | cut -d/ -f2)
    fname=$(basename "$f")
    grep -rl "$fname" "plugins/$plugin/skills/" 2>/dev/null | grep 'SKILL\.md'
done
```

- Referenced and in plugin-local `_shared/` → **[medium] 27c**: `<plugin>/<skill>: references <file> from plugin-local _shared/ which is not mounted at runtime — move to foundry/_shared/ or inline`
- Exists in plugin-local `_shared/` but not referenced → **[low]**: unreachable dead file; suggest removal

**Report only** — do not auto-fix; resolution requires deciding whether to inline content or move the file to `foundry/_shared/`.

| Sub-check | Severity | Auto-fix |
| --- | --- | --- |
| 27a — file absent from foundry's \_shared/ | high | no |
| 27b — catch-22 (fallback file needs foundry to reach) | critical | no |
| 27c — plugin-local \_shared/ file referenced but not mounted | medium | no |

## Check 28 — Cross-plugin agent dispatch fallback

Skills dispatching agents via `Agent(subagent_type="<plugin>:<name>", ...)` depend on that plugin being installed. When the dispatched agent belongs to a different plugin from the skill's own plugin, and no fallback is declared for the case where that plugin is absent, the skill fails at runtime.

**Exempt from this check**: `general-purpose` (built-in, always available); `codex:*` agents (conditional dispatch already tracked by Check 7).

**Step 1 — Map skills to their owning plugin:**

```bash
# Map each plugin skill file to its owning plugin  # timeout: 5000
for f in plugins/*/skills/*/SKILL.md; do
    plugin=$(echo "$f" | cut -d/ -f2)
    skill=$(echo "$f" | cut -d/ -f4)
    echo "$plugin $skill $f"
done
```

**Step 2 — Collect cross-plugin dispatches per skill:**

```bash
# Find all subagent_type values across plugin skill files  # timeout: 5000
grep -rn 'subagent_type' plugins/*/skills/*/SKILL.md 2>/dev/null | grep -v '^Binary'
```

For each match: extract `(skill_file, dispatched_plugin, dispatched_agent)`. A dispatch is **cross-plugin** when `dispatched_plugin ≠ owning_plugin`. Build a map: `skill_file → [cross-plugin agents]`.

Skip: any `general-purpose` dispatch and any `codex:*` dispatch.

**Step 3 — Verify fallback coverage:**

For each skill with one or more cross-plugin dispatches, read the skill file and search for a fallback declaration. A valid fallback is any of:

- A section heading matching `Agent Resolution`, `Fallback`, or `Plugin Check` (case-insensitive)
- A sentence containing the cross-plugin agent name AND a word from `{fallback, not installed, substitute, general-purpose, unavailable}` within 5 lines of each other
- A conditional dispatch block: `if not installed` or `plugin list.*grep.*<plugin>` followed by an alternative

No fallback found → **[high] 28a**: `<plugin>/<skill>: dispatches <cross-plugin-agent> with no fallback for missing plugin`

**Step 4 — Completeness check:**

For each skill where a fallback section exists: verify every cross-plugin agent dispatched by that skill is named within the fallback block (bare name OR fully-qualified `plugin:name` form). An agent is covered when its name appears in the fallback block.

Partially covered → **[medium] 28b**: `<plugin>/<skill>: fallback section present but does not cover <agent>`

**Report only** — fixing requires adding an Agent Resolution section with fallback substitutes for each cross-plugin dependency; the pattern in `develop:plan` (Agent Resolution table with `foundry agent | Fallback | Model | Role description prefix`) is the reference implementation.

> **Related**: Check 25 (in `checks-shared.md`) covers bare-name dispatch (missing plugin prefix). Check 25 and Check 28 address different failure modes — run both.

| Sub-check | Condition | Severity | Auto-fix |
| --- | --- | --- | --- |
| 28a — no fallback for cross-plugin dispatch | high | no |
| 28b — fallback present but agent not covered | medium | no |

## Check 30 — Plugin skill bash operational correctness

Four static-grep patterns catching silent failures in skill SKILL.md bash blocks. Run across both `.claude/skills/` and `plugins/*/skills/` — these bugs appear in any skill.

### 30a — Pipe exit code capture (PIPESTATUS)

```bash
YEL='\033[1;33m'
GRN='\033[0;32m'
CYN='\033[0;36m'
NC='\033[0m'
printf "=== Check 30a: Pipe exit code capture ===\n"
# Find | tail or | head followed by $? assignment within 3 lines — tail/head always exit 0
grep -rn '| tail\b\|| head\b' plugins/*/skills/ .claude/skills/ 2>/dev/null |
  grep -v 'PIPESTATUS\|pipefail\|#.*tail\|#.*head' |
  grep -v '^Binary' &&
printf "  ${CYN}hint${NC}: use \${PIPESTATUS[0]} or set -o pipefail; \$? captures tail/head exit (always 0)\n" || true
printf "${GRN}✓${NC}: Check 30a scan complete\n"
```  # timeout: 5000

Severity: **critical** — gate commands appear to pass even on genuine failure; `$?` after `cmd | tail -N` is tail's exit code (0), not cmd's.

Fix pattern: `cmd 2>&1 | tail -N; EXIT=${PIPESTATUS[0]}`

### 30b — SKIP variable guard missing

```bash
printf "=== Check 30b: SKIP variable guard ===\n"
# Find SKIP_X=1 detection lines; check whether subsequent runner commands have a guard
grep -rn 'SKIP_[A-Z_]*=1' plugins/*/skills/ .claude/skills/ 2>/dev/null |
  grep -v '^Binary' | grep -v '#' | while IFS= read -r match; do
    file=$(echo "$match" | cut -d: -f1)
    # Check if any guard exists in same file
    grep -q '\[ "\${SKIP_' "$file" 2>/dev/null ||
      printf "${YEL}⚠ SKIP guard missing${NC}: %s — SKIP variable set but no conditional guard found\n" "$file"
done
printf "${GRN}✓${NC}: Check 30b scan complete\n"
```  # timeout: 5000

Severity: **critical** — `SKIP_RUFF=1` set by tool detection, but `$RUNNER ruff check` runs unconditionally; detection is cosmetic.

Fix pattern: `[ "${SKIP_RUFF:-0}" -ne 1 ] && $RUNNER ruff check ...`

### 30c — Agent filename convention mismatch (model reasoning)

Cannot be caught by grep alone — requires reading spawn prompt and consolidator read pattern in the same file.

Flag when a skill file:
1. Spawns agents with a prompt instructing them to write findings to a file named with a plugin-prefixed format (e.g. `foundry:sw-engineer.md`)
2. AND the consolidator reads files using a bare-name format (e.g. `sw-engineer.md`)

These never match → all agent findings silently dropped.

Severity: **high**

Fix: standardize to bare agent name in both spawn prompt and consolidator read pattern (e.g. `sw-engineer.md`).

### 30d — TEST_CMD used with pytest-specific flags without PYTEST_CMD split

```bash
printf "=== Check 30d: TEST_CMD/PYTEST_CMD split ===\n"
grep -rn '\$TEST_CMD.*--tb\b\|\$TEST_CMD.*--co\b\|\$TEST_CMD.*::\|\$TEST_CMD.*--cov\b\|\$TEST_CMD.*--doctest' \
  plugins/*/skills/ .claude/skills/ 2>/dev/null |
  grep -v 'PYTEST_CMD\|#' | grep -v '^Binary' &&
printf "  ${CYN}hint${NC}: derive PYTEST_CMD for pytest-specific flags; TEST_CMD=tox or make won't accept --tb/--co/::/--cov\n" || true
printf "${GRN}✓${NC}: Check 30d scan complete\n"
```  # timeout: 5000

Severity: **high** — skill fails silently on tox/make projects when pytest-specific flags appended to TEST_CMD.

Fix: after detecting TEST_CMD, derive `PYTEST_CMD` for targeted runs: `tox` → `PYTEST_CMD="uv run pytest"`; `make test` → `PYTEST_CMD="uv run pytest"`.

**Report only** — do not auto-fix; resolution requires understanding each skill's runner detection block.

| Sub-check | Pattern | Severity | Auto-fix |
| --- | --- | --- | --- |
| 30a — pipe exit code | `\ | tail` / `\ | head` without PIPESTATUS | critical | no |
| 30b — SKIP guard missing | `SKIP_X=1` with no `[ "${SKIP_X:-0}" ]` guard | critical | no |
| 30c — filename mismatch | spawn filename ≠ consolidator filename (model reasoning) | high | no |
| 30d — TEST_CMD+pytest flags | `$TEST_CMD --tb` / `--co` / `::` / `--cov` without PYTEST_CMD | high | no |
