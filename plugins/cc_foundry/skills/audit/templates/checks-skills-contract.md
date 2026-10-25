# Skill Checks — Skill Contract (24, 31, 38)

> Scan-root convention for every bash block below: `checks-skills.md` §Scan-root derivation.

## Check 24 — Skill sequence compatibility

Skill `<notes>` and `<workflow>` sections often document multi-skill chains (e.g., `→ /audit`, `suggested next: /brainstorm breakdown <file>`). Check verifies documented sequences internally consistent:

- **24a (target existence)**: every skill referenced in documented chain exists on disk — root skills under `.claude/skills/<name>/`, plugin skills under `plugins/<plugin>/skills/<skill>/`
- **24b (argument plausibility)**: when suggestion includes explicit argument (e.g., `→ /audit fix`), that argument must appear as substring in target skill's `argument-hint:` frontmatter (case-insensitive)

**Step 1 — Extract sequence references**:

Scan three sources for documented chains:

1. **Skill files**: Grep (pattern `→.*` + backtick + `/[a-z]|suggest.*` + backtick + `/[a-z]|run.*after.*` + backtick + `/[a-z]`, glob `skills/*/SKILL.md`, path `.claude/`, output mode `content`)
2. **Agent files**: same Grep on `agents/*.md` (path `.claude/`)
3. **README files**: Grep same pattern in `README.md` (project root), `plugins/*/README.md`, `.claude/README.md` — README sequence tables are canonical workflow chain documentation; must be consistent with what is installed

Filter out:

- Lines starting with `#` (comments)
- Lines containing `e.g.` or `for example` (illustrative, not directive)
- Lines whose surrounding context describes what skill does rather than "run next" directive

Collect all unique (source-file, skill-reference, trailing-argument) triples. README-sourced sequences held to same validity standard as skill-sourced ones: broken README sequence = **high** (user-facing workflow documentation).

**Step 2 — Resolve each reference (Check 24a)**:

| Reference form | Resolution |
| -- | -- |
| `/name` | Glob `.claude/skills/name/SKILL.md` — must exist |
| `/plugin:name` | Glob `plugins/plugin/skills/name/SKILL.md` — must exist; if no `plugins/` dir, note "installed plugin — cannot verify statically" and skip |

Missing target → **[high]**: `Sequence reference /<name> in <file> resolves to no installed skill`

**Step 3 — Argument plausibility (Check 24b)**:

For references with trailing argument token (e.g., `--adversarial` in `/audit --adversarial`, `breakdown` in `/brainstorm breakdown`):

1. Read target skill's frontmatter `argument-hint:` (Glob-resolved path, first 5 lines)
2. If argument token does NOT appear as case-insensitive substring of `argument-hint` → **[medium]**: `Sequence argument '<arg>' absent from /<name> argument-hint: '<hint>'`

**Step 4 — Cycle detection (Check 24c)**:

Build directed graph from (source-file, skill-reference) pairs collected in Step 1. Walk all paths from each node; flag back-edges (skill A → skill B → … → skill A).

→ Any cycle found: **[high] 24c**: `Cycle: <A> → <B> → … → <A>` — document full cycle path; do not auto-fix; resolution requires removing or redirecting one chain edge.

**Report only** — no auto-fix; sequence intent requires human judgment.

| Sub-check | Severity | Auto-fix |
| -- | -- | -- |
| 24a — target skill not on disk | high | no |
| 24b — argument absent from argument-hint | medium | no |
| 24c — directed cycle in follow-up chain | high | no |

## Check 31 — Skill tool call vs allowed-tools consistency

For each SKILL.md, verify every **gating or dispatch tool** called in workflow body is declared in `allowed-tools:` frontmatter. Runtime enforces `allowed-tools` — undeclared tool calls blocked silently; entire workflow phase fails with no error message.

**High-risk tools** (absence breaks entire workflow phases, not just individual steps):

| Tool | Consequence if absent from `allowed-tools` |
| -- | -- |
| `Skill` | Follow-up gate never dispatches target skill; user's selection silently dropped |
| `AskUserQuestion` | Skill falls back to prose questions (violates communication.md; interactive gates broken) |
| `Agent` | Sub-agent spawns blocked; orchestration phase fails silently |

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
[ "$LOCAL_MODE" = "true" ] && _ROOT="plugins" || _ROOT=".claude"
found=0
# process substitution, not pipe — piped while runs in subshell, found wouldn't survive
while IFS= read -r f; do  # timeout: 5000
    [ -f "$f" ] || continue
    skill_name=$(basename "$(dirname "$f")")
    allowed=$(awk '/^---$/{c++} c==1{print} c==2{exit}' "$f" 2>/dev/null | grep '^allowed-tools:' | sed 's/allowed-tools:[[:space:]]*//')
    [ -z "$allowed" ] && continue
    body=$(awk '/^---$/{c++} c>=2{print}' "$f" 2>/dev/null)
    for tool in Skill AskUserQuestion Agent; do
        if echo "$body" | grep -qE "\b${tool}\(" 2>/dev/null; then
            if ! echo "$allowed" | grep -qw "$tool"; then
                printf "! BREAKING skills/%s: body calls %s() but '%s' absent from allowed-tools\n" "$skill_name" "$tool" "$tool"
                printf "  fix: add '%s' to allowed-tools: in %s\n" "$tool" "$f"
                found=1
            fi
        fi
    done
done < <(find "$_ROOT" -path "*skills/*/SKILL.md" 2>/dev/null | sort)
[ "$found" -eq 0 ] && printf "✓: Check 31 — all gating tool calls covered by allowed-tools\n"  # timeout: 5000
```

Severity: **critical** — blocked gating tool = entire workflow phase silently broken at runtime.

Auto-fix: append missing tool name to `allowed-tools:` frontmatter line.

| Sub-check | Condition | Severity | Auto-fix |
| -- | -- | -- | -- |
| 31 — tool-body mismatch | body calls `Skill()`, `AskUserQuestion()`, or `Agent()` but tool absent from `allowed-tools` | critical | yes — add to frontmatter |

### Sub-check 31a — Skill frontmatter completeness

Verify required frontmatter fields present in every SKILL.md. Missing fields cause undocumented default behavior or miscategorized routing.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
[ "$LOCAL_MODE" = "true" ] && _ROOT="plugins" || _ROOT=".claude"
printf "=== Check 31a: Frontmatter completeness ===\n"
found=0
while IFS= read -r f; do  # timeout: 5000
    [ -f "$f" ] || continue
    skill=$(basename "$(dirname "$f")")
    fm=$(awk '/^---$/{c++} c==1{print} c==2{exit}' "$f" 2>/dev/null)
    echo "$fm" | grep -q '^effort:' || {
        printf "⚠ 31a: %s — missing effort: field (required; no default)\n" "$skill"
        found=1
    }
    # when_to_use: is valid (appended to description in the listing; both share one 1,536-char cap)
    wtu=$(echo "$fm" | sed -n 's/^when_to_use:[[:space:]]*//p')
    desc=$(echo "$fm" | sed -n 's/^description:[[:space:]]*//p')
    [ -n "$wtu" ] && [ $(( ${#wtu} + ${#desc} )) -gt 1536 ] && {
        printf "⚠ 31a: %s — description + when_to_use exceed the 1,536-char listing cap\n" "$skill"
        found=1
    }
    [ -n "$wtu" ] && echo "$desc" | grep -q 'TRIGGER' && echo "$wtu" | grep -q 'TRIGGER' && {
        printf "⚠ 31a: %s — when_to_use repeats TRIGGER/SKIP text already in description\n" "$skill"
        found=1
    }
done < <(find "$_ROOT" -path "*skills/*/SKILL.md" 2>/dev/null | sort)
[ "$found" -eq 0 ] && printf "✓: Check 31a — frontmatter complete across all skills\n"
```

Severity: **medium** for `effort:` (no default documented); **low** for `when_to_use:` cap overflow or TRIGGER/SKIP duplication (the field itself is valid).

| Sub-check | Field | Condition | Severity | Auto-fix |
| -- | -- | -- | -- | -- |
| 31 — tool-body mismatch | `allowed-tools` | body calls Skill/AskUserQuestion/Agent, not in frontmatter | critical | yes |
| 31a — effort missing | `effort:` | always required | medium | yes |
| 31a — when_to_use overflow/dup | `when_to_use:` | combined with `description:` over 1,536 chars, or repeats its TRIGGER/SKIP text | low | no |

## Check 38 — AskUserQuestion cap violation

`communication.md` §Interactive Questions hard-caps `AskUserQuestion` at 4 questions per call, and skills must not ask >4 questions across a single decision branch. Violations cause UX breakage (silent truncation or tool error).

Scan all SKILL.md files in scope. For each file, count the total number of `AskUserQuestion` occurrences. Apply the following heuristic:

- ≤4 calls → `✓` (likely safe; one branch per call is typical)
- 5–8 calls → flag **medium** — review that no single decision branch asks >4 sequentially
- > 8 calls → flag **high** — almost certainly some branch exceeds the cap

```bash
printf "=== Check 38: AskUserQuestion cap ===\n"
for f in $(find . -path "*skills/*/SKILL.md" 2>/dev/null | sort); do
    # grep -c prints 0 AND exits 1 on no match — || echo 0 double-fires; most files have none, so this hit the majority
    count=$(grep -c "AskUserQuestion" "$f" 2>/dev/null) || count=0
    if [ "$count" -gt 8 ]; then
        printf "C38-HIGH: %s AskUserQuestion calls in %s — review for >4-per-branch\n" "$count" "$f"
    elif [ "$count" -gt 4 ]; then
        printf "C38-MEDIUM: %s AskUserQuestion calls in %s — review for >4-per-branch\n" "$count" "$f"
    fi
done  # timeout: 5000
```

**Severity**: high (>8 calls) / medium (5–8 calls) — functional regression; gate prompts that exceed cap silently drop later questions. Fix: collapse related sub-questions into one `AskUserQuestion` call (max 4 options per call, max 4 calls per skill workflow branch).
