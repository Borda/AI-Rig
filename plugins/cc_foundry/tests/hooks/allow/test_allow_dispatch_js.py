"""Tests for ``hooks/allow-dispatch.js``, the single registered Bash auto-allow hook.

Byte-parity with the blueprint and shape modules lives in ``test_hook_stdout_captures.py``, which replays a frozen
capture. This suite covers what the dispatcher adds on top of that: rank order across the three lanes (blueprint, shape,
gh-read), isolation between them, the audit row it appends, and the promise that no audit outcome can reach the
decision.

The three accepted behaviour deltas are asserted where they are observable and left alone where they are not. In
particular, nothing here claims the host accepted a payload — a subprocess cannot see that — and nothing tests the
shared timeout boundary, which is a documented delta rather than a property.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import build_blueprint_manifest as bbm
import pytest
from _audit_harness import AuditEnv, install
from _grant_repo import make_repo

DISPATCH_HOOK = "allow-dispatch.js"
SENTINEL_HOOK = "sentinel-read-allow.js"
GH_READ_HOOK = "github-read-allow.js"
VERIFIER = Path(__file__).resolve().parents[3] / "bin" / "verify_blueprint_audit.py"
LIBRARY = Path("lib") / "audit-log.js"

#: A command the shape module allows on its own, used wherever a shape allow is needed.
SHAPE_ALLOWED = 'V=$(cat "${TMPDIR:-/tmp}/x-${CSID}")'
#: A command the gh-read module allows under a grant, and neither other lane allows without a manifest entry.
GH_READ = "gh pr view 12 --json title"
#: A command only the blueprint lane can allow, and only when the manifest lists it.
BLUEPRINT_ALLOWED = "git rev-parse --show-toplevel"
#: ``command`` values the host should never send; the lanes must treat them as "no opinion", not as a broken module.
NON_STRING_COMMANDS = [42, pytest.param(["ls"], id="list"), pytest.param({"cmd": "ls"}, id="object"), True]

NODE_UNAVAILABLE = shutil.which("node") is None
_skip_node_unavailable = pytest.mark.skipif(NODE_UNAVAILABLE, reason="requires node to execute the hook")
_skip_git_unavailable = pytest.mark.skipif(shutil.which("git") is None, reason="requires git to resolve the grant")


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


def _remove_hooks(env: AuditEnv, *hooks: str) -> None:
    """Delete the named hook modules from an isolated plugin root."""
    for hook in hooks:
        (env.root / "hooks" / hook).unlink()


def _read_rows(path: Path) -> tuple[list[dict], int]:
    """Return the JSON rows of one log file and the number of non-blank lines that are not valid JSON."""
    parsed, torn = [], 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            parsed.append(json.loads(line))
        except ValueError:
            torn += 1
    return parsed, torn


def _hash_verifies(row: dict) -> bool:
    """Return whether a record's ``record_hash`` matches the canonical form of the rest of the record."""
    body = {key: value for key, value in row.items() if key != "record_hash"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest() == row["record_hash"]


@pytest.fixture(name="env")
def _env(tmp_path: Path) -> AuditEnv:
    """Return an isolated plugin root and home with no blueprint manifest installed."""
    return install(tmp_path)


@pytest.fixture(name="blueprint_env")
def _blueprint_env(tmp_path: Path) -> AuditEnv:
    """Return an isolated plugin root whose manifest blesses ``BLUEPRINT_ALLOWED``."""
    return install(tmp_path, manifest=_manifest_for(BLUEPRINT_ALLOWED))


@pytest.fixture(name="dual_allow_env")
def _dual_allow_env(tmp_path: Path) -> AuditEnv:
    """Return an isolated plugin root whose manifest blesses ``SHAPE_ALLOWED``, which the shape lane also allows."""
    return install(tmp_path, manifest=_manifest_for(SHAPE_ALLOWED))


@_skip_node_unavailable
@pytest.mark.integration
class TestAuditRoundTrip:
    """Real dispatcher rows verify through the shipped verifier CLI."""

    @pytest.mark.parametrize(
        ("removed", "lanes", "allowed"),
        [
            pytest.param((), ["blueprint", "shape", "gh-read"], True, id="three-lanes"),
            pytest.param((GH_READ_HOOK,), ["blueprint", "shape"], False, id="gh-read-absent"),
        ],
    )
    def test_rows_verify_with_each_installed_lane(
        self, env: AuditEnv, removed: tuple[str, ...], lanes: list[str], allowed: bool
    ) -> None:
        """Real dispatcher rows verify with three lanes in Foundry and two when gh-read is absent.

        A sibling plugin ships only the blueprint and shape modules, so the row lists two verdicts there. Both shapes
        must pass the shipped verifier's schema and hash checks, and only the gh-read-bearing root may allow the read.
        """
        _remove_hooks(env, *removed)
        proc = env.run(DISPATCH_HOOK, _payload(GH_READ))
        assert proc.returncode == 0
        assert bool(proc.stdout) is allowed
        (row,) = env.rows()
        verdicts = row["action_detail"]["verdicts"]
        assert [entry["lane"] for entry in verdicts] == lanes
        assert all(entry.get("why") != "module-error" for entry in verdicts)
        verified = subprocess.run(
            [sys.executable, str(VERIFIER), "verify", str(env.audit_dir), "--json"],
            capture_output=True,
            text=True,
            env=env.env(),
            timeout=30,
            check=False,
        )
        assert verified.returncode == 0, verified.stderr
        totals = json.loads(verified.stdout)["totals"]
        assert totals["schema-invalid"] == totals["corrupt-record"] == 0
        assert totals["counts"]["lane"]["blueprint"]["passthrough"] == 1


@_skip_node_unavailable
class TestEffectiveVerdict:
    """One row per call, recording the effective decision and both lanes' raw verdicts."""

    def test_blueprint_allow_records_rank_src_and_both_lanes(self, blueprint_env: AuditEnv) -> None:
        """A blueprint allow is rank 1, carries its provenance, and still records what the shape lane said.

        The manifest lists the command, so the blueprint lane wins; the shape lane is still asked and its passthrough
        stays in the row, which is what lets a reader see that two independent lanes were consulted.
        """
        assert blueprint_env.run(DISPATCH_HOOK, _payload(BLUEPRINT_ALLOWED)).returncode == 0
        (row,) = blueprint_env.rows()
        detail = row["action_detail"]
        assert (detail["decision"], detail["lane"], detail["rank"]) == ("allow", "blueprint", 1)
        assert detail["src"] == "skills/x/SKILL.md:1"
        assert row["trust_level"] == "plugin"
        assert _verdicts(row)["shape"]["decision"] == "passthrough"

    def test_shape_only_allow_is_rank_two_and_carries_no_src(self, env: AuditEnv) -> None:
        """A shape allow records the lane that produced it and claims no manifest provenance it does not have.

        With no manifest installed only the shape lane can allow, so the row must name rank 2 and omit ``src``.
        """
        env.run(DISPATCH_HOOK, _payload(SHAPE_ALLOWED))
        (row,) = env.rows()
        detail = row["action_detail"]
        assert (detail["decision"], detail["lane"], detail["rank"]) == ("allow", "shape", 2)
        assert "src" not in detail

    def test_whitespace_only_command_splits_the_lanes(self, env: AuditEnv) -> None:
        """The two modules genuinely disagree on whitespace, and the record keeps both answers distinct.

        Blueprint normalizes and reaches ``no-match``; shape trims and returns before examining anything. One is an
        abstention, the other is the absence of an opinion, and collapsing them would lose the difference.
        """
        env.run(DISPATCH_HOOK, _payload("   "))
        (row,) = env.rows()
        verdicts = _verdicts(row)
        assert verdicts["blueprint"]["decision"] == "passthrough"
        assert verdicts["shape"] == {"lane": "shape", "decision": "none", "why": "not-applicable"}

    @pytest.mark.parametrize("command", NON_STRING_COMMANDS)
    def test_dispatcher_ignores_non_string_command(self, env: AuditEnv, command: object) -> None:
        """A malformed host payload makes the dispatcher print nothing and exit 0.

        A non-string ``command`` used to reach ``.trim()`` and raise inside a lane. The dispatcher must stay silent and
        healthy for it, whatever the value's type.
        """
        proc = env.run(DISPATCH_HOOK, json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}))
        assert proc.stdout == b""
        assert proc.returncode == 0

    @pytest.mark.parametrize("module", [SENTINEL_HOOK, GH_READ_HOOK])
    @pytest.mark.parametrize("command", NON_STRING_COMMANDS)
    def test_module_verdict_for_non_string_command_is_not_applicable(
        self, env: AuditEnv, command: object, module: str
    ) -> None:
        """A malformed host payload must never be recorded as a broken module.

        ``module-error`` is the signal that a decision module could not be loaded or threw. A non-string ``command``
        used to reach ``.trim()`` and raise, which the dispatcher caught and filed under exactly that word — making a
        host payload problem indistinguishable in the log from a module that actually broke. Every lane returns ``none``
        here, so no row is written and the verdict has to be read from ``evaluate`` directly.
        """
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
        script = (
            f"const m = require({json.dumps(str(env.root / 'hooks' / module))});"
            f"process.stdout.write(JSON.stringify(m.evaluate({json.dumps(payload)})));"
        )
        result = subprocess.run(
            ["node", "-e", script], capture_output=True, encoding="utf-8", timeout=30, check=False, env=env.env()
        )
        assert result.returncode == 0, result.stderr
        verdict = json.loads(result.stdout)
        assert verdict["decision"] == "none"
        assert verdict["why"] == "not-applicable", f"{command!r} must not be filed as a module failure"

    def test_both_none_writes_no_row_at_all(self, env: AuditEnv) -> None:
        """With neither lane reaching a decision there is no opinion to record, so nothing is written.

        A non-Bash tool call is outside every lane, so the hook stays silent and leaves the log directory untouched.
        """
        proc = env.run(DISPATCH_HOOK, _payload("anything", tool_name="Read"))
        assert proc.stdout == b""
        assert proc.returncode == 0
        assert env.rows() == []

    def test_digest_is_present_once_the_blueprint_lane_decided(self, env: AuditEnv) -> None:
        """The digest is recorded when the blueprint lane produced a verdict for the command.

        The digest is the blueprint normalizer's output, so a verdict from that lane (here a passthrough on a
        whitespace-only command) is what entitles the row to carry one.
        """
        env.run(DISPATCH_HOOK, _payload("   "))
        (row,) = env.rows()
        assert "digest" in row["action_detail"]

    def test_digest_is_absent_when_the_blueprint_lane_never_normalized(self, env: AuditEnv) -> None:
        """No digest is invented for a command the blueprint lane never looked at.

        The digest is the blueprint normalizer's output, and that normalizer is the only one that exists. When the
        blueprint lane produces no verdict at all the field is omitted rather than filled from somewhere else — an
        absent digest is the honest record, not a gap to paper over.
        """
        (env.root / "hooks" / "blueprint-allow.js").write_text("throw new Error('boom');", encoding="utf-8")
        env.run(DISPATCH_HOOK, _payload(SHAPE_ALLOWED))
        (row,) = env.rows()
        assert _verdicts(row)["blueprint"]["why"] == "module-error"
        assert "digest" not in row["action_detail"]


@_skip_node_unavailable
@_skip_git_unavailable
@pytest.mark.integration
class TestGhReadLane:
    """The gh-read lane is third: it allows covered gh reads with no record, and only when the first two did not."""

    @pytest.fixture(name="project")
    def _project(self, tmp_path: Path):
        """Return a factory building a plugin root, a git repository holding no approval record, and a runner."""

        def build(manifest: bytes | None = None):
            """Install the plugin root (with ``manifest`` when given) and bind a runner to the new repository."""
            env = install(tmp_path, manifest=manifest)
            repo = make_repo(tmp_path)

            def run(command: str):
                """Run the dispatcher on ``command`` from inside the repository."""
                return env.run(DISPATCH_HOOK, _payload(command, cwd=str(repo)), cwd=repo, CLAUDE_PROJECT_DIR=str(repo))

            return env, run

        return build

    def test_read_allow_is_rank_three_with_verdicts_in_rank_order(self, project) -> None:
        """A covered read is allowed by the gh-read lane with no record on disk, listed after the other two lanes.

        Neither earlier lane allows this command, so the effective verdict is the third lane's (user policy: gh reads
        run free). The row lists every lane's verdict in LANES order, which is where rank order is observable without
        exporting LANES.
        """
        env, run = project()
        proc = run(GH_READ)
        assert b'"permissionDecision":"allow"' in proc.stdout
        (row,) = env.rows()
        detail = row["action_detail"]
        assert (detail["decision"], detail["lane"], detail["rank"]) == ("allow", "gh-read", 3)
        assert [entry["lane"] for entry in detail["verdicts"]] == ["blueprint", "shape", "gh-read"]

    def test_blueprint_outranks_a_read_allow(self, project) -> None:
        """When the manifest also blesses the read, blueprint wins and the gh-read allow is recorded, never printed.

        Exactly one payload reaches the host and its reason is blueprint's, so provenance stays the stronger statement.
        """
        env, run = project(_manifest_for(GH_READ))
        proc = run(GH_READ)
        assert b"plugin blueprint" in proc.stdout
        assert proc.stdout.count(b'"permissionDecision":') == 1
        (row,) = env.rows()
        assert (row["action_detail"]["lane"], row["action_detail"]["rank"]) == ("blueprint", 1)
        assert _verdicts(row)["gh-read"]["decision"] == "allow"


@_skip_node_unavailable
class TestLaneIsolation:
    """One module's failure must never suppress the other."""

    @pytest.mark.parametrize(
        ("hook", "lane"),
        [pytest.param(SENTINEL_HOOK, "shape", id="shape"), pytest.param(GH_READ_HOOK, "gh-read", id="gh-read")],
    )
    @pytest.mark.parametrize(
        "body",
        [
            pytest.param("module.exports = { evaluate() { throw new Error('boom'); } };", id="evaluate-throws"),
            pytest.param("this is not javascript(", id="unparsable"),
            pytest.param("throw new Error('boom');", id="throws-on-require"),
        ],
    )
    def test_broken_lane_module_leaves_blueprint_working(
        self, blueprint_env: AuditEnv, hook: str, lane: str, body: str
    ) -> None:
        """A blueprint allow still reaches stdout, and the broken lane is recorded as ``module-error``.

        ``module-error`` is deliberately a ``none``, not a passthrough: a module that crashed did not abstain, it said
        nothing, and the reader must not count it as evidence that the command was examined.
        """
        (blueprint_env.root / "hooks" / hook).write_text(body, encoding="utf-8")
        proc = blueprint_env.run(DISPATCH_HOOK, _payload(BLUEPRINT_ALLOWED))
        assert proc.returncode == 0
        assert b'"permissionDecision":"allow"' in proc.stdout
        (row,) = blueprint_env.rows()
        assert _verdicts(row)[lane] == {"lane": lane, "decision": "none", "why": "module-error"}
        assert row["action_detail"]["decision"] == "allow"

    def test_missing_required_lane_still_records_module_error(self, env: AuditEnv) -> None:
        """A missing shape module remains a failure verdict while the gh-read allow survives.

        The shape module is deleted from the plugin root, as in a damaged install; the gh-read lane must still allow the
        read and the row must name the absent module as an error rather than skipping the lane.
        """
        _remove_hooks(env, SENTINEL_HOOK)
        proc = env.run(DISPATCH_HOOK, _payload(GH_READ))
        assert b'"permissionDecision":"allow"' in proc.stdout
        (row,) = env.rows()
        assert _verdicts(row)["shape"] == {"lane": "shape", "decision": "none", "why": "module-error"}

    def test_blueprint_allow_is_written_before_the_shape_lane_finishes(
        self, blueprint_env: AuditEnv, tmp_path: Path
    ) -> None:
        """The blueprint allow is written before the shape lane is even required.

        The shape module blocks until the test releases it, so the allow can only have reached stdout while that lane
        was still unfinished. This asserts the dispatcher's own output ordering and nothing more. It is NOT a claim
        that the host accepted the bytes: a hook that exceeds its timeout has its entire output discarded, printed or
        not, and that residual risk is an accepted delta rather than something a subprocess could disprove.
        """
        release, finished = tmp_path / "release-shape", tmp_path / "shape-finished"
        (blueprint_env.root / "hooks" / SENTINEL_HOOK).write_text(
            "const fs = require('fs');"
            "module.exports = { evaluate() {"
            "  const deadline = Date.now() + 15000;"
            f"  while (!fs.existsSync({json.dumps(str(release))}) && Date.now() < deadline) {{}}"
            f"  fs.writeFileSync({json.dumps(str(finished))}, '');"
            "  return { lane: 'shape', rank: 2, payload: null, decision: 'passthrough', why: 'shape-mismatch' };"
            "} };",
            encoding="utf-8",
        )
        with subprocess.Popen(
            ["node", str(blueprint_env.root / "hooks" / DISPATCH_HOOK)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=blueprint_env.env(),
        ) as proc:
            proc.stdin.write(json.dumps(_payload(BLUEPRINT_ALLOWED)).encode("utf-8"))
            proc.stdin.close()
            first_byte = proc.stdout.read(1)
            shape_finished_at_first_byte = finished.exists()
            release.write_text("go", encoding="utf-8")
            proc.wait(timeout=30)
        assert first_byte == b"{"
        assert not shape_finished_at_first_byte, "the allow must be written while the shape lane is still running"
        assert finished.exists(), "the shape lane must still have been evaluated after the allow"
        assert proc.returncode == 0


@_skip_node_unavailable
class TestAuditNeverChangesTheDecision:
    """Logging is downstream of the decision in every sense that matters."""

    def test_kill_switch_leaves_stdout_identical_and_never_loads_the_library(self, dual_allow_env: AuditEnv) -> None:
        """``RIG_AUDIT=0`` disables logging only.

        The library is not even required, so it cannot fail during load.
        """
        (dual_allow_env.root / "hooks" / LIBRARY).write_text(
            "throw new Error('library must not be required');", encoding="utf-8"
        )
        proc = dual_allow_env.run(DISPATCH_HOOK, _payload(SHAPE_ALLOWED), RIG_AUDIT="0")
        assert proc.returncode == 0
        assert b'"permissionDecision":"allow"' in proc.stdout
        assert not dual_allow_env.audit_dir.exists()

    def test_library_that_throws_on_require_does_not_change_stdout(self, dual_allow_env: AuditEnv) -> None:
        """With audit enabled and the library broken, the decision is unaffected and the hook still exits 0.

        The healthy baseline is taken with logging off, then the library is replaced by one that throws on load; the two
        runs must print byte-identical output.
        """
        healthy = dual_allow_env.run(DISPATCH_HOOK, _payload(SHAPE_ALLOWED), RIG_AUDIT="0")
        (dual_allow_env.root / "hooks" / LIBRARY).write_text("throw new Error('broken library');", encoding="utf-8")
        broken = dual_allow_env.run(DISPATCH_HOOK, _payload(SHAPE_ALLOWED))
        assert broken.stdout == healthy.stdout
        assert broken.returncode == 0

    def test_unwritable_log_directory_does_not_change_stdout(self, dual_allow_env: AuditEnv) -> None:
        """A log directory that cannot be created is a logging failure, never a permission one.

        A regular file sits where ``~/.claude`` should be a directory, so the audit append cannot succeed; the allow
        must still reach stdout.
        """
        (dual_allow_env.home / ".claude").write_text("not a directory", encoding="utf-8")
        proc = dual_allow_env.run(DISPATCH_HOOK, _payload(SHAPE_ALLOWED))
        assert proc.returncode == 0
        assert b'"permissionDecision":"allow"' in proc.stdout


@_skip_node_unavailable
class TestPrivacyAndFootprint:
    """What the hook writes, and where."""

    def test_raw_command_text_never_reaches_the_log(self, env: AuditEnv) -> None:
        """Only a digest is recorded.

        The limit is stated honestly in the docs; the file itself must be clean.
        """
        secret = 'echo "correct-horse-battery-staple"'  # noqa: S105 - dummy command fixture, not a credential
        env.run(DISPATCH_HOOK, _payload(secret))
        written = env.log_files()[0].read_text(encoding="utf-8")
        assert "correct-horse" not in written
        digest = hashlib.sha256(bbm.normalize(secret).encode("utf-8")).hexdigest()
        assert digest in written

    def test_touches_nothing_outside_the_audit_directory(self, env: AuditEnv) -> None:
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

    def test_missing_session_id_goes_to_the_shared_stream_with_an_explicit_null(self, env: AuditEnv) -> None:
        """A missing identifier is never replaced by a shared default such as ``default``.

        The payload carries no session or tool-use id, so the row lands in ``_no-session.jsonl`` and both join keys are
        recorded as JSON null rather than a placeholder that would join unrelated calls.
        """
        env.run(DISPATCH_HOOK, _payload(SHAPE_ALLOWED, session_id=None, tool_use_id=None))
        assert [path.name for path in env.log_files()] == ["_no-session.jsonl"]
        (row,) = env.rows()
        assert row["session_id"] is None
        assert row["tool_use_id"] is None


@_skip_node_unavailable
@pytest.mark.integration
class TestConcurrentWriters:
    """Several plugins append one file with nothing coordinating them."""

    def test_every_completed_append_verifies_its_own_hash(self, env: AuditEnv) -> None:
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
        parsed, torn = _read_rows(path)
        assert len(parsed) + torn == writers, "every writer must have appended exactly one line"
        assert all(_hash_verifies(row) for row in parsed)
