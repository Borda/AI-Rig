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

    @pytest.mark.parametrize(
        ("overlap_count", "expected"),
        [
            pytest.param(0, "No patient overlap", id="zero-overlap-clean-message"),
            pytest.param(3, "Overlap: 3 patients", id="positive-overlap-includes-count"),
            pytest.param(1, "Overlap: 1 patients", id="single-overlap-keeps-plural-form"),
        ],
    )
    def test_verdict_matches_inline_block(self, overlap_count: int, expected: str) -> None:
        """Verdict strings match the inline block exactly.

        Empty intersection → ``"No patient overlap"``; non-zero overlap → ``"Overlap: <N> patients"``. The inline block
        did not pluralize, so 'patients' is preserved for N=1.
        """
        assert vps.format_verdict(overlap_count) == expected

    def test_negative_count_raises(self) -> None:
        """Negative count is invalid input — raises ValueError."""
        with pytest.raises(ValueError, match="overlap_count must be non-negative"):
            vps.format_verdict(-1)


# ---------- I/O glue: compute_overlap ----------


@_skip_pandas_unavailable
class TestComputeOverlap:
    """CSV reading and set-intersection contract."""

    @pytest.mark.parametrize(
        ("train_text", "test_text", "kwargs", "expected"),
        [
            pytest.param(
                "patient_id,label\n1,a\n2,b\n3,c\n", "patient_id,label\n4,a\n5,b\n", {}, 0, id="disjoint-splits-zero"
            ),
            pytest.param(
                "patient_id,label\n1,a\n2,b\n2,c\n3,d\n",
                "patient_id,label\n2,a\n4,b\n",
                {},
                1,
                id="shared-patient-counted-once",
            ),
            pytest.param("patient_id,label\n", "patient_id,label\n", {}, 0, id="empty-splits-zero"),
            pytest.param(
                "patient_id\n1\n2\n3\n4\n", "patient_id\n3\n4\n5\n", {}, 2, id="multiple-overlapping-patients"
            ),
            pytest.param(
                "subject_id,label\nA,1\nB,2\n",
                "subject_id,label\nB,1\nC,2\n",
                {"column": "subject_id"},
                1,
                id="custom-column-name",
            ),
        ],
    )
    def test_overlap_count(
        self, tmp_path: Path, train_text: str, test_text: str, kwargs: dict[str, str], expected: int
    ) -> None:
        """The overlap count is the number of distinct patient ids present in both CSVs.

        Disjoint ids and header-only files have zero overlap. A patient appearing several times in one CSV counts once
        (patient 2 appears twice in train and once in test → 1). Two distinct shared patients count 2. A nondefault
        patient identifier column is selected via ``column=``.
        """
        train = tmp_path / "train.csv"
        test = tmp_path / "test.csv"
        train.write_text(train_text)
        test.write_text(test_text)
        assert vps.compute_overlap(train, test, **kwargs) == expected

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

    @pytest.mark.parametrize(
        ("train_text", "test_text", "message"),
        [
            pytest.param(
                "id,label\n1,a\n",
                "patient_id\n1\n",
                "train CSV missing required column 'patient_id'",
                id="train-csv-missing-column",
            ),
            pytest.param(
                "patient_id\n1\n",
                "id,label\n1,a\n",
                "test CSV missing required column 'patient_id'",
                id="test-csv-missing-column",
            ),
        ],
    )
    def test_missing_column_raises_keyerror(
        self, tmp_path: Path, train_text: str, test_text: str, message: str
    ) -> None:
        """A CSV lacking the ``patient_id`` column → KeyError naming which CSV is missing it.

        Validation is symmetric: the train and the test CSV are each checked for the required column.
        """
        train = tmp_path / "train.csv"
        test = tmp_path / "test.csv"
        train.write_text(train_text)
        test.write_text(test_text)
        with pytest.raises(KeyError, match=message):
            vps.compute_overlap(train, test)


# ---------- CLI: main() ----------


class TestMainCLI:
    """End-to-end argv/stdout/exit-code contract."""

    @_skip_pandas_unavailable
    @pytest.mark.parametrize(
        ("train_text", "test_text", "extra_args", "expected_out"),
        [
            pytest.param("patient_id\n1\n2\n", "patient_id\n3\n4\n", [], "No patient overlap\n", id="disjoint-splits"),
            pytest.param(
                "patient_id\n1\n2\n3\n", "patient_id\n2\n3\n4\n", [], "Overlap: 2 patients\n", id="overlap-detected"
            ),
            pytest.param(
                "subject_id\nA\nB\n",
                "subject_id\nB\nC\n",
                ["--column", "subject_id"],
                "Overlap: 1 patients\n",
                id="custom-column-via-cli",
            ),
        ],
    )
    def test_verdict_exits_zero(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        train_text: str,
        test_text: str,
        extra_args: list[str],
        expected_out: str,
    ) -> None:
        """A readable train/test pair → exit 0, stdout matches the inline block's verdict, stderr stays empty.

        Disjoint splits print the clean verdict, overlap prints the count, and ``--column`` overrides the default
        patient identifier column from the command line.
        """
        train = tmp_path / "train.csv"
        test = tmp_path / "test.csv"
        train.write_text(train_text)
        test.write_text(test_text)
        exit_code = vps.main(["--train", str(train), "--test", str(test), *extra_args])
        captured = capsys.readouterr()
        assert exit_code == 0
        assert captured.out == expected_out
        assert captured.err == ""

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
