"""Tests for ``bin/release_setup.py``.

All git subprocess calls are monkeypatched via a sequential fake that returns pre-configured ``(returncode, stdout)``
pairs in call order. No real ``git`` invocations occur. The stable-branch path (3 git calls) and fallback path (8 calls)
are both covered.

Output is written below ``${TMPDIR}/release-setup-<CSID>``; tests redirect ``TMPDIR`` with monkeypatch. The
``conftest.py`` autouse fixture strips ``CLAUDE_CODE_SESSION_ID`` and ``CSID``, so ``<CSID>`` resolves to the literal
``"shared"`` fallback here.
"""

from __future__ import annotations

from typing import Any

import pytest

import release_setup as rs


class _FakeCompleted:
    """Minimal stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        """Store the status and output returned by a fake Git command."""
        self.returncode = returncode
        self.stdout = stdout


def _patch_git_sequence(monkeypatch: pytest.MonkeyPatch, *outcomes: tuple[int, str]) -> list[list[str]]:
    """Register a sequential subprocess.run fake; return recorded command lists."""
    call_n = [0]
    recorded: list[list[str]] = []

    def _fake_run(cmd: list[str], **_kwargs: Any) -> _FakeCompleted:
        """Return the configured Git command response while recording argv."""
        recorded.append(list(cmd))
        idx = call_n[0]
        call_n[0] += 1
        rc, out = outcomes[idx] if idx < len(outcomes) else (0, "")
        return _FakeCompleted(returncode=rc, stdout=out)

    monkeypatch.setattr(rs.subprocess, "run", _fake_run)
    monkeypatch.setattr(rs, "which", lambda _: "/fake/git")
    return recorded


def test_stable_branch_all_keys_emitted(monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory) -> None:
    """Stable branch (BRANCH_TAG found) → all 7 key files written under TMPDIR/release-setup-shared/, exit 0."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    _patch_git_sequence(
        monkeypatch,
        (0, "/repo"),  # rev-parse --show-toplevel
        (0, "main"),  # branch --show-current
        (0, "v1.0.0"),  # describe --first-parent → branch tag
    )
    rc = rs.main([])
    assert rc == 0
    out_dir = tmp_path / "release-setup-shared"
    for key in ("SKILL_DIR", "REPO_ROOT", "BRANCH", "DATE", "LAST_TAG", "CHERRY_PICK_SUBJECTS", "SOURCE_TAG_REF"):
        assert (out_dir / key).exists(), f"expected output file missing: {key}"


def test_stable_branch_last_tag_value(monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory) -> None:
    """Stable branch → LAST_TAG file contains the first-parent describe result."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    _patch_git_sequence(
        monkeypatch,
        (0, "/repo"),
        (0, "main"),
        (0, "v2.3.1"),
    )
    rs.main([])
    out_dir = tmp_path / "release-setup-shared"
    assert (out_dir / "LAST_TAG").read_text() == "v2.3.1\n"
    assert (out_dir / "SOURCE_TAG_REF").read_text() == "\n"


def test_branch_slash_replaced_with_hyphen(monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory) -> None:
    """Branch name containing '/' → slashes replaced with '-' in BRANCH file."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    _patch_git_sequence(
        monkeypatch,
        (0, "/repo"),
        (0, "feature/my-thing"),
        (0, "v1.0.0"),
    )
    rs.main([])
    assert (tmp_path / "release-setup-shared" / "BRANCH").read_text() == "feature-my-thing\n"


def test_fallback_path_emits_source_and_cherry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pytest.TempPathFactory,
) -> None:
    """Fallback path (no first-parent tag) → LAST_TAG from common ancestor, SOURCE_TAG_REF set."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    _patch_git_sequence(
        monkeypatch,
        (0, "/repo"),  # rev-parse
        (0, "main"),  # branch
        (1, ""),  # describe --first-parent → no branch tag
        (0, "v0.9.0"),  # describe → source tag
        (0, "abc123"),  # rev-list -n1 refs/tags/v0.9.0
        (0, "def456"),  # merge-base
        (0, "v0.8.0"),  # describe def456 → last tag
        (0, "cp1\ncp2"),  # git log v0.8.0..v0.9.0
    )
    rc = rs.main([])
    assert rc == 0
    out_dir = tmp_path / "release-setup-shared"
    assert (out_dir / "LAST_TAG").read_text() == "v0.8.0\n"
    assert (out_dir / "SOURCE_TAG_REF").read_text() == "v0.9.0\n"


def test_fallback_path_stderr_banner(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Fallback path → 'Stable-branch mode' banner on stderr."""
    _patch_git_sequence(
        monkeypatch,
        (0, "/repo"),
        (0, "main"),
        (1, ""),
        (0, "v0.9.0"),
        (0, "abc123"),
        (0, "def456"),
        (0, "v0.8.0"),
        (0, ""),
    )
    rs.main([])
    captured = capsys.readouterr()
    assert "Stable-branch mode" in captured.err


def test_no_tags_uses_initial_commit(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """No stable tags found → 'No stable tags found' on stderr, still exits 0."""
    _patch_git_sequence(
        monkeypatch,
        (0, "/repo"),  # rev-parse
        (0, "main"),  # branch
        (1, ""),  # describe --first-parent → empty
        (1, ""),  # describe → no source tag
        (0, "abc0123"),  # rev-list --max-parents=0 → first commit
        (1, ""),  # rev-list -n1 refs/tags/abc0123 → fail
        (0, "def456"),  # merge-base
        (1, ""),  # describe def456 → no tag
        (0, ""),  # git log
    )
    rc = rs.main([])
    assert rc == 0
    captured = capsys.readouterr()
    assert "No stable tags found" in captured.err


def test_all_output_files_written(monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory) -> None:
    """All 7 expected key files written to TMPDIR/release-setup-shared/ with non-empty content for mandatory keys."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    _patch_git_sequence(
        monkeypatch,
        (0, "/my/repo"),
        (0, "main"),
        (0, "v1.0.0"),
    )
    rs.main([])
    out_dir = tmp_path / "release-setup-shared"
    for key in ("SKILL_DIR", "REPO_ROOT", "BRANCH", "DATE", "LAST_TAG", "CHERRY_PICK_SUBJECTS", "SOURCE_TAG_REF"):
        assert (out_dir / key).exists(), f"output file missing: {key}"
    assert (out_dir / "REPO_ROOT").read_text() != ""
    assert (out_dir / "LAST_TAG").read_text() == "v1.0.0\n"


def test_git_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Return None → FileNotFoundError propagates."""
    monkeypatch.setattr(rs, "which", lambda _: None)
    with pytest.raises(FileNotFoundError, match="git"):
        rs.main([])


def test_help_exits_0_no_git(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Print usage and exit 0 before resolving git."""
    monkeypatch.setattr(rs, "which", lambda _cmd: (_ for _ in ()).throw(AssertionError("which must not run on --help")))
    with pytest.raises(SystemExit) as exc:
        rs.main(["--help"])
    assert exc.value.code == 0
    assert "usage: release_setup.py" in capsys.readouterr().out


def test_output_values_end_with_newline(monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory) -> None:
    """Every written key file ends with a trailing newline.

    Consumers read these files with ``IFS= read -r VAR < file || VAR=""``; a file with no trailing newline makes
    ``read`` return non-zero on EOF, which fires the ``||`` fallback and silently clobbers the just-read value back to
    empty.
    """
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    _patch_git_sequence(
        monkeypatch,
        (0, "/repo"),
        (0, "main"),
        (0, "v1.0.0"),
    )
    rs.main([])
    out_dir = tmp_path / "release-setup-shared"
    for key in ("SKILL_DIR", "REPO_ROOT", "BRANCH", "DATE", "LAST_TAG", "CHERRY_PICK_SUBJECTS", "SOURCE_TAG_REF"):
        assert (out_dir / key).read_text().endswith("\n"), f"{key} missing trailing newline"


def test_resolve_skill_dir_matches_versioned_plugin_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pytest.TempPathFactory,
) -> None:
    """Installed cache layout inserts a version dir between plugin id and ``skills``.

    Real layout is ``~/.claude/plugins/cache/<marketplace>/oss/<version>/skills/release``
    — one segment deeper than the un-versioned layout the old 3-segment check assumed,
    so it never matched and always fell through to the source-tree fallback.
    """
    skill_dir = tmp_path / ".claude" / "plugins" / "cache" / "borda-ai-rig" / "oss" / "0.38.4" / "skills" / "release"
    skill_dir.mkdir(parents=True)
    monkeypatch.setattr(rs.Path, "home", classmethod(lambda _cls: tmp_path))

    resolved = rs._resolve_skill_dir()

    assert resolved == str(skill_dir)


def test_resolve_skill_dir_falls_back_when_not_installed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pytest.TempPathFactory,
) -> None:
    """No installed ``oss/<version>/skills/release`` anywhere → source-tree fallback."""
    monkeypatch.setattr(rs.Path, "home", classmethod(lambda _cls: tmp_path))

    resolved = rs._resolve_skill_dir()

    assert resolved == "plugins/cc_oss/skills/release"
