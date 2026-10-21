"""Docker argv preserves colon paths and maps users only when the host exposes POSIX IDs."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import docker_sandbox_run as ds
import pytest


@pytest.mark.parametrize(
    "builder",
    [pytest.param(ds.build_explore_command, id="explore"), pytest.param(ds.build_verify_command, id="verify")],
)
def test_colon_path_is_one_bind_source(builder: Any, tmp_path: Path) -> None:
    """Both modes keep the full absolute source in each mount argv element."""
    workdir = str(tmp_path / "project:copy")
    argv = builder("probe.py", "none", workdir)
    mounts = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--mount"]
    expected = [f"type=bind,source={workdir},target=/workspace,readonly"]
    if builder is ds.build_verify_command:
        expected.append(f"type=bind,source={Path(workdir) / '.experiments'},target=/workspace/.experiments")
    assert mounts == expected
    assert "-v" not in argv


def test_comma_path_is_quoted_as_one_csv_field(tmp_path: Path) -> None:
    """A comma in the host path stays inside one quoted ``source`` field.

    Docker reads ``--mount`` as one CSV record; an unquoted comma would split the path into a truncated source plus an
    unknown option, so the container would bind the wrong directory.
    """
    workdir = tmp_path / "project,copy"
    mount = ds.bind_mount(workdir, "/workspace", readonly=True)
    assert next(csv.reader([mount])) == ["type=bind", f"source={workdir.resolve()}", "target=/workspace", "readonly"]


def test_verify_warns_on_unwritable_experiments_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify mode names the chown fix when ``.experiments`` is not writable by the invoking user.

    Linux sandboxes ran as root before ``--user`` was added, so an upgraded project can hold a root-owned
    ``.experiments``; the container would then fail its writes with no hint about the cause.
    """
    monkeypatch.setattr(ds.os, "getuid", lambda: 1234, raising=False)
    monkeypatch.setattr(ds.os, "getgid", lambda: 5678, raising=False)
    monkeypatch.setattr(ds.os, "access", lambda *_args: False)
    monkeypatch.setattr(ds.subprocess, "run", lambda *_a, **_k: ds.subprocess.CompletedProcess([], 0))
    ds.main(["--mode", "verify", "pytest -q"], env={}, cwd=str(tmp_path))
    assert "sudo chown -R" in capsys.readouterr().err


@pytest.mark.parametrize(
    "builder",
    [pytest.param(ds.build_explore_command, id="explore"), pytest.param(ds.build_verify_command, id="verify")],
)
def test_posix_user_mapping(builder: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Invoking uid and gid are passed as one Docker user value."""
    monkeypatch.setattr(ds.os, "getuid", lambda: 1234, raising=False)
    monkeypatch.setattr(ds.os, "getgid", lambda: 5678, raising=False)
    argv = builder("probe.py", "none", str(tmp_path))
    assert argv.count("--user") == 1
    assert argv[argv.index("--user") + 1] == "1234:5678"


@pytest.mark.parametrize(
    "builder",
    [pytest.param(ds.build_explore_command, id="explore"), pytest.param(ds.build_verify_command, id="verify")],
)
def test_missing_getuid_omits_user_mapping(builder: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Windows-like hosts never call uid or gid APIs or emit --user."""
    monkeypatch.delattr(ds.os, "getuid", raising=False)
    monkeypatch.delattr(ds.os, "getgid", raising=False)
    assert "--user" not in builder("probe.py", "none", str(tmp_path))
