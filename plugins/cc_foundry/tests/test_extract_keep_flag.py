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

import os
import stat
import tempfile
from pathlib import Path

# Loaded by conftest.py — `extract_keep_flag` is registered in sys.modules there.
import extract_keep_flag as ekf
import pytest


def _probe_symlink() -> bool:
    """Return whether this process may create a symlink (capability probe, not a platform test)."""
    with tempfile.TemporaryDirectory() as tmp:
        try:
            Path(tmp, "link").symlink_to(Path(tmp, "target"))
        except (OSError, NotImplementedError):
            return False
        return True


_skip_no_symlink = pytest.mark.skipif(not _probe_symlink(), reason="symlinks unavailable to this process")


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

    @pytest.mark.parametrize(
        ("stamp", "extra", "survives"),
        [
            pytest.param('{"session_id": "other-session"}', [], True, id="foreign-stamp-kept"),
            pytest.param('{"session_id": "this-session"}', [], False, id="own-stamp-cleared"),
            pytest.param("not json", [], False, id="malformed-stamp-cleared"),
            pytest.param("[1]", [], False, id="non-object-stamp-cleared"),
            pytest.param(None, [], False, id="no-stamp-cleared"),
            pytest.param('{"session_id": "this-session"}', ["--inlined"], True, id="inlined-keeps-own-contract"),
        ],
    )
    def test_contract_clear_respects_owner_and_inlined(
        self, session: Path, monkeypatch: pytest.MonkeyPatch, stamp: str | None, extra: list[str], survives: bool
    ) -> None:
        """Another session's live contract and an inlined caller's parent contract are never deleted.

        Concurrent sessions in one working directory share ``.temp/state/skill-contract.md``, and a sweep that loads
        judge inline calls this script after writing its own loop contract; an unconditional clear erased both.
        """
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "this-session")
        contract = session / ".temp" / "state" / "skill-contract.md"
        contract.parent.mkdir(parents=True)
        contract.write_text("live\n", encoding="utf-8")
        if stamp is not None:
            contract.with_suffix(".owner").write_text(stamp, encoding="utf-8")
        assert ekf.main(["extract-keep-flag.py", "myskill", '--keep "x"', *extra]) == 0
        assert contract.exists() is survives

    @pytest.mark.parametrize("identity", ["slug", "session"])
    @pytest.mark.parametrize("value", ["a..b", "../escape", "bad/name", "bad\nname"])
    def test_invalid_identity_preserves_state(
        self,
        session: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        identity: str,
        value: str,
    ) -> None:
        """Reject unsafe names before clearing the contract or writing any sentinel."""
        contract = session / ".temp/state/skill-contract.md"
        contract.parent.mkdir(parents=True)
        contract.write_text("live\n", encoding="utf-8")
        slug = value if identity == "slug" else "safe.slug-1_2"
        monkeypatch.setenv("CSID", value if identity == "session" else "safe.session-1_2")
        assert ekf.main(["extract-keep-flag.py", slug, '--keep "x"']) == 2
        output = capsys.readouterr()
        assert output.out == ""
        assert len(output.err.splitlines()) == 1
        assert contract.read_text(encoding="utf-8") == "live\n"
        assert list(session.iterdir()) == [session / ".temp"]

    def test_valid_identity_punctuation(self, session: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Allow a single dot, underscore and hyphen in both filename components."""
        monkeypatch.setenv("CSID", "session.1_2-3")
        assert ekf.main(["extract-keep-flag.py", "skill.1_2-3", '--keep "x"']) == 0
        assert (session / "skill.1_2-3-keep-items-session.1_2-3").read_text(encoding="utf-8") == "x\n"

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


class TestAtomicSentinelWrite:
    """Sentinels sit at predictable names in a shared temp dir, so a planted symlink must never be written through."""

    @_skip_no_symlink
    def test_symlink_planted_at_the_sentinel_is_replaced_not_followed(self, session: Path) -> None:
        """A symlink at the sentinel name ends up a regular file; the link's target is left untouched.

        ``write_text`` follows the link and overwrites whatever it points at, so another user able to pre-create
        ``<slug>-keep-items-<csid>`` in a world-writable temp dir could redirect the write onto any file the caller
        owns.
        """
        victim = session / "victim.txt"
        victim.write_text("precious\n", encoding="utf-8")
        sentinel = session / "myskill-keep-items-sess1"
        sentinel.symlink_to(victim)
        assert ekf.main(["extract-keep-flag.py", "myskill", '--keep "v"']) == 0
        assert (victim.read_text(encoding="utf-8"), sentinel.is_symlink(), sentinel.read_text(encoding="utf-8")) == (
            "precious\n",
            False,
            "v\n",
        )

    def test_replaces_an_existing_value_and_leaves_no_temp_file(self, session: Path) -> None:
        """A rerun overwrites the previous value and the sibling temp file does not linger beside the sentinel."""
        sentinel = session / "myskill-keep-items-sess1"
        sentinel.write_text("old\n", encoding="utf-8")
        assert ekf.main(["extract-keep-flag.py", "myskill", '--keep "new"']) == 0
        assert (sentinel.read_text(encoding="utf-8"), sorted(p.name for p in session.iterdir())) == (
            "new\n",
            ["myskill-keep-items-sess1"],
        )


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


_skip_without_uids = pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX uids and mode bits unavailable")


class TestSentinelOwnership:
    """A sentinel another user owns is refused, and a state directory this script creates is private to the user."""

    def test_foreign_owned_sentinel_is_refused_and_left_intact(
        self, session: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A keep sentinel owned by another user makes the run exit 1 without replacing that file.

        On a shared ``/tmp`` another user can create ``<slug>-keep-items-<csid>`` first, most easily when the session
        token is guessable. Refusing keeps the planted value from being silently swapped and makes the failure visible.
        The foreign owner is simulated by reporting a different current uid.
        """
        sentinel = session / "myskill-keep-items-sess1"
        sentinel.write_text("planted\n", encoding="utf-8")
        monkeypatch.setattr(ekf.os, "getuid", lambda: sentinel.stat().st_uid + 1, raising=False)

        exit_code = ekf.main(["extract-keep-flag.py", "myskill", '--keep "v"'])

        assert (exit_code, sentinel.read_text(encoding="utf-8")) == (1, "planted\n")

    def test_platform_without_uids_still_writes(self, session: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Where the OS has no ``os.getuid`` (native Windows) the ownership check is skipped and the write proceeds.

        The per-user temp dir there already keeps other users out; refusing every write would break each skill.
        """
        sentinel = session / "myskill-keep-items-sess1"
        sentinel.write_text("old\n", encoding="utf-8")
        monkeypatch.delattr(ekf.os, "getuid", raising=False)

        assert ekf.main(["extract-keep-flag.py", "myskill", '--keep "v"']) == 0
        assert sentinel.read_text(encoding="utf-8") == "v\n"

    @_skip_without_uids
    def test_created_state_directory_is_private(self, session: Path) -> None:
        """A ``--out-file`` state directory this script creates gets mode 0700, so other users cannot list it.

        ``mkdir(parents=True)`` would apply the default mode, leaving ``<skill>-state-<csid>`` world-readable in a
        shared temp dir.
        """
        state_dir = session / "myskill-state-sess1"

        ekf.main(["extract-keep-flag.py", "myskill", '--keep "v"', "--out-file", str(state_dir / "keep-items")])

        assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700

    @_skip_without_uids
    def test_existing_state_directory_keeps_its_mode(self, session: Path) -> None:
        """A directory that already exists is not re-moded; only directories this run creates are made private.

        The temp dir itself (``/tmp`` on Linux) belongs to root, so changing an existing directory's mode is never this
        script's call.
        """
        state_dir = session / "myskill-state-sess1"
        state_dir.mkdir(mode=0o755)
        state_dir.chmod(0o755)

        ekf.main(["extract-keep-flag.py", "myskill", '--keep "v"', "--out-file", str(state_dir / "keep-items")])

        assert stat.S_IMODE(state_dir.stat().st_mode) == 0o755
