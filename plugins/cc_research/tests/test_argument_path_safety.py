"""Reject unsafe Kaggle and fencing paths before changing project artifacts."""

from __future__ import annotations

import tempfile
from pathlib import Path

import fence_untrusted
import parse_kaggle_args
import pytest


def _symlinks_supported() -> bool:
    """Probe file symlink support rather than assuming Windows privilege availability."""
    with tempfile.TemporaryDirectory() as directory:
        try:
            (Path(directory) / "link").symlink_to(Path(directory) / "target")
        except OSError:
            return False
    return True


_skip_no_symlink = pytest.mark.skipif(not _symlinks_supported(), reason="host cannot create symlinks")


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Use separate project and sentinel roots for argument parsing."""
    work = tmp_path / "project"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "path-test")
    return work


@pytest.mark.parametrize("competition", ["../../x", "--eda-only", "", "a..b", "a/b", "a\\b", "."])
def test_competition_must_be_a_safe_name(project: Path, competition: str) -> None:
    """Invalid competition identities fail as usage errors before creating experiment state."""
    with pytest.raises(SystemExit) as exc:
        parse_kaggle_args.main(["--", competition])
    assert exc.value.code == 2
    assert not (project / ".experiments").exists()
    assert not (project.parent / "kaggle-competition-name-path-test").exists()


@pytest.mark.parametrize("resume", ["../outside.py", pytest.param("ABSOLUTE", id="absolute-outside")])
def test_resume_cannot_leave_project(project: Path, resume: str) -> None:
    """A resume script outside the project is rejected before its path reaches a sentinel."""
    path = str(project.parent / "outside.py") if resume == "ABSOLUTE" else resume
    with pytest.raises(SystemExit) as exc:
        parse_kaggle_args.main(["--", f"titanic --resume {path}"])
    assert exc.value.code == 2
    assert not (project.parent / "kaggle-resume-path-test").exists()


@_skip_no_symlink
def test_resume_symlink_cannot_leave_project(project: Path) -> None:
    """A resume link into another directory cannot bypass resolved containment."""
    target = project.parent / "outside.py"
    target.write_text("outside", encoding="utf-8")
    (project / "resume.py").symlink_to(target)
    with pytest.raises(SystemExit) as exc:
        parse_kaggle_args.main(["--", "titanic --resume resume.py"])
    assert exc.value.code == 2


def test_resume_absolute_project_path_is_persisted_as_local_path(project: Path) -> None:
    """An absolute resume path inside the project remains usable and uses its canonical local coordinate."""
    resume = project / "old.py"
    resume.write_text("pass", encoding="utf-8")
    assert parse_kaggle_args.main(["--", f"titanic --resume {resume}"]) == 0
    assert (project.parent / "kaggle-resume-path-test").read_text(encoding="utf-8") == "old.py\n"


def test_fence_does_not_delete_raw_outside_run_directory(tmp_path: Path) -> None:
    """Fencing cannot consume an input from outside the destination's run directory."""
    raw = tmp_path / "outside.raw.md"
    raw.write_text("keep", encoding="utf-8")
    run = tmp_path / "run"
    run.mkdir()
    assert fence_untrusted.main([str(raw), str(run / "paper.md"), "--label", "paper"]) == 2
    assert raw.read_text(encoding="utf-8") == "keep"
    assert not (run / "paper.md").exists()


@_skip_no_symlink
def test_fence_replaces_output_link_without_overwriting_target(tmp_path: Path) -> None:
    """Atomic publication replaces an in-run output symlink rather than writing through it."""
    raw, out, target = tmp_path / "paper.raw.md", tmp_path / "paper.md", tmp_path / "keep.md"
    raw.write_text("paper body", encoding="utf-8")
    target.write_text("keep", encoding="utf-8")
    out.symlink_to(target)
    assert fence_untrusted.main([str(raw), str(out), "--label", "paper"]) == 0
    assert target.read_text(encoding="utf-8") == "keep"
    assert not out.is_symlink()
    assert "paper body" in out.read_text(encoding="utf-8")


@_skip_no_symlink
def test_fence_rejects_raw_symlink(tmp_path: Path) -> None:
    """The consumed staging file must be a local file rather than a link to another artifact."""
    target = tmp_path / "keep.md"
    target.write_text("keep", encoding="utf-8")
    raw = tmp_path / "paper.raw.md"
    raw.symlink_to(target)
    assert fence_untrusted.main([str(raw), str(tmp_path / "paper.md"), "--label", "paper"]) == 2
    assert raw.is_symlink()
    assert target.read_text(encoding="utf-8") == "keep"


@_skip_no_symlink
def test_fence_rejects_output_link_outside_run(tmp_path: Path) -> None:
    """An output link outside the run cannot redirect fenced publication or consume the staging file."""
    run = tmp_path / "run"
    run.mkdir()
    raw = run / "paper.raw.md"
    raw.write_text("body", encoding="utf-8")
    target = tmp_path / "keep.md"
    target.write_text("keep", encoding="utf-8")
    out = run / "paper.md"
    out.symlink_to(target)
    assert fence_untrusted.main([str(raw), str(out), "--label", "paper"]) == 2
    assert raw.exists()
    assert target.read_text(encoding="utf-8") == "keep"


def test_fence_rejects_run_outside_allowed_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A raw/output pair in one arbitrary directory still must satisfy the allowed-root boundary."""
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setattr(fence_untrusted.tempfile, "gettempdir", lambda: str(project))
    raw = tmp_path / "paper.raw.md"
    raw.write_text("body", encoding="utf-8")
    out = tmp_path / "paper.md"
    assert fence_untrusted.main([str(raw), str(out), "--label", "paper"]) == 2
    assert raw.exists()
    assert not out.exists()


@_skip_no_symlink
def test_fence_admits_run_under_symlinked_experiments(
    project: Path, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resolved experiments root outside the project and active temp root remains usable."""
    outside = tmp_path_factory.mktemp("fence-experiments")
    (project / ".experiments").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(fence_untrusted.tempfile, "gettempdir", lambda: str(project.parent))
    raw = project / ".experiments" / "paper.raw.md"
    raw.write_text("paper body", encoding="utf-8")

    assert fence_untrusted.main([str(raw), str(raw.with_name("paper.md")), "--label", "paper"]) == 0
