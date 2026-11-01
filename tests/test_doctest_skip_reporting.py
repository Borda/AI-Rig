"""Ensure capability-skipped modules report doctest skips without aborting pytest."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Force only the skip decision, not sys.platform: pathlib and pytest must retain real host semantics.
_FORCE_SKIP_PROBE = """
import pytest
import sys

class _ForceCapabilitySkip:
    '''Simulate an unavailable capability without running test bodies.'''
    def pytest_collection_modifyitems(self, items):
        '''Apply a collection-time skip to every selected test and doctest.'''
        for item in items:
            item.add_marker(pytest.mark.skip(reason="capability unavailable in regression probe"))

raise SystemExit(pytest.main(
    [sys.argv[1], "-q", "--doctest-modules", "--no-cov", "--color=no"], plugins=[_ForceCapabilitySkip()]
))
"""


@pytest.mark.integration
@pytest.mark.parametrize(
    "module",
    [
        "benchmarks/tests/entrypoints/test_run_all.py",
        "tests/test_makefile_sync.py",
        "plugins/cc_develop/tests/review/test_enforce_review_header_js.py",
        "plugins/cc_oss/tests/vitality/test_enforce_analyse_header_js.py",
        "plugins/cc_oss/tests/resolve/test_enforce_review_header_js.py",
        "plugins/cc_research/tests/test_enforce_topic_header_js.py",
    ],
)
def test_capability_skips_report_without_internal_error(module: str) -> None:
    """Exercise skip reporting on every host without executing platform-specific test bodies.

    Each module is collected by a real nested pytest with every test and doctest force-skipped at collection. The run
    must end normally and report skips, so a module whose capability gate skips cannot abort the whole session.
    """
    env = os.environ.copy()
    env.pop("PYTEST_ADDOPTS", None)

    result = subprocess.run(
        [sys.executable, "-c", _FORCE_SKIP_PROBE, module],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "skipped" in output, output
    assert "INTERNALERROR" not in output, output
