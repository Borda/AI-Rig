"""Tests for ``bin/parse_audit_json.py`` — the pip-audit JSON summariser.

``summarize*`` and the ``pyproject.toml`` readers are pure; ``main`` is driven through a replaced ``sys.stdin``.
"""

from __future__ import annotations

import doctest
import json
import sys
from collections.abc import Callable
from pathlib import Path

import parse_audit_json
import pytest
from parse_audit_json import (
    main,
    shipped_extra_groups,
    shipped_requirements,
    summarize,
    summarize_scoped,
)


class _BrokenStdin:
    """Stdin stand-in whose read raises an operating-system error."""

    def read(self, *_args: object) -> str:
        """Raise the injected stdin failure when the parser reads input."""
        raise OSError("broken pipe")


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


class TestShippedExtraGroups:
    """shipped_extra_groups: the optional-dependency groups a release exports for its CVE scan."""

    @pytest.mark.parametrize(
        ("pyproject", "expected"),
        [
            pytest.param('[project]\nname = "x"\n', [], id="no-extras"),
            pytest.param(
                '[project]\nname = "x"\n[project.optional-dependencies]\nzeta = ["a"]\nalpha = ["b"]\ndev = ["c"]\n'
                'Docs = ["d"]\n',
                ["alpha", "zeta"],
                id="dev-groups-dropped-and-sorted",
            ),
            pytest.param('[tool.x]\nname = "x"\n', None, id="no-project-table"),
            pytest.param("", None, id="empty-file"),
        ],
    )
    def test_lists_non_dev_groups_or_none_without_project(
        self, tmp_path: Path, pyproject: str, expected: list[str] | None
    ) -> None:
        """Non-dev extras come back sorted as spelled; a file without ``[project]`` means no shipped set.

        A dropped shipped extra would hide its vulnerabilities from the release scan, and a dev group kept in would
        report test tooling as a release finding.
        """
        path = tmp_path / "pyproject.toml"
        path.write_text(pyproject)
        assert shipped_extra_groups(path) == expected


class TestShippedRequirements:
    """shipped_requirements: the declared requirements an unlocked release scan audits."""

    @pytest.mark.parametrize(
        ("pyproject", "expected"),
        [
            pytest.param('[project]\nname = "x"\n', [], id="no-dependencies"),
            pytest.param(
                '[project]\nname = "x"\ndependencies = ["a>=1", "b ; sys_platform == \'win32\'"]\n'
                '[project.optional-dependencies]\nplot = ["c"]\ndev = ["pytest"]\n',
                ["a>=1", "b ; sys_platform == 'win32'", "c"],
                id="core-plus-non-dev-extras",
            ),
            pytest.param(
                '[project]\nname = "x"\ndynamic = ["dependencies"]\n', None, id="dynamic-dependencies-unknowable"
            ),
            pytest.param('[tool.x]\nname = "x"\n', None, id="no-project-table"),
        ],
    )
    def test_lists_shipped_requirements_or_none_when_unknowable(
        self, tmp_path: Path, pyproject: str, expected: list[str] | None
    ) -> None:
        """Core and non-dev extra requirements come back in file order; an unknowable set is ``None``.

        Dynamic dependencies are filled in by the build backend, so reading them statically would report an empty set
        and turn a release that ships dependencies into a clean CVE row.
        """
        path = tmp_path / "pyproject.toml"
        path.write_text(pyproject)
        assert shipped_requirements(path) == expected


class TestSkippedDependencies:
    """Pip-audit entries with a ``skip_reason`` were never audited and must be reported, not counted as clean."""

    def test_reports_skipped_dependencies_apart_from_the_scanned_count(self) -> None:
        """A local or URL requirement is listed under ``skipped`` and excluded from ``N deps``.

        Counting it as a clean dependency would let a release with unauditable requirements read as fully scanned.
        """
        payload = {
            "dependencies": [
                {"name": "scanned", "vulns": []},
                {"name": "local-pkg", "skip_reason": "Dependency not found on PyPI"},
            ]
        }
        assert summarize_scoped(payload, set()) == (
            "1 deps, 0 vulns | new: 0 [] | pre-existing: 0 [] | skipped: 1 [local-pkg]"
        )
