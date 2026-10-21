# 🧩 plugins/ — Plugin Authoring & Behavior Reference

This document covers the six Claude Code plugins and their permission model. Codex Rig is a separate peer product with Codex-native packaging, lifecycle, and trust rules; see [`codex-rig/README.md`](codex-rig/README.md).

Authoritative reference: critical behaviors, permission model, known limitations, and user expectations across all six plugins. Read before editing plugin files or diagnosing unexpected behavior.

> Plugin inventory, install instructions, versioning policy, cross-plugin dependency rules: see root `README.md` + `plugins/CLAUDE.md`. This file covers trust model, bin/ execution, operational guarantees only.

<details>
<summary><strong>Contents</strong></summary>

- [Permission Model](#-permission-model)
- [bin/ Script Architecture](#-bin-script-architecture)
- [bin/ Script Principles](#-bin-script-principles)
- [Test Coverage & CI](#-test-coverage--ci)
- [Known Limitations](#-known-limitations)
- [Settings Sync](#-settings-sync)

</details>

______________________________________________________________________

## 🔐 Permission Model

### Trust boundary is at install time, not invocation time

Install plugin = consent to trust its bin/ scripts. Claude Code allow list enforces: once plugin installed, its bin/ executables run without per-invocation approval prompts.

### Scope of trust

`Bash(python:*)` matches any `python ...` invocation — not only `${CLAUDE_PLUGIN_ROOT}/bin/*.py`. By design: same entry covers `python -m pytest`, `python -m cProfile`, etc. Allow list not path-restricted.

More trust boundaries to know:

- **Integrity at install time only**: `claude plugin install` fetches code from marketplace. Nothing verifies marketplace authenticity or pins hash. Marketplace compromised at install time → installed bin/ scripts run without prompts. Accepted threat model for personal dev tool; not appropriate for multi-tenant environments.
- **Updates inherit prior consent**: plugin update ships new bin/\*.py code → runs without re-consent. `/foundry:setup` re-run syncs settings, not re-authorize.
- **No auto-revocation on uninstall**: setup adds shipped permissions and removes explicitly retired exact entries, including identical user-added entries. Uninstall does not remove the remaining plugin allow entries; manual cleanup is required.

### What is pre-approved (in `~/.claude/settings.json`)

All entries merged from `plugins/cc_foundry/.claude-plugin/permissions-allow.json` by `/foundry:setup`. Key entries for plugin execution:

<details>
<summary><strong>Show pre-approved entries</strong></summary>

| Entry                     | What it covers                         | Why                                                                                     |
| ------------------------- | -------------------------------------- | --------------------------------------------------------------------------------------- |
| `Bash(python:*)`          | Broad Python execution stays by design | Plugin helpers and `python -m` recipes depend on it                                     |
| `Bash(eval:*)`            | Broad shell evaluation stays by design | Existing state reload and flag-parsing recipes depend on it; eval migration is separate |
| `Bash(git *:*)` (various) | Read-only git operations               | Standard dev workflow                                                                   |
| `Bash(gh *:*)` (various)  | GitHub CLI read operations             | OSS plugin workflows                                                                    |

</details>

Ad-hoc `find`, `node`, `claude`, and `sed` commands outside shipped blueprint blocks prompt unless a scoped read-only rule covers them. Setup retires the four former broad entries, including identical rules added by hand; Codemap provides the equivalent manual merge in its README.

### What deliberately prompts

<details>
<summary><strong>Show prompt-triggering entries</strong></summary>

| Entry                                       | Reason                                                                                                                                                                                                                                                                        |
| ------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `python -c "..."` inline code               | `python -c` does not match `Bash(python:*)` — Claude Code matcher tokenizes full prefix; separate `Bash(python -c:*)` entry needed (intentionally absent). Check 23a is the policy enforcement; prompt is side-effect of matcher tokenization, not designed security feature. |
| `Bash(python3:*)`                           | Standardized to `python`; `python3` invocations signal unconverted code                                                                                                                                                                                                       |
| `git push`                                  | Push requires explicit user confirmation per session — intentional friction                                                                                                                                                                                                   |
| Any `python*` wildcard beyond bare `python` | Only bare `python:*` added; `python3.11`, `python3.x` etc. still prompt                                                                                                                                                                                                       |

</details>

### Check 23a — inline Python detector

`/audit` Check 23a scans all SKILL.md files for `python -c` and `python <<` patterns, flags HIGH. `Bash(python:*)` allow entry does **not** exempt inline code — matcher requires `Bash(python -c:*)` for that. Check 23a finding → fix by extracting logic to `bin/*.py` script.

______________________________________________________________________

## 🧰 bin/ Script Architecture

### `${CLAUDE_PLUGIN_ROOT}` and the fallback pattern

Every bin/ call uses `${CLAUDE_PLUGIN_ROOT:-plugins/<plugin>}`:

- **Installed** (normal use): `CLAUDE_PLUGIN_ROOT` set by Claude Code to plugin cache path (`~/.claude/plugins/cache/borda-ai-rig/<plugin>/<version>/`).
- **Dev/testing** (local tree, `CLAUDE_PLUGIN_ROOT` unset): falls back to `plugins/<plugin>` — source tree location.

Fallback exists so skills work from both installed cache and local dev tree, no configuration. Never use bare `plugins/<name>/` as primary path — Check C32 flags as violation.

### Two call patterns

**Pattern A — Python scripts (`.py`) via subshell variable assignment:**

```bash
_FS=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/resolve_shared_path.py" foundry skills/_shared 2>/dev/null)  # timeout: 5000
RUN_DIR=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/make_run_dir.py" "skill" ".reports" 2>/dev/null)         # timeout: 5000
```

`VAR=$(...)` form = shell variable assignment — Claude Code permission matcher treats it as shell builtin construct. Inner script not separately matched against allow list. **Note**: observed behavior, not documented guarantee; Claude Code update could change it. ~48 call sites across all plugin SKILL.md files depend on this (see Known Limitations).

**Pattern B — Python scripts (`.py`) via direct call or subshell:**

```bash
# Side-effect call (writes a file, prints findings) — direct invocation
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/check_tag_symmetry.py" .claude/agents/*.md  # timeout: 10000

# Value-producing call — subshell
MEMORY_DIR=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/resolve_memory_dir.py" 2>/dev/null)  # timeout: 5000
```

`Bash(python:*)` in allow list — both forms run without prompts.

### What NOT to do

```bash
# ✗ python3 — not standardized, triggers prompt
python3 "${CLAUDE_PLUGIN_ROOT}/bin/script.py" ...

# ✗ timeout S wrapper — redundant with # timeout: N annotation, adds subprocess fork
_FS=$(timeout 5 python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_shared_path.py" foundry skills/_shared ...)  # timeout: 5000

# ✗ inline python — Check 23a HIGH violation, triggers prompt
RESULT=$(python -c "import json; ...")
```

### Timeout mechanism

Use Claude Code `# timeout: N` annotation (N in milliseconds) on Bash block line. Correct mechanism — tells Bash tool to hard-kill after N ms. `timeout S cmd` shell wrapper NOT needed in SKILL.md context (only valid for scripts invoked outside Claude Code, e.g. CI or standalone shell).

```bash
# ✓ correct
RESULT=$("${CLAUDE_PLUGIN_ROOT}/bin/script.sh" args 2>/dev/null || echo "fallback")  # timeout: 5000
python "${CLAUDE_PLUGIN_ROOT}/bin/script.py" args                                     # timeout: 10000
```

______________________________________________________________________

## 🧰 bin/ Script Principles

- **Token optimisation** — bin/ scripts and prose rules both reduce tokens in SKILL.md; prefer prose when precision-equivalent and shorter; prefer bin/ when logic too complex for prose.
- **Reduce complexity** — keep skill files simple; complex logic belongs in tested executables, not inline.
- **Cross-OS friendly** — bin/ scripts must work on macOS and Linux; avoid `grep -P`, `sed -i ''` vs `sed -i`, other GNU vs BSD differences; Python preferred over bash for portability.
- **Reproducible and precise** — same input, same output; deterministic; no edge-case ambiguity; prose only when it achieves 100% precision and 100% reproducibility.
- **Tests as safety net for non-obvious cases** — tests exist precisely when precision or reproducibility not self-evident from reading script; presence of tests signals script stays executable, not converted to prose.
- **Faster via direct executable call** — one tested executable beats model reasoning through complex inline logic; extract to bin/ when alternative is model-interpreted inline bash.
- **Reusability** — script used by 2+ skills or agents earns `Reuse +2` in extraction score, must stay executable; shared logic belongs in `bin/`, not duplicated inline across multiple `.md` files.

See [bin/ Authoring Guide](cc_foundry/skills/_shared/bin-authoring-guide.md) for full extraction gate, scoring, prose-over-code rules.

______________________________________________________________________

## 🧪 Test Coverage & CI

Each plugin ships pytest coverage for its deterministic helpers and workflow contracts. Tests run on pull requests and pushes to `main` through `ci-tests.yml`, whose executable matrix currently covers 12 combinations: Ubuntu, macOS, and Windows across Python 3.10, 3.11, 3.12, and 3.13.

<details>
<summary><strong>Show coverage table</strong></summary>

| Plugin          | Test modules with test functions | Literal test functions |
| --------------- | -------------------------------: | ---------------------: |
| Foundry         |                               46 |                    814 |
| OSS             |                               34 |                    476 |
| Develop         |                               16 |                    275 |
| Research        |                               14 |                    179 |
| Codemap-py      |                               52 |                   1076 |
| Codex Rig       |                               38 |                    410 |
| bridge_CC-Codex |                                3 |                     80 |

</details>

Snapshot measured from the checked-in tree on 2026-08-19 with `rg '^[[:space:]]*def test_'`; parametrization can produce more executed cases than literal functions. These counts are maintenance evidence, not a claim that every path or runtime environment is covered.

`/audit` Check 23a and Check C32 continuously verify SKILL.md files don't introduce inline Python or bare `plugins/` path references — structural violations caught before reaching users.

The matrix exercises the supported operating-system and Python combinations. A green run proves the configured checks passed for that revision; it does not prove identical behavior outside those checks or in every host/plugin version.

______________________________________________________________________

## 🧭 Known Limitations

### Pattern A passthrough is observed behavior, not a contract

Shell script calls inside `VAR=$(script.sh ...)` work without explicit allow entries. Inferred from production behavior — Claude Code permission matcher appears to treat variable assignment as shell builtin, not descend into `$(...)`. If matcher ever changes, ~48 `.sh` bin/ call sites across all plugins require restructuring. Explicit allow entries not clean mitigation (paths install-path-dependent). No test harness verifies this behavior.

______________________________________________________________________

## 🔄 Settings Sync

`plugins/cc_foundry/.claude-plugin/permissions-allow.json` = canonical allow list for all entries foundry needs. `/foundry:setup` merges into `~/.claude/settings.json` on install. Merge **additive** — entries never removed automatically. Remove entry from `permissions-allow.json` → manually remove from `~/.claude/settings.json` too.

Adding new allow entry:

1. Edit `plugins/cc_foundry/.claude-plugin/permissions-allow.json`
2. Run `/foundry:manage add perm "Bash(X:*)" "description" "use case"` OR manually update `~/.claude/settings.json` + `~/.claude/permissions-guide.md`
3. Re-run `/foundry:setup` to sync symlinks and verify
