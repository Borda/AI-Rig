"""Black-box regression contracts for the compact Codemap discovery surface."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from pathlib import Path

import pytest
from codemap_py import cli, query

_PLUGIN_ROOT = Path(__file__).resolve().parents[2]


_CODEX_QUERY_SKILL = _PLUGIN_ROOT / "codex-skills/query-code/SKILL.md"


def _symbol(name: str, qualified_name: str, start_line: int, end_line: int, **extra: object) -> dict[str, object]:
    """Return one untested function symbol record for a synthetic index."""
    return {
        "name": name,
        "qualified_name": qualified_name,
        "type": "function",
        "start_line": start_line,
        "end_line": end_line,
        "fn_rdep_test_count": 0,
        "mock_rdep_count": 0,
        **extra,
    }


def _index(*symbols: dict[str, object]) -> dict[str, object]:
    """Return a one-module synthetic index named ``pkg.module`` holding *symbols*."""
    return {
        "scan_version": 11,
        "modules": [{"name": "pkg.module", "status": "ok", "is_test": False, "symbols": list(symbols)}],
    }


class TestTopLevelCli:
    """The bare ``codemap-py`` entry point."""

    def test_help_teaches_compact_query_form(self, capsys: pytest.CaptureFixture[str]) -> None:
        """``--help`` exits 0 and shows the compact query form for the common discovery commands.

        First use starts at ``--help``; if it failed, or did not show ``query --compact``, users would never learn the
        public query form.
        """
        assert cli.main(["--help"]) == 0

        captured = capsys.readouterr()
        help_text = captured.out + captured.err
        assert "query" in help_text
        assert "codemap-py query --compact rdeps" in help_text
        assert "codemap-py query --compact fn-rdeps" in help_text
        assert "codemap-py query --compact undocumented" in help_text
        assert "codemap-py query --compact uncovered" in help_text

    def test_no_arguments_is_usage_error(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Running with no command exits 2 with a usage line, distinct from the successful help path.

        Only an explicit ``--help`` is a success; a missing command stays a syntax error so scripts notice it.
        """
        assert cli.main([]) == 2
        assert "usage: codemap-py" in capsys.readouterr().err


class TestCodexQuerySkill:
    """The Codex ``query-code`` skill text, re-sent on every invocation, stays small but complete."""

    @pytest.mark.parametrize(
        "phrase",
        [
            "smallest complete query set",
            "query --compact",
            "Run each compact query alone",
            "shell persistence",
            "retain its literal",
            "SCAN_NO_AUTOBUILD=1",
            "An ordinary query prepares the canonical project index",
            "including when no index exists yet",
            "--root <root>",
            "--index <path>",
            "--root <same-root>",
            "If frozen mode reports a missing index",
            "Never invoke that skill to repair an ordinary query",
            "symbol <name> --with-imports",
            "source-body implementation/runtime detail",
        ],
    )
    def test_keeps_required_fact(self, phrase: str) -> None:
        """Each independently required disclosure is still present in the compact skill.

        Compression must not drop launcher identity, index-root selection, frozen-mode handling or the query routing
        facts the agentic benchmark showed agents need; each phrase is one such fact.
        """
        assert phrase in _CODEX_QUERY_SKILL.read_text(encoding="utf-8")

    @pytest.mark.parametrize("phrase", ["make one query", "Maximum: three Codemap calls"])
    def test_omits_retired_guidance(self, phrase: str) -> None:
        """Retired call-budget guidance is gone from the skill.

        Telling agents to make one query or cap calls at three contradicted the smallest-complete-query-set rule and
        cost correct answers.
        """
        assert phrase not in _CODEX_QUERY_SKILL.read_text(encoding="utf-8")


class TestQualityQueryTotals:
    """``undocumented`` and ``uncovered`` label what their total counts and expose the unique-name count."""

    @pytest.mark.parametrize(
        ("command", "arguments", "result_key", "semantics_fragment"),
        [
            pytest.param(
                query.cmd_undocumented, ("pkg.module", False), "undocumented", "declaration", id="undocumented"
            ),
            pytest.param(
                query.cmd_uncovered,
                (argparse.Namespace(module="pkg.module", all_modules=False, sort="loc", top=20),),
                "uncovered",
                "symbol",
                id="uncovered",
            ),
        ],
    )
    def test_labels_the_meaning_of_its_total(
        self,
        capsys: pytest.CaptureFixture[str],
        command: Callable[..., object],
        arguments: tuple[object, ...],
        result_key: str,
        semantics_fragment: str,
    ) -> None:
        """The JSON output separates the declaration total from the unique-name count and says what each counts.

        Two declarations share one qualified name, so ``total`` is 2 and ``unique_total`` is 1; ``count_semantics`` must
        name the unit so the total is not mistaken for a different unique-name oracle.
        """
        index = _index(
            _symbol("missing_docs", "pkg.module::missing_docs", 1, 3, has_docstring=False),
            _symbol("same_name_second_declaration", "pkg.module::missing_docs", 4, 6, has_docstring=False),
        )

        command(index, *arguments)
        payload = json.loads(capsys.readouterr().out)

        assert payload["total"] == len(payload[result_key]) == 2
        assert payload["unique_total"] == 1
        assert payload["unique_qualified_names"] == ["pkg.module::missing_docs"]
        assert isinstance(payload["count_semantics"], dict)
        assert semantics_fragment in payload["count_semantics"]["total"].lower()

    def test_uncovered_keeps_unique_semantics_when_top_hides_findings(self, capsys: pytest.CaptureFixture[str]) -> None:
        """With ``--top 1`` the full static count stays distinct from the capped display list.

        Two uncovered symbols exist but one is shown; ``total`` and ``unique_total`` must still report both while
        ``showing`` reports the cap.
        """
        index = _index(_symbol("first", "pkg.module::first", 1, 3), _symbol("second", "pkg.module::second", 4, 6))

        query.cmd_uncovered(index, argparse.Namespace(module="pkg.module", all_modules=False, sort="name", top=1))
        payload = json.loads(capsys.readouterr().out)

        assert payload["total"] == payload["unique_total"] == 2
        assert payload["showing"] == len(payload["uncovered"]) == 1
        assert payload["unique_qualified_names"] == ["pkg.module::first", "pkg.module::second"]
