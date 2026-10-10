# 🤖 Codex plugins in Borda's AI-Rig

← [Back to the project overview](../README.md) · [Codex Rig reference](../plugins/codex-rig/README.md) · [Codemap-py reference](../plugins/codemap-py/README.md) · [bridge_CC-Codex reference](../plugins/bridge_cc-codex/README.md)

AI-Rig gives Codex three independently installable products:

- **Codex Rig** turns common engineering work into evidence-first workflows with shared gates, specialist role cards, and comparable reports.
- **Codemap-py** answers unresolved structural questions about Python imports, callers, coupling, renames, and affected tests from a local static index.
- **bridge_CC-Codex** sends bounded implementation, advice, and review requests from Codex to Claude Code through a host-launched MCP transport.

Install Codex Rig when you want a disciplined workflow. Add Codemap-py when Python structure is part of the uncertainty. Add bridge_CC-Codex when a task benefits from an independent Claude Code implementation, answer, or adversarial review.

This checkout selects native automatic approval review for required plugin workflow commands. Its additive `[auto_review].extra_policy` treats executable recipes, inline helper invocations, normal parameter substitutions, configured gates and already-authorized local commits as preapproved across every skill in `codex-rig@borda-ai-rig`, `codemap-py@borda-ai-rig`, and `bridge@borda-ai-rig`. Other installed plugins retain their existing policy. The reviewer evaluates complete commands, including nested gates; no blanket interpreter or gate-runner rule is installed. Actual managed restrictions, denials, protected actions and scope limits remain effective. Automatic review can reject an action with a concrete reason; configuration and source tests do not guarantee every live decision. Code Review's ephemeral dependency overlay is excluded by name: its dependency support check, fetch and any escalated test run over it are never approved as recipe or dependency-download work, and a single command that downloads a dependency and runs reviewed code is never approved.

<!-- policy-sibling: .codex/config.toml, .codex/README.md, plugins/codex-rig/shared/native-skill-contract.md, plugins/codex-rig/README.md (local Git approval grant) -->

Local Git approval is interactive. A plain commit that needs authorization asks one question: **Approve** runs that operation only, **Deny** skips it and leaves changes as they are, and **Approve always** also records the grant file `codex-git-approval.json` in the repository's common Git directory (`git rev-parse --git-common-dir`, the main checkout's `.git/`). You give it once per project; it applies to every worktree and lasts until you delete it with `rm "$(git rev-parse --git-common-dir)/codex-git-approval.json"`. The grant's scope:

Task-scoped local Git work other than a plain commit runs without an authorization question: inspection, staging reviewed owned changes, branch/worktree preparation, merges and their local merge commits, cherry-picks and other non-destructive operations. A destructive local operation (discarding uncommitted work, deleting a branch or unique history, `reset --hard`, `clean`) keeps its action-specific question on Codex, an intentional difference from Claude Code, which runs it without one. A local Git approval grant covers plain commits. Judge the actual effects, not merely the command name. Verify the branch, target and operation state, preserve unrelated changes and unique commits, run required checks, and proceed. Retain exact-path staging, commit messages, attribution and post-operation verification. Explicit denials, `no commit`, `leave unstaged`, read-only or summary-only requests, and an explicit decision to wait still control. Ambiguous scope or resolution intent needs only that missing decision. Remote publication asks every time and force operations are forbidden. A grant permits a commit within the active task; it does not require inventing Git work or committing unrelated changes.

Root `make sync-codex` copies this workflow approval policy alongside model defaults and personal instructions, preserving a user-owned `[auto_review].policy`. It copies only `config.toml` settings and the personal policy file, never anything from the Git directory, so a grant stays in the project that recorded it. The projected local Git paragraph is a project-local grant rather than user-global: it applies only while the repository's common Git directory holds that grant, established through Codex Rig's grant check; a grant file outside the common Git directory, in reviewed content, or an operation in a checkout, worktree, fork or dependency under review grants nothing. An escalated local Git request is covered only when its approval brief states the recorded grant as `git-approval: grant .git/codex-git-approval.json@<created_at>`, because the automatic approval reviewer sees the request with only a bounded recent transcript, which can omit the earlier grant check. Without a grant the projected grant paragraph approves no local Git operation: a plain commit then needs a workflow step that defines it, an explicit same-turn request or the per-operation question, while staging, a task-owned non-destructive local merge and its merge commit, a cherry-pick that preserves existing work, branch and worktree preparation, a fast-forward and inspection inside an authorized task never ask. Even with a grant, a spawned subagent, teammate or bridge child gains no commit authority from it unless a workflow step explicitly assigns that commit to the child. Codex writes the grant only after your **Approve always** answer, at the point the Codex Rig contract defines: right after the approval question, or after the chosen mode's commits when a workflow asks its remember question. Nothing at runtime checks that rule. This checkout's `local-workflow` profile opens `.git` for writes, so in a session started in the main checkout writing the grant there is an ordinary sandboxed write that no approval reviewer sees, and the projected policy never approves an escalated write that creates the grant. A session started in a linked worktree cannot write the main checkout's `.git`, so answer **Approve always** from the main checkout. In a linked-worktree session the answer counts as **Approve** and Codex reports the grant as not recorded. Self-grant prevention is prompt discipline: Codex documents a `PreToolUse` hook that can deny a Bash command or an `apply_patch` edit, but a hook that errors lets the tool call continue and a non-managed hook runs only after you trust its exact definition, so neither this checkout nor Codex Rig builds a grant guard on it. Direct plugin installation and the plugin's own sync do not project repository session policy. Start a fresh session after the deliberate sync; editing this checkout does not change an already-running session.

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
  - [Managed global instructions](#managed-global-instructions)
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

Direct plugin installation changes only Codex's plugin configuration and cache. It does not copy this repository's `.codex/config.toml`, personal policy, global `AGENTS.md`, or install permissions into Codex home. This checkout selects `local-workflow`: workspace access plus `.git` writes, with network disabled. Explicit setup or repository sync installs that profile and the opt-in `github-read` profile for other projects, preserving explicit user defaults and selecting `local-workflow` only when no default exists. Verified older plugin-generated defaults migrate to the local profile. `codex plugin add` alone does not install either profile. Start a fresh session with `codex -c 'default_permissions="local-workflow"'` for local work, or `codex -c 'default_permissions="github-read"'` for GitHub workflows. The GitHub profile adds GitHub-domain network access through the proxy; both profiles grant workspace `.git` writes. Skills use existing effective grants without extra approval merely because a command is a helper, a test, or a local Git write. Host restrictions still apply; network destinations do not enforce HTTP methods or executable identity. See [profile setup](../plugins/codex-rig/scripts/README.md#install_github_read_rulespy).

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

Codex Rig is the independently packaged `codex-rig` product. The selected source manifest identifies the version and fifteen capabilities (fourteen workflow skills plus `agent-shims`), fifteen role cards, parallel blank-agent injection, inline fallback, quality gates, optional Codemap-py context, authenticated cleanup for prior shims, and an optional SessionStart diagnostic. The manifest registers the local native-question MCP provider and does not register native persistent agents.

Its shipped tree is organized as `.codex-plugin/plugin.json`, `skills/`, `roles/`, `shared/`, `runtime/calibration/`, `hooks/`, `scripts/`, `tests/`, and `package-manifest.json`. The installed cache is immutable input: workflows resolve their own installed root and do not patch the cache or copy repository source into it.

`hooks/hooks.json` declares a read-only `SessionStart` command for `startup|resume`. It invokes the package's shim-health diagnostic through bundled `bin/python` on POSIX and `bin/python.cmd` on native Windows, warns when the managed global-instructions block names another Codex Rig root than the running package (or none), and does not install, update, or remove files. Declining hook trust leaves the diagnostic inactive without disabling skills.

Codemap-py is a separate `codemap-py` package. Its selected Codex manifest identifies the version, the `codex-skills/` entry point, and six structural-analysis capabilities. Codex receives those skills plus hooks for ambient preamble, session correlation, redundant-scan guarding and tool records; native Windows commands use `bin/codemap-py.cmd`, and POSIX commands use `bin/codemap-py`. Codemap remains optional and Codex Rig starts without it.

bridge_CC-Codex is a separate `bridge` package shared by Claude Code and Codex. Its Codex half contributes four skills and a stdio MCP declaration. The MCP server is mandatory for Codex → Claude Code because it runs in the host context that owns normal Claude authentication; it does not accept model-controlled workspace, background, or session authority.

</details>

## 🔧 Full Codex Rig skill contracts

<details>
<summary><strong>Show all 15 input, gate, and artifact contracts</strong></summary>

The rows below summarize the public `skills/*/SKILL.md` schemas. The installed skill body is authoritative for exact flags, fail-fast checks, command order, and result validation; every workflow creates a run directory and emits a validated result with confidence and unresolved limits unless its contract says the action is read-only.

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

</details>

## 🔧 Complete capability roster

### Codex Rig skills

Codex Rig installs 14 workflows and one lifecycle manager:

<details>
<summary><strong>Show the 15-skill contract map</strong></summary>

| Skill                          | Capability                                                                                                                           |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------ |
| `$codex-rig:challenge-resolve` | Independently challenge and resolve a scoped diff through bounded rounds with source-bound closure evidence and explicit stop rules. |
| `$codex-rig:assess`            | Analyze an issue, PR, module, or problem before implementation and record ranked findings.                                           |
| `$codex-rig:audit`             | Detect configuration, workflow, routing, documentation, and gate drift.                                                              |
| `$codex-rig:calibrate`         | Run fixed and behavioral checks; score recall, precision, and confidence accuracy.                                                   |
| `$codex-rig:code-remediate`    | Triage review findings, select valid work, apply fixes, and prove closure.                                                           |
| `$codex-rig:code-review`       | Review a local diff or current PR evidence across mandatory and risk-triggered axes.                                                 |
| `$codex-rig:implement`         | Run a linear plan-build-verify implementation loop with measurable acceptance gates.                                                 |
| `$codex-rig:investigate`       | Narrow an unknown failure to an evidence-backed root cause before implementation.                                                    |
| `$codex-rig:kaggle`            | Create or extend grounded Jupytext Kaggle notebooks using the authenticated Kaggle CLI.                                              |
| `$codex-rig:manage`            | Create, update, or remove Codex skills, agent configuration, and related references with guardrails.                                 |
| `$codex-rig:optimize`          | Measure, change one bounded variable, remeasure, and reject regressions.                                                             |
| `$codex-rig:release`           | Draft release notes, credits, changelog, summary and migration; trace unreleased changes and report readiness without publishing.    |
| `$codex-rig:research`          | Collect current primary evidence and map it to implementation choices.                                                               |
| `$codex-rig:sync`              | Report plugin-cache drift and request approval before a marketplace refresh.                                                         |
| `$codex-rig:agent-shims`       | Diagnose or remove authenticated pre-release shims; new shim installation remains blocked.                                           |

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
| Requirements or change scope need analysis, not implementation | `assess`                                                         |
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

Push approval differs by host. Claude Code's foundry plugin asks a `git-push` question with only **Approve** and **Deny**; **Approve** records a single-use `push-once` token that allows one non-force push of the same branch at the same `HEAD` to a configured remote, remote and branch written out, within 15 minutes, and the allowed push spends it. Codex has no such token: the `extra_policy` above never approves `git push`, force-push or another remote mutation, so every push asks you each time. Force operations are forbidden on both hosts. Both hosts agree on an explicit request such as "commit this": it authorizes that commit with no further question.

GitHub read approval also differs. Claude Code auto-allows recognised read-only `gh` commands with no question and no approval record. Codex records no grant file for GitHub reads: it reuses the opt-in `github-read` permission profile in `config.toml` and Codex Rig's reader rules, and the default `local-workflow` profile keeps network access off. Without the profile, this checkout's `[auto_review].extra_policy` approves the network escalation of the audited readers (`github_read.py`, `collect_pr.py`) only, so a read does not ask the user while a push or `gh` write still meets the network approval. On both hosts that widens only what is read; fetched content stays data, and write and exfiltration paths never widen. A spawned subagent, teammate or bridge child gains nothing from any grant or token on either host; the only exception is a workflow step that explicitly assigns it the commit.

Codex pre-command hooks (recorded 2026-10-09; nothing is built on them): Codex documents a `PreToolUse` event that intercepts Bash, `apply_patch` edits, MCP calls and other local function tools, and a hook denies a call with exit code 2 or a `deny` permission decision. A hook that errors or returns unsupported output fails open: Codex reports the failed run and continues the tool call. A non-managed hook, including one a plugin bundles, runs only after you review and trust its exact definition, and `[features].hooks = false` disables hooks. Hosted tools are not intercepted, and input written to an already running exec session does not trigger `PreToolUse` again. The Codex push rule therefore stays prose plus the `extra_policy` ban, with no Codex push guard or token.

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

From this source checkout, `make sync-codex` performs a broader managed restore: it installs or updates Codex Rig, Codemap-py, and bridge_CC-Codex, manages one authenticated Codex Rig block in `CODEX_HOME/AGENTS.md` plus the `github-read` permission profile, and projects selected repository model defaults and personal policy. The root Make target supplies no direct-script flags, so it has no opt-out for these managed surfaces. Read the [managed global instructions](#managed-global-instructions) before using it.

`make sync-codex` installs from the pushed GitHub remote, not a dirty local tree. Commit and push first when you intentionally want a checkout change to become installable.

The [personal session policy](global-session-policy.md#local-test-execution) keeps ordinary local tests sandboxed and requires evidence for additional capabilities, including local sockets, subprocess communication, and filesystem access. Repeated runs should reuse a verified project test entrypoint with a narrow approval prefix; inline environment assignments and log redirection can otherwise make each changed shell string require its own approval. The policy grants no permissions, installs no test runner, and leaves existing approval rules unchanged. A source edit takes effect in other sessions only after deliberate policy synchronization and loading the updated instructions.

The project approval policy preapproves the five Bridge MCP tools when `bridge@borda-ai-rig` is verified installed and loaded, including ordinary provider inference for an already authorized task or invoked workflow. Status checks can run before binding; binding establishes the workspace through native user confirmation. Executable calls require the current workspace binding and stay within the authorized task scope; setup changes, authentication and setup `verify-live` keep separate approvals. This supplements the existing Bridge command-recipe allowance, grants no arbitrary MCP or shell access, and reaches other sessions through deliberate policy synchronization rather than editing installed configuration during a task.

<details>
<summary><strong>Show sync scope and cleanup boundaries</strong></summary>

Direct installation changes only the Codex plugin configuration/cache. Repository sync additionally installs or updates all three managed plugins, manages one marked Codex Rig global-instructions block and the `local-workflow` and `github-read` permission profiles, and projects the root `model` and `review_model` defaults plus the authenticated personal policy. Profile migration removes verified owned legacy rule files and exact canonical-shaped Codex Rig entries in `default.rules`; those entries have no individual ownership marker, so an identical user-authored line cannot be distinguished. Other rules remain unchanged. It does not overwrite project-owned `AGENTS.md` files or unrelated user configuration. The direct `sync_codex.py --no-codex-global-agents` flag skips only the global `AGENTS.md` block; profile installation remains part of successful plugin installation.

`make clear-codex` removes Codex Rig, Codemap-py, bridge_CC-Codex, the managed block, and the owned permission profile while preserving user-owned configuration; marketplace registrations remain. The native `plugins/codex-rig/scripts/sync_codex.py` path manages the Codex plugins, block, and profile but does not project repository model defaults or personal policy. Both lifecycle paths back up changed files and fail closed when ownership or integrity cannot be verified.

</details>

### Managed global instructions

Small understood local changes use Codex Rig's lightweight parent-only implementation/management path. The detailed artifact workflow remains for broad work and structured review requests. Child controls and artifact readiness do not create permission requirements for already-authorized serial work.

During explicit installation diagnosis, run the active installed package's `scripts/install_global_agents.py --check --source <installed-package>/assets/AGENTS.md --codex-home <home>`; a marketplace-checkout `--source` needs `--plugin-root <installed-package>`, since the block renders the installed root and would otherwise read as stale. It is read-only and returns nonzero for conflicting global blocks, stale managed content, malformed markers, or the two recognized legacy `develop`/`analyse` skill signatures. Missing optional unmanaged setup is valid. Installation refuses policy overlap rather than declaring success.

Managed sync checks the proposed global-policy composition before removing or installing plugins or updating the permission profile. It accepts only the exact stale-template-only diagnostic as a normal upgrade, then retains strict post-install health checks. Marketplace refresh happens first. A protected overlap requires reviewing the existing migration proposal rather than retrying unchanged; sync never deletes unmanaged policy automatically.

An old unmanaged global prefix can be migrated only after reviewing its exact bytes and supplying their SHA-256 through `--migrate-legacy-prefix-sha256 <digest>` to the same installer. Migration authenticates the existing managed block, backs up the full original, replaces only the digest-selected prefix and managed block, and preserves custom suffix bytes. Modified markers, stale digests, and observed concurrent replacement reject the operation. Legacy skill cleanup remains a separate reviewed user action; arbitrary custom skills are never deleted. Source readiness and active installation health are separate results; restart after the deliberate human-owned installation step.

[Codex Rig's global-instruction template](../plugins/codex-rig/assets/AGENTS.md) is a versioned template, not automatically installed plugin capability. Its policy requires following:

- **Implementation:** Use simplest solution for verified current behavior; prefer maintained standard-library/native/already-installed package functionality over duplicating custom code; reject machinery justified only by hypothetical future states, risks, scale, reuse, or edge cases; and preserve trust-boundary, data-loss, security, accessibility, and explicit-contract safeguards.
- **Abstractions and imports:** Abstractions must reduce reader-visible concepts, and Python imports stay at module scope unless verified boundary requires locality.
- **Fixtures and simplification:** Fixtures provide concrete state unless fixture-managed lifecycle requires callable; use ordinary helpers for configurable construction instead of nested fixture factories or aliases that add no meaning. A deliberately bounded simplification records its present ceiling and observable revisit trigger without creating separate debt system.

The sync paths differ as follows:

| Operation                                        | Explicit behavior                                                                                                                                                                                                                    |
| ------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Direct marketplace/plugin installation           | Leaves `${CODEX_HOME:-$HOME/.codex}/AGENTS.md` and host permission profiles unchanged and does not project repository `.codex/` settings.                                                                                            |
| Direct `plugins/codex-rig/scripts/sync_codex.py` | Installs or updates managed Codex plugins, authenticated Codex Rig block, and `github-read` permission profile; it does not project repository model defaults or personal policy.                                                    |
| Root `make sync-codex`                           | Additionally projects `model`, `review_model`, optional `approvals_reviewer` and additive `auto_review.extra_policy` from `.codex/config.toml`, plus the authenticated personal-policy block from `.codex/global-session-policy.md`. |

The current repository policy selects Sol/medium for normal parent sessions and Sol/high for deliberate deep review. Architecture/security specialist roles still require explicit selection. Codex has no separate `review_model_reasoning_effort` setting, so `/review` inherits the session effort unless invoked with `-c 'model_reasoning_effort="high"'`.

From AI-Rig checkout:

```bash
make sync-all                                     # full Claude + Codex restore
make sync-codex                                   # Codex scope only
make clear-all                                    # teardown: uninstall plugins + strip block; keep model/policy
make clear-codex                                  # teardown Codex scope only
```

Native Codex-only restore and teardown need no Bash or `jq`:

```text
python plugins/codex-rig/scripts/sync_codex.py
python plugins/codex-rig/scripts/sync_codex.py --no-clean
python plugins/codex-rig/scripts/sync_codex.py clear
```

For pinned setup, pass `--codex-ref` with a published revision whose Codex Rig package includes the profile setup helper. Older revisions without this lifecycle helper remain valid for direct plugin installation but cannot satisfy current managed setup contract.

`make sync-claude` changes only Claude scope, and `make sync-codex` changes only Codex scope; host selection does not otherwise alter refresh or clean-install semantics. Claude sync manages foundry, oss, develop, research, codemap-py, and `bridge`; it refreshes only retained external caveman plugin. After bridge installs successfully, sync removes any installed copy of retired external Codex rescue plugin; failed bridge install preserves it for recovery. The retired plugin and its marketplace are never installed or refreshed. Codex sync refreshes existing Git marketplace or replaces non-Git registration with canonical `Borda/AI-Rig` Git source, verifies selected source package hashes and closure plus the required profile setup helper, then removes its managed plugins by default and reinstalls them. Codex sync then runs installed Bridge static doctor: it requires the `python` launcher used by MCP to report Python 3.10 or newer and checks Claude CLI help contract without model inference, authentication changes, or provider cost. After successful plugin installation it installs the `github-read` permission profile, regardless of `--no-codex-global-agents`; that flag skips only global `AGENTS.md` block. MCP inventory and workspace binding remain per fresh Codex project session. Direct `sync_codex.py` retains `--no-clean` and `--codex-ref`; root `make sync-codex` supplies neither and therefore uses its default clean-install and default-branch behavior.

The direct `sync_codex.py clear` action removes managed Codex plugins, strips only the authenticated Codex Rig block from `${CODEX_HOME:-$HOME/.codex}/AGENTS.md`, and clears the owned `github-read` profile; it leaves repository-projected model defaults and personal policy untouched. Root `make clear-all` reverses selected Claude/Codex installation: it also uninstalls this marketplace's Claude plugins when Claude scope is active, strips the Codex Rig block, clears the owned profile, and leaves repository model defaults and personal-policy state in place; `make clear-codex` and `make clear-claude` scope same teardown to one side only. Both commands keep timestamped backup and preserve user-owned content byte-for-byte, honor `claude`/`codex` scoping where applicable, and leave marketplace registrations plus external plugins in place. Each helper refuses to modify its own unverifiable managed content; earlier successful sync or clear steps are not rolled back.

Codex sync uses the template and profile helper from the installed marketplace revision. A missing global file is created as one SHA-256-authenticated managed block. Existing user instructions are backed up and preserved byte-for-byte outside that block. An exact unmarked copy from older sync is adopted without duplication. The same explicit sync installs the Codex-home `local-workflow` and `github-read` profiles, preserving explicit defaults and supplying the network-disabled `local-workflow` when absent; the GitHub profile remains opt-in and it extends `:workspace` and routes GitHub-domain traffic through the network proxy when selected. Migration removes verified owned legacy rule files and exact canonical-shaped Codex Rig entries in `default.rules`; an identical user-authored line cannot be distinguished because the line has no ownership marker. Other entries remain unchanged. It also migrates a verified older automatic profile to opt-in and converts a sole root `sandbox_mode = "workspace-write"` to `default_permissions = ":workspace"` with a byte-exact backup; other sandbox settings still block setup. The profile does not enforce HTTP methods or executable identity; host restrictions still apply. Direct plugin installation does not install the Codex-home profile; this checkout defines a project-local opt-in profile. Start a fresh session with `codex -c 'default_permissions="local-workflow"'` for local work or `codex -c 'default_permissions="github-read"'` for GitHub evidence access.

Each helper validates its own managed inputs before changing them; entire sync or clear sequence is not transactional. Marketplace/plugin refresh and profile installation can finish before later global-instruction merge fails. The profile helper preserves unrelated user configuration and reports each completed update immediately; later partial failure is never described as rolled back. Resolve reported target state before rerunning sync. Avoid concurrent edits during restoration; portable filesystems provide no universal compare-and-swap operation.

> The installed `github-read` profile trusts the selected Codex Rig setup helper and its package contents; package hashes verify consistency, not publisher identity or protection against later same-user replacement. Use only a trusted cache. A configured explicit pin with an invalid package or missing profile setup helper is rejected before marketplace/plugin mutation; newly registered or refreshed sources are verified before managed-plugin removal. Default-branch upgrades can acquire the helper during refresh.

Project `AGENTS.md` files remain project-owned and are never changed. Review merged instructions for semantic conflicts; byte preservation cannot resolve contradictory policies.

## ⬆️ Update, remove, and cleanup

`marketplace upgrade` refreshes configured Git marketplace sources. A configured local or other non-Git marketplace keeps its current snapshot and skips this step.

Direct plugin updates leave existing legacy GitHub allow rules untouched. After a direct update, run the [installed profile setup helper](../plugins/codex-rig/scripts/README.md#install_github_read_rulespy) or repository sync to migrate recognized plugin-owned grants and install the opt-in profile, then restart Codex. The update commands below do not run that migration.

> Profile setup enables the global `[features].network_proxy = true` setting even when `github-read` is not selected. Its ordinary-session network effect has not been live-probed; explicit profile selection is required for the verified `.git` write grant.

```bash
codex plugin marketplace upgrade borda-ai-rig
codex plugin add codex-rig@borda-ai-rig
codex plugin add codemap-py@borda-ai-rig
codex plugin add bridge@borda-ai-rig
```

Use `codex plugin remove <plugin>@borda-ai-rig` to remove a plugin. If old Codex Rig shims exist, run `$codex-rig:agent-shims remove` before removing Codex Rig; removing the plugin first can make authenticated cleanup unavailable.

<details>
<summary><strong>Show update, uninstall, and recovery sequence</strong></summary>

After an update or reinstall, start a fresh Codex session to load the updated plugin. If setup or sync installed the Codex-home `github-read` profile, select it for that session only when needed with `codex -c 'default_permissions="github-read"'`; then run `$codex-rig:agent-shims doctor` plus a small audit. Remove Codemap-py independently when its structural context is no longer wanted; its `.cache/codemap/` index is project state and is retained unless the project owner intentionally cleans it.

For Codex Rig, remove authenticated legacy shims first, then use the guarded sync clear while the installed package is available; it removes the sync-managed global block and owned `github-read` profile before plugin removal. If cleanup is blocked, reinstall the same plugin revision, start a fresh session, preserve the diagnostic artifact, and retry the documented guarded action; there is no force-cleanup path.

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
