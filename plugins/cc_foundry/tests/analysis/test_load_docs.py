"""Tests for the two doc-loading bin/ scripts.

Covers ``bin/load_mode.py`` (skill ``modes/``/``templates/`` loader) and ``bin/load_shared_doc.py`` (plugin
``skills/_shared`` loader). Both wrap an existing resolver and add file emission, so the tests focus on the contract the
call sites depend on: byte-exact stdout, ``! BREAKING`` on stdout, and exit codes.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

BIN_DIR = Path(__file__).resolve().parent.parent.parent / "bin"


def _load(name: str) -> ModuleType:
    """Import a ``bin/`` script by file name.

    Args:
        name: Script stem (e.g. ``load_mode``).

    Returns:
        The imported module.
    """
    if str(BIN_DIR) not in sys.path:
        sys.path.insert(0, str(BIN_DIR))
    spec = importlib.util.spec_from_file_location(name, BIN_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


load_mode = _load("load_mode")
load_shared_doc = _load("load_shared_doc")


@pytest.fixture(name="fake_plugin")
def _fake_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build a minimal plugin tree and point ``CLAUDE_PLUGIN_ROOT`` at it.

    Args:
        tmp_path: pytest temp dir.
        monkeypatch: pytest monkeypatch fixture.

    Returns:
        The plugin root directory.
    """
    root = tmp_path / "plugin_install"
    modes = root / "skills" / "demo" / "modes"
    modes.mkdir(parents=True)
    # write_bytes, not write_text: text mode translates "\n" to CRLF on Windows, which would
    # make the byte-exact assertions below test the fixture's newline handling, not the loader's.
    (modes / "one.md").write_bytes(b"mode body\n")
    shared = root / "skills" / "_shared"
    shared.mkdir(parents=True)
    (shared / "doc.md").write_bytes(b"shared body\n")
    (root / ".claude-plugin").mkdir()
    (root / ".claude-plugin" / "plugin.json").write_text('{"name": "foundry"}', encoding="utf-8")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))
    monkeypatch.chdir(tmp_path)
    return root


def test_load_mode_emits_file_verbatim(fake_plugin: Path, capsysbinary: pytest.CaptureFixture[bytes]) -> None:
    """Resolved file is written to stdout byte-for-byte, exit 0."""
    assert load_mode.main(["demo", "modes", "one.md"]) == 0
    assert capsysbinary.readouterr().out == b"mode body\n"


def test_load_mode_passes_crlf_through_unchanged(fake_plugin: Path, capsysbinary: pytest.CaptureFixture[bytes]) -> None:
    """A CRLF file emits CRLF — the loader is a byte pipe, never a newline translator."""
    (fake_plugin / "skills" / "demo" / "modes" / "crlf.md").write_bytes(b"line one\r\nline two\r\n")

    assert load_mode.main(["demo", "modes", "crlf.md"]) == 0

    assert capsysbinary.readouterr().out == b"line one\r\nline two\r\n"


def test_load_mode_missing_subdir_breaks(fake_plugin: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Report an unresolvable mode directory and exit with status 1.

    The stdout diagnostic begins with ``! BREAKING``.
    """
    assert load_mode.main(["demo", "nosuchdir", "one.md"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "! BREAKING: demo/nosuchdir not found — run /foundry:setup first\n"
    assert captured.err == ""


def test_load_mode_missing_file_breaks(fake_plugin: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Resolved dir but absent file also breaks with exit 1."""
    assert load_mode.main(["demo", "modes", "absent.md"]) == 1
    assert capsys.readouterr().out.startswith("! BREAKING: absent.md not found in ")


def test_load_mode_rejects_path_separator(fake_plugin: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Traversal in the file argument is an argument error (exit 2), not a load."""
    assert load_mode.main(["demo", "modes", "../x.md"]) == 2
    assert "plain file name" in capsys.readouterr().err


def test_load_mode_fallback_source_finds_sibling_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve this install's own skills dir when the cascade misses."""
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    assert load_mode.source_fallback_dir("audit", "modes") is not None
    assert load_mode.source_fallback_dir("__nonexistent__", "modes") is None


def test_load_shared_doc_emits_file_verbatim(fake_plugin: Path, capsysbinary: pytest.CaptureFixture[bytes]) -> None:
    """Shared doc is written to stdout byte-for-byte, exit 0."""
    assert load_shared_doc.main(["foundry", "skills/_shared", "doc.md"]) == 0
    assert capsysbinary.readouterr().out == b"shared body\n"


def test_load_shared_doc_missing_file_breaks(fake_plugin: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Absent shared doc breaks with exit 1 rather than a raw cat error."""
    assert load_shared_doc.main(["foundry", "skills/_shared", "absent.md"]) == 1
    assert capsys.readouterr().out.startswith("! BREAKING: absent.md not found in ")


def test_load_shared_doc_rejects_path_separator(fake_plugin: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """File argument validation is shared with ``load_mode`` — no duplicated rule."""
    assert load_shared_doc.main(["foundry", "skills/_shared", "sub/doc.md"]) == 2
    assert "plain file name" in capsys.readouterr().err
