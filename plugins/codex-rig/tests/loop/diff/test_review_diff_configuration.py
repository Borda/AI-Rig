"""Exercise review patch consistency with real local Git configuration.

Every test drives the shipped diff collector, evidence validator and review preparation against a throwaway Git
repository, proving that user-level Git presentation settings neither change nor break the reviewed patch bytes.
"""

from __future__ import annotations

import functools
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from types import ModuleType
from typing import NamedTuple

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
GitRunner = Callable[..., bytes]


class RenamedReview(NamedTuple):
    """A staged rename already collected into a local review worktree."""

    run: Path
    report: Path
    paths: list[str]


class CommitHistory(NamedTuple):
    """A base and tip commit pair with the per-commit collection directory."""

    paths: list[str]
    base: str
    head: str
    output: Path


def _module(name: str, path: Path) -> ModuleType:
    """Load a shipped helper by its source path."""
    specification = importlib.util.spec_from_file_location(name, path)
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


def _git(repository: Path, *arguments: str) -> bytes:
    """Run real Git in ``repository`` and return stdout, surfacing stderr on failure."""
    result = subprocess.run(["git", *arguments], cwd=repository, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    return result.stdout


def _configure(git: GitRunner, settings: Mapping[str, str]) -> None:
    """Apply each local Git configuration setting."""
    for key, value in settings.items():
        git("config", key, value)


def _set_environment(monkeypatch: pytest.MonkeyPatch, variables: Mapping[str, str]) -> None:
    """Set each environment variable for the rest of the test."""
    for name, value in variables.items():
        monkeypatch.setenv(name, value)


def _legacy_receipt(path: Path, **overrides: str) -> None:
    """Rewrite a collected receipt into the historical schema that recorded no diff format."""
    receipt = json.loads(path.read_text())
    receipt["schema_version"] = 1
    del receipt["diff_format"]
    receipt.update(overrides)
    path.write_text(json.dumps(receipt))


@pytest.fixture
def isolated_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide system and global Git configuration and expose the shared helper directory."""
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    monkeypatch.syspath_prepend(str(PLUGIN_ROOT / "shared"))


@pytest.fixture
def collector(isolated_git: None) -> ModuleType:
    """Load the shipped diff collector."""
    return _module("review_config_collector", PLUGIN_ROOT / "shared" / "collect_diff.py")


@pytest.fixture
def validator(isolated_git: None) -> ModuleType:
    """Load the shipped challenge evidence validator."""
    return _module("review_config_validator", PLUGIN_ROOT / "skills" / "challenge-resolve" / "validate_evidence.py")


@pytest.fixture
def prepare(isolated_git: None) -> ModuleType:
    """Load the shipped review preparation helper."""
    return _module("review_config_prepare", PLUGIN_ROOT / "skills" / "code-review" / "review_prepare.py")


@pytest.fixture
def repository(tmp_path: Path, isolated_git: None) -> Path:
    """Create an empty Git repository with a committer identity."""
    root = tmp_path / "repository"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Diff configuration fixture")
    _git(root, "config", "user.email", "diff@example.invalid")
    return root


@pytest.fixture
def git(repository: Path) -> GitRunner:
    """Real Git bound to the fixture repository."""
    return functools.partial(_git, repository)


@pytest.fixture
def renamed_review(tmp_path: Path, repository: Path, git: GitRunner, collector: ModuleType) -> RenamedReview:
    """Stage an ``old.py`` to ``new.py`` rename and collect it into a local review worktree."""
    (repository / "old.py").write_text("value = 1\n")
    git("add", "old.py")
    git("commit", "-qm", "base")
    git("mv", "old.py", "new.py")
    run = tmp_path / "run"
    report = run / "local-source"
    collector.collect_review_worktree(repository, report)
    return RenamedReview(run, report, ["new.py", "old.py"])


@pytest.fixture(
    params=[
        pytest.param(("diff.noprefix", "true"), id="no-prefix"),
        pytest.param(("color.diff", "always"), id="forced-color"),
    ]
)
def commit_history(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repository: Path, git: GitRunner
) -> CommitHistory:
    """Commit text, binary and mode-only changes, then enable one presentation setting locally."""
    git("config", "core.filemode", "false")
    paths = ["module.py", "binary.dat", "mode.py"]
    (repository / paths[0]).write_bytes(b"before = 0\nvalue = 1\nafter = 0\n")
    (repository / paths[1]).write_bytes(b"\x00before")
    (repository / paths[2]).write_bytes(b"value = 1\n")
    git("add", *paths)
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD").decode().strip()
    (repository / paths[0]).write_bytes(b"before = 0\nvalue = 2\nafter = 0\n")
    (repository / paths[1]).write_bytes(b"\x00after")
    git("add", *paths)
    git("update-index", "--chmod=+x", paths[2])
    git("commit", "-qm", "tip")
    head = git("rev-parse", "HEAD").decode().strip()
    git("checkout", "--detach", head)
    git("config", *request.param)
    monkeypatch.chdir(repository)
    return CommitHistory(paths, base, head, tmp_path / "collected")


class TestWorkingTreePatch:
    """The working-tree patch collected for review."""

    @pytest.mark.parametrize(
        ("config", "environment", "workdir"),
        [
            pytest.param({}, {}, "", id="default"),
            pytest.param({"color.diff": "always"}, {}, "", id="forced-color"),
            pytest.param({"diff.noprefix": "true"}, {}, "", id="no-prefix"),
            pytest.param({"diff.mnemonicPrefix": "true"}, {}, "", id="mnemonic-prefix"),
            pytest.param({"core.quotePath": "false"}, {}, "", id="unicode-path"),
            pytest.param({"diff.context": "0"}, {}, "", id="zero-context"),
            pytest.param({}, {"GIT_DIFF_OPTS": "--unified=0"}, "", id="environment-zero-context"),
            pytest.param({"diff.relative": "true"}, {}, "subdirectory", id="relative-subdirectory"),
        ],
    )
    def test_stable_under_local_git_settings(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        repository: Path,
        git: GitRunner,
        collector: ModuleType,
        validator: ModuleType,
        config: Mapping[str, str],
        environment: Mapping[str, str],
        workdir: str,
    ) -> None:
        """Git presentation settings never change or break the reviewed patch.

        A repository with a modified ASCII file and a modified non-ASCII path is reviewed while one local setting
        (color, prefixes, quoting, context, relative paths or ``GIT_DIFF_OPTS``) is active. The collector, the scoped
        comparison and the review mirror must all reproduce the canonical patch Git emits with fixed arguments.
        """
        paths = ["module.py", "calfé.py"]
        (repository / paths[0]).write_bytes(b"before = 0\nvalue = 1\nafter = 0\n")
        (repository / paths[1]).write_bytes(b"value = 1\n")
        git("add", *paths)
        git("commit", "-qm", "fixture")
        (repository / paths[0]).write_bytes(b"before = 0\nvalue = 2\nafter = 0\n")
        (repository / paths[1]).write_bytes(b"value = 2\n")
        _configure(git, config)
        monkeypatch.delenv("GIT_DIFF_OPTS", raising=False)
        expected = git(
            "-c",
            "core.quotePath=true",
            "diff",
            "--binary",
            "--no-renames",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            "--unified=3",
            "HEAD",
            "--",
            *paths,
        )
        _set_environment(monkeypatch, environment)
        working_directory = repository / workdir
        working_directory.mkdir(exist_ok=True)
        monkeypatch.chdir(working_directory)
        collected = tmp_path / "collected"

        assert collector.collect_diff("working-tree", "", collected) == 0
        snapshot = collector.capture_source_snapshot(repository, paths)
        compared = validator._current_scoped_diff(repository, paths, snapshot, "capture-failed")
        collector.collect_review_worktree(repository, tmp_path / "mirror")
        collector.verify_review_worktree(tmp_path / "mirror")

        assert (collected / "diff.patch").read_bytes() == expected
        assert compared == expected
        assert (tmp_path / "mirror" / "diff.patch").read_bytes() == expected


class TestReceiptVerification:
    """Review worktree receipts written before the canonical diff format was recorded."""

    @pytest.fixture
    def modified_unicode_source(self, repository: Path, git: GitRunner) -> Path:
        """Commit a non-ASCII file, then edit it without committing."""
        source = repository / "calfé.py"
        source.write_bytes(b"value = 1\n")
        git("add", source.name)
        git("commit", "-qm", "base")
        source.write_bytes(b"value = 2\n")
        return source

    def test_old_default_receipt_verifies_untouched(
        self, tmp_path: Path, collector: ModuleType, modified_unicode_source: Path
    ) -> None:
        """A receipt from the old default format still verifies and is left byte-for-byte unchanged.

        Reviews collected before the diff format was recorded must stay valid; verification may not rewrite them.
        """
        report = tmp_path / "old-default"
        collector.collect_review_worktree(modified_unicode_source.parent, report)
        receipt_path = report / "review-worktree.json"
        _legacy_receipt(receipt_path)
        before = receipt_path.read_bytes()

        collector.verify_review_worktree(report)

        assert receipt_path.read_bytes() == before

    def test_old_receipt_with_transformed_patch_requires_recollection(
        self, tmp_path: Path, git: GitRunner, collector: ModuleType, modified_unicode_source: Path
    ) -> None:
        """An old receipt whose patch bytes differ from the canonical form is rejected and preserved.

        With ``core.quotePath=false`` native Git output differs from the canonical patch; blessing it silently would
        accept transformed bytes, so the reviewer must recollect.
        """
        git("config", "core.quotePath", "false")
        changed = tmp_path / "old-unquoted"
        collector.collect_review_worktree(modified_unicode_source.parent, changed)
        receipt_path = changed / "review-worktree.json"
        _legacy_receipt(receipt_path, diff_sha256=hashlib.sha256(git("diff", "--binary", "HEAD", "--")).hexdigest())
        preserved = receipt_path.read_bytes()

        with pytest.raises(RuntimeError, match="patch changed"):
            collector.verify_review_worktree(changed)

        assert receipt_path.read_bytes() == preserved


class TestStagedRename:
    """A staged rename must stay a deletion plus an addition through local review and challenge."""

    def test_patch_records_deletion_and_addition(
        self, collector: ModuleType, validator: ModuleType, repository: Path, renamed_review: RenamedReview
    ) -> None:
        """The collected patch and the challenge comparison both carry the deleted and the added path.

        Collapsing a rename into one entry would hide the old path from review; both sides must appear in the patch, the
        scope paths and the challenge's own diff.
        """
        _, report, paths = renamed_review
        collector.verify_review_worktree(report)
        snapshot = collector.capture_source_snapshot(repository, paths)

        diff = validator._current_scoped_diff(repository, paths, snapshot, "failed")

        assert diff == (report / "diff.patch").read_bytes()
        assert b"deleted file mode" in diff
        assert b"new file mode" in diff
        assert set(json.loads((report / "review-worktree.json").read_text())["scope_paths"]) == set(paths)

    def test_preparation_covers_both_paths(
        self, collector: ModuleType, prepare: ModuleType, repository: Path, renamed_review: RenamedReview
    ) -> None:
        """Review preparation reports both rename paths as changed against the snapshot revision."""
        run, report, paths = renamed_review
        snapshot = collector.capture_source_snapshot(repository, paths)
        (run / "diff.patch").write_bytes((report / "diff.patch").read_bytes())
        mirror = Path(json.loads((report / "review-worktree.json").read_text())["review_worktree"])
        (run / "files.txt").write_bytes(
            subprocess.run(
                ["git", "-C", str(mirror), "diff", "--no-renames", "--name-only", "HEAD", "--"],
                capture_output=True,
                check=True,
            ).stdout
        )

        _, changed, comparison_base = prepare._source_snapshot(run, mirror, None, paths, None, None)

        assert changed == set(paths)
        assert comparison_base == snapshot["revision"]

    def test_verification_rejects_dropped_rename_path(
        self, collector: ModuleType, renamed_review: RenamedReview
    ) -> None:
        """Dropping the old path from both the receipt and the snapshot fails the inventory check.

        The deleted side of a rename is part of the inventory; a coherent edit removing it must still be rejected.
        """
        report = renamed_review.report
        receipt_path = report / "review-worktree.json"
        snapshot_path = report / "source-snapshot.json"
        receipt = json.loads(receipt_path.read_text())
        source = json.loads(snapshot_path.read_text())
        receipt["scope_paths"].remove("old.py")
        source["scope_paths"].remove("old.py")
        source["files"] = [record for record in source["files"] if record["path"] != "old.py"]
        receipt_path.write_text(json.dumps(receipt))
        snapshot_path.write_text(json.dumps(source))

        with pytest.raises(RuntimeError, match="inventory"):
            collector.verify_review_worktree(report)


class TestNativeGitOutput:
    """Git output requested without canonical arguments."""

    def test_uncanonicalized_output_keeps_native_presentation(
        self, repository: Path, git: GitRunner, collector: ModuleType
    ) -> None:
        """Callers that do not request canonical diff arguments receive Git's native, colored output.

        Canonicalization must be opt-in: a caller asking for a plain ``git diff`` still sees the user's own settings.
        """
        (repository / "x.py").write_text("value = 1\n")
        git("add", "x.py")
        git("commit", "-qm", "base")
        (repository / "x.py").write_text("value = 2\n")
        git("config", "color.diff", "always")
        expected = git("diff", "--binary", "HEAD", "--")
        assert b"\x1b[" in expected

        output = collector._git_output(repository, ("diff", "--binary", "HEAD", "--"))

        assert output == expected


class TestCommitPatch:
    """Commit-range evidence collected while a presentation setting is active."""

    def test_collection_and_preparation_use_canonical_patch(
        self, collector: ModuleType, prepare: ModuleType, repository: Path, commit_history: CommitHistory
    ) -> None:
        """Text, binary and mode-only commit changes are collected canonically and consumable by preparation.

        Forced color or missing prefixes would corrupt section parsing; the stored patch must stay plain and carry all
        three changes, and the source snapshot must cover every path.
        """
        paths, base, head, output = commit_history

        assert collector.collect_diff("commit", base, output) == 0
        patch = (output / "diff.patch").read_bytes()
        snapshot, changed, comparison_base = prepare._source_snapshot(output, repository, head, paths, base, None)

        assert b"\x1b[" not in patch
        assert set(collector.diff_sections(patch)) == set(paths)
        assert prepare.review_routing.read_review_paths(output, "files.txt") == set(paths)
        assert b"GIT binary patch" in patch
        assert b"old mode 100644\nnew mode 100755" in patch
        assert changed == set(paths)
        assert comparison_base == base
        assert {record["path"] for record in snapshot["files"]} == set(paths)

    def test_snapshot_ignores_later_setting_changes(
        self,
        collector: ModuleType,
        prepare: ModuleType,
        repository: Path,
        git: GitRunner,
        commit_history: CommitHistory,
    ) -> None:
        """Resetting the presentation settings after collection does not invalidate the stored patch.

        The patch is canonical, so preparation must compare it against a canonical regeneration, not the ambient config.
        """
        paths, base, head, output = commit_history
        assert collector.collect_diff("commit", base, output) == 0
        git("config", "diff.noprefix", "false")
        git("config", "color.diff", "never")

        _, changed, _ = prepare._source_snapshot(output, repository, head, paths, base, None)

        assert changed == set(paths)

    def test_snapshot_rejects_tampered_patch(
        self, collector: ModuleType, prepare: ModuleType, repository: Path, commit_history: CommitHistory
    ) -> None:
        """A stored patch edited after collection is reported as stale source evidence."""
        paths, base, head, output = commit_history
        assert collector.collect_diff("commit", base, output) == 0
        patch = (output / "diff.patch").read_bytes()
        (output / "diff.patch").write_bytes(patch.replace(b"+value = 2", b"+value = 9"))

        with pytest.raises(ValueError, match="review-source-diff-stale"):
            prepare._source_snapshot(output, repository, head, paths, base, None)
