<!-- file: provision-env.md — consumers: plugins/cc_oss/skills/review/SKILL.md (Step 2, after the snapshot, before the PR-code gate and agent spawn; loaded by plugins/cc_oss/skills/review/modes/agent-launch.md), plugins/cc_oss/skills/review/modes/claim-repro.md -->

## Reviewer environment — provision, never block

Foreground parent, once per review, after the Step 0 snapshot and before the PR-code gate plans its command: a missing Python package never stops a review and never becomes a bare "X not installed" gap. `review_env_provision.py` builds (or reuses) one ephemeral environment per repository + PR head under `${TMPDIR:-/tmp}/oss-review-env-<uid>/`, outside the reviewed tree and outside the project's own `.venv`. The root and its repository directories are owner-only; a root another user owns, can enter, or reaches through a link is refused (`env-create-failed`) before anything under it runs.

- Reads base + head manifests from git objects only (`pyproject.toml` runtime, extras, test/dev groups, root and `requirements/` files, test-imported optional packages such as `cv2` via one OpenCV wheel, `pycocotools`, `pandas`, `sklearn`, `pytest` + declared `pytest-*`). Never reads the worktree, never installs the project, never runs `setup.py`.
- Installs only *trusted* names: declared at base, plain version range at head, official index, wheels only — never a source build (`uv` with `--no-config`, else `venv` + `pip --isolated`; an sdist-only name fails with cause `unsupported-platform`). Everything else lands in `skipped_gated[]` with `cause: gated` and `gate_reason` (`added-by-pr`, `direct-reference`, `lookalike`, `unsafe-spec`, `index-override`, `index-routing-undetermined`, `user-index-setting`, `uv-environment-override`) and stays with the PR-code gate; a user index setting is never stripped to reach PyPI.
- Never blocks: install failures become `failed[]` with `cause` (`network`, `resolver-conflict`, `build-failure`, `unsupported-platform`, `direct-reference`, `timeout`, `no-installer`, `env-create-failed`, `manifest-unparsed`, `unknown`) and the installer's `error_tail`.
- `host` = observed facts: `os`, `machine` plus `native_arch` (Rosetta flagged), `cpu_model`, `cpu_count`, `memory_bytes`, `accelerators` (classes from `cuda`, `rocm`, `mps`, `tpu`) plus per-absent-class evidence in `absent_accelerators` (`cuda: nvidia-smi not on PATH`).

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
IFS= read -r SNAP_DIR < "${TMPDIR:-/tmp}/oss-review-snap-dir-${CSID}" 2>/dev/null || SNAP_DIR=""
IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/oss-review-run-dir-${CSID}" 2>/dev/null || RUN_DIR=""
[ -n "$SNAP_DIR" ] && [ -n "$RUN_DIR" ] || { echo "⚠ review env not provisioned: snapshot or run-dir sentinel missing"; exit 0; }
PR_HEAD_SHA=$(jq -er '.headRefOid' "$SNAP_DIR/pr-meta.json") || { echo "⚠ review env not provisioned: snapshot has no PR head"; exit 0; }
BASE_SHA=$(jq -er '.baseRefOid' "$SNAP_DIR/pr-meta.json") || BASE_SHA=""
git cat-file -e "$BASE_SHA^{commit}" 2>/dev/null || { echo "⚠ review env not provisioned: base commit not local, every name would be gated"; exit 0; }
python "$PLUGIN_ROOT/bin/review_env_provision.py" --repo . --base "$BASE_SHA" --head "$PR_HEAD_SHA" --probe-import pytest --timeout 500 --lock-wait 60 --budget 540 --out "$RUN_DIR/review-env.json" > /dev/null  # timeout: 600000
jq -r '"review env: \(.status) · python \(.env_python // "none") · installed \(.installed | length) · gated \(.skipped_gated | length) · failed \(.failed | length) · host \(.host.os) \(.host.native_arch) \(.host.cpu_model) · accelerators \(.host.accelerators | if length > 0 then join(",") else "none" end)"' "$RUN_DIR/review-env.json" 2>/dev/null || echo "⚠ review env not provisioned: provisioner wrote no document"
```

> Cleanup: each run first deletes environments under `oss-review-env-<uid>/` unused for 7 days (only directories carrying the tool's marker, never the current one, never one whose lock is held, never through a link); `pruned` reports the count and bytes freed.

> `failed[]` with cause `timeout` on a large environment: run the block once more — the same head and plan reuse the environment, downloaded wheels are cached under the env root, and an incomplete run installs again rather than starting over.

**Using it.** `env_python` (status `ready` or `partial`) is the interpreter for every run that loads PR code: targeted tests, probes, overlay runs, benchmarks. The parent plans the PR-code command with it (`<env_python> -m pytest -p no:xdist …`), so the gate binds that exact argv, and launches the guard with the document's `gate_environment`: its `VIRTUAL_ENV`, and its `PATH_PREPEND` (environment script directory plus `python.base_prefix`) first on `PATH`: the guard's sandbox makes only interpreter prefixes, `VIRTUAL_ENV` and `PATH` entries readable. The helper emits these in this host's path form and separator (`Scripts` and `;` on native Windows); never assemble them by hand. The project itself reaches a run only through the guard's copy of the tree (`PYTHONPATH` from `gate_environment`, the copy-relative `src` and `.`, or the harness), never as an installed package.

**Gaps it closes and gaps it names.**

- A name in `installed[]` → no package gap; never report it missing.
- A name in `failed[]` → gap `<name>: <cause>` with the first line of `error_tail`; the missing-package overlay procedure is the fallback only for that name.
- A name in `skipped_gated[]` → contribution finding `<name>: gated (<gate_reason>)`; never fetched by any reviewer.
- `host` (OS, architecture, accelerators) → recorded once per run as a fact, never a gap and never a confidence deduction. Only a claim or code path whose required OS or accelerator class differs from this run's host is reported, as `not reproducible on this host: requires <CLASS>; …` naming exactly that class and its probe evidence (`absent_accelerators`). On a host of the required class nothing about the host is reported. No emulation.
