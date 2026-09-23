# Phase 5 — Verify metric — run/SKILL.md sidecar

Loaded by the main iteration loop (Phase 5 step) in `run/SKILL.md`.

#### Phase 5 — Verify metric

**If `sandbox_mode = "docker"`**:

```bash
SANDBOX_NETWORK="${SANDBOX_NETWORK}" python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/docker_sandbox_run.py" --mode verify "$METRIC_CMD"
```

Wrapper mounts project read-only, `.experiments` read-write, runs under `python:3.11-slim` with read-only rootfs, dropped Linux caps, `no-new-privileges` (network via `SANDBOX_NETWORK`, default `none`). No CPU/memory caps. Use Bash tool `timeout` parameter (not shell `timeout`): `timeout: $VERIFY_TIMEOUT_MS`.

**If `sandbox_mode = "local"`**: Run `metric_cmd` via Bash (`timeout: $VERIFY_TIMEOUT_MS`). Not shell `timeout`. Different CWD → separate `cd <path>` call first. Complex metric parsing → write parser to `.experiments/state/<run-id>/scripts/parse-metric-<i>.py`, run with `python <path>` — no inline one-liner.

**If `--colab` active**: routes through `mcp__colab-mcp__runtime_execute_code`; Docker not used (`--colab` + `--compute=docker` conflict caught at R2). `colab_hw` non-null → prepend GPU identity check via `mcp__colab-mcp__runtime_execute_code` — substitute configured hardware name (env var `COLAB_HW` overrides config) into assertion string before sending:

```python
import os, torch
expected_hw = os.environ.get("COLAB_HW", "")  # falls back to colab_hw from state.json injected at call site
actual = torch.cuda.get_device_name(0)
if expected_hw and expected_hw not in actual:
    raise AssertionError(f"Wrong GPU: expected {expected_hw!r}, got {actual!r}")
```

Assertion raises → print `"⚠ GPU mismatch: requested ${colab_hw} but runtime has {actual}. Change the Colab runtime type and re-run."` Stop — do not proceed to Phase 6. `colab_hw` null or `COLAB_HW` env var unset → check is no-op (environment-specific validation skipped).

<!-- Colab assertion: MCP call, not Bash — exempt from the script-file rule; correct as an inline one-liner. -->

Timeout expires → refresh sentinel (use REPO_SLUG and BRANCH_SLUG from `<constants>` — re-derive per canonical formula, then `touch "${TMPDIR:-/tmp}/claude-commit-auth-${REPO_SLUG}-${BRANCH_SLUG}"` <!-- tmpdir-exempt: user-shell-boundary -->), append `status: timeout`, then revert only if not already reverted this iteration: <!-- policy-sibling: plugins/cc_research/skills/run/SKILL.md §Phase 7 double-revert guard -->

```bash
# revert subject embeds the original ("Revert \"experiment(...)\"") — anchor at subject start, never substring
[ -n "$I" ] || { echo "! BLOCKED — Phase 5: iteration number unset, cannot scope revert guard"; exit 1; }
if git log -1 --format=%s 2>/dev/null | grep -qE "^experiment\(optimize/i${I}\):"; then
    git revert HEAD --no-edit  # timeout: 15000
else
    echo "Phase 5: HEAD is not iteration ${I}'s experiment commit — already reverted; skipping double-revert."
fi
```

Continue loop.
