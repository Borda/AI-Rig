#!/usr/bin/env bash
# Provider-neutral benchmark batch entrypoint.
#
# Usage:
#   bash benchmarks/run-all.sh smoke   # fail-fast Claude + Codex smoke only
#   bash benchmarks/run-all.sh claude  # fail-fast Claude smoke, then full Claude batches
#   bash benchmarks/run-all.sh claude --struct --dry-run  # Claude structural plans only, no model
#   bash benchmarks/run-all.sh claude --struct  # Claude structural batches only
#   bash benchmarks/run-all.sh claude --agentic --dry-run  # shared 144-cell Claude agentic plan, no model
#   bash benchmarks/run-all.sh claude --agentic --tasks=BA-02,BA-04,BA-12,BA-16 --dry-run  # selected nonpoolable Claude plan
#   bash benchmarks/run-all.sh claude --agentic --repetitions=2 --dry-run  # scope-bound Claude repeat override
#   bash benchmarks/run-all.sh codex --struct --dry-run  # default Luna+Terra 73-task/438-cell Codex plans, no model
#   bash benchmarks/run-all.sh claude --struct --models=opus,haiku  # declared tiers, restricted and reordered
#   bash benchmarks/run-all.sh codex --struct --models=luna,terra --dry-run  # both Codex strata, one approval
#   bash benchmarks/run-all.sh codex --struct --models=terra --dry-run  # second Codex stratum alone
#
#   bash benchmarks/run-all.sh codex --models=terra --dry-run  # combined plan on one named stratum
#   bash benchmarks/run-all.sh codex --agentic --models=sol --dry-run  # agentic plan on one named stratum
#
#   bash benchmarks/run-all.sh codex --struct --isolated --dry-run  # private worktree, runs beside another study
#
# --isolated gives the run its own git worktree off the managed clone instead of sharing the one
# canonical checkout, so two studies can run at the same time. The worktree is removed when the run
# succeeds and kept, with its path printed, when it fails; the next isolated run prunes what a killed
# run left. The locked index is copied in with only its scan root moved rather than scanned again,
# and that relocation provenance travels to every lane the run launches. It costs one checkout and
# one index copy, and it cannot be combined with REPO=, which
# already names a tree the operator manages. Off the canonical clone the index is verified by its
# path-independent semantic digest; the byte hash is checked only at the canonical clone itself.
#
# --models restricts and orders the provider's declared strata; it never introduces one. A stratum
# answers to its full declared name or to its nickname — the segment after the last dash, so
# gpt-5.6-terra is also "terra" — whenever that nickname belongs to exactly one declared stratum.
# The selection pairs with any lane and is validated in every mode. The structural lane runs one study
# per selected stratum, as does the agentic lane. Combined invocations run both lanes on the same
# ordered selection. Codex defaults to luna,terra and prints one token covering the selected studies.
#   bash benchmarks/run-all.sh codex --struct  # paid unified Codex task study
#   bash benchmarks/run-all.sh codex --dry-run  # default Luna+Terra task + agentic Codex plans, no model
#   bash benchmarks/run-all.sh codex   # paid Luna+Terra task studies, then paid Luna+Terra agentic studies
#   bash benchmarks/run-all.sh codex --struct --tasks=RC,FS,FM,PT [--dry-run]  # selected stage-native task families
#   bash benchmarks/run-all.sh codex --agentic --dry-run  # default Luna+Terra 96-cell agentic plans, no model
#   bash benchmarks/run-all.sh codex --agentic --tasks=BA-02,BA-04,BA-12,BA-16 --dry-run  # selected nonpoolable Codex plan
#   bash benchmarks/run-all.sh codex --agentic --repetitions=2 --dry-run  # scope-bound repeat override
#   bash benchmarks/run-all.sh codex --agentic  # paid shared agentic study
#
# Paid Codex modes require the active scope approval and a private auth source.
# The launcher creates a fresh evidence directory when CODEX_RUN_DIR is omitted.
# No mode runs when the argument is missing or unknown. This entrypoint
# reconstructs a missing index and accepts it only when normalization reproduces
# the reviewed byte hash exactly.
set -euo pipefail

ROOT="${CODEX_LAUNCHER_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
CODEX_RESULTS_ROOT="${CODEX_RESULTS_ROOT:-$ROOT/benchmarks/results}"
# Preserve evaluator boundaries when paid execution re-enters a frozen launcher. Child
# providers consume this parent-only provenance before sanitizing their model environments.
BENCHMARK_EVIDENCE_ROOTS="$(python3 -c '
import json, os, sys
from pathlib import Path
roots = json.loads(os.environ.get("BENCHMARK_EVIDENCE_ROOTS", "[]"))
if not isinstance(roots, list) or any(not isinstance(root, str) or not Path(root).is_absolute() for root in roots):
    raise SystemExit("BENCHMARK_EVIDENCE_ROOTS must be a JSON list of absolute paths")
roots.extend(root for root in sys.argv[1:] if root)
print(json.dumps(list(dict.fromkeys(str(Path(root).resolve()) for root in roots))))
' "$ROOT" "$CODEX_RESULTS_ROOT" "${CODEX_RUN_DIR:-}")"
export BENCHMARK_EVIDENCE_ROOTS
PL_TAG="2.6.5"
PL_URL="${PL_URL:-https://github.com/Lightning-AI/pytorch-lightning.git}"
BENCHMARK_TEMP_ROOT="$(python3 -c 'import os; from pathlib import Path; print((Path(os.sep) / "tmp").resolve())')"
# BENCH_MANAGED_REPO names where the managed clone lives; it never changes what that clone must
# contain. The byte-identity gate still compares this run's index against the locked hash, which
# only reproduces at the path the lock recorded, so relocating the clone cannot smuggle a different
# graph past the check — it fails loudly instead.
MANAGED_REPO="${BENCH_MANAGED_REPO:-$BENCHMARK_TEMP_ROOT/codemap-provider-parity-pl-2.6.5}"
REPO_OVERRIDDEN="${REPO:+1}"
# A paid isolated run re-execs itself from its frozen snapshot and launches child studies; each of
# them inherits this run's worktree as REPO. That is this run's own tree, not an operator override,
# so --isolated must not refuse its own re-invocation over it.
if [ -n "${BENCH_RUN_WORKTREE:-}" ] && [ "${REPO:-}" = "${BENCH_RUN_WORKTREE:-}" ]; then
  REPO_OVERRIDDEN=""
fi
REPO="${REPO:-$MANAGED_REPO}"
INDEX_PATH="$REPO/.cache/codemap/$(basename "$REPO").json"
#: Directory of this run's private worktree, set by --isolated and removed when the run succeeds.
BENCH_RUN_WORKTREE="${BENCH_RUN_WORKTREE:-}"
#: PID of the process that created the worktree; only it may remove one at exit.
BENCH_RUN_WORKTREE_OWNER="${BENCH_RUN_WORKTREE_OWNER:-}"
#: Provenance file proving this run's index is the locked graph with only its scan root moved.
BENCH_RUN_INDEX_RELOCATION="${BENCH_RUN_INDEX_RELOCATION:-}"
ISOLATED=false
#: This invocation's own argument vector, captured before anything reads it. Every copyable command
#: the launcher prints is rendered from this one array rather than rebuilt from the flags a given
#: block happened to remember, so a flag the operator typed cannot go missing from the command they
#: are told to run next.
INVOCATION_ARGV=("$@")
MODE="${1:-}"
DRY_RUN=false
AGENTIC=false
STRUCTURAL=false
AGENTIC_REPETITIONS=1
AGENTIC_REPETITIONS_SET=false
AGENTIC_SCOPE_SHA=""
AGENTIC_TOTAL_CELLS=""
AGENTIC_RUNNER=""
AGENTIC_DISPATCH_ARGS=()
INDEX_RELOCATION_ARGS=()
CODEX_TASKS=""
CODEX_SELECTED_MODEL=""
CODEX_SELECTED_MODELS=()
MODELS_SELECTION=""
PROVIDER_MODELS=()
# Empty for a scalar manifest-default stratum, preserving its manifest-bound approval identity.
AGENTIC_MODEL=""
AGENTIC_SELECTED_MODELS=()
AGENTIC_SWEEP_APPROVALS=()
AGENTIC_MODELS_RESOLVED=false
AGENTIC_SWEEP_TOTAL_CELLS=0
AGENTIC_TASK_IDS=()
SHARED_STRUCTURAL_TASK_IDS=()
CODEX_SELECTION_SCOPE_SHA=""
CODEX_SELECTION_TOTAL_CELLS=""
CODEX_EXECUTION_SCOPE_SHA=""
CODEX_SELECTION_TASK_IDS=()
MANIFEST_PATH="$ROOT/benchmarks/manifests/codex-integration.json"
AGENTIC_MANIFEST_PATH="$ROOT/benchmarks/manifests/codex-agentic.json"
METHODOLOGY_PATH="$ROOT/benchmarks/manifests/provider-parity-methodology.json"
MANIFEST_CHECKER="$ROOT/benchmarks/build-codex-integration-manifest.py"
AGENTIC_MANIFEST_CHECKER="$ROOT/benchmarks/build-codex-agentic-manifest.py"
METHODOLOGY_CHECKER="$ROOT/benchmarks/build-provider-parity-methodology-manifest.py"
CODEMAP_BIN="${CODEMAP_BIN:-$ROOT/plugins/codemap-py/bin/codemap-py}"
INDEX_PREPARER="$ROOT/benchmarks/prepare-codex-index.py"
SCHEMA_PATH="$ROOT/plugins/codemap-py/src/codemap_py/schema.py"
#: Entrypoint rendering a launcher header the way every Python runner renders its own headers.
SECTION_RENDERER="$ROOT/benchmarks/_bench_common/render_cli.py"
LOCKED_INDEX_SHA=""
LOCKED_INDEX_SCAN_VERSION=""

# Announce one run phase. On a terminal the shared presentation layer draws a titled rule; with
# stdout redirected it prints "== title ==", which is what run logs and downstream readers parse.
# A header is decoration on a run that may be paid, so a renderer that cannot run falls back to
# that same line rather than ending the run under set -e; its own error still reaches stderr.
section_rule() {
  python3 "$SECTION_RENDERER" rule "$1" || echo "== $1 =="
}

usage() {
  echo "usage: bash benchmarks/run-all.sh {smoke | claude [--struct|--agentic] [--dry-run] [--isolated] [--models=MODEL[,MODEL...]] [--tasks=TASK[,TASK...]] [--repetitions=N] | codex [--struct|--agentic] [--dry-run] [--isolated] [--models=MODEL[,MODEL...]] [--tasks=TASK[,TASK...]] [--repetitions=N]}" >&2
}

# Render one of this invocation's arguments the way an operator would retype it.
#
# A typed --models= is reprinted under the declared names it resolved to, because the structural
# scope hashes those names: a nickname and its full spelling then copy as one command instead of two
# that look different. Everything else is reprinted verbatim, quoted only when it carries a character
# the shell would reinterpret.
launcher_argument() {
  local argument="$1" resolved=""
  case "$argument" in
    --models=*)
      if [ -n "$MODELS_SELECTION" ]; then
        if [ "${#CODEX_SELECTED_MODELS[@]}" -gt 0 ]; then
          resolved="$(IFS=,; echo "${CODEX_SELECTED_MODELS[*]}")"
        elif [ "${#PROVIDER_MODELS[@]}" -gt 0 ]; then
          resolved="$(IFS=,; echo "${PROVIDER_MODELS[*]}")"
        fi
        if [ -n "$resolved" ]; then
          argument="--models=$resolved"
        fi
      fi
      ;;
  esac
  case "$argument" in
    *[!A-Za-z0-9=,._:/+-]*) printf "'%s'" "$(printf '%s' "$argument" | sed "s/'/'\\\\''/g")" ;;
    *) printf '%s' "$argument" ;;
  esac
}

# Render the one command this run authorizes: the operator's own invocation, with --dry-run dropped
# for a paid command ("paid") or guaranteed present for a plan ("plan").
#
# Every printed command goes through here. Rebuilding them from remembered flags is what let
# --isolated and --tasks fall out of a copied command and silently change what the paid run did, so
# no caller re-lists what it thinks the invocation contained.
launcher_command() {
  local disposition="$1" argument rendered=""
  # The guarded expansion keeps an empty argument vector safe under macOS Bash 3.2 with `set -u`.
  for argument in ${INVOCATION_ARGV[@]+"${INVOCATION_ARGV[@]}"}; do
    if [ "$argument" = "--dry-run" ]; then
      continue
    fi
    rendered="$rendered $(launcher_argument "$argument")"
  done
  if [ "$disposition" = "plan" ]; then
    rendered="$rendered --dry-run"
  fi
  printf 'bash benchmarks/run-all.sh%s' "$rendered"
}

if [ "$#" -lt 1 ]; then
  usage
  exit 2
fi
case "$MODE" in
  smoke)
    if [ "$#" -ne 1 ]; then
      usage
      exit 2
    fi
    ;;
  claude | codex)
    for option in "${@:2}"; do
      case "$option" in
        --dry-run)
          if [ "$DRY_RUN" = true ]; then
            usage
            exit 2
          fi
          DRY_RUN=true
          ;;
        --agentic)
          if [ "$AGENTIC" = true ]; then
            usage
            exit 2
          fi
          AGENTIC=true
          ;;
        --struct)
          if [ "$STRUCTURAL" = true ]; then
            usage
            exit 2
          fi
          STRUCTURAL=true
          ;;
        --isolated)
          if [ "$ISOLATED" = true ]; then
            usage
            exit 2
          fi
          ISOLATED=true
          ;;
        --models=*)
          if [ -n "$MODELS_SELECTION" ]; then
            usage
            exit 2
          fi
          MODELS_SELECTION="${option#--models=}"
          if [ -z "$MODELS_SELECTION" ]; then
            usage
            exit 2
          fi
          ;;
        --tasks=*)
          if [ -n "$CODEX_TASKS" ]; then
            usage
            exit 2
          fi
          CODEX_TASKS="${option#--tasks=}"
          if [ -z "$CODEX_TASKS" ]; then
            usage
            exit 2
          fi
          ;;
        --repetitions=*)
          if [ "$AGENTIC_REPETITIONS_SET" = true ]; then
            usage
            exit 2
          fi
          AGENTIC_REPETITIONS_SET=true
          AGENTIC_REPETITIONS="${option#--repetitions=}"
          if ! [[ "$AGENTIC_REPETITIONS" =~ ^[1-9][0-9]*$ ]]; then
            echo "ERROR: --repetitions must be a positive integer." >&2
            usage
            exit 2
          fi
          ;;
        *)
          usage
          exit 2
          ;;
      esac
    done
    if [ "$AGENTIC" = true ] && [ "$STRUCTURAL" = true ]; then
      echo "ERROR: --struct and --agentic are mutually exclusive." >&2
      usage
      exit 2
    fi
    if [ "$AGENTIC" != true ] && [ "$MODE" != "codex" ] && [ -n "$CODEX_TASKS" ]; then
      echo "ERROR: --tasks is available for Codex structural or either provider's agentic mode." >&2
      usage
      exit 2
    fi
    if [ "$AGENTIC" != true ] && [ "$AGENTIC_REPETITIONS_SET" = true ]; then
      echo "ERROR: --repetitions is available only with --agentic." >&2
      usage
      exit 2
    fi
    # --models is perpendicular to the lane selectors: each named stratum becomes one sequential
    # study in every selected lane. The parent binds the ordered child scopes before paid execution.
    if [ "$ISOLATED" = true ] && [ -n "${REPO_OVERRIDDEN:-}" ]; then
      echo "ERROR: --isolated creates this run's own worktree, so it cannot also take REPO=$REPO." >&2
      echo "       Drop one: REPO= to run in a tree you manage, --isolated to get a private one." >&2
      usage
      exit 2
    fi
    if [ "$MODE" = "codex" ] && [ -n "$CODEX_TASKS" ] && [ "$AGENTIC" != true ] && [ "$STRUCTURAL" != true ]; then
      echo "ERROR: Codex --tasks requires an explicit --struct or --agentic selector." >&2
      usage
      exit 2
    fi
    ;;
  *)
    usage
    exit 2
    ;;
esac

cd "$ROOT"

refresh_generated_manifests() {
  if [ "${CODEX_LAUNCHER_SNAPSHOT_ACTIVE:-}" = "1" ] || {
    [ "$ROOT" = "${CODEX_LAUNCHER_ROOT:-}" ] && [ -n "${CODEX_SOURCE_MANIFEST_SHA256:-}" ]
  }; then
    section_rule "CHECK frozen generated benchmark manifests (no model)"
    CODEX_LAUNCHER_SNAPSHOT_ACTIVE=1 python3 "$METHODOLOGY_CHECKER" --check
    CODEX_LAUNCHER_SNAPSHOT_ACTIVE=1 python3 "$MANIFEST_CHECKER" --check
    CODEX_LAUNCHER_SNAPSHOT_ACTIVE=1 python3 "$AGENTIC_MANIFEST_CHECKER" --check
    return
  fi
  section_rule "BUILD generated benchmark manifests (no model)"
  python3 "$METHODOLOGY_CHECKER"
  python3 "$MANIFEST_CHECKER"
  python3 "$AGENTIC_MANIFEST_CHECKER"
}

refresh_generated_manifests

# Resolved once per run rather than per file: build_source_checksum_manifest hashes
# every file of the paid source snapshot (~420), and probing for the hasher on each
# one made the probe itself a measurable share of the run.
SHA256_CMD=()

resolve_sha256_cmd() {
  if [ "${#SHA256_CMD[@]}" -gt 0 ]; then
    return 0
  fi
  if command -v shasum >/dev/null 2>&1; then
    SHA256_CMD=(shasum -a 256)
  elif command -v sha256sum >/dev/null 2>&1; then
    SHA256_CMD=(sha256sum)
  else
    echo "ERROR: shasum or sha256sum is required to validate frozen evidence." >&2
    return 1
  fi
}

sha256_file() {
  resolve_sha256_cmd || return 1
  "${SHA256_CMD[@]}" "$1" | awk '{print $1}'
}

sha256_string() {
  resolve_sha256_cmd || return 1
  printf '%s' "$1" | "${SHA256_CMD[@]}" | awk '{print $1}'
}

release_target_repo_lock() {
  [ -n "${BENCH_TARGET_LOCK_DIR:-}" ] && rm -rf "$BENCH_TARGET_LOCK_DIR"
  BENCH_TARGET_LOCK_DIR=""
}

# A failed study's worktree carries the staged edit, the half-applied patch, or the rebuilt index that
# explains the failure, so only a clean exit removes it. What survives a failure is named on the way
# out and pruned by the next isolated run, not left for the operator to discover.
release_run_worktree() {
  # The exit status arrives as an argument: reading $? here would see the caller's last assignment,
  # not the status the trap fired on, and every failed run would have its evidence removed.
  local status="${1:-0}"
  [ -z "${BENCH_RUN_WORKTREE:-}" ] && return "$status"
  # Child studies inherit the tree but never own it: a stratum finishing early would otherwise
  # delete the tree the strata still to come are running in.
  [ "${BENCH_RUN_WORKTREE_OWNER:-}" = "$$" ] || return "$status"
  if [ "$status" -ne 0 ]; then
    echo "→ run worktree kept for diagnosis: $BENCH_RUN_WORKTREE" >&2
    return "$status"
  fi
  git -C "$MANAGED_REPO" worktree remove --force "$BENCH_RUN_WORKTREE" >/dev/null 2>&1 ||
    rm -rf "$BENCH_RUN_WORKTREE"
  git -C "$MANAGED_REPO" worktree prune >/dev/null 2>&1 || true
  BENCH_RUN_WORKTREE=""
  return "$status"
}

release_run_scope() {
  local status="$?"
  release_run_worktree "$status" || true
  release_target_repo_lock
  return "$status"
}

# --isolated trades one checkout's disk and one index scan for the ability to run two studies at once.
# The private worktree keeps its own path in its own index's scan_root, so Codemap's root-mismatch
# guard stays armed and the graph is still verified — by semantic identity, which is path-independent,
# rather than by the byte hash that only reproduces at the canonical clone.
prepare_run_worktree() {
  local run_token stamp
  # The re-exec into a paid run's frozen launcher carries --isolated with it. Adopting the inherited
  # tree keeps one worktree per run: a second one would hold a second copy of the relocated index and
  # leave the first behind, since only its creator removes it.
  if [ -n "$BENCH_RUN_WORKTREE" ] && [ -d "$BENCH_RUN_WORKTREE" ]; then
    REPO="$BENCH_RUN_WORKTREE"
    INDEX_PATH="$REPO/.cache/codemap/$(basename "$REPO").json"
    echo "→ run worktree (inherited): $REPO"
    return 0
  fi
  ensure_managed_clone || return "$?"
  git -C "$MANAGED_REPO" worktree prune >/dev/null 2>&1 || true
  run_token="${CLAUDE_CODE_SESSION_ID:-$$}"
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  BENCH_RUN_WORKTREE="$BENCHMARK_TEMP_ROOT/codemap-parity-run-${stamp}-${run_token##*-}"
  section_rule "PREPARE private run worktree"
  if ! git -C "$MANAGED_REPO" worktree add --detach --force "$BENCH_RUN_WORKTREE" "$PL_TAG" >/dev/null; then
    echo "ERROR: cannot create the isolated run worktree at $BENCH_RUN_WORKTREE" >&2
    BENCH_RUN_WORKTREE=""
    return 1
  fi
  REPO="$BENCH_RUN_WORKTREE"
  INDEX_PATH="$REPO/.cache/codemap/$(basename "$REPO").json"
  BENCH_RUN_WORKTREE_OWNER="$$"
  # Child studies re-enter this script, so they read the tree and its index from the environment
  # instead of re-deriving the managed clone and refusing the relocated index they were handed.
  export REPO INDEX_PATH BENCH_RUN_WORKTREE BENCH_RUN_WORKTREE_OWNER
  echo "→ run worktree: $REPO (removed on success, kept on failure)"
  relocate_locked_index_into_run_worktree
}

# Scanning the worktree would build a second graph with no provenance link to the locked one, and
# every admission gate would refuse it. The frozen bytes are copied instead and only scan_root moves,
# which is the relocation the executable stages already perform for their disposable checkouts: the
# graph stays the reviewed one, the worktree keeps its own root, and the provenance the gates check
# in place of the byte hash travels with the run.
relocate_locked_index_into_run_worktree() {
  local frozen_index
  frozen_index="$MANAGED_REPO/.cache/codemap/$(basename "$MANAGED_REPO").json"
  if [ ! -f "$frozen_index" ]; then
    echo "ERROR: --isolated relocates the locked index, which the managed clone does not have yet." >&2
    echo "       Run once without --isolated to build it: bash benchmarks/run-all.sh smoke" >&2
    return 1
  fi
  BENCH_RUN_INDEX_RELOCATION="$BENCH_RUN_WORKTREE/.cache/codemap/index-relocation.json"
  if ! python3 "$INDEX_PREPARER" \
    --index-path "$frozen_index" \
    --relocate-into "$BENCH_RUN_WORKTREE" \
    --provenance-path "$BENCH_RUN_INDEX_RELOCATION" \
    --manifest-path "$METHODOLOGY_PATH" \
    --schema-path "$SCHEMA_PATH" >/dev/null; then
    echo "ERROR: cannot relocate the locked index into $BENCH_RUN_WORKTREE" >&2
    BENCH_RUN_INDEX_RELOCATION=""
    return 1
  fi
  export BENCH_RUN_INDEX_RELOCATION
  echo "→ relocated locked index: $INDEX_PATH"
}

acquire_target_repo_lock() {
  # Every study stages mutations in the one shared clone — diff-impact edits, patch checkouts, index
  # rebuilds — and each verifies a clean worktree before it trusts a cell. A second study started
  # while the first holds the clone therefore fails mid-run on the other study's staged edit, after
  # its cells have already been paid for. The lock is deliberately machine-wide rather than
  # session-scoped: mutual exclusion across concurrent sessions is the whole point.
  local key owner_line owner_pid owner_label
  if [ "${BENCH_TARGET_LOCK_OWNED:-}" = "$$" ]; then
    # Same process after a launcher-snapshot exec: the lock still stands, only the trap is gone.
    trap release_run_scope EXIT
    return 0
  fi
  if [ -n "${BENCH_TARGET_LOCK_OWNED:-}" ]; then
    # A child study of an outer run; the outer process owns the clone and releases the lock.
    BENCH_TARGET_LOCK_DIR=""
    return 0
  fi
  key="$(sha256_string "$REPO" | cut -c1-16)"
  BENCH_TARGET_LOCK_DIR="${TMPDIR:-/tmp}/codemap-bench-target-${key}.lock"
  if ! mkdir "$BENCH_TARGET_LOCK_DIR" 2>/dev/null; then
    owner_line="unknown study"
    IFS= read -r owner_line < "$BENCH_TARGET_LOCK_DIR/owner" 2>/dev/null || owner_line="unknown study"
    owner_pid="${owner_line%% *}"
    owner_label="${owner_line#* }"
    if [ -n "$owner_pid" ] && kill -0 "$owner_pid" 2>/dev/null; then
      echo "ERROR: '$owner_label' (pid $owner_pid) is already benchmarking $REPO" >&2
      echo "       Two studies sharing one clone corrupt each other's worktree and waste paid cells." >&2
      echo "       Wait for that run to finish, or point this one at a separate clone via REPO=." >&2
      BENCH_TARGET_LOCK_DIR=""
      exit 2
    fi
    echo "note: clearing stale target lock left by pid ${owner_pid:-unknown}"
    rm -rf "$BENCH_TARGET_LOCK_DIR"
    mkdir "$BENCH_TARGET_LOCK_DIR" || {
      echo "ERROR: cannot create target lock $BENCH_TARGET_LOCK_DIR" >&2
      BENCH_TARGET_LOCK_DIR=""
      exit 2
    }
  fi
  printf '%s %s\n' "$$" "${1:-benchmark study}" > "$BENCH_TARGET_LOCK_DIR/owner"
  BENCH_TARGET_LOCK_OWNED="$$"
  export BENCH_TARGET_LOCK_DIR BENCH_TARGET_LOCK_OWNED
  trap release_run_scope EXIT
}

codex_combined_scope_sha() {
  # One combined run prices two scopes, so its approval binds both: the unified
  # structural scope the structural child re-derives from its own no-model plan,
  # and the agentic approval the agentic child expects.
  sha256_string "$1"$'\n'"$2"$'\n'
}

archive_paid_source() {
  local source_root="$1"
  mkdir -p "$source_root"
  (
    cd "$ROOT"
    COPYFILE_DISABLE=1 tar \
      --exclude='benchmarks/results' \
      --exclude='benchmarks/results/*' \
      --exclude='*/.git' \
      --exclude='*/.git/*' \
      --exclude='*/.cache' \
      --exclude='*/.cache/*' \
      --exclude='*/__pycache__' \
      --exclude='*/__pycache__/*' \
      --exclude='*/.pytest_cache' \
      --exclude='*/.pytest_cache/*' \
      --exclude='*/.ruff_cache' \
      --exclude='*/.ruff_cache/*' \
      --exclude='*/.mypy_cache' \
      --exclude='*/.mypy_cache/*' \
      --exclude='*.pyc' \
      --exclude='*.pyo' \
      -cf - \
      benchmarks \
      plugins/codemap-py \
      plugins/codex-rig \
      .agents/plugins/marketplace.json
  ) | (
    cd "$source_root"
    COPYFILE_DISABLE=1 tar -xf -
  )
}

capture_codemap_mode_map() {
  local output_path="$1"
  python3 -c \
    'import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); from _probe_runtime import write_real_mode_map; write_real_mode_map(Path(sys.argv[2]), Path(sys.argv[3]))' \
    "$ROOT/plugins/codemap-py/scripts" \
    "$ROOT" \
    "$output_path"
}

build_source_checksum_manifest() {
  local source_root="$1"
  local output_path="$2"
  local source_symlink listing
  source_symlink="$(find "$source_root" -type l -print -quit)"
  if [ -n "$source_symlink" ]; then
    echo "ERROR: paid Codex source snapshot contains a symlink: $source_symlink" >&2
    return 2
  fi
  resolve_sha256_cmd || return 1
  # One hasher invocation per xargs batch instead of three processes per file. The
  # snapshot holds ~420 files, and the per-file form spent ~9 s of a 16 s run purely
  # on fork/exec. `shasum -a 256` and `sha256sum` both emit "<sha>  <path>", which is
  # the exact line this used to printf, so the manifest stays byte-identical.
  listing="$(mktemp)"
  ( cd "$source_root" && find . -type f -print | LC_ALL=C sort | sed 's|^\./||' ) > "$listing"
  : > "$output_path"
  # An empty listing must not reach xargs: with no operands the hasher reads stdin
  # and the run hangs instead of writing an empty manifest.
  if [ -s "$listing" ]; then
    ( cd "$source_root" && tr '\n' '\0' < "$listing" | xargs -0 "${SHA256_CMD[@]}" ) > "$output_path"
  fi
  rm -f "$listing"
}

validate_paid_source_snapshot() {
  local expected_source="$CODEX_RUN_DIR/.launcher/source"
  local source_manifest="$CODEX_RUN_DIR/.launcher/source.sha256"
  local actual_manifest
  if [ "$ROOT" != "$expected_source" ] || [ "${CODEX_LAUNCHER_ROOT:-}" != "$expected_source" ]; then
    echo "ERROR: paid Codex mode is not using its run-scoped source snapshot." >&2
    exit 2
  fi
  if [ ! -d "$expected_source" ] || [ ! -f "$source_manifest" ]; then
    echo "ERROR: paid Codex source snapshot is incomplete." >&2
    exit 2
  fi
  if [ ! -f "$expected_source/benchmarks/manifests/codemap-package-mode-map.json" ]; then
    echo "ERROR: paid Codex source snapshot lacks the Codemap package mode map." >&2
    exit 2
  fi
  if [ "$(sha256_file "$source_manifest")" != "${CODEX_SOURCE_MANIFEST_SHA256:-}" ]; then
    echo "ERROR: paid Codex source checksum manifest changed before execution." >&2
    exit 2
  fi
  if ! actual_manifest="$(mktemp "$BENCHMARK_TEMP_ROOT/codex-source-checksums.XXXXXX")"; then
    echo "ERROR: failed to create the paid Codex source validation manifest." >&2
    exit 2
  fi
  build_source_checksum_manifest "$expected_source" "$actual_manifest"
  if ! cmp -s "$source_manifest" "$actual_manifest"; then
    rm -f "$actual_manifest"
    echo "ERROR: paid Codex source snapshot changed before execution." >&2
    exit 2
  fi
  rm -f "$actual_manifest"
}

append_launcher_checksum_attestation() {
  local checksum_path="$1"
  local launcher_artifact
  for launcher_artifact in "$CODEX_RUN_DIR/.launcher/run-all.sh" "$CODEX_RUN_DIR/.launcher/source.sha256"; do
    if [ -f "$launcher_artifact" ]; then
      shasum -a 256 "$launcher_artifact" >> "$checksum_path"
    fi
  done
}

write_codex_result_checksums() {
  local checksum_path="$1"
  local artifact input_artifact benchmark_artifact
  : > "$checksum_path"
  for artifact in run.log telemetry.jsonl telemetry-canonical.jsonl run-metadata.json runtime-isolation.jsonl; do
    if [ -f "$CODEX_RUN_DIR/$artifact" ]; then
      shasum -a 256 "$CODEX_RUN_DIR/$artifact" >> "$checksum_path"
    fi
  done
  if [ -d "$CODEX_RUN_DIR/inputs" ]; then
    while IFS= read -r input_artifact; do
      shasum -a 256 "$input_artifact" >> "$checksum_path"
    done < <(find "$CODEX_RUN_DIR/inputs" -type f -print | LC_ALL=C sort)
  fi
  if [ -d "$CODEX_RUN_DIR/benchmark" ]; then
    while IFS= read -r benchmark_artifact; do
      shasum -a 256 "$benchmark_artifact" >> "$checksum_path"
    done < <(find "$CODEX_RUN_DIR/benchmark" -type f -print | LC_ALL=C sort)
  fi
  append_launcher_checksum_attestation "$checksum_path"
}

# Which declared stratum the Codex agentic lane runs, canonicalized the way the runner canonicalizes
# it: naming the manifest's own default is not an override, so it leaves this empty and the run keeps
# the identity and the token the manifest digest already authorizes.
# The stratum the active agentic manifest names as its own default.
agentic_manifest_model() {
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["model"]["name"])' "$AGENTIC_MANIFEST_PATH"
}

resolve_agentic_model() {
  local default_model
  [ "$AGENTIC_MODELS_RESOLVED" = true ] && return 0
  AGENTIC_MODEL=""
  AGENTIC_SELECTED_MODELS=()
  AGENTIC_MODELS_RESOLVED=true
  if [ -n "$MODELS_SELECTION" ]; then
    resolve_provider_models "$MODE" || return "$?"
    AGENTIC_SELECTED_MODELS=("${PROVIDER_MODELS[@]}")
  elif [ "$MODE" = "codex" ]; then
    AGENTIC_SELECTED_MODELS=("$(agentic_manifest_model)")
  else
    return 0
  fi
  if [ "$MODE" = "codex" ] && [ "${#AGENTIC_SELECTED_MODELS[@]}" -eq 1 ]; then
    default_model="$(agentic_manifest_model)" || {
      echo "ERROR: active Codex agentic manifest has no model stratum: $AGENTIC_MANIFEST_PATH" >&2
      return 2
    }
    [ "${AGENTIC_SELECTED_MODELS[0]}" = "$default_model" ] || AGENTIC_MODEL="${AGENTIC_SELECTED_MODELS[0]}"
  elif [ "${#AGENTIC_SELECTED_MODELS[@]}" -eq 1 ]; then
    AGENTIC_MODEL="${AGENTIC_SELECTED_MODELS[0]}"
  fi
}

agentic_selection_is_sweep() {
  [ "${#AGENTIC_SELECTED_MODELS[@]}" -gt 1 ]
}

select_agentic_model() {
  AGENTIC_MODEL="$1"
  if [ "$MODE" = "codex" ] && [ "$AGENTIC_MODEL" = "$(agentic_manifest_model)" ]; then
    AGENTIC_MODEL=""
  fi
}

# One rule, four readers: the dispatch that forwards a derived scope hash, the approval hint, the
# paid-input gate, and the authorization block's wording. Separate copies of this disjunction would
# drift, and the first symptom of a drifted copy is a refusal at paid launch.
agentic_scope_is_default() {
  [ -z "$CODEX_TASKS" ] && [ "$AGENTIC_REPETITIONS" = "1" ] && [ -z "$AGENTIC_MODEL" ]
}

resolve_agentic_scope() {
  local scope_json
  local -a resolver
  AGENTIC_TASK_IDS=()
  resolve_agentic_model || return "$?"
  if [ "$MODE" = "claude" ]; then
    resolver=(
      python3 "$ROOT/benchmarks/run-claude-agentic.py"
      --manifest-path "$METHODOLOGY_PATH"
      --repeat "$AGENTIC_REPETITIONS"
      --resolve-scope
    )
    if [ -n "$CODEX_TASKS" ]; then
      resolver+=(--tasks "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1].split(",")))' "$CODEX_TASKS")")
    fi
    if [ -n "$AGENTIC_MODEL" ]; then
      resolver+=(--model "$AGENTIC_MODEL")
    fi
  else
    resolver=(
      python3 "$ROOT/benchmarks/run-codex-agentic.py"
      --manifest-path "$AGENTIC_MANIFEST_PATH"
      --repetitions "$AGENTIC_REPETITIONS"
      --resolve-scope
    )
    if [ -n "$CODEX_TASKS" ]; then
      resolver+=(--task-id "$CODEX_TASKS")
    fi
    if [ -n "$AGENTIC_MODEL" ]; then
      resolver+=(--model "$AGENTIC_MODEL")
    fi
  fi
  if ! scope_json="$("${resolver[@]}" 2>&1)"; then
    echo "ERROR: invalid $MODE agentic scope:" >&2
    echo "$scope_json" >&2
    return 2
  fi
  # Three fields out of one blob in one interpreter start, not three.
  {
    IFS= read -r AGENTIC_SCOPE_SHA
    IFS= read -r AGENTIC_TOTAL_CELLS
    IFS= read -r AGENTIC_COORDINATE_TIMEOUT
  } < <(python3 -c 'import json,sys; d=json.loads(sys.stdin.read()); print(d["scope_sha256"]); print(d["total_cells"]); print(d["coordinate_timeout_seconds"])' <<<"$scope_json")
  while IFS= read -r task_id; do
    [ -n "$task_id" ] && AGENTIC_TASK_IDS+=("$task_id")
  done < <(python3 -c 'import json,sys; print(*json.loads(sys.stdin.read())["task_ids"], sep="\n")' <<<"$scope_json")
  if [ "${#AGENTIC_TASK_IDS[@]}" -eq 0 ]; then
    echo "ERROR: $MODE agentic scope resolver returned no task IDs." >&2
    return 2
  fi
}

configure_agentic_dispatch() {
  # Default suites are manifest-defined; only selected or repeated runs need a
  # resolver-derived identity forwarded to prevent a scope mismatch.
  if [ "$MODE" = "claude" ]; then
    AGENTIC_RUNNER="$ROOT/benchmarks/run-claude-agentic.py"
    AGENTIC_DISPATCH_ARGS=(
      --repo-path "$REPO"
      --manifest-path "$METHODOLOGY_PATH"
    )
    if [ -n "$CODEX_TASKS" ]; then
      AGENTIC_DISPATCH_ARGS+=(--tasks "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1].split(",")))' "$CODEX_TASKS")")
    else
      AGENTIC_DISPATCH_ARGS+=(--run-all)
    fi
    if [ -n "$AGENTIC_MODEL" ]; then
      AGENTIC_DISPATCH_ARGS+=(--model "$AGENTIC_MODEL")
    fi
    AGENTIC_DISPATCH_ARGS+=(--repeat "$AGENTIC_REPETITIONS")
  else
    AGENTIC_RUNNER="$ROOT/benchmarks/run-codex-agentic.py"
    AGENTIC_DISPATCH_ARGS=(
      --repo-path "$REPO"
      --index-path "$INDEX_PATH"
      --marketplace-root "$ROOT"
      --codemap-bin "$CODEMAP_BIN"
      --manifest-path "$AGENTIC_MANIFEST_PATH"
    )
    if [ -n "$CODEX_TASKS" ]; then
      AGENTIC_DISPATCH_ARGS+=(--task-id "$CODEX_TASKS")
    fi
    if [ -n "$AGENTIC_MODEL" ]; then
      AGENTIC_DISPATCH_ARGS+=(--model "$AGENTIC_MODEL")
    fi
    AGENTIC_DISPATCH_ARGS+=(--repetitions "$AGENTIC_REPETITIONS")
  fi
  if ! agentic_scope_is_default; then
    AGENTIC_DISPATCH_ARGS+=(--scope-sha256 "$AGENTIC_SCOPE_SHA")
  fi
  set_index_relocation_args
  # The guarded expansion keeps an empty optional array safe under macOS Bash 3.2 with `set -u`.
  AGENTIC_DISPATCH_ARGS+=(${INDEX_RELOCATION_ARGS[@]+"${INDEX_RELOCATION_ARGS[@]}"})
}

# Every lane re-checks the index it was handed, so an isolated run has to hand each of them the
# provenance that replaces the byte hash — not the Codex structural lane alone. Bash 3.2 has no
# namerefs, so this fills one global that callers expand into their own command line.
set_index_relocation_args() {
  INDEX_RELOCATION_ARGS=()
  if [ -n "${BENCH_RUN_INDEX_RELOCATION:-}" ]; then
    INDEX_RELOCATION_ARGS=(--index-relocation-path "$BENCH_RUN_INDEX_RELOCATION")
  fi
}

load_index_contract() {
  local manifest_path="$1"
  local methodology_path="${2:-}"
  local contract_json
  local -a methodology_args=()
  if [ -n "$methodology_path" ]; then
    methodology_args=(--methodology-path "$methodology_path")
  fi
  # The guarded expansion keeps an empty optional array safe under macOS Bash 3.2 with `set -u`.
  if ! contract_json="$(python3 "$INDEX_PREPARER" \
    --manifest-path "$manifest_path" \
    ${methodology_args[@]+"${methodology_args[@]}"} \
    --schema-path "$SCHEMA_PATH" \
    --print-contract 2>&1)"; then
    echo "ERROR: active index contract validation failed:" >&2
    echo "$contract_json" >&2
    exit 1
  fi
  LOCKED_INDEX_SHA="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read())["raw_sha256"])' <<<"$contract_json")"
  LOCKED_INDEX_SCAN_VERSION="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read())["scan_version"])' <<<"$contract_json")"
  echo "→ index contract: scan_version=$LOCKED_INDEX_SCAN_VERSION sha256=$LOCKED_INDEX_SHA"
}

verify_current_index() {
  local manifest_path="$1"
  local methodology_path="${2:-}"
  local verification
  local -a methodology_args=()
  # The byte hash reproduces only at the canonical clone, whose path the locked index records.
  # Demanding it elsewhere would reject every correct index a private worktree builds.
  local -a hash_args=()
  if [ "$REPO" = "$MANAGED_REPO" ]; then
    hash_args=(--require-hash)
  fi
  # A relocated index moves scan_root and nothing else, so the module paths it records still name the
  # canonical clone. The semantic digest strips whichever root it is given, and stripping the
  # worktree root would leave those canonical paths in the hash — the graph must therefore be hashed
  # against the root it describes, not the root it currently sits in.
  local -a semantic_root_args=()
  if [ -n "${BENCH_RUN_INDEX_RELOCATION:-}" ]; then
    semantic_root_args=(--source-root "$MANAGED_REPO")
  fi
  if [ -n "$methodology_path" ]; then
    methodology_args=(--methodology-path "$methodology_path")
  fi
  if verification="$(python3 "$INDEX_PREPARER" \
    --index-path "$INDEX_PATH" \
    --manifest-path "$manifest_path" \
    ${methodology_args[@]+"${methodology_args[@]}"} \
    --schema-path "$SCHEMA_PATH" \
    ${hash_args[@]+"${hash_args[@]}"} \
    ${semantic_root_args[@]+"${semantic_root_args[@]}"} \
    --verify 2>&1)"; then
    echo "$verification"
    return 0
  fi
  echo "⚠ existing index failed the active contract; rebuilding: $verification" >&2
  return 1
}

# The managed clone is the object store every isolated worktree branches from, so it has to exist
# before a worktree is added, which happens earlier than the first ensure_repo call.
ensure_managed_clone() {
  if [ ! -d "$MANAGED_REPO/.git" ]; then
    section_rule "clone pytorch-lightning @ $PL_TAG into .sandbox"
    rm -rf "$MANAGED_REPO"
    git clone --depth 1 --branch "$PL_TAG" "$PL_URL" "$MANAGED_REPO"
  fi
}

ensure_repo() {
  # Only the canonical benchmark target is reset. An overridden REPO is never mutated.
  if [ "$REPO" = "$MANAGED_REPO" ]; then
    ensure_managed_clone
    section_rule "reset .sandbox clone to $PL_TAG (pristine baseline)"
    if ! git -C "$REPO" reset --hard "$PL_TAG"; then
      echo "ERROR: managed benchmark clone cannot reset to $PL_TAG; recreate $REPO before retrying." >&2
      return 1
    fi
    if ! git -C "$REPO" clean -fd; then
      echo "ERROR: managed benchmark clone cannot remove untracked files." >&2
      return 1
    fi
  elif ! git -C "$REPO" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "ERROR: \$REPO ($REPO) is not a Git worktree at tag $PL_TAG." >&2
    exit 1
  fi
  echo "→ target repo: $REPO ($(git -C "$REPO" rev-parse --short HEAD))"
}

resolve_codemap_python() {
  local candidate resolved
  local -a candidates=()
  if [ -n "${CODEMAP_PYTHON:-}" ]; then
    candidates+=("$CODEMAP_PYTHON")
  fi
  for candidate in python3.11 python3 python; do
    if resolved="$(command -v "$candidate" 2>/dev/null)"; then
      candidates+=("$resolved")
    fi
  done
  for candidate in "${candidates[@]}"; do
    if [ -x "$candidate" ] && "$candidate" -c 'import sys; raise SystemExit(sys.version_info[:2] != (3, 11))' >/dev/null 2>&1; then
      "$candidate" -c 'import os,sys; print(os.path.realpath(sys.executable))'
      return 0
    fi
  done
  echo "ERROR: Codemap benchmark preparation requires an executable Python 3.11; set CODEMAP_PYTHON or add python3.11 to PATH." >&2
  return 2
}

set_default_codex_run_dir() {
  local prefix="codex-combined"
  if [ -n "${CODEX_RUN_DIR:-}" ]; then
    if [[ "$CODEX_RUN_DIR" != /* ]]; then
      CODEX_RUN_DIR="$ROOT/$CODEX_RUN_DIR"
      export CODEX_RUN_DIR
    fi
    return 0
  fi
  if [ "$AGENTIC" = true ]; then
    prefix="codex-agentic"
  elif [ "$STRUCTURAL" = true ]; then
    prefix="codex-integration"
  fi
  if [ -n "$CODEX_TASKS" ]; then
    prefix="$prefix-selected"
  fi
  CODEX_RUN_DIR="$CODEX_RESULTS_ROOT/$prefix-$(date -u +%Y%m%dT%H%M%SZ)"
  if [[ "$CODEX_RUN_DIR" != /* ]]; then
    CODEX_RUN_DIR="$ROOT/$CODEX_RUN_DIR"
  fi
  export CODEX_RUN_DIR
}

prepare_index_inputs() {
  local manifest_path="$1"
  local methodology_path="${2:-}"
  local -a methodology_args=()
  if [ -n "$methodology_path" ]; then
    methodology_args=(--methodology-path "$methodology_path")
  fi
  load_index_contract "$manifest_path" "$methodology_path" || return "$?"
  ensure_repo || return "$?"
  section_rule "PREPARE frozen parity index"
  # A relocated index is already the locked graph, and its provenance commits to the exact bytes on
  # disk. Rebuilding or renormalizing it here would produce different bytes and invalidate the very
  # provenance the runners check, so this run only verifies what the relocation installed.
  if [ -n "${BENCH_RUN_INDEX_RELOCATION:-}" ]; then
    if ! verify_current_index "$manifest_path" "$methodology_path"; then
      echo "ERROR: relocated index failed the active schema contract (scan_version=$LOCKED_INDEX_SCAN_VERSION)" >&2
      exit 1
    fi
    echo "→ relocated index: $INDEX_PATH ($(sha256_file "$INDEX_PATH"))"
    echo "→ relocation provenance: $BENCH_RUN_INDEX_RELOCATION"
    return 0
  fi
  if [ ! -f "$INDEX_PATH" ] || ! verify_current_index "$manifest_path" "$methodology_path"; then
    if [ -f "$INDEX_PATH" ]; then
      echo "→ existing index is stale or schema-incompatible; rebuild from the locked target"
    else
      echo "→ build missing index from the locked target"
    fi
    codemap_python="$(resolve_codemap_python)"
    CODEMAP_PYTHON="$codemap_python" "$CODEMAP_BIN" index --root "$REPO"
  fi
  index_sha="$(sha256_file "$INDEX_PATH")"
  if [ "$index_sha" != "$LOCKED_INDEX_SHA" ]; then
    python3 "$INDEX_PREPARER" \
      --index-path "$INDEX_PATH" \
      --source-root "$REPO" \
      --manifest-path "$manifest_path" \
      ${methodology_args[@]+"${methodology_args[@]}"} \
      --schema-path "$SCHEMA_PATH"
  fi
  if ! verify_current_index "$manifest_path" "$methodology_path"; then
    echo "ERROR: rebuilt index failed the active schema contract (scan_version=$LOCKED_INDEX_SCAN_VERSION)" >&2
    exit 1
  fi
  index_sha="$(sha256_file "$INDEX_PATH")"
  # The byte lock records the canonical clone's own path inside the index, so it can only be checked
  # there. Off that path the graph is still verified — semantically, by the same path-independent
  # digest the patch stage uses — and the skipped byte check is named rather than silently dropped.
  if [ "$REPO" = "$MANAGED_REPO" ]; then
    if [ "$index_sha" != "$LOCKED_INDEX_SHA" ]; then
      echo "ERROR: locked parity index SHA-256 mismatch: expected $LOCKED_INDEX_SHA, got $index_sha" >&2
      exit 1
    fi
    echo "→ locked index: $INDEX_PATH ($index_sha)"
  else
    echo "⚠ raw byte-identity check skipped: $REPO is not the canonical root $MANAGED_REPO;" >&2
    echo "  only semantic graph identity was verified for this run's index." >&2
    echo "→ verified index: $INDEX_PATH ($index_sha)"
  fi
}

prepare_patch_index_inputs() {
  local patch_locks="$ROOT/benchmarks/suites/patch-index-locks.json"
  local -a patch_commits=(
    8e805f9268043c9aa8f0d70800be537b56a93c19
    aa0ee0d18d49c6b26f18d34a3473f177adefc262
    9df1910f0833100478886bef0a08b450ff2d0c14
    3876cc525d2678463199407ca48230d3eba09461
    b15d394f2a9d36ccefba08328ac8dc2bd13e49b2
  )
  section_rule "PREPARE frozen historical patch indexes"
  for commit in "${patch_commits[@]}"; do
    if ! git -C "$REPO" cat-file -e "$commit^{commit}" 2>/dev/null; then
      echo "→ fetch missing patch baseline $commit"
      git -C "$REPO" fetch origin "$commit" || return "$?"
    fi
  done
  if ! python3 "$INDEX_PREPARER" \
    --prepare-patch-bundle \
    --source-root "$REPO" \
    --patch-locks-path "$patch_locks" \
    --scan-index-bin "$ROOT/plugins/codemap-py/bin/scan-index"; then
    echo "ERROR: historical patch indexes could not be prepared. Ensure the exact PT baseline commits are present, then rerun this no-model command." >&2
    echo "git -C $REPO fetch origin 8e805f9268043c9aa8f0d70800be537b56a93c19 aa0ee0d18d49c6b26f18d34a3473f177adefc262 9df1910f0833100478886bef0a08b450ff2d0c14 3876cc525d2678463199407ca48230d3eba09461 b15d394f2a9d36ccefba08328ac8dc2bd13e49b2" >&2
    echo "python3 benchmarks/prepare-codex-index.py --prepare-patch-bundle --source-root $REPO --patch-locks-path $patch_locks --scan-index-bin $ROOT/plugins/codemap-py/bin/scan-index" >&2
    return 1
  fi
}

patch_bundle_required() {
  # Historical indexes are only consumed by the PT stage. The unqualified
  # Codex task study includes every stage, while selected structural scopes
  # expose their resolved IDs before input preparation.
  if [ "$MODE" != "codex" ] || [ "$AGENTIC" = true ]; then
    return 1
  fi
  if [ -z "$CODEX_TASKS" ]; then
    return 0
  fi
  local task_id
  for task_id in "${CODEX_SELECTION_TASK_IDS[@]}"; do
    case "$task_id" in
      PT-*) return 0 ;;
    esac
  done
  return 1
}

prepare_patch_test_env() {
  # The PT behavior oracle runs Lightning's own tests against the disposable
  # checkout's `src`, so it needs an interpreter carrying Lightning's runtime and
  # test dependencies. It lives beside the managed clone rather than under $ROOT so
  # a paid launcher's frozen source snapshot and its checksum ledger stay unchanged.
  local venv_root="${CODEMAP_BENCH_PATCH_VENV:-$BENCHMARK_TEMP_ROOT/codemap-bench-patch-venv}"
  local venv_python="$venv_root/bin/python"
  local venv_pytest="$venv_root/bin/pytest"
  local requirements="$REPO/requirements"
  if [ -n "${CODEMAP_BENCH_PATCH_PYTEST:-}" ]; then
    if [ ! -x "$CODEMAP_BENCH_PATCH_PYTEST" ]; then
      echo "ERROR: CODEMAP_BENCH_PATCH_PYTEST is not an executable pytest: $CODEMAP_BENCH_PATCH_PYTEST" >&2
      return 1
    fi
    echo "→ patch test runtime: $CODEMAP_BENCH_PATCH_PYTEST (caller supplied)"
    return 0
  fi
  section_rule "PREPARE patch-stage test runtime"
  if [ -x "$venv_pytest" ] && "$venv_python" -c "import torch" >/dev/null 2>&1; then
    echo "→ reusing $venv_root"
  else
    local -a requirement_files=(
      "$requirements/pytorch/base.txt"
      "$requirements/pytorch/test.txt"
      "$requirements/fabric/base.txt"
      "$requirements/fabric/test.txt"
    )
    local requirement_file
    for requirement_file in "${requirement_files[@]}"; do
      if [ ! -f "$requirement_file" ]; then
        echo "ERROR: patch test requirements are missing from the target clone: $requirement_file" >&2
        return 1
      fi
    done
    local -a install_arguments=()
    for requirement_file in "${requirement_files[@]}"; do
      install_arguments+=(-r "$requirement_file")
    done
    echo "→ building $venv_root (first run downloads torch and Lightning test dependencies)"
    if command -v uv >/dev/null 2>&1; then
      uv venv --python 3.12 "$venv_root" >/dev/null || return "$?"
      uv pip install --quiet --python "$venv_python" "${install_arguments[@]}" || return "$?"
    else
      python3 -m venv "$venv_root" || return "$?"
      "$venv_python" -m pip install --quiet --upgrade pip || return "$?"
      "$venv_python" -m pip install --quiet "${install_arguments[@]}" || return "$?"
    fi
  fi
  if [ ! -x "$venv_pytest" ]; then
    echo "ERROR: patch test runtime has no pytest launcher: $venv_pytest" >&2
    return 1
  fi
  export CODEMAP_BENCH_PATCH_PYTEST="$venv_pytest"
  echo "→ patch test runtime: $CODEMAP_BENCH_PATCH_PYTEST"
}

prepare_locked_inputs() {
  load_shared_structural_tasks || return "$?"
  prepare_index_inputs "$MANIFEST_PATH" "$METHODOLOGY_PATH"
  if patch_bundle_required; then
    prepare_patch_index_inputs
    prepare_patch_test_env
  fi
}

prepare_claude_inputs() {
  load_shared_structural_tasks || return "$?"
  prepare_index_inputs "$METHODOLOGY_PATH"
}

load_shared_structural_tasks() {
  SHARED_STRUCTURAL_TASK_IDS=()
  while IFS= read -r task_id; do
    [ -n "$task_id" ] && SHARED_STRUCTURAL_TASK_IDS+=("$task_id")
  done < <(
    python3 -c \
      'import json,sys; print(*json.load(open(sys.argv[1]))["preregistered_cells"]["structural_execution_task_ids"], sep="\n")' \
      "$METHODOLOGY_PATH"
  )
  if [ "${#SHARED_STRUCTURAL_TASK_IDS[@]}" -eq 0 ]; then
    echo "ERROR: provider-neutral methodology defines no structural execution tasks." >&2
    return 2
  fi
}

# Resolve which declared model strata this invocation runs. --models never introduces a model:
# it restricts and orders the provider's declared list, so a typo fails instead of silently running
# an unlocked stratum. Launcher defaults are assigned before dispatch; each named stratum runs as
# its own study under one approval that binds every resolved scope and the ordered list.
configure_codex_model() {
  local primary
  primary="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["model"]["name"])' "$MANIFEST_PATH")"
  CODEX_SELECTED_MODELS=("$primary")
  if [ -n "$MODELS_SELECTION" ]; then
    resolve_provider_models codex
    CODEX_SELECTED_MODELS=("${PROVIDER_MODELS[@]}")
  fi
  CODEX_SELECTED_MODEL="${CODEX_SELECTED_MODELS[0]}"
}

# A multi-stratum approval binds the ordered model list as well as the scope, so a token minted for
# one pair of strata cannot authorize a different pair or a longer list at the same scope.
codex_models_scope_sha() {
  sha256_string "$1"$'\n'"$2"$'\n'
}

# A stratum may be named in full ("gpt-5.6-terra") or by its nickname — the segment after the last
# dash ("terra"). A nickname resolves only when exactly one declared stratum carries it, so an
# ambiguous short name fails rather than choosing a stratum on the operator's behalf. Every selection
# is canonicalized to the declared full name before it reaches a run directory or an approval hash,
# so the two spellings are the same authorization rather than two.
canonical_provider_model() {
  local name="$1" declared="$2" provider="$3" candidate
  local -a matches=()
  for candidate in $declared; do
    if [ "$candidate" = "$name" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  for candidate in $declared; do
    [ "${candidate##*-}" = "$name" ] && matches+=("$candidate")
  done
  if [ "${#matches[@]}" -eq 1 ]; then
    printf '%s\n' "${matches[0]}"
    return 0
  fi
  if [ "${#matches[@]}" -gt 1 ]; then
    echo "ERROR: --models: '$name' is a nickname for more than one declared $provider stratum (${matches[*]}); name it in full." >&2
    return 1
  fi
  echo "ERROR: --models: '$name' is not a declared $provider stratum (declared: $declared)" >&2
  return 1
}

resolve_provider_models() {
  local provider="$1" declared name canonical seen
  declared="$(python3 -c 'import json,sys; print(" ".join(json.load(open(sys.argv[1]))["agentic_execution_contract"]["models_by_provider"][sys.argv[2]]))' "$METHODOLOGY_PATH" "$provider")" || {
    echo "ERROR: cannot read the declared $provider model strata from $METHODOLOGY_PATH" >&2
    exit 2
  }
  PROVIDER_MODELS=()
  if [ -z "$MODELS_SELECTION" ]; then
    read -r -a PROVIDER_MODELS <<< "$declared"
    return 0
  fi
  seen=""
  while IFS= read -r name; do
    [ -z "$name" ] && continue
    canonical="$(canonical_provider_model "$name" "$declared" "$provider")" || exit 2
    case " $seen " in
      *" $canonical "*)
        echo "ERROR: --models: '$canonical' is selected more than once." >&2
        exit 2
        ;;
    esac
    seen="$seen $canonical"
    PROVIDER_MODELS+=("$canonical")
  done < <(printf '%s\n' "$MODELS_SELECTION" | tr ',' '\n')
  if [ "${#PROVIDER_MODELS[@]}" -eq 0 ]; then
    echo "ERROR: --models selected no model." >&2
    exit 2
  fi
}

query_check() {
  section_rule "QUERY (no model)"
  python3 "$ROOT/benchmarks/run-codemap-cli.py" --repo-path "$REPO"
}

validate_codex_cli() {
  local actual_codex_version
  if ! actual_codex_version="$(command codex --version)"; then
    echo "ERROR: Codex CLI is unavailable or cannot report its identity." >&2
    exit 2
  fi
  CODEX_CLI_OBSERVED_VERSION="$actual_codex_version"
  export CODEX_CLI_OBSERVED_VERSION
  echo "→ Codex CLI: $actual_codex_version"
}

claude_structural_preflight() {
  section_rule "CLAUDE STRUCTURAL PREFLIGHT (no model)"
  set_index_relocation_args
  # The guarded expansion keeps an empty optional array safe under macOS Bash 3.2 with `set -u`.
  python3 "$ROOT/benchmarks/run-claude-structural.py" \
    --repo-path "$REPO" \
    --tasks "['FN-02']" \
    --arm all \
    --model haiku \
    ${INDEX_RELOCATION_ARGS[@]+"${INDEX_RELOCATION_ARGS[@]}"} \
    --dry-run
}

claude_agentic_preflight() {
  section_rule "CLAUDE AGENTIC PREFLIGHT (no model)"
  set_index_relocation_args
  python3 "$ROOT/benchmarks/run-claude-agentic.py" \
    --repo-path "$REPO" \
    --tasks "['BA-01']" \
    --arm A_plain \
    --model haiku \
    ${INDEX_RELOCATION_ARGS[@]+"${INDEX_RELOCATION_ARGS[@]}"} \
    --dry-run
}

claude_preflight() {
  claude_structural_preflight
  claude_agentic_preflight
}

codex_smoke_preflight() {
  local legend_arg="${1:-}"
  local codex_model codex_reasoning_effort
  configure_codex_model
  codex_model="$CODEX_SELECTED_MODEL"
  codex_reasoning_effort="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["model"]["reasoning_effort"])' "$MANIFEST_PATH")"
  section_rule "CODEX PREFLIGHT (no model)"
  validate_codex_cli
  local -a relocation_arg=()
  if [ -n "${BENCH_RUN_INDEX_RELOCATION:-}" ]; then
    relocation_arg=(--index-relocation-path "$BENCH_RUN_INDEX_RELOCATION")
  fi
  # The guarded expansion keeps an empty optional array safe under macOS Bash 3.2 with `set -u`.
  python3 "$ROOT/benchmarks/run-codex-structural.py" \
    --repo-path "$REPO" \
    --manifest-path "$MANIFEST_PATH" \
    --index-path "$INDEX_PATH" \
    --marketplace-root "$ROOT" \
    --codemap-bin "$CODEMAP_BIN" \
    --model "$codex_model" \
    --reasoning-effort "$codex_reasoning_effort" \
    --tasks FN-02 \
    --dry-run \
    --no-paid-command \
    ${relocation_arg[@]+"${relocation_arg[@]}"} \
    $legend_arg
}

smoke() {
  query_check
  claude_preflight
  codex_smoke_preflight
  echo "→ smoke OK: query command completed; Claude/Codex no-model preflights passed"
}

run_claude_structural_study() {
  local structural_tasks
  structural_tasks="$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "${SHARED_STRUCTURAL_TASK_IDS[@]}")"
  query_check
  claude_structural_preflight
  resolve_provider_models claude
  section_rule "CLAUDE STRUCTURAL (paid model runs)"
  echo "→ models: ${PROVIDER_MODELS[*]}"
  set_index_relocation_args
  for model in "${PROVIDER_MODELS[@]}"; do
    python3 "$ROOT/benchmarks/run-claude-structural.py" \
      --repo-path "$REPO" \
      --tasks "$structural_tasks" \
      --model "$model" \
      ${INDEX_RELOCATION_ARGS[@]+"${INDEX_RELOCATION_ARGS[@]}"} \
      --provider-parity
  done
}

claude() {
  run_claude_structural_study
  claude_agentic_preflight
  run_claude_agentic_study
}

run_claude_agentic_prepared_plan() {
  local model_count=3
  [ -z "$AGENTIC_MODEL" ] || model_count=1
  resolve_agentic_scope || return "$?"
  configure_agentic_dispatch || return "$?"
  section_rule "CLAUDE SHARED AGENTIC A/B/C PREFLIGHT (no model)"
  echo "→ design: $AGENTIC_TOTAL_CELLS cells (${#AGENTIC_TASK_IDS[@]} tasks × $AGENTIC_REPETITIONS repetitions × 3 arms × $model_count models; nonpoolable)"
  echo "→ scope: $AGENTIC_SCOPE_SHA"
  python3 "$AGENTIC_RUNNER" "${AGENTIC_DISPATCH_ARGS[@]}" --dry-run
}

run_claude_agentic_plan() {
  local model
  resolve_agentic_model || return "$?"
  if ! agentic_selection_is_sweep; then
    run_claude_agentic_prepared_plan
    return "$?"
  fi
  for model in "${AGENTIC_SELECTED_MODELS[@]}"; do
    AGENTIC_MODEL="$model"
    run_claude_agentic_prepared_plan || return "$?"
  done
  AGENTIC_MODEL=""
}

run_claude_agentic_prepared_study() {
  local model_count=3
  [ -z "$AGENTIC_MODEL" ] || model_count=1
  resolve_agentic_scope || return "$?"
  configure_agentic_dispatch || return "$?"
  section_rule "CLAUDE SHARED AGENTIC A/B/C STUDY (paid model runs)"
  echo "→ design: $AGENTIC_TOTAL_CELLS cells (${#AGENTIC_TASK_IDS[@]} tasks × $AGENTIC_REPETITIONS repetitions × 3 arms × $model_count models; nonpoolable)"
  echo "→ timeout: $AGENTIC_COORDINATE_TIMEOUT seconds per cell, including retries"
  echo "→ manifest: $METHODOLOGY_PATH ($(sha256_file "$METHODOLOGY_PATH"))"
  echo "→ scope: $AGENTIC_SCOPE_SHA"
  python3 "$AGENTIC_RUNNER" "${AGENTIC_DISPATCH_ARGS[@]}" --report
}

run_claude_agentic_study() {
  local model
  resolve_agentic_model || return "$?"
  if ! agentic_selection_is_sweep; then
    run_claude_agentic_prepared_study
    return "$?"
  fi
  for model in "${AGENTIC_SELECTED_MODELS[@]}"; do
    AGENTIC_MODEL="$model"
    run_claude_agentic_prepared_study || return "$?"
  done
  AGENTIC_MODEL=""
}

run_claude_structural_plan() {
  local structural_tasks
  structural_tasks="$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "${SHARED_STRUCTURAL_TASK_IDS[@]}")"
  query_check
  claude_structural_preflight
  resolve_provider_models claude
  section_rule "CLAUDE STRUCTURAL (no-model full plans)"
  echo "→ models: ${PROVIDER_MODELS[*]}"
  set_index_relocation_args
  for model in "${PROVIDER_MODELS[@]}"; do
    python3 "$ROOT/benchmarks/run-claude-structural.py" \
      --repo-path "$REPO" \
      --tasks "$structural_tasks" \
      --model "$model" \
      ${INDEX_RELOCATION_ARGS[@]+"${INDEX_RELOCATION_ARGS[@]}"} \
      --provider-parity \
      --dry-run
  done
}

run_claude_plan() {
  run_claude_structural_plan
  claude_agentic_preflight
  run_claude_agentic_plan
}

require_codex_paid_inputs() {
  if [ ! -f "$MANIFEST_PATH" ]; then
    echo "ERROR: active provider-parity manifest is missing: $MANIFEST_PATH" >&2
    exit 2
  fi
  if [[ ! "${CODEX_PAID_APPROVAL:-}" =~ ^[0-9a-f]{16,64}$ ]]; then
    echo "ERROR: paid Codex mode requires the 16-character approval token (or longer matching prefix) printed by --dry-run." >&2
    print_codex_paid_guidance
    exit 2
  fi
  if [ -z "${CODEX_AUTH_SOURCE:-}" ] || [ ! -f "$CODEX_AUTH_SOURCE" ]; then
    echo "ERROR: paid Codex mode requires CODEX_AUTH_SOURCE pointing to a private auth.json." >&2
    print_codex_paid_guidance
    exit 2
  fi
  set_default_codex_run_dir
  if [ "${CODEX_LAUNCHER_SNAPSHOT_ACTIVE:-}" = "1" ]; then
    expected_launcher="$CODEX_RUN_DIR/.launcher/run-all.sh"
    if [ "$0" != "$expected_launcher" ] || [ "${CODEX_INVOCATION_LAUNCHER:-}" != "$expected_launcher" ]; then
      echo "ERROR: paid Codex mode is not executing its private launcher snapshot." >&2
      exit 2
    fi
    if [ "$(sha256_file "$expected_launcher")" != "${CODEX_LAUNCHER_SHA256:-}" ]; then
      echo "ERROR: paid Codex launcher snapshot changed before execution." >&2
      exit 2
    fi
    validate_paid_source_snapshot
  elif [ -e "$CODEX_RUN_DIR" ]; then
    echo "ERROR: CODEX_RUN_DIR already exists: $CODEX_RUN_DIR" >&2
    print_codex_paid_guidance
    exit 2
  fi
}

exec_codex_launcher_snapshot() {
  local launcher_dir="$CODEX_RUN_DIR/.launcher"
  local launcher_snapshot="$launcher_dir/run-all.sh"
  local source_root="$launcher_dir/source"
  local source_manifest="$launcher_dir/source.sha256"
  mkdir -p "$launcher_dir"
  archive_paid_source "$source_root"
  if [ -f "$ROOT/benchmarks/manifests/codemap-package-mode-map.json" ]; then
    cp "$ROOT/benchmarks/manifests/codemap-package-mode-map.json" "$source_root/benchmarks/manifests/codemap-package-mode-map.json"
  else
    capture_codemap_mode_map "$source_root/benchmarks/manifests/codemap-package-mode-map.json"
  fi
  build_source_checksum_manifest "$source_root" "$source_manifest"
  cp "$source_root/benchmarks/run-all.sh" "$launcher_snapshot"
  chmod 500 "$launcher_snapshot"
  if [[ "${CODEMAP_BIN:-}" == "$ROOT/plugins/codemap-py/"* ]]; then
    export CODEMAP_BIN="$source_root/${CODEMAP_BIN#"$ROOT"/}"
  fi
  export CODEX_LAUNCHER_ROOT="$source_root"
  export CODEX_INVOCATION_LAUNCHER="$launcher_snapshot"
  export CODEX_LAUNCHER_SHA256="$(sha256_file "$launcher_snapshot")"
  export CODEX_SOURCE_MANIFEST_SHA256="$(sha256_file "$source_manifest")"
  export CODEX_LAUNCHER_SNAPSHOT_ACTIVE=1
  export PYTHONDONTWRITEBYTECODE=1
  exec /bin/bash "$launcher_snapshot" "$@"
}

print_codex_paid_guidance() {
  if [ -n "$CODEX_TASKS" ]; then
    ensure_codex_scope_resolved
    scope_guidance="Selected tasks: $CODEX_TASKS; $CODEX_SELECTION_TOTAL_CELLS stage-native A/B/C cells."
  else
    scope_guidance=""
  fi
  cat >&2 <<EOF

Review the exact no-model plan first:
  $(launcher_command plan)

Then launch the paid study with the short approval token printed above:
  CODEX_PAID_APPROVAL=<approval-token-printed-above> \\
  CODEX_AUTH_SOURCE="\$HOME/.codex/auth.json" \\
    $(launcher_command paid)

The command records paid authorization for this exact aggregate scope. The launcher creates a fresh run directory under benchmarks/results; set CODEX_RUN_DIR only to choose another new path. Review benchmarks/manifests/codex-integration.md for the locked scope.
Credential warning: use an immutable, user-owned 0600 auth source. Do not run a concurrent Codex session with it; use an independently authenticated benchmark credential instead. The runner keeps private run state and atomically propagates valid refreshes between cells. A private sequential refresh can invalidate an unchanged source, so reauthenticate after the run if needed. Known refresh-token authentication failures stop immediately; three matching unknown zero-token pre-response failures preserve partial artifacts and stop scheduling.
${scope_guidance:+$'\n'"$scope_guidance"$'\n'}
EOF
}

codex_agentic_approval_hint() {
  # Callers resolve the agentic scope first: this runs inside a command
  # substitution, where any global the resolver sets would be discarded.
  if agentic_selection_is_sweep; then
    codex_agentic_sweep_approval
    return 0
  fi
  codex_agentic_child_approval_hint
}

codex_agentic_child_approval_hint() {
  if agentic_scope_is_default; then
    sha256_file "$AGENTIC_MANIFEST_PATH"
  else
    printf '%s' "$AGENTIC_SCOPE_SHA"
  fi
}

codex_agentic_sweep_approval() {
  codex_models_scope_sha "${AGENTIC_SWEEP_APPROVALS[*]}" "${AGENTIC_SELECTED_MODELS[*]}"
}

resolve_codex_agentic_sweep_scopes() {
  local model
  resolve_agentic_model || return "$?"
  if ! agentic_selection_is_sweep; then
    resolve_agentic_scope || return "$?"
    AGENTIC_SWEEP_TOTAL_CELLS="$AGENTIC_TOTAL_CELLS"
    return 0
  fi
  AGENTIC_SWEEP_APPROVALS=()
  AGENTIC_SWEEP_TOTAL_CELLS=0
  for model in "${AGENTIC_SELECTED_MODELS[@]}"; do
    select_agentic_model "$model"
    resolve_agentic_scope || return "$?"
    AGENTIC_SWEEP_APPROVALS+=("$(codex_agentic_child_approval_hint)")
    AGENTIC_SWEEP_TOTAL_CELLS="$((AGENTIC_SWEEP_TOTAL_CELLS + AGENTIC_TOTAL_CELLS))"
  done
  AGENTIC_MODEL=""
}

print_codex_agentic_paid_guidance() {
  if [ -z "$AGENTIC_SCOPE_SHA" ]; then
    resolve_codex_agentic_sweep_scopes
  fi
  approval_hint="$(codex_agentic_approval_hint)"
  cat >&2 <<EOF

Review the exact no-model shared agentic plan:
  $(launcher_command plan)

Then launch the paid $AGENTIC_SWEEP_TOTAL_CELLS-cell study with one scope-bound command:
  CODEX_PAID_APPROVAL=${approval_hint:0:16} \\
  CODEX_AUTH_SOURCE="\$HOME/.codex/auth.json" \\
    $(launcher_command paid)

The launcher creates a fresh run directory under benchmarks/results; set CODEX_RUN_DIR only to choose another new path. Review benchmarks/manifests/codex-agentic.md for the locked scope before running the paid study.
Credential warning: use an immutable, user-owned 0600 auth source. Do not run a concurrent Codex session with it; use an independently authenticated benchmark credential instead. The runner keeps private run state and atomically propagates valid refreshes between cells. A private sequential refresh can invalidate an unchanged source, so reauthenticate after the run if needed.
EOF
}

require_codex_agentic_paid_inputs() {
  if [ ! -f "$AGENTIC_MANIFEST_PATH" ]; then
    echo "ERROR: active Codex agentic manifest is missing: $AGENTIC_MANIFEST_PATH" >&2
    exit 2
  fi
  local agentic_manifest_sha
  agentic_manifest_sha="$(sha256_file "$AGENTIC_MANIFEST_PATH")"
  resolve_codex_agentic_sweep_scopes
  if agentic_selection_is_sweep; then
    approval_hint="$(codex_agentic_sweep_approval)"
  elif agentic_scope_is_default; then
    approval_hint="$agentic_manifest_sha"
  else
    approval_hint="$AGENTIC_SCOPE_SHA"
  fi
  # Both lanes authorize the same way from the operator's side: paste the token the plan printed.
  # The agentic lane used to demand its own variable and the whole 64-character digest, so a token
  # copied in the structural lane's shape was refused even when it named this exact scope.
  local supplied_approval="${CODEX_AGENTIC_PAID_APPROVAL:-${CODEX_PAID_APPROVAL:-}}"
  if [ -z "$supplied_approval" ] || [ "${#supplied_approval}" -lt 16 ] || [[ "$approval_hint" != "$supplied_approval"* ]]; then
    echo "ERROR: paid Codex agentic mode requires CODEX_PAID_APPROVAL=${approval_hint:0:16}" >&2
    print_codex_agentic_paid_guidance
    exit 2
  fi
  CODEX_AGENTIC_PAID_APPROVAL="$approval_hint"
  export CODEX_AGENTIC_PAID_APPROVAL
  if [ -z "${CODEX_AUTH_SOURCE:-}" ] || [ ! -f "$CODEX_AUTH_SOURCE" ]; then
    echo "ERROR: paid Codex agentic mode requires CODEX_AUTH_SOURCE pointing to a private auth.json." >&2
    print_codex_agentic_paid_guidance
    exit 2
  fi
  set_default_codex_run_dir
  if [ "${CODEX_LAUNCHER_SNAPSHOT_ACTIVE:-}" = "1" ]; then
    expected_launcher="$CODEX_RUN_DIR/.launcher/run-all.sh"
    if [ "$0" != "$expected_launcher" ] || [ "${CODEX_INVOCATION_LAUNCHER:-}" != "$expected_launcher" ]; then
      echo "ERROR: paid Codex agentic mode is not executing its private launcher snapshot." >&2
      exit 2
    fi
    if [ "$(sha256_file "$expected_launcher")" != "${CODEX_LAUNCHER_SHA256:-}" ]; then
      echo "ERROR: paid Codex agentic launcher snapshot changed before execution." >&2
      exit 2
    fi
    validate_paid_source_snapshot
  elif [ -e "$CODEX_RUN_DIR" ]; then
    echo "ERROR: CODEX_RUN_DIR already exists: $CODEX_RUN_DIR" >&2
    print_codex_agentic_paid_guidance
    exit 2
  fi
}

# The structural half of a combined token is whatever that lane's own study binds: one stratum binds
# its execution scope, several bind the ordered model list on top of it. Both providers therefore run
# the same shape — one combined invocation, every selected stratum, one token — instead of Codex
# alone forcing the strata onto a separate command.
codex_multi_stratum_design() {
  local per_model_cells
  per_model_cells="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["task_selection"]["default_total_cells"])' "$MANIFEST_PATH")"
  [ -z "$CODEX_TASKS" ] || per_model_cells="$CODEX_SELECTION_TOTAL_CELLS"
  printf '%s cells = %s per stratum × %s strata (separate, nonpoolable studies)\n' \
    "$(( per_model_cells * ${#CODEX_SELECTED_MODELS[@]} ))" "$per_model_cells" "${#CODEX_SELECTED_MODELS[@]}"
}

codex_combined_structural_scope() {
  if [ "${#CODEX_SELECTED_MODELS[@]}" -gt 1 ]; then
    codex_models_scope_sha "${CODEX_EXECUTION_SCOPES[*]}" "${CODEX_SELECTED_MODELS[*]}"
  else
    printf '%s\n' "$CODEX_EXECUTION_SCOPE_SHA"
  fi
}

print_codex_combined_plan_authorization() {
  # Printed by the combined no-model plan, after both child scopes are known.
  local combined_scope agentic_approval structural_scope strata_design="" strata_lanes="both lanes"
  agentic_approval="$(codex_agentic_approval_hint)"
  structural_scope="$(codex_combined_structural_scope)"
  combined_scope="$(codex_combined_scope_sha "$structural_scope" "$agentic_approval")"
  # Several strata multiply the structural lane's cell count, which the plan above prints once per
  # stratum; the total the token buys has to be disclosed where the token is minted.
  if [ "${#CODEX_SELECTED_MODELS[@]}" -gt 1 ]; then
    strata_design="
 strata design     $(codex_multi_stratum_design)"
  fi
  # The header leads a block a reader has to find in a long run log, so it is drawn by the same
  # renderer as every other phase header instead of being spelled out once more inside the heredoc.
  # The fields below it, and the copyable command under them, stay exactly as they were printed.
  printf '\n'
  section_rule "CODEX COMBINED AUTHORIZATION (structural + agentic)"
  cat <<EOF
COMBINED SCOPE     $combined_scope
 structural scope  $structural_scope
 strata            ${CODEX_SELECTED_MODELS[*]} ($strata_lanes)$strata_design
 agentic scope     $agentic_approval
 agentic design    $AGENTIC_SWEEP_TOTAL_CELLS cells
PAID_COMMAND:
------------------------------------------------------------------------------
  CODEX_PAID_APPROVAL=${combined_scope:0:16} \\
  CODEX_AUTH_SOURCE="\$HOME/.codex/auth.json" \\
    $(launcher_command paid)
------------------------------------------------------------------------------
EOF
}

print_codex_combined_paid_guidance() {
  cat >&2 <<EOF

Review both exact no-model plans first:
  $(launcher_command plan)

Then launch the paid structural and agentic studies from one frozen source:
  CODEX_PAID_APPROVAL=<combined-approval-token-printed-by-the-unified-dry-run> \\
  CODEX_AUTH_SOURCE="\$HOME/.codex/auth.json" \\
    $(launcher_command paid)

One token authorizes both studies: it binds the structural scope and the agentic scope together, so either one drifting invalidates it.

The launcher creates one combined result root with structural/ and agentic/ child runs; set CODEX_RUN_DIR only to choose another new combined root. Review benchmarks/manifests/codex-integration.md and benchmarks/manifests/codex-agentic.md before running the paid studies.
Credential warning: use an immutable, user-owned 0600 auth source. Do not run a concurrent Codex session with it; use an independently authenticated benchmark credential instead. The two studies run sequentially and stop on the first failure while preserving completed artifacts.
EOF
}

require_codex_combined_paid_inputs() {
  local expected_launcher
  if [[ ! "${CODEX_PAID_APPROVAL:-}" =~ ^[0-9a-f]{16,64}$ ]]; then
    echo "ERROR: paid combined Codex mode requires the combined approval token printed by --dry-run." >&2
    print_codex_combined_paid_guidance
    exit 2
  fi
  if [ -z "${CODEX_AUTH_SOURCE:-}" ] || [ ! -f "$CODEX_AUTH_SOURCE" ]; then
    echo "ERROR: paid combined Codex mode requires CODEX_AUTH_SOURCE pointing to a private auth.json." >&2
    print_codex_combined_paid_guidance
    exit 2
  fi
  set_default_codex_run_dir
  if [ "${CODEX_LAUNCHER_SNAPSHOT_ACTIVE:-}" = "1" ]; then
    expected_launcher="$CODEX_RUN_DIR/.launcher/run-all.sh"
    if [ "$0" != "$expected_launcher" ] || [ "${CODEX_INVOCATION_LAUNCHER:-}" != "$expected_launcher" ]; then
      echo "ERROR: paid combined Codex mode is not executing its private launcher snapshot." >&2
      exit 2
    fi
    if [ "$(sha256_file "$expected_launcher")" != "${CODEX_LAUNCHER_SHA256:-}" ]; then
      echo "ERROR: paid combined Codex launcher snapshot changed before execution." >&2
      exit 2
    fi
    validate_paid_source_snapshot
  elif [ -e "$CODEX_RUN_DIR" ]; then
    echo "ERROR: CODEX_RUN_DIR already exists: $CODEX_RUN_DIR" >&2
    print_codex_combined_paid_guidance
    exit 2
  fi
}

resolve_codex_tasks() {
  local selection_json
  if ! selection_json="$(python3 "$ROOT/benchmarks/run-codex-structural.py" \
    --manifest-path "$MANIFEST_PATH" \
    --resolve-tasks "$CODEX_TASKS" 2>&1)"; then
    echo "ERROR: invalid Codex task selection '$CODEX_TASKS':" >&2
    echo "$selection_json" >&2
    return 2
  fi
  if ! CODEX_SELECTION_SCOPE_SHA="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read())["scope_sha256"])' <<<"$selection_json" 2>/dev/null)" \
    || ! CODEX_SELECTION_TOTAL_CELLS="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read())["total_cells"])' <<<"$selection_json" 2>/dev/null)"; then
    echo "ERROR: Codex task resolver returned malformed selection metadata:" >&2
    echo "$selection_json" >&2
    return 2
  fi
  CODEX_SELECTION_TASK_IDS=()
  while IFS= read -r task_id; do
    [ -n "$task_id" ] && CODEX_SELECTION_TASK_IDS+=("$task_id")
  done < <(python3 -c 'import json,sys; print(*json.loads(sys.stdin.read())["task_ids"], sep="\n")' <<<"$selection_json")
  if [ "${#CODEX_SELECTION_TASK_IDS[@]}" -eq 0 ]; then
    echo "ERROR: Codex task resolver returned no task IDs." >&2
    return 2
  fi
}

ensure_codex_scope_resolved() {
  if [ -n "$CODEX_TASKS" ] && [ -z "$CODEX_SELECTION_SCOPE_SHA" ]; then
    resolve_codex_tasks
  fi
}

configure_codex_plan() {
  active_manifest_sha="$(sha256_file "$MANIFEST_PATH")"
  # One interpreter start for the whole manifest read, not one per field.
  {
    IFS= read -r coordinate_timeout
    IFS= read -r codex_model
    IFS= read -r codex_reasoning_effort
  } < <(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["execution_controls"]["parity_timeout_seconds"]); print(d["model"]["name"]); print(d["model"]["reasoning_effort"])' "$MANIFEST_PATH")
  configure_codex_model
  codex_model="$CODEX_SELECTED_MODEL"
  task_count="$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["task_selection"]["allowed_task_ids"]))' "$MANIFEST_PATH")"
  planned_cells="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["task_selection"]["default_total_cells"])' "$MANIFEST_PATH")"
  section_rule "CODEX UNIFIED TASK STUDY"
  echo "→ design: $planned_cells cells ($task_count tasks × A/B/C; stage-native scoring)"
  echo "→ stages: 55 structural + 6 ReadCrop + 4 Fix-Single + 3 Fix-Multi + 5 Patch"
  echo "→ model: $codex_model; reasoning effort: $codex_reasoning_effort"
  echo "→ timeout: $coordinate_timeout seconds per cell, including retries"
  echo "→ manifest: $MANIFEST_PATH ($active_manifest_sha)"
  common_args=(
    --repo-path "$REPO" \
    --manifest-path "$MANIFEST_PATH" \
    --index-path "$INDEX_PATH" \
    --marketplace-root "$ROOT" \
    --codemap-bin "$CODEMAP_BIN" \
    --model "$codex_model" \
    --reasoning-effort "$codex_reasoning_effort"
  )
  # An isolated run's index is the locked graph with only its scan root moved, so admission checks
  # this provenance in place of the byte hash that reproduces solely at the canonical clone.
  if [ -n "${BENCH_RUN_INDEX_RELOCATION:-}" ]; then
    common_args+=(--index-relocation-path "$BENCH_RUN_INDEX_RELOCATION")
  fi
}

configure_codex_selected_plan() {
  ensure_codex_scope_resolved
  active_manifest_sha="$(sha256_file "$MANIFEST_PATH")"
  configure_codex_model
  codex_model="$CODEX_SELECTED_MODEL"
  codex_reasoning_effort="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["model"]["reasoning_effort"])' "$MANIFEST_PATH")"
  selected_task_count="${#CODEX_SELECTION_TASK_IDS[@]}"
  selected_cells="$CODEX_SELECTION_TOTAL_CELLS"
  section_rule "CODEX SELECTED A/B/C STUDY"
  echo "→ design: $selected_cells cells ($selected_task_count selected tasks; stage-native scoring)"
  echo "→ tasks: ${CODEX_TASKS}"
  echo "→ model: $codex_model; reasoning effort: $codex_reasoning_effort"
  echo "→ selection scope: $CODEX_SELECTION_SCOPE_SHA"
  common_args=(
    --repo-path "$REPO" \
    --manifest-path "$MANIFEST_PATH" \
    --index-path "$INDEX_PATH" \
    --marketplace-root "$ROOT" \
    --codemap-bin "$CODEMAP_BIN" \
    --model "$codex_model" \
    --reasoning-effort "$codex_reasoning_effort" \
    --tasks "$CODEX_TASKS"
  )
  if [ -n "${BENCH_RUN_INDEX_RELOCATION:-}" ]; then
    common_args+=(--index-relocation-path "$BENCH_RUN_INDEX_RELOCATION")
  fi
}

run_codex_plan() {
  local plan_output model model_scope index
  local -a model_args
  # Paid execution wraps this function in a tee pipeline. Explicit propagation
  # prevents the full plan or paid cells from starting after a failed smoke.
  query_check || return "$?"
  codex_smoke_preflight --no-legend || return "$?"
  if [ -n "$CODEX_TASKS" ]; then
    configure_codex_selected_plan || return "$?"
  else
    configure_codex_plan || return "$?"
  fi
  if [ -n "$CODEX_TASKS" ]; then
    section_rule "CODEX SELECTED A/B/C PREFLIGHT (no model)"
  else
    section_rule "CODEX CONFIRMATORY A/B/C PREFLIGHT (no model)"
  fi
  if ! plan_output="$(mktemp "$BENCHMARK_TEMP_ROOT/codex-unified-plan.XXXXXX")"; then
    echo "ERROR: failed to create the private Codex plan capture." >&2
    return 2
  fi
  # The runner's own copyable command is always suppressed here, and the launcher prints the one
  # command this plan authorizes instead: the combined block for a combined plan, the multi-stratum
  # block for a selection, the structural block below for everything else. One plan, one command,
  # and always the launcher-shaped one the operator can actually retype.
  CODEX_EXECUTION_SCOPES=()
  for model in "${CODEX_SELECTED_MODELS[@]}"; do
    model_args=("${common_args[@]}")
    for index in "${!model_args[@]}"; do
      if [ "${model_args[$index]}" = "--model" ]; then
        model_args[$((index + 1))]="$model"
        break
      fi
    done
    : > "$plan_output"
    if ! python3 "$ROOT/benchmarks/run-codex-structural.py" "${model_args[@]}" --dry-run \
      --no-paid-command | tee -a "$plan_output"; then
      rm -f "$plan_output"
      return 1
    fi
    model_scope="$(awk '$1 == "SCOPE" {scope = $2} END {print scope}' "$plan_output")"
    if [[ ! "$model_scope" =~ ^[0-9a-f]{64}$ ]]; then
      rm -f "$plan_output"
      echo "ERROR: Codex preflight did not emit a valid scope for $model." >&2
      return 2
    fi
    CODEX_EXECUTION_SCOPES+=("$model_scope")
    if [ "$model" = "${CODEX_SELECTED_MODELS[0]}" ]; then
      CODEX_EXECUTION_SCOPE_SHA="$model_scope"
    fi
  done
  rm -f "$plan_output"
  if [[ ! "$CODEX_EXECUTION_SCOPE_SHA" =~ ^[0-9a-f]{64}$ ]]; then
    echo "ERROR: unified Codex preflight did not emit one valid aggregate SCOPE." >&2
    return 2
  fi
  print_codex_models_authorization
  print_codex_structural_authorization
}

# The structural lane used to hand the operator the runner's own command: a python entrypoint
# carrying eight absolute paths and its own generated run directory, which bypasses the launcher and
# with it the target lock, the frozen launcher snapshot, and the worktree. On an isolated run it was
# worse than asymmetric — the --repo-path and --index-path it named were the private worktree this
# dry run removes on its way out, so the command could not succeed at all. The paid run is told to
# cut its own fresh worktree from the same flags instead.
print_codex_structural_authorization() {
  # Paid execution reuses this plan as its preflight; the command belongs to the plan, not the run.
  [ "$DRY_RUN" = true ] || return 0
  # A combined plan authorizes both lanes under one token and a selection mints its own over the
  # whole ordered list. Each prints the single command it authorizes; this block covers the rest.
  [ "${CODEX_PLAN_SUPPRESSES_LANE_COMMAND:-}" = "1" ] && return 0
  [ "${#CODEX_SELECTED_MODELS[@]}" -gt 1 ] && return 0
  printf '\n'
  section_rule "CODEX STRUCTURAL AUTHORIZATION"
  cat <<EOF
STRATUM            ${CODEX_SELECTED_MODELS[0]}
 structural scope  $CODEX_EXECUTION_SCOPE_SHA
 token binds       the aggregate structural scope above
PAID_COMMAND:
------------------------------------------------------------------------------
  CODEX_PAID_APPROVAL=${CODEX_EXECUTION_SCOPE_SHA:0:16} \\
  CODEX_AUTH_SOURCE="\$HOME/.codex/auth.json" \\
    $(launcher_command paid)
------------------------------------------------------------------------------
EOF
}

# The runner prices one stratum, so a multi-stratum study has to disclose its own total and mint its
# own token; otherwise the printed cell count would understate what the approval actually buys.
print_codex_models_authorization() {
  local models_scope
  [ "${#CODEX_SELECTED_MODELS[@]}" -le 1 ] && return 0
  # A combined invocation mints its own token over both lanes and prints it below, and its structural
  # child runs under that same token. A structural-only token in either place would offer a second
  # copyable command that silently drops the agentic study.
  [ "$STRUCTURAL" != true ] && return 0
  [ -n "${CODEX_COMBINED_AGENTIC_APPROVAL:-}" ] && return 0
  models_scope="$(codex_models_scope_sha "${CODEX_EXECUTION_SCOPES[*]}" "${CODEX_SELECTED_MODELS[*]}")"
  # Same header treatment as the combined block: rendered, not hand-spelled, so both authorizations
  # and every phase header around them are one surface.
  printf '\n'
  section_rule "CODEX MULTI-STRATUM AUTHORIZATION"
  cat <<EOF
MODELS             ${CODEX_SELECTED_MODELS[*]}
DESIGN             $(codex_multi_stratum_design)
MODELS SCOPE       $models_scope
 structural scope  $CODEX_EXECUTION_SCOPE_SHA
PAID_COMMAND:
------------------------------------------------------------------------------
  CODEX_PAID_APPROVAL=${models_scope:0:16} \\
  CODEX_AUTH_SOURCE="\$HOME/.codex/auth.json" \\
    $(launcher_command paid)
------------------------------------------------------------------------------
EOF
}

# One approval, N strata: the parent verifies the model-bound token once, then each stratum runs as
# its own child study in its own run directory, holding the scope token the child re-derives.
run_codex_model_strata() {
  local models_scope expected_approval parent_dir model child_status
  local parent_agentic_approval="${CODEX_COMBINED_AGENTIC_APPROVAL:-}"
  local -a child_args
  models_scope="$(codex_models_scope_sha "${CODEX_EXECUTION_SCOPES[*]}" "${CODEX_SELECTED_MODELS[*]}")"
  # Inside a combined run the same strata are one half of a token that also binds the agentic scope,
  # so the parent verifies whichever token this invocation was actually authorized with.
  if [ -n "${CODEX_COMBINED_AGENTIC_APPROVAL:-}" ]; then
    expected_approval="$(codex_combined_scope_sha "$models_scope" "$CODEX_COMBINED_AGENTIC_APPROVAL")"
  else
    expected_approval="$models_scope"
  fi
  if [[ "$expected_approval" != "$CODEX_PAID_APPROVAL"* ]]; then
    echo "ERROR: paid multi-stratum Codex mode requires CODEX_PAID_APPROVAL=${expected_approval:0:16}" >&2
    echo "Copy the token printed by the completed multi-stratum no-model preflight." >&2
    return 2
  fi
  parent_dir="$CODEX_RUN_DIR"
  for model in "${CODEX_SELECTED_MODELS[@]}"; do
    child_args=(codex --struct --models="$model")
    [ -z "$CODEX_TASKS" ] || child_args+=(--tasks="$CODEX_TASKS")
    section_rule "CODEX STRATUM $model"
    validate_paid_source_snapshot
    (
      unset CODEX_INVOCATION_LAUNCHER CODEX_LAUNCHER_SHA256 CODEX_LAUNCHER_SNAPSHOT_ACTIVE
      # This parent already verified whichever token authorized the whole selection, combined or
      # not. Each stratum is a single-model study from here, so it verifies its own execution scope.
      unset CODEX_COMBINED_AGENTIC_APPROVAL
      export CODEX_RUN_DIR="$parent_dir/$model"
      # Each child must match its own recorded scope before it can spend under the parent token.
      export CODEX_PAID_APPROVAL="$expected_approval"
      export CODEX_STRATUM_PARENT_SCOPE="${CODEX_EXECUTION_SCOPES[*]}"
      export CODEX_STRATUM_MODELS="${CODEX_SELECTED_MODELS[*]}"
      export CODEX_STRATUM_AGENTIC_APPROVAL="$parent_agentic_approval"
      /bin/bash "$ROOT/benchmarks/run-all.sh" "${child_args[@]}"
    )
    child_status="$?"
    [ "$child_status" -ne 0 ] && return "$child_status"
  done
  # The guard above is the loop's last command, so a clean run would otherwise report its own
  # false test as the function's status and abort the caller after every stratum had succeeded.
  return 0
}

# A stratum child cannot re-derive the multi-stratum token on its own: that token binds the whole
# ordered selection and every parent's resolved scope, and a child knows only its own. The parent
# therefore passes the terms, and the child recomputes the token from them rather than trusting the
# value it was handed. Prints nothing and returns non-zero when this is not a stratum child.
codex_stratum_delegated_approval() {
  local models_scope index matched=false
  local -a parent_models parent_scopes
  [ -n "${CODEX_STRATUM_PARENT_SCOPE:-}" ] || return 1
  [ -n "${CODEX_STRATUM_MODELS:-}" ] || return 1
  [ "${#CODEX_SELECTED_MODELS[@]}" -eq 1 ] || return 1
  # The child must be one of the strata the operator authorized, not an unrelated model riding a
  # token minted for someone else.
  read -r -a parent_models <<< "$CODEX_STRATUM_MODELS"
  read -r -a parent_scopes <<< "$CODEX_STRATUM_PARENT_SCOPE"
  [ "${#parent_models[@]}" -eq "${#parent_scopes[@]}" ] || return 1
  for index in "${!parent_models[@]}"; do
    if [ "${parent_models[$index]}" = "${CODEX_SELECTED_MODELS[0]}" ]; then
      [ "${parent_scopes[$index]}" = "$CODEX_EXECUTION_SCOPE_SHA" ] || return 1
      matched=true
      break
    fi
  done
  [ "$matched" = true ] || return 1
  models_scope="$(codex_models_scope_sha "$CODEX_STRATUM_PARENT_SCOPE" "$CODEX_STRATUM_MODELS")"
  if [ -n "${CODEX_STRATUM_AGENTIC_APPROVAL:-}" ]; then
    codex_combined_scope_sha "$models_scope" "$CODEX_STRATUM_AGENTIC_APPROVAL"
  else
    printf '%s\n' "$models_scope"
  fi
}

run_codex_study() {
  local expected_approval delegated_approval
  run_codex_plan || return "$?"
  if [ "${#CODEX_SELECTED_MODELS[@]}" -gt 1 ]; then
    run_codex_model_strata
    return "$?"
  fi
  delegated_approval="$(codex_stratum_delegated_approval || true)"
  if [ -n "${CODEX_STRATUM_PARENT_SCOPE:-}" ] && [ -z "$delegated_approval" ]; then
    echo "ERROR: child structural scope differs from its approved model stratum." >&2
    return 2
  fi
  if [ -n "$delegated_approval" ]; then
    expected_approval="$delegated_approval"
  elif [ -n "${CODEX_COMBINED_AGENTIC_APPROVAL:-}" ]; then
    expected_approval="$(codex_combined_scope_sha "$CODEX_EXECUTION_SCOPE_SHA" "$CODEX_COMBINED_AGENTIC_APPROVAL")"
  else
    expected_approval="$CODEX_EXECUTION_SCOPE_SHA"
  fi
  if [[ "$expected_approval" != "$CODEX_PAID_APPROVAL"* ]]; then
    echo "ERROR: paid Codex mode requires CODEX_PAID_APPROVAL=${expected_approval:0:16}" >&2
    echo "Copy the short approval token printed by the completed no-model preflight." >&2
    return 2
  fi
  if [ -n "$CODEX_TASKS" ]; then
    section_rule "CODEX SELECTED A/B/C STUDY (paid model runs)"
  else
    section_rule "CODEX CONFIRMATORY A/B/C STUDY (paid model runs)"
  fi
  python3 "$ROOT/benchmarks/run-codex-structural.py" \
    "${common_args[@]}" \
    --auth-source "$CODEX_AUTH_SOURCE" \
    --invocation-launcher-path "$CODEX_INVOCATION_LAUNCHER" \
    --no-legend \
    --run-dir "$CODEX_RUN_DIR/benchmark" \
    --paid-approval "${CODEX_EXECUTION_SCOPE_SHA:0:16}"
}

run_codex_agentic_prepared_plan() {
  resolve_agentic_scope || return "$?"
  section_rule "CODEX SHARED AGENTIC A/B/C PREFLIGHT (no model)"
  echo "→ design: $AGENTIC_TOTAL_CELLS cells (${#AGENTIC_TASK_IDS[@]} tasks × $AGENTIC_REPETITIONS repetitions × 3 arms; nonpoolable)"
  echo "→ scope: $AGENTIC_SCOPE_SHA"
  configure_agentic_dispatch || return "$?"
  python3 "$AGENTIC_RUNNER" "${AGENTIC_DISPATCH_ARGS[@]}" --dry-run || return "$?"
  print_codex_agentic_authorization
}

run_codex_agentic_prepared_plans() {
  local model prior_suppression
  resolve_agentic_model || return "$?"
  if ! agentic_selection_is_sweep; then
    run_codex_agentic_prepared_plan || return "$?"
    AGENTIC_SWEEP_TOTAL_CELLS="$AGENTIC_TOTAL_CELLS"
    return 0
  fi
  prior_suppression="${CODEX_PLAN_SUPPRESSES_LANE_COMMAND:-}"
  CODEX_PLAN_SUPPRESSES_LANE_COMMAND=1
  AGENTIC_SWEEP_APPROVALS=()
  AGENTIC_SWEEP_TOTAL_CELLS=0
  for model in "${AGENTIC_SELECTED_MODELS[@]}"; do
    select_agentic_model "$model"
    run_codex_agentic_prepared_plan || return "$?"
    AGENTIC_SWEEP_APPROVALS+=("$(codex_agentic_child_approval_hint)")
    AGENTIC_SWEEP_TOTAL_CELLS="$((AGENTIC_SWEEP_TOTAL_CELLS + AGENTIC_TOTAL_CELLS))"
  done
  AGENTIC_MODEL=""
  CODEX_PLAN_SUPPRESSES_LANE_COMMAND="$prior_suppression"
  [ -n "$prior_suppression" ] || print_codex_agentic_sweep_authorization
}

# Every other no-model plan ends in the one command it authorizes. The agentic plan used to print
# that command only for a task selection, so the plain `codex --agentic --dry-run` walked an operator
# through 48 planned cells and then named no way to run them.
print_codex_agentic_authorization() {
  local approval_hint token_binding
  # In a combined plan this lane is half of one authorization; the combined block names the command.
  [ "${CODEX_PLAN_SUPPRESSES_LANE_COMMAND:-}" = "1" ] && return 0
  if [ -z "$AGENTIC_SCOPE_SHA" ]; then
    resolve_agentic_scope
  fi
  approval_hint="$(codex_agentic_approval_hint)"
  # The full locked suite is authorized by the manifest digest and a narrowed run by its own scope,
  # so the token beside a printed scope is deliberately a different digest. Saying which one it is
  # keeps that from reading as a mismatch.
  if agentic_scope_is_default; then
    token_binding="the locked agentic manifest digest (whole suite)"
  else
    token_binding="this task, repetition, and stratum selection (agentic scope above)"
  fi
  printf '\n'
  section_rule "CODEX AGENTIC AUTHORIZATION"
  cat <<EOF
AGENTIC SCOPE      $AGENTIC_SCOPE_SHA
 design            $AGENTIC_TOTAL_CELLS cells (${#AGENTIC_TASK_IDS[@]} tasks × $AGENTIC_REPETITIONS repetitions × 3 arms; nonpoolable)
 stratum           ${AGENTIC_MODEL:-$(agentic_manifest_model) (manifest default; select another with --models)}
 token binds       $token_binding
PAID_COMMAND:
------------------------------------------------------------------------------
  CODEX_PAID_APPROVAL=${approval_hint:0:16} \\
  CODEX_AUTH_SOURCE="\$HOME/.codex/auth.json" \\
    $(launcher_command paid)
------------------------------------------------------------------------------
EOF
}

print_codex_agentic_sweep_authorization() {
  local approval_hint
  approval_hint="$(codex_agentic_sweep_approval)"
  printf '\n'
  section_rule "CODEX AGENTIC MODEL-SWEEP AUTHORIZATION"
  cat <<EOF
AGENTIC SWEEP SCOPE  $approval_hint
 design              $AGENTIC_SWEEP_TOTAL_CELLS cells (${#AGENTIC_SELECTED_MODELS[@]} strata × ${#AGENTIC_TASK_IDS[@]} tasks × $AGENTIC_REPETITIONS repetitions × 3 arms; separate, nonpoolable studies)
 strata              ${AGENTIC_SELECTED_MODELS[*]}
 token binds         the ordered stratum list and each resolved agentic child scope
PAID_COMMAND:
------------------------------------------------------------------------------
  CODEX_PAID_APPROVAL=${approval_hint:0:16} \\
  CODEX_AUTH_SOURCE="\$HOME/.codex/auth.json" \\
    $(launcher_command paid)
------------------------------------------------------------------------------
EOF
}

run_codex_agentic_plan() {
  # Agentic execution reuses the structural target/index preparation contract
  # but resolves its own shared task/repeat scope before dispatch.
  prepare_locked_inputs || return "$?"
  validate_codex_cli || return "$?"
  run_codex_agentic_prepared_plans
}

run_codex_agentic_study() {
  local agentic_manifest_sha agentic_model agentic_reasoning_effort
  agentic_manifest_sha="$(sha256_file "$AGENTIC_MANIFEST_PATH")"
  resolve_agentic_scope
  section_rule "CODEX SHARED AGENTIC A/B/C STUDY"
  echo "→ design: $AGENTIC_TOTAL_CELLS cells (${#AGENTIC_TASK_IDS[@]} tasks × $AGENTIC_REPETITIONS repetitions × 3 arms; nonpoolable)"
  agentic_model="${AGENTIC_MODEL:-$(agentic_manifest_model)}"
  agentic_reasoning_effort="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["model"]["reasoning_effort"])' "$AGENTIC_MANIFEST_PATH")"
  echo "→ model: $agentic_model; reasoning effort: $agentic_reasoning_effort"
  echo "→ timeout: $AGENTIC_COORDINATE_TIMEOUT seconds per cell, including retries"
  echo "→ manifest: $AGENTIC_MANIFEST_PATH ($agentic_manifest_sha)"
  echo "→ scope: $AGENTIC_SCOPE_SHA"
  echo "ARTIFACTS:"
  echo " - telemetry=$CODEX_RUN_DIR/telemetry.jsonl"
  echo " - metadata=$CODEX_RUN_DIR/run-metadata.json"
  configure_agentic_dispatch
  python3 "$AGENTIC_RUNNER" \
    "${AGENTIC_DISPATCH_ARGS[@]}" \
    --auth-source "$CODEX_AUTH_SOURCE" \
    --invocation-launcher-path "$CODEX_INVOCATION_LAUNCHER" \
    --run-dir "$CODEX_RUN_DIR" \
    --paid-approval "$CODEX_AGENTIC_PAID_APPROVAL"
}

run_codex_agentic_sweep_studies() {
  local parent_dir model approval_hint supplied_approval child_status index
  local -a child_args
  resolve_codex_agentic_sweep_scopes || return "$?"
  approval_hint="$(codex_agentic_sweep_approval)"
  supplied_approval="${CODEX_AGENTIC_PAID_APPROVAL:-${CODEX_PAID_APPROVAL:-}}"
  if [[ "$approval_hint" != "$supplied_approval"* ]]; then
    echo "ERROR: paid Codex agentic model sweep requires CODEX_PAID_APPROVAL=${approval_hint:0:16}" >&2
    return 2
  fi
  parent_dir="$CODEX_RUN_DIR"
  for index in "${!AGENTIC_SELECTED_MODELS[@]}"; do
    model="${AGENTIC_SELECTED_MODELS[$index]}"
    child_args=(codex --agentic --models="$model" --repetitions="$AGENTIC_REPETITIONS")
    [ -z "$CODEX_TASKS" ] || child_args+=(--tasks="$CODEX_TASKS")
    section_rule "CODEX AGENTIC STRATUM $model"
    validate_paid_source_snapshot
    (
      unset CODEX_INVOCATION_LAUNCHER CODEX_LAUNCHER_SHA256 CODEX_LAUNCHER_SNAPSHOT_ACTIVE
      export CODEX_RUN_DIR="$parent_dir/$model"
      export CODEX_PAID_APPROVAL="${AGENTIC_SWEEP_APPROVALS[$index]}"
      export CODEX_AGENTIC_PAID_APPROVAL="$CODEX_PAID_APPROVAL"
      /bin/bash "$ROOT/benchmarks/run-all.sh" "${child_args[@]}"
    )
    child_status="$?"
    [ "$child_status" -ne 0 ] && return "$child_status"
  done
  return 0
}

run_codex_with_artifacts() {
  # Keep the artifact log lossless; the structural runner renders only the console stream.
  local study_runner="$1"
  if "$study_runner" 2>&1 | tee "$CODEX_RUN_DIR/run.log" | python3 "$ROOT/benchmarks/run-codex-structural.py" --render-results --hide-plan; then
    run_status=0
  else
    run_status=$?
  fi
  checksum_path="$CODEX_RUN_DIR/checksums.sha256"
  write_codex_result_checksums "$checksum_path"
  echo "→ artifact checksums: $checksum_path"
  return "$run_status"
}

run_codex_agentic_with_artifacts() {
  # Agentic owns its run metadata/log lifecycle. Capture outside the admitted
  # run directory so Python still sees only the locked launcher at startup.
  local study_runner="$1"
  local stream_path
  local render_status
  local -a pipeline_status
  if ! stream_path="$(mktemp "$BENCHMARK_TEMP_ROOT/codex-agentic-console.XXXXXX")"; then
    echo "ERROR: failed to create the private Codex agentic console stream." >&2
    print_codex_agentic_paid_guidance
    return 2
  fi
  if "$study_runner" 2>&1 | tee "$stream_path" | python3 "$ROOT/benchmarks/run-codex-structural.py" --render-results --hide-plan; then
    pipeline_status=("${PIPESTATUS[@]}")
  else
    pipeline_status=("${PIPESTATUS[@]}")
  fi
  run_status="${pipeline_status[0]:-1}"
  render_status="${pipeline_status[2]:-1}"
  if [ "$run_status" -eq 0 ] && [ "$render_status" -ne 0 ]; then
    run_status="$render_status"
  fi
  cp "$stream_path" "$CODEX_RUN_DIR/run.log"
  rm -f "$stream_path"
  checksum_path="$CODEX_RUN_DIR/checksums.sha256"
  write_codex_result_checksums "$checksum_path"
  echo "→ artifact checksums: $checksum_path"
  if [ "$run_status" -ne 0 ]; then
    echo "ERROR: Codex agentic execution failed. Preserve the reported artifact for diagnosis; any retry requires a fresh CODEX_RUN_DIR." >&2
    print_codex_agentic_paid_guidance
  fi
  return "$run_status"
}

run_codex_combined_child() {
  local child_run_dir="$1"
  local selector="$2"
  shift 2
  local -a extra_args=("$@")
  validate_paid_source_snapshot
  (
    unset CODEX_INVOCATION_LAUNCHER CODEX_LAUNCHER_SHA256 CODEX_LAUNCHER_SNAPSHOT_ACTIVE
    export CODEX_RUN_DIR="$child_run_dir"
    # The structural child inherits CODEX_COMBINED_AGENTIC_APPROVAL and is the
    # first place both halves of the combined token are known, so it verifies
    # the combined approval against its own re-derived scope before any paid
    # cell. The agentic child then runs on the scope that token already bound.
    if [ "$selector" != "--struct" ]; then
      export CODEX_AGENTIC_PAID_APPROVAL="$CODEX_COMBINED_AGENTIC_APPROVAL"
      unset CODEX_COMBINED_AGENTIC_APPROVAL
    fi
    # The guarded expansion keeps an empty optional array safe under macOS Bash 3.2 with `set -u`.
    /bin/bash "$ROOT/benchmarks/run-all.sh" codex "$selector" ${extra_args[@]+"${extra_args[@]}"}
  )
}

run_codex_combined_studies() {
  local combined_root="$CODEX_RUN_DIR"
  local -a structural_extra_args=()
  configure_codex_model
  resolve_codex_agentic_sweep_scopes
  CODEX_COMBINED_AGENTIC_APPROVAL="$(codex_agentic_approval_hint)"
  export CODEX_COMBINED_AGENTIC_APPROVAL
  # The combined children each receive the canonical ordered selection. This includes the launcher
  # default, whose omitted spelling still has to reproduce the same two-lane sweep in both children.
  local -a agentic_extra_args=()
  local selected_models
  selected_models="$(IFS=,; echo "${CODEX_SELECTED_MODELS[*]}")"
  structural_extra_args=(--models="$selected_models")
  agentic_extra_args=(--models="$selected_models")
  run_codex_combined_child "$combined_root/structural" --struct ${structural_extra_args[@]+"${structural_extra_args[@]}"}
  run_codex_combined_child "$combined_root/agentic" --agentic ${agentic_extra_args[@]+"${agentic_extra_args[@]}"}
}

# A --models typo used to surface only when the study reached its own resolution step, after the
# sandbox reset, the frozen index preparation, and the full no-model query benchmark had already
# run. Resolving the selection here fails in a second instead, and the resolved list is discarded
# so each study still resolves its own.
if [ "$MODE" = "codex" ] && [ -z "$MODELS_SELECTION" ]; then
  MODELS_SELECTION="luna,terra"
fi
if [ -n "$MODELS_SELECTION" ] && [ "$MODE" != "smoke" ]; then
  resolve_provider_models "$MODE"
fi

if [ "$ISOLATED" = true ]; then
  prepare_run_worktree
fi

acquire_target_repo_lock "run-all.sh $MODE"

case "$MODE" in
  smoke)
    prepare_locked_inputs
    smoke
    ;;
  claude)
    prepare_claude_inputs
    if [ "$AGENTIC" = true ]; then
      if [ "$DRY_RUN" = true ]; then
        run_claude_agentic_plan
      else
        run_claude_agentic_study
      fi
    elif [ "$STRUCTURAL" = true ]; then
      if [ "$DRY_RUN" = true ]; then
        run_claude_structural_plan
      else
        run_claude_structural_study
      fi
    elif [ "$DRY_RUN" = true ]; then
      run_claude_plan
    else
      claude
    fi
    ;;
  codex)
    if [ "$AGENTIC" = true ]; then
      if [ "$DRY_RUN" = true ]; then
        run_codex_agentic_plan
      else
        require_codex_agentic_paid_inputs
        if [ "${CODEX_LAUNCHER_SNAPSHOT_ACTIVE:-}" != "1" ]; then
          exec_codex_launcher_snapshot "$@"
        fi
        if agentic_selection_is_sweep; then
          run_codex_agentic_sweep_studies
        else
          prepare_locked_inputs
          validate_codex_cli
          run_codex_agentic_with_artifacts run_codex_agentic_study
        fi
      fi
      echo "→ done. Results in benchmarks/results/"
      exit 0
    fi
    if [ "$STRUCTURAL" = true ]; then
      if [ -n "$CODEX_TASKS" ]; then
        ensure_codex_scope_resolved
      fi
      if [ "$DRY_RUN" = true ]; then
        prepare_locked_inputs
        run_codex_plan
      else
        require_codex_paid_inputs
        if [ "${CODEX_LAUNCHER_SNAPSHOT_ACTIVE:-}" != "1" ]; then
          exec_codex_launcher_snapshot "$@"
        fi
        prepare_locked_inputs
        run_codex_with_artifacts run_codex_study
      fi
    elif [ "$DRY_RUN" = true ]; then
      prepare_locked_inputs
      validate_codex_cli
      CODEX_PLAN_SUPPRESSES_LANE_COMMAND=1
      run_codex_plan
      run_codex_agentic_prepared_plans
      CODEX_PLAN_SUPPRESSES_LANE_COMMAND=""
      print_codex_combined_plan_authorization
    else
      require_codex_combined_paid_inputs
      if [ "${CODEX_LAUNCHER_SNAPSHOT_ACTIVE:-}" != "1" ]; then
        exec_codex_launcher_snapshot "$@"
      fi
      run_codex_combined_studies
    fi
    ;;
esac

echo "→ done. Results in benchmarks/results/"
