"""Admit current benchmark models without executing paid coordinates or rewriting old evidence."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from _bench_codex import runtime
from _bench_codex.structural.config import PARITY_CODEX_MODEL
from _bench_codex.structural.diff_impact import build_codex_command
from _bench_codex.structural.runner import CodexRunner
from _bench_common import change_impact_stage

BENCHMARKS = Path(__file__).resolve().parents[1]
_PLATFORM_TESTS_DIR = BENCHMARKS.parent / "plugins" / "codex-rig" / "tests"
if str(_PLATFORM_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_PLATFORM_TESTS_DIR))

from _platform import POSIX_BASH  # noqa: E402

MODELS = ["gpt-6.1-sol", "gpt-6-luna", "gpt-6-sol", "gpt-6-astra"]
_requires_bash = pytest.mark.skipif(POSIX_BASH is None, reason="requires a working POSIX Bash executable")


def test_current_default_models_agree() -> None:
    """Make fresh scopes and structural CLI choose the normal current parent model."""
    assert PARITY_CODEX_MODEL == "gpt-6.1-sol"
    assert change_impact_stage.resolve_scope("codex")["model"] == "gpt-6.1-sol"
    for name in ("codex-integration.json", "codex-agentic.json"):
        model = json.loads((BENCHMARKS / "manifests" / name).read_bytes())["model"]
        assert [model["name"], *model["additional_strata"]] == MODELS


@pytest.mark.parametrize("model", MODELS)
def test_supported_model_admission(model: str) -> None:
    """Admit every explicitly supported model against the fresh study declaration."""
    runtime.validate_codex_stratum(model, "high", BENCHMARKS / "manifests/codex-integration.json")
    assert change_impact_stage.resolve_scope("codex", model=model)["model"] == model


@pytest.mark.parametrize("model", ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.3-codex"])
def test_obsolete_execution_models_rejected_even_if_declared(tmp_path: Path, model: str) -> None:
    """Prevent a historical model declaration from becoming a new executable route."""
    manifest = tmp_path / "old-study.json"
    manifest.write_text(json.dumps({"model": {"name": model, "reasoning_effort": "high"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="supported Codex benchmark model"):
        runtime.validate_codex_stratum(model, "high", manifest)
    with pytest.raises(ValueError, match="supported Codex benchmark model"):
        change_impact_stage.resolve_scope("codex", model=model)


@pytest.mark.integration
@pytest.mark.parametrize("model", [None, *MODELS])
def test_agentic_scope_cli_admits_current_models(model: str | None) -> None:
    """Exercise Fire's real scope CLI without paid authorization or model calls."""
    command = [sys.executable, str(BENCHMARKS / "run-codex-agentic.py"), "--resolve-scope"]
    if model is not None:
        command.append(f"--model={model}")
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    scope = json.loads(result.stdout)
    assert scope.get(
        "model", json.loads((BENCHMARKS / "manifests/codex-agentic.json").read_bytes())["model"]["name"]
    ) == (model or "gpt-6.1-sol")


@pytest.mark.integration
def test_agentic_scope_cli_rejects_obsolete_model() -> None:
    """Reject obsolete selection before a scope or paid invocation can be produced."""
    result = subprocess.run(
        [sys.executable, str(BENCHMARKS / "run-codex-agentic.py"), "--resolve-scope", "--model=gpt-5.6-luna"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "supported Codex benchmark model" in result.stderr


@pytest.mark.integration
@_requires_bash
@pytest.mark.parametrize(
    ("selection", "expected_code", "expected_output"),
    [
        pytest.param("luna", 0, "gpt-6-luna", id="unique-support-nickname"),
        pytest.param("gpt-6.1-sol", 0, "gpt-6.1-sol", id="explicit-parent-model"),
        pytest.param("sol", 1, "more than one declared", id="ambiguous-sol-nickname"),
        pytest.param("gpt-5.6-terra", 1, "not a declared", id="obsolete-shell-selection"),
    ],
)
def test_shell_model_selection_uses_current_declarations(
    selection: str, expected_code: int, expected_output: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolve current models through verified Bash independently of PATH."""
    # The resolver uses only builtins; an empty PATH exposes accidental bare-name invocation.
    monkeypatch.setenv("PATH", str(tmp_path))
    launcher = (BENCHMARKS / "run-all.sh").read_text(encoding="utf-8")
    function = re.search(r"^canonical_provider_model\(\) \{.*?^\}", launcher, re.M | re.S)
    assert function is not None
    models = json.loads((BENCHMARKS / "manifests/provider-parity-methodology.json").read_bytes())[
        "agentic_execution_contract"
    ]["models_by_provider"]["codex"]
    result = subprocess.run(
        [
            POSIX_BASH,
            "-c",
            function[0] + '\ncanonical_provider_model "$1" "$2" codex',
            "model-selection",
            selection,
            " ".join(models),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == expected_code
    assert expected_output in (result.stdout if expected_code == 0 else result.stderr)


@pytest.mark.parametrize("model", ["gpt-5.6-terra", "gpt-5.3-codex"])
def test_transport_and_runner_reject_obsolete_models(tmp_path: Path, model: str) -> None:
    """Reject old routes at both direct runner and command transport boundaries."""
    manifest = tmp_path / "historical.json"
    manifest.write_text(json.dumps({"model": {"name": model, "reasoning_effort": "high"}}))
    with pytest.raises(ValueError, match="supported Codex benchmark model"):
        build_codex_command(tmp_path, model, "neutral prompt")
    with pytest.raises(ValueError, match="supported Codex benchmark model"):
        CodexRunner(model, tmp_path, manifest_path=manifest)


@pytest.mark.integration
@pytest.mark.parametrize("task", ["RC-01", "FS-01", "FM-01", "PT-01"])
def test_unified_stage_cli_rejects_obsolete_model_before_scope(task: str) -> None:
    """Admit stage-native selections through the same model gate as structural cells."""
    result = subprocess.run(
        [
            sys.executable,
            str(BENCHMARKS / "run-codex-structural.py"),
            "--dry-run",
            f"--tasks={task}",
            "--model=gpt-5.6-terra",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "supported Codex benchmark model" in result.stderr
    assert "scope_sha256" not in result.stdout
    assert "PAID_COMMAND" not in result.stdout


@pytest.mark.integration
@_requires_bash
def test_shell_rejects_legacy_route_from_custom_declarations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject historical shell declarations through verified Bash independently of PATH."""
    monkeypatch.setenv("PATH", str(tmp_path))
    launcher = (BENCHMARKS / "run-all.sh").read_text(encoding="utf-8")
    function = re.search(r"^canonical_provider_model\(\) \{.*?^\}", launcher, re.M | re.S)
    assert function is not None
    result = subprocess.run(
        [
            POSIX_BASH,
            "-c",
            function[0] + '\ncanonical_provider_model "$1" "$2" codex',
            "model-selection",
            "gpt-5.6-terra",
            "gpt-5.6-terra",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "supported Codex benchmark model" in result.stderr
    assert not result.stdout


@pytest.mark.integration
def test_structural_cli_omitted_model_reaches_current_parent_admission(tmp_path: Path) -> None:
    """Default execution admits the parent model before resolving task selectors."""
    manifest = tmp_path / "parent-only.json"
    payload = json.loads((BENCHMARKS / "manifests/codex-integration.json").read_bytes())
    payload["model"] = {"name": "gpt-6.1-sol", "reasoning_effort": "high"}
    manifest.write_text(json.dumps(payload))
    for model_args in ([], ["--model=gpt-6.1-sol"]):
        result = subprocess.run(
            [
                sys.executable,
                str(BENCHMARKS / "run-codex-structural.py"),
                "--dry-run",
                f"--repo-path={tmp_path}",
                "--tasks=UNKNOWN",
                f"--manifest-path={manifest}",
                *model_args,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode != 0
        assert "UNKNOWN" in result.stderr
        assert "Codex provider parity requires" not in result.stderr
        assert "supported Codex benchmark model" not in result.stderr
        assert "--model is required" not in result.stderr
        assert "PAID_COMMAND" not in result.stdout


@pytest.mark.parametrize("model", MODELS)
def test_current_model_survives_runner_command_transport(tmp_path: Path, model: str) -> None:
    """Preserve the selected current model exactly in the native provider command."""
    runner = CodexRunner(model, tmp_path)
    command = build_codex_command(tmp_path, runner.model, "neutral prompt")
    assert command[command.index("--model") + 1] == model
    assert command[-1] == "neutral prompt"
