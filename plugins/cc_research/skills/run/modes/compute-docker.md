<!-- file: compute-docker.md — consumers: run/SKILL.md -->

# Docker Sandbox Phases (compute: docker mode)

These phases execute only when `sandbox_mode = "docker"`. When `sandbox_mode = "local"`, skip this entire file.

## Phase 2a — Sandbox validate

Skip entirely if `sandbox_mode = "local"`.

If Phase 2 returned non-empty `"scripts"`: run each in Docker sandbox with read-only project mount. Per script (use `${SANDBOX_NETWORK}` initialized at R2 — Phase 5 uses identical pattern):

```bash
SANDBOX_NETWORK="${SANDBOX_NETWORK}" python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/docker_sandbox_run.py" --mode explore ".experiments/state/${RUN_ID}/scripts/${script}"
```

Use Bash tool `timeout`: `timeout: $VERIFY_TIMEOUT_MS` (computed in R2 from `$VERIFY_TIMEOUT_SEC`). Not shell `timeout` command.

If any script exits non-zero: append `status: sandbox-failed` to `ideation-<i>.md`, skip to Phase 8 with `status: sandbox-failed`. Do not proceed to 2b.

If `"scripts"` empty or absent: 2a no-op — proceed to 2b.

## Phase 2b — Apply change

Skip if `sandbox_mode = "local"` (Phase 2 already applied changes).

Spawn same specialist agent (R3), `maxTurns: 10`:

```text
Read proposed change in `.experiments/state/<run-id>/ideation-<i>.md`.
Apply proposed change to source files.
Use Write and Edit tools ONLY — no Bash execution on codebase files.
Scope files (read and modify only these): <scope_files>
Return ONLY: {"files_modified":[...]}
```
