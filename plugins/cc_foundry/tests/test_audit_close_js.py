"""Tests for ``hooks/audit-close.js``, the observation half of the audit log.

The design decision this suite defends is that there is no coordination. Four installed plugins each append their own
after-row for one completed Bash call, and that is corroboration rather than duplication: no claim file, no dedupe, no
tombstone, so no row can be lost to a race and no writer has to survive another's crash. Earlier revisions coordinated
through the filesystem and each mechanism was found unsound, so "four rows, all retained" is asserted directly.

The other load-bearing property is silence. ``SessionStart`` stdout is injected into the model's context and
``PostToolUse`` stdout can interfere with tool-result handling, so this hook prints nothing on any event — including the
events it ignores.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from _audit_harness import install


CLOSE_HOOK = "audit-close.js"
SESSION = "session-under-test"
TOOL_USE = "toolu_under_test"

NODE_UNAVAILABLE = shutil.which("node") is None
_skip_node_unavailable = pytest.mark.skipif(NODE_UNAVAILABLE, reason="requires node to execute the hook")


def _event(name: str, **overrides: object) -> dict:
    """Return a stdin payload for one hook event."""
    payload = {
        "hook_event_name": name,
        "session_id": SESSION,
        "cwd": "/repo/under/test",
        **overrides,
    }
    if name in ("PostToolUse", "PostToolUseFailure"):
        payload.setdefault("tool_name", "Bash")
        payload.setdefault("tool_use_id", TOOL_USE)
    return payload


@pytest.fixture(name="env")
def _env(tmp_path: Path):
    """Return an isolated plugin root and home."""
    return install(tmp_path)


@_skip_node_unavailable
class TestCompletionRows:
    """One after-row per completion event, carrying a status word and nothing else."""

    def test_success_row(self, env) -> None:
        """``PostToolUse`` on a Bash call records a successful completion."""
        assert env.run(CLOSE_HOOK, _event("PostToolUse")).returncode == 0
        (row,) = env.rows()
        assert row["action_type"] == "tool.bash"
        assert row["record_phase"] == "post_execution"
        assert row["action_detail"] == {"status": "ok", "event": "PostToolUse"}
        assert row["outcome"] == "success"

    def test_failure_row(self, env) -> None:
        """``PostToolUseFailure`` records a failed completion, and never a fabricated ``denied``."""
        env.run(CLOSE_HOOK, _event("PostToolUseFailure"))
        (row,) = env.rows()
        assert row["action_detail"] == {"status": "error", "event": "PostToolUseFailure"}
        assert row["outcome"] == "failure"

    def test_both_events_write_both_rows(self, env) -> None:
        """Whether the host can fire both events for one call is unestablished, so neither suppresses the other.

        With no dedupe this needs no protocol at all: a group holding a success and a failure is a reported warning
        for a reader to interpret, not a race for a writer to resolve.
        """
        env.run(CLOSE_HOOK, _event("PostToolUse"))
        env.run(CLOSE_HOOK, _event("PostToolUseFailure"))
        assert sorted(row["outcome"] for row in env.rows()) == ["failure", "success"]

    def test_authority_is_never_inferred_from_execution(self, env) -> None:
        """That a tool ran proves nothing about who approved it, so an after-row claims no authority.

        A sibling plugin's allow, a settings.json rule, or a permission mode all execute with no prompt. Writing
        ``human`` here would invent evidence.
        """
        env.run(CLOSE_HOOK, _event("PostToolUse"))
        (row,) = env.rows()
        assert row["trust_level"] == "unknown"

    def test_non_bash_completion_is_ignored(self, env) -> None:
        """This hook observes Bash calls; every other tool belongs to nothing here."""
        env.run(CLOSE_HOOK, _event("PostToolUse", tool_name="Read"))
        assert env.rows() == []

    def test_missing_tool_use_id_is_recorded_as_null(self, env) -> None:
        """An unjoinable row is still written.

        A dropped row is evidence lost; a null join key is evidence kept.
        """
        env.run(CLOSE_HOOK, _event("PostToolUse", tool_use_id=None))
        (row,) = env.rows()
        assert row["tool_use_id"] is None


@_skip_node_unavailable
class TestLifecycleRows:
    """Session boundaries, one row per plugin, with no shared bookkeeping."""

    @pytest.mark.parametrize(
        ("event", "action_type"),
        [("SessionStart", "session.start"), ("SessionEnd", "session.end")],
    )
    def test_lifecycle_row_is_written(self, env, event: str, action_type: str) -> None:
        """Each lifecycle event appends exactly one row and carries no tool identifier."""
        env.run(CLOSE_HOOK, _event(event))
        (row,) = env.rows()
        assert row["action_type"] == action_type
        assert row["outcome"] == "success"
        assert "tool_use_id" not in row

    def test_session_end_reason_is_recorded_when_present(self, env) -> None:
        """A reason the host supplies is kept; one it does not supply is not invented."""
        env.run(CLOSE_HOOK, _event("SessionEnd", reason="clear"))
        env.run(CLOSE_HOOK, _event("SessionStart"))
        rows = {row["action_type"]: row for row in env.rows()}
        assert rows["session.end"]["action_detail"] == {"reason": "clear"}
        assert rows["session.start"]["action_detail"] == {}

    def test_build_record_is_callable_with_the_identity_close_passes_it(self, env) -> None:
        """Pin the ``identity`` contract, which is a seam nothing else exercises directly.

        ``buildRecord`` is exported so it can be tested without a filesystem, but every existing test reaches it through
        ``close()``. When it gained a function-valued ``usableId`` member, a caller still passing the old two-member
        identity would throw inside the lifecycle branch — and ``close()``'s catch-all would swallow it, silently
        dropping every lifecycle row while completion rows kept working.
        """
        script = (
            f"const c = require({json.dumps(str(env.root / 'hooks' / CLOSE_HOOK))});"
            "const identity = {plugin: 'cc_foundry', version: '0.0.0',"
            " usableId: (v) => (typeof v === 'string' && v.length ? v : null)};"
            "const end = c.buildRecord({hook_event_name: 'SessionEnd', session_id: 's', reason: 'clear'}, identity);"
            "const bad = c.buildRecord({hook_event_name: 'SessionEnd', session_id: '', reason: 'x'}, identity);"
            "process.stdout.write(JSON.stringify({end: end && end.action_type, detail: end && end.action_detail,"
            " bad: bad}));"
        )
        proc = subprocess.run(
            ["node", "-e", script], capture_output=True, encoding="utf-8", timeout=30, check=False, env=env.env()
        )
        assert proc.returncode == 0, proc.stderr
        built = json.loads(proc.stdout)
        assert built["end"] == "session.end"
        assert built["detail"] == {"reason": "clear"}
        assert built["bad"] is None

    @pytest.mark.parametrize("event", [["PostToolUse"], ["SessionEnd"], 42, None, {"a": 1}])
    def test_a_non_string_event_writes_nothing(self, env, event) -> None:
        """A property lookup coerces its key, so ``["PostToolUse"]`` would pass the membership tests unnoticed.

        The row it produced carried the array verbatim in ``action_detail.event``, which no reader can classify — and
        the reader reached a membership test with it and raised, ending the whole verification run.
        """
        env.run(CLOSE_HOOK, {"hook_event_name": event, "tool_name": "Bash", "tool_use_id": "t", "session_id": "s"})
        assert env.rows() == []

    def test_session_start_never_carries_a_reason(self, env) -> None:
        """``reason`` is a session.end field, and a session.start carrying one is rejected by the reader.

        The host sends ``source`` on SessionStart today, so this is latent rather than live — but the day a ``reason``
        appears there, copying it would put a schema-invalid row in the log once per plugin on every session start.
        """
        env.run(CLOSE_HOOK, _event("SessionStart", reason="clear"))
        (row,) = env.rows()
        assert row["action_type"] == "session.start"
        assert row["action_detail"] == {}

    @pytest.mark.parametrize("session_id", [None, "", "lone-surrogate-\ud800-here"])
    def test_no_lifecycle_row_without_a_usable_session_id(self, env, session_id) -> None:
        """A lifecycle row exists to bound one session's records; with no session there is nothing to bound.

        Usable means the library's own definition, ill-formed UTF-16 included. An id that passes a length check but
        fails that one seals to null and routes to the shared ``_no-session.jsonl`` — which would give that shared
        stream a session boundary belonging to no session, the exact outcome this guard exists to prevent.
        """
        env.run(CLOSE_HOOK, _event("SessionStart", session_id=session_id))
        env.run(CLOSE_HOOK, _event("SessionEnd", session_id=session_id))
        assert env.rows() == []
        assert env.log_files() == []

    def test_completion_row_still_written_without_a_session_id(self, env) -> None:
        """A completion observation is worth keeping even when it cannot be joined; it lands in the shared stream."""
        env.run(CLOSE_HOOK, _event("PostToolUse", session_id=None))
        assert [path.name for path in env.log_files()] == ["_no-session.jsonl"]


@_skip_node_unavailable
class TestFourPluginsCorroborate:
    """Every plugin writes its own row; none of them is a duplicate to suppress."""

    def test_four_plugins_produce_four_retained_rows(self, tmp_path: Path) -> None:
        """Four independent observations of one completion are all kept, and each names its own writer.

        The plan's earlier revisions used a claim file so that only one closer wrote. Every version of that mechanism
        was found racy; removing it made the row count go up by three and the failure modes go down to zero.
        """
        home = tmp_path / "shared-home"
        home.mkdir()
        rows = []
        for name, version in (("foundry", "1.0.0"), ("oss", "2.0.0"), ("develop", "3.0.0"), ("research", "4.0.0")):
            env = install(tmp_path / name, name=name, version=version)
            proc = env.run(CLOSE_HOOK, _event("PostToolUse"), HOME=str(home), USERPROFILE=str(home))
            assert proc.returncode == 0
        for path in sorted((home / ".claude" / "logs" / "audit").glob("*.jsonl")):
            rows += [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

        assert len(rows) == 4
        assert sorted(row["agent_id"] for row in rows) == [
            "cc_develop/audit-close",
            "cc_foundry/audit-close",
            "cc_oss/audit-close",
            "cc_research/audit-close",
        ]
        assert sorted(row["agent_version"] for row in rows) == ["1.0.0", "2.0.0", "3.0.0", "4.0.0"]


@_skip_node_unavailable
class TestSilenceAndFootprint:
    """The hook prints nothing and touches nothing but its log."""

    @pytest.mark.parametrize(
        "event",
        ["PostToolUse", "PostToolUseFailure", "SessionStart", "SessionEnd", "Stop", "PreCompact", "UserPromptSubmit"],
    )
    def test_stdout_is_empty_on_every_event(self, env, event: str) -> None:
        """``SessionStart`` stdout is injected into the model's context; nothing this hook knows belongs there."""
        proc = env.run(CLOSE_HOOK, _event(event))
        assert proc.stdout == b""
        assert proc.returncode == 0

    def test_unhandled_events_write_nothing(self, env) -> None:
        """An event this hook does not handle produces no record at all."""
        env.run(CLOSE_HOOK, _event("PreCompact"))
        assert env.rows() == []

    def test_malformed_stdin_never_crashes(self, env) -> None:
        """A logging hook must not interfere with execution, whatever arrives on stdin."""
        for payload in ("not json", "", "null", "[]"):
            proc = env.run(CLOSE_HOOK, payload)
            assert proc.returncode == 0 and proc.stdout == b""

    def test_creates_and_removes_nothing_outside_the_log(self, env) -> None:
        """No state directory, no tombstone, no sweep — a session teardown has nothing here to collide with."""
        env.run(CLOSE_HOOK, _event("SessionStart"))
        env.run(CLOSE_HOOK, _event("PostToolUse"))
        env.run(CLOSE_HOOK, _event("SessionEnd"))
        expected = {
            ".claude",
            ".claude/logs",
            ".claude/logs/audit",
            f".claude/logs/audit/s-{hashlib.sha256(SESSION.encode()).hexdigest()[:32]}.jsonl",
        }
        assert env.created_paths() == expected

    def test_kill_switch_disables_writing_only(self, env) -> None:
        """``RIG_AUDIT=0`` writes nothing and still exits 0."""
        proc = env.run(CLOSE_HOOK, _event("PostToolUse"), RIG_AUDIT="0")
        assert proc.returncode == 0
        assert not env.audit_dir.exists()
