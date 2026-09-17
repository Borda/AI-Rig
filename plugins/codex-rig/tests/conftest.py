"""Shared platform fixtures for Codex Rig tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from _platform import POSIX_BASH  # noqa: E402


@pytest.fixture(params=["\n", "\r\n"])
def text_newline_default(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise fixture text writes under both host newline defaults on every platform."""
    write_text = Path.write_text

    def write_with_default(
        path: Path, data: str, encoding: str | None = None, errors: str | None = None, newline: str | None = None
    ) -> int:
        """Translate only implicit newlines; preserve explicit byte-stability choices."""
        return write_text(
            path, data, encoding=encoding, errors=errors, newline=request.param if newline is None else newline
        )

    monkeypatch.setattr(Path, "write_text", write_with_default)


def pytest_configure(config: pytest.Config) -> None:
    """Register selectors when tests run from a payload without repository configuration."""
    for marker in (
        "installed_plugin: runs against an installed plugin without repository context",
        "integration: exercises interactions between real components or processes",
        "live: uses real external services, user credentials, or provider budget",
        "packaging: validates plugin build and distribution contracts",
    ):
        config.addinivalue_line("markers", marker)


@pytest.fixture(name="posix_bash", scope="session")
def _posix_bash() -> str:
    """Return the decorator-validated POSIX Bash executable."""
    assert POSIX_BASH is not None, "POSIX Bash test lacks requires_posix_bash"
    return POSIX_BASH
