"""Tests for write_skill_contract bin script.

Covers the contract file's exact shape, argument validation, and the fact that the file is written relative to the
caller's working directory.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path, PureWindowsPath

import pytest
import write_skill_contract as wsc

_ARGS = ["foundry:distill", "gap-analysis", ".reports/x", "run-dir=n/a", "Step 3 → Step 4"]


class TestContractFile:
    """Covers the written contract."""

    def test_writes_all_five_fields(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Each argument lands on its own labelled line, in order."""
        monkeypatch.chdir(tmp_path)
        assert wsc.main(_ARGS) == 0
        text = (tmp_path / ".temp" / "state" / "skill-contract.md").read_text(encoding="utf-8")
        assert text.splitlines() == [
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
    def test_labelled_list_appended(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, label: str, items: str, tail: list[str]
    ) -> None:
        """The 7-argument form appends the ledger a `next:` string refers to, one bullet per non-blank line."""
        monkeypatch.chdir(tmp_path)
        assert wsc.main([*_ARGS, label, items]) == 0
        text = (tmp_path / ".temp" / "state" / "skill-contract.md").read_text(encoding="utf-8")
        assert text.splitlines()[-len(tail) :] == tail

    def test_empty_list_omits_block(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """An empty ledger produces no label line, so callers need not branch."""
        monkeypatch.chdir(tmp_path)
        assert wsc.main([*_ARGS, "tried", ""]) == 0
        text = (tmp_path / ".temp" / "state" / "skill-contract.md").read_text(encoding="utf-8")
        assert "tried" not in text
        assert text.splitlines()[-1].startswith("- next:")

    def test_creates_parent_directories(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """.temp/state/ is created when absent."""
        monkeypatch.chdir(tmp_path)
        wsc.main(_ARGS)
        assert (tmp_path / ".temp" / "state").is_dir()

    def test_overwrites_previous_contract(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A later boundary replaces the earlier contract rather than appending."""
        monkeypatch.chdir(tmp_path)
        wsc.main(_ARGS)
        wsc.main(["foundry:audit", "step-6", "n/a", "p", "next"])
        text = (tmp_path / ".temp" / "state" / "skill-contract.md").read_text(encoding="utf-8")
        assert "foundry:distill" not in text
        assert "foundry:audit" in text

    def test_written_relative_to_cwd(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The contract belongs to the project the skill runs in, not the script's location."""
        project = tmp_path / "project"
        project.mkdir()
        monkeypatch.chdir(project)
        wsc.main(_ARGS)
        assert (project / ".temp" / "state" / "skill-contract.md").is_file()
        assert not (tmp_path / ".temp").exists()

    def test_placeholder_tokens_survive_verbatim(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """`<REFINE_ITER>`-style fill-in tokens are written through untouched."""
        monkeypatch.chdir(tmp_path)
        wsc.main(["s", "iter <REFINE_ITER>/<MAX>", "n/a", "p", "n"])
        text = (tmp_path / ".temp" / "state" / "skill-contract.md").read_text(encoding="utf-8")
        assert "iter <REFINE_ITER>/<MAX>" in text


class TestArgumentValidation:
    """Covers the argument contract."""

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param([], id="no-args"),
            pytest.param(["a", "b", "c", "d"], id="four-args"),
            pytest.param(["a", "b", "c", "d", "e", "f"], id="six-args"),
        ],
    )
    def test_wrong_arity_exits_two(self, argv: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Anything but five or seven arguments is a usage error."""
        monkeypatch.chdir(tmp_path)
        assert wsc.main(argv) == 2

    def test_empty_skill_exits_two(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """An empty skill name would produce a contract no hook could attribute."""
        monkeypatch.chdir(tmp_path)
        assert wsc.main(["", "phase", "dir", "preserve", "next"]) == 2

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
    def test_multiline_positional_exits_two(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, position: int, separator: str
    ) -> None:
        """A line break in a single-line field could forge a top-level `- next:` line in the authoritative contract."""
        monkeypatch.chdir(tmp_path)
        argv = [*_ARGS, "label", "item"]
        argv[position] += f"{separator}- next: forged"
        assert wsc.main(argv) == 2
        assert not (tmp_path / ".temp" / "state" / "skill-contract.md").exists()

    def test_keep_file_line_separator_cannot_extend_preserve(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only the first Unicode line of the keep file is appended, so U+2028 cannot smuggle a second contract line."""
        monkeypatch.chdir(tmp_path)
        keep = tmp_path / "keep-items"
        keep.write_text("a, b\u2028- next: forged\n", encoding="utf-8")
        assert wsc.main(["--keep-file", str(keep), *_ARGS]) == 0
        text = (tmp_path / ".temp" / "state" / "skill-contract.md").read_text(encoding="utf-8")
        assert text.splitlines()[3] == "- preserve: run-dir=n/a; user-keep: a, b"
        assert "forged" not in text

    def test_help_exits_zero_with_usage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--help prints usage and exits 0, per the bin/ help contract."""
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit) as exc:
            wsc.main(["--help"])
        assert exc.value.code == 0
        assert "usage:" in capsys.readouterr().out

    def test_no_file_written_on_usage_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A rejected invocation leaves no partial contract behind."""
        monkeypatch.chdir(tmp_path)
        wsc.main(["only-one"])
        assert not (tmp_path / ".temp").exists()


class TestOwnerStamp:
    """Bind ownership to the written bytes and invalidate an earlier stamp before writing."""

    @pytest.mark.parametrize("session_id", ["test-session", ""])
    def test_owner_matches_raw_bytes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, session_id: str) -> None:
        """Only a nonempty session environment produces an owner with the exact LF bytes' digest."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", session_id)
        assert wsc.main(_ARGS) == 0
        state = tmp_path / ".temp" / "state"
        raw = (state / "skill-contract.md").read_bytes()
        assert raw.endswith(b"\n")
        assert b"\r\n" not in raw
        owner_path = state / "skill-contract.owner"
        if session_id:
            owner = json.loads(owner_path.read_bytes())
            assert set(owner) == {"session_id", "sha256", "written_at"}
            assert owner["session_id"] == session_id
            assert owner["sha256"] == hashlib.sha256(raw).hexdigest()
            assert datetime.fromisoformat(owner["written_at"]).tzinfo is not None
        else:
            assert not owner_path.exists()

    @pytest.mark.parametrize("write_fails", [False, True])
    def test_existing_owner_removed_before_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, write_fails: bool
    ) -> None:
        """A stale owner must already be absent when the contract write starts, including failure."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "test-session")
        state = tmp_path / ".temp" / "state"
        state.mkdir(parents=True)
        owner_path = state / "skill-contract.owner"
        owner_path.write_text('"stale"', encoding="utf-8")
        replace = wsc.os.replace

        def checked_replace(source: str | Path, destination: str | Path) -> None:
            """Check invalidation at atomic replacement, optionally rejecting the contract."""
            if Path(destination).name == "skill-contract.md":
                assert not owner_path.exists()
                if write_fails:
                    raise OSError("contract unavailable")
            replace(source, destination)

        monkeypatch.setattr(wsc.os, "replace", checked_replace)
        assert wsc.main(_ARGS) == (1 if write_fails else 0)
        assert not list(state.glob(".*"))
        if write_fails:
            assert not owner_path.exists()
        else:
            assert json.loads(owner_path.read_bytes())["session_id"] == "test-session"

    def test_unset_session_removes_existing_owner(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """An unstamped write cannot inherit the previous session's owner."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        state = tmp_path / ".temp" / "state"
        state.mkdir(parents=True)
        (state / "skill-contract.owner").write_text('"stale"', encoding="utf-8")
        assert wsc.main(_ARGS) == 0
        assert (state / "skill-contract.md").is_file()
        assert not (state / "skill-contract.owner").exists()

    def test_windows_style_working_directory(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Native CWD components and serialized Windows paths survive without separator conversion."""
        project = tmp_path / "Windows workspace" / "project"
        project.mkdir(parents=True)
        monkeypatch.chdir(project)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "test-session")
        run_dir = str(PureWindowsPath("C:/work/project/reports/run"))
        assert wsc.main(["sample:skill", "boundary", run_dir, "preserve", "next"]) == 0
        state = project / ".temp" / "state"
        raw = (state / "skill-contract.md").read_bytes()
        assert f"- run-dir: {run_dir}\n".encode() in raw
        assert json.loads((state / "skill-contract.owner").read_bytes())["sha256"] == hashlib.sha256(raw).hexdigest()
        assert not (tmp_path / ".temp").exists()

    @pytest.mark.parametrize("failure", ["unlink", "contract", "stamp", "temporary"])
    def test_failed_boundary_cannot_retain_owner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
    ) -> None:
        """Invalidation and atomic-write failures preserve complete bytes and remove temporary files."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "test-session")
        state = tmp_path / ".temp" / "state"
        state.mkdir(parents=True)
        contract, owner = state / "skill-contract.md", state / "skill-contract.owner"
        contract.write_bytes(b"previous contract\n")
        owner.write_bytes(b"previous owner\n")
        unlink, replace = Path.unlink, wsc.os.replace

        def checked_unlink(path: Path, missing_ok: bool = False) -> None:
            """Reject invalidation before any replacement can occur."""
            if path.name == "skill-contract.owner" and failure == "unlink":
                raise OSError("owner locked")
            unlink(path, missing_ok=missing_ok)

        def checked_replace(source: str | Path, destination: str | Path) -> None:
            """Reject either atomic commit after its complete temporary bytes are present."""
            target = Path(destination).resolve()
            assert Path(source).parent == target.parent
            assert Path(source).read_bytes()
            if (target == contract and failure == "contract") or (target == owner and failure == "stamp"):
                raise OSError("replace unavailable")
            replace(source, destination)

        def blocked_temporary(**kwargs: object) -> None:
            """Reject temporary-file creation without leaving a partial artifact."""
            raise OSError("temporary unavailable")

        monkeypatch.setattr(Path, "unlink", checked_unlink)
        monkeypatch.setattr(wsc.os, "replace", checked_replace)
        if failure == "temporary":
            monkeypatch.setattr(wsc.tempfile, "NamedTemporaryFile", blocked_temporary)
        assert wsc.main(_ARGS) == 1
        assert not list(state.glob(".*"))
        if failure == "unlink":
            assert contract.read_bytes() == b"previous contract\n"
            assert owner.read_bytes() == b"previous owner\n"
        else:
            assert not owner.exists()
            if failure in ("contract", "temporary"):
                assert contract.read_bytes() == b"previous contract\n"
            else:
                assert contract.read_bytes().startswith(b"## Active Skill Contract\n")

    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            pytest.param([], 2, id="invalid-arity"),
            pytest.param(["", "phase", "dir", "preserve", "next"], 2, id="empty-skill"),
            pytest.param(["--help"], 0, id="help"),
        ],
    )
    def test_validation_exit_invalidates_prior_owner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str], expected: int
    ) -> None:
        """Every invocation invalidates earlier ownership, even when no new contract is written."""
        monkeypatch.chdir(tmp_path)
        state = tmp_path / ".temp" / "state"
        state.mkdir(parents=True)
        contract = state / "skill-contract.md"
        contract.write_bytes(b"previous contract\n")
        owner = state / "skill-contract.owner"
        owner.write_text(
            json.dumps({"session_id": "test-session", "sha256": hashlib.sha256(contract.read_bytes()).hexdigest()})
        )
        try:
            result = wsc.main(argv)
        except SystemExit as exc:
            result = exc.code
        assert result == expected
        assert not owner.exists()
        assert contract.read_bytes() == b"previous contract\n"


class TestBoundaryMetadata:
    """Validate optional metadata without changing positional or ownership contracts."""

    @pytest.mark.parametrize("with_list", [False, True])
    def test_metadata_appended_before_owner_stamp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_list: bool
    ) -> None:
        """The owner digest includes each metadata line after any labelled list."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "test-session")
        positional = [*_ARGS, "retained", "one item"] if with_list else _ARGS
        assert wsc.main([*positional, "--", "calls_ahead=60", "drop=temporary notes", "interactive=false"]) == 0
        state = tmp_path / ".temp" / "state"
        raw = (state / "skill-contract.md").read_bytes()
        assert raw.decode("utf-8").splitlines()[-4:] == [
            "- gate-metadata:",
            "    - calls_ahead: 60",
            "    - drop: temporary notes",
            "    - interactive: false",
        ]
        if with_list:
            assert "- retained:\n    - one item\n- gate-metadata:\n    - calls_ahead:" in raw.decode("utf-8")
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
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tail: list[str]
    ) -> None:
        """Rejected metadata prints usage and never creates a partial contract."""
        monkeypatch.chdir(tmp_path)
        assert wsc.main([*_ARGS, "--", *tail]) == 2
        assert "usage:" in capsys.readouterr().err
        assert not (tmp_path / ".temp" / "state" / "skill-contract.md").exists()

    @pytest.mark.parametrize(
        "positional",
        [pytest.param(_ARGS[:4], id="four-positional"), pytest.param([*_ARGS, "extra"], id="six-positional")],
    )
    def test_metadata_does_not_change_positional_arity(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, positional: list[str]
    ) -> None:
        """Only five or seven arguments before the delimiter remain valid."""
        monkeypatch.chdir(tmp_path)
        assert wsc.main([*positional, "--", "calls_ahead=0"]) == 2

    def test_zero_calls_empty_drop_and_embedded_help(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Zero is valid and embedded help text is preserved without triggering help."""
        monkeypatch.chdir(tmp_path)
        assert wsc.main([*_ARGS, "--", "calls_ahead=0", "drop="]) == 0
        contract = tmp_path / ".temp" / "state" / "skill-contract.md"
        assert contract.read_text(encoding="utf-8").endswith("- gate-metadata:\n    - calls_ahead: 0\n    - drop: \n")
        assert wsc.main([*_ARGS, "--", "drop=contains --help and -h"]) == 0
        assert contract.read_text(encoding="utf-8").endswith("- gate-metadata:\n    - drop: contains --help and -h\n")


@pytest.mark.parametrize("position", range(7))
def test_reserved_sentinel_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, position: int) -> None:
    """Every positional field rejects a stripped reserved sentinel line."""
    monkeypatch.chdir(tmp_path)
    args = [*_ARGS, "retained", "item"]
    args[position] += "\n  - gate-metadata:  \n"
    assert wsc.main(args) == 2
    assert not wsc._CONTRACT.exists()


def test_owner_hashes_intended_bytes_after_overwrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An intervening writer cannot make this session own its replacement bytes."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "test-session")
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


def test_reserved_label_cannot_forge_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A labelled list cannot render the dedicated gate block from positional prose."""
    monkeypatch.chdir(tmp_path)
    assert wsc.main([*_ARGS, "gate-metadata", "calls_ahead: 99"]) == 2
    assert not wsc._CONTRACT.exists()


def test_partial_temporary_write_cleaned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An interrupted temporary write leaves complete old bytes and removes its partial file."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "test-session")
    wsc._CONTRACT.parent.mkdir(parents=True)
    wsc._CONTRACT.write_bytes(b"previous contract\n")
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
    assert wsc._CONTRACT.read_bytes() == b"previous contract\n"
    assert not wsc._OWNER.exists()
    assert not list(wsc._CONTRACT.parent.glob(".*"))


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


class TestKeepSentinel:
    """Covers the leading ``--keep-sentinel <name>`` option that replaced the per-site bash keep append."""

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
        rc = wsc.main(["--keep-sentinel", "dev-keep", *_ARGS])
        assert rc == 0
        assert (Path(".temp/state/skill-contract.md")).read_text(encoding="utf-8").splitlines()[3] == preserve_line

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param(["--keep-sentinel"], id="missing-name"),
            pytest.param(["--keep-sentinel", "../escape", *_ARGS], id="path-traversal"),
            pytest.param(["--keep-sentinel", "", *_ARGS], id="empty-name"),
        ],
    )
    def test_rejects_bad_name(self, session_tmp: Path, argv: list[str]) -> None:
        """A sentinel name that is missing or path-like is refused with exit 2 and no contract written."""
        rc = wsc.main(argv)
        assert rc == 2
        assert not Path(".temp/state/skill-contract.md").exists()

    def test_flag_only_parsed_in_leading_position(self, session_tmp: Path) -> None:
        """A ``preserve`` value spelled like the flag stays literal text — only position zero is an option."""
        rc = wsc.main(["foundry:distill", "gap", "n/a", "--keep-sentinel", "next step"])
        assert rc == 0
        assert Path(".temp/state/skill-contract.md").read_text(encoding="utf-8").splitlines()[3] == (
            "- preserve: --keep-sentinel"
        )


class TestKeepFile:
    """Covers the leading ``--keep-file <path>`` option for keep items held in a per-session state directory."""

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
        rc = wsc.main(["--keep-file", str(state / "keep-items"), *_ARGS])
        assert rc == 0
        assert Path(".temp/state/skill-contract.md").read_text(encoding="utf-8").splitlines()[3] == preserve_line

    @pytest.mark.parametrize(
        "make_path",
        [
            pytest.param(lambda tmp: tmp / "absent" / "keep-items", id="missing-file"),
            pytest.param(lambda tmp: tmp, id="directory"),
        ],
    )
    def test_unreadable_path_appends_nothing(self, session_tmp: Path, make_path) -> None:
        """A missing or unreadable file is no keep items, matching the ``|| _KEEP=""`` default of the idiom."""
        rc = wsc.main(["--keep-file", str(make_path(session_tmp)), *_ARGS])
        assert rc == 0
        assert Path(".temp/state/skill-contract.md").read_text(encoding="utf-8").splitlines()[3] == (
            "- preserve: run-dir=n/a"
        )

    def test_relative_path_resolves_against_cwd(self, session_tmp: Path) -> None:
        """A relative path is read from the working directory, like a shell redirection would."""
        Path("keep-items").write_text("rel\n", encoding="utf-8")
        rc = wsc.main(["--keep-file", "keep-items", *_ARGS])
        assert rc == 0
        assert Path(".temp/state/skill-contract.md").read_text(encoding="utf-8").splitlines()[3] == (
            "- preserve: run-dir=n/a; user-keep: rel"
        )

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param(["--keep-file"], id="missing-path"),
            pytest.param(["--keep-file", "", *_ARGS], id="empty-path"),
            pytest.param(["--keep-file", "a", "--keep-sentinel", "b", *_ARGS], id="file-then-sentinel"),
            pytest.param(["--keep-sentinel", "b", "--keep-file", "a", *_ARGS], id="sentinel-then-file"),
        ],
    )
    def test_rejects_bad_option_use(self, session_tmp: Path, argv: list[str]) -> None:
        """A missing path, or both keep options together, is refused with exit 2 and no contract written."""
        rc = wsc.main(argv)
        assert rc == 2
        assert not Path(".temp/state/skill-contract.md").exists()

    def test_flag_only_parsed_in_leading_position(self, session_tmp: Path) -> None:
        """A ``preserve`` value spelled like the flag stays literal text — only position zero is an option."""
        rc = wsc.main(["foundry:distill", "gap", "n/a", "--keep-file", "next step"])
        assert rc == 0
        assert Path(".temp/state/skill-contract.md").read_text(encoding="utf-8").splitlines()[3] == (
            "- preserve: --keep-file"
        )

    def test_help_documents_option(self, capsys: pytest.CaptureFixture[str]) -> None:
        """``--help`` lists the option next to ``--keep-sentinel`` so the bin/ help contract names every flag."""
        with pytest.raises(SystemExit):
            wsc.main(["--help"])
        assert "--keep-file <path>" in capsys.readouterr().out


class TestLinkedStateDirectory:
    """Covers a `.temp/state` that is a link into the instruction sources."""

    @pytest.mark.skipif(not hasattr(Path, "symlink_to") or wsc.os.name != "posix", reason="needs POSIX symlinks")
    def test_contract_is_not_written_through_a_link_into_the_tree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A state directory linked to a rules folder leaves a file of that folder untouched and fails the write."""
        rules = tmp_path / "plugins" / "cc_demo" / "rules"
        rules.mkdir(parents=True)
        (rules / "skill-contract.md").write_text("instructions\n", encoding="utf-8")
        (tmp_path / ".temp").mkdir()
        (tmp_path / ".temp" / "state").symlink_to(rules)
        monkeypatch.chdir(tmp_path)
        code = wsc.main(_ARGS)
        assert (
            code,
            (rules / "skill-contract.md").read_text(encoding="utf-8"),
            "inside the tree" in capsys.readouterr().err,
        ) == (
            1,
            "instructions\n",
            True,
        )
