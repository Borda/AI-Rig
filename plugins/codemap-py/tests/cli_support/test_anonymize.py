"""Tests for ``bin/anonymize.py`` — free-text scrubbing, export separation, salt safety.

Covers:

- ``error`` / ``stderr`` free-text fields: qualified names embedded in prose are
  pseudonymized token-by-token while surrounding text survives.
- ``intent`` / ``target`` command fields: every identifying token is pseudonymized —
  including the dot-free ones a whole-value "is this qualified?" gate exported verbatim —
  while flags, separators and file extensions survive so the shape stays readable.
- ``session`` / ``hook_session`` and the exported filename: no raw session id leaves.
- ``not_covered`` lists: each element scrubbed individually (qualified elements
  hashed, plain diagnostic labels untouched).
- Export-dir separation: the default output target is the dedicated export dir,
  never the salt directory.
- Salt safety: writing into any directory that holds a ``.salt`` file is refused
  with a nonzero exit code.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import anonymize

_SALT = b"x" * 32
_BIN = Path(anonymize.__file__)


def test_directory_input_preserves_runtime_topology_and_excludes_salt(tmp_path: Path) -> None:
    """Directory exports retain runtime subtrees without exporting the local reversal salt."""
    logs = tmp_path / "logs"
    (logs / "claude").mkdir(parents=True)
    (logs / "direct").mkdir()
    (logs / "claude" / "cli_sensitive-session.jsonl").write_text('{"session":"sensitive-session"}\n')
    (logs / "direct" / "tools_sensitive-session.jsonl").write_text('{"session":"sensitive-session"}\n')
    output = tmp_path / "export"
    salt = logs / ".salt"

    assert anonymize.main(["--input", str(logs), "--out-dir", str(output), "--salt", str(salt)]) == 0
    exported = sorted(path.relative_to(output).as_posix() for path in output.rglob("*.jsonl"))
    assert exported == [
        f"claude/cli_{anonymize._pseudo('sensitive-session', anonymize._load_salt(salt))}-anon.jsonl",
        f"direct/tools_{anonymize._pseudo('sensitive-session', anonymize._load_salt(salt))}-anon.jsonl",
    ]
    # Helper-independent leak check: the raw id must not survive in any exported
    # name or body even if `_pseudo` degraded to identity.
    assert all("sensitive-session" not in name for name in exported)
    assert all("sensitive-session" not in path.read_text() for path in output.rglob("*.jsonl"))
    assert not list(output.rglob(".salt"))


# ---------------------------------------------------------------------------
# Free-text error / stderr scrubbing
# ---------------------------------------------------------------------------


def test_error_string_with_module_name_is_hashed() -> None:
    """A module name embedded in a free-text error is replaced; prose survives."""
    record = {"cmd": "rdeps", "result": {"error": "module pkg.auth.core not indexed"}}
    out = anonymize.anonymize_record(record, _SALT)
    scrubbed = out["result"]["error"]
    assert "pkg.auth.core" not in scrubbed
    assert scrubbed.startswith("module sym_")
    assert scrubbed.endswith(" not indexed")


def test_error_double_colon_qualname_is_hashed() -> None:
    """A ``module::symbol`` token in error prose is pseudonymized."""
    record = {"result": {"error": "call to pkg.auth::login failed at line 3"}}
    out = anonymize.anonymize_record(record, _SALT)
    scrubbed = out["result"]["error"]
    assert "pkg.auth::login" not in scrubbed
    assert "sym_" in scrubbed
    assert scrubbed.endswith(" failed at line 3")


def test_error_with_multiple_qualnames_hashes_each() -> None:
    """Every qualified token in one error string is replaced independently."""
    record = {"result": {"error": "pkg.a.b calls pkg.c.d but pkg.c.d is missing"}}
    out = anonymize.anonymize_record(record, _SALT)
    scrubbed = out["result"]["error"]
    assert "pkg.a.b" not in scrubbed
    assert "pkg.c.d" not in scrubbed
    # Repeated original -> identical pseudonym (stable within salt).
    first = anonymize._pseudo("pkg.a.b", _SALT)
    second = anonymize._pseudo("pkg.c.d", _SALT)
    assert scrubbed == f"{first} calls {second} but {second} is missing"


def test_error_without_qualname_unchanged() -> None:
    """Free text with no qualified names passes through verbatim."""
    record = {"result": {"error": "index is not valid json"}}
    out = anonymize.anonymize_record(record, _SALT)
    assert out["result"]["error"] == "index is not valid json"


def test_stderr_field_is_hashed() -> None:
    """A qualified name in a captured stderr/traceback field is pseudonymized."""
    record = {"stderr": "Traceback: pkg.auth.core.login raised ValueError"}
    out = anonymize.anonymize_record(record, _SALT)
    scrubbed = out["stderr"]
    assert "pkg.auth.core.login" not in scrubbed
    assert "sym_" in scrubbed
    assert scrubbed.startswith("Traceback: sym_")


def test_stderr_nested_in_result_is_hashed() -> None:
    """A ``stderr`` field nested inside ``result`` is scrubbed too."""
    record = {"result": {"stderr": "error in module.sub.func"}}
    out = anonymize.anonymize_record(record, _SALT)
    assert "module.sub.func" not in out["result"]["stderr"]
    assert "sym_" in out["result"]["stderr"]


def test_non_string_error_field_left_alone() -> None:
    """A non-string ``error`` value (e.g. bool/None) is not treated as free text."""
    record = {"result": {"error": None, "count": 3}}
    out = anonymize.anonymize_record(record, _SALT)
    assert out["result"]["error"] is None
    assert out["result"]["count"] == 3


# ---------------------------------------------------------------------------
# not_covered list scrubbing
# ---------------------------------------------------------------------------


def test_not_covered_list_hashed_per_element() -> None:
    """Qualified ``not_covered`` elements are hashed; plain labels are preserved."""
    record = {
        "result": {
            "not_covered": ["importlib.import_module", "lazy-loading", "pkg.mod::fn"],
        }
    }
    out = anonymize.anonymize_record(record, _SALT)
    nc = out["result"]["not_covered"]
    assert nc[0].startswith("sym_")  # importlib.import_module -> qualified -> hashed
    assert nc[1] == "lazy-loading"  # plain label preserved
    assert nc[2].startswith("sym_")  # pkg.mod::fn -> qualified -> hashed
    assert "importlib.import_module" not in nc
    assert "pkg.mod::fn" not in nc


def test_not_covered_stable_per_element() -> None:
    """Each ``not_covered`` element maps to the same pseudonym as a standalone name."""
    record = {"result": {"not_covered": ["pkg.a.b"]}}
    out = anonymize.anonymize_record(record, _SALT)
    assert out["result"]["not_covered"][0] == anonymize._pseudo("pkg.a.b", _SALT)


def test_not_covered_top_level_list() -> None:
    """A ``not_covered`` list at the record top level is scrubbed as well."""
    record = {"not_covered": ["a.b.c", "dynamic-dispatch"]}
    out = anonymize.anonymize_record(record, _SALT)
    assert out["not_covered"][0].startswith("sym_")
    assert out["not_covered"][1] == "dynamic-dispatch"


def test_not_covered_non_string_elements_survive() -> None:
    """Non-string ``not_covered`` elements are passed through untouched."""
    record = {"result": {"not_covered": [42, None, "pkg.a.b"]}}
    out = anonymize.anonymize_record(record, _SALT)
    nc = out["result"]["not_covered"]
    assert nc[0] == 42
    assert nc[1] is None
    assert nc[2].startswith("sym_")


# ---------------------------------------------------------------------------
# Command fields: intent / target
# ---------------------------------------------------------------------------


class TestCommandFieldLeak:
    """A ``target``/``intent`` value carries project data whether or not it has a dot.

    The gate these tests replace pseudonymized the field only when the *whole* value looked like a qualified name, so a
    dot-free search command was exported verbatim while a path with any dot in it collapsed to one opaque token.
    """

    def test_dot_free_command_is_scrubbed(self) -> None:
        """A search command with no qualified name in it must not survive verbatim."""
        record = {"tool": "Bash", "target": "grep -rn internal_secret_name src/"}

        out = anonymize.anonymize_record(record, _SALT)

        assert "internal_secret_name" not in out["target"]
        assert out["target"].startswith("grep -rn sym_")
        assert out["target"].endswith(" src/")

    def test_dot_free_grep_pattern_is_scrubbed(self) -> None:
        """A bare Grep pattern is project data too, dots or not."""
        out = anonymize.anonymize_record({"tool": "Grep", "target": "validate_token"}, _SALT)

        assert out["target"] == anonymize._pseudo("validate_token", _SALT)

    def test_intent_prose_is_scrubbed(self) -> None:
        """Skill arguments are the user's own words — every identifying token is hashed."""
        out = anonymize.anonymize_record({"layer": "skill", "intent": "who calls validate_token"}, _SALT)

        assert "validate_token" not in out["intent"]
        assert out["intent"].count("sym_") == 3  # who / calls / validate_token

    def test_path_keeps_its_shape_and_extension(self) -> None:
        """A Read path is scrubbed per segment, not collapsed to one token."""
        out = anonymize.anonymize_record({"tool": "Read", "target": "/Users/someone/proj/src/auth.py"}, _SALT)

        target = out["target"]
        assert "someone" not in target and "auth" not in target
        assert target.endswith(".py"), "the file type is diagnostic and must survive"
        assert target.count("/") == 5, "the path shape must survive"
        assert "/src/" in target, "a conventional directory name is not identifying"

    def test_generic_tool_tokens_survive(self) -> None:
        """The tools the telemetry exists to measure stay readable in the export."""
        out = anonymize.anonymize_record({"tool": "Bash", "target": "rg -n 'import' tests/"}, _SALT)

        assert out["target"] == "rg -n 'import' tests/"

    def test_command_pseudonyms_are_stable(self) -> None:
        """The same identifier maps to one pseudonym, so repeat-read counting survives."""
        first = anonymize.anonymize_record({"target": "/proj/auth_service.py"}, _SALT)["target"]
        second = anonymize.anonymize_record({"target": "/other/auth_service.py"}, _SALT)["target"]

        assert first.rsplit("/", 1)[-1] == second.rsplit("/", 1)[-1]

    def test_non_string_target_left_alone(self) -> None:
        """A non-string ``target`` is not treated as a command."""
        out = anonymize.anonymize_record({"target": None, "count": 2}, _SALT)

        assert out["target"] is None
        assert out["count"] == 2


# ---------------------------------------------------------------------------
# Session identifiers: record fields and the exported filename
# ---------------------------------------------------------------------------


class TestSessionPseudonymization:
    """The session id correlates an export back to the machine that produced it."""

    def test_session_field_is_pseudonymized(self) -> None:
        """The raw session id never survives in a record."""
        out = anonymize.anonymize_record({"layer": "tool", "session": "8f14e45f-ea"}, _SALT)

        assert out["session"] == anonymize._pseudo("8f14e45f-ea", _SALT)

    def test_hook_session_field_is_pseudonymized(self) -> None:
        """The skill layer's second session field is covered too."""
        out = anonymize.anonymize_record({"layer": "skill", "hook_session": "hook-sid-9"}, _SALT)

        assert out["hook_session"] == anonymize._pseudo("hook-sid-9", _SALT)

    def test_session_pseudonym_joins_across_layers(self) -> None:
        """One session id maps to one pseudonym, so cross-layer joins still work."""
        cli = anonymize.anonymize_record({"layer": "cli", "session": "sid-7"}, _SALT)
        tool = anonymize.anonymize_record({"layer": "tool", "session": "sid-7"}, _SALT)

        assert cli["session"] == tool["session"]

    def test_empty_session_stays_empty(self) -> None:
        """An unseeded session stays an empty string rather than becoming a pseudonym."""
        out = anonymize.anonymize_record({"layer": "skill", "hook_session": ""}, _SALT)

        assert out["hook_session"] == ""

    def test_shard_filename_drops_the_raw_session_id(self, tmp_path: Path) -> None:
        """End-to-end: the exported filename carries a pseudonym, not the session id."""
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        src = log_dir / "tools_8f14e45f-ea.jsonl"
        src.write_text('{"layer":"tool","session":"8f14e45f-ea","tool":"Read","target":"/p/auth.py"}\n')
        export_dir = tmp_path / "export"

        rc = anonymize.main(["--input", str(src), "--out-dir", str(export_dir), "--salt", str(log_dir / ".salt")])

        assert rc == 0
        written = [path.name for path in export_dir.iterdir()]
        # Derive the expectation from the salt the run actually used. _load_salt mints a
        # fresh random salt when the file is absent, so a fixed module-level salt names a
        # pseudonym this run could never produce.
        run_salt = anonymize._load_salt(log_dir / ".salt")
        assert written == [f"tools_{anonymize._pseudo('8f14e45f-ea', run_salt)}-anon.jsonl"]
        record = json.loads((export_dir / written[0]).read_text().strip())
        assert "8f14e45f-ea" not in json.dumps(record)

    def test_unsuffixed_shard_name_is_unchanged(self, tmp_path: Path) -> None:
        """A shard with no session suffix keeps its plain ``<layer>-anon.jsonl`` name."""
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        src = log_dir / "cli.jsonl"
        src.write_text('{"cmd":"rdeps","args":{"module":"pkg.auth"}}\n')
        export_dir = tmp_path / "export"

        rc = anonymize.main(["--input", str(src), "--out-dir", str(export_dir), "--salt", str(log_dir / ".salt")])

        assert rc == 0
        assert (export_dir / "cli-anon.jsonl").exists()

    def test_refusal_path_creates_no_salt(self, tmp_path: Path) -> None:
        """Resolving the output name must not create the salt before the refusal check."""
        src = tmp_path / "tools_sid.jsonl"
        src.write_text('{"layer":"tool"}\n')
        unsafe_dir = tmp_path / "logs"
        unsafe_dir.mkdir()
        (unsafe_dir / ".salt").write_text("00" * 32)
        salt_file = tmp_path / "keep" / ".salt"

        rc = anonymize.main(["--input", str(src), "--out-dir", str(unsafe_dir), "--salt", str(salt_file)])

        assert rc == anonymize._EXIT_UNSAFE_OUT_DIR
        assert not salt_file.exists(), "a refused run must not leave a salt file behind"


# ---------------------------------------------------------------------------
# Regression: existing fields still anonymized
# ---------------------------------------------------------------------------


def test_args_module_still_anonymized() -> None:
    """The pre-existing args-payload pseudonymization is unaffected by hardening."""
    record = {"cmd": "rdeps", "args": {"module": "pkg.auth"}}
    out = anonymize.anonymize_record(record, _SALT)
    assert out["args"]["module"].startswith("sym_")
    assert out["cmd"] == "rdeps"


# ---------------------------------------------------------------------------
# Output-path resolution and export separation
# ---------------------------------------------------------------------------


def test_default_out_dir_is_export_not_salt_dir() -> None:
    """The default resolved output lives under the export dir, never the log/salt dir."""
    resolved = anonymize._resolve_output(Path("logs/cli.jsonl"), None, None)
    assert resolved == Path(anonymize.DEFAULT_OUT_DIR) / "cli-anon.jsonl"
    assert "export" in anonymize.DEFAULT_OUT_DIR
    assert "logs" not in resolved.parent.name


def test_explicit_out_dir_used() -> None:
    """An explicit ``--out-dir`` places the derived ``-anon`` file inside it."""
    resolved = anonymize._resolve_output(Path("logs/skills.jsonl"), "my-export", None)
    assert resolved == Path("my-export") / "skills-anon.jsonl"


def test_explicit_output_wins() -> None:
    """An explicit ``--output`` overrides ``--out-dir`` derivation."""
    resolved = anonymize._resolve_output(Path("logs/cli.jsonl"), "ignored", "out/custom.jsonl")
    assert resolved == Path("out/custom.jsonl")


def test_cli_default_target_has_no_salt(tmp_path: Path) -> None:
    """End-to-end: default export target is created and separate from the salt dir."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    src = log_dir / "cli.jsonl"
    src.write_text('{"cmd":"rdeps","args":{"module":"pkg.auth"}}\n')
    salt_file = log_dir / ".salt"  # salt sits in the log dir, as in production
    export_dir = tmp_path / "export"

    rc = anonymize.main(["--input", str(src), "--out-dir", str(export_dir), "--salt", str(salt_file)])
    assert rc == 0
    out_file = export_dir / "cli-anon.jsonl"
    assert out_file.exists()
    assert not (export_dir / ".salt").exists()  # salt never copied into export dir
    record = json.loads(out_file.read_text().strip())
    assert record["args"]["module"].startswith("sym_")


# ---------------------------------------------------------------------------
# Salt-safety refusal
# ---------------------------------------------------------------------------


def test_dir_has_salt_detects_file(tmp_path: Path) -> None:
    """_dir_has_salt is True only when a .salt file is present."""
    assert not anonymize._dir_has_salt(tmp_path)
    (tmp_path / ".salt").write_text("00")
    assert anonymize._dir_has_salt(tmp_path)


def test_refuse_when_out_dir_contains_salt(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Writing into a directory that already holds a .salt file is refused (exit 2)."""
    src = tmp_path / "cli.jsonl"
    src.write_text('{"cmd":"rdeps","args":{"module":"pkg.auth"}}\n')
    unsafe_dir = tmp_path / "logs"
    unsafe_dir.mkdir()
    (unsafe_dir / ".salt").write_text("00" * 32)
    salt_file = tmp_path / "keep" / ".salt"

    rc = anonymize.main(["--input", str(src), "--out-dir", str(unsafe_dir), "--salt", str(salt_file)])
    assert rc == anonymize._EXIT_UNSAFE_OUT_DIR
    assert not (unsafe_dir / "cli-anon.jsonl").exists()  # nothing written
    err = capsys.readouterr().err
    assert "refusing to write" in err
    assert ".salt" in err


def test_refuse_when_explicit_output_dir_contains_salt(tmp_path: Path) -> None:
    """The salt refusal also applies when an explicit ``--output`` targets a salt dir."""
    src = tmp_path / "cli.jsonl"
    src.write_text('{"cmd":"rdeps"}\n')
    unsafe_dir = tmp_path / "logs"
    unsafe_dir.mkdir()
    (unsafe_dir / ".salt").write_text("00" * 32)
    salt_file = tmp_path / "keep" / ".salt"

    rc = anonymize.main(["--input", str(src), "--output", str(unsafe_dir / "cli-anon.jsonl"), "--salt", str(salt_file)])
    assert rc == anonymize._EXIT_UNSAFE_OUT_DIR
    assert not (unsafe_dir / "cli-anon.jsonl").exists()


def test_cli_subprocess_refusal_exit_code(tmp_path: Path) -> None:
    """Running the script as a subprocess returns a nonzero exit on salt collision."""
    src = tmp_path / "cli.jsonl"
    src.write_text('{"cmd":"rdeps"}\n')
    unsafe_dir = tmp_path / "logs"
    unsafe_dir.mkdir()
    (unsafe_dir / ".salt").write_text("00" * 32)

    proc = subprocess.run(
        [
            sys.executable,
            str(_BIN),
            "--input",
            str(src),
            "--out-dir",
            str(unsafe_dir),
            "--salt",
            str(tmp_path / "keep" / ".salt"),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert proc.returncode == anonymize._EXIT_UNSAFE_OUT_DIR
    assert "refusing to write" in proc.stderr


# ---------------------------------------------------------------------------
# Salt file permissions — the "opaque without salt" guarantee assumes secrecy
# ---------------------------------------------------------------------------


_skip_windows_posix = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX file-mode bits are not meaningful on Windows"
)


@_skip_windows_posix
def test_salt_file_created_0600(tmp_path: Path) -> None:
    """A freshly created salt file is owner-only (0o600) so no local user can reverse pseudonyms."""
    salt_file = tmp_path / ".salt"
    anonymize._load_salt(salt_file)
    mode = stat.S_IMODE(salt_file.stat().st_mode)
    assert mode == 0o600, f"salt file mode is {oct(mode)}, expected 0o600"


@_skip_windows_posix
def test_salt_file_0600_regardless_of_umask(tmp_path: Path) -> None:
    """The 0o600 mode holds even under a permissive umask that would otherwise widen it."""
    salt_file = tmp_path / ".salt"
    old_umask = os.umask(0o000)  # most permissive — default write_text would yield 0o666
    try:
        anonymize._load_salt(salt_file)
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE(salt_file.stat().st_mode) == 0o600


def test_load_salt_defers_to_existing_salt(tmp_path: Path) -> None:
    """An existing salt is read as-is (never overwritten), so its value stays stable across calls."""
    salt_file = tmp_path / ".salt"
    salt_file.write_text(("ab" * 32))
    assert anonymize._load_salt(salt_file) == bytes.fromhex("ab" * 32)


def test_load_salt_works_without_posix_fchmod(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Salt creation stays usable on platforms that expose no ``os.fchmod``."""
    monkeypatch.delattr(anonymize.os, "fchmod", raising=False)
    assert len(anonymize._load_salt(tmp_path / ".salt")) == 32
