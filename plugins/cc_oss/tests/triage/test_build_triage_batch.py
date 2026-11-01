"""Tests for ``bin/build_triage_batch.py`` — thread identifiers → codemap-py query batch spec.

The module maps extracted candidate identifiers to ``codemap-py query batch`` queries (dotted modules → ``rdeps``, bare
symbols → ``find-symbol``) and writes the JSON array to an output file for the oss:analyse stale-symbol check. It only
builds data and writes a file, so nothing is mocked and ``codemap-py`` itself is never invoked.
"""

from __future__ import annotations

import json
from pathlib import Path

import build_triage_batch  # type: ignore[import-not-found]
import pytest


class TestBuildQueries:
    """Mapping candidate identifiers to query specs."""

    @pytest.mark.parametrize(
        ("candidates", "expected"),
        [
            pytest.param(
                ["pkg.mod.sub", "MyClass"],
                [
                    {"cmd": "rdeps", "args": ["pkg.mod.sub", "--exclude-tests"]},
                    {"cmd": "find-symbol", "args": ["^MyClass$", "--limit", "1"]},
                ],
                id="dotted-module-vs-bare-symbol",
            ),
            pytest.param(
                ["  ", "", "x"], [{"cmd": "find-symbol", "args": ["^x$", "--limit", "1"]}], id="blank-skipped"
            ),
            pytest.param(
                [".hidden"],
                [{"cmd": "find-symbol", "args": ["^\\.hidden$", "--limit", "1"]}],
                id="leading-dot-falls-back-to-symbol",
            ),
        ],
    )
    def test_maps_identifiers_to_queries(self, candidates: list[str], expected: list[dict[str, object]]) -> None:
        """A dotted name becomes an ``rdeps`` query and a bare name an anchored, escaped ``find-symbol`` lookup.

        Blank or whitespace-only entries are dropped, and a leading dot (an invalid Python module name) is treated as a
        symbol rather than queried as a module.
        """
        assert build_triage_batch.build_queries(candidates) == expected


class TestMain:
    """The command-line entry point: candidate file in, batch JSON file and count out."""

    def test_wrong_argument_count_exits_1(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Wrong arg count → exit 1 with usage on stderr.

        Called with a single argument where the candidate file and the output file are both required.
        """
        rc = build_triage_batch.main(["only-one"])

        assert rc == 1
        assert "Usage" in capsys.readouterr().err

    def test_unreadable_candidate_file_exits_1(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A missing candidate file → exit 1, error on stderr.

        The caller must see the failure rather than an empty batch that silently skips the stale-symbol check.
        """
        rc = build_triage_batch.main([str(tmp_path / "nope.txt"), str(tmp_path / "out.json")])

        assert rc == 1
        assert "cannot read" in capsys.readouterr().err

    @pytest.mark.parametrize(
        ("candidates", "count", "commands"),
        [
            pytest.param("a.b.c\nWidget\n", "2", ["rdeps", "find-symbol"], id="two-candidates"),
            pytest.param("", "0", [], id="empty-file"),
        ],
    )
    def test_writes_batch_and_prints_count(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        candidates: str,
        count: str,
        commands: list[str],
    ) -> None:
        """The candidate file becomes a batch JSON array and its length is printed to stdout.

        An empty candidate file writes ``[]`` and prints ``0``, which is the signal for the caller to skip the
        ``codemap-py query`` call entirely.
        """
        cand = tmp_path / "cand.txt"
        cand.write_text(candidates, encoding="utf-8")
        out = tmp_path / "batch.json"

        rc = build_triage_batch.main([str(cand), str(out)])

        assert rc == 0
        assert capsys.readouterr().out.strip() == count
        assert [query["cmd"] for query in json.loads(out.read_text(encoding="utf-8"))] == commands
