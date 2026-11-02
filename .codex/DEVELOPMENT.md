# Codex plugins — development and internals

← [Codex user guide](README.md) · [Codex Rig reference](../plugins/codex-rig/README.md)

This file holds technical detail for contributors, maintainers, and agents. The user-facing install and usage guide is [README.md](README.md).

## Package identity and layout

- **Codex Rig** is the independently packaged `codex-rig` product. Its selected source manifest identifies the version and fifteen capabilities (fourteen workflow skills plus `agent-shims`), fifteen role cards, parallel blank-agent injection, inline fallback, quality gates, optional Codemap-py context, authenticated cleanup for prior shims, and an optional SessionStart diagnostic. The manifest registers the local native-question MCP provider and does not register native persistent agents.
- **Shipped tree:** `.codex-plugin/plugin.json`, `skills/`, `roles/`, `shared/`, `runtime/calibration/`, `hooks/`, `scripts/`, `tests/`, and `package-manifest.json`. The installed cache is immutable input: workflows resolve their own installed root and do not patch the cache or copy repository source into it.
- **Health hook:** `hooks/hooks.json` declares a read-only `SessionStart` command for `startup|resume`. It invokes the package's shim-health diagnostic through bundled `bin/python` on POSIX and `bin/python.cmd` on native Windows, warns when the managed global-instructions block names another Codex Rig root than the running package (or none), and does not install, update, or remove files. Declining hook trust leaves the diagnostic inactive without disabling skills.
- **Codemap-py** is a separate `codemap-py` package. Its selected Codex manifest identifies the version, the `codex-skills/` entry point, and six structural-analysis capabilities. Codex receives those skills plus hooks for ambient preamble, session correlation, redundant-scan guarding and tool records. Native Windows commands use `bin/codemap-py.cmd`; POSIX commands use `bin/codemap-py`. Codemap remains optional and Codex Rig starts without it.
- **bridge_CC-Codex** is a separate `bridge` package shared by Claude Code and Codex. Its Codex half contributes four skills and a stdio MCP declaration. The MCP server is mandatory for Codex → Claude Code because it runs in the host context that owns normal Claude authentication; it does not accept model-controlled workspace, background, or session authority.

## Full Codex Rig skill contracts

The rows summarize the public `skills/*/SKILL.md` schemas. The installed skill body is authoritative for exact flags, fail-fast checks, command order, and result validation. Every workflow creates a run directory and emits a validated result with confidence and unresolved limits unless its contract says the action is read-only.

| Skill               | Input contract                                                                                                              | Core gate and outcome                                                                                                                                                                                                                       |
| ------------------- | --------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `challenge-resolve` | Goal, scoped files, specification, symptom, optional caller run, and a clean independent-review completion condition.       | Challenge, report old/new findings, resolve feasible fixes, escalate severe residue, and continue while the unchanged-scope integer score decreases; stop on plateau, non-convergence, unavailable independence or stricter caller budgets. |
| `assess`            | Question, scope, `local`, `github`, `report`, or `ecosystem` mode, and `done_when`.                                         | Build an evidence ledger, rank findings and risks, and leave measurable next gates before implementation.                                                                                                                                   |
| `audit`             | `config`, `skills`, `roles`, or `all` scope; optional target; `upgrade` or `adversarial` mode; and optional gate skip.      | Inventory configuration and references, run shared gates, rank drift, and choose a repair level.                                                                                                                                            |
| `calibrate`         | `skills`, `agents`, `routing`, or `all` scope; `fast` or `full` pace; `ab-test` or `apply` mode; optional live routes.      | Run fixed and behavioral checks, score recall, precision, and confidence accuracy, and write proposals only in apply mode.                                                                                                                  |
| `code-remediate`    | Findings source or review shorthand, report/PR mode, target scope, optional severity or finding selection, and `done_when`. | Refresh required PR evidence when in PR mode, ask for selection before editing, apply selected fixes, rerun gates, and defer unselected critical/high work explicitly.                                                                      |
| `code-review`       | Optional working-tree/path/commit/PR scope and target.                                                                      | Resolve scope and risk mechanically, collect source evidence, run required specialist review, and emit a proposal/close decision without remote mutation.                                                                                   |
| `implement`         | Goal; `feature`, `fix`, `refactor`, `config`, or `spike` mode; constraints; and an acceptance statement.                    | Record baseline, investigate or demonstrate before editing as required, make the smallest change, run quality gates, and report residual risk.                                                                                              |
| `investigate`       | Symptom, optional scope, `fast` or `full` pace, and a root-cause completion condition.                                      | Reproduce or characterize the failure, rank hypotheses, falsify alternatives, and stop at a confirmed cause or explicit uncertainty.                                                                                                        |
| `kaggle`            | Competition slug, context, optional problem type/mode, offline/resume/keep controls, and completion condition.              | Ground schema and submission format through authenticated Kaggle CLI, write a Jupytext notebook, structurally verify it, and record the artifact.                                                                                           |
| `manage`            | Create/update/delete/rename/permission intent, target, change/spec, and completion condition.                               | Resolve ownership and references, run safety gates, apply the smallest reversible edit, and verify all affected references.                                                                                                                 |
| `optimize`          | Goal, mode, metric command/direction, guard command, iteration limit, minimum delta, scope files, and completion condition. | Validate metric/guard, record baseline and hypothesis, change one bounded variable at a time, reject regressions, and stop at the iteration bound.                                                                                          |
| `release`           | `notes`, `prepare`, `audit`, or `demo`; optional range/version and `--changelog`, `--summary`, `--migration`, `--append`.   | Draft traced release communication, complete contributor credits, preserve changelog history/detail, and report readiness in a table; never publish.                                                                                        |
| `research`          | Research question; `docs`, `sota`, `paper`, `methodology`, or `code-fidelity` mode; constraints; completion condition.      | Gather current primary sources, map claims to code context when relevant, and produce source-backed recommendations with confidence.                                                                                                        |
| `sync`              | Fixed marketplace/plugin identity, `check` or `refresh` mode, optional Git ref, and completion condition.                   | Inspect active selection and cache drift read-only; refresh only after explicit approval, then recheck package identity.                                                                                                                    |
| `agent-shims`       | Exactly one action: `doctor`, `status`, `install`, or `remove`.                                                             | Diagnose read-only, report the platform block for install, or perform guarded exact-digest removal; never mutate on ambiguous or untrusted state.                                                                                           |

Common completion fields are the requested output, gate results or explicit not-applicable reasons, evidence paths, confidence, and unresolved gaps. The parent retains final acceptance for runtime/API changes, executable verification, security, architecture, and release-blocking decisions.

## Artifact lifecycle

- Run directory: `.reports/codex/<skill>/<canonical-safe-identity>/run-<NNN>/` only for a bounded validated non-sensitive identity; otherwise `.reports/codex/<skill>/<timestamp>/`. Raw arguments are never serialized into paths. Assessed PR reviews use `pr-<number>`.
- The run directory is allocated once for local reviews and non-PR workflows.
- PR review collection begins in a temporary timestamped directory because current-branch input may not yet reveal a PR number. After authoritative `pr.json` succeeds, the run creator promotes the complete run to the next numeric `.reports/codex/code-review/pr-<number>/run-<NNN>/` path; every later helper and artifact uses the printed promoted path.
- Failed pre-identity collection remains a timestamped unavailable diagnostic rather than an assessed PR review.
- Shared helpers collect diffs and PR evidence, execute gates, validate artifact shape, map severity, and write the final result.
- A result is not complete because a file exists: the requested output, gate outcomes, evidence paths, confidence, and unresolved risks must agree.
- Historical flat code-review, `review/`, and `resolve/` artifacts remain readable fallback inputs without migration; current canonical names are `code-review` and `code-remediate`.

Confidence bands:

| Confidence                  | Meaning                                             |
| --------------------------- | --------------------------------------------------- |
| `<=0.80`                    | Incomplete.                                         |
| `0.80 < confidence < 0.85`  | Needs stronger evidence.                            |
| `0.85 <= confidence < 0.90` | Cautious-low; requires objective recovery evidence. |
| `>=0.90`                    | Fair, but still requires material residual limits.  |

## Codemap probe statuses

Codex Rig's `implement`, `investigate`, and `optimize` routes may probe the public Codemap CLI once and persist one status/artifact for specialists. Explicit statuses: `available`, `absent`, `stale`, `incompatible`, `degraded`, `stale+degraded`, `skipped`. Absence or incompatibility falls back to bounded source inspection. The adapter never reads Codemap cache internals or imports Codemap Python code.

## Sync internals

**Scope of each path**

- Direct installation changes only the Codex plugin configuration/cache.
- Native `plugins/codex-rig/scripts/sync_codex.py` manages the Codex plugins, block, and profile but does not project repository model defaults or personal policy.
- Repository sync (`make sync-codex`) additionally installs or updates all three managed plugins, manages one marked Codex Rig global-instructions block and the `local-workflow` and `github-read` permission profiles, and projects the root `model` and `review_model` defaults plus the authenticated personal policy. The root Make target supplies no direct-script flags, so it has no opt-out for these managed surfaces.
- `make sync-codex` installs from the pushed GitHub remote, not a dirty local tree.

**Codex sync sequence**

1. Refresh the existing Git marketplace, or replace a non-Git registration with the canonical `Borda/AI-Rig` Git source.
2. Verify selected source package hashes and closure plus the required profile setup helper (from the installed marketplace revision).
3. Remove its managed plugins by default and reinstall them (`--no-clean` keeps them).
4. Run the installed Bridge static doctor: the `python` launcher used by MCP must report Python 3.10 or newer; the Claude CLI help contract is checked without model inference, authentication changes, or provider cost.
5. Install the `github-read` permission profile after successful plugin installation, regardless of `--no-codex-global-agents` (that flag skips only the global `AGENTS.md` block).

**Claude sync (reference)**: manages foundry, oss, develop, research, codemap-py, and `bridge`; refreshes only the retained external caveman plugin. After bridge installs successfully it removes any installed copy of the retired external Codex rescue plugin; a failed bridge install preserves it for recovery.

**Profile and rule migration**

- Migration removes verified owned legacy rule files and exact canonical-shaped Codex Rig entries in `default.rules`. Those entries have no individual ownership marker, so an identical user-authored line cannot be distinguished. Other rules remain unchanged.
- A verified older automatic profile migrates to opt-in; a sole root `sandbox_mode = "workspace-write"` converts to `default_permissions = ":workspace"` with a byte-exact backup; other sandbox settings still block setup.
- The profile does not enforce HTTP methods or executable identity; host restrictions still apply.
- Older revisions without the profile setup helper remain valid for direct plugin installation but cannot satisfy the current managed setup contract.

**Teardown**

- `sync_codex.py clear` removes managed Codex plugins, strips only the authenticated Codex Rig block from `${CODEX_HOME:-$HOME/.codex}/AGENTS.md`, and clears the owned `github-read` profile; repository-projected model defaults and personal policy stay untouched.
- `make clear-all` also uninstalls this marketplace's Claude plugins when Claude scope is active; `make clear-codex` and `make clear-claude` scope the same teardown to one side.
- All paths keep timestamped backups, preserve user-owned content byte-for-byte, and leave marketplace registrations plus external plugins in place. Each helper refuses to modify its own unverifiable managed content; earlier successful steps are not rolled back.

**Non-transactional behavior**

- Marketplace/plugin refresh and profile installation can finish before a later global-instruction merge fails.
- The profile helper preserves unrelated user configuration and reports each completed update immediately; partial failure is never described as rolled back.
- Resolve the reported target state before rerunning sync, and avoid concurrent edits: portable filesystems provide no universal compare-and-swap operation.

## Managed global instructions — mechanics

- `scripts/install_global_agents.py --check --source <installed-package>/assets/AGENTS.md --codex-home <home>` is read-only and returns nonzero for conflicting global blocks, stale managed content, malformed markers, or the two recognized legacy `develop`/`analyse` skill signatures. A marketplace-checkout `--source` needs `--plugin-root <installed-package>`, since the block renders the installed root and would otherwise read as stale.
- Managed sync checks the proposed global-policy composition before removing or installing plugins or updating the permission profile. It accepts only the exact stale-template-only diagnostic as a normal upgrade, then retains strict post-install health checks.
- Legacy-prefix migration: review the exact bytes, supply their SHA-256 via `--migrate-legacy-prefix-sha256 <digest>`; the installer authenticates the existing managed block, backs up the full original, replaces only the digest-selected prefix and managed block, and preserves custom suffix bytes. Modified markers, stale digests, and observed concurrent replacement reject the operation.

## Project-policy notes

- The [personal session policy](global-session-policy.md#local-test-execution) keeps ordinary local tests sandboxed and requires evidence for additional capabilities, including local sockets, subprocess communication, and filesystem access.
- The project approval policy preapproves the five Bridge MCP tools when `bridge@borda-ai-rig` is verified installed and loaded, including ordinary provider inference for an already authorized task or invoked workflow.
- Bridge repository-content transfer is a separate explicit consent stage in Bridge setup; see the consent section of the [README](README.md#-direct-install-versus-repository-sync).
