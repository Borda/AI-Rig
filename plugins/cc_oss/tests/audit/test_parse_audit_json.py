"""Tests for ``bin/parse_audit_json.py`` — the pip-audit JSON summariser.

``summarize*`` and ``runtime_closure`` are pure; ``main`` is driven through a replaced ``sys.stdin``, and the installed-
distribution lookup is replaced by a small in-memory table so no real package metadata is read.
"""

from __future__ import annotations

import doctest
import json
import sys
from collections.abc import Callable
from importlib import metadata
from pathlib import Path
from types import SimpleNamespace

import parse_audit_json
import pytest
from parse_audit_json import main, runtime_closure, summarize, summarize_scoped, summarize_shipped

_REQUIRES = {
    "app-dep": ["sub-dep>=1", "pytest ; extra == 'test'"],
    "sub-dep": [],
    "plot-dep": [],
}


class _BrokenStdin:
    """Stdin stand-in whose read raises an operating-system error."""

    def read(self, *_args: object) -> str:
        """Raise the injected stdin failure when the parser reads input."""
        raise OSError("broken pipe")


def _fake_distribution(name: str) -> SimpleNamespace:
    """Return a distribution stand-in exposing ``requires``, or raise the real not-installed error."""
    if name not in _REQUIRES:
        raise metadata.PackageNotFoundError(name)
    return SimpleNamespace(requires=_REQUIRES[name])


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
    def test_vuln_count_matches_list_length(self, payload: dict, expected: str) -> None:
        """The vuln count equals the length of the dependency's ``vulns`` list."""
        assert summarize(payload) == expected


class TestMain:
    """Main: command-line entry point reading pip-audit JSON from stdin."""

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            pytest.param(
                {"dependencies": [{"vulns": []}, {"vulns": [{}]}]}, "2 deps, 1 vulns", id="pipe-with-one-vuln"
            ),
            pytest.param({"dependencies": []}, "0 deps, 0 vulns", id="empty-dependency-list"),
        ],
    )
    def test_prints_summary_and_exits_zero(
        self, feed_stdin: Callable[[str], None], capsys: pytest.CaptureFixture[str], payload: dict, expected: str
    ) -> None:
        """Valid pip-audit JSON piped on stdin → exit code 0 and the summary on stdout.

        This is the documented call site (``pip-audit --format=json | parse_audit_json.py``): stdin only, no arguments.
        """
        feed_stdin(json.dumps(payload))
        rc = main()
        assert (rc, capsys.readouterr().out.strip()) == (0, expected)

    def test_invalid_json_exits_one(
        self, feed_stdin: Callable[[str], None], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Malformed JSON → exit code 1 and error on stderr."""
        feed_stdin("not json {{")
        rc = main()
        assert (rc, "invalid JSON" in capsys.readouterr().err) == (1, True)

    def test_stdin_read_error_exits_one(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """OSError on stdin read → exit code 1 and error on stderr."""
        monkeypatch.setattr(sys, "stdin", _BrokenStdin())
        rc = main()
        assert (rc, "stdin read error" in capsys.readouterr().err) == (1, True)

    def test_ignores_unknown_argv(self, feed_stdin: Callable[[str], None]) -> None:
        """Arguments retained for signature compatibility are accepted and ignored."""
        feed_stdin(json.dumps({"dependencies": []}))
        assert main(["--ignored"]) == 0


def test_module_doctests_pass() -> None:
    """Doctest examples in parse_audit_json.py must not regress.

    The module documents its summary format through doctests, which pytest does not collect from ``bin/`` on its own.
    """
    assert doctest.testmod(parse_audit_json, verbose=False).failed == 0


class TestScopedSummary:
    """summarize_scoped / ``--introduced``: vulnerable packages touched in the release range are new, the rest old."""

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
    def test_splits_by_introduced_names(self, introduced: set[str], expected: str) -> None:
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

    def test_cli_introduced_flag(self, feed_stdin: Callable[[str], None], capsys: pytest.CaptureFixture[str]) -> None:
        """``--introduced a,b`` switches the CLI to the scoped line; an empty value marks everything pre-existing."""
        feed_stdin(json.dumps({"dependencies": [{"name": "a", "vulns": [{}]}]}))
        rc = main(["--introduced", ""])
        assert (rc, capsys.readouterr().out.strip()) == (0, "1 deps, 1 vulns | new: 0 [] | pre-existing: 1 [a]")


class TestRuntimeClosure:
    """runtime_closure: core and non-dev extras plus transitive runtime requirements, never dev groups."""

    @pytest.fixture(autouse=True)
    def _installed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Route metadata lookups to the in-memory installed set."""
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
    def test_follows_runtime_requirements_only(self, tmp_path: Path, pyproject: str, expected: set[str] | None) -> None:
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
    """summarize_shipped: vulnerable packages outside the shipped set are reported as not shipped, never as findings."""

    def test_excludes_dev_only_vulns(self) -> None:
        """A vulnerable docs tool is listed under ``not shipped`` and stays out of the shipped counts."""
        payload = {"dependencies": [{"name": "app", "vulns": [{}]}, {"name": "mkdocs", "vulns": [{}, {}]}]}
        assert summarize_shipped(payload, {"app"}, {"app"}) == (
            "1 deps, 1 vulns | new: 1 [app] | pre-existing: 0 [] | not shipped: 2 [mkdocs]"
        )

    def test_unknown_scope_scans_everything(self) -> None:
        """Without a shipped set every vulnerable package is counted and the line says the scope is unknown."""
        payload = {"dependencies": [{"name": "a", "vulns": [{}]}]}
        assert summarize_shipped(payload, None, set()).endswith("| scope: unknown")
