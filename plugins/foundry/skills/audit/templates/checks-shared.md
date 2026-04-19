**Re: Compress markdown to caveman format**

# Shared Checks (all scopes) — 17, 21, 4, 5, 9, 16, 15

______________________________________________________________________

## Check 12 — File length (context budget risk)

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

## Check 13 — Markdown heading hierarchy continuity

````bash
GRN='\033[0;32m'
YEL='\033[1;33m'
NC='\033[0m'
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
    printf "${GRN}✓${NC}: Check 13 — no heading hierarchy violations found\n"
fi
````

**Severity**: **medium** — heading jumps impair navigation. Fix: insert missing intermediate heading level, or demote/promote offending heading. **Report only** — never auto-fix.

______________________________________________________________________

## Check 14 — Orphaned follow-up references

Use Grep tool (pattern `` `/[a-z-]*` ``, glob `skills/*/SKILL.md`, path `.claude/`, output mode `content`) to find skill-name references; compare against disk inventory.

______________________________________________________________________

## Check 15 — Hardcoded user paths

Use Grep tool (pattern `/Users/|/home/`, glob `{agents/*.md,skills/*/SKILL.md}`, path `.claude/`, output mode `content`) to flag non-portable paths in agent and skill files. Run second Grep on `.claude/settings.json` with same pattern to catch absolute hook paths.

**Important**: run on every file regardless of prior critical/high findings — path portability orthogonal to other severity classes, must not deprioritize.

______________________________________________________________________

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

______________________________________________________________________

## Check 17 — Cross-file content duplication (>40% consecutive step overlap)

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

Via model reasoning, compare workflow body of each file against all others in its class. Per pair:

1. Count steps: N_A and N_B
2. Find longest consecutive run of substantially similar steps: N_run
3. Compute run fraction: `max(N_run / N_A, N_run / N_B)`
4. Flag if run fraction ≥ 0.4 (40%)

Scattered similarity doesn't count — only contiguous block triggers. **Severity**: **medium** — report only, never auto-fix.

For flagged agent pairs, name canonical owner of duplicated content. If no clear owner because both files describe same role, route pair to Check 20 as `merge-prune` candidate instead of leaving duplication ambiguous.

______________________________________________________________________

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

______________________________________________________________________

## Check 25 — Implicit agent references (missing plugin prefix)

All agent dispatch calls must use fully-qualified plugin-prefixed form (`foundry:sw-engineer`, `oss:shepherd`, etc.). Bare names like `sw-engineer` ambiguous: rely on `~/.claude/agents/` symlinks being present, break if symlinks stale, missing, or pointing to wrong plugin.

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

```
[high] Implicit agent reference: subagent_type="<name>" in <file>
fix: use fully-qualified form, e.g. subagent_type="foundry:<name>"
```

**Report only** — no auto-fix; correct prefix depends on which plugin owns agent.

______________________________________________________________________

## Check 29 — LLM context minimality (verbosity)

Every token in agent, skill, rule file = inference cost on every invocation. Check each file semantically minimal — all information retained, zero redundant wording.

**Scan targets**: `.claude/agents/*.md`, `.claude/skills/*/SKILL.md`, `.claude/rules/*.md`.

Via model reasoning, apply four criteria per file:

**1 — Within-file repetition**: same rule or instruction appears in two sections. Sub-bullet fully restates parent with no additive content. Workflow step re-explains constraint already defined in preamble or `<notes>`.

**2 — Prose inflation**: filler preambles ("Note that", "It is important to", "Please be aware", "Keep in mind") — flag phrase; substantive content survives without it. Unconditional rule hedged with "might", "could potentially", "in some cases" where rule is absolute. Opening sentence of section paraphrases heading with no additive content.

**3 — Restatement of obvious consequence**: "Do X" immediately followed by "Failing to do X causes Y" where Y self-evident from X alone.

**4 — Information gap test (mandatory before flagging any candidate)**: "If this text removed, can reader reconstruct from remaining content?" YES = safe to flag. NO = not a finding — content load-bearing even if verbose. Always skip: code blocks, inline examples (covered by Check 16), cross-reference tables, numbered lists where order carries meaning.

Per finding: location (section heading + approx line range) · pattern type (repetition / prose-inflation / obvious-consequence) · estimated token savings (small <20 / medium 20–80 / large >80) · proposed shorter form or "remove entirely".

**Severity**: **medium** — total savings >= medium across >= 2 distinct locations. **low** — isolated small savings only. **Report only** — never auto-fix; minimization risks removing load-bearing nuance.

______________________________________________________________________

## Check 26 — Symbol and shortcut consistency

Three sub-checks for within-file consistency of emoji symbols, slash-command notation, legend alignment.

**26a — Emoji/symbol consistency within files**

Per agent or skill file, extract lines with emoji and annotated concept label. Group by concept. Flag concepts with more than one distinct emoji in same file.

````bash
printf "=== Check 26a: Emoji/symbol consistency ===\n"
for f in .claude/agents/*.md .claude/skills/*/SKILL.md; do # timeout: 5000
    [ -f "$f" ] || continue
    # Print filename + any line containing common status emoji (skip code fences)
    awk '/^```/{skip=!skip} !skip && /[🔴🟡🟢🔵⛔✅❌⚠️💭▶️🔗🔹🔸🚫]/{print FILENAME": "NR": "$0}' "$f" 2>/dev/null
done
````

Via model reasoning, identify concept labels (e.g., "closed", "open", "active focus", "merged") appearing with two+ distinct symbols in same file. Example: file marks branch 🔴 (closed) in one section and ⛔ closed in another = violation.

Flag: `[medium] Inconsistent symbol for "<concept>" in <file>: <symbol-A> (line N) vs <symbol-B> (line M)`

**26b — Slash command notation consistency**

Directive references to other skills (e.g., "run → /audit fix") must use `/name` form. Prose mentions (e.g., "the audit skill") may omit slash. Flag files mixing `` `/name` `` and `` `name` `` in same directive context.

```bash
printf "=== Check 26b: Slash command notation ===\n"
for f in .claude/agents/*.md .claude/skills/*/SKILL.md; do # timeout: 5000
    [ -f "$f" ] || continue
    # Collect directive-looking references in both forms
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
| --- | --- | --- |
| 26a — same concept, different symbols | medium | no |
| 26b — directive notation mixed `/name` vs `name` | low | no |
| 26c — body symbol contradicts legend | medium | no |
