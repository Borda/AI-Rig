"""Tests for ``bin/check_oss_pr_signals.py``.

Subprocess and ``shutil.which`` are replaced throughout — no real ``gh`` or ``git`` invocation. Tests cover argv
validation, the four signal checks, the ``--diff-file`` snapshot mode, and JSON output routing (stdout vs ``--output-
file``).
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

import check_oss_pr_signals as cops  # type: ignore[import-not-found]
import pytest

# --------------------------------------------------------------------------- #
# Fake subprocess scaffolding
# --------------------------------------------------------------------------- #


@pytest.fixture(name="fake_subprocess")
def _fake_subprocess(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch ``which`` + ``subprocess.run`` with a scripted ``gh``/``git``.

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

    def _fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Record a command and return the longest configured response prefix."""
        state["calls"].append(list(cmd))
        basename = Path(cmd[0]).name
        non_flag = [t for t in cmd[1:] if not t.startswith("--") and not t.startswith(":")]
        for take in range(min(4, len(non_flag)), 0, -1):
            key = ":".join([basename, *non_flag[:take]])
            if key in state["responses"]:
                return subprocess.CompletedProcess(cmd, 0, stdout=state["responses"][key])
        return subprocess.CompletedProcess(cmd, 0, stdout="")

    monkeypatch.setattr(cops, "which", _fake_which)
    monkeypatch.setattr(cops.subprocess, "run", _fake_run)
    return state


def _collect(latest_tag: str, **extra: Any) -> Any:
    """Run ``collect_signals`` for PR 42 against the fake ``gh``/``git`` binaries."""
    return cops.collect_signals(
        clean_args="42", latest_tag=latest_tag, timeout=5, gh="/fake/gh", git="/fake/git", **extra
    )


def _gh_diff_calls(state: dict[str, Any]) -> list[list[str]]:
    """Return the recorded ``gh pr diff`` argv lists."""
    return [c for c in state["calls"] if c[:3] == ["/fake/gh", "pr", "diff"]]


class TestMainArgValidation:
    """Argv plumbing — required flags, numeric check, missing tools."""

    def test_missing_clean_args_exits_2(self) -> None:
        """Argparse exits 2 when ``--clean-args`` is missing.

        The PR number is the one mandatory input; running without it must fail loudly before any tool is looked up.
        """
        with pytest.raises(SystemExit) as exc:
            cops.main([])
        assert exc.value.code == 2

    def test_option_shaped_latest_tag_exits_1(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A ``--latest-tag`` starting with ``-`` is rejected before it can reach ``git show`` as an option.

        The ``=`` form slips past argparse's own dash detection, which is why the script checks it.
        """
        assert cops.main(["--clean-args", "42", "--latest-tag=--output=x"]) == 1
        assert "--latest-tag" in capsys.readouterr().err

    def test_output_file_symlink_exits_1(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A symlinked ``--output-file`` is refused instead of writing through to its target.

        The link points at a not-yet-existing target; after the refusal the target must still be absent.
        """
        target = tmp_path / "target.json"
        link = tmp_path / "link.json"
        link.symlink_to(target)
        assert cops.main(["--clean-args", "42", "--output-file", str(link)]) == 1
        assert not target.exists()
        assert "symlink" in capsys.readouterr().err

    def test_non_numeric_clean_args_exits_1(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A non-numeric PR identifier is rejected before any subprocess runs.

        The value arrives from user text and later feeds a ``gh`` argv, so shell-shaped input must stop at validation.
        """
        rc = cops.main(["--clean-args", "abc; rm -rf /"])
        assert rc == 1
        assert "numeric PR number" in capsys.readouterr().err

    def test_gh_missing_exits_2(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        """The missing-tool exit code 2 is returned when the GitHub CLI is unavailable.

        ``which`` finds ``git`` but not ``gh``; the error message must name the absent binary.
        """
        monkeypatch.setattr(cops, "which", lambda cmd: None if cmd == "gh" else "/fake/git")
        rc = cops.main(["--clean-args", "42"])
        assert rc == 2
        assert "gh" in capsys.readouterr().err


class TestGrepSecrets:
    """Edge cases for the secret scan beyond the docstring examples."""

    def test_short_value_skipped(self) -> None:
        """A 7-char value below the 8-char threshold is not flagged.

        Short strings are too noisy to be credentials, so the scan only reports values at or above the length floor.
        """
        assert cops._grep_secrets("+password = 'short12'") == []

    def test_dedupes_repeats(self) -> None:
        """The same finding on a repeated line appears once.

        Two identical added lines would otherwise double-report one credential in the JSON.
        """
        diff = "+token=abcdef12345\n+token=abcdef12345\n"
        assert cops._grep_secrets(diff) == ["<unknown>: token"]

    @pytest.mark.parametrize(
        ("line", "kind"),
        [
            pytest.param("+PASSWORD = 'longenoughval'", "password", id="uppercase-keyword-still-matches"),
            pytest.param("+aws_key = AKIAABCDEFGHIJKLMNOP", "aws-access-key", id="aws-access-key-id-no-key-prefix"),
            # Two literals joined at runtime so this fixture never trips the repo's ``detect-private-key`` hook.
            pytest.param("+-----BEGIN RSA" + " PRIVATE KEY-----", "private-key-header", id="pem-private-key-header"),
            pytest.param(
                "+Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
                "jwt",
                id="bare-jwt-without-keyword-prefix",
            ),
        ],
    )
    def test_secret_shaped_line_is_flagged(self, line: str, kind: str) -> None:
        """A credential-shaped added line is flagged by its kind, whatever its keyword casing or prefix.

        Covers an uppercase keyword, an AWS access key ID (no ``key=`` prefix needed), a PEM private-key header and a
        bare JWT. The JWT line carries no ``token=``-style keyword: with one, the older ``key[=:]value`` branch would
        already match and the case would pass without the JWT-shape alternation it is meant to pin.
        """
        assert cops._grep_secrets(line) == [f"<unknown>: {kind}"]

    def test_output_never_contains_the_credential(self) -> None:
        """Findings carry path, line number and kind only — the matched value must not leak into the JSON.

        A diff with a file header and hunk header lets the scan name the new-side line; the credential text itself
        flowing into reports and transcripts is the defect this guards against.
        """
        diff = (
            "diff --git a/src/a.py b/src/a.py\n@@ -1,2 +1,3 @@\n x = 1\n+token = 'abcdef123456'\n-secret: oldvalue99\n"
        )
        found = cops._grep_secrets(diff)
        assert found == ["src/a.py:2: token", "src/a.py (removed): secret"]
        assert not any("abcdef123456" in f or "oldvalue99" in f for f in found)


class TestExtractRemovedExports:
    """Boundary cases for removed-export extraction — diff headers, mixed +/- blocks."""

    @pytest.mark.parametrize(
        ("diff", "expected"),
        [
            pytest.param("+only_added", [], id="added-lines-skipped"),
            pytest.param(
                "--- a/src/x/__init__.py\n+++ b/src/x/__init__.py\n-Removed\n", ["Removed"], id="headers-skipped"
            ),
        ],
    )
    def test_extracts_only_removed_symbols(self, diff: str, expected: list[str]) -> None:
        """Only identifiers on removed lines are returned; ``+`` lines and ``---``/``+++`` headers are ignored.

        Counting a header path or an added line as a removed export would fabricate deprecation findings.
        """
        assert cops._extract_removed_exports(diff) == expected


class TestCollectSignals:
    """``collect_signals`` wired against the fake subprocess fixture."""

    def test_happy_path_aggregates_all_signals(self, fake_subprocess: dict[str, Any]) -> None:
        """All four checks contribute to the resulting signal record.

        A dependency bump, a secret-shaped line, a removed export that exists at the tag, and the tag itself must each
        land in their own field.
        """
        fake_subprocess["responses"] = {
            "gh:pr:diff:42:pyproject.toml": "+numpy>=2.0\n",
            "gh:pr:diff:42:*.py": "+password = 'longenough12'\n+x = 1\n",
            "gh:pr:diff:42": "-OldExport\n+NewExport\n",
            "git:show:v1.0.0": "OldExport = None\n",
            "git:describe": "",
        }
        signals = _collect("v1.0.0")
        assert "+numpy>=2.0" in signals.deps_diff
        assert any("password" in line for line in signals.secret_matches)
        assert "OldExport" in signals.removed_exports
        assert signals.latest_tag == "v1.0.0"
        assert any("DEPRECATION_NEEDED" in f for f in signals.deprecation_findings)

    def test_no_latest_tag_emits_no_release_tag_finding(self, fake_subprocess: dict[str, Any]) -> None:
        """An empty ``latest_tag`` with an empty ``git describe`` yields only the ``NO_RELEASE_TAG`` marker.

        Without release history the deprecation check cannot run, and the marker says so rather than reporting nothing.
        """
        signals = _collect("")
        assert signals.latest_tag == ""
        assert signals.deprecation_findings == [
            "NO_RELEASE_TAG: cannot determine release history — skip deprecation check"
        ]

    def test_unreleased_removal_when_symbol_absent_from_tag(self, fake_subprocess: dict[str, Any]) -> None:
        """A removed symbol that is not present at the latest tag is reported as ``UNRELEASED_REMOVAL``.

        Removing something that never shipped needs no deprecation cycle, so it must not be flagged as a break.
        """
        fake_subprocess["responses"] = {
            "gh:pr:diff:42": "-NewlyAddedThenRemoved\n",
            "git:show:v0.5.0": "Unrelated = 1\n",
        }
        signals = _collect("v0.5.0")
        assert any("UNRELEASED_REMOVAL: NewlyAddedThenRemoved" in f for f in signals.deprecation_findings)

    def test_git_show_runs_once_for_many_removed_exports(self, fake_subprocess: dict[str, Any]) -> None:
        """Many removed identifiers cost one ``git show`` of the tag, not one per identifier.

        The removed-identifier list is every token on removed ``__init__.py`` lines of an attacker-controlled PR; a
        subprocess per token made the run time proportional to PR content while the output never changes.
        """
        removed = "".join(f"-Export{i}\n" for i in range(50))
        fake_subprocess["responses"] = {"gh:pr:diff:42": removed, "git:show:v1.0.0": "Export3 = 1\n"}
        signals = _collect("v1.0.0")
        show_calls = [c for c in fake_subprocess["calls"] if c[1] == "show"]
        assert len(show_calls) == 1
        assert show_calls[0][2] == "--end-of-options"
        assert "DEPRECATION_NEEDED: Export3 " in "\n".join(signals.deprecation_findings)
        assert len(signals.deprecation_findings) == 50

    def test_export_count_is_capped(self, fake_subprocess: dict[str, Any]) -> None:
        """Past the cap only the first identifiers are checked and an ``EXPORT_CAP`` marker says so.

        Bounds the work an oversized PR can demand while still telling the reader the list was truncated.
        """
        names = [f"E{i:05d}" for i in range(cops._MAX_EXPORTS_CHECKED + 5)]
        fake_subprocess["responses"] = {"git:show:v1": ""}
        findings = cops._check_deprecations("/fake/git", "v1", names, timeout=5)
        assert len(findings) == cops._MAX_EXPORTS_CHECKED + 1
        assert findings[-1].startswith("EXPORT_CAP:")


@pytest.mark.usefixtures("fake_subprocess")
class TestOutputRouting:
    """Stdout versus ``--output-file`` paths."""

    def test_stdout_default(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Without ``--output-file`` the JSON goes to stdout and the exit code is 0.

        The default mode is what the skill pipes; the payload must parse and echo the supplied tag.
        """
        rc = cops.main(["--clean-args", "42", "--latest-tag", "v1"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert "deps_diff" in data
        assert data["latest_tag"] == "v1"

    def test_output_file_write(self, tmp_path: Path) -> None:
        """``--output-file`` writes the JSON to disk with every signal field present.

        The review skill reads the file back, so each documented key must exist even when its value is empty.
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


class TestTimeoutResilience:
    """Timeouts return empty strings rather than propagating."""

    @mock.patch(
        "check_oss_pr_signals.subprocess.run",
        autospec=True,
        side_effect=subprocess.TimeoutExpired(cmd=["fake"], timeout=1),
    )
    def test_timeout_yields_empty(self, mock_run: mock.MagicMock) -> None:
        """A timed-out subprocess yields empty output instead of an exception.

        A hung ``gh`` call must degrade to "no signal" so the rest of the review still runs.
        """
        assert cops._run(["/fake/gh", "pr", "diff", "1"], timeout=1) == ""


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
        """``diff_text`` populates all four slices while zero ``gh pr diff`` subprocesses run.

        A regression here means the snapshot silently fetches from the network again, reintroducing the redundant
        requests that the flag removes.
        """
        fake_subprocess["responses"] = {"git:show:v1.0.0": "OldExport = None\n"}
        signals = _collect("v1.0.0", diff_text=_SNAPSHOT_DIFF)
        assert _gh_diff_calls(fake_subprocess) == []
        assert "+numpy>=2.0" in signals.deps_diff
        assert any("password" in line for line in signals.secret_matches)
        assert "OldExport" in signals.removed_exports
        assert "## 1.1" in signals.changelog_diff

    def test_root_init_matches_glob(self) -> None:
        """A top-level source initializer stays in the API slice.

        Git's ``src/**/__init__.py`` glob matches the depth-zero path; the fnmatch translation drops it unless the extra
        root pattern is present.
        """
        sections = cops._split_diff_sections("diff --git a/src/__init__.py b/src/__init__.py\n-Gone\n")
        assert "Gone" in cops._filter_diff(sections, ["src/**/__init__.py", "src/__init__.py"])

    def test_non_src_init_excluded(self) -> None:
        """Non-source package initializers are excluded from the API-stability slice.

        The deprecation check is scoped to the public package surface under src/ — counting test-package inits would
        fabricate removed exports.
        """
        sections = cops._split_diff_sections("diff --git a/tests/__init__.py b/tests/__init__.py\n-x\n")
        assert cops._filter_diff(sections, ["src/**/__init__.py", "src/__init__.py"]) == ""


class TestDiffFileCli:
    """``--diff-file`` argv plumbing: happy path and unreadable or oversized snapshot fallbacks."""

    def test_diff_file_read_and_used(self, fake_subprocess: dict[str, Any], tmp_path: Path) -> None:
        """A readable ``--diff-file`` supplies the slices and no ``gh pr diff`` subprocess runs.

        End-to-end pin of the /oss:review data-collection invocation: ``--clean-args N --latest-tag T --diff-file SNAP
        --output-file OUT``.
        """
        snap = tmp_path / "pr.diff"
        snap.write_text(_SNAPSHOT_DIFF, encoding="utf-8")
        out_path = tmp_path / "signals.json"
        argv = ["--clean-args", "42", "--latest-tag", "v1", "--diff-file", str(snap), "--output-file", str(out_path)]
        rc = cops.main(argv)
        assert rc == 0
        assert _gh_diff_calls(fake_subprocess) == []
        assert "+numpy>=2.0" in json.loads(out_path.read_text())["deps_diff"]

    @pytest.mark.parametrize(
        ("prepare_snapshot", "stderr_note"),
        [
            pytest.param(lambda _snap: None, "falling back to gh", id="unreadable"),
            pytest.param(lambda snap: snap.write_text("x" * 100, encoding="utf-8"), "too large", id="oversized"),
        ],
    )
    def test_unusable_diff_file_falls_back_to_gh(
        self,
        fake_subprocess: dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        prepare_snapshot: Callable[[Path], object],
        stderr_note: str,
    ) -> None:
        """A missing or over-cap ``--diff-file`` degrades to the four ``gh pr diff`` calls with a stderr note.

        A broken snapshot must degrade to the pre-flag behavior, never to an empty result set that would read as "no
        signals" downstream. The size cap is lowered via monkeypatch instead of writing a real 10 MB file.
        """
        monkeypatch.setattr(cops, "_MAX_DIFF_FILE_SIZE", 10)
        snap = tmp_path / "pr.diff"
        prepare_snapshot(snap)
        rc = cops.main(["--clean-args", "42", "--diff-file", str(snap)])
        assert rc == 0
        assert stderr_note in capsys.readouterr().err
        assert len(_gh_diff_calls(fake_subprocess)) == 4
