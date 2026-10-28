"""Exercise review inputs against real Git paths, binary changes, and gitlinks."""

from __future__ import annotations

import importlib.util
import json
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[2]


def _supports_control_names() -> bool:
    with tempfile.TemporaryDirectory() as directory:
        try:
            (Path(directory) / "a\nb\tc").write_bytes(b"x")
        except OSError:
            return False
    return True


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Fixture")
    _git(root, "config", "user.email", "test@example.invalid")
    (root / "data.bin").write_bytes(b"\x00old")
    _git(root, "add", "data.bin")
    _git(root, "commit", "-qm", "base")
    return root


def _git(root: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True).stdout


@pytest.mark.parametrize("driver", ["external", "textconv"])
def test_commit_diff_does_not_execute_git_diff_drivers(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, driver: str
) -> None:
    """A commit review must use the actual source patch without executing configured diff drivers."""
    monkeypatch.syspath_prepend(str(PLUGIN / "shared"))
    monkeypatch.syspath_prepend(str(PLUGIN / "skills/code-review"))
    collector = _load("review_safe_driver_collector", PLUGIN / "shared/collect_diff.py")
    producer = _load("review_safe_driver_producer", PLUGIN / "skills/code-review/review_prepare.py")
    (repository / ".gitattributes").write_text("*.txt diff=fixture\n", encoding="utf-8", newline="\n")
    (repository / "module.txt").write_text("value = 1\n", encoding="utf-8", newline="\n")
    _git(repository, "add", ".gitattributes", "module.txt")
    _git(repository, "commit", "-qm", "fixture base")
    base = _git(repository, "rev-parse", "HEAD").decode().strip()
    (repository / "module.txt").write_text("value = 2\n", encoding="utf-8", newline="\n")
    _git(repository, "commit", "-qam", "fixture change")
    head = _git(repository, "rev-parse", "HEAD").decode().strip()
    _git(repository, "checkout", "--detach", "-q")
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
    _git(repository, "config", "diff.external" if driver == "external" else "diff.fixture.textconv", command)
    expected = _git(repository, "diff", "--binary", "--no-ext-diff", "--no-textconv", base, head, "--")
    assert b"+value = 2" in expected
    monkeypatch.chdir(repository)
    out = tmp_path / "review"
    assert collector.collect_diff("commit", f"{base}..{head}", out) == 0
    assert (out / "diff.patch").read_bytes() == expected
    snapshot, changed, comparison_base = producer._source_snapshot(out, repository, head, ["module.txt"], base, None)
    assert changed == {"module.txt"}
    assert comparison_base == base
    assert snapshot["files"][0]["content"] == "value = 2\n"
    assert not marker.exists()


def test_commit_binary_collection_matches_review_comparison(repository: Path, tmp_path: Path, monkeypatch) -> None:
    collector = _load("git_inventory_collector_binary", PLUGIN / "shared/collect_diff.py")
    monkeypatch.syspath_prepend(str(PLUGIN / "skills/code-review"))
    monkeypatch.syspath_prepend(str(PLUGIN / "shared"))
    producer = _load("git_inventory_review_producer", PLUGIN / "skills/code-review/review_prepare.py")
    (repository / "data.bin").write_bytes(b"\x00new")
    _git(repository, "commit", "-qam", "change")
    monkeypatch.chdir(repository)
    out = tmp_path / "out"
    assert collector.collect_diff("commit", "HEAD^..HEAD", out) == 0
    assert (out / "diff.patch").read_bytes() == _git(repository, "diff", "--binary", "HEAD^", "HEAD", "--")
    base = _git(repository, "rev-parse", "HEAD^").decode().strip()
    head = _git(repository, "rev-parse", "HEAD").decode().strip()
    _git(repository, "checkout", "--detach", "-q")
    _, changed, comparison_base = producer._source_snapshot(out, repository, head, ["data.bin"], base, None)
    assert changed == {"data.bin"}
    assert comparison_base == base


@pytest.mark.parametrize(
    "name",
    [
        "café.py",
        pytest.param(
            "a\nb\tc.py",
            marks=pytest.mark.skipif(
                not _supports_control_names(), reason="filesystem rejects control-character names"
            ),
        ),
    ],
)
def test_collected_untracked_names_decode_losslessly(repository: Path, tmp_path: Path, monkeypatch, name: str) -> None:
    collector = _load("git_inventory_collector_names", PLUGIN / "shared/collect_diff.py")
    (repository / name).write_bytes(b"value = 1\n")
    monkeypatch.chdir(repository)
    out = tmp_path / "out"
    assert collector.collect_diff("working-tree", "", out) == 0
    assert collector.read_git_path_lines(out / "untracked.txt") == {name}


def test_ignored_submodule_change_is_visible_and_rejected(repository: Path, tmp_path: Path, monkeypatch) -> None:
    collector = _load("git_inventory_collector_submodule", PLUGIN / "shared/collect_diff.py")
    old = _git(repository, "rev-parse", "HEAD").decode().strip()
    _git(repository, "update-index", "--add", "--cacheinfo", f"160000,{old},dependency")
    _git(repository, "commit", "-qm", "add gitlink")
    (repository / "data.bin").write_bytes(b"\x00new")
    _git(repository, "add", "data.bin")
    _git(repository, "commit", "-qm", "next")
    new = _git(repository, "rev-parse", "HEAD").decode().strip()
    _git(repository, "update-index", "--cacheinfo", f"160000,{new},dependency")
    _git(repository, "config", "diff.ignoreSubmodules", "all")
    assert not _git(repository, "diff", "--cached", "--name-only", "HEAD", "--", "dependency")
    visible = collector._git_output(
        repository, collector.canonical_diff_arguments(("diff", "--cached", "--name-only", "-z", "HEAD", "--"))
    )
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


@pytest.mark.skipif(not _supports_control_names(), reason="filesystem rejects control-character names")
def test_real_pr_patch_preserves_newline_source_name(repository: Path, tmp_path: Path, monkeypatch) -> None:
    """Use Git's patch path rather than the PR collector's ambiguous literal lines."""
    monkeypatch.syspath_prepend(str(PLUGIN / "shared"))
    routing = _load("git_inventory_routing", PLUGIN / "skills/code-review/review_routing.py")
    name = "app\ntest_unit.py"
    (repository / name).write_bytes(b"value = 1\n")
    _git(repository, "add", name)
    out = tmp_path / "pr"
    out.mkdir()
    (out / "pr.json").write_text("{}", encoding="utf-8")
    (out / "files.txt").write_text(name + "\n", encoding="utf-8", newline="\n")
    (out / "diff.patch").write_bytes(_git(repository, "diff", "--binary", "HEAD", "--"))
    assert routing.read_review_paths(out, "files.txt") == {name}
    base = _git(repository, "rev-parse", "HEAD").decode().strip()
    _git(repository, "commit", "-qm", "change")
    head = _git(repository, "rev-parse", "HEAD").decode().strip()
    _git(repository, "checkout", "--detach", "-q")
    (out / "diff.patch").write_bytes(_git(repository, "diff", "--binary", f"{base}...{head}", "--"))
    (out / "local-checkout.json").write_text(
        json.dumps(
            {
                "worktree": repository.resolve().as_posix(),
                "expected_head": head,
                "diff_base_oid": base,
                "diff_head_oid": head,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(PLUGIN / "skills/code-review"))
    producer = _load("git_newline_pr_producer", PLUGIN / "skills/code-review/review_prepare.py")
    _, changed, comparison_base = producer._source_snapshot(out, repository, head, [name], None, None)
    assert changed == {name}
    assert comparison_base == base


def _supports_symlinks() -> bool:
    """Probe whether this filesystem allows creating symbolic links."""
    with tempfile.TemporaryDirectory() as directory:
        try:
            (Path(directory) / "link").symlink_to("target")
        except OSError:
            return False
    return True


@pytest.mark.skipif(not _supports_symlinks(), reason="filesystem rejects symlinks")
def test_git_type_change_retains_both_patch_sections(repository: Path, tmp_path: Path, monkeypatch) -> None:
    """A valid type change may have two Git sections with the same source path."""
    monkeypatch.syspath_prepend(str(PLUGIN / "shared"))
    routing = _load("git_type_change_routing", PLUGIN / "skills/code-review/review_routing.py")
    collector = _load("git_type_change_collector", PLUGIN / "shared/collect_diff.py")
    path = repository / "module.py"
    path.symlink_to("target")
    _git(repository, "add", "module.py")
    _git(repository, "commit", "-qm", "symlink")
    path.unlink()
    path.write_bytes(b"value = 1\n")
    diff = _git(repository, "diff", "--binary", "HEAD", "--")
    assert diff.count(b"diff --git ") == 2
    sections = collector.diff_sections(diff)
    assert sections["module.py"] == diff
    out = tmp_path / "review-type"
    out.mkdir()
    (out / "diff.patch").write_bytes(diff)
    (out / "files.txt").write_text("module.py\n", encoding="utf-8", newline="\n")
    assert routing.read_review_paths(out, "files.txt") == {"module.py"}
