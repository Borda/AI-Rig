"""Tests for ``bin/load-agent-reference.py`` — agent sidecar fragment loader.

Covers fragment emission from ``CLAUDE_PLUGIN_ROOT``, the degraded message for a missing fragment, the missing-sidecar
block, argument and path-component validation, and the stderr notice when the cwd-relative fallback is used.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "load-agent-reference.py"
_spec = importlib.util.spec_from_file_location("research_load_agent_reference", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

main = _mod.main
_DEGRADED = "degraded: fragment unavailable"


@pytest.fixture
def plugin_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build a plugin root with one sidecar, isolated from the real home directory's plugin cache."""
    root = tmp_path / "plugin"
    sidecar = root / "references" / "scientist"
    sidecar.mkdir(parents=True)
    (sidecar / "frag.md").write_bytes(b"line one\r\nline two\n")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))
    return root


class TestEmit:
    """Fragment output for a resolvable sidecar."""

    def test_writes_fragment_bytes_verbatim(self, plugin_root: Path, capfdbinary: pytest.CaptureFixture[bytes]) -> None:
        """The fragment is copied byte for byte, CRLF included, as ``cat`` would.

        Callers paste the fragment into prompts verbatim; a decode and re-encode could alter it.
        """
        code = main(["load-agent-reference.py", "scientist", "frag.md", _DEGRADED])

        assert code == 0
        assert capfdbinary.readouterr().out == b"line one\r\nline two\n"

    def test_missing_fragment_prints_degraded_message(
        self, plugin_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An absent fragment prints the caller's degraded message and exits 0, so the skill continues."""
        code = main(["load-agent-reference.py", "scientist", "absent.md", _DEGRADED])

        assert code == 0
        assert capsys.readouterr().out.strip() == _DEGRADED

    def test_missing_sidecar_blocks(self, plugin_root: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """An agent with no sidecar directory exits 1 with a BLOCKED line on stderr."""
        code = main(["load-agent-reference.py", "unknown-agent", "frag.md", _DEGRADED])

        assert code == 1
        assert "! BLOCKED" in capsys.readouterr().err


class TestValidation:
    """Arguments rejected with exit 2 before any filesystem access."""

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param(["scientist", "frag.md"], id="too-few-args"),
            pytest.param(["scientist", "frag.md", _DEGRADED, "extra"], id="too-many-args"),
            pytest.param(["../scientist", "frag.md", _DEGRADED], id="agent-traversal"),
            pytest.param(["scientist\n", "frag.md", _DEGRADED], id="agent-trailing-newline"),
            pytest.param(["scientist", "../frag.md", _DEGRADED], id="fragment-traversal"),
            pytest.param(["scientist", "", _DEGRADED], id="empty-fragment"),
        ],
    )
    def test_invalid_arguments(self, argv: list[str], plugin_root: Path) -> None:
        """Wrong argument count, an agent outside ``[A-Za-z0-9_-]+``, or an unsafe fragment name exits 2."""
        assert main(["load-agent-reference.py", *argv]) == 2


class TestCacheTier:
    """Installed-cache lookup when the plugin root carries no sidecar for the agent."""

    def test_newest_non_orphaned_version_wins(
        self, plugin_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The highest non-orphaned research version's sidecar is read; a newer orphaned install is skipped.

        Several cached versions coexist after upgrades. The old full-cache walk returned whichever directory it met
        first; a superseded (``.orphaned_at``) install must never shadow the live one.
        """
        research = tmp_path / "home" / ".claude" / "plugins" / "cache" / "market" / "research"
        for version, text in (("0.9.0", "old\n"), ("0.29.0", "live\n"), ("0.30.0", "orphaned\n")):
            sidecar = research / version / "references" / "data-steward"
            sidecar.mkdir(parents=True)
            (sidecar / "frag.md").write_text(text, encoding="utf-8", newline="\n")
        (research / "0.30.0" / ".orphaned_at").write_text("", encoding="utf-8")

        code = main(["load-agent-reference.py", "data-steward", "frag.md", _DEGRADED])

        assert code == 0
        assert capsys.readouterr().out == "live\n"


@pytest.fixture
def cwd_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Enter a cwd holding a ``scientist`` sidecar under ``plugins/cc_research``, no plugin root, an empty home."""
    sidecar = tmp_path / "plugins" / "cc_research" / "references" / "scientist"
    sidecar.mkdir(parents=True)
    (sidecar / "frag.md").write_text("from cwd\n", encoding="utf-8", newline="\n")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestCwdFallback:
    """The cwd-relative source-tree fallback used when ``CLAUDE_PLUGIN_ROOT`` is unset."""

    def test_notice_names_the_unset_root(self, cwd_tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Inside the research source repository, reading ``plugins/cc_research`` from the cwd is announced on stderr.

        Even there the cwd tier is local-dev only, so the fallback must not be silent.
        """
        manifest = cwd_tree / "plugins" / "cc_research" / ".claude-plugin" / "plugin.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text('{"name": "research"}', encoding="utf-8")

        code = main(["load-agent-reference.py", "scientist", "frag.md", _DEGRADED])

        captured = capsys.readouterr()
        assert code == 0
        assert captured.out == "from cwd\n"
        assert "CLAUDE_PLUGIN_ROOT unset" in captured.err

    def test_untrusted_cwd_tree_blocks_without_install(
        self, cwd_tree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A cwd tree without research's own manifest is never read; with no install the loader blocks.

        A repository under analysis can plant ``plugins/cc_research/references/<agent>``; its fragments would be pasted
        into the agent's prompt verbatim.
        """
        code = main(["load-agent-reference.py", "scientist", "frag.md", _DEGRADED])

        captured = capsys.readouterr()
        assert code == 1
        assert captured.out == ""
        assert "not the research plugin source repository" in captured.err
        assert "! BLOCKED" in captured.err

    def test_untrusted_cwd_tree_falls_through_to_cache(
        self, cwd_tree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A cwd tree without research's own manifest is skipped in favour of the installed cache copy."""
        cached = cwd_tree / "home" / ".claude" / "plugins" / "cache" / "market" / "research" / "0.1.0"
        (cached / "references" / "scientist").mkdir(parents=True)
        (cached / "references" / "scientist" / "frag.md").write_text("from cache\n", encoding="utf-8", newline="\n")

        code = main(["load-agent-reference.py", "scientist", "frag.md", _DEGRADED])

        assert code == 0
        assert capsys.readouterr().out == "from cache\n"
