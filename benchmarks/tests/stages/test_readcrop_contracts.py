"""Regression tests for the shared read-crop benchmark contract.

The read-crop contract binds a task to the frozen source of one symbol, parses a labelled JSON answer, and scores it
against the source parameters and optional behaviour facts. No provider or model is involved.
"""

from __future__ import annotations

from typing import Any

import pytest

from benchmarks._bench_common import readcrop_contracts as contracts

_SOURCE = "def method(self, value: int, *, flag: bool = False) -> None:\n    pass\n"


def _task() -> dict[str, object]:
    """Build a fresh read-crop task requiring both source parameter names.

    >>> task = _task()
    >>> task["symbol"], task["expected_keywords"]
    ('Example.method', ['value', 'flag'])
    >>> task["expected_keywords"].clear()
    >>> _task()["expected_keywords"]
    ['value', 'flag']
    """
    return {
        "id": "RC-fixture",
        "type": "read_crop",
        "prompt": "Describe `Example.method`.",
        "primary_module": "package.module",
        "symbol": "Example.method",
        "expected_keywords": ["value", "flag"],
    }


class TestReadcropAnswer:
    """The labelled answer must be complete before it can be scored."""

    def test_rejects_answer_missing_a_required_field(self) -> None:
        """An answer without ``parameters`` and ``behavior`` is refused rather than partially scored.

        A model that names only the signature must not receive partial credit through a lenient parser.
        """
        with pytest.raises(ValueError, match="missing required answer field"):
            contracts.parse_readcrop_answer('{"signature": "method(value)"}')

    def test_complete_symbol_answer_scores_fully(self) -> None:
        """A signature, every source parameter, and a behavior statement earn full quality.

        Without required behavior facts, the behavior protocol is valid but its fact recall and correctness stay
        unscored (``None``) instead of defaulting to a pass.
        """
        contract = contracts.build_readcrop_contract(_task(), source=_SOURCE)
        answer = contracts.parse_readcrop_answer(
            '{"signature": "Example.method(self, value: int, *, flag: bool = False) -> None", '
            '"parameters": ["value", "flag"], "behavior": "Does the requested work."}'
        )

        score = contracts.score_readcrop_answer(contract, answer)

        assert score.primary_correct is True
        assert score.behavior_protocol_valid is True
        assert score.behavior_fact_recall is None
        assert score.behavior_facts_correct is None
        assert score.quality_score == 1.0
        assert score.keyword_recall == 1.0


class TestFocusedContract:
    """Focused parameters and behavior facts narrow the reviewed task contract."""

    @pytest.fixture(name="focused_contract")
    def _focused_contract(self) -> contracts.ReadcropContract:
        """Build a contract requiring only ``flag``, with the behavior fact that it acts as a switch."""
        task = {
            **_task(),
            "required_parameters": ["flag"],
            "required_behavior_facts": [{"parameter": "flag", "terms": ["switch"]}],
        }
        return contracts.build_readcrop_contract(task, source=_SOURCE)

    def test_records_source_and_required_parameter_names(self, focused_contract: contracts.ReadcropContract) -> None:
        """The contract keeps every source parameter but requires only the focused one.

        Scoring against the focused subset must not forget which parameters the frozen source really declares.
        """
        assert focused_contract.source_parameter_names == ("value", "flag")
        assert focused_contract.required_parameter_names == ("flag",)

    @pytest.mark.parametrize(
        ("behavior", "recall", "facts_correct", "primary_correct", "quality"),
        [
            pytest.param("Nonsense context.", 0.0, False, False, pytest.approx(2 / 3), id="empty-context-claim"),
            pytest.param("Controls the switch.", 1.0, True, True, 1.0, id="behavior-fact-covered"),
        ],
    )
    def test_behavior_facts_decide_primary_correctness(
        self,
        focused_contract: contracts.ReadcropContract,
        behavior: str,
        recall: float,
        facts_correct: bool,
        primary_correct: bool,
        quality: float,
    ) -> None:
        """A correct parameter list still fails when its behavior statement misses the required fact.

        Both answers name ``flag``; only the one mentioning the switch earns the behavior fact, so an empty context
        claim cannot be promoted to a correct answer.
        """
        answer = contracts.parse_readcrop_answer(
            f'{{"signature": "Example.method", "parameters": ["flag"], "behavior": "{behavior}"}}'
        )

        score = contracts.score_readcrop_answer(focused_contract, answer)

        assert score.behavior_fact_recall == recall
        assert score.behavior_facts_correct is facts_correct
        assert score.primary_correct is primary_correct
        assert score.quality_score == quality

    @pytest.mark.parametrize(
        ("extra", "match"),
        [
            pytest.param(
                {"required_parameters": ["missing"]},
                "required_parameters are absent from source: missing",
                id="parameter-absent-from-source",
            ),
            pytest.param(
                {
                    "required_parameters": ["flag"],
                    "required_behavior_facts": [{"parameter": "value", "terms": ["value"]}],
                },
                "behavior fact parameter is not required: value",
                id="behavior-fact-for-unrequired-parameter",
            ),
        ],
    )
    def test_rejects_unverified_focused_oracle(self, extra: dict[str, Any], match: str) -> None:
        """Task authors cannot add a focused parameter or behavior fact that the frozen source does not back.

        A focused parameter missing from the source, or a behavior fact about a parameter that is not required, would
        let an unreviewed oracle into the shared scoring truth.
        """
        with pytest.raises(ValueError, match=match):
            contracts.build_readcrop_contract({**_task(), **extra}, source=_SOURCE)

    def test_missing_focused_parameters_keep_source_wide_recall(self) -> None:
        """Tasks without a focused list retain the full source signature contract.

        Naming only ``value`` of the two source parameters halves parameter recall and cannot be a correct answer.
        """
        contract = contracts.build_readcrop_contract(_task(), source=_SOURCE)
        answer = contracts.parse_readcrop_answer(
            '{"signature": "Example.method", "parameters": ["value"], "behavior": "Context."}'
        )

        score = contracts.score_readcrop_answer(contract, answer)

        assert contract.required_parameter_names == ("value", "flag")
        assert score.parameter_recall == 0.5
        assert score.primary_correct is False


class TestProviderBinding:
    """Provider metadata may vary while shared read-crop truth stays locked."""

    @pytest.fixture(name="contract_binding")
    def _contract_binding(self) -> tuple[contracts.ReadcropContract, dict[str, Any]]:
        """Build a contract and a mutable copy of its provider binding."""
        contract = contracts.build_readcrop_contract(_task(), source=_SOURCE)
        return contract, dict(contract.provider_binding())

    def test_accepts_the_contract_own_binding(self, contract_binding: tuple[Any, dict[str, Any]]) -> None:
        """The binding a contract produces validates against that contract and carries a behavior-contract digest.

        The digest is a 64-character SHA-256 hex string, which is what pairs provider runs to the same oracle.
        """
        contract, binding = contract_binding

        contracts.validate_provider_binding(contract, binding)

        assert len(binding["behavior_contract_sha256"]) == 64

    def test_rejects_a_changed_oracle_identity(self, contract_binding: tuple[Any, dict[str, Any]]) -> None:
        """A binding whose oracle hash was altered is refused as a change to the scientific fields.

        A provider adapter must not be able to relabel the shared task or oracle identity it was handed.
        """
        contract, binding = contract_binding
        binding["oracle_sha256"] = "f" * 64

        with pytest.raises(ValueError, match="scientific fields"):
            contracts.validate_provider_binding(contract, binding)


class TestReadcropUsage:
    """Provider-native total tokens and observed tool payload cost are kept apart."""

    @pytest.mark.parametrize(
        "tool_result_tokens",
        [pytest.param(30, id="payload-counted"), pytest.param(None, id="payload-unavailable-stays-none")],
    )
    def test_tool_result_tokens_are_separate_from_total_input_tokens(self, tool_result_tokens: int | None) -> None:
        """Total input tokens and tool-result tokens never overwrite each other, and a missing payload stays ``None``.

        A provider that omits payload accounting must not be coerced to zero, and a reported payload must not be
        conflated with the provider-native total.
        """
        usage = contracts.ReadcropUsage(total_input_tokens=100, tool_result_tokens=tool_result_tokens)

        assert usage.total_input_tokens == 100
        assert usage.tool_result_tokens == tool_result_tokens
