"""Tests for ``bin/fetch_gh_data_group2.py``.

Arg-validation tests run without any subprocess calls. The happy-path and decode tests monkeypatch ``subprocess.run``
and ``which`` so no real ``gh`` invocation occurs.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import fetch_gh_data_group2 as fgd
import pytest


class _FakeCompleted:
    """Minimal stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        """Store the status and output returned by a fake GitHub CLI command."""
        self.returncode = returncode
        self.stdout = stdout


def _b64(text: str) -> str:
    """Encode UTF-8 text as base64 without a trailing newline.

    Examples:
        >>> _b64("hello")
        'aGVsbG8='
    """
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


# --- arg validation ---------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "needle"),
    [
        pytest.param(
            ["--repo", "repo", "--default-branch", "main", "--data-file", "out.jsonl"],
            "--owner required",
            id="missing-owner",
        ),
        pytest.param(
            ["--owner", "owner", "--default-branch", "main", "--data-file", "out.jsonl"],
            "--repo required",
            id="missing-repo",
        ),
        pytest.param(
            ["--owner", "owner", "--repo", "repo", "--data-file", "out.jsonl"],
            "--default-branch required",
            id="missing-default-branch",
        ),
        pytest.param(
            ["--owner", "owner", "--repo", "repo", "--default-branch", "main"],
            "--data-file required",
            id="missing-data-file",
        ),
        pytest.param(
            ["--owner", "../evil", "--repo", "repo", "--default-branch", "main", "--data-file", "out.jsonl"],
            "--owner must match",
            id="owner-traversal",
        ),
        pytest.param(
            ["--owner", "owner", "--repo", "../evil", "--default-branch", "main", "--data-file", "out.jsonl"],
            "--repo must match",
            id="repo-traversal",
        ),
        pytest.param(
            ["--owner", "owner", "--repo", "repo", "--default-branch", "..", "--data-file", "out.jsonl"],
            "--default-branch must match",
            id="branch-traversal",
        ),
        pytest.param(
            ["--owner", "owner", "--repo", "repo", "--default-branch", "a/../b", "--data-file", "out.jsonl"],
            "--default-branch must match",
            id="branch-embedded-traversal",
        ),
    ],
)
def test_invalid_args_exit_1_with_stderr_hint(argv: list[str], needle: str, capsys: pytest.CaptureFixture[str]) -> None:
    """A missing required arg, or a path-traversal pattern in an identifier arg, → exit 1 with the hint on stderr."""
    rc = fgd.main(argv)
    assert rc == 1
    assert needle in capsys.readouterr().err


# --- pure helpers -----------------------------------------------------------


def test_decode_b64_roundtrip() -> None:
    """Round-trip arbitrary UTF-8 text."""
    assert fgd._decode_b64(_b64("hello world\nline 2")) == "hello world\nline 2"


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("", id="empty-input-short-circuits"),
        pytest.param("!!!definitely-not-base64$$$", id="non-base64-garbage"),
    ],
)
def test_decode_b64_unusable_input_returns_empty(raw: str) -> None:
    """Empty raw input short-circuits to an empty result; non-base64 garbage yields an empty string (no exception)."""
    assert fgd._decode_b64(raw) == ""


@pytest.mark.parametrize(
    ("owner", "repo", "branch"),
    [
        pytest.param("owner", "repo", "main", id="all-valid"),
        pytest.param("o", "r", "release/1.x", id="slash-in-branch-allowed"),
    ],
)
def test_validate_args_accepts_valid_args(owner: str, repo: str, branch: str) -> None:
    """All valid args → ``None``; a branch may contain ``/`` (e.g. ``release/1.x``)."""
    assert fgd._validate_args(owner, repo, branch, "/tmp/x.jsonl") is None


def _records(data_file: Path) -> list[dict]:
    """Read non-empty JSONL records from a fetch output fixture.

    Examples:
        >>> path = getfixture("tmp_path") / "records.jsonl"
        >>> _ = path.write_text('{"ok": true}\\n\\n{"ok": false}\\n')
        >>> _records(path)
        [{'ok': True}, {'ok': False}]
    """
    if not data_file.exists():
        return []
    return [json.loads(line) for line in data_file.read_text(encoding="utf-8").splitlines() if line]


# --- happy-path with mocked subprocess --------------------------------------


def _stub_gh_run(payload_map: dict[str, str]):
    """Build a ``subprocess.run`` stub that maps full api-path → stdout.

    Args:
        payload_map: Mapping from exact api path suffix to stdout payload.
            Match is suffix-based — first matching key wins; unmatched
            calls return rc=1 + empty (simulates 404).
    """
    # Order keys longest-first so more-specific paths (e.g.
    # ``/contents/.github/CODEOWNERS``) match before less-specific
    # prefixes (``/contents/.github``).
    ordered_keys = sorted(payload_map, key=len, reverse=True)

    def _run(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        """Return the configured GitHub response for one mocked API call."""
        # cmd = [gh_path, "api", api_path, "--jq", expr?]
        api_path = cmd[2] if len(cmd) >= 3 else ""
        for needle in ordered_keys:
            if api_path.endswith(needle):
                return _FakeCompleted(returncode=0, stdout=payload_map[needle])
        return _FakeCompleted(returncode=1, stdout="")

    return _run


def test_happy_path_writes_jsonl_records(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Mocked gh returning README + .github/ listing + CODEOWNERS → matching JSONL records appended."""
    payloads = {
        "/readme": _b64("# Hello\n"),
        # `.content` jq extracts the inline base64 — stub returns it directly.
        "/contents/CONTRIBUTING.md": _b64("Contributing guide.\n"),
        "/contents/.github": json.dumps(["CODEOWNERS", "workflows"]),
        "/contents/.github/CODEOWNERS": _b64("* @owner\n"),
        # branches/main/protection returns full JSON, no jq filter
        "/branches/main/protection": json.dumps({"required_status_checks": {"strict": True}}),
        # workflows listing
        "/contents/.github/workflows": json.dumps(["ci.yml"]),
        "/contents/.github/workflows/ci.yml": _b64("name: ci\non: push\njobs:\n  test:\n    runs-on: ubuntu-latest\n"),
        # dependabot config — no jq, raw JSON returned
        "/contents/.github/dependabot.yml": json.dumps({"name": "dependabot.yml", "type": "file"}),
    }
    monkeypatch.setattr(fgd.subprocess, "run", _stub_gh_run(payloads))
    monkeypatch.setattr(fgd, "which", lambda _: "/fake/gh")
    data_file = tmp_path / "out.jsonl"
    rc = fgd.main(["--owner", "owner", "--repo", "repo", "--default-branch", "main", "--data-file", str(data_file)])
    assert rc == 0
    assert data_file.exists()
    lines = data_file.read_text(encoding="utf-8").strip().splitlines()
    records = [json.loads(line) for line in lines]
    types = {rec["type"] for rec in records}
    assert "readme_content" in types
    assert "contributing_text" in types
    assert "github_dir" in types
    assert "codeowners_text" in types
    assert "branch_protection" in types
    assert "workflows_list" in types
    assert "workflow_files" in types
    assert "dependabot_config" in types
    # README content decoded correctly
    readme = next(rec for rec in records if rec["type"] == "readme_content")
    assert readme["data"] == "# Hello\n"
    # CODEOWNERS records source path
    co = next(rec for rec in records if rec["type"] == "codeowners_text")
    assert co["source"] == ".github/CODEOWNERS"
    assert co["data"] == "* @owner\n"
    # branch_protection echoes branch name
    bp = next(rec for rec in records if rec["type"] == "branch_protection")
    assert bp["branch"] == "main"
    # workflow_files concatenation includes per-workflow header
    wf = next(rec for rec in records if rec["type"] == "workflow_files")
    assert "--- workflow: ci.yml ---" in wf["data"]
    assert "name: ci" in wf["data"]


@pytest.mark.parametrize(
    ("payloads", "unexpected_types"),
    [
        pytest.param({"/contents/.github": "not-json"}, {"github_dir"}, id="invalid-github-dir-json"),
        pytest.param(
            {"/contents/.github/workflows": "not-json"},
            {"workflows_list", "workflow_files"},
            id="invalid-workflows-json",
        ),
        pytest.param({"/contents/.github/workflows": "[]"}, {"workflows_list", "workflow_files"}, id="empty-workflows"),
        pytest.param({"/readme": "!!!not-base64!!!"}, {"readme_content"}, id="invalid-readme-base64"),
    ],
)
def test_malformed_success_payloads_are_skipped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    payloads: dict[str, str],
    unexpected_types: set[str],
) -> None:
    monkeypatch.setattr(fgd.subprocess, "run", _stub_gh_run(payloads))
    monkeypatch.setattr(fgd, "which", lambda _: "/fake/gh")
    data_file = tmp_path / "out.jsonl"
    rc = fgd.main(
        ["--owner", "owner", "--repo", "repo", "--default-branch", "release/1.x", "--data-file", str(data_file)]
    )
    assert rc == 0
    assert {rec["type"] for rec in _records(data_file)}.isdisjoint(unexpected_types)


def test_branch_names_containing_slash_are_encoded_in_protection_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured_paths: list[str] = []

    def _run(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        """Capture API paths and return the branch-protection fixture when requested."""
        api_path = cmd[2] if len(cmd) >= 3 else ""
        captured_paths.append(api_path)
        if api_path.endswith("/branches/release/1.x/protection"):
            return _FakeCompleted(returncode=0, stdout=json.dumps({"required_status_checks": {"strict": True}}))
        return _FakeCompleted(returncode=1, stdout="")

    monkeypatch.setattr(fgd.subprocess, "run", _run)
    monkeypatch.setattr(fgd, "which", lambda _: "/fake/gh")
    data_file = tmp_path / "out.jsonl"
    rc = fgd.main(
        ["--owner", "owner", "--repo", "repo", "--default-branch", "release/1.x", "--data-file", str(data_file)]
    )
    assert rc == 0
    assert "repos/owner/repo/branches/release/1.x/protection" in captured_paths
    bp = next(rec for rec in _records(data_file) if rec["type"] == "branch_protection")
    assert bp["branch"] == "release/1.x"


def test_all_404s_writes_no_records(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """All gh calls return rc=1 (404) → no records written, exit 0."""
    monkeypatch.setattr(
        fgd.subprocess,
        "run",
        lambda *a, **k: _FakeCompleted(returncode=1, stdout=""),
    )
    monkeypatch.setattr(fgd, "which", lambda _: "/fake/gh")
    data_file = tmp_path / "out.jsonl"
    rc = fgd.main(["--owner", "owner", "--repo", "repo", "--default-branch", "main", "--data-file", str(data_file)])
    assert rc == 0
    # No records appended → file never opened for write; absent or empty both fine.
    if data_file.exists():
        assert data_file.read_text(encoding="utf-8") == ""


def test_codeowners_fallback_to_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Fall back to the root ownership file when the GitHub-specific path is absent."""

    def _run(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        """Return the configured CODEOWNERS fallback response."""
        api_path = cmd[2] if len(cmd) >= 3 else ""
        if api_path.endswith(".github/CODEOWNERS"):
            return _FakeCompleted(returncode=1, stdout="")
        if api_path.endswith("/contents/CODEOWNERS"):
            return _FakeCompleted(returncode=0, stdout=_b64("* @root-owner\n"))
        return _FakeCompleted(returncode=1, stdout="")

    monkeypatch.setattr(fgd.subprocess, "run", _run)
    monkeypatch.setattr(fgd, "which", lambda _: "/fake/gh")
    data_file = tmp_path / "out.jsonl"
    rc = fgd.main(["--owner", "owner", "--repo", "repo", "--default-branch", "main", "--data-file", str(data_file)])
    assert rc == 0
    records = [json.loads(line) for line in data_file.read_text(encoding="utf-8").splitlines() if line]
    co = [rec for rec in records if rec["type"] == "codeowners_text"]
    assert len(co) == 1
    assert co[0]["source"] == "CODEOWNERS"
    assert co[0]["data"] == "* @root-owner\n"


def test_gh_missing_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Return None → FileNotFoundError propagates."""
    monkeypatch.setattr(fgd, "which", lambda _: None)
    with pytest.raises(FileNotFoundError, match="gh"):
        fgd.main(
            [
                "--owner",
                "owner",
                "--repo",
                "repo",
                "--default-branch",
                "main",
                "--data-file",
                str(tmp_path / "out.jsonl"),
            ]
        )


# --- content needed by the checkpoint rubrics ---------------------------------


def _run_main(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payloads: dict[str, str]) -> dict[str, dict]:
    """Run ``main`` against stubbed gh payloads and return the appended records by type."""
    monkeypatch.setattr(fgd.subprocess, "run", _stub_gh_run(payloads))
    monkeypatch.setattr(fgd, "which", lambda _: "/fake/gh")
    data_file = tmp_path / "out.jsonl"
    rc = fgd.main(["--owner", "owner", "--repo", "repo", "--default-branch", "main", "--data-file", str(data_file)])
    assert rc == 0
    return {rec["type"]: rec for rec in _records(data_file)}


def test_every_workflow_file_is_fetched_and_counted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Read every YAML workflow file, skip non-workflow files, and record listed/fetched counts.

    The CI rubric greps workflow content for test, lint and security steps; reading only the first two files left most
    checkpoints unconfirmable on any repository with a split CI.
    """
    # Arrange
    payloads = {
        "/contents/.github/workflows": json.dumps(["a.yml", "b.yaml", "README.md"]),
        "/contents/.github/workflows/a.yml": _b64("run: pytest\n"),
        "/contents/.github/workflows/b.yaml": _b64("run: ruff check\n"),
    }

    # Act
    records = _run_main(monkeypatch, tmp_path, payloads)

    # Assert
    workflow_files = records["workflow_files"]
    assert (workflow_files["listed"], workflow_files["fetched"], workflow_files["partial"]) == (2, 2, False)
    assert workflow_files["failed"] == []
    assert "--- workflow: b.yaml ---\nrun: ruff check" in workflow_files["data"]


@pytest.mark.parametrize(
    ("cap", "payloads", "expected"),
    [
        pytest.param(
            50,
            {
                "/contents/.github/workflows": json.dumps(["a.yml", "b.yml"]),
                "/contents/.github/workflows/a.yml": _b64("x\n"),
            },
            (2, 1, True, ["b.yml"]),
            id="failed-file-marks-partial",
        ),
        pytest.param(
            1,
            {
                "/contents/.github/workflows": json.dumps(["a.yml", "b.yml"]),
                "/contents/.github/workflows/a.yml": _b64("x\n"),
                "/contents/.github/workflows/b.yml": _b64("y\n"),
            },
            (2, 1, True, []),
            id="cap-marks-partial",
        ),
    ],
)
def test_incomplete_workflow_content_is_flagged_partial(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    cap: int,
    payloads: dict[str, str],
    expected: tuple,
) -> None:
    """Flag the workflow content partial when a file fails to fetch or the file cap is reached."""
    # Arrange
    monkeypatch.setattr(fgd, "_WORKFLOW_FETCH_CAP", cap)

    # Act
    record = _run_main(monkeypatch, tmp_path, payloads)["workflow_files"]

    # Assert
    assert (record["listed"], record["fetched"], record["partial"], record["failed"]) == expected


@pytest.mark.parametrize(
    ("payloads", "record_type", "source"),
    [
        pytest.param(
            {
                "/contents": json.dumps(["README.md", ".github"]),
                "/contents/.github": json.dumps(["CONTRIBUTING.md"]),
                "/contents/.github/CONTRIBUTING.md": _b64("Setup\n"),
            },
            "contributing_text",
            ".github/CONTRIBUTING.md",
            id="contributing-in-github-dir",
        ),
        pytest.param(
            {
                "/contents": json.dumps(["README.md", "docs"]),
                "/contents/docs/SECURITY.md": _b64("Email security@example.org within 2 days\n"),
            },
            "security_text",
            "docs/SECURITY.md",
            id="security-in-docs",
        ),
        pytest.param(
            {
                "/contents": json.dumps(["Security.rst"]),
                "/contents/Security.rst": _b64("Report privately\n"),
            },
            "security_text",
            "Security.rst",
            id="root-name-from-listing",
        ),
        pytest.param(
            {
                "/contents": json.dumps(["README.md", "docs"]),
                "/contents/docs": json.dumps(["index.md", "CONTRIBUTING.rst"]),
                "/contents/docs/CONTRIBUTING.rst": _b64("Setup: make dev\n"),
            },
            "contributing_text",
            "docs/CONTRIBUTING.rst",
            id="docs-name-from-docs-listing",
        ),
    ],
)
def test_community_file_found_where_github_looks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    payloads: dict[str, str],
    record_type: str,
    source: str,
) -> None:
    """Fetch CONTRIBUTING and SECURITY from the root, ``.github/`` or ``docs/`` and record which one was used."""
    # Act
    records = _run_main(monkeypatch, tmp_path, payloads)

    # Assert
    assert records[record_type]["source"] == source


@pytest.mark.parametrize(
    ("root", "docs_dir"),
    [
        pytest.param(["README.md", "docs"], ["index.md"], id="root-lists-docs"),
        pytest.param(["README.md", "src"], None, id="root-without-docs-skips-the-listing"),
    ],
)
def test_docs_listing_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, root: list[str], docs_dir: list[str] | None
) -> None:
    """Record the ``docs/`` listing when the root lists ``docs/``, so a policy file there is provable either way."""
    # Arrange
    payloads = {"/contents": json.dumps(root), "/contents/docs": json.dumps(["index.md"])}

    # Act
    records = _run_main(monkeypatch, tmp_path, payloads)

    # Assert
    assert records.get("docs_dir", {}).get("data") == docs_dir


def test_changelog_outline_record(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep the changelog's first lines and heading lines, where release dates live, plus its size."""
    # Arrange
    text = "# Changelog\n\n## [Unreleased]\n\n- big entry\n\n## [1.2.0] — 2026-09-30\n\n- shipped\n"
    payloads = {"/contents": json.dumps(["CHANGELOG.md"]), "/contents/CHANGELOG.md": _b64(text)}

    # Act
    record = _run_main(monkeypatch, tmp_path, payloads)["changelog_headings"]

    # Assert
    assert record["source"] == "CHANGELOG.md"
    assert record["bytes"] == len(text.encode("utf-8"))
    assert record["data"]["headings"] == ["# Changelog", "## [Unreleased]", "## [1.2.0] — 2026-09-30"]
    assert record["truncated"] is False


def test_changelog_outline_flags_heading_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flag the outline truncated when a changelog has more headings than the cap keeps."""
    # Arrange
    monkeypatch.setattr(fgd, "_CHANGELOG_HEADING_CAP", 2)

    # Act
    outline, truncated = fgd.changelog_outline("## a\n## b\n## c\n")

    # Assert
    assert outline["headings"] == ["## a", "## b"]
    assert truncated is True


@pytest.mark.parametrize("name", ["dependabot.yml", "dependabot.yaml"])
def test_dependabot_config_either_extension(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str) -> None:
    """Record the Dependabot config under either extension GitHub accepts, with the path it came from."""
    # Arrange
    payloads = {f"/contents/.github/{name}": json.dumps({"name": name, "type": "file"})}

    # Act
    record = _run_main(monkeypatch, tmp_path, payloads)["dependabot_config"]

    # Assert
    assert record["source"] == f".github/{name}"


def test_ci_runs_record_is_scoped_to_the_default_branch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Fetch one page of completed default-branch runs with their event, under a timeout sized for the large page.

    Unscoped, the newest runs of a pull-request-heavy repository were mostly contributors' work in progress, so their
    failures decided the project's CI health; ``event`` lets the extractor keep only push, schedule, workflow_dispatch
    and merge_group runs, dropping the fork pull requests ``branch=`` still returns.
    """
    # Arrange
    calls: list[tuple[str, str, int]] = []
    runs = [{"conclusion": "success", "name": "CI", "event": "push", "head_branch": "main"}]

    def _run(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        """Record the runs call and answer it; every other call 404s."""
        if "/actions/runs" in cmd[2]:
            calls.append((cmd[2], cmd[4], kwargs["timeout"]))
            return _FakeCompleted(returncode=0, stdout=json.dumps(runs))
        return _FakeCompleted(returncode=1, stdout="")

    monkeypatch.setattr(fgd.subprocess, "run", _run)
    monkeypatch.setattr(fgd, "which", lambda _: "/fake/gh")
    data_file = tmp_path / "out.jsonl"

    # Act
    fgd.main(["--owner", "owner", "--repo", "repo", "--default-branch", "main", "--data-file", str(data_file)])

    # Assert
    record = next(rec for rec in _records(data_file) if rec["type"] == "ci_runs")
    assert record == {"type": "ci_runs", "branch": "main", "records": 1, "data": runs}
    assert calls == [
        (
            "repos/owner/repo/actions/runs?branch=main&status=completed&per_page=100",
            "[.workflow_runs[] | {conclusion, name, event, head_branch}]",
            30,
        )
    ]


@pytest.mark.parametrize(
    ("payloads", "record_type", "source"),
    [
        pytest.param(
            {"/contents": json.dumps(["README.md", "Docs"]), "/contents/Docs/SECURITY.md": _b64("Report privately\n")},
            "security_text",
            "Docs/SECURITY.md",
            id="capitalised-docs-dir-keeps-its-name",
        ),
        pytest.param(
            {"/contents": json.dumps(["README.md", "docs"]), "/contents/docs/CODEOWNERS": _b64("* @docs-owner\n")},
            "codeowners_text",
            "docs/CODEOWNERS",
            id="codeowners-in-docs",
        ),
    ],
)
def test_docs_dir_files_use_the_listed_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payloads: dict[str, str], record_type: str, source: str
) -> None:
    """Fetch ``docs/`` files under the directory's real name, CODEOWNERS included as GitHub's third location.

    A lower-case ``docs`` path 404s for a ``Docs/`` directory, which left the file and the ``docs_dir`` listing missing.
    """
    # Act
    records = _run_main(monkeypatch, tmp_path, payloads)

    # Assert
    assert records[record_type]["source"] == source


def test_docs_listing_uses_the_listed_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """List a ``Docs/`` directory under its own name, so its files are provable either way."""
    # Arrange
    payloads = {"/contents": json.dumps(["Docs"]), "/contents/Docs": json.dumps(["index.md"])}

    # Act
    records = _run_main(monkeypatch, tmp_path, payloads)

    # Assert
    assert records["docs_dir"]["data"] == ["index.md"]


def test_default_branch_status_record(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Record the default branch's protected flag, which the branches API shows without admin rights."""
    # Arrange
    payloads = {"/branches/main": json.dumps({"protected": False})}

    # Act
    record = _run_main(monkeypatch, tmp_path, payloads)["default_branch_status"]

    # Assert
    assert record == {"type": "default_branch_status", "branch": "main", "data": {"protected": False}}
