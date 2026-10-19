"""Shared platform fixtures for Codex Rig tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).parent
# Domain subdirectories share one flat module namespace so cross-group helper imports keep resolving after grouping.
for _MODULE_DIR in (
    _TESTS_DIR,
    *sorted(path for path in _TESTS_DIR.iterdir() if path.is_dir() and path.name != "__pycache__"),
):
    if str(_MODULE_DIR) not in sys.path:
        sys.path.insert(0, str(_MODULE_DIR))

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

    # Every test using one newline default patches an equivalent writer; the tag lets cached fixture trees be shared.
    write_with_default.template_identity = ("text_newline_default", request.param)  # type: ignore[attr-defined]
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


def pytest_runtest_logstart(nodeid: str, location: tuple[str, int | None, str]) -> None:
    """Expose active installed-package cases even when a child times out before reporting."""
    if os.environ.get("CODEX_RIG_PACKAGE_TIMINGS") == "1":
        print(f"\n[installed-package] start {nodeid}", flush=True)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Flush completed phase timings so call completion cannot conceal a teardown stall."""
    if os.environ.get("CODEX_RIG_PACKAGE_TIMINGS") == "1":
        print(
            f"\n[installed-package] {report.when} {report.outcome} {report.nodeid} duration={report.duration:.3f}s",
            flush=True,
        )


@pytest.fixture(name="posix_bash", scope="session")
def _posix_bash() -> str:
    """Return the decorator-validated POSIX Bash executable."""
    assert POSIX_BASH is not None, "POSIX Bash test lacks requires_posix_bash"
    return POSIX_BASH
