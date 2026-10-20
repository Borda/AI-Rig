"""Exercise optimization checkpoints and retrospective reports through the public CLI."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from _platform import DIRECTORY_SYMLINKS_AVAILABLE, FILE_SYMLINKS_AVAILABLE, POSIX_FILE_MODES_AVAILABLE

HELPER = Path(__file__).resolve().parents[1] / "skills" / "optimize" / "campaign.py"


def cli(action: str, run: Path, *arguments: str, success: bool = True) -> dict:
    """Invoke the installed-style helper and retain meaningful error diagnostics."""
    result = subprocess.run(
        [sys.executable, str(HELPER), action, "--run-dir", str(run), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) == success, result.stdout + result.stderr
    return json.loads(result.stdout) if success else {"error": result.stderr}


@pytest.fixture
def campaign(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create a real committed source tree and an external campaign directory."""
    repo = tmp_path / "source"
    repo.mkdir()
    (repo / "value.txt").write_bytes(b"10\n")
    (repo / "obsolete.txt").write_bytes(b"old\n")
    (repo / "outside.txt").write_bytes(b"unrelated\n")
    env = os.environ | {
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }
    for args in (["init", "-q"], ["add", "."], ["commit", "-qm", "baseline"]):
        subprocess.run(["git", "-C", str(repo), *args], env=env, check=True, capture_output=True)
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "goal": "Reduce value",
                "metric_cmd": "read value.txt",
                "guard_cmd": "validate value.txt",
                "metric_direction": "lower",
                "scope_files": ["value.txt", "extra.txt", "obsolete.txt"],
                "max_iterations": 3,
                "min_delta": 1,
            }
        ),
        encoding="utf-8",
    )
    run = tmp_path / "run"
    cli("init", run, "--root", str(repo), "--config", str(config), "--baseline", "10")
    return repo, run, config


def test_real_campaign_resume_and_retrospective_preserve_history(campaign: tuple[Path, Path, Path]) -> None:
    """Keep a measured change, reject another, and resume at the next unfinished iteration."""
    repo, run, config = campaign
    assert cli("resume", run, "--config", str(config))["next_iteration"] == 1
    cli("begin", run, "--hypothesis", "Reduce redundant work")
    (repo / "value.txt").write_text("8\n", encoding="utf-8")
    cli("finish", run, "--metric", "8", "--guard", "pass", "--decision", "kept")
    first = (run / "experiments.jsonl").read_bytes()
    cli("begin", run, "--hypothesis", "Try another representation")
    cli("finish", run, "--metric", "9", "--guard", "pass", "--decision", "reverted")
    assert (run / "experiments.jsonl").read_bytes().startswith(first)
    resumed = cli("resume", run, "--config", str(config))
    assert resumed["next_iteration"] == 3
    assert resumed["best_metric"] == 8
    before = {p.name: p.read_bytes() for p in run.iterdir() if p.is_file()}
    retro = cli("retro", run)
    assert retro["kept"] == 1
    assert retro["reverted"] == 1
    assert retro["delta"] == -2
    assert retro["next_hypotheses"] == ["Try another representation"]
    assert retro["statistical_inference"] == "not-supported-by-adaptive-iteration-log"
    assert before == {p.name: p.read_bytes() for p in run.iterdir() if p.is_file()}


def test_resume_rejects_source_and_configuration_drift(
    text_newline_default: None, campaign: tuple[Path, Path, Path]
) -> None:
    """A matching run path cannot conceal changed source or measurement commands."""
    repo, run, config = campaign
    original = (repo / "value.txt").read_bytes()
    (repo / "value.txt").write_text("11\n", encoding="utf-8")
    assert "source-mismatch" in cli("resume", run, success=False)["error"]
    (repo / "value.txt").write_bytes(original)
    assert cli("resume", run)["next_iteration"] == 1
    data = json.loads(config.read_text())
    data["guard_cmd"] = "different guard"
    config.write_text(json.dumps(data), encoding="utf-8")
    assert "config-mismatch" in cli("resume", run, "--config", str(config), success=False)["error"]


def test_pending_iteration_is_never_replayed(campaign: tuple[Path, Path, Path]) -> None:
    """An interrupted edit leaves an explicit recovery condition, including after source is restored."""
    _, run, _ = campaign
    cli("begin", run, "--hypothesis", "Interrupted attempt")
    assert "pending-iteration" in cli("resume", run, success=False)["error"]
    assert "pending-iteration" in cli("begin", run, "--hypothesis", "Replay", success=False)["error"]
    cli("finish", run, "--metric", "10", "--guard", "fail", "--decision", "reverted")
    assert cli("resume", run)["next_iteration"] == 2


@pytest.mark.parametrize("metric", ["nan", "inf", "-inf"])
def test_nonfinite_measurements_do_not_append(campaign: tuple[Path, Path, Path], metric: str) -> None:
    """Invalid measurements retain pending work and leave the ledger untouched."""
    _, run, _ = campaign
    cli("begin", run, "--hypothesis", "Invalid metric")
    before = (run / "experiments.jsonl").read_bytes()
    assert (
        "finite"
        in cli("finish", run, "--metric=" + metric, "--guard", "pass", "--decision", "kept", success=False)["error"]
    )
    assert before == (run / "experiments.jsonl").read_bytes()
    assert (run / "pending.json").is_file()


def test_kept_regression_and_false_rollback_are_rejected(
    text_newline_default: None, campaign: tuple[Path, Path, Path]
) -> None:
    """Neither an improved number with failing guards nor un-restored source is accepted."""
    repo, run, _ = campaign
    original = (repo / "value.txt").read_bytes()
    cli("begin", run, "--hypothesis", "Unsafe improvement")
    (repo / "value.txt").write_text("8\n", encoding="utf-8")
    assert (
        "guard-regression"
        in cli("finish", run, "--metric", "8", "--guard", "fail", "--decision", "kept", success=False)["error"]
    )
    assert (
        "rollback-source-mismatch"
        in cli("finish", run, "--metric", "8", "--guard", "fail", "--decision", "reverted", success=False)["error"]
    )
    (repo / "value.txt").write_bytes(original)
    assert cli("finish", run, "--metric", "8", "--guard", "fail", "--decision", "reverted")["best_metric"] == 10


def test_corrupt_history_and_exhausted_budget_stop_resume(campaign: tuple[Path, Path, Path]) -> None:
    """Resume validates every record and honors the original total budget across restarts."""
    _, run, _ = campaign
    for _ in range(3):
        cli("begin", run, "--hypothesis", "No gain")
        cli("finish", run, "--metric", "10", "--guard", "pass", "--decision", "reverted")
    assert "iteration-budget-exhausted" in cli("resume", run, success=False)["error"]
    with (run / "experiments.jsonl").open("a", encoding="utf-8") as stream:
        stream.write('{"iteration": 99}\n')
    assert "invalid-iteration-record" in cli("retro", run, success=False)["error"]


def reconstruct_fixture_source(repo: Path, variant: Path, config: Path) -> None:
    """Rebuild regular-file fixture source from retained Git patches and exact raw identity."""
    names = set(json.loads(config.read_text())["scope_files"])
    for arguments in (("ls-tree", "-r", "--name-only", "-z", "HEAD"), ("ls-files", "-z")):
        names.update(
            os.fsdecode(name)
            for name in subprocess.check_output(["git", "-C", str(repo), *arguments]).split(b"\0")
            if name
        )
    retained: dict[str, tuple[bytes, int] | None] = {}
    for name in sorted(names):
        path = repo / name
        assert not any(part.is_symlink() for part in (path, *path.parents) if part.is_relative_to(repo))
        assert not path.exists() or path.is_file()
        retained[name] = (path.read_bytes(), path.stat().st_mode & 0o777) if path.exists() else None
    index_patch = subprocess.check_output(
        ["git", "-C", str(repo), "diff", "--cached", "--binary", "--no-ext-diff", "HEAD", "--"]
    )
    tracked_patch = subprocess.check_output(["git", "-C", str(repo), "diff", "--binary", "--no-ext-diff", "HEAD", "--"])
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "--detach", str(variant), "HEAD"], check=True, capture_output=True
    )
    if index_patch:
        subprocess.run(["git", "-C", str(variant), "apply", "--cached", "--binary"], input=index_patch, check=True)
    if tracked_patch:
        subprocess.run(["git", "-C", str(variant), "apply", "--binary"], input=tracked_patch, check=True)
    # Git patches reproduce normalized content; raw overlay preserves checkout conversions and ignored modes.
    for name, record in retained.items():
        path = variant / name
        if record is None:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(record[0])
            path.chmod(record[1])


@pytest.mark.parametrize("autocrlf", ["false", "true"])
def test_ablation_ranks_real_worktree_measurements(
    text_newline_default: None, campaign: tuple[Path, Path, Path], tmp_path: Path, autocrlf: str
) -> None:
    """Measure an actual isolated removal variant and rank only passing guards."""
    repo, run, config = campaign
    subprocess.run(["git", "-C", str(repo), "config", "core.autocrlf", autocrlf], check=True)
    (repo / "value.txt").write_text("9\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "value.txt"], check=True)
    if POSIX_FILE_MODES_AVAILABLE:
        subprocess.run(["git", "-C", str(repo), "config", "core.filemode", "false"], check=True)
        (repo / "value.txt").chmod((repo / "value.txt").stat().st_mode | 0o111)
    original_index = subprocess.check_output(["git", "-C", str(repo), "diff", "--cached", "--binary", "HEAD"])
    run = run.with_name("staged-run")
    cli("init", run, "--root", str(repo), "--config", str(config), "--baseline", "9")
    cli("begin", run, "--hypothesis", "Keep dirty best source")
    (repo / "value.txt").write_text("8\n", encoding="utf-8")
    (repo / "extra.txt").write_text("new\n", encoding="utf-8")
    (repo / "obsolete.txt").unlink()
    cli("finish", run, "--metric", "8", "--guard", "pass", "--decision", "kept")
    frozen = cli("snapshot", run, "--root", str(repo), "--config", str(config))
    variant = tmp_path / "variant"
    reconstruct_fixture_source(repo, variant, config)
    assert not (variant / "obsolete.txt").exists()
    assert cli("snapshot", run, "--root", str(variant), "--config", str(config)) == frozen
    assert subprocess.check_output(["git", "-C", str(repo), "diff", "--cached", "--binary", "HEAD"]) == original_index
    measurement = "from pathlib import Path; print(float(Path('value.txt').read_text()))"
    full = subprocess.check_output([sys.executable, "-c", measurement], cwd=repo, text=True)
    assert (
        float(full) == float(subprocess.check_output([sys.executable, "-c", measurement], cwd=variant, text=True)) == 8
    )
    (variant / "value.txt").write_text("13\n", encoding="utf-8")
    removed = subprocess.check_output([sys.executable, "-c", measurement], cwd=variant, text=True)
    changed = cli("snapshot", run, "--root", str(variant), "--config", str(config))
    config_hash = hashlib.sha256(
        json.dumps(json.loads(config.read_text()), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    records = []
    for name, metric, source, code in (
        ("reference", float(full), frozen["digest"], 0),
        ("remove shortcut", float(removed), changed["digest"], 0),
        ("unsafe removal", 99.0, changed["digest"], 1),
    ):
        record = {
            "name": name,
            "metric": metric,
            "source_before": source,
            "source_after": source,
            "frozen_config": config_hash,
            "metric_exit_code": 0,
            "guard_exit_code": code,
        }
        for kind, content in (("metric_output", str(metric)), ("guard_output", "guard result")):
            output = run / f"{len(records)}-{kind}.txt"
            output.write_text(content, encoding="utf-8")
            record[kind] = {"path": output.name, "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}
        records.append(record)
    data = {
        "schema_version": 1,
        "measurement_basis": "worktree",
        "metric_direction": "lower",
        "frozen_source": frozen["digest"],
        "frozen_config": config_hash,
        "reference": records[0],
        "variants": records[1:],
    }
    results = run / "ablation-input.json"
    results.write_text(json.dumps(data), encoding="utf-8")
    ranked = cli("ablation", run, "--input", str(results))
    assert ranked["ranked"] == [{"name": "remove shortcut", "metric": 13.0, "loss": 5.0}]
    assert ranked["excluded"] == ["unsafe removal"]
    rows = [
        ["remove shortcut", "8", "13", "5", "pass", "ranked"],
        ["unsafe removal", "8", "99", "91", "fail", "excluded"],
    ]
    accepted = managed_handoff(run, "ablation", rows)
    assert accepted.returncode == 0, accepted.stderr
    for column, wrong in ((0, "other component"), (1, "10"), (2, "1"), (3, "-5"), (4, "fail"), (5, "excluded")):
        corrupt = [list(row) for row in rows]
        corrupt[0][column] = wrong
        rejected = managed_handoff(run, "ablation", corrupt)
        assert rejected.returncode != 0
        assert "optimize-final-results-mismatch" in rejected.stderr
    rejected = managed_handoff(run, "ablation", rows[:1])
    assert rejected.returncode != 0
    assert "optimize-final-results-mismatch" in rejected.stderr
    assert (repo / "value.txt").read_text() == "8\n"
    data["reference"]["guard_exit_code"] = 1
    results.write_text(json.dumps(data), encoding="utf-8")
    assert "reference-guard-failed" in cli("ablation", run, "--input", str(results), success=False)["error"]
    data["reference"]["guard_exit_code"] = 0
    guard = "import subprocess; raise SystemExit(float(subprocess.check_output(['git', 'show', ':value.txt'])) >= 10)"
    assert float((variant / "value.txt").read_text()) == 13
    assert subprocess.run([sys.executable, "-c", guard], cwd=variant, check=False).returncode == 0
    assert subprocess.run([sys.executable, "-c", guard], cwd=repo, check=False).returncode == 0
    data["measurement_basis"] = "git-index"
    results.write_text(json.dumps(data), encoding="utf-8")
    assert (
        "worktree-independent-measurements-required"
        in cli("ablation", run, "--input", str(results), success=False)["error"]
    )


@pytest.mark.parametrize("membership", ["unchanged", "addition", "deletion"])
def test_ablation_reconstructs_staged_membership(
    campaign: tuple[Path, Path, Path], tmp_path: Path, membership: str
) -> None:
    """Preserve diff semantics when raw worktree bytes differ from staged entry membership."""
    repo, run, config = campaign
    if membership == "addition":
        (repo / "extra.txt").write_bytes(b"staged\n")
        subprocess.run(["git", "-C", str(repo), "add", "extra.txt"], check=True)
    elif membership == "deletion":
        subprocess.run(["git", "-C", str(repo), "rm", "--cached", "value.txt"], check=True, capture_output=True)
    run = run.with_name("membership-run")
    cli("init", run, "--root", str(repo), "--config", str(config), "--baseline", "10")
    cli("begin", run, "--hypothesis", "Change worktree independently of index")
    (repo / "value.txt").write_bytes(b"8\n")
    if membership == "addition":
        (repo / "extra.txt").write_bytes(b"unstaged\n")
    cli("finish", run, "--metric", "8", "--guard", "pass", "--decision", "kept")
    frozen = cli("snapshot", run, "--root", str(repo), "--config", str(config))
    variant = tmp_path / "membership-variant"
    reconstruct_fixture_source(repo, variant, config)
    assert cli("snapshot", run, "--root", str(variant), "--config", str(config)) == frozen
    assert cli("validate", run)["best_source"] == frozen


def test_checkpoint_append_crash_is_reconciled_without_duplicate(campaign: tuple[Path, Path, Path]) -> None:
    """A crash after the durable append cannot cause the same hypothesis to run twice."""
    repo, run, _ = campaign
    cli("begin", run, "--hypothesis", "Durable result")
    pending = (run / "pending.json").read_bytes()
    (repo / "value.txt").write_text("8\n", encoding="utf-8")
    cli("finish", run, "--metric", "8", "--guard", "pass", "--decision", "kept")
    history = (run / "experiments.jsonl").read_bytes()
    (run / "pending.json").write_bytes(pending)
    assert cli("resume", run)["next_iteration"] == 2
    assert history == (run / "experiments.jsonl").read_bytes()
    assert not (run / "pending.json").exists()


def test_retro_works_after_source_checkout_disappears(campaign: tuple[Path, Path, Path]) -> None:
    """Historical descriptive analysis does not depend on mutable live source."""
    repo, run, _ = campaign
    repo.rename(repo.with_name("moved-source"))
    assert cli("retro", run)["baseline"] == 10


def test_source_snapshot_catches_uncommitted_new_scoped_files(campaign: tuple[Path, Path, Path]) -> None:
    """Git HEAD identity alone cannot establish the current optimization source."""
    repo, run, _ = campaign
    cli("begin", run, "--hypothesis", "Change source without commit")
    (repo / "value.txt").write_text("8\n", encoding="utf-8")
    cli("finish", run, "--metric", "8", "--guard", "pass", "--decision", "kept")
    (repo / "value.txt").write_text("7\n", encoding="utf-8")
    assert "source-mismatch" in cli("resume", run, success=False)["error"]


def test_promotion_requires_bound_managed_history_and_rejects_pending(campaign: tuple[Path, Path, Path]) -> None:
    """Ordinary shared admission cannot silently route managed runs through historical validation."""
    _, run, _ = campaign
    module_path = HELPER.parents[2] / "shared" / "validate-artifacts.py"
    spec = importlib.util.spec_from_file_location("optimize_shared_admission", module_path)
    assert spec is not None
    assert spec.loader is not None
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    candidate = run / "result.candidate.json"
    candidate.write_text('{"metadata": {}}', encoding="utf-8")
    with pytest.raises(SystemExit, match="optimize-lifecycle-metadata-required"):
        validator._validate_optimize_lifecycle(run, candidate)
    binding = {
        name: hashlib.sha256((run / name).read_bytes()).hexdigest() for name in ("campaign.json", "experiments.jsonl")
    }
    metadata = {"optimize_lifecycle": {"schema_version": 1, "mode": "campaign", "bindings": binding}}
    candidate.write_text(json.dumps({"metadata": metadata}), encoding="utf-8")
    cli("admit", run, "--input", str(candidate))
    cli("begin", run, "--hypothesis", "Pending must block promotion")
    with pytest.raises(SystemExit, match="pending-iteration"):
        validator._validate_optimize_lifecycle(run, candidate)
    cli("finish", run, "--metric", "10", "--guard", "pass", "--decision", "reverted")
    with pytest.raises(SystemExit, match="evidence-hash-mismatch"):
        validator._validate_optimize_lifecycle(run, candidate)
    metadata["optimize_lifecycle"]["bindings"]["experiments.jsonl"] = hashlib.sha256(
        (run / "experiments.jsonl").read_bytes()
    ).hexdigest()
    candidate.write_text(json.dumps({"metadata": metadata}), encoding="utf-8")
    cli("admit", run, "--input", str(candidate))


def test_historical_pass_fail_and_completed_budget_remain_admissible(
    campaign: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    """New lifecycle admission preserves old reports and validates completed scheduling budgets."""
    historical = tmp_path / "historical"
    historical.mkdir()
    candidate = historical / "result.candidate.json"
    candidate.write_text('{"metadata": {}, "metric": "pass"}', encoding="utf-8")
    assert cli("admit", historical, "--input", str(candidate)) == {"mode": "historical"}
    _, run, _ = campaign
    for _ in range(3):
        cli("begin", run, "--hypothesis", "No improvement")
        cli("finish", run, "--metric", "10", "--guard", "pass", "--decision", "reverted")
    assert cli("validate", run)["next_iteration"] == 4
    assert "iteration-budget-exhausted" in cli("begin", run, "--hypothesis", "Extra iteration", success=False)["error"]


def test_out_of_scope_edit_cannot_become_an_accepted_checkpoint(campaign: tuple[Path, Path, Path]) -> None:
    """A better number cannot authorize absorbing an unrelated tracked change."""
    repo, run, _ = campaign
    cli("begin", run, "--hypothesis", "Measured improvement")
    (repo / "value.txt").write_text("8\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "value.txt"], check=True)
    assert (
        "out-of-scope-change"
        in cli("finish", run, "--metric", "8", "--guard", "pass", "--decision", "kept", success=False)["error"]
    )


@pytest.mark.parametrize("hypothesis", [None, "", False])
def test_corrupt_pending_is_rejected_before_durable_append(
    campaign: tuple[Path, Path, Path], hypothesis: object
) -> None:
    """A failed finish must preserve history and pending evidence for recovery."""
    _, run, _ = campaign
    cli("begin", run, "--hypothesis", "Original attempt")
    path = run / "pending.json"
    pending = json.loads(path.read_text())
    pending["hypothesis"] = hypothesis
    path.write_text(json.dumps(pending), encoding="utf-8")
    before = (run / "experiments.jsonl").read_bytes()
    cli("finish", run, "--metric", "10", "--guard", "pass", "--decision", "reverted", success=False)
    assert (run / "experiments.jsonl").read_bytes() == before
    assert path.is_file()


def test_checkpoint_supplies_missing_final_newline(campaign: tuple[Path, Path, Path]) -> None:
    """Appending a second event must preserve a valid unterminated first JSON line."""
    _, run, _ = campaign
    cli("begin", run, "--hypothesis", "First attempt")
    cli("finish", run, "--metric", "10", "--guard", "pass", "--decision", "reverted")
    log = run / "experiments.jsonl"
    before = log.read_bytes().rstrip(b"\n")
    log.write_bytes(before)
    cli("begin", run, "--hypothesis", "Second attempt")
    cli("finish", run, "--metric", "10", "--guard", "pass", "--decision", "reverted")
    assert log.read_bytes().startswith(before + b"\n")
    assert len(log.read_text().splitlines()) == 2
    assert cli("resume", run)["next_iteration"] == 3


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
@pytest.mark.parametrize("action", ["init", "finish", "resume"])
def test_hidden_index_modes_cannot_hide_tracked_changes(
    campaign: tuple[Path, Path, Path], flag: str, action: str
) -> None:
    """Reject diff-hidden index entries without rewriting the index or campaign."""
    repo, run, config = campaign
    if action == "finish":
        cli("begin", run, "--hypothesis", "Measured improvement")
    subprocess.run(["git", "-C", str(repo), "update-index", flag, "outside.txt"], check=True)
    index_path = repo / ".git" / "index"
    before = index_path.read_bytes()
    (repo / "outside.txt").write_text("hidden change\n", encoding="utf-8")
    if action != "resume":
        (repo / "value.txt").write_text("8\n", encoding="utf-8")
    arguments = {
        "init": ("--root", str(repo), "--config", str(config), "--baseline", "8"),
        "finish": ("--metric", "8", "--guard", "pass", "--decision", "kept"),
        "resume": (),
    }
    target = run.with_name("hidden-run") if action == "init" else run
    assert "unsupported-hidden-index-entry" in cli(action, target, *arguments[action], success=False)["error"]
    assert index_path.read_bytes() == before
    assert (run / "experiments.jsonl").read_bytes() == b""


@pytest.mark.skipif(not DIRECTORY_SYMLINKS_AVAILABLE, reason="directory symlinks unavailable")
def test_run_alias_admission_preserves_descendant_link_rejection(
    campaign: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    """Accept a producer-supported run alias while rejecting linked evidence below it."""
    _, run, _ = campaign
    alias = tmp_path / "run-alias"
    alias.symlink_to(run, target_is_directory=True)
    candidate = run / "result.candidate.json"
    bindings = {
        name: hashlib.sha256((run / name).read_bytes()).hexdigest() for name in ("campaign.json", "experiments.jsonl")
    }
    candidate.write_text(
        json.dumps(
            {"metadata": {"optimize_lifecycle": {"schema_version": 1, "mode": "campaign", "bindings": bindings}}}
        ),
        encoding="utf-8",
    )
    assert cli("admit", alias, "--input", str(candidate))["best_metric"] == 10
    original = run / "campaign.json"
    original.rename(tmp_path / "linked-campaign.json")
    directory = run / "linked"
    directory.symlink_to(tmp_path, target_is_directory=True)
    original.symlink_to(directory / "linked-campaign.json")
    assert "evidence-path-escape" in cli("admit", alias, "--input", str(candidate), success=False)["error"]


def managed_handoff(run: Path, mode: str, rows: list[list[str]]) -> subprocess.CompletedProcess[str]:
    """Render, write and validate an ordinary result from real managed evidence."""
    fixture_path = HELPER.parents[2] / "tests" / "review" / "test_final_handoff.py"
    spec = importlib.util.spec_from_file_location("optimize_handoff_fixtures", fixture_path)
    assert spec is not None
    assert spec.loader is not None
    fixtures = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixtures)
    candidate = fixtures._write_schema_v2_assess(run)
    payload = json.loads((run / "final-handoff.json").read_text())
    payload.update(skill="optimize", outcome={"title": "Outcome", "summary": "Managed measurements retained."})
    payload["tables"] = [
        {
            "heading": "Results",
            "columns": ["Iteration", "Baseline", "After", "Delta", "Guard", "Decision"],
            "rows": [
                {"id": f"I-{index}", "cells": cells, "source_ids": [f"measurement:{index}"]}
                for index, cells in enumerate(rows)
            ],
        }
    ]
    payload["source_records"] = [
        {"id": f"measurement:{index}", "evidence": "experiments.jsonl"} for index in range(len(rows))
    ]
    payload["source_coverage"] = {
        "source_records_total": len(rows),
        "represented_source_records_total": len(rows),
        "omitted_source_records_total": 0,
    }
    (run / "final-handoff.json").write_text(json.dumps(payload), encoding="utf-8")
    bound = fixtures._load_finalizer().render_files(
        run / "final-handoff.json", run / "final.md", run / "final-handoff.validation.json"
    )
    metadata = json.loads(candidate.read_text())["metadata"]
    metadata["final_handoff"].update({key: bound[key] for key in ("handoff_sha256", "rendered_sha256")})
    names = ("ablation-input.json",) if mode == "ablation" else ("campaign.json", "experiments.jsonl")
    metadata["optimize_lifecycle"] = {
        "schema_version": 1,
        "mode": mode,
        "bindings": {name: hashlib.sha256((run / name).read_bytes()).hexdigest() for name in names},
    }
    (run / "hypothesis.md").write_text("Measured reduction\n", encoding="utf-8")
    (run / "comparison.md").write_text("baseline 10 after 8 delta -2 guard pass confidence 0.95\n", encoding="utf-8")
    shared = HELPER.parents[2] / "shared"
    writer = subprocess.run(
        [
            sys.executable,
            str(shared / "write-result.py"),
            "--out",
            str(candidate),
            "--gates",
            str(run / "gates.json"),
            "--status",
            "pass",
            "--checks-run",
            "lint,format,types,tests,review",
            "--confidence",
            "0.95",
            "--artifact-path",
            str(run / "result.json"),
            "--metadata",
            json.dumps(metadata),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert writer.returncode == 0, writer.stderr
    return subprocess.run(
        [
            sys.executable,
            str(shared / "validate-artifacts.py"),
            "--skill",
            "optimize",
            "--out",
            str(run),
            "--result",
            str(candidate),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("mode", ["campaign", "resume", "retrospective"])
@pytest.mark.parametrize("summary", [False, True])
def test_managed_final_rows_reconcile_through_ordinary_pipeline(
    campaign: tuple[Path, Path, Path], mode: str, summary: bool
) -> None:
    """Bind published values, guards and decisions to complete retained attempts."""
    repo, run, _ = campaign
    cli("begin", run, "--hypothesis", "Measured improvement")
    (repo / "value.txt").write_text("8\n", encoding="utf-8")
    cli("finish", run, "--metric", "8", "--guard", "pass", "--decision", "kept")
    cli("begin", run, "--hypothesis", "Rejected follow-up")
    cli("finish", run, "--metric", "9", "--guard", "fail", "--decision", "reverted")
    rows = (
        [["campaign", "10", "8", "-2", "fail", "kept"]]
        if summary
        else [["1", "10", "8", "-2", "pass", "kept"], ["2", "8", "9", "1", "fail", "reverted"]]
    )
    accepted = managed_handoff(run, mode, rows)
    assert accepted.returncode == 0, accepted.stderr
    candidate = run / "result.candidate.json"
    candidate.replace(run / "result.json")
    promoted = subprocess.run(
        [
            sys.executable,
            str(HELPER.parents[2] / "shared" / "validate-artifacts.py"),
            "--skill",
            "optimize",
            "--out",
            str(run),
            "--result",
            str(run / "result.json"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert promoted.returncode == 0, promoted.stderr
    for column, wrong in ((2, "1"), (3, "-9"), (4, "pass" if summary else "fail"), (5, "reverted")):
        corrupt = [list(row) for row in rows]
        corrupt[0][column] = wrong
        rejected = managed_handoff(run, mode, corrupt)
        assert rejected.returncode != 0, rejected.stdout
        assert "optimize-final-results-mismatch" in rejected.stderr
    if not summary:
        rejected = managed_handoff(run, mode, rows[:1])
        assert rejected.returncode != 0
        assert "optimize-final-results-mismatch" in rejected.stderr


def test_empty_campaign_requires_explicit_summary(campaign: tuple[Path, Path, Path]) -> None:
    """Represent no recorded attempts truthfully rather than accepting an empty result."""
    _, run, _ = campaign
    accepted = managed_handoff(run, "campaign", [["campaign", "10", "10", "0", "pass", "inconclusive"]])
    assert accepted.returncode == 0, accepted.stderr


@pytest.fixture
def normalized_campaign(text_newline_default: None, campaign: tuple[Path, Path, Path]) -> tuple[Path, Path, Path]:
    """Freeze real text-normalizing attributes before recording source identity."""
    repo, run, config = campaign
    (repo / ".gitattributes").write_bytes(b"outside.txt text eol=lf\nvalue.txt text eol=lf\n")
    subprocess.run(["git", "-C", str(repo), "add", ".gitattributes"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "--renormalize", "."], check=True)
    env = os.environ | {
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-qm", "Freeze text normalization"], env=env, check=True, capture_output=True
    )
    settings = json.loads(config.read_text())
    settings["scope_files"] = ["./" + name for name in settings["scope_files"]]
    config.write_text(json.dumps(settings), encoding="utf-8")
    run = run.with_name("normalized-run")
    cli("init", run, "--root", str(repo), "--config", str(config), "--baseline", "10")
    return repo, run, config


def test_normalized_outside_bytes_cannot_be_kept(normalized_campaign: tuple[Path, Path, Path]) -> None:
    """Clean Git content cannot hide changed measurement-visible outside bytes."""
    repo, run, _ = normalized_campaign
    cli("begin", run, "--hypothesis", "Measured reduction")
    index = (repo / ".git" / "index").read_bytes()
    assert (repo / "outside.txt").read_bytes() == b"unrelated\n"
    (repo / "outside.txt").write_bytes(b"unrelated\r\n")
    assert not subprocess.check_output(["git", "-C", str(repo), "diff", "HEAD", "--", "outside.txt"])
    (repo / "value.txt").write_bytes(b"8\n")
    error = cli("finish", run, "--metric", "8", "--guard", "pass", "--decision", "kept", success=False)
    assert "out-of-scope-change" in error["error"]
    assert (run / "experiments.jsonl").read_bytes() == b""
    assert (run / "pending.json").is_file()
    assert (repo / ".git" / "index").read_bytes() == index


@pytest.mark.parametrize("action", ["resume", "validate", "admit", "ordinary"])
def test_normalized_post_checkpoint_drift_reaches_every_live_consumer(
    normalized_campaign: tuple[Path, Path, Path], action: str
) -> None:
    """A valid scoped normalized edit remains accepted until unrelated raw bytes drift."""
    repo, run, _ = normalized_campaign
    assert (repo / "value.txt").read_bytes() == b"10\n"
    assert (repo / "outside.txt").read_bytes() == b"unrelated\n"
    cli("begin", run, "--hypothesis", "Scoped normalization")
    (repo / "value.txt").write_bytes(b"10\r\n")
    assert not subprocess.check_output(["git", "-C", str(repo), "diff", "HEAD", "--", "value.txt"])
    cli("finish", run, "--metric", "8", "--guard", "pass", "--decision", "kept")
    rows = [["1", "10", "8", "-2", "pass", "kept"]]
    accepted = managed_handoff(run, "campaign", rows)
    assert accepted.returncode == 0, accepted.stderr
    (repo / "outside.txt").write_bytes(b"unrelated\r\n")
    assert not subprocess.check_output(["git", "-C", str(repo), "diff", "HEAD", "--", "outside.txt"])
    if action == "ordinary":
        rejected = managed_handoff(run, "campaign", rows)
        assert rejected.returncode != 0
        assert "source-mismatch" in rejected.stderr
    else:
        arguments = ("--input", str(run / "result.candidate.json")) if action == "admit" else ()
        assert "source-mismatch" in cli(action, run, *arguments, success=False)["error"]


@pytest.mark.skipif(not POSIX_FILE_MODES_AVAILABLE, reason="executable mode changes unavailable")
def test_core_filemode_false_does_not_hide_outside_executable_change(campaign: tuple[Path, Path, Path]) -> None:
    """Actual executable identity remains frozen when Git ignores mode changes."""
    repo, run, _ = campaign
    subprocess.run(["git", "-C", str(repo), "config", "core.filemode", "false"], check=True)
    cli("begin", run, "--hypothesis", "Measured reduction")
    original = (repo / "outside.txt").stat().st_mode
    (repo / "outside.txt").chmod(original ^ 0o111)
    assert not subprocess.check_output(["git", "-C", str(repo), "diff", "HEAD", "--", "outside.txt"])
    (repo / "value.txt").write_bytes(b"8\n")
    assert (
        "out-of-scope-change"
        in cli("finish", run, "--metric", "8", "--guard", "pass", "--decision", "kept", success=False)["error"]
    )


@pytest.mark.skipif(not DIRECTORY_SYMLINKS_AVAILABLE, reason="directory symlinks unavailable")
def test_tracked_directory_link_is_rejected_before_descendant_read(
    campaign: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    """A replaced tracked ancestor cannot make source reads escape the repository."""
    repo, run, config = campaign
    nested = repo / "nested"
    nested.mkdir()
    (nested / "tracked.txt").write_bytes(b"inside\n")
    subprocess.run(["git", "-C", str(repo), "add", "nested/tracked.txt"], check=True)
    external = tmp_path / "external"
    external.mkdir()
    (external / "tracked.txt").write_bytes(b"outside\n")
    (nested / "tracked.txt").unlink()
    nested.rmdir()
    nested.symlink_to(external, target_is_directory=True)
    before = (repo / ".git" / "index").read_bytes()
    error = cli("snapshot", run, "--root", str(repo), "--config", str(config), success=False)
    assert "tracked-source-symlink-ancestor" in error["error"]
    assert (repo / ".git" / "index").read_bytes() == before
    assert (external / "tracked.txt").read_bytes() == b"outside\n"


@pytest.mark.skipif(not FILE_SYMLINKS_AVAILABLE, reason="file symlinks unavailable")
def test_tracked_leaf_link_binds_target_without_following_it(campaign: tuple[Path, Path, Path], tmp_path: Path) -> None:
    """External target contents stay outside identity while the link text remains bound."""
    repo, run, config = campaign
    target = tmp_path / "target.txt"
    target.write_bytes(b"original\n")
    link = repo / "link.txt"
    link.symlink_to(target)
    subprocess.run(["git", "-C", str(repo), "add", "link.txt"], check=True)
    frozen = cli("snapshot", run, "--root", str(repo), "--config", str(config))
    target.write_bytes(b"changed external bytes\n")
    assert cli("snapshot", run, "--root", str(repo), "--config", str(config)) == frozen
    link.unlink()
    link.symlink_to(tmp_path / "different-target.txt")
    changed = cli("snapshot", run, "--root", str(repo), "--config", str(config))
    assert changed["tracked_bytes"] != frozen["tracked_bytes"]
    assert changed["outside_scope_bytes"] != frozen["outside_scope_bytes"]


def test_submodule_snapshot_is_explicitly_unsupported(campaign: tuple[Path, Path, Path]) -> None:
    """A Gitlink is not misrepresented as exact working-tree file content."""
    repo, run, config = campaign
    head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    subprocess.run(
        ["git", "-C", str(repo), "update-index", "--add", "--cacheinfo", f"160000,{head},component"], check=True
    )
    index = (repo / ".git" / "index").read_bytes()
    error = cli("snapshot", run, "--root", str(repo), "--config", str(config), success=False)
    assert "unsupported-source-submodule" in error["error"]
    assert (repo / ".git" / "index").read_bytes() == index
