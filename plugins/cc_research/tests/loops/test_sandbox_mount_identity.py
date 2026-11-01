"""Docker argv preserves colon paths and maps users only when the host exposes POSIX IDs."""

from __future__ import annotations

import csv
import subprocess
from collections.abc import Callable
from pathlib import Path
from unittest import mock

import docker_sandbox_run as ds
import pytest

_BUILDERS = [pytest.param(ds.build_explore_command, id="explore"), pytest.param(ds.build_verify_command, id="verify")]


@pytest.fixture
def posix_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the host look POSIX with uid 1234 and gid 5678, even where ``os`` has no such functions."""
    monkeypatch.setattr(ds.os, "getuid", lambda: 1234, raising=False)
    monkeypatch.setattr(ds.os, "getgid", lambda: 5678, raising=False)


class TestBindMounts:
    """Host paths reach Docker's ``--mount`` argv without being split or truncated."""

    @pytest.mark.parametrize(
        ("builder", "extra_mounts"),
        [
            pytest.param(ds.build_explore_command, (), id="explore"),
            pytest.param(ds.build_verify_command, (".experiments",), id="verify"),
        ],
    )
    def test_colon_path_is_one_bind_source(
        self, builder: Callable[..., list[str]], extra_mounts: tuple[str, ...], tmp_path: Path
    ) -> None:
        """Both modes keep the full absolute source in each mount argv element.

        A colon in the project path is legal on POSIX but is Docker's ``-v`` field separator, so the mounts must use
        ``--mount`` and never ``-v``. Verify mode adds a writable ``.experiments`` mount beside the read-only workdir.
        """
        workdir = str(tmp_path / "project:copy")

        argv = builder("probe.py", "none", workdir)

        mounts = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--mount"]
        expected = [f"type=bind,source={workdir},target=/workspace,readonly"] + [
            f"type=bind,source={Path(workdir) / name},target=/workspace/{name}" for name in extra_mounts
        ]
        assert mounts == expected
        assert "-v" not in argv

    def test_comma_path_is_quoted_as_one_csv_field(self, tmp_path: Path) -> None:
        """A comma in the host path stays inside one quoted ``source`` field.

        Docker reads ``--mount`` as one CSV record; an unquoted comma would split the path into a truncated source plus
        an unknown option, so the container would bind the wrong directory.
        """
        workdir = tmp_path / "project,copy"

        mount = ds.bind_mount(workdir, "/workspace", readonly=True)

        assert next(csv.reader([mount])) == [
            "type=bind",
            f"source={workdir.resolve()}",
            "target=/workspace",
            "readonly",
        ]


class TestUserMapping:
    """The container user follows the invoking user only where the host has POSIX ids."""

    @pytest.mark.parametrize("builder", _BUILDERS)
    def test_passes_uid_and_gid_as_one_user_value(
        self, builder: Callable[..., list[str]], posix_ids: None, tmp_path: Path
    ) -> None:
        """Invoking uid and gid are passed as one Docker ``--user`` value (``uid:gid``).

        Without the mapping a Linux container runs as root and leaves root-owned files in the user's project.
        """
        argv = builder("probe.py", "none", str(tmp_path))

        assert argv.count("--user") == 1
        assert argv[argv.index("--user") + 1] == "1234:5678"

    @pytest.mark.parametrize("builder", _BUILDERS)
    def test_missing_getuid_omits_user_mapping(
        self, builder: Callable[..., list[str]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Windows-like hosts never call uid or gid APIs or emit ``--user``.

        ``os.getuid`` and ``os.getgid`` are removed to simulate the host; the argv must still build without them.
        """
        monkeypatch.delattr(ds.os, "getuid", raising=False)
        monkeypatch.delattr(ds.os, "getgid", raising=False)

        argv = builder("probe.py", "none", str(tmp_path))

        assert "--user" not in argv

    @mock.patch("docker_sandbox_run.subprocess.run", autospec=True)
    def test_verify_warns_on_unwritable_experiments_dir(
        self,
        mock_run: mock.MagicMock,
        posix_ids: None,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Verify mode names the chown fix when ``.experiments`` is not writable by the invoking user.

        Linux sandboxes ran as root before ``--user`` was added, so an upgraded project can hold a root-owned
        ``.experiments``; the container would then fail its writes with no hint about the cause. The Docker call is
        stubbed because only the warning, not the container run, is under test.
        """
        mock_run.return_value = subprocess.CompletedProcess([], 0)
        monkeypatch.setattr(ds.os, "access", lambda *_args: False)

        ds.main(["--mode", "verify", "pytest -q"], env={}, cwd=str(tmp_path))

        assert "sudo chown -R" in capsys.readouterr().err
