"""Tests for the ``write_skill_contract`` bin script.

Covers the contract file's exact shape, argument validation, owner-stamp binding, the optional gate metadata block, and
atomic-write failure handling. Every test runs in an isolated working directory because the contract is written relative
to the caller's cwd.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path, PureWindowsPath

import pytest
import write_skill_contract as wsc

_ARGS = ["foundry:distill", "gap-analysis", ".reports/x", "run-dir=n/a", "Step 3 → Step 4"]
_PREVIOUS_CONTRACT = b"previous contract\n"
_PREVIOUS_OWNER = b"previous owner\n"


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run every test in ``tmp_path`` with no inherited session id, so the contract never lands in the repository."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


@pytest.fixture
def state(tmp_path: Path) -> Path:
    """Return the ``.temp/state`` directory the script writes into (not yet created)."""
    return tmp_path / ".temp" / "state"


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch) -> str:
    """Set a fixed session id so the script stamps an owner file."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "test-session")
    return "test-session"


@pytest.fixture
def previous_state(state: Path, session: str) -> tuple[Path, Path, Path]:
    """Stage an earlier contract and owner on disk and return ``(state, contract, owner)``."""
    state.mkdir(parents=True)
    contract, owner = state / "skill-contract.md", state / "skill-contract.owner"
    contract.write_bytes(_PREVIOUS_CONTRACT)
    owner.write_bytes(_PREVIOUS_OWNER)
    return state, contract, owner


def _spy_replace(monkeypatch: pytest.MonkeyPatch, fail_target: Path | None = None) -> None:
    """Wrap ``os.replace`` to assert each commit is atomic and, for ``fail_target`` only, raise ``OSError``."""
    replace = wsc.os.replace

    def checked_replace(source: str | Path, destination: str | Path) -> None:
        """Check the temporary file is complete and beside its target, optionally rejecting the commit."""
        target = Path(destination).resolve()
        assert Path(source).parent == target.parent
        assert Path(source).read_bytes()
        if target == fail_target:
            raise OSError("replace unavailable")
        replace(source, destination)

    monkeypatch.setattr(wsc.os, "replace", checked_replace)


def _owner_session(owner: Path) -> str | None:
    """Return the session id stamped in ``owner``, or ``None`` when no owner file exists."""
    return json.loads(owner.read_bytes())["session_id"] if owner.exists() else None


class TestContractFile:
    """The shape and location of the written contract."""

    def test_writes_all_five_fields(self, state: Path) -> None:
        """Each argument lands on its own labelled line, in order.

        The five positional arguments are the whole contract a post-compaction resume reads back, so their order and
        labels are what downstream hooks parse.
        """
        assert wsc.main(_ARGS) == 0
        assert (state / "skill-contract.md").read_text(encoding="utf-8").splitlines() == [
            "## Active Skill Contract",
            "- skill: foundry:distill · phase: gap-analysis",
            "- run-dir: .reports/x",
            "- preserve: run-dir=n/a",
            "- next: Step 3 → Step 4",
        ]

    @pytest.mark.parametrize(
        ("label", "ledger", "expected_tail"),
        [
            pytest.param(
                "probed (do NOT re-probe)",
                "hypothesis A :: Ruled-out\nhypothesis B :: Confirmed",
                ["- probed (do NOT re-probe):", "    - hypothesis A :: Ruled-out", "    - hypothesis B :: Confirmed"],
                id="labelled-list-appended",
            ),
            pytest.param("tried", "only one\n\n", ["- tried:", "    - only one"], id="blank-list-lines-dropped"),
        ],
    )
    def test_labelled_list_appended_without_blank_bullets(
        self, state: Path, label: str, ledger: str, expected_tail: list[str]
    ) -> None:
        """The 7-argument form appends the ledger a `next:` string refers to, one bullet per non-blank line.

        Without it a post-compaction resume is told to skip candidates "above" that were never written, and re-probes
        what it already ruled out. A trailing newline from a `head`/`tail` capture does not emit an empty bullet.
        """
        assert wsc.main([*_ARGS, label, ledger]) == 0
        lines = (state / "skill-contract.md").read_text(encoding="utf-8").splitlines()
        assert lines[-len(expected_tail) :] == expected_tail

    def test_empty_list_omits_block(self, state: Path) -> None:
        """An empty ledger produces no label line, so callers need not branch.

        A skill that has probed nothing yet passes an empty capture; the contract must still end at its ``next:`` line.
        """
        assert wsc.main([*_ARGS, "tried", ""]) == 0
        text = (state / "skill-contract.md").read_text(encoding="utf-8")
        assert "tried" not in text
        assert text.splitlines()[-1].startswith("- next:")

    def test_creates_state_dir(self, state: Path) -> None:
        """``.temp/state/`` is created when absent.

        A fresh project has no ``.temp`` yet, and the first skill boundary must still be able to write its contract.
        """
        wsc.main(_ARGS)
        assert state.is_dir()

    def test_overwrites_previous_contract(self, state: Path) -> None:
        """A later boundary replaces the earlier contract rather than appending.

        Two skills run back to back must not leave the first skill's preserve list in the second one's contract.
        """
        wsc.main(_ARGS)
        wsc.main(["foundry:audit", "step-6", "n/a", "p", "next"])
        text = (state / "skill-contract.md").read_text(encoding="utf-8")
        assert "foundry:distill" not in text
        assert "foundry:audit" in text

    def test_written_relative_to_cwd(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The contract belongs to the project the skill runs in, not the script's location.

        Run from a subdirectory of ``tmp_path``: the file must appear under that subdirectory and not beside it.
        """
        project = tmp_path / "project"
        project.mkdir()
        monkeypatch.chdir(project)
        wsc.main(_ARGS)
        assert (project / ".temp" / "state" / "skill-contract.md").is_file()
        assert not (tmp_path / ".temp").exists()

    def test_placeholder_tokens_survive_verbatim(self, state: Path) -> None:
        """`<REFINE_ITER>`-style fill-in tokens are written through untouched.

        Skills pass angle-bracket placeholders for the hook to substitute later; escaping them would break that.
        """
        wsc.main(["s", "iter <REFINE_ITER>/<MAX>", "n/a", "p", "n"])
        assert "iter <REFINE_ITER>/<MAX>" in (state / "skill-contract.md").read_text(encoding="utf-8")


class TestArgumentValidation:
    """The argument contract: arity, required skill name, help, and no partial output."""

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param([], id="no-args"),
            pytest.param(["a", "b", "c", "d"], id="four-args"),
            pytest.param(["a", "b", "c", "d", "e", "f"], id="six-args"),
        ],
    )
    def test_wrong_arity_exits_two(self, argv: list[str]) -> None:
        """Anything but five or seven arguments is a usage error.

        Four and six arguments are the near misses a caller makes when it forgets or doubles a field.
        """
        assert wsc.main(argv) == 2

    def test_empty_skill_exits_two(self) -> None:
        """An empty skill name would produce a contract no hook could attribute.

        All five arguments are present, so arity passes and only the empty skill name can be the reason for rejection.
        """
        assert wsc.main(["", "phase", "dir", "preserve", "next"]) == 2

    def test_help_exits_zero_with_usage(self, capsys: pytest.CaptureFixture[str]) -> None:
        """--help prints usage and exits 0, per the bin/ help contract.

        Every bin/ script answers ``--help`` so users and audits can discover the interface without reading source.
        """
        with pytest.raises(SystemExit) as exc:
            wsc.main(["--help"])
        assert exc.value.code == 0
        assert "usage:" in capsys.readouterr().out

    def test_no_file_written_on_usage_error(self, tmp_path: Path) -> None:
        """A rejected invocation leaves no partial contract behind.

        A one-argument call fails arity; nothing, not even the ``.temp`` directory, may be created by the attempt.
        """
        wsc.main(["only-one"])
        assert not (tmp_path / ".temp").exists()


class TestOwnerStamp:
    """Ownership is bound to the written bytes and an earlier stamp is invalidated before writing."""

    def test_owner_matches_raw_bytes(self, state: Path, session: str) -> None:
        """A session environment produces an owner holding the digest of the exact LF-terminated bytes.

        The hook trusts a contract only when the owner's sha256 equals the file's bytes, so the stamp is checked against
        the raw file, with no CRLF conversion.
        """
        assert wsc.main(_ARGS) == 0
        raw = (state / "skill-contract.md").read_bytes()
        owner = json.loads((state / "skill-contract.owner").read_bytes())
        assert raw.endswith(b"\n")
        assert b"\r\n" not in raw
        assert set(owner) == {"session_id", "sha256", "written_at"}
        assert owner["session_id"] == session
        assert owner["sha256"] == hashlib.sha256(raw).hexdigest()
        assert datetime.fromisoformat(owner["written_at"]).tzinfo is not None

    def test_empty_session_writes_no_owner(self, state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """An empty session id writes the contract but no owner file.

        Without a session there is nothing to attribute the contract to, so an owner would be a forged claim.
        """
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "")
        assert wsc.main(_ARGS) == 0
        raw = (state / "skill-contract.md").read_bytes()
        assert raw.endswith(b"\n")
        assert b"\r\n" not in raw
        assert not (state / "skill-contract.owner").exists()

    def test_unset_session_removes_existing_owner(self, state: Path) -> None:
        """An unstamped write cannot inherit the previous session's owner.

        A stale owner beside freshly written bytes would claim the new contract for the old session.
        """
        state.mkdir(parents=True)
        (state / "skill-contract.owner").write_text('"stale"', encoding="utf-8")
        assert wsc.main(_ARGS) == 0
        assert (state / "skill-contract.md").is_file()
        assert not (state / "skill-contract.owner").exists()

    def test_windows_style_working_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, session: str
    ) -> None:
        """Native CWD components and serialized Windows paths survive without separator conversion.

        The run-dir argument is a Windows path; it must reach the file byte for byte, and the owner digest must cover
        it.
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
        "argv",
        [
            pytest.param([], id="invalid-arity"),
            pytest.param(["", "phase", "dir", "preserve", "next"], id="empty-skill"),
        ],
    )
    def test_validation_exit_invalidates_prior_owner(
        self, previous_state: tuple[Path, Path, Path], argv: list[str]
    ) -> None:
        """Every invocation invalidates earlier ownership, even when no new contract is written.

        A rejected call may still be followed by a resume, which must not trust the old owner over unchanged bytes.
        """
        _, contract, owner = previous_state
        assert wsc.main(argv) == 2
        assert not owner.exists()
        assert contract.read_bytes() == _PREVIOUS_CONTRACT

    def test_help_invalidates_prior_owner(self, previous_state: tuple[Path, Path, Path]) -> None:
        """``--help`` exits 0 yet still invalidates earlier ownership and leaves the contract bytes alone.

        Help is an invocation like any other; skipping the invalidation would leave a stale owner trusted.
        """
        _, contract, owner = previous_state
        with pytest.raises(SystemExit) as exc:
            wsc.main(["--help"])
        assert exc.value.code == 0
        assert not owner.exists()
        assert contract.read_bytes() == _PREVIOUS_CONTRACT

    def test_owner_hashes_intended_bytes_after_overwrite(self, session: str) -> None:
        """An intervening writer cannot make this session own its replacement bytes.

        Both the old direct-write boundary and the atomic replace are hit by a foreign write right after the commit; the
        owner must hold the digest of what this run intended, which then differs from the file on disk.
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

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(Path, "write_bytes", overwrite)
            patch.setattr(wsc.os, "replace", replace)
            assert wsc.main(_ARGS) == 0
        owner = json.loads(wsc._OWNER.read_bytes())
        assert owner["sha256"] == hashlib.sha256(intended[0]).hexdigest()
        assert owner["sha256"] != hashlib.sha256(wsc._CONTRACT.read_bytes()).hexdigest()


class TestAtomicWrite:
    """Invalidation and atomic-write failures preserve complete bytes and leave no temporary files."""

    @pytest.mark.parametrize(
        ("write_fails", "exit_code", "owner_session"),
        [
            pytest.param(False, 0, "test-session", id="write-succeeds-restamps-owner"),
            pytest.param(True, 1, None, id="write-fails-leaves-no-owner"),
        ],
    )
    def test_existing_owner_removed_before_write(
        self,
        previous_state: tuple[Path, Path, Path],
        monkeypatch: pytest.MonkeyPatch,
        write_fails: bool,
        exit_code: int,
        owner_session: str | None,
    ) -> None:
        """A stale owner must already be absent when the contract write starts, including when the write then fails.

        The check runs inside ``os.replace`` at the instant the contract is committed, so an owner removed only after
        the write would be caught.
        """
        state, _, owner = previous_state
        replace = wsc.os.replace

        def checked_replace(source: str | Path, destination: str | Path) -> None:
            """Check invalidation at atomic replacement, optionally rejecting the contract."""
            if Path(destination).name == "skill-contract.md":
                assert not owner.exists()
                if write_fails:
                    raise OSError("contract unavailable")
            replace(source, destination)

        monkeypatch.setattr(wsc.os, "replace", checked_replace)
        assert wsc.main(_ARGS) == exit_code
        assert not list(state.glob(".*"))
        assert _owner_session(owner) == owner_session

    def test_owner_removal_failure_keeps_previous_files(
        self, previous_state: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the stale owner cannot be removed, nothing is written and the previous pair stays intact.

        Replacing the contract while an owner that certifies the old bytes remains would forge ownership of new bytes.
        """
        state, contract, owner = previous_state
        unlink = Path.unlink

        def locked_unlink(path: Path, missing_ok: bool = False) -> None:
            """Reject invalidation before any replacement can occur."""
            if path.name == "skill-contract.owner":
                raise OSError("owner locked")
            unlink(path, missing_ok=missing_ok)

        _spy_replace(monkeypatch)
        monkeypatch.setattr(Path, "unlink", locked_unlink)
        assert wsc.main(_ARGS) == 1
        assert not list(state.glob(".*"))
        assert contract.read_bytes() == _PREVIOUS_CONTRACT
        assert owner.read_bytes() == _PREVIOUS_OWNER

    def test_contract_commit_failure_keeps_previous_contract_without_owner(
        self, previous_state: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rejected contract commit leaves the old contract bytes and no owner claiming them.

        The owner was invalidated first, so the surviving old contract is simply unowned rather than mis-owned.
        """
        state, contract, owner = previous_state
        _spy_replace(monkeypatch, fail_target=contract)
        assert wsc.main(_ARGS) == 1
        assert not list(state.glob(".*"))
        assert not owner.exists()
        assert contract.read_bytes() == _PREVIOUS_CONTRACT

    def test_owner_commit_failure_keeps_new_contract_without_owner(
        self, previous_state: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rejected owner commit leaves the freshly written contract unowned.

        The contract is already replaced when the stamp fails, so the new bytes stay but must not be claimed by anyone.
        """
        state, contract, owner = previous_state
        _spy_replace(monkeypatch, fail_target=owner)
        assert wsc.main(_ARGS) == 1
        assert not list(state.glob(".*"))
        assert not owner.exists()
        assert contract.read_bytes().startswith(b"## Active Skill Contract\n")

    def test_temporary_file_failure_keeps_previous_contract_without_owner(
        self, previous_state: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When no temporary file can be created, the old contract survives and no owner remains.

        Creation fails before any byte is written, so no partial artifact may be left in the state directory.
        """
        state, contract, owner = previous_state

        def blocked_temporary(**kwargs: object) -> None:
            """Reject temporary-file creation without leaving a partial artifact."""
            raise OSError("temporary unavailable")

        _spy_replace(monkeypatch)
        monkeypatch.setattr(wsc.tempfile, "NamedTemporaryFile", blocked_temporary)
        assert wsc.main(_ARGS) == 1
        assert not list(state.glob(".*"))
        assert not owner.exists()
        assert contract.read_bytes() == _PREVIOUS_CONTRACT

    def test_partial_temporary_write_cleaned(
        self, previous_state: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An interrupted temporary write leaves complete old bytes and removes its partial file.

        The real temporary file is created and 10 bytes land in it before the write raises, so cleanup must delete
        genuine partial content, not just an empty placeholder.
        """
        state, contract, owner = previous_state
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
        assert contract.read_bytes() == _PREVIOUS_CONTRACT
        assert not owner.exists()
        assert not list(state.glob(".*"))


class TestBoundaryMetadata:
    """Optional gate metadata after ``--``, validated without changing positional or ownership contracts."""

    @pytest.mark.parametrize(
        ("list_args", "list_block"),
        [
            pytest.param([], "", id="without-labelled-list"),
            pytest.param(["retained", "one item"], "- retained:\n    - one item\n", id="after-labelled-list"),
        ],
    )
    def test_metadata_appended_before_owner_stamp(
        self, state: Path, session: str, list_args: list[str], list_block: str
    ) -> None:
        """The owner digest includes each metadata line, which follows any labelled list.

        The gate block is the last thing written before the stamp, so the digest in the owner must cover it.
        """
        assert wsc.main([*_ARGS, *list_args, "--", "calls_ahead=60", "drop=temporary notes", "interactive=false"]) == 0
        raw = (state / "skill-contract.md").read_bytes()
        text = raw.decode("utf-8")
        assert text.splitlines()[-4:] == [
            "- gate-metadata:",
            "    - calls_ahead: 60",
            "    - drop: temporary notes",
            "    - interactive: false",
        ]
        assert f"{list_block}- gate-metadata:\n    - calls_ahead:" in text
        owner = json.loads((state / "skill-contract.owner").read_bytes())
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

        Each case is a value that could forge an extra metadata line or smuggle a second key into the gate block.
        """
        assert wsc.main([*_ARGS, "--", *tail]) == 2
        assert "usage:" in capsys.readouterr().err
        assert not (state / "skill-contract.md").exists()

    @pytest.mark.parametrize(
        "positional",
        [pytest.param(_ARGS[:4], id="four-positional"), pytest.param([*_ARGS, "extra"], id="six-positional")],
    )
    def test_metadata_does_not_change_positional_arity(self, positional: list[str]) -> None:
        """Only five or seven arguments before the delimiter remain valid.

        A valid metadata tail must not rescue an invalid positional count.
        """
        assert wsc.main([*positional, "--", "calls_ahead=0"]) == 2

    def test_zero_calls_empty_drop_and_embedded_help(self, state: Path) -> None:
        """Zero is valid and embedded help text is preserved without triggering help.

        ``drop=contains --help and -h`` sits after ``--``, so it is data and must not be parsed as a help request.
        """
        contract = state / "skill-contract.md"
        assert wsc.main([*_ARGS, "--", "calls_ahead=0", "drop="]) == 0
        assert contract.read_text(encoding="utf-8").endswith("- gate-metadata:\n    - calls_ahead: 0\n    - drop: \n")
        assert wsc.main([*_ARGS, "--", "drop=contains --help and -h"]) == 0
        assert contract.read_text(encoding="utf-8").endswith("- gate-metadata:\n    - drop: contains --help and -h\n")


class TestReservedSentinel:
    """Positional prose cannot forge the dedicated gate-metadata block."""

    @pytest.mark.parametrize("position", range(7))
    def test_stripped_sentinel_line_rejected_in_every_field(self, position: int) -> None:
        """Every positional field rejects a stripped reserved sentinel line.

        The sentinel is matched after stripping whitespace, so surrounding spaces and newlines do not hide it.
        """
        args = [*_ARGS, "retained", "item"]
        args[position] += "\n  - gate-metadata:  \n"
        assert wsc.main(args) == 2
        assert not wsc._CONTRACT.exists()

    def test_reserved_label_cannot_forge_metadata(self) -> None:
        """A labelled list cannot render the dedicated gate block from positional prose.

        ``gate-metadata`` as the list label would print the same heading the validated metadata block owns.
        """
        assert wsc.main([*_ARGS, "gate-metadata", "calls_ahead: 99"]) == 2
        assert not wsc._CONTRACT.exists()
