# 🎭 Codex Rig Roles

Each subdirectory under `roles/` holds one `ROLE.md` — role card that packages Codex specialist: its reasoning effort, sandbox and approval posture, and trigger/evidence/execution/handover/confidence contract. This README explains the effort guidance and the card schema checked by calibration.

<details open>
<summary><strong>Navigation</strong></summary>

## 📋 Contents

- [Effort and role routing](#-effort-and-role-routing)
- [Role roster](#-role-roster)
- [Role-card contract](#-role-card-contract)
- [Fallback modes](#-fallback-modes)

</details>

> Value at a glance: fifteen versioned role cards make specialist behavior portable and auditable while keeping model selection, sandbox posture, and fallback behavior explicit.

> Current limits at a glance: role cards are behavioral profiles, not proof of native persistent-agent selection; the `solution-architect` and `security-auditor` roles are read-only advisory paths, and new shim installation remains platform-blocked.

## 🎚️ Effort and role routing

Role cards define `medium` or `high` reasoning effort, not a model. The user's explicit model choice takes precedence; a concrete model may be selected for an actual subagent dispatch. All fifteen roles share `approval_policy: on-request` and `fallback_modes: [shim, built-in-injected, inline]`; sandbox mode remains role-specific.

Historical paired calibration evidence and human overrides remain in `runtime/calibration/accepted-route-evidence.json`. They document past decisions and strict failures, not proof of current model quality or cost.

Role selection follows [task routing](../shared/specialist-orchestration.md#delegation-lead-and-model-routing). Parent owns integration and executable verification; architecture and security advisory roles require explicit selection. Record evidence for any escalation.

## 🤖 Role roster

| Role                 | Sandbox mode    | Purpose                                                                                                                     |
| -------------------- | --------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `solution-architect` | read-only       | System-design specialist for architecture, public API contracts, migrations, and module boundaries.                         |
| `security-auditor`   | read-only       | Security specialist for Python/web trust boundaries, ML supply chains, secrets, and CI/CD permissions.                      |
| `sw-engineer`        | workspace-write | Primary production Python source reviewer; implementation specialist for code, bug fixes, refactors, and typed public APIs. |
| `qa-specialist`      | workspace-write | Testing specialist for regression proof, risk-proportional edge coverage, and independent verification.                     |
| `challenger`         | read-only       | Adversarial reviewer for plans, architecture, migrations, releases, and non-trivial diffs.                                  |
| `curator`            | workspace-write | Configuration-quality specialist for instruction hygiene, routing clarity, duplication, and drift.                          |
| `data-steward`       | workspace-write | ML data-pipeline integrity specialist for datasets, splits, labels, transforms, and leakage prevention.                     |
| `scientist`          | workspace-write | ML research specialist for paper analysis, hypotheses, ablations, and evaluation protocols.                                 |
| `squeezer`           | read-only       | Performance specialist for throughput, latency, memory, GPU utilization, and profiling evidence.                            |
| `doc-scribe`         | workspace-write | Documentation specialist for public API docs, docstrings, README content, and changelogs.                                   |
| `cicd-steward`       | workspace-write | CI/CD reliability specialist for GitHub Actions, release automation, and flaky-CI diagnosis.                                |
| `delegation-lead`    | workspace-write | Cost-aware orchestration specialist for decomposing work and consolidating specialist evidence.                             |
| `linting-expert`     | workspace-write | Static-analysis specialist for Ruff, mypy, pre-commit, and suppression hygiene.                                             |
| `oss-shepherd`       | read-only       | Open-source lifecycle specialist for issue triage, semantic versioning, and release readiness.                              |
| `web-explorer`       | read-only       | External-evidence specialist for official documentation, release notes, and version verification.                           |

## 🤖 Role-card contract

<details open>
<summary><strong>Required frontmatter and body schema</strong></summary>

Every `roles/<role_id>/ROLE.md` follows one fixed schema, and `runtime/calibration/run.py`'s `check_agents()` enforces it mechanically — role card missing any required field or section fails calibration.

**Frontmatter (6 required fields):**

| Field                    | Constraint                                                      |
| ------------------------ | --------------------------------------------------------------- |
| `role_id`                | Must match containing directory name.                           |
| `name`                   | Must be `codex-rig-<role_id>`.                                  |
| `model_reasoning_effort` | `medium` or `high` per role.                                    |
| `approval_policy`        | `on-request` for every role.                                    |
| `sandbox_mode`           | `read-only` or `workspace-write`.                               |
| `fallback_modes`         | `[shim, built-in-injected, inline]` for every role — see below. |

**Body (5 required `##` sections, mechanically enforced by `check_agents()`):**

1. **Trigger and skip boundaries** — when role fires, when it skips, and what it explicitly is not for. Keeps routing between roles unambiguous.
2. **Evidence ownership** — what role must read or establish before acting, and what it must record (rejected alternatives, tradeoffs, verified-vs-assumed state) as it works.
3. **Execution constraints** — house style, conventions, and hard "do not" rules role must respect, plus which other role owns adjacent work it must hand off instead of doing itself.
4. **Handover contract** — exact ordered content role must return to its parent or caller.
5. **Confidence contract** — Assess correctness of the delivered result and how it was produced and checked within scope, not likelihood of later task completion. 0–1 confidence score role must report for a named process or claim, with completion status separate, a ≥0.90 bar for completion claim evaluated on the relevant process or substantive judgment, and every material evidence gap named rather than omitted. A separate status-confidence score cannot satisfy a completion or handover gate. Every standalone card also states numerical deduction accounting: ASCII minus/two-decimal contributions for each gap or limitation, zero-impact limits, overlap counted once, exact `1.00 - score` shortfall, named score-setting constraints and residual uncertainty. These are evidence-backed judgment allocations, not calibrated probabilities.

> **Selection boundary (advisory only — not part of the enforced 5)**: `solution-architect` and `security-auditor` additionally carry an explicit-user-selection rule and read-only advisory boundary in their own `ROLE.md` prose. `check_agents()`'s enforced-sections tuple has exactly 5 entries and does not check for this section; other roles do not carry it.

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
