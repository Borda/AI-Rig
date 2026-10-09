"""Subprocess tests for ``hooks/stale-plugin-check.js``.

The hook fires on every ``SessionStart``. It compares the plugin root this session loaded with the install record in
``<config>/plugins/installed_plugins.json`` and, when the loaded copy was replaced (``.orphaned_at``) or is no longer
the installed path, prints one JSON object: a ``systemMessage`` the user sees and ``additionalContext`` the model reads.
Every plugin ships a byte-identical copy that checks only its own root, so each behavioral case runs every shipped copy
against a fake cache dir named after that copy's plugin. Each case builds a fake config dir and passes it through
``CLAUDE_CONFIG_DIR``; ``CLAUDE_PLUGIN_ROOT`` names the loaded root.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import propagate_shared
import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = "stale-plugin-check.js"
SESSION_ID = "0f6c2b9e-1d4a-4c8e-9b7a-2e5d3f1a6c40"
START_PAYLOAD = json.dumps({"hook_event_name": "SessionStart", "source": "startup", "session_id": SESSION_ID})


def _payload(source: str, session_id: str | None = SESSION_ID) -> str:
    """SessionStart payload for one ``source``; ``session_id=None`` leaves the id out."""
    payload = {"hook_event_name": "SessionStart", "source": source}
    if session_id is not None:
        payload["session_id"] = session_id
    return json.dumps(payload)


#: Source dir of each plugin that ships a copy, mapped to the plugin name its install cache dir carries.
PLUGIN_DIRS = {"cc_foundry": "foundry", "cc_oss": "oss", "cc_develop": "develop", "cc_research": "research"}
COPIES = [
    pytest.param(REPO_ROOT / "plugins" / source / "hooks" / SCRIPT, plugin, id=plugin)
    for source, plugin in PLUGIN_DIRS.items()
]

_requires_node = pytest.mark.skipif(shutil.which("node") is None, reason="node executable not available")


def _install(config: Path, plugin: str, version: str) -> Path:
    """Create one cached version dir of ``plugin`` and return it."""
    root = config / "plugins" / "cache" / "borda-ai-rig" / plugin / version
    root.mkdir(parents=True)
    return root


def _record(config: Path, plugin: str, *installed: Path) -> None:
    """Write installed_plugins.json naming the given roots as the installed copies of ``plugin``."""
    entries = [{"scope": "user", "installPath": str(root), "version": root.name} for root in installed]
    path = config / "plugins" / "installed_plugins.json"
    path.write_text(json.dumps({"version": 2, "plugins": {f"{plugin}@borda-ai-rig": entries}}), encoding="utf-8")


def _run(hook: Path, config: Path, root: Path, payload: str = START_PAYLOAD) -> str:
    """Run one hook copy for one loaded root and return its stdout."""
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(config), "CLAUDE_PLUGIN_ROOT": str(root), "TMPDIR": str(config)}
    env.pop("CLAUDE_CODE_SESSION_ID", None)  # the outer session's id would key the warned sentinel instead
    result = subprocess.run(
        ["node", str(hook)], input=payload, env=env, capture_output=True, text=True, encoding="utf-8", timeout=15
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _hooks(source: str) -> dict:
    """Return one plugin's parsed hook registrations."""
    hooks_json = REPO_ROOT / "plugins" / source / "hooks" / "hooks.json"
    return json.loads(hooks_json.read_text(encoding="utf-8"))["hooks"]


@_requires_node
@pytest.mark.parametrize(("hook", "plugin"), COPIES)
class TestStalePluginCheck:
    """Each copy warns only when its own loaded plugin copy is not the installed one."""

    def test_silent_when_loaded_root_is_installed(self, tmp_path: Path, hook: Path, plugin: str) -> None:
        """A session running the installed version prints nothing."""
        root = _install(tmp_path, plugin, "0.63.0")
        _record(tmp_path, plugin, root)

        assert _run(hook, tmp_path, root) == ""

    def test_warns_user_with_plugin_versions_and_resume_step(self, tmp_path: Path, hook: Path, plugin: str) -> None:
        """The user-visible message names this plugin, both versions, and the exact resume command.

        This is the observed failure: a long-running session kept executing an old oss copy after a newer one was
        installed, while the only warning named foundry and reached the model alone.
        """
        old = _install(tmp_path, plugin, "0.62.1")
        (old / ".orphaned_at").write_text("1790681924819", encoding="utf-8")
        _record(tmp_path, plugin, _install(tmp_path, plugin, "0.63.0"))

        message = json.loads(_run(hook, tmp_path, old))["systemMessage"]

        assert f"{plugin} 0.62.1 (replaced — marked orphaned)" in message
        assert "installed version is 0.63.0" in message
        assert message.endswith(f"exit, then claude --resume {SESSION_ID}.")

    def test_tells_model_to_ask_before_using_the_stale_copy(self, tmp_path: Path, hook: Path, plugin: str) -> None:
        """The model context stops this plugin's workflows and requires an explicit user decision."""
        old = _install(tmp_path, plugin, "0.62.1")
        _record(tmp_path, plugin, _install(tmp_path, plugin, "0.63.0"))

        specific = json.loads(_run(hook, tmp_path, old))["hookSpecificOutput"]

        assert specific["hookEventName"] == "SessionStart"
        context = specific["additionalContext"]
        assert f"STALE PLUGIN SESSION ({plugin})" in context
        assert f"Do not run {plugin} skills" in context
        assert "AskUserQuestion" in context
        assert "Never continue silently." in context

    def test_stdout_is_one_json_object(self, tmp_path: Path, hook: Path, plugin: str) -> None:
        """SessionStart parses stdout as JSON only when it starts with ``{`` and ends with ``}`` — no trailing newline.

        Plain text would reach the model only, which is exactly how the earlier warning stayed invisible to the user.
        """
        old = _install(tmp_path, plugin, "0.62.1")
        _record(tmp_path, plugin, _install(tmp_path, plugin, "0.63.0"))

        out = _run(hook, tmp_path, old)

        assert (out[:1], out[-1:]) == ("{", "}")
        assert sorted(json.loads(out)) == ["hookSpecificOutput", "systemMessage"]

    def test_warns_when_orphan_marker_alone_flags_root(self, tmp_path: Path, hook: Path, plugin: str) -> None:
        """A root still named in the record but marked orphaned is stale."""
        root = _install(tmp_path, plugin, "0.63.0")
        (root / ".orphaned_at").write_text("1", encoding="utf-8")
        _record(tmp_path, plugin, root)

        assert "Stale plugin" in json.loads(_run(hook, tmp_path, root))["systemMessage"]

    def test_reports_none_when_plugin_was_uninstalled(self, tmp_path: Path, hook: Path, plugin: str) -> None:
        """A loaded plugin with no install record left is reported as installed version ``none``."""
        old = _install(tmp_path, plugin, "0.62.1")
        _record(tmp_path, plugin)

        assert "installed version is none" in json.loads(_run(hook, tmp_path, old))["systemMessage"]

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param("", id="no-stdin"),
            pytest.param("not json", id="malformed-stdin"),
            pytest.param('{"hook_event_name":"SessionStart"}', id="no-session-id"),
        ],
    )
    def test_warns_without_resume_step_when_session_id_missing(
        self, tmp_path: Path, hook: Path, plugin: str, payload: str
    ) -> None:
        """The check never depends on the payload; without a session id the restart step omits ``--resume``."""
        old = _install(tmp_path, plugin, "0.62.1")
        _record(tmp_path, plugin, _install(tmp_path, plugin, "0.63.0"))

        message = json.loads(_run(hook, tmp_path, old, payload))["systemMessage"]

        assert message.endswith("exit and restart Claude Code.")
        assert "--resume" not in message

    def test_silent_for_root_outside_the_plugin_cache(self, tmp_path: Path, hook: Path, plugin: str) -> None:
        """A source-tree root has no install record to compare, so it stays silent."""
        _record(tmp_path, plugin, _install(tmp_path, plugin, "0.63.0"))
        source = tmp_path / "workspace" / "plugins" / plugin
        source.mkdir(parents=True)

        assert _run(hook, tmp_path, source) == ""

    def test_silent_without_install_record(self, tmp_path: Path, hook: Path, plugin: str) -> None:
        """A missing installed_plugins.json cannot prove staleness, so the hook stays silent."""
        root = _install(tmp_path, plugin, "0.62.1")

        assert _run(hook, tmp_path, root) == ""

    @pytest.mark.parametrize("source", ["compact", "clear"])
    def test_rebuilt_context_after_the_ask_omits_the_mandate(
        self, tmp_path: Path, hook: Path, plugin: str, source: str
    ) -> None:
        """Once this session got the ask, a compact/clear warning keeps the user notice but drops the AskUserQuestion.

        Compaction drops the user's earlier answer, so re-sending the mandate re-asked after every compaction of a long
        stale session — once per stale plugin.
        """
        old = _install(tmp_path, plugin, "0.62.1")
        _record(tmp_path, plugin, _install(tmp_path, plugin, "0.63.0"))
        _run(hook, tmp_path, old)

        out = json.loads(_run(hook, tmp_path, old, _payload(source)))

        assert "Stale plugin" in out["systemMessage"]
        context = out["hookSpecificOutput"]["additionalContext"]
        assert f"STALE PLUGIN SESSION ({plugin})" in context
        assert "AskUserQuestion" not in context
        assert "do not ask again" in context

    @pytest.mark.parametrize("source", ["compact", "clear"])
    def test_first_detection_at_rebuilt_context_still_asks(
        self, tmp_path: Path, hook: Path, plugin: str, source: str
    ) -> None:
        """A plugin replaced mid-session is first seen at a compact/clear; that first warning still asks.

        The loaded copy was current at startup, so this is the earliest point staleness is detectable — the originating
        incident — and dropping the ask there would let the stale workflows run on unasked.
        """
        old = _install(tmp_path, plugin, "0.62.1")
        _record(tmp_path, plugin, _install(tmp_path, plugin, "0.63.0"))

        context = json.loads(_run(hook, tmp_path, old, _payload(source)))["hookSpecificOutput"]["additionalContext"]

        assert "AskUserQuestion" in context

    @pytest.mark.parametrize(
        ("first", "second"),
        [
            pytest.param(_payload("startup"), _payload("resume"), id="resume-after-ask"),
            pytest.param(_payload("startup", None), _payload("compact", None), id="compact-without-session-id"),
        ],
    )
    def test_mandate_repeats(self, tmp_path: Path, hook: Path, plugin: str, first: str, second: str) -> None:
        """Resume always asks, and without a session id no sentinel can scope the ask, so compaction asks again."""
        old = _install(tmp_path, plugin, "0.62.1")
        _record(tmp_path, plugin, _install(tmp_path, plugin, "0.63.0"))
        _run(hook, tmp_path, old, first)

        context = json.loads(_run(hook, tmp_path, old, second))["hookSpecificOutput"]["additionalContext"]

        assert "AskUserQuestion" in context

    def test_silent_for_other_hook_events(self, tmp_path: Path, hook: Path, plugin: str) -> None:
        """An explicit non-SessionStart event is ignored even when the root is stale."""
        old = _install(tmp_path, plugin, "0.62.1")
        _record(tmp_path, plugin, _install(tmp_path, plugin, "0.63.0"))

        assert _run(hook, tmp_path, old, '{"hook_event_name":"Stop"}') == ""


@pytest.mark.parametrize("source", list(PLUGIN_DIRS))
def test_every_plugin_registers_its_own_copy_on_session_start(source: str) -> None:
    """Each plugin runs its own copy once per SessionStart, with no matcher, so every session source is checked.

    One plugin cannot stand in for another: they install and update independently, so a fresh foundry says nothing
    about a replaced oss.
    """
    commands = [hook["command"] for entry in _hooks(source)["SessionStart"] for hook in entry["hooks"]]
    entries = [entry for entry in _hooks(source)["SessionStart"] if SCRIPT in json.dumps(entry)]

    assert sum(re.search(rf"/hooks/{re.escape(SCRIPT)}\"", command) is not None for command in commands) == 1
    assert "matcher" not in entries[0]


def test_copies_are_in_the_propagation_manifest() -> None:
    """A copy outside the manifest drifts silently; the manifest is what the pre-commit drift gate enforces."""
    entries = [
        entry for entry in propagate_shared.MANIFEST if entry["canonical"] == f"plugins/cc_foundry/hooks/{SCRIPT}"
    ]

    assert sorted(entries[0]["copies"]) == sorted(
        f"plugins/{source}/hooks/{SCRIPT}" for source in PLUGIN_DIRS if source != "cc_foundry"
    )
