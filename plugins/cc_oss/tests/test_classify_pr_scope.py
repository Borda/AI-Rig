"""Tests for ``bin/classify_pr_scope.py``.

Pure deterministic classifier — no subprocess, no I/O. Covers the five classification branches plus the refactor-signal
override and CLI plumbing.
"""

from __future__ import annotations

import pytest

import classify_pr_scope as cps  # type: ignore[import-not-found]


class TestClassify:
    """Direct ``classify()`` calls — covers all five scope branches."""

    @pytest.mark.parametrize(
        ("py_files", "loc_delta", "new_api_lines", "labels", "title", "expected"),
        [
            pytest.param(0, 0, 0, "", "", cps.PRScope.CHORE, id="zero-py-files"),
            pytest.param(0, 50, 0, "deps", "Bump numpy", cps.PRScope.CHORE, id="deps-only-no-py"),
            pytest.param(1, 10, 3, "", "", cps.PRScope.FEATURE, id="new-api-single-line"),
            pytest.param(5, 200, 12, "", "Add new exports", cps.PRScope.FEATURE, id="new-api-large-pr"),
            pytest.param(1, 10, 0, "", "fix typo", cps.PRScope.FIX, id="fix-tiny-diff"),
            pytest.param(2, 49, 0, "bug", "fix off-by-one", cps.PRScope.FIX, id="fix-just-under-thresholds"),
            pytest.param(3, 30, 0, "", "Restructure modules", cps.PRScope.REFACTOR, id="refactor-3-files"),
            pytest.param(10, 400, 0, "", "Refactor internals", cps.PRScope.REFACTOR, id="refactor-many-files"),
            pytest.param(1, 80, 0, "", "Heavy single-file change", cps.PRScope.MIXED, id="mixed-1-file-large"),
            pytest.param(2, 100, 0, "", "Large 2-file edit", cps.PRScope.MIXED, id="mixed-2-file-large"),
        ],
    )
    def test_branches(
        self,
        py_files: int,
        loc_delta: int,
        new_api_lines: int,
        labels: str,
        title: str,
        expected: cps.PRScope,
    ) -> None:
        """Each parametrized case exercises one classification branch."""
        assert (
            cps.classify(
                py_files=py_files,
                loc_delta=loc_delta,
                new_api_lines=new_api_lines,
                labels=labels,
                title=title,
            )
            == expected
        )


class TestRefactorOverride:
    """FIX → REFACTOR upgrade when labels/title carry a perf/refactor signal."""

    @pytest.mark.parametrize(
        ("labels", "title"),
        [
            pytest.param("perf", "small change", id="label-perf"),
            pytest.param("performance", "tiny edit", id="label-performance"),
            pytest.param("optimization", "speed up", id="label-optimization"),
            pytest.param("refactor", "small change", id="label-refactor"),
            pytest.param("architecture", "small change", id="label-architecture"),
            pytest.param("cleanup", "small change", id="label-cleanup"),
            pytest.param("", "Refactor parser into helper", id="title-refactor"),
            pytest.param("", "perf: faster loop", id="title-perf-prefix"),
            pytest.param("", "Rewrite hot path", id="title-rewrite"),
        ],
    )
    def test_signal_promotes_fix_to_refactor(self, labels: str, title: str) -> None:
        """Tiny diff (would be FIX) + signal token → REFACTOR."""
        assert (
            cps.classify(py_files=2, loc_delta=30, new_api_lines=0, labels=labels, title=title) == cps.PRScope.REFACTOR
        )

    def test_no_signal_stays_fix(self) -> None:
        """Tiny diff without signal stays FIX."""
        assert (
            cps.classify(py_files=2, loc_delta=30, new_api_lines=0, labels="bug", title="fix typo in docstring")
            == cps.PRScope.FIX
        )

    def test_signal_case_insensitive(self) -> None:
        """Signal matching is case-insensitive."""
        assert (
            cps.classify(py_files=2, loc_delta=30, new_api_lines=0, labels="PERF", title="Speed UP")
            == cps.PRScope.REFACTOR
        )

    def test_signal_ignored_for_feature(self) -> None:
        """Refactor signal does not override FEATURE — new API takes precedence."""
        assert (
            cps.classify(py_files=2, loc_delta=30, new_api_lines=5, labels="refactor", title="Add new export")
            == cps.PRScope.FEATURE
        )


class TestHasRefactorSignal:
    """Direct coverage of the helper — boundary tokens, empties."""

    def test_empty_inputs(self) -> None:
        """Empty labels and title → False."""
        assert cps._has_refactor_signal("", "") is False

    def test_signal_in_labels_only(self) -> None:
        """Signal token in labels alone is enough."""
        assert cps._has_refactor_signal("cleanup", "") is True

    def test_signal_in_title_only(self) -> None:
        """Signal token in title alone is enough."""
        assert cps._has_refactor_signal("", "architecture rewrite") is True


class TestMain:
    """CLI behaviour — argparse plumbing and stdout shape."""

    def test_prints_scope_only(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Stdout is the bare scope label — no ``SCOPE=`` prefix, no trailing data."""
        rc = cps.main(["--py-files", "0", "--loc-delta", "0", "--new-api-lines", "0", "--labels", "", "--title", ""])
        assert rc == 0
        captured = capsys.readouterr()
        assert captured.out.strip() == "CHORE"

    def test_feature_via_cli(self, capsys: pytest.CaptureFixture[str]) -> None:
        """New-api-lines > 0 routes to FEATURE end-to-end."""
        rc = cps.main(["--py-files", "2", "--loc-delta", "10", "--new-api-lines", "5", "--labels", "", "--title", ""])
        assert rc == 0
        assert capsys.readouterr().out.strip() == "FEATURE"

    def test_missing_required_arg_exits_nonzero(self) -> None:
        """Argparse exits 2 when a required flag is missing."""
        with pytest.raises(SystemExit) as exc:
            cps.main(["--py-files", "1"])
        assert exc.value.code == 2

    def test_labels_and_title_default_empty(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Verify command-line option behavior.

        --labels and --title are optional — default empty strings.
        """
        rc = cps.main(["--py-files", "2", "--loc-delta", "30", "--new-api-lines", "0"])
        assert rc == 0
        # No refactor signal → FIX
        assert capsys.readouterr().out.strip() == "FIX"
