"""Tests for ``bin/setup_release_dir.py``.

Pure filesystem tests: the script performs only pathlib/shutil operations, so ``tmp_path`` provides all isolation and
nothing is mocked. They cover the release directory and changelog link, artifact backups, rerunning over an existing
link, and the refusals that keep a planted symlink or an unrelated file from being read or overwritten. Two closing
tests pin that the shipped prepare and changelog-audit instructions call ``--validate-only`` first.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import setup_release_dir as srd

_RELEASE = Path(__file__).resolve().parents[2] / "skills/release"
_PYPROJECT = b"[project]\nname = 'keep'\n"
_PHASES = [pytest.param([], id="setup"), pytest.param(["--validate-only"], id="validate-only")]


@pytest.fixture
def changelog(tmp_path: Path) -> Path:
    """Return a canonical ``CHANGELOG.md`` below ``tmp_path``."""
    path = tmp_path / "CHANGELOG.md"
    path.write_text("# Changelog\n", encoding="utf-8")
    return path


def _prose(relative: str) -> str:
    """Read one shipped release-skill file with whitespace runs collapsed so phrase checks ignore wrapping."""
    return " ".join((_RELEASE / relative).read_text(encoding="utf-8").split())


def _write_files(root: Path, files: dict[str, bytes]) -> None:
    """Write each ``files`` entry (name to bytes) below ``root``."""
    for name, data in files.items():
        (root / name).write_bytes(data)


class TestArguments:
    """Required positional arguments."""

    @pytest.mark.parametrize(
        ("args", "message"),
        [
            pytest.param([], "release_dir required", id="no-arguments"),
            pytest.param(["rel"], "changelog_file required", id="release-dir-only"),
        ],
    )
    def test_missing_argument_exits_1(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        args: list[str],
        message: str,
    ) -> None:
        """A missing positional argument exits 1 and names it on stderr.

        Run from ``tmp_path`` so a relative release dir could never land in the repository if validation slipped.
        """
        monkeypatch.chdir(tmp_path)

        rc = srd.main(args)

        assert rc == 1
        assert message in capsys.readouterr().err


class TestSetup:
    """The happy path: release directory, changelog link, and artifact backups."""

    @pytest.mark.parametrize(
        "release_name",
        [pytest.param("v1.0.0", id="flat"), pytest.param("releases/v1.0.0", id="nested-parents")],
    )
    def test_creates_release_dir_and_symlink(self, tmp_path: Path, changelog: Path, release_name: str) -> None:
        """The documented ``RELEASE_DIR CHANGELOG_FILE`` call creates the directory and a changelog symlink.

        The release dir may have non-existent parents; it is created recursively and its ``CHANGELOG.md`` resolves to
        the canonical file.
        """
        release_dir = tmp_path / release_name

        rc = srd.main([str(release_dir), str(changelog)])

        link = release_dir / "CHANGELOG.md"
        assert (rc, release_dir.is_dir(), link.is_symlink(), link.resolve()) == (0, True, True, changelog.resolve())

    def test_validate_only_accepts_docs_changelog(self, tmp_path: Path) -> None:
        """The documented ``docs/CHANGELOG.md`` discovery path passes the preflight without writing.

        Release discovery may pick a changelog below ``docs/``; the preflight must accept it and leave it untouched.
        """
        changelog = tmp_path / "docs" / "CHANGELOG.md"
        changelog.parent.mkdir()
        changelog.write_bytes(b"# Changes\n")
        release_dir = tmp_path / "releases" / "v1.0.0"

        rc = srd.main(["--validate-only", str(release_dir), str(changelog)])

        assert (rc, release_dir.exists(), changelog.read_bytes()) == (0, False, b"# Changes\n")

    def test_links_docs_changelog(self, tmp_path: Path) -> None:
        """The setup phase links a ``docs/CHANGELOG.md`` source into the release directory.

        Same discovery path as the preflight, now through the writing phase.
        """
        changelog = tmp_path / "docs" / "CHANGELOG.md"
        changelog.parent.mkdir()
        changelog.write_bytes(b"# Changes\n")
        release_dir = tmp_path / "releases" / "v1.0.0"

        rc = srd.main([str(release_dir), str(changelog)])

        assert (rc, (release_dir / "CHANGELOG.md").resolve(), changelog.read_bytes()) == (
            0,
            changelog,
            b"# Changes\n",
        )

    @pytest.mark.parametrize("name", srd._ARTIFACTS)
    def test_backs_up_existing_artifact(self, tmp_path: Path, changelog: Path, name: str) -> None:
        """A pre-existing recognised artifact is copied to ``<name>.bak`` before it can be overwritten.

        Hand edits to a previous draft must survive a rerun of prepare.
        """
        release_dir = tmp_path / "rel"
        release_dir.mkdir()
        (release_dir / name).write_text("# old\n", encoding="utf-8")

        rc = srd.main([str(release_dir), str(changelog)])

        assert (rc, (release_dir / f"{name}.bak").read_text(encoding="utf-8")) == (0, "# old\n")

    def test_backs_up_all_artifacts_present_together(self, tmp_path: Path, changelog: Path) -> None:
        """All recognised artifacts get backed up when present at the same time.

        Backups must not collide or stop after the first artifact.
        """
        release_dir = tmp_path / "rel"
        release_dir.mkdir()
        _write_files(release_dir, {name: f"# {name}\n".encode() for name in srd._ARTIFACTS})

        rc = srd.main([str(release_dir), str(changelog)])

        backups = {name: (release_dir / f"{name}.bak").read_bytes() for name in srd._ARTIFACTS}
        assert rc == 0
        assert backups == {name: f"# {name}\n".encode() for name in srd._ARTIFACTS}

    def test_absent_artifacts_are_not_backed_up(self, tmp_path: Path, changelog: Path) -> None:
        """Artifacts absent from the release dir produce no ``.bak`` files.

        A first run has nothing to back up, and empty backups would read as lost edits.
        """
        release_dir = tmp_path / "rel"

        srd.main([str(release_dir), str(changelog)])

        assert not any((release_dir / f"{name}.bak").exists() for name in srd._ARTIFACTS)

    def test_rerun_replaces_existing_symlink(self, tmp_path: Path, changelog: Path) -> None:
        """Re-running with another changelog repoints the existing ``CHANGELOG.md`` symlink safely.

        The second invocation selects ``CHANGELOG2.md``; the link must follow it instead of failing on the old link.
        """
        release_dir = tmp_path / "rel"
        srd.main([str(release_dir), str(changelog)])
        changelog2 = tmp_path / "CHANGELOG2.md"
        changelog2.write_text("v2\n", encoding="utf-8")

        rc = srd.main([str(release_dir), str(changelog2)])

        assert (rc, (release_dir / "CHANGELOG.md").resolve()) == (0, changelog2.resolve())

    def test_validate_only_checks_sources_without_writing(self, tmp_path: Path) -> None:
        """Prepare can validate artifact paths before the first changelog edit.

        The preflight exits 0 on a hand-edited draft and creates no backup and no changelog link.
        """
        release_dir = tmp_path / "release"
        release_dir.mkdir()
        draft = release_dir / "DRAFT.md"
        draft.write_bytes(b"HAND EDITED\n")
        changelog = tmp_path / "CHANGELOG.md"
        changelog.write_bytes(b"PRIOR\n")

        rc = srd.main(["--validate-only", str(release_dir), str(changelog)])

        assert (
            rc,
            draft.read_bytes(),
            changelog.read_bytes(),
            (release_dir / "DRAFT.md.bak").exists(),
            (release_dir / "CHANGELOG.md").exists(),
        ) == (0, b"HAND EDITED\n", b"PRIOR\n", False, False)


class TestRefusals:
    """Inputs that would read or overwrite the wrong file are refused with exit 1 before anything is written."""

    def test_relative_release_symlink_cannot_replace_external_changelog(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A relative release path through a symlink must preserve bytes outside the project.

        ``releases/v1.0.0`` points at a directory holding an unrelated ``CHANGELOG.md``; linking there would replace it.
        """
        repo = tmp_path / "repo"
        outside = tmp_path / "outside"
        (repo / "releases").mkdir(parents=True)
        outside.mkdir()
        external = outside / "CHANGELOG.md"
        external.write_bytes(b"KEEP\n")
        (repo / "CHANGELOG.md").write_bytes(b"SOURCE\n")
        (repo / "releases" / "v1.0.0").symlink_to(outside, target_is_directory=True)
        monkeypatch.chdir(repo)

        rc = srd.main(["releases/v1.0.0", "CHANGELOG.md"])

        assert (rc, external.read_bytes(), external.is_symlink()) == (1, b"KEEP\n", False)

    @pytest.mark.parametrize("flags", _PHASES)
    def test_changelog_alias_cannot_select_unrelated_project_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, flags: list[str]
    ) -> None:
        """Both prepare phases reject a changelog alias before reading an unrelated file.

        ``CHANGELOG.md`` is a symlink to ``pyproject.toml``; neither the preflight nor the setup may follow it.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        unrelated = repo / "pyproject.toml"
        unrelated.write_bytes(_PYPROJECT)
        (repo / "CHANGELOG.md").symlink_to(unrelated)
        monkeypatch.chdir(repo)
        release_dir = repo / "releases" / "v1.0.0"

        rc = srd.main([*flags, str(release_dir), "CHANGELOG.md"])

        assert (rc, unrelated.read_bytes(), release_dir.exists()) == (1, _PYPROJECT, False)

    def test_changelog_argument_cannot_name_unrelated_file(self, tmp_path: Path) -> None:
        """The selected changelog path must have the name used by release discovery.

        Passing ``pyproject.toml`` as the changelog would back it up and link it into the release directory.
        """
        unrelated = tmp_path / "pyproject.toml"
        unrelated.write_bytes(_PYPROJECT)
        release_dir = tmp_path / "releases" / "v1.0.0"

        rc = srd.main(["--validate-only", str(release_dir), str(unrelated)])

        assert (rc, unrelated.read_bytes(), release_dir.exists()) == (1, _PYPROJECT, False)

    def test_changelog_argument_cannot_select_same_named_file_outside_project(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An allowed temporary root must not substitute for the release project's changelog.

        A file named ``CHANGELOG.md`` in a sibling directory is not the project's changelog and must be refused.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        changelog = outside / "CHANGELOG.md"
        changelog.write_bytes(b"UNRELATED\n")
        monkeypatch.chdir(repo)
        release_dir = repo / "releases" / "v1.0.0"

        rc = srd.main(["--validate-only", str(release_dir), str(changelog)])

        assert (rc, changelog.read_bytes(), release_dir.exists()) == (1, b"UNRELATED\n", False)

    @pytest.mark.parametrize("flags", _PHASES)
    def test_regular_release_changelog_is_preserved(self, tmp_path: Path, flags: list[str]) -> None:
        """A hand-written release changelog survives preflight and setup without replacement.

        An existing regular ``CHANGELOG.md`` in the release dir is never turned into a symlink, and no draft is backed
        up.
        """
        release_dir = tmp_path / "releases" / "v1.0.0"
        release_dir.mkdir(parents=True)
        _write_files(release_dir, {"CHANGELOG.md": b"HAND WRITTEN\n", "DRAFT.md": b"DRAFT\n"})
        source = tmp_path / "CHANGELOG.md"
        source.write_bytes(b"SOURCE\n")

        rc = srd.main([*flags, str(release_dir), str(source)])

        release_changelog = release_dir / "CHANGELOG.md"
        assert (rc, release_changelog.read_bytes(), release_changelog.is_symlink()) == (1, b"HAND WRITTEN\n", False)
        assert (release_dir / "DRAFT.md").read_bytes() == b"DRAFT\n"
        assert (source.read_bytes(), (release_dir / "DRAFT.md.bak").exists()) == (b"SOURCE\n", False)

    @pytest.mark.parametrize(
        "outside_files",
        [pytest.param({"outside.txt": b"KEEP\n"}, id="existing-target"), pytest.param({}, id="dangling-target")],
    )
    def test_backup_destination_symlink_cannot_write_outside_release(
        self, tmp_path: Path, outside_files: dict[str, bytes]
    ) -> None:
        """An existing or dangling backup symlink must not redirect a release backup.

        ``waived-changes.md.bak`` is a symlink to a file outside the release directory (present or absent); writing the
        backup through it would create or overwrite that file, so the run stops before any other write.
        """
        release_dir = tmp_path / "release"
        release_dir.mkdir()
        _write_files(release_dir, {"waived-changes.md": b"NEW\n", "HIGHLIGHTS.md": b"HIGHLIGHTS\n"})
        _write_files(tmp_path, outside_files)
        backup = release_dir / "waived-changes.md.bak"
        backup.symlink_to(tmp_path / "outside.txt")
        changelog = tmp_path / "CHANGELOG.md"
        changelog.write_bytes(b"CHANGELOG\n")

        rc = srd.main([str(release_dir), str(changelog)])

        outside_after = {path.name: path.read_bytes() for path in tmp_path.glob("outside.txt")}
        assert (rc, backup.is_symlink(), outside_after) == (1, True, outside_files)
        assert not (release_dir / "HIGHLIGHTS.md.bak").exists()
        assert not (release_dir / "CHANGELOG.md").exists()

    @pytest.mark.parametrize("name", srd._ARTIFACTS)
    def test_linked_artifact_source_refused_before_changelog_or_backup(self, tmp_path: Path, name: str) -> None:
        """A linked prior draft must not be read through or allow earlier writes.

        Each recognised artifact in turn is a symlink to an outside file; the run stops before it links the changelog or
        backs up the (regular) prior ``HIGHLIGHTS.md``.
        """
        release_dir = tmp_path / "release"
        release_dir.mkdir()
        regular = {key: value for key, value in {"HIGHLIGHTS.md": b"PRIOR\n"}.items() if key != name}
        _write_files(release_dir, regular)
        outside = tmp_path / "outside.txt"
        outside.write_bytes(b"KEEP\n")
        (release_dir / name).symlink_to(outside)
        changelog = tmp_path / "CHANGELOG.md"
        changelog.write_bytes(b"CHANGELOG\n")

        rc = srd.main([str(release_dir), str(changelog)])

        assert (rc, outside.read_bytes(), changelog.read_bytes()) == (1, b"KEEP\n", b"CHANGELOG\n")
        assert not (release_dir / "HIGHLIGHTS.md.bak").exists()
        assert not (release_dir / "CHANGELOG.md").exists()


class TestShippedInstructions:
    """The release instructions that call this script keep the validate-before-write ordering."""

    def test_prepare_validates_paths_before_changelog_edit(self) -> None:
        """The inline prepare audit rechecks paths before its own edit.

        A changelog write that runs before ``--validate-only`` could follow a planted link.
        """
        prepare = _prose("modes/prepare.md")
        phase = prepare[prepare.index("**b. Audit changelog**") : prepare.index("### Phase 3:")]

        assert phase.index("--validate-only") < phase.index("Then apply **Audit changelog** logic inline")

    def test_delegated_changelog_audit_validates_prepare_paths_before_read_or_write(self) -> None:
        """Agent A validates its selected path before touching the changelog.

        The delegated audit must hand the agent the release mode and version, and run ``--validate-only`` before it
        reads the classified change table.
        """
        delegation = _prose("modes/delegation.md")
        prompt = _prose("modes/changelog-audit-prompt.md")
        dispatch = delegation[
            delegation.index("**Phases 5–6 parallel delegation**") : delegation.index("Validate both envelopes:")
        ]
        agent_a = prompt[prompt.index("Agent A — Audit changelog") : prompt.index("Agent B — Extract contributors")]

        assert 'IFS= read -r RELEASE_MODE < "${TMPDIR:-/tmp}/release-mode-${CSID}"' in dispatch
        assert 'IFS= read -r VERSION < "${TMPDIR:-/tmp}/release-prepare-version-${CSID}"' in dispatch
        assert "`$RELEASE_MODE`, `$VERSION`" in prompt
        assert agent_a.index("--validate-only") < agent_a.index("Read classified change table")
        assert "<RELEASE_MODE>" in agent_a
        assert "<VERSION>" in agent_a
