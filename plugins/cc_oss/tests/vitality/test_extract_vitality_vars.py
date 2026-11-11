"""Tests for ``bin/extract_vitality_vars.py``.

``extract_vars`` and ``emit`` are pure and run in-process. The ``--keep-state`` tests redirect sentinel writes into
``tmp_path`` through ``TMPDIR`` and ``CSID``; one integration test reads a sentinel back with the documented bash
command to prove score text is never evaluated as shell.
"""

from __future__ import annotations

import json
import shlex
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from extract_vitality_vars import emit, extract_vars, main


def test_null_health_uses_unavailable_sentinel() -> None:
    """Unknown health reaches shell consumers as the existing numeric unavailable sentinel."""
    assert extract_vars({"health_score_pct": None})["HEALTH_SCORE_PCT"] == "-1"


def _scores(overrides: dict | None = None) -> dict:
    """Build complete vitality-score input with optional top-level overrides.

    Examples:
        >>> _scores({"health_score_pct": 99.0})["health_score_pct"]
        99.0
        >>> len(_scores()["axes"])
        9
    """
    base: dict = {
        "analysis_now": "1700000000",
        "overall_confidence": 0.85,
        "health_score_pct": 72.3,
        "axis3_202_pending": False,
        "total_passes": 2,
        "confidence_history": "0.80:0.85",
        "axes": {str(n): {"score": float(n), "conf": 0.9, "label": "🟢", "signal": f"sig{n}"} for n in range(1, 10)},
        "weights": {str(n): 0.11 for n in range(1, 10)},
    }
    if overrides:
        base.update(overrides)
    return base


def _write_files(root: Path, files: dict[str, str]) -> None:
    """Create each ``name -> text`` file under ``root``."""
    for name, text in files.items():
        (root / name).write_text(text, encoding="utf-8")


def _sentinel(root: Path, key: str) -> Path:
    """Return the sentinel path ``--keep-state vars`` uses for a variable name."""
    return root / f"vars-{key.lower().replace('_', '-')}-vitality-test"


def _owner_only(paths: list[Path]) -> bool:
    """Report whether every file is readable by its owner alone; POSIX modes do not exist on Windows."""
    return sys.platform == "win32" or all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in paths)


@pytest.fixture
def scores_file(tmp_path: Path) -> Callable[..., Path]:
    """Return a writer that stores a scores payload (with optional top-level overrides) and yields its path."""

    def _write(overrides: dict | None = None) -> Path:
        """Write the payload to ``scores.json`` under ``tmp_path``."""
        path = tmp_path / "scores.json"
        path.write_text(json.dumps(_scores(overrides)), encoding="utf-8")
        return path

    return _write


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``--keep-state`` sentinels at ``tmp_path`` with a fixed session token."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "vitality-test")
    return tmp_path


class TestExtractVars:
    """extract_vars: flatten a vitality-score payload into upper-case string variables."""

    def test_stringifies_top_level_fields(self) -> None:
        """Every top-level scalar becomes an upper-case string variable.

        A complete payload with ``axis3_202_pending`` false exercises each scalar once; shell consumers only ever see
        strings, so numbers and booleans must arrive pre-rendered.
        """
        expected = {
            "ANALYSIS_NOW": "1700000000",
            "OVERALL_CONFIDENCE": "0.85",
            "HEALTH_SCORE_PCT": "72.3",
            "AXIS3_202_PENDING": "false",
            "TOTAL_PASSES": "2",
            "CONFIDENCE_HISTORY": "0.80:0.85",
        }
        variables = extract_vars(_scores())
        assert {key: variables[key] for key in expected} == expected

    @pytest.mark.parametrize(
        ("pending", "expected"), [pytest.param(True, "true", id="true"), pytest.param(False, "false", id="false")]
    )
    def test_renders_axis3_pending_as_lowercase_word(self, pending: bool, expected: str) -> None:
        """The axis-3 pending flag is the lower-case word ``true`` or ``false``, never Python's ``True``/``False``.

        The report template compares the variable against the literal words, so a capitalised value would silently take
        the wrong branch.
        """
        assert extract_vars(_scores({"axis3_202_pending": pending}))["AXIS3_202_PENDING"] == expected

    def test_exposes_every_axis_score(self) -> None:
        """Each of the nine axes publishes its score as ``AXIS<n>_SCORE``."""
        expected = {f"AXIS{n}_SCORE": str(float(n)) for n in range(1, 10)}
        variables = extract_vars(_scores())
        assert {key: variables[key] for key in expected} == expected

    def test_null_axis_score_becomes_minus_one(self) -> None:
        """An axis with no score reports ``-1`` so the template can tell it from a real zero."""
        scores = _scores()
        scores["axes"]["3"] = {"score": None, "conf": 0.0, "label": "⚪", "signal": ""}
        assert extract_vars(scores)["AXIS3_SCORE"] == "-1"

    def test_exposes_axis_status_and_signal(self) -> None:
        """Each axis publishes its label as ``_STATUS`` and its prose as ``_SIGNAL``."""
        variables = extract_vars(_scores())
        assert (variables["AXIS1_STATUS"], variables["AXIS1_SIGNAL"]) == ("🟢", "sig1")

    def test_rounds_weights_to_whole_percent(self) -> None:
        """A fractional weight is published as a whole percentage."""
        scores = _scores()
        scores["weights"]["1"] = 0.17
        assert extract_vars(scores)["WEIGHT_1"] == "17"

    def test_missing_axis_uses_defaults(self) -> None:
        """An axis absent from the payload gets score ``-1`` and the neutral ``⚪`` status instead of a KeyError."""
        scores = _scores()
        del scores["axes"]["5"]
        variables = extract_vars(scores)
        assert (variables["AXIS5_SCORE"], variables["AXIS5_STATUS"]) == ("-1", "⚪")

    def test_confidence_history_falls_back_to_overall(self) -> None:
        """Without a confidence history the overall confidence stands in for it."""
        scores = _scores()
        del scores["confidence_history"]
        assert extract_vars(scores)["CONFIDENCE_HISTORY"] == "0.85"


class TestEmit:
    """Emit: render variables as shell assignments."""

    @pytest.mark.parametrize(
        ("variables", "expected"),
        [
            pytest.param({"FOO": "bar"}, "FOO=bar", id="simple-values-unquoted"),
            pytest.param({"MSG": "hello world"}, "MSG='hello world'", id="spaces-single-quoted"),
            pytest.param({"A": "1", "B": "2"}, "A=1\nB=2", id="multiple-vars-newline-separated"),
        ],
    )
    def test_formats_assignments(self, variables: dict[str, str], expected: str) -> None:
        """Plain values stay unquoted, values with spaces are single-quoted, several vars are newline-separated."""
        assert emit(variables) == expected

    @pytest.mark.parametrize(
        "value",
        [
            "has 'single quotes'",
            'has "double quotes"',
            "semi;colon",
            "$(touch pwned)",
            "",
            "line1\nline2",
            "🟡 review needed",
        ],
    )
    def test_shell_sensitive_values_round_trip(self, value: str) -> None:
        """A value full of shell metacharacters survives a shell-style split unchanged.

        Legacy callers ``eval`` the output; quoting must make every special character literal so a score field can never
        inject a command.
        """
        assignment = shlex.split(emit({"MSG": value}))[0]
        key, decoded = assignment.split("=", 1)
        assert (key, decoded) == ("MSG", value)


class TestMain:
    """Main: command-line entry point for the legacy stdout mode."""

    def test_prints_variables_and_exits_zero(
        self, scores_file: Callable[..., Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A valid scores file prints top-level, per-axis and weight variables and exits 0."""
        rc = main([str(scores_file())])
        out = capsys.readouterr().out
        assert (rc, "ANALYSIS_NOW=" in out, "AXIS1_SCORE=" in out, "WEIGHT_9=" in out) == (0, True, True, True)

    @pytest.mark.parametrize(
        ("files", "argv"),
        [
            pytest.param({}, [], id="no-args"),
            pytest.param({}, ["missing.json"], id="missing-file"),
            pytest.param({"bad.json": "not json"}, ["bad.json"], id="invalid-json"),
        ],
    )
    def test_rejects_unusable_input(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, files: dict[str, str], argv: list[str]
    ) -> None:
        """No argument, a missing file or malformed JSON each exit 1 instead of raising.

        The working directory is ``tmp_path`` so the relative file names resolve inside the sandbox.
        """
        monkeypatch.chdir(tmp_path)
        _write_files(tmp_path, files)
        assert main(argv) == 1

    def test_stdout_is_eval_safe_assignments_only(
        self, scores_file: Callable[..., Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Documented call site ``extract_vitality_vars.py SCORES_FILE`` prints ONLY ``VAR=value`` lines.

        Legacy consumers parse shell assignments, so a stray argparse banner would corrupt the output contract; every
        line must be an assignment. Session-file consumers use ``--keep-state`` instead.
        """
        rc = main([str(scores_file())])
        lines = capsys.readouterr().out.strip().splitlines()
        assert (rc, bool(lines), all("=" in line for line in lines)) == (0, True, True)


class TestKeepState:
    """``--keep-state vars``: write one private sentinel per variable instead of printing shell source."""

    @pytest.mark.parametrize(
        "signal",
        [
            pytest.param("$(touch pwned); 'quoted' \\backslash", id="shell-metacharacters"),
            pytest.param("", id="empty-clears-stale-value"),
        ],
    )
    def test_stores_literal_values_privately(
        self,
        state_dir: Path,
        scores_file: Callable[..., Path],
        capsys: pytest.CaptureFixture[str],
        signal: str,
    ) -> None:
        """State mode prints no shell source, overwrites stale values and stores literal UTF-8 with owner-only mode.

        An empty signal must clear the previous run's sentinel rather than leave it behind, and nothing in the value may
        ever execute.
        """
        overrides = {"axes": {"1": {"signal": signal}}}
        expected = {key: (value + "\n").encode() for key, value in extract_vars(_scores(overrides)).items()}
        _sentinel(state_dir, "AXIS1_SIGNAL").write_text("stale\n", encoding="utf-8")
        rc = main(["--keep-state", "vars", str(scores_file(overrides))])
        written = {key: _sentinel(state_dir, key).read_bytes() for key in expected}
        assert (rc, capsys.readouterr().out, written) == (0, "", expected)
        assert _owner_only([_sentinel(state_dir, key) for key in expected])
        assert not (state_dir / "pwned").exists()

    def test_folds_multiline_signal_to_one_line(self, state_dir: Path, scores_file: Callable[..., Path]) -> None:
        """A multi-line signal is stored on one line, because the documented ``IFS= read -r`` keeps only line one.

        The agent writes axis signals as free prose; the removed ``eval`` path kept embedded newlines, but a one-line
        sentinel would silently truncate the report text at the first break.
        """
        signal = "first line\nsecond line\r\nthird"
        rc = main(["--keep-state", "vars", str(scores_file({"axes": {"1": {"signal": signal}}}))])
        stored = _sentinel(state_dir, "AXIS1_SIGNAL").read_text(encoding="utf-8")
        assert (rc, stored) == (0, "first line second line third\n")

    @pytest.mark.integration
    @pytest.mark.skipif(shutil.which("bash") is None, reason="bash required for the documented sentinel reader")
    @pytest.mark.parametrize(
        "value", [pytest.param("$(touch pwned); \\ literal", id="metacharacters"), pytest.param("", id="empty")]
    )
    def test_bash_read_never_evaluates_value(
        self, state_dir: Path, scores_file: Callable[..., Path], value: str
    ) -> None:
        """The documented read command returns the stored text verbatim and never evaluates shell syntax in it.

        Real bash is the contract here: the sentinel is only safe if the exact ``IFS= read -r`` idiom the skill ships
        treats the score field as data, including when the stored value is empty and the shell variable starts stale.
        """
        rc = main(["--keep-state", "vars", str(scores_file({"confidence_history": value}))])
        result = subprocess.run(
            [
                shutil.which("bash"),
                "-c",
                'VAR=stale; IFS= read -r VAR < "$1" 2>/dev/null || VAR=""; printf "%s" "$VAR"',
                "reader",
                str(_sentinel(state_dir, "CONFIDENCE_HISTORY")),
            ],
            cwd=state_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        assert (rc, result.returncode, result.stdout) == (0, 0, value)
        assert not (state_dir / "pwned").exists()
