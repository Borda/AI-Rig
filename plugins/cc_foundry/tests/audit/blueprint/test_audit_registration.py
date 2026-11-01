"""Registration, propagation and coexistence checks for the audit hooks.

These assert facts about the repository rather than about a running hook, and each one covers a gap no existing gate
closes:

* ``check_orphaned_bin.py`` only requires a ``bin/`` script to be mentioned *somewhere* under its own plugin, and
  ``check_readme_drift.py`` only validates references that already exist. Neither asserts that the new verifier reaches
  all four plugin READMEs, so that is asserted here.
* Nothing else checks that every plugin registers the dispatcher, registers neither decision module directly, and
  registers the closer on all four events.
* ``bin/audit_hook_coverage.py`` subprocesses an installed ``sentinel-read-allow.js``. Adding a ``require.main`` guard
  to that module is exactly the change that would break it silently, so its standalone entry point is exercised.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import audit_hook_coverage
import propagate_shared
import pytest
from _audit_harness import install

REPO_ROOT = Path(__file__).resolve().parents[5]
PLUGINS = ("cc_foundry", "cc_oss", "cc_develop", "cc_research")
BIN_DIR = REPO_ROOT / "plugins" / "cc_foundry" / "bin"
SHAPE_HOOK = REPO_ROOT / "plugins" / "cc_foundry" / "hooks" / "sentinel-read-allow.js"

#: Files the audit work adds and that must stay byte-identical across every plugin that ships them.
PROPAGATED = (
    "hooks/allow-dispatch.js",
    "hooks/audit-close.js",
    "hooks/lib/audit-log.js",
    "bin/verify_blueprint_audit.py",
)

#: Events the closer must be registered on, mapped to the matcher each registration must carry.
CLOSE_REGISTRATIONS = {
    "PostToolUse": "Bash",
    "PostToolUseFailure": "Bash",
    "SessionStart": None,
    "SessionEnd": None,
}

_skip_node_unavailable = pytest.mark.skipif(shutil.which("node") is None, reason="requires node to execute the hooks")


def _hooks(plugin: str) -> dict:
    """Return one plugin's parsed hook registrations."""
    return json.loads((REPO_ROOT / "plugins" / plugin / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]


def _run_gate(script: str, encoding: str, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run one shipped gate script with its stdout forced to ``encoding``, as a legacy console would."""
    return subprocess.run(
        [sys.executable, str(BIN_DIR / script), *args],
        capture_output=True,
        encoding=encoding,
        env={**os.environ, "PYTHONIOENCODING": encoding},
        cwd=str(cwd),
        check=False,
    )


def _scripts(entry: dict) -> list[str]:
    """Return the script basenames one registration entry runs."""
    return [
        match.group(1)
        for hook in entry.get("hooks", [])
        if (match := re.search(r"\$\{CLAUDE_PLUGIN_ROOT\}/hooks/([^\"'\s]+\.js)", hook.get("command", "")))
    ]


def _platform_sentinel_base() -> Path:
    """Return the directory ``task-log.js`` keeps its ``claude-state-*`` trees in, as the hook itself picks it.

    The hook hardcodes ``/tmp`` on every non-Windows platform and ignores ``TMPDIR``; Node is asked for the Windows case
    so the value matches what the hook computes.
    """
    script = "const os=require('os');process.stdout.write(process.platform==='win32'?os.tmpdir():'/tmp')"
    out = subprocess.run(["node", "-e", script], capture_output=True, encoding="utf-8", timeout=30, check=True).stdout
    return Path(out).resolve()


def _inside_stale_session_sweep(audit_dir: Path, sentinel_base: Path) -> bool:
    """Return whether ``audit_dir`` sits in a top-level ``claude-state-*`` tree below the sentinel base.

    Only top-level ``claude-state-*`` trees are eligible for the stale-session sweep, so a temporary HOME elsewhere
    under the base is valid.
    """
    if not audit_dir.is_relative_to(sentinel_base):
        return False
    parts = audit_dir.relative_to(sentinel_base).parts
    return not parts or parts[0].startswith("claude-state-")


@pytest.mark.parametrize("plugin", PLUGINS)
class TestRegistration:
    """Every plugin registers the dispatcher and the closer, and neither decision module directly."""

    def test_dispatcher_is_the_only_bash_auto_allow_entry(self, plugin: str) -> None:
        """The two decision modules become libraries behind one registration.

        Both stay shipped, propagated and independently tested — they are simply not registered. Leaving one registered
        alongside the dispatcher would double-evaluate it and write a second, contradictory audit row.
        """
        registered = [script for entry in _hooks(plugin)["PreToolUse"] for script in _scripts(entry)]
        assert (
            "allow-dispatch.js" in registered,
            "blueprint-allow.js" in registered,
            "sentinel-read-allow.js" in registered,
        ) == (
            True,
            False,
            False,
        )

    @pytest.mark.parametrize(
        ("event", "matcher"), [pytest.param(event, matcher, id=event) for event, matcher in CLOSE_REGISTRATIONS.items()]
    )
    def test_closer_is_registered_once_on_every_event(self, plugin: str, event: str, matcher: str | None) -> None:
        """The closer is registered exactly once per event; completion events are Bash-matched, lifecycle ones are not.

        Lifecycle events carry no matcher because they have no tool. A second registration would write the same audit
        row twice; a missing one leaves that event's commands unaudited.
        """
        entries = [entry for entry in _hooks(plugin).get(event, []) if "audit-close.js" in _scripts(entry)]
        assert [entry.get("matcher") for entry in entries] == [matcher], (
            f"{plugin} must register the closer once on {event}"
        )

    def test_every_plugin_ships_its_own_audit_files(self, plugin: str) -> None:
        """No plugin may depend on another being installed to write its own audit rows.

        Requiring cc_foundry as the sole writer would make a standalone install silently unaudited, and the authoring
        rules forbid proposing a prerequisite plugin as a resilience mechanism.
        """
        shipped = REPO_ROOT / "plugins" / plugin / "hooks"
        assert [(shipped / name).is_file() for name in ("allow-dispatch.js", "audit-close.js", "lib/audit-log.js")] == [
            True
        ] * 3


class TestPropagation:
    """The new files are byte-identical everywhere they ship."""

    @pytest.mark.parametrize("relative", PROPAGATED)
    def test_file_is_in_the_propagation_manifest(self, relative: str) -> None:
        """A shared file outside the manifest drifts silently; the manifest is what the pre-commit gate enforces.

        Each file must be listed with exactly the other three plugins as its copies.
        """
        canonical = f"plugins/cc_foundry/{relative}"
        entries = [entry for entry in propagate_shared.MANIFEST if entry["canonical"] == canonical]
        assert entries, f"{canonical} is missing from the propagation MANIFEST"
        assert sorted(entries[0]["copies"]) == sorted(
            f"plugins/{plugin}/{relative}" for plugin in PLUGINS if plugin != "cc_foundry"
        )

    @pytest.mark.parametrize("encoding", ["utf-8", "cp1252", "ascii"])
    def test_copies_are_identical_to_the_canonical(self, encoding: str) -> None:
        """The whole manifest checks clean without crashing on a legacy stdout encoding.

        ``propagate_shared.py`` runs against the real repository under each console encoding; a non-ASCII character in
        its report would raise on a legacy Windows console before the comparison result was shown.
        """
        proc = _run_gate("propagate_shared.py", encoding, REPO_ROOT)
        assert proc.returncode == 0, proc.stdout + proc.stderr


class TestDocumentationGates:
    """Gaps the shipped gates do not cover."""

    @pytest.mark.parametrize("plugin", PLUGINS)
    def test_verifier_is_named_in_every_plugin_readme(self, plugin: str) -> None:
        """Each plugin ships the verifier, so each plugin's README has to say so.

        ``check_orphaned_bin.py`` is satisfied by a single mention anywhere under the owning plugin, which would leave
        three READMEs silent about a script their users have installed.
        """
        readme = (REPO_ROOT / "plugins" / plugin / "README.md").read_text(encoding="utf-8")
        assert "verify_blueprint_audit.py" in readme

    @pytest.mark.parametrize("encoding", ["cp1252", "ascii"])
    def test_orphaned_bin_success_output_is_encoding_safe(self, encoding: str, tmp_path: Path) -> None:
        """The gate's success report survives a legacy console encoding.

        A minimal plugin tree with one referenced script drives the same success path the real repository takes: the
        scan reads Markdown as UTF-8 regardless of console encoding and the success line is fixed text, so only the
        printing step varies with ``encoding``. The UTF-8 console is exercised by the real-repository case above.
        """
        plugin = tmp_path / "plugins" / "demo"
        (plugin / "bin").mkdir(parents=True)
        (plugin / "bin" / "tool.py").write_text("print('tool')\n", encoding="utf-8")
        (plugin / "skills" / "use").mkdir(parents=True)
        (plugin / "skills" / "use" / "SKILL.md").write_text(
            'python "${CLAUDE_PLUGIN_ROOT}/bin/tool.py"\n', encoding="utf-8"
        )
        proc = _run_gate("check_orphaned_bin.py", encoding, tmp_path, "--plugins-dir", "plugins")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert proc.stdout.startswith("OK: Check 32d")


@_skip_node_unavailable
class TestStandaloneDriverStillWorks:
    """``bin/audit_hook_coverage.py`` runs the shape module as a hook, not as a library."""

    def test_shape_module_runs_as_a_subprocess(self) -> None:
        """The ``require.main`` guard must keep the standalone entry point, not replace it.

        A guard that also removed the driver would leave the module importable and the coverage tool broken — and the
        tool's failure mode is a silent zero score, not an error. The hook runs under real Node because that is the
        contract.
        """
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": 'V=$(cat "${TMPDIR:-/tmp}/x-${CSID}")'}})
        proc = subprocess.run(
            ["node", str(SHAPE_HOOK)], input=payload, capture_output=True, encoding="utf-8", timeout=30, check=False
        )
        assert proc.returncode == 0
        assert json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_coverage_tool_classifies_through_the_shape_hook(self) -> None:
        """Drive the tool's own classifier over the repo's shape module and require real verdicts.

        The tool subprocesses ``sentinel-read-allow.js`` and reads its stdout. That module gained a ``require.main``
        guard and had its driver body moved into ``decide`` — either could leave the tool silently scoring every command
        as uncovered. Running the whole CLI would not serve as the guard: it walks the user's entire transcript history,
        needs a populated plugin cache, and takes minutes. This drives the one seam the refactor could have broken, and
        asserts both verdicts so a classifier stuck on a constant fails too.
        """
        classifier = audit_hook_coverage.Classifier({}, SHAPE_HOOK)
        idiom = 'IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/oss-review-run-dir-${CSID}"'
        assert (classifier.verdict(idiom), classifier.verdict("rm -rf /")) == (
            audit_hook_coverage.verdict_of(None, shape_allowed=True),
            audit_hook_coverage.verdict_of(None, shape_allowed=False),
        )


@_skip_node_unavailable
class TestCoexistenceWithTaskLog:
    """The audit hooks own no path that another hook cleans up."""

    def test_audit_log_lives_outside_everything_task_log_wipes(self, tmp_path: Path) -> None:
        """``task-log.js`` removes ``claude-state-<session id>`` at SessionEnd; nothing here lives under it.

        An earlier revision of this design put coordination state inside exactly that directory, which meant a
        concurrent teardown could delete live evidence. The current design touches one path, under the user's home. The
        real SessionEnd teardown is not invoked: it hardcodes ``/tmp`` and also sweeps other sessions' stale
        directories, and a test may not reach outside its sandbox to prove a point, so the sentinel base selection is
        mirrored instead.
        """
        env = install(tmp_path)
        session = "coexistence-session"
        env.run(
            "audit-close.js",
            {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_use_id": "t", "session_id": session},
        )
        before = {path: path.read_bytes() for path in env.log_files()}
        assert before, "the fixture must have written something for the teardown to threaten"

        sentinel_base = _platform_sentinel_base()
        audit_dir = env.audit_dir.resolve()

        assert not audit_dir.is_relative_to(sentinel_base / f"claude-state-{session}")
        assert not _inside_stale_session_sweep(audit_dir, sentinel_base)
        assert {path: path.read_bytes() for path in env.log_files()} == before
        assert not any("claude-audit" in name for name in env.created_paths())
        assert not any(path.name.startswith("claude-state") for path in env.home.rglob("*"))
