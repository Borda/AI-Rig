"""Tests for ``bin/verify_patient_split.py``.

Covers:
    - ``format_verdict`` pure-function contract — boundary values, validation.
    - ``compute_overlap`` I/O contract — happy path, missing file, missing column.
    - ``main()`` end-to-end CLI: stdin/stdout/exit-code contract preserving the
      original inline-block output shape.
"""

from __future__ import annotations

from importlib.util import find_spec
from pathlib import Path

import pytest

import verify_patient_split as vps

_skip_pandas_unavailable = pytest.mark.skipif(find_spec("pandas") is None, reason="requires pandas to read CSV files")


# ---------- Pure function: format_verdict ----------


class TestFormatVerdict:
    """Verdict-line formatting — preserves exact inline-block output strings."""

    def test_zero_overlap_returns_clean_message(self) -> None:
        """Empty intersection → ``"No patient overlap"`` (matches inline block)."""
        assert vps.format_verdict(0) == "No patient overlap"

    def test_positive_overlap_includes_count(self) -> None:
        """Non-zero overlap → ``"Overlap: <N> patients"`` (matches inline block)."""
        assert vps.format_verdict(3) == "Overlap: 3 patients"

    def test_single_overlap_uses_plural_form(self) -> None:
        """Inline block did not pluralize — preserve 'patients' for N=1."""
        assert vps.format_verdict(1) == "Overlap: 1 patients"

    def test_negative_count_raises(self) -> None:
        """Negative count is invalid input — raises ValueError."""
        with pytest.raises(ValueError, match="overlap_count must be non-negative"):
            vps.format_verdict(-1)


# ---------- I/O glue: compute_overlap ----------


@_skip_pandas_unavailable
class TestComputeOverlap:
    """CSV reading and set-intersection contract."""

    def test_disjoint_splits_return_zero(self, tmp_path: Path) -> None:
        """Train and test patient_ids are disjoint → overlap count is 0."""
        train = tmp_path / "train.csv"
        test = tmp_path / "test.csv"
        train.write_text("patient_id,label\n1,a\n2,b\n3,c\n")
        test.write_text("patient_id,label\n4,a\n5,b\n")
        assert vps.compute_overlap(train, test) == 0

    def test_shared_patients_counted_once(self, tmp_path: Path) -> None:
        """Patient appearing multiple times in one CSV counts once in overlap."""
        train = tmp_path / "train.csv"
        test = tmp_path / "test.csv"
        # patient 2 appears twice in train, once in test → counted once.
        train.write_text("patient_id,label\n1,a\n2,b\n2,c\n3,d\n")
        test.write_text("patient_id,label\n2,a\n4,b\n")
        assert vps.compute_overlap(train, test) == 1

    def test_empty_splits_return_zero(self, tmp_path: Path) -> None:
        """CSV files with headers and no rows have zero overlap."""
        train = tmp_path / "train.csv"
        test = tmp_path / "test.csv"
        train.write_text("patient_id,label\n")
        test.write_text("patient_id,label\n")
        assert vps.compute_overlap(train, test) == 0

    def test_multiple_overlapping_patients(self, tmp_path: Path) -> None:
        """Two distinct patients shared → count is 2."""
        train = tmp_path / "train.csv"
        test = tmp_path / "test.csv"
        train.write_text("patient_id\n1\n2\n3\n4\n")
        test.write_text("patient_id\n3\n4\n5\n")
        assert vps.compute_overlap(train, test) == 2

    def test_custom_column_name(self, tmp_path: Path) -> None:
        """Select a nondefault patient identifier column."""
        train = tmp_path / "train.csv"
        test = tmp_path / "test.csv"
        train.write_text("subject_id,label\nA,1\nB,2\n")
        test.write_text("subject_id,label\nB,1\nC,2\n")
        assert vps.compute_overlap(train, test, column="subject_id") == 1

    def test_missing_train_file_raises(self, tmp_path: Path) -> None:
        """Train CSV path not on disk → FileNotFoundError naming the file."""
        test = tmp_path / "test.csv"
        test.write_text("patient_id\n1\n")
        with pytest.raises(FileNotFoundError, match="train CSV not found"):
            vps.compute_overlap(tmp_path / "missing.csv", test)

    def test_missing_test_file_raises(self, tmp_path: Path) -> None:
        """Test CSV path not on disk → FileNotFoundError naming the file."""
        train = tmp_path / "train.csv"
        train.write_text("patient_id\n1\n")
        with pytest.raises(FileNotFoundError, match="test CSV not found"):
            vps.compute_overlap(train, tmp_path / "missing.csv")

    def test_missing_column_raises_keyerror(self, tmp_path: Path) -> None:
        """CSV lacking ``patient_id`` column → KeyError listing available columns."""
        train = tmp_path / "train.csv"
        test = tmp_path / "test.csv"
        train.write_text("id,label\n1,a\n")
        test.write_text("patient_id\n1\n")
        with pytest.raises(KeyError, match="train CSV missing required column 'patient_id'"):
            vps.compute_overlap(train, test)

    def test_missing_test_column_raises_keyerror(self, tmp_path: Path) -> None:
        """The test CSV is validated symmetrically for the required column."""
        train = tmp_path / "train.csv"
        test = tmp_path / "test.csv"
        train.write_text("patient_id\n1\n")
        test.write_text("id,label\n1,a\n")
        with pytest.raises(KeyError, match="test CSV missing required column 'patient_id'"):
            vps.compute_overlap(train, test)


# ---------- CLI: main() ----------


class TestMainCLI:
    """End-to-end argv/stdout/exit-code contract."""

    @_skip_pandas_unavailable
    def test_disjoint_splits_exit_zero_prints_clean(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Disjoint splits → exit 0, stdout matches inline block's clean verdict."""
        train = tmp_path / "train.csv"
        test = tmp_path / "test.csv"
        train.write_text("patient_id\n1\n2\n")
        test.write_text("patient_id\n3\n4\n")
        exit_code = vps.main(["--train", str(train), "--test", str(test)])
        captured = capsys.readouterr()
        assert exit_code == 0
        assert captured.out == "No patient overlap\n"
        assert captured.err == ""

    @_skip_pandas_unavailable
    def test_overlap_exits_zero_prints_count(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Overlap detected → exit 0, stdout matches inline block's overlap verdict."""
        train = tmp_path / "train.csv"
        test = tmp_path / "test.csv"
        train.write_text("patient_id\n1\n2\n3\n")
        test.write_text("patient_id\n2\n3\n4\n")
        exit_code = vps.main(["--train", str(train), "--test", str(test)])
        captured = capsys.readouterr()
        assert exit_code == 0
        assert captured.out == "Overlap: 2 patients\n"

    def test_missing_file_exits_two(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Missing input file → exit 2 with descriptive stderr."""
        test = tmp_path / "test.csv"
        test.write_text("patient_id\n1\n")
        exit_code = vps.main(["--train", str(tmp_path / "absent.csv"), "--test", str(test)])
        captured = capsys.readouterr()
        assert exit_code == 2
        assert "train CSV not found" in captured.err

    @_skip_pandas_unavailable
    def test_missing_column_exits_two(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Required column absent → exit 2 with descriptive stderr."""
        train = tmp_path / "train.csv"
        test = tmp_path / "test.csv"
        train.write_text("id\n1\n")
        test.write_text("patient_id\n1\n")
        exit_code = vps.main(["--train", str(train), "--test", str(test)])
        captured = capsys.readouterr()
        assert exit_code == 2
        assert "missing required column" in captured.err

    @_skip_pandas_unavailable
    def test_custom_column_via_cli(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Override the default patient identifier column from the command line."""
        train = tmp_path / "train.csv"
        test = tmp_path / "test.csv"
        train.write_text("subject_id\nA\nB\n")
        test.write_text("subject_id\nB\nC\n")
        exit_code = vps.main(["--train", str(train), "--test", str(test), "--column", "subject_id"])
        captured = capsys.readouterr()
        assert exit_code == 0
        assert captured.out == "Overlap: 1 patients\n"
