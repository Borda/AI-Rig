# 🧰 Codex Rig Scripts

`scripts/` holds every executable and library module the Codex Rig plugin needs to build, validate, install, and run itself. It contains seven public/maintainer CLI entry points, one import-only role generator, and nine underscore-prefixed internal helpers.

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

> Quick start: run `build_package.py --check`, `validate_package.py`, and `manage_role_agents.py doctor` from the repository root before investigating an installed package or shim.

> Current limits at a glance: `generate_roles.py` is import-only; shim installation is platform-blocked; shim mutation is POSIX-only; and `sync_codex.py` may require runtime network approval for marketplace operations.

## 🧰 Public/maintainer CLIs

### `build_package.py`

<details>
<summary><strong>Manifest generation and checks</strong></summary>

**Purpose:** Generates the deterministic Codex Rig installed-package manifest (`package-manifest.json`) by hashing every packaged file, role card, skill, and the bootstrap/generator scripts themselves.

**Usage** (verified via `--help`):

```
usage: build_package.py [-h] (--check | --update)

--check   fail when package-manifest.json differs from generation
--update  write current hashes to package-manifest.json
```

**How-to:**

```bash
python3 plugins/codex-rig/scripts/build_package.py --update
```

**When-to-use:** After adding, removing, or editing any packaged file (a `ROLE.md`, a skill, a script) — run `--update` to refresh the manifest, then `--check` in CI or pre-commit to confirm the manifest still matches the tree. The repository's `codex-rig-package-manifest` pre-commit hook also runs `--update` for matching plugin changes.

</details>

### `validate_package.py`

<details>
<summary><strong>Closure and hash validation</strong></summary>

**Purpose:** Validates the full role-card-injected package contract and payload closure — that every manifest entry, role card, and skill file the manifest references actually exists and hashes correctly, and that the packaged set is self-contained with no dangling references.

**Usage** (verified via `--help`):

```
usage: validate_package.py [-h]
```

Takes no flags beyond `-h`; it always runs the full validation pass.

**How-to:**

```bash
python3 plugins/codex-rig/scripts/validate_package.py
```

**When-to-use:** Before a release, or any time `build_package.py --check` alone isn't enough reassurance — this catches closure problems (a manifest entry pointing at a file that doesn't exist, a role card with a mismatched hash) that a plain hash-diff would miss.

</details>

### `install_global_agents.py`

<details>
<summary><strong>Authenticated global-instruction block management</strong></summary>

**Purpose:** Safely installs, updates, or removes Codex Rig's managed global-instruction block inside a `CODEX_HOME` `AGENTS.md`, using an authenticated `sha256`-marked region so it never clobbers user-authored content outside that region.

**Usage** (verified via `--help`):

```
usage: install_global_agents.py [-h] [--source SOURCE] --codex-home CODEX_HOME [--remove]

--source SOURCE          packaged assets/AGENTS.md template (required unless --remove)
--codex-home CODEX_HOME  target Codex home
--remove                 strip the managed block instead of installing it
```

**How-to:**

```bash
python3 plugins/codex-rig/scripts/install_global_agents.py \
    --source plugins/codex-rig/assets/AGENTS.md --codex-home ~/.codex
```

**When-to-use:** During plugin install/sync (called by `sync_codex.py`'s install path) or when diagnosing a `CODEX_HOME/AGENTS.md` that has a stale or missing managed block. Use `--remove` to strip the block cleanly, e.g. before uninstalling the plugin.

</details>

### `install_github_read_rules.py`

<details>
<summary><strong>Managed GitHub reader-rule lifecycle</strong></summary>

**Purpose:** Installs or removes the Codex Rig GitHub reader rules in the selected `CODEX_HOME`, validating the installed cache location and package identity before approving its wrapper path.

**Usage:**

```bash
python3 <installed-codex-rig-cache-root>/scripts/install_github_read_rules.py \
    --plugin-root <installed-codex-rig-cache-root> --codex-home ~/.codex
python3 <installed-codex-rig-cache-root>/scripts/install_github_read_rules.py \
    --remove --codex-home ~/.codex
```

`--plugin-root` is the installed Codex Rig cache root, not an arbitrary source-tree path. The helper owns `CODEX_HOME/rules/codex-rig-github-read.rules`; during migration it may also rewrite `CODEX_HOME/rules/default.rules` to remove only exact canonical two-token legacy reader allow entries. Its managed content grants the literal `python`/`python3` launcher union and the installed wrapper-path allow-rule union, including native and POSIX path spellings on Windows. It does not add broad Python or `gh` grants and does not change network settings.

Install validates the selected cache location, `.codex-plugin/plugin.json` name/version, complete package hashes/closure through the existing package verifier, ordinary reader wrapper, and canonical managed-rule body. It is idempotent and regenerates the rule after an installed-package version change. All required existing-file backups are prepared and verified before either rules file changes; creation and no-op need no backup. Migration removes only exact canonical two-token legacy reader allow entries from `default.rules` under the same Codex home/cache and preserves every unrelated byte. Removal deletes only an owned, canonical, integrity-valid rules file; an unverifiable or foreign file blocks rules mutation. Later filesystem failures can leave partial changes: each completed rules update is reported immediately, and the CLI identifies partial failure without claiming rollback.

> Trust boundary: package hashes prove setup-time consistency, not publisher authenticity or immutable code. Reusable approval trusts the installed cache for its lifetime. A same-user process can replace code or rehash its manifest after setup; these checks do not prevent that. Never use an untrusted or shared-writable cache as an approval target.

**When-to-use:** `sync_codex.py install` invokes this helper after successful managed-plugin installation, regardless of `--no-codex-global-agents`; that flag skips only `CODEX_HOME/AGENTS.md`. `sync_codex.py clear` invokes `--remove` alongside removal of the managed global-instruction block. Direct plugin installation remains inert until an explicit setup or sync invokes this helper. Restart existing Codex sessions after rules are installed, regenerated, or removed.

</details>

### `manage_role_agents.py`

<details>
<summary><strong>Legacy shim doctor and authenticated removal</strong></summary>

**Purpose:** Diagnoses and manages the complete Codex Rig user-agent shim roster — the single tool behind the `agent-shims` skill's `doctor`, `status`, `install`, and `remove` actions.

**Usage** (verified via `--help`):

```
usage: agent-shims [-h] {doctor,status,install,remove}
```

Each action returns one deterministic JSON object on stdout. `doctor` and `status` are zero-write reads; `remove` performs an authenticated, plan-then-approve shim removal on POSIX hosts (blocked on Windows); `install` is platform-blocked on supported POSIX and native Windows hosts — it returns `{"classification": "platform-blocked", ...}` because Codex does not yet expose a verifiable custom-agent selector for new named-agent activation. Unknown POSIX hosts are rejected earlier with `{"classification": "blocked", ...}`.

**How-to:**

```bash
python3 plugins/codex-rig/scripts/manage_role_agents.py doctor
python3 plugins/codex-rig/scripts/manage_role_agents.py remove
```

**When-to-use:** Run `doctor` any time you want a read-only health check of the shim roster (this is also what the plugin's `startup`/`resume` hook runs automatically). Run `remove` to clean up thin shims left behind by prior development, especially before or after uninstalling the plugin.

</details>

### `sync_codex.py`

<details>
<summary><strong>Cross-platform install, refresh, and clear</strong></summary>

**Purpose:** Installs, refreshes, or removes Codex Rig and Codemap without depending on a POSIX shell — resolves system commands cross-platform (including Windows batch-file launchers) and drives the marketplace plugin install/clear flow plus Codex Rig's managed global-instruction block and GitHub reader rules.

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
python3 plugins/codex-rig/scripts/sync_codex.py install
python3 plugins/codex-rig/scripts/sync_codex.py install --no-clean
python3 plugins/codex-rig/scripts/sync_codex.py clear
```

**When-to-use:** The top-level entry point for getting Codex Rig, Codemap, and Bridge onto a machine or off it — this is what the repo's `Makefile` calls for the Codex side of installation. Install refreshes or registers the canonical Git marketplace, verifies selected-source package hashes/closure and helper availability, then removes the managed plugins by default, reinstalls them, and installs or regenerates the GitHub reader rules. Unsupported configured pins stop before marketplace/plugin mutation; newly registered sources are inspected before plugin removal/add. Clear removes the owned reader rules and managed global-instruction block before the managed plugins. Use `--no-clean` to retain installed plugins before reinstalling without suppressing marketplace refresh, `--codex-ref` to pin a specific marketplace ref instead of tracking the default branch, and `--no-codex-global-agents` when you manage `CODEX_HOME/AGENTS.md` yourself and don't want `sync_codex.py` touching that file; it does not skip reader-rule installation or removal. These remain `sync_codex.py`'s own CLI flags — the Makefile's `install-codex-plugins` target simply calls it without passing any of them. Restart existing Codex sessions after sync.

</details>

### `verify_role_link.py`

<details>
<summary><strong>Bootstrap role-card verification</strong></summary>

**Purpose:** Verifies and emits one role card from the currently enabled Codex Rig package — the bootstrap helper every generated shim's `developer_instructions` tells Codex to invoke, with the exact plugin root, role id, and expected hashes, before trusting that role's card bytes.

**Usage** (from `generate_roles.py`'s `_render_shim`, the authoritative caller — this script uses `add_help=False`, so it has no `--help` text of its own):

```
verify_role_link.py --plugin-root PLUGIN_ROOT --role ROLE_ID --role-sha256 ROLE_SHA256 \
  --manifest-sha256 MANIFEST_SHA256 --helper-sha256 HELPER_SHA256 \
  --codex-binary CODEX_BINARY --codex-sha256 CODEX_SHA256
```

All seven value flags are required. On success stdout starts with a protocol-1 ok envelope for the role, followed by the `--- codex-rig-role-card ---` separator and the verified card bytes; on failure it prints a JSON object with `status: "codex-rig-role-unavailable"` and a reason.

**How-to:** Internal — not invoked directly by a maintainer. Every generated shim TOML already embeds its exact `argv` for this script; Codex itself runs that `argv` before trusting a role. To reproduce a shim's exact invocation for debugging, copy the `argv` JSON array out of the shim's `developer_instructions` block and run it as-is:

```bash
python3 plugins/codex-rig/scripts/verify_role_link.py --plugin-root /path/to/codex-rig \
    --role sw-engineer --role-sha256 <sha> --manifest-sha256 <sha> --helper-sha256 <sha> \
    --codex-binary /path/to/codex --codex-sha256 <sha>
```

**When-to-use:** When a shim is rejecting a role and you need to see the exact verifier failure reason without going through Codex's own invocation path.

</details>

## 🧰 Import-only module

### `generate_roles.py`

<details open>
<summary><strong>Import-only role-shim generator</strong></summary>

**Purpose:** Deterministically renders thin-role shim bytes (`codex-rig-<role_id>.toml`) — the exact verifier `argv`, marker line, and `developer_instructions` block a generated shim must contain — from a validated roster of role cards, the package manifest, and the bootstrap verifier.

**Usage:** This module has no `argparse` entry point and no `if __name__ == "__main__"` guard — it is not a CLI. It is imported directly by `manage_role_agents.py`, the `_agent_shim_*` helpers, and the test suite for its public functions (`load_generated_roster`, `render_role_shims`, `generate_role_shims`, `roster_identity_hash`) and constants (`ROLE_IDS`, `RUNTIME_KEYS`, `FRONTMATTER_KEYS`).

**How-to** (from Python, not the shell):

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

**When-to-use:** Internal — not invoked directly. Reach for it when you need to reproduce or test what a generated shim's exact bytes should be; day-to-day shim diagnosis goes through `manage_role_agents.py` instead.

</details>

## 🧰 Internal helpers

<details>
<summary><strong>Fail-closed lifecycle and package-identity helpers</strong></summary>

The nine underscore-prefixed modules are library code only — none defines a CLI, and each is imported by name from `manage_role_agents.py`, `verify_role_link.py`, `build_package.py`, `validate_package.py`, or one another. Together, `_agent_shim_observe.py`, `_agent_shim_plan.py`, `_agent_shim_approval.py`, and `_agent_shim_transaction.py` form the shim lifecycle's transaction pipeline:

1. **`_agent_shim_observe.py`** (internal — not invoked directly) — observes Codex Rig shim lifecycle filesystem evidence (what shim files exist, their hashes and states) without performing any writes.
2. **`_agent_shim_plan.py`** (internal — not invoked directly) — derives immutable shim operation candidates (what an install or remove *would* do) purely from observed evidence, without touching the filesystem.
3. **`_agent_shim_approval.py`** (internal — not invoked directly) — binds a complete convergence approval (the exact bytes a user or automated caller approves) from the read-only lifecycle evidence and candidate plan.
4. **`_agent_shim_transaction.py`** (internal — not invoked directly) — executes an already-approved transaction, and can roll it back; this is the only stage of the four that writes to disk.

Every transaction produces an entry validated by **`_agent_shim_journal.py`** (internal — not invoked directly), which parses and validates immutable transaction journals without touching the filesystem — the audit trail proving what a transaction did. **`_agent_shim_lifecycle.py`** (internal — not invoked directly) parses and classifies that lifecycle evidence into the states `manage_role_agents.py`'s `doctor`/`status` actions report. Transaction operations use **`_agent_shim_posix.py`** (internal — not invoked directly) for fail-closed POSIX primitives. Observation and package-identity helpers retain their own bounded filesystem checks, so `_agent_shim_posix.py` is not a universal wrapper for every filesystem operation in the pipeline; each stage rejects unsafe conditions (symlink races, unexpected file types, concurrent drift) rather than proceeding on an assumption.

The remaining two helpers back package identity rather than the shim lifecycle: **`_package_identity.py`** (internal — not invoked directly) verifies one complete installed Codex Rig package without any lifecycle writes — the same verification `build_package.py` and `validate_package.py` both import — and **`_safe_package_io.py`** (internal — not invoked directly) reads installed-package inputs through bounded, no-link filesystem handles so that verification itself can't be tricked by a symlinked or oversized file.

The result is a pipeline that is observe-then-plan-then-approve-then-execute at every step, journalled so every transaction is auditable after the fact, and fail-closed throughout: any stage that can't prove a safe precondition refuses rather than guesses.

</details>
