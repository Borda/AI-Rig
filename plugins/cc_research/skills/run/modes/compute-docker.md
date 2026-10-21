<!-- file: compute-docker.md — consumers: run/SKILL.md -->

# Docker Sandbox Phases (compute: docker mode)

These phases execute only when `sandbox_mode = "docker"`. When `sandbox_mode = "local"`, skip this entire file.

> Image limit: sandbox runs `python:3.11-slim` — stdlib only, no project dependencies, network per `SANDBOX_NETWORK` (default `none`). R2 checks 3–4 and the R4 baseline use the same image, so a metric needing project deps fails at R2; use local or Colab compute for it.

## Phase 2a — Sandbox validate

Skip entirely if `sandbox_mode = "local"`.

If Phase 2 returned non-empty `"scripts"`: run each in Docker sandbox with read-only project mount. Per script (`SANDBOX_NETWORK` resolves from the R1 sentinel — Phase 5 uses the identical pattern):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r RUN_ID < "${TMPDIR:-/tmp}/research-run-id-${CSID}" 2>/dev/null || RUN_ID=""
[ -n "$RUN_ID" ] || { echo "! BLOCKED — Phase 2a: research run-id sentinel missing"; exit 1; }
IFS= read -r _CFG_NET < "${TMPDIR:-/tmp}/research-run-sandbox-network-${CSID}" 2>/dev/null || _CFG_NET=""
IFS= read -r VERIFY_TIMEOUT_MS < "${TMPDIR:-/tmp}/research-run-verify-timeout-ms-${CSID}" 2>/dev/null || VERIFY_TIMEOUT_MS=""
[ -n "$VERIFY_TIMEOUT_MS" ] || { echo "! BLOCKED — Phase 2a: R2 verify-timeout sentinel missing"; exit 1; }
echo "VERIFY_TIMEOUT_MS=$VERIFY_TIMEOUT_MS"
SANDBOX_NETWORK="${SANDBOX_NETWORK:-${_CFG_NET:-none}}" python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/docker_sandbox_run.py" --mode explore ".experiments/state/${RUN_ID}/scripts/${script}"
```

Use Bash tool `timeout`: `timeout: $VERIFY_TIMEOUT_MS` (R2 computes it from `$VERIFY_TIMEOUT_SEC`, prints it, and persists it to the sentinel the block above reloads; take the number from R2's printed line or the previous script's reload, since the Bash tool needs it before the call). Not shell `timeout` command.

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
