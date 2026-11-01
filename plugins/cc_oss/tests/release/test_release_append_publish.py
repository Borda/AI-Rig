"""Exercise staged append publication, interruption recovery and the release-skill shell blocks that drive them.

Most tests run the real ``bin/release_append_publish.py`` against a throw-away git repository copied from a module-level
template. The skill-block tests execute the shipped bash fences of ``skills/release`` unchanged, with the repository
state planted as the per-session sentinels those blocks read back. Git, bash and the publisher's own file replacement
are the contract here, so they are not mocked; only the interruption points and the Windows-only branches are simulated.
"""

import dataclasses
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path, PureWindowsPath
from unittest import mock
from urllib.parse import quote

import pytest
import release_append_marker as marker_api
from _text_io import bash_blocks, bash_path, write_lf

_PLUGIN_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _PLUGIN_ROOT / "bin/release_append_publish.py"
_SPEC = importlib.util.spec_from_file_location("release_append_publish", _SCRIPT)
assert _SPEC
assert _SPEC.loader
publisher = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(publisher)
_MAIN_MARKER = marker_api.state_relative("main", "marker")
_MAIN_PROVENANCE = marker_api.state_relative("main", "provenance.jsonl")
_SKILL = _PLUGIN_ROOT / "skills/release/SKILL.md"
_TEMPLATE = _PLUGIN_ROOT / "skills/release/modes/release-draft-template.md"
_MARKER_SCRIPT = _PLUGIN_ROOT / "bin/release_append_marker.py"

#: Live files exactly as ``_start`` leaves them before any candidate is promoted.
_ORIGINAL_DRAFT = "## Summary\nOriginal\n"
_ORIGINAL_CHANGELOG = "# Changelog\n"
_OLD_MARKER = "old\n"
_OLD_PROVENANCE = '{"schema_version":1,"patch_id":"old"}\n'
#: Setup sentinels every release shell block expects for a plain ``main`` checkout.
_MAIN_SETUP = {"BRANCH": "main", "BRANCH_REF": "main"}
_KEYED_SETUP = {**_MAIN_SETUP, "BRANCH_KEY": marker_api.branch_state_key("main")}

_skip_bash_unavailable = pytest.mark.skipif(shutil.which("bash") is None, reason="Release skill blocks need Bash")
_skip_bash_git_unavailable = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None, reason="Requires Bash and Git"
)


def _can_sync_directory() -> bool:
    """Probe whether this host can fsync an opened directory."""
    if not hasattr(os, "O_DIRECTORY"):
        return False
    try:
        descriptor = os.open(tempfile.gettempdir(), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        return False
    return True


_skip_directory_sync_unavailable = pytest.mark.skipif(
    not _can_sync_directory(), reason="Directory fsync is unavailable on this host"
)


def _native_path(shell_path: str) -> Path:
    """Resolve a Git Bash output path for native Python assertions."""
    if sys.platform != "win32":
        return Path(shell_path)
    return Path(subprocess.check_output(["cygpath", "-w", shell_path], text=True).strip())


def _read(path: Path) -> str:
    """Read UTF-8 text."""
    return path.read_text(encoding="utf-8")


def _write_files(directory: Path, files: dict[str, str] | dict[str, bytes]) -> None:
    """Create each ``name: content`` entry (text or bytes) directly under ``directory``."""
    for name, content in files.items():
        (directory / name).write_bytes(content if isinstance(content, bytes) else content.encode())


def _write_state(root: Path, relative: str, content: str) -> None:
    """Seed a versioned candidate or live release-state file."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    write_lf(path, content)


def _git(root: Path, *args: str) -> str:
    """Run git in ``root`` with a fixed identity and return its stripped stdout."""
    identity = ["-c", "user.name=Test", "-c", "user.email=test@example.invalid"]
    completed = subprocess.run(["git", *identity, *args], cwd=root, check=True, capture_output=True, text=True)
    return completed.stdout.strip()


def _head(root: Path) -> str:
    """Return the checked-out commit."""
    return _git(root, "rev-parse", "HEAD")


def _commit(root: Path, message: str = "later") -> None:
    """Move HEAD forward with an empty commit."""
    _git(root, "commit", "--allow-empty", "-qm", message)


def _history(root: Path, count: int) -> list[str]:
    """Add ``count`` content commits and return their shas, oldest first."""
    shas = []
    for index in range(count):
        write_lf(root / "source.txt", str(index))
        _git(root, "add", "source.txt")
        _git(root, "commit", "-qm", f"c{index}")
        shas.append(_head(root))
    return shas


#: Source changes that invalidate a finished Gather, keyed by the name tests use as a parameter.
_DRIFT: dict[str, Callable[[Path], object]] = {
    "new-commit": lambda root: _commit(root, "later"),
    "branch-switch": lambda root: _git(root, "switch", "-q", "-c", "other"),
    "new-tag": lambda root: _git(root, "tag", "v2"),
}

#: The one-commit repository ``_init_git`` copies, as ``{"root": Path, "head": str}``; written once per module.
_GIT_TEMPLATE: dict[str, object] = {}


@pytest.fixture(scope="module", autouse=True)
def _git_template(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Build the initial main-branch repository once per module for every ``_init_git`` call to copy.

    Creating it costs six git launches (init, two identity settings, add, commit, rev-parse), and nearly every test
    here starts from it; process creation is slow on Windows, so building it per test dominated setup there. Nothing
    ever writes to the template after this fixture: each test receives a private byte copy, so its commits, tags and
    branch switches stay in its own ``tmp_path``.
    """
    root = tmp_path_factory.mktemp("git-template")
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True, capture_output=True)
    (root / "source.txt").write_text("release source\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "add", "source.txt"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "source"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    _GIT_TEMPLATE["root"] = root
    _GIT_TEMPLATE["head"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()


def _init_git(tmp_path: Path) -> str:
    """Create a main-branch repository with a local test commit identity and return its HEAD.

    The repository is a private copy of the module's template, so it already holds one commit on ``main`` and the
    repository-local identity later commits rely on.
    """
    shutil.copytree(_GIT_TEMPLATE["root"], tmp_path, dirs_exist_ok=True)
    return str(_GIT_TEMPLATE["head"])


def _stage_marker(stage: Path) -> str:
    """Stage the marker for the candidate's HEAD, seal the candidate and return the marker text."""
    head_sha = json.loads(_read(stage / "candidate.json"))["head_sha"]
    _write_state(stage, _MAIN_MARKER, head_sha + "\n")
    publisher.seal(stage.parent.parent, "main", stage)
    return head_sha + "\n"


def _start(tmp_path: Path) -> Path:
    """Begin a staging directory over live DRAFT, CHANGELOG, provenance and marker files; return the stage."""
    _init_git(tmp_path)
    (tmp_path / ".temp").mkdir()
    write_lf(tmp_path / "DRAFT.md", _ORIGINAL_DRAFT)
    write_lf(tmp_path / "CHANGELOG.md", _ORIGINAL_CHANGELOG)
    _write_state(tmp_path, _MAIN_PROVENANCE, _OLD_PROVENANCE)
    _write_state(tmp_path, _MAIN_MARKER, _OLD_MARKER)
    return publisher.begin(tmp_path, "main", "CHANGELOG.md", range_start="HEAD")


def _staged(tmp_path: Path, draft: str = "candidate\n", changelog: str | None = None) -> tuple[Path, str]:
    """Begin a stage, write the candidate draft (and changelog), seal it and return ``(stage, marker_text)``."""
    stage = _start(tmp_path)
    write_lf(stage / "DRAFT.md", draft)
    if changelog is not None:
        write_lf(stage / "CHANGELOG.md", changelog)
    return stage, _stage_marker(stage)


def _plain_candidate(tmp_path: Path) -> tuple[Path, str, str, tuple[str, str]]:
    """Stage a plain range ending before HEAD with both dated output destinations."""
    old_marker = _init_git(tmp_path)
    _commit(tmp_path, "represented")
    marker_sha = _head(tmp_path)
    _commit(tmp_path, "later")
    (tmp_path / "DRAFT.md").write_bytes(b"ORIGINAL\r\n")
    (tmp_path / "CHANGELOG.md").write_bytes(b"# Changelog\r\n")
    _write_state(tmp_path, _MAIN_MARKER, old_marker + "\n")
    outputs = (
        ".temp/output-release-summary-main-2026-09-26.md",
        ".temp/output-release-migration-main-2026-09-26.md",
    )
    _write_state(tmp_path, outputs[0], "Prior summary\n")
    _write_state(tmp_path, outputs[1], "Prior migration\n")
    stage = publisher.begin(
        tmp_path, "main", "CHANGELOG.md", marker_sha=marker_sha, extra_outputs=outputs, range_start=old_marker
    )
    (stage / "DRAFT.md").write_bytes(b"COMPLETED\n")
    (stage / "CHANGELOG.md").write_bytes(b"# Changelog\nCompleted\n")
    _write_state(stage, outputs[0], "New summary\n")
    _write_state(stage, outputs[1], "New migration\n")
    _write_state(stage, _MAIN_MARKER, marker_sha + "\n")
    publisher.seal(tmp_path, "main", stage)
    return stage, old_marker, marker_sha, outputs


def _gathered_repo(tmp_path: Path) -> str:
    """Create live DRAFT, CHANGELOG and marker files as a finished Gather leaves them; return the frozen HEAD."""
    frozen_head = _init_git(tmp_path)
    (tmp_path / ".temp").mkdir()
    write_lf(tmp_path / "DRAFT.md", "original\n")
    write_lf(tmp_path / "CHANGELOG.md", "changelog\n")
    _write_state(tmp_path, _MAIN_MARKER, _OLD_MARKER)
    return frozen_head


def _interrupt_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: Path,
    fail_when: Callable[[int, str], bool],
    *,
    after_replace: bool = False,
    branch: str = "main",
) -> None:
    """Publish ``stage`` until ``fail_when(call_number, relative)`` holds, then restore the real replacement.

    The failure raises ``OSError("interrupted")`` before the matching replacement, or right after it when
    ``after_replace`` is set. On return the journal of the half-finished publication is on disk and the real
    ``_replace_candidate`` is back in place, so the caller can go straight to recovery.
    """
    real_replace = publisher._replace_candidate
    calls: list[str] = []

    def interrupted(root: Path, staged: Path, relative: str) -> None:
        """Fail at the requested replacement, otherwise delegate to the real one."""
        calls.append(relative)
        stop = fail_when(len(calls), relative)
        if stop and not after_replace:
            raise OSError("interrupted")
        real_replace(root, staged, relative)
        if stop:
            raise OSError("interrupted")

    monkeypatch.setattr(publisher, "_replace_candidate", interrupted)
    with pytest.raises(OSError, match="interrupted"):
        publisher.publish(tmp_path, branch, stage)
    monkeypatch.setattr(publisher, "_replace_candidate", real_replace)


def _on_replace(
    monkeypatch: pytest.MonkeyPatch,
    action: Callable[..., object],
    when: Callable[[Path, str], bool] = lambda staged, relative: True,
) -> None:
    """Run ``action(root, staged, relative)`` once, right after the first real replacement for which ``when`` holds."""
    real_replace = publisher._replace_candidate
    fired: list[bool] = []

    def wrapper(root: Path, staged: Path, relative: str) -> None:
        """Replace for real, then fire the action at the chosen boundary."""
        real_replace(root, staged, relative)
        if not fired and when(staged, relative):
            fired.append(True)
            action(root, staged, relative)

    monkeypatch.setattr(publisher, "_replace_candidate", wrapper)


def _fail_directory_fsync(
    monkeypatch: pytest.MonkeyPatch, message: str, armed: Callable[[], bool] = lambda: True
) -> None:
    """Make fsync of a directory raise ``OSError(message)`` while ``armed()`` holds; file fsyncs stay real."""
    real_fsync = os.fsync

    def fsync(fd: int) -> None:
        """Fail only the directory barrier."""
        if armed() and stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(message)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)


def _seed_sentinels(
    root: Path,
    *,
    setup: dict[str, str] | None = None,
    flat: dict[str, str] | None = None,
    tags: bytes | None = None,
    session: str = "test",
) -> None:
    """Plant the per-session sentinels the release shell blocks read back from ``TMPDIR``.

    ``setup`` entries land in ``release-setup-<session>/<NAME>``, ``flat`` entries in ``release-<name>-<session>``, and
    ``tags`` in the tag-state file ``release-append-tags-<session>``.
    """
    for name, value in (setup or {}).items():
        _write_state(root, f"release-setup-{session}/{name}", value + "\n")
    for name, value in (flat or {}).items():
        _write_state(root, f"release-{name}-{session}", value + "\n")
    if tags is not None:
        (root / f"release-append-tags-{session}").write_bytes(tags)


def _block_env(root: Path, session: str = "test", **extra: str) -> dict[str, str]:
    """Environment for a shipped shell block: session-scoped sentinels live in ``root``."""
    environment = os.environ.copy()
    environment.update(
        {"TMPDIR": str(root), "CLAUDE_CODE_SESSION_ID": session, "CLAUDE_PLUGIN_ROOT": str(_PLUGIN_ROOT), **extra}
    )
    return environment


def _run_block(root: Path, script: str, session: str = "test", **extra: str) -> subprocess.CompletedProcess[str]:
    """Run shell ``script`` in ``root`` with the release session environment."""
    return subprocess.run(
        [shutil.which("bash"), "-c", script],
        cwd=root,
        env=_block_env(root, session, **extra),
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def _block(document: Path, needle: str) -> str:
    """Return the first ``bash`` fence of ``document`` containing ``needle``."""
    found = next((block for block in bash_blocks(document.read_text(encoding="utf-8")) if needle in block), None)
    assert found is not None, f"no bash block in {document.name} contains {needle!r}"
    return found


def _gather_prefix(marker: str = "# quote RANGE") -> str:
    """Return the gather block of the skill up to its ``marker`` comment, i.e. only the range validation."""
    return _block(_SKILL, marker).split(marker, 1)[0]


def _cli(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the publisher script as the shipped skill does and capture its streams."""
    return subprocess.run([sys.executable, str(_SCRIPT), *args], cwd=root, capture_output=True, text=True, check=False)


class TestWindowsPortability:
    """Windows-only branches of the publisher, simulated on every host."""

    def test_file_sync_opens_writable_handle(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Syncing a file on Windows opens it read-write so FlushFileBuffers gets a writable handle.

        The platform is switched to ``win32`` while the real sync still runs, and the access mode is recorded.
        """
        candidate = tmp_path / "candidate.json"
        candidate.write_bytes(b"sealed\n")
        original_open = Path.open
        opened_modes: list[str] = []

        def record_open(path: Path, mode: str = "r", *args: object, **kwargs: object):
            """Record the access mode while preserving the real sync operation."""
            opened_modes.append(mode)
            return original_open(path, mode, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(publisher.sys, "platform", "win32")
            patch.setattr(Path, "open", record_open)
            publisher._sync_file(candidate)

        assert opened_modes == ["r+b"]
        assert candidate.read_bytes() == b"sealed\n"

    @mock.patch("subprocess.run", autospec=True)
    def test_stage_path_is_translated_for_git_bash(self, run: mock.Mock, monkeypatch: pytest.MonkeyPatch) -> None:
        """The stage path printed by native Python is converted with ``cygpath -u`` so Git Bash blocks can use it.

        ``cygpath`` itself is replaced; the contract is the exact command issued and the path handed back.
        """
        run.return_value = subprocess.CompletedProcess([], 0, "/c/Users/test/stage\n", "")
        monkeypatch.setattr(publisher.sys, "platform", "win32")

        assert publisher._shell_path(Path("C:\\Users\\test\\stage")) == "/c/Users/test/stage"
        assert run.call_args.args[0] == ["cygpath", "-u", "C:\\Users\\test\\stage"]

    @pytest.mark.parametrize(
        "unsafe", [r"\CHANGELOG.md", r"C:CHANGELOG.md", r"C:\release\CHANGELOG.md", r"\\server\share\CHANGELOG.md"]
    )
    @mock.patch.object(publisher, "_head_sha", autospec=True, side_effect=AssertionError("source was read"))
    def test_rooted_changelog_is_rejected_before_staging(
        self, head_sha: mock.Mock, tmp_path: Path, unsafe: str
    ) -> None:
        """A Windows rooted or drive-relative changelog path cannot escape the repository.

        Rejection must come before any git state is read or any staging directory is created.
        """
        assert PureWindowsPath(unsafe).root or PureWindowsPath(unsafe).drive
        _init_git(tmp_path)

        with pytest.raises(ValueError, match="unsafe artifact path"):
            publisher.begin(tmp_path, "main", unsafe, expected_start="old", range_start="HEAD")

        assert not (tmp_path / ".temp").exists()
        head_sha.assert_not_called()


class TestPublish:
    """``publish``: promote a sealed candidate exactly once, or leave every live file untouched."""

    def test_truth_abort_leaves_live_artifacts_unchanged(self, tmp_path: Path) -> None:
        """Writing an unapproved candidate into its stage changes no live file, marker or provenance, and no journal.

        Only publication may touch live files; an abandoned review must leave the working tree as it found it.
        """
        stage = _start(tmp_path)
        write_lf(stage / "DRAFT.md", "## Summary\nOriginal\n### Since last draft\nIncrement\n")
        write_lf(stage / "CHANGELOG.md", "# Changelog\nIncrement\n")
        _write_state(stage, _MAIN_PROVENANCE, _OLD_PROVENANCE + '{"schema_version":1,"patch_id":"new"}\n')

        assert _read(tmp_path / "DRAFT.md") == _ORIGINAL_DRAFT
        assert _read(tmp_path / "CHANGELOG.md") == _ORIGINAL_CHANGELOG
        assert _read(tmp_path / _MAIN_MARKER) == _OLD_MARKER
        assert _read(tmp_path / _MAIN_PROVENANCE) == _OLD_PROVENANCE
        assert not publisher.journal_path(tmp_path, "main").exists()

    def test_advances_marker_once_and_rejects_replay(self, tmp_path: Path) -> None:
        """A completed append publishes each artifact once and rejects replay of the same candidate.

        The second publish of an already-promoted stage must fail on the moved marker instead of appending again.
        """
        stage, expected_marker = _staged(tmp_path, "## Summary\nOriginal\n### Since last draft\nIncrement\n")

        publisher.publish(tmp_path, "main", stage)

        assert _read(tmp_path / "DRAFT.md").count("Increment") == 1
        assert _read(tmp_path / _MAIN_MARKER) == expected_marker
        with pytest.raises(ValueError, match="saved marker changed"):
            publisher.publish(tmp_path, "main", stage)

    def test_rejects_candidate_changed_after_truth_seal(self, tmp_path: Path) -> None:
        """Publication uses the exact bytes accepted by the final truth check.

        A candidate edited after sealing is refused and the live draft stays as it was.
        """
        stage, _ = _staged(tmp_path, "checked candidate\n")
        publisher.seal(tmp_path, "main", stage)
        write_lf(stage / "DRAFT.md", "unchecked candidate\n")

        with pytest.raises(ValueError, match="candidate changed after truth gate"):
            publisher.publish(tmp_path, "main", stage)

        assert _read(tmp_path / "DRAFT.md") == _ORIGINAL_DRAFT

    @pytest.mark.parametrize(
        "existing_files",
        [pytest.param({}, id="summary-absent"), pytest.param({"SUMMARY.md": "existing\n"}, id="summary-exists")],
    )
    def test_rejects_user_change_to_unchanged_candidate(self, tmp_path: Path, existing_files: dict[str, str]) -> None:
        """A changed or newly created live optional file cannot bypass candidate validation.

        SUMMARY.md is absent from the candidate; a user who creates or edits it between sealing and publishing must stop
        the publication, whether the file existed beforehand or not.
        """
        (tmp_path / ".temp").mkdir()
        write_lf(tmp_path / "DRAFT.md", "original\n")
        write_lf(tmp_path / "CHANGELOG.md", "changelog\n")
        _write_state(tmp_path, _MAIN_MARKER, _OLD_MARKER)
        _write_files(tmp_path, existing_files)
        _init_git(tmp_path)
        stage = publisher.begin(tmp_path, "main", "CHANGELOG.md", range_start="HEAD")
        write_lf(stage / "DRAFT.md", "candidate\n")
        _stage_marker(stage)
        write_lf(tmp_path / "SUMMARY.md", "user edit\n")

        with pytest.raises(ValueError, match="changed outside"):
            publisher.publish(tmp_path, "main", stage)

        assert _read(tmp_path / "DRAFT.md") == "original\n"
        assert _read(tmp_path / _MAIN_MARKER) == _OLD_MARKER


@pytest.fixture
def stale_journal_after_final_unlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, bytes]:
    """Publish fully, lose only the final journal removal, then bring the journal back after the source moved on.

    Returns ``(journal_path, marker_bytes)``. The publication itself succeeded; the failed directory sync after the last
    unlink is what makes a stale journal reappear, so recovery must not roll the finished notes back.
    """
    stage, marker_text = _staged(tmp_path, "COMPLETED\r\n")
    journal = publisher.journal_path(tmp_path, "main")
    journal_bytes: list[bytes] = []
    original_unlink = Path.unlink

    def record_unlink(path: Path, *args: object, **kwargs: object) -> None:
        """Capture the journal bytes that may reappear after a failed unlink sync."""
        if path == journal:
            journal_bytes.append(path.read_bytes())
        original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", record_unlink)
        _fail_directory_fsync(patch, "final journal directory sync failed", armed=lambda: bool(journal_bytes))
        with pytest.raises(OSError, match="final journal directory sync failed"):
            publisher.publish(tmp_path, "main", stage)

    assert journal_bytes
    assert not journal.exists()
    assert (tmp_path / "DRAFT.md").read_bytes() == b"COMPLETED\r\n"
    assert (tmp_path / _MAIN_MARKER).read_bytes() == marker_text.encode()
    journal.write_bytes(journal_bytes[0])
    _commit(tmp_path, "later source")
    return journal, marker_text.encode()


@_skip_directory_sync_unavailable
class TestDurabilityBarriers:
    """Directory fsync barriers around journal persistence, live promotion and journal removal."""

    def test_orders_journal_sync_before_live_replace_and_marker_before_journal_removal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Directory barriers order journal persistence before live promotion, and marker before journal removal.

        Replacements, unlinks and directory syncs are recorded in one timeline while the real operations still run.
        """
        stage, _ = _staged(tmp_path, "completed notes\n")
        journal = publisher.journal_path(tmp_path, "main")
        events: list[tuple[str, Path | None]] = []
        original_fsync = os.fsync
        original_replace = os.replace
        original_unlink = Path.unlink

        def record_fsync(fd: int) -> None:
            """Record directory barriers while retaining actual filesystem sync behavior."""
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                events.append(("dir-sync", None))
            original_fsync(fd)

        def record_replace(source: str | Path, target: str | Path) -> None:
            """Record each visible rename in publication order."""
            events.append(("replace", Path(target)))
            original_replace(source, target)

        def record_unlink(path: Path, *args: object, **kwargs: object) -> None:
            """Record the journal removal that ends crash recovery."""
            if path == journal:
                events.append(("journal-unlink", path))
            original_unlink(path, *args, **kwargs)

        monkeypatch.setattr(os, "fsync", record_fsync)
        monkeypatch.setattr(os, "replace", record_replace)
        monkeypatch.setattr(Path, "unlink", record_unlink)

        publisher.publish(tmp_path, "main", stage)

        journal_replace = events.index(("replace", journal))
        first_live_replace = events.index(("replace", tmp_path / "DRAFT.md"))
        marker_replace = events.index(("replace", tmp_path / _MAIN_MARKER))
        journal_unlink = events.index(("journal-unlink", journal))
        assert ("dir-sync", None) in events[journal_replace + 1 : first_live_replace]
        assert ("dir-sync", None) in events[marker_replace + 1 : journal_unlink]

    def test_journal_sync_failure_prevents_live_replacement(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed journal durability barrier stops promotion before touching user files."""
        stage, _ = _staged(tmp_path, "completed notes\n")
        _fail_directory_fsync(monkeypatch, "directory sync unavailable")

        with pytest.raises(OSError, match="directory sync unavailable"):
            publisher.publish(tmp_path, "main", stage)

        assert _read(tmp_path / "DRAFT.md") == _ORIGINAL_DRAFT
        assert _read(tmp_path / _MAIN_MARKER) == _OLD_MARKER

    def test_artifact_sync_failure_keeps_journal_and_stops_promotion(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed live rename barrier leaves the journal and blocks the next artifact.

        The barrier starts failing right after the draft rename, so the changelog and marker must stay untouched.
        """
        stage, _ = _staged(tmp_path, "completed notes\n")
        original_replace = os.replace
        replaced: list[Path] = []

        def record_replace(source: str | Path, target: str | Path) -> None:
            """Remember each completed rename so the barrier can fail after the draft one."""
            original_replace(source, target)
            replaced.append(Path(target))

        monkeypatch.setattr(os, "replace", record_replace)
        _fail_directory_fsync(
            monkeypatch, "artifact directory sync failed", armed=lambda: tmp_path / "DRAFT.md" in replaced
        )

        with pytest.raises(OSError, match="artifact directory sync failed"):
            publisher.publish(tmp_path, "main", stage)

        assert _read(tmp_path / "DRAFT.md") == "completed notes\n"
        assert _read(tmp_path / "CHANGELOG.md") == _ORIGINAL_CHANGELOG
        assert _read(tmp_path / _MAIN_MARKER) == _OLD_MARKER
        assert publisher.journal_path(tmp_path, "main").exists()

    def test_recovery_after_lost_final_unlink_keeps_completed_notes(
        self, tmp_path: Path, stale_journal_after_final_unlink: tuple[Path, bytes]
    ) -> None:
        """A stale journal cannot roll back complete notes after the source advanced.

        The notes were fully published; recovering the resurrected journal only clears it.
        """
        journal, marker_bytes = stale_journal_after_final_unlink

        publisher.recover(tmp_path, "main")

        assert (tmp_path / "DRAFT.md").read_bytes() == b"COMPLETED\r\n"
        assert not journal.exists()
        assert (tmp_path / _MAIN_MARKER).read_bytes() == marker_bytes

    def test_recovery_after_lost_final_unlink_refuses_foreign_edit(
        self, tmp_path: Path, stale_journal_after_final_unlink: tuple[Path, bytes]
    ) -> None:
        """A user edit made after the lost journal removal stops recovery without being overwritten."""
        journal, marker_bytes = stale_journal_after_final_unlink
        (tmp_path / "DRAFT.md").write_bytes(b"USER EDIT\r\n")

        with pytest.raises(ValueError, match="artifact changed outside append publication"):
            publisher.recover(tmp_path, "main")

        assert (tmp_path / "DRAFT.md").read_bytes() == b"USER EDIT\r\n"
        assert journal.exists()
        assert (tmp_path / _MAIN_MARKER).read_bytes() == marker_bytes


@dataclasses.dataclass(frozen=True)
class _AdvancedMarker:
    """A publication interrupted before any replacement whose marker was then advanced in live state by hand."""

    ancestor: str
    frozen_head: str
    marker_candidate: str
    journal: Path
    draft: Path


@pytest.fixture
def advanced_marker_after_interrupted_publish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _AdvancedMarker:
    """Stage a candidate whose marker was written to live state after an interrupted publish."""
    ancestor = _init_git(tmp_path)
    _commit(tmp_path, "release")
    frozen_head = _head(tmp_path)
    assert ancestor != frozen_head
    (tmp_path / ".temp").mkdir()
    draft = tmp_path / "DRAFT.md"
    write_lf(draft, "original\n")
    write_lf(tmp_path / "CHANGELOG.md", "changelog\n")
    _write_state(tmp_path, _MAIN_MARKER, _OLD_MARKER)
    stage = publisher.begin(tmp_path, "main", "CHANGELOG.md", range_start=ancestor)
    write_lf(stage / "DRAFT.md", "candidate\n")
    marker_candidate = _stage_marker(stage)
    _interrupt_publish(tmp_path, monkeypatch, stage, lambda call, relative: True)
    journal = publisher.journal_path(tmp_path, "main")
    assert journal.exists()
    write_lf(tmp_path / _MAIN_MARKER, marker_candidate)
    return _AdvancedMarker(ancestor, frozen_head, marker_candidate, journal, draft)


class TestRecovery:
    """``recover``: finish, roll back or refuse an interrupted publication without losing user bytes."""

    def test_finishes_validated_candidate_after_interrupted_replacement(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Recovery finishes the validated candidate after an interrupted replacement, without replaying the summary.

        The second replacement fails, so the draft is already live when recovery runs; its text must not be appended a
        second time.
        """
        candidate = "## Summary\nOriginal\n### Since last draft\nIncrement\n"
        stage, expected_marker = _staged(tmp_path, candidate, changelog="# Changelog\nIncrement\n")
        _interrupt_publish(tmp_path, monkeypatch, stage, lambda call, relative: call == 2)
        assert publisher.journal_path(tmp_path, "main").exists()

        publisher.recover(tmp_path, "main")

        assert _read(tmp_path / "DRAFT.md") == candidate
        assert _read(tmp_path / "DRAFT.md").count("Increment") == 1
        assert _read(tmp_path / _MAIN_MARKER) == expected_marker
        assert not publisher.journal_path(tmp_path, "main").exists()

    def test_accepts_marker_already_published(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A crash after marker replacement can clear its journal without replaying anything."""
        stage, expected_marker = _staged(tmp_path)
        _interrupt_publish(
            tmp_path, monkeypatch, stage, lambda call, relative: relative == _MAIN_MARKER, after_replace=True
        )
        assert publisher.journal_path(tmp_path, "main").exists()

        publisher.recover(tmp_path, "main")

        assert _read(tmp_path / "DRAFT.md") == "candidate\n"
        assert _read(tmp_path / _MAIN_MARKER) == expected_marker
        assert not publisher.journal_path(tmp_path, "main").exists()

    @pytest.mark.parametrize("interrupted_call", [1, 2])
    def test_user_edit_after_interruption_blocks_recovery(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interrupted_call: int
    ) -> None:
        """A user edit made after the interruption is never replaced by the candidate.

        The publication stops before the first or the second replacement; either way the edited draft must survive,
        because recovery preflights every remaining target before replacing any of them.
        """
        stage, _ = _staged(tmp_path, "candidate draft\n", changelog="candidate changelog\n")
        _interrupt_publish(tmp_path, monkeypatch, stage, lambda call, relative: call == interrupted_call)
        write_lf(tmp_path / "DRAFT.md", "user edit\n")

        with pytest.raises(ValueError, match="changed outside"):
            publisher.recover(tmp_path, "main")

        assert _read(tmp_path / "DRAFT.md") == "user edit\n"
        assert _read(tmp_path / _MAIN_MARKER) == _OLD_MARKER

    def test_rejects_change_to_path_omitted_from_journal(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Recovery also inventories paths omitted from the changed-file journal."""
        stage, _ = _staged(tmp_path)
        _interrupt_publish(tmp_path, monkeypatch, stage, lambda call, relative: True)
        write_lf(tmp_path / "SUMMARY.md", "user edit\n")

        with pytest.raises(ValueError, match="changed outside"):
            publisher.recover(tmp_path, "main")

        assert _read(tmp_path / _MAIN_MARKER) == _OLD_MARKER

    def test_rejects_journal_missing_changed_artifact(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A shortened journal cannot mark an unpromoted draft as processed."""
        stage, _ = _staged(tmp_path)
        _interrupt_publish(tmp_path, monkeypatch, stage, lambda call, relative: True)
        journal_file = publisher.journal_path(tmp_path, "main")
        journal = json.loads(_read(journal_file))
        journal["entries"] = [entry for entry in journal["entries"] if entry["path"] != "DRAFT.md"]
        write_lf(journal_file, json.dumps(journal))

        with pytest.raises(ValueError, match=r"journal.*candidate"):
            publisher.recover(tmp_path, "main")

        assert _read(tmp_path / "DRAFT.md") == _ORIGINAL_DRAFT
        assert _read(tmp_path / _MAIN_MARKER) == _OLD_MARKER

    @pytest.mark.parametrize("extra_path", ["SECRET.md", "DRAFT.md"])
    def test_malformed_journal_cannot_promote_unlisted_or_duplicate_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra_path: str
    ) -> None:
        """Recovery rejects extra or duplicate journal targets before touching remaining files.

        A tampered journal naming an unrelated user file (or the draft twice) must never be able to overwrite it.
        """
        stage, _ = _staged(tmp_path)
        write_lf(tmp_path / "SECRET.md", "user bytes\n")
        _interrupt_publish(tmp_path, monkeypatch, stage, lambda call, relative: True)
        journal_file = publisher.journal_path(tmp_path, "main")
        journal = json.loads(_read(journal_file))
        journal["entries"].insert(0, {"path": extra_path, "old": None, "new": "fake"})
        write_lf(journal_file, json.dumps(journal))

        with pytest.raises(ValueError, match="duplicate or unexpected"):
            publisher.recover(tmp_path, "main")

        assert _read(tmp_path / "SECRET.md") == "user bytes\n"
        assert _read(tmp_path / "DRAFT.md") == _ORIGINAL_DRAFT
        assert _read(tmp_path / _MAIN_MARKER) == _OLD_MARKER

    def test_succeeds_when_marker_advanced_and_source_unchanged(
        self, tmp_path: Path, advanced_marker_after_interrupted_publish: _AdvancedMarker
    ) -> None:
        """An externally advanced marker still recovers cleanly when the source has not drifted."""
        state = advanced_marker_after_interrupted_publish
        assert _head(tmp_path) == state.frozen_head

        publisher.recover(tmp_path, "main")

        assert _read(state.draft) == "candidate\n"
        assert not state.journal.exists()
        assert _read(tmp_path / _MAIN_MARKER) == state.marker_candidate

    def test_rejects_branch_switch_after_marker_advanced(
        self, tmp_path: Path, advanced_marker_after_interrupted_publish: _AdvancedMarker
    ) -> None:
        """A branch switch after the marker was externally advanced still blocks recovery."""
        state = advanced_marker_after_interrupted_publish
        _git(tmp_path, "switch", "-q", "-c", "other")
        assert _head(tmp_path) == state.frozen_head

        with pytest.raises(ValueError, match=r"branch changed|baseline tags changed"):
            publisher.recover(tmp_path, "main")

        assert _read(state.draft) == "original\n"
        assert state.journal.exists()
        assert _read(tmp_path / _MAIN_MARKER) == state.marker_candidate

    def test_rejects_ancestor_tag_after_marker_advanced(
        self, tmp_path: Path, advanced_marker_after_interrupted_publish: _AdvancedMarker
    ) -> None:
        """A tag cut on the old ancestor after the marker was externally advanced still blocks recovery."""
        state = advanced_marker_after_interrupted_publish
        _git(tmp_path, "tag", "v2", state.ancestor)
        assert _head(tmp_path) == state.frozen_head

        with pytest.raises(ValueError, match=r"branch changed|baseline tags changed"):
            publisher.recover(tmp_path, "main")

        assert _read(state.draft) == "original\n"
        assert not state.journal.exists()
        assert _read(tmp_path / _MAIN_MARKER) == _OLD_MARKER

    def test_persists_rollback_choice_before_rejecting_foreign_edit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Source drift commits to rollback even when a user edit fails the first preflight.

        After the foreign edit is put back to the candidate bytes, a second recovery must finish the rollback.
        """
        stage, _ = _staged(tmp_path)
        _interrupt_publish(
            tmp_path, monkeypatch, stage, lambda call, relative: relative == "DRAFT.md", after_replace=True
        )
        draft = tmp_path / "DRAFT.md"
        draft.write_bytes(b"foreign edit\r\n")
        _commit(tmp_path, "later")
        journal = publisher.journal_path(tmp_path, "main")

        with pytest.raises(ValueError, match="changed outside append publication"):
            publisher.recover(tmp_path, "main")

        assert json.loads(_read(journal))["mode"] == "rollback"
        assert draft.read_bytes() == b"foreign edit\r\n"
        assert _read(tmp_path / _MAIN_MARKER) == _OLD_MARKER
        write_lf(draft, "candidate\n")
        publisher.recover(tmp_path, "main")
        assert _read(draft) == _ORIGINAL_DRAFT
        assert not journal.exists()

    def test_uses_raw_branch_identity_when_legacy_slugs_collide(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A colliding sibling branch cannot consume another branch's pending journal.

        ``feature/append`` and ``feature-append`` share a legacy slug; a copy of the journal under the sibling's name
        must be refused, and only the branch that owns it may finish it.
        """
        _init_git(tmp_path)
        _git(tmp_path, "branch", "-M", "feature/append")
        (tmp_path / ".temp").mkdir()
        write_lf(tmp_path / "DRAFT.md", "original\n")
        write_lf(tmp_path / "CHANGELOG.md", "changelog\n")
        stage = publisher.begin(tmp_path, "feature-append", "CHANGELOG.md", range_start="HEAD")
        write_lf(stage / "DRAFT.md", "candidate\n")
        head_sha = json.loads(_read(stage / "candidate.json"))["head_sha"]
        staged_marker = stage / ".temp" / "release-state-v2" / marker_api.branch_state_key("feature/append") / "marker"
        staged_marker.parent.mkdir(parents=True, exist_ok=True)
        write_lf(staged_marker, head_sha + "\n")
        publisher.seal(tmp_path, "feature-append", stage)
        _interrupt_publish(tmp_path, monkeypatch, stage, lambda call, relative: True, branch="feature-append")
        journal = publisher.journal_path(tmp_path, "feature/append")
        assert journal.exists()
        _git(tmp_path, "switch", "-q", "-c", "feature-append")

        with pytest.raises(FileNotFoundError):
            publisher.recover(tmp_path, "feature-append")
        sibling_journal = publisher.journal_path(tmp_path, "feature-append")
        sibling_journal.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(journal, sibling_journal)
        with pytest.raises(ValueError, match="branch changed"):
            publisher.recover(tmp_path, "feature-append")
        assert journal.exists()
        assert _read(tmp_path / "DRAFT.md") == "original\n"

        _git(tmp_path, "switch", "-q", "feature/append")
        publisher.recover(tmp_path, "feature-append")
        assert _read(tmp_path / "DRAFT.md") == "candidate\n"
        assert not journal.exists()


_SOURCE_MOVES = [
    pytest.param("new-commit", "HEAD changed after release gather", id="new-commit"),
    pytest.param("branch-switch", "branch changed after release gather", id="branch-switch"),
]


class TestSourceDrift:
    """Publication and its shell gates must notice HEAD, branch or tag moving after Gather."""

    @pytest.mark.parametrize(("drift", "message"), _SOURCE_MOVES)
    def test_begin_rejects_source_moved_since_gather(self, tmp_path: Path, drift: str, message: str) -> None:
        """The staging input must be the same HEAD and branch frozen during Gather."""
        frozen_head = _gathered_repo(tmp_path)
        _DRIFT[drift](tmp_path)

        with pytest.raises(ValueError, match=message):
            publisher.begin(tmp_path, "main", "CHANGELOG.md", frozen_head, range_start="HEAD")

        assert _read(tmp_path / "DRAFT.md") == "original\n"
        assert _read(tmp_path / _MAIN_MARKER) == _OLD_MARKER

    @pytest.mark.parametrize(("drift", "message"), _SOURCE_MOVES)
    def test_publish_rejects_source_moved_since_gather(self, tmp_path: Path, drift: str, message: str) -> None:
        """Publication cannot certify a newer HEAD, or another branch's marker, than the classified commit set.

        A branch switch that keeps the same commit would otherwise publish under a marker that belongs elsewhere.
        """
        frozen_head = _gathered_repo(tmp_path)
        stage = publisher.begin(tmp_path, "main", "CHANGELOG.md", frozen_head, range_start="HEAD")
        write_lf(stage / "DRAFT.md", "candidate\n")
        _stage_marker(stage)
        _DRIFT[drift](tmp_path)

        with pytest.raises(ValueError, match=message):
            publisher.publish(tmp_path, "main", stage)

        assert _read(tmp_path / "DRAFT.md") == "original\n"
        assert _read(tmp_path / _MAIN_MARKER) == _OLD_MARKER

    @pytest.mark.parametrize("drift", ["branch-switch", "new-tag"])
    def test_change_during_promotion_blocks_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
    ) -> None:
        """The final marker transition rechecks branch and tag refs after artifact writes.

        The ref moves right after the draft is promoted; the draft must be rolled back and no journal left behind.
        """
        stage, _ = _staged(tmp_path)
        _on_replace(
            monkeypatch,
            lambda root, *_: _DRIFT[drift](root),
            when=lambda staged, relative: staged == stage and relative == "DRAFT.md",
        )

        with pytest.raises(ValueError, match=r"branch changed|baseline tags changed"):
            publisher.publish(tmp_path, "main", stage)

        assert _read(tmp_path / "DRAFT.md") == _ORIGINAL_DRAFT
        assert _read(tmp_path / _MAIN_MARKER) == _OLD_MARKER
        assert not publisher.journal_path(tmp_path, "main").exists()

    def test_new_commit_during_promotion_restores_originals(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A moving HEAD cannot become the marker after earlier files were published.

        HEAD moves after the first replacement; the originals come back and the finished rollback leaves nothing to
        recover.
        """
        stage, _ = _staged(tmp_path)
        _on_replace(monkeypatch, lambda root, *_: _commit(root, "later"))

        with pytest.raises(ValueError, match="HEAD changed after release gather"):
            publisher.publish(tmp_path, "main", stage)

        assert not publisher.journal_path(tmp_path, "main").exists()
        assert _read(tmp_path / "DRAFT.md") == _ORIGINAL_DRAFT
        assert _read(tmp_path / _MAIN_MARKER) == _OLD_MARKER
        with pytest.raises(FileNotFoundError):
            publisher.recover(tmp_path, "main")
        assert _read(tmp_path / _MAIN_MARKER) == _OLD_MARKER

    def test_drift_after_marker_replacement_restores_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The final source check can undo a marker already replaced by this transaction."""
        stage, _ = _staged(tmp_path)
        _on_replace(
            monkeypatch,
            lambda root, *_: _commit(root, "later"),
            when=lambda staged, relative: staged == stage and relative == _MAIN_MARKER,
        )

        with pytest.raises(ValueError, match="HEAD changed after release gather"):
            publisher.publish(tmp_path, "main", stage)

        assert _read(tmp_path / "DRAFT.md") == _ORIGINAL_DRAFT
        assert _read(tmp_path / _MAIN_MARKER) == _OLD_MARKER
        assert not publisher.journal_path(tmp_path, "main").exists()

    def test_rollback_resumes_after_interruption(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A crash during rollback keeps its decision and restores the remaining original bytes on retry.

        HEAD moves after the draft is promoted and the rollback itself crashes after restoring the draft only.
        """
        frozen_head = _init_git(tmp_path)
        (tmp_path / ".temp").mkdir()
        (tmp_path / "DRAFT.md").write_bytes(b"original draft\r\n")
        (tmp_path / "CHANGELOG.md").write_bytes(b"original changelog\r\n")
        stage = publisher.begin(tmp_path, "main", "CHANGELOG.md", expected_head=frozen_head, range_start="HEAD")
        (stage / "DRAFT.md").write_bytes(b"candidate draft\n")
        (stage / "CHANGELOG.md").write_bytes(b"candidate changelog\n")
        _write_state(stage, _MAIN_MARKER, frozen_head + "\n")
        publisher.seal(tmp_path, "main", stage)
        real_replace = publisher._replace_candidate
        _on_replace(
            monkeypatch,
            lambda root, *_: _commit(root, "later"),
            when=lambda staged, relative: staged == stage and relative == "DRAFT.md",
        )
        _on_replace(
            monkeypatch,
            mock.Mock(side_effect=OSError("rollback interrupted")),
            when=lambda staged, relative: staged == stage / "originals" and relative == "DRAFT.md",
        )
        with pytest.raises(OSError, match="rollback interrupted"):
            publisher.publish(tmp_path, "main", stage)
        journal = publisher.journal_path(tmp_path, "main")
        assert json.loads(_read(journal))["mode"] == "rollback"
        assert (tmp_path / "DRAFT.md").read_bytes() == b"original draft\r\n"
        assert (tmp_path / "CHANGELOG.md").read_bytes() == b"candidate changelog\n"
        assert not (tmp_path / _MAIN_MARKER).exists()
        monkeypatch.setattr(publisher, "_replace_candidate", real_replace)

        publisher.recover(tmp_path, "main")

        assert (tmp_path / "DRAFT.md").read_bytes() == b"original draft\r\n"
        assert (tmp_path / "CHANGELOG.md").read_bytes() == b"original changelog\r\n"
        assert not journal.exists()
        assert not (tmp_path / _MAIN_MARKER).exists()

    def test_rollback_preserves_foreign_edit(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Rollback refuses an artifact changed by a user after the candidate replacement.

        The refusal must keep the rollback journal; once the user's text is gone a retry restores the original.
        """
        frozen_head = _init_git(tmp_path)
        (tmp_path / ".temp").mkdir()
        changelog = tmp_path / "CHANGELOG.md"
        changelog.write_bytes(b"original changelog\r\n")
        stage = publisher.begin(tmp_path, "main", "CHANGELOG.md", expected_head=frozen_head, range_start="HEAD")
        (stage / "CHANGELOG.md").write_bytes(b"candidate changelog\n")
        _write_state(stage, _MAIN_MARKER, frozen_head + "\n")
        publisher.seal(tmp_path, "main", stage)
        real_replace = publisher._replace_candidate

        def drift_and_edit(root: Path, *_: object) -> None:
            """Introduce source drift and an unrelated user edit at the promotion boundary."""
            _commit(root, "later")
            changelog.write_bytes(b"user edit\r\n")

        _on_replace(
            monkeypatch, drift_and_edit, when=lambda staged, relative: staged == stage and relative == "CHANGELOG.md"
        )
        with pytest.raises(ValueError, match="changed outside append publication"):
            publisher.publish(tmp_path, "main", stage)
        journal = publisher.journal_path(tmp_path, "main")
        assert json.loads(_read(journal))["mode"] == "rollback"
        assert changelog.read_bytes() == b"user edit\r\n"
        monkeypatch.setattr(publisher, "_replace_candidate", real_replace)
        with pytest.raises(ValueError, match="changed outside append publication"):
            publisher.recover(tmp_path, "main")
        assert changelog.read_bytes() == b"user edit\r\n"
        assert journal.exists()
        changelog.write_bytes(b"candidate changelog\n")

        publisher.recover(tmp_path, "main")

        assert changelog.read_bytes() == b"original changelog\r\n"
        assert not journal.exists()

    @pytest.mark.parametrize(
        ("drift", "error", "draft_after", "journal_remains"),
        [
            pytest.param("branch-switch", "branch changed", "candidate\n", True, id="branch-switch"),
            pytest.param("new-tag", "baseline tags changed", _ORIGINAL_DRAFT, False, id="new-tag"),
        ],
    )
    def test_recover_cli_rejects_source_drift_before_marker(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        drift: str,
        error: str,
        draft_after: str,
        journal_remains: bool,
    ) -> None:
        """The recover command refuses to finish a partial publication when the release source changed.

        The draft is already live and the marker still old; a moved branch keeps the journal for a later retry, while a
        new baseline tag has already rolled the draft back.
        """
        stage, _ = _staged(tmp_path)
        _interrupt_publish(tmp_path, monkeypatch, stage, lambda call, relative: relative != "DRAFT.md")
        frozen_head = _head(tmp_path)
        _DRIFT[drift](tmp_path)
        assert _head(tmp_path) == frozen_head

        completed = _cli(tmp_path, "recover", "--branch", "main")

        assert completed.returncode == 1
        assert error in completed.stderr
        assert _read(tmp_path / "DRAFT.md") == draft_after
        assert _read(tmp_path / _MAIN_MARKER) == _OLD_MARKER
        assert publisher.journal_path(tmp_path, "main").exists() is journal_remains

    def test_promoted_artifact_edit_after_marker_blocks_journal_clear(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An edit made right after the marker replacement cannot be certified or forgotten."""
        stage, _ = _staged(tmp_path, "candidate draft\n", changelog="candidate changelog\n")
        _on_replace(
            monkeypatch,
            lambda root, *_: write_lf(root / "DRAFT.md", "user edit\n"),
            when=lambda staged, relative: relative == _MAIN_MARKER,
        )

        with pytest.raises(ValueError, match="changed outside append publication"):
            publisher.publish(tmp_path, "main", stage)

        assert _read(tmp_path / "DRAFT.md") == "user edit\n"
        assert publisher.journal_path(tmp_path, "main").exists()

    def test_promoted_artifact_edit_before_marker_blocks_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An edit made right after the first replacement keeps the marker old and the journal pending."""
        stage, _ = _staged(tmp_path, "candidate draft\n", changelog="candidate changelog\n")
        _on_replace(monkeypatch, lambda root, *_: write_lf(root / "DRAFT.md", "user edit\n"))

        with pytest.raises(ValueError, match="changed outside append publication"):
            publisher.publish(tmp_path, "main", stage)

        assert _read(tmp_path / "DRAFT.md") == "user edit\n"
        assert publisher.journal_path(tmp_path, "main").exists()
        assert _read(tmp_path / _MAIN_MARKER) == _OLD_MARKER


class TestPlainCandidate:
    """Plain (non-append) notes: draft, changelog, dated outputs and marker share one recoverable candidate."""

    @pytest.mark.parametrize(
        ("after_marker", "live_marker"),
        [
            pytest.param(False, "old", id="interrupted-before-marker"),
            pytest.param(True, "new", id="interrupted-after-marker"),
        ],
    )
    def test_recovers_exact_artifacts_without_replaying_notes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, after_marker: bool, live_marker: str
    ) -> None:
        """A plain candidate publishes once even when interrupted just before or just after its marker.

        Recovery must restore the exact staged bytes of all four artifacts and then refuse to run a second time.
        """
        stage, old_marker, marker_sha, outputs = _plain_candidate(tmp_path)
        _interrupt_publish(
            tmp_path, monkeypatch, stage, lambda call, relative: relative == _MAIN_MARKER, after_replace=after_marker
        )
        assert publisher.journal_path(tmp_path, "main").exists()
        assert _read(tmp_path / _MAIN_MARKER) == {"old": old_marker, "new": marker_sha}[live_marker] + "\n"

        publisher.recover(tmp_path, "main")

        assert (tmp_path / "DRAFT.md").read_bytes() == b"COMPLETED\n"
        assert (tmp_path / "CHANGELOG.md").read_bytes() == b"# Changelog\nCompleted\n"
        assert (tmp_path / outputs[0]).read_bytes() == b"New summary\n"
        assert (tmp_path / outputs[1]).read_bytes() == b"New migration\n"
        assert (tmp_path / _MAIN_MARKER).read_bytes() == (marker_sha + "\n").encode()
        assert not publisher.journal_path(tmp_path, "main").exists()
        with pytest.raises(FileNotFoundError):
            publisher.recover(tmp_path, "main")

    def test_recovery_preserves_foreign_output_edit_and_pending_journal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A user edit to a dated output stops recovery without overwriting its bytes."""
        stage, old_marker, _, outputs = _plain_candidate(tmp_path)
        _interrupt_publish(tmp_path, monkeypatch, stage, lambda call, relative: relative == _MAIN_MARKER)
        foreign = b"USER CHANGE\r\n"
        (tmp_path / outputs[0]).write_bytes(foreign)

        with pytest.raises(ValueError, match="artifact changed outside append publication"):
            publisher.recover(tmp_path, "main")

        assert (tmp_path / outputs[0]).read_bytes() == foreign
        assert (tmp_path / _MAIN_MARKER).read_bytes() == (old_marker + "\n").encode()
        assert publisher.journal_path(tmp_path, "main").exists()

    def test_revision_publishes_with_unchanged_completed_marker(self, tmp_path: Path) -> None:
        """Revising already drafted prose preserves the same completed range baseline."""
        stage, _, marker_sha, outputs = _plain_candidate(tmp_path)
        publisher.publish(tmp_path, "main", stage)
        revision = publisher.begin(
            tmp_path, "main", "CHANGELOG.md", marker_sha=marker_sha, extra_outputs=outputs, range_start=marker_sha
        )
        (revision / "DRAFT.md").write_bytes(b"REVISED COMPLETED NOTES\n")
        publisher.seal(tmp_path, "main", revision)

        publisher.publish(tmp_path, "main", revision)

        assert (tmp_path / "DRAFT.md").read_bytes() == b"REVISED COMPLETED NOTES\n"
        assert (tmp_path / _MAIN_MARKER).read_bytes() == (marker_sha + "\n").encode()
        assert not publisher.journal_path(tmp_path, "main").exists()

    def test_source_drift_rolls_back_exact_original_bytes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A commit during plain promotion restores the originals byte for byte instead of certifying stale notes."""
        stage, old_marker, _, outputs = _plain_candidate(tmp_path)
        _on_replace(monkeypatch, lambda root, *_: _commit(root, "later still"))

        with pytest.raises(publisher.SourceDriftError):
            publisher.publish(tmp_path, "main", stage)

        assert (tmp_path / "DRAFT.md").read_bytes() == b"ORIGINAL\r\n"
        assert (tmp_path / "CHANGELOG.md").read_bytes() == b"# Changelog\r\n"
        assert (tmp_path / outputs[0]).read_bytes() == b"Prior summary\n"
        assert (tmp_path / outputs[1]).read_bytes() == b"Prior migration\n"
        assert (tmp_path / _MAIN_MARKER).read_bytes() == (old_marker + "\n").encode()
        assert not publisher.journal_path(tmp_path, "main").exists()

    def test_publish_refuses_foreign_dated_output_before_first_live_write(self, tmp_path: Path) -> None:
        """A user edit between staging and publication survives without any partial promotion."""
        stage, old_marker, _, outputs = _plain_candidate(tmp_path)
        foreign = b"USER SUMMARY\r\n"
        (tmp_path / outputs[0]).write_bytes(foreign)

        with pytest.raises(ValueError, match="artifact changed outside append staging"):
            publisher.publish(tmp_path, "main", stage)

        assert (tmp_path / "DRAFT.md").read_bytes() == b"ORIGINAL\r\n"
        assert (tmp_path / outputs[0]).read_bytes() == foreign
        assert (tmp_path / _MAIN_MARKER).read_bytes() == (old_marker + "\n").encode()
        assert not publisher.journal_path(tmp_path, "main").exists()


@pytest.fixture
def first_append_repo(tmp_path: Path) -> str:
    """Build a repository for a first append with live DRAFT and CHANGELOG but no marker; return the frozen HEAD.

    The gather sentinels are planted exactly as Gather leaves them for an append without a saved marker.
    """
    frozen_head = _init_git(tmp_path)
    (tmp_path / "DRAFT.md").write_bytes(b"user draft\r\n")
    (tmp_path / "CHANGELOG.md").write_bytes(b"user changelog\r\n")
    _seed_sentinels(
        tmp_path,
        setup={**_MAIN_SETUP, "LAST_TAG": ""},
        flat={
            "do-append": "true",
            "mode": "notes",
            "append-head": frozen_head,
            "append-start": frozen_head,
            "append-end": frozen_head,
            "append-branch": "main",
        },
        tags=b"",
    )
    return frozen_head


@pytest.fixture
def sealed_fallback_candidate(tmp_path: Path) -> tuple[Path, str]:
    """Seal an invalid-marker candidate over CRLF user files; return ``(stage, frozen_head)``."""
    frozen_head = _init_git(tmp_path)
    (tmp_path / ".temp").mkdir()
    (tmp_path / "DRAFT.md").write_bytes(b"user draft\r\n")
    (tmp_path / "CHANGELOG.md").write_bytes(b"user changelog\r\n")
    stage = publisher.begin(
        tmp_path, "main", "CHANGELOG.md", expected_head=frozen_head, expected_branch_ref="main", range_start="HEAD"
    )
    (stage / "DRAFT.md").write_bytes(b"candidate draft\n")
    (stage / "CHANGELOG.md").write_bytes(b"candidate changelog\n")
    _write_state(stage, _MAIN_MARKER, frozen_head + "\n")
    publisher.seal(tmp_path, "main", stage)
    return stage, frozen_head


class TestInvalidMarkerFallback:
    """Append without a usable saved marker: a frozen candidate is published, or every user byte is preserved."""

    def test_publishes_only_frozen_candidate(self, tmp_path: Path, sealed_fallback_candidate: tuple[Path, str]) -> None:
        """With the source unchanged, the sealed candidate replaces both files and sets the marker."""
        stage, frozen_head = sealed_fallback_candidate

        publisher.publish(tmp_path, "main", stage)

        assert (tmp_path / "DRAFT.md").read_bytes() == b"candidate draft\n"
        assert (tmp_path / "CHANGELOG.md").read_bytes() == b"candidate changelog\n"
        assert _read(tmp_path / _MAIN_MARKER) == frozen_head + "\n"

    @pytest.mark.parametrize("drift", ["new-commit", "branch-switch", "new-tag"])
    def test_keeps_user_bytes_when_source_moves_after_sealing(
        self, tmp_path: Path, sealed_fallback_candidate: tuple[Path, str], drift: str
    ) -> None:
        """Final publication preserves all user bytes if the source moves after sealing."""
        stage, _ = sealed_fallback_candidate
        _DRIFT[drift](tmp_path)

        with pytest.raises(ValueError, match=r"changed after (release )?gather"):
            publisher.publish(tmp_path, "main", stage)

        assert (tmp_path / "DRAFT.md").read_bytes() == b"user draft\r\n"
        assert (tmp_path / "CHANGELOG.md").read_bytes() == b"user changelog\r\n"
        assert not (tmp_path / _MAIN_MARKER).exists()

    def test_restores_originals_when_source_moves_during_promotion(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sealed_fallback_candidate: tuple[Path, str]
    ) -> None:
        """HEAD moving between two journaled replacements restores the user files from the stage's originals."""
        stage, _ = sealed_fallback_candidate
        _on_replace(
            monkeypatch,
            lambda root, *_: _commit(root, "later"),
            when=lambda staged, relative: relative == "CHANGELOG.md",
        )

        with pytest.raises(ValueError, match=r"changed after (release )?gather"):
            publisher.publish(tmp_path, "main", stage)

        assert not publisher.journal_path(tmp_path, "main").exists()
        assert (stage / "originals/DRAFT.md").read_bytes() == b"user draft\r\n"
        assert (stage / "originals/CHANGELOG.md").read_bytes() == b"user changelog\r\n"
        assert (tmp_path / "DRAFT.md").read_bytes() == b"user draft\r\n"
        assert (tmp_path / "CHANGELOG.md").read_bytes() == b"user changelog\r\n"
        assert not (tmp_path / _MAIN_MARKER).exists()

    @_skip_bash_git_unavailable
    def test_changelog_gate_rejects_moved_source(self, tmp_path: Path, first_append_repo: str) -> None:
        """A changed source stops the invalid-marker changelog path before any live edit."""
        _commit(tmp_path, "later")
        prewrite = _block(_SKILL, 'EDIT_CHANGELOG_FILE="$CHANGELOG_FILE"')

        completed = _run_block(
            tmp_path, prewrite + '\nprintf "replacement\\n" > CHANGELOG.md\n', CHANGELOG_FILE="CHANGELOG.md"
        )

        assert completed.returncode != 0, completed.stdout
        assert "HEAD changed after release gather" in completed.stderr
        assert (tmp_path / "CHANGELOG.md").read_bytes() == b"user changelog\r\n"

    @_skip_bash_git_unavailable
    def test_first_append_stages_changelog_edit_in_candidate(self, tmp_path: Path, first_append_repo: str) -> None:
        """A first append directs the ordinary changelog edit to a candidate copy, never the live file.

        After the edit lands in the stage, a later commit makes sealing fail and every live byte stays as it was.
        """
        prewrite = _block(_SKILL, 'EDIT_CHANGELOG_FILE="$CHANGELOG_FILE"')

        completed = _run_block(
            tmp_path, prewrite + '\nprintf "candidate\\n" >> "$EDIT_CHANGELOG_FILE"\n', CHANGELOG_FILE="CHANGELOG.md"
        )

        assert completed.returncode == 0, completed.stderr
        assert (tmp_path / "CHANGELOG.md").read_bytes() == b"user changelog\r\n"
        staged = _read(tmp_path / "release-append-stage-test").strip()
        assert staged
        stage = _native_path(staged)
        assert (stage / "CHANGELOG.md").read_bytes() == b"user changelog\r\ncandidate\n"
        (stage / "DRAFT.md").write_bytes(b"candidate draft\n")
        _write_state(stage, _MAIN_MARKER, first_append_repo + "\n")
        _commit(tmp_path, "later")
        with pytest.raises(ValueError, match="HEAD changed after release gather"):
            publisher.seal(tmp_path, "main", stage)
        assert (tmp_path / "DRAFT.md").read_bytes() == b"user draft\r\n"
        assert (tmp_path / "CHANGELOG.md").read_bytes() == b"user changelog\r\n"
        assert not (tmp_path / _MAIN_MARKER).exists()


@pytest.fixture
def fallback_draft_stage(tmp_path: Path) -> Path:
    """Open a stage for the invalid-marker draft path and plant the gather sentinels the draft gate reads."""
    frozen_head = _init_git(tmp_path)
    write_lf(tmp_path / "DRAFT.md", "user draft\n")
    (tmp_path / ".temp").mkdir()
    stage = publisher.begin(
        tmp_path, "main", "CHANGELOG.md", expected_head=frozen_head, expected_branch_ref="main", range_start="HEAD"
    )
    _seed_sentinels(
        tmp_path,
        setup=_MAIN_SETUP,
        flat={
            "append-stage": bash_path(stage),
            "do-append": "true",
            "append-head": frozen_head,
            "append-branch": "main",
        },
        tags=b"",
    )
    return stage


@_skip_bash_git_unavailable
class TestFallbackDraftGate:
    """The shipped gate that rechecks the gather source immediately before the fallback draft write."""

    _GATE = "Recheck source immediately before fallback draft write"
    _WRITE = '\nprintf "replacement\\n" > "$APPEND_STAGE/DRAFT.md"\n'

    def test_allows_write_when_source_unchanged(self, tmp_path: Path, fallback_draft_stage: Path) -> None:
        """With HEAD, branch and tags as gathered, the gate passes and only the candidate copy is edited."""
        completed = _run_block(tmp_path, _block(_TEMPLATE, self._GATE) + self._WRITE)

        assert completed.returncode == 0, completed.stderr
        assert _read(fallback_draft_stage / "DRAFT.md") == "replacement\n"
        assert _read(tmp_path / "DRAFT.md") == "user draft\n"

    @pytest.mark.parametrize(
        ("drift", "message"),
        [
            pytest.param("new-commit", "changed after release gather", id="new-commit"),
            pytest.param("branch-switch", "changed after release gather", id="branch-switch"),
            pytest.param("new-tag", "changed after gather", id="new-tag"),
        ],
    )
    def test_blocks_write_when_source_moved(
        self, tmp_path: Path, fallback_draft_stage: Path, drift: str, message: str
    ) -> None:
        """A commit, branch switch or new tag after Gather stops the gate before the candidate is edited."""
        _DRIFT[drift](tmp_path)

        completed = _run_block(tmp_path, _block(_TEMPLATE, self._GATE) + self._WRITE)

        assert completed.returncode != 0, completed.stdout
        assert message in completed.stderr
        assert _read(fallback_draft_stage / "DRAFT.md") == "user draft\n"
        assert _read(tmp_path / "DRAFT.md") == "user draft\n"


@dataclasses.dataclass(frozen=True)
class _RangeStartRepo:
    """A two-commit repository plus the ``begin`` arguments that still lack ``--range-start``."""

    begin_args: list[str]
    base: str
    late: str
    head: str


def _begin_args(head: str, tags: Path) -> list[str]:
    """Arguments of a direct ``begin`` call for ``head`` on ``main`` with an empty tag-state file."""
    return [
        "begin",
        "--branch",
        "main",
        "--changelog",
        "CHANGELOG.md",
        "--head-sha",
        head,
        "--marker-sha",
        head,
        "--branch-ref",
        "main",
        "--tag-state-file",
        str(tags),
    ]


@pytest.fixture
def range_start_repo(tmp_path: Path) -> _RangeStartRepo:
    """Build a two-commit repository (``undrafted`` then ``drafted``) and the direct ``begin`` call for its HEAD."""
    base = _init_git(tmp_path)
    _commit(tmp_path, "undrafted")
    late = _head(tmp_path)
    _commit(tmp_path, "drafted")
    head = _head(tmp_path)
    assert _git(tmp_path, "rev-list", "--count", f"{base}..{head}") == "2"
    tags = tmp_path / "tags.txt"
    tags.write_bytes(b"")
    (tmp_path / "DRAFT.md").write_bytes(b"Notes only for drafted commit\n")
    (tmp_path / "CHANGELOG.md").write_bytes(b"old\n")
    return _RangeStartRepo(_begin_args(head, tags), base, late, head)


def _begin_with_range_start(tmp_path: Path, repo: _RangeStartRepo) -> tuple[Path, Path, dict]:
    """Run a valid ``begin --range-start <base>`` and return the stage, its candidate file and parsed candidate."""
    valid = _cli(tmp_path, *repo.begin_args, "--range-start", repo.base)
    assert valid.returncode == 0, valid.stderr
    stage = _native_path(valid.stdout.strip())
    candidate_path = stage / "candidate.json"
    return stage, candidate_path, json.loads(_read(candidate_path))


class TestBeginRangeStart:
    """Direct ``begin`` calls cannot certify an endpoint without a valid, completed range start."""

    @pytest.mark.parametrize(
        ("range_args", "exit_codes"),
        [
            pytest.param([], (1, 2), id="missing"),
            pytest.param(["--range-start", "f" * 40], (1,), id="unknown-sha"),
            pytest.param(["--range-start", "LATE"], (1,), id="skips-undrafted-commit"),
        ],
    )
    def test_rejects_missing_invalid_or_skipped_range_start(
        self, tmp_path: Path, range_start_repo: _RangeStartRepo, range_args: list[str], exit_codes: tuple[int, ...]
    ) -> None:
        """A missing, unknown or skipping range start is refused and no stage directory is left behind.

        ``LATE`` stands for the undrafted commit: starting after it would silently drop it from the notes.
        """
        arguments = [range_start_repo.late if argument == "LATE" else argument for argument in range_args]

        completed = _cli(tmp_path, *range_start_repo.begin_args, *arguments)

        assert completed.returncode in exit_codes
        assert not list((tmp_path / ".temp").glob("release-append-stage-*"))

    def test_valid_range_start_is_recorded_in_candidate(
        self, tmp_path: Path, range_start_repo: _RangeStartRepo
    ) -> None:
        """A begin call with a valid ``--range-start`` records it as the candidate's range start."""
        _, _, candidate = _begin_with_range_start(tmp_path, range_start_repo)

        assert candidate["range_start_sha"] == range_start_repo.base

    def test_seal_rejects_tampered_range_start_but_accepts_original(
        self, tmp_path: Path, range_start_repo: _RangeStartRepo
    ) -> None:
        """Seal rejects a candidate whose range_start_sha was tampered after staging, then accepts the original.

        Restoring the field after the refusal proves the rejection was keyed to the tampered value alone.
        """
        stage, candidate_path, candidate = _begin_with_range_start(tmp_path, range_start_repo)
        _write_state(stage, _MAIN_MARKER, range_start_repo.head + "\n")
        write_lf(candidate_path, json.dumps({**candidate, "range_start_sha": "f" * 40}))
        tampered_seal = _cli(tmp_path, "seal", "--branch", "main", "--stage", str(stage))
        write_lf(candidate_path, json.dumps(candidate))

        restored_seal = _cli(tmp_path, "seal", "--branch", "main", "--stage", str(stage))

        assert tampered_seal.returncode == 1
        assert restored_seal.returncode == 0, restored_seal.stderr

    def test_publish_rejects_range_start_tampered_after_seal(
        self, tmp_path: Path, range_start_repo: _RangeStartRepo
    ) -> None:
        """Publish rejects a sealed candidate whose range_start_sha was tampered after sealing."""
        stage, candidate_path, candidate = _begin_with_range_start(tmp_path, range_start_repo)
        _write_state(stage, _MAIN_MARKER, range_start_repo.head + "\n")
        seal = _cli(tmp_path, "seal", "--branch", "main", "--stage", str(stage))
        assert seal.returncode == 0, seal.stderr
        write_lf(candidate_path, json.dumps({**candidate, "range_start_sha": "f" * 40}))

        publish = _cli(tmp_path, "publish", "--branch", "main", "--stage", str(stage))

        assert publish.returncode == 1
        assert not (tmp_path / _MAIN_MARKER).exists()

    def test_last_tag_anchors_baseline_sha(self, tmp_path: Path, range_start_repo: _RangeStartRepo) -> None:
        """A begin call anchored on ``--last-tag`` records the tagged commit as the release baseline."""
        _git(tmp_path, "tag", "v2", range_start_repo.late)
        (tmp_path / "tags.txt").write_bytes(publisher._tag_state(tmp_path))

        tagged = _cli(
            tmp_path, *range_start_repo.begin_args, "--last-tag", "v2", "--range-start", range_start_repo.late
        )

        assert tagged.returncode == 0, tagged.stderr
        metadata = json.loads(_read(_native_path(tagged.stdout.strip()) / "candidate.json"))
        assert metadata["baseline_sha"] == range_start_repo.late

    def test_direct_begin_cannot_skip_a_saved_marker(self, tmp_path: Path) -> None:
        """An omitted saved-marker argument cannot hide pending commits from a direct caller."""
        base = _init_git(tmp_path)
        _commit(tmp_path, "pending")
        head = _head(tmp_path)
        _write_state(tmp_path, _MAIN_MARKER, base + "\n")
        tags = tmp_path / "tags.txt"
        tags.write_bytes(b"")
        (tmp_path / "DRAFT.md").write_bytes(b"Notes omit pending commit\n")
        (tmp_path / "CHANGELOG.md").write_bytes(b"old\n")

        skipped = _cli(tmp_path, *_begin_args(head, tags), "--range-start", head)

        assert skipped.returncode == 1
        assert (tmp_path / _MAIN_MARKER).read_bytes() == (base + "\n").encode()

    def test_direct_begin_cannot_publish_behind_a_saved_marker(self, tmp_path: Path) -> None:
        """An omitted continuity argument cannot downgrade a valid live marker."""
        base = _init_git(tmp_path)
        _commit(tmp_path, "represented")
        saved = _head(tmp_path)
        _write_state(tmp_path, _MAIN_MARKER, saved + "\n")

        with pytest.raises(ValueError, match=r"saved marker|endpoint"):
            publisher.begin(tmp_path, "main", "CHANGELOG.md", marker_sha=base, range_start=base)

        assert (tmp_path / _MAIN_MARKER).read_bytes() == (saved + "\n").encode()

    def test_rejects_completed_endpoint_before_saved_marker(self, tmp_path: Path) -> None:
        """Direct candidate staging must not permit a marker rewind."""
        _init_git(tmp_path)
        _commit(tmp_path, "historical endpoint")
        stale_end = _head(tmp_path)
        _commit(tmp_path, "already drafted")
        old_marker = _head(tmp_path)
        _write_state(tmp_path, _MAIN_MARKER, old_marker + "\n")

        with pytest.raises(ValueError, match="before saved marker"):
            publisher.begin(
                tmp_path, "main", "CHANGELOG.md", expected_start=old_marker, marker_sha=stale_end, range_start="HEAD"
            )

        assert _read(tmp_path / _MAIN_MARKER) == old_marker + "\n"
        assert not publisher.journal_path(tmp_path, "main").exists()

    def test_tag_supersession_requires_selected_tag_baseline(self, tmp_path: Path) -> None:
        """A tag replacing an older marker cannot leave later commits out of notes.

        Starting after the tag (at the omitted commit) is refused; starting at the tag itself is accepted.
        """
        saved = _init_git(tmp_path)
        _commit(tmp_path, "tagged")
        tagged = _head(tmp_path)
        _git(tmp_path, "tag", "v2", tagged)
        _commit(tmp_path, "omitted")
        omitted = _head(tmp_path)
        _commit(tmp_path, "drafted")
        endpoint = _head(tmp_path)
        _write_state(tmp_path, _MAIN_MARKER, saved + "\n")

        with pytest.raises(ValueError, match=r"tag|baseline|start"):
            publisher.begin(tmp_path, "main", "CHANGELOG.md", marker_sha=endpoint, range_start=omitted, last_tag="v2")
        stage = publisher.begin(
            tmp_path, "main", "CHANGELOG.md", marker_sha=endpoint, range_start=tagged, last_tag="v2"
        )

        assert json.loads(_read(stage / "candidate.json"))["range_start_sha"] == tagged

    def test_cli_accepts_release_setup_dotted_branch_slug(self, tmp_path: Path) -> None:
        """A normalized release branch with a dotted version reaches staging through the command line."""
        head_sha = _init_git(tmp_path)
        _git(tmp_path, "branch", "-M", "release/v1.2")
        tag_state = tmp_path / "tag-state"
        tag_state.write_bytes(
            subprocess.check_output(
                ["git", "for-each-ref", "--sort=refname", "--format=%(refname) %(objectname)", "refs/tags"],
                cwd=tmp_path,
            )
        )
        (tmp_path / ".temp").mkdir()
        dotted_marker = marker_api.state_relative("release/v1.2", "marker")
        _write_state(tmp_path, dotted_marker, head_sha + "\n")
        write_lf(tmp_path / "DRAFT.md", "draft\n")
        write_lf(tmp_path / "CHANGELOG.md", "changelog\n")

        completed = _cli(
            tmp_path,
            "begin",
            "--branch",
            "release-v1.2",
            "--changelog",
            "CHANGELOG.md",
            "--head-sha",
            head_sha,
            "--start-sha",
            head_sha,
            "--range-start",
            head_sha,
            "--branch-ref",
            "release/v1.2",
            "--tag-state-file",
            str(tag_state),
        )

        assert completed.returncode == 0, completed.stderr
        stage = _native_path(completed.stdout.strip())
        assert stage.is_dir()
        assert _read(stage / dotted_marker) == head_sha + "\n"


class TestBranchIdentity:
    """Branch names map to scratch slugs and legacy state without collisions or traversal."""

    @pytest.mark.parametrize(
        "branch", ["../escape", "release..escape", "release/escape", "feature+../escape", "feature\\escape"]
    )
    def test_rejects_path_like_branch_slug(self, branch: str) -> None:
        """The scratch slug rejects separators and traversal tokens."""
        with pytest.raises(ValueError, match="filename-safe slug"):
            publisher._safe_branch(branch)

    @pytest.mark.parametrize(
        ("git_branch", "slug"),
        [
            pytest.param("feature+append", "feature+append", id="plus"),
            pytest.param("feature%append", "feature%25append", id="percent"),
        ],
    )
    def test_punctuation_branch_completes_append_without_journal_collision(
        self, tmp_path: Path, git_branch: str, slug: str
    ) -> None:
        """Git-valid punctuation survives setup, source checks and publication end to end.

        Release setup, the gather range check and the publisher run in turn on a branch such as ``feature+append``; its
        journal must not collide with the one of the hyphenated sibling name.
        """
        start_sha = _init_git(tmp_path)
        _git(tmp_path, "branch", "-M", git_branch)
        _commit(tmp_path, "next")
        head_sha = _head(tmp_path)
        (tmp_path / ".temp").mkdir()
        marker = tmp_path / marker_api.state_relative(git_branch, "marker")
        marker.parent.mkdir(parents=True, exist_ok=True)
        write_lf(marker, start_sha + "\n")
        write_lf(tmp_path / "DRAFT.md", "original\n")
        write_lf(tmp_path / "CHANGELOG.md", "changelog\n")
        setup = subprocess.run(
            [sys.executable, str(_SCRIPT.with_name("release_setup.py"))],
            cwd=tmp_path,
            env=_block_env(tmp_path, "punctuation"),
            capture_output=True,
            text=True,
            check=True,
        )
        assert _read(tmp_path / "release-setup-punctuation/BRANCH").strip() == slug, setup.stderr
        assert publisher.journal_path(tmp_path, git_branch) != publisher.journal_path(tmp_path, "feature-append")
        _seed_sentinels(tmp_path, flat={"do-append": "true", "mode": "notes"}, session="punctuation")

        gathered = _run_block(tmp_path, f"RANGE={start_sha}..HEAD; " + _gather_prefix(), "punctuation")

        assert gathered.returncode == 0, gathered.stderr
        assert _read(tmp_path / "release-append-branch-punctuation").strip() == git_branch
        stage = publisher.begin(tmp_path, slug, "CHANGELOG.md", head_sha, start_sha, git_branch, range_start=start_sha)
        write_lf(stage / "DRAFT.md", "original\nnew change\n")
        _write_state(stage, marker_api.state_relative(git_branch, "marker"), head_sha + "\n")
        publisher.seal(tmp_path, slug, stage)
        publisher.publish(tmp_path, slug, stage)
        assert _read(tmp_path / "DRAFT.md") == "original\nnew change\n"
        assert _read(marker) == head_sha + "\n"
        assert not publisher.journal_path(tmp_path, git_branch).exists()

    @pytest.mark.parametrize(
        ("branch_ref", "legacy_slug"),
        [
            pytest.param("main", "main", id="ordinary"),
            pytest.param("feature/append", "feature-append", id="slash-collision"),
            pytest.param("feature%append", "feature%25append", id="prior-percent-encoding"),
        ],
    )
    def test_legacy_release_state_blocks_append_without_changing_old_bytes(
        self, tmp_path: Path, branch_ref: str, legacy_slug: str
    ) -> None:
        """Ambiguous legacy marker, provenance and journal bytes require a migration decision."""
        _init_git(tmp_path)
        _git(tmp_path, "branch", "-M", branch_ref)
        old = tmp_path / ".temp"
        old.mkdir()
        legacy = {
            f"release-last-processed-{legacy_slug}": b"old marker\n",
            f"release-provenance-{legacy_slug}.json": b"old provenance\n",
            f"release-append-publish-{legacy_slug}.json": b"old journal\n",
        }
        _write_files(old, legacy)
        write_lf(tmp_path / "DRAFT.md", "original\n")
        write_lf(tmp_path / "CHANGELOG.md", "changelog\n")

        with pytest.raises(ValueError, match="migration decision"):
            publisher.begin(
                tmp_path, quote(branch_ref.replace("/", "-"), safe="-_.+"), "CHANGELOG.md", range_start="HEAD"
            )

        assert {name: (old / name).read_bytes() for name in legacy} == legacy
        assert not (old / "release-state-v2").exists()
        assert _read(tmp_path / "DRAFT.md") == "original\n"


@pytest.fixture
def tagged_baseline_repo(tmp_path: Path) -> tuple[str, str]:
    """Tag a repo ``v1``, save a valid marker one commit behind HEAD and plant all append sentinels.

    Returns ``(marker_sha, frozen_head)``. No ``v2`` tag exists yet: tests add it to model a release cut after Gather.
    """
    baseline = _init_git(tmp_path)
    _git(tmp_path, "tag", "v1")
    _commit(tmp_path, "marker")
    marker = _head(tmp_path)
    _commit(tmp_path, "release")
    frozen_head = _head(tmp_path)
    assert baseline != marker != frozen_head
    (tmp_path / ".temp").mkdir()
    write_lf(tmp_path / "DRAFT.md", "original\n")
    write_lf(tmp_path / "CHANGELOG.md", "changelog\n")
    _write_state(tmp_path, _MAIN_MARKER, marker + "\n")
    _seed_sentinels(
        tmp_path,
        setup={**_KEYED_SETUP, "LAST_TAG": "v1"},
        flat={
            "do-append": "true",
            "mode": "notes",
            "append-head": frozen_head,
            "append-start": marker,
            "append-end": frozen_head,
            "append-branch": "main",
            "marker-valid": "true",
        },
        tags=publisher._tag_state(tmp_path),
    )
    status = subprocess.run(
        [sys.executable, str(_MARKER_SCRIPT), "is-valid", "--branch", "main", "--last-tag", "v1"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert status.stdout.strip() == "true"
    return marker, frozen_head


@_skip_bash_git_unavailable
class TestNewReleaseTag:
    """A tag cut after Gather invalidates the release baseline even though HEAD and branch are unchanged."""

    def test_blocks_staging_block(self, tmp_path: Path, tagged_baseline_repo: tuple[str, str]) -> None:
        """The shipped staging block refuses to begin once a release tag was cut after Gather."""
        marker, frozen_head = tagged_baseline_repo
        _git(tmp_path, "tag", "v2", frozen_head)
        assert _head(tmp_path) == frozen_head

        completed = _run_block(tmp_path, _block(_SKILL, 'release_append_publish.py" begin'))

        assert completed.returncode != 0, completed.stdout
        assert _read(tmp_path / "DRAFT.md") == "original\n"
        assert _read(tmp_path / _MAIN_MARKER) == marker + "\n"

    def test_blocks_publish_block(self, tmp_path: Path, tagged_baseline_repo: tuple[str, str]) -> None:
        """The shipped publish block refuses to promote a staged candidate once a release tag was cut."""
        marker, frozen_head = tagged_baseline_repo
        stage = publisher.begin(tmp_path, "main", "CHANGELOG.md", frozen_head, marker, range_start=marker)
        write_lf(stage / "DRAFT.md", "candidate\n")
        _stage_marker(stage)
        _seed_sentinels(tmp_path, flat={"append-stage": bash_path(stage)})
        _git(tmp_path, "tag", "v2", frozen_head)
        assert _head(tmp_path) == frozen_head

        completed = _run_block(tmp_path, _block(_TEMPLATE, 'release_append_publish.py" publish'))

        assert completed.returncode != 0, completed.stdout
        assert _read(tmp_path / "DRAFT.md") == "original\n"
        assert _read(tmp_path / _MAIN_MARKER) == marker + "\n"


class TestSkillGatherBlocks:
    """Shipped gather, marker-refresh and setup blocks run unchanged against real repositories."""

    @_skip_bash_git_unavailable
    def test_explicit_range_must_end_at_frozen_head(self, tmp_path: Path) -> None:
        """A caller-supplied range ending before HEAD cannot certify skipped commits."""
        _git(tmp_path, "init", "-q", "-b", "main")
        commits = _history(tmp_path, 3)
        _git(tmp_path, "tag", "v1", commits[0])
        _seed_sentinels(tmp_path, setup={**_KEYED_SETUP, "LAST_TAG": "v1"}, flat={"do-append": "true", "mode": "notes"})

        completed = _run_block(tmp_path, f"RANGE=v1..{commits[1]}; " + _gather_prefix())

        assert completed.returncode != 0
        assert "does not end at frozen HEAD" in completed.stderr
        assert not (tmp_path / "release-range-test").exists()

    @_skip_bash_git_unavailable
    def test_explicit_range_cannot_skip_valid_marker(self, tmp_path: Path) -> None:
        """An explicit left endpoint must include every commit after the valid marker."""
        base = _init_git(tmp_path)
        _git(tmp_path, "tag", "v1", base)
        commits = _history(tmp_path, 3)
        (tmp_path / ".temp").mkdir()
        _write_state(tmp_path, _MAIN_MARKER, commits[0] + "\n")
        _seed_sentinels(tmp_path, setup={**_KEYED_SETUP, "LAST_TAG": "v1"}, flat={"do-append": "true", "mode": "notes"})

        completed = _run_block(tmp_path, f"RANGE={commits[1]}..HEAD; " + _gather_prefix())

        assert completed.returncode != 0
        assert "does not start at saved marker" in completed.stderr
        assert not (tmp_path / "release-range-test").exists()

    @_skip_bash_unavailable
    @pytest.mark.parametrize(
        ("marker_commit", "range_template"),
        [
            pytest.param(0, "{1}..HEAD", id="range-disjoint-from-saved-marker"),
            pytest.param(1, "v1..{0}", id="range-ending-before-saved-marker"),
        ],
    )
    def test_plain_notes_reject_range_outside_saved_marker(
        self, tmp_path: Path, marker_commit: int, range_template: str
    ) -> None:
        """A plain explicit range may neither skip commits after the saved marker nor end before it.

        Three commits follow the ``v1`` tag. With the saved marker on the first, a range starting at the second would
        skip the commit in between; with the marker on the second, a historical range ending at the first would rewind
        the marker and replay already represented commits. Gather must refuse both and leave the marker untouched.
        """
        base = _init_git(tmp_path)
        _git(tmp_path, "tag", "v1", base)
        commits = _history(tmp_path, 3)
        old_marker = commits[marker_commit]
        _write_state(tmp_path, _MAIN_MARKER, old_marker + "\n")
        _seed_sentinels(
            tmp_path, setup={**_KEYED_SETUP, "LAST_TAG": "v1"}, flat={"do-append": "false", "mode": "notes"}
        )
        gather_block = _block(_SKILL, "append range does not start at saved marker").split("# persist (Check 41)", 1)[0]

        completed = _run_block(tmp_path, f'RANGE="{range_template.format(*commits)}"\n{gather_block}')

        assert completed.returncode != 0
        assert _read(tmp_path / _MAIN_MARKER) == old_marker + "\n"

    @_skip_bash_git_unavailable
    def test_marker_refresh_rejects_new_commit_after_gather(self, tmp_path: Path) -> None:
        """The marker-refresh block checks the frozen HEAD before any marker write.

        A commit lands after Gather froze HEAD; refreshing the marker would certify a commit nobody classified.
        """
        frozen_head = _init_git(tmp_path)
        _commit(tmp_path, "later")
        _seed_sentinels(tmp_path, setup={"BRANCH": "main"}, flat={"do-append": "true", "append-head": frozen_head})

        completed = _run_block(tmp_path, _block(_TEMPLATE, "HEAD changed after append gather"))

        assert completed.returncode != 0
        assert "HEAD changed after append gather" in completed.stderr
        assert not (tmp_path / _MAIN_MARKER).exists()

    def test_marker_refresh_stages_only_gathered_endpoint(self) -> None:
        """Both plain and append notes stage only the gathered endpoint marker, once, inside the candidate."""
        block = _block(_TEMPLATE, "HEAD changed after append gather")

        assert '[ "$MARKER_SHA" = "$FROZEN_END" ]' in block
        assert block.count('--sha "$MARKER_SHA"') == 1
        assert block.count('--receipt "$RECEIPT_FILE"') == 1
        assert '--marker-dir "$APPEND_STAGE/.temp"' in block

    @_skip_bash_git_unavailable
    def test_plain_notes_marker_tracks_completed_explicit_range_endpoint(self, tmp_path: Path) -> None:
        """An explicit notes range ending before HEAD leaves the later commits for the next append.

        Notes cover ``v1..v2`` while HEAD is one commit past ``v2``. The refresh block must stage no live marker, the
        publish block must then save ``v2`` as the marker, and the next range must start there and run to HEAD.
        """
        base = _init_git(tmp_path)
        _git(tmp_path, "tag", "v1", base)
        _commit(tmp_path, "represented")
        represented = _head(tmp_path)
        _git(tmp_path, "tag", "v2", represented)
        _commit(tmp_path, "later")
        head = _head(tmp_path)
        assert head != represented
        stage = publisher.begin(
            tmp_path,
            "main",
            "CHANGELOG.md",
            head,
            expected_branch_ref="main",
            marker_sha=represented,
            range_start=base,
        )
        write_lf(stage / "DRAFT.md", "notes for v1..v2\n")
        write_lf(stage / "CHANGELOG.md", "# Changelog\n")
        _seed_sentinels(
            tmp_path,
            setup=_MAIN_SETUP,
            flat={
                "do-append": "false",
                "mode": "notes",
                "range": f"{base}..{represented}",
                "append-head": head,
                "append-end": represented,
                "append-branch": "main",
                "append-stage": bash_path(stage),
            },
            tags=publisher._tag_state(tmp_path),
        )

        refreshed = _run_block(tmp_path, _block(_TEMPLATE, "HEAD changed after append gather"))
        marker_after_refresh = (tmp_path / _MAIN_MARKER).exists()
        published = _run_block(tmp_path, _block(_TEMPLATE, 'release_append_publish.py" publish'))
        resolved = subprocess.run(
            [sys.executable, str(_MARKER_SCRIPT), "resolve", "--branch", "main", "--last-tag", "v1"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        )

        assert refreshed.returncode == 0, refreshed.stderr
        assert not marker_after_refresh
        assert published.returncode == 0, published.stderr
        assert _read(tmp_path / _MAIN_MARKER) == represented + "\n"
        assert resolved.stdout.strip() == f"{represented}..HEAD"

    @_skip_bash_unavailable
    def test_plain_notes_reject_head_change_after_gather(self, tmp_path: Path) -> None:
        """A symbolic HEAD range cannot certify a commit that landed after the notes were drafted."""
        base = _init_git(tmp_path)
        _git(tmp_path, "tag", "v1", base)
        _commit(tmp_path, "represented")
        frozen = _head(tmp_path)
        write_lf(tmp_path / "DRAFT.md", "notes through frozen HEAD\n")
        _seed_sentinels(
            tmp_path,
            setup=_MAIN_SETUP,
            flat={"do-append": "false", "range": "v1..HEAD", "append-head": frozen},
        )
        _commit(tmp_path, "later")

        completed = _run_block(tmp_path, _block(_TEMPLATE, "HEAD changed after append gather"))

        assert completed.returncode != 0
        assert not (tmp_path / _MAIN_MARKER).exists()

    @_skip_bash_unavailable
    def test_plain_notes_stage_before_artifact_edits(self, tmp_path: Path) -> None:
        """A plain draft and marker share a recoverable candidate before any live artifact is edited.

        The staging block copies the live draft, changelog and both dated outputs into the stage and records the output
        paths in the candidate metadata, leaving the live files as they were.
        """
        head = _init_git(tmp_path)
        (tmp_path / "DRAFT.md").write_bytes(b"ORIGINAL\n")
        (tmp_path / "CHANGELOG.md").write_bytes(b"# Changelog\n")
        summary = ".temp/output-release-summary-main-2026-09-26.md"
        migration = ".temp/output-release-migration-main-2026-09-26.md"
        _write_state(tmp_path, summary, "OLD SUMMARY\n")
        _write_state(tmp_path, migration, "OLD MIGRATION\n")
        _seed_sentinels(
            tmp_path,
            setup={**_MAIN_SETUP, "DATE": "2026-09-26"},
            flat={
                "do-append": "false",
                "do-summary": "true",
                "do-migration": "true",
                "append-head": head,
                "mode": "notes",
                "append-start": head,
                "append-end": head,
                "append-branch": "main",
            },
            tags=b"",
        )

        completed = _run_block(tmp_path, _block(_SKILL, 'release_append_publish.py" begin'))

        assert completed.returncode == 0, completed.stderr
        stage_text = _read(tmp_path / "release-append-stage-test").strip()
        assert stage_text, f"plain notes must create a candidate before editing artifacts: {completed.stdout}"
        stage = _native_path(stage_text)
        assert (stage / "DRAFT.md").read_bytes() == b"ORIGINAL\n"
        assert (tmp_path / "DRAFT.md").read_bytes() == b"ORIGINAL\n"
        assert (stage / summary).read_bytes() == b"OLD SUMMARY\n"
        assert (stage / migration).read_bytes() == b"OLD MIGRATION\n"
        assert json.loads(_read(stage / "candidate.json"))["extra_outputs"] == [summary, migration]

    @_skip_bash_git_unavailable
    def test_tag_supersession_resumes_from_selected_tag(self, tmp_path: Path) -> None:
        """The ordinary release shell path may resume at a tag that superseded its saved marker.

        Staging, marker refresh and publication run in turn: the candidate baseline is the tag, the live marker stays on
        the old commit until publish, and publish then moves it to HEAD.
        """
        saved = _init_git(tmp_path)
        _commit(tmp_path, "tagged")
        tagged = _head(tmp_path)
        _git(tmp_path, "tag", "v2", tagged)
        _commit(tmp_path, "drafted")
        head = _head(tmp_path)
        _write_state(tmp_path, _MAIN_MARKER, saved + "\n")
        (tmp_path / "DRAFT.md").write_bytes(b"earlier notes\n")
        (tmp_path / "CHANGELOG.md").write_bytes(b"earlier changelog\n")
        _seed_sentinels(
            tmp_path,
            session="tagged",
            setup={**_KEYED_SETUP, "LAST_TAG": "v2", "DATE": "2026-09-26"},
            flat={
                "do-append": "true",
                "mode": "notes",
                "append-head": head,
                "append-start": tagged,
                "append-end": head,
                "append-branch": "main",
            },
            tags=publisher._tag_state(tmp_path),
        )

        staged = _run_block(tmp_path, _block(_SKILL, 'release_append_publish.py" begin'), "tagged")
        assert staged.returncode == 0, staged.stderr
        stage = _native_path(_read(tmp_path / "release-append-stage-tagged").strip())
        metadata = json.loads(_read(stage / "candidate.json"))
        _seed_sentinels(tmp_path, session="tagged", flat={"range": f"{tagged}..{head}", "marker-valid": "false"})
        refreshed = _run_block(tmp_path, _block(_TEMPLATE, 'release_append_marker.py" receipt'), "tagged")
        marker_in_stage = (stage / _MAIN_MARKER).read_bytes()
        marker_live_before_publish = (tmp_path / _MAIN_MARKER).read_bytes()
        publisher.publish(tmp_path, "main", stage)

        assert metadata["baseline_sha"] == tagged
        assert metadata["range_start_sha"] == tagged
        assert refreshed.returncode == 0, refreshed.stderr
        assert marker_in_stage == (head + "\n").encode()
        assert marker_live_before_publish == (saved + "\n").encode()
        assert (tmp_path / _MAIN_MARKER).read_bytes() == (head + "\n").encode()


def _edit_draft(root: Path, draft: Path) -> None:
    """Overwrite the draft with a foreign edit."""
    draft.write_bytes(b"foreign edit\n")


def _land_commit(root: Path, draft: Path) -> None:
    """Move the source forward with a new commit."""
    _commit(root, "later")


class TestCompletionReceipt:
    """The receipt written between truth review and marker write expires when the draft or the source changes."""

    @pytest.mark.parametrize(
        "interruption", [pytest.param(_edit_draft, id="draft-edit"), pytest.param(_land_commit, id="new-commit")]
    )
    def test_interrupted_receipt_cannot_advance_marker_after_change(
        self, tmp_path: Path, interruption: Callable[[Path, Path], None]
    ) -> None:
        """A receipt stranded between truth review and write is refused after a draft edit or a new commit.

        The marker command must exit 1, keep the old marker bytes and leave the receipt for inspection.
        """
        base = _init_git(tmp_path)
        _commit(tmp_path, "represented")
        represented = _head(tmp_path)
        draft = tmp_path / "DRAFT.md"
        draft.write_bytes(b"completed notes\n")
        marker = tmp_path / _MAIN_MARKER
        marker.parent.mkdir(parents=True)
        marker.write_bytes(b"OLD\n")
        completed_range = tmp_path / "completed-range"
        write_lf(completed_range, f"{base}..{represented}\n")
        receipt = tmp_path / "receipt.json"
        subprocess.run(
            [
                *(sys.executable, str(_MARKER_SCRIPT), "receipt", "--branch", "main"),
                *("--range-file", str(completed_range), "--draft", str(draft), "--output", str(receipt)),
            ],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        )
        interruption(tmp_path, draft)

        completed = subprocess.run(
            [
                *(sys.executable, str(_MARKER_SCRIPT), "write", "--branch", "main", "--sha", represented),
                *("--receipt", str(receipt), "--draft", str(draft)),
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )

        assert completed.returncode == 1
        assert marker.read_bytes() == b"OLD\n"
        assert receipt.exists()


@_skip_bash_unavailable
class TestSetupBlock:
    """The shipped setup block guards legacy state and pending publications before any artifact work."""

    _SETUP = "release branch identity or mode missing"

    def test_refuses_legacy_branch_state(self, tmp_path: Path) -> None:
        """Plain notes refuse old branch state instead of treating its marker as absent.

        A pre-migration marker file means the user must decide how to migrate; the old bytes stay untouched.
        """
        _init_git(tmp_path)
        _seed_sentinels(tmp_path, flat={"mode": "notes"}, session="notes")
        old_marker = tmp_path / ".temp/release-last-processed-main"
        old_marker.parent.mkdir(parents=True, exist_ok=True)
        old_marker.write_bytes(b"legacy marker\n")

        completed = _run_block(tmp_path, _block(_SKILL, self._SETUP), "notes")

        assert completed.returncode == 1
        assert "migration decision required" in completed.stderr
        assert old_marker.read_bytes() == b"legacy marker\n"
        assert _read(tmp_path / "release-setup-notes/BRANCH_KEY").strip() == marker_api.branch_state_key("main")

    def test_proceeds_without_legacy_state(self, tmp_path: Path) -> None:
        """Plain notes on a branch with no legacy state pass setup and create no migrated state."""
        _init_git(tmp_path)
        _seed_sentinels(tmp_path, flat={"mode": "notes"}, session="notes")

        completed = _run_block(tmp_path, _block(_SKILL, self._SETUP), "notes")

        assert completed.returncode == 0, completed.stderr
        assert not (tmp_path / ".temp/release-state-v2").exists()
        assert _read(tmp_path / "release-setup-notes/BRANCH_KEY").strip() == marker_api.branch_state_key("main")

    def test_refuses_pending_publication(self, tmp_path: Path) -> None:
        """Plain notes stop before editing artifacts while an append publication is pending."""
        _init_git(tmp_path)
        _seed_sentinels(tmp_path, flat={"mode": "notes"}, session="notes")
        (tmp_path / "DRAFT.md").write_bytes(b"ORIGINAL\n")
        marker = tmp_path / _MAIN_MARKER
        marker.parent.mkdir(parents=True)
        marker.write_bytes(b"OLD\n")
        journal = publisher.journal_path(tmp_path, "main")
        journal.write_bytes(b'{"mode":"rollback"}\n')

        completed = _run_block(tmp_path, _block(_SKILL, self._SETUP), "notes")

        assert completed.returncode == 1
        assert "branch changed after release gather" in completed.stderr
        assert (tmp_path / "DRAFT.md").read_bytes() == b"ORIGINAL\n"
        assert marker.read_bytes() == b"OLD\n"
        assert journal.read_bytes() == b'{"mode":"rollback"}\n'

    def test_append_recovers_pending_publication_before_marker_guard(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Append setup reconciles its journal before the marker guard can reject it."""
        stage, expected_marker = _staged(tmp_path, "## Summary\nNew\n")
        _interrupt_publish(tmp_path, monkeypatch, stage, lambda call, relative: True)
        journal = publisher.journal_path(tmp_path, "main")
        assert journal.exists()
        _seed_sentinels(tmp_path, flat={"mode": "notes", "do-append": "true"}, session="append")

        completed = _run_block(tmp_path, _block(_SKILL, self._SETUP), "append")

        assert completed.returncode == 0, completed.stderr
        assert "Reconciled interrupted append publication" in completed.stdout
        assert _read(tmp_path / "DRAFT.md") == "## Summary\nNew\n"
        assert _read(tmp_path / _MAIN_MARKER) == expected_marker
        assert not journal.exists()


class TestIsOwnStage:
    """Only the directory ``begin`` created under ``.temp`` may be sealed, published or recovered."""

    def test_accepts_directory_created_by_begin(self, tmp_path: Path) -> None:
        """The staging directory returned by ``begin`` passes."""
        stage = _start(tmp_path)

        assert publisher._is_own_stage(tmp_path.resolve(), stage.resolve()) is True

    @pytest.mark.parametrize("name", [pytest.param("scratch", id="foreign-name"), pytest.param("", id="temp-itself")])
    def test_rejects_other_directory_under_temp(self, tmp_path: Path, name: str) -> None:
        """A real directory in ``.temp`` without the staging prefix is refused.

        Another tool's scratch directory sits directly under ``.temp``, so the parent check alone would accept it and
        promote its ``candidate.json`` into live release files.
        """
        root = tmp_path.resolve()
        other = root / ".temp" / name
        other.mkdir(parents=True, exist_ok=True)

        assert publisher._is_own_stage(root, other) is False

    def test_rejects_symlink_with_staging_name(self, tmp_path: Path) -> None:
        """A symlink named like a stage never counts, even when its target is a directory."""
        root = tmp_path.resolve()
        target = root / "elsewhere"
        target.mkdir()
        link = root / ".temp" / f"{publisher._STAGE_PREFIX}link"
        link.parent.mkdir(exist_ok=True)
        link.symlink_to(target, target_is_directory=True)

        assert publisher._is_own_stage(root, link) is False

    def test_rejects_directory_owned_by_another_user(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A staging-named directory whose owner is not the current user is refused.

        The owner is simulated by changing the uid the process reports, since a test cannot chown to another account.
        """
        root = tmp_path.resolve()
        stage = root / ".temp" / f"{publisher._STAGE_PREFIX}foreign"
        stage.mkdir(parents=True)
        monkeypatch.setattr(publisher.os, "getuid", lambda: -1, raising=False)

        assert publisher._is_own_stage(root, stage) is False
