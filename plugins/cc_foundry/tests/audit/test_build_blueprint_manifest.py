"""Tests for build_blueprint_manifest.py — normalization spec, filters, and CLI.

The normalization pipeline is the contract a companion runtime hook must reproduce byte-identically, so it is tested by
explicit vectors rather than only by doctest.
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath, PureWindowsPath

import build_blueprint_manifest as bbm
import pytest

PLUGIN_JSON = '{"name": "foundry", "version": "1.2.3"}\n'

#: Cross-language contract vectors, also executed by ``test_blueprint_allow_js.py``
#: against the JS port in ``hooks/blueprint-allow.js``. A vector passing on only one
#: side means the two hash spaces have diverged.
VECTORS = json.loads(
    (Path(__file__).resolve().parent.parent / "fixtures" / "blueprint_normalization_vectors.json").read_text(
        encoding="utf-8"
    )
)


def _vector_params(group: str) -> list:
    """Return the shared fixture's vectors for ``group`` as one ``pytest.param`` each."""
    return [pytest.param(vector, id=vector["id"]) for vector in VECTORS[group]]


def _make_plugin(scan_dir: Path, name: str, skill_md: str = "", claude_md: str = "") -> Path:
    """Create a minimal plugin tree under ``scan_dir`` and return its directory.

    Examples:
        >>> from tempfile import TemporaryDirectory
        >>> with TemporaryDirectory() as directory:
        ...     plugin = _make_plugin(Path(directory), "demo")
        ...     (plugin.name, (plugin / ".claude-plugin" / "plugin.json").read_text())
        ('demo', '{"name": "foundry", "version": "1.2.3"}\\n')
    """
    plugin_dir = scan_dir / name
    (plugin_dir / ".claude-plugin").mkdir(parents=True)
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text(PLUGIN_JSON, encoding="utf-8")
    if skill_md:
        skill_dir = plugin_dir / "skills" / "demo"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")
    if claude_md:
        (plugin_dir / "CLAUDE.md").write_text(claude_md, encoding="utf-8")
    return plugin_dir


@pytest.fixture(name="scan_dir")
def _scan_dir(tmp_path: Path) -> Path:
    """Scan directory holding all four target plugins; only cc_foundry has content."""
    root = tmp_path / "plugins"
    root.mkdir()
    _make_plugin(
        root,
        "cc_foundry",
        skill_md="# Demo\n\n```bash\necho alpha\necho beta\n```\n",
        claude_md="```bash\nls -la\n```\n",
    )
    for name in ("cc_oss", "cc_develop", "cc_research"):
        _make_plugin(root, name)
    return root


class TestNormalize:
    """Normalize: the four-step pipeline that the runtime hook must mirror exactly."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param("echo a\r\necho b\r\n", "echo a\necho b", id="crlf-to-lf"),
            pytest.param("echo a\recho b", "echo a\necho b", id="lone-cr-to-lf"),
            pytest.param("echo a   \t\necho b  ", "echo a\necho b", id="trailing-whitespace"),
            pytest.param("\n\n\necho a\n\n\n", "echo a", id="leading-trailing-blanks"),
            pytest.param("echo a\n\n\n\n\necho b", "echo a\n\necho b", id="collapse-blank-run"),
            pytest.param("echo a  # note", "echo a", id="trailing-comment"),
            pytest.param("#!/usr/bin/env bash\necho a", "echo a", id="hash-at-line-start-is-comment"),
            pytest.param("echo   a    b", "echo   a    b", id="no-intra-line-collapse"),
            pytest.param('echo "a  b"', 'echo "a  b"', id="no-quote-rewriting"),
            pytest.param("curl http://x#y", "curl http://x#y", id="url-fragment-survives"),
            pytest.param("curl http://x#y  # fetch", "curl http://x#y", id="url-fragment-plus-comment"),
            pytest.param('echo "${V:-#x}"', 'echo "${V:-#x}"', id="parameter-default-hash-survives"),
            pytest.param("echo ${V:-#x}", "echo ${V:-#x}", id="unquoted-parameter-default-survives"),
            pytest.param("echo '# literal'", "echo '# literal'", id="single-quoted-hash-survives"),
            pytest.param('echo "# literal"', 'echo "# literal"', id="double-quoted-hash-survives"),
            pytest.param("echo a\t# tabbed", "echo a", id="tab-before-hash-is-comment"),
            # A ``#`` inside a multi-line quoted string is not treated as a comment.
            pytest.param(
                'echo "line one\n# still inside\nline three"',
                'echo "line one\n# still inside\nline three"',
                id="quote-state-spans-lines",
            ),
            # A lone comment line between commands collapses to exactly one blank line.
            pytest.param("echo a\n# note\necho b", "echo a\n\necho b", id="comment-only-line-leaves-single-blank"),
        ],
    )
    def test_normalize_vectors(self, raw: str, expected: str) -> None:
        """Each documented normalization step, and the quote-aware comment rule, produces the documented result.

        Covers line-ending and whitespace cleanup, blank-run collapsing, and trailing-comment stripping where a ``#`` is
        a comment only when unquoted and at a word start (URL fragments, parameter defaults and quoted hashes survive,
        including a quote spanning several lines), and a lone comment line leaving exactly one blank line.
        """
        assert bbm.normalize(raw) == expected

    def test_spacing_variants_do_not_collide(self) -> None:
        """Step (d): commands differing only in spacing keep distinct digests."""
        assert bbm.sha256_text(bbm.normalize("echo  a")) != bbm.sha256_text(bbm.normalize("echo a"))


class TestSharedVectors:
    """The shared fixture is the cross-language contract; Python must satisfy all of it.

    ``test_blueprint_allow_js.py`` runs the identical vectors against the JS hook, so a change landing on one side alone
    fails here or there rather than silently opening the normalization-asymmetry bug class the manifest design depends
    on avoiding.
    """

    @pytest.mark.parametrize("vector", _vector_params("normalize"))
    def test_normalize(self, vector: dict) -> None:
        """Produce the fixture's expected text for every shared vector."""
        assert bbm.normalize(vector["input"]) == vector["expected"]

    @pytest.mark.parametrize("vector", _vector_params("needs_bailout"))
    def test_needs_bailout(self, vector: dict) -> None:
        """Agree with the fixture on heredocs and unterminated quotes."""
        assert bbm.needs_bailout(vector["input"]) is vector["expected"]

    @pytest.mark.parametrize("vector", _vector_params("split_logical_commands"))
    def test_split_logical_commands(self, vector: dict) -> None:
        """Split exactly as the fixture specifies."""
        assert bbm.split_logical_commands(vector["input"]) == vector["expected"]

    @pytest.mark.parametrize("vector", _vector_params("is_dangerous"))
    def test_is_dangerous(self, vector: dict) -> None:
        """Classify every shared vector as the fixture specifies.

        Classification is the most security-critical function of the pair: a command the
        generator judges safe and the hook judges dangerous merely wastes an allow, but
        the reverse silently ships a destructive command into the manifest that the
        hook's own defence-in-depth layer then declines to catch.
        """
        assert bbm.is_dangerous(vector["input"]) is vector["expected"]


class TestSplitLogicalCommands:
    """split_logical_commands: newline-only splitting with verbatim continuations."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param("a; b && c", ["a; b && c"], id="never-splits-on-operators"),
            pytest.param(
                "cmd \\\n  --flag\nnext",
                ["cmd \\\n  --flag", "next"],
                id="continuation-kept-verbatim",
            ),
            pytest.param("echo a\\\\\necho b", ["echo a\\\\", "echo b"], id="escaped-backslash-is-not-a-continuation"),
        ],
    )
    def test_splits_only_on_newlines(self, text: str, expected: list[str]) -> None:
        """Commands split on newlines only, never on shell operators, with continuations kept verbatim.

        Backslash-newlines are preserved so the text still matches what is sent, and an even run of trailing backslashes
        ends the command instead of continuing it.
        """
        assert bbm.split_logical_commands(text) == expected


class TestBailout:
    """needs_bailout: heredocs and multi-line quotes disable per-command extraction."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param("echo a\necho b", False, id="plain-block"),
            pytest.param("cat <<EOF\nbody\nEOF", True, id="heredoc"),
            pytest.param("cat <<-'EOF'\nbody\nEOF", True, id="dashed-quoted-heredoc"),
            pytest.param('grep <<< "word"', True, id="herestring-over-bails"),
            pytest.param("echo 'one\ntwo'", True, id="multi-line-quote"),
            pytest.param("echo 'one line'", False, id="closed-quote"),
        ],
    )
    def test_bailout_triggers(self, text: str, expected: bool) -> None:
        """Bail-out fires on heredoc markers and unterminated quotes."""
        assert bbm.needs_bailout(text) is expected


class TestDangerFilter:
    """The danger filter's effect on manifest entries.

    Classification itself is asserted by :class:`TestSharedVectors` against the shared cross-language fixture, so the
    same cases bind the JS hook too; what remains here is how a dangerous verdict shapes the entries and the reported
    drop count.
    """

    def test_dropped_count_reported_on_stderr(self, scan_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """The danger-filter drop count is always printed, never silent."""
        skill = scan_dir / "cc_foundry" / "skills" / "demo" / "SKILL.md"
        skill.write_text("```bash\necho safe\nrm -rf x\n```\n", encoding="utf-8")
        assert bbm.main(["--update", "--scan-dir", str(scan_dir)]) == 0
        assert "cc_foundry: 2 entries, 2 dropped by danger filter" in capsys.readouterr().err


class TestHashing:
    """block_entries: whole-block plus per-command digests."""

    def test_block_and_command_entries(self) -> None:
        """A multi-command block contributes one block digest and one per command."""
        entries, _ = bbm.block_entries("echo alpha\necho beta", "f.md:1")
        assert entries[bbm.sha256_text("echo alpha\necho beta")]["kind"] == "block"
        assert entries[bbm.sha256_text("echo alpha")]["kind"] == "command"
        assert entries[bbm.sha256_text("echo beta")]["kind"] == "command"

    @pytest.mark.parametrize(
        ("text", "kinds", "dropped"),
        [
            pytest.param("cat <<EOF\nline one\nline two\nEOF", ["block"], 0, id="heredoc-emits-only-whole-block"),
            pytest.param("echo safe\nrm -rf x", ["command"], 2, id="dangerous-block-keeps-safe-commands"),
        ],
    )
    def test_block_entry_kinds_and_dropped_count(self, text: str, kinds: list[str], dropped: int) -> None:
        """Entry kinds and the danger-filter drop count follow from each entry being judged on its own text.

        A heredoc block yields exactly one ``block`` entry; a dangerous block is dropped while its safe lines survive as
        ``command`` entries, and the number of dropped entries is reported.
        """
        entries, dropped_count = bbm.block_entries(text, "f.md:1")
        assert [record["kind"] for record in entries.values()] == kinds
        assert dropped_count == dropped

    def test_single_command_block_emitted_once_as_block(self) -> None:
        """When block and command text coincide, only the block entry is recorded."""
        entries, _ = bbm.block_entries("echo alpha", "f.md:1")
        assert entries == {bbm.sha256_text("echo alpha"): {"kind": "block", "src": "f.md:1"}}

    def test_src_points_at_opening_fence(self, scan_dir: Path) -> None:
        """Every entry from a block carries that block's opening-fence provenance."""
        manifest, _ = bbm.build_plugin(scan_dir / "cc_foundry")
        entries = manifest["entries"]
        assert isinstance(entries, dict)
        assert entries[bbm.sha256_text("echo beta")]["src"] == "skills/demo/SKILL.md:3"

    def test_non_bash_blocks_ignored(self, tmp_path: Path) -> None:
        """Only blocks detected as bash are hashed."""
        plugin_dir = _make_plugin(tmp_path, "cc_foundry", skill_md="```python\nprint(1)\n```\n")
        manifest, _ = bbm.build_plugin(plugin_dir)
        assert manifest["entries"] == {}


class TestPosixSrc:
    """posix_src: provenance paths are forward-slashed on every host."""

    @pytest.mark.parametrize(
        ("path", "line", "expected"),
        [
            pytest.param(
                PureWindowsPath(r"skills\demo\SKILL.md"), 7, "skills/demo/SKILL.md:7", id="simulated-windows-path"
            ),
            pytest.param(PurePosixPath("agents/curator.md"), 1, "agents/curator.md:1", id="posix-path-unchanged"),
        ],
    )
    def test_provenance_path_is_forward_slashed(self, path, line: int, expected: str) -> None:
        """A Windows-flavoured path emits forward slashes, and a POSIX path passes through untouched."""
        assert bbm.posix_src(path, line) == expected

    def test_simulated_windows_src_is_forward_slashed_in_emitted_json(self) -> None:
        """The encoded manifest contains no backslash in a Windows-derived src."""
        src = bbm.posix_src(PureWindowsPath(r"rules\_full\claude-config.md"), 42)
        payload = bbm.encode_manifest({"schema": 1, "entries": {"deadbeef": {"kind": "block", "src": src}}})
        text = payload.decode("utf-8")
        assert '"src": "rules/_full/claude-config.md:42"' in text
        assert "\\\\" not in text

    def test_generated_manifest_has_no_backslash_src(self, scan_dir: Path) -> None:
        """No src emitted by a real build contains a backslash."""
        manifest, _ = bbm.build_plugin(scan_dir / "cc_foundry")
        entries = manifest["entries"]
        assert isinstance(entries, dict)
        assert all("\\" not in record["src"] for record in entries.values())


class TestManifestShape:
    """Manifest identity and encoding."""

    def test_plugin_label_uses_directory_name(self, scan_dir: Path) -> None:
        """Identity is ``<dir-name>@<version>``, not plugin.json's ``name`` field."""
        assert bbm.plugin_label(scan_dir / "cc_foundry") == "cc_foundry@1.2.3"

    def test_encoding_is_sorted_ascii_bytes_with_newline(self) -> None:
        """Encoding matches the committed-manifest convention exactly."""
        payload = bbm.encode_manifest({"schema": 1, "plugin": "p@1", "entries": {}})
        assert payload.endswith(b"\n")
        assert payload.startswith(b'{\n  "entries": {}')
        assert b"\r" not in payload


class TestCli:
    """Main: ``--check`` / ``--update`` behaviour and determinism."""

    def test_update_writes_all_target_plugins(self, scan_dir: Path) -> None:
        """Verify command-line option behavior.

        ``--update`` creates a manifest for each target plugin.
        """
        assert bbm.main(["--update", "--scan-dir", str(scan_dir)]) == 0
        for name in bbm.TARGET_PLUGINS:
            assert (scan_dir / name / bbm.MANIFEST_NAME).is_file()

    def test_check_passes_after_update(self, scan_dir: Path) -> None:
        """Verify command-line option behavior.

        ``--check`` is clean immediately after ``--update``.
        """
        bbm.main(["--update", "--scan-dir", str(scan_dir)])
        assert bbm.main(["--check", "--scan-dir", str(scan_dir)]) == 0

    def test_check_detects_drift(self, scan_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Verify command-line option behavior.

        ``--check`` fails when a source block changed after the manifest was written.
        """
        bbm.main(["--update", "--scan-dir", str(scan_dir)])
        skill = scan_dir / "cc_foundry" / "skills" / "demo" / "SKILL.md"
        skill.write_text("```bash\necho gamma\n```\n", encoding="utf-8")
        capsys.readouterr()
        assert bbm.main(["--check", "--scan-dir", str(scan_dir)]) == 1
        assert "BLUEPRINT-MANIFEST-DRIFT: cc_foundry" in capsys.readouterr().err

    def test_check_treats_missing_manifest_as_drift(self, scan_dir: Path) -> None:
        """A never-generated manifest is drift, not a pass."""
        assert bbm.main(["--check", "--scan-dir", str(scan_dir)]) == 1

    def test_check_writes_nothing(self, scan_dir: Path) -> None:
        """Verify command-line option behavior.

        ``--check`` never creates or mutates a manifest file.
        """
        bbm.main(["--check", "--scan-dir", str(scan_dir)])
        assert not (scan_dir / "cc_foundry" / bbm.MANIFEST_NAME).exists()

    def test_missing_plugin_directory_fails(self, scan_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A missing target plugin is reported and exits non-zero."""
        for entry in (scan_dir / "cc_oss").iterdir():
            entry.rename(scan_dir / entry.name)
        (scan_dir / "cc_oss").rmdir()
        assert bbm.main(["--update", "--scan-dir", str(scan_dir)]) == 1
        assert "plugin directory not found" in capsys.readouterr().err

    def test_two_runs_are_byte_identical(self, scan_dir: Path) -> None:
        """Regeneration is deterministic — the drift gate depends on it."""
        manifest_path = scan_dir / "cc_foundry" / bbm.MANIFEST_NAME
        bbm.main(["--update", "--scan-dir", str(scan_dir)])
        first = manifest_path.read_bytes()
        bbm.main(["--update", "--scan-dir", str(scan_dir)])
        assert manifest_path.read_bytes() == first

    def test_manifest_json_shape(self, scan_dir: Path) -> None:
        """The written manifest carries schema, plugin identity, and entry records."""
        bbm.main(["--update", "--scan-dir", str(scan_dir)])
        manifest = json.loads((scan_dir / "cc_foundry" / bbm.MANIFEST_NAME).read_text(encoding="utf-8"))
        assert manifest["schema"] == 1
        assert manifest["plugin"] == "cc_foundry@1.2.3"
        assert manifest["entries"][bbm.sha256_text("ls -la")] == {"kind": "block", "src": "CLAUDE.md:1"}

    def test_mode_flag_is_required(self, scan_dir: Path) -> None:
        """Neither ``--check`` nor ``--update`` given is a usage error."""
        with pytest.raises(SystemExit) as excinfo:
            bbm.main(["--scan-dir", str(scan_dir)])
        assert excinfo.value.code == 2
