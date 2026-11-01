"""Admit current benchmark models without executing paid coordinates or rewriting old evidence."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from _bench_codex import runtime
from _bench_codex.structural.config import PARITY_CODEX_MODEL
from _bench_codex.structural.diff_impact import build_codex_command
from _bench_codex.structural.runner import CodexRunner
from _bench_common import change_impact_stage

BENCHMARKS = Path(__file__).resolve().parents[2]
_PLATFORM_TESTS_DIR = BENCHMARKS.parent / "plugins" / "codex-rig" / "tests"
if str(_PLATFORM_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_PLATFORM_TESTS_DIR))

from _platform import POSIX_BASH  # noqa: E402

CURRENT_PARENT_MODEL = "gpt-6.1-sol"
MODELS = [CURRENT_PARENT_MODEL, "gpt-6-luna", "gpt-6-sol", "gpt-6-astra"]
OBSOLETE_MODELS = ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.3-codex"]
UNSUPPORTED_MESSAGE = "supported Codex benchmark model"
_requires_bash = pytest.mark.skipif(POSIX_BASH is None, reason="requires a working POSIX Bash executable")


def _run_cli(script: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run one benchmark launcher script under the current interpreter and capture its text output."""
    return subprocess.run(
        [sys.executable, str(BENCHMARKS / script), *arguments], capture_output=True, text=True, check=False
    )


def _write_historical_study(directory: Path, model: str) -> Path:
    """Write a study manifest declaring *model*, as an archived run would."""
    manifest = directory / "historical.json"
    manifest.write_text(json.dumps({"model": {"name": model, "reasoning_effort": "high"}}), encoding="utf-8")
    return manifest


def _shell_function(script: str, name: str) -> str:
    """Return the shell function *name* from *script*, from its `name() {` line through the closing `}` line."""
    lines = script.splitlines()
    start = lines.index(f"{name}() {{")
    end = lines.index("}", start)
    return "\n".join(lines[start : end + 1])


def _canonical_provider_model(selection: str, declared_models: str) -> subprocess.CompletedProcess[str]:
    """Run the launcher's `canonical_provider_model` shell function under verified Bash for a codex selection."""
    launcher = (BENCHMARKS / "run-all.sh").read_text(encoding="utf-8")
    function = _shell_function(launcher, "canonical_provider_model")
    return subprocess.run(
        [
            POSIX_BASH,
            "-c",
            function + '\ncanonical_provider_model "$1" "$2" codex',
            "model-selection",
            selection,
            declared_models,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


class TestCurrentModelAdmission:
    """Current Codex benchmark models are admitted and defaulted consistently across scope, runner and manifests."""

    def test_default_parent_model_is_the_current_one(self) -> None:
        """Fresh scopes and the structural CLI choose the normal current parent model.

        The structural default and the freshly resolved Codex scope must name the same parent model, otherwise a run
        launched without `--model` would be admitted against a different model than the CLI advertises.
        """
        assert PARITY_CODEX_MODEL == CURRENT_PARENT_MODEL
        assert change_impact_stage.resolve_scope("codex")["model"] == CURRENT_PARENT_MODEL

    @pytest.mark.parametrize("manifest_name", ["codex-integration.json", "codex-agentic.json"])
    def test_study_manifests_declare_the_current_model_strata(self, manifest_name: str) -> None:
        """Each Codex study manifest lists the parent model followed by its additional strata, in order.

        Both studies must declare the same four current models so that scope admission and shell selection agree.
        """
        model = json.loads((BENCHMARKS / "manifests" / manifest_name).read_bytes())["model"]

        assert [model["name"], *model["additional_strata"]] == MODELS

    @pytest.mark.parametrize("model", MODELS)
    def test_supported_model_is_admitted(self, model: str) -> None:
        """Every explicitly supported model is admitted against the fresh study declaration.

        Both the runtime stratum validator and scope resolution must accept the model and echo it back unchanged.
        """
        runtime.validate_codex_stratum(model, "high", BENCHMARKS / "manifests/codex-integration.json")

        assert change_impact_stage.resolve_scope("codex", model=model)["model"] == model

    @pytest.mark.parametrize("model", MODELS)
    def test_model_survives_runner_command_transport(self, tmp_path: Path, model: str) -> None:
        """The selected current model reaches the native provider command exactly.

        A runner built for the model must emit a command whose `--model` value is that model and whose last argument is
        the prompt, so no normalisation silently swaps the model before a paid call.
        """
        runner = CodexRunner(model, tmp_path)

        command = build_codex_command(tmp_path, runner.model, "neutral prompt")

        assert command[command.index("--model") + 1] == model
        assert command[-1] == "neutral prompt"


class TestObsoleteModelRejection:
    """Historical models stay rejected at every entry point even when an old study manifest declares them."""

    @pytest.mark.parametrize("model", OBSOLETE_MODELS)
    def test_runtime_rejects_a_declared_obsolete_stratum(self, tmp_path: Path, model: str) -> None:
        """A historical model declaration cannot become a new executable route through stratum validation.

        The manifest itself names the obsolete model, so validation against that manifest would pass if declaration
        alone granted admission; the supported-model gate must still refuse it.
        """
        manifest = _write_historical_study(tmp_path, model)

        with pytest.raises(ValueError, match=UNSUPPORTED_MESSAGE):
            runtime.validate_codex_stratum(model, "high", manifest)

    @pytest.mark.parametrize("model", OBSOLETE_MODELS)
    def test_scope_resolution_rejects_an_obsolete_model(self, model: str) -> None:
        """Scope resolution refuses an obsolete model instead of producing a scope for it.

        No scope, and therefore no paid-approval scope hash, may exist for a model outside the supported set.
        """
        with pytest.raises(ValueError, match=UNSUPPORTED_MESSAGE):
            change_impact_stage.resolve_scope("codex", model=model)

    @pytest.mark.parametrize("model", OBSOLETE_MODELS)
    def test_command_builder_rejects_an_obsolete_model(self, tmp_path: Path, model: str) -> None:
        """The command transport refuses to build a Codex command for an obsolete model.

        This is the last step before a subprocess launch, so the gate must hold here even if a caller skipped earlier
        admission.
        """
        with pytest.raises(ValueError, match=UNSUPPORTED_MESSAGE):
            build_codex_command(tmp_path, model, "neutral prompt")

    @pytest.mark.parametrize("model", OBSOLETE_MODELS)
    def test_runner_rejects_an_obsolete_model_declared_in_its_manifest(self, tmp_path: Path, model: str) -> None:
        """A direct runner construction refuses an obsolete model even when its own manifest declares it.

        The runner is a second entry point beside the CLI; a historical manifest must not let it be built for an old
        route.
        """
        manifest = _write_historical_study(tmp_path, model)

        with pytest.raises(ValueError, match=UNSUPPORTED_MESSAGE):
            CodexRunner(model, tmp_path, manifest_path=manifest)


@pytest.mark.integration
class TestStructuralAndAgenticCli:
    """The real launcher CLIs admit current models and refuse obsolete ones without any paid call."""

    @pytest.mark.parametrize(
        ("arguments", "expected_model"),
        [
            pytest.param([], CURRENT_PARENT_MODEL, id="omitted-model"),
            *[pytest.param([f"--model={model}"], model, id=model) for model in MODELS],
        ],
    )
    def test_agentic_scope_admits_current_models(self, arguments: list[str], expected_model: str) -> None:
        """The agentic scope CLI resolves a scope for each current model without paid authorization or model calls.

        Run for real because the exit code and JSON scope printed by the launcher are the contract; an omitted `--model`
        must resolve to the current parent model.
        """
        manifest = json.loads((BENCHMARKS / "manifests/codex-agentic.json").read_bytes())

        result = _run_cli("run-codex-agentic.py", "--resolve-scope", *arguments)

        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout).get("model", manifest["model"]["name"]) == expected_model

    def test_agentic_scope_rejects_an_obsolete_model(self) -> None:
        """An obsolete selection is rejected before any scope or paid invocation can be produced.

        The launcher must exit non-zero and name the supported-model rule on stderr.
        """
        result = _run_cli("run-codex-agentic.py", "--resolve-scope", "--model=gpt-5.6-luna")

        assert result.returncode != 0
        assert UNSUPPORTED_MESSAGE in result.stderr

    @pytest.mark.parametrize("task", ["RC-01", "FS-01", "FM-01", "PT-01"])
    def test_stage_dry_run_rejects_an_obsolete_model_before_scope(self, task: str) -> None:
        """Stage-native task selections go through the same model gate as structural cells.

        Each stage family (RC, FS, FM, PT) must stop at the model gate: no scope hash and no paid command may be printed
        for an obsolete model.
        """
        result = _run_cli("run-codex-structural.py", "--dry-run", f"--tasks={task}", "--model=gpt-5.6-terra")

        assert result.returncode != 0
        assert UNSUPPORTED_MESSAGE in result.stderr
        assert "scope_sha256" not in result.stdout
        assert "PAID_COMMAND" not in result.stdout

    @pytest.mark.parametrize(
        "model_arguments",
        [pytest.param([], id="omitted-model"), pytest.param([f"--model={CURRENT_PARENT_MODEL}"], id="explicit-parent")],
    )
    def test_omitted_model_reaches_current_parent_admission(self, tmp_path: Path, model_arguments: list[str]) -> None:
        """Default execution admits the parent model before resolving task selectors.

        With a parent-only manifest and an unknown task, the run must fail on the unknown task itself, never on model
        admission, a missing `--model`, or the provider-parity requirement, and must print no paid command.
        """
        manifest = tmp_path / "parent-only.json"
        payload = json.loads((BENCHMARKS / "manifests/codex-integration.json").read_bytes())
        payload["model"] = {"name": CURRENT_PARENT_MODEL, "reasoning_effort": "high"}
        manifest.write_text(json.dumps(payload), encoding="utf-8")

        result = _run_cli(
            "run-codex-structural.py",
            "--dry-run",
            f"--repo-path={tmp_path}",
            "--tasks=UNKNOWN",
            f"--manifest-path={manifest}",
            *model_arguments,
        )

        assert result.returncode != 0
        assert "UNKNOWN" in result.stderr
        assert "Codex provider parity requires" not in result.stderr
        assert UNSUPPORTED_MESSAGE not in result.stderr
        assert "--model is required" not in result.stderr
        assert "PAID_COMMAND" not in result.stdout


@pytest.mark.integration
@_requires_bash
class TestShellModelSelection:
    """The launcher's `canonical_provider_model` shell function resolves models through verified Bash alone."""

    @pytest.mark.parametrize(
        ("selection", "expected_code", "expected_output"),
        [
            pytest.param("luna", 0, "gpt-6-luna", id="unique-support-nickname"),
            pytest.param(CURRENT_PARENT_MODEL, 0, CURRENT_PARENT_MODEL, id="explicit-parent-model"),
            pytest.param("sol", 1, "more than one declared", id="ambiguous-sol-nickname"),
            pytest.param("gpt-5.6-terra", 1, "not a declared", id="obsolete-shell-selection"),
        ],
    )
    def test_resolves_current_declarations(
        self, selection: str, expected_code: int, expected_output: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nicknames resolve uniquely, ambiguous ones fail, and obsolete selections are not declared.

        The resolver uses only shell builtins; an empty PATH exposes any accidental bare-name tool invocation. The
        declared models come from the methodology manifest, so the shell and Python sides cannot drift apart.
        """
        monkeypatch.setenv("PATH", str(tmp_path))
        methodology = json.loads((BENCHMARKS / "manifests/provider-parity-methodology.json").read_bytes())
        declared = methodology["agentic_execution_contract"]["models_by_provider"]["codex"]

        result = _canonical_provider_model(selection, " ".join(declared))

        assert result.returncode == expected_code
        assert expected_output in (result.stdout if expected_code == 0 else result.stderr)

    def test_rejects_a_legacy_route_from_custom_declarations(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A historical route is rejected even when the shell is handed it as the only declared model.

        Declaration alone must not admit an obsolete model: the function exits 1, names the supported-model rule on
        stderr, and prints nothing to stdout.
        """
        monkeypatch.setenv("PATH", str(tmp_path))

        result = _canonical_provider_model("gpt-5.6-terra", "gpt-5.6-terra")

        assert result.returncode == 1
        assert UNSUPPORTED_MESSAGE in result.stderr
        assert not result.stdout
