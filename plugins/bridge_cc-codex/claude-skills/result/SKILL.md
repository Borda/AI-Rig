---
name: result
description: Read a completed detached Codex bridge job's compact result.
argument-hint: '[--workspace PATH] JOB_ID'
allowed-tools: Bash
effort: low
---

<!-- Diagram: ../../assets/images/bridge-request-arch.svg (request lifecycle; documentation only, SKILL.md wins) -->

# Read Bridge Job Result

<objective>
Read a detached Codex bridge job's compact result and report its verified completion limits.
</objective>

<workflow>

Require one job identifier. Run `python "${CLAUDE_PLUGIN_ROOT}/bin/bridge_call.py" result --job-id "<job-id>"`; always pass the same `--workspace` value the originating detached call used; a different or omitted value can resolve to another default workspace and report `missing` for a job still running elsewhere. Return compact JSON envelope. Preserve workspace-relative `transcript_path` and `incident` references; never inline the bounded transcript or peer `details`.

<!-- policy-sibling: plugins/bridge_cc-codex/claude-skills/advise/SKILL.md, plugins/bridge_cc-codex/claude-skills/review/SKILL.md, plugins/bridge_cc-codex/claude-skills/implement/SKILL.md (failure handling; preserve verb-specific recovery) -->

If the Bash call fails before producing JSON (missing interpreter, non-zero exit, malformed stdout), stop and report the raw failure; never fabricate a status. If `status=blocked`, open the JSON file referenced by `incident` and inspect its `fault`; `incident` may be `null` — then report blocked with no incident detail. For `output-limit`, report incomplete; report `partial_output` only labelled as an unverified fragment and use it to scope the split follow-up, never as findings or a verdict; the follow-up brief must keep commands small with no inline scripts or data. Inspect bounded transcript and, for write-capable work, delta, changed files, and checks, then request verified remaining work as a fresh bounded task. If `status=timeout`, report the returned `effort` and the incomplete work, then request a fresh call, optionally narrower in scope or with a larger `--timeout-seconds` — never a higher effort against the same budget. If `status=refused`, report its `fault` (for example `recursion-depth`); never retry. A failed `github-read` preflight is also refused: report it; the caller fetches the data and requests a fresh call through the originating verb (`/bridge:implement`) with `--network none`; never dispatch from here. Never replay the original task automatically.

Lifecycle states without an envelope: `queued`, `running` or `cancel_requested` — not finished; check again once when there is a reason to, subject to the Windows deadline below. `failed` — the supervisor errored before writing a result; report its `error`. `stalled` — the supervisor died or never started; report no result. For `failed` or `stalled` `implement` jobs, inspect the worktree delta and changed files: the child may have edited files before it stopped. `missing` — mismatched `--workspace` or unknown job. A `status=error` object (exit 2) is a refused CLI call, such as a malformed job id: report its `error`, never retry unchanged. When the originating call omitted `--workspace`, pass the absolute directory it ran from, never `.`.

On Windows, for `running` without an envelope, the caller must retain the launch budget and dispatch time (`--timeout-seconds`, default 600 seconds for `implement` when the option was omitted). Use the same deadline as `/bridge:status`: `--timeout-seconds` × 1.2 + 9 seconds termination/drain + 120 seconds startup grace, measured from dispatch. At that deadline, stop waiting and report incomplete work if no result exists; do not infer that the child has stopped or release ownership of paths an implement job may still be editing.

</workflow>
