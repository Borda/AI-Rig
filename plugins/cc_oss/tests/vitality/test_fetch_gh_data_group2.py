"""Tests for ``bin/fetch_gh_data_group2.py``.

Argument validation and the pure helpers run without any subprocess. The fetch tests patch ``subprocess.run`` and
``which`` with canned ``gh api`` payloads (keyed by API-path suffix) so no real ``gh`` invocation occurs, then read back
the JSONL records the script appends.
"""

from __future__ import annotations

import base64
import json
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from unittest import mock

import fetch_gh_data_group2 as fgd
import pytest
from _process import done


def _b64(text: str) -> str:
    """Encode UTF-8 text as base64 without a trailing newline.

    Examples:
        >>> _b64("hello")
        'aGVsbG8='
    """
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _records(data_file: Path) -> list[dict]:
    """Read non-empty JSONL records from a fetch output fixture; a missing file has no records.

    Examples:
        >>> path = getfixture("tmp_path") / "records.jsonl"
        >>> _ = path.write_text('{"ok": true}\\n\\n{"ok": false}\\n')
        >>> _records(path)
        [{'ok': True}, {'ok': False}]
    """
    if not data_file.exists():
        return []
    return [json.loads(line) for line in data_file.read_text(encoding="utf-8").splitlines() if line]


def _by_type(records: list[dict]) -> dict[str, dict]:
    """Index appended records by their ``type`` field."""
    return {rec["type"]: rec for rec in records}


def _stub_gh_run(payload_map: dict[str, str]) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Build a ``subprocess.run`` stub that maps an API-path suffix to stdout.

    Args:
        payload_map: Mapping from exact api path suffix to stdout payload. Match is suffix-based, longest key first so
            ``/contents/.github/CODEOWNERS`` wins over ``/contents/.github``; unmatched calls return rc=1 + empty
            (simulates 404).
    """
    ordered_keys = sorted(payload_map, key=len, reverse=True)

    def _run(cmd: list[str], *_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        """Return the configured GitHub response for one mocked API call (``cmd = [gh, "api", path, ...]``)."""
        api_path = cmd[2] if len(cmd) >= 3 else ""
        for needle in ordered_keys:
            if api_path.endswith(needle):
                return done(stdout=payload_map[needle])
        return done(returncode=1)

    return _run


@pytest.fixture
def gh_run() -> Iterator[mock.MagicMock]:
    """Patch ``which`` to find ``gh`` and ``subprocess.run`` to a mock answering 404 for every call."""
    with (
        mock.patch.object(fgd, "which", autospec=True, return_value="/fake/gh"),
        mock.patch.object(fgd.subprocess, "run", autospec=True, return_value=done(returncode=1)) as run,
    ):
        yield run


@pytest.fixture
def fetch(gh_run: mock.MagicMock, tmp_path: Path) -> Callable[..., list[dict]]:
    """Return a runner that serves canned payloads to ``main`` and yields the appended JSONL records.

    The runner asserts exit code 0: every scenario here is a non-fatal fetch.
    """

    def _fetch(payloads: dict[str, str], *, branch: str = "main") -> list[dict]:
        """Run ``main`` for ``owner/repo`` on ``branch`` against ``payloads`` and read back its records."""
        gh_run.side_effect = _stub_gh_run(payloads)
        data_file = tmp_path / "out.jsonl"
        argv = ["--owner", "owner", "--repo", "repo", "--default-branch", branch, "--data-file", str(data_file)]
        assert fgd.main(argv) == 0
        return _records(data_file)

    return _fetch


class TestInvalidArgs:
    """Main: bad command lines are refused before any ``gh`` call."""

    @pytest.mark.parametrize(
        ("owner", "repo"),
        [
            pytest.param(".", "repo", id="owner-current-directory"),
            pytest.param("..", "repo", id="owner-parent-directory"),
            pytest.param("owner", ".", id="repo-current-directory"),
            pytest.param("owner", "..", id="repo-parent-directory"),
        ],
    )
    def test_dot_identifiers_rejected_before_fetch(
        self, gh_run: mock.MagicMock, tmp_path: Path, owner: str, repo: str
    ) -> None:
        """Refuse dot segments in owner and repository names before they reach an API path."""
        assert (
            fgd.main(
                ["--owner", owner, "--repo", repo, "--default-branch", "main", "--data-file", str(tmp_path / "out")]
            )
            == 1
        )
        gh_run.assert_not_called()

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
    def test_exits_1_with_stderr_hint(self, argv: list[str], needle: str, capsys: pytest.CaptureFixture[str]) -> None:
        """A missing required arg, or a path-traversal pattern in an identifier arg, → exit 1 with the hint on stderr.

        Owner, repo and branch are spliced into ``gh api`` paths, so a ``..`` segment would read a different resource.
        """
        rc = fgd.main(argv)
        assert rc == 1
        assert needle in capsys.readouterr().err

    @mock.patch("fetch_gh_data_group2.which", autospec=True, return_value=None)
    def test_missing_gh_binary_raises(self, which: mock.MagicMock, tmp_path: Path) -> None:
        """No ``gh`` on PATH → ``FileNotFoundError`` naming it propagates instead of a silent empty run."""
        argv = [
            "--owner",
            "owner",
            "--repo",
            "repo",
            "--default-branch",
            "main",
            "--data-file",
            str(tmp_path / "o.jsonl"),
        ]
        with pytest.raises(FileNotFoundError, match="gh"):
            fgd.main(argv)


class TestHelpers:
    """Pure helpers: base64 decoding and argument validation."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param(_b64("hello world\nline 2"), "hello world\nline 2", id="utf8-round-trip"),
            pytest.param("", "", id="empty-input-short-circuits"),
            pytest.param("!!!definitely-not-base64$$$", "", id="non-base64-garbage"),
        ],
    )
    def test_decode_b64(self, raw: str, expected: str) -> None:
        """Valid base64 decodes to UTF-8 text; empty input and non-base64 garbage yield an empty string, no exception.

        GitHub returns file bodies base64-encoded; an undecodable body must degrade to "no content", not abort the run.
        """
        assert fgd._decode_b64(raw) == expected

    @pytest.mark.parametrize(
        ("owner", "repo", "branch"),
        [
            pytest.param("owner", "repo", "main", id="all-valid"),
            pytest.param("o", "r", "release/1.x", id="slash-in-branch-allowed"),
        ],
    )
    def test_validate_args_accepts_valid_args(self, tmp_path: Path, owner: str, repo: str, branch: str) -> None:
        """All valid args → ``None``; a branch may contain ``/`` (e.g. ``release/1.x``)."""
        assert fgd._validate_args(owner, repo, branch, str(tmp_path / "x.jsonl")) is None


class TestMainRecords:
    """Main: which JSONL records are appended for the canned ``gh api`` responses."""

    def test_writes_records_for_every_fetched_file(self, fetch: Callable[..., list[dict]]) -> None:
        """README, CONTRIBUTING, ``.github/`` listing, CODEOWNERS, protection, workflows and Dependabot → JSONL records.

        The happy path of the whole fetch: every file the rubrics read is decoded, labelled by type and keeps the
        source path or branch it came from.
        """
        payloads = {
            "/readme": _b64("# Hello\n"),
            "/contents/CONTRIBUTING.md": _b64("Contributing guide.\n"),
            "/contents/.github": json.dumps(["CODEOWNERS", "workflows"]),
            "/contents/.github/CODEOWNERS": _b64("* @owner\n"),
            "/branches/main/protection": json.dumps({"required_status_checks": {"strict": True}}),
            "/contents/.github/workflows": json.dumps(["ci.yml"]),
            "/contents/.github/workflows/ci.yml": _b64(
                "name: ci\non: push\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
            ),
            "/contents/.github/dependabot.yml": json.dumps({"name": "dependabot.yml", "type": "file"}),
        }
        records = fetch(payloads)
        by_type = _by_type(records)
        assert {rec["type"] for rec in records} >= {
            "readme_content",
            "contributing_text",
            "github_dir",
            "codeowners_text",
            "branch_protection",
            "workflows_list",
            "workflow_files",
            "dependabot_config",
        }
        assert by_type["readme_content"]["data"] == "# Hello\n"
        assert (by_type["codeowners_text"]["source"], by_type["codeowners_text"]["data"]) == (
            ".github/CODEOWNERS",
            "* @owner\n",
        )
        assert by_type["branch_protection"]["branch"] == "main"
        assert "--- workflow: ci.yml ---" in by_type["workflow_files"]["data"]
        assert "name: ci" in by_type["workflow_files"]["data"]

    @pytest.mark.parametrize(
        ("payloads", "unexpected_types"),
        [
            pytest.param({"/contents/.github": "not-json"}, {"github_dir"}, id="invalid-github-dir-json"),
            pytest.param(
                {"/contents/.github/workflows": "not-json"},
                {"workflows_list", "workflow_files"},
                id="invalid-workflows-json",
            ),
            pytest.param(
                {"/contents/.github/workflows": "[]"}, {"workflows_list", "workflow_files"}, id="empty-workflows"
            ),
            pytest.param({"/readme": "!!!not-base64!!!"}, {"readme_content"}, id="invalid-readme-base64"),
        ],
    )
    def test_skips_malformed_success_payloads(
        self, fetch: Callable[..., list[dict]], payloads: dict[str, str], unexpected_types: set[str]
    ) -> None:
        """A 200 response with unparsable JSON or base64 (or an empty workflow list) appends no record of that type.

        The run still exits 0: a garbled listing for one file must not block the records that did arrive.
        """
        records = fetch(payloads, branch="release/1.x")
        assert {rec["type"] for rec in records}.isdisjoint(unexpected_types)

    def test_all_404s_write_no_records(self, fetch: Callable[..., list[dict]]) -> None:
        """All gh calls return rc=1 (404) → no records written, exit 0.

        The output file is only opened once a record exists, so an absent file and an empty one both mean "nothing".
        """
        assert fetch({}) == []

    def test_slash_in_branch_is_kept_in_protection_path(
        self, fetch: Callable[..., list[dict]], gh_run: mock.MagicMock
    ) -> None:
        """A branch like ``release/1.x`` reaches the protection endpoint with its slash and is echoed in the record."""
        payloads = {"/branches/release/1.x/protection": json.dumps({"required_status_checks": {"strict": True}})}
        records = fetch(payloads, branch="release/1.x")
        api_paths = [call.args[0][2] for call in gh_run.call_args_list]
        assert "repos/owner/repo/branches/release/1.x/protection" in api_paths
        assert _by_type(records)["branch_protection"]["branch"] == "release/1.x"

    def test_codeowners_falls_back_to_root(self, fetch: Callable[..., list[dict]]) -> None:
        """Fall back to the root ownership file when the GitHub-specific path is absent.

        GitHub honours CODEOWNERS in ``.github/``, the root or ``docs/``; a repository using the root copy must still be
        credited with code owners.
        """
        records = fetch({"/contents/CODEOWNERS": _b64("* @root-owner\n")})
        owners = [rec for rec in records if rec["type"] == "codeowners_text"]
        assert [(rec["source"], rec["data"]) for rec in owners] == [("CODEOWNERS", "* @root-owner\n")]

    def test_every_workflow_file_is_fetched_and_counted(self, fetch: Callable[..., list[dict]]) -> None:
        """Read every YAML workflow file, skip non-workflow files, and record listed/fetched counts.

        The CI rubric greps workflow content for test, lint and security steps; reading only the first two files left
        most checkpoints unconfirmable on any repository with a split CI.
        """
        payloads = {
            "/contents/.github/workflows": json.dumps(["a.yml", "b.yaml", "README.md"]),
            "/contents/.github/workflows/a.yml": _b64("run: pytest\n"),
            "/contents/.github/workflows/b.yaml": _b64("run: ruff check\n"),
        }
        record = _by_type(fetch(payloads))["workflow_files"]
        assert (record["listed"], record["fetched"], record["partial"], record["failed"]) == (2, 2, False, [])
        assert "--- workflow: b.yaml ---\nrun: ruff check" in record["data"]

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
        self,
        monkeypatch: pytest.MonkeyPatch,
        fetch: Callable[..., list[dict]],
        cap: int,
        payloads: dict[str, str],
        expected: tuple,
    ) -> None:
        """Flag the workflow content partial when a file fails to fetch or the file cap is reached.

        A partial flag tells the rubric that a missing test or lint step is unproven rather than absent.
        """
        monkeypatch.setattr(fgd, "_WORKFLOW_FETCH_CAP", cap)
        record = _by_type(fetch(payloads))["workflow_files"]
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
                {"/contents": json.dumps(["Security.rst"]), "/contents/Security.rst": _b64("Report privately\n")},
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
            pytest.param(
                {
                    "/contents": json.dumps(["README.md", "Docs"]),
                    "/contents/Docs/SECURITY.md": _b64("Report privately\n"),
                },
                "security_text",
                "Docs/SECURITY.md",
                id="capitalised-docs-dir-keeps-its-name",
            ),
            pytest.param(
                {
                    "/contents": json.dumps(["README.md", "docs"]),
                    "/contents/docs/CODEOWNERS": _b64("* @docs-owner\n"),
                },
                "codeowners_text",
                "docs/CODEOWNERS",
                id="codeowners-in-docs",
            ),
        ],
    )
    def test_community_file_found_where_github_looks(
        self, fetch: Callable[..., list[dict]], payloads: dict[str, str], record_type: str, source: str
    ) -> None:
        """Fetch CONTRIBUTING, SECURITY and CODEOWNERS from the root, ``.github/`` or ``docs/`` and record the source.

        A ``docs`` directory is fetched under the name the root listing reports, CODEOWNERS included as GitHub's third
        location; a lower-case ``docs`` path 404s for a ``Docs/`` directory, which left the file and the ``docs_dir``
        listing missing.
        """
        assert _by_type(fetch(payloads))[record_type]["source"] == source

    @pytest.mark.parametrize(
        ("root", "docs_dir"),
        [
            pytest.param(["README.md", "docs"], ["index.md"], id="root-lists-docs"),
            pytest.param(["README.md", "src"], None, id="root-without-docs-skips-the-listing"),
            pytest.param(["Docs"], ["index.md"], id="capitalised-docs-listed-under-its-own-name"),
        ],
    )
    def test_docs_listing_record(
        self, fetch: Callable[..., list[dict]], root: list[str], docs_dir: list[str] | None
    ) -> None:
        """Record the ``docs/`` listing when the root lists it (under its real name), so a policy file is provable.

        With no docs directory in the root there is nothing to list and no ``docs_dir`` record is written.
        """
        listing = json.dumps(["index.md"])
        payloads = {"/contents": json.dumps(root), "/contents/docs": listing, "/contents/Docs": listing}
        assert _by_type(fetch(payloads)).get("docs_dir", {}).get("data") == docs_dir

    def test_changelog_outline_record(self, fetch: Callable[..., list[dict]]) -> None:
        """Keep the changelog's first lines and heading lines, where release dates live, plus its size."""
        text = "# Changelog\n\n## [Unreleased]\n\n- big entry\n\n## [1.2.0] — 2026-09-30\n\n- shipped\n"
        payloads = {"/contents": json.dumps(["CHANGELOG.md"]), "/contents/CHANGELOG.md": _b64(text)}
        record = _by_type(fetch(payloads))["changelog_headings"]
        assert (record["source"], record["bytes"], record["truncated"]) == ("CHANGELOG.md", len(text.encode()), False)
        assert record["data"]["headings"] == ["# Changelog", "## [Unreleased]", "## [1.2.0] — 2026-09-30"]

    @pytest.mark.parametrize("name", ["dependabot.yml", "dependabot.yaml"])
    def test_dependabot_config_under_either_extension(self, fetch: Callable[..., list[dict]], name: str) -> None:
        """Record the Dependabot config under either extension GitHub accepts, with the path it came from."""
        payloads = {f"/contents/.github/{name}": json.dumps({"name": name, "type": "file"})}
        assert _by_type(fetch(payloads))["dependabot_config"]["source"] == f".github/{name}"

    def test_ci_runs_scoped_to_default_branch(self, fetch: Callable[..., list[dict]], gh_run: mock.MagicMock) -> None:
        """Fetch one page of completed default-branch runs with their event, under a timeout sized for the large page.

        Unscoped, the newest runs of a pull-request-heavy repository were mostly contributors' work in progress, so
        their failures decided the project's CI health; ``event`` lets the extractor keep only push, schedule,
        workflow_dispatch and merge_group runs, dropping the fork pull requests ``branch=`` still returns.
        """
        runs = [{"conclusion": "success", "name": "CI", "event": "push", "head_branch": "main"}]
        records = fetch({"/actions/runs?branch=main&status=completed&per_page=100": json.dumps(runs)})
        run_calls = [
            (call.args[0][2], call.args[0][4], call.kwargs["timeout"])
            for call in gh_run.call_args_list
            if "/actions/runs" in call.args[0][2]
        ]
        assert _by_type(records)["ci_runs"] == {"type": "ci_runs", "branch": "main", "records": 1, "data": runs}
        assert run_calls == [
            (
                "repos/owner/repo/actions/runs?branch=main&status=completed&per_page=100",
                "[.workflow_runs[] | {conclusion, name, event, head_branch}]",
                30,
            )
        ]

    def test_default_branch_status_record(self, fetch: Callable[..., list[dict]]) -> None:
        """Record the default branch's protected flag, which the branches API shows without admin rights."""
        record = _by_type(fetch({"/branches/main": json.dumps({"protected": False})}))["default_branch_status"]
        assert record == {"type": "default_branch_status", "branch": "main", "data": {"protected": False}}


class TestChangelogOutline:
    """changelog_outline: heading extraction with a cap."""

    def test_flags_heading_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Flag the outline truncated when a changelog has more headings than the cap keeps."""
        monkeypatch.setattr(fgd, "_CHANGELOG_HEADING_CAP", 2)
        outline, truncated = fgd.changelog_outline("## a\n## b\n## c\n")
        assert (outline["headings"], truncated) == (["## a", "## b"], True)


@pytest.mark.parametrize("path", ["../README.md", "docs/../../README.md", "docs/./README.md"])
def test_contents_file_dot_segments_never_requested(gh_run: mock.MagicMock, tmp_path: Path, path: str) -> None:
    """Reject directory navigation in a remote file path before calling GitHub."""
    ctx = fgd.FetchContext("gh", "owner/repo", tmp_path / "out", 1)
    assert fgd._fetch_file_text(ctx, path) is None
    gh_run.assert_not_called()


@pytest.mark.parametrize("path", ["..", "docs/..", "docs/."])
def test_contents_listing_dot_segments_never_requested(gh_run: mock.MagicMock, tmp_path: Path, path: str) -> None:
    """Reject directory navigation in a listing path before calling GitHub."""
    ctx = fgd.FetchContext("gh", "owner/repo", tmp_path / "out", 1)
    assert fgd._list_names(ctx, path) is None
    gh_run.assert_not_called()
