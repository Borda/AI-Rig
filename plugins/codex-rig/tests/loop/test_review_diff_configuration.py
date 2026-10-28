"""Exercise review patch consistency with real local Git configuration."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[2]


def _module(name: str, path: Path) -> ModuleType:
    """Load a shipped helper by its source path."""
    specification = importlib.util.spec_from_file_location(name, path)
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        pytest.param(None, None, id="default"),
        pytest.param("color.diff", "always", id="forced-color"),
        pytest.param("diff.noprefix", "true", id="no-prefix"),
        pytest.param("diff.mnemonicPrefix", "true", id="mnemonic-prefix"),
        pytest.param("core.quotePath", "false", id="unicode-path"),
        pytest.param("diff.context", "0", id="zero-context"),
        pytest.param("env:GIT_DIFF_OPTS", "--unified=0", id="environment-zero-context"),
        pytest.param("diff.relative", "true", id="relative-subdirectory"),
    ],
)
def test_review_patch_is_stable_under_local_git_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, setting: str | None, value: str | None
) -> None:
    """Prevent Git presentation settings from changing or breaking the reviewed patch."""
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    monkeypatch.syspath_prepend(str(PLUGIN_ROOT / "shared"))
    collector = _module("review_config_collector", PLUGIN_ROOT / "shared" / "collect_diff.py")
    validator = _module(
        "review_config_validator", PLUGIN_ROOT / "skills" / "challenge-resolve" / "validate_evidence.py"
    )
    repository = tmp_path / "repository"
    repository.mkdir()

    def git(*arguments: str) -> bytes:
        result = subprocess.run(["git", *arguments], cwd=repository, capture_output=True, check=False)
        assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
        return result.stdout

    git("init", "-q")
    git("config", "user.name", "Diff configuration fixture")
    git("config", "user.email", "diff@example.invalid")
    paths = ["module.py", "calf\u00e9.py"]
    (repository / paths[0]).write_bytes(b"before = 0\nvalue = 1\nafter = 0\n")
    (repository / paths[1]).write_bytes(b"value = 1\n")
    git("add", *paths)
    git("commit", "-qm", "fixture")
    (repository / paths[0]).write_bytes(b"before = 0\nvalue = 2\nafter = 0\n")
    (repository / paths[1]).write_bytes(b"value = 2\n")
    if setting is not None:
        assert value is not None
        if setting.startswith("env:"):
            monkeypatch.setenv(setting[4:], value)
        else:
            git("config", setting, value)

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
    if setting == "env:GIT_DIFF_OPTS":
        monkeypatch.setenv("GIT_DIFF_OPTS", value)
    if setting == "diff.relative":
        (repository / "subdirectory").mkdir()
        monkeypatch.chdir(repository / "subdirectory")
    else:
        monkeypatch.chdir(repository)
    collected = tmp_path / "collected"
    assert collector.collect_diff("working-tree", "", collected) == 0
    snapshot = collector.capture_source_snapshot(repository, paths)
    compared = validator._current_scoped_diff(repository, paths, snapshot, "capture-failed")
    mirror_error = None
    try:
        collector.collect_review_worktree(repository, tmp_path / "mirror")
        collector.verify_review_worktree(tmp_path / "mirror")
    except (RuntimeError, ValueError) as error:
        mirror_error = str(error)

    assert (collected / "diff.patch").read_bytes() == expected
    assert compared == expected
    assert mirror_error is None, mirror_error
    assert (tmp_path / "mirror" / "diff.patch").read_bytes() == expected


def test_historical_default_receipt_verifies_but_noncanonical_requires_recollection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preserve old default receipts without silently blessing transformed patch bytes."""
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.syspath_prepend(str(PLUGIN_ROOT / "shared"))
    collector = _module("review_config_collector", PLUGIN_ROOT / "shared" / "collect_diff.py")
    repository = tmp_path / "repository"
    repository.mkdir()

    def git(*arguments: str) -> bytes:
        result = subprocess.run(["git", *arguments], cwd=repository, capture_output=True, check=False)
        assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
        return result.stdout

    git("init", "-q")
    git("config", "user.name", "Diff configuration fixture")
    git("config", "user.email", "diff@example.invalid")
    source = repository / "calf\u00e9.py"
    source.write_bytes(b"value = 1\n")
    git("add", source.name)
    git("commit", "-qm", "base")
    source.write_bytes(b"value = 2\n")
    report = tmp_path / "old-default"
    collector.collect_review_worktree(repository, report)
    receipt_path = report / "review-worktree.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["schema_version"] = 1
    del receipt["diff_format"]
    receipt_path.write_text(json.dumps(receipt))
    receipt_before = receipt_path.read_bytes()
    collector.verify_review_worktree(report)
    assert receipt_path.read_bytes() == receipt_before
    git("config", "core.quotePath", "false")
    changed = tmp_path / "old-unquoted"
    collector.collect_review_worktree(repository, changed)
    changed_receipt = changed / "review-worktree.json"
    receipt = json.loads(changed_receipt.read_text())
    receipt["schema_version"] = 1
    del receipt["diff_format"]
    native_diff = git("diff", "--binary", "HEAD", "--")
    receipt["diff_sha256"] = hashlib.sha256(native_diff).hexdigest()
    changed_receipt.write_text(json.dumps(receipt))
    preserved = changed_receipt.read_bytes()
    with pytest.raises(RuntimeError, match="patch changed"):
        collector.verify_review_worktree(changed)
    assert changed_receipt.read_bytes() == preserved


def test_staged_rename_is_complete_and_review_matches_challenge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bind a real rename as both deletion and addition in local review and challenge."""
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.syspath_prepend(str(PLUGIN_ROOT / "shared"))
    collector = _module("review_config_collector", PLUGIN_ROOT / "shared" / "collect_diff.py")
    validator = _module("review_config_validator", PLUGIN_ROOT / "skills/challenge-resolve/validate_evidence.py")
    repository = tmp_path / "repository"
    repository.mkdir()

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repository, capture_output=True, check=True)

    git("init", "-q")
    git("config", "user.name", "fixture")
    git("config", "user.email", "fixture@example.invalid")
    (repository / "old.py").write_text("value = 1\n")
    git("add", "old.py")
    git("commit", "-qm", "base")
    git("mv", "old.py", "new.py")
    paths = ["new.py", "old.py"]
    run = tmp_path / "run"
    report = run / "local-source"
    collector.collect_review_worktree(repository, report)
    collector.verify_review_worktree(report)
    snapshot = collector.capture_source_snapshot(repository, paths)
    diff = validator._current_scoped_diff(repository, paths, snapshot, "failed")
    assert diff == (report / "diff.patch").read_bytes()
    assert b"deleted file mode" in diff
    assert b"new file mode" in diff
    assert set(json.loads((report / "review-worktree.json").read_text())["scope_paths"]) == set(paths)
    (run / "diff.patch").write_bytes(diff)
    prepare = _module("review_config_prepare", PLUGIN_ROOT / "skills/code-review/review_prepare.py")
    mirror = Path(json.loads((report / "review-worktree.json").read_text())["review_worktree"])
    names = subprocess.run(
        ["git", "-C", str(mirror), "diff", "--no-renames", "--name-only", "HEAD", "--"],
        capture_output=True,
        check=True,
    ).stdout
    (run / "files.txt").write_bytes(names)
    _, changed, comparison_base = prepare._source_snapshot(run, mirror, None, paths, None, None)
    assert changed == set(paths)
    assert comparison_base == snapshot["revision"]
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


def test_uncanonicalized_git_output_preserves_native_presentation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preserve native output for callers that do not request canonical diff arguments."""
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.syspath_prepend(str(PLUGIN_ROOT / "shared"))
    collector = _module("review_config_collector", PLUGIN_ROOT / "shared" / "collect_diff.py")
    repository = tmp_path / "repository"
    repository.mkdir()

    def git(*args: str) -> bytes:
        return subprocess.run(["git", *args], cwd=repository, capture_output=True, check=True).stdout

    git("init", "-q")
    git("config", "user.name", "fixture")
    git("config", "user.email", "fixture@example.invalid")
    (repository / "x.py").write_text("value = 1\n")
    git("add", "x.py")
    git("commit", "-qm", "base")
    (repository / "x.py").write_text("value = 2\n")
    git("config", "color.diff", "always")
    expected = git("diff", "--binary", "HEAD", "--")
    assert b"\x1b[" in expected
    assert collector._git_output(repository, ("diff", "--binary", "HEAD", "--")) == expected


@pytest.mark.parametrize(
    ("setting", "value"),
    [pytest.param("diff.noprefix", "true", id="no-prefix"), pytest.param("color.diff", "always", id="forced-color")],
)
def test_commit_collection_and_preparation_use_canonical_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, setting: str, value: str
) -> None:
    """Keep text, binary and mode-only commit evidence consumable under local diff settings."""
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.syspath_prepend(str(PLUGIN_ROOT / "shared"))
    collector = _module("commit_config_collector", PLUGIN_ROOT / "shared/collect_diff.py")
    prepare = _module("commit_config_prepare", PLUGIN_ROOT / "skills/code-review/review_prepare.py")
    repository = tmp_path / "repository"
    repository.mkdir()

    def git(*arguments: str) -> bytes:
        return subprocess.run(["git", *arguments], cwd=repository, capture_output=True, check=True).stdout

    git("init", "-q")
    git("config", "core.filemode", "false")
    git("config", "user.name", "fixture")
    git("config", "user.email", "fixture@example.invalid")
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
    git("config", setting, value)
    monkeypatch.chdir(repository)
    output = tmp_path / "collected"

    assert collector.collect_diff("commit", base, output) == 0
    patch = (output / "diff.patch").read_bytes()
    assert b"\x1b[" not in patch
    assert set(collector.diff_sections(patch)) == set(paths)
    assert prepare.review_routing.read_review_paths(output, "files.txt") == set(paths)
    assert b"GIT binary patch" in patch
    assert b"old mode 100644\nnew mode 100755" in patch
    snapshot, changed, comparison_base = prepare._source_snapshot(output, repository, head, paths, base, None)
    assert changed == set(paths)
    assert comparison_base == base
    assert {record["path"] for record in snapshot["files"]} == set(paths)
    git("config", "diff.noprefix", "false")
    git("config", "color.diff", "never")
    assert prepare._source_snapshot(output, repository, head, paths, base, None)[1] == set(paths)
    (output / "diff.patch").write_bytes(patch.replace(b"+value = 2", b"+value = 9"))
    with pytest.raises(ValueError, match="review-source-diff-stale"):
        prepare._source_snapshot(output, repository, head, paths, base, None)
