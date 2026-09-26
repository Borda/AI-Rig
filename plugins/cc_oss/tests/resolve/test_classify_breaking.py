"""Tests for ``bin/classify_breaking.py``.

Pure JSON transform — no subprocess, no git. ``main`` reads stdin via ``monkeypatch.setattr(sys, "stdin", ...)``;
``classify`` and the private helpers are exercised directly. Covers Breaking (external caller), internal (same-package
caller only), removed-symbol Breaking, empty-module external treatment, incomplete-coverage propagation, and malformed
stdin.
"""

from __future__ import annotations

import io
import json
import sys

import pytest

import classify_breaking as cb


def _caller(module: str, symbol: str = "f", path: str = "x.py") -> dict:
    """Build a ``called_by`` entry for a caller in ``module``.

    Examples:
        >>> _caller("app.jobs", "run", "jobs.py")
        {'caller': 'app.jobs::run', 'module': 'app.jobs', 'path': 'jobs.py'}
    """
    return {"caller": f"{module}::{symbol}", "module": module, "path": path}


def _batch(entries: list[dict], *, query_complete: bool = True) -> dict:
    """Wrap ``fn-rdeps`` batch entries in a ``codemap-py query batch`` envelope.

    Examples:
        >>> _batch([])
        {'batch': [], 'index': {'query_complete': True}}
        >>> _batch([], query_complete=False)["index"]["query_complete"]
        False
    """
    return {"batch": entries, "index": {"query_complete": query_complete}}


def test_external_caller_labels_breaking() -> None:
    """Caller in a different top-level package → symbol classified Breaking."""
    entry = {"ok": True, "result": {"qname": "mypkg.core::Thing", "called_by": [_caller("app.svc")]}}
    out = cb.classify(_batch([entry]))
    assert len(out["breaking"]) == 1
    assert out["breaking"][0]["symbol"] == "mypkg.core::Thing"
    assert out["breaking"][0]["external_callers"][0]["module"] == "app.svc"
    assert out["internal"] == []


def test_same_package_caller_labels_internal() -> None:
    """Caller inside the symbol's own package → classified internal, not Breaking."""
    entry = {"ok": True, "result": {"qname": "mypkg.core::Thing", "called_by": [_caller("mypkg.util")]}}
    out = cb.classify(_batch([entry]))
    assert out["breaking"] == []
    assert out["internal"][0]["symbol"] == "mypkg.core::Thing"
    assert out["internal"][0]["caller_count"] == 1


def test_no_callers_labels_internal() -> None:
    """Symbol with zero callers → internal with caller_count 0."""
    entry = {"ok": True, "result": {"qname": "mypkg.core::Thing", "called_by": []}}
    out = cb.classify(_batch([entry]))
    assert out["breaking"] == []
    assert out["internal"][0]["caller_count"] == 0


def test_removed_symbol_labels_breaking() -> None:
    """Fn-rdeps error (previously-public symbol absent) → Breaking with removed reason."""
    entry = {"ok": False, "args": ["mypkg.core::Gone"], "result": {"error": "Symbol not found"}}
    out = cb.classify(_batch([entry]))
    assert out["breaking"][0]["symbol"] == "mypkg.core::Gone"
    assert out["breaking"][0]["reason"] == "removed"


def test_empty_module_caller_treated_external() -> None:
    """Caller with an empty module cannot be proven same-package → Breaking."""
    entry = {"ok": True, "result": {"qname": "mypkg.core::Thing", "called_by": [_caller("")]}}
    out = cb.classify(_batch([entry]))
    assert len(out["breaking"]) == 1


def test_migration_lines_emitted_for_external_callers() -> None:
    """Each external caller yields one migration evidence line naming caller and path."""
    entry = {
        "ok": True,
        "result": {"qname": "mypkg.core::Thing", "called_by": [_caller("app.svc", path="app/svc.py")]},
    }
    out = cb.classify(_batch([entry]))
    assert out["migration_lines"] == ["- `mypkg.core::Thing` — called by `app.svc::f` (`app/svc.py`)"]


def test_incomplete_coverage_propagates() -> None:
    """query_complete:false in the shared coverage block → query_complete false in output."""
    entry = {"ok": True, "result": {"qname": "mypkg.core::Thing", "called_by": []}}
    out = cb.classify(_batch([entry], query_complete=False))
    assert out["query_complete"] is False


def test_main_reads_stdin_and_prints_json(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Read a batch from standard input and print its classification as JSON."""
    entry = {"ok": True, "result": {"qname": "mypkg.core::Thing", "called_by": [_caller("app.svc")]}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(_batch([entry]))))
    rc = cb.main([])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["breaking"][0]["symbol"] == "mypkg.core::Thing"


def test_main_malformed_stdin_exits_2(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Non-JSON stdin → exit 2 with an error payload."""
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json {"))
    rc = cb.main([])
    assert rc == 2
    assert "error" in json.loads(capsys.readouterr().out)


def test_golden_invocation_stdin_pipe(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Documented call site (``codemap-py query batch | classify_breaking.py``) — stdin-only, no argv."""
    entry = {"ok": True, "result": {"qname": "mypkg.core::Thing", "called_by": [_caller("app.svc")]}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(_batch([entry]))))
    rc = cb.main([])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["breaking"][0]["symbol"] == "mypkg.core::Thing"
