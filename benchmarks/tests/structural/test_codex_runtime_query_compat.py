"""No-model compatibility tests for Codex query telemetry and offline replay.

Covers how Codex ``exec --json`` events are classified as Codemap queries (native item shapes, launcher spellings, skill
delivery), why historical wrapped or compound command shapes stay evidence only, the direct-CLI arm preparation and
admission, and the offline rescore that replays frozen events without a model.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest

# Patch seams live in the package modules the runner shim re-exports from: patching the shim
# would leave each package module's own global binding untouched. Inside the package every seam
# below is reached through its defining module, so this is the single place to patch it.
from _bench_codex.structural import arms as codex_arms
from _bench_codex.structural import manifest as codex_manifest
from _bench_codex.structural import runner as codex_structural_runner
from _bench_codex.structural import tasks as codex_tasks
from _codex_stream import completed_stream

from benchmarks._bench_common import provider_parity_contracts as core

BENCHMARKS_DIR = Path(__file__).resolve().parent.parent.parent
MANIFEST_PATH = BENCHMARKS_DIR / "manifests" / "codex-integration.json"
POSIX_SECURITY = pytest.mark.skipif(
    sys.platform == "win32", reason="requires POSIX private-mode and executable-shell semantics"
)

_COMPACT_RESULT = json.dumps({"index": {"query_complete": True, "compact": True}})
_COMPLETE_RESULT = json.dumps({"index": {"query_complete": True}})
_STUB_LAUNCHER_SOURCE = "#!/bin/sh\nexit 0\n"
_SKILL_CONTENT = b"# query-code\nUse the compact query.\n"
_RDEPS_QUERY = '"$CODEMAP_BIN" query --compact rdeps pkg.core'
_FN_RDEPS_QUERY = '"$CODEMAP_BIN" query --compact fn-rdeps "pkg.core::target"'
_SKILL_READ_PLAIN = 'cat "$CODEMAP_SKILL_FILE"'
_SKILL_READ_WRAPPED = "/bin/zsh -lc 'cat \"$CODEMAP_SKILL_FILE\"'"
#: Paid local-alias query shape; ``{launcher}`` is replaced by the verified launcher path.
_BOUND_ALIAS_QUERY = '/bin/zsh -lc \'codemap_bin="${CODEMAP_BIN:-{launcher}}"; "$codemap_bin" query --compact fn-rdeps "pkg.core::target"\''


def _write_launcher(path: Path) -> Path:
    """Create an executable stand-in for the Codemap launcher and return its path.

    Example:
        >>> from tempfile import TemporaryDirectory
        >>> with TemporaryDirectory() as directory:
        ...     launcher = _write_launcher(Path(directory) / "bin" / "codemap-py")
        ...     launcher.read_text() == _STUB_LAUNCHER_SOURCE
        True
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_STUB_LAUNCHER_SOURCE, encoding="utf-8")
    path.chmod(0o755)
    return path


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
    launcher = _write_launcher(runtime / "bin" / "codemap-py")
    exclusions = runtime / "bin" / "_exclusions.py"
    entrypoint = runtime / "scripts" / "codemap_py_entry.py"
    package = runtime / "src" / "codemap_py"
    entrypoint.parent.mkdir(parents=True)
    package.mkdir(parents=True)
    exclusions.write_text("EXCLUSION_PATTERNS = ()\n", encoding="utf-8")
    entrypoint.write_text("raise SystemExit(0)\n", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    return launcher


def _write_private_config(home_path: Path) -> Path:
    """Create an arm home holding an owner-only ``config.toml`` and return the config path.

    Example:
        >>> from tempfile import TemporaryDirectory
        >>> with TemporaryDirectory() as directory:
        ...     _write_private_config(Path(directory) / "home").name
        'config.toml'
    """
    home_path.mkdir(parents=True, exist_ok=True)
    config = home_path / "config.toml"
    config.write_text("", encoding="utf-8")
    config.chmod(0o600)
    return config


@dataclass(frozen=True)
class _Skill:
    """An installed query-code Skill file together with the digest the parser binds it to."""

    path: Path
    content: bytes

    @property
    def text(self) -> str:
        """Return the Skill bytes as the text a successful read prints."""
        return self.content.decode()

    @property
    def binding(self) -> dict[str, Any]:
        """Return the keyword arguments that bind ``parse_codex_jsonl`` to this exact Skill file."""
        return {"skill_path": self.path, "skill_sha256": hashlib.sha256(self.content).hexdigest()}


def _write_skill(root: Path, content: bytes = _SKILL_CONTENT) -> _Skill:
    """Install a Skill file with the given bytes under ``root`` and return it.

    Example:
        >>> from tempfile import TemporaryDirectory
        >>> with TemporaryDirectory() as directory:
        ...     skill = _write_skill(Path(directory))
        ...     skill.path.read_bytes() == skill.content, sorted(skill.binding)
        (True, ['skill_path', 'skill_sha256'])
    """
    path = root / "codex-skills" / "query-code" / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return _Skill(path, content)


def _command(
    command: str, output: str = _COMPACT_RESULT, *, status: str = "completed", exit_code: int = 0
) -> dict[str, Any]:
    """Build one Codex ``command_execution`` item body.

    Example:
        >>> _command("ls", "out")["aggregated_output"], _command("ls", status="failed", exit_code=1)["exit_code"]
        ('out', 1)
    """
    return {
        "type": "command_execution",
        "command": command,
        "status": status,
        "exit_code": exit_code,
        "aggregated_output": output,
    }


def _item_event(item_id: str, item: dict[str, Any]) -> dict[str, Any]:
    """Wrap an item body in the ``item.completed`` event Codex emits.

    Example:
        >>> _item_event("query", {"type": "command_execution"})
        {'type': 'item.completed', 'item': {'id': 'query', 'type': 'command_execution'}}
    """
    return {"type": "item.completed", "item": {"id": item_id, **item}}


def _jsonl(events: list[dict[str, Any]]) -> str:
    """Serialize events one JSON object per line.

    Example:
        >>> _jsonl([{"a": 1}, {"b": 2}])
        '{"a": 1}\\n{"b": 2}'
    """
    return "\n".join(json.dumps(event) for event in events)


def _read_then_query_stream(
    skill: _Skill,
    read_command: str,
    query_command: str,
    *,
    read_output: str | None = None,
    query_output: str = _COMPACT_RESULT,
) -> str:
    """Build a stream where a Skill-read item is followed by a query item.

    Example:
        >>> from tempfile import TemporaryDirectory
        >>> with TemporaryDirectory() as directory:
        ...     lines = _read_then_query_stream(_write_skill(Path(directory)), "cat f", "query").splitlines()
        ...     [json.loads(line).get("item", {}).get("id") for line in lines]
        ['skill-read', 'query', None]
    """
    events = [
        _item_event("skill-read", _command(read_command, skill.text if read_output is None else read_output)),
        _item_event("query", _command(query_command, query_output)),
        {"type": "turn.completed"},
    ]
    return _jsonl(events)


def _fields(subject: object, expected: dict[str, object]) -> dict[str, object]:
    """Read back the attributes named in ``expected`` so one assertion compares them all.

    Example:
        >>> _fields(SimpleNamespace(a=1, b=2), {"a": 0})
        {'a': 1}
    """
    return {name: getattr(subject, name) for name in expected}


@pytest.fixture(name="runtime")
def _runtime(script_run_codex: Any) -> Any:
    """Return the adapter's runtime module, where command classification and event parsing live."""
    return script_run_codex.runtime


@pytest.fixture(name="launcher")
def _launcher(tmp_path: Path) -> Path:
    """Return an executable stand-in for the verified Codemap launcher."""
    return _write_launcher(tmp_path / "runtime" / "bin" / "codemap-py")


@pytest.fixture(name="skill")
def _skill(tmp_path: Path) -> _Skill:
    """Return an installed query-code Skill with the default bytes."""
    return _write_skill(tmp_path)


class TestIsCodemapCommand:
    """Only a standalone native query item is recognized; every other shell spelling stays historical evidence."""

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
            pytest.param("$CODEMAP_BIN query --compact rdeps pkg.core; echo done", False, id="trailing-compound"),
            pytest.param("/plugin/bin/codemap-py query --compact rdeps pkg.core", False, id="literal-plugin-path"),
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
    def test_classifies_native_item_shell_shapes(self, runtime: Any, command: str, expected: bool) -> None:
        """Only standalone native query commands are recognized as the native item contract.

        Standalone native commands remain evidence across quoted launcher spellings, while inspection, help, background,
        redirect, substitution, and group shapes are rejected. Only one exact Codex transport wrapper may contain the
        native payload: nested, compound, redirected, or commandless wrappers are rejected, and neither a literal
        launcher path nor a trailing compound is a native item.
        """
        assert runtime._is_codemap_command(command) is expected

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param('"{launcher}" query --compact rdeps pkg.core', id="exact-launcher-path"),
            pytest.param(
                "printf 'codemap_bin=%s\\n' \"${CODEMAP_BIN-}\"; "
                'codemap_bin="${CODEMAP_BIN:-{launcher}}"; '
                '"$codemap_bin" query --compact rdeps pkg.core',
                id="diagnostic-then-bound-launcher",
            ),
            pytest.param(
                'CODEMAP_BIN="${CODEMAP_BIN:-/wrong/codemap-py}"; "$CODEMAP_BIN" query --compact rdeps pkg.core',
                id="wrong-default-fallback",
            ),
            pytest.param(
                'CODEMAP_BIN="{launcher}"; "$CODEMAP_BIN" query --compact rdeps pkg.core', id="plain-assignment"
            ),
            pytest.param(
                'CODEMAP_BIN="${CODEMAP_BIN:-{launcher}}"; CODEMAP_BIN=/wrong/codemap-py; '
                '"$CODEMAP_BIN" query --compact rdeps pkg.core',
                id="fallback-then-wrong-reassignment",
            ),
            pytest.param(
                'export CODEMAP_BIN="${CODEMAP_BIN:-{launcher}}"; "$CODEMAP_BIN" query --compact rdeps pkg.core',
                id="exported-fallback",
            ),
            pytest.param(
                'readonly CODEMAP_BIN="${CODEMAP_BIN:-{launcher}}"; "$CODEMAP_BIN" query --compact rdeps pkg.core',
                id="readonly-fallback",
            ),
            pytest.param(
                'typeset CODEMAP_BIN="${CODEMAP_BIN:-{launcher}}"; "$CODEMAP_BIN" query --compact rdeps pkg.core',
                id="typeset-fallback",
            ),
            pytest.param(
                'CODEMAP_BIN="${CODEMAP_BIN:-{launcher}}"; unset CODEMAP_BIN; "$CODEMAP_BIN" query --compact rdeps pkg.core',
                id="unset-after-fallback",
            ),
            pytest.param(
                'CODEMAP_BIN="$(printf \'%s\' {launcher})"; "$CODEMAP_BIN" query --compact rdeps pkg.core',
                id="command-substitution-assignment",
            ),
            pytest.param(
                'payload="CODEMAP_BIN=/wrong/codemap-py"; eval "$payload"; "$CODEMAP_BIN" query --compact rdeps pkg.core',
                id="eval-payload-assignment",
            ),
            pytest.param(
                'if true; then CODEMAP_BIN="${CODEMAP_BIN:-{launcher}}"; fi; "$CODEMAP_BIN" query --compact rdeps pkg.core',
                id="conditional-fallback-block",
            ),
            pytest.param(
                'CODEMAP_BIN=/wrong/codemap-py; "$CODEMAP_BIN" query --compact rdeps pkg.core', id="wrong-assignment"
            ),
            pytest.param(
                'export CODEMAP_BIN=/wrong/codemap-py; "$CODEMAP_BIN" query --compact rdeps pkg.core',
                id="exported-wrong-assignment",
            ),
            pytest.param(
                'codemap_bin="${CODEMAP_BIN:-{launcher}}"; codemap_bin=/wrong; "$codemap_bin" query --compact rdeps pkg.core',
                id="lowercase-alias-reassigned",
            ),
            pytest.param(
                'name=CODEMAP_BIN; typeset "$name"=/wrong/codemap-py; "$CODEMAP_BIN" query --compact rdeps pkg.core',
                id="indirect-typeset-name",
            ),
            pytest.param(
                'for CODEMAP_BIN in /wrong/codemap-py; do "$CODEMAP_BIN" query --compact rdeps pkg.core; done',
                id="for-loop-binding",
            ),
            pytest.param(
                "printf 'CODEMAP_BIN=%s\\n' \"${CODEMAP_BIN-}\"; "
                "CODEMAP_BIN=/wrong/codemap-py; "
                'if [ -n "${CODEMAP_BIN-}" ]; then '
                '"$CODEMAP_BIN" query --compact rdeps pkg.core; fi',
                id="diagnostic-assignment-conditional-query",
            ),
            pytest.param(
                'while IFS= read -r CODEMAP_BIN; do "$CODEMAP_BIN" query --compact '
                "rdeps pkg.core; break; done <<< /wrong/codemap-py",
                id="while-read-binding",
            ),
            pytest.param(
                'codemap_bin="${CODEMAP_BIN:-/wrong/codemap-py}"; "$codemap_bin" query --compact rdeps pkg.core',
                id="lowercase-wrong-default-fallback",
            ),
            pytest.param(
                'codemap_bin="${CODEMAP_BIN:-{launcher}}"; "$codemap_bin" --version', id="lowercase-version-probe"
            ),
            pytest.param(
                'codemap_bin="{launcher}"; "$codemap_bin" query --compact rdeps pkg.core',
                id="lowercase-plain-assignment",
            ),
            pytest.param(
                'runner="${CODEMAP_BIN:-{launcher}}"; "$runner" query --compact rdeps pkg.core',
                id="unrelated-variable-fallback",
            ),
            pytest.param('echo "$CODEMAP_BIN query --compact rdeps pkg.core"', id="echo-inspection"),
        ],
    )
    def test_rejects_launcher_variable_forms(self, runtime: Any, launcher: Path, command: str) -> None:
        """Shell forms that rebind, indirect, or merely mention the launcher never count as a native query.

        Assignments never substitute for the future standalone native item, shell reassignment cannot swap an unlocked
        executable for the staged launcher, and query evidence is not widened to unrelated variables, paths, or non-
        query commands. The exact absolute launcher path is likewise not the native ``$CODEMAP_BIN`` item.
        """
        assert not runtime._is_codemap_command(command.replace("{launcher}", str(launcher)), launcher_path=launcher)

    @pytest.mark.parametrize(
        "command_template",
        [
            '"{launcher}" --help',
            'echo "{launcher}" query --compact rdeps pkg.core',
            '"{other}" query --compact rdeps pkg.core',
        ],
    )
    def test_verified_launcher_rejects_non_dedicated_or_wrong_commands(
        self, runtime: Any, tmp_path: Path, launcher: Path, command_template: str
    ) -> None:
        """Only a standalone query through the verified absolute path is observed.

        Help output, an ``echo`` that merely mentions the path, and a query through a different executable are neither
        observed as compact queries nor credited as Codemap commands, even when equivalent launchers are allowed.
        """
        other = _write_launcher(tmp_path / "other" / "bin" / "codemap-py")
        command = command_template.format(launcher=launcher, other=other)

        assert not runtime._observes_compact_codemap_query(command, launcher_path=launcher)
        assert not runtime._is_codemap_command(command, launcher_path=launcher, allow_equivalent_launcher=True)


class TestLauncherObservation:
    """A query through the verified launcher is observed as evidence, but credit needs the native item shape."""

    @pytest.mark.parametrize(
        ("allow_equivalent_launcher", "expected"),
        [
            pytest.param(
                False,
                {
                    "codemap_observed_calls": 1,
                    "codemap_observed_successful_calls": 1,
                    "codemap_observed_complete_calls": 1,
                    "codemap_calls": 0,
                },
                id="replay-observes-without-credit",
            ),
            pytest.param(
                True, {"codemap_calls": 1, "codemap_compact_successful_calls": 1}, id="opt-in-credits-the-launcher"
            ),
        ],
    )
    def test_verified_launcher_requires_opt_in_for_credit(
        self, runtime: Any, launcher: Path, allow_equivalent_launcher: bool, expected: dict[str, int]
    ) -> None:
        """A verified literal launcher is observable without rewriting historical credit.

        The same stream is parsed twice in meaning: by default the query is only observed, so historical rows keep the
        credit they were scored with; when the caller opts in, the verified path counts as a Codemap call.
        """
        stream = completed_stream(commands=[_command(f"/bin/zsh -lc '\"{launcher}\" query --compact rdeps pkg.core'")])

        parsed = runtime.parse_codex_jsonl(
            stream, launcher_path=launcher, allow_equivalent_launcher=allow_equivalent_launcher
        )

        assert _fields(parsed, expected) == expected

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
    def test_success_and_completion_are_distinct_evidence(
        self, runtime: Any, launcher: Path, status: str, exit_code: int, output: str, successful: int, complete: int
    ) -> None:
        """A completed process and a complete query result are independent evidence.

        A failed process, a result without ``query_complete``, and a truncated result are each still observed as a call
        but contribute neither success nor completion, and none of them earns credit.
        """
        command = f"/bin/zsh -lc '\"{launcher}\" query --compact rdeps pkg.core'"
        stream = completed_stream(commands=[_command(command, output, status=status, exit_code=exit_code)])

        parsed = runtime.parse_codex_jsonl(stream, launcher_path=launcher)

        assert parsed.codemap_observed_calls == 1
        assert parsed.codemap_observed_successful_calls == successful
        assert parsed.codemap_observed_complete_calls == complete
        assert parsed.codemap_calls == 0

    @pytest.mark.parametrize(
        "command",
        [
            "'$CODEMAP_BIN' query --compact rdeps pkg.core",
            "'${CODEMAP_BIN}' query --compact rdeps pkg.core",
            "echo ';' \"$CODEMAP_BIN\" query --compact rdeps pkg.core",
            'CODEMAP_BIN=/wrong/codemap-py; "$CODEMAP_BIN" query --compact rdeps pkg.core',
            '"$CODEMAP_BIN" query --compact rdeps --help',
        ],
    )
    def test_rejects_unexpanded_or_mutated_launcher_commands(self, runtime: Any, command: str) -> None:
        """Observation requires an executable launcher token, not shell text or help.

        A single-quoted variable never expands, an ``echo`` only mentions the query, a prior assignment swaps the
        executable, and ``--help`` is not a query; none of them is observed.
        """
        parsed = runtime.parse_codex_jsonl(completed_stream(commands=[_command(command)]))

        assert parsed.codemap_observed_calls == 0
        assert parsed.codemap_observed_successful_calls == 0
        assert parsed.codemap_observed_complete_calls == 0

    @pytest.mark.parametrize(
        ("template", "pass_launcher"),
        [
            pytest.param(
                'CODEMAP_BIN=/wrong/codemap-py; "{launcher}" query --compact rdeps pkg.core',
                True,
                id="verified-path-survives-unrelated-reassignment",
            ),
            pytest.param(
                '"$CODEMAP_BIN" query --compact rdeps pkg.core; CODEMAP_BIN=/wrong/codemap-py',
                False,
                id="variable-query-survives-later-reassignment",
            ),
        ],
    )
    def test_survives_reassignment_that_cannot_change_the_executed_query(
        self, runtime: Any, launcher: Path, template: str, pass_launcher: bool
    ) -> None:
        """An exact proven executable stays observable when a variable is changed around it.

        Either the query goes through the verified absolute path, so the reassigned variable is irrelevant, or the
        reassignment happens after the query ran and cannot retroactively change what executed.
        """
        stream = completed_stream(commands=[_command(template.replace("{launcher}", str(launcher)))])

        parsed = runtime.parse_codex_jsonl(stream, launcher_path=launcher if pass_launcher else None)

        assert parsed.codemap_observed_calls == 1
        assert parsed.codemap_observed_successful_calls == 1
        assert parsed.codemap_calls == 0


class TestArmCompliance:
    """An arm complies only with a successful compact query delivered the way that arm requires."""

    @pytest.mark.parametrize(
        ("arm", "command", "expected"),
        [
            pytest.param(
                "B_auto",
                _RDEPS_QUERY,
                {"compliance": True, "codemap_direct_successful_calls": 1, "codemap_skill_successful_calls": 0},
                id="direct-compact-query",
            ),
            pytest.param(
                "B_auto",
                '"$CODEMAP_BIN" query rdeps pkg.core',
                {"compliance": False, "codemap_delivery": "none"},
                id="missing-compact-flag",
            ),
            pytest.param(
                "C_strict",
                _RDEPS_QUERY,
                {"compliance": False, "codemap_direct_successful_calls": 1, "codemap_delivery": "none"},
                id="skill-arm-without-skill-delivery",
            ),
        ],
    )
    def test_requires_successful_compact_delivery_by_arm(
        self, script_run_codex: Any, tmp_path: Path, arm: str, command: str, expected: dict[str, object]
    ) -> None:
        """A query attempt, wrong delivery mode, or missing compact flag cannot comply.

        Each case runs the arm against one synthetic completed query: B needs a compact direct query, and C cannot be
        satisfied by a direct query because it never delivered the Skill.
        """
        task = {"id": "fixture", "prompt": "unchanged prompt", "type": "demo", "scoreable": True}
        runner = script_run_codex.CodexRunner(
            "gpt-6.1-sol",
            tmp_path,
            transport=lambda *_args, **_kwargs: completed_stream(commands=[_command(command)]),
            evaluator=lambda *_args: core.EvaluationResult(scored=True, correct=True, quality_score=1.0),
        )

        result = runner.run(task, arm)

        assert _fields(result, expected) == expected


@POSIX_SECURITY
class TestDirectCliArm:
    """The B arm exposes only the supplied launcher and proves it runs before any model call."""

    def test_never_installs_a_plugin(self, script_run_codex: Any, tmp_path: Path) -> None:
        """B must expose the supplied launcher without using Codex plugin setup.

        The staged copy keeps the launcher bytes and the runtime files it needs, and carries no plugin, skill or shared
        directories, so the arm measures the bare CLI rather than a plugin install.
        """
        launcher = _make_direct_runtime_bundle(tmp_path)
        installer = mock.Mock(return_value=True)

        with script_run_codex.prepare_arm_home(
            "B_auto", root=tmp_path, codemap_bin=launcher, plugin_installer=installer
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

        installer.assert_not_called()

    def test_admission_executes_a_task_shaped_query(self, script_run_codex: Any, tmp_path: Path) -> None:
        """B preflight must execute its staged CLI before any model can consume a cell.

        The probe query named in the manifest runs through the Codex sandbox with the arm's permission profile, so a
        launcher that cannot answer a task-shaped query is rejected before it costs a paid cell.
        """
        repo_path = tmp_path / "target"
        repo_path.mkdir()
        index_path = repo_path / "locked-index.json"
        index_path.write_text("{}", encoding="utf-8")
        home_path = tmp_path / "home"
        launcher = _write_launcher(home_path / "direct-cli" / "bin" / "codemap-py")
        _write_private_config(home_path)
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
        command_runner = mock.Mock(return_value=SimpleNamespace(returncode=0, stdout=_COMPACT_RESULT, stderr=""))

        script_run_codex._admit_staged_direct_cli(
            home, repo_path, index_path, manifest_path=manifest_path, command_runner=command_runner
        )

        command_runner.assert_called_once()
        assert command_runner.call_args.args[0] == [
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

    def test_no_model_probe_removes_its_coordination_root(
        self, script_run_codex: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dry probe must not leave the index reader-coordination skeleton behind.

        The probe prepares a coordination root next to the index and never starts a model cell, so it must tear both
        that root and its arm home down itself.
        """
        home_path = tmp_path / "home"
        _write_private_config(home_path)
        home_path.chmod(0o700)
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
        runner = script_run_codex.CodexRunner("gpt-6.1-sol", tmp_path)
        monkeypatch.setattr(runner, "_prepare_verified_home", lambda _arm: home)

        runner.probe_arm("B_auto")

        assert not coordination_root.exists()
        assert not home_path.exists()


class TestTreatmentArtifactLocks:
    """A staged treatment artifact is accepted only when its bytes match the locked manifest."""

    @staticmethod
    def _lock_document(home: Any, script_run_codex: Any) -> dict[str, Any]:
        """Return a lock document that names the staged launcher and its whole runtime tree."""
        runtime_root = home.codemap_launcher_path.parent.parent
        files = script_run_codex._runtime_file_hashes(runtime_root)
        return {
            "artifact_sha256": {"codemap_runtime_cli": home.codemap_launcher_sha256},
            "codemap_candidate": {"version": "0.27.0"},
            "direct_cli_runtime": {"files": files, "aggregate_sha256": script_run_codex._aggregate_file_hashes(files)},
        }

    @pytest.fixture(name="direct_cli_home")
    def _direct_cli_home(self, script_run_codex: Any, tmp_path: Path) -> Any:
        """Prepare a real B arm home from a source-shaped runtime and release it after the test."""
        launcher = _make_direct_runtime_bundle(tmp_path)
        with script_run_codex.prepare_arm_home("B_auto", root=tmp_path, codemap_bin=launcher) as home:
            yield home

    @POSIX_SECURITY
    def test_accepts_launcher_matching_its_locked_hash(
        self, script_run_codex: Any, direct_cli_home: Any, tmp_path: Path
    ) -> None:
        """B accepts a direct executable whose bytes are the locked runtime launcher.

        The lock names the launcher digest and every runtime file digest, so a staged copy identical to the locked
        runtime passes without raising.
        """
        lock_path = tmp_path / "locks.json"
        lock_path.write_text(json.dumps(self._lock_document(direct_cli_home, script_run_codex)), encoding="utf-8")

        assert script_run_codex._verify_treatment_artifact_locks(direct_cli_home, lock_path) is None

    @POSIX_SECURITY
    def test_rejects_launcher_not_matching_its_locked_hash(
        self, script_run_codex: Any, direct_cli_home: Any, tmp_path: Path
    ) -> None:
        """B rejects a direct executable unless its bytes are the locked runtime launcher.

        A lock recorded for a different launcher digest means the staged executable is not the one the benchmark
        admitted, so the arm must not run.
        """
        lock_path = tmp_path / "locks.json"
        lock_path.write_text(
            json.dumps(
                {"artifact_sha256": {"codemap_runtime_cli": "0" * 64}, "codemap_candidate": {"version": "0.27.0"}}
            ),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="launcher does not match"):
            script_run_codex._verify_treatment_artifact_locks(direct_cli_home, lock_path)

    def test_mismatch_message_explains_safe_refresh(self, script_run_codex: Any) -> None:
        """A stale local treatment lock names the no-model recovery sequence.

        The message must say that no paid call started, name the manifest rebuild command, and warn against reusing the
        previous paid approval, so a stale lock never sends someone straight to a paid rerun.
        """
        message = script_run_codex._treatment_artifact_lock_mismatch_message("codex_rig_adapter")

        assert "codex_rig_adapter" in message
        assert "no paid model call was started" in message
        assert "uv run python benchmarks/build-codex-integration-manifest.py" in message
        assert "Do not reuse the previous --paid-approval value" in message


class TestHistoricalDirectQuery:
    """Diagnostic, multiline and compound B commands remain evidence but never earn direct credit."""

    @pytest.mark.parametrize(
        ("command", "output"),
        [
            pytest.param(
                'printf "ready\\n"; "$CODEMAP_BIN" query --compact rdeps pkg.core; printf "done\\n"',
                "ready\n" + _COMPACT_RESULT + "\ndone\n",
                id="compound-direct-query",
            ),
            pytest.param(
                '/bin/zsh -lc "printf \'CODEMAP_BIN=%s\\\\n\' \\""\'$CODEMAP_BIN"\n'
                '"$CODEMAP_BIN" query --compact fn-rdeps "pkg.core::target"\'',
                "CODEMAP_BIN=/fixture/codemap-py\n" + _COMPACT_RESULT,
                id="exact-paid-multiline-wrapper",
            ),
            pytest.param(
                'printf \'CODEMAP_BIN=%s\\n\' "$CODEMAP_BIN"; "$CODEMAP_BIN" query --compact rdeps pkg.core',
                _COMPACT_RESULT,
                id="printf-echo-then-query",
            ),
            pytest.param(
                'printf ready &&\n"$CODEMAP_BIN" query --compact rdeps pkg.core',
                _COMPACT_RESULT,
                id="and-continuation-newline",
            ),
            pytest.param(
                'false ||\n"$CODEMAP_BIN" query --compact rdeps pkg.core', _COMPACT_RESULT, id="or-continuation-newline"
            ),
            pytest.param(
                'printf ready |\n"$CODEMAP_BIN" query --compact rdeps pkg.core',
                _COMPACT_RESULT,
                id="pipe-continuation-newline",
            ),
            pytest.param(
                'CODEMAP_BIN=/wrong/codemap-py\n"$CODEMAP_BIN" query --compact rdeps pkg.core',
                _COMPACT_RESULT,
                id="wrong-assignment-newline",
            ),
            pytest.param(
                'printf \'CODEMAP_BIN=/wrong\\n\' "$CODEMAP_BIN" "$CODEMAP_BIN" query --compact rdeps pkg.core',
                _COMPACT_RESULT,
                id="printf-doubled-launcher-argument",
            ),
            pytest.param(
                'printf \'CODEMAP_BIN=%s\\n\n"$CODEMAP_BIN" query --compact rdeps pkg.core\' "$CODEMAP_BIN"',
                _COMPACT_RESULT,
                id="printf-format-spanning-newline",
            ),
            pytest.param(
                'printf \'CODEMAP_BIN=%s\\n\' "$CODEMAP_BIN" \\\n"$CODEMAP_BIN" query --compact rdeps pkg.core',
                _COMPACT_RESULT,
                id="backslash-continuation",
            ),
            pytest.param(
                "printf 'CODEMAP_BIN=%s\\n' \"${CODEMAP_BIN-}\"; "
                "rg -n target .; "
                'if [ -n "${CODEMAP_BIN-}" ]; then '
                '"$CODEMAP_BIN" query --compact fn-rdeps "pkg.core::target"; '
                "else printf 'CODEMAP_BIN is unset\\n'; fi",
                _COMPACT_RESULT,
                id="diagnostic-conditional-query",
            ),
        ],
    )
    def test_multiline_or_diagnostic_shell_forms_do_not_comply(
        self, script_run_codex: Any, runtime: Any, command: str, output: str
    ) -> None:
        """No multiline or diagnostic/control shell form is the dedicated future native query item.

        Historical multiline shell forms, a diagnostic that prints the launcher, and a diagnostic/conditional control
        command stay evidence only: none can score a future direct query or satisfy B compliance.
        """
        parsed = runtime.parse_codex_jsonl(completed_stream(commands=[_command(command, output)]))

        assert parsed.codemap_direct_compact_successful_calls == 0
        assert script_run_codex._arm_compliance("B_auto", parsed) is False


class TestSkillDelivery:
    """The C arm's Skill is delivered by the immutable installed binding; reads are audit evidence."""

    @pytest.mark.parametrize(
        ("template", "output_kind", "exit_code", "expected"),
        [
            pytest.param(_SKILL_READ_PLAIN, "complete", 0, True, id="canonical-complete"),
            pytest.param("cat {skill}", "complete", 0, False, id="literal-path"),
            pytest.param("sed -n '1,1p' {skill}", "partial", 0, False, id="partial-output"),
            pytest.param("cat {other}", "complete", 0, False, id="wrong-path"),
            pytest.param(_SKILL_READ_PLAIN, "wrong", 0, False, id="wrong-bytes"),
            pytest.param(_SKILL_READ_PLAIN, "complete", 1, False, id="failed-command"),
            pytest.param("cat $CODEMAP_SKILL_FILE", "complete", 0, False, id="unquoted-variable"),
        ],
    )
    def test_read_requires_exact_environment_command_bytes_and_success(
        self,
        runtime: Any,
        tmp_path: Path,
        template: str,
        output_kind: str,
        exit_code: int,
        expected: bool,
    ) -> None:
        """Only the exact bound reader with exact bytes and zero exit proves activation.

        A literal path, a partial or altered output, another file, a failed exit and an unquoted variable each fail the
        activation check; only ``cat "$CODEMAP_SKILL_FILE"`` that printed every byte and exited zero passes.
        """
        skill = _write_skill(tmp_path, b"# query-code\nline 2\nline 3\n")
        command = template.format(skill=skill.path, other=tmp_path / "other" / "SKILL.md")
        outputs = {
            "complete": skill.text,
            "partial": "# query-code\n",
            "wrong": skill.text.replace("line 2", "changed"),
        }
        item = _command(
            command, outputs[output_kind], status="completed" if exit_code == 0 else "failed", exit_code=exit_code
        )

        parsed = runtime.parse_codex_jsonl(completed_stream(commands=[item]), **skill.binding)

        assert parsed.skill_delivery_observed is expected

    def test_binding_credits_compact_query_without_manual_read(
        self, script_run_codex: Any, runtime: Any, skill: _Skill
    ) -> None:
        """A C treatment is productive when its installed Skill binding guides one exact compact query.

        Manual ``cat "$CODEMAP_SKILL_FILE"`` reads are audit diagnostics, not a prerequisite for counting a query made
        through the immutable installed-Skill treatment. Requiring that extra read would measure benchmark ceremony
        rather than normal integration use.
        """
        parsed = runtime.parse_codex_jsonl(completed_stream(commands=[_command(_RDEPS_QUERY)]), **skill.binding)

        assert parsed.skill_delivery_observed is False
        assert parsed.codemap_calls == 1
        assert parsed.codemap_skill_compact_successful_calls == 1
        assert script_run_codex._arm_compliance("C_strict", parsed) is True

    def test_query_with_incomplete_result_fails_compliance(self, script_run_codex: Any, runtime: Any) -> None:
        """A query whose result lacks ``query_complete`` is a call, but it does not satisfy B compliance."""
        parsed = runtime.parse_codex_jsonl(
            completed_stream(commands=[_command("$CODEMAP_BIN query --compact rdeps pkg.core", "{}")])
        )

        assert parsed.codemap_calls == 1
        assert script_run_codex._arm_compliance("B_auto", parsed) is False

    @pytest.mark.parametrize(
        ("read_template", "output_template"),
        [
            pytest.param("sed -n '1,260p' {skill}; printf 'activated\\n'", "{text}activated\n", id="sed-then-printf"),
            pytest.param(
                "skill_path='{skill}'; wc -l \"$skill_path\"; sed -n '1,260p' \"$skill_path\"",
                "{lines} {skill}\n{text}",
                id="wc-then-sed",
            ),
        ],
    )
    def test_noncanonical_reader_leaves_later_standalone_query_credited(
        self, script_run_codex: Any, runtime: Any, skill: _Skill, read_template: str, output_template: str
    ) -> None:
        """A standalone compact C query remains valid when an earlier reader is noncanonical.

        Reader provenance stays diagnostic while the immutable C binding owns delivery, so a compound or non-``cat``
        reader before the query neither earns Skill credit nor blocks the later query's credit.
        """
        read_output = output_template.format(skill=skill.path, text=skill.text, lines=len(skill.content.splitlines()))
        stream = _read_then_query_stream(
            skill, read_template.format(skill=skill.path), _FN_RDEPS_QUERY, read_output=read_output
        )

        parsed = runtime.parse_codex_jsonl(stream, **skill.binding)

        assert parsed.skill_delivery_observed is False
        assert parsed.codemap_skill_compact_successful_calls == 1
        assert parsed.codemap_direct_successful_calls == 0
        assert script_run_codex._arm_compliance("C_strict", parsed) is True

    def test_compound_compact_query_is_observed_without_canonical_credit(
        self, script_run_codex: Any, runtime: Any, skill: _Skill
    ) -> None:
        """A parsed query segment records use without satisfying standalone credit."""
        command = 'printf "%s\\n" "$CODEMAP_BIN"; "$CODEMAP_BIN" query --compact symbol pkg.Item'

        parsed = runtime.parse_codex_jsonl(completed_stream(commands=[_command(command)]), **skill.binding)

        assert parsed.codemap_observed_calls == 1
        assert parsed.codemap_observed_successful_calls == 1
        assert parsed.codemap_observed_complete_calls == 1
        assert parsed.codemap_calls == 0
        assert parsed.codemap_skill_compact_successful_calls == 0
        assert script_run_codex._arm_compliance("C_strict", parsed) is False


class TestHistoricalSkillDelivery:
    """Historical wrapped, compound and alias C commands stay evidence but cannot earn credit in a new cell."""

    def test_wrapped_launcher_query_after_skill_read_earns_no_credit(
        self, script_run_codex: Any, runtime: Any, launcher: Path, skill: _Skill
    ) -> None:
        """Historical wrapped C evidence stays available but cannot score a new cell.

        The Skill read is observed, yet a query through the transport-wrapped absolute launcher is not the native item,
        so the arm is not compliant.
        """
        stream = _read_then_query_stream(
            skill,
            _SKILL_READ_WRAPPED,
            f"/bin/zsh -lc '\"{launcher}\" query --compact rdeps pkg.core'",
            query_output=_COMPLETE_RESULT,
        )

        parsed = runtime.parse_codex_jsonl(stream, launcher_path=launcher, **skill.binding)

        assert parsed.skill_delivery_observed is True
        assert parsed.codemap_calls == 0
        assert script_run_codex._arm_compliance("C_strict", parsed) is False

    def test_wrapped_direct_query_after_skill_read_is_not_c_compliant(
        self, script_run_codex: Any, runtime: Any, launcher: Path, skill: _Skill
    ) -> None:
        """A wrapped ``$CODEMAP_BIN`` query after an observed Skill read counts as a call but not as C compliance.

        The transport wrapper makes the direct call count, but the C rule needs the Skill and the query as separate
        native items, so the wrapped shape cannot satisfy it.
        """
        stream = _read_then_query_stream(
            skill, _SKILL_READ_WRAPPED, f"/bin/zsh -lc '{_RDEPS_QUERY}'", query_output=_COMPLETE_RESULT
        )

        parsed = runtime.parse_codex_jsonl(stream, launcher_path=launcher, **skill.binding)

        assert parsed.skill_delivery_observed is True
        assert parsed.codemap_calls == 1
        assert script_run_codex._arm_compliance("C_strict", parsed) is False

    def test_bound_launcher_query_is_historical_only(
        self, script_run_codex: Any, runtime: Any, launcher: Path, skill: _Skill
    ) -> None:
        """The paid local-alias shape remains historical-only evidence.

        A query through a local ``codemap_bin`` default-alias is not a standalone native query, so it earns neither
        direct nor Skill credit even though the Skill read is observed.
        """
        query = _BOUND_ALIAS_QUERY.replace("{launcher}", str(launcher))
        stream = _read_then_query_stream(skill, _SKILL_READ_WRAPPED, query)

        parsed = runtime.parse_codex_jsonl(stream, launcher_path=launcher, **skill.binding)

        assert parsed.skill_delivery_observed is True
        assert parsed.codemap_calls == 0
        assert parsed.codemap_direct_successful_calls == 0
        assert script_run_codex._arm_compliance("C_strict", parsed) is False

    def test_query_before_skill_read_cannot_make_the_query_compliant(
        self, script_run_codex: Any, runtime: Any, launcher: Path, skill: _Skill
    ) -> None:
        """The exact Skill read is still observable, but it cannot make the preceding non-canonical query compliant."""
        query = _BOUND_ALIAS_QUERY.replace("{launcher}", str(launcher))
        events = [
            _item_event("query", _command(query)),
            _item_event("skill-read", _command(_SKILL_READ_WRAPPED, skill.text)),
            {"type": "turn.completed"},
        ]

        parsed = runtime.parse_codex_jsonl(_jsonl(events), launcher_path=launcher, **skill.binding)

        assert parsed.skill_delivery_observed is True
        assert parsed.codemap_skill_compact_successful_calls == 0
        assert script_run_codex._arm_compliance("C_strict", parsed) is False

    def test_compound_skill_read_and_control_query_earn_no_credit(
        self, script_run_codex: Any, runtime: Any, launcher: Path, skill: _Skill
    ) -> None:
        """C now requires two dedicated native items rather than compound shell evidence.

        A reader chained with ``printf`` is not an observed delivery, and an ``if`` that selects the launcher is not a
        standalone query, so neither the Skill nor the query is credited.
        """
        read_command = f"sed -n '1,240p' {skill.path}; printf 'activated\\n'"
        query_command = (
            'if [ -n "$CODEMAP_BIN" ]; then "$CODEMAP_BIN" query --compact fn-rdeps "pkg.core::target"; '
            f'else "{launcher}" query --compact fn-rdeps "pkg.core::target"; fi'
        )
        stream = _read_then_query_stream(
            skill,
            read_command,
            query_command,
            read_output=skill.text + "activated\n",
            query_output="diagnostic\n" + _COMPACT_RESULT,
        )

        parsed = runtime.parse_codex_jsonl(stream, launcher_path=launcher, **skill.binding)

        assert parsed.skill_delivery_observed is False
        assert parsed.codemap_calls == 0
        assert parsed.codemap_direct_successful_calls == 0
        assert script_run_codex._arm_compliance("C_strict", parsed) is False

    def test_conditional_launcher_alias_replay_is_not_canonical_compliance(
        self, script_run_codex: Any, runtime: Any, launcher: Path, skill: _Skill
    ) -> None:
        """Record the observed C command as historical replay incompatibility.

        The prospective C contract is the standalone ``$CODEMAP_BIN query`` form. This conditional alias remains corpus
        evidence for interpreting historical rows, not a second command grammar eligible for future compliance credit.
        """
        query = (
            '/bin/zsh -lc \'if [ -n "$CODEMAP_BIN" ]; then CODEMAP_LAUNCHER="$CODEMAP_BIN"; '
            f'else CODEMAP_LAUNCHER="{launcher}"; fi\n'
            '"$CODEMAP_LAUNCHER" query --compact fn-rdeps "pkg.core::target"\''
        )
        stream = _read_then_query_stream(skill, _SKILL_READ_WRAPPED, query)

        parsed = runtime.parse_codex_jsonl(stream, launcher_path=launcher, **skill.binding)

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
                id="other-variable-name",
            ),
            pytest.param(
                'if [ -n "$OTHER" ]; then CODEMAP_LAUNCHER="$CODEMAP_BIN"; '
                'else CODEMAP_LAUNCHER="{launcher}"; fi\n'
                '"$CODEMAP_LAUNCHER" query --compact fn-rdeps "pkg.core::target"',
                id="unrelated-condition-variable",
            ),
            pytest.param(
                'if [ -z "$CODEMAP_BIN" ]; then CODEMAP_LAUNCHER="$CODEMAP_BIN"; '
                'else CODEMAP_LAUNCHER="{launcher}"; fi\n'
                '"$CODEMAP_LAUNCHER" query --compact fn-rdeps "pkg.core::target"',
                id="inverted-test",
            ),
            pytest.param(
                'if [ -n "$CODEMAP_BIN" ]; then CODEMAP_LAUNCHER="/wrong/codemap-py"; '
                'else CODEMAP_LAUNCHER="{launcher}"; fi\n'
                '"$CODEMAP_LAUNCHER" query --compact fn-rdeps "pkg.core::target"',
                id="wrong-then-branch",
            ),
            pytest.param(
                'if [ -n "$CODEMAP_BIN" ]; then CODEMAP_LAUNCHER="$CODEMAP_BIN"; '
                'else CODEMAP_LAUNCHER="/wrong/codemap-py"; fi\n'
                '"$CODEMAP_LAUNCHER" query --compact fn-rdeps "pkg.core::target"',
                id="wrong-else-branch",
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
                id="empty-then-branch",
            ),
            pytest.param(
                'CODEMAP_BIN=/wrong/codemap-py; if [ -n "$CODEMAP_BIN" ]; '
                'then CODEMAP_LAUNCHER="$CODEMAP_BIN"; else CODEMAP_LAUNCHER="{launcher}"; fi\n'
                '"$CODEMAP_LAUNCHER" query --compact fn-rdeps "pkg.core::target"',
                id="prior-wrong-codemap-bin",
            ),
            pytest.param(
                'if [ -n "$CODEMAP_BIN" ]; then CODEMAP_LAUNCHER="$CODEMAP_BIN"; '
                'else CODEMAP_LAUNCHER="{launcher}"; fi; CODEMAP_LAUNCHER=/wrong/codemap-py\n'
                '"$CODEMAP_LAUNCHER" query --compact fn-rdeps "pkg.core::target"',
                id="later-wrong-reassignment",
            ),
            pytest.param(
                'export CODEMAP_LAUNCHER=/wrong/codemap-py; if [ -n "$CODEMAP_BIN" ]; '
                'then CODEMAP_LAUNCHER="$CODEMAP_BIN"; else CODEMAP_LAUNCHER="{launcher}"; fi\n'
                '"$CODEMAP_LAUNCHER" query --compact fn-rdeps "pkg.core::target"',
                id="prior-exported-wrong-launcher",
            ),
            pytest.param(
                'readonly CODEMAP_LAUNCHER=/wrong/codemap-py; if [ -n "$CODEMAP_BIN" ]; '
                'then CODEMAP_LAUNCHER="$CODEMAP_BIN"; else CODEMAP_LAUNCHER="{launcher}"; fi\n'
                '"$CODEMAP_LAUNCHER" query --compact fn-rdeps "pkg.core::target"',
                id="prior-readonly-wrong-launcher",
            ),
            pytest.param(
                'typeset CODEMAP_LAUNCHER=/wrong/codemap-py; if [ -n "$CODEMAP_BIN" ]; '
                'then CODEMAP_LAUNCHER="$CODEMAP_BIN"; else CODEMAP_LAUNCHER="{launcher}"; fi\n'
                '"$CODEMAP_LAUNCHER" query --compact fn-rdeps "pkg.core::target"',
                id="prior-typeset-wrong-launcher",
            ),
            pytest.param(
                'unset CODEMAP_LAUNCHER; if [ -n "$CODEMAP_BIN" ]; then CODEMAP_LAUNCHER="$CODEMAP_BIN"; '
                'else CODEMAP_LAUNCHER="{launcher}"; fi\n'
                '"$CODEMAP_LAUNCHER" query --compact fn-rdeps "pkg.core::target"',
                id="prior-unset",
            ),
            pytest.param(
                'if [ -n "$CODEMAP_BIN" ]; then CODEMAP_LAUNCHER="$(printf %s "$CODEMAP_BIN")"; '
                'else CODEMAP_LAUNCHER="{launcher}"; fi\n'
                '"$CODEMAP_LAUNCHER" query --compact fn-rdeps "pkg.core::target"',
                id="command-substitution-then-branch",
            ),
            pytest.param(
                'if [ -n "$CODEMAP_BIN" ]; then CODEMAP_LAUNCHER="$CODEMAP_BIN"; '
                'else CODEMAP_LAUNCHER="{launcher}"; fi; payload="CODEMAP_LAUNCHER=/wrong/codemap-py"; '
                'eval "$payload"\n"$CODEMAP_LAUNCHER" query --compact fn-rdeps "pkg.core::target"',
                id="eval-payload-reassignment",
            ),
            pytest.param(
                'source /dev/null; if [ -n "$CODEMAP_BIN" ]; then CODEMAP_LAUNCHER="$CODEMAP_BIN"; '
                'else CODEMAP_LAUNCHER="{launcher}"; fi\n'
                '"$CODEMAP_LAUNCHER" query --compact fn-rdeps "pkg.core::target"',
                id="source-prefix",
            ),
            pytest.param(
                'read -r CODEMAP_LAUNCHER <<< /wrong/codemap-py; if [ -n "$CODEMAP_BIN" ]; '
                'then CODEMAP_LAUNCHER="$CODEMAP_BIN"; else CODEMAP_LAUNCHER="{launcher}"; fi\n'
                '"$CODEMAP_LAUNCHER" query --compact fn-rdeps "pkg.core::target"',
                id="prior-read-binding",
            ),
            pytest.param(
                'while false; do CODEMAP_LAUNCHER="$CODEMAP_BIN"; done; if [ -n "$CODEMAP_BIN" ]; '
                'then CODEMAP_LAUNCHER="$CODEMAP_BIN"; else CODEMAP_LAUNCHER="{launcher}"; fi\n'
                '"$CODEMAP_LAUNCHER" query --compact fn-rdeps "pkg.core::target"',
                id="while-loop-prefix",
            ),
            pytest.param(
                '( if [ -n "$CODEMAP_BIN" ]; then CODEMAP_LAUNCHER="$CODEMAP_BIN"; '
                'else CODEMAP_LAUNCHER="{launcher}"; fi )\n'
                '"$CODEMAP_LAUNCHER" query --compact fn-rdeps "pkg.core::target"',
                id="subshell-assignment",
            ),
            pytest.param(
                'if true; then if [ -n "$CODEMAP_BIN" ]; then CODEMAP_LAUNCHER="$CODEMAP_BIN"; '
                'else CODEMAP_LAUNCHER="{launcher}"; fi; fi\n'
                '"$CODEMAP_LAUNCHER" query --compact fn-rdeps "pkg.core::target"',
                id="nested-if",
            ),
            pytest.param(
                'if [ -n "$CODEMAP_BIN" ]; then CODEMAP_LAUNCHER="$CODEMAP_BIN"; '
                '"$CODEMAP_LAUNCHER" query --compact fn-rdeps "pkg.core::target"; '
                'else CODEMAP_LAUNCHER="{launcher}"; fi',
                id="query-inside-then-branch",
            ),
            pytest.param(
                'if [ -n "$CODEMAP_BIN" ]; then CODEMAP_LAUNCHER="$CODEMAP_BIN"; '
                'else CODEMAP_LAUNCHER="{launcher}"; fi; printf ready\n'
                '"$CODEMAP_LAUNCHER" query --compact fn-rdeps "pkg.core::target"',
                id="command-before-query",
            ),
            pytest.param(
                "/bin/zsh -lc '"
                'CODEMAP_BIN="${CODEMAP_BIN:-{launcher}}"; '
                '"$CODEMAP_BIN" query --compact fn-rdeps "pkg.core::target"\'',
                id="zsh-wrapped-launcher-assignment",
            ),
        ],
    )
    def test_conditional_launcher_alias_rejects_unproven_forms(
        self, script_run_codex: Any, runtime: Any, launcher: Path, skill: _Skill, template: str
    ) -> None:
        """Keep conditional alias credit limited to one immutable two-branch form.

        Each case could execute a launcher-like command, so a recognizer that only matches ``CODEMAP_LAUNCHER query``
        would incorrectly satisfy C compliance. A historical assigned launcher, including one inside a transport
        wrapper, is not a future standalone native item either.
        """
        stream = _read_then_query_stream(skill, _SKILL_READ_PLAIN, template.replace("{launcher}", str(launcher)))

        parsed = runtime.parse_codex_jsonl(stream, launcher_path=launcher, **skill.binding)

        assert parsed.skill_delivery_observed is True
        assert parsed.codemap_calls == 0
        assert parsed.codemap_skill_compact_successful_calls == 0
        assert parsed.codemap_direct_successful_calls == 0
        assert script_run_codex._arm_compliance("C_strict", parsed) is False

    @pytest.mark.parametrize(
        ("template", "output_kind"),
        [
            pytest.param("skill_path='{other}'; sed -n '1,260p' \"$skill_path\"; {query}", "complete", id="wrong-path"),
            pytest.param(
                "skill_path='{skill}'; skill_path='{other}'; sed -n '1,260p' \"$skill_path\"; {query}",
                "complete",
                id="reassigned",
            ),
            pytest.param(
                "export skill_path='{skill}'; sed -n '1,260p' \"$skill_path\"; {query}",
                "complete",
                id="export-declaration",
            ),
            pytest.param(
                "typeset skill_path='{skill}'; sed -n '1,260p' \"$skill_path\"; {query}",
                "complete",
                id="shell-declaration",
            ),
            pytest.param(
                "skill_path='{skill}'; read -r skill_path <<< '{other}'; sed -n '1,260p' \"$skill_path\"; {query}",
                "complete",
                id="read-mutation",
            ),
            pytest.param(
                "while :; do skill_path='{skill}'; break; done; sed -n '1,260p' \"$skill_path\"; {query}",
                "complete",
                id="while-binding",
            ),
            pytest.param(
                "if true; then skill_path='{skill}'; fi; sed -n '1,260p' \"$skill_path\"; {query}",
                "complete",
                id="if-binding",
            ),
            pytest.param(
                "until false; do skill_path='{skill}'; break; done; sed -n '1,260p' \"$skill_path\"; {query}",
                "complete",
                id="until-binding",
            ),
            pytest.param(
                "( skill_path='{skill}'; sed -n '1,260p' \"$skill_path\" ); {query}",
                "complete",
                id="subshell-binding",
            ),
            pytest.param(
                "skill_path='{skill}' && sed -n '1,260p' \"$skill_path\"; {query}",
                "complete",
                id="conditional-and-binding",
            ),
            pytest.param(
                "skill_path=\"$(printf '%s' '{skill}')\"; sed -n '1,260p' \"$skill_path\"; {query}",
                "complete",
                id="command-substitution",
            ),
            pytest.param(
                "skill_path='{skill}'; sed -n '1,260p' \"$skill_path\"; {query}", "incomplete", id="incomplete-bytes"
            ),
            pytest.param("skill_path='{skill}'; sed -n '1,260p' \"$skill_path\"; {query}", "wrong", id="wrong-bytes"),
        ],
    )
    def test_bound_skill_reader_forms_earn_no_credit(
        self,
        script_run_codex: Any,
        runtime: Any,
        tmp_path: Path,
        skill: _Skill,
        template: str,
        output_kind: str,
    ) -> None:
        """No compound historical reader/query command can earn direct or C credit.

        A reader and a query joined in one command are never an observed Skill delivery or a compact direct query,
        however the shell variable naming the Skill path was bound, rebound, or corrupted.
        """
        other = _write_skill(tmp_path / "other")
        command = template.format(skill=skill.path, other=other.path, query=_FN_RDEPS_QUERY)
        output = {
            "complete": skill.text,
            "incomplete": "# query-code\n",
            "wrong": skill.text.replace("compact", "expanded"),
        }[output_kind]

        parsed = runtime.parse_codex_jsonl(
            completed_stream(commands=[_command(command, output + _COMPACT_RESULT)]), **skill.binding
        )

        assert parsed.skill_delivery_observed is False
        assert parsed.codemap_skill_successful_calls == 0
        assert parsed.codemap_direct_compact_successful_calls == 0
        assert parsed.codemap_direct_successful_calls == 0
        assert script_run_codex._arm_compliance("C_strict", parsed) is False

    def test_query_then_skill_read_in_one_item_earns_no_credit(
        self, script_run_codex: Any, runtime: Any, tmp_path: Path
    ) -> None:
        """Reading a Skill in the query item cannot satisfy the separate-item C rule."""
        skill = _write_skill(tmp_path, b"# query-code\n")
        command = f"{_RDEPS_QUERY}; cat {skill.path}"
        output = json.dumps({"index": {"query_complete": True}}) + "\n" + skill.text

        parsed = runtime.parse_codex_jsonl(completed_stream(commands=[_command(command, output)]), **skill.binding)

        assert parsed.skill_delivery_observed is False
        assert parsed.codemap_direct_compact_successful_calls == 0
        assert parsed.codemap_skill_compact_successful_calls == 0
        assert script_run_codex._arm_compliance("C_strict", parsed) is False


class _FrozenRun:
    """A completed Codex structural run directory whose inputs, events and metadata are all on disk."""

    def __init__(self, run_dir: Path, model: str) -> None:
        """Write the frozen task suite, Skill snapshot, two-arm telemetry and the metadata that binds them."""
        self.run_dir = run_dir
        snapshot_dir = run_dir / "inputs"
        shared = snapshot_dir / "shared"
        shared.mkdir(parents=True)
        self.task = {
            "id": "SE-01",
            "prompt": "locate pkg.symbol",
            "type": "symbol_extraction",
            "scoreable": True,
            "expected_queries": [{"cmd": "symbols", "args": ["pkg.symbol"]}],
            "ground_truth": {"start_line": 10, "qualified_name": "pkg.symbol"},
        }
        self.tasks_path = shared / "tasks-bench.json"
        tasks_bytes = (json.dumps({"tasks": [self.task]}, sort_keys=True) + "\n").encode()
        self.tasks_path.write_bytes(tasks_bytes)
        self.skill_path = snapshot_dir / "C_strict" / "codemap-py" / "codex-skills" / "query-code" / "SKILL.md"
        self.skill_path.parent.mkdir(parents=True)
        skill_bytes = b"# query-code\nUse the compact query.\n"
        self.skill_path.write_bytes(skill_bytes)
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
        self.snapshot_path = snapshot_dir / "input-snapshot.json"
        snapshot_bytes = (json.dumps(snapshot_payload, sort_keys=True) + "\n").encode()
        self.snapshot_path.write_bytes(snapshot_bytes)
        self.telemetry_path = run_dir / "telemetry.jsonl"
        self.direct_row = {
            "task_id": "SE-01",
            "task_type": "symbol_extraction",
            "arm": "B_auto",
            "repetition": 1,
            "model": model,
            "scoreable": True,
            "output_text": "stale answer",
            "codemap_semantic_compliance": False,
            "task_query_fitness": 0.25,
            "raw_events": [
                {"type": "thread.started", "thread_id": "frozen-thread"},
                _item_event("answer", {"type": "agent_message", "text": "start_line: 10"}),
                _item_event("query", _command("$CODEMAP_BIN query --compact symbols pkg.symbol", _COMPACT_RESULT)),
                {"type": "turn.completed", "status": "completed"},
            ],
        }
        skill_row = {
            **self.direct_row,
            "arm": "C_strict",
            "raw_events": [
                {"type": "thread.started", "thread_id": "frozen-skill-thread"},
                _item_event("skill", _command(_SKILL_READ_PLAIN, skill_bytes.decode())),
                _item_event("query", _command('"$CODEMAP_BIN" query --compact symbols pkg.symbol', _COMPACT_RESULT)),
                _item_event("answer", {"type": "agent_message", "text": "start_line: 10"}),
                {"type": "turn.completed", "status": "completed"},
            ],
        }
        self.telemetry_path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in (self.direct_row, skill_row)), encoding="utf-8"
        )
        self.metadata_path = run_dir / "run-metadata.json"
        self.metadata: dict[str, Any] = {
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
            "inputs": {
                "snapshot": {"path": str(self.snapshot_path), "sha256": hashlib.sha256(snapshot_bytes).hexdigest()}
            },
            "artifacts": {
                "telemetry_jsonl": str(self.telemetry_path),
                "telemetry_sha256": hashlib.sha256(self.telemetry_path.read_bytes()).hexdigest(),
            },
        }
        self._write_metadata()

    @property
    def source_paths(self) -> tuple[Path, ...]:
        """Return every on-disk input a rescore must read without modifying."""
        return (self.telemetry_path, self.metadata_path, self.snapshot_path, self.tasks_path, self.skill_path)

    def _write_metadata(self) -> None:
        """Persist the current metadata document."""
        self.metadata_path.write_text(json.dumps(self.metadata, sort_keys=True) + "\n", encoding="utf-8")

    def corrupt_telemetry(self) -> None:
        """Replace the telemetry so its bytes no longer match the recorded hash."""
        self.telemetry_path.write_text("{}\n", encoding="utf-8")

    def mark_running(self) -> None:
        """Record the run as still running instead of completed."""
        self.metadata["status"] = "running"
        self._write_metadata()

    def select_unknown_task(self) -> None:
        """Select a task id that is absent from the frozen suite."""
        self.metadata["execution"]["selected_task_ids"] = ["not-in-frozen-suite"]
        self._write_metadata()

    def drop_second_arm(self) -> None:
        """Keep only the first arm's telemetry, with its hash updated so the file itself looks intact."""
        self.telemetry_path.write_text(json.dumps(self.direct_row, sort_keys=True) + "\n", encoding="utf-8")
        self.metadata["artifacts"]["telemetry_sha256"] = hashlib.sha256(self.telemetry_path.read_bytes()).hexdigest()
        self._write_metadata()


@pytest.fixture(name="frozen_run")
def _frozen_run(script_run_codex: Any, tmp_path: Path) -> _FrozenRun:
    """Return a completed two-arm run directory ready for offline rescoring."""
    return _FrozenRun(tmp_path / "run", script_run_codex.PARITY_CODEX_MODEL)


class TestRescoreResults:
    """Offline rescore derives corrected fields from frozen inputs without model credentials."""

    def test_replay_is_idempotent_and_leaves_run_artifacts_untouched(
        self, script_run_codex: Any, frozen_run: _FrozenRun
    ) -> None:
        """Rescoring twice yields the same artifact and never mutates the frozen run's inputs.

        The run directory is a record of a paid run, so rescoring may only add its own artifact; repeating it must land
        on the same path with identical bytes.
        """
        before = {path: path.read_bytes() for path in frozen_run.source_paths}

        artifact_path = script_run_codex.rescore_results(frozen_run.run_dir)
        first_bytes = artifact_path.read_bytes()
        repeated_path = script_run_codex.rescore_results(frozen_run.run_dir)

        assert repeated_path == artifact_path
        assert artifact_path.read_bytes() == first_bytes
        assert {path: path.read_bytes() for path in frozen_run.source_paths} == before

    def test_derives_corrected_rows_from_frozen_events(self, script_run_codex: Any, frozen_run: _FrozenRun) -> None:
        """Stale stored fields are recomputed from the frozen raw events and locked suite.

        The stored rows carry a stale answer and old compliance fields; the rescore must replace them with values
        derived from the replayed events and drop the superseded keys.
        """
        artifact = json.loads(script_run_codex.rescore_results(frozen_run.run_dir).read_bytes())

        rows = {row["arm"]: row for row in artifact["rows"]}
        assert artifact["schema_version"] == "codex-structural-offline-rescore-v2"
        assert (
            artifact["source"]["telemetry_sha256"] == hashlib.sha256(frozen_run.telemetry_path.read_bytes()).hexdigest()
        )
        assert artifact["source"]["frozen_suite_semantic_sha256"] == core.semantic_suite_hash([frozen_run.task])
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

    @pytest.mark.parametrize(
        ("damage", "match"),
        [
            pytest.param(_FrozenRun.corrupt_telemetry, "telemetry hash mismatch", id="telemetry-hash-mismatch"),
            pytest.param(_FrozenRun.mark_running, "requires completed", id="run-not-completed"),
            pytest.param(_FrozenRun.select_unknown_task, "task scope", id="task-outside-frozen-suite"),
            pytest.param(_FrozenRun.drop_second_arm, "telemetry is incomplete", id="missing-arm-telemetry"),
        ],
    )
    def test_rejects_run_that_cannot_be_replayed(
        self, script_run_codex: Any, frozen_run: _FrozenRun, damage: Callable[[_FrozenRun], None], match: str
    ) -> None:
        """A run whose telemetry, status, scope or coverage is unusable is refused rather than rescored.

        Each case damages one property the replay depends on, so a rescore can never present results derived from a
        record that is tampered with, unfinished, out of scope or missing an arm.
        """
        damage(frozen_run)

        with pytest.raises(ValueError, match=match):
            script_run_codex.rescore_results(frozen_run.run_dir)


class TestMain:
    """The structural runner's entry point honors an explicitly selected manifest."""

    def test_threads_explicit_manifest_path_into_task_loading_and_ordering(
        self, script_run_codex: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A future relock must not be silently replaced by the module-global manifest.

        A dry run is given a copy of the manifest at another path; task loading, arm ordering and the runner must all
        receive that path instead of the default one.
        """
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

        monkeypatch.setattr(codex_tasks, "load_tasks_with_provenance", _load_tasks)
        monkeypatch.setattr(codex_manifest, "_read_manifest_revision", lambda path: str(path))
        monkeypatch.setattr(codex_arms, "deterministic_arm_order", _order)
        monkeypatch.setattr(codex_structural_runner, "CodexRunner", FixtureRunner)

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
