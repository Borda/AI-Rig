"""Tests for ``bin/parse_audit_json.py`` — pip-audit JSON summariser."""

from __future__ import annotations

import io
import json
from importlib import metadata
from pathlib import Path
from unittest.mock import patch

import parse_audit_json
import pytest
from parse_audit_json import main, runtime_closure, summarize, summarize_scoped, summarize_shipped

# ---------------------------------------------------------------------------
# summarize() — pure function
# ---------------------------------------------------------------------------


class TestSummarize:
    """Summarize: output format and edge cases."""

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            pytest.param({"dependencies": []}, "0 deps, 0 vulns", id="empty-dependencies"),
            pytest.param(
                {"dependencies": [{"vulns": []}, {"vulns": [{}, {}]}]}, "2 deps, 2 vulns", id="vulns-summed-across-deps"
            ),
            pytest.param({}, "0 deps, 0 vulns", id="missing-dependencies-key"),
            pytest.param({"dependencies": [{}]}, "1 deps, 0 vulns", id="missing-vulns-key-in-dep"),
        ],
    )
    def test_summary_for_payload_shape(self, payload: dict, expected: str) -> None:
        """Zero deps give '0 deps, 0 vulns'; vuln counts are summed across deps; absent keys count as empty.

        A missing top-level ``dependencies`` key is treated as an empty list (graceful fallback), and a dep entry
        without a ``vulns`` key is treated as zero vulns.
        """
        assert summarize(payload) == expected

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            pytest.param({"dependencies": [{"vulns": [{}] * n}]}, f"1 deps, {n} vulns", id=f"{n}-vulns")
            for n in [0, 1, 5, 10]
        ],
    )
    def test_vuln_counts(self, payload: dict, expected: str) -> None:
        """Vuln count matches length of 'vulns' list."""
        assert summarize(payload) == expected


# ---------------------------------------------------------------------------
# main() — CLI entry point with stdin
# ---------------------------------------------------------------------------


class TestMain:
    """Verify command-line output and exit codes for standard input."""

    def test_valid_json_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Valid pip-audit JSON → exit code 0 and summary on stdout."""
        payload = {"dependencies": [{"vulns": []}, {"vulns": [{}]}]}
        fake_stdin = io.StringIO(json.dumps(payload))
        with patch("sys.stdin", fake_stdin):
            rc = main()
        assert rc == 0
        out = capsys.readouterr().out
        assert out.strip() == "2 deps, 1 vulns"

    def test_empty_deps_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Empty dependency list is valid JSON → exit code 0."""
        fake_stdin = io.StringIO(json.dumps({"dependencies": []}))
        with patch("sys.stdin", fake_stdin):
            rc = main()
        assert rc == 0
        assert capsys.readouterr().out.strip() == "0 deps, 0 vulns"

    def test_invalid_json_exits_one(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Malformed JSON → exit code 1 and error on stderr."""
        fake_stdin = io.StringIO("not json {{")
        with patch("sys.stdin", fake_stdin):
            rc = main()
        assert rc == 1
        err = capsys.readouterr().err
        assert "invalid JSON" in err

    def test_os_error_exits_one(self, capsys: pytest.CaptureFixture[str]) -> None:
        """OSError on stdin read → exit code 1 and error on stderr."""

        class _BrokenStdin:
            """Raise an operating-system error when stdin is read."""

            def read(self, *_):
                """Raise the injected stdin failure when the parser reads input."""
                raise OSError("broken pipe")

        with patch("sys.stdin", _BrokenStdin()):
            rc = main()
        assert rc == 1
        err = capsys.readouterr().err
        assert "stdin read error" in err

    def test_argv_override_unused(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Accept but ignore arguments retained for signature compatibility."""
        fake_stdin = io.StringIO(json.dumps({"dependencies": []}))
        with patch("sys.stdin", fake_stdin):
            rc = main(["--ignored"])
        assert rc == 0

    def test_golden_invocation_stdin_pipe(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Documented call site (``pip-audit --format=json | parse_audit_json.py``) — stdin-only."""
        payload = {"dependencies": [{"vulns": []}, {"vulns": [{}]}]}
        with patch("sys.stdin", io.StringIO(json.dumps(payload))):
            rc = main()
        assert rc == 0
        assert capsys.readouterr().out.strip() == "2 deps, 1 vulns"


# ---------------------------------------------------------------------------
# Doctest hookup
# ---------------------------------------------------------------------------


def test_doctests_pass() -> None:
    """Doctest examples in parse_audit_json.py must not regress."""
    import doctest

    import parse_audit_json as _mod

    results = doctest.testmod(_mod, verbose=False)
    assert results.failed == 0, f"{results.failed} doctest(s) failed"


# ---------------------------------------------------------------------------
# summarize_scoped() / --introduced — new versus pre-existing split
# ---------------------------------------------------------------------------


class TestScopedSummary:
    """Scope split: vulnerable packages touched in the release range are new, the rest pre-existing."""

    @pytest.mark.parametrize(
        ("introduced", "expected"),
        [
            pytest.param(set(), "3 deps, 3 vulns | new: 0 [] | pre-existing: 3 [Old_Pkg, fresh]", id="none-introduced"),
            pytest.param({"fresh"}, "3 deps, 3 vulns | new: 1 [fresh] | pre-existing: 2 [Old_Pkg]", id="one-new"),
            pytest.param(
                {"old-pkg", "fresh"}, "3 deps, 3 vulns | new: 3 [Old_Pkg, fresh] | pre-existing: 0 []", id="all-new"
            ),
        ],
    )
    def test_split_by_introduced_names(self, introduced: set[str], expected: str) -> None:
        """Names are PEP 503 normalized, so ``Old_Pkg`` matches ``old-pkg``; clean packages never appear.

        Guards the release audit's red-flag-versus-warning decision: a vulnerable package mislabelled as pre-existing
        would downgrade a regression introduced by the release to a warning.
        """
        payload = {
            "dependencies": [
                {"name": "Old_Pkg", "vulns": [{}, {}]},
                {"name": "fresh", "vulns": [{}]},
                {"name": "clean", "vulns": []},
            ]
        }
        assert summarize_scoped(payload, introduced) == expected

    def test_cli_introduced_flag(self, capsys: pytest.CaptureFixture[str]) -> None:
        """``--introduced a,b`` switches the CLI to the scoped line; an empty value marks everything pre-existing."""
        payload = {"dependencies": [{"name": "a", "vulns": [{}]}]}
        with patch("sys.stdin", io.StringIO(json.dumps(payload))):
            rc = main(["--introduced", ""])
        assert rc == 0
        assert capsys.readouterr().out.strip() == "1 deps, 1 vulns | new: 0 [] | pre-existing: 1 [a]"


# ---------------------------------------------------------------------------
# runtime_closure() / summarize_shipped() — only what the built package ships
# ---------------------------------------------------------------------------

_REQUIRES = {
    "app-dep": ["sub-dep>=1", "pytest ; extra == 'test'"],
    "sub-dep": [],
    "plot-dep": [],
}


class _FakeDist:
    """Installed-distribution stand-in exposing only ``requires``."""

    def __init__(self, requires: list[str]) -> None:
        self.requires = requires


def _fake_distribution(name: str) -> _FakeDist:
    """Return the fake distribution or raise the real not-installed error."""
    if name not in _REQUIRES:
        raise metadata.PackageNotFoundError(name)
    return _FakeDist(_REQUIRES[name])


class TestRuntimeClosure:
    """Shipped set: core and non-dev extras plus transitive runtime requirements, never dev groups."""

    @pytest.fixture(autouse=True)
    def _installed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Route metadata lookups to the fake installed set."""
        monkeypatch.setattr(parse_audit_json.metadata, "distribution", _fake_distribution)

    @pytest.mark.parametrize(
        ("pyproject", "expected"),
        [
            pytest.param(
                '[project]\nname = "x"\ndependencies = ["App_Dep>=2"]\n',
                {"app-dep", "sub-dep"},
                id="transitive-followed-extra-requirement-skipped",
            ),
            pytest.param(
                '[project]\nname = "x"\n[dependency-groups]\ntest = ["pytest"]\n', set(), id="dependency-groups-ignored"
            ),
            pytest.param(
                '[project]\nname = "x"\n[project.optional-dependencies]\ndocs = ["pytest"]\nplot = ["plot-dep"]\n',
                {"plot-dep"},
                id="dev-named-extra-dropped-other-extra-kept",
            ),
            pytest.param(
                '[project]\nname = "x"\ndependencies = ["not-installed"]\n', None, id="uninstalled-root-unknown"
            ),
            pytest.param(
                '[project]\nname = "x"\ndependencies = ["not-installed ; sys_platform == \'nope\'"]\n',
                set(),
                id="uninstalled-marker-root-skipped",
            ),
            pytest.param(
                '[project]\nname = "x"\ndynamic = ["dependencies"]\n', None, id="dynamic-dependencies-unknown"
            ),
        ],
    )
    def test_closure(self, tmp_path: Path, pyproject: str, expected: set[str] | None) -> None:
        """Closure follows runtime requirements only, and refuses to guess when the set is incomplete.

        An incomplete closure would silently drop vulnerable shipped packages, so unknown scope returns ``None`` and the
        audit falls back to scanning everything.
        """
        path = tmp_path / "pyproject.toml"
        path.write_text(pyproject)
        assert runtime_closure(path) == expected

    def test_missing_file_is_unknown(self, tmp_path: Path) -> None:
        """No pyproject.toml means the shipped set is unknowable."""
        assert runtime_closure(tmp_path / "pyproject.toml") is None


class TestShippedSummary:
    """Vulnerable packages outside the shipped set are reported as not shipped, never as findings."""

    def test_dev_only_vulns_excluded(self) -> None:
        """A vulnerable docs tool is listed under ``not shipped`` and stays out of the shipped counts."""
        payload = {"dependencies": [{"name": "app", "vulns": [{}]}, {"name": "mkdocs", "vulns": [{}, {}]}]}
        assert summarize_shipped(payload, {"app"}, {"app"}) == (
            "1 deps, 1 vulns | new: 1 [app] | pre-existing: 0 [] | not shipped: 2 [mkdocs]"
        )

    def test_unknown_scope_scans_everything(self) -> None:
        """Without a shipped set every vulnerable package is counted and the line says the scope is unknown."""
        payload = {"dependencies": [{"name": "a", "vulns": [{}]}]}
        assert summarize_shipped(payload, None, set()).endswith("| scope: unknown")
