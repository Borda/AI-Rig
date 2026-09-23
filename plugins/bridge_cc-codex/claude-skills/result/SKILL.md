---
name: result
description: Read a completed detached Codex bridge job's compact result.
argument-hint: JOB_ID
allowed-tools: Bash
---

# Read Bridge Job Result

Require one job identifier. Run `python "${CLAUDE_PLUGIN_ROOT}/bin/bridge_call.py" result --job-id "<job-id>"`; always pass the same `--workspace` value the originating detached call used; a different or omitted value can resolve to another default workspace and report `missing` for a job still running elsewhere. Return compact JSON envelope. Preserve workspace-relative `transcript_path` and `incident` references; never inline the bounded transcript or peer `details`.

If the Bash call fails before producing JSON (missing interpreter, non-zero exit, malformed stdout), stop and report the raw failure; never fabricate a status. If `status=blocked`, open the JSON file referenced by `incident` and inspect its `fault`; `incident` may be `null` — then report blocked with no incident detail. For `output-limit`, report incomplete; inspect bounded transcript and, for write-capable work, delta, changed files, and checks, then request verified remaining work as a fresh bounded task. If `status=timeout`, report the returned `effort` and the incomplete work, then request a fresh call, optionally narrower in scope or with a larger `--timeout-seconds` — never a higher effort against the same budget. If `status=refused`, report its `fault` (for example `recursion-depth`); never retry. Never replay the original task automatically.
