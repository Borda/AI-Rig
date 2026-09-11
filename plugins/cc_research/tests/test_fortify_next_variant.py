"""Tests for ``bin/fortify_next_variant.py`` — the fortify ablation loop's cursor step.

Covers:
* Cursor past the last variant → ``FORTIFY_LOOP_DONE=1``, exit 0
* Unreadable / null / malformed ``.variant_name`` → ``! BLOCKED``, exit 1
* Name normalisation, including an already-prefixed and an upper-case label
* Resume guard: terminal statuses skip and advance the cursor; a timeout status does not
* Cleanup pre-registration, its fallback accumulator path, and the ``FORTIFY_WORKTREE`` override
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


@pytest.fixture()
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
    ("line", "expected"),
    [
        ('{"variant_name": "a"}', "a"),
        ('{"variant_name": null}', ""),
        ("{}", ""),
        ("not json", ""),
    ],
    ids=["ok", "null", "missing-key", "malformed"],
)
def test_read_variant_name(tmp_path: Path, line: str, expected: str) -> None:
    target = tmp_path / "variants.jsonl"
    target.write_text(line + "\n", encoding="utf-8")
    assert read_variant_name(target, 1) == expected


def test_read_variant_name_out_of_range(tmp_path: Path) -> None:
    target = tmp_path / "variants.jsonl"
    target.write_text('{"variant_name": "a"}\n', encoding="utf-8")
    assert read_variant_name(target, 5) == ""


def test_loop_done_when_cursor_past_last(env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    sentinels, fortify = env
    _write_variants(fortify, ["one", "two"])
    (sentinels / "fortify-variant-idx-testsess").write_text("3\n", encoding="utf-8")
    assert main(["--", str(fortify)]) == 0
    assert "FORTIFY_LOOP_DONE=1 — all 2 variants processed" in capsys.readouterr().out


def test_missing_variants_file_is_loop_done(env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    _sentinels, fortify = env
    assert main(["--", str(fortify)]) == 0
    assert "FORTIFY_LOOP_DONE=1 — all 0 variants processed" in capsys.readouterr().out


def test_blocked_on_unreadable_name(env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    _sentinels, fortify = env
    fortify.joinpath("variants.jsonl").write_text("{}\n", encoding="utf-8")
    assert main(["--", str(fortify)]) == 1
    assert "! BLOCKED — variants.jsonl line 1 has no readable .variant_name" in capsys.readouterr().out


def test_happy_path_writes_name_and_registers_cleanup(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    sentinels, fortify = env
    _write_variants(fortify, ["No Augmentation"])
    accumulator = sentinels / "paths.txt"
    (sentinels / "fortify-paths-ptr-testsess").write_text(str(accumulator) + "\n", encoding="utf-8")
    assert main(["--", str(fortify)]) == 0
    assert (sentinels / "fortify-variant-name-testsess").read_text(encoding="utf-8") == "variant-no-augmentation\n"
    assert accumulator.read_text(encoding="utf-8") == f"{fortify}/worktrees/variant-no-augmentation\n"
    assert "→ variant 1/1: variant-no-augmentation" in capsys.readouterr().out


def test_cleanup_uses_fallback_accumulator_when_pointer_absent(env: tuple[Path, Path]) -> None:
    sentinels, fortify = env
    _write_variants(fortify, ["alpha"])
    assert main(["--", str(fortify)]) == 0
    fallback = sentinels / "fortify-worktree-paths-fallback-testsess"
    assert fallback.read_text(encoding="utf-8") == f"{fortify}/worktrees/variant-alpha\n"


def test_fortify_worktree_env_override(env: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    sentinels, fortify = env
    _write_variants(fortify, ["alpha"])
    monkeypatch.setenv("FORTIFY_WORKTREE", "/custom/wt")
    assert main(["--", str(fortify)]) == 0
    fallback = sentinels / "fortify-worktree-paths-fallback-testsess"
    assert fallback.read_text(encoding="utf-8") == "/custom/wt\n"


@pytest.mark.parametrize(
    "status",
    ["completed", "revert-conflict", "revert-missing", "metric-failed"],
)
def test_resume_guard_skips_terminal_variant(
    env: tuple[Path, Path], status: str, capsys: pytest.CaptureFixture[str]
) -> None:
    sentinels, fortify = env
    _write_variants(fortify, ["alpha", "beta"])
    fortify.joinpath("results.jsonl").write_text(f'{{"variant":"variant-alpha","status":"{status}"}}\n', "utf-8")
    assert main(["--", str(fortify)]) == 0
    out = capsys.readouterr().out
    assert "→ alpha already terminal (non-timeout) in results.jsonl — skipping (resume)" in out
    assert "FORTIFY_SKIP_VARIANT=1" in out
    assert (sentinels / "fortify-variant-idx-testsess").read_text(encoding="utf-8") == "2\n"


def test_resume_guard_retries_timeout_status(env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    """A timed-out variant is deliberately retried, unlike the four terminal statuses."""
    _sentinels, fortify = env
    _write_variants(fortify, ["alpha"])
    fortify.joinpath("results.jsonl").write_text('{"variant":"variant-alpha","status":"timeout"}\n', "utf-8")
    assert main(["--", str(fortify)]) == 0
    assert "FORTIFY_SKIP_VARIANT=1" not in capsys.readouterr().out


def test_resume_guard_matches_unprefixed_variant_field(tmp_path: Path) -> None:
    results = tmp_path / "results.jsonl"
    results.write_text('{"variant":"alpha","status":"completed"}\n', encoding="utf-8")
    assert is_already_terminal(results, "alpha") is True


def test_resume_guard_requires_same_line(tmp_path: Path) -> None:
    results = tmp_path / "results.jsonl"
    results.write_text('{"variant":"alpha","status":"running"}\n{"variant":"beta","status":"completed"}\n', "utf-8")
    assert is_already_terminal(results, "alpha") is False


def test_resume_guard_treats_name_literally(tmp_path: Path) -> None:
    """``re.escape`` keeps a metacharacter-carrying name from matching a different variant."""
    results = tmp_path / "results.jsonl"
    results.write_text('{"variant":"axb","status":"completed"}\n', encoding="utf-8")
    assert is_already_terminal(results, "a.b") is False


def test_blank_lines_do_not_count_toward_total(env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    """``grep -c .`` counts lines holding at least one character."""
    sentinels, fortify = env
    fortify.joinpath("variants.jsonl").write_text('{"variant_name": "alpha"}\n\n', encoding="utf-8")
    (sentinels / "fortify-variant-idx-testsess").write_text("2\n", encoding="utf-8")
    assert main(["--", str(fortify)]) == 0
    assert "all 1 variants processed" in capsys.readouterr().out


def test_corrupt_cursor_falls_back_to_one(env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    sentinels, fortify = env
    _write_variants(fortify, ["alpha"])
    (sentinels / "fortify-variant-idx-testsess").write_text("not-a-number\n", encoding="utf-8")
    assert main(["--", str(fortify)]) == 0
    assert "→ variant 1/1: variant-alpha" in capsys.readouterr().out
