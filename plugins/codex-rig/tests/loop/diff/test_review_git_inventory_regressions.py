"""Exercise review inputs against real Git paths, binary changes, and gitlinks.

The tests build throwaway Git repositories and drive the shipped diff collector, review routing, review preparation and
chunk planner so that unusual paths, diff drivers, binary changes, type changes and gitlinks cannot slip past review.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType

import pytest
from _git_fixtures import git_bytes
from _modules import load_module_registered

PLUGIN = Path(__file__).resolve().parents[3]
NEWLINE_NAME = "app\ntest_unit.py"


def _supports_control_names() -> bool:
    """Probe whether this filesystem accepts control characters in file names."""
    with tempfile.TemporaryDirectory() as directory:
        try:
            (Path(directory) / "a\nb\tc").write_bytes(b"x")
        except OSError:
            return False
    return True


def _supports_symlinks() -> bool:
    """Probe whether this filesystem allows creating symbolic links."""
    with tempfile.TemporaryDirectory() as directory:
        try:
            (Path(directory) / "link").symlink_to("target")
        except OSError:
            return False
    return True


_requires_control_names = pytest.mark.skipif(
    not _supports_control_names(), reason="filesystem rejects control-character names"
)


def _rev(root: Path, revision: str) -> str:
    """Resolve ``revision`` to a full object id."""
    return git_bytes(root, "rev-parse", revision).decode().strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """Create a repository whose only commit holds a binary ``data.bin``."""
    root = tmp_path / "repo"
    root.mkdir()
    git_bytes(root, "init", "-q")
    git_bytes(root, "config", "user.name", "Fixture")
    git_bytes(root, "config", "user.email", "test@example.invalid")
    (root / "data.bin").write_bytes(b"\x00old")
    git_bytes(root, "add", "data.bin")
    git_bytes(root, "commit", "-qm", "base")
    return root


@pytest.fixture
def collector(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Load the shipped diff collector."""
    monkeypatch.syspath_prepend(str(PLUGIN / "shared"))
    return load_module_registered("git_inventory_collector", PLUGIN / "shared/collect_diff.py")


@pytest.fixture
def producer(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Load the shipped review preparation helper."""
    monkeypatch.syspath_prepend(str(PLUGIN / "shared"))
    monkeypatch.syspath_prepend(str(PLUGIN / "skills/code-review"))
    return load_module_registered("git_inventory_review_producer", PLUGIN / "skills/code-review/review_prepare.py")


@pytest.fixture
def routing(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Load the shipped review routing helper."""
    monkeypatch.syspath_prepend(str(PLUGIN / "shared"))
    return load_module_registered("git_inventory_routing", PLUGIN / "skills/code-review/review_routing.py")


class TestCommitCollection:
    """Commit-range patches collected for review."""

    @pytest.mark.parametrize("driver", ["external", "textconv"])
    def test_diff_drivers_are_not_executed(
        self,
        repository: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        collector: ModuleType,
        producer: ModuleType,
        driver: str,
    ) -> None:
        """A commit review uses the actual source patch and never executes configured diff drivers.

        A repository can configure an external diff or textconv program; running it during review would execute
        repository-controlled code and could replace the reviewed patch with a forged one.
        """
        (repository / ".gitattributes").write_text("*.txt diff=fixture\n", encoding="utf-8", newline="\n")
        (repository / "module.txt").write_text("value = 1\n", encoding="utf-8", newline="\n")
        git_bytes(repository, "add", ".gitattributes", "module.txt")
        git_bytes(repository, "commit", "-qm", "fixture base")
        base = _rev(repository, "HEAD")
        (repository / "module.txt").write_text("value = 2\n", encoding="utf-8", newline="\n")
        git_bytes(repository, "commit", "-qam", "fixture change")
        head = _rev(repository, "HEAD")
        git_bytes(repository, "checkout", "--detach", "-q")
        marker = tmp_path / "driver-ran"
        script = tmp_path / "driver.py"
        script.write_text(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('executed')\n"
            "print('diff --git a/module.txt b/module.txt\\n--- a/module.txt\\n+++ b/module.txt\\n"
            "@@ -1 +1 @@\\n-value = 1\\n+value = 99')\n",
            encoding="utf-8",
            newline="\n",
        )
        command = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"
        git_bytes(repository, "config", "diff.external" if driver == "external" else "diff.fixture.textconv", command)
        expected = git_bytes(repository, "diff", "--binary", "--no-ext-diff", "--no-textconv", base, head, "--")
        assert b"+value = 2" in expected
        monkeypatch.chdir(repository)
        out = tmp_path / "review"

        assert collector.collect_diff("commit", f"{base}..{head}", out) == 0
        snapshot, changed, comparison_base = producer._source_snapshot(
            out, repository, head, ["module.txt"], base, None
        )

        assert (out / "diff.patch").read_bytes() == expected
        assert changed == {"module.txt"}
        assert comparison_base == base
        assert snapshot["files"][0]["content"] == "value = 2\n"
        assert not marker.exists()

    def test_binary_change_matches_review_comparison(
        self,
        repository: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        collector: ModuleType,
        producer: ModuleType,
    ) -> None:
        """A committed binary change collects as Git's binary patch and is accepted by review preparation.

        Binary files carry no textual hunks, so the collected patch and the later comparison must agree on the ``GIT
        binary patch`` form for the changed path.
        """
        (repository / "data.bin").write_bytes(b"\x00new")
        git_bytes(repository, "commit", "-qam", "change")
        monkeypatch.chdir(repository)
        out = tmp_path / "out"
        base = _rev(repository, "HEAD^")
        head = _rev(repository, "HEAD")

        assert collector.collect_diff("commit", "HEAD^..HEAD", out) == 0
        git_bytes(repository, "checkout", "--detach", "-q")
        _, changed, comparison_base = producer._source_snapshot(out, repository, head, ["data.bin"], base, None)

        assert (out / "diff.patch").read_bytes() == git_bytes(repository, "diff", "--binary", "HEAD^", "HEAD", "--")
        assert changed == {"data.bin"}
        assert comparison_base == base


class TestWorkingTreeCollection:
    """Working-tree inventories collected for review."""

    @pytest.mark.parametrize(
        "name",
        [
            pytest.param("café.py", id="non-ascii"),
            pytest.param("a\nb\tc.py", marks=_requires_control_names, id="control-characters"),
        ],
    )
    def test_untracked_names_round_trip_losslessly(
        self, repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, collector: ModuleType, name: str
    ) -> None:
        """Untracked file names, including non-ASCII and control characters, decode back to the exact name.

        The untracked inventory is written as NUL-separated Git paths; a lossy decode would let a crafted name hide from
        or masquerade in review.
        """
        (repository / name).write_bytes(b"value = 1\n")
        monkeypatch.chdir(repository)
        out = tmp_path / "out"

        assert collector.collect_diff("working-tree", "", out) == 0

        assert collector.read_git_path_lines(out / "untracked.txt") == {name}

    def test_ignored_submodule_change_is_visible_and_rejected(
        self, repository: Path, tmp_path: Path, collector: ModuleType
    ) -> None:
        """A gitlink change hidden by ``diff.ignoreSubmodules`` is still seen and refused by the review tools.

        With the setting ``all`` plain ``git diff`` reports nothing, but the canonical arguments must reveal the change
        so that both the worktree collector and the chunk planner reject the unsupported gitlink.
        """
        old = _rev(repository, "HEAD")
        git_bytes(repository, "update-index", "--add", "--cacheinfo", f"160000,{old},dependency")
        git_bytes(repository, "commit", "-qm", "add gitlink")
        (repository / "data.bin").write_bytes(b"\x00new")
        git_bytes(repository, "add", "data.bin")
        git_bytes(repository, "commit", "-qm", "next")
        git_bytes(repository, "update-index", "--cacheinfo", f"160000,{_rev(repository, 'HEAD')},dependency")
        git_bytes(repository, "config", "diff.ignoreSubmodules", "all")
        assert not git_bytes(repository, "diff", "--cached", "--name-only", "HEAD", "--", "dependency")
        arguments = collector.canonical_diff_arguments(("diff", "--cached", "--name-only", "-z", "HEAD", "--"))

        visible = collector._git_output(repository, arguments)

        assert b"dependency\0" in visible
        with pytest.raises(RuntimeError, match="gitlink"):
            collector.collect_review_worktree(repository, tmp_path / "review")
        planner = subprocess.run(
            [
                sys.executable,
                str(PLUGIN / "skills/challenge-resolve/chunk_diff.py"),
                "plan",
                "--repository",
                str(repository),
                "--out",
                str(tmp_path / "plan"),
                "--goal",
                "review",
                "--specification",
                "complete",
                "--done-when",
                "reviewed",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert planner.returncode != 0
        assert "gitlink" in planner.stderr


@_requires_control_names
class TestPullRequestNewlineName:
    """A pull-request source name containing a newline."""

    @pytest.fixture
    def pr_directory(self, repository: Path, tmp_path: Path) -> Path:
        """Stage the newline-named file and write a PR directory listing it."""
        (repository / NEWLINE_NAME).write_bytes(b"value = 1\n")
        git_bytes(repository, "add", NEWLINE_NAME)
        out = tmp_path / "pr"
        out.mkdir()
        (out / "pr.json").write_text("{}", encoding="utf-8")
        (out / "files.txt").write_text(NEWLINE_NAME + "\n", encoding="utf-8", newline="\n")
        return out

    def test_routing_reads_name_from_real_patch(
        self, repository: Path, routing: ModuleType, pr_directory: Path
    ) -> None:
        """Routing recovers the newline-bearing name from Git's patch, not from the ambiguous literal list.

        ``files.txt`` is line oriented and cannot represent the name unambiguously; Git's own patch path must win.
        """
        (pr_directory / "diff.patch").write_bytes(git_bytes(repository, "diff", "--binary", "HEAD", "--"))

        assert routing.read_review_paths(pr_directory, "files.txt") == {NEWLINE_NAME}

    def test_preparation_accepts_committed_pr_patch(
        self, repository: Path, producer: ModuleType, pr_directory: Path
    ) -> None:
        """Review preparation binds a committed PR patch with a newline-bearing name to its base."""
        base = _rev(repository, "HEAD")
        git_bytes(repository, "commit", "-qm", "change")
        head = _rev(repository, "HEAD")
        git_bytes(repository, "checkout", "--detach", "-q")
        (pr_directory / "diff.patch").write_bytes(git_bytes(repository, "diff", "--binary", f"{base}...{head}", "--"))
        checkout = {
            "worktree": repository.resolve().as_posix(),
            "expected_head": head,
            "diff_base_oid": base,
            "diff_head_oid": head,
        }
        (pr_directory / "local-checkout.json").write_text(json.dumps(checkout), encoding="utf-8")

        _, changed, comparison_base = producer._source_snapshot(
            pr_directory, repository, head, [NEWLINE_NAME], None, None
        )

        assert changed == {NEWLINE_NAME}
        assert comparison_base == base


@pytest.mark.skipif(not _supports_symlinks(), reason="filesystem rejects symlinks")
def test_type_change_keeps_both_patch_sections(
    repository: Path, tmp_path: Path, collector: ModuleType, routing: ModuleType
) -> None:
    """A file replaced by a symlink (or vice versa) keeps both Git sections under one source path.

    Git emits two ``diff --git`` sections for a type change; collapsing them would drop the deletion half from review.
    """
    path = repository / "module.py"
    path.symlink_to("target")
    git_bytes(repository, "add", "module.py")
    git_bytes(repository, "commit", "-qm", "symlink")
    path.unlink()
    path.write_bytes(b"value = 1\n")
    diff = git_bytes(repository, "diff", "--binary", "HEAD", "--")
    assert diff.count(b"diff --git ") == 2
    out = tmp_path / "review-type"
    out.mkdir()
    (out / "diff.patch").write_bytes(diff)
    (out / "files.txt").write_text("module.py\n", encoding="utf-8", newline="\n")

    sections = collector.diff_sections(diff)

    assert sections["module.py"] == diff
    assert routing.read_review_paths(out, "files.txt") == {"module.py"}
