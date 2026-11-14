"""Shared platform fixtures for Codex Rig tests."""

from __future__ import annotations

import hashlib
import os
import sys
from collections.abc import Generator
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).parent
_OFF_PATH_PARTS = frozenset({"__pycache__", "fixtures"})


def _module_dir(path: Path) -> bool:
    """Return whether ``path`` is a test directory whose modules join the flat test namespace."""
    parts = path.relative_to(_TESTS_DIR).parts
    return path.is_dir() and _OFF_PATH_PARTS.isdisjoint(parts) and not any(part.startswith(".") for part in parts)


# Domain subdirectories and their topic folders share one flat module namespace so cross-group helper imports keep
# resolving after grouping; fixture trees stay off the path so their modules cannot shadow shipped ones.
for _MODULE_DIR in (_TESTS_DIR, *sorted(filter(_module_dir, _TESTS_DIR.rglob("*")))):
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


@pytest.fixture(scope="session", autouse=True)
def hermetic_git_config(tmp_path_factory: pytest.TempPathFactory) -> Generator[None, None, None]:
    """Keep every test's Git independent of the machine's global and system configuration.

    An ambient executable filter such as Git LFS makes the diff collector's safety check run seven more Git processes on
    each call, so a test's duration and behaviour would otherwise depend on the developer or runner it happens to run
    on. Repository-local configuration still applies, so tests that exercise filter rejection configure their own.

    Shipped helpers strip every ``GIT_*`` variable before running Git, so the home directory is redirected too: Git
    reads the global configuration from there. The system configuration stays in place: shipped helpers would read it
    regardless, and Git for Windows keeps platform semantics there (symbolic links, line endings), so ignoring it for
    test-side Git alone makes the tests and the shipped helpers disagree about the same repository. Windows keeps its
    configuration entirely for the same reason; the CI workflow removes the runner's system LFS filter instead.
    """
    if os.name == "nt":
        yield
        return
    home = tmp_path_factory.mktemp("git-home")
    empty = home / "gitconfig"
    empty.write_bytes(b"")
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("GIT_CONFIG_GLOBAL", os.fspath(empty))
        for variable in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME"):
            patch.setenv(variable, os.fspath(home))
        yield


def pytest_configure(config: pytest.Config) -> None:
    """Register selectors when tests run from a payload without repository configuration."""
    for marker in (
        "installed_plugin: runs against an installed plugin without repository context",
        "integration: exercises interactions between real components or processes",
        "live: uses real external services, user credentials, or provider budget",
        "packaging: validates plugin build and distribution contracts",
        "xdist_group: keeps shared fixture consumers on one package shard and xdist worker",
    ):
        config.addinivalue_line("markers", marker)


def pytest_addoption(parser: pytest.Parser) -> None:
    """Expose deterministic partitions for the copied-package acceptance harness."""
    parser.addoption("--installed-package-shard", help="Run one zero-based INDEX/COUNT partition of selected tests")


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> Generator[None, None, None]:
    """Partition the final marker-selected collection identically on every child worker."""
    shard = config.getoption("--installed-package-shard")
    coordinates = None
    if shard is not None:
        try:
            index, count = map(int, shard.split("/"))
            if not 0 <= index < count:
                raise ValueError
        except ValueError as error:
            raise pytest.UsageError("shard must be INDEX/COUNT with 0 <= INDEX < COUNT") from error
        coordinates = index, count
    yield
    if coordinates is None:
        return
    index, count = coordinates
    selected, deselected = [], []
    for item in items:
        # Preserve xdist fixture groups across shards as well as workers, avoiding repeated expensive setup.
        groups = {
            str(mark.args[0] if mark.args else mark.kwargs.get("name", "default"))
            for mark in item.iter_markers("xdist_group")
        }
        identity = "_".join(sorted(groups)) if groups else item.nodeid
        destination = int.from_bytes(hashlib.sha256(identity.encode("utf-8")).digest(), "big") % count
        (selected if destination == index else deselected).append(item)
    items[:] = selected
    config.hook.pytest_deselected(items=deselected)


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
