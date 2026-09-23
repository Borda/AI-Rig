# 🎭 Codex Rig Roles

Each subdirectory under `roles/` holds one `ROLE.md` — role card that packages Codex specialist: its model, reasoning effort, sandbox and approval posture, and trigger/evidence/execution/handover/confidence contract. This README explains the active GPT-6 routing map and the card schema checked by calibration.

<details open>
<summary><strong>Navigation</strong></summary>

## 📋 Contents

- [GPT-6 model and effort routing](#-gpt-6-model-and-effort-routing)
- [Role roster](#-role-roster)
- [Role-card contract](#-role-card-contract)
- [Fallback modes](#-fallback-modes)

</details>

> Value at a glance: fifteen versioned role cards make specialist behavior portable and auditable while keeping model selection, sandbox posture, and fallback behavior explicit.

> Current limits at a glance: role cards are behavioral profiles, not proof of native persistent-agent selection; Sol roles are read-only advisory paths, and new shim installation remains platform-blocked.

## 🎚️ GPT-6 model and effort routing

Every role runs on `gpt-6-sol` or `gpt-6-luna` with independently assigned `medium` or `high` reasoning effort. All fifteen roles share `approval_policy: on-request` and `fallback_modes: [shim, built-in-injected, inline]`; sandbox mode remains role-specific. Astra has no standing route.

| Model        | Effort   | Roles                                                                               |
| ------------ | -------- | ----------------------------------------------------------------------------------- |
| `gpt-6-sol`  | `medium` | `sw-engineer`, `qa-specialist`, `squeezer`                                          |
| `gpt-6-sol`  | `high`   | `challenger`, `data-steward`, `scientist`, `security-auditor`, `solution-architect` |
| `gpt-6-luna` | `medium` | `linting-expert`, `web-explorer`                                                    |
| `gpt-6-luna` | `high`   | `cicd-steward`, `curator`, `delegation-lead`, `doc-scribe`, `oss-shepherd`          |

### Rationale

<details>
<summary><strong>Evidence behind tier and sandbox assignments</strong></summary>

The GPT-6 assignments are an explicit user-directed rollout. `runtime/calibration/accepted-route-evidence.json` records the current model-and-effort map in `active_assignments`, including parent Sol/medium and deep-review Sol/high requiring an explicit effort override. `active_assignment_basis` marks GPT-6 quality/cost evidence pending; the `historical_assignments` and `adjudication` sections retain the separate GPT-5.6 paid baseline and explain the older decisions:

- `delegation-lead` used Luna by explicit human override while the older Terra parent retained implementation and executable acceptance.
- `cicd-steward`, `doc-scribe`, `linting-expert`, `oss-shepherd`, and `web-explorer` used Luna by a second override for bounded support.
- `adjudication.luna_strict_failure_preserved: true` means strict-route calibration result — Luna failed strict quality bar on its own (`luna-score.json` records `strict_status: "fail"`) — is kept on record rather than silently overwritten by human override. The override changes assignment; it does not erase evidence that produced different strict answer.
- The adjudication rule itself only accepts evidence-derived candidate "with zero pair quality regressions and either mean F1 gain >= 0.01 or geometric-mean normalized cost ratio \<= 1.0" — human overrides above are recorded as explicit exceptions to that rule, not replacements for it.

`sandbox_mode` is set per role, independent of tier: `read-only` for six analysis or advisory roles — `challenger`, `security-auditor`, `solution-architect`, `squeezer`, `oss-shepherd`, `web-explorer` — and `workspace-write` for remaining nine, which are expected to produce or modify artifacts as part of their job.

### Task-difficulty selection

Role selection uses canonical [model-difficulty policy](../shared/specialist-orchestration.md#delegation-lead-and-model-routing). Luna owns bounded support and curation; Sol parent owns behavior and executable verification; architecture/security specialist roles still require explicit selection. The routing record must cite task boundary or observed insufficiency before model or effort escalation; cost alone is insufficient.

</details>

## 🤖 Role roster

| Role                 | Tier | Sandbox mode    | Purpose                                                                                                 |
| -------------------- | ---- | --------------- | ------------------------------------------------------------------------------------------------------- |
| `solution-architect` | Sol  | read-only       | System-design specialist for architecture, public API contracts, migrations, and module boundaries.     |
| `security-auditor`   | Sol  | read-only       | Security specialist for Python/web trust boundaries, ML supply chains, secrets, and CI/CD permissions.  |
| `sw-engineer`        | Sol  | workspace-write | Implementation specialist for production code, bug fixes, refactors, and typed public API changes.      |
| `qa-specialist`      | Sol  | workspace-write | Testing specialist for regression proof, risk-proportional edge coverage, and independent verification. |
| `challenger`         | Sol  | read-only       | Adversarial reviewer for plans, architecture, migrations, releases, and non-trivial diffs.              |
| `curator`            | Luna | workspace-write | Configuration-quality specialist for instruction hygiene, routing clarity, duplication, and drift.      |
| `data-steward`       | Sol  | workspace-write | ML data-pipeline integrity specialist for datasets, splits, labels, transforms, and leakage prevention. |
| `scientist`          | Sol  | workspace-write | ML research specialist for paper analysis, hypotheses, ablations, and evaluation protocols.             |
| `squeezer`           | Sol  | read-only       | Performance specialist for throughput, latency, memory, GPU utilization, and profiling evidence.        |
| `doc-scribe`         | Luna | workspace-write | Documentation specialist for public API docs, docstrings, README content, and changelogs.               |
| `cicd-steward`       | Luna | workspace-write | CI/CD reliability specialist for GitHub Actions, release automation, and flaky-CI diagnosis.            |
| `delegation-lead`    | Luna | workspace-write | Cost-aware orchestration specialist for decomposing work and consolidating specialist evidence.         |
| `linting-expert`     | Luna | workspace-write | Static-analysis specialist for Ruff, mypy, pre-commit, and suppression hygiene.                         |
| `oss-shepherd`       | Luna | read-only       | Open-source lifecycle specialist for issue triage, semantic versioning, and release readiness.          |
| `web-explorer`       | Luna | read-only       | External-evidence specialist for official documentation, release notes, and version verification.       |

## 🤖 Role-card contract

<details open>
<summary><strong>Required frontmatter and body schema</strong></summary>

Every `roles/<role_id>/ROLE.md` follows one fixed schema, and `runtime/calibration/run.py`'s `check_agents()` enforces it mechanically — role card missing any required field or section fails calibration.

**Frontmatter (7 required fields):**

| Field                    | Constraint                                                      |
| ------------------------ | --------------------------------------------------------------- |
| `role_id`                | Must match containing directory name.                           |
| `name`                   | Must be `codex-rig-<role_id>`.                                  |
| `model`                  | `gpt-6-sol` or `gpt-6-luna` — see routing table above.          |
| `model_reasoning_effort` | `medium` or `high` per role.                                    |
| `approval_policy`        | `on-request` for every role.                                    |
| `sandbox_mode`           | `read-only` or `workspace-write`.                               |
| `fallback_modes`         | `[shim, built-in-injected, inline]` for every role — see below. |

**Body (5 required `##` sections, mechanically enforced by `check_agents()`):**

1. **Trigger and skip boundaries** — when role fires, when it skips, and what it explicitly is not for. Keeps routing between roles unambiguous.
2. **Evidence ownership** — what role must read or establish before acting, and what it must record (rejected alternatives, tradeoffs, verified-vs-assumed state) as it works.
3. **Execution constraints** — house style, conventions, and hard "do not" rules role must respect, plus which other role owns adjacent work it must hand off instead of doing itself.
4. **Handover contract** — exact ordered content role must return to its parent or caller.
5. **Confidence contract** — 0–1 confidence score role must report, ≥0.90 bar for completion claim, and instruction to name every material evidence gap rather than omit it.

> **Selection boundary (advisory only — not part of the enforced 5)**: `solution-architect` and `security-auditor` additionally carry an explicit-user-selection rule and read-only advisory boundary in their own `ROLE.md` prose. `check_agents()`'s enforced-sections tuple has exactly 5 entries and does not check for this section; no other Sol role carries or needs it.

A role card that satisfies this contract is portable: any consumer of calibration harness can parse its frontmatter for routing and its required sections for behavior, without reading role-specific prose.

</details>

## 🔗 Fallback modes

<details open>
<summary><strong>Three-step portable role routing order</strong></summary>

Role cards expose `fallback_modes: [shim, built-in-injected, inline]`, but that field is not promise that persistent shims are attempted first. Canonical runtime routing follows this order:

1. **Runtime-provided blank/default subagent** — inject complete exact role-card bytes before narrow context pack.
2. **Inline pass** — apply exact role card in parent context and report that independence is false.
3. **`unavailable`** — stop when runtime cannot provide safe route or cannot prove mandatory profile setting.

Persistent named shims remain platform-blocked until Codex exposes verifiable custom-agent selector and fresh-session probe proves that selected TOML was consumed. Their lifecycle manager remains available for diagnosis and authenticated cleanup of prior development installations. Do not retry another route because specialist disagreed, returned finding, or failed acceptance gate.

</details>
