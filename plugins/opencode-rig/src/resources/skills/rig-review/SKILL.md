---
name: Rig Review
description: Review the current diff, a commit, or a path with independent read-only reviewers and a validated findings artifact requiring the mandatory correctness axis. Use before merge or after a large change. NOT for applying fixes; use rig-remediate.
---

# Rig Review

> Read [Evidence rules](../../references/evidence.md). This workflow is read-only with the sole PR preparation exception of one new dedicated local ref and the objects `git fetch` stores; it never writes to GitHub. Follow [Role dispatch](../../references/evidence.md#role-dispatch) for native-agent fallback and stop on reported scope violations; use the shared [Optional command compression](../../references/evidence.md#optional-command-compression) guidance for read-heavy commands.

## Input

- `workspace` (default): staged and unstaged changes.
- `path <path>`: one file or directory.
- `commit <rev>`: one commit's changed files.
- `pr <number|url>`: optional PR mode when `gh` is authenticated; resolves the PR head to commit mode. Earlier PR commits are a coverage limit unless separately reviewed.
- Optional: an implementation run ID and acceptance criteria for an implementation handoff.

## Workflow

1. **Classify.** `TRIVIAL`, `LOCAL`, `BROAD`, or `HIGH_RISK`, using the scope thresholds in [Rig Plan, Workflow step 1](../rig-plan/SKILL.md#workflow); read those definitions before classifying. Risk categories set depth; they do not grant execution authority.
2. **Start the run.** Prepare the scope before starting:
   - a. For an implementation handoff, first inspect its run with `rig_evidence` (`action: "inspect"`, `run_id: <implementation run ID>`), read its recorded `outputs` and bound plan, and take acceptance criteria from that plan or the supplied criteria. Use only those output paths within the plan's owned scope; exclude unrelated changed paths. If paths or criteria are missing or ambiguous, resolve the scope before starting instead of falling back to every changed path.
   - b. For PR mode, resolve the PR repository and head SHA through authenticated `gh`. Count its commits with `gh pr view <n> --json commits --repo <owner/repo>`. Use the PR repository's URL as `<pr-repo-url>` (including for fork PRs), its number as `<n>`, and the first 12 characters of the observed head SHA as `<sha12>`.
   - c. For PR mode, check `git show-ref --verify --quiet refs/rig-review/pr-<n>-<sha12>`; abort if it exists (exit 0), and proceed only on the absent-ref result (exit 1). Stop on any other error.
   - d. For PR preparation, fetch that head into the new dedicated local ref with `git fetch --no-tags --no-write-fetch-head --no-auto-maintenance <pr-repo-url> refs/pull/<n>/head:refs/rig-review/pr-<n>-<sha12>` (no `+`). Stop on failure. Verify with `git rev-parse refs/rig-review/pr-<n>-<sha12>` that the fetched SHA matches the observed PR head; stop on mismatch. Never switch branches or overwrite existing refs.
   - e. For PR mode, inspect `git rev-list --parents -n 1 <verified SHA>`. If the head has more than one parent, stop before starting a commit-mode run and record the limit "PR head is a merge commit; review each parent range separately" in the output; do not start a run with an empty scope. Stop if the parent check fails.
   - f. Start `rig_evidence` with `action: "start"`, `skill: "rig-review"`, the exact target paths, and `commit: <verified SHA>` for commit or PR mode; resolve a commit revision to its SHA first. Record the implementation run ID and acceptance criteria with `action: "record"` in `decision`, and the bound plan in `inputs` when supplied.
   - g. In PR mode, when the commit count is above 1, use `rig_evidence` with `action: "record"` and `limits: ["PR <n>: only head commit <sha> reviewed; <k> earlier commits unreviewed"]`, where `k = commit count - 1` and `<sha>` is the verified head SHA.
3. **Collect exact source.** Record the diff and the complete bytes of every reviewed file. For `workspace`, read the staged and unstaged diffs and current file bytes from the same project root used by `rig_evidence`. Do not use a detached worktree for workspace evidence: it omits uncommitted changes. For commit or PR mode, read the diff and file bytes from the verified commit, not the working tree. For an implementation handoff, compare each output's recorded `sha256` with the new scope entry for that exact path; if an entry is missing or mismatched, stop and report the changed output instead of treating it as the implementation's recorded bytes. A digest change invalidates the review.
4. **Select axes.** `correctness` is mandatory and must be run (cannot be omitted). Add `security` for auth, credentials, deserialization, or network input; `tests` when behavior changes; `api` for public interfaces; `performance`, `docs`, and `data` when the diff warrants. Cap at four axes below `BROAD`. Also ensure coverage records what was actually inspected for correctness.
5. **Dispatch independent reviewers.** Use `rig-reviewer` (or `rig_dispatch`). Give each only the task and acceptance criteria, role card, exact source bytes or paths that resolve to those bytes, the diff, their digests, and assigned axis. Reviewers are read-only and return JSON findings; the parent never counts its own reading as reviewer evidence.
6. **Validate findings.** Reject any finding without a concrete file and observed evidence. De-duplicate by signature (file + invariant), not by wording. Use `rig_findings` with `action: "write"`, the axes, the coverage, and `reviewer` naming the independent reviewer(s). That write records the review evidence bound to the scope digest.
7. **Report.** Order by severity. State the axes that ran, the axes that did not, and why. An honest partial review beats a fabricated complete one.
8. **Finalize.** `rig_evidence` with `action: "finalize"`. Open findings are a valid review outcome; they are not a completion failure. Source drift is: start a new review instead of certifying changed bytes.

## Improvement claim evidence

Assess every material improvement claimed in the request, PR title/body or reviewed documentation, including claims without supplied benchmarks. Record each claim, its scope and evidence in the existing review report. Include the relevant claims, supplied evidence and this measurement protocol in reviewer context; the parent reconciles claim dispositions in the final report even when a specialist is omitted. Route performance claims to the performance dimension when admitted by the existing selection rules; the parent retains claim coverage when that dimension is not selected. Preserve existing scope, fan-out limits and execution permissions; inspection-only reviewers hand feasible reproduction checks to the parent. Behavioral claims need targeted tests; correctness tests alone do not prove performance or model-quality gains.

Bind numerical comparisons to exact baseline/candidate revisions, commands, representative workload/data, software, relevant hardware and settings. Attribute supplied results separately from independent reproduction. Use a predefined practical tolerance, appropriate warmup and accelerator timing, repeated measurements, raw samples, sample counts, summary statistic and variability. Check correctness and material tradeoffs. Explain hardware relevance and limit conclusions to measured workloads/devices.

Shared local CPU/GPU, memory pressure or swapping, I/O contention and thermal/power changes can confound measurements. Record relevant observable contention and unavailable observations; never assume isolation. Run comparable baseline/candidate samples sequentially in paired, alternating or randomized order; avoid concurrent competing benchmarks or agent-owned heavy checks. Bound reruns and retain noisy attempts and exclusion criteria. More samples do not remove systematic bias. Never stop unrelated processes or change system resource controls without existing authorization. If noise or unequal contention could explain the effect, label measurements inconclusive and name the quieter or representative-hardware check needed; do not confirm or refute the claim from that run.

Conclude supported, contradicted or unverified per claim, with provenance, tested scope and whether independent reproduction succeeded. Missing hardware or unresolved noise leaves the claim unverified with prerequisite, owner and next check; feasible unperformed checks remain incomplete. Reliable counterevidence is required for contradiction. Missing verification alone is a coverage limit, not a source defect or grounds for rejection. Preserve each workflow's existing readiness and confidence contract.

## Rules

- Never write to the repository or GitHub during review. Sole exception: PR preparation may create one new dedicated local ref (and the objects `git fetch` stores); never switch branches, touch the index or working tree, or overwrite existing refs.
- Do not fix findings here; hand them to `rig-remediate`.
- A finding must cite source that was actually read; pattern-matched suspicion is labeled as suspicion.
- If an independent reviewer is unavailable, say so and cap confidence; do not substitute the parent's own analysis.

## Output

Report run ID, scope digest, axes run and skipped, findings by severity, coverage limits, and confidence.

Reviewer independence is instruction-enforced: the tools accept a reviewer identity string without authenticating its dispatch session. Report the actual independent reviewer and any unavailable review; never present that string as session attestation.
