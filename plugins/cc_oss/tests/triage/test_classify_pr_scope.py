"""Tests for ``bin/classify_pr_scope.py``.

Pure deterministic classifier — no subprocess, no I/O. Covers the five classification branches plus the refactor-signal
override and CLI plumbing.
"""

from __future__ import annotations

import classify_pr_scope as cps  # type: ignore[import-not-found]
import pytest


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
        self, py_files: int, loc_delta: int, new_api_lines: int, labels: str, title: str, expected: cps.PRScope
    ) -> None:
        """Each case lands in its classification branch, including the threshold edges.

        Zero Python files is a chore, new API lines mean a feature, tiny diffs are fixes, wide diffs are refactors, and
        a large change in few files is mixed.
        """
        result = cps.classify(
            py_files=py_files, loc_delta=loc_delta, new_api_lines=new_api_lines, labels=labels, title=title
        )
        assert result == expected


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
            pytest.param("PERF", "Speed UP", id="signal-case-insensitive"),
        ],
    )
    def test_signal_promotes_fix_to_refactor(self, labels: str, title: str) -> None:
        """A tiny diff that would be FIX becomes REFACTOR when a label or title carries a signal token.

        Labels and titles are matched case-insensitively; the diff size (2 files, 30 lines) is held constant.
        """
        result = cps.classify(py_files=2, loc_delta=30, new_api_lines=0, labels=labels, title=title)
        assert result == cps.PRScope.REFACTOR

    @pytest.mark.parametrize(
        ("new_api_lines", "labels", "title", "expected"),
        [
            pytest.param(0, "bug", "fix typo in docstring", cps.PRScope.FIX, id="no-signal-stays-fix"),
            pytest.param(5, "refactor", "Add new export", cps.PRScope.FEATURE, id="signal-ignored-for-feature"),
        ],
    )
    def test_without_override_the_base_scope_is_kept(
        self, new_api_lines: int, labels: str, title: str, expected: cps.PRScope
    ) -> None:
        """A tiny diff without a signal stays FIX, and a refactor signal never overrides FEATURE.

        New API lines take precedence over the refactor signal, so the override only upgrades FIX.
        """
        result = cps.classify(py_files=2, loc_delta=30, new_api_lines=new_api_lines, labels=labels, title=title)
        assert result == expected


class TestHasRefactorSignal:
    """Direct coverage of the signal helper — boundary tokens, empties."""

    @pytest.mark.parametrize(
        ("labels", "title", "expected"),
        [
            pytest.param("", "", False, id="empty-inputs"),
            pytest.param("cleanup", "", True, id="signal-in-labels-only"),
            pytest.param("", "architecture rewrite", True, id="signal-in-title-only"),
        ],
    )
    def test_signal_is_found_in_labels_or_title(self, labels: str, title: str, expected: bool) -> None:
        """A signal token in labels alone or in title alone is enough; empty labels and title carry no signal."""
        assert cps._has_refactor_signal(labels, title) is expected


class TestMain:
    """CLI behaviour — argparse plumbing and stdout shape."""

    @pytest.mark.parametrize(
        ("py_files", "loc_delta", "new_api_lines", "expected"),
        [
            pytest.param("0", "0", "0", "CHORE", id="bare-scope-label-no-prefix"),
            pytest.param("2", "10", "5", "FEATURE", id="new-api-lines-route-to-feature"),
        ],
    )
    def test_prints_scope_label_only(
        self, capsys: pytest.CaptureFixture[str], py_files: str, loc_delta: str, new_api_lines: str, expected: str
    ) -> None:
        """Stdout is the bare scope label — no ``SCOPE=`` prefix, no trailing data.

        Zero changed files prints CHORE; new-api-lines > 0 routes to FEATURE end-to-end.
        """
        argv = ["--py-files", py_files, "--loc-delta", loc_delta, "--new-api-lines", new_api_lines]
        rc = cps.main([*argv, "--labels", "", "--title", ""])
        assert rc == 0
        assert capsys.readouterr().out.strip() == expected

    def test_missing_required_arg_exits_2(self) -> None:
        """Argparse exits 2 when a required flag is missing.

        Only ``--py-files`` is supplied; the other required counters are absent.
        """
        with pytest.raises(SystemExit) as exc:
            cps.main(["--py-files", "1"])
        assert exc.value.code == 2

    def test_labels_and_title_default_empty(self, capsys: pytest.CaptureFixture[str]) -> None:
        """``--labels`` and ``--title`` are optional and default to empty strings, so no refactor signal applies.

        A 2-file, 30-line diff with neither flag stays FIX, proving the defaults do not inject a signal.
        """
        rc = cps.main(["--py-files", "2", "--loc-delta", "30", "--new-api-lines", "0"])
        assert rc == 0
        assert capsys.readouterr().out.strip() == "FIX"
