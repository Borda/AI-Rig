"""Tests for ``bin/setup_release_dir.py``.

Pure filesystem tests — no subprocess mocking required. The script performs only pathlib/shutil operations; ``tmp_path``
provides isolation. Tests mirror the shell-script coverage and add cases for rerunning with a symlink overwrite and for
an absent file that was not backed up.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import setup_release_dir as srd


def test_missing_release_dir_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    """No args → exit 1 with 'release_dir required' on stderr."""
    rc = srd.main([])
    assert rc == 1
    assert "release_dir required" in capsys.readouterr().err


def test_missing_changelog_exits_1(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    """One arg only → exit 1 with 'changelog_file required' on stderr."""
    rc = srd.main([str(tmp_path / "rel")])
    assert rc == 1
    assert "changelog_file required" in capsys.readouterr().err


def test_creates_release_dir_and_symlink(tmp_path: Path) -> None:
    """Happy path: creates release dir and CHANGELOG.md symlink to canonical file."""
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("# Changelog\n")
    release_dir = tmp_path / "v1.0.0"

    rc = srd.main([str(release_dir), str(changelog)])

    assert rc == 0
    assert release_dir.is_dir()
    link = release_dir / "CHANGELOG.md"
    assert link.is_symlink()
    assert link.resolve() == changelog.resolve()


def test_creates_nested_dirs(tmp_path: Path) -> None:
    """RELEASE_DIR with non-existent parents → directories created recursively."""
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("")
    release_dir = tmp_path / "releases" / "v1.0.0"

    rc = srd.main([str(release_dir), str(changelog)])

    assert rc == 0
    assert release_dir.is_dir()


def test_project_docs_changelog_is_valid_source(tmp_path: Path) -> None:
    """The documented docs/CHANGELOG.md discovery path remains valid."""
    changelog = tmp_path / "docs" / "CHANGELOG.md"
    changelog.parent.mkdir()
    changelog.write_bytes(b"# Changes\n")
    release_dir = tmp_path / "releases" / "v1.0.0"

    assert srd.main(["--validate-only", str(release_dir), str(changelog)]) == 0
    assert srd.main([str(release_dir), str(changelog)]) == 0
    assert (release_dir / "CHANGELOG.md").resolve() == changelog
    assert changelog.read_bytes() == b"# Changes\n"


def test_backs_up_existing_highlights(tmp_path: Path) -> None:
    """Pre-existing HIGHLIGHTS.md → backed up to HIGHLIGHTS.md.bak."""
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("")
    release_dir = tmp_path / "rel"
    release_dir.mkdir()
    highlights = release_dir / "HIGHLIGHTS.md"
    highlights.write_text("# old highlights\n")

    rc = srd.main([str(release_dir), str(changelog)])

    assert rc == 0
    bak = release_dir / "HIGHLIGHTS.md.bak"
    assert bak.exists()
    assert bak.read_text() == "# old highlights\n"


def test_backs_up_all_five_artifacts(tmp_path: Path) -> None:
    """All five recognised artifacts get backed up when present."""
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("")
    release_dir = tmp_path / "rel"
    release_dir.mkdir()
    for name in srd._ARTIFACTS:
        (release_dir / name).write_text(f"# {name}\n")

    rc = srd.main([str(release_dir), str(changelog)])

    assert rc == 0
    for name in srd._ARTIFACTS:
        assert (release_dir / f"{name}.bak").exists(), f"missing backup for {name}"


def test_nonexistent_artifact_not_backed_up(tmp_path: Path) -> None:
    """Artifacts absent from release dir → no .bak files created."""
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("")
    release_dir = tmp_path / "rel"

    srd.main([str(release_dir), str(changelog)])

    for name in srd._ARTIFACTS:
        assert not (release_dir / f"{name}.bak").exists()


def test_rerun_overwrites_symlink(tmp_path: Path) -> None:
    """Re-running replaces existing CHANGELOG.md symlink safely."""
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("v1\n")
    release_dir = tmp_path / "rel"

    srd.main([str(release_dir), str(changelog)])

    changelog2 = tmp_path / "CHANGELOG2.md"
    changelog2.write_text("v2\n")
    rc = srd.main([str(release_dir), str(changelog2)])

    assert rc == 0
    link = release_dir / "CHANGELOG.md"
    assert link.resolve() == changelog2.resolve()


def test_golden_invocation_two_positionals(tmp_path: Path) -> None:
    """Documented call site ``setup_release_dir.py RELEASE_DIR CHANGELOG_FILE`` (2 positional) succeeds."""
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("# Changelog\n")
    release_dir = tmp_path / "v2.0.0"
    rc = srd.main([str(release_dir), str(changelog)])
    assert rc == 0
    assert (release_dir / "CHANGELOG.md").resolve() == changelog.resolve()


def test_relative_release_symlink_cannot_replace_external_changelog(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A relative release path through a symlink must preserve outside bytes."""
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    (repo / "releases").mkdir(parents=True)
    outside.mkdir()
    external = outside / "CHANGELOG.md"
    external.write_bytes(b"KEEP\n")
    source = repo / "CHANGELOG.md"
    source.write_bytes(b"SOURCE\n")
    (repo / "releases" / "v1.0.0").symlink_to(outside, target_is_directory=True)
    monkeypatch.chdir(repo)

    assert srd.main(["releases/v1.0.0", "CHANGELOG.md"]) == 1
    assert external.read_bytes() == b"KEEP\n"
    assert not external.is_symlink()


@pytest.mark.parametrize("validate_only", [True, False])
def test_changelog_alias_cannot_select_unrelated_project_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, validate_only: bool
) -> None:
    """Both prepare phases must reject a changelog alias before reading an unrelated file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    unrelated = repo / "pyproject.toml"
    unrelated.write_bytes(b"[project]\nname = 'keep'\n")
    (repo / "CHANGELOG.md").symlink_to(unrelated)
    monkeypatch.chdir(repo)
    release_dir = repo / "releases" / "v1.0.0"

    args = ["--validate-only"] if validate_only else []
    assert srd.main([*args, str(release_dir), "CHANGELOG.md"]) == 1
    assert unrelated.read_bytes() == b"[project]\nname = 'keep'\n"
    assert not release_dir.exists()


def test_changelog_argument_cannot_name_unrelated_file(tmp_path: Path) -> None:
    """The selected changelog path must have the name used by release discovery."""
    unrelated = tmp_path / "pyproject.toml"
    unrelated.write_bytes(b"[project]\nname = 'keep'\n")
    release_dir = tmp_path / "releases" / "v1.0.0"

    assert srd.main(["--validate-only", str(release_dir), str(unrelated)]) == 1
    assert unrelated.read_bytes() == b"[project]\nname = 'keep'\n"
    assert not release_dir.exists()


def test_changelog_argument_cannot_select_same_named_file_outside_project(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An allowed temporary root must not substitute for the release project's changelog."""
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    changelog = outside / "CHANGELOG.md"
    changelog.write_bytes(b"UNRELATED\n")
    monkeypatch.chdir(repo)
    release_dir = repo / "releases" / "v1.0.0"

    assert srd.main(["--validate-only", str(release_dir), str(changelog)]) == 1
    assert changelog.read_bytes() == b"UNRELATED\n"
    assert not release_dir.exists()


@pytest.mark.parametrize("validate_only", [True, False])
def test_prepare_preserves_regular_release_changelog(tmp_path: Path, validate_only: bool) -> None:
    """A hand-written release changelog must survive preflight and setup without replacement."""
    release_dir = tmp_path / "releases" / "v1.0.0"
    release_dir.mkdir(parents=True)
    release_changelog = release_dir / "CHANGELOG.md"
    release_changelog.write_bytes(b"HAND WRITTEN\n")
    draft = release_dir / "DRAFT.md"
    draft.write_bytes(b"DRAFT\n")
    source = tmp_path / "CHANGELOG.md"
    source.write_bytes(b"SOURCE\n")

    args = ["--validate-only"] if validate_only else []
    assert srd.main([*args, str(release_dir), str(source)]) == 1
    assert release_changelog.read_bytes() == b"HAND WRITTEN\n"
    assert not release_changelog.is_symlink()
    assert draft.read_bytes() == b"DRAFT\n"
    assert not (release_dir / "DRAFT.md.bak").exists()
    assert source.read_bytes() == b"SOURCE\n"


@pytest.mark.parametrize("outside_exists", [True, False])
def test_backup_destination_symlink_cannot_write_outside_release(tmp_path: Path, outside_exists: bool) -> None:
    """An existing or dangling backup symlink must not redirect a release backup."""
    release_dir = tmp_path / "release"
    release_dir.mkdir()
    source = release_dir / "waived-changes.md"
    source.write_bytes(b"NEW\n")
    prior = release_dir / "HIGHLIGHTS.md"
    prior.write_bytes(b"HIGHLIGHTS\n")
    outside = tmp_path / "outside.txt"
    if outside_exists:
        outside.write_bytes(b"KEEP\n")
    backup = release_dir / "waived-changes.md.bak"
    backup.symlink_to(outside)
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_bytes(b"CHANGELOG\n")

    assert srd.main([str(release_dir), str(changelog)]) == 1
    assert backup.is_symlink()
    if outside_exists:
        assert outside.read_bytes() == b"KEEP\n"
    else:
        assert not outside.exists()
    assert not (release_dir / "HIGHLIGHTS.md.bak").exists()
    assert not (release_dir / "CHANGELOG.md").exists()


@pytest.mark.parametrize("name", srd._ARTIFACTS)
def test_linked_artifact_source_refused_before_changelog_or_backup(tmp_path: Path, name: str) -> None:
    """A linked prior draft must not be read through or allow earlier writes."""
    release_dir = tmp_path / "release"
    release_dir.mkdir()
    prior = release_dir / "HIGHLIGHTS.md"
    if name != "HIGHLIGHTS.md":
        prior.write_bytes(b"PRIOR\n")
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"KEEP\n")
    (release_dir / name).symlink_to(outside)
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_bytes(b"CHANGELOG\n")

    assert srd.main([str(release_dir), str(changelog)]) == 1
    assert outside.read_bytes() == b"KEEP\n"
    assert changelog.read_bytes() == b"CHANGELOG\n"
    assert not (release_dir / "HIGHLIGHTS.md.bak").exists()
    assert not (release_dir / "CHANGELOG.md").exists()


def test_validate_only_checks_sources_without_writing(tmp_path: Path) -> None:
    """Prepare can validate artifact paths before the first changelog edit."""
    release_dir = tmp_path / "release"
    release_dir.mkdir()
    draft = release_dir / "DRAFT.md"
    draft.write_bytes(b"HAND EDITED\n")
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_bytes(b"PRIOR\n")

    assert srd.main(["--validate-only", str(release_dir), str(changelog)]) == 0
    assert draft.read_bytes() == b"HAND EDITED\n"
    assert changelog.read_bytes() == b"PRIOR\n"
    assert not (release_dir / "DRAFT.md.bak").exists()
    assert not (release_dir / "CHANGELOG.md").exists()


def test_prepare_validates_paths_before_changelog_edit() -> None:
    """The inline prepare audit must recheck paths before its own edit."""
    prepare = (Path(__file__).parents[1] / "skills/release/modes/prepare.md").read_text(encoding="utf-8")
    phase = prepare[prepare.index("**b. Audit changelog**") : prepare.index("### Phase 3:")]
    assert phase.index("--validate-only") < phase.index("Then apply **Audit changelog** logic inline")


def test_delegated_changelog_audit_validates_prepare_paths_before_read_or_write() -> None:
    """Agent A must validate its selected path before touching the changelog."""
    release = Path(__file__).parents[1] / "skills/release"
    skill = (release / "SKILL.md").read_text(encoding="utf-8")
    prompt = (release / "modes/changelog-audit-prompt.md").read_text(encoding="utf-8")
    dispatch = skill[skill.index("**Phases 5–6 parallel delegation**") : skill.index("Validate both envelopes:")]
    agent_a = prompt[prompt.index("Agent A — Audit changelog") : prompt.index("Agent B — Extract contributors")]

    assert 'IFS= read -r RELEASE_MODE < "${TMPDIR:-/tmp}/release-mode-${CSID}"' in dispatch
    assert 'IFS= read -r VERSION < "${TMPDIR:-/tmp}/release-prepare-version-${CSID}"' in dispatch
    assert "`$RELEASE_MODE`, `$VERSION`" in prompt
    assert agent_a.index("--validate-only") < agent_a.index("Read classified change table")
    assert "<RELEASE_MODE>" in agent_a and "<VERSION>" in agent_a
