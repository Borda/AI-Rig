"""Guard the review's Codex quota stops: once the peer is out of credits, no later Codex call in the run.

The Step 1 gate challenger runs its own Codex cross-check. When that call stops on quota, its reply says so, the
orchestrator keeps an in-run flag, and Step 2 sends no bridge review. A Step 2 bridge review can also stop on a harness
quota incident. Either way a Step 4 ``foundry:challenger`` verifier would otherwise make its own Codex call that cannot
succeed, so its prompt carries ``--no-codex``. Agent 7 always runs with ``--no-codex`` and needs no quota rule.
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
_REVIEW = _PLUGIN / "skills/review"
_SESSION = "review-quota"
#: Run directory as the launch writes it into the run-dir sentinel: relative to the launch directory.
_RUN_DIR = ".temp/review/2026-11-16T10-00-00Z"
_INCIDENT = ".temp/bridge/incidents/1700000000000000000-0a0b0c0d-quota-exhausted.json"
#: One-line Step 2 record the launch writes when the gate flag skipped the bridge review.
_GATE_RECORD = (
    "⊘ codex-blocked:quota — gate challenger's Codex cross-check stopped on quota; bridge review not launched\n"
)
#: Prose right above the Step 4 quota block.
_STEP4_MARKER = "Before composing verifier prompts, run this block once, unedited"
_BASH = shutil.which("bash")
_skip_no_bash = pytest.mark.skipif(_BASH is None, reason="review blocks run under bash")


def _lines(relative: str) -> list[str]:
    """Return one review mode file as lines."""
    return (_REVIEW / relative).read_text(encoding="utf-8").splitlines()


def _rule(relative: str, lead: str) -> list[str]:
    """Return a bold-lead rule line and the bullets that follow it, up to the next paragraph."""
    lines = _lines(relative)
    start = next(index for index, line in enumerate(lines) if line.startswith(lead))
    end = next(
        index for index in range(start + 2, len(lines)) if not lines[index].startswith("- ") and lines[index].strip()
    )
    return lines[start:end]


def _put(path: Path, content: str) -> None:
    """Write LF-only UTF-8 text, creating parent directories, as the Write tool would."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def _reply(incident: str | None) -> str:
    """Return a stopped bridge:review reply whose verdict already carries the quota wording."""
    verdict = "Codex quota exhausted: usage limit reached"
    return json.dumps({"status": "blocked", "verdict": verdict, "findings": [], "files_touched": [], "remaining": [],
                       "blockers": [verdict], "incident": incident})  # fmt: skip


def _seed(run_dir: Path, files: dict[str, str]) -> None:
    """Write the Step 2 records a run left in its run directory, by file name."""
    for name, content in files.items():
        _put(run_dir / name, content)


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    """Create the launch directory, its run directory and the session's run-dir sentinel pointing at it."""
    run = tmp_path / "launch" / _RUN_DIR
    run.mkdir(parents=True)
    _put(tmp_path / f"oss-review-run-dir-{_SESSION}", f"{_RUN_DIR}\n")
    return run


def _run_step4_check(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Run the Step 4 quota block from the launch directory with the session token and a scratch TMPDIR."""
    text = (_REVIEW / "modes/post-agent.md").read_text(encoding="utf-8")
    env = os.environ | {
        "CLAUDE_CODE_SESSION_ID": _SESSION,
        "CLAUDE_PLUGIN_ROOT": str(_PLUGIN),
        "TMPDIR": str(tmp_path),
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}",
    }
    script = render_shell(bash_block_after(text, _STEP4_MARKER), _PLUGIN, "oss")
    return subprocess.run(
        [_BASH, "-c", script], cwd=tmp_path / "launch", env=env, capture_output=True, text=True, check=False
    )


class TestGateQuotaFlag:
    """The acceptance gate turns its challenger's quota signal into an in-run flag Step 2 and Step 4 obey."""

    def test_gate_reads_either_challenger_carrier_into_an_in_run_flag(self) -> None:
        """The gate sets the flag from the envelope key or the anchored Cross-Check line, never from prose.

        Without the flag rule, a quota stop inside the gate challenger stays invisible to Step 2.
        """
        lines = _lines("modes/acceptance-gate.md")

        assert any(line.startswith("**Codex quota flag — in-run by design") for line in lines)
        assert any(
            '`"codex_blocked": "quota"`' in line
            and "`⚠ codex-blocked:quota`" in line
            and "`GATE_CODEX=quota-blocked`" in line
            for line in lines
        )
        assert any("Never infer the stop from rationale prose" in line for line in lines)

    def test_gate_flag_skips_the_co_review_and_verifier_cross_checks(self) -> None:
        """The flag drops the Step 2 bridge review and the Codex cross-check of Step 4 challenger verifiers.

        Agent 7 already runs with ``--no-codex`` on every review, so the rule names only the calls the flag changes.
        """
        rule = _rule("modes/acceptance-gate.md", "**Codex quota flag — in-run by design")

        assert any(
            line.startswith("- Step 2 then makes no `bridge:review` call")
            and "`foundry:challenger` verifier" in line
            and "`--no-codex`" in line
            and "`post-agent.md`" in line
            for line in rule
        )

    def test_launch_skips_codex_and_tells_why_after_a_gate_quota_stop(self) -> None:
        """Step 2 drops the bridge review and leaves a one-line reason for the report and for Step 4."""
        rule = _rule("modes/agent-launch.md", "**Gate Codex quota flag.**")

        assert "`GATE_CODEX=quota-blocked`" in rule[0]
        assert "`post-agent.md`" in rule[0]
        assert any(line.startswith("- No `bridge:review` call, even with `CODEX_AVAILABLE=1`") for line in rule)
        assert any("`$RUN_DIR/foundry--codex.md`" in line and _GATE_RECORD.split(" — ", 1)[0] in line for line in rule)


class TestStep4VerifierQuotaStop:
    """Step 4 sends challenger verifiers ``--no-codex`` after a gate stop or a co-review quota incident."""

    def test_launch_persists_the_co_review_reply(self) -> None:
        """The bridge reply is kept verbatim as JSON, so Step 4 classifies its harness incident from disk."""
        lines = _lines("modes/agent-launch.md")

        assert any(
            "Write the returned JSON object verbatim to `$RUN_DIR/codex-review-reply.json`" in line for line in lines
        )

    def test_rule_sends_challenger_verifiers_no_codex(self) -> None:
        """The block's verdict or the in-context gate flag ends every challenger verifier prompt with ``--no-codex``."""
        lines = _lines("modes/post-agent.md")

        assert any(
            "`REVIEW_CODEX=quota-blocked`" in line
            and "`GATE_CODEX=quota-blocked`" in line
            and "`foundry:challenger` verifier prompt ends with the line `--no-codex`" in line
            for line in lines
        )

    @_skip_no_bash
    @pytest.mark.parametrize(
        "files",
        [
            pytest.param(
                {"codex-review-reply.json": _reply(_INCIDENT), "foundry--codex.md": "Codex findings: none\n"},
                id="co-review-quota-incident",
            ),
            pytest.param({"foundry--codex.md": _GATE_RECORD}, id="gate-stop-record-without-a-reply"),
        ],
    )
    def test_quota_stop_drops_codex_from_challenger_verifiers(
        self, tmp_path: Path, run_dir: Path, files: dict[str, str]
    ) -> None:
        """A harness quota incident on the co-review, or the launch's gate record, marks the run as out of credits.

        The gate record is the on-disk trace of the in-context gate flag, so the stop survives a compaction between the
        launch and Step 4.
        """
        _put(tmp_path / "launch" / _INCIDENT, json.dumps({"schema_version": 2, "fault": "quota-exhausted"}))
        _seed(run_dir, files)

        result = _run_step4_check(tmp_path)

        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout.startswith("REVIEW_CODEX=quota-blocked"), result.stdout

    @_skip_no_bash
    @pytest.mark.parametrize(
        ("fault", "files"),
        [
            pytest.param(
                "quota-exhausted", {"codex-review-reply.json": _reply(None)}, id="model-authored-null-incident"
            ),
            pytest.param("rate-limited", {"codex-review-reply.json": _reply(_INCIDENT)}, id="rate-limited"),
            pytest.param("capacity", {"codex-review-reply.json": _reply(_INCIDENT)}, id="capacity"),
            pytest.param(
                "quota-exhausted",
                {"codex-review-reply.json": _reply(None), "foundry--codex.md": _GATE_RECORD},
                id="reply-decides-over-a-peer-written-line",
            ),
            pytest.param("quota-exhausted", {}, id="no-co-review-ran"),
            pytest.param(
                "quota-exhausted", {"foundry--codex.md": "⏱ bridge review timed_out\n"}, id="co-review-timed-out"
            ),
            pytest.param(
                "quota-exhausted",
                {"foundry--codex.md": f"Codex: {_GATE_RECORD}"},
                id="gate-wording-not-opening-the-record",
            ),
        ],
    )
    def test_other_records_keep_codex(self, tmp_path: Path, run_dir: Path, fault: str, files: dict[str, str]) -> None:
        """Model-authored quota wording, other faults, a timeout or no co-review leave verifier prompts unchanged.

        A persisted reply decides alone: when Codex returned one, the Codex record may hold peer text, so its opening
        line never counts.
        """
        _put(tmp_path / "launch" / _INCIDENT, json.dumps({"fault": fault}))
        _seed(run_dir, files)

        result = _run_step4_check(tmp_path)

        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout == "REVIEW_CODEX=none\n"

    @_skip_no_bash
    def test_missing_run_dir_sentinel_blocks(self, tmp_path: Path) -> None:
        """Without the run-dir sentinel the check cannot read the Step 2 record, so it stops visibly."""
        (tmp_path / "launch").mkdir()

        result = _run_step4_check(tmp_path)

        assert (result.returncode, result.stdout.startswith("! BLOCKED")) == (1, True), result.stdout + result.stderr

    @pytest.mark.skipif(shutil.which("node") is None, reason="the blueprint hook runs under node")
    def test_check_block_is_auto_allowed_verbatim(self) -> None:
        """The Step 4 quota block is an exact blueprint-manifest hit, so the verifier spawn never parks on a prompt."""
        text = (_REVIEW / "modes/post-agent.md").read_text(encoding="utf-8")
        command = bash_block_after(text, _STEP4_MARKER).replace("${CLAUDE_PLUGIN_ROOT}", _PLUGIN.resolve().as_posix())
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
