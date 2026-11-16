"""Production-only in-degree for ``central --exclude-tests``.

A module imported only by test modules has no production importer, so it is absent from the recomputed production
counts. The ranking must score it zero rather than fall back to its stored ``rdep_count``, which includes test
importers.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SRC_DIR = Path(__file__).resolve().parent.parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

import codemap_py.query as query_mod  # noqa: E402
from codemap_py.schema import EntityType  # noqa: E402


def _module(name: str, imports: list[str] | None = None, *, is_test: bool = False, stored_count: int = 0) -> dict:
    """Build a minimal index module entry with a chosen stored ``rdep_count``."""
    return {
        "name": name,
        "status": "ok",
        "path": f"{name.replace('.', '/')}.py",
        "direct_imports": imports or [],
        "is_test": is_test,
        "rdep_count": stored_count,
    }


def _index() -> dict:
    """Build an index where ``tested_only`` is imported by a test module alone and carries a stale stored count."""
    return {
        "modules": [
            _module("used"),
            _module("tested_only", stored_count=5),
            _module("prod.consumer", ["used"]),
            _module("tests.test_tested_only", ["tested_only"], is_test=True),
        ],
    }


class TestCentralExcludingTests:
    """Ranking by production importers only."""

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            pytest.param(
                {},
                [("used", 1), ("prod.consumer", 0), ("tested_only", 0)],
                id="repository-wide",
            ),
            pytest.param(
                {"among": ["used", "tested_only"]},
                [("used", 1), ("tested_only", 0)],
                id="among",
            ),
            pytest.param(
                {"entity": EntityType.PKG},
                [("used", 1), ("prod.consumer", 0), ("tested_only", 0)],
                id="entity",
            ),
        ],
    )
    def test_scores_a_test_only_imported_module_as_zero(self, capsys, kwargs, expected):
        """A module imported only by tests reports zero and ranks behind every module with a production importer.

        ``tested_only`` stores a ``rdep_count`` of 5 from its test importer. With ``--exclude-tests`` the production
        graph has no edge into it, so the reported count must be zero, not the stored five, in every query form.
        """
        query_mod.cmd_central(_index(), top=None, exclude_tests=True, **kwargs)

        central = json.loads(capsys.readouterr().out)["central"]

        assert [(entry["name"], entry["rdep_count"]) for entry in central] == expected
