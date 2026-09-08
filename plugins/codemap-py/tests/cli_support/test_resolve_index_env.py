"""Tests for ``bin/resolve_index_env.py`` — PROJ + INDEX temp-file writer.

The script calls ``resolve_proj_index.py`` via subprocess, reads PROJ (line 1)
and INDEX (line 2), and writes each to ``${TMPDIR}/codemap-resolve-{proj,index}-${CSID}``
for the caller to read back with ``cat``.

Tests cover:
* Happy path — resolver returns valid PROJ + INDEX → temp files written, exit 0
* ``--check-exists`` with present INDEX file → exit 0, temp files written
* ``--check-exists`` with missing INDEX file → exit 1, temp files still written
* Resolver failure (empty output) → exit 1, temp files written (empty)
* Unknown flag → exit 2 with stderr message
* Newline contract — written files end with the delimiter ``IFS= read -r`` needs
"""

from __future__ import annotations

import importlib.util
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent.parent / "bin" / "resolve_index_env.py"
_spec = importlib.util.spec_from_file_location("codemap_resolve_index_env", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

format_eval_line = _mod.format_eval_line
parse_resolver_output = _mod.parse_resolver_output
main = _mod.main
_own_plugin_root = _mod._own_plugin_root
_validate_plugin_root = _mod._validate_plugin_root
_validate_output_prefix = _mod._validate_output_prefix
_write_sentinel_file = _mod._write_sentinel_file


@pytest.fixture(autouse=True)
def _no_session_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear CSID/CLAUDE_CODE_SESSION_ID so ``_resolve_csid()`` degrades to "shared".

    Without this, a real Claude Code session running the suite would leak its own session id into written filenames,
    making ``_read_resolve_file``'s fixed ``-shared`` suffix assumption non-deterministic.
    """
    monkeypatch.delenv("CSID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


def _resolve_file_path(tmp_path: Path, key: str, prefix: str = "codemap") -> Path:
    """Build the exact temp-file path ``main()`` writes for *key*.

    Args:
        tmp_path: Directory where temp files are written.
        key: File key — ``"proj"`` or ``"index"``.
        prefix: File name prefix (default ``"codemap"``).

    Returns:
        Path of the temp file for *key*.

    >>> _resolve_file_path(Path("tmp"), "index").name
    'codemap-resolve-index-shared'
    """
    # tests monkeypatch CSID/CLAUDE_CODE_SESSION_ID empty (see conftest below) so
    # _resolve_csid() always degrades to "shared" here.
    return tmp_path / f"{prefix}-resolve-{key}-shared"


def _read_resolve_file(tmp_path: Path, key: str, prefix: str = "codemap") -> str:
    """Read a resolve temp file by exact path, dropping its trailing newline.

    Sentinel files are newline-terminated so the shell's ``IFS= read -r`` succeeds
    instead of falling through to its ``||`` default; ``read`` consumes that
    delimiter, so these assertions compare against the value the shell would see.
    ``TestSentinelNewlineContract`` locks the raw byte contract.

    Args:
        tmp_path: Directory where temp files are written.
        key: File key — ``"proj"`` or ``"index"``.
        prefix: File name prefix (default ``"codemap"``).

    Returns:
        Contents of the temp file without its trailing newline.
    """
    path = _resolve_file_path(tmp_path, key, prefix)
    assert path.exists(), f"Expected temp file not found: {path}"
    return path.read_text().removesuffix("\n")


def _make_resolver_mock(
    proj: str,
    index_path: str,
    returncode: int = 0,
) -> Any:
    """Return a callable that mimics ``subprocess.run`` against the resolver.

    Args:
        proj: PROJ value to emit on line 1 of the mocked stdout.
        index_path: INDEX value to emit on line 2 of the mocked stdout.
        returncode: Exit code for the mocked CompletedProcess.

    Returns:
        Callable suitable for ``monkeypatch.setattr`` of ``subprocess.run``.

    >>> _make_resolver_mock("demo", "index.json")(["resolve"]).stdout
    'demo\\nindex.json\\n'
    """

    def _run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Return the configured resolver subprocess result for this test."""
        stdout = "" if returncode != 0 else f"{proj}\n{index_path}\n"
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="")

    return _run


def _make_empty_resolver_mock() -> Any:
    """Return a callable that mimics a resolver producing no output."""

    def _run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Return an empty resolver subprocess result for this test."""
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return _run


class TestParseResolverOutput:
    """Unit tests for ``parse_resolver_output()``."""

    def test_two_line_input(self) -> None:
        """Extracts PROJ from line 1 and INDEX from line 2."""
        assert parse_resolver_output("myproj\n/tmp/idx.json\n") == ("myproj", "/tmp/idx.json")

    def test_missing_second_line(self) -> None:
        """Return empty INDEX when only PROJ line present."""
        assert parse_resolver_output("only-proj\n") == ("only-proj", "")

    def test_empty_input(self) -> None:
        """Return both empty when stdout is empty."""
        assert parse_resolver_output("") == ("", "")

    def test_extra_lines_ignored(self) -> None:
        """Lines beyond the second are discarded."""
        assert parse_resolver_output("a\nb\nc\nd\n") == ("a", "b")


class TestFormatEvalLine:
    """Unit tests for ``format_eval_line()`` — retained as pure helper."""

    def test_simple_values_unquoted(self) -> None:
        """Values without metacharacters appear bare (shlex.quote shortcut)."""
        assert format_eval_line("myproj", "/tmp/index.json") == "PROJ=myproj INDEX=/tmp/index.json"

    def test_space_in_proj_is_quoted(self) -> None:
        """Whitespace forces single-quote wrapping."""
        assert format_eval_line("proj with space", "/tmp/x.json") == "PROJ='proj with space' INDEX=/tmp/x.json"

    def test_eval_round_trip_simple(self) -> None:
        """format_eval_line output round-trips through shlex back to original values."""
        line = format_eval_line("round-trip", "/tmp/idx.json")
        parts = dict(tok.split("=", 1) for tok in shlex.split(line) if "=" in tok)
        assert parts["PROJ"] == "round-trip"
        assert parts["INDEX"] == "/tmp/idx.json"

    def test_eval_round_trip_with_quote(self) -> None:
        """Embedded single quotes survive the shlex round-trip via shlex.quote."""
        tricky = "proj'with'quote"
        line = format_eval_line(tricky, "/tmp/idx.json")
        parts = dict(tok.split("=", 1) for tok in shlex.split(line) if "=" in tok)
        assert parts["PROJ"] == tricky


class TestMainHappyPath:
    """Write temporary output files when resolution succeeds."""

    def test_happy_path_writes_temp_files(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No flags: writes PROJ and INDEX to temp files and exits 0."""
        monkeypatch.setattr(_mod.subprocess, "run", _make_resolver_mock("demo-proj", "/tmp/demo.json"))
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        rc = main([])
        assert rc == 0
        assert _read_resolve_file(tmp_path, "proj") == "demo-proj"
        assert _read_resolve_file(tmp_path, "index") == "/tmp/demo.json"

    def test_happy_path_tricky_values(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Values with spaces and special chars are written raw (no shell quoting)."""
        tricky_proj = "proj with space"
        tricky_index = str(tmp_path / "tricky idx.json")
        monkeypatch.setattr(_mod.subprocess, "run", _make_resolver_mock(tricky_proj, tricky_index))
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        rc = main([])
        assert rc == 0
        assert _read_resolve_file(tmp_path, "proj") == tricky_proj
        assert _read_resolve_file(tmp_path, "index") == tricky_index


class TestSentinelNewlineContract:
    """Sentinel files are newline-terminated — the shell ``IFS= read -r`` contract."""

    def test_written_files_are_newline_terminated(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Raw bytes end with the delimiter, so readers never hit their ``|| VAR=`` fallback."""
        monkeypatch.setattr(_mod.subprocess, "run", _make_resolver_mock("demo-proj", "/tmp/demo.json"))
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        assert main([]) == 0
        assert _resolve_file_path(tmp_path, "proj").read_bytes() == b"demo-proj\n"
        assert _resolve_file_path(tmp_path, "index").read_bytes() == b"/tmp/demo.json\n"


class TestCheckExists:
    """Gate the exit code on whether the index file exists."""

    def test_present_index_exits_0(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Write temporary state and succeed when the index exists."""
        index = tmp_path / "with-index.json"
        index.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(_mod.subprocess, "run", _make_resolver_mock("with-index", str(index)))
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        rc = main(["--check-exists"])
        assert rc == 0
        assert _read_resolve_file(tmp_path, "proj") == "with-index"
        assert _read_resolve_file(tmp_path, "index") == str(index)

    def test_missing_index_exits_1_but_writes_temp_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Write temporary state but fail when the index is absent."""
        missing = tmp_path / "absent.json"  # never created
        monkeypatch.setattr(_mod.subprocess, "run", _make_resolver_mock("no-idx", str(missing)))
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        rc = main(["--check-exists"])
        captured = capsys.readouterr()
        assert rc == 1
        # Temp files still written — PROJ and INDEX path available for diagnostics.
        assert _read_resolve_file(tmp_path, "proj") == "no-idx"
        assert _read_resolve_file(tmp_path, "index") == str(missing)
        assert "INDEX file not found" in captured.err
        assert str(missing) in captured.err


class TestResolverFailure:
    """Resolver returns no output → exit 1, empty temp files written."""

    def test_empty_resolver_exits_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Empty resolver stdout → exit 1, empty temp files written, error on stderr."""
        monkeypatch.setattr(_mod.subprocess, "run", _make_empty_resolver_mock())
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        rc = main([])
        captured = capsys.readouterr()
        assert rc == 1
        assert _read_resolve_file(tmp_path, "proj") == ""
        assert _read_resolve_file(tmp_path, "index") == ""
        assert "produced no output" in captured.err

    def test_check_exists_with_empty_resolver_exits_1(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Do not change behaviour when resolver itself fails first."""
        monkeypatch.setattr(_mod.subprocess, "run", _make_empty_resolver_mock())
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        rc = main(["--check-exists"])
        assert rc == 1


class TestUnknownFlag:
    """Unknown CLI flag → exit 2 with stderr message naming the flag."""

    def test_unknown_flag_exits_2(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Reject an unknown option and identify it in the error output."""
        rc = main(["--no-such-flag"])
        captured = capsys.readouterr()
        assert rc == 2
        assert "unknown flag" in captured.err
        assert "--no-such-flag" in captured.err


class TestValidatePluginRoot:
    """Validate plugin-root containment through resolved path identity.

    Regression coverage for the residual-critical finding that the prior regex hardcoded the pre-rename plugin name
    ``codemap`` (rejecting the real installed/source root after the ``codemap-py`` rename) while also accepting
    attacker-chosen directories that merely matched the pattern's shape (no traversal normalization).
    """

    def test_own_plugin_root_is_accepted(self) -> None:
        """The directory this script itself runs from always validates."""
        own = str(_own_plugin_root())
        assert _validate_plugin_root(own) == own

    def test_unrelated_absolute_path_is_rejected(self) -> None:
        """A directory that merely matches the old shape (``.../plugins/codemap``) is rejected."""
        with pytest.raises(ValueError, match="not a safe path"):
            _validate_plugin_root("/tmp/attacker/plugins/codemap")

    def test_traversal_out_of_own_root_is_rejected(self) -> None:
        """Appending ``../`` segments to the real root no longer bypasses containment."""
        own = _own_plugin_root()
        with pytest.raises(ValueError, match="not a safe path"):
            _validate_plugin_root(str(own) + "/../../../../tmp/evil")

    def test_relative_path_is_rejected(self) -> None:
        """A relative value is rejected before any resolution is attempted."""
        with pytest.raises(ValueError, match="not a safe path"):
            _validate_plugin_root("plugins/codemap-py")


class TestValidateOutputPrefix:
    """Accept dotted output basenames while rejecting directory traversal.

    Regression coverage for the residual-critical finding that the prior ``[a-zA-Z0-9_-]+`` pattern rejected any project
    directory name containing a dot (including this repository's own ``Borda.local``), a hard break in the documented
    ``codemap-$(basename ...)`` recipe.
    """

    def test_dotted_basename_prefix_is_accepted(self) -> None:
        """A prefix built from a dotted project basename validates."""
        assert _validate_output_prefix("codemap-Borda.local") == "codemap-Borda.local"

    @pytest.mark.parametrize("bad", [".", "..", "../escape", ""])
    def test_dot_forms_and_traversal_are_rejected(self, bad: str) -> None:
        """Bare ``.``/``..``, a traversal segment, and an empty value all raise."""
        with pytest.raises(ValueError, match="output-prefix"):
            _validate_output_prefix(bad)


class TestSentinelSymlinkSafety:
    """Refuse to follow a pre-existing sentinel symlink.

    Regression coverage for the residual-critical finding that ``Path.write_text`` (the prior implementation) follows an
    existing symlink at the predictable sentinel path, letting a co-located attacker overwrite an arbitrary file the
    invoking user can write.
    """

    def test_preplanted_symlink_is_not_followed(self, tmp_path: Path) -> None:
        """Writing to a path that is a symlink raises instead of truncating the target."""
        victim = tmp_path / "victim.txt"
        victim.write_text("IMPORTANT ORIGINAL CONTENT\n", encoding="utf-8")
        link = tmp_path / "codemap-resolve-proj-shared"
        link.symlink_to(victim)

        with pytest.raises(OSError):
            _write_sentinel_file(link, "PWNED\n")

        assert victim.read_text(encoding="utf-8") == "IMPORTANT ORIGINAL CONTENT\n"

    @pytest.mark.skipif(os.name == "nt", reason="requires POSIX private-mode semantics")
    def test_written_file_is_mode_0600(self, tmp_path: Path) -> None:
        """A freshly written sentinel is owner-only readable/writable regardless of umask."""
        target = tmp_path / "codemap-resolve-index-shared"
        _write_sentinel_file(target, "value\n")
        assert (target.stat().st_mode & 0o777) == 0o600


class TestStaleSentinelClearedOnValidationFailure:
    """A validation failure after a successful run must not leave the prior PROJ/INDEX readable.

    Regression coverage for the residual-critical finding that an exit-3 (unsafe ``CLAUDE_PLUGIN_ROOT``) run used to
    skip the temp-file write entirely, so the shell consumer's ``[ -n "$PROJ" ]`` liveness check would pass on stale,
    unrelated-project data.
    """

    def test_failing_run_after_success_empties_sentinels(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A seed run writes real values; a subsequent validation failure clears them."""
        monkeypatch.setattr(_mod.subprocess, "run", _make_resolver_mock("old-proj", "/tmp/old.json"))
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        assert main(["--output-prefix", "codemap-stale"]) == 0
        assert _read_resolve_file(tmp_path, "proj", prefix="codemap-stale") == "old-proj"

        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", os.path.join(str(tmp_path), "not-the-real-root"))
        rc = main(["--output-prefix", "codemap-stale"])

        assert rc == 3
        assert _read_resolve_file(tmp_path, "proj", prefix="codemap-stale") == ""
        assert _read_resolve_file(tmp_path, "index", prefix="codemap-stale") == ""
