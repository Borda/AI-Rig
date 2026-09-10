# 🎭 Codex Rig Roles

Each subdirectory under `roles/` holds one `ROLE.md` — role card that packages Codex specialist: its model tier, sandbox and approval posture, and trigger/evidence/execution/handover/confidence contract that keeps it inside its lane. This README explains two things maintainer needs before touching role card: three-tier model-routing schema that decides which model role runs on, and schema every `ROLE.md` must satisfy to pass calibration harness.

<details open>
<summary><strong>Navigation</strong></summary>

## 📋 Contents

- [Three-tier model-routing schema](#-three-tier-model-routing-schema)
- [Role roster](#-role-roster)
- [Role-card contract](#-role-card-contract)
- [Fallback modes](#-fallback-modes)

</details>

> Value at a glance: fifteen versioned role cards make specialist behavior portable and auditable while keeping model selection, sandbox posture, and fallback behavior explicit.

> Current limits at a glance: role cards are behavioral profiles, not proof of native persistent-agent selection; Sol roles are read-only advisory paths, and new shim installation remains platform-blocked.

## 🎚️ Three-tier model-routing schema

Every role runs on one of three `gpt-5.6-<tier>` models. All fifteen roles share same `model_reasoning_effort: high`, `approval_policy: on-request`, and `fallback_modes: [shim, built-in-injected, inline]` — only `model` and `sandbox_mode` vary per role.

| Tier      | Model           | Purpose                                                                                                                                                                              | Roles                                                                                             |
| --------- | --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------- |
| **Sol**   | `gpt-5.6-sol`   | Deepest reasoning — architecture and security decisions that are expensive to get wrong and cheap to slow down.                                                                      | `solution-architect` (read-only), `security-auditor` (read-only)                                  |
| **Terra** | `gpt-5.6-terra` | Core build-and-verify work, plus roles that own final parent-facing decision — implementation, executable acceptance, adversarial review, data integrity, research, and performance. | `sw-engineer`, `qa-specialist`, `challenger`, `curator`, `data-steward`, `scientist`, `squeezer`  |
| **Luna**  | `gpt-5.6-luna`  | Cost-aware coordination and execution — documentation, CI/CD, static analysis, web evidence, OSS triage, and delegation coordination itself.                                         | `doc-scribe`, `cicd-steward`, `delegation-lead`, `linting-expert`, `oss-shepherd`, `web-explorer` |

### Rationale

<details>
<summary><strong>Evidence behind tier and sandbox assignments</strong></summary>

The tier assignment is not preference guess — it is recorded, evidence-derived routing state in `runtime/calibration/accepted-route-evidence.json`. That file's `active_assignments` block lists same three tier-to-role mappings above, and its `adjudication` block explains why:

- `delegation-lead` runs on Luna by explicit human override: rationale is "a cost-aware delegation leader that uses Luna for coordination while routing implementation and executable acceptance to Terra and architecture/security to Sol; the strict Luna route failure remains preserved."
- `cicd-steward`, `doc-scribe`, `linting-expert`, `oss-shepherd`, and `web-explorer` run on Luna by second override, for same reason: documentation, CI/CD stewardship, web evidence, OSS triage, and static analysis stay on Luna, while architecture and security stay on Sol and general implementation plus final parent decisions stay on Terra.
- `adjudication.luna_strict_failure_preserved: true` means strict-route calibration result — Luna failed strict quality bar on its own (`luna-score.json` records `strict_status: "fail"`) — is kept on record rather than silently overwritten by human override. The override changes assignment; it does not erase evidence that produced different strict answer.
- The adjudication rule itself only accepts evidence-derived candidate "with zero pair quality regressions and either mean F1 gain >= 0.01 or geometric-mean normalized cost ratio \<= 1.0" — human overrides above are recorded as explicit exceptions to that rule, not replacements for it.

`sandbox_mode` is set per role, independent of tier: `read-only` for six analysis or advisory roles — `challenger`, `security-auditor`, `solution-architect`, `squeezer`, `oss-shepherd`, `web-explorer` — and `workspace-write` for remaining nine, which are expected to produce or modify artifacts as part of their job.

### Task-difficulty selection

Role selection uses canonical [model-difficulty policy](../shared/specialist-orchestration.md#delegation-lead-and-model-routing), not model preference. Luna is limited to bounded support; Terra owns behavior and executable verification; Sol is reserved for architecture and security. The routing record must cite current task boundary or observed lower-tier insufficiency to escalate, and evidenced scope split to de-escalate; cost alone is insufficient.

</details>

## 🤖 Role roster

| Role                 | Tier  | Sandbox mode    | Purpose                                                                                                 |
| -------------------- | ----- | --------------- | ------------------------------------------------------------------------------------------------------- |
| `solution-architect` | Sol   | read-only       | System-design specialist for architecture, public API contracts, migrations, and module boundaries.     |
| `security-auditor`   | Sol   | read-only       | Security specialist for Python/web trust boundaries, ML supply chains, secrets, and CI/CD permissions.  |
| `sw-engineer`        | Terra | workspace-write | Implementation specialist for production code, bug fixes, refactors, and typed public API changes.      |
| `qa-specialist`      | Terra | workspace-write | Testing specialist for regression proof, risk-proportional edge coverage, and independent verification. |
| `challenger`         | Terra | read-only       | Adversarial reviewer for plans, architecture, migrations, releases, and non-trivial diffs.              |
| `curator`            | Terra | workspace-write | Configuration-quality specialist for instruction hygiene, routing clarity, duplication, and drift.      |
| `data-steward`       | Terra | workspace-write | ML data-pipeline integrity specialist for datasets, splits, labels, transforms, and leakage prevention. |
| `scientist`          | Terra | workspace-write | ML research specialist for paper analysis, hypotheses, ablations, and evaluation protocols.             |
| `squeezer`           | Terra | read-only       | Performance specialist for throughput, latency, memory, GPU utilization, and profiling evidence.        |
| `doc-scribe`         | Luna  | workspace-write | Documentation specialist for public API docs, docstrings, README content, and changelogs.               |
| `cicd-steward`       | Luna  | workspace-write | CI/CD reliability specialist for GitHub Actions, release automation, and flaky-CI diagnosis.            |
| `delegation-lead`    | Luna  | workspace-write | Cost-aware orchestration specialist for decomposing work and consolidating specialist evidence.         |
| `linting-expert`     | Luna  | workspace-write | Static-analysis specialist for Ruff, mypy, pre-commit, and suppression hygiene.                         |
| `oss-shepherd`       | Luna  | read-only       | Open-source lifecycle specialist for issue triage, semantic versioning, and release readiness.          |
| `web-explorer`       | Luna  | read-only       | External-evidence specialist for official documentation, release notes, and version verification.       |

## 🤖 Role-card contract

<details open>
<summary><strong>Required frontmatter and body schema</strong></summary>

Every `roles/<role_id>/ROLE.md` follows one fixed schema, and `runtime/calibration/run.py`'s `check_agents()` enforces it mechanically — role card missing any required field or section fails calibration.

**Frontmatter (7 required fields):**

| Field                    | Constraint                                                                    |
| ------------------------ | ----------------------------------------------------------------------------- |
| `role_id`                | Must match containing directory name.                                         |
| `name`                   | Must be `codex-rig-<role_id>`.                                                |
| `model`                  | One of `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna` — see tier table above. |
| `model_reasoning_effort` | `high` for every role.                                                        |
| `approval_policy`        | `on-request` for every role.                                                  |
| `sandbox_mode`           | `read-only` or `workspace-write`.                                             |
| `fallback_modes`         | `[shim, built-in-injected, inline]` for every role — see below.               |

**Body (5 required `##` sections for most roles, or 6 for Sol roles because selection boundary is explicit):**

1. **Trigger and skip boundaries** — when role fires, when it skips, and what it explicitly is not for. Keeps routing between roles unambiguous.
2. **Selection boundary (Sol roles only)** — explicit-user-selection rule and read-only advisory boundary for `solution-architect` and `security-auditor`.
3. **Evidence ownership** — what role must read or establish before acting, and what it must record (rejected alternatives, tradeoffs, verified-vs-assumed state) as it works.
4. **Execution constraints** — house style, conventions, and hard "do not" rules role must respect, plus which other role owns adjacent work it must hand off instead of doing itself.
5. **Handover contract** — exact ordered content role must return to its parent or caller.
6. **Confidence contract** — 0–1 confidence score role must report, ≥0.90 bar for completion claim, and instruction to name every material evidence gap rather than omit it.

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
