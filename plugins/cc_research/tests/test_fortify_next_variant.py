"""Tests for ``bin/fortify_next_variant.py`` — the fortify ablation loop's cursor step.

Covers:
* Cursor past the last variant → ``FORTIFY_LOOP_DONE=1``, exit 0
* Unreadable / null / malformed ``.variant_name`` → ``! BLOCKED``, exit 1
* Name normalisation, including an already-prefixed and an upper-case label
* Resume guard: terminal statuses skip and advance the cursor; a timeout status does not
* Cleanup pre-registration in the run-dir accumulator (never a TMPDIR pointer), the ``FORTIFY_WORKTREE`` override,
  and the stale-worktree marker
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "fortify_next_variant.py"
_spec = importlib.util.spec_from_file_location("research_fortify_next_variant", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

is_already_terminal = _mod.is_already_terminal
normalise_variant_name = _mod.normalise_variant_name
read_variant_name = _mod.read_variant_name
main = _mod.main


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Return ``(sentinel_dir, fortify_dir)`` with a fixed session token."""
    sentinels = tmp_path / "sentinels"
    sentinels.mkdir()
    fortify = tmp_path / "run"
    fortify.mkdir()
    monkeypatch.setenv("TMPDIR", str(sentinels))
    monkeypatch.setenv("CSID", "testsess")
    monkeypatch.delenv("FORTIFY_WORKTREE", raising=False)
    return sentinels, fortify


def _write_variants(fortify: Path, names: list[str]) -> None:
    fortify.joinpath("variants.jsonl").write_text(
        "\n".join(json.dumps({"variant_name": n}) for n in names) + "\n", encoding="utf-8"
    )


_FULL_DONE = '{"variant":"variant-full","status":"completed"}\n'


def _after_full(sentinels: Path, fortify: Path, names: list[str], results: str = "") -> None:
    """Lay out a run whose ``full`` variant (line 1) already completed, with the cursor on line 2."""
    _write_variants(fortify, ["full", *names])
    fortify.joinpath("results.jsonl").write_text(_FULL_DONE + results, encoding="utf-8")
    (sentinels / "fortify-variant-idx-testsess").write_text("2\n", encoding="utf-8")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("No Augmentation", "variant-no-augmentation"),
        ("variant-Dropout", "variant-dropout"),
        ("PLAIN", "variant-plain"),
        ("variant-already-lower", "variant-already-lower"),
    ],
    ids=["spaces", "prefixed", "upper", "idempotent"],
)
def test_normalise_variant_name(raw: str, expected: str) -> None:
    assert normalise_variant_name(raw) == expected


@pytest.mark.parametrize(
    ("line", "index", "expected"),
    [
        pytest.param('{"variant_name": "a"}', 1, "a", id="ok"),
        pytest.param('{"variant_name": null}', 1, "", id="null"),
        pytest.param("{}", 1, "", id="missing-key"),
        pytest.param("not json", 1, "", id="malformed"),
        pytest.param('{"variant_name": "a"}', 5, "", id="out-of-range"),
    ],
)
def test_read_variant_name(tmp_path: Path, line: str, index: int, expected: str) -> None:
    """The variant name at a 1-based line index, or an empty string when unreadable, null, absent or out of range.

    A well-formed line yields its name. A null name, a missing key and malformed JSON all degrade to an empty string,
    and so does an index past the last line, which lets the caller report a blocked variant instead of crashing.
    """
    target = tmp_path / "variants.jsonl"
    target.write_text(line + "\n", encoding="utf-8")
    assert read_variant_name(target, index) == expected


def test_missing_variants_file_is_loop_done(env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    _sentinels, fortify = env
    assert main(["--", str(fortify)]) == 0
    assert "FORTIFY_LOOP_DONE=1 — all 0 variants processed" in capsys.readouterr().out


def test_blocked_on_unreadable_name(env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    _sentinels, fortify = env
    fortify.joinpath("variants.jsonl").write_text("{}\n", encoding="utf-8")
    assert main(["--", str(fortify)]) == 1
    assert "! BLOCKED — variants.jsonl line 1 has no readable .variant_name" in capsys.readouterr().out


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("../../outside", id="parent-traversal"),
        pytest.param("nested/dir", id="slash"),
        pytest.param("win\\dir", id="backslash"),
        pytest.param("a\nb", id="newline"),
    ],
)
def test_blocked_on_unsafe_name(env: tuple[Path, Path], capsys: pytest.CaptureFixture[str], name: str) -> None:
    """A variant name that would escape ``worktrees/`` or inject an accumulator line is BLOCKED before any write.

    The name is scientist-authored and joined into ``<fortify_dir>/worktrees/<name>`` for ``git worktree add`` and
    ``remove --force``; a separator escapes that directory and a newline adds an arbitrary path to the cleanup sweep.
    """
    sentinels, fortify = env
    _write_variants(fortify, [name])
    assert main(["--", str(fortify)]) == 1
    assert "contains a path separator or control character" in capsys.readouterr().out
    assert not (sentinels / "fortify-variant-name-testsess").exists()


def test_happy_path_writes_name_and_registers_cleanup(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    sentinels, fortify = env
    _after_full(sentinels, fortify, ["No Augmentation"])
    assert main(["--", str(fortify)]) == 0
    assert (sentinels / "fortify-variant-name-testsess").read_text(encoding="utf-8") == "variant-no-augmentation\n"
    accumulator = fortify / "worktree-paths.txt"
    assert accumulator.read_text(encoding="utf-8") == f"{fortify}/worktrees/variant-no-augmentation\n"
    assert "→ variant 2/2: variant-no-augmentation" in capsys.readouterr().out


def test_cleanup_ignores_pointer_sentinel(env: tuple[Path, Path]) -> None:
    """A repointed ``fortify-paths-ptr`` sentinel in shared TMPDIR no longer redirects the accumulator append."""
    sentinels, fortify = env
    _after_full(sentinels, fortify, ["alpha"])
    elsewhere = sentinels / "elsewhere.txt"
    (sentinels / "fortify-paths-ptr-testsess").write_text(str(elsewhere) + "\n", encoding="utf-8")
    assert main(["--", str(fortify)]) == 0
    assert not elsewhere.exists()
    assert (fortify / "worktree-paths.txt").read_text(encoding="utf-8") == f"{fortify}/worktrees/variant-alpha\n"


def test_fortify_worktree_env_override(env: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    sentinels, fortify = env
    _after_full(sentinels, fortify, ["alpha"])
    monkeypatch.setenv("FORTIFY_WORKTREE", "/custom/wt")
    assert main(["--", str(fortify)]) == 0
    assert (fortify / "worktree-paths.txt").read_text(encoding="utf-8") == "/custom/wt\n"


def test_stale_worktree_directory_is_flagged(env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    """A worktree directory left by an interrupted attempt is reported so 4f runs before ``git worktree add``."""
    sentinels, fortify = env
    _after_full(sentinels, fortify, ["alpha"])
    (fortify / "worktrees" / "variant-alpha").mkdir(parents=True)
    assert main(["--", str(fortify)]) == 0
    assert "FORTIFY_STALE_WORKTREE=1" in capsys.readouterr().out


def test_fresh_worktree_directory_is_not_flagged(env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    """No pre-existing worktree directory → no stale marker."""
    sentinels, fortify = env
    _after_full(sentinels, fortify, ["alpha"])
    assert main(["--", str(fortify)]) == 0
    assert "FORTIFY_STALE_WORKTREE" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "status",
    ["completed", "revert-conflict", "revert-missing", "metric-failed", "diverged"],
)
def test_resume_guard_skips_terminal_variant(
    env: tuple[Path, Path], status: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """A variant already recorded with a terminal status is skipped on resume and the cursor moves past it."""
    sentinels, fortify = env
    _after_full(sentinels, fortify, ["alpha", "beta"], f'{{"variant":"variant-alpha","status":"{status}"}}\n')
    assert main(["--", str(fortify)]) == 0
    out = capsys.readouterr().out
    assert "→ alpha already terminal (non-timeout) in results.jsonl — skipping (resume)" in out
    assert "FORTIFY_SKIP_VARIANT=1" in out
    assert (sentinels / "fortify-variant-idx-testsess").read_text(encoding="utf-8") == "3\n"


def test_resume_guard_retries_timeout_status(env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    """A timed-out variant is deliberately retried, unlike the terminal statuses."""
    sentinels, fortify = env
    _after_full(sentinels, fortify, ["alpha"], '{"variant":"variant-alpha","status":"timeout"}\n')
    assert main(["--", str(fortify)]) == 0
    assert "FORTIFY_SKIP_VARIANT=1" not in capsys.readouterr().out


def test_first_line_must_be_full(env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    """An ablation on line 1 is refused: the baseline must be measured and reproduced before any ablation."""
    sentinels, fortify = env
    _write_variants(fortify, ["no-aug", "full"])
    assert main(["--", str(fortify)]) == 1
    assert "! BLOCKED — variants.jsonl line 1 is 'variant-no-aug', not the full variant" in capsys.readouterr().out
    assert not (sentinels / "fortify-variant-name-testsess").exists()


@pytest.mark.parametrize(
    "results",
    [
        pytest.param('{"variant":"variant-full","status":"diverged"}\n', id="diverged"),
        pytest.param('{"variant":"variant-full","status":"metric-failed"}\n', id="metric-failed"),
        pytest.param("", id="not-recorded"),
    ],
)
def test_full_first_gate_halts_ablations(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str], results: str
) -> None:
    """Past line 1, no ablation runs unless the full variant completed within the divergence tolerance."""
    sentinels, fortify = env
    _write_variants(fortify, ["full", "alpha"])
    fortify.joinpath("results.jsonl").write_text(results, encoding="utf-8")
    (sentinels / "fortify-variant-idx-testsess").write_text("2\n", encoding="utf-8")
    assert main(["--", str(fortify)]) == 1
    assert "! BLOCKED — full-first gate: full variant" in capsys.readouterr().out
    assert not (fortify / "worktree-paths.txt").exists()


@pytest.mark.parametrize(
    ("content", "name", "expected"),
    [
        pytest.param(
            '{"variant":"alpha","status":"completed"}\n', "alpha", True, id="matches-unprefixed-variant-field"
        ),
        pytest.param(
            '{"variant":"alpha","status":"running"}\n{"variant":"beta","status":"completed"}\n',
            "alpha",
            False,
            id="requires-same-line",
        ),
        pytest.param('{"variant":"axb","status":"completed"}\n', "a.b", False, id="treats-name-literally"),
    ],
)
def test_resume_guard_is_already_terminal(tmp_path: Path, content: str, name: str, expected: bool) -> None:
    """The resume guard flags a variant terminal only on its own results line with a terminal status.

    The unprefixed ``variant`` field matches. A terminal status on a different variant's line does not count — status
    and name must share a line. ``re.escape`` keeps a metacharacter-carrying name (``a.b``) from matching a different
    variant (``axb``).
    """
    results = tmp_path / "results.jsonl"
    results.write_text(content, encoding="utf-8")
    assert is_already_terminal(results, name) is expected


def test_blank_lines_do_not_count_toward_total(env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    """``grep -c .`` counts lines holding at least one character."""
    sentinels, fortify = env
    fortify.joinpath("variants.jsonl").write_text('{"variant_name": "alpha"}\n\n', encoding="utf-8")
    (sentinels / "fortify-variant-idx-testsess").write_text("2\n", encoding="utf-8")
    assert main(["--", str(fortify)]) == 0
    assert "all 1 variants processed" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("names", "cursor", "expected_out"),
    [
        pytest.param(
            ["one", "two"], "3\n", "FORTIFY_LOOP_DONE=1 — all 2 variants processed", id="cursor-past-last-is-loop-done"
        ),
        pytest.param(["full"], "not-a-number\n", "→ variant 1/1: variant-full", id="corrupt-cursor-falls-back-to-one"),
    ],
)
def test_cursor_sentinel_drives_loop_position(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str], names: list[str], cursor: str, expected_out: str
) -> None:
    """The cursor sentinel decides where the loop resumes.

    A cursor past the last variant ends the loop (``FORTIFY_LOOP_DONE=1``); an unparsable cursor falls back to the first
    variant.
    """
    sentinels, fortify = env
    _write_variants(fortify, names)
    (sentinels / "fortify-variant-idx-testsess").write_text(cursor, encoding="utf-8")
    assert main(["--", str(fortify)]) == 0
    assert expected_out in capsys.readouterr().out
