"""Exercise optimization checkpoints and retrospective reports through the public CLI."""

from __future__ import annotations

import functools
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import pytest
from _fixture_files import file_sha256
from _modules import load_module
from _platform import DIRECTORY_SYMLINKS_AVAILABLE, FILE_SYMLINKS_AVAILABLE, POSIX_FILE_MODES_AVAILABLE

PLUGIN = Path(__file__).resolve().parents[1]
HELPER = PLUGIN / "skills" / "optimize" / "campaign.py"
SHARED = PLUGIN / "shared"
GIT_ENV = os.environ | {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}
MEASUREMENT = "from pathlib import Path; print(float(Path('value.txt').read_text()))"
GUARD = "import subprocess; raise SystemExit(float(subprocess.check_output(['git', 'show', ':value.txt'])) >= 10)"
ABLATION_ROWS = [
    ["remove shortcut", "8", "13", "5", "pass", "ranked"],
    ["unsafe removal", "8", "99", "91", "fail", "excluded"],
]
ABLATION_CORRUPTIONS = [(0, "other component"), (1, "10"), (2, "1"), (3, "-5"), (4, "fail"), (5, "excluded")]
MISMATCH = "optimize-final-results-mismatch"


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


def git(repo: Path, *arguments: str) -> None:
    """Run one Git command in the repository, failing the test when it fails."""
    subprocess.run(["git", "-C", str(repo), *arguments], env=GIT_ENV, check=True, capture_output=True)


def git_bytes(repo: Path, *arguments: str) -> bytes:
    """Return the standard output of one Git command run in the repository."""
    return subprocess.check_output(["git", "-C", str(repo), *arguments])


def exhaust_budget(run: Path) -> None:
    """Spend the campaign's three-iteration budget on attempts that gain nothing."""
    for _ in range(3):
        cli("begin", run, "--hypothesis", "No gain")
        cli("finish", run, "--metric", "10", "--guard", "pass", "--decision", "reverted")


@functools.cache
def _load(path: Path, name: str) -> ModuleType:
    """Load a repository script as a module once; the scripts hold no state between tests."""
    return load_module(name, path)


@pytest.fixture
def campaign(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create a real committed source tree and an external campaign directory."""
    repo = tmp_path / "source"
    repo.mkdir()
    (repo / "value.txt").write_bytes(b"10\n")
    (repo / "obsolete.txt").write_bytes(b"old\n")
    (repo / "outside.txt").write_bytes(b"unrelated\n")
    git(repo, "init", "-q")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "baseline")
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
    """Keep a measured change, reject another, and resume at the next unfinished iteration.

    A real two-attempt campaign (one kept, one reverted) is resumed and summarized; the ledger only ever grows and the
    retrospective is a read-only view of it.
    """
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


def test_resume_rejects_source_drift_until_restored(
    text_newline_default: None, campaign: tuple[Path, Path, Path]
) -> None:
    """A matching run path cannot conceal changed source; restoring the recorded bytes resumes the campaign.

    The measured file is edited after the run was recorded: resume names a source mismatch, and succeeds again once the
    original bytes are back.
    """
    repo, run, _ = campaign
    original = (repo / "value.txt").read_bytes()
    (repo / "value.txt").write_text("11\n", encoding="utf-8")

    assert "source-mismatch" in cli("resume", run, success=False)["error"]

    (repo / "value.txt").write_bytes(original)
    assert cli("resume", run)["next_iteration"] == 1


def test_resume_rejects_configuration_drift(campaign: tuple[Path, Path, Path]) -> None:
    """A matching run path cannot conceal a changed measurement command.

    The guard command in the supplied configuration differs from the one frozen at init, so resume must refuse it.
    """
    _, run, config = campaign
    data = json.loads(config.read_text())
    data["guard_cmd"] = "different guard"
    config.write_text(json.dumps(data), encoding="utf-8")

    assert "config-mismatch" in cli("resume", run, "--config", str(config), success=False)["error"]


def test_pending_iteration_is_never_replayed(campaign: tuple[Path, Path, Path]) -> None:
    """An interrupted edit leaves an explicit recovery condition, including after source is restored.

    An attempt was begun and never finished; neither resume nor a new begin may proceed until it is finished, after
    which the next iteration is 2.
    """
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

    error = cli("finish", run, "--metric=" + metric, "--guard", "pass", "--decision", "kept", success=False)["error"]

    assert "finite" in error
    assert before == (run / "experiments.jsonl").read_bytes()
    assert (run / "pending.json").is_file()


def test_kept_regression_and_false_rollback_are_rejected(
    text_newline_default: None, campaign: tuple[Path, Path, Path]
) -> None:
    """Neither an improved number with failing guards nor un-restored source is accepted.

    A better metric with a failing guard cannot be kept, a revert while the edited source is still in place is refused,
    and the revert succeeds only after the original bytes are restored.
    """
    repo, run, _ = campaign
    original = (repo / "value.txt").read_bytes()
    cli("begin", run, "--hypothesis", "Unsafe improvement")
    (repo / "value.txt").write_text("8\n", encoding="utf-8")
    kept = cli("finish", run, "--metric", "8", "--guard", "fail", "--decision", "kept", success=False)
    reverted = cli("finish", run, "--metric", "8", "--guard", "fail", "--decision", "reverted", success=False)
    assert "guard-regression" in kept["error"]
    assert "rollback-source-mismatch" in reverted["error"]
    (repo / "value.txt").write_bytes(original)
    assert cli("finish", run, "--metric", "8", "--guard", "fail", "--decision", "reverted")["best_metric"] == 10


def test_corrupt_history_and_exhausted_budget_stop_resume(campaign: tuple[Path, Path, Path]) -> None:
    """Resume validates every record and honors the original total budget across restarts.

    After three attempts resume reports the budget exhausted; a hand-appended record without a decision is refused by
    the retrospective.
    """
    _, run, _ = campaign
    exhaust_budget(run)
    assert "iteration-budget-exhausted" in cli("resume", run, success=False)["error"]
    with (run / "experiments.jsonl").open("a", encoding="utf-8") as stream:
        stream.write('{"iteration": 99}\n')
    assert "invalid-iteration-record" in cli("retro", run, success=False)["error"]


def reconstruct_fixture_source(repo: Path, variant: Path, config: Path) -> None:
    """Rebuild regular-file fixture source from retained Git patches and exact raw identity."""
    names = set(json.loads(config.read_text())["scope_files"])
    for arguments in (("ls-tree", "-r", "--name-only", "-z", "HEAD"), ("ls-files", "-z")):
        names.update(os.fsdecode(name) for name in git_bytes(repo, *arguments).split(b"\0") if name)
    retained: dict[str, tuple[bytes, int] | None] = {}
    for name in sorted(names):
        path = repo / name
        assert not any(part.is_symlink() for part in (path, *path.parents) if part.is_relative_to(repo))
        assert not path.exists() or path.is_file()
        retained[name] = (path.read_bytes(), path.stat().st_mode & 0o777) if path.exists() else None
    index_patch = git_bytes(repo, "diff", "--cached", "--binary", "--no-ext-diff", "HEAD", "--")
    tracked_patch = git_bytes(repo, "diff", "--binary", "--no-ext-diff", "HEAD", "--")
    git(repo, "worktree", "add", "--detach", str(variant), "HEAD")
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


def stage_dirty_best_source(repo: Path, autocrlf: str) -> None:
    """Stage a changed value file with the given line-ending conversion, executable when the host has file modes."""
    git(repo, "config", "core.autocrlf", autocrlf)
    (repo / "value.txt").write_text("9\n", encoding="utf-8")
    git(repo, "add", "value.txt")
    if POSIX_FILE_MODES_AVAILABLE:
        git(repo, "config", "core.filemode", "false")
        (repo / "value.txt").chmod((repo / "value.txt").stat().st_mode | 0o111)


def measure(directory: Path) -> float:
    """Run the example metric command in a directory and return its number."""
    return float(subprocess.check_output([sys.executable, "-c", MEASUREMENT], cwd=directory, text=True))


@dataclass
class AblationSource:
    """A kept campaign over a dirty staged tree and an isolated worktree rebuilt from its retained evidence."""

    repo: Path
    run: Path
    config: Path
    variant: Path
    frozen: dict
    original_index: bytes


@dataclass
class AblationInput:
    """Measured removal variants written as the ablation command's input file."""

    source: AblationSource
    data: dict
    path: Path


def keep_dirty_best_source(campaign: tuple[Path, Path, Path], tmp_path: Path, autocrlf: str) -> AblationSource:
    """Keep a measured change over a dirty staged tree, then rebuild its source as an isolated worktree variant."""
    repo, run, config = campaign
    stage_dirty_best_source(repo, autocrlf)
    original_index = git_bytes(repo, "diff", "--cached", "--binary", "HEAD")
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
    return AblationSource(repo, run, config, variant, frozen, original_index)


@pytest.fixture(params=[pytest.param("false", id="autocrlf-false"), pytest.param("true", id="autocrlf-true")])
def ablation_source(
    request: pytest.FixtureRequest, text_newline_default: None, campaign: tuple[Path, Path, Path], tmp_path: Path
) -> AblationSource:
    """Provide the kept dirty-tree campaign under each line-ending conversion and newline default."""
    return keep_dirty_best_source(campaign, tmp_path, request.param)


@pytest.fixture
def plain_ablation_source(campaign: tuple[Path, Path, Path], tmp_path: Path) -> AblationSource:
    """Provide the kept dirty-tree campaign once, for checks that do not depend on line-ending conversion."""
    return keep_dirty_best_source(campaign, tmp_path, "false")


def ablation_record(run: Path, index: int, config_hash: str, name: str, metric: float, source: str, guard: int) -> dict:
    """Build one measured ablation record and write its retained metric and guard output files."""
    record = {
        "name": name,
        "metric": metric,
        "source_before": source,
        "source_after": source,
        "frozen_config": config_hash,
        "metric_exit_code": 0,
        "guard_exit_code": guard,
    }
    for kind, content in (("metric_output", str(metric)), ("guard_output", "guard result")):
        output = run / f"{index}-{kind}.txt"
        output.write_text(content, encoding="utf-8")
        record[kind] = {"path": output.name, "sha256": file_sha256(output)}
    return record


def measure_ablation(source: AblationSource) -> AblationInput:
    """Measure the reference and one passing and one failing removal in the variant, and write the input file."""
    reference = measure(source.repo)
    (source.variant / "value.txt").write_text("13\n", encoding="utf-8")
    removed = measure(source.variant)
    changed = cli("snapshot", source.run, "--root", str(source.variant), "--config", str(source.config))
    config_hash = hashlib.sha256(
        json.dumps(json.loads(source.config.read_text()), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    specs = (
        ("reference", reference, source.frozen["digest"], 0),
        ("remove shortcut", removed, changed["digest"], 0),
        ("unsafe removal", 99.0, changed["digest"], 1),
    )
    records = [ablation_record(source.run, index, config_hash, *spec) for index, spec in enumerate(specs)]
    data = {
        "schema_version": 1,
        "measurement_basis": "worktree",
        "metric_direction": "lower",
        "frozen_source": source.frozen["digest"],
        "frozen_config": config_hash,
        "reference": records[0],
        "variants": records[1:],
    }
    path = source.run / "ablation-input.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return AblationInput(source, data, path)


@pytest.fixture
def ablation_input(plain_ablation_source: AblationSource) -> AblationInput:
    """Provide measured ablation variants over the kept dirty-tree campaign."""
    return measure_ablation(plain_ablation_source)


def test_reconstructed_variant_reproduces_frozen_source_and_measurement(ablation_source: AblationSource) -> None:
    """Rebuild an isolated variant that has the same source identity and metric as the kept campaign state.

    The kept change added a file, removed one and left the index staged; the worktree variant must match the frozen
    snapshot, leave the real index alone and measure the same value, for both line-ending conversion settings.
    """
    source = ablation_source

    rebuilt = cli("snapshot", source.run, "--root", str(source.variant), "--config", str(source.config))

    assert not (source.variant / "obsolete.txt").exists()
    assert rebuilt == source.frozen
    assert git_bytes(source.repo, "diff", "--cached", "--binary", "HEAD") == source.original_index
    assert measure(source.repo) == measure(source.variant) == 8


def test_ablation_ranks_real_worktree_measurements_and_excludes_failed_guards(ablation_source: AblationSource) -> None:
    """Measure an actual isolated removal variant and rank only passing guards.

    The passing removal loses 5 against the reference; the removal whose guard failed is listed as excluded instead of
    ranked. The example guard command passes in both trees, so only the recorded guard exit decides.
    """
    ablation_input = measure_ablation(ablation_source)
    source = ablation_source
    assert float((source.variant / "value.txt").read_text()) == 13
    assert subprocess.run([sys.executable, "-c", GUARD], cwd=source.variant, check=False).returncode == 0
    assert subprocess.run([sys.executable, "-c", GUARD], cwd=source.repo, check=False).returncode == 0

    ranked = cli("ablation", source.run, "--input", str(ablation_input.path))

    assert ranked["ranked"] == [{"name": "remove shortcut", "metric": 13.0, "loss": 5.0}]
    assert ranked["excluded"] == ["unsafe removal"]


def test_ablation_rows_are_checked_against_retained_measurements(ablation_input: AblationInput) -> None:
    """Published ablation rows pass the ordinary pipeline only when they match the retained measurements.

    The matching rows are accepted. Every other copy alters exactly one column of the first row (component, baseline,
    after, loss, guard or decision) or omits the excluded variant, and is refused as a results mismatch; none of the
    attempts touches the source tree.
    """
    source = ablation_input.source
    corrupted = [_replace_cell(ABLATION_ROWS, column, wrong) for column, wrong in ABLATION_CORRUPTIONS]

    accepted, *rejected = [
        managed_handoff(source.run, "ablation", rows) for rows in [ABLATION_ROWS, *corrupted, ABLATION_ROWS[:1]]
    ]

    assert accepted.returncode == 0, accepted.stderr
    assert all(result.returncode != 0 for result in rejected)
    assert all(MISMATCH in result.stderr for result in rejected)
    assert (source.repo / "value.txt").read_text() == "8\n"


def test_ablation_refuses_a_failing_reference_guard(ablation_input: AblationInput) -> None:
    """A reference measurement whose guard failed cannot anchor a ranking."""
    ablation_input.data["reference"]["guard_exit_code"] = 1
    ablation_input.path.write_text(json.dumps(ablation_input.data), encoding="utf-8")

    error = cli("ablation", ablation_input.source.run, "--input", str(ablation_input.path), success=False)["error"]

    assert "reference-guard-failed" in error


def test_ablation_requires_independent_worktree_measurements(ablation_input: AblationInput) -> None:
    """Measurements taken from the shared Git index instead of isolated worktrees are not independent evidence."""
    ablation_input.data["measurement_basis"] = "git-index"
    ablation_input.path.write_text(json.dumps(ablation_input.data), encoding="utf-8")

    error = cli("ablation", ablation_input.source.run, "--input", str(ablation_input.path), success=False)["error"]

    assert "worktree-independent-measurements-required" in error


def _no_change(repo: Path) -> None:
    """Leave the repository as the campaign fixture made it."""


def _stage_extra_file(repo: Path) -> None:
    """Stage a new scoped file so the index holds an addition."""
    (repo / "extra.txt").write_bytes(b"staged\n")
    git(repo, "add", "extra.txt")


def _stage_value_deletion(repo: Path) -> None:
    """Remove the measured file from the index while leaving it in the working tree."""
    git(repo, "rm", "--cached", "value.txt")


def _rewrite_extra_file(repo: Path) -> None:
    """Change the staged scoped file in the working tree only."""
    (repo / "extra.txt").write_bytes(b"unstaged\n")


@pytest.mark.parametrize(
    ("stage", "after_begin"),
    [
        pytest.param(_no_change, _no_change, id="unchanged"),
        pytest.param(_stage_extra_file, _rewrite_extra_file, id="addition"),
        pytest.param(_stage_value_deletion, _no_change, id="deletion"),
    ],
)
def test_ablation_reconstructs_staged_membership(
    campaign: tuple[Path, Path, Path],
    tmp_path: Path,
    stage: Callable[[Path], None],
    after_begin: Callable[[Path], None],
) -> None:
    """Preserve diff semantics when raw worktree bytes differ from staged entry membership.

    The index holds an added file, a deleted file or nothing special while the working tree differs from it; the rebuilt
    variant must hash to the same source identity as the kept campaign state.
    """
    repo, run, config = campaign
    stage(repo)
    run = run.with_name("membership-run")
    cli("init", run, "--root", str(repo), "--config", str(config), "--baseline", "10")
    cli("begin", run, "--hypothesis", "Change worktree independently of index")
    (repo / "value.txt").write_bytes(b"8\n")
    after_begin(repo)
    cli("finish", run, "--metric", "8", "--guard", "pass", "--decision", "kept")
    frozen = cli("snapshot", run, "--root", str(repo), "--config", str(config))
    variant = tmp_path / "membership-variant"

    reconstruct_fixture_source(repo, variant, config)

    assert cli("snapshot", run, "--root", str(variant), "--config", str(config)) == frozen
    assert cli("validate", run)["best_source"] == frozen


def test_checkpoint_append_crash_is_reconciled_without_duplicate(campaign: tuple[Path, Path, Path]) -> None:
    """A crash after the durable append cannot cause the same hypothesis to run twice.

    The pending marker is restored after a finish, as if the process died between the ledger append and the marker
    removal; resume must drop the stale marker and leave the ledger as it was.
    """
    repo, run, _ = campaign
    cli("begin", run, "--hypothesis", "Durable result")
    pending = (run / "pending.json").read_bytes()
    (repo / "value.txt").write_text("8\n", encoding="utf-8")
    cli("finish", run, "--metric", "8", "--guard", "pass", "--decision", "kept")
    history = (run / "experiments.jsonl").read_bytes()
    (run / "pending.json").write_bytes(pending)

    resumed = cli("resume", run)

    assert resumed["next_iteration"] == 2
    assert history == (run / "experiments.jsonl").read_bytes()
    assert not (run / "pending.json").exists()


def test_retro_after_source_checkout_disappears(campaign: tuple[Path, Path, Path]) -> None:
    """Historical descriptive analysis does not depend on mutable live source."""
    repo, run, _ = campaign
    repo.rename(repo.with_name("moved-source"))

    assert cli("retro", run)["baseline"] == 10


def test_source_snapshot_catches_uncommitted_new_scoped_files(campaign: tuple[Path, Path, Path]) -> None:
    """Git HEAD identity alone cannot establish the current optimization source.

    The kept source is edited again without a commit; resume must notice because the source identity covers working
    bytes, not just the commit.
    """
    repo, run, _ = campaign
    cli("begin", run, "--hypothesis", "Change source without commit")
    (repo / "value.txt").write_text("8\n", encoding="utf-8")
    cli("finish", run, "--metric", "8", "--guard", "pass", "--decision", "kept")
    (repo / "value.txt").write_text("7\n", encoding="utf-8")

    assert "source-mismatch" in cli("resume", run, success=False)["error"]


def test_promotion_requires_bound_managed_history_and_rejects_pending(campaign: tuple[Path, Path, Path]) -> None:
    """Ordinary shared admission cannot silently route managed runs through historical validation.

    A result without lifecycle metadata is refused; once bound to the campaign and ledger hashes it is admitted, a
    pending attempt blocks promotion again, and a ledger that changed after binding is refused until rebound.
    """
    _, run, _ = campaign
    validator = _load(SHARED / "validate-artifacts.py", "optimize_shared_admission")
    candidate = run / "result.candidate.json"
    candidate.write_text('{"metadata": {}}', encoding="utf-8")
    with pytest.raises(SystemExit, match="optimize-lifecycle-metadata-required"):
        validator._validate_optimize_lifecycle(run, candidate)
    binding = {name: file_sha256(run / name) for name in ("campaign.json", "experiments.jsonl")}
    metadata = {"optimize_lifecycle": {"schema_version": 1, "mode": "campaign", "bindings": binding}}
    candidate.write_text(json.dumps({"metadata": metadata}), encoding="utf-8")
    cli("admit", run, "--input", str(candidate))
    cli("begin", run, "--hypothesis", "Pending must block promotion")
    with pytest.raises(SystemExit, match="pending-iteration"):
        validator._validate_optimize_lifecycle(run, candidate)
    cli("finish", run, "--metric", "10", "--guard", "pass", "--decision", "reverted")
    with pytest.raises(SystemExit, match="evidence-hash-mismatch"):
        validator._validate_optimize_lifecycle(run, candidate)
    metadata["optimize_lifecycle"]["bindings"]["experiments.jsonl"] = file_sha256(run / "experiments.jsonl")
    candidate.write_text(json.dumps({"metadata": metadata}), encoding="utf-8")

    admitted = cli("admit", run, "--input", str(candidate))

    assert admitted["best_metric"] == 10


def test_historical_pass_fail_and_completed_budget_remain_admissible(
    campaign: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    """New lifecycle admission preserves old reports and validates completed scheduling budgets.

    A pre-lifecycle report with a pass/fail metric is admitted as historical; a managed run that spent its whole budget
    still validates, but refuses a further iteration.
    """
    historical = tmp_path / "historical"
    historical.mkdir()
    candidate = historical / "result.candidate.json"
    candidate.write_text('{"metadata": {}, "metric": "pass"}', encoding="utf-8")
    assert cli("admit", historical, "--input", str(candidate)) == {"mode": "historical"}
    _, run, _ = campaign
    exhaust_budget(run)
    assert cli("validate", run)["next_iteration"] == 4
    assert "iteration-budget-exhausted" in cli("begin", run, "--hypothesis", "Extra iteration", success=False)["error"]


def test_out_of_scope_edit_cannot_become_an_accepted_checkpoint(campaign: tuple[Path, Path, Path]) -> None:
    """A better number cannot authorize absorbing an unrelated tracked change.

    The in-scope value file improves but is staged along with its edit; the finish is refused as an out-of-scope change.
    """
    repo, run, _ = campaign
    cli("begin", run, "--hypothesis", "Measured improvement")
    (repo / "value.txt").write_text("8\n", encoding="utf-8")
    git(repo, "add", "value.txt")

    error = cli("finish", run, "--metric", "8", "--guard", "pass", "--decision", "kept", success=False)["error"]

    assert "out-of-scope-change" in error


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
    """Appending a second event must preserve a valid unterminated first JSON line.

    The first record's trailing newline is stripped, as an interrupted writer might leave it; the next append must add
    the separator rather than glue two records together.
    """
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


def _begin_attempt(run: Path) -> None:
    """Start an attempt so a finish has something to close."""
    cli("begin", run, "--hypothesis", "Measured improvement")


def _no_attempt(run: Path) -> None:
    """Start no attempt."""


def _edit_value(repo: Path) -> None:
    """Make the in-scope improvement."""
    (repo / "value.txt").write_text("8\n", encoding="utf-8")


def _keep_value(repo: Path) -> None:
    """Leave the in-scope file as committed."""


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
@pytest.mark.parametrize(
    ("action", "prepare", "edit", "arguments_for", "target_name"),
    [
        pytest.param(
            "init",
            _no_attempt,
            _edit_value,
            lambda repo, config: ("--root", str(repo), "--config", str(config), "--baseline", "8"),
            "hidden-run",
            id="init",
        ),
        pytest.param(
            "finish",
            _begin_attempt,
            _edit_value,
            lambda repo, config: ("--metric", "8", "--guard", "pass", "--decision", "kept"),
            "run",
            id="finish",
        ),
        pytest.param("resume", _no_attempt, _keep_value, lambda repo, config: (), "run", id="resume"),
    ],
)
def test_hidden_index_modes_cannot_hide_tracked_changes(
    campaign: tuple[Path, Path, Path],
    flag: str,
    action: str,
    prepare: Callable[[Path], None],
    edit: Callable[[Path], None],
    arguments_for: Callable[[Path, Path], tuple[str, ...]],
    target_name: str,
) -> None:
    """Reject diff-hidden index entries without rewriting the index or campaign.

    An out-of-scope file is marked assume-unchanged or skip-worktree and then modified, so plain `git diff` would show
    nothing; init, finish and resume must each still refuse the run and leave the index and ledger untouched.
    """
    repo, run, config = campaign
    prepare(run)
    git(repo, "update-index", flag, "outside.txt")
    index_path = repo / ".git" / "index"
    before = index_path.read_bytes()
    (repo / "outside.txt").write_text("hidden change\n", encoding="utf-8")
    edit(repo)

    error = cli(action, run.with_name(target_name), *arguments_for(repo, config), success=False)["error"]

    assert "unsupported-hidden-index-entry" in error
    assert index_path.read_bytes() == before
    assert (run / "experiments.jsonl").read_bytes() == b""


@pytest.fixture
def run_alias(campaign: tuple[Path, Path, Path], tmp_path: Path) -> tuple[Path, Path, Path]:
    """Provide a bound lifecycle candidate in the run and a directory symlink aliasing the run."""
    _, run, _ = campaign
    alias = tmp_path / "run-alias"
    alias.symlink_to(run, target_is_directory=True)
    candidate = run / "result.candidate.json"
    bindings = {name: file_sha256(run / name) for name in ("campaign.json", "experiments.jsonl")}
    metadata = {"optimize_lifecycle": {"schema_version": 1, "mode": "campaign", "bindings": bindings}}
    candidate.write_text(json.dumps({"metadata": metadata}), encoding="utf-8")
    return run, alias, candidate


@pytest.mark.skipif(not DIRECTORY_SYMLINKS_AVAILABLE, reason="directory symlinks unavailable")
def test_run_alias_admission_is_supported(run_alias: tuple[Path, Path, Path]) -> None:
    """Accept a producer-supported run alias, so a run reached through a directory link is still admitted."""
    _, alias, candidate = run_alias

    assert cli("admit", alias, "--input", str(candidate))["best_metric"] == 10


@pytest.mark.skipif(not DIRECTORY_SYMLINKS_AVAILABLE, reason="directory symlinks unavailable")
def test_run_alias_admission_rejects_linked_descendant_evidence(
    run_alias: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    """Reject evidence below an aliased run that is itself a link leaving the run.

    The campaign file is moved out and replaced by a link through an escaping directory link; admission through the
    alias must name the escape rather than follow it.
    """
    run, alias, candidate = run_alias
    original = run / "campaign.json"
    original.rename(tmp_path / "linked-campaign.json")
    directory = run / "linked"
    directory.symlink_to(tmp_path, target_is_directory=True)
    original.symlink_to(directory / "linked-campaign.json")

    error = cli("admit", alias, "--input", str(candidate), success=False)["error"]

    assert "evidence-path-escape" in error


def managed_handoff(run: Path, mode: str, rows: list[list[str]]) -> subprocess.CompletedProcess[str]:
    """Render, write and validate an ordinary result from real managed evidence."""
    fixtures = _load(PLUGIN / "tests" / "review" / "handoff" / "test_final_handoff.py", "optimize_handoff_fixtures")
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
        "bindings": {name: file_sha256(run / name) for name in names},
    }
    (run / "hypothesis.md").write_text("Measured reduction\n", encoding="utf-8")
    (run / "comparison.md").write_text("baseline 10 after 8 delta -2 guard pass confidence 0.95\n", encoding="utf-8")
    writer = subprocess.run(
        [
            sys.executable,
            str(SHARED / "write-result.py"),
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
    return validate_result(run, candidate)


def validate_result(run: Path, result: Path) -> subprocess.CompletedProcess[str]:
    """Run the shared optimize artifact validator on a result file."""
    return subprocess.run(
        [
            sys.executable,
            str(SHARED / "validate-artifacts.py"),
            "--skill",
            "optimize",
            "--out",
            str(run),
            "--result",
            str(result),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def two_attempts(campaign: tuple[Path, Path, Path]) -> tuple[Path, Path]:
    """Record one kept improvement and one reverted attempt whose guard failed."""
    repo, run, _ = campaign
    cli("begin", run, "--hypothesis", "Measured improvement")
    (repo / "value.txt").write_text("8\n", encoding="utf-8")
    cli("finish", run, "--metric", "8", "--guard", "pass", "--decision", "kept")
    cli("begin", run, "--hypothesis", "Rejected follow-up")
    cli("finish", run, "--metric", "9", "--guard", "fail", "--decision", "reverted")
    return repo, run


def _replace_cell(rows: list[list[str]], column: int, wrong: str) -> list[list[str]]:
    """Return a copy of the rows with one cell of the first row replaced."""
    return [[*rows[0][:column], wrong, *rows[0][column + 1 :]], *rows[1:]]


SUMMARY_ROWS = [["campaign", "10", "8", "-2", "fail", "kept"]]
ATTEMPT_ROWS = [["1", "10", "8", "-2", "pass", "kept"], ["2", "8", "9", "1", "fail", "reverted"]]
SUMMARY_CORRUPTIONS = [(2, "1"), (3, "-9"), (4, "pass"), (5, "reverted")]
ATTEMPT_CORRUPTIONS = [(2, "1"), (3, "-9"), (4, "fail"), (5, "reverted")]
FINAL_ROW_CASES = [
    pytest.param(
        SUMMARY_ROWS,
        [_replace_cell(SUMMARY_ROWS, column, wrong) for column, wrong in SUMMARY_CORRUPTIONS],
        id="summary",
    ),
    pytest.param(
        ATTEMPT_ROWS,
        [*(_replace_cell(ATTEMPT_ROWS, column, wrong) for column, wrong in ATTEMPT_CORRUPTIONS), ATTEMPT_ROWS[:1]],
        id="per-attempt",
    ),
]
MODES = ["campaign", "resume", "retrospective"]


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(("rows", "corrupted_rows"), FINAL_ROW_CASES)
def test_managed_final_rows_reconcile_and_promote_through_ordinary_pipeline(
    two_attempts: tuple[Path, Path], mode: str, rows: list[list[str]], corrupted_rows: list[list[list[str]]]
) -> None:
    """Bind published values, guards and decisions to complete retained attempts.

    Rows that agree with the retained attempts (as one campaign summary or one row per attempt) pass the ordinary
    pipeline, and the candidate then promotes to the canonical result and validates there.
    """
    _, run = two_attempts

    accepted = managed_handoff(run, mode, rows)
    (run / "result.candidate.json").replace(run / "result.json")
    promoted = validate_result(run, run / "result.json")

    assert accepted.returncode == 0, accepted.stderr
    assert promoted.returncode == 0, promoted.stderr


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(("rows", "corrupted_rows"), FINAL_ROW_CASES)
def test_managed_final_rows_that_disagree_with_attempts_are_rejected(
    two_attempts: tuple[Path, Path], mode: str, rows: list[list[str]], corrupted_rows: list[list[list[str]]]
) -> None:
    """Published values, guards or decisions that contradict the retained attempts are refused.

    Each corrupted copy changes one cell (after, delta, guard or decision); the per-attempt table also loses a row.
    """
    _, run = two_attempts

    results = [managed_handoff(run, mode, bad) for bad in corrupted_rows]

    assert all(result.returncode != 0 for result in results)
    assert all(MISMATCH in result.stderr for result in results)


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
    git(repo, "add", ".gitattributes")
    git(repo, "add", "--renormalize", ".")
    git(repo, "commit", "-qm", "Freeze text normalization")
    settings = json.loads(config.read_text())
    settings["scope_files"] = ["./" + name for name in settings["scope_files"]]
    config.write_text(json.dumps(settings), encoding="utf-8")
    run = run.with_name("normalized-run")
    cli("init", run, "--root", str(repo), "--config", str(config), "--baseline", "10")
    return repo, run, config


def test_normalized_outside_bytes_cannot_be_kept(normalized_campaign: tuple[Path, Path, Path]) -> None:
    """Clean Git content cannot hide changed measurement-visible outside bytes.

    An out-of-scope file gets CRLF line endings that the repository's text normalization hides from `git diff`; the
    finish must still be refused, with the ledger, pending marker and index untouched.
    """
    repo, run, _ = normalized_campaign
    cli("begin", run, "--hypothesis", "Measured reduction")
    index = (repo / ".git" / "index").read_bytes()
    assert (repo / "outside.txt").read_bytes() == b"unrelated\n"
    (repo / "outside.txt").write_bytes(b"unrelated\r\n")
    assert not git_bytes(repo, "diff", "HEAD", "--", "outside.txt")
    (repo / "value.txt").write_bytes(b"8\n")

    error = cli("finish", run, "--metric", "8", "--guard", "pass", "--decision", "kept", success=False)

    assert "out-of-scope-change" in error["error"]
    assert (run / "experiments.jsonl").read_bytes() == b""
    assert (run / "pending.json").is_file()
    assert (repo / ".git" / "index").read_bytes() == index


@pytest.fixture
def drifted_normalized_campaign(normalized_campaign: tuple[Path, Path, Path]) -> tuple[Path, Path]:
    """Keep a scoped normalized edit, publish it, then drift an unrelated file's raw bytes after the checkpoint."""
    repo, run, _ = normalized_campaign
    assert (repo / "value.txt").read_bytes() == b"10\n"
    assert (repo / "outside.txt").read_bytes() == b"unrelated\n"
    cli("begin", run, "--hypothesis", "Scoped normalization")
    (repo / "value.txt").write_bytes(b"10\r\n")
    assert not git_bytes(repo, "diff", "HEAD", "--", "value.txt")
    cli("finish", run, "--metric", "8", "--guard", "pass", "--decision", "kept")
    accepted = managed_handoff(run, "campaign", [["1", "10", "8", "-2", "pass", "kept"]])
    assert accepted.returncode == 0, accepted.stderr
    (repo / "outside.txt").write_bytes(b"unrelated\r\n")
    assert not git_bytes(repo, "diff", "HEAD", "--", "outside.txt")
    return repo, run


@pytest.mark.parametrize(
    ("action", "extra_arguments"),
    [
        pytest.param("resume", (), id="resume"),
        pytest.param("validate", (), id="validate"),
        pytest.param("admit", ("--input", "result.candidate.json"), id="admit"),
    ],
)
def test_normalized_post_checkpoint_drift_reaches_every_live_consumer(
    drifted_normalized_campaign: tuple[Path, Path], action: str, extra_arguments: tuple[str, ...]
) -> None:
    """A valid scoped normalized edit remains accepted until unrelated raw bytes drift.

    After the drift, resume, validate and admit each report the source mismatch instead of trusting Git's clean diff.
    """
    _, run = drifted_normalized_campaign
    arguments = [str(run / name) if name.endswith(".json") else name for name in extra_arguments]

    error = cli(action, run, *arguments, success=False)

    assert "source-mismatch" in error["error"]


def test_normalized_post_checkpoint_drift_rejects_ordinary_republication(
    drifted_normalized_campaign: tuple[Path, Path],
) -> None:
    """Rendering the same final rows through the ordinary pipeline fails once unrelated raw bytes have drifted."""
    _, run = drifted_normalized_campaign

    rejected = managed_handoff(run, "campaign", [["1", "10", "8", "-2", "pass", "kept"]])

    assert rejected.returncode != 0
    assert "source-mismatch" in rejected.stderr


@pytest.mark.skipif(not POSIX_FILE_MODES_AVAILABLE, reason="executable mode changes unavailable")
def test_core_filemode_false_does_not_hide_outside_executable_change(campaign: tuple[Path, Path, Path]) -> None:
    """Actual executable identity remains frozen when Git ignores mode changes.

    With `core.filemode=false` an out-of-scope file gains its executable bit, which `git diff` ignores; the finish must
    still be refused as an out-of-scope change.
    """
    repo, run, _ = campaign
    git(repo, "config", "core.filemode", "false")
    cli("begin", run, "--hypothesis", "Measured reduction")
    original = (repo / "outside.txt").stat().st_mode
    (repo / "outside.txt").chmod(original ^ 0o111)
    assert not git_bytes(repo, "diff", "HEAD", "--", "outside.txt")
    (repo / "value.txt").write_bytes(b"8\n")

    error = cli("finish", run, "--metric", "8", "--guard", "pass", "--decision", "kept", success=False)["error"]

    assert "out-of-scope-change" in error


@pytest.mark.skipif(not DIRECTORY_SYMLINKS_AVAILABLE, reason="directory symlinks unavailable")
def test_tracked_directory_link_is_rejected_before_descendant_read(
    campaign: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    """A replaced tracked ancestor cannot make source reads escape the repository.

    A tracked directory is replaced by a link to an outside directory holding a same-named file; the snapshot must
    refuse the link ancestor without reading through it or touching the index.
    """
    repo, run, config = campaign
    nested = repo / "nested"
    nested.mkdir()
    (nested / "tracked.txt").write_bytes(b"inside\n")
    git(repo, "add", "nested/tracked.txt")
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
    """External target contents stay outside identity while the link text remains bound.

    Changing the bytes a tracked link points to leaves the snapshot identical; retargeting the link itself changes it.
    """
    repo, run, config = campaign
    target = tmp_path / "target.txt"
    target.write_bytes(b"original\n")
    link = repo / "link.txt"
    link.symlink_to(target)
    git(repo, "add", "link.txt")
    frozen = cli("snapshot", run, "--root", str(repo), "--config", str(config))
    target.write_bytes(b"changed external bytes\n")
    assert cli("snapshot", run, "--root", str(repo), "--config", str(config)) == frozen
    link.unlink()
    link.symlink_to(tmp_path / "different-target.txt")

    changed = cli("snapshot", run, "--root", str(repo), "--config", str(config))

    assert changed["tracked_bytes"] != frozen["tracked_bytes"]
    assert changed["outside_scope_bytes"] != frozen["outside_scope_bytes"]


def test_submodule_snapshot_is_explicitly_unsupported(campaign: tuple[Path, Path, Path]) -> None:
    """A Gitlink is not misrepresented as exact working-tree file content.

    A gitlink entry is added to the index; the snapshot must name the unsupported submodule and leave the index as it
    found it.
    """
    repo, run, config = campaign
    head = git_bytes(repo, "rev-parse", "HEAD").decode().strip()
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{head},component")
    index = (repo / ".git" / "index").read_bytes()

    error = cli("snapshot", run, "--root", str(repo), "--config", str(config), success=False)

    assert "unsupported-source-submodule" in error["error"]
    assert (repo / ".git" / "index").read_bytes() == index
