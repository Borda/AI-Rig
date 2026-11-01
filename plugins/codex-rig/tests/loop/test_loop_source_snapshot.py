"""Real-Git acceptance checks for loop source snapshots and local review worktrees."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import runpy
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
COLLECT_DIFF = PLUGIN_ROOT / "shared" / "collect_diff.py"


def _git(repository: Path, *arguments: str) -> None:
    """Run one successful local Git command in a disposable repository."""
    completed = subprocess.run(["git", *arguments], cwd=repository, capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr


def _status(repository: Path, *flags: str) -> bytes:
    """Return the NUL-separated porcelain status of `repository`."""
    return subprocess.check_output(["git", "status", "--porcelain", "-z", *flags], cwd=repository)


def _run_collector(*arguments: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run the standalone collector CLI in a subprocess."""
    return subprocess.run(
        [sys.executable, str(COLLECT_DIFF), *arguments], cwd=cwd, capture_output=True, text=True, check=False
    )


def _load_collector() -> ModuleType:
    """Load the standalone collector without installing the plugin."""
    specification = importlib.util.spec_from_file_location("loop_source_snapshot", COLLECT_DIFF)
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _supports_symlinks() -> bool:
    """Probe whether this host can create a local symbolic link during collection."""
    with tempfile.TemporaryDirectory() as directory:
        link = Path(directory) / "link"
        try:
            link.symlink_to("target")
        except OSError:
            return False
        return link.is_symlink()


_skip_without_symlinks = pytest.mark.skipif(not _supports_symlinks(), reason="host cannot create symbolic links")


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """Return an initialized repository with a committer identity and no commits."""
    root = tmp_path / "repository"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Snapshot fixture")
    _git(root, "config", "user.email", "snapshot@example.invalid")
    return root


@pytest.fixture
def committed_source_repository(repository: Path) -> Path:
    """Return a repository whose only commit tracks one source file."""
    (repository / "source.py").write_text("original\n", encoding="utf-8")
    _git(repository, "add", "source.py")
    _git(repository, "commit", "-qm", "fixture")
    return repository


@pytest.fixture
def reports_ignored_repository(repository: Path) -> Path:
    """Return a repository whose only commit ignores the `.reports/` artifact directory."""
    (repository / ".gitignore").write_bytes(b".reports/\n")
    _git(repository, "add", ".gitignore")
    _git(repository, "commit", "-qm", "fixture")
    return repository


def _move_head(repository: Path) -> None:
    """Advance HEAD without touching the index or any worktree byte."""
    _git(repository, "commit", "--allow-empty", "-qm", "moved")


def _add_untracked_file(repository: Path) -> None:
    """Create a new untracked, non-ignored source file."""
    (repository / "added.py").write_text("added\n", encoding="utf-8")


def _restage_executable_mode(repository: Path) -> None:
    """Change only the staged mode of the tracked file, leaving its bytes and worktree untouched."""
    _git(repository, "update-index", "--chmod=+x", "source.py")


def _initialize_repository(repository: Path) -> None:
    """Create a repository without any commit, leaving HEAD unborn."""
    _git(repository, "init", "-q")


def _leave_plain_directory(repository: Path) -> None:
    """Leave the directory outside any Git repository."""


class TestCaptureSourceSnapshot:
    """`capture_source_snapshot` records selected source bytes, kinds and Git state from a real repository."""

    @pytest.mark.parametrize("newline", ["\n", "\r\n"])
    def test_binds_worktree_contents_and_index(self, repository: Path, newline: str) -> None:
        """Selected source bytes and staged state are preserved for either newline convention on every host.

        The tree holds a modified-after-staging file, an untracked nested file, a binary file and a tracked file deleted
        from the worktree; the snapshot must record worktree bytes, a missing record, and the index digest.
        """
        source = repository / "source"
        source.mkdir()
        base_content = f"value = 'base'{newline}"
        new_content = f"new source{newline}"
        (source / "module.py").write_bytes(base_content.encode("utf-8"))
        (source / "staged.py").write_bytes(base_content.encode("utf-8"))
        (repository / "removed.py").write_bytes(f"removed = True{newline}".encode())
        _git(repository, "add", "source", "removed.py")
        _git(repository, "commit", "-qm", "fixture")
        (source / "staged.py").write_bytes(f"value = 'staged'{newline}".encode())
        _git(repository, "add", "source/staged.py")
        (source / "staged.py").write_bytes(base_content.encode("utf-8"))
        (source / "nested").mkdir()
        (source / "nested" / "new.txt").write_bytes(new_content.encode("utf-8"))
        (source / "data.bin").write_bytes(b"\x00\xffsource")
        (repository / "removed.py").unlink()

        snapshot = _load_collector().capture_source_snapshot(repository, ["removed.py", "source"])

        assert snapshot["schema_version"] == 1
        assert snapshot["repository"] == repository.resolve().as_posix()
        assert snapshot["scope_paths"] == ["removed.py", "source"]
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
        assert snapshot["revision"] == head
        assert snapshot["index_sha256"] != hashlib.sha256(b"").hexdigest()
        assert snapshot["files"] == [
            {
                "path": "removed.py",
                "kind": "missing",
                "sha256": None,
                "executable": False,
                "encoding": "utf-8",
                "content": "",
            },
            {
                "path": "source/data.bin",
                "kind": "file",
                "sha256": hashlib.sha256(b"\x00\xffsource").hexdigest(),
                "executable": False,
                "encoding": "base64",
                "content": "AP9zb3VyY2U=",
            },
            {
                "path": "source/module.py",
                "kind": "file",
                "sha256": hashlib.sha256(base_content.encode("utf-8")).hexdigest(),
                "executable": False,
                "encoding": "utf-8",
                "content": base_content,
            },
            {
                "path": "source/nested/new.txt",
                "kind": "file",
                "sha256": hashlib.sha256(new_content.encode("utf-8")).hexdigest(),
                "executable": False,
                "encoding": "utf-8",
                "content": new_content,
            },
            {
                "path": "source/staged.py",
                "kind": "file",
                "sha256": hashlib.sha256(base_content.encode("utf-8")).hexdigest(),
                "executable": False,
                "encoding": "utf-8",
                "content": base_content,
            },
        ]

    def test_cli_writes_canonical_json_bytes(self, committed_source_repository: Path, tmp_path: Path) -> None:
        """The `--snapshot` command serializes the returned snapshot with one stable JSON representation.

        The CLI is run for real and its output file must equal the sorted, indented, ASCII-only dump of the API result.
        """
        output = tmp_path / "snapshot.json"

        completed = _run_collector(
            "--snapshot",
            "--repository",
            str(committed_source_repository),
            "--scope-path",
            "source.py",
            "--out",
            str(output),
        )

        assert completed.returncode == 0, completed.stderr
        snapshot = _load_collector().capture_source_snapshot(committed_source_repository, ["source.py"])
        expected = json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
        assert output.read_text(encoding="utf-8") == expected

    def test_rejects_content_change_during_capture(
        self, committed_source_repository: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An edited tracked file is rejected even when its name and index entry stay stable.

        The file is rewritten right after its bytes were read into the candidate snapshot, so the second read of the
        working tree disagrees with the first.
        """
        source = committed_source_repository / "source.py"
        module = _load_collector()
        original_record = module._source_record

        def edit_after_read(root: Path, path: str) -> dict[str, object]:
            """Change the source after its bytes have entered the candidate snapshot."""
            record = original_record(root, path)
            source.write_text("modified\n", encoding="utf-8")
            return record

        monkeypatch.setattr(module, "_source_record", edit_after_read)

        with pytest.raises(RuntimeError, match="changed while capturing"):
            module.capture_source_snapshot(committed_source_repository, ["source.py"])

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(_move_head, id="head-moves"),
            pytest.param(_add_untracked_file, id="untracked-file-appears"),
            pytest.param(_restage_executable_mode, id="index-entry-changes"),
        ],
    )
    def test_rejects_git_state_change_during_capture(
        self,
        committed_source_repository: Path,
        monkeypatch: pytest.MonkeyPatch,
        mutate: Callable[[Path], None],
    ) -> None:
        """A Git-state change that leaves every captured file byte identical is still rejected.

        The mutation runs right after the first read of each source file, so the revision, the index, or the untracked
        inventory differs between the two reads while the bytes of the tracked file do not. Each view must keep its own
        post-read comparison for the capture to fail.
        """
        module = _load_collector()
        original_record = module._source_record

        def record_then_mutate(root: Path, path: str) -> dict[str, object]:
            """Return the first-read record, then change one Git view."""
            record = original_record(root, path)
            mutate(root)
            return record

        monkeypatch.setattr(module, "_source_record", record_then_mutate)

        with pytest.raises(RuntimeError, match="changed while capturing"):
            module.capture_source_snapshot(committed_source_repository, ["."])

    def test_reads_each_git_view_only_as_often_as_needed(
        self, committed_source_repository: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One combined top-level and HEAD lookup and two reads of every other Git view are spawned.

        A capture runs on every review and validation step, so each extra Git process multiplies across the workflow and
        costs most on Windows. The HEAD tree listing is still read twice: a HEAD that moves away and back during the
        capture is caught only when one of the two listings lands on the other commit.
        """
        module = _load_collector()
        real_run = subprocess.run
        subcommands: list[str] = []

        def record_run(argv: list[str], *args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            """Record the Git subcommand, then run it unchanged."""
            subcommands.append(argv[4])
            return real_run(argv, *args, **kwargs)

        monkeypatch.setattr(module.subprocess, "run", record_run)

        module.capture_source_snapshot(committed_source_repository, ["source.py"])

        assert sorted(subcommands) == ["ls-files"] * 4 + ["ls-tree"] * 2 + ["rev-parse"] * 2

    @pytest.mark.parametrize(
        ("prepare", "failed_command"),
        [
            pytest.param(_initialize_repository, "rev-parse --verify HEAD", id="unborn-head"),
            pytest.param(_leave_plain_directory, "rev-parse --show-toplevel", id="not-a-repository"),
        ],
    )
    def test_names_failed_git_lookup(
        self, tmp_path: Path, prepare: Callable[[Path], None], failed_command: str
    ) -> None:
        """An unusable repository is reported through the single Git lookup that failed, not the combined call.

        The top-level and HEAD lookups share one Git process on the success path; when it fails, callers must still see
        the message of the individual lookup that cannot be satisfied.
        """
        root = tmp_path / "repository"
        root.mkdir()
        prepare(root)

        message = re.escape(f"Git source snapshot command failed: {failed_command}")
        with pytest.raises(RuntimeError, match=f"^{message}$"):
            _load_collector().capture_source_snapshot(root, ["source.py"])

    @pytest.mark.parametrize("scope_path", ["/outside", "../outside", ":(top)source", "C:\\outside"])
    def test_rejects_unsafe_scope_paths(self, repository: Path, scope_path: str) -> None:
        """Paths that could escape the repository or activate Git pathspec magic are rejected.

        Absolute, parent-relative, magic-pathspec and Windows drive paths must never reach Git.
        """
        with pytest.raises(ValueError, match="scope"):
            _load_collector().capture_source_snapshot(repository, [scope_path])

    @_skip_without_symlinks
    def test_hashes_symlink_target_without_following(self, repository: Path) -> None:
        """A symlink is represented by its target bytes even when the target does not exist.

        A dangling link must be recorded as kind `symlink` with the hash of its target text instead of failing.
        """
        (repository / "broken-link").symlink_to("missing-target")
        _git(repository, "add", "broken-link")
        _git(repository, "commit", "-qm", "fixture")

        snapshot = _load_collector().capture_source_snapshot(repository, ["broken-link"])

        assert snapshot["files"] == [
            {
                "path": "broken-link",
                "kind": "symlink",
                "sha256": hashlib.sha256(b"missing-target").hexdigest(),
                "executable": False,
                "encoding": "utf-8",
                "content": "missing-target",
            }
        ]

    @_skip_without_symlinks
    def test_rejects_scope_symlink_escaping_repository(self, repository: Path, tmp_path: Path) -> None:
        """A selected symlink whose resolution leaves the repository is refused.

        A tracked link to a directory outside the repository must not let a snapshot read foreign files.
        """
        external = tmp_path / "external"
        external.mkdir()
        (repository / "external-link").symlink_to(external)
        _git(repository, "add", "external-link")
        _git(repository, "commit", "-qm", "fixture")

        with pytest.raises(ValueError, match="escapes repository"):
            _load_collector().capture_source_snapshot(repository, ["external-link"])


class TestReviewWorktree:
    """The local review worktree mirrors the caller's changed bytes without disturbing the source repository."""

    def test_output_guard_uses_real_git_ignore_rules(self, repository: Path) -> None:
        """Ignored artifact paths are accepted while visible paths are rejected before checkout.

        The guard asks real Git whether the report directory is ignored, so a `.gitignore` entry decides the verdict.
        """
        (repository / ".gitignore").write_text(".reports/\n", encoding="utf-8")
        collector = _load_collector()

        collector._require_ignored_inside_repository(repository, repository / ".reports" / "codex" / "review")
        with pytest.raises(ValueError, match="must be ignored"):
            collector._require_ignored_inside_repository(repository, repository / "visible" / "review")

    @pytest.mark.parametrize(
        "current_content",
        [
            pytest.param(b"staged content\n", id="unedited-after-staging"),
            pytest.param(b"current content\n", id="edited-after-staging"),
        ],
    )
    def test_includes_staged_new_file(self, reports_ignored_repository: Path, current_content: bytes) -> None:
        """A staged addition and its later edits are mirrored without changing the source index.

        The new executable file is staged with one content and left on disk with `current_content`; the mirrored
        worktree must hold the on-disk bytes and mode, the patches must match Git's, and the caller must be untouched.
        """
        repository = reports_ignored_repository
        added = repository / "added.py"
        added.write_bytes(b"staged content\n")
        added.chmod(added.stat().st_mode | stat.S_IXUSR)
        _git(repository, "add", "added.py")
        added.write_bytes(current_content)
        original_status = _status(repository, "--untracked-files=all")
        expected_patch = subprocess.check_output(["git", "diff", "--binary", "HEAD", "--"], cwd=repository)
        expected_staged = subprocess.check_output(["git", "diff", "--cached", "--binary", "HEAD", "--"], cwd=repository)
        report = repository / ".reports" / "codex" / "review"

        collection = _run_collector("--review-worktree", "--repository", str(repository), "--out", str(report))

        assert collection.returncode == 0, collection.stderr
        review = Path(json.loads((report / "review-worktree.json").read_bytes())["review_worktree"])
        assert (review / "added.py").read_bytes() == added.read_bytes()
        assert bool((review / "added.py").stat().st_mode & stat.S_IXUSR) == bool(added.stat().st_mode & stat.S_IXUSR)
        assert (report / "diff.patch").read_bytes() == expected_patch
        assert (report / "staged.patch").read_bytes() == expected_staged
        assert subprocess.check_output(["git", "diff", "--binary", "HEAD", "--"], cwd=review) == expected_patch
        assert _status(repository, "--untracked-files=all") == original_status
        _load_collector().verify_review_worktree(report)

    @pytest.mark.integration
    def test_staged_addition_reaches_review_preparation(self, reports_ignored_repository: Path) -> None:
        """A staged addition and its later edit are delivered through the ordinary review context producer.

        The real collector, review worktree and `review_prepare.py` run in sequence; every role context must contain the
        current file bytes and the matching diff excerpt, and the caller's repository must stay untouched.
        """
        repository = reports_ignored_repository
        added = repository / "added.py"
        added.write_bytes(b"value = 'staged'\n")
        _git(repository, "add", "added.py")
        added.write_bytes(b"value = 'current'\n")
        original_status = _status(repository)
        run = repository / ".reports" / "review"
        context = _run_collector("--scope", "working-tree", "--out", str(run), cwd=repository)
        assert context.returncode == 0, context.stderr
        collected = _run_collector(
            "--review-worktree", "--repository", str(repository), "--out", str(run / "local-source")
        )
        assert collected.returncode == 0, collected.stderr
        receipt = json.loads((run / "local-source" / "review-worktree.json").read_bytes())
        skill = PLUGIN_ROOT / "skills" / "code-review"
        signal_names = runpy.run_path(str(skill / "review_routing.py"))["ROUTING_SIGNALS"]
        roles = ["qa-specialist", "sw-engineer"]
        routing = {
            "schema_version": 1,
            "risk_tier": "LOCAL",
            "signals": {name: name == "behavior_change" for name in signal_names},
            "signal_evidence": {name: ["Review the staged addition."] for name in signal_names},
            "triggered_roles": roles,
            "trigger_reasons": {role: ["Inspect changed Python source."] for role in roles},
        }
        (run / "review-routing.json").write_text(json.dumps(routing), encoding="utf-8", newline="\n")
        (run / "brief.md").write_text("Review the added source.\n", encoding="utf-8", newline="\n")
        briefs = {role: {"axis": role, "evidence_path": "brief.md", "source_paths": ["added.py"]} for role in roles}
        (run / "review-briefs.json").write_text(json.dumps(briefs), encoding="utf-8", newline="\n")

        prepared = subprocess.run(
            [
                sys.executable,
                str(skill / "review_prepare.py"),
                "prepare",
                "--out",
                str(run),
                "--run-id",
                "test",
                "--parent-thread-id",
                "parent",
                "--source-root",
                receipt["review_worktree"],
                "--paged",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        assert prepared.returncode == 0, prepared.stderr
        plan = json.loads((run / "inspection-plan.json").read_bytes())
        assert {context["role_id"] for context in plan["contexts"]} == set(roles)
        contents = [(run / context["context_path"]).read_text(encoding="utf-8") for context in plan["contexts"]]
        assert all(
            "### added.py (file," in content
            and "value = 'current'" in content
            and "Matching diff excerpt:" in content
            and "+value = 'current'" in content
            for content in contents
        )
        assert _status(repository) == original_status

    @pytest.mark.parametrize("autocrlf", ["false", "true", "input"])
    @pytest.mark.parametrize("newline", [pytest.param(b"\n", id="lf"), pytest.param(b"\r\n", id="crlf")])
    def test_preserves_caller_and_mirrors_changed_bytes(self, repository: Path, autocrlf: str, newline: bytes) -> None:
        """Caller and mirrored bytes survive even when Git normalizes tracked line endings, and tampering is caught.

        A staged edit, an unstaged edit, a changed binary, a deletion and a new binary are mirrored byte for byte under
        each `core.autocrlf` setting while the caller's tree and status stay identical. The same worktree is then
        verified through the API and CLI, and a changed file, a stray file and a switched branch must each be reported.
        This is one lifecycle (collect, verify, tamper) because the later steps need the earlier receipt.
        """
        _git(repository, "config", "core.autocrlf", autocrlf)
        (repository / ".gitignore").write_text(".reports/\n", encoding="utf-8")
        staged = repository / "staged.py"
        unstaged = repository / "unstaged.py"
        tracked_binary = repository / "tracked.bin"
        removed = repository / "removed.py"
        staged.write_bytes(b"before staged\n")
        unstaged.write_bytes(b"before unstaged\n")
        tracked_binary.write_bytes(b"\x00old")
        removed.write_bytes(b"removed source\n")
        _git(repository, "add", ".gitignore", "staged.py", "unstaged.py", "tracked.bin", "removed.py")
        _git(repository, "commit", "-qm", "fixture")
        staged_bytes = b"after staged" + newline
        unstaged_bytes = b"after unstaged" + newline
        staged.write_bytes(staged_bytes)
        _git(repository, "add", "staged.py")
        unstaged.write_bytes(unstaged_bytes)
        tracked_binary.write_bytes(b"\x00\xffupdated")
        removed.unlink()
        (repository / "binary.bin").write_bytes(b"\x00\xffnew")
        original_status = _status(repository, "--untracked-files=all")
        report = repository / ".reports" / "codex" / "review"

        collection = _run_collector("--review-worktree", "--repository", str(repository), "--out", str(report))
        assert collection.returncode == 0, collection.stderr
        receipt = json.loads((report / "review-worktree.json").read_text(encoding="utf-8"))
        review = Path(str(receipt["review_worktree"]))

        assert receipt["source_worktree"] == repository.resolve().as_posix()
        assert review != repository.resolve()
        assert (review / "staged.py").read_bytes() == staged_bytes
        assert (review / "unstaged.py").read_bytes() == unstaged_bytes
        assert (review / "tracked.bin").read_bytes() == b"\x00\xffupdated"
        assert (review / "binary.bin").read_bytes() == b"\x00\xffnew"
        assert not (review / "removed.py").exists()
        assert not removed.exists()
        assert staged.read_bytes() == staged_bytes
        assert unstaged.read_bytes() == unstaged_bytes
        assert tracked_binary.read_bytes() == b"\x00\xffupdated"
        assert _status(repository, "--untracked-files=all") == original_status
        assert (report / "staged.patch").read_bytes().find(b"after staged") > 0
        assert json.loads((report / "review-worktree.json").read_text(encoding="utf-8")) == receipt
        collector = _load_collector()
        collector.verify_review_worktree(report)
        with pytest.raises(ValueError, match="path is occupied"):
            collector.collect_review_worktree(repository, report)
        assert (review / "binary.bin").read_bytes() == b"\x00\xffnew"
        verification = _run_collector("--verify-review-worktree", "--out", str(report))
        assert verification.returncode == 0, verification.stderr
        (review / "staged.py").write_bytes(b"changed during review\n")
        with pytest.raises(RuntimeError, match="Review worktree source changed"):
            collector.verify_review_worktree(report)
        verification = _run_collector("--verify-review-worktree", "--out", str(report))
        assert verification.returncode != 0
        assert "Review worktree source changed" in verification.stderr
        (review / "staged.py").write_bytes(staged_bytes)
        (review / "unexpected.py").write_text("new source\n", encoding="utf-8")
        with pytest.raises(RuntimeError, match="Review worktree status changed"):
            collector.verify_review_worktree(report)
        _git(review, "switch", "-c", "review-test-branch")
        with pytest.raises(RuntimeError, match="no longer detached"):
            collector.verify_review_worktree(report)
