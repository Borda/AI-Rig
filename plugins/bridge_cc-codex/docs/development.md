# Development and release verification

`bridge_CC-Codex` is a standalone Python 3.10+ plugin package. Its normalized plugin identifier is `bridge`; both manifests must keep that name and the same version. Its Claude Code manifest is `.claude-plugin/plugin.json`; its Codex manifest is `.codex-plugin/plugin.json`; its Codex reverse-transport declaration is `.codex-mcp.json`. Claude Code has no MCP declaration at all -- the plugin ships no `.mcp.json`, since Claude Code auto-discovers any file by that literal name at a plugin's root and the Claude-to-Codex direction needs no MCP.

## Source layout

- `bin/bridge_call.py` owns request normalization, peer command construction, supervision, compact-result validation, artifacts, and health logging. Implement requests are write-capable; review requests use read-only general Codex execution with an explicit adversarial-review prompt rather than Codex's native review subcommand.
- `bin/bridge_mcp.py` owns the stdio MCP protocol and the five Codex-facing tools `bridge_implement`, `bridge_advise`, `bridge_review`, the zero-provider `bridge_status` session/workspace evidence tool, and `bridge_bind_workspace`.
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
pre-commit run mdformat --files plugins/bridge_cc-codex/README.md plugins/bridge_cc-codex/docs/security.md plugins/bridge_cc-codex/docs/operations.md plugins/bridge_cc-codex/docs/development.md plugins/bridge_cc-codex/CHANGELOG.md
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

When changing the bridge runtime, add or update a public-contract test that would fail under a plausible incorrect implementation, then run the complete plugin suite. Exercise both directions where the change affects transport, and inspect the compact returned envelope plus `.temp/bridge` artifacts for timeout, refusal, authentication, and partial-result behavior. For setup changes, test the default `all/peer/auto/prompt` plan; exact action/workspace/state binding; expiry and success/failure replay rejection; cross-workspace locking; non-symlink user-state records; post-configuration reinspection; safe denial; no-capture authentication; approval-bound live verification; rollback boundaries; `bridge_status`; explicit two-form workspace binding, stale binding identities, cancellation, directory retargeting, installed cache guards; and sync's static-only dispatch. Live setup probes are optional and require explicit operator approval because they invoke authenticated provider inference; static setup alone does not verify provider or schema compatibility.

Before a release, verify both host manifests, the `.codex-mcp.json` plugin-relative `cwd: "."` and `bin/bridge_mcp.py` script arguments, that no `.mcp.json` ships at the plugin root, the five MCP tool schemas, the setup-result schema, the version in `CHANGELOG.md`, and the install-shaped package. Confirm ordinary sync invokes only the direct static doctor and that no setup path stores authentication material. Remote marketplace publication and host installation remain operator-owned actions after these local gates pass.

The standalone `bin/bridge_consent.py` stage tests native user configuration only in disposable homes: absent/check/decline and unapproved noninteractive paths never grant; `grant --approve` with closed stdin works only with a one-time token minted by `issue-token` from a terminal, preserves unrelated TOML and is recognized by the next `check`, while a missing, wrong, expired or reused token and a piped `issue-token` never grant; the flag is rejected with `check` or `revoke`, even when consent exists. Repeated grant is idempotent; interactive revoke removes only the owned block; drift and unsafe files fail closed. The installed-shape scenario also grants with the explicit flag and checks the resulting consent. Skill contract tests retain explicit user-flag provenance and separate setup approvals. The repository policy-sync regression grants, synchronizes, revokes and synchronizes again to prove consent preservation without resurrection. These local checks do not establish a live native reviewer decision.

## Reference: MCP declaration and Claude `.mcp.json`

- The Codex manifest declares the bridge MCP server via `.codex-mcp.json`, which starts `bin/bridge_mcp.py` using native `cwd: "."` and relative script arguments.
- Claude Code ships no `.mcp.json` at all. The Claude-to-Codex direction is CLI-only (`bin/bridge_call.py`), and Claude Code auto-discovers any file literally named `.mcp.json` at a plugin's root regardless of manifest intent, so the plugin deliberately never ships one to avoid registering a server Claude Code has no use for.
- Why MCP is mandatory for Codex → Claude Code: a `claude --print` process started from a sandboxed Codex model turn cannot rely on the normal Claude authentication context, while the Codex host launches the MCP server outside that model sandbox. Claude Code → Codex launches `codex exec` directly.
- The direct MCP command requires Python 3.10+ available as `python` on the host search path.

## Reference: workspace binding

- Flow: call `bridge_bind_workspace` with the known absolute project folder as untrusted `proposed_workspace`, then confirm its exact canonical target in one native form, which states that Codex proposed the folder.
- Every advise/review/implement call requires the current `binding_id` returned by binding/status.
- Refused folders: filesystem root; home itself and every directory above it; installed payload/cache folders; agent configuration homes with everything inside them (`~/.claude`, `~/.codex`, and the directories `CLAUDE_CONFIG_DIR` and `CODEX_HOME` name); user credential directories with everything inside them (`~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.kube`, `~/.docker`); and, outside Windows, `/etc` with everything inside it. Project subdirectories of home are allowed.
- `bridge_setup.py --workspace` refuses the same denied folders (no marker rule) for every action except the read-only `check`.

## Reference: Codex user-question routing

Shipped contract: `rules/codex-user-questions.md`, with details in `rules/codex-user-questions-details.md`.

- Codex asks only for a genuinely missing decision, material input or protected action. Authorized preparation, internal recovery and completion need no new consent; transport discovery follows that necessity check.
- When a decision is missing, questions prefer permitted synchronous input, then the packaged native terminal form (`ask_user`, served by `bin/user_questions_mcp.py`). The local form waits for the answer, binds it to the decision ID and frozen scope digest, and does not emit a second assistant question message.
- Structured question controls come first. When plain text is required, all context, reports, confidence and limitations precede the question; the question and complete options or accepted input syntax are the final terminal block, with no trailing summary or status.
- Before fallback, discover deferred native question tools through the current catalog or tool search, including `functions.exec` `ALL_TOOLS` when exposed, and record route eligibility.
- An explicit current host contract establishing rendered controls, text input, and answer lifetime permits first use; tool acceptance alone does not prove delivery. Unknown async usability is unsuitable. Async questions are used only where current host evidence establishes usable controls.
- Select the route before asking, and deliver a plain-chat fallback once in the final response, never in both commentary and final. A text-delivered async question stays pending without a second control or repeated menu.
- A synchronous mode error does not establish async unavailability; explicit higher-priority host requirements for plain text remain binding and are reported as policy restrictions, not missing tools.
- Explicit Approve/Deny replies bind to one unchanged, unambiguous conversational decision without demanding its displayed key again. No answer, cancellation or unsupported input grants consent.
- The root uses a permitted question route with meaningful presets and built-in custom input; children hand decisions back to the root. Detailed approval/recovery rules load only when needed.
