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
from pathlib import Path
from typing import ClassVar

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


_SESSION = "sess"  # every test owns its TMPDIR, so one session id never collides across tests
_RDEPS_COMMAND = 'scan-query rdeps "mypackage.auth"'
_FN_RDEPS_RESPONSE = _response("mypackage.auth::validate_token", _COMPLETE_BLOCK, key="qname")
_ARMED_TOKENS = {"mypackage.auth", "mypackage/auth"}


def _sentinel(tmp_path: Path, session: str = _SESSION) -> Path:
    """Return the path of the exhausted-sentinel file the hook writes for *session* under the isolated TMPDIR."""
    return tmp_path / f"codemap-exhausted-{session}"


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
    ("command", "response"),
    [
        pytest.param(_RDEPS_COMMAND, _EXHAUSTIVE_RESPONSE, id="rdeps-quoted"),
        pytest.param("scan-query rdeps mypackage.auth", _EXHAUSTIVE_RESPONSE, id="rdeps-bare"),
        pytest.param('$SQ rdeps "mypackage.auth"', _EXHAUSTIVE_RESPONSE, id="sq-fallback"),
        pytest.param("codemap-py query rdeps mypackage.auth", _EXHAUSTIVE_RESPONSE, id="codemap-query-rdeps"),
        pytest.param(
            'scan-query fn-rdeps "mypackage.auth::validate_token"', _FN_RDEPS_RESPONSE, id="fn-rdeps-module-portion"
        ),
        pytest.param(
            'scan-query --timeout 5 fn-rdeps "mypackage.auth::validate_token" --exclude-tests',
            _FN_RDEPS_RESPONSE,
            id="fn-rdeps-interposed-flags",
        ),
        # A future index that has dropped the legacy `exhaustive` alias must still arm via `query_complete` alone.
        pytest.param(_RDEPS_COMMAND, _QUERY_COMPLETE_ONLY_RESPONSE, id="query-complete-alone"),
        # The Bash tool's `{"stdout": ...}` envelope is parsed, not stringified.
        pytest.param(
            _RDEPS_COMMAND,
            {"stdout": _EXHAUSTIVE_RESPONSE, "stderr": "", "interrupted": False, "isImage": False},
            id="bash-stdout-envelope",
        ),
    ],
)
def test_sentinel_written_for_real_emitted_commands(command: str, response: object, tmp_path: Path) -> None:
    """Every real emitted rdeps/fn-rdeps form with a complete result writes the module to the sentinel.

    Each command string is copied from a live skill block (see module docstring) and paired with the response shape it
    produces; fn-rdeps matches on the echoed qname and records the module portion before ``::``.
    """
    _run(command, response, _SESSION, tmp_path)

    sentinel = _sentinel(tmp_path)
    assert sentinel.exists(), f"sentinel not written for command: {command}"
    assert _ARMED_TOKENS <= set(sentinel.read_text().split())


_TRUNCATED_PREVIEW_RESPONSE = _response(
    "mypackage.auth", {"query_complete": True, "exhaustive": True, "truncated": True, "total_available": 1000}
)


@pytest.mark.parametrize(
    ("command", "response"),
    [
        pytest.param(_RDEPS_COMMAND, _NONEXHAUSTIVE_RESPONSE, id="non-exhaustive-result"),
        # A graph-complete display slice cannot suppress a later exhaustive caller query.
        pytest.param(
            'scan-query rdeps "mypackage.auth" --limit 20', _TRUNCATED_PREVIEW_RESPONSE, id="truncated-rdeps-preview"
        ),
        pytest.param('grep -r "import mypackage.auth" .', _EXHAUSTIVE_RESPONSE, id="unrelated-command"),
        # A complete result whose own identity is another module must not arm this one.
        pytest.param(
            _RDEPS_COMMAND, _response("mypackage.other", _COMPLETE_BLOCK), id="response-for-a-different-module"
        ),
        # Output the hook cannot parse fails open — it never guesses completeness.
        pytest.param(_RDEPS_COMMAND, "mypackage.auth is query_complete: true, trust me", id="unparsable-response"),
        # A complete flag on an unrelated sub-result must not arm the queried module.
        pytest.param(
            _RDEPS_COMMAND,
            json.dumps(
                {
                    "module": "mypackage.auth",
                    "index": {"query_complete": False, "stale": False},
                    "other_query": {"module": "mypackage.other", "index": {"query_complete": True}},
                }
            ),
            id="mixed-response-foreign-flag",
        ),
    ],
)
def test_result_that_does_not_arm_leaves_no_sentinel(command: str, response: object, tmp_path: Path) -> None:
    """A non-exhaustive, truncated, unrelated, foreign-module, unparsable or mixed result must NOT arm the guard.

    Completeness is taken only from the queried target's OWN, parseable, graph-complete result of a scan-query rdeps/fn-
    rdeps command, never from a text scan of the whole response; anything else leaves no sentinel behind.
    """
    _run(command, response, _SESSION, tmp_path)

    assert not _sentinel(tmp_path).exists()


def test_codemap_hooks_read_stdin_by_fd_for_simulated_windows() -> None:
    """Codemap hooks must not use POSIX-only /dev/stdin.

    Hook sources are scanned for the literal path; Windows has no such file, so any hit would break the hook there.
    """
    hooks_dir = _HOOK.parent
    offenders = [
        path.name for path in sorted(hooks_dir.glob("*.py")) if "/dev/stdin" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


class TestEditInvalidation:
    """A source edit drops the sentinel: an exhaustive caller set cannot outlive its tree."""

    @staticmethod
    def _arm(tmp_path: Path, session: str = _SESSION) -> Path:
        """Arm the sentinel through the real recording path and return it."""
        _run(_RDEPS_COMMAND, _EXHAUSTIVE_RESPONSE, session, tmp_path)
        sentinel = _sentinel(tmp_path, session)
        assert sentinel.exists(), "precondition: sentinel armed"
        return sentinel

    @pytest.mark.parametrize(
        ("tool_name", "tool_input"),
        [
            pytest.param("Edit", {"file_path": "/repo/mypackage/auth.py"}, id="edit-python-file"),
            pytest.param("Write", {"file_path": "/repo/mypackage/auth.py"}, id="write-python-file"),
            pytest.param("apply_patch", {"file_path": "/repo/mypackage/auth.py"}, id="apply-patch-python-file"),
            pytest.param("MultiEdit", {"file_path": "/repo/mypackage/auth.py"}, id="multi-edit-python-file"),
            pytest.param("NotebookEdit", {"file_path": "/repo/mypackage/auth.py"}, id="notebook-edit-python-file"),
            # An edit whose path cannot be read invalidates anyway — fail safe, not open.
            pytest.param("Write", {}, id="pathless-edit"),
        ],
    )
    def test_source_edit_drops_sentinel(self, tool_name: str, tool_input: dict, tmp_path: Path) -> None:
        """Editing a Python source file, or an edit with an unreadable path, invalidates the recorded caller set.

        The sentinel records an exhaustive caller set for a module; any edit that could change the import graph makes
        that set untrustworthy, so the guard must stop honouring it.
        """
        sentinel = self._arm(tmp_path)

        _drive({"tool_name": tool_name, "tool_input": tool_input, "session_id": _SESSION}, tmp_path)

        assert not sentinel.exists(), (
            f"{tool_name} on {tool_input or 'an unreadable target'} must invalidate the sentinel"
        )

    def test_non_source_edit_keeps_sentinel(self, tmp_path: Path) -> None:
        """Editing a file that cannot change the import graph leaves the sentinel armed.

        A docs edit cannot change who imports a module, so discarding the caller set would only force a needless re-
        query.
        """
        sentinel = self._arm(tmp_path)

        _drive({"tool_name": "Edit", "tool_input": {"file_path": "/repo/README.md"}, "session_id": _SESSION}, tmp_path)

        assert sentinel.exists(), "a docs edit must not discard a still-valid caller set"

    def test_invalidation_without_sentinel_is_a_noop(self, tmp_path: Path) -> None:
        """Invalidating when nothing is armed must not fail the hook.

        The hook fires on every edit, most of them in sessions that never armed a sentinel; ``_drive`` asserts exit 0.
        """
        _drive({"tool_name": "Edit", "tool_input": {"file_path": "/repo/a.py"}, "session_id": _SESSION}, tmp_path)

        assert not _sentinel(tmp_path).exists()


class TestMissingSessionKey:
    """A missing session_id must not collapse to one machine-global sentinel.

    The literal ``"nosession"`` key this replaces lived in a shared temp directory, so two projects running without a
    session id wrote to — and denied each other through — the same file.
    """

    _EVENT: ClassVar = {"tool_input": {"command": _RDEPS_COMMAND}, "tool_response": _EXHAUSTIVE_RESPONSE}

    @staticmethod
    def _drive_session_less(tmp_path: Path, runs: list[tuple[str, str]]) -> list[str]:
        """Feed the session-less event once per ``(project, CSID)`` pair and return the sentinel names written."""
        for name, csid in runs:
            project = tmp_path / name
            project.mkdir(exist_ok=True)
            _drive(TestMissingSessionKey._EVENT, tmp_path, cwd=project, CSID=csid)
        return sorted(p.name for p in tmp_path.glob("codemap-exhausted-*"))

    def test_fallback_key_is_not_the_literal_nosession(self, tmp_path: Path) -> None:
        """The fallback still records, but never under a project-agnostic name.

        The sentinel name must be derived from the working project and the session token instead of a constant.
        """
        written = self._drive_session_less(tmp_path, [("proj-alpha", "session-one")])

        assert not (tmp_path / "codemap-exhausted-nosession").exists()
        assert written == ["codemap-exhausted-proj-alpha-session-one"]

    @pytest.mark.parametrize(
        ("runs", "expected"),
        [
            pytest.param(
                [("proj-alpha", "session-one"), ("proj-beta", "session-one")],
                ["codemap-exhausted-proj-alpha-session-one", "codemap-exhausted-proj-beta-session-one"],
                id="two-projects-one-session",
            ),
            pytest.param(
                [("proj-alpha", "session-one"), ("proj-alpha", "session-two")],
                ["codemap-exhausted-proj-alpha-session-one", "codemap-exhausted-proj-alpha-session-two"],
                id="two-sessions-one-project",
            ),
        ],
    )
    def test_concurrent_runs_do_not_share_one_sentinel(
        self, tmp_path: Path, runs: list[tuple[str, str]], expected: list[str]
    ) -> None:
        """Session-less runs differing by project or by session token stay in separate sentinels.

        The session token stays the terminal suffix, so concurrent sessions in one project stay apart as well.
        """
        assert self._drive_session_less(tmp_path, runs) == expected

    def test_traversal_in_session_id_cannot_escape_the_temp_dir(self, tmp_path: Path) -> None:
        """A host-supplied session id is sanitized before it reaches a filename.

        A ``../`` session id would otherwise write the sentinel outside the temp directory.
        """
        _run(_RDEPS_COMMAND, _EXHAUSTIVE_RESPONSE, "../escaped", tmp_path)

        assert not (tmp_path.parent / "codemap-exhausted-escaped").exists()
        assert list(tmp_path.glob("codemap-exhausted-*"))


def test_sentinel_mtime_tracks_last_record(tmp_path: Path) -> None:
    """Recording refreshes the sentinel mtime, which is what the guard's TTL reads.

    An armed sentinel is aged to the epoch, then recorded again; a fresh mtime shows the guard's TTL restarts from the
    latest record rather than the first.
    """
    _run(_RDEPS_COMMAND, _EXHAUSTIVE_RESPONSE, _SESSION, tmp_path)
    sentinel = _sentinel(tmp_path)
    os.utime(sentinel, (1, 1))

    _run(_RDEPS_COMMAND, _EXHAUSTIVE_RESPONSE, _SESSION, tmp_path)

    assert sentinel.stat().st_mtime > 1
