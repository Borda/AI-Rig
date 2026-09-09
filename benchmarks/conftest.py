"""Make ``_bench_common`` importable and generate the manifests collection depends on.

``--doctest-modules`` (repo-root ``pyproject.toml``) imports every module under ``benchmarks/`` during collection, and
``--import-mode=importlib`` deliberately does not put a module's parent directory on ``sys.path``. Consequently, the
benchmark support import fails for modules collected outside ``benchmarks/tests/``, whose own conftest inserts the
directory only for that subtree. This parent-level conftest runs first for the whole ``benchmarks/`` tree and applies
the same insert once.

The manifest session hooks live here rather than in ``benchmarks/tests/conftest.py`` because ``ini_options.testpaths``
starts at ``benchmarks/``: only conftests for the initial collection roots load before ``pytest_sessionstart``, so a
hook one level down never ran, and every module reading a gitignored manifest at import time — the runners collected as
doctest modules included — failed collection on a fresh clone.
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

_BENCHMARKS_DIR = Path(__file__).resolve().parent
if str(_BENCHMARKS_DIR) not in sys.path:
    sys.path.insert(0, str(_BENCHMARKS_DIR))

from _bench_common import manifest_session  # noqa: E402 — needs the sys.path insert above.

# This is an isolated input repository, not importable project doctest modules.
# Dedicated change-impact tests validate it in its own repository context.
collect_ignore = ["fixtures/change-impact"]


@pytest.hookimpl(tryfirst=True)
def pytest_sessionstart(session: pytest.Session) -> None:
    """Build the generated manifests before any collected module imports them.

    Args:
        session: Active pytest session, whose config records the build outcome.
    """
    manifest_session.start_session(session)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Restore the pre-session manifest state of an ignored checkout.

    Args:
        session: Active pytest session that carries the recorded artifacts.
        exitstatus: Session exit status (unused; required by the hook signature).
    """
    manifest_session.finish_session(session)


@pytest.fixture(name="generated_manifest_artifacts", scope="session")
def _generated_manifest_artifacts(pytestconfig: pytest.Config) -> manifest_session.GeneratedManifestArtifacts:
    """Expose session-generated manifest paths and build evidence without rebuilding them.

    >>> artifacts = getfixture("generated_manifest_artifacts")
    >>> artifacts is getfixture("pytestconfig")._generated_manifest_artifacts
    True
    """
    return pytestconfig._generated_manifest_artifacts
