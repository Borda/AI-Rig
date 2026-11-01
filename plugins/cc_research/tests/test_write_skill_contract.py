"""Tests for write_skill_contract bin script.

Covers the contract file's exact shape, argument validation, the owner stamp that binds a contract to the session that
wrote it, the atomic-write failure paths, and the fact that the file is written relative to the caller's working
directory. Every test runs in a temporary project directory that is the working directory for the call.
"""

from __future__ import annotations

import functools
import hashlib
import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path, PureWindowsPath

import pytest
import write_skill_contract as wsc

_ARGS = ["foundry:distill", "gap-analysis", ".reports/x", "run-dir=n/a", "Step 3 → Step 4"]
_BASE_LINES = [
    "## Active Skill Contract",
    "- skill: foundry:distill · phase: gap-analysis",
    "- run-dir: .reports/x",
    "- preserve: run-dir=n/a",
    "- next: Step 3 → Step 4",
]
_METADATA_ARGS = ["calls_ahead=60", "drop=temporary notes", "interactive=false"]
_METADATA_LINES = [
    "- gate-metadata:",
    "    - calls_ahead: 60",
    "    - drop: temporary notes",
    "    - interactive: false",
]
_PREVIOUS_CONTRACT = b"previous contract\n"
_PREVIOUS_OWNER = json.dumps(
    {"session_id": "test-session", "sha256": hashlib.sha256(_PREVIOUS_CONTRACT).hexdigest()}
).encode()
_NEW_CONTRACT_HEADER = b"## Active Skill Contract\n"

Injector = Callable[[pytest.MonkeyPatch, Path], None]


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Empty project directory that is the working directory, with no inherited session id."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    return tmp_path


@pytest.fixture
def session(project: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Set the session id that owner stamps carry and return it."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "test-session")
    return "test-session"


@pytest.fixture
def state(project: Path) -> Path:
    """Path of the (not yet created) ``.temp/state`` directory inside the project."""
    return project / ".temp" / "state"


@pytest.fixture
def prior(state: Path) -> Path:
    """State directory already holding a previous contract and the owner stamp that matches its digest."""
    state.mkdir(parents=True)
    (state / "skill-contract.md").write_bytes(_PREVIOUS_CONTRACT)
    (state / "skill-contract.owner").write_bytes(_PREVIOUS_OWNER)
    return state


def _lines(state: Path) -> list[str]:
    """Return the lines of the contract written under ``state``."""
    return (state / "skill-contract.md").read_text(encoding="utf-8").splitlines()


def _bytes_or_none(path: Path) -> bytes | None:
    """Return the bytes of ``path``, or None when it does not exist."""
    return path.read_bytes() if path.exists() else None


def _owner_session(path: Path) -> str | None:
    """Return the session id of the owner stamp at ``path``, or None when no stamp exists."""
    return json.loads(path.read_bytes())["session_id"] if path.exists() else None


def _exit_code(argv: list[str]) -> object:
    """Return the status ``wsc.main`` ends with, whether it returns it or exits with it (``--help`` exits)."""
    try:
        return wsc.main(argv)
    except SystemExit as exc:
        return exc.code


def _reject_owner_unlink(monkeypatch: pytest.MonkeyPatch, state: Path) -> None:
    """Make removing the owner stamp fail, so invalidation is rejected before any replacement can occur."""
    unlink = Path.unlink

    def checked_unlink(path: Path, missing_ok: bool = False) -> None:
        """Reject the owner stamp's removal; delegate every other path."""
        if path.name == "skill-contract.owner":
            raise OSError("owner locked")
        unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", checked_unlink)


def _reject_replace(monkeypatch: pytest.MonkeyPatch, state: Path, name: str) -> None:
    """Make the atomic commit of ``name`` fail after its complete temporary bytes are staged beside it."""
    rejected = (state / name).resolve()
    replace = wsc.os.replace

    def checked_replace(source: str | Path, destination: str | Path) -> None:
        """Check the staged file sits beside its target and holds bytes, then reject or delegate the commit."""
        target = Path(destination).resolve()
        assert Path(source).parent == target.parent
        assert Path(source).read_bytes()
        if target == rejected:
            raise OSError("replace unavailable")
        replace(source, destination)

    monkeypatch.setattr(wsc.os, "replace", checked_replace)


def _reject_temporary(monkeypatch: pytest.MonkeyPatch, state: Path) -> None:
    """Make creating the temporary file fail without leaving a partial artifact."""

    def blocked_temporary(**kwargs: object) -> None:
        """Reject temporary-file creation."""
        raise OSError("temporary unavailable")

    monkeypatch.setattr(wsc.tempfile, "NamedTemporaryFile", blocked_temporary)


class TestContractFile:
    """The written contract file: its shape, location and replacement."""

    def test_writes_all_five_fields(self, state: Path) -> None:
        """Each argument lands on its own labelled line, in order.

        A plain five-argument call is the contract the PreCompact hook appends verbatim, so the exact lines matter.
        """
        assert wsc.main(_ARGS) == 0
        assert _lines(state) == _BASE_LINES

    @pytest.mark.parametrize(
        ("list_args", "extra_lines"),
        [
            pytest.param(
                ["probed (do NOT re-probe)", "hypothesis A :: Ruled-out\nhypothesis B :: Confirmed"],
                ["- probed (do NOT re-probe):", "    - hypothesis A :: Ruled-out", "    - hypothesis B :: Confirmed"],
                id="labelled-list-appended",
            ),
            pytest.param(["tried", "only one\n\n"], ["- tried:", "    - only one"], id="blank-lines-dropped"),
            pytest.param(["tried", ""], [], id="empty-list-omits-block"),
        ],
    )
    def test_renders_list_argument(self, state: Path, list_args: list[str], extra_lines: list[str]) -> None:
        """The 7-argument form appends the ledger a ``next:`` string refers to, one indented bullet per line.

        Without the ledger a post-compaction resume is told to skip candidates "above" that were never written, and re-
        probes what it already ruled out. Blank lines, such as a trailing newline from a ``head``/``tail`` capture, emit
        no bullet, and an empty ledger writes no label line, so callers need not branch.
        """
        assert wsc.main([*_ARGS, *list_args]) == 0
        assert _lines(state) == [*_BASE_LINES, *extra_lines]

    def test_creates_parent_directories(self, state: Path) -> None:
        """``.temp/state/`` is created when absent.

        A fresh project has no ``.temp`` yet, and the first compaction boundary must still be able to write.
        """
        wsc.main(_ARGS)
        assert state.is_dir()

    def test_overwrites_previous_contract(self, state: Path) -> None:
        """A later boundary replaces the earlier contract rather than appending.

        Only the newest boundary is resumable, so a second write for another skill must leave nothing of the first.
        """
        wsc.main(_ARGS)
        wsc.main(["foundry:audit", "step-6", "n/a", "p", "next"])
        text = "\n".join(_lines(state))
        assert "foundry:distill" not in text
        assert "foundry:audit" in text

    def test_written_relative_to_cwd(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The contract belongs to the project the skill runs in, not the script's location.

        The call runs from a subdirectory of the temporary directory; the contract must appear under that subdirectory
        and nowhere above it.
        """
        project = tmp_path / "project"
        project.mkdir()
        monkeypatch.chdir(project)
        wsc.main(_ARGS)
        assert (project / ".temp" / "state" / "skill-contract.md").is_file()
        assert not (tmp_path / ".temp").exists()

    def test_keeps_placeholder_tokens_verbatim(self, state: Path) -> None:
        """``<REFINE_ITER>``-style fill-in tokens are written through untouched.

        Callers pass literal placeholders that the orchestrator substitutes as prose, so the script must not expand or
        strip angle-bracket tokens.
        """
        wsc.main(["s", "iter <REFINE_ITER>/<MAX>", "n/a", "p", "n"])
        assert "- skill: s · phase: iter <REFINE_ITER>/<MAX>" in _lines(state)


class TestArgumentValidation:
    """The argument contract: arity, empty skill, help text and reserved lines."""

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param([], id="no-args"),
            pytest.param(["only-one"], id="one-arg"),
            pytest.param(["a", "b", "c", "d"], id="four-args"),
            pytest.param(["a", "b", "c", "d", "e", "f"], id="six-args"),
            pytest.param(["", "phase", "dir", "preserve", "next"], id="empty-skill"),
        ],
    )
    def test_usage_error_exits_two_and_writes_nothing(self, project: Path, argv: list[str]) -> None:
        """Anything but five or seven arguments, or an empty skill, exits 2 and leaves no partial contract behind.

        An empty skill name would produce a contract no hook could attribute. A rejected invocation must not create even
        the state directory.
        """
        assert wsc.main(argv) == 2
        assert not (project / ".temp").exists()

    def test_help_exits_zero_with_usage(self, project: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """``--help`` prints usage and exits 0, per the bin/ help contract.

        The flag must be honoured even though the script avoids argparse, which would misread flag-like values.
        """
        with pytest.raises(SystemExit) as exc:
            wsc.main(["--help"])
        assert exc.value.code == 0
        assert "usage:" in capsys.readouterr().out

    @pytest.mark.parametrize("position", range(7))
    def test_rejects_reserved_sentinel_line(self, project: Path, position: int) -> None:
        """Every positional field rejects a stripped reserved sentinel line.

        A field that smuggles a ``- gate-metadata:`` line, even padded with spaces, could forge the dedicated gate
        block, so it exits 2 and writes nothing, whichever of the seven positions carries it.
        """
        args = [*_ARGS, "retained", "item"]
        args[position] += "\n  - gate-metadata:  \n"
        assert wsc.main(args) == 2
        assert not wsc._CONTRACT.exists()

    def test_reserved_label_cannot_forge_metadata(self, project: Path) -> None:
        """A labelled list cannot render the dedicated gate block from positional prose.

        Using ``gate-metadata`` as the list label would print a ``- gate-metadata:`` line indistinguishable from the
        real metadata block, so the call exits 2 and writes nothing.
        """
        assert wsc.main([*_ARGS, "gate-metadata", "calls_ahead: 99"]) == 2
        assert not wsc._CONTRACT.exists()


class TestOwnerStamp:
    """Bind ownership to the written bytes and invalidate an earlier stamp before writing."""

    @pytest.mark.parametrize("session_id", ["test-session", ""])
    def test_contract_is_lf_terminated(self, state: Path, monkeypatch: pytest.MonkeyPatch, session_id: str) -> None:
        """The contract is written with LF line endings and a final newline, with or without a session.

        The owner digest covers the exact bytes, so CRLF translation on Windows would break the verification.
        """
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", session_id)
        assert wsc.main(_ARGS) == 0
        raw = (state / "skill-contract.md").read_bytes()
        assert raw.endswith(b"\n")
        assert b"\r\n" not in raw

    def test_owner_binds_session_and_digest(self, state: Path, session: str) -> None:
        """A session environment produces an owner holding the session id and the digest of the exact bytes.

        The stamp is what lets a resumed session tell its own contract from a stale or foreign one.
        """
        assert wsc.main(_ARGS) == 0
        raw = (state / "skill-contract.md").read_bytes()
        owner = json.loads((state / "skill-contract.owner").read_bytes())
        assert set(owner) == {"session_id", "sha256", "written_at"}
        assert owner["session_id"] == session
        assert owner["sha256"] == hashlib.sha256(raw).hexdigest()
        assert datetime.fromisoformat(owner["written_at"]).tzinfo is not None

    def test_empty_session_writes_no_owner(self, state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """An empty session id writes the contract but no owner stamp.

        A contract with no owning session stays advisory; stamping it with an empty id would claim ownership.
        """
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "")
        assert wsc.main(_ARGS) == 0
        assert not (state / "skill-contract.owner").exists()

    @pytest.mark.parametrize(
        ("error", "exit_code", "owner_session"),
        [
            pytest.param(None, 0, "test-session", id="write-succeeds"),
            pytest.param(OSError("contract unavailable"), 1, None, id="write-fails"),
        ],
    )
    def test_stale_owner_removed_before_write(
        self,
        prior: Path,
        session: str,
        monkeypatch: pytest.MonkeyPatch,
        error: OSError | None,
        exit_code: int,
        owner_session: str | None,
    ) -> None:
        """A stale owner must already be absent when the contract write starts, including when the write fails.

        The commit of the new contract is observed: at that moment the previous owner stamp must be gone, otherwise a
        crash between the two writes would leave an old stamp vouching for new bytes. A failed write ends with no
        stamp; a successful one ends with the new session's stamp.
        """
        owner = prior / "skill-contract.owner"
        replace = wsc.os.replace

        def checked_replace(source: str | Path, destination: str | Path) -> None:
            """Assert invalidation already happened at the contract commit, optionally rejecting that commit."""
            if Path(destination).name == "skill-contract.md":
                assert not owner.exists()
                if error is not None:
                    raise error
            replace(source, destination)

        monkeypatch.setattr(wsc.os, "replace", checked_replace)
        assert wsc.main(_ARGS) == exit_code
        assert not list(prior.glob(".*"))
        assert _owner_session(owner) == owner_session

    def test_unset_session_removes_existing_owner(self, prior: Path) -> None:
        """An unstamped write cannot inherit the previous session's owner.

        With no session id in the environment the new contract is unowned, so the earlier stamp must be removed rather
        than left pointing at bytes it no longer describes.
        """
        assert wsc.main(_ARGS) == 0
        assert (prior / "skill-contract.md").read_bytes().startswith(_NEW_CONTRACT_HEADER)
        assert not (prior / "skill-contract.owner").exists()

    def test_windows_style_working_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, session: str
    ) -> None:
        """Native CWD components and serialized Windows paths survive without separator conversion.

        The working directory contains a space and the run-dir argument is a Windows-absolute path; the path must be
        written verbatim and hashed as written, whatever host runs the test.
        """
        project = tmp_path / "Windows workspace" / "project"
        project.mkdir(parents=True)
        monkeypatch.chdir(project)
        run_dir = str(PureWindowsPath("C:/work/project/reports/run"))
        assert wsc.main(["sample:skill", "boundary", run_dir, "preserve", "next"]) == 0
        state = project / ".temp" / "state"
        raw = (state / "skill-contract.md").read_bytes()
        assert f"- run-dir: {run_dir}\n".encode() in raw
        assert json.loads((state / "skill-contract.owner").read_bytes())["sha256"] == hashlib.sha256(raw).hexdigest()
        assert not (tmp_path / ".temp").exists()

    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            pytest.param([], 2, id="invalid-arity"),
            pytest.param(["", "phase", "dir", "preserve", "next"], 2, id="empty-skill"),
            pytest.param(["--help"], 0, id="help"),
        ],
    )
    def test_validation_exit_invalidates_prior_owner(self, prior: Path, argv: list[str], expected: int) -> None:
        """Every invocation invalidates earlier ownership, even when no new contract is written.

        The stamp is removed before arguments are validated, so a usage error or ``--help`` leaves the previous contract
        in place but no longer vouched for.
        """
        assert _exit_code(argv) == expected
        assert not (prior / "skill-contract.owner").exists()
        assert (prior / "skill-contract.md").read_bytes() == _PREVIOUS_CONTRACT


class TestAtomicWrite:
    """Failures while writing leave complete old bytes, no temporary files and no valid-looking owner."""

    @pytest.mark.parametrize(
        ("inject", "owner_after"),
        [
            pytest.param(_reject_owner_unlink, _PREVIOUS_OWNER, id="owner-unlink-rejected"),
            pytest.param(
                functools.partial(_reject_replace, name="skill-contract.md"), None, id="contract-commit-rejected"
            ),
            pytest.param(_reject_temporary, None, id="temporary-file-rejected"),
        ],
    )
    def test_failure_keeps_previous_contract(
        self, prior: Path, session: str, monkeypatch: pytest.MonkeyPatch, inject: Injector, owner_after: bytes | None
    ) -> None:
        """A failure before the new contract is committed exits 1 and keeps the previous contract bytes whole.

        If the owner stamp cannot be removed, nothing else is touched and the old stamp stays beside the old contract.
        Once invalidation succeeded, a rejected commit or temporary file leaves the old contract with no owner. Either
        way no temporary file is left behind.
        """
        inject(monkeypatch, prior)
        assert wsc.main(_ARGS) == 1
        assert not list(prior.glob(".*"))
        assert (prior / "skill-contract.md").read_bytes() == _PREVIOUS_CONTRACT
        assert _bytes_or_none(prior / "skill-contract.owner") == owner_after

    def test_stamp_failure_leaves_no_owner(self, prior: Path, session: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """If only the owner stamp cannot be committed, the new contract stands but nothing vouches for it.

        The contract commit succeeded and the stamp commit was rejected, so the run reports failure and must not leave a
        stamp, stale or partial, that could vouch for the new bytes.
        """
        _reject_replace(monkeypatch, prior, "skill-contract.owner")
        assert wsc.main(_ARGS) == 1
        assert not list(prior.glob(".*"))
        assert not (prior / "skill-contract.owner").exists()
        assert (prior / "skill-contract.md").read_bytes().startswith(_NEW_CONTRACT_HEADER)

    def test_owner_hashes_intended_bytes_after_overwrite(
        self, state: Path, session: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An intervening writer cannot make this session own its replacement bytes.

        Foreign bytes are written over the contract right after both a direct write and the atomic commit. The stamp
        must carry the digest of the bytes this call meant to write, so it never matches the foreign content.
        """
        original_write, original_replace = Path.write_bytes, wsc.os.replace
        intended = []

        def overwrite(path: Path, data: bytes) -> int:
            """Simulate replacement at the old direct-write boundary."""
            result = original_write(path, data)
            if path == wsc._CONTRACT:
                intended.append(data)
                original_write(path, b"foreign replacement\n")
            return result

        def replace(source: str | Path, destination: str | Path) -> None:
            """Simulate replacement immediately after the atomic contract commit."""
            target = Path(destination)
            if target == wsc._CONTRACT:
                intended.append(Path(source).read_bytes())
            original_replace(source, destination)
            if target == wsc._CONTRACT:
                original_write(target, b"foreign replacement\n")

        monkeypatch.setattr(Path, "write_bytes", overwrite)
        monkeypatch.setattr(wsc.os, "replace", replace)
        assert wsc.main(_ARGS) == 0
        owner = json.loads(wsc._OWNER.read_bytes())
        assert owner["sha256"] == hashlib.sha256(intended[0]).hexdigest()
        assert owner["sha256"] != hashlib.sha256(wsc._CONTRACT.read_bytes()).hexdigest()

    def test_partial_temporary_write_cleaned(self, prior: Path, session: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """An interrupted temporary write leaves complete old bytes and removes its partial file.

        The real temporary file receives only a ten-byte prefix before the write raises, which is the half-written state
        an interrupted process would leave behind.
        """
        create = wsc.tempfile.NamedTemporaryFile

        def interrupted_temporary(**kwargs: object) -> object:
            """Interrupt the real temporary file after writing a partial prefix."""
            handle = create(**kwargs)
            write = handle.write

            def interrupted_write(raw: bytes) -> None:
                """Leave partial bytes in the temporary file before reporting the I/O failure."""
                write(raw[:10])
                raise OSError("write interrupted")

            handle.write = interrupted_write
            return handle

        monkeypatch.setattr(wsc.tempfile, "NamedTemporaryFile", interrupted_temporary)
        assert wsc.main(_ARGS) == 1
        assert (prior / "skill-contract.md").read_bytes() == _PREVIOUS_CONTRACT
        assert not (prior / "skill-contract.owner").exists()
        assert not list(prior.glob(".*"))


class TestBoundaryMetadata:
    """Validate optional metadata without changing positional or ownership contracts."""

    @pytest.mark.parametrize(
        ("positional", "expected_tail"),
        [
            pytest.param(_ARGS, _METADATA_LINES, id="without-list"),
            pytest.param(
                [*_ARGS, "retained", "one item"],
                ["- retained:", "    - one item", *_METADATA_LINES],
                id="after-labelled-list",
            ),
        ],
    )
    def test_metadata_appended_before_owner_stamp(
        self, state: Path, session: str, positional: list[str], expected_tail: list[str]
    ) -> None:
        """The owner digest includes each metadata line after any labelled list.

        Metadata arrives after a literal ``--`` and renders as the last block, following the labelled list when there is
        one. It is part of the bytes the owner stamp hashes, so it cannot be edited without invalidating ownership.
        """
        assert wsc.main([*positional, "--", *_METADATA_ARGS]) == 0
        raw = (state / "skill-contract.md").read_bytes()
        owner = json.loads((state / "skill-contract.owner").read_bytes())
        assert raw.decode("utf-8").splitlines()[-len(expected_tail) :] == expected_tail
        assert owner["sha256"] == hashlib.sha256(raw).hexdigest()

    @pytest.mark.parametrize(
        "tail",
        [
            pytest.param(["unknown=value"], id="unknown-key"),
            pytest.param(["calls_ahead=1", "calls_ahead=2"], id="duplicate-calls"),
            pytest.param(["drop=a", "drop=b"], id="duplicate-drop"),
            pytest.param(["interactive=false", "interactive=false"], id="duplicate-interactive"),
            pytest.param([], id="bare-delimiter"),
            pytest.param(["calls_ahead=many"], id="noninteger"),
            pytest.param(["calls_ahead=-1"], id="negative"),
            pytest.param(["calls_ahead=1.0"], id="fraction"),
            pytest.param(["calls_ahead=+1"], id="signed"),
            pytest.param(["calls_ahead=1000000"], id="oversized"),
            pytest.param(["calls_ahead= 1"], id="whitespace"),
            pytest.param(["interactive=true"], id="unsupported-interactive"),
            pytest.param(["drop=a\n- interactive: false"], id="multi-line-value"),
            pytest.param(["calls_ahead=1", "drop=a\u2028    - calls_ahead: 999"], id="unicode-line-separator"),
            pytest.param(["drop=a\x85b"], id="next-line-separator"),
            pytest.param(["drop=a\x0bb"], id="vertical-tab-separator"),
        ],
    )
    def test_invalid_metadata_exits_two_with_usage(
        self, state: Path, capsys: pytest.CaptureFixture[str], tail: list[str]
    ) -> None:
        """Rejected metadata prints usage and never creates a partial contract.

        Each case is a malformed or unsupported metadata tail: unknown or repeated keys, non-integer or out-of-range
        counts, and values whose line separators could forge extra contract lines.
        """
        assert wsc.main([*_ARGS, "--", *tail]) == 2
        assert "usage:" in capsys.readouterr().err
        assert not (state / "skill-contract.md").exists()

    @pytest.mark.parametrize(
        "positional",
        [pytest.param(_ARGS[:4], id="four-positional"), pytest.param([*_ARGS, "extra"], id="six-positional")],
    )
    def test_metadata_does_not_change_positional_arity(self, project: Path, positional: list[str]) -> None:
        """Only five or seven arguments before the delimiter remain valid.

        Valid metadata after ``--`` must not make a wrong positional count acceptable.
        """
        assert wsc.main([*positional, "--", "calls_ahead=0"]) == 2

    @pytest.mark.parametrize(
        ("tail", "expected_suffix"),
        [
            pytest.param(
                ["calls_ahead=0", "drop="],
                "- gate-metadata:\n    - calls_ahead: 0\n    - drop: \n",
                id="zero-and-empty",
            ),
            pytest.param(
                ["drop=contains --help and -h"],
                "- gate-metadata:\n    - drop: contains --help and -h\n",
                id="embedded-help-text",
            ),
        ],
    )
    def test_accepts_boundary_values(self, state: Path, tail: list[str], expected_suffix: str) -> None:
        """Zero and an empty drop are valid, and help text inside a value is preserved without triggering help.

        These are the edges of the accepted grammar: ``calls_ahead=0`` is a non-negative integer, ``drop=`` is an
        empty value, and a value that merely contains ``--help`` is data, not the help flag.
        """
        assert wsc.main([*_ARGS, "--", *tail]) == 0
        assert (state / "skill-contract.md").read_text(encoding="utf-8").endswith(expected_suffix)
