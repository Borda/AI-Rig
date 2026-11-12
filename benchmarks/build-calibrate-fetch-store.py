"""Write the offline candidate-fetch store for frozen ``/oss:analyse`` calibration problems.

Example:
    python benchmarks/build-calibrate-fetch-store.py --dry_run

For each frozen problem in ``fixtures`` this writes ``<out_dir>/<problem_id>.json``: per candidate number of the
problem's pool, what the skill's candidate-verification fetch would have returned at the problem's cutoff (see
``_bench_calibrate.real_threads.build_fetch_store``). The calibrate pipeline hands that file to the solver in place of
the live fetch. Existing store files are never overwritten: like the problems, they are fixed benchmark coordinates.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import fire
from _bench_calibrate import real_threads

_SET = Path(__file__).resolve().parent / "calibrate" / "oss-analyse"


def main(
    fixtures: str = str(_SET / "real-problems"), out_dir: str = str(_SET / "real-fetch"), dry_run: bool = False
) -> None:
    """Build one store file per problem in ``fixtures`` that has none yet in ``out_dir``.

    A problem whose thread can no longer be cut at its recorded cutoff, or whose ``gh`` call fails, is reported and
    skipped; no partial store is written. ``dry_run`` prints what would be written without touching disk.
    """
    target = Path(out_dir)
    written = 0
    for path in sorted(Path(fixtures).glob("*.json")):
        store_path = target / path.name
        if store_path.exists():
            print(f"keep {store_path} (frozen, not overwritten)")
            continue
        problem = json.loads(path.read_text(encoding="utf-8"))
        try:
            store = real_threads.build_fetch_store(problem)
        except real_threads.Skip as reason:
            print(f"skip {path.name}: {reason}")
            continue
        except subprocess.CalledProcessError as err:
            print(f"skip {path.name}: gh call failed: {(err.stderr or '').strip()[:200]}")
            continue
        size = sum(len(entry["body"]) + sum(len(c["body"]) for c in entry["comments"]) for entry in store.values())
        pool = problem["provenance"]["candidates"]
        print(f"{'would write' if dry_run else 'write'} {store_path}: {len(store)}/{pool} candidates, {size} chars")
        if not dry_run:
            target.mkdir(parents=True, exist_ok=True)
            store_path.write_text(
                json.dumps(store, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
            )
            written += 1
    print(f"done: {written} store file(s) written to {target}")


if __name__ == "__main__":
    fire.Fire(main)
