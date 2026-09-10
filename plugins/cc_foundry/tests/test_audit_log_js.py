"""Tests for ``hooks/lib/audit-log.js``, the shared append-only audit record writer.

Three contracts are load-bearing and each is tested against something the library cannot influence:

* **Canonical form** — a JavaScript writer and a Python reader must produce identical bytes for the same record. The
  vectors come from a committed fixture built by an independent longhand implementation, so agreement here is
  cross-language agreement rather than a module reproducing itself.
* **Append-only** — nothing in the module may remove, rename, truncate or rewrite a file. Deletion lives solely in the
  explicitly invoked ``prune`` command, and a regression would be silent, so the export surface is asserted directly.
* **Identifier injectivity** — the log file name is a hash, not a sanitised id, because a ``[^a-zA-Z0-9_-] -> _``
  transform maps distinct sessions onto one file. Ill-formed UTF-16 is rejected before hashing for the same reason.

Everything runs through ``node`` against a throwaway home, so no test can touch the runner's real log directory.
"""

from __future__ import annotations

import json
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Callable

import pytest

from _audit_harness import HOOKS_DIR, install


LIBRARY = HOOKS_DIR / "lib" / "audit-log.js"
VECTORS = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "audit" / "canonical_vectors.json").read_text(encoding="utf-8")
)

NODE_UNAVAILABLE = shutil.which("node") is None
_skip_node_unavailable = pytest.mark.skipif(NODE_UNAVAILABLE, reason="requires node to execute the library")


@pytest.fixture(name="audit_env")
def _audit_env(tmp_path: Path):
    """Return an isolated plugin root and home for the library under test."""
    return install(tmp_path)


@pytest.fixture(name="call_lib")
def _call_lib(audit_env) -> Callable[..., object]:
    """Return a callable evaluating one JavaScript expression against the installed library."""

    def _call(expression: str, **env_extra: str) -> object:
        """Evaluate ``expression`` with the library bound to ``lib`` and return its JSON value."""
        script = (
            f"const lib = require({json.dumps(str(audit_env.root / 'hooks' / 'lib' / 'audit-log.js'))});"
            f"process.stdout.write(JSON.stringify({expression}) ?? 'undefined');"
        )
        proc = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            encoding="utf-8",
            env=audit_env.env(**env_extra),
            timeout=30,
            check=False,
        )
        assert proc.returncode == 0, f"node failed: {proc.stderr}"
        return None if proc.stdout == "undefined" else json.loads(proc.stdout)

    return _call


def _mode_bits_are_enforced(tmp_path: Path) -> bool:
    """Probe whether the filesystem actually honours POSIX mode bits.

    A capability probe rather than a platform test: native Windows has no mode bits, but so does a POSIX filesystem
    mounted without them, and the interesting question is the capability either way.
    """
    probe = tmp_path / "mode-probe"
    probe.mkdir()
    probe.chmod(0o700)
    return stat.S_IMODE(probe.stat().st_mode) == 0o700


def _owner_can_be_denied(tmp_path: Path) -> bool:
    """Probe whether clearing the write bit actually denies this process."""
    probe = tmp_path / "deny-probe"
    probe.mkdir()
    probe.chmod(0o500)
    try:
        (probe / "file").write_text("x", encoding="utf-8")
    except OSError:
        return True
    finally:
        probe.chmod(0o700)
    return False


def _symlinks_available(tmp_path: Path) -> bool:
    """Probe whether this process may create a directory symlink."""
    target = tmp_path / "symlink-target"
    target.mkdir()
    try:
        (tmp_path / "symlink-probe").symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        return False
    return True


def _vector_params() -> list:
    """Return one ``pytest.param`` per canonical vector."""
    return [pytest.param(vector, id=vector["id"]) for vector in VECTORS["vectors"]]


@_skip_node_unavailable
class TestCanonicalForm:
    """The JavaScript canonical form must equal the Python one byte-for-byte."""

    @pytest.mark.parametrize("vector", _vector_params())
    def test_canonical_bytes_match(self, vector: dict, call_lib: Callable[..., object]) -> None:
        """Match the committed canonical serialization exactly.

        Divergence here is the one failure that cannot be caught downstream: two languages that disagree on key order
        or on how a character is escaped produce different hashes for the same record, and every integrity check then
        reports corruption that never happened.
        """
        assert call_lib(f"lib.canonicalize({json.dumps(vector['record'])})") == vector["canonical"]

    @pytest.mark.parametrize("vector", _vector_params())
    def test_record_hash_matches(self, vector: dict, call_lib: Callable[..., object]) -> None:
        """Match the committed SHA-256 of the canonical bytes."""
        assert call_lib(f"lib.recordHash({json.dumps(vector['record'])})") == vector["sha256"]

    def test_record_hash_excludes_itself(self, call_lib: Callable[..., object]) -> None:
        """A record's own hash is computed over the record WITHOUT that hash, or verification could never succeed."""
        with_hash = call_lib('lib.recordHash({"a": 1, "record_hash": "whatever"})')
        without = call_lib('lib.recordHash({"a": 1})')
        assert with_hash == without

    def test_float_is_rejected(self, call_lib: Callable[..., object]) -> None:
        """No float may be serialized: JavaScript and Python format them differently, so the hash would diverge."""
        assert (
            call_lib(
                '(() => { try { lib.canonicalize({"a": 1.5}); return "accepted"; } '
                'catch (e) { return "rejected"; } })()'
            )
            == "rejected"
        )

    def test_ill_formed_string_is_rejected(self, call_lib: Callable[..., object]) -> None:
        """A lone surrogate cannot be UTF-8 encoded by the Python reader, so the writer must refuse it."""
        assert (
            call_lib(
                '(() => { try { lib.canonicalize({"a": "x\\uD800"}); return "accepted"; } '
                'catch (e) { return "rejected"; } })()'
            )
            == "rejected"
        )

    def test_proto_key_cannot_silently_vanish(self, call_lib: Callable[..., object]) -> None:
        """``__proto__`` is rejected, and would not be lost even if it were not.

        Assigning that key into an ordinary object sets the prototype instead of creating an own property, so the key
        disappears from ``JSON.stringify`` while a Python reader keeps it — the same record, two hashes, and every
        such record reported as corrupt. The parse is done with ``JSON.parse`` because that is how such a key would
        realistically arrive: as data, not as a literal an author typed.
        """
        rejected = call_lib(
            '(() => { try { lib.canonicalize(JSON.parse(\'{"__proto__":1,"a":2}\')); return "accepted"; } '
            'catch (e) { return "rejected"; } })()'
        )
        assert rejected == "rejected"
        # Even with the guard removed, the sorter must not drop it: `sortDeep` uses a null-prototype accumulator.
        survives = call_lib('Object.keys(JSON.parse(\'{"__proto__":1,"a":2}\')).length')
        assert survives == 2

    # `__proto__` is absent here on purpose: this case builds a JS object LITERAL, where `__proto__:` sets the
    # prototype and never becomes a key, so the guard would have nothing to reject. Its real form — arriving through
    # `JSON.parse`, where it IS an own property — is covered by the dedicated test above.
    @pytest.mark.parametrize("key", ["9", "0", "42", "a-b", "a.b", "a b", "", "é", "constructor", "prototype"])
    def test_non_identifier_key_is_rejected(self, key: str, call_lib: Callable[..., object]) -> None:
        """Only ASCII identifiers may be keys, and a decimal one is the case that would silently corrupt.

        JavaScript treats a decimal key as an array index and emits such keys first, in numeric order, whatever the sort
        produced; Python's ``sort_keys`` orders them lexicographically. ``{"9":1,"10":2}`` therefore serializes
        differently in the two languages, so every record carrying one would read as corrupt on the Python side. The
        remaining cases are the same rule applied to keys that are merely not identifiers.
        """
        record = json.dumps({key: 1})
        assert (
            call_lib(
                f'(() => {{ try {{ lib.canonicalize({record}); return "accepted"; }} '
                'catch (e) { return "rejected"; } })()'
            )
            == "rejected"
        )

    def test_python_agrees_on_key_order_for_every_accepted_key(self, call_lib: Callable[..., object]) -> None:
        """Whatever the guard admits must serialize identically in both languages — that is the point of the guard."""
        record = {"zed": 1, "Alpha": 2, "_under": 3, "m9": 4, "M": 5}
        expected = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        assert call_lib(f"lib.canonicalize({json.dumps(record)})") == expected

    def test_null_is_explicit_and_absence_is_absence(self, call_lib: Callable[..., object]) -> None:
        """An explicit null and an absent key are different records with different hashes."""
        explicit = call_lib('lib.canonicalize({"a": null})')
        absent = call_lib("lib.canonicalize({})")
        assert explicit == '{"a":null}'
        assert absent == "{}"


@_skip_node_unavailable
class TestIdentifiers:
    """Filesystem keys are hashes because sanitising them merges distinct sessions."""

    def test_distinct_ids_do_not_collide(self, call_lib: Callable[..., object]) -> None:
        """``a/b`` and ``a?b`` must land in different files — a character-class sanitiser maps both to ``a_b``."""
        assert call_lib('lib.fsKey("a/b")') != call_lib('lib.fsKey("a?b")')

    @pytest.mark.parametrize(
        "identifier",
        [
            pytest.param("\\uD800", id="lone-high-surrogate"),
            pytest.param("x\\uDC00", id="lone-low-surrogate"),
            pytest.param("\\uD800\\uD800", id="two-high-surrogates"),
        ],
    )
    def test_ill_formed_ids_are_rejected_not_hashed(self, identifier: str, call_lib: Callable[..., object]) -> None:
        """Reject ill-formed UTF-16 before hashing.

        UTF-8 encoding of ill-formed UTF-16 is not injective, so hashing first would map several distinct ids onto one
        key. Rejecting yields ``_no-session.jsonl`` and an explicit null, which is honest about what is not known.
        """
        assert call_lib(f'lib.usableId("{identifier}")') is None
        assert call_lib(f'lib.fsKey("{identifier}")') is None

    def test_empty_id_is_unusable(self, call_lib: Callable[..., object]) -> None:
        """An empty string is not an identifier."""
        assert call_lib('lib.usableId("")') is None

    def test_no_session_goes_to_the_shared_stream(self, call_lib: Callable[..., object], audit_env) -> None:
        """Records with no usable session id share one file whose name cannot collide with a real key."""
        assert Path(str(call_lib("lib.logPath(null)"))).name == "_no-session.jsonl"
        assert Path(str(call_lib('lib.logPath("real")'))).name.startswith("s-")


@_skip_node_unavailable
class TestAppendOnly:
    """The module may only ever append."""

    def test_exports_nothing_that_deletes(self, call_lib: Callable[..., object]) -> None:
        """No exported function may call a filesystem removal API.

        The invariant is the design: three earlier revisions each added a coordination mechanism that deleted or
        replaced a file, and each was found unsound. A regression would be silent at runtime, so it is asserted here.
        """
        source = LIBRARY.read_text(encoding="utf-8")
        forbidden = ("unlinkSync", "rmSync", "rmdirSync", "renameSync", "truncateSync", "writeFileSync", "ftruncate")
        assert [name for name in forbidden if name in source] == []

    def test_append_creates_the_directory_private(self, audit_env, call_lib: Callable[..., object]) -> None:
        """The log directory is 0700 and the file 0600 — nothing here is meant to be shared."""
        call_lib('lib.appendRecord({"session_id": "s", "action_type": "tool.bash"})')
        if not _mode_bits_are_enforced(audit_env.home):
            pytest.skip("this filesystem does not enforce POSIX mode bits")
        assert stat.S_IMODE(audit_env.audit_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(audit_env.log_files()[0].stat().st_mode) == 0o600

    def test_second_append_does_not_rewrite_the_first(self, audit_env, call_lib: Callable[..., object]) -> None:
        """Appending leaves earlier bytes untouched, which is what lets several processes write one file at once."""
        call_lib('lib.appendRecord({"session_id": "s", "action_type": "tool.bash", "record_id": "first"})')
        first_bytes = audit_env.log_files()[0].read_bytes()
        call_lib('lib.appendRecord({"session_id": "s", "action_type": "tool.bash", "record_id": "second"})')
        assert audit_env.log_files()[0].read_bytes().startswith(first_bytes)


@_skip_node_unavailable
class TestFailureIsReturnedNeverRaised:
    """Callers are hooks whose decision must not depend on whether logging worked."""

    def test_disabled_writes_nothing(self, audit_env, call_lib: Callable[..., object]) -> None:
        """``RIG_AUDIT=0`` writes no file at all and reports why."""
        result = call_lib('lib.appendRecord({"session_id": "s", "action_type": "tool.bash"})', RIG_AUDIT="0")
        assert result["_unwritten"] is True and result["reason"] == "disabled"
        assert not audit_env.audit_dir.exists()

    def test_unwritable_directory_returns_instead_of_throwing(self, audit_env, call_lib) -> None:
        """An unusable log directory degrades to no-audit; it never raises into the hook that called it."""
        if not _owner_can_be_denied(audit_env.home):
            pytest.skip("this filesystem does not deny the owner a write")
        audit_env.audit_dir.mkdir(parents=True)
        audit_env.audit_dir.chmod(0o500)
        try:
            result = call_lib('lib.appendRecord({"session_id": "s", "action_type": "tool.bash"})')
            assert result["_unwritten"] is True
            assert "append-failed" in result["reason"]
        finally:
            audit_env.audit_dir.chmod(0o700)

    def test_symlinked_log_directory_degrades(self, audit_env, call_lib: Callable[..., object]) -> None:
        """A symlinked log directory is refused rather than followed."""
        if not _symlinks_available(audit_env.home):
            pytest.skip("this process may not create directory symlinks")
        elsewhere = audit_env.home / "elsewhere"
        elsewhere.mkdir(parents=True)
        (audit_env.home / ".claude" / "logs").mkdir(parents=True)
        (audit_env.home / ".claude" / "logs" / "audit").symlink_to(elsewhere, target_is_directory=True)
        result = call_lib('lib.appendRecord({"session_id": "s", "action_type": "tool.bash"})')
        assert result["_unwritten"] is True and result["reason"] == "log-dir-symlink"
        assert list(elsewhere.iterdir()) == []

    def test_unserializable_record_returns_instead_of_throwing(self, call_lib: Callable[..., object]) -> None:
        """A record the canonical form forbids is reported, not raised."""
        result = call_lib('lib.appendRecord({"session_id": "s", "action_type": "tool.bash", "n": 1.5})')
        assert result["_unwritten"] is True and result["reason"].startswith("unserializable")


@_skip_node_unavailable
class TestNoSessionCutoff:
    """The shared stream has a soft cutoff, never a rewrite."""

    def test_refuses_at_the_cutoff_and_leaves_the_file_unchanged(self, audit_env, call_lib) -> None:
        """At the cutoff the writer stops appending; it never truncates, rotates or compacts what is already there."""
        call_lib('lib.appendRecord({"session_id": null, "action_type": "tool.bash"})')
        target = audit_env.audit_dir / "_no-session.jsonl"
        before = target.read_bytes()
        result = call_lib(
            'lib.appendRecord({"session_id": null, "action_type": "tool.bash"})',
            RIG_AUDIT_NOSESSION_MAX_BYTES="1",
        )
        assert result["_unwritten"] is True and result["reason"] == "no-session-cutoff"
        assert target.read_bytes() == before


@_skip_node_unavailable
class TestIdentityAndRedaction:
    """Plugin identity comes from ``plugin.json``; command text never enters a record."""

    def test_identity_comes_from_the_manifest_not_the_directory(self, tmp_path: Path) -> None:
        """The installed cache nests plugins under a VERSION directory, so the basename is not a name."""
        env = install(tmp_path / "case", name="oss", version="1.2.3")
        script = (
            f"const lib = require({json.dumps(str(env.root / 'hooks' / 'lib' / 'audit-log.js'))});"
            "process.stdout.write(JSON.stringify([lib.logicalPlugin(), lib.pluginVersion()]));"
        )
        proc = subprocess.run(
            ["node", "-e", script], capture_output=True, encoding="utf-8", env=env.env(), timeout=30, check=False
        )
        assert json.loads(proc.stdout) == ["cc_oss", "1.2.3"]

    def test_unreadable_manifest_yields_unknown_identity(self, tmp_path: Path) -> None:
        """An unreadable ``plugin.json`` gives ``unknown`` and a null version, never an invented one."""
        env = install(tmp_path / "case")
        (env.root / ".claude-plugin" / "plugin.json").unlink()
        script = (
            f"const lib = require({json.dumps(str(env.root / 'hooks' / 'lib' / 'audit-log.js'))});"
            "process.stdout.write(JSON.stringify([lib.logicalPlugin(), lib.pluginVersion()]));"
        )
        proc = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            encoding="utf-8",
            env={**env.env(), "CLAUDE_PLUGIN_ROOT": str(env.root)},
            timeout=30,
            check=False,
        )
        assert json.loads(proc.stdout) == ["unknown", None]

    def test_redact_returns_a_digest_and_never_the_text(self, call_lib: Callable[..., object]) -> None:
        """Command text must never leave this function."""
        result = call_lib('lib.redact("echo secret-token")')
        assert set(result) == {"digest"}
        assert "secret" not in json.dumps(result)
