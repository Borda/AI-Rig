"""Reject unreadable explicit reference files without publishing partial hit lists."""

import json
from pathlib import Path
from unittest import mock

import find_module_refs
import pytest


@pytest.mark.parametrize("path_kind", ["missing", "directory"])
def test_explicit_unreadable_path_fails_closed(tmp_path: Path, capsys, path_kind: str, monkeypatch) -> None:
    """Missing files and directory paths cannot silently shrink an indexed reference set."""
    monkeypatch.chdir(tmp_path)
    target = tmp_path if path_kind == "directory" else tmp_path / "gone.py"
    answer = tmp_path / "answer.json"
    answer.write_text(json.dumps({"called_by": [{"path": str(target)}]}), encoding="utf-8")

    code = find_module_refs.main(["--kind", "name", "--name", "old", "--files-from-json", str(answer)])

    captured = capsys.readouterr()
    assert code == 1
    assert f"! cannot read {target}" in captured.err
    assert captured.out == ""


def test_explicit_permission_failure_discards_partial_hits(tmp_path: Path, capsys, monkeypatch) -> None:
    """An open failure after an earlier hit still fails without emitting a partial reference list."""
    monkeypatch.chdir(tmp_path)
    good = tmp_path / "a.py"
    denied = tmp_path / "b.py"
    good.write_text("old()\n", encoding="utf-8")
    denied.write_text("old()\n", encoding="utf-8")
    answer = tmp_path / "answer.json"
    answer.write_text(json.dumps({"matches": [{"path": str(good)}, {"path": str(denied)}]}), encoding="utf-8")
    real_open = open

    def open_file(path, *args, **kwargs):
        """Deny one source open on every host, including privileged test runners."""
        if str(path) == str(denied):
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, *args, **kwargs)

    with mock.patch("builtins.open", side_effect=open_file, autospec=True):
        code = find_module_refs.main(["--kind", "name", "--name", "old", "--files-from-json", str(answer)])

    captured = capsys.readouterr()
    assert code == 1
    assert f"! cannot read {denied}" in captured.err
    assert captured.out == ""
