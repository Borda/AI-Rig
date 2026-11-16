# Benchmark index relock to scanner generation 16 — 2026-11-16

The frozen benchmark indexes were locked at scanner generation (`scan_version`) 13. Generations 14, 15 and 16 shipped without a relock, so `prepare-codex-index.py --print-contract` rejected the lock (`active index scan_version 13 does not match current Codemap schema …; rebuild and relock the index before running the benchmark`) and every benchmark preflight stopped there. This record covers the relock to generation 16.

## Comparability: results before this relock were measured at generation 13

Every benchmark result recorded under the generation 13 lock, including the agentic `B_auto` figures that a later treatment of optional Codemap use will be compared against, was measured against the generation 13 index of `pytorch-lightning` 2.6.5 (`be98784a1a03581b7051a355ae1084fd352d7cea`, raw `3c5840893e9c939baa61a6c5ce95994ff69ffe4a67d225aeb412c73deb61e0c1`, semantic `4086690e2b7bed8ff4fc95ed37606228caa236200ccad2e9d8c5dfb9dda1062e`). A run after this relock queries the generation 16 index of the same source commit. The graphs differ (different semantic digest), so a before/after comparison mixes the treatment effect with the index change; a comparison that must isolate the treatment re-measures the baseline arm at generation 16.

## Pins

| Pin                              | Field             | Before (generation 13)                                             | After (generation 16)                                              |
| -------------------------------- | ----------------- | ------------------------------------------------------------------ | ------------------------------------------------------------------ |
| methodology builder `INDEX_LOCK` | `scan_version`    | 13                                                                 | 16                                                                 |
| methodology builder `INDEX_LOCK` | `module_count`    | 645                                                                | 645                                                                |
| methodology builder `INDEX_LOCK` | `raw_sha256`      | `3c5840893e9c939baa61a6c5ce95994ff69ffe4a67d225aeb412c73deb61e0c1` | `6c2ec534abb8acb4d4fd516d67a8e65327d6f6c0109f58c0577289691cb77413` |
| methodology builder `INDEX_LOCK` | `semantic_sha256` | `4086690e2b7bed8ff4fc95ed37606228caa236200ccad2e9d8c5dfb9dda1062e` | `8ff0e1686b99ef04392fb03c54f1101764e04f0b6bc98d2021a393fff98b9295` |
| methodology builder `INDEX_LOCK` | `scanned_at`      | `2026-08-06T09:04:54.432797+00:00`                                 | `2026-11-16T00:00:00+00:00`                                        |

Generated manifests follow the builder: the methodology, Codex integration and Codex agentic manifests were rebuilt with their builders (each `--check` exits 0), and the agentic manifest's `frozen_index_contract` now carries `6c2ec534…` at generation 16.

## How the hashes were captured

1. Clone `pytorch-lightning` at tag 2.6.5 (`be98784a1a03581b7051a355ae1084fd352d7cea`) into the canonical root `codemap-provider-parity-pl-2.6.5` under the resolved system temporary directory (`BENCHMARK_TEMP_ROOT` in `run-all.sh`), the only path where the raw digest reproduces. No repository code was executed, tested or installed; the scanner only parses it.
2. Prove the capture recipe on the old lock first. With the scanner from the commit that created each lock (`eb232d79` for the main index, `0a7e4f3d` for the patch indexes), the repository's own tools reproduce every generation 13 pin exactly: `prepare-codex-index.py` normalisation plus `--verify --require-hash` gives `3c5840…`, and `--prepare-patch-bundle` passes all five patch locks, raw digests included.
3. Scan the canonical root with the generation 16 scanner under Python 3.11 (`codemap-py index --root`, as `run-all.sh` does), normalise as `prepare-codex-index.py` does (`project`, `scan_root`, `scanned_at`, compact JSON) and hash with its `semantic_index_sha256`. Two independent scans gave identical raw and semantic digests.
4. Verify through the real gate: `prepare-codex-index.py --print-contract --schema-path plugins/codemap-py/src/codemap_py/schema.py` prints `scan_version` 16, and `prepare-codex-index.py --index-path … --require-hash --verify` passes.

Observation: generation 13 output was not stable across scanner revisions. The last generation 13 scanner (`011b21d9`) gives patch task PT-01 a different semantic digest than the scanner that created its lock, so a generation number alone does not pin graph content; the semantic digest does.

## Preflight after the relock

| Command                                                 | Exit | Key line                                                                                                                                                                                                                                                                                                         |
| ------------------------------------------------------- | ---: | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `bash benchmarks/run-all.sh claude --struct --dry-run`  |    0 | `→ index contract: scan_version=16 sha256=6c2ec534…`, index `verified`                                                                                                                                                                                                                                           |
| `bash benchmarks/run-all.sh claude --agentic --dry-run` |    0 | same contract line, index `verified`                                                                                                                                                                                                                                                                             |
| `bash benchmarks/run-all.sh codex --agentic --dry-run`  |    0 | same contract line, index `verified`; needs a `PATH` without an installed `codemap-py`, else the A_plain isolation guard stops it                                                                                                                                                                                |
| `bash benchmarks/run-all.sh codex --struct --dry-run`   |    1 | same contract line; `run-all.sh` rebuilt the index with its own build step and normalised it to exactly `6c2ec534…` (`verified`); the patch bundle installed all five locked digests; then the B_auto staged direct-CLI admission query stopped with `⚠ codemap index stale — 1 file(s) changed since last scan` |

The structural dry-run ran with `CODEMAP_BENCH_PATCH_PYTEST` set to an existing pytest so the patch-stage runtime was not installed. Its last failure is not an index-pin mismatch: every lock and digest gate before it passed. The same index, checked with the generation 16 query code from the canonical root, reports no changed file. Which file the staged B_auto runtime counts as changed was not identified; that freshness disagreement is open.

## Historical patch indexes: root cause and fix

Since generation 14 the scanner writes `scan_root_key`, a digest of the scanned root path, next to `scan_root`. `prepare-codex-index.py --prepare-patch-bundle` scans each task in a random temporary worktree and relocates the path strings to the canonical root, but not that digest. The installed bytes therefore carried a digest of a deleted temporary path, changed on every run, and could never match `raw_sha256_at_canonical_root`. Two independent generation 16 captures before the fix gave identical semantic digests and module counts for all five tasks, and different raw digests for all five. The semantic digest excludes `scan_root_key`, which is why only the raw lock broke.

Fix: after relocation the preparer recomputes `scan_root_key` for the relocated root with the scanner's own `root_key` helper, loaded from the same checkout as the `scan-index` it runs. The stored key now matches the stored `scan_root`, the query-side scan-root check accepts the installed index, and repeated runs install identical bytes. Two regression tests in `benchmarks/tests/entrypoints/test_prepare_codex_index.py` fail on the unfixed preparer (different raw digests on two runs; stored key not equal to the root key of the stored root) and pass on the fixed one.

After the fix, two more independent captures were byte-identical, and the real `--prepare-patch-bundle` with the live `scan-index` passed twice at the canonical root, raw byte check included.

| Task  | Commit     | `module_count` | `raw_sha256_at_canonical_root` before (13) | after (16)                                                         | `semantic_sha256` before (13) | after (16)                                                         |
| ----- | ---------- | -------------: | ------------------------------------------ | ------------------------------------------------------------------ | ----------------------------- | ------------------------------------------------------------------ |
| PT-01 | `8e805f92` |            646 | `c9755d74…`                                | `1ce0cffa425c8c365a64524b4c56b6672386c2d1c2c710db94ff910b0c4b4df7` | `9d0c4d53…`                   | `8199218158333533a18470478be46b018e34fb1955d63d297aa4ad77ff4c137c` |
| PT-02 | `aa0ee0d1` |            646 | `b4fbe558…`                                | `c00c292afdb09a38b93391ea3bd51eba635f381ea001b10e7790d1d266045a0e` | `06d7efdf…`                   | `de6df438678dd53f574d40152780c1d562f0415053f9c6f08420bf6eb00a1b1a` |
| PT-03 | `9df1910f` |            646 | `eae63ec0…`                                | `570e4b66b2bce04c2b28fbc405d2aed3ebf7c1239eb263cb4fd6ca6767cb147e` | `8160e882…`                   | `a009b36f1565836487f019faad9c809255d3412be57ddddae06317c34b7c2b70` |
| PT-04 | `3876cc52` |            644 | `b921f03d…`                                | `fd5ae1a4f0c0f88e1c3075be5ef5fa3e5cc35e5c43db5ac3505b34059be9facd` | `872fe553…`                   | `877759128862caf4873cff967b1f8c7b541355cb639ffd2a48e327b58b5e6815` |
| PT-05 | `b15d394f` |            643 | `2d13e9a4…`                                | `40de11eeddb28a4bf5cea4c7fd5697a533c9b37d40dc8a4e11cb3f299040b37e` | `1268c6b6…`                   | `b05c60899be7f7822714b697e1fdc57081fe48f1fde37f147d64f830ba729868` |

All five patch locks now carry `scan_version` 16 and `scanned_at` `2026-11-16T00:00:00+00:00`; module counts are unchanged.

## Drift guard

`benchmarks/tests/manifests/test_index_pin_drift.py` fails when the methodology builder's `INDEX_LOCK` or any task in `patch-index-locks.json` names a `scan_version` other than the live `SCAN_VERSION` in `plugins/codemap-py/src/codemap_py/schema.py`, which it reads with `ast`, without importing the scanner. Before this relock it failed on all six pins (13 against 16); it now passes. A generation bump without a relock now fails in the test suite instead of at benchmark preflight. The guard covers the generation only: graph content is pinned by the semantic and raw digests, which the preparer checks at run time.

## Re-running the relock

The digests depend on the exact scanner source, so any scanner change before or after this relock needs the sequence below again. Run it from the repository root on macOS (the raw digests reproduce only at the canonical root). Save the capture script from the block at the end of this section as `relock-capture.py` outside the repository first. Applied to the scanner this record was made with, the sequence reproduces the pins above byte for byte, including the whole `patch-index-locks.json`.

```bash
R="$(python3 -c 'import os; from pathlib import Path; print((Path(os.sep) / "tmp").resolve())')/codemap-provider-parity-pl-2.6.5"
OUT="$(mktemp -d)"
STAMP="$(date -u +%Y-%m-%d)T00:00:00+00:00"
PY=.venv/bin/python
MANIFESTS=(--manifest-path benchmarks/manifests/codex-integration.json --methodology-path benchmarks/manifests/provider-parity-methodology.json --schema-path plugins/codemap-py/src/codemap_py/schema.py)

# 1. Canonical clone at the pinned commits; nothing from it is executed.
[ -d "$R/.git" ] || git clone --depth 1 --branch 2.6.5 https://github.com/Lightning-AI/pytorch-lightning.git "$R"
git -C "$R" fetch --depth 1 origin 8e805f9268043c9aa8f0d70800be537b56a93c19 aa0ee0d18d49c6b26f18d34a3473f177adefc262 9df1910f0833100478886bef0a08b450ff2d0c14 3876cc525d2678463199407ca48230d3eba09461 b15d394f2a9d36ccefba08328ac8dc2bd13e49b2
git -C "$R" reset --hard 2.6.5 && git -C "$R" clean -fd

# 2. Capture twice in separate processes; both outputs must be byte-identical.
for n in 1 2; do
    "$PY" relock-capture.py --repo-root . --source-root "$R" --scanner plugins/codemap-py \
        --codemap-python "$(command -v python3.11)" --scanned-at "$STAMP" \
        --out "$OUT/capture-$n.json" --locks-out "$OUT/locks-$n.json"
done
cmp "$OUT/capture-1.json" "$OUT/capture-2.json" && cmp "$OUT/locks-1.json" "$OUT/locks-2.json"

# 3. Pins: the patch locks are written whole; the main index lock is three edits by hand.
cp "$OUT/locks-1.json" benchmarks/suites/patch-index-locks.json
"$PY" -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1]))["main"], indent=2))' "$OUT/capture-1.json"
#   In benchmarks/build-provider-parity-methodology-manifest.py INDEX_LOCK set raw_sha256, semantic_sha256,
#   module_count and scan_version from the printed "main" block and scanned_at to "$STAMP".
#   In benchmarks/tests/manifests/test_codex_agentic_active_manifest.py set the frozen index raw_sha256 to the same raw digest.

# 4. Regenerate the manifests and run every gate.
for b in build-provider-parity-methodology-manifest build-codex-integration-manifest build-codex-agentic-manifest; do
    "$PY" "benchmarks/$b.py" && "$PY" "benchmarks/$b.py" --check
done
"$PY" benchmarks/prepare-codex-index.py "${MANIFESTS[@]}" --print-contract
"$PY" benchmarks/prepare-codex-index.py --index-path "$R/.cache/codemap/$(basename "$R").json" --source-root "$R" "${MANIFESTS[@]}"
"$PY" benchmarks/prepare-codex-index.py --index-path "$R/.cache/codemap/$(basename "$R").json" "${MANIFESTS[@]}" --require-hash --verify
"$PY" benchmarks/prepare-codex-index.py --prepare-patch-bundle --source-root "$R" --patch-locks-path benchmarks/suites/patch-index-locks.json --scan-index-bin plugins/codemap-py/bin/scan-index
"$PY" -m pytest -n 4 -q -p no:cacheprovider benchmarks/tests

# 5. Dry-run preflight, one at a time. An installed codemap-py on PATH trips the A_plain isolation guard,
#    and a preset pytest keeps the structural lane from installing the patch-stage test runtime.
CLEAN_PATH="$(printf '%s' "$PATH" | tr ':' '\n' | grep -v '/codemap-py/' | paste -sd: -)"
for lane in "claude --struct" "claude --agentic" "codex --agentic" "codex --struct"; do
    PATH="$CLEAN_PATH" CODEMAP_BENCH_PATCH_PYTEST="$PWD/.venv/bin/pytest" bash benchmarks/run-all.sh $lane --dry-run
done
```

<details>
<summary>relock-capture.py</summary>

```python
"""Capture benchmark index identities for a relock with the preparer's own helpers.

Main index: scanned in place at the canonical root, normalised as ``prepare_index`` does. Patch indexes: scanned in a
throwaway worktree per task and relocated as ``_install_patch_task_index`` does (root key recomputed). Writes the
captured identities as JSON and, with ``--locks-out``, a complete ``patch-index-locks.json`` ready to copy into place.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--repo-root", type=Path, required=True)
parser.add_argument("--source-root", type=Path, required=True)
parser.add_argument("--scanner", type=Path, required=True, help="codemap-py plugin directory (bin/, src/)")
parser.add_argument("--codemap-python", required=True, help="Python 3.11 used for the main scan, as run-all.sh does")
parser.add_argument("--scanned-at", required=True)
parser.add_argument("--out", type=Path, required=True)
parser.add_argument("--locks-out", type=Path)
args = parser.parse_args()
repo_root = args.repo_root.resolve(strict=True)
source_root = args.source_root.resolve(strict=True)
scanner = args.scanner.resolve(strict=True)
scan_index = scanner / "bin" / "scan-index"

spec = importlib.util.spec_from_file_location("prep", repo_root / "benchmarks" / "prepare-codex-index.py")
prep = importlib.util.module_from_spec(spec)
sys.path.insert(0, str(repo_root / "benchmarks"))
spec.loader.exec_module(prep)

result = {"scanner": str(scanner), "python_patch": sys.executable, "codemap_python": args.codemap_python}

index_path = source_root / ".cache" / "codemap" / f"{source_root.name}.json"
index_path.unlink(missing_ok=True)
subprocess.run(
    [str(scanner / "bin" / "codemap-py"), "index", "--root", str(source_root)],
    check=True,
    env=dict(os.environ, CODEMAP_PYTHON=args.codemap_python),
    capture_output=True,
    text=True,
)
payload = json.loads(index_path.read_text(encoding="utf-8"))
payload.update(project=source_root.name, scan_root=str(source_root), scanned_at=args.scanned_at)
result["main"] = {
    "scan_version": payload["scan_version"],
    "module_count": len(payload["modules"]),
    "semantic_sha256": prep.semantic_index_sha256(payload, source_root),
    "raw_sha256": hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode("utf-8")).hexdigest(),
}

locks_path = repo_root / "benchmarks" / "suites" / "patch-index-locks.json"
locks = json.loads(locks_path.read_text(encoding="utf-8"))
work = Path(tempfile.mkdtemp(prefix="relock-capture-")).resolve(strict=True)
result["patch"] = {}
for task_id, lock in sorted(locks["tasks"].items()):
    worktree = work / task_id
    prep._git(source_root, "worktree", "add", "--detach", str(worktree), lock["baseline_commit"])
    try:
        subprocess.run(
            [sys.executable, str(scan_index), "--root", str(worktree)],
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
        scanned = worktree / ".cache" / "codemap" / f"{worktree.name}.json"
        patch = prep._replace_root(json.loads(scanned.read_text(encoding="utf-8")), str(worktree), str(source_root))
        patch.update(project=f"provider-parity-{task_id}", scan_root=str(source_root), scanned_at=args.scanned_at)
        if "scan_root_key" in patch:
            patch["scan_root_key"] = prep._scanner_root_key(scan_index, source_root)
        encoded = (json.dumps(patch, indent=2, sort_keys=True) + "\n").encode("utf-8")
        result["patch"][task_id] = {
            "scan_version": patch["scan_version"],
            "module_count": len(patch["modules"]),
            "semantic_sha256": prep.semantic_index_sha256(patch, source_root),
            "raw_sha256_at_canonical_root": hashlib.sha256(encoded).hexdigest(),
        }
    finally:
        failure = prep._release_worktree(source_root, worktree, task_id)
        if failure:
            print("WARNING", failure, file=sys.stderr)

args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
if args.locks_out is not None:
    for task_id, captured in result["patch"].items():
        locks["tasks"][task_id].update(captured, scanned_at=args.scanned_at)
    args.locks_out.write_text(json.dumps(locks, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(json.dumps(result["main"], indent=2, sort_keys=True))
```

</details>
