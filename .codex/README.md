# 🤖 Codex plugins in Borda's AI-Rig

← [Back to the project overview](../README.md) · [Codex Rig reference](../plugins/codex-rig/README.md) · [Codemap-py reference](../plugins/codemap-py/README.md) · [bridge_CC-Codex reference](../plugins/bridge_cc-codex/README.md)

AI-Rig gives Codex three independently installable products:

- **Codex Rig** turns common engineering work into evidence-first workflows with shared gates, specialist role cards, and comparable reports.
- **Codemap-py** answers unresolved structural questions about Python imports, callers, coupling, renames, and affected tests from a local static index.
- **bridge_CC-Codex** sends bounded implementation, advice, and review requests from Codex to Claude Code through a host-launched MCP transport.

Install Codex Rig when you want a disciplined workflow. Add Codemap-py when Python structure is part of the uncertainty. Add bridge_CC-Codex when a task benefits from an independent Claude Code implementation, answer, or adversarial review.

<details>
<summary><strong>Contents</strong></summary>

- [Install](#-install)
- [First five minutes](#-first-five-minutes)
- [Installed package blueprint](#-installed-package-blueprint)
- [Complete capability roster](#-complete-capability-roster)
- [Choosing the right workflow](#-choosing-the-right-workflow)
- [Artifacts and gates](#-artifacts-and-gates)
- [PR and network boundaries](#-pr-and-network-boundaries)
- [Agent-shim lifecycle](#-agent-shim-lifecycle)
- [Codemap-py structural context](#-codemap-py-structural-context)
- [Direct install versus repository sync](#-direct-install-versus-repository-sync)
- [Update, remove, and cleanup](#-update-remove-and-cleanup)
- [Troubleshooting](#-troubleshooting)
- [Source of truth](#-source-of-truth)

</details>

## 📦 Install

Prerequisite: a current Codex release with plugin support. The commands below match the [official Codex developer command reference](https://developers.openai.com/codex/cli/reference#codex-plugin).

```bash
codex plugin marketplace add Borda/AI-Rig
codex plugin add codex-rig@borda-ai-rig
codex plugin add codemap-py@borda-ai-rig
codex plugin add bridge@borda-ai-rig
codex plugin list
```

`codemap-py` and `bridge` are optional for direct installation. The repository synchronization path manages all three Codex plugins. Start a fresh Codex session after installation. To pin an immutable marketplace revision, add it with `--ref <release-tag>` rather than relying on a copied "latest" tag.

Direct plugin installation changes only Codex's plugin configuration and cache. It does not copy this repository's `.codex/config.toml`, personal policy, global `AGENTS.md`, or `rules/codex-rig-github-read.rules` into your Codex home. Reader rules are inert until explicit setup or repository sync invokes the installed helper.

## ⚡ First five minutes

Verify Codex Rig without writing:

```text
$codex-rig:agent-shims doctor
$codex-rig:audit
```

Try the investigation-to-review loop:

```text
$codex-rig:investigate find the root cause of the failing test
$codex-rig:implement apply the verified fix and run the relevant gates
$codex-rig:code-review review the current diff with no prior assumptions
$codex-rig:code-remediate close the selected findings
```

Try Codemap-py on a Python project:

```text
$codemap-py:scan-codebase
$codemap-py:query-code rdeps mypackage.auth
$codemap-py:test-impact mypackage.auth::validate_token
```

Check the reverse bridge before sending a request:

```text
$bridge:setup
$bridge:advise explain the smallest safe next step without editing files
$bridge:review review the current diff for correctness and missing tests
```

When passing an invocation from a shell, quote it so `$` is not expanded:

```bash
codex '$codex-rig:code-review #123'
codex '$codemap-py:query-code rdeps mypackage.auth'
```

## 🏗️ Installed package blueprint

<details>
<summary><strong>Show package identity, architecture, and health behavior</strong></summary>

Codex Rig is the independently packaged `codex-rig` product. The current manifest identifies version `0.9.0`, fourteen capabilities (thirteen workflow skills plus `agent-shims`), fifteen role cards, parallel blank-agent injection, inline fallback, quality gates, optional Codemap-py context, authenticated cleanup for prior shims, and an optional SessionStart diagnostic. The manifest does not register native persistent agents or an MCP server.

Its shipped tree is organized as `.codex-plugin/plugin.json`, `skills/`, `roles/`, `shared/`, `runtime/calibration/`, `hooks/`, `scripts/`, `tests/`, and `package-manifest.json`. The installed cache is immutable input: workflows resolve their own installed root and do not patch the cache or copy repository source into it.

`hooks/hooks.json` declares a read-only `SessionStart` command for `startup|resume`. It invokes the package's shim-health diagnostic with `python3` on POSIX and `python` on Windows, and does not install, update, or remove files. Declining hook trust leaves the diagnostic inactive without disabling skills.

Codemap-py is a separate `codemap-py` package. Its current Codex manifest identifies version `0.30.1`, the `codex-skills/` entry point, and six structural-analysis capabilities. Codex receives those skills but no Codemap hook manifest: there is no ambient preamble, hook-seeded session correlation, or redundant-scan guard. Codemap remains optional and Codex Rig starts without it.

bridge_CC-Codex is a separate `bridge` package shared by Claude Code and Codex. Its Codex half contributes four skills and a stdio MCP declaration. The MCP server is mandatory for Codex → Claude Code because it runs in the host context that owns normal Claude authentication; it does not accept model-controlled workspace, background, or session authority.

</details>

## 🔧 Full Codex Rig skill contracts

<details>
<summary><strong>Show all 14 input, gate, and artifact contracts</strong></summary>

The rows below summarize the public `skills/*/SKILL.md` schemas. The installed skill body is authoritative for exact flags, fail-fast checks, command order, and result validation; every workflow creates a run directory and emits a validated result with confidence and unresolved limits unless its contract says the action is read-only.

| Skill             | Input contract                                                                                                              | Core gate and outcome                                                                                                                                                  |
| ----------------- | --------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `change-analysis` | Question, scope, `local`, `github`, `report`, or `ecosystem` mode, and `done_when`.                                         | Build an evidence ledger, rank findings and risks, and leave measurable next gates before implementation.                                                              |
| `audit`           | `config`, `skills`, `roles`, or `all` scope; optional target; `upgrade` or `adversarial` mode; and optional gate skip.      | Inventory configuration and references, run shared gates, rank drift, and choose a repair level.                                                                       |
| `calibrate`       | `skills`, `agents`, `routing`, or `all` scope; `fast` or `full` pace; `ab-test` or `apply` mode; optional live routes.      | Run fixed and behavioral checks, score recall, precision, and confidence accuracy, and write proposals only in apply mode.                                             |
| `code-remediate`  | Findings source or review shorthand, report/PR mode, target scope, optional severity or finding selection, and `done_when`. | Refresh required PR evidence when in PR mode, ask for selection before editing, apply selected fixes, rerun gates, and defer unselected critical/high work explicitly. |
| `code-review`     | Optional working-tree/path/commit/PR scope and target.                                                                      | Resolve scope and risk mechanically, collect source evidence, run required specialist review, and emit a proposal/close decision without remote mutation.              |
| `implement`       | Goal; `feature`, `fix`, `refactor`, `config`, or `spike` mode; constraints; and an acceptance statement.                    | Record baseline, investigate or demonstrate before editing as required, make the smallest change, run quality gates, and report residual risk.                         |
| `investigate`     | Symptom, optional scope, `fast` or `full` pace, and a root-cause completion condition.                                      | Reproduce or characterize the failure, rank hypotheses, falsify alternatives, and stop at a confirmed cause or explicit uncertainty.                                   |
| `kaggle`          | Competition slug, context, optional problem type/mode, offline/resume/keep controls, and completion condition.              | Ground schema and submission format through authenticated Kaggle CLI, write a Jupytext notebook, structurally verify it, and record the artifact.                      |
| `manage`          | Create/update/delete/rename/permission intent, target, change/spec, and completion condition.                               | Resolve ownership and references, run safety gates, apply the smallest reversible edit, and verify all affected references.                                            |
| `optimize`        | Goal, mode, metric command/direction, guard command, iteration limit, minimum delta, scope files, and completion condition. | Validate metric/guard, record baseline and hypothesis, change one bounded variable at a time, reject regressions, and stop at the iteration bound.                     |
| `release`         | `notes`, `prepare`, `audit`, or `demo` mode; optional range/target version; and a release completion condition.             | Collect SemVer, changelog, migration, and package evidence, run readiness gates, and never tag, publish, upload, or force-push.                                        |
| `research`        | Research question; `docs`, `sota`, `paper`, `methodology`, or `code-fidelity` mode; constraints; completion condition.      | Gather current primary sources, map claims to code context when relevant, and produce source-backed recommendations with confidence.                                   |
| `sync`            | Fixed marketplace/plugin identity, `check` or `refresh` mode, optional Git ref, and completion condition.                   | Inspect active selection and cache drift read-only; refresh only after explicit approval, then recheck package identity.                                               |
| `agent-shims`     | Exactly one action: `doctor`, `status`, `install`, or `remove`.                                                             | Diagnose read-only, report the platform block for install, or perform guarded exact-digest removal; never mutate on ambiguous or untrusted state.                      |

Common completion fields are the requested output, gate results or explicit not-applicable reasons, evidence paths, confidence, and unresolved gaps. The parent retains final acceptance for runtime/API changes, executable verification, security, architecture, and release-blocking decisions.

</details>

## 🔧 Complete capability roster

### Codex Rig skills

Codex Rig installs 13 workflows and one lifecycle manager:

<details>
<summary><strong>Show the 14-skill contract map</strong></summary>

| Skill                        | Capability                                                                                           |
| ---------------------------- | ---------------------------------------------------------------------------------------------------- |
| `$codex-rig:change-analysis` | Analyze an issue, PR, module, or problem before implementation and record ranked findings.           |
| `$codex-rig:audit`           | Detect configuration, workflow, routing, documentation, and gate drift.                              |
| `$codex-rig:calibrate`       | Run fixed and behavioral checks; score recall, precision, and confidence accuracy.                   |
| `$codex-rig:code-remediate`  | Triage review findings, select valid work, apply fixes, and prove closure.                           |
| `$codex-rig:code-review`     | Review a local diff or current PR evidence across mandatory and risk-triggered axes.                 |
| `$codex-rig:implement`       | Run a linear plan-build-verify implementation loop with measurable acceptance gates.                 |
| `$codex-rig:investigate`     | Narrow an unknown failure to an evidence-backed root cause before implementation.                    |
| `$codex-rig:kaggle`          | Create or extend grounded Jupytext Kaggle notebooks using the authenticated Kaggle CLI.              |
| `$codex-rig:manage`          | Create, update, or remove Codex skills, agent configuration, and related references with guardrails. |
| `$codex-rig:optimize`        | Measure, change one bounded variable, remeasure, and reject regressions.                             |
| `$codex-rig:release`         | Assess SemVer, changelog, migration, packaging, and release readiness without publishing.            |
| `$codex-rig:research`        | Collect current primary evidence and map it to implementation choices.                               |
| `$codex-rig:sync`            | Report plugin-cache drift and request approval before a marketplace refresh.                         |
| `$codex-rig:agent-shims`     | Diagnose or remove authenticated pre-release shims; new shim installation remains blocked.           |

</details>

Each workflow defines input, fail-fast, gate, artifact, and confidence contracts. Full arguments and edge cases live in the owning [`SKILL.md` files](../plugins/codex-rig/skills/) and the [Codex Rig README](../plugins/codex-rig/README.md).

### Codex Rig role cards

Codex Rig ships 15 canonical role cards:

<details>
<summary><strong>Show all 15 role cards</strong></summary>

| Role                 | Primary ownership                                                         |
| -------------------- | ------------------------------------------------------------------------- |
| `delegation-lead`    | Cost-aware decomposition and handover consolidation                       |
| `sw-engineer`        | Implementation, APIs, types, and reproducible Python/ML code              |
| `qa-specialist`      | Regression proof, edge cases, and executable acceptance                   |
| `squeezer`           | Profile-first performance and resource analysis                           |
| `doc-scribe`         | Public docs, docstrings, examples, changelogs, and migrations             |
| `security-auditor`   | Trust boundaries, credentials, dependencies, and supply chain             |
| `data-steward`       | Dataset provenance, split integrity, leakage, and pipelines               |
| `cicd-steward`       | CI/CD, matrices, caching, publishing, and flaky-run diagnosis             |
| `linting-expert`     | Ruff, mypy, pre-commit, and suppression policy                            |
| `oss-shepherd`       | Triage, SemVer, deprecations, contributor workflow, and release readiness |
| `solution-architect` | Read-only system design, public contracts, coupling, and migrations       |
| `web-explorer`       | Current official docs, changelogs, and migration evidence                 |
| `curator`            | Configuration hygiene, duplication, drift, and weak gates                 |
| `challenger`         | Adversarial review of plans, risky changes, and no-finding conclusions    |
| `scientist`          | Papers, hypotheses, methods, metrics, and ablations                       |

</details>

Role cards are behavioral profiles, not proof that Codex selected a persistent named agent or a requested model. A workflow may inject the exact card into a runtime blank agent; if that route is unavailable, it performs a disclosed inline pass. The parent retains final acceptance.

### Codemap-py skills

Codemap-py exposes the same six capabilities in Codex and Claude Code:

<details>
<summary><strong>Show the six Codemap-py skills</strong></summary>

| Skill                        | Capability                                                                             |
| ---------------------------- | -------------------------------------------------------------------------------------- |
| `$codemap-py:scan-codebase`  | Build or refresh the static Python index.                                              |
| `$codemap-py:query-code`     | Query imports, symbols, call graphs, coverage, docs, dead code, diffs, and batches.    |
| `$codemap-py:test-impact`    | Identify structurally affected tests and emit a pytest command; it does not run tests. |
| `$codemap-py:rename-refs`    | Rename a Python symbol or module using static evidence, confirmation, and caveats.     |
| `$codemap-py:integration`    | Check, plan, apply, sync, or demo supported consumer wiring.                           |
| `$codemap-py:debrief-coding` | Analyze Claude Codemap telemetry, optionally with anonymization.                       |

</details>

Codex does not add the plugin's `bin/` directory to PATH and does not receive the optional Claude hook manifest. Use the `$codemap-py:*` skills unless you deliberately resolve the installed plugin root.

### bridge_CC-Codex skills

bridge_CC-Codex exposes four Codex skills:

| Skill               | Capability                                                                               |
| ------------------- | ---------------------------------------------------------------------------------------- |
| `$bridge:implement` | Ask Claude Code to make one bounded write-capable change and return a compact envelope.  |
| `$bridge:advise`    | Ask Claude Code for a read-only answer with explicit model, effort, and budget controls. |
| `$bridge:review`    | Ask Claude Code for a read-only adversarial review with actionable findings.             |
| `$bridge:setup`     | Check the local Claude CLI and bridge transport; paid live probing remains opt-in.       |

The reverse bridge keeps detailed provider output in a workspace-relative transcript. Decision-critical verdicts, findings, files touched, remaining work, and blockers stay in the compact response.

## 🗺️ Choosing the right workflow

| Situation                                                      | Route                                                            |
| -------------------------------------------------------------- | ---------------------------------------------------------------- |
| Symptom or failing CI with unknown cause                       | `investigate`, then `implement` after root-cause evidence exists |
| Requirements or change scope need analysis, not implementation | `change-analysis`                                                |
| Implement a bounded verified change                            | `implement`                                                      |
| Review a local diff                                            | `code-review`                                                    |
| Review a GitHub PR                                             | `code-review #123`                                               |
| Apply selected findings from the latest matching PR review     | `code-remediate #123 +review`                                    |
| Improve a measurable performance or quality metric             | `optimize`                                                       |
| Check release readiness                                        | `release`                                                        |
| Research current external behavior or migration guidance       | `research`                                                       |
| Unresolved importers, callers, coupling, or test impact        | the smallest matching Codemap query                              |
| Exact file and symbol known; no structural uncertainty remains | skip Codemap and inspect/edit directly                           |

## 📊 Artifacts and gates

Codex Rig workflows use `.reports/codex/<skill>/<canonical-safe-identity>/run-<NNN>/` only for a bounded validated non-sensitive identity and otherwise use `.reports/codex/<skill>/<timestamp>/`; raw arguments are never serialized into paths. Assessed PR reviews use the identity `pr-<number>`. A completed run normally includes development or review notes, per-gate logs, confidence calibration, and a validated `result.json`. Required gate IDs are lint, format, types, tests, and review; a non-applicable gate needs an explicit reason.

<details>
<summary><strong>Show artifact lifecycle and confidence gates</strong></summary>

```text
.reports/codex/<skill>/<timestamp>/
├── result.json
├── gates.json
├── gates.log
└── skill-specific evidence
```

The run directory is allocated once for local reviews and non-PR workflows. PR review collection begins in a temporary timestamped directory because current-branch input may not yet reveal a PR number. After authoritative `pr.json` succeeds, the run creator promotes the complete run to the next numeric `.reports/codex/code-review/pr-<number>/run-<NNN>/` path; every later helper and artifact uses the printed promoted path. Failed pre-identity collection remains a timestamped unavailable diagnostic rather than an assessed PR review. Shared helpers collect diffs and PR evidence, execute gates, validate artifact shape, map severity, and write the final result. A result is not complete because a file exists: the requested output, gate outcomes, evidence paths, confidence, and unresolved risks must agree. Historical flat code-review, `review/`, and `resolve/` artifacts remain readable fallback inputs without migration, but current canonical names are `code-review` and `code-remediate`.

Confidence is evidence-backed: `<=0.80` is incomplete; `0.80 < confidence < 0.85` needs stronger evidence; `0.85 <= confidence < 0.90` is cautious-low and requires objective recovery evidence; `>=0.90` is fair but still requires material residual limits. A not-applicable gate is recorded with its reason rather than silently omitted.

</details>

Codemap-py writes its default index to `.cache/codemap/<project>.json`. Queries report freshness, degradation, coverage, truncation, and blind spots. Read that metadata before treating a list as complete.

Generated artifacts are evidence, not authority. Inspect the code, tests, commands, and residual limits before accepting consequential work.

## 🧭 PR and network boundaries

Codex Rig can read GitHub evidence and prepare local changes when the runtime authorizes the owning command. It does not silently enable persistent network access. In this repository's policy, `gh` and `git` remote mutations—pushes, comments, reviews, merges, release publication, workflow dispatch, and forced updates—remain human-owned.

The Kaggle workflow requires the authenticated Kaggle CLI. Codex Rig explains missing user-owned prerequisites; it does not install credentials. Codemap-py indexing and querying are local.

<details>
<summary><strong>Show human-owned actions and network evidence rules</strong></summary>

| Area                | Codex Rig may do                                                                                                                | Human/runtime remains responsible for                                                                          |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| GitHub PR review    | Read approved evidence, check out the exact PR head locally, derive a diff, and prepare findings/remediation artifacts.         | Authentication recovery, network approval, comments, reviews, merges, pushes, and every other remote mutation. |
| Public PR fallback  | Use limited HTTPS metadata only for approved network/auth/rate-limit/timeout failure classes with an unambiguous GitHub target. | Accepting reduced evidence; private review evidence cannot be reconstructed by the fallback.                   |
| Kaggle              | Ground a notebook when the authenticated CLI is available.                                                                      | Credentials, dataset access, competition terms, and any upload or submission.                                  |
| Marketplace refresh | Inspect configured Git marketplace state and refresh only after explicit approval.                                              | Deciding whether new package bytes are trusted and resolving authentication or network failures.               |
| Release             | Assess SemVer, changelog, migration, packaging, and readiness.                                                                  | Tags, publication, package uploads, force-pushes, and release announcements.                                   |

Network approval applies to the complete owning collector/helper command when nested subprocesses or HTTPS are involved; approving only a nested executable is not proof that the workflow can complete.

</details>

## 🤖 Agent-shim lifecycle

<details>
<summary><strong>Show doctor, status, install, remove, and recovery rules</strong></summary>

The `agent-shims` skill accepts exactly one action: `$codex-rig:agent-shims doctor`, `status`, `install`, or `remove`. `doctor` and `status` are read-only health/roster checks. `install` reports the stable platform block and never writes a shim. `remove` plans exact authenticated cleanup for intact managed shims and never deletes by filename prefix or marker alone.

The manager records package identity, role-card and executable hashes, exact targets, lifecycle state, and approval digests. Review the displayed plan and type its exact digest only after explicit approval. Modified, foreign, ambiguous, malformed, oversized, unsafe, or concurrently changed evidence blocks without writes. Exit codes distinguish usage, cancellation, drift/conflict, prerequisite block, untrusted state, and recovery failure; do not retry a mutating action after drift, untrusted-state, or internal-recovery failures.

On POSIX, approved cleanup can recover one recognized interrupted transaction using its separate recovery digest; repeat the original action after recovery. Windows supports package verification and read-only inventory but not shim mutation. Uninstalling Codex Rig before cleanup leaves thin shims unavailable; reinstall the plugin, start a fresh session, run `doctor`, then run the guarded `remove`.

</details>

## 🔗 Codemap-py structural context

<details>
<summary><strong>Show index, query, freshness, and Codex limitations</strong></summary>

Codemap-py scans Python source into `.cache/codemap/<project>.json`. The canonical CLI is `codemap-py index`, `codemap-py query`, `codemap-py doctor --json`, and `codemap-py integrate`; the Codex skills are the supported installed entry point because Codex does not add the plugin's `bin/` directory to `PATH`. The dispatcher requires CPython `>=3.11,<3.15`; an unavailable eligible interpreter is a named compatibility failure, not an empty result.

Use `scan-codebase` to build or refresh, `query-code` for imports/symbols/call graphs/coverage/docs/dead code/diffs/batches, `test-impact` to identify structurally affected tests and emit a pytest command without running it, `rename-refs` for guarded static reference edits, `integration` for check/plan/apply/sync/demo consumer wiring, and `debrief-coding` for optional Claude Codemap telemetry analysis.

Read each result's `index` block. `stale`, `degraded`, `query_complete`, `confidence`, `truncated`, `total_available`, and `not_covered` describe different limits. Query-time incremental self-heal can write unless `SCAN_NO_AUTOBUILD=1`; an explicit scan is predictable after a clone, branch switch, or large change. Static AST evidence does not prove dynamic dispatch, callbacks, string imports, inheritance, external consumers, runtime behavior, or test success.

Codex Rig's `implement`, `investigate`, and `optimize` routes may probe the public Codemap CLI once and persist one status/artifact for specialists. `available`, `absent`, `stale`, `incompatible`, `degraded`, `stale+degraded`, and `skipped` are explicit statuses; absence/incompatibility fall back to bounded source inspection. The adapter never reads Codemap cache internals or imports Codemap Python code.

</details>

## 🧭 Current limitations

- Persistent named-agent selection is not verifiable in current Codex, so new thin-shim installation is platform-blocked. Blank-agent injection and disclosed inline fallback are the supported routes.
- Requested role model, effort, sandbox, and approval settings are not automatically proof of observed runtime controls. Safety-critical workflows must record and enforce the controls they can verify.
- Codemap-py is static Python analysis. Dynamic dispatch, callbacks, string imports, inheritance, external consumers, and runtime outcomes still require source inspection or tests.
- Codemap-py's dispatcher currently requires CPython `>=3.11,<3.15`. Codex lacks Claude's ambient Codemap status, telemetry correlation hook, and redundant-scan guard.
- Codex Rig's authenticated cleanup of legacy shims requires a POSIX local filesystem; the workflows, package checks, sync, and read-only diagnostics otherwise target Windows, macOS, and Linux.
- Possible future work is not a commitment. The installed package, skill/role contracts, tests, and documented fallbacks define current support.

## 🔄 Direct install versus repository sync

Direct marketplace installation is the public path and leaves global/project instructions alone.

From this source checkout, `make sync-codex` performs a broader managed restore: it installs or updates Codex Rig, Codemap-py, and bridge_CC-Codex, manages one authenticated Codex Rig block in `CODEX_HOME/AGENTS.md` plus the owned `rules/codex-rig-github-read.rules`, and projects selected repository model defaults and personal policy. The root Make target supplies no direct-script flags, so it has no opt-out for these managed surfaces. Read the [Codex Rig managed-instructions section](../plugins/codex-rig/README.md#managed-global-instructions) before using it.

`make sync-codex` installs from the pushed GitHub remote, not a dirty local tree. Commit and push first when you intentionally want a checkout change to become installable.

<details>
<summary><strong>Show sync scope and cleanup boundaries</strong></summary>

Direct installation changes only the Codex plugin configuration/cache. Repository sync additionally installs or updates all three managed plugins, manages one marked Codex Rig global-instructions block and the owned GitHub reader rules, and projects the root `model` and `review_model` defaults plus the authenticated personal policy. Reader-rule migration may remove only exact canonical two-token legacy reader allow entries from `CODEX_HOME/rules/default.rules`; unrelated bytes remain unchanged and every required existing-file backup is prepared before rules mutation. It does not overwrite project-owned `AGENTS.md` files or unrelated user configuration. The direct `sync_codex.py --no-codex-global-agents` flag skips only the global `AGENTS.md` block; reader-rule installation remains part of successful plugin installation.

`make clear-codex` removes Codex Rig, Codemap-py, bridge_CC-Codex, the managed block, and the owned reader rules while preserving user-owned bytes; marketplace registrations remain. The native `plugins/codex-rig/scripts/sync_codex.py` path manages the Codex plugins, block, and reader rules but does not project repository model defaults or personal policy. Both lifecycle paths back up changed files and fail closed when ownership or integrity cannot be verified.

</details>

## ⬆️ Update, remove, and cleanup

`marketplace upgrade` refreshes configured Git marketplace sources. A configured local or other non-Git marketplace keeps its current snapshot and skips this step.

```bash
codex plugin marketplace upgrade borda-ai-rig
codex plugin add codex-rig@borda-ai-rig
codex plugin add codemap-py@borda-ai-rig
codex plugin add bridge@borda-ai-rig
```

Use `codex plugin remove <plugin>@borda-ai-rig` to remove a plugin. If old Codex Rig shims exist, run `$codex-rig:agent-shims remove` before removing Codex Rig; removing the plugin first can make authenticated cleanup unavailable.

<details>
<summary><strong>Show update, uninstall, and recovery sequence</strong></summary>

After an update or reinstall, start a fresh Codex session so regenerated reader rules are observed, then run `$codex-rig:agent-shims doctor` plus a small audit. Remove Codemap-py independently when its structural context is no longer wanted; its `.cache/codemap/` index is project state and is retained unless the project owner intentionally cleans it.

For Codex Rig, remove authenticated legacy shims first, then use the guarded sync clear while the installed package is available; it removes the sync-managed global block and owned reader rules before plugin removal. If cleanup is blocked, reinstall the same plugin revision, start a fresh session, preserve the diagnostic artifact, and retry the documented guarded action; there is no force-cleanup path.

</details>

## 🔍 Troubleshooting

<details>
<summary><strong>Show common recovery paths</strong></summary>

- No skills appear: confirm `codex plugin list`, start a fresh session, and check that the marketplace source and package identity are current.
- `doctor` reports an active-package or manifest failure: reinstall/refresh Codex Rig, start a fresh session, and rerun `doctor`; do not edit the installed cache.
- A workflow reports missing `gh`, Kaggle, Docker, or Colab: install/authenticate or connect the user-owned prerequisite only when that path is required; explicit requests stop rather than silently substitute another integration.
- Codemap reports stale or incompatible: run `$codemap-py:scan-codebase` from the target project, then read the freshness and blind-spot metadata; use bounded source inspection when it remains unavailable.
- `sync` reports drift or a dirty source: inspect the active package and remember that repository sync consumes pushed remote bytes, not uncommitted checkout changes.
- Shim cleanup is blocked: preserve the named invariant and diagnostic evidence, avoid recursive permission changes or deletion, reinstall if necessary, and retry only the guarded action.

</details>

## 🏗️ Source of truth

- [Codex Rig product and workflow reference](../plugins/codex-rig/README.md)
- [Codex Rig role-card reference](../plugins/codex-rig/roles/README.md)
- [Codex Rig maintainer scripts](../plugins/codex-rig/scripts/README.md)
- [Codex Rig manifest](../plugins/codex-rig/.codex-plugin/plugin.json)
- [Codex Rig health-hook manifest](../plugins/codex-rig/hooks/hooks.json)
- [Codemap-py product and skill reference](../plugins/codemap-py/README.md)
- [Codemap-py runtime executables](../plugins/codemap-py/bin/README.md)
- [Codemap-py packaging and install probes](../plugins/codemap-py/scripts/README.md)
- [Codemap-py manifest](../plugins/codemap-py/.codex-plugin/plugin.json)
- [bridge_CC-Codex product and transport reference](../plugins/bridge_cc-codex/README.md)
- [bridge_CC-Codex manifest](../plugins/bridge_cc-codex/.codex-plugin/plugin.json)

License: [Apache-2.0](../LICENSE).
