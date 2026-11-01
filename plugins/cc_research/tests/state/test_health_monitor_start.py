"""Tests for ``bin/health_monitor_start.py`` — research health-monitoring sentinel.

Covers:
* Happy-path: ``LAUNCH_AT`` + ``SENTINEL`` printed; sentinel file created
* Sentinel path format: ``research-<skill-id>-check-<ts>``
* ``LAUNCH_AT`` value embedded in sentinel path
* Missing skill-id → exit 1
* Invalid skill-id (unsafe chars) → exit 2
* Windows-portability invariants: no CRLF in stdout, forward-slash sentinel path, platform-conditional sentinel dir
"""

from __future__ import annotations

import re
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

import health_monitor_start
import pytest


def _parse_kv(output: str) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines from stdout into a dict.

    Examples:
        >>> _parse_kv("LAUNCH_AT=123\\nSENTINEL=/tmp/check\\nnoise")
        {'LAUNCH_AT': '123', 'SENTINEL': '/tmp/check'}
    """
    pairs: dict[str, str] = {}
    for line in output.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            pairs[key] = value
    return pairs


@pytest.fixture
def sentinel_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the script's platform sentinel dir (``/tmp`` on POSIX) into ``tmp_path`` so no test writes globally."""
    target = tmp_path / "sentinels"
    target.mkdir()
    monkeypatch.setattr(health_monitor_start, "_sentinel_dir", lambda: target)
    return target


@pytest.fixture
def launch(sentinel_dir: Path, capsys: pytest.CaptureFixture[str]) -> Callable[[str], tuple[int, dict[str, str], str]]:
    """Return a callable that runs ``main([skill_id])`` and yields ``(exit_code, parsed_stdout, raw_stdout)``."""

    def _launch(skill_id: str) -> tuple[int, dict[str, str], str]:
        rc = health_monitor_start.main([skill_id])
        out = capsys.readouterr().out
        return rc, _parse_kv(out), out

    return _launch


class TestSentinelDir:
    """Platform-conditional location of the sentinel directory."""

    def test_matches_hook_sentinel_dir(self) -> None:
        """The sentinel dir is ``/tmp`` on POSIX and the platform temp dir on Windows, as the JS hooks expect.

        Hooks look sentinels up in the same place, so a drift here silently hides every health check from them.
        """
        expected = Path(tempfile.gettempdir()) if sys.platform == "win32" else Path("/tmp")

        assert health_monitor_start._sentinel_dir() == expected


class TestArgparse:
    """Argparse-layer behaviour: ``--help`` and golden README invocation."""

    def test_help_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        """``--help`` prints usage naming the script and exits 0 (argparse ``SystemExit``)."""
        with pytest.raises(SystemExit) as exc:
            health_monitor_start.main(["--help"])

        assert exc.value.code == 0
        assert "health_monitor_start.py" in capsys.readouterr().out

    def test_golden_readme_invocation(self, launch: Callable[[str], tuple[int, dict[str, str], str]]) -> None:
        """README shape ``health_monitor_start.py <skill-id>`` → exit 0 with numeric ``LAUNCH_AT`` and a sentinel."""
        rc, kv, _out = launch("research-run")

        assert rc == 0
        assert kv["LAUNCH_AT"].isdigit()
        assert Path(kv["SENTINEL"]).name.startswith("research-research-run-check-")


class TestValidation:
    """Argument validation."""

    def test_missing_skill_id_exits_one(self, capsys: pytest.CaptureFixture[str]) -> None:
        """No arguments → exit 1 with a ``skill-id`` complaint on stderr, before any sentinel is created."""
        rc = health_monitor_start.main([])

        assert rc == 1
        assert "skill-id" in capsys.readouterr().err

    @pytest.mark.parametrize("skill_id", ["bad/id", "bad id", "bad!id", "../evil"])
    def test_unsafe_skill_id_exits_two(self, skill_id: str, capsys: pytest.CaptureFixture[str]) -> None:
        """A skill-id with characters outside ``[A-Za-z0-9_-]`` → exit 2 with a ``SKILL_ID`` error on stderr.

        The id is embedded in a sentinel path, so a separator or traversal segment must never reach the filesystem.
        """
        rc = health_monitor_start.main([skill_id])

        assert rc == 2
        assert "SKILL_ID" in capsys.readouterr().err

    def test_trailing_newline_skill_id_is_rejected(self) -> None:
        """A newline-terminated skill-id exits 2 instead of embedding the newline in the sentinel path.

        ``_SKILL_RE`` ends in ``$``, which ``re.match`` lets match before a final newline; ``fullmatch`` does not.
        """
        assert health_monitor_start.main(["skill\n"]) == 2


class TestLaunch:
    """Output and side effects of a valid invocation."""

    def test_emits_launch_at_and_sentinel(self, launch: Callable[[str], tuple[int, dict[str, str], str]]) -> None:
        """Stdout carries both ``LAUNCH_AT`` (epoch digits) and ``SENTINEL`` keys."""
        _rc, kv, _out = launch("test-skill-kv")

        assert kv["LAUNCH_AT"].isdigit()
        assert "SENTINEL" in kv

    def test_creates_sentinel_file(
        self, launch: Callable[[str], tuple[int, dict[str, str], str]], sentinel_dir: Path
    ) -> None:
        """The sentinel path printed on stdout exists on disk, inside the sentinel dir, named for the skill."""
        _rc, kv, _out = launch("test-skill-file")

        sentinel = Path(kv["SENTINEL"])
        assert sentinel.exists()
        assert sentinel.parent == sentinel_dir
        assert sentinel.name.startswith("research-test-skill-file-check-")

    def test_launch_at_is_sentinel_suffix(self, launch: Callable[[str], tuple[int, dict[str, str], str]]) -> None:
        """The sentinel path ends in ``-check-<LAUNCH_AT>`` so the monitor can derive the launch time from it."""
        _rc, kv, _out = launch("test-skill-tsmatch")

        match = re.search(r"-check-(\d+)$", kv["SENTINEL"])
        assert match is not None
        assert match.group(1) == kv["LAUNCH_AT"]

    def test_output_has_no_crlf(self, launch: Callable[[str], tuple[int, dict[str, str], str]]) -> None:
        """Stdout must not contain CR (Windows text-mode regression guard)."""
        _rc, _kv, out = launch("test-skill-crlf")

        assert "\r" not in out

    def test_sentinel_path_uses_forward_slashes(self, launch: Callable[[str], tuple[int, dict[str, str], str]]) -> None:
        """The ``SENTINEL`` value uses forward slashes so bash can consume it even on Windows."""
        _rc, kv, _out = launch("test-skill-posix")

        assert "\\" not in kv["SENTINEL"]
