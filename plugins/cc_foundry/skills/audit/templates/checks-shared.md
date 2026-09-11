# Shared Checks (all scopes) — 17, 4, 5, 9, 16, 15

## Check 12 — File length (context budget risk)

Thresholds: agents > 300 lines (~4 k tokens) · skill SKILL.md > 600 lines (~8 k tokens) · rules > 200 lines (~2.5 k tokens).

> **`bin/` scripts are exempt** — executables run via subprocess, never loaded into LLM context; size irrelevant to token budget. Check 12 applies to `.md` config files only.

> **Line count = human-readable proxy; token count = true measure.** Thresholds guide human review — not actual context budget. Short sentences + short lines preferred: easier to read AND cheaper per logical unit. Collapsing multiple short lines into one long line does NOT reduce token cost and destroys readability. Fix = remove or distill content. Collapsing lines is not a fix.

```bash
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/measure_config_size.py" --mode inventory  # timeout: 10000
```

**Severity**: **medium** — report only, never auto-fix. When flagging, remind fixer: only content removal or distillation counts; collapsing lines not acceptable.

## Check 13 — Markdown heading hierarchy continuity

````bash
printf "=== Check 13: Heading hierarchy continuity ===\n"
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
        printf "  ⚠ HEADING JUMP: %s:%d — h%d followed by h%d (skipped h%d)\n", \
          file, NR, prev, n, prev+1
        found++
      }
      prev = n
    }
    END { exit (found > 0) ? 1 : 0 }
  ' "$f" || violations=$((violations + 1))
done
if [ "$violations" -eq 0 ]; then
    printf "✓: Check 13 — no heading hierarchy violations found\n"
fi
````

**Severity**: **medium** — heading jumps impair navigation. Fix: insert missing intermediate heading level, or demote/promote offending heading. **Report only** — never auto-fix.

## Check 14a — Structural tag symmetry

Checks two failure modes: (1) empty blocks — `<tag></tag>` with only whitespace between open and close; (2) unbalanced tags — open count differs from close count. Both leave files structurally broken.

Scan all agent and skill files via deterministic bin/ script:

```bash
printf "=== Check 14a: Structural tag symmetry ===\n"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/check_tag_symmetry.py" \
    .claude/agents/*.md .claude/skills/*/SKILL.md  # timeout: 10000
```

**Severity**: **medium** — gate-level; must fix before audit passes.

- **Empty block**: **Auto-fix: YES** — remove empty open+close tag pair entirely; no content to lose.
- **Unbalanced tag**: **Auto-fix: NO** — missing open or close tag requires manual inspection to determine intended structure.

> Root cause: prior fix moved or removed block content but left container tags (empty block); or copy-paste error dropped closing tag (unbalanced). Empty `<constants>` most common empty-block case.

## Check 14b — Code fence symmetry

Detects two failure modes: (1) unclosed fence — opening ```` ``` ```` or ```` ```lang ```` with no matching closing ```` ``` ````; (2) bad nesting — inner fence uses same or more backticks as outer (outer must use ` ` or more to contain inner ```` ``` ````).

Scan all agent and skill files via deterministic bin/ script:

```bash
printf "=== Check 14b: Code fence symmetry ===\n"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/check_fence_symmetry.py" \
    .claude/agents/*.md .claude/skills/*/SKILL.md  # timeout: 10000
```

**Severity**: **high** — unclosed fence corrupts all subsequent code blocks in the file; Claude misparses the rest of the file.

- **Unclosed fence**: **Auto-fix: YES** — add missing closing ```` ``` ```` at end of block; confirm content boundary by reading surrounding context.
- **Bad nesting**: **Auto-fix: YES** — promote outer fence to ` ` ` `; or demote inner if outer is intentionally 3-backtick.
- **Timeout comment on closing fence** (```` ``` # timeout: N ````): **Auto-fix: YES** — move comment to last command inside block; change closing line to plain ```` ``` ````.

> Root cause: timeout annotation placed on closing fence delimiter instead of inside block (most common); or copy-paste lost a closing ```` ``` ````.

## Check 14c — README drift

Detects README facts drifted from disk: (1) a literal `Current version: `X.Y.Z\`\` marker not matching the plugin's `plugin.json` version; (2) a `.py`/`.sh` script named on a README line mentioning `bin/` (or as an explicit `plugins/<plugin>/bin/<name>` path) existing nowhere in the plugin. Arbitrary version-shaped strings (release examples, historical benchmark tags) ignored — only the explicit marker checked.

Scan all plugins via deterministic bin/ script:

```bash
printf "=== Check 14c: README drift ===\n"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/check_readme_drift.py" \
    --scan-dir plugins  # timeout: 10000
```

**Severity**: **medium** — user-facing wrong facts; gate-level.

- **Version marker drift**: **Auto-fix: YES** — update the marker to the current `plugin.json` version.
- **Stale bin/ reference**: **Auto-fix: NO** — resolve to the current script name (often a sh→py migration) by inspecting the actual `bin/` directory.

> Root cause: operational constants and inventories duplicated into README prose by hand; README-sync is convention-only and demonstrably fails. Also enforced pre-commit (per-file on `README.md`).

## Check 14d — Mode dispatch integrity

Detects dangling mode-dispatch references: a SKILL.md line routing control to a named section (e.g. `go to "Mode: Lessons Distillation"` or `skip to **Mode: X**`) with no matching `## Mode: X` header in the same file — the half-done-rename bug class where the header was renamed but a dispatch line still points at the old name. Scan all plugins via deterministic bin/ script:

```bash
printf "=== Check 14d: Mode dispatch integrity ===\n"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/check_mode_dispatch.py" \
    --scan-dir plugins  # timeout: 10000
```

**Severity**: **high** — dangling dispatch sends the agent to a section that does not exist; the mode silently never runs.

- **Dangling dispatch**: **Auto-fix: NO** — resolve to the intended header name (restore the renamed header or update the dispatch line to match); manual inspection determines which side is stale.

> Root cause: a `## Mode: <Name>` header renamed without updating every `go to`/`skip to`/`see` dispatch line that references it (or vice versa).

## Check 14e — Cross-plugin shared-file drift

Detects byte-level drift in files that must be identical across plugins because each plugin ships its own copy of a shared mechanism (a plugin cannot depend on another being installed). The canonical copy lives in one plugin; others must track it byte-for-byte. Source of truth is the `MANIFEST` in the script (currently the `agent-router.js` fallback hook: foundry canonical → oss/develop/research copies). Files that legitimately vary per plugin (e.g. `agent-resolution.md` fallback tables, per-plugin `rules/quality-gates.md`) intentionally NOT in the manifest.

Scan via deterministic bin/ script:

```bash
printf "=== Check 14e: Cross-plugin shared-file drift ===\n"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/propagate_shared.py"  # timeout: 10000
```

**Severity**: **high** — a stale copy means one plugin runs old fallback logic; behaviour diverges silently by plugin.

- **Drifted copy**: **Auto-fix: YES** — run `propagate_shared.py --apply` to overwrite copies with the canonical.

> Root cause: the root `Makefile` does not propagate cross-plugin shared files; a canonical edit was not mirrored into the consuming plugins. Also enforced pre-commit.

## Check 14f — Unmanaged codemap index-guard copy

Detects a hand-written codemap index path in a file that neither a `MANIFEST` entry nor the guard registry covers. The guard ("is codemap-py installed, and does an index exist for this project?") was hand-copied across four plugins with nothing linking the copies, so each drifted alone and one path fix cost ten edits. Two shapes are permitted: consume the provider CLI (`codemap-py query`, `codemap_resolve.py` — such a consumer never spells the path, so it never trips this check), or one canonical copy propagated byte-identical. Inline bash in agent/skill prose is a fragment `MANIFEST` cannot propagate, so those copies are named in the script's `REGISTRY` with a reason and held to two invariants: index dir anchored to a project-root variable (never CWD), project name the raw basename (never sanitized).

```bash
printf "=== Check 14f: Unmanaged codemap index-guard copy ===\n"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/check_codemap_guard.py"  # timeout: 10000
```

**Severity**: **high** — an unmanaged copy drifts silently; a subdir-anchored or sanitized-name copy reports `no_index` while an index exists, and the agent falls back to Grep with no error.

- **Unregistered copy**: **Auto-fix: NO** — pick a shape first (provider CLI preferred), then add the `MANIFEST` or `REGISTRY` entry.
- **Invariant violation**: **Auto-fix: NO** — restore root anchoring / raw basename at the offending line.
- **Stale registry entry**: **Auto-fix: YES** — drop the entry; the file no longer holds a guard.

> Root cause: no structural link between copies of a mechanism duplicated by policy. `--list` prints the full inventory with each copy's shape. Also enforced pre-commit.

## Check 15 — Hardcoded user paths

Use Grep tool (pattern `/Users/|/home/`, glob `{agents/*.md,skills/*/SKILL.md}`, path `.claude/`, output mode `content`) to flag non-portable paths in agent and skill files. Run second Grep on `.claude/settings.json` with same pattern to catch absolute hook paths.

**Important**: run on every file regardless of prior critical/high findings — path portability orthogonal to other severity classes, must not deprioritize.

Also grep for bare `plugins/<name>/` prefix as primary path in skill/agent bodies — source-tree paths working during authoring but breaking post-install. See Check C32 for full scan.

## Check 16 — Example value vs. token cost

First, detect whether project has local context files:

```bash
for f in AGENTS.md CONTRIBUTING.md .claude/CLAUDE.md; do # timeout: 5000
    [ -f "$f" ] && printf "✓ found: %s\n" "$f"
done
```

Scan agent and skill files for inline examples:

````bash
for f in .claude/agents/*.md .claude/skills/*/SKILL.md; do # timeout: 5000
    count=$(grep -cE '^```|^## Example|^### Example' "$f" 2>/dev/null || true)
    lines=$(wc -l <"$f" | tr -d ' ')
    [ "$count" -gt 0 ] && printf "%s: %d example blocks, %d total lines\n" "$f" "$count" "$lines"
done
````

Classify each example block via model reasoning:

- **High-value**: non-obvious pattern, nuanced judgment, or output-format spec prose can't convey → keep
- **Low-value**: restates prose, trivial, or superseded by project-local docs → **low** finding: suggest removing or replacing with pointer to local doc

Report per-file: `N examples total, K high-value, M low-value (est. ~X tokens wasted)`.

## Check 17 — Cross-file code block inventory

Block count across all .md files in scope. NxN similarity analysis is expensive — runs in `--efficiency` mode only (Phase B2), which subsumes this check. When `--efficiency` active, skip Check 17.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/list_audit_files.py" $( [ "$LOCAL_MODE" = "true" ] && echo "--local" )  # timeout: 30000
```

Flag files with block count ≥ 10 as extraction candidates — recommend `--efficiency` run for full NxN analysis.

> **A `0` in the BLOCKS column is a correct result, not a filtering bug** — `_shared/agent-resolution.md` is the standing example. Those rows are the regression test for the `grep -c` fallback defect fixed in the block above: with `|| echo 0`, a zero-match file captured `"0\n0"` and aborted the arithmetic, so the check emitted nothing at all. Keep zero rows in the output; their presence is the evidence the count path still works.

For 17a (step-level prose overlap, ≥40% consecutive steps): flag pair, name canonical owner; route to Check 20 `merge-prune` if no clear owner.

| Sub-check | Algorithm | Threshold | Severity | Output |
| -- | -- | -- | -- | -- |
| 17a — step overlap | consecutive step fraction | ≥40% steps | medium | findings list only |
| 17b — block duplicate | NxN similarity (moved) | run `--efficiency` for full analysis | — | Phase B2 in efficiency.md |

## Check C32 — Hardcoded source-tree paths (install-path regression)

Plugin skill and agent files must not contain bare `plugins/<name>/` paths as primary references. Resolve in source tree but break post-install where `plugins/` absent. Install-path resolution pattern (cache + fallback) mandatory.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
printf "=== Check C32: Hardcoded source-tree paths ===\n"
# C32 inherently scans plugin source tree — LOCAL mode only
if [ "$LOCAL_MODE" != "true" ]; then
    printf "✓ [Check C32/shared] Skipped in non-local mode (no plugin source tree)\n"
else
    grep -rn ' plugins/[a-z]' plugins/*/skills/*/SKILL.md plugins/*/agents/*.md 2>/dev/null |
      grep -v '^Binary' |
      grep -v '^\s*#' |
      grep -v '&& .*plugins/' |
      grep -v ':-.*plugins/' |
      grep -v '"plugins/' | grep -v "'plugins/" | while IFS= read -r hit; do
        printf "! BREAKING C32: %s\n" "$hit"
        printf "  fix: replace with installed-path resolution:\n"
        printf "        VAR=\"\$(ls -td ~/.claude/plugins/cache/borda-ai-rig/<plugin>/*/skills/_shared 2>/dev/null | head -1)\"\n"
        printf "        [ -z \"\$VAR\" ] && VAR=\"plugins/<plugin>/skills/_shared\"\n"
    done
    printf "✓: Check C32 scan complete\n"
fi  # timeout: 5000
```

Severity: **high** — skill silently fails for any user who installed via marketplace (primary install path).

| Sub-check | Pattern | Severity | Auto-fix |
| -- | -- | -- | -- |
| C32 — source-tree primary path | `plugins/<name>/` not in comment or fallback | high | no |

> **Related**: Check 15 covers `/Users/` and `/home/` hardcoded paths. C32 covers `plugins/` source-tree paths.

## Check 18 — Rules integrity and efficiency

Four sub-checks covering `.claude/rules/`. Skip if `rules/` directory absent or empty.

**18a — Inventory vs MEMORY.md**:

```bash
ls .claude/rules/*.md 2>/dev/null | xargs -I{} basename {} .md | sort # timeout: 5000
```

Rules on disk absent from MEMORY.md → **medium**. Rules in MEMORY.md absent on disk → **medium**.

**18b — Frontmatter completeness**:

```bash
for f in .claude/rules/*.md; do # timeout: 5000
    desc=$(awk '/^---$/{c++; if(c==2)exit} c==1 && /^description:/{found=1} END{print found+0}' "$f")
    [ "$desc" -eq 0 ] && printf "MISSING description: %s\n" "$f"
done
```

Missing `description:` → **high**. Malformed `paths:` → **high**.

**18c — Redundancy check**: Per rule file, identify 2–3 most specific directive phrases. Grep verbatim in `.claude/CLAUDE.md` and `.claude/agents/*.md`. Exact phrase in ≥2 locations outside rule file → **medium** (distillation incomplete).

```bash
grep -l "Never switch to NumPy" .claude/agents/*.md .claude/CLAUDE.md 2>/dev/null # timeout: 5000
grep -l "never git add" .claude/agents/*.md .claude/CLAUDE.md 2>/dev/null         # timeout: 5000
```

**18d — Cross-reference integrity**: Grep agent files, skill files, CLAUDE.md for `.claude/rules/<name>.md` patterns. Verify each referenced filename exists on disk → missing → **high**.

```bash
grep -rh '\.claude/rules/[a-z_-]*\.md' .claude/agents/ .claude/skills/ .claude/CLAUDE.md 2>/dev/null |
grep -o 'rules/[a-z_-]*\.md' | sort -u # timeout: 5000
```

Severity: 18b = **high**; 18a/18c/18d = **medium**.

## Check 25 — Implicit agent references (missing plugin prefix)

All agent dispatch calls must use fully-qualified plugin-prefixed form (`foundry:sw-engineer`, `oss:shepherd`, etc.). Bare names like `sw-engineer` ambiguous: rely on `~/.claude/agents/` symlinks present, break if symlinks stale, missing, or pointing to wrong plugin.

Scan agent files, skill files, CLAUDE.md for `subagent_type=` patterns:

```bash
printf "=== Check 25: Implicit agent references ===\n"
grep -rn 'subagent_type=' .claude/agents/ .claude/skills/ .claude/CLAUDE.md 2>/dev/null |
grep -v '^Binary' |
grep 'subagent_type="[a-z]' |
grep -v '"[a-z][a-z-]*:[a-z]' |
grep -v '"general-purpose"\|"Explore"\|"Plan"\|"claude-code-guide"\|"statusline-setup"' || true  # timeout: 5000
```

Exempt built-in types (no plugin prefix required): `general-purpose`, `Explore`, `Plan`, `claude-code-guide`, `statusline-setup`.

Every non-exempt bare name = **high** finding:

```text
[high] Implicit agent reference: subagent_type="<name>" in <file>
fix: use fully-qualified form, e.g. subagent_type="foundry:<name>"
```

**Report only** — no auto-fix; correct prefix depends on which plugin owns agent.

> **Related**: Check 28 (in `checks-skills.md`) covers cross-plugin fallback coverage — dispatched agent exists but no fallback when that plugin absent. Check 25 and Check 28 address different failure modes; run both.

## Check 29 — LLM context minimality (verbosity)

Every token in agent, skill, rule file = inference cost on every invocation. Each file must be semantically minimal — all information retained, zero redundant wording.

**Scan targets**: `.claude/agents/*.md`, `.claude/skills/*/SKILL.md`, `.claude/rules/*.md`.

Via model reasoning, apply four criteria per file:

**1 — Within-file repetition**: same rule or instruction in two sections. Sub-bullet fully restates parent with no additive content. Workflow step re-explains constraint already defined in preamble or `<notes>`.

**2 — Prose inflation**: filler preambles ("Note that", "It is important to", "Please be aware", "Keep in mind") — flag phrase; substantive content survives without it. Unconditional rule hedged with "might", "could potentially", "in some cases" where rule absolute. Opening sentence paraphrases heading with no additive content.

**3 — Restatement of obvious consequence**: "Do X" immediately followed by "Failing to do X causes Y" where Y self-evident from X alone.

**4 — Information gap test (mandatory before flagging any candidate)**: "If removed, can reader reconstruct from remaining content?" YES = safe to flag. NO = not a finding — content load-bearing even if verbose. Always skip: code blocks, inline examples (Check 16), cross-reference tables, numbered lists where order carries meaning.

Per finding: location (section heading + approx line range) · pattern type (repetition / prose-inflation / obvious-consequence) · estimated token savings (small \<20 / medium 20–80 / large >80) · proposed shorter form or "remove entirely".

**Severity**: **medium** — total savings >= medium across >= 2 distinct locations. **low** — isolated small savings only. **Report only** — never auto-fix; minimization risks removing load-bearing nuance.

**29a — Trigger-inverse restatement**: TRIGGER/SKIP or fires-when/skip-when adjacent pairs where second item is pure logical negation of first. Example: "TRIGGER when X" immediately followed by "SKIP when not X" — second adds zero information.

Via model reasoning: extract TRIGGER and SKIP bullet lists from agent and skill files. For each TRIGGER condition, check adjacent SKIP section for negation complement (same subject, negated predicate). Flag pair if second is reconstructable from first by negation alone.

Information gap test (mandatory): "If SKIP bullet removed, can reader infer from TRIGGER + closed-world assumption?" YES = flag. NO = retain (SKIP carries additive context — e.g. different agent type, overlapping domain clarification).

Per finding: file · TRIGGER line · SKIP line · one-line reason second is pure negation. **Severity**: **low** — report only; never auto-remove (negation-form SKIP bullets may carry implicit scope narrowing not obvious from trigger alone).

**29b — Non-actionable / hedged absolute directive**: directive using "consider", "may", "might", "should ideally", "try to", "where possible" where surrounding context makes rule absolute (no conditionality intended). Also: step missing verb+object+condition triad — subject-only or object-only instructions with no triggering condition.

Via model reasoning per file: scan workflow steps and rule bullets. Flag where:

- Hedging word present + no conditional clause justifying it (absolute rule weakened by hedge)
- Step body is object-only ("error handling", "edge cases") with no verb or condition

Information gap test (mandatory): "Does hedge word change correct behavior?" YES (hedge load-bearing) = skip. NO = flag as prose-inflation variant.

Per finding: file · section · hedged phrase → proposed imperative form. **Severity**: **low** — report only.

## Check 26 — Symbol and shortcut consistency

Three sub-checks for within-file consistency of emoji symbols, slash-command notation, legend alignment.

**26a — Emoji/symbol consistency within files**

Per agent or skill file, extract lines with emoji and annotated concept label. Group by concept. Flag concepts with 2+ distinct emoji in same file.

````bash
printf "=== Check 26a: Emoji/symbol consistency ===\n"
for f in .claude/agents/*.md .claude/skills/*/SKILL.md; do # timeout: 5000
    [ -f "$f" ] || continue
    awk '/^```/{skip=!skip} !skip && /[🔴🟡🟢🔵⛔✅❌⚠️💭▶️🔗🔹🔸🚫]/{print FILENAME": "NR": "$0}' "$f" 2>/dev/null
done
````

Via model reasoning, identify concept labels (e.g., "closed", "open", "active focus", "merged") appearing with two+ distinct symbols in same file. Example: file marks branch 🔴 (closed) in one section and ⛔ closed in another = violation.

Flag: `[medium] Inconsistent symbol for "<concept>" in <file>: <symbol-A> (line N) vs <symbol-B> (line M)`

**26b — Slash command notation consistency**

Directive references to other skills (e.g., "run → /audit") must use `/name` form. Prose mentions (e.g., "the audit skill") may omit slash. Flag files mixing `` `/name` `` and `` `name` `` in same directive context.

```bash
printf "=== Check 26b: Slash command notation ===\n"
for f in .claude/agents/*.md .claude/skills/*/SKILL.md; do # timeout: 5000
    [ -f "$f" ] || continue
    grep -n '→ `/\?[a-z][a-z:-]*`\|run `/\?[a-z][a-z:-]*`\|suggest.*`/\?[a-z][a-z:-]*`' "$f" 2>/dev/null
done
```

Via model reasoning: same skill referenced with both `/name` and bare `name` in directive context in same file → **low** finding.

**26c — Legend ↔ body symbol alignment**

When file defines legend (any line matching `Legend:` followed by symbol/concept pairs), every body use of concept must match legend symbol exactly.

```bash
printf "=== Check 26c: Legend/key alignment ===\n"
grep -n 'Legend:\|^Key:' .claude/agents/*.md .claude/skills/*/SKILL.md 2>/dev/null || true # timeout: 5000
```

Via model reasoning: extract (symbol, concept) pairs from legend. Per concept, scan file body outside code fences for different symbol. Flag: `Legend defines <concept> as <symbol-A> but body uses <symbol-B> at line N`.

**Report only** — never auto-fix; symbol choices may be intentional or constrained by existing docs.

| Sub-check | Severity | Auto-fix |
| -- | -- | -- |
| 26a — same concept, different symbols | medium | no |
| 26b — directive notation mixed `/name` vs `name` | low | no |
| 26c — body symbol contradicts legend | medium | no |

## Check 41 — LLM-first formatting conventions

Config files consumed primarily by LLM at inference time. Formatting inconsistencies force LLM to resolve ambiguity before parsing content — wasted tokens, degraded reliability. **Principle**: compact + robust + minimal variation. One canonical form per pattern type per file.

**Scan targets**: all `*.md` files under `.claude/` and `plugins/`, excluding any file named `README.md`. Each sub-check block below re-derives that file list inline — a shared assignment in its own block would not survive into the next Bash call (Check 43), and `mapfile` is a bash builtin absent from zsh (Check 45's note).

Via model reasoning, apply four sub-checks per file:

**41a — List marker uniformity**: scan all unordered list lines outside code fences. Collect distinct markers used (`-`, `*`, `+`). More than one distinct marker in same file = finding. Mixed markers = ambiguous parse order for LLM; `-` is canonical.

````bash
printf "=== Check 41a: List marker uniformity ===\n"
while IFS= read -r f; do  # timeout: 5000
    [ -f "$f" ] || continue
    markers=$(awk '/^```/{skip=!skip} !skip && /^[[:space:]]*[*+] /{print $1}' "$f" | sort -u | tr '\n' ' ')
    [ -n "$markers" ] && echo "$f: uses markers: $markers"
done < <(find .claude plugins -name "*.md" ! -name "README.md" 2>/dev/null | sort)
````

Via model reasoning: for each file printing markers, confirm multiple distinct markers present outside code fences. Flag files with `*` or `+` alongside `-`.

**41b — Numbering intent clarity**: two numbering registers must not be mixed in same document context:

- `1.` `2.` `3.` — sequential steps (implies ordering + dependency)
- `(a)` `(b)` `(c)` — choices / alternatives (implies selection, no ordering)

Violations to flag:

- `1.` `2.` used for choices inside AskUserQuestion option blocks or "choose one" lists
- `(a)` `(b)` used for sequential workflow sub-steps where ordering matters

```bash
printf "=== Check 41b: Numbering intent ===\n"
while IFS= read -r f; do  # timeout: 5000
    [ -f "$f" ] || continue
    grep -n 'AskUserQuestion' "$f" 2>/dev/null | grep -q '.' && grep -B5 -A5 'AskUserQuestion' "$f" | grep -n '^\s*[0-9]\.' 2>/dev/null | head -5 && echo "  ^^^ $f (numbered options in AskUserQuestion block)"
done < <(find .claude plugins -name "*.md" ! -name "README.md" 2>/dev/null | sort)
```

Via model reasoning: around each AskUserQuestion block, check whether options use `1.`/`2.` (violation) or `(a)`/`(b)` (compliant). In workflow steps, verify numbered sub-items (`1.`, `2.`) represent sequential actions, not option choices.

**41c — Table vs nested prose**: when content has 3+ list items each with 2+ fixed-schema attributes, a Markdown table is more compact and structurally clearer for LLM parse than nested prose bullets.

Via model reasoning per file: identify nested bullet blocks where each top-level bullet has ≥2 sub-bullets with consistent attribute structure across items (e.g., every item has "Input:", "Output:", "When:"). If block has ≥3 top-level items with ≥2 uniform sub-attributes → flag as table candidate.

Exception: skip when attributes vary per item (non-uniform schema — prose correct).

**41d — Legacy mixed/decimal phase-step numbering**: canonical sub-step convention (precedent: `oss:review`'s `Step 3a`–`3e`, `oss:audit`'s `Step 5b`, Check 44 below) is **number-primary, letter-secondary, no separator** — `1b`, `3a`, `41a`. A `### Phase`/`Step`/`Check`/`Mode`/`Section` header using a decimal or letter-primary variant instead (`1.5`, `A.5`, `5.b`) is a legacy/inconsistent form that predates or bypassed that convention — same intent, wrong register, and outside Check 44's scope (whose regex only matches literal `Check N<letter>`, not general workflow headers).

```bash
printf "=== Check 41d: Legacy phase/step numbering ===\n"
found41d=0
while IFS= read -r f; do  # timeout: 5000
    [ -f "$f" ] || continue
    hdr=$(grep -nE '^#{1,6}[[:space:]]+(Phase|Step|Check|Mode|Section)[[:space:]]+[A-Za-z]*[0-9]+\.[0-9A-Za-z]+' "$f" 2>/dev/null)
    [ -n "$hdr" ] && { echo "$hdr" | sed "s|^|$f:|"; found41d=1; }
done < <(find .claude plugins -name "*.md" ! -name "README.md" 2>/dev/null | sort)
[ "$found41d" -eq 0 ] && printf "✓: Check 41d — no legacy decimal/mixed phase-step headers\n"
```

Via model reasoning: for each flagged header, find the file's own established sibling convention (existing `<N><letter>` sub-steps in the same file) and propose the matching rename. Before renaming, grep the same file (and, for cross-plugin shared files, every consumer) for every other mention of the flagged token — header and every prose cross-reference get renamed together in one pass, never left half-updated.

**Severity**: P3 — report only. Never auto-fix; reformatting risks layout regression in rendered contexts. Flag only clear violations with concrete line references.

| Sub-check | Severity | Auto-fix |
| -- | -- | -- |
| 41a — mixed list markers | low | no |
| 41b — numbering register mismatch | medium | no |
| 41c — nested prose where table fits | low | no |
| 41d — legacy phase/step numbering | low | no |

## Check 44 — Sub-check naming symmetry

Sub-check letter suffixes must be contiguous starting at `a`. A file containing `Check Nb` or `Check Nc` without `Check Na` is an orphan — the `a` variant was never created or was removed, leaving a misleading gap. Same applies to any letter sequence gap (e.g., `a`, `b`, `d` missing `c`).

```bash
printf "=== Check 44: Sub-check naming symmetry ===\n"
found=0
while IFS= read -r f <&3; do  # timeout: 5000
    [ -f "$f" ] || continue
    # skip CLAUDE.md — cross-references check numbers, not defining sub-checks
    [ "$(basename "$f")" = "CLAUDE.md" ] && continue
    # -w: BSD grep whole-word (macOS — \b unsupported in ERE)
    while IFS= read -r entry; do
        num=$(printf '%s' "$entry" | grep -oE '[0-9]+')
        letter=$(printf '%s' "$entry" | grep -oE '[a-z]$')
        if ! grep -qw "Check ${num}a" "$f"; then
            printf "⚠ 44: %s — Check %s%s exists without Check %sa\n" "$f" "$num" "$letter" "$num"
            found=1
        fi
    done < <(grep -oE 'Check [0-9]+[b-z]' "$f" 2>/dev/null | sort -u)
done 3< <(find .claude plugins -name "*.md" ! -name "README.md" 2>/dev/null | sort)
[ "$found" -eq 0 ] && printf "✓: Check 44 — sub-check naming symmetric across all files\n"
```

**Severity**: low — gap in sub-check labeling. No runtime impact; misleads readers into expecting a missing variant.

| Sub-check | Pattern | Severity | Auto-fix |
| -- | -- | -- | -- |
| 44 — orphan letter suffix | `Check Nb` in file without `Check Na` | low | no — rename or add missing variant |

## Check 45 — Policy-sibling marker symmetry (reference-graph completeness)

Some policies (safety rules, scoping rules, format conventions) are **restated in prose** across multiple files instead of cross-referenced, because each consumer needs the rule inline in its own reading context. A restated copy has no structural link back to its siblings, so refining the policy in one location can silently leave others stating a stale version — grep-for-violations doesn't catch a file that correctly states an *old* rule. `plugins/CLAUDE.md §Policy Duplication Marker` requires a `<!-- policy-sibling: path1, path2, ... -->` comment in every copy, listing every other file stating the same policy. This check verifies that declared graph is complete and symmetric — it does not (cannot, mechanically) verify the restated *content* itself stays in sync; that judgment call is `foundry:curator`'s reference-graph trace (see curator `<workflow>` step on policy edits).

Two failure modes:

- **45-BROKEN**: marker lists a sibling path that doesn't exist on disk (stale — file renamed/deleted, marker not updated)
- **45-ASYMMETRIC**: file A's marker lists file B as a sibling, but B has no `policy-sibling` marker pointing back — B was never updated to know it's part of the group (exactly how `git-commit.md` was missed before this check existed)

```bash
printf "=== Check 45: Policy-sibling marker symmetry ===\n"
OUT=""
for f in $(grep -rl "<!-- policy-sibling:" .claude plugins --include="*.md" 2>/dev/null); do  # timeout: 10000
    siblings=$(grep -o '<!-- policy-sibling:[^—]*' "$f" | head -1 | sed 's/<!-- policy-sibling://')
    for sib in $(printf '%s' "$siblings" | tr ',' '\n' | awk '{print $1}' | grep -E '/.*\.md$'); do
        if [ ! -f "$sib" ]; then
            OUT="${OUT}⚠ 45-BROKEN: $f — policy-sibling lists missing file: $sib\n"
            continue
        fi
        grep -q "<!-- policy-sibling:" "$sib" || OUT="${OUT}⚠ 45-ASYMMETRIC: $f — declares sibling $sib, but that file has no policy-sibling marker back\n"
    done
done
if [ -n "$OUT" ]; then printf "$OUT"; else printf "✓: Check 45 — no unsynced policy-sibling markers found\n"; fi
```

Extraction anchors on the literal `<!-- policy-sibling:` prefix (not a bare grep for the word) so prose documenting the convention — like this file's own explanation above, or an example snippet — never self-matches; the sibling-token filter (`/.*\.md$`) additionally drops non-path fragments (placeholder text, trailing rationale words) that survive the comma split. No array syntax (`read -ra`, `mapfile`) — Claude Code's Bash tool runs under the user's login shell, which is `zsh` on macOS by default, and zsh's `read` does not support bash's `-a` flag; plain `for x in $(...)` word-splitting is portable to both.

**Severity**:

- `45-BROKEN` — **high** — sibling reference points nowhere; anyone following it to propagate a fix finds nothing
- `45-ASYMMETRIC` — **medium** — one-directional link; the group is incomplete, next refinement likely repeats the git-commit.md miss

Fix: add the missing `policy-sibling` marker to the un-listed file (45-ASYMMETRIC), or correct/remove the stale path (45-BROKEN). Both directions must resolve — A→B requires B→A.

| Sub-check | Pattern | Severity | Auto-fix |
| -- | -- | -- | -- |
| 45-BROKEN — dangling sibling path | listed path does not exist on disk | high | no — fix or remove path |
| 45-ASYMMETRIC — one-directional link | B listed by A but B has no marker back | medium | no — add reciprocal marker to B |
