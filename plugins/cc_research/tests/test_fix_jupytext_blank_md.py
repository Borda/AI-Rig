"""Tests for ``research/bin/fix_jupytext_blank_md.py``.

Output contract:

* Bare ``#``/``##``/... lines inside ``# %% [markdown]`` cells are cleared to truly empty lines; the same lines inside
  ``# %%`` (code) cells are left untouched.
* ``fix_text`` is pure (string in, string + count out); ``main`` wraps it with file I/O and a ``--check`` (report-only,
  exit 1 on violations) mode.
"""

from __future__ import annotations

from pathlib import Path

import fix_jupytext_blank_md as gate
import pytest

_SPACER_NOTEBOOK = "# %% [markdown]\n# Para one.\n#\n# Para two.\n"
_CLEARED_NOTEBOOK = "# %% [markdown]\n# Para one.\n\n# Para two.\n"


class TestFixText:
    """The pure text transformation: which lines are cleared and which are left alone."""

    def test_clears_bare_hash_in_markdown_cell(self) -> None:
        """A lone ``#`` between markdown paragraphs becomes an empty line.

        This is the exact shape jupytext emits for a blank line inside a markdown cell — the gate must turn it back into
        a real blank line so it renders as whitespace instead of an empty heading.
        """
        fixed, count = gate.fix_text(_SPACER_NOTEBOOK)

        assert fixed == _CLEARED_NOTEBOOK
        assert count == 1

    @pytest.mark.parametrize("spacer", ["#", "##", "# ", "###  "])
    def test_clears_any_hash_only_spacer(self, spacer: str) -> None:
        """Any run of ``#`` with only whitespace after it counts as a spacer.

        notebook-style.md's Markdown blank lines anchor covers not just a bare ``#`` but any hash-only line, since
        ``##`` alone renders as an empty H2 just as ``#`` renders as an empty H1.
        """
        _fixed, count = gate.fix_text(f"# %% [markdown]\n# Para one.\n{spacer}\n# Para two.\n")

        assert count == 1

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("# %%\nx = 1\n#\ny = 2\n", id="bare-hash-in-code-cell"),
            pytest.param("# %% [markdown]\n# ## Heading\n# Some real sentence.\n", id="real-content-lines"),
        ],
    )
    def test_leaves_non_spacer_text_untouched(self, text: str) -> None:
        """Text that is not a markdown-cell spacer survives unchanged with a zero count.

        A lone ``#`` inside a code cell is not a markdown-cell artifact: the gate is scoped to markdown cells only, so a
        stray ``#`` comment line in a code cell has no heading-rendering consequence. Markdown lines carrying actual
        text are never modified — this guards against an overly broad pattern that would also strip legitimate ``# ``
        prefixed prose.
        """
        fixed, count = gate.fix_text(text)

        assert fixed == text
        assert count == 0

    def test_second_pass_is_idempotent(self) -> None:
        """Running the gate twice finds nothing left to fix.

        The Kaggle skill's Step 4 verify pass may run this gate more than once across a session; a non-idempotent fixer
        would corrupt an already-clean file on a repeat run.
        """
        once, _ = gate.fix_text(_SPACER_NOTEBOOK)

        twice, count = gate.fix_text(once)

        assert twice == once
        assert count == 0

    def test_code_cell_after_markdown_cell_is_not_scoped(self) -> None:
        """Cell-marker tracking resets scope at the next ``# %%``.

        A file with a markdown cell followed by a code cell must not keep treating the code cell's bare ``#`` lines as
        markdown spacers.
        """
        fixed, count = gate.fix_text("# %% [markdown]\n# Para.\n#\n\n# %%\n#\nz = 1\n")

        assert count == 1
        assert "\n#\nz = 1\n" in fixed


class TestMain:
    """The CLI wrapper: file rewriting, report-only mode and the working-directory guard."""

    @pytest.fixture(autouse=True)
    def _work_in_tmp(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Run from ``tmp_path``, as kaggle runs from the project root that holds its notebook."""
        monkeypatch.chdir(tmp_path)

    def test_writes_fixed_file_and_reports_count(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Default mode rewrites the notebook with spacers cleared and reports how many were replaced.

        A notebook with one bare ``#`` spacer ends up with a real blank line and a ``cleared 1`` line on stdout.
        """
        target = tmp_path / "notebook.py"
        target.write_text(_SPACER_NOTEBOOK, encoding="utf-8")

        assert gate.main([str(target)]) == 0

        assert "cleared 1" in capsys.readouterr().out
        assert target.read_text(encoding="utf-8") == _CLEARED_NOTEBOOK

    def test_clean_file_produces_no_output(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A file with nothing to fix prints nothing and exits 0."""
        target = tmp_path / "notebook.py"
        target.write_text(_CLEARED_NOTEBOOK, encoding="utf-8")

        assert gate.main([str(target)]) == 0
        assert capsys.readouterr().out == ""

    def test_check_mode_reports_without_writing(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """``--check`` reports violations and exits 1 while leaving the file untouched.

        Report-only mode lets a pre-commit style caller fail on a dirty notebook without rewriting it.
        """
        target = tmp_path / "notebook.py"
        target.write_text(_SPACER_NOTEBOOK, encoding="utf-8")

        assert gate.main(["--check", str(target)]) == 1

        assert "1 bare" in capsys.readouterr().out
        assert target.read_text(encoding="utf-8") == _SPACER_NOTEBOOK

    @pytest.mark.parametrize(
        "argument",
        [pytest.param("../outside/notebook.py", id="dot-dot-segment"), pytest.param("{target}", id="absolute-path")],
    )
    def test_file_outside_working_directory_is_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argument: str
    ) -> None:
        """A path resolving outside the working directory is left untouched with a skip notice on stderr.

        The fixer rewrites files in place; its only caller passes the kaggle notebook it just wrote under the project,
        so any argument escaping the working tree is refused rather than rewritten.
        """
        target = tmp_path / "outside" / "notebook.py"
        target.parent.mkdir()
        target.write_text(_SPACER_NOTEBOOK, encoding="utf-8")
        (tmp_path / "work").mkdir()
        monkeypatch.chdir(tmp_path / "work")

        assert gate.main([argument.format(target=target)]) == 0

        assert "outside the working directory" in capsys.readouterr().err
        assert target.read_text(encoding="utf-8") == _SPACER_NOTEBOOK
