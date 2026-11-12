"""Reject revision options and test paths that escape the selected repository."""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from _modules import load_module
from _platform import FILE_SYMLINKS_AVAILABLE

HELPER = load_module("safe_test_targets", Path(__file__).resolve().parents[2] / "shared/test_targets.py")


@pytest.mark.parametrize("entrypoint", ["changed_files", "codemap_targets"])
def test_revision_options_never_reach_subprocesses(tmp_path, monkeypatch, entrypoint):
    """Reject an output option before either Git or the codemap adapter can execute it."""
    execute = Mock(return_value=(0, {}, ""))
    monkeypatch.setattr(HELPER.codemap_adapter, "_resolve_codemap_executable", lambda _: SimpleNamespace(launcher="cm"))
    monkeypatch.setattr(HELPER.codemap_adapter, "_run_json", execute)
    git = Mock(return_value=SimpleNamespace(returncode=0, stdout=""))
    monkeypatch.setattr(HELPER.subprocess, "run", git)
    args = (tmp_path, "--output=sentinel")
    with pytest.raises(HELPER.TargetError, match="invalid-base"):
        getattr(HELPER, entrypoint)(*args, *([None, 1.0] if entrypoint == "codemap_targets" else []))
    execute.assert_not_called()
    git.assert_not_called()


def test_git_verifies_base_and_terminates_options(tmp_path, monkeypatch):
    """Resolve a revision to a commit and delimit revision and path arguments before diffing."""
    oid = "a" * 40
    execute = Mock(
        side_effect=[
            SimpleNamespace(returncode=0, stdout=oid),
            SimpleNamespace(returncode=0, stdout=""),
            SimpleNamespace(returncode=0, stdout=""),
        ]
    )
    monkeypatch.setattr(HELPER.subprocess, "run", execute)
    assert HELPER.changed_files(tmp_path, "HEAD") == []
    assert execute.call_args_list[0].args[0][-3:] == ["--verify", "--end-of-options", "HEAD^{commit}"]
    assert execute.call_args_list[1].args[0][-5:] == ["diff", "--name-only", "--end-of-options", oid, "--"]


@pytest.mark.parametrize("path", ["../test_outside.py", "../outside.py", "C:/outside/test_escape.py"])
def test_changed_paths_must_stay_inside_root(tmp_path, path):
    """Reject escaping changed paths even when they would otherwise be treated as unmapped sources."""
    with pytest.raises(HELPER.TargetError, match="path-outside-root"):
        HELPER.choose(tmp_path, [path], "HEAD", False, None)


@pytest.mark.integration
def test_pytest_option_like_directory_is_a_literal_target(tmp_path):
    """Put options before the separator and dot-prefix a leading dash so a test path cannot become a pytest option.

    Pytest on Python 3.10 and 3.11 still reads an option-shaped word after the separator, so the separator alone does
    not make the directory a literal target there.
    """
    directory = tmp_path / "--confcutdir=elsewhere"
    directory.mkdir()
    (directory / "test_value.py").write_text("def test_value():\n    assert True\n", encoding="utf-8")
    report = HELPER.choose(tmp_path, ["--confcutdir=elsewhere/test_value.py"], "HEAD", False, None)
    assert report["targets"] == ["--confcutdir=elsewhere/test_value.py"]
    assert report["pytest_args"] == ["-p", "no:xdist", "--", "./--confcutdir=elsewhere/test_value.py"]
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-o", "addopts=", *report["pytest_args"]],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "1 passed" in completed.stdout


@pytest.mark.skipif(not FILE_SYMLINKS_AVAILABLE, reason="File symlinks are unavailable on this host")
def test_symlinked_changed_test_cannot_escape_root(tmp_path):
    """Resolve symlinks before selecting a test so an in-root spelling cannot hide external bytes."""
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "test_outside.py"
    outside.write_text("assert False\n", encoding="utf-8")
    (root / "test_link.py").symlink_to(outside)
    with pytest.raises(HELPER.TargetError, match="path-outside-root"):
        HELPER.choose(root, ["test_link.py"], "HEAD", False, None)


@pytest.mark.skipif(not FILE_SYMLINKS_AVAILABLE, reason="File symlinks are unavailable on this host")
def test_contained_changed_test_keeps_its_symlink_name(tmp_path):
    """Keep a test alias selectable even when its contained target does not have a pytest module name."""
    target = tmp_path / "helper.py"
    target.write_text("def test_value():\n    assert True\n", encoding="utf-8")
    (tmp_path / "test_alias.py").symlink_to(target)
    report = HELPER.choose(tmp_path, ["test_alias.py"], "HEAD", False, None)
    assert report["targets"] == ["test_alias.py"]
    assert report["sources"] == {"test_alias.py": "changed-test"}


def test_codemap_cannot_select_an_external_test(tmp_path, monkeypatch):
    """Reject an external test returned by the provider before opening it as a sandbox fixture."""
    monkeypatch.setattr(HELPER, "codemap_targets", lambda *args: (["../test_outside.py"], "available"))
    with pytest.raises(HELPER.TargetError, match="path-outside-root"):
        HELPER.choose(tmp_path, [], "HEAD", True, None)
