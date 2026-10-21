"""Tests for the test-impact skill helpers: ``parse_test_impact_args.py`` and ``summarize_test_impact.py``."""

from __future__ import annotations

import io
import json
import tempfile
from pathlib import Path

import parse_test_impact_args
import pytest
import summarize_test_impact
from parse_test_impact_args import ArgumentsError, ImpactArgs, parse_arguments
from summarize_test_impact import describe_failure, extract_payload


@pytest.fixture
def sentinel_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point sentinel writes at a private temp dir with a fixed session token."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "s1")
    return tmp_path


class TestParseArguments:
    """Tokenizing ``$ARGUMENTS`` into one target plus the exact ``--no-mocks`` flag."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param("pkg.mod::fn", ImpactArgs("pkg.mod::fn", False, ()), id="target-only"),
            pytest.param("--no-mocks pkg.mod::fn", ImpactArgs("pkg.mod::fn", True, ()), id="flag-first"),
            pytest.param("pkg.mod::fn --no-mocks", ImpactArgs("pkg.mod::fn", True, ()), id="flag-last"),
            pytest.param("pkg.a::f pkg.b::g", ImpactArgs("pkg.a::f", False, ("pkg.b::g",)), id="extra-targets"),
            pytest.param("pkg.no_mocks_util", ImpactArgs("pkg.no_mocks_util", False, ()), id="flag-text-in-target"),
            pytest.param("", ImpactArgs("", False, ()), id="empty"),
        ],
    )
    def test_extracts_target_and_flag(self, raw: str, expected: ImpactArgs) -> None:
        """A flag never becomes the target and flag text inside a target never sets the flag.

        The old ``awk '{print $1}'`` split sent ``--no-mocks`` as the target when the flag came first.
        """
        assert parse_arguments(raw) == expected

    @pytest.mark.parametrize("raw", ["--mocks pkg::f", "-x pkg::f", "'pkg::f"])
    def test_rejects_unknown_flag_and_unbalanced_quote(self, raw: str) -> None:
        """A flag-shaped token other than ``--no-mocks`` or an unbalanced quote is rejected, not guessed."""
        with pytest.raises(ArgumentsError):
            parse_arguments(raw)


def _file_symlinks_available() -> bool:
    """Probe file-symlink capability at collection time instead of assuming the host grants it."""
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory, "target")
        target.write_text("", encoding="utf-8")
        try:
            Path(directory, "link").symlink_to(target)
        except OSError:
            return False
        return True


_skip_no_file_symlinks = pytest.mark.skipif(not _file_symlinks_available(), reason="file symlinks unavailable")


class TestSentinelSafety:
    """Both helpers write sentinels through the shared writer, never through a planted link."""

    @_skip_no_file_symlinks
    def test_parse_refuses_a_planted_symlink(self, sentinel_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A link planted at the target sentinel is refused with exit 2; the file it points at is untouched."""
        victim = sentinel_dir / "victim.txt"
        victim.write_text("keep\n", encoding="utf-8")
        (sentinel_dir / "codemap-p-ti-qname-s1").symlink_to(victim)

        rc = parse_test_impact_args.main(["--output-prefix", "codemap-p", "--arguments=pkg::f"])

        assert rc == 2
        assert victim.read_text(encoding="utf-8") == "keep\n"
        assert "sentinels not written" in capsys.readouterr().err

    def test_parse_refuses_a_path_shaped_prefix(self, sentinel_dir: Path) -> None:
        """An ``--output-prefix`` holding a separator would place sentinels outside the temp dir; it is refused."""
        rc = parse_test_impact_args.main(["--output-prefix", "../escape", "--arguments=pkg::f"])

        assert rc == 2
        assert not (sentinel_dir.parent / "escape-ti-qname-s1").exists()


class TestParseMain:
    """Exit codes and sentinel writes of ``parse_test_impact_args.main``."""

    def test_writes_sentinels_for_a_target(self, sentinel_dir: Path) -> None:
        """A parsed target and the mocks flag land in the session-scoped sentinels Step 2 reads back."""
        rc = parse_test_impact_args.main(["--output-prefix", "codemap-p", "--arguments=--no-mocks pkg::f"])

        assert rc == 0
        assert (sentinel_dir / "codemap-p-ti-qname-s1").read_text() == "pkg::f\n"
        assert (sentinel_dir / "codemap-p-ti-mocks-s1").read_text() == "--no-mocks\n"

    @pytest.mark.parametrize(
        ("arguments", "expected_rc"),
        [pytest.param("", 3, id="empty-asks-user"), pytest.param("--bogus pkg::f", 2, id="unknown-flag")],
    )
    def test_clears_stale_target_on_failure(self, sentinel_dir: Path, arguments: str, expected_rc: int) -> None:
        """An empty or rejected argument string clears a target left by an earlier run.

        Step 2 must never query a stale symbol from a previous invocation in the same session.
        """
        (sentinel_dir / "codemap-p-ti-qname-s1").write_text("old::target\n")

        rc = parse_test_impact_args.main(["--output-prefix", "codemap-p", f"--arguments={arguments}"])

        assert rc == expected_rc
        assert (sentinel_dir / "codemap-p-ti-qname-s1").read_text() == "\n"

    def test_answer_pass_keeps_the_original_no_mocks_flag(self, sentinel_dir: Path) -> None:
        """Re-parsing the asked-for target together with the original flags keeps ``--no-mocks``.

        ``/codemap-py:test-impact --no-mocks`` with no target exits 3 and asks for one; the skill re-parses ``"$QNAME
        $ARGUMENTS"``. Parsing the answer alone rewrote ``ti-mocks`` empty and dropped the user's flag.
        """
        parse_test_impact_args.main(["--output-prefix", "codemap-p", "--arguments=--no-mocks"])

        rc = parse_test_impact_args.main(["--output-prefix", "codemap-p", "--arguments=pkg::f --no-mocks"])

        assert rc == 0
        assert (sentinel_dir / "codemap-p-ti-qname-s1").read_text() == "pkg::f\n"
        assert (sentinel_dir / "codemap-p-ti-mocks-s1").read_text() == "--no-mocks\n"

    def test_warns_when_more_than_one_target(self, sentinel_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Surplus targets keep the skill's one-symbol warning wording verbatim."""
        parse_test_impact_args.main(["--output-prefix", "codemap-p", "--arguments=a::f b::g"])

        assert "⚠ test-impact accepts one symbol at a time — using first token only: a::f." in capsys.readouterr().out


class TestDescribeFailure:
    """Failure routing: target errors never get rebuild advice, provider failures always do."""

    def test_unresolved_target_lists_candidates_without_rebuild_advice(self) -> None:
        """An ambiguous name prints its candidates and steers away from rebuilding the index."""
        payload = {"error": "ambiguous", "candidates": ["a.b::f", "a.c::f"], "rejected_target": "f"}

        lines = describe_failure(1, payload, "")

        assert "  candidate: a.b::f" in lines
        assert summarize_test_impact.REBUILD_ADVICE not in lines

    def test_module_not_indexed_exit_three_lists_suggestions(self) -> None:
        """Exit 3 with ``rejected_target`` is a target problem, even though corrupt-index JSON also exits 3."""
        payload = {"error": "module not indexed", "suggestions": ["pkg.mod"], "rejected_target": "pkg.mdo"}

        assert "  candidate: pkg.mod" in describe_failure(3, payload, "")

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param({"error": "Index not found at x. Run /codemap-py:scan-codebase first."}, id="missing-index"),
            pytest.param({"error": "index is not valid JSON", "path": "x"}, id="corrupt-index"),
            pytest.param(None, id="no-json"),
        ],
    )
    def test_provider_failure_gets_rebuild_advice(self, payload: dict | None) -> None:
        """Missing or corrupt index failures end with the rebuild instruction."""
        assert describe_failure(1, payload, "warn line\n")[-1] == summarize_test_impact.REBUILD_ADVICE

    def test_bad_input_is_not_routed_to_rebuild(self) -> None:
        """A rejected argument (exit 2, no target key) is not an index problem."""
        assert summarize_test_impact.REBUILD_ADVICE not in describe_failure(2, None, "usage: ...")


class TestSummarizeMain:
    """End-to-end ``summarize_test_impact.main`` over stdin."""

    def test_prints_every_reported_field_and_writes_sentinels(
        self, sentinel_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The summary carries the test list and via counts the report renders, so nothing is reconstructed."""
        payload = {
            "qname": "pkg::f",
            "test_files": ["tests/test_a.py", "tests/test_b.py"],
            "via_call": 2,
            "via_mock": 1,
            "pytest_cmd": "pytest tests/test_a.py tests/test_b.py",
            "index": {"query_complete": True, "stale": False, "not_covered": ["dynamic-dispatch"]},
        }
        monkeypatch.setattr("sys.stdin", io.StringIO("log line\n" + json.dumps(payload) + "\n"))

        rc = summarize_test_impact.main(["--rc", "0", "--output-prefix", "codemap-p"])

        out = capsys.readouterr().out
        assert rc == 0
        assert "total: 2 (via_call: 2, via_mock: 1)" in out
        assert "- tests/test_b.py" in out
        assert json.loads((sentinel_dir / "codemap-p-ti-test-files-s1").read_text()) == payload["test_files"]

    def test_non_json_success_fails_loudly(self, sentinel_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Exit 0 with unparsable stdout is a failure, never an empty test set."""
        monkeypatch.setattr("sys.stdin", io.StringIO("garbage"))

        assert summarize_test_impact.main(["--rc", "0", "--output-prefix", "codemap-p"]) == 1
        assert not (sentinel_dir / "codemap-p-ti-total-s1").exists()


def test_extract_payload_prefers_whole_text() -> None:
    """A clean single JSON document parses directly."""
    assert extract_payload('{"a": 1}') == {"a": 1}
