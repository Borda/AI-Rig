"""Contract tests for the review's structural-context record: impact causes, tiering and the codemap block.

The review used to query the invoking checkout's index and printed a bare "impact unknown" when that failed. It now asks
``codemap-py context`` for a per-head record with a closed-set cause, tiers impact from that record's own index, and
names every unavailable answer with the provider's cause slug. These tests run the tiering on real records and pin the
codemap block lines that produce and copy the record.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import review_impact_tier as rit

_PLUGIN = Path(__file__).resolve().parents[2]
_MODES = _PLUGIN / "skills/review/modes"
_SCRIPT = _PLUGIN / "bin/review_impact_tier.py"
_VIZ = {"changed_modules": [{"path": "src/pkg/viz/draw.py", "changed_symbols": ["pkg.viz.draw::box"]}]}
_CORE = {"changed_modules": [{"path": "src/pkg/core.py", "changed_symbols": ["pkg.core::run"]}]}

#: Provider cause slugs that name an unavailable record (``codemap-py`` structural-context contract), minus
#: ``non-python-scope``, which is ``not-applicable`` rather than unknown impact.
_PROVIDER_UNAVAILABLE_CAUSES = (
    "codemap-not-installed",
    "codemap-interpreter-unsupported",
    "codemap-timeout",
    "index-missing",
    "index-stale",
    "index-root-mismatch",
    "index-busy",
    "index-build-failed",
    "query-failed",
    "head-mismatch",
    "source-unavailable",
    "diff-unavailable",
    "context-request-invalid",
)


def _record(status: str, **fields: object) -> dict:
    """Build a structural-context record with the schema id and the given status and fields."""
    return {"schema": rit.CONTEXT_SCHEMA, "schema_version": 1, "status": status, **fields}


def _no_callers(_qname: str) -> dict:
    """Answer fn-blast with an empty caller set, so the payload alone decides."""
    return {"blast_radius": []}


def _block_lines(name: str) -> list[str]:
    """Return the lines inside the fenced bash blocks of one review mode file."""
    inside, lines = False, []
    for line in (_MODES / name).read_text(encoding="utf-8").splitlines():
        if line.startswith("```"):
            inside = line.startswith("```bash") if not inside else False
            continue
        if inside:
            lines.append(line)
    return lines


class TestContextCauses:
    """Every cause the provider can report maps onto one impact cause by value."""

    @pytest.mark.parametrize("slug", _PROVIDER_UNAVAILABLE_CAUSES)
    def test_provider_cause_is_an_impact_cause(self, slug: str) -> None:
        """An unavailable record's cause prints its own phrase, never the query-failed fallback."""
        tier, reason = rit.classify_context(_record("unavailable", cause=slug, detail=""))
        assert (tier, rit.cause_of(reason)) == ("FULL", rit.ImpactCause(slug))

    def test_detail_rides_after_the_phrase_on_one_line(self) -> None:
        """The provider's diagnostic follows the phrase, collapsed onto one bounded line for the sentinel."""
        detail = "scan exited 2:\nlease held by pid 42 " + "x" * 200
        _, reason = rit.classify_context(_record("unavailable", cause="index-busy", detail=detail))
        assert reason.startswith("impact unknown: index busy scan exited 2: lease held by pid 42")
        assert "\n" not in reason
        assert rit.cause_of(reason) is rit.ImpactCause.INDEX_BUSY

    def test_unrecognised_cause_still_names_a_cause(self) -> None:
        """A cause a newer provider adds degrades to query-failed with the slug, never to an unnamed unknown."""
        _, reason = rit.classify_context(_record("unavailable", cause="brand-new-cause", detail=""))
        assert rit.cause_of(reason) is rit.ImpactCause.QUERY_FAILED
        assert "brand-new-cause" in reason


class TestClassifyContext:
    """The tier comes from the record's own answer and never trims on missing or partial evidence."""

    def test_available_record_tiers_from_its_diff_impact(self) -> None:
        """A complete answer in which every change stays in leaf modules is LIGHT."""
        tier, _ = rit.classify_context(_record("available", diff_impact=_VIZ), _no_callers)
        assert tier == "LIGHT"

    def test_available_record_reaching_main_path_is_full(self) -> None:
        """A public main-path symbol keeps the review at full depth."""
        assert rit.classify_context(_record("available", diff_impact=_CORE), _no_callers) == (
            "FULL",
            "public main-path symbol pkg.core::run",
        )

    def test_degraded_record_never_yields_light(self) -> None:
        """A degraded answer misses part of the change set, so a LIGHT result is raised with its first reason."""
        record = _record("degraded", diff_impact=_VIZ, degraded_reasons=["deleted module pkg/old.py", "second"])
        assert rit.classify_context(record, _no_callers) == (
            "FULL",
            "degraded structural answer: deleted module pkg/old.py",
        )

    def test_unavailable_fallback_never_sets_the_tier(self) -> None:
        """Text-search hits are evidence for reviewers only; the tier stays the cause's unknown-impact line."""
        fallback = {"modules": [{"path": "src/pkg/viz/draw.py", "importer_files": []}], "symbols": []}
        record = _record("unavailable", cause="index-missing", detail="", fallback=fallback, diff_impact=None)
        assert rit.classify_context(record, _no_callers) == ("FULL", "impact unknown: index missing")

    def test_not_applicable_is_full_without_an_unknown_cause(self) -> None:
        """A diff with no Python file has no structural question, so it is neither trimmed nor a codemap gap."""
        tier, reason = rit.classify_context(_record("not-applicable", cause="non-python-scope"))
        assert (tier, reason, rit.cause_of(reason)) == ("FULL", rit.NOT_APPLICABLE_REASON, None)

    @pytest.mark.parametrize(
        "record",
        [
            pytest.param(None, id="not-a-dict"),
            pytest.param({"status": "available", "diff_impact": _VIZ}, id="no-schema"),
            pytest.param(_record("mystery", diff_impact=_VIZ), id="unknown-status"),
        ],
    )
    def test_unreadable_record_is_full(self, record: object) -> None:
        """Anything that is not a recognised record keeps the fail-safe full depth with a named cause."""
        tier, reason = rit.classify_context(record, _no_callers)
        assert (tier, rit.cause_of(reason)) == ("FULL", rit.ImpactCause.UNREADABLE_OUTPUT)

    def test_fn_blast_is_pinned_to_the_record_index(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Callers come from the record's own index and root, never from the invoking checkout's index."""
        calls: list[tuple[str, tuple[str, ...]]] = []

        def _fake_blast(qname: str, index_args: tuple[str, ...] = ()) -> dict:
            """Record the query and answer with no callers."""
            calls.append((qname, index_args))
            return {"blast_radius": []}

        monkeypatch.setattr(rit, "run_fn_blast", _fake_blast)
        index = {"path": "/tmp/ctx/abc/proj.json", "dir": "/tmp/ctx/abc", "action": "built"}
        diff_impact = {"changed_modules": [{"path": "src/pkg/core.py", "changed_symbols": ["pkg.core::_prep"]}]}
        record = _record("available", diff_impact=diff_impact, index=index, root="/work/wt")
        assert rit.classify_context(record)[0] == "LIGHT"
        assert calls == [("pkg.core::_prep", ("--index", "/tmp/ctx/abc/proj.json", "--root", "/work/wt", "--no-heal"))]

    def test_record_without_index_never_queries_another_index(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A record naming no index cannot be paired with callers from elsewhere: the symbol stays unknown."""
        monkeypatch.setattr(rit, "run_fn_blast", lambda *_a, **_k: pytest.fail("queried an index the record lacks"))
        diff_impact = {"changed_modules": [{"path": "src/pkg/core.py", "changed_symbols": ["pkg.core::_prep"]}]}
        tier, reason = rit.classify_context(_record("available", diff_impact=diff_impact, index=None))
        assert (tier, rit.cause_of(reason)) == ("FULL", rit.ImpactCause.FN_BLAST_FAILED)


class TestContextRecordCli:
    """The script reads the record file the codemap block stages and prints one sentinel line."""

    @staticmethod
    def _run(*args: str) -> subprocess.CompletedProcess[str]:
        """Run the shipped script with ``args`` and no stdin."""
        return subprocess.run(
            [sys.executable, str(_SCRIPT), *args], capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL
        )

    def test_unavailable_record_file(self, tmp_path: Path) -> None:
        """An unavailable record prints its cause phrase and exits 0."""
        path = tmp_path / "record.json"
        path.write_text(json.dumps(_record("unavailable", cause="head-mismatch", detail="")), encoding="utf-8")
        proc = self._run("--context-record", str(path))
        assert (proc.returncode, proc.stdout) == (0, "FULL · impact unknown: checkout not at the reviewed head\n")

    def test_missing_record_file(self, tmp_path: Path) -> None:
        """A record file that is absent or empty still prints a named unknown-impact line, never a crash."""
        empty = tmp_path / "empty.json"
        empty.write_text("", encoding="utf-8")
        for path in (tmp_path / "absent.json", empty):
            proc = self._run("--context-record", str(path))
            assert proc.returncode == 0
            assert rit.cause_of(proc.stdout.strip()) is rit.ImpactCause.UNREADABLE_OUTPUT

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param(b'{"status": "\xff"}', id="invalid-utf8"),
            pytest.param(json.dumps(_record([], diff_impact=_VIZ)).encode(), id="invalid-status-type"),
            pytest.param(
                json.dumps(
                    _record(
                        "degraded", diff_impact={"changed_modules": [{"path": "src/pkg/viz.py"}]}, degraded_reasons=123
                    )
                ).encode(),
                id="invalid-degraded-reasons",
            ),
        ],
    )
    def test_malformed_record_prints_unknown_impact(self, tmp_path: Path, body: bytes) -> None:
        """Invalid record bytes or fields still produce a FULL sentinel and a successful exit."""
        path = tmp_path / "record.json"
        path.write_bytes(body)
        proc = self._run("--context-record", str(path))

        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.startswith("FULL · impact unknown:")
        assert len(proc.stdout.splitlines()) == 1

    def test_record_and_unknown_are_exclusive(self, tmp_path: Path) -> None:
        """One invocation decides from one source: a caller-detected cause or a record, never both."""
        proc = self._run("--unknown", "index-stale", "--context-record", str(tmp_path / "r.json"))
        assert proc.returncode == 2


class TestCodemapBlock:
    """The review's Step 1 codemap block asks for the per-head record and tiers from it."""

    def test_context_call_verifies_the_reviewed_head(self) -> None:
        """The call names the checkout root, the snapshot diff, the snapshot head and a record file."""
        call = [line for line in _block_lines("codemap-context.md") if "codemap-py context " in line]
        assert len(call) == 1
        for token in (
            '--root "$(git rev-parse --show-toplevel)"',
            '--diff-file "$_SNAP_DIR/pr.diff"',
            '--expect-head "$_PR_HEAD_SHA"',
            '--out "$_CONTEXT_JSON"',
        ):
            assert token in call[0]

    def test_only_an_inherited_freeze_stops_the_per_head_build(self) -> None:
        """The caller's freeze is captured before the stale-currency export and passed to the context call."""
        lines = _block_lines("codemap-context.md")
        capture = lines.index('_INHERITED_FREEZE="${SCAN_NO_AUTOBUILD:-0}"')
        export = next(i for i, line in enumerate(lines) if "export SCAN_NO_AUTOBUILD=1" in line)
        call = next(line for line in lines if "codemap-py context " in line)
        assert capture < export
        assert call.lstrip().startswith('SCAN_NO_AUTOBUILD="$_INHERITED_FREEZE" codemap-py context ')

    @pytest.mark.parametrize(
        "fragment",
        [
            pytest.param('review_impact_tier.py" --context-record "$_CONTEXT_JSON"', id="tier-from-record"),
            pytest.param('review_impact_tier.py" --unknown codemap-interpreter-unsupported', id="launcher-127"),
            pytest.param("diff-impact --diff-file", id="older-provider-keeps-query"),
            pytest.param("#### Text-search fallback (method: text-search, confidence: low", id="fallback-as-evidence"),
        ],
    )
    def test_block_handles_each_record_outcome(self, fragment: str) -> None:
        """Record, launcher failure, older provider and fallback evidence each have their own line."""
        assert any(fragment in line for line in _block_lines("codemap-context.md"))

    @pytest.mark.skipif(shutil.which("bash") is None, reason="the review's codemap block runs in Bash")
    @pytest.mark.parametrize(
        ("rc", "stderr", "heads", "legacy", "unknown"),
        [
            pytest.param(2, "codemap-py: unknown command 'context'\nusage: codemap-py {index,query,doctor,integrate}",
                         ("abc1234", "abc1234"), True, "", id="legacy-query-at-reviewed-head"),
            pytest.param(2, "codemap-py: unknown command 'context'", ("abc1234", "abc1235"), False,
                         "head-mismatch", id="legacy-query-refused-on-other-head"),
            pytest.param(2, "invalid choice: 'context'", ("", ""), False,
                         "head-mismatch", id="legacy-query-refused-without-reviewed-head"),
            pytest.param(2, "invalid choice: 'context'", ("abc1234", ""), False,
                         "head-mismatch", id="legacy-query-refused-when-git-cannot-read-head"),
            pytest.param(2, '{"error": "invalid_context_request", "detail": "--expect-head must be hex"}', ("", ""), False,
                         "context-request-invalid", id="usage-refusal-names-its-own-cause"),
            pytest.param(0, '{"error": "context_out_unwritable", "detail": "PermissionError"}', ("", ""), False, "query-failed",
                         id="record-not-written-is-a-failed-query"),
            pytest.param(127, "no eligible CPython", ("", ""), False, "codemap-interpreter-unsupported", id="launcher-127"),
        ],
    )  # fmt: skip
    def test_only_a_launcher_without_context_falls_back_to_this_checkout(
        self, tmp_path: Path, rc: int, stderr: str, heads: tuple[str, str], legacy: bool, unknown: str
    ) -> None:
        """A missing record runs the legacy query on this checkout only when the launcher has no ``context`` command.

        Every other empty record (a usage refusal, an unwritten ``--out``) names its cause instead: the legacy query
        reads the invoking checkout's index, which the per-head context exists to avoid.
        """
        lines = _block_lines("codemap-context.md")
        start = next(i for i, line in enumerate(lines) if line.strip().startswith("_CONTEXT_JSON="))
        end = next(i for i in range(start, len(lines)) if lines[i] == "        fi")
        stubs = (
            'codemap-py() { if [ "$1" = context ]; then printf \'%s\\n\' "$STUB_ERR" >&2; return "$STUB_RC"; fi; '
            'echo "LEGACY-QUERY $*"; }\n'
            'git() { if [ "$2" = --show-toplevel ]; then echo /repo; '
            'else [ -n "$STUB_HEAD" ] && echo "$STUB_HEAD"; fi; }\n'
            'jq() { echo "$STUB_PR_HEAD"; }\npython() { echo "PY $*"; }\n'
        )
        script = stubs + "\n".join(lines[start : end + 1]) + "\n"
        env = os.environ | {"TMPDIR": str(tmp_path), "CLEAN_ARGS": "t", "CSID": "s", "PLUGIN_ROOT": "/p",
                            "_SNAP_DIR": "/snap", "_INHERITED_FREEZE": "0", "STUB_RC": str(rc), "STUB_ERR": stderr,
                            "STUB_PR_HEAD": heads[0], "STUB_HEAD": heads[1]}  # fmt: skip
        done = subprocess.run([shutil.which("bash"), "-c", script], env=env, capture_output=True, text=True, timeout=30)
        impact = tmp_path / "oss-review-impact-t-s"
        tier = impact.read_text(encoding="utf-8") if impact.is_file() else ""
        assert ("LEGACY-QUERY" in done.stdout, f"--unknown {unknown}" in tier if unknown else True) == (legacy, True)
        assert legacy or "--unknown" in tier, (done.stdout, done.stderr, tier)

    def test_step2_copies_the_record_and_names_a_missing_sentinel(self) -> None:
        """Step 2 copies the staged record into the run dir, and a missing impact sentinel names its cause."""
        lines = _block_lines("agent-launch.md")
        assert any('cp "$_STRUCTURAL_RECORD" "$RUN_DIR/codemap-structural-context.json"' in line for line in lines)
        fallback = next(line for line in lines if "IMPACT_LINE=" in line and "oss-review-impact-" in line)
        default = fallback.split('IMPACT_LINE="', 1)[1].rstrip('"')
        assert rit.cause_of(default) is rit.ImpactCause.CODEMAP_UNAVAILABLE
