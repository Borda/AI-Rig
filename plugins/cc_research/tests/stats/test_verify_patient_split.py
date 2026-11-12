"""Tests for ``bin/verify_patient_split.py``.

Covers:
    - ``format_verdict`` pure-function contract — boundary values, validation.
    - ``compute_overlap`` I/O contract — happy path, missing file, missing column.
    - ``main()`` end-to-end CLI: stdin/stdout/exit-code contract preserving the
      original inline-block output shape.
"""

from __future__ import annotations

from collections.abc import Callable
from importlib.util import find_spec
from pathlib import Path

import pytest
import verify_patient_split as vps

_skip_pandas_unavailable = pytest.mark.skipif(find_spec("pandas") is None, reason="requires pandas to read CSV files")

CsvPair = tuple[Path, Path]


@pytest.fixture
def write_csvs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[str, str], CsvPair]:
    """Return a factory that writes ``train.csv`` and ``test.csv`` under ``tmp_path`` and returns both paths."""
    monkeypatch.chdir(tmp_path)

    def write(train_text: str, test_text: str) -> CsvPair:
        """Write both split CSVs from the given text."""
        train, test = tmp_path / "train.csv", tmp_path / "test.csv"
        train.write_text(train_text)
        test.write_text(test_text)
        return train, test

    return write


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
    def test_matches_inline_block(self, overlap_count: int, expected: str) -> None:
        """Verdict strings match the inline block exactly.

        Empty intersection → ``"No patient overlap"``; non-zero overlap → ``"Overlap: <N> patients"``. The inline block
        did not pluralize, so 'patients' is preserved for N=1.
        """
        assert vps.format_verdict(overlap_count) == expected

    def test_rejects_negative_count(self) -> None:
        """A negative overlap count is invalid input and raises ValueError.

        A count is a set size, so a negative value signals a caller bug that must not be formatted into a verdict.
        """
        with pytest.raises(ValueError, match="overlap_count must be non-negative"):
            vps.format_verdict(-1)


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
    def test_counts_distinct_shared_ids(
        self,
        write_csvs: Callable[[str, str], CsvPair],
        train_text: str,
        test_text: str,
        kwargs: dict[str, str],
        expected: int,
    ) -> None:
        """The overlap count is the number of distinct patient ids present in both CSVs.

        Disjoint ids have zero overlap. A patient appearing several times in one CSV counts once (patient 2 appears
        twice in train and once in test → 1). Two distinct shared patients count 2. A nondefault patient identifier
        column is selected via ``column=``.
        """
        train, test = write_csvs(train_text, test_text)
        assert vps.compute_overlap(train, test, **kwargs) == expected

    @pytest.mark.parametrize(
        ("missing", "message"),
        [
            pytest.param("train.csv", "train CSV not found", id="train-missing"),
            pytest.param("test.csv", "test CSV not found", id="test-missing"),
        ],
    )
    def test_missing_file_raises(
        self, tmp_path: Path, write_csvs: Callable[[str, str], CsvPair], missing: str, message: str
    ) -> None:
        """A CSV path not on disk raises FileNotFoundError naming which split is missing.

        Both CSVs are written, then one is removed, so only the path check can fail. The error names the split so a
        caller can tell which input to fix.
        """
        train, test = write_csvs("patient_id\n1\n", "patient_id\n1\n")
        (tmp_path / missing).unlink()
        with pytest.raises(FileNotFoundError, match=message):
            vps.compute_overlap(train, test)

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
        self, write_csvs: Callable[[str, str], CsvPair], train_text: str, test_text: str, message: str
    ) -> None:
        """A CSV lacking the ``patient_id`` column raises KeyError naming which CSV is missing it.

        Validation is symmetric: the train and the test CSV are each checked for the required column.
        """
        train, test = write_csvs(train_text, test_text)
        with pytest.raises(KeyError, match=message):
            vps.compute_overlap(train, test)


class TestMainCLI:
    """End-to-end argv/stdout/exit-code contract."""

    @_skip_pandas_unavailable
    @pytest.mark.parametrize(
        ("train_text", "test_text", "extra_args", "expected_out", "expected_exit"),
        [
            pytest.param(
                "patient_id\n1\n2\n", "patient_id\n3\n4\n", [], "No patient overlap\n", 0, id="disjoint-splits"
            ),
            pytest.param(
                "patient_id\n1\n2\n3\n", "patient_id\n2\n3\n4\n", [], "Overlap: 2 patients\n", 1, id="overlap-detected"
            ),
            pytest.param(
                "subject_id\nA\nB\n",
                "subject_id\nB\nC\n",
                ["--column", "subject_id"],
                "Overlap: 1 patients\n",
                1,
                id="custom-column-via-cli",
            ),
        ],
    )
    def test_prints_verdict_and_returns_overlap_status(
        self,
        write_csvs: Callable[[str, str], CsvPair],
        capsys: pytest.CaptureFixture[str],
        train_text: str,
        test_text: str,
        extra_args: list[str],
        expected_out: str,
        expected_exit: int,
    ) -> None:
        """Readable splits exit zero when disjoint and one on overlap, with a verdict and no stderr.

        Disjoint splits print the clean verdict, overlap prints the count, and ``--column`` overrides the default
        patient identifier column from the command line.
        """
        train, test = write_csvs(train_text, test_text)
        exit_code = vps.main(["--train", str(train), "--test", str(test), *extra_args])
        captured = capsys.readouterr()
        assert (exit_code, captured.out, captured.err) == (expected_exit, expected_out, "")

    @pytest.mark.parametrize(
        ("missing", "message"),
        [
            pytest.param("train.csv", "train CSV not found", id="train-missing"),
            pytest.param("test.csv", "test CSV not found", id="test-missing"),
        ],
    )
    def test_missing_file_exits_two(
        self,
        tmp_path: Path,
        write_csvs: Callable[[str, str], CsvPair],
        capsys: pytest.CaptureFixture[str],
        missing: str,
        message: str,
    ) -> None:
        """A missing input file exits 2 with a descriptive stderr naming the split.

        The path is rejected before any CSV is read, so the failure is reported without needing pandas.
        """
        train, test = write_csvs("patient_id\n1\n", "patient_id\n1\n")
        (tmp_path / missing).unlink()
        exit_code = vps.main(["--train", str(train), "--test", str(test)])
        assert exit_code == 2
        assert message in capsys.readouterr().err

    @_skip_pandas_unavailable
    def test_missing_column_exits_two(
        self, write_csvs: Callable[[str, str], CsvPair], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A split CSV without the required column exits 2 with a descriptive stderr.

        The train CSV has ``id`` instead of ``patient_id``, which compute_overlap reports as KeyError; main turns that
        into a user-facing error and status 2.
        """
        train, test = write_csvs("id\n1\n", "patient_id\n1\n")
        exit_code = vps.main(["--train", str(train), "--test", str(test)])
        assert exit_code == 2
        assert "missing required column" in capsys.readouterr().err
