"""Tests for ``develop/bin/dev_load_shared.py``.

Output contract:

* The named ``skills/_shared`` document's bytes on stdout, unchanged — the same output as the ``cat`` it replaces.
* Directory: the ``dev-shared-<CSID>`` sentinel when it names an existing directory, else the cold resolver's answer.
* Missing document: one ``! BREAKING`` line, exit 1; with ``--optional`` one ``⚠ WARNING … — skipping`` line, exit 0.
* A document name with a path separator or traversal segment exits 2 and reads nothing.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import dev_load_shared
import pytest
from dev_run_dir import write_sentinel


@pytest.fixture(name="shared")
def _shared_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate the sentinel dir and session token, and publish one shared directory through the warm sentinel."""
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    monkeypatch.setenv("CSID", "load-test")
    (tmp_path / "tmp").mkdir()
    shared = tmp_path / "warm" / "skills" / "_shared"
    shared.mkdir(parents=True)
    assert write_sentinel("dev-shared", str(shared)) is not None
    return shared


@pytest.fixture(name="cold")
def _cold_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Stand in for the cold resolver with a separate directory, so a test can tell which tier answered."""
    cold = tmp_path / "cold" / "skills" / "_shared"
    cold.mkdir(parents=True)
    monkeypatch.setattr(dev_load_shared, "resolve_shared_path", lambda: str(cold))
    return cold


class TestOutput:
    """A present document is emitted exactly as stored."""

    def test_emits_bytes_verbatim(self, shared: Path, capsysbinary: pytest.CaptureFixture[bytes]) -> None:
        """CRLF, non-ASCII text and a missing final newline all pass through untouched, as ``cat`` would print them.

        The loaded text is an instruction document the model follows; any rewrite would change what it reads.
        """
        payload = "# Protocol\r\n⚠ keep — exact\nlast line".encode()
        (shared / "plan-inline.md").write_bytes(payload)

        assert dev_load_shared.main(["plan-inline.md"]) == 0

        assert capsysbinary.readouterr().out == payload


class TestResolution:
    """The directory follows the cold start's sentinel, falling back to a cold resolve only when it is unusable."""

    def test_warm_sentinel_wins_over_cold_resolve(
        self, shared: Path, cold: Path, capsysbinary: pytest.CaptureFixture[bytes]
    ) -> None:
        """A migrated block reads the same directory as blocks that still hand ``$_DEV_SHARED`` to other tools."""
        (shared / "doc.md").write_bytes(b"warm\n")
        (cold / "doc.md").write_bytes(b"cold\n")

        assert dev_load_shared.main(["doc.md"]) == 0

        assert capsysbinary.readouterr().out == b"warm\n"

    def test_missing_sentinel_falls_back_to_cold_resolve(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        cold: Path,
        capsysbinary: pytest.CaptureFixture[bytes],
    ) -> None:
        """A block run before the cold start (or after its sentinel was cleaned) still loads from a fresh resolve."""
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.setenv("CSID", "load-test")
        (cold / "doc.md").write_bytes(b"cold\n")

        assert dev_load_shared.main(["doc.md"]) == 0

        assert capsysbinary.readouterr().out == b"cold\n"

    @pytest.mark.parametrize(
        "sentinel_value",
        [
            pytest.param(lambda base: "", id="empty-sentinel"),
            pytest.param(lambda base: str(base / "gone"), id="sentinel-names-missing-dir"),
        ],
    )
    def test_unusable_sentinel_falls_back_to_cold_resolve(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        cold: Path,
        capsysbinary: pytest.CaptureFixture[bytes],
        sentinel_value: Callable[[Path], str],
    ) -> None:
        """An empty sentinel or one naming a vanished directory resolves afresh instead of failing the load.

        The cold start writes an empty value when it cannot resolve, and a plugin update mid-session can remove the
        version directory the sentinel still names.
        """
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.setenv("CSID", "load-test")
        assert write_sentinel("dev-shared", sentinel_value(tmp_path)) is not None
        (cold / "doc.md").write_bytes(b"cold\n")

        assert dev_load_shared.main(["doc.md"]) == 0

        assert capsysbinary.readouterr().out == b"cold\n"

    def test_cold_resolve_without_install_is_the_source_tree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Inside the develop source repository, with no sentinel or install, the directory is the literal fallback."""
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.setenv("CSID", "load-test")
        manifest = tmp_path / "plugins" / "cc_develop" / ".claude-plugin" / "plugin.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text('{"name": "develop"}', encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(dev_load_shared, "resolve_shared_path", lambda: "plugins/cc_develop/skills/_shared")

        assert dev_load_shared.shared_dir() == Path("plugins") / "cc_develop" / "skills" / "_shared"

    @pytest.mark.parametrize("flags", [pytest.param([], id="required"), pytest.param(["--optional"], id="optional")])
    def test_untrusted_cwd_source_tree_breaks(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsysbinary: pytest.CaptureFixture[bytes],
        flags: list[str],
    ) -> None:
        """A cold resolve that lands on a cwd tree without develop's own manifest exits 1 and never prints it.

        A repository under analysis can plant ``plugins/cc_develop/skills/_shared/<doc>``; loading it would hand that
        repository's text to the model as protocol. ``--optional`` does not soften this: the doc exists, it is refused.
        """
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.setenv("CSID", "load-test")
        planted = tmp_path / "plugins" / "cc_develop" / "skills" / "_shared"
        planted.mkdir(parents=True)
        (planted / "doc.md").write_bytes(b"planted\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(dev_load_shared, "resolve_shared_path", lambda: "plugins/cc_develop/skills/_shared")

        assert dev_load_shared.main([*flags, "doc.md"]) == 1

        out = capsysbinary.readouterr().out
        assert out.startswith(b"! BREAKING")
        assert b"planted" not in out
        assert b"not the develop plugin source repository" in out


class TestMissingDocument:
    """A document that cannot be read is reported on stdout, where the calling step sees it."""

    def test_required_document_breaks(self, shared: Path, capsysbinary: pytest.CaptureFixture[bytes]) -> None:
        """Exit 1 with one ``!

        BREAKING`` line naming the document, its directory, and the reinstall remedy.
        """
        expected = f"! BREAKING: absent.md not found in {shared.as_posix()} — reinstall develop@borda-ai-rig\n"

        assert dev_load_shared.main(["absent.md"]) == 1

        assert capsysbinary.readouterr().out.decode() == expected

    def test_optional_document_warns_and_skips(self, shared: Path, capsysbinary: pytest.CaptureFixture[bytes]) -> None:
        """``--optional`` turns the same miss into one warning line and exit 0, so the caller's skip path continues."""
        expected = f"⚠ WARNING: absent.md not found in {shared.as_posix()} — skipping\n"

        assert dev_load_shared.main(["--optional", "absent.md"]) == 0

        assert capsysbinary.readouterr().out.decode() == expected

    def test_directory_named_as_document_is_missing(
        self, shared: Path, capsysbinary: pytest.CaptureFixture[bytes]
    ) -> None:
        """A directory in place of the document fails like a missing file instead of raising a traceback."""
        (shared / "nested.md").mkdir()

        assert dev_load_shared.main(["nested.md"]) == 1

        assert capsysbinary.readouterr().out.startswith(b"! BREAKING: nested.md not found")


class TestDocumentName:
    """Only a plain file name is accepted, so a load can never leave the shared directory."""

    @pytest.mark.parametrize(
        "name",
        [
            pytest.param("../SKILL.md", id="parent-traversal"),
            pytest.param("sub/doc.md", id="posix-separator"),
            pytest.param("sub\\doc.md", id="windows-separator"),
            pytest.param("..", id="dot-dot"),
            pytest.param("", id="empty"),
        ],
    )
    def test_non_plain_name_is_an_argument_error(
        self, shared: Path, capsys: pytest.CaptureFixture[str], name: str
    ) -> None:
        """Exit 2 with the reason on stderr and nothing on stdout, even when the escaped target exists."""
        (shared.parent / "SKILL.md").write_text("outside\n", encoding="utf-8")

        assert dev_load_shared.main([name]) == 2

        captured = capsys.readouterr()
        assert captured.out == ""
        assert "dev_load_shared:" in captured.err

    def test_missing_argument_exits_through_argparse(self) -> None:
        """No document name is a usage error, not an empty load."""
        with pytest.raises(SystemExit) as excinfo:
            dev_load_shared.main([])
        assert excinfo.value.code == 2
