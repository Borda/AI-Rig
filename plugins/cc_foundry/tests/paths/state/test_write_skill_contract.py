"""Tests for the ``write_skill_contract`` bin script.

Covers the contract file's exact shape, argument validation, the owner stamp that binds a contract to a session, the
keep-item options, and the fact that the file is written relative to the caller's working directory.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path, PureWindowsPath

import pytest
import write_skill_contract as wsc

_ARGS = ["foundry:distill", "gap-analysis", ".reports/x", "run-dir=n/a", "Step 3 → Step 4"]
_STATE = Path(".temp/state")
_PREVIOUS_CONTRACT = b"previous contract\n"
_PREVIOUS_OWNER = b"previous owner\n"


@pytest.fixture(autouse=True)
def _no_ambient_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip the session id of the Claude Code session running the tests, so no owner stamp appears unasked."""
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


@pytest.fixture(name="project")
def _project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run the test inside an empty project directory and return it."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture(name="session")
def _session(monkeypatch: pytest.MonkeyPatch) -> str:
    """Set a session id, so the script stamps an owner file, and return it."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "test-session")
    return "test-session"


@pytest.fixture(name="stale_state")
def _stale_state(project: Path, session: str) -> tuple[Path, Path]:
    """Write a previous contract and owner under ``.temp/state`` and return their paths."""
    state = project / _STATE
    state.mkdir(parents=True)
    contract, owner = state / "skill-contract.md", state / "skill-contract.owner"
    contract.write_bytes(_PREVIOUS_CONTRACT)
    owner.write_bytes(_PREVIOUS_OWNER)
    return contract, owner


def _contract_text(root: Path) -> str:
    """Read the contract the script wrote under ``root``."""
    return (root / _STATE / "skill-contract.md").read_text(encoding="utf-8")


def _main_code(argv: list[str]) -> int | str | None:
    """Run ``wsc.main`` and return its exit code, whether it returned it or raised ``SystemExit``."""
    try:
        return wsc.main(argv)
    except SystemExit as exc:
        return exc.code


def _fail_replace_of(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    """Make the atomic replacement of ``path`` raise ``OSError`` once its complete temporary bytes exist."""
    replace = wsc.os.replace

    def checked_replace(source: str | Path, destination: str | Path) -> None:
        """Check the temporary file is complete and beside its target, then reject or perform the replacement."""
        target = Path(destination).resolve()
        assert Path(source).parent == target.parent
        assert Path(source).read_bytes()
        if target == path.resolve():
            raise OSError("replace unavailable")
        replace(source, destination)

    monkeypatch.setattr(wsc.os, "replace", checked_replace)


class TestContractFile:
    """The written contract file: shape, labelled lists, location and overwrite behaviour."""

    def test_writes_all_five_fields(self, project: Path) -> None:
        """Each argument lands on its own labelled line, in order.

        The five positional arguments are the whole resume contract, so the file must carry them in a fixed layout.
        """
        assert wsc.main(_ARGS) == 0
        assert _contract_text(project).splitlines() == [
            "## Active Skill Contract",
            "- skill: foundry:distill · phase: gap-analysis",
            "- run-dir: .reports/x",
            "- preserve: run-dir=n/a",
            "- next: Step 3 → Step 4",
        ]

    @pytest.mark.parametrize(
        ("label", "items", "tail"),
        [
            # Without the appended ledger a post-compaction resume is told to skip candidates "above" that were never
            # written, and re-probes what it already ruled out.
            pytest.param(
                "probed (do NOT re-probe)",
                "hypothesis A :: Ruled-out\nhypothesis B :: Confirmed",
                ["- probed (do NOT re-probe):", "    - hypothesis A :: Ruled-out", "    - hypothesis B :: Confirmed"],
                id="labelled-list-appended",
            ),
            # A trailing newline from a `head`/`tail` capture does not emit an empty bullet.
            pytest.param("tried", "only one\n\n", ["- tried:", "    - only one"], id="blank-list-lines-dropped"),
        ],
    )
    def test_labelled_list_appended(self, project: Path, label: str, items: str, tail: list[str]) -> None:
        """The 7-argument form appends the ledger a `next:` string refers to, one bullet per non-blank line.

        The ledger sits after the five base lines; blank lines in the captured text never become empty bullets.
        """
        assert wsc.main([*_ARGS, label, items]) == 0
        assert _contract_text(project).splitlines()[-len(tail) :] == tail

    def test_empty_list_omits_block(self, project: Path) -> None:
        """An empty ledger produces no label line, so callers need not branch.

        Callers pass whatever a capture produced; when it is empty the file must end at the ``next:`` line.
        """
        assert wsc.main([*_ARGS, "tried", ""]) == 0
        text = _contract_text(project)
        assert "tried" not in text
        assert text.splitlines()[-1].startswith("- next:")

    def test_creates_parent_directories(self, project: Path) -> None:
        """``.temp/state/`` is created when absent, so the first boundary of a run needs no setup."""
        wsc.main(_ARGS)
        assert (project / _STATE).is_dir()

    def test_overwrites_previous_contract(self, project: Path) -> None:
        """A later boundary replaces the earlier contract rather than appending.

        Only the newest boundary describes where to resume, so stale text from the previous skill must disappear.
        """
        wsc.main(_ARGS)
        wsc.main(["foundry:audit", "step-6", "n/a", "p", "next"])
        text = _contract_text(project)
        assert "foundry:distill" not in text
        assert "foundry:audit" in text

    def test_written_relative_to_cwd(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The contract belongs to the project the skill runs in, not the script's location.

        Running from a nested project directory must leave the parent directory without a ``.temp`` folder.
        """
        project = tmp_path / "project"
        project.mkdir()
        monkeypatch.chdir(project)
        wsc.main(_ARGS)
        assert (project / _STATE / "skill-contract.md").is_file()
        assert not (tmp_path / ".temp").exists()

    def test_placeholder_tokens_survive_verbatim(self, project: Path) -> None:
        """`<REFINE_ITER>`-style fill-in tokens are written through untouched.

        Skills pass literal placeholders for the orchestrator to fill, so the script must not expand or strip them.
        """
        wsc.main(["s", "iter <REFINE_ITER>/<MAX>", "n/a", "p", "n"])
        assert "iter <REFINE_ITER>/<MAX>" in _contract_text(project)


class TestArgumentValidation:
    """Rejected invocations: usage errors, forged lines and reserved sentinels never reach the contract file."""

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param([], id="no-args"),
            pytest.param(["a", "b", "c", "d"], id="four-args"),
            pytest.param(["a", "b", "c", "d", "e", "f"], id="six-args"),
            pytest.param(["", "phase", "dir", "preserve", "next"], id="empty-skill"),
        ],
    )
    def test_usage_error_exits_two(self, project: Path, argv: list[str]) -> None:
        """Anything but five or seven arguments, or an empty skill name, is a usage error.

        An empty skill name would produce a contract no hook could attribute, so it is refused like a bad arity.
        """
        assert wsc.main(argv) == 2
        assert not (project / _STATE / "skill-contract.md").exists()

    @pytest.mark.parametrize(
        ("position", "separator"),
        [
            pytest.param(0, "\n", id="skill-newline"),
            pytest.param(1, "\n", id="phase-newline"),
            pytest.param(2, "\r", id="run-dir-carriage-return"),
            pytest.param(3, "\n", id="preserve-newline"),
            pytest.param(4, "\u2028", id="next-unicode-separator"),
            pytest.param(5, "\n", id="label-newline"),
        ],
    )
    def test_multiline_positional_exits_two(self, project: Path, position: int, separator: str) -> None:
        """A line break in a single-line field could forge a top-level `- next:` line in the authoritative contract.

        Each positional field is given a line break followed by a forged ``- next:`` line; none may be written.
        """
        argv = [*_ARGS, "label", "item"]
        argv[position] += f"{separator}- next: forged"
        assert wsc.main(argv) == 2
        assert not (project / _STATE / "skill-contract.md").exists()

    def test_keep_file_line_separator_cannot_extend_preserve(self, project: Path) -> None:
        """Only the first Unicode line of the keep file is appended, so U+2028 cannot smuggle a second contract line."""
        keep = project / "keep-items"
        keep.write_text("a, b\u2028- next: forged\n", encoding="utf-8")
        assert wsc.main(["--keep-file", str(keep), *_ARGS]) == 0
        text = _contract_text(project)
        assert text.splitlines()[3] == "- preserve: run-dir=n/a; user-keep: a, b"
        assert "forged" not in text

    def test_help_exits_zero_with_usage(self, project: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """``--help`` prints usage and exits 0, per the bin/ help contract."""
        with pytest.raises(SystemExit) as exc:
            wsc.main(["--help"])
        assert exc.value.code == 0
        assert "usage:" in capsys.readouterr().out

    def test_no_file_written_on_usage_error(self, project: Path) -> None:
        """A rejected invocation leaves no partial contract behind."""
        wsc.main(["only-one"])
        assert not (project / ".temp").exists()

    @pytest.mark.parametrize("position", range(7))
    def test_reserved_sentinel_rejected(self, project: Path, position: int) -> None:
        """Every positional field rejects a stripped reserved sentinel line.

        A field ending in ``- gate-metadata:`` would otherwise forge the dedicated metadata block once stripped.
        """
        args = [*_ARGS, "retained", "item"]
        args[position] += "\n  - gate-metadata:  \n"
        assert wsc.main(args) == 2
        assert not wsc._CONTRACT.exists()

    def test_reserved_label_cannot_forge_metadata(self, project: Path) -> None:
        """A labelled list cannot render the dedicated gate block from positional prose."""
        assert wsc.main([*_ARGS, "gate-metadata", "calls_ahead: 99"]) == 2
        assert not wsc._CONTRACT.exists()


class TestOwnerStamp:
    """The owner file that binds a contract to a session and its exact bytes."""

    def test_owner_matches_raw_bytes(self, project: Path, session: str) -> None:
        """A session environment produces an owner holding the digest of the exact LF bytes written.

        The digest is how a resume proves the contract was not replaced by another writer.
        """
        assert wsc.main(_ARGS) == 0
        state = project / _STATE
        raw = (state / "skill-contract.md").read_bytes()
        owner = json.loads((state / "skill-contract.owner").read_bytes())
        assert raw.endswith(b"\n")
        assert b"\r\n" not in raw
        assert set(owner) == {"session_id", "sha256", "written_at"}
        assert owner["session_id"] == session
        assert owner["sha256"] == hashlib.sha256(raw).hexdigest()
        assert datetime.fromisoformat(owner["written_at"]).tzinfo is not None

    def test_no_owner_without_session(self, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """An empty session environment writes LF bytes and no owner file.

        Without a session there is nobody to own the contract, so the stamp is skipped rather than written blank.
        """
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "")
        assert wsc.main(_ARGS) == 0
        state = project / _STATE
        raw = (state / "skill-contract.md").read_bytes()
        assert raw.endswith(b"\n")
        assert b"\r\n" not in raw
        assert not (state / "skill-contract.owner").exists()

    def test_stale_owner_removed_before_write(
        self, project: Path, session: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stale owner is already gone when the contract replacement starts, and a new owner is stamped after.

        Removing the old stamp first means a crash mid-write can never leave a new contract under an old owner.
        """
        state = project / _STATE
        state.mkdir(parents=True)
        owner_path = state / "skill-contract.owner"
        owner_path.write_text('"stale"', encoding="utf-8")
        replace = wsc.os.replace

        def checked_replace(source: str | Path, destination: str | Path) -> None:
            """Assert the owner is gone at the contract's atomic replacement."""
            if Path(destination).name == "skill-contract.md":
                assert not owner_path.exists()
            replace(source, destination)

        monkeypatch.setattr(wsc.os, "replace", checked_replace)

        assert wsc.main(_ARGS) == 0
        assert not list(state.glob(".*"))
        assert json.loads(owner_path.read_bytes())["session_id"] == session

    def test_stale_owner_removed_when_write_fails(
        self, project: Path, session: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stale owner is gone before the contract replacement even when that replacement fails.

        The failed write exits 1, leaves no temporary file, and must not leave the old owner vouching for anything.
        """
        state = project / _STATE
        state.mkdir(parents=True)
        owner_path = state / "skill-contract.owner"
        owner_path.write_text('"stale"', encoding="utf-8")

        def failing_replace(source: str | Path, destination: str | Path) -> None:
            """Assert the owner is gone, then reject the contract replacement."""
            assert not owner_path.exists()
            raise OSError("contract unavailable")

        monkeypatch.setattr(wsc.os, "replace", failing_replace)

        assert wsc.main(_ARGS) == 1
        assert not list(state.glob(".*"))
        assert not owner_path.exists()

    def test_unset_session_removes_existing_owner(self, project: Path) -> None:
        """An unstamped write cannot inherit the previous session's owner."""
        state = project / _STATE
        state.mkdir(parents=True)
        (state / "skill-contract.owner").write_text('"stale"', encoding="utf-8")
        assert wsc.main(_ARGS) == 0
        assert (state / "skill-contract.md").is_file()
        assert not (state / "skill-contract.owner").exists()

    def test_windows_style_working_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, session: str
    ) -> None:
        """Native CWD components and serialized Windows paths survive without separator conversion.

        A run directory recorded in Windows form must reach the contract byte for byte and be covered by the digest.
        """
        project = tmp_path / "Windows workspace" / "project"
        project.mkdir(parents=True)
        monkeypatch.chdir(project)
        run_dir = str(PureWindowsPath("C:/work/project/reports/run"))
        assert wsc.main(["sample:skill", "boundary", run_dir, "preserve", "next"]) == 0
        state = project / _STATE
        raw = (state / "skill-contract.md").read_bytes()
        assert f"- run-dir: {run_dir}\n".encode() in raw
        assert json.loads((state / "skill-contract.owner").read_bytes())["sha256"] == hashlib.sha256(raw).hexdigest()
        assert not (tmp_path / ".temp").exists()

    def test_failed_owner_invalidation_keeps_previous_state(
        self, stale_state: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the stale owner cannot be removed, the previous contract and owner are both left intact.

        Writing a new contract while the old owner cannot be invalidated would pair them wrongly, so the run aborts.
        """
        contract, owner = stale_state
        unlink = Path.unlink

        def locked_unlink(path: Path, missing_ok: bool = False) -> None:
            """Reject removal of the owner file."""
            if path.name == "skill-contract.owner":
                raise OSError("owner locked")
            unlink(path, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", locked_unlink)

        assert wsc.main(_ARGS) == 1
        assert not list(contract.parent.glob(".*"))
        assert contract.read_bytes() == _PREVIOUS_CONTRACT
        assert owner.read_bytes() == _PREVIOUS_OWNER

    def test_failed_contract_replace_leaves_old_bytes_and_no_owner(
        self, stale_state: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed atomic commit of the contract keeps the complete old bytes and leaves no owner or temporary file.

        The stale owner was already invalidated, so after the failure nothing may vouch for the unchanged old contract.
        """
        contract, owner = stale_state
        _fail_replace_of(monkeypatch, contract)

        assert wsc.main(_ARGS) == 1
        assert not list(contract.parent.glob(".*"))
        assert not owner.exists()
        assert contract.read_bytes() == _PREVIOUS_CONTRACT

    def test_failed_owner_stamp_keeps_new_contract_without_owner(
        self, stale_state: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed owner commit leaves the new contract written, no owner file, and no temporary file.

        An unstamped contract is not authoritative, so the missing owner is the safe outcome of this failure.
        """
        contract, owner = stale_state
        _fail_replace_of(monkeypatch, owner)

        assert wsc.main(_ARGS) == 1
        assert not list(contract.parent.glob(".*"))
        assert not owner.exists()
        assert contract.read_bytes().startswith(b"## Active Skill Contract\n")

    def test_failed_temporary_creation_keeps_old_bytes(
        self, stale_state: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the temporary file cannot be created, the old contract survives and no owner remains.

        Creating the temporary file is the first step of the atomic write, so its failure must change nothing but the
        invalidated owner.
        """
        contract, owner = stale_state

        def blocked_temporary(**kwargs: object) -> None:
            """Reject temporary-file creation without leaving a partial artifact."""
            raise OSError("temporary unavailable")

        monkeypatch.setattr(wsc.tempfile, "NamedTemporaryFile", blocked_temporary)

        assert wsc.main(_ARGS) == 1
        assert not list(contract.parent.glob(".*"))
        assert not owner.exists()
        assert contract.read_bytes() == _PREVIOUS_CONTRACT

    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            pytest.param([], 2, id="invalid-arity"),
            pytest.param(["", "phase", "dir", "preserve", "next"], 2, id="empty-skill"),
            pytest.param(["--help"], 0, id="help"),
        ],
    )
    def test_validation_exit_invalidates_prior_owner(self, project: Path, argv: list[str], expected: int) -> None:
        """Every invocation invalidates earlier ownership, even when no new contract is written.

        The previous contract stays byte-identical, but its owner is removed, so an interrupted or rejected run cannot
        leave an old stamp vouching for a contract the caller meant to replace.
        """
        state = project / _STATE
        state.mkdir(parents=True)
        contract = state / "skill-contract.md"
        contract.write_bytes(_PREVIOUS_CONTRACT)
        owner = state / "skill-contract.owner"
        owner.write_text(
            json.dumps({"session_id": "test-session", "sha256": hashlib.sha256(_PREVIOUS_CONTRACT).hexdigest()})
        )

        assert _main_code(argv) == expected
        assert not owner.exists()
        assert contract.read_bytes() == _PREVIOUS_CONTRACT

    def test_owner_hashes_intended_bytes_after_overwrite(
        self, project: Path, session: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An intervening writer cannot make this session own its replacement bytes.

        A foreign write lands right after the atomic commit; the owner must hash what this run intended to write, so the
        resume check fails against the foreign bytes.
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

    def test_partial_temporary_write_cleaned(
        self, stale_state: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An interrupted temporary write leaves complete old bytes and removes its partial file.

        The real temporary file is created, a partial prefix is written, and then the write fails; the old contract must
        survive untouched and the partial file must not be left behind.
        """
        contract, owner = stale_state
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
        assert not list(contract.parent.glob(".*"))


class TestBoundaryMetadata:
    """The optional ``--`` delimited metadata block, validated without changing positional or ownership contracts."""

    @pytest.mark.parametrize(
        ("extra", "tail"),
        [
            pytest.param(
                [],
                [
                    "- gate-metadata:",
                    "    - calls_ahead: 60",
                    "    - drop: temporary notes",
                    "    - interactive: false",
                ],
                id="metadata-only",
            ),
            pytest.param(
                ["retained", "one item"],
                [
                    "- retained:",
                    "    - one item",
                    "- gate-metadata:",
                    "    - calls_ahead: 60",
                    "    - drop: temporary notes",
                    "    - interactive: false",
                ],
                id="after-labelled-list",
            ),
        ],
    )
    def test_metadata_appended_before_owner_stamp(
        self, project: Path, session: str, extra: list[str], tail: list[str]
    ) -> None:
        """The metadata block closes the file, after any labelled list, and the owner digest covers it.

        The stamp is computed over the final bytes, so adding metadata must not leave the owner hashing a shorter file.
        """
        assert wsc.main([*_ARGS, *extra, "--", "calls_ahead=60", "drop=temporary notes", "interactive=false"]) == 0
        state = project / _STATE
        raw = (state / "skill-contract.md").read_bytes()
        owner = json.loads((state / "skill-contract.owner").read_bytes())
        assert raw.decode("utf-8").splitlines()[-len(tail) :] == tail
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
        self, project: Path, capsys: pytest.CaptureFixture[str], tail: list[str]
    ) -> None:
        """Rejected metadata prints usage and never creates a partial contract."""
        assert wsc.main([*_ARGS, "--", *tail]) == 2
        assert "usage:" in capsys.readouterr().err
        assert not (project / _STATE / "skill-contract.md").exists()

    @pytest.mark.parametrize(
        "positional",
        [pytest.param(_ARGS[:4], id="four-positional"), pytest.param([*_ARGS, "extra"], id="six-positional")],
    )
    def test_metadata_does_not_change_positional_arity(self, project: Path, positional: list[str]) -> None:
        """Only five or seven arguments before the delimiter remain valid."""
        assert wsc.main([*positional, "--", "calls_ahead=0"]) == 2

    @pytest.mark.parametrize(
        ("tail", "ending"),
        [
            pytest.param(
                ["calls_ahead=0", "drop="],
                "- gate-metadata:\n    - calls_ahead: 0\n    - drop: \n",
                id="zero-calls-and-empty-drop",
            ),
            pytest.param(
                ["drop=contains --help and -h"],
                "- gate-metadata:\n    - drop: contains --help and -h\n",
                id="embedded-help-text",
            ),
        ],
    )
    def test_zero_empty_and_embedded_help_values(self, project: Path, tail: list[str], ending: str) -> None:
        """Zero and an empty drop are valid, and help text inside a value is preserved without triggering help."""
        assert wsc.main([*_ARGS, "--", *tail]) == 0
        assert _contract_text(project).endswith(ending)


@pytest.fixture(name="session_tmp")
def _session_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Provide a private TMPDIR plus session token, with the contract written under a separate working directory."""
    tmp = tmp_path / "tmp"
    work = tmp_path / "work"
    tmp.mkdir()
    work.mkdir()
    monkeypatch.setenv("TMPDIR", str(tmp))
    monkeypatch.setenv("CSID", "sess-1")
    monkeypatch.chdir(work)
    return tmp


def _preserve_line() -> str:
    """Return the ``- preserve:`` line of the contract written in the current working directory."""
    return (_STATE / "skill-contract.md").read_text(encoding="utf-8").splitlines()[3]


class TestKeepSentinel:
    """The leading ``--keep-sentinel <name>`` option that replaced the per-site bash keep append."""

    @pytest.mark.parametrize(
        ("sentinel_file", "content", "preserve_line"),
        [
            pytest.param("dev-keep-sess-1", "a, b\n", "- preserve: run-dir=n/a; user-keep: a, b", id="appended"),
            pytest.param(
                "dev-keep-sess-1", "first\nsecond\n", "- preserve: run-dir=n/a; user-keep: first", id="line-1"
            ),
            pytest.param("dev-keep-sess-1", "x y\r\n", "- preserve: run-dir=n/a; user-keep: x y", id="crlf-dropped"),
            pytest.param("dev-keep-sess-1", "a\rb\n", "- preserve: run-dir=n/a; user-keep: a", id="bare-cr-ends-line"),
            pytest.param("dev-keep-sess-1", "a\x0bb\n", "- preserve: run-dir=n/a; user-keep: a", id="vt-ends-line"),
            pytest.param("dev-keep-sess-1", "keep tail", "- preserve: run-dir=n/a", id="no-trailing-newline"),
            pytest.param("dev-keep-sess-1", "\n", "- preserve: run-dir=n/a", id="empty-line-appends-nothing"),
            pytest.param("dev-keep-other", "a\n", "- preserve: run-dir=n/a", id="other-session-not-read"),
        ],
    )
    def test_preserve_line(self, session_tmp: Path, sentinel_file: str, content: str, preserve_line: str) -> None:
        """The first line of this session's sentinel lands as ``; user-keep: …``; anything else appends nothing.

        Mirrors the bash idiom it replaced (``IFS= read -r _KEEP … || _KEEP=""`` then ``[ -n "$_KEEP" ] && …``): a
        sentinel written by another session (different CSID suffix) must never leak into this contract.
        """
        (session_tmp / sentinel_file).write_text(content, encoding="utf-8", newline="")
        assert wsc.main(["--keep-sentinel", "dev-keep", *_ARGS]) == 0
        assert _preserve_line() == preserve_line


class TestKeepFile:
    """The leading ``--keep-file <path>`` option for keep items held in a per-session state directory."""

    @pytest.mark.parametrize(
        ("content", "preserve_line"),
        [
            pytest.param("a, b\n", "- preserve: run-dir=n/a; user-keep: a, b", id="appended"),
            pytest.param("first\nsecond\n", "- preserve: run-dir=n/a; user-keep: first", id="line-1"),
            pytest.param("x y\r\n", "- preserve: run-dir=n/a; user-keep: x y", id="crlf-dropped"),
            pytest.param("a\rb\n", "- preserve: run-dir=n/a; user-keep: a", id="bare-cr-ends-line"),
            pytest.param("keep tail", "- preserve: run-dir=n/a", id="no-trailing-newline"),
            pytest.param("\n", "- preserve: run-dir=n/a", id="empty-line-appends-nothing"),
            pytest.param("", "- preserve: run-dir=n/a", id="empty-file-appends-nothing"),
        ],
    )
    def test_preserve_line(self, session_tmp: Path, content: str, preserve_line: str) -> None:
        """The first line of the named file lands as ``; user-keep: …``; an empty first line appends nothing.

        Mirrors the bash idiom the audit, calibrate and distill sites carried (``IFS= read -r _KEEP <
        "${TMPDIR:-/tmp}/<skill>-state-${CSID}/keep-items" || _KEEP=""``), whose file sits one directory below the temp
        root, which the flat ``--keep-sentinel`` name cannot address.
        """
        state = session_tmp / "audit-state-sess-1"
        state.mkdir()
        (state / "keep-items").write_text(content, encoding="utf-8", newline="")
        assert wsc.main(["--keep-file", str(state / "keep-items"), *_ARGS]) == 0
        assert _preserve_line() == preserve_line

    @pytest.mark.parametrize(
        "relative", [pytest.param("absent/keep-items", id="missing-file"), pytest.param("", id="directory")]
    )
    def test_unreadable_path_appends_nothing(self, session_tmp: Path, relative: str) -> None:
        """A missing or unreadable file is no keep items, matching the ``|| _KEEP=""`` default of the idiom.

        The empty relative path resolves to the temp directory itself, which cannot be read as a file.
        """
        assert wsc.main(["--keep-file", str(session_tmp / relative), *_ARGS]) == 0
        assert _preserve_line() == "- preserve: run-dir=n/a"

    def test_relative_path_resolves_against_cwd(self, session_tmp: Path) -> None:
        """A relative path is read from the working directory, like a shell redirection would."""
        Path("keep-items").write_text("rel\n", encoding="utf-8")
        assert wsc.main(["--keep-file", "keep-items", *_ARGS]) == 0
        assert _preserve_line() == "- preserve: run-dir=n/a; user-keep: rel"

    def test_help_documents_option(self, capsys: pytest.CaptureFixture[str]) -> None:
        """``--help`` lists the option next to ``--keep-sentinel`` so the bin/ help contract names every flag."""
        with pytest.raises(SystemExit):
            wsc.main(["--help"])
        assert "--keep-file <path>" in capsys.readouterr().out


class TestKeepOptionValidation:
    """Rules shared by ``--keep-sentinel`` and ``--keep-file``: bad use is refused, only position zero is an option."""

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param(["--keep-sentinel"], id="sentinel-missing-name"),
            pytest.param(["--keep-sentinel", "../escape", *_ARGS], id="sentinel-path-traversal"),
            pytest.param(["--keep-sentinel", "", *_ARGS], id="sentinel-empty-name"),
            pytest.param(["--keep-file"], id="file-missing-path"),
            pytest.param(["--keep-file", "", *_ARGS], id="file-empty-path"),
            pytest.param(["--keep-file", "a", "--keep-sentinel", "b", *_ARGS], id="file-then-sentinel"),
            pytest.param(["--keep-sentinel", "b", "--keep-file", "a", *_ARGS], id="sentinel-then-file"),
        ],
    )
    def test_rejects_bad_option_use(self, session_tmp: Path, argv: list[str]) -> None:
        """A missing or path-like name, or both keep options together, is refused with exit 2 and writes nothing."""
        assert wsc.main(argv) == 2
        assert not (_STATE / "skill-contract.md").exists()

    @pytest.mark.parametrize("flag", ["--keep-sentinel", "--keep-file"])
    def test_flag_only_parsed_in_leading_position(self, session_tmp: Path, flag: str) -> None:
        """A ``preserve`` value spelled like the flag stays literal text — only position zero is an option."""
        assert wsc.main(["foundry:distill", "gap", "n/a", flag, "next step"]) == 0
        assert _preserve_line() == f"- preserve: {flag}"


class TestLinkedStateDirectory:
    """A `.temp/state` that is a link into the instruction sources."""

    @pytest.mark.skipif(not hasattr(Path, "symlink_to") or wsc.os.name != "posix", reason="needs POSIX symlinks")
    def test_contract_is_not_written_through_a_link_into_the_tree(
        self, project: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A state directory linked to a rules folder leaves a file of that folder untouched and fails the write.

        Writing through the link would overwrite an instruction file of the same name inside the source tree.
        """
        rules = project / "plugins" / "cc_demo" / "rules"
        rules.mkdir(parents=True)
        (rules / "skill-contract.md").write_text("instructions\n", encoding="utf-8")
        (project / ".temp").mkdir()
        (project / _STATE).symlink_to(rules)

        code = wsc.main(_ARGS)

        assert code == 1
        assert (rules / "skill-contract.md").read_text(encoding="utf-8") == "instructions\n"
        assert "inside the tree" in capsys.readouterr().err
