---
name: sync
description: Dry-run active plugin cache drift; refresh/reinstall only with approval; keep shims separate.
---

> Before asking, read [User Questions](../../shared/codex-user-questions.md).

# Sync

Inspect and refresh the public-GitHub Codex Rig plugin through supported Codex CLI operations. Never copy files into an installed cache, edit Codex configuration by hand, or treat cached package directories as mutable source trees.

Sync never mutates external agent files. Direct plugin installation does not install Codex-home permissions; explicit setup or sync invokes the installed-package GitHub profile helper without changing default permissions. This checkout separately defines a project-local opt-in profile. Never edit Codex home by hand in place of this managed lifecycle. Before plugin removal, run `agent-shims remove` while manager is still available. After refresh or reinstall, run `agent-shims doctor` to report prior shim residue; new installation and relinking remain platform-blocked. Report unknown or modified `codex-rig-*.toml` files without removing, adopting, or repairing them.

## Input Schema

```json
{
  "mode": "check|refresh",
  "marketplace": "borda-ai-rig",
  "plugin": "codex-rig@borda-ai-rig",
  "ref": "optional Git ref; omitted follows the remote default branch",
  "done_when": "active selection, package identity, and profile ownership/integrity are recorded; an approved refresh is reinstalled and rechecked"
}
```

Only frozen marketplace and plugin identifiers are accepted. `check` is default and is read-only. `refresh` requires explicit user approval because it fetches marketplace state, changes local plugin cache, and manages the opt-in GitHub-read profile in Codex home.

## Workflow

### 01: Create the result directory

Create `.reports/codex/sync/<timestamp>/` in consuming project. Record Codex CLI version, resolved executable, `CODEX_HOME` presence without secret values, operating system, and requested mode.

### 02: Inspect current state without mutation

Run authoritative help for available CLI, then collect:

```bash
codex plugin marketplace list --json
codex plugin list --marketplace borda-ai-rig --json
```

If documented `--json` option is absent, capture text form and mark structured comparison unavailable. Never invent flag. Record exactly one of: `not-configured`, `not-installed`, `disabled`, `active`, `ambiguous`, or `cli-unsupported`.

For one active installation, resolve selected cache path reported or implied by observed CLI contract. Require regular `.codex-plugin/plugin.json` and `package-manifest.json`; reject symlinks, path escape, duplicate selections, name/version disagreement, unsupported manifest schema, and package-file hash mismatch. Never select cache by lexical or modification-time "latest" rules.

Inspect the managed `github-read` config state without writing: report absent, current, or unverifiable. Inspect legacy plugin-owned reader and PR rules for safe removal; an edited or unrecognized owned rule blocks setup before writes. Absence is valid before setup and does not authorize installation during `check`.

### 03: Report external-agent residue without touching it

Read-only scan user agent directory for exact `codex-rig-*.toml` names. Record names and hashes, never file bodies. Classify every match `unmanaged-or-unknown` unless compatible lifecycle manager and its ownership state are available and verified. Plugin-only sync never deletes or overwrites match.

### 04: Stop after dry run unless refresh was explicitly approved

Show installed state, marketplace source, configured ref or default-branch tracking, resolved revision when marketplace checkout exposes it, current version, package verification result, possible external-agent residue, proposed commands, network/cache effects, profile changes, and rollback limit. Disclose GitHub reads, local PR checkout, output-file writes, `.git` workspace write access, and proxy destination rules. Include legacy-rule removal and backups in approved effects. Ask for approval before `refresh`. A check-only request, missing approval, ambiguous source, foreign marketplace, or unverified active package stops without mutation.

### 05: Refresh through the Codex CLI

After approval, use only commands confirmed by authoritative help:

Apply full networked CLI approval/denial contract in `../../shared/native-skill-contract.md` to the complete owning command for each Git marketplace add/upgrade or `sync_codex.py` wrapper that owns one. Operation-specific brief: `Action and purpose`: refresh approved marketplace and reconcile selected Codex Rig plugin; `External capability`: marketplace download and lifecycle refresh; `Credential behavior`: use configured Codex marketplace access without reading or changing credentials; `Filesystem and worktree effects`: change local plugin cache and Codex-home plugin state, never source worktree; `Retry policy and safe denial outcome`: stop turn on denial, leave checked state unchanged. Runtime approval is separate from lifecycle approval, never expands marketplace, plugin, ref, or mutation scope; never request a broad `codex` approval prefix. Local marketplace/plugin listing stays sandboxed. `codex plugin add` from configured snapshot needs no separate network escalation; an approved wrapper already owns its nested marketplace add/upgrade.

```bash
codex plugin marketplace add Borda/AI-Rig
codex plugin marketplace upgrade borda-ai-rig
codex plugin add codex-rig@borda-ai-rig
```

`Borda/AI-Rig` (the GitHub-style slug the CLI's `marketplace add` expects) and `borda-ai-rig` (the frozen id used by `upgrade`, `plugin add`, and everywhere else in this file) name the same marketplace — the latter matches the `name` field in the repo's `.agents/plugins/marketplace.json`, the canonical id source. Supply each literally as shown; nothing here implies the CLI derives one from the other.

For release pin, supply `--ref` with published revision whose Codex Rig package includes `scripts/install_github_read_rules.py`; older helper-free revisions cannot complete this refresh workflow. Native `sync_codex.py` accepts same selection through `--codex-ref`.

Omitting `--ref` follows remote default branch. An explicit ref pins it. Never silently change existing marketplace between pinned and unpinned modes: report mismatch, require legacy shim cleanup before deliberate marketplace removal and re-addition. Never use `git clone`, edit marketplace configuration, delete old cache directories, or force update. A failed refresh must preserve and report prior installation state; never claim rollback unless CLI evidence proves it.

The native sync wrapper verifies selected source package hashes/closure and required profile helper before managed-plugin removal. An already configured explicit pin is checked before marketplace mutation; default-branch refresh and new marketplace registration may precede source verification.

After successful managed-plugin installation, resolve active installed Codex Rig cache root and invoke the packaged profile helper:

```bash
python <installed-codex-rig-cache-root>/scripts/install_github_read_rules.py \
  --plugin-root <installed-codex-rig-cache-root> --codex-home <CODEX_HOME>
```

The helper has no positional install verb:

- Owns the marked `CODEX_HOME/config.toml` profile settings and state file. The profile extends `:workspace`, enables the network proxy for `api.github.com` and `github.com`, and permits `.git` writes under workspace roots. GitHub helper validation and the remote-mutation ban remain mandatory because domain rules do not restrict HTTP methods.
- Validate installed cache location, manifest name/version, complete package hashes/closure, ordinary reader, owned state, and unchanged managed config settings. Refuse a conflicting user profile or edited owned content.
- Remove only verified plugin-owned legacy reader and PR rules; migrate exact canonical reader entries from `default.rules`. Preserve unrelated bytes. Repeated setup is idempotent.
- `--no-codex-global-agents` skips only global `AGENTS.md` block. Native `sync_codex.py clear` invokes `--remove --codex-home <CODEX_HOME>` before removing plugins and restores prior root/feature settings while preserving unrelated later edits.
- Prepare backups before changing existing files. Restart existing Codex sessions after sync. Report completed updates and later failures accurately: each replacement is atomic, but the migration across files and overall sync are not transactional.

### 06: Recheck exact active identity

Repeat read-only inspection and package validation. Pass only when exactly one enabled selection is reported and its manifest plus all recorded payload hashes agree. After refresh, re-read the owned profile settings and legacy-rule cleanup; bind state integrity, backup, and migration evidence to refreshed package identity. Creation and idempotent setup need no backup; record that reason. Record requested/configured ref, resolved revision when available, old/new version, and package-manifest hashes. Same version with different package bytes is cache-identity failure.

### 07: Write the validated artifact

Follow `../../shared/helper-cli-contract.md`. Write `SYNC_METADATA`, gate logs, `state-before.json`, `proposed-actions.json`, and, for refresh, `state-after.json`. Promote only candidate accepted by shared validator.

## Fail-Fast Rules

1. Unknown marketplace/plugin identifier => fail before command execution.
2. Refresh without explicit approval => stop without mutation.
3. Missing, ambiguous, disabled, escaped, symlinked, or hash-invalid active package => fail.
4. Manual cache/config/source-tree mutation => fail.
5. External agent mutation or cleanup claim => fail.
6. Same version with different package bytes => fail.
7. Refresh command failure or post-refresh identity mismatch => fail; report prior state without invented rollback.
8. Result artifact missing => fail.
9. A planned profile or legacy-rule mutation has unverifiable cache, ownership, or checksum state => fail before writing. Required post-change migration, backup, or removal evidence missing => fail without claiming rollback.

## Quality Gates

Required: CLI-help evidence, before-state identity, complete package hash validation, external-agent residue summary, clean diff review, profile ownership/integrity and backup evidence, and validated result JSON. Refresh also requires explicit approval evidence, exact command/exit logs, after-state identity, and confirmation that existing sessions must restart to observe synchronized permissions.

## Calibration Hooks

Behavioral coverage includes dry-run default, missing approval, unavailable JSON output, duplicate active selections, same-version byte drift, source-unavailable cache validation, failed marketplace refresh, stale thin links, and preservation of unknown external agent files.

Networked CLI owning-command approval is required calibration coverage for Git marketplace add/upgrade behavior.

## Output Contract

Before writing result candidate, follow `../../shared/final-handoff-contract.md`: render and bind `final-handoff.json`, `final.md`, and `final-handoff.validation.json`; after both validators and promotion pass, emit `final.md` verbatim.

Use `../../shared/quality-gates.md` and `result-template.json`. Final chat follows shared ordered frame. `Outcome` is `pass`, `fail`, `partial`, or `blocked` and states whether requested check or approved refresh completed. `Results` has one inspected or refreshed surface per row and exactly `Surface | Outcome | Verification | Remaining limit`. Apply shared `Verification`, `Remaining`, `Next steps`, `Confidence`, and supplemental `Artifact` rules; include state/version/package hashes, commands, package validation, verified changes, lifecycle limits, and external-agent residue.
