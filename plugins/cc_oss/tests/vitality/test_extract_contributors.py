"""Tests for ``bin/extract_contributors.py``.

``subprocess.run`` and the module-level ``which`` are mocked — no real ``git`` invocations. ``is_bot``,
``dedupe_by_email``, and ``_build_range`` are tested directly as pure functions; argument validation and the ``git log``
path are covered through ``main(argv)`` calls.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import extract_contributors as ec
import pytest


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    """Build the result of one ``git log`` call."""
    return subprocess.CompletedProcess(args=["git"], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def git() -> Iterator[mock.Mock]:
    """Resolve ``git`` to a fake path and mock ``subprocess.run`` to succeed with no output; tests reset the result."""
    with (
        mock.patch("extract_contributors.which", autospec=True, return_value="/fake/git"),
        mock.patch("extract_contributors.subprocess.run", autospec=True, return_value=_completed()) as run,
    ):
        yield run


class TestIsBot:
    """Telling bot identities from humans in a ``Name <email>`` line."""

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            pytest.param(
                "dependabot[bot] <49699333+dependabot[bot]@users.noreply.github.com>",
                True,
                id="bot-login-privacy-domain",
            ),
            pytest.param(
                "pre-commit-ci[bot] <66853113+pre-commit-ci[bot]@users.noreply.github.com>",
                True,
                id="bot-login-privacy-domain-ci",
            ),
            pytest.param("CI <ci@noreply.example.com>", True, id="noreply-email-generic"),
            pytest.param("Jane Doe <jane@example.com>", False, id="human-plain"),
            pytest.param("Jirka Borovec <6035284+Borda@users.noreply.github.com>", False, id="human-privacy-email"),
            pytest.param("Alice <123+alice@users.noreply.github.com>", False, id="human-web-ui-commit"),
        ],
    )
    def test_flags_bot_logins_and_generic_noreply(self, line: str, expected: bool) -> None:
        """Flag ``[bot]`` logins and generic noreply; keeps GitHub privacy-email humans.

        GitHub's own ``users.noreply.github.com`` privacy addresses belong to humans unless the login ends in ``[bot]``.
        """
        assert ec.is_bot(line) is expected


class TestDedupeByEmail:
    """Collapsing a raw ``git log`` identity list into one entry per email."""

    def test_oversized_author_finishes_with_whole_line_fallback(self) -> None:
        """Keep a malformed long identity without quadratic regular-expression matching."""
        program = (
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "from extract_contributors import dedupe_by_email; "
            "text = sys.stdin.read(); result = dedupe_by_email([text]); "
            "print(len(result)); print(result == [text])"
        )
        result = subprocess.run(
            [sys.executable, "-c", program, str(Path(ec.__file__).parent)],
            input="Author" + " " * 256_000 + "missing-email",
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        assert result.returncode == 0
        assert result.stdout == "1\nTrue\n"

    def test_keeps_first_name_drops_bots_sorted(self) -> None:
        """Dedup by email, drop bots, keep first display name, sort case-folded.

        Blank lines are ignored and ``J. Doe`` loses to the earlier ``Jane Doe`` for the same address.
        """
        result = ec.dedupe_by_email(
            [
                "Jane Doe <jane@example.com>",
                "",
                "J. Doe <jane@example.com>",
                "bot[bot] <bot@noreply.github.com>",
                "Al Pace <al@example.com>",
            ]
        )
        assert result == ["Al Pace <al@example.com>", "Jane Doe <jane@example.com>"]

    def test_can_retain_bots_for_release_credits(self) -> None:
        """Release mode retains bot identities so prose can aggregate their credits.

        With ``include_bots`` the dependabot identity stays in the sorted list, beside humans with privacy emails.
        """
        result = ec.dedupe_by_email(
            [
                "Jane Doe <jane@example.com>",
                "dependabot[bot] <49699333+dependabot[bot]@users.noreply.github.com>",
                "Jirka Borovec <6035284+Borda@users.noreply.github.com>",
            ],
            include_bots=True,
        )
        assert result == [
            "dependabot[bot] <49699333+dependabot[bot]@users.noreply.github.com>",
            "Jane Doe <jane@example.com>",
            "Jirka Borovec <6035284+Borda@users.noreply.github.com>",
        ]


class TestBuildRange:
    """Resolving ``--range`` / ``--from`` / ``--to`` into a git range."""

    @pytest.mark.parametrize(
        ("range_arg", "from_ref", "to_ref", "expected"),
        [
            pytest.param("v1..v2", "", "", "v1..v2", id="explicit-range"),
            pytest.param("", "v1", "v2", "v1..v2", id="from-to"),
            pytest.param("", "v1", "", "v1..HEAD", id="from-defaults-head"),
            pytest.param("", "", "", "", id="none"),
        ],
    )
    def test_resolves_range_forms(self, range_arg: str, from_ref: str, to_ref: str, expected: str) -> None:
        """Resolve ``--range`` / ``--from`` / ``--to`` into a git range.

        An explicit range wins as written, ``--from`` alone runs to ``HEAD``, and no input gives an empty range.
        """
        assert ec._build_range(range_arg, from_ref, to_ref) == expected


class TestArgumentValidation:
    """``main`` rejects unusable argument combinations before running git."""

    @pytest.mark.parametrize(
        ("argv", "message"),
        [
            pytest.param([], "--range or --from required", id="no-range-given"),
            pytest.param(["--range", "v1..v2", "--from", "v1"], "not both", id="range-and-from-conflict"),
            pytest.param(["--bogus", "x"], "unknown arg", id="unrecognized-flag"),
        ],
    )
    def test_invalid_args_exit_1_with_stderr_message(
        self, git: mock.Mock, capsys: pytest.CaptureFixture[str], argv: list[str], message: str
    ) -> None:
        """No range, both ``--range`` and ``--from``, or an unrecognized flag → exit 1 with the matching message.

        Each is a usage mistake the caller can fix, so the message is on stderr and git is never run.
        """
        assert ec.main(argv) == 1
        assert message in capsys.readouterr().err
        git.assert_not_called()


class TestGitLog:
    """The ``git log`` path, with the subprocess mocked."""

    @pytest.mark.parametrize(
        ("extra_args", "stdout", "expected"),
        [
            pytest.param(
                [],
                "Jane Doe <jane@example.com>\nJ. Doe <jane@example.com>\nbot[bot] <b@noreply.github.com>\n"
                "Al <al@example.com>\n",
                "Al <al@example.com>\nJane Doe <jane@example.com>\n",
                id="sorted-bot-free-email-deduped",
            ),
            pytest.param(
                ["--include-bots"],
                "dependabot[bot] <49699333+dependabot[bot]@users.noreply.github.com>\n"
                "Jirka Borovec <6035284+Borda@users.noreply.github.com>\n",
                "dependabot[bot] <49699333+dependabot[bot]@users.noreply.github.com>\n"
                "Jirka Borovec <6035284+Borda@users.noreply.github.com>\n",
                id="include-bots-keeps-bot-and-privacy-email-human",
            ),
        ],
    )
    def test_emits_contributor_list(
        self,
        git: mock.Mock,
        capsys: pytest.CaptureFixture[str],
        extra_args: list[str],
        stdout: str,
        expected: str,
    ) -> None:
        """A successful git log → sorted, email-deduped stdout list, bot-free unless ``--include-bots`` is given.

        Release extraction with ``--include-bots`` keeps bot credits while retaining GitHub privacy-email humans.
        """
        git.return_value = _completed(stdout=stdout)
        assert ec.main(["--range", "v1..v2", *extra_args]) == 0
        assert capsys.readouterr().out == expected

    def test_failure_exits_2(self, git: mock.Mock, capsys: pytest.CaptureFixture[str]) -> None:
        """Git log non-zero exit → exit 2 with stderr surfaced.

        A bad revision is reported with git's own message instead of an empty contributor list.
        """
        git.return_value = _completed(returncode=128, stderr="bad revision")
        assert ec.main(["--range", "v1..v2"]) == 2
        assert "bad revision" in capsys.readouterr().err

    def test_repo_flag_inserts_git_c(self, git: mock.Mock) -> None:
        """``--repo`` inserts ``-C <root>`` into the git command.

        Release extraction runs against a checkout that is not the current directory.
        """
        ec.main(["--repo", "/repo/x", "--range", "v1..v2"])
        assert git.call_args.args[0][1:3] == ["-C", "/repo/x"]

    def test_exclude_file_drops_only_shipped_commits(self, git: mock.Mock, tmp_path: Path) -> None:
        """Commits the exclusion TSV marks ``shipped`` are not logged; ``review`` rows stay credited.

        The release range still holds work an older off-branch tag shipped, and its authors were credited then.
        """
        tsv = tmp_path / "shipped.tsv"
        tsv.write_text("aaa\tshipped\t1.11.1\tcherry\nbbb\treview\t1.11.1\tsame PR\n", encoding="utf-8")
        git.side_effect = [_completed(stdout="aaa\nbbb\nccc\n"), _completed(stdout="Al <al@example.com>\n")]

        assert ec.main(["--range", "v1..v2", "--exclude-file", str(tsv)]) == 0

        listing, log = git.call_args_list
        assert listing.args[0][1:] == ["rev-list", "v1..v2", "--no-merges"]
        assert "--stdin" in log.args[0]
        assert log.kwargs["input"] == "bbb\nccc\n"

    def test_exclude_file_covering_every_commit_lists_nobody(
        self, git: mock.Mock, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """When every commit already shipped, no ``git log`` runs and nothing is printed.

        An empty ``--stdin`` would make git fall back to HEAD and credit an unrelated author.
        """
        tsv = tmp_path / "shipped.tsv"
        tsv.write_text("aaa\tshipped\t1.11.1\tcherry\n", encoding="utf-8")
        git.return_value = _completed(stdout="aaa\n")

        assert ec.main(["--range", "v1..v2", "--exclude-file", str(tsv)]) == 0

        assert git.call_count == 1
        assert capsys.readouterr().out == ""

    def test_unreadable_exclude_file_exits_2(self, git: mock.Mock, tmp_path: Path) -> None:
        """A missing exclusion file stops the run instead of crediting the whole range."""
        assert ec.main(["--range", "v1..v2", "--exclude-file", str(tmp_path / "absent.tsv")]) == 2

    def test_timeout_exits_2(self, git: mock.Mock, capsys: pytest.CaptureFixture[str]) -> None:
        """A ``git log`` slower than the bound exits 2 with a message, the same code as a failed log.

        The bound sits under the release call site's Bash timeout, so a slow range is reported by the script itself
        rather than killed by the harness with no explanation.
        """

        def slow(*_a: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            """Time out the way ``subprocess.run`` does when ``timeout`` elapses."""
            raise subprocess.TimeoutExpired(cmd="git", timeout=kwargs["timeout"])

        git.side_effect = slow
        assert ec.main(["--range", "v1..v2"]) == 2
        assert f"timed out after {ec._GIT_LOG_TIMEOUT} s" in capsys.readouterr().err

    @mock.patch("extract_contributors.which", autospec=True, return_value=None)
    def test_missing_git_raises(self, mock_which: mock.Mock) -> None:
        """No ``git`` executable on PATH raises ``FileNotFoundError`` naming git.

        Failing loudly beats exiting 0 with an empty list that would read as "no contributors".
        """
        with pytest.raises(FileNotFoundError, match="git"):
            ec.main(["--range", "v1..v2"])
        mock_which.assert_called_once_with("git")
