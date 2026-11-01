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

Everything runs in-process on files under ``tmp_path``; the two tests that launch the script as a real subprocess keep
the process call because the locale handling and the exit status are what they assert.
"""

from __future__ import annotations

import functools
import json
import operator
import os
import stat
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

import anonymize
import pytest

_SALT = b"x" * 32
_BIN = Path(anonymize.__file__)

_skip_windows_posix = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX file-mode bits are not meaningful on Windows"
)


class _SaltedDir(NamedTuple):
    """An input shard, an output directory that already holds a ``.salt``, and a separate salt path."""

    src: Path
    unsafe_dir: Path
    salt_file: Path


@pytest.fixture
def salted_dir(tmp_path: Path) -> _SaltedDir:
    """Provide a shard plus an output directory that holds a ``.salt`` file, for the salt-refusal tests."""
    src = tmp_path / "cli.jsonl"
    src.write_text('{"cmd":"rdeps"}\n')
    unsafe_dir = tmp_path / "logs"
    unsafe_dir.mkdir()
    (unsafe_dir / ".salt").write_text("00" * 32)
    return _SaltedDir(src, unsafe_dir, tmp_path / "keep" / ".salt")


@pytest.fixture
def open_umask() -> Iterator[None]:
    """Run the test under the most permissive umask, restoring the previous one afterwards."""
    old_umask = os.umask(0o000)
    yield
    os.umask(old_umask)


def _dig(record: dict, path: tuple[str, ...]) -> object:
    """Return the value at the key *path* inside a nested record."""
    return functools.reduce(operator.getitem, path, record)


def _export(tmp_path: Path, shard_name: str, content: str) -> tuple[int, Path, Path]:
    """Write one shard under ``tmp_path/logs`` and export it; return ``(exit code, export dir, log dir)``.

    The salt sits in the log directory, as in production, and the export goes to a separate ``export`` directory.
    """
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / shard_name).write_text(content)
    export_dir = tmp_path / "export"
    rc = anonymize.main(
        ["--input", str(log_dir / shard_name), "--out-dir", str(export_dir), "--salt", str(log_dir / ".salt")]
    )
    return rc, export_dir, log_dir


class TestCliSubprocess:
    """The script run as a real child process: console encoding and exit status."""

    def test_exports_utf8_under_non_utf8_locale(self, tmp_path: Path) -> None:
        """UTF-8 logs and the success arrow survive a legacy console and locale without a failed export status.

        The child runs with a cp1252 console and the C locale; the export must still decode as UTF-8, use LF newlines,
        and keep the non-ASCII note while pseudonymizing the module name.
        """
        source = tmp_path / "cli.jsonl"
        source.write_text('{"note":"café 漢字","module":"private.module"}\n', encoding="utf-8", newline="\n")
        output = tmp_path / "export" / "cli-anon.jsonl"
        env = {**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0", "LC_ALL": "C"}
        result = subprocess.run(
            [
                sys.executable,
                str(_BIN),
                "--input",
                str(source),
                "--output",
                str(output),
                "--salt",
                str(tmp_path / ".salt"),
            ],
            env=env,
            capture_output=True,
            check=False,
            timeout=15,
        )

        assert result.returncode == 0, result.stderr
        assert "1 records →" in result.stdout.decode("utf-8")
        exported = output.read_bytes()
        assert b"\r\n" not in exported
        record = json.loads(exported.decode("utf-8"))
        assert record["note"] == "café 漢字"
        assert record["module"].startswith("sym_")

    def test_salt_collision_exits_with_refusal_code(self, tmp_path: Path, salted_dir: _SaltedDir) -> None:
        """Running the script as a subprocess returns the refusal exit code on salt collision.

        The shell-visible status is what the debrief skill checks, so it is asserted through a real process.
        """
        proc = subprocess.run(
            [
                sys.executable,
                str(_BIN),
                *("--input", str(salted_dir.src)),
                *("--out-dir", str(salted_dir.unsafe_dir)),
                *("--salt", str(salted_dir.salt_file)),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        assert proc.returncode == anonymize._EXIT_UNSAFE_OUT_DIR
        assert "refusing to write" in proc.stderr


class TestDirectoryInput:
    """Directory inputs: runtime subtrees are kept, the salt and symlinks are not exported."""

    def test_preserves_runtime_topology_and_excludes_salt(self, tmp_path: Path) -> None:
        """Directory exports retain runtime subtrees without exporting the local reversal salt.

        A log tree with ``claude/`` and ``direct/`` shards named after one session id must export both under the same
        subfolders with pseudonymized names, and the ``.salt`` beside them must not travel.
        """
        logs = tmp_path / "logs"
        (logs / "claude").mkdir(parents=True)
        (logs / "direct").mkdir()
        (logs / "claude" / "cli_sensitive-session.jsonl").write_text('{"session":"sensitive-session"}\n')
        (logs / "direct" / "tools_sensitive-session.jsonl").write_text('{"session":"sensitive-session"}\n')
        output = tmp_path / "export"
        salt = logs / ".salt"

        assert anonymize.main(["--input", str(logs), "--out-dir", str(output), "--salt", str(salt)]) == 0
        exported = sorted(path.relative_to(output).as_posix() for path in output.rglob("*.jsonl"))
        pseudo = anonymize._pseudo("sensitive-session", anonymize._load_salt(salt))
        assert exported == [f"claude/cli_{pseudo}-anon.jsonl", f"direct/tools_{pseudo}-anon.jsonl"]
        # Helper-independent leak check: the raw id must not survive in any exported
        # name or body even if `_pseudo` degraded to identity.
        assert all("sensitive-session" not in name for name in exported)
        assert all("sensitive-session" not in path.read_text() for path in output.rglob("*.jsonl"))
        assert not list(output.rglob(".salt"))

    @_skip_windows_posix
    def test_skips_symlinked_jsonl(self, tmp_path: Path) -> None:
        """A symlink inside the input tree is not followed, so a log outside the tree is never exported.

        A shard symlinked to a ``.jsonl`` elsewhere (another project's log, a file the user never selected) would be
        read and anonymized into the export; the regular shard beside it still exports.
        """
        outside = tmp_path / "outside.jsonl"
        outside.write_text('{"session":"leaked-session"}\n')
        logs = tmp_path / "logs"
        logs.mkdir()
        (logs / "tools_real.jsonl").write_text('{"session":"real-session"}\n')
        (logs / "tools_link.jsonl").symlink_to(outside)
        output = tmp_path / "export"

        assert anonymize.main(["--input", str(logs), "--out-dir", str(output), "--salt", str(logs / ".salt")]) == 0
        assert len(list(output.rglob("*.jsonl"))) == 1


class TestFreeTextScrubbing:
    """Qualified names inside ``error`` / ``stderr`` prose are pseudonymized while the prose survives."""

    @pytest.mark.parametrize(
        ("record", "path", "hidden", "prefix", "suffix"),
        [
            pytest.param(
                {"cmd": "rdeps", "result": {"error": "module pkg.auth.core not indexed"}},
                ("result", "error"),
                "pkg.auth.core",
                "module sym_",
                " not indexed",
                id="error-module-name",
            ),
            pytest.param(
                {"result": {"error": "call to pkg.auth::login failed at line 3"}},
                ("result", "error"),
                "pkg.auth::login",
                "",
                " failed at line 3",
                id="error-double-colon-qualname",
            ),
            pytest.param(
                {"stderr": "Traceback: pkg.auth.core.login raised ValueError"},
                ("stderr",),
                "pkg.auth.core.login",
                "Traceback: sym_",
                "",
                id="top-level-stderr",
            ),
            pytest.param(
                {"result": {"stderr": "error in module.sub.func"}},
                ("result", "stderr"),
                "module.sub.func",
                "",
                "",
                id="stderr-nested-in-result",
            ),
        ],
    )
    def test_qualified_token_replaced_and_prose_kept(
        self, record: dict, path: tuple[str, ...], hidden: str, prefix: str, suffix: str
    ) -> None:
        """A qualified name in an error or stderr field is pseudonymized; the words around it survive.

        Covers dotted names, ``module::symbol`` tokens, and both the top-level and ``result``-nested ``stderr``: the
        original token must vanish, a ``sym_`` pseudonym must take its place, and the leading and trailing prose stays.
        """
        scrubbed = _dig(anonymize.anonymize_record(record, _SALT), path)

        assert hidden not in scrubbed
        assert "sym_" in scrubbed
        assert scrubbed.startswith(prefix)
        assert scrubbed.endswith(suffix)

    def test_each_qualname_hashed_independently(self) -> None:
        """Every qualified token in one error string is replaced independently.

        The same name appearing twice must map to the same pseudonym, so the message stays readable.
        """
        record = {"result": {"error": "pkg.a.b calls pkg.c.d but pkg.c.d is missing"}}

        scrubbed = anonymize.anonymize_record(record, _SALT)["result"]["error"]

        first = anonymize._pseudo("pkg.a.b", _SALT)
        second = anonymize._pseudo("pkg.c.d", _SALT)
        assert scrubbed == f"{first} calls {second} but {second} is missing"

    def test_text_without_qualname_unchanged(self) -> None:
        """Free text with no qualified names passes through verbatim."""
        record = {"result": {"error": "index is not valid json"}}

        assert anonymize.anonymize_record(record, _SALT)["result"]["error"] == "index is not valid json"

    def test_non_string_error_left_alone(self) -> None:
        """A non-string ``error`` value (e.g. bool/None) is not treated as free text.

        Sibling numeric fields in the same ``result`` must be untouched as well.
        """
        out = anonymize.anonymize_record({"result": {"error": None, "count": 3}}, _SALT)

        assert out["result"]["error"] is None
        assert out["result"]["count"] == 3


class TestNotCovered:
    """``not_covered`` lists are scrubbed element by element."""

    def test_qualified_elements_hashed_plain_labels_kept(self) -> None:
        """Qualified ``not_covered`` elements are hashed; plain labels are preserved.

        A dotted name and a ``module::symbol`` token are hashed while the diagnostic label ``lazy-loading`` stays
        readable, and neither original name remains in the list.
        """
        record = {"result": {"not_covered": ["importlib.import_module", "lazy-loading", "pkg.mod::fn"]}}

        scrubbed = anonymize.anonymize_record(record, _SALT)["result"]["not_covered"]

        assert scrubbed[0].startswith("sym_")
        assert scrubbed[1] == "lazy-loading"
        assert scrubbed[2].startswith("sym_")
        assert "importlib.import_module" not in scrubbed
        assert "pkg.mod::fn" not in scrubbed

    def test_element_pseudonym_matches_standalone_name(self) -> None:
        """Each ``not_covered`` element maps to the same pseudonym as a standalone name.

        Stability across fields lets an analyst join a ``not_covered`` entry to the same symbol elsewhere in the export.
        """
        out = anonymize.anonymize_record({"result": {"not_covered": ["pkg.a.b"]}}, _SALT)

        assert out["result"]["not_covered"][0] == anonymize._pseudo("pkg.a.b", _SALT)

    def test_top_level_list_scrubbed(self) -> None:
        """A ``not_covered`` list at the record top level is scrubbed as well."""
        out = anonymize.anonymize_record({"not_covered": ["a.b.c", "dynamic-dispatch"]}, _SALT)

        assert out["not_covered"][0].startswith("sym_")
        assert out["not_covered"][1] == "dynamic-dispatch"

    def test_non_string_elements_survive(self) -> None:
        """Non-string ``not_covered`` elements are passed through untouched.

        A number and ``None`` next to a qualified name must not crash the scrubber or be stringified.
        """
        out = anonymize.anonymize_record({"result": {"not_covered": [42, None, "pkg.a.b"]}}, _SALT)

        scrubbed = out["result"]["not_covered"]
        assert scrubbed[0] == 42
        assert scrubbed[1] is None
        assert scrubbed[2].startswith("sym_")


class TestCommandFieldLeak:
    """A ``target``/``intent`` value carries project data whether or not it has a dot.

    The gate these tests replace pseudonymized the field only when the *whole* value looked like a qualified name, so a
    dot-free search command was exported verbatim while a path with any dot in it collapsed to one opaque token.
    """

    def test_dot_free_command_scrubbed(self) -> None:
        """A search command with no qualified name in it must not survive verbatim.

        The identifier after ``grep -rn`` is project data; flags and the trailing ``src/`` stay readable.
        """
        record = {"tool": "Bash", "target": "grep -rn internal_secret_name src/"}

        out = anonymize.anonymize_record(record, _SALT)

        assert "internal_secret_name" not in out["target"]
        assert out["target"].startswith("grep -rn sym_")
        assert out["target"].endswith(" src/")

    def test_dot_free_grep_pattern_scrubbed(self) -> None:
        """A bare Grep pattern is project data too, dots or not."""
        out = anonymize.anonymize_record({"tool": "Grep", "target": "validate_token"}, _SALT)

        assert out["target"] == anonymize._pseudo("validate_token", _SALT)

    def test_grep_search_path_scrubbed_like_target(self) -> None:
        """The Grep/Glob scope path is project data on the same footing as the pattern.

        ``search_path`` was added beside ``target`` so the overlap join can see a search's scope; an export that
        pseudonymized the pattern but left the scope verbatim would leak the module path it was meant to hide.
        """
        record = {"tool": "Grep", "target": "validate_token", "search_path": "src/auth_service/tokens.py"}

        out = anonymize.anonymize_record(record, _SALT)

        assert "auth_service" not in out["search_path"]
        assert "tokens" not in out["search_path"]
        assert out["search_path"].endswith(".py")

    def test_intent_prose_scrubbed(self) -> None:
        """Skill arguments are the user's own words — every identifying token is hashed.

        Three words in, three pseudonyms out: even plain English words are hashed because they cannot be told apart from
        identifiers.
        """
        out = anonymize.anonymize_record({"layer": "skill", "intent": "who calls validate_token"}, _SALT)

        assert "validate_token" not in out["intent"]
        assert out["intent"].count("sym_") == 3  # who / calls / validate_token

    def test_path_keeps_shape_and_extension(self) -> None:
        """A Read path is scrubbed per segment, not collapsed to one token.

        The user and project names disappear, while the ``.py`` type, the separator count and a conventional ``src``
        directory stay, so repeat-read analysis still works.
        """
        out = anonymize.anonymize_record({"tool": "Read", "target": "/Users/someone/proj/src/auth.py"}, _SALT)

        target = out["target"]
        assert "someone" not in target
        assert "auth" not in target
        assert target.endswith(".py"), "the file type is diagnostic and must survive"
        assert target.count("/") == 5, "the path shape must survive"
        assert "/src/" in target, "a conventional directory name is not identifying"

    def test_generic_tool_tokens_survive(self) -> None:
        """The tools the telemetry exists to measure stay readable in the export."""
        out = anonymize.anonymize_record({"tool": "Bash", "target": "rg -n 'import' tests/"}, _SALT)

        assert out["target"] == "rg -n 'import' tests/"

    def test_pseudonyms_stable_across_directories(self) -> None:
        """The same identifier maps to one pseudonym, so repeat-read counting survives.

        Two paths in different directories ending in the same file name must end in the same pseudonymized segment.
        """
        first = anonymize.anonymize_record({"target": "/proj/auth_service.py"}, _SALT)["target"]
        second = anonymize.anonymize_record({"target": "/other/auth_service.py"}, _SALT)["target"]

        assert first.rsplit("/", 1)[-1] == second.rsplit("/", 1)[-1]

    def test_non_string_target_left_alone(self) -> None:
        """A non-string ``target`` is not treated as a command."""
        out = anonymize.anonymize_record({"target": None, "count": 2}, _SALT)

        assert out["target"] is None
        assert out["count"] == 2


class TestSessionPseudonymization:
    """The session id correlates an export back to the machine that produced it."""

    @pytest.mark.parametrize(
        ("record", "field", "raw_id"),
        [
            pytest.param({"layer": "tool", "session": "8f14e45f-ea"}, "session", "8f14e45f-ea", id="session"),
            pytest.param(
                {"layer": "skill", "hook_session": "hook-sid-9"}, "hook_session", "hook-sid-9", id="hook-session"
            ),
        ],
    )
    def test_session_fields_pseudonymized(self, record: dict[str, str], field: str, raw_id: str) -> None:
        """The raw session id never survives in a record, in either session field.

        The tool layer carries ``session``; the skill layer's second field, ``hook_session``, is covered too.
        """
        out = anonymize.anonymize_record(record, _SALT)

        assert out[field] == anonymize._pseudo(raw_id, _SALT)

    def test_pseudonym_joins_across_layers(self) -> None:
        """One session id maps to one pseudonym, so cross-layer joins still work.

        The cli and tool layers log the same session; their exported ids must be equal.
        """
        cli = anonymize.anonymize_record({"layer": "cli", "session": "sid-7"}, _SALT)
        tool = anonymize.anonymize_record({"layer": "tool", "session": "sid-7"}, _SALT)

        assert cli["session"] == tool["session"]

    def test_empty_session_stays_empty(self) -> None:
        """An unseeded session stays an empty string rather than becoming a pseudonym."""
        out = anonymize.anonymize_record({"layer": "skill", "hook_session": ""}, _SALT)

        assert out["hook_session"] == ""

    def test_shard_filename_drops_raw_session_id(self, tmp_path: Path) -> None:
        """End-to-end: the exported filename carries a pseudonym, not the session id.

        A tools shard named after the raw session id is exported through ``main``; the output name and body must hold
        only the pseudonym.
        """
        content = '{"layer":"tool","session":"8f14e45f-ea","tool":"Read","target":"/p/auth.py"}\n'

        rc, export_dir, log_dir = _export(tmp_path, "tools_8f14e45f-ea.jsonl", content)

        assert rc == 0
        written = [path.name for path in export_dir.iterdir()]
        # Derive the expectation from the salt the run actually used. _load_salt mints a
        # fresh random salt when the file is absent, so a fixed module-level salt names a
        # pseudonym this run could never produce.
        run_salt = anonymize._load_salt(log_dir / ".salt")
        assert written == [f"tools_{anonymize._pseudo('8f14e45f-ea', run_salt)}-anon.jsonl"]
        record = json.loads((export_dir / written[0]).read_text().strip())
        assert "8f14e45f-ea" not in json.dumps(record)


class TestArgsPayload:
    """The ``args`` payload keeps being pseudonymized."""

    def test_module_argument_still_anonymized(self) -> None:
        """The pre-existing args-payload pseudonymization is unaffected by hardening.

        The ``module`` argument is hashed while the ``cmd`` name stays readable.
        """
        record = {"cmd": "rdeps", "args": {"module": "pkg.auth"}}

        out = anonymize.anonymize_record(record, _SALT)

        assert out["args"]["module"].startswith("sym_")
        assert out["cmd"] == "rdeps"


class TestOutputResolution:
    """Output-path resolution and export separation."""

    def test_default_out_dir_is_export_not_salt_dir(self) -> None:
        """The default resolved output lives under the export dir, never the log/salt dir."""
        resolved = anonymize._resolve_output(Path("logs/cli.jsonl"), None, None)

        assert resolved == Path(anonymize.DEFAULT_OUT_DIR) / "cli-anon.jsonl"
        assert "export" in anonymize.DEFAULT_OUT_DIR
        assert "logs" not in resolved.parent.name

    @pytest.mark.parametrize(
        ("source", "out_dir", "output", "expected"),
        [
            pytest.param(
                Path("logs/skills.jsonl"),
                "my-export",
                None,
                Path("my-export") / "skills-anon.jsonl",
                id="explicit-out-dir",
            ),
            pytest.param(
                Path("logs/cli.jsonl"), "ignored", "out/custom.jsonl", Path("out/custom.jsonl"), id="explicit-output"
            ),
        ],
    )
    def test_explicit_target_options(self, source: Path, out_dir: str, output: str | None, expected: Path) -> None:
        """An explicit ``--out-dir`` holds the derived ``-anon`` file; an explicit ``--output`` overrides it.

        When both options are given, ``--output`` wins and ``--out-dir`` is ignored.
        """
        assert anonymize._resolve_output(source, out_dir, output) == expected

    def test_default_target_separate_from_salt(self, tmp_path: Path) -> None:
        """End-to-end: default export target is created and separate from the salt dir.

        Exporting a ``cli.jsonl`` shard writes ``cli-anon.jsonl`` with the module hashed and never copies the salt into
        the export directory.
        """
        rc, export_dir, _log_dir = _export(tmp_path, "cli.jsonl", '{"cmd":"rdeps","args":{"module":"pkg.auth"}}\n')

        assert rc == 0
        out_file = export_dir / "cli-anon.jsonl"
        assert out_file.exists()
        assert not (export_dir / ".salt").exists()  # salt never copied into export dir
        assert json.loads(out_file.read_text().strip())["args"]["module"].startswith("sym_")


class TestSaltRefusal:
    """Writing into a directory that holds a ``.salt`` file is refused with a nonzero exit code."""

    def test_dir_has_salt_false_for_empty_dir(self, tmp_path: Path) -> None:
        """_dir_has_salt is False when no .salt file is present."""
        assert not anonymize._dir_has_salt(tmp_path)

    def test_dir_has_salt_true_when_present(self, tmp_path: Path) -> None:
        """_dir_has_salt is True once a .salt file is present."""
        (tmp_path / ".salt").write_text("00")

        assert anonymize._dir_has_salt(tmp_path)

    def test_out_dir_containing_salt_refused(self, salted_dir: _SaltedDir, capsys: pytest.CaptureFixture[str]) -> None:
        """Writing into a directory that already holds a .salt file is refused (exit 2).

        The refusal must write nothing and name both the refusal and the ``.salt`` file on stderr.
        """
        rc = anonymize.main(
            [
                "--input",
                str(salted_dir.src),
                "--out-dir",
                str(salted_dir.unsafe_dir),
                "--salt",
                str(salted_dir.salt_file),
            ]
        )

        assert rc == anonymize._EXIT_UNSAFE_OUT_DIR
        assert not (salted_dir.unsafe_dir / "cli-anon.jsonl").exists()  # nothing written
        err = capsys.readouterr().err
        assert "refusing to write" in err
        assert ".salt" in err

    def test_refusal_creates_no_salt(self, salted_dir: _SaltedDir) -> None:
        """Resolving the output name must not create the salt before the refusal check.

        The salt path points at a fresh location; after a refused run it must still not exist.
        """
        rc = anonymize.main(
            [
                "--input",
                str(salted_dir.src),
                "--out-dir",
                str(salted_dir.unsafe_dir),
                "--salt",
                str(salted_dir.salt_file),
            ]
        )

        assert rc == anonymize._EXIT_UNSAFE_OUT_DIR
        assert not salted_dir.salt_file.exists(), "a refused run must not leave a salt file behind"

    def test_explicit_output_in_salt_dir_refused(self, salted_dir: _SaltedDir) -> None:
        """The salt refusal also applies when an explicit ``--output`` targets a salt dir."""
        output = salted_dir.unsafe_dir / "cli-anon.jsonl"

        rc = anonymize.main(
            ["--input", str(salted_dir.src), "--output", str(output), "--salt", str(salted_dir.salt_file)]
        )

        assert rc == anonymize._EXIT_UNSAFE_OUT_DIR
        assert not output.exists()


class TestSaltFile:
    """The "opaque without salt" guarantee assumes the salt stays secret, well-formed, and not symlinked."""

    @_skip_windows_posix
    def test_created_owner_only(self, tmp_path: Path) -> None:
        """A freshly created salt file is owner-only (0o600) so no local user can reverse pseudonyms."""
        salt_file = tmp_path / ".salt"

        anonymize._load_salt(salt_file)

        mode = stat.S_IMODE(salt_file.stat().st_mode)
        assert mode == 0o600, f"salt file mode is {oct(mode)}, expected 0o600"

    @_skip_windows_posix
    @pytest.mark.usefixtures("open_umask")
    def test_owner_only_regardless_of_umask(self, tmp_path: Path) -> None:
        """The 0o600 mode holds even under a permissive umask that would otherwise widen it.

        With umask 0 a plain ``write_text`` would yield 0o666; the salt must still come out owner-only.
        """
        salt_file = tmp_path / ".salt"

        anonymize._load_salt(salt_file)

        assert stat.S_IMODE(salt_file.stat().st_mode) == 0o600

    def test_existing_salt_read_as_is(self, tmp_path: Path) -> None:
        """An existing salt is read as-is (never overwritten), so its value stays stable across calls."""
        salt_file = tmp_path / ".salt"
        salt_file.write_text("ab" * 32)
        salt_file.chmod(0o600)

        assert anonymize._load_salt(salt_file) == bytes.fromhex("ab" * 32)

    @pytest.mark.parametrize(
        "content",
        [
            pytest.param("", id="empty"),
            pytest.param("ab", id="one-byte"),
            pytest.param("ab" * 16, id="sixteen-bytes"),
            pytest.param("ab" * 33, id="thirty-three-bytes"),
            pytest.param("zz" * 32, id="not-hex"),
        ],
    )
    def test_rejects_stored_salt_that_is_not_32_hex_bytes(self, tmp_path: Path, content: str) -> None:
        """A planted or truncated salt must fail loudly, not silently weaken every pseudonym.

        ``bytes.fromhex("")`` accepts an empty salt, which would leave pseudonyms a plain unsalted digest; malformed hex
        used to crash with a bare ``ValueError`` traceback.
        """
        salt_file = tmp_path / ".salt"
        salt_file.write_text(content)
        salt_file.chmod(0o600)

        with pytest.raises(ValueError, match="salt file"):
            anonymize._load_salt(salt_file)

    @_skip_windows_posix
    def test_rejects_group_readable_salt(self, tmp_path: Path) -> None:
        """A salt readable beyond its owner lets another local user reverse every pseudonym."""
        salt_file = tmp_path / ".salt"
        salt_file.write_text("ab" * 32)
        salt_file.chmod(0o640)

        with pytest.raises(ValueError, match="mode 0600"):
            anonymize._load_salt(salt_file)

    @_skip_windows_posix
    def test_refuses_to_follow_symlinked_salt(self, tmp_path: Path) -> None:
        """A salt path that is a symlink could be repointed at attacker-chosen content."""
        target = tmp_path / "elsewhere"
        target.write_text("ab" * 32)
        target.chmod(0o600)
        link = tmp_path / ".salt"
        link.symlink_to(target)

        with pytest.raises(ValueError, match="symlink"):
            anonymize._load_salt(link)

    def test_works_without_posix_fchmod(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Salt creation stays usable on platforms that expose no ``os.fchmod``.

        The attribute is removed on every host, so the Windows-like branch runs on POSIX runners as well.
        """
        monkeypatch.delattr(anonymize.os, "fchmod", raising=False)

        assert len(anonymize._load_salt(tmp_path / ".salt")) == 32

    def test_main_reports_malformed_salt_without_traceback(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A bad salt file is a clean exit 1 with the file named, not an uncaught ValueError."""
        src = tmp_path / "tools_x.jsonl"
        src.write_text('{"session": "s"}\n')
        salt_file = tmp_path / "keep" / ".salt"
        salt_file.parent.mkdir()
        salt_file.write_text("ab")
        salt_file.chmod(0o600)

        rc = anonymize.main(["--input", str(src), "--out-dir", str(tmp_path / "out"), "--salt", str(salt_file)])

        assert rc == 1
        assert str(salt_file) in capsys.readouterr().err

    @_skip_windows_posix
    def test_process_refuses_symlinked_output(self, tmp_path: Path) -> None:
        """An export path that is a planted symlink is refused and its target keeps its content."""
        src = tmp_path / "tools_x.jsonl"
        src.write_text('{"session": "s"}\n')
        victim = tmp_path / "victim.txt"
        victim.write_text("keep")
        link = tmp_path / "out-anon.jsonl"
        link.symlink_to(victim)

        with pytest.raises(OSError, match="symbolic link"):  # ELOOP from O_NOFOLLOW on Linux and macOS
            anonymize.process(src, link, _SALT)
        assert victim.read_text() == "keep"
