---
name: Rig Distill
description: Mine recorded runs for recurring failure patterns and propose durable assets with counted evidence.
---

# Rig Distill

> Read [Evidence rules](../../references/evidence.md).

> This workflow proposes assets. It never installs, edits, or deletes one in the same run.

## Modes

| Mode | Evidence base | Use it for |
| -- | -- | -- |
| `runs` | This project's `.opencode-rig/runs/` artifacts (default) | Repeated failures, gate misses, and manual workarounds |
| `external` | A named plugin, repository, or skill inventory supplied read-only | Deciding whether an outside asset is worth adopting |

## Workflow

1. **Start the run.** `rig_evidence` with `action: "start"`, `skill: "rig-distill"`, and the artifact root or the external inventory path.
2. **Build the index.** Read every `run.json`; summarize counts by skill, status, and stop reason. Exclude the in-progress distill run itself. For `external`, inventory names and purposes first, without judging quality.
3. **Extract signatures.** Normalize each failure to a stable signature: gate id, finding dimension, loop stop reason, or missing-asset name. Never key on a line number, timestamp, or run id.
4. **Apply the evidence threshold.** A proposal needs at least three occurrences across two or more runs, or one security or critical finding with a systemic cause. Below the threshold, report the pattern as noise and stop.
5. **Classify the gap.** Skill gap, role gap, gate gap, rule gap, reference gap, or tooling gap. Check the existing `rig.json` roster and shipped cards first; a proposal that duplicates an existing asset is rejected with the name of the asset that already covers it.
6. **Cost the proposal.** State maintenance cost, blast radius, and the workflow that would own it. Prefer extending an existing asset over adding a new one.
7. **Falsify each proposal.** Ask whether the pattern predates the current plugin version, whether it is one project's quirk, and whether a gate or a doc line would fix it more cheaply. Keep only proposals that survive.
8. **Finalize.** Record proposals and rejections with `action: "record"`, then `action: "finalize"`.

## Rules

- Do not delete, prune, archive, or rewrite run artifacts. Deletion belongs to `rig_doctor` with explicit confirmation.
- Do not install an external asset, copy its files, or run its scripts. `external` mode is read-only.
- Do not count the same run twice or inflate a pattern by splitting one signature into variants.
- Do not propose a new skill for something a rule, a gate, or one reference paragraph already prevents.
- Redact secrets, tokens, personal paths, and proprietary content from every quoted artifact.

## Output

The evidence index, the patterns that met the threshold, proposals with counts and evidence run ids, rejected proposals with the reason, maintenance cost, and the handoff to `rig-plan` for each accepted proposal.
