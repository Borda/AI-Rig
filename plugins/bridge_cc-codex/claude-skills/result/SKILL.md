---
name: result
description: Read a completed detached Codex bridge job's compact result.
argument-hint: JOB_ID
allowed-tools: Bash
---

# Read Bridge Job Result

Require one job identifier. Run `python "${CLAUDE_PLUGIN_ROOT}/bin/bridge_call.py" result --job-id "<job-id>"`; pass `--workspace` only when explicitly supplied. Return compact JSON envelope. Preserve workspace-relative `transcript_path` and `incident` references; never inline the bounded transcript or verbose peer `details`.

If `status=blocked`, open the JSON file referenced by `incident` and inspect its `fault` member. For `output-limit`, report incomplete; inspect the bounded transcript and, for write-capable work, the delta, changed files, and checks, then request verified remaining work as a fresh bounded task. Never replay the original task automatically.
