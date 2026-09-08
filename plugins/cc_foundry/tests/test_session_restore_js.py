"""Subprocess tests for ``hooks/session-restore.js``.

The hook fires on ``SessionStart`` with matcher ``clear``.  It reads
``<cwd>/.claude/state/session/LATEST`` — resolving ``cwd`` from the hook
_payload, never ``process.cwd()`` — and injects the handover document that
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
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

HOOK = "session-restore.js"

NODE_UNAVAILABLE = subprocess.run(["node", "--version"], capture_output=True, timeout=5).returncode != 0


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


# ── Silence by default ────────────────────────────────────────────────────────


_skip_node_unavailable = pytest.mark.skipif(
    NODE_UNAVAILABLE,
    reason="requires node on PATH",
)


@_skip_node_unavailable
def test_no_pointer_is_silent(run_hook, tmp_path: Path) -> None:
    (tmp_path / ".claude" / "state" / "session").mkdir(parents=True)
    result = run_hook(HOOK, _payload(tmp_path))
    assert result.returncode == 0
    assert result.stdout == ""


@_skip_node_unavailable
def test_missing_cwd_is_silent(run_hook) -> None:
    result = run_hook(HOOK, _payload(None))
    assert result.returncode == 0
    assert result.stdout == ""


@_skip_node_unavailable
def test_other_event_is_silent(run_hook, tmp_path: Path) -> None:
    _write_handover(tmp_path)
    result = run_hook(HOOK, _payload(tmp_path, hook_event_name="SessionEnd"))
    assert result.returncode == 0
    assert result.stdout == ""


@_skip_node_unavailable
def test_other_source_is_silent(run_hook, tmp_path: Path) -> None:
    """Filter in production; the in-code gate is a second line."""
    _write_handover(tmp_path)
    result = run_hook(HOOK, _payload(tmp_path, source="startup"))
    assert result.returncode == 0
    assert result.stdout == ""


@_skip_node_unavailable
def test_absent_source_still_injects(run_hook, tmp_path: Path) -> None:
    """Source gate is lenient — a _payload without the field must not silently no-op."""
    _write_handover(tmp_path)
    data = _payload(tmp_path)
    del data["source"]
    result = run_hook(HOOK, data)
    assert "[session] restored" in result.stdout


@_skip_node_unavailable
def test_blank_pointer_is_silent(run_hook, tmp_path: Path) -> None:
    """Empty the latest-session marker during session recall."""
    _write_handover(tmp_path, pointer="")
    result = run_hook(HOOK, _payload(tmp_path))
    assert result.returncode == 0
    assert result.stdout == ""


@_skip_node_unavailable
def test_traversal_pointer_is_silent(run_hook, tmp_path: Path) -> None:
    _write_handover(tmp_path, pointer="../../../etc/passwd")
    result = run_hook(HOOK, _payload(tmp_path))
    assert result.returncode == 0
    assert result.stdout == ""


@_skip_node_unavailable
def test_pointer_to_missing_doc_is_silent(run_hook, tmp_path: Path) -> None:
    _write_handover(tmp_path, pointer="does-not-exist")
    result = run_hook(HOOK, _payload(tmp_path))
    assert result.returncode == 0
    assert result.stdout == ""


@_skip_node_unavailable
def test_malformed_stdin_is_silent() -> None:
    """Bypasses ``run_hook`` deliberately — it JSON-encodes its _payload."""
    hook_path = Path(__file__).resolve().parent.parent / "hooks" / HOOK
    result = subprocess.run(
        ["node", str(hook_path)],
        input="not json at all",
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0
    assert result.stdout == ""


# ── Gates ─────────────────────────────────────────────────────────────────────


@_skip_node_unavailable
def test_consumed_doc_is_silent(run_hook, tmp_path: Path) -> None:
    _write_handover(tmp_path, consumed="true")
    result = run_hook(HOOK, _payload(tmp_path))
    assert result.stdout == ""


@_skip_node_unavailable
def test_expired_doc_is_silent(run_hook, tmp_path: Path) -> None:
    _write_handover(tmp_path, created=_iso(minutes_ago=31))
    result = run_hook(HOOK, _payload(tmp_path))
    assert result.stdout == ""


@_skip_node_unavailable
def test_doc_just_inside_window_injects(run_hook, tmp_path: Path) -> None:
    _write_handover(tmp_path, created=_iso(minutes_ago=29))
    result = run_hook(HOOK, _payload(tmp_path))
    assert "[session] restored" in result.stdout


@_skip_node_unavailable
def test_unparseable_created_is_silent(run_hook, tmp_path: Path) -> None:
    _write_handover(tmp_path, created="whenever")
    result = run_hook(HOOK, _payload(tmp_path))
    assert result.stdout == ""


# ── Injection content ─────────────────────────────────────────────────────────


@_skip_node_unavailable
def test_fresh_doc_injects_full_body(run_hook, tmp_path: Path) -> None:
    _write_handover(tmp_path)
    out = run_hook(HOOK, _payload(tmp_path)).stdout
    assert "[session] restored from `plan-x`" in out
    assert "branch main" in out
    assert "## Decisions" in out
    assert "## Files touched" in out
    assert "run the pytest suite" in out


@_skip_node_unavailable
def test_injection_strips_frontmatter(run_hook, tmp_path: Path) -> None:
    _write_handover(tmp_path)
    out = run_hook(HOOK, _payload(tmp_path)).stdout
    assert "slug: plan-x" not in out
    assert "consumed:" not in out


@_skip_node_unavailable
def test_oversized_doc_injects_head_only(run_hook, tmp_path: Path) -> None:
    filler = "- filler decision line, repeated for bulk\n" * 220
    _write_handover(tmp_path, slug="big-x", filler=filler)
    out = run_hook(HOOK, _payload(tmp_path)).stdout
    assert "filler decision line" not in out
    assert "## Goal" in out
    assert "## Files touched" in out
    assert "## Next step" in out
    assert "/foundry:session recall big-x" in out


# ── Consumption ───────────────────────────────────────────────────────────────


@_skip_node_unavailable
def test_injection_marks_consumed_and_clears_pointer(run_hook, tmp_path: Path) -> None:
    doc = _write_handover(tmp_path)
    run_hook(HOOK, _payload(tmp_path))
    assert "consumed: true" in doc.read_text(encoding="utf8")
    assert not (tmp_path / ".claude" / "state" / "session" / "LATEST").exists()


@_skip_node_unavailable
def test_second_clear_is_idempotent(run_hook, tmp_path: Path) -> None:
    doc = _write_handover(tmp_path)
    first = run_hook(HOOK, _payload(tmp_path))
    second = run_hook(HOOK, _payload(tmp_path))
    assert "[session] restored" in first.stdout
    assert second.stdout == ""
    assert "consumed: true" in doc.read_text(encoding="utf8")


@_skip_node_unavailable
def test_consumption_rewrites_only_the_flag(run_hook, tmp_path: Path) -> None:
    """The rewrite must round-trip the doc — closing ``---`` delimiter and body intact."""
    doc = _write_handover(tmp_path)
    before = doc.read_text(encoding="utf8")
    run_hook(HOOK, _payload(tmp_path))
    after = doc.read_text(encoding="utf8")
    assert after == before.replace("consumed: false", "consumed: true", 1)
    assert after.split("\n")[5] == "---"


# ── CRLF documents ────────────────────────────────────────────────────────────
#
# A doc written on Windows carries CRLF. `.` in a JS regex excludes `\r` and `$` without the `m`
# flag anchors at end-of-string, so a frontmatter parser splitting on `\n` alone matched no line
# at all: every field came back undefined and each gate below it exited silent. These run on every
# platform — the line ending is written explicitly, not inherited from the host.


@_skip_node_unavailable
def test_crlf_doc_injects(run_hook, tmp_path: Path) -> None:
    """Frontmatter gates must parse a CRLF document, not fall through to silence."""
    _write_handover(tmp_path, newline="\r\n")
    result = run_hook(HOOK, _payload(tmp_path))
    assert "[session] restored from `plan-x`" in result.stdout
    assert "branch main" in result.stdout


@_skip_node_unavailable
def test_crlf_doc_gates_still_reject_consumed(run_hook, tmp_path: Path) -> None:
    """CRLF parsing must read the real flag value, not merely find the key."""
    _write_handover(tmp_path, consumed="true", newline="\r\n")
    result = run_hook(HOOK, _payload(tmp_path))
    assert result.stdout == ""


@_skip_node_unavailable
def test_crlf_consumption_preserves_line_endings(run_hook, tmp_path: Path) -> None:
    """The in-place rewrite round-trips byte for byte apart from the flag — no lone LF left behind."""
    doc = _write_handover(tmp_path, newline="\r\n")
    before = doc.read_bytes()
    run_hook(HOOK, _payload(tmp_path))
    after = doc.read_bytes()
    assert after == before.replace(b"consumed: false", b"consumed: true", 1)
    assert b"\n" not in after.replace(b"\r\n", b"")


# ── Registration ──────────────────────────────────────────────────────────────


@_skip_node_unavailable
def test_hook_is_registered_with_clear_matcher() -> None:
    """Verify that the result has an unregistered SessionStart branch — this one must not."""
    hooks_json = Path(__file__).resolve().parent.parent / "hooks" / "hooks.json"
    entries = json.loads(hooks_json.read_text(encoding="utf8"))["hooks"]["SessionStart"]
    matching = [entry for entry in entries if any(HOOK in hook.get("command", "") for hook in entry.get("hooks", []))]
    assert len(matching) == 1
    assert matching[0].get("matcher") == "clear"
