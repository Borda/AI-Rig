"""Exercise patient identifier normalization and CSV input failures."""

from __future__ import annotations

from pathlib import Path

import pytest
import verify_patient_split as vps


@pytest.mark.parametrize(
    ("train_text", "test_text"),
    [
        pytest.param("patient_id\n123\n", "patient_id\n123\nother\n", id="different-inferred-dtypes"),
        pytest.param("patient_id\n Alice \n", "patient_id\nalice\n", id="case-and-whitespace"),
        pytest.param("patient_id\nStraße\n", "patient_id\nSTRASSE\n", id="unicode-casefold"),
    ],
)
def test_normalized_patient_ids_detect_overlap(tmp_path: Path, train_text: str, test_text: str) -> None:
    """Textual patient identity is independent of CSV dtype, whitespace, and case."""
    train, test = tmp_path / "train.csv", tmp_path / "test.csv"
    train.write_text(train_text, encoding="utf-8")
    test.write_text(test_text, encoding="utf-8")
    assert vps.compute_overlap(train, test) == 1


@pytest.mark.parametrize("empty_text", ["patient_id\n", 'patient_id,label\n,1\n"   ",2\n'])
@pytest.mark.parametrize("empty_split", ["train", "test"])
def test_empty_patient_split_is_input_error(tmp_path: Path, empty_text: str, empty_split: str) -> None:
    """Neither an empty split nor a split with only missing identifiers can pass verification."""
    train, test = tmp_path / "train.csv", tmp_path / "test.csv"
    train.write_text(empty_text if empty_split == "train" else "patient_id\nA\n", encoding="utf-8")
    test.write_text(empty_text if empty_split == "test" else "patient_id\nB\n", encoding="utf-8")
    with pytest.raises(ValueError, match=f"{empty_split}.*no usable"):
        vps.compute_overlap(train, test)


def test_missing_ids_are_dropped_and_reported(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Missing identifiers are counted separately and cannot create spurious overlap."""
    train, test = tmp_path / "train.csv", tmp_path / "test.csv"
    train.write_text('patient_id,label\n,1\n"  ",2\nA,3\n', encoding="utf-8")
    test.write_text("patient_id,label\n,1\nB,2\n", encoding="utf-8")
    assert vps.compute_overlap(train, test) == 0
    errors = capsys.readouterr().err
    assert "train: dropped 2" in errors
    assert "test: dropped 1" in errors


def test_leading_zero_ids_remain_distinct(tmp_path: Path) -> None:
    """Reading identifiers as strings preserves meaningful leading zeroes."""
    train, test = tmp_path / "train.csv", tmp_path / "test.csv"
    train.write_text("patient_id\n001\n", encoding="utf-8")
    test.write_text("patient_id\n1\n", encoding="utf-8")
    assert vps.compute_overlap(train, test) == 0


@pytest.mark.parametrize(
    "csv_bytes",
    [
        pytest.param(b"", id="empty-file"),
        pytest.param(b'patient_id\n"unterminated\n', id="malformed-csv"),
        pytest.param(b"patient_id\n\xff\n", id="invalid-utf8"),
    ],
)
def test_csv_read_errors_exit_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], csv_bytes: bytes
) -> None:
    """CSV parsing failures use the documented input-error status without a traceback."""
    monkeypatch.chdir(tmp_path)
    train, test = tmp_path / "train.csv", tmp_path / "test.csv"
    train.write_bytes(csv_bytes)
    test.write_text("patient_id\nB\n", encoding="utf-8")
    assert vps.main(["--train", str(train), "--test", str(test)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error:")


@pytest.mark.parametrize("external_root", ["temp", "claude-projects"])
def test_csv_paths_outside_project_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, external_root: str
) -> None:
    """A shared temporary directory or another Claude project is not an input trust root."""
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    outside = tmp_path / (".claude/projects/other" if external_root == "claude-projects" else "temp") / "data.csv"
    outside.parent.mkdir(parents=True)
    outside.write_text("patient_id\nA\n", encoding="utf-8")
    assert vps._validate_csv_path(str(outside)) is None


def test_overlap_exits_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A leakage verdict fails the gate while retaining the readable overlap count."""
    monkeypatch.chdir(tmp_path)
    train, test = tmp_path / "train.csv", tmp_path / "test.csv"
    train.write_text("patient_id\nA\n", encoding="utf-8")
    test.write_text("patient_id\na\n", encoding="utf-8")
    assert vps.main(["--train", str(train), "--test", str(test)]) == 1
    assert capsys.readouterr().out == "Overlap: 1 patients\n"
