# Phase 5 — Verify metric — run/SKILL.md sidecar

Loaded by the main iteration loop (Phase 5 step) in `run/SKILL.md`.

#### Phase 5 — Verify metric

Reload the verify timeout first (R2 shell vars do not survive); use the printed number as the Bash tool `timeout` below:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r VERIFY_TIMEOUT_MS < "${TMPDIR:-/tmp}/research-run-verify-timeout-ms-${CSID}" 2>/dev/null || VERIFY_TIMEOUT_MS=""
[ -n "$VERIFY_TIMEOUT_MS" ] || { echo "! BLOCKED — R2 verify-timeout sentinel missing"; exit 1; }
echo "VERIFY_TIMEOUT_MS=$VERIFY_TIMEOUT_MS"
```

**If `sandbox_mode = "docker"`**:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r _CFG_NET < "${TMPDIR:-/tmp}/research-run-sandbox-network-${CSID}" 2>/dev/null || _CFG_NET=""
SANDBOX_NETWORK="${SANDBOX_NETWORK:-${_CFG_NET:-none}}" python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/docker_sandbox_run.py" --mode verify "$METRIC_CMD"
```

Wrapper mounts project read-only, `.experiments` read-write, runs under `python:3.11-slim` with read-only rootfs, dropped Linux caps, `no-new-privileges` (network via `SANDBOX_NETWORK`, default `none`). In-container quotas `--cpus 2 --memory 2g --pids-limit 512` (override via `SANDBOX_CPUS` / `SANDBOX_MEMORY` / `SANDBOX_PIDS_LIMIT`; loosen or tighten, never disable); host-side kill after `SANDBOX_TIMEOUT_SEC` (default 600). Each call starts a fresh container. Use Bash tool `timeout` parameter (not shell `timeout`): `timeout: $VERIFY_TIMEOUT_MS`.

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

**Code identity check** (`--colab`, every Phase 5 call, before `metric_cmd`) — nothing syncs this repo to the runtime, so a stale runtime checkout would measure unrelated code. Fail closed; never sync by `git push` (always-ask). Get the local experiment commit:

```bash
git rev-parse HEAD  # timeout: 5000 — substitute into EXPECTED_SHA of the cell below
```

Send via `mcp__colab-mcp__runtime_execute_code`, from the runtime directory `metric_cmd` runs in:

```python
import subprocess

expected_sha = "<EXPECTED_SHA>"  # local `git rev-parse HEAD`, substituted at call site
if not expected_sha or "<" in expected_sha:
    raise AssertionError("EXPECTED_SHA not substituted")
actual_sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
if actual_sha != expected_sha:
    raise AssertionError(f"Stale runtime code: expected {expected_sha}, runtime has {actual_sha}")
```

Runtime copy without `.git` (e.g. Drive-mounted) → compare sha256 of every file in this iteration's `files_modified` instead: local `shasum -a 256 <files>` vs runtime `hashlib.sha256(open(p, "rb").read()).hexdigest()`. Assertion raises or digests differ → print `! BLOCKED — Colab runtime code does not match local commit <sha>; update the runtime checkout (pull or re-upload) and re-run.` Stop — do not proceed to Phase 6.

<!-- Colab assertions: MCP calls, not Bash — exempt from the script-file rule; correct as inline cells. -->

**`metric_repeats` > 1** (state.json `config`) — run the route above that many times, each run its own call under `timeout: $VERIFY_TIMEOUT_MS`; never one call looping over runs, which would squeeze every run into one timeout. With `--colab` the GPU and code-identity checks run once, before the first run. After each run Write one line `{"iteration":<I>,"metric":<float or null>,"exit":<code>}` to `.experiments/state/<run-id>/metric-runs.jsonl.rec` with the Write tool (`<I>` = the `iteration=` Phase 0 printed), then run:

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r RUN_ID < "${TMPDIR:-/tmp}/research-run-id-${CSID}" 2>/dev/null || RUN_ID=""
IFS= read -r I < "${TMPDIR:-/tmp}/research-run-iter-${CSID}" 2>/dev/null || I=""
[ -n "$RUN_ID" ] && [ -n "$I" ] || { echo "! BLOCKED — Phase 5: run-id or iteration sentinel missing; metric-runs.jsonl.rec left in place"; exit 1; }
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/append_ledger.py" ".experiments/state/$RUN_ID/metric-runs.jsonl"  # timeout: 5000
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/run_metric_stats.py" status ".experiments/state/$RUN_ID" --iteration "$I"  # timeout: 5000
```

| Marker | Next |
| -- | -- |
| `RUN_METRIC_NEXT_RUN=<k>` | run `metric_cmd` again |
| `RUN_METRIC_DONE=1 … mean=… sigma=…` | Phase 6; this iteration's metric is `mean` |
| `RUN_METRIC_FAILED=1` | no usable measurement: run the revert block below, `status: reverted`, decision `metric run <k> failed`; skip Phases 6–7 |

The ledger, never memory, is the cursor: after a compaction between runs, `status` names the next run.

Timeout expires → append `status: timeout`, then revert only if not already reverted this iteration (same script as SKILL.md §Phase 7 double-revert guard):

```bash
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r I < "${TMPDIR:-/tmp}/research-run-iter-${CSID}" 2>/dev/null || I=""  # written by run Phase 0
python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_research}/bin/revert_iteration_commit.py" --iteration "$I" --phase 5  # timeout: 15000 — reverts HEAD only if its subject starts with `experiment(optimize/i${I}):` (revert subjects embed the original, so never a substring match); unset $I → ! BLOCKED, exit 1
```

Continue loop.
