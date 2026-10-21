"""Regression tests for input hardening across foundry ``bin/`` scripts.

Each class pins one fix from the audit: trailing-newline token validators, the purge registry guard, name and timestamp
allowlists, the session-id glob guard, literal ledger-name matching, sentinel-name validation, the symlink guard's
marker and source checks, and bounded Git calls in the version-sync check.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

import append_ledger
import check_plugin_version_sync as cpvs
import compact_gate as gate
import extract_keep_flag as ekf
import get_plugin_install_path
import purge_plugin_cache as purge
import pytest
import resolve_agent_file as raf
import resolve_plugin_root
import resolve_shared_path
import symlink_with_guard as swg


def _symlinks_supported() -> bool:
    """Probe whether the test user may create a symlink on this host."""
    with tempfile.TemporaryDirectory() as raw:
        base = Path(raw)
        (base / "target").write_text("x", encoding="utf-8")
        try:
            (base / "link").symlink_to(base / "target")
        except (OSError, NotImplementedError):
            return False
    return True


_skip_without_symlinks = pytest.mark.skipif(not _symlinks_supported(), reason="host cannot create symlinks")


class TestTokenValidatorsRejectTrailingNewline:
    """A ``$``-anchored ``match`` accepts ``"name\\n"``; the validators must not."""

    @pytest.mark.parametrize(
        "validate",
        [
            pytest.param(lambda value: resolve_plugin_root._validate_token(value, "plugin"), id="resolve_plugin_root"),
            pytest.param(
                lambda value: get_plugin_install_path._validate_token(value, "plugin"), id="get_plugin_install_path"
            ),
            pytest.param(resolve_shared_path._validate_plugin, id="resolve_shared_path-plugin"),
            pytest.param(
                lambda value: resolve_shared_path._validate_subdir(f"skills/{value}"), id="resolve_shared_path-subdir"
            ),
        ],
    )
    def test_trailing_newline_is_rejected(self, validate: Callable[[str], None]) -> None:
        """A name carrying a trailing newline fails validation instead of reaching a path or command.

        Scenario: the value reads like a valid token to a human or a `$`-anchored regex but would put a newline
        into a path component downstream.
        """
        with pytest.raises(ValueError, match=r"disallowed|invalid"):
            validate("foundry\n")

    def test_purge_marketplace_with_trailing_newline_is_argument_error(self, tmp_path: Path) -> None:
        """The purge CLI refuses a marketplace token with a trailing newline before scanning anything."""
        assert purge.main(["--cache-dir", str(tmp_path), "--marketplace", "market\n"]) == 2


class TestPurgeRegistryGuard:
    """An unreadable registry must stop the purge rather than make every plugin look uninstalled."""

    @staticmethod
    def _orphan(cache: Path) -> Path:
        """Create an ancient orphaned version dir that every other guard would let the purge delete."""
        version = cache / "borda-ai-rig" / "ghost" / "0.1.0"
        (version / ".claude-plugin").mkdir(parents=True)
        (version / ".claude-plugin" / "plugin.json").write_text("{}", encoding="utf-8")
        (version / ".orphaned_at").write_text("1", encoding="utf-8")
        return version

    @pytest.mark.parametrize(
        "content",
        [
            pytest.param("{not json", id="malformed-json"),
            pytest.param("[]", id="not-an-object"),
            pytest.param('{"plugins": []}', id="plugins-not-an-object"),
        ],
    )
    def test_unusable_registry_deletes_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], content: str
    ) -> None:
        """A registry that parses to the wrong shape aborts the run with exit 1 and leaves the cache intact."""
        version = self._orphan(tmp_path / "cache")
        registry = tmp_path / "installed_plugins.json"
        registry.write_text(content, encoding="utf-8")
        code = purge.main(["--cache-dir", str(tmp_path / "cache"), "--registry", str(registry), "--apply"])
        assert (code, version.is_dir(), "plugin registry" in capsys.readouterr().err) == (1, True, True)

    def test_missing_registry_deletes_nothing(self, tmp_path: Path) -> None:
        """A registry file that does not exist aborts the run just like a corrupt one."""
        version = self._orphan(tmp_path / "cache")
        code = purge.main(
            ["--cache-dir", str(tmp_path / "cache"), "--registry", str(tmp_path / "absent.json"), "--apply"]
        )
        assert (code, version.is_dir()) == (1, True)


class TestResolveAgentFileInput:
    """Names and timestamps become path components, so separators and dots are refused."""

    @pytest.mark.parametrize(
        ("name", "timestamp"),
        [
            pytest.param("foundry:../../x", "", id="agent-name-traversal"),
            pytest.param("../../etc/passwd", "", id="bare-name-traversal"),
            pytest.param("/audit/../../x", "", id="skill-name-traversal"),
            pytest.param("../evil:curator", "", id="plugin-traversal"),
            pytest.param("curator", "../../x", id="timestamp-traversal"),
            pytest.param("curator", "a/b", id="timestamp-separator"),
        ],
    )
    def test_path_components_are_rejected(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        name: str,
        timestamp: str,
    ) -> None:
        """A traversal-shaped name or timestamp exits 2 and prints no resolved path."""
        monkeypatch.setattr("sys.argv", ["resolve_agent_file.py", "--name", name, "--timestamp", timestamp])
        assert (raf.main(), "agent-file=" in capsys.readouterr().out) == (2, False)


class TestCompactGateSessionId:
    """The session id is spliced into a transcript glob, so wildcard characters must not reach it."""

    @pytest.mark.parametrize("session_id", ["*", "../x", "a/b", "[a-z]", "abc\n"])
    def test_wildcard_session_id_is_rejected(self, tmp_path: Path, session_id: str) -> None:
        """A session id with glob or separator characters yields the invalid-session decision, not a transcript scan."""
        assert gate._decide({"session_id": session_id}, tmp_path, b"contract") == "invalid session id"


class TestAppendLedgerLiteralName:
    """The ledger name is matched literally when sweeping parallel writers' staged files."""

    def test_glob_characters_in_ledger_name_do_not_widen_the_match(self, tmp_path: Path) -> None:
        """A ledger named ``log[1].jsonl`` sweeps its own writer file and leaves ``log1.jsonl``'s alone.

        Scenario: unescaped, ``[1]`` is a character class that matches `log1.jsonl.w.rec` and misses the ledger's own
        `log[1].jsonl.w.rec`, so one ledger would consume another's staged record.
        """
        ledger = tmp_path / "log[1].jsonl"
        ledger.write_bytes(b"old\n")
        (tmp_path / "log[1].jsonl.w.rec").write_bytes(b"mine\n")
        other = tmp_path / "log1.jsonl.w.rec"
        other.write_bytes(b"theirs\n")
        code = append_ledger.main([str(ledger)])
        assert (code, ledger.read_bytes(), other.read_bytes()) == (0, b"old\nmine\n", b"theirs\n")

    @_skip_without_symlinks
    def test_symlinked_staged_file_is_not_appended(self, tmp_path: Path) -> None:
        """A ``.rec`` that is a symlink is skipped, so it cannot copy another file's content into the ledger."""
        secret = tmp_path / "secret.txt"
        secret.write_bytes(b"secret\n")
        ledger = tmp_path / "log.jsonl"
        (tmp_path / "log.jsonl.rec").symlink_to(secret)
        assert (append_ledger.main([str(ledger)]), ledger.exists()) == (0, False)


class TestExtractKeepFlagNames:
    """Slug and session id are joined into sentinel file names, so separators are refused."""

    @pytest.mark.parametrize(
        ("slug", "csid"),
        [
            pytest.param("../escape", "sess1", id="slug-traversal"),
            pytest.param("a/b", "sess1", id="slug-separator"),
            pytest.param("dev-fix", "../sess", id="csid-traversal"),
            pytest.param("dev-fix", "a/b", id="csid-separator"),
        ],
    )
    def test_separator_in_name_returns_two_without_writing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, slug: str, csid: str
    ) -> None:
        """A slug or session id with a path separator exits 2 before any sentinel is written."""
        monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
        monkeypatch.setenv("CSID", csid)
        monkeypatch.chdir(tmp_path)
        code = ekf.main(["extract-keep-flag.py", slug, '--keep "x"'])
        assert (code, list(tmp_path.rglob("*keep-items*"))) == (2, [])


class TestSymlinkGuardInputs:
    """The guard's ownership marker and ``create`` source must be real, in-bounds values."""

    @pytest.mark.parametrize(
        "marker",
        [
            pytest.param("/", id="bare-separator"),
            pytest.param("..", id="dots-only"),
            pytest.param("ab/", id="too-short"),
            pytest.param("foundry/\n", id="trailing-newline"),
        ],
    )
    def test_degenerate_marker_is_rejected(self, marker: str) -> None:
        """A marker that substring-matches every absolute link target is refused instead of purging them all."""
        with pytest.raises(ValueError, match="marker"):
            swg._validate_marker(marker)

    def test_cleanup_cli_rejects_bare_separator_marker(self, tmp_path: Path) -> None:
        """``cleanup --marker /`` exits 2 before touching ``~/.claude/skills`` or ``~/.claude/agents``."""
        argv = ["cleanup", "--plugin-root", str(tmp_path), "--home", str(tmp_path), "--marker", "/"]
        assert swg.main(argv) == 2

    @_skip_without_symlinks
    def test_create_rejects_source_link_leaving_plugin_root(self, tmp_path: Path) -> None:
        """A source that sits under the plugin root lexically but resolves outside it is refused.

        Scenario: the copy tier would follow the link and materialise the outside file under ``~/.claude``.
        """
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        plugin = tmp_path / "plugin"
        plugin.mkdir()
        outside = tmp_path / "outside.md"
        outside.write_text("x", encoding="utf-8")
        (plugin / "link.md").symlink_to(outside)
        dest = home / ".claude" / "link.md"
        argv = [
            "create",
            "--plugin-root",
            str(plugin),
            "--src",
            str(plugin / "link.md"),
            "--dest",
            str(dest),
            "--home",
            str(home),
        ]
        assert (swg.main(argv), os.path.lexists(dest)) == (2, False)


class TestVersionSyncBoundedGit:
    """A hung Git call must fail the check closed instead of stalling pre-commit."""

    @staticmethod
    def _hang(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        """Stand in for ``subprocess.run`` and time out, requiring the caller to have passed ``timeout=``."""
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])  # type: ignore[arg-type]

    def test_timeout_reads_as_failed_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A Git call that exceeds its bound comes back as return code 124 rather than raising."""
        monkeypatch.setattr(cpvs.subprocess, "run", self._hang)
        assert cpvs._git(["status"]).returncode == 124

    def test_timeout_fails_checkout_lookup_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The host-manifest scan reports the checkout as unlocatable when Git times out."""
        monkeypatch.setattr(cpvs.subprocess, "run", self._hang)
        assert cpvs.find_removed_host_manifests(Path("plugins")) == [
            "cannot locate the Git checkout for tracked host manifests"
        ]
