# Development and release verification

`bridge_CC-Codex` is a standalone Python 3.10+ plugin package. Its normalized plugin identifier is `bridge`; both manifests must keep that name and the same version. Its Claude Code manifest is `.claude-plugin/plugin.json`; its Codex manifest is `.codex-plugin/plugin.json`; its reverse transport declaration is `.mcp.json`. The current release is `0.4.1`.

## Source layout

- `bin/bridge_call.py` owns request normalization, peer command construction, supervision, compact-result validation, artifacts, and health logging. Implement requests are write-capable; review requests use read-only general Codex execution with an explicit adversarial-review prompt rather than Codex's native review subcommand.
- `bin/bridge_mcp.py` owns the stdio MCP protocol and the four Codex-facing tools `bridge_implement`, `bridge_advise`, `bridge_review`, and the zero-provider `bridge_status` session/workspace evidence tool.
- `bin/bridge_diagnose.py` owns static host-surface checks and optional live diagnosis. The baseline is deliberately flag-only — it pins no CLI version number, because a depended-on flag disappearing from `--help` is the failure that actually breaks dispatch — and the free static check reports without appending health records; only completed bridge requests write `health.jsonl` lines.
- `bin/bridge_setup.py` owns deterministic setup argument parsing, capability inspection, expiring one-use action approvals, user-scoped locks and credential-free records, post-configuration reinspection, provider-owned no-capture authentication, and approval-bound live verification. Host skills own decision flow and prompts; the planner never accepts credentials or model-selected workspace overrides.
- `schemas/` contains the model-core, harness-envelope, MCP input, and setup-result contracts. The setup-result schema is not a model-run envelope and must not gain transcript, token, cost, or model-verb fields.
- `rules/` contains the effort, prompting, recursion, envelope, and recovery policies used by both host integrations.
- `claude-skills/` contains Claude Code commands; `codex-skills/` contains Codex commands. Both host trees expose `implement`, `advise`, `review`, and the approval-bound `setup` lifecycle.
- `tests/` proves runtime, contract, setup approval/no-capture boundaries, cross-platform, and install-shaped package behavior.

Keep public names, schemas, manifests, skills, and README examples synchronized when a user-facing behavior changes. Do not make the package depend on a source checkout, another plugin, or a private workspace path.

## Local checks

Run the focused plugin suite:

```bash
python -m pytest -q plugins/bridge_cc-codex
```

Format and lint changed Markdown through the pinned pre-commit hook:

```bash
pre-commit run mdformat --files plugins/bridge_cc-codex/README.md plugins/bridge_cc-codex/docs/architecture.md plugins/bridge_cc-codex/docs/security.md plugins/bridge_cc-codex/docs/operations.md plugins/bridge_cc-codex/docs/development.md plugins/bridge_cc-codex/CHANGELOG.md
```

Check whitespace and build an install-shaped package outside the source tree on Linux or macOS:

```bash
git diff --check -- plugins/bridge_cc-codex
disposable_parent_directory="$(mktemp -d)"
python plugins/bridge_cc-codex/scripts/build_package.py --output "$disposable_parent_directory/bridge"
python plugins/bridge_cc-codex/scripts/validate_package.py "$disposable_parent_directory/bridge"
```

Use the native PowerShell equivalents on Windows:

```powershell
git diff --check -- plugins/bridge_cc-codex
$disposableParentDirectory = Join-Path ([System.IO.Path]::GetTempPath()) ("bridge-" + [System.Guid]::NewGuid())
New-Item -ItemType Directory -Path $disposableParentDirectory | Out-Null
$disposablePackageDirectory = Join-Path $disposableParentDirectory "bridge"
& python plugins/bridge_cc-codex/scripts/build_package.py --output $disposablePackageDirectory
& python plugins/bridge_cc-codex/scripts/validate_package.py $disposablePackageDirectory
```

The package validator rejects symlinks, private artifact directories, private absolute paths, malformed JSON, missing runtime closure, unresolved manifest paths, and version mismatches. The disposable build excludes tests, caches, `.DS_Store`, and temporary bridge state. The public envelope must remain compact: decisions, blockers, and remaining work belong in public fields, while bounded peer `details` belong only in the bounded transcript with workspace-relative transcript and incident references returned as metadata. The setup result is validated independently and carries only credential-free lifecycle evidence; sensitive authentication output is never an artifact.

## Behavioral verification

When changing the bridge runtime, add or update a public-contract test that would fail under a plausible incorrect implementation, then run the complete plugin suite. Exercise both directions where the change affects transport, and inspect the compact returned envelope plus `.temp/bridge` artifacts for timeout, refusal, authentication, and partial-result behavior. For setup changes, test the default `all/peer/auto/prompt` plan; exact action/workspace/state binding; expiry and success/failure replay rejection; cross-workspace locking; non-symlink user-state records; post-configuration reinspection; safe denial; no-capture authentication; approval-bound live verification; rollback boundaries; `bridge_status`; and sync's static-only dispatch. Live setup probes are optional and require explicit operator approval because they invoke authenticated provider inference; static setup alone does not verify provider or schema compatibility.

Before a release, verify both host manifests, the `.mcp.json` `${PLUGIN_ROOT}` path, the four MCP tool schemas, the setup-result schema, the version in `CHANGELOG.md`, and the install-shaped package. Confirm ordinary sync invokes only the direct static doctor and that no setup path stores authentication material. Remote marketplace publication and host installation remain operator-owned actions after these local gates pass.
