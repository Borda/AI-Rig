"""Tests for ``dev_scope_findings.py``: refactor scope findings fail closed and every special case maps to a test."""

from __future__ import annotations

import json
import os
from pathlib import Path

import dev_scope_findings as sf
import pytest


def _run(tmp_path: Path, command: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    """Run the CLI against ``tmp_path`` as run directory and return exit code and stdout."""
    code = sf.main([command, str(tmp_path)])
    return code, capsys.readouterr().out


def _write_findings(tmp_path: Path, text: str) -> None:
    """Write ``scope-findings.md`` into the run directory."""
    (tmp_path / sf.FINDINGS_NAME).write_text(text, encoding="utf-8")


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        pytest.param(None, "missing or unreadable", id="missing-file"),
        pytest.param("", "is empty", id="empty-file"),
        pytest.param("F1 special-case a.py::f retry\n", "malformed finding line", id="malformed-line"),
        pytest.param("F1 · smell · a.py · x\nF1 · smell · b.py · y\n", "duplicate label F1", id="duplicate-label"),
        pytest.param("F1 · special-case · a.py:12 · retry\n", "path::symbol anchor", id="special-case-line-anchor"),
    ],
)
def test_check_fails_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], content: str | None, reason: str
) -> None:
    """A missing, empty or malformed findings file blocks instead of reading as "no special cases"."""
    if content is not None:
        _write_findings(tmp_path, content)
    code, out = _run(tmp_path, "check", capsys)
    assert code == 1
    assert "! BLOCKED" in out
    assert reason in out


def test_check_accepts_explicit_zero_findings(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The explicit no-findings line passes with zero special cases."""
    _write_findings(tmp_path, f"{sf.NONE_LINE}\n")
    code, out = _run(tmp_path, "check", capsys)
    assert code == 0
    assert "SPECIAL_CASES=0" in out


def test_check_lists_special_cases(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Special-case labels are printed in numeric order with the absolute findings path."""
    _write_findings(
        tmp_path,
        "F1 · coupling · a.py::f · copies b\nF10 · special-case · a.py::g · retry\nF2 · special-case · a.py::h · one value\n",
    )
    code, out = _run(tmp_path, "check", capsys)
    assert code == 0
    assert f"SCOPE_FINDINGS={(tmp_path / sf.FINDINGS_NAME).resolve()}" in out
    assert "SPECIAL_CASES=2 F2 F10" in out


def test_reset_does_not_require_findings(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``reset`` prints the target before Step 1 writes it."""
    code, out = _run(tmp_path, "reset", capsys)
    assert code == 0
    assert out.startswith("SCOPE_FINDINGS=")


def test_blank_run_dir_blocks(capsys: pytest.CaptureFixture[str]) -> None:
    """An empty run-directory argument blocks rather than resolving to the working directory."""
    assert sf.main(["check", ""]) == 1
    assert "run directory missing" in capsys.readouterr().out


def _junit(outcomes: dict[str, str]) -> str:
    """Render ``{node id: outcome}`` as the xunit1 JUnit XML pytest writes (``file`` plus dotted ``classname``)."""
    children = {"SKIPPED": '<skipped type="pytest.skip"/>', "XFAIL": '<skipped type="pytest.xfail"/>'}
    children |= {"FAILED": '<failure message="boom"/>', "XPASS": '<failure message="[XPASS(strict)] "/>'}
    children |= {"ERROR": '<error message="failed on teardown"/>'}
    cases = []
    for node, outcome in outcomes.items():
        file_part, _, rest = node.partition("::")
        *classes, name = rest.split("::")
        classname = ".".join([file_part.removesuffix(".py").replace("/", "."), *classes])
        body = children.get(outcome, "")
        cases.append(f'<testcase file="{file_part}" classname="{classname}" name="{name}">{body}</testcase>')
    return f"<testsuites><testsuite>{''.join(cases)}</testsuite></testsuites>"


def _fake_runner(
    monkeypatch: pytest.MonkeyPatch, outcomes: dict[str, str], code: int = 0, output: str = ""
) -> list[list[str]]:
    """Replace the runner: write ``outcomes`` as the JUnit report and print ``output``; return the node lists run."""
    calls: list[list[str]] = []

    def fake(nodes: list[str], extra: list[str]) -> tuple[int, str]:
        calls.append(nodes)
        report = next(Path(arg.split("=", 1)[1]) for arg in extra if arg.startswith("--junitxml="))
        if code in (0, 1):
            report.write_text(_junit(outcomes), encoding="utf-8")
        return code, output

    monkeypatch.setattr(sf, "_run", fake)
    return calls


class TestMap:
    """Special-case test map validation, reported by every command that reads the map."""

    @pytest.fixture
    def run_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Return a run directory with two special cases and a characterization test file in the cwd."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_char.py").write_text("def test_a():\n    pass\n", encoding="utf-8")
        run = tmp_path / "run"
        run.mkdir()
        _write_findings(
            run, "F1 · special-case · a.py::f · retry\nF2 · smell · a.py::g · long\nF3 · special-case · a.py::h · x\n"
        )
        return run

    def _map(self, run: Path, text: str) -> None:
        """Write the special-case test map."""
        (run / sf.MAP_NAME).write_text(text, encoding="utf-8")

    def test_unmapped_special_case_fails_the_pin(
        self, run_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A special case without a mapped test fails the gate and is named."""
        self._map(run_dir, "F1 · tests/test_char.py::test_a\n")
        _fake_runner(monkeypatch, {"tests/test_char.py::test_a": "PASSED"})
        code, out = _run(run_dir, "pin", capsys)
        assert code == sf.EXIT_PIN_FAILED
        assert "F3 (unmapped)" in out

    def test_missing_map_fails_the_pin_for_every_special_case(
        self, run_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """No map file at all fails while special cases exist, without running pytest."""
        code, out = _run(run_dir, "pin", capsys)
        assert code == sf.EXIT_PIN_FAILED
        assert "F1 (unmapped); F3 (unmapped)" in out

    @pytest.mark.parametrize(
        ("row", "cause"),
        [
            pytest.param("F2 · tests/test_char.py::test_a", "F2 is not a special case", id="stale-label"),
            pytest.param("F1 · -k::x", "unsafe node id", id="option-shaped"),
            pytest.param("F1 · ../tests/test_char.py::test_a", "unsafe node id", id="parent-reference"),
            pytest.param("F1 · tests/absent.py::test_a", "mapped test file missing for F1", id="missing-file"),
            pytest.param("F1 tests/test_char.py::test_a", "malformed map row", id="malformed-row"),
        ],
    )
    @pytest.mark.parametrize("command", ["pin", "verify", "accept-unpinned"])
    def test_invalid_map_row_blocks(
        self, run_dir: Path, capsys: pytest.CaptureFixture[str], row: str, cause: str, command: str
    ) -> None:
        """Every reader blocks on an invalid row and names the real cause; nothing is accepted."""
        self._map(run_dir, f"{row}\nF3 · tests/test_char.py::test_a\n")
        code, out = _run(run_dir, command, capsys)
        assert code == sf.EXIT_BLOCKED
        assert cause in out
        assert not (run_dir / sf.ACCEPTED_NAME).exists()

    def test_complete_map_pins_each_unique_node_once(
        self, run_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A complete map runs each unique node once and pins every special case."""
        self._map(run_dir, "F1 · tests/test_char.py::test_a\nF3 · tests/test_char.py::test_a\n")
        calls = _fake_runner(monkeypatch, {"tests/test_char.py::test_a": "PASSED"})
        code, out = _run(run_dir, "pin", capsys)
        assert code == 0
        assert "SPECIAL-CASE PIN OK: 2 special case(s) pinned" in out
        assert calls == [["tests/test_char.py::test_a"]]

    def test_accepted_label_is_excused(
        self, run_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A label the user accepted as unpinned is left out of the pin and reported."""
        self._map(run_dir, "F1 · tests/test_char.py::test_a\n")
        record = {"unpinned": {"F3": []}, **sf._hashes(run_dir)}
        (run_dir / sf.ACCEPTED_NAME).write_text(json.dumps(record), encoding="utf-8")
        _fake_runner(monkeypatch, {"tests/test_char.py::test_a": "PASSED"})
        code, out = _run(run_dir, "pin", capsys)
        assert code == 0
        assert "ACCEPTED UNPINNED: F3" in out

    def test_zero_special_cases_run_no_tests(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """No special cases pins and verifies without running pytest, so the whole suite never runs by accident."""
        _write_findings(tmp_path, f"{sf.NONE_LINE}\n")
        calls = _fake_runner(monkeypatch, {})
        assert _run(tmp_path, "pin", capsys)[0] == 0
        assert _run(tmp_path, "verify", capsys)[0] == 0
        assert calls == []


class TestPinEvaluation:
    """Pin verdicts come from per-node xunit1 outcomes and the -rX summary, and route each state to its exit."""

    @pytest.fixture
    def run_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Return a run directory with two special cases, a test file and a map using a ``./`` path."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_char.py").write_text("def test_a():\n    pass\n", encoding="utf-8")
        _write_findings(tmp_path, "F1 · special-case · a.py::f · x\nF2 · special-case · a.py::g · y\n")
        (tmp_path / sf.MAP_NAME).write_text(
            "F1 · ./tests/test_char.py::test_a\nF2 · tests/test_char.py::test_b\n", encoding="utf-8"
        )
        return tmp_path

    def test_dot_slash_node_is_normalized(
        self, run_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A ``./`` map path matches pytest's reported id instead of reading as "not reported"."""
        calls = _fake_runner(
            monkeypatch, {"tests/test_char.py::test_a": "PASSED", "tests/test_char.py::test_b": "PASSED"}
        )
        code, out = _run(run_dir, "pin", capsys)
        assert code == 0, out
        assert calls[0] == ["tests/test_char.py::test_a", "tests/test_char.py::test_b"]

    @pytest.mark.parametrize(
        ("outcome", "shown"),
        [
            pytest.param("SKIPPED", "F2 (tests/test_char.py::test_b SKIPPED)", id="skipped"),
            pytest.param("XFAIL", "F2 (tests/test_char.py::test_b XFAIL)", id="xfailed"),
            pytest.param("XPASS", "F2 (tests/test_char.py::test_b XPASS)", id="xpassed"),
            pytest.param("FAILED", "F2 (tests/test_char.py::test_b FAILED)", id="failed"),
            pytest.param("ERROR", "F2 (tests/test_char.py::test_b ERROR)", id="fixture-error"),
        ],
    )
    def test_non_pass_outcome_fails_pin(
        self,
        run_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        outcome: str,
        shown: str,
    ) -> None:
        """Only a pass pins a special case; every other reported outcome fails the gate and is named."""
        _fake_runner(monkeypatch, {"tests/test_char.py::test_a": "PASSED", "tests/test_char.py::test_b": outcome}, 1)
        code, out = _run(run_dir, "pin", capsys)
        assert code == sf.EXIT_PIN_FAILED
        assert shown in out

    def test_summary_xpass_never_pins(
        self, run_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A non-strict xpass reported as a plain pass is caught from the -rX summary line."""
        outcomes = {"tests/test_char.py::test_a": "PASSED", "tests/test_char.py::test_b": "PASSED"}
        _fake_runner(monkeypatch, outcomes, output="XPASS tests/test_char.py::test_b - runtime marker\n")
        code, out = _run(run_dir, "pin", capsys)
        assert code == sf.EXIT_PIN_FAILED
        assert "F2 (tests/test_char.py::test_b XPASS)" in out

    def test_parametrized_id_with_space_fails_when_one_case_skips(
        self, run_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A skipped parametrized case whose id contains a space still fails the pin."""
        outcomes = {
            "tests/test_char.py::test_a": "PASSED",
            "tests/test_char.py::test_b[a b]": "SKIPPED",
            "tests/test_char.py::test_b[c]": "PASSED",
        }
        _fake_runner(monkeypatch, outcomes)
        code, out = _run(run_dir, "pin", capsys)
        assert code == sf.EXIT_PIN_FAILED
        assert "F2 (tests/test_char.py::test_b SKIPPED)" in out

    def test_every_mapped_node_needs_its_own_outcome(
        self, run_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A passing node never hides a second node of the same label that reported nothing."""
        (run_dir / sf.MAP_NAME).write_text(
            "F1 · tests/test_char.py::test_a\nF2 · tests/test_char.py::test_b\nF2 · tests/test_char.py::test_c\n",
            encoding="utf-8",
        )
        _fake_runner(monkeypatch, {"tests/test_char.py::test_a": "PASSED", "tests/test_char.py::test_b": "PASSED"})
        code, out = _run(run_dir, "pin", capsys)
        assert code == sf.EXIT_PIN_FAILED
        assert "F2 (tests/test_char.py::test_c not reported)" in out

    @pytest.mark.parametrize(
        ("code", "exit_code", "cause"),
        [
            pytest.param(2, sf.EXIT_NO_VERDICT, "pytest interrupted", id="interrupted"),
            pytest.param(3, sf.EXIT_NO_VERDICT, "pytest internal error", id="internal-error"),
            pytest.param(4, sf.EXIT_NO_VERDICT, "pytest usage error", id="usage-error"),
            pytest.param(5, sf.EXIT_NO_VERDICT, "no tests collected", id="nothing-collected"),
            pytest.param(64, 64, "test runner did not run", id="runner-refusal"),
            pytest.param(70, 70, "test runner did not run", id="runner-fault"),
        ],
    )
    def test_no_verdict_names_its_cause(
        self,
        run_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        code: int,
        exit_code: int,
        cause: str,
    ) -> None:
        """A run without per-test verdicts never takes the failing-pin code (3), names why and writes no verdict."""
        _fake_runner(monkeypatch, {}, code=code)
        result, out = _run(run_dir, "pin", capsys)
        assert result == exit_code
        assert cause in out
        assert "GATE ERROR" in out
        assert "GATE FAIL" not in out
        assert not (run_dir / sf.VERDICT_NAME).exists()

    @pytest.mark.parametrize(
        ("report", "cause"),
        [
            pytest.param(None, "no readable JUnit report", id="missing-report"),
            pytest.param("<testsuites><testsuite><testcase", "no readable JUnit report", id="truncated-xml"),
            pytest.param(
                '<testsuites><testcase classname="t" name="a"/></testsuites>', "no file attribute", id="xunit2-report"
            ),
            pytest.param(
                '<testsuites><testcase file="tests/test_char.py" classname="tests.test_char" name="test_a"/>'
                '<testcase file="tests/test_char.py" classname="tests.test_char" name="test_a"/></testsuites>',
                "repeats test id",
                id="duplicate-id",
            ),
        ],
    )
    def test_unusable_report_is_no_verdict(
        self,
        run_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        report: str | None,
        cause: str,
    ) -> None:
        """A run whose report is missing, broken, lacks file attributes or repeats an id has no verdict."""

        def fake(nodes: list[str], extra: list[str]) -> tuple[int, str]:
            if report is not None:
                target = next(Path(arg.split("=", 1)[1]) for arg in extra if arg.startswith("--junitxml="))
                target.write_text(report, encoding="utf-8")
            return 0, ""

        monkeypatch.setattr(sf, "_run", fake)
        code, out = _run(run_dir, "pin", capsys)
        assert code == sf.EXIT_NO_VERDICT
        assert cause in out

    def test_foreign_rootdir_ids_name_the_mismatch(
        self, run_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Report ids relative to another rootdir are a no-verdict mismatch, not failing tests."""
        _fake_runner(monkeypatch, {"test_char.py::test_a": "PASSED", "test_char.py::test_b": "PASSED"})
        code, out = _run(run_dir, "pin", capsys)
        assert code == sf.EXIT_NO_VERDICT
        assert "rootdir differs" in out


class TestVerdictAndAcceptance:
    """The persisted verdict is the only source of an acceptance and of the Step 4 entry decision."""

    @pytest.fixture
    def run_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Return a run directory with a passing, a failing and an unmapped special case, already pinned."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "tests").mkdir()
        for name in ("test_pass.py", "test_red.py"):
            (tmp_path / "tests" / name).write_text("def test_a():\n    pass\n", encoding="utf-8")
        _write_findings(
            tmp_path,
            "F1 · special-case · a.py::f · x\nF2 · special-case · a.py::g · y\nF3 · special-case · a.py::h · z\n",
        )
        (tmp_path / sf.MAP_NAME).write_text(
            "F1 · tests/test_pass.py::test_a\nF2 · tests/test_red.py::test_a\n", encoding="utf-8"
        )
        _fake_runner(monkeypatch, {"tests/test_pass.py::test_a": "PASSED", "tests/test_red.py::test_a": "FAILED"}, 1)
        return tmp_path

    def test_accept_copies_exactly_the_verdict_without_running_tests(
        self, run_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The acceptance holds the verdict's unpinned labels and failing nodes; accepting re-runs nothing."""
        assert _run(run_dir, "pin", capsys)[0] == sf.EXIT_PIN_FAILED
        calls = _fake_runner(monkeypatch, {"tests/test_red.py::test_a": "PASSED"})
        code, out = _run(run_dir, "accept-unpinned", capsys)
        assert code == 0
        assert calls == []
        assert "ACCEPTED UNPINNED: F2 F3" in out
        assert b"\r" not in (run_dir / sf.ACCEPTED_NAME).read_bytes()
        record = json.loads((run_dir / sf.ACCEPTED_NAME).read_text(encoding="utf-8"))
        assert record["unpinned"] == {"F2": ["tests/test_red.py::test_a"], "F3": []}

    @pytest.mark.parametrize("state", ["no-verdict", "stale-verdict"])
    def test_accept_refuses_without_a_current_verdict(
        self, run_dir: Path, capsys: pytest.CaptureFixture[str], state: str
    ) -> None:
        """Without a verdict for the current findings and map nothing is accepted."""
        if state == "stale-verdict":
            assert _run(run_dir, "pin", capsys)[0] == sf.EXIT_PIN_FAILED
            with (run_dir / sf.MAP_NAME).open("a", encoding="utf-8") as handle:
                handle.write("\n")
        code, out = _run(run_dir, "accept-unpinned", capsys)
        assert code == sf.EXIT_NO_VERDICT
        assert "no current Step 3 verdict" in out
        assert not (run_dir / sf.ACCEPTED_NAME).exists()

    @pytest.mark.parametrize(
        ("setup", "exit_code", "line"),
        [
            pytest.param("none", 1, "no current Step 3 verdict", id="no-verdict"),
            pytest.param("stale", 1, "no current Step 3 verdict", id="stale-verdict"),
            pytest.param("unpinned", 1, "F2 F3 not pinned and not accepted", id="unpinned-not-accepted"),
            pytest.param("accepted", 0, "SPECIAL-CASE VERIFY OK", id="unpinned-fully-accepted"),
            pytest.param("stale-acceptance", 1, "STALE ACCEPTANCE", id="acceptance-stale"),
            pytest.param("pin-ok", 0, "SPECIAL-CASE VERIFY OK", id="pin-ok"),
        ],
    )
    def test_verify_admits_only_a_current_settled_verdict(
        self,
        run_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        setup: str,
        exit_code: int,
        line: str,
    ) -> None:
        """Step 4 entry needs a current verdict that is PIN OK or fully covered by a current acceptance."""
        if setup == "pin-ok":
            (run_dir / sf.MAP_NAME).write_text(
                "F1 · tests/test_pass.py::test_a\nF2 · tests/test_pass.py::test_a\nF3 · tests/test_pass.py::test_a\n",
                encoding="utf-8",
            )
            _fake_runner(monkeypatch, {"tests/test_pass.py::test_a": "PASSED"})
        if setup != "none":
            _run(run_dir, "pin", capsys)
        if setup in ("accepted", "stale-acceptance"):
            assert _run(run_dir, "accept-unpinned", capsys)[0] == 0
        if setup in ("stale", "stale-acceptance"):
            with (run_dir / sf.FINDINGS_NAME).open("a", encoding="utf-8") as handle:
                handle.write("\n")
        if setup == "stale-acceptance":
            _run(run_dir, "pin", capsys)
        code, out = _run(run_dir, "verify", capsys)
        assert code == exit_code, out
        assert line in out

    def test_corrupt_acceptance_is_stale(self, run_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """An unreadable acceptance record is ignored as stale, so every label is enforced."""
        _run(run_dir, "pin", capsys)
        (run_dir / sf.ACCEPTED_NAME).write_text("{not json", encoding="utf-8")
        code, out = _run(run_dir, "verify", capsys)
        assert code == 1
        assert "STALE ACCEPTANCE" in out

    def test_reset_removes_map_verdict_and_acceptance(self, run_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Reset leaves no map, verdict or acceptance from an earlier pass."""
        _run(run_dir, "pin", capsys)
        assert _run(run_dir, "accept-unpinned", capsys)[0] == 0
        code, out = _run(run_dir, "reset", capsys)
        assert code == 0
        assert out.startswith("SCOPE_FINDINGS=")
        assert not any((run_dir / name).exists() for name in (sf.MAP_NAME, sf.ACCEPTED_NAME, sf.VERDICT_NAME))


def test_baseline_filter_blocks_when_a_file_cannot_be_enumerated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A file holding an accepted node that cannot be collected blocks instead of dropping its other tests."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_red.py").write_text("def test_a():\n    pass\n", encoding="utf-8")
    _write_findings(tmp_path, "F1 · special-case · a.py::f · x\n")
    (tmp_path / sf.MAP_NAME).write_text("F1 · tests/test_red.py::test_a\n", encoding="utf-8")
    record = {"unpinned": {"F1": ["tests/test_red.py::test_a"]}, **sf._hashes(tmp_path)}
    (tmp_path / sf.ACCEPTED_NAME).write_text(json.dumps(record), encoding="utf-8")
    (tmp_path / "selection.json").write_text(json.dumps({"tests": ["tests/test_red.py"]}), encoding="utf-8")
    monkeypatch.setattr(sf, "_run", lambda nodes, extra: (2, "ERROR collecting tests/test_red.py"))
    code = sf.main(["baseline-filter", str(tmp_path), str(tmp_path / "selection.json"), str(tmp_path / "out.json")])
    out = capsys.readouterr().out
    assert code == 1
    assert "cannot enumerate tests in tests/test_red.py" in out
    assert not (tmp_path / "out.json").exists()


@pytest.mark.parametrize(
    "node",
    [
        pytest.param("@args.txt::x", id="response-file"),
        pytest.param("C:tests/test_a.py::x", id="drive-relative"),
        pytest.param("D:/tests/test_a.py::x", id="drive-absolute"),
    ],
)
def test_safe_node_mirrors_runner_rules(tmp_path: Path, node: str) -> None:
    """Node ids the test runner would refuse never reach it from the map."""
    assert sf._safe_node(node, tmp_path) is False


@pytest.mark.skipif(os.name == "nt", reason="creating symlinks needs elevated rights on native Windows")
def test_safe_node_refuses_symlink_escape(tmp_path: Path) -> None:
    """A test file symlinked to a location outside the project root is refused."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "escape.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "escape.py").symlink_to(outside / "escape.py")
    assert sf._safe_node("tests/escape.py::test_x", root) is False
