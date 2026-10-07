"""Tests for ``bin/extract-keep-flag.py``.

Covers:
    - ``option_value`` spaced and attached ``--name value`` spellings.
    - ``main()`` happy path: keep value parsed, written to the session sentinel, printed.
    - Identity guards: missing slug and missing session ID both exit 2 before any write.
    - ``--out-file`` redirection, parent creation, and the containment rejection.
    - ``--venue-choices`` validation and persistence.
    - Stale-contract clearing, which is CWD-relative and must not escape the test dir.
"""

from __future__ import annotations

from pathlib import Path

# Loaded by conftest.py — `extract_keep_flag` is registered in sys.modules there.
import extract_keep_flag as ekf
import pytest


@pytest.fixture
def session(tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate one run: temp dir, working directory, and session ID all inside ``tmp_path``.

    ``_CONTRACT`` is deliberately CWD-relative, so without the chdir every test would unlink the repository's own
    ``.temp/state/skill-contract.md``.
    """
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "sess1")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestOptionValue:
    """Hand-rolled option parsing — the spellings five live call sites actually pass."""

    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            pytest.param(["--out-file", "/x/y"], "/x/y", id="spaced"),
            pytest.param(["--out-file=/x/y"], "/x/y", id="attached"),
            pytest.param(["--other", "z"], "", id="absent"),
            pytest.param(["--out-file"], "", id="named-without-value"),
        ],
    )
    def test_reads_requested_option(self, argv: list[str], expected: str) -> None:
        """The named option's value is returned, or an empty string when it is absent.

        The attached spelling matters because callers forward a raw ``$ARGUMENTS`` string that may already have been
        joined by the shell.
        """
        assert ekf.option_value(argv, "--out-file") == expected


class TestMain:
    """End-to-end CLI contract — sentinel contents, stdout, and exit codes."""

    def test_writes_and_prints_keep_value(self, session: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A quoted ``--keep`` value lands in the session sentinel and on stdout.

        This is the whole point of the script: the skill re-reads the sentinel after a
        compaction, so a value that parses but is never written is silently lost.
        """
        exit_code = ekf.main(["extract-keep-flag.py", "myskill", '--keep "run-dir, task ids"'])
        assert exit_code == 0
        assert (session / "myskill-keep-items-sess1").read_text(encoding="utf-8") == "run-dir, task ids\n"
        assert capsys.readouterr().out == "run-dir, task ids\n"

    def test_absent_keep_flag_writes_empty_value(self, session: Path) -> None:
        """No ``--keep`` in the arguments still writes the sentinel, holding an empty value.

        Callers read the sentinel unconditionally, so an absent file would be a missing read rather than a legitimately
        empty preserve list.
        """
        exit_code = ekf.main(["extract-keep-flag.py", "myskill", "--other-flag x"])
        assert exit_code == 0
        assert (session / "myskill-keep-items-sess1").read_text(encoding="utf-8") == "\n"

    def test_clears_stale_contract_file(self, session: Path) -> None:
        """A contract left by a crashed prior run is removed before the new value is written.

        A stale contract would otherwise leak its preserve items into an unrelated later compaction, which is the
        failure this clear exists to prevent.
        """
        contract = session / ".temp" / "state" / "skill-contract.md"
        contract.parent.mkdir(parents=True)
        contract.write_text("stale\n", encoding="utf-8")
        ekf.main(["extract-keep-flag.py", "myskill", '--keep "x"'])
        assert not contract.exists()

    def test_missing_slug_returns_two(self, session: Path) -> None:
        """No sentinel slug is a caller bug and exits 2 before touching any state.

        The slug names the sentinel and prefixes every error message, so there is no sensible default to fall back on.
        """
        assert ekf.main(["extract-keep-flag.py"]) == 2

    def test_missing_session_id_returns_two(self, session: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """An unset session ID exits 2 rather than guessing one.

        Deriving it from the parent process would name the calling shell, producing a sentinel under a session ID no
        caller ever reads back.
        """
        monkeypatch.delenv("CSID", raising=False)
        assert ekf.main(["extract-keep-flag.py", "myskill", '--keep "x"']) == 2


class TestOutFile:
    """``--out-file`` redirection — where a per-session state directory is used instead."""

    def test_redirects_write_and_creates_parents(self, session: Path) -> None:
        """The keep value goes to the named path, whose parent directory is created.

        Skills keeping state in ``<skill>-state-<csid>/keep-items`` make this script the first writer into that
        directory.
        """
        target = session / "myskill-state-sess1" / "keep-items"
        exit_code = ekf.main(["extract-keep-flag.py", "myskill", '--keep "v"', "--out-file", str(target)])
        assert exit_code == 0
        assert target.read_text(encoding="utf-8") == "v\n"

    def test_named_without_value_returns_two(self, session: Path) -> None:
        """``--out-file`` with no value exits 2 instead of falling back to the slug path.

        The fallback would exit 0 while writing somewhere the caller never reads, losing the keep value with no signal.
        """
        exit_code = ekf.main(["extract-keep-flag.py", "myskill", '--keep "v"', "--out-file"])
        assert exit_code == 2
        assert not (session / "myskill-keep-items-sess1").exists()

    def test_path_outside_allowed_roots_returns_two(self, session: Path) -> None:
        """A path outside the temp dir and working directory is rejected.

        This script creates parent directories, so an unconstrained path would let a value that reached argv build a
        tree anywhere the process can write.
        """
        outside = session.parent / "elsewhere" / "keep-items"
        exit_code = ekf.main(["extract-keep-flag.py", "myskill", '--keep "v"', "--out-file", str(outside)])
        assert exit_code == 2
        assert not outside.exists()


class TestVenue:
    """``--venue-choices`` validation, which runs after the keep value is already written."""

    @pytest.mark.parametrize(
        ("keep_arg", "expected"),
        [
            pytest.param('--keep "v" --venue pr', "pr\n", id="valid-venue-persisted"),
            # No ``--venue`` is legal and writes an empty venue for the caller's skip rule.
            pytest.param('--keep "v"', "\n", id="absent-venue-writes-empty-value"),
        ],
    )
    def test_venue_is_persisted(self, session: Path, keep_arg: str, expected: str) -> None:
        """A venue in the allowed list is written to its own sentinel; an absent venue writes an empty one."""
        exit_code = ekf.main(["extract-keep-flag.py", "myskill", keep_arg, "--venue-choices", "pr,issue"])
        assert exit_code == 0
        assert (session / "myskill-venue-sess1").read_text(encoding="utf-8") == expected

    def test_invalid_venue_returns_two_after_keep_is_written(self, session: Path) -> None:
        """An unlisted venue exits 2, but the keep value written earlier still stands.

        The documented ordering is deliberate: the caller owes no cleanup on this path
        because the contract was already cleared and the keep sentinel already written.
        """
        exit_code = ekf.main(
            [
                "extract-keep-flag.py",
                "myskill",
                '--keep "v" --venue nowhere',
                "--venue-choices",
                "pr,issue",
            ]
        )
        assert exit_code == 2
        assert (session / "myskill-keep-items-sess1").read_text(encoding="utf-8") == "v\n"
