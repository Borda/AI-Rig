"""Contract test: record-exhausted.py maintains the sentinel for REAL emitted commands.

The PostToolUse hook (`record-exhausted.py`) must recognise every command shape that
codemap skills actually emit — otherwise the exhausted-sentinel is never written and
`guard-redundant-scan.py` never fires, leaving the documented grep-loop worst case
unguarded. The command strings below are copied from the live SKILL.md / codemap-context
blocks that produce them:

- ``scan-query rdeps "mypackage.auth"``                       — query-code/SKILL.md
- ``scan-query fn-rdeps "mypackage.auth::validate_token"``    — query-code/SKILL.md
- ``$SQ rdeps "mypackage.auth"``                              — query-code missing-binary fallback
- ``scan-query --timeout 5 fn-rdeps "...::..." --exclude-tests`` — develop codemap-context.md

The response fixtures mirror the real emission shape: ``cmd_rdeps`` echoes the queried
module as ``module`` and embeds the coverage block under ``index`` (``query.py``
``cmd_rdeps``), while ``cmd_fn_rdeps`` echoes ``qname`` the same way. That identity is
what scopes completeness to the queried target — see :class:`TestCompletenessScoping`.

This converts the hook↔skill contract from silent breakage to a CI-caught failure.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_HOOK = Path(__file__).parent.parent.parent / "hooks" / "record-exhausted.py"

# scan-query emits the forward `query_complete` field and the legacy `exhaustive` alias
# byte-compatibly during the deprecation cycle.
_COMPLETE_BLOCK = {"query_complete": True, "exhaustive": True, "stale": False}


def _response(target: str, index: dict, *, key: str = "module") -> str:
    """Render one scan-query result for *target* with the given coverage block."""
    return json.dumps({key: target, "imported_by": ["pkg.a", "pkg.b"], "index": index})


_EXHAUSTIVE_RESPONSE = _response("mypackage.auth", _COMPLETE_BLOCK)
_NONEXHAUSTIVE_RESPONSE = _response("mypackage.auth", {"query_complete": False, "exhaustive": False, "stale": False})
# Forward-only response: a future index that has dropped the legacy `exhaustive` alias must
# still arm the sentinel via `query_complete` alone.
_QUERY_COMPLETE_ONLY_RESPONSE = _response("mypackage.auth", {"query_complete": True, "stale": False})


def _run(command: str, response: object, session: str, tmp_path: Path, **extra: object) -> None:
    """Feed one PostToolUse(Bash) event through the hook with TMPDIR isolated to tmp_path."""
    payload = {"tool_input": {"command": command}, "tool_response": response, "session_id": session, **extra}
    _drive(payload, tmp_path)


def _drive(payload: dict, tmp_path: Path, cwd: Path | None = None, **env_extra: str) -> None:
    """Feed an arbitrary PostToolUse event through the hook, asserting it fails open."""
    env = {**os.environ, "TMPDIR": str(tmp_path), "TEMP": str(tmp_path), "TMP": str(tmp_path), **env_extra}
    result = subprocess.run(
        [sys.executable, str(_HOOK)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=str(cwd) if cwd else None,
        env=env,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("command", "session"),
    [
        pytest.param('scan-query rdeps "mypackage.auth"', "sess-rdeps-quoted", id="rdeps-quoted"),
        pytest.param("scan-query rdeps mypackage.auth", "sess-rdeps-bare", id="rdeps-bare"),
        pytest.param('$SQ rdeps "mypackage.auth"', "sess-sq-fallback", id="sq-fallback"),
        pytest.param("codemap-py query rdeps mypackage.auth", "sess-codemap-query", id="codemap-query-rdeps"),
    ],
)
def test_sentinel_written_for_real_commands(command: str, session: str, tmp_path: Path) -> None:
    """Every real emitted rdeps form must write the module to the sentinel."""
    _run(command, _EXHAUSTIVE_RESPONSE, session, tmp_path)

    sentinel = tmp_path / f"codemap-exhausted-{session}"
    assert sentinel.exists(), f"sentinel not written for command: {command}"
    assert {"mypackage.auth", "mypackage/auth"} <= set(sentinel.read_text().split())


@pytest.mark.parametrize(
    ("command", "session"),
    [
        pytest.param('scan-query fn-rdeps "mypackage.auth::validate_token"', "sess-fnrdeps", id="fn-rdeps"),
        pytest.param(
            'scan-query --timeout 5 fn-rdeps "mypackage.auth::validate_token" --exclude-tests',
            "sess-interposed",
            id="fn-rdeps-interposed-flags",
        ),
    ],
)
def test_fn_rdeps_records_module_portion(command: str, session: str, tmp_path: Path) -> None:
    """fn-rdeps records the module portion (before ``::``), matching on the echoed qname."""
    response = _response("mypackage.auth::validate_token", _COMPLETE_BLOCK, key="qname")
    _run(command, response, session, tmp_path)

    sentinel = tmp_path / f"codemap-exhausted-{session}"
    assert sentinel.exists(), f"sentinel not written for command: {command}"
    assert {"mypackage.auth", "mypackage/auth"} <= set(sentinel.read_text().split())


def test_non_exhaustive_writes_no_sentinel(tmp_path: Path) -> None:
    """A non-exhaustive result must NOT arm the guard."""
    _run('scan-query rdeps "mypackage.auth"', _NONEXHAUSTIVE_RESPONSE, "sess-nonexh", tmp_path)
    assert not (tmp_path / "codemap-exhausted-sess-nonexh").exists()


def test_truncated_rdeps_preview_writes_no_sentinel(tmp_path: Path) -> None:
    """A graph-complete display slice cannot suppress a later exhaustive caller query."""
    preview = _response(
        "mypackage.auth",
        {"query_complete": True, "exhaustive": True, "truncated": True, "total_available": 1000},
    )

    _run('scan-query rdeps "mypackage.auth" --limit 20', preview, "sess-preview", tmp_path)

    assert not (tmp_path / "codemap-exhausted-sess-preview").exists()


def test_query_complete_alone_arms_sentinel(tmp_path: Path) -> None:
    """A forward-only result (query_complete, no legacy exhaustive) still arms the guard."""
    _run('scan-query rdeps "mypackage.auth"', _QUERY_COMPLETE_ONLY_RESPONSE, "sess-qc-only", tmp_path)

    sentinel = tmp_path / "codemap-exhausted-sess-qc-only"
    assert sentinel.exists(), "query_complete:true alone must write the sentinel"
    assert "mypackage.auth" in sentinel.read_text().split()


def test_unrelated_command_ignored(tmp_path: Path) -> None:
    """A command that is not a scan-query rdeps/fn-rdeps must be ignored."""
    _run('grep -r "import mypackage.auth" .', _EXHAUSTIVE_RESPONSE, "sess-grep", tmp_path)
    assert not (tmp_path / "codemap-exhausted-sess-grep").exists()


def test_codemap_hooks_read_stdin_by_fd_for_simulated_windows() -> None:
    """Codemap hooks must not use POSIX-only /dev/stdin."""
    hooks_dir = _HOOK.parent
    offenders = [
        path.name for path in sorted(hooks_dir.glob("*.py")) if "/dev/stdin" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


class TestCompletenessScoping:
    """Completeness must come from the queried target's OWN result, never a text scan.

    The hook used to regex a flattened dump of the entire ``tool_response`` for ``"query_complete": true``. A combined
    response — one Bash call whose output carries more than the rdeps result — therefore armed the deny for the queried
    module using a completeness flag that belonged to some other sub-result.
    """

    _COMMAND = 'scan-query rdeps "mypackage.auth"'

    def test_mixed_response_does_not_arm_from_foreign_flag(self, tmp_path: Path) -> None:
        """A complete flag on an unrelated sub-result must not arm the queried module."""
        mixed = json.dumps(
            {
                "module": "mypackage.auth",
                "index": {"query_complete": False, "stale": False},
                "other_query": {"module": "mypackage.other", "index": {"query_complete": True}},
            }
        )
        _drive(
            {"tool_input": {"command": self._COMMAND}, "tool_response": mixed, "session_id": "sess-mixed"},
            tmp_path,
        )

        assert not (tmp_path / "codemap-exhausted-sess-mixed").exists()

    def test_response_for_a_different_module_does_not_arm(self, tmp_path: Path) -> None:
        """A complete result whose own identity is another module must not arm this one."""
        _run(self._COMMAND, _response("mypackage.other", _COMPLETE_BLOCK), "sess-foreign", tmp_path)
        assert not (tmp_path / "codemap-exhausted-sess-foreign").exists()

    def test_bash_stdout_envelope_arms(self, tmp_path: Path) -> None:
        """The Bash tool's ``{"stdout": ...}`` response envelope is parsed, not stringified."""
        envelope = {"stdout": _EXHAUSTIVE_RESPONSE, "stderr": "", "interrupted": False, "isImage": False}
        _run(self._COMMAND, envelope, "sess-envelope", tmp_path)

        sentinel = tmp_path / "codemap-exhausted-sess-envelope"
        assert sentinel.exists(), "a Bash stdout envelope carrying a complete result must arm the guard"
        assert "mypackage.auth" in sentinel.read_text().split()

    def test_unparsable_response_does_not_arm(self, tmp_path: Path) -> None:
        """Output the hook cannot parse fails open — it never guesses completeness."""
        _run(self._COMMAND, "mypackage.auth is query_complete: true, trust me", "sess-prose", tmp_path)
        assert not (tmp_path / "codemap-exhausted-sess-prose").exists()


class TestEditInvalidation:
    """A source edit drops the sentinel: an exhaustive caller set cannot outlive its tree."""

    @staticmethod
    def _arm(tmp_path: Path, session: str = "sess-edit") -> Path:
        """Arm the sentinel through the real recording path and return it."""
        _run('scan-query rdeps "mypackage.auth"', _EXHAUSTIVE_RESPONSE, session, tmp_path)
        sentinel = tmp_path / f"codemap-exhausted-{session}"
        assert sentinel.exists(), "precondition: sentinel armed"
        return sentinel

    @pytest.mark.parametrize("tool_name", ["Edit", "Write", "apply_patch", "MultiEdit", "NotebookEdit"])
    def test_python_edit_drops_sentinel(self, tool_name: str, tmp_path: Path) -> None:
        """Editing a Python source file invalidates the recorded caller set."""
        sentinel = self._arm(tmp_path)

        _drive(
            {
                "tool_name": tool_name,
                "tool_input": {"file_path": "/repo/mypackage/auth.py"},
                "session_id": "sess-edit",
            },
            tmp_path,
        )

        assert not sentinel.exists(), f"{tool_name} on a .py file must invalidate the sentinel"

    def test_non_source_edit_keeps_sentinel(self, tmp_path: Path) -> None:
        """Editing a file that cannot change the import graph leaves the sentinel armed."""
        sentinel = self._arm(tmp_path, "sess-doc-edit")

        _drive(
            {"tool_name": "Edit", "tool_input": {"file_path": "/repo/README.md"}, "session_id": "sess-doc-edit"},
            tmp_path,
        )

        assert sentinel.exists(), "a docs edit must not discard a still-valid caller set"

    def test_pathless_edit_drops_sentinel(self, tmp_path: Path) -> None:
        """An edit whose path cannot be read invalidates anyway — fail safe, not open."""
        sentinel = self._arm(tmp_path, "sess-pathless")

        _drive({"tool_name": "Write", "tool_input": {}, "session_id": "sess-pathless"}, tmp_path)

        assert not sentinel.exists(), "an unreadable edit target must be treated as a source change"

    def test_invalidation_without_sentinel_is_a_noop(self, tmp_path: Path) -> None:
        """Invalidating when nothing is armed must not fail the hook."""
        _drive(
            {"tool_name": "Edit", "tool_input": {"file_path": "/repo/a.py"}, "session_id": "sess-absent"},
            tmp_path,
        )
        assert not (tmp_path / "codemap-exhausted-sess-absent").exists()


class TestMissingSessionKey:
    """A missing session_id must not collapse to one machine-global sentinel.

    The literal ``"nosession"`` key this replaces lived in a shared temp directory, so two projects running without a
    session id wrote to — and denied each other through — the same file.
    """

    _EVENT = {"tool_input": {"command": 'scan-query rdeps "mypackage.auth"'}, "tool_response": _EXHAUSTIVE_RESPONSE}

    def test_fallback_key_is_not_the_literal_nosession(self, tmp_path: Path) -> None:
        """The fallback still records, but never under a project-agnostic name."""
        project = tmp_path / "proj-alpha"
        project.mkdir()

        _drive(self._EVENT, tmp_path, cwd=project, CSID="session-one")

        assert not (tmp_path / "codemap-exhausted-nosession").exists()
        written = [p.name for p in tmp_path.glob("codemap-exhausted-*")]
        assert written == ["codemap-exhausted-proj-alpha-session-one"]

    def test_two_projects_do_not_share_one_sentinel(self, tmp_path: Path) -> None:
        """Concurrent session-less runs in different projects stay in separate sentinels."""
        for name in ("proj-alpha", "proj-beta"):
            project = tmp_path / name
            project.mkdir()
            _drive(self._EVENT, tmp_path, cwd=project, CSID="session-one")

        assert sorted(p.name for p in tmp_path.glob("codemap-exhausted-*")) == [
            "codemap-exhausted-proj-alpha-session-one",
            "codemap-exhausted-proj-beta-session-one",
        ]

    def test_two_sessions_in_one_project_do_not_share_one_sentinel(self, tmp_path: Path) -> None:
        """The session token stays the terminal suffix, so concurrent sessions stay apart."""
        project = tmp_path / "proj-alpha"
        project.mkdir()

        for csid in ("session-one", "session-two"):
            _drive(self._EVENT, tmp_path, cwd=project, CSID=csid)

        assert sorted(p.name for p in tmp_path.glob("codemap-exhausted-*")) == [
            "codemap-exhausted-proj-alpha-session-one",
            "codemap-exhausted-proj-alpha-session-two",
        ]

    def test_traversal_in_session_id_cannot_escape_the_temp_dir(self, tmp_path: Path) -> None:
        """A host-supplied session id is sanitized before it reaches a filename."""
        _run('scan-query rdeps "mypackage.auth"', _EXHAUSTIVE_RESPONSE, "../escaped", tmp_path)

        assert not (tmp_path.parent / "codemap-exhausted-escaped").exists()
        assert list(tmp_path.glob("codemap-exhausted-*"))


def test_sentinel_mtime_tracks_last_record(tmp_path: Path) -> None:
    """Recording refreshes the sentinel mtime, which is what the guard's TTL reads."""
    before = time.time()
    _run('scan-query rdeps "mypackage.auth"', _EXHAUSTIVE_RESPONSE, "sess-mtime", tmp_path)

    sentinel = tmp_path / "codemap-exhausted-sess-mtime"
    assert sentinel.stat().st_mtime >= before - 1
