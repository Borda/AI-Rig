"""Collect every Bash command the two decision-module suites parametrize over.

Used only by :mod:`_capture_hook_stdout` when a new stdout baseline is generated. The commands it returns are frozen
into the capture fixture as its keys, so the test suite that replays the captures never runs this module and never
depends on pytest collection succeeding.

Collection goes through pytest rather than a hand-maintained list because the two suites grow: a bypass proof-of-concept
added to the sentinel suite belongs in the next baseline automatically, not once somebody remembers to copy it.
"""

from __future__ import annotations

from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent

#: Suites whose parametrized ``command`` values, and whose shared-vector ``input`` values, form the corpus.
SUITES = ("test_blueprint_allow_js.py", "test_sentinel_read_allow_js.py")


class _Collector:
    """Pytest plugin that records the string parameters of every collected test."""

    def __init__(self) -> None:
        self.commands: set[str] = set()

    def pytest_collection_modifyitems(self, items: list) -> None:
        """Harvest ``command`` parameters and the ``input`` field of shared normalization vectors."""
        for item in items:
            callspec = getattr(item, "callspec", None)
            if callspec is None:
                continue
            value = callspec.params.get("command")
            if isinstance(value, str):
                self.commands.add(value)
            vector = callspec.params.get("vector")
            if isinstance(vector, dict) and isinstance(vector.get("input"), str):
                self.commands.add(vector["input"])


def collect_commands() -> list[str]:
    """Return the sorted corpus of commands the two suites exercise.

    Raises:
        RuntimeError: when collection yields nothing, which would silently produce an empty baseline.
    """
    import pytest

    collector = _Collector()
    code = pytest.main(
        [
            *(str(_TESTS_DIR / suite) for suite in SUITES),
            "--collect-only",
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
            # The project's ``addopts`` turn on coverage for every run; collection neither needs it nor may pay for it.
            "-o",
            "addopts=",
        ],
        plugins=[collector],
    )
    if int(code) != 0:
        raise RuntimeError(f"pytest collection failed with exit {code}")
    if not collector.commands:
        raise RuntimeError("collected no commands — the capture would be empty")
    return sorted(collector.commands)
