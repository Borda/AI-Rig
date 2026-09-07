"""No-model compatibility tests for Codex query telemetry and offline replay."""

from __future__ import annotations

import importlib.util
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from benchmarks._bench_common import provider_parity_contracts as core


BENCHMARKS_DIR = Path(__file__).resolve().parent.parent
SCRIPT_PATH = BENCHMARKS_DIR / "run-codex-structural.py"
SUITE_PATH = BENCHMARKS_DIR / "suites" / "tasks-bench.json"
MANIFEST_PATH = BENCHMARKS_DIR / "manifests" / "codex-integration.json"
POSIX_SECURITY = pytest.mark.skipif(
    sys.platform == "win32", reason="requires POSIX private-mode and executable-shell semantics"
)


@pytest.fixture(name="script_run_codex", scope="module")
def _script_run_codex() -> Any:
    """Load the Codex adapter without executing its command-line entry point.

    Example:
        >>> getfixture("script_run_codex").__name__
        'run_codemap_codex'
    """
    spec = importlib.util.spec_from_file_location("run_codemap_codex", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load Codex adapter at {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _make_direct_runtime_bundle(root: Path) -> Path:
    """Create the minimum source-shaped direct CLI runtime used by isolation tests.

    Example:
        >>> from tempfile import TemporaryDirectory
        >>> with TemporaryDirectory() as directory:
        ...     launcher = _make_direct_runtime_bundle(Path(directory))
        ...     launcher.name, (launcher.parents[1] / "src/codemap_py/__init__.py").is_file()
        ('codemap-py', True)
    """
    runtime = root / "codemap-runtime"
    launcher = runtime / "bin" / "codemap-py"
    exclusions = runtime / "bin" / "_exclusions.py"
    entrypoint = runtime / "scripts" / "codemap_py_entry.py"
    package = runtime / "src" / "codemap_py"
    launcher.parent.mkdir(parents=True)
    entrypoint.parent.mkdir(parents=True)
    package.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o755)
    exclusions.write_text("EXCLUSION_PATTERNS = ()\n", encoding="utf-8")
    entrypoint.write_text("raise SystemExit(0)\n", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    return launcher


def _completed_stream(
    *,
    output: str = "fixture answer",
    input_tokens: int = 10,
    cached_input_tokens: int = 0,
    output_tokens: int = 2,
    commands: list[dict[str, Any]] | None = None,
) -> str:
    """Build one official-shape completed Codex event stream.

    Example:
        >>> events = [json.loads(line) for line in _completed_stream(output="example", input_tokens=0).splitlines()]
        >>> [event["type"] for event in events]
        ['thread.started', 'item.completed', 'turn.completed']
        >>> events[1]["item"]["text"], events[-1]["usage"]["input_tokens"]
        ('example', 0)
    """
    events: list[dict[str, Any]] = [{"type": "thread.started", "thread_id": "fixture-thread"}]
    events.extend(
        {"type": "item.completed", "item": {"id": f"command-{index}", **command}}
        for index, command in enumerate(commands or [], start=1)
    )
    events.extend(
        [
            {
                "type": "item.completed",
                "item": {"id": "answer", "type": "agent_message", "text": output},
            },
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": input_tokens,
                    "cached_input_tokens": cached_input_tokens,
                    "output_tokens": output_tokens,
                    "reasoning_output_tokens": 0,
                },
            },
        ]
    )
    return "\n".join(json.dumps(event) for event in events)


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        pytest.param("$CODEMAP_BIN query --compact rdeps pkg.core", True, id="unquoted-query"),
        pytest.param('"$CODEMAP_BIN" query --compact rdeps pkg.core', True, id="quoted-query"),
        pytest.param("echo $CODEMAP_BIN", False, id="echo-inspection"),
        pytest.param("env | rg CODEMAP_BIN", False, id="environment-inspection"),
        pytest.param('"$CODEMAP_BIN" --help', False, id="launcher-inspection"),
        pytest.param("$CODEMAP_BIN query --compact rdeps pkg.core &", False, id="historical-background"),
        pytest.param("'$CODEMAP_BIN' query --compact rdeps pkg.core", False, id="single-quoted-variable"),
        pytest.param("'${CODEMAP_BIN}' query --compact rdeps pkg.core", False, id="single-quoted-braced-variable"),
        pytest.param("$CODEMAP_BIN query --compact rdeps --help", False, id="subcommand-help"),
        pytest.param("$CODEMAP_BIN query --compact rdeps pkg.core\nwait", False, id="historical-newline-wait"),
        pytest.param("`$CODEMAP_BIN query --compact rdeps pkg.core`", False, id="backticks"),
        pytest.param('$("$CODEMAP_BIN" query --compact rdeps pkg.core)', False, id="historical-substitution"),
        pytest.param('("$CODEMAP_BIN" query --compact rdeps pkg.core)', False, id="historical-subshell-group"),
        pytest.param('{ "$CODEMAP_BIN" query --compact rdeps pkg.core; }', False, id="brace-group"),
        pytest.param('"$CODEMAP_BIN" query --compact rdeps pkg.core > out.json', False, id="historical-redirect"),
        pytest.param('"$CODEMAP_BIN" query --compact rdeps pkg.core 2>&1', False, id="historical-stderr-redirect"),
    ],
)
def test_historical_shell_query_shapes_reject_the_native_item_contract(
    script_run_codex: Any, command: str, expected: bool
) -> None:
    """Standalone native commands remain evidence across quoted launcher spellings."""
    assert script_run_codex.runtime._is_codemap_command(command) is expected


def test_required_compliance_needs_successful_compact_delivery_by_arm(script_run_codex: Any, tmp_path: Path) -> None:
    """A query attempt, wrong delivery mode, or missing compact flag cannot comply."""
    task = {"id": "fixture", "prompt": "unchanged prompt", "type": "demo", "scoreable": True}

    def _run(arm: str, command: str) -> Any:
        """Evaluate one arm against synthetic completed query events."""
        runner = script_run_codex.CodexRunner(
            "fixture-model",
            tmp_path,
            transport=lambda *_args, **_kwargs: _completed_stream(
                commands=[
                    {
                        "type": "command_execution",
                        "command": command,
                        "status": "completed",
                        "exit_code": 0,
                        "aggregated_output": '{"index":{"query_complete":true,"compact":true}}',
                    }
                ]
            ),
            evaluator=lambda *_args: core.EvaluationResult(scored=True, correct=True, quality_score=1.0),
        )
        return runner.run(task, arm)

    direct = _run("B_auto", '"$CODEMAP_BIN" query --compact rdeps pkg.core')
    noncompact = _run("B_auto", '"$CODEMAP_BIN" query rdeps pkg.core')
    skill = _run("C_strict", '"$CODEMAP_BIN" query --compact rdeps pkg.core')

    assert direct.compliance is True
    assert direct.codemap_direct_successful_calls == 1
    assert direct.codemap_skill_successful_calls == 0
    assert noncompact.compliance is False
    assert noncompact.codemap_delivery == "none"
    assert skill.compliance is False
    assert skill.codemap_direct_successful_calls == 1
    assert skill.codemap_delivery == "none"


@POSIX_SECURITY
def test_direct_cli_arm_never_installs_a_plugin(script_run_codex: Any, tmp_path: Path) -> None:
    """B must expose the supplied launcher without using Codex plugin setup."""
    launcher = _make_direct_runtime_bundle(tmp_path)
    installer_calls: list[Path] = []

    with script_run_codex.prepare_arm_home(
        "B_auto",
        root=tmp_path,
        codemap_bin=launcher,
        plugin_installer=lambda home: installer_calls.append(home) or True,
    ) as home:
        assert home.codemap_available is True
        assert home.codemap_verified is True
        staged_launcher = Path(home.env["CODEMAP_BIN"])
        assert staged_launcher == home.path / "direct-cli" / "bin" / "codemap-py"
        assert staged_launcher.read_bytes() == launcher.read_bytes()
        assert (home.path / "direct-cli" / "bin" / "_exclusions.py").is_file()
        assert (home.path / "direct-cli" / "scripts" / "codemap_py_entry.py").is_file()
        assert (home.path / "direct-cli" / "src" / "codemap_py" / "__init__.py").is_file()
        assert not (home.path / "direct-cli" / ".codex-plugin").exists()
        assert not (home.path / "direct-cli" / "codex-skills").exists()
        assert not (home.path / "direct-cli" / "shared").exists()

    assert installer_calls == []


@POSIX_SECURITY
def test_staged_direct_cli_admission_executes_a_task_shaped_query(script_run_codex: Any, tmp_path: Path) -> None:
    """B preflight must execute its staged CLI before any model can consume a cell."""
    repo_path = tmp_path / "target"
    repo_path.mkdir()
    index_path = repo_path / "locked-index.json"
    index_path.write_text("{}", encoding="utf-8")
    home_path = tmp_path / "home"
    launcher = home_path / "direct-cli" / "bin" / "codemap-py"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o755)
    config = home_path / "config.toml"
    config.write_text("", encoding="utf-8")
    config.chmod(0o600)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "direct_cli_admission": {
                    "probe_subcommand": "fn-rdeps",
                    "probe_target": "lightning.pytorch.trainer.call::_call_lightning_module_hook",
                }
            }
        ),
        encoding="utf-8",
    )
    home = script_run_codex.ArmHome(
        "B_auto",
        home_path,
        {
            "PATH": "/fixture/bin",
            "CODEMAP_BIN": str(launcher),
            "CODEMAP_PYTHON": "/usr/bin/python3",
            "SCAN_NO_AUTOBUILD": "1",
        },
        True,
        True,
        permission_profile="provider-parity-codemap",
        codemap_launcher_path=launcher,
    )
    calls: list[list[str]] = []

    def _command_runner(command: list[str], **_kwargs: Any) -> SimpleNamespace:
        """Record query argv and return successful compact-query evidence."""
        calls.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"index": {"query_complete": True, "compact": True}}),
            stderr="",
        )

    script_run_codex._admit_staged_direct_cli(
        home,
        repo_path,
        index_path,
        manifest_path=manifest_path,
        command_runner=_command_runner,
    )

    assert calls == [
        [
            "codex",
            "sandbox",
            "-P",
            "provider-parity-codemap",
            "--include-managed-config",
            "-C",
            str(repo_path),
            "--",
            str(launcher),
            "query",
            "--compact",
            "fn-rdeps",
            "lightning.pytorch.trainer.call::_call_lightning_module_hook",
        ]
    ]


@POSIX_SECURITY
def test_no_model_probe_removes_its_coordination_root(
    script_run_codex: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dry probe must not leave the index reader-coordination skeleton behind."""
    home_path = tmp_path / "home"
    home_path.mkdir()
    home_path.chmod(0o700)
    config = home_path / "config.toml"
    config.write_text("", encoding="utf-8")
    config.chmod(0o600)
    index_path = tmp_path / ".cache" / "codemap" / "fixture.json"
    index_path.parent.mkdir(parents=True)
    index_path.write_text("{}", encoding="utf-8")
    coordination_root = script_run_codex._prepare_coordination_root(index_path)
    home = script_run_codex.ArmHome(
        "B_auto",
        home_path,
        {},
        codemap_available=True,
        codemap_verified=True,
        coordination_path=coordination_root,
    )
    runner = script_run_codex.CodexRunner("fixture-model", tmp_path)
    monkeypatch.setattr(runner, "_prepare_verified_home", lambda _arm: home)

    runner.probe_arm("B_auto")

    assert not coordination_root.exists()
    assert not home_path.exists()


@POSIX_SECURITY
def test_direct_cli_launcher_must_match_its_manifest_hash(script_run_codex: Any, tmp_path: Path) -> None:
    """B rejects a direct executable unless its bytes are the locked runtime launcher."""
    launcher = _make_direct_runtime_bundle(tmp_path)
    lock_path = tmp_path / "locks.json"

    with script_run_codex.prepare_arm_home("B_auto", root=tmp_path, codemap_bin=launcher) as home:
        lock_path.write_text(
            json.dumps(
                {
                    "artifact_sha256": {"codemap_runtime_cli": home.codemap_launcher_sha256},
                    "codemap_candidate": {"version": "0.27.0"},
                    "direct_cli_runtime": {
                        "files": script_run_codex._runtime_file_hashes(home.codemap_launcher_path.parent.parent),
                        "aggregate_sha256": script_run_codex._aggregate_file_hashes(
                            script_run_codex._runtime_file_hashes(home.codemap_launcher_path.parent.parent)
                        ),
                    },
                }
            ),
            encoding="utf-8",
        )
        script_run_codex._verify_treatment_artifact_locks(home, lock_path)

        lock_path.write_text(
            json.dumps(
                {"artifact_sha256": {"codemap_runtime_cli": "0" * 64}, "codemap_candidate": {"version": "0.27.0"}}
            ),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="launcher does not match"):
            script_run_codex._verify_treatment_artifact_locks(home, lock_path)


def test_treatment_artifact_lock_mismatch_explains_safe_refresh(script_run_codex: Any) -> None:
    """A stale local treatment lock names the no-model recovery sequence."""
    message = script_run_codex._treatment_artifact_lock_mismatch_message("codex_rig_adapter")

    assert "codex_rig_adapter" in message
    assert "no paid model call was started" in message
    assert "uv run python benchmarks/build-codex-integration-manifest.py" in message
    assert "Do not reuse the previous --paid-approval value" in message


def test_historical_exact_launcher_and_compound_forms_reject_native_item_contract(
    script_run_codex: Any, tmp_path: Path
) -> None:
    """The prospective contract does not infer delivery from paths or shell composition."""
    launcher = tmp_path / "codemap-py"
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o755)

    assert script_run_codex.runtime._is_codemap_command("$CODEMAP_BIN query --compact rdeps pkg.core")
    assert not script_run_codex.runtime._is_codemap_command(
        f'"{launcher}" query --compact rdeps pkg.core', launcher_path=launcher
    )
    assert not script_run_codex.runtime._is_codemap_command("/plugin/bin/codemap-py query --compact rdeps pkg.core")
    assert not script_run_codex.runtime._is_codemap_command("$CODEMAP_BIN query --compact rdeps pkg.core; echo done")


def test_verified_launcher_is_observed_and_requires_opt_in_for_credit(script_run_codex: Any, tmp_path: Path) -> None:
    """A verified literal launcher is observable without rewriting historical credit."""
    launcher = tmp_path / "runtime" / "bin" / "codemap-py"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o755)
    command = f"/bin/zsh -lc '\"{launcher}\" query --compact rdeps pkg.core'"
    stream = _completed_stream(
        commands=[
            {
                "type": "command_execution",
                "command": command,
                "status": "completed",
                "exit_code": 0,
                "aggregated_output": '{"index":{"query_complete":true,"compact":true}}',
            }
        ]
    )

    replay = script_run_codex.runtime.parse_codex_jsonl(stream, launcher_path=launcher)
    credited = script_run_codex.runtime.parse_codex_jsonl(
        stream,
        launcher_path=launcher,
        allow_equivalent_launcher=True,
    )

    assert replay.codemap_observed_calls == 1
    assert replay.codemap_observed_successful_calls == 1
    assert replay.codemap_observed_complete_calls == 1
    assert replay.codemap_calls == 0
    assert credited.codemap_calls == 1
    assert credited.codemap_compact_successful_calls == 1


@pytest.mark.parametrize(
    "command_template",
    [
        pytest.param('"{launcher}" --help', id="help-only"),
        pytest.param('echo "{launcher}" query --compact rdeps pkg.core', id="echo-only"),
        pytest.param('"{other}" query --compact rdeps pkg.core', id="same-basename-wrong-path"),
    ],
)
def test_verified_launcher_observation_rejects_non_dedicated_or_wrong_commands(
    script_run_codex: Any, tmp_path: Path, command_template: str
) -> None:
    """Only a standalone query through the verified absolute path is observed."""
    launcher = tmp_path / "runtime" / "bin" / "codemap-py"
    other = tmp_path / "other" / "bin" / "codemap-py"
    for path in (launcher, other):
        path.parent.mkdir(parents=True)
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)

    command = command_template.format(launcher=launcher, other=other)

    assert not script_run_codex.runtime._observes_compact_codemap_query(command, launcher_path=launcher)
    assert not script_run_codex.runtime._is_codemap_command(
        command,
        launcher_path=launcher,
        allow_equivalent_launcher=True,
    )


@pytest.mark.parametrize(
    ("status", "exit_code", "output", "successful", "complete"),
    [
        pytest.param("failed", 1, '{"error":"failed"}', 0, 0, id="failed"),
        pytest.param("completed", 0, "{}", 0, 0, id="incomplete-json"),
        pytest.param(
            "completed",
            0,
            '{"index":{"query_complete":true,"compact":true,"truncated":true}}',
            0,
            0,
            id="truncated-json",
        ),
    ],
)
def test_observed_launcher_success_and_completion_are_distinct(
    script_run_codex: Any,
    tmp_path: Path,
    status: str,
    exit_code: int,
    output: str,
    successful: int,
    complete: int,
) -> None:
    """A completed process and a complete query result are independent evidence."""
    launcher = tmp_path / "runtime" / "bin" / "codemap-py"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o755)
    stream = _completed_stream(
        commands=[
            {
                "type": "command_execution",
                "command": f"/bin/zsh -lc '\"{launcher}\" query --compact rdeps pkg.core'",
                "status": status,
                "exit_code": exit_code,
                "aggregated_output": output,
            }
        ]
    )

    parsed = script_run_codex.runtime.parse_codex_jsonl(stream, launcher_path=launcher)

    assert parsed.codemap_observed_calls == 1
    assert parsed.codemap_observed_successful_calls == successful
    assert parsed.codemap_observed_complete_calls == complete
    assert parsed.codemap_calls == 0


@pytest.mark.parametrize(
    "command",
    [
        pytest.param("'$CODEMAP_BIN' query --compact rdeps pkg.core", id="single-quoted-variable"),
        pytest.param("'${CODEMAP_BIN}' query --compact rdeps pkg.core", id="single-quoted-braced-variable"),
        pytest.param("echo ';' \"$CODEMAP_BIN\" query --compact rdeps pkg.core", id="quoted-semicolon-data"),
        pytest.param(
            'CODEMAP_BIN=/wrong/codemap-py; "$CODEMAP_BIN" query --compact rdeps pkg.core',
            id="reassigned-variable",
        ),
        pytest.param('"$CODEMAP_BIN" query --compact rdeps --help', id="subcommand-help"),
    ],
)
def test_observation_rejects_unexpanded_or_mutated_launcher_commands(script_run_codex: Any, command: str) -> None:
    """Observation requires an executable launcher token, not shell text or help."""
    stream = _completed_stream(
        commands=[
            {
                "type": "command_execution",
                "command": command,
                "status": "completed",
                "exit_code": 0,
                "aggregated_output": '{"index":{"query_complete":true,"compact":true}}',
            }
        ]
    )

    parsed = script_run_codex.runtime.parse_codex_jsonl(stream)

    assert parsed.codemap_observed_calls == 0
    assert parsed.codemap_observed_successful_calls == 0
    assert parsed.codemap_observed_complete_calls == 0


def test_verified_launcher_observation_survives_an_unrelated_variable_reassignment(
    script_run_codex: Any, tmp_path: Path
) -> None:
    """An exact proven executable remains observable when a separate variable is changed."""
    launcher = tmp_path / "runtime" / "bin" / "codemap-py"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o755)
    stream = _completed_stream(
        commands=[
            {
                "type": "command_execution",
                "command": f'CODEMAP_BIN=/wrong/codemap-py; "{launcher}" query --compact rdeps pkg.core',
                "status": "completed",
                "exit_code": 0,
                "aggregated_output": '{"index":{"query_complete":true,"compact":true}}',
            }
        ]
    )

    parsed = script_run_codex.runtime.parse_codex_jsonl(stream, launcher_path=launcher)

    assert parsed.codemap_observed_calls == 1
    assert parsed.codemap_observed_successful_calls == 1
    assert parsed.codemap_calls == 0


def test_variable_launcher_observation_survives_a_later_reassignment(script_run_codex: Any) -> None:
    """A later assignment cannot retroactively change an earlier query executable."""
    stream = _completed_stream(
        commands=[
            {
                "type": "command_execution",
                "command": '"$CODEMAP_BIN" query --compact rdeps pkg.core; CODEMAP_BIN=/wrong/codemap-py',
                "status": "completed",
                "exit_code": 0,
                "aggregated_output": '{"index":{"query_complete":true,"compact":true}}',
            }
        ]
    )

    parsed = script_run_codex.runtime.parse_codex_jsonl(stream)

    assert parsed.codemap_observed_calls == 1
    assert parsed.codemap_observed_successful_calls == 1
    assert parsed.codemap_calls == 0


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        pytest.param(
            "/bin/zsh -lc '\"$CODEMAP_BIN\" query --compact rdeps pkg.core'", True, id="one-outer-transport-wrapper"
        ),
        pytest.param(
            '/bin/zsh -lc \'/bin/zsh -lc "\\"$CODEMAP_BIN\\" query --compact rdeps pkg.core"\'',
            False,
            id="nested-wrapper",
        ),
        pytest.param(
            "/bin/zsh -lc '\"$CODEMAP_BIN\" query --compact rdeps pkg.core; echo done'",
            False,
            id="historical-compound-wrapper",
        ),
        pytest.param(
            "/bin/zsh -lc '\"$CODEMAP_BIN\" query --compact rdeps pkg.core > out.json'",
            False,
            id="historical-redirect-wrapper",
        ),
        pytest.param("/bin/zsh -lc", False, id="missing-wrapper-command"),
    ],
)
def test_one_outer_transport_wrapper_preserves_the_native_item_contract(
    script_run_codex: Any, command: str, expected: bool
) -> None:
    """Only one exact Codex transport wrapper may contain the native payload."""
    assert script_run_codex.runtime._is_codemap_command(command) is expected


def test_historical_wrapped_C_delivery_rejects_native_item_contract(script_run_codex: Any, tmp_path: Path) -> None:
    """Historical wrapped C evidence stays available but cannot score a new cell."""
    skill_path = tmp_path / "codex-skills" / "query-code" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_bytes = b"# query-code\n"
    skill_path.write_bytes(skill_bytes)
    launcher = tmp_path / "plugins" / "codemap-py" / "bin" / "codemap-py"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o755)
    complete_result = json.dumps({"index": {"query_complete": True}})
    events = [
        {
            "type": "item.completed",
            "item": {
                "id": "skill-read",
                "type": "command_execution",
                "command": "/bin/zsh -lc 'cat \"$CODEMAP_SKILL_FILE\"'",
                "status": "completed",
                "exit_code": 0,
                "aggregated_output": skill_bytes.decode(),
            },
        },
        {
            "type": "item.completed",
            "item": {
                "id": "query",
                "type": "command_execution",
                "command": f"/bin/zsh -lc '\"{launcher}\" query --compact rdeps pkg.core'",
                "status": "completed",
                "exit_code": 0,
                "aggregated_output": complete_result,
            },
        },
        {"type": "turn.completed"},
    ]

    parsed = script_run_codex.runtime.parse_codex_jsonl(
        "\n".join(json.dumps(event) for event in events),
        launcher_path=launcher,
        skill_path=skill_path,
        skill_sha256=hashlib.sha256(skill_bytes).hexdigest(),
    )

    assert parsed.skill_delivery_observed is True
    assert parsed.codemap_calls == 0
    assert script_run_codex._arm_compliance("C_strict", parsed) is False

    direct_after_skill = [
        events[0],
        {
            "type": "item.completed",
            "item": {
                "id": "direct-query",
                "type": "command_execution",
                "command": "/bin/zsh -lc '\"$CODEMAP_BIN\" query --compact rdeps pkg.core'",
                "status": "completed",
                "exit_code": 0,
                "aggregated_output": complete_result,
            },
        },
        {"type": "turn.completed"},
    ]
    direct_parsed = script_run_codex.runtime.parse_codex_jsonl(
        "\n".join(json.dumps(event) for event in direct_after_skill),
        launcher_path=launcher,
        skill_path=skill_path,
        skill_sha256=hashlib.sha256(skill_bytes).hexdigest(),
    )

    assert direct_parsed.skill_delivery_observed is True
    assert direct_parsed.codemap_calls == 1
    assert script_run_codex._arm_compliance("C_strict", direct_parsed) is False


def test_compound_compact_query_is_observed_without_receiving_canonical_credit(
    script_run_codex: Any, tmp_path: Path
) -> None:
    """A parsed query segment records use without satisfying standalone credit."""
    skill_path = tmp_path / "codex-skills" / "query-code" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_bytes = b"# query-code\n"
    skill_path.write_bytes(skill_bytes)
    events = _completed_stream(
        commands=[
            {
                "type": "command_execution",
                "command": 'printf "%s\\n" "$CODEMAP_BIN"; "$CODEMAP_BIN" query --compact symbol pkg.Item',
                "status": "completed",
                "exit_code": 0,
                "aggregated_output": '{"index":{"query_complete":true,"compact":true}}',
            }
        ]
    )

    parsed = script_run_codex.runtime.parse_codex_jsonl(
        events,
        skill_path=skill_path,
        skill_sha256=hashlib.sha256(skill_bytes).hexdigest(),
    )

    assert parsed.codemap_observed_calls == 1
    assert parsed.codemap_observed_successful_calls == 1
    assert parsed.codemap_observed_complete_calls == 1
    assert parsed.codemap_calls == 0
    assert parsed.codemap_skill_compact_successful_calls == 0
    assert script_run_codex._arm_compliance("C_strict", parsed) is False


def test_installed_skill_binding_credits_compact_query_without_manual_skill_read(
    script_run_codex: Any, tmp_path: Path
) -> None:
    """A C treatment is productive when its installed Skill binding guides one exact compact query.

    Manual ``cat "$CODEMAP_SKILL_FILE"`` reads are audit diagnostics, not a prerequisite for counting a query made
    through the immutable installed-Skill treatment. Requiring that extra read would measure benchmark ceremony rather
    than normal integration use.
    """
    skill_path = tmp_path / "codex-skills" / "query-code" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_bytes = b"# query-code\nUse compact queries.\n"
    skill_path.write_bytes(skill_bytes)
    events = _completed_stream(
        commands=[
            {
                "type": "command_execution",
                "command": '"$CODEMAP_BIN" query --compact rdeps pkg.core',
                "status": "completed",
                "exit_code": 0,
                "aggregated_output": '{"index":{"query_complete":true,"compact":true}}',
            }
        ]
    )

    parsed = script_run_codex.runtime.parse_codex_jsonl(
        events,
        skill_path=skill_path,
        skill_sha256=hashlib.sha256(skill_bytes).hexdigest(),
    )

    assert parsed.skill_delivery_observed is False
    assert parsed.codemap_calls == 1
    assert parsed.codemap_skill_compact_successful_calls == 1
    assert script_run_codex._arm_compliance("C_strict", parsed) is True

    incomplete = script_run_codex.runtime.parse_codex_jsonl(
        _completed_stream(
            commands=[
                {
                    "type": "command_execution",
                    "command": "$CODEMAP_BIN query --compact rdeps pkg.core",
                    "status": "completed",
                    "exit_code": 0,
                    "aggregated_output": "{}",
                }
            ]
        )
    )
    assert incomplete.codemap_calls == 1
    assert script_run_codex._arm_compliance("B_auto", incomplete) is False


def test_historical_bound_launcher_query_rejects_native_item_contract(script_run_codex: Any, tmp_path: Path) -> None:
    """The paid local-alias shape remains historical-only evidence."""
    skill_path = tmp_path / "codex-skills" / "query-code" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_bytes = b"# query-code\nUse the compact query.\n"
    skill_path.write_bytes(skill_bytes)
    launcher = tmp_path / "plugins" / "codemap-py" / "bin" / "codemap-py"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o755)
    events = [
        {
            "type": "item.completed",
            "item": {
                "id": "skill-read",
                "type": "command_execution",
                "command": "/bin/zsh -lc 'cat \"$CODEMAP_SKILL_FILE\"'",
                "status": "completed",
                "exit_code": 0,
                "aggregated_output": skill_bytes.decode(),
            },
        },
        {
            "type": "item.completed",
            "item": {
                "id": "query",
                "type": "command_execution",
                "command": (
                    "/bin/zsh -lc '"
                    f'codemap_bin="${{CODEMAP_BIN:-{launcher}}}"; '
                    '"$codemap_bin" query --compact fn-rdeps "pkg.core::target"\''
                ),
                "status": "completed",
                "exit_code": 0,
                "aggregated_output": json.dumps({"index": {"query_complete": True, "compact": True}}),
            },
        },
        {"type": "turn.completed"},
    ]

    parsed = script_run_codex.runtime.parse_codex_jsonl(
        "\n".join(json.dumps(event) for event in events),
        launcher_path=launcher,
        skill_path=skill_path,
        skill_sha256=hashlib.sha256(skill_bytes).hexdigest(),
    )

    assert parsed.skill_delivery_observed is True
    assert parsed.codemap_calls == 0
    assert parsed.codemap_direct_successful_calls == 0
    assert script_run_codex._arm_compliance("C_strict", parsed) is False

    reversed_parsed = script_run_codex.runtime.parse_codex_jsonl(
        "\n".join(json.dumps(event) for event in [events[1], events[0], events[2]]),
        launcher_path=launcher,
        skill_path=skill_path,
        skill_sha256=hashlib.sha256(skill_bytes).hexdigest(),
    )
    # The exact Skill read is still observable, but it cannot make the preceding
    # non-canonical query compliant.
    assert reversed_parsed.skill_delivery_observed is True
    assert reversed_parsed.codemap_skill_compact_successful_calls == 0
    assert script_run_codex._arm_compliance("C_strict", reversed_parsed) is False


def test_historical_uppercase_launcher_assignment_rejects_native_item_contract(
    script_run_codex: Any, tmp_path: Path
) -> None:
    """A historical assigned launcher is not a future standalone native item."""
    skill_path = tmp_path / "codex-skills" / "query-code" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_bytes = b"# query-code\nUse the compact query.\n"
    skill_path.write_bytes(skill_bytes)
    launcher = tmp_path / "plugins" / "codemap-py" / "bin" / "codemap-py"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o755)
    events = [
        {
            "type": "item.completed",
            "item": {
                "id": "skill-read",
                "type": "command_execution",
                "command": 'cat "$CODEMAP_SKILL_FILE"',
                "status": "completed",
                "exit_code": 0,
                "aggregated_output": skill_bytes.decode(),
            },
        },
        {
            "type": "item.completed",
            "item": {
                "id": "query",
                "type": "command_execution",
                "command": (
                    "/bin/zsh -lc '"
                    f'CODEMAP_BIN="${{CODEMAP_BIN:-{launcher}}}"; '
                    '"$CODEMAP_BIN" query --compact fn-rdeps "pkg.core::target"\''
                ),
                "status": "completed",
                "exit_code": 0,
                "aggregated_output": json.dumps({"index": {"query_complete": True, "compact": True}}),
            },
        },
        {"type": "turn.completed"},
    ]

    parsed = script_run_codex.runtime.parse_codex_jsonl(
        "\n".join(json.dumps(event) for event in events),
        launcher_path=launcher,
        skill_path=skill_path,
        skill_sha256=hashlib.sha256(skill_bytes).hexdigest(),
    )

    assert parsed.skill_delivery_observed is True
    assert parsed.codemap_calls == 0
    assert parsed.codemap_direct_successful_calls == 0
    assert script_run_codex._arm_compliance("C_strict", parsed) is False


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(
            'CODEMAP_BIN="${CODEMAP_BIN:-/wrong/codemap-py}"; "$CODEMAP_BIN" query --compact rdeps pkg.core',
            id="wrong-fallback",
        ),
        pytest.param(
            'CODEMAP_BIN="{launcher}"; "$CODEMAP_BIN" query --compact rdeps pkg.core',
            id="literal-assignment",
        ),
        pytest.param(
            'CODEMAP_BIN="${CODEMAP_BIN:-{launcher}}"; CODEMAP_BIN=/wrong/codemap-py; '
            '"$CODEMAP_BIN" query --compact rdeps pkg.core',
            id="reassigned",
        ),
        pytest.param(
            'export CODEMAP_BIN="${CODEMAP_BIN:-{launcher}}"; "$CODEMAP_BIN" query --compact rdeps pkg.core',
            id="exported",
        ),
        pytest.param(
            'readonly CODEMAP_BIN="${CODEMAP_BIN:-{launcher}}"; "$CODEMAP_BIN" query --compact rdeps pkg.core',
            id="readonly",
        ),
        pytest.param(
            'typeset CODEMAP_BIN="${CODEMAP_BIN:-{launcher}}"; "$CODEMAP_BIN" query --compact rdeps pkg.core',
            id="typeset",
        ),
        pytest.param(
            'CODEMAP_BIN="${CODEMAP_BIN:-{launcher}}"; unset CODEMAP_BIN; '
            '"$CODEMAP_BIN" query --compact rdeps pkg.core',
            id="unset",
        ),
        pytest.param(
            'CODEMAP_BIN="$(printf \'%s\' {launcher})"; "$CODEMAP_BIN" query --compact rdeps pkg.core',
            id="command-substitution",
        ),
        pytest.param(
            'payload="CODEMAP_BIN=/wrong/codemap-py"; eval "$payload"; "$CODEMAP_BIN" query --compact rdeps pkg.core',
            id="dynamic-eval",
        ),
        pytest.param(
            'if true; then CODEMAP_BIN="${CODEMAP_BIN:-{launcher}}"; fi; "$CODEMAP_BIN" query --compact rdeps pkg.core',
            id="control-flow-binding",
        ),
    ],
)
def test_uppercase_launcher_fallback_rejects_untrusted_shell_forms(
    script_run_codex: Any, tmp_path: Path, command: str
) -> None:
    """Assignments never substitute for the future standalone native item."""
    launcher = tmp_path / "codemap-py"
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o755)

    assert not script_run_codex.runtime._is_codemap_command(
        command.replace("{launcher}", str(launcher)), launcher_path=launcher
    )


def test_historical_compound_direct_query_rejects_native_item_contract(script_run_codex: Any) -> None:
    """A diagnostic/query compound is historical evidence, not a future query item."""
    output = "ready\n" + json.dumps({"index": {"query_complete": True, "compact": True}}) + "\ndone\n"
    parsed = script_run_codex.runtime.parse_codex_jsonl(
        _completed_stream(
            commands=[
                {
                    "type": "command_execution",
                    "command": ('printf "ready\\n"; "$CODEMAP_BIN" query --compact rdeps pkg.core; printf "done\\n"'),
                    "status": "completed",
                    "exit_code": 0,
                    "aggregated_output": output,
                }
            ]
        )
    )

    assert parsed.codemap_direct_compact_successful_calls == 0
    assert script_run_codex._arm_compliance("B_auto", parsed) is False


def test_historical_multiline_direct_query_rejects_native_item_contract(script_run_codex: Any) -> None:
    """The exact paid B wrapper cannot satisfy prospective native telemetry."""
    command = (
        '/bin/zsh -lc "printf \'CODEMAP_BIN=%s\\\\n\' \\""\'$CODEMAP_BIN"\n'
        '"$CODEMAP_BIN" query --compact fn-rdeps "pkg.core::target"\''
    )
    parsed = script_run_codex.runtime.parse_codex_jsonl(
        _completed_stream(
            commands=[
                {
                    "type": "command_execution",
                    "command": command,
                    "status": "completed",
                    "exit_code": 0,
                    "aggregated_output": (
                        "CODEMAP_BIN=/fixture/codemap-py\n"
                        + json.dumps({"index": {"query_complete": True, "compact": True}})
                    ),
                }
            ]
        )
    )

    assert parsed.codemap_direct_compact_successful_calls == 0
    assert script_run_codex._arm_compliance("B_auto", parsed) is False


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(
            'printf \'CODEMAP_BIN=%s\\n\' "$CODEMAP_BIN"; "$CODEMAP_BIN" query --compact rdeps pkg.core',
            id="historical-semicolon",
        ),
        pytest.param(
            'printf ready &&\n"$CODEMAP_BIN" query --compact rdeps pkg.core',
            id="historical-and-newline",
        ),
        pytest.param(
            'false ||\n"$CODEMAP_BIN" query --compact rdeps pkg.core',
            id="historical-or-newline",
        ),
        pytest.param(
            'printf ready |\n"$CODEMAP_BIN" query --compact rdeps pkg.core',
            id="historical-pipe-newline",
        ),
        pytest.param(
            'CODEMAP_BIN=/wrong/codemap-py\n"$CODEMAP_BIN" query --compact rdeps pkg.core',
            id="newline-before-mutated-launcher",
        ),
        pytest.param(
            'printf \'CODEMAP_BIN=/wrong\\n\' "$CODEMAP_BIN" "$CODEMAP_BIN" query --compact rdeps pkg.core',
            id="diagnostic-data-without-separator",
        ),
        pytest.param(
            'printf \'CODEMAP_BIN=%s\\n\n"$CODEMAP_BIN" query --compact rdeps pkg.core\' "$CODEMAP_BIN"',
            id="quoted-literal-newline",
        ),
        pytest.param(
            'printf \'CODEMAP_BIN=%s\\n\' "$CODEMAP_BIN" \\\n"$CODEMAP_BIN" query --compact rdeps pkg.core',
            id="escaped-line-continuation",
        ),
    ],
)
def test_historical_newline_shell_forms_reject_native_item_contract(script_run_codex: Any, command: str) -> None:
    """No multiline shell form is the dedicated future native query item."""
    parsed = script_run_codex.runtime.parse_codex_jsonl(
        _completed_stream(
            commands=[
                {
                    "type": "command_execution",
                    "command": command,
                    "status": "completed",
                    "exit_code": 0,
                    "aggregated_output": json.dumps({"index": {"query_complete": True, "compact": True}}),
                }
            ]
        )
    )

    assert parsed.codemap_direct_compact_successful_calls == 0
    assert script_run_codex._arm_compliance("B_auto", parsed) is False


def test_historical_diagnostic_conditional_query_rejects_native_item_contract(script_run_codex: Any) -> None:
    """A historical diagnostic/control command cannot score a future direct query."""
    command = (
        "printf 'CODEMAP_BIN=%s\\n' \"${CODEMAP_BIN-}\"; "
        "rg -n target .; "
        'if [ -n "${CODEMAP_BIN-}" ]; then '
        '"$CODEMAP_BIN" query --compact fn-rdeps "pkg.core::target"; '
        "else printf 'CODEMAP_BIN is unset\\n'; fi"
    )
    parsed = script_run_codex.runtime.parse_codex_jsonl(
        _completed_stream(
            commands=[
                {
                    "type": "command_execution",
                    "command": command,
                    "status": "completed",
                    "exit_code": 0,
                    "aggregated_output": json.dumps({"index": {"query_complete": True, "compact": True}}),
                }
            ]
        )
    )

    assert parsed.codemap_direct_compact_successful_calls == 0
    assert script_run_codex._arm_compliance("B_auto", parsed) is False


def test_historical_bound_launcher_diagnostic_rejects_native_item_contract(
    script_run_codex: Any, tmp_path: Path
) -> None:
    """A local launcher binding is not a standalone native query command."""
    launcher = tmp_path / "codemap-py"
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o755)
    command = (
        "printf 'codemap_bin=%s\\n' \"${CODEMAP_BIN-}\"; "
        f'codemap_bin="${{CODEMAP_BIN:-{launcher}}}"; '
        '"$codemap_bin" query --compact rdeps pkg.core'
    )

    assert not script_run_codex.runtime._is_codemap_command(command, launcher_path=launcher)


def test_historical_compound_skill_and_control_query_reject_native_item_contract(
    script_run_codex: Any, tmp_path: Path
) -> None:
    """C now requires two dedicated native items rather than compound shell evidence."""
    skill_path = tmp_path / "query-code" / "SKILL.md"
    skill_path.parent.mkdir()
    skill_bytes = b"# query-code\nUse the compact query.\n"
    skill_path.write_bytes(skill_bytes)
    launcher = tmp_path / "codemap-py"
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o755)
    query_output = "diagnostic\n" + json.dumps({"index": {"query_complete": True, "compact": True}})
    events = [
        {
            "type": "item.completed",
            "item": {
                "id": "skill-read",
                "type": "command_execution",
                "command": f"sed -n '1,240p' {skill_path}; printf 'activated\\n'",
                "status": "completed",
                "exit_code": 0,
                "aggregated_output": skill_bytes.decode() + "activated\n",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "id": "query",
                "type": "command_execution",
                "command": (
                    'if [ -n "$CODEMAP_BIN" ]; then "$CODEMAP_BIN" query --compact '
                    'fn-rdeps "pkg.core::target"; else "'
                    f'{launcher}" query --compact fn-rdeps "pkg.core::target"; fi'
                ),
                "status": "completed",
                "exit_code": 0,
                "aggregated_output": query_output,
            },
        },
        {"type": "turn.completed"},
    ]

    parsed = script_run_codex.runtime.parse_codex_jsonl(
        "\n".join(json.dumps(event) for event in events),
        launcher_path=launcher,
        skill_path=skill_path,
        skill_sha256=hashlib.sha256(skill_bytes).hexdigest(),
    )

    assert parsed.skill_delivery_observed is False
    assert parsed.codemap_calls == 0
    assert parsed.codemap_direct_successful_calls == 0
    assert script_run_codex._arm_compliance("C_strict", parsed) is False


def test_historical_conditional_launcher_alias_replay_is_not_canonical_C_compliance(
    script_run_codex: Any, tmp_path: Path
) -> None:
    """Record the observed C command as historical replay incompatibility.

    The prospective C contract is the standalone ``$CODEMAP_BIN query`` form. This conditional alias remains corpus
    evidence for interpreting historical rows, not a second command grammar eligible for future compliance credit.
    """
    skill_path = tmp_path / "codex-skills" / "query-code" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_bytes = b"# query-code\nUse the compact query.\n"
    skill_path.write_bytes(skill_bytes)
    launcher = tmp_path / "plugins" / "codemap-py" / "bin" / "codemap-py"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o755)
    query_command = (
        '/bin/zsh -lc \'if [ -n "$CODEMAP_BIN" ]; then CODEMAP_LAUNCHER="$CODEMAP_BIN"; '
        f'else CODEMAP_LAUNCHER="{launcher}"; fi\n'
        '"$CODEMAP_LAUNCHER" query --compact fn-rdeps "pkg.core::target"\''
    )
    events = [
        {
            "type": "item.completed",
            "item": {
                "id": "skill-read",
                "type": "command_execution",
                "command": "/bin/zsh -lc 'cat \"$CODEMAP_SKILL_FILE\"'",
                "status": "completed",
                "exit_code": 0,
                "aggregated_output": skill_bytes.decode(),
            },
        },
        {
            "type": "item.completed",
            "item": {
                "id": "query",
                "type": "command_execution",
                "command": query_command,
                "status": "completed",
                "exit_code": 0,
                "aggregated_output": json.dumps({"index": {"query_complete": True, "compact": True}}),
            },
        },
        {"type": "turn.completed"},
    ]

    parsed = script_run_codex.runtime.parse_codex_jsonl(
        "\n".join(json.dumps(event) for event in events),
        launcher_path=launcher,
        skill_path=skill_path,
        skill_sha256=hashlib.sha256(skill_bytes).hexdigest(),
    )

    assert parsed.skill_delivery_observed is True
    assert parsed.codemap_calls == 0
    assert parsed.codemap_skill_compact_successful_calls == 0
    assert parsed.codemap_direct_successful_calls == 0
    assert script_run_codex._arm_compliance("C_strict", parsed) is False


@pytest.mark.parametrize(
    "template",
    [
        pytest.param(
            'if [ -n "$CODEMAP_BIN" ]; then CODEMAP_OTHER="$CODEMAP_BIN"; '
            'else CODEMAP_OTHER="{launcher}"; fi\n'
            '"$CODEMAP_OTHER" query --compact fn-rdeps "pkg.core::target"',
            id="wrong-alias-name",
        ),
        pytest.param(
            'if [ -n "$OTHER" ]; then CODEMAP_LAUNCHER="$CODEMAP_BIN"; '
            'else CODEMAP_LAUNCHER="{launcher}"; fi\n'
            '"$CODEMAP_LAUNCHER" query --compact fn-rdeps "pkg.core::target"',
            id="wrong-condition-variable",
        ),
        pytest.param(
            'if [ -z "$CODEMAP_BIN" ]; then CODEMAP_LAUNCHER="$CODEMAP_BIN"; '
            'else CODEMAP_LAUNCHER="{launcher}"; fi\n'
            '"$CODEMAP_LAUNCHER" query --compact fn-rdeps "pkg.core::target"',
            id="wrong-condition-operator",
        ),
        pytest.param(
            'if [ -n "$CODEMAP_BIN" ]; then CODEMAP_LAUNCHER="/wrong/codemap-py"; '
            'else CODEMAP_LAUNCHER="{launcher}"; fi\n'
            '"$CODEMAP_LAUNCHER" query --compact fn-rdeps "pkg.core::target"',
            id="wrong-then-source",
        ),
        pytest.param(
            'if [ -n "$CODEMAP_BIN" ]; then CODEMAP_LAUNCHER="$CODEMAP_BIN"; '
            'else CODEMAP_LAUNCHER="/wrong/codemap-py"; fi\n'
            '"$CODEMAP_LAUNCHER" query --compact fn-rdeps "pkg.core::target"',
            id="wrong-else-path",
        ),
        pytest.param(
            'if [ -n "$CODEMAP_BIN" ]; then CODEMAP_LAUNCHER="{launcher}"; '
            'else CODEMAP_LAUNCHER="$CODEMAP_BIN"; fi\n'
            '"$CODEMAP_LAUNCHER" query --compact fn-rdeps "pkg.core::target"',
            id="swapped-branches",
        ),
        pytest.param(
            'if [ -n "$CODEMAP_BIN" ]; then :; else CODEMAP_LAUNCHER="{launcher}"; fi\n'
            '"$CODEMAP_LAUNCHER" query --compact fn-rdeps "pkg.core::target"',
            id="missing-then-branch",
        ),
        pytest.param(
            'CODEMAP_BIN=/wrong/codemap-py; if [ -n "$CODEMAP_BIN" ]; '
            'then CODEMAP_LAUNCHER="$CODEMAP_BIN"; else CODEMAP_LAUNCHER="{launcher}"; fi\n'
            '"$CODEMAP_LAUNCHER" query --compact fn-rdeps "pkg.core::target"',
            id="precondition-codemap-bin-mutation",
        ),
        pytest.param(
            'if [ -n "$CODEMAP_BIN" ]; then CODEMAP_LAUNCHER="$CODEMAP_BIN"; '
            'else CODEMAP_LAUNCHER="{launcher}"; fi; CODEMAP_LAUNCHER=/wrong/codemap-py\n'
            '"$CODEMAP_LAUNCHER" query --compact fn-rdeps "pkg.core::target"',
            id="post-fi-alias-reassignment",
        ),
        pytest.param(
            'export CODEMAP_LAUNCHER=/wrong/codemap-py; if [ -n "$CODEMAP_BIN" ]; '
            'then CODEMAP_LAUNCHER="$CODEMAP_BIN"; else CODEMAP_LAUNCHER="{launcher}"; fi\n'
            '"$CODEMAP_LAUNCHER" query --compact fn-rdeps "pkg.core::target"',
            id="export-alias",
        ),
        pytest.param(
            'readonly CODEMAP_LAUNCHER=/wrong/codemap-py; if [ -n "$CODEMAP_BIN" ]; '
            'then CODEMAP_LAUNCHER="$CODEMAP_BIN"; else CODEMAP_LAUNCHER="{launcher}"; fi\n'
            '"$CODEMAP_LAUNCHER" query --compact fn-rdeps "pkg.core::target"',
            id="readonly-alias",
        ),
        pytest.param(
            'typeset CODEMAP_LAUNCHER=/wrong/codemap-py; if [ -n "$CODEMAP_BIN" ]; '
            'then CODEMAP_LAUNCHER="$CODEMAP_BIN"; else CODEMAP_LAUNCHER="{launcher}"; fi\n'
            '"$CODEMAP_LAUNCHER" query --compact fn-rdeps "pkg.core::target"',
            id="typeset-alias",
        ),
        pytest.param(
            'unset CODEMAP_LAUNCHER; if [ -n "$CODEMAP_BIN" ]; then CODEMAP_LAUNCHER="$CODEMAP_BIN"; '
            'else CODEMAP_LAUNCHER="{launcher}"; fi\n'
            '"$CODEMAP_LAUNCHER" query --compact fn-rdeps "pkg.core::target"',
            id="unset-alias",
        ),
        pytest.param(
            'if [ -n "$CODEMAP_BIN" ]; then CODEMAP_LAUNCHER="$(printf %s "$CODEMAP_BIN")"; '
            'else CODEMAP_LAUNCHER="{launcher}"; fi\n'
            '"$CODEMAP_LAUNCHER" query --compact fn-rdeps "pkg.core::target"',
            id="command-substitution",
        ),
        pytest.param(
            'if [ -n "$CODEMAP_BIN" ]; then CODEMAP_LAUNCHER="$CODEMAP_BIN"; '
            'else CODEMAP_LAUNCHER="{launcher}"; fi; payload="CODEMAP_LAUNCHER=/wrong/codemap-py"; '
            'eval "$payload"\n"$CODEMAP_LAUNCHER" query --compact fn-rdeps "pkg.core::target"',
            id="eval",
        ),
        pytest.param(
            'source /dev/null; if [ -n "$CODEMAP_BIN" ]; then CODEMAP_LAUNCHER="$CODEMAP_BIN"; '
            'else CODEMAP_LAUNCHER="{launcher}"; fi\n'
            '"$CODEMAP_LAUNCHER" query --compact fn-rdeps "pkg.core::target"',
            id="source",
        ),
        pytest.param(
            'read -r CODEMAP_LAUNCHER <<< /wrong/codemap-py; if [ -n "$CODEMAP_BIN" ]; '
            'then CODEMAP_LAUNCHER="$CODEMAP_BIN"; else CODEMAP_LAUNCHER="{launcher}"; fi\n'
            '"$CODEMAP_LAUNCHER" query --compact fn-rdeps "pkg.core::target"',
            id="read",
        ),
        pytest.param(
            'while false; do CODEMAP_LAUNCHER="$CODEMAP_BIN"; done; if [ -n "$CODEMAP_BIN" ]; '
            'then CODEMAP_LAUNCHER="$CODEMAP_BIN"; else CODEMAP_LAUNCHER="{launcher}"; fi\n'
            '"$CODEMAP_LAUNCHER" query --compact fn-rdeps "pkg.core::target"',
            id="loop",
        ),
        pytest.param(
            '( if [ -n "$CODEMAP_BIN" ]; then CODEMAP_LAUNCHER="$CODEMAP_BIN"; '
            'else CODEMAP_LAUNCHER="{launcher}"; fi )\n'
            '"$CODEMAP_LAUNCHER" query --compact fn-rdeps "pkg.core::target"',
            id="subshell",
        ),
        pytest.param(
            'if true; then if [ -n "$CODEMAP_BIN" ]; then CODEMAP_LAUNCHER="$CODEMAP_BIN"; '
            'else CODEMAP_LAUNCHER="{launcher}"; fi; fi\n'
            '"$CODEMAP_LAUNCHER" query --compact fn-rdeps "pkg.core::target"',
            id="nested-conditional",
        ),
        pytest.param(
            'if [ -n "$CODEMAP_BIN" ]; then CODEMAP_LAUNCHER="$CODEMAP_BIN"; '
            '"$CODEMAP_LAUNCHER" query --compact fn-rdeps "pkg.core::target"; '
            'else CODEMAP_LAUNCHER="{launcher}"; fi',
            id="query-inside-branch",
        ),
        pytest.param(
            'if [ -n "$CODEMAP_BIN" ]; then CODEMAP_LAUNCHER="$CODEMAP_BIN"; '
            'else CODEMAP_LAUNCHER="{launcher}"; fi; printf ready\n'
            '"$CODEMAP_LAUNCHER" query --compact fn-rdeps "pkg.core::target"',
            id="intervening-command",
        ),
        pytest.param(
            'if [ -n "$CODEMAP_BIN" ]; then CODEMAP_LAUNCHER="$CODEMAP_BIN"; '
            'else CODEMAP_LAUNCHER="/wrong/codemap-py"; fi\n'
            '"$CODEMAP_LAUNCHER" query --compact fn-rdeps "pkg.core::target"',
            id="wrong-locked-path",
        ),
    ],
)
def test_conditional_launcher_alias_rejects_unproven_forms(
    script_run_codex: Any, tmp_path: Path, template: str
) -> None:
    """Keep conditional alias credit limited to one immutable two-branch form.

    Each case could execute a launcher-like command, so a recognizer that only matches ``CODEMAP_LAUNCHER query`` would
    incorrectly satisfy C compliance.
    """
    skill_path = tmp_path / "codex-skills" / "query-code" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_bytes = b"# query-code\nUse the compact query.\n"
    skill_path.write_bytes(skill_bytes)
    launcher = tmp_path / "plugins" / "codemap-py" / "bin" / "codemap-py"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o755)
    events = [
        {
            "type": "item.completed",
            "item": {
                "id": "skill-read",
                "type": "command_execution",
                "command": 'cat "$CODEMAP_SKILL_FILE"',
                "status": "completed",
                "exit_code": 0,
                "aggregated_output": skill_bytes.decode(),
            },
        },
        {
            "type": "item.completed",
            "item": {
                "id": "query",
                "type": "command_execution",
                "command": template.format(launcher=launcher),
                "status": "completed",
                "exit_code": 0,
                "aggregated_output": json.dumps({"index": {"query_complete": True, "compact": True}}),
            },
        },
        {"type": "turn.completed"},
    ]

    parsed = script_run_codex.runtime.parse_codex_jsonl(
        "\n".join(json.dumps(event) for event in events),
        launcher_path=launcher,
        skill_path=skill_path,
        skill_sha256=hashlib.sha256(skill_bytes).hexdigest(),
    )

    assert parsed.skill_delivery_observed is True
    assert parsed.codemap_calls == 0
    assert parsed.codemap_skill_compact_successful_calls == 0
    assert script_run_codex._arm_compliance("C_strict", parsed) is False


def test_compound_skill_reader_is_diagnostic_but_does_not_block_a_later_c_query(
    script_run_codex: Any, tmp_path: Path
) -> None:
    """A standalone compact C query remains valid when an earlier reader is noncanonical."""
    skill_path = tmp_path / "query-code" / "SKILL.md"
    skill_path.parent.mkdir()
    skill_bytes = b"# query-code\nline 2\nline 3\n"
    skill_path.write_bytes(skill_bytes)
    events = [
        {
            "type": "item.completed",
            "item": {
                "id": "skill-read",
                "type": "command_execution",
                "command": f"sed -n '1,260p' {skill_path}; printf 'activated\\n'",
                "status": "completed",
                "exit_code": 0,
                "aggregated_output": skill_bytes.decode() + "activated\n",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "id": "query",
                "type": "command_execution",
                "command": '"$CODEMAP_BIN" query --compact fn-rdeps "pkg.core::target"',
                "status": "completed",
                "exit_code": 0,
                "aggregated_output": json.dumps({"index": {"query_complete": True, "compact": True}}),
            },
        },
        {"type": "turn.completed"},
    ]

    parsed = script_run_codex.runtime.parse_codex_jsonl(
        "\n".join(json.dumps(event) for event in events),
        skill_path=skill_path,
        skill_sha256=hashlib.sha256(skill_bytes).hexdigest(),
    )

    assert parsed.skill_delivery_observed is False
    assert parsed.codemap_skill_compact_successful_calls == 1
    assert script_run_codex._arm_compliance("C_strict", parsed) is True


def test_noncanonical_skill_reader_does_not_block_a_later_standalone_c_query(
    script_run_codex: Any, tmp_path: Path
) -> None:
    """Reader provenance remains diagnostic while the immutable C binding owns delivery."""
    skill_path = tmp_path / "query-code" / "SKILL.md"
    skill_path.parent.mkdir()
    skill_bytes = b"# query-code\nUse the compact query.\n"
    skill_path.write_bytes(skill_bytes)
    read_command = f"skill_path='{skill_path}'; wc -l \"$skill_path\"; sed -n '1,260p' \"$skill_path\""
    read_output = f"{len(skill_bytes.splitlines())} {skill_path}\n" + skill_bytes.decode()
    query_command = '"$CODEMAP_BIN" query --compact fn-rdeps "pkg.core::target"'
    parsed = script_run_codex.runtime.parse_codex_jsonl(
        "\n".join(
            json.dumps(event)
            for event in [
                {
                    "type": "item.completed",
                    "item": {
                        "id": "skill-read",
                        "type": "command_execution",
                        "command": read_command,
                        "status": "completed",
                        "exit_code": 0,
                        "aggregated_output": read_output,
                    },
                },
                {
                    "type": "item.completed",
                    "item": {
                        "id": "query",
                        "type": "command_execution",
                        "command": query_command,
                        "status": "completed",
                        "exit_code": 0,
                        "aggregated_output": json.dumps({"index": {"query_complete": True, "compact": True}}),
                    },
                },
                {"type": "turn.completed"},
            ]
        ),
        skill_path=skill_path,
        skill_sha256=hashlib.sha256(skill_bytes).hexdigest(),
    )

    assert parsed.skill_delivery_observed is False
    assert parsed.codemap_skill_compact_successful_calls == 1
    assert parsed.codemap_direct_successful_calls == 0
    assert script_run_codex._arm_compliance("C_strict", parsed) is True


@pytest.mark.parametrize(
    ("template", "output_kind", "_expected_direct_calls"),
    [
        pytest.param(
            "skill_path='{other}'; sed -n '1,260p' \"$skill_path\"; {query}",
            "complete",
            1,
            id="wrong-path",
        ),
        pytest.param(
            "skill_path='{skill}'; skill_path='{other}'; sed -n '1,260p' \"$skill_path\"; {query}",
            "complete",
            1,
            id="reassigned",
        ),
        pytest.param(
            "export skill_path='{skill}'; sed -n '1,260p' \"$skill_path\"; {query}",
            "complete",
            1,
            id="export-declaration",
        ),
        pytest.param(
            "typeset skill_path='{skill}'; sed -n '1,260p' \"$skill_path\"; {query}",
            "complete",
            1,
            id="shell-declaration",
        ),
        pytest.param(
            "skill_path='{skill}'; read -r skill_path <<< '{other}'; sed -n '1,260p' \"$skill_path\"; {query}",
            "complete",
            0,
            id="read-mutation",
        ),
        pytest.param(
            "while :; do skill_path='{skill}'; break; done; sed -n '1,260p' \"$skill_path\"; {query}",
            "complete",
            1,
            id="while-binding",
        ),
        pytest.param(
            "if true; then skill_path='{skill}'; fi; sed -n '1,260p' \"$skill_path\"; {query}",
            "complete",
            1,
            id="if-binding",
        ),
        pytest.param(
            "until false; do skill_path='{skill}'; break; done; sed -n '1,260p' \"$skill_path\"; {query}",
            "complete",
            1,
            id="until-binding",
        ),
        pytest.param(
            "( skill_path='{skill}'; sed -n '1,260p' \"$skill_path\" ); {query}",
            "complete",
            0,
            id="subshell-binding",
        ),
        pytest.param(
            "skill_path='{skill}' && sed -n '1,260p' \"$skill_path\"; {query}",
            "complete",
            1,
            id="conditional-and-binding",
        ),
        pytest.param(
            "skill_path=\"$(printf '%s' '{skill}')\"; sed -n '1,260p' \"$skill_path\"; {query}",
            "complete",
            1,
            id="command-substitution",
        ),
        pytest.param(
            "skill_path='{skill}'; sed -n '1,260p' \"$skill_path\"; {query}",
            "incomplete",
            1,
            id="incomplete-bytes",
        ),
        pytest.param(
            "skill_path='{skill}'; sed -n '1,260p' \"$skill_path\"; {query}",
            "wrong",
            1,
            id="wrong-bytes",
        ),
    ],
)
def test_historical_bound_skill_reader_forms_reject_native_item_contract(
    script_run_codex: Any, tmp_path: Path, template: str, output_kind: str, _expected_direct_calls: int
) -> None:
    """No compound historical reader/query command can earn direct or C credit."""
    skill_path = tmp_path / "query-code" / "SKILL.md"
    other_path = tmp_path / "other" / "SKILL.md"
    skill_path.parent.mkdir()
    other_path.parent.mkdir()
    skill_bytes = b"# query-code\nUse the compact query.\n"
    skill_path.write_bytes(skill_bytes)
    other_path.write_bytes(skill_bytes)
    query = '"$CODEMAP_BIN" query --compact fn-rdeps "pkg.core::target"'
    command = template.format(skill=skill_path, other=other_path, query=query)
    output_by_kind = {
        "complete": skill_bytes.decode(),
        "incomplete": "# query-code\n",
        "wrong": skill_bytes.decode().replace("compact", "expanded"),
    }
    parsed = script_run_codex.runtime.parse_codex_jsonl(
        _completed_stream(
            commands=[
                {
                    "type": "command_execution",
                    "command": command,
                    "status": "completed",
                    "exit_code": 0,
                    "aggregated_output": output_by_kind[output_kind]
                    + json.dumps({"index": {"query_complete": True, "compact": True}}),
                }
            ]
        ),
        skill_path=skill_path,
        skill_sha256=hashlib.sha256(skill_bytes).hexdigest(),
    )

    assert parsed.skill_delivery_observed is False
    assert parsed.codemap_skill_successful_calls == 0
    assert parsed.codemap_direct_compact_successful_calls == 0
    assert script_run_codex._arm_compliance("C_strict", parsed) is False


def test_historical_query_then_skill_read_rejects_native_item_contract(script_run_codex: Any, tmp_path: Path) -> None:
    """Reading a Skill in the query item cannot satisfy the separate-item C rule."""
    skill_path = tmp_path / "query-code" / "SKILL.md"
    skill_path.parent.mkdir()
    skill_bytes = b"# query-code\n"
    skill_path.write_bytes(skill_bytes)
    output = json.dumps({"index": {"query_complete": True}}) + "\n" + skill_bytes.decode()
    command = f'"$CODEMAP_BIN" query --compact rdeps pkg.core; cat {skill_path}'
    parsed = script_run_codex.runtime.parse_codex_jsonl(
        _completed_stream(
            commands=[
                {
                    "type": "command_execution",
                    "command": command,
                    "status": "completed",
                    "exit_code": 0,
                    "aggregated_output": output,
                }
            ]
        ),
        skill_path=skill_path,
        skill_sha256=hashlib.sha256(skill_bytes).hexdigest(),
    )

    assert parsed.skill_delivery_observed is False
    assert parsed.codemap_direct_compact_successful_calls == 0
    assert parsed.codemap_skill_compact_successful_calls == 0
    assert script_run_codex._arm_compliance("C_strict", parsed) is False


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(
            'CODEMAP_BIN=/wrong/codemap-py; "$CODEMAP_BIN" query --compact rdeps pkg.core',
            id="direct-variable-reassigned",
        ),
        pytest.param(
            'export CODEMAP_BIN=/wrong/codemap-py; "$CODEMAP_BIN" query --compact rdeps pkg.core',
            id="direct-variable-exported",
        ),
        pytest.param(
            'codemap_bin="${CODEMAP_BIN:-{launcher}}"; codemap_bin=/wrong; '
            '"$codemap_bin" query --compact rdeps pkg.core',
            id="bound-variable-reassigned",
        ),
        pytest.param(
            'payload="CODEMAP_BIN=/wrong/codemap-py"; eval "$payload"; "$CODEMAP_BIN" query --compact rdeps pkg.core',
            id="direct-variable-eval-indirection",
        ),
        pytest.param(
            'name=CODEMAP_BIN; typeset "$name"=/wrong/codemap-py; "$CODEMAP_BIN" query --compact rdeps pkg.core',
            id="direct-variable-typeset-indirection",
        ),
        pytest.param(
            'for CODEMAP_BIN in /wrong/codemap-py; do "$CODEMAP_BIN" query --compact rdeps pkg.core; done',
            id="direct-variable-loop-reassignment",
        ),
        pytest.param(
            "printf 'CODEMAP_BIN=%s\\n' \"${CODEMAP_BIN-}\"; "
            "CODEMAP_BIN=/wrong/codemap-py; "
            'if [ -n "${CODEMAP_BIN-}" ]; then '
            '"$CODEMAP_BIN" query --compact rdeps pkg.core; fi',
            id="diagnostic-then-direct-reassignment",
        ),
        pytest.param(
            'while IFS= read -r CODEMAP_BIN; do "$CODEMAP_BIN" query --compact '
            "rdeps pkg.core; break; done <<< /wrong/codemap-py",
            id="while-read-direct-reassignment",
        ),
    ],
)
def test_query_credit_rejects_launcher_variable_mutation(script_run_codex: Any, tmp_path: Path, command: str) -> None:
    """Shell reassignment cannot substitute an unlocked executable for the staged launcher."""
    launcher = tmp_path / "codemap-py"
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o755)

    assert not script_run_codex.runtime._is_codemap_command(
        command.replace("{launcher}", str(launcher)),
        launcher_path=launcher,
    )


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(
            'codemap_bin="${CODEMAP_BIN:-/wrong/codemap-py}"; "$codemap_bin" query --compact rdeps pkg.core',
            id="wrong-fallback",
        ),
        pytest.param(
            'codemap_bin="${CODEMAP_BIN:-{launcher}}"; "$codemap_bin" --version',
            id="not-query",
        ),
        pytest.param(
            'codemap_bin="{launcher}"; "$codemap_bin" query --compact rdeps pkg.core',
            id="unbound-direct-assignment",
        ),
        pytest.param(
            'runner="${CODEMAP_BIN:-{launcher}}"; "$runner" query --compact rdeps pkg.core',
            id="other-variable",
        ),
        pytest.param(
            'echo "$CODEMAP_BIN query --compact rdeps pkg.core"',
            id="echo-only",
        ),
    ],
)
def test_bound_launcher_query_rejects_ambiguous_or_unlocked_shell_forms(
    script_run_codex: Any, tmp_path: Path, command: str
) -> None:
    """Do not widen query evidence to unrelated variables, paths, or non-query commands."""
    launcher = tmp_path / "codemap-py"
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o755)

    assert not script_run_codex.runtime._is_codemap_command(
        command.replace("{launcher}", str(launcher)), launcher_path=launcher
    )


@pytest.mark.parametrize(
    ("template", "output_kind", "exit_code", "expected"),
    [
        pytest.param('cat "$CODEMAP_SKILL_FILE"', "complete", 0, True, id="canonical-complete"),
        pytest.param("cat {skill}", "complete", 0, False, id="literal-path"),
        pytest.param("sed -n '1,1p' {skill}", "partial", 0, False, id="partial-output"),
        pytest.param("cat {other}", "complete", 0, False, id="wrong-path"),
        pytest.param('cat "$CODEMAP_SKILL_FILE"', "wrong", 0, False, id="wrong-bytes"),
        pytest.param('cat "$CODEMAP_SKILL_FILE"', "complete", 1, False, id="failed-command"),
        pytest.param("cat $CODEMAP_SKILL_FILE", "complete", 0, False, id="unquoted-variable"),
    ],
)
def test_skill_read_requires_exact_environment_command_bytes_and_success(
    script_run_codex: Any,
    tmp_path: Path,
    template: str,
    output_kind: str,
    exit_code: int,
    expected: bool,
) -> None:
    """Only the exact bound reader with exact bytes and zero exit proves activation."""
    skill = tmp_path / "query-code" / "SKILL.md"
    skill.parent.mkdir()
    skill_bytes = b"# query-code\nline 2\nline 3\n"
    skill.write_bytes(skill_bytes)
    command = template.format(skill=skill, other=tmp_path / "other" / "SKILL.md")
    outputs = {
        "complete": skill_bytes.decode(),
        "partial": "# query-code\n",
        "wrong": skill_bytes.decode().replace("line 2", "changed"),
    }
    parsed = script_run_codex.runtime.parse_codex_jsonl(
        _completed_stream(
            commands=[
                {
                    "type": "command_execution",
                    "command": command,
                    "status": "completed" if exit_code == 0 else "failed",
                    "exit_code": exit_code,
                    "aggregated_output": outputs[output_kind],
                }
            ]
        ),
        skill_path=skill,
        skill_sha256=hashlib.sha256(skill_bytes).hexdigest(),
    )

    assert parsed.skill_delivery_observed is expected


def test_rescore_results_replays_frozen_events_without_mutating_run_artifacts(
    script_run_codex: Any, tmp_path: Path
) -> None:
    """Offline rescore derives corrected fields from frozen inputs without model credentials."""
    run_dir = tmp_path / "run"
    snapshot_dir = run_dir / "inputs"
    shared = snapshot_dir / "shared"
    shared.mkdir(parents=True)
    task = {
        "id": "SE-01",
        "prompt": "locate pkg.symbol",
        "type": "symbol_extraction",
        "scoreable": True,
        "expected_queries": [{"cmd": "symbols", "args": ["pkg.symbol"]}],
        "ground_truth": {"start_line": 10, "qualified_name": "pkg.symbol"},
    }
    tasks_path = shared / "tasks-bench.json"
    tasks_bytes = (json.dumps({"tasks": [task]}, sort_keys=True) + "\n").encode()
    tasks_path.write_bytes(tasks_bytes)
    skill_path = snapshot_dir / "C_strict" / "codemap-py" / "codex-skills" / "query-code" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_bytes = b"# query-code\nUse the compact query.\n"
    skill_path.write_bytes(skill_bytes)
    skill_sha256 = hashlib.sha256(skill_bytes).hexdigest()
    snapshot_payload = {
        "schema_version": "codex-structural-input-snapshot-v1",
        "files": [
            {
                "role": "task_suite",
                "archived_path": "shared/tasks-bench.json",
                "sha256": hashlib.sha256(tasks_bytes).hexdigest(),
                "bytes": len(tasks_bytes),
            },
            {
                "role": "C_strict:codemap-py",
                "archived_path": "C_strict/codemap-py/codex-skills/query-code/SKILL.md",
                "sha256": skill_sha256,
                "bytes": len(skill_bytes),
            },
        ],
        "auth_source": None,
    }
    snapshot_path = snapshot_dir / "input-snapshot.json"
    snapshot_bytes = (json.dumps(snapshot_payload, sort_keys=True) + "\n").encode()
    snapshot_path.write_bytes(snapshot_bytes)
    telemetry_path = run_dir / "telemetry.jsonl"
    raw_events = [
        {"type": "thread.started", "thread_id": "frozen-thread"},
        {"type": "item.completed", "item": {"id": "answer", "type": "agent_message", "text": "start_line: 10"}},
        {
            "type": "item.completed",
            "item": {
                "id": "query",
                "type": "command_execution",
                "command": "$CODEMAP_BIN query --compact symbols pkg.symbol",
                "status": "completed",
                "exit_code": 0,
                "aggregated_output": '{"index":{"query_complete":true,"compact":true}}',
            },
        },
        {"type": "turn.completed", "status": "completed"},
    ]
    direct_row = {
        "task_id": "SE-01",
        "task_type": "symbol_extraction",
        "arm": "B_auto",
        "repetition": 1,
        "model": script_run_codex.PARITY_CODEX_MODEL,
        "scoreable": True,
        "output_text": "stale answer",
        "codemap_semantic_compliance": False,
        "task_query_fitness": 0.25,
        "raw_events": raw_events,
    }
    skill_events = [
        {"type": "thread.started", "thread_id": "frozen-skill-thread"},
        {
            "type": "item.completed",
            "item": {
                "id": "skill",
                "type": "command_execution",
                "command": 'cat "$CODEMAP_SKILL_FILE"',
                "status": "completed",
                "exit_code": 0,
                "aggregated_output": skill_bytes.decode(),
            },
        },
        {
            "type": "item.completed",
            "item": {
                "id": "query",
                "type": "command_execution",
                "command": '"$CODEMAP_BIN" query --compact symbols pkg.symbol',
                "status": "completed",
                "exit_code": 0,
                "aggregated_output": '{"index":{"query_complete":true,"compact":true}}',
            },
        },
        {"type": "item.completed", "item": {"id": "answer", "type": "agent_message", "text": "start_line: 10"}},
        {"type": "turn.completed", "status": "completed"},
    ]
    skill_row = {**direct_row, "arm": "C_strict", "raw_events": skill_events}
    telemetry_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in (direct_row, skill_row)),
        encoding="utf-8",
    )
    metadata_path = run_dir / "run-metadata.json"
    metadata = {
        "schema_version": "codex-structural-run-metadata-v1",
        "status": "completed",
        "execution": {
            "selected_task_ids": ["SE-01"],
            "coordinates": [
                {"task_id": "SE-01", "repetition": 1, "arm": "B_auto"},
                {"task_id": "SE-01", "repetition": 1, "arm": "C_strict"},
            ],
        },
        "treatments": {"artifact_sha256": {"codemap_query_skill": skill_sha256}},
        "inputs": {"snapshot": {"path": str(snapshot_path), "sha256": hashlib.sha256(snapshot_bytes).hexdigest()}},
        "artifacts": {
            "telemetry_jsonl": str(telemetry_path),
            "telemetry_sha256": hashlib.sha256(telemetry_path.read_bytes()).hexdigest(),
        },
    }
    metadata_path.write_text(json.dumps(metadata, sort_keys=True) + "\n", encoding="utf-8")
    source_bytes = {
        path: path.read_bytes() for path in (telemetry_path, metadata_path, snapshot_path, tasks_path, skill_path)
    }

    artifact_path = script_run_codex.rescore_results(run_dir)
    first_bytes = artifact_path.read_bytes()
    repeated_path = script_run_codex.rescore_results(run_dir)

    assert repeated_path == artifact_path
    assert artifact_path.read_bytes() == first_bytes
    assert all(path.read_bytes() == contents for path, contents in source_bytes.items())
    artifact = json.loads(first_bytes)
    assert artifact["schema_version"] == "codex-structural-offline-rescore-v2"
    assert artifact["source"]["telemetry_sha256"] == hashlib.sha256(source_bytes[telemetry_path]).hexdigest()
    assert artifact["source"]["frozen_suite_semantic_sha256"] == core.semantic_suite_hash([task])
    rows = {row["arm"]: row for row in artifact["rows"]}
    assert rows["B_auto"]["quality_score"] == 1.0
    assert rows["B_auto"]["output_text"] == "start_line: 10"
    assert rows["B_auto"]["codemap_direct_compact_successful_calls"] == 1
    assert rows["B_auto"]["treatment_adherence"] is True
    assert rows["B_auto"]["locked_query_conformance"] is True
    assert rows["B_auto"]["locked_query_fitness"] == 1.0
    assert "codemap_semantic_compliance" not in rows["B_auto"]
    assert "task_query_fitness" not in rows["B_auto"]
    assert rows["C_strict"]["skill_delivery_observed"] is True
    assert rows["C_strict"]["codemap_skill_compact_successful_calls"] == 1
    assert rows["C_strict"]["compliance"] is True
    assert rows["C_strict"]["treatment_adherence"] is True

    telemetry_path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="telemetry hash mismatch"):
        script_run_codex.rescore_results(run_dir)
    telemetry_path.write_bytes(source_bytes[telemetry_path])
    metadata["status"] = "running"
    metadata_path.write_text(json.dumps(metadata, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="requires completed"):
        script_run_codex.rescore_results(run_dir)
    metadata["status"] = "completed"
    metadata["execution"]["selected_task_ids"] = ["not-in-frozen-suite"]
    metadata_path.write_text(json.dumps(metadata, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="task scope"):
        script_run_codex.rescore_results(run_dir)
    metadata["execution"]["selected_task_ids"] = ["SE-01"]
    telemetry_path.write_text(json.dumps(direct_row, sort_keys=True) + "\n", encoding="utf-8")
    metadata["artifacts"]["telemetry_sha256"] = hashlib.sha256(telemetry_path.read_bytes()).hexdigest()
    metadata_path.write_text(json.dumps(metadata, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="telemetry is incomplete"):
        script_run_codex.rescore_results(run_dir)


def test_main_threads_an_explicit_manifest_path_into_task_loading_and_ordering(
    script_run_codex: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A future relock must not be silently replaced by the module-global manifest."""
    manifest_path = tmp_path / "future-manifest.json"
    manifest_path.write_bytes(MANIFEST_PATH.read_bytes())
    seen: dict[str, Path] = {}
    tasks = [{"id": "fixture", "prompt": "prompt", "type": "demo"}]

    def _load_tasks(_tasks_path: Path, selected_manifest: Path) -> list[dict[str, str]]:
        """Record the selected manifest and return the fixture task list."""
        seen["tasks"] = selected_manifest
        return tasks

    def _order(revision: str, *_args: Any, **_kwargs: Any) -> tuple[str, ...]:
        """Record the revision and return the fixture treatment order."""
        seen["revision"] = Path(revision)
        return ("A_plain", "B_auto", "C_strict")

    class FixtureRunner:
        """Provide deterministic preflight evidence without invoking Codex."""

        def __init__(self, *_args: Any, **kwargs: Any) -> None:
            """Initialize the test double's fixture-controlled state."""
            seen["runner"] = kwargs["manifest_path"]
            self.timeout = kwargs["timeout"]

        def probe_arm(self, _arm: str) -> dict[str, bool]:
            """Report fixture-controlled Codemap availability for a treatment arm."""
            return {"codemap_available": False}

    monkeypatch.setattr(script_run_codex, "load_tasks_with_provenance", _load_tasks)
    monkeypatch.setattr(script_run_codex, "_read_manifest_revision", lambda path: str(path))
    monkeypatch.setattr(script_run_codex, "deterministic_arm_order", _order)
    monkeypatch.setattr(script_run_codex, "CodexRunner", FixtureRunner)

    script_run_codex.main(
        repo_path=tmp_path,
        model=script_run_codex.PARITY_CODEX_MODEL,
        tasks_path=tmp_path / "tasks.json",
        manifest_path=manifest_path,
        dry_run=True,
    )

    assert seen["tasks"] == manifest_path
    assert seen["runner"] == manifest_path
    assert seen["revision"] == manifest_path
