# Install Checks — I1, I2, I3

Checks validate post-install state in `~/.claude/`. Operate on home dir, not project `.claude/`. Run via `/foundry:audit setup` (or `/audit setup` after `foundry:setup link`).

## Check I1 — Plugin cache intact

Verify foundry plugin installed, cache dir accessible.

```bash
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/check_install_state.py" --check I1  # timeout: 10000
```

**Severity**: missing/broken cache → **high** (plugin non-functional).

## Check I2 — Settings merge complete

Verify `foundry:setup` ran: `~/.claude/settings.json` has required entries, no stale hooks block.

```bash
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/check_install_state.py" --check I2  # timeout: 10000
```

**Severity**: missing entry or stale hooks block → **medium** per sub-check (non-blocking, degrades functionality). Fix: re-run `/foundry:setup` — idempotent.

## Check I3 — Link health (conditional)

Two distinct expectations, both asserted here:

- `~/.claude/agents/` and `~/.claude/skills/` must contain **zero** foundry symlinks. Both namespaces dispatch from the plugin (`foundry:sw-engineer`, `/foundry:audit`); a skills link additionally registers a user-level skill that shadows Claude Code's bundled skill of the same name. `/foundry:setup` Step 10 Phase 1 purges both.
- `~/.claude/rules/` and `~/.claude/TEAM_PROTOCOL.md` **are** symlinked, so those are checked for staleness — they break silently when a version upgrade moves the cache path.

```bash
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/check_install_state.py" --check I3  # timeout: 10000
```

**Severity**: both → **high**. A foundry symlink under `agents/` or `skills/` shadows namespaced dispatch; a broken `rules/` symlink silently drops a rule file. Fix for either: re-run `/foundry:setup` (no `link` subcommand exists — `argument-hint` is `[--approve]`).

## Check R1 — Computed path resolution (local + installed duality)

Root cause guard for `adversarial.md` / `upgrade.md` silent-deletion bug class. Skill `.md` files construct paths via variable substitution (`$AUDIT_TPL/../modes/upgrade.md`, `$_FS/task-hygiene.md`, `${CLAUDE_PLUGIN_ROOT:-plugins/<x>}/bin/<script>`). Those paths exist as literal strings only if target filename is grep-visible. File existing locally but never copied to installed plugin cache silently fails for users who install plugin.

**What it checks**: for every computed-path reference in `plugins/*/skills/*/SKILL.md`, `plugins/*/skills/*/modes/*.md`, `plugins/*/agents/*.md` — verify resolved target exists both locally (`plugins/<plugin>/...`) and in installed cache (`~/.claude/plugins/cache/borda-ai-rig/<plugin>/*/<path>`). Skip if `LOCAL_MODE != true` (no plugin source tree to scan).

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
printf "=== Check R1: Computed path resolution (local + installed duality) ===\n"
if [ "$LOCAL_MODE" != "true" ]; then
    printf "✓: Check R1 skipped in non-local mode (no plugin source tree)\n"
else
    python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/check_routing_links.py" \
        --plugins-dir plugins \
        --installed-plugins-json ~/.claude/plugins/installed_plugins.json \
        --check R1  # timeout: 15000
fi
```

**Severity**:

- `R1-FAIL` (file exists locally but absent from installed cache) → **high** — users who install plugin get broken dispatch at runtime; likely file added locally but plugin not re-installed
- `R1-WARN` (file exists in installed cache but absent locally) → **medium** — stale installed copy; breaks after plugin update
- `R1-INFO` (plugin not installed) → **low/info** — cannot verify installed state; note in report only

Fix: re-install plugin with `claude plugin install <plugin>@borda-ai-rig` to sync installed state with local source tree. WARN: restore missing local file or remove reference.

## Check R2 — Grep-visible referencing (orphan-risk detection)

Structural guard: for every `.md` file in `plugins/*/skills/*/modes/`, `plugins/*/skills/*/templates/`, `plugins/*/skills/_shared/` — verify its **basename** appears as literal string in ≥1 consumer `.md` file in same plugin.

**Scope**: `modes/`, `templates/`, `_shared/` only. SKILL.md and agent `.md` files covered by Check 32a (checks-skills.md); R2 complementary — covers subdirectories 32a does not walk.

**Why**: grep-based dead-file checks (Check 32a, 32b) and agent zero-hit analysis search for filename. File loaded only via computed path (e.g. `$AUDIT_TPL/../modes/adversarial.md`) has zero literal-basename hits → grep tools conclude unreferenced → deletion risk.

Skip if `LOCAL_MODE != true`.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
printf "=== Check R2: Grep-visible referencing (orphan-risk detection) ===\n"
if [ "$LOCAL_MODE" != "true" ]; then
    printf "✓: Check R2 skipped in non-local mode (no plugin source tree)\n"
else
    python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/check_routing_links.py" \
        --plugins-dir plugins \
        --check R2  # timeout: 15000
fi
```

**Severity**: `R2-ORPHAN-RISK` → **medium** — file is grep-invisible; any automated or agent-assisted dead-file sweep will incorrectly flag as unreferenced and may delete it.

Fix: add comment in consumer `SKILL.md` making basename a literal string, e.g.:

```
# loads: adversarial.md  (via $AUDIT_TPL/../modes/adversarial.md)
```

This single-line comment costs ~5 tokens and permanently protects file from grep-based false-positive orphan detection.

## Check R3 — bin/ script reference integrity (reverse of Check 32d)

Check 32d walks `bin/` scripts and flags those unreferenced by any `.md` file (orphaned scripts). R3 is the reverse: for every `${CLAUDE_PLUGIN_ROOT:-plugins/<x>}/bin/<script>` reference in any plugin `.md` file, verify script exists locally — catches typos, deleted scripts, refactor leftovers leaving dangling references.

Skip if `LOCAL_MODE != true`.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
printf "=== Check R3: bin/ script existence (local + installed) ===\n"
if [ "$LOCAL_MODE" != "true" ]; then
    printf "✓: Check R3 skipped in non-local mode (no plugin source tree)\n"
else
    python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/check_routing_links.py" \
        --plugins-dir plugins \
        --installed-plugins-json ~/.claude/plugins/installed_plugins.json \
        --check R3  # timeout: 15000
fi
```

**Severity**:

- `R3-FAIL` (script referenced but missing locally) → **high** — skill dispatch fails immediately at `python ...` call site
- `R3-WARN` (script exists locally but absent from installed cache) → **high** — same as R1-FAIL but for bin/ scripts; users get broken skill at runtime after install

Fix: create missing script locally (FAIL) or re-install plugin to sync (WARN).

> **Convenience shortcut**: run all three checks together:
>
> ```bash
> python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/check_routing_links.py" \
>     --plugins-dir plugins \
>     --installed-plugins-json ~/.claude/plugins/installed_plugins.json  # timeout: 20000
> ```
>
> Omitting `--check` runs R1, R2, R3 in one pass.

| Sub-check | Condition | Severity | Auto-fix |
| -- | -- | -- | -- |
| R1-FAIL — local-only file | resolved file exists locally but absent from installed cache | high | no — re-install plugin |
| R1-WARN — installed-only file | file exists in cache but missing locally | medium | no — restore local file or remove ref |
| R1-INFO — plugin not installed | cannot verify installed state | info | n/a |
| R2-ORPHAN-RISK — grep-invisible | basename not literal in any consumer .md file | medium | no — add `# loads: <basename>` comment |
| R3-FAIL — bin/ script missing locally | script referenced but local file absent | high | no — create script |
| R3-WARN — bin/ script missing from cache | script local but absent from installed cache | high | no — re-install plugin |

## Check R4 — bin/ Python test coverage

For every `plugins/<plugin>/bin/<script>.py`, verify corresponding `plugins/<plugin>/tests/test_<script>.py` exists and non-empty. Skip if `LOCAL_MODE != true`.

**Implementation note**: `check_orphaned_bin.py` lacks `--check-tests`; until that flag is added, run the check inline:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/check_bin_test_coverage.py" $( [ "$LOCAL_MODE" = "true" ] && echo "--local" )  # timeout: 30000
```

**Severity**:

- `R4-FAIL` (any sub-check) → **medium** — plugin policy violation; new bin/ scripts must ship with tests containing real assertions

Fix: create `tests/test_<basename>.py` with at minimum one test class covering script's public API.

| Sub-check | Condition | Severity | Auto-fix |
| -- | -- | -- | -- |
| R4-FAIL — missing test file | `bin/*.py` with no matching `tests/test_*.py` | medium | no — write tests |
| R4-FAIL — empty test file | `tests/test_*.py` exists but has zero size | medium | no — populate test file |
| R4-FAIL — no test functions | test file non-empty but has zero `def test_` functions | medium | no — write tests |
| R4-FAIL — stub tests only | all `def test_` functions body is only `pass` or `...` | medium | no — implement assertions |

Note: the `_*.py` exclusion (e.g., `_schema.py`) is intentional only for pure type-definition modules with no runnable logic. Files with `__name__ == "__main__"` guards must have tests regardless of leading underscore. Auditor verifies exclusion is appropriate per file when `_FAIL` reports are absent.

## Check R5 — Consumer→template orphan (reverse of R2)

Check R2 verifies every template file has its basename visible in a consumer `.md` file. R5 is the reverse: for every `<!-- loads: X -->` or `# loads: X` comment in any `.md` file, verify `X` exists on disk (locally or in installed cache).

Catches deleted or renamed templates where consumer `<!-- loads: -->` comment was not updated — silent runtime failure when audit tries to `Read $AUDIT_TPL/X`.

Skip if `LOCAL_MODE != true`.

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r LOCAL_MODE < "${TMPDIR:-/tmp}/audit-state-${CSID}/local-mode" 2>/dev/null || LOCAL_MODE="false"
printf "=== Check R5: Consumer→template orphan ===\n"
if [ "$LOCAL_MODE" != "true" ]; then
    printf "✓: Check R5 skipped in non-local mode\n"
else
    grep -rn "loads:\s\+\([a-zA-Z0-9_-]\+\.md\)" plugins/ \
        --include="*.md" -o 2>/dev/null \
    | sed 's/.*loads:[[:space:]]*//' \
    | sort -u \
    | while IFS= read -r target; do
        found=$(find plugins -name "$target" 2>/dev/null | head -1)
        if [ -z "$found" ]; then
            printf "R5-FAIL: loads: %s — file not found in plugins/ tree\n" "$target"
        fi
    done  # timeout: 10000
fi
```

**Severity**: medium — audit itself may crash at runtime trying to read missing template; silent breakage for users. Fix: restore missing template file or remove/update stale `<!-- loads: -->` comment in consumer.

| Sub-check | Condition | Severity | Auto-fix |
| -- | -- | -- | -- |
| R5-FAIL — missing template | `loads: X` comment but `X` not found on disk | medium | no — restore file or remove dead comment |
