"""Tests for write_skill_contract bin script.

Covers the contract file's exact shape, argument validation, the owner stamp that binds a contract to a session, and the
fact that the file is written relative to the caller's working directory.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path, PureWindowsPath

import pytest
import write_skill_contract as wsc

_ARGS = ["foundry:distill", "gap-analysis", ".reports/x", "run-dir=n/a", "Step 3 → Step 4"]
_PREVIOUS_CONTRACT = b"previous contract\n"
_CONTRACT_HEADER = b"## Active Skill Contract\n"
_SESSION = "test-session"


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Empty working directory with no ambient session id, so only tests that opt in stamp an owner."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    return tmp_path


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch) -> str:
    """Set a nonempty session id in the environment and return it."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _SESSION)
    return _SESSION


@pytest.fixture
def state(workdir: Path) -> Path:
    """Create and return the ``.temp/state`` directory under the working directory."""
    directory = workdir / ".temp" / "state"
    directory.mkdir(parents=True)
    return directory


@pytest.fixture
def stale_owner(state: Path, session: str) -> Path:
    """Write a leftover owner stamp from an earlier session before the script runs, and return its path."""
    owner = state / "skill-contract.owner"
    owner.write_text('"stale"', encoding="utf-8")
    return owner


def _contract(workdir: Path) -> str:
    """Return the written contract text."""
    return (workdir / ".temp" / "state" / "skill-contract.md").read_text(encoding="utf-8")


def _read_or_none(path: Path) -> bytes | None:
    """Return the file's bytes, or ``None`` when it does not exist."""
    return path.read_bytes() if path.exists() else None


def _exit_code(argv: list[str]) -> int | str | None:
    """Return ``main``'s result whether it returns it or exits through ``SystemExit``."""
    try:
        return wsc.main(argv)
    except SystemExit as exc:
        return exc.code


def _replace_guard(owner: Path, write_fails: bool) -> Callable[[str | Path, str | Path], None]:
    """Build an ``os.replace`` wrapper asserting the owner is gone when the contract commits, optionally failing it."""
    replace = wsc.os.replace

    def guarded(source: str | Path, destination: str | Path) -> None:
        """Check invalidation at atomic replacement, optionally rejecting the contract."""
        if Path(destination).name == "skill-contract.md":
            assert not owner.exists()
            if write_fails:
                raise OSError("contract unavailable")
        replace(source, destination)

    return guarded


def _checked_replace(fail_for: Path | None) -> Callable[[str | Path, str | Path], None]:
    """Build an ``os.replace`` wrapper that demands complete same-directory temp bytes and fails for ``fail_for``."""
    replace = wsc.os.replace

    def checked(source: str | Path, destination: str | Path) -> None:
        """Reject an atomic commit to ``fail_for`` after its complete temporary bytes are present."""
        target = Path(destination).resolve()
        assert Path(source).parent == target.parent
        assert Path(source).read_bytes()
        if target == fail_for:
            raise OSError("replace unavailable")
        replace(source, destination)

    return checked


def _fail_owner_unlink(monkeypatch: pytest.MonkeyPatch, state: Path) -> None:
    """Make invalidating the old owner stamp fail before any replacement can occur."""
    unlink = Path.unlink

    def checked_unlink(path: Path, missing_ok: bool = False) -> None:
        """Reject removal of the owner stamp; delegate every other removal."""
        if path.name == "skill-contract.owner":
            raise OSError("owner locked")
        unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", checked_unlink)
    monkeypatch.setattr(wsc.os, "replace", _checked_replace(None))


def _fail_contract_replace(monkeypatch: pytest.MonkeyPatch, state: Path) -> None:
    """Make the atomic commit of the contract file fail."""
    monkeypatch.setattr(wsc.os, "replace", _checked_replace(state / "skill-contract.md"))


def _fail_owner_replace(monkeypatch: pytest.MonkeyPatch, state: Path) -> None:
    """Make the atomic commit of the owner stamp fail after the contract committed."""
    monkeypatch.setattr(wsc.os, "replace", _checked_replace(state / "skill-contract.owner"))


def _fail_temporary_file(monkeypatch: pytest.MonkeyPatch, state: Path) -> None:
    """Make temporary-file creation fail without leaving a partial artifact."""

    def blocked_temporary(**kwargs: object) -> None:
        """Reject temporary-file creation."""
        raise OSError("temporary unavailable")

    monkeypatch.setattr(wsc.os, "replace", _checked_replace(None))
    monkeypatch.setattr(wsc.tempfile, "NamedTemporaryFile", blocked_temporary)


class TestContractFile:
    """The written contract file."""

    def test_writes_all_five_fields(self, workdir: Path) -> None:
        """Each argument lands on its own labelled line, in order."""
        assert wsc.main(_ARGS) == 0
        assert _contract(workdir).splitlines() == [
            "## Active Skill Contract",
            "- skill: foundry:distill · phase: gap-analysis",
            "- run-dir: .reports/x",
            "- preserve: run-dir=n/a",
            "- next: Step 3 → Step 4",
        ]

    @pytest.mark.parametrize(
        ("label", "items", "expected_tail"),
        [
            # without the ledger a post-compaction resume is told to skip candidates "above" that were never written,
            # and re-probes what it already ruled out
            pytest.param(
                "probed (do NOT re-probe)",
                "hypothesis A :: Ruled-out\nhypothesis B :: Confirmed",
                ["- probed (do NOT re-probe):", "    - hypothesis A :: Ruled-out", "    - hypothesis B :: Confirmed"],
                id="labelled-list-appended",
            ),
            # a trailing newline from a `head`/`tail` capture does not emit an empty bullet
            pytest.param("tried", "only one\n\n", ["- tried:", "    - only one"], id="blank-list-lines-dropped"),
        ],
    )
    def test_labelled_list_appended(self, workdir: Path, label: str, items: str, expected_tail: list[str]) -> None:
        """The 7-argument form appends the labelled ledger a `next:` string refers to, one bullet per non-blank line.

        Without the ledger a post-compaction resume is told to skip candidates "above" that were never written. Blank
        lines, such as the trailing newline of a `head`/`tail` capture, never become empty bullets.
        """
        assert wsc.main([*_ARGS, label, items]) == 0
        assert _contract(workdir).splitlines()[-len(expected_tail) :] == expected_tail

    def test_empty_list_omits_block(self, workdir: Path) -> None:
        """An empty ledger produces no label line, so callers need not branch."""
        assert wsc.main([*_ARGS, "tried", ""]) == 0
        text = _contract(workdir)
        assert "tried" not in text
        assert text.splitlines()[-1].startswith("- next:")

    def test_creates_parent_directories(self, workdir: Path) -> None:
        """`.temp/state/` is created when absent."""
        wsc.main(_ARGS)
        assert (workdir / ".temp" / "state").is_dir()

    def test_overwrites_previous_contract(self, workdir: Path) -> None:
        """A later boundary replaces the earlier contract rather than appending."""
        wsc.main(_ARGS)
        wsc.main(["foundry:audit", "step-6", "n/a", "p", "next"])
        text = _contract(workdir)
        assert "foundry:distill" not in text
        assert "foundry:audit" in text

    def test_written_relative_to_cwd(self, workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The contract belongs to the project the skill runs in, not the script's location."""
        project = workdir / "project"
        project.mkdir()
        monkeypatch.chdir(project)
        wsc.main(_ARGS)
        assert (project / ".temp" / "state" / "skill-contract.md").is_file()
        assert not (workdir / ".temp").exists()

    def test_placeholder_tokens_survive_verbatim(self, workdir: Path) -> None:
        """`<REFINE_ITER>`-style fill-in tokens are written through untouched."""
        wsc.main(["s", "iter <REFINE_ITER>/<MAX>", "n/a", "p", "n"])
        assert "iter <REFINE_ITER>/<MAX>" in _contract(workdir)


class TestArgumentValidation:
    """The argument contract."""

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param([], id="no-args"),
            pytest.param(["a", "b", "c", "d"], id="four-args"),
            pytest.param(["a", "b", "c", "d", "e", "f"], id="six-args"),
            pytest.param(["", "phase", "dir", "preserve", "next"], id="empty-skill"),
        ],
    )
    def test_rejected_arguments_exit_two(self, workdir: Path, argv: list[str]) -> None:
        """Anything but five or seven arguments is a usage error, and an empty skill name is too.

        An empty skill name would produce a contract no hook could attribute to a skill.
        """
        assert wsc.main(argv) == 2

    def test_help_exits_zero_with_usage(self, workdir: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """--help prints usage and exits 0, per the bin/ help contract."""
        with pytest.raises(SystemExit) as exc:
            wsc.main(["--help"])
        assert exc.value.code == 0
        assert "usage:" in capsys.readouterr().out

    def test_no_file_written_on_usage_error(self, workdir: Path) -> None:
        """A rejected invocation leaves no partial contract behind."""
        wsc.main(["only-one"])
        assert not (workdir / ".temp").exists()


class TestOwnerStamp:
    """Bind ownership to the written bytes and invalidate an earlier stamp before writing."""

    def test_owner_matches_raw_bytes(self, workdir: Path, session: str) -> None:
        """With a session id the owner stamp holds the session and the digest of the exact LF bytes written.

        The digest lets a later hook prove the contract was not replaced by another writer after this session wrote it.
        """
        assert wsc.main(_ARGS) == 0
        state = workdir / ".temp" / "state"
        raw = (state / "skill-contract.md").read_bytes()
        owner = json.loads((state / "skill-contract.owner").read_bytes())
        assert raw.endswith(b"\n")
        assert b"\r\n" not in raw
        assert set(owner) == {"session_id", "sha256", "written_at"}
        assert owner["session_id"] == session
        assert owner["sha256"] == hashlib.sha256(raw).hexdigest()
        assert datetime.fromisoformat(owner["written_at"]).tzinfo is not None

    def test_empty_session_writes_no_owner(self, workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """An empty session id yields a contract in LF bytes but no owner stamp.

        An unattributable contract must stay advisory, so no owner may claim it.
        """
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "")
        assert wsc.main(_ARGS) == 0
        state = workdir / ".temp" / "state"
        raw = (state / "skill-contract.md").read_bytes()
        assert (raw.endswith(b"\n"), b"\r\n" in raw, (state / "skill-contract.owner").exists()) == (True, False, False)

    def test_existing_owner_removed_before_write(
        self, stale_owner: Path, state: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stale owner must already be absent when the contract write commits, so the new stamp replaces it.

        If the old owner were still present at commit time, a crash between commit and stamp would leave the previous
        session owning bytes it never wrote.
        """
        monkeypatch.setattr(wsc.os, "replace", _replace_guard(stale_owner, write_fails=False))
        assert wsc.main(_ARGS) == 0
        assert not list(state.glob(".*"))
        assert json.loads(stale_owner.read_bytes())["session_id"] == _SESSION

    def test_existing_owner_stays_removed_when_write_fails(
        self, stale_owner: Path, state: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stale owner is already gone when the contract write fails, and the failure exits 1 without temp debris."""
        monkeypatch.setattr(wsc.os, "replace", _replace_guard(stale_owner, write_fails=True))
        assert wsc.main(_ARGS) == 1
        assert not list(state.glob(".*"))
        assert not stale_owner.exists()

    def test_unset_session_removes_existing_owner(self, workdir: Path, state: Path) -> None:
        """An unstamped write cannot inherit the previous session's owner."""
        (state / "skill-contract.owner").write_text('"stale"', encoding="utf-8")
        assert wsc.main(_ARGS) == 0
        assert (state / "skill-contract.md").is_file()
        assert not (state / "skill-contract.owner").exists()

    def test_windows_style_working_directory(
        self, workdir: Path, session: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Native CWD components and serialized Windows paths survive without separator conversion.

        The run-dir field carries a Windows path as plain text; it must be written and hashed exactly as given.
        """
        project = workdir / "Windows workspace" / "project"
        project.mkdir(parents=True)
        monkeypatch.chdir(project)
        run_dir = str(PureWindowsPath("C:/work/project/reports/run"))
        assert wsc.main(["sample:skill", "boundary", run_dir, "preserve", "next"]) == 0
        state = project / ".temp" / "state"
        raw = (state / "skill-contract.md").read_bytes()
        assert f"- run-dir: {run_dir}\n".encode() in raw
        assert json.loads((state / "skill-contract.owner").read_bytes())["sha256"] == hashlib.sha256(raw).hexdigest()
        assert not (workdir / ".temp").exists()

    @pytest.mark.parametrize(
        ("inject_failure", "contract_kept", "expected_owner"),
        [
            pytest.param(_fail_owner_unlink, True, b"previous owner\n", id="unlink"),
            pytest.param(_fail_contract_replace, True, None, id="contract"),
            pytest.param(_fail_owner_replace, False, None, id="stamp"),
            pytest.param(_fail_temporary_file, True, None, id="temporary"),
        ],
    )
    def test_failed_boundary_cannot_retain_owner(
        self,
        state: Path,
        session: str,
        monkeypatch: pytest.MonkeyPatch,
        inject_failure: Callable[[pytest.MonkeyPatch, Path], None],
        contract_kept: bool,
        expected_owner: bytes | None,
    ) -> None:
        """Invalidation and atomic-write failures preserve complete bytes, drop a stale owner and leave no temp files.

        Only a failure to remove the old owner keeps it; every later failure must leave no stamp, so a previous session
        can never own bytes it did not write. A failure after the contract committed keeps the new complete contract.
        """
        contract, owner = state / "skill-contract.md", state / "skill-contract.owner"
        contract.write_bytes(_PREVIOUS_CONTRACT)
        owner.write_bytes(b"previous owner\n")
        inject_failure(monkeypatch, state)
        assert wsc.main(_ARGS) == 1
        contract_bytes = contract.read_bytes()
        assert not list(state.glob(".*"))
        assert _read_or_none(owner) == expected_owner
        assert (contract_bytes == _PREVIOUS_CONTRACT) is contract_kept
        assert contract_bytes.startswith(_PREVIOUS_CONTRACT if contract_kept else _CONTRACT_HEADER)

    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            pytest.param([], 2, id="invalid-arity"),
            pytest.param(["", "phase", "dir", "preserve", "next"], 2, id="empty-skill"),
            pytest.param(["--help"], 0, id="help"),
        ],
    )
    def test_validation_exit_invalidates_prior_owner(self, state: Path, argv: list[str], expected: int) -> None:
        """Every invocation invalidates earlier ownership, even when no new contract is written."""
        contract, owner = state / "skill-contract.md", state / "skill-contract.owner"
        contract.write_bytes(_PREVIOUS_CONTRACT)
        owner.write_text(
            json.dumps({"session_id": _SESSION, "sha256": hashlib.sha256(contract.read_bytes()).hexdigest()}),
            encoding="utf-8",
        )
        assert _exit_code(argv) == expected
        assert not owner.exists()
        assert contract.read_bytes() == _PREVIOUS_CONTRACT


class TestBoundaryMetadata:
    """Optional metadata after ``--`` is validated without changing the positional or ownership contracts."""

    @pytest.mark.parametrize(
        ("positional", "expected_context"),
        [
            pytest.param(_ARGS, "- next: Step 3 → Step 4\n- gate-metadata:\n    - calls_ahead:", id="after-next"),
            pytest.param(
                [*_ARGS, "retained", "one item"],
                "- retained:\n    - one item\n- gate-metadata:\n    - calls_ahead:",
                id="after-labelled-list",
            ),
        ],
    )
    def test_metadata_appended_before_owner_stamp(
        self, workdir: Path, session: str, positional: list[str], expected_context: str
    ) -> None:
        """The owner digest includes each metadata line, which follows the labelled list when there is one."""
        assert wsc.main([*positional, "--", "calls_ahead=60", "drop=temporary notes", "interactive=false"]) == 0
        state = workdir / ".temp" / "state"
        raw = (state / "skill-contract.md").read_bytes()
        assert raw.decode("utf-8").splitlines()[-4:] == [
            "- gate-metadata:",
            "    - calls_ahead: 60",
            "    - drop: temporary notes",
            "    - interactive: false",
        ]
        assert expected_context in raw.decode("utf-8")
        assert json.loads((state / "skill-contract.owner").read_bytes())["sha256"] == hashlib.sha256(raw).hexdigest()

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
        self, workdir: Path, capsys: pytest.CaptureFixture[str], tail: list[str]
    ) -> None:
        """Rejected metadata prints usage and never creates a partial contract."""
        assert wsc.main([*_ARGS, "--", *tail]) == 2
        assert "usage:" in capsys.readouterr().err
        assert not (workdir / ".temp" / "state" / "skill-contract.md").exists()

    @pytest.mark.parametrize(
        "positional",
        [pytest.param(_ARGS[:4], id="four-positional"), pytest.param([*_ARGS, "extra"], id="six-positional")],
    )
    def test_metadata_does_not_change_positional_arity(self, workdir: Path, positional: list[str]) -> None:
        """Only five or seven arguments before the delimiter remain valid."""
        assert wsc.main([*positional, "--", "calls_ahead=0"]) == 2

    def test_zero_calls_and_empty_drop_are_valid(self, workdir: Path) -> None:
        """Zero is a valid ``calls_ahead`` and an empty ``drop`` is written as an empty value."""
        assert wsc.main([*_ARGS, "--", "calls_ahead=0", "drop="]) == 0
        assert _contract(workdir).endswith("- gate-metadata:\n    - calls_ahead: 0\n    - drop: \n")

    def test_embedded_help_text_is_preserved(self, workdir: Path) -> None:
        """Help flags inside a metadata value are data: they are written through without triggering help."""
        assert wsc.main([*_ARGS, "--", "drop=contains --help and -h"]) == 0
        assert _contract(workdir).endswith("- gate-metadata:\n    - drop: contains --help and -h\n")


class TestReservedLabels:
    """Positional prose cannot forge the dedicated ``gate-metadata`` block."""

    @pytest.mark.parametrize("position", range(7))
    def test_reserved_sentinel_rejected(self, workdir: Path, position: int) -> None:
        """Every positional field rejects a stripped reserved sentinel line."""
        args = [*_ARGS, "retained", "item"]
        args[position] += "\n  - gate-metadata:  \n"
        assert wsc.main(args) == 2
        assert not wsc._CONTRACT.exists()

    def test_reserved_label_cannot_forge_metadata(self, workdir: Path) -> None:
        """A labelled list cannot render the dedicated gate block from positional prose."""
        assert wsc.main([*_ARGS, "gate-metadata", "calls_ahead: 99"]) == 2
        assert not wsc._CONTRACT.exists()


class TestAtomicWrite:
    """The contract is committed atomically and the owner binds to the bytes this session intended to write."""

    def test_owner_hashes_intended_bytes_after_overwrite(
        self, workdir: Path, session: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An intervening writer cannot make this session own its replacement bytes.

        A foreign writer overwrites the contract right after the atomic commit; the owner digest must still describe the
        bytes this session meant to write, so the mismatch is detectable later.
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

    def test_partial_temporary_write_cleaned(self, state: Path, session: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """An interrupted temporary write leaves complete old bytes and removes its partial file."""
        wsc._CONTRACT.write_bytes(_PREVIOUS_CONTRACT)
        wsc._OWNER.write_bytes(b"previous owner\n")
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
        assert wsc._CONTRACT.read_bytes() == _PREVIOUS_CONTRACT
        assert not wsc._OWNER.exists()
        assert not list(wsc._CONTRACT.parent.glob(".*"))
