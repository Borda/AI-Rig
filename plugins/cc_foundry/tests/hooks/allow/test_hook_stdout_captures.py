"""Byte-for-byte replay of the frozen stdout captures for the two decision modules and the dispatcher.

``fixtures/hook_stdout_captures.json`` records what ``blueprint-allow.js`` and ``sentinel-read-allow.js`` wrote to
stdout, for the whole command corpus of both suites, at the revision named in the fixture. Those bytes were captured
before either module was given an ``evaluate()`` wrapper and before the dispatcher existed.

This is the suite that makes the refactor provable. The existing harnesses strip and re-decode stdout, so they cannot
see a byte difference; and comparing two freshly refactored implementations against each other would let drift that
both share pass unnoticed. The captures are the only party to the comparison that neither implementation can influence.

Three properties are checked against them:

* each standalone driver retains its frozen stdout, except raw CR inputs now require blueprint passthrough;
* the dispatcher writes ``payload ? JSON.stringify(payload) : <empty>`` for ``blueprint || shape`` — never the string
  ``"null"``, and never a byte either module would not have written;
* the exit code is 0 on every path, for every input, including malformed stdin.

Scope note: parity is defined over inputs where both modules COMPLETE. The dispatcher's shared timeout boundary is a
documented behaviour delta and deliberately sits outside this contract — no test here asserts what the host did with
the bytes, which a subprocess cannot observe anyway.
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
from collections.abc import Callable
from pathlib import Path

import build_blueprint_manifest as bbm
import pytest
from _audit_harness import AuditEnv, install

CAPTURES = json.loads(
    (Path(__file__).resolve().parents[2] / "fixtures" / "hook_stdout_captures.json").read_text(encoding="utf-8")
)

BLUEPRINT_HOOK = "blueprint-allow.js"
SENTINEL_HOOK = "sentinel-read-allow.js"
DISPATCH_HOOK = "allow-dispatch.js"

#: Scenario names whose payloads use a non-Bash tool; every other scenario sends ``Bash``.
NON_BASH = "non-bash-tool"

#: Raw stdin payloads whose captured behaviour is contractual, keyed by the fixture's case name.
RAW_STDIN = {
    "not-json": "not json",
    "empty-stdin": "",
    "empty-object": "{}",
    "missing-tool-input": '{"tool_name": "Bash"}',
    "null-command": '{"tool_name": "Bash", "tool_input": {"command": null}}',
    "command-not-string": '{"tool_name": "Bash", "tool_input": {"command": 42}}',
}

#: Slices each large captured scenario is split into, so xdist can spread one scenario's spawns across its workers.
LARGE_SCENARIO_SLICES = 4

#: Scenarios with at least this many commands are sliced; smaller ones replay as one case.
LARGE_SCENARIO_MIN_COMMANDS = 50

NODE_UNAVAILABLE = shutil.which("node") is None
_skip_node_unavailable = pytest.mark.skipif(NODE_UNAVAILABLE, reason="requires node to execute the hooks")


def _payload(command: str, scenario: str) -> str:
    """Return the PreToolUse stdin the capture used for this scenario."""
    tool = "Read" if scenario == NON_BASH else "Bash"
    return json.dumps({"tool_name": tool, "tool_input": {"command": command}})


@pytest.fixture(scope="module", name="bare_env")
def _bare_env(tmp_path_factory: pytest.TempPathFactory) -> AuditEnv:
    """Return one installed plugin root without a manifest, shared read-only by the manifest-free cases.

    Every run passes ``RIG_AUDIT=0``, which stops the only write the hooks can make, so the root and its throwaway home
    stay exactly as installed. A fresh install per case cost 214 installs for the shape corpus alone.
    """
    return install(tmp_path_factory.mktemp("bare-hook-env"))


@pytest.fixture(name="replay")
def _replay(tmp_path: Path) -> Callable[..., None]:
    """Return a callable that replays one captured scenario and asserts every case matches."""
    env = install(tmp_path)

    def _install_manifest(scenario: str, command: str) -> None:
        """Install the manifest the capture used for this scenario."""
        if scenario == "tampered-self":
            entry = {bbm.sha256_text(bbm.normalize(command)): {"kind": "block", "src": "tampered.md:1"}}
            env.write_manifest(bbm.encode_manifest({"schema": 1, "plugin": "cc_foundry@0.0.0", "entries": entry}))
            return
        name = "seeded" if scenario == NON_BASH else scenario
        described = CAPTURES["manifests"].get(name)
        env.write_manifest(None if described is None else base64.b64decode(described["b64"]))

    def _run(scenario: str, part: tuple[int, int], hook: str, expected_for: Callable[[str], dict]) -> None:
        """Replay one ``(index, count)`` slice of ``scenario``'s commands through ``hook`` and compare raw stdout bytes.

        Slices take every ``count``-th command of the sorted corpus starting at ``index``, so the ``count`` slices of a
        scenario partition it exactly: every captured command is replayed by exactly one case.
        """
        index, count = part
        mismatches = []
        for command in sorted(CAPTURES["blueprint"].get(scenario, {}))[index::count]:
            expected = expected_for(command)
            if expected is None:
                continue
            _install_manifest(scenario, command)
            proc = env.run(hook, _payload(command, scenario), RIG_AUDIT="0")
            if base64.b64encode(proc.stdout).decode() != expected["stdout_b64"] or proc.returncode != expected["exit"]:
                mismatches.append((command, proc.stdout, base64.b64decode(expected["stdout_b64"])))
        assert mismatches == [], f"{len(mismatches)} case(s) diverged from the frozen capture: {mismatches[:3]}"

    return _run


def _blueprint_scenarios() -> list:
    """Return one param per slice of every captured blueprint scenario, as ``(scenario, (index, count))``.

    A whole 214-command scenario in one case was the longest single test of the suite (about 25 s on Windows CI) and
    pinned one xdist worker while the others idled; slicing it lets the scheduler balance the same spawns.
    """
    params = []
    for name in sorted(CAPTURES["blueprint"]):
        count = LARGE_SCENARIO_SLICES if len(CAPTURES["blueprint"][name]) >= LARGE_SCENARIO_MIN_COMMANDS else 1
        params.extend(pytest.param(name, (index, count), id=f"{name}-{index + 1}of{count}") for index in range(count))
    return params


@_skip_node_unavailable
class TestDecisionModuleDrivers:
    """Each driver retains captured bytes with explicit refusal of raw CR blueprint inputs."""

    @pytest.mark.parametrize(("scenario", "part"), _blueprint_scenarios())
    def test_blueprint_driver_bytes_unchanged(
        self, scenario: str, part: tuple[int, int], replay: Callable[..., None]
    ) -> None:
        """Replay the blueprint module over one slice of a manifest scenario's commands.

        Starting state is the manifest that scenario was captured under (seeded, tampered, or absent); every command in
        the slice must reproduce the frozen stdout bytes and exit code exactly, except raw CR inputs must pass through.
        """
        replay(
            scenario,
            part,
            BLUEPRINT_HOOK,
            lambda command: (
                {"exit": 0, "stdout_b64": ""} if "\r" in command else CAPTURES["blueprint"][scenario][command]
            ),
        )

    @pytest.mark.parametrize("command", sorted(CAPTURES["shape"]["bash"]))
    def test_shape_driver_bytes_unchanged(self, command: str, bare_env: AuditEnv) -> None:
        """Replay the shape module, which reads no manifest, over the whole corpus.

        The shape module gained a ``require.main`` guard and had its driver body moved into a function. Both are exactly
        the kind of change that silently alters what reaches stdout, which is why the comparison is on bytes.
        """
        expected = CAPTURES["shape"]["bash"][command]
        proc = bare_env.run(SENTINEL_HOOK, _payload(command, "bash"), RIG_AUDIT="0")
        assert base64.b64encode(proc.stdout).decode() == expected["stdout_b64"]
        assert proc.returncode == expected["exit"]

    @pytest.mark.parametrize("case", sorted(RAW_STDIN))
    @pytest.mark.parametrize("module", [BLUEPRINT_HOOK, SENTINEL_HOOK, DISPATCH_HOOK])
    def test_malformed_stdin_bytes_unchanged(self, module: str, case: str, bare_env: AuditEnv) -> None:
        """Malformed stdin writes nothing and exits 0, for both modules and for the dispatcher.

        The dispatcher shares the modules' captured expectation here: neither lane reaches a decision, so it has nothing
        to print and nothing to record.
        """
        expected = CAPTURES["raw_stdin"]["blueprint" if module != SENTINEL_HOOK else "shape"][case]
        proc = bare_env.run(module, RAW_STDIN[case], RIG_AUDIT="0")
        assert base64.b64encode(proc.stdout).decode() == expected["stdout_b64"]
        assert proc.returncode == 0


@_skip_node_unavailable
class TestDispatcherParity:
    """The dispatcher's stdout is the two modules' composition, byte-for-byte."""

    @pytest.mark.parametrize(("scenario", "part"), _blueprint_scenarios())
    def test_dispatcher_equals_blueprint_or_shape(
        self, scenario: str, part: tuple[int, int], replay: Callable[..., None]
    ) -> None:
        """For every captured command, dispatcher stdout equals blueprint's bytes, else shape's, else empty.

        This is the load-bearing half of the registration change's safety argument, and its limit is worth stating.
        It shows that for every input on which both modules complete, the bytes handed to the host are the bytes the
        two separately registered hooks would have handed over. It cannot show anything about what the host does with
        them: two processes became one, so the timeout budget, the failure boundary and the host's own conflict
        resolution across hooks all changed in ways no subprocess can observe. Those live in the documented deltas.
        """
        shape_scenario = NON_BASH if scenario == NON_BASH else "bash"
        shape_cases = CAPTURES["shape"][shape_scenario]

        def expected_for(command: str) -> dict | None:
            """Return the composed oracle for one command, or None when the corpus has no shape capture for it."""
            shape = shape_cases.get(command)
            if shape is None:
                return None
            blueprint = {"exit": 0, "stdout_b64": ""} if "\r" in command else CAPTURES["blueprint"][scenario][command]
            return blueprint if blueprint["stdout_b64"] else shape

        replay(scenario, part, DISPATCH_HOOK, expected_for)

    def test_never_serializes_null(self, bare_env: AuditEnv) -> None:
        """A passthrough is silence, never the four bytes ``null``.

        ``JSON.stringify(null)`` returns the string ``"null"``, which a host would parse as a decision object. The
        oracle is ``payload ? JSON.stringify(payload) : <empty>`` precisely to keep that unreachable.
        """
        proc = bare_env.run(DISPATCH_HOOK, _payload("ls -la", "bash"), RIG_AUDIT="0")
        assert (proc.stdout, proc.returncode) == (b"", 0)

    def test_blueprint_wins_when_both_allow(self, tmp_path: Path) -> None:
        """With both lanes allowing, the host gets ONE payload and it is blueprint's, deterministically.

        Two separately registered hooks both emitted an allow and the host picked by completion order. Rank order
        replaces that with a rule.
        """
        command = 'V=$(cat "${TMPDIR:-/tmp}/x-${CSID}")'
        entries = {bbm.sha256_text(bbm.normalize(command)): {"kind": "block", "src": "skills/x/SKILL.md:1"}}
        manifest = bbm.encode_manifest({"schema": 1, "plugin": "cc_foundry@0.0.0", "entries": entries})
        env = install(tmp_path, manifest=manifest)
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})

        shape = env.run(SENTINEL_HOOK, payload, RIG_AUDIT="0")
        blueprint = env.run(BLUEPRINT_HOOK, payload, RIG_AUDIT="0")
        dispatched = env.run(DISPATCH_HOOK, payload, RIG_AUDIT="0")
        assert blueprint.stdout, "the fixture must make BOTH lanes allow"
        assert shape.stdout, "the fixture must make BOTH lanes allow"
        assert dispatched.stdout == blueprint.stdout
        assert dispatched.stdout != shape.stdout


class TestCaptureFixtureIsHonest:
    """A capture nobody can fail is worth nothing; these assert the corpus actually covers the paths."""

    @pytest.mark.parametrize(
        ("lane", "scenario"),
        [
            pytest.param("blueprint", "seeded", id="blueprint-seeded"),
            pytest.param("shape", "bash", id="shape-bash"),
            pytest.param("blueprint", "tampered-self", id="blueprint-tampered-self"),
        ],
    )
    def test_corpus_contains_allows(self, lane: str, scenario: str) -> None:
        """Each lane's capture holds real allows, or a byte comparison proves only that silence is silent.

        The fixture is read as data only (no hook is spawned): an allow is a captured case with non-empty stdout.
        """
        allows = [command for command, case in CAPTURES[lane][scenario].items() if case["stdout_b64"]]
        assert allows != []

    def test_records_the_revision_and_runtime_it_came_from(self) -> None:
        """A baseline with no provenance cannot be reasoned about when it later disagrees with the code.

        The capture must name a clean revision and the Node version that produced it.
        """
        assert (
            bool(CAPTURES["captured_at_rev"]),
            CAPTURES["captured_at_rev"].endswith("-dirty"),
            CAPTURES["node_version"].startswith("v"),
        ) == (True, False, True)

    def test_manifest_bytes_are_pinned_by_digest(self) -> None:
        """The seeded manifest is frozen with its own hash, so a regenerated manifest cannot silently shift ``src``.

        The decoded manifest bytes must hash to the digest stored beside them in the fixture.
        """
        described = CAPTURES["manifests"]["seeded"]
        assert hashlib.sha256(base64.b64decode(described["b64"])).hexdigest() == described["sha256"]


class TestCorpusSize:
    """Guard against a corpus that quietly shrinks."""

    def test_every_scenario_is_populated(self) -> None:
        """An empty scenario would pass every comparison above without testing anything.

        Both the blueprint and the shape corpora must hold at least one captured command per scenario.
        """
        empty = [name for name, cases in CAPTURES["blueprint"].items() if not cases]
        empty += [name for name, cases in CAPTURES["shape"].items() if not cases]
        assert empty == []
