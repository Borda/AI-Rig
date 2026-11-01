"""Tests for ``bin/classify_breaking.py``.

Pure JSON transform — no subprocess, no git. ``main`` reads stdin via ``monkeypatch.setattr(sys, "stdin", ...)``;
``classify`` is exercised directly. Covers Breaking (external caller), internal (same-package caller only), removed-
symbol Breaking, empty-module external treatment, incomplete-coverage propagation, and malformed stdin.
"""

from __future__ import annotations

import io
import json
import sys

import classify_breaking as cb
import pytest

_QNAME = "mypkg.core::Thing"


def _caller(module: str, symbol: str = "f", path: str = "x.py") -> dict:
    """Build a ``called_by`` entry for a caller in ``module``.

    Examples:
        >>> _caller("app.jobs", "run", "jobs.py")
        {'caller': 'app.jobs::run', 'module': 'app.jobs', 'path': 'jobs.py'}
    """
    return {"caller": f"{module}::{symbol}", "module": module, "path": path}


def _entry(*callers: dict) -> dict:
    """Build a successful ``fn-rdeps`` batch entry for ``mypkg.core::Thing`` with the given callers."""
    return {"ok": True, "result": {"qname": _QNAME, "called_by": list(callers)}}


def _batch(entries: list[dict], *, query_complete: bool = True) -> dict:
    """Wrap ``fn-rdeps`` batch entries in a ``codemap-py query batch`` envelope.

    Examples:
        >>> _batch([])
        {'batch': [], 'index': {'query_complete': True}}
        >>> _batch([], query_complete=False)["index"]["query_complete"]
        False
    """
    return {"batch": entries, "index": {"query_complete": query_complete}}


class TestClassify:
    """``classify`` labels each queried symbol Breaking or internal from its callers."""

    def test_external_caller_labels_breaking(self) -> None:
        """A caller in a different top-level package makes the symbol Breaking.

        ``app.svc`` is outside ``mypkg``, so renaming or changing the symbol breaks code the PR does not control.
        """
        out = cb.classify(_batch([_entry(_caller("app.svc"))]))
        assert len(out["breaking"]) == 1
        assert out["breaking"][0]["symbol"] == _QNAME
        assert out["breaking"][0]["external_callers"][0]["module"] == "app.svc"
        assert out["internal"] == []

    def test_same_package_caller_labels_internal(self) -> None:
        """A caller inside the symbol's own package leaves it internal, not Breaking.

        Same-package callers ship in the same change, so they cannot be broken from outside.
        """
        out = cb.classify(_batch([_entry(_caller("mypkg.util"))]))
        assert out["breaking"] == []
        assert out["internal"][0]["symbol"] == _QNAME
        assert out["internal"][0]["caller_count"] == 1

    def test_no_callers_labels_internal(self) -> None:
        """A symbol with zero callers is internal with ``caller_count`` 0.

        Nothing depends on it, so changing it is not a breaking change.
        """
        out = cb.classify(_batch([_entry()]))
        assert out["breaking"] == []
        assert out["internal"][0]["caller_count"] == 0

    def test_removed_symbol_labels_breaking(self) -> None:
        """A failed ``fn-rdeps`` lookup (previously public symbol now absent) is Breaking with reason ``removed``.

        The symbol was queried because the PR removed it; the "not found" error is the evidence of removal.
        """
        entry = {"ok": False, "args": ["mypkg.core::Gone"], "result": {"error": "Symbol not found"}}
        out = cb.classify(_batch([entry]))
        assert out["breaking"][0]["symbol"] == "mypkg.core::Gone"
        assert out["breaking"][0]["reason"] == "removed"

    def test_empty_module_caller_treated_external(self) -> None:
        """A caller with an empty module cannot be proven same-package, so it counts as external.

        Failing closed here keeps an unattributable caller from hiding a real break.
        """
        out = cb.classify(_batch([_entry(_caller(""))]))
        assert len(out["breaking"]) == 1

    def test_migration_lines_emitted_for_external_callers(self) -> None:
        """Each external caller yields one migration evidence line naming caller and path.

        The line is pasted into the review report so the author can see exactly which call sites need updating.
        """
        out = cb.classify(_batch([_entry(_caller("app.svc", path="app/svc.py"))]))
        assert out["migration_lines"] == ["- `mypkg.core::Thing` — called by `app.svc::f` (`app/svc.py`)"]

    def test_incomplete_coverage_propagates(self) -> None:
        """``query_complete: false`` in the shared coverage block yields ``query_complete`` false in the output.

        A partial caller index must not read as "no external callers"; the flag lets the report say coverage is partial.
        """
        out = cb.classify(_batch([_entry()], query_complete=False))
        assert out["query_complete"] is False


class TestMain:
    """CLI behaviour — stdin in, JSON out, error payload on unusable input."""

    def test_reads_stdin_and_prints_json(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A batch on standard input is classified and printed as JSON with exit 0.

        This is the shape the skill pipes from ``codemap-py query batch``.
        """
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(_batch([_entry(_caller("app.svc"))]))))
        rc = cb.main([])
        assert rc == 0
        assert json.loads(capsys.readouterr().out)["breaking"][0]["symbol"] == _QNAME

    @pytest.mark.parametrize(
        "stdin_text",
        [
            pytest.param("not json {", id="malformed-json"),
            pytest.param("[" * 200_000, id="nesting-beyond-recursion-limit"),
        ],
    )
    def test_unusable_stdin_exits_2_with_error_payload(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], stdin_text: str
    ) -> None:
        """Non-JSON or absurdly deep stdin gives exit 2 and an ``error`` payload, never a traceback.

        The batch comes from a subprocess pipe, so a runaway or hostile producer can send arbitrarily deep brackets;
        that must produce the same error payload as plain malformed text rather than a ``RecursionError``.
        """
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_text))
        rc = cb.main([])
        assert rc == 2
        assert "error" in json.loads(capsys.readouterr().out)
