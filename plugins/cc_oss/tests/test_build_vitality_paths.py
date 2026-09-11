"""Tests for ``bin/build_vitality_paths.py``.

Path construction, manifest reading and the agents-list assembly are pure; the git and bridge lookups monkeypatch
``subprocess.run`` so no real subprocess runs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import build_vitality_paths as bvp


class _FakeCompleted:
    """Minimal stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        """Store the status and output consumed by the resolver."""
        self.returncode = returncode
        self.stdout = stdout


@pytest.fixture
def tmp_sentinels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point sentinel writes at an isolated temp directory."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    return tmp_path


def _fake_subprocess(monkeypatch: pytest.MonkeyPatch, git_out: str, bridge_out: str) -> None:
    """Answer the git commit lookup and the bridge status probe with canned output."""

    def fake_run(cmd: list[str], **_kwargs: object) -> _FakeCompleted:
        if cmd[0] == "git":
            return _FakeCompleted(stdout=git_out)
        return _FakeCompleted(stdout=bridge_out)

    monkeypatch.setattr(bvp.subprocess, "run", fake_run)


def test_build_report_path() -> None:
    """The vitality report path embeds owner, repo and the UTC timestamp."""
    got = bvp.build_report_path("owner", "repo", "2026-09-11T10-00-00Z")
    assert got == ".reports/analyse/vitality/output-analyse-vitality-owner-repo-2026-09-11T10-00-00Z.md"


def test_read_version(tmp_path: Path) -> None:
    """A well-formed manifest yields its version string."""
    manifest = tmp_path / "plugin.json"
    manifest.write_text(json.dumps({"version": "1.2.3"}), encoding="utf-8")
    assert bvp.read_version(manifest) == "1.2.3"


@pytest.mark.parametrize("payload", ["not json", "{}", "[]"])
def test_read_version_falls_back_to_unknown(tmp_path: Path, payload: str) -> None:
    """Missing, malformed or non-object manifests report ``unknown`` rather than failing."""
    manifest = tmp_path / "plugin.json"
    manifest.write_text(payload, encoding="utf-8")
    assert bvp.read_version(manifest) == "unknown"


def test_read_version_missing_file(tmp_path: Path) -> None:
    """An absent manifest reports ``unknown``."""
    assert bvp.read_version(tmp_path / "nope.json") == "unknown"


def test_resolve_version_file_prefers_newest_cache_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The lexically last cache entry wins, matching the ``ls … | sort | tail -1`` pipeline it replaces."""
    for version in ("0.9.0", "0.10.0"):
        target = tmp_path / ".claude/plugins/cache/borda-ai-rig/oss" / version / ".claude-plugin"
        target.mkdir(parents=True)
        (target / "plugin.json").write_text(json.dumps({"version": version}), encoding="utf-8")
    monkeypatch.setattr(bvp.Path, "home", staticmethod(lambda: tmp_path))
    # Lexical, not version-aware: "0.9.0" sorts after "0.10.0".
    assert bvp.resolve_version_file().parts[-3] == "0.9.0"


def test_resolve_version_file_falls_back_to_source_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With no installed cache, the source-tree manifest is used."""
    monkeypatch.setattr(bvp.Path, "home", staticmethod(lambda: tmp_path))
    assert bvp.resolve_version_file() == Path("plugins/cc_oss/.claude-plugin/plugin.json")


@pytest.mark.parametrize(
    ("quick", "codex", "expected_lines"),
    [
        pytest.param(True, True, 1, id="quick-drops-review-agents"),
        pytest.param(False, False, 2, id="full-without-bridge"),
        pytest.param(False, True, 3, id="full-with-bridge"),
    ],
)
def test_agents_yaml(quick: bool, codex: bool, expected_lines: int) -> None:
    """The agents list reflects which passes actually ran."""
    assert len(bvp.agents_yaml(quick, codex).splitlines()) == expected_lines


def test_agents_yaml_names_bridge_only_when_available() -> None:
    """``bridge:review`` is listed only when the Codex bridge answered ``available``."""
    assert "bridge:review" in bvp.agents_yaml(False, True)
    assert "bridge:review" not in bvp.agents_yaml(False, False)


def test_main_writes_report_sentinel_and_emits_metadata(
    tmp_sentinels: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """The report path is persisted before the report exists, and provenance is printed for the header."""
    _fake_subprocess(monkeypatch, git_out="abc1234", bridge_out="available")
    assert bvp.main(["--owner", "owner", "--repo", "repo", "--quick", "false"]) == 0
    sentinel = (tmp_sentinels / "analyse-report-file-shared").read_text(encoding="utf-8")
    assert sentinel.startswith(".reports/analyse/vitality/output-analyse-vitality-owner-repo-")
    assert sentinel.endswith(".md\n")
    out = capsys.readouterr().out
    assert "REPORT_COMMIT=abc1234" in out
    assert "CODEX_AVAILABLE=1" in out
    assert "bridge:review" in out


def test_main_marks_codex_absent(
    tmp_sentinels: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A bridge that is not available is reported as ``CODEX_AVAILABLE=0``."""
    _fake_subprocess(monkeypatch, git_out="abc1234", bridge_out="absent")
    assert bvp.main(["--owner", "owner", "--repo", "repo", "--quick", "true"]) == 0
    out = capsys.readouterr().out
    assert "CODEX_AVAILABLE=0" in out
    assert "--quick: core scoring only" in out


def test_main_reports_unknown_commit_when_git_fails(
    tmp_sentinels: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A failing git call degrades to ``unknown`` instead of aborting the report."""
    monkeypatch.setattr(bvp.subprocess, "run", lambda *_a, **_k: _FakeCompleted(returncode=1))
    assert bvp.main(["--owner", "owner", "--repo", "repo"]) == 0
    assert "REPORT_COMMIT=unknown" in capsys.readouterr().out


class TestDryRun:
    """Covers --dry-run: compute and print, write no session state.

    These sentinels are live skill state keyed by ``CSID``, read by later steps and by PreToolUse hooks. An inspection
    run of this script must not forge them — one such run wrote an ``analyse-report-file`` naming a report that was
    never produced, and the hook denial that followed blocked an unrelated question.
    """

    def test_writes_no_sentinels(self, tmp_sentinels: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """--dry-run leaves the sentinel directory empty and exits 0."""
        assert bvp.main(["--owner", "owner", "--repo", "repo", "--quick", "false", "--dry-run"]) == 0
        assert list(tmp_sentinels.glob("*")) == []

    def test_announces_what_it_would_write(self, tmp_sentinels: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Each suppressed write is still reported, so a verifier sees the computed value."""
        assert bvp.main(["--owner", "owner", "--repo", "repo", "--quick", "false", "--dry-run"]) == 0
        assert "[dry-run] would write " in capsys.readouterr().out

    def test_default_still_writes(self, tmp_sentinels: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Without the flag the real skill path is unchanged."""
        assert bvp.main(["--owner", "owner", "--repo", "repo", "--quick", "false"]) == 0
        assert list(tmp_sentinels.glob("*")) != []
