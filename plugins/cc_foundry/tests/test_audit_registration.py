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
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import propagate_shared


REPO_ROOT = Path(__file__).resolve().parents[3]
PLUGINS = ("cc_foundry", "cc_oss", "cc_develop", "cc_research")
BIN_DIR = REPO_ROOT / "plugins" / "cc_foundry" / "bin"

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

NODE_UNAVAILABLE = shutil.which("node") is None


def _hooks(plugin: str) -> dict:
    """Return one plugin's parsed hook registrations."""
    return json.loads((REPO_ROOT / "plugins" / plugin / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]


def _scripts(entry: dict) -> list[str]:
    """Return the script basenames one registration entry runs."""
    return [hook.get("command", "").rstrip('"').split("/")[-1] for hook in entry.get("hooks", [])]


@pytest.mark.parametrize("plugin", PLUGINS)
class TestRegistration:
    """Every plugin registers the dispatcher and the closer, and neither decision module directly."""

    def test_dispatcher_is_the_only_bash_auto_allow_entry(self, plugin: str) -> None:
        """The two decision modules become libraries behind one registration.

        Both stay shipped, propagated and independently tested — they are simply not registered. Leaving one registered
        alongside the dispatcher would double-evaluate it and write a second, contradictory audit row.
        """
        registered = [script for entry in _hooks(plugin)["PreToolUse"] for script in _scripts(entry)]
        assert "allow-dispatch.js" in registered
        assert "blueprint-allow.js" not in registered
        assert "sentinel-read-allow.js" not in registered

    @pytest.mark.parametrize(("event", "matcher"), sorted(CLOSE_REGISTRATIONS.items(), key=lambda item: item[0]))
    def test_closer_is_registered_on_every_event(self, plugin: str, event: str, matcher: str | None) -> None:
        """Completion events are Bash-matched; lifecycle events carry no matcher because they have no tool."""
        entries = [entry for entry in _hooks(plugin).get(event, []) if "audit-close.js" in _scripts(entry)]
        assert len(entries) == 1, f"{plugin} must register the closer exactly once on {event}"
        assert entries[0].get("matcher") == matcher

    def test_every_plugin_writes_its_own_rows(self, plugin: str) -> None:
        """No plugin may depend on another being installed.

        Requiring cc_foundry as the sole writer would make a standalone install silently unaudited, and the authoring
        rules forbid proposing a prerequisite plugin as a resilience mechanism.
        """
        shipped = REPO_ROOT / "plugins" / plugin / "hooks"
        assert (shipped / "allow-dispatch.js").is_file()
        assert (shipped / "audit-close.js").is_file()
        assert (shipped / "lib" / "audit-log.js").is_file()


class TestPropagation:
    """The new files are byte-identical everywhere they ship."""

    @pytest.mark.parametrize("relative", PROPAGATED)
    def test_file_is_in_the_propagation_manifest(self, relative: str) -> None:
        """A shared file outside the manifest drifts silently; the manifest is what the pre-commit gate enforces."""
        canonical = f"plugins/cc_foundry/{relative}"
        entries = [entry for entry in propagate_shared.MANIFEST if entry["canonical"] == canonical]
        assert entries, f"{canonical} is missing from the propagation MANIFEST"
        assert sorted(entries[0]["copies"]) == sorted(
            f"plugins/{plugin}/{relative}" for plugin in PLUGINS if plugin != "cc_foundry"
        )

    @pytest.mark.parametrize("encoding", ["utf-8", "cp1252", "ascii"])
    def test_copies_are_identical_to_the_canonical(self, encoding: str) -> None:
        """Check the whole manifest without crashing on legacy stdout encodings."""
        proc = subprocess.run(
            [sys.executable, str(BIN_DIR / "propagate_shared.py")],
            capture_output=True,
            encoding=encoding,
            env={**os.environ, "PYTHONIOENCODING": encoding},
            cwd=str(REPO_ROOT),
            check=False,
        )
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

    def test_no_document_still_claims_a_registration_order(self) -> None:
        """The old wording described one hook as registered after the other; parallel hooks never had an order.

        With the dispatcher there is a real order — rank — but it is inside one process, so any surviving sentence about
        registration order is now wrong twice over.
        """
        stale = []
        for path in (REPO_ROOT / "plugins").rglob("*.md"):
            if ".claude-plugin" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if "registered immediately after the sentinel hook" in text:
                stale.append(path.relative_to(REPO_ROOT).as_posix())
        assert stale == []

    @pytest.mark.parametrize("encoding", ["utf-8", "cp1252", "ascii"])
    def test_orphaned_bin_gate_passes(self, encoding: str) -> None:
        """Require shipped references and encoding-safe success output."""
        proc = subprocess.run(
            [sys.executable, str(BIN_DIR / "check_orphaned_bin.py")],
            capture_output=True,
            encoding=encoding,
            env={**os.environ, "PYTHONIOENCODING": encoding},
            cwd=str(REPO_ROOT),
            check=False,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.skipif(NODE_UNAVAILABLE, reason="requires node to execute the hook")
class TestStandaloneDriverStillWorks:
    """``bin/audit_hook_coverage.py`` runs the shape module as a hook, not as a library."""

    def test_shape_module_still_runs_as_a_subprocess(self, tmp_path: Path) -> None:
        """The ``require.main`` guard must keep the standalone entry point, not replace it.

        A guard that also removed the driver would leave the module importable and the coverage tool broken — and the
        tool's failure mode is a silent zero score, not an error.
        """
        hook = REPO_ROOT / "plugins" / "cc_foundry" / "hooks" / "sentinel-read-allow.js"
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": 'V=$(cat "${TMPDIR:-/tmp}/x-${CSID}")'}})
        proc = subprocess.run(
            ["node", str(hook)], input=payload, capture_output=True, encoding="utf-8", timeout=30, check=False
        )
        assert proc.returncode == 0
        assert json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_coverage_tool_still_classifies_through_the_shape_hook(self) -> None:
        """Drive the tool's own classifier over the repo's shape module and require real verdicts.

        The tool subprocesses ``sentinel-read-allow.js`` and reads its stdout. That module gained a ``require.main``
        guard and had its driver body moved into ``decide`` — either could leave the tool silently scoring every command
        as uncovered. Running the whole CLI would not serve as the guard: it walks the user's entire transcript history,
        needs a populated plugin cache, and takes minutes. This drives the one seam the refactor could have broken, and
        asserts both verdicts so a classifier stuck on a constant fails too.
        """
        import audit_hook_coverage

        shape_hook = REPO_ROOT / "plugins" / "cc_foundry" / "hooks" / "sentinel-read-allow.js"
        classifier = audit_hook_coverage.Classifier({}, shape_hook)
        idiom = 'IFS= read -r RUN_DIR < "${TMPDIR:-/tmp}/oss-review-run-dir-${CSID}"'
        assert classifier.verdict(idiom) == audit_hook_coverage.verdict_of(None, shape_allowed=True)
        assert classifier.verdict("rm -rf /") == audit_hook_coverage.verdict_of(None, shape_allowed=False)


@pytest.mark.skipif(NODE_UNAVAILABLE, reason="requires node to execute the hooks")
class TestCoexistenceWithTaskLog:
    """The audit hooks own no path that another hook cleans up."""

    def test_audit_log_lives_outside_everything_task_log_wipes(self, tmp_path: Path) -> None:
        """``task-log.js`` removes ``claude-state-<session id>`` at SessionEnd; nothing here lives under it.

        An earlier revision of this design put coordination state inside exactly that directory, which meant a
        concurrent teardown could delete live evidence. The current design touches one path, under the user's home.
        """
        from _audit_harness import install

        env = install(tmp_path)
        session = "coexistence-session"
        env.run(
            "audit-close.js",
            {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_use_id": "t", "session_id": session},
        )
        before = {path: path.read_bytes() for path in env.log_files()}
        assert before, "the fixture must have written something for the teardown to threaten"

        # Mirror task-log's sentinel-base selection without invoking its destructive teardown.
        # Invoking the real SessionEnd teardown is not an option: `getSentinelDir()` hardcodes `/tmp` on every
        # non-Windows platform and ignores TMPDIR, and that path also sweeps OTHER sessions' stale directories — a
        # test may not reach outside its sandbox to prove a point.
        sentinel_base = subprocess.run(
            [
                "node",
                "-e",
                "const os=require('os');process.stdout.write(process.platform==='win32'?os.tmpdir():'/tmp')",
            ],
            capture_output=True,
            encoding="utf-8",
            timeout=30,
            check=True,
        ).stdout
        swept = Path(sentinel_base).resolve() / f"claude-state-{session}"
        audit_dir = env.audit_dir.resolve()
        assert not audit_dir.is_relative_to(swept), "the audit log must not live inside a directory task-log.js wipes"
        # A temporary HOME is valid; only top-level claude-state-* trees are eligible for the stale-session sweep.
        sentinel_root = Path(sentinel_base).resolve()
        if audit_dir.is_relative_to(sentinel_root):
            relative = audit_dir.relative_to(sentinel_root)
            assert relative.parts and not relative.parts[0].startswith("claude-state-")

        assert {path: path.read_bytes() for path in env.log_files()} == before
        assert not any("claude-audit" in name for name in env.created_paths())
        assert not any(path.name.startswith("claude-state") for path in env.home.rglob("*"))
