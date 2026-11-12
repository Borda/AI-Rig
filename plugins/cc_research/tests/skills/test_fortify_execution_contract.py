"""Exercise Fortify's source gates, worktree-relative paths and measurement provenance."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from _research_shell_plugin import render_shell

_PLUGIN = Path(__file__).resolve().parents[2]
_SKILL = _PLUGIN / "skills/fortify/SKILL.md"
_BASH = shutil.which("bash")
_requires_bash = pytest.mark.skipif(_BASH is None, reason="Bash is required for shell recipes")
_SHELLS = [
    pytest.param("bash", marks=pytest.mark.skipif(_BASH is None, reason="bash unavailable")),
    pytest.param("zsh", marks=pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh unavailable")),
]


def _block(marker: str) -> str:
    """Read the executable Bash block containing a distinctive instruction."""
    current: list[str] | None = None
    for line in _SKILL.read_text(encoding="utf-8").splitlines():
        if line == "```bash":
            current = []
        elif line == "```" and current is not None:
            body = "\n".join(current)
            if marker in body:
                return body
            current = None
        elif current is not None:
            current.append(line)
    raise AssertionError(f"No Bash block containing {marker!r}")


def _git(cwd: Path, env: dict[str, str], *args: str) -> str:
    """Run real Git only inside the temporary campaign repository."""
    result = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def campaign(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    """Prepare a real repository and detached variant worktree entirely under tmp_path."""
    origin = tmp_path / "project with spaces"
    run = origin / ".experiments/fortify-test"
    run.mkdir(parents=True)
    variant = run / "worktrees/variant-no-component"
    state = origin / ".experiments/state/source"
    state.mkdir(parents=True)
    (state / "state.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "best_metric": 10,
                "config": {"metric_cmd": "printf '10\\n'", "guard_cmd": "true", "compute": "local"},
            }
        ),
        encoding="utf-8",
        newline="\n",
    )
    sentinels = tmp_path / "sentinels"
    sentinels.mkdir()
    values = {
        "orig-dir": origin.as_posix(),
        "dir": ".experiments/fortify-test",
        "variant-name": variant.name,
        "run-id": "source",
        "runs": "1",
        "metric-cmd": "cat m.txt",
        "program-file": "",
    }
    for name, value in values.items():
        (sentinels / f"fortify-{name}-test").write_text(value + "\n", encoding="utf-8", newline="\n")
    env = os.environ.copy()
    env.pop("STATE_DIR_BASE", None)
    env.pop("METRIC_CMD", None)
    env.pop("GUARD_CMD", None)
    env.update(
        CLAUDE_CODE_SESSION_ID="test",
        CLAUDE_PLUGIN_ROOT=_PLUGIN.as_posix(),
        TMPDIR=sentinels.as_posix(),
        TEST_PYTHON=Path(sys.executable).as_posix(),
        PYTHONIOENCODING="cp1252",
        ARGUMENTS="source --runs 1",
        GIT_AUTHOR_NAME="Test",
        GIT_AUTHOR_EMAIL="test@example.invalid",
        GIT_COMMITTER_NAME="Test",
        GIT_COMMITTER_EMAIL="test@example.invalid",
    )
    _git(origin, env, "init", "--template=")
    _git(origin, env, "config", "core.hooksPath", str(tmp_path / "no-hooks"))
    (origin / ".gitignore").write_text(".experiments/\n.reports/\n", encoding="utf-8", newline="\n")
    for value in (8, 9, 10):
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = f"2000-01-{value - 7:02d}T00:00:00+00:00"
        (origin / "m.txt").write_text(f"{value}\n", encoding="utf-8", newline="\n")
        _git(origin, env, "add", ".gitignore", "m.txt")
        _git(origin, env, "commit", "-m", f"Metric {value}")
    best = _git(origin, env, "rev-parse", "HEAD")
    first = _git(origin, env, "rev-parse", "HEAD^")
    (sentinels / "fortify-best-commit-test").write_text(best + "\n", encoding="utf-8", newline="\n")
    (run / "variants.jsonl").write_text(
        json.dumps({"variant_name": "no-component", "revert_commits": [first, best]}) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    _git(origin, env, "worktree", "add", "--detach", str(variant), best)
    return origin, run, env


def _run(marker: str, cwd: Path, env: dict[str, str], shell: str = "bash") -> subprocess.CompletedProcess[str]:
    """Run a shipped shell block with real jq, helpers and the temporary checkout's Git."""
    prelude = 'python() { "$TEST_PYTHON" "$@"; }\n'
    return subprocess.run(
        [
            shutil.which(shell),
            "-c",
            render_shell(prelude + _block(marker), Path(env.get("CLAUDE_PLUGIN_ROOT", str(_PLUGIN))), "research"),
        ],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


@pytest.fixture
def reverted_campaign(campaign: tuple[Path, Path, dict[str, str]]) -> tuple[Path, Path, dict[str, str]]:
    """Apply the shipped revert blocks before measuring an ablated checkout."""
    _origin, run, env = campaign
    variant = run / "worktrees/variant-no-component"
    ordered = _run("REVERT_COMMITS_RAW=", variant, env)
    assert ordered.returncode == 0, ordered.stdout + ordered.stderr
    reverted = _run("git revert $(", variant, env)
    assert reverted.returncode == 0, reverted.stdout + reverted.stderr
    return campaign


@_requires_bash
@pytest.mark.integration
@pytest.mark.parametrize(
    "shell_cwd",
    [
        "/c/Users/runner/project with spaces",
        "C:/Users/runner/project with spaces",
        r"C:\Users\runner\project with spaces",
    ],
)
def test_metric_receipt_uses_native_physical_path(
    reverted_campaign: tuple[Path, Path, dict[str, str]], shell_cwd: str
) -> None:
    """Shell path spellings cannot reject a real checkout or contaminate its metric receipt."""
    _origin, run, env = reverted_campaign
    variant = run / "worktrees/variant-no-component"
    shell_env = variant / "shell-env"
    shell_env.write_text(
        f"pwd() {{ printf '%s\\n' {shlex.quote(shell_cwd)}; }}\n"
        f"realpath() {{ printf '%s\\n' {shlex.quote(variant.resolve().as_posix())}; }}\n",
        encoding="utf-8",
        newline="\n",
    )
    env["BASH_ENV"] = shell_env.as_posix()
    measured = _run('--kind metric --command-override "$METRIC_CMD"', variant, env)
    assert measured.returncode == 0, measured.stdout + measured.stderr
    (run / "metric-runs.jsonl.rec").write_text(
        json.dumps({"variant": variant.name, "metric": 8, "exit": 0}) + "\n", encoding="utf-8", newline="\n"
    )
    recorded = _run('fortify_metric_stats.py" status', variant, env)
    assert recorded.returncode == 0, recorded.stdout + recorded.stderr
    row = json.loads((run / "metric-runs.jsonl").read_text(encoding="utf-8"))
    assert row["checkout"]["cwd"] == variant.resolve().as_posix()


def test_original_directory_capture_ignores_windows_text_translation(tmp_path: Path) -> None:
    """Native path captures emit UTF-8 without CRLF even with a Windows-style text stream."""
    checkout = tmp_path / "project \u2603"
    checkout.mkdir()
    line = next(line for line in _block("ORIG_DIR=$(").splitlines() if line.startswith("ORIG_DIR="))
    code = shlex.split(line.partition("python -c ")[2].partition(") ||")[0])[0]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            'import io, sys; sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="cp1252", newline="\\r\\n"); exec(sys.argv[1])',
            code,
        ],
        cwd=checkout,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == checkout.resolve().as_posix().encode("utf-8")


@pytest.fixture
def approved_campaign(campaign: tuple[Path, Path, dict[str, str]]) -> tuple[Path, Path, dict[str, str]]:
    """Provide the same-program judge receipt needed after the source selection gates."""
    origin, _run_dir, _env = campaign
    state = origin / ".experiments/state/source/state.json"
    data = json.loads(state.read_text(encoding="utf-8"))
    data["program_file"] = "program.md"
    state.write_text(json.dumps(data), encoding="utf-8", newline="\n")
    (origin / "program.md").write_text("Controlled campaign", encoding="utf-8", newline="\n")
    reports = origin / ".reports/research"
    reports.mkdir(parents=True)
    program_hash = hashlib.sha256((origin / "program.md").read_bytes()).hexdigest()
    (reports / "judge-test.md").write_text(
        f"Verdict: APPROVED\nProgram: program.md\nProgram SHA256: {program_hash}\n", encoding="utf-8", newline="\n"
    )
    return campaign


@_requires_bash
@pytest.mark.integration
@pytest.mark.parametrize("status", ["completed", "goal-achieved"])
def test_explicit_completed_local_run_reaches_judge(
    approved_campaign: tuple[Path, Path, dict[str, str]], status: str
) -> None:
    """The source gates preserve both accepted terminal statuses and local execution."""
    origin, _run_dir, env = approved_campaign
    state = origin / ".experiments/state/source/state.json"
    data = json.loads(state.read_text(encoding="utf-8"))
    data["status"] = status
    state.write_text(json.dumps(data), encoding="utf-8", newline="\n")

    result = _run('STATE_DIR_BASE="${STATE_DIR_BASE:-.experiments/state}"', origin, env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (Path(env["TMPDIR"]) / "fortify-judge-verdict-test").read_text(encoding="utf-8") == "APPROVED\n"


@_requires_bash
@pytest.mark.integration
@pytest.mark.parametrize("compute", ["docker", "colab"])
def test_skip_run_accepts_nonlocal_source_campaign(
    approved_campaign: tuple[Path, Path, dict[str, str]], compute: str
) -> None:
    """Candidate-only planning keeps the judge prerequisite without executing nonlocal metrics."""
    origin, _run_dir, env = approved_campaign
    state = origin / ".experiments/state/source/state.json"
    data = json.loads(state.read_text(encoding="utf-8"))
    data["config"]["compute"] = compute
    data["config"]["metric_cmd"] = "touch metric-executed"
    state.write_text(json.dumps(data), encoding="utf-8", newline="\n")
    env["ARGUMENTS"] += " --skip-run"

    result = _run('STATE_DIR_BASE="${STATE_DIR_BASE:-.experiments/state}"', origin, env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (Path(env["TMPDIR"]) / "fortify-args-skip-run-test").read_text(encoding="utf-8") == "true\n"
    assert (Path(env["TMPDIR"]) / "fortify-judge-verdict-test").read_text(encoding="utf-8") == "APPROVED\n"
    assert not (origin / "metric-executed").exists()


@pytest.mark.integration
@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize("commit_count", [1, 2])
def test_revert_commits_are_found_from_variant_cwd(
    campaign: tuple[Path, Path, dict[str, str]], shell: str, commit_count: int
) -> None:
    """A relative run directory still resolves the actual component commits in 4c."""
    origin, run, env = campaign
    commits = [_git(origin, env, "rev-parse", "HEAD^"), _git(origin, env, "rev-parse", "HEAD")][-commit_count:]
    (run / "variants.jsonl").write_text(
        json.dumps({"variant_name": "no-component", "revert_commits": commits}) + "\n", encoding="utf-8", newline="\n"
    )

    result = _run("REVERT_COMMITS_RAW=", run / "worktrees/variant-no-component", env, shell)

    assert result.returncode == 0, result.stderr
    assert "FORTIFY_SKIP_VARIANT" not in result.stdout
    ordered = Path(env["TMPDIR"]) / "fortify-revert-sorted-test"
    assert ordered.read_text(encoding="utf-8").split() == list(reversed(commits))
    assert not (run / "results.jsonl").exists()


@_requires_bash
@pytest.mark.integration
def test_missing_revert_is_recorded_from_variant_cwd(campaign: tuple[Path, Path, dict[str, str]]) -> None:
    """A genuinely empty component writes its skip record to the original run directory."""
    _origin, run, env = campaign
    (run / "variants.jsonl").write_text(
        '{"variant_name":"no-component","revert_commits":[]}\n', encoding="utf-8", newline="\n"
    )

    result = _run("REVERT_COMMITS_RAW=", run / "worktrees/variant-no-component", env)

    assert result.returncode == 0, result.stderr
    assert "FORTIFY_SKIP_VARIANT=1" in result.stdout
    assert json.loads((run / "results.jsonl").read_text(encoding="utf-8")) == {
        "variant": "variant-no-component",
        "status": "revert-missing",
    }


@_requires_bash
@pytest.mark.integration
def test_conflict_result_appends_before_leaving_worktree(campaign: tuple[Path, Path, dict[str, str]]) -> None:
    """4g consumes the conflict record even when cleanup has not restored the original cwd."""
    _origin, run, env = campaign
    record = '{"variant":"variant-no-component","status":"revert-conflict"}\n'
    (run / "results.jsonl.rec").write_text(record, encoding="utf-8", newline="\n")

    result = _run('"$_FDIR/results.jsonl"', run / "worktrees/variant-no-component", env)

    assert result.returncode == 0, result.stderr
    assert (run / "results.jsonl").read_text(encoding="utf-8") == record
    assert not (run / "results.jsonl.rec").exists()


@_requires_bash
@pytest.mark.integration
@pytest.mark.parametrize("status", ["running", "failed", "paused"])
def test_explicit_run_rejects_incomplete_campaign(campaign: tuple[Path, Path, dict[str, str]], status: str) -> None:
    """Selecting a run by name cannot bypass the completed-campaign prerequisite."""
    origin, _run_dir, env = campaign
    state = origin / ".experiments/state/source/state.json"
    data = json.loads(state.read_text(encoding="utf-8"))
    data["status"] = status
    state.write_text(json.dumps(data), encoding="utf-8", newline="\n")

    result = _run('STATE_DIR_BASE="${STATE_DIR_BASE:-.experiments/state}"', origin, env)

    assert result.returncode != 0
    assert f"run source status '{status}'" in result.stdout
    assert "fortify needs a completed or goal-achieved campaign" in result.stdout
    assert "Finish it with /research:run first" in result.stdout
    assert "judge" not in result.stdout.lower()


@_requires_bash
@pytest.mark.integration
@pytest.mark.parametrize("compute", ["docker", "colab", "unknown"])
def test_nonlocal_compute_stops_before_judge_or_measurement(
    campaign: tuple[Path, Path, dict[str, str]], compute: str
) -> None:
    """Unsupported execution routes never reach the expensive full-baseline run."""
    origin, _run_dir, env = campaign
    state = origin / ".experiments/state/source/state.json"
    data = json.loads(state.read_text(encoding="utf-8"))
    data["config"]["compute"] = compute
    state.write_text(json.dumps(data), encoding="utf-8", newline="\n")

    result = _run('STATE_DIR_BASE="${STATE_DIR_BASE:-.experiments/state}"', origin, env)

    assert result.returncode != 0
    assert "! UNSUPPORTED" in result.stdout
    assert "local" in result.stdout
    assert "judge" not in result.stdout.lower()


@_requires_bash
@pytest.mark.integration
def test_metric_rejects_main_repository_cwd(campaign: tuple[Path, Path, dict[str, str]]) -> None:
    """Every metric call enforces the variant cwd even if an earlier navigation check passed."""
    origin, _run_dir, env = campaign
    (Path(env["TMPDIR"]) / "fortify-metric-cmd-test").write_text(
        "printf ran > metric-executed\n", encoding="utf-8", newline="\n"
    )

    result = _run('--kind metric --command-override "$METRIC_CMD"', origin, env)

    assert result.returncode != 0
    assert "! BLOCKED" in result.stdout
    assert not (origin / "metric-executed").exists()


@pytest.mark.integration
@pytest.mark.parametrize("shell", _SHELLS)
def test_metric_rejects_an_unreverted_ablation(campaign: tuple[Path, Path, dict[str, str]], shell: str) -> None:
    """A correct worktree path cannot certify an ablation still checked out at the best commit."""
    _origin, run, env = campaign
    variant = run / "worktrees/variant-no-component"
    ordered = _run("REVERT_COMMITS_RAW=", variant, env, shell)
    assert ordered.returncode == 0, ordered.stderr
    (Path(env["TMPDIR"]) / "fortify-metric-cmd-test").write_text(
        "touch metric-executed\n", encoding="utf-8", newline="\n"
    )

    measured = _run('--kind metric --command-override "$METRIC_CMD"', variant, env, shell)

    assert measured.returncode != 0
    assert "variant checkout is not best_commit plus its reverts" in measured.stdout
    assert not (variant / "metric-executed").exists()
    assert (run / "metric-checkout-verified.json").read_text(encoding="utf-8") == ""


@pytest.mark.integration
@pytest.mark.parametrize("shell", _SHELLS)
def test_full_metric_rejects_extra_commits_despite_stale_reverts(
    reverted_campaign: tuple[Path, Path, dict[str, str]], shell: str
) -> None:
    """The full variant requires the best commit even when another variant left revert sentinels."""
    origin, run, env = reverted_campaign
    full = run / "worktrees/variant-full"
    _git(origin, env, "worktree", "add", "--detach", str(full), "HEAD")
    _git(full, env, "commit", "--allow-empty", "-m", "Extra commit")
    _git(full, env, "commit", "--allow-empty", "-m", "Another extra commit")
    (Path(env["TMPDIR"]) / "fortify-variant-name-test").write_text("variant-full\n", encoding="utf-8", newline="\n")
    (Path(env["TMPDIR"]) / "fortify-metric-cmd-test").write_text(
        "touch metric-executed\n", encoding="utf-8", newline="\n"
    )

    measured = _run('--kind metric --command-override "$METRIC_CMD"', full, env, shell)

    assert measured.returncode != 0
    assert "variant checkout is not best_commit plus its reverts" in measured.stdout
    assert not (full / "metric-executed").exists()


@_requires_bash
@pytest.mark.integration
@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("cwd", "other-checkout", id="wrong-directory"),
        pytest.param("head", "0" * 40, id="wrong-commit"),
        pytest.param("variant", "variant-full", id="wrong-variant"),
        pytest.param("verified", False, id="unverified-measurement"),
        pytest.param("exit", 9, id="wrong-exit-code"),
    ],
)
def test_metric_run_binds_checkout_before_ranking(
    reverted_campaign: tuple[Path, Path, dict[str, str]], field: str, value: str | int | bool
) -> None:
    """Actual measurement provenance survives staging and a tampered checkout prevents ranking."""
    origin, run, env = reverted_campaign
    variant = run / "worktrees/variant-no-component"

    measured = _run('--kind metric --command-override "$METRIC_CMD"', variant, env)

    assert measured.returncode == 0, measured.stderr
    (run / "metric-runs.jsonl.rec").write_text(
        '{"variant":"variant-no-component","metric":10,"exit":0}\n', encoding="utf-8", newline="\n"
    )
    recorded = _run('"$_FDIR/metric-runs.jsonl" || exit 1', variant, env)
    assert recorded.returncode == 0, recorded.stderr
    row = json.loads((run / "metric-runs.jsonl").read_text(encoding="utf-8"))
    assert Path(row["checkout"]["cwd"]) == variant.resolve()
    assert len(row["checkout"]["head"]) == 40
    assert row["checkout"]["variant"] == row["variant"]
    assert row["checkout"]["exit"] == row["exit"]
    row["checkout"][field] = value
    (run / "metric-runs.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8", newline="\n")

    ranked = _run('rank "$_FDIR"', origin, env)

    assert ranked.returncode != 0
    assert "checkout" in ranked.stdout.lower()
    assert not (run / "results-deltas.jsonl").exists()


@_requires_bash
@pytest.mark.integration
def test_stale_receipt_cannot_record_an_unexecuted_metric(reverted_campaign: tuple[Path, Path, dict[str, str]]) -> None:
    """A failed second metric call cannot reuse the previous call's verified checkout receipt."""
    _origin, run, env = reverted_campaign
    variant = run / "worktrees/variant-no-component"
    first = _run('--kind metric --command-override "$METRIC_CMD"', variant, env)
    assert first.returncode == 0, first.stderr
    (Path(env["TMPDIR"]) / "fortify-metric-cmd-test").write_text("\n", encoding="utf-8", newline="\n")

    second = _run('--kind metric --command-override "$METRIC_CMD"', variant, env)

    assert second.returncode != 0
    (run / "metric-runs.jsonl.rec").write_text(
        '{"variant":"variant-no-component","metric":10,"exit":0}\n', encoding="utf-8", newline="\n"
    )
    recorded = _run('"$_FDIR/metric-runs.jsonl" || exit 1', variant, env)
    assert recorded.returncode != 0
    assert "checkout" in recorded.stdout.lower()
    assert not (run / "metric-runs.jsonl").exists()


@_requires_bash
@pytest.mark.integration
def test_metric_receipt_is_consumed_after_one_record(reverted_campaign: tuple[Path, Path, dict[str, str]]) -> None:
    """One real metric execution cannot certify two separately staged numeric samples."""
    _origin, run, env = reverted_campaign
    variant = run / "worktrees/variant-no-component"
    measured = _run('--kind metric --command-override "$METRIC_CMD"', variant, env)
    assert measured.returncode == 0, measured.stderr
    (run / "metric-runs.jsonl.rec").write_text(
        '{"variant":"variant-no-component","metric":10,"exit":0}\n', encoding="utf-8", newline="\n"
    )
    first = _run('"$_FDIR/metric-runs.jsonl" || exit 1', variant, env)
    assert first.returncode == 0, first.stderr
    (run / "metric-runs.jsonl.rec").write_text(
        '{"variant":"variant-no-component","metric":80,"exit":0}\n', encoding="utf-8", newline="\n"
    )

    replayed = _run('"$_FDIR/metric-runs.jsonl" || exit 1', variant, env)

    assert replayed.returncode != 0
    rows = (run / "metric-runs.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1
    assert json.loads(rows[0])["metric"] == 10


@pytest.mark.integration
@pytest.mark.parametrize("shell", _SHELLS)
def test_matching_checkout_evidence_allows_expected_ranking(
    campaign: tuple[Path, Path, dict[str, str]], shell: str
) -> None:
    """The provenance gate permits a measured ablation and preserves the statistical result."""
    origin, run, env = campaign
    best = _git(origin, env, "rev-parse", "HEAD")
    full = run / "worktrees/variant-full"
    _git(origin, env, "worktree", "add", "--detach", str(full), best)
    variant_sentinel = Path(env["TMPDIR"]) / "fortify-variant-name-test"
    variant_sentinel.write_text("variant-full\n", encoding="utf-8", newline="\n")
    full_measured = _run('--kind metric --command-override "$METRIC_CMD"', full, env, shell)
    assert full_measured.returncode == 0, full_measured.stdout + full_measured.stderr
    assert full_measured.stdout == "10\nMETRIC_EXIT=0\n"
    (run / "metric-runs.jsonl.rec").write_text(
        '{"variant":"variant-full","metric":10,"exit":0}\n', encoding="utf-8", newline="\n"
    )
    full_recorded = _run('"$_FDIR/metric-runs.jsonl" || exit 1', full, env, shell)
    assert full_recorded.returncode == 0, full_recorded.stdout + full_recorded.stderr
    variant_sentinel.write_text("variant-no-component\n", encoding="utf-8", newline="\n")
    variant = run / "worktrees/variant-no-component"
    ordered = _run("REVERT_COMMITS_RAW=", variant, env, shell)
    assert ordered.returncode == 0, ordered.stdout + ordered.stderr
    reverted = _run("git revert $(", variant, env, shell)
    assert reverted.returncode == 0, reverted.stdout + reverted.stderr
    measured = _run('--kind metric --command-override "$METRIC_CMD"', variant, env, shell)
    assert measured.returncode == 0, measured.stdout + measured.stderr
    assert measured.stdout == "8\nMETRIC_EXIT=0\n"
    (run / "metric-runs.jsonl.rec").write_text(
        '{"variant":"variant-no-component","metric":8,"exit":0}\n', encoding="utf-8", newline="\n"
    )
    recorded = _run('"$_FDIR/metric-runs.jsonl" || exit 1', variant, env, shell)
    assert recorded.returncode == 0, recorded.stderr
    (run / "results.jsonl").write_text(
        '{"variant":"full","status":"completed"}\n{"variant":"no-component","status":"completed"}\n',
        encoding="utf-8",
        newline="\n",
    )

    ranked = _run('rank "$_FDIR"', origin, env, shell)

    assert ranked.returncode == 0, ranked.stdout + ranked.stderr
    delta = json.loads((run / "results-deltas.jsonl").read_text(encoding="utf-8"))
    assert delta["importance_pct"] == 20
    assert delta["class"] == "SIGNIFICANT"
    rows = [json.loads(line) for line in (run / "metric-runs.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[0]["checkout"]["head"] == best
    assert rows[1]["checkout"]["head"] == _git(variant, env, "rev-parse", "HEAD")
    assert rows[1]["checkout"]["head"] != best
    assert _git(origin, env, "rev-parse", "HEAD") == best
