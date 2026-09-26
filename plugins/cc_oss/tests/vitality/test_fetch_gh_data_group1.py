"""Tests for ``bin/fetch_gh_data_group1.py``.

Arg-parsing tests run without any subprocess calls. The happy-path and individual-failure tests monkeypatch
``subprocess.run`` and ``which`` so no real ``gh`` invocation occurs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fetch_gh_data_group1 as fgd


class _FakeCompleted:
    """Minimal stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        """Store the status and output returned by a fake GitHub CLI command."""
        self.returncode = returncode
        self.stdout = stdout


def test_missing_repo_exits_1(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    """No ``--repo`` → exit 1 with '--repo required' on stderr."""
    rc = fgd.main(["--output-dir", str(tmp_path)])
    assert rc == 1
    assert "--repo required" in capsys.readouterr().err


def test_missing_output_dir_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    """Require an output directory when a repository is provided."""
    rc = fgd.main(["--repo", "owner/repo"])
    assert rc == 1
    assert "--output-dir required" in capsys.readouterr().err


def test_unknown_arg_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    """Unrecognized flag → exit 1 with 'unknown arg' on stderr."""
    rc = fgd.main(["--unknown"])
    assert rc == 1
    assert "unknown arg" in capsys.readouterr().err


def test_no_args_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    """No args → exit 1 (--repo required)."""
    rc = fgd.main([])
    assert rc == 1
    assert "--repo required" in capsys.readouterr().err


def test_build_datasets_returns_21_entries() -> None:
    """Return exactly 21 (name, args) tuples."""
    datasets = fgd._build_datasets("o/r", "2023-01-01", "2023-01-01T00:00:00Z", "2022-07-01T00:00:00Z")
    assert len(datasets) == 21
    names = [name for name, _ in datasets]
    assert "open_issues" in names
    assert "commits_50" in names


@pytest.mark.parametrize(
    "expected_name",
    [
        "open_issues",
        "closed_issues",
        "open_prs",
        "closed_prs",
        "commits",
        "releases",
        "contributor_stats",
        "root_contents",
        "repo_metadata",
        "dependabot_alerts",
        "secret_scanning_alerts",
        "fork_dates",
        "all_issues",
        "all_prs",
        "discussions",
        "responsiveness_gql",
        "review_coverage_gql",
        "ci_workflows",
        "ci_runs",
        "merged_prs_90d",
        "commits_50",
    ],
)
def test_build_datasets_includes_expected_dataset_names(expected_name: str) -> None:
    datasets = fgd._build_datasets("owner/repo", "2023-01-01", "2024-06-01T00:00:00Z", "2024-03-01T00:00:00Z")
    assert expected_name in {name for name, _ in datasets}


def test_build_datasets_commands_include_repo_and_cutoffs() -> None:
    datasets = dict(fgd._build_datasets("owner/repo", "2023-01-01", "2024-06-01T00:00:00Z", "2024-03-01T00:00:00Z"))
    assert datasets["open_issues"][2:4] == ["-R", "owner/repo"]
    assert "closed:>=2023-01-01" in datasets["closed_issues"]
    assert "merged:>=2024-06-01T00:00:00Z" in datasets["merged_prs_90d"]
    assert "repos/owner/repo/commits?per_page=100" in datasets["commits"]
    assert "-f" in datasets["discussions"]
    assert "owner=owner" in datasets["discussions"]
    assert "repo=repo" in datasets["discussions"]


def test_successful_fetch_writes_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """All gh calls succeed → one JSON file per dataset written, exit 0."""
    monkeypatch.setattr(
        fgd.subprocess,
        "run",
        lambda *a, **k: _FakeCompleted(returncode=0, stdout="[]"),
    )
    monkeypatch.setattr(fgd, "which", lambda _: "/fake/gh")
    rc = fgd.main(["--repo", "owner/repo", "--output-dir", str(tmp_path)])
    assert rc == 0
    json_files = list(tmp_path.glob("*.json"))
    assert len(json_files) == 21


def test_individual_failure_nonfatal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """All gh calls fail → empty files written, warnings on stderr, exit 0."""
    monkeypatch.setattr(
        fgd.subprocess,
        "run",
        lambda *a, **k: _FakeCompleted(returncode=1, stdout=""),
    )
    monkeypatch.setattr(fgd, "which", lambda _: "/fake/gh")
    rc = fgd.main(["--repo", "owner/repo", "--output-dir", str(tmp_path)])
    assert rc == 0
    err = capsys.readouterr().err
    assert "failed (non-fatal)" in err
    empty_files = [f for f in tmp_path.glob("*.json") if f.read_text() == ""]
    assert len(empty_files) == 21


def test_successful_fetch_file_content(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Successful gh call → file content equals gh stdout."""
    monkeypatch.setattr(
        fgd.subprocess,
        "run",
        lambda *a, **k: _FakeCompleted(returncode=0, stdout='[{"sha":"abc"}]'),
    )
    monkeypatch.setattr(fgd, "which", lambda _: "/fake/gh")
    fgd.main(["--repo", "owner/repo", "--output-dir", str(tmp_path)])
    assert (tmp_path / "open_issues.json").read_text() == '[{"sha":"abc"}]'


def test_custom_cutoffs_accepted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Explicit cutoff flags accepted without error."""
    monkeypatch.setattr(
        fgd.subprocess,
        "run",
        lambda *a, **k: _FakeCompleted(returncode=0, stdout="[]"),
    )
    monkeypatch.setattr(fgd, "which", lambda _: "/fake/gh")
    rc = fgd.main(
        [
            "--repo",
            "owner/repo",
            "--output-dir",
            str(tmp_path),
            "--cutoff-3y",
            "2023-01-01",
            "--cutoff-90d",
            "2024-06-01T00:00:00Z",
            "--cutoff-180d",
            "2024-03-01T00:00:00Z",
        ]
    )
    assert rc == 0


def test_gh_missing_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Return None → FileNotFoundError propagates."""
    monkeypatch.setattr(fgd, "which", lambda _: None)
    with pytest.raises(FileNotFoundError, match="gh"):
        fgd.main(["--repo", "owner/repo", "--output-dir", str(tmp_path)])
