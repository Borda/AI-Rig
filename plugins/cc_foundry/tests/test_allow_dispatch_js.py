"""Tests for ``hooks/allow-dispatch.js``, the single registered Bash auto-allow hook.

Byte-parity with the two decision modules lives in ``test_hook_stdout_captures.py``, which replays a frozen capture.
This suite covers what the dispatcher adds on top of that: rank order, isolation between the two lanes, the audit row it
appends, and the promise that no audit outcome can reach the decision.

The three accepted behaviour deltas are asserted where they are observable and left alone where they are not. In
particular, nothing here claims the host accepted a payload — a subprocess cannot see that — and nothing tests the
shared timeout boundary, which is a documented delta rather than a property.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import build_blueprint_manifest as bbm
from _audit_harness import install


DISPATCH_HOOK = "allow-dispatch.js"
SENTINEL_HOOK = "sentinel-read-allow.js"
LIBRARY = Path("lib") / "audit-log.js"

#: A command the shape module allows on its own, used wherever a shape allow is needed.
SHAPE_ALLOWED = 'V=$(cat "${TMPDIR:-/tmp}/x-${CSID}")'

NODE_UNAVAILABLE = shutil.which("node") is None
_skip_node_unavailable = pytest.mark.skipif(NODE_UNAVAILABLE, reason="requires node to execute the hook")


def _payload(command: str, **overrides: object) -> dict:
    """Return a PreToolUse stdin payload carrying ``command`` and the identifiers the record joins on."""
    return {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "session-under-test",
        "tool_use_id": "toolu_under_test",
        "cwd": "/repo/under/test",
        **overrides,
    }


def _manifest_for(command: str) -> bytes:
    """Return a manifest whose single entry is the digest of ``command``."""
    entries = {bbm.sha256_text(bbm.normalize(command)): {"kind": "block", "src": "skills/x/SKILL.md:1"}}
    return bbm.encode_manifest({"schema": 1, "plugin": "cc_foundry@0.0.0", "entries": entries})


def _verdicts(row: dict) -> dict[str, dict]:
    """Return a row's per-lane verdicts keyed by lane."""
    return {entry["lane"]: entry for entry in row["action_detail"]["verdicts"]}


@pytest.fixture(name="env")
def _env(tmp_path: Path):
    """Return an isolated plugin root and home with no blueprint manifest installed."""
    return install(tmp_path)


@_skip_node_unavailable
class TestEffectiveVerdict:
    """One row per call, recording the effective decision and both lanes' raw verdicts."""

    def test_blueprint_allow_records_rank_src_and_both_lanes(self, tmp_path: Path) -> None:
        """A blueprint allow is rank 1, carries its provenance, and still records what the shape lane said."""
        command = "git rev-parse --show-toplevel"
        env = install(tmp_path, manifest=_manifest_for(command))
        assert env.run(DISPATCH_HOOK, _payload(command)).returncode == 0
        (row,) = env.rows()
        detail = row["action_detail"]
        assert (detail["decision"], detail["lane"], detail["rank"]) == ("allow", "blueprint", 1)
        assert detail["src"] == "skills/x/SKILL.md:1"
        assert row["trust_level"] == "plugin"
        assert _verdicts(row)["shape"]["decision"] == "passthrough"

    def test_shape_only_allow_is_rank_two_and_carries_no_src(self, env) -> None:
        """A shape allow records the lane that produced it and claims no manifest provenance it does not have."""
        env.run(DISPATCH_HOOK, _payload(SHAPE_ALLOWED))
        (row,) = env.rows()
        detail = row["action_detail"]
        assert (detail["decision"], detail["lane"], detail["rank"]) == ("allow", "shape", 2)
        assert "src" not in detail

    def test_whitespace_only_command_splits_the_lanes(self, env) -> None:
        """The two modules genuinely disagree on whitespace, and the record keeps both answers distinct.

        Blueprint normalizes and reaches ``no-match``; shape trims and returns before examining anything. One is an
        abstention, the other is the absence of an opinion, and collapsing them would lose the difference.
        """
        env.run(DISPATCH_HOOK, _payload("   "))
        (row,) = env.rows()
        verdicts = _verdicts(row)
        assert verdicts["blueprint"]["decision"] == "passthrough"
        assert verdicts["shape"] == {"lane": "shape", "decision": "none", "why": "not-applicable"}

    @pytest.mark.parametrize("command", [42, ["ls"], {"cmd": "ls"}, True], ids=["int", "list", "object", "bool"])
    def test_a_non_string_command_is_none_not_module_error(self, env, command) -> None:
        """A malformed host payload must never be recorded as a broken module.

        ``module-error`` is the signal that a decision module could not be loaded or threw. A non-string ``command``
        used to reach ``.trim()`` and raise, which the dispatcher caught and filed under exactly that word — making a
        host payload problem indistinguishable in the log from a module that actually broke.
        """
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
        proc = env.run(DISPATCH_HOOK, payload)
        assert proc.stdout == b"" and proc.returncode == 0

        # Both lanes return `none` here, so no row is written — scanning `env.rows()` for a bad verdict would pass
        # whether the module answered cleanly or threw. The verdict has to be read from `evaluate` directly.
        script = (
            f"const m = require({json.dumps(str(env.root / 'hooks' / SENTINEL_HOOK))});"
            f"process.stdout.write(JSON.stringify(m.evaluate({json.dumps(payload)})));"
        )
        result = subprocess.run(
            ["node", "-e", script], capture_output=True, encoding="utf-8", timeout=30, check=False, env=env.env()
        )
        assert result.returncode == 0, result.stderr
        verdict = json.loads(result.stdout)
        assert verdict["decision"] == "none"
        assert verdict["why"] == "not-applicable", f"{command!r} must not be filed as a module failure"

    def test_both_none_writes_no_row_at_all(self, env) -> None:
        """With neither lane reaching a decision there is no opinion to record, so nothing is written."""
        proc = env.run(DISPATCH_HOOK, _payload("anything", tool_name="Read"))
        assert proc.stdout == b"" and proc.returncode == 0
        assert env.rows() == []

    def test_digest_is_absent_when_the_blueprint_lane_never_normalized(self, tmp_path: Path) -> None:
        """No digest is invented for a command the blueprint lane never looked at.

        The digest is the blueprint normalizer's output, and that normalizer is the only one that exists. When the
        blueprint lane produces no verdict at all the field is omitted rather than filled from somewhere else — an
        absent digest is the honest record, not a gap to paper over.
        """
        env = install(tmp_path)
        env.run(DISPATCH_HOOK, _payload("   "))
        (decided,) = env.rows()
        assert "digest" in decided["action_detail"], "blueprint reached a decision here, so the digest belongs"

        broken = install(tmp_path / "broken")
        (broken.root / "hooks" / "blueprint-allow.js").write_text("throw new Error('boom');", encoding="utf-8")
        broken.run(DISPATCH_HOOK, _payload(SHAPE_ALLOWED))
        (row,) = broken.rows()
        assert _verdicts(row)["blueprint"]["why"] == "module-error"
        assert "digest" not in row["action_detail"]


@_skip_node_unavailable
class TestLaneIsolation:
    """One module's failure must never suppress the other."""

    @pytest.mark.parametrize(
        ("body", "case"),
        [
            pytest.param("module.exports = { evaluate() { throw new Error('boom'); } };", "throws", id="module-throws"),
            pytest.param("this is not javascript(", "unparsable", id="require-fails"),
        ],
    )
    def test_broken_shape_module_leaves_blueprint_working(self, tmp_path: Path, body: str, case: str) -> None:
        """A blueprint allow still reaches stdout, and the broken lane is recorded as ``module-error``.

        ``module-error`` is deliberately a ``none``, not a passthrough: a module that crashed did not abstain, it said
        nothing, and the reader must not count it as evidence that the command was examined.
        """
        command = "git rev-parse --show-toplevel"
        env = install(tmp_path, manifest=_manifest_for(command))
        (env.root / "hooks" / SENTINEL_HOOK).write_text(body, encoding="utf-8")
        proc = env.run(DISPATCH_HOOK, _payload(command))
        assert proc.returncode == 0
        assert b'"permissionDecision":"allow"' in proc.stdout
        (row,) = env.rows()
        assert _verdicts(row)["shape"] == {"lane": "shape", "decision": "none", "why": "module-error"}
        assert row["action_detail"]["decision"] == "allow", case

    def test_slow_shape_does_not_delay_an_already_decided_blueprint_allow(self, tmp_path: Path) -> None:
        """The blueprint allow is written before the shape lane is even required.

        This asserts the dispatcher's own output timing and nothing more. It is NOT a claim that the host accepted the
        bytes: a hook that exceeds its timeout has its entire output discarded, printed or not, and that residual risk
        is an accepted delta rather than something a subprocess could disprove.
        """
        command = "git rev-parse --show-toplevel"
        env = install(tmp_path, manifest=_manifest_for(command))
        stall_ms = 1500
        (env.root / "hooks" / SENTINEL_HOOK).write_text(
            "module.exports = { evaluate() {"
            f"  const until = Date.now() + {stall_ms};"
            "  while (Date.now() < until) {}"
            "  return { lane: 'shape', rank: 2, payload: null, decision: 'passthrough', why: 'shape-mismatch' };"
            "} };",
            encoding="utf-8",
        )
        started = time.monotonic()
        with subprocess.Popen(
            ["node", str(env.root / "hooks" / DISPATCH_HOOK)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env.env(),
        ) as proc:
            proc.stdin.write(json.dumps(_payload(command)).encode("utf-8"))
            proc.stdin.close()
            first_byte = proc.stdout.read(1)
            elapsed_to_first_byte = time.monotonic() - started
            proc.wait(timeout=30)
            total = time.monotonic() - started
        assert first_byte == b"{"
        assert total >= stall_ms / 1000, "the stub must actually have stalled, or this proves nothing"
        assert elapsed_to_first_byte < total - 0.5


@_skip_node_unavailable
class TestAuditNeverChangesTheDecision:
    """Logging is downstream of the decision in every sense that matters."""

    def test_kill_switch_leaves_stdout_identical_and_never_loads_the_library(self, tmp_path: Path) -> None:
        """``RIG_AUDIT=0`` disables logging only.

        The library is not even required, so it cannot fail during load.
        """
        env = install(tmp_path, manifest=_manifest_for(SHAPE_ALLOWED))
        (env.root / "hooks" / LIBRARY).write_text("throw new Error('library must not be required');", encoding="utf-8")
        proc = env.run(DISPATCH_HOOK, _payload(SHAPE_ALLOWED), RIG_AUDIT="0")
        assert proc.returncode == 0
        assert b'"permissionDecision":"allow"' in proc.stdout
        assert not env.audit_dir.exists()

    def test_library_that_throws_on_require_does_not_change_stdout(self, tmp_path: Path) -> None:
        """With audit enabled and the library broken, the decision is unaffected and the hook still exits 0."""
        env = install(tmp_path, manifest=_manifest_for(SHAPE_ALLOWED))
        healthy = env.run(DISPATCH_HOOK, _payload(SHAPE_ALLOWED), RIG_AUDIT="0")
        (env.root / "hooks" / LIBRARY).write_text("throw new Error('broken library');", encoding="utf-8")
        broken = env.run(DISPATCH_HOOK, _payload(SHAPE_ALLOWED))
        assert broken.stdout == healthy.stdout
        assert broken.returncode == 0

    def test_unwritable_log_directory_does_not_change_stdout(self, tmp_path: Path) -> None:
        """A log directory that cannot be created is a logging failure, never a permission one."""
        env = install(tmp_path, manifest=_manifest_for(SHAPE_ALLOWED))
        (env.home / ".claude").write_text("not a directory", encoding="utf-8")
        proc = env.run(DISPATCH_HOOK, _payload(SHAPE_ALLOWED))
        assert proc.returncode == 0
        assert b'"permissionDecision":"allow"' in proc.stdout


@_skip_node_unavailable
class TestPrivacyAndFootprint:
    """What the hook writes, and where."""

    def test_raw_command_text_never_reaches_the_log(self, env) -> None:
        """Only a digest is recorded.

        The limit is stated honestly in the docs; the file itself must be clean.
        """
        secret = 'echo "correct-horse-battery-staple"'
        env.run(DISPATCH_HOOK, _payload(secret))
        written = env.log_files()[0].read_text(encoding="utf-8")
        assert "correct-horse" not in written
        digest = hashlib.sha256(bbm.normalize(secret).encode("utf-8")).hexdigest()
        assert digest in written

    def test_touches_nothing_outside_the_audit_directory(self, env) -> None:
        """No state directory, no temp file, no lock, no marker — the append is the whole footprint.

        Three earlier designs coordinated through the filesystem and each was found unsound. Nothing coordinates now,
        and this asserts it stays that way.
        """
        env.run(DISPATCH_HOOK, _payload(SHAPE_ALLOWED))
        expected = {
            ".claude",
            ".claude/logs",
            ".claude/logs/audit",
            f".claude/logs/audit/s-{hashlib.sha256(b'session-under-test').hexdigest()[:32]}.jsonl",
        }
        assert env.created_paths() == expected

    def test_missing_session_id_goes_to_the_shared_stream_with_an_explicit_null(self, env) -> None:
        """A missing identifier is never replaced by a shared default such as ``default``."""
        env.run(DISPATCH_HOOK, _payload(SHAPE_ALLOWED, session_id=None, tool_use_id=None))
        assert [path.name for path in env.log_files()] == ["_no-session.jsonl"]
        (row,) = env.rows()
        assert row["session_id"] is None and row["tool_use_id"] is None


@_skip_node_unavailable
@pytest.mark.integration
class TestConcurrentWriters:
    """Several plugins append one file with nothing coordinating them."""

    def test_every_completed_append_verifies_its_own_hash(self, env) -> None:
        """Under concurrent appends, every line that parses must verify — torn framing is the only tolerated damage.

        Concurrency is exercised, not simulated: eight processes append one file at once, which is what four installed
        plugins do on a single Bash call with a subagent in flight.
        """
        writers = 8
        with ThreadPoolExecutor(max_workers=writers) as pool:
            results = [
                pool.submit(env.run, DISPATCH_HOOK, _payload(SHAPE_ALLOWED, tool_use_id=f"toolu_{index}"))
                for index in range(writers)
            ]
            assert [future.result().returncode for future in results] == [0] * writers

        (path,) = env.log_files()
        parsed, torn = [], 0
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                parsed.append(json.loads(line))
            except ValueError:
                torn += 1
        assert len(parsed) + torn == writers, "every writer must have appended exactly one line"
        for row in parsed:
            body = {key: value for key, value in row.items() if key != "record_hash"}
            canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            assert hashlib.sha256(canonical.encode("utf-8")).hexdigest() == row["record_hash"]
