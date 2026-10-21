# Skill Checks — Cross-Plugin (27, 28)

> Scan-root convention for every bash block below: `checks-skills.md` §Scan-root derivation.

## Check 27 — Cross-plugin shared-file reference integrity

**Policy — every plugin's `_shared` is its own** (`plugins/CLAUDE.md` §Self-Contained \_shared). A plugin must resolve `skills/_shared` through its **own** resolver and read only files it **ships itself**. Two forbidden shapes:

- **Global path** — `$HOME/.claude/skills/_shared/...` or bare `.claude/skills/_shared/...`. No such path exists any more: `/foundry:setup` symlinks only `rules/*.md` and `TEAM_PROTOCOL.md`, and purges any leftover `~/.claude/skills/` link. A dir with `SKILL.md` there would register as a user-level skill and shadow Claude Code's bundled skill of that name.
- **Sibling reach-in** — resolving another plugin's tree (`resolve_shared_path.py foundry` from a non-foundry plugin, `dev_shared_resolve.py --foundry`, `$_FOUNDRY_SHARED`, `$_FOUNDRY_BIN`, or a literal `plugins/cc_<other>/` path). Content genuinely needed by two plugins is **duplicated**, not borrowed: add a `MANIFEST` entry in `bin/propagate_shared.py` so the copies stay byte-identical.

**Sanctioned exception — optional-provider contract**: `resolve_shared_path.py codemap-py claude-skills/_shared` inside a `codemap-gates.md` / `codemap-context.md` wrapper is **not** a finding when the block also gates on `command -v codemap-py` and prints a fallback line on absence. Second instance: `resolve_shared_path.py foundry bin` executes the provider's `compact_gate.py`. Consumers reach it through the propagated `bin/compaction_boundary.py` (Python, outside this shell scan; it prints the consumer's own fallback `compact-gate: continue (foundry gate unavailable)` when the provider or gate is missing), or inline in the `compaction-contract.md` Boundary block, with `[ -f "$_GD/compact_gate.py" ]` on the resolver call's same line and the same fallback echo. The feature is a no-op without its provider; both instances retain the resolution and fallback conditions (`plugins/CLAUDE.md` §Self-Contained `_shared`). Flag it as 27b if the gate or fallback is missing or the call falls outside these instances.

**Special antipattern — foundry-dependency catch-22**: a borrowed file that describes how to degrade *without* foundry (e.g. `agent-resolution.md` listing `general-purpose` substitutes) is **critical** — the instructions for surviving foundry's absence are reachable only when foundry is present.

**Step 1 — Global `_shared` paths** (any plugin, foundry included):

```bash
grep -rn '\.claude/skills/_shared/' plugins/*/skills/ plugins/*/claude-skills/ plugins/*/*.md 2>/dev/null  # timeout: 5000
```

- Any match → **[high] 27a**: `<plugin>/<file>: reads _shared via global .claude/skills/ path — resolve own plugin's skills/_shared instead`

**Step 2 — Sibling reach-in**:

```bash
grep -rn 'resolve_shared_path\.py" [a-z-]*\|"\$_[A-Z]*" [a-z-]* \(bin\|skills/_shared\)\|--foundry\|_FOUNDRY_SHARED\|_FOUNDRY_BIN\|plugins/cc_[a-z]*}\?/\(bin\|skills/_shared\)' plugins/*/skills/ plugins/*/claude-skills/ 2>/dev/null | grep -v '/audit/templates/checks-skills[a-z-]*\.md:' | awk -F/ '{ own=$2; l=$0; keep=0; while (match(l,/plugins\/cc_[a-z]*/)) { t=substr(l,RSTART+8,RLENGTH-8); if (t!="cc_" && t!=own) keep=1; l=substr(l,RSTART+RLENGTH) }; l=$0; while (match(l,/(resolve_shared_path\.py" [a-z-]*( (bin|skills\/_shared))?|\$_[A-Z]*" [a-z-]* (bin|skills\/_shared))/)) { split(substr(l,RSTART,RLENGTH),a," "); t=a[2]; if ("cc_" t!=own && t!="codemap-py" && !(t=="foundry" && a[3]=="bin" && $0 ~ /\[ -f "\$_GD\/compact_gate\.py" \]/ && index($0,"python \"$_GD/compact_gate.py\"") && index($0,"echo \"compact-gate: continue (foundry gate unavailable)\""))) keep=1; if (t=="codemap-py" && $0 !~ /command -v codemap-py/) keep=1; l=substr(l,RSTART+RLENGTH) }; if (own!="cc_foundry" && $0 ~ /_FOUNDRY_|--foundry/) keep=1; if (keep) print }'  # timeout: 5000
```

The `}\?` admits the `${CLAUDE_PLUGIN_ROOT:-plugins/cc_<x>}/bin/…` idiom as well as a bare `plugins/cc_<x>/bin/` literal; the resolver patterns accept a double-quoted path or `"$_RS" foundry bin` variable call (an unquoted `resolve_shared_path.py foundry` in prose is not a call).

- The `awk` derives the requested plugin and subdir from literal and variable resolver calls. It drops rows whose every `plugins/cc_<x>` token is the file's own plugin and whose every resolver token is its own name or a guarded optional provider (both loops walk the whole line — a second, foreign token after an own-plugin one is still caught).
- `_FOUNDRY_*` / `--foundry` count as reach-ins only outside `cc_foundry`, so foundry's own skills stay in scope for every other shape.
- A `plugins/cc_` token with no plugin letters after it (`cc_<x>`, `cc_*`) names no sibling and is ignored (`t!="cc_"` — the token keeps its `cc_` prefix).
- A `codemap-py` resolver call is dropped only when `command -v codemap-py` gates it on the same line, so an ungated provider read still surfaces for the sanctioned-exception judgement.
- A `foundry bin` resolver call is dropped only when the same line contains `[ -f "$_GD/compact_gate.py" ]`, `python "$_GD/compact_gate.py"`, and `echo "compact-gate: continue (foundry gate unavailable)"`; every other foreign foundry call still surfaces.
- The `checks-skills*.md` templates are excluded from this scan because their prose quotes every forbidden shape — ~410 raw rows become the handful worth judging (POSIX awk, no GNU back-references).

Ignore a plugin's own resolver call (`resolve_shared_path.py" oss` inside `cc_oss`) and its own bare-path fallback (e.g. `plugins/cc_oss/...` inside `cc_oss`) — that is the sanctioned last-resort tier — and the gated `codemap-py` provider read or foundry compaction gate under the sanctioned exception above. Every plugin now ships `resolve_shared_path.py`, so a reach-in can name any sibling, not only foundry; the pattern catches the plugin token, the auditor judges it. Everything else:

- Match → **[high] 27b**: `<plugin>/<skill>: reads <file> from cc_<other>'s _shared or bin — ship a copy in this plugin's own tree and add a propagate_shared.py MANIFEST entry`

**Step 3 — Catch-22 upgrade**:

For each 27b match, inspect the borrowed filename and surrounding context for degraded-mode signals (keywords: `fallback`, `without foundry`, `agent-resolution`, `general-purpose`, `not installed`).

- Match → upgrade to **[critical] 27c**: `<plugin>/<skill>: fallback file <name> is only reachable via foundry — catch-22`
- No match → keep as **[high] 27b**

**Step 4 — Orphaned own-plugin \_shared files**:

```bash
for f in plugins/*/*skills/_shared/*; do
    plugin=$(echo "$f" | cut -d/ -f2)
    fname=$(basename "$f")
    grep -rq "$fname" "plugins/$plugin/" 2>/dev/null || echo "unreferenced: $f"
done
```

Own-plugin `_shared/` IS reachable at runtime (each plugin's resolver finds it), so presence there is correct — but a file no consumer names is dead weight a grep-based sweep will eventually delete (see `plugins/CLAUDE.md` §Shared File Authoring Rule).

- Unreferenced → **[low] 27d**: `<plugin>: _shared/<file> named by no consumer — add a `# loads:` reference or delete`
- Exists in plugin-local `_shared/` but not referenced → **[low]**: unreachable dead file; suggest removal

**Report only** — no auto-fix; resolution requires deciding whether to inline content or move file to `foundry/_shared/`.

| Sub-check | Severity | Auto-fix |
| -- | -- | -- |
| 27a — global `.claude/skills/_shared/` path | high | no |
| 27b — sibling reach-in (another plugin's `_shared` or `bin`) | high | no |
| 27c — catch-22 (borrowed fallback file reachable only via foundry; upgraded 27b) | critical | no |
| 27d — orphaned own-plugin `_shared/` file (named by no consumer) | low | no |

## Check 28 — Cross-plugin agent dispatch fallback

Skills dispatching agents via `Agent(subagent_type="<plugin>:<name>", ...)` depend on that plugin being installed. When dispatched agent belongs to different plugin from skill's own plugin, and no fallback declared for absent-plugin case, skill fails at runtime. **Exempt**: `general-purpose` (built-in, always available); `codex:*` agents (conditional dispatch tracked by Check 7).

**Step 1 — Map skills to owning plugin:**

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
# needs plugin source tree — .claude/skills/ has no plugin-prefixed structure
if [ "$LOCAL_MODE" != "true" ]; then
    echo "[Check 28 Step 1] Skipped in non-local mode (no plugin source tree)"
else
    for f in plugins/*/*skills/*/SKILL.md; do
        plugin=$(echo "$f" | cut -d/ -f2)
        skill=$(echo "$f" | cut -d/ -f4)
        echo "$plugin $skill $f"
    done
fi
```

**Step 2 — Collect cross-plugin dispatches per skill:**

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
if [ "$LOCAL_MODE" != "true" ]; then
    echo "[Check 28 Step 2] Skipped in non-local mode (no plugin source tree)"
else
    grep -rn 'subagent_type' plugins/*/*skills/*/SKILL.md 2>/dev/null | grep -v '^Binary'  # timeout: 5000
fi
```

For each match: extract `(skill_file, dispatched_plugin, dispatched_agent)`. Dispatch is **cross-plugin** when `dispatched_plugin ≠ owning_plugin`. Build map: `skill_file → [cross-plugin agents]`.

Skip: any `general-purpose` dispatch and any `codex:*` dispatch.

**Step 3 — Verify fallback coverage:**

For each skill with one or more cross-plugin dispatches, read skill file and search for fallback declaration. Valid fallback is any of:

- Section heading matching `Agent Resolution`, `Fallback`, or `Plugin Check` (case-insensitive)
- Sentence containing cross-plugin agent name AND word from `{fallback, not installed, substitute, general-purpose, unavailable}` within 5 lines of each other
- Conditional dispatch block: `if not installed` or `plugin list.*grep.*<plugin>` followed by alternative

No fallback found → **[high] Check 28a**: `<plugin>/<skill>: dispatches <cross-plugin-agent> with no fallback for missing plugin`

**Step 4 — Completeness check:**

For each skill where fallback section exists: verify every cross-plugin agent dispatched by that skill is named within fallback block (bare name OR fully-qualified `plugin:name` form). Agent covered when name appears in fallback block.

Partially covered → **[medium] Check 28b**: `<plugin>/<skill>: fallback section present but does not cover <agent>`

**Report only** — fixing requires adding Agent Resolution section with fallback substitutes for each cross-plugin dependency; pattern in `develop:plan` (Agent Resolution table with `foundry agent | Fallback | Model | Role description prefix`) is reference implementation.

> **Related**: Check 25 (in `checks-shared.md`) covers bare-name dispatch (missing plugin prefix). Check 25 and Check 28 address different failure modes — run both.

| Sub-check | Condition | Severity | Auto-fix |
| -- | -- | -- | -- |
| 28a | no fallback for cross-plugin dispatch | high | no |
| 28b | fallback present but agent not covered | medium | no |

### Sub-check 28c — Cross-plugin prose references without availability guard

Skills may reference other plugins' skills in `<notes>`, follow-up chains, and prose documentation without runtime dispatch (no `Agent(subagent_type=...)` call). These prose references shown to users as runnable next-steps; if referenced plugin absent, command fails silently.

**Step — Scan for unguarded prose cross-plugin references**:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
printf "=== Check 28c: Cross-plugin prose refs ===\n"
# needs plugin source tree
if [ "$LOCAL_MODE" != "true" ]; then
    printf "✓: Check 28c skipped in non-local mode (no plugin source tree)\n"
else
    for f in plugins/*/*skills/*/SKILL.md; do
      [ -f "$f" ] || continue
      skill_dir=$(echo "$f" | cut -d/ -f2)
      plugin_json="plugins/$skill_dir/.claude-plugin/plugin.json"
      [ -f "$plugin_json" ] || plugin_json="plugins/$skill_dir/.codex-plugin/plugin.json"
      # routing prefix is plugin.json `name` (cc_foundry -> foundry); author.name precedes it, so parse JSON, never grep
      routing_prefix=$(python -c "import json,sys; print(json.load(open(sys.argv[1]))['name'])" "$plugin_json" 2>/dev/null)
      # unreadable manifest must not silently drop the file from scan — fall back to no self-filter
      [ -n "$routing_prefix" ] || routing_prefix="__no_self_filter__"
      # match backtick-wrapped or plain /plugin:skill refs
      matches=$(grep -nE '`/[a-z]+:[a-z]|/oss:|/develop:|/research:|/codemap:|/codemap-py:|/foundry:' "$f" 2>/dev/null |
        grep -v "subagent_type\|#.*requires\|requires.*plugin\|plugin.*installed\|if.*plugin" |
        grep -v "/${routing_prefix}:" || true)
      if [ -n "$matches" ]; then
        echo "$matches" | while IFS= read -r line; do
          printf "⚠ 28c: %s — cross-plugin ref without availability guard: %s\n" "$f" "$line"
          printf "  fix: add '(requires <plugin> plugin)' inline, or wrap in availability check\n"
        done
      fi
    done
    printf "✓: Check 28c scan complete\n"
fi  # timeout: 5000
```

Severity: **medium** — user sees broken command in follow-up gate or documentation prose.

Fix: append `` (requires `<plugin>` plugin) `` immediately after cross-plugin skill reference, or restructure as conditional.

| Sub-check | Condition | Severity | Auto-fix |
| -- | -- | -- | -- |
| 28a | no fallback for cross-plugin dispatch | high | no |
| 28b | fallback present but agent not covered | medium | no |
| 28c | prose cross-plugin ref without availability guard | medium | no |
