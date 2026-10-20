#!/usr/bin/env python3
"""Retain optimization progress and validate descriptive experiment evidence.

## Purpose

Provide deterministic checkpoints, source identity checks, and numerical summaries for the optimize skill without
executing user measurement commands.

## Scope

Numeric managed campaigns and retained ablation receipts only; historical single/campaign pass-fail workflows remain
outside this opt-in data contract.

## Usage

Invoke through the plugin Python launcher with init, begin, finish, resume, validate, snapshot, retro, ablation, or
admit and --run-dir. Inspect --help for inputs.

## Outputs

Init freezes schema-one campaign.json and creates experiments.jsonl; begin creates pending.json, finish appends one
durable validated event, and resume can remove an already-completed pending marker after verifying live source. Other
operations return JSON summaries without editing source or executing metric/guard commands. Each campaign has one
writer, enforced by workflow ownership.

## Failure

Invalid configuration, corrupted history, nonfinite measurements, source drift, unresolved pending work, or inconsistent
receipts exits nonzero with a specific diagnostic. No automatic rollback, truncation, commit, or replay occurs.

## Used by

Optimize mode instructions, shared artifact admission and portable real Git acceptance tests. Source fingerprints
describe retained bytes, not provenance of measurement execution; the parent verifies actual commands and source reads.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import stat
import subprocess
from pathlib import Path, PureWindowsPath
from typing import Any


def digest(value: Any) -> str:
    """Hash a canonical JSON value independently of formatting."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def read(path: Path) -> Any:
    """Read JSON without accepting nonfinite constants."""
    return json.loads(path.read_text(encoding="utf-8"), parse_constant=lambda value: number(value))


def number(value: Any) -> float:
    """Require a finite numeric measurement, excluding booleans."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("finite-numeric-measurement-required")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("finite-numeric-measurement-required")
    return result


def config(value: Any) -> dict[str, Any]:
    """Validate the fixed configuration used throughout one numeric campaign."""
    if not isinstance(value, dict):
        raise ValueError("config-object-required")
    for key in ("goal", "metric_cmd", "guard_cmd"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise ValueError(f"config-required:{key}")
    if value.get("metric_direction") not in {"higher", "lower"}:
        raise ValueError("metric-direction-required")
    budget = value.get("max_iterations")
    if type(budget) is not int or budget < 1:
        raise ValueError("positive-iteration-budget-required")
    if number(value.get("min_delta", 0)) < 0:
        raise ValueError("nonnegative-min-delta-required")
    files = value.get("scope_files")
    if not isinstance(files, list) or not files or len(set(files)) != len(files):
        raise ValueError("explicit-scope-files-required")
    for name in files:
        if (
            not isinstance(name, str)
            or not name
            or Path(name).is_absolute()
            or PureWindowsPath(name).is_absolute()
            or ".." in Path(name).parts
            or "\\" in name
            or ".git" in Path(name).parts
        ):
            raise ValueError("unsafe-scope-file")
    return value


def git(root: Path, *arguments: str) -> bytes:
    """Read local Git state without shell evaluation or environment redirection."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    return subprocess.check_output(["git", "-C", str(root), *arguments], env=env, stderr=subprocess.PIPE)  # noqa: S603,S607 - fixed local read-only Git recipes, no shell


def file_identity(root: Path, name: str) -> dict[str, Any] | None:
    """Fingerprint actual source bytes or a leaf link without traversing linked directories."""
    path = root / name
    if any(parent.is_symlink() for parent in path.parents if parent != root and parent.is_relative_to(root)):
        raise ValueError("tracked-source-symlink-ancestor")
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(mode):
        return {"symlink_sha256": hashlib.sha256(os.fsencode(os.readlink(path))).hexdigest()}
    if not stat.S_ISREG(mode):
        raise ValueError("tracked-source-not-regular-or-symlink")
    return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "executable": bool(mode & 0o111)}


def snapshot(root: Path, settings: dict[str, Any]) -> dict[str, Any]:
    """Bind HEAD, tracked changes and scoped additions/deletions to portable bytes."""
    root = root.resolve(strict=True)
    if Path(git(root, "rev-parse", "--show-toplevel").decode().strip()).resolve() != root:
        raise ValueError("repository-root-required")
    # Diff output omits these entries; refuse them without mutating user index flags.
    if any(
        entry[:1].islower() or entry[:1] == b"S" for entry in git(root, "ls-files", "-v", "-z").split(b"\0") if entry
    ):
        raise ValueError("unsupported-hidden-index-entry")
    scope = {Path(name).as_posix() for name in settings["scope_files"]}
    for name in scope:
        path = root / name
        if not path.resolve().is_relative_to(root) or path.is_symlink():
            raise ValueError("scope-file-escape-or-symlink")
    tracked = set(scope)
    # HEAD retains staged deletions; the index additionally names staged additions.
    for listing in (git(root, "ls-tree", "-r", "-z", "HEAD"), git(root, "ls-files", "--stage", "-z")):
        for entry in listing.split(b"\0"):
            if not entry:
                continue
            info, _, name = entry.partition(b"\t")
            if info.split()[0] == b"160000":
                raise ValueError("unsupported-source-submodule")
            tracked.add(os.fsdecode(name))
    identities = {name: file_identity(root, name) for name in sorted(tracked)}
    files = {name: identities[Path(name).as_posix()] for name in settings["scope_files"]}
    # Literal exclusions prevent pathspec metacharacters from widening owned scope.
    exclusions = [f":(exclude,literal){name}" for name in sorted(scope)]
    state = {
        "head": git(root, "rev-parse", "HEAD").decode().strip(),
        "tracked_diff": hashlib.sha256(git(root, "diff", "--binary", "--no-ext-diff", "HEAD", "--")).hexdigest(),
        "outside_scope": hashlib.sha256(
            git(root, "diff", "--binary", "--no-ext-diff", "HEAD", "--", ".", *exclusions)
        ).hexdigest(),
        "index": hashlib.sha256(git(root, "diff", "--cached", "--binary", "--no-ext-diff", "HEAD", "--")).hexdigest(),
        "files": files,
        "tracked_bytes": digest(identities),
        "outside_scope_bytes": digest({name: identity for name, identity in identities.items() if name not in scope}),
    }
    # The portable digest omits the index checksum, but reconstruction still needs
    # staged entry membership to reproduce Git diff views alongside actual raw bytes.
    return state | {"digest": digest({key: value for key, value in state.items() if key != "index"})}


def write(path: Path, value: Any) -> None:
    """Create one record exclusively so existing evidence is never overwritten."""
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def improvement(before: float, after: float, settings: dict[str, Any]) -> float:
    """Return positive movement in the configured metric direction."""
    return (after - before) * (1 if settings["metric_direction"] == "higher" else -1)


def history(run: Path) -> tuple[dict[str, Any], list[dict[str, Any]], float, dict[str, Any]]:
    """Validate every transition and derive progress without mutable counters."""
    state = read(run / "campaign.json")
    if state.get("schema_version") != 1:
        raise ValueError("unsupported-campaign-schema")
    settings = config(state["config"])
    if state.get("config_sha256") != digest(settings):
        raise ValueError("config-mismatch")
    best = number(state["baseline"])
    source = state["source"]
    if source["digest"] != digest({k: v for k, v in source.items() if k not in {"digest", "index"}}):
        raise ValueError("baseline-source-digest-mismatch")
    records = []
    for index, line in enumerate((run / "experiments.jsonl").read_text(encoding="utf-8").splitlines(), 1):
        try:
            record = json.loads(line)
            metric = number(record["metric_after"])
            if (
                record["iteration"] != index
                or record["metric_before"] != best
                or not isinstance(record["hypothesis"], str)
                or not record["hypothesis"].strip()
            ):
                raise ValueError("progression")
            if record["previous_source"] != source or number(record["delta"]) != metric - best:
                raise ValueError("transition-mismatch")
            if record["guard"] not in {"pass", "fail"} or record["decision"] not in {
                "kept",
                "reverted",
                "inconclusive",
                "failed",
            }:
                raise ValueError("decision")
            next_source = record["source"]
            if next_source["digest"] != digest({k: v for k, v in next_source.items() if k not in {"digest", "index"}}):
                raise ValueError("source-digest")
            if record["decision"] == "kept":
                gain = improvement(best, metric, settings)
                if record["guard"] != "pass" or gain <= 0 or gain < number(settings.get("min_delta", 0)):
                    raise ValueError("invalid-kept-result")
                if any(
                    next_source[key] != source[key] for key in ("head", "outside_scope", "outside_scope_bytes", "index")
                ):
                    raise ValueError("out-of-scope-change")
                best = metric
            elif next_source != source:
                raise ValueError("rollback-source-mismatch")
            source = next_source
            records.append(record)
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"invalid-iteration-record:{index}:{error}") from error
    if len(records) > settings["max_iterations"]:
        raise ValueError("iteration-budget-exceeded")
    return state, records, best, source


def inspect(run: Path, *, live: bool, schedule: bool = False, reconcile: bool = False) -> dict[str, Any]:
    """Check retained progress and optionally verify the exact live checkout."""
    state, records, best, source = history(run)
    pending_path = run / "pending.json"
    if live and snapshot(Path(state["root"]), state["config"]) != source:
        raise ValueError("source-mismatch")
    if pending_path.exists():
        pending = read(pending_path)
        if (
            records
            and pending
            == {
                "iteration": len(records),
                "hypothesis": records[-1]["hypothesis"],
                "source": records[-1]["previous_source"],
            }
            and reconcile
            and live
        ):
            pending_path.unlink()
        else:
            raise ValueError("pending-iteration:inspect-and-finish-recorded-attempt-before-resume")
    if schedule and len(records) >= state["config"]["max_iterations"]:
        raise ValueError("iteration-budget-exhausted")
    return {
        "next_iteration": len(records) + 1,
        "best_metric": best,
        "best_source": source,
        "config_sha256": state["config_sha256"],
    }


def initialize(args: argparse.Namespace) -> dict[str, Any]:
    """Freeze measured initial state without replacing an existing campaign."""
    settings = config(read(args.config))
    root = args.root.resolve(strict=True)
    source = snapshot(root, settings)
    run = args.run_dir.resolve()
    if any(
        (root / name).resolve() == run or run.is_relative_to((root / name).resolve())
        for name in settings["scope_files"]
    ):
        raise ValueError("artifacts-overlap-source")
    if run.is_relative_to(root):
        relative = run.relative_to(root).as_posix()
        if git(root, "ls-files", "--", relative).strip():
            raise ValueError("tracked-campaign-artifacts")
    run.mkdir(parents=True, exist_ok=True)
    if any((run / name).exists() for name in ("campaign.json", "experiments.jsonl", "pending.json")):
        raise ValueError("campaign-already-exists")
    write(
        run / "campaign.json",
        {
            "schema_version": 1,
            "root": root.as_posix(),
            "config": settings,
            "config_sha256": digest(settings),
            "baseline": number(args.baseline),
            "source": source,
        },
    )
    (run / "experiments.jsonl").touch(exist_ok=False)
    return inspect(run, live=True)


def finish(args: argparse.Namespace) -> dict[str, Any]:
    """Validate an attempt, append it durably, then clear its pending marker."""
    run = args.run_dir
    state, records, best, previous = history(run)
    pending = read(run / "pending.json")
    if (
        not isinstance(pending, dict)
        or set(pending) != {"iteration", "hypothesis", "source"}
        or type(pending["iteration"]) is not int
        or not isinstance(pending["hypothesis"], str)
        or not pending["hypothesis"].strip()
    ):
        raise ValueError("invalid-pending-record")
    if pending.get("iteration") != len(records) + 1 or pending.get("source") != previous:
        raise ValueError("pending-iteration-mismatch:use-resume-to-reconcile-recorded-result")
    metric = number(args.metric)
    source = snapshot(Path(state["root"]), state["config"])
    if args.decision == "kept":
        if args.guard != "pass":
            raise ValueError("guard-regression")
        gain = improvement(best, metric, state["config"])
        if gain <= 0 or gain < number(state["config"].get("min_delta", 0)):
            raise ValueError("improvement-below-threshold")
        if any(source[key] != previous[key] for key in ("head", "outside_scope", "outside_scope_bytes", "index")):
            raise ValueError("out-of-scope-change")
    elif source != previous:
        raise ValueError("rollback-source-mismatch")
    if len(records) >= state["config"]["max_iterations"]:
        raise ValueError("iteration-budget-exhausted")
    record = {
        "iteration": len(records) + 1,
        "hypothesis": pending["hypothesis"],
        "metric_before": best,
        "metric_after": metric,
        "delta": metric - best,
        "guard": args.guard,
        "decision": args.decision,
        "previous_source": previous,
        "source": source,
        "rollback_evidence": "exact-source-restoration" if args.decision != "kept" else "not-required",
    }
    # Appending is the commit point; resume reconciles an interrupted marker removal.
    log = run / "experiments.jsonl"
    prefix = "\n" if records and not log.read_bytes().endswith(b"\n") else ""
    serialized = json.dumps(record, allow_nan=False)
    with log.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(prefix + serialized + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    (run / "pending.json").unlink()
    return inspect(run, live=True)


def retrospective(run: Path) -> dict[str, Any]:
    """Summarize adaptive iterations without inventing inferential statistics."""
    state, records, best, _ = history(run)
    counts = {
        key: sum(row["decision"] == key for row in records) for key in ("kept", "reverted", "inconclusive", "failed")
    }
    hypotheses = [row["hypothesis"] for row in records]
    return {
        "baseline": state["baseline"],
        "best": best,
        "delta": best - state["baseline"],
        **counts,
        "repeated_hypotheses": sorted({item for item in hypotheses if hypotheses.count(item) > 1}),
        "next_hypotheses": list(dict.fromkeys(row["hypothesis"] for row in records if row["decision"] != "kept")),
        "statistical_inference": "not-supported-by-adaptive-iteration-log",
        "pending": (run / "pending.json").exists(),
    }


def evidence(run: Path, value: Any) -> bytes:
    """Require a digest-bound regular output file contained in the evidence root."""
    run = run.resolve(strict=True)
    path = run / value["path"]
    if (
        Path(value["path"]).is_absolute()
        or PureWindowsPath(value["path"]).is_absolute()
        or any(part.is_symlink() for part in (path, *path.parents) if part != run and part.is_relative_to(run))
        or not path.resolve().is_relative_to(run.resolve())
        or not path.is_file()
    ):
        raise ValueError("evidence-path-escape")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != value["sha256"]:
        raise ValueError("evidence-hash-mismatch")
    return data


def ablation(run: Path, value: dict[str, Any]) -> dict[str, Any]:
    """Rank component removals only after validating their retained measurements."""
    if value.get("schema_version") != 1 or value.get("measurement_basis") != "worktree":
        raise ValueError("worktree-independent-measurements-required")
    if value.get("metric_direction") not in {"higher", "lower"}:
        raise ValueError("metric-direction-required")
    reference = value["reference"]
    if reference["guard_exit_code"] != 0:
        raise ValueError("reference-guard-failed")
    if reference["source_before"] != value["frozen_source"]:
        raise ValueError("reference-source-mismatch")
    rows = [reference, *value["variants"]]
    names = [row["name"] for row in rows]
    if not value["variants"] or len(names) != len(set(names)):
        raise ValueError("unique-nonempty-variants-required")
    ranked, excluded = [], []
    for row in rows:
        if (
            not isinstance(row["name"], str)
            or not row["name"].strip()
            or row["frozen_config"] != value["frozen_config"]
            or row["source_before"] != row["source_after"]
        ):
            raise ValueError("measurement-identity-mismatch")
        for key in ("source_before", "frozen_config"):
            token = row[key]
            if not isinstance(token, str) or len(token) != 64 or any(c not in "0123456789abcdef" for c in token):
                raise ValueError("measurement-digest-required")
        metric = number(row["metric"])
        if (
            type(row["metric_exit_code"]) is not int
            or type(row["guard_exit_code"]) is not int
            or row["metric_exit_code"] != 0
            or number(evidence(run, row["metric_output"]).decode().strip()) != metric
        ):
            raise ValueError("metric-output-mismatch")
        evidence(run, row["guard_output"])
        if row is reference:
            continue
        if row["guard_exit_code"] != 0:
            excluded.append(row["name"])
            continue
        loss = (metric - number(reference["metric"])) * (1 if value["metric_direction"] == "lower" else -1)
        ranked.append({"name": row["name"], "metric": metric, "loss": loss})
    return {
        "ranked": sorted(ranked, key=lambda row: -row["loss"]),
        "excluded": excluded,
        "limit": "one-component-removals-do-not-estimate-interactions",
    }


def admit(run: Path, result: dict[str, Any]) -> dict[str, Any]:
    """Bind managed evidence at result admission while preserving historical runs."""
    lifecycle = result.get("metadata", {}).get("optimize_lifecycle")
    markers = any((run / name).exists() for name in ("campaign.json", "pending.json", "ablation-input.json"))
    if lifecycle is None:
        if markers:
            raise ValueError("optimize-lifecycle-metadata-required")
        return {"mode": "historical"}
    if lifecycle.get("schema_version") != 1:
        raise ValueError("unsupported-optimize-lifecycle-schema")
    mode = lifecycle["mode"]
    if mode not in {"campaign", "resume", "retrospective", "ablation"}:
        raise ValueError("unsupported-optimize-lifecycle-mode")
    names = ("ablation-input.json",) if mode == "ablation" else ("campaign.json", "experiments.jsonl")
    if set(lifecycle["bindings"]) != set(names):
        raise ValueError("optimize-lifecycle-bindings-mismatch")
    for name in names:
        evidence(run, {"path": name, "sha256": lifecycle["bindings"][name]})
    if mode == "ablation":
        data = read(run / "ablation-input.json")
        summary = ablation(run, data)
        baseline = number(data["reference"]["metric"])
        rows = [
            [
                row["name"],
                baseline,
                number(row["metric"]),
                (number(row["metric"]) - baseline) * (1 if data["metric_direction"] == "lower" else -1),
                "pass" if row["guard_exit_code"] == 0 else "fail",
                "ranked" if row["guard_exit_code"] == 0 else "excluded",
            ]
            for row in data["variants"]
        ]
        return summary | {"result_rows": [rows]}
    summary = inspect(run, live=mode != "retrospective")
    state, records, best, _ = history(run)
    rows = [
        [str(row["iteration"]), row["metric_before"], row["metric_after"], row["delta"], row["guard"], row["decision"]]
        for row in records
    ]
    decision = (
        "kept"
        if any(row["decision"] == "kept" for row in records)
        else records[-1]["decision"]
        if records
        else "inconclusive"
    )
    aggregate = [
        "campaign",
        number(state["baseline"]),
        best,
        best - number(state["baseline"]),
        "pass" if all(row["guard"] == "pass" for row in records) else "fail",
        decision,
    ]
    return summary | {"result_rows": [rows, [aggregate]] if rows else [[aggregate]]}


def main() -> int:
    """Dispatch explicit local operations and print actionable failure diagnostics."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("init", "begin", "finish", "resume", "validate", "snapshot", "retro", "ablation", "admit")
    )
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--baseline")
    parser.add_argument("--hypothesis")
    parser.add_argument("--metric")
    parser.add_argument("--guard", choices=("pass", "fail"))
    parser.add_argument("--decision", choices=("kept", "reverted", "inconclusive", "failed"))
    args = parser.parse_args()
    try:
        if args.action == "init":
            result = initialize(args)
        elif args.action == "snapshot":
            result = snapshot(args.root, config(read(args.config)))
        elif args.action == "admit":
            result = admit(args.run_dir, read(args.input))
        elif args.action == "ablation":
            result = ablation(args.run_dir, read(args.input))
        elif args.action == "retro":
            result = retrospective(args.run_dir)
        elif args.action == "finish":
            if not args.guard or not args.decision:
                raise ValueError("guard-and-decision-required")
            result = finish(args)
        else:
            state = read(args.run_dir / "campaign.json")
            if args.config and config(read(args.config)) != state["config"]:
                raise ValueError("config-mismatch")
            result = inspect(
                args.run_dir, live=True, schedule=args.action in {"begin", "resume"}, reconcile=args.action == "resume"
            )
            if args.action == "begin":
                if not args.hypothesis or not args.hypothesis.strip():
                    raise ValueError("hypothesis-required")
                write(
                    args.run_dir / "pending.json",
                    {
                        "iteration": result["next_iteration"],
                        "hypothesis": args.hypothesis,
                        "source": result["best_source"],
                    },
                )
        print(json.dumps(result, allow_nan=False))
    except (ValueError, TypeError, KeyError, AttributeError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"optimize-campaign: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
