"""Tests for ``bin/release_append_marker.py``.

The reachability checks and the marker CLI are driven through a stubbed ``subprocess.run``/``which`` (the ``git_run``
fixture), so no real ``git`` is spawned for parsing and decision logic. ``TestRealHistory`` keeps real ``git``
repositories: receipt and write semantics depend on genuine commit ancestry, so that boundary is the contract there.
Marker state lives under ``tmp_path`` (via ``--marker-dir`` or a ``chdir``), never in a real ``.temp/`` directory.

``resolve``/``is-valid`` call ``_is_valid_commit`` and then, only when it is true, ``_tag_advanced_past``. Tests that
cover that combined path give each call its own exit code through ``git_run.side_effect``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
import release_append_marker as ram

_IDENTITY = ["-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false"]
_ANCESTOR_RC = [
    pytest.param(0, True, id="ancestor"),
    pytest.param(1, False, id="not-ancestor"),
]
_LINKED_COMMANDS = [
    pytest.param(["guard", "--branch", "main"], id="guard"),
    pytest.param(["resolve", "--branch", "main", "--last-tag", "v1", "--marker-dir", ".temp"], id="resolve"),
]
_REACHABILITY_CHECKS = [
    pytest.param(ram._is_valid_commit, ("deadbeef",), "HEAD", id="is-valid-commit"),
    pytest.param(ram._tag_advanced_past, ("deadbeef", "v2.0.0"), "v2.0.0", id="tag-advanced-past"),
]


def _completed(returncode: int = 0, stdout: str = "") -> subprocess.CompletedProcess[str]:
    """Build the result a stubbed git command returns."""
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout)


def _fake_marker_git(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Answer branch, HEAD and range-endpoint lookups with fixed values; every other git call succeeds."""
    if "symbolic-ref" in argv:
        return _completed(stdout="main\n")
    if "HEAD^{commit}" in argv:
        return _completed(stdout="fedcba123\n")
    return _completed(stdout="deadbeef123\n")


def _is_ancestor_argv(sha: str, ref: str) -> list[str]:
    """Return the exact argv expected for ``git merge-base --is-ancestor <sha> <ref>``."""
    return ["/fake/git", "merge-base", "--is-ancestor", "--end-of-options", sha, ref]


def _seed_marker(branch: str, content: bytes, marker_dir: Path | None = None) -> Path:
    """Write a versioned marker for ``branch`` (relative to the cwd unless ``marker_dir`` is given) and return it."""
    marker = ram._marker_path(branch, str(marker_dir) if marker_dir else None)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_bytes(content)
    return marker


def _seed_journal(branch: str) -> Path:
    """Write a pending-publication journal for ``branch`` in the cwd and return it."""
    journal = Path(ram.state_relative(branch, "journal.json"))
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_bytes(b'{"mode":"rollback"}\n')
    return journal


def _write_receipt(path: Path, range_text: str, endpoint: str, draft: Path, **overrides: object) -> None:
    """Write a completion receipt bound to ``draft``'s exact bytes; ``overrides`` replace default fields."""
    receipt = {
        "branch": "main",
        "range": range_text,
        "head": "fedcba123",
        "endpoint": endpoint,
        "draft_sha256": hashlib.sha256(draft.read_bytes()).hexdigest(),
        "last_tag": "",
        "tag_sha": None,
        "saved_marker_sha256": None,
        "baseline": "deadbeef123",
        **overrides,
    }
    path.write_text(json.dumps(receipt), encoding="utf-8")


def _write_args(branch: str, sha: str, receipt: Path, draft: Path, *extra: str) -> list[str]:
    """Build the ``write`` command line for a receipt/draft pair."""
    return ["write", "--branch", branch, "--sha", sha, "--receipt", str(receipt), "--draft", str(draft), *extra]


def _receipt_args(branch: str, range_file: Path, draft: Path, output: Path, *extra: str) -> list[str]:
    """Build the ``receipt`` command line for a range file and draft."""
    return [
        "receipt",
        "--branch",
        branch,
        "--range-file",
        str(range_file),
        "--draft",
        str(draft),
        "--output",
        str(output),
        *extra,
    ]


def _digest(data: bytes) -> str:
    """Return the sha256 hex digest the receipt stores for marker bytes."""
    return hashlib.sha256(data).hexdigest()


def _git(repo: Path, *args: str) -> str:
    """Run a real git command in ``repo`` and return its stripped stdout."""
    done = subprocess.run(["git", "-C", str(repo), *_IDENTITY, *args], check=True, capture_output=True, text=True)
    return done.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    """Create an empty commit and return its sha."""
    _git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@dataclasses.dataclass(frozen=True)
class _History:
    """Commits of the test repository: ``base``, ``second`` (tagged ``v2``), ``head``; ``side`` forks from ``base``."""

    repo: Path
    branch: str
    base: str
    second: str
    head: str
    side: str
    unknown: str = "f" * 40


@pytest.fixture(scope="module")
def _history_template(tmp_path_factory: pytest.TempPathFactory) -> _History:
    """Build the read-only template repository once per module; tests copy it."""
    repo = tmp_path_factory.mktemp("history") / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    base = _commit(repo, "first")
    branch = _git(repo, "symbolic-ref", "--short", "HEAD")
    second = _commit(repo, "second")
    _git(repo, "tag", "v2")
    head = _commit(repo, "third")
    _git(repo, "checkout", "-q", "-b", "side", base)
    side = _commit(repo, "side")
    _git(repo, "checkout", "-q", branch)
    return _History(repo, branch, base, second, head, side)


@pytest.fixture
def history(_history_template: _History, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _History:
    """Copy the template repository privately and enter it as the working directory."""
    repo = tmp_path / "repo"
    shutil.copytree(_history_template.repo, repo)
    monkeypatch.chdir(repo)
    return dataclasses.replace(_history_template, repo=repo)


@pytest.fixture
def git_run() -> Any:
    """Stub ``git`` on PATH and every ``subprocess.run`` call made by the script; yield the run mock."""
    with (
        mock.patch("release_append_marker.which", autospec=True, return_value="/fake/git"),
        mock.patch("release_append_marker.subprocess.run", autospec=True) as run,
    ):
        yield run


@pytest.fixture
def marker_git(git_run: Any) -> Any:
    """``git_run`` answering like a repository on ``main`` whose HEAD and range endpoints resolve."""
    git_run.side_effect = _fake_marker_git
    return git_run


@pytest.fixture
def notes(tmp_path: Path) -> Any:
    """Return a factory that writes ``DRAFT.md`` and a matching ``receipt.json``; it yields ``(draft, receipt)``."""

    def build(
        range_text: str = "v1..deadbeef123", draft_text: str = "completed notes\n", **overrides: object
    ) -> tuple[Path, Path]:
        """Write the pair; ``overrides`` replace receipt fields."""
        draft = tmp_path / "DRAFT.md"
        draft.write_text(draft_text, encoding="utf-8")
        receipt = tmp_path / "receipt.json"
        _write_receipt(receipt, range_text, "deadbeef123", draft, **overrides)
        return draft, receipt

    return build


class TestMarkerStorage:
    """_marker_path/_read_marker: where the marker lives and how it is read back."""

    def test_path_joins_branch_into_versioned_namespace(self, tmp_path: Path) -> None:
        """The marker path is branch-bound and sits in the versioned state namespace.

        Different branches must never share a marker, and the ``release-state-v2`` directory keeps this layout apart
        from older marker files.
        """
        path = ram._marker_path("main", str(tmp_path))
        assert path == tmp_path / "release-state-v2" / ram.branch_state_key("main") / "marker"

    def test_read_missing_marker_is_empty(self, tmp_path: Path) -> None:
        """A branch without a marker file reads back as an empty string, not an error.

        The first ``--append`` run has no marker yet; callers rely on "" to mean "establish a baseline".
        """
        assert ram._read_marker("main", str(tmp_path)) == ""

    def test_read_strips_whitespace(self, tmp_path: Path) -> None:
        """The stored sha is returned without its trailing newline.

        The marker is written with a final newline; leaving it on would corrupt every ``<sha>..HEAD`` range.
        """
        _seed_marker("main", b"deadbeef123\n", tmp_path)
        assert ram._read_marker("main", str(tmp_path)) == "deadbeef123"


class TestLinkedMarker:
    """A symlinked marker stops every command before it can redirect a write outside the state directory."""

    @pytest.fixture
    def linked(self, workdir: Path) -> tuple[Path, Path]:
        """Link the branch marker to a missing ``outside.txt``; return ``(marker, outside)``."""
        marker = ram._marker_path("main", str(workdir / ".temp"))
        marker.parent.mkdir(parents=True)
        outside = workdir / "outside.txt"
        marker.symlink_to(outside)
        return marker, outside

    @pytest.mark.parametrize("argv", _LINKED_COMMANDS)
    def test_refuses_dangling_link(
        self, linked: tuple[Path, Path], capsys: pytest.CaptureFixture[str], argv: list[str]
    ) -> None:
        """A marker symlink whose target does not exist is refused and the target is not created.

        Falling through to the "no marker" path would write a fresh marker through the link and create the target.
        """
        marker, outside = linked
        assert ram.main(argv) == 1
        assert ("must not traverse a symlink" in capsys.readouterr().err, marker.is_symlink(), outside.exists()) == (
            True,
            True,
            False,
        )

    @pytest.mark.parametrize("argv", _LINKED_COMMANDS)
    def test_refuses_link_to_existing_file(
        self, linked: tuple[Path, Path], capsys: pytest.CaptureFixture[str], argv: list[str]
    ) -> None:
        """A marker symlink to an existing file is refused and the file keeps its bytes.

        An external file reachable through the marker path must be neither read as a baseline nor overwritten.
        """
        marker, outside = linked
        outside.write_bytes(b"deadbeef1234\n")
        assert ram.main(argv) == 1
        assert (
            "must not traverse a symlink" in capsys.readouterr().err,
            marker.is_symlink(),
            outside.read_bytes(),
        ) == (
            True,
            True,
            b"deadbeef1234\n",
        )


class TestPendingPublication:
    """A pending journal for the branch blocks every marker command until the publication is recovered."""

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param(["is-valid", "--branch", "main", "--last-tag", "v1"], id="is-valid"),
            pytest.param(["resolve", "--branch", "main", "--last-tag", "v1"], id="resolve"),
            pytest.param(["guard", "--branch", "main"], id="guard"),
            pytest.param(["write", "--branch", "main", "--sha", "abc123"], id="write"),
        ],
    )
    def test_journal_blocks_command_and_keeps_state(
        self, workdir: Path, capsys: pytest.CaptureFixture[str], argv: list[str]
    ) -> None:
        """With a rollback journal present each command exits 1, prints nothing on stdout and changes no file.

        A marker must not certify a release, nor be overwritten, while an earlier append publication is half done.
        """
        marker = _seed_marker("main", b"ORIGINAL\n")
        journal = _seed_journal("main")
        assert ram.main(argv) == 1
        captured = capsys.readouterr()
        assert (
            captured.out,
            "pending append publication" in captured.err,
            marker.read_bytes(),
            journal.read_bytes(),
        ) == (
            "",
            True,
            b"ORIGINAL\n",
            b'{"mode":"rollback"}\n',
        )

    def test_removing_journal_restores_marker_reads(
        self, workdir: Path, git_run: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Once the journal is gone the same marker is certified again.

        Recovery must not leave the branch permanently locked: after the journal is removed ``is-valid`` answers
        "true" for a marker that is on HEAD and not behind the release tag.
        """
        _seed_marker("main", b"deadbeef123\n")
        _seed_journal("main").unlink()
        git_run.side_effect = [_completed(0), _completed(1)]  # ancestor of HEAD: yes; ancestor of tag: no
        assert ram.main(["is-valid", "--branch", "main", "--last-tag", "v1"]) == 0
        assert capsys.readouterr().out == "true\n"

    def test_write_to_other_marker_dir_leaves_live_marker(self, workdir: Path, marker_git: Any, notes: Any) -> None:
        """A write staged in its own marker directory succeeds without touching the pending branch's live marker.

        The live marker has a pending journal; a candidate run writing under a separate ``--marker-dir`` must only
        produce its own marker.
        """
        live = _seed_marker("main", b"ORIGINAL\n")
        journal = _seed_journal("main")
        draft, receipt = notes(saved_marker_sha256=_digest(live.read_bytes()))
        stage_dir = workdir / "candidate" / ".temp"
        assert ram.main(_write_args("main", "deadbeef123", receipt, draft, "--marker-dir", str(stage_dir))) == 0
        assert (
            ram._marker_path("main", str(stage_dir)).read_bytes(),
            live.read_bytes(),
            journal.read_bytes(),
        ) == (b"deadbeef123\n", b"ORIGINAL\n", b'{"mode":"rollback"}\n')


class TestReachabilityChecks:
    """_is_valid_commit/_tag_advanced_past: mirror ``git merge-base --is-ancestor`` (reachability, not existence)."""

    @pytest.mark.parametrize(
        ("check", "args"),
        [
            pytest.param(ram._is_valid_commit, ("",), id="blank-sha"),
            pytest.param(ram._tag_advanced_past, ("", "v1.0.0"), id="no-marker"),
            pytest.param(ram._tag_advanced_past, ("deadbeef", ""), id="no-tag"),
        ],
    )
    def test_blank_input_is_false_without_git(self, git_run: Any, check: Any, args: tuple[str, ...]) -> None:
        """A blank sha or tag short-circuits to False and never reaches git.

        With nothing to compare there is no answer to ask git for; spawning it would only add latency.
        """
        assert check(*args) is False
        git_run.assert_not_called()

    @mock.patch("release_append_marker.subprocess.run", autospec=True)
    @mock.patch("release_append_marker.which", autospec=True, return_value=None)
    @pytest.mark.parametrize(
        ("check", "args"),
        [
            pytest.param(ram._is_valid_commit, ("deadbeef",), id="is-valid-commit"),
            pytest.param(ram._tag_advanced_past, ("deadbeef", "v2.0.0"), id="tag-advanced-past"),
        ],
    )
    def test_missing_git_is_false(self, which: Any, run: Any, check: Any, args: tuple[str, ...]) -> None:
        """Without git on PATH both checks answer False and spawn no subprocess.

        The caller then falls back to the last release tag instead of crashing on a machine without git.
        """
        assert check(*args) is False
        run.assert_not_called()

    @pytest.mark.parametrize(("check", "args", "ref"), _REACHABILITY_CHECKS)
    @pytest.mark.parametrize(("returncode", "expected"), _ANCESTOR_RC)
    def test_follows_ancestor_exit_code(
        self, git_run: Any, check: Any, args: tuple[str, ...], ref: str, returncode: int, expected: bool
    ) -> None:
        """Exit 0 from the ancestor check is True and exit 1 is False.

        For the marker-vs-HEAD check a False means history was rewritten; for marker-vs-tag a True means a release was
        cut at or after the marker.
        """
        git_run.return_value = _completed(returncode)
        assert check(*args) is expected

    @pytest.mark.parametrize(("check", "args", "ref"), _REACHABILITY_CHECKS)
    def test_asks_merge_base_is_ancestor_not_cat_file(
        self, git_run: Any, check: Any, args: tuple[str, ...], ref: str
    ) -> None:
        """Reachability is asked with ``merge-base --is-ancestor``, never ``cat-file -e`` (dangling-commit bug).

        ``cat-file -e`` only tests object-database existence: a commit orphaned by rebase or force-push stays reflog-
        protected for about 90 days and would still count as present, so a stale marker would pass as valid.
        """
        git_run.return_value = _completed(0)
        check(*args)
        assert [call.args[0] for call in git_run.call_args_list] == [_is_ancestor_argv("deadbeef", ref)]


class TestWrite:
    """The ``write`` command persists a marker only for a completed range bound to a receipt and the exact draft."""

    def test_persists_sha_creating_parent_dirs(self, tmp_path: Path, marker_git: Any, notes: Any) -> None:
        """A valid receipt creates the nested marker directory and stores the sha with a trailing newline.

        The marker directory does not exist yet (first run on a branch); the write must create it.
        """
        marker_dir = tmp_path / "nested" / ".temp"
        draft, receipt = notes()
        assert ram.main(_write_args("main", "deadbeef123", receipt, draft, "--marker-dir", str(marker_dir))) == 0
        assert ram._marker_path("main", str(marker_dir)).read_text(encoding="utf-8") == "deadbeef123\n"

    def test_without_receipt_keeps_marker(self, workdir: Path) -> None:
        """A draft and range file alone cannot advance the marker before a receipt exists.

        The receipt is the proof that the notes were completed for this exact range; a bare ``--sha`` is not.
        """
        marker = _seed_marker("main", b"OLD\n")
        (workdir / "DRAFT.md").write_text("completed notes\n", encoding="utf-8")
        (workdir / "completed-range").write_text("v1..deadbeef123\n", encoding="utf-8")
        assert ram.main(["write", "--branch", "main", "--sha", "deadbeef123"]) != 0
        assert marker.read_bytes() == b"OLD\n"

    @pytest.mark.parametrize(
        ("range_text", "draft_text", "sha"),
        [
            pytest.param("v1..deadbeef123", "", "deadbeef123", id="missing-draft"),
            pytest.param("v1..deadbeef123", "completed notes\n", "cafebabe123", id="different-endpoint"),
            pytest.param("v1...deadbeef123", "completed notes\n", "deadbeef123", id="three-dot-range"),
        ],
    )
    def test_refuses_incomplete_or_mismatched_range(
        self, workdir: Path, marker_git: Any, notes: Any, range_text: str, draft_text: str, sha: str
    ) -> None:
        """An empty draft, an endpoint that differs from the receipt, or a three-dot range keeps the old marker.

        Each is a completion input that does not describe the notes actually written, so the marker must not move.
        """
        marker = _seed_marker("main", b"OLD\n")
        draft, receipt = notes(range_text, draft_text)
        assert ram.main(_write_args("main", sha, receipt, draft)) == 1
        assert marker.read_bytes() == b"OLD\n"

    def test_refuses_symlink_marker_without_touching_external_bytes(
        self, tmp_path: Path, marker_git: Any, notes: Any
    ) -> None:
        """A marker symlink is refused and the external file it points to keeps its bytes.

        Writing through the link would let a planted symlink overwrite an arbitrary file with a commit sha.
        """
        external = tmp_path / "outside"
        external.write_bytes(b"KEEP\n")
        marker_dir = tmp_path / ".temp"
        marker = ram._marker_path("main", str(marker_dir))
        marker.parent.mkdir(parents=True)
        marker.symlink_to(external)
        draft, receipt = notes()
        assert ram.main(_write_args("main", "deadbeef123", receipt, draft, "--marker-dir", str(marker_dir))) == 1
        assert (external.read_bytes(), marker.is_symlink()) == (b"KEEP\n", True)


class TestRealHistory:
    """``receipt`` and ``write`` against real git repositories, where commit ancestry is the contract."""

    def test_write_on_detached_head_keeps_marker(self, history: _History) -> None:
        """A direct write on a detached HEAD must not change the branch's baseline.

        The marker belongs to an attached branch; recording a detached checkout would corrupt the next range.
        """
        marker = _seed_marker("main", b"ORIGINAL\n")
        _git(history.repo, "checkout", "-q", "--detach", "HEAD")
        draft = history.repo / "DRAFT.md"
        draft.write_text("completed notes\n", encoding="utf-8")
        receipt = history.repo / "receipt.json"
        _write_receipt(receipt, f"base..{history.head}", history.head, draft, head=history.head)
        assert ram.main(_write_args("main", history.head, receipt, draft)) == 1
        assert marker.read_bytes() == b"ORIGINAL\n"

    @pytest.fixture
    def stage(self, history: _History) -> Any:
        """Return a factory that writes ``DRAFT.md`` and ``range.txt`` for ``<start>..<head>``."""

        def build(start: str) -> tuple[Path, Path]:
            """Write the draft and the range file starting at ``start``."""
            draft = history.repo / "DRAFT.md"
            draft.write_bytes(b"Notes for the selected range\n")
            range_file = history.repo / "range.txt"
            range_file.write_text(f"{start}..{history.head}\n", encoding="utf-8")
            return draft, range_file

        return build

    @pytest.mark.parametrize("start_attr", ["unknown", "side", "second"])
    def test_receipt_refuses_unrepresented_start(self, history: _History, stage: Any, start_attr: str) -> None:
        """A range start that is unknown, on a disjoint branch, or past the saved marker gets no receipt.

        The saved marker is ``base``. Starting after it (``second``) would skip undrafted history; an unknown or
        disjoint start is not an ancestor of the endpoint at all.
        """
        _seed_marker(history.branch, (history.base + "\n").encode())
        draft, range_file = stage(getattr(history, start_attr))
        receipt = history.repo / "receipt.json"
        assert ram.main(_receipt_args(history.branch, range_file, draft, receipt)) == 1
        assert not receipt.exists()

    def test_receipt_refuses_start_skipping_first_baseline(self, history: _History, stage: Any) -> None:
        """Without a marker or tag the root commit is the baseline, and a later start skips it.

        A first release must cover the history from its root; starting at ``second`` leaves ``base`` undrafted.
        """
        draft, range_file = stage(history.second)
        receipt = history.repo / "receipt.json"
        assert ram.main(_receipt_args(history.branch, range_file, draft, receipt)) == 1
        assert not receipt.exists()

    @pytest.mark.parametrize("start_attr", ["unknown", "side", "second"])
    def test_write_refuses_unrepresented_start(self, history: _History, stage: Any, start_attr: str) -> None:
        """A receipt whose range start is unknown, disjoint or past the saved marker cannot advance the marker.

        The receipt is hand-built to look valid; the write re-checks the start against real ancestry.
        """
        saved = (history.base + "\n").encode()
        marker = _seed_marker(history.branch, saved)
        start = getattr(history, start_attr)
        draft, _ = stage(start)
        receipt = history.repo / "receipt.json"
        _write_receipt(
            receipt,
            f"{start}..{history.head}",
            history.head,
            draft,
            branch=history.branch,
            head=history.head,
            saved_marker_sha256=_digest(saved),
            baseline=history.base,
        )
        assert ram.main(_write_args(history.branch, history.head, receipt, draft)) == 1
        assert marker.read_bytes() == saved

    def test_write_refuses_start_skipping_first_baseline(self, history: _History, stage: Any) -> None:
        """With no saved marker, a receipt starting after the root commit leaves no marker behind.

        Without a marker the root commit is the baseline; a range that skips it must not create the first marker.
        """
        draft, _ = stage(history.second)
        receipt = history.repo / "receipt.json"
        _write_receipt(
            receipt,
            f"{history.second}..{history.head}",
            history.head,
            draft,
            branch=history.branch,
            head=history.head,
            baseline=None,
        )
        assert ram.main(_write_args(history.branch, history.head, receipt, draft)) == 1
        assert not ram._marker_path(history.branch, None).exists()

    def test_write_refuses_saved_marker_changed_after_receipt(self, history: _History, stage: Any) -> None:
        """A receipt cannot certify notes against a saved marker that changed after it was issued.

        The receipt records the digest of the marker it was derived from; moving the marker forward in between means the
        notes no longer start where the new baseline is.
        """
        marker = _seed_marker(history.branch, (history.base + "\n").encode())
        draft, range_file = stage(history.base)
        receipt = history.repo / "receipt.json"
        assert ram.main(_receipt_args(history.branch, range_file, draft, receipt)) == 0
        marker.write_bytes((history.head + "\n").encode())
        assert ram.main(_write_args(history.branch, history.head, receipt, draft)) == 1
        assert marker.read_bytes() == (history.head + "\n").encode()

    @pytest.mark.parametrize(
        ("endpoint_attr", "tag_args"),
        [
            pytest.param("base", [], id="root-baseline"),
            pytest.param("second", ["--last-tag", "v2"], id="selected-tag"),
        ],
    )
    def test_first_marker_uses_root_or_selected_tag(
        self, history: _History, endpoint_attr: str, tag_args: list[str]
    ) -> None:
        """A first release is accepted from its root baseline or from an explicitly selected later tag.

        No marker exists yet. The range is the single endpoint commit; the receipt then the write must both pass and
        record that endpoint as the first marker.
        """
        endpoint = getattr(history, endpoint_attr)
        draft = history.repo / "DRAFT.md"
        draft.write_bytes(b"Completed first notes\n")
        range_file = history.repo / "range.txt"
        range_file.write_text(f"{endpoint}..{endpoint}\n", encoding="utf-8")
        receipt = history.repo / "receipt.json"
        assert ram.main(_receipt_args(history.branch, range_file, draft, receipt, *tag_args)) == 0
        assert ram.main(_write_args(history.branch, endpoint, receipt, draft, *tag_args)) == 0
        assert ram._marker_path(history.branch, None).read_bytes() == (endpoint + "\n").encode()

    def test_first_marker_rejects_ambiguous_history_root(self, history: _History, stage: Any) -> None:
        """A branch merged with an unrelated history needs an explicit baseline, so no receipt is issued.

        Two root commits make "the root" ambiguous; guessing one could silently drop the other history's commits.
        """
        _git(history.repo, "checkout", "-q", "--orphan", "other")
        _commit(history.repo, "other-root")
        _git(history.repo, "checkout", "-q", history.branch)
        _git(history.repo, "merge", "-q", "--allow-unrelated-histories", "-s", "ours", "other", "-m", "merged")
        draft, range_file = stage(history.base)
        receipt = history.repo / "receipt.json"
        assert ram.main(_receipt_args(history.branch, range_file, draft, receipt)) == 1
        assert not receipt.exists()


class TestIsValidCli:
    """``is-valid`` prints true only for a marker that is on HEAD and not behind the last release tag."""

    def test_prints_false_without_marker(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """No marker on disk prints "false" and still exits 0.

        The answer is data for the caller's branching, not an error; git is never consulted.
        """
        rc = ram.main(["is-valid", "--branch", "main", "--last-tag", "v1.0.0", "--marker-dir", str(tmp_path)])
        assert (rc, capsys.readouterr().out.strip()) == (0, "false")

    @pytest.mark.parametrize(
        ("returncodes", "expected"),
        [
            pytest.param([0, 1], "true", id="on-head-tag-predates-marker"),
            pytest.param([0, 0], "false", id="superseded-by-later-tag"),
            pytest.param([1], "false", id="rebased-away"),
        ],
    )
    def test_reports_marker_validity(
        self,
        tmp_path: Path,
        git_run: Any,
        capsys: pytest.CaptureFixture[str],
        returncodes: list[int],
        expected: str,
    ) -> None:
        """A marker reachable from HEAD is valid only while no release tag landed at or after it.

        ``returncodes`` are the exits of the HEAD-ancestor check and, when reached, the tag-ancestor check. A tag cut at
        or after the marker (tag-blind fix) or a marker rebased out of HEAD both turn the answer to "false".
        """
        _seed_marker("main", b"deadbeef\n", tmp_path)
        git_run.side_effect = [_completed(rc) for rc in returncodes]
        rc = ram.main(["is-valid", "--branch", "main", "--last-tag", "v1.0.0", "--marker-dir", str(tmp_path)])
        assert (rc, capsys.readouterr().out.strip()) == (0, expected)


class TestResolve:
    """``resolve`` prints the ``--append`` range: ``<marker>..HEAD`` when the marker is usable, else ``<tag>..HEAD``."""

    def test_without_marker_falls_back_to_last_tag(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """With no prior marker the range is ``<last-tag>..HEAD`` and stderr notes the first baseline.

        The first ``--append`` run has nothing to resume from, so it starts from the last release tag.
        """
        rc = ram.main(["resolve", "--branch", "main", "--last-tag", "v1.2.0", "--marker-dir", str(tmp_path)])
        captured = capsys.readouterr()
        assert (rc, captured.out.strip(), "establishing first append baseline" in captured.err) == (
            0,
            "v1.2.0..HEAD",
            True,
        )

    @pytest.mark.parametrize(
        ("returncodes", "expected_range", "note"),
        [
            pytest.param([0, 1], "deadbeef1234..HEAD", "resuming from marker", id="valid-incremental"),
            pytest.param([1], "v1.2.0..HEAD", "not found in history", id="rebased-away-warns"),
            pytest.param([0, 0], "v1.2.0..HEAD", "superseded by a release tag", id="superseded-by-tag"),
        ],
    )
    def test_range_follows_marker_validity(
        self,
        tmp_path: Path,
        git_run: Any,
        capsys: pytest.CaptureFixture[str],
        returncodes: list[int],
        expected_range: str,
        note: str,
    ) -> None:
        """A usable marker gives an incremental range; a dangling or tag-superseded one falls back with a warning.

        ``returncodes`` are the HEAD-ancestor and (when reached) tag-ancestor exits. A release cut between the marker
        and now (tag-blind fix) overrides a stale-but-reachable marker, which would otherwise re-draft shipped commits.
        """
        _seed_marker("main", b"deadbeef1234\n", tmp_path)
        git_run.side_effect = [_completed(rc) for rc in returncodes]
        rc = ram.main(["resolve", "--branch", "main", "--last-tag", "v1.2.0", "--marker-dir", str(tmp_path)])
        captured = capsys.readouterr()
        assert (rc, captured.out.strip(), note in captured.err) == (0, expected_range, True)

    def test_checks_reachability_against_head_then_tag(self, tmp_path: Path, git_run: Any) -> None:
        """A usable marker is checked against ``HEAD`` first and then against the latest tag.

        The order matters: the tag comparison is only meaningful for a marker that is already on HEAD.
        """
        _seed_marker("main", b"deadbeef\n", tmp_path)
        git_run.return_value = _completed(0)
        ram.main(["resolve", "--branch", "main", "--last-tag", "v1.2.0", "--marker-dir", str(tmp_path)])
        assert [call.args[0] for call in git_run.call_args_list] == [
            _is_ancestor_argv("deadbeef", "HEAD"),
            _is_ancestor_argv("deadbeef", "v1.2.0"),
        ]
