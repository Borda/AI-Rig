"""Test host manifest agreement and committed version continuity."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

_MOD_PATH = Path(__file__).resolve().parent.parent.parent / "bin" / "check_plugin_version_sync.py"
_spec = importlib.util.spec_from_file_location("check_plugin_version_sync", _MOD_PATH)
assert _spec
assert _spec.loader
vs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vs)


def _committed_file(tmp_path: Path, relative_path: str, content: str) -> Path:
    """Create a committed source baseline and return its path for CLI checks."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    path = tmp_path / relative_path
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", relative_path], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "baseline",
        ],
        check=True,
    )
    return path


def _committed_json(tmp_path: Path, relative_path: str, data: dict) -> Path:
    """Create a committed JSON baseline for a shipped plugin file."""
    return _committed_file(tmp_path, relative_path, json.dumps(data))


def _plugin(root: Path, name: str, claude: str | None, codex: str | None) -> Path:
    """Create a plugin dir with the requested host manifests (None = omit that host)."""
    plugin = root / name
    if claude is not None:
        (plugin / ".claude-plugin").mkdir(parents=True)
        (plugin / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": name, "version": claude}), encoding="utf-8"
        )
    if codex is not None:
        (plugin / ".codex-plugin").mkdir(parents=True)
        (plugin / ".codex-plugin" / "plugin.json").write_text(
            json.dumps({"name": name, "version": codex}), encoding="utf-8"
        )
    return plugin


@pytest.mark.packaging
class TestFindDesyncs:
    """Flag disagreeing dual-manifest pairs only."""

    def test_matching_pair_is_clean(self, tmp_path: Path) -> None:
        """A dual-host plugin whose manifests agree produces no findings.

        The everyday post-bump state: both manifests were bumped together, so
        the gate must stay silent.
        """
        _plugin(tmp_path, "dual", "1.2.3", "1.2.3")
        assert vs.find_desyncs(tmp_path) == []

    def test_mismatch_is_reported_with_both_versions(self, tmp_path: Path) -> None:
        """A version bump applied to one host manifest only is flagged, naming both values.

        This is the incident shape: codemap-py's Claude manifest was bumped to
        0.31.3 while the Codex manifest stayed 0.31.2 — two installs claiming
        different releases of identical code.
        """
        _plugin(tmp_path, "dual", "0.31.3", "0.31.2")
        findings = vs.find_desyncs(tmp_path)
        assert len(findings) == 1
        assert "0.31.3" in findings[0]
        assert "0.31.2" in findings[0]

    def test_single_host_plugins_ignored(self, tmp_path: Path) -> None:
        """Plugins shipping only one host manifest are out of scope.

        Most plugins are Claude-only (or Codex-only, like codex-rig); they have no counterpart to desync from and must
        not produce noise.
        """
        _plugin(tmp_path, "claude-only", "9.9.9", None)
        _plugin(tmp_path, "codex-only", None, "8.8.8")
        assert vs.find_desyncs(tmp_path) == []

    def test_missing_version_field_is_flagged(self, tmp_path: Path) -> None:
        """A dual-host manifest without a ``version`` string is a finding, not a pass.

        Silently treating an absent field as matching would let a malformed manifest disable the gate exactly when it is
        needed.
        """
        plugin = _plugin(tmp_path, "dual", "1.0.0", "1.0.0")
        (plugin / ".codex-plugin" / "plugin.json").write_text(json.dumps({"name": "dual"}), encoding="utf-8")
        findings = vs.find_desyncs(tmp_path)
        assert len(findings) == 1
        assert ".codex-plugin" in findings[0]

    def test_unparseable_manifest_is_flagged(self, tmp_path: Path) -> None:
        """Invalid JSON in either manifest is a finding rather than a crash or a pass.

        The checker runs in pre-commit; a corrupt file must fail the gate with a location, never traceback.
        """
        plugin = _plugin(tmp_path, "dual", "1.0.0", "1.0.0")
        (plugin / ".claude-plugin" / "plugin.json").write_text("{not json", encoding="utf-8")
        findings = vs.find_desyncs(tmp_path)
        assert len(findings) == 1
        assert ".claude-plugin" in findings[0]


@pytest.mark.packaging
class TestMain:
    """CLI exit codes mirror the findings."""

    @pytest.mark.parametrize(
        ("claude", "codex", "expected"),
        [pytest.param("1.0.0", "1.0.0", 0, id="in-sync"), pytest.param("1.0.1", "1.0.0", 1, id="desynced")],
    )
    def test_exit_codes(self, tmp_path: Path, capsys, claude: str, codex: str, expected: int) -> None:
        """Exit 0 when every pair agrees, 1 on any desync (with a VERSION-DESYNC line).

        pre-commit keys purely on the exit code; the printed finding is what the committer acts on.
        """
        _plugin(tmp_path, "dual", claude, codex)
        rc = vs.main(["--scan-dir", str(tmp_path)])
        out = capsys.readouterr().out
        assert rc == expected
        assert ("VERSION-DESYNC" in out) == bool(expected)

    def test_real_repo_scan_is_clean(self) -> None:
        """The actual repository passes — bridge and codemap-py pairs agree.

        Guards the live tree: a merge that desyncs a real pair fails here even
        before the pre-commit hook runs.
        """
        repo_plugins = Path(__file__).resolve().parents[4] / "plugins"
        assert vs.find_desyncs(repo_plugins) == []


@pytest.mark.packaging
class TestHeadContinuity:
    """Version fields use the same field in committed HEAD as their baseline."""

    @pytest.mark.parametrize(
        ("relative_path", "baseline", "current", "fragment"),
        [
            pytest.param(
                "plugins/example/runtime/contract.json",
                {"schema_version": 1},
                {"schema_version": 3},
                "schema_version",
                id="skipped-integer-version",
            ),
            pytest.param(
                "plugins/example/skills/review/result-template.json",
                {"metadata": {}},
                {"metadata": {"action_contract_version": 3}},
                "metadata.action_contract_version",
                id="new-nested-contract-not-at-one",
            ),
            pytest.param(
                "plugins/example/runtime/contract.json",
                {"payload": {}},
                {"payload": {"version": 3}},
                "payload.version",
                id="generic-numeric-version-field",
            ),
            pytest.param(
                "plugins/example/runtime/contract.json", {"schema": 1}, {"schema": 3}, "schema", id="schema-field"
            ),
            pytest.param(
                "plugins/example/runtime/contract.json",
                {"journal_schema": 1},
                {"journal_schema": 3},
                "journal_schema",
                id="journal-schema-field",
            ),
        ],
    )
    def test_json_version_jump_fails(
        self, tmp_path: Path, monkeypatch, capsys, relative_path: str, baseline: dict, current: dict, fragment: str
    ) -> None:
        """A shipped JSON version field that jumps from its committed HEAD value fails and names the field.

        Covers a skipped integer ``schema_version`` (1 to 3), a new nested contract that begins at 3, a generic integer
        ``version`` field that cannot bypass the gate, and a schema field using either shipped naming convention.
        """
        path = _committed_json(tmp_path, relative_path, baseline)
        path.write_text(json.dumps(current), encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins", str(path.relative_to(tmp_path))]) == 1
        assert fragment in capsys.readouterr().out

    @pytest.mark.parametrize(
        ("current", "expected"),
        [pytest.param(2, 0, id="next-protocol"), pytest.param(3, 1, id="skipped-protocol")],
    )
    def test_nested_json_protocol_continuity(
        self, tmp_path: Path, monkeypatch, capsys, current: int, expected: int
    ) -> None:
        """A shipped nested integer protocol advances at most once from HEAD."""
        path = _committed_json(tmp_path, "plugins/example/package-manifest.json", {"bootstrap": {"protocol": 1}})
        path.write_text(json.dumps({"bootstrap": {"protocol": current}}), encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins", str(path.relative_to(tmp_path))]) == expected
        if expected:
            assert "bootstrap.protocol" in capsys.readouterr().out

    @pytest.mark.parametrize(
        ("current", "expected"),
        [pytest.param(2, 0, id="next"), pytest.param(3, 1, id="skip")],
    )
    def test_json_protocol_suffix_continuity(
        self, tmp_path: Path, monkeypatch, capsys, current: int, expected: int
    ) -> None:
        """A shipped integer field ending in `_protocol` keeps its HEAD baseline."""
        path = _committed_json(tmp_path, "plugins/example/runtime/contract.json", {"bootstrap_protocol": 1})
        path.write_text(json.dumps({"bootstrap_protocol": current}), encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins", str(path.relative_to(tmp_path))]) == expected
        if expected:
            assert "bootstrap_protocol" in capsys.readouterr().out

    def test_removed_dual_host_manifest_fails_without_selected_files(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """A removed tracked host manifest cannot turn a dual-host plugin into a silent single-host one."""
        plugin = _plugin(tmp_path / "plugins", "example", "0.1.0", "0.1.0")
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        subprocess.run(["git", "-C", str(tmp_path), "add", "plugins/example"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(tmp_path),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-qm",
                "baseline",
            ],
            check=True,
        )
        (plugin / ".codex-plugin" / "plugin.json").unlink()
        (plugin / ".codex-plugin").rmdir()
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins"]) == 1
        assert ".codex-plugin" in capsys.readouterr().out

    def test_removed_single_host_manifest_fails_without_selected_files(self, tmp_path: Path, monkeypatch) -> None:
        """Deleting the only tracked release manifest must fail while plugin code remains."""
        manifest = _committed_file(
            tmp_path, "plugins/example/.claude-plugin/plugin.json", json.dumps({"name": "example", "version": "0.1.0"})
        )
        writer = tmp_path / "plugins/example/bin/writer.py"
        writer.parent.mkdir()
        writer.write_text("SCHEMA_VERSION = 1\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(tmp_path), "add", "plugins/example/bin/writer.py"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(tmp_path),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-qm",
                "writer",
            ],
            check=True,
        )
        manifest.unlink()
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins"]) == 1

    @pytest.mark.parametrize(
        ("name", "baseline", "current"),
        [
            pytest.param("_SCHEMA", 2, 4, id="package-schema"),
            pytest.param("PROTOCOL", 1, 3, id="role-link-protocol"),
            pytest.param("SCHEMA_VERSION", 1, 3, id="version-suffix"),
        ],
    )
    def test_shipped_python_schema_families_cannot_skip_head(
        self, tmp_path: Path, monkeypatch, capsys, name: str, baseline: int, current: int
    ) -> None:
        """Known shipped static schema and protocol names obey HEAD continuity."""
        path = _committed_file(tmp_path, "plugins/example/bin/writer.py", f"{name} = {baseline}\n")
        path.write_text(f"{name} = {current}\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins", str(path.relative_to(tmp_path))]) == 1
        assert name in capsys.readouterr().out

    @pytest.mark.parametrize("name", ["_SCHEMA", "PROTOCOL"])
    def test_shipped_python_schema_families_accept_next(self, tmp_path: Path, monkeypatch, name: str) -> None:
        """The extra shipped names accept one increment from committed HEAD."""
        path = _committed_file(tmp_path, "plugins/example/bin/writer.py", f"{name} = 2\n")
        path.write_text(f"{name} = 3\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins", str(path.relative_to(tmp_path))]) == 0

    @pytest.mark.parametrize(
        ("current", "expected"),
        [
            pytest.param(1, 0, id="unchanged"),
            pytest.param(2, 0, id="next"),
            pytest.param(0, 1, id="downgrade"),
            pytest.param(3, 1, id="skip"),
        ],
    )
    def test_python_literal_version_boundaries(self, tmp_path: Path, monkeypatch, current: int, expected: int) -> None:
        """Only the committed integer or its immediate successor is accepted."""
        path = _committed_file(tmp_path, "plugins/example/bin/writer.py", "SCHEMA_VERSION: int = 1\n")
        path.write_text(f"SCHEMA_VERSION: int = {current}\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins", str(path.relative_to(tmp_path))]) == expected

    def test_new_python_version_starts_at_one(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """A new static version family begins at 1 within an existing module."""
        path = _committed_file(tmp_path, "plugins/example/bin/writer.py", "VALUE = 1\n")
        path.write_text("VALUE = 1\nSCHEMA_VERSION = 2\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins", str(path.relative_to(tmp_path))]) == 1
        assert "new version family" in capsys.readouterr().out

    def test_new_python_version_matches_deleted_sibling(self, tmp_path: Path, monkeypatch) -> None:
        """A version constant carried unchanged into a new file passes when its old file is deleted in the same diff.

        Splitting one committed module into several new files loses the constant's own path-keyed HEAD baseline. This
        covers the pure-extraction case: the same plugin still HEAD-tracks a now-deleted file defining the same
        constant at the same value, so the new file is not starting a fresh family.
        """
        old_path = _committed_file(tmp_path, "plugins/example/bin/writer.py", "SCHEMA_VERSION = 2\n")
        old_path.unlink()
        new_path = tmp_path / "plugins/example/bin/writer_pkg/types.py"
        new_path.parent.mkdir(parents=True)
        new_path.write_text("SCHEMA_VERSION = 2\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins", str(new_path.relative_to(tmp_path))]) == 0

    def test_new_python_version_ignores_unrelated_deleted_sibling(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """A deleted sibling's value only excuses an exact match, not an arbitrary new value.

        Confirms the deleted-sibling fallback does not turn into a blanket exemption for new files: a genuinely new
        family still has to start at 1 even when the same plugin lost an unrelated versioned file this diff.
        """
        old_path = _committed_file(tmp_path, "plugins/example/bin/writer.py", "SCHEMA_VERSION = 2\n")
        old_path.unlink()
        new_path = tmp_path / "plugins/example/bin/writer_pkg/types.py"
        new_path.parent.mkdir(parents=True)
        new_path.write_text("SCHEMA_VERSION = 5\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins", str(new_path.relative_to(tmp_path))]) == 1
        assert "new version family" in capsys.readouterr().out

    @pytest.mark.parametrize(
        ("current_source", "fragment"),
        [
            pytest.param("SCHEMA_VERSION = version()\n", "nonliteral version mutation", id="literal-replaced-by-call"),
            pytest.param(
                "SCHEMA_VERSION = 1\nSCHEMA_VERSION = version()\n", "SCHEMA_VERSION", id="later-dynamic-assignment"
            ),
            pytest.param(
                "SCHEMA_VERSION = 1\nSCHEMA_VERSION = version()\nSCHEMA_VERSION = 1\n",
                "SCHEMA_VERSION",
                id="intermediate-dynamic-assignment",
            ),
            pytest.param("SCHEMA_VERSION = 1\nSCHEMA_VERSION += 2\n", "SCHEMA_VERSION", id="augmented-assignment"),
        ],
    )
    def test_nonliteral_version_mutation_fails(
        self, tmp_path: Path, monkeypatch, capsys, current_source: str, fragment: str
    ) -> None:
        """A dynamic or augmented mutation of a committed static version cannot evade the gate.

        Covers replacing the literal with a call, a later dynamic assignment overriding an earlier literal, a later
        literal concealing an intervening dynamic mutation, and a post-literal augmented assignment.
        """
        path = _committed_file(tmp_path, "plugins/example/bin/writer.py", "SCHEMA_VERSION = 1\n")
        path.write_text(current_source, encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins", str(path.relative_to(tmp_path))]) == 1
        assert fragment in capsys.readouterr().out

    @pytest.mark.parametrize(
        ("current", "expected"),
        [pytest.param(2, 0, id="next"), pytest.param(3, 1, id="skip")],
    )
    def test_python_serialized_dict_protocol_continuity(
        self, tmp_path: Path, monkeypatch, capsys, current: int, expected: int
    ) -> None:
        """A literal protocol in a serialized Python dictionary keeps its HEAD baseline."""
        path = _committed_file(
            tmp_path, "plugins/example/bin/writer.py", 'def payload():\n    return {"bootstrap_protocol": 1}\n'
        )
        path.write_text(f'def payload():\n    return {{"bootstrap_protocol": {current}}}\n', encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins", str(path.relative_to(tmp_path))]) == expected
        if expected:
            assert "bootstrap_protocol" in capsys.readouterr().out

    @pytest.mark.parametrize(
        ("baseline", "current"),
        [
            pytest.param(
                'def existing():\n    return {"schema": 2}\n',
                'def existing():\n    return {"schema": 2}\ndef payload():\n    return {"schema": 2}\n',
                id="new-reference-to-existing-format",
            ),
            pytest.param(
                'def old():\n    return {"schema": 2}\n',
                'def new():\n    return {"schema": 1}\ndef old():\n    return {"schema": 2}\n',
                id="inserted-reference-preserves-existing-version",
            ),
            pytest.param(
                'def producer_a():\n    return {"schema": 2}\n',
                'def new_producer():\n    return {"schema": 1}\ndef producer_a():\n    return {"schema": 2}\n',
                id="new-sibling-producer-preserves-baseline",
            ),
            pytest.param(
                'def payload():\n    first = {"schema": 1}\n    second = {"schema": 2}\n    return first, second\n',
                "def payload():\n"
                '    added = {"schema": 1}\n'
                '    first = {"schema": 1}\n'
                '    second = {"schema": 2}\n'
                "    return added, first, second\n",
                id="inserted-named-dict-in-same-function",
            ),
        ],
    )
    def test_inserted_producer_preserves_existing_baselines(
        self, tmp_path: Path, monkeypatch, baseline: str, current: str
    ) -> None:
        """Adding a dictionary, function or named assignment does not shift an existing producer's baseline.

        Covers a new dictionary referring to a format already at version 2, an earlier reference to version 1 preceding
        an unchanged version 2, a newly inserted function, and a new named dictionary inside an existing function.
        """
        path = _committed_file(tmp_path, "plugins/example/bin/writer.py", baseline)
        path.write_text(current, encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins", str(path.relative_to(tmp_path))]) == 0

    def test_new_python_dict_version_family_starts_at_one(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """A new dictionary key cannot borrow another version family's HEAD baseline."""
        baseline = 'def existing():\n    return {"schema": 2}\n'
        path = _committed_file(tmp_path, "plugins/example/bin/writer.py", baseline)
        path.write_text(baseline + 'PAYLOAD = {"fresh_protocol": 9}\n', encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins", str(path.relative_to(tmp_path))]) == 1
        output = capsys.readouterr().out
        assert "fresh_protocol" in output
        assert "new version family must start at 1" in output

    @pytest.mark.parametrize(
        ("baseline", "current", "fragment"),
        [
            pytest.param(
                'def existing():\n    return {"schema": 2}\n',
                'def existing():\n    return {"schema": 2}\ndef payload():\n    return {"schema": 9}\n',
                "schema",
                id="new-scope-cannot-skip-existing-format",
            ),
            pytest.param(
                'def old():\n    return {"bootstrap_protocol": 1}\n',
                'def new():\n    return {"bootstrap_protocol": 1}\ndef old():\n    return {"bootstrap_protocol": 3}\n',
                "bootstrap_protocol",
                id="inserted-reference-cannot-hide-protocol-jump",
            ),
            pytest.param(
                'def producer_a():\n    return {"schema": 1}\ndef producer_b():\n    return {"schema": 2}\n',
                'def producer_a():\n    return {"schema": 3}\ndef producer_b():\n    return {"schema": 2}\n',
                "producer_a",
                id="sibling-producer-cannot-mask-jump",
            ),
            pytest.param(
                'class A:\n    def payload(self):\n        return {"schema": 1}\n'
                'class B:\n    def payload(self):\n        return {"schema": 2}\n',
                'class A:\n    def payload(self):\n        return {"schema": 3}\n'
                'class B:\n    def payload(self):\n        return {"schema": 2}\n',
                "class:A.function:payload",
                id="same-method-name-in-different-classes",
            ),
            pytest.param(
                'def payload(which):\n    if which:\n        return {"schema": 1}\n    return {"schema": 2}\n',
                'def payload(which):\n    if which:\n        return {"schema": 3}\n    return {"schema": 2}\n',
                "ambiguous changed inline version producers",
                id="changed-unnamed-dicts",
            ),
            pytest.param(
                'def payload(which):\n    if which:\n        return {"schema": 1}\n    return {"schema": 2}\n',
                'def payload(which):\n    if which:\n        return {"schema": 2}\n    return {"schema": 1}\n',
                "ambiguous changed inline version producers",
                id="reordered-unnamed-dicts",
            ),
        ],
    )
    def test_inline_producer_jump_or_ambiguity_fails(
        self, tmp_path: Path, monkeypatch, capsys, baseline: str, current: str, fragment: str
    ) -> None:
        """An inline version producer that jumps, or whose change cannot be attributed, fails closed.

        Covers a new dictionary scope claiming a version beyond the existing format's next step, a new earlier
        dictionary taking the old occurrence's baseline, one producer borrowing a sibling's higher baseline, same-named
        methods in different classes keeping separate versions, and duplicate unnamed fields (changed or merely
        reordered) that lack the identity needed to prove the change safe.
        """
        path = _committed_file(tmp_path, "plugins/example/bin/writer.py", baseline)
        path.write_text(current, encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins", str(path.relative_to(tmp_path))]) == 1
        assert fragment in capsys.readouterr().out

    @pytest.mark.parametrize(
        ("current", "expected"),
        [pytest.param(2, 0, id="one-step"), pytest.param(3, 1, id="masked-jump")],
    )
    def test_named_dicts_in_one_function_keep_separate_versions(
        self, tmp_path: Path, monkeypatch, capsys, current: int, expected: int
    ) -> None:
        """A higher sibling version cannot mask a named dictionary's jump."""
        source = 'def payload():\n    first = {"schema": 1}\n    second = {"schema": 2}\n    return first, second\n'
        path = _committed_file(tmp_path, "plugins/example/bin/writer.py", source)
        path.write_text(source.replace('first = {"schema": 1}', f'first = {{"schema": {current}}}'), encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins", str(path.relative_to(tmp_path))]) == expected
        if expected:
            assert "first" in capsys.readouterr().out

    def test_complete_plugin_removal_is_allowed(self, tmp_path: Path, monkeypatch) -> None:
        """Removing an entire plugin does not leave a manifest obligation."""
        manifest = _committed_file(
            tmp_path, "plugins/example/.claude-plugin/plugin.json", json.dumps({"version": "0.1.0"})
        )
        manifest.unlink()
        manifest.parent.rmdir()
        manifest.parent.parent.rmdir()
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins"]) == 0

    @pytest.mark.parametrize(
        ("current", "expected"),
        [
            pytest.param(1, 0, id="new-family-one"),
            pytest.param(2, 1, id="new-family-two"),
        ],
    )
    def test_new_nested_family_boundary(self, tmp_path: Path, monkeypatch, current: int, expected: int) -> None:
        """A new nested field accepts only its first integer version."""
        path = _committed_json(tmp_path, "plugins/example/skills/review/result-template.json", {"metadata": {}})
        path.write_text(json.dumps({"metadata": {"action_contract_version": current}}), encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins", str(path.relative_to(tmp_path))]) == expected

    @pytest.mark.parametrize(
        ("baseline", "current", "expected"),
        [
            pytest.param("0.4.7", "0.4.7", 0, id="unchanged"),
            pytest.param("0.4.7", "0.4.8", 0, id="next-patch"),
            pytest.param("0.4.7", "0.5.0", 0, id="next-minor"),
            pytest.param("0.4.7", "0.4.9", 1, id="skipped-patch"),
            pytest.param("0.4.7", "0.6.0", 1, id="skipped-minor"),
            pytest.param("0.4.7", "0.4.6", 1, id="downgrade"),
            pytest.param("0.59.0", "1.0.0", 0, id="next-major"),
            pytest.param("0.59.0", "1.0.1", 1, id="major-plus-patch"),
            pytest.param("0.59.0", "2.0.0", 1, id="skipped-major"),
        ],
    )
    def test_manifest_semver_continuity(
        self, tmp_path: Path, monkeypatch, baseline: str, current: str, expected: int
    ) -> None:
        """A single-host manifest accepts only unchanged, one patch, one minor, or one major step.

        A major bump resets both lower components and cannot skip a major; a skipped patch or minor and a downgrade
        fail.
        """
        path = _committed_json(tmp_path, "plugins/example/.claude-plugin/plugin.json", {"version": baseline})
        path.write_text(json.dumps({"version": current}), encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        assert vs.main(["--scan-dir", "plugins", str(path.relative_to(tmp_path))]) == expected
