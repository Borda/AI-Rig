"""Subprocess tests for ``hooks/session-restore.js``.

The hook fires on ``SessionStart`` with matcher ``clear``.  It reads
``<cwd>/.claude/state/session/LATEST`` — resolving ``cwd`` from the hook
payload, never ``process.cwd()`` — and injects the handover document that
pointer names back into the fresh session as raw stdout.

Behavioural areas covered:

* **Silence by default** — no pointer, no ``cwd``, wrong event, wrong
  source, blank pointer, traversal pointer, malformed stdin: every one of
  them exits 0 with empty stdout.  A hook that blocks session start is
  worse than a hook that does nothing.
* **Gates** — a document is injected only while it is unconsumed *and*
  younger than 30 minutes.
* **Consumption** — a successful injection rewrites ``consumed: true`` and
  unlinks the pointer, so a second ``/clear`` re-injects nothing.
* **Size guard** — above ~8000 chars only ``## Goal``, the files table,
  ``## Next step`` and a ``/foundry:session recall`` pointer are injected.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

HOOK = "session-restore.js"

_skip_node_unavailable = pytest.mark.skipif(shutil.which("node") is None, reason="requires node on PATH")
HOOK_PATH = Path(__file__).resolve().parents[3] / "hooks" / HOOK


# ── Helpers ───────────────────────────────────────────────────────────────────


def _iso(minutes_ago: float = 0) -> str:
    """Format a relative UTC timestamp to whole seconds for handover age checks.

    >>> earliest = datetime.now(timezone.utc) - timedelta(minutes=5, seconds=1)
    >>> stamp = _iso(5)
    >>> parsed = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    >>> earliest <= parsed <= datetime.now(timezone.utc) - timedelta(minutes=5)
    True
    """
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_handover(
    project: Path,
    slug: str = "plan-x",
    *,
    consumed: str = "false",
    created: str | None = None,
    filler: str = "",
    pointer: str | None = None,
    newline: str = "\n",
) -> Path:
    """Create a handover doc plus its ``LATEST`` pointer under *project*.

    Written as bytes with an explicit line ending rather than through text mode: text mode
    silently emits CRLF on Windows, which made the platform — not the parameter — decide what
    the hook was fed. ``newline="\\r\\n"`` now exercises the CRLF path on every OS.

    Args:
        project: Directory standing in for the hook _payload's ``cwd``.
        slug: Handover slug — also the document's basename.
        consumed: Raw value written to the ``consumed`` frontmatter field.
        created: ISO8601 stamp; defaults to now.
        filler: Extra body text appended under ``## Decisions``, used to
            push the document past the size guard.
        pointer: Contents of ``LATEST``; defaults to *slug*.
        newline: Line ending written into both the pointer and the document.

    Returns:
        Path of the handover document.

    Example:
        >>> from tempfile import TemporaryDirectory
        >>> with TemporaryDirectory() as directory:
        ...     doc = _write_handover(Path(directory), slug="example", created="2026-01-01T00:00:00Z")
        ...     doc.name, (doc.parent / "LATEST").read_bytes()
        ('example.md', b'example\\n')
    """
    store_dir = project / ".claude" / "state" / "session"
    store_dir.mkdir(parents=True, exist_ok=True)
    (store_dir / "LATEST").write_bytes(f"{slug if pointer is None else pointer}{newline}".encode())
    doc = store_dir / f"{slug}.md"
    doc.write_bytes(
        newline.join(
            [
                "---",
                f"slug: {slug}",
                f"created: {created or _iso()}",
                f"consumed: {consumed}",
                "branch: main",
                "---",
                "",
                "## Goal",
                "ship the session handover mechanism",
                "",
                "## Decisions",
                "- inline skill — why: a fork sees no conversation history",
                filler,
                "",
                "## Files touched",
                "",
                "| File | Change | State | Ref |",
                "| --- | --- | --- | --- |",
                "| `session-restore.js` | added SessionStart hook | done | +150/-0 |",
                "",
                "## Next step",
                "run the pytest suite",
                "",
            ]
        ).encode()
    )
    return doc


def _payload(project: Path | None, **overrides) -> dict:
    """Build a clear-session payload, omitting the working directory when no project is supplied.

    >>> _payload(None)
    {'hook_event_name': 'SessionStart', 'source': 'clear'}
    >>> _payload(Path("example"), source="resume")["source"]
    'resume'
    """
    data: dict = {"hook_event_name": "SessionStart", "source": "clear"}
    if project is not None:
        data["cwd"] = str(project)
    data.update(overrides)
    return data


# ── Tests ─────────────────────────────────────────────────────────────────────


@_skip_node_unavailable
class TestSilence:
    """Every unusable input exits 0 with empty stdout, so session start is never blocked."""

    def test_no_pointer(self, run_hook, tmp_path: Path) -> None:
        """A clear with no ``LATEST`` pointer injects nothing.

        The session directory exists but holds no handover, the state of every project that never ran the dump.
        """
        (tmp_path / ".claude" / "state" / "session").mkdir(parents=True)

        result = run_hook(HOOK, _payload(tmp_path))

        assert (result.returncode, result.stdout) == (0, "")

    def test_missing_cwd(self, run_hook) -> None:
        """A payload without ``cwd`` injects nothing instead of falling back to ``process.cwd()``.

        Resolving the project from the process directory would read whichever checkout launched the hook.
        """
        result = run_hook(HOOK, _payload(None))

        assert (result.returncode, result.stdout) == (0, "")

    @pytest.mark.parametrize(
        "overrides",
        [
            pytest.param({"hook_event_name": "SessionEnd"}, id="other-event"),
            pytest.param({"source": "startup"}, id="other-source"),
        ],
    )
    def test_other_event_or_source(self, run_hook, tmp_path: Path, overrides: dict) -> None:
        """A non-SessionStart event or a non-clear source exits 0 with empty stdout.

        The matcher filters in production; the in-code gate is a second line, so a fresh handover stays untouched.
        """
        _write_handover(tmp_path)

        result = run_hook(HOOK, _payload(tmp_path, **overrides))

        assert (result.returncode, result.stdout) == (0, "")

    def test_absent_source_still_injects(self, run_hook, tmp_path: Path) -> None:
        """Source gate is lenient: a payload without the field must not silently no-op.

        Only an explicit non-clear source is rejected; a missing one is treated as a clear.
        """
        _write_handover(tmp_path)
        data = _payload(tmp_path)
        del data["source"]

        result = run_hook(HOOK, data)

        assert "[session] restored" in result.stdout

    @pytest.mark.parametrize(
        "pointer",
        [
            pytest.param("", id="blank-pointer"),
            pytest.param("../../../etc/passwd", id="traversal-pointer"),
            pytest.param("does-not-exist", id="pointer-to-missing-doc"),
        ],
    )
    def test_unusable_pointer(self, run_hook, tmp_path: Path, pointer: str) -> None:
        """An empty marker, a traversal pointer or a pointer to a missing doc exits 0 silently.

        A traversal pointer must never read outside the session directory.
        """
        _write_handover(tmp_path, pointer=pointer)

        result = run_hook(HOOK, _payload(tmp_path))

        assert (result.returncode, result.stdout) == (0, "")

    def test_malformed_stdin(self) -> None:
        """Garbage on stdin exits 0 with empty stdout.

        Bypasses ``run_hook`` deliberately because it JSON-encodes its payload.
        """
        result = subprocess.run(
            ["node", str(HOOK_PATH)], input="not json at all", capture_output=True, text=True, timeout=15
        )

        assert (result.returncode, result.stdout) == (0, "")


@_skip_node_unavailable
class TestGates:
    """A document is injected only while it is unconsumed and younger than 30 minutes."""

    @pytest.mark.parametrize(
        "handover",
        [
            pytest.param({"consumed": "true"}, id="consumed-doc"),
            pytest.param({"created": _iso(minutes_ago=31)}, id="expired-doc"),
            pytest.param({"created": "whenever"}, id="unparseable-created"),
            pytest.param({"consumed": "true", "newline": "\r\n"}, id="crlf-doc-consumed"),
        ],
    )
    def test_gated_doc_is_silent(self, run_hook, tmp_path: Path, handover: dict) -> None:
        """A consumed, expired or unparseable-age document is not injected.

        The gates read frontmatter, so a doc already consumed, older than 30 minutes, or with an unparsable ``created``
        stamp injects nothing; a CRLF document must still be rejected when consumed, because CRLF parsing has to read
        the real flag value, not merely find the key.
        """
        _write_handover(tmp_path, **handover)

        result = run_hook(HOOK, _payload(tmp_path))

        assert result.stdout == ""

    def test_doc_just_inside_window_injects(self, run_hook, tmp_path: Path) -> None:
        """A document created 29 minutes ago is still injected.

        Pins the upper edge of the 30-minute freshness window from the inside.
        """
        _write_handover(tmp_path, created=_iso(minutes_ago=29))

        result = run_hook(HOOK, _payload(tmp_path))

        assert "[session] restored" in result.stdout


@_skip_node_unavailable
class TestInjection:
    """What the model receives from a restored handover."""

    @pytest.mark.parametrize("newline", [pytest.param("\n", id="lf"), pytest.param("\r\n", id="crlf")])
    def test_fresh_doc_injects_full_body(self, run_hook, tmp_path: Path, newline: str) -> None:
        """A fresh document injects its header line and every section, whatever its line endings.

        A doc written on Windows carries CRLF. ``.`` in a JS regex excludes ``\\r`` and ``$`` without the ``m`` flag
        anchors at end-of-string, so a parser splitting on ``\\n`` alone matched no line: every field came back
        undefined and each gate exited silent. The line ending is written explicitly so both run on every platform.
        """
        _write_handover(tmp_path, newline=newline)

        out = run_hook(HOOK, _payload(tmp_path)).stdout

        assert "[session] restored from `plan-x`" in out
        assert "branch main" in out
        assert "## Decisions" in out
        assert "## Files touched" in out
        assert "run the pytest suite" in out

    def test_strips_frontmatter(self, run_hook, tmp_path: Path) -> None:
        """Frontmatter fields never reach the model.

        The slug and consumed flag are bookkeeping for the hook, not context for the new session.
        """
        _write_handover(tmp_path)

        out = run_hook(HOOK, _payload(tmp_path)).stdout

        assert "slug: plan-x" not in out
        assert "consumed:" not in out

    def test_oversized_doc_injects_head_only(self, run_hook, tmp_path: Path) -> None:
        """Above the size guard only the goal, files table, next step and a recall pointer are injected.

        A bulky Decisions section is dropped and the model is pointed at ``/foundry:session recall`` for the rest.
        """
        _write_handover(tmp_path, slug="big-x", filler="- filler decision line, repeated for bulk\n" * 220)

        out = run_hook(HOOK, _payload(tmp_path)).stdout

        assert "filler decision line" not in out
        assert "## Goal" in out
        assert "## Files touched" in out
        assert "## Next step" in out
        assert "/foundry:session recall big-x" in out


@_skip_node_unavailable
class TestConsumption:
    """A successful injection marks the document consumed and removes the pointer."""

    def test_marks_consumed_and_clears_pointer(self, run_hook, tmp_path: Path) -> None:
        """After injection the doc says ``consumed: true`` and ``LATEST`` is gone.

        Without this a second ``/clear`` would replay a handover the user already resumed from.
        """
        doc = _write_handover(tmp_path)

        run_hook(HOOK, _payload(tmp_path))

        assert "consumed: true" in doc.read_text(encoding="utf8")
        assert not (tmp_path / ".claude" / "state" / "session" / "LATEST").exists()

    def test_second_clear_is_idempotent(self, run_hook, tmp_path: Path) -> None:
        """A second clear injects nothing and leaves the doc consumed.

        The first clear restores the handover; repeating it must not restore it again.
        """
        doc = _write_handover(tmp_path)

        first = run_hook(HOOK, _payload(tmp_path))
        second = run_hook(HOOK, _payload(tmp_path))

        assert "[session] restored" in first.stdout
        assert second.stdout == ""
        assert "consumed: true" in doc.read_text(encoding="utf8")

    @pytest.mark.parametrize("newline", [pytest.param("\n", id="lf"), pytest.param("\r\n", id="crlf")])
    def test_rewrites_only_the_flag(self, run_hook, tmp_path: Path, newline: str) -> None:
        """The in-place rewrite changes the flag and nothing else, keeping the closing delimiter and line endings.

        The CRLF case guards against a lone LF left behind by a rewrite that normalises line endings.
        """
        doc = _write_handover(tmp_path, newline=newline)
        before = doc.read_bytes()

        run_hook(HOOK, _payload(tmp_path))

        after = doc.read_bytes()
        assert after == before.replace(b"consumed: false", b"consumed: true", 1)
        assert after.split(newline.encode())[5] == b"---"


def test_hook_is_registered_with_clear_matcher() -> None:
    """The hook is registered exactly once on SessionStart, with the ``clear`` matcher.

    An unregistered SessionStart branch would leave the restore script dead; a wider matcher would fire it on startup.
    """
    hooks_json = HOOK_PATH.parent / "hooks.json"
    entries = json.loads(hooks_json.read_text(encoding="utf8"))["hooks"]["SessionStart"]

    matching = [entry for entry in entries if any(HOOK in hook.get("command", "") for hook in entry.get("hooks", []))]

    assert len(matching) == 1
    assert matching[0].get("matcher") == "clear"
