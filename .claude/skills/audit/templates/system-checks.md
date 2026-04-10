# System-Wide Check Instructions

Full implementation details for all 21 checks in `/audit` Step 4. Read this file at the start of Step 4 before executing any check. Each section contains the bash script and reasoning instructions for the corresponding check number.

______________________________________________________________________

## Check 1 — Inventory drift (MEMORY.md vs disk)

Use Glob (`agents/*.md`, path `.claude/`) to list agent files; extract basenames and sort, then write to `/tmp/agents_disk.txt` via Bash:

```bash
ls .claude/agents/*.md 2>/dev/null | xargs -n1 basename 2>/dev/null | sed 's/\.md$//' | sort >/tmp/agents_disk.txt || true # timeout: 5000
```

Read the `- Agents:` and `- Skills:` roster lines from the MEMORY.md content injected in the conversation context (available as auto-memory at session start). Do not attempt to Grep a file path — the MEMORY.md is not stored under `.claude/` but in Claude Code's auto-memory system. Repeat with Glob (`skills/*/`, path `.claude/`) for skills on disk — write to `/tmp/skills_disk.txt`.

**macOS caution**: BSD grep treats arguments starting with `-` as option flags. If constructing a bash comparison from the MEMORY.md roster via grep, always use `grep -E 'Agents:'` (no leading `- `) or `grep -- '- Agents:'` rather than `grep '- Agents:'` — the latter exits 2 on macOS and silently produces an empty result. The safest approach is to use the Read tool (not grep) for MEMORY.md as the instruction above states.

______________________________________________________________________

## Check 2 — README vs disk

Use Grep tool (pattern `^\| \*\*`, file `README.md`, output mode `content`) to extract agent/skill table rows.

______________________________________________________________________

## Check 3 — settings.json permissions

Use Grep tool (pattern `gh |python -m|ruff|mypy|pytest`, glob `skills/*/SKILL.md`, path `.claude/`, output mode `content`) to collect bash commands used in skills.

______________________________________________________________________

## Check 4 — Orphaned follow-up references

Use Grep tool (pattern `` `/[a-z-]*` ``, glob `skills/*/SKILL.md`, path `.claude/`, output mode `content`) to find skill-name references; compare against disk inventory.

______________________________________________________________________

## Check 5 — Hardcoded user paths

Use Grep tool (pattern `/Users/|/home/`, glob `{agents/*.md,skills/*/SKILL.md}`, path `.claude/`, output mode `content`) to flag non-portable paths in agent and skill files. Then run a second Grep directly on `.claude/settings.json` with the same pattern to catch absolute hook paths in the settings file.

**Important**: run this check on every file regardless of whether critical or high findings were already found — path portability issues are orthogonal to other severity classes and must not be deprioritized due to presence of more serious findings in the same file.

______________________________________________________________________

## Check 6 — permissions-guide.md drift

Every allow entry must appear in the guide, and vice versa.

```bash
RED='\033[1;31m'; YEL='\033[1;33m'; GRN='\033[0;32m'; NC='\033[0m'
if [ "${JQ_AVAILABLE:-false}" = "false" ] || ! command -v jq &>/dev/null; then  # timeout: 5000
    printf "${YEL}⚠ SKIPPED${NC}: Check 6 — jq not available\n"
elif [ ! -f ".claude/settings.json" ]; then
    printf "${YEL}⚠ SKIPPED${NC}: Check 6 — .claude/settings.json not found\n"
elif [ ! -f ".claude/permissions-guide.md" ]; then
    printf "${YEL}⚠ SKIPPED${NC}: Check 6 — .claude/permissions-guide.md not found\n"
else
    # Allow entries missing from guide
    jq -r '.permissions.allow[]' .claude/settings.json 2>/dev/null | \  # timeout: 5000
    while IFS= read -r perm; do
        grep -qF "\`$perm\`" .claude/permissions-guide.md 2>/dev/null \
            || printf "${YEL}⚠ MISSING from guide${NC}: %s\n" "$perm"
    done

    # Guide entries orphaned (not in allow list)
    grep '^| `' .claude/permissions-guide.md 2>/dev/null | awk -F'`' '{print $2}' | \  # timeout: 5000
    while IFS= read -r perm; do
        jq -e --arg p "$perm" '(.permissions.allow // []) + (.permissions.deny // []) | contains([$p])' .claude/settings.json > /dev/null 2>&1 \  # timeout: 5000
        || printf "${YEL}⚠ ORPHANED in guide${NC}: %s\n" "$perm"
    done
fi
```

______________________________________________________________________

## Check 6b — Permission safety audit

Every `allow` entry must be non-destructive, reversible, and local-only.

Read `.claude/settings.json` using the Read tool and extract the `permissions.allow` list. For each entry, use model reasoning to evaluate it against three criteria:

- **Non-destructive**: does not permanently delete or overwrite data (no `rm -rf`, `git push --force`, `DROP TABLE`)
- **Reversible**: effect can be undone without data loss (local file edits, test runs, read-only queries)
- **Local-only**: does not affect systems outside the working directory or send data to external services

Flag destructive patterns as **critical** (auto-approved destructive commands are always a breaking safety failure). Flag external-state mutations as **high** and raise to user — some (e.g., `gh release create`) may be intentional but must be explicitly acknowledged.

______________________________________________________________________

## Check 7 — Skill frontmatter conflicts

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

______________________________________________________________________

## Check 8 — Model tier appropriateness

Three capability tiers:

| Tier                  | Model      | Example agents                                            |
| --------------------- | ---------- | --------------------------------------------------------- |
| Plan-gated            | `opusplan` | solution-architect, oss-shepherd, self-mentor             |
| Implementation        | `opus`     | sw-engineer, qa-specialist, ai-researcher, perf-optimizer |
| Diagnostics / writing | `sonnet`   | web-explorer, doc-scribe, data-steward                    |
| High-freq diagnostics | `haiku`    | linting-expert, ci-guardian                               |

Extract declared models:

```bash
printf "%-30s %s\n" "AGENT" "MODEL"
for f in .claude/agents/*.md; do # timeout: 5000
    name=$(basename "$f" .md)
    model=$(awk '/^---$/{c++; if(c==2)exit} c==1 && /^model:/{sub(/^model: /,""); print}' "$f")
    printf "%-30s %s\n" "$name" "${model:-(inherit)}"
done
```

Using model reasoning, classify each agent into a tier based on its `<role>`, `description`, and workflow body content. Cross-reference against declared model:

- `focused-execution` agent using `opus` or `opusplan` → **medium** (potential overkill)
- `deep-reasoning` agent using `sonnet` → **high** (likely underpowered)
- **Orchestration signal**: if the agent's workflow body contains `Spawn`, `Agent tool`, or explicit sub-agent delegation, classify as `deep-reasoning` tier regardless of description — `sonnet` on an orchestrating agent → **high**
- `plan-gated` agent using `sonnet` → **high**
- `focused-execution` agent using `haiku` → **not a finding**

**Important**: CLAUDE.md's `## Agent Teams` section specifies models for team-mode spawn instructions — it is NOT a mandate for agent frontmatter. Do NOT flag frontmatter models as violations because they differ from CLAUDE.md's team-mode model spec.

**Report only** — never auto-fix. Model assignments may be intentional trade-offs.

______________________________________________________________________

## Check 9 — Example value vs. token cost

First, detect whether the project has local context files:

```bash
for f in AGENTS.md CONTRIBUTING.md .claude/CLAUDE.md; do # timeout: 5000
    [ -f "$f" ] && printf "✓ found: %s\n" "$f"
done
```

Then scan agent and skill files for inline examples:

````bash
for f in .claude/agents/*.md .claude/skills/*/SKILL.md; do # timeout: 5000
    count=$(grep -cE '^```|^## Example|^### Example' "$f" 2>/dev/null || true)
    lines=$(wc -l <"$f" | tr -d ' ')
    [ "$count" -gt 0 ] && printf "%s: %d example blocks, %d total lines\n" "$f" "$count" "$lines"
done
````

Using model reasoning, classify each example block:

- **High-value**: non-obvious pattern, nuanced judgment, or output-format spec that prose cannot convey → keep
- **Low-value**: restates prose, trivial, or superseded by project-local docs → **low** finding: suggest removing or replacing with a pointer to the local doc

Report per-file: `N examples total, K high-value, M low-value (est. ~X tokens wasted)`.

______________________________________________________________________

## Check 10 — Agent color drift (statusline COLOR_MAP vs frontmatter)

```bash
# Extract color: values declared in agent frontmatter
for f in .claude/agents/*.md; do # timeout: 5000
    name=$(basename "$f" .md)
    color=$(awk '/^---$/{c++; if(c==2)exit} c==1 && /^color:/{sub(/^color: */,""); print}' "$f")
    [ -n "$color" ] && printf "%s: %s\n" "$name" "$color"
done
```

Using model reasoning, cross-reference each extracted color name against the `COLOR_MAP` keys in `.claude/hooks/statusline.js`. Flag:

- Color declared in agent frontmatter but **not a key in `COLOR_MAP`** → **medium** (agent will appear uncolored)
- Color in `COLOR_MAP` that is **not declared by any agent** → **low** (dead mapping, no functional impact)

______________________________________________________________________

## Check 11 — RTK hook alignment

Verify that the prefix list in `.claude/hooks/rtk-rewrite.js` (`RTK_PREFIXES` array) is consistent with the commands the installed RTK binary actually supports.

Skip this check if RTK is not installed (`rtk --version` fails) or if `.claude/hooks/rtk-rewrite.js` does not exist.

```bash
YEL='\033[1;33m'
RED='\033[1;31m'
GRN='\033[0;32m'
CYN='\033[0;36m'
NC='\033[0m'
printf "=== Check 11: RTK hook alignment ===\n"

if ! command -v rtk &>/dev/null; then # timeout: 5000
	printf "${YEL}⚠ SKIPPED${NC}: Check 11 — rtk not installed\n"
elif [ ! -f ".claude/hooks/rtk-rewrite.js" ]; then
	printf "${YEL}⚠ SKIPPED${NC}: Check 11 — .claude/hooks/rtk-rewrite.js not found\n"
else
	if ! command -v node &>/dev/null; then # timeout: 5000
		printf "${YEL}⚠ SKIPPED${NC}: Check 11 RTK parsing — node not in PATH\n"
	else
		RTK_HELP=$(rtk --help 2>&1) # timeout: 5000
		HOOK_PREFIXES=$(node -e "
    const fs = require('fs');
    const src = fs.readFileSync('.claude/hooks/rtk-rewrite.js', 'utf8');
    const m = src.match(/RTK_PREFIXES\s*=\s*\[([^\]]*)\]/s);
    if (!m) { process.exit(1); }
    const entries = m[1].match(/\"[^\"]+\"/g) || [];
    entries.forEach(e => console.log(e.replace(/'/g, '')));
  " 2>/dev/null) # timeout: 5000

		if [ -z "$HOOK_PREFIXES" ]; then
			printf "${YEL}⚠ SKIPPED${NC}: Check 11 — could not parse RTK_PREFIXES from hook file\n"
		else
			INVALID=0
			while IFS= read -r prefix; do
				[ -z "$prefix" ] && continue
				if ! echo "$RTK_HELP" | grep -qw "$prefix"; then
					printf "${RED}! INVALID hook prefix${NC}: '%s' — not a recognized RTK subcommand\n" "$prefix"
					INVALID=$((INVALID + 1))
				fi
			done <<<"$HOOK_PREFIXES"

			META_CMDS="gain discover proxy init version help"
			MISSING=0
			while IFS= read -r rtk_cmd; do
				[ -z "$rtk_cmd" ] && continue
				is_meta=0
				for meta in $META_CMDS; do
					[ "$rtk_cmd" = "$meta" ] && is_meta=1 && break
				done
				[ "$is_meta" -eq 1 ] && continue
				if ! echo "$HOOK_PREFIXES" | grep -qw "$rtk_cmd"; then
					printf "${YEL}⚠ MISSING hook prefix${NC}: '%s' — RTK supports filtering this command but hook does not list it\n" "$rtk_cmd"
					MISSING=$((MISSING + 1))
				fi
			done < <(echo "$RTK_HELP" | grep -oE '^\s{2,4}[a-z][a-z0-9_-]+' | tr -d ' ' | sort -u)

			if [ "$INVALID" -eq 0 ] && [ "$MISSING" -eq 0 ]; then
				printf "${GRN}✓ OK${NC}: Check 11 — RTK hook prefixes aligned with installed RTK version\n"
			fi
		fi
	fi
fi
```

Severity: invalid prefix entries = **high**; missing filterable commands = **medium**. **Report only** — never auto-fix.

______________________________________________________________________

## Check 12 — Memory health (MEMORY.md noise accumulation)

MEMORY.md has a 200-line truncation limit. Three sub-checks:

**12a — Duplicate with CLAUDE.md**: Read both MEMORY.md and CLAUDE.md. For each section in MEMORY.md, check whether the same rule or directive exists verbatim or near-verbatim in CLAUDE.md. Flag duplicates as **low**.

**12b — Stale version pins**:

```bash
MEMORY_FILE="$HOME/.claude/projects/$(git rev-parse --show-toplevel | sed 's|[/.]|-|g')/memory/MEMORY.md" # timeout: 3000
if [ -f "$MEMORY_FILE" ]; then
    grep -nE '(v[0-9]+\.[0-9]+\.[0-9]+|as of [A-Z][a-z]+ 20[0-9]{2})' "$MEMORY_FILE" || echo "no stale pins found" # timeout: 5000
else
    printf "${YEL}⚠ SKIPPED${NC}: Check 12b — MEMORY.md not found at derived path: %s\n" "$MEMORY_FILE"
fi
```

**12c — Absorbed feedback files**:

```bash
MEMORY_DIR="$HOME/.claude/projects/$(git rev-parse --show-toplevel | sed 's|[/.]|-|g')/memory" # timeout: 3000
if [ -d "$MEMORY_DIR" ]; then
    ls "$MEMORY_DIR"/feedback_*.md 2>/dev/null || echo "no feedback files" # timeout: 5000
else
    printf "${YEL}⚠ SKIPPED${NC}: Check 12c — memory dir not found: %s\n" "$MEMORY_DIR"
fi
```

All three sub-checks produce only **low** findings — auto-fixed under `/audit fix all`. Fix action: remove duplicate section, drop version pin, delete absorbed feedback file.

______________________________________________________________________

## Check 13 — Agent description routing alignment

Three sub-checks, all **report-only**.

Extract all agent descriptions:

```bash
printf "%-25s %s\n" "AGENT" "DESCRIPTION"
for f in .claude/agents/*.md; do # timeout: 5000
    name=$(basename "$f" .md)
    desc=$(awk '/^---$/{c++; if(c==2)exit} c==1 && /^description:/{sub(/^description: /,""); print}' "$f")
    printf "%-25s %s\n" "$name" "$desc"
done
```

Apply model reasoning:

**13a — Overlap analysis**: For each pair of agents, assess domain overlap. Flag pairs where descriptions alone do not disambiguate → **medium** finding per ambiguous pair.

**13b — NOT-for clause coverage**: For each high-overlap pair from 13a, check whether at least one agent has a "NOT for" exclusion clause referencing the other or its domain. Missing disambiguation → **medium**.

**13c — Trigger phrase specificity**: For each agent, check whether the description's first clause states an exclusive domain. A vague opener → **low**.

Fix reference: run `/calibrate routing` to verify whether description overlap translates to actual routing confusion.

______________________________________________________________________

## Check 14 — codex plugin integration check

Skip if codex (openai-codex) plugin is not installed.

```bash
RED='\033[1;31m'
GRN='\033[0;32m'
YEL='\033[1;33m'
NC='\033[0m'
CODEX_LINE=$(claude plugin list 2>/dev/null | grep 'codex@openai-codex') # timeout: 5000
if [ -z "$CODEX_LINE" ]; then
    printf "${YEL}⚠ SKIPPED${NC}: Check 14 — codex (openai-codex) plugin not installed\n"
elif echo "$CODEX_LINE" | grep -q 'disabled'; then
    printf "${YEL}⚠ WARN${NC}: Check 14 — codex (openai-codex) plugin installed but DISABLED\n"
    printf "  Fix: run \`claude plugin enable codex@openai-codex\` then \`/reload-plugins\`\n"
else
    printf "${GRN}✓ OK${NC}: Check 14 — codex (openai-codex) plugin present and enabled\n"
fi
```

- Plugin installed but **disabled** → **medium** (fix: `claude plugin enable codex@openai-codex` + `/reload-plugins`)
- Plugin present but dispatches fail → **high** (verify with `/calibrate skills`)

______________________________________________________________________

## Check 15 — Rules integrity and efficiency

Four sub-checks covering `.claude/rules/`. Skip if `rules/` directory does not exist or is empty.

**15a — Inventory vs MEMORY.md**:

```bash
ls .claude/rules/*.md 2>/dev/null | xargs -I{} basename {} .md | sort # timeout: 5000
```

Rules on disk but absent from MEMORY.md roster → **medium**. Rules in MEMORY.md but absent on disk → **medium**.

**15b — Frontmatter completeness**:

```bash
for f in .claude/rules/*.md; do # timeout: 5000
    desc=$(awk '/^---$/{c++; if(c==2)exit} c==1 && /^description:/{found=1} END{print found+0}' "$f")
    [ "$desc" -eq 0 ] && printf "MISSING description: %s\n" "$f"
done
```

Missing `description:` → **high**. Malformed `paths:` → **high**.

**15c — Redundancy check**: For each rule file, identify 2–3 most specific directive phrases. Grep those phrases verbatim in `.claude/CLAUDE.md` and `.claude/agents/*.md`. If exact phrase exists in ≥2 locations outside the rule file → **medium** (distillation incomplete).

```bash
grep -l "Never switch to NumPy" .claude/agents/*.md .claude/CLAUDE.md 2>/dev/null # timeout: 5000
grep -l "never git add" .claude/agents/*.md .claude/CLAUDE.md 2>/dev/null         # timeout: 5000
```

**15d — Cross-reference integrity**: Grep agent files, skill files, and CLAUDE.md for references to `.claude/rules/<name>.md` patterns. Verify each referenced filename exists on disk → missing file → **high**.

```bash
grep -rh '\.claude/rules/[a-z_-]*\.md' .claude/agents/ .claude/skills/ .claude/CLAUDE.md 2>/dev/null |
grep -o 'rules/[a-z_-]*\.md' | sort -u # timeout: 5000
```

Severity: 15b = **high**; 15a/15c/15d = **medium**.

______________________________________________________________________

## Check 16 — Cross-file content duplication (>40% consecutive step overlap)

```bash
printf "%-30s %s\n" "FILE" "STEPS"
for f in .claude/skills/*/SKILL.md; do # timeout: 5000
    name="skills/$(basename "$(dirname "$f")")"
    steps=$(grep -c '^## Step' "$f" 2>/dev/null || echo 0)
    printf "%-30s %d\n" "$name" "$steps"
done
for f in .claude/agents/*.md; do
    name="agents/$(basename "$f" .md)"
    sections=$(grep -c '^## ' "$f" 2>/dev/null || echo 0)
    printf "%-30s %d\n" "$name" "$sections"
done
```

Using model reasoning, compare the workflow body of each file against all others in its class. For each pair:

1. Count steps in each file: N_A and N_B
2. Find the longest consecutive run of substantially similar steps: N_run
3. Compute run fraction: `max(N_run / N_A, N_run / N_B)`
4. Flag if run fraction ≥ 0.4 (40%)

Scattered similarity does **not** count — only a contiguous block triggers this check. **Severity**: **medium** — report only, never auto-fix.

______________________________________________________________________

## Check 17 — File length (context budget risk)

Thresholds: agents > 300 lines · skill SKILL.md > 600 lines · rules > 200 lines.

```bash
YEL='\033[1;33m'
GRN='\033[0;32m'
NC='\033[0m'
printf "%-52s %s\n" "FILE" "LINES"
for f in .claude/agents/*.md; do # timeout: 5000
    lines=$(wc -l <"$f" | tr -d ' ')
    [ "$lines" -gt 300 ] &&
    printf "${YEL}⚠ TOO LONG${NC}: agents/%s — %d lines (threshold: 300)\n" "$(basename "$f")" "$lines" ||
    printf "  %-50s %d\n" "agents/$(basename "$f")" "$lines"
done
for f in .claude/skills/*/SKILL.md; do
    lines=$(wc -l <"$f" | tr -d ' ')
    [ "$lines" -gt 600 ] &&
    printf "${YEL}⚠ TOO LONG${NC}: skills/%s/SKILL.md — %d lines (threshold: 600)\n" "$(basename "$(dirname "$f")")" "$lines" ||
    printf "  %-50s %d\n" "skills/$(basename "$(dirname "$f")")/SKILL.md" "$lines"
done
for f in .claude/rules/*.md; do
    lines=$(wc -l <"$f" | tr -d ' ')
    [ "$lines" -gt 200 ] &&
    printf "${YEL}⚠ TOO LONG${NC}: rules/%s — %d lines (threshold: 200)\n" "$(basename "$f")" "$lines" ||
    printf "  %-50s %d\n" "rules/$(basename "$f")" "$lines"
done
```

**Severity**: **medium** — report only, never auto-fix.

______________________________________________________________________

## Check 18 — Bash command misuse / native tool substitution

```bash
YEL='\033[1;33m'
GRN='\033[0;32m'
CYN='\033[0;36m'
NC='\033[0m'
printf "=== Check 18: Bash misuse candidates ===\n"
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
printf "${GRN}✓${NC}: Check 18 scan complete\n"
```

After the scan, apply model reasoning to each match — exclude cases where the shell command is genuinely necessary. Flag only where the native tool is a direct drop-in replacement.

| Shell command                      | Preferred native tool | Severity |
| ---------------------------------- | --------------------- | -------- |
| `cat <file>`                       | Read tool             | medium   |
| `grep`/`rg` for content search     | Grep tool             | medium   |
| `find`/`ls` for file listing       | Glob tool             | medium   |
| `echo … >` / `tee` to write a file | Write tool            | medium   |
| `sed`/`awk` for text substitution  | Edit tool             | medium   |

**Report only** — never auto-fix; some Bash invocations in example/illustration code blocks are intentional.

______________________________________________________________________

## Check 19 — Stale settings.json allow entries

```bash
YEL='\033[1;33m'
GRN='\033[0;32m'
NC='\033[0m'
if [ "${JQ_AVAILABLE:-false}" = "false" ] || ! command -v jq &>/dev/null; then # timeout: 5000
    printf "${YEL}⚠ SKIPPED${NC}: Check 19 — jq not available\n"
elif [ ! -f ".claude/settings.json" ]; then
    printf "${YEL}⚠ SKIPPED${NC}: Check 19 — .claude/settings.json not found\n"
else
    printf "=== Check 19: Stale allow entries ===\n"
    jq -r '.permissions.allow[]' .claude/settings.json 2>/dev/null | while IFS= read -r entry; do # timeout: 5000
        cmd=$(echo "$entry" | sed 's/^[A-Za-z]*(\(.*\))$/\1/' | sed 's/^"\(.*\)"$/\1/')
        hits=$(grep -rl "$cmd" .claude/agents/ .claude/skills/ .claude/rules/ .claude/hooks/ .claude/CLAUDE.md 2>/dev/null | wc -l | tr -d ' ') # timeout: 5000
        if [ "$hits" -eq 0 ]; then
            printf "${YEL}⚠ STALE allow${NC}: %s — no usage found in .claude/ files\n" "$entry"
        fi
    done
    printf "${GRN}✓${NC}: Check 19 scan complete\n"
fi
```

**Severity**: **low** per stale entry. Fix: remove the stale entry from `settings.json` (report only — `settings.json` is never auto-edited per audit policy).

**Important**: some allow entries intentionally grant broad patterns (e.g., `Bash(mkdir -p .reports/audit/*)`) that do not appear verbatim in config files — they are exercised at runtime. Flag only entries whose command fragment appears nowhere in any `.claude/` file.

______________________________________________________________________

## Check 20 — Calibration coverage gap

**Step 1 — Read the calibrate domain table**: Read `.claude/skills/calibrate/modes/skills.md` and extract the registered target list under `### Domain table`. Build the set of registered targets.

**Step 2 — Scan all skill modes on disk**: Use Glob (`skills/*/SKILL.md`, path `.claude/`) and Glob (`skills/*/modes/*.md`, path `.claude/`) to enumerate every skill and mode file. Extract mode names from `argument-hint:` frontmatter and `## Mode:` / `### Mode:` headings.

**Step 3 — Validate registered targets exist on disk**: For each registered target, verify the corresponding skill/mode file exists. A registered target with no matching file → **medium** (calibrate will fail at runtime).

**Step 4 — Identify unregistered calibratable candidates** (model reasoning):

A mode is calibratable when ALL three signals are present:

1. **Deterministic structured output**: findings list, completeness checklist, structured table, or machine-readable verdict
2. **Synthetic input feasible**: can be tested without external services
3. **Ground truth constructable**: known issues can be injected and scored

→ Unregistered mode matching all three signals: **low** (add to `calibrate/modes/skills.md` domain table)

______________________________________________________________________

## Check 21 — Markdown heading hierarchy continuity

````bash
GRN='\033[0;32m'
YEL='\033[1;33m'
NC='\033[0m'
printf "=== Check 21: Heading hierarchy continuity ===\n"
violations=0
for f in .claude/agents/*.md .claude/skills/*/SKILL.md .claude/rules/*.md; do # timeout: 5000
    [ -f "$f" ] || continue
    awk -v file="$f" '
    /^```/ { in_code = !in_code; next }
    in_code { next }
    /^#+ / {
      n = 0; s = $0
      while (substr(s,1,1) == "#") { n++; s = substr(s,2) }
      if (prev > 0 && n > prev + 1) {
        printf "  \033[1;33m⚠ HEADING JUMP\033[0m: %s:%d — h%d followed by h%d (skipped h%d)\n", \
          file, NR, prev, n, prev+1
        found++
      }
      prev = n
    }
    END { exit (found > 0) ? 1 : 0 }
  ' "$f" || violations=$((violations + 1))
done
if [ "$violations" -eq 0 ]; then
    printf "${GRN}✓${NC}: Check 21 — no heading hierarchy violations found\n"
fi
````

**Severity**: **medium** — heading jumps impair navigation. Fix: insert missing intermediate heading level, or demote/promote the offending heading. **Report only** — never auto-fix.
