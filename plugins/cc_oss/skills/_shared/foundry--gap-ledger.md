# Gap ledger

<!-- file: gap-ledger.md — consumers: bin/gap_ledger.py (schema owner, byte-identical copies in cc_oss bin/ and codex-rig shared/) and bin/gap_rollup.py (foundry only) implement this contract; review, resolve and remediate gap emitters load their own plugin's copy (cc_oss: foundry--gap-ledger.md). Canonical here; propagate_shared.py keeps the copy byte-identical. -->

Every review, resolve or remediate run records its coverage gaps and limitations in `gaps.jsonl` in its durable directory (Codex run directory, Claude review or resolve report directory). Writers use `gap-ledger` version 1, one JSON object per gap. The ledger counts gaps across runs by repository, pull request or head, and ID.

## Record

Run `gap_ledger.py schema` for the exact key lists and closed value sets.

- `id`: stable kebab-case gap identity. Use the canonical table for known gaps. For uncatalogued gaps, reuse an established ID when available. Codex free text falls back to a word stem plus the existing digest.
- `class`, with its usual `owner`:

| `class` | Meaning | Usual `owner` |
| -- | -- | -- |
| `process` | the workflow lost, mislabelled or never produced its own evidence | `workflow` |
| `environment-provisionable` | a package still missing after provisioning failed, or a hook cache or index problem | `workflow` |
| `hardware-absent` | a hardware class this host lacks; needs `hardware_class` and the exact `reason` | `host` |
| `project` | a repository property: base-branch CI, CLA, hosted checks | `maintainer` |
| `contribution` | a property of the contribution: fork code policy | `contributor` |
| `human-decision` | the user's explicit choice: a skipped push, a gate cut on request | `user` |

- `summary`, `closable_by` (the action or condition that closes it), `evidence` (path relative to the run or repository).
- `subject`: `repo` (`owner/name`, compared case-insensitively; emitters store it case-folded), `pr` (number or null), `head` (commit), optional `ref`.
- `run`: `run_id`, `kind` (`review`, `resolve`, `remediate`), `harness` (`claude`, `codex`), `plugin`, `plugin_version` (the installed version that ran, not the repository's), `route`.
- `route`: `direct` (independent reviewers dispatched from the frozen schedule, including Claude agent spawns), `paged` (historical paged or batched dispatch), `serial-fallback` (parent-only review), `parent-substitute` (a direct or paged run where the parent covered at least one required role), `none` (no reviewer dispatched).
- `deduction`: the gap's `(-0.NN)` share of the confidence shortfall, or null.
- `host_class`: required for hardware gaps, with `os` and `architecture` from the observing host.
- Optional `status: closed` retracts an earlier record of the same `id` in the same run; readers take the last record per `id`.

Never record as a gap: the host operating system or a single Python version (facts, not gaps); a missing Python package before trying to provision it in an ephemeral environment outside the reviewed repository; a closure note (gap fixed, check rerun clean).

A stated device absence is recorded as `hardware-absent` with its deduction, including when the line also describes a missing driver or package. A device test that did not run or a device seen as unavailable by a particular package or build does not establish device absence. With no stated device absence, package or build gaps follow the provisioning rules.

## Canonical gap IDs

Use `gap_ledger.py lookup <id>` for the canonical class and owner. Normalize qualifiers to kebab case. Keep the original gate or confidence detail in the summary and evidence.

| ID | Class | Owner | Evidence and Codex counterpart |
| -- | -- | -- | -- |
| `<class>-absent` | `hardware-absent` | `host` | An observed absence from the closed hardware set in `schema`, such as a host without CUDA hardware. |
| `provisioning-failed-<package>` | `environment-provisionable` | `workflow` | A failed package provisioning attempt or a package still missing on the provisioned rerun. Use the distribution, for example `opencv-python` for `cv2`. Codex maps known imports and falls back to the module name. |
| `provisioning-not-attempted-<gate>` | `process` | `workflow` | A required-dependency gate ran and failed without package provisioning. The gap identifies the skipped workflow step by gate. |
| `pr-code-not-executed` | `process` | `workflow` | PR code was not executed. Includes Codex's former `gate-<gate>-sandbox-refused` and `gate-refusal-<source-cause>`. Preserve the cause in the summary. |
| `readiness-incomplete` | `process` | `workflow` | Verification readiness is incomplete or blocked, including the Codex review validation field. |
| `unreviewed-dimension-<unit>` | `process` | `workflow` | A required review dimension was omitted. Use the unit named in `Unreviewed dimension: <unit>`. |
| `full-suite-not-run` | `process` | `workflow` | The full suite was not run and required coverage is missing. A zero-weight scope note is excluded. |
| `push-not-landed` | `process` | `workflow` | A required or attempted push did not land. A user-declined push is excluded. |
| `codex-co-review-<reason>` | `process` | `workflow` | Explicitly absent Codex co-review. Use the recorded reason code, for example `Codex co-review absent: quota-exhausted`. |
| `gate-<gate>-hook-cache-unwritable` | `environment-provisionable` | `workflow` | A provisioned hook cache still cannot be written. |
| `gate-<gate>-network-unavailable` | `environment-provisionable` | `workflow` | A gate reports unavailable network access. |
| `gate-<gate>-loopback-bind-blocked` | `process` | `workflow` | A gate cannot bind a required loopback socket. |
| `gate-<gate>-timeout` | `process` | `workflow` | A gate exceeds its timeout. |
| `gate-<gate>-missing-command` | `process` | `workflow` | A required gate executable is missing. |
| `gate-<gate>-import-origin-unproven` | `process` | `workflow` | A gate cannot prove imports came from the reviewed checkout. |
| `implementation-challenge-not-run` | `process` | `workflow` | The bounded independent challenge of an implemented change did not run: approval denied, route unavailable or user declined. A deducted confidence gap, never a zero-deduction known limit, never a blocker. Codex lines start with the literal ID. |

For Codex, a rerun with `project_env` establishes that provisioning was attempted. Set `gate_env_provision.py apply --out` to a result path in the run directory. Apply retains `gate-provisioning.json` beside that result so the ledger can detect an apply failure before a rerun.

## Append

Write the records, one JSON object per line, to `<RUN_DIR>/gaps.jsonl.rec` with the Write tool (parallel writers: `gaps.jsonl.<writer>.rec`), then run the consumer's fixed block calling `python "$PLUGIN_ROOT/bin/gap_ledger.py" append "$RUN_DIR/gaps.jsonl"`, with `RUN_DIR` read back from the skill's sentinel. The helper validates the extended ledger, appends and deletes the staged files; on exit 1 nothing is appended and the staged file stays for correction. Never `append_ledger.py` for this ledger.

Codex promotion stages and appends through the same schema helper in-process.

## Carry forward

A rerun on the same repository, pull request and head, with the same run kind, plugin and plugin version, carries the earlier run's open gaps instead of re-deriving them: write `{"subject": …, "run": …}` for the new run to a file, then run `gap_ledger.py carry --from <prior>/gaps.jsonl --identity <file> "$RUN_DIR/gaps.jsonl"`. Each carried record keeps the earlier `id` and names the earlier run in `carried_from`. Hardware gaps carry only on the same recorded host class. A known limit carries only while `--known-limits` of the same repository still lists it; otherwise it is not carried and this run records it fresh with its own deduction. Exit 3 means the run identity differs. Derive this run's gaps fresh.

## Known limits

`.notes/known-limits.json` in the reviewed repository uses `known-limits` version 1. Each `limits` entry requires the canonical `id`, `class: hardware-absent`, `hardware_class`, exact `reason` and `host_class` with `os` and `architecture`. `gap_ledger.py limits <file>` validates the file.

The review emitter tries to register each newly observed absent hardware class after recording it. Use a direct physical-absence statement for the whole reason, such as “This host has no NVIDIA GPU.” An optional short host or test-scope label may precede it, as in “Test runner: no NVIDIA GPU.” The device must name the same class as `hardware_class`. A generic GPU does not identify a class, and a single GPU establishes only a `multi-gpu` limit. Use the same kind of statement in the summary or report a neutral coverage status such as “Accelerator tests skipped.” Observations about detection, software access, compatibility or provisioning keep their deductions, as do summaries that explain a package, build, driver or container cause. The first observation also retains its deduction. The emitter reports registration or a best-effort failure in the run output and continues. A registration lock left by a writer that died is removed once it is 10 minutes old. Later observations that meet the same evidence requirements on the same repository, OS and architecture use `known_limit: true` and no deduction.

Only a matching registry entry makes a record a known limit. The registry checks the same exact-reason requirement as registration and matching. Emitters report and ignore an invalid registry. `validate` and `append` refuse a `known_limit` claim they cannot verify against `--known-limits <file>` for the same repository. `carry` re-derives the flag from `--known-limits` instead of copying it.

## Rollup

`gap_rollup.py <roots…> [--known-limits <file> …] [--format markdown|json] [--output <sidecar>]` finds every `gaps.jsonl` under the roots and reports distinct gaps by repository, subject and ID, carried entries, known limits by repository and host class, and runs by harness, plugin version and route. Known limits come from `--known-limits` and from `.notes/known-limits.json` directly under each directory root. A record flagged `known_limit` that no given file lists counts as an ordinary gap and is named under unverified known-limit claims. Invalid ledgers are listed with their first error and excluded from counts. Incomplete runs include Codex stubs without a promoted result, Claude review directories without a final report, and Claude resolve directories with a stub or missing report.
