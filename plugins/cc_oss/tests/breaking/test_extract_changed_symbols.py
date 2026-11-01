"""Tests for ``bin/extract_changed_symbols.py``.

``subprocess.run`` and ``which`` are replaced by autospecced fakes, so no real ``git`` call is made. A ``tmp_path``
working directory controls which ``__init__.py`` files ``_find_init_files`` sees. Cases cover ref validation, empty
diffs, symbol extraction, deduplication and the default range.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any
from unittest import mock

import extract_changed_symbols as ecs
import pytest


def _patch_git(
    monkeypatch: pytest.MonkeyPatch,
    *,
    rev_parse_rc: int = 0,
    diff_stdout: str = "",
) -> list[list[str]]:
    """Fake ``git`` by subcommand (ref validation vs diff) and return the list of recorded commands."""
    calls: list[list[str]] = []

    def _fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Record the command, then return the configured ref-validation result or diff payload."""
        calls.append(list(cmd))
        if "rev-parse" in cmd:
            return subprocess.CompletedProcess(cmd, rev_parse_rc, stdout="")
        return subprocess.CompletedProcess(cmd, 0, stdout=diff_stdout)

    monkeypatch.setattr(ecs.subprocess, "run", mock.create_autospec(subprocess.run, side_effect=_fake_run))
    monkeypatch.setattr(ecs, "which", mock.create_autospec(shutil.which, return_value="/fake/git"))
    return calls


class TestMain:
    """The CLI prints the sorted, unique ``class``/``def`` names touched in ``__init__.py`` diffs."""

    @pytest.mark.parametrize(
        "ref",
        [
            pytest.param("nonexistent..HEAD", id="left-ref-of-range-unresolved"),
            pytest.param("nonexistent_ref", id="single-ref-unresolved"),
        ],
    )
    def test_unresolvable_ref_exits_0_with_empty_output(
        self, monkeypatch: pytest.MonkeyPatch, workdir: Path, capsys: pytest.CaptureFixture[str], ref: str
    ) -> None:
        """A range whose left ref, or a single ref, does not resolve exits 0 and prints nothing.

        A ref that git cannot resolve (shallow clone, deleted branch) is not an error for the caller: there is simply
        nothing to extract, and failing here would abort the surrounding resolve run.
        """
        _patch_git(monkeypatch, rev_parse_rc=1)

        rc = ecs.main([ref])

        assert rc == 0
        assert capsys.readouterr().out.strip() == ""

    @pytest.mark.parametrize(
        ("filename", "content"),
        [
            pytest.param("module.py", "class Foo: pass\n", id="no-init-py-in-tree"),
            pytest.param("__init__.py", "", id="empty-diff-for-unchanged-initializer"),
        ],
    )
    def test_nothing_to_extract_exits_0_with_empty_output(
        self,
        monkeypatch: pytest.MonkeyPatch,
        workdir: Path,
        capsys: pytest.CaptureFixture[str],
        filename: str,
        content: str,
    ) -> None:
        """No ``__init__.py`` in the tree, or an empty diff for an unchanged initializer, exits 0 and prints nothing.

        Both situations are normal (a change that never touched a package surface) and must stay silent.
        """
        (workdir / filename).write_text(content)
        _patch_git(monkeypatch, diff_stdout="")

        rc = ecs.main(["HEAD~1..HEAD"])

        assert rc == 0
        assert capsys.readouterr().out.strip() == ""

    @pytest.mark.parametrize(
        ("diff_stdout", "expected"),
        [
            pytest.param("+class Foo:\n+def bar():\n+    pass\n", ["Foo", "bar"], id="class-and-def-names-extracted"),
            pytest.param(
                "+def zoo():\n+class Alpha:\n+def Beta():\n",
                ["Alpha", "Beta", "zoo"],
                id="symbols-sorted-sort-u-behaviour",
            ),
            pytest.param("+class Dup:\n-class Dup:\n+class Dup:\n", ["Dup"], id="repeated-symbol-printed-once"),
            pytest.param(" class Context: pass\n+class Added:\n", ["Added"], id="context-lines-not-extracted"),
            pytest.param(
                "--- a/__init__.py\n+++ b/__init__.py\n+class Real:\n",
                ["Real"],
                id="diff-header-lines-not-symbol-lines",
            ),
        ],
    )
    def test_symbols_extracted_from_changed_lines(
        self,
        monkeypatch: pytest.MonkeyPatch,
        workdir: Path,
        capsys: pytest.CaptureFixture[str],
        diff_stdout: str,
        expected: list[str],
    ) -> None:
        """Only added or removed ``class``/``def`` lines yield symbols, printed sorted and without duplicates.

        Context lines (no leading +/-) and the ``---``/``+++`` diff header lines are not symbol lines; the same name
        from several diff lines is printed once; output order is the ``sort -u`` order.
        """
        (workdir / "__init__.py").write_text("")
        _patch_git(monkeypatch, diff_stdout=diff_stdout)

        rc = ecs.main(["HEAD~1..HEAD"])

        assert rc == 0
        assert capsys.readouterr().out.splitlines() == expected

    def test_defaults_to_previous_commit_range(self, monkeypatch: pytest.MonkeyPatch, workdir: Path) -> None:
        """Without arguments the script validates and diffs ``HEAD~1..HEAD``.

        Both ends of the default range go through ``git rev-parse`` before the diff runs, which is how an unresolvable
        default (a repository with one commit) is detected.
        """
        (workdir / "__init__.py").write_text("")
        calls = _patch_git(monkeypatch)

        ecs.main([])

        rev_parse_calls = [c for c in calls if "rev-parse" in c]
        assert any("HEAD~1" in c for c in rev_parse_calls)
        assert any("HEAD" in c for c in rev_parse_calls)
