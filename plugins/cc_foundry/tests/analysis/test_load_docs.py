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
    assert spec is not None
    assert spec.loader is not None
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


_SHARED_ARGS = ["foundry", "skills/_shared"]


class TestLoadersEmit:
    """Both loaders are byte pipes: the resolved file reaches stdout unchanged with exit 0."""

    @pytest.mark.parametrize(
        ("module", "args", "expected"),
        [
            pytest.param(load_mode, ["demo", "modes", "one.md"], b"mode body\n", id="load-mode"),
            pytest.param(load_shared_doc, [*_SHARED_ARGS, "doc.md"], b"shared body\n", id="load-shared-doc"),
        ],
    )
    def test_emits_file_verbatim(
        self,
        fake_plugin: Path,
        capsysbinary: pytest.CaptureFixture[bytes],
        module: ModuleType,
        args: list[str],
        expected: bytes,
    ) -> None:
        """The resolved file is written to stdout byte-for-byte and the loader exits 0.

        Call sites cat the output straight into the agent's context, so any byte the loader adds or drops is visible.
        """
        assert module.main(args) == 0
        assert capsysbinary.readouterr().out == expected

    def test_load_mode_passes_crlf_through_unchanged(
        self, fake_plugin: Path, capsysbinary: pytest.CaptureFixture[bytes]
    ) -> None:
        """A CRLF file emits CRLF: the loader is a byte pipe, never a newline translator."""
        (fake_plugin / "skills" / "demo" / "modes" / "crlf.md").write_bytes(b"line one\r\nline two\r\n")
        assert load_mode.main(["demo", "modes", "crlf.md"]) == 0
        assert capsysbinary.readouterr().out == b"line one\r\nline two\r\n"


class TestLoadersReject:
    """Failure contracts shared by both loaders."""

    @pytest.mark.parametrize(
        ("module", "args"),
        [
            pytest.param(load_mode, ["demo", "modes", "absent.md"], id="load-mode"),
            pytest.param(load_shared_doc, [*_SHARED_ARGS, "absent.md"], id="load-shared-doc"),
        ],
    )
    def test_missing_file_breaks(
        self, fake_plugin: Path, capsys: pytest.CaptureFixture[str], module: ModuleType, args: list[str]
    ) -> None:
        """A resolved directory without the requested file prints ``! BREAKING`` on stdout and exits 1.

        Without the flag a missing doc must fail loudly instead of leaving a raw ``cat`` error in the agent's context.
        """
        assert module.main(args) == 1
        assert capsys.readouterr().out.startswith("! BREAKING: absent.md not found in ")

    @pytest.mark.parametrize(
        ("module", "args"),
        [
            pytest.param(load_mode, ["demo", "modes", "../x.md"], id="load-mode"),
            pytest.param(load_shared_doc, [*_SHARED_ARGS, "sub/doc.md"], id="load-shared-doc"),
            pytest.param(load_shared_doc, ["--optional", *_SHARED_ARGS, "sub/doc.md"], id="optional-is-no-escape"),
        ],
    )
    def test_path_separator_is_an_argument_error(
        self, fake_plugin: Path, capsys: pytest.CaptureFixture[str], module: ModuleType, args: list[str]
    ) -> None:
        """A path separator in the file argument exits 2 and never loads, even under ``--optional``.

        Traversal is a caller bug; ``--optional`` softens only missing docs, not malformed arguments.
        """
        assert module.main(args) == 2
        assert "plain file name" in capsys.readouterr().err


class TestLoadMode:
    """Directory resolution specific to the skill ``modes/``/``templates/`` loader."""

    def test_missing_subdir_breaks(self, fake_plugin: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """An unresolvable mode directory exits 1 with a ``!

        BREAKING`` diagnostic on stdout and nothing on stderr.
        """
        assert load_mode.main(["demo", "nosuchdir", "one.md"]) == 1
        captured = capsys.readouterr()
        assert (captured.out, captured.err) == ("! BREAKING: demo/nosuchdir not found — run /foundry:setup first\n", "")

    @pytest.mark.parametrize(
        ("skill", "found"),
        [pytest.param("audit", True, id="existing-skill"), pytest.param("__nonexistent__", False, id="unknown-skill")],
    )
    def test_fallback_source_finds_sibling_tree(self, monkeypatch: pytest.MonkeyPatch, skill: str, found: bool) -> None:
        """Without ``CLAUDE_PLUGIN_ROOT`` the loader falls back to this install's skills dir when the skill exists."""
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        assert (load_mode.source_fallback_dir(skill, "modes") is not None) is found


class TestOptional:
    """``--optional`` turns every missing-doc case into one warning line with exit 0: warn-and-skip."""

    def test_present_doc_is_emitted_verbatim(
        self, fake_plugin: Path, capsysbinary: pytest.CaptureFixture[bytes]
    ) -> None:
        """A doc that exists loads exactly as without the flag: bytes on stdout, nothing else."""
        assert load_shared_doc.main(["--optional", *_SHARED_ARGS, "doc.md"]) == 0
        captured = capsysbinary.readouterr()
        assert (captured.out, captured.err) == (b"shared body\n", b"")

    def test_missing_file_warns_and_exits_zero(self, fake_plugin: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """An absent file prints one warning line and no doc: the caller's skip path, not a failure."""
        assert load_shared_doc.main(["--optional", *_SHARED_ARGS, "absent.md"]) == 0
        shared_dir = (fake_plugin / "skills" / "_shared").as_posix()
        assert capsys.readouterr().out == f"⚠ WARNING: absent.md not found at {shared_dir} — skipping\n"

    @pytest.mark.parametrize(
        ("plugin", "subdir"),
        [
            pytest.param("foundry", "skills/nosuchdir", id="missing-subdir"),
            pytest.param("nosuchplugin", "skills/_shared", id="missing-plugin"),
        ],
    )
    def test_missing_dir_or_plugin_warns_and_exits_zero(
        self, fake_plugin: Path, capsys: pytest.CaptureFixture[str], plugin: str, subdir: str
    ) -> None:
        """An unresolvable plugin or directory prints the same single-line warning, exit 0, no doc output."""
        assert load_shared_doc.main(["--optional", plugin, subdir, "doc.md"]) == 0
        assert capsys.readouterr().out == f"⚠ WARNING: {plugin}/{subdir} not found — skipping doc.md\n"
