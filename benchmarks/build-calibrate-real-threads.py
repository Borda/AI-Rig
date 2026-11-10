"""Freeze public duplicate-closed GitHub issues as calibration problems for ``/oss:analyse``.

Example:
    python benchmarks/build-calibrate-real-threads.py --repo roboflow/rf-detr --count 3 --negatives 1 --dry_run

Each written file is one schema-v2 problem in the calibrate pipeline (see ``_bench_calibrate.real_threads``). A batch
holds ``count`` duplicate problems and ``negatives`` negative controls: issues closed as completed by a pull request,
whose correct answer is that no candidate is a duplicate. Existing files are never overwritten: frozen problems are
immutable benchmark coordinates.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import fire
from _bench_calibrate import real_threads

DEFAULT_OUT = Path(__file__).resolve().parent / "calibrate" / "oss-analyse" / "real"

Builder = Callable[..., real_threads.BuiltProblem]


@dataclass(frozen=True)
class BatchOptions:
    """Where and how a batch is written: output directory, haystack size, base seed, and dry-run switch."""

    target: Path
    size: int
    seed: int
    dry_run: bool


def _report_line(built: real_threads.BuiltProblem) -> str:
    """Summarise one built problem: kind, canonical and its position, candidates, surviving comments, input size."""
    prov = built.problem["provenance"]
    if prov["canonical"] is None:
        answer = "negative control, canonical none"
    else:
        answer = f"canonical #{prov['canonical']} at {built.canonical_position}/{prov['candidates']}"
    return (
        f"{answer} via {prov['verdict_source']}, {prov['candidates']} candidates, {built.comments} comments,"
        f" {len(built.problem['input'])} chars"
    )


def _build_batch(repo: str, numbers: list[int], builder: Builder, want: int, opts: BatchOptions) -> int:
    """Build up to ``want`` problems from ``numbers`` in order with ``builder``; return how many are on disk or planned.

    Issues the builder rejects are reported with the reason and skipped. A failed ``gh`` call (transient API error)
    skips only that issue: no partial problem is ever written, and the rest of the batch still runs.
    """
    done = 0
    for number in numbers:
        if done >= want:
            break
        try:
            built = builder(repo, number, size=opts.size, seed=opts.seed + number)
        except real_threads.Skip as reason:
            print(f"skip {repo}#{number}: {reason}")
            continue
        except subprocess.CalledProcessError as err:
            print(f"skip {repo}#{number}: gh call failed: {(err.stderr or '').strip()[:200]}")
            continue
        path = opts.target / f"{built.problem['problem_id']}.json"
        if path.exists():
            print(f"keep {path} (frozen, not overwritten)")
            done += 1
            continue
        print(f"{'would write' if opts.dry_run else 'write'} {path}: {_report_line(built)}")
        if not opts.dry_run:
            opts.target.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(built.problem, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
            )
        done += 1
    return done


def main(
    repo: str,
    count: int = 3,
    negatives: int = 1,
    out_dir: str = str(DEFAULT_OUT),
    size: int = 20,
    seed: int = 0,
    dry_run: bool = False,
) -> None:
    """Build ``count`` duplicate problems and ``negatives`` negative controls from ``repo``, newest first.

    Issues without a maintainer verdict, whose pre-cutoff text would leak the answer, or that fail any other selection
    rule are skipped and reported with the reason. ``dry_run`` prints what would be written without touching disk.
    """
    opts = BatchOptions(target=Path(out_dir), size=size, seed=seed, dry_run=dry_run)
    duplicates = _build_batch(repo, real_threads.list_duplicate_issues(repo), real_threads.build_problem, count, opts)
    controls = _build_batch(
        repo, real_threads.list_completed_issues(repo), real_threads.build_negative_problem, negatives, opts
    )
    print(f"done: {duplicates} duplicate problem(s) and {controls} negative control(s) for {repo}")


if __name__ == "__main__":
    fire.Fire(main)
