# Skill Checks — Skill Contract (24, 31, 38)

> Scan-root convention for every bash block below: `checks-skills.md` §Scan-root derivation.

## Check 24 — Skill sequence compatibility

Skill `<notes>` and `<workflow>` sections often document multi-skill chains (e.g., `→ /foundry:audit`, `suggested next: /foundry:brainstorm breakdown <file>`). Check verifies documented sequences internally consistent:

- **24a (target existence)**: every skill referenced in documented chain exists on disk — root skills under `.claude/skills/<name>/`, plugin skills under the root whose `.claude-plugin/plugin.json` declares the routing name (in `skills/` or `claude-skills/`)
- **24b (argument plausibility)**: when suggestion includes explicit argument (e.g., `→ /foundry:audit fix`), that argument must appear as substring in target skill's `argument-hint:` frontmatter (case-insensitive)

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
| `/plugin:name` | In local mode, parse `plugins/*/.claude-plugin/plugin.json` and match its JSON `name` to `plugin`; inspect that root's `skills/name/SKILL.md` and `claude-skills/name/SKILL.md`. Directory names are not routing names. Otherwise read the marketplace/plugin identity from `~/.claude/plugins/installed_plugins.json`, then resolve its active install with the audit plugin's own `bin/get_plugin_install_path.py <marketplace> <plugin-name>`, then inspect those same subdirectories. An unavailable install record is unverified, not a missing target. |

Missing target → **[high]**: `Sequence reference /<name> in <file> resolves to no installed skill`

**Step 3 — Argument plausibility (Check 24b)**:

For references with trailing argument token (e.g., `--adversarial` in `/foundry:audit --adversarial`, `breakdown` in `/foundry:brainstorm breakdown`):

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

For each Claude skill, scan its `SKILL.md` and child Markdown executed by that owner, excluding spawn-prompt templates (`templates/*prompt*.md`) and the audit skill's `templates/checks-*.md`. Verify every **gating or dispatch tool** called in workflow body is declared in `allowed-tools:` frontmatter. Runtime enforces `allowed-tools` — undeclared tool calls blocked silently; entire workflow phase fails with no error message.

**High-risk tools** (absence breaks entire workflow phases, not just individual steps):

| Tool | Consequence if absent from `allowed-tools` |
| -- | -- |
| `Skill` | Follow-up gate never dispatches target skill; user's selection silently dropped |
| `AskUserQuestion` | Skill falls back to prose questions (violates communication.md; interactive gates broken) |
| `Agent` | Sub-agent spawns blocked; orchestration phase fails silently |

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
found=0
# process substitution, not pipe — piped while runs in subshell, found wouldn't survive
while IFS= read -r f; do  # timeout: 5000
    [ -f "$f" ] || continue
    skill_name=$(basename "$(dirname "$f")")
    allowed=$(awk '/^---$/{c++} c==1{print} c==2{exit}' "$f" 2>/dev/null | grep '^allowed-tools:' | sed 's/allowed-tools:[[:space:]]*//')
    [ -z "$allowed" ] && continue
    owner_body=$(awk '/^---$/{c++} c>=2{print}' "$f" 2>/dev/null)
    body="$owner_body"
    while IFS= read -r child; do
        [ "$child" = "$f" ] && continue
        body="$body
$(cat "$child")"
    done < <(find "$(dirname "$f")" -type f -name '*.md' ! -path '*/templates/*prompt*.md' ! -path '*/audit/templates/checks-*.md' 2>/dev/null | sort)
    for tool in Skill AskUserQuestion Agent; do
        if echo "$body" | grep -qE "\b${tool}\(" 2>/dev/null; then
            if ! echo "$allowed" | grep -qw "$tool"; then
                printf "! BREAKING skills/%s: body calls %s() but '%s' absent from allowed-tools\n" "$skill_name" "$tool" "$tool"
                if echo "$owner_body" | grep -qE "\b${tool}\("; then
                    printf "  fix: add '%s' to allowed-tools: in %s\n" "$tool" "$f"
                else
                    printf "  review: child-only call; verify execution under owner before changing %s (auto-fix: no)\n" "$f"
                fi
                found=1
            fi
        fi
    done
done < <(
if [ "$LOCAL_MODE" = "true" ]; then
    while IFS= read -r manifest; do
        [ -f "$manifest" ] || continue
        plugin_root=$(dirname "$(dirname "$manifest")")
        find "$plugin_root/skills" "$plugin_root/claude-skills" -mindepth 2 -maxdepth 2 -name SKILL.md -type f 2>/dev/null
    done < <(find plugins -mindepth 3 -maxdepth 3 -type f -path '*/.claude-plugin/plugin.json' 2>/dev/null | sort)
else
    find .claude/skills -mindepth 2 -maxdepth 2 -name SKILL.md -type f 2>/dev/null
fi | sort
)
[ "$found" -eq 0 ] && printf "✓: Check 31 — all gating tool calls covered by allowed-tools\n"  # timeout: 5000
```

Severity: **critical** — blocked gating tool = entire workflow phase silently broken at runtime.

Auto-fix: append missing tool name to `allowed-tools:` frontmatter line only for calls in the owner's `SKILL.md` body. Child-only hits require execution-context review; auto-fix **no**.

| Sub-check | Condition | Severity | Auto-fix |
| -- | -- | -- | -- |
| 31 — tool-body mismatch | body calls `Skill()`, `AskUserQuestion()`, or `Agent()` but tool absent from `allowed-tools` | critical | yes for owner-body calls; no for child-only hits |

### Sub-check 31a — Skill frontmatter completeness

Review frontmatter in Claude skills only. Missing `effort:` is advisory: the effective default depends on the selected model and runtime configuration; verify that default before recommending an explicit override. Never auto-insert an effort value.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
printf "=== Check 31a: Frontmatter completeness ===\n"
found=0
while IFS= read -r f; do  # timeout: 5000
    [ -f "$f" ] || continue
    skill=$(basename "$(dirname "$f")")
    fm=$(awk '/^---$/{c++} c==1{print} c==2{exit}' "$f" 2>/dev/null)
    echo "$fm" | grep -q '^effort:' || {
        printf "31a-INFO: %s — effort omitted; verify selected model/runtime default before recommending an override\n" "$skill"
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
done < <(
if [ "$LOCAL_MODE" = "true" ]; then
    while IFS= read -r manifest; do
        [ -f "$manifest" ] || continue
        plugin_root=$(dirname "$(dirname "$manifest")")
        find "$plugin_root/skills" "$plugin_root/claude-skills" -mindepth 2 -maxdepth 2 -name SKILL.md -type f 2>/dev/null
    done < <(find plugins -mindepth 3 -maxdepth 3 -type f -path '*/.claude-plugin/plugin.json' 2>/dev/null | sort)
else
    find .claude/skills -mindepth 2 -maxdepth 2 -name SKILL.md -type f 2>/dev/null
fi | sort
)
[ "$found" -eq 0 ] && printf "✓: Check 31a — frontmatter complete across all skills\n"
```

Severity: **advisory** for omitted `effort:` (model/runtime default applies); **low** for `when_to_use:` cap overflow or TRIGGER/SKIP duplication (the field itself is valid).

| Sub-check | Field | Condition | Severity | Auto-fix |
| -- | -- | -- | -- | -- |
| 31 — tool-body mismatch | `allowed-tools` | body calls Skill/AskUserQuestion/Agent, not in frontmatter | critical | yes for owner-body calls; no for child-only hits |
| 31a — effort omitted | `effort:` | verify selected model/runtime default | advisory | no |
| 31a — when_to_use overflow/dup | `when_to_use:` | combined with `description:` over 1,536 chars, or repeats its TRIGGER/SKIP text | low | no |

## Check 38 — Interactive question limits

`communication.md` §Interactive Questions limits each `AskUserQuestion` call to **4 questions** and asks related sub-questions to be grouped into one option set. It does not cap total calls per skill or decision branch. Count actual questions within each call; inspect each reachable branch separately, including mode files, and distinguish mutually exclusive gates from sequential calls. A skill-wide occurrence count cannot establish a violation.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
printf "=== Check 38: Interactive question review candidates ===\n"
while IFS= read -r owner; do  # timeout: 5000
    while IFS= read -r f; do
        awk '
          BEGIN { tick = sprintf("%c", 96); open = "^(" tick tick tick tick "*|~~~~*)" }
          FNR == 1 { fence = ""; front = ($0 == "---") }
          front && FNR > 1 && /^---$/ { front = 0; next }
          front || /^allowed-tools:/ { next }
          {
            run = match($0, open) ? substr($0, 1, RLENGTH) : ""
            if (fence == "" && run != "") { fence = run; next }
            if (fence != "" && run != "" && substr(run, 1, 1) == substr(fence, 1, 1) && length(run) >= length(fence) && substr($0, length(run) + 1) ~ /^[[:space:]]*$/) { fence = ""; next }
            call = "AskUserQuestion[[:space:]]*\\("
            if (fence != "") {
              if ($0 !~ /^[[:space:]]*#/ && $0 ~ call) print FNR ":" $0
              next
            }
            rest = $0; delimiter = ""; span = ""; inline_call = 0
            while (match(rest, tick tick "*")) {
              if (delimiter != "") span = span substr(rest, 1, RSTART - 1)
              marker = substr(rest, RSTART, RLENGTH)
              if (delimiter == "") delimiter = marker
              else if (marker == delimiter) {
                if (span ~ call) inline_call = 1
                delimiter = ""; span = ""
              } else span = span marker
              rest = substr(rest, RSTART + RLENGTH)
            }
            if (inline_call) print FNR ":" $0
          }' "$f" 2>/dev/null | while IFS= read -r hit; do
            printf "38-REVIEW: owner=%s source=%s:%s — inspect questions per call and reachable branch\n" "$owner" "$f" "$hit"
        done
    done < <(find "$(dirname "$owner")" -type f -name '*.md' 2>/dev/null | sort)
done < <(
if [ "$LOCAL_MODE" = "true" ]; then
    while IFS= read -r manifest; do
        [ -f "$manifest" ] || continue
        plugin_root=$(dirname "$(dirname "$manifest")")
        find "$plugin_root/skills" "$plugin_root/claude-skills" -mindepth 2 -maxdepth 2 -name SKILL.md -type f 2>/dev/null
    done < <(find plugins -mindepth 3 -maxdepth 3 -type f -path '*/.claude-plugin/plugin.json' 2>/dev/null | sort)
else
    find .claude/skills -mindepth 2 -maxdepth 2 -name SKILL.md -type f 2>/dev/null
fi | sort
)
```

The snippet collects fenced and inline call candidates, excluding frontmatter declarations and prose-only mentions; model review identifies real calls, counts each call's question objects and reports the source line, branch and observed count. Flag **medium** only when a real call contains more than 4 questions. Split that call into focused gates while retaining every required decision. Repeated calls alone are not findings.
