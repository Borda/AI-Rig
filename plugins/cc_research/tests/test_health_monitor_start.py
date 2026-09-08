"""Tests for ``bin/health_monitor_start.py`` — research health-monitoring sentinel.

Covers:
* Happy-path: ``LAUNCH_AT`` + ``SENTINEL`` printed; sentinel file created
* Sentinel path format: ``research-<skill-id>-check-<ts>``
* ``LAUNCH_AT`` value embedded in sentinel path
* Missing skill-id → exit 1
* Invalid skill-id (unsafe chars) → exit 2
* Windows-portability invariants: no CRLF in stdout (behavioural),
  ``_sentinel_dir()`` defined for platform-conditional sentinel path
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

import pytest

import health_monitor_start

SCRIPT = Path(health_monitor_start.__file__)


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


@pytest.fixture(name="sentinel_cleanup")
def _sentinel_cleanup() -> list[str]:
    """Collect skill-ids used by a test; remove their sentinel files on teardown.

    ``health_monitor_start.py`` writes to the fixed platform temp dir (not ``tmp_path`` — the script owns that path, not
    the test), so cleanup must happen out-of-band. Tests append the skill-id(s) they used.
    """
    skill_ids: list[str] = []
    yield skill_ids
    sentinel_base = Path(tempfile.gettempdir()) if sys.platform == "win32" else Path("/tmp")
    for skill_id in skill_ids:
        for stale in sentinel_base.glob(f"research-{skill_id}-check-*"):
            stale.unlink(missing_ok=True)


class TestArgparse:
    """Argparse-layer behaviour: ``--help`` and golden README invocation."""

    def test_help_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Print usage and exit 0 (argparse SystemExit)."""
        with pytest.raises(SystemExit) as exc:
            health_monitor_start.main(["--help"])
        assert exc.value.code == 0
        assert "health_monitor_start.py" in capsys.readouterr().out

    def test_golden_readme_invocation(self, capsys: pytest.CaptureFixture[str], sentinel_cleanup: list[str]) -> None:
        """README shape ``health_monitor_start.py <skill-id>`` → exit 0 with sentinel keys."""
        skill_id = "research-run"
        sentinel_cleanup.append(skill_id)
        rc = health_monitor_start.main([skill_id])
        assert rc == 0
        kv = _parse_kv(capsys.readouterr().out)
        assert kv["LAUNCH_AT"].isdigit()
        assert Path(kv["SENTINEL"]).name.startswith(f"research-{skill_id}-check-")


class TestValidation:
    """Argument validation tests."""

    def test_missing_skill_id_exit_one(self, capsys: pytest.CaptureFixture[str]) -> None:
        """No args → exit 1 with error on stderr."""
        rc = health_monitor_start.main([])
        assert rc == 1
        assert "skill-id" in capsys.readouterr().err

    @pytest.mark.parametrize("skill_id", ["bad/id", "bad id", "bad!id", "../evil"])
    def test_invalid_skill_id_exit_two(self, skill_id: str, capsys: pytest.CaptureFixture[str]) -> None:
        """Skill-id with unsafe chars → exit 2 with SKILL_ID error on stderr."""
        rc = health_monitor_start.main([skill_id])
        assert rc == 2
        assert "SKILL_ID" in capsys.readouterr().err


class TestHappyPath:
    """Integration tests for valid invocations."""

    def test_exit_zero(self, capsys: pytest.CaptureFixture[str], sentinel_cleanup: list[str]) -> None:
        """Valid skill-id → exit 0."""
        skill_id = "test-skill-exitcode"
        sentinel_cleanup.append(skill_id)
        rc = health_monitor_start.main([skill_id])
        assert rc == 0

    def test_emits_launch_at_and_sentinel(
        self, capsys: pytest.CaptureFixture[str], sentinel_cleanup: list[str]
    ) -> None:
        """Output contains both ``LAUNCH_AT`` and ``SENTINEL`` keys."""
        skill_id = "test-skill-kv"
        sentinel_cleanup.append(skill_id)
        health_monitor_start.main([skill_id])
        kv = _parse_kv(capsys.readouterr().out)
        assert "LAUNCH_AT" in kv
        assert "SENTINEL" in kv
        assert kv["LAUNCH_AT"].isdigit()

    def test_sentinel_file_created(self, capsys: pytest.CaptureFixture[str], sentinel_cleanup: list[str]) -> None:
        """Sentinel path printed in stdout exists on disk."""
        skill_id = "test-skill-file"
        sentinel_cleanup.append(skill_id)
        health_monitor_start.main([skill_id])
        kv = _parse_kv(capsys.readouterr().out)
        sentinel = Path(kv["SENTINEL"])
        assert sentinel.exists()
        assert sentinel.name.startswith(f"research-{skill_id}-check-")

    def test_launch_at_matches_sentinel_timestamp(
        self, capsys: pytest.CaptureFixture[str], sentinel_cleanup: list[str]
    ) -> None:
        """Append the launch timestamp to the sentinel path."""
        skill_id = "test-skill-tsmatch"
        sentinel_cleanup.append(skill_id)
        health_monitor_start.main([skill_id])
        kv = _parse_kv(capsys.readouterr().out)
        m = re.search(r"-check-(\d+)$", kv["SENTINEL"])
        assert m is not None
        assert m.group(1) == kv["LAUNCH_AT"]

    def test_output_has_no_crlf(self, capsys: pytest.CaptureFixture[str], sentinel_cleanup: list[str]) -> None:
        """Stdout must not contain CRLF (Windows text-mode regression guard)."""
        skill_id = "test-skill-crlf"
        sentinel_cleanup.append(skill_id)
        health_monitor_start.main([skill_id])
        out = capsys.readouterr().out
        assert "\r" not in out

    def test_sentinel_path_uses_forward_slashes(
        self, capsys: pytest.CaptureFixture[str], sentinel_cleanup: list[str]
    ) -> None:
        """SENTINEL value uses forward slashes (bash-compatible even on Windows)."""
        skill_id = "test-skill-posix"
        sentinel_cleanup.append(skill_id)
        health_monitor_start.main([skill_id])
        kv = _parse_kv(capsys.readouterr().out)
        assert "\\" not in kv["SENTINEL"]
