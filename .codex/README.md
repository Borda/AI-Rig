# 🤖 Codex plugins in Borda's AI-Rig

← [Back to the project overview](../README.md) · [Codex Rig reference](../plugins/codex-rig/README.md) · [Codemap-py reference](../plugins/codemap-py/README.md) · [bridge_CC-Codex reference](../plugins/bridge_cc-codex/README.md) · [OpenCode Rig reference](../plugins/opencode-rig/README.md)

AI-Rig gives Codex three independently installable products:

- **Codex Rig** turns common engineering work into evidence-first workflows with shared gates, specialist role cards, and comparable reports.
- **Codemap-py** answers unresolved structural questions about Python imports, callers, coupling, renames, and affected tests from a local static index.
- **bridge_CC-Codex** sends bounded implementation, advice, and review requests from Codex to Claude Code through a host-launched MCP transport.

Install Codex Rig when you want a disciplined workflow. Add Codemap-py when Python structure is part of the uncertainty. Add bridge_CC-Codex when a task benefits from an independent Claude Code implementation, answer, or adversarial review.

> Contributing or want internals? See [DEVELOPMENT.md](DEVELOPMENT.md) for package layout, full skill contracts, artifact lifecycle, and sync internals.

**OpenCode Rig** is a separate npm package for [OpenCode](https://opencode.ai) (`@opencode/plugin`, Node 20+). It is not installed via `codex plugin add` — install it with `opencode plugin add opencode-rig@<version>` or from the repo as shown below. See the [OpenCode Rig reference](../plugins/opencode-rig/README.md) for full details.

## 🛡️ Approval policy of this checkout

This checkout selects native automatic approval review for required plugin workflow commands. Its additive `[auto_review].extra_policy`:

- Treats executable recipes, inline helper invocations, normal parameter substitutions, configured gates and already-authorized local commits as preapproved across every skill in `codex-rig@borda-ai-rig`, `codemap-py@borda-ai-rig`, and `bridge@borda-ai-rig`.
- Leaves other installed plugins on their existing policy.
- Has the reviewer evaluate complete commands, including nested gates; no blanket interpreter or gate-runner rule is installed.
- Keeps actual managed restrictions, denials, protected actions and scope limits effective. Automatic review can reject an action with a concrete reason; configuration and source tests do not guarantee every live decision.
- Excludes Code Review's ephemeral dependency overlay by name: its dependency support check, fetch and any escalated test run over it are never approved as recipe or dependency-download work, and a single command that downloads a dependency and runs reviewed code is never approved.

<!-- policy-sibling: .codex/config.toml, .codex/README.md, plugins/codex-rig/shared/native-skill-contract.md, plugins/codex-rig/README.md (local Git approval grant) -->

### Local Git approval

Local Git approval is interactive. A plain commit that needs authorization asks one question: **Approve** runs that operation only, **Deny** skips it and leaves changes as they are, and **Approve always** also records the grant file `codex-git-approval.json` in the repository's common Git directory (`git rev-parse --git-common-dir`, the main checkout's `.git/`).

You give it once per project; it applies to every worktree and lasts until you delete it with `rm "$(git rev-parse --git-common-dir)/codex-git-approval.json"`. The grant's scope:

Task-scoped local Git work other than a plain commit runs without an authorization question: inspection, staging reviewed owned changes, branch/worktree preparation, merges and their local merge commits, cherry-picks and other non-destructive operations. A destructive local operation (discarding uncommitted work, deleting a branch or unique history, `reset --hard`, `clean`) keeps its action-specific question on Codex, an intentional difference from Claude Code, which runs it without one. A local Git approval grant covers plain commits. Judge the actual effects, not merely the command name. Verify the branch, target and operation state, preserve unrelated changes and unique commits, run required checks, and proceed. Retain exact-path staging, commit messages, attribution and post-operation verification. Explicit denials, `no commit`, `leave unstaged`, read-only or summary-only requests, and an explicit decision to wait still control. Ambiguous scope or resolution intent needs only that missing decision. Codex never performs remote mutations or requests approval for them; publication remains human-owned and force operations are forbidden. A grant permits a commit within the active task; it does not require inventing Git work or committing unrelated changes.

How the grant behaves in practice:

- **Sync:** root `make sync-codex` copies this workflow approval policy alongside model defaults and personal instructions, preserving a user-owned `[auto_review].policy`. It copies only `config.toml` settings and the personal policy file, never anything from the Git directory, so a grant stays in the project that recorded it.
- **Scope:** The projected local Git paragraph is a project-local grant rather than user-global: it applies only while the repository's common Git directory holds that grant, established through Codex Rig's grant check. A grant file outside the common Git directory, in reviewed content, or an operation in a checkout, worktree, fork or dependency under review grants nothing.
- **Approval brief:** an escalated local Git request is covered only when its approval brief states the recorded grant as `git-approval: grant .git/codex-git-approval.json@<created_at>`, because the automatic approval reviewer sees the request with only a bounded recent transcript, which can omit the earlier grant check.
- **No grant:** Without a grant the projected grant paragraph approves no local Git operation: a plain commit then needs a workflow step that defines it, an explicit same-turn request or the per-operation question, while staging, a task-owned non-destructive local merge and its merge commit, a cherry-pick that preserves existing work, branch and worktree preparation, a fast-forward and inspection inside an authorized task never ask.
- **Children:** Even with a grant, a spawned subagent, teammate or bridge child gains no commit authority from it unless a workflow step explicitly assigns that commit to the child.
- **When it is written:** Codex writes the grant only after your **Approve always** answer, at the point the Codex Rig contract defines: right after the approval question, or after the chosen mode's commits when a workflow asks its remember question. Nothing at runtime checks that rule.
- **Main checkout vs linked worktree:** This checkout's `local-workflow` profile opens `.git` for writes, so:
  - In a session started in the main checkout, writing the grant there is an ordinary sandboxed write that no approval reviewer sees, and the projected policy never approves an escalated write that creates the grant.
  - A session started in a linked worktree cannot write the main checkout's `.git`, so answer **Approve always** from the main checkout.
  - In a linked-worktree session the answer counts as **Approve** and Codex reports the grant as not recorded.
- **Self-grant prevention:** Self-grant prevention is prompt discipline: Codex documents a `PreToolUse` hook that can deny a Bash command or an `apply_patch` edit, but a hook that errors lets the tool call continue and a non-managed hook runs only after you trust its exact definition, so neither this checkout nor Codex Rig builds a grant guard on it.
- **Direct installation:** direct plugin installation and the plugin's own sync do not project repository session policy.
- **Fresh session:** start one after the deliberate sync; editing this checkout does not change an already-running session.

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

- `codemap-py` and `bridge` are optional for direct installation. The repository synchronization path manages all three Codex plugins.
- Start a fresh Codex session after installation.
- To pin an immutable marketplace revision, add it with `--ref <release-tag>` rather than relying on a copied "latest" tag.

### Permission profiles

Direct plugin installation changes only Codex's plugin configuration and cache. It does not copy this repository's `.codex/config.toml`, personal policy, global `AGENTS.md`, or install permissions into Codex home. `codex plugin add` alone does not install either profile.

- **`local-workflow`** (selected by this checkout): workspace access plus `.git` writes, network disabled.
- **`github-read`** (opt-in): adds GitHub-domain network access through the proxy; also grants workspace `.git` writes.
- **Setup or repository sync** installs both profiles for other projects, preserving explicit user defaults and selecting `local-workflow` only when no default exists. Verified older plugin-generated defaults migrate to the local profile.

Start a fresh session with the profile you need:

```bash
codex -c 'default_permissions="local-workflow"'   # local work
codex -c 'default_permissions="github-read"'      # GitHub workflows
```

Skills use existing effective grants without extra approval merely because a command is a helper, a test, or a local Git write. Host restrictions still apply; network destinations do not enforce HTTP methods or executable identity. See [profile setup](../plugins/codex-rig/scripts/README.md#install_github_read_rulespy).

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

- **Codex Rig** (`codex-rig`): fifteen capabilities (fourteen workflow skills plus `agent-shims`), fifteen role cards, parallel blank-agent injection, inline fallback, quality gates, optional Codemap-py context, authenticated cleanup for prior shims, and an optional SessionStart diagnostic. It registers the local native-question MCP provider and no native persistent agents.
  - The installed cache is immutable input: workflows resolve their own installed root and do not patch the cache or copy repository source into it.
- **Health hook:** `hooks/hooks.json` declares a read-only `SessionStart` command for `startup|resume`. It runs the shim-health diagnostic and warns when the managed global-instructions block names another Codex Rig root than the running package (or none). It does not install, update, or remove files. Declining hook trust leaves the diagnostic inactive without disabling skills.
- **Codemap-py** (`codemap-py`): six structural-analysis skills plus hooks for ambient preamble, session correlation, redundant-scan guarding and tool records. Codemap remains optional and Codex Rig starts without it.
- **bridge_CC-Codex** (`bridge`): shared by Claude Code and Codex. Its Codex half contributes four skills and a stdio MCP declaration. The MCP server is mandatory for Codex → Claude Code because it runs in the host context that owns normal Claude authentication; it does not accept model-controlled workspace, background, or session authority.

</details>

## 🔧 Complete capability roster

### Codex Rig skills

Codex Rig installs 14 workflows and one lifecycle manager. Each workflow defines input, fail-fast, gate, artifact, and confidence contracts; full arguments and edge cases live in the owning [`SKILL.md` files](../plugins/codex-rig/skills/) and the [Codex Rig README](../plugins/codex-rig/README.md).

<details>
<summary><strong>Show the 15-skill contract map</strong></summary>

| Skill                                                                                    | Input                                                                                                  | Outcome                                                                                                                            |
| ---------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------- |
| [`$codex-rig:challenge-resolve`](../plugins/codex-rig/skills/challenge-resolve/SKILL.md) | Goal, scoped files, specification, symptom                                                             | Challenge a scoped diff and resolve findings through bounded rounds; stop on plateau, non-convergence or unavailable independence. |
| [`$codex-rig:assess`](../plugins/codex-rig/skills/assess/SKILL.md)                       | Question, scope, `local`/`github`/`report`/`ecosystem` mode                                            | Analyze before implementation; record an evidence ledger and ranked findings.                                                      |
| [`$codex-rig:audit`](../plugins/codex-rig/skills/audit/SKILL.md)                         | `config`/`skills`/`roles`/`all` scope; `upgrade` or `adversarial`                                      | Detect configuration, workflow, routing, documentation and gate drift.                                                             |
| [`$codex-rig:calibrate`](../plugins/codex-rig/skills/calibrate/SKILL.md)                 | `skills`/`agents`/`routing`/`all` scope; `fast` or `full`; `ab-test`/`apply`                           | Fixed and behavioral checks; score recall, precision and confidence accuracy.                                                      |
| [`$codex-rig:code-remediate`](../plugins/codex-rig/skills/code-remediate/SKILL.md)       | Findings source or review shorthand, report/PR mode                                                    | Triage findings, ask for selection, apply fixes, rerun gates; defer unselected critical/high work explicitly.                      |
| [`$codex-rig:code-review`](../plugins/codex-rig/skills/code-review/SKILL.md)             | Working-tree, path, commit or PR scope                                                                 | Review across mandatory and risk-triggered axes; no remote mutation.                                                               |
| [`$codex-rig:implement`](../plugins/codex-rig/skills/implement/SKILL.md)                 | Goal; `feature`/`fix`/`refactor`/`config`/`spike`; acceptance                                          | Baseline, smallest change, quality gates, residual risk.                                                                           |
| [`$codex-rig:investigate`](../plugins/codex-rig/skills/investigate/SKILL.md)             | Symptom, scope, `fast` or `full`                                                                       | Reproduce, rank hypotheses, falsify alternatives; stop at a confirmed cause or explicit uncertainty.                               |
| [`$codex-rig:kaggle`](../plugins/codex-rig/skills/kaggle/SKILL.md)                       | Competition slug, context, offline/resume/keep controls                                                | Ground schema via authenticated Kaggle CLI; write and verify a Jupytext notebook.                                                  |
| [`$codex-rig:manage`](../plugins/codex-rig/skills/manage/SKILL.md)                       | Create/update/delete/rename/permission intent, target                                                  | Resolve ownership and references, apply the smallest reversible edit, verify references.                                           |
| [`$codex-rig:optimize`](../plugins/codex-rig/skills/optimize/SKILL.md)                   | Goal, metric command/direction, guard, iteration limit                                                 | Change one bounded variable at a time; reject regressions.                                                                         |
| [`$codex-rig:release`](../plugins/codex-rig/skills/release/SKILL.md)                     | `notes`/`prepare`/`audit`/`demo`; range/version; `--changelog`, `--summary`, `--migration`, `--append` | Traced release notes, credits, changelog and readiness table; never publishes.                                                     |
| [`$codex-rig:research`](../plugins/codex-rig/skills/research/SKILL.md)                   | Question; `docs`/`sota`/`paper`/`methodology`/`code-fidelity`                                          | Current primary sources mapped to implementation choices, with confidence.                                                         |
| [`$codex-rig:sync`](../plugins/codex-rig/skills/sync/SKILL.md)                           | `check` or `refresh`; optional Git ref                                                                 | Report plugin-cache drift read-only; refresh only after explicit approval.                                                         |
| [`$codex-rig:agent-shims`](../plugins/codex-rig/skills/agent-shims/SKILL.md)             | Exactly one of `doctor`, `status`, `install`, `remove`                                                 | Diagnose read-only, report platform block, or guarded exact-digest removal.                                                        |

Every run creates a run directory and emits a validated result with confidence and unresolved limits unless its contract says the action is read-only. The parent retains final acceptance for runtime/API changes, executable verification, security, architecture, and release-blocking decisions.

</details>

### Codex Rig role cards

Codex Rig ships 15 canonical role cards. The tier is each card's `model_reasoning_effort`.

<details>
<summary><strong>Show all 15 role cards</strong></summary>

| Role                                                                          | Owns                                                                  | Tier   |
| ----------------------------------------------------------------------------- | --------------------------------------------------------------------- | ------ |
| [`delegation-lead`](../plugins/codex-rig/roles/delegation-lead/ROLE.md)       | Cost-aware decomposition and handover consolidation                   | high   |
| [`sw-engineer`](../plugins/codex-rig/roles/sw-engineer/ROLE.md)               | Implementation, APIs, types, reproducible Python/ML code              | medium |
| [`qa-specialist`](../plugins/codex-rig/roles/qa-specialist/ROLE.md)           | Regression proof, edge cases, executable acceptance                   | medium |
| [`squeezer`](../plugins/codex-rig/roles/squeezer/ROLE.md)                     | Profile-first performance and resource analysis                       | medium |
| [`doc-scribe`](../plugins/codex-rig/roles/doc-scribe/ROLE.md)                 | Public docs, docstrings, examples, changelogs, migrations             | high   |
| [`security-auditor`](../plugins/codex-rig/roles/security-auditor/ROLE.md)     | Trust boundaries, credentials, dependencies, supply chain             | high   |
| [`data-steward`](../plugins/codex-rig/roles/data-steward/ROLE.md)             | Dataset provenance, split integrity, leakage, pipelines               | high   |
| [`cicd-steward`](../plugins/codex-rig/roles/cicd-steward/ROLE.md)             | CI/CD, matrices, caching, publishing, flaky-run diagnosis             | high   |
| [`linting-expert`](../plugins/codex-rig/roles/linting-expert/ROLE.md)         | Ruff, mypy, pre-commit, suppression policy                            | medium |
| [`oss-shepherd`](../plugins/codex-rig/roles/oss-shepherd/ROLE.md)             | Triage, SemVer, deprecations, contributor workflow, release readiness | high   |
| [`solution-architect`](../plugins/codex-rig/roles/solution-architect/ROLE.md) | Read-only system design, public contracts, coupling, migrations       | high   |
| [`web-explorer`](../plugins/codex-rig/roles/web-explorer/ROLE.md)             | Current official docs, changelogs, migration evidence                 | medium |
| [`curator`](../plugins/codex-rig/roles/curator/ROLE.md)                       | Configuration hygiene, duplication, drift, weak gates                 | high   |
| [`challenger`](../plugins/codex-rig/roles/challenger/ROLE.md)                 | Adversarial review of plans, risky changes, no-finding conclusions    | high   |
| [`scientist`](../plugins/codex-rig/roles/scientist/ROLE.md)                   | Papers, hypotheses, methods, metrics, ablations                       | high   |

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

Codex Rig workflows write under `.reports/codex/<skill>/<canonical-safe-identity>/run-<NNN>/` for a bounded validated non-sensitive identity, and otherwise under `.reports/codex/<skill>/<timestamp>/`. Raw arguments are never serialized into paths. Assessed PR reviews use the identity `pr-<number>`.

- A completed run normally includes development or review notes, per-gate logs, confidence calibration, and a validated `result.json`.
- Required gate IDs are lint, format, types, tests, and review; a non-applicable gate needs an explicit reason.
- Codemap-py writes its default index to `.cache/codemap/<project>.json`. Queries report freshness, degradation, coverage, truncation, and blind spots; read that metadata before treating a list as complete.

> Generated artifacts are evidence, not authority. Inspect the code, tests, commands, and residual limits before accepting consequential work.

<details>
<summary><strong>Show artifact lifecycle and confidence gates</strong></summary>

```text
.reports/codex/<skill>/<timestamp>/
├── result.json
├── gates.json
├── gates.log
└── skill-specific evidence
```

- A local review or non-PR workflow allocates its run directory once.
- PR review collection begins in a temporary timestamped directory because current-branch input may not yet reveal a PR number. After authoritative `pr.json` succeeds, the run is promoted to the next numeric `.reports/codex/code-review/pr-<number>/run-<NNN>/` path; every later helper and artifact uses the printed promoted path. Failed pre-identity collection stays a timestamped unavailable diagnostic rather than an assessed PR review.
- A result is not complete because a file exists: requested output, gate outcomes, evidence paths, confidence, and unresolved risks must agree.
- Historical flat code-review, `review/`, and `resolve/` artifacts remain readable fallback inputs without migration; current canonical names are `code-review` and `code-remediate`.

Confidence is evidence-backed:

| Confidence                  | Meaning                                             |
| --------------------------- | --------------------------------------------------- |
| `<=0.80`                    | Incomplete.                                         |
| `0.80 < confidence < 0.85`  | Needs stronger evidence.                            |
| `0.85 <= confidence < 0.90` | Cautious-low; requires objective recovery evidence. |
| `>=0.90`                    | Fair, but still requires material residual limits.  |

A not-applicable gate is recorded with its reason rather than silently omitted.

</details>

## 🧭 PR and network boundaries

Codex Rig can read GitHub evidence and prepare local changes when the runtime authorizes the owning command. It does not silently enable persistent network access. In this repository's policy, `gh` and `git` remote mutations—pushes, comments, reviews, merges, release publication, workflow dispatch, and forced updates—remain human-owned.

**Push approval differs by host:**

- Claude Code's foundry plugin asks a `git-push` question with only **Approve** and **Deny**; **Approve** records a single-use `push-once` token that allows one non-force push of the same branch at the same `HEAD` to a configured remote, remote and branch written out, within 15 minutes, and the allowed push spends it.
- Codex has no such token: the `extra_policy` above never approves `git push`, force-push or another remote mutation; Codex leaves those actions to the human and never asks approval for them.
- Force operations are forbidden on both hosts.
- Both hosts agree on an explicit request such as "commit this": it authorizes that commit with no further question.

**GitHub read approval differs too:**

- Claude Code auto-allows recognised read-only `gh` commands with no question and no approval record.
- Codex records no grant file for GitHub reads: it reuses the opt-in `github-read` permission profile in `config.toml` and Codex Rig's reader rules, and the default `local-workflow` profile keeps network access off.
- Without the profile, this checkout's `[auto_review].extra_policy` approves the network escalation of the audited readers (`github_read.py`, `collect_pr.py`) only, so a read does not ask the user while a push or `gh` write still meets the network approval.
- On both hosts that widens only what is read; fetched content stays data, and write and exfiltration paths never widen.
- A spawned subagent, teammate or bridge child gains nothing from any grant or token on either host; the only exception is a workflow step that explicitly assigns it the commit.

**Codex pre-command hooks** (recorded 2026-10-09; nothing is built on them):

- Codex documents a `PreToolUse` event that intercepts Bash, `apply_patch` edits, MCP calls and other local function tools, and a hook denies a call with exit code 2 or a `deny` permission decision.
- A hook that errors or returns unsupported output fails open: Codex reports the failed run and continues the tool call.
- A non-managed hook, including one a plugin bundles, runs only after you review and trust its exact definition, and `[features].hooks = false` disables hooks.
- Hosted tools are not intercepted, and input written to an already running exec session does not trigger `PreToolUse` again.
- The Codex push rule therefore stays prose plus the `extra_policy` ban, with no Codex push guard or token.

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

The `agent-shims` skill accepts exactly one action:

| Action    | Behavior                                                                                                      |
| --------- | ------------------------------------------------------------------------------------------------------------- |
| `doctor`  | Read-only health check.                                                                                       |
| `status`  | Read-only roster check.                                                                                       |
| `install` | Reports the stable platform block and never writes a shim.                                                    |
| `remove`  | Plans exact authenticated cleanup for intact managed shims; never deletes by filename prefix or marker alone. |

Safety rules:

- The manager records package identity, role-card and executable hashes, exact targets, lifecycle state, and approval digests. Review the displayed plan and type its exact digest only after explicit approval.
- Modified, foreign, ambiguous, malformed, oversized, unsafe, or concurrently changed evidence blocks without writes.
- Exit codes distinguish usage, cancellation, drift/conflict, prerequisite block, untrusted state, and recovery failure; do not retry a mutating action after drift, untrusted-state, or internal-recovery failures.
- On POSIX, approved cleanup can recover one recognized interrupted transaction using its separate recovery digest; repeat the original action after recovery. Windows supports package verification and read-only inventory but not shim mutation.
- Uninstalling Codex Rig before cleanup leaves thin shims unavailable: reinstall the plugin, start a fresh session, run `doctor`, then run the guarded `remove`.

</details>

## 🔗 Codemap-py structural context

<details>
<summary><strong>Show index, query, freshness, and Codex limitations</strong></summary>

- Codemap-py scans Python source into `.cache/codemap/<project>.json`. The canonical CLI is `codemap-py index`, `codemap-py query`, `codemap-py doctor --json`, and `codemap-py integrate`; the Codex skills are the supported installed entry point because Codex does not add the plugin's `bin/` directory to `PATH`.
- The dispatcher requires CPython `>=3.11,<3.15`; an unavailable eligible interpreter is a named compatibility failure, not an empty result.
- Read each result's `index` block. `stale`, `degraded`, `query_complete`, `confidence`, `truncated`, `total_available`, and `not_covered` describe different limits.
- Query-time incremental self-heal can write unless `SCAN_NO_AUTOBUILD=1`; an explicit scan is predictable after a clone, branch switch, or large change.
- Static AST evidence does not prove dynamic dispatch, callbacks, string imports, inheritance, external consumers, runtime behavior, or test success.
- Codex Rig's `implement`, `investigate`, and `optimize` routes may probe the public Codemap CLI once and persist one status/artifact for specialists. Statuses are `available`, `absent`, `stale`, `incompatible`, `degraded`, `stale+degraded`, and `skipped`; absence or incompatibility falls back to bounded source inspection. The adapter never reads Codemap cache internals or imports Codemap Python code.

</details>

## 🧭 Current limitations

- Persistent named-agent selection is not verifiable in current Codex, so new thin-shim installation is platform-blocked. Blank-agent injection and disclosed inline fallback are the supported routes.
- Requested role model, effort, sandbox, and approval settings are not automatically proof of observed runtime controls. Safety-critical workflows must record and enforce the controls they can verify.
- Codemap-py is static Python analysis. Dynamic dispatch, callbacks, string imports, inheritance, external consumers, and runtime outcomes still require source inspection or tests.
- Codemap-py's dispatcher currently requires CPython `>=3.11,<3.15`. Codex lacks Claude's ambient Codemap status, telemetry correlation hook, and redundant-scan guard.
- Codex Rig's authenticated cleanup of legacy shims requires a POSIX local filesystem; the workflows, package checks, sync, and read-only diagnostics otherwise target Windows, macOS, and Linux.
- Possible future work is not a commitment. The installed package, skill/role contracts, tests, and documented fallbacks define current support.

## 🔄 Direct install versus repository sync

Direct marketplace installation is the public path and leaves global/project instructions alone: it changes only the Codex plugin configuration and cache.

From this source checkout, `make sync-codex` performs a broader managed restore:

- Installs or updates Codex Rig, Codemap-py, and bridge_CC-Codex.
- Manages one authenticated Codex Rig block in `CODEX_HOME/AGENTS.md` plus the `local-workflow` and `github-read` permission profiles.
- Projects selected repository model defaults (`model`, `review_model`) and personal policy.
- Supplies no direct-script flags, so it has no opt-out for these managed surfaces. Read the [managed global instructions](#managed-global-instructions) before using it.
- Installs from the pushed GitHub remote, not a dirty local tree. Commit and push first when you intentionally want a checkout change to become installable.

Related policy:

- **Local tests:** the [personal session policy](global-session-policy.md#local-test-execution) keeps ordinary local tests sandboxed and requires evidence for additional capabilities, including local sockets, subprocess communication, and filesystem access.
  - Repeated runs should reuse a verified project test entrypoint with a narrow approval prefix; inline environment assignments and log redirection can otherwise make each changed shell string require its own approval.
  - The policy grants no permissions, installs no test runner, and leaves existing approval rules unchanged.
  - A source edit takes effect in other sessions only after deliberate policy synchronization and loading the updated instructions.
- **Bridge MCP tools:** the project approval policy preapproves the five Bridge MCP tools when `bridge@borda-ai-rig` is verified installed and loaded, including ordinary provider inference for an already authorized task or invoked workflow.
  - Status checks can run before binding; binding establishes the workspace through native user confirmation.
  - Executable calls require the current workspace binding and stay within the authorized task scope; setup changes, authentication and setup `verify-live` keep separate approvals.
  - This supplements the existing Bridge command-recipe allowance, grants no arbitrary MCP or shell access, and reaches other sessions through deliberate policy synchronization rather than editing installed configuration during a task.
- **Bridge repository-content consent:** this is a separate explicit stage in Bridge setup. The operator grants or revokes it through the installed `bridge_consent.py` helper; plugin installation alone grants nothing.
  - Root policy sync preserves that existing exact approved block in `auto_review.extra_policy`, rejects edited/unsupported blocks, and cannot seed one from repository defaults or restore a revoked grant.
  - From a source checkout, `make sync-all` or `make sync-codex APPROVE_CLAUDE_CONSENT=1` records the grant only with operator proof, an interactive terminal or a one-time `BRIDGE_OPERATOR_TOKEN` from `bridge_consent.py issue-token`.
  - Otherwise the implicit `make sync-all` default prints `consent unchanged: needs operator terminal or BRIDGE_OPERATOR_TOKEN` and the sync still succeeds, while an explicit `APPROVE_CLAUDE_CONSENT=1` fails.
  - Existing project or managed policy may override user configuration, so stored consent does not prove current-session approval.

### Sync commands

From the AI-Rig checkout:

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

For pinned setup, pass `--codex-ref` with a published revision whose Codex Rig package includes the profile setup helper. Older revisions without this lifecycle helper remain valid for direct plugin installation but cannot satisfy the current managed setup contract.

| Operation                                        | Explicit behavior                                                                                                                                                                                                                    |
| ------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Direct marketplace/plugin installation           | Leaves `${CODEX_HOME:-$HOME/.codex}/AGENTS.md` and host permission profiles unchanged and does not project repository `.codex/` settings.                                                                                            |
| Direct `plugins/codex-rig/scripts/sync_codex.py` | Installs or updates managed Codex plugins, authenticated Codex Rig block, and `github-read` permission profile; it does not project repository model defaults or personal policy.                                                    |
| Root `make sync-codex`                           | Additionally projects `model`, `review_model`, optional `approvals_reviewer` and additive `auto_review.extra_policy` from `.codex/config.toml`, plus the authenticated personal-policy block from `.codex/global-session-policy.md`. |

The current repository policy selects Sol/medium for normal parent sessions and Sol/high for deliberate deep review. Architecture/security specialist roles still require explicit selection. Codex has no separate `review_model_reasoning_effort` setting, so `/review` inherits the session effort unless invoked with `-c 'model_reasoning_effort="high"'`.

`make sync-claude` changes only Claude scope, and `make sync-codex` changes only Codex scope; host selection does not otherwise alter refresh or clean-install semantics.

<details>
<summary><strong>Show sync scope, cleanup boundaries, and failure behavior</strong></summary>

**What Codex sync does**

- Refreshes an existing Git marketplace, or replaces a non-Git registration with the canonical `Borda/AI-Rig` Git source, then verifies selected source package hashes and closure plus the required profile setup helper.
- Removes its managed plugins by default and reinstalls them.
- Runs the installed Bridge static doctor: it requires the `python` launcher used by MCP to report Python 3.10 or newer and checks the Claude CLI help contract without model inference, authentication changes, or provider cost.
- Installs the `github-read` permission profile after successful plugin installation, regardless of `--no-codex-global-agents`; that flag skips only the global `AGENTS.md` block.
- MCP inventory and workspace binding remain per fresh Codex project session.
- Direct `sync_codex.py` retains `--no-clean` and `--codex-ref`; root `make sync-codex` supplies neither and therefore uses its default clean-install and default-branch behavior.

**What Claude sync does (for reference)**

- Manages foundry, oss, develop, research, codemap-py, and `bridge`; it refreshes only the retained external caveman plugin.
- After bridge installs successfully, removes any installed copy of the retired external Codex rescue plugin; a failed bridge install preserves it for recovery. The retired plugin and its marketplace are never installed or refreshed.

**Global instructions and profiles**

- A missing global file is created as one SHA-256-authenticated managed block. Existing user instructions are backed up and preserved byte-for-byte outside that block. An exact unmarked copy from older sync is adopted without duplication.
- Sync installs the Codex-home `local-workflow` and `github-read` profiles, preserving explicit defaults and supplying the network-disabled `local-workflow` when absent. The GitHub profile remains opt-in; when selected it extends `:workspace` and routes GitHub-domain traffic through the network proxy.
- Migration removes verified owned legacy rule files and exact canonical-shaped Codex Rig entries in `default.rules`; an identical user-authored line cannot be distinguished because the line has no ownership marker. Other entries remain unchanged.
- Migration also converts a verified older automatic profile to opt-in and a sole root `sandbox_mode = "workspace-write"` to `default_permissions = ":workspace"` with a byte-exact backup; other sandbox settings still block setup.
- The profile does not enforce HTTP methods or executable identity; host restrictions still apply. It does not overwrite project-owned `AGENTS.md` files or unrelated user configuration.

**Teardown**

- `make clear-codex` removes Codex Rig, Codemap-py, bridge_CC-Codex, the managed block, and the owned permission profile while preserving user-owned configuration; marketplace registrations remain.
- `make clear-all` also uninstalls this marketplace's Claude plugins when Claude scope is active; `make clear-claude` scopes the same teardown to Claude only. All leave repository model defaults, personal-policy state, marketplace registrations, and external plugins in place.
- Direct `sync_codex.py clear` removes managed Codex plugins, strips only the authenticated Codex Rig block from `${CODEX_HOME:-$HOME/.codex}/AGENTS.md`, and clears the owned `github-read` profile.
- Every lifecycle path keeps timestamped backups, preserves user-owned content byte-for-byte, and refuses to modify its own unverifiable managed content.

**Failure behavior**

- Each helper validates its own managed inputs before changing them; the entire sync or clear sequence is not transactional. Marketplace/plugin refresh and profile installation can finish before a later global-instruction merge fails. Completed updates are reported immediately and are never described as rolled back.
- Resolve the reported target state before rerunning sync, and avoid concurrent edits during restoration.

> The installed `github-read` profile trusts the selected Codex Rig setup helper and its package contents; package hashes verify consistency, not publisher identity or protection against later same-user replacement. Use only a trusted cache.
>
> A configured explicit pin with an invalid package or missing profile setup helper is rejected before marketplace/plugin mutation; newly registered or refreshed sources are verified before managed-plugin removal. Default-branch upgrades can acquire the helper during refresh.

</details>

Project `AGENTS.md` files remain project-owned and are never changed. Review merged instructions for semantic conflicts; byte preservation cannot resolve contradictory policies.

### Managed global instructions

Small understood local changes use Codex Rig's lightweight parent-only implementation/management path. The detailed artifact workflow remains for broad work and structured review requests. Child controls and artifact readiness do not create permission requirements for already-authorized serial work.

**Diagnosing an installation**

- Run the active installed package's `scripts/install_global_agents.py --check --source <installed-package>/assets/AGENTS.md --codex-home <home>`.
- A marketplace-checkout `--source` needs `--plugin-root <installed-package>`, since the block renders the installed root and would otherwise read as stale.
- The check is read-only and returns nonzero for conflicting global blocks, stale managed content, malformed markers, or the two recognized legacy `develop`/`analyse` skill signatures. Missing optional unmanaged setup is valid.
- Installation refuses policy overlap rather than declaring success.

**How managed sync handles it**

- It checks the proposed global-policy composition before removing or installing plugins or updating the permission profile, and accepts only the exact stale-template-only diagnostic as a normal upgrade. Strict post-install health checks remain.
- Marketplace refresh happens first.
- A protected overlap requires reviewing the existing migration proposal rather than retrying unchanged; sync never deletes unmanaged policy automatically.

**Migrating an old unmanaged global prefix**

1. Review the prefix's exact bytes.
2. Supply their SHA-256 through `--migrate-legacy-prefix-sha256 <digest>` to the same installer.
3. Migration authenticates the existing managed block, backs up the full original, replaces only the digest-selected prefix and managed block, and preserves custom suffix bytes.
4. Modified markers, stale digests, and observed concurrent replacement reject the operation.

Legacy skill cleanup remains a separate reviewed user action; arbitrary custom skills are never deleted. Source readiness and active installation health are separate results; restart after the deliberate human-owned installation step.

[Codex Rig's global-instruction template](../plugins/codex-rig/assets/AGENTS.md) is a versioned template, not automatically installed plugin capability. Its policy requires following:

- **Implementation:** Use simplest solution for verified current behavior; prefer maintained standard-library/native/already-installed package functionality over duplicating custom code; reject machinery justified only by hypothetical future states, risks, scale, reuse, or edge cases; and preserve trust-boundary, data-loss, security, accessibility, and explicit-contract safeguards.
- **Abstractions and imports:** Abstractions must reduce reader-visible concepts, and Python imports stay at module scope unless verified boundary requires locality.
- **Fixtures and simplification:** Fixtures provide concrete state unless fixture-managed lifecycle requires callable; use ordinary helpers for configurable construction instead of nested fixture factories or aliases that add no meaning. A deliberately bounded simplification records its present ceiling and observable revisit trigger without creating separate debt system.

## ⬆️ Update, remove, and cleanup

`marketplace upgrade` refreshes configured Git marketplace sources. A configured local or other non-Git marketplace keeps its current snapshot and skips this step.

```bash
codex plugin marketplace upgrade borda-ai-rig
codex plugin add codex-rig@borda-ai-rig
codex plugin add codemap-py@borda-ai-rig
codex plugin add bridge@borda-ai-rig
```

- Direct plugin updates leave existing legacy GitHub allow rules untouched. The update commands above do not run profile migration: afterwards, run the [installed profile setup helper](../plugins/codex-rig/scripts/README.md#install_github_read_rulespy) or repository sync to migrate recognized plugin-owned grants and install the opt-in profile, then restart Codex.
- To remove a plugin, run `codex plugin remove <plugin>@borda-ai-rig`.
- If old Codex Rig shims exist, run `$codex-rig:agent-shims remove` before removing Codex Rig; removing the plugin first can make authenticated cleanup unavailable.

> Profile setup enables the global `[features].network_proxy = true` setting even when `github-read` is not selected. Its ordinary-session network effect has not been live-probed; explicit profile selection is required for the verified `.git` write grant.

<details>
<summary><strong>Show update, uninstall, and recovery sequence</strong></summary>

1. After an update or reinstall, start a fresh Codex session to load the updated plugin.
2. If setup or sync installed the Codex-home `github-read` profile, select it for that session only when needed with `codex -c 'default_permissions="github-read"'`.
3. Run `$codex-rig:agent-shims doctor` plus a small audit.

Remove Codemap-py independently when its structural context is no longer wanted; its `.cache/codemap/` index is project state and is retained unless the project owner intentionally cleans it.

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
- [OpenCode Rig product and workflow reference](../plugins/opencode-rig/README.md)

License: [Apache-2.0](../LICENSE).
