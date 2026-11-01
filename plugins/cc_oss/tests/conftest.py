"""Auto-load all bin/ Python scripts as importable modules.

Registers each ``plugins/cc_oss/bin/<name>.py`` under the alias ``<name>`` with hyphens replaced by underscores, so
tests can ``from parse_resolve_args import parse_resolve_args`` directly. Also holds the fixtures shared by several test
folders: ``workdir``, ``feed_stdin`` and ``tmp_sentinels``.
"""

from __future__ import annotations

import importlib.util
import io
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).parent.parent / "bin"
_TESTS_DIR = Path(__file__).parent

# Pytest's importlib mode leaves the tests directory off sys.path; the ``_``-prefixed helper modules need it.
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run the test from an empty temporary directory and return it."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def _no_session_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip session-scoping env vars so TMPDIR sentinel tests are deterministic.

    Without this, a test run from inside a live Claude Code session inherits ``CLAUDE_CODE_SESSION_ID`` from the ambient
    shell, making CSID-suffixed filenames unpredictable across environments. Absent both vars, bin/ scripts fall back to
    the literal ``"shared"`` token.
    """
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CSID", raising=False)


@pytest.fixture
def feed_stdin(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    """Return a setter that makes ``sys.stdin`` yield the given text."""

    def _feed(text: str) -> None:
        """Replace ``sys.stdin`` with a stream holding ``text``."""
        monkeypatch.setattr(sys, "stdin", io.StringIO(text))

    return _feed


@pytest.fixture
def tmp_sentinels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point sentinel writes at an isolated temp directory under the fixed session token ``shared``."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "shared")
    return tmp_path


def _load_bin_modules() -> None:
    """Load every plugin ``bin`` script under its importable test alias."""
    for script in sorted(_BIN_DIR.glob("*.py")):
        module_name = script.stem.replace("-", "_")
        if module_name in sys.modules:
            continue
        spec = importlib.util.spec_from_file_location(module_name, script)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)  # type: ignore[union-attr]


_load_bin_modules()
