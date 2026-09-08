"""Behavioral tests for the provider-neutral benchmark batch entrypoint."""

from __future__ import annotations

import hashlib
import errno
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


# Every test here drives ``run-all.sh`` through ``/bin/bash`` with executable
# shell stubs, so the whole module is POSIX-only — same boundary the shared
# benchmark fixtures draw in conftest.py.
_skip_windows_posix = pytest.mark.skipif(
    sys.platform == "win32",
    reason="benchmark harness exercises the POSIX launcher only",
)


BENCHMARKS_DIR = Path(__file__).resolve().parent.parent
SCRIPT = BENCHMARKS_DIR / "run-all.sh"
sys.path.insert(0, str(BENCHMARKS_DIR))

from _bench_common.presentation import LEGEND_CLOSE_RULE, LEGEND_OPEN_RULE  # noqa: E402

REAL_GIT = shutil.which("git")
ACTIVE_MANIFEST = BENCHMARKS_DIR / "manifests" / "codex-integration.json"
ACTIVE_MANIFEST_SHA = hashlib.sha256(ACTIVE_MANIFEST.read_bytes()).hexdigest()
AGENTIC_MANIFEST = BENCHMARKS_DIR / "manifests" / "codex-agentic.json"
AGENTIC_MANIFEST_SHA = hashlib.sha256(AGENTIC_MANIFEST.read_bytes()).hexdigest()
AGENTIC_MANIFEST_DATA = json.loads(AGENTIC_MANIFEST.read_text(encoding="utf-8"))
AGENTIC_TOTAL_CELLS = AGENTIC_MANIFEST_DATA["preregistered_scope"]["total_cells"]
AGENTIC_CELL_TIMEOUT = AGENTIC_MANIFEST_DATA["preregistered_scope"]["coordinate_timeout_seconds"]
AGENTIC_SCOPE_SHA = "agentic-default-scope"
AGENTIC_REPEAT_TWO_SCOPE_SHA = "agentic-repeat-two-scope"
AGENTIC_SELECTED_SCOPE_SHA = "agentic-selected-scope"
#: A selected stratum is a study of its own, so it resolves to its own scope and its own token.
AGENTIC_STRATUM_SCOPE_SHA = "agentic-stratum-scope"
AGENTIC_LUNA_SCOPE_SHA = "agentic-luna-scope"
AGENTIC_SOL_SCOPE_SHA = "agentic-sol-scope"
AGENTIC_MANIFEST_MODEL = AGENTIC_MANIFEST_DATA["model"]["name"]
AGENTIC_SELECTED_TASK_IDS = ("BA-02", "BA-04")
AGENTIC_SELECTED_TOTAL_CELLS = len(AGENTIC_SELECTED_TASK_IDS) * 3
METHODOLOGY_MANIFEST = BENCHMARKS_DIR / "manifests" / "provider-parity-methodology.json"
METHODOLOGY_MANIFEST_DATA = json.loads(METHODOLOGY_MANIFEST.read_text(encoding="utf-8"))
SHARED_STRUCTURAL_TASK_IDS = METHODOLOGY_MANIFEST_DATA["preregistered_cells"]["structural_execution_task_ids"]
CLAUDE_AGENTIC_TOTAL_CELLS = METHODOLOGY_MANIFEST_DATA["agentic_execution_contract"]["default_total_cells_by_provider"][
    "claude"
]
CLAUDE_AGENTIC_SCOPE_SHA = "claude-agentic-default-scope"
CLAUDE_AGENTIC_REPEAT_TWO_SCOPE_SHA = "claude-agentic-repeat-two-scope"
ACTIVE_MANIFEST_DATA = json.loads(ACTIVE_MANIFEST.read_text(encoding="utf-8"))
LOCKED_INDEX_SHA = ACTIVE_MANIFEST_DATA["index"]["raw_sha256"]
LOCKED_INDEX_SCAN_VERSION = ACTIVE_MANIFEST_DATA["index"]["scan_version"]
CONFIRMATORY_TASK_IDS = json.loads(ACTIVE_MANIFEST.read_text(encoding="utf-8"))["preregistered_cells"][
    "structural_execution_task_ids"
]
DEFAULT_SCOPE_SHA = "d" * 64
SELECTED_SCOPE_SHA = "e" * 64
#: Model-specific scopes must all participate in the ordered aggregate.
SECOND_STRATUM_SCOPE_SHA = "f" * 64
#: Combined admission binds both child scopes into the one token the unified plan prints.
DEFAULT_STRUCTURAL_SWEEP_SCOPE_SHA = hashlib.sha256(
    f"{DEFAULT_SCOPE_SHA} {SECOND_STRATUM_SCOPE_SHA}\ngpt-5.6-luna gpt-5.6-terra\n".encode()
).hexdigest()
DEFAULT_AGENTIC_SWEEP_SCOPE_SHA = hashlib.sha256(
    f"{AGENTIC_MANIFEST_SHA} {AGENTIC_STRATUM_SCOPE_SHA}\ngpt-5.6-luna gpt-5.6-terra\n".encode()
).hexdigest()
COMBINED_SCOPE_SHA = hashlib.sha256(
    f"{DEFAULT_STRUCTURAL_SWEEP_SCOPE_SHA}\n{DEFAULT_AGENTIC_SWEEP_SCOPE_SHA}\n".encode()
).hexdigest()
#: A one-model combined run prices that model's own execution scope in both halves, never the
#: parent's default: the agentic lane runs the selected stratum too, so its half is that scope.
COMBINED_SECOND_STRATUM_SCOPE_SHA = hashlib.sha256(
    f"{SECOND_STRATUM_SCOPE_SHA}\n{AGENTIC_STRATUM_SCOPE_SHA}\n".encode()
).hexdigest()
#: Each lane binds every ordered model scope before combined approval hashes the two lanes.
MULTI_STRATUM_SCOPE_SHA = hashlib.sha256(
    f"{DEFAULT_SCOPE_SHA} {SECOND_STRATUM_SCOPE_SHA}\ngpt-5.6-sol gpt-5.6-terra\n".encode()
).hexdigest()
AGENTIC_MULTI_STRATUM_SCOPE_SHA = hashlib.sha256(
    f"{AGENTIC_SOL_SCOPE_SHA} {AGENTIC_STRATUM_SCOPE_SHA}\ngpt-5.6-sol gpt-5.6-terra\n".encode()
).hexdigest()
COMBINED_AGENTIC_MULTI_STRATUM_SCOPE_SHA = hashlib.sha256(
    f"{MULTI_STRATUM_SCOPE_SHA}\n{AGENTIC_MULTI_STRATUM_SCOPE_SHA}\n".encode()
).hexdigest()
AGENTIC_TRIPLE_STRATUM_SCOPE_SHA = hashlib.sha256(
    f"{AGENTIC_MANIFEST_SHA} {AGENTIC_STRATUM_SCOPE_SHA} {AGENTIC_SOL_SCOPE_SHA}\n"
    "gpt-5.6-luna gpt-5.6-terra gpt-5.6-sol\n".encode()
).hexdigest()
AGENTIC_SOL_TERRA_LUNA_SCOPE_SHA = hashlib.sha256(
    f"{AGENTIC_SOL_SCOPE_SHA} {AGENTIC_STRATUM_SCOPE_SHA} {AGENTIC_MANIFEST_SHA}\n"
    "gpt-5.6-sol gpt-5.6-terra gpt-5.6-luna\n".encode()
).hexdigest()
AGENTIC_SELECTED_REPEAT_TERRA_SCOPE_SHA = "agentic-selected-repeat-terra-scope"
AGENTIC_SELECTED_REPEAT_SOL_SCOPE_SHA = "agentic-selected-repeat-sol-scope"
AGENTIC_SELECTED_REPEAT_MULTI_SCOPE_SHA = hashlib.sha256(
    f"{AGENTIC_SELECTED_REPEAT_TERRA_SCOPE_SHA} {AGENTIC_SELECTED_REPEAT_SOL_SCOPE_SHA}\n"
    "gpt-5.6-terra gpt-5.6-sol\n".encode()
).hexdigest()
STRUCTURAL_SELECTED_MULTI_SCOPE_SHA = hashlib.sha256(
    f"{SELECTED_SCOPE_SHA} {SELECTED_SCOPE_SHA}\ngpt-5.6-sol gpt-5.6-terra\n".encode()
).hexdigest()
SELECTED_TASK_IDS = ("DI-01", "GR-01")


def _assert_safe_paid_preflight(calls: list[str], *, agentic: bool) -> None:
    """Assert manifest generation occurs before paid-input admission.

    Every launch rebuilds the five generated manifest records from source before
    paid-input admission. Agentic admission then resolves its immutable scope.
    Neither route may prepare the repository, access auth, or start a model
    runner.

    >>> calls = [
    ...     "build-provider-parity-methodology-manifest.py",
    ...     "build-codex-integration-manifest.py",
    ...     "build-codex-agentic-manifest.py",
    ... ]
    >>> _assert_safe_paid_preflight(calls, agentic=False)
    >>> with pytest.raises(AssertionError):
    ...     _assert_safe_paid_preflight(calls + ["prepare-codex-index.py"], agentic=False)
    """
    expected_checkers = [
        "build-provider-parity-methodology-manifest.py",
        "build-codex-integration-manifest.py",
        "build-codex-agentic-manifest.py",
    ]

    assert len(calls) == len(expected_checkers) + int(agentic)
    for call, checker in zip(calls[: len(expected_checkers)], expected_checkers, strict=True):
        assert checker in call
        assert not call.endswith(" --check")
    if agentic:
        assert "run-codex-agentic.py" in calls[-1]
        assert "--resolve-scope" in calls[-1]
    assert all("--auth-source" not in call for call in calls)
    assert all("prepare-codex-index.py" not in call for call in calls)
    assert all("run-codex-structural.py" not in call for call in calls)
    assert all("run-codex-agentic.py" not in call or "--resolve-scope" in call for call in calls)


def _claude_task_values(call: str) -> list[str]:
    """Decode the task-list argument between the recorded tasks and model options.

    >>> _claude_task_values('runner --tasks ["one", "two"] --model fixture')
    ['one', 'two']
    """
    start = call.index("--tasks ") + len("--tasks ")
    end = call.index(" --model ", start)
    return json.loads(call[start:end])


def _option_value(call: str, option: str) -> str:
    """Return the token following an option in a recorded whitespace-delimited call.

    This fixture parser does not interpret shell quoting or multi-token values.

    >>> _option_value("runner --repeat 2 --model fixture", "--repeat")
    '2'
    """
    tokens = call.split()
    return tokens[tokens.index(option) + 1]


def _write_executable(path: Path, body: str) -> None:
    """Write a Bash stub with a shebang and trailing newline, then mark it executable.

    >>> from tempfile import TemporaryDirectory
    >>> with TemporaryDirectory() as directory:
    ...     path = Path(directory) / "example"
    ...     _write_executable(path, "exit 0")
    ...     path.read_text(encoding="utf-8").splitlines()
    ['#!/usr/bin/env bash', 'exit 0']
    """
    path.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture(name="batch_env")
def _batch_env(tmp_path: Path) -> tuple[dict[str, str], Path]:
    """Write an isolated target and command stubs, returning an environment that routes execution to them.

    No commands run while this fixture is constructed.
    """
    repo = tmp_path / "target"
    (repo / ".git").mkdir(parents=True)
    index_path = repo / ".cache" / "codemap" / "target.json"
    index_path.parent.mkdir(parents=True)
    index_path.write_text(json.dumps({"scan_version": LOCKED_INDEX_SCAN_VERSION, "modules": []}), encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    call_log = tmp_path / "calls.log"
    auth_source = tmp_path / "auth.json"
    auth_source.write_text("fixture", encoding="utf-8")
    auth_source.chmod(0o600)

    _write_executable(
        bin_dir / "git",
        f'''if [ -n "${{FAIL_ON_GIT_FETCH:-}}" ] && [[ "$*" == *" fetch "* ]]; then exit 42; fi
if [[ "$*" == *"ls-files --stage -- ."* ]]; then exec "{REAL_GIT}" "$@"; fi
if [[ "$*" == *"worktree add"* ]]; then
  target="${{@: -2:1}}"
  mkdir -p "$target/.cache/codemap"
  printf "gitdir: fixture\\n" > "$target/.git"
  exit 0
fi
if [[ "$*" == *"worktree remove"* ]]; then
  rm -rf "${{@: -1}}"
  exit 0
fi
printf "fixture-head\\n"''',
    )
    _write_executable(
        bin_dir / "python3",
        f"""if [ "$1" = "-c" ]; then exec {sys.executable} "$@"; fi
# Phase headers render through a CLI; answer with the redirected form and keep them out of the call log.
if [[ "$*" == *"render_cli.py"* ]]; then printf "== %s ==\\n" "$3"; exit 0; fi
printf "python %s\\n" "$*" >> "$CALL_LOG"
if [[ "$*" == *"--resolve-tasks"* ]]; then
  if [[ "$*" == *"INVALID"* ]]; then
    printf "invalid task selector\\n" >&2
    exit 2
  fi
  if [[ "$*" == *"PT-01"* ]]; then
    printf '{{"task_ids":["PT-01"],"total_cells":3,"scope_sha256":"{SELECTED_SCOPE_SHA}"}}\\n'
    exit 0
  fi
  printf '{{"task_ids":["DI-01","GR-01"],"total_cells":6,"scope_sha256":"{SELECTED_SCOPE_SHA}"}}\\n'
  exit 0
fi
if [[ "$*" == *"run-codex-agentic.py"* && "$*" == *"--resolve-scope"* ]]; then
  if [ -n "${{DRIFT_AGENTIC_SCOPE_MODEL:-}}" ] && [[ "$*" == *"--model $DRIFT_AGENTIC_SCOPE_MODEL"* ]]; then
    drift_count=0
    if [ -f "$DRIFT_AGENTIC_SCOPE_COUNT" ]; then drift_count="$(<"$DRIFT_AGENTIC_SCOPE_COUNT")"; fi
    drift_count=$((drift_count + 1))
    printf '%s\n' "$drift_count" > "$DRIFT_AGENTIC_SCOPE_COUNT"
    if [ "$drift_count" -gt 3 ]; then
      printf '{{"task_ids":["BA-01"],"repetitions":1,"total_cells":3,"arms":["A_plain","B_auto","C_strict"],"models":["%s"],"coordinate_timeout_seconds":600,"scope_sha256":"agentic-drift-scope"}}\n' "$DRIFT_AGENTIC_SCOPE_MODEL"
      exit 0
    fi
  fi
  if [[ "$*" == *"--task-id BA-02,BA-04"* && "$*" == *"--repetitions 2"* && "$*" == *"--model gpt-5.6-terra"* ]]; then
    printf '{{"task_ids":["BA-02","BA-04"],"repetitions":2,"total_cells":12,"arms":["A_plain","B_auto","C_strict"],"models":["gpt-5.6-terra"],"coordinate_timeout_seconds":600,"scope_sha256":"{AGENTIC_SELECTED_REPEAT_TERRA_SCOPE_SHA}"}}\n'
  elif [[ "$*" == *"--task-id BA-02,BA-04"* && "$*" == *"--repetitions 2"* && "$*" == *"--model gpt-5.6-sol"* ]]; then
    printf '{{"task_ids":["BA-02","BA-04"],"repetitions":2,"total_cells":12,"arms":["A_plain","B_auto","C_strict"],"models":["gpt-5.6-sol"],"coordinate_timeout_seconds":600,"scope_sha256":"{AGENTIC_SELECTED_REPEAT_SOL_SCOPE_SHA}"}}\n'
  elif [[ "$*" == *"--task-id BA-02,BA-04"* ]]; then
    printf '{{"task_ids":["BA-02","BA-04"],"repetitions":1,"total_cells":{AGENTIC_SELECTED_TOTAL_CELLS},"arms":["A_plain","B_auto","C_strict"],"models":["gpt-5.6-luna"],"coordinate_timeout_seconds":600,"scope_sha256":"{AGENTIC_SELECTED_SCOPE_SHA}"}}\n'
  elif [[ "$*" == *"--model gpt-5.6-luna"* ]]; then
    printf '{{"task_ids":["BA-01","BA-02","BA-03","BA-04","BA-05","BA-06","BA-07","BA-08","BA-09","BA-10","BA-11","BA-12","BA-13","BA-14","BA-15","BA-16"],"repetitions":1,"total_cells":{AGENTIC_TOTAL_CELLS},"arms":["A_plain","B_auto","C_strict"],"models":["gpt-5.6-luna"],"coordinate_timeout_seconds":600,"scope_sha256":"{AGENTIC_LUNA_SCOPE_SHA}"}}\n'
  elif [[ "$*" == *"--model gpt-5.6-terra"* ]]; then
    printf '{{"task_ids":["BA-01","BA-02","BA-03","BA-04","BA-05","BA-06","BA-07","BA-08","BA-09","BA-10","BA-11","BA-12","BA-13","BA-14","BA-15","BA-16"],"repetitions":1,"total_cells":{AGENTIC_TOTAL_CELLS},"arms":["A_plain","B_auto","C_strict"],"models":["gpt-5.6-terra"],"coordinate_timeout_seconds":600,"scope_sha256":"{AGENTIC_STRATUM_SCOPE_SHA}"}}\n'
  elif [[ "$*" == *"--model gpt-5.6-sol"* ]]; then
    printf '{{"task_ids":["BA-01","BA-02","BA-03","BA-04","BA-05","BA-06","BA-07","BA-08","BA-09","BA-10","BA-11","BA-12","BA-13","BA-14","BA-15","BA-16"],"repetitions":1,"total_cells":{AGENTIC_TOTAL_CELLS},"arms":["A_plain","B_auto","C_strict"],"models":["gpt-5.6-sol"],"coordinate_timeout_seconds":600,"scope_sha256":"{AGENTIC_SOL_SCOPE_SHA}"}}\n'
  elif [[ "$*" == *"--repetitions 2"* ]]; then
    printf '{{"task_ids":["BA-01","BA-02","BA-03","BA-04","BA-05","BA-06","BA-07","BA-08","BA-09","BA-10","BA-11","BA-12","BA-13","BA-14","BA-15","BA-16"],"repetitions":2,"total_cells":96,"arms":["A_plain","B_auto","C_strict"],"models":["gpt-5.6-luna"],"coordinate_timeout_seconds":600,"scope_sha256":"{AGENTIC_REPEAT_TWO_SCOPE_SHA}"}}\n'
  else
    printf '{{"task_ids":["BA-01","BA-02","BA-03","BA-04","BA-05","BA-06","BA-07","BA-08","BA-09","BA-10","BA-11","BA-12","BA-13","BA-14","BA-15","BA-16"],"repetitions":1,"total_cells":{AGENTIC_TOTAL_CELLS},"arms":["A_plain","B_auto","C_strict"],"models":["gpt-5.6-luna"],"coordinate_timeout_seconds":600,"scope_sha256":"{AGENTIC_SCOPE_SHA}"}}\n'
  fi
  exit 0
fi
if [[ "$*" == *"run-claude-agentic.py"* && "$*" == *"--resolve-scope"* ]]; then
  if [[ "$*" == *"--repeat 2"* ]]; then
    printf '{{"task_ids":["BA-01","BA-02","BA-03","BA-04","BA-05","BA-06","BA-07","BA-08","BA-09","BA-10","BA-11","BA-12","BA-13","BA-14","BA-15","BA-16"],"repetitions":2,"total_cells":{CLAUDE_AGENTIC_TOTAL_CELLS * 2},"arms":["A_plain","B_auto","C_strict"],"models":["haiku","sonnet","opus"],"coordinate_timeout_seconds":600,"scope_sha256":"{CLAUDE_AGENTIC_REPEAT_TWO_SCOPE_SHA}"}}\n'
  else
    printf '{{"task_ids":["BA-01","BA-02","BA-03","BA-04","BA-05","BA-06","BA-07","BA-08","BA-09","BA-10","BA-11","BA-12","BA-13","BA-14","BA-15","BA-16"],"repetitions":1,"total_cells":{CLAUDE_AGENTIC_TOTAL_CELLS},"arms":["A_plain","B_auto","C_strict"],"models":["haiku","sonnet","opus"],"coordinate_timeout_seconds":600,"scope_sha256":"{CLAUDE_AGENTIC_SCOPE_SHA}"}}\n'
  fi
  exit 0
fi
if [[ "$*" == *"prepare-codex-index.py"* && "$*" == *"--relocate-into"* ]]; then
  source_index=""; worktree=""; provenance=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --index-path) source_index="$2"; shift 2;;
      --relocate-into) worktree="$2"; shift 2;;
      --provenance-path) provenance="$2"; shift 2;;
      *) shift;;
    esac
  done
  mkdir -p "$worktree/.cache/codemap"
  cp "$source_index" "$worktree/.cache/codemap/$(basename "$worktree").json"
  printf '{{"frozen_index_sha256":"{LOCKED_INDEX_SHA}"}}\n' > "$provenance"
  printf '{{"frozen_index_sha256":"{LOCKED_INDEX_SHA}"}}\n'
  exit 0
fi
if [[ "$*" == *"prepare-codex-index.py"* && "$*" == *"--print-contract"* ]]; then
  printf '{{"raw_sha256":"{LOCKED_INDEX_SHA}","scan_version":{LOCKED_INDEX_SCAN_VERSION}}}\\n'
  exit 0
fi
if [[ "$*" == *"prepare-codex-index.py"* && "$*" == *"--verify"* ]]; then
  if grep -q '"scan_version": {LOCKED_INDEX_SCAN_VERSION}' "$3" && grep -q '"modules": \\[\\]' "$3"; then
    printf "verified: %s\\n" "$3"
    exit 0
  fi
  exit 43
fi
if [[ "$*" == *"build-codex-integration-manifest.py"* ]]; then
  if [ -n "${{FAIL_MANIFEST_CHECK:-}}" ]; then
    printf "generated Codex manifest build failed\\n" >&2
    exit 47
  fi
fi
if [[ "$*" == *"build-provider-parity-methodology-manifest.py"* ]]; then
  if [ -n "${{FAIL_METHODOLOGY_CHECK:-}}" ]; then
    printf "generated methodology manifest build failed\n" >&2
    exit 46
  fi
fi
if [[ "$*" == *"build-codex-agentic-manifest.py"* ]]; then
if [ -n "${{FAIL_AGENTIC_MANIFEST_CHECK:-}}" ]; then
    printf "stale generated Codex agentic manifest\\n" >&2
    exit 48
  fi
fi
if [ -n "${{FAIL_AGENTIC_MODEL:-}}" ] && [[ "$*" == *"run-codex-agentic.py"* && "$*" == *"--auth-source"* && "$*" == *"--model $FAIL_AGENTIC_MODEL"* ]]; then
  exit 41
fi
if [ -n "${{FAIL_WHEN_ARGS_CONTAIN:-}}" ] && [[ "$*" == *"$FAIL_WHEN_ARGS_CONTAIN"* ]]; then
  exit 41
fi
if [[ "$*" == *"--render-results"* ]]; then
  if [ -n "${{FAIL_RENDER_RESULTS:-}}" ]; then
    exit 43
  fi
  exec {sys.executable} "$@"
fi
if [[ "$*" == *"run-codex-structural.py"* && "$*" != *"--no-legend"* ]]; then
  printf "{LEGEND_OPEN_RULE}\n  treatments: A_plain=no Codemap, B_auto=direct Codemap required, C_strict=Codemap Skill required\n{LEGEND_CLOSE_RULE}\n"
fi
if [[ "$*" == *"run-codex-structural.py"* && "$*" == *"--dry-run"* ]]; then
  if [ -n "${{DRIFT_STRUCTURAL_SCOPE_MODEL:-}}" ] && [[ "$*" == *"--model $DRIFT_STRUCTURAL_SCOPE_MODEL"* ]]; then
    drift_count=0
    if [ -f "$DRIFT_STRUCTURAL_SCOPE_COUNT" ]; then drift_count="$(<"$DRIFT_STRUCTURAL_SCOPE_COUNT")"; fi
    drift_count=$((drift_count + 1))
    printf '%s\n' "$drift_count" > "$DRIFT_STRUCTURAL_SCOPE_COUNT"
    if [ "$drift_count" -gt 1 ]; then
      printf "SCOPE   {"a" * 64}\\n"
      exit 0
    fi
  fi
  printf "PLAN    FN-02  rep=1  A_plain\\n"
  if [[ "$*" == *"--tasks DI,GR"* ]]; then
    printf "SCOPE   {SELECTED_SCOPE_SHA}\\n"
  elif [[ "$*" == *"--tasks PT-01"* ]]; then
    printf "SCOPE   {SELECTED_SCOPE_SHA}\\n"
  elif [[ "$*" == *"--model gpt-5.6-terra"* ]]; then
    printf "SCOPE   {SECOND_STRATUM_SCOPE_SHA}\\n"
  elif [[ "$*" != *"--tasks FN-02"* ]]; then
    printf "SCOPE   {DEFAULT_SCOPE_SHA}\\n"
  fi
fi
if [[ "$*" == *"run-codex-agentic.py"* && "$*" == *"--dry-run"* ]]; then
  printf "PROBE   A_plain    codemap=false skill-required=false\\n"
  printf "PLAN    BA-01  rep=1  A_plain\\n"
fi
if [[ "$*" == *"run-codex-agentic.py"* && "$*" == *"--auth-source"* ]]; then
  if [ -n "${{ASSERT_AGENTIC_ADMISSION_FRESH:-}}" ] && [ -e "$CODEX_RUN_DIR/.agentic-console.log" ]; then
    printf "agentic console artifact existed before paid Python admission\\n" >&2
    exit 49
  fi
  printf "{LEGEND_OPEN_RULE}\\n  treatments: A_plain=no Codemap, B_auto=CLI available and optional, C_strict=installed Codemap Skill with compact query required\\n  metrics:\\n      EREC: expected direct-importer recall\\n      RREC: final-report recall\\n      DEFF: expected dependencies exposed per tool call\\n  status: ✓ completed, ✗ failed\\n  progress: N completed cells / {AGENTIC_TOTAL_CELLS} planned cells\\n  treatment: ✓ assigned arm followed, ✗ assigned arm not followed\\n  codemap-used: ✓ Codemap call observed; ✗ no call observed (A_plain expects none)\\n  input tokens: gross total; cached and fresh details remain in telemetry only\\n{LEGEND_CLOSE_RULE}\\n"
  printf "agentic raw\\n" > "$CODEX_RUN_DIR/telemetry.jsonl"
  printf "agentic canonical\\n" > "$CODEX_RUN_DIR/telemetry-canonical.jsonl"
  printf "{{}}\\n" > "$CODEX_RUN_DIR/run-metadata.json"
  printf "agentic checksums\\n" > "$CODEX_RUN_DIR/checksums.sha256"
  printf "SUMMARY  status=completed  persisted_cells={AGENTIC_TOTAL_CELLS}/{AGENTIC_TOTAL_CELLS}\\n" > "$CODEX_RUN_DIR/run.log"
  printf "SUMMARY  status=completed  persisted_cells={AGENTIC_TOTAL_CELLS}/{AGENTIC_TOTAL_CELLS}\\n"
fi
if [ -n "${{FAIL_AGENTIC_DRY_MODEL:-}}" ] && [[ "$*" == *"run-codex-agentic.py"* && "$*" == *"--dry-run"* && "$*" == *"--model $FAIL_AGENTIC_DRY_MODEL"* ]]; then
  exit 41
fi
if [[ "$*" == *"--auth-source"* ]]; then
  if [[ "$*" != *"run-codex-agentic.py"* ]]; then
    structural_run_dir="$CODEX_RUN_DIR"
    expect_run_dir=false
    for arg in "$@"; do
      if [ "$expect_run_dir" = true ]; then
        structural_run_dir="$arg"
        break
      fi
      if [ "$arg" = "--run-dir" ]; then
        expect_run_dir=true
      fi
    done
    mkdir -p "$structural_run_dir"
    printf "raw\\n" > "$structural_run_dir/telemetry.jsonl"
    printf "canonical\\n" > "$structural_run_dir/telemetry-canonical.jsonl"
    printf "{{}}\\n" > "$structural_run_dir/run-metadata.json"
    printf "PLAN    FN-02  rep=1  A_plain\\n"
    printf "RESULT  completed  FN-02  rep=1  A_plain  in=1  out=1  time=1s  quality=1.0  compliance:✓\\n"
    printf "ARTIFACTS:\\n - telemetry=%s/telemetry.jsonl\\n - metadata=%s/run-metadata.json\\n" "$structural_run_dir" "$structural_run_dir"
  fi
fi""",
    )
    _write_executable(
        bin_dir / "python3.11",
        """if [[ "$*" == *"version_info"* ]]; then exit 0; fi
if [[ "$*" == *"realpath"* ]]; then printf "%s\\n" "$0"; exit 0; fi
printf "Python 3.11.0\\n""",
    )
    _write_executable(bin_dir / "codex", 'printf "codex-cli 0.146.1\\n"')
    for group in ("pytorch", "fabric"):
        for kind in ("base", "test"):
            requirement = repo / "requirements" / group / f"{kind}.txt"
            requirement.parent.mkdir(parents=True, exist_ok=True)
            requirement.write_text(f"# fixture {group} {kind}\n", encoding="utf-8")
    _write_executable(bin_dir / "pytest", 'printf "pytest fixture\\n"')
    _write_executable(
        bin_dir / "uv",
        """printf "uv %s\\n" "$*" >> "$CALL_LOG"
if [ "$1" = "venv" ]; then
  root="${!#}"
  mkdir -p "$root/bin"
  printf '#!/bin/sh\\nexit 0\\n' > "$root/bin/python"
  printf '#!/bin/sh\\nexit 0\\n' > "$root/bin/pytest"
  chmod +x "$root/bin/python" "$root/bin/pytest"
fi""",
    )
    _write_executable(
        bin_dir / "shasum",
        f"""if [ -z "${{3:-}}" ]; then
  exec /usr/bin/shasum -a 256
elif [[ "$3" == "$REPO/"* && "$(sed -n '1p' "$3")" == *'"scan_version": {LOCKED_INDEX_SCAN_VERSION}'* && "$(sed -n '1p' "$3")" == *'"modules": []'* ]]; then
  printf "{LOCKED_INDEX_SHA}  %s\\n" "$3"
elif [[ "$3" == *"/benchmarks/manifests/codex-integration.json" ]]; then
  printf "{ACTIVE_MANIFEST_SHA}  %s\\n" "$3"
elif [[ "$3" == *"/benchmarks/manifests/codex-agentic.json" ]]; then
  printf "{AGENTIC_MANIFEST_SHA}  %s\\n" "$3"
elif [[ "$3" == "$CODEX_RUN_DIR/"* ]]; then
  exec /usr/bin/shasum -a 256 "$3"
else
  printf "%064d  %s\\n" 0 "$3"
fi""",
    )

    env = {
        **os.environ,
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "CALL_LOG": str(call_log),
        "CODEX_AUTH_SOURCE": str(auth_source),
        "CODEX_RUN_DIR": str(tmp_path / "codex-run"),
        "CODEX_PAID_APPROVAL": DEFAULT_SCOPE_SHA,
        "CODEMAP_BIN": str(bin_dir / "codemap-py"),
        "REPO": str(repo),
        "CODEMAP_BENCH_PATCH_PYTEST": str(bin_dir / "pytest"),
        "CODEMAP_BENCH_PATCH_VENV": str(tmp_path / "patch-venv"),
    }
    _write_executable(
        bin_dir / "codemap-py",
        f'''root="${{!#}}"; mkdir -p "$root/.cache/codemap"; printf '{{"scan_version": {LOCKED_INDEX_SCAN_VERSION}, "modules": []}}' > "$root/.cache/codemap/$(basename "$root").json"''',
    )
    return env, call_log


def _run_batch(mode: str, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    """Run one batch mode against command stubs and capture its public output."""
    return subprocess.run(
        ["/bin/bash", str(SCRIPT), mode, *args],
        cwd=BENCHMARKS_DIR.parent,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _combined_scope(stdout: str) -> str:
    """Return the combined Codex scope hash printed by a dry run's authorization block."""
    for line in stdout.splitlines():
        if line.startswith("COMBINED SCOPE"):
            return line.split()[-1]
    raise AssertionError(f"no combined scope in output: {stdout}")


def _agentic_scope(stdout: str) -> str:
    """Return the agentic scope hash printed by a dry run's authorization block."""
    for line in stdout.splitlines():
        if line.startswith("AGENTIC SCOPE"):
            return line.split()[-1]
    raise AssertionError(f"no agentic scope in output: {stdout}")


def _run_batch_tty(mode: str, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    """Run one batch with stdout and stderr attached to a pseudo-terminal."""
    import pty  # POSIX-only (pty -> tty -> termios); absent on Windows, so import at call time

    master_fd, slave_fd = pty.openpty()
    command = ["/bin/bash", str(SCRIPT), mode, *args]
    process = subprocess.Popen(
        command,
        cwd=BENCHMARKS_DIR.parent,
        env=env,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        close_fds=True,
    )
    os.close(slave_fd)
    output: list[bytes] = []
    try:
        while True:
            try:
                chunk = os.read(master_fd, 4096)
            except OSError as error:
                if error.errno == errno.EIO:
                    break
                raise
            if not chunk:
                break
            output.append(chunk)
    finally:
        os.close(master_fd)
    return subprocess.CompletedProcess(command, process.wait(), b"".join(output).decode(), "")


@_skip_windows_posix
def test_batch_entrypoint_accepts_exactly_three_modes(batch_env: tuple[dict[str, str], Path]) -> None:
    """Reject missing, obsolete, or extra modes before any setup command runs."""
    env, call_log = batch_env

    missing = subprocess.run(
        ["/bin/bash", str(SCRIPT)],
        cwd=BENCHMARKS_DIR.parent,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert missing.returncode == 2
    assert "smoke | claude" in missing.stderr
    assert "| codex" in missing.stderr
    for obsolete in ("all", "full", "refresh", "unknown"):
        rejected = _run_batch(obsolete, env)
        assert rejected.returncode == 2
        assert "smoke | claude" in rejected.stderr
        assert "| codex" in rejected.stderr
    rejected = _run_batch("smoke", env, "--dry-run")
    assert rejected.returncode == 2
    rejected = _run_batch("claude", env, "--unknown")
    assert rejected.returncode == 2
    rejected = _run_batch("codex", env, "--unknown")
    assert rejected.returncode == 2
    assert not call_log.exists()


@_skip_windows_posix
def test_codex_default_dry_run_dispatches_structural_then_agentic_without_paid_inputs(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """The selector-free no-model command advertises both Codex study plans in order."""
    env, call_log = batch_env
    for name in (
        "CODEX_PAID_APPROVAL",
        "CODEX_AGENTIC_PAID_APPROVAL",
        "CODEX_AUTH_SOURCE",
        "CODEX_RUN_DIR",
    ):
        env.pop(name, None)

    completed = _run_batch("codex", env, "--dry-run")

    assert completed.returncode == 0, completed.stderr
    calls = call_log.read_text(encoding="utf-8").splitlines()
    codex_calls = [line for line in calls if "run-codex-structural.py" in line]
    assert len(codex_calls) == 3
    assert all("--dry-run" in line for line in codex_calls)
    structural_plans = [line for line in codex_calls if "--tasks FN-02" not in line]
    assert [_option_value(line, "--model") for line in structural_plans] == ["gpt-5.6-luna", "gpt-5.6-terra"]
    full_plan = structural_plans[0]
    assert "--task-id" not in full_plan
    assert "--study" not in full_plan
    assert "--paid" not in full_plan
    assert "--paid-approval" not in full_plan
    assert "--max-wall-clock-seconds" not in full_plan
    agentic_calls = [line for line in calls if "run-codex-agentic.py" in line and "--resolve-scope" not in line]
    assert len(agentic_calls) == 2
    assert f"--manifest-path {AGENTIC_MANIFEST}" in agentic_calls[0]
    assert all("--repetitions 1" in line and "--dry-run" in line for line in agentic_calls)
    assert "--model" not in agentic_calls[0]
    assert "--model gpt-5.6-terra" in agentic_calls[1]
    dispatched = [
        line
        for line in calls
        if "run-codex-structural.py" in line or ("run-codex-agentic.py" in line and "--resolve-scope" not in line)
    ]
    assert all("run-codex-structural.py" in line for line in dispatched[: len(codex_calls)])
    assert all("run-codex-agentic.py" in line for line in dispatched[len(codex_calls) :])
    assert all("--auth-source" not in line for line in codex_calls)
    assert all("--auth-source" not in line for line in agentic_calls)
    assert all("--output-path" not in line for line in codex_calls)
    assert all("--output-path" not in line for line in agentic_calls)
    assert all("--render-results" not in line for line in codex_calls)
    assert "PLAN " in completed.stdout
    assert "438 cells" in completed.stdout
    assert completed.stdout.count(f"SCOPE   {DEFAULT_SCOPE_SHA}") == 1
    assert "96 cells" in completed.stdout


@_skip_windows_posix
@pytest.mark.parametrize(
    "lane_args",
    [
        pytest.param(("--struct",), id="struct"),
        pytest.param(("--agentic",), id="agentic"),
        pytest.param((), id="combined"),
    ],
)
def test_codex_default_models_match_an_explicit_luna_terra_selection(
    batch_env: tuple[dict[str, str], Path], lane_args: tuple[str, ...]
) -> None:
    """Omitting --models plans the same ordered two-model studies as naming Luna and Terra."""
    env, call_log = batch_env

    default = _run_batch("codex", env, *lane_args, "--dry-run")
    default_calls = call_log.read_text(encoding="utf-8").splitlines()
    call_log.unlink()
    explicit = _run_batch("codex", env, *lane_args, "--models=luna,terra", "--dry-run")
    explicit_calls = call_log.read_text(encoding="utf-8").splitlines()

    assert default.returncode == 0, default.stderr
    assert explicit.returncode == 0, explicit.stderr
    default_runners = [
        line
        for line in default_calls
        if ("run-codex-structural.py" in line or "run-codex-agentic.py" in line) and "--resolve-scope" not in line
    ]
    explicit_runners = [
        line
        for line in explicit_calls
        if ("run-codex-structural.py" in line or "run-codex-agentic.py" in line) and "--resolve-scope" not in line
    ]
    assert default_runners == explicit_runners
    default_approvals = [line.strip() for line in default.stdout.splitlines() if "CODEX_PAID_APPROVAL=" in line]
    explicit_approvals = [line.strip() for line in explicit.stdout.splitlines() if "CODEX_PAID_APPROVAL=" in line]
    assert len(default_approvals) == 1
    assert default_approvals == explicit_approvals


@_skip_windows_posix
def test_codex_accepts_supported_flags_without_an_argument_count_ceiling(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """Supported selectors remain composable as the launcher grows new options."""
    env, _ = batch_env

    completed = _run_batch(
        "codex",
        env,
        "--agentic",
        "--tasks=BA-02,BA-04",
        "--repetitions=2",
        "--dry-run",
    )

    assert completed.returncode == 0, completed.stderr


@_skip_windows_posix
def test_codex_version_is_observed_without_becoming_an_admission_requirement(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """CLI version is provenance only; direct capability checks protect execution."""
    env, _ = batch_env
    codex = Path(env["PATH"].split(":", maxsplit=1)[0]) / "codex"
    _write_executable(codex, 'printf "codex-cli 0.999.0\\n"')

    accepted = _run_batch("codex", env, "--dry-run")

    assert accepted.returncode == 0, accepted.stderr
    assert "Codex CLI: codex-cli 0.999.0" in accepted.stdout

    _write_executable(codex, 'printf "codex-cli 0.1.0\\n"')
    older = _run_batch("codex", env, "--dry-run")

    assert older.returncode == 0, older.stderr
    assert "Codex CLI: codex-cli 0.1.0" in older.stdout


@_skip_windows_posix
def test_paid_codex_uses_a_fresh_default_run_directory_without_total_timeout(
    batch_env: tuple[dict[str, str], Path],
    tmp_path: Path,
) -> None:
    """Paid launch needs approval and auth, while run naming and total duration stay automatic."""
    env, _ = batch_env
    results_root = tmp_path / "results"
    env["CODEX_RESULTS_ROOT"] = str(results_root)
    env.pop("CODEX_RUN_DIR")

    completed = _run_batch("codex", env, "--struct", "--models=luna")

    assert completed.returncode == 0, completed.stderr
    run_dirs = list(results_root.glob("codex-integration-*"))
    assert len(run_dirs) == 1
    assert (run_dirs[0] / "benchmark" / "run-metadata.json").is_file()


@_skip_windows_posix
def test_paid_codex_executes_from_a_run_scoped_source_snapshot(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """Workspace edits after launch cannot change benchmark code, manifests, or plugins."""
    env, call_log = batch_env
    expected_launcher = SCRIPT.read_bytes()
    expected_manifest = ACTIVE_MANIFEST.read_bytes()

    completed = _run_batch("codex", env, "--struct", "--models=luna")

    assert completed.returncode == 0, completed.stderr
    source_root = Path(env["CODEX_RUN_DIR"]) / ".launcher" / "source"
    assert (source_root / "benchmarks" / "run-all.sh").read_bytes() == expected_launcher
    assert (source_root / "benchmarks" / "manifests" / "codex-integration.json").read_bytes() == expected_manifest
    assert (source_root / "plugins" / "codemap-py" / ".codex-plugin" / "plugin.json").is_file()
    mode_map = json.loads(
        (source_root / "benchmarks" / "manifests" / "codemap-package-mode-map.json").read_text(encoding="utf-8")
    )
    assert mode_map["README.md"] is False
    paid_call = next(line for line in call_log.read_text(encoding="utf-8").splitlines() if "--auth-source" in line)
    assert str(source_root / "benchmarks" / "run-codex-structural.py") in paid_call


@_skip_windows_posix
@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_struct_flag_dispatches_only_the_provider_structural_runner(
    batch_env: tuple[dict[str, str], Path],
    provider: str,
) -> None:
    """The explicit structural selector excludes the provider's agentic runner."""
    env, call_log = batch_env

    completed = _run_batch(provider, env, "--struct", "--dry-run")

    assert completed.returncode == 0, completed.stderr
    calls = call_log.read_text(encoding="utf-8")
    assert f"run-{provider}-structural.py" in calls
    assert f"run-{provider}-agentic.py" not in calls
    other_provider = "codex" if provider == "claude" else "claude"
    assert f"run-{other_provider}-" not in calls


@_skip_windows_posix
@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize(
    "selectors",
    [
        pytest.param(("--struct", "--agentic"), id="conflicting-selectors"),
        pytest.param(("--struct", "--struct"), id="duplicate-struct"),
    ],
)
def test_struct_selector_rejects_conflicts_before_setup(
    batch_env: tuple[dict[str, str], Path],
    provider: str,
    selectors: tuple[str, str],
) -> None:
    """Mode selection must be singular and validated before repository setup."""
    env, call_log = batch_env

    completed = _run_batch(provider, env, *selectors)

    assert completed.returncode == 2
    assert "usage: bash benchmarks/run-all.sh" in completed.stderr
    assert not call_log.exists()


@_skip_windows_posix
def test_codex_agentic_dry_run_dispatches_the_default_shared_scope_once(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """The launcher advertises the 16-task, three-arm, one-repeat dry-run plan.

    Prevents launcher drift where the runner is correct but ``run-all.sh`` still
    dispatches only BA-01 or supplies the retired three-repeat default.
    """
    env, call_log = batch_env
    for name in (
        "CODEX_AGENTIC_PAID_APPROVAL",
        "CODEX_AUTH_SOURCE",
        "CODEX_RUN_DIR",
    ):
        env.pop(name, None)

    completed = _run_batch("codex", env, "--agentic", "--dry-run")

    assert completed.returncode == 0, completed.stderr
    calls = call_log.read_text(encoding="utf-8").splitlines()
    agentic_call = next(line for line in calls if "run-codex-agentic.py" in line and "--dry-run" in line)
    assert f"--manifest-path {AGENTIC_MANIFEST}" in agentic_call
    assert "--task-id" not in agentic_call
    assert "--repetitions 1" in agentic_call
    assert "--scope-sha256" not in agentic_call
    assert "--dry-run" in agentic_call
    assert "--auth-source" not in agentic_call
    assert "--output-path" not in agentic_call
    assert "--metadata-path" not in agentic_call
    assert "run-codex-structural.py" not in "\n".join(calls)
    assert "PROBE   A_plain" in completed.stdout
    assert "48 cells" in completed.stdout
    assert not Path(env.get("CODEX_RUN_DIR", "unused")).exists()


@_skip_windows_posix
def test_codex_agentic_launcher_resolves_a_positive_repeat_override_before_setup(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """A positive override binds the paid plan to its derived 96-cell scope.

    Prevents the launcher from retaining the retired fixed-repeat admission or
    dispatching a larger scope without forwarding its explicit scope identity.
    """
    env, call_log = batch_env

    completed = _run_batch("codex", env, "--agentic", "--dry-run", "--repetitions=2")

    assert completed.returncode == 0, completed.stderr
    agentic_call = next(
        line
        for line in call_log.read_text(encoding="utf-8").splitlines()
        if "run-codex-agentic.py" in line and "--dry-run" in line
    )
    assert "--repetitions 2" in agentic_call
    assert f"--scope-sha256 {AGENTIC_REPEAT_TWO_SCOPE_SHA}" in agentic_call
    assert "96 cells" in completed.stdout


@_skip_windows_posix
def test_claude_agentic_dry_run_dispatches_only_the_default_shared_scope(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """The Claude launcher binds the 144-cell dry-run to its resolved scope.

    Prevents the two providers from exposing incompatible agentic flags or from
    retaining Claude's historical full-batch path for the shared suite.
    """
    env, call_log = batch_env

    completed = _run_batch("claude", env, "--agentic", "--dry-run")

    assert completed.returncode == 0, completed.stderr
    calls = call_log.read_text(encoding="utf-8").splitlines()
    claude_agentic_calls = [line for line in calls if "run-claude-agentic.py" in line]
    assert len(claude_agentic_calls) == 2
    resolver_call = next(line for line in claude_agentic_calls if "--resolve-scope" in line)
    plan_call = next(line for line in claude_agentic_calls if "--dry-run" in line)
    assert f"--manifest-path {METHODOLOGY_MANIFEST}" in resolver_call
    assert "--repeat 1" in resolver_call
    assert "--dry-run" not in resolver_call
    assert f"--manifest-path {METHODOLOGY_MANIFEST}" in plan_call
    assert "--repeat 1" in plan_call
    assert "--scope-sha256" not in plan_call
    assert "--dry-run" in plan_call
    assert "--tasks" not in plan_call
    assert "--arm" not in plan_call
    assert "--model" not in plan_call
    assert not any("run-claude-structural.py" in line for line in calls)
    assert not any("run-codex-" in line for line in calls)
    assert f"{CLAUDE_AGENTIC_TOTAL_CELLS} cells" in completed.stdout


@_skip_windows_posix
def test_claude_agentic_launcher_binds_repeat_override_to_its_exact_scope(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """A nondefault Claude repeat forwards its distinct resolved scope hash."""
    env, call_log = batch_env

    completed = _run_batch("claude", env, "--agentic", "--dry-run", "--repetitions=2")

    assert completed.returncode == 0, completed.stderr
    calls = call_log.read_text(encoding="utf-8").splitlines()
    claude_agentic_calls = [line for line in calls if "run-claude-agentic.py" in line]
    assert len(claude_agentic_calls) == 2
    resolver_call = next(line for line in claude_agentic_calls if "--resolve-scope" in line)
    plan_call = next(line for line in claude_agentic_calls if "--dry-run" in line)
    assert "--repeat 2" in resolver_call
    assert "--repeat 2" in plan_call
    assert f"--scope-sha256 {CLAUDE_AGENTIC_REPEAT_TWO_SCOPE_SHA}" in plan_call
    assert f"{CLAUDE_AGENTIC_TOTAL_CELLS * 2} cells" in completed.stdout


@_skip_windows_posix
@pytest.mark.parametrize(
    "args",
    [
        pytest.param(("--repetitions=2",), id="repeat-without-agentic"),
        pytest.param(("--agentic", "--agentic"), id="duplicate-agentic"),
        pytest.param(("--agentic", "--dry-run", "--dry-run"), id="duplicate-dry-run"),
        pytest.param(("--agentic", "--repetitions=0"), id="zero-repeat"),
        pytest.param(("--agentic", "--repetitions=invalid"), id="invalid-repeat"),
        pytest.param(("--agentic", "--unknown"), id="unknown-flag"),
    ],
)
def test_claude_agentic_launcher_rejects_invalid_or_duplicate_flags_before_setup(
    batch_env: tuple[dict[str, str], Path],
    args: tuple[str, ...],
) -> None:
    """Claude flag validation is symmetric with Codex and runs before setup."""
    env, call_log = batch_env

    completed = _run_batch("claude", env, *args)

    assert completed.returncode == 2
    assert "usage: bash benchmarks/run-all.sh" in completed.stderr
    assert not call_log.exists()


@_skip_windows_posix
@pytest.mark.parametrize(
    ("missing", "expected_error"),
    [
        pytest.param("approval", f"requires CODEX_PAID_APPROVAL={AGENTIC_MANIFEST_SHA[:16]}", id="missing-approval"),
        pytest.param("auth", "requires CODEX_AUTH_SOURCE", id="missing-auth"),
    ],
)
def test_codex_agentic_rejects_missing_paid_inputs_before_setup(
    batch_env: tuple[dict[str, str], Path],
    missing: str,
    expected_error: str,
) -> None:
    """Every required paid input fails before setup, auth access, or model dispatch.

    The approval case removes both accepted token variables, because either one on its own now
    admits the run; leaving the structural name populated would prove a wrong token is refused
    rather than a missing one. The expected error is matched on the gate's own ERROR line, since the
    guidance block below it names CODEX_PAID_APPROVAL in every rejection.
    """
    env, call_log = batch_env
    env["CODEX_AGENTIC_PAID_APPROVAL"] = AGENTIC_MANIFEST_SHA
    for name in {
        "approval": ("CODEX_AGENTIC_PAID_APPROVAL", "CODEX_PAID_APPROVAL"),
        "auth": ("CODEX_AUTH_SOURCE",),
    }[missing]:
        env.pop(name, None)

    completed = _run_batch("codex", env, "--agentic", "--models=luna")

    assert completed.returncode == 2
    assert expected_error in completed.stderr
    assert "bash benchmarks/run-all.sh codex --agentic --models=gpt-5.6-luna --dry-run" in completed.stderr
    assert f"CODEX_PAID_APPROVAL={AGENTIC_MANIFEST_SHA[:16]}" in completed.stderr
    assert "CODEX_AUTH_SOURCE=" in completed.stderr
    assert "CODEX_RUN_DIR only to choose another new path" in completed.stderr
    assert "CODEX_MAX_WALL_CLOCK_SECONDS" not in completed.stderr
    assert "benchmarks/manifests/codex-agentic.md" in completed.stderr
    _assert_safe_paid_preflight(call_log.read_text(encoding="utf-8").splitlines(), agentic=True)


@_skip_windows_posix
def test_codex_agentic_rejects_reused_run_directory_before_setup(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """Agentic evidence cannot overwrite an earlier paid run directory."""
    env, call_log = batch_env
    env["CODEX_AGENTIC_PAID_APPROVAL"] = AGENTIC_MANIFEST_SHA
    Path(env["CODEX_RUN_DIR"]).mkdir()

    completed = _run_batch("codex", env, "--agentic", "--models=luna")

    assert completed.returncode == 2
    assert "CODEX_RUN_DIR already exists" in completed.stderr
    assert "Traceback" not in completed.stderr
    assert "Review the exact no-model shared agentic plan:" in completed.stderr
    assert "bash benchmarks/run-all.sh codex --agentic --models=gpt-5.6-luna --dry-run" in completed.stderr
    assert f"Then launch the paid {AGENTIC_TOTAL_CELLS}-cell study with one scope-bound command:" in completed.stderr
    assert f"CODEX_PAID_APPROVAL={AGENTIC_MANIFEST_SHA[:16]}" in completed.stderr
    assert 'CODEX_AUTH_SOURCE="$HOME/.codex/auth.json"' in completed.stderr
    assert "set CODEX_RUN_DIR only to choose another new path" in completed.stderr
    assert "CODEX_MAX_WALL_CLOCK_SECONDS" not in completed.stderr
    assert "bash benchmarks/run-all.sh codex --agentic" in completed.stderr
    assert "benchmarks/manifests/codex-agentic.md" in completed.stderr
    _assert_safe_paid_preflight(call_log.read_text(encoding="utf-8").splitlines(), agentic=True)


@_skip_windows_posix
def test_paid_codex_agentic_uses_snapshot_and_exact_runner_contract(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """The paid default scope passes only the admitted manifest-bound controls."""
    env, call_log = batch_env
    env["CODEX_AGENTIC_PAID_APPROVAL"] = AGENTIC_MANIFEST_SHA
    env["CODEX_RUN_DIR"] = str(Path(env["CODEX_RUN_DIR"]).with_name("codex-agentic-run"))

    completed = _run_batch("codex", env, "--agentic", "--models=luna")

    assert completed.returncode == 0, completed.stderr
    calls = call_log.read_text(encoding="utf-8").splitlines()
    paid_call = next(line for line in calls if "run-codex-agentic.py" in line and "--auth-source" in line)
    launcher_snapshot = Path(env["CODEX_RUN_DIR"]) / ".launcher" / "run-all.sh"
    source_root = launcher_snapshot.parent / "source"
    for flag, value in (
        ("--repo-path", env["REPO"]),
        ("--index-path", f"{env['REPO']}/.cache/codemap/target.json"),
        ("--marketplace-root", str(source_root)),
        ("--codemap-bin", env["CODEMAP_BIN"]),
        ("--manifest-path", str(source_root / "benchmarks/manifests/codex-agentic.json")),
        ("--auth-source", env["CODEX_AUTH_SOURCE"]),
        ("--invocation-launcher-path", str(launcher_snapshot)),
        ("--run-dir", env["CODEX_RUN_DIR"]),
        ("--paid-approval", AGENTIC_MANIFEST_SHA),
    ):
        assert f"{flag} {value}" in paid_call
    assert "--scope-sha256" not in paid_call
    assert "--dry-run" not in paid_call
    assert "run-codex-structural.py" not in paid_call
    assert launcher_snapshot.read_bytes() == SCRIPT.read_bytes()
    run_log = (Path(env["CODEX_RUN_DIR"]) / "run.log").read_text(encoding="utf-8")
    assert "\x1b[" not in run_log
    assert run_log.count(LEGEND_CLOSE_RULE) == 1
    assert sum(line == LEGEND_OPEN_RULE for line in run_log.splitlines()) == 1
    assert f"SUMMARY  status=completed  persisted_cells={AGENTIC_TOTAL_CELLS}/{AGENTIC_TOTAL_CELLS}" in run_log
    assert (Path(env["CODEX_RUN_DIR"]) / "checksums.sha256").is_file()
    assert "== CODEX SHARED AGENTIC A/B/C STUDY ==" in completed.stdout
    assert sum(line == LEGEND_OPEN_RULE for line in completed.stdout.splitlines()) == 1
    assert sum(line == LEGEND_CLOSE_RULE for line in completed.stdout.splitlines()) == 1
    assert "PLAN " not in completed.stdout


@_skip_windows_posix
def test_paid_codex_agentic_admits_the_short_token_under_the_structural_variable(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """A 16-character token exported as CODEX_PAID_APPROVAL admits the agentic study.

    Scenario: the operator copies the short token the agentic plan prints and exports it under the
    same variable name the structural lane uses. That paste was refused on a real paid run — the
    gate demanded the whole 64-character digest under an agentic-only variable — even though the
    token named this exact scope. Admission must follow, and the launcher must hand the runner the
    full digest the prefix stands for rather than the shortened copy.
    """
    env, call_log = batch_env
    env.pop("CODEX_AGENTIC_PAID_APPROVAL", None)
    env["CODEX_PAID_APPROVAL"] = AGENTIC_MANIFEST_SHA[:16]
    env["CODEX_RUN_DIR"] = str(Path(env["CODEX_RUN_DIR"]).with_name("codex-agentic-short-token-run"))

    completed = _run_batch("codex", env, "--agentic", "--models=luna")

    assert completed.returncode == 0, completed.stderr
    calls = call_log.read_text(encoding="utf-8").splitlines()
    paid_call = next(line for line in calls if "run-codex-agentic.py" in line and "--auth-source" in line)
    assert f"--paid-approval {AGENTIC_MANIFEST_SHA}" in paid_call
    assert "== CODEX SHARED AGENTIC A/B/C STUDY ==" in completed.stdout
    assert f"SUMMARY  status=completed  persisted_cells={AGENTIC_TOTAL_CELLS}/{AGENTIC_TOTAL_CELLS}" in (
        Path(env["CODEX_RUN_DIR"]) / "run.log"
    ).read_text(encoding="utf-8")


@_skip_windows_posix
def test_paid_codex_agentic_final_checksums_exclude_archived_source_tree(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """Publish launcher attestations without rehashing the validated source archive."""
    env, _ = batch_env
    env["CODEX_AGENTIC_PAID_APPROVAL"] = AGENTIC_MANIFEST_SHA
    env["CODEX_RUN_DIR"] = str(Path(env["CODEX_RUN_DIR"]).with_name("codex-agentic-checksum-run"))

    completed = _run_batch("codex", env, "--agentic", "--models=luna")

    assert completed.returncode == 0, completed.stderr
    entries = (Path(env["CODEX_RUN_DIR"]) / "checksums.sha256").read_text(encoding="utf-8").splitlines()
    assert any(".launcher/run-all.sh" in entry for entry in entries)
    assert any(".launcher/source.sha256" in entry for entry in entries)
    assert not any(".launcher/source/" in entry for entry in entries)


@_skip_windows_posix
def test_paid_codex_agentic_admits_the_run_directory_before_console_capture(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """The wrapper leaves the paid runner's launcher-only directory untouched on admission."""
    env, _ = batch_env
    env["ASSERT_AGENTIC_ADMISSION_FRESH"] = "1"
    env["CODEX_AGENTIC_PAID_APPROVAL"] = AGENTIC_MANIFEST_SHA
    env["CODEX_RUN_DIR"] = str(Path(env["CODEX_RUN_DIR"]).with_name("codex-agentic-admission-run"))

    completed = _run_batch("codex", env, "--agentic", "--models=luna")

    assert completed.returncode == 0, completed.stderr
    assert "agentic console artifact existed before paid Python admission" not in completed.stdout


@_skip_windows_posix
def test_paid_codex_agentic_failure_preserves_artifacts_and_prints_fresh_command(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """A failed paid runner preserves diagnostics and explains the fresh retry contract."""
    env, _ = batch_env
    env["CODEX_AGENTIC_PAID_APPROVAL"] = AGENTIC_MANIFEST_SHA
    env["CODEX_RUN_DIR"] = str(Path(env["CODEX_RUN_DIR"]).with_name("codex-agentic-failed-run"))
    env["FAIL_WHEN_ARGS_CONTAIN"] = "--auth-source"

    completed = _run_batch("codex", env, "--agentic", "--models=luna")

    assert completed.returncode == 41
    assert "Preserve the reported artifact for diagnosis" in completed.stderr
    assert "any retry requires a fresh CODEX_RUN_DIR" in completed.stderr
    assert f"CODEX_PAID_APPROVAL={AGENTIC_MANIFEST_SHA[:16]}" in completed.stderr
    assert 'CODEX_AUTH_SOURCE="$HOME/.codex/auth.json"' in completed.stderr
    assert "set CODEX_RUN_DIR only to choose another new path" in completed.stderr
    assert "CODEX_MAX_WALL_CLOCK_SECONDS" not in completed.stderr
    assert "bash benchmarks/run-all.sh codex --agentic" in completed.stderr
    assert (Path(env["CODEX_RUN_DIR"]) / "run.log").is_file()
    assert (Path(env["CODEX_RUN_DIR"]) / "checksums.sha256").is_file()


@_skip_windows_posix
def test_paid_codex_agentic_tty_output_uses_shared_renderer(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """Interactive agentic runs show one colored shared legend, not raw plan output."""
    env, _ = batch_env
    env["CODEX_AGENTIC_PAID_APPROVAL"] = AGENTIC_MANIFEST_SHA
    env["CODEX_RUN_DIR"] = str(Path(env["CODEX_RUN_DIR"]).with_name("codex-agentic-tty-run"))

    completed = _run_batch_tty("codex", env, "--agentic", "--models=luna")

    assert completed.returncode == 0, completed.stdout
    assert "Legend" in completed.stdout
    assert "End legend" in completed.stdout
    assert "\x1b[" in completed.stdout
    assert "PLAN " not in completed.stdout
    run_log = (Path(env["CODEX_RUN_DIR"]) / "run.log").read_text(encoding="utf-8")
    assert "\x1b[" not in run_log
    assert run_log.count(LEGEND_CLOSE_RULE) == 1


@_skip_windows_posix
def test_codex_agentic_selected_dry_run_dispatches_resolved_scope(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """Selected agentic tasks resolve and dispatch without paid credentials."""
    env, call_log = batch_env
    for name in (
        "CODEX_AGENTIC_PAID_APPROVAL",
        "CODEX_AUTH_SOURCE",
        "CODEX_RUN_DIR",
    ):
        env.pop(name, None)

    completed = _run_batch("codex", env, "--agentic", "--tasks=BA-02,BA-04", "--dry-run")

    assert completed.returncode == 0, completed.stderr
    calls = call_log.read_text(encoding="utf-8").splitlines()
    resolver = next(line for line in calls if "run-codex-agentic.py" in line and "--resolve-scope" in line)
    dispatched = next(line for line in calls if "run-codex-agentic.py" in line and "--dry-run" in line)
    assert "--task-id BA-02,BA-04" in resolver
    assert "--task-id BA-02,BA-04" in dispatched
    assert f"--scope-sha256 {AGENTIC_SELECTED_SCOPE_SHA}" in dispatched
    assert "--auth-source" not in dispatched
    assert f"{AGENTIC_SELECTED_TOTAL_CELLS} cells" in completed.stdout


@_skip_windows_posix
def test_codex_tasks_dry_run_dispatches_resolved_scope(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """A task selector resolves to an explicit nonpoolable runner scope."""
    env, call_log = batch_env
    for name in (
        "CODEX_PAID_APPROVAL",
        "CODEX_AUTH_SOURCE",
        "CODEX_RUN_DIR",
    ):
        env.pop(name)

    completed = _run_batch("codex", env, "--struct", "--models=luna", "--tasks=DI,GR", "--dry-run")

    assert completed.returncode == 0, completed.stderr
    codex_calls = [
        line
        for line in call_log.read_text(encoding="utf-8").splitlines()
        if "run-codex-structural.py" in line and "--resolve-tasks" not in line
    ]
    assert len(codex_calls) == 2
    selected = next(line for line in codex_calls if "--tasks DI,GR" in line)
    assert "--dry-run" in selected
    assert "--tasks DI,GR" in selected
    assert "--task-id" not in selected
    assert "--study" not in selected
    assert "--paid" not in selected
    assert "--paid-approval" not in selected
    assert "--max-wall-clock-seconds" not in selected
    assert "--scope-sha256" not in selected
    assert "--auth-source" not in selected
    assert "--output-path" not in selected
    assert "6 cells" in completed.stdout
    assert completed.stdout.count(f"SCOPE   {SELECTED_SCOPE_SHA}") == 1


@_skip_windows_posix
@pytest.mark.parametrize(
    ("mode", "args"),
    [
        pytest.param("smoke", (), id="smoke"),
        pytest.param("claude", ("--struct", "--dry-run"), id="claude-structural"),
        pytest.param("codex", ("--agentic", "--dry-run"), id="codex-agentic"),
        pytest.param("codex", ("--struct", "--tasks=DI,GR", "--dry-run"), id="codex-non-patch-selection"),
    ],
)
def test_non_patch_no_model_modes_do_not_prepare_historical_patch_indexes(
    batch_env: tuple[dict[str, str], Path],
    mode: str,
    args: tuple[str, ...],
) -> None:
    """Historical PT indexes stay out of legacy and non-PT no-model paths."""
    env, call_log = batch_env
    env["FAIL_ON_GIT_FETCH"] = "1"
    env["FAIL_WHEN_ARGS_CONTAIN"] = "--prepare-patch-bundle"

    completed = _run_batch(mode, env, *args)

    assert completed.returncode == 0, completed.stderr
    calls = call_log.read_text(encoding="utf-8")
    assert "--prepare-patch-bundle" not in calls


@_skip_windows_posix
@pytest.mark.parametrize(
    "args",
    [
        pytest.param(("--struct", "--tasks=PT-01", "--dry-run"), id="selected-patch"),
        pytest.param(("--dry-run",), id="unified-codex"),
    ],
)
def test_patch_and_unified_codex_dry_runs_prepare_historical_patch_indexes(
    batch_env: tuple[dict[str, str], Path],
    args: tuple[str, ...],
) -> None:
    """PT selections and the unified Codex study require the locked PT bundle."""
    env, call_log = batch_env

    completed = _run_batch("codex", env, *args)

    assert completed.returncode == 0, completed.stderr
    assert "--prepare-patch-bundle" in call_log.read_text(encoding="utf-8")


@_skip_windows_posix
@pytest.mark.parametrize(
    ("mode", "args"),
    [
        pytest.param("smoke", (), id="smoke"),
        pytest.param("claude", ("--struct", "--dry-run"), id="claude-structural"),
        pytest.param("codex", ("--agentic", "--dry-run"), id="codex-agentic"),
        pytest.param("codex", ("--struct", "--tasks=DI,GR", "--dry-run"), id="codex-non-patch-selection"),
    ],
)
def test_non_patch_no_model_modes_do_not_prepare_the_patch_test_runtime(
    batch_env: tuple[dict[str, str], Path],
    mode: str,
    args: tuple[str, ...],
) -> None:
    """Only a scope containing a PT task pays for the Lightning test environment.

    Building it downloads torch, so a smoke check or a DI/GR selection that never runs the Patch
    behavior oracle must not trigger that download as a side effect of running the launcher.
    """
    env, _ = batch_env
    env.pop("CODEMAP_BENCH_PATCH_PYTEST", None)

    completed = _run_batch(mode, env, *args)

    assert completed.returncode == 0, completed.stderr
    assert "PREPARE patch-stage test runtime" not in completed.stdout


@_skip_windows_posix
def test_patch_dry_run_builds_and_exports_the_patch_test_runtime(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """A PT scope provisions its own pytest so the operator needs no separate preparation step.

    The Patch oracle runs Lightning's tests against the disposable checkout, so the launcher builds
    the environment from the target clone's own requirement files and exports its pytest.
    """
    env, call_log = batch_env
    env.pop("CODEMAP_BENCH_PATCH_PYTEST", None)
    venv_root = Path(env["CODEMAP_BENCH_PATCH_VENV"])

    completed = _run_batch("codex", env, "--struct", "--tasks=PT-01", "--dry-run")

    assert completed.returncode == 0, completed.stderr
    assert f"patch test runtime: {venv_root / 'bin' / 'pytest'}" in completed.stdout
    calls = call_log.read_text(encoding="utf-8")
    assert "uv pip install" in calls
    assert "requirements/pytorch/base.txt" in calls
    assert "requirements/fabric/test.txt" in calls


@_skip_windows_posix
def test_patch_dry_run_keeps_a_caller_supplied_pytest(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """An operator-provided interpreter is used verbatim rather than rebuilt.

    Scope admission fingerprints the pytest launcher, so a caller who minted an approval with one
    interpreter must be able to present that same interpreter for the paid execution.
    """
    env, call_log = batch_env

    completed = _run_batch("codex", env, "--struct", "--tasks=PT-01", "--dry-run")

    assert completed.returncode == 0, completed.stderr
    assert f"patch test runtime: {env['CODEMAP_BENCH_PATCH_PYTEST']} (caller supplied)" in completed.stdout
    assert "uv venv" not in call_log.read_text(encoding="utf-8")


@_skip_windows_posix
def test_patch_dry_run_rejects_a_non_executable_pytest_override(
    batch_env: tuple[dict[str, str], Path],
    tmp_path: Path,
) -> None:
    """A stale or misspelled override fails before setup instead of at the first PT cell.

    Silently rebuilding over a bad override would swap the interpreter the caller's approval was
    bound to, so the launcher stops and names the offending path.
    """
    env, _ = batch_env
    missing = tmp_path / "no-such-pytest"
    env["CODEMAP_BENCH_PATCH_PYTEST"] = str(missing)

    completed = _run_batch("codex", env, "--struct", "--tasks=PT-01", "--dry-run")

    assert completed.returncode != 0
    assert str(missing) in completed.stderr


@_skip_windows_posix
def test_codex_rejects_removed_diagnostic_switch(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """The former asymmetric diagnostic switch is no longer part of the CLI."""
    env, call_log = batch_env

    completed = _run_batch("codex", env, "--diagnostic")

    assert completed.returncode == 2
    assert "--tasks=TASK" in completed.stderr
    assert not call_log.exists()


@_skip_windows_posix
def test_codex_tasks_reject_invalid_selector_before_setup(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """Resolver syntax errors propagate before target/index preparation."""
    env, call_log = batch_env

    completed = _run_batch("codex", env, "--struct", "--tasks=INVALID", "--dry-run")

    assert completed.returncode == 2
    assert "invalid Codex task selection" in completed.stderr
    assert not any("prepare-codex-index.py" in line for line in call_log.read_text(encoding="utf-8").splitlines())


@_skip_windows_posix
@pytest.mark.parametrize(
    "args",
    [
        pytest.param(("--struct", "--tasks=DI,GR", "--dry-run"), id="struct"),
        pytest.param(("--dry-run", "--tasks=DI,GR", "--struct"), id="dry-run"),
    ],
)
def test_codex_tasks_accepts_option_ordering(
    batch_env: tuple[dict[str, str], Path],
    args: tuple[str, str, str],
) -> None:
    """Task selection composes with dry-run regardless of option order."""
    env, call_log = batch_env
    completed = _run_batch("codex", env, *args)

    assert completed.returncode == 0, completed.stderr
    selected = next(
        line
        for line in call_log.read_text(encoding="utf-8").splitlines()
        if "run-codex-structural.py" in line and "--tasks DI,GR" in line
    )
    assert "--dry-run" in selected
    assert "--scope-sha256" not in selected
    assert "--task-id" not in selected
    assert "--study" not in selected
    assert "--paid" not in selected


@_skip_windows_posix
def test_smoke_checks_claude_and_codex_without_paid_codex(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """Smoke must cover both providers while keeping the Codex probe no-model."""
    env, call_log = batch_env

    completed = _run_batch("smoke", env)

    assert completed.returncode == 0, completed.stderr
    calls = call_log.read_text(encoding="utf-8")
    assert "run-claude-structural.py" in calls
    assert "run-claude-agentic.py" in calls
    claude_calls = [line for line in calls.splitlines() if "run-claude-" in line]
    assert claude_calls
    assert all("--dry-run" in line for line in claude_calls)
    codex_call = next(line for line in calls.splitlines() if "run-codex-structural.py" in line)
    assert "--dry-run" in codex_call
    assert "--output-path" not in codex_call
    assert "--auth-source" not in codex_call
    assert f"--manifest-path {ACTIVE_MANIFEST}" in codex_call
    assert f"--codemap-bin {env['CODEMAP_BIN']}" in codex_call
    assert "--no-legend" not in codex_call
    assert "--tasks FN-02" in codex_call
    assert "--task-id" not in codex_call
    assert "--tasks-path" not in codex_call
    assert "--arm" not in codex_call


@_skip_windows_posix
@pytest.mark.parametrize(
    ("mode", "args"),
    [
        pytest.param("smoke", (), id="smoke"),
        pytest.param("codex", ("--models=luna", "--dry-run"), id="codex-dry-run"),
        pytest.param("codex", ("--struct", "--models=luna"), id="codex-struct-paid"),
        pytest.param("codex", ("--struct", "--models=luna", "--tasks=DI,GR", "--dry-run"), id="tasks-dry-run"),
        pytest.param("codex", ("--struct", "--models=luna", "--tasks=DI,GR"), id="tasks-paid"),
    ],
)
def test_top_level_provider_invocation_emits_one_bounded_legend(
    batch_env: tuple[dict[str, str], Path],
    mode: str,
    args: tuple[str, ...],
) -> None:
    """A single-model invocation suppresses nested runner legends."""
    env, _ = batch_env
    if "--tasks=DI,GR" in args:
        env["CODEX_RUN_DIR"] = str(Path(env["CODEX_RUN_DIR"]).with_name("codex-selected-run"))
        env["CODEX_PAID_APPROVAL"] = SELECTED_SCOPE_SHA

    completed = _run_batch(mode, env, *args)

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.count(LEGEND_CLOSE_RULE) == 1
    assert sum(line == LEGEND_OPEN_RULE for line in completed.stdout.splitlines()) == 1


@_skip_windows_posix
@pytest.mark.parametrize(
    ("mode", "failure_pattern", "full_marker"),
    [
        pytest.param("smoke", "run-claude-structural.py", "run-codex-structural.py", id="both-providers"),
        pytest.param("claude", "run-claude-structural.py", "--model haiku --arm A_plain", id="claude"),
        pytest.param("codex", "--tasks FN-02 --dry-run", "--auth-source", id="codex"),
    ],
)
def test_provider_smoke_failure_prevents_full_dispatch(
    batch_env: tuple[dict[str, str], Path],
    mode: str,
    failure_pattern: str,
    full_marker: str,
) -> None:
    """A failed provider smoke must stop before that mode's full workload."""
    env, call_log = batch_env
    env["FAIL_WHEN_ARGS_CONTAIN"] = failure_pattern
    if mode == "codex":
        env["CODEX_AGENTIC_PAID_APPROVAL"] = AGENTIC_MANIFEST_SHA

    completed = _run_batch(mode, env)

    assert completed.returncode == 41
    calls = call_log.read_text(encoding="utf-8")
    assert failure_pattern in calls
    assert full_marker not in calls


@_skip_windows_posix
def test_smoke_rebuilds_mismatched_locked_index_before_provider_commands(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """Rebuild an old or malformed index before provider preflights."""
    env, call_log = batch_env
    index_path = Path(env["REPO"]) / ".cache" / "codemap" / "target.json"
    index_path.write_text("stale-index", encoding="utf-8")

    completed = _run_batch("smoke", env)

    assert completed.returncode == 0, completed.stderr
    assert "stale or schema-incompatible" in completed.stdout
    calls = call_log.read_text(encoding="utf-8")
    assert "run-claude-" in calls
    assert "run-codex-structural.py" in calls


@_skip_windows_posix
def test_smoke_rebuilds_wrong_bytes_with_current_schema_before_provider_commands(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """A v12-shaped but wrong graph must not reach either provider."""
    env, call_log = batch_env
    index_path = Path(env["REPO"]) / ".cache" / "codemap" / "target.json"
    index_path.write_text(
        json.dumps({"scan_version": LOCKED_INDEX_SCAN_VERSION, "modules": [{"wrong": True}]}),
        encoding="utf-8",
    )

    completed = _run_batch("smoke", env)

    assert completed.returncode == 0, completed.stderr
    assert "stale or schema-incompatible" in completed.stdout
    calls = call_log.read_text(encoding="utf-8")
    assert "run-claude-" in calls
    assert "run-codex-structural.py" in calls


@_skip_windows_posix
def test_generated_manifest_build_failure_blocks_provider_preflights(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """Reject a generated Codex record failure before either provider can run."""
    env, call_log = batch_env
    env["FAIL_MANIFEST_CHECK"] = "1"

    completed = _run_batch("smoke", env)

    assert completed.returncode == 47
    assert "generated Codex manifest build failed" in completed.stderr
    calls = call_log.read_text(encoding="utf-8")
    assert "build-codex-integration-manifest.py" in calls
    assert "run-claude-" not in calls
    assert "run-codex-structural.py" not in calls


@_skip_windows_posix
def test_generated_codex_manifest_build_failure_blocks_claude_and_codex_plans(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """Every entrypoint builds the complete manifest closure before planning."""
    env, call_log = batch_env
    env["FAIL_MANIFEST_CHECK"] = "1"

    claude = _run_batch("claude", env, "--struct", "--dry-run")

    assert claude.returncode == 47
    assert "generated Codex manifest build failed" in claude.stderr
    claude_calls = call_log.read_text(encoding="utf-8")
    assert "build-provider-parity-methodology-manifest.py" in claude_calls
    assert "build-codex-integration-manifest.py" in claude_calls
    assert "build-codex-agentic-manifest.py" not in claude_calls
    assert "prepare-codex-index.py" not in claude_calls

    call_log.unlink()
    codex = _run_batch("codex", env, "--struct", "--dry-run")

    assert codex.returncode == 47
    assert "generated Codex manifest build failed" in codex.stderr
    codex_calls = call_log.read_text(encoding="utf-8")
    assert "build-codex-integration-manifest.py" in codex_calls
    assert "build-codex-agentic-manifest.py" not in codex_calls


@_skip_windows_posix
def test_codex_index_preparation_retains_the_dual_lock_cross_check(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """Codex keeps its integration-lock comparison against the methodology lock."""
    env, call_log = batch_env

    completed = _run_batch("codex", env, "--struct", "--dry-run")

    assert completed.returncode == 0, completed.stderr
    contract_call = next(
        line
        for line in call_log.read_text(encoding="utf-8").splitlines()
        if "prepare-codex-index.py" in line and "--print-contract" in line
    )
    assert f"--manifest-path {ACTIVE_MANIFEST}" in contract_call
    assert f"--methodology-path {METHODOLOGY_MANIFEST}" in contract_call


@_skip_windows_posix
@pytest.mark.parametrize(
    ("mode", "arguments"),
    [
        pytest.param("claude", ("--struct", "--dry-run"), id="claude-structural"),
        pytest.param("codex", ("--struct", "--dry-run"), id="codex-structural"),
        pytest.param("codex", ("--agentic", "--dry-run"), id="codex-agentic"),
    ],
)
def test_methodology_manifest_build_failure_stops_every_public_study_before_setup(
    batch_env: tuple[dict[str, str], Path],
    mode: str,
    arguments: tuple[str, ...],
) -> None:
    """A failed first checker must not be hidden by later successful commands or shell contexts."""
    env, call_log = batch_env
    env["FAIL_METHODOLOGY_CHECK"] = "1"

    completed = _run_batch(mode, env, *arguments)

    assert completed.returncode == 46
    assert "generated methodology manifest build failed" in completed.stderr
    calls = call_log.read_text(encoding="utf-8")
    assert "build-provider-parity-methodology-manifest.py" in calls
    assert "build-codex-integration-manifest.py" not in calls
    assert "prepare-codex-index.py" not in calls
    assert "run-claude-" not in calls
    assert "run-codex-" not in calls


@_skip_windows_posix
@pytest.mark.parametrize(
    ("arguments", "approval_variable"),
    [
        pytest.param(("--struct",), "CODEX_PAID_APPROVAL", id="structural"),
        pytest.param(("--agentic",), "CODEX_AGENTIC_PAID_APPROVAL", id="agentic"),
    ],
)
def test_methodology_manifest_build_failure_never_prints_unrunnable_paid_guidance(
    batch_env: tuple[dict[str, str], Path],
    arguments: tuple[str, ...],
    approval_variable: str,
) -> None:
    """Approval guidance is valid only after every manifest it depends on passes validation."""
    env, call_log = batch_env
    env["FAIL_METHODOLOGY_CHECK"] = "1"
    env.pop(approval_variable, None)

    completed = _run_batch("codex", env, *arguments)

    assert completed.returncode == 46
    assert "generated methodology manifest build failed" in completed.stderr
    assert f"{approval_variable}=" not in completed.stderr
    calls = call_log.read_text(encoding="utf-8")
    assert "build-provider-parity-methodology-manifest.py" in calls
    assert "run-codex-" not in calls


@_skip_windows_posix
def test_smoke_accepts_git_worktree_metadata_file(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """Accept linked Git worktrees whose .git metadata is a file, not a directory."""
    env, call_log = batch_env
    git_metadata = Path(env["REPO"]) / ".git"
    git_metadata.rmdir()
    git_metadata.write_text("gitdir: /fixture/worktrees/target", encoding="utf-8")

    completed = _run_batch("smoke", env)

    assert completed.returncode == 0, completed.stderr
    assert "run-codex-structural.py" in call_log.read_text(encoding="utf-8")


@_skip_windows_posix
@pytest.mark.parametrize(
    ("invalid_input", "expected_error"),
    [
        pytest.param("approval", "CODEX_PAID_APPROVAL", id="missing-approval"),
        pytest.param("auth", "CODEX_AUTH_SOURCE", id="missing-auth"),
        pytest.param("existing-run-dir", "already exists", id="existing-run-dir"),
    ],
)
def test_codex_mode_requires_explicit_paid_inputs_before_setup(
    batch_env: tuple[dict[str, str], Path],
    invalid_input: str,
    expected_error: str,
) -> None:
    """Reject incomplete paid authorization before refreshing or invoking tools."""
    env, call_log = batch_env
    if invalid_input == "existing-run-dir":
        Path(env["CODEX_RUN_DIR"]).mkdir()
    else:
        env.pop(
            {
                "approval": "CODEX_PAID_APPROVAL",
                "auth": "CODEX_AUTH_SOURCE",
            }[invalid_input]
        )

    completed = _run_batch("codex", env, "--struct")

    assert completed.returncode == 2
    assert expected_error in completed.stderr
    _assert_safe_paid_preflight(call_log.read_text(encoding="utf-8").splitlines(), agentic=False)


@_skip_windows_posix
def test_codex_paid_rejection_prints_actionable_launch_guidance(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """A rejected paid launch must explain dry-run and copyable paid-run commands."""
    env, _ = batch_env
    env.pop("CODEX_PAID_APPROVAL")

    completed = _run_batch("codex", env, "--struct")

    assert completed.returncode == 2
    assert "bash benchmarks/run-all.sh codex --struct --dry-run" in completed.stderr
    assert "CODEX_PAID_APPROVAL=<approval-token-printed-above>" in completed.stderr
    assert "CODEX_AUTH_SOURCE=" in completed.stderr
    assert 'CODEX_AUTH_SOURCE="$HOME/.codex/auth.json"' in completed.stderr
    assert str(Path.home()) not in completed.stderr
    assert "set CODEX_RUN_DIR only to choose another new path" in completed.stderr
    assert "CODEX_MAX_WALL_CLOCK_SECONDS" not in completed.stderr
    assert "benchmarks/manifests/codex-integration.md" in completed.stderr
    assert "The command records paid authorization for this exact aggregate scope" in completed.stderr
    assert "use an immutable, user-owned 0600 auth source" in completed.stderr
    assert "Do not run a concurrent Codex session with it" in completed.stderr
    assert "independently authenticated benchmark credential" in completed.stderr
    assert "private sequential refresh can invalidate an unchanged source" in completed.stderr
    assert "reauthenticate after the run if needed" in completed.stderr


@_skip_windows_posix
def test_codex_default_paid_rejection_asks_only_for_the_combined_approval(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """Default paid Codex admission asks for one combined token covering both studies.

    Both studies run from one command, so their authorization is one value; the guidance must not send the operator
    looking for a separate agentic variable that combined mode no longer reads.
    """
    env, call_log = batch_env
    env.pop("CODEX_PAID_APPROVAL")

    completed = _run_batch("codex", env)

    assert completed.returncode == 2
    assert "CODEX_PAID_APPROVAL=<combined-approval-token-printed-by-the-unified-dry-run>" in completed.stderr
    assert "CODEX_AGENTIC_PAID_APPROVAL" not in completed.stderr
    assert "bash benchmarks/run-all.sh codex --dry-run" in completed.stderr
    assert "benchmarks/manifests/codex-integration.md" in completed.stderr
    assert "benchmarks/manifests/codex-agentic.md" in completed.stderr
    calls = call_log.read_text(encoding="utf-8").splitlines() if call_log.exists() else []
    assert not any("run-codex-structural.py" in line and "--auth-source" in line for line in calls)
    assert not any("run-codex-agentic.py" in line and "--auth-source" in line for line in calls)


@_skip_windows_posix
def test_codex_default_paid_run_accepts_the_combined_token_without_an_agentic_variable(
    batch_env: tuple[dict[str, str], Path],
    tmp_path: Path,
) -> None:
    """One combined token authorizes the structural and agentic children together.

    This is the contract the unified plan advertises: the operator copies a single command, and the launcher hands
    each child the scope token that child's own gate expects.
    """
    env, call_log = batch_env
    env["CODEX_PAID_APPROVAL"] = COMBINED_SCOPE_SHA[:16]
    env.pop("CODEX_AGENTIC_PAID_APPROVAL", None)
    env["CODEX_RESULTS_ROOT"] = str(tmp_path / "results")
    env.pop("CODEX_RUN_DIR")

    completed = _run_batch("codex", env)

    assert completed.returncode == 0, completed.stderr
    calls = call_log.read_text(encoding="utf-8").splitlines()
    assert any("run-codex-structural.py" in line and "--auth-source" in line for line in calls)
    assert any("run-codex-agentic.py" in line and "--auth-source" in line for line in calls)


@_skip_windows_posix
def test_combined_paid_run_carries_one_token_through_every_selected_stratum(
    batch_env: tuple[dict[str, str], Path],
    tmp_path: Path,
) -> None:
    """A multi-stratum combined token admits the strata children and the agentic child alike.

    Scenario: the operator pastes the one command the combined plan printed for two strata. The
    structural child verifies the combined token, then each stratum runs as its own single-model
    study whose gate expects the plain execution scope — an inherited combined expectation would
    refuse the stratum after the parent had already been admitted.
    """
    env, call_log = batch_env
    env["CODEX_PAID_APPROVAL"] = COMBINED_AGENTIC_MULTI_STRATUM_SCOPE_SHA[:16]
    env.pop("CODEX_AGENTIC_PAID_APPROVAL", None)
    env["CODEX_RESULTS_ROOT"] = str(tmp_path / "results")
    env.pop("CODEX_RUN_DIR")

    completed = _run_batch("codex", env, "--models=gpt-5.6-sol,gpt-5.6-terra")

    assert completed.returncode == 0, completed.stderr + completed.stdout
    calls = call_log.read_text(encoding="utf-8").splitlines()
    paid_structural = [line for line in calls if "run-codex-structural.py" in line and "--auth-source" in line]
    assert any("--model gpt-5.6-sol" in line for line in paid_structural)
    assert any("--model gpt-5.6-terra" in line for line in paid_structural)
    assert any("run-codex-agentic.py" in line and "--auth-source" in line for line in calls)


@_skip_windows_posix
def test_combined_agentic_sweep_rejects_reordered_scope_before_paid_dispatch(
    batch_env: tuple[dict[str, str], Path], tmp_path: Path
) -> None:
    """An approval for one ordered agentic sweep cannot fund a reordered combined run.

    Scenario: the aggregate includes each scalar child approval in order. Reusing the same prefix
    after swapping models must fail during no-model preflight, before either lane receives auth.
    """
    env, call_log = batch_env
    env["CODEX_PAID_APPROVAL"] = COMBINED_AGENTIC_MULTI_STRATUM_SCOPE_SHA[:16]
    env["CODEX_RESULTS_ROOT"] = str(tmp_path / "results")
    env.pop("CODEX_RUN_DIR")

    completed = _run_batch("codex", env, "--models=gpt-5.6-terra,gpt-5.6-sol")

    assert completed.returncode == 2
    assert "requires CODEX_PAID_APPROVAL" in completed.stderr + completed.stdout
    calls = call_log.read_text(encoding="utf-8").splitlines()
    assert not any("--auth-source" in line for line in calls)


@_skip_windows_posix
def test_agentic_sweep_stops_after_a_failed_child(batch_env: tuple[dict[str, str], Path], tmp_path: Path) -> None:
    """A failed scalar agentic child prevents later strata from starting.

    Scenario: a sweep is sequential so completed artifacts survive a failure, while the remaining
    selected models must not receive paid credentials after the first child runner exits non-zero.
    """
    env, call_log = batch_env
    env["CODEX_PAID_APPROVAL"] = AGENTIC_TRIPLE_STRATUM_SCOPE_SHA[:16]
    env["CODEX_RESULTS_ROOT"] = str(tmp_path / "results")
    env.pop("CODEX_RUN_DIR")
    env["FAIL_WHEN_ARGS_CONTAIN"] = "--auth-source"

    completed = _run_batch("codex", env, "--agentic", "--models=luna,terra,sol")

    assert completed.returncode != 0
    paid = [
        line
        for line in call_log.read_text(encoding="utf-8").splitlines()
        if "run-codex-agentic.py" in line and "--auth-source" in line
    ]
    assert paid
    assert not any("--model gpt-5.6-terra" in line for line in paid)
    assert not any("--model gpt-5.6-sol" in line for line in paid)


@_skip_windows_posix
def test_agentic_dry_sweep_stops_without_an_authorization_after_a_runner_failure(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """A failed scalar dry runner prevents both aggregate output and later planned models."""
    env, call_log = batch_env
    env["FAIL_AGENTIC_DRY_MODEL"] = "gpt-5.6-terra"

    completed = _run_batch("codex", env, "--agentic", "--models=terra,sol", "--dry-run")

    assert completed.returncode == 41
    assert "CODEX AGENTIC MODEL-SWEEP AUTHORIZATION" not in completed.stdout
    plans = [
        line
        for line in call_log.read_text(encoding="utf-8").splitlines()
        if "run-codex-agentic.py" in line and "--dry-run" in line and "--resolve-scope" not in line
    ]
    assert [_option_value(line, "--model") for line in plans] == ["gpt-5.6-terra"]


@_skip_windows_posix
def test_agentic_sweep_children_keep_the_selected_tasks_and_repeat_scope(
    batch_env: tuple[dict[str, str], Path], tmp_path: Path
) -> None:
    """Paid sweep children receive the exact task and repetition selections their parent authorized.

    Scenario: parent scope resolution includes both flags. A child that loses either must be refused
    rather than silently running a default suite under a token minted for a narrower repeated scope.
    """
    env, call_log = batch_env
    env["CODEX_PAID_APPROVAL"] = AGENTIC_SELECTED_REPEAT_MULTI_SCOPE_SHA[:16]
    env["CODEX_RESULTS_ROOT"] = str(tmp_path / "results")
    env.pop("CODEX_RUN_DIR")

    completed = _run_batch(
        "codex",
        env,
        "--agentic",
        "--models=terra,sol",
        "--tasks=BA-02,BA-04",
        "--repetitions=2",
    )

    assert completed.returncode == 0, completed.stderr + completed.stdout
    paid = [
        line
        for line in call_log.read_text(encoding="utf-8").splitlines()
        if "run-codex-agentic.py" in line and "--auth-source" in line
    ]
    assert [_option_value(line, "--model") for line in paid] == ["gpt-5.6-terra", "gpt-5.6-sol"]
    assert all("--task-id BA-02,BA-04" in line and "--repetitions 2" in line for line in paid)


@_skip_windows_posix
def test_agentic_sweep_rejects_a_first_child_scope_that_changes_after_approval(
    batch_env: tuple[dict[str, str], Path], tmp_path: Path
) -> None:
    """A child cannot mint a new token after the aggregate already bound its scope.

    Scenario: the parent resolves Terra and Sol before admitting the aggregate. If Terra resolves
    differently immediately before its child starts, no paid runner may receive the replacement
    scope or credential.
    """
    env, call_log = batch_env
    env["CODEX_PAID_APPROVAL"] = hashlib.sha256(
        f"{AGENTIC_STRATUM_SCOPE_SHA} {AGENTIC_SOL_SCOPE_SHA}\ngpt-5.6-terra gpt-5.6-sol\n".encode()
    ).hexdigest()[:16]
    env["CODEX_RESULTS_ROOT"] = str(tmp_path / "results")
    env["DRIFT_AGENTIC_SCOPE_MODEL"] = "gpt-5.6-terra"
    env["DRIFT_AGENTIC_SCOPE_COUNT"] = str(tmp_path / "agentic-scope-count")
    env.pop("CODEX_RUN_DIR")

    completed = _run_batch("codex", env, "--agentic", "--models=terra,sol")

    assert completed.returncode != 0
    assert int(Path(env["DRIFT_AGENTIC_SCOPE_COUNT"]).read_text(encoding="utf-8")) == 4
    paid = [
        line
        for line in call_log.read_text(encoding="utf-8").splitlines()
        if "run-codex-agentic.py" in line and "--auth-source" in line
    ]
    assert not paid


@_skip_windows_posix
def test_agentic_sweep_stops_before_the_third_child_after_the_second_fails(
    batch_env: tuple[dict[str, str], Path], tmp_path: Path
) -> None:
    """Sequential agentic dispatch does not reach a third model after child two fails."""
    env, call_log = batch_env
    env["CODEX_PAID_APPROVAL"] = AGENTIC_SOL_TERRA_LUNA_SCOPE_SHA[:16]
    env["CODEX_RESULTS_ROOT"] = str(tmp_path / "results")
    env["FAIL_AGENTIC_MODEL"] = "gpt-5.6-terra"
    env.pop("CODEX_RUN_DIR")

    completed = _run_batch("codex", env, "--agentic", "--models=sol,terra,luna")

    assert completed.returncode == 41
    paid = [
        line
        for line in call_log.read_text(encoding="utf-8").splitlines()
        if "run-codex-agentic.py" in line and "--auth-source" in line
    ]
    assert [_option_value(line, "--model") for line in paid] == ["gpt-5.6-sol", "gpt-5.6-terra"]


@_skip_windows_posix
def test_structural_sweep_children_keep_the_selected_tasks(
    batch_env: tuple[dict[str, str], Path], tmp_path: Path
) -> None:
    """Structural children receive the selected task scope their aggregate authorized."""
    env, call_log = batch_env
    env["CODEX_PAID_APPROVAL"] = STRUCTURAL_SELECTED_MULTI_SCOPE_SHA[:16]
    env["CODEX_RESULTS_ROOT"] = str(tmp_path / "results")
    env.pop("CODEX_RUN_DIR")

    completed = _run_batch("codex", env, "--struct", "--models=sol,terra", "--tasks=DI,GR")

    assert completed.returncode == 0, completed.stderr + completed.stdout
    paid = [
        line
        for line in call_log.read_text(encoding="utf-8").splitlines()
        if "run-codex-structural.py" in line and "--auth-source" in line
    ]
    assert [_option_value(line, "--model") for line in paid] == ["gpt-5.6-sol", "gpt-5.6-terra"]
    assert all("--tasks DI,GR" in line for line in paid)


@_skip_windows_posix
def test_structural_sweep_stops_when_the_second_child_scope_drifts(
    batch_env: tuple[dict[str, str], Path], tmp_path: Path
) -> None:
    """A second structural child cannot replace the scope bound in the parent aggregate."""
    env, call_log = batch_env
    env["CODEX_PAID_APPROVAL"] = MULTI_STRATUM_SCOPE_SHA[:16]
    env["CODEX_RESULTS_ROOT"] = str(tmp_path / "results")
    env["DRIFT_STRUCTURAL_SCOPE_MODEL"] = "gpt-5.6-terra"
    env["DRIFT_STRUCTURAL_SCOPE_COUNT"] = str(tmp_path / "structural-scope-count")
    env.pop("CODEX_RUN_DIR")

    completed = _run_batch("codex", env, "--struct", "--models=sol,terra")

    assert completed.returncode != 0
    paid = [
        line
        for line in call_log.read_text(encoding="utf-8").splitlines()
        if "run-codex-structural.py" in line and "--auth-source" in line
    ]
    assert [_option_value(line, "--model") for line in paid] == ["gpt-5.6-sol"]


@_skip_windows_posix
def test_multi_stratum_paid_run_admits_a_stratum_whose_scope_differs_from_the_parents(
    batch_env: tuple[dict[str, str], Path],
    tmp_path: Path,
) -> None:
    """Every selected stratum runs, including the ones deriving their own execution scope.

    Scenario: this is the defect that cost a real paid study half its data. A stratum's execution
    scope binds its own model, so only the primary stratum can ever match the scope the parent
    derived. The parent used to hand each child that parent scope, so `gpt-5.6-sol` ran its full 219
    cells and `gpt-5.6-terra` was then refused for a token it could not have matched — after the
    operator had already paid for the first half. The stub answers a distinct scope for the second
    stratum, which is what makes this test able to fail.
    """
    env, call_log = batch_env
    env["CODEX_PAID_APPROVAL"] = MULTI_STRATUM_SCOPE_SHA[:16]
    env.pop("CODEX_AGENTIC_PAID_APPROVAL", None)
    env["CODEX_RESULTS_ROOT"] = str(tmp_path / "results")
    env.pop("CODEX_RUN_DIR")

    completed = _run_batch("codex", env, "--struct", "--models=gpt-5.6-sol,gpt-5.6-terra")

    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert "requires CODEX_PAID_APPROVAL" not in completed.stderr
    paid_structural = [
        line
        for line in call_log.read_text(encoding="utf-8").splitlines()
        if "run-codex-structural.py" in line and "--auth-source" in line
    ]
    assert any("--model gpt-5.6-sol" in line for line in paid_structural)
    assert any("--model gpt-5.6-terra" in line for line in paid_structural)


@_skip_windows_posix
def test_a_stratum_token_does_not_admit_a_model_outside_the_authorized_selection(
    batch_env: tuple[dict[str, str], Path],
    tmp_path: Path,
) -> None:
    """Parent-delegated authorization covers only the strata the operator actually approved.

    Scenario: the child accepts a token it cannot re-derive alone, so the delegation has to stay
    bound to the approved selection. A stratum child launched for a model outside that list must
    still be refused, or the delegation would become a way to pay for an unapproved study.
    """
    env, call_log = batch_env
    env["CODEX_PAID_APPROVAL"] = MULTI_STRATUM_SCOPE_SHA[:16]
    env["CODEX_STRATUM_PARENT_SCOPE"] = f"{DEFAULT_SCOPE_SHA} {SECOND_STRATUM_SCOPE_SHA}"
    env["CODEX_STRATUM_MODELS"] = "gpt-5.6-sol gpt-5.6-terra"
    env["CODEX_STRATUM_AGENTIC_APPROVAL"] = ""
    env["CODEX_RESULTS_ROOT"] = str(tmp_path / "results")
    env.pop("CODEX_RUN_DIR")

    completed = _run_batch("codex", env, "--struct", "--models=gpt-5.6-luna")

    assert completed.returncode != 0
    assert not any(
        "run-codex-structural.py" in line and "--auth-source" in line
        for line in call_log.read_text(encoding="utf-8").splitlines()
    )


@_skip_windows_posix
def test_codex_default_paid_run_rejects_a_token_bound_to_the_structural_scope_alone(
    batch_env: tuple[dict[str, str], Path],
    tmp_path: Path,
) -> None:
    """A structural-only token is refused for a combined run before any paid cell.

    The combined token binds both scopes, so the token minted for `--struct` must not silently authorize the agentic
    study that a selector-free run also pays for.
    """
    env, call_log = batch_env
    env["CODEX_PAID_APPROVAL"] = DEFAULT_SCOPE_SHA[:16]
    env["CODEX_RESULTS_ROOT"] = str(tmp_path / "results")
    env.pop("CODEX_RUN_DIR")

    completed = _run_batch("codex", env)

    assert completed.returncode != 0
    assert f"CODEX_PAID_APPROVAL={COMBINED_SCOPE_SHA[:16]}" in completed.stdout
    calls = call_log.read_text(encoding="utf-8").splitlines()
    assert not any("run-codex-structural.py" in line and "--auth-source" in line for line in calls)
    assert not any("run-codex-agentic.py" in line and "--auth-source" in line for line in calls)


@_skip_windows_posix
def test_combined_paid_run_defers_exact_aggregate_match_until_structural_preflight(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """A syntactically valid stale aggregate approval reaches the no-model structural check only.

    Prevents combined admission from incorrectly comparing the user-provided aggregate
    approval to the manifest hash before the structural preflight derives its scope.
    """
    env, call_log = batch_env
    env["CODEX_PAID_APPROVAL"] = "a" * 64
    env["CODEX_RUN_DIR"] = str(Path(env["CODEX_RUN_DIR"]).with_name("codex-stale-combined-run"))

    completed = _run_batch("codex", env)

    assert completed.returncode == 2
    # Paid study output is intentionally merged into the persisted/rendered stream.
    # The completed preflight exposes the exact replacement approval before the
    # stale value is rejected; direct-runner tests cover the full PAID_COMMAND.
    assert f"CODEX_PAID_APPROVAL={COMBINED_SCOPE_SHA[:16]}" in completed.stdout
    calls = call_log.read_text(encoding="utf-8").splitlines()
    assert any("run-codex-structural.py" in line and "--dry-run" in line for line in calls)
    assert not any("run-codex-structural.py" in line and "--auth-source" in line for line in calls)
    assert not any("run-codex-agentic.py" in line and "--auth-source" in line for line in calls)


@_skip_windows_posix
def test_explicit_codex_struct_rejection_preserves_selector_in_guidance(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """A rejected explicit structural launch prints matching copyable commands."""
    env, _ = batch_env
    env.pop("CODEX_PAID_APPROVAL")

    completed = _run_batch("codex", env, "--struct")

    assert completed.returncode == 2
    assert "bash benchmarks/run-all.sh codex --struct --dry-run" in completed.stderr
    assert "bash benchmarks/run-all.sh codex --struct\n" in completed.stderr


@_skip_windows_posix
def test_provider_modes_dispatch_only_the_selected_provider(
    batch_env: tuple[dict[str, str], Path],
    tmp_path: Path,
) -> None:
    """Selector-free Codex runs both suites from one frozen source in suite order."""
    env, call_log = batch_env

    claude = _run_batch("claude", env)
    assert claude.returncode == 0, claude.stderr
    claude_calls = call_log.read_text(encoding="utf-8")
    assert "run-claude-structural.py" in claude_calls
    assert "run-claude-agentic.py" in claude_calls
    assert "run-codex-structural.py" not in claude_calls

    call_log.unlink()
    env["CODEX_PAID_APPROVAL"] = COMBINED_SCOPE_SHA[:16]
    env["CODEX_RESULTS_ROOT"] = str(tmp_path / "results")
    env.pop("CODEX_RUN_DIR")
    codex = _run_batch("codex", env)
    assert codex.returncode == 0, codex.stderr
    calls = call_log.read_text(encoding="utf-8").splitlines()
    paid_structural = [line for line in calls if "run-codex-structural.py" in line and "--auth-source" in line]
    paid_agentic = [line for line in calls if "run-codex-agentic.py" in line and "--auth-source" in line]
    assert [_option_value(line, "--model") for line in paid_structural] == ["gpt-5.6-luna", "gpt-5.6-terra"]
    assert len(paid_agentic) == 2
    assert "--model" not in paid_agentic[0]
    assert "--model gpt-5.6-terra" in paid_agentic[1]
    structural_call = paid_structural[0]
    agentic_call = paid_agentic[0]
    assert calls.index(structural_call) < calls.index(agentic_call)
    assert "--tasks" not in structural_call
    assert "--task-id" not in structural_call
    assert "--study" not in structural_call
    assert "--paid" not in structural_call.replace("--paid-approval", "")
    assert "--reasoning-effort high" in structural_call
    assert "--max-wall-clock-seconds" not in "\n".join(calls)
    assert not any("run-claude-" in line for line in calls)

    structural_run_dir = Path(_option_value(structural_call, "--run-dir"))
    agentic_run_dir = Path(_option_value(agentic_call, "--run-dir"))
    assert structural_run_dir != agentic_run_dir
    assert structural_run_dir.is_dir()
    assert agentic_run_dir.is_dir()

    structural_launcher = Path(_option_value(structural_call, "--invocation-launcher-path"))
    agentic_launcher = Path(_option_value(agentic_call, "--invocation-launcher-path"))
    assert structural_launcher != agentic_launcher
    structural_source = structural_launcher.parent / "source"
    agentic_source = agentic_launcher.parent / "source"
    assert _option_value(structural_call, "--manifest-path") == str(
        structural_source / "benchmarks/manifests/codex-integration.json"
    )
    assert _option_value(agentic_call, "--manifest-path") == str(
        agentic_source / "benchmarks/manifests/codex-agentic.json"
    )
    assert (
        structural_source / "benchmarks/manifests/codex-integration.json"
    ).read_bytes() == ACTIVE_MANIFEST.read_bytes()
    assert (agentic_source / "benchmarks/manifests/codex-agentic.json").read_bytes() == AGENTIC_MANIFEST.read_bytes()
    assert (structural_launcher.parent / "source.sha256").read_bytes() == (
        agentic_launcher.parent / "source.sha256"
    ).read_bytes()


@_skip_windows_posix
def test_paid_claude_structural_dispatches_shared_provider_parity_matrix(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """Each model receives the shared tasks and deterministic A/B/C scheduler."""
    env, call_log = batch_env

    completed = _run_batch("claude", env, "--struct")

    assert completed.returncode == 0, completed.stderr
    structural_calls = [
        line
        for line in call_log.read_text(encoding="utf-8").splitlines()
        if "run-claude-structural.py" in line and "--tasks" in line and "--dry-run" not in line
    ]
    models = {line.split()[line.split().index("--model") + 1] for line in structural_calls}
    assert models == {"haiku", "sonnet", "opus"}
    assert len(structural_calls) == len(models)
    assert all("--provider-parity" in call for call in structural_calls)
    assert all("--arm" not in call for call in structural_calls)
    assert all(_claude_task_values(call) == SHARED_STRUCTURAL_TASK_IDS for call in structural_calls)
    assert SHARED_STRUCTURAL_TASK_IDS == CONFIRMATORY_TASK_IDS
    assert not any(task_id.startswith("RI-") for task_id in SHARED_STRUCTURAL_TASK_IDS)


@_skip_windows_posix
def test_paid_codex_tasks_runs_only_resolved_scope(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """A paid selected run uses the resolver scope for approval and cells."""
    env, call_log = batch_env
    env["CODEX_PAID_APPROVAL"] = SELECTED_SCOPE_SHA[:16]
    env["CODEX_RUN_DIR"] = str(Path(env["CODEX_RUN_DIR"]).with_name("codex-selected-run"))

    completed = _run_batch("codex", env, "--struct", "--models=luna", "--tasks=DI,GR")

    assert completed.returncode == 0, completed.stderr
    codex_calls = [
        line
        for line in call_log.read_text(encoding="utf-8").splitlines()
        if "run-codex-structural.py" in line and "--render-results" not in line and "--resolve-tasks" not in line
    ]
    assert len(codex_calls) == 3
    paid = next(line for line in codex_calls if "--auth-source" in line)
    assert _option_value(paid, "--paid-approval") == SELECTED_SCOPE_SHA[:16]
    assert "--dry-run" not in paid
    assert "--no-legend" in paid
    assert "--tasks DI,GR" in paid
    assert "--task-id" not in paid
    assert "--study" not in paid
    assert "--paid" not in paid.replace("--paid-approval", "")
    assert "--repetitions" not in paid
    assert "--scope-sha256" not in paid
    assert "--max-wall-clock-seconds" not in paid
    assert f"--run-dir {env['CODEX_RUN_DIR']}/benchmark" in paid
    diagnostic_smoke = next(line for line in codex_calls if "--tasks FN-02 --dry-run" in line)
    assert "--no-legend" in diagnostic_smoke
    selected_plan = next(line for line in codex_calls if "--tasks DI,GR" in line and "--dry-run" in line)
    assert "--no-legend" not in selected_plan


@_skip_windows_posix
def test_paid_codex_checksums_include_canonical_telemetry_sidecar(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """Record and verify the canonical telemetry sidecar in the artifact checksum list."""
    env, _ = batch_env

    completed = _run_batch("codex", env, "--struct", "--models=luna")

    assert completed.returncode == 0, completed.stderr
    run_dir = Path(env["CODEX_RUN_DIR"])
    canonical = run_dir / "benchmark" / "telemetry-canonical.jsonl"
    assert canonical.is_file()
    checksums = (run_dir / "checksums.sha256").read_text(encoding="utf-8").splitlines()
    canonical_line = next(line for line in checksums if "telemetry-canonical.jsonl" in line)
    assert canonical_line.split()[0] == hashlib.sha256(canonical.read_bytes()).hexdigest()


@_skip_windows_posix
def test_codex_mode_reconstructs_a_missing_locked_index_before_dispatch(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """A fresh target must build and byte-check the frozen index before any model command."""
    env, call_log = batch_env
    index_path = Path(env["REPO"]) / ".cache" / "codemap" / "target.json"
    index_path.unlink()

    completed = _run_batch("codex", env, "--struct", "--models=luna")

    assert completed.returncode == 0, completed.stderr
    rebuilt = json.loads(index_path.read_text(encoding="utf-8"))
    assert rebuilt["scan_version"] == LOCKED_INDEX_SCAN_VERSION
    assert rebuilt["modules"] == []
    assert "run-codex-structural.py" in call_log.read_text(encoding="utf-8")


@_skip_windows_posix
def test_paid_codex_noninteractive_output_and_artifact_log_remain_plain(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """A redirected paid run must retain plain terminal output and a plain tee log."""
    env, _ = batch_env

    completed = _run_batch("codex", env, "--struct", "--models=luna")

    assert completed.returncode == 0, completed.stderr
    run_log = (Path(env["CODEX_RUN_DIR"]) / "run.log").read_text(encoding="utf-8")
    assert "\x1b[" not in completed.stdout
    assert "\x1b[" not in run_log
    assert run_log.count(LEGEND_CLOSE_RULE) == 1
    assert sum(line == LEGEND_OPEN_RULE for line in run_log.splitlines()) == 1
    script = SCRIPT.read_text(encoding="utf-8")
    assert "PLAN " not in completed.stdout
    assert "PLAN " in run_log
    assert "RESULT  completed" in completed.stdout
    assert "ARTIFACTS:" in completed.stdout
    assert " - telemetry=" in completed.stdout
    assert "RESULT_RENDERER" not in script
    assert "render-codex-results.py" not in script
    assert (
        'tee "$CODEX_RUN_DIR/run.log" | python3 "$ROOT/benchmarks/run-codex-structural.py" --render-results --hide-plan'
    ) in script
    assert "if [ -t 1 ]; then" not in script
    assert 'echo "→ telemetry:' not in script
    assert 'echo "→ metadata:' not in script


@_skip_windows_posix
def test_paid_codex_tty_output_hides_plan_rows_and_uses_shared_renderer(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """An interactive paid run uses the same renderer while preserving its plan log."""
    env, call_log = batch_env

    completed = _run_batch_tty("codex", env, "--struct", "--models=luna")

    assert completed.returncode == 0, completed.stdout
    run_log = (Path(env["CODEX_RUN_DIR"]) / "run.log").read_text(encoding="utf-8")
    assert "PLAN " not in completed.stdout
    assert "PLAN " in run_log
    assert "--render-results --hide-plan" in call_log.read_text(encoding="utf-8")


@_skip_windows_posix
def test_paid_codex_runner_failure_survives_the_artifact_tee(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """The paid runner's non-zero exit must remain visible after its log pipeline."""
    env, call_log = batch_env
    env["FAIL_WHEN_ARGS_CONTAIN"] = "--auth-source"

    completed = _run_batch("codex", env, "--struct", "--models=luna")

    assert completed.returncode == 41
    assert "--auth-source" in call_log.read_text(encoding="utf-8")
    assert (Path(env["CODEX_RUN_DIR"]) / "run.log").is_file()


@_skip_windows_posix
def test_paid_claude_runner_failure_stops_the_batch(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """Do not reclassify a failed Claude runner as a successful cell outcome."""
    env, call_log = batch_env
    env["FAIL_WHEN_ARGS_CONTAIN"] = "--model haiku --provider-parity"

    completed = _run_batch("claude", env, "--struct")

    assert completed.returncode == 41
    calls = call_log.read_text(encoding="utf-8")
    assert "--model haiku --provider-parity" in calls
    assert "--model sonnet --provider-parity" not in calls


@_skip_windows_posix
def test_paid_codex_renderer_failure_survives_the_artifact_pipeline(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """A failed console renderer must fail the paid pipeline after teeing its log."""
    env, _ = batch_env
    env["FAIL_RENDER_RESULTS"] = "1"

    completed = _run_batch("codex", env, "--struct", "--models=luna")

    assert completed.returncode == 43
    assert (Path(env["CODEX_RUN_DIR"]) / "run.log").is_file()


@_skip_windows_posix
def test_paid_codex_tee_failure_survives_the_artifact_pipeline(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """A failed tee must fail the paid pipeline even when the renderer exits cleanly."""
    env, _ = batch_env
    tee = Path(env["PATH"].split(":", maxsplit=1)[0]) / "tee"
    _write_executable(tee, "exit 44")

    completed = _run_batch("codex", env, "--struct", "--models=luna")

    assert completed.returncode == 44


def _target_lock_dir(env: dict[str, str]) -> Path:
    """Return the shared-clone lock directory the launcher derives for this environment."""
    key = hashlib.sha256(env["REPO"].encode()).hexdigest()[:16]
    return Path(env["TMPDIR"]) / f"codemap-bench-target-{key}.lock"


@_skip_windows_posix
def test_second_study_refuses_to_share_the_target_clone_with_a_live_run(
    batch_env: tuple[dict[str, str], Path],
    tmp_path: Path,
) -> None:
    """A study started while another holds the target clone is refused before any paid work.

    Scenario: a Codex study was launched while the Claude suite was still staging diff-impact edits in
    the same clone. It ran 33 paid cells and then aborted on the other study's dirty worktree, so the
    guard has to reject the second run up front rather than after the spend.
    """
    env, call_log = batch_env
    lock_root = tmp_path / "lock-tmp"
    lock_root.mkdir()
    env["TMPDIR"] = str(lock_root)
    lock = _target_lock_dir(env)
    lock.mkdir()
    (lock / "owner").write_text(f"{os.getpid()} run-all.sh claude\n", encoding="utf-8")

    completed = _run_batch("codex", env, "--struct")

    assert completed.returncode == 2
    assert "already benchmarking" in completed.stderr
    calls = call_log.read_text(encoding="utf-8") if call_log.exists() else ""
    assert "run-codex-structural.py" not in calls
    assert lock.is_dir()


@_skip_windows_posix
def test_stale_lock_from_a_dead_run_does_not_block_the_next_study(
    batch_env: tuple[dict[str, str], Path],
    tmp_path: Path,
) -> None:
    """A lock left behind by a killed run is cleared instead of blocking every later study.

    Scenario: a study is interrupted hard enough to skip its cleanup. Its lock must not become a
    permanent gate that forces the operator to hunt for a stray directory in the temp tree.
    """
    env, _ = batch_env
    lock_root = tmp_path / "lock-tmp"
    lock_root.mkdir()
    env["TMPDIR"] = str(lock_root)
    lock = _target_lock_dir(env)
    lock.mkdir()
    (lock / "owner").write_text("2147483646 run-all.sh claude\n", encoding="utf-8")

    completed = _run_batch("codex", env, "--struct", "--models=luna")

    assert completed.returncode == 0, completed.stderr
    assert "clearing stale target lock" in completed.stdout
    assert not lock.exists()


@_skip_windows_posix
def test_models_selection_restricts_and_orders_the_claude_tiers(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """--models runs the named declared tiers in the order given, and nothing else.

    Scenario: a re-run only needs two of the three Claude tiers, in a specific order, so the
    launcher must pass exactly those to the structural runner instead of its fixed triple.
    """
    env, call_log = batch_env

    completed = _run_batch("claude", env, "--struct", "--dry-run", "--models=opus,haiku")

    assert completed.returncode == 0, completed.stderr
    assert "→ models: opus haiku" in completed.stdout
    calls = call_log.read_text(encoding="utf-8") if call_log.exists() else ""
    invoked = [name for line in calls.splitlines() for name in ("haiku", "sonnet", "opus") if f"--model {name}" in line]
    assert "sonnet" not in invoked
    # The fixed-tier preflight runs first; the selection is the study that follows it.
    assert invoked[-2:] == ["opus", "haiku"]


@_skip_windows_posix
def test_models_selection_rejects_a_model_the_provider_never_declared(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """A model outside the provider's declared strata fails instead of running an unlocked tier.

    Scenario: --models is a restriction, never an addition, so a typo or a Codex model name passed
    to Claude has to stop the run before any study starts.
    """
    env, call_log = batch_env

    completed = _run_batch("claude", env, "--struct", "--dry-run", "--models=opus,gpt-5.6-luna")

    assert completed.returncode == 2
    assert "not a declared claude stratum" in completed.stderr
    calls = call_log.read_text(encoding="utf-8") if call_log.exists() else ""
    assert "--model gpt-5.6-luna" not in calls


@_skip_windows_posix
def test_models_selection_rejects_a_repeated_model(batch_env: tuple[dict[str, str], Path]) -> None:
    """A duplicated name is a typo rather than a request to run a tier twice.

    Scenario: --models exists to narrow a run; silently collapsing or silently repeating a duplicate
    would make the printed model list disagree with what actually ran.
    """
    env, _ = batch_env

    completed = _run_batch("claude", env, "--struct", "--dry-run", "--models=opus,opus")

    assert completed.returncode == 2
    assert "selected more than once" in completed.stderr


@_skip_windows_posix
def test_codex_multi_stratum_dry_run_discloses_the_full_design_and_its_own_token(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """A two-stratum selection prints the summed cell count and a model-bound approval token.

    Scenario: one token buying two studies must say so. The runner prices a single stratum, so
    without this block the printed 219 cells would understate what the approval actually authorizes.
    """
    env, _ = batch_env

    completed = _run_batch("codex", env, "--struct", "--dry-run", "--models=gpt-5.6-luna,gpt-5.6-terra")

    assert completed.returncode == 0, completed.stderr
    assert "CODEX MULTI-STRATUM AUTHORIZATION" in completed.stdout
    assert "MODELS             gpt-5.6-luna gpt-5.6-terra" in completed.stdout
    assert "2 strata" in completed.stdout
    assert "--models=gpt-5.6-luna,gpt-5.6-terra" in completed.stdout


@_skip_windows_posix
def test_codex_multi_stratum_token_binds_the_ordered_model_list(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """Reordering the strata changes the token, so an approval cannot be reused for a different run.

    Scenario: the model list is part of what was disclosed, so it has to be part of what the token
    commits to — otherwise one approval would silently cover any pair of strata at the same scope.
    """
    env, _ = batch_env

    forward = _run_batch("codex", env, "--struct", "--dry-run", "--models=gpt-5.6-luna,gpt-5.6-terra")
    reversed_order = _run_batch("codex", env, "--struct", "--dry-run", "--models=gpt-5.6-terra,gpt-5.6-luna")

    def _token(output: str) -> str:
        line = next(row for row in output.splitlines() if "CODEX_PAID_APPROVAL=" in row)
        return line.split("CODEX_PAID_APPROVAL=", 1)[1].split()[0]

    assert _token(forward.stdout) != _token(reversed_order.stdout)


@_skip_windows_posix
def test_codex_models_selection_runs_the_named_stratum(batch_env: tuple[dict[str, str], Path]) -> None:
    """A single declared stratum reaches the structural runner as its --model argument.

    Scenario: the second Codex tier is run as its own study, so the launcher must forward that name
    rather than the manifest's primary stratum.
    """
    env, call_log = batch_env

    completed = _run_batch("codex", env, "--struct", "--dry-run", "--models=gpt-5.6-terra")

    assert completed.returncode == 0, completed.stderr
    calls = call_log.read_text(encoding="utf-8") if call_log.exists() else ""
    assert "--model gpt-5.6-terra" in calls


@_skip_windows_posix
def test_models_selection_accepts_a_stratum_nickname(batch_env: tuple[dict[str, str], Path]) -> None:
    """A stratum's trailing nickname selects the declared full name it belongs to.

    Scenario: the Codex strata differ only after the last dash, so an operator naming "terra" means
    exactly one declared stratum. The runner still has to receive the full declared name, because
    that name is what the manifest, the run directory, and the results are keyed by.
    """
    env, call_log = batch_env

    completed = _run_batch("codex", env, "--struct", "--dry-run", "--models=terra")

    assert completed.returncode == 0, completed.stderr
    calls = call_log.read_text(encoding="utf-8") if call_log.exists() else ""
    assert "--model gpt-5.6-terra" in calls


@_skip_windows_posix
def test_nickname_and_full_name_mint_the_same_multi_stratum_token(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """Naming strata by nickname authorizes the same run as naming them in full.

    Scenario: nicknames are a spelling of the selection, not a different selection, so an approval
    minted from one spelling has to cover the other. A token that changed with the spelling would
    force a second dry run for a run the operator already priced.
    """
    env, _ = batch_env

    full = _run_batch("codex", env, "--struct", "--dry-run", "--models=gpt-5.6-luna,gpt-5.6-terra")
    nicknamed = _run_batch("codex", env, "--struct", "--dry-run", "--models=luna,terra")

    def _token(output: str) -> str:
        line = next(row for row in output.splitlines() if "CODEX_PAID_APPROVAL=" in row)
        return line.split("CODEX_PAID_APPROVAL=", 1)[1].split()[0]

    assert nicknamed.returncode == 0, nicknamed.stderr
    assert _token(nicknamed.stdout) == _token(full.stdout)
    assert "MODELS             gpt-5.6-luna gpt-5.6-terra" in nicknamed.stdout


@_skip_windows_posix
def test_models_selection_rejects_a_stratum_named_twice_under_two_spellings(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """A nickname and its full name are one stratum, so naming both is still a duplicate.

    Scenario: canonicalizing before the duplicate check is what makes this fail; comparing the raw
    spellings would let "luna,gpt-5.6-luna" through and run one stratum twice under one approval.
    """
    env, _ = batch_env

    completed = _run_batch("codex", env, "--struct", "--dry-run", "--models=luna,gpt-5.6-luna")

    assert completed.returncode == 2
    assert "selected more than once" in completed.stderr


@_skip_windows_posix
def test_agentic_selector_runs_the_stratum_it_names(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """An agentic-only run executes the selected stratum instead of the manifest default.

    Scenario: the agentic lane used to keep its manifest default whatever --models named, printing a
    notice and running the default anyway. Operators who selected another stratum paid for further
    studies of the default one and read them back as the stratum they had asked for, so the selection
    now reaches the runner that executes it and appears in the block that mints the token.
    """
    env, call_log = batch_env

    completed = _run_batch("codex", env, "--agentic", "--models=gpt-5.6-terra", "--dry-run")

    assert completed.returncode == 0, completed.stderr
    assert "stratum           gpt-5.6-terra" in completed.stdout
    calls = call_log.read_text(encoding="utf-8").splitlines()
    agentic_calls = [line for line in calls if "run-codex-agentic.py" in line]
    assert agentic_calls
    assert all("--model gpt-5.6-terra" in line for line in agentic_calls)
    assert all("run-codex-structural.py" not in line for line in calls)


@_skip_windows_posix
def test_agentic_selector_sweeps_each_selected_stratum_in_requested_order(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """A multi-model agentic selection plans one complete study per selected stratum.

    Scenario: each agentic child accepts one model, so the launcher has to retain the ordered
    selection and dispatch three scalar children. Dropping a name or forwarding the whole comma
    list would silently plan the wrong paid scope.
    """
    env, call_log = batch_env

    completed = _run_batch("codex", env, "--agentic", "--models=luna,terra,sol", "--repetitions=1", "--dry-run")

    assert completed.returncode == 0, completed.stderr
    assert "144 cells" in completed.stdout
    calls = call_log.read_text(encoding="utf-8").splitlines()
    plans = [
        line
        for line in calls
        if "run-codex-agentic.py" in line and "--dry-run" in line and "--resolve-scope" not in line
    ]
    assert len(plans) == 3
    assert "--model" not in plans[0]
    assert [_option_value(line, "--model") for line in plans[1:]] == ["gpt-5.6-terra", "gpt-5.6-sol"]
    assert all("--repetitions 1" in line for line in plans)


@_skip_windows_posix
@pytest.mark.parametrize(
    ("provider", "models", "expected"),
    [
        pytest.param("codex", "terra,sol", ("gpt-5.6-terra", "gpt-5.6-sol"), id="codex"),
        pytest.param("claude", "haiku,sonnet", ("haiku", "sonnet"), id="claude"),
    ],
)
def test_agentic_selection_reaches_each_provider_runner_in_requested_order(
    batch_env: tuple[dict[str, str], Path], provider: str, models: str, expected: tuple[str, str]
) -> None:
    """An explicit multi-model agentic selection dispatches scalar ordered runner calls.

    Scenario: the selector is meaningful only if the runner receives each requested model; leaving
    it in the launcher while the provider's default reaches the runner would plan a different study.
    """
    env, call_log = batch_env

    completed = _run_batch(provider, env, "--agentic", f"--models={models}", "--dry-run")

    assert completed.returncode == 0, completed.stderr
    plans = [
        line
        for line in call_log.read_text(encoding="utf-8").splitlines()
        if f"run-{provider}-agentic.py" in line and "--dry-run" in line and "--resolve-scope" not in line
    ]
    assert [_option_value(line, "--model") for line in plans] == list(expected)


@_skip_windows_posix
def test_explicit_agentic_luna_selection_keeps_the_manifest_default_study(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """Naming Luna explicitly keeps its manifest-bound scalar study.

    Scenario: one physical study must not have two valid approvals. Selecting the default explicitly
    is the default, so it has to resolve to the manifest digest rather than to a second, scope-derived
    token that would authorize the same 48 cells under a different name.
    """
    env, _ = batch_env

    selected = _run_batch("codex", env, "--agentic", f"--models={AGENTIC_MANIFEST_MODEL}", "--dry-run")

    assert selected.returncode == 0, selected.stderr
    assert f"CODEX_PAID_APPROVAL={AGENTIC_MANIFEST_SHA[:16]}" in selected.stdout
    assert "the locked agentic manifest digest (whole suite)" in selected.stdout
    assert _agentic_scope(selected.stdout) == AGENTIC_SCOPE_SHA


@_skip_windows_posix
def test_agentic_selector_still_rejects_an_undeclared_model(batch_env: tuple[dict[str, str], Path]) -> None:
    """A stratum the provider never declared fails on an agentic-only run too.

    Scenario: accepting the pairing must not turn --models into free text on the lane that ignores
    it; a typo caught here is a typo the operator would otherwise carry into the structural run.
    """
    env, call_log = batch_env

    completed = _run_batch("codex", env, "--agentic", "--models=nope", "--dry-run")

    assert completed.returncode == 2
    assert "not a declared codex stratum" in completed.stderr
    calls = call_log.read_text(encoding="utf-8") if call_log.exists() else ""
    assert "run-codex-agentic.py" not in calls


@_skip_windows_posix
def test_combined_dry_run_prints_exactly_one_copyable_paid_command(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """A combined no-model plan names one command, not one per lane it walks through.

    Scenario: a combined run pays for both studies under a single token, so the two lane plans it
    prints on the way must not each end in their own copyable command. Three blocks in one plan left
    the operator choosing between commands, two of which authorize only half of what the run does.
    """
    env, call_log = batch_env

    completed = _run_batch("codex", env, "--dry-run")

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.count("PAID_COMMAND:") == 1
    assert "== CODEX COMBINED AUTHORIZATION" in completed.stdout
    assert "== CODEX AGENTIC AUTHORIZATION" not in completed.stdout
    lane_plans = [
        line
        for line in call_log.read_text(encoding="utf-8").splitlines()
        if "run-codex-structural.py" in line and "--dry-run" in line and "--tasks FN-02" not in line
    ]
    assert lane_plans
    assert all("--no-paid-command" in line for line in lane_plans)


@_skip_windows_posix
def test_structural_only_dry_run_keeps_the_command_its_own_lane_authorizes(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """A structural-only plan names exactly one command, and the launcher is what names it.

    Scenario: `--struct --dry-run` is the entire plan for a structural-only study, so it must still
    end in a command the operator can run — naming nothing was the defect the agentic lane once had.
    The runner's own command is suppressed because it is a python entrypoint carrying absolute paths
    that bypasses the launcher; the launcher prints the invocation the operator typed instead.
    """
    env, call_log = batch_env

    completed = _run_batch("codex", env, "--struct", "--dry-run")

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.count("PAID_COMMAND:") == 1
    authorization = completed.stdout.split("== CODEX MULTI-STRATUM AUTHORIZATION", 1)[1]
    assert "bash benchmarks/run-all.sh codex --struct\n" in authorization
    lane_plans = [
        line
        for line in call_log.read_text(encoding="utf-8").splitlines()
        if "run-codex-structural.py" in line and "--dry-run" in line and "--tasks FN-02" not in line
    ]
    assert lane_plans
    assert all("--no-paid-command" in line for line in lane_plans)


@_skip_windows_posix
def test_environment_probe_runs_without_advertising_its_own_single_task_study(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """The one-task environment probe prints no copyable command of its own.

    Scenario: every full run opens with an FN-02 dry run whose only job is proving the environment
    works. It used to print a complete copyable command for that single-task study in the middle of
    the run, which reads as the command the operator is meant to paste. Only the command is
    suppressed — the probe still runs as a dry run whose SCOPE line the launcher parses.
    """
    env, call_log = batch_env

    completed = _run_batch("smoke", env)

    assert completed.returncode == 0, completed.stderr
    assert "PAID_COMMAND:" not in completed.stdout
    probes = [
        line
        for line in call_log.read_text(encoding="utf-8").splitlines()
        if "run-codex-structural.py" in line and "--tasks FN-02" in line
    ]
    assert probes
    assert all("--dry-run" in line and "--no-paid-command" in line for line in probes)


@_skip_windows_posix
def test_explicit_luna_agentic_dry_run_names_the_command_it_authorizes(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """The manifest-default scalar agentic plan ends in a copyable command.

    Scenario: `codex --agentic --dry-run` used to walk an operator through every planned cell and
    then name no way to run them, so the plan authorized nothing. The block also states what its
    token binds, because a full-suite token is the locked manifest digest while the scope printed
    directly above it is the run's own resolved scope — two different digests that would otherwise
    read as a mismatch.
    """
    env, _ = batch_env

    completed = _run_batch("codex", env, "--agentic", "--models=luna", "--dry-run")

    assert completed.returncode == 0, completed.stderr
    authorization = completed.stdout.split("== CODEX AGENTIC AUTHORIZATION", 1)[1]
    assert AGENTIC_SCOPE_SHA in authorization
    assert f"{AGENTIC_TOTAL_CELLS} cells" in authorization
    assert f"stratum           {AGENTIC_MANIFEST_MODEL} (manifest default; select another with --models)" in (
        authorization
    )
    assert "token binds" in authorization
    assert "the locked agentic manifest digest (whole suite)" in authorization
    assert "PAID_COMMAND:" in authorization
    assert f"CODEX_PAID_APPROVAL={AGENTIC_MANIFEST_SHA[:16]}" in authorization
    assert "bash benchmarks/run-all.sh codex --agentic" in authorization


@_skip_windows_posix
def test_combined_mode_binds_every_selected_stratum_into_one_authorization(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """Several strata and both lanes mint a single combined token that carries the ordered list.

    Scenario: `--models` is perpendicular to the lane selectors, so a combined invocation has to
    authorize the whole selection at once. The structural half hashes the ordered model list, the
    combined half wraps that with the agentic scope, and the reprinted command must carry both
    strata — a copy that dropped one would re-derive a different scope and be refused.
    """
    env, call_log = batch_env

    completed = _run_batch("codex", env, "--models=gpt-5.6-sol,gpt-5.6-terra", "--dry-run")

    assert completed.returncode == 0, completed.stderr
    authorization = completed.stdout.split("== CODEX COMBINED AUTHORIZATION", 1)[1]
    assert "strata            gpt-5.6-sol gpt-5.6-terra" in authorization
    assert "bash benchmarks/run-all.sh codex --models=gpt-5.6-sol,gpt-5.6-terra" in authorization
    assert "2 strata (separate, nonpoolable studies)" in authorization
    # One combined token, so one copyable command: a structural-only block here would drop the
    # agentic study from whatever the operator pastes.
    assert "== CODEX MULTI-STRATUM AUTHORIZATION ==" not in completed.stdout
    calls = call_log.read_text(encoding="utf-8").splitlines()
    assert [line for line in calls if "run-codex-structural.py" in line]
    assert [line for line in calls if "run-codex-agentic.py" in line]


@_skip_windows_posix
def test_combined_token_separates_a_multi_stratum_run_from_a_single_stratum_one(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """The ordered model list reaches the combined hash instead of being dropped after the plan.

    Scenario: a token that ignored the selection would let a single-stratum approval pay for a
    two-stratum study. Two dry runs differing only in their selection must therefore mint two
    different combined scopes.
    """
    env, _ = batch_env

    one = _run_batch("codex", env, "--models=gpt-5.6-terra", "--dry-run")
    several = _run_batch("codex", env, "--models=gpt-5.6-sol,gpt-5.6-terra", "--dry-run")

    assert one.returncode == 0, one.stderr
    assert several.returncode == 0, several.stderr
    assert _combined_scope(one.stdout) != _combined_scope(several.stdout)


@_skip_windows_posix
def test_combined_paid_command_carries_the_declared_name_of_the_selected_stratum(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """The combined approval block reprints the selection under its declared name.

    Scenario: the stratum is hashed into the structural scope, so a copied command without the
    selection would re-derive a different scope and be refused by the token this block just minted.
    Naming the stratum in full also makes a nickname selection and its full spelling one command.
    """
    env, _ = batch_env

    completed = _run_batch("codex", env, "--models=terra", "--dry-run")

    assert completed.returncode == 0, completed.stderr
    authorization = completed.stdout.split("== CODEX COMBINED AUTHORIZATION", 1)[1]
    assert "bash benchmarks/run-all.sh codex --models=gpt-5.6-terra" in authorization


@_skip_windows_posix
def test_combined_dry_run_runs_one_selected_stratum_in_both_lanes(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """A selector-free Codex dry run with one stratum plans that stratum in the structural and agentic lanes.

    Scenario: a combined invocation runs a structural child and an agentic child, and one named
    stratum is a study each lane can run. Reaching only the structural child is what made a combined
    terra run publish a terra structural study beside an agentic study of a different model.
    """
    env, call_log = batch_env

    completed = _run_batch("codex", env, "--models=gpt-5.6-terra", "--dry-run")

    assert completed.returncode == 0, completed.stderr
    authorization = completed.stdout.split("== CODEX COMBINED AUTHORIZATION", 1)[1]
    assert "strata            gpt-5.6-terra (both lanes)" in authorization
    calls = call_log.read_text(encoding="utf-8").splitlines()
    structural_dry_run = [line for line in calls if "run-codex-structural.py" in line and "--dry-run" in line]
    assert any("--model gpt-5.6-terra" in line for line in structural_dry_run)
    agentic_dry_run = [line for line in calls if "run-codex-agentic.py" in line and "--dry-run" in line]
    assert agentic_dry_run
    assert all("--model gpt-5.6-terra" in line for line in agentic_dry_run)


@_skip_windows_posix
def test_combined_dry_run_sweeps_every_selected_stratum_in_both_lanes(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """Several strata reach the structural and agentic lanes in the requested order.

    Scenario: combined selection must not make the agentic lane fall back to its manifest default;
    both lanes need scalar children for every selected model.
    """
    env, call_log = batch_env

    completed = _run_batch("codex", env, "--models=gpt-5.6-sol,gpt-5.6-terra", "--dry-run")

    assert completed.returncode == 0, completed.stderr
    authorization = completed.stdout.split("== CODEX COMBINED AUTHORIZATION", 1)[1]
    assert "gpt-5.6-sol gpt-5.6-terra (both lanes)" in authorization
    calls = call_log.read_text(encoding="utf-8").splitlines()
    agentic_dry_run = [line for line in calls if "run-codex-agentic.py" in line and "--dry-run" in line]
    assert [_option_value(line, "--model") for line in agentic_dry_run if "--resolve-scope" not in line] == [
        "gpt-5.6-sol",
        "gpt-5.6-terra",
    ]


@_skip_windows_posix
def test_combined_dry_run_still_rejects_an_undeclared_model(batch_env: tuple[dict[str, str], Path]) -> None:
    """A typo'd stratum fails fast even once the combined guard stops refusing the whole run.

    Scenario: relaxing the combined-mode refusal must not weaken --models into silently accepting
    an unlocked stratum name; the existing per-provider resolution still has to reject it.
    """
    env, call_log = batch_env

    completed = _run_batch("codex", env, "--models=nope", "--dry-run")

    assert completed.returncode == 2
    assert "not a declared codex stratum" in completed.stderr
    calls = call_log.read_text(encoding="utf-8") if call_log.exists() else ""
    assert "run-codex-structural.py" not in calls
    assert "run-codex-agentic.py" not in calls


@_skip_windows_posix
def test_combined_paid_run_forwards_one_selected_stratum_to_both_children(
    batch_env: tuple[dict[str, str], Path],
    tmp_path: Path,
) -> None:
    """The paid combined run threads the selected stratum into the structural child and the agentic child.

    Scenario: each child is a fresh nested invocation of this same script, so threading --models into
    them must be a plain forwarded argument rather than a second, parallel resolution path. Selecting
    one stratum binds that stratum's own execution scope, so the combined token here is derived from
    the terra scope rather than the scope a selector-free run would price.
    """
    env, call_log = batch_env
    env["CODEX_PAID_APPROVAL"] = COMBINED_SECOND_STRATUM_SCOPE_SHA[:16]
    env.pop("CODEX_AGENTIC_PAID_APPROVAL", None)
    env["CODEX_RESULTS_ROOT"] = str(tmp_path / "results")
    env.pop("CODEX_RUN_DIR")

    completed = _run_batch("codex", env, "--models=gpt-5.6-terra")

    assert completed.returncode == 0, completed.stderr
    calls = call_log.read_text(encoding="utf-8").splitlines()
    paid_structural = [line for line in calls if "run-codex-structural.py" in line and "--auth-source" in line]
    paid_agentic = [line for line in calls if "run-codex-agentic.py" in line and "--auth-source" in line]
    assert any("--model gpt-5.6-terra" in line for line in paid_structural)
    assert paid_agentic
    assert all("--model gpt-5.6-terra" in line for line in paid_agentic)


@_skip_windows_posix
def test_isolated_refuses_to_share_the_run_with_an_operator_supplied_repo(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """--isolated and REPO= each name the tree to run in, so asking for both is refused.

    Scenario: honouring one silently would put the study in a tree the operator did not expect —
    either mutating a checkout they manage, or ignoring the one they explicitly named.
    """
    env, call_log = batch_env

    completed = _run_batch("codex", env, "--struct", "--isolated", "--dry-run")

    assert completed.returncode == 2
    assert "--isolated creates this run's own worktree" in completed.stderr
    calls = call_log.read_text(encoding="utf-8") if call_log.exists() else ""
    assert "run-codex-structural.py" not in calls


@_skip_windows_posix
def test_isolated_run_uses_its_own_worktree_and_removes_it_when_the_study_succeeds(
    batch_env: tuple[dict[str, str], Path],
    tmp_path: Path,
) -> None:
    """A successful isolated run works in a private worktree and leaves nothing behind.

    Scenario: the point of the flag is a second study that can run beside the first, which needs a
    tree of its own rather than the one shared checkout. A tree that outlived every successful run
    would turn the feature into a disk leak, so the clean exit has to remove what it created.
    """
    env, call_log = batch_env
    managed = tmp_path / "managed"
    (managed / ".git").mkdir(parents=True)
    frozen_index = managed / ".cache" / "codemap" / f"{managed.name}.json"
    frozen_index.parent.mkdir(parents=True)
    frozen_index.write_text(
        json.dumps({"scan_version": LOCKED_INDEX_SCAN_VERSION, "scan_root": str(managed), "modules": []}),
        encoding="utf-8",
    )
    env.pop("REPO")
    env["BENCH_MANAGED_REPO"] = str(managed)

    completed = _run_batch("codex", env, "--struct", "--isolated", "--dry-run")

    assert completed.returncode == 0, completed.stderr
    assert "== PREPARE private run worktree ==" in completed.stdout
    worktree = next(
        line.split("→ run worktree: ", 1)[1].split(" ", 1)[0]
        for line in completed.stdout.splitlines()
        if line.startswith("→ run worktree: ")
    )
    assert not Path(worktree).exists()
    calls = call_log.read_text(encoding="utf-8")
    assert f"--repo-path {worktree}" in calls


@_skip_windows_posix
def test_isolated_run_keeps_its_worktree_when_the_study_fails(
    batch_env: tuple[dict[str, str], Path],
    tmp_path: Path,
) -> None:
    """A failed isolated run names the worktree it kept instead of deleting the evidence.

    Scenario: the tree holds the staged edit, half-applied patch, or rebuilt index that explains the
    failure. Removing it on the way out would destroy exactly what a diagnosis needs, and leaving it
    unnamed would make the operator hunt for it.
    """
    env, _ = batch_env
    managed = tmp_path / "managed"
    (managed / ".git").mkdir(parents=True)
    frozen_index = managed / ".cache" / "codemap" / f"{managed.name}.json"
    frozen_index.parent.mkdir(parents=True)
    frozen_index.write_text(
        json.dumps({"scan_version": LOCKED_INDEX_SCAN_VERSION, "scan_root": str(managed), "modules": []}),
        encoding="utf-8",
    )
    env.pop("REPO")
    env["BENCH_MANAGED_REPO"] = str(managed)
    env["FAIL_WHEN_ARGS_CONTAIN"] = "run-codex-structural.py"

    completed = _run_batch("codex", env, "--struct", "--isolated", "--dry-run")

    assert completed.returncode != 0
    assert "→ run worktree kept for diagnosis: " in completed.stderr
    kept = completed.stderr.split("→ run worktree kept for diagnosis: ", 1)[1].splitlines()[0]
    assert Path(kept).is_dir()


@_skip_windows_posix
def test_index_gate_off_the_canonical_clone_verifies_semantics_and_names_the_skipped_byte_check(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """Away from the canonical clone the byte hash is skipped out loud, not demanded or dropped.

    Scenario: the locked hash covers bytes that embed the canonical checkout's own path, so it can
    only reproduce there. Demanding it elsewhere would reject every correct index a private worktree
    builds; dropping it silently would leave the operator believing a check that never ran.
    """
    env, call_log = batch_env

    completed = _run_batch("codex", env, "--struct", "--dry-run")

    assert completed.returncode == 0, completed.stderr
    assert "raw byte-identity check skipped" in completed.stderr
    calls = call_log.read_text(encoding="utf-8")
    verify_calls = [line for line in calls.splitlines() if "prepare-codex-index.py" in line and "--verify" in line]
    assert verify_calls
    assert all("--require-hash" not in line for line in verify_calls)


@_skip_windows_posix
def test_isolated_run_relocates_the_locked_index_and_forwards_its_provenance(
    batch_env: tuple[dict[str, str], Path],
    tmp_path: Path,
) -> None:
    """An isolated run installs the locked graph by relocation and tells the runner where its proof is.

    Scenario: scanning the worktree would build a second graph with no link to the locked one, and
    every admission gate would refuse it. The launcher must copy the frozen index instead and pass
    the relocation provenance down, because that is what admission checks in place of the byte hash.
    """
    env, call_log = batch_env
    managed = tmp_path / "managed"
    (managed / ".git").mkdir(parents=True)
    frozen_index = managed / ".cache" / "codemap" / f"{managed.name}.json"
    frozen_index.parent.mkdir(parents=True)
    frozen_index.write_text(
        json.dumps({"scan_version": LOCKED_INDEX_SCAN_VERSION, "scan_root": str(managed), "modules": []}),
        encoding="utf-8",
    )
    env.pop("REPO")
    env["BENCH_MANAGED_REPO"] = str(managed)

    completed = _run_batch("codex", env, "--struct", "--isolated", "--dry-run")

    assert completed.returncode == 0, completed.stderr
    calls = call_log.read_text(encoding="utf-8").splitlines()
    assert any("--relocate-into" in line for line in calls)
    assert all("codemap-py index" not in line for line in calls)
    # The relocated graph still records the canonical clone's module paths, so the semantic digest
    # has to be taken against that root; hashing it against the worktree would leave them unstripped.
    verify_calls = [line for line in calls if "prepare-codex-index.py" in line and "--verify" in line]
    assert verify_calls
    assert all(f"--source-root {managed}" in line for line in verify_calls)
    structural = [line for line in calls if "run-codex-structural.py" in line]
    assert structural
    assert all("--index-relocation-path" in line for line in structural)


@_skip_windows_posix
def test_isolated_run_hands_every_lane_its_relocation_proof(
    batch_env: tuple[dict[str, str], Path],
    tmp_path: Path,
) -> None:
    """Both Claude lanes receive the provenance, not the Codex structural lane alone.

    Scenario: each runner re-checks the index it is handed against the lock. A lane launched
    without the relocation provenance would refuse a perfectly valid isolated run, so `--isolated`
    would work for one provider and fail for the other.
    """
    env, call_log = batch_env
    managed = tmp_path / "managed"
    (managed / ".git").mkdir(parents=True)
    frozen_index = managed / ".cache" / "codemap" / f"{managed.name}.json"
    frozen_index.parent.mkdir(parents=True)
    frozen_index.write_text(
        json.dumps({"scan_version": LOCKED_INDEX_SCAN_VERSION, "scan_root": str(managed), "modules": []}),
        encoding="utf-8",
    )
    env.pop("REPO")
    env["BENCH_MANAGED_REPO"] = str(managed)

    completed = _run_batch("claude", env, "--isolated", "--dry-run")

    assert completed.returncode == 0, completed.stderr
    calls = call_log.read_text(encoding="utf-8").splitlines()
    # The scope resolver reads the manifest alone and never opens the index, so it needs no proof.
    claude_runs = [
        line
        for line in calls
        if ("run-claude-structural.py" in line or "run-claude-agentic.py" in line) and "--resolve-scope" not in line
    ]
    assert claude_runs
    unproven = [line for line in claude_runs if "--index-relocation-path" not in line]
    assert not unproven, unproven


@_skip_windows_posix
def test_isolated_paid_run_keeps_every_child_study_on_the_one_worktree(
    batch_env: tuple[dict[str, str], Path],
    tmp_path: Path,
) -> None:
    """A paid isolated run creates its worktree once and every re-entry runs in that same tree.

    Scenario: a paid run re-execs itself from its frozen launcher and then launches a child study
    per stratum. Each re-entry parses `--isolated` again, so without inheritance it would cut a
    second worktree, and a child re-deriving the managed clone would be handed a relocated index
    belonging to a tree it is not running in.
    """
    env, call_log = batch_env
    managed = tmp_path / "managed"
    (managed / ".git").mkdir(parents=True)
    frozen_index = managed / ".cache" / "codemap" / f"{managed.name}.json"
    frozen_index.parent.mkdir(parents=True)
    frozen_index.write_text(
        json.dumps({"scan_version": LOCKED_INDEX_SCAN_VERSION, "scan_root": str(managed), "modules": []}),
        encoding="utf-8",
    )
    env.pop("REPO")
    env["BENCH_MANAGED_REPO"] = str(managed)
    env["CODEX_PAID_APPROVAL"] = COMBINED_AGENTIC_MULTI_STRATUM_SCOPE_SHA[:16]
    env.pop("CODEX_AGENTIC_PAID_APPROVAL", None)
    env["CODEX_RESULTS_ROOT"] = str(tmp_path / "results")
    env.pop("CODEX_RUN_DIR")

    completed = _run_batch("codex", env, "--isolated", "--models=gpt-5.6-sol,gpt-5.6-terra")

    assert completed.returncode == 0, completed.stderr
    created = [line for line in completed.stdout.splitlines() if line.startswith("→ run worktree: ")]
    assert len(created) == 1, created
    assert any(line.startswith("→ run worktree (inherited): ") for line in completed.stdout.splitlines())
    worktree = created[0].split("→ run worktree: ", 1)[1].split(" ", 1)[0]
    calls = call_log.read_text(encoding="utf-8").splitlines()
    repo_paths = {line.split("--repo-path ", 1)[1].split()[0] for line in calls if "--repo-path " in line}
    assert repo_paths == {worktree}


@_skip_windows_posix
def test_isolated_run_refuses_when_the_managed_clone_has_no_locked_index(
    batch_env: tuple[dict[str, str], Path],
    tmp_path: Path,
) -> None:
    """Without a frozen index to relocate, the isolated run stops and says how to produce one.

    Scenario: relocation is the only way an isolated run may obtain its graph. Falling back to a
    scan would silently produce an index the admission gates cannot tie to the lock, so the refusal
    has to arrive before the worktree is used for anything.
    """
    env, call_log = batch_env
    managed = tmp_path / "managed"
    (managed / ".git").mkdir(parents=True)
    env.pop("REPO")
    env["BENCH_MANAGED_REPO"] = str(managed)

    completed = _run_batch("codex", env, "--struct", "--isolated", "--dry-run")

    assert completed.returncode != 0
    assert "--isolated relocates the locked index" in completed.stderr
    calls = call_log.read_text(encoding="utf-8") if call_log.exists() else ""
    assert "run-codex-structural.py" not in calls


def _paid_command_blocks(stdout: str) -> list[str]:
    """Return the body of every copyable PAID_COMMAND block, borders excluded.

    A block opens with the `PAID_COMMAND:` label, is fenced by two border rules, and holds the exact
    lines an operator is meant to copy — so it is the surface any claim about a printed command has
    to be checked against, rather than the surrounding plan text.
    """
    blocks: list[str] = []
    lines = stdout.splitlines()
    for position, line in enumerate(lines):
        if line.strip() != "PAID_COMMAND:":
            continue
        body: list[str] = []
        for row in lines[position + 2 :]:
            if row.startswith("---"):
                break
            body.append(row)
        blocks.append("\n".join(body))
    return blocks


def _managed_clone_with_frozen_index(env: dict[str, str], tmp_path: Path) -> Path:
    """Point the environment at a managed clone that already holds a relocatable frozen index.

    An isolated run may only obtain its graph by relocating the locked index out of the managed
    clone, so any test that lets `--isolated` reach the worktree step has to stage that clone first.
    """
    managed = tmp_path / "managed"
    (managed / ".git").mkdir(parents=True)
    frozen_index = managed / ".cache" / "codemap" / f"{managed.name}.json"
    frozen_index.parent.mkdir(parents=True)
    frozen_index.write_text(
        json.dumps({"scan_version": LOCKED_INDEX_SCAN_VERSION, "scan_root": str(managed), "modules": []}),
        encoding="utf-8",
    )
    env.pop("REPO", None)
    env["BENCH_MANAGED_REPO"] = str(managed)
    return managed


@_skip_windows_posix
def test_agentic_authorization_reprints_the_isolation_the_operator_asked_for(
    batch_env: tuple[dict[str, str], Path],
    tmp_path: Path,
) -> None:
    """An agentic plan started with --isolated names --isolated in the command it authorizes.

    Scenario: `--isolated` is not decoration — it cuts the run its own git worktree and relocates the
    frozen index into it. A copied command that lost the flag runs the paid study against the shared
    clone instead, which is a different run from the one the plan above it described. The flag used
    to be dropped because the block rebuilt its command from a remembered subset of the invocation.
    """
    env, _ = batch_env
    _managed_clone_with_frozen_index(env, tmp_path)

    completed = _run_batch("codex", env, "--agentic", "--isolated", "--dry-run")

    assert completed.returncode == 0, completed.stderr
    authorization = completed.stdout.split("== CODEX AGENTIC MODEL-SWEEP AUTHORIZATION", 1)[1]
    assert "bash benchmarks/run-all.sh codex --agentic --isolated\n" in authorization


@_skip_windows_posix
def test_multi_stratum_authorization_reprints_the_isolation_the_operator_asked_for(
    batch_env: tuple[dict[str, str], Path],
    tmp_path: Path,
) -> None:
    """A structural multi-stratum plan started with --isolated names --isolated in its paid command.

    Scenario: the structural lane is the one that stages edits in the target tree, so running it
    against the shared clone rather than the private worktree is exactly what `--isolated` exists to
    prevent. The authorization block has to reprint the whole invocation, not the two flags it
    happens to hash.
    """
    env, _ = batch_env
    _managed_clone_with_frozen_index(env, tmp_path)

    completed = _run_batch("codex", env, "--struct", "--isolated", "--models=gpt-5.6-sol,gpt-5.6-terra", "--dry-run")

    assert completed.returncode == 0, completed.stderr
    authorization = completed.stdout.split("== CODEX MULTI-STRATUM AUTHORIZATION", 1)[1]
    assert "bash benchmarks/run-all.sh codex --struct --isolated --models=gpt-5.6-sol,gpt-5.6-terra\n" in authorization


@_skip_windows_posix
def test_multi_stratum_authorization_keeps_the_task_selection_its_token_binds(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """A task-narrowed multi-stratum plan reprints --tasks alongside the strata it names.

    Scenario: the selection is hashed into the structural scope the multi-stratum token wraps, so a
    command that named only the strata would re-derive the whole 73-task scope and be refused by the
    token printed directly above it. Dropping it was the same defect as dropping --isolated: the
    block listed the flags it remembered instead of reprinting the invocation.
    """
    env, _ = batch_env

    completed = _run_batch("codex", env, "--struct", "--tasks=DI,GR", "--models=gpt-5.6-sol,gpt-5.6-terra", "--dry-run")

    assert completed.returncode == 0, completed.stderr
    authorization = completed.stdout.split("== CODEX MULTI-STRATUM AUTHORIZATION", 1)[1]
    assert (
        "bash benchmarks/run-all.sh codex --struct --tasks=DI,GR --models=gpt-5.6-sol,gpt-5.6-terra\n" in authorization
    )


@_skip_windows_posix
def test_agentic_authorization_reprints_the_selected_model(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """The paid command preserves the model selected in the reviewed agentic plan."""
    env, _ = batch_env

    completed = _run_batch("codex", env, "--agentic", "--models=gpt-5.6-terra", "--dry-run")

    assert completed.returncode == 0, completed.stderr
    authorization = completed.stdout.split("== CODEX AGENTIC AUTHORIZATION", 1)[1]
    assert "bash benchmarks/run-all.sh codex --agentic --models=gpt-5.6-terra\n" in authorization


@_skip_windows_posix
def test_structural_paid_command_never_names_the_worktree_the_dry_run_removes(
    batch_env: tuple[dict[str, str], Path],
    tmp_path: Path,
) -> None:
    """No PAID_COMMAND block names an ephemeral run worktree path.

    Scenario: an isolated dry run cuts a private worktree and deletes it on success. The runner's own
    copyable command named that worktree as --repo-path and --index-path, so an operator who copied
    it was handed a command pointing at a repo and an index that no longer existed — not a different
    study but an impossible one. Every printed command must survive the run that printed it.
    """
    env, _ = batch_env
    _managed_clone_with_frozen_index(env, tmp_path)

    completed = _run_batch("codex", env, "--struct", "--isolated", "--dry-run")

    assert completed.returncode == 0, completed.stderr
    blocks = _paid_command_blocks(completed.stdout)
    assert blocks
    assert all("codemap-parity-run-" not in block for block in blocks), blocks


@_skip_windows_posix
def test_structural_authorization_reprints_the_isolation_the_operator_asked_for(
    batch_env: tuple[dict[str, str], Path],
    tmp_path: Path,
) -> None:
    """A structural plan started with --isolated names --isolated in the command it authorizes.

    Scenario: the structural lane stages edits in the target tree, so running it against the shared
    clone rather than the private worktree is what `--isolated` exists to prevent. Reprinting the
    flag lets the paid run cut its own fresh worktree instead of being pointed at a stale one.
    """
    env, _ = batch_env
    _managed_clone_with_frozen_index(env, tmp_path)

    completed = _run_batch("codex", env, "--struct", "--isolated", "--dry-run")

    assert completed.returncode == 0, completed.stderr
    authorization = completed.stdout.split("== CODEX MULTI-STRATUM AUTHORIZATION", 1)[1]
    assert "bash benchmarks/run-all.sh codex --struct --isolated\n" in authorization


@_skip_windows_posix
def test_multi_stratum_dry_run_prints_exactly_one_copyable_paid_command(
    batch_env: tuple[dict[str, str], Path],
) -> None:
    """A multi-stratum structural plan names one command, not one per stratum it walks through.

    Scenario: the runner prices a single stratum, so its own command authorized only the primary one
    while the multi-stratum block beside it authorized the whole ordered list. Two blocks in one plan
    left the operator choosing between commands, one of which silently pays for less than the plan.
    """
    env, _ = batch_env

    completed = _run_batch("codex", env, "--struct", "--models=gpt-5.6-sol,gpt-5.6-terra", "--dry-run")

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.count("PAID_COMMAND:") == 1
    assert "== CODEX MULTI-STRATUM AUTHORIZATION" in completed.stdout
    assert "== CODEX STRUCTURAL AUTHORIZATION" not in completed.stdout
