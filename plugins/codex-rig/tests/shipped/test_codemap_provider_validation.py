"""Reject malformed provider metadata without crashing optional structural lookup."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from _modules import load_module_registered

ADAPTER = Path(__file__).resolve().parents[2] / "shared" / "codemap_adapter.py"


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> tuple[ModuleType, dict[str, Any], list[list[str]]]:
    """Supply a healthy doctor and mutable query response at the subprocess boundary."""
    adapter = load_module_registered("codemap_provider_validation", ADAPTER)
    responses: dict[str, Any] = {
        "doctor": {
            "python": "python",
            "version": "3.12.4",
            "implementation": "cpython",
            "supported": True,
            "plugin_root": "provider",
            "index_path": "index.json",
        },
        "query": {"index": {"stale": False, "query_complete": True, "degraded": 0, "not_covered": []}},
    }
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Return public JSON from the selected provider endpoint."""
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps(responses[argv[1]]), "")

    monkeypatch.setattr(adapter.shutil, "which", lambda _: "codemap-py")
    monkeypatch.setattr(adapter.subprocess, "run", run)
    return adapter, responses, calls


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("degraded", None, id="null-count"),
        pytest.param("degraded", "many", id="text-count"),
        pytest.param("degraded", True, id="boolean-count"),
        pytest.param("degraded", -1, id="negative-count"),
        pytest.param("stale", "false", id="string-false"),
        pytest.param("query_complete", "false", id="string-completeness"),
        pytest.param("not_covered", None, id="null-coverage"),
        pytest.param("not_covered", "dynamic", id="string-coverage"),
        pytest.param("not_covered", [1], id="nontext-coverage"),
        pytest.param("root_mismatch", "false", id="string-root-verdict"),
    ],
)
def test_malformed_query_metadata_is_incompatible(
    provider: tuple[ModuleType, dict[str, Any], list[list[str]]], field: str, value: Any
) -> None:
    """Malformed completeness metadata never becomes a successful or stale answer."""
    adapter, responses, _ = provider
    responses["query"]["index"][field] = value
    context = adapter.gather_structural_context("analysis", query_kind="central")
    assert context.status == "incompatible"
    assert context.queries[0].error.startswith("query payload invalid:")
    assert context.queries[0].stale is False


@pytest.mark.parametrize("field", ["python", "version", "implementation", "plugin_root", "index_path", "supported"])
def test_malformed_doctor_metadata_is_incompatible(
    provider: tuple[ModuleType, dict[str, Any], list[list[str]]], field: str
) -> None:
    """Doctor fields retain their declared string and boolean types."""
    adapter, responses, calls = provider
    responses["doctor"][field] = "false" if field == "supported" else None
    result = adapter.probe_codemap()
    assert result.status == "incompatible"
    assert len(calls) == 1


@pytest.mark.parametrize("kind", ["standard", "callers", "dependencies", "blast", "test-impact"])
def test_option_shaped_targets_never_reach_provider(
    provider: tuple[ModuleType, dict[str, Any], list[list[str]]], kind: str
) -> None:
    """A target beginning with a dash cannot become a provider CLI option."""
    adapter, _, calls = provider
    context = adapter.gather_structural_context("analysis", target="  --help", query_kind=kind)
    assert all("--help" not in command and "  --help" not in command for command in calls)
    assert any(query.error is not None and "target malformed" in query.error for query in context.queries)


def test_invalid_provider_metadata_keeps_cli_exit_zero(
    provider: tuple[ModuleType, dict[str, Any], list[list[str]]], capsys: pytest.CaptureFixture[str]
) -> None:
    """Provider incompatibility remains data at the adapter CLI boundary."""
    adapter, responses, _ = provider
    responses["query"]["index"]["degraded"] = None
    assert adapter.main(["context", "--category", "analysis", "--query-kind", "central"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "incompatible"
