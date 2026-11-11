"""Repeated recursive globs retain semantics without repeated regex branches."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import check_iteration_scope as scope
import pytest


def test_repeated_recursive_segments_reject_nonmatch_promptly() -> None:
    """Redundant recursive segments cannot stall the scope guard on a nested nonmatching path."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); import check_iteration_scope as s; "
            "print(bool(s._glob_regex('src/' + '**/' * 80 + '*.py').fullmatch('src/' + 'pkg/' * 24 + 'x.txt')))",
            str(Path(scope.__file__).parent),
        ],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "False\n"


@pytest.mark.parametrize("path", ["src/module.py", "src/pkg/module.py", "src/pkg/deeper/module.py"])
def test_recursive_collapse_preserves_matching(path: str) -> None:
    """Both direct children and nested paths remain in the declared scope."""
    assert re.fullmatch(scope._glob_regex("src/**/**/**/module.py"), path) is not None
