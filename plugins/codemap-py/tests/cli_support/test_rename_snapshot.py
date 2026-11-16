"""Behaviour of ``bin/rename_snapshot.py``, the per-edit rollback journal behind ``rename-refs``.

Every case drives the shipped executable in a subprocess against a throwaway project, so the tests exercise the same
command line both skills run. Each test gets its own ``TMPDIR`` so journals never leak between cases. Pure helpers
(Windows path containment, reparse-point detection) are loaded from the script file inside the test that needs them.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import ModuleType, SimpleNamespace

import pytest
from _host_skips import skip_file_symlink_unavailable

_PLUGIN_ROOT = Path(__file__).resolve().parents[2]
_HELPER = _PLUGIN_ROOT / "bin" / "rename_snapshot.py"
_HOOKUTIL = _PLUGIN_ROOT / "hooks" / "_hookutil.py"
_SESSION_VARS = ("CSID", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CODEMAP_RUNTIME")
_REFUSED = 3
_CONFLICTS = 4
_LEFTOVER = 5
_FILES = ("a", "b", "c", "d", "e")


def _load(path: Path, name: str) -> ModuleType:
    """Import a plugin script by file path, so a missing script fails only the test that needs it."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


def _tree(root: Path) -> dict[str, bytes]:
    """Map every file under *root* (outside ``.git``) to its bytes."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".git" not in path.relative_to(root).parts
    }


def _git(root: Path, *args: str) -> str:
    """Run git in a throwaway repository and return its stdout."""
    done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=60, check=True)
    return done.stdout


class _Journal:
    """Run the helper for one project with one session environment."""

    def __init__(self, root: Path, base: Path, **extra: str) -> None:
        """Bind the project root and a private ``TMPDIR``; *extra* adds environment variables."""
        self.root = root
        self.base = base
        self.env = {key: value for key, value in os.environ.items() if key not in _SESSION_VARS}
        self.env.update(TMPDIR=str(base), CSID="journal-test")
        self.env.update(extra)

    def run(self, *args: str, cwd: Path | None = None, pin_root: bool = True) -> subprocess.CompletedProcess[str]:
        """Invoke the helper; ``pin_root`` passes ``--root`` so an enclosing repository is never picked up."""
        prefix = ["--root", str(self.root)] if pin_root else []
        return subprocess.run(
            [sys.executable, str(_HELPER), *prefix, *args],
            cwd=cwd or self.root,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def ok(self, *args: str, cwd: Path | None = None, pin_root: bool = True) -> str:
        """Invoke the helper and require exit 0; return stdout."""
        done = self.run(*args, cwd=cwd, pin_root=pin_root)
        assert done.returncode == 0, done.stdout + done.stderr
        return done.stdout

    def journals(self) -> list[Path]:
        """Return every journal directory (active or retained) under this test's ``TMPDIR``."""
        return sorted(self.base.glob("codemap-rename-journal-*"))


@pytest.fixture(name="project")
def _project(tmp_path: Path) -> Path:
    """Create a small project whose files mix line endings, so restores are checked byte for byte."""
    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    for name in _FILES:
        (root / "pkg" / f"{name}.py").write_bytes(f"def {name}():\r\n    return '{name}'\n".encode())
    return root


@pytest.fixture(name="journal")
def _journal(tmp_path: Path, project: Path) -> _Journal:
    """Return a helper runner with its own journal base directory."""
    base = tmp_path / "tmp"
    base.mkdir()
    return _Journal(project, base)


def _edit(path: Path, text: str) -> None:
    """Replace a file's bytes, standing in for one skill ``Edit``."""
    path.write_bytes(text.encode())


def _conflict_paths(report: str) -> list[str]:
    """Return the path named by each ``CONFLICT`` line of a restore report, in report order."""
    lines = [line for line in report.splitlines() if line.startswith("CONFLICT ")]
    return [line.removeprefix("CONFLICT ").split(" — ", 1)[0] for line in lines]


def _retained_path(report: str) -> Path:
    """Return the directory named by the ``RETAINED_JOURNAL`` line of a helper report."""
    return Path(next(line.split(" ", 1)[1] for line in report.splitlines() if line.startswith("RETAINED_JOURNAL ")))


@pytest.fixture(name="repo")
def _repo(project: Path) -> Path:
    """Commit the project as a git repository whose ``pkg/use.py`` imports ``pkg/old.py``."""
    _edit(project / "pkg" / "old.py", "def old():\n    return 1\n")
    _edit(project / "pkg" / "use.py", "from pkg.old import old\n")
    _git(project, "init", "-q")
    _git(project, "add", "--", "pkg")
    _git(project, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "base")
    return project


def _move_module(journal: _Journal) -> None:
    """Journal a module rename up to the caller edit: ``git mv`` old to new, edit new, edit the caller.

    The caller's ``after`` record is left to the test, so one test can skip it and another can record it.
    """
    journal.ok("before", "--file", "pkg/old.py")
    _git(journal.root, "mv", "--", "pkg/old.py", "pkg/new.py")
    journal.ok("after", "--move", "pkg/old.py", "pkg/new.py")
    journal.ok("before", "--file", "pkg/new.py")
    _edit(journal.root / "pkg" / "new.py", "def new():\n    return 1\n")
    journal.ok("after", "--file", "pkg/new.py")
    journal.ok("before", "--file", "pkg/use.py")
    _edit(journal.root / "pkg" / "use.py", "from pkg.new import new\n")


def _journaled_edits(journal: _Journal, names: tuple[str, ...], text: str) -> None:
    """Edit each ``pkg/<name>.py`` and record it with ``after``, as the skill does after every ``Edit``."""
    for name in names:
        _edit(journal.root / "pkg" / f"{name}.py", text)
        journal.ok("after", "--file", f"pkg/{name}.py")


class TestRestoreAfterFailure:
    """A post-edit failure returns the project to its pre-run bytes."""

    def test_forced_failure_restores_tree_byte_identical(self, journal: _Journal, project: Path) -> None:
        """Journal five files, edit three, create one, then restore: the tree and file modes match the original."""
        os.chmod(project / "pkg" / "c.py", 0o700)
        original = _tree(project)
        modes = {name: stat.S_IMODE((project / "pkg" / f"{name}.py").stat().st_mode) for name in _FILES}
        journal.ok("before")
        journal.ok("before", *[arg for name in _FILES for arg in ("--file", f"pkg/{name}.py")])
        _journaled_edits(journal, ("a", "b", "c"), "def renamed():\n    pass\n")
        journal.ok("before", "--file", "pkg/new.py")
        _edit(project / "pkg" / "new.py", "created = True\n")
        journal.ok("after", "--file", "pkg/new.py")

        report = journal.ok("restore")

        assert _tree(project) == original
        assert {name: stat.S_IMODE((project / "pkg" / f"{name}.py").stat().st_mode) for name in _FILES} == modes
        assert "RESTORE_SUMMARY restored=3 removed=1 moved_back=0" in report
        assert journal.journals() == []

    def test_second_restore_finds_nothing(self, journal: _Journal, project: Path) -> None:
        """A completed restore removes the journal; running restore again changes nothing."""
        original = _tree(project)
        journal.ok("before", "--file", "pkg/a.py")
        _edit(project / "pkg" / "a.py", "changed\n")
        journal.ok("after", "--file", "pkg/a.py")
        journal.ok("restore")

        assert "NO_JOURNAL" in journal.ok("restore")
        assert _tree(project) == original

    def test_discard_after_success_keeps_edits(self, journal: _Journal, project: Path) -> None:
        """Discard removes the journal and leaves the run's edits in place."""
        journal.ok("before", "--file", "pkg/a.py")
        _edit(project / "pkg" / "a.py", "renamed\n")
        journal.ok("after", "--file", "pkg/a.py")

        assert "DISCARDED" in journal.ok("discard")
        assert (project / "pkg" / "a.py").read_bytes() == b"renamed\n"
        assert journal.journals() == []


class TestConflicts:
    """Bytes the run did not write are never overwritten or deleted; they are reported."""

    def test_human_edit_after_recorded_edit_is_kept(self, journal: _Journal, project: Path) -> None:
        """A file changed after the run's last recorded edit keeps its bytes; the original stays in the journal.

        Restore is all-or-nothing, so the cleanly restorable ``b.py`` beside the conflict also keeps the run's edit.
        """
        original_a = (project / "pkg" / "a.py").read_bytes()
        journal.ok("before", "--file", "pkg/a.py", "--file", "pkg/b.py")
        _journaled_edits(journal, ("a", "b"), "skill edit\n")
        _edit(project / "pkg" / "a.py", "human edit\n")

        done = journal.run("restore")

        assert done.returncode == _CONFLICTS, done.stdout + done.stderr
        assert "CONFLICT pkg/a.py" in done.stdout
        assert (project / "pkg" / "a.py").read_bytes() == b"human edit\n"
        assert (project / "pkg" / "b.py").read_bytes() == b"skill edit\n"
        retained = journal.journals()
        assert len(retained) == 1
        assert "RETAINED_JOURNAL" in done.stdout
        assert original_a in {blob.read_bytes() for blob in retained[0].rglob("*.bin")}

    @pytest.mark.parametrize(
        "record_created",
        [
            pytest.param(False, id="write-never-recorded"),
            pytest.param(True, id="rewritten-after-create"),
        ],
    )
    def test_destination_created_by_someone_else_is_kept(
        self, journal: _Journal, project: Path, record_created: bool
    ) -> None:
        """A path journaled as absent is deleted only when it still holds the run's recorded created bytes."""
        journal.ok("before", "--file", "pkg/new.py")
        if record_created:
            _edit(project / "pkg" / "new.py", "skill created\n")
            journal.ok("after", "--file", "pkg/new.py")
        _edit(project / "pkg" / "new.py", "someone else\n")

        done = journal.run("restore")

        assert done.returncode == _CONFLICTS, done.stdout + done.stderr
        assert "CONFLICT pkg/new.py" in done.stdout
        assert (project / "pkg" / "new.py").read_bytes() == b"someone else\n"

    def test_file_edited_without_before_is_refused_and_reported(self, journal: _Journal, project: Path) -> None:
        """A late file changed after the journal opened is refused by ``before``; ``after`` alone is reported."""
        journal.ok("before", "--file", "pkg/a.py")
        _edit(project / "pkg" / "d.py", "edited without journal\n")
        later = time.time_ns() + 5_000_000_000
        os.utime(project / "pkg" / "d.py", ns=(later, later))

        refused = journal.run("before", "--file", "pkg/d.py")
        unjournaled = journal.run("after", "--file", "pkg/d.py")
        done = journal.run("restore")

        assert refused.returncode == _REFUSED
        assert "changed after the rename journal opened" in refused.stderr
        assert unjournaled.returncode == _REFUSED
        assert "without `before`" in unjournaled.stderr
        assert done.returncode == _CONFLICTS
        assert "CONFLICT pkg/d.py" in done.stdout
        assert (project / "pkg" / "d.py").read_bytes() == b"edited without journal\n"

    def test_change_between_recorded_edits_refuses_next_before(self, journal: _Journal, project: Path) -> None:
        """A second ``before`` on a journaled file refuses when the file moved past its last recorded state."""
        journal.ok("before", "--file", "pkg/a.py")
        _edit(project / "pkg" / "a.py", "skill edit\n")
        journal.ok("after", "--file", "pkg/a.py")
        _edit(project / "pkg" / "a.py", "human edit\n")

        done = journal.run("before", "--file", "pkg/a.py")

        assert done.returncode == _REFUSED
        assert "changed since its last journaled state" in done.stderr


class TestRestoreIsAllOrNothing:
    """Restore checks every recorded operation before writing and writes nothing when any one conflicts."""

    def test_conflict_alone_keeps_tree_and_journal(self, journal: _Journal, project: Path) -> None:
        """A single conflicting file is left as is, the journal is kept and the report says nothing was restored."""
        journal.ok("before", "--file", "pkg/a.py")
        _journaled_edits(journal, ("a",), "skill edit\n")
        _edit(project / "pkg" / "a.py", "human edit\n")
        before_restore = _tree(project)

        done = journal.run("restore")

        assert done.returncode == _CONFLICTS, done.stdout + done.stderr
        assert _conflict_paths(done.stdout) == ["pkg/a.py"]
        assert "NOTHING_RESTORED" in done.stdout
        assert _tree(project) == before_restore
        assert len(journal.journals()) == 1

    def test_skipped_after_record_blocks_every_restore(self, journal: _Journal, project: Path) -> None:
        """A file edited without its ``after`` record is a conflict, so the recorded edit beside it stays too.

        This is the run that wrote a file and died (or skipped a step) before recording it: restoring only the recorded
        half would leave a tree that matches neither the original nor the rename.
        """
        journal.ok("before", "--file", "pkg/a.py", "--file", "pkg/b.py")
        _edit(project / "pkg" / "a.py", "edit without after\n")
        _journaled_edits(journal, ("b",), "recorded edit\n")
        before_restore = _tree(project)

        done = journal.run("restore")

        assert done.returncode == _CONFLICTS, done.stdout + done.stderr
        assert _conflict_paths(done.stdout) == ["pkg/a.py"]
        assert "no recorded edit by this run" in done.stdout
        assert _tree(project) == before_restore
        assert len(journal.journals()) == 1


@pytest.mark.integration
class TestRestoreModuleMove:
    """A recorded module ``git mv`` is undone only when the whole restore plan is clean."""

    def test_conflict_leaves_move_and_index_in_place(self, journal: _Journal, repo: Path) -> None:
        """A caller edited without its ``after`` keeps the move: nothing moves back and git's index is unchanged.

        Undoing the move while the caller keeps its edit would leave ``use.py`` importing a module that no longer
        exists, a new broken state the rename never produced.
        """
        _move_module(journal)
        state = (_tree(repo), _git(repo, "ls-files", "-s"), _git(repo, "status", "--porcelain"))

        done = journal.run("restore")

        assert done.returncode == _CONFLICTS, done.stdout + done.stderr
        assert _conflict_paths(done.stdout) == ["pkg/use.py"]
        assert "MOVED_BACK" not in done.stdout
        assert (_tree(repo), _git(repo, "ls-files", "-s"), _git(repo, "status", "--porcelain")) == state
        assert len(journal.journals()) == 1

    def test_unstaged_destination_blocks_every_restore(self, journal: _Journal, repo: Path) -> None:
        """A moved file dropped from git's index is a conflict found before any write, so no other file is restored.

        ``git mv`` back needs the destination in the index; finding that only at the move would leave the caller and the
        moved file already restored while the module keeps its new name.
        """
        _move_module(journal)
        journal.ok("after", "--file", "pkg/use.py")
        _git(repo, "rm", "--cached", "-f", "-q", "--", "pkg/new.py")
        before_restore = _tree(repo)

        done = journal.run("restore")

        assert done.returncode == _CONFLICTS, done.stdout + done.stderr
        assert _conflict_paths(done.stdout) == ["pkg/new.py"]
        assert "NOTHING_RESTORED" in done.stdout
        assert _tree(repo) == before_restore

    def test_clean_plan_moves_back_and_restores_every_file(self, journal: _Journal, repo: Path) -> None:
        """With every write recorded, restore returns the caller, the moved module and git's index to the commit.

        The check phase must judge each operation against the state the earlier undos leave behind, not the raw disk:
        before any write the moved file still holds its edit and the original path is empty.
        """
        state = (_tree(repo), _git(repo, "ls-files", "-s"))
        _move_module(journal)
        journal.ok("after", "--file", "pkg/use.py")

        report = journal.ok("restore")

        assert "MOVED_BACK pkg/new.py -> pkg/old.py" in report
        assert (_tree(repo), _git(repo, "ls-files", "-s")) == state
        assert journal.journals() == []


class TestRetainedJournalRestore:
    """``restore --journal`` replays a journal that ``discard --retain`` set aside."""

    def test_retained_journal_restores_original_bytes(self, journal: _Journal, project: Path) -> None:
        """A set-aside journal restores its run's edits by path and is removed afterwards."""
        original = _tree(project)
        journal.ok("before", "--file", "pkg/a.py")
        _journaled_edits(journal, ("a",), "abandoned run\n")
        retained = _retained_path(journal.run("discard", "--retain").stdout)

        report = journal.ok("restore", "--journal", str(retained))

        assert "RESTORED pkg/a.py" in report
        assert _tree(project) == original
        assert journal.journals() == []

    def test_retained_journal_conflict_keeps_it_in_place(self, journal: _Journal, project: Path) -> None:
        """A conflict while restoring a retained journal keeps that journal at the same path, not renamed again."""
        journal.ok("before", "--file", "pkg/a.py")
        _journaled_edits(journal, ("a",), "abandoned run\n")
        retained = _retained_path(journal.run("discard", "--retain").stdout)
        _edit(project / "pkg" / "a.py", "human edit\n")

        done = journal.run("restore", "--journal", str(retained))

        assert done.returncode == _CONFLICTS, done.stdout + done.stderr
        assert _retained_path(done.stdout) == retained
        assert journal.journals() == [retained]
        assert (project / "pkg" / "a.py").read_bytes() == b"human edit\n"

    def test_journal_outside_temporary_base_refused(self, journal: _Journal, project: Path, tmp_path: Path) -> None:
        """A ``--journal`` directory outside the temporary base is refused, even when it holds a valid journal."""
        journal.ok("before", "--file", "pkg/a.py")
        _journaled_edits(journal, ("a",), "skill edit\n")
        copied = tmp_path / "codemap-rename-journal-copy"
        shutil.copytree(journal.journals()[0], copied)

        done = journal.run("restore", "--journal", str(copied))

        assert done.returncode == _REFUSED, done.stdout + done.stderr
        assert (project / "pkg" / "a.py").read_bytes() == b"skill edit\n"


class TestContainment:
    """Only regular files inside the project root, reached without links, are journaled or restored."""

    @pytest.mark.parametrize(
        "relative",
        [pytest.param(True, id="dot-dot-escape"), pytest.param(False, id="absolute-outside")],
    )
    def test_outside_root_refused(self, journal: _Journal, tmp_path: Path, relative: bool) -> None:
        """A path resolving outside the project root is refused and never journaled."""
        outside = tmp_path / "outside.py"
        outside.write_bytes(b"outside\n")
        target = "../outside.py" if relative else str(outside)

        done = journal.run("before", "--file", target)

        assert done.returncode == _REFUSED
        assert "outside the project root" in done.stderr
        assert outside.read_bytes() == b"outside\n"

    @skip_file_symlink_unavailable
    def test_absolute_path_through_root_alias_accepted(self, journal: _Journal, project: Path, tmp_path: Path) -> None:
        """An absolute path spelled through another name for the root (``/var`` vs ``/private/var``) is journaled."""
        (tmp_path / "alias").symlink_to(project, target_is_directory=True)

        done = journal.run("before", "--file", str(tmp_path / "alias" / "pkg" / "a.py"))

        assert done.returncode == 0, done.stderr
        assert "JOURNALED pkg/a.py" in done.stdout

    @skip_file_symlink_unavailable
    def test_symlinked_leaf_refused(self, journal: _Journal, project: Path, tmp_path: Path) -> None:
        """A symlinked file is refused even when its target lies inside the project."""
        (project / "pkg" / "link.py").symlink_to(project / "pkg" / "a.py")

        done = journal.run("before", "--file", "pkg/link.py")

        assert done.returncode == _REFUSED
        assert "symlink" in done.stderr

    @skip_file_symlink_unavailable
    def test_symlinked_parent_refused(self, journal: _Journal, project: Path, tmp_path: Path) -> None:
        """A path through a symlinked directory is refused, so no write can land outside the root."""
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "x.py").write_bytes(b"x\n")
        (project / "linked").symlink_to(elsewhere, target_is_directory=True)

        done = journal.run("before", "--file", "linked/x.py")

        assert done.returncode == _REFUSED
        assert "symlink" in done.stderr

    @skip_file_symlink_unavailable
    def test_link_planted_after_journaling_refused_at_restore(
        self, journal: _Journal, project: Path, tmp_path: Path
    ) -> None:
        """Containment is checked again at restore: a leaf swapped for a link is reported, its target untouched."""
        outside = tmp_path / "outside.py"
        outside.write_bytes(b"outside\n")
        journal.ok("before", "--file", "pkg/a.py")
        _edit(project / "pkg" / "a.py", "skill edit\n")
        journal.ok("after", "--file", "pkg/a.py")
        (project / "pkg" / "a.py").unlink()
        (project / "pkg" / "a.py").symlink_to(outside)

        done = journal.run("restore")

        assert done.returncode == _CONFLICTS
        assert "CONFLICT pkg/a.py" in done.stdout
        assert outside.read_bytes() == b"outside\n"

    @pytest.mark.parametrize(
        ("candidate", "expected"),
        [
            pytest.param(r"C:\proj\pkg\a.py", "pkg/a.py", id="inside"),
            pytest.param(r"pkg\sub\..\a.py", "pkg/a.py", id="relative-normalized"),
            pytest.param(r"C:\proj\..\other\a.py", None, id="dot-dot-escape"),
            pytest.param(r"D:\proj\pkg\a.py", None, id="other-drive"),
            pytest.param(r"C:\proj", None, id="root-itself"),
        ],
    )
    def test_windows_paths_contained_lexically(self, candidate: str, expected: str | None) -> None:
        """Windows-shaped paths are normalized and contained without touching the filesystem."""
        helper = _load(_HELPER, "rename_snapshot_lexical")

        relative = helper._lexical_relative(PureWindowsPath(r"C:\proj"), PureWindowsPath(candidate))

        assert relative == (None if expected is None else PurePosixPath(expected))

    @pytest.mark.parametrize(
        ("attributes", "expected"),
        [
            pytest.param(stat.FILE_ATTRIBUTE_REPARSE_POINT, True, id="junction"),
            pytest.param(0, False, id="plain-directory"),
        ],
    )
    def test_reparse_point_component_detected(self, attributes: int, expected: bool) -> None:
        """A directory carrying the Windows reparse-point attribute (a junction) counts as a link component."""
        helper = _load(_HELPER, "rename_snapshot_reparse")
        status = SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_file_attributes=attributes)

        assert helper._is_link_like(status) is expected

    @pytest.mark.skipif(os.name != "nt", reason="directory junctions exist only on Windows")
    def test_real_junction_refused(self, journal: _Journal, project: Path, tmp_path: Path) -> None:
        """A path through a real NTFS junction is refused."""
        import _winapi

        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "x.py").write_bytes(b"x\n")
        _winapi.CreateJunction(str(elsewhere), str(project / "junction"))

        done = journal.run("before", "--file", "junction/x.py")

        assert done.returncode == _REFUSED


class TestSessionScope:
    """The journal key follows the hook layer's session key, scoped per project and runtime."""

    @pytest.mark.parametrize(
        ("thread", "suffix"),
        [pytest.param("thread-9", "proj-thread-9", id="codex-thread"), pytest.param("", "proj-shared", id="no-thread")],
    )
    def test_codex_runtime_uses_thread_id(self, tmp_path: Path, project: Path, thread: str, suffix: str) -> None:
        """Codex keys on ``CODEX_THREAD_ID``; without one the key stays project-scoped, never a bare ``shared``."""
        base = tmp_path / "tmp"
        base.mkdir()
        journal = _Journal(project, base, CODEX_THREAD_ID=thread)
        del journal.env["CSID"]

        journal.ok("--runtime", "codex", "before")

        assert [path.name for path in journal.journals()] == [f"codemap-rename-journal-{suffix}"]

    def test_key_matches_hook_session_key(
        self, journal: _Journal, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """From the project root the journal key equals ``_hookutil.session_key`` for the same environment."""
        for name in _SESSION_VARS:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("CSID", "journal-test")
        monkeypatch.chdir(project)
        expected = _load(_HOOKUTIL, "codemap_hookutil_for_journal").session_key(None)

        journal.ok("before")

        assert [path.name for path in journal.journals()] == [f"codemap-rename-journal-{expected}"]

    @pytest.mark.integration
    def test_subdirectory_call_reaches_same_journal(self, journal: _Journal, project: Path) -> None:
        """Calls from the repository root and from a subdirectory share one journal."""
        _git(project, "init", "-q")
        original = (project / "pkg" / "a.py").read_bytes()
        journal.ok("before", "--file", "pkg/a.py", pin_root=False)
        _edit(project / "pkg" / "a.py", "skill edit\n")
        journal.ok("after", "--file", "pkg/a.py", pin_root=False)

        journal.ok("restore", cwd=project / "pkg", pin_root=False)

        assert (project / "pkg" / "a.py").read_bytes() == original


class TestJournalDirectory:
    """The journal directory is private and never reached through a link."""

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    def test_journal_directory_is_private(self, journal: _Journal) -> None:
        """A new journal directory is created with mode 0700."""
        journal.ok("before")

        assert [stat.S_IMODE(path.stat().st_mode) for path in journal.journals()] == [0o700]

    @skip_file_symlink_unavailable
    def test_symlinked_journal_directory_refused(self, journal: _Journal, tmp_path: Path) -> None:
        """A planted symlink at the journal path is refused and its target stays empty."""
        planted = tmp_path / "planted"
        planted.mkdir()
        (journal.base / "codemap-rename-journal-proj-journal-test").symlink_to(planted, target_is_directory=True)

        done = journal.run("before", "--file", "pkg/a.py")

        assert done.returncode == _REFUSED
        assert list(planted.iterdir()) == []

    def test_leftover_journal_set_aside_before_new_run(self, journal: _Journal, project: Path) -> None:
        """``discard --retain`` sets an abandoned run aside; the next run's restore never touches its files."""
        journal.ok("before", "--file", "pkg/a.py")
        _edit(project / "pkg" / "a.py", "abandoned run\n")
        journal.ok("after", "--file", "pkg/a.py")
        original_b = (project / "pkg" / "b.py").read_bytes()

        left = journal.run("discard", "--retain")
        journal.ok("before", "--file", "pkg/b.py")
        _edit(project / "pkg" / "b.py", "second run\n")
        journal.ok("after", "--file", "pkg/b.py")
        journal.ok("restore")

        assert (left.returncode, "RETAINED_JOURNAL" in left.stdout) == (_LEFTOVER, True)
        assert (project / "pkg" / "a.py").read_bytes() == b"abandoned run\n"
        assert (project / "pkg" / "b.py").read_bytes() == original_b
        assert len(journal.journals()) == 1

    def test_leftover_journal_with_operations_exits_distinct_status(self, journal: _Journal, project: Path) -> None:
        """``discard --retain`` sets aside a journal holding a previous run's operations and exits 5.

        The distinct status lets the skill stop before it opens a new journal over a half-renamed tree, which would
        record the abandoned edits as this run's originals.
        """
        journal.ok("before", "--file", "pkg/a.py")
        _journaled_edits(journal, ("a",), "abandoned run\n")

        done = journal.run("discard", "--retain")

        assert done.returncode == _LEFTOVER, done.stdout + done.stderr
        assert "unfinished edits" in done.stderr
        assert journal.journals() == [_retained_path(done.stdout)]
        assert "-retained-" in _retained_path(done.stdout).name
        assert (project / "pkg" / "a.py").read_bytes() == b"abandoned run\n"

    def test_journal_without_operations_discarded_silently(self, journal: _Journal) -> None:
        """A leftover journal that recorded nothing is removed with exit 0, so a new run starts without a stop."""
        journal.ok("before")

        done = journal.run("discard", "--retain")

        assert done.returncode == 0, done.stdout + done.stderr
        assert "DISCARDED" in done.stdout
        assert journal.journals() == []


class TestUnrecordedMove:
    """A move the journal cannot record is still reported by restore, never left silent."""

    def test_move_of_never_existing_source_is_reported(self, journal: _Journal, project: Path) -> None:
        """A ``before`` on a wrong (absent) source path cannot hide the real move from restore.

        The source path is guessed wrong, so ``before`` journals an absent file; the real file is then moved, and
        ``after --move`` refuses. Restore must report the destination instead of finishing with no conflict.
        """
        journal.ok("before", "--file", "pkg/guessed.py")
        (project / "pkg" / "a.py").rename(project / "pkg" / "moved.py")

        refused = journal.run("after", "--move", "pkg/guessed.py", "pkg/moved.py")
        done = journal.run("restore")

        assert refused.returncode == _REFUSED
        assert done.returncode == _CONFLICTS
        assert "CONFLICT pkg/moved.py" in done.stdout


@pytest.mark.integration
class TestGitMove:
    """A journaled ``git mv`` is undone with ``git mv`` back, leaving the user's index as it was."""

    def test_git_mv_undone_with_index_matching(self, journal: _Journal, project: Path) -> None:
        """Move, then edit the moved file; restore returns bytes, ``git ls-files -s`` and ``git status`` exactly."""
        _git(project, "init", "-q")
        _git(project, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "add", "--", "pkg")
        _git(project, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "base")
        _edit(project / "pkg" / "a.py", "def a():\n    return 'staged'\n")
        _git(project, "add", "--", "pkg/a.py")
        _edit(project / "pkg" / "a.py", "def a():\n    return 'unstaged'\n")
        _edit(project / "pkg" / "c.py", "def c():\n    return 'user staged'\n")
        _git(project, "add", "--", "pkg/c.py")
        index, status, tree = _git(project, "ls-files", "-s"), _git(project, "status", "--porcelain"), _tree(project)

        journal.ok("before", "--file", "pkg/a.py")
        _git(project, "mv", "--", "pkg/a.py", "pkg/moved.py")
        journal.ok("after", "--move", "pkg/a.py", "pkg/moved.py")
        journal.ok("before", "--file", "pkg/moved.py")
        _edit(project / "pkg" / "moved.py", "def moved():\n    pass\n")
        journal.ok("after", "--file", "pkg/moved.py")
        report = journal.ok("restore")

        assert "MOVED_BACK pkg/moved.py -> pkg/a.py" in report
        assert _git(project, "ls-files", "-s") == index
        assert _git(project, "status", "--porcelain") == status
        assert _tree(project) == tree
