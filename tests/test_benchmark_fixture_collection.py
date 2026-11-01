"""Keep isolated benchmark repositories out of project-wide doctest collection."""

import subprocess
import sys
from pathlib import Path


def test_change_impact_fixture_is_not_collected_as_project_tests() -> None:
    """Collect real fixture parents without importing their isolated example packages.

    ``benchmarks/fixtures`` holds self-contained example repositories with their own tests and packages. A real ``pytest
    --collect-only`` over the fixtures directory and this file must find only this test, never a fixture test.
    """
    root = Path(__file__).resolve().parents[1]
    command = [
        sys.executable,
        "-m",
        "pytest",
        "benchmarks/fixtures",
        "tests/test_benchmark_fixture_collection.py",
        "--collect-only",
        "-q",
        "--no-cov",
    ]

    result = subprocess.run(command, cwd=root, capture_output=True, text=True, check=False, timeout=60)

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "test_change_impact_fixture_is_not_collected_as_project_tests" in output
    assert "benchmarks/fixtures/" not in output
    assert "1 test collected" in output
