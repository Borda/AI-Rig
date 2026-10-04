# 🧰 Codex Rig Scripts

Packaged script examples use the explicit launcher described in [Helper CLI Contract](../shared/helper-cli-contract.md).

`scripts/` holds every executable and library module Codex Rig plugin needs to build, validate, install, and run itself. It contains seven public/maintainer CLI entry points, one import-only role generator, and nine underscore-prefixed internal helpers.

<details open>
<summary><strong>Navigation</strong></summary>

## 📋 Contents

- [Public/maintainer CLIs](#-publicmaintainer-clis)
  - [build_package.py](#build_packagepy)
  - [validate_package.py](#validate_packagepy)
  - [install_global_agents.py](#install_global_agentspy)
  - [install_github_read_rules.py](#install_github_read_rulespy)
  - [manage_role_agents.py](#manage_role_agentspy)
  - [sync_codex.py](#sync_codexpy)
  - [verify_role_link.py](#verify_role_linkpy)
- [Import-only module](#-import-only-module)
  - [generate_roles.py](#generate_rolespy)
- [Internal helpers](#-internal-helpers)

</details>

> Value at a glance: the scripts turn package identity, role-card verification, global-instruction sync, and legacy-shim cleanup into deterministic, inspectable operations.

> Quick start: run `build_package.py --check`, `validate_package.py`, and `manage_role_agents.py doctor` from repository root before investigating installed package or shim.

> Current limits at a glance: `generate_roles.py` is import-only; shim installation is platform-blocked; shim mutation is POSIX-only; and `sync_codex.py` may require runtime network approval for marketplace operations.

## 🧰 Public/maintainer CLIs

### `build_package.py`

<details>
<summary><strong>Manifest generation and checks</strong></summary>

**Purpose:** Generates deterministic Codex Rig installed-package manifest (`package-manifest.json`) by hashing every packaged file, role card, skill, and bootstrap/generator scripts themselves.

**Usage** (verified via `--help`):

```
usage: build_package.py [-h] (--check | --update)

--check   fail when package-manifest.json differs from generation
--update  write current hashes to package-manifest.json
```

**How-to:**

```bash
plugins/codex-rig/bin/python plugins/codex-rig/scripts/build_package.py --update
```

**When-to-use:** After adding, removing, or editing any packaged file (a `ROLE.md`, skill, script) — run `--update` to refresh manifest, then `--check` in CI or pre-commit to confirm manifest still matches tree. The repository's `codex-rig-package-manifest` pre-commit hook also runs `--update` for matching plugin changes.

</details>

### `validate_package.py`

<details>
<summary><strong>Closure and hash validation</strong></summary>

**Purpose:** Validates full role-card-injected package contract and payload closure — that every manifest entry, role card, and skill file manifest references actually exists and hashes correctly, and that packaged set is self-contained with no dangling references.

**Usage** (verified via `--help`):

```
usage: validate_package.py [-h]
```

Takes no flags beyond `-h`; it always runs full validation pass.

**How-to:**

```bash
plugins/codex-rig/bin/python plugins/codex-rig/scripts/validate_package.py
```

**When-to-use:** Before release, or any time `build_package.py --check` alone isn't enough reassurance — this catches closure problems (manifest entry pointing at file that doesn't exist, role card with mismatched hash) that plain hash-diff would miss.

</details>

### `install_global_agents.py`

<details>
<summary><strong>Authenticated global-instruction block management</strong></summary>

**Purpose:** Safely installs, updates, or removes Codex Rig's managed global-instruction block inside a `CODEX_HOME` `AGENTS.md`, using authenticated `sha256`-marked region so it never clobbers user-authored content outside that region.

**Usage** (verified via `--help`):

```
usage: install_global_agents.py [-h] [--source SOURCE] --codex-home CODEX_HOME
                               [--remove | --check | --migrate-legacy-prefix-sha256 DIGEST]

--source SOURCE          packaged assets/AGENTS.md template (required unless --remove)
--codex-home CODEX_HOME  target Codex home
--remove                 strip the managed block instead of installing it
--check                  read-only bounded instruction and legacy-route health check
--migrate-legacy-prefix-sha256 DIGEST
                         explicitly migrate the exact reviewed old global prefix
```

**How-to:**

```bash
plugins/codex-rig/bin/python plugins/codex-rig/scripts/install_global_agents.py \
    --source plugins/codex-rig/assets/AGENTS.md --codex-home ~/.codex
```

**Safety:** Ordinary install refuses an overlapping unmanaged global policy before writes. `--check` examines the proposed installed composition, including incompatible unmarked global policy; an exact unmarked copy of the current template remains adoptable. It detects stale or invalid managed blocks and exact recognized legacy `develop`/`analyse` skill descriptions; optional absence is valid. Sync runs this check before plugin removal, installation, or profile writes, permitting only the precise stale-template-only diagnostic as a normal upgrade and retaining strict post-install health validation. Marketplace refresh precedes this check. Migration authenticates the current managed block, requires the old prefix SHA-256, saves the full original, and preserves custom suffix bytes. It does not preserve custom obligations inside the removed prefix or delete skills. Review the complete replacement diff before migration. Observed concurrent drift rejects replacement, although no portable atomic compare-and-swap prevents the final check/replace race; a verified backup may remain after late rejection.

**When-to-use:** During plugin install/sync (called by `sync_codex.py`'s install path) or when diagnosing a `CODEX_HOME/AGENTS.md` that has stale or missing managed block. Use `--remove` to strip block cleanly, e.g. before uninstalling plugin.

</details>

### `install_github_read_rules.py`

This helper installs the managed Codex-home `local-workflow` and `github-read` permission profiles despite its legacy filename. Both extend `:workspace` and grant `.git` writes under workspace roots. `local-workflow` explicitly disables network; `github-read` allows GitHub destinations through the network proxy when selected. Setup preserves explicit defaults and supplies `default_permissions = "local-workflow"` only when no default exists. State schema 4 migrates verified schemas 1–3, including their plugin-generated defaults and interrupted writes, without replacing explicit user choices. Clear restores original bytes, preserves later user edits, and retains an ordinary `:workspace` default when surviving user profiles require one. No unsandboxed shell, interpreter, gate runner, or Git prefix whitelist is installed. Direct `codex plugin add` does not run setup. This checkout selects `local-workflow`; other projects can start with `codex -c 'default_permissions="local-workflow"'`, or `codex -c 'default_permissions="github-read"'` for GitHub evidence. Runtime restrictions still apply, and destination filtering does not enforce HTTP methods or authorize remote mutation.

> Setup also sets the global `[features].network_proxy = true` setting. It refuses before writing if this would newly affect any existing named permission profile with `network.enabled = true`; an already enabled proxy or a profile with network disabled does not trigger that guard. A sole root `sandbox_mode = "workspace-write"` is replaced by `default_permissions = ":workspace"` during explicit setup or sync, preserving the line's trailing comment and backing up the original config; clear restores the original bytes when the managed config is unchanged. Other root sandbox modes, a root mode mixed with `default_permissions`, any `[sandbox_workspace_write]` table, and `sandbox_mode` in a named `[profiles.<name>]` entry still require manual reconciliation because they can override the selected profile. Removal refuses before writing if restoring the prior proxy setting would turn the proxy off while any remaining named permission profile has `network.enabled = true`. Other loaded configuration layers can still override the opt-in selection; setup validates only the selected Codex home's config. The global proxy's effect on ordinary-session network enforcement has not been live-probed.

<details>
<summary><strong>Managed workflow permission lifecycle</strong></summary>

**Purpose:** Installs or removes Codex Rig's `local-workflow` and `github-read` permission profiles in selected `CODEX_HOME`, validating installed cache location and package identity before changing host permissions.

**Usage:**

```bash
<installed-codex-rig-cache-root>/bin/python <installed-codex-rig-cache-root>/scripts/install_github_read_rules.py \
    --plugin-root <installed-codex-rig-cache-root> --codex-home ~/.codex
<installed-codex-rig-cache-root>/bin/python <installed-codex-rig-cache-root>/scripts/install_github_read_rules.py \
    --remove --codex-home ~/.codex
```

`--plugin-root` is installed Codex Rig cache root, not arbitrary source-tree path. The helper installs or clears the owned `local-workflow` and `github-read` permission profiles. During migration it removes exact owned legacy GitHub reader and PR-collector rule files and canonical-shaped entries in `default.rules` that point to this Codex home's versioned Codex Rig cache; unrelated rules remain untouched. A matching `default.rules` line has no individual ownership marker, so its creator cannot be proved from that file alone.

Install validates the selected cache location, `.codex-plugin/plugin.json` name/version, complete package hashes/closure through the existing package verifier, and the profile it is about to install. It is idempotent. Migration removes the owned legacy rule files and matching canonical-shaped `default.rules` entries, converts a verified older automatic profile to opt-in, converts an unambiguous root workspace sandbox to `:workspace`, and preserves unrelated bytes. Removal clears the owned profiles and recognized legacy entries; unverifiable or foreign state blocks mutation. A first install interrupted after its canonical state write but before config creation can resume on retry; other state-only or unverifiable cases still fail before writes. Later filesystem failures can leave partial changes: each completed update is reported immediately, and the CLI identifies partial failure without claiming rollback.

Before changing `config.toml`, setup and clear parse the proposed TOML and reject changes to unrelated parsed settings. Unrelated multiline strings and nested TOML values are preserved as complete statements, so table-looking content inside them cannot become managed configuration. Multiline values for profile-owned settings remain unsupported and are refused before writes. Collector approvals with a literal launcher in `default.rules` are user-owned and remain byte-for-byte unchanged. Collector grants using the managed interpreter-alternatives shape must match the canonical grant; an unrecognized managed shape stops migration before writes. Python 3.10 requires the `tomli` TOML parser backport for permission-profile edits; Python 3.11+ uses `tomllib`.

> Permission boundary: the profile extends workspace access to GitHub network destinations through the proxy for every sandboxed command in a selected session. It does not override host restrictions or enforce HTTP methods or executable identity. PR fetch and checkout also require write access to the repository's `.git` directory under workspace roots; inherited subagents need that same workspace-root exception. Profile installation does not authorize other workflow-specific local effects or remote mutations.

**When-to-use:** Run this helper during explicit setup or let `sync_codex.py install` invoke it after successful managed-plugin installation, regardless of `--no-codex-global-agents`; that flag skips only `CODEX_HOME/AGENTS.md`. `sync_codex.py clear` invokes `--remove` alongside removal of the managed global-instruction block. Direct plugin installation leaves the profile unchanged. Start a fresh opted-in session with `codex -c 'default_permissions="github-read"'` when GitHub access is needed; ordinary sessions keep an explicit existing default or use the generated `local-workflow` default.

</details>

### `manage_role_agents.py`

<details>
<summary><strong>Legacy shim doctor and authenticated removal</strong></summary>

**Purpose:** Diagnoses and manages complete Codex Rig user-agent shim roster — single tool behind the `agent-shims` skill's `doctor`, `status`, `install`, and `remove` actions.

**Usage** (verified via `--help`):

```
usage: agent-shims [-h] {doctor,status,install,remove}
```

Each action returns one deterministic JSON object on stdout. `doctor` and `status` are zero-write reads; `remove` performs authenticated, plan-then-approve shim removal on POSIX hosts (blocked on Windows); `install` is platform-blocked on supported POSIX and native Windows hosts — it returns `{"classification": "platform-blocked", ...}` because Codex does not yet expose verifiable custom-agent selector for new named-agent activation. Unknown POSIX hosts are rejected earlier with `{"classification": "blocked", ...}`.

**How-to:**

```bash
plugins/codex-rig/bin/python plugins/codex-rig/scripts/manage_role_agents.py doctor
plugins/codex-rig/bin/python plugins/codex-rig/scripts/manage_role_agents.py remove
```

**When-to-use:** Run `doctor` any time you want read-only health check of shim roster (this is also what plugin's `startup`/`resume` hook runs automatically). Run `remove` to clean up thin shims left behind by prior development, especially before or after uninstalling plugin.

</details>

### `sync_codex.py`

<details>
<summary><strong>Cross-platform install, refresh, and clear</strong></summary>

**Purpose:** Installs, refreshes, or removes Codex Rig and Codemap without depending on POSIX shell — resolves system commands cross-platform (including Windows batch-file launchers) and drives marketplace plugin install/clear flow plus Codex Rig's managed global-instruction block and `github-read` permission profile.

**Usage** (verified via `--help`):

```
usage: sync_codex.py [-h] [--codex-ref CODEX_REF] [--no-clean]
                     [--no-codex-global-agents]
                     [{install,clear}]

--codex-ref CODEX_REF      Git ref to pin; default follows the marketplace default branch
--no-clean                 skip managed-plugin removal before reinstalling
--no-codex-global-agents   leave CODEX_HOME/AGENTS.md unchanged
```

`action` defaults to `install` when omitted.

**How-to:**

```bash
plugins/codex-rig/bin/python plugins/codex-rig/scripts/sync_codex.py install
plugins/codex-rig/bin/python plugins/codex-rig/scripts/sync_codex.py install --no-clean
plugins/codex-rig/bin/python plugins/codex-rig/scripts/sync_codex.py clear
```

**When-to-use:** The top-level entry point for getting Codex Rig, Codemap, and Bridge onto machine or off it — this is what repo's `Makefile` calls for Codex side of installation. Python 3.10 requires `tomli` in the invoking interpreter; sync checks this before either action can change local state. Install refreshes or registers canonical Git marketplace, verifies selected-source package hashes/closure and helper availability, then removes managed plugins by default, reinstalls them, and installs the `github-read` profile. Unsupported configured pins stop before marketplace/plugin mutation; newly registered sources are inspected before plugin removal/add. Clear removes the owned profile and managed global-instruction block before managed plugins. Use `--no-clean` to retain installed plugins before reinstalling without suppressing marketplace refresh, `--codex-ref` to pin specific marketplace ref instead of tracking default branch, and `--no-codex-global-agents` when you manage `CODEX_HOME/AGENTS.md` yourself and don't want `sync_codex.py` touching that file; it does not skip profile installation or removal. These remain `sync_codex.py`'s own CLI flags — Makefile's `install-codex-plugins` target simply calls it without passing any of them. Restart existing Codex sessions after sync.

</details>

### `verify_role_link.py`

<details>
<summary><strong>Bootstrap role-card verification</strong></summary>

**Purpose:** Verifies and emits one role card from currently enabled Codex Rig package — bootstrap helper every generated shim's `developer_instructions` tells Codex to invoke, with exact plugin root, role id, and expected hashes, before trusting that role's card bytes.

**Usage** (from `generate_roles.py`'s `_render_shim`, authoritative caller — this script uses `add_help=False`, so it has no `--help` text of its own):

```
verify_role_link.py --plugin-root PLUGIN_ROOT --role ROLE_ID --role-sha256 ROLE_SHA256 \
  --manifest-sha256 MANIFEST_SHA256 --helper-sha256 HELPER_SHA256 \
  --codex-binary CODEX_BINARY --codex-sha256 CODEX_SHA256
```

All seven value flags are required. On success stdout starts with a protocol-1 ok envelope for the role, followed by the `--- codex-rig-role-card ---` separator and the verified card bytes; on failure it prints a JSON object with `status: "codex-rig-role-unavailable"` and a reason.

**How-to:** Internal — not invoked directly by maintainer. Every generated shim TOML already embeds its exact `argv` for this script; Codex itself runs that `argv` before trusting role. To reproduce shim's exact invocation for debugging, copy the `argv` JSON array out of shim's `developer_instructions` block and run it as-is:

```bash
plugins/codex-rig/bin/python plugins/codex-rig/scripts/verify_role_link.py --plugin-root /path/to/codex-rig \
    --role sw-engineer --role-sha256 <sha> --manifest-sha256 <sha> --helper-sha256 <sha> \
    --codex-binary /path/to/codex --codex-sha256 <sha>
```

**When-to-use:** When shim is rejecting role and you need to see exact verifier failure reason without going through Codex's own invocation path.

</details>

## 🧭 Shared runtime helper

### `shared/adversarial_loop.py`

<details>
<summary><strong>Ledger validation and in-turn progress transcript</strong></summary>

**Purpose:** Validates one bounded adversarial-review ledger and emits its deterministic JSON summary. Current schema 2 keeps a small `loop-ledger.json` header and an append-only `loop-rounds.jsonl` round log; historical schema-1 files with inline rounds stay readable, but current results require schema 2. Only `--append` writes: it validates the round staged in `loop-rounds.jsonl.rec` against the earlier rounds, appends it as one line, and deletes the staged file, or exits nonzero and appends nothing. The optional `--progress` flag writes the complete cumulative progress table and legend to stderr while keeping JSON stdout unchanged. The optional `--actions` flag binds parent triage and resolution records to every open finding; it checks completeness, not the truth of a claimed fix.

**Usage** (contract; the progress rendering is exercised by the adversarial-loop test suite):

```bash
plugins/codex-rig/bin/python plugins/codex-rig/shared/adversarial_loop.py --ledger <run-directory>/loop-ledger.json --append --progress
plugins/codex-rig/bin/python plugins/codex-rig/shared/adversarial_loop.py --ledger <run-directory>/loop-ledger.json --actions <run-directory>/loop-actions.json
```

The progress table has exactly `Iteration | Critical | High | Medium | Low | Nits | Weighted score`. Every numeric cell is literal `old + new`: currently open signatures seen in any prior round, including signatures that were closed and later reopened, plus signatures first seen in the current round. Only `open` and `fixed-pending-verification` count; security and critical combine in the display, while scoring retains weights `20/10/6/4/2/1`. Invoke after each newly completed validated round; an empty ledger prints no progress table or unreviewed zero row. Feasible structural or repeated findings may continue when score and authority permit; plateau, nonconvergence, unavailable evidence, the third occurrence of the same open signature and stricter caller budgets still stop their affected routes. No arbitrary three-review cap applies to strictly decreasing unchanged-scope integer scores.

`--require-clean` remains an independent exit-status gate. The final adversarial-loop result table is not replaced by this in-turn stderr transcript.

The shared artifact validator requires `action_contract_version: 2` and a schema-2 `loop-actions.json` for both candidate and final `challenge-resolve` results. Every `fix` action records nonempty `invariant:`, `original:`, `consumer:`, `sibling:`, and `source-paths:` evidence entries; the checker cannot prove the claims true. The CLI can inspect schema-1 archives, but older files have no unverified archive exception in current result validation.

</details>

## 🧰 Import-only module

### `generate_roles.py`

<details open>
<summary><strong>Import-only role-shim generator</strong></summary>

**Purpose:** Deterministically renders thin-role shim bytes (`codex-rig-<role_id>.toml`) — exact verifier `argv`, marker line, and `developer_instructions` block generated shim must contain — from validated roster of role cards, package manifest, and bootstrap verifier.

**Usage:** This module has no `argparse` entry point and no `if __name__ == "__main__"` guard — it is not CLI. It is imported directly by `manage_role_agents.py`, the `_agent_shim_*` helpers, and test suite for its public functions (`load_generated_roster`, `render_role_shims`, `generate_role_shims`, `roster_identity_hash`) and constants (`ROLE_IDS`, `RUNTIME_KEYS`, `FRONTMATTER_KEYS`).

**How-to** (from Python, not shell):

```python
from generate_roles import load_generated_roster

roster = load_generated_roster(
    plugin_root,
    install_id=install_id,
    python_executable=python_executable,
    python_executable_hash=python_executable_hash,
    codex_binary=codex_binary,
    codex_binary_hash=codex_binary_hash,
)
```

**When-to-use:** Internal — not invoked directly. Reach for it when you need to reproduce or test what generated shim's exact bytes should be; day-to-day shim diagnosis goes through `manage_role_agents.py` instead.

</details>

## 🧰 Internal helpers

<details>
<summary><strong>Fail-closed lifecycle and package-identity helpers</strong></summary>

The nine underscore-prefixed modules are library code only — none defines CLI, and each is imported by name from `manage_role_agents.py`, `verify_role_link.py`, `build_package.py`, `validate_package.py`, or one another. Together, `_agent_shim_observe.py`, `_agent_shim_plan.py`, `_agent_shim_approval.py`, and `_agent_shim_transaction.py` form shim lifecycle's transaction pipeline:

1. **`_agent_shim_observe.py`** (internal — not invoked directly) — observes Codex Rig shim lifecycle filesystem evidence (what shim files exist, their hashes and states) without performing any writes.
2. **`_agent_shim_plan.py`** (internal — not invoked directly) — derives immutable shim operation candidates (what install or remove *would* do) purely from observed evidence, without touching filesystem.
3. **`_agent_shim_approval.py`** (internal — not invoked directly) — binds complete convergence approval (exact bytes user or automated caller approves) from read-only lifecycle evidence and candidate plan.
4. **`_agent_shim_transaction.py`** (internal — not invoked directly) — executes already-approved transaction, and can roll it back; this is only stage of four that writes to disk.

Every transaction produces entry validated by **`_agent_shim_journal.py`** (internal — not invoked directly), which parses and validates immutable transaction journals without touching filesystem — audit trail proving what transaction did. **`_agent_shim_lifecycle.py`** (internal — not invoked directly) parses and classifies that lifecycle evidence into states `manage_role_agents.py`'s `doctor`/`status` actions report. Transaction operations use **`_agent_shim_posix.py`** (internal — not invoked directly) for fail-closed POSIX primitives. Observation and package-identity helpers retain their own bounded filesystem checks, so `_agent_shim_posix.py` is not universal wrapper for every filesystem operation in pipeline; each stage rejects unsafe conditions (symlink races, unexpected file types, concurrent drift) rather than proceeding on assumption.

The remaining two helpers back package identity rather than shim lifecycle: **`_package_identity.py`** (internal — not invoked directly) verifies one complete installed Codex Rig package without any lifecycle writes — same verification `build_package.py` and `validate_package.py` both import — and **`_safe_package_io.py`** (internal — not invoked directly) reads installed-package inputs through bounded, no-link filesystem handles so that verification itself can't be tricked by symlinked or oversized file.

The result is pipeline that is observe-then-plan-then-approve-then-execute at every step, journalled so every transaction is auditable after fact, and fail-closed throughout: any stage that can't prove safe precondition refuses rather than guesses.

</details>
