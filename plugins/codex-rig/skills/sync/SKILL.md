---
name: sync
description: Dry-run active plugin cache drift; refresh/reinstall only with approval; keep shims separate.
---

# Sync

Inspect and refresh the public-GitHub Codex Rig plugin through supported Codex CLI operations. Never copy files into an installed cache, edit Codex configuration by hand, or treat cached package directories as mutable source trees.

Sync never mutates external agent files. Direct plugin installation stays inert; explicit setup or sync invokes the installed-package GitHub reader-rule helper. Never substitute an approval-UI saved prefix or direct home-file edit for this managed lifecycle. Before plugin removal, run `agent-shims remove` while the manager is still available. After refresh or reinstall, run `agent-shims doctor` to report prior shim residue; new installation and relinking remain platform-blocked. Report unknown or modified `codex-rig-*.toml` files without removing, adopting, or repairing them.

## Input Schema

```json
{
  "mode": "check|refresh",
  "marketplace": "borda-ai-rig",
  "plugin": "codex-rig@borda-ai-rig",
  "ref": "optional Git ref; omitted follows the remote default branch",
  "done_when": "active selection, package identity, and reader-rule ownership/integrity are recorded; an approved refresh is reinstalled and rechecked"
}
```

Only the frozen marketplace and plugin identifiers are accepted. `check` is the default and is read-only. `refresh` requires explicit user approval because it fetches marketplace state, changes the local plugin cache, and manages persistent GitHub reader approval in Codex home.

## Workflow

### 01: Create the result directory

Create `.reports/codex/sync/<timestamp>/` in the consuming project. Record the Codex CLI version, resolved executable, `CODEX_HOME` presence without secret values, operating system, and requested mode.

### 02: Inspect current state without mutation

Run the authoritative help for the available CLI, then collect:

```bash
codex plugin marketplace list --json
codex plugin list --marketplace borda-ai-rig --json
```

If a documented `--json` option is absent, capture the text form and mark structured comparison unavailable. Never invent a flag. Record exactly one of: `not-configured`, `not-installed`, `disabled`, `active`, `ambiguous`, or `cli-unsupported`.

For one active installation, resolve the selected cache path reported or implied by the observed CLI contract. Require a regular `.codex-plugin/plugin.json` and `package-manifest.json`; reject symlinks, path escape, duplicate selections, name/version disagreement, unsupported manifest schema, and package-file hash mismatch. Do not select a cache by lexical or modification-time "latest" rules.

Inspect the managed reader-rule state without writing: report absent, current, stale-version, or unverifiable. Absence is valid before setup and does not authorize installing rules during `check`.

### 03: Report external-agent residue without touching it

Read-only scan the user agent directory for exact `codex-rig-*.toml` names. Record names and hashes, never file bodies. Classify every match `unmanaged-or-unknown` unless a compatible lifecycle manager and its ownership state are available and verified. Plugin-only sync never deletes or overwrites a match.

### 04: Stop after dry run unless refresh was explicitly approved

Show the installed state, marketplace source, configured ref or default-branch tracking, resolved revision when the marketplace checkout exposes it, current version, package verification result, possible external-agent residue, proposed commands, network/cache effects, reader-rule changes, and rollback limit. Disclose the full wrapper scope: GitHub reads, local PR checkout, and output-file writes. Include exact legacy-rule migration and backups in the approved effects. Ask for approval before `refresh`. A check-only request, missing approval, ambiguous source, foreign marketplace, or unverified active package stops without mutation.

### 05: Refresh through the Codex CLI

After approval, use only commands confirmed by authoritative help:

Apply the full networked CLI approval and denial contract in `../../shared/native-skill-contract.md` to the complete owning command for each Git marketplace add/upgrade or `sync_codex.py` wrapper that owns one. The operation-specific brief is: `Action and purpose`: refresh the approved marketplace and reconcile the selected Codex Rig plugin; `External capability`: marketplace download and lifecycle refresh; `Credential behavior`: use configured Codex marketplace access without reading or changing credentials; `Filesystem and worktree effects`: change the local plugin cache and Codex-home plugin state, never the source worktree; `Retry policy and safe denial outcome`: stop the turn on denial and leave the checked state unchanged. Runtime approval is separate from lifecycle approval and never expands marketplace, plugin, ref, or mutation scope; never request a broad `codex` approval prefix. Local marketplace/plugin listing remains sandboxed. `codex plugin add` from the configured snapshot needs no separate network escalation; an approved wrapper already owns its nested marketplace add/upgrade.

```bash
codex plugin marketplace add Borda/AI-Rig
codex plugin marketplace upgrade borda-ai-rig
codex plugin add codex-rig@borda-ai-rig
```

For a release pin, supply `--ref` with a published revision whose Codex Rig package includes `scripts/install_github_read_rules.py`; older helper-free revisions cannot complete this refresh workflow. Native `sync_codex.py` accepts the same selection through `--codex-ref`.

Omitting `--ref` follows the remote default branch. An explicit ref pins it. Do not silently change an existing marketplace between pinned and unpinned modes: report the mismatch and require legacy shim cleanup before deliberate marketplace removal and re-addition. Do not use `git clone`, edit marketplace configuration, delete old cache directories, or force an update. A failed refresh must preserve and report the prior installation state; never claim rollback unless the CLI evidence proves it.

The native sync wrapper verifies the selected source package hashes/closure and required reader-rule helper before managed-plugin removal. An already configured explicit pin is checked before marketplace mutation; default-branch refresh and new marketplace registration may precede source verification.

After successful managed-plugin installation, resolve the active installed Codex Rig cache root and invoke the packaged reader-rule helper:

```bash
python <installed-codex-rig-cache-root>/scripts/install_github_read_rules.py \
  --plugin-root <installed-codex-rig-cache-root> --codex-home <CODEX_HOME>
```

The helper has no positional install verb:

- Owns `CODEX_HOME/rules/codex-rig-github-read.rules`; migration may rewrite `CODEX_HOME/rules/default.rules` to remove only exact canonical two-token legacy reader allow entries. Preserve unrelated bytes and back up changed existing files.
- Validate the installed cache location, manifest name/version, complete package hashes/closure, ordinary reader, canonical managed body, and checksum. A checksum alone does not establish ownership or publisher authenticity. Refuse unverifiable state. Persistent approval trusts the cache throughout its lifetime; setup-time validation cannot prevent later same-user code replacement.
- Regenerate for the installed version; repeated setup is idempotent. Allow only the literal `python`/`python3` launcher union and installed wrapper-path union, including native and POSIX spellings on Windows. Never grant broad Python or `gh` access or change network settings.
- `--no-codex-global-agents` skips only the global `AGENTS.md` block. Native `sync_codex.py clear` invokes `--remove --codex-home <CODEX_HOME>` before removing the plugins and migrates recognized legacy entries while removing the owned, canonical, integrity-valid rules file.
- Prepare every required existing-file backup before changing either rules file. Restart existing Codex sessions after sync. Report completed updates and later failures accurately: each replacement is atomic, but migration across two files and the overall sync are not transactional.

### 06: Recheck exact active identity

Repeat the read-only inspection and package validation. Pass only when exactly one enabled selection is reported and its manifest plus all recorded payload hashes agree. After refresh, re-read the managed reader-rule file and any migrated `default.rules` bytes; bind canonical-body, ownership, checksum, wrapper-path, launcher, backup, and migration evidence to the refreshed package identity. Creation and idempotent setup need no backup; record that reason. Record requested/configured ref, resolved revision when available, old/new version, and package-manifest hashes. Same version with different package bytes is a cache-identity failure.

### 07: Write the validated artifact

Follow `../../shared/helper-cli-contract.md`. Write `SYNC_METADATA`, gate logs, `state-before.json`, `proposed-actions.json`, and, for refresh, `state-after.json`. Promote only the candidate accepted by the shared validator.

## Fail-Fast Rules

1. Unknown marketplace/plugin identifier => fail before command execution.
2. Refresh without explicit approval => stop without mutation.
3. Missing, ambiguous, disabled, escaped, symlinked, or hash-invalid active package => fail.
4. Manual cache/config/source-tree mutation => fail.
5. External agent mutation or cleanup claim => fail.
6. Same version with different package bytes => fail.
7. Refresh command failure or post-refresh identity mismatch => fail; report prior state without invented rollback.
8. Result artifact missing => fail.
9. A planned reader-rule mutation has unverifiable cache, ownership, or checksum state => fail before writing. Required post-change migration, backup, or removal evidence missing => fail without claiming rollback.

## Quality Gates

Required: CLI-help evidence, before-state identity, complete package hash validation, external-agent residue summary, clean diff review, reader-rule ownership/integrity and backup evidence, and validated result JSON. Refresh also requires explicit approval evidence, exact command/exit logs, after-state identity, and confirmation that existing sessions must restart to observe the synchronized rules.

## Calibration Hooks

Behavioral coverage includes dry-run default, missing approval, unavailable JSON output, duplicate active selections, same-version byte drift, source-unavailable cache validation, failed marketplace refresh, stale thin links, and preservation of unknown external agent files.

Networked CLI owning-command approval is required calibration coverage for Git marketplace add/upgrade behavior.

## Output Contract

Before writing the result candidate, follow `../../shared/final-handoff-contract.md`: render and bind `final-handoff.json`, `final.md`, and `final-handoff.validation.json`; after both validators and promotion pass, emit `final.md` verbatim.

Use `../../shared/quality-gates.md` and `result-template.json`. Final chat follows the shared ordered frame. `Outcome` is `pass`, `fail`, `partial`, or `blocked` and states whether the requested check or approved refresh completed. `Results` has one inspected or refreshed surface per row and exactly `Surface | Outcome | Verification | Remaining limit`. Apply the shared `Verification`, `Remaining`, `Next steps`, `Confidence`, and supplemental `Artifact` rules; include state/version/package hashes, commands, package validation, verified changes, lifecycle limits, and external-agent residue.
