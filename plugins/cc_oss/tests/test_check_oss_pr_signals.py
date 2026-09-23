"""Tests for ``bin/check_oss_pr_signals.py``.

Subprocess and ``shutil.which`` are monkeypatched throughout — no real ``gh`` or ``git`` invocation. Tests cover argv
validation, the four signal checks, and JSON output routing (stdout vs ``--output-file``).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

import check_oss_pr_signals as cops  # type: ignore[import-not-found]


# --------------------------------------------------------------------------- #
# Fake subprocess scaffolding
# --------------------------------------------------------------------------- #


class _FakeCompleted:
    """Minimal stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        """Store the subprocess status and output consumed by the checker."""
        self.returncode = returncode
        self.stdout = stdout


@pytest.fixture(name="fake_subprocess")
def _fake_subprocess(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch ``which`` + ``subprocess.run``.

    Returns a dict with:
        - ``responses``: ``dict[str, str]`` mapping command-key prefix → stdout.
        - ``calls``: list of recorded argv lists.

    Command-key prefix is the first 3 tokens after the binary basename
    (e.g. ``"gh:pr:diff:--"`` for ``gh pr diff <N> -- pyproject.toml ...``).
    Unknown keys return empty stdout.
    """
    state: dict[str, Any] = {"responses": {}, "calls": []}

    def _fake_which(cmd: str) -> str:
        """Return a stable fake executable path for any requested command."""
        return f"/fake/{cmd}"

    def _fake_run(cmd: list[str], **_kwargs: Any) -> _FakeCompleted:
        """Record a command and return the longest configured response prefix."""
        state["calls"].append(list(cmd))
        # Look up a response by progressively shorter prefix
        basename = Path(cmd[0]).name
        # Build candidate keys from longest to shortest using non-flag tokens
        non_flag = [t for t in cmd[1:] if not t.startswith("--") and not t.startswith(":")]
        for take in range(min(4, len(non_flag)), 0, -1):
            key = ":".join([basename] + non_flag[:take])
            if key in state["responses"]:
                return _FakeCompleted(returncode=0, stdout=state["responses"][key])
        # Default empty
        return _FakeCompleted(returncode=0, stdout="")

    monkeypatch.setattr(cops, "which", _fake_which)
    monkeypatch.setattr(cops.subprocess, "run", _fake_run)
    return state


# --------------------------------------------------------------------------- #
# argv / validation
# --------------------------------------------------------------------------- #


class TestMainArgValidation:
    """Argv plumbing — required flags, numeric check, missing tools."""

    def test_missing_clean_args_exits_2(self) -> None:
        """Argparse exits 2 when ``--clean-args`` is missing."""
        with pytest.raises(SystemExit) as exc:
            cops.main([])
        assert exc.value.code == 2

    def test_non_numeric_clean_args_exits_1(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Non-numeric PR identifier rejected before any subprocess."""
        rc = cops.main(["--clean-args", "abc; rm -rf /"])
        assert rc == 1
        assert "numeric PR number" in capsys.readouterr().err

    def test_gh_missing_exits_2(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Return the missing-tool exit code when GitHub CLI is unavailable."""
        monkeypatch.setattr(cops, "which", lambda cmd: None if cmd == "gh" else "/fake/git")
        rc = cops.main(["--clean-args", "42"])
        assert rc == 2
        assert "gh" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Pure helpers — already doctested; add edge cases
# --------------------------------------------------------------------------- #


class TestGrepSecrets:
    """Edge cases beyond the docstring examples."""

    def test_short_value_skipped(self) -> None:
        """7-char value below the 8-char threshold → not flagged."""
        assert cops._grep_secrets("+password = 'short12'") == []

    def test_dedupes_repeats(self) -> None:
        """Same line repeated → appears once."""
        diff = "+token=abcdef12345\n+token=abcdef12345\n"
        assert cops._grep_secrets(diff) == ["+token=abcdef12345"]

    def test_case_insensitive_match(self) -> None:
        """Uppercase keyword still matches."""
        assert cops._grep_secrets("+PASSWORD = 'longenoughval'") == ["+PASSWORD = 'longenoughval'"]

    def test_aws_access_key_detected(self) -> None:
        """An AWS-style access key ID is flagged with no ``key=`` prefix needed."""
        assert cops._grep_secrets("+aws_key = AKIAABCDEFGHIJKLMNOP") == ["+aws_key = AKIAABCDEFGHIJKLMNOP"]

    def test_pem_private_key_header_detected(self) -> None:
        """A PEM private-key header line is flagged.

        Built from two literals joined at runtime (not a contiguous string in the source) so this test fixture itself
        never trips the repo's ``detect-private-key`` pre-commit hook.
        """
        header = "-----BEGIN RSA" + " PRIVATE KEY-----"
        line = f"+{header}"
        assert cops._grep_secrets(line) == [line]

    def test_bare_jwt_detected_without_keyword_prefix(self) -> None:
        """A bare JWT (no ``token=``/``secret=``/etc. keyword prefix) is still flagged by its own shape.

        A value line built with a keyword like ``token=`` would already match the pre-existing ``key[=:]value`` branch
        (the JWT's dots and underscores are inside that branch's value charset) and would pass even without the new JWT-
        shape alternation — this line has no such keyword, so only the new alternation can catch it.
        """
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        line = f"+Authorization: Bearer {jwt}"
        assert cops._grep_secrets(line) == [line]


class TestExtractRemovedExports:
    """Boundary cases — diff headers, mixed +/- blocks."""

    def test_ignores_added_lines(self) -> None:
        """Lines starting with ``+`` are skipped."""
        assert cops._extract_removed_exports("+only_added") == []

    def test_ignores_diff_headers(self) -> None:
        """Exclude diff headers from changed-symbol extraction."""
        diff = "--- a/src/x/__init__.py\n+++ b/src/x/__init__.py\n-Removed\n"
        assert cops._extract_removed_exports(diff) == ["Removed"]


# --------------------------------------------------------------------------- #
# End-to-end: collect_signals via fake subprocess
# --------------------------------------------------------------------------- #


class TestCollectSignals:
    """Wire up collect_signals against the fake subprocess fixture."""

    def test_happy_path_aggregates_all_signals(self, fake_subprocess: dict[str, Any]) -> None:
        """All four checks contribute to the resulting dataclass."""
        fake_subprocess["responses"] = {
            "gh:pr:diff:42:pyproject.toml": "+numpy>=2.0\n",
            "gh:pr:diff:42:*.py": "+password = 'longenough12'\n+x = 1\n",
            "gh:pr:diff:42": "-OldExport\n+NewExport\n",
            "git:show:v1.0.0": "OldExport = None\n",
            "git:describe": "",
        }
        signals = cops.collect_signals(
            clean_args="42",
            latest_tag="v1.0.0",
            timeout=5,
            gh="/fake/gh",
            git="/fake/git",
        )
        assert "+numpy>=2.0" in signals.deps_diff
        assert any("password" in line for line in signals.secret_matches)
        assert "OldExport" in signals.removed_exports
        assert signals.latest_tag == "v1.0.0"
        assert any("DEPRECATION_NEEDED" in f for f in signals.deprecation_findings)

    def test_no_latest_tag_emits_no_release_tag_finding(self, fake_subprocess: dict[str, Any]) -> None:
        """Empty ``latest_tag`` + git describe empty → NO_RELEASE_TAG marker."""
        signals = cops.collect_signals(
            clean_args="42",
            latest_tag="",
            timeout=5,
            gh="/fake/gh",
            git="/fake/git",
        )
        assert signals.latest_tag == ""
        assert signals.deprecation_findings == [
            "NO_RELEASE_TAG: cannot determine release history — skip deprecation check"
        ]

    def test_unreleased_removal_when_symbol_absent_from_tag(
        self,
        fake_subprocess: dict[str, Any],
    ) -> None:
        """Removed symbol not present at the latest tag → UNRELEASED_REMOVAL."""
        fake_subprocess["responses"] = {
            "gh:pr:diff:42": "-NewlyAddedThenRemoved\n",
            # git show returns text NOT containing the symbol
            "git:show:v0.5.0": "Unrelated = 1\n",
        }
        signals = cops.collect_signals(
            clean_args="42",
            latest_tag="v0.5.0",
            timeout=5,
            gh="/fake/gh",
            git="/fake/git",
        )
        assert any("UNRELEASED_REMOVAL: NewlyAddedThenRemoved" in f for f in signals.deprecation_findings)


# --------------------------------------------------------------------------- #
# Output routing
# --------------------------------------------------------------------------- #


class TestOutputRouting:
    """Stdout versus ``--output-file`` paths."""

    def test_stdout_default(
        self,
        fake_subprocess: dict[str, Any],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """No ``--output-file`` → JSON to stdout, exit 0."""
        rc = cops.main(["--clean-args", "42", "--latest-tag", "v1"])
        assert rc == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "deps_diff" in data
        assert data["latest_tag"] == "v1"

    def test_output_file_write(
        self,
        fake_subprocess: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        """Verify command-line option behavior.

        ``--output-file`` path → JSON written to disk.
        """
        out_path = tmp_path / "signals.json"
        rc = cops.main(["--clean-args", "42", "--output-file", str(out_path)])
        assert rc == 0
        data = json.loads(out_path.read_text())
        assert set(data.keys()) >= {
            "deps_diff",
            "secret_matches",
            "removed_exports",
            "deprecation_findings",
            "latest_tag",
            "changelog_diff",
        }


# --------------------------------------------------------------------------- #
# Subprocess timeout handling
# --------------------------------------------------------------------------- #


class TestTimeoutResilience:
    """Timeouts return empty strings rather than propagating."""

    def test_timeout_yields_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Return empty output after a subprocess timeout."""

        def _raise_timeout(*_args: Any, **_kwargs: Any) -> _FakeCompleted:
            """Raise the timeout exercised by the fail-open subprocess path."""
            raise subprocess.TimeoutExpired(cmd=["fake"], timeout=1)

        monkeypatch.setattr(cops.subprocess, "run", _raise_timeout)
        assert cops._run(["/fake/gh", "pr", "diff", "1"], timeout=1) == ""


# --------------------------------------------------------------------------- #
# --diff-file snapshot mode
# --------------------------------------------------------------------------- #

_SNAPSHOT_DIFF = (
    "diff --git a/pyproject.toml b/pyproject.toml\n"
    "+numpy>=2.0\n"
    "diff --git a/src/pkg/__init__.py b/src/pkg/__init__.py\n"
    "-OldExport\n"
    "+NewExport\n"
    "diff --git a/src/pkg/core.py b/src/pkg/core.py\n"
    "+password = 'longenough12'\n"
    "diff --git a/CHANGELOG.md b/CHANGELOG.md\n"
    "+## 1.1\n"
)


class TestDiffFileFiltering:
    """Local slice filtering replaces the per-pathspec gh diff subprocesses.

    The snapshot mode exists so /oss:review can fetch the PR diff once and feed every consumer the same bytes — these
    tests pin that no gh diff subprocess fires and each slice lands in its correct signal field.
    """

    def test_slices_derived_without_gh_diff_calls(self, fake_subprocess: dict[str, Any]) -> None:
        """diff_text populates all four slices; zero gh pr diff subprocesses run.

        A regression here means the snapshot silently fetches from the network again, reintroducing the redundant
        requests that the flag removes.
        """
        fake_subprocess["responses"] = {"git:show:v1.0.0": "OldExport = None\n"}
        signals = cops.collect_signals(
            clean_args="42",
            latest_tag="v1.0.0",
            timeout=5,
            gh="/fake/gh",
            git="/fake/git",
            diff_text=_SNAPSHOT_DIFF,
        )
        gh_diff_calls = [c for c in fake_subprocess["calls"] if c[:3] == ["/fake/gh", "pr", "diff"]]
        assert gh_diff_calls == []
        assert "+numpy>=2.0" in signals.deps_diff
        assert any("password" in line for line in signals.secret_matches)
        assert "OldExport" in signals.removed_exports
        assert "## 1.1" in signals.changelog_diff

    def test_root_init_matches_glob(self) -> None:
        """Keep a top-level source initializer in the API slice.

        Git's `src/**/__init__.py` glob matches the depth-zero path; the fnmatch translation drops it unless the extra
        root pattern is present.
        """
        sections = cops._split_diff_sections("diff --git a/src/__init__.py b/src/__init__.py\n-Gone\n")
        assert "Gone" in cops._filter_diff(sections, ["src/**/__init__.py", "src/__init__.py"])

    def test_non_src_init_excluded(self) -> None:
        """Exclude non-source package initializers from the API-stability slice.

        The deprecation check is scoped to the public package surface under src/ — counting test-package inits would
        fabricate removed exports.
        """
        sections = cops._split_diff_sections("diff --git a/tests/__init__.py b/tests/__init__.py\n-x\n")
        assert cops._filter_diff(sections, ["src/**/__init__.py", "src/__init__.py"]) == ""


class TestDiffFileCli:
    """Verify command-line option behavior.

    ``--diff-file`` argv plumbing: happy path and unreadable-path fallback.
    """

    def test_diff_file_read_and_used(
        self,
        fake_subprocess: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        """Readable ``--diff-file`` → slices from the file, no gh diff subprocess.

        End-to-end pin of the /oss:review data-collection invocation: `--clean-args N --latest-tag T --diff-file SNAP
        --output-file OUT`.
        """
        snap = tmp_path / "pr.diff"
        snap.write_text(_SNAPSHOT_DIFF, encoding="utf-8")
        out_path = tmp_path / "signals.json"
        rc = cops.main(
            ["--clean-args", "42", "--latest-tag", "v1", "--diff-file", str(snap), "--output-file", str(out_path)]
        )
        assert rc == 0
        gh_diff_calls = [c for c in fake_subprocess["calls"] if c[:3] == ["/fake/gh", "pr", "diff"]]
        assert gh_diff_calls == []
        data = json.loads(out_path.read_text())
        assert "+numpy>=2.0" in data["deps_diff"]

    def test_unreadable_diff_file_falls_back_to_gh(
        self,
        fake_subprocess: dict[str, Any],
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Missing ``--diff-file`` path → stderr note + gh subprocess path runs.

        A broken snapshot must degrade to the pre-flag behavior, never to an empty result set that would read as "no
        signals" downstream.
        """
        rc = cops.main(["--clean-args", "42", "--diff-file", str(tmp_path / "absent.diff")])
        assert rc == 0
        assert "falling back to gh" in capsys.readouterr().err
        gh_diff_calls = [c for c in fake_subprocess["calls"] if c[:3] == ["/fake/gh", "pr", "diff"]]
        assert len(gh_diff_calls) == 4

    def test_oversized_diff_file_falls_back_to_gh(
        self,
        fake_subprocess: dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A ``--diff-file`` above the size cap is never read; falls back to gh like an unreadable one.

        Guards against an unbounded read on a runaway snapshot file — the cap is lowered via monkeypatch rather than
        writing a real 10 MB fixture.
        """
        monkeypatch.setattr(cops, "_MAX_DIFF_FILE_SIZE", 10)
        snap = tmp_path / "pr.diff"
        snap.write_text("x" * 100, encoding="utf-8")
        rc = cops.main(["--clean-args", "42", "--diff-file", str(snap)])
        assert rc == 0
        assert "too large" in capsys.readouterr().err
        gh_diff_calls = [c for c in fake_subprocess["calls"] if c[:3] == ["/fake/gh", "pr", "diff"]]
        assert len(gh_diff_calls) == 4
