"""Exercise malformed-input and acceptance boundaries of the convergence ledger."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from _modules import load_module

_SCRIPT = Path(__file__).resolve().parents[2] / "shared" / "adversarial_loop.py"
_INVALID_JSON = [
    pytest.param(b"\xff", id="invalid-utf8"),
    pytest.param(b"[" * 100000 + b"0" + b"]" * 100000, id="excessive-nesting"),
    pytest.param(b'{"index":' + b"9" * 5000 + b"}", id="oversized-integer"),
]


@pytest.fixture
def loop_module() -> ModuleType:
    """Load the canonical standalone helper."""
    return load_module("codex_rig_loop_input_safety", _SCRIPT)


@pytest.fixture
def round_record() -> dict[str, object]:
    """Return one clean independently declared review round."""
    return {
        "index": 1,
        "reviewer": {"identity": "reviewer", "independent": True},
        "snapshot": {"revision": "HEAD", "diff_digest": "a" * 64},
        "report_path": "review.md",
        "findings": [],
    }


@pytest.fixture
def ledger_file(tmp_path: Path) -> Path:
    """Write a current ledger header with no completed rounds."""
    path = tmp_path / "loop-ledger.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "implementation_author": "author",
                "current_snapshot": {"revision": "HEAD", "diff_digest": "a" * 64},
            }
        ),
        encoding="utf-8",
    )
    return path


def _cli(path: Path, *flags: str) -> subprocess.CompletedProcess[str]:
    """Run the real command line with captured output."""
    return subprocess.run(
        [sys.executable, str(_SCRIPT), "--ledger", str(path), *flags], capture_output=True, text=True, check=False
    )


@pytest.mark.parametrize("payload", _INVALID_JSON)
@pytest.mark.parametrize("surface", ["header", "rounds", "staged", "actions"])
def test_malformed_json_returns_coded_errors(
    ledger_file: Path, round_record: dict[str, object], surface: str, payload: bytes
) -> None:
    """Keep every JSON boundary inside the CLI's invalid-input exit contract."""
    targets = {
        "header": ledger_file,
        "rounds": ledger_file.with_name("loop-rounds.jsonl"),
        "staged": ledger_file.with_name("loop-rounds.jsonl.rec"),
        "actions": ledger_file.with_name("loop-actions.json"),
    }
    ledger_file.with_name("loop-rounds.jsonl").write_text(json.dumps(round_record) + "\n", encoding="utf-8")
    targets[surface].write_bytes(payload)
    flags = {"header": (), "rounds": (), "staged": ("--append",), "actions": ("--actions", str(targets[surface]))}

    result = _cli(ledger_file, *flags[surface])

    assert result.returncode == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["status"] == "invalid"
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("payload", _INVALID_JSON)
def test_malformed_header_digest_keeps_raw_bytes_identity(
    loop_module: ModuleType, ledger_file: Path, payload: bytes
) -> None:
    """Keep malformed-header digests deterministic even when the JSON parser refuses nesting or integers."""
    ledger_file.write_bytes(payload)

    assert loop_module.ledger_digest(ledger_file) == hashlib.sha256(payload).hexdigest()


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\x85"])
def test_unicode_separators_inside_json_strings_are_not_log_boundaries(
    ledger_file: Path, round_record: dict[str, object], separator: str
) -> None:
    """Read JSONL records by newline without splitting valid Unicode report paths."""
    round_record["report_path"] = f"review{separator}evidence.md"
    ledger_file.with_name("loop-rounds.jsonl").write_text(
        json.dumps(round_record, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    result = _cli(ledger_file, "--require-clean")

    assert result.returncode == 0
    assert json.loads(result.stdout)["reason"] == "clean"


@pytest.mark.parametrize(
    ("author", "reviewer"),
    [
        pytest.param("Author", "author", id="case"),
        pytest.param("author", " author ", id="reviewer-whitespace"),
        pytest.param(" author ", "AUTHOR", id="author-whitespace"),
    ],
)
def test_normalized_self_review_is_rejected(
    loop_module: ModuleType, ledger_file: Path, round_record: dict[str, object], author: str, reviewer: str
) -> None:
    """Case and padding cannot turn the implementation author into an independent reviewer."""
    ledger = json.loads(ledger_file.read_text(encoding="utf-8"))
    round_record["reviewer"] = {"identity": reviewer, "independent": True}
    ledger.update(implementation_author=author, rounds=[round_record])

    assert "round-1-self-review-forbidden" in loop_module.validate_ledger(ledger)
    assert not loop_module._is_independent_round(round_record, author)


def test_historical_clean_ledger_cannot_pass_current_acceptance(
    ledger_file: Path, round_record: dict[str, object]
) -> None:
    """Keep schema-one archives readable while refusing them as a current clean result."""
    ledger = json.loads(ledger_file.read_text(encoding="utf-8"))
    ledger.update(schema_version=1, rounds=[round_record])
    ledger_file.write_text(json.dumps(ledger), encoding="utf-8")

    historical = _cli(ledger_file)
    acceptance = _cli(ledger_file, "--require-clean")

    assert historical.returncode == 0
    assert json.loads(historical.stdout)["reason"] == "clean"
    assert acceptance.returncode == 1
    assert json.loads(acceptance.stdout) == json.loads(historical.stdout)
    assert acceptance.stderr.strip() == "require-clean-schema-not-current"


def test_round_append_write_failure_returns_error(
    loop_module: ModuleType, ledger_file: Path, round_record: dict[str, object]
) -> None:
    """Report a failed round-log write and retain the staged record."""
    staged = ledger_file.with_name("loop-rounds.jsonl.rec")
    staged.write_text(json.dumps(round_record), encoding="utf-8")
    ledger_file.with_name("loop-rounds.jsonl").mkdir()

    errors = loop_module.append_round(ledger_file)

    assert errors[0].startswith("append-round-write-failed:")
    assert staged.is_file()


def test_round_append_unlink_failure_keeps_success_and_warns(
    loop_module: ModuleType,
    ledger_file: Path,
    round_record: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A retained staged record cannot hide an already appended round behind a traceback."""
    staged = ledger_file.with_name("loop-rounds.jsonl.rec")
    staged.write_text(json.dumps(round_record), encoding="utf-8")

    def deny_unlink(path: Path, missing_ok: bool = False) -> None:
        """Model a filesystem refusing to remove the staged record."""
        raise PermissionError("staged record is protected")

    monkeypatch.setattr(Path, "unlink", deny_unlink)

    errors = loop_module.append_round(ledger_file)

    assert errors == []
    assert loop_module.load_ledger(ledger_file)["rounds"] == [round_record]
    assert staged.is_file()
    assert "warning: round-staged-record-not-removed:PermissionError" in capsys.readouterr().err
