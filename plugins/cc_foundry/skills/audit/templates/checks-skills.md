# Skill Checks — 22, 23, 24, 27, 28, 30, 31

## Scan-root derivation — prepend to every bash block in this file

Each fenced block below runs as its own Bash tool call in a **fresh shell**, so no variable set in one block survives into the next (`audit/SKILL.md` §State re-derivation; Check 43 in this file flags the same pattern). Each block therefore re-reads `LOCAL_MODE` and re-derives its own scan root.

Roots are **plain directory paths**, never glob patterns held in variables: the tool shell may be `zsh`, which — unlike bash — performs neither word-splitting nor filename generation on an unquoted `$VAR`. A var-held glob such as `for f in $_SKILL_GLOB` iterates once over the literal pattern string and matches nothing. Enumerate with `find` piped into `while IFS= read -r`, which behaves identically under both shells.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
[ "$LOCAL_MODE" = "true" ] && _ROOT="plugins" || _ROOT=".claude"
```

`find "$_ROOT" -path "*/skills/*/SKILL.md"` enumerates skills; `find "$_ROOT" -path "*/agents/*.md"` enumerates agents **including nested subdirectories** (a `*/agents/*.md` glob silently misses `agents/<parent>/<file>.md`); `find "$_ROOT" -path "*/rules/*.md"` enumerates rules.

## Check 22 — Calibration coverage gap

**Step 1 — Read calibrate domain table**: Load calibrate `skills.md` via `cat` (not the Read tool — `Bash(cat:*)` grant is version-proof), extract registered target list under `### Domain table`. Build registered-targets set.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
CALIB_MODES=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/resolve_skill_subdir.py" calibrate modes $([ "$LOCAL_MODE" = "true" ] && echo --local) 2>/dev/null || echo "plugins/cc_foundry/skills/calibrate/modes")  # timeout: 5000
cat "$CALIB_MODES/skills.md"  # timeout: 5000
```

**Step 2 — Scan all skill modes on disk**: Use Glob (`skills/*/SKILL.md`, path `.claude/`) and Glob (`skills/*/modes/*.md`, path `.claude/`) to enumerate every skill and mode file. Extract mode names from `argument-hint:` frontmatter and `## Mode:` / `### Mode:` headings.

**Step 3 — Validate registered targets exist on disk**: For each registered target, verify matching skill/mode file exists. Registered target with no matching file → **medium** (calibrate fails at runtime).

**Step 4 — Identify unregistered calibratable candidates** (model reasoning):

Mode is calibratable when ALL three signals present:

1. **Deterministic structured output**: findings list, completeness checklist, structured table, or machine-readable verdict
2. **Synthetic input feasible**: testable without external services
3. **Ground truth constructable**: known issues injectable and scorable

→ Unregistered mode matching all three: **low** (add to `calibrate/modes/skills.md` domain table)

**Step 5 — Read agents domain table**: Load calibrate `agents.md` via `cat` (not the Read tool — `Bash(cat:*)` grant is version-proof), extract all agent names from `### Domain table`. Build registered-agent-names set.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
CALIB_MODES=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/resolve_skill_subdir.py" calibrate modes $([ "$LOCAL_MODE" = "true" ] && echo --local) 2>/dev/null || echo "plugins/cc_foundry/skills/calibrate/modes")  # timeout: 5000
cat "$CALIB_MODES/agents.md"  # timeout: 5000
```

**Step 6 — Scan all agent files on disk**: (When `LOCAL_MODE=false`, skip `plugins/*/agents/*.md` — scan `.claude/agents/` only.) Use Glob (`plugins/*/agents/*.md`, path project root) for plugin agent files; Glob (`agents/*.md`, path `.claude/`) for directly installed agents. Derive qualified name per file: `plugins/<plugin>/agents/<name>.md` → `<plugin>:<name>`; `.claude/agents/<name>.md` → `<name>`. Build full discovered-agent set.

**Step 7 — Validate registered agents exist on disk**: For each registered agent in domain table, verify it resolves to discovered file. Bare name (e.g. `sw-engineer`) matches `foundry:sw-engineer` when no `.claude/agents/sw-engineer.md` exists — apply model reasoning to resolve bare names against plugin-qualified discoveries. Registered agent with no matching file → **medium** (stale entry causes calibrate to fail at runtime; remove from domain table or correct prefix).

**Step 8 — Identify unregistered agents**: For each discovered agent not in domain table, apply same three-signal calibratability test from Step 4. → Unregistered calibratable agent: **low** (add to `calibrate/modes/agents.md` domain table with appropriate domain string).

## Check 23 — Bash command misuse / native tool substitution

```bash
printf "=== Check 23: Bash misuse candidates ===\n"
grep -rn '\bcat \|`cat ' .claude/agents/ .claude/skills/ .claude/rules/ 2>/dev/null |
grep -v '^Binary' | grep -v '# ' &&
printf "  hint: replace cat with Read tool\n" || true
grep -rn '\bgrep \|\brg \b' .claude/agents/ .claude/skills/ .claude/rules/ 2>/dev/null |
grep -v '^Binary' | grep -v '# .*grep\|Grep tool\|Use Grep' &&
printf "  hint: replace grep/rg with Grep tool\n" || true
grep -rn '\bfind \b.*-name\|\bls \b.*\*' .claude/agents/ .claude/skills/ .claude/rules/ 2>/dev/null |
grep -v '^Binary' | grep -v '# .*Glob\|Use Glob\|Glob tool' &&
printf "  hint: replace find/ls with Glob tool\n" || true
grep -rn 'echo .* >\|tee ' .claude/agents/ .claude/skills/ .claude/rules/ 2>/dev/null |
grep -v '^Binary' | grep -v '# .*Write tool\|Use Write' &&
printf "  hint: replace echo-redirect/tee with Write tool\n" || true
grep -rn '\bsed \b\|\bawk \b' .claude/agents/ .claude/skills/ .claude/rules/ 2>/dev/null |
grep -v '^Binary' | grep -v '# .*Edit tool\|Use Edit\|awk.*{print\|awk.*BEGIN' &&
printf "  hint: replace sed/awk text-substitution with Edit tool\n" || true
printf "✓: Check 23 scan complete\n"
```

After scan, apply model reasoning to each match — exclude cases where shell command genuinely necessary. Flag only where native tool is direct drop-in.

| Shell command | Preferred native tool | Severity |
| -- | -- | -- |
| `cat <file>` | Read tool | medium |
| `grep`/`rg` for content search | Grep tool | medium |
| `find`/`ls` for file listing | Glob tool | medium |
| `echo … >` / `tee` to write a file | Write tool | medium |
| `sed`/`awk` for text substitution | Edit tool | medium |

### Sub-check 23a — python inline policy (CLAUDE.md / MEMORY.md violation)

`Bash(python:*)` in allow list, covers bare `python script.py`. But `python -c "..."` does NOT match `Bash(python:*)` — Claude Code's permission matcher tokenizes the full prefix, so `python -c` needs a separate `Bash(python -c:*)` entry (intentionally absent). Any `python -c` in a skill body pauses for a permission prompt mid-workflow; user deny = phase fails. Enforcement mechanism for the inline-Python antipattern, not a coverage gap.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
[ "$LOCAL_MODE" = "true" ] && _ROOT="plugins" || _ROOT=".claude"
printf "=== Check 23a: python inline policy ===\n"
find "$_ROOT" -path "*/skills/*/SKILL.md" -exec grep -Hn 'python -c\b' {} + 2>/dev/null |
  grep -v '^Binary' | grep -v '^\s*#' &&
printf "  hint: python not in allow list by design — move logic to bin/*.py or use native tools (Read/Write/Edit/Bash with jq)\n" || true
printf "=== Check 23a: heredoc python policy ===\n"
find "$_ROOT" -path "*/skills/*/SKILL.md" -exec grep -Hn "python << '\|python <<\"" {} + 2>/dev/null |
  grep -v '^Binary' | grep -v '^\s*#' &&
printf "  hint: CLAUDE.md bans heredoc python; use bin/*.py instead\n" || true
printf "✓: Check 23a scan complete\n"  # timeout: 5000
```

Severity: **high** — permission prompt mid-workflow blocks automation; user deny = skill phase fails.

| Sub-check | Pattern | Severity |
| -- | -- | -- |
| 23a — python -c inline | `python -c` in skill body | high |
| 23a — python heredoc | `python << '` in skill body | high |

**Report only** — never auto-fix; some Bash invocations in example/illustration code blocks intentional.

### Sub-check 23b — `# timeout:` annotation without shell enforcement

`# timeout: N` on a bash line is a hint to Claude Code's Bash tool — no effect when the command runs outside the tool (bin/ scripts, CI, direct shell). Hard enforcement needs `timeout S <cmd>` prefix (bash) or `--timeout S` via argparse passed to every blocking call (Python subprocess). See `bin-authoring-guide.md` §Timeout Policy for patterns and ms→s conversion table.

Rules:

- **Bash call sites**: line with `# timeout: N` must have `timeout S` shell prefix (S = N ÷ 1000); no internal fallback exists.
- **Python call sites**: shell `timeout S` wrapper optional — timeout enforced internally via `--timeout` argparse parameter whose `default=` must equal N ÷ 1000 (from the calling site's `# timeout: N` annotation).
- **Python `bin/` scripts with subprocess**: must expose `--timeout SECS` and pass it to every `subprocess.*` call.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
[ "$LOCAL_MODE" = "true" ] && _ROOT="plugins" || _ROOT=".claude"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/check_skill_contracts.py" --check 23b --root "$_ROOT"  # timeout: 30000
```

After scan, apply model reasoning — exclude lines inside illustration/example code blocks (marked `# ✗`, surrounded by explanatory prose, or not reachable as actual tool-call commands). Flag only live executable lines. Severity: **medium** — bash comment-only timeout silently ignored at runtime; Python script missing `--timeout` default has no internal enforcement.

| Sub-check | Pattern | Severity |
| -- | -- | -- |
| 23b — bash comment-only timeout | `# timeout: N` without `timeout S` shell prefix (non-python invocations) | medium |
| 23b — subprocess no timeout= | `subprocess.*` call without `timeout=` in `bin/*.py` | medium |
| 23b — missing --timeout default | Python `bin/` script uses subprocess but no `--timeout` argparse arg | medium |

**Report only** — flag for human review; timeout default values must match `# timeout: N` at call site (N ÷ 1000).

### Sub-check 23c — `eval` for multi-value data output

Skill uses `eval "$(...)"` or `eval "$(python ...)"` to capture data values from a bin/ script, rather than writing to TMPDIR files. Distinct from shell-setup eval (health_sentinel.py, ssh-agent) — those exempt.

```bash
# timeout: 10000
printf "=== Check 23c: eval for data output ===\n"
grep -rn 'eval.*"\$.*python\|eval.*"\$.*bin/' \
    plugins/*/skills/*/SKILL.md .claude/skills/*/SKILL.md 2>/dev/null |
  grep -v 'health_sentinel\|ssh-agent\|direnv\|rbenv\|pyenv\|nvm\|# shell-setup' |
  grep -v '^\s*#' | head -20
```

False-positive exemption: `eval "$(python .../health_sentinel.py ...)"` — health monitoring shell-setup (emits `SENTINEL=...` for calling shell, not data output). Any other `eval "$(python ...)` = finding.

**Sub-check 23d** — shell variable used for state across separate Bash tool calls. **Not grep-detectable** (requires cross-block analysis of Bash call boundaries, which are runtime not lexical). Flag during curator per-file review only: when auditing a skill, scan for `VAR=$(...)` pattern in one fenced block and `"$VAR"` or `[ -z "$VAR" ]` in a later fenced block with no `cat "${TMPDIR:-/tmp}/...-${CSID}"` supplying `VAR` between them.

| Sub-check | Pattern | Severity | Auto-fix |
| -- | -- | -- | -- |
| 23c — eval for data output | `eval "$(python ...)` without `health_sentinel` context | medium | no — replace with TMPDIR-file pattern per `bin-authoring-guide.md §Script Output Routing` |
| 23d — cross-call shell var | `$VAR` in block N, set in earlier block M, no TMPDIR bridge | medium | no — curator flag only |

**Report only** — never auto-fix; replacement requires understanding the script's full output contract.

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

## Check 27 — Cross-plugin shared-file reference integrity

**Policy — every plugin's `_shared` is its own** (`plugins/CLAUDE.md` §Self-Contained \_shared). A plugin must resolve `skills/_shared` through its **own** resolver and read only files it **ships itself**. Two forbidden shapes:

- **Global path** — `$HOME/.claude/skills/_shared/...` or bare `.claude/skills/_shared/...`. No such path exists any more: `/foundry:setup` symlinks only `rules/*.md` and `TEAM_PROTOCOL.md`, and purges any leftover `~/.claude/skills/` link. A dir with `SKILL.md` there would register as a user-level skill and shadow Claude Code's bundled skill of that name.
- **Sibling reach-in** — resolving another plugin's tree (`resolve_shared_path.py foundry` from a non-foundry plugin, `dev_shared_resolve.py --foundry`, `$_FOUNDRY_SHARED`, `$_FOUNDRY_BIN`, or a literal `plugins/cc_<other>/` path). Content genuinely needed by two plugins is **duplicated**, not borrowed: add a `MANIFEST` entry in `bin/propagate_shared.py` so the copies stay byte-identical.

**Special antipattern — foundry-dependency catch-22**: a borrowed file that describes how to degrade *without* foundry (e.g. `agent-resolution.md` listing `general-purpose` substitutes) is **critical** — the instructions for surviving foundry's absence are reachable only when foundry is present.

**Step 1 — Global `_shared` paths** (any plugin, foundry included):

```bash
grep -rn '\.claude/skills/_shared/' plugins/*/skills/ plugins/*/*.md 2>/dev/null  # timeout: 5000
```

- Any match → **[high] 27a**: `<plugin>/<file>: reads _shared via global .claude/skills/ path — resolve own plugin's skills/_shared instead`

**Step 2 — Sibling reach-in**:

```bash
grep -rn 'resolve_shared_path\.py" foundry\|--foundry\|_FOUNDRY_SHARED\|_FOUNDRY_BIN\|plugins/cc_[a-z]*/skills/_shared' plugins/*/skills/ 2>/dev/null | grep -v '^plugins/cc_foundry/'  # timeout: 5000
```

Ignore a plugin's own bare-path fallback (e.g. `plugins/cc_oss/...` inside `cc_oss`) — that is the sanctioned last-resort tier. Everything else:

- Match → **[high] 27b**: `<plugin>/<skill>: reads <file> from cc_foundry's _shared — ship a copy in this plugin's skills/_shared and add a propagate_shared.py MANIFEST entry`

**Step 3 — Catch-22 upgrade**:

For each 27b match, inspect the borrowed filename and surrounding context for degraded-mode signals (keywords: `fallback`, `without foundry`, `agent-resolution`, `general-purpose`, `not installed`).

- Match → upgrade to **[critical] 27c**: `<plugin>/<skill>: fallback file <name> is only reachable via foundry — catch-22`
- No match → keep as **[high] 27b**

**Step 4 — Orphaned own-plugin \_shared files**:

```bash
for f in plugins/*/skills/_shared/*; do
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
| 27a — file absent from foundry's \_shared/ | high | no |
| 27b — catch-22 (fallback file needs foundry to reach) | critical | no |
| 27c — plugin-local \_shared/ file referenced but not mounted | medium | no |

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
    for f in plugins/*/skills/*/SKILL.md; do
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
    grep -rn 'subagent_type' plugins/*/skills/*/SKILL.md 2>/dev/null | grep -v '^Binary'  # timeout: 5000
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
    for f in plugins/*/skills/*/SKILL.md; do
      [ -f "$f" ] || continue
      skill_plugin=$(echo "$f" | cut -d/ -f2)
      # match backtick-wrapped or plain /plugin:skill refs
      matches=$(grep -nE '`/[a-z]+:[a-z]|/oss:|/develop:|/research:|/codemap:|/codemap-py:|/foundry:' "$f" 2>/dev/null |
        grep -v "subagent_type\|#.*requires\|requires.*plugin\|plugin.*installed\|if.*plugin" |
        grep -v "$(echo "$skill_plugin" | sed 's/[^a-z]//g'):" || true)
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

## Check 30 — Plugin skill bash operational correctness

Four static-grep patterns catching silent failures in skill SKILL.md bash blocks. Run across both `.claude/skills/` and `plugins/*/skills/` — bugs appear in any skill.

### 30a — Pipe exit code capture (PIPESTATUS)

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
[ "$LOCAL_MODE" = "true" ] && _ROOT="plugins" || _ROOT=".claude"
printf "=== Check 30a: Pipe exit code capture ===\n"
# tail/head always exit 0 — $? after them ≠ upstream cmd's exit
find "$_ROOT" -path "*/skills/*" -name "*.md" -exec grep -Hn '| tail\b\|| head\b' {} + 2>/dev/null |
  grep -v 'PIPESTATUS\|pipefail\|#.*tail\|#.*head' |
  grep -v '^Binary' &&
printf "  hint: use \${PIPESTATUS[0]} or set -o pipefail; \$? captures tail/head exit (always 0)\n" || true
printf "✓: Check 30a scan complete\n"  # timeout: 5000
```

Severity: **critical** — gate commands appear to pass on genuine failure; `$?` after `cmd | tail -N` = tail's exit code (0), not cmd's.

Fix pattern: `cmd 2>&1 | tail -N; EXIT=${PIPESTATUS[0]}`

### 30b — SKIP variable guard missing

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
[ "$LOCAL_MODE" = "true" ] && _ROOT="plugins" || _ROOT=".claude"
printf "=== Check 30b: SKIP variable guard ===\n"
find "$_ROOT" -path "*/skills/*" -name "*.md" -exec grep -Hn 'SKIP_[A-Z_]*=1' {} + 2>/dev/null |
  grep -v '^Binary' | grep -v '#' | while IFS= read -r match; do
    file=$(echo "$match" | cut -d: -f1)
    grep -q '\[ "\${SKIP_' "$file" 2>/dev/null ||
      printf "⚠ SKIP guard missing: %s — SKIP variable set but no conditional guard found\n" "$file"
done
printf "✓: Check 30b scan complete\n"  # timeout: 5000
```

Severity: **critical** — `SKIP_RUFF=1` set by tool detection, but `$RUNNER ruff check` runs unconditionally; detection is cosmetic.

Fix pattern: `[ "${SKIP_RUFF:-0}" -ne 1 ] && $RUNNER ruff check ...`

### 30c — Agent filename convention mismatch (model reasoning)

Cannot be caught by grep alone — requires reading spawn prompt and consolidator read pattern in same file.

Flag when skill file:

1. Spawns agents with prompt instructing them to write findings to file named with plugin-prefixed format (e.g. `foundry:sw-engineer.md`)
2. AND consolidator reads files using bare-name format (e.g. `sw-engineer.md`)

These never match → all agent findings silently dropped.

Severity: **high**

Fix: standardize to bare agent name in both spawn prompt and consolidator read pattern (e.g. `sw-engineer.md`).

### 30d — TEST_CMD used with pytest-specific flags without PYTEST_CMD split

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
[ "$LOCAL_MODE" = "true" ] && _ROOT="plugins" || _ROOT=".claude"
printf "=== Check 30d: TEST_CMD/PYTEST_CMD split ===\n"
find "$_ROOT" -path "*/skills/*" -name "*.md" -exec grep -Hn \
  '\$TEST_CMD.*--tb\b\|\$TEST_CMD.*--co\b\|\$TEST_CMD.*::\|\$TEST_CMD.*--cov\b\|\$TEST_CMD.*--doctest' {} + 2>/dev/null |
  grep -v 'PYTEST_CMD\|#' | grep -v '^Binary' &&
printf "  hint: derive PYTEST_CMD for pytest-specific flags; TEST_CMD=tox or make won't accept --tb/--co/::/--cov\n" || true
printf "✓: Check 30d scan complete\n"  # timeout: 5000
```

Severity: **high** — skill fails silently on tox/make projects when pytest-specific flags appended to TEST_CMD.

Fix: after detecting TEST_CMD, derive `PYTEST_CMD` for targeted runs: `tox` → `PYTEST_CMD="uv run pytest"`; `make test` → `PYTEST_CMD="uv run pytest"`.

**Report only** — no auto-fix; resolution requires understanding each skill's runner detection block.

### 30e — Heredoc python in skill bodies

Heredoc python blocks (`python << 'EOF'`) banned by CLAUDE.md. Distinct from 23a (targets `python -c` one-liners); 30e catches multi-line heredoc forms that bypass one-liner size limit.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
[ "$LOCAL_MODE" = "true" ] && _ROOT="plugins" || _ROOT=".claude"
printf "=== Check 30e: Heredoc python ===\n"
find "$_ROOT" -path "*/skills/*" -name "*.md" -exec grep -Hn "python <<\|python << '" {} + 2>/dev/null |
  grep -v '^Binary' | grep -v '^\s*#' &&
printf "  hint: CLAUDE.md bans python heredoc; use bin/*.py script instead\n" || true
printf "✓: Check 30e scan complete\n"  # timeout: 5000
```

Severity: **high** — heredoc triggers permission prompt; user deny = workflow block; violates CLAUDE.md §Pre-Authorized Operations.

### 30f — Missing exit on confirmed failure path

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
[ "$LOCAL_MODE" = "true" ] && _ROOT="plugins" || _ROOT=".claude"
printf "=== Check 30f: Missing exit on confirmed failure ===\n"
find "$_ROOT" -path "*/skills/*" -name "*.md" -exec grep -Hn \
  'GENUINE.FAILURE\|all retries failed\|failed.*abort\|error.*critical\|cannot continue' {} + 2>/dev/null |
  grep -v '^Binary' | grep -v '^\s*#' | while IFS= read -r match; do
    file=$(echo "$match" | cut -d: -f1)
    line=$(echo "$match" | cut -d: -f2)
    context=$(awk "NR>=$line && NR<=$((line+3))" "$file" 2>/dev/null)
    echo "$context" | grep -q 'exit [1-9]' ||
      printf "⚠ missing exit: %s:%s — failure detected but execution continues\n" "$file" "$line"
  done
printf "✓: Check 30f scan complete\n"  # timeout: 5000
```

Severity: **high** — workflow continues past a detected failure; downstream steps produce misleading output or incorrect partial results.

Fix pattern: add `exit 1` (or appropriate non-zero exit) immediately after the failure is confirmed; do NOT continue to next step.

| Sub-check | Pattern | Severity | Auto-fix |
| -- | -- | -- | -- |
| 30a — pipe exit code | `\| tail` / `\| head` without PIPESTATUS | critical | no |
| 30b — SKIP guard missing | `SKIP_X=1` with no `[ "${SKIP_X:-0}" ]` guard | critical | no |
| 30c — filename mismatch | spawn filename ≠ consolidator filename (model reasoning) | high | no |
| 30d — TEST_CMD+pytest flags | `$TEST_CMD --tb` / `--co` / `::` / `--cov` without PYTEST_CMD | high | no |
| 30e — heredoc python | `python <<` in skill body | high | no |
| 30f — missing exit | confirmed failure with no `exit 1` within 3 lines | high | no |

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
done < <(find "$_ROOT" -path "*/skills/*/SKILL.md" 2>/dev/null | sort)
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
    echo "$fm" | grep -q '^when_to_use:' && {
        printf "⚠ 31a: %s — when_to_use: present (deprecated; merge content into description: then remove)\n" "$skill"
        found=1
    }
done < <(find "$_ROOT" -path "*/skills/*/SKILL.md" 2>/dev/null | sort)
[ "$found" -eq 0 ] && printf "✓: Check 31a — frontmatter complete across all skills\n"
```

Severity: **medium** for `effort:` (no default documented); **low** for `when_to_use:` (deprecated field).

| Sub-check | Field | Condition | Severity | Auto-fix |
| -- | -- | -- | -- | -- |
| 31 — tool-body mismatch | `allowed-tools` | body calls Skill/AskUserQuestion/Agent, not in frontmatter | critical | yes |
| 31a — effort missing | `effort:` | always required | medium | yes |
| 31a — when_to_use present | `when_to_use:` | deprecated — any presence flagged (merge into `description:`, then strip) | low | no |

## Check C35 — Background agent health monitoring compliance (CLAUDE.md §6)

CLAUDE.md §6 requires every skill spawning background agents to implement: (1) launch sentinel creation, (2) 5-min file-activity poll, (3) 15-min hard cutoff. Absence = stalled agents silently drop findings.

**Step 1 — Find skills with background agent spawns**:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
[ "$LOCAL_MODE" = "true" ] && _ROOT="plugins" || _ROOT=".claude"
printf "=== Check C35: Background agent health monitoring ===\n"
mkdir -p "${TMPDIR:-/tmp}/audit-state-${CSID}"
find "$_ROOT" -path "*/skills/*/SKILL.md" -exec grep -l 'run_in_background.*true\|run_in_background=true' {} + 2>/dev/null |
  sort > "${TMPDIR:-/tmp}/audit-state-${CSID}/c35-bg-skills"
if [ ! -s "${TMPDIR:-/tmp}/audit-state-${CSID}/c35-bg-skills" ]; then
    printf "✓: No background agent spawns found — C35 N/A\n"
else
    cat "${TMPDIR:-/tmp}/audit-state-${CSID}/c35-bg-skills"
fi  # timeout: 5000
```

**Step 2 — For each skill found, verify §8 protocol elements**:

Step 1's list is re-read from the state file — a variable set in Step 1's block is gone by the time this block runs (fresh shell per Bash call).

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
while IFS= read -r f; do  # timeout: 5000
    [ -f "$f" ] || continue
    skill=$(basename "$(dirname "$f")")
    if grep -q 'agent-spawn-protocol' "$f" 2>/dev/null; then
        printf "✓ C35: %s — references agent-spawn-protocol.md\n" "$skill"
        continue
    fi
    # grep -c prints 0 AND exits 1 on no match — || echo 0 double-fires, "0\n0" breaks numeric tests below
    has_sentinel=$(grep -c 'LAUNCH_AT\|touch /tmp/' "$f" 2>/dev/null) || has_sentinel=0
    has_poll=$(grep -c 'find.*-newer.*-type f.*wc -l\|MONITOR_INTERVAL' "$f" 2>/dev/null) || has_poll=0
    has_cutoff=$(grep -c 'HARD_CUTOFF\|timed.out\|15 min\|900' "$f" 2>/dev/null) || has_cutoff=0
    [ "$has_sentinel" -eq 0 ] && printf "⚠ C35a: %s — no launch sentinel (CLAUDE.md §6 step 1)\n" "$skill"
    [ "$has_poll" -eq 0 ]    && printf "⚠ C35b: %s — no 5-min file-activity poll (§8 step 2)\n" "$skill"
    [ "$has_cutoff" -eq 0 ]  && printf "⚠ C35c: %s — no 15-min hard cutoff (§8 step 3)\n" "$skill"
done < "${TMPDIR:-/tmp}/audit-state-${CSID}/c35-bg-skills"
```

Severity: **high** for C35a/b/c — stalled background agents drop findings with no user-visible signal. Fix: reference `$_FOUNDRY_SHARED/agent-spawn-protocol.md` (preferred once file exists) or inline all three §8 elements in skill.

| Sub-check | Pattern | Severity | Auto-fix |
| -- | -- | -- | -- |
| C35a — no launch sentinel | no `touch /tmp/<sentinel>` after background spawn | high | no |
| C35b — no file-activity poll | no 5-min `find -newer` loop | high | no |
| C35c — no hard cutoff | no `HARD_CUTOFF` / 15-min signal | high | no |

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
    skill_md="$(dirname "$(dirname "$mode_file")")/SKILL.md"
    [ -f "$skill_md" ] || continue
    mode_name=$(basename "$mode_file")
    if ! /usr/bin/grep -qF "$mode_name" "$skill_md" 2>/dev/null; then
        printf "⚠ 32a: %s — not referenced in %s\n" "$mode_file" "$skill_md"
        found=1
    fi
done < <(find "$_ROOT" -path "*/skills/*/modes/*.md" 2>/dev/null | sort)
[ "$found" -eq 0 ] && printf "✓: Check 32a — all mode files referenced in parent SKILL.md\n"
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
    skill_md="$(dirname "$(dirname "$tpl_file")")/SKILL.md"
    [ -f "$skill_md" ] || continue
    tpl_name=$(basename "$tpl_file")
    if ! /usr/bin/grep -qF "$tpl_name" "$skill_md" 2>/dev/null; then
        printf "⚠ 32b: %s — not referenced in %s\n" "$tpl_file" "$skill_md"
        found=1
    fi
done < <(find "$_ROOT" -path "*/skills/*/templates/*" -type f 2>/dev/null | sort)
[ "$found" -eq 0 ] && printf "✓: Check 32b — all template files referenced in parent SKILL.md\n"
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

`bin/` scripts that exist in the plugin source tree but are not referenced by any `.md` file in that plugin (SKILL.md, agents, rules, modes, templates, \_shared) are unreachable at runtime. Common cause: script authored as scaffolding but never wired into its caller SKILL.md.

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
        printf "⚙ Check 32e — %d bin/ scripts found; delegating similarity analysis to foundry:curator\n" "$_C32E_N"
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

Detects explicit sync instructions in plugin `.md` files (`SYNC:`, `lock-step`, `keep both`, `duplicated from`, `mirror this in`, `keep.*copies.*sync`). Each marker = author admitted manual sync is required = extraction not yet done.

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

## Check 33 — Code block duplication (NxN similarity matrix)

<!-- policy-sibling: plugins/cc_foundry/skills/audit/modes/efficiency.md (Phase B2 Table 2 — same Gate/Score spec) -->

Full-spectrum detection of duplicate or near-duplicate fenced code blocks across all .md files (SKILL.md, agents, rules, templates, modes) — any language (bash, python, sh, perl, ruby, js, etc.). Produces NxN pairwise similarity matrix to surface extraction candidates: 33a within-file (same block 3+ times — bin/ script or helper function candidate); 33b cross-file NxN (same block in 3+ .md files — shared bin/ script candidate).

**Check 33a — Within-file repetition**: delegate to Phase A foundry:curator (has full file context). Curator prompt must include:

> "Extract every fenced code block (any language marker — ```` ```bash ````, ```` ```python ````, ```` ```sh ````, ```` ```perl ````, ```` ```ruby ````, ```` ```js ````, etc.) from this file. For each pair of blocks, compute normalized similarity: strip comments → normalize variable names to `<VAR>` → normalize string literals to `<STR>` → compare structure. Report any pair with similarity ≥ 0.8 that appears 3+ times (within this file) as a 33a finding. For each candidate: block language, purpose, occurrence count, similarity score, what differs between instances, and suggested extraction (bash function defined once in pre-flight, or `bin/<name>.py` — bin/ scripts are Python only). Context saving estimate: (block_lines − 1) × occurrence_count. Skip: blocks marked `# audit-skip: resilience-replication` (first line of block) or prose annotation matching 'intentional resilience replication'."

**33b — Cross-file NxN** — two phases: bash quick scan identifies known hotspots; curator NxN delegation runs when clusters found. Scope: all .md files in plugin tree (SKILL.md, agents, rules, templates, modes).

**Phase 1 — Bash quick scan** (known duplication hotspots):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
if [ "$LOCAL_MODE" = "true" ]; then
    _C33_DIR="plugins/"
else
    # latest foundry version dir — skip other cached versions
    _C33_DIR=$(ls -td ~/.claude/plugins/cache/borda-ai-rig/foundry/*/ 2>/dev/null | head -1)
    _C33_DIR="${_C33_DIR:-.claude/}"
fi
printf "=== Check 33b: scope=%s files=%d ===\n" "$_C33_DIR" \
    "$(find "$_C33_DIR" -name '*.md' -type f 2>/dev/null | wc -l | tr -d ' ')"

printf "=== Check 33b Phase 1: Cross-file code block quick scan ===\n"

MODE_DISPATCH=$(grep -rl 'find.*plugins/cache.*-path.*modes/' "$_C33_DIR" --include="*.md" 2>/dev/null | wc -l | tr -d ' ')
[ "${MODE_DISPATCH:-0}" -ge 3 ] && printf "⚠ 33b: bash mode-dispatch pattern in %s files — bin/ extraction candidate: resolve_skill_mode.py <mode>\n" "$MODE_DISPATCH"

SHARED_RES=$(grep -rl '=\$(find.*plugins/cache.*_shared\|=\$(ls -td.*plugins/cache' "$_C33_DIR" --include="*.md" 2>/dev/null | wc -l | tr -d ' ')
[ "${SHARED_RES:-0}" -ge 3 ] && printf "⚠ 33b: bash _shared resolution pattern in %s files (variants may be inconsistent) — bin/ extraction candidate\n" "$SHARED_RES"

PY_HEREDOC=$(grep -rl 'python -c' "$_C33_DIR" --include="*.md" 2>/dev/null | wc -l | tr -d ' ')
[ "${PY_HEREDOC:-0}" -ge 3 ] && printf "⚠ 33b: python -c one-liner in %s files — evaluate if any cluster repeats\n" "$PY_HEREDOC"

# audit-skip: resilience-replication — unsupported-flag-check is intentional per-plugin
FLAG_CHECK=$(grep -rl 'Unknown flag' "$_C33_DIR" --include="*.md" 2>/dev/null | wc -l | tr -d ' ')
[ "${FLAG_CHECK:-0}" -ge 3 ] && printf "ℹ 33b: unsupported-flag-check boilerplate in %s files — known intentional per-plugin resilience\n" "$FLAG_CHECK"

echo "--- Code block language distribution across all .md files (Phase 2 trigger signals) ---"
NEEDS_CURATOR_NXN=false
for lang in bash python sh perl ruby node js; do
  count=$(grep -rl "^\`\`\`${lang}" "$_C33_DIR" --include="*.md" 2>/dev/null | wc -l | tr -d ' ')
  [ "${count:-0}" -ge 5 ] && { echo "  ${lang}: ${count} files — TRIGGER Phase 2 curator NxN"; NEEDS_CURATOR_NXN=true; }
  [ "${count:-0}" -ge 2 ] && [ "${count:-0}" -lt 5 ] && echo "  ${lang}: ${count} files"
done

[ "$NEEDS_CURATOR_NXN" = "true" ] && printf "→ Phase 2: curator NxN delegation triggered — see 33b Phase 2 instructions below\n"
printf "✓: Check 33b Phase 1 complete\n"  # timeout: 5000
```

**Phase 2 — Curator NxN delegation**: run when Phase 1 finds any known pattern in ≥3 files OR any language marker appears in ≥5 .md files. Spawn **foundry:curator** with all flagged files:

> "Perform Check 33b cross-file code block analysis on: \<list all .md files that contain flagged language markers from Phase 1>. For each file, extract every fenced code block (any language) ≥ 5 lines. Skip: blocks marked `# audit-skip: resilience-replication` (first line) or prose annotation matching 'intentional resilience replication'.
>
> **Step 1 — Purpose statements**: for each block, write a one-sentence purpose statement describing what it does functionally (not how) — e.g. 'resolves `_shared/` path from plugin cache', 'detects codex plugin availability', 'emits boilerplate-duplication counts'. Syntactic line-intersection alone is blind to conditional-inversion and variable renaming — purpose grouping catches duplicates that normalization misses.
>
> **Step 2 — Purpose clusters**: group blocks with equivalent purpose. This is the primary grouping. Singletons omitted. Assign cluster ID `C<n>`. **Computational equivalence gate (mandatory before finalizing)**: purpose-wording similarity is necessary but not sufficient — verify cluster members target the same output namespace (e.g. `.reports/audit/*` vs `.reports/research/*` are different namespaces even when both "write a report file"), same inputs, and same side-effect semantics before merging; split into separate clusters when destinations/inputs differ structurally despite similar wording, and never propose renaming/redirecting one site's output to match another's without this equivalence evidence stated in the row.
>
> **Step 3 — Syntactic similarity (secondary)**: within each cluster, normalize: strip `#` comment lines → replace path segments / slugs / numeric literals with `<STR>` → **replace ALL argument/parameter values (flag values, option strings, concrete command arguments) with `<ARG>`** — e.g. `CODEMAP_AVAILABLE=$(find ~/.claude/plugins/cache -name "codemap*" -type d ...)` and `FOUNDRY_AVAILABLE=$(find ~/.claude/plugins/cache -name "foundry*" -type d ...)` both normalize to `<VAR>=$(find ~/.claude/plugins/cache -name "<ARG>" -type d ...)`. Compute `sim(A,B) = 2 × |lines(A_norm) ∩ lines(B_norm)| / (|A| + |B|)`. Record max-sim per cluster. Mark **DUPLICATE** when max-sim ≥ 0.90.
>
> Write Table 1 and Table 2 to `$RUN_DIR/similarity-check33.md` using the Write tool.
>
> Table 1 format: `| Cluster | Block IDs | Files | Lang | Lines each | Purpose | Max-sim | Duplicate? |`
>
> Table 2 format: `| Cluster | ParamSlots | Tokens | Gate | Score | Verdict | Differs-by | Recommended extraction |`
>
> **Never-extract list — checked before the gate.** A cluster matching any of these is not a candidate: replacement costs more tokens than the block (a one-command block becomes a longer invocation line); the block is marked `# audit-skip` or declared an intentional replication in prose beside it; its only purpose is echoing values for the model to read; it is one-off glue under ~10 lines in a single file; its body carries model-substituted placeholders (`<changed_files>`, `<TARGET>`) rather than shell variables; it is an `Agent(...)` prompt, an output template, or a prompt string assigned to a shell variable; it is a propagated copy owned by another plugin's MANIFEST entry (extract at the canonical); or its shape is load-bearing for a permission hook — `IFS= read -r VAR < "${TMPDIR:-/tmp}/…-${CSID}"` passes `sentinel-read-allow.js` by shape, and a `$(python …)` replacement reintroduces the prompt it exists to avoid. Record each in Table 1 with its reason and omit it from Table 2.
>
> Where: **ParamSlots** = count of distinct `<ARG>` slots after normalization; **Tokens** = estimated token count of block; **Gate** = `G1:P/F · G2:P/F · G3:P/F` (all must pass or Verdict = HOLD) — G1 (Size OR execution cost): block > 100 tokens, **or** block launches an external interpreter process (subprocess/heredoc/`-c` — python/node/perl/ruby/etc) **and** occurs ≥3× in cluster (repeated process-fork cost is real even when each instance is token-small — a tiny `python -c "..."` one-liner repeated 84× must not gate out on size alone); G2 (Independence): no branch on prior LLM decision that cannot become explicit arg; G3 (Identity): has computational meaning outside orchestration prose (high CallerScopeDeps = G3 fail indicator); **Score** = sum of applicable positive-dimension weights when gate passes — Testable (deterministic I/O, writable pytest/shellcheck test) +2 · Reuse (same logic in 2+ .md files) +2 · Token drain (block > 300 tokens) +2 · Process overhead (external interpreter launched ≥3× in cluster) +2 · Lintable (shellcheck/ruff applicable) +1 · Run frequency (executes >1× per skill invocation) +1 · Standalone debuggable (runnable with no SKILL.md context) +1; **Verdict** = HOLD (any gate fail) · LOW (0–1) · MEDIUM (2–3) · HIGH (≥4); **Differs-by** = concrete `<ARG>` slot values varying across instances (become CLI parameters in extracted script).
>
> Return ONLY: `{\"status\":\"done\",\"file\":\"$RUN_DIR/similarity-check33.md\",\"clusters\":N,\"duplicates\":N,\"similar\":N,\"findings\":N,\"confidence\":0.N}`"

Severity: **medium** for actionable extraction candidates (mode-dispatch, \_shared resolution, multi-file python clusters); **low/info** for known intentional replications (unsupported-flag-check, health-monitoring constants — per-plugin resilience by design per `plugins/CLAUDE.md`).

Auto-fix guidance:

- **Bin/ script**: any self-contained block (stdout output, no function defs, no shell state mutation) → `plugins/<plugin>/bin/<name>.py`; callers: `python "${CLAUDE_PLUGIN_ROOT:-plugins/<plugin>}/bin/<name>.py"`. Python only — never `.sh`, which does not execute on native Windows (`plugins/CLAUDE.md` §Installability). Prefer the bare invocation over a `$(...)` capture: the bare form is covered by each plugin's `Bash(python:*)` allow entry, while a capture needs an exact-text entry in that plugin's `blueprint-manifest.json`.
- **Inline function**: for within-skill bash duplication where block uses caller shell state → define bash function once in pre-flight, call at each site
- **Never extract**: blocks explicitly marked as resilience replications (unsupported-flag-check, health-monitoring constants)

| Sub-check | Target | Condition | Severity | Auto-fix |
| -- | -- | -- | -- | -- |
| 33a — within-file repetition | single .md file | same code block (any language) 3+ times, constants only differ | medium | inline helper function or bin/ script |
| 33b — cross-file repetition | 3+ .md files | same block (excluding known resilience replications) | medium/low | bin/ script with fallback chain |

## Check 38 — AskUserQuestion cap violation

`communication.md` §Interactive Questions hard-caps `AskUserQuestion` at 4 questions per call, and skills must not ask >4 questions across a single decision branch. Violations cause UX breakage (silent truncation or tool error).

Scan all SKILL.md files in scope. For each file, count the total number of `AskUserQuestion` occurrences. Apply the following heuristic:

- ≤4 calls → `✓` (likely safe; one branch per call is typical)
- 5–8 calls → flag **medium** — review that no single decision branch asks >4 sequentially
- > 8 calls → flag **high** — almost certainly some branch exceeds the cap

```bash
printf "=== Check 38: AskUserQuestion cap ===\n"
for f in $(find . -path "*/skills/*/SKILL.md" 2>/dev/null | sort); do
    # grep -c prints 0 AND exits 1 on no match — || echo 0 double-fires; most files have none, so this hit the majority
    count=$(grep -c "AskUserQuestion" "$f" 2>/dev/null) || count=0
    if [ "$count" -gt 8 ]; then
        printf "C38-HIGH: %d AskUserQuestion calls in %s — review for >4-per-branch\n" "$count" "$f"
    elif [ "$count" -gt 4 ]; then
        printf "C38-MEDIUM: %d AskUserQuestion calls in %s — review for >4-per-branch\n" "$count" "$f"
    fi
done  # timeout: 5000
```

**Severity**: high (>8 calls) / medium (5–8 calls) — functional regression; gate prompts that exceed cap silently drop later questions. Fix: collapse related sub-questions into one `AskUserQuestion` call (max 4 options per call, max 4 calls per skill workflow branch).

## Check 40 — Health monitoring gap

Any SKILL.md that spawns `Agent(...)` with `run_in_background=True` (or the Agent tool's equivalent) **must** implement the CLAUDE.md §6 health monitoring protocol: sentinel file creation + 5-min find-newer poll + 15-min hard cutoff + one extension.

Scan all SKILL.md files in scope. For each file, detect `run_in_background` (case-insensitive). If found, verify that the SAME file also contains `health_sentinel` OR (`find ... -newer` AND `wc -l`). If not → flag.

```bash
printf "=== Check 40: Health monitoring gap ===\n"
for f in $(find . -path "*/skills/*/SKILL.md" 2>/dev/null | sort); do
    if grep -qi "run_in_background" "$f" 2>/dev/null; then
        if ! grep -q "health_sentinel\|find.*-newer.*wc -l" "$f" 2>/dev/null; then
            printf "C40-HIGH: background agent without monitoring protocol: %s\n" "$f"
        fi
    fi
done  # timeout: 5000
```

**Severity**: high — background agents can silently time out with no user notification; lost work and false-progress indicators result. Fix: add CLAUDE.md §6 sentinel + poll protocol immediately after every `Agent(..., run_in_background=True)` spawn call.

## Check 43 — Shell variable persistence across Bash calls

Variables assigned in one ```` ```bash ```` block are NOT available in later bash blocks — each Bash tool call runs in a fresh shell. Referencing a variable from a prior block silently expands to empty string, corrupting commands, paths, and conditional guards without any error.

```bash
printf "=== Check 43: Shell variable persistence across Bash calls ===\n"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/check_bash_persistence.py" --scan-dir .  # timeout: 15000
```

**Severity**: critical — silent empty-string expansion; corrupts file paths, conditional branches, spawn prompts. No error surfaced at runtime.

Fix: re-assign the variable at the top of every bash block that needs it, or combine dependent commands into a single bash block. Do NOT rely on variable state persisting between bash tool calls.

| Sub-check | Pattern | Severity | Auto-fix |
| -- | -- | -- | -- |
| 43 — cross-block ref | `$VAR` in block N where VAR assigned only in block M\<N | critical | no — requires combining blocks or re-assigning |

## Check 42 — Unexpanded variables in agent spawn prompts

Variables written as `$VAR` or `${VAR}` inside ```` ```markdown ```` fenced blocks (spawn prompt templates) are passed literally to the spawned agent — the agent receives the dollar-sign string, not the resolved value. The agent cannot resolve orchestrator shell variables; `$_FOUNDRY_SHARED/foo.md` becomes the literal path string `$_FOUNDRY_SHARED/foo.md` and the Read tool fails silently.

```bash
printf "=== Check 42: Unexpanded variables in spawn prompt markdown blocks ===\n"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/check_spawn_prompt_vars.py" --scan-dir .  # timeout: 10000
```

**Severity**: critical — spawn prompt contains unresolved path or name; spawned agent reads wrong file or fails silently with no diagnostic.

Fix: resolve the variable in a preceding bash block and substitute the resolved value inline into the spawn prompt string. For paths: use `$(cat /tmp/resolved-path)` or embed the bash-resolved value as a literal string in the prompt text. Do NOT pass `$VAR` directly inside a markdown spawn prompt unless the caller explicitly substitutes it before dispatch.

| Sub-check | Pattern | Severity | Auto-fix |
| -- | -- | -- | -- |
| 42 — unexpanded spawn var | `$VAR` inside ```` ```markdown ```` block | critical | no — requires resolving value before spawn |

## Check 44 — TMPDIR sentinel session scoping

`/tmp` is machine-global. Every `${TMPDIR:-/tmp}/<name>` sentinel must carry the session token — `-${CSID}` (bash), `-{_CSID}`/`_csid()` (python), or the inline `${CLAUDE_CODE_SESSION_ID:-$PPID}` form (used where a preceding `export` line would displace the first-token permission match, e.g. `git`/`cd` blocks) — or a trailing `# tmpdir-exempt: <reason>` marker (valid reasons: `mktemp`, `git-hook-boundary`, `user-shell-boundary`). Bare names collide across concurrent sessions/projects. Canonical pattern: `rules/claude-config.md` §TMPDIR Sentinel Scoping.

```bash
printf "=== Check 44: TMPDIR sentinel session scoping ===\n"
_C44_PAT='TMPDIR:-/tmp}/'  # tmpdir-exempt: lint-self-reference — pattern literal, not a sentinel
grep -rn "$_C44_PAT" . --include='*.md' --include='*.py' --include='*.sh' 2>/dev/null \
  | grep -v 'CSID' | grep -v '_csid' | grep -v 'CLAUDE_CODE_SESSION_ID' | grep -v 'tmpdir-exempt' \
  | grep -v 'rules/claude-config.md' \
  | while IFS= read -r line; do printf "C44-HIGH: unscoped TMPDIR sentinel: %s\n" "$line"; done  # timeout: 10000
```

**Severity**: high — silent cross-session/cross-project state bleed; one session reads another's run-dir, flags, or checkpoints with no error surfaced.

Fix: apply `rules/claude-config.md` §TMPDIR Sentinel Scoping (export `CSID` first line of the block, terminal `-${CSID}` suffix on the sentinel filename); genuinely out-of-session sentinels (git hooks, user-shell-created auth files, mktemp templates) get the `# tmpdir-exempt: <reason>` marker instead.

| Sub-check | Pattern | Severity | Auto-fix |
| -- | -- | -- | -- |
| 44 — unscoped sentinel | `TMPDIR:-/tmp}/` line without session token or `tmpdir-exempt` marker | high | yes — mechanical suffix per claude-config rule |
