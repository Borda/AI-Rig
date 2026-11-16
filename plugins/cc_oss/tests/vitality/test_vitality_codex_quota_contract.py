"""Guard the vitality Codex quota stop: a harness quota fault ends Codex review for the rest of the run.

Step 5 runs one independent Codex review and each Step 6 rework iteration one more. After a quota-exhausted incident
every later call would fail the same way, so the run continues challenger-only and reports Codex as blocked by quota
instead of timed out. The tests run the real fenced Bash blocks against a scratch review directory.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from _oss_shell_plugin import render_shell
from _text_io import bash_block_after

_PLUGIN = Path(__file__).resolve().parents[2]
_MODES = _PLUGIN / "skills/analyse/modes"
_SESSION = "vitality-quota"
_INCIDENT = ".temp/bridge/incidents/1700000000000000000-0a0b0c0d-quota-exhausted.json"
_BASH = shutil.which("bash")
_skip_no_bash = pytest.mark.skipif(_BASH is None, reason="vitality blocks run under bash")
#: Line a challenger opens its ``## Codex Cross-Check`` section with after its own cross-check stopped on quota.
_CROSS_CHECK_QUOTA = (
    "⚠ codex-blocked:quota — Codex out of credits (incident fault `quota-exhausted`); cross-check not retried. "
    "Report above is Claude-only."
)
#: Prose right above each block under test.
_STEP5_MARKER = "Then run the quota check block once, unedited"
_REWORK_MARKER = "After spawning, verify sentinels and parse the challenger verdict"


def _mode(name: str) -> str:
    """Return one analyse mode file."""
    return (_MODES / name).read_text(encoding="utf-8")


def _put(path: Path, content: str) -> None:
    """Write LF-only UTF-8 text, creating parent directories, as the Write tool would."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def _reply(incident: str | None) -> str:
    """Return a stopped bridge:review reply whose verdict already carries the quota wording."""
    verdict = "Codex quota exhausted: usage limit reached"
    return json.dumps({"status": "blocked", "verdict": verdict, "findings": [], "files_touched": [], "remaining": [],
                       "blockers": [verdict], "incident": incident})  # fmt: skip


@pytest.fixture
def review_dir(tmp_path: Path) -> Path:
    """Create the run's review directory, the session sentinels pointing at it, and a launch directory."""
    review = tmp_path / "launch/.reports/analyse/vitality/o-r/run-review"
    review.mkdir(parents=True)
    for kind, value in {"review-dir": str(review), "codex-available": "1", "rework-iter": "0"}.items():
        _put(tmp_path / f"vitality-{kind}-{_SESSION}", f"{value}\n")
    return review


def _run(block: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Run one fenced block from the launch directory with the session token and scratch TMPDIR."""
    env = os.environ | {
        "CLAUDE_CODE_SESSION_ID": _SESSION,
        "CLAUDE_PLUGIN_ROOT": str(_PLUGIN),
        "TMPDIR": str(tmp_path),
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}",
    }
    script = render_shell(block, _PLUGIN, "oss")
    return subprocess.run(
        [_BASH, "-c", script],
        cwd=tmp_path / "launch",
        env=env,
        capture_output=True,
        encoding="utf-8",  # the blocks print UTF-8 symbols; the Windows default codepage would garble or drop them
        errors="replace",
        check=False,
    )


class TestStep5QuotaCheck:
    """Step 5's quota check records the stop only for a harness quota incident."""

    @_skip_no_bash
    def test_quota_incident_writes_the_run_marker(self, tmp_path: Path, review_dir: Path) -> None:
        """A quota-exhausted incident linked from the persisted reply disables Codex for the rest of the run."""
        _put(tmp_path / "launch" / _INCIDENT, json.dumps({"schema_version": 2, "fault": "quota-exhausted"}))
        _put(review_dir / "codex-repo-review.json", _reply(_INCIDENT))

        result = _run(bash_block_after(_mode("vitality-codex-review.md"), _STEP5_MARKER), tmp_path)

        assert result.returncode == 0, result.stdout + result.stderr
        assert "CODEX_QUOTA=codex-blocked:quota" in result.stdout
        assert (review_dir / "codex-quota-blocked.txt").read_text(encoding="utf-8") == "codex-blocked:quota\n"

    @_skip_no_bash
    @pytest.mark.parametrize(
        ("incident", "fault"),
        [
            pytest.param(None, "quota-exhausted", id="model-authored-null-incident"),
            pytest.param(_INCIDENT, "rate-limited", id="rate-limited"),
            pytest.param(_INCIDENT, "capacity", id="capacity"),
            pytest.param(_INCIDENT, "blocked", id="old-bridge-generic-fault"),
        ],
    )
    def test_other_replies_keep_codex_on(
        self, tmp_path: Path, review_dir: Path, incident: str | None, fault: str
    ) -> None:
        """A peer-authored quota verdict, a rate limit, capacity or an older bridge's fault never stops Codex."""
        _put(tmp_path / "launch" / _INCIDENT, json.dumps({"fault": fault}))
        _put(review_dir / "codex-repo-review.json", _reply(incident))

        result = _run(bash_block_after(_mode("vitality-codex-review.md"), _STEP5_MARKER), tmp_path)

        assert result.returncode == 0, result.stdout + result.stderr
        assert "CODEX_QUOTA=none" in result.stdout
        assert not (review_dir / "codex-quota-blocked.txt").exists()


class TestReworkQuotaStop:
    """Step 6a reports a quota stop as its own outcome and keeps other missing Codex results as timed out."""

    @_skip_no_bash
    @pytest.mark.parametrize(
        ("earlier_file", "fault"),
        [
            pytest.param("codex-quota-blocked.txt", "rate-limited", id="stopped-earlier"),
            pytest.param("unrelated.txt", "quota-exhausted", id="stopped-this-iteration"),
        ],
    )
    def test_quota_stop_is_reported_instead_of_a_timeout(
        self, tmp_path: Path, review_dir: Path, earlier_file: str, fault: str
    ) -> None:
        """A quota stop, from Step 5 or from this iteration's reply, prints the blocked line and no timeout.

        ``stopped-earlier`` starts with the marker an earlier call wrote; ``stopped-this-iteration`` starts without it.
        """
        _put(review_dir / earlier_file, "codex-blocked:quota\n")
        _put(tmp_path / "launch" / _INCIDENT, json.dumps({"fault": fault}))
        _put(review_dir / "codex-iter0.json", _reply(_INCIDENT))

        result = _run(bash_block_after(_mode("vitality-adversarial-rework.md"), _REWORK_MARKER), tmp_path)

        assert "⊘ codex-blocked:quota — challenger-only from here, challenger prompt gets --no-codex" in result.stdout
        assert "⏱ codex iter0 timed_out" not in result.stdout
        assert (review_dir / "codex-quota-blocked.txt").exists()

    @_skip_no_bash
    def test_other_missing_result_stays_timed_out(self, tmp_path: Path, review_dir: Path) -> None:
        """A rate-limited Codex call keeps today's timed-out report and leaves Codex enabled for the next pass."""
        _put(tmp_path / "launch" / _INCIDENT, json.dumps({"fault": "rate-limited"}))
        _put(review_dir / "codex-iter0.json", _reply(_INCIDENT))

        result = _run(bash_block_after(_mode("vitality-adversarial-rework.md"), _REWORK_MARKER), tmp_path)

        assert "⏱ codex iter0 timed_out — no .done sentinel" in result.stdout
        assert not (review_dir / "codex-quota-blocked.txt").exists()

    def test_later_passes_run_challenger_only_and_report_the_stop(self) -> None:
        """The rework text skips the bridge review once the marker exists and names the outcome in the report."""
        rework = _mode("vitality-adversarial-rework.md")
        step5 = _mode("vitality-codex-review.md")

        assert "**When CODEX_AVAILABLE=0 or `$REVIEW_DIR/codex-quota-blocked.txt` exists**" in rework
        assert '"⊘ codex-blocked:quota — later passes challenger-only"' in rework
        assert "⊘ codex-blocked:quota — single-pass analysis only" in step5
        assert "`rate-limited`, `capacity` and every other fault keep today's handling" in step5

    def test_bridge_review_runs_only_while_the_marker_is_absent(self) -> None:
        """The dispatch branch that calls ``bridge:review`` is gated on the quota marker being absent.

        Deleting that guard while keeping the challenger-only heading would let every later pass call Codex again.
        """
        lines = _mode("vitality-adversarial-rework.md").splitlines()

        assert any(
            line.startswith("**When CODEX_AVAILABLE=1**")
            and "and `$REVIEW_DIR/codex-quota-blocked.txt` is absent" in line
            and "call the bridge review" in line
            for line in lines
        )

    def test_challenger_gets_no_codex_after_the_stop(self) -> None:
        """The step 1 challenger prompt gains ``--no-codex`` once the marker exists, so it skips its own Codex call."""
        lines = _mode("vitality-adversarial-rework.md").splitlines()

        assert any("`$REVIEW_DIR/codex-quota-blocked.txt`" in line and "`--no-codex`" in line for line in lines)


class TestChallengerCrossCheckQuotaStop:
    """A challenger whose own Codex cross-check stopped on quota ends Codex review for the rest of the run.

    The challenger's ``bridge:review`` reply never reaches the orchestrator; only its envelope does, persisted per
    iteration, with a top-level ``codex_blocked`` of ``"quota"``. The run's own bridge review may still have finished.
    """

    @_skip_no_bash
    @pytest.mark.parametrize(
        "codex_file",
        [
            pytest.param("codex-iter0.done", id="codex-review-finished"),
            pytest.param("unrelated.txt", id="codex-missing"),
        ],
    )
    def test_quota_envelope_records_the_stop(self, tmp_path: Path, review_dir: Path, codex_file: str) -> None:
        """The envelope field writes the run marker and the blocked line, never a timeout.

        The run's own bridge review may have finished or not; either way later passes run challenger-only.
        """
        _put(review_dir / "challenger-iter0.json", json.dumps({"verdict": "pass", "codex_blocked": "quota"}))
        _put(review_dir / codex_file, "done\n")

        result = _run(bash_block_after(_mode("vitality-adversarial-rework.md"), _REWORK_MARKER), tmp_path)

        assert "⊘ codex-blocked:quota — challenger-only from here, challenger prompt gets --no-codex" in result.stdout
        assert "⏱ codex iter0 timed_out" not in result.stdout
        assert (review_dir / "codex-quota-blocked.txt").read_text(encoding="utf-8") == "codex-blocked:quota\n"

    @_skip_no_bash
    @pytest.mark.parametrize(
        ("opening", "stopped"),
        [
            pytest.param(_CROSS_CHECK_QUOTA, True, id="anchored-opening-line"),
            pytest.param(f"Codex agrees.\n{_CROSS_CHECK_QUOTA}", False, id="token-below-the-opening-line"),
        ],
    )
    def test_reply_without_envelope_uses_the_second_carrier(
        self, tmp_path: Path, review_dir: Path, opening: str, stopped: bool
    ) -> None:
        """A persisted reply with no JSON envelope decides by its Cross-Check opening line alone."""
        _put(review_dir / "challenger-iter0.json", f"## Challenge: vitality\n\n## Codex Cross-Check\n\n{opening}\n")
        _put(review_dir / "codex-iter0.done", "done\n")

        result = _run(bash_block_after(_mode("vitality-adversarial-rework.md"), _REWORK_MARKER), tmp_path)

        assert (review_dir / "codex-quota-blocked.txt").exists() is stopped
        assert ("⊘ codex-blocked:quota — challenger-only from here" in result.stdout) is stopped

    @_skip_no_bash
    @pytest.mark.parametrize(
        "envelope",
        [
            pytest.param({"verdict": "pass"}, id="field-absent"),
            pytest.param({"verdict": "pass", "codex_blocked": "capacity"}, id="capacity"),
            pytest.param({"verdict": "pass", "summary": "Codex quota exhausted: usage limit"}, id="quota-wording-only"),
        ],
    )
    def test_other_envelopes_keep_codex_on(self, tmp_path: Path, review_dir: Path, envelope: dict) -> None:
        """Without the field, a finished Codex review reports neither a stop nor a timeout and Codex stays on."""
        _put(review_dir / "challenger-iter0.json", json.dumps(envelope))
        _put(review_dir / "codex-iter0.done", "done\n")

        result = _run(bash_block_after(_mode("vitality-adversarial-rework.md"), _REWORK_MARKER), tmp_path)

        assert "codex-blocked:quota" not in result.stdout
        assert "⏱ codex iter0 timed_out" not in result.stdout
        assert not (review_dir / "codex-quota-blocked.txt").exists()

    def test_envelope_is_persisted_for_the_check(self) -> None:
        """Step 1 tells the orchestrator to persist the challenger envelope the sentinel check reads."""
        lines = _mode("vitality-adversarial-rework.md").splitlines()

        assert any(
            "`$REVIEW_DIR/challenger-iter${REWORK_ITER}.json`" in line and "`codex_blocked`" in line for line in lines
        )

    def test_final_codex_findings_survive_a_challenger_stop(self) -> None:
        """The report keeps the last iteration's Codex findings when the challenger's stop came beside them.

        Before this stop source existed the marker meant no Codex result for the final iteration; now the run's own
        review can finish in the same iteration the challenger reports the stop.
        """
        lines = _mode("vitality-adversarial-rework.md").splitlines()

        assert any(
            '"⊘ codex-blocked:quota — later passes challenger-only"' in line
            and "after the final iteration's findings when its `.done` exists" in line
            for line in lines
        )

    @pytest.mark.skipif(shutil.which("node") is None, reason="the blueprint hook runs under node")
    def test_check_block_is_auto_allowed_verbatim(self) -> None:
        """The edited sentinel check stays an exact blueprint-manifest hit, so no rework iteration parks on a prompt."""
        block = bash_block_after(_mode("vitality-adversarial-rework.md"), _REWORK_MARKER)
        command = block.replace("${CLAUDE_PLUGIN_ROOT}", _PLUGIN.resolve().as_posix())
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}}

        proc = subprocess.run(
            ["node", str(_PLUGIN / "hooks/blueprint-allow.js")],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "allow"
