"""No-model contract tests for the Claude ReadCrop adapter.

Covers the no-model planning entry points, the strict-treatment prompt, the stream-event parser that scores and
quarantines ReadCrop cells, and the Fix-Multi scope binding shared with Codex. Events are built from small helpers so
each test shows only the tool calls that make its scenario distinct.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_TASK = {
    "id": "RC-fixture",
    "type": "read_crop",
    "prompt": "Describe Example.method.",
    "symbol": "Example.method",
    "expected_keywords": ["value"],
}
_SOURCE = "def method(self, value: int) -> None:\n    pass\n"
_ANSWER = (
    "BEGIN_READ_CROP_JSON\n"
    '{"signature":"Example.method(self, value: int) -> None","parameters":["self","value"],'
    '"behavior":"Records the supplied value."}\nEND_READ_CROP_JSON'
)
_WORKSPACE = Path("/disposable/repository")
_SKILL_INPUT = {"skill": "codemap-py:query-code", "args": "symbol Example.method"}


def _write_readcrop_sources(repo_path: Path) -> None:
    """Write minimal source modules containing the six symbols consumed by the read-crop suite.

    >>> from tempfile import TemporaryDirectory
    >>> with TemporaryDirectory() as directory:
    ...     root = Path(directory)
    ...     _write_readcrop_sources(root)
    ...     sorted(path.name for path in root.rglob("*.py"))
    ['early_stopping.py', 'fsdp.py', 'model_checkpoint.py', 'module.py', 'trainer.py']
    """
    sources = {
        "lightning.pytorch.core.module": "class LightningModule:\n    def log(self, value, prog_bar=False, logger=True, on_step=None, on_epoch=None, reduce_fx='mean', sync_dist=False, batch_size=None, rank_zero_only=False, add_dataloader_idx=True, metric_attribute=None, enable_graph=False):\n        pass\n",
        "lightning.pytorch.trainer.trainer": "class Trainer:\n    def fit(self, model, train_dataloaders=None, val_dataloaders=None, datamodule=None, ckpt_path=None):\n        pass\n\n    def __init__(self, enable_checkpointing=True, val_check_interval=1.0, check_val_every_n_epoch=1, accumulate_grad_batches=1, fast_dev_run=False, detect_anomaly=False):\n        pass\n",
        "lightning.pytorch.callbacks.early_stopping": "class EarlyStopping:\n    def __init__(self, monitor='x', min_delta=0.0, patience=3, mode='min', stopping_threshold=None, divergence_threshold=None, check_finite=True, check_on_train_epoch_end=None):\n        pass\n",
        "lightning.pytorch.callbacks.model_checkpoint": "class ModelCheckpoint:\n    def _save_checkpoint(self, trainer, filepath):\n        pass\n",
        "lightning.pytorch.strategies.fsdp": "class FSDPStrategy:\n    def setup(self, trainer):\n        pass\n",
    }
    for module, source in sources.items():
        path = repo_path / Path(*module.split(".")).with_suffix(".py")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")


def _tool_use(tool_id: str, name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    """Build one assistant ``tool_use`` content block.

    >>> _tool_use("q", "Bash", {"command": "ls"})["name"]
    'Bash'
    """
    return {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}


def _tool_result(tool_id: str, content: str, *, is_error: bool = False) -> dict[str, Any]:
    """Build one user ``tool_result`` content block answering ``tool_id``.

    >>> _tool_result("q", "{}", is_error=True)["is_error"]
    True
    """
    return {"type": "tool_result", "tool_use_id": tool_id, "is_error": is_error, "content": content}


def _assistant(*blocks: dict[str, Any]) -> dict[str, Any]:
    """Wrap content blocks as one assistant stream event.

    >>> _assistant({"type": "text", "text": "hi"})["message"]["content"]
    [{'type': 'text', 'text': 'hi'}]
    """
    return {"type": "assistant", "message": {"content": list(blocks)}}


def _user(*results: dict[str, Any]) -> dict[str, Any]:
    """Wrap tool results as one user stream event.

    >>> _user(_tool_result("q", "{}"))["type"]
    'user'
    """
    return {"type": "user", "message": {"content": list(results)}}


def _answer_block() -> dict[str, Any]:
    """Build the assistant text block carrying the fixture's strict read-crop answer envelope."""
    return {"type": "text", "text": _ANSWER}


def _result_event(usage: dict[str, int] | None = None) -> dict[str, Any]:
    """Build a successful terminal ``result`` event with optional token usage.

    >>> _result_event()["usage"], _result_event({"output_tokens": 1})["usage"]
    ({}, {'output_tokens': 1})
    """
    return {"type": "result", "subtype": "success", "usage": usage or {}}


def _skill_and_query_events(command: str) -> list[dict[str, Any]]:
    """Build a Skill launch plus one Bash query running ``command``, both completed, then the answer.

    >>> [event["type"] for event in _skill_and_query_events("codemap-py query symbol X")]
    ['assistant', 'user', 'result']
    """
    return [
        _assistant(
            _tool_use("skill", "Skill", _SKILL_INPUT), _tool_use("query", "Bash", {"command": command}), _answer_block()
        ),
        _user(_tool_result("skill", "started"), _tool_result("query", "{}")),
        _result_event(),
    ]


@pytest.fixture(name="contract")
def _contract(script_run_agentic: Any) -> Any:
    """Frozen read-crop contract for the ``Example.method`` fixture task."""
    return script_run_agentic.build_readcrop_contract(dict(_TASK), source=_SOURCE)


class TestReadcropPlanning:
    """The no-model planning surface: dry-run plan, strict prompt and scope hash."""

    def test_dry_run_plans_all_locked_cells_without_a_claude_call(
        self, tmp_path: Path, capsys: Any, script_run_agentic: Any
    ) -> None:
        """The no-model entry point binds the shared six-task suite to all A/B/C arms.

        Regression: ReadCrop was only a legacy agentic task type, so no immutable shared-contract plan or scope could be
        reviewed before spending. Six tasks times three arms must therefore be planned, with a SCOPE line, and no model
        call made.
        """
        _write_readcrop_sources(tmp_path)

        script_run_agentic.main(repo_path=tmp_path, study="readcrop", dry_run=True)

        output = capsys.readouterr().out
        assert output.count("PLAN    RC-") == 18
        assert "PLAN    RC-06  rep=1  C_strict" in output
        assert "SCOPE   " in output

    def test_strict_prompt_uses_installed_skill_namespace_and_exact_symbol_query(self, script_run_agentic: Any) -> None:
        """The strict Claude treatment prompt names the installed Codemap Skill and the exact symbol query.

        The C arm is only a valid treatment when its prompt invokes the plugin-namespaced Skill with the task's symbol,
        so an unqualified or paraphrased query would silently weaken the integration under test.
        """
        prompt = script_run_agentic.readcrop_prompt(
            "C_strict", {"symbol": "Example.method", "prompt": "Describe Example.method."}
        )

        assert "/codemap-py:query-code symbol Example.method" in prompt

    def test_scope_is_deterministic_and_source_bound(self, script_run_agentic: Any) -> None:
        """Scope approval changes when a frozen source contract changes.

        Two task lists that differ only in the source hash must yield different scope digests; otherwise an approval
        granted for one source snapshot would also cover another.
        """
        first = {
            "id": "RC-01",
            "contract": SimpleNamespace(task_id="RC-01", oracle_sha256="a" * 64, source_sha256="b" * 64),
        }
        changed = {
            "id": "RC-01",
            "contract": SimpleNamespace(task_id="RC-01", oracle_sha256="a" * 64, source_sha256="c" * 64),
        }

        first_scope = script_run_agentic.resolve_readcrop_scope([first])
        second_scope = script_run_agentic.resolve_readcrop_scope([changed])

        assert first_scope["total_cells"] == 3
        assert first_scope["scope_sha256"] != second_scope["scope_sha256"]
        assert json.loads(json.dumps(first_scope))["arms"] == ["A_plain", "B_auto", "C_strict"]


class TestParseClaudeReadcropEvents:
    """Score a Claude stream and decide treatment compliance, contamination and pooling eligibility."""

    def test_scores_strict_envelope_without_inventing_tool_tokens(self, script_run_agentic: Any, contract: Any) -> None:
        """Claude-native usage stays intact when tool-result token usage is unavailable.

        Regression: a generic output scorer accepted prose-only answers and could manufacture tiktoken estimates
        despite Claude not emitting this metric. A correct envelope with plain usage must score as a success whose
        tool-result token count stays unknown rather than estimated.
        """
        usage = {"input_tokens": 10, "cache_creation_input_tokens": 2, "cache_read_input_tokens": 1, "output_tokens": 3}
        events = [_assistant(_answer_block()), _result_event(usage)]

        row = script_run_agentic.parse_claude_readcrop_events(events, arm="A_plain", contract=contract)

        assert row["success"] is True
        assert row["primary_correct"] is True
        assert row["input_tokens"] == 13
        assert row["cached_input_tokens"] == 3
        assert row["tool_result_tokens"] is None
        assert row["compliance"] is True
        assert row["provider_binding"] == dict(contract.provider_binding())

    def test_rejects_prose_and_flags_strict_arm_noncompliance(self, script_run_agentic: Any, contract: Any) -> None:
        """Missing JSON and an unused C treatment remain distinct diagnosable failures.

        Regression: formatting failures could receive keyword credit, while C could be reported compliant without a
        Claude Codemap tool invocation. A prose-only answer on the strict arm must fail with the envelope diagnosis.
        """
        events = [_assistant({"type": "text", "text": "value only"}), _result_event()]

        row = script_run_agentic.parse_claude_readcrop_events(events, arm="C_strict", contract=contract)

        assert row["success"] is False
        assert row["answer_error"] == "missing strict read-crop answer envelope"
        assert row["compliance"] is False

    @pytest.mark.parametrize(
        ("arm", "events", "expected"),
        [
            pytest.param(
                "C_strict",
                [_assistant(_answer_block()), _result_event({"input_tokens": 1, "output_tokens": 1})],
                {"primary_correct": True, "compliance": False, "success": False},
                id="strict-arm-without-codemap",
            ),
            pytest.param(
                "A_plain",
                [
                    _assistant(
                        {"type": "tool_use", "name": "Skill", "input": {"skill": "codemap-py:query-code"}},
                        _answer_block(),
                    ),
                    _result_event({"input_tokens": 1, "output_tokens": 1}),
                ],
                {"primary_correct": True, "contaminated": True, "success": False},
                id="plain-arm-with-codemap-skill",
            ),
        ],
    )
    def test_invalid_codemap_assignment_is_primary_ineligible(
        self, script_run_agentic: Any, contract: Any, arm: str, events: list[dict[str, Any]], expected: dict[str, Any]
    ) -> None:
        """A valid answer cannot rescue an invalid A/C treatment assignment.

        Regression: the parser exposed a false C ``success`` when the answer was correct but no Codemap query
        occurred; this could pool a treatment failure with valid answer-quality evidence. The same holds for an A cell
        that used Codemap, which is contaminated.
        """
        row = script_run_agentic.parse_claude_readcrop_events(events, arm=arm, contract=contract)

        assert {key: row[key] for key in expected} == expected

    @pytest.mark.parametrize(
        ("is_error", "content", "expected"),
        [
            pytest.param(
                True,
                "command unavailable",
                {"codemap_calls": 1, "codemap_successful_calls": 0, "compliance": False, "success": False},
                id="errored-query",
            ),
            pytest.param(
                False,
                "query completed",
                {
                    "codemap_successful_calls": 1,
                    "strict_query_conformance": False,
                    "compliance": False,
                    "success": False,
                },
                id="completed-non-canonical-query",
            ),
        ],
    )
    def test_requires_successful_codemap_result_not_a_mere_attempt(
        self, script_run_agentic: Any, contract: Any, is_error: bool, content: str, expected: dict[str, Any]
    ) -> None:
        """C-strict eligibility depends on completed query evidence, not a tool-use block.

        Regression: treating an attempted or errored Codemap call as C compliance would pool a treatment that never
        received the frozen graph answer. An errored call counts as an attempt only, and a completed call that is not
        the canonical query still does not make the cell compliant.
        """
        attempt = _tool_use("attempt", "Bash", {"command": "codemap-py query refs Example.method"})
        events = [
            _assistant(attempt, _answer_block()),
            _user(_tool_result("attempt", content, is_error=is_error)),
            _result_event({"input_tokens": 1, "output_tokens": 1}),
        ]

        row = script_run_agentic.parse_claude_readcrop_events(events, arm="C_strict", contract=contract)

        assert {key: row[key] for key in expected} == expected

    @pytest.mark.parametrize(
        ("events", "expected"),
        [
            pytest.param(
                [
                    _assistant(_tool_use("skill", "Skill", _SKILL_INPUT), _answer_block()),
                    _user(_tool_result("skill", "started")),
                    _result_event(),
                ],
                {"codemap_successful_calls": 0, "compliance": False, "pooling_eligible": False},
                id="skill-wrapper-only",
            ),
            pytest.param(
                [
                    _assistant(
                        _tool_use("query", "Bash", {"command": "codemap-py query symbol Example.method 2>/dev/null"}),
                        _answer_block(),
                    ),
                    _user(_tool_result("query", "{}")),
                    _result_event(),
                ],
                {"codemap_successful_calls": 1, "codemap_query_skill_launches": 0, "compliance": False},
                id="direct-query-without-skill",
            ),
            pytest.param(
                _skill_and_query_events("codemap-py query symbol Example.method"),
                {
                    "successful_query_arguments": [["symbol", "Example.method"]],
                    "codemap_query_skill_launches": 1,
                    "compliance": True,
                    "pooling_eligible": True,
                },
                id="skill-plus-canonical-query",
            ),
        ],
    )
    def test_credits_only_completed_underlying_cli_queries(
        self, script_run_agentic: Any, contract: Any, events: list[dict[str, Any]], expected: dict[str, Any]
    ) -> None:
        """A successful Skill launch cannot stand in for the frozen query result.

        Regression: the Claude artifact recorded a successful ``Skill`` wrapper despite the wrapped command being
        permission-denied. The decision-grade C arm must therefore observe its completed ``codemap-py query`` or legacy
        ``scan-query`` Bash call with the canonical arguments, launched through the Skill.
        """
        row = script_run_agentic.parse_claude_readcrop_events(events, arm="C_strict", contract=contract)

        assert {key: row[key] for key in expected} == expected

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            pytest.param(
                "/opt/codemap-py/bin/codemap-py query --compact symbol Example.method",
                {
                    "successful_query_arguments": [["symbol", "Example.method"]],
                    "outside_workspace_paths": [],
                    "pooling_eligible": True,
                },
                id="absolute-launcher",
            ),
            pytest.param(
                '"/opt/codemap-py/bin/codemap-py" query --compact symbol Example.method',
                {
                    "successful_query_arguments": [["symbol", "Example.method"]],
                    "outside_workspace_paths": [],
                    "pooling_eligible": True,
                },
                id="quoted-launcher",
            ),
            pytest.param(
                '"${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/codemap-py" query --compact symbol Example.method',
                {
                    "successful_query_arguments": [["symbol", "Example.method"]],
                    "outside_workspace_paths": [],
                    "pooling_eligible": True,
                },
                id="plugin-root-variable-launcher",
            ),
            pytest.param(
                'cd /opt/codemap-py && "/opt/codemap-py/bin/codemap-py" query --compact symbol Example.method',
                {"outside_workspace_paths": ["/opt/codemap-py"], "contaminated": True, "pooling_eligible": False},
                id="escaped-cd-into-launcher-parent",
            ),
        ],
    )
    def test_accepts_only_installable_launcher_outside_worktree(
        self, script_run_agentic: Any, contract: Any, command: str, expected: dict[str, Any]
    ) -> None:
        """Installable launcher forms are valid, but their parent directory is not.

        Regression: the production Skill invokes its absolute ``bin/codemap-py`` launcher without changing cwd.
        Treating that path as source contamination rejects a valid integration; exempting the parent would hide an
        escaped ``cd`` and stale index lookup.
        """
        row = script_run_agentic.parse_claude_readcrop_events(
            _skill_and_query_events(command), arm="C_strict", contract=contract, workspace_root=_WORKSPACE
        )

        assert {key: row[key] for key in expected} == expected

    def test_quarantines_external_paths_and_frozen_index_recovery(self, script_run_agentic: Any, contract: Any) -> None:
        """External source reads and attempts to rebuild the frozen index cannot pool.

        Regression: RC-02 read an unrelated checkout after a stale-index recovery attempt, while the result still
        appeared as a valid C treatment. A completed read outside the workspace and a ``scan --incremental`` rebuild
        must both mark the cell contaminated.
        """
        events = [
            _assistant(
                _tool_use("query", "Bash", {"command": "codemap-py query symbol Example.method"}),
                _tool_use("rebuild", "Bash", {"command": "codemap-py scan --incremental"}),
                _tool_use("read", "Read", {"file_path": "/outside/repository/module.py"}),
                _answer_block(),
            ),
            _user(_tool_result("query", "{}"), _tool_result("rebuild", "built"), _tool_result("read", "source")),
            _result_event(),
        ]

        row = script_run_agentic.parse_claude_readcrop_events(
            events, arm="C_strict", contract=contract, workspace_root=_WORKSPACE
        )

        assert row["outside_workspace_paths"] == ["/outside/repository/module.py"]
        assert row["frozen_index_recovery_attempted"] is True
        assert row["contaminated"] is True
        assert row["pooling_eligible"] is False

    def test_records_but_does_not_quarantine_a_denied_external_read(
        self, script_run_agentic: Any, contract: Any
    ) -> None:
        """A denied path guess cannot leak source bytes or invalidate an otherwise clean cell.

        Regression: the paid Claude calibration marked A/B cells contaminated from permission-denied absolute Reads
        before they found the disposable source. The denied path is recorded as an attempt only.
        """
        events = [
            _assistant(
                _tool_use("denied-read", "Read", {"file_path": "/outside/repository/module.py"}), _answer_block()
            ),
            _user(_tool_result("denied-read", "Permission denied", is_error=True)),
            _result_event(),
        ]

        row = script_run_agentic.parse_claude_readcrop_events(
            events, arm="A_plain", contract=contract, workspace_root=_WORKSPACE
        )

        assert row["attempted_outside_workspace_paths"] == ["/outside/repository/module.py"]
        assert row["outside_workspace_paths"] == []
        assert row["contaminated"] is False
        assert row["pooling_eligible"] is True


def test_external_path_evidence_ignores_relative_find_glob(script_run_agentic: Any, tmp_path: Path) -> None:
    """A successful workspace-relative ``find -path`` glob is not an absolute access.

    Regression: the RC-01/B paid cell was quarantined because the scanner read the slash in ``*/package/module/*`` as an
    absolute ``/package/module/*``.
    """
    command = 'find . -type f -name "module.py" -path "*/lightning/pytorch/core/*"'
    events = [
        _assistant(_tool_use("find", "Bash", {"command": command})),
        _user(_tool_result("find", "./src/lightning/pytorch/core/module.py")),
    ]

    attempted, successful = script_run_agentic._outside_workspace_path_evidence(events, tmp_path)

    assert attempted == []
    assert successful == []


def test_stage_row_reports_only_successful_codemap_queries(script_run_agentic: Any) -> None:
    """The human row never reports Codemap use when every query was denied.

    A C cell whose only Codemap call was denied has ``codemap_attempted`` true but zero successful calls, so the
    terminal row must show the Codemap marker as failed rather than implying the treatment ran.
    """
    row = {
        "success": False,
        "task_id": "RC-01",
        "arm": "C_strict",
        "input_tokens": 10,
        "output_tokens": 2,
        "command_calls": 1,
        "elapsed_s": 1.0,
        "quality_score": 1.0,
        "study": "readcrop",
        "primary_correct": True,
        "codemap_used": False,
        "codemap_attempted": True,
        "codemap_successful_calls": 0,
    }

    rendered = script_run_agentic._format_claude_stage_row(row, completed=1, total=3)

    assert "correct=✓ codemap=✗" in rendered


class TestFixMulti:
    """Claude's Fix-Multi planning shares its task bytes, oracle and scorer binding with Codex."""

    def test_dry_run_reuses_shared_contract_without_a_claude_call(self, capsys: Any, script_run_agentic: Any) -> None:
        """Claude planning binds the same multi-caller task bytes and scorer hashes as Codex.

        Regression: Claude's historical Fix-Multi path used diagnostic keyword recall, so an adapter could silently
        disagree with Codex executable scoring. Three tasks times three arms must be planned with a SCOPE line.
        """
        script_run_agentic.main(study="fix-multi", dry_run=True)

        output = capsys.readouterr().out
        assert output.count("PLAN    FM-") == 9
        assert "PLAN    FM-03  rep=1  C_strict" in output
        assert "SCOPE   " in output

    def test_scope_changes_when_a_shared_oracle_hash_changes(self, script_run_agentic: Any) -> None:
        """The Claude scope records shared scorer and oracle fields rather than keyword heuristics.

        Two scopes whose provider bindings differ only in the oracle hash must digest differently, otherwise a changed
        oracle could run under an approval granted for the old one.
        """
        binding = {
            "canonical_task_sha256": "a" * 64,
            "prompt_sha256": "b" * 64,
            "baseline_commit": "c" * 40,
            "oracle_sha256": "d" * 64,
            "scorer_sha256": "e" * 64,
        }
        first = {"task": {}, "contract": SimpleNamespace(task_id="FM-01", provider_binding=lambda: binding)}
        second = {
            "task": {},
            "contract": SimpleNamespace(
                task_id="FM-01", provider_binding=lambda: {**binding, "oracle_sha256": "f" * 64}
            ),
        }

        first_scope = script_run_agentic.resolve_claude_fix_multi_scope([first])
        second_scope = script_run_agentic.resolve_claude_fix_multi_scope([second])

        assert first_scope["total_cells"] == 3
        assert first_scope["scope_sha256"] != second_scope["scope_sha256"]
