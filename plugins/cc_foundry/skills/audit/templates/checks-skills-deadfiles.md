# Skill Checks — Dead Files (32)

> Scan-root convention for every bash block below: `checks-skills.md` §Scan-root derivation.

## Check 32 — Dead file detection

Surfaces skill subdirectory files and rule files that exist on disk but are never loaded at runtime — accumulated from past iterations where references were removed but files were not.

### Sub-check 32a — Dead mode files

Mode files in `*/skills/*/modes/` that are not referenced from the parent skill's `SKILL.md` are never executed. They create maintenance confusion and may contain outdated logic that silently diverges from the live mode.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
[ "$LOCAL_MODE" = "true" ] && _ROOT="plugins" || _ROOT=".claude"
printf "=== Check 32a: Dead mode files ===\n"
found=0
while IFS= read -r mode_file; do  # timeout: 5000
    skill_dir="$(dirname "$(dirname "$mode_file")")"
    [ -f "$skill_dir/SKILL.md" ] || continue
    mode_name=$(basename "$mode_file")
    hit=0
    # dispatch can be multi-level: a mode may be loaded from a sibling mode file, not only SKILL.md
    while IFS= read -r consumer; do
        # a shared file's own `<!-- file: -->` header names it — self-match is not a reference
        [ "$consumer" = "$mode_file" ] && continue
        /usr/bin/grep -qF "$mode_name" "$consumer" 2>/dev/null && { hit=1; break; }
    done < <(find "$skill_dir" \( -name "SKILL.md" -o -path "*/modes/*.md" \) 2>/dev/null | sort)
    if [ "$hit" -eq 0 ]; then
        printf "⚠ 32a: %s — not referenced in %s/SKILL.md or any sibling modes/*.md\n" "$mode_file" "$skill_dir"
        found=1
    fi
done < <(find "$_ROOT" -path "*/skills/*/modes/*.md" 2>/dev/null | sort)
[ "$found" -eq 0 ] && printf "✓: Check 32a — all mode files referenced in SKILL.md or a sibling mode file\n"
```

Severity: **medium** — dead mode file = unreachable code; may diverge silently from live workflow. Auto-fix: delete the file, or add a reference in SKILL.md if omission was accidental.

### Sub-check 32b — Dead template files

Template files in `*/skills/*/templates/` not referenced from the parent `SKILL.md` are never injected into prompts.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
[ "$LOCAL_MODE" = "true" ] && _ROOT="plugins" || _ROOT=".claude"
printf "=== Check 32b: Dead template files ===\n"
found=0
while IFS= read -r tpl_file; do  # timeout: 5000
    skill_dir="$(dirname "$(dirname "$tpl_file")")"
    [ -f "$skill_dir/SKILL.md" ] || continue
    tpl_name=$(basename "$tpl_file")
    hit=0
    # templates are routinely loaded from a mode file, not SKILL.md (audit/modes/steps-4-5-7.md loads every checks-*.md)
    while IFS= read -r consumer; do
        [ "$consumer" = "$tpl_file" ] && continue
        /usr/bin/grep -qF "$tpl_name" "$consumer" 2>/dev/null && { hit=1; break; }
    done < <(find "$skill_dir" \( -name "SKILL.md" -o -path "*/modes/*.md" \) 2>/dev/null | sort)
    if [ "$hit" -eq 0 ]; then
        printf "⚠ 32b: %s — not referenced in %s/SKILL.md or any sibling modes/*.md\n" "$tpl_file" "$skill_dir"
        found=1
    fi
done < <(find "$_ROOT" -path "*/skills/*/templates/*" -type f 2>/dev/null | sort)
[ "$found" -eq 0 ] && printf "✓: Check 32b — all template files referenced in SKILL.md or a mode file\n"
```

Severity: **low** — templates may be referenced indirectly via agent spawn prompts that mention the filename inline; human review required before deletion. Auto-fix: delete if confirmed unused; no auto-delete.

### Sub-check 32c — Dead rule files (paths: matches no project files)

Rule files with `paths:` frontmatter that match no existing project files are never applied. Rules without `paths:` (global rules) are always active — skip those.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
[ "$LOCAL_MODE" = "true" ] && _ROOT="plugins" || _ROOT=".claude"
printf "=== Check 32c: Dead rule files ===\n"
found=0
while IFS= read -r rule_file; do  # timeout: 5000
    [ -f "$rule_file" ] || continue
    paths_block=$(awk '/^---$/{c++} c==1{print} c==2{exit}' "$rule_file" 2>/dev/null | awk '/^paths:/{p=1;next} p && /^[^ ]/{p=0} p{print}')
    [ -z "$paths_block" ] && continue  # no paths: — global rule, always active, skip
    matched=0
    while IFS= read -r pat_line; do
        pat=$(echo "$pat_line" | sed "s/^ *- *'//;s/'$//;s/^ *- *//")
        [ -z "$pat" ] && continue
        found_file=$(find . -path "./$pat" -not -path "./.git/*" 2>/dev/null | head -1)
        [ -n "$found_file" ] && { matched=1; break; }
    done <<< "$paths_block"
    if [ "$matched" -eq 0 ]; then
        printf "⚠ 32c: %s — paths: patterns match no project files (rule never applied)\n" "$rule_file"
        found=1
    fi
done < <(find "$_ROOT" -path "*/rules/*.md" 2>/dev/null | sort)
[ "$found" -eq 0 ] && printf "✓: Check 32c — all scoped rules match at least one project file\n"
```

Severity: **medium** — rule with non-matching paths is never applied; may represent outdated scope (e.g., `src/**/*.py` when project no longer has Python files). Note: false positives possible if project files are in a non-standard location or generated at runtime. Human review before deletion. Auto-fix: remove `paths:` to make the rule global, or delete the file if rule is obsolete.

### Sub-check 32d — Orphaned bin/ scripts

`bin/` scripts existing in the plugin source tree but not referenced by any `.md` file in that plugin (SKILL.md, agents, rules, modes, templates, \_shared) are unreachable at runtime. Common cause: script authored as scaffolding but never wired into its caller SKILL.md.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
printf "=== Check 32d: Orphaned bin/ scripts ===\n"
if [ "$LOCAL_MODE" != "true" ]; then
    printf "✓: Check 32d skipped in non-local mode (no plugin source tree)\n"
else
    python "${CLAUDE_PLUGIN_ROOT}/bin/check_orphaned_bin.py" --plugins-dir plugins  # timeout: 10000
fi
```

Severity: **high** — orphaned script = either dead code or incomplete extraction; both are runtime gaps, not stylistic noise. An extraction that creates a bin/ script without wiring it into the caller leaves the inline twin active and the new script unreachable. Auto-fix: no — each category requires human judgment: (a) zero plausible consumer: confirm no in-progress branch before deleting; (b) cross-plugin consumer: search other plugins for the basename — if found, add `<!-- file: <basename> — consumers: <plugin> skills/<name> -->` doc header; (c) extraction started but wire-in skipped: identify the correct consumer SKILL.md and replace inline twin with bin/ invocation.

> Note: search covers the entire plugins tree — cross-plugin callers are found correctly. False negatives possible only if the caller references the script by a dynamic path or alias that does not include the basename.

| Sub-check | Target | Condition | Severity | Auto-fix |
| -- | -- | -- | -- | -- |
| 32a — dead mode file | `*/modes/*.md` | file exists but not referenced in parent SKILL.md | medium | delete file or add reference |
| 32b — dead template file | `*/templates/*` | file exists but not referenced in parent SKILL.md | low | human review — may be indirect ref |
| 32c — dead rule file | `*/rules/*.md` | `paths:` set but matches no project files | medium | remove `paths:` or delete file |
| 32d — orphaned bin/ script | `plugins/*/bin/*.py`, `*.sh` | script not referenced in any plugin .md file | high | yes — see severity guidance above |
| 32e — bin/ script cross-similarity | `plugins/*/bin/*.py` | ≥ 2 scripts with structural similarity ≥ 0.8 after normalization | medium | no — semantic review required |

### Sub-check 32e — Cross-similarity between bin/ scripts

bin/ scripts sharing structural patterns (≥ 0.8 similarity after normalizing identifiers and string literals) across or within plugins are merge candidates — one parametrized script replaces both, reducing duplicated maintenance surface. Common case: per-plugin helpers differing only in a path constant, plugin name, or threshold.

Skip in non-local mode (no source tree). Skip when fewer than 2 non-private bin/ Python scripts exist.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
printf "=== Check 32e: bin/ script cross-similarity ===\n"
if [ "$LOCAL_MODE" != "true" ]; then
    printf "✓: Check 32e skipped in non-local mode (no plugin source tree)\n"
else
    # newline-delimited scalar, not array — mapfile has no zsh equivalent (see checks-shared Check 45)
    _C32E_SCRIPTS=$(find plugins -path '*/bin/*.py' -not -name '_*.py' 2>/dev/null | sort)  # timeout: 5000
    _C32E_N=$(printf '%s' "$_C32E_SCRIPTS" | grep -c '^')
    if [ "$_C32E_N" -lt 2 ]; then
        printf "✓: Check 32e — fewer than 2 bin/ scripts, skip\n"
    else
        printf "⚙ Check 32e — %s bin/ scripts found; delegating similarity analysis to foundry:curator\n" "$_C32E_N"
    fi
fi
```

**Delegation prompt** (when `_C32E_N ≥ 2`): spawn foundry:curator with the `_C32E_SCRIPTS` file list and this instruction:

> "Scan these bin/ Python scripts for cross-similarity. For each pair: strip docstrings and inline comments → normalize variable names to `<VAR>` → normalize string literals and path constants to `<STR>` → compare AST-level structure. Report pairs with structural similarity ≥ 0.8. For each candidate pair: script names, plugin origin, similarity score, what differs (constant, path, plugin name, threshold), lines saved by merge, and suggested parametrization (e.g. `--plugin-root` flag, `--output-dir` arg, or move shared logic to `_shared/` helper). Skip pairs serving clearly different semantic roles despite structural overlap. Skip pairs marked `# audit-skip: resilience-replication` in their module docstring (intentional per-plugin independence)."

Severity: **medium** — near-duplicate scripts = duplicated maintenance; a bug fix in one copy likely missed in the other. Auto-fix: no — merge requires semantic review of both scripts and all callers; confirm no behavioral difference before merging.

## Check 32f — Mode-file body shadowed in SKILL.md (extraction integrity)

Detects the "extraction done but inline twin survived" topology: `modes/<name>.md` is referenced from `SKILL.md` AND its body is also present inline in `SKILL.md`. Complements Check 32a (which catches unreferenced mode files — the inverse polarity).

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/check_skill_contracts.py" --check 32f $( [ "$LOCAL_MODE" = "true" ] && echo "--local" )  # timeout: 30000
```

Severity: **medium** — inline twin diverges silently from canonical mode file on every future edit. Auto-fix: delete inline block from SKILL.md; replace with bash+read pattern matching other modes.

## Check 32g — Self-confessed manual sync markers

Detects explicit sync instructions in plugin `.md` files (`SYNC:`, `lock-step`, `keep both`, `duplicated from`, `mirror this in`, `keep.*copies.*sync`). Each marker = author admitted manual sync required = extraction not yet done.

```bash
printf "=== Check 32g: self-confessed sync markers ===\n"
_C32G_HITS=$(grep -rn \
    -e 'SYNC:' \
    -e 'lock-step' \
    -e 'lockstep' \
    -e 'keep both' \
    -e 'keep.*copies.*sync' \
    -e 'duplicated from' \
    -e 'mirror this in' \
    plugins/*/agents/*.md plugins/*/skills/*/SKILL.md plugins/*/skills/*/modes/*.md \
    plugins/*/rules/*.md 2>/dev/null | grep -v '# audit-skip:')
if [ -n "$_C32G_HITS" ]; then
    echo "$_C32G_HITS" | while IFS= read -r hit; do
        printf "⚠ 32g [medium] %s — self-confessed manual sync; extract canonical source or delete duplicate\n" "$hit"
    done
else
    printf "✓: Check 32g — no sync markers found\n"
fi  # timeout: 10000
```

Severity: **medium** — self-confessed sync = guaranteed future drift. Auto-fix: run `/distill memory` or extract to `modes/` + replace inline block with bash+read pattern.
