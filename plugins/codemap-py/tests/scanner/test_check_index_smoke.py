"""Tests for ``bin/check_index_smoke.py`` — wrapper around smoke_test_index.py.

The wrapper script invokes ``bin/smoke_test_index.py`` as a subprocess, projects the result down to
``{"ok":bool,"stale":bool,"age_hours":N}`` (plus an ``error`` field on failure), and derives the exit code:

0 — ok+fresh 1 — invalid / stale / empty smoke output 2 — invalid arguments

Strategy — exercise ``main()`` and the pure projection helpers directly, mocking ``subprocess.run`` so tests don't
depend on a real Python interpreter on PATH and don't write index fixtures to disk.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from unittest import mock

import check_index_smoke
import pytest
from check_index_smoke import SmokeResult, derive_exit_code, main, project_smoke_result

_INDEX_ARG = "idx.json"
_OK_RAW = json.dumps({"ok": True, "stale": False, "age_hours": 1.5, "path": "/x"})


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    """Build a ``CompletedProcess`` stand-in for mocked ``subprocess.run``."""
    return subprocess.CompletedProcess(
        args=["python", "smoke_test_index.py"], returncode=returncode, stdout=stdout, stderr=""
    )


@pytest.fixture(name="smoke_script_exists")
def _smoke_script_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub ``Path.is_file`` to ``True`` so the secondary file-existence guard does not fire before the mocked spawn."""
    monkeypatch.setattr(check_index_smoke.Path, "is_file", lambda self: True)


@pytest.fixture(name="fake_smoke")
def _fake_smoke(monkeypatch: pytest.MonkeyPatch, smoke_script_exists: None) -> Callable[[str], None]:
    """Return a setter declaring the raw stdout the upstream ``smoke_test_index.py`` appears to emit.

    ``subprocess.run`` inside ``check_index_smoke`` is replaced by an autospecced fake returning that output, so no
    interpreter is spawned.
    """

    def _set(stdout: str) -> None:
        fake_run = mock.create_autospec(subprocess.run, return_value=_completed(stdout))
        monkeypatch.setattr(check_index_smoke.subprocess, "run", fake_run)

    return _set


# ---------------------------------------------------------------------------
# Pure helper coverage — project_smoke_result + derive_exit_code
# ---------------------------------------------------------------------------


class TestProjectSmokeResult:
    """Projection drops upstream-only keys, preserves error on failure paths."""

    def test_success_path_strips_extra_keys(self) -> None:
        """Ok+fresh result must surface only ok/stale/age_hours."""
        out = project_smoke_result(_OK_RAW)

        assert out == SmokeResult(ok=True, stale=False, age_hours=1.5)

    def test_success_path_omits_error_key_from_payload(self) -> None:
        """The emitted-JSON projection must not carry an ``error`` key on success.

        Shell callers branch on key presence, so a always-present ``error: null`` would read as a failure to any caller
        testing for the key rather than its value.
        """
        out = project_smoke_result(_OK_RAW)

        assert out.to_dict() == {"ok": True, "stale": False, "age_hours": 1.5}

    def test_error_field_is_preserved(self) -> None:
        """Failed result must keep the upstream ``error`` message intact."""
        raw = json.dumps({"ok": False, "stale": False, "age_hours": None, "error": "index file not found"})

        out = project_smoke_result(raw)

        assert out == SmokeResult(ok=False, stale=False, age_hours=None, error="index file not found")

    @pytest.mark.parametrize("raw", ["", "   \n"])
    def test_empty_input_returns_no_output_error(self, raw: str) -> None:
        """Empty / whitespace-only stdin must yield the canonical no-output error."""
        out = project_smoke_result(raw)

        assert (out.ok, out.stale, out.age_hours) == (False, False, None)
        assert out.error == "smoke_test_index.py produced no output"

    @pytest.mark.parametrize(
        ("raw", "fragment"),
        [
            pytest.param("not-json{", "invalid JSON", id="malformed-json"),
            pytest.param(json.dumps([1, 2, 3]), "non-object", id="non-object-json"),
        ],
    )
    def test_unusable_input_returns_error_payload(self, raw: str, fragment: str) -> None:
        """Malformed JSON and a non-object top level are both rejected with a descriptive error.

        The wrapper treats the child's output as untrusted: anything that is not a JSON object becomes ``ok=false`` with
        a message naming what was wrong, instead of an exception.
        """
        out = project_smoke_result(raw)

        assert out.ok is False
        assert fragment in out.error

    def test_absent_upstream_keys_stay_none(self) -> None:
        """Upstream omitting ``ok``/``stale`` must record ``None``, not ``False``.

        The tri-state distinguishes "upstream said false" from "upstream said nothing"; collapsing it would let a silent
        upstream regression read as a definite verdict.
        """
        out = project_smoke_result(json.dumps({"age_hours": 3.0}))

        assert (out.ok, out.stale, out.age_hours) == (None, None, 3.0)


class TestDeriveExitCode:
    """Exit code derivation: 0 only when ok=true AND stale=false."""

    @pytest.mark.parametrize(
        ("projected", "expected"),
        [
            pytest.param(SmokeResult(ok=True, stale=False, age_hours=0.1), 0, id="ok_fresh"),
            pytest.param(SmokeResult(ok=True, stale=True, age_hours=999.0), 1, id="ok_stale"),
            pytest.param(SmokeResult(ok=False, stale=False, age_hours=None, error="x"), 1, id="failed"),
            pytest.param(SmokeResult(ok=False, stale=True, age_hours=None, error="x"), 1, id="failed_stale"),
            pytest.param(SmokeResult(ok=None, stale=None, age_hours=None), 1, id="undetermined"),
        ],
    )
    def test_exit_code_matches_contract(self, projected: SmokeResult, expected: int) -> None:
        """Truth table from the legacy bash script must be preserved.

        Only a definite ok-and-fresh verdict is success; failure, staleness and an undetermined result all exit 1.
        """
        assert derive_exit_code(projected) == expected


# ---------------------------------------------------------------------------
# main() — argv + stdout + exit code surface
# ---------------------------------------------------------------------------


class TestMainArgumentValidation:
    """Argument-parsing branch — missing/unknown flags exit 2."""

    @pytest.mark.parametrize(
        ("argv", "fragments"),
        [
            pytest.param([], ("--index-path",), id="missing-index-path"),
            pytest.param(
                [f"--index-path={_INDEX_ARG}", "--bogus", "1"], ("unrecognized arguments", "--bogus"), id="unknown"
            ),
        ],
    )
    def test_invalid_arguments_exit_two(
        self, argv: list[str], fragments: tuple[str, ...], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A missing required flag or an unknown flag makes argparse exit 2 with a diagnostic on stderr.

        Exit 2 is the wrapper's documented "invalid arguments" code, distinct from the exit-1 stale/invalid verdicts.
        """
        with pytest.raises(SystemExit) as excinfo:
            main(argv)

        err = capsys.readouterr().err
        assert excinfo.value.code == 2
        assert any(fragment in err for fragment in fragments)


class TestMainProjection:
    """``main()`` prints the projected JSON and returns the derived exit code for each upstream outcome."""

    @pytest.mark.parametrize(
        ("upstream", "extra_args", "expected_rc", "expected_payload"),
        [
            pytest.param(
                json.dumps({"ok": True, "stale": False, "age_hours": 2.31, "path": "/x"}),
                [],
                0,
                {"ok": True, "stale": False, "age_hours": 2.31},
                id="ok-fresh",
            ),
            pytest.param(
                json.dumps({"ok": True, "stale": True, "age_hours": 48.0}),
                ["--max-age-hours", "1"],
                1,
                {"ok": True, "stale": True, "age_hours": 48.0},
                id="stale",
            ),
            pytest.param(
                json.dumps({"ok": False, "stale": False, "age_hours": None, "error": "index file not found"}),
                [],
                1,
                {"ok": False, "stale": False, "age_hours": None, "error": "index file not found"},
                id="upstream-failure",
            ),
            pytest.param(
                "",
                [],
                1,
                {"ok": False, "stale": False, "age_hours": None, "error": "smoke_test_index.py produced no output"},
                id="empty-output",
            ),
        ],
    )
    def test_projects_upstream_output(
        self,
        fake_smoke: Callable[[str], None],
        capsys: pytest.CaptureFixture[str],
        upstream: str,
        extra_args: list[str],
        expected_rc: int,
        expected_payload: dict,
    ) -> None:
        """Healthy, stale, failed and empty upstream results each print the projected JSON and the matching exit code.

        A fresh result prints only ok/stale/age_hours (no ``error``, no upstream ``path``); a stale one exits 1 without
        an ``error``; an upstream failure keeps its message verbatim; empty output yields the canonical no-output error.
        """
        fake_smoke(upstream)

        rc = main(["--index-path", _INDEX_ARG, *extra_args])

        assert rc == expected_rc
        assert json.loads(capsys.readouterr().out) == expected_payload


class TestRunSmokeOSError:
    """Convert subprocess launch errors into structured output."""

    @pytest.mark.usefixtures("smoke_script_exists")
    @mock.patch("check_index_smoke.subprocess.run", autospec=True, side_effect=OSError("exec failed"))
    def test_oserror_yields_invocation_error(self, mock_run: mock.Mock, capsys: pytest.CaptureFixture[str]) -> None:
        """OS-level failure to spawn the upstream script must be reported, not raised.

        The wrapper must stay a well-behaved CLI when the interpreter cannot be launched: exit 1 and a JSON payload
        saying it failed to invoke the script.
        """
        rc = main(["--index-path", _INDEX_ARG])

        payload = json.loads(capsys.readouterr().out)
        assert rc == 1
        assert payload["ok"] is False
        assert "failed to invoke" in payload["error"]


class TestSmokeScriptIsAnchoredOnThisFile:
    """The smoke script is chosen by this file's location, never by the environment or working directory."""

    def test_foreign_plugin_root_is_refused(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """An absolute ``CLAUDE_PLUGIN_ROOT`` holding its own smoke script must not be executed.

        The old containment check compared the script against a root built from the same variable, so any directory
        passed its own test.
        """
        planted = tmp_path / "bin"
        planted.mkdir()
        (planted / "smoke_test_index.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))

        with pytest.raises(ValueError, match="own plugin root"):
            check_index_smoke._resolve_smoke_script()

    def test_own_root_resolves_to_the_sibling_script(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With the variable unset or naming this tree, the script beside this file is returned."""
        own_root = check_index_smoke._own_plugin_root()
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        unset = check_index_smoke._resolve_smoke_script()
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(own_root))

        named = check_index_smoke._resolve_smoke_script()

        assert named == unset == own_root / "bin" / "smoke_test_index.py"

    def test_cwd_relative_default_is_not_used(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A ``plugins/<name>/bin/smoke_test_index.py`` planted in the working directory is ignored."""
        planted = tmp_path / "plugins" / "codemap" / "bin"
        planted.mkdir(parents=True)
        (planted / "smoke_test_index.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)

        resolved = check_index_smoke._resolve_smoke_script()

        assert resolved == check_index_smoke._own_plugin_root() / "bin" / "smoke_test_index.py"


class TestAgeHoursIsCoercedToANumber:
    """Child output is untrusted, so ``age_hours`` leaves the wrapper as a number or ``None``."""

    @pytest.mark.parametrize(
        ("age", "expected"),
        [
            pytest.param("1.5", None, id="string"),
            pytest.param(True, None, id="bool"),
            pytest.param([1], None, id="list"),
            pytest.param(3, 3.0, id="int"),
            pytest.param(2.5, 2.5, id="float"),
        ],
    )
    def test_age_hours_is_number_or_none(self, age: object, expected: float | None) -> None:
        """Strings, booleans and containers are dropped; ints and floats pass through as floats.

        A boolean is an ``int`` subclass in Python, so ``True`` is the case that would slip through a naive numeric
        check and surface as ``1.0`` hours.
        """
        raw = json.dumps({"ok": True, "stale": False, "age_hours": age})

        assert project_smoke_result(raw).age_hours == expected
